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

import logging
import math
import os
import threading
import time
from io import BytesIO

import numpy as np
import requests
from requests.adapters import HTTPAdapter
import tifffile
from PIL import Image

from src.data_sources import DataSource, get_source

logger = logging.getLogger(__name__)


# Sentinel returned by fetch_tile_raster when the caller asks for an
# unregistered source ID. The Flask layer maps it to HTTP 400 so the
# client (or a typo'd curl) gets a clear error instead of a silent
# fallback that mislabels the disk cache.
UNKNOWN_SOURCE = object()


HTTP_TIMEOUT_S = 30
OUTPUT_TILE_PX = 256
BUFFER_PX = 16

# NOAA outbound concurrency. Empirically tuned (2026-06 live measurement):
# at 6 in-flight, 12 cold Keys tiles finish in ~3.5s with per-request p50
# ~1.3s; at 12 in-flight NOAA throttles and per-request p50 degrades to
# ~3.8s with WORSE wall time. So 6 is the sweet spot — raising it hurts.
NOAA_CONCURRENCY = 6

# Transient-failure retry policy for NOAA fetches.
#
# A single connection reset, 5xx, or 429 used to surface as one blank tile
# that stayed blank until the user happened to pan back over it (the client
# only retries on a fresh fetchTile). NOAA is generally reliable but under a
# pan-burst of dozens of tiles the occasional transient is expected; a small
# bounded retry absorbs it invisibly. Kept short so a genuinely-down endpoint
# still fails fast into the 503 path rather than wedging the tile for 90s.
#
#   - Retried: network exceptions (timeout, conn reset), HTTP 5xx, HTTP 429.
#   - NOT retried: 200-but-not-an-image and 4xx (deterministic — retrying
#     just re-fetches the same error page).
#   - 429 Retry-After is honored but capped so one throttled tile can't block
#     a worker render slot for longer than the client's own 20s timeout.
NOAA_MAX_RETRIES = 2            # total attempts = 1 + NOAA_MAX_RETRIES
NOAA_BACKOFF_BASE_S = 0.4       # 0.4s, then 0.8s (×2 per attempt)
NOAA_RETRY_AFTER_CAP_S = 5.0    # never sleep longer than this on a 429

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


def _is_transient_status(code: int) -> bool:
    """HTTP statuses worth retrying: rate-limit (429) and server errors (5xx).
    4xx (other than 429) are deterministic — retrying re-fetches the same
    error and just wastes a NOAA slot."""
    return code == 429 or 500 <= code < 600


def _retry_after_seconds(resp, fallback: float) -> float:
    """Parse a 429/503 Retry-After header (delta-seconds form), capped so a
    single throttled tile can't outlast the client's render timeout. Falls
    back to the exponential-backoff value when the header is absent/unparsable."""
    raw = resp.headers.get('Retry-After')
    if raw:
        try:
            return min(max(float(raw), 0.0), NOAA_RETRY_AFTER_CAP_S)
        except (TypeError, ValueError):
            pass  # HTTP-date form is rare here; fall back to backoff
    return min(fallback, NOAA_RETRY_AFTER_CAP_S)


def _fetch_raster_bytes(source: DataSource, bbox_mercator, size_px: int):
    """GET an EPSG:3857 raster from NOAA, with bounded retry on transient
    failures. Returns raw image bytes, or None on a non-transient failure or
    after exhausting retries.

    The semaphore is acquired per attempt and released across the backoff
    sleep, so a tile waiting out its backoff does NOT hold one of the six
    NOAA slots hostage from other tiles.
    """
    params = _build_noaa_params(source, bbox_mercator, size_px)

    for attempt in range(NOAA_MAX_RETRIES + 1):
        with _noaa_semaphore:
            try:
                resp = _session.get(source.url, params=params,
                                    timeout=source.timeout_s)
            except requests.RequestException as e:
                # Network-level failure (timeout, conn reset) — transient.
                if attempt < NOAA_MAX_RETRIES:
                    delay = NOAA_BACKOFF_BASE_S * (2 ** attempt)
                    logger.warning(f"[NOAA] {source.id}: request failed ({e}); "
                                   f"retry {attempt + 1}/{NOAA_MAX_RETRIES} in {delay:.1f}s")
                else:
                    logger.warning(f"[NOAA] {source.id}: request failed after "
                                   f"{NOAA_MAX_RETRIES} retries: {e}")
                    return None
                time.sleep(delay)
                continue

        if resp.status_code == 200:
            if 'image' not in resp.headers.get('Content-Type', ''):
                # Deterministic: a 200 HTML/JSON error page. Don't retry.
                logger.warning(f"[NOAA] {source.id}: unexpected content-type "
                               f"{resp.headers.get('Content-Type')!r}")
                return None
            return resp.content

        if _is_transient_status(resp.status_code) and attempt < NOAA_MAX_RETRIES:
            delay = _retry_after_seconds(
                resp, NOAA_BACKOFF_BASE_S * (2 ** attempt))
            logger.warning(f"[NOAA] {source.id}: HTTP {resp.status_code}; "
                           f"retry {attempt + 1}/{NOAA_MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)
            continue

        # Non-transient status, or transient but out of retries.
        logger.warning(f"[NOAA] {source.id}: HTTP {resp.status_code}")
        return None

    return None


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
        logger.warning(f"[raster] decode failed: {e}")
        return None

    if arr.ndim != 2:
        # Some endpoints can serve RGB previews — not usable as a depth grid.
        return None
    arr = arr.astype(np.float32, copy=False)

    # Identify nodata on the ORIGINAL grid, BEFORE any resampling. The
    # |v|>=11000 fallback catches large-magnitude sentinels (-32768, ±1e6, …);
    # several NOAA sources use -9999, which sits inside the plausible-elevation
    # range, so we also mask the source's exact sentinel. Masking up-front is
    # what makes the worker's all-nodata-tile detection trigger and stops
    # no-coverage pixels rendering as "very deep water".
    if source.nodata is not None:
        mask = ((np.abs(arr) >= 11000.0)
                | (arr == np.float32(source.nodata)))
    else:
        mask = np.abs(arr) >= 11000.0

    # Resize if NOAA returned slightly-off dimensions (happens on edge-of-
    # coverage requests). Resample the data and the nodata mask SEPARATELY:
    #   - mask with NEAREST so nodata stays exactly nodata (no half-sentinel
    #     cells), and
    #   - data with BILINEAR for continuity — but only AFTER neutralising the
    #     sentinels, because bilinear across a -9999 boundary smears the
    #     sentinel into adjacent real cells and produces plausible-looking
    #     FAKE depths (e.g. -5000 m) right at the coverage edge. We fill the
    #     sentinel cells with the median of the valid data first; those cells
    #     are masked out afterward anyway, and edge real-cells now blend with
    #     a sane neighbour instead of an extreme sentinel.
    if arr.shape != (expected_size, expected_size):
        valid = arr[~mask]
        fill = float(np.median(valid)) if valid.size else 0.0
        data = np.where(mask, np.float32(fill), arr)
        data = np.asarray(
            Image.fromarray(data).resize((expected_size, expected_size),
                                         Image.BILINEAR),
            dtype=np.float32,
        )
        mask = np.asarray(
            Image.fromarray(mask.astype(np.uint8)).resize(
                (expected_size, expected_size), Image.NEAREST),
            dtype=bool,
        )
        arr = data

    return np.where(mask, np.nan, arr)


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
            logger.warning(f"[raster] cache read failed for {cache_path}: {e}")

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
            logger.warning(f"[raster] cache write failed for {cache_path}: {e}")

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
