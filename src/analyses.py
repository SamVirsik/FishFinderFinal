"""
Visualization algorithms for the bathymetric rasters.

Each public analysis takes:
    elev_m       — float32 ndarray of elevation in METRES (NOAA convention:
                   positive above sea level, negative below). May contain 0s
                   where the upstream nodata mask was applied; the LayerGenerator
                   re-applies that mask to the alpha channel after rendering.
    cellsize_m   — real ground sample distance in METRES per pixel for this
                   tile, accounting for Mercator stretch. Lets every analysis
                   express its parameters in physical units (degrees of slope,
                   metres of feature size, etc.) so output is consistent across
                   zoom levels.
    param        — single integer knob exposed in the UI. Each analysis
                   interprets it in its own natural unit (depth ft, slope deg,
                   feature size m, vertical exaggeration, etc.). The UI
                   re-labels and re-bounds the slider per analysis.

All return a PIL.Image in 'RGBA'.

The set is intentionally small (8 analyses), each visually and analytically
distinct from the others.
"""

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import gaussian_filter

M_TO_FT = 3.28084


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# Land = transparent (basemap shows through). Water = increasing depth.
DEPTH_PALETTE = np.array([
    (  0,   0,   0,   0),  # 0: land (transparent)
    (212, 255,   1, 255), (190, 255,   1, 255),
    (166, 255,  17, 255), (139, 255,  17, 255), (115, 255,  49, 255),
    ( 83, 255,  84, 255), ( 53, 255, 114, 255), ( 53, 255, 147, 255),
    ( 53, 255, 199, 255), ( 17, 255, 220, 255), (  0, 255, 249, 255),
    (  0, 245, 249, 255), (  0, 226, 249, 255), (  0, 212, 242, 255),
    (  0, 198, 242, 255), (  0, 182, 242, 255), (  0, 167, 242, 255),
    (  0, 150, 242, 255), (  0, 128, 242, 255), (  0, 110, 218, 255),
    (  0,  88, 218, 255), (  0,  61, 215, 255), (  0,  30, 215, 255),
    (  0,   0, 207, 255), (  0,   0, 187, 255), (  0,   0, 163, 255),
    (  0,   0, 140, 255), (  0,   0, 128, 255), (  0,   0, 108, 255),
    (  0,   0,  88, 255),
], dtype=np.uint8)

# Same palette but with a brown for land — for color-relief, where we want
# land to be visible (and the hillshade is going to darken it anyway).
LAND_DEPTH_PALETTE = DEPTH_PALETTE.copy()
LAND_DEPTH_PALETTE[0] = (139, 69, 19, 255)

# Slope palette: cool green → yellow → red → purple as steepness rises.
SLOPE_PALETTE = np.array([
    (220, 255, 220, 255), (184, 255, 184, 255),
    (140, 255, 140, 255), (100, 255, 100, 255),
    (255, 255, 100, 255), (255, 230,  50, 255),
    (255, 200,   0, 255), (255, 160,   0, 255),
    (255, 120,   0, 255), (255,  80,   0, 255),
    (255,  40,   0, 255), (220,   0, 120, 255),
    (160,   0, 180, 255),
], dtype=np.uint8)

CONTOUR_COLOR = np.array([0, 0, 0, 255], dtype=np.uint8)
SPOT_COLOR    = np.array([255, 30, 255, 255], dtype=np.uint8)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _depth_below_sea_ft(elev_m):
    """Magnitude of depth below sea level in feet, with land clamped to 0."""
    return np.where(elev_m < 0, -elev_m * M_TO_FT, 0.0).astype(np.float32, copy=False)


def _land_mask(elev_m):
    return elev_m >= 0


def _slope_components(elev_m, cellsize_m):
    """Real elevation gradients (m of dz per m of dx/dy)."""
    dzdy, dzdx = np.gradient(elev_m, cellsize_m, cellsize_m)
    return dzdx, dzdy


def _slope_degrees(elev_m, cellsize_m):
    """True slope angle from horizontal, in degrees (0–90)."""
    dzdx, dzdy = _slope_components(elev_m, cellsize_m)
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy)))


def _hillshade(elev_m, cellsize_m, *, azimuth=315.0, altitude=45.0,
               exaggeration=5.0):
    """
    0..1 illumination layer. NOAA convention puts water below sea level,
    so we negate to make deep water read as "high terrain" — gives a
    consistent shaded look whether or not a tile contains land.
    """
    z = -elev_m * exaggeration
    dzdy, dzdx = np.gradient(z, cellsize_m, cellsize_m)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    aspect = np.where(aspect < 0, aspect + 2 * np.pi, aspect)

    az = np.deg2rad(azimuth)
    zenith = np.deg2rad(90.0 - altitude)
    hs = (np.cos(zenith) * np.cos(slope)
          + np.sin(zenith) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(hs, 0.0, 1.0).astype(np.float32)


def _depth_band_index(depth_ft, band_ft, palette_len, land_mask):
    """Map depth (ft) to palette slot. Slot 0 reserved for land."""
    idx = (depth_ft / max(band_ft, 1e-3)).astype(np.int32, copy=False) + 1
    np.clip(idx, 1, palette_len - 1, out=idx)
    idx[land_mask] = 0
    return idx


def _band_edge_mask(idx):
    """Pixels whose band differs from the right or below neighbour."""
    mask = np.zeros(idx.shape, dtype=bool)
    mask[:-1, :] |= idx[:-1, :] != idx[1:, :]
    mask[:, :-1] |= idx[:, :-1] != idx[:, 1:]
    return mask


def _to_rgba(rgb, alpha=255):
    """Stack a uint8 [...,3] RGB array into an RGBA image."""
    out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = alpha
    return Image.fromarray(out, mode='RGBA')


def _grey_to_rgba(grey, alpha=255):
    grey = grey.astype(np.uint8, copy=False)
    out = np.empty(grey.shape + (4,), dtype=np.uint8)
    out[..., 0] = grey
    out[..., 1] = grey
    out[..., 2] = grey
    out[..., 3] = alpha
    return Image.fromarray(out, mode='RGBA')


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def color_relief(elev_m, cellsize_m, param):
    """
    Best general-purpose view: depth-coloured terrain with a hillshade
    overlay. `param` = vertical exaggeration (1–50, default ~5).
    """
    exaggeration = max(1.0, float(param))
    hs = _hillshade(elev_m, cellsize_m, exaggeration=exaggeration)

    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)
    # Use a fixed depth-step that's smooth enough to look continuous.
    idx = _depth_band_index(depth_ft, 4.0, LAND_DEPTH_PALETTE.shape[0], land)
    rgba = LAND_DEPTH_PALETTE[idx]

    ambient = 0.35
    lit = (ambient + hs * (1.0 - ambient))[..., None]
    out = np.empty_like(rgba)
    out[..., :3] = np.clip(rgba[..., :3].astype(np.float32) * lit,
                           0, 255).astype(np.uint8)
    out[..., 3] = rgba[..., 3]
    return Image.fromarray(out, mode='RGBA')


def depth_smooth(elev_m, cellsize_m, param):
    """
    Continuous viridis depth gradient. `param` = max depth shown (ft);
    deeper pixels saturate to the deep colour. Land is transparent.
    """
    max_ft = max(10.0, float(param))
    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)

    norm = np.clip(depth_ft / max_ft, 0.0, 1.0)
    rgb = (plt.get_cmap('viridis')(norm)[..., :3] * 255).astype(np.uint8)

    out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = np.where(land, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode='RGBA')


def depth_bands(elev_m, cellsize_m, param):
    """
    Discrete depth bands with crisp black contour lines on every band edge.
    `param` = band size in feet (e.g. 10 → one colour step every 10 ft).
    """
    band_ft = max(0.5, float(param))
    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)

    idx = _depth_band_index(depth_ft, band_ft, DEPTH_PALETTE.shape[0], land)
    rgba = DEPTH_PALETTE[idx].copy()
    rgba[_band_edge_mask(idx)] = CONTOUR_COLOR
    return Image.fromarray(rgba, mode='RGBA')


def hillshade(elev_m, cellsize_m, param):
    """
    Pure greyscale shaded relief on a black background. Reveals sea-floor
    structure without any depth colouring. `param` = vertical exaggeration.
    """
    exaggeration = max(1.0, float(param))
    hs = _hillshade(elev_m, cellsize_m, exaggeration=exaggeration)
    return _grey_to_rgba(hs * 255.0)


def slope(elev_m, cellsize_m, param):
    """
    Real slope angle (degrees from horizontal). `param` = max slope on the
    colour scale: anything steeper saturates to purple. Smaller `param`
    exaggerates subtle drop-offs; larger smooths them out.
    """
    max_deg = max(2.0, float(param))
    deg = _slope_degrees(elev_m, cellsize_m)
    # Land alpha is handled by the LayerGenerator nodata mask, but a clear
    # land/water break still helps; mute slope on land slightly.
    land = _land_mask(elev_m)

    # Map [0..max_deg] across palette slots.
    idx = (deg / max_deg * (SLOPE_PALETTE.shape[0] - 1)).astype(np.int32)
    np.clip(idx, 0, SLOPE_PALETTE.shape[0] - 1, out=idx)
    rgba = SLOPE_PALETTE[idx].copy()
    rgba[land, 3] = 80  # let basemap show through on land
    return Image.fromarray(rgba, mode='RGBA')


def aspect(elev_m, cellsize_m, param):
    """
    Direction the sea floor faces (compass bearing of the down-slope vector),
    coloured as an HSV wheel. Saturation/value drop on near-flat ground so
    the eye isn't drawn to noise. `param` = minimum slope (deg) below which
    a pixel reads as neutral grey.
    """
    min_slope_deg = max(0.5, float(param))
    dzdx, dzdy = _slope_components(elev_m, cellsize_m)
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))

    # Aspect: compass bearing the down-slope direction faces (0=N, 90=E).
    # In image arrays, rows increase southward, so the north component of
    # the down-slope vector is +dzdy (since uphill-y = +dzdy and we want -uphill).
    # The east component is -dzdx. Compass bearing = atan2(east, north).
    asp = np.degrees(np.arctan2(-dzdx, dzdy))
    asp = np.mod(asp, 360.0)
    hue = (asp / 360.0).astype(np.float32)

    # Saturation rises with slope; flat areas → grey.
    sat = np.clip(slope_deg / (min_slope_deg * 4.0), 0.0, 1.0).astype(np.float32)
    val = np.full_like(hue, 0.95, dtype=np.float32)

    rgb = _hsv_to_rgb(hue, sat, val)
    rgba = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    rgba[..., :3] = (rgb * 255).astype(np.uint8)
    rgba[..., 3] = 255

    # Below the threshold → fully transparent so the basemap shows through.
    weak = slope_deg < min_slope_deg
    rgba[weak, 3] = 0
    rgba[_land_mask(elev_m), 3] = 0
    return Image.fromarray(rgba, mode='RGBA')


def roughness(elev_m, cellsize_m, param):
    """
    Local sea-floor roughness — the magnitude of elevation deviation from a
    smoothed background at a chosen feature scale. Bright = rough (wrecks,
    ledges, rubble); dark = smooth. `param` = feature scale in METRES; we
    convert to a Gaussian sigma in pixels for the local mean.
    """
    feature_scale_m = max(2.0, float(param))
    sigma_px = max(0.5, feature_scale_m / cellsize_m)

    background = gaussian_filter(elev_m, sigma_px, mode='nearest')
    resid_m = np.abs(elev_m - background)

    # Express the dynamic range relative to feature scale: an excursion of
    # ~5% of the feature scale (e.g. 2.5 m on a 50 m feature) reads as full
    # white. This keeps the visual range comparable across zoom levels.
    full_white = 0.05 * feature_scale_m
    grey = np.clip(resid_m / max(full_white, 1e-3), 0.0, 1.0) * 255.0
    return _grey_to_rgba(grey)


# Per-band thresholds for slope steepness (deg) that flag a "fishy" pixel.
# Shallower spots need less slope to be interesting; deep water needs more.
_SPOT_DEPTH_BREAKS_FT = (0.0, 30.0, 80.0, 200.0, 600.0, np.inf)
_SPOT_SLOPE_THRESH_DEG = (3.0, 5.0, 8.0, 12.0, 18.0)


def fishing_spots(elev_m, cellsize_m, param):
    """
    Stepped depth heat-map with magenta highlights wherever the local slope
    rises above a depth-aware threshold. Encodes the rule of thumb that
    fish congregate where the bottom changes character: ledges, drop-offs,
    pinnacles. `param` = depth band size in feet (for the underlying map).
    """
    band_ft = max(1.0, float(param))
    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)
    slope_deg = _slope_degrees(elev_m, cellsize_m)

    spot = np.zeros(elev_m.shape, dtype=bool)
    for lo, hi, thresh in zip(_SPOT_DEPTH_BREAKS_FT[:-1],
                              _SPOT_DEPTH_BREAKS_FT[1:],
                              _SPOT_SLOPE_THRESH_DEG):
        in_band = (depth_ft >= lo) & (depth_ft < hi)
        spot |= in_band & (slope_deg >= thresh)

    # Slight dilation so a single-pixel ridge becomes a visible mark.
    spot = gaussian_filter(spot.astype(np.float32), sigma=1.0,
                           mode='nearest') > 0.3

    idx = _depth_band_index(depth_ft, band_ft, DEPTH_PALETTE.shape[0], land)
    rgba = DEPTH_PALETTE[idx].copy()
    rgba[spot] = SPOT_COLOR
    return Image.fromarray(rgba, mode='RGBA')


# ---------------------------------------------------------------------------
# HSV → RGB (vectorised)
# ---------------------------------------------------------------------------

def _hsv_to_rgb(h, s, v):
    """h,s,v in 0..1 ndarray of any shape. Returns float32 ndarray (...,3)."""
    h = (h * 6.0).astype(np.float32)
    i = np.floor(h).astype(np.int32) % 6
    f = h - np.floor(h)

    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))

    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

ANALYSES = {
    'color-relief':   color_relief,
    'depth':          depth_smooth,
    'depth-bands':    depth_bands,
    'hillshade':      hillshade,
    'slope':          slope,
    'aspect':         aspect,
    'roughness':      roughness,
    'fishing-spots':  fishing_spots,
}
