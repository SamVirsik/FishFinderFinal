# FishFinder
 
## Basemap providers

The basemap registry lives in `src/basemap_sources.py` and is surfaced
to the browser via `GET /basemaps`. Three categories of provider are
supported: built-in Esri basemaps (default), arbitrary XYZ tile
templates, and ArcGIS REST MapServer endpoints.

### MapTiler Satellite

To enable the MapTiler Satellite basemap, set the `MAPTILER_API_KEY`
environment variable before starting the server:

```bash
export MAPTILER_API_KEY=your_key_here   # macOS / Linux
$env:MAPTILER_API_KEY = "your_key_here" # PowerShell
python app.py
```

If the variable is unset, the MapTiler button is omitted from the grid
entirely — no broken button, no placeholder URL.

**The key ships to the browser** with every tile request (this is
unavoidable for client-direct tile fetches). Domain-restrict the key in
the [MapTiler Cloud dashboard](https://cloud.maptiler.com/) so it can
only be used from your deployment hostname(s).

The MapTiler free tier is **100,000 tile requests + 5,000 map sessions
per month**, whichever cap is hit first. MapTiler hard-pauses the key
at the cap rather than auto-billing.

### Sentinel-2 Cloudless 2024 (EOX)

Free, no API key. **CC BY-NC-SA 4.0** — non-commercial use only.
FishFinder is a non-commercial personal/fishing tool, so this is fine
for the intended use case; if you fork it for commercial distribution
you must remove this basemap entry or negotiate licensing separately
with EOX.

### USGS Aerial (NAIP)

Free, no API key, public domain. **CONUS land-only** — open water
renders as black/blank tiles. The button carries a tooltip warning to
this effect.
