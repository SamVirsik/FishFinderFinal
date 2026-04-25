import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from PIL import Image
import seaborn as sns
import matplotlib.pyplot as plt
from scipy.signal import convolve2d
from scipy.ndimage import gaussian_filter
from collections.abc import Iterable
#import io

_PALETTE = np.array([
    (0,   0,   0,  50),  (212, 255,   1, 255), (190, 255,   1, 255),
    (166, 255,  17, 255), (139, 255,  17, 255), (115, 255,  49, 255),
    (83,  255,  84, 255), (53,  255, 114, 255), (53,  255, 147, 255),
    (53,  255, 199, 255), (17,  255, 220, 255), (0,   255, 249, 255),
    (0,   245, 249, 255), (0,   226, 249, 255), (0,   212, 242, 255),
    (0,   198, 242, 255), (0,   182, 242, 255), (0,   167, 242, 255),
    (0,   150, 242, 255), (0,   128, 242, 255), (0,   110, 218, 255),
    (0,    88, 218, 255), (0,    61, 215, 255), (0,    30, 215, 255),
    (0,     0, 207, 255), (0,     0, 187, 255), (0,     0, 163, 255),
    (0,     0, 140, 255), (0,     0, 128, 255), (0,     0, 108, 255),
    (0,     0,  88, 255)
], dtype=np.uint8)

NEW_PALETTE = np.array([
    (139,  69,  19, 255),   # SaddleBrown for land
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
    (0,     0,  88, 255)
], dtype=np.uint8)

_SLOPE_PALETTE = np.array([
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

_CONTOUR_COLOUR = np.array([0, 0, 0, 255], dtype=np.uint8)   # black lines

_HIGHLIGHT = np.array([255,  30, 255, 255], dtype=np.uint8)   # magenta

_ANCHORS = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float32)
_COLORS  = np.array([
    [  0,   0, 128],   # dark navy (lowest flow)
    [  0,   0, 255],   # blue
    [  0, 255, 255],   # cyan
    [255, 255,   0],   # yellow
    [255,   0,   0],   # red   (highest flow)
], dtype=np.float32)

'''
def seaborn_heat_map(df):
    """
    Heatmap created using seaborn heatmapping. 
    To change the scale you simply need to change the 'min_value' parameter. 
    The more negative it is the greater the range. 

    Other colors scales of heat maps are also possible, changing the viridis parameter. 
    """
    min_value, max_value = -5, 1

    clipped_data = df.clip(lower=min_value, upper=max_value)
    normalized_data = (clipped_data - min_value) / (max_value - min_value)

    normalized_data = normalized_data.fillna(0).replace([np.inf, -np.inf], 0)

    colormap = plt.get_cmap("viridis")
    colored_data = colormap(normalized_data.values)[:, :, :3]  # Remove alpha channel

    image = Image.fromarray((colored_data * 255).astype(np.uint8))

    return image
'''
def _scalar_to_rgb(norm):
    """
    Vectorised piece-wise linear mapping of 0-1 values to RGB based on _COLORS.
    """
    idx = np.clip(np.searchsorted(_ANCHORS, norm, side='right') - 1, 0, 3)
    left, right = _ANCHORS[idx], _ANCHORS[idx + 1]
    t = (norm - left) / (right - left)
    rgb = _COLORS[idx] + ( _COLORS[idx + 1] - _COLORS[idx]) * t[..., None]
    return rgb.astype(np.uint8)

def seaborn_heat_map(df, width=1):
    """
    Heatmap created using seaborn-like color scaling.
    
    The 'width' parameter scales the range of depth values. 
    For example, width=1 gives a range from -5 to 1 (default),
    width=5 gives a range from -25 to 5, etc.

    Other color maps (e.g., 'plasma', 'inferno') can be used 
    by changing the 'viridis' parameter.
    """
    min_value = -5 * width
    max_value = 1 * width

    clipped_data = df.clip(lower=min_value, upper=max_value)
    normalized_data = (clipped_data - min_value) / (max_value - min_value)
    normalized_data = normalized_data.fillna(0).replace([np.inf, -np.inf], 0)

    colormap = plt.get_cmap("viridis")
    colored_data = colormap(normalized_data.values)[:, :, :3]  # Remove alpha channel

    image = Image.fromarray((colored_data * 255).astype(np.uint8))
    return image

def granular_heat_map(df, width=1.0, *, metres_to_feet=True):
    """Render a DataFrame of depths to an RGBA heat-map."""
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    # 1. Numeric view
    depth = df.to_numpy(dtype=np.float32, copy=False)

    # 2. Optional m → ft
    if metres_to_feet:
        depth *= 3.28084

    # 3. Positive depth magnitude (land = 0)
    depth_mag = np.where(depth < 0, -depth, 0.0).astype(np.float32, copy=False)

    # 4. Palette index (one colour step every `width` feet)
    idx = (depth_mag / width).astype(np.int16, copy=False)
    np.clip(idx, 0, _PALETTE.shape[0] - 1, out=idx)

    # 5. Colour look-up and Pillow image
    rgba = _PALETTE[idx]                     # shape (rows, cols, 4)
    return Image.fromarray(rgba, mode='RGBA')

def hillshade(df, width=1.0, *, metres_to_feet=True,
                      cellsize=1.0, azimuth=315.0, altitude=45.0,
                      rgba=False):
    """
    Seam-free, all-NumPy hill-shade suitable for tiled rendering.
    Parameters are unchanged; `width` is still your vertical-exaggeration knob.
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    # 1. numeric view ---------------------------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)

    if np.nanmin(z) < 0:          # crude check: values are negative
        z = -z                    # invert so –20 m becomes +20 “metres up”

    if metres_to_feet:
        z *= 3.28084
    z *= width                                           # exaggeration

    # 2. first-order gradient (keeps full array size) -------------------
    dzdy, dzdx = np.gradient(z, cellsize)                # row, col order

    # 3. slope & aspect -----------------------------------------------
    slope  = np.hypot(dzdx, dzdy)
    aspect = np.arctan2(dzdy, -dzdx)
    aspect[aspect < 0] += 2*np.pi

    # 4. illumination --------------------------------------------------
    az     = np.deg2rad(azimuth)
    zenith = np.deg2rad(90.0 - altitude)

    hs = ( np.cos(zenith)*np.cos(np.arctan(slope)) +
           np.sin(zenith)*np.sin(np.arctan(slope))*np.cos(az - aspect) )

    shade = (hs.clip(0) * 255).astype(np.uint8)          # 0-255 greys

    # 5. Pillow image ---------------------------------------------------
    if rgba:
        out = np.empty(shade.shape + (4,), dtype=np.uint8)
        out[..., :3] = shade[..., None]
        out[..., 3]  = 255
        return Image.fromarray(out, mode='RGBA')
    else:
        return Image.fromarray(shade, mode='L')
    
def textureshade(df, width=1.0, *,          # 1 – 20 works well
                 metres_to_feet=True,
                 cellsize=1.0,
                 smooth_sigma=3.0,
                 detail_gain=4.0,
                 clip_range=(0, 40),
                 gamma=0.7,
                 rgba=False):
    """
    Texture-shade: black = smooth seafloor, white = rough.
    `width` (positive) scales the visual contrast. 2× width ≈ 2× brighter
    whites / deeper blacks, without breaking tile-to-tile consistency.
    """
    if width <= 0:
        raise ValueError("width must be positive")

    # 1. numeric view ---------------------------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)
    if np.nanmin(z) < 0:      # NOAA convention: depths negative
        z = -z
    if metres_to_feet:
        z *= 3.28084

    # 2. background + texture ------------------------------------------
    background = gaussian_filter(z, smooth_sigma, mode='nearest')

    # *** ONE new factor: multiply residual by `width` ******************
    resid = (z - background) * detail_gain * width      # ← here

    # 3. magnitude map --------------------------------------------------
    texture = np.abs(resid)
    lo, hi = clip_range
    texture = np.clip(texture, lo, hi)

    grey = (texture - lo) * (255.0 / (hi - lo))         # linear
    if gamma != 1.0:
        grey = 255.0 * (grey / 255.0) ** (1.0 / gamma)  # γ-curve

    shade = grey.astype(np.uint8)

    # 4. Pillow image ---------------------------------------------------
    if rgba:
        out = np.empty(shade.shape + (4,), dtype=np.uint8)
        out[..., :3] = shade[..., None]
        out[..., 3]  = 255
        return Image.fromarray(out, mode='RGBA')
    else:
        return Image.fromarray(shade, mode='L')

def colored_hillshade(df, width=1.0, *, metres_to_feet=True,
                      cellsize=1.0, azimuth=315.0, altitude=45.0,
                      ambient=0.35):
    """
    Hillshade with subtle depth colouring.
    - Uses the same gradient/illumination math as `hillshade`.
    - Uses the same palette indexing logic as `granular_heat_map`.
    - `ambient` (0–1) controls base light so colours don't go fully black.
    Returns a Pillow RGBA image.
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    # 1) Numeric views ---------------------------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)         # elevations/depths
    depth = z.copy()                                      # keep original sign for palette

    # If depths are negative, flip for hillshade "up" direction (matches `hillshade`)
    if np.nanmin(z) < 0:
        z = -z

    if metres_to_feet:
        z     *= 3.28084
        depth *= 3.28084

    # Vertical exaggeration (matches `hillshade`)
    z *= width

    # 2) First-order gradient (keeps full size) --------------------------
    dzdy, dzdx = np.gradient(z, cellsize)                 # row, col order

    # 3) Slope & aspect --------------------------------------------------
    slope  = np.hypot(dzdx, dzdy)
    aspect = np.arctan2(dzdy, -dzdx)
    aspect[aspect < 0] += 2*np.pi

    # 4) Illumination (0..1) ---------------------------------------------
    az     = np.deg2rad(azimuth)
    zenith = np.deg2rad(90.0 - altitude)

    hs = ( np.cos(zenith)*np.cos(np.arctan(slope)) +
           np.sin(zenith)*np.sin(np.arctan(slope))*np.cos(az - aspect) )
    shade = np.clip(hs, 0.0, 1.0).astype(np.float32)      # 0..1

    # 5) Depth → palette RGBA (matches `granular_heat_map`) --------------
    # 5) Depth → palette RGBA
    depth_mag = np.where(depth < 0, -depth, 0.0).astype(np.float32, copy=False)

    idx = (depth_mag / width).astype(np.int16, copy=False) + 1  # +1 to leave slot 0 = land
    np.clip(idx, 1, _PALETTE.shape[0] - 1, out=idx)

    # Explicitly set land to 0 (brown)
    idx[depth >= 0] = 0

    rgba = NEW_PALETTE[idx].astype(np.uint8)             # (H, W, 4)

    # 6) Apply shading to colour (preserve alpha) ------------------------
    # Simple Lambertian modulation with an ambient floor so colours stay visible.
    # out_rgb = base_rgb * (ambient + shade*(1 - ambient))
    lit = (ambient + shade*(1.0 - ambient)).astype(np.float32)  # (H, W)
    out = np.empty_like(rgba)
    out[..., :3] = np.clip(rgba[..., :3].astype(np.float32) * lit[..., None], 0, 255).astype(np.uint8)
    out[..., 3]  = rgba[..., 3]  # keep palette alpha (or 255 if your palette is opaque)

    return Image.fromarray(out, mode='RGBA')


def flow_exposure(df, width=1.0, *,              # 1–20 ≈ sensible gain range
                  metres_to_feet=True,
                  cellsize=1.0,                  # physical pixel size (m)
                  clip_range=(0.0, 30.0),        # slope-deg window for color map
                  gamma=1.0,                     # optional γ on normalised flow
                  rgba=False):
    """
    Flow-Exposure Model: bright warm colors = stronger bottom current potential.

    Logic (absolute, tile-safe):
    1. Convert depths & cell size to consistent units (feet if requested).
    2. Compute 3-D slope angle: arctan(√(dz/dx² + dz/dy²)).
       -> purely local; no global stats used.
    3. Multiply by `width` (user gain), clip to fixed `clip_range`.
    4. Map the normalised value through a hard-coded 5-stop heat palette.
    """
    if width <= 0:
        raise ValueError("width must be positive")

    # --- depth array ---------------------------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)
    if np.nanmin(z) < 0:                # NOAA negative-depth convention
        z = -z
    if metres_to_feet:
        z        *= 3.28084
        cellsize *= 3.28084             # keep units consistent

    # --- slope (°) ------------------------------------------------------
    dzdy, dzdx = np.gradient(z, cellsize)   # central diff w/ phys. spacing
    slope_rad  = np.arctan( np.hypot(dzdx, dzdy) )   # rise/run → radians
    slope_deg  = np.degrees(slope_rad)

    # --- exposure metric -----------------------------------------------
    flow = slope_deg * width
    lo, hi = clip_range
    flow   = np.clip(flow, lo, hi)
    norm   = (flow - lo) / (hi - lo)          # 0–1 invariant scale
    if gamma != 1.0:
        norm = norm ** (1.0 / gamma)

    # --- colourise ------------------------------------------------------
    rgb = _scalar_to_rgb(norm)

    # --- Pillow image ---------------------------------------------------
    if rgba:
        out = np.empty(rgb.shape[:-1] + (4,), dtype=np.uint8)
        out[..., :3] = rgb
        out[..., 3]  = 255
        return Image.fromarray(out, mode='RGBA')
    else:
        return Image.fromarray(rgb, mode='RGB')

def contour_map(df, width=1.0, *, metres_to_feet=True,
                        include_diagonal=False):
    """
    Depth heat-map + contour lines along band boundaries.

    Parameters
    ----------
    df : pandas.DataFrame
        2-D grid of depths (negative = water, ≥0 = land).
    width : float > 0
        Size of one colour band in the same units as `df`.
    metres_to_feet : bool
        If True, convert metres → feet before colouring.
    include_diagonal : bool
        If True, draw contours on diagonal band changes as well.

    Returns
    -------
    PIL.Image.Image ('RGBA')
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    # 1. Raw numeric view
    depth = df.to_numpy(dtype=np.float32, copy=False)

    # 2. Optional m → ft conversion
    if metres_to_feet:
        depth *= 3.28084

    # 3. Depth magnitude (land = 0)
    depth_mag = np.where(depth < 0, -depth, 0.0).astype(np.float32, copy=False)

    # 4. Palette index (one step every `width`)
    idx = (depth_mag / width).astype(np.int16, copy=False)
    np.clip(idx, 0, _PALETTE.shape[0] - 1, out=idx)

    # 5. Base RGBA image
    rgba = _PALETTE[idx].copy()        # copy so we can overwrite contours

    # 6. Contour mask: any cell differing from a neighbour
    mask = np.zeros(idx.shape, dtype=bool)
    mask[:-1, :] |= idx[:-1, :] != idx[1:, :]      # vertical boundaries
    mask[:, :-1] |= idx[:, :-1] != idx[:, 1:]      # horizontal boundaries
    if include_diagonal:                           # optional diagonals
        mask[:-1, :-1] |= idx[:-1, :-1] != idx[1:, 1:]
        mask[1:, :-1]  |= idx[1:, :-1]  != idx[:-1, 1:]

    # 7. Overlay contour colour (single broadcast write)
    rgba[mask] = _CONTOUR_COLOUR

    return Image.fromarray(rgba, mode='RGBA')

def slope_magnitude(df, width=5.0, *, metres_to_feet=True,
                      cellsize=1.0, draw_contours=True,
                      include_diagonal=False):
    """
    Raster slope-magnitude map (degrees) with optional contour outlines.

    Parameters
    ----------
    df : pandas.DataFrame
        2-D grid of elevations/depths.  Units may be m or ft.
    width : float > 0
        Degree interval represented by ONE colour band
        (default 5° ➝ 0-5°, 5-10°, …).
    metres_to_feet : bool
        If True, convert metres → feet (× 3.28084) before gradient.
        (Slope ratio is unit-less, so this only matters for consistency.)
    cellsize : float
        Horizontal grid spacing in the *same* units as `df`.
    draw_contours : bool
        Overlay 1-pixel black outlines at band boundaries.
    include_diagonal : bool
        Detect diagonal band changes as well (slightly denser outlines).

    Returns
    -------
    PIL.Image.Image   ('RGBA')
    """
    if width <= 0:
        raise ValueError("width must be positive and non-zero")

    # 1. numeric view ----------------------------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)
    if metres_to_feet:
        z *= 3.28084          # optional m → ft

    # 2. gradient (keeps full size → no tile seams) ----------------------
    dzdy, dzdx = np.gradient(z, cellsize)

    # 3. slope in degrees -----------------------------------------------
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))  # 0-90°

    # 4. palette index ---------------------------------------------------
    idx = (slope_deg / width).astype(np.int16, copy=False)
    np.clip(idx, 0, _SLOPE_PALETTE.shape[0] - 1, out=idx)

    # 5. base RGBA image -------------------------------------------------
    rgba = _SLOPE_PALETTE[idx].copy()

    # 6. optional contour outlines --------------------------------------
    if draw_contours:
        mask = np.zeros(idx.shape, dtype=bool)
        mask[:-1, :]  |= idx[:-1, :]  != idx[1:, :]      # N-S edges
        mask[:, :-1]  |= idx[:, :-1]  != idx[:, 1:]      # E-W edges
        if include_diagonal:
            mask[:-1, :-1] |= idx[:-1, :-1] != idx[1:, 1:]
            mask[1:, :-1]  |= idx[1:, :-1]  != idx[:-1, 1:]
        rgba[mask] = _CONTOUR_COLOUR

    return Image.fromarray(rgba, mode='RGBA')

def spot_finder(df, width=1.0, *, metres_to_feet=True,
                             cellsize=1.0,
                             depth_breaks=(0, 50, 200, 1000, np.inf),
                             percentile=95,
                             blur_px=1.0):
    """
    Depth-aware rapid-terrain-change highlighter.

    1.  Compute slope magnitude at every cell (|∇z|, degrees).
    2.  Bucket all pixels into depth bands (depth_breaks).
    3.  Within *each* band, mark pixels whose slope exceeds the given
        percentile (default top 5 %) – a global test, not local.
    4.  Optional 1-px Gaussian blur on the mask for smoother blobs.
    5.  Overlay magenta highlights on your normal blue-green heat-map.

    Parameters
    ----------
    df : pandas.DataFrame     2-D grid of depths (negative underwater).
    width : float             Depth interval per palette band (same as before).
    metres_to_feet : bool     Convert metres → feet before thresholds.
    cellsize : float          Grid spacing (same units as df, default 1).
    depth_breaks : tuple      Boundaries of depth buckets (ft after conversion).
                              Example (0,50,200,1000,inf) → 0-50, 50-200, …
    percentile : int|float    Slope percentile that defines “rapid change”.
                              95 ⇒ highlight steepest 5 % *within each depth bin*.
    blur_px : float           Gaussian σ to dilate the mask (0 → none).

    Returns
    -------
    PIL.Image ('RGBA')        Heat-map with magenta rapid-change patches.
    """
    # --------------  1. numeric view  -----------------------------------
    z = df.to_numpy(dtype=np.float32, copy=False)
    if metres_to_feet:
        z *= 3.28084

    # --------------  2. slope magnitude in degrees  --------------------
    dzdy, dzdx = np.gradient(z, cellsize)
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))   # 0-90°

    # --------------  3. per-depth-band thresholding  -------------------
    high_mask = np.zeros_like(slope_deg, dtype=bool)

    abs_depth = np.maximum(-z, 0.0)      # feet below datum

    for lo, hi in zip(depth_breaks[:-1], depth_breaks[1:]):
        band = (abs_depth >= lo) & (abs_depth < hi)
        if np.any(band):
            thresh = np.percentile(slope_deg[band], percentile)
            high_mask |= band & (slope_deg >= thresh)

    # --------------  4. optional blur / dilation  ----------------------
    if blur_px > 0:
        high_mask = gaussian_filter(high_mask.astype(np.uint8),
                                    sigma=blur_px, mode='nearest') > 0

    # --------------  5. base heat-map  ---------------------------------
    idx = (abs_depth / width).astype(np.int16, copy=False)
    np.clip(idx, 0, _PALETTE.shape[0]-1, out=idx)
    rgba = _PALETTE[idx].copy()

    # --------------  6. overlay highlights  ----------------------------
    rgba[high_mask] = _HIGHLIGHT
    return Image.fromarray(rgba, mode='RGBA')


def depth_change_frequency(df):
    '''
    Makes a grayscale image for which the intensity of gray is based on 
    the number of surrounding pixels for which there was a downward 
    depth decrease. 
    '''
    from PIL import Image
    import numpy as np

    num_rows, num_cols = df.shape

    # Precompute downward changes
    blank = np.zeros_like(df.values, dtype=np.uint8)
    blank[1:] = (df.values[1:] < df.values[:-1]).astype(np.uint8)

    # Compute 3x3 neighborhood sums using convolution
    from scipy.ndimage import convolve
    kernel = np.ones((3, 3), dtype=np.uint8)
    neighborhood_sums = convolve(blank, kernel, mode='constant', cval=0)

    # Prepare color mapping
    land_color = (246, 185, 33) 

    # Create a blank image
    image_array = np.zeros((num_rows, num_cols, 3), dtype=np.uint8)

    # Assign colors based on conditions
    land_mask = (df.values >= 0)
    grayscale_values = (255 * (neighborhood_sums / 8)).astype(np.uint8)

    # Apply colors
    image_array[land_mask] = land_color
    image_array[~land_mask] = np.stack([grayscale_values, grayscale_values, grayscale_values], axis=-1)[~land_mask]

    # Convert the array to an image
    image = Image.fromarray(image_array, 'RGB')
    return image




def depth_change(df):
    '''
    Plots black as downward depth increase, and white as the same or decrease. 
    For texture. 
    '''
    from PIL import Image
    import numpy as np

    num_rows, num_cols = df.shape

    # Precompute values for efficiency
    blank = np.zeros_like(df.values, dtype=np.uint8)
    blank[1:] = (df.values[1:] < df.values[:-1]).astype(np.uint8)

    # Prepare color mapping
    land_color = (246, 185, 33)  
    depth_decrease_color = (255, 255, 255)  # White
    depth_increase_color = (0, 0, 0)  # Black

    # Create a blank image
    image_array = np.zeros((num_rows, num_cols, 3), dtype=np.uint8)

    # Assign colors based on conditions
    land_mask = (df.values >= 0)
    depth_decrease_mask = (blank == 1) & ~land_mask

    image_array[land_mask] = land_color
    image_array[depth_decrease_mask] = depth_decrease_color
    image_array[~land_mask & ~depth_decrease_mask] = depth_increase_color

    # Convert the array to an image
    image = Image.fromarray(image_array, 'RGB')
    return image


def boating_map(df, width): 
    number_rows, number_columns = df.shape
    image = Image.new('RGB', (number_columns, number_rows), color = 'white')
    pixels = image.load()
    
    colorList = [(139, 69, 19), (212, 255, 1), (190, 255, 1), (166, 255, 17), (139, 255, 17), 
                 (115, 255, 49), (83, 255, 84), (53, 255, 114), (53, 255, 147), (53, 255, 199), 
                 (17, 255, 220), (0, 255, 249), (0, 245, 249), (0, 226, 249), (0, 212, 242), 
                 (0, 198, 242), (0, 182, 242), (0, 167, 242), (0, 150, 242), (0, 128, 242), 
                 (0, 110, 218), (0, 88, 218), (0, 61, 215), (0, 30, 215), (0, 0, 207), (0, 0, 187), 
                 (0, 0, 163), (0, 0, 140), (0, 0, 128), (0, 0, 108), (0, 0, 88)]

    for row_index, row_data in df.iterrows():
        for col_name in df.columns:
            mValue = row_data[col_name]
            fValue = mValue*3.28084 #foot value

            scaled = ((abs(fValue)) - fValue) / 2

            if scaled == 0.0:
                pixels[col_name, row_index] = colorList[0]
            else:
                scaled = int(scaled/width) + 1
            
            if (scaled > len(colorList)-2):
                pixels[col_name, row_index] = colorList[len(colorList)-1]
            else:
                pixels[col_name, row_index] = colorList[int(scaled)]


    
    return image

def inshore_navigation(df):
    # Define the RGB color mapping for depth ranges
    color_mapping = np.array([
        ((0, float('inf')), (246, 185, 33)),      # 0+ depth
        ((-1, 0), (124, 153, 100)),              # 0 to -1
        ((-2, -1), (180, 175, 132)),             # -1 to -2
        ((-6, -2), (224, 206, 192)),             # -2 to -6
        ((-20, -6), (192, 198, 196)),            # -6 to -20
        ((-30, -20), (202, 207, 208)),           # -20 to -30
        ((-50, -30), (138, 177, 189)),           # -30 to -50
        ((float('-inf'), -50), (255, 255, 255))  # Below -50
    ], dtype=object)
    
    # Convert DataFrame to a NumPy array in feet
    df_array = df.to_numpy() * 3.28084  # Convert meters to feet
    
    # Create an empty array for storing RGB values
    number_rows, number_columns = df_array.shape
    rgb_array = np.zeros((number_rows, number_columns, 3), dtype=np.uint8)
    
    # Apply color mapping based on depth ranges
    for depth_range, color in color_mapping:
        mask = (df_array >= depth_range[0]) & (df_array < depth_range[1])
        rgb_array[mask] = color  # Assign the RGB color for the matching depth range
    
    # Create an image from the RGB array
    image = Image.fromarray(rgb_array, 'RGB')
    
    return image


#NOT EVEN CLOSE TO CORRECT!!!!!!
def contour_map_type1_optimized(df, roll, width):
    num_rows, num_cols = df.shape
    df_array = df.to_numpy()

    # Apply rolling average only if roll > 1
    if roll > 1:
        kernel = np.ones((roll, roll)) / (roll**2)
        df_array = convolve2d(np.pad(df_array, roll//2, mode='edge'), kernel, mode='valid')

    # Convert depths from meters to feet
    df_array *= -3.28084

    # Initialize output image
    pixels = np.full((num_rows, num_cols, 3), (255, 255, 255), dtype='uint8')

    # Define contour bounds
    lower_bounds, upper_bounds = np.arange(5, 500, 5), np.arange(10, 505, 5)

    for lower, upper in zip(lower_bounds, upper_bounds):
        within_range = (df_array >= lower) & (df_array < upper)
        surrounding_outside = (
            within_range &
            ~np.pad(within_range, 1, mode='constant')[1:-1, 1:-1]
        )
        pixels[surrounding_outside] = (0, 0, 0)  # Black for contour lines

    # Heatmap for non-contour points
    non_contour = (pixels[..., 0] == 255) & (pixels[..., 1] == 255) & (pixels[..., 2] == 255)
    if np.any(non_contour):  # Skip if no non-contour points exist
        normalized = (df_array[non_contour] - np.min(df_array)) / (np.ptp(df_array))
        red = (255 * normalized).astype('uint8')
        green_blue = (255 * (1 - normalized)).astype('uint8')
        pixels[non_contour] = np.stack([red, green_blue, green_blue], axis=-1)

    return Image.fromarray(pixels, 'RGB')