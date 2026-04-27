// FishFinder live tile viewer.
//
// Architecture (post-rework — read this before touching anything):
//
// ZOOM PREFETCH (warm adjacent levels while idle):
//   When the view is stationary, we speculatively render tiles at
//   `zoom-1` and `zoom+1` covering the current extent. Those land in the
//   canvas LRU and the disk cache, so the moment the user zooms ArcGIS's
//   `fetchTile` is a cache hit — no blurry "stretched parent tile" while
//   the new level loads. Prefetches are SEQUENTIAL (the render worker is
//   single-threaded — a parallel flood would queue ahead of real user
//   tiles and make zooms worse), and ABORT on the next movement. Capped
//   by a budget so a wide view at high zoom can't burn unbounded NOAA
//   bandwidth.
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
//   - The worker reports `'empty'` only on HTTP 204 (NOAA legitimately has
//     no coverage); we cache the blank canvas for that key. Transient
//     fetch errors come back as `'error'` and are NEVER cached, so the
//     next pan/zoom retries — fixes the "sometimes does not render and
//     stays blank forever" symptom.

const baseurl = window.location.origin;


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
    "hillshade": {
        label: "Vertical exaggeration", unit: "×",
        min: 1, max: 30, step: 1, default: 5,
        hint: "Higher = more contrast between flat and steep ground.",
        intro: "Pure greyscale shaded relief. Reveals structure without colour.",
        pretty: "Hillshade",
    },
    "slope": {
        label: "Max slope on scale", unit: "°",
        min: 5, max: 90, step: 1, default: 30,
        hint: "Smaller value exaggerates subtle slopes; larger smooths them.",
        intro: "True slope angle. Cool = flat, hot = steep, purple = vertical.",
        pretty: "Slope",
    },
    "aspect": {
        label: "Min slope to colour", unit: "°",
        min: 1, max: 20, step: 1, default: 2,
        hint: "Flatter pixels stay transparent so noise doesn't dominate.",
        intro: "Direction the sea floor faces. Hue = compass bearing of down-slope.",
        pretty: "Aspect",
    },
    "roughness": {
        label: "Feature scale", unit: " m",
        min: 5, max: 500, step: 5, default: 50,
        hint: "Size of the features you want to highlight. Smaller = finer texture.",
        intro: "High-pass detail. Bright = rough (wrecks, ledges, rubble).",
        pretty: "Roughness",
    },
    "fishing-spots": {
        label: "Band size", unit: " ft",
        min: 1, max: 50, step: 1, default: 10,
        hint: "Underlying depth banding; magenta highlights are the spots.",
        intro: "Magenta where slope is unusually high for that depth band.",
        pretty: "Fishing Spots",
    },
};


// ─── Render worker (singleton) ──────────────────────────────────
const renderWorker = new Worker(`${baseurl}/static/analyses-worker.js`);
const pending = new Map();
let nextRequestId = 0;

renderWorker.addEventListener('message', (ev) => {
    const { type, id } = ev.data;
    const slot = pending.get(id);
    if (!slot) return;
    pending.delete(id);
    if (type === 'rendered') {
        slot.resolve({ status: 'ok', bitmap: ev.data.bitmap, size: ev.data.size });
    } else if (type === 'empty') {
        slot.resolve({ status: 'empty' });
    } else {
        slot.resolve({ status: 'error' });
    }
});

function requestRender(url, analysisKey, param) {
    const id = ++nextRequestId;
    return new Promise((resolve) => {
        pending.set(id, { resolve });
        renderWorker.postMessage({
            type: 'render', id, url, analysisKey, param,
        });
    });
}


// ─── Render-canvas LRU cache ────────────────────────────────────
// Keyed by (rasterURL, analysisKey, param). A hit short-circuits both the
// worker and the network. Bounded so a long session can't grow without
// bound.
const CANVAS_CACHE_LIMIT = 384;
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

// One blank reusable canvas — used for both legitimately-empty tiles
// (HTTP 204) and transient errors. The crucial difference: empty tiles
// are CACHED into the LRU under their key (NOAA confirmed nothing is
// there, no point asking again), while error tiles are NOT cached, so
// the next pan/zoom retries them.
const blankTileCanvas = (() => {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    return c;
})();

async function getRenderedCanvas(url, analysisKey, param) {
    const key = `${url}|${analysisKey}|${param}`;
    const hit = canvasCacheGet(key);
    if (hit) return hit;

    const result = await requestRender(url, analysisKey, param);
    if (result.status === 'empty') {
        canvasCacheSet(key, blankTileCanvas);
        return blankTileCanvas;
    }
    if (result.status === 'error') {
        // Don't cache. Returning the blank for THIS request keeps ArcGIS
        // happy; the next visit will re-issue the render and (hopefully)
        // succeed.
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
}


require([
    "esri/Map",
    "esri/views/MapView",
    "esri/layers/BaseTileLayer",
    "esri/layers/GraphicsLayer",
    "esri/layers/support/TileInfo",
    "esri/geometry/SpatialReference",
    "esri/Graphic",
    "esri/geometry/Point",
], (EsriMap, MapView, BaseTileLayer, GraphicsLayer, TileInfo, SpatialReference,
    Graphic, Point) => {

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
    const $pendingAnalysis = document.getElementById(`pending-analysis-${id}`);
    const $pendingSource   = document.getElementById(`pending-source-${id}`);
    const $pendingRes      = document.getElementById(`pending-resolution-${id}`);
    const $panel           = document.getElementById("control-panel");
    const $panelToggle     = document.getElementById("panel-toggle");
    const $panelClose      = document.getElementById("panel-close");
    const $loading         = document.getElementById("loading-indicator");
    const $loadingText     = $loading ? $loading.querySelector(".loading-text") : null;


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
    }

    function updateParamLabel() {
        const cfg = ANALYSES[$analysis.value] || ANALYSES["color-relief"];
        $paramValue.textContent = `${$param.value}${cfg.unit}`;
    }


    // ─── Custom tile layer ─────────────────────────────────
    // Each instance is bound to one (source, resolution, analysis, param)
    // tuple. Config changes always build a fresh layer — never mutate.
    const RasterAnalysisLayer = BaseTileLayer.createSubclass({
        properties: {
            source: null,
            resolution: null,
            analysisKey: null,
            param: null,
        },
        fetchTile: function (level, row, col) {
            const url = `${baseurl}/raster/${this.source}/${this.resolution}`
                      + `/${level}/${col}/${row}.bin`;
            return getRenderedCanvas(url, this.analysisKey, this.param);
        },
    });


    // ─── Map setup ─────────────────────────────────────────

    syncParamControl();

    const markerLayer = new GraphicsLayer({ listMode: "hide" });

    const map = new EsriMap({
        basemap: "dark-gray-vector",
        layers: [markerLayer],
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
        return {
            analysis:   $analysis.value,
            source:     $source.value,
            resolution: parseInt($resolution.value, 10),
            param:      Math.round(parseFloat($param.value)),
        };
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

        const markerIdx = map.layers.indexOf(markerLayer);
        if (markerIdx >= 0) map.layers.add(newLayer, markerIdx);
        else                map.layers.add(newLayer);

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
        }).catch(() => { /* layer was removed before view resolved */ });
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
        committed = { ...committed, param: draft.param };
        applyConfig(committed, "overlap");
    }

    function resetDraft() {
        $analysis.value   = committed.analysis;
        $source.value     = committed.source;
        $resolution.value = committed.resolution;
        $param.value      = committed.param;
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


    // ─── Adjacent-zoom prefetch ────────────────────────────
    //
    // While the view is at rest, walk the tile grid at zoom-1 and zoom+1
    // (sorted by distance to view centre, capped at a budget) and call
    // getRenderedCanvas for each. Anything not already in the canvas LRU
    // is fetched + rendered in the background, populating the LRU + the
    // server-side disk cache. The next zoom is then an instant cache
    // hit instead of a 200-800 ms NOAA fetch.
    //
    // Sequential because the worker is single-threaded — a parallel
    // flood would queue *ahead* of any user-issued fetchTile and make
    // zoom transitions visibly slower, the opposite of what we want.

    const WEB_MERCATOR_HALF = 20037508.342789244;
    const PREFETCH_BUDGET = 16;
    const PREFETCH_IDLE_MS = 500;

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
    function tileCenterMeters(t) {
        const numTiles = Math.pow(2, t.z);
        const tileSize = (2 * WEB_MERCATOR_HALF) / numTiles;
        return [
            -WEB_MERCATOR_HALF + (t.x + 0.5) * tileSize,
             WEB_MERCATOR_HALF - (t.y + 0.5) * tileSize,
        ];
    }

    let prefetchToken     = 0;
    let prefetchInFlight  = false;
    let prefetchTimer     = null;

    async function runPrefetch() {
        if (prefetchInFlight)            return;
        if (!view.extent || !committed)  return;
        if (isDirtyMajor())              return;

        const myToken = ++prefetchToken;
        const cfg = { ...committed };
        const zoom = Math.round(view.zoom);

        const targets = [];
        if (zoom + 1 <= 23) targets.push(...tilesInView(view.extent, zoom + 1));
        if (zoom - 1 >= 0)  targets.push(...tilesInView(view.extent, zoom - 1));

        const cx = (view.extent.xmin + view.extent.xmax) / 2;
        const cy = (view.extent.ymin + view.extent.ymax) / 2;
        targets.sort((a, b) => {
            const [ax, ay] = tileCenterMeters(a);
            const [bx, by] = tileCenterMeters(b);
            return Math.hypot(ax - cx, ay - cy) - Math.hypot(bx - cx, by - cy);
        });

        const slice = targets.slice(0, PREFETCH_BUDGET);

        prefetchInFlight = true;
        try {
            for (const t of slice) {
                if (myToken !== prefetchToken) return;  // user moved, drop the rest
                const url = `${baseurl}/raster/${cfg.source}/${cfg.resolution}`
                          + `/${t.z}/${t.x}/${t.y}.bin`;
                const key = `${url}|${cfg.analysis}|${cfg.param}`;
                if (canvasCache.has(key)) continue;     // already warm
                try { await getRenderedCanvas(url, cfg.analysis, cfg.param); }
                catch { /* keep the chain alive */ }
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
    async function lookupDepth(lat, lon, point) {
        const requestId = ++activeRequest;
        lastCoord = { lat, lon };
        dropMarker(point);
        showCard();
        setDepthValue("Loading…", true);
        $depthCoord.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
        try {
            const url = `${baseurl}/depth/${lat}/${lon}`
                      + `?source=${encodeURIComponent(committed.source)}`;
            const resp = await fetch(url);
            if (requestId !== activeRequest) return;
            const data = await resp.json();
            const m = data.depth_meters;
            if (m == null) setDepthValue("No data", true);
            else setDepthValue(`${(m * 3.28084).toFixed(1)} ft`);
        } catch (err) {
            if (requestId !== activeRequest) return;
            console.error(err);
            setDepthValue("Error", true);
        }
    }
    view.on("click", (event) => {
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


    // ─── Jump-to-coords dialog ─────────────────────────────
    const $gotoBtn    = document.getElementById("goto-spot-btn");
    const $gotoDialog = document.getElementById("goto-dialog");
    const $gotoLat    = document.getElementById("goto-lat");
    const $gotoLon    = document.getElementById("goto-lon");
    const $gotoSubmit = document.getElementById("goto-submit");
    const $gotoCancel = document.getElementById("goto-cancel");

    $gotoBtn.addEventListener("click", () => $gotoDialog.showModal());
    $gotoCancel.addEventListener("click", (e) => {
        e.preventDefault();
        $gotoDialog.close();
    });
    $gotoSubmit.addEventListener("click", async (e) => {
        e.preventDefault();
        const lat = parseFloat($gotoLat.value);
        const lon = parseFloat($gotoLon.value);
        if (Number.isNaN(lat) || Number.isNaN(lon)) return;
        $gotoDialog.close();
        const point = new Point({ longitude: lon, latitude: lat });
        await view.goTo({ center: [lon, lat], zoom: Math.max(view.zoom, 11) });
        lookupDepth(lat, lon, point);
    });
});
