# FishFinder
 
## Basemap providers

FishFinder runs **fully keyless**: no API keys, no account-gated
services, and no calls to Esri-hosted basemaps, MapTiler, or EOX. The
ArcGIS JS API is still the map rendering engine (it's loaded as a library
from `js.arcgis.com`), but every basemap is a public-domain tile service.

The basemap registry lives in `src/basemap_sources.py` and is surfaced
to the browser via `GET /basemaps`. Two provider flavours are supported:
`arcgis_rest` (an ArcGIS REST MapServer URL, loaded as a `TileLayer`) and
`xyz` (an XYZ tile template, loaded as a `WebTileLayer`). Note that
`arcgis_rest` is just a protocol — the registered services are USGS's
own National Map endpoints, not Esri-hosted basemaps.

### USGS National Map basemaps (all keyless, public domain)

Three entries, all served from `basemap.nationalmap.gov` with no key:

- **USGS Imagery** (`USGSImageryTopo`) — aerial imagery with topographic
  labels. The default basemap. Reads shorelines, keys, and shallow reefs
  well; open ocean shows dark, where the bathymetry overlay is the point.
- **USGS Topo** (`USGSTopo`) — standard topographic map with place names.
- **USGS Aerial** (`USGSImageryOnly`, NAIP) — pure high-res aerial.
  **CONUS land-only** — open water renders as black/blank tiles. The
  button carries a tooltip warning to this effect.

Attribution shown on the map: **USGS, USDA** (basemaps) and **NOAA**
(bathymetry overlay).

## Finding a location

There is no place-name search (that required the Esri World Geocoder).
Instead, enter decimal-degree coordinates in the topbar lat/long bar (or
the **Jump to coordinates** box in the Tools sidebar) to recenter the
map — handy for divers punching in known dive-site coordinates.
