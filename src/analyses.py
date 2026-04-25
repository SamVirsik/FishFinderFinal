"""
Visualization algorithms for the bathymetric rasters.

Each public function takes a depth grid as a pandas DataFrame plus a `width`
parameter that controls the visual scale of features, and returns a PIL.Image.

NOAA-elevation convention is fixed: positive values are above sea level,
negative values are below. The helpers below depend on that convention being
respected — never on per-tile inspection of the data — so adjacent tiles render
seam-free.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import gaussian_filter

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# Slot 0 reserved for land (semi-transparent so the basemap shows through).
# Slots 1-30 are deepening shades of blue/green for water.
DEPTH_PALETTE = np.array([
    (0,     0,   0,   0),  # land — transparent
    (212, 255,   1, 255), (190, 255,   1, 255),
    (166, 255,  17, 255), (139, 255,  17, 255), (115, 255,  49, 255),
    (83,  255,  84, 255), (53,  255, 114, 255), (53,  255, 147, 255),
    (53,  255, 199, 255), (17,  255, 220, 255), (0,   255, 249, 255),
    (0,   245, 249, 255), (0,   226, 249, 255), (0,   212, 242, 255),
    (0,   198, 242, 255), (0,   182, 242, 255), (0,   167, 242, 255),
    (0,   150, 242, 255), (0,   128, 242, 255), (0,   110, 218, 255),
    (0,    88, 218, 255), (0,    61, 215, 255), (0,    30, 215, 255),
    (0,     0, 207, 255), (0,     0, 187, 255), (0,     0, 163, 255),
    (0,     0, 140, 255), (0,     0, 128, 255), (0,     0, 108, 255),
    (0,     0,  88, 255),
], dtype=np.uint8)

# Same palette but with brown for land, used by colored_hillshade where the
# basemap is occluded.
LAND_DEPTH_PALETTE = DEPTH_PALETTE.copy()
LAND_DEPTH_PALETTE[0] = (139, 69, 19, 255)   # SaddleBrown

SLOPE_PALETTE = np.array([
    (220, 255, 220, 255),  # very gentle – pastel green
    (184, 255, 184, 255),
    (140, 255, 140, 255),
    (100, 255, 100, 255),
    (255, 255, 100, 255),  # mid-range – yellow
    (255, 230,  50, 255),
    (255, 200,   0, 255),
    (255, 160,   0, 255),
    (255, 120,   0, 255),
    (255,  80,   0, 255),
    (255,  40,   0, 255),  # steep – bright red
    (220,   0, 120, 255),
    (160,   0, 180, 255)   # near-vertical – purple
], dtype=np.uint8)

CONTOUR_COLOR = np.array([0, 0, 0, 255], dtype=np.uint8)
HIGHLIGHT_COLOR = np.array([255, 30, 255, 255], dtype=np.uint8)

_FLOW_ANCHORS = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float32)
_FLOW_COLORS = np.array([
    [  0,   0, 128],   # dark navy (lowest flow)
    [  0,   0, 255],   # blue
    [  0, 255, 255],   # cyan
    [255, 255,   0],   # yellow
    [255,   0,   0],   # red   (highest flow)
], dtype=np.float32)

M_TO_FT = 3.28084


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _as_elevation_ft(df, *, metres_to_feet=True):
    """DataFrame -> float32 elevation grid in feet (NOAA sign convention preserved)."""
    z = df.to_numpy(dtype=np.float32, copy=False)
    if metres_to_feet:
        z = z * M_TO_FT
    return z


def _depth_below_sea_ft(elev_ft):
    """Magnitude of depth below sea level in feet, with land clamped to 0."""
    return np.where(elev_ft < 0, -elev_ft, 0.0).astype(np.float32, copy=False)


def _land_mask(elev_ft):
    return elev_ft >= 0


def _depth_palette_index(depth_ft, width, palette_len, land_mask):
    """
    Map depth-magnitude (ft) to a palette index.
    Slot 0 is reserved for land; water occupies slots [1, palette_len-1].
    """
    idx = (depth_ft / width).astype(np.int32, copy=False) + 1
    np.clip(idx, 1, palette_len - 1, out=idx)
    idx[land_mask] = 0
    return idx


def _slope_degrees(elev_ft, cellsize=1.0):
    """Magnitude of the elevation gradient, in degrees (0–90)."""
    dzdy, dzdx = np.gradient(elev_ft, cellsize)
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy)))


def _hillshade_layer(elev_ft, *, cellsize=1.0, azimuth=315.0,
                     altitude=45.0, exaggeration=1.0):
    """
    Compute a 0..1 illumination layer for an underwater hillshade.

    NOAA convention has water at negative elevations, so we negate so deeper
    water reads as "higher" terrain — consistent across tiles regardless of
    whether a tile happens to contain land.
    """
    z = -elev_ft * exaggeration
    dzdy, dzdx = np.gradient(z, cellsize)
    slope = np.hypot(dzdx, dzdy)
    aspect = np.arctan2(dzdy, -dzdx)
    aspect = np.where(aspect < 0, aspect + 2 * np.pi, aspect)

    az = np.deg2rad(azimuth)
    zenith = np.deg2rad(90.0 - altitude)
    hs = (np.cos(zenith) * np.cos(np.arctan(slope)) +
          np.sin(zenith) * np.sin(np.arctan(slope)) * np.cos(az - aspect))
    return np.clip(hs, 0.0, 1.0).astype(np.float32)


def _grey_to_image(grey, rgba=False):
    shade = grey.astype(np.uint8)
    if not rgba:
        return Image.fromarray(shade, mode='L')
    out = np.empty(shade.shape + (4,), dtype=np.uint8)
    out[..., :3] = shade[..., None]
    out[..., 3] = 255
    return Image.fromarray(out, mode='RGBA')


def _flow_to_rgb(norm):
    """Vectorised piece-wise linear mapping of 0..1 values to RGB."""
    idx = np.clip(np.searchsorted(_FLOW_ANCHORS, norm, side='right') - 1, 0, 3)
    left, right = _FLOW_ANCHORS[idx], _FLOW_ANCHORS[idx + 1]
    t = (norm - left) / (right - left)
    rgb = _FLOW_COLORS[idx] + (_FLOW_COLORS[idx + 1] - _FLOW_COLORS[idx]) * t[..., None]
    return rgb.astype(np.uint8)


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def seaborn_heat_map(df, width=1.0):
    """
    Smooth viridis heat-map. `width` scales the displayed depth range:
    width=1 spans -5..1 (metres), width=5 spans -25..5, etc.
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    z = df.to_numpy(dtype=np.float32, copy=False)
    lo, hi = -5.0 * width, 1.0 * width
    norm = np.clip((z - lo) / (hi - lo), 0.0, 1.0)
    norm = np.nan_to_num(norm, nan=0.0, posinf=1.0, neginf=0.0)

    rgb = plt.get_cmap("viridis")(norm)[..., :3]
    return Image.fromarray((rgb * 255).astype(np.uint8))


def granular_heat_map(df, width=1.0, *, metres_to_feet=True):
    """Banded heat-map — one colour step every `width` feet of depth."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    depth = _depth_below_sea_ft(elev)
    idx = _depth_palette_index(depth, width, DEPTH_PALETTE.shape[0], _land_mask(elev))
    return Image.fromarray(DEPTH_PALETTE[idx], mode='RGBA')


def hillshade(df, width=1.0, *, metres_to_feet=True,
              cellsize=1.0, azimuth=315.0, altitude=45.0, rgba=False):
    """Greyscale underwater hillshade. `width` is the vertical-exaggeration knob."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    hs = _hillshade_layer(elev, cellsize=cellsize, azimuth=azimuth,
                          altitude=altitude, exaggeration=width)
    return _grey_to_image(hs * 255.0, rgba=rgba)


def colored_hillshade(df, width=1.0, *, metres_to_feet=True,
                      cellsize=1.0, azimuth=315.0, altitude=45.0,
                      ambient=0.35):
    """Underwater hillshade with depth coloring on top."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    land = _land_mask(elev)

    hs = _hillshade_layer(elev, cellsize=cellsize, azimuth=azimuth,
                          altitude=altitude, exaggeration=width)

    depth = _depth_below_sea_ft(elev)
    idx = _depth_palette_index(depth, width, LAND_DEPTH_PALETTE.shape[0], land)
    rgba = LAND_DEPTH_PALETTE[idx]

    lit = (ambient + hs * (1.0 - ambient)).astype(np.float32)
    out = np.empty_like(rgba)
    out[..., :3] = np.clip(rgba[..., :3].astype(np.float32) * lit[..., None],
                           0, 255).astype(np.uint8)
    out[..., 3] = rgba[..., 3]
    return Image.fromarray(out, mode='RGBA')


def textureshade(df, width=1.0, *, metres_to_feet=True, cellsize=1.0,
                 smooth_sigma=3.0, detail_gain=4.0,
                 clip_range=(0, 40), gamma=0.7, rgba=False):
    """High-pass texture map. Black = smooth seafloor, white = rough."""
    if width <= 0:
        raise ValueError("width must be positive")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    # Texture is sign-invariant (we take |residual|), so we can work directly
    # on elevation without flipping for sign convention.
    background = gaussian_filter(elev, smooth_sigma, mode='nearest')
    resid = (elev - background) * detail_gain * width

    lo, hi = clip_range
    texture = np.clip(np.abs(resid), lo, hi)
    grey = (texture - lo) * (255.0 / (hi - lo))
    if gamma != 1.0:
        grey = 255.0 * (grey / 255.0) ** (1.0 / gamma)
    return _grey_to_image(grey, rgba=rgba)


def flow_exposure(df, width=1.0, *, metres_to_feet=True, cellsize=1.0,
                  clip_range=(0.0, 30.0), gamma=1.0, rgba=False):
    """Bright warm colors mark stronger bottom-current potential (slope-based)."""
    if width <= 0:
        raise ValueError("width must be positive")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    if metres_to_feet:
        cellsize = cellsize * M_TO_FT

    slope_deg = _slope_degrees(elev, cellsize=cellsize)

    lo, hi = clip_range
    flow = np.clip(slope_deg * width, lo, hi)
    norm = (flow - lo) / (hi - lo)
    if gamma != 1.0:
        norm = norm ** (1.0 / gamma)

    rgb = _flow_to_rgb(norm)
    if not rgba:
        return Image.fromarray(rgb, mode='RGB')
    out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255
    return Image.fromarray(out, mode='RGBA')


def contour_map(df, width=1.0, *, metres_to_feet=True, include_diagonal=False):
    """Banded depth heat-map with black contour lines on band boundaries."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    depth = _depth_below_sea_ft(elev)
    idx = _depth_palette_index(depth, width, DEPTH_PALETTE.shape[0], _land_mask(elev))
    rgba = DEPTH_PALETTE[idx].copy()

    mask = np.zeros(idx.shape, dtype=bool)
    mask[:-1, :] |= idx[:-1, :] != idx[1:, :]
    mask[:, :-1] |= idx[:, :-1] != idx[:, 1:]
    if include_diagonal:
        mask[:-1, :-1] |= idx[:-1, :-1] != idx[1:, 1:]
        mask[1:,  :-1] |= idx[1:,  :-1] != idx[:-1, 1:]

    rgba[mask] = CONTOUR_COLOR
    return Image.fromarray(rgba, mode='RGBA')


def slope_magnitude(df, width=5.0, *, metres_to_feet=True, cellsize=1.0,
                    draw_contours=True, include_diagonal=False):
    """Slope-magnitude map (degrees) with optional contour outlines."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    slope_deg = _slope_degrees(elev, cellsize=cellsize)

    idx = (slope_deg / width).astype(np.int32, copy=False)
    np.clip(idx, 0, SLOPE_PALETTE.shape[0] - 1, out=idx)
    rgba = SLOPE_PALETTE[idx].copy()

    if draw_contours:
        mask = np.zeros(idx.shape, dtype=bool)
        mask[:-1, :] |= idx[:-1, :] != idx[1:, :]
        mask[:, :-1] |= idx[:, :-1] != idx[:, 1:]
        if include_diagonal:
            mask[:-1, :-1] |= idx[:-1, :-1] != idx[1:, 1:]
            mask[1:,  :-1] |= idx[1:,  :-1] != idx[:-1, 1:]
        rgba[mask] = CONTOUR_COLOR

    return Image.fromarray(rgba, mode='RGBA')


# Slope (deg) above which a depth band is flagged as "rapid change". Fixed
# values, not per-tile percentiles, so the result is consistent across tiles.
_SPOT_DEPTH_BREAKS_FT = (0.0, 50.0, 200.0, 1000.0, np.inf)
_SPOT_SLOPE_THRESH_DEG = (8.0, 12.0, 18.0, 25.0)


def spot_finder(df, width=1.0, *, metres_to_feet=True, cellsize=1.0,
                blur_px=1.0):
    """
    Heat-map with magenta highlights wherever the local slope exceeds a
    fixed threshold for that depth band.
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    elev = _as_elevation_ft(df, metres_to_feet=metres_to_feet)
    depth = _depth_below_sea_ft(elev)
    slope_deg = _slope_degrees(elev, cellsize=cellsize)

    high_mask = np.zeros(elev.shape, dtype=bool)
    for lo, hi, thresh in zip(_SPOT_DEPTH_BREAKS_FT[:-1],
                              _SPOT_DEPTH_BREAKS_FT[1:],
                              _SPOT_SLOPE_THRESH_DEG):
        band = (depth >= lo) & (depth < hi)
        high_mask |= band & (slope_deg >= thresh)

    if blur_px > 0:
        high_mask = gaussian_filter(high_mask.astype(np.uint8),
                                    sigma=blur_px, mode='nearest') > 0

    idx = _depth_palette_index(depth, width, DEPTH_PALETTE.shape[0], _land_mask(elev))
    rgba = DEPTH_PALETTE[idx].copy()
    rgba[high_mask] = HIGHLIGHT_COLOR
    return Image.fromarray(rgba, mode='RGBA')


# Public dispatch consumed by LayerGenerator.
ANALYSES = {
    'heatmap':           seaborn_heat_map,
    'heatmap-granular':  granular_heat_map,
    'contour':           contour_map,
    'hillshade':         hillshade,
    'colored-hillshade': colored_hillshade,
    'texture-shade':     textureshade,
    'flow-exposure':     flow_exposure,
    'slope-magnitude':   slope_magnitude,
    'spot-finder':       spot_finder,
}
