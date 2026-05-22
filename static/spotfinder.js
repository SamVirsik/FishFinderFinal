// Spotfinder analysis page controller.
//
// Reads the bbox the user drew on the map (carried via ?n=&s=&e=&w=),
// drives the algorithm in window.FishFinderSpotfinder, persists the
// result, and renders the four result-section states (empty / running
// / error / ready). The map overlay rendering happens on the map page
// after the user clicks "View on map" — see map.js.
//
// The page state machine is driven by `setResultsState(state)`, which
// flips a single `data-state` attribute on #sf-results; CSS shows the
// matching .sf-results-* pane and hides the others.

(() => {
    // ─── Bounding box from query string ────────────────────
    const $n      = document.getElementById("bbox-n");
    const $s      = document.getElementById("bbox-s");
    const $e      = document.getElementById("bbox-e");
    const $w      = document.getElementById("bbox-w");
    const $tag    = document.getElementById("bbox-status-tag");
    const $hint   = document.getElementById("bbox-hint");

    // ─── Run button + results section ──────────────────────
    const $run        = document.getElementById("spotfinder-run-btn");
    const $runLabel   = document.getElementById("spotfinder-run-label");
    const $results    = document.getElementById("sf-results");
    const $resultsTag = document.getElementById("sf-results-tag");

    const $progressLabel = document.getElementById("sf-progress-label");
    const $progressPct   = document.getElementById("sf-progress-pct");
    const $progressFill  = document.getElementById("sf-progress-fill");

    const $errorMsg = document.getElementById("sf-error-message");
    const $retry    = document.getElementById("sf-retry-btn");

    const $metricSpots      = document.getElementById("sf-metric-spots");
    const $metricRuntime    = document.getElementById("sf-metric-runtime");
    const $metricResolution = document.getElementById("sf-metric-resolution");
    const $metricSource     = document.getElementById("sf-metric-source");
    const $topSpotsList     = document.getElementById("sf-top-spots-list");

    const $rerun   = document.getElementById("sf-rerun-btn");
    const $viewMap = document.getElementById("sf-view-btn");


    // ─── Bbox parse + summary ──────────────────────────────
    const params = new URLSearchParams(window.location.search);
    const raw = {
        n: parseFloat(params.get("n")),
        s: parseFloat(params.get("s")),
        e: parseFloat(params.get("e")),
        w: parseFloat(params.get("w")),
    };
    const bboxValid =
        Number.isFinite(raw.n) && Number.isFinite(raw.s) &&
        Number.isFinite(raw.e) && Number.isFinite(raw.w) &&
        raw.n >= -90 && raw.n <= 90 && raw.s >= -90 && raw.s <= 90 &&
        raw.e >= -180 && raw.e <= 180 && raw.w >= -180 && raw.w <= 180 &&
        raw.n > raw.s;

    const bbox = bboxValid
        ? { north: raw.n, south: raw.s, east: raw.e, west: raw.w }
        : null;

    function fmtLat(v) { return `${v.toFixed(5)}° ${v >= 0 ? "N" : "S"}`; }
    function fmtLon(v) { return `${v.toFixed(5)}° ${v >= 0 ? "E" : "W"}`; }

    if (bbox) {
        $n.textContent = fmtLat(bbox.north);
        $s.textContent = fmtLat(bbox.south);
        $e.textContent = fmtLon(bbox.east);
        $w.textContent = fmtLon(bbox.west);
        $tag.textContent = "Defined";
        $tag.classList.add("ok");
        $hint.textContent =
            "Press Run Spotfinder to analyse this area. Results will save automatically.";
        $run.disabled = false;
        $run.removeAttribute("aria-disabled");
        $run.removeAttribute("title");
    } else {
        $tag.textContent = "No selection";
        $hint.textContent =
            "No bounding box was supplied. Return to the map and draw an area before running Spotfinder.";
        $run.disabled = true;
        $run.setAttribute("aria-disabled", "true");
        $run.title = "Draw a bounding box on the map first";
    }


    // ─── Results state machine ─────────────────────────────
    function setResultsState(state) {
        $results.dataset.state = state;
        // Tag styling: muted in empty/running, accent on ready/error.
        $resultsTag.classList.remove("ok", "muted");
        switch (state) {
            case "empty":
                $resultsTag.textContent = "No run yet";
                $resultsTag.classList.add("muted");
                break;
            case "running":
                $resultsTag.textContent = "Running…";
                $resultsTag.classList.add("muted");
                break;
            case "ready":
                $resultsTag.textContent = "Complete";
                $resultsTag.classList.add("ok");
                break;
            case "error":
                $resultsTag.textContent = "Failed";
                $resultsTag.classList.add("muted");
                break;
        }
    }
    setResultsState("empty");


    // ─── Run / re-run ──────────────────────────────────────
    let currentRun = null;   // SpotfinderResult once available
    let inFlight   = false;

    function setRunButton(running) {
        $run.disabled = running || !bbox;
        $rerun.disabled = running;
        if (running) {
            $run.classList.add("pending");
            $runLabel.textContent = "Running…";
        } else {
            $run.classList.remove("pending");
            $runLabel.textContent = currentRun ? "Run again" : "Run Spotfinder";
        }
    }

    function onProgress(pct, label) {
        const clamped = Math.max(0, Math.min(100, pct));
        $progressFill.style.width = `${clamped.toFixed(1)}%`;
        $progressPct.textContent  = `${Math.round(clamped)}%`;
        if (label) $progressLabel.textContent = label;
    }

    async function run() {
        if (!bbox || inFlight) return;
        inFlight = true;
        setRunButton(true);
        setResultsState("running");
        onProgress(0, "Starting…");
        try {
            const result = await window.FishFinderSpotfinder.run(
                { bbox, params: {} },   // params reserved for the future tuning UI
                onProgress,
            );
            // Persist before painting results so a refresh during the
            // paint window doesn't lose the run.
            const save = window.FishFinderSpotfinderStorage.saveRun(result);
            if (!save.ok) {
                // Most likely QuotaExceededError — let the user know
                // rather than silently dropping the result on the floor.
                throw new Error(
                    "Couldn't save run (storage full). " +
                    "Delete some past runs from the map page and try again."
                );
            }
            currentRun = result;
            renderReady(result);
            setResultsState("ready");
        } catch (err) {
            console.error("[spotfinder] run failed:", err);
            $errorMsg.textContent = (err && err.message) || String(err);
            setResultsState("error");
        } finally {
            inFlight = false;
            setRunButton(false);
        }
    }

    function renderReady(result) {
        $metricSpots.textContent      = String(result.spots.length);
        $metricRuntime.textContent    = `${(result.manifest.runtime_ms / 1000).toFixed(1)} s`;
        $metricResolution.textContent = `${result.manifest.resolution_m} m`;
        $metricSource.textContent     = result.manifest.data_source;

        // Top spots — already sorted by score in the runner output.
        // Showing up to 5 keeps the page compact; users see the rest on
        // the map after clicking through.
        const top = result.spots.slice(0, 5);
        $topSpotsList.innerHTML = top.map((s, i) => `
            <li class="sf-top-spot">
                <span class="sf-top-spot-rank">#${i + 1}</span>
                <span class="sf-top-spot-coord">${s.lat.toFixed(4)}, ${s.lng.toFixed(4)}</span>
                <span class="sf-top-spot-depth">${s.depth_m.toFixed(0)} m</span>
                <span class="sf-top-spot-score">${(s.score * 100).toFixed(0)}%</span>
            </li>
        `).join("");

        $viewMap.href = `/?run=${encodeURIComponent(result.run_id)}`;
    }

    $run.addEventListener("click", run);
    $rerun.addEventListener("click", run);
    $retry.addEventListener("click", run);
})();
