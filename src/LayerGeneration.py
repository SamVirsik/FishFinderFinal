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
from io import BytesIO

import numpy as np
import requests
from requests.adapters import HTTPAdapter
import tifffile
from PIL import Image

from src.data_sources import DataSource, get_source


# Sentinel returned by fetch_tile_raster when the caller asks for an
# unregistered source ID. The Flask layer maps it to HTTP 400 so the
# client (or a typo'd curl) gets a clear error instead of a silent
# fallback that mislabels the disk cache.
UNKNOWN_SOURCE = object()


HTTP_TIMEOUT_S = 30
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


def lonlat_to_mercator(lon: float, lat: float):
    """Forward Web Mercator (lon, lat in degrees) → (x, y) in metres."""
    lat_clamped = max(-85.0511, min(85.0511, lat))
    x = math.radians(lon) * _R
    y = _R * math.log(math.tan(math.pi / 4 + math.radians(lat_clamped) / 2))
    return x, y


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
# Fetch
#
# Source-level configuration (URL, nodata sentinel, rendering rule, etc.)
# lives in `src/data_sources.py`. This module is responsible for the per-
# tile mechanics: bbox math, HTTP, disk cache, decode.
# ---------------------------------------------------------------------------

def _build_noaa_params(source: DataSource, bbox_mercator, size_px: int) -> dict:
    """Translate a DataSource + tile bbox into NOAA exportImage query params."""
    xmin, ymin, xmax, ymax = bbox_mercator
    params = {
        'pixelType': source.pixel_type,
        'bbox': f"{xmin},{ymin},{xmax},{ymax}",
        'bboxSR': source.image_sr,
        'imageSR': source.image_sr,
        'size': f"{size_px},{size_px}",
        'format': 'tiff',
        'interpolation': 'RSP_BilinearInterpolation',
        'f': 'image',
    }
    if source.nodata is not None:
        params['noData'] = source.nodata
        params['noDataInterpretation'] = 'esriNoDataMatchAny'
    if source.rendering_rule is not None:
        params['renderingRule'] = source.rendering_rule
    if source.extra_params:
        params.update(source.extra_params)
    return params


def _fetch_raster_bytes(source: DataSource, bbox_mercator, size_px: int):
    """GET an EPSG:3857 raster from NOAA. Returns raw image bytes or None."""
    params = _build_noaa_params(source, bbox_mercator, size_px)

    with _noaa_semaphore:
        try:
            resp = _session.get(source.url, params=params,
                                timeout=source.timeout_s)
        except requests.RequestException as e:
            print(f"[NOAA] {source.id}: request failed: {e}")
            return None

    if resp.status_code != 200:
        print(f"[NOAA] {source.id}: HTTP {resp.status_code}")
        return None
    if 'image' not in resp.headers.get('Content-Type', ''):
        print(f"[NOAA] {source.id}: unexpected content-type "
              f"{resp.headers.get('Content-Type')!r}")
        return None
    return resp.content


def _decode_raster(raw_bytes, expected_size: int, source: DataSource):
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

    # Coerce sentinel nodata to NaN. The |v|>=11000 fallback catches the
    # large-magnitude sentinels (-32768, +/-1e6, ...), but several NOAA
    # sources use -9999, which sits inside the plausible-elevation range
    # and slips through. Masking the source-specific sentinel exactly fixes
    # the "looks like very deep water at no-coverage pixels" artifact and
    # lets the worker's all-nodata-tile detection actually trigger.
    if source.nodata is not None:
        arr = np.where(
            (np.abs(arr) >= 11000.0) | (arr == np.float32(source.nodata)),
            np.nan, arr)
    else:
        arr = np.where(np.abs(arr) >= 11000.0, np.nan, arr)
    return arr


def _load_or_fetch(source: DataSource, z: int, x: int, y: int,
                   bbox_mercator, size_px: int, cache_dir: str):
    """Disk-cached float32 raster for a tile bbox. Array or None.

    Only rasters that decode AND carry at least one real sample are ever
    written to disk. An all-nodata grid is indistinguishable later from
    genuine no-coverage, so persisting it would turn a transient NOAA
    empty into a forever-grey tile: the disk cache never expires, the
    worker reads the all-nodata grid as `empty`, and the client paints an
    opaque blank under that tile slot for good. Keeping empties out of the
    cache leaves the tile retryable — the next visit re-fetches and can
    recover once NOAA serves data again. Same reasoning for undecodable
    bytes (truncated download, HTML/JSON error page, RGB preview): caching
    them just means re-reading and re-rejecting the same garbage forever.
    """
    cache_path = os.path.join(cache_dir, f"{z}_{x}_{y}.tiff")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'rb') as f:
                arr = _decode_raster(f.read(), size_px, source)
            # Trust a cached grid only if it decodes and holds real data.
            # An all-nodata file on disk is either a legacy poisoned entry
            # (written before empties were excluded) or a stale no-coverage
            # result; treating it as a miss lets the tile recover instead of
            # rendering grey forever.
            if arr is not None and not np.isnan(arr).all():
                return arr
        except OSError as e:
            print(f"[raster] cache read failed for {cache_path}: {e}")

    raw = _fetch_raster_bytes(source, bbox_mercator, size_px)
    if raw is None:
        return None

    arr = _decode_raster(raw, size_px, source)
    if arr is None:
        # Undecodable body — transient/garbage. Do NOT persist; return None
        # so the Flask layer reports a retryable failure (HTTP 503).
        return None

    if not np.isnan(arr).all():
        os.makedirs(cache_dir, exist_ok=True)
        try:
            with open(cache_path, 'wb') as f:
                f.write(raw)
        except OSError as e:
            print(f"[raster] cache write failed for {cache_path}: {e}")

    return arr


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
    without edge seams.

    Returns:
        (arr, cellsize_m, buffer_px) on success.
        UNKNOWN_SOURCE                if `data_source` is not in the registry.
        None                          on any other failure (out of zoom range,
                                      NOAA error, decode failure).

    The unknown-source case is split out so the Flask layer can return 400
    instead of 503 — and so that the disk cache directory always comes from
    the resolved registry entry, never from the (possibly bogus) request
    string. That closes the cache-mislabelling bug where `/raster/typo/...`
    used to silently serve dem-tiles bytes but cache them under `typo/`.
    """
    source = get_source(data_source)
    if source is None:
        return UNKNOWN_SOURCE

    if z < source.min_zoom or z > source.max_zoom:
        return None

    src_size = max(OUTPUT_TILE_PX, int(resolution))
    fetch_size = src_size + 2 * BUFFER_PX
    buffer_frac = BUFFER_PX / src_size
    fetch_bbox = expand_bbox(tile_bbox_mercator(z, x, y), buffer_frac)

    cache_dir = os.path.join(raster_root, source.cache_key, str(src_size))
    arr = _load_or_fetch(source, z, x, y,
                         fetch_bbox, fetch_size, cache_dir)
    if arr is None:
        return None

    cellsize_m = true_cellsize_m(fetch_bbox, fetch_size)
    return arr, cellsize_m, BUFFER_PX


# ---------------------------------------------------------------------------
# Public: get a single float32 grid covering an arbitrary lon/lat bbox.
#
# Used by the 3D inspector. Unlike the XYZ tile path, this is a one-shot
# request — no edge buffer (the inspector doesn't run gradient analyses
# across tile seams), no disk cache (each user-drawn bbox is unique
# enough that caching would just bloat the disk without ever hitting).
# ---------------------------------------------------------------------------

def fetch_bbox_raster(data_source: str,
                      west: float, south: float, east: float, north: float,
                      size_px: int):
    """
    Fetch one NxN float32 elevation grid covering the given lon/lat bbox.

    Returns:
        (arr, cellsize_m) on success.
        UNKNOWN_SOURCE     if `data_source` is not in the registry.
        None               on NOAA failure or decode failure.
    """
    source = get_source(data_source)
    if source is None:
        return UNKNOWN_SOURCE

    xmin, ymin = lonlat_to_mercator(west, south)
    xmax, ymax = lonlat_to_mercator(east, north)
    if not (xmax > xmin and ymax > ymin):
        return None
    bbox_mercator = (xmin, ymin, xmax, ymax)

    raw = _fetch_raster_bytes(source, bbox_mercator, size_px)
    if raw is None:
        return None
    arr = _decode_raster(raw, size_px, source)
    if arr is None:
        return None

    cellsize_m = true_cellsize_m(bbox_mercator, size_px)
    return arr, cellsize_m
