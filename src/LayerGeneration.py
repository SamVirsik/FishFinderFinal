"""
Stateless bathymetric raster fetching for the live tile pipeline.

This module is data-only: it knows how to talk to NOAA's ImageServer
endpoints, how to translate XYZ tile coords into Web Mercator bboxes,
and how to disk-cache decoded float32 elevation grids. It does NOT
colourise — every analysis runs in the browser worker, so the server's
job is just to hand over numbers.

Per-tile pipeline:
  1. Compute the tile's bbox in Web Mercator (EPSG:3857) for (z, x, y),
     expanded by BUFFER_PX pixels on each side.
  2. Fetch the buffered float32 elevation raster from NOAA at the matching
     pixel size in EPSG:3857. Disk-cache the bytes by (source, res, z, x, y).
  3. Replace sentinel nodata with NaN.
  4. Compute the *real* ground sample distance (metres/pixel) for this tile,
     accounting for Mercator stretch via the centre latitude. The browser
     side passes this into every gradient/Gaussian analysis so slope,
     hillshade, roughness etc. are scale-correct at every zoom.

The buffer-and-crop step (cropping happens in the worker) is the standard
fix for tile-edge seams in any neighbourhood-based terrain analysis.

A module-level `requests.Session` keeps HTTPS connections to NOAA alive
across calls — TLS handshake is the dominant cost on a cold tile, so
reusing connections shaves ~100-200 ms off every fetch after the first.
"""

import math
import os
import threading
from functools import lru_cache
from io import BytesIO

import numpy as np
import requests
from requests.adapters import HTTPAdapter
import tifffile
from PIL import Image


HTTP_TIMEOUT_S = 30
SAMPLE_TIMEOUT_S = 8
OUTPUT_TILE_PX = 256
BUFFER_PX = 16
NOAA_CONCURRENCY = 6

# Web Mercator constants.
_R = 6378137.0
_ORIGIN = math.pi * _R  # ~20037508.342789244 m

_noaa_semaphore = threading.BoundedSemaphore(NOAA_CONCURRENCY)


# ---------------------------------------------------------------------------
# Shared HTTP session — keep-alive + bounded connection pool. Sized to a
# little above the NOAA concurrency cap so the semaphore is the limit, not
# the connection pool.
# ---------------------------------------------------------------------------
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=NOAA_CONCURRENCY,
                       pool_maxsize=NOAA_CONCURRENCY * 2,
                       max_retries=0)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)


# ---------------------------------------------------------------------------
# Web Mercator <-> tile / lat-lon helpers
# ---------------------------------------------------------------------------

def tile_bbox_mercator(z: int, x: int, y: int):
    """(xmin, ymin, xmax, ymax) in EPSG:3857 metres for an XYZ tile."""
    n = 2 ** z
    span = 2 * _ORIGIN / n
    xmin = -_ORIGIN + x * span
    xmax = xmin + span
    ymax = _ORIGIN - y * span
    ymin = ymax - span
    return xmin, ymin, xmax, ymax


def expand_bbox(bbox, frac: float):
    """Symmetric outward expansion of a bbox by a fraction of its size."""
    xmin, ymin, xmax, ymax = bbox
    dx = (xmax - xmin) * frac
    dy = (ymax - ymin) * frac
    return xmin - dx, ymin - dy, xmax + dx, ymax + dy


def _mercator_y_to_lat(y_merc: float) -> float:
    """Inverse Web Mercator y → latitude in degrees."""
    return math.degrees(math.atan(math.sinh(y_merc / _R)))


def true_cellsize_m(bbox_mercator, fetch_size_px: int) -> float:
    """
    Real ground sample distance in metres/pixel for a tile.

    Web Mercator distances are stretched by sec(lat); the true metric distance
    along a horizontal at latitude φ is `mercator_distance * cos(φ)`. We use
    the centre latitude of the bbox as the representative for the whole tile
    — a reasonable approximation since tiles are small relative to the curve.
    """
    xmin, ymin, xmax, ymax = bbox_mercator
    centre_lat = _mercator_y_to_lat(0.5 * (ymin + ymax))
    span_m_horizontal = (xmax - xmin) * math.cos(math.radians(centre_lat))
    return max(span_m_horizontal / fetch_size_px, 1e-3)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

# Endpoint + per-source query overrides. All sources are requested at F32 in
# EPSG:3857, so the only differences are the URL and the noData/rendering hints.
_SOURCE_SPEC = {
    'dem-tiles': dict(
        url=('https://gis.ngdc.noaa.gov/arcgis/rest/services/'
             'DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage'),
        params={'pixelType': 'F32', 'noData': -9999,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'bag-bathymetry': dict(
        url='https://gis.ngdc.noaa.gov/arcgis/rest/services/bag_bathymetry/ImageServer/exportImage',
        params={'pixelType': 'F32',
                'renderingRule': '{"rasterFunction":"None"}',
                'noData': 1000000, 'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'multibeam': dict(
        url='https://gis.ngdc.noaa.gov/arcgis/rest/services/multibeam_mosaic/ImageServer/exportImage',
        params={'pixelType': 'F32', 'noData': -32768,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'crm-mosaic': dict(
        url='https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/CRM_mosaic/ImageServer/exportImage',
        params={'pixelType': 'F32', 'noData': -9999,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'dem-all': dict(
        url='https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/exportImage',
        params={'pixelType': 'F32', 'noData': -9999,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'dem-global': dict(
        url='https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage',
        params={'pixelType': 'F32', 'noData': -9999,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
    'fknms-multibeam': dict(
        url=('https://gis.ngdc.noaa.gov/arcgis/rest/services/'
             'nccos/FKNMS_multibeam_dem/ImageServer/exportImage'),
        params={'pixelType': 'F32', 'noData': -32768,
                'noDataInterpretation': 'esriNoDataMatchAny'},
    ),
}

DEFAULT_SOURCE = 'dem-tiles'


def _source(name: str):
    return _SOURCE_SPEC.get(name) or _SOURCE_SPEC[DEFAULT_SOURCE]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_raster_bytes(source: str, bbox_mercator, size_px: int):
    """GET an EPSG:3857 raster from NOAA. Returns raw image bytes or None."""
    spec = _source(source)
    xmin, ymin, xmax, ymax = bbox_mercator
    params = {
        **spec['params'],
        'bbox': f"{xmin},{ymin},{xmax},{ymax}",
        'bboxSR': 3857,
        'imageSR': 3857,
        'size': f"{size_px},{size_px}",
        'format': 'tiff',
        'interpolation': 'RSP_BilinearInterpolation',
        'f': 'image',
    }

    with _noaa_semaphore:
        try:
            resp = _session.get(spec['url'], params=params,
                                timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as e:
            print(f"[NOAA] {source}: request failed: {e}")
            return None

    if resp.status_code != 200:
        print(f"[NOAA] {source}: HTTP {resp.status_code}")
        return None
    if 'image' not in resp.headers.get('Content-Type', ''):
        print(f"[NOAA] {source}: unexpected content-type "
              f"{resp.headers.get('Content-Type')!r}")
        return None
    return resp.content


def _decode_raster(raw_bytes, expected_size: int):
    """
    Decode TIFF bytes -> 2-D float32 ndarray with NaN for nodata.
    Returns None if the response wasn't a usable single-band float raster.

    Uses `tifffile` instead of PIL because NOAA returns LZW/LERC-compressed
    TIFFs that PIL cannot decode (it raises "image file is truncated").
    """
    try:
        arr = tifffile.imread(BytesIO(raw_bytes))
    except Exception as e:
        print(f"[raster] decode failed: {e}")
        return None

    if arr.ndim != 2:
        # Some endpoints can serve RGB previews — not usable as a depth grid.
        return None
    arr = arr.astype(np.float32, copy=False)

    # Resize if NOAA returned slightly-off dimensions (happens on edge-of-
    # coverage requests). Bilinear keeps elevation values continuous.
    if arr.shape != (expected_size, expected_size):
        arr = np.asarray(
            Image.fromarray(arr).resize((expected_size, expected_size),
                                        Image.BILINEAR),
            dtype=np.float32,
        )

    # Coerce sentinel nodata to NaN. NOAA conventions vary by source:
    # -9999, -32768, +/- 1e6, etc. Anything well outside the plausible range
    # for Earth elevation in metres is treated as nodata.
    arr = np.where(np.abs(arr) >= 11000.0, np.nan, arr)
    return arr


def _load_or_fetch(source: str, z: int, x: int, y: int,
                   bbox_mercator, size_px: int, cache_dir: str):
    """Disk-cached float32 raster for a tile bbox. Array or None."""
    cache_path = os.path.join(cache_dir, f"{z}_{x}_{y}.tiff")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'rb') as f:
                arr = _decode_raster(f.read(), size_px)
                if arr is not None:
                    return arr
        except OSError as e:
            print(f"[raster] cache read failed for {cache_path}: {e}")

    raw = _fetch_raster_bytes(source, bbox_mercator, size_px)
    if raw is None:
        return None

    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(cache_path, 'wb') as f:
            f.write(raw)
    except OSError as e:
        print(f"[raster] cache write failed for {cache_path}: {e}")

    return _decode_raster(raw, size_px)


# ---------------------------------------------------------------------------
# Public: get the float32 grid for a tile (with edge buffer for the worker
# to crop after running gradient/Gaussian analyses).
# ---------------------------------------------------------------------------

def fetch_tile_raster(data_source: str, resolution: int,
                      z: int, x: int, y: int,
                      raster_root: str = 'img/raster'):
    """
    Get the raw float32 elevation raster for a tile, including a BUFFER_PX
    margin on every edge so the client can run gradient/Gaussian analyses
    without edge seams. Returns (arr, cellsize_m, buffer_px) or None.
    """
    if z < 0 or z > 22:
        return None

    src_size = max(OUTPUT_TILE_PX, int(resolution))
    fetch_size = src_size + 2 * BUFFER_PX
    buffer_frac = BUFFER_PX / src_size
    fetch_bbox = expand_bbox(tile_bbox_mercator(z, x, y), buffer_frac)

    cache_dir = os.path.join(raster_root, data_source, str(src_size))
    arr = _load_or_fetch(data_source, z, x, y,
                         fetch_bbox, fetch_size, cache_dir)
    if arr is None:
        return None

    cellsize_m = true_cellsize_m(fetch_bbox, fetch_size)
    return arr, cellsize_m, BUFFER_PX


# ---------------------------------------------------------------------------
# Point sampling for click-for-depth
# ---------------------------------------------------------------------------

@lru_cache(maxsize=20_000)
def sample_depth(source: str, lat: float, lon: float):
    """Depth in metres at (lat, lon) for the given source. None on failure."""
    spec = _source(source)
    sample_url = spec['url'].rsplit('/', 1)[0] + '/getSamples'

    params = {
        'geometry': f"{lon},{lat}",
        'geometryType': 'esriGeometryPoint',
        'returnFirstValueOnly': 'true',
        'outFields': 'PixelValue',
        'f': 'json',
    }
    for key in ('renderingRule', 'noData', 'noDataInterpretation'):
        if key in spec['params']:
            params[key] = spec['params'][key]

    try:
        r = _session.get(sample_url, params=params, timeout=SAMPLE_TIMEOUT_S)
        r.raise_for_status()
        samples = r.json().get('samples') or []
        if not samples:
            return None
        v = samples[0].get('value')
        if v is None or v == '':
            return None
        f = float(v)
        if not math.isfinite(f) or abs(f) >= 11000.0:
            return None
        return f
    except Exception as e:
        print(f"[sample_depth] {source}: {e}")
        return None
