"""
Single registry for every basemap the viewer can show beneath the
bathymetry overlay.

This is the one file to edit when you want to add a basemap provider,
swap a tile-server URL, or change attribution text. Both the server
(`app.py:/basemaps`) and the browser (`static/map.js`) read from here —
there is no second list.

----------------------------------------------------------------------
Keyless by design
----------------------------------------------------------------------
Every entry here is a public-domain / no-key tile service. FishFinder
runs with zero API keys and makes no calls to Esri-hosted basemaps,
MapTiler, or EOX. The only basemap providers are USGS's public National
Map tile services (USGS + USDA NAIP imagery) — see each entry. If you
add a provider that needs a key, you are breaking the keyless guarantee;
don't.

----------------------------------------------------------------------
Why a registry (mirrors src/data_sources.py)
----------------------------------------------------------------------
Same reasoning as the bathymetry registry: frozen dataclass for
type-checked, immutable entries; one tuple in source order so the UI
grid layout is decided here, not in HTML; a `list_basemaps()`
serialiser that strips server-only fields before the JSON crosses the
wire.

----------------------------------------------------------------------
Why `/basemaps` has no `default` field (divergence from `/sources`)
----------------------------------------------------------------------
Deliberate. The client pins its cold-load basemap with a hardcoded
literal so the map can render before any HTTP round-trip lands. A
server-supplied default would either be ignored (we already chose) or
cause an extra basemap swap during boot. Don't "fix" this by adding
one — read `static/map.js` for the boot-order constraint that makes
this choice load-bearing. The hardcoded client default MUST match the
`id` of one entry below (currently "usgs-imagery-topo").

----------------------------------------------------------------------
Provider flavours
----------------------------------------------------------------------
  - "arcgis_rest": `value` is an ArcGIS REST MapServer URL (loaded as a
                   TileLayer). The service's published tile info supplies
                   size + max zoom, so the registry leaves those None.
                   This is *not* an Esri-hosted basemap — it is USGS's
                   own National Map service, which happens to speak the
                   ArcGIS REST protocol. No key, no account.
  - "xyz":         `value` is an XYZ URL template using ArcGIS
                   WebTileLayer tokens ({level}/{col}/{row}). Reserved
                   for future keyless XYZ providers; none are registered
                   today.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BasemapSource:
    """One basemap entry surfaced in the Basemap button grid."""
    id: str
    display_name: str
    provider: str                              # "arcgis_rest" | "xyz"
    value: str
    attribution: str = ""
    max_zoom: Optional[int] = None             # None → defer to provider default
    tile_size: Optional[int] = None            # None → defer to provider default
    tooltip: Optional[str] = None              # used as button title attr


# ---------------------------------------------------------------------------
# Basemap registry. Order in this tuple is the order rendered in the grid.
# All entries are keyless USGS National Map services (public domain).
# The first entry ("usgs-imagery-topo") is the cold-load default and must
# match the hardcoded literal in static/map.js.
# ---------------------------------------------------------------------------
BASEMAPS: tuple = (
    # USGS Imagery + Topo. Aerial imagery with topographic labels/contours
    # overlaid. Public domain, no key. Good default for coastal work — you
    # can read shorelines, keys, and shallow reefs straight from the
    # imagery. Open ocean shows dark, but the bathymetry overlay is the
    # point there anyway.
    BasemapSource(
        id="usgs-imagery-topo",
        display_name="USGS Imagery",
        provider="arcgis_rest",
        value=("https://basemap.nationalmap.gov/arcgis/rest/"
               "services/USGSImageryTopo/MapServer"),
        attribution="USGS, USDA",
        tooltip="Aerial imagery with topo labels. Public domain, no key.",
    ),

    # USGS Topo. Standard topographic basemap — neutral land/water fill
    # plus place names, useful as a labelled reference layer. Public
    # domain, no key.
    BasemapSource(
        id="usgs-topo",
        display_name="USGS Topo",
        provider="arcgis_rest",
        value=("https://basemap.nationalmap.gov/arcgis/rest/"
               "services/USGSTopo/MapServer"),
        attribution="USGS, USDA",
        tooltip="USGS topographic map. Public domain, no key.",
    ),

    # USGS Imagery (NAIP-backed where available). Pure aerial, no labels.
    # Public domain, no key. CONUS land-only — open water shows as
    # blank/black tiles. The tooltip warns before the user wonders where
    # the ocean went.
    BasemapSource(
        id="usgs-aerial",
        display_name="USGS Aerial",
        provider="arcgis_rest",
        value=("https://basemap.nationalmap.gov/arcgis/rest/"
               "services/USGSImageryOnly/MapServer"),
        attribution="USGS, USDA",
        tooltip="High-res NAIP aerial; open water shows as blank.",
    ),
)


def _to_client_dict(b: BasemapSource) -> dict:
    """Wire-format projection of one entry."""
    return {
        "id":          b.id,
        "display_name": b.display_name,
        "provider":    b.provider,
        "value":       b.value,
        "attribution": b.attribution,
        "max_zoom":    b.max_zoom,
        "tile_size":   b.tile_size,
        "tooltip":     b.tooltip,
    }


def list_basemaps() -> list:
    """Registry entries surfaced to the browser, in registry order.

    Every entry is keyless, so there is no env-var gating or token
    substitution — the list is the registry, verbatim.
    """
    return [_to_client_dict(b) for b in BASEMAPS]
