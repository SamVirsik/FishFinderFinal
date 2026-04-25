# FishFinder

A geospatial visualization tool for **bathymetric data** (sea-floor depth) of the
**Florida Keys**. The app pulls digital elevation / bathymetry rasters from public
NOAA ImageServer endpoints, then runs a variety of visualization algorithms over
the depth grid (heatmaps, hillshades, contour maps, slope, flow exposure, etc.)
to help the user better understand sea-floor structure for navigation, fishing
spot identification, and general exploration.

The visualization runs as a Flask web app that serves map tiles to a browser
front-end built on the ArcGIS JS API. Tiles are generated on demand by
downloading the underlying raster from NOAA, running the chosen analysis, and
slicing the result into the requested zoom / row / column tile.

## Layout

```
app.py                Flask entry point. Tile server + reload-layer endpoint + depth-sample API.
view_spot.py          Standalone script: download a small DEM patch, render it offline with one analysis.
requirements.txt      Python dependencies.
src/
  LayerGeneration.py  LayerGenerator class. Knows the NOAA endpoints, downloads rasters,
                      and dispatches to analyses.py to build the colored image.
  analyses.py         All visualization algorithms (heatmap, hillshade, contour,
                      slope, flow exposure, spot finder, etc.). Each takes a depth
                      DataFrame + width param and returns a PIL Image.
  FishFinderTools.py  Older standalone helpers (legacy palette renderer, used by view_spot.py).
templates/            Jinja templates: index, map, about_us.
static/               CSS / JS / images for the web UI. map.js drives the map and reload calls.
img/
  blank.png           Served when NOAA returns no data for a tile.
  noaa/               Runtime cache of downloaded NOAA tiles (created on demand).
  tile/               Runtime cache of generated PNG tiles (created/cleared per layer reload).
```

## Pipeline (high level)

1. Browser loads `/map` -> ArcGIS JS map. User picks an analysis, data source,
   resolution, smoothness, and width in the sidebar; clicks **Reload**.
2. Front-end calls `/reload-layer/<bounds>_<res>_<analysis>_<smoothness>_<width>_<source>`.
   `app.py:reload_layer` configures the singleton `LayerGenerator` and clears
   the tile cache.
3. As the user pans/zooms, the map requests `/tile/<level>_<row>_<col>`.
   `app.py:serve_tile` either returns a cached PNG or calls `generate_tile`,
   which:
     - converts the tile coordinates to a lat/lon bbox,
     - asks `LayerGenerator.load_data` to fetch the raster from the chosen
       NOAA endpoint (`_source_spec` picks the URL + params),
     - dispatches to the matching function in `analyses.py` to colorize it,
     - saves the PNG, then crops it to the requested sub-tile.
4. `/depth/<lat>/<lon>` returns the depth in meters at a single point by
   hitting NOAA's `getSamples` endpoint for the active data source.

## Analyses

Defined in `src/analyses.py` and selected in the sidebar dropdown
(`templates/map.html`). Currently exposed: `heatmap`, `heatmap-granular`,
`contour`, `hillshade`, `colored-hillshade`, `texture-shade`, `flow-exposure`,
`slope-magnitude`, `spot-finder`. Each takes the depth grid as a pandas
DataFrame plus a `width` parameter that controls the visual scale of features.

## Data sources

Selectable in the sidebar; mapped to NOAA endpoints in
`LayerGenerator._source_spec`. Includes: `dem-tiles` (default DEM mosaic),
`bag-bathymetry`, `multibeam`, `crm-mosaic`, `dem-all`, `dem-global`, and
`fknms-multibeam` (Florida Keys 2-5 m DEM).

## Running it

```
pip install -r requirements.txt
python app.py     # http://localhost:8080/
```

`view_spot.py` runs the colored-hillshade analysis on a hard-coded Keys patch
without touching Flask -- useful for quickly previewing an area or trying a
single algorithm.

## Notes for future edits

- `img/tile/` and `img/noaa/<analysis>/` are caches; safe to delete at any time.
  `app.py` clears them on startup and shutdown.
- A Flask filesystem session lives in `flask_session/` once the app runs;
  also safe to delete.
- The main analysis dispatch is the `ANALYSES` dict at the bottom of
  `src/analyses.py`. When adding a new analysis: add the function (signature
  `(df, width, **kwargs) -> PIL.Image`), register it in `ANALYSES`, and add an
  `<option>` in `templates/map.html`.
- When adding a new data source: add a branch in `_source_spec` in
  `src/LayerGeneration.py` and an `<option>` in the data-source dropdown in
  `templates/map.html`.
