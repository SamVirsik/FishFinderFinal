// FishFinder analysis worker.
//
// The worker owns the data side of the live viewer:
//
//   1. Holds the raster cache (URL → Float32 grid) and dedupes inflight fetches.
//   2. Loads analyses.js via importScripts so the same algorithm code runs here
//      as would in the main thread.
//   3. On each `render` message: fetches the raster (or hits the cache), runs
//      the chosen analysis on a reused scratch buffer, crops the buffer-px
//      margin off via createImageBitmap, and posts the bitmap back as a
//      transferable so there is no copy across the worker boundary.
//
// The whole render math runs off the main thread, which is what keeps map
// pan/zoom at 60 fps even when many tiles are repainting at once.

importScripts('/static/analyses.js');


// ─── Raster cache (Float32 elevation grids, keyed by URL) ─────────────
//
// LRU-bounded so a long session can't OOM the worker; ~200 tiles ×
// ~330 KB worst case ≈ 66 MB ceiling. In practice you'll churn through
// far fewer than that since the cache is keyed by URL — once you've
// scrolled across a region at a fixed (source, res) the only cost on
// revisit is the canvas paint.
const RASTER_CACHE_LIMIT = 256;
const rasterCache = new Map();
const inflight = new Map();

function rasterCacheGet(url) {
    if (!rasterCache.has(url)) return undefined;
    const v = rasterCache.get(url);
    rasterCache.delete(url);
    rasterCache.set(url, v);
    return v;
}

function rasterCacheSet(url, val) {
    rasterCache.set(url, val);
    while (rasterCache.size > RASTER_CACHE_LIMIT) {
        rasterCache.delete(rasterCache.keys().next().value);
    }
}

// Returns the raster on success; throws on any transient failure (network
// error, non-OK HTTP status, decode error) so the caller can report it as
// 'error' rather than 'empty'. The server now returns 5xx (not 204) on
// transient failures so this branch is the one that actually fires for
// NOAA blips — keeping the canvas LRU from caching a permanent blank.
// (HTTP 204 is still handled defensively below; legitimate "no coverage
// here" is now signalled by an all-NaN raster, detected post-decode.)
function fetchRaster(url) {
    const cached = rasterCacheGet(url);
    if (cached !== undefined) return Promise.resolve(cached);
    if (inflight.has(url)) return inflight.get(url);

    const promise = (async () => {
        try {
            const resp = await fetch(url);
            if (resp.status === 204) {
                rasterCacheSet(url, null);
                return null;
            }
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const buf = await resp.arrayBuffer();
            const dv = new DataView(buf);
            const w        = dv.getUint32(0, true);
            const h        = dv.getUint32(4, true);
            const cellsize = dv.getFloat32(8, true);
            const bufferPx = dv.getUint32(12, true);
            const data     = new Float32Array(buf, 16, w * h);
            const raster   = { w, h, cellsize, bufferPx, data };
            rasterCacheSet(url, raster);
            return raster;
        } finally {
            inflight.delete(url);
        }
    })();
    inflight.set(url, promise);
    return promise;
}


// ─── Reusable scratch buffers ─────────────────────────────────────────
//
// The worker is single-threaded and processes render messages serially,
// so one scratch set is enough — each render starts by overwriting it.
// Avoids ~2 MB of allocation per tile per analysis.
let scratchN     = 0;
let scratchElev  = null;
let scratchMask  = null;
let scratchRgba  = null;

function ensureScratch(n) {
    if (n !== scratchN) {
        scratchN    = n;
        scratchElev = new Float32Array(n);
        scratchMask = new Uint8Array(n);
        scratchRgba = new Uint8ClampedArray(4 * n);
    }
}


// ─── Render dispatch ──────────────────────────────────────────────────

async function handleRender(id, url, analysisKey, param) {
    let raster;
    try {
        raster = await fetchRaster(url);
    } catch (err) {
        // Transient failure — main thread MUST NOT cache this as a blank
        // tile, or every retry forever sees a blank. Reporting 'error'
        // lets the canvas LRU skip the cache entry.
        console.warn('[worker] raster fetch failed:', url, err.message || err);
        self.postMessage({ type: 'error', id, message: String(err && err.message || err) });
        return;
    }
    if (raster === null) {
        // HTTP 204 — NOAA confirmed there is no coverage here. Safe to
        // cache as a permanent blank tile.
        self.postMessage({ type: 'empty', id });
        return;
    }

    const { w, h, cellsize, bufferPx, data } = raster;
    const n = w * h;
    ensureScratch(n);

    // Coerce NaN nodata → 0 (so gradients/Gaussians don't propagate),
    // record the mask so we can punch the alpha channel afterwards.
    let nodataCount = 0;
    for (let i = 0; i < n; i++) {
        const v = data[i];
        if (Number.isNaN(v)) {
            scratchElev[i] = 0;
            scratchMask[i] = 1;
            nodataCount++;
        } else {
            scratchElev[i] = v;
            scratchMask[i] = 0;
        }
    }

    // Entirely-nodata tile: NOAA returned a raster but every pixel was the
    // nodata sentinel. That used to render as a fully-transparent canvas,
    // letting the basemap show through the whole tile slot — read by users
    // as "the depth tile randomly didn't load." Treat it the same as the
    // legitimate-no-coverage signal so the main thread paints the opaque
    // blank instead. Crop to the visible region (excluding the buffer
    // margin) so a few stray nodata cells in the buffer don't matter.
    const inner = w - 2 * bufferPx;
    if (inner > 0 && inner < w) {
        let innerNodata = 0;
        for (let yy = bufferPx; yy < bufferPx + inner; yy++) {
            const rowOff = yy * w + bufferPx;
            for (let xx = 0; xx < inner; xx++) {
                if (scratchMask[rowOff + xx]) innerNodata++;
            }
        }
        if (innerNodata === inner * inner) {
            self.postMessage({ type: 'empty', id });
            return;
        }
    } else if (nodataCount === n) {
        self.postMessage({ type: 'empty', id });
        return;
    }

    const fn = self.FFAnalyses[analysisKey] || self.FFAnalyses['color-relief'];
    fn(scratchElev, scratchMask, w, h, cellsize, param, scratchRgba);

    for (let i = 0; i < n; i++) {
        if (scratchMask[i]) scratchRgba[i * 4 + 3] = 0;
    }

    // Slice into an independent Uint8ClampedArray so the next render
    // can't trample what createImageBitmap is rasterising. Then crop
    // the buffer margin in one step via the source-rect overload —
    // the resulting bitmap is exactly res×res (the on-screen tile size).
    const res = w - 2 * bufferPx;
    const rgbaCopy = scratchRgba.slice(0, 4 * n);
    const imageData = new ImageData(rgbaCopy, w, h);
    const bitmap = await createImageBitmap(
        imageData, bufferPx, bufferPx, res, res
    );

    self.postMessage(
        { type: 'rendered', id, bitmap, size: res },
        [bitmap]
    );
}


// ─── Point sampling ───────────────────────────────────────────────────
//
// Reads a single elevation value out of the tile's float32 grid, at a
// position given as (fracX, fracY) ∈ [0, 1) within the *visible* tile
// (i.e. excluding the buffer margin). Reuses the same fetch + cache as
// the render path, so a click on a visible tile is a pure cache lookup
// and a click on a not-yet-loaded tile reuses any inflight render fetch.
//
// This is the source of truth for the depth-card readout — it hits the
// exact pixel grid the user is looking at, which removes the mosaic-
// rule ambiguity that NOAA's identify/getSamples endpoints introduce
// (those resolve to different sub-rasters of the source mosaic depending
// on the pixelSize hint, which is why the old depth endpoint disagreed
// with itself across zoom levels by hundreds of feet).
async function handleSample(id, url, fracX, fracY) {
    let raster;
    try {
        raster = await fetchRaster(url);
    } catch {
        self.postMessage({ type: 'sampled', id, value: null });
        return;
    }
    if (raster === null) {
        self.postMessage({ type: 'sampled', id, value: null });
        return;
    }
    const { w, h, bufferPx, data } = raster;
    const inner = w - 2 * bufferPx;
    const fx = Math.min(Math.max(fracX, 0), 0.999999);
    const fy = Math.min(Math.max(fracY, 0), 0.999999);
    const ix = bufferPx + Math.floor(fx * inner);
    const iy = bufferPx + Math.floor(fy * inner);
    const v = data[iy * w + ix];
    self.postMessage({
        type: 'sampled', id,
        value: Number.isFinite(v) ? v : null,
    });
}


self.addEventListener('message', (ev) => {
    const msg = ev.data;
    if (!msg) return;
    if (msg.type === 'render') {
        handleRender(msg.id, msg.url, msg.analysisKey, msg.param)
            .catch((err) => {
                self.postMessage({
                    type: 'error', id: msg.id, message: String(err)
                });
            });
    } else if (msg.type === 'sample') {
        handleSample(msg.id, msg.url, msg.fracX, msg.fracY)
            .catch(() => {
                self.postMessage({ type: 'sampled', id: msg.id, value: null });
            });
    }
});
