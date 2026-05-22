// FishFinder live tile viewer.
//
// Architecture (post-rework — read this before touching anything):
//
// TILE FETCH PIPELINE:
//   ArcGIS `fetchTile(level, row, col, options)` → `getRenderedCanvas`,
//   which: (a) checks the canvas LRU, (b) shares an in-flight promise
//   for the same key (dedup — ArcGIS occasionally double-requests a
//   slot during fast pans, and we don't want two competing renders),
//   (c) honors `options.signal` so aborted tile requests free up the
//   slot in ArcGIS immediately even though the worker render keeps
//   running and will populate the cache for next time, and (d) wraps
//   the worker call in a 20s timeout so a wedged render can never
//   leave the slot in a permanent "loading" state.
//
//   The worker is a restartable singleton: if it crashes, every
//   pending request is resolved as 'error' (so ArcGIS can move on)
//   and a fresh worker takes its place. Without this, a single worker
//   fault froze the page until reload.
//
// TIERED PREFETCH (warm adjacent geometry while idle):
//   When the view is stationary, we speculatively render tiles in five
//   tiers, each with its own budget and sorted by distance to view
//   centre:
//     1. Same-zoom 1-tile ring around the visible extent (covers pans)
//     2. zoom + 1 (covers zoom-in)
//     3. zoom - 1, - 2, - 3 (parent tiles for ArcGIS's stretched-
//        parent fallback, so when the user zooms in fast there's
//        always *some* parent to draw — never bare basemap)
//   Sequential because the worker is single-threaded; cancelled the
//   instant the view starts moving so user-issued tile requests don't
//   queue behind speculative work.
//
// CONTROLS ARE SPLIT INTO TWO TIERS:
//
//   * MAJOR (Visualization, Source, Resolution): change what data the user
//     is looking at, or which algorithm interprets it. These are queued
//     into a `draft` config and committed only when the user presses
//     Apply. Pressing Apply does a HARD CUTOVER: the old tile layer is
//     removed BEFORE the new one starts loading, so the user briefly sees
//     basemap-only and then the new tiles fade in. They never see the old
//     dataset/algorithm masquerading as the new one. (This is the central
//     correctness rule of the rework — misrepresentation > flicker.)
//
//   * LIVE (Param slider, Opacity): can update without Apply.
//     - Opacity mutates compositor properties on the live layer pair —
//       zero recompute.
//     - Param re-renders the SAME raster with a new param. Because the
//       data and algorithm are unchanged, an overlap swap is safe (the
//       old param-rendered tiles fade out as new ones come in). However,
//       if any major change is also queued in the draft, the param
//       slider waits for Apply too — otherwise the screen would briefly
//       show "the new param applied to the old algorithm" which is the
//       same misrepresentation we just promised to avoid.
//
// LAYER LIFECYCLE:
//   - `currentLayer`: the layer currently rendered to the screen.
//   - `pendingLayer`: the freshest in-flight layer, sitting above current
//     during overlap mode. Promoted to current on `LayerView.updating:
//     false`.
//   - `pendingApplyId`: monotonic counter, bumped on every layer build.
//     Stale swaps (an older config whose load completes after a newer one
//     has been requested) are silently torn down rather than promoted.
//
// FAILURE HANDLING:
//   - The worker reports `'empty'` when NOAA legitimately has no coverage
//     (raster decoded but every pixel is the nodata sentinel); we cache
//     the opaque dark blank canvas for that key so the basemap doesn't
//     show through the slot. Transient fetch errors (server-side 5xx,
//     network blip, decode failure) come back as `'error'` and are NEVER
//     cached, so the next pan/zoom retries — fixes the "tile randomly
//     stays blank / basemap leaks through forever" symptom that was
//     happening when a single NOAA blip during initial load got cached
//     as a permanent empty under HTTP 204.

const baseurl = window.location.origin;


// ─── Bathymetry-source registry (one source of truth) ──────────
// Started at module load so it runs in parallel with the ArcGIS
// `require([...])` dependency load — by the time the require callback
// is ready to populate the dropdown the JSON has usually already
// landed. The dropdown is rendered empty in `templates/map.html` and
// filled from this response; we also stash per-source min/max zoom so
// the prefetch loop can clamp tiers to what each source actually
// supports (a hardcoded `zoom + 1 <= 23` used to issue guaranteed-503
// prefetch requests at zoom 22).
const _sourcesReady = fetch(`${baseurl}/sources`, { cache: 'no-store' })
    .then(r => r.ok ? r.json()
                    : Promise.reject(new Error(`/sources HTTP ${r.status}`)))
    .catch(err => {
        console.error('[FishFinder] failed to load /sources:', err);
        // Hard-fail open: empty list means the dropdown stays in
        // "Loading sources…" state forever, which is the right UX
        // signal that the backend registry is broken — better than
        // silently inventing a fallback that masks the failure.
        return { default: null, sources: [] };
    });


// ─── Basemap registry (one source of truth, mirrors /sources) ──
// Also kicked off at module load so it overlaps the AMD require()
// dependency download. Failure-mode is different from /sources: the
// basemap grid is purely cosmetic — losing it must not leave the user
// without basemap controls. We fall back to a hardcoded six-Esri list
// so the switcher remains useful even if the Flask backend is dead.
const _BASEMAP_FALLBACK = {
    basemaps: [
        { id: "dark-gray-vector", display_name: "Dark",        provider: "esri", value: "dark-gray-vector", attribution: "", max_zoom: null, tile_size: null, tooltip: null },
        { id: "streets-vector",   display_name: "Streets",     provider: "esri", value: "streets-vector",   attribution: "", max_zoom: null, tile_size: null, tooltip: null },
        { id: "satellite",        display_name: "Satellite",   provider: "esri", value: "satellite",        attribution: "", max_zoom: null, tile_size: null, tooltip: null },
        { id: "hybrid",           display_name: "Hybrid",      provider: "esri", value: "hybrid",           attribution: "", max_zoom: null, tile_size: null, tooltip: null },
        { id: "oceans",           display_name: "Oceans",      provider: "esri", value: "oceans",           attribution: "", max_zoom: null, tile_size: null, tooltip: null },
        { id: "topo-vector",      display_name: "Topographic", provider: "esri", value: "topo-vector",      attribution: "", max_zoom: null, tile_size: null, tooltip: null },
    ],
};
const _basemapsReady = fetch(`${baseurl}/basemaps`, { cache: 'no-store' })
    .then(r => r.ok ? r.json()
                    : Promise.reject(new Error(`/basemaps HTTP ${r.status}`)))
    .catch(err => {
        console.warn('[FishFinder] /basemaps unavailable, using Esri fallback:', err);
        return _BASEMAP_FALLBACK;
    });


// ─── Browser-presence heartbeat ─────────────────────────────────
// The Python server self-terminates ~3s after the last ping, so closing
// the tab kills the backing process automatically. Uses sendBeacon
// rather than fetch so the ping rides a separate background channel —
// regular fetch heartbeats compete for the per-origin HTTP/1.1
// connection pool with the worker's tile fetches, and under panning
// load that contention was causing tile fetches to occasionally stall
// (= 'error' = blank canvas = basemap visible through the slot).
// sendBeacon returns true/false synchronously and never throws.
const HEARTBEAT_INTERVAL_MS = 1000;
const _heartbeatUrl = `${window.location.origin}/heartbeat`;
function _pingHeartbeat() {
    if (navigator.sendBeacon) navigator.sendBeacon(_heartbeatUrl);
    else fetch(_heartbeatUrl, { method: 'POST', keepalive: true }).catch(() => {});
}
_pingHeartbeat();
setInterval(_pingHeartbeat, HEARTBEAT_INTERVAL_MS);


// ─── Per-analysis UI config ─────────────────────────────────────
// The slider re-labels and re-bounds itself per analysis. The integer
// `param` value is what the analysis functions actually consume.
const ANALYSES = {
    "color-relief": {
        label: "Vertical exaggeration", unit: "×",
        min: 1, max: 30, step: 1, default: 5,
        hint: "Higher = more dramatic shading. Best general view.",
        intro: "Coloured depth with overlaid hillshade. Default starting view.",
        pretty: "Color Relief",
    },
    "hillshade": {
        label: "Vertical exaggeration", unit: "×",
        min: 1, max: 30, step: 1, default: 5,
        hint: "Higher = more contrast between flat and steep ground.",
        intro: "Pure greyscale shaded relief. Reveals structure without colour.",
        pretty: "Hillshade",
    },
    "roughness": {
        label: "Feature scale", unit: " m",
        min: 5, max: 500, step: 5, default: 50,
        hint: "Size of the features you want to highlight. Smaller = finer texture.",
        intro: "High-pass detail. Bright = rough (wrecks, ledges, rubble).",
        pretty: "Roughness",
    },
    "slope": {
        label: "Max slope on scale", unit: "°",
        min: 5, max: 90, step: 1, default: 30,
        hint: "Smaller value exaggerates subtle slopes; larger smooths them.",
        intro: "True slope angle. Cool = flat, hot = steep, purple = vertical.",
        pretty: "Slope",
    },
    "depth": {
        label: "Max depth shown", unit: " ft",
        min: 30, max: 3000, step: 10, default: 300,
        hint: "Pixels deeper than this saturate to the deepest colour.",
        intro: "Continuous depth gradient. Good for seeing overall basin shape.",
        pretty: "Depth Map",
    },
    "depth-bands": {
        label: "Band size", unit: " ft",
        min: 1, max: 100, step: 1, default: 10,
        hint: "Width of each colour band. Small = many lines, lots of detail.",
        intro: "Stepped colour bands with crisp contour lines on every edge.",
        pretty: "Depth Bands",
    },
};

// Color Relief's depth-range control. Default range covers typical coastal
// use; absolute bounds clamp to a sensible ceiling (1500 ft ≈ 460 m, which
// is comfortably deeper than the Florida Straits but well shy of dem-global
// abyssal depths).
const COLOR_RELIEF_DEPTH_RANGE = {
    minBound: 0,
    maxBound: 1500,
    step:     5,
    defaultMin: 0,
    defaultMax: 300,
};


// ─── Render worker (restartable singleton) ──────────────────────
//
// One worker handles all render + sample requests serially. We hold it
// behind a factory so that if the browser kills it (uncaught error,
// memory pressure on deep zoom, etc.) we can spin up a fresh one and
// resolve every dangling request as 'error'. Without this, a single
// worker crash leaves every in-flight fetchTile() promise unresolved
// forever — ArcGIS waits indefinitely, the slot stays blank, and the
// user sees basemap leaking through with no recovery short of a reload.
let renderWorker;
const pending = new Map();
let nextRequestId = 0;

function onWorkerMessage(ev) {
    const { type, id } = ev.data;
    const slot = pending.get(id);
    if (!slot) {
        // Late arrival: the request was already abandoned (timed out, worker
        // reset, or message races past terminate). The bitmap holds GPU
        // memory that would otherwise sit until a JS GC fires AND the
        // browser's GPU process gets the finalizer message — under sustained
        // toggling that pile-up of orphan bitmaps is what eventually starves
        // createImageBitmap, which then starts returning 'error', which
        // ArcGIS draws as the dark blank canvas. Closing here breaks the
        // feedback loop.
        if (type === 'rendered' && ev.data.bitmap && ev.data.bitmap.close) {
            try { ev.data.bitmap.close(); } catch { /* nothing to do */ }
        }
        return;
    }
    pending.delete(id);
    if (type === 'rendered') {
        slot.resolve({ status: 'ok', bitmap: ev.data.bitmap, size: ev.data.size });
    } else if (type === 'empty') {
        slot.resolve({ status: 'empty' });
    } else if (type === 'sampled') {
        slot.resolve({ status: 'sampled', value: ev.data.value });
    } else {
        slot.resolve({ status: 'error' });
    }
}

// Tear down + respawn. Drains every in-flight `pending` entry as
// 'error' before terminating so callers see a clean transient failure
// (and will retry on the next ArcGIS request) instead of a dangling
// promise. Used by both the crash recovery path and the manual reload
// button — same operation, different trigger.
function resetWorker() {
    try { renderWorker.terminate(); } catch { /* already gone */ }
    for (const slot of pending.values()) slot.resolve({ status: 'error' });
    pending.clear();
    renderWorker = makeWorker();
}

function onWorkerError(ev) {
    console.error('[worker] crashed, restarting:', ev.message || ev);
    resetWorker();
}

function makeWorker() {
    const w = new Worker(`${baseurl}/static/analyses-worker.js`);
    w.addEventListener('message', onWorkerMessage);
    w.addEventListener('error', onWorkerError);
    w.addEventListener('messageerror', onWorkerError);
    return w;
}
renderWorker = makeWorker();

// Returns { promise, id } — the id lets the caller drop the pending slot
// if it gives up before the worker responds (timeout), so the late
// response lands in onWorkerMessage's late-arrival branch and its bitmap
// gets explicitly closed instead of pinning GPU memory until GC.
//
// `paramExtra` carries analysis-specific structured parameters that don't
// fit the single-scalar `param` (e.g. Color Relief's min/max depth). The
// worker forwards it verbatim to the analysis function; for analyses that
// don't use it the field is undefined and ignored.
function requestRender(url, analysisKey, param, paramExtra) {
    const id = ++nextRequestId;
    const promise = new Promise((resolve) => {
        pending.set(id, { resolve });
        renderWorker.postMessage({
            type: 'render', id, url, analysisKey, param, paramExtra,
        });
    });
    return { promise, id };
}

// Encode paramExtra into the cache key. Same extras → same canvas; differing
// extras → fresh render. Order of keys matters for stringify; we sort to
// keep the encoding deterministic regardless of object construction order.
function paramExtraKey(paramExtra) {
    if (!paramExtra) return '';
    const keys = Object.keys(paramExtra).sort();
    return keys.map(k => `${k}=${paramExtra[k]}`).join(',');
}

function requestSample(url, fracX, fracY) {
    const id = ++nextRequestId;
    return new Promise((resolve) => {
        pending.set(id, { resolve });
        renderWorker.postMessage({
            type: 'sample', id, url, fracX, fracY,
        });
    });
}


// ─── Render-canvas LRU cache ────────────────────────────────────
// Keyed by (rasterURL, analysisKey, param). A hit short-circuits both the
// worker and the network. Sized to comfortably hold a wide pan + zoom
// in/out at the default resolution (≈384 visible+adjacent tiles, plus
// a few hundred warmed by prefetch) so that backtracking pans hit cache
// instead of triggering a re-render and a basemap-flash.
const CANVAS_CACHE_LIMIT = 768;
const canvasCache = new Map();

function canvasCacheGet(key) {
    if (!canvasCache.has(key)) return undefined;
    const v = canvasCache.get(key);
    canvasCache.delete(key);
    canvasCache.set(key, v);
    return v;
}

function canvasCacheSet(key, val) {
    canvasCache.set(key, val);
    while (canvasCache.size > CANVAS_CACHE_LIMIT) {
        canvasCache.delete(canvasCache.keys().next().value);
    }
}

// Per-key in-flight dedup: ArcGIS occasionally calls fetchTile() for
// the same (level, row, col) more than once before the first resolves
// (e.g. during fast pan-then-back, or when its internal mosaic decides
// to re-evaluate a slot). Without dedup, every duplicate enqueues
// another full worker render that fights for the single-threaded worker
// against tiles for slots that *are* still on screen — the queue grows
// and the user sees basemap until it drains. Sharing one promise per
// key collapses dupes to a single render.
const inflightCanvas = new Map();

// Safety timeout. A wedged render (worker stuck, fetch hanging) used
// to leave the fetchTile promise dangling forever, which ArcGIS reads
// as "still loading, keep the slot empty" — a permanent basemap hole.
// 20s is well past the slowest legitimate NOAA cold fetch; anything
// past that is a real failure and ArcGIS should be told to move on.
const RENDER_TIMEOUT_MS = 20000;

function abortError() {
    const e = new Error('Aborted');
    e.name = 'AbortError';
    return e;
}

// One blank reusable canvas — used for both legitimately-empty tiles
// (HTTP 204) and transient errors. The crucial difference: empty tiles
// are CACHED into the LRU under their key (NOAA confirmed nothing is
// there, no point asking again), while error tiles are NOT cached, so
// the next pan/zoom retries them.
//
// Filled with an opaque dark color (rather than left transparent) so
// that empty/error slots COVER the basemap. With a transparent blank
// the satellite/hybrid basemap leaks through, which reads as "my depth
// tile failed to render". Opaque dark reads correctly as "no data here".
const blankTileCanvas = (() => {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#141418';
    ctx.fillRect(0, 0, 256, 256);
    return c;
})();

function renderToCanvas(url, analysisKey, param, paramExtra, key) {
    const work = (async () => {
        let result;
        let timer;
        const { promise: renderPromise, id: renderId } =
            requestRender(url, analysisKey, param, paramExtra);
        try {
            result = await Promise.race([
                renderPromise,
                new Promise((_, rej) => {
                    timer = setTimeout(
                        () => rej(new Error('Render timeout')),
                        RENDER_TIMEOUT_MS,
                    );
                }),
            ]);
        } catch (err) {
            // Timeout / unexpected throw: treat as transient. Don't cache —
            // the next ArcGIS call retries. Drop the pending slot so when
            // the worker eventually responds, onWorkerMessage takes the
            // late-arrival branch and explicitly closes the bitmap rather
            // than resolving a promise nobody is listening to (which would
            // pin GPU memory until JS GC fires).
            pending.delete(renderId);
            console.warn('[render] giving up on tile:', err && err.message || err);
            return blankTileCanvas;
        } finally {
            if (timer) clearTimeout(timer);
        }
        if (result.status === 'empty') {
            canvasCacheSet(key, blankTileCanvas);
            return blankTileCanvas;
        }
        if (result.status === 'error') {
            // Don't cache. Returning the blank for THIS request keeps
            // ArcGIS happy; the next visit re-issues the render and
            // (hopefully) succeeds.
            return blankTileCanvas;
        }
        const { bitmap, size } = result;
        const canvas = document.createElement('canvas');
        canvas.width = size;
        canvas.height = size;
        canvas.getContext('2d').drawImage(bitmap, 0, 0);
        if (bitmap.close) bitmap.close();
        canvasCacheSet(key, canvas);
        return canvas;
    })();
    work.finally(() => {
        if (inflightCanvas.get(key) === work) inflightCanvas.delete(key);
    }).catch(() => { /* already handled */ });
    return work;
}

// Returns a Promise<canvas>. Honors `signal` so ArcGIS can abort
// requests for tiles it no longer cares about — the worker render still
// completes in the background and lands in the LRU, ready for the next
// request, but the fetchTile promise rejects immediately so ArcGIS can
// redraw the mosaic without waiting on dead work.
async function getRenderedCanvas(url, analysisKey, param, paramExtra, signal) {
    if (signal && signal.aborted) throw abortError();
    const extraKey = paramExtraKey(paramExtra);
    const key = `${url}|${analysisKey}|${param}|${extraKey}`;
    const hit = canvasCacheGet(key);
    if (hit) return hit;

    let work = inflightCanvas.get(key);
    if (!work) {
        work = renderToCanvas(url, analysisKey, param, paramExtra, key);
        inflightCanvas.set(key, work);
    }

    if (!signal) return work;

    return new Promise((resolve, reject) => {
        const onAbort = () => { cleanup(); reject(abortError()); };
        const cleanup = () => signal.removeEventListener('abort', onAbort);
        signal.addEventListener('abort', onAbort);
        work.then(
            (v) => { cleanup(); resolve(v); },
            (e) => { cleanup(); reject(e); },
        );
    });
}


require([
    "esri/Map",
    "esri/Basemap",
    "esri/views/MapView",
    "esri/layers/BaseTileLayer",
    "esri/layers/WebTileLayer",
    "esri/layers/TileLayer",
    "esri/layers/GraphicsLayer",
    "esri/layers/MediaLayer",
    "esri/layers/support/TileInfo",
    "esri/layers/support/ImageElement",
    "esri/layers/support/ExtentAndRotationGeoreference",
    "esri/geometry/SpatialReference",
    "esri/Graphic",
    "esri/geometry/Point",
    "esri/geometry/Polyline",
    "esri/geometry/Polygon",
    "esri/geometry/Extent",
], (EsriMap, Basemap, MapView, BaseTileLayer, WebTileLayer, TileLayer,
    GraphicsLayer, MediaLayer, TileInfo,
    ImageElement, ExtentAndRotationGeoreference, SpatialReference,
    Graphic, Point, Polyline, Polygon, Extent) => {
    // NOTE: this callback is intentionally NOT `async`. ArcGIS 4.26 ships
    // Dojo's AMD loader, which silently fails to invoke an async-function
    // callback — no error, no console message, the whole boot just stops.
    // That symptom was the entire "black map + dropdown stuck on Loading
    // sources…" regression. Anything that needs to wait on a Promise (the
    // `/sources` fetch below) is handled with `.then()` so the require
    // callback stays a plain function.

    const layerSlot = map_layers[0];
    const id = layerSlot.id;

    // ─── DOM refs ──────────────────────────────────────────
    const $analysis        = document.getElementById(`analysis-dropdown-${id}`);
    const $source          = document.getElementById(`data-source-dropdown-${id}`);
    const $param           = document.getElementById(`param-${id}`);
    const $paramLabel      = document.getElementById(`param-label-${id}`);
    const $paramValue      = document.getElementById(`param-value-${id}`);
    const $paramHint       = document.getElementById(`param-hint-${id}`);
    const $resolution      = document.getElementById(`resolution-${id}`);
    const $resValue        = document.getElementById(`res-value-${id}`);
    const $opacity         = document.getElementById(`opacity-${id}`);
    const $opacityVal      = document.getElementById(`opacity-value-${id}`);
    const $hint            = document.getElementById(`analysis-hint-${id}`);
    const $apply           = document.getElementById(`apply-btn-${id}`);
    const $reset           = document.getElementById(`reset-btn-${id}`);
    const $reload          = document.getElementById(`reload-btn-${id}`);
    const $pendingAnalysis = document.getElementById(`pending-analysis-${id}`);
    const $pendingSource   = document.getElementById(`pending-source-${id}`);
    const $pendingRes      = document.getElementById(`pending-resolution-${id}`);
    const $depthRangeSection = document.getElementById(`depth-range-section-${id}`);
    const $depthMin        = document.getElementById(`depth-min-${id}`);
    const $depthMax        = document.getElementById(`depth-max-${id}`);
    const $depthRangeValue = document.getElementById(`depth-range-value-${id}`);
    const $panel           = document.getElementById("control-panel");
    const $panelToggle     = document.getElementById("panel-toggle");
    const $panelClose      = document.getElementById("panel-close");
    const $loading         = document.getElementById("loading-indicator");
    const $loadingText     = $loading ? $loading.querySelector(".loading-text") : null;

    // Left sidebar
    const $leftPanel       = document.getElementById("left-panel");
    const $leftPanelToggle = document.getElementById("left-panel-toggle");
    const $leftPanelClose  = document.getElementById("left-panel-close");
    const $basemapGrid     = document.getElementById("basemap-grid");
    const $sidebarLat      = document.getElementById("sidebar-goto-lat");
    const $sidebarLon      = document.getElementById("sidebar-goto-lon");
    const $sidebarGoto     = document.getElementById("sidebar-goto-submit");
    const $measureStart    = document.getElementById("measure-start-btn");
    const $measureUndo     = document.getElementById("measure-undo-btn");
    const $measureClear    = document.getElementById("measure-clear-btn");
    const $measureHint     = document.getElementById("measure-hint");
    const $measureReadout  = document.getElementById("measure-readout");
    const $measurePrimary  = document.getElementById("measure-primary");
    const $measureSecondary= document.getElementById("measure-secondary");
    const $measureTag      = document.getElementById("measure-status-tag");
    const $workspace       = document.querySelector(".workspace");


    // ─── Populate bathymetry-source dropdown from /sources ─────
    // Boot is decoupled from the registry fetch: the HTML placeholder
    // option carries value="dem-tiles" so the map's initial tile layer
    // builds with a valid source ID even if /sources is slow or fails.
    // When the fetch lands, we swap the dropdown contents for the real
    // registry list and (re)select the default. `sourcesById` is read
    // by the prefetch loop to clamp tiers to each source's true max
    // zoom; it stays `{}` until the response arrives, at which point
    // the next prefetch tick picks up the per-source caps.
    let sourcesById = {};
    _sourcesReady.then((payload) => {
        const sourcesList = payload.sources || [];
        sourcesById = Object.fromEntries(sourcesList.map(s => [s.id, s]));
        const defaultSourceId = payload.default;

        if (!sourcesList.length) {
            // Registry fetch failed or returned empty. Surface the
            // failure in the dropdown — the indefinite "Loading sources…"
            // text would otherwise read as "still loading" forever.
            // The map keeps running on the HTML placeholder's dem-tiles
            // value so the user at least sees tiles.
            $source.innerHTML =
                '<option value="dem-tiles" disabled selected>'
              + 'Sources unavailable — using default</option>';
            $source.disabled = true;
            return;
        }

        const prev = $source.value;  // bootstrap-time selection
        $source.innerHTML = "";
        for (const s of sourcesList) {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = s.experimental
                ? `${s.display_name} (experimental)`
                : s.display_name;
            if (s.notes) opt.title = s.notes;
            $source.appendChild(opt);
        }
        // Honor the boot-time selection if it survives in the registry,
        // otherwise fall back to the registry's declared default.
        $source.value = sourcesById[prev] ? prev : defaultSourceId;
        $source.disabled = false;

        // The dropdown's value may have shifted (boot-fallback dem-tiles
        // → registry default) if dem-tiles isn't in the registry. Sync
        // draft and committed so the Apply button doesn't false-positive
        // as dirty, and rebuild the live layer if the source genuinely
        // changed so the visible tiles match the new source ID.
        draft = readDraft();
        const sourceChanged = draft.source !== committed.source;
        committed = { ...committed, source: draft.source };
        if (sourceChanged) applyConfig(committed, "cutover");
        syncControlState();
    });


    // ─── Slider/label sync ─────────────────────────────────
    function syncParamControl() {
        const cfg = ANALYSES[$analysis.value] || ANALYSES["color-relief"];
        $paramLabel.textContent = cfg.label;
        $paramHint.textContent  = cfg.hint;
        $hint.textContent       = cfg.intro;
        $param.min   = cfg.min;
        $param.max   = cfg.max;
        $param.step  = cfg.step;
        const cur = parseFloat($param.value);
        if (!Number.isFinite(cur) || cur < cfg.min || cur > cfg.max) {
            $param.value = cfg.default;
        }
        $paramValue.textContent = `${$param.value}${cfg.unit}`;

        // Depth-range section visibility: only Color Relief uses it today.
        // Hiding it for other analyses keeps the panel uncluttered AND
        // avoids the user fiddling with a control that has no effect on
        // the active analysis.
        const showRange = $analysis.value === "color-relief";
        if ($depthRangeSection) {
            $depthRangeSection.classList.toggle("hidden", !showRange);
        }
    }

    function updateParamLabel() {
        const cfg = ANALYSES[$analysis.value] || ANALYSES["color-relief"];
        $paramValue.textContent = `${$param.value}${cfg.unit}`;
    }

    // ─── Depth-range (Color Relief) ────────────────────────
    // The two range inputs share a single visual track. We coerce them
    // here so the min handle can never exceed the max handle (and vice
    // versa), and refresh the readable "0–300 ft" label.
    const MIN_DEPTH_SPAN_FT = 10;   // smallest range the user can squeeze to

    function readDepthRange() {
        let lo = parseInt($depthMin.value, 10);
        let hi = parseInt($depthMax.value, 10);
        if (!Number.isFinite(lo)) lo = COLOR_RELIEF_DEPTH_RANGE.defaultMin;
        if (!Number.isFinite(hi)) hi = COLOR_RELIEF_DEPTH_RANGE.defaultMax;
        if (hi - lo < MIN_DEPTH_SPAN_FT) hi = lo + MIN_DEPTH_SPAN_FT;
        return { minDepthFt: lo, maxDepthFt: hi };
    }

    function syncDepthRangeUI() {
        const { minDepthFt, maxDepthFt } = readDepthRange();
        $depthMin.value = String(minDepthFt);
        $depthMax.value = String(maxDepthFt);
        $depthRangeValue.textContent = `${minDepthFt}–${maxDepthFt} ft`;
    }

    // Init bounds + defaults from the constant. The HTML carries sensible
    // values too but we re-set them here so any future tweak to the
    // constant is the single source of truth.
    if ($depthMin && $depthMax) {
        $depthMin.min  = String(COLOR_RELIEF_DEPTH_RANGE.minBound);
        $depthMin.max  = String(COLOR_RELIEF_DEPTH_RANGE.maxBound);
        $depthMin.step = String(COLOR_RELIEF_DEPTH_RANGE.step);
        $depthMax.min  = String(COLOR_RELIEF_DEPTH_RANGE.minBound);
        $depthMax.max  = String(COLOR_RELIEF_DEPTH_RANGE.maxBound);
        $depthMax.step = String(COLOR_RELIEF_DEPTH_RANGE.step);
        $depthMin.value = String(COLOR_RELIEF_DEPTH_RANGE.defaultMin);
        $depthMax.value = String(COLOR_RELIEF_DEPTH_RANGE.defaultMax);
        syncDepthRangeUI();
    }


    // ─── Custom tile layer ─────────────────────────────────
    // Each instance is bound to one (source, resolution, analysis, param)
    // tuple. Config changes always build a fresh layer — never mutate.
    //
    // The `options.signal` we forward to getRenderedCanvas is what lets
    // ArcGIS retire tile requests it has decided to abandon — without it
    // ArcGIS would hold the slot in "loading" state until our promise
    // resolves, which is exactly the "tile blank, basemap visible"
    // window during fast pans.
    const RasterAnalysisLayer = BaseTileLayer.createSubclass({
        properties: {
            source: null,
            resolution: null,
            analysisKey: null,
            param: null,
            paramExtra: null,
        },
        fetchTile: function (level, row, col, options) {
            const url = `${baseurl}/raster/${this.source}/${this.resolution}`
                      + `/${level}/${col}/${row}.bin`;
            const signal = options && options.signal;
            return getRenderedCanvas(
                url, this.analysisKey, this.param, this.paramExtra, signal);
        },
    });


    // ─── Map setup ─────────────────────────────────────────

    syncParamControl();

    const markerLayer    = new GraphicsLayer({ listMode: "hide" });
    const measureLayer   = new GraphicsLayer({ listMode: "hide" });
    const spotfinderLayer = new GraphicsLayer({ listMode: "hide" });

    const map = new EsriMap({
        basemap: "dark-gray-vector",
        layers: [markerLayer, measureLayer, spotfinderLayer],
    });

    const view = new MapView({
        container: "map",
        map,
        center: [-81.083, 24.713],
        zoom: 8,
        ui: { components: ["zoom", "attribution"] },
    });


    // ─── Config state ──────────────────────────────────────
    // `committed` = the config whose layer is on the map (or being loaded
    //               in cutover mode and "claimed" already).
    // `draft`     = the config the user is currently editing.
    // The diff between them drives the pending tags + Apply button state.

    function readDraft() {
        const { minDepthFt, maxDepthFt } = readDepthRange();
        return {
            analysis:   $analysis.value,
            source:     $source.value,
            resolution: parseInt($resolution.value, 10),
            param:      Math.round(parseFloat($param.value)),
            minDepthFt,
            maxDepthFt,
        };
    }

    // Structured per-analysis extras passed to the worker. Only set for
    // analyses that consume them — others get `null` so the cache key
    // stays compact and there's no false-positive cache miss.
    function paramExtraFor(cfg) {
        if (cfg.analysis === "color-relief") {
            return { minDepthFt: cfg.minDepthFt, maxDepthFt: cfg.maxDepthFt };
        }
        return null;
    }

    let committed = readDraft();
    let draft     = { ...committed };

    function diffMajor(a, b) {
        return {
            analysis:   a.analysis   !== b.analysis,
            source:     a.source     !== b.source,
            resolution: a.resolution !== b.resolution,
        };
    }
    function isDirtyMajor() {
        const d = diffMajor(committed, draft);
        return d.analysis || d.source || d.resolution;
    }

    function syncControlState() {
        const d = diffMajor(committed, draft);

        $pendingAnalysis.classList.toggle("visible", d.analysis);
        $pendingSource.classList.toggle("visible", d.source);
        $pendingRes.classList.toggle("visible", d.resolution);
        $analysis.classList.toggle("pending", d.analysis);
        $source.classList.toggle("pending", d.source);
        $resolution.classList.toggle("pending", d.resolution);

        const dirty = d.analysis || d.source || d.resolution;
        if (dirty) {
            $apply.disabled = false;
            $apply.classList.add("pending");
            $apply.textContent = "Apply changes";
            $reset.disabled = false;
        } else {
            $apply.classList.remove("pending");
            $apply.disabled = true;
            $apply.textContent = "Up to date";
            $reset.disabled = true;
        }
    }


    // ─── Layer-swap pipeline ───────────────────────────────
    let currentLayer   = null;
    let pendingLayer   = null;
    let pendingHandle  = null;
    let pendingApplyId = 0;

    function tearDown(layer, handle) {
        if (handle) handle.remove();
        if (layer && map.layers.includes(layer)) map.remove(layer);
    }

    function buildLayer(cfg) {
        return new RasterAnalysisLayer({
            tileInfo: TileInfo.create({
                spatialReference: SpatialReference.WebMercator,
            }),
            spatialReference: SpatialReference.WebMercator,
            opacity: parseInt($opacity.value, 10) / 100,
            source:      cfg.source,
            resolution:  cfg.resolution,
            analysisKey: cfg.analysis,
            param:       cfg.param,
            paramExtra:  paramExtraFor(cfg),
        });
    }

    // Apply a config in one of two modes:
    //
    //   'cutover' — the major change path. Old layer is removed from the
    //   map BEFORE the new one is added, so there is no period during
    //   which old data masquerades as new. The user briefly sees just
    //   the basemap. Used by the Apply button.
    //
    //   'overlap' — the live-param path. New layer is added on top while
    //   the old one keeps painting underneath, then removed when the new
    //   layer's tiles are ready. Safe ONLY when the underlying data and
    //   algorithm are identical — a same-data re-render with a different
    //   parameter. Used for live param drags.
    function applyConfig(cfg, mode) {
        const myId = ++pendingApplyId;
        const newLayer = buildLayer(cfg);

        // Drop any older pending swap. (Independent of mode — we only ever
        // want one in-flight pending layer at a time.)
        tearDown(pendingLayer, pendingHandle);
        pendingLayer  = newLayer;
        pendingHandle = null;

        if (mode === "cutover" && currentLayer) {
            // The promise of this mode: no stale data on screen during
            // the load. Pull it now.
            tearDown(currentLayer, null);
            currentLayer = null;
        }

        // Bathymetry sits at the very bottom of the overlay stack so any
        // active Spotfinder heatmap + spots paint above it. Inserting at
        // markerIdx (the original approach) puts a re-applied bathymetry
        // layer above already-active heatmaps, hiding them under the
        // opaque tile pixels.
        map.layers.add(newLayer, 0);

        showLoading(cfg, mode);

        view.whenLayerView(newLayer).then((lv) => {
            if (myId !== pendingApplyId) return;  // superseded already

            const handle = lv.watch("updating", (val) => {
                // Drop callbacks for superseded layers FIRST. Otherwise
                // a stale layer toggling false could clear the loading
                // indicator while a newer layer is still loading.
                if (myId !== pendingApplyId) return;

                if (val) { showLoading(cfg, mode); return; }
                hideLoading();

                handle.remove();
                pendingHandle = null;
                pendingLayer  = null;

                if (currentLayer && currentLayer !== newLayer) {
                    map.remove(currentLayer);
                }
                currentLayer = newLayer;

                // The on-screen tiles are now in the canvas LRU. Warm
                // adjacent zooms in the background so the next zoom is
                // a cache hit instead of a NOAA round trip.
                schedulePrefetch();
            });
            pendingHandle = handle;
        }).catch(() => {
            // The most common cause is supersession: a newer apply tore
            // this layer down before its layerView was created and the
            // newer apply has already overwritten pendingLayer / etc., so
            // we MUST NOT touch those globals here — clearing pendingLayer
            // would mean the next apply's `tearDown(pendingLayer, ...)`
            // becomes a no-op and the orphan stays on the map forever
            // (a stack of ghost tile layers each calling fetchTile, which
            // is what makes Reload "kill" the view after a couple of
            // presses). Just clear the loading indicator if we are still
            // the latest in-flight apply; the next apply will re-show it.
            if (myId === pendingApplyId) hideLoading();
        });
    }

    function commitDraft() {
        if (!isDirtyMajor()) return;
        // Abandon speculative work for the config the user just discarded.
        // Otherwise the worker keeps rendering old-source tiles ahead of
        // the new visible ones.
        cancelPrefetch();
        committed = { ...draft };

        // Update controls to reflect the new committed state. With
        // draft == committed, syncControlState will set the button to
        // "Up to date"/disabled. The instant the user changes a dropdown
        // again it'll snap back to "Apply changes" — clicking again
        // supersedes the in-flight load via pendingApplyId. Rendering
        // feedback lives on the bottom-of-panel $loading indicator,
        // not on the Apply button.
        syncControlState();

        applyConfig(committed, "cutover");
    }

    function liveParamUpdate() {
        // Caller has guaranteed isDirtyMajor() is false. Same data, same
        // algorithm, just a different param — overlap is safe.
        // Cancel any prefetch in flight: it was rendering with the OLD
        // param, so the canvas cache entries it would produce don't
        // match what we're about to ask for.
        cancelPrefetch();
        committed = {
            ...committed,
            param: draft.param,
            minDepthFt: draft.minDepthFt,
            maxDepthFt: draft.maxDepthFt,
        };
        applyConfig(committed, "overlap");
    }

    function resetDraft() {
        $analysis.value   = committed.analysis;
        $source.value     = committed.source;
        $resolution.value = committed.resolution;
        $param.value      = committed.param;
        if ($depthMin && $depthMax) {
            $depthMin.value = String(committed.minDepthFt);
            $depthMax.value = String(committed.maxDepthFt);
            syncDepthRangeUI();
        }
        syncParamControl();
        $resValue.textContent = $resolution.value;
        draft = readDraft();
        syncControlState();
    }


    // ─── Loading indicator ─────────────────────────────────
    // For cutover (a user-initiated reload), show immediately so the user
    // gets unambiguous feedback the moment the screen goes blank. For
    // overlap (live param), delay 200 ms so an instant cache hit doesn't
    // flash the spinner.
    let loadingShowTimer = null;
    function showLoading(cfg, mode) {
        if (!$loading) return;
        if (loadingShowTimer) {
            clearTimeout(loadingShowTimer);
            loadingShowTimer = null;
        }
        if ($loadingText) {
            const a = ANALYSES[cfg.analysis];
            const name = (a && a.pretty) || cfg.analysis;
            $loadingText.textContent = mode === "cutover"
                ? `Loading ${name}…`
                : "Updating…";
        }
        if (mode === "cutover") {
            $loading.classList.add("active");
        } else {
            loadingShowTimer = setTimeout(() => {
                $loading.classList.add("active");
                loadingShowTimer = null;
            }, 200);
        }
    }
    function hideLoading() {
        if (loadingShowTimer) {
            clearTimeout(loadingShowTimer);
            loadingShowTimer = null;
        }
        if ($loading) $loading.classList.remove("active");
    }


    // ─── Tiered prefetch ───────────────────────────────────
    //
    // While the view is at rest, warm the canvas LRU with tiles the user
    // is likely to need next. Without this, the moment they pan or zoom
    // the new visible tiles are uncached — the worker has to render
    // them from scratch (50-100 ms hot, 200-800 ms cold from NOAA), and
    // for that window the slots show basemap-only.
    //
    // Three classes of warming, processed in priority order:
    //
    //   1. Same-zoom 1-ring  — tiles immediately outside the visible
    //      extent at the current zoom. Covers the most common move
    //      (a small pan into adjacent geography). Highest priority.
    //
    //   2. Zoom + 1          — child tiles. Covers zoom-in, where ArcGIS
    //      can stretch the current tiles temporarily but the child tiles
    //      are sharper and arrive instantly if cached.
    //
    //   3. Zoom - 1, -2, -3  — parent tiles. ArcGIS's stretched-parent
    //      fallback is what fills the gap while finer tiles load; if
    //      the parent isn't cached either, the slot goes to basemap.
    //      Three coarse levels means *some* parent is always available
    //      no matter how fast the user is zooming in.
    //
    // Each tier has its own budget so a busy z+1 (4× tile count) can't
    // crowd out the small but critical z-2/z-3 set. Within a tier we
    // sort by distance to view centre so the most-visible first.
    //
    // Sequential because the worker is single-threaded — a parallel
    // flood would queue *ahead* of any user-issued fetchTile and make
    // pan/zoom transitions visibly slower, the opposite of what we want.

    const WEB_MERCATOR_HALF = 20037508.342789244;
    const PREFETCH_IDLE_MS = 400;
    // Tier budgets sum to the total work we'll do per idle tick.
    const PREFETCH_TIERS = [
        { name: "ring",   budget: 12 },
        { name: "z+1",    budget: 12 },
        { name: "z-1",    budget: 6  },
        { name: "z-2",    budget: 4  },
        { name: "z-3",    budget: 4  },
    ];

    function tilesInView(extent, zoom) {
        const numTiles = Math.pow(2, zoom);
        const tileSize = (2 * WEB_MERCATOR_HALF) / numTiles;
        const xmin = Math.floor((extent.xmin + WEB_MERCATOR_HALF) / tileSize);
        const xmax = Math.floor((extent.xmax + WEB_MERCATOR_HALF) / tileSize);
        const ymin = Math.floor((WEB_MERCATOR_HALF - extent.ymax) / tileSize);
        const ymax = Math.floor((WEB_MERCATOR_HALF - extent.ymin) / tileSize);
        const lim = numTiles - 1;
        const out = [];
        for (let y = Math.max(0, ymin); y <= Math.min(lim, ymax); y++) {
            for (let x = Math.max(0, xmin); x <= Math.min(lim, xmax); x++) {
                out.push({ z: zoom, x, y });
            }
        }
        return out;
    }
    function tilesAroundView(extent, zoom, ringSize) {
        // Tiles in a `ringSize`-wide band around the visible extent,
        // excluding the visible tiles themselves (those are ArcGIS's
        // job — duplicating them would compete for the worker).
        const numTiles = Math.pow(2, zoom);
        const tileSize = (2 * WEB_MERCATOR_HALF) / numTiles;
        const xmin = Math.floor((extent.xmin + WEB_MERCATOR_HALF) / tileSize);
        const xmax = Math.floor((extent.xmax + WEB_MERCATOR_HALF) / tileSize);
        const ymin = Math.floor((WEB_MERCATOR_HALF - extent.ymax) / tileSize);
        const ymax = Math.floor((WEB_MERCATOR_HALF - extent.ymin) / tileSize);
        const lim = numTiles - 1;
        const out = [];
        const y0 = Math.max(0, ymin - ringSize);
        const y1 = Math.min(lim, ymax + ringSize);
        const x0 = Math.max(0, xmin - ringSize);
        const x1 = Math.min(lim, xmax + ringSize);
        for (let y = y0; y <= y1; y++) {
            for (let x = x0; x <= x1; x++) {
                if (x >= xmin && x <= xmax && y >= ymin && y <= ymax) continue;
                out.push({ z: zoom, x, y });
            }
        }
        return out;
    }
    function tileCenterMeters(t) {
        const numTiles = Math.pow(2, t.z);
        const tileSize = (2 * WEB_MERCATOR_HALF) / numTiles;
        return [
            -WEB_MERCATOR_HALF + (t.x + 0.5) * tileSize,
             WEB_MERCATOR_HALF - (t.y + 0.5) * tileSize,
        ];
    }
    function sortByDistance(tiles, cx, cy) {
        tiles.sort((a, b) => {
            const [ax, ay] = tileCenterMeters(a);
            const [bx, by] = tileCenterMeters(b);
            return Math.hypot(ax - cx, ay - cy) - Math.hypot(bx - cx, by - cy);
        });
    }

    let prefetchToken     = 0;
    let prefetchInFlight  = false;
    let prefetchTimer     = null;

    function buildPrefetchPlan(extent, zoom, sourceId) {
        const cx = (extent.xmin + extent.xmax) / 2;
        const cy = (extent.ymin + extent.ymax) / 2;
        // Per-source zoom caps, fed from the registry. Falls back to
        // [0, 22] (the server's hard floor/ceiling) if the source is
        // missing — keeps the loop running even if the registry race
        // somehow leaves us without entries.
        const srcMeta = sourcesById[sourceId];
        const zMin = srcMeta ? srcMeta.min_zoom : 0;
        const zMax = srcMeta ? srcMeta.max_zoom : 22;
        const plan = [];
        const tiers = {
            "ring": zoom <= zMax       ? tilesAroundView(extent, zoom, 1)   : [],
            "z+1":  zoom + 1 <= zMax   ? tilesInView(extent, zoom + 1)      : [],
            "z-1":  zoom - 1 >= zMin   ? tilesInView(extent, zoom - 1)      : [],
            "z-2":  zoom - 2 >= zMin   ? tilesInView(extent, zoom - 2)      : [],
            "z-3":  zoom - 3 >= zMin   ? tilesInView(extent, zoom - 3)      : [],
        };
        for (const tier of PREFETCH_TIERS) {
            const tiles = tiers[tier.name];
            if (!tiles || !tiles.length) continue;
            sortByDistance(tiles, cx, cy);
            for (const t of tiles.slice(0, tier.budget)) plan.push(t);
        }
        return plan;
    }

    async function runPrefetch() {
        if (prefetchInFlight)            return;
        if (!view.extent || !committed)  return;
        if (isDirtyMajor())              return;

        const myToken = ++prefetchToken;
        const cfg = { ...committed };
        const zoom = Math.round(view.zoom);
        const plan = buildPrefetchPlan(view.extent, zoom, cfg.source);

        prefetchInFlight = true;
        const extra = paramExtraFor(cfg);
        const extraKey = paramExtraKey(extra);
        try {
            for (const t of plan) {
                if (myToken !== prefetchToken) return;  // user moved, bail
                const url = `${baseurl}/raster/${cfg.source}/${cfg.resolution}`
                          + `/${t.z}/${t.x}/${t.y}.bin`;
                const key = `${url}|${cfg.analysis}|${cfg.param}|${extraKey}`;
                if (canvasCache.has(key)) continue;     // already warm
                try { await getRenderedCanvas(url, cfg.analysis, cfg.param, extra); }
                catch { /* keep the chain alive on individual failures */ }
            }
        } finally {
            prefetchInFlight = false;
        }
    }

    function schedulePrefetch() {
        if (prefetchTimer) clearTimeout(prefetchTimer);
        prefetchTimer = setTimeout(() => {
            prefetchTimer = null;
            runPrefetch();
        }, PREFETCH_IDLE_MS);
    }
    function cancelPrefetch() {
        if (prefetchTimer) { clearTimeout(prefetchTimer); prefetchTimer = null; }
        prefetchToken++;  // invalidates any in-flight loop
    }

    view.watch("stationary", (val) => {
        if (val) schedulePrefetch();
        else     cancelPrefetch();
    });


    // ─── Initial layer ─────────────────────────────────────
    // Cutover mode is correct here: there's no previous layer, so no
    // overlap is possible anyway, and we want the prominent loading hint.
    applyConfig(committed, "cutover");
    syncControlState();


    // ─── Inputs ────────────────────────────────────────────
    //
    // Major controls only update the draft. They never trigger a load —
    // the user must press Apply.
    $analysis.addEventListener("change", () => {
        syncParamControl();
        draft = readDraft();
        syncControlState();
    });
    $source.addEventListener("change", () => {
        draft = readDraft();
        syncControlState();
    });
    $resolution.addEventListener("input", () => {
        $resValue.textContent = $resolution.value;
    });
    $resolution.addEventListener("change", () => {
        draft = readDraft();
        syncControlState();
    });

    // Param slider: live re-render WHEN it's the only thing that's
    // changed. Otherwise queue with the rest. Coalesce drag ticks to one
    // rebuild per animation frame — the canvas LRU makes repeated values
    // a hit anyway.
    let liveParamScheduled = false;
    function maybeLiveParam() {
        if (liveParamScheduled) return;
        liveParamScheduled = true;
        requestAnimationFrame(() => {
            liveParamScheduled = false;
            // Re-check at the rAF boundary; user may have changed
            // something major in the interim.
            draft = readDraft();
            if (isDirtyMajor()) { syncControlState(); return; }
            if (draft.param === committed.param) return;
            liveParamUpdate();
            syncControlState();
        });
    }
    $param.addEventListener("input", () => {
        updateParamLabel();
        draft = readDraft();
        if (isDirtyMajor()) {
            // Other changes pending — the param slider is part of the
            // queue too. The user will see the new param when they Apply.
            syncControlState();
            return;
        }
        maybeLiveParam();
    });
    $param.addEventListener("change", () => {
        // Belt-and-braces: if a coalesced live update was lost to a race,
        // catch it on release.
        draft = readDraft();
        if (!isDirtyMajor() && draft.param !== committed.param) {
            liveParamUpdate();
        }
        syncControlState();
    });

    // Depth-range inputs: same live-update semantics as the param slider.
    // Same algorithm, same raster — only the colour mapping shifts — so
    // an overlap swap is safe. The two handles are coupled to never
    // cross, with a minimum 10 ft span so the colour ramp never collapses
    // to a single hue.
    function depthRangeChanged() {
        return draft.minDepthFt !== committed.minDepthFt
            || draft.maxDepthFt !== committed.maxDepthFt;
    }
    let liveDepthRangeScheduled = false;
    function maybeLiveDepthRange() {
        // Mirror maybeLiveParam: coalesce drag ticks to one render per
        // animation frame, and skip if any major change is queued.
        if (liveDepthRangeScheduled) return;
        liveDepthRangeScheduled = true;
        requestAnimationFrame(() => {
            liveDepthRangeScheduled = false;
            draft = readDraft();
            // Reflect the coerced values back to the inputs so the user
            // sees the clamp + min-span behaviour immediately.
            syncDepthRangeUI();
            if (isDirtyMajor()) { syncControlState(); return; }
            if (!depthRangeChanged()) return;
            liveParamUpdate();
            syncControlState();
        });
    }
    function onDepthInputInput(which) {
        // While dragging, keep the two handles from crossing without
        // forcing one to grab the other prematurely — instead pin the
        // moving handle just on its side of the other.
        let lo = parseInt($depthMin.value, 10);
        let hi = parseInt($depthMax.value, 10);
        if (!Number.isFinite(lo)) lo = COLOR_RELIEF_DEPTH_RANGE.defaultMin;
        if (!Number.isFinite(hi)) hi = COLOR_RELIEF_DEPTH_RANGE.defaultMax;
        if (which === "min" && lo > hi - MIN_DEPTH_SPAN_FT) {
            lo = Math.max(COLOR_RELIEF_DEPTH_RANGE.minBound, hi - MIN_DEPTH_SPAN_FT);
            $depthMin.value = String(lo);
        }
        if (which === "max" && hi < lo + MIN_DEPTH_SPAN_FT) {
            hi = Math.min(COLOR_RELIEF_DEPTH_RANGE.maxBound, lo + MIN_DEPTH_SPAN_FT);
            $depthMax.value = String(hi);
        }
        $depthRangeValue.textContent = `${lo}–${hi} ft`;
        draft = readDraft();
        if (isDirtyMajor()) {
            syncControlState();
            return;
        }
        maybeLiveDepthRange();
    }
    if ($depthMin && $depthMax) {
        $depthMin.addEventListener("input",  () => onDepthInputInput("min"));
        $depthMax.addEventListener("input",  () => onDepthInputInput("max"));
        // Belt-and-braces on release in case a coalesced update was lost.
        $depthMin.addEventListener("change", () => {
            draft = readDraft();
            if (!isDirtyMajor() && depthRangeChanged()) liveParamUpdate();
            syncControlState();
        });
        $depthMax.addEventListener("change", () => {
            draft = readDraft();
            if (!isDirtyMajor() && depthRangeChanged()) liveParamUpdate();
            syncControlState();
        });
    }

    // Opacity is a compositor property — mutate the live layer pair and
    // we're done. Zero recompute, zero rebuild.
    $opacity.addEventListener("input", (e) => {
        const pct = parseInt(e.target.value, 10);
        const op  = pct / 100;
        $opacityVal.textContent = `${pct}%`;
        if (currentLayer) currentLayer.opacity = op;
        if (pendingLayer) pendingLayer.opacity = op;
    });

    $apply.addEventListener("click", commitDraft);
    $reset.addEventListener("click", resetDraft);


    // ─── Reload view (manual pipeline reset) ───────────────
    //
    // Forces a fresh tile layer with the current committed config. Goes
    // through the SAME cutover path as Apply: ArcGIS's per-tile abort
    // signals fire on the old layer as it is removed, the new layer
    // re-issues fetchTile for every visible slot, and any newly-visible
    // tile re-renders. Tiles already in the canvas LRU paint instantly
    // — the LRU is the user's friend, not what they're trying to escape.
    //
    // What this previously did and no longer does:
    //   * Wipe canvasCache. Pointless (the cache is keyed by analysis +
    //     param, so a "bad" entry would only repeat under the exact same
    //     key — and after the worker scratch + GPU-bitmap leak fixes
    //     that scenario is no longer reachable).
    //   * Wipe inflightCanvas. Unsafe — the in-flight `work` IIFEs are
    //     still going to land their results and try to clean up their
    //     map slot.
    //   * Terminate + respawn the render worker. Concurrent with the
    //     cutover this opened a window where ArcGIS aborts on the old
    //     layer raced with `pending` resolutions from the dying worker
    //     and the freshly-added new layer's first fetchTile burst —
    //     in practice this is what was breaking the view on every press.
    //
    // What it still does NOT touch (unchanged):
    //   * Settings, view position, markers, measurement state, search
    //     history, or the server-side disk cache.
    //
    // If the user genuinely needs a full memory wipe (worker has
    // somehow gone wild, GPU starvation, etc.), a hard page refresh
    // is the right escape hatch — and the heartbeat will let the
    // backend recycle as well.
    let reloadInFlight = false;
    function reloadView() {
        if (reloadInFlight) return;
        reloadInFlight = true;
        $reload.disabled = true;

        cancelPrefetch();
        applyConfig(committed, "cutover");

        const start = performance.now();
        const enable = () => {
            // Floor the spinner duration to ~500 ms so a fully-cached
            // reload reads as "something happened" instead of a flicker.
            const wait = Math.max(0, 500 - (performance.now() - start));
            setTimeout(() => {
                $reload.disabled = false;
                reloadInFlight = false;
            }, wait);
        };
        const layerToWatch = pendingLayer || currentLayer;
        if (!layerToWatch) { enable(); return; }
        view.whenLayerView(layerToWatch)
            .then((lv) => {
                const h = lv.watch("updating", (val) => {
                    if (val) return;
                    h.remove();
                    enable();
                });
            })
            .catch(enable);
    }
    $reload.addEventListener("click", reloadView);

    // Enter inside the panel applies any pending changes — convenient
    // after a keyboard-only dropdown change.
    $panel.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && isDirtyMajor()) {
            e.preventDefault();
            commitDraft();
        }
    });

    $panelToggle.addEventListener("click", () => {
        $panel.classList.remove("collapsed");
        $panelToggle.classList.remove("visible");
    });
    $panelClose.addEventListener("click", () => {
        $panel.classList.add("collapsed");
        $panelToggle.classList.add("visible");
    });

    // Left sidebar toggle. Starts open, with the floating tab hidden until
    // the user collapses the panel.
    $leftPanelToggle.addEventListener("click", () => {
        $leftPanel.classList.remove("collapsed");
        $leftPanelToggle.classList.remove("visible");
    });
    $leftPanelClose.addEventListener("click", () => {
        $leftPanel.classList.add("collapsed");
        $leftPanelToggle.classList.add("visible");
    });


    // ─── Click → depth lookup ──────────────────────────────
    //
    // Independent of the tile pipeline. The depth lookup uses whichever
    // source is currently COMMITTED — not the draft — so the value the
    // user reads matches the data on screen.

    const $card       = document.getElementById("depth-card");
    const $depthValue = document.getElementById("depth-value");
    const $depthCoord = document.getElementById("depth-coord");
    const $depthClose = document.getElementById("depth-close");
    const $depthCopy  = document.getElementById("depth-copy");

    let activeRequest = 0;
    let lastCoord = null;

    function showCard()  { $card.classList.remove("hidden"); }
    function hideCard()  { $card.classList.add("hidden"); markerLayer.removeAll(); }
    function setDepthValue(text, muted = false) {
        $depthValue.textContent = text;
        $depthValue.classList.toggle("muted", muted);
    }
    function dropMarker(point) {
        markerLayer.removeAll();
        markerLayer.add(new Graphic({
            geometry: point,
            symbol: {
                type: "simple-marker",
                style: "circle",
                color: [58, 163, 255, 0.9],
                size: 10,
                outline: { color: [255, 255, 255, 0.95], width: 1.5 },
            },
        }));
    }
    // Convert a click (lat, lon) to the raster URL + sub-tile fraction we
    // need to sample. We use the same (source, resolution, z) the visible
    // tile uses, so the depth value matches the colored pixel on screen.
    //
    // The previous server-side approach used NOAA's identify/getSamples
    // with a pixelSize hint. NOAA's mosaic rule resolves to *different
    // sub-rasters* of the source mosaic depending on pixelSize — at one
    // zoom you'd hit a high-res multibeam patch, at another a coarse CRM
    // mosaic that averages over hundreds of metres. That's the source of
    // the "different (and sometimes wildly wrong) values at different
    // zooms" symptom. Sampling the rendered grid directly removes the
    // mosaic-rule ambiguity entirely.
    function depthSampleParams(lat, lon, source, resolution) {
        const ORIGIN = WEB_MERCATOR_HALF;
        const z = Math.max(0, Math.min(22,
            Number.isFinite(view.zoom) ? Math.round(view.zoom) : 10));
        const mx = lon * ORIGIN / 180;
        const myDeg = Math.log(Math.tan((90 + lat) * Math.PI / 360))
                    / (Math.PI / 180);
        const my = myDeg * ORIGIN / 180;
        const numTiles = Math.pow(2, z);
        const tileSpan = (2 * ORIGIN) / numTiles;
        const tx = Math.floor((mx + ORIGIN) / tileSpan);
        const ty = Math.floor((ORIGIN - my) / tileSpan);
        const fracX = ((mx + ORIGIN) - tx * tileSpan) / tileSpan;
        const fracY = ((ORIGIN - my) - ty * tileSpan) / tileSpan;
        const url = `${baseurl}/raster/${source}/${resolution}`
                  + `/${z}/${tx}/${ty}.bin`;
        return { url, fracX, fracY };
    }

    async function lookupDepth(lat, lon, point) {
        const requestId = ++activeRequest;
        lastCoord = { lat, lon };
        dropMarker(point);
        showCard();
        setDepthValue("Loading…", true);
        $depthCoord.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
        try {
            const { url, fracX, fracY } = depthSampleParams(
                lat, lon, committed.source, committed.resolution);
            const result = await requestSample(url, fracX, fracY);
            if (requestId !== activeRequest) return;
            const m = result.value;
            if (m == null) setDepthValue("No data", true);
            else setDepthValue(`${(m * 3.28084).toFixed(1)} ft`);
        } catch (err) {
            if (requestId !== activeRequest) return;
            console.error(err);
            setDepthValue("Error", true);
        }
    }
    view.on("click", (event) => {
        // Distance tool intercepts clicks until two points are captured.
        // Returning early skips the depth lookup so the click is purely
        // a measurement input — no depth card flash, no spurious marker.
        if (measureMode) {
            captureMeasurePoint(event.mapPoint);
            return;
        }
        // Spotfinder is a drag-only interaction; a click while the
        // panel is open should not fall through to a depth lookup
        // (which would drop a marker over the user's selection).
        if (spotfinderActive) return;
        // Spot dots are visual-only — clicks pass through to the depth
        // lookup so the user can read sea-floor depth at a spot without
        // an extra popup blocking the existing depth card.
        const { latitude: lat, longitude: lon } = event.mapPoint;
        lookupDepth(lat, lon, event.mapPoint);
    });
    $depthClose.addEventListener("click", hideCard);
    $depthCopy.addEventListener("click", async () => {
        if (!lastCoord) return;
        const text = `${lastCoord.lat.toFixed(5)}, ${lastCoord.lon.toFixed(5)}`;
        try {
            await navigator.clipboard.writeText(text);
            $depthCopy.textContent = "Copied";
            $depthCopy.classList.add("copied");
            setTimeout(() => {
                $depthCopy.textContent = "Copy coordinates";
                $depthCopy.classList.remove("copied");
            }, 1400);
        } catch { /* ignore */ }
    });


    // ─── Search w/ autocomplete ────────────────────────────
    // ArcGIS World Geocoder, anonymous tier — free, no API key.
    // /suggest gives lightweight typeahead candidates (with magicKey),
    // findAddressCandidates resolves a magicKey to a precise location.
    const GEOCODE_BASE   = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer";
    const KEYS_CENTER    = "-81.2,24.85";              // proximity bias toward Florida Keys (soft — global results still allowed)
    const SUGGEST_DEBOUNCE_MS = 180;
    const MAX_SUGGESTIONS     = 6;

    const $search      = document.getElementById("search-input");
    const $suggestList = document.getElementById("search-suggestions");
    const $searchClear = document.getElementById("search-clear");

    let suggestSeq          = 0;     // monotonic — latest /suggest fetch wins
    let resolveSeq          = 0;     // monotonic — latest magicKey resolve wins
    let currentSuggestions  = [];
    let activeIdx           = -1;
    let suggestTimer        = null;

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    function setExpanded(open) {
        $suggestList.hidden = !open;
        $search.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function closeSuggest() {
        setExpanded(false);
        currentSuggestions = [];
        activeIdx = -1;
    }

    function renderSuggestions(items) {
        currentSuggestions = items;
        activeIdx = -1;
        if (!items.length) {
            $suggestList.innerHTML = `<li class="search-suggestion empty">No results</li>`;
        } else {
            $suggestList.innerHTML = items.map((it, i) =>
                `<li class="search-suggestion" data-idx="${i}" role="option">${escapeHtml(it.text)}</li>`
            ).join("");
        }
        setExpanded(true);
    }

    function setActive(idx) {
        const els = $suggestList.querySelectorAll(".search-suggestion[data-idx]");
        if (!els.length) return;
        activeIdx = Math.max(0, Math.min(idx, els.length - 1));
        els.forEach(el => el.classList.remove("active"));
        els[activeIdx].classList.add("active");
        els[activeIdx].scrollIntoView({ block: "nearest" });
    }

    async function fetchSuggestions(text) {
        const seq = ++suggestSeq;
        const params = new URLSearchParams({
            text,
            f: "json",
            maxSuggestions: String(MAX_SUGGESTIONS),
            location: KEYS_CENTER,
        });
        try {
            const resp = await fetch(`${GEOCODE_BASE}/suggest?${params}`);
            if (seq !== suggestSeq) return;
            const data = await resp.json();
            if (seq !== suggestSeq) return;
            renderSuggestions(data.suggestions || []);
        } catch (err) {
            if (seq !== suggestSeq) return;
            console.error("suggest failed:", err);
            closeSuggest();
        }
    }

    function pickZoomForExtent(extent) {
        if (!extent) return 13;
        const dx = Math.abs(extent.xmax - extent.xmin);
        const dy = Math.abs(extent.ymax - extent.ymin);
        const span = Math.max(dx, dy);
        if (span > 1.5)   return 8;   // state / large region
        if (span > 0.5)   return 10;  // metro
        if (span > 0.1)   return 12;  // city
        if (span > 0.02)  return 14;  // neighborhood
        return 16;                    // single address
    }

    async function jumpToCandidate(cand) {
        const lon  = cand.location.x;
        const lat  = cand.location.y;
        const zoom = pickZoomForExtent(cand.extent);
        const point = new Point({ longitude: lon, latitude: lat });
        await view.goTo({ center: [lon, lat], zoom });
        lookupDepth(lat, lon, point);
    }

    async function resolveAndJump(suggestion) {
        const seq = ++resolveSeq;
        $search.value = suggestion.text;
        $searchClear.hidden = false;
        closeSuggest();
        const params = new URLSearchParams({
            f: "json",
            magicKey: suggestion.magicKey,
            maxLocations: "1",
        });
        try {
            const resp = await fetch(`${GEOCODE_BASE}/findAddressCandidates?${params}`);
            if (seq !== resolveSeq) return;
            const data = await resp.json();
            if (seq !== resolveSeq) return;
            const cand = (data.candidates || [])[0];
            if (!cand) return;
            await jumpToCandidate(cand);
        } catch (err) {
            if (seq !== resolveSeq) return;
            console.error("resolve failed:", err);
        }
    }

    // Fallback: user hits Enter before /suggest results arrive.
    async function directSearch(text) {
        const seq = ++resolveSeq;
        closeSuggest();
        const params = new URLSearchParams({
            SingleLine: text,
            f: "json",
            maxLocations: "1",
            location: KEYS_CENTER,
        });
        try {
            const resp = await fetch(`${GEOCODE_BASE}/findAddressCandidates?${params}`);
            if (seq !== resolveSeq) return;
            const data = await resp.json();
            if (seq !== resolveSeq) return;
            const cand = (data.candidates || [])[0];
            if (!cand) return;
            await jumpToCandidate(cand);
        } catch (err) {
            if (seq !== resolveSeq) return;
            console.error("direct search failed:", err);
        }
    }

    $search.addEventListener("input", () => {
        const q = $search.value.trim();
        $searchClear.hidden = !q;
        clearTimeout(suggestTimer);
        if (!q) { closeSuggest(); return; }
        suggestTimer = setTimeout(() => fetchSuggestions(q), SUGGEST_DEBOUNCE_MS);
    });

    $search.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            if ($suggestList.hidden && $search.value.trim()) {
                fetchSuggestions($search.value.trim());
                return;
            }
            setActive(activeIdx + 1);
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive(activeIdx <= 0 ? 0 : activeIdx - 1);
        } else if (e.key === "Enter") {
            e.preventDefault();
            const q = $search.value.trim();
            if (activeIdx >= 0 && currentSuggestions[activeIdx]) {
                resolveAndJump(currentSuggestions[activeIdx]);
            } else if (currentSuggestions.length > 0) {
                resolveAndJump(currentSuggestions[0]);
            } else if (q) {
                directSearch(q);
            }
        } else if (e.key === "Escape") {
            closeSuggest();
            $search.blur();
        }
    });

    // mousedown — fires before the input's blur, so we don't lose the click
    $suggestList.addEventListener("mousedown", (e) => {
        const li = e.target.closest(".search-suggestion[data-idx]");
        if (!li) return;
        e.preventDefault();
        const idx = parseInt(li.dataset.idx, 10);
        if (currentSuggestions[idx]) resolveAndJump(currentSuggestions[idx]);
    });

    $search.addEventListener("focus", () => {
        const q = $search.value.trim();
        if (q && currentSuggestions.length === 0) fetchSuggestions(q);
        else if (currentSuggestions.length > 0) setExpanded(true);
    });

    $search.addEventListener("blur", () => {
        // delay so suggestion-click mousedown has a chance to land first
        setTimeout(closeSuggest, 120);
    });

    $searchClear.addEventListener("click", () => {
        $search.value = "";
        $searchClear.hidden = true;
        closeSuggest();
        $search.focus();
    });


    // ─── Basemap switch ────────────────────────────────────
    //
    // The button grid is populated from /basemaps (kicked off at module
    // load — see `_basemapsReady` above). The map itself was already
    // built with a hardcoded Esri default ("dark-gray-vector") so the
    // user sees a basemap from the first frame, independent of when
    // (or whether) the registry response lands. If /basemaps fails the
    // fallback list of six Esri entries renders instead — the switcher
    // is never absent.
    //
    // setBasemap() dispatches by provider:
    //   - "esri":        string assignment, same behavior as before.
    //   - "xyz":         WebTileLayer (with TileInfo override when the
    //                    registry specifies a non-default tile size, so
    //                    ArcGIS doesn't request the wrong row/col count
    //                    for a 512px tile source like MapTiler).
    //   - "arcgis_rest": TileLayer pointed at a MapServer URL. Tile
    //                    size and max-zoom come from the service's
    //                    published tile info — we deliberately do NOT
    //                    override them from the registry.
    // Marker / measure / bathymetry layers live in `map.layers` and are
    // untouched by `map.basemap` reassignment, so nothing to clean up.
    let currentBasemapId = "dark-gray-vector";  // matches the literal at map construction
    let basemapsById = {};

    function setBasemap(entry) {
        if (entry.provider === "esri") {
            map.basemap = entry.value;
            return;
        }
        if (entry.provider === "xyz") {
            const layerOpts = {
                urlTemplate: entry.value,
                copyright:   entry.attribution,
            };
            if (entry.tile_size != null) {
                layerOpts.tileInfo = TileInfo.create({
                    spatialReference: SpatialReference.WebMercator,
                    size: entry.tile_size,
                });
            }
            const layer = new WebTileLayer(layerOpts);
            if (entry.max_zoom != null) layer.maxZoom = entry.max_zoom;
            map.basemap = new Basemap({ baseLayers: [layer] });
            return;
        }
        if (entry.provider === "arcgis_rest") {
            const layer = new TileLayer({
                url:       entry.value,
                copyright: entry.attribution,
            });
            map.basemap = new Basemap({ baseLayers: [layer] });
            return;
        }
        console.warn("[basemap] unknown provider:", entry.provider, entry);
    }

    function renderBasemapGrid(entries) {
        $basemapGrid.innerHTML = "";
        for (const entry of entries) {
            const btn = document.createElement("button");
            btn.className = "basemap-btn";
            btn.dataset.basemapId = entry.id;
            btn.textContent = entry.display_name;
            if (entry.tooltip) btn.title = entry.tooltip;
            if (entry.id === currentBasemapId) btn.classList.add("active");
            $basemapGrid.appendChild(btn);
        }
    }

    $basemapGrid.addEventListener("click", (e) => {
        const btn = e.target.closest(".basemap-btn[data-basemap-id]");
        if (!btn) return;
        const id = btn.dataset.basemapId;
        if (id === currentBasemapId) return;
        const entry = basemapsById[id];
        if (!entry) return;
        setBasemap(entry);
        currentBasemapId = id;
        $basemapGrid.querySelectorAll(".basemap-btn")
            .forEach(b => b.classList.toggle("active", b === btn));
    });

    _basemapsReady.then((payload) => {
        const entries = (payload && payload.basemaps) || [];
        basemapsById = Object.fromEntries(entries.map(e => [e.id, e]));
        renderBasemapGrid(entries);
    });


    // ─── Sidebar jump-to-coords ────────────────────────────
    async function sidebarJump() {
        const lat = parseFloat($sidebarLat.value);
        const lon = parseFloat($sidebarLon.value);
        if (Number.isNaN(lat) || Number.isNaN(lon)) return;
        if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return;
        const point = new Point({ longitude: lon, latitude: lat });
        await view.goTo({ center: [lon, lat], zoom: Math.max(view.zoom, 11) });
        lookupDepth(lat, lon, point);
    }
    $sidebarGoto.addEventListener("click", (e) => { e.preventDefault(); sidebarJump(); });
    [$sidebarLat, $sidebarLon].forEach(el => {
        el.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); sidebarJump(); }
        });
    });


    // ─── Distance measurement tool ─────────────────────────
    // Multi-point path flow:
    //   1. "Start measuring" → measureMode = true. Map clicks now route
    //      to captureMeasurePoint instead of depth lookup.
    //   2. Each click appends a point. The path is rendered as one
    //      continuous polyline through all points + a marker per click.
    //      Total distance = sum of haversine over consecutive points,
    //      updated live after every click.
    //   3. "Finish" exits measure mode but keeps the path on screen.
    //   4. "Add points" (visible after Finish, when a path exists)
    //      re-enters mode and continues extending from the last point.
    //   5. "Undo" removes the most recent point. "Clear" wipes everything.
    //
    // Distance uses haversine on Earth-surface — accurate at any latitude.
    // Primary readout in nautical miles (marine context); secondary in
    // statute miles + km.

    let measureMode   = false;
    let measurePoints = [];   // [{lat, lon, point}] — the path

    const MEASURE_POINT_SYMBOL = {
        type: "simple-marker",
        style: "circle",
        color: [58, 163, 255, 0.95],
        size: 10,
        outline: { color: [255, 255, 255, 0.95], width: 1.5 },
    };
    const MEASURE_LINE_SYMBOL = {
        type: "simple-line",
        color: [58, 163, 255, 0.9],
        width: 2.5,
        style: "dash",
    };

    function haversineMeters(lat1, lon1, lat2, lon2) {
        const R = 6371008.8;  // mean Earth radius in metres (IUGG)
        const toRad = (d) => d * Math.PI / 180;
        const phi1 = toRad(lat1), phi2 = toRad(lat2);
        const dPhi = toRad(lat2 - lat1);
        const dLam = toRad(lon2 - lon1);
        const a = Math.sin(dPhi / 2) ** 2
                + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLam / 2) ** 2;
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    function formatDistance(metres) {
        const nm = metres / 1852;
        const mi = metres / 1609.344;
        const km = metres / 1000;
        const fmt = (n) => n >= 100 ? n.toFixed(0)
                          : n >= 10  ? n.toFixed(1)
                                     : n.toFixed(2);
        return {
            primary: `${fmt(nm)} nm`,
            secondary: `${fmt(mi)} mi · ${fmt(km)} km`,
        };
    }

    function totalPathMeters() {
        let total = 0;
        for (let i = 1; i < measurePoints.length; i++) {
            const a = measurePoints[i - 1];
            const b = measurePoints[i];
            total += haversineMeters(a.lat, a.lon, b.lat, b.lon);
        }
        return total;
    }

    // Re-render the entire path on every change. Polyline goes in first
    // so markers render on top of segment intersections (the dashed line
    // would otherwise visually cut through the marker centers).
    function renderMeasureLayer() {
        measureLayer.removeAll();
        if (measurePoints.length >= 2) {
            const path = measurePoints.map(p => [p.lon, p.lat]);
            measureLayer.add(new Graphic({
                geometry: new Polyline({
                    paths: [path],
                    spatialReference: { wkid: 4326 },
                }),
                symbol: MEASURE_LINE_SYMBOL,
            }));
        }
        for (const p of measurePoints) {
            measureLayer.add(new Graphic({
                geometry: p.point,
                symbol: MEASURE_POINT_SYMBOL,
            }));
        }
    }

    function updateReadout() {
        if (measurePoints.length < 2) {
            $measureReadout.classList.add("hidden");
            $measurePrimary.textContent   = "— nm";
            $measureSecondary.textContent = "—";
            return;
        }
        const { primary, secondary } = formatDistance(totalPathMeters());
        $measurePrimary.textContent   = primary;
        $measureSecondary.textContent = secondary;
        $measureReadout.classList.remove("hidden");
    }

    // Single source of truth for the button labels, hints, and tag. Drive
    // state machine off (measureMode, measurePoints.length).
    function syncMeasureUI() {
        const n = measurePoints.length;
        $workspace.classList.toggle("measuring", measureMode);
        $measureTag.classList.toggle("visible", measureMode);
        $measureStart.classList.toggle("active", measureMode);
        $measureUndo.disabled  = (n === 0);
        $measureClear.disabled = (n === 0);

        if (measureMode) {
            $measureStart.textContent = n === 0 ? "Cancel" : "Finish";
            $measureHint.textContent = n === 0
                ? "Click your starting point on the map."
                : `${n} point${n === 1 ? "" : "s"} placed. Click to extend, Finish when done.`;
        } else {
            if (n === 0) {
                $measureStart.textContent = "Start measuring";
                $measureHint.textContent  = "Click two or more points on the map to measure a path. Each leg sums into the total.";
            } else {
                $measureStart.textContent = "Add points";
                $measureHint.textContent  = `Path complete (${n} point${n === 1 ? "" : "s"}). Add more, Undo, or Clear.`;
            }
        }
    }

    function setMeasureMode(active) {
        measureMode = active;
        syncMeasureUI();
    }

    function clearMeasurement() {
        measurePoints = [];
        renderMeasureLayer();
        updateReadout();
        syncMeasureUI();
    }

    function undoLastPoint() {
        if (!measurePoints.length) return;
        measurePoints.pop();
        renderMeasureLayer();
        updateReadout();
        syncMeasureUI();
    }

    function captureMeasurePoint(mapPoint) {
        const lat = mapPoint.latitude;
        const lon = mapPoint.longitude;
        // Defensive — ArcGIS occasionally emits clicks outside Web Mercator
        // bounds when zooming out far. Skip rather than crash the tool.
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;

        measurePoints.push({ lat, lon, point: mapPoint });
        renderMeasureLayer();
        updateReadout();
        syncMeasureUI();
    }

    $measureStart.addEventListener("click", () => {
        if (measureMode) {
            // "Finish" (or "Cancel" when nothing placed) — exit mode,
            // keep whatever path exists on screen.
            setMeasureMode(false);
        } else {
            // "Start measuring" (no points) or "Add points" (extending).
            // Either way: enter mode without wiping the existing path.
            setMeasureMode(true);
        }
    });
    $measureUndo.addEventListener("click", undoLastPoint);
    $measureClear.addEventListener("click", () => {
        clearMeasurement();
        if (measureMode) setMeasureMode(false);
    });

    // Initial paint so the disabled state on Undo/Clear is correct.
    syncMeasureUI();


    // ─── Spotfinder ────────────────────────────────────────
    // Bottom-left FAB opens a panel; while the panel is open, the user
    // can left-click-drag on the map to draw a geo-anchored rectangle
    // (axis-aligned initially), after which the rectangle is in EDIT
    // MODE: corner / edge / rotation handles are shown, the body is
    // grab-to-translate, and a Shift-held rotation snaps to 15°.
    //
    // INTERACTION DISPATCH:
    //   On every drag start we hit-test the screen-space positions of
    //   the handles + the rectangle body. Routes:
    //     - on a handle    → resize / rotate (handle type decides)
    //     - inside body    → translate
    //     - outside        → start a new draw (cleanly replaces the
    //                        existing rectangle — that's the documented
    //                        "fresh draw replaces" policy)
    //
    //   ArcGIS's pan is suppressed by `event.stopPropagation()` while
    //   the panel is open, the same trick the original drag-to-draw
    //   implementation used. Wheel/pinch zoom continues to work.
    //
    // STATE:
    //   `area`        — the live SearchArea (from spotfinder-shape.js)
    //                   or null if nothing drawn yet.
    //   `dragState`   — null when idle; otherwise an object with `kind`
    //                   (`draw` / `translate` / `resize-corner` /
    //                   `resize-edge` / `rotate`) plus the per-drag
    //                   anchors needed to derive the next geometry.
    //
    // RENDERING:
    //   Two layers stacked above the bathymetry tiles:
    //     * spotfinderLayer        — the rectangle polygon itself
    //     * spotfinderHandleLayer  — handles + rotation arm + top-edge
    //                                tick. Re-drawn whenever the area
    //                                changes OR the view zoom/extent
    //                                changes (so the 20-px rotation arm
    //                                rescales with screen).
    //
    // Mutually exclusive with the measurement tool: entering Spotfinder
    // exits any active measurement session so the cursor/click
    // semantics don't conflict. Existing measurement geometry stays on
    // screen.

    const SHAPE = window.FishFinderSpotfinderShape;

    const $spotfinderFab       = document.getElementById("spotfinder-fab");
    const $spotfinderPanel     = document.getElementById("spotfinder-panel");
    const $spotfinderClose     = document.getElementById("spotfinder-panel-close");
    const $spotfinderStatusTag = document.getElementById("spotfinder-status-tag");
    const $spotfinderHint      = document.getElementById("spotfinder-hint");
    const $spotfinderCenter    = document.getElementById("spotfinder-center");
    const $spotfinderSize      = document.getElementById("spotfinder-size");
    const $spotfinderRotation  = document.getElementById("spotfinder-rotation");
    const $spotfinderArea      = document.getElementById("spotfinder-area");
    const $spotfinderRedraw    = document.getElementById("spotfinder-redraw-btn");
    const $spotfinderContinue  = document.getElementById("spotfinder-continue-btn");

    // A second graphics layer just for handles so the rectangle's fill
    // colour never bleeds onto them and so a handle redraw never
    // touches the rectangle polygon.
    const spotfinderHandleLayer = new GraphicsLayer({ listMode: "hide" });
    // Sits ABOVE spotfinderLayer (which itself sits among the runs +
    // marker layers; the runs activation code inserts heatmaps at
    // index 1 and spots just below marker, so handles need to land at
    // the very top so they're never obscured by an active heatmap).
    map.layers.add(spotfinderHandleLayer);

    const SPOTFINDER_FILL_SYMBOL = {
        type: "simple-fill",
        color: [179, 136, 255, 0.16],
        outline: { color: [179, 136, 255, 1.0], width: 2.5, style: "solid" },
    };
    const SPOTFINDER_DRAFT_SYMBOL = {
        type: "simple-fill",
        color: [179, 136, 255, 0.10],
        outline: { color: [179, 136, 255, 0.85], width: 1.5, style: "dash" },
    };
    // Handles always render upright on screen (simple-marker symbols
    // are not rotated by the underlying geometry), per spec.
    const HANDLE_CORNER_SYMBOL = {
        type: "simple-marker", style: "square", size: 11,
        color: [255, 255, 255, 1.0],
        outline: { color: [60, 30, 110, 1.0], width: 1.5 },
    };
    const HANDLE_EDGE_SYMBOL = {
        type: "simple-marker", style: "square", size: 9,
        color: [255, 255, 255, 1.0],
        outline: { color: [60, 30, 110, 1.0], width: 1.5 },
    };
    const HANDLE_ROTATION_SYMBOL = {
        type: "simple-marker", style: "circle", size: 13,
        color: [179, 136, 255, 1.0],
        outline: { color: [255, 255, 255, 1.0], width: 2 },
    };
    const HANDLE_ARM_SYMBOL = {
        type: "simple-line",
        color: [179, 136, 255, 1.0], width: 1.5, style: "solid",
    };
    // Top-edge orientation tick. A short stroke OUTWARD from the top
    // edge midpoint so the user can read which side is "up" at a
    // glance even when the rotation handle is hidden mid-drag.
    const HANDLE_TICK_SYMBOL = {
        type: "simple-line",
        color: [255, 255, 255, 0.95], width: 3, style: "solid",
    };

    // Hit-test radii in pixels. Corners win over edges when overlapping
    // (smaller target), so check corners first in `hitTestHandles`.
    const HIT_RADIUS_HANDLE   = 14;
    const HIT_RADIUS_ROTATION = 16;
    // Rotation arm length in screen pixels — the spec calls for ~20px,
    // slightly tuned up so the handle clears the top-edge tick.
    const ROTATION_ARM_PX = 24;
    // Top-edge tick length in pixels (purely cosmetic).
    const TOP_TICK_PX = 12;

    let spotfinderActive = false;    // panel open?
    let area = null;                 // live SearchArea, or null
    let dragState = null;            // see "STATE" comment above

    // ─── Formatting helpers ────────────────────────────────
    function fmtLatLng(lat, lng) {
        const ns = lat >= 0 ? "N" : "S";
        const ew = lng >= 0 ? "E" : "W";
        return `${Math.abs(lat).toFixed(4)}° ${ns}, `
             + `${Math.abs(lng).toFixed(4)}° ${ew}`;
    }
    function fmtSizeKm(m) {
        if (m < 1000) return `${m.toFixed(0)} m`;
        return `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
    }
    function fmtRotationDeg(deg) {
        // Wrap to (-180, 180] so the readout never shows e.g. "359°".
        let d = ((deg + 180) % 360 + 360) % 360 - 180;
        if (d === -180) d = 180;
        if (Math.abs(d) < 0.5) return "0° — axis-aligned";
        const dir = d > 0 ? "clockwise" : "counter-clockwise";
        return `${Math.abs(d).toFixed(d < 1 ? 1 : 0)}° ${dir}`;
    }

    function setSpotfinderStatus(state) {
        // state: "undrawn" | "drawing" | "defined"
        $spotfinderStatusTag.classList.remove("visible", "undrawn", "drawing", "defined");
        $spotfinderStatusTag.classList.add("visible", state);
        if (state === "drawing") {
            $spotfinderStatusTag.textContent = "Drawing…";
        } else if (state === "defined") {
            $spotfinderStatusTag.textContent = "Defined";
        } else {
            $spotfinderStatusTag.textContent = "Undrawn";
        }
    }

    function syncSpotfinderUI() {
        const hasArea = area !== null;
        // Continue is enabled the moment the rectangle has nonzero
        // area, regardless of rotation. width/height are guaranteed >0
        // by the drag-end "reject degenerate" guard.
        $spotfinderRedraw.disabled   = !hasArea;
        $spotfinderContinue.disabled = !hasArea;
        if (hasArea) {
            setSpotfinderStatus("defined");
            $spotfinderCenter.textContent   = fmtLatLng(area.center.lat, area.center.lng);
            $spotfinderSize.textContent     = `${fmtSizeKm(area.width_m)} × ${fmtSizeKm(area.height_m)}`;
            $spotfinderRotation.textContent = fmtRotationDeg(area.rotation_deg);
            $spotfinderArea.textContent     = `${SHAPE.areaKm2(area).toFixed(2)} km²`;
            $spotfinderHint.textContent =
                "Drag handles to resize or rotate, the rectangle body to move. "
              + "Press Continue when ready.";
        } else if (dragState && dragState.kind === "draw") {
            setSpotfinderStatus("drawing");
            $spotfinderCenter.textContent   = "—";
            $spotfinderSize.textContent     = "—";
            $spotfinderRotation.textContent = "—";
            $spotfinderArea.textContent     = "—";
            $spotfinderHint.textContent = "Release to lock in the rectangle.";
        } else {
            setSpotfinderStatus("undrawn");
            $spotfinderCenter.textContent   = "—";
            $spotfinderSize.textContent     = "—";
            $spotfinderRotation.textContent = "—";
            $spotfinderArea.textContent     = "—";
            $spotfinderHint.textContent =
                "Drag on the map to draw a rectangle. After drawing, "
              + "use the handles to resize, rotate, or move it.";
        }
    }


    // ─── Rect rendering ────────────────────────────────────
    function ringFromCorners(corners) {
        // corners: TL, TR, BR, BL (lat/lng). Close the ring with TL again.
        const c = corners;
        return [
            [c[0].lng, c[0].lat],
            [c[1].lng, c[1].lat],
            [c[2].lng, c[2].lat],
            [c[3].lng, c[3].lat],
            [c[0].lng, c[0].lat],
        ];
    }

    function drawRectFromCorners(corners, symbol) {
        spotfinderLayer.removeAll();
        spotfinderLayer.add(new Graphic({
            geometry: new Polygon({
                rings: [ringFromCorners(corners)],
                spatialReference: { wkid: 4326 },
            }),
            symbol,
        }));
    }

    function renderArea() {
        if (!area) {
            spotfinderLayer.removeAll();
            spotfinderHandleLayer.removeAll();
            return;
        }
        drawRectFromCorners(area.corners, SPOTFINDER_FILL_SYMBOL);
        renderHandles();
    }

    /**
     * Re-render the handles. The rotation arm + top-edge tick are
     * positioned in SCREEN pixel space (so they stay visually
     * constant-size across zoom levels), then back-projected to map
     * coordinates. Handles themselves anchor at known map locations
     * (the corners + edge midpoints) so they don't need re-projection
     * on each frame — only the screen-space accents do.
     */
    function renderHandles() {
        spotfinderHandleLayer.removeAll();
        if (!area) return;
        if (dragState && dragState.kind === "draw") return;  // draw mode hides handles

        const corners = area.corners;
        // Corner handles (4). attributes.handleType is what
        // hitTestHandles dispatches on.
        for (let i = 0; i < 4; i++) {
            spotfinderHandleLayer.add(new Graphic({
                geometry: new Point({
                    longitude: corners[i].lng, latitude: corners[i].lat,
                    spatialReference: { wkid: 4326 },
                }),
                symbol: HANDLE_CORNER_SYMBOL,
                attributes: { handleType: "corner", cornerIdx: i },
            }));
        }

        // Edge midpoint handles (4). Index 0=top (TL-TR mid), 1=right
        // (TR-BR), 2=bottom (BR-BL), 3=left (BL-TL). Order matters for
        // the resize logic in `applyResizeEdge`.
        const edgeMids = edgeMidpointsLatLng(corners);
        for (let i = 0; i < 4; i++) {
            spotfinderHandleLayer.add(new Graphic({
                geometry: new Point({
                    longitude: edgeMids[i].lng, latitude: edgeMids[i].lat,
                    spatialReference: { wkid: 4326 },
                }),
                symbol: HANDLE_EDGE_SYMBOL,
                attributes: { handleType: "edge", edgeIdx: i },
            }));
        }

        // Rotation handle + arm + top tick. All three live in screen
        // space so they look right at any zoom level.
        const topMidScr = view.toScreen(new Point({
            longitude: edgeMids[0].lng, latitude: edgeMids[0].lat,
            spatialReference: { wkid: 4326 },
        }));
        const centerScr = view.toScreen(new Point({
            longitude: area.center.lng, latitude: area.center.lat,
            spatialReference: { wkid: 4326 },
        }));
        if (topMidScr && centerScr) {
            // Outward unit vector (from center to top midpoint).
            const dx = topMidScr.x - centerScr.x;
            const dy = topMidScr.y - centerScr.y;
            const mag = Math.hypot(dx, dy) || 1;
            const ux = dx / mag, uy = dy / mag;

            // Rotation handle position.
            const handleScr = {
                x: topMidScr.x + ux * ROTATION_ARM_PX,
                y: topMidScr.y + uy * ROTATION_ARM_PX,
            };
            const handleMap = view.toMap(handleScr);
            if (handleMap) {
                spotfinderHandleLayer.add(new Graphic({
                    geometry: new Polyline({
                        paths: [[
                            [edgeMids[0].lng, edgeMids[0].lat],
                            [handleMap.longitude, handleMap.latitude],
                        ]],
                        spatialReference: { wkid: 4326 },
                    }),
                    symbol: HANDLE_ARM_SYMBOL,
                }));
                spotfinderHandleLayer.add(new Graphic({
                    geometry: new Point({
                        longitude: handleMap.longitude,
                        latitude: handleMap.latitude,
                        spatialReference: { wkid: 4326 },
                    }),
                    symbol: HANDLE_ROTATION_SYMBOL,
                    attributes: { handleType: "rotation" },
                }));
            }

            // Top-edge orientation tick: a short outward stroke from
            // the top midpoint. Same direction as the rotation arm,
            // shorter and white so it doesn't look like another handle.
            const tickStart = topMidScr;
            const tickEnd = {
                x: topMidScr.x + ux * TOP_TICK_PX,
                y: topMidScr.y + uy * TOP_TICK_PX,
            };
            const tickStartMap = view.toMap(tickStart);
            const tickEndMap   = view.toMap(tickEnd);
            if (tickStartMap && tickEndMap) {
                spotfinderHandleLayer.add(new Graphic({
                    geometry: new Polyline({
                        paths: [[
                            [tickStartMap.longitude, tickStartMap.latitude],
                            [tickEndMap.longitude,   tickEndMap.latitude],
                        ]],
                        spatialReference: { wkid: 4326 },
                    }),
                    symbol: HANDLE_TICK_SYMBOL,
                }));
            }
        }
    }

    function edgeMidpointsLatLng(corners) {
        // Midpoints in lat/lng — fine at this scale. (Mercator midpoints
        // would round-trip identically for the purpose of drawing the
        // handles, since we never reverse-engineer them into local-frame
        // distances; resize uses the corner geometry directly.)
        const mid = (a, b) => ({ lat: (a.lat + b.lat) / 2, lng: (a.lng + b.lng) / 2 });
        return [
            mid(corners[0], corners[1]),   // top    (TL-TR)
            mid(corners[1], corners[2]),   // right  (TR-BR)
            mid(corners[2], corners[3]),   // bottom (BR-BL)
            mid(corners[3], corners[0]),   // left   (BL-TL)
        ];
    }


    // ─── Hit testing (screen-space) ────────────────────────
    function distSq(ax, ay, bx, by) {
        const dx = ax - bx, dy = ay - by;
        return dx * dx + dy * dy;
    }

    /**
     * Project a {lat, lng} to screen pixels. Returns null if the point
     * is offscreen / unprojectable (happens far from the viewport).
     */
    function toScreen(latLng) {
        return view.toScreen(new Point({
            longitude: latLng.lng, latitude: latLng.lat,
            spatialReference: { wkid: 4326 },
        }));
    }

    /**
     * Returns the handle hit at screen-space (sx, sy), or null. Corner
     * handles win over edge handles when overlapping (smaller visual
     * target, so the user's intent is corner). The rotation handle is
     * checked separately and at a generous radius.
     */
    function hitTestHandles(sx, sy) {
        if (!area) return null;
        const corners = area.corners;
        // Corners first.
        for (let i = 0; i < 4; i++) {
            const p = toScreen(corners[i]);
            if (p && distSq(sx, sy, p.x, p.y) <= HIT_RADIUS_HANDLE * HIT_RADIUS_HANDLE) {
                return { handleType: "corner", cornerIdx: i };
            }
        }
        // Edges.
        const edgeMids = edgeMidpointsLatLng(corners);
        for (let i = 0; i < 4; i++) {
            const p = toScreen(edgeMids[i]);
            if (p && distSq(sx, sy, p.x, p.y) <= HIT_RADIUS_HANDLE * HIT_RADIUS_HANDLE) {
                return { handleType: "edge", edgeIdx: i };
            }
        }
        // Rotation handle (position derived from top midpoint, same
        // math as renderHandles).
        const topMidScr = toScreen(edgeMids[0]);
        const centerScr = toScreen(area.center);
        if (topMidScr && centerScr) {
            const dx = topMidScr.x - centerScr.x;
            const dy = topMidScr.y - centerScr.y;
            const mag = Math.hypot(dx, dy) || 1;
            const ux = dx / mag, uy = dy / mag;
            const hx = topMidScr.x + ux * ROTATION_ARM_PX;
            const hy = topMidScr.y + uy * ROTATION_ARM_PX;
            if (distSq(sx, sy, hx, hy) <= HIT_RADIUS_ROTATION * HIT_RADIUS_ROTATION) {
                return { handleType: "rotation" };
            }
        }
        return null;
    }

    /**
     * Point-in-rectangle test, in screen space. Used to dispatch a
     * drag that didn't land on a handle: inside → translate, outside →
     * new draw. Uses the standard "point on left side of every edge
     * (consistent winding)" check that works for any convex quad.
     */
    function pointInRect(sx, sy) {
        if (!area) return false;
        const c = area.corners;
        const screen = c.map(toScreen);
        if (screen.some(p => !p)) return false;
        // Consistent CW winding (TL → TR → BR → BL in lat/lng maps to
        // CW in screen coords because the screen y-axis is flipped).
        // For each edge, check that the test point is on the right
        // side. If any edge says otherwise, it's outside.
        let sign = 0;
        for (let i = 0; i < 4; i++) {
            const a = screen[i];
            const b = screen[(i + 1) % 4];
            const cross = (b.x - a.x) * (sy - a.y) - (b.y - a.y) * (sx - a.x);
            if (cross === 0) continue;
            const s = cross > 0 ? 1 : -1;
            if (sign === 0) sign = s;
            else if (s !== sign) return false;
        }
        return true;
    }


    // ─── Geometry mutations ────────────────────────────────
    function projectArea() {
        // Cached Mercator centre — used by every resize / rotate math
        // path. Recomputed per drag (called once on "start") because
        // the centre can move mid-drag and we want the same anchor
        // throughout a single drag.
        return SHAPE.project(area.center.lat, area.center.lng);
    }
    function projectLatLng(p) {
        return SHAPE.project(p.lat, p.lng);
    }
    function unprojectXY(x, y) {
        return SHAPE.unproject(x, y);
    }

    function buildAreaFrom(centerLat, centerLng, widthM, heightM, rotationDeg) {
        // Minimum 1 m so we never produce a degenerate SearchArea.
        return SHAPE.buildSearchArea(
            centerLat, centerLng,
            Math.max(1, widthM), Math.max(1, heightM),
            rotationDeg
        );
    }

    function startResizeCorner(cornerIdx, _startMapPt) {
        // Opposite corner (index + 2 mod 4) stays fixed for the whole
        // drag. Snapshot it in Mercator now so subsequent drag updates
        // don't have to fight floating-point drift.
        const oppIdx = (cornerIdx + 2) % 4;
        const opposite = projectLatLng(area.corners[oppIdx]);
        return {
            kind: "resize-corner",
            cornerIdx,
            oppositeMerc: opposite,
            rotationDeg: area.rotation_deg,
        };
    }
    function applyResizeCorner(state, mapPt) {
        // Drag point in Mercator.
        const drag = SHAPE.project(mapPt.latitude, mapPt.longitude);
        const opp  = state.oppositeMerc;
        // Vector from opposite corner to drag point.
        const vx = drag.x - opp.x;
        const vy = drag.y - opp.y;
        // Express in the local frame (un-rotate by -rotation_deg).
        const local = SHAPE.rotateClockwise(vx, vy, -state.rotationDeg);
        // The local vector spans the whole rectangle (opposite → dragged
        // corner is the rect's diagonal). Width/height in Mercator are
        // |local.dx|, |local.dy|.
        const widthMerc  = Math.abs(local.dx);
        const heightMerc = Math.abs(local.dy);
        // New centre = midpoint of opposite + drag in Mercator → lat/lng.
        const cx = (opp.x + drag.x) / 2;
        const cy = (opp.y + drag.y) / 2;
        const center = unprojectXY(cx, cy);
        // Convert Mercator → ground metres at the NEW centre latitude.
        const scale = SHAPE.mercatorScaleAt(center.lat);
        area = buildAreaFrom(
            center.lat, center.lng,
            widthMerc * scale, heightMerc * scale,
            state.rotationDeg,
        );
    }

    function startResizeEdge(edgeIdx, _startMapPt) {
        // The OPPOSITE edge stays fixed. Snapshot its two corners in
        // Mercator (= a line segment that the new rectangle's
        // opposite-edge corners must continue to lie on). The
        // OTHER dimension (perpendicular to the edge being dragged)
        // also stays fixed.
        const oppEdgeIdx = (edgeIdx + 2) % 4;
        // Each edge connects corners[edgeIdx] and corners[edgeIdx+1].
        const oppA = projectLatLng(area.corners[oppEdgeIdx]);
        const oppB = projectLatLng(area.corners[(oppEdgeIdx + 1) % 4]);
        return {
            kind:         "resize-edge",
            edgeIdx,
            oppAMerc:     oppA,
            oppBMerc:     oppB,
            rotationDeg:  area.rotation_deg,
            // The dimension perpendicular to the dragged edge changes;
            // the parallel dimension stays at this value:
            keptDimM:     (edgeIdx % 2 === 0) ? area.width_m : area.height_m,
        };
    }
    function applyResizeEdge(state, mapPt) {
        const drag = SHAPE.project(mapPt.latitude, mapPt.longitude);
        // Midpoint of the opposite edge stays anchored.
        const oppMidX = (state.oppAMerc.x + state.oppBMerc.x) / 2;
        const oppMidY = (state.oppAMerc.y + state.oppBMerc.y) / 2;
        // Vector from opposite-edge midpoint to drag point. Project
        // onto the local axis perpendicular to the kept dimension.
        const vx = drag.x - oppMidX;
        const vy = drag.y - oppMidY;
        const local = SHAPE.rotateClockwise(vx, vy, -state.rotationDeg);
        // edgeIdx 0=top → moves along +local.dy; 2=bottom → -local.dy.
        // edgeIdx 1=right → +local.dx; 3=left → -local.dx.
        // Either way, the new dimension is |projection along that axis|.
        const movingHeight = (state.edgeIdx === 0 || state.edgeIdx === 2);
        const projection = movingHeight ? local.dy : local.dx;
        const newDimMerc = Math.abs(projection);
        // New centre lies halfway between opp midpoint and drag point
        // ALONG the perpendicular axis only. Translate opp midpoint by
        // projection / 2 in that axis (in local frame), then rotate
        // back into Mercator.
        const halfLocal = { dx: 0, dy: 0 };
        if (movingHeight) halfLocal.dy = projection / 2;
        else              halfLocal.dx = projection / 2;
        const halfMerc = SHAPE.rotateClockwise(halfLocal.dx, halfLocal.dy, state.rotationDeg);
        const cx = oppMidX + halfMerc.dx;
        const cy = oppMidY + halfMerc.dy;
        const center = unprojectXY(cx, cy);
        const scale = SHAPE.mercatorScaleAt(center.lat);
        const newDimM = newDimMerc * scale;
        const widthM  = movingHeight ? state.keptDimM : newDimM;
        const heightM = movingHeight ? newDimM       : state.keptDimM;
        area = buildAreaFrom(center.lat, center.lng,
                             widthM, heightM, state.rotationDeg);
    }

    function startRotate(_startMapPt) {
        // The rotation handle is being dragged. We don't actually need
        // a "start angle" anchor because we recompute the absolute
        // angle every frame (from centre → cursor); we just snapshot
        // the dimensions + centre so the rotate path doesn't fight
        // floating-point drift from buildSearchArea ↔ unproject.
        return {
            kind:    "rotate",
            centerMerc: projectArea(),
            widthM:  area.width_m,
            heightM: area.height_m,
            centerLat: area.center.lat,
            centerLng: area.center.lng,
        };
    }
    function applyRotate(state, mapPt, shiftKey) {
        const drag = SHAPE.project(mapPt.latitude, mapPt.longitude);
        // Vector from centre to drag.
        const vx = drag.x - state.centerMerc.x;
        const vy = drag.y - state.centerMerc.y;
        // Clockwise angle from north: atan2(east, north) = atan2(vx, vy).
        let deg = Math.atan2(vx, vy) * 180 / Math.PI;
        if (shiftKey) {
            // 15° snap per spec. Round to nearest 15°.
            deg = Math.round(deg / 15) * 15;
        }
        area = buildAreaFrom(
            state.centerLat, state.centerLng,
            state.widthM, state.heightM,
            deg,
        );
    }

    function startTranslate(startMapPt) {
        return {
            kind:        "translate",
            startMerc:   SHAPE.project(startMapPt.latitude, startMapPt.longitude),
            startCenterMerc: projectArea(),
            widthM:      area.width_m,
            heightM:     area.height_m,
            rotationDeg: area.rotation_deg,
        };
    }
    function applyTranslate(state, mapPt) {
        const drag = SHAPE.project(mapPt.latitude, mapPt.longitude);
        const dx = drag.x - state.startMerc.x;
        const dy = drag.y - state.startMerc.y;
        const newCenterMerc = {
            x: state.startCenterMerc.x + dx,
            y: state.startCenterMerc.y + dy,
        };
        const center = unprojectXY(newCenterMerc.x, newCenterMerc.y);
        area = buildAreaFrom(center.lat, center.lng,
                             state.widthM, state.heightM, state.rotationDeg);
    }


    // ─── Draw (initial) ───────────────────────────────────
    function startDraw(startMapPt) {
        return {
            kind:    "draw",
            startLatLng: { lat: startMapPt.latitude, lng: startMapPt.longitude },
            // Track the latest position for the live preview symbol.
            lastLatLng: null,
        };
    }
    function applyDraw(state, mapPt) {
        state.lastLatLng = { lat: mapPt.latitude, lng: mapPt.longitude };
        // Build an axis-aligned SearchArea from the drag rectangle.
        // The drag is in lat/lng; convert to centre + width/height in
        // metres at the centre latitude via the shape helpers.
        const a = state.startLatLng, b = state.lastLatLng;
        const north = Math.max(a.lat, b.lat);
        const south = Math.min(a.lat, b.lat);
        const east  = Math.max(a.lng, b.lng);
        const west  = Math.min(a.lng, b.lng);
        const draftArea = SHAPE.searchAreaFromBbox({ north, south, east, west });
        // Live preview uses the dashed draft symbol — handles stay
        // hidden during the initial draw.
        drawRectFromCorners(draftArea.corners, SPOTFINDER_DRAFT_SYMBOL);
        return draftArea;
    }


    // ─── Drag dispatch ────────────────────────────────────
    function clearSpotfinder() {
        area = null;
        dragState = null;
        spotfinderLayer.removeAll();
        spotfinderHandleLayer.removeAll();
    }

    function openSpotfinderPanel() {
        // Mutually exclusive with measure mode — would otherwise fight
        // for the click/drag semantics.
        if (measureMode) setMeasureMode(false);
        spotfinderActive = true;
        $workspace.classList.add("spotfinder-active");
        $spotfinderPanel.classList.remove("collapsed");
        $spotfinderFab.classList.add("hidden");
        $spotfinderFab.setAttribute("aria-expanded", "true");
        syncSpotfinderUI();
    }

    function closeSpotfinderPanel() {
        spotfinderActive = false;
        $workspace.classList.remove("spotfinder-active");
        setHoverCursor(null);
        $spotfinderPanel.classList.add("collapsed");
        $spotfinderFab.classList.remove("hidden");
        $spotfinderFab.setAttribute("aria-expanded", "false");
        // Closing cleans up everything related to drawing per the
        // brief — drop the rectangle, status, and any in-flight drag.
        clearSpotfinder();
        syncSpotfinderUI();
    }

    $spotfinderFab.addEventListener("click", openSpotfinderPanel);
    $spotfinderClose.addEventListener("click", closeSpotfinderPanel);

    // Keyboard accessibility: real <button>, so Enter/Space already
    // activate it. Add Escape-to-close while the panel is open so
    // keyboard users can bail without reaching for the mouse.
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && spotfinderActive) {
            e.preventDefault();
            closeSpotfinderPanel();
        }
    });

    $spotfinderRedraw.addEventListener("click", () => {
        clearSpotfinder();
        syncSpotfinderUI();
    });

    $spotfinderContinue.addEventListener("click", () => {
        if (!area) return;
        const enc = SHAPE.encodeForUrl(area);
        // Also include legacy n/s/e/w so older bookmarks and any
        // outside tool that consumed the old URL shape keep working.
        // Old consumers will read it as the AABB, which is correct
        // up to "they don't know about the rotation".
        enc.n = area.bbox.north.toFixed(6);
        enc.s = area.bbox.south.toFixed(6);
        enc.e = area.bbox.east.toFixed(6);
        enc.w = area.bbox.west.toFixed(6);
        const q = new URLSearchParams(enc);
        window.location.href = `/spotfinder?${q.toString()}`;
    });


    // ─── Cursor hover hint ─────────────────────────────────
    // We attach a single set of classes to the workspace and toggle
    // them based on hit-test results. Each class maps to a cursor that
    // hints at the action the user would get if they dragged here.
    function setHoverCursor(kind) {
        const wsc = $workspace.classList;
        wsc.remove("sf-cursor-grab", "sf-cursor-rotate",
                   "sf-cursor-resize", "sf-cursor-move");
        if (kind) wsc.add(`sf-cursor-${kind}`);
    }
    view.on("pointer-move", (event) => {
        if (!spotfinderActive) return;
        // Mid-drag: cursor stays at whatever the drag set, never
        // hover-derived (which would be wrong while resizing).
        if (dragState) return;
        if (!area) { setHoverCursor(null); return; }
        const hit = hitTestHandles(event.x, event.y);
        if (hit) {
            if (hit.handleType === "rotation") setHoverCursor("rotate");
            else                                setHoverCursor("resize");
        } else if (pointInRect(event.x, event.y)) {
            setHoverCursor("move");
        } else {
            setHoverCursor(null);
        }
    });
    view.on("pointer-leave", () => { if (spotfinderActive) setHoverCursor(null); });


    // ─── Drag pipeline ─────────────────────────────────────
    view.on("drag", (event) => {
        if (!spotfinderActive) return;
        if (event.button !== 0) return;       // ignore right/middle drag
        event.stopPropagation();

        const mapPt = view.toMap({ x: event.x, y: event.y });
        if (!mapPt
            || !Number.isFinite(mapPt.latitude)
            || !Number.isFinite(mapPt.longitude)) return;

        const shiftKey = !!(event.native && event.native.shiftKey);

        if (event.action === "start") {
            // Decide which drag-mode this is BEFORE doing anything
            // destructive to `area`. Order: handle hit > body interior
            // > otherwise a fresh draw (replaces).
            if (area) {
                const hit = hitTestHandles(event.x, event.y);
                if (hit) {
                    if (hit.handleType === "corner") {
                        dragState = startResizeCorner(hit.cornerIdx, mapPt);
                    } else if (hit.handleType === "edge") {
                        dragState = startResizeEdge(hit.edgeIdx, mapPt);
                    } else if (hit.handleType === "rotation") {
                        dragState = startRotate(mapPt);
                    }
                    return;
                }
                if (pointInRect(event.x, event.y)) {
                    dragState = startTranslate(mapPt);
                    return;
                }
            }
            // Either no rectangle exists or the drag started outside →
            // replace cleanly. Hand the live preview a new draft area.
            area = null;
            dragState = startDraw(mapPt);
            spotfinderLayer.removeAll();
            spotfinderHandleLayer.removeAll();
            syncSpotfinderUI();
            return;
        }

        if (!dragState) return;  // drag we never claimed

        if (event.action === "update") {
            if (dragState.kind === "draw") {
                applyDraw(dragState, mapPt);
                syncSpotfinderUI();
            } else if (dragState.kind === "translate") {
                applyTranslate(dragState, mapPt);
                renderArea();
                syncSpotfinderUI();
            } else if (dragState.kind === "resize-corner") {
                applyResizeCorner(dragState, mapPt);
                renderArea();
                syncSpotfinderUI();
            } else if (dragState.kind === "resize-edge") {
                applyResizeEdge(dragState, mapPt);
                renderArea();
                syncSpotfinderUI();
            } else if (dragState.kind === "rotate") {
                applyRotate(dragState, mapPt, shiftKey);
                renderArea();
                syncSpotfinderUI();
            }
        } else if (event.action === "end") {
            if (dragState.kind === "draw") {
                const finalArea = dragState.lastLatLng
                    ? applyDraw(dragState, mapPt)
                    : null;
                dragState = null;
                if (!finalArea
                    || finalArea.width_m < 1 || finalArea.height_m < 1) {
                    // Treat as a click (no movement). Clear any draft
                    // preview and stay in undrawn state.
                    area = null;
                    spotfinderLayer.removeAll();
                    spotfinderHandleLayer.removeAll();
                    syncSpotfinderUI();
                    return;
                }
                area = finalArea;
                renderArea();
                syncSpotfinderUI();
            } else {
                // resize / rotate / translate end — geometry is already
                // up to date from the last "update".
                dragState = null;
                renderArea();
                syncSpotfinderUI();
            }
        }
    });

    // The rotation arm + top-edge tick are positioned in screen
    // pixels and back-projected to map coords, so they need a refresh
    // whenever the screen projection changes (pan/zoom). Throttle to
    // one re-render per animation frame — `view.extent` fires
    // continuously during a pan and a per-event redraw would churn
    // GraphicsLayer for no visible benefit.
    let _handleRedrawQueued = false;
    function queueRenderHandles() {
        if (_handleRedrawQueued) return;
        _handleRedrawQueued = true;
        requestAnimationFrame(() => {
            _handleRedrawQueued = false;
            if (spotfinderActive && area && !dragState) renderHandles();
        });
    }
    view.watch("extent", queueRenderHandles);

    // Initial UI sync (so the status tag has a class and the buttons
    // are in their disabled state from the first frame).
    syncSpotfinderUI();


    // ─── Spotfinder runs (sidebar doorway + fullscreen modal) ──────
    //
    // STATE MODEL
    //   runs        : Map<run_id, SpotfinderResult>      every saved run
    //   activeIds   : Set<run_id>                        currently on map
    //   runLayers   : Map<run_id, {heatmapLayer, spotsLayer}>
    //                  only populated for active runs — heatmap MediaLayers
    //                  are heavy (a full georeferenced PNG per run), so we
    //                  build them on activation and tear them down on
    //                  deactivation rather than holding every run's layers
    //                  in memory permanently.
    //
    // LAYERING
    //   All heatmaps stack just above the bathymetry tile layer (index 0).
    //   All spots stack just below markerLayer so they paint on top of
    //   every heatmap regardless of which run was activated when —
    //   otherwise a later run's heatmap would obscure an earlier run's
    //   spots. Toolbar graphics (depth marker, measure path, draft
    //   Spotfinder rectangle) sit above everything.
    //
    // UI
    //   Sidebar shows one button ("Spotfinder runs — N saved · M active").
    //   Clicking opens a fullscreen modal with a card per run, a global
    //   opacity slider, and Apply/select-all/clear actions. Local
    //   selection state inside the modal lets the user explore without
    //   touching the map until they hit Apply.
    //
    // SPOT INTERACTION
    //   Spots are purely visual markers — clicks fall through to the
    //   depth lookup. The earlier "click a spot → open spot card" flow
    //   was removed because the card blocked the depth card the rest of
    //   the app uses. See the TODO above buildSpotsLayer for waypoint
    //   save plans.

    const STORE = window.FishFinderSpotfinderStorage;

    const $runsButton    = document.getElementById("sf-runs-open-btn");
    const $runsButtonSub = document.getElementById("sf-runs-button-sub");

    const $modal           = document.getElementById("sf-runs-modal");
    const $modalBackdrop   = document.getElementById("sf-runs-modal-backdrop");
    const $modalClose      = document.getElementById("sf-runs-modal-close");
    const $modalBody       = document.getElementById("sf-runs-modal-body");
    const $modalCounter    = document.getElementById("sf-runs-modal-counter");
    const $modalSelectAll  = document.getElementById("sf-runs-select-all");
    const $modalClear      = document.getElementById("sf-runs-clear");
    const $modalApply      = document.getElementById("sf-runs-apply");
    const $modalOpacity    = document.getElementById("sf-runs-global-opacity");
    const $modalOpacityVal = document.getElementById("sf-runs-global-opacity-val");

    /** @type {Map<string, object>}                       */ const runs       = new Map();
    /** @type {Set<string>}                               */ const activeIds  = new Set();
    /** @type {Map<string, {heatmapLayer, spotsLayer}>}   */ const runLayers  = new Map();

    let globalOpacity = STORE.getGlobalOpacity();


    // ─── Formatting helpers ───────────────────────────────
    function fmtRunTimestamp(iso) {
        try {
            const d = new Date(iso);
            return d.toLocaleString(undefined, {
                month: "short", day: "numeric",
                hour: "numeric", minute: "2-digit",
            });
        } catch { return iso; }
    }
    function fmtBboxCenter(bbox) {
        const lat = (bbox.north + bbox.south) / 2;
        const lon = (bbox.east  + bbox.west)  / 2;
        return `${lat.toFixed(2)}°, ${lon.toFixed(2)}°`;
    }

    /**
     * Scale factor that shrinks the rotated thumbnail just enough so
     * its corners stay inside the aspect-ratio'd container. For a
     * square box, the worst case is 45°, where the rotated image
     * needs to fit a 1/√2 ≈ 0.707 scale. We can't know the exact
     * thumbnail+image aspect ratio at format time without extra
     * layout work, so we approximate with the worst case — slightly
     * conservative but never clips.
     */
    function _thumbnailFitScale(rotationDeg) {
        const r = (rotationDeg * Math.PI / 180);
        const c = Math.abs(Math.cos(r));
        const s = Math.abs(Math.sin(r));
        // sqrt(2)/2 at 45°, 1 at 0°/90°.
        return 1 / (c + s);
    }


    // ─── Layer builders ───────────────────────────────────
    // Bigger marker for higher score so the visual hierarchy matches
    // the data. Spots are purely visual markers — clicks fall through
    // to the depth lookup (no popup, no hitTest interception).
    // TODO: re-add waypoint save flow (right-click menu? sidebar action?)
    // when we wire up a persistent waypoint store.
    const SPOT_SYMBOL_BASE = {
        type: "simple-marker",
        style: "circle",
        color: [179, 136, 255, 0.92],
        outline: { color: [255, 255, 255, 0.95], width: 1.5 },
    };
    function spotSymbol(score) {
        return { ...SPOT_SYMBOL_BASE, size: 8 + score * 8 };
    }

    // Region overlay palette. One colour per class so the user can read
    // the labelled-region map at a glance. Centerlines for linear
    // classes use the same hue at higher opacity.
    const CLASS_RGB = {
        pinnacle: [255, 130,  90],
        ridge:    [255, 200,  90],
        ledge:    [255, 220, 130],
        hole:     [120, 180, 255],
        channel:  [ 90, 200, 255],
        saddle:   [200, 140, 255],
    };
    function classFill(cls, score) {
        const rgb = CLASS_RGB[cls] || [180, 180, 180];
        const alpha = 0.10 + 0.18 * Math.max(0, Math.min(1, score));
        return {
            type: "simple-fill",
            color: [rgb[0], rgb[1], rgb[2], alpha],
            outline: {
                color: [rgb[0], rgb[1], rgb[2], 0.95],
                width: 1.4,
                style: "solid",
            },
        };
    }
    function classCenterline(cls) {
        const rgb = CLASS_RGB[cls] || [255, 255, 255];
        return {
            type: "simple-line",
            color: [rgb[0], rgb[1], rgb[2], 0.95],
            width: 2.6,
            style: "solid",
        };
    }
    const COMPOSITE_FILL_SYMBOL = {
        type: "simple-fill",
        color: [255, 215, 100, 0.06],
        outline: {
            color: [255, 215, 100, 0.95],
            width: 2,
            style: "short-dash",
        },
    };

    function buildHeatmapLayer(result) {
        // The PNG is oriented as if the rectangle were axis-aligned
        // (the runner generates it in the rectangle's LOCAL frame —
        // canvas top edge = rectangle top edge). To draw it rotated on
        // the map we use ExtentAndRotationGeoreference, which:
        //   1. places the image inside the supplied extent (un-rotated)
        //   2. then rotates the placed image around its own centre.
        //
        // We work in Web Mercator metres so the extent's width/height
        // can be derived directly from the SearchArea's true ground
        // metres (Mercator stretches by 1/cos(lat); we undo that to
        // convert ground metres → Mercator metres). Doing the same in
        // WGS84 degrees would distort the aspect ratio at any
        // significant latitude.
        const area = result.search_area;
        const c = SHAPE.project(area.center.lat, area.center.lng);
        const scale = SHAPE.mercatorScaleAt(area.center.lat);
        const hw = (area.width_m  / 2) / Math.max(1e-9, scale);
        const hh = (area.height_m / 2) / Math.max(1e-9, scale);
        const extent = new Extent({
            xmin: c.x - hw, ymin: c.y - hh,
            xmax: c.x + hw, ymax: c.y + hh,
            spatialReference: SpatialReference.WebMercator,
        });
        const elem = new ImageElement({
            image: result.heatmap_png_url,
            georeference: new ExtentAndRotationGeoreference({
                extent,
                // ArcGIS's rotation is clockwise (positive) in screen
                // orientation. Our rotation_deg is clockwise from
                // north, which matches.
                rotation: area.rotation_deg,
            }),
        });
        return new MediaLayer({
            source: [elem],
            opacity: globalOpacity,
            listMode: "hide",
        });
    }
    function buildSpotsLayer(result) {
        const layer = new GraphicsLayer({ listMode: "hide" });
        for (const spot of result.spots) {
            layer.add(new Graphic({
                geometry: new Point({
                    longitude: spot.lng,
                    latitude:  spot.lat,
                    spatialReference: { wkid: 4326 },
                }),
                symbol: spotSymbol(spot.score),
            }));
        }
        return layer;
    }

    // Region polygons + centerlines + composite hulls. Returns null if
    // the result predates the labelled-region algorithm (old runs only
    // carried `spots[]`); the caller skips adding a layer in that case
    // so old saved runs render exactly as they did before.
    function buildRegionsLayer(result) {
        const regions = Array.isArray(result.regions) ? result.regions : [];
        const composites = Array.isArray(result.composites) ? result.composites : [];
        if (!regions.length && !composites.length) return null;
        const layer = new GraphicsLayer({
            listMode: "hide",
            opacity:  globalOpacity,
        });

        // Composites first (bottom of the overlay stack) — their hulls
        // are big and would otherwise hide the individual regions inside.
        for (const c of composites) {
            const ring = (c.polygon || []).map(p => [p.lng, p.lat]);
            if (ring.length < 3) continue;
            ring.push(ring[0]);
            layer.add(new Graphic({
                geometry: new Polygon({
                    rings: [ring],
                    spatialReference: { wkid: 4326 },
                }),
                symbol: COMPOSITE_FILL_SYMBOL,
            }));
        }

        // Region polygons.
        for (const r of regions) {
            const poly = r.polygon || [];
            if (poly.length < 3) continue;
            const ring = poly.map(p => [p.lng, p.lat]);
            ring.push(ring[0]);
            layer.add(new Graphic({
                geometry: new Polygon({
                    rings: [ring],
                    spatialReference: { wkid: 4326 },
                }),
                symbol: classFill(r.class, r.score),
            }));
        }

        // Centerlines (ledges / ridges / channels). Drawn after polygons
        // so the line crisply sits on top of the fill.
        for (const r of regions) {
            const line = r.centerline;
            if (!Array.isArray(line) || line.length < 2) continue;
            layer.add(new Graphic({
                geometry: new Polyline({
                    paths: [line.map(p => [p.lng, p.lat])],
                    spatialReference: { wkid: 4326 },
                }),
                symbol: classCenterline(r.class),
            }));
        }
        return layer;
    }


    // ─── Activate / deactivate runs ───────────────────────
    //
    // Single source of truth for "which runs are on the map." Build
    // lazily on activation, tear down on deactivation. activeIds is
    // the persisted truth; runLayers is the live mirror.
    function activateRun(runId) {
        if (runLayers.has(runId)) return;
        const result = runs.get(runId);
        if (!result) return;
        const heatmapLayer = buildHeatmapLayer(result);
        const regionsLayer = buildRegionsLayer(result);   // may be null for old runs
        const spotsLayer   = buildSpotsLayer(result);
        // Heatmaps stack just above the bathymetry tile layer (which lives
        // at index 0). Regions overlay sits above the heatmap. Spots go
        // directly below markerLayer so they paint on top of every
        // heatmap + regions overlay, regardless of activation order — the
        // alternative (everything inserted together at markerIdx) lets a
        // later run's heatmap obscure an earlier run's spots.
        map.layers.add(heatmapLayer, 1);
        if (regionsLayer) map.layers.add(regionsLayer, 2);
        const markerIdx = map.layers.indexOf(markerLayer);
        const spotsIdx  = markerIdx >= 0 ? markerIdx : map.layers.length;
        map.layers.add(spotsLayer, spotsIdx);
        const layerSet = { heatmapLayer, regionsLayer, spotsLayer };
        runLayers.set(runId, layerSet);
        // Honor an already-active "hide overlay" toggle: activating a run
        // while overlays are hidden should keep them hidden (otherwise the
        // new run's heatmap pops in while every other run stays invisible,
        // which reads as a bug).
        applyOverlayVisibilityTo(layerSet);
    }
    function deactivateRun(runId) {
        const set = runLayers.get(runId);
        if (!set) return;
        if (map.layers.includes(set.heatmapLayer)) map.remove(set.heatmapLayer);
        if (set.regionsLayer && map.layers.includes(set.regionsLayer)) {
            map.remove(set.regionsLayer);
        }
        if (map.layers.includes(set.spotsLayer))   map.remove(set.spotsLayer);
        runLayers.delete(runId);
    }

    function applyActiveIds(nextIds) {
        // Diff against current active set so we only touch the layers
        // that actually changed.
        const next = new Set(nextIds.filter(id => runs.has(id)));
        for (const id of activeIds) {
            if (!next.has(id)) deactivateRun(id);
        }
        for (const id of next) {
            if (!activeIds.has(id)) activateRun(id);
        }
        activeIds.clear();
        for (const id of next) activeIds.add(id);
        STORE.setActiveRunIds(Array.from(activeIds));
        updateSidebarButton();
        updateOverlayToggleButton();
    }

    function setGlobalOpacity(value01) {
        globalOpacity = Math.max(0, Math.min(1, value01));
        for (const { heatmapLayer, regionsLayer } of runLayers.values()) {
            heatmapLayer.opacity = globalOpacity;
            // Region outlines + centerlines + composite hulls fade with the
            // heatmap so a "hide overlays" intent is one slider drag, not
            // two. Spots stay at full opacity — they're the navigational
            // anchor, not the overlay.
            if (regionsLayer) regionsLayer.opacity = globalOpacity;
        }
        STORE.setGlobalOpacity(globalOpacity);
    }


    // ─── Overlay visibility toggle ─────────────────────────
    //
    // Quick on/off for the rendered Spotfinder output (heatmap + regions
    // + centerlines + spots) without touching the active-runs set or the
    // global opacity. The layers stay built — we just flip `.visible`,
    // so toggling back on is instant. Session-only state (no storage):
    // the spec is explicit that this is for in-session flipping, and
    // persisting it would surprise a returning user who left it OFF
    // and forgot.
    //
    // The button is hidden until at least one run is active; that's
    // the "spotfinder display is enabled" signal the spec calls out.
    // No active runs → nothing to toggle → no button.

    const $overlayToggle = document.getElementById("sf-overlay-toggle-fab");
    let overlayHidden = false;

    function applyOverlayVisibilityTo(layerSet) {
        if (!layerSet) return;
        const visible = !overlayHidden;
        layerSet.heatmapLayer.visible = visible;
        if (layerSet.regionsLayer) layerSet.regionsLayer.visible = visible;
        layerSet.spotsLayer.visible = visible;
    }
    function applyOverlayVisibility() {
        for (const set of runLayers.values()) applyOverlayVisibilityTo(set);
    }

    function updateOverlayToggleButton() {
        if (!$overlayToggle) return;
        const anyActive = activeIds.size > 0;
        if (!anyActive) {
            // Reset to ON so the next activation isn't silently hidden —
            // a user who toggled OFF, removed all runs, then activates a
            // fresh one expects to see it (otherwise they'd think the
            // run produced nothing).
            overlayHidden = false;
            $overlayToggle.classList.remove("overlay-off");
            $overlayToggle.hidden = true;
            return;
        }
        $overlayToggle.hidden = false;
        $overlayToggle.classList.toggle("overlay-off", overlayHidden);
        $overlayToggle.setAttribute("aria-pressed", overlayHidden ? "false" : "true");
        $overlayToggle.setAttribute(
            "aria-label",
            overlayHidden ? "Show Spotfinder overlay" : "Hide Spotfinder overlay",
        );
        $overlayToggle.title =
            overlayHidden ? "Show Spotfinder overlay" : "Hide Spotfinder overlay";
    }

    function toggleOverlayVisibility() {
        if (activeIds.size === 0) return;   // defensive — button shouldn't be visible
        overlayHidden = !overlayHidden;
        applyOverlayVisibility();
        updateOverlayToggleButton();
    }

    if ($overlayToggle) {
        $overlayToggle.addEventListener("click", toggleOverlayVisibility);
    }


    // ─── Sidebar button ───────────────────────────────────
    function updateSidebarButton() {
        const saved = runs.size;
        const active = activeIds.size;
        if (saved === 0) {
            $runsButtonSub.textContent = "No runs yet";
        } else {
            $runsButtonSub.textContent =
                `${saved} saved · ${active} active`;
        }
    }
    $runsButton.addEventListener("click", openModal);


    // ─── Modal: open / close / discard-confirm ────────────
    //
    // modalOpen is the source of truth for "is the takeover visible."
    // modalSelection is the user's pending pick — committed to activeIds
    // only when Apply runs. Closing via X/ESC/backdrop discards it
    // (with a confirm if it differs from the active set on the map).

    let modalOpen = false;
    /** @type {Set<string>} */ let modalSelection = new Set();
    let lastFocusedBeforeModal = null;

    function openModal() {
        if (modalOpen) return;
        modalOpen = true;
        lastFocusedBeforeModal = document.activeElement;
        modalSelection = new Set(activeIds);
        // Slider reflects the persisted opacity. Reset every open so
        // the user can't get a stale value from a previous session.
        const pct = Math.round(globalOpacity * 100);
        $modalOpacity.value = String(pct);
        $modalOpacityVal.textContent = `${pct}%`;
        renderModalBody();
        updateModalChrome();
        $modal.classList.remove("hidden");
        $modal.setAttribute("aria-hidden", "false");
        document.body.style.overflow = "hidden";
        // Defer focus until after the fade-in transition starts so the
        // first focus ring doesn't snap into place while the modal is
        // still translucent — same trick the depth card uses on open.
        requestAnimationFrame(() => {
            $modalClose.focus();
        });
    }

    function selectionDirty() {
        if (modalSelection.size !== activeIds.size) return true;
        for (const id of modalSelection) if (!activeIds.has(id)) return true;
        return false;
    }

    function attemptCloseModal() {
        // The Apply button is the only path that commits selection. Any
        // other close path silently discards local edits — but only after
        // a confirm if the user has actually changed something, since
        // losing 10 toggles to a stray ESC would be infuriating.
        if (selectionDirty()) {
            const ok = window.confirm(
                "Discard selection changes? Your edits to the runs picker "
              + "haven't been applied to the map yet."
            );
            if (!ok) return;
        }
        closeModal();
    }
    function closeModal() {
        modalOpen = false;
        $modal.classList.add("hidden");
        $modal.setAttribute("aria-hidden", "true");
        document.body.style.overflow = "";
        if (lastFocusedBeforeModal && lastFocusedBeforeModal.focus) {
            lastFocusedBeforeModal.focus();
        }
        lastFocusedBeforeModal = null;
    }

    $modalClose.addEventListener("click", attemptCloseModal);
    $modalBackdrop.addEventListener("click", attemptCloseModal);

    // Modal keyboard: ESC closes, Enter applies. Registered at the
    // document level so it fires regardless of which element inside
    // the modal currently has focus.
    document.addEventListener("keydown", (e) => {
        if (!modalOpen) return;
        if (e.key === "Escape") {
            e.preventDefault();
            attemptCloseModal();
        } else if (e.key === "Enter") {
            const tag = (e.target && e.target.tagName) || "";
            // Don't hijack Enter inside text inputs / textareas — none
            // exist in the modal today, but it costs nothing to be safe
            // and Enter on the opacity slider naturally maps to Apply.
            if (tag === "INPUT" && e.target.type !== "range") return;
            if (tag === "TEXTAREA") return;
            e.preventDefault();
            applyModalSelection();
        }
    });


    // ─── Modal: body rendering ────────────────────────────
    function renderModalBody() {
        if (!runs.size) {
            $modalBody.innerHTML = `
                <div class="sf-runs-empty">
                    <div class="sf-runs-empty-icon" aria-hidden="true">
                        <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" stroke-width="1.8"
                             stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 21l-4.35-4.35"/><circle cx="11" cy="11" r="7"/>
                        </svg>
                    </div>
                    <div class="sf-runs-empty-title">No Spotfinder runs yet</div>
                    <p class="sf-runs-empty-sub">
                        Draw a search area on the map with the Spotfinder
                        button, then run an analysis. Completed runs save
                        here automatically.
                    </p>
                </div>
            `;
            return;
        }

        const grid = document.createElement("div");
        grid.className = "sf-runs-grid";

        // Newest first — getAllRuns already returns in insertion (newest
        // first) order; preserve it.
        const sorted = Array.from(runs.values());

        for (const result of sorted) {
            const card = document.createElement("div");
            card.className = "sf-run-card";
            card.dataset.runId = result.run_id;
            card.tabIndex = 0;
            card.setAttribute("role", "button");
            card.setAttribute("aria-pressed",
                modalSelection.has(result.run_id) ? "true" : "false");

            const spotCount = result.spots.length;
            const spotsTxt = `${spotCount} spot${spotCount === 1 ? "" : "s"}`;

            // Thumbnail rotation: the heatmap PNG is stored in the
            // rectangle's local (un-rotated) frame. Rotate the <img>
            // by the same angle so the thumbnail visually matches the
            // overlay the user will see on the map. We scale-down
            // slightly so the rotated corners stay inside the
            // aspect-ratio'd container at high rotation angles.
            const rot = (result.search_area && result.search_area.rotation_deg) || 0;
            const thumbScale = _thumbnailFitScale(rot);
            const thumbStyle = rot
                ? `transform: rotate(${rot}deg) scale(${thumbScale.toFixed(3)});`
                : "";

            card.innerHTML = `
                <div class="sf-run-card-thumb">
                    <img src="${escapeHtml(result.heatmap_png_url)}"
                         alt="Heatmap preview"
                         loading="lazy"
                         draggable="false"
                         style="${thumbStyle}">
                    <button class="sf-run-card-delete" type="button"
                            title="Delete this run" aria-label="Delete this run"
                            data-action="delete">×</button>
                    <div class="sf-run-card-check" aria-hidden="true">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                             stroke="currentColor" stroke-width="3"
                             stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="20 6 9 17 4 12"/>
                        </svg>
                    </div>
                </div>
                <div class="sf-run-card-body">
                    <div class="sf-run-card-title">
                        ${escapeHtml(fmtRunTimestamp(result.timestamp))}
                    </div>
                    <div class="sf-run-card-meta-row">
                        <span class="sf-run-card-meta">${escapeHtml(fmtBboxCenter(result.bbox))}</span>
                        <span class="sf-run-card-spots">${spotsTxt}</span>
                    </div>
                </div>
            `;
            if (modalSelection.has(result.run_id)) card.classList.add("selected");
            grid.appendChild(card);
        }

        $modalBody.innerHTML = "";
        $modalBody.appendChild(grid);
    }

    function updateModalChrome() {
        const saved = runs.size;
        const sel = modalSelection.size;
        $modalCounter.textContent = `${saved} saved · ${sel} selected`;

        if (sel === 0) {
            $modalApply.textContent = "Hide all runs";
            // Apply with zero selected when zero are active is a no-op —
            // disable to make that clear instead of letting the user mash
            // it expecting something to happen.
            $modalApply.disabled = (activeIds.size === 0);
        } else {
            $modalApply.textContent =
                `Show ${sel} run${sel === 1 ? "" : "s"} on map`;
            $modalApply.disabled = false;
        }
        $modalClear.disabled    = (sel === 0);
        $modalSelectAll.disabled = (saved === 0 || sel === saved);
    }

    function toggleSelection(runId) {
        if (modalSelection.has(runId)) modalSelection.delete(runId);
        else                            modalSelection.add(runId);
        const card = $modalBody.querySelector(
            `.sf-run-card[data-run-id="${CSS.escape(runId)}"]`);
        if (card) {
            const on = modalSelection.has(runId);
            card.classList.toggle("selected", on);
            card.setAttribute("aria-pressed", on ? "true" : "false");
        }
        updateModalChrome();
    }

    // Card click: toggle. Delete button: confirm + remove run.
    $modalBody.addEventListener("click", (e) => {
        const delBtn = e.target.closest("[data-action='delete']");
        if (delBtn) {
            e.stopPropagation();
            const card = delBtn.closest(".sf-run-card");
            if (!card) return;
            const runId = card.dataset.runId;
            const result = runs.get(runId);
            const label = result ? fmtRunTimestamp(result.timestamp) : "this run";
            if (!window.confirm(`Delete ${label}? This can't be undone.`)) return;
            deleteRunPermanently(runId);
            return;
        }
        const card = e.target.closest(".sf-run-card");
        if (!card) return;
        toggleSelection(card.dataset.runId);
    });

    // Keyboard on cards: Space/Enter to toggle, arrow keys to navigate.
    $modalBody.addEventListener("keydown", (e) => {
        const card = e.target.closest(".sf-run-card");
        if (!card) return;
        if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            toggleSelection(card.dataset.runId);
            return;
        }
        if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
            e.preventDefault();
            const cards = Array.from($modalBody.querySelectorAll(".sf-run-card"));
            const idx = cards.indexOf(card);
            if (idx < 0) return;
            // Figure out grid columns from layout so up/down jump rows
            // correctly regardless of viewport width.
            const cardsPerRow = (() => {
                if (cards.length < 2) return 1;
                const top0 = cards[0].offsetTop;
                let n = 1;
                while (n < cards.length && cards[n].offsetTop === top0) n++;
                return n;
            })();
            let target = idx;
            if (e.key === "ArrowRight") target = Math.min(cards.length - 1, idx + 1);
            if (e.key === "ArrowLeft")  target = Math.max(0, idx - 1);
            if (e.key === "ArrowDown")  target = Math.min(cards.length - 1, idx + cardsPerRow);
            if (e.key === "ArrowUp")    target = Math.max(0, idx - cardsPerRow);
            if (cards[target]) cards[target].focus();
        }
    });

    $modalSelectAll.addEventListener("click", () => {
        for (const id of runs.keys()) modalSelection.add(id);
        for (const card of $modalBody.querySelectorAll(".sf-run-card")) {
            card.classList.add("selected");
            card.setAttribute("aria-pressed", "true");
        }
        updateModalChrome();
    });
    $modalClear.addEventListener("click", () => {
        modalSelection.clear();
        for (const card of $modalBody.querySelectorAll(".sf-run-card")) {
            card.classList.remove("selected");
            card.setAttribute("aria-pressed", "false");
        }
        updateModalChrome();
    });

    // Global opacity: live mutation on the active heatmaps + persist.
    // Keeps the slider one of those rare "no Apply needed" controls —
    // it's a compositor adjustment, the same logic as the right panel's
    // layer opacity.
    $modalOpacity.addEventListener("input", (e) => {
        const pct = parseInt(e.target.value, 10);
        $modalOpacityVal.textContent = `${pct}%`;
        setGlobalOpacity(pct / 100);
    });

    $modalApply.addEventListener("click", applyModalSelection);
    function applyModalSelection() {
        if ($modalApply.disabled) return;
        applyActiveIds(Array.from(modalSelection));
        closeModal();
    }


    // ─── Hard delete (from the modal) ─────────────────────
    function deleteRunPermanently(runId) {
        // Pull from active set + storage + in-memory registry, then
        // re-render the grid so the card disappears.
        if (activeIds.has(runId)) {
            deactivateRun(runId);
            activeIds.delete(runId);
            STORE.setActiveRunIds(Array.from(activeIds));
        }
        modalSelection.delete(runId);
        runs.delete(runId);
        STORE.deleteRun(runId);
        renderModalBody();
        updateModalChrome();
        updateSidebarButton();
    }


    function fitToRun(result) {
        const b = result.bbox;
        const extent = new Extent({
            xmin: b.west, ymin: b.south, xmax: b.east, ymax: b.north,
            spatialReference: { wkid: 4326 },
        });
        view.goTo(extent.expand(1.2)).catch(() => {});
    }


    // ─── Cold-load: read storage + handle ?run=<id> ────────
    //
    // Persisted active set drives initial visibility — what the user
    // had on the map last session comes back on refresh. The
    // `?run=<id>` query param (from the analysis page's "View on map"
    // CTA) is added to that set if not already, and the view zooms
    // to its bbox.

    function bootRunsPanel() {
        const all = STORE.getAllRuns();
        for (const result of all) runs.set(result.run_id, result);

        const qs = new URLSearchParams(window.location.search);
        const focusId = qs.get("run");

        // Filter persisted active IDs against actually-existing runs in
        // case storage drifted out of sync (e.g. a run got pruned from
        // outside).
        const persisted = STORE.getActiveRunIds().filter(id => runs.has(id));
        const initialActive = new Set(persisted);
        if (focusId && runs.has(focusId)) initialActive.add(focusId);

        applyActiveIds(Array.from(initialActive));
        updateSidebarButton();

        if (focusId && runs.has(focusId)) {
            view.when().then(() => fitToRun(runs.get(focusId))).catch(() => {});
        }
    }
    bootRunsPanel();


    // ─── 3D Inspector wiring ──────────────────────────────────────
    //
    // inspector3d.js owns the modal, drag overlay, Three.js scene, and
    // controls. It does NOT depend on the ArcGIS API directly — the
    // page (this file) is the only piece that knows view.toMap, so we
    // install a small host shim that converts a screen-pixel rectangle
    // into a lat/lng bbox and routes the post-draw callback back into
    // the inspector with the live (source, analysis, param) config.
    //
    // The FAB itself just enters draw mode; on release the host's
    // onRectangleDrawn fires and we invoke inspector3d.open() with the
    // bbox plus whatever the user currently has committed in the
    // bathymetry layer controls.

    const INSP = window.FishFinderInspector3D;
    const $inspectFab = document.getElementById("inspect3d-fab");
    if (INSP && $inspectFab) {
        INSP.registerHost({
            // Pixel rect → lat/lng bbox via view.toMap. The rect comes
            // in as { x0, y0, x1, y1 } in viewport coordinates; we need
            // map-container-relative coords for view.toMap, so subtract
            // the map container's bounding rect.
            screenRectToBbox: (rect) => {
                const mapEl = document.getElementById("map");
                if (!mapEl) return null;
                const r = mapEl.getBoundingClientRect();
                const local = {
                    x0: rect.x0 - r.left, y0: rect.y0 - r.top,
                    x1: rect.x1 - r.left, y1: rect.y1 - r.top,
                };
                const p0 = view.toMap({ x: local.x0, y: local.y0 });
                const p1 = view.toMap({ x: local.x1, y: local.y1 });
                if (!p0 || !p1
                    || !Number.isFinite(p0.latitude)
                    || !Number.isFinite(p1.latitude)) return null;
                return {
                    north: Math.max(p0.latitude,  p1.latitude),
                    south: Math.min(p0.latitude,  p1.latitude),
                    east:  Math.max(p0.longitude, p1.longitude),
                    west:  Math.min(p0.longitude, p1.longitude),
                };
            },
            onRectangleDrawn: (bbox) => {
                // Read from `committed` — what the user is actually
                // looking at right now. If draft has unsaved changes we
                // honour the on-screen reality, not the pending edit,
                // since that's what the user just visually framed.
                INSP.open({
                    bbox,
                    sourceId:    committed.source,
                    analysisKey: committed.analysis,
                    param:       committed.param,
                    paramExtra:  paramExtraFor(committed),
                });
            },
        });

        $inspectFab.addEventListener("click", () => {
            // Exit competing modes: measure mode steals clicks; Spotfinder
            // owns drag. Both would fight the inspector's pointer capture.
            if (measureMode) setMeasureMode(false);
            if (spotfinderActive) closeSpotfinderPanel();
            INSP.startDrawMode();
        });
    }
});
