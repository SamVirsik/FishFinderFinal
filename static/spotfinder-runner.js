// ============================================================
// SPOTFINDER ALGORITHM — STUB IMPLEMENTATION
// ============================================================
//
// This file is the single swap-out point for the Spotfinder algorithm.
// Everything outside this file (the analysis page, the map overlay
// layers, the persistence layer, the runs panel) consumes the contract
// declared at the top of this file and must continue to work unchanged
// when the real algorithm replaces the stub.
//
// CONTRACT (do not change without updating callers):
//   window.FishFinderSpotfinder.run(input, onProgress?) → Promise<SpotfinderResult>
//
// If you find yourself wanting to leak algorithm-specific concepts out
// of this file (a new field on the bbox, a new top-level property on
// the result, a UI mode that only makes sense for one analysis kind),
// stop and hide it behind `manifest` or each spot's `features` map
// instead — those are the documented extension points.
// ============================================================


// ─── Type contract (JSDoc — express the spec's TS types) ──────

/**
 * @typedef {Object} BoundingBox
 * @property {number} north   Latitude of the top edge, in decimal degrees.
 * @property {number} south   Latitude of the bottom edge.
 * @property {number} east    Longitude of the right edge.
 * @property {number} west    Longitude of the left edge.
 */

/**
 * Free-form algorithm parameters. Kept opaque on purpose — adding a
 * UI knob shouldn't require changing this contract. Persist whatever
 * the algorithm needs to reproduce the run.
 *
 * @typedef {Object.<string, unknown>} SpotfinderParams
 */

/**
 * @typedef {Object} SpotfinderInput
 * @property {BoundingBox}      bbox
 * @property {SpotfinderParams} params
 */

/**
 * One identified spot. `features` is the per-spot extension map — any
 * algorithm-specific scalar (BPI, slope, rugosity, predator likelihood,
 * etc.) lives here so the UI doesn't grow a special-case column for it.
 *
 * @typedef {Object} Spot
 * @property {string} id
 * @property {number} lat
 * @property {number} lng
 * @property {number} depth_m
 * @property {number} score                       0..1, higher = better.
 * @property {Object.<string, number>} features
 */

/**
 * @typedef {Object} SpotfinderManifest
 * @property {string} data_source                 Human label for the source raster.
 * @property {number} resolution_m                Ground sample distance, metres.
 * @property {number} cell_count                  Number of grid cells analysed.
 * @property {number} runtime_ms                  Wall-clock time spent in the algorithm.
 */

/**
 * @typedef {Object} SpotfinderResult
 * @property {string}            run_id           Caller-opaque unique id.
 * @property {string}            timestamp        ISO 8601, when the run completed.
 * @property {BoundingBox}       bbox
 * @property {SpotfinderParams}  params
 * @property {string}            heatmap_png_url  data: or blob: URL of the colorised score raster (with alpha).
 * @property {BoundingBox}       heatmap_bounds   Geographic corners the heatmap PNG is georeferenced to.
 * @property {Spot[]}            spots
 * @property {SpotfinderManifest} manifest
 */

/**
 * Progress callback — invoked with monotonically non-decreasing `pct`
 * in [0, 100] and a short human label of the current stage. Safe to
 * ignore.
 *
 * @callback ProgressFn
 * @param   {number} pct
 * @param   {string} label
 * @returns {void}
 */

/**
 * Run the Spotfinder algorithm.
 *
 * @param {SpotfinderInput}    input
 * @param {ProgressFn} [onProgress]
 * @returns {Promise<SpotfinderResult>}
 */
async function runSpotfinder(input, onProgress) {
    const t0 = performance.now();
    const report = typeof onProgress === "function"
        ? onProgress
        : () => {};

    // STUB: stages exist purely to surface meaningful progress; the
    // real algorithm will replace this with the actual fetch / compute
    // / detect work and tick `report` from there.
    const stages = [
        { label: "Fetching bathymetry…",  duration: _rand(300, 600),  endPct: 25 },
        { label: "Computing features…",    duration: _rand(700, 1200), endPct: 60 },
        { label: "Detecting spots…",       duration: _rand(600, 1000), endPct: 85 },
        { label: "Generating heatmap…",    duration: _rand(400, 700),  endPct: 100 },
    ];
    let cursor = 0;
    for (const stage of stages) {
        await _tween(cursor, stage.endPct, stage.duration,
                     (p) => report(p, stage.label));
        cursor = stage.endPct;
    }
    // Heatmap + spot generation runs synchronously after the staged
    // "compute" tick — visually the progress bar reaches 100% just
    // before the result is handed back, which matches real behaviour.
    const { heatmapUrl, spots, cellCount, resolutionM } =
        _generateStubField(input.bbox);

    return {
        run_id:    _genId(),
        timestamp: new Date().toISOString(),
        bbox:      { ...input.bbox },
        params:    { ...(input.params || {}) },
        heatmap_png_url: heatmapUrl,
        heatmap_bounds:  { ...input.bbox },
        spots,
        manifest: {
            data_source:  "stub",
            resolution_m: resolutionM,
            cell_count:   cellCount,
            runtime_ms:   Math.round(performance.now() - t0),
        },
    };
}


// ============================================================
// EVERYTHING BELOW THIS LINE IS STUB-ONLY.
//
// When the real algorithm lands it will compute a true score raster
// from bathymetry and produce its own heatmap PNG. The helpers below
// (_generateStubField, _colorRamp, _tween, _rand, _randInt, _genId)
// can be deleted wholesale at that point — they are not part of the
// public contract.
// ============================================================

/**
 * Build a plausible-looking score field from random gaussian blobs,
 * colorise it with a transparent → yellow → orange → red ramp, encode
 * it as a PNG data URL, and pick a handful of "spots" near the bright
 * peaks. Output canvas is ~512 px on the long side, aspect-matched to
 * the bbox so map overlay rendering doesn't distort it.
 */
function _generateStubField(bbox) {
    // Match canvas aspect to bbox aspect so the MediaLayer overlay
    // renders 1:1 with no implicit stretching.
    const dLon = Math.max(1e-6, bbox.east  - bbox.west);
    const dLat = Math.max(1e-6, bbox.north - bbox.south);
    const aspect = dLon / dLat;
    const longSide = 512;
    let w, h;
    if (aspect >= 1) { w = longSide; h = Math.max(64, Math.round(longSide / aspect)); }
    else             { h = longSide; w = Math.max(64, Math.round(longSide * aspect)); }

    // Place blobs in pixel space. Sigma is a fraction of the short
    // side so blob radius scales with canvas size rather than always
    // being ~30 px.
    const shortSide = Math.min(w, h);
    const blobCount = _randInt(6, 12);
    const blobs = [];
    for (let i = 0; i < blobCount; i++) {
        blobs.push({
            x:     Math.random() * w,
            y:     Math.random() * h,
            sigma: _rand(0.06, 0.18) * shortSide,
            amp:   _rand(0.5, 1.0),
        });
    }

    // Sum-of-gaussians field. Cheap and visually convincing at this
    // resolution — Perlin noise would be nicer but isn't worth the
    // dependency for stub output.
    const field = new Float32Array(w * h);
    let fieldMax = 0;
    for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
            let v = 0;
            for (const b of blobs) {
                const dx = x - b.x;
                const dy = y - b.y;
                const d2 = dx * dx + dy * dy;
                v += b.amp * Math.exp(-d2 / (2 * b.sigma * b.sigma));
            }
            field[y * w + x] = v;
            if (v > fieldMax) fieldMax = v;
        }
    }
    if (fieldMax > 0) {
        for (let i = 0; i < field.length; i++) field[i] /= fieldMax;
    }

    // Rasterize.
    const canvas = document.createElement("canvas");
    canvas.width  = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(w, h);
    for (let i = 0; i < field.length; i++) {
        const [r, g, b, a] = _colorRamp(field[i]);
        img.data[i * 4    ] = r;
        img.data[i * 4 + 1] = g;
        img.data[i * 4 + 2] = b;
        img.data[i * 4 + 3] = a;
    }
    ctx.putImageData(img, 0, 0);
    // PNG keeps the alpha channel so the overlay genuinely blends
    // with the basemap instead of showing a black rectangle around
    // the low-intensity edges.
    const heatmapUrl = canvas.toDataURL("image/png");

    // Spots: top-N blob centres by amplitude, slightly jittered so they
    // don't sit on the exact peak pixel. Convert pixel coords to lat/lon
    // using the bbox (y=0 at top → north).
    const sortedBlobs = blobs.slice().sort((a, b) => b.amp - a.amp);
    const spotCount = Math.min(sortedBlobs.length, _randInt(5, 12));
    const spots = [];
    for (let i = 0; i < spotCount; i++) {
        const b = sortedBlobs[i];
        const jitterPx = b.sigma * 0.3;
        const sx = b.x + _rand(-jitterPx, jitterPx);
        const sy = b.y + _rand(-jitterPx, jitterPx);
        const px = Math.max(0, Math.min(w - 1, Math.round(sx)));
        const py = Math.max(0, Math.min(h - 1, Math.round(sy)));
        const fieldValue = field[py * w + px];
        const lon = bbox.west  + (sx / w) * dLon;
        const lat = bbox.north - (sy / h) * dLat;
        spots.push({
            id:      _genId(),
            lat,
            lng:     lon,
            depth_m: _rand(10, 80),
            score:   Math.max(0.1, Math.min(1.0, fieldValue)),
            features: {
                bpi:      _rand(0.2, 0.95),
                slope:    _rand(0.1, 0.8),
                rugosity: _rand(0.1, 0.9),
            },
        });
    }
    spots.sort((a, b) => b.score - a.score);

    return {
        heatmapUrl,
        spots,
        cellCount:   w * h,
        resolutionM: _randInt(5, 15),
    };
}

/**
 * Transparent → yellow → orange → red, with alpha rising alongside
 * intensity so low-score areas fade out smoothly instead of showing
 * a hard edge.
 */
function _colorRamp(t) {
    if (t <= 0.05) return [0, 0, 0, 0];
    const stops = [
        { t: 0.05, r: 255, g: 255, b: 180, a: 0   },
        { t: 0.30, r: 255, g: 230, b: 110, a: 110 },
        { t: 0.60, r: 255, g: 170, b:  50, a: 200 },
        { t: 1.00, r: 255, g:  70, b:  60, a: 235 },
    ];
    for (let i = 1; i < stops.length; i++) {
        if (t <= stops[i].t) {
            const a = stops[i - 1];
            const b = stops[i];
            const f = (t - a.t) / (b.t - a.t);
            return [
                Math.round(a.r + (b.r - a.r) * f),
                Math.round(a.g + (b.g - a.g) * f),
                Math.round(a.b + (b.b - a.b) * f),
                Math.round(a.a + (b.a - a.a) * f),
            ];
        }
    }
    const last = stops[stops.length - 1];
    return [last.r, last.g, last.b, last.a];
}

/** rAF-driven linear interpolation from `fromPct` to `toPct` over
 *  `durationMs`, calling `cb(pct)` each frame. */
function _tween(fromPct, toPct, durationMs, cb) {
    return new Promise((resolve) => {
        const start = performance.now();
        function step(now) {
            const t = Math.min(1, (now - start) / durationMs);
            cb(fromPct + (toPct - fromPct) * t);
            if (t < 1) requestAnimationFrame(step);
            else       resolve();
        }
        requestAnimationFrame(step);
    });
}

function _rand(lo, hi)     { return lo + Math.random() * (hi - lo); }
function _randInt(lo, hi)  { return Math.floor(_rand(lo, hi + 1)); }
function _genId() {
    // crypto.randomUUID is available everywhere we care about (Chrome 92+,
    // Firefox 95+, Safari 15.4+). Fall back to a manual UUID-ish string
    // for older runtimes — only matters in the unlikely case the user
    // runs this in something ancient.
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }
    return "sf-" + Math.random().toString(36).slice(2, 10)
                 + "-" + Date.now().toString(36);
}


// ─── Public namespace ─────────────────────────────────────────
window.FishFinderSpotfinder = { run: runSpotfinder };
