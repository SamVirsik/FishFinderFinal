import os
import time
from functools import lru_cache
from io import BytesIO

import numpy as np
import pandas as pd
import requests
from PIL import Image

from src.analyses import *


class GPSBounds():
    def __init__(self, extent=None, lonmin=0, latmax=0, latmin=0, lonmax=0):
        if isinstance(extent, dict):
            self.latmin = extent['latmin']
            self.latmax = extent['latmax']
            self.lonmin = extent['lonmin']
            self.lonmax = extent['lonmax']
        elif isinstance(extent, list):
            self.lonmin = extent[0]
            self.lonmax = extent[1]
            self.latmin = extent[2]
            self.latmax = extent[3]
        elif isinstance(extent, GPSBounds):
            self.lonmin = extent.lonmin
            self.lonmax = extent.lonmax
            self.latmin = extent.latmin
            self.latmax = extent.latmax
        else:
            self.latmin = latmin
            self.latmax = latmax
            self.lonmin = lonmin
            self.lonmax = lonmax
    def noaabox(self):
        return str(self.lonmin)+','+str(self.latmin)+','+str(self.lonmax)+','+str(self.latmax)
    def array(self): #lon_min, lat_max, lon_max, lat_min
        return [self.lonmin, self.lonmax, self.latmin, self.latmax]
    def __str__(self):
        return f"GPSBounds(Longitude: {self.lonmin} - {self.lonmax}, Latitude {self.latmin} - {self.latmax})"


class LayerGenerator():
    def __init__(self):
        self.analysis_method = "Heatmap"
        self.roll = 1
        self.width = 0.5
        self.resolution = 1024
        self.data_source = "dem-tiles"
        self.GPS = GPSBounds()

    def calculate_size(self, max_width):
        xmin, xmax, ymin, ymax = self.GPS.array()
        aspect_ratio = (xmax - xmin) / (ymax - ymin)
        width = max_width
        height = int(width / aspect_ratio)
        return f"{width},{height}"

    def set_sizing(self):
        self.size_parameter = self.calculate_size(self.resolution)

    def set_resolution(self, res=None):
        if res is None:
            res = self.resolution
        self.resolution = res
        self.set_sizing()

    def set_analysis(self, selection):
        self.analysis_method = selection

    def set_roll(self, roll):
        self.roll = roll

    def set_width(self, width):
        self.width = width

    def set_gps_bounds(self, extent):
        self.set_GPS(GPSBounds(extent))

    def set_GPS(self, gps):
        self.GPS = gps

    def set_data_source(self, source):
        """Switch the active NOAA endpoint; clears the depth cache when the source actually changes."""
        if source != self.data_source:
            self.sample_depth.cache_clear()
        self.data_source = source

    def load_data(self, cache_file, debug_prints=False, event=None):
        self.image = self.make_image(cache_file, debug_prints)
        return self.image

    def _source_spec(self):
        """
        Decide which endpoint to hit and which extra parameters to
        send.  Tweak by commenting lines in/out or adding new `elif`
        blocks.  Return (url, extra_params_dict).
        """
        if self.data_source == "dem-tiles":
            # --- DEM Mosaic (default) -----------------------------
            url = 'https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage'
            params = {
                'bbox': self.GPS.noaabox(),
                'size': self.size_parameter, 
                'bboxSR': '4326', 
                'imageSR': '4326',
                'format': 'tiff', 
                'f': 'image' 
            }

        elif self.data_source == "bag-bathymetry":
            url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/bag_bathymetry/ImageServer/exportImage"
            params = {
                "bbox"     : self.GPS.noaabox(),
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,      # e.g. "4096,4096"
                "format"   : "tiff",
                "renderingRule":
                    '{"rasterFunction":"None"}',       # URL-encode if you build the URL manually
                "noData"   : 0,
                "noDataInterpretation": "esriNoDataMatchAny",
                "compression": "LZ77",
                "f"        : "image"
            }
        
        elif self.data_source == "multibeam":
            url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/multibeam_mosaic/ImageServer/exportImage"
            params = {
                "bbox"     : self.GPS.noaabox(),
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,
                "format"   : "tiff",
                "pixelType": "F32",
                "noData"   : -32768,
                "noDataInterpretation": "esriNoDataMatchAny",
                "compression": "LERC",
                "f"        : "image"
            }

        
        elif self.data_source == "crm-mosaic":
            url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/CRM_mosaic/ImageServer/exportImage"
            params = {
                "bbox"     : self.GPS.noaabox(),
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,
                "format"   : "tiff",
                "pixelType": "F32",
                "noData"   : -9999,
                "noDataInterpretation": "esriNoDataMatchAny",
                "compression": "LERC",
                "f"        : "image"
            }
        
        elif self.data_source == "dem-all":
            # --- NCEI Best-available Coastal DEMs mosaic ------------------------
            url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/exportImage"
            params = {
                "bbox"     : self.GPS.noaabox(),
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,
                "format"   : "tiff",
                "pixelType": "F32",        # 32-bit float depths/elevations
                "noData"   : -9999,
                "compression": "LERC",
                "f"        : "image"
            }
        
        elif self.data_source == "dem-global":
            # --- Global 1-arc-sec (≈30 m) mosaic -------------------------------
            url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage"
            params = {
                "bbox"     : self.GPS.noaabox(),
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,
                "format"   : "tiff",
                "pixelType": "F32",
                "noData"   : -9999,
                "compression": "LERC",
                "f"        : "image"
            }
        
        elif self.data_source == "fknms-multibeam":
            # NCCOS Florida Keys multibeam DEM Mosaic (2–5 m)
            url = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
                "nccos/FKNMS_multibeam_dem/ImageServer/exportImage")

            params = {
                "bbox"     : self.GPS.noaabox(),   # lonmin,latmin,lonmax,latmax
                "bboxSR"   : "4326",
                "imageSR"  : "4326",
                "size"     : self.size_parameter,  # e.g. "1024,1024"
                "format"   : "tiff",               # ImageServer honors this ✔
                "pixelType": "F32",                # 32-bit float depths
                "noData"   : -32768,
                "compression": "LERC",
                "f"        : "image"
            }

        


        

        # ►► ADD MORE SOURCES BY COPYING THIS elif TEMPLATE ◄◄
        # elif self.data_source == "gebco-2023":
        #     url = "https://www.gebco.net/foobar/exportImage"
        #     params = {
        #         "something": "value",
        #     }

        else:   # fallback
            url = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
                    "DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage")
            params = {}

        return url, params
    # ──────────────────────────────────────────────────────────────

    def handle_calls(self, url, params):
        if self.data_source == "bluetopo":
            response = requests.get(url, stream=True)
            if response.status_code != 200:
                print(f"Failed to fetch BlueTopo tile: {response.status_code}")
                return None
            return BytesIO(response.content)
        else:
            response = requests.get(url, params=params)
            return response
        


    def make_image(self, cache_file, debug_prints=False):
        start_time = time.time()

        url, params = self._source_spec()
        try:
            response = self.handle_calls(url, params=params)
        except Exception:
            return None

        if debug_prints:
            print("Retrieve data", f"{time.time() - start_time}")

        image = None
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            if "image" not in content_type:
                print(f"Unexpected content type: {content_type}")
                print(f"Response content: {response.text}")
            else:
                try:
                    image = Image.open(BytesIO(response.content))
                    image.load()
                except Exception as e:
                    image = None
                    print(f"Error loading image: {e}")
        else:
            print(f"Failed to load data. Status code: {response.status_code}")

        if image is not None:
            image.save(cache_file)
            image_array = np.array(image)
            df = pd.DataFrame(image_array)

            curr_method = self.analysis_method.lower()
            width = self.width

            if curr_method == 'heatmap':
                img = seaborn_heat_map(df, width)
            elif curr_method == 'heatmap-granular':
                img = granular_heat_map(df, width)
            elif curr_method == 'contour':
                img = contour_map(df, width)
            elif curr_method == 'hillshade':
                img = hillshade(df, width)
            elif curr_method == 'colored-hillshade':
                img = colored_hillshade(df, width)
            elif curr_method == 'texture-shade':
                img = textureshade(df, width)
            elif curr_method == 'flow-exposure':
                img = flow_exposure(df, width)
            elif curr_method == 'slope-magnitude':
                img = slope_magnitude(df, width)
            elif curr_method == 'spot-finder':
                img = spot_finder(df, width)

            if debug_prints:
                print("Process image", f"{time.time() - start_time}")
            return img
        else:
            return None

    # Returns metres at a single lat/lon (negative = water, positive = land).
    # Cache is cleared in set_data_source() so values never mix across sources.
    @lru_cache(maxsize=20_000)
    def sample_depth(self, lat: float, lon: float) -> float | None:
        export_url, base_params = self._source_spec()
        sample_url = export_url.rsplit("/", 1)[0] + "/getSamples"

        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "returnFirstValueOnly": "true",
            "outFields": "PixelValue",
            "f": "json",
        }
        for key in ("renderingRule", "noData", "noDataInterpretation"):
            if key in base_params:
                params[key] = base_params[key]

        try:
            r = requests.get(sample_url, params=params, timeout=5)
            r.raise_for_status()
            sample = r.json()["samples"][0]["value"]
            return None if sample is None else float(sample)
        except Exception as e:
            print(f"[sample_depth] {self.data_source} query failed: {e}")
            return None