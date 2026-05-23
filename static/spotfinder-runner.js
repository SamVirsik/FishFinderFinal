// ============================================================
// SPOTFINDER ALGORITHM — REAL IMPLEMENTATION
// ============================================================
//
// This file is the single swap-out point for the Spotfinder algorithm.
// Everything outside this file (the analysis page, the map overlay
// layers, the persistence layer, the runs panel) consumes the contract
// declared at the top of this file and must continue to work unchanged
// when the runner changes.
//
// CONTRACT (do not change without updating callers):
//   window.FishFinderSpotfinder.run(input, onProgress?) → Promise<SpotfinderResult>
//
// The runner is a thin streaming client. The actual algorithm runs on
// the Python backend at POST /spotfinder/run — see src/spotfinder.py.
// The backend was chosen over an all-JS implementation because:
//
//   - The heavy math (BPI annulus means, percentile-rank over millions
//     of cells, peak detection) is 10-100× faster in numpy + scipy
//     than in pure JS, and scipy.ndimage.uniform_filter in particular
//     runs the BPI kernels in O(N) regardless of radius.
//   - The existing tile pipeline already disk-caches NOAA bytes and
//     manages an HTTPS session, so the Spotfinder fetch path piggybacks
//     on infrastructure that's already known-good.
//   - Algorithm changes ship as one server file with no asset rebuilds
//     and no risk of an outdated cached worker on a user's browser.
//
// The wire format is newline-delimited JSON (NDJSON) over a single
// long-lived POST response. One JSON event per line; the last event is
// either `result` or `error`. This keeps the runner trivial — no SSE
// wire-format parsing, no separate kickoff/poll endpoints.
//
// If you find yourself wanting to leak algorithm-specific concepts out
// of this file (a new field on the search area, a new top-level
// property on the result, a UI mode that only makes sense for one
// analysis kind), stop and hide it behind `manifest` or each spot's
// `features` map instead — those are the documented extension points.
// ============================================================


// ─── Type contract (JSDoc — mirrors the spec's TS types) ──────

/**
 * @typedef {Object} LatLng
 * @property {number} lat
 * @property {number} lng
 */

/**
 * @typedef {Object} BoundingBox
 * @property {number} north
 * @property {number} south
 * @property {number} east
 * @property {number} west
 */

/**
 * @typedef {Object} SearchArea
 * @property {[LatLng, LatLng, LatLng, LatLng]} corners   TL, TR, BR, BL.
 * @property {LatLng}      center
 * @property {number}      width_m
 * @property {number}      height_m
 * @property {number}      rotation_deg
 * @property {BoundingBox} bbox
 */

/** @typedef {Object.<string, unknown>} SpotfinderParams */

/**
 * @typedef {Object} SpotfinderConfig
 * @property {string}   [environment]      Environment mode key (e.g. "reef").
 * @property {string[]} [structure_types]  Target structure type keys.
 * @property {?string}  [source]           Preferred data source id, or null.
 */

/**
 * @typedef {Object} SpotfinderInput
 * @property {SearchArea}       search_area
 * @property {SpotfinderParams} params
 * @property {SpotfinderConfig} [config]
 */

/**
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
 * @property {string} data_source
 * @property {number} resolution_m
 * @property {number} cell_count
 * @property {number} runtime_ms
 * @property {number} rotation_deg
 */

/**
 * @typedef {Object} SpotfinderResult
 * @property {string}            run_id
 * @property {string}            timestamp
 * @property {SearchArea}        search_area
 * @property {SpotfinderParams}  params
 * @property {string}            heatmap_png_url
 * @property {[LatLng, LatLng, LatLng, LatLng]} heatmap_corners
 * @property {Spot[]}            spots
 * @property {SpotfinderManifest} manifest
 * @property {BoundingBox}       bbox
 * @property {BoundingBox}       heatmap_bounds
 */

/**
 * Progress callback. `remaining_ms` is provided once enough work has
 * elapsed for the estimate to be stable (`pct > ~1`); it's `undefined`
 * before that.
 *
 * @callback ProgressFn
 * @param   {number}            pct          0..100.
 * @param   {string}            label        Short human-readable stage.
 * @param   {number|undefined}  remaining_ms Estimated millis until done.
 */

/**
 * Run the Spotfinder algorithm.
 *
 * @param {SpotfinderInput} input
 * @param {ProgressFn} [onProgress]
 * @returns {Promise<SpotfinderResult>}
 */
async function runSpotfinder(input, onProgress) {
    const SHAPE = window.FishFinderSpotfinderShape;
    const report = typeof onProgress === "function" ? onProgress : () => {};

    // Tolerate callers that still pass `bbox`. SHAPE.coerceSearchArea
    // accepts either a SearchArea or a BoundingBox.
    const area = SHAPE.coerceSearchArea(input.search_area || input.bbox);
    if (!area) throw new Error("runSpotfinder: invalid search_area");

    const body = JSON.stringify({
        search_area: area,
        params:      input.params || {},
        // User-facing run configuration (environment mode + structure types
        // + preferred source). The backend (resolve_config) translates it
        // into concrete thresholds + a class filter; an empty/absent config
        // falls back to the defaults, so old callers keep working.
        config:      input.config || {},
    });

    // The server streams newline-delimited JSON. We read the ReadableStream
    // until either:
    //   - a `result` event arrives (success)
    //   - an `error` event arrives (throw with the server's message)
    //   - the stream ends without either (treat as a failed run)
    let response;
    try {
        response = await fetch("/spotfinder/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            // Spotfinder runs can be long for big boxes; explicitly
            // opt out of any default response timeout the browser
            // might apply. (Most don't, but be explicit.)
            cache: "no-store",
        });
    } catch (err) {
        throw new Error("Couldn't reach the Spotfinder service. "
                      + "Is the FishFinder backend running?");
    }

    if (!response.ok) {
        let msg = `HTTP ${response.status}`;
        try {
            const text = await response.text();
            if (text) msg += ` — ${text.slice(0, 200)}`;
        } catch (_) { /* swallow */ }
        throw new Error("Spotfinder backend rejected the request: " + msg);
    }
    if (!response.body) {
        throw new Error("Spotfinder backend returned no body.");
    }

    const reader  = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let   buf     = "";
    let   final   = null;
    let   errMsg  = null;

    // Drain the stream line-by-line. Each newline-terminated chunk is one
    // JSON event. Partial lines are buffered until the next read.
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, nl).trim();
            buf = buf.slice(nl + 1);
            if (!line) continue;
            let event;
            try {
                event = JSON.parse(line);
            } catch (parseErr) {
                console.warn("[spotfinder] bad event line:", line);
                continue;
            }
            if (event.type === "progress") {
                const pct   = Number.isFinite(event.pct)   ? event.pct   : 0;
                const label = typeof event.label === "string" ? event.label : "";
                const rem   = Number.isFinite(event.remaining_ms)
                            ? event.remaining_ms
                            : undefined;
                try { report(pct, label, rem); }
                catch (cbErr) { console.warn("[spotfinder] onProgress threw:", cbErr); }
            } else if (event.type === "result") {
                final = event.result;
            } else if (event.type === "error") {
                errMsg = event.message || "Spotfinder failed.";
            } else {
                console.warn("[spotfinder] unknown event type:", event);
            }
        }
    }

    // Flush any trailing partial line (the server emits a newline after
    // every event, so this is defensive).
    if (buf.trim()) {
        try {
            const event = JSON.parse(buf.trim());
            if (event.type === "result") final  = event.result;
            else if (event.type === "error") errMsg = event.message;
        } catch (_) { /* swallow */ }
    }

    if (errMsg) throw new Error(errMsg);
    if (!final) throw new Error("Spotfinder backend closed the stream "
                              + "without returning a result.");
    return final;
}


// ─── Public namespace ─────────────────────────────────────────
window.FishFinderSpotfinder = { run: runSpotfinder };
