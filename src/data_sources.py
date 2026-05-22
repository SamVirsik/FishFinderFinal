"""
Single registry for every external bathymetry data source the viewer
talks to.

This is the one file to edit when you want to add a new NOAA endpoint,
change a URL, tweak a nodata sentinel, hide a source from the dropdown,
or adjust a per-source quirk. Both the server (`src/LayerGeneration.py`,
`app.py`) and the browser (`static/map.js`, via `/sources`) read from
here — there is no second authoritative list anywhere.

----------------------------------------------------------------------
Audit & rework: 2026-05
----------------------------------------------------------------------
All entries in this registry have been verified against NOAA's live
ArcGIS REST directory (https://gis.ngdc.noaa.gov/arcgis/rest/services).
Sources are ordered by usefulness for Florida Keys boating / fishing
visualisation — the primary deployment target — so the dropdown's
top entries are the ones that actually return good data for nearshore
reefs, channels, and flats around 24–26°N, -80 to -82°W.

Rules each entry obeys:

  - Endpoint exists on NOAA's ArcGIS REST directory and accepts an
    `exportImage?...&format=tiff&pixelType=F32` request — verified
    by fetching the service description page.
  - `rendering_rule='{"rasterFunction":"None"}'` is set whenever the
    service's first listed `rasterFunctionInfos` entry is NOT "None"
    (i.e. the service's *default* is a colourised hillshade RGB
    instead of raw elevation). Without this, the server returns a
    3-band RGB image and `_decode_raster` rejects it because the
    array isn't 2-D. This is the most common cause of "this source
    quietly returns nothing" bugs — see notes per entry.
  - `nodata` is set to the exact sentinel a given service uses, so
    nodata pixels round-trip to NaN even when they sit inside the
    plausible-elevation range (e.g. -9999 m). Large-magnitude
    sentinels like 1e6 or -32768 are caught by the |v|>=11000
    fallback in `_decode_raster` regardless, but setting them
    explicitly is documentation and belt-and-braces.

Removed in this rework:

  - `fknms-multibeam` (was: `nccos/FKNMS_multibeam_dem/ImageServer`).
    This URL does not exist and never appears in the NOAA REST
    directory. The only Florida Keys NCCOS service is
    `nccos/BenthicMapping_FKNMS_Dataviewer`, which is a MapServer
    (renders PNG, no raw raster access). Any request was returning
    an HTML 404 page that the decoder rejected, so the source has
    been silently broken since it was added. For high-detail
    Florida Keys data we now rely on `bag-bathymetry`, `nos-mbab`,
    and the 1/9 arc-second CUDEM tiles that `dem-all` stitches in.

----------------------------------------------------------------------
Why dataclasses, not dicts
----------------------------------------------------------------------
  - Field names are type-checked, not stringly-typed.
  - `frozen=True` prevents accidental mutation of a registry entry.
  - Easy to serialise (`to_client_dict`) for the JSON endpoint without
    leaking server-only fields like the upstream URL.

----------------------------------------------------------------------
Why `cache_dir_name` is decoupled from `id`
----------------------------------------------------------------------
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


# `{"rasterFunction":"None"}` tells the ArcGIS ImageServer "no raster
# function — just give me the raw mosaic data". Required for any
# service whose first listed raster function is a colourised
# hillshade (e.g. ColorHillshadeBAG), since omitting renderingRule
# on those endpoints causes the server to apply the first one as the
# default and return a 3-band RGB instead of raw F32. Kept as a
# module-level constant rather than a literal sprinkled across
# entries so the intent is one grep away.
_RAW_PIXELS = '{"rasterFunction":"None"}'


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


# ---------------------------------------------------------------------------
# Source registry.
#
# Order in this tuple is the order shown in the dropdown, ranked by
# usefulness for Florida Keys nearshore boating / fishing — highest
# detail and best coastal coverage first, global / coarse / deep-water
# sources last.
#
# Resolution figures below are taken from each service's published
# `Pixel Size X/Y` field. Florida Keys notes describe what the source
# actually delivers in the 24–26°N, -80 to -82°W box.
# ---------------------------------------------------------------------------
SOURCES: tuple = (

    # -- 1. Best general-purpose source for Florida Keys -------------------
    # `DEM_all` is NCEI's stitched best-available coastal DEM mosaic.
    # It pulls the highest-resolution DEM available for each pixel
    # from across NOAA's full coastal DEM library (CUDEM 1/9 arc-second
    # ≈ 3 m where it exists, CUDEM 1/3 arc-second ≈ 10 m elsewhere,
    # then CRM, regional DEMs, etc., filling in toward deep water).
    # For the Florida Keys the active layer is the 1/9 arc-second
    # CUDEM tiles built from the 2018-2019 NGS topobathy lidar
    # (Hurricane Irma survey, Miami → Marquesas Keys). This is the
    # one source to start with for almost any fishing/boating use.
    DataSource(
        id="dem-all",
        display_name="Coastal Stitched DEM — Best for Florida Keys",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_all/ImageServer/exportImage"),
        nodata=-9999,
        notes=("NCEI stitched best-available coastal DEM. For the "
               "Florida Keys this is CUDEM 1/9 arc-second (~3 m) "
               "from 2018-19 NGS topobathy lidar, with lower-res "
               "DEMs blended in toward deep water."),
    ),

    # -- 2. Hydrographic survey BAGs (sub-meter where surveyed) ------------
    # The BAG mosaic is the highest-detail raw bathymetry NOAA hosts:
    # individual BAG (Bathymetric Attributed Grid) files from
    # multibeam hydrographic surveys, served at their native ≈ 0.1 m
    # raster posting (degrees), which for the Keys works out to
    # roughly sub-meter where surveyed. Coverage is patchy — only
    # actual NOS hydrographic survey footprints — but where it
    # exists it's the gold standard for finding reefs, ledges, and
    # channel edges. Values are depths only (always negative); the
    # service uses 1e6 as nodata.
    #
    # Quirk: this service's default raster function is the colour
    # hillshade, so we MUST pass renderingRule=None to get raw F32.
    DataSource(
        id="bag-bathymetry",
        display_name="BAG Hydrographic Surveys — Highest Detail (Patchy)",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "bag_bathymetry/ImageServer/exportImage"),
        nodata=1000000,
        rendering_rule=_RAW_PIXELS,
        min_zoom=8,
        notes=("Individual NOAA BAG hydrographic survey grids "
               "(sub-meter where surveyed). Depths only — coverage "
               "is patchy but unmatched in detail where it exists. "
               "Excellent for spotting reefs, wrecks, and channel "
               "edges around the Keys."),
    ),

    # -- 3. NOS multibeam-attributed bathymetry (F32) ----------------------
    # NOS_MBAB_F32 is the NOAA NOS Multibeam Bathymetric Attributed
    # Grid mosaic as a raw F32 service. Conceptually similar to the
    # BAG service above but specifically the NOS-collected multibeam
    # subset, sometimes with different/more recent coverage. Includes
    # FL Keys surveys not yet folded into the broader bag_bathymetry
    # mosaic. Like BAG, it returns depths and uses the BAG-standard
    # nodata sentinel (1e6).
    #
    # Quirk: generic-type service, so explicit renderingRule=None.
    # `experimental=True` because the public-facing nodata convention
    # is not documented for this endpoint specifically — the |v|>=11000
    # decoder fallback covers the common cases regardless.
    DataSource(
        id="nos-mbab",
        display_name="NOS Multibeam Surveys — Survey-Grade Detail",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "NOS_MBAB/NOS_MBAB_F32/ImageServer/exportImage"),
        nodata=1000000,
        rendering_rule=_RAW_PIXELS,
        min_zoom=8,
        experimental=True,
        notes=("NOS multibeam-attributed BAG mosaic. Similar in "
               "spirit to the BAG service but the NOS-collected "
               "subset specifically; often picks up newer survey "
               "lines not yet in the broader BAG mosaic."),
    ),

    # -- 4. US-focused stitched DEM mosaic ---------------------------------
    # `DEM_tiles_mosaic` is NCEI's US-focused stitched DEM. It
    # overlaps heavily with `DEM_all` for US waters but has slightly
    # different blending priorities and is the source the public
    # NCEI Bathymetric Data Viewer uses by default. Useful as a
    # cross-check / fallback if `dem-all` looks wrong at a tile.
    DataSource(
        id="dem-tiles",
        display_name="US Coastal DEM Mosaic — Regional Best-Available",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_tiles_mosaic/ImageServer/exportImage"),
        nodata=-9999,
        notes=("NCEI US-focused stitched DEM. Heavily overlaps "
               "dem-all for the Keys; useful as a cross-check or "
               "fallback when dem-all has gaps."),
    ),

    # -- 5. Coastal Relief Model (CRM) -------------------------------------
    # NOAA's classic Coastal Relief Model: ~3 arc-second (~90 m)
    # bathymetry-and-topography mosaic, regionally complete for US
    # coasts including the Florida Keys. Older and coarser than
    # CUDEM but reliable, complete, and well-vetted. Useful at
    # mid-zoom scales where high-res sources are overkill, and as
    # an offshore fallback for the 50–500 m depth range.
    DataSource(
        id="crm-mosaic",
        display_name="Coastal Relief Model — ~90 m, Complete Coverage",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/CRM_mosaic/ImageServer/exportImage"),
        nodata=-9999,
        max_zoom=15,
        notes=("NOAA's classic ~90 m coastal relief mosaic. "
               "Complete US coastal coverage including the Keys "
               "and Tortugas. Coarser than CUDEM but reliable; "
               "good for mid-zoom views and the 50–500 m depths."),
    ),

    # -- 6. Global multibeam mosaic (deep water) ---------------------------
    # NCEI's global multibeam mosaic, ≈ 100 m / 3 arc-second posting.
    # For the Keys specifically this matters offshore: the Florida
    # Strait, the Pourtalès Terrace, the deep wall east of the
    # Keys, and the Gulf side of the Tortugas. Inside the reef tract
    # it has very little coverage — use one of the higher entries.
    # The label "deep-water" is therefore accurate for the role this
    # source plays, even though the service technically spans the
    # globe.
    #
    # Quirk: default raster function is `ColorHillshadeHaxby_8000-0`
    # — a colour hillshade. Without renderingRule=None the server
    # returns an RGB image and the decoder silently rejects it.
    # This was the latent bug in the previous version of this file.
    DataSource(
        id="multibeam",
        display_name="Global Multibeam — Offshore Deep Water (~100 m)",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "multibeam_mosaic/ImageServer/exportImage"),
        nodata=-32768,
        rendering_rule=_RAW_PIXELS,
        max_zoom=14,
        notes=("NCEI ~100 m global multibeam mosaic. Useful for "
               "offshore Florida — the Strait, deep wall, "
               "Pourtalès Terrace — but sparse inside the reef "
               "tract. Switch to dem-all or BAG for nearshore."),
    ),

    # -- 7. Global relief (ETOPO 2022) -------------------------------------
    # `DEM_global_mosaic` serves the latest NOAA global relief
    # product (currently ETOPO 2022, 15 arc-second ≈ 460 m, blending
    # GEBCO with the best-available regional DEMs). Far too coarse
    # for FL Keys nearshore work — depths under ~50 m all collapse
    # to a few pixels of "shallow" — but useful for global context
    # and very low zoom levels where everything else returns no
    # data.
    DataSource(
        id="dem-global",
        display_name="Global Relief (ETOPO 2022) — Worldwide Context",
        url=("https://gis.ngdc.noaa.gov/arcgis/rest/services/"
             "DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage"),
        nodata=-9999,
        max_zoom=11,
        notes=("NOAA's latest global relief mosaic (ETOPO 2022, "
               "~460 m). Worldwide context only — far too coarse "
               "for the Keys nearshore. Useful at very low zoom "
               "where everything else has no data."),
    ),
)

DEFAULT_SOURCE_ID = "dem-all"

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
