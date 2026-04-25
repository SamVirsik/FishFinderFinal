#!/usr/bin/env python3
"""
preview_standalone.py
=====================
• NO Flask, NO LayerGeneration import.
• Only external project dependency is the *viewer / data-processing* helper
  you already have:  FishFinderTools.zeroToOneFifty

Edit the four coordinate constants (LAT_MAX, LON_MIN, LAT_MIN, LON_MAX)
and the pixel width if you want a different area or resolution.
"""

import io
import math
import sys
from pathlib import Path

import requests
import tifffile as tiff
import pandas as pd
from PIL import Image
import matplotlib.pyplot as plt

# ---- 1. bring in the existing colour-ramp renderer ------------------
from src.FishFinderTools import zeroToOneFifty
from src.analyses import colored_hillshade

# ---- 2. user-tunable parameters -------------------------------------
#   (lat_max, lon_min)  = north-west corner
#   (lat_min, lon_max)  = south-east corner

#top_left = (24.861117, -80.825807)
#bottom_right = (24.798404, -80.728636)


top_left = (24.745848, -81.065087)
bottom_right = (24.709701, -80.996635)




LAT_MAX = top_left[0]          #  example: Florida Keys
LON_MIN = top_left[1]
LAT_MIN = bottom_right[0]
LON_MAX = bottom_right[1]

PIXEL_WIDTH = 4096         #  increase for finer detail
NOAA_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/"
    "DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage"
)


# ---------------------------------------------------------------------
def geo_height(lat_max, lat_min, lon_max, lon_min, width):
    """Maintain geographic aspect ratio (roughly)."""
    mid_lat = 0.5 * (lat_max + lat_min)
    dx = lon_max - lon_min
    dy = lat_max - lat_min
    # compensate for longitude shrinking toward poles
    height = width * dy / (dx * math.cos(math.radians(mid_lat)) + 1e-9)
    return max(1, int(round(height)))


def fetch_dem_tiff():
    """Download the GeoTIFF bytes for our box at the requested size."""
    width = PIXEL_WIDTH
    height = geo_height(LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, width)

    bbox = f"{LON_MIN},{LAT_MIN},{LON_MAX},{LAT_MAX}"
    params = {
        "f": "image",
        "format": "tiff",
        "pixelType": "F32",          # real-valued metres
        "size": f"{width},{height}",
        "bbox": bbox,
        "bboxSR": 4326,
        "imageSR": 4326,
        "interpolation": "Bilinear",
        "noDataInterpretation": "esriNoDataMatchAny",
    }

    print("Contacting NOAA ImageServer …")
    r = requests.get(NOAA_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.content


def main() -> None:
    try:
        tiff_bytes = fetch_dem_tiff()
    except Exception as exc:
        print("Download failed:", exc)
        sys.exit(1)

    # ---- 3. convert to DataFrame ------------------------------------
    dem_array = tiff.imread(io.BytesIO(tiff_bytes))
    df = pd.DataFrame(dem_array)
    n_rows, n_cols = df.shape

    # ---- 4. colourise with the existing helper ----------------------
    print("Converting depth matrix → RGB image …")
    #rgb_img: Image.Image = zeroToOneFifty(df, n_rows, n_cols)
    rgb_img: Image.Image = colored_hillshade(df, 0.675, azimuth=315.0, altitude=40.0, ambient=0.05)

    # ---- 5. show it & (optionally) save -----------------------------
    print("Displaying – close the window to exit.")
    plt.figure(figsize=(10, 10))
    plt.axis("off")
    plt.imshow(rgb_img)
    plt.tight_layout()
    plt.show()

    out_path = Path(__file__).with_suffix(".png")
    rgb_img.save(out_path)
    print("Saved a copy to", out_path)


if __name__ == "__main__":
    main()