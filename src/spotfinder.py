"""
FishFinder Spotfinder: labeled-region bathymetric spot-finder.

Public entry point is `run_spotfinder(payload)`, a generator that yields a
stream of progress + result events. The Flask layer wraps this in an
NDJSON streaming response (one JSON event per line) so the browser can
update its progress bar live without polling. The final event is a
`result` (success) or `error` (failure); after that, the generator stops.

Pipeline:

    1. Resolve the highest-resolution data source whose coverage of the
       rotated rectangle is at least 95 %.
    2. Fetch one contiguous raster covering the AABB at that source's
       native resolution, chunking the NOAA request grid if the
       raster is larger than one ImageServer call.
    3. Build a mask: cells inside the rotated rectangle AND non-null AND
       below sea level (depth < 0).
    4. Preprocess: small-gap interpolation + light Gaussian smoothing.
    5. Multi-scale derivative stack (small / mid / large radii in cells):
       slope, profile + planform curvature, annulus TPI, local relief.
    6. Per-pixel rule-based classification into
       pinnacle / ridge / ledge / hole / channel / saddle (+ slope / flat
       as the baseline that is never surfaced).
    7. Connected-component region extraction per class with morphological
       closing + minimum-area filtering. For linear classes the major-
       axis "spine" is sampled as a centerline polyline for rendering.
    8. Per-region scoring with weights:
            0.20 relief + 0.35 local_percentile + 0.15 shape
          + 0.15 isolation + 0.15 depth_fit
       `local_percentile` is the key term: it compares this region's
       local relief to the relief distribution in a much larger window
       (default 6× the region bbox). On deep flats that lifts subtle
       structure; on reefy areas it suppresses anything that isn't the
       most prominent feature in its neighborhood.
    9. Region-level NMS: when two regions overlap/are adjacent and one
       outscores the other by ≥ `nms_score_ratio`, the weaker is dropped
       and its class joins the winner's `secondary_tags`.
    10. Composite pass: clusters of 2+ high-scoring regions within a
        proximity radius (a multiple of each region's characteristic
        size) become a `composite` meta-feature carrying a convex hull
        and an aggregate score.
    11. Colorise the score raster (transparent → yellow → orange → red),
        resample into the rectangle's LOCAL frame at ~512 px on the long
        side, and emit as a base64 PNG data URL plus the four corner
        lat/lngs the overlay should be georeferenced to.

Every heavy step lives in numpy / scipy. uniform_filter, maximum_filter
and minimum_filter are separable and run in O(N) regardless of the
window radius, so the broad-scale features over a multi-megacell raster
land in a few hundred milliseconds.

The output contract preserves the legacy `spots[]` list (one entry per
region centroid) so the existing point-marker renderer keeps working
unchanged, and adds `regions[]` (full geometry + metrics + secondary
tags) and `composites[]` (convex hulls of high-scoring clusters) for
the richer renderer in `static/map.js`.
"""

import base64
import io
import math
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import requests
import tifffile
from PIL import Image
from scipy.ndimage import (
    binary_closing, binary_erosion,
    gaussian_filter, label, map_coordinates,
    maximum_filter, minimum_filter, uniform_filter,
)
from scipy.spatial import ConvexHull
from scipy.spatial.qhull import QhullError

from src.LayerGeneration import _build_noaa_params, _noaa_semaphore, _session
from src.data_sources import get_source


# ---------------------------------------------------------------------------
# Source priorities. Ordered most-detailed → coarsest. Each entry pairs a
# source ID (must exist in src/data_sources.SOURCES) with the rough native
# resolution we'll ask NOAA for, in metres per pixel. We never downsample
# below this — the spec is explicit that the algorithm runs at the source's
# native posting.
#
# `dem-global` (ETOPO 2022, ~460 m) is intentionally absent: its resolution
# is too coarse for spot-finding. If no source in this list reaches the
# 95 % coverage bar, we surface the "insufficient coverage" error instead
# of silently falling back to GEBCO.
# ---------------------------------------------------------------------------
SPOTFINDER_SOURCES = (
    ("bag-bathymetry",  1.0),
    ("nos-mbab",        1.0),
    ("dem-all",         3.0),
    ("dem-tiles",       3.0),
    ("crm-mosaic",     90.0),
    ("multibeam",     100.0),
)

# Hard limits.
MAX_TOTAL_CELLS = 50_000_000
MAX_SINGLE_FETCH_DIM = 4096
COVERAGE_THRESHOLD = 0.95
COVERAGE_PROBE_PX = 128


# ---------------------------------------------------------------------------
# Classification class IDs. Stored as int8 in the per-pixel class raster.
# Order matters only insofar as the classifier's priority overrides — see
# `_classify`.
# ---------------------------------------------------------------------------
CLASS_FLAT     = 0
CLASS_SLOPE    = 1
CLASS_PINNACLE = 2
CLASS_RIDGE    = 3
CLASS_LEDGE    = 4
CLASS_HOLE     = 5
CLASS_CHANNEL  = 6
CLASS_SADDLE   = 7

CLASS_NAMES = {
    CLASS_FLAT:     "flat",
    CLASS_SLOPE:    "slope",
    CLASS_PINNACLE: "pinnacle",
    CLASS_RIDGE:    "ridge",
    CLASS_LEDGE:    "ledge",
    CLASS_HOLE:     "hole",
    CLASS_CHANNEL:  "channel",
    CLASS_SADDLE:   "saddle",
}
# Classes that produce output regions. `flat` and `slope` are baseline
# context — we don't surface them.
SURFACED_CLASSES = (CLASS_PINNACLE, CLASS_RIDGE, CLASS_LEDGE,
                    CLASS_HOLE, CLASS_CHANNEL, CLASS_SADDLE)
# Linear classes get a centerline polyline for rendering.
LINEAR_CLASSES = (CLASS_RIDGE, CLASS_LEDGE, CLASS_CHANNEL)


# ---------------------------------------------------------------------------
# Tunable parameters. Every value here is exposed through the `params`
# field on the input payload so a future tuning UI can drive them without
# code changes.
# ---------------------------------------------------------------------------
DEFAULT_PARAMS = {
    # Preprocess
    "smooth_sigma_cells":      0.7,
    "gap_fill_radius":         2,

    # Multi-scale stack (radii in cells).
    "scale_small":             3,
    "scale_mid":              10,
    "scale_large":            30,

    # Classification thresholds (metres / degrees / 1-per-metre).
    "tpi_small_threshold_m":   0.30,
    "tpi_large_threshold_m":   0.60,
    "slope_small_deg":         3.0,
    "slope_large_deg":         1.2,
    "curvature_threshold":     0.002,

    # Region extraction.
    "min_region_area_cells":   12,
    "morph_close_radius":      1,

    # Score weights (sum to 1.0 by convention; they do not have to).
    "weight_relief":           0.20,
    "weight_local_percentile": 0.35,
    "weight_shape":            0.15,
    "weight_isolation":        0.15,
    "weight_depth_fit":        0.15,

    # Local-percentile window expressed as a multiple of region bbox dims.
    # 6× is the spec midpoint (5-10×).
    "local_window_factor":     6.0,

    # Relief normalisation: 10 m of relief reads as full-scale.
    "relief_scale_m":         10.0,

    # Isolation normalisation: distance at which the isolation score saturates.
    "isolation_scale_m":     200.0,

    # Depth fit: 1.0 inside [min, max] (in metres below sea level),
    # decaying linearly over `falloff` outside that band.
    "depth_fit_min_m":         5.0,
    "depth_fit_max_m":        60.0,
    "depth_fit_falloff_m":    30.0,

    # Selection.
    "score_threshold":         0.30,
    "max_regions":             80,

    # NMS.
    "nms_score_ratio":         1.5,
    "nms_adjacency_cells":     3,

    # Composite clustering.
    "composite_radius_factor":   4.0,
    "composite_min_members":     2,
    "composite_score_threshold": 0.50,
}


# ---------------------------------------------------------------------------
# Environment modes — the "detection strictness" axis.
#
# The same bump that is a genuine spot over a featureless flat is just
# background texture on a reef, so each mode overrides a SMALL, NAMED set of
# DEFAULT_PARAMS rather than one magic number:
#
#   tpi_small/large_threshold_m : how far a cell must stand proud of its
#       neighbourhood (metres) before it can be classed as structure — the
#       single biggest strictness lever.
#   score_threshold             : the floor a region's final score must clear
#       to be surfaced at all.
#   weight_local_percentile     : how heavily we reward "prominent vs the
#       local neighbourhood" over raw relief. High on reefs (only the most
#       prominent feature in a busy field survives); still meaningful on
#       flats (a subtle bump is unusual against featureless ground). This is
#       the term that normalises against local roughness so flat mode isn't
#       just "return every bump".
#   weight_relief               : how much raw vertical relief counts.
#   local_window_factor         : size of the neighbourhood the percentile is
#       measured against, as a multiple of the region bbox.
#   relief_scale_m              : relief that reads as full-scale.
#
# To add a mode (drop-off, channel, grass flat, …) just add an entry here —
# no other code changes. `param_overrides` may name ANY DEFAULT_PARAMS key.
# The values below are deliberately conservative starting points; tune them
# in place and re-run tools/spotfinder_smoke.py to eyeball the effect.
# ---------------------------------------------------------------------------
ENVIRONMENT_MODES = {
    # Flat bottom: lower the bar, but stay smart. Everything around a feature
    # is featureless, so subtle structure matters — we drop the prominence
    # thresholds and lean on the local-percentile term so this isn't just
    # "return every bump". Sits close to the legacy DEFAULT_PARAMS baseline,
    # which is why legacy runs migrate to this mode (see spotfinder-storage).
    "flat": {
        "label": "Flat bottom",
        "param_overrides": {
            "tpi_small_threshold_m":   0.20,
            "tpi_large_threshold_m":   0.40,
            "score_threshold":         0.28,
            "weight_local_percentile": 0.40,
            "weight_relief":           0.15,
            "local_window_factor":     6.0,
            "relief_scale_m":          6.0,
        },
    },
    # Reef: raise the bar. The seafloor is already textured, so a feature has
    # to genuinely dominate its neighbourhood to count. Higher prominence
    # thresholds, a higher score floor, more weight on local-percentile over
    # a LARGER comparison window, and a larger relief scale so only real
    # relief reads as full-scale.
    "reef": {
        "label": "Reef",
        "param_overrides": {
            "tpi_small_threshold_m":   0.45,
            "tpi_large_threshold_m":   0.90,
            "score_threshold":         0.42,
            "weight_local_percentile": 0.45,
            "weight_relief":           0.20,
            "local_window_factor":     8.0,
            "relief_scale_m":          14.0,
        },
    },
}

# Default environment when the client doesn't specify one. Reef is the
# primary deployment target (the Florida Keys reef tract), so a user who
# just hits Run gets the stricter, higher-confidence result set.
DEFAULT_ENVIRONMENT = "reef"


# ---------------------------------------------------------------------------
# Structure types — the user-facing taxonomy, mapped onto the internal
# per-pixel classes. Selecting a subset both (a) filters surfaced results to
# those classes and (b) lets region extraction skip the unselected classes
# entirely (free perf + avoids edge-case false positives — see
# `_extract_regions`). Extensible: add an entry to expose a new class, or
# point a new user-facing type at an existing class. The next iteration
# (sizing / depth range / slope constraints / custom kernels) hangs off the
# same `config` object this taxonomy lives in.
# ---------------------------------------------------------------------------
STRUCTURE_TYPES = {
    "ledge":    {"label": "Ledge",        "classes": (CLASS_LEDGE,)},
    # A mound / hump is a raised, broadly elongated feature; the internal
    # `ridge` class (positive TPI + positive planform curvature) is the best
    # available fit. Sharp, compact peaks are the separate `pinnacle` class.
    "mound":    {"label": "Mound / hump", "classes": (CLASS_RIDGE,)},
    "saddle":   {"label": "Saddle",       "classes": (CLASS_SADDLE,)},
    "pinnacle": {"label": "Pinnacle",     "classes": (CLASS_PINNACLE,)},
    "hole":     {"label": "Hole",         "classes": (CLASS_HOLE,)},
    "channel":  {"label": "Channel",      "classes": (CLASS_CHANNEL,)},
}
# All types on by default so a user can hit Run without touching anything.
DEFAULT_STRUCTURE_TYPES = tuple(STRUCTURE_TYPES.keys())


# ---------------------------------------------------------------------------
# Per-structure-type size filter — a POST-CLASSIFICATION output gate.
#
# The detection + classification + scoring pipeline is untouched; after the
# regions are finalised we drop any whose real-world footprint falls outside
# the configured [min_ft, max_ft] for their assigned structure type. This is
# purely a filter on the output, so it's fully reversible and never shifts a
# classification or score threshold.
#
# SIZE_MEASURE picks WHAT "size" means per type, because "longest extent" is
# the wrong "is it small?" question for long-thin features:
#   - "longest" : maximum caliper diameter of the footprint (Feret max) — the
#                 longest straight-line distance between two boundary points.
#                 Right for compact features (pinnacle / hole / saddle).
#   - "width"   : minimum caliper width of the footprint (Feret min) — the
#                 narrow cross-feature dimension. Right for elongated features
#                 (mound / ledge / channel), where length tells you nothing
#                 about whether the spot is small enough to fish.
#
# All sizes are real-world FEET, derived from the source's Mercator-corrected
# ground sample distance (cell_x_m / cell_y_m). No pixel- or cell-space
# proxy: cell deltas are converted to ground metres first (the conversion is
# exact for the area at hand — see _hull_metric_pts), then to feet.
#
# Defaults reflect realistic fishable scales. min_region_area_cells already
# removes sub-feature noise; these maxes are what keep a 200-ft "mound" out
# of the result set unless the user widens the range for the trip.
# ---------------------------------------------------------------------------
SIZE_MEASURE = {
    "pinnacle": "longest",
    "saddle":   "longest",
    "hole":     "longest",
    "mound":    "width",
    "ledge":    "width",
    "channel":  "width",
}
DEFAULT_SIZE_RANGES = {
    "pinnacle": {"min_ft":  5.0, "max_ft":  60.0},
    "mound":    {"min_ft": 20.0, "max_ft": 150.0},
    "ledge":    {"min_ft":  5.0, "max_ft":  80.0},
    "saddle":   {"min_ft": 20.0, "max_ft": 200.0},
    "hole":     {"min_ft": 10.0, "max_ft": 150.0},
    "channel":  {"min_ft": 10.0, "max_ft": 100.0},
}
# Absolute clamps for a client-supplied range (defensive; the UI stays well
# inside these). 5000 ft is far larger than any fishable structure but small
# enough to catch a garbage payload.
SIZE_FT_ABS_MIN = 0.0
SIZE_FT_ABS_MAX = 5000.0
_M_TO_FT = 3.280839895

# Reverse map: internal class id → user-facing structure-type key. The size
# range + measure are keyed by structure type, but the filter sees regions
# keyed by class id. Built once; first type wins if a class were ever shared.
_CLASS_TO_STRUCTURE_TYPE = {}
for _tkey, _spec in STRUCTURE_TYPES.items():
    for _c in _spec["classes"]:
        _CLASS_TO_STRUCTURE_TYPE.setdefault(_c, _tkey)


def _coerce_ft(value, default):
    """Best-effort float-feet coercion; falls back to `default` on garbage."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def resolve_size_ranges(config):
    """Validate `config.size_ranges` into a full per-type {min_ft, max_ft} map.

    Always returns an entry for EVERY structure type (not just the selected
    ones) so a saved run records the complete size configuration. Missing or
    malformed entries fall back to DEFAULT_SIZE_RANGES; values are clamped to
    [SIZE_FT_ABS_MIN, SIZE_FT_ABS_MAX] and min/max are swapped if inverted.
    """
    requested = config.get("size_ranges") if isinstance(config, dict) else None
    if not isinstance(requested, dict):
        requested = {}
    out = {}
    for tkey, default in DEFAULT_SIZE_RANGES.items():
        lo, hi = default["min_ft"], default["max_ft"]
        entry = requested.get(tkey)
        if isinstance(entry, dict):
            lo = _coerce_ft(entry.get("min_ft"), lo)
            hi = _coerce_ft(entry.get("max_ft"), hi)
        lo = max(SIZE_FT_ABS_MIN, min(lo, SIZE_FT_ABS_MAX))
        hi = max(SIZE_FT_ABS_MIN, min(hi, SIZE_FT_ABS_MAX))
        if hi < lo:
            lo, hi = hi, lo
        out[tkey] = {"min_ft": lo, "max_ft": hi}
    return out


def resolve_config(payload):
    """Translate the client payload into the concrete run configuration.

    Pure and side-effect-free (no NOAA, no globals mutated) so it can be
    unit-tested directly. Returns a dict:

        params           — DEFAULT_PARAMS + the environment's overrides +
                           any explicit per-knob `params` (explicit wins, so
                           a power user / future tuning UI can still override
                           an individual threshold).
        environment      — validated mode key (falls back to default).
        structure_types  — validated user-facing type keys (always >= 1).
        selected_classes — set of internal class ids to extract + surface.
        source_id        — preferred data source id, or None for auto-resolve.
        size_ranges      — full per-type {min_ft, max_ft} map (every type),
                           used by the post-classification size filter.
    """
    if not isinstance(payload, dict):
        payload = {}
    config = payload.get("config") or {}
    if not isinstance(config, dict):
        config = {}

    # Environment → param overrides.
    environment = config.get("environment")
    if environment not in ENVIRONMENT_MODES:
        environment = DEFAULT_ENVIRONMENT
    params = dict(DEFAULT_PARAMS)
    params.update(ENVIRONMENT_MODES[environment]["param_overrides"])
    user_params = payload.get("params") or {}
    if isinstance(user_params, dict):
        params.update(user_params)

    # Structure types → selected internal classes.
    requested = config.get("structure_types")
    if not isinstance(requested, (list, tuple)):
        requested = []
    structure_types = [t for t in requested if t in STRUCTURE_TYPES]
    if not structure_types:
        # Defensive: the UI enforces "at least one", but we must never
        # silently surface nothing because of a malformed payload.
        structure_types = list(DEFAULT_STRUCTURE_TYPES)
    selected_classes = set()
    for t in structure_types:
        selected_classes.update(STRUCTURE_TYPES[t]["classes"])

    source_id = config.get("source")
    if not isinstance(source_id, str) or not source_id:
        source_id = None

    size_ranges = resolve_size_ranges(config)

    return {
        "params":           params,
        "environment":      environment,
        "structure_types":  structure_types,
        "selected_classes": selected_classes,
        "source_id":        source_id,
        "size_ranges":      size_ranges,
    }


# Web Mercator constants — keep in sync with src/LayerGeneration.py and
# static/spotfinder-shape.js.
_R = 6378137.0
_ORIGIN = math.pi * _R


class SpotfinderError(Exception):
    """Domain-specific error surfaced to the client verbatim."""


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _lng_to_merc_x(lng):
    return lng * _ORIGIN / 180.0


def _lat_to_merc_y(lat):
    lat_c = max(-85.0511, min(85.0511, lat))
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat_c) / 2.0)) * _R


def _merc_x_to_lng(x):
    return x * 180.0 / _ORIGIN


def _merc_y_to_lat(y):
    return math.degrees(math.atan(math.sinh(y / _R)))


def _aabb_bbox_3857(area):
    """AABB of the rotated rectangle in Web Mercator metres."""
    bbox = area["bbox"]
    xmin = _lng_to_merc_x(bbox["west"])
    xmax = _lng_to_merc_x(bbox["east"])
    ymin = _lat_to_merc_y(bbox["south"])
    ymax = _lat_to_merc_y(bbox["north"])
    return xmin, ymin, xmax, ymax


def _compute_raster_dims(bbox_3857, target_res_m, center_lat):
    """Pick a (w, h) so each pixel covers ~target_res_m on the ground."""
    xmin, ymin, xmax, ymax = bbox_3857
    cos_lat = max(math.cos(math.radians(center_lat)), 1e-9)
    merc_per_cell = target_res_m / cos_lat
    w = max(1, int(math.ceil((xmax - xmin) / merc_per_cell)))
    h = max(1, int(math.ceil((ymax - ymin) / merc_per_cell)))
    return w, h


# ---------------------------------------------------------------------------
# NOAA fetch
# ---------------------------------------------------------------------------

def _fetch_bbox_raster(source, bbox_3857, raster_w, raster_h):
    """Single rectangular NOAA call. Returns float32 ndarray (h, w) or None."""
    params = _build_noaa_params(source, bbox_3857, size_px=max(raster_w, raster_h))
    # _build_noaa_params writes a square `size`. Override for rectangular.
    params["size"] = f"{raster_w},{raster_h}"

    with _noaa_semaphore:
        try:
            resp = _session.get(source.url, params=params,
                                timeout=source.timeout_s)
        except requests.RequestException as e:
            print(f"[spotfinder] NOAA fetch failed ({source.id}): {e}")
            return None
    if resp.status_code != 200:
        print(f"[spotfinder] NOAA HTTP {resp.status_code} ({source.id})")
        return None
    if "image" not in resp.headers.get("Content-Type", ""):
        print(f"[spotfinder] NOAA returned non-image ({source.id}): "
              f"{resp.headers.get('Content-Type')!r}")
        return None

    try:
        arr = tifffile.imread(io.BytesIO(resp.content))
    except Exception as e:
        print(f"[spotfinder] decode failed ({source.id}): {e}")
        return None
    if arr.ndim != 2:
        return None
    arr = arr.astype(np.float32, copy=False)
    if arr.shape != (raster_h, raster_w):
        arr = np.asarray(
            Image.fromarray(arr).resize((raster_w, raster_h), Image.BILINEAR),
            dtype=np.float32,
        )
    # Sentinel nodata → NaN. Mirror src/LayerGeneration._decode_raster.
    if source.nodata is not None:
        arr = np.where(
            (np.abs(arr) >= 11000.0) | (arr == np.float32(source.nodata)),
            np.nan, arr,
        )
    else:
        arr = np.where(np.abs(arr) >= 11000.0, np.nan, arr)
    return arr


def _fetch_full_raster(source, area, target_res_m):
    """Build a full raster of the AABB at ~target_res_m. Chunks if needed."""
    bbox_3857 = _aabb_bbox_3857(area)
    xmin, ymin, xmax, ymax = bbox_3857
    center_lat = area["center"]["lat"]
    raster_w, raster_h = _compute_raster_dims(bbox_3857, target_res_m, center_lat)

    if raster_w * raster_h > MAX_TOTAL_CELLS:
        raise SpotfinderError(
            "Area is too large for high-resolution analysis. "
            "Draw a smaller box."
        )

    if raster_w <= MAX_SINGLE_FETCH_DIM and raster_h <= MAX_SINGLE_FETCH_DIM:
        arr = _fetch_bbox_raster(source, bbox_3857, raster_w, raster_h)
        if arr is None:
            raise SpotfinderError(
                "Couldn't fetch bathymetry from NOAA. Try again in a moment."
            )
    else:
        arr = _fetch_chunked_raster(source, bbox_3857, raster_w, raster_h)

    cos_lat = max(math.cos(math.radians(center_lat)), 1e-9)
    cellsize_m_x = (xmax - xmin) * cos_lat / raster_w
    cellsize_m_y = (ymax - ymin) * cos_lat / raster_h
    return arr, cellsize_m_x, cellsize_m_y


def _fetch_chunked_raster(source, bbox_3857, raster_w, raster_h):
    """Stitch the full raster from a grid of MAX_SINGLE_FETCH_DIM sub-fetches."""
    xmin, ymin, xmax, ymax = bbox_3857
    merc_w = xmax - xmin
    merc_h = ymax - ymin

    chunk = MAX_SINGLE_FETCH_DIM
    n_x = (raster_w + chunk - 1) // chunk
    n_y = (raster_h + chunk - 1) // chunk

    full = np.full((raster_h, raster_w), np.nan, dtype=np.float32)
    for cy in range(n_y):
        for cx in range(n_x):
            x0 = cx * chunk
            x1 = min(x0 + chunk, raster_w)
            y0 = cy * chunk
            y1 = min(y0 + chunk, raster_h)

            sub = _fetch_bbox_raster(
                source,
                (
                    xmin + (x0 / raster_w) * merc_w,
                    ymax - (y1 / raster_h) * merc_h,
                    xmin + (x1 / raster_w) * merc_w,
                    ymax - (y0 / raster_h) * merc_h,
                ),
                x1 - x0, y1 - y0,
            )
            if sub is not None:
                full[y0:y1, x0:x1] = sub
    return full


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def _coverage_probe(source, area):
    """Fraction of the rotated rectangle that has non-null data in this source."""
    bbox_3857 = _aabb_bbox_3857(area)
    probe = _fetch_bbox_raster(source, bbox_3857,
                               COVERAGE_PROBE_PX, COVERAGE_PROBE_PX)
    if probe is None:
        return None
    rect_mask = _build_rotated_mask(probe.shape, area)
    rect_cells = int(rect_mask.sum())
    if rect_cells == 0:
        return 0.0
    have_data = rect_mask & np.isfinite(probe)
    return float(have_data.sum()) / rect_cells


# Native posting (metres/pixel) keyed by source id, for the spot-finding
# source table. Used to honour a client-requested source at the right
# resolution without re-deriving it.
_SPOTFINDER_NATIVE_RES = dict(SPOTFINDER_SOURCES)


def _resolve_source(area, emit, preferred_source_id=None):
    """Pick the data source for the run.

    If the client passed a preferred source — the one active on the map when
    the box was drawn — and it both exists in our spot-finding source table
    AND clears the coverage threshold for this box, we honour it. The run
    then reflects exactly what the user was looking at. Otherwise (no
    request, an unsuitable source like the coarse global mosaics, or
    insufficient coverage) we fall back to the highest-resolution source
    that meets the coverage bar.
    """
    # 1. Honour the user's on-map source when it's viable here. Sources not
    #    in SPOTFINDER_SOURCES (e.g. dem-global, far too coarse for
    #    spot-finding) intentionally don't qualify and fall through to (2).
    if preferred_source_id and preferred_source_id in _SPOTFINDER_NATIVE_RES:
        source = get_source(preferred_source_id)
        if source is not None:
            emit(1.0, f"Checking your map source ({source.display_name})…")
            coverage = _coverage_probe(source, area)
            if coverage is not None and coverage >= COVERAGE_THRESHOLD:
                return source, _SPOTFINDER_NATIVE_RES[preferred_source_id], coverage
            emit(2.0, "Map source lacks coverage here — finding the best "
                      "available…")

    # 2. Auto-resolve: highest-resolution source meeting the threshold.
    pct_per_probe = 4.0 / max(1, len(SPOTFINDER_SOURCES))
    base_pct = 1.0

    last_seen = None
    for i, (source_id, native_res) in enumerate(SPOTFINDER_SOURCES):
        source = get_source(source_id)
        if source is None:
            continue
        emit(base_pct + i * pct_per_probe,
             f"Checking {source.display_name}…")
        coverage = _coverage_probe(source, area)
        if coverage is None:
            continue
        last_seen = (source, native_res, coverage)
        if coverage >= COVERAGE_THRESHOLD:
            return source, native_res, coverage

    if last_seen is None:
        raise SpotfinderError(
            "Couldn't reach any bathymetry source. Check your connection "
            "and try again."
        )
    raise SpotfinderError(
        "Insufficient high-resolution coverage for this area. "
        "Try a smaller box or a different location."
    )


# ---------------------------------------------------------------------------
# Mask building
# ---------------------------------------------------------------------------

def _build_rotated_mask(shape, area):
    """True where the cell center is inside the rotated rectangle."""
    h, w = shape
    xmin, ymin, xmax, ymax = _aabb_bbox_3857(area)

    ys = ymax - (np.arange(h, dtype=np.float64) + 0.5) * (ymax - ymin) / h
    xs = xmin + (np.arange(w, dtype=np.float64) + 0.5) * (xmax - xmin) / w

    center = area["center"]
    cx_m = _lng_to_merc_x(center["lng"])
    cy_m = _lat_to_merc_y(center["lat"])

    cos_lat = max(math.cos(math.radians(center["lat"])), 1e-9)
    half_w_merc = (area["width_m"]  / 2.0) / cos_lat
    half_h_merc = (area["height_m"] / 2.0) / cos_lat

    rot = math.radians(area["rotation_deg"])
    cos_r = math.cos(rot)
    sin_r = math.sin(rot)

    X = xs[None, :] - cx_m
    Y = ys[:, None] - cy_m

    # Inverse of SHAPE.rotateClockwise: maps AABB-space back into rect-local.
    local_dx = X * cos_r - Y * sin_r
    local_dy = X * sin_r + Y * cos_r
    return (np.abs(local_dx) <= half_w_merc) & (np.abs(local_dy) <= half_h_merc)


# ---------------------------------------------------------------------------
# Preprocess: small-gap interpolation + light Gaussian smoothing.
# ---------------------------------------------------------------------------

def _preprocess(elev, valid, params):
    """Return (smoothed elev, valid mask) ready for the derivative stack.

    Holes (NaN cells inside `valid`) are filled with the local mean over a
    small radius; remaining NaN cells (no nearby data) stay NaN and the
    valid mask is tightened accordingly. The Gaussian smoothing is done
    NaN-safely by zeroing NaN, smoothing both the data and a 0/1 weight
    mask, and dividing.
    """
    sigma = float(params.get("smooth_sigma_cells",
                             DEFAULT_PARAMS["smooth_sigma_cells"]))
    radius = int(params.get("gap_fill_radius",
                            DEFAULT_PARAMS["gap_fill_radius"]))

    filled = np.where(np.isfinite(elev) & valid, elev, 0.0).astype(np.float32)
    has_data = (np.isfinite(elev) & valid).astype(np.float32)
    if radius > 0:
        size = 2 * radius + 1
        s = uniform_filter(filled, size=size, mode="constant", cval=0.0)
        n = uniform_filter(has_data, size=size, mode="constant", cval=0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            local_mean = np.where(n > 0, s / n, np.nan)
        bridge = np.where(np.isfinite(elev), elev, local_mean).astype(np.float32)
    else:
        bridge = np.where(np.isfinite(elev), elev, np.nan).astype(np.float32)

    bridge_zero = np.where(np.isfinite(bridge) & valid, bridge, 0.0).astype(np.float32)
    mask_f = (np.isfinite(bridge) & valid).astype(np.float32)
    if sigma > 0:
        smoothed = gaussian_filter(bridge_zero, sigma=sigma, mode="reflect")
        weight = gaussian_filter(mask_f, sigma=sigma, mode="reflect")
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(weight > 1e-6, smoothed / weight, np.nan).astype(np.float32)
    else:
        out = np.where(mask_f > 0, bridge, np.nan).astype(np.float32)

    new_valid = valid & np.isfinite(out)
    return out, new_valid


# ---------------------------------------------------------------------------
# Derivative stack
# ---------------------------------------------------------------------------

def _box_mean_masked(arr, valid_f32, size):
    """Mean of `arr` over a (size×size) box, ignoring cells where valid=False."""
    s = uniform_filter(arr, size=size, mode="constant", cval=0.0)
    n = uniform_filter(valid_f32, size=size, mode="constant", cval=0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, s / n, np.nan).astype(np.float32)


def _annulus_mean(elev, valid_f32, inner_r, outer_r):
    """Mean of `elev` over the annulus between inner and outer square windows.

    Approximated as (outer_sum − inner_sum) / (outer_count − inner_count),
    which collapses to the strict square-annulus mean when the inner box
    is contained in the outer one. With NaN-safe counting this is the
    right thing for every cell where at least one annulus cell is valid.
    """
    filled = np.where(valid_f32 > 0, elev, 0.0).astype(np.float32)
    inner_size = max(1, 2 * inner_r + 1)
    outer_size = max(inner_size + 2, 2 * outer_r + 1)
    inner_mean = _box_mean_masked(filled, valid_f32, inner_size)
    outer_mean = _box_mean_masked(filled, valid_f32, outer_size)
    inner_n = uniform_filter(valid_f32, size=inner_size,
                             mode="constant", cval=0.0) * (inner_size * inner_size)
    outer_n = uniform_filter(valid_f32, size=outer_size,
                             mode="constant", cval=0.0) * (outer_size * outer_size)
    ann_n = outer_n - inner_n
    inner_sum = np.where(np.isfinite(inner_mean), inner_mean * inner_n, 0.0)
    outer_sum = np.where(np.isfinite(outer_mean), outer_mean * outer_n, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ann_n > 0, (outer_sum - inner_sum) / ann_n, np.nan).astype(np.float32)


def _slope_deg(elev, cell_x_m, cell_y_m):
    """Slope angle (degrees from horizontal) from np.gradient. NaN-propagating."""
    dzdy, dzdx = np.gradient(elev, cell_y_m, cell_x_m)
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)


def _box_slope_deg(elev, valid_f32, cell_x_m, cell_y_m, smooth_size):
    """Slope of a box-smoothed surface — slope at the smoothed scale."""
    filled = np.where(valid_f32 > 0, elev, 0.0).astype(np.float32)
    smoothed = _box_mean_masked(filled, valid_f32, smooth_size)
    # NaN-propagation through gradient is fine here — slope_deg is only
    # consumed via valid-masked indexing downstream.
    return _slope_deg(np.nan_to_num(smoothed, nan=0.0), cell_x_m, cell_y_m)


def _curvatures(elev, cell_x_m, cell_y_m):
    """Profile + planform curvature (Zevenbergen-Thorne).

    Returns (profile, planform) in units of 1/m. Profile measures the
    rate of change of slope down the slope line; planform measures the
    same across-slope. Saddles flip the sign between the two; closed
    bumps have the same sign on both.
    """
    dy, dx = np.gradient(elev, cell_y_m, cell_x_m)
    dyy = np.gradient(dy, cell_y_m, axis=0)
    dxx = np.gradient(dx, cell_x_m, axis=1)
    dxy = np.gradient(dx, cell_y_m, axis=0)
    p = dx * dx + dy * dy
    q = p + 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        prof = -(dxx * dx * dx + 2.0 * dxy * dx * dy + dyy * dy * dy) / (p * q ** 1.5)
        plan = -(dxx * dy * dy - 2.0 * dxy * dx * dy + dyy * dx * dx) / (p ** 1.5)
    prof = np.where((p > 1e-10) & np.isfinite(prof), prof, 0.0)
    plan = np.where((p > 1e-10) & np.isfinite(plan), plan, 0.0)
    return prof.astype(np.float32), plan.astype(np.float32)


def _local_relief(elev, valid, size):
    """max(elev) − min(elev) over a (size×size) window, NaN-safe."""
    safe_max = np.where(valid, elev, -np.inf)
    safe_min = np.where(valid, elev,  np.inf)
    mx = maximum_filter(safe_max, size=size, mode="constant", cval=-np.inf)
    mn = minimum_filter(safe_min, size=size, mode="constant", cval= np.inf)
    out = mx - mn
    return np.where(np.isfinite(out), out, 0.0).astype(np.float32)


def _derivative_stack(elev, valid, cell_x_m, cell_y_m, params):
    """Compute every per-scale derivative the classifier + scorer consume."""
    s_small = int(params.get("scale_small", DEFAULT_PARAMS["scale_small"]))
    s_mid   = int(params.get("scale_mid",   DEFAULT_PARAMS["scale_mid"]))
    s_large = int(params.get("scale_large", DEFAULT_PARAMS["scale_large"]))
    valid_f = valid.astype(np.float32)

    slope_small = _box_slope_deg(elev, valid_f, cell_x_m, cell_y_m, max(3, s_small))
    slope_large = _box_slope_deg(elev, valid_f, cell_x_m, cell_y_m, max(3, s_mid))

    smooth_small = _box_mean_masked(
        np.where(valid_f > 0, elev, 0.0).astype(np.float32),
        valid_f, max(3, s_small))
    prof_curv, plan_curv = _curvatures(np.nan_to_num(smooth_small, nan=0.0),
                                       cell_x_m, cell_y_m)
    prof_curv = np.where(valid, prof_curv, 0.0).astype(np.float32)
    plan_curv = np.where(valid, plan_curv, 0.0).astype(np.float32)

    tpi_small = (elev - _annulus_mean(elev, valid_f, max(1, s_small // 2), s_small)).astype(np.float32)
    tpi_large = (elev - _annulus_mean(elev, valid_f, s_small, s_large)).astype(np.float32)

    relief = _local_relief(elev, valid, max(3, s_mid))

    return {
        "slope_small": slope_small,
        "slope_large": slope_large,
        "prof_curv":   prof_curv,
        "plan_curv":   plan_curv,
        "tpi_small":   tpi_small,
        "tpi_large":   tpi_large,
        "relief":      relief,
    }


# ---------------------------------------------------------------------------
# Per-pixel classification
# ---------------------------------------------------------------------------

def _classify(stack, valid, params):
    """Assign every cell a class id. Priority: pinnacle/hole > ridge/channel
    > saddle > ledge > slope > flat. Later overrides dominate by writing
    last; conditions are written to forbid double-classification where it
    would be wrong.
    """
    tpi_s = stack["tpi_small"]
    tpi_l = stack["tpi_large"]
    sl_s  = stack["slope_small"]
    sl_l  = stack["slope_large"]
    prof  = stack["prof_curv"]
    plan  = stack["plan_curv"]

    th_tpi_s = float(params.get("tpi_small_threshold_m", DEFAULT_PARAMS["tpi_small_threshold_m"]))
    th_tpi_l = float(params.get("tpi_large_threshold_m", DEFAULT_PARAMS["tpi_large_threshold_m"]))
    th_sl_s  = float(params.get("slope_small_deg",       DEFAULT_PARAMS["slope_small_deg"]))
    th_sl_l  = float(params.get("slope_large_deg",       DEFAULT_PARAMS["slope_large_deg"]))
    th_curv  = float(params.get("curvature_threshold",   DEFAULT_PARAMS["curvature_threshold"]))

    finite_tpi_s = np.isfinite(tpi_s)
    finite_tpi_l = np.isfinite(tpi_l)
    finite_sl_s  = np.isfinite(sl_s)
    finite_sl_l  = np.isfinite(sl_l)

    out = np.full(valid.shape, CLASS_FLAT, dtype=np.int8)

    # Baseline slope-vs-flat split.
    out = np.where(valid & finite_sl_s & (sl_s > 0.5 * th_sl_s),
                   CLASS_SLOPE, out)

    # Ledge: high small-scale slope + flat at distance.
    is_ledge = (valid & finite_sl_s & finite_sl_l
                & (sl_s >= th_sl_s) & (sl_l <= th_sl_l))
    out = np.where(is_ledge, CLASS_LEDGE, out)

    # Saddle: opposite-sign profile/planform curvature, both significant.
    is_saddle = (valid & (np.abs(prof) >= th_curv) & (np.abs(plan) >= th_curv)
                 & (np.sign(prof) != np.sign(plan)) & (prof != 0.0) & (plan != 0.0))
    out = np.where(is_saddle, CLASS_SADDLE, out)

    # Ridge: + tpi small + positive planform.
    is_ridge = (valid & finite_tpi_s
                & (tpi_s >= 0.5 * th_tpi_s) & (plan >= th_curv))
    out = np.where(is_ridge, CLASS_RIDGE, out)

    # Channel: − tpi small + negative planform.
    is_chan = (valid & finite_tpi_s
               & (tpi_s <= -0.5 * th_tpi_s) & (plan <= -th_curv))
    out = np.where(is_chan, CLASS_CHANNEL, out)

    # Pinnacle: + TPI at BOTH scales. Wins over ridge/saddle.
    is_pin = (valid & finite_tpi_s & finite_tpi_l
              & (tpi_s >= th_tpi_s) & (tpi_l >= th_tpi_l))
    out = np.where(is_pin, CLASS_PINNACLE, out)

    # Hole: − TPI at BOTH scales. Wins over channel/saddle.
    is_hole = (valid & finite_tpi_s & finite_tpi_l
               & (tpi_s <= -th_tpi_s) & (tpi_l <= -th_tpi_l))
    out = np.where(is_hole, CLASS_HOLE, out)

    out[~valid] = CLASS_FLAT
    return out


# ---------------------------------------------------------------------------
# Region extraction
# ---------------------------------------------------------------------------

def _gen_id(prefix="rg-"):
    return prefix + uuid.uuid4().hex[:12]


def _extract_regions(class_map, valid, params, selected_classes=None):
    """Connected components per class, with closing + min-area filtering.

    `selected_classes` (a set of internal class ids) restricts which classes
    are extracted. Unselected classes are skipped entirely — this is the
    structure-type filter: it's both free perf and avoids surfacing
    edge-case false positives in a class the user didn't ask for. `None`
    means "all surfaced classes" (back-compat for callers/tests).

    Each region carries:
        id, class_id, class, area_cells,
        bbox_px = (ymin, xmin, yend, xend)  [half-open, like numpy slices]
        mask_local — cropped bool mask the size of the bbox
        ys, xs   — 1D arrays of pixel coords in the full raster
    """
    morph_r = int(params.get("morph_close_radius",
                             DEFAULT_PARAMS["morph_close_radius"]))
    min_area = int(params.get("min_region_area_cells",
                              DEFAULT_PARAMS["min_region_area_cells"]))
    structure = np.ones((3, 3), dtype=bool)
    regions = []

    classes = (SURFACED_CLASSES if selected_classes is None
               else tuple(c for c in SURFACED_CLASSES if c in selected_classes))
    for cls in classes:
        mask = (class_map == cls) & valid
        if not mask.any():
            continue
        if morph_r > 0:
            mask = binary_closing(mask, iterations=morph_r, structure=structure)
            # Closing can grow into non-valid cells — clip back.
            mask = mask & valid
        labels, n = label(mask, structure=structure)
        if n == 0:
            continue
        for lbl in range(1, n + 1):
            comp = (labels == lbl)
            area = int(comp.sum())
            if area < min_area:
                continue
            ys, xs = np.where(comp)
            ymin, ymax = int(ys.min()), int(ys.max())
            xmin, xmax = int(xs.min()), int(xs.max())
            yend = ymax + 1
            xend = xmax + 1
            mask_local = comp[ymin:yend, xmin:xend]
            regions.append({
                "id":         _gen_id(),
                "class_id":   cls,
                "class":      CLASS_NAMES[cls],
                "area_cells": area,
                "bbox_px":    (ymin, xmin, yend, xend),
                "mask_local": mask_local,
                "ys":         ys,
                "xs":         xs,
            })
    return regions


# ---------------------------------------------------------------------------
# Centerline for linear regions
# ---------------------------------------------------------------------------

def _compute_centerline_px(ys, xs, n_segments=12):
    """Polyline along the region's principal axis, in (row, col) pixels.

    Bins region pixels along the projection onto the major axis (PCA on
    the 2-D pixel cloud) and takes the median (row, col) in each bin.
    This gives a polyline that follows the spine even for curved
    features, without needing a full morphological thinning pass.
    """
    pts = np.stack([xs, ys], axis=1).astype(np.float64)  # (N, 2) as (col, row)
    if len(pts) < 4:
        return None
    centroid = pts.mean(axis=0)
    centered = pts - centroid
    try:
        cov = np.cov(centered.T)
        if not np.all(np.isfinite(cov)):
            return None
        vals, vecs = np.linalg.eigh(cov)
    except np.linalg.LinAlgError:
        return None
    major = vecs[:, -1]
    t = centered @ major
    t_min, t_max = float(t.min()), float(t.max())
    if t_max - t_min < 2.0:
        return None
    n_bins = max(2, min(n_segments, int(math.ceil((t_max - t_min) / 3.0))))
    edges = np.linspace(t_min, t_max, n_bins + 1)
    bin_idx = np.clip(np.searchsorted(edges, t, side="right") - 1, 0, n_bins - 1)
    line = []
    for b in range(n_bins):
        in_b = (bin_idx == b)
        if not in_b.any():
            continue
        seg = pts[in_b]
        med = np.median(seg, axis=0)
        line.append((float(med[1]), float(med[0])))   # (row, col)
    return line if len(line) >= 2 else None


# ---------------------------------------------------------------------------
# Per-region scoring
# ---------------------------------------------------------------------------

def _shape_score(r, stack, params):
    """Class-dependent shape goodness in [0, 1]."""
    cls = r["class_id"]
    ys, xs = r["ys"], r["xs"]
    if cls in (CLASS_PINNACLE, CLASS_HOLE):
        # Compactness 4πA / P² → 1 for a perfect disc, 0 for a sliver.
        mask_local = r["mask_local"]
        eroded = binary_erosion(mask_local)
        perim = float((mask_local & ~eroded).sum())
        area = float(mask_local.sum())
        if perim <= 0 or area <= 0:
            return 0.0
        compactness = 4.0 * math.pi * area / (perim * perim)
        return float(np.clip(compactness, 0.0, 1.0))
    if cls in (CLASS_RIDGE, CLASS_LEDGE, CLASS_CHANNEL):
        pts = np.stack([xs, ys], axis=1).astype(np.float64)
        if len(pts) < 4:
            return 0.0
        centered = pts - pts.mean(axis=0)
        try:
            cov = np.cov(centered.T)
            if not np.all(np.isfinite(cov)):
                return 0.0
            vals = np.linalg.eigvalsh(cov)
        except np.linalg.LinAlgError:
            return 0.0
        vmin = max(float(vals[0]), 1e-9)
        vmax = max(float(vals[-1]), 0.0)
        elong = math.sqrt(vmax / vmin)
        # elong 1 ≈ round, 5 = clearly elongated, 8+ ≈ saturated.
        return float(np.clip(math.log1p(elong) / math.log1p(8.0), 0.0, 1.0))
    if cls == CLASS_SADDLE:
        prof = stack["prof_curv"][ys, xs]
        plan = stack["plan_curv"][ys, xs]
        if prof.size == 0 or plan.size == 0:
            return 0.0
        avg_prof = float(np.abs(prof).mean())
        avg_plan = float(np.abs(plan).mean())
        # Saddles need BOTH directions to be curved — take the min.
        magnitude = min(avg_prof, avg_plan)
        # Curvature is ~ params['curvature_threshold'] at the floor.
        scale = max(float(params.get("curvature_threshold",
                                     DEFAULT_PARAMS["curvature_threshold"])),
                    1e-6)
        return float(np.clip(magnitude / (scale * 4.0), 0.0, 1.0))
    return 0.0


def _score_regions(regions, elev, valid, stack, cell_x_m, cell_y_m, params):
    """Annotate each region with `score` + component values."""
    if not regions:
        return regions

    relief_raster = stack["relief"]
    h, w = elev.shape
    win_factor = float(params.get("local_window_factor",
                                  DEFAULT_PARAMS["local_window_factor"]))
    relief_scale = max(float(params.get("relief_scale_m",
                                        DEFAULT_PARAMS["relief_scale_m"])),
                       1e-6)
    isolation_scale = max(float(params.get("isolation_scale_m",
                                           DEFAULT_PARAMS["isolation_scale_m"])),
                          1e-6)
    depth_min = float(params.get("depth_fit_min_m",
                                 DEFAULT_PARAMS["depth_fit_min_m"]))
    depth_max = float(params.get("depth_fit_max_m",
                                 DEFAULT_PARAMS["depth_fit_max_m"]))
    depth_falloff = max(float(params.get("depth_fit_falloff_m",
                                         DEFAULT_PARAMS["depth_fit_falloff_m"])),
                        1e-6)

    w_r  = float(params.get("weight_relief",           DEFAULT_PARAMS["weight_relief"]))
    w_lp = float(params.get("weight_local_percentile", DEFAULT_PARAMS["weight_local_percentile"]))
    w_sh = float(params.get("weight_shape",            DEFAULT_PARAMS["weight_shape"]))
    w_is = float(params.get("weight_isolation",        DEFAULT_PARAMS["weight_isolation"]))
    w_df = float(params.get("weight_depth_fit",        DEFAULT_PARAMS["weight_depth_fit"]))

    # Pre-compute centroids in cell space (rows, cols).
    centroids = np.array([
        (float(r["ys"].mean()), float(r["xs"].mean())) for r in regions
    ])

    for i, r in enumerate(regions):
        ys, xs = r["ys"], r["xs"]
        ymin, xmin, yend, xend = r["bbox_px"]
        bh = yend - ymin
        bw = xend - xmin

        z = elev[ys, xs]
        finite = np.isfinite(z)
        if not finite.any():
            r["score"] = 0.0
            r["relief_m"] = 0.0
            r["local_percentile"] = 0.0
            r["shape_score"] = 0.0
            r["isolation_m"] = None
            r["depth_fit"] = 0.0
            r["mean_depth_m"] = 0.0
            continue
        z_finite = z[finite]
        z_min  = float(z_finite.min())
        z_max  = float(z_finite.max())
        z_mean = float(z_finite.mean())
        relief_m = max(0.0, z_max - z_min)

        # Local-percentile term: how does this region's relief compare to
        # the relief distribution in a much larger surrounding window?
        win_h = max(bh * 3, int(round(bh * win_factor)))
        win_w = max(bw * 3, int(round(bw * win_factor)))
        wy0 = max(0, ymin - win_h // 2)
        wy1 = min(h, yend + win_h // 2)
        wx0 = max(0, xmin - win_w // 2)
        wx1 = min(w, xend + win_w // 2)
        ngh = relief_raster[wy0:wy1, wx0:wx1]
        ngh_valid = valid[wy0:wy1, wx0:wx1]
        ngh_vals = ngh[ngh_valid]
        if ngh_vals.size > 0:
            local_percentile = float((ngh_vals < relief_m).sum()) / ngh_vals.size
        else:
            local_percentile = 0.5

        relief_score = float(np.clip(relief_m / relief_scale, 0.0, 1.0))
        shape_score  = _shape_score(r, stack, params)

        # Isolation: distance to the nearest OTHER region (any class).
        if len(centroids) > 1:
            dy_cells = centroids[:, 0] - centroids[i, 0]
            dx_cells = centroids[:, 1] - centroids[i, 1]
            d_m = np.hypot(dy_cells * cell_y_m, dx_cells * cell_x_m)
            d_m[i] = np.inf
            min_d_m = float(d_m.min())
            isolation_score = float(np.clip(min_d_m / isolation_scale, 0.0, 1.0))
        else:
            min_d_m = float("inf")
            isolation_score = 1.0

        # Depth fit. `z` is signed elevation (negative below sea level).
        depth = -z_mean
        if depth_min <= depth <= depth_max:
            depth_fit = 1.0
        elif depth < depth_min:
            depth_fit = float(np.clip(1.0 - (depth_min - depth) / depth_falloff,
                                      0.0, 1.0))
        else:
            depth_fit = float(np.clip(1.0 - (depth - depth_max) / depth_falloff,
                                      0.0, 1.0))

        score = (w_r  * relief_score
               + w_lp * local_percentile
               + w_sh * shape_score
               + w_is * isolation_score
               + w_df * depth_fit)
        r["score"]            = float(np.clip(score, 0.0, 1.0))
        r["relief_m"]         = relief_m
        r["local_percentile"] = local_percentile
        r["shape_score"]      = shape_score
        r["isolation_m"]      = None if math.isinf(min_d_m) else float(min_d_m)
        r["depth_fit"]        = depth_fit
        r["mean_depth_m"]     = z_mean
        # Confidence: tied to score, lifted slightly by area (a 3-cell
        # blob is less reliable than a 300-cell one).
        area_term = math.log1p(r["area_cells"]) / math.log1p(200.0)
        r["confidence"] = float(np.clip(
            0.5 + 0.4 * r["score"] + 0.1 * min(area_term, 1.0), 0.0, 1.0))
    return regions


# ---------------------------------------------------------------------------
# Region-level NMS
# ---------------------------------------------------------------------------

def _bbox_overlap(a, b, pad):
    ay0, ax0, ay1, ax1 = a
    by0, bx0, by1, bx1 = b
    return (ax0 - pad < bx1 and bx0 < ax1 + pad
            and ay0 - pad < by1 and by0 < ay1 + pad)


def _nms_regions(regions, params):
    """Drop adjacent weaker regions; record their class as a secondary tag."""
    ratio = float(params.get("nms_score_ratio",
                             DEFAULT_PARAMS["nms_score_ratio"]))
    pad = int(params.get("nms_adjacency_cells",
                         DEFAULT_PARAMS["nms_adjacency_cells"]))
    if not regions:
        return regions

    ordered = sorted(regions, key=lambda r: -r.get("score", 0.0))
    suppressed = set()
    kept = []

    for r in ordered:
        if r["id"] in suppressed:
            continue
        r.setdefault("secondary_tags", [])
        kept.append(r)
        for q in ordered:
            if q is r or q["id"] in suppressed:
                continue
            if not _bbox_overlap(r["bbox_px"], q["bbox_px"], pad):
                continue
            rs = r.get("score", 0.0)
            qs = q.get("score", 0.0)
            if qs <= 0 or rs >= ratio * qs:
                suppressed.add(q["id"])
                if q["class"] not in r["secondary_tags"]:
                    r["secondary_tags"].append(q["class"])
    return kept


# ---------------------------------------------------------------------------
# Post-classification size filter
#
# Real-world footprint sizing + the size gate. Everything here runs on the
# already-classified, already-scored regions; it never feeds back into
# classification or scoring.
# ---------------------------------------------------------------------------

def _hull_metric_pts(r, cell_x_m, cell_y_m):
    """Convex hull of the region footprint in GROUND METRES, as (x_m, y_m).

    Pixel coords are converted to ground metres up front using the source's
    Mercator-corrected per-axis sample distance (cell_x_m for columns,
    cell_y_m for rows), so all subsequent distance maths is true ground
    distance regardless of the raster's pixel aspect. Over a drawn search box
    (≤ a few km) this equals the great-circle distance between the same two
    points to far better than 0.1 %, so we measure on the projected hull
    rather than re-deriving haversine per vertex.

    Degenerate (collinear / <3 pt) footprints can't form a hull; we fall back
    to the metric bounding-box corners, which is both cheap and a safe bound.
    """
    xs_m = r["xs"].astype(np.float64) * cell_x_m
    ys_m = r["ys"].astype(np.float64) * cell_y_m
    pts = np.stack([xs_m, ys_m], axis=1)
    if len(pts) >= 3:
        try:
            hull = ConvexHull(pts)
            return pts[hull.vertices]
        except QhullError:
            pass
    ymin, xmin, yend, xend = r["bbox_px"]
    bx0, bx1 = xmin * cell_x_m, (xend - 1) * cell_x_m
    by0, by1 = ymin * cell_y_m, (yend - 1) * cell_y_m
    return np.array([[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]],
                    dtype=np.float64)


def _feret_max_m(hull_pts):
    """Maximum caliper diameter (longest extent) of a hull, in metres."""
    n = len(hull_pts)
    if n < 2:
        return 0.0
    best = 0.0
    for i in range(n):
        d = np.hypot(hull_pts[:, 0] - hull_pts[i, 0],
                     hull_pts[:, 1] - hull_pts[i, 1])
        m = float(d.max())
        if m > best:
            best = m
    return best


def _feret_min_width_m(hull_pts):
    """Minimum caliper width of a hull, in metres (rotating-calipers).

    The min-width support line of a convex polygon is parallel to one of its
    edges, so we take, for each edge, the perpendicular span of all vertices
    and return the smallest. `hull_pts` is assumed convex and ordered (as
    ConvexHull.vertices returns); the bbox-corner fallback is convex too.
    """
    n = len(hull_pts)
    if n < 3:
        # A line/point has no meaningful width — fall back to its length so a
        # sliver isn't auto-passed by a zero width sneaking under min_ft.
        return _feret_max_m(hull_pts)
    best = float("inf")
    for i in range(n):
        a = hull_pts[i]
        b = hull_pts[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        length = math.hypot(ex, ey)
        if length < 1e-9:
            continue
        # Unit normal to the edge; project every vertex onto it.
        nx, ny = -ey / length, ex / length
        proj = (hull_pts[:, 0] - a[0]) * nx + (hull_pts[:, 1] - a[1]) * ny
        width = float(proj.max() - proj.min())
        if width < best:
            best = width
    return best if math.isfinite(best) else _feret_max_m(hull_pts)


def _region_size_ft(r, cell_x_m, cell_y_m, measure):
    """Real-world size of a region's footprint in feet, per `measure`."""
    hull_pts = _hull_metric_pts(r, cell_x_m, cell_y_m)
    size_m = (_feret_min_width_m(hull_pts) if measure == "width"
              else _feret_max_m(hull_pts))
    return float(size_m * _M_TO_FT)


def _filter_regions_by_size(regions, size_ranges, cell_x_m, cell_y_m):
    """Drop regions whose footprint is outside their type's [min_ft, max_ft].

    Annotates every region (kept or not, for traceability) with `size_ft` +
    `size_measure`, then keeps only those inside the configured band. A region
    whose class maps to no known structure type (shouldn't happen — extraction
    is already class-filtered) is passed through unfiltered.
    """
    if not regions:
        return regions
    kept = []
    for r in regions:
        tkey = _CLASS_TO_STRUCTURE_TYPE.get(r["class_id"])
        measure = SIZE_MEASURE.get(tkey, "longest")
        size_ft = _region_size_ft(r, cell_x_m, cell_y_m, measure)
        r["size_ft"] = size_ft
        r["size_measure"] = measure
        rng = size_ranges.get(tkey) if tkey else None
        if rng is None or (rng["min_ft"] <= size_ft <= rng["max_ft"]):
            kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# Composite pass
# ---------------------------------------------------------------------------

def _composite_pass(regions, cell_x_m, cell_y_m, params):
    """Cluster high-scoring regions whose proximity is mutually close."""
    if not regions:
        return []
    threshold = float(params.get("composite_score_threshold",
                                 DEFAULT_PARAMS["composite_score_threshold"]))
    factor = float(params.get("composite_radius_factor",
                              DEFAULT_PARAMS["composite_radius_factor"]))
    min_members = int(params.get("composite_min_members",
                                 DEFAULT_PARAMS["composite_min_members"]))

    high = [r for r in regions if r.get("score", 0.0) >= threshold]
    if len(high) < min_members:
        return []

    n = len(high)
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    centroids = []
    char_sizes = []
    for r in high:
        cy_m = float(r["ys"].mean()) * cell_y_m
        cx_m = float(r["xs"].mean()) * cell_x_m
        centroids.append((cy_m, cx_m))
        ymin, xmin, yend, xend = r["bbox_px"]
        char_sizes.append(
            math.hypot((yend - ymin) * cell_y_m, (xend - xmin) * cell_x_m) / 2.0
        )

    for i in range(n):
        for j in range(i + 1, n):
            d = math.hypot(centroids[i][0] - centroids[j][0],
                           centroids[i][1] - centroids[j][1])
            radius = factor * max(char_sizes[i] + char_sizes[j], 1e-3)
            if d <= radius:
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    composites = []
    for members_idx in clusters.values():
        if len(members_idx) < min_members:
            continue
        member_ids = []
        scores = []
        hull_pts = []
        for idx in members_idx:
            r = high[idx]
            member_ids.append(r["id"])
            scores.append(r.get("score", 0.0))
            ymin, xmin, yend, xend = r["bbox_px"]
            hull_pts.append((xmin, ymin))
            hull_pts.append((xend - 1, ymin))
            hull_pts.append((xmin, yend - 1))
            hull_pts.append((xend - 1, yend - 1))
        pts = np.array(hull_pts, dtype=np.float64)
        try:
            if len(pts) >= 3:
                hull = ConvexHull(pts)
                hull_verts = pts[hull.vertices]
            else:
                hull_verts = pts
        except QhullError:
            hull_verts = pts
        # Aggregate score: average lifted by member count (3 medium spots
        # often beat one strong one).
        avg = float(np.mean(scores))
        bonus = min(0.1 * (len(members_idx) - 1), 0.25)
        agg = float(np.clip(avg + bonus, 0.0, 1.0))
        composites.append({
            "id":         _gen_id("cp-"),
            "member_ids": member_ids,
            "score":      agg,
            "hull_px":    [(float(p[1]), float(p[0])) for p in hull_verts],  # (row, col)
        })
    return composites


# ---------------------------------------------------------------------------
# Score raster → heatmap PNG
# ---------------------------------------------------------------------------

def _paint_score_raster(regions, shape):
    """Paint each region's score onto a raster (max-blend on overlaps)."""
    out = np.zeros(shape, dtype=np.float32)
    for r in regions:
        ys, xs = r["ys"], r["xs"]
        s = float(r.get("score", 0.0))
        if s <= 0.0:
            continue
        cur = out[ys, xs]
        out[ys, xs] = np.maximum(cur, s)
    return out


_COLOR_STOPS = (
    (0.05, 255, 255, 180,   0),
    (0.30, 255, 230, 110, 110),
    (0.60, 255, 170,  50, 200),
    (1.00, 255,  70,  60, 235),
)


def _colorize(score_local, valid_local):
    h, w = score_local.shape
    s = np.clip(score_local, 0.0, 1.0).astype(np.float32)

    r = np.zeros((h, w), dtype=np.float32)
    g = np.zeros((h, w), dtype=np.float32)
    b = np.zeros((h, w), dtype=np.float32)
    a = np.zeros((h, w), dtype=np.float32)

    for i in range(1, len(_COLOR_STOPS)):
        lo = _COLOR_STOPS[i - 1]
        hi = _COLOR_STOPS[i]
        in_range = (s > lo[0]) & (s <= hi[0])
        span = max(hi[0] - lo[0], 1e-9)
        f = (s - lo[0]) / span
        r = np.where(in_range, lo[1] + (hi[1] - lo[1]) * f, r)
        g = np.where(in_range, lo[2] + (hi[2] - lo[2]) * f, g)
        b = np.where(in_range, lo[3] + (hi[3] - lo[3]) * f, b)
        a = np.where(in_range, lo[4] + (hi[4] - lo[4]) * f, a)

    above = s > _COLOR_STOPS[-1][0]
    last = _COLOR_STOPS[-1]
    r = np.where(above, last[1], r)
    g = np.where(above, last[2], g)
    b = np.where(above, last[3], b)
    a = np.where(above, last[4], a)

    a = a * valid_local

    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0] = np.clip(r, 0, 255).astype(np.uint8)
    rgba[..., 1] = np.clip(g, 0, 255).astype(np.uint8)
    rgba[..., 2] = np.clip(b, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.clip(a, 0, 255).astype(np.uint8)
    return rgba


def _render_heatmap_png(score, mask, area):
    """Resample score into the rectangle's LOCAL frame and PNG-encode it."""
    width_m = max(1.0, float(area["width_m"]))
    height_m = max(1.0, float(area["height_m"]))
    aspect = width_m / height_m
    long_side = 512
    if aspect >= 1.0:
        out_w = long_side
        out_h = max(64, int(round(long_side / aspect)))
    else:
        out_h = long_side
        out_w = max(64, int(round(long_side * aspect)))

    h_src, w_src = score.shape
    xmin, ymin, xmax, ymax = _aabb_bbox_3857(area)

    center_lat = area["center"]["lat"]
    cos_lat = max(math.cos(math.radians(center_lat)), 1e-9)
    cx_m = _lng_to_merc_x(area["center"]["lng"])
    cy_m = _lat_to_merc_y(center_lat)
    half_w_merc = (width_m / 2.0) / cos_lat
    half_h_merc = (height_m / 2.0) / cos_lat

    rot = math.radians(area["rotation_deg"])
    cos_r = math.cos(rot)
    sin_r = math.sin(rot)

    lx = (np.arange(out_w, dtype=np.float32) + 0.5) / out_w
    ly = (np.arange(out_h, dtype=np.float32) + 0.5) / out_h
    dx_local = (lx[None, :] - 0.5) * 2.0 * half_w_merc
    dy_local = (0.5 - ly[:, None]) * 2.0 * half_h_merc
    dx_aabb = dx_local * cos_r + dy_local * sin_r
    dy_aabb = -dx_local * sin_r + dy_local * cos_r

    x_m = cx_m + dx_aabb
    y_m = cy_m + dy_aabb

    src_cols = (x_m - xmin) / (xmax - xmin) * w_src - 0.5
    src_rows = (ymax - y_m) / (ymax - ymin) * h_src - 0.5
    coords = np.stack([src_rows, src_cols], axis=0)

    score_filled = np.where(np.isfinite(score) & mask, score, 0.0).astype(np.float32)
    valid_f = mask.astype(np.float32) * np.isfinite(score).astype(np.float32)

    sampled = map_coordinates(score_filled, coords, order=1,
                              mode="constant", cval=0.0)
    sampled_valid = map_coordinates(valid_f, coords, order=1,
                                    mode="constant", cval=0.0)
    rgba = _colorize(sampled, (sampled_valid > 0.5).astype(np.float32))

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return data_url


# ---------------------------------------------------------------------------
# Pixel → lat/lng helpers and output assembly
# ---------------------------------------------------------------------------

def _pixel_to_lat_lng(row, col, raster_shape, area):
    h, w = raster_shape
    xmin, ymin, xmax, ymax = _aabb_bbox_3857(area)
    x_m = xmin + (col + 0.5) / w * (xmax - xmin)
    y_m = ymax - (row + 0.5) / h * (ymax - ymin)
    return _merc_y_to_lat(y_m), _merc_x_to_lng(x_m)


def _region_polygon_px(r, max_vertices=64):
    """Convex hull of region pixels, returned as a list of (row, col) tuples."""
    pts = np.stack([r["xs"], r["ys"]], axis=1).astype(np.float64)
    if len(pts) < 4:
        ymin, xmin, yend, xend = r["bbox_px"]
        return [(ymin, xmin), (ymin, xend - 1),
                (yend - 1, xend - 1), (yend - 1, xmin)]
    try:
        hull = ConvexHull(pts)
        verts = pts[hull.vertices]
    except QhullError:
        ymin, xmin, yend, xend = r["bbox_px"]
        return [(ymin, xmin), (ymin, xend - 1),
                (yend - 1, xend - 1), (yend - 1, xmin)]
    # Cap polygon vertex count — long curved regions don't need 1k points
    # on the wire and the renderer just antialiases the polyline.
    if len(verts) > max_vertices:
        step = len(verts) / max_vertices
        verts = verts[(np.arange(max_vertices) * step).astype(int)]
    return [(float(v[1]), float(v[0])) for v in verts]


def _px_polyline_to_latlng(poly_px, raster_shape, area):
    return [
        _latlng_pair(_pixel_to_lat_lng(row, col, raster_shape, area))
        for (row, col) in poly_px
    ]


def _latlng_pair(t):
    return {"lat": float(t[0]), "lng": float(t[1])}


def _safe_finite(value, default=0.0):
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public entry point — generator yielding NDJSON events
# ---------------------------------------------------------------------------

def run_spotfinder(payload):
    """
    Generator. Yields one of:
        {'type': 'progress', 'pct', 'label', 'remaining_ms'}
        {'type': 'result',   'result': <SpotfinderResult-shaped dict>}
        {'type': 'error',    'message': str}
    """
    t0 = time.monotonic()

    def progress(pct, label):
        pct = float(max(0.0, min(100.0, pct)))
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        remaining_ms = None
        if pct > 1.0:
            remaining_ms = int(elapsed_ms / pct * (100.0 - pct))
        return {
            "type": "progress",
            "pct": pct,
            "label": label,
            "remaining_ms": remaining_ms,
        }

    progress_queue = []

    def emit(pct, label):
        progress_queue.append(progress(pct, label))

    def flush():
        while progress_queue:
            yield progress_queue.pop(0)

    try:
        area = payload.get("search_area") if isinstance(payload, dict) else None
        if not area or "corners" not in area or "bbox" not in area:
            yield {"type": "error",
                   "message": "invalid search_area in request"}
            return

        # Resolve the user-facing config (environment mode + structure types
        # + preferred source) into concrete params, the set of classes to
        # surface, and a source preference. See resolve_config().
        cfg = resolve_config(payload)
        params = cfg["params"]
        selected_classes = cfg["selected_classes"]
        size_ranges = cfg["size_ranges"]

        yield progress(0.5, "Resolving data source…")
        source, native_res_m, coverage = _resolve_source(
            area, emit, cfg["source_id"])
        for ev in flush():
            yield ev
        yield progress(8.0,
                       f"Using {source.display_name} "
                       f"(coverage {int(coverage * 100)}%)…")

        yield progress(10.0, "Fetching bathymetry…")
        elev, cell_x_m, cell_y_m = _fetch_full_raster(source, area, native_res_m)
        h, w = elev.shape

        yield progress(14.0, "Building masks…")
        rect_mask = _build_rotated_mask(elev.shape, area)
        valid = rect_mask & np.isfinite(elev) & (elev < 0)
        if int(valid.sum()) < 200:
            yield {"type": "error",
                   "message": ("Not enough underwater bathymetry inside the "
                               "selected area. Pick an area that contains "
                               "more open water.")}
            return
        yield progress(18.0, f"Loaded {w}×{h} cells")

        # ── Preprocess (18 → 25 %) ──
        yield progress(20.0, "Smoothing + gap-fill…")
        elev_p, valid_p = _preprocess(elev, valid, params)
        if int(valid_p.sum()) < 200:
            yield {"type": "error",
                   "message": ("Not enough valid bathymetry after preprocessing. "
                               "Try a different area.")}
            return

        # ── Multi-scale stack (25 → 55 %) ──
        yield progress(28.0, "Computing slope (multi-scale)…")
        stack = _derivative_stack(elev_p, valid_p, cell_x_m, cell_y_m, params)
        yield progress(52.0, "Derivative stack complete")

        # ── Classify + extract regions (55 → 75 %) ──
        yield progress(58.0, "Classifying terrain…")
        class_map = _classify(stack, valid_p, params)

        yield progress(65.0, "Extracting regions…")
        regions = _extract_regions(class_map, valid_p, params, selected_classes)

        # ── Score (75 → 88 %) ──
        yield progress(76.0, "Scoring regions…")
        regions = _score_regions(regions, elev_p, valid_p, stack,
                                 cell_x_m, cell_y_m, params)
        # Drop everything below the configurable score floor before NMS so
        # we don't waste cycles suppressing junk.
        threshold = float(params.get("score_threshold",
                                     DEFAULT_PARAMS["score_threshold"]))
        regions = [r for r in regions if r.get("score", 0.0) >= threshold]
        max_regions = int(params.get("max_regions",
                                     DEFAULT_PARAMS["max_regions"]))
        regions = sorted(regions, key=lambda r: -r.get("score", 0.0))[:max_regions]

        # ── NMS + composites (88 → 94 %) ──
        yield progress(86.0, "Suppressing duplicates…")
        regions = _nms_regions(regions, params)

        # ── Size filter (88 %) ──
        # Post-classification output gate: detection/scoring/NMS above ran
        # unchanged; here we drop regions whose real-world footprint is
        # outside the per-type [min_ft, max_ft]. Done BEFORE composites and
        # the heatmap so every downstream artifact reflects the same set the
        # user will see.
        yield progress(88.0, "Applying size filter…")
        regions = _filter_regions_by_size(
            regions, size_ranges, cell_x_m, cell_y_m)

        yield progress(90.0, "Clustering composites…")
        composites_raw = _composite_pass(regions, cell_x_m, cell_y_m, params)

        # ── Centerlines (linear classes only) ──
        for r in regions:
            if r["class_id"] in LINEAR_CLASSES:
                r["centerline_px"] = _compute_centerline_px(r["ys"], r["xs"])
            else:
                r["centerline_px"] = None

        # ── Heatmap (94 → 98 %) ──
        yield progress(94.0, "Painting score raster…")
        score_r = _paint_score_raster(regions, elev_p.shape)
        yield progress(96.0, "Rendering heatmap…")
        heatmap_url = _render_heatmap_png(score_r, valid_p, area)

        # ── Output assembly (98 → 100 %) ──
        yield progress(98.0, "Finalising…")
        spots_out = []
        regions_out = []
        for r in regions:
            cy = float(r["ys"].mean())
            cx = float(r["xs"].mean())
            lat, lng = _pixel_to_lat_lng(cy, cx, elev_p.shape, area)

            polygon_px = _region_polygon_px(r)
            polygon = _px_polyline_to_latlng(polygon_px, elev_p.shape, area)
            centerline_px = r.get("centerline_px")
            centerline = (_px_polyline_to_latlng(centerline_px, elev_p.shape, area)
                          if centerline_px else None)
            ymin, xmin, yend, xend = r["bbox_px"]
            bb_lat0, bb_lng0 = _pixel_to_lat_lng(ymin, xmin, elev_p.shape, area)
            bb_lat1, bb_lng1 = _pixel_to_lat_lng(yend - 1, xend - 1,
                                                 elev_p.shape, area)

            # Region orientation (degrees CW from north) for linear classes.
            aspect_deg = None
            length_m = None
            if r["class_id"] in LINEAR_CLASSES and centerline_px and len(centerline_px) >= 2:
                (r0, c0) = centerline_px[0]
                (r1, c1) = centerline_px[-1]
                dy_m = (r0 - r1) * cell_y_m
                dx_m = (c1 - c0) * cell_x_m
                if dx_m != 0 or dy_m != 0:
                    aspect_deg = (math.degrees(math.atan2(dx_m, dy_m)) + 360.0) % 360.0
                # Length: sum of segment lengths in metres
                length_m = 0.0
                for k in range(1, len(centerline_px)):
                    a_r, a_c = centerline_px[k - 1]
                    b_r, b_c = centerline_px[k]
                    length_m += math.hypot((b_r - a_r) * cell_y_m,
                                           (b_c - a_c) * cell_x_m)

            metrics = {
                "relief_m":         _safe_finite(r.get("relief_m")),
                "local_percentile": _safe_finite(r.get("local_percentile")),
                "mean_depth_m":     _safe_finite(r.get("mean_depth_m")),
                "shape_score":      _safe_finite(r.get("shape_score")),
                "depth_fit":        _safe_finite(r.get("depth_fit")),
                "isolation_m":      None if r.get("isolation_m") is None
                                         else _safe_finite(r.get("isolation_m")),
                "area_cells":       int(r["area_cells"]),
                "length_m":         None if length_m is None
                                         else _safe_finite(length_m),
                "aspect_deg":       None if aspect_deg is None
                                         else _safe_finite(aspect_deg),
                # Footprint size used by the size filter, in feet, plus which
                # measure produced it ("longest" caliper diameter for compact
                # types, "width" min caliper for elongated types).
                "size_ft":          _safe_finite(r.get("size_ft")),
                "size_measure":     r.get("size_measure", "longest"),
            }

            regions_out.append({
                "id":             r["id"],
                "class":          r["class"],
                "score":          float(r.get("score", 0.0)),
                "centroid":       {"lat": float(lat), "lng": float(lng)},
                "polygon":        polygon,
                "centerline":     centerline,
                "bbox": {
                    "north": max(bb_lat0, bb_lat1),
                    "south": min(bb_lat0, bb_lat1),
                    "east":  max(bb_lng0, bb_lng1),
                    "west":  min(bb_lng0, bb_lng1),
                },
                "metrics":        metrics,
                "secondary_tags": list(r.get("secondary_tags", [])),
                "confidence":     _safe_finite(r.get("confidence", 0.5)),
            })

            # Back-compat: each region also surfaces a `spot` at its
            # centroid, so the existing point-marker renderer keeps
            # working unchanged. Depth is reported positive (metres
            # below sea level) — matches the legacy contract.
            spots_out.append({
                "id":      r["id"],
                "lat":     float(lat),
                "lng":     float(lng),
                "depth_m": _safe_finite(-r.get("mean_depth_m", 0.0)),
                "score":   float(r.get("score", 0.0)),
                "features": {
                    "class":            r["class"],
                    "relief_m":         _safe_finite(r.get("relief_m")),
                    "local_percentile": _safe_finite(r.get("local_percentile")),
                    "shape_score":      _safe_finite(r.get("shape_score")),
                    "depth_fit":        _safe_finite(r.get("depth_fit")),
                    "secondary_tags":   list(r.get("secondary_tags", [])),
                },
            })

        # Resolve composite hull pixel coords to lat/lng.
        composites_out = []
        for c in composites_raw:
            polygon = [
                _latlng_pair(_pixel_to_lat_lng(row, col, elev_p.shape, area))
                for (row, col) in c["hull_px"]
            ]
            composites_out.append({
                "id":         c["id"],
                "member_ids": list(c["member_ids"]),
                "score":      float(c["score"]),
                "polygon":    polygon,
            })

        cellsize_m = (cell_x_m + cell_y_m) / 2.0
        # Sort spots/regions by score descending so the renderer doesn't have to.
        spots_out.sort(key=lambda s: -s["score"])
        regions_out.sort(key=lambda r: -r["score"])

        result = {
            "run_id":    _gen_id("sf-"),
            "timestamp": _now_iso(),
            "search_area": area,
            "params":    params,
            # The user-facing config this run was produced with. `source`
            # is the source ACTUALLY used (after resolution / fallback), so
            # the page reflects reality, not the request.
            "config": {
                "environment":     cfg["environment"],
                "structure_types": list(cfg["structure_types"]),
                "source":          source.id,
                # Full per-type size config this run was produced with, so a
                # saved/renamed run remembers exactly what size constraints
                # shaped it (the map page surfaces these in the run settings).
                "size_ranges":     {k: dict(v) for k, v in size_ranges.items()},
            },
            "heatmap_png_url": heatmap_url,
            "heatmap_corners": [
                {"lat": c["lat"], "lng": c["lng"]} for c in area["corners"]
            ],
            "spots":      spots_out,
            "regions":    regions_out,
            "composites": composites_out,
            "manifest": {
                "data_source":  source.display_name,
                "source_id":    source.id,
                "resolution_m": round(cellsize_m, 2),
                "cell_count":   int(valid_p.sum()),
                "runtime_ms":   int((time.monotonic() - t0) * 1000),
                "rotation_deg": float(area["rotation_deg"]),
                "environment":       cfg["environment"],
                "environment_label": ENVIRONMENT_MODES[cfg["environment"]]["label"],
                "structure_types":   list(cfg["structure_types"]),
            },
            # Backwards-compat fields the storage layer still mirrors.
            "bbox":           dict(area["bbox"]),
            "heatmap_bounds": dict(area["bbox"]),
        }

        yield progress(100.0, "Done.")
        yield {"type": "result", "result": result}

    except SpotfinderError as e:
        yield {"type": "error", "message": str(e)}
    except Exception as e:
        # Never expose a raw stacktrace, but log it so the dev console
        # has something to chase.
        print(f"[spotfinder] unhandled error: {e!r}")
        import traceback; traceback.print_exc()
        yield {"type": "error",
               "message": f"Spotfinder failed: {e.__class__.__name__}"}
