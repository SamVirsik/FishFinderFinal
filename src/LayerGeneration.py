import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

import numpy as np
import pandas as pd
import requests
from PIL import Image

from src.analyses import ANALYSES


HTTP_TIMEOUT_S = 30


class GPSBounds:
    def __init__(self, extent=None, lonmin=0, latmax=0, latmin=0, lonmax=0):
        if isinstance(extent, dict):
            self.latmin = extent['latmin']
            self.latmax = extent['latmax']
            self.lonmin = extent['lonmin']
            self.lonmax = extent['lonmax']
        elif isinstance(extent, list):
            self.lonmin, self.lonmax, self.latmin, self.latmax = extent
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
        return f"{self.lonmin},{self.latmin},{self.lonmax},{self.latmax}"

    def array(self):
        return [self.lonmin, self.lonmax, self.latmin, self.latmax]

    def __str__(self):
        return (f"GPSBounds(Longitude: {self.lonmin} - {self.lonmax}, "
                f"Latitude {self.latmin} - {self.latmax})")


@dataclass(frozen=True)
class RenderConfig:
    """Immutable snapshot of the renderer's state for a single tile request."""
    gps: GPSBounds
    resolution: int
    analysis: str
    width: float
    roll: int
    data_source: str

    def size_param(self) -> str:
        xmin, xmax, ymin, ymax = self.gps.array()
        # Guard against degenerate boxes (would make height = 0 or div-by-zero).
        dx = xmax - xmin
        dy = ymax - ymin
        if dx <= 0 or dy <= 0:
            return f"{self.resolution},{self.resolution}"
        height = max(1, int(self.resolution * dy / dx))
        return f"{self.resolution},{height}"


def _source_spec(cfg: RenderConfig):
    """
    Endpoint + query params for the active data source.
    To add a source: append an `elif`, then list it in the front-end dropdown.
    """
    bbox = cfg.gps.noaabox()
    size = cfg.size_param()
    common = {
        "bbox": bbox,
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": size,
        "format": "tiff",
        "f": "image",
    }

    if cfg.data_source == "dem-tiles":
        url = ('https://gis.ngdc.noaa.gov/arcgis/rest/services/'
               'DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage')
        return url, common

    if cfg.data_source == "bag-bathymetry":
        url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/bag_bathymetry/ImageServer/exportImage"
        return url, {**common,
                     "renderingRule": '{"rasterFunction":"None"}',
                     "noData": 0,
                     "noDataInterpretation": "esriNoDataMatchAny",
                     "compression": "LZ77"}

    if cfg.data_source == "multibeam":
        url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/multibeam_mosaic/ImageServer/exportImage"
        return url, {**common,
                     "pixelType": "F32",
                     "noData": -32768,
                     "noDataInterpretation": "esriNoDataMatchAny",
                     "compression": "LERC"}

    if cfg.data_source == "crm-mosaic":
        url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/CRM_mosaic/ImageServer/exportImage"
        return url, {**common,
                     "pixelType": "F32",
                     "noData": -9999,
                     "noDataInterpretation": "esriNoDataMatchAny",
                     "compression": "LERC"}

    if cfg.data_source == "dem-all":
        url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/exportImage"
        return url, {**common,
                     "pixelType": "F32",
                     "noData": -9999,
                     "compression": "LERC"}

    if cfg.data_source == "dem-global":
        url = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage"
        return url, {**common,
                     "pixelType": "F32",
                     "noData": -9999,
                     "compression": "LERC"}

    if cfg.data_source == "fknms-multibeam":
        url = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
               "nccos/FKNMS_multibeam_dem/ImageServer/exportImage")
        return url, {**common,
                     "pixelType": "F32",
                     "noData": -32768,
                     "compression": "LERC"}

    # Unknown source — fall back to default.
    url = ('https://gis.ngdc.noaa.gov/arcgis/rest/services/'
           'DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage')
    return url, common


def _fetch_raster(cfg: RenderConfig):
    """Hit the NOAA ImageServer and return a PIL.Image, or None on failure."""
    url, params = _source_spec(cfg)
    try:
        response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        print(f"[NOAA] request failed: {e}")
        return None

    if response.status_code != 200:
        print(f"[NOAA] HTTP {response.status_code} for {cfg.data_source}")
        return None

    if "image" not in response.headers.get("Content-Type", ""):
        print(f"[NOAA] unexpected content-type: {response.headers.get('Content-Type')!r}")
        return None

    try:
        image = Image.open(BytesIO(response.content))
        image.load()
        return image
    except Exception as e:
        print(f"[NOAA] could not decode raster: {e}")
        return None


def _load_cached_raster(cache_file):
    if not cache_file or not os.path.exists(cache_file):
        return None
    try:
        image = Image.open(cache_file)
        image.load()
        return image
    except Exception as e:
        print(f"[cache] could not load {cache_file}: {e}")
        return None


def _raster_to_dataframe(image):
    """Coerce a PIL image into a 2-D float DataFrame, or return None."""
    arr = np.asarray(image)
    if arr.ndim == 3:
        # Some endpoints return RGB previews rather than single-band float —
        # nothing useful we can do with that as a depth grid.
        return None
    return pd.DataFrame(arr)


class LayerGenerator:
    """
    Holds the active rendering configuration. Tile-render entry points snapshot
    the config under a lock so concurrent requests can render in parallel
    without racing on shared state.
    """

    def __init__(self):
        self.analysis_method = "heatmap"
        self.roll = 1
        self.width = 0.5
        self.resolution = 1024
        self.data_source = "dem-tiles"
        self.GPS = GPSBounds()
        self._lock = threading.Lock()

    # -- mutators (called from /reload-layer and /tile setup) ----------------

    def set_resolution(self, res=None):
        with self._lock:
            if res is not None:
                self.resolution = int(res)

    def set_analysis(self, selection):
        with self._lock:
            self.analysis_method = selection

    def set_roll(self, roll):
        with self._lock:
            self.roll = roll

    def set_width(self, width):
        with self._lock:
            self.width = width

    def set_gps_bounds(self, extent):
        with self._lock:
            self.GPS = GPSBounds(extent)

    def set_GPS(self, gps):
        with self._lock:
            self.GPS = GPSBounds(gps)

    def set_data_source(self, source):
        """Switch the active NOAA endpoint; clears the depth cache on change."""
        with self._lock:
            if source != self.data_source:
                self.sample_depth.cache_clear()
            self.data_source = source

    # -- render --------------------------------------------------------------

    def snapshot(self, gps=None) -> RenderConfig:
        """Atomic copy of the current config, optionally with an override bbox."""
        with self._lock:
            return RenderConfig(
                gps=GPSBounds(gps) if gps is not None else GPSBounds(self.GPS),
                resolution=self.resolution,
                analysis=self.analysis_method,
                width=self.width,
                roll=self.roll,
                data_source=self.data_source,
            )

    def render(self, cfg: RenderConfig, cache_file=None):
        """Produce the colorized PIL image for `cfg`. Returns None on failure."""
        image = _load_cached_raster(cache_file)
        if image is None:
            image = _fetch_raster(cfg)
            if image is None:
                return None
            if cache_file:
                try:
                    image.save(cache_file)
                except Exception as e:
                    print(f"[cache] could not write {cache_file}: {e}")

        df = _raster_to_dataframe(image)
        if df is None:
            print(f"[render] non-2D raster from {cfg.data_source}; skipping")
            return None

        analysis = ANALYSES.get(cfg.analysis.lower())
        if analysis is None:
            print(f"[render] unknown analysis {cfg.analysis!r}")
            return None

        try:
            return analysis(df, cfg.width)
        except Exception as e:
            print(f"[render] {cfg.analysis} failed: {e}")
            return None

    # Backwards-compat shim used by the existing tile route. New code should
    # call .snapshot()/.render() directly.
    def load_data(self, cache_file=None, gps=None):
        cfg = self.snapshot(gps=gps)
        return self.render(cfg, cache_file=cache_file)

    # -- point sampling -------------------------------------------------------

    @lru_cache(maxsize=20_000)
    def sample_depth(self, lat: float, lon: float):
        """Metres at a single lat/lon (negative = water, positive = land)."""
        cfg = self.snapshot()
        export_url, base_params = _source_spec(cfg)
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
            print(f"[sample_depth] {cfg.data_source} query failed: {e}")
            return None
