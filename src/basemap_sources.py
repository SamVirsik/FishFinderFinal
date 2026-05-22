"""
Single registry for every basemap the viewer can show beneath the
bathymetry overlay.

This is the one file to edit when you want to add a basemap provider,
swap a tile-server URL, change attribution text, or gate an entry behind
an API key. Both the server (`app.py:/basemaps`) and the browser
(`static/map.js`) read from here — there is no second list.

----------------------------------------------------------------------
Why a registry (mirrors src/data_sources.py)
----------------------------------------------------------------------
Same reasoning as the bathymetry registry: frozen dataclass for
type-checked, immutable entries; one tuple in source order so the UI
grid layout is decided here, not in HTML; a `list_basemaps()`
serialiser that strips server-only fields (env-var bookkeeping)
before the JSON crosses the wire.

----------------------------------------------------------------------
Why `/basemaps` has no `default` field (divergence from `/sources`)
----------------------------------------------------------------------
Deliberate. The client pins its cold-load basemap with a hardcoded
literal so the map can render before any HTTP round-trip lands. A
server-supplied default would either be ignored (we already chose) or
cause an extra basemap swap during boot. Don't "fix" this by adding
one — read `static/map.js` for the boot-order constraint that makes
this choice load-bearing.

----------------------------------------------------------------------
Three provider flavours
----------------------------------------------------------------------
  - "esri":        `value` is a built-in Esri basemap ID. The client
                   assigns it to `map.basemap` as a string; Esri owns
                   the layer set, tile info, and attribution.
  - "xyz":         `value` is an XYZ URL template using ArcGIS
                   WebTileLayer tokens ({level}/{col}/{row}). NOTE
                   that EOX puts row before col — the registry value
                   carries whatever order the provider expects, no
                   reordering happens on the wire.
  - "arcgis_rest": `value` is an ArcGIS REST MapServer URL. The
                   service's published tile info supplies size + max
                   zoom, so the registry leaves those None.

----------------------------------------------------------------------
Env-var gating
----------------------------------------------------------------------
Entries with `env_required` set are filtered out of `list_basemaps()`
when that env var is unset (or empty). The MapTiler entry uses this
to disappear entirely when the user hasn't supplied a key — no broken
button, no placeholder URL, no console noise. When set,
`env_substitute_token` is replaced inside `value` with the env-var
value before the dict crosses the wire.
"""

from dataclasses import dataclass
import os
from typing import Optional


@dataclass(frozen=True)
class BasemapSource:
    """One basemap entry surfaced in the Basemap button grid."""
    id: str
    display_name: str
    provider: str                              # "esri" | "xyz" | "arcgis_rest"
    value: str
    attribution: str = ""                      # "" is correct for esri entries
    max_zoom: Optional[int] = None             # None → defer to provider default
    tile_size: Optional[int] = None            # None → defer to provider default
    tooltip: Optional[str] = None              # used as button title attr
    env_required: Optional[str] = None         # entry dropped when env var unset
    env_substitute_token: Optional[str] = None # placeholder replaced with env value


# ---------------------------------------------------------------------------
# Basemap registry. Order in this tuple is the order rendered in the grid.
# Esri entries first (Dark is the cold-load default and is marked active in
# `static/map.js`), then the three free non-Esri additions:
#   - MapTiler Satellite (gated on MAPTILER_API_KEY)
#   - USGS Aerial (NAIP, public domain, no key)
#   - EOX Sentinel-2 Cloudless 2024 (CC BY-NC-SA, no key)
# ---------------------------------------------------------------------------
BASEMAPS: tuple = (
    BasemapSource("dark-gray-vector", "Dark",        "esri", "dark-gray-vector"),
    BasemapSource("streets-vector",   "Streets",     "esri", "streets-vector"),
    BasemapSource("satellite",        "Satellite",   "esri", "satellite"),
    BasemapSource("hybrid",           "Hybrid",      "esri", "hybrid"),
    BasemapSource("oceans",           "Oceans",      "esri", "oceans"),
    BasemapSource("topo-vector",      "Topographic", "esri", "topo-vector"),

    # MapTiler Satellite v2. 512px tiles, max zoom 20. The key ships to
    # the browser (any tile request is client-direct), so domain-restrict
    # it in MapTiler Cloud — see README. Entry vanishes when the env var
    # is unset; do not switch to a placeholder display.
    BasemapSource(
        id="maptiler_satellite",
        display_name="MapTiler Sat",
        provider="xyz",
        value=("https://api.maptiler.com/tiles/satellite-v2/"
               "{level}/{col}/{row}.jpg?key={MAPTILER_API_KEY}"),
        attribution="© MapTiler © OpenStreetMap contributors",
        max_zoom=20,
        tile_size=512,
        env_required="MAPTILER_API_KEY",
        env_substitute_token="{MAPTILER_API_KEY}",
    ),

    # USGS Imagery (NAIP-backed where available). Public domain, no key.
    # CONUS land-only — open water shows as blank/black tiles. The
    # tooltip is rendered as a `title` attribute so the user is warned
    # before they wonder where the ocean went.
    BasemapSource(
        id="usgs_aerial",
        display_name="USGS Aerial",
        provider="arcgis_rest",
        value=("https://basemap.nationalmap.gov/arcgis/rest/"
               "services/USGSImageryOnly/MapServer"),
        attribution="USDA-FSA-NAIP, USGS, The National Map",
        tooltip="High-res aerial; open water shows as blank.",
    ),

    # EOX Sentinel-2 Cloudless 2024. ~10 m/pixel global cloudless mosaic,
    # CC BY-NC-SA — FishFinder is non-commercial so the licence is fine.
    # IMPORTANT: this URL uses {level}/{row}/{col} (row before col).
    # Do NOT reorder to match other XYZ providers — EOX will 404. The
    # attribution string is verbatim from EOX and must NOT be edited,
    # abbreviated, or have its URL stripped.
    BasemapSource(
        id="eox_s2_cloudless_2024",
        display_name="Sentinel-2",
        provider="xyz",
        value=("https://tiles.maps.eox.at/wmts/1.0.0/"
               "s2cloudless-2024_3857/default/g/{level}/{row}/{col}.jpg"),
        attribution=("Sentinel-2 cloudless - https://s2maps.eu by EOX IT "
                     "Services GmbH (Contains modified Copernicus Sentinel "
                     "data 2024)"),
        max_zoom=14,
    ),
)


def _to_client_dict(b: BasemapSource, resolved_value: str) -> dict:
    """Wire-format projection of one entry.

    Excludes env-var bookkeeping (`env_required`, `env_substitute_token`)
    so server-internal gating logic doesn't leak. `resolved_value` is
    the `value` field after any token substitution.
    """
    return {
        "id":          b.id,
        "display_name": b.display_name,
        "provider":    b.provider,
        "value":       resolved_value,
        "attribution": b.attribution,
        "max_zoom":    b.max_zoom,
        "tile_size":   b.tile_size,
        "tooltip":     b.tooltip,
    }


def list_basemaps() -> list:
    """Registry entries surfaced to the browser, in registry order.

    Behavior for env-gated entries:
      - If `env_required` is set and the env var is unset / empty, the
        entry is omitted from the list entirely. The browser sees no
        trace of it — no disabled button, no broken URL.
      - If the env var is set, `env_substitute_token` (if any) is
        replaced inside `value` with the env-var value before the entry
        is serialised.
    """
    out: list = []
    for b in BASEMAPS:
        if b.env_required:
            env_val = os.environ.get(b.env_required)
            if not env_val:
                continue
            value = (b.value.replace(b.env_substitute_token, env_val)
                     if b.env_substitute_token else b.value)
        else:
            value = b.value
        out.append(_to_client_dict(b, value))
    return out
