"""
Single registry for every external bathymetry data source the viewer
talks to.

This is the one file to edit when you want to add a new NOAA endpoint,
change a URL, tweak a nodata sentinel, hide a source from the dropdown,
or adjust a per-source quirk. Both the server (`src/LayerGeneration.py`,
`app.py`) and the browser (`static/map.js`, via `/sources`) read from
here — there is no second authoritative list anywhere.

Why dataclasses, not dicts:
  - Field names are type-checked, not stringly-typed.
  - `frozen=True` prevents accidental mutation of a registry entry.
  - Easy to serialise (`to_client_dict`) for the JSON endpoint without
    leaking server-only fields like the upstream URL.

Why `cache_dir_name` is decoupled from `id`:
  - `id` is the URL token clients send (`/raster/<id>/...`).
  - `cache_dir_name` is the on-disk path component
    (`img/raster/<cache_dir_name>/...`).
  - Separating them means we can rename `id` without invalidating
    existing disk caches, AND it closes the silent-cache-leak bug
    where unknown source IDs used to fall back to dem-tiles but
    cached the bytes under the bogus name.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DataSource:
    """One external bathymetry data source.

    All NOAA mosaics are queried in EPSG:3857 at pixelType=F32, so the
    common params live as defaults; only the URL and per-source quirks
    (nodata sentinel, rendering rule) actually vary.
    """
    id: str
    display_name: str
    url: str
    pixel_type: str = "F32"
    image_sr: int = 3857
    nodata: Optional[float] = None
    rendering_rule: Optional[str] = None
    extra_params: dict = field(default_factory=dict)
    min_zoom: int = 0
    max_zoom: int = 22
    cache_dir_name: Optional[str] = None
    enabled: bool = True
    hidden: bool = False
    experimental: bool = False
    timeout_s: float = 30.0
    notes: str = ""

    @property
    def cache_key(self) -> str:
        """Disk-cache directory name. Defaults to the id."""
        return self.cache_dir_name or self.id


# Order in this tuple is the order shown in the dropdown.
SOURCES: tuple = (
    DataSource(
        id="dem-tiles",
        display_name="Default DEM Mosaic — broad coverage",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage"),
        nodata=-9999,
    ),
    DataSource(
        id="dem-all",
        display_name="Stitched Coastal — best near shore",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_all/ImageServer/exportImage"),
        nodata=-9999,
    ),
    DataSource(
        id="fknms-multibeam",
        display_name="Florida Keys 2–5 m — highest detail",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "nccos/FKNMS_multibeam_dem/ImageServer/exportImage"),
        nodata=-32768,
    ),
    DataSource(
        id="bag-bathymetry",
        display_name="BAG — high-res, sparse coverage",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "bag_bathymetry/ImageServer/exportImage"),
        nodata=1000000,
        rendering_rule='{"rasterFunction":"None"}',
    ),
    DataSource(
        id="multibeam",
        display_name="Deep-Water Multibeam — offshore",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "multibeam_mosaic/ImageServer/exportImage"),
        nodata=-32768,
    ),
    DataSource(
        id="crm-mosaic",
        display_name="Coastal Relief — coarse, complete",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/CRM_mosaic/ImageServer/exportImage"),
        nodata=-9999,
    ),
    DataSource(
        id="dem-global",
        display_name="Global Bathymetry — worldwide",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage"),
        nodata=-9999,
    ),
)

DEFAULT_SOURCE_ID = "dem-tiles"

_BY_ID = {s.id: s for s in SOURCES}


def get_source(source_id: str) -> Optional[DataSource]:
    """Resolve a source ID. Returns None for unknown IDs.

    Callers should treat None as 'reject the request' — there is no
    silent fallback. The previous fallback-to-default behavior was the
    cause of the cache-mislabelling foot-gun (audit finding #7).
    """
    if not source_id:
        return None
    src = _BY_ID.get(source_id)
    if src is None or not src.enabled:
        return None
    return src


def all_visible_sources() -> list:
    """Sources to surface in the UI dropdown, in registry order."""
    return [s for s in SOURCES if s.enabled and not s.hidden]


def to_client_dict(s: DataSource) -> dict:
    """Subset of a DataSource the browser is allowed to see.

    Deliberately omits the upstream URL and request quirks — those are
    server-internal. The client only needs enough to render the dropdown
    and clamp prefetch to per-source zoom limits.
    """
    return {
        "id": s.id,
        "display_name": s.display_name,
        "min_zoom": s.min_zoom,
        "max_zoom": s.max_zoom,
        "experimental": s.experimental,
        "notes": s.notes,
    }
