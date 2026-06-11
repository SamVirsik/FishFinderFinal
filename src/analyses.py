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

The set is intentionally small (5 analyses), each visually and analytically
distinct from the others. These mirror static/analyses.js 1:1 — keep them
visually aligned if you change either side.
"""

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import gaussian_filter

M_TO_FT = 3.28084

# ---------------------------------------------------------------------------
# Tuning constants (mirror static/analyses.js)
# ---------------------------------------------------------------------------

# texture-relief
TEXTURE_VERT_EXAG = 5      # fixed hillshade exaggeration
TEXTURE_K         = 0.6    # detail-modulation strength
# structure (curvature)
STRUCT_VERT_EXAG     = 5      # base hillshade exaggeration
STRUCT_CURV_AMP      = 2.0    # m — curvature that saturates the ramp
STRUCT_BASE_CONTRAST = 0.7    # base hillshade contrast under colour
STRUCT_ALPHA         = 0.85   # max curvature-colour opacity
STRUCT_CONCAVE = np.array([40, 110, 215], dtype=np.float32)  # blue (L>0)
STRUCT_CONVEX  = np.array([210, 70, 50],  dtype=np.float32)  # red  (L<0)
# spot-score
SPOT_FEATURE_M     = 40     # fixed roughness feature scale (m)
SPOT_TOL_FT        = 12     # depth tolerance around target (ft)
SPOT_R_WEIGHT      = 0.6    # roughness weight in score
SPOT_S_WEIGHT      = 0.4    # slope weight in score
SPOT_SLOPE_MAX_DEG = 45     # slope that maps to S=1
# depth-contours
CONTOUR_FALLBACK_MAXFT = 300                               # tile with no water
CONTOUR_LINE = np.array([20, 30, 40], dtype=np.float32)    # dark line colour
CONTOUR_MIX  = 0.7                                         # blend toward line


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


def _smoothstep(e0, e1, x):
    """GLSL-style smoothstep applied element-wise to an array."""
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


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

def color_relief(elev_m, cellsize_m, param,
                 min_depth_ft=0.0, max_depth_ft=140.0):
    """
    Best general-purpose view: depth-coloured terrain with a hillshade
    overlay. `param` = vertical exaggeration (1–50, default ~5).

    `min_depth_ft` / `max_depth_ft` rescale the colour mapping so the
    palette spans a user-chosen depth window. Depths shallower than
    `min_depth_ft` clamp to the shallowest colour; depths deeper than
    `max_depth_ft` clamp to the deepest. The defaults match the original
    fixed-range behaviour (4 ft per band, saturating near ~140 ft).
    """
    exaggeration = max(1.0, float(param))
    hs = _hillshade(elev_m, cellsize_m, exaggeration=exaggeration)

    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)

    palette_len = LAND_DEPTH_PALETTE.shape[0]
    usable = palette_len - 1   # water-depth slots (1..usable)
    min_ft = float(min_depth_ft)
    max_ft = max(min_ft + 1e-3, float(max_depth_ft))
    t = np.clip((depth_ft - min_ft) / (max_ft - min_ft), 0.0, 1.0)
    idx = 1 + np.minimum(usable - 1, (t * usable).astype(np.int32))
    idx[land] = 0
    rgba = LAND_DEPTH_PALETTE[idx]

    ambient = 0.35
    lit = (ambient + hs * (1.0 - ambient))[..., None]
    out = np.empty_like(rgba)
    out[..., :3] = np.clip(rgba[..., :3].astype(np.float32) * lit,
                           0, 255).astype(np.uint8)
    out[..., 3] = rgba[..., 3]
    return Image.fromarray(out, mode='RGBA')


def texture_relief(elev_m, cellsize_m, param):
    """
    Detail-enhanced shaded relief. A hillshade carries the macro form; a
    high-pass residual (depth minus its blurred self at the chosen feature
    scale) modulates luminance so wrecks, ledges and rubble pop. Warm-grey
    tint. `param` = feature scale in METRES. Replaces hillshade + roughness.
    """
    feature_scale = max(5.0, float(param))
    sigma = max(0.5, feature_scale / cellsize_m)
    bg = gaussian_filter(elev_m, sigma, mode='nearest')
    hs = _hillshade(elev_m, cellsize_m, exaggeration=TEXTURE_VERT_EXAG)

    dnorm = np.clip((elev_m - bg) / max(0.05 * feature_scale, 1e-3), -1.0, 1.0)
    lum = np.clip(hs * (1 + TEXTURE_K * dnorm), 0.0, 1.0) * 255.0
    rgb = np.stack([lum, lum * 0.97, lum * 0.92], axis=-1)
    return _to_rgba(np.clip(rgb, 0, 255).astype(np.uint8))


def structure(elev_m, cellsize_m, param):
    """
    Diverging concave/convex (curvature) map over a faint hillshade. Blue =
    concave (holes, channels), red = convex (humps, ledges). Flat ground
    stays near-transparent so the relief reads through. `param` = feature
    scale in METRES (denoise pre-blur). Replaces slope.
    """
    feature_scale = max(5.0, float(param))
    sigma = max(0.5, feature_scale / cellsize_m)
    zb = gaussian_filter(elev_m, sigma, mode='nearest')
    hs = _hillshade(elev_m, cellsize_m, exaggeration=STRUCT_VERT_EXAG)

    # Laplacian of elevation (+up) with replicated edges. Concave → positive.
    up = np.empty_like(zb); up[1:, :] = zb[:-1, :]; up[0, :] = zb[0, :]
    dn = np.empty_like(zb); dn[:-1, :] = zb[1:, :]; dn[-1, :] = zb[-1, :]
    lf = np.empty_like(zb); lf[:, 1:] = zb[:, :-1]; lf[:, 0] = zb[:, 0]
    rt = np.empty_like(zb); rt[:, :-1] = zb[:, 1:]; rt[:, -1] = zb[:, -1]
    lap = (up + dn + lf + rt - 4 * zb) / (cellsize_m * cellsize_m)

    t = np.clip(lap * feature_scale * feature_scale / STRUCT_CURV_AMP, -1.0, 1.0)
    base = (hs * STRUCT_BASE_CONTRAST * 255.0)[..., None]
    a = (np.abs(t) * STRUCT_ALPHA)[..., None]
    col = np.where((t >= 0)[..., None], STRUCT_CONCAVE, STRUCT_CONVEX)
    rgb = col * a + base * (1 - a)
    return _to_rgba(np.clip(rgb, 0, 255).astype(np.uint8))


def spot_score(elev_m, cellsize_m, param):
    """
    Headline fusion. Roughness + slope, gated by a Gaussian preference for a
    reachable target depth, painted in inferno. Low scores fade out so the
    basemap reads through; bright yellow = best structure at the depth you
    want. `param` = target depth in FEET.
    """
    target = max(10.0, float(param))
    sigma = max(0.5, SPOT_FEATURE_M / cellsize_m)
    bg = gaussian_filter(elev_m, sigma, mode='nearest')
    deg = _slope_degrees(elev_m, cellsize_m)
    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)

    rough = np.clip(np.abs(elev_m - bg) / max(0.05 * SPOT_FEATURE_M, 1e-3), 0.0, 1.0)
    s = np.clip(deg / SPOT_SLOPE_MAX_DEG, 0.0, 1.0)
    pref = np.exp(-(((depth_ft - target) / SPOT_TOL_FT) ** 2))
    score = np.clip((SPOT_R_WEIGHT * rough + SPOT_S_WEIGHT * s) * pref, 0.0, 1.0)
    score[land] = 0.0

    rgb = (plt.get_cmap('inferno')(score)[..., :3] * 255).astype(np.uint8)
    alpha = (_smoothstep(0.2, 0.5, score) * 255).astype(np.uint8)

    out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = np.where(land, 0, alpha).astype(np.uint8)
    return Image.fromarray(out, mode='RGBA')


def depth_contours(elev_m, cellsize_m, param):
    """
    Calm chart view. Smooth viridis depth fill (auto-scaled to the deepest
    water in the tile) with dark contour lines wherever the contour band
    changes. `param` = contour interval in FEET. Merges depth + depth-bands.
    """
    interval = max(1.0, float(param))
    depth_ft = _depth_below_sea_ft(elev_m)
    land = _land_mask(elev_m)

    max_depth = float(depth_ft.max()) if depth_ft.size else 0.0
    if max_depth < 1e-3:
        max_depth = CONTOUR_FALLBACK_MAXFT

    norm = np.clip(depth_ft / max_depth, 0.0, 1.0)
    rgb = (plt.get_cmap('viridis')(norm)[..., :3] * 255).astype(np.float32)

    # Contour band per pixel (land = -1 so the shoreline reads as an edge).
    band = np.where(land, -1, np.floor(depth_ft / interval)).astype(np.int32)
    edge = np.zeros(band.shape, dtype=bool)
    edge[:, :-1] |= band[:, :-1] != band[:, 1:]
    edge[:-1, :] |= band[:-1, :] != band[1:, :]
    rgb[edge] = rgb[edge] * (1 - CONTOUR_MIX) + CONTOUR_LINE * CONTOUR_MIX

    out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[..., 3] = np.where(land, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode='RGBA')


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

ANALYSES = {
    'color-relief':    color_relief,
    'texture-relief':  texture_relief,
    'structure':       structure,
    'spot-score':      spot_score,
    'depth-contours':  depth_contours,
}
