# FishFinder

A geospatial visualization tool for **bathymetric data** (sea-floor depth) of
the **Florida Keys**. The app pulls digital elevation rasters from public NOAA
ImageServer endpoints and runs a small set of focused visualization algorithms
over the depth grid (color relief, depth bands, hillshade, slope,
roughness) so the user can read sea-floor structure for navigation,
fishing, or general exploration.

The viewer is a Flask web app that serves **raw float32 elevation grids** to a
browser front-end built on the ArcGIS JS API. All colorisation happens in the
browser, in a Web Worker — the server's only job is to fetch NOAA bytes and
disk-cache them.

## What runs where

```
┌─────────────────────────────┐    /raster/...   ┌──────────────────────────┐
│ Browser main thread         │ ───────────────▶ │ Flask (app.py)           │
│  static/map.js              │                  │  src/LayerGeneration.py  │
│  • ArcGIS MapView           │                  │  • bbox math             │
│  • RasterAnalysisLayer      │                  │  • NOAA fetch + decode   │
│  • applyConfig layer-swap   │                  │  • disk cache (img/...)  │
│  • render-canvas LRU        │                  └────────┬─────────────────┘
└─────┬───────────────────────┘                           │
      │ postMessage(URL, analysisKey, param)              │ HTTPS keep-alive
      ▼                                                   ▼
┌─────────────────────────────┐                  ┌──────────────────────────┐
│ Render Web Worker            │                  │ NOAA ImageServer         │
│  static/analyses-worker.js  │                  │  exportImage / getSamples│
│  • raster cache (URL→F32)   │                  └──────────────────────────┘
│  • inflight dedupe          │
│  • analyses.js (importScripts)
│  • returns ImageBitmap      │
└─────────────────────────────┘
```

The main thread does **zero numerical work**. That's the load-bearing property
that keeps pan/zoom at 60 fps no matter how many tiles are repainting.

## Layout

```
app.py                Flask entry point. /raster, /depth, /map, /.
view_spot.py          Standalone offline previewer (no Flask). Uses the Python
                      analyses to render a hard-coded patch.
requirements.txt      Python deps (flask, numpy, scipy, tifffile, requests, …).

src/
  LayerGeneration.py  Stateless raster fetch. NOAA endpoints, Web Mercator
                      bbox math, Mercator-corrected metres-per-pixel,
                      tifffile decode, disk cache. No colourisation.
  analyses.py         Python analyses (Float32 ndarray → PIL.Image). Used by
                      view_spot.py only — kept in sync with analyses.js.
  FishFinderTools.py  Legacy palette renderer. Only view_spot.py imports it.

static/
  map.js              Live tile viewer. Per-analysis UI config, worker
                      dispatch, canvas cache, layer-swap pipeline, depth
                      lookup, search, jump-to-coords.
  analyses-worker.js  Render Web Worker. Owns the raster cache, calls
                      analyses.js, returns transferable ImageBitmaps.
  analyses.js         Bathymetric algorithms (Float32Array → RGBA).
                      Loaded into the worker via importScripts.
  style.css, main.js  UI shell.

templates/
  index.html          Dark shell + topbar (search, branding).
  map.html            Single-page viewer (panel, depth card, dialogs).

img/
  raster/<src>/<res>/<z>_<x>_<y>.tiff   NOAA float32 TIFFs. Persists.
  blank.png           Unused (kept for backwards compatibility).
```

## Pipelines

### Tile pipeline (the hot path)

1. User changes a control. `static/map.js` updates a `draft` config but does
   NOT auto-apply for major fields (analysis / source / resolution). Only
   live fields (param slider when nothing major is queued, opacity) commit
   immediately. Major changes wait for the user to press **Apply**, which
   calls `commitDraft()` → `applyConfig(cfg, "cutover")`. `applyConfig` builds
   a fresh `RasterAnalysisLayer` bound to one `(source, resolution, analysis,
   param)` tuple. In `cutover` mode the previous layer is removed *before*
   the new one is added (so the user briefly sees basemap-only and never
   sees the old dataset masquerading as the new). In `overlap` mode (used
   only for live param drags — same data, same algorithm) the new layer
   sits on top of the old until its tiles arrive.
2. ArcGIS calls `RasterAnalysisLayer.fetchTile(level, row, col, options)`
   for each visible tile. The layer constructs the raster URL and calls
   `getRenderedCanvas(url, analysis, param, options.signal)`. The signal
   is the abort handle for that specific tile request — when ArcGIS
   abandons a slot (mid-pan), it fires; we reject the fetchTile promise
   immediately so ArcGIS can redraw without waiting on dead work. The
   underlying worker render keeps running and the canvas still lands in
   the LRU, ready for the next request.
3. `getRenderedCanvas` first checks the **render-canvas LRU** keyed by
   `(url, analysis, param)`. Hit → return the cached `<canvas>` immediately.
   Miss → check the **per-key in-flight map** (so ArcGIS double-requesting
   the same slot only triggers one worker render). Miss → `postMessage`
   to the worker, wrapped in a 20s timeout.
4. Worker checks its **raster cache** (URL → Float32 grid). Hit → skip the
   network. Miss → fetch `/raster/...` (deduped by URL through `inflight`).
5. Server (`app.py:serve_raster` → `LayerGeneration.fetch_tile_raster`)
   computes the Web Mercator bbox expanded by `BUFFER_PX=16`, fetches
   `(res+32)×(res+32)` float32 from NOAA, disk-caches the TIFF under
   `img/raster/<source>/<res>/`, decodes with `tifffile`, replaces sentinel
   nodata with NaN, computes the **Mercator-corrected** cellsize_m, and
   returns the binary blob (16-byte header + body).
6. Worker decodes the header, copies elev into a reusable scratch
   `Float32Array`, runs the analysis (`self.FFAnalyses[key]`) into a scratch
   `Uint8ClampedArray`, punches alpha=0 on nodata pixels, then crops the
   buffer margin via `createImageBitmap(imageData, buf, buf, res, res)`.
   Posts the bitmap back as a transferable.
7. Main thread paints the bitmap into a `<canvas>`, stores it in the LRU,
   returns it to ArcGIS. `LayerView.updating` flips false; the layer-swap
   logic promotes the pending layer to current and removes the previous.

### Depth lookup pipeline (shares the tile raster)

User clicks → `static/map.js:lookupDepth` computes the (z, x, y) tile +
sub-tile fraction for the click using the COMMITTED (source, resolution)
→ `postMessage({type:'sample', url, fracX, fracY})` to the worker → the
worker reuses `fetchRaster` (cache hit on visible tiles, otherwise the
same `/raster/...` fetch the renderer uses) and returns the float32
elevation value at that pixel.

This is deliberately NOT a separate NOAA endpoint. An earlier version
called NOAA's `identify`/`getSamples` with a `pixelSize` hint derived from
view zoom; NOAA's mosaic rule resolves to *different sub-rasters of the
source mosaic* depending on `pixelSize`, so the same click at different
zoom levels returned wildly different depths (often hundreds of feet
off — coarse CRM at z=8 vs. high-res multibeam at z=14). Sampling the
already-rendered grid removes the mosaic-rule ambiguity entirely: the
value the user reads is exactly the value that produced the colored
pixel they clicked on.

### Tiered prefetch

When the view is stationary, `static/map.js:runPrefetch` warms the
canvas LRU with tiles the user is likely to need next. Five tiers, each
with its own budget (so a high-count tier can't crowd out a low-count
one) and sorted by distance to view centre:

1. **Same-zoom 1-tile ring** around the visible extent — covers the
   most common move (a small pan into adjacent geography). Highest
   priority because there's no built-in fallback for "tile just outside
   what's loaded": ArcGIS shows basemap until the new tile arrives.
2. **zoom + 1** — child tiles. Covers zoom-in.
3. **zoom - 1, - 2, - 3** — parent tiles. ArcGIS's stretched-parent
   fallback is what fills the gap while finer tiles load; if no
   parent is cached, the slot goes to basemap. Three coarse levels
   means *some* parent is always available no matter how fast the
   user is zooming in.

Sequential, not parallel: the render worker is single-threaded, and a
parallel flood would queue *ahead* of any user-issued `fetchTile` and
make pan/zoom transitions visibly slower. A `prefetchToken` is bumped
on every cancel (`view.stationary` going false, or commit/live-param),
causing the loop to bail on the next iteration without losing the
already-rendered tiles that landed in the cache.

### Worker resilience

The render worker is wrapped in a restartable factory in
`static/map.js`. On `error` / `messageerror`, every entry in the
`pending` map is resolved as `'error'` (so callers — fetchTile,
sample — return the dark blank and let ArcGIS move on), then a fresh
worker is spawned. Without this, a single worker crash left every
in-flight tile request dangling forever, which ArcGIS reads as
"still loading" — the slot never gets a canvas and the basemap
shows through indefinitely.

### Apply pipeline (commit / draft / cutover)

The viewer maintains two configs in `static/map.js`:
- `committed` — the config whose layer is on screen (or being loaded after
  Apply was pressed and its draft was claimed).
- `draft` — what the user is currently editing.

Major controls (analysis / source / resolution) update `draft` only. Their
visible "Pending" tag and the highlighted Apply button surface the diff
against `committed`. Apply calls `commitDraft()`, which copies draft to
committed and runs `applyConfig(cfg, "cutover")`.

The param slider commits live (and runs in `overlap` mode) **only if**
`isDirtyMajor()` is false — i.e. nothing else is queued. Otherwise it joins
the queue: previewing the new param against the OLD analysis would be the
same misrepresentation the cutover rule exists to prevent. Opacity is the
only true compositor-only control: it mutates `currentLayer.opacity` and
`pendingLayer.opacity` in place.

### Layer-swap invariants

`static/map.js` keeps three pieces of state:
- `currentLayer` — the layer presently displayed.
- `pendingLayer` — the freshest in-flight layer.
- `pendingApplyId` — monotonic counter, bumped on every `applyConfig()`.

Rules:
1. **Cutover mode** (Apply button): `currentLayer` is removed *before* the
   new layer is added. Brief basemap-only window > misrepresentation.
2. **Overlap mode** (live param): `currentLayer` keeps painting until the
   new layer's `LayerView.updating` clears. Safe because the new layer is
   the same data + algorithm with a different render parameter.
3. Only one pending swap exists at a time. A new `applyConfig()` tears down
   the previous pending immediately so we don't accumulate dead layers.
4. A pending layer only promotes to current if its `applyId` is still the
   latest. Stale swaps are torn down silently when their `updating` clears.
5. The marker layer (`GraphicsLayer` for depth-click pins) sits at a fixed
   index above tile layers, so swaps never hide markers.

This pattern replaces the old `layer.refresh()` approach, which left stale
canvases on screen because `BaseTileLayer.refresh()` does not reliably
re-issue `fetchTile` for slots already in its mosaic cache.

## Wire formats

### `/raster/<source>/<res>/{z}/{x}/{y}.bin`

Little-endian, 16-byte header followed by `w*h` float32 body.

```
u32   width      = max(OUTPUT_TILE_PX, res) + 2*BUFFER_PX   (== height)
u32   height
f32   cellsize_m = true ground sample distance (Mercator-corrected)
u32   buffer_px  = pixels of overdraw on every edge (client crops)
…body…  width*height float32, row-major. NaN = nodata.
```

`Cache-Control: public, max-age=86400` — the browser HTTP cache handles
revisits; the worker raster cache handles same-session reuse without re-
parsing the bytes.

### Worker message protocol

```js
// main → worker
{ type: 'render', id, url, analysisKey, param }

// main → worker (cancel a stale in-flight render so it skips the encode and
// stops queueing ahead of the tiles the user actually needs). Two triggers:
//   • cutover layer switch — cancelInflightRenders() flushes every in-flight
//     render (the layer they belonged to is gone, keys are now wrong).
//   • stale-ZOOM tile abort — getRenderedCanvas cancels an aborted tile whose
//     level no longer matches the current zoom (the user zoomed past it). A
//     SAME-level abort is a pan and is left running to warm the LRU.
{ type: 'cancel', id }

// worker → main (success — bitmap is transferable)
{ type: 'rendered', id, bitmap, size }

// worker → main (HTTP 204 — NOAA confirms no coverage; main thread
// caches a blank canvas under this URL/analysis/param key)
{ type: 'empty', id }

// worker → main (anything else — main thread does NOT cache. The next
// pan/zoom retries; transient failures don't poison the cache.)
{ type: 'error', id, message }
```

`id` is a monotonic integer assigned by the main thread; `pending` map
correlates replies. Many tiles can be in flight simultaneously without
crossing wires.

## Caching layers

| Where             | Key                           | Lifetime         | Eviction           |
| ----------------- | ----------------------------- | ---------------- | ------------------ |
| Browser HTTP      | URL                           | 1 day            | Browser policy     |
| Render-canvas LRU | `(url, analysis, param)`      | Page lifetime    | LRU @ 768 entries  |
| Worker raster LRU | URL                           | Page lifetime    | LRU @ 256 entries  |
| Server disk cache | `(source, res, z, x, y)`      | Forever          | Manual / disk full |

The render-canvas cache is what makes slider drags feel instant: most drag
positions repeat within a few seconds, so the second visit is one Map lookup.

## Concurrency

- **Browser main thread**: pure orchestration. ArcGIS pan/zoom + DOM events
  + worker postMessage. Should never block on render math.
- **Render worker**: single-threaded JS event loop. Renders one tile at a
  time, but raster fetches are async so multiple tiles' fetches overlap.
- **Server**: Flask in `threaded=True` mode. NOAA outbound calls are bounded
  by `NOAA_CONCURRENCY=6` (a `BoundedSemaphore` in `LayerGeneration.py`).
- **HTTP keep-alive**: a module-level `requests.Session` with an
  `HTTPAdapter` pool sized to `NOAA_CONCURRENCY*2` so the semaphore is the
  limit, not connection setup. Saves ~100-200 ms TLS handshake per fetch
  after the first.

## Performance budget (rough, modern laptop)

- Worker render of a buffered 288×288 tile: ~3-15 ms depending on analysis.
- 12 visible tiles, all cache-miss, all unique: ~50-100 ms total worker
  time. Main thread free throughout.
- Repeated slider value: render-canvas cache hit ≈ 0 ms.
- Cold NOAA fetch: 200-800 ms (TLS + NOAA latency). Subsequent: 80-300 ms.

If you find yourself adding work to the main thread, stop. Push it into the
worker.

## Analyses

JS implementations live in `static/analyses.js` (run in the worker), Python
copies in `src/analyses.py` (used by `view_spot.py`). Same algorithms; keep
them visually aligned if you change either side.

| key             | what it shows                                       | param meaning                  |
| --------------- | --------------------------------------------------- | ------------------------------ |
| `color-relief`  | depth coloring + hillshade overlay (default)        | vertical exaggeration (×)      |
| `hillshade`     | pure greyscale shaded relief                        | vertical exaggeration (×)      |
| `roughness`     | high-pass detail; bright = wrecks, ledges, rubble   | feature scale (m)              |
| `slope`         | true slope angle (degrees)                          | max slope on color scale (deg) |
| `depth`         | smooth viridis depth gradient                       | max depth shown (ft)           |
| `depth-bands`   | discrete depth bands with black contour lines       | band size (ft)                 |

`color-relief` also takes a `paramExtra = {minDepthFt, maxDepthFt}` that
rescales the depth-to-colour mapping so deeper water doesn't all saturate to
dark blue. Other analyses ignore `paramExtra`.

Signatures:
- JS: `(elev, nodataMask, w, h, cellsize_m, param, outRgba, paramExtra?) → void`
- Python: `(elev_m, cellsize_m, param, **kwargs) → PIL.Image`

`cellsize_m` is the **true** ground sample distance (Mercator-stretch
corrected via centre latitude), so analyses can use real physical units —
slope-in-degrees, feature-scale-in-metres, etc. — and stay consistent across
zoom levels.

## Data sources

The source registry is the `SOURCES` tuple in **`src/data_sources.py`** (each
entry a frozen `DataSource` dataclass) — `src/LayerGeneration.py` and
`templates/map.html` both read from it via `get_source` / `/sources`. Current
registry (default `dem-all`): `dem-all`, `bag-bathymetry`, `nos-mbab`,
`dem-tiles`, `crm-mosaic`, `multibeam`, `dem-global`. All requested at
`pixelType=F32` in EPSG:3857, ordered by usefulness for the Keys.

All seven were re-verified **live** against NOAA on 2026-06-07 by
`tools/raster_diagnostics.py` (6 Keys regions × zooms 9–15): every source is
reachable and returns valid F32 with a wire-format that round-trips to the
worker; coverage gaps render correctly as no-data. Run that tool after any
registry edit. `tools/raster_pipeline_test.py` covers the decode/cache/format
invariants offline (no network).

NOAA fetches retry transient failures (5xx / 429 / connection error) with
bounded exponential backoff and honor a capped `Retry-After`; deterministic
failures (4xx, 200-non-image) are not retried. See
`_fetch_raster_bytes` + the `NOAA_*` constants in `src/LayerGeneration.py`.
`NOAA_CONCURRENCY=6` is empirically optimal — raising it makes NOAA throttle
per-request latency and *worsens* wall time.

## Adding things

**New analysis** (touches 3-4 files):
1. Implement in `static/analyses.js`. Signature
   `(elev, nodataMask, w, h, cellsize, param, outRgba)`. Register in the
   `self.FFAnalyses` map at the bottom.
2. (Optional) Mirror in `src/analyses.py` if you want `view_spot.py` to
   support it. Register in the `ANALYSES` dict at the bottom.
3. Add an `<option>` in the visualization dropdown in `templates/map.html`.
4. Add a config entry in the `ANALYSES` map at the top of `static/map.js`
   (slider label, range, default, hint, intro). The slider auto-rebounds.

**New data source** (touches 1-2 files):
1. Add a `DataSource(...)` entry to the `SOURCES` tuple in
   `src/data_sources.py` (URL + the source's specific nodata sentinel; set
   `rendering_rule=_RAW_PIXELS` if the service's default raster function is a
   colour hillshade, or it returns RGB and the decoder rejects it). The
   dropdown is populated from `/sources`, so no template edit is needed unless
   you want a hand-authored `<option>`.
2. Verify it live with `python tools/raster_diagnostics.py --source <id>`.

**New layer-construction path**: funnel through `applyConfig()` (or
`scheduleApply()` for high-frequency events). Bypassing it means
re-implementing the layer-swap invariants — almost certainly wrong.

## Running it

`app.py` refuses to start unless `SECRET_KEY` is set in the environment (it
signs the Flask session cookie — no hardcoded or per-process-random fallback,
by design). Generate one and set it before launching:

```
pip install -r requirements.txt

# PowerShell
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
# bash
export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')

python app.py     # http://localhost:8080/
```

`view_spot.py` runs `color_relief` on a hard-coded Keys patch without
touching Flask — useful for previewing an area or trying a single algorithm.

## Known quirks

- **Nodata sentinel coverage** (resolved): `_decode_raster` masks `|v| ≥ 11000`
  *plus* the source's exact `nodata` sentinel from the `SOURCES` registry, so
  the literal `-9999` that `dem-tiles`/`dem-all`/`crm`/`dem-global` use for
  no-coverage now round-trips to NaN instead of rendering as deepest blue.
  Verified by `tools/raster_pipeline_test.py::test_nodata_sentinel_roundtrips_to_nan`
  and live (0% spurious deep-blue in the 2026-06 diagnostics sweep). The
  off-size-resize edge case is also handled: `_decode_raster` now builds the
  nodata mask on the original grid, neutralises sentinels before the bilinear
  data resize, and resamples the mask with NEAREST — so an edge-of-coverage
  off-size response can no longer smear -9999 into fake mid-range depths
  (`test_offsize_resize_does_not_smear_sentinel_into_fake_depth`).
- **Worker raster cache is in-memory only**: a hard reload is the only way
  to invalidate it. There is no /reset endpoint.
- **Disk raster cache never expires**: if NOAA changes upstream coverage,
  delete `img/raster/<source>/` to force a refetch.
- **Only data-bearing rasters are persisted**: `_load_or_fetch` writes a
  tile to disk only if it decodes AND has at least one non-nodata sample,
  and it ignores an all-nodata file already on disk (treats it as a miss).
  This fixes the "tiles randomly render as opaque grey boxes, worse after a
  broad/global zoom-out then zoom-in, accumulating over a session" bug. Root
  cause: a single transient NOAA response (an all-nodata grid for a tile that
  really has data — more likely under the request bursts a broad zoom-out
  triggers) used to be written to the never-expiring disk cache, after which
  every future visit read it back as no-coverage; the worker flagged it
  `empty` and the client painted a permanent opaque blank over that slot.
  `serve_raster` also sends `Cache-Control: no-store` for an all-nodata tile
  (data tiles keep the 1-day cache) so the browser HTTP cache can't hold a
  transient empty across reloads either. Net effect: empties stay retryable
  at every persistent cache layer; genuine no-coverage simply re-fetches.
- **Resolution slider applies on release** (`change`), not during drag —
  every intermediate value would otherwise force a fresh raster fetch
  series. The label still updates live.
- **Per-source LOD cap on switch** (resolved): `buildLayer` caps the tile
  layer's `numLODs` at `min(BATHY_MAX_ZOOM, source.max_zoom)` (read from
  `/sources` via `sourcesById`), NOT a flat `BATHY_MAX_ZOOM`. Coarse sources
  stop short of z20 (`crm` z15, `multibeam` z14, `dem-global` z11); if the
  layer advertised LODs past a source's `max_zoom`, ArcGIS would call
  `fetchTile` for levels the server `503`s (`z > source.max_zoom` in
  `fetch_tile_raster`, covered by
  `raster_pipeline_test.py::test_zoom_out_of_source_range_returns_none`), the
  worker reports `error`, and the slot stays blank — the "switch source →
  blank until you zoom out below its ceiling" bug. Capping the LODs makes
  ArcGIS over-zoom the source's deepest real tile instead. `depthSampleParams`
  clamps the sampled `z` to the same ceiling so the depth readout doesn't
  `503` into a spurious "No data" on an over-zoomed coarse source.
- **Stationary tile-abort completes instead of rejecting** (resolved):
  `getRenderedCanvas`'s abort handler only rejects same-level aborts while the
  view is *moving* (`viewIsStationary` false → a pan; reject fast to keep
  ArcGIS's pending queue short). A same-level abort while the view is *at rest*
  is a layer-swap re-evaluation (a source/analysis/resolution cutover), not a
  pan: ArcGIS will not re-issue `fetchTile` on its own because the needed-tile
  set never changed, so rejecting left a freshly switched source blank until a
  manual pan/zoom. The handler now lets the in-flight render resolve the tile
  in place. (A same-source Reload dodged this because its visible tiles are
  canvas-LRU hits — no render, no abort window; only a *fresh* source exposes
  it.) Stale-ZOOM aborts (different level) still cancel + evict as before.
