// Spotfinder analysis page controller.
//
// Reads the search area the user drew on the map (carried in the URL —
// either the new clat/clng/w_m/h_m/r form or the legacy n/s/e/w
// bbox), drives the algorithm in window.FishFinderSpotfinder,
// persists the result, and renders the four result-section states
// (empty / running / error / ready). The map overlay rendering
// happens on the map page after the user clicks "View on map" — see
// map.js.
//
// The page state machine is driven by `setResultsState(state)`, which
// flips a single `data-state` attribute on #sf-results; CSS shows the
// matching .sf-results-* pane and hides the others.

(() => {
    const SHAPE = window.FishFinderSpotfinderShape;

    // ─── Search area readout ───────────────────────────────
    const $center   = document.getElementById("area-center");
    const $size     = document.getElementById("area-size");
    const $rotation = document.getElementById("area-rotation");
    const $areaKm2  = document.getElementById("area-size-km2");
    const $tag      = document.getElementById("bbox-status-tag");
    const $hint     = document.getElementById("bbox-hint");

    // ─── Run button + results section ──────────────────────
    const $run        = document.getElementById("spotfinder-run-btn");
    const $runLabel   = document.getElementById("spotfinder-run-label");
    const $results    = document.getElementById("sf-results");
    const $resultsTag = document.getElementById("sf-results-tag");

    const $progressLabel = document.getElementById("sf-progress-label");
    const $progressPct   = document.getElementById("sf-progress-pct");
    const $progressFill  = document.getElementById("sf-progress-fill");
    const $progressEta   = document.getElementById("sf-progress-eta");

    const $errorMsg = document.getElementById("sf-error-message");
    const $retry    = document.getElementById("sf-retry-btn");

    const $metricSpots      = document.getElementById("sf-metric-spots");
    const $metricRuntime    = document.getElementById("sf-metric-runtime");
    const $metricResolution = document.getElementById("sf-metric-resolution");
    const $metricSource     = document.getElementById("sf-metric-source");
    const $topSpotsList     = document.getElementById("sf-top-spots-list");

    const $rerun   = document.getElementById("sf-rerun-btn");
    const $viewMap = document.getElementById("sf-view-btn");


    // ─── Parse URL into a SearchArea ───────────────────────
    // SHAPE.decodeFromUrl honours both the new (clat/clng/w_m/h_m/r)
    // and the legacy (n/s/e/w) shapes — old bookmarks survive.
    const params = new URLSearchParams(window.location.search);
    const searchArea = SHAPE.decodeFromUrl(params);

    function fmtLatLng(lat, lng) {
        const ns = lat >= 0 ? "N" : "S";
        const ew = lng >= 0 ? "E" : "W";
        return `${Math.abs(lat).toFixed(4)}° ${ns}, `
             + `${Math.abs(lng).toFixed(4)}° ${ew}`;
    }
    function fmtKm(m) {
        // Show m for < 1 km, km otherwise. Tabular numbers in the
        // style sheet keep the values aligned.
        if (m < 1000) return `${m.toFixed(0)} m`;
        return `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
    }
    function fmtRotation(deg) {
        // Wrap to (-180, 180] so a user-set 359° reads as -1° instead
        // of "almost a full turn".
        let d = ((deg + 180) % 360 + 360) % 360 - 180;
        if (d === -180) d = 180;
        if (Math.abs(d) < 0.5) return "0° — axis-aligned";
        const dir = d > 0 ? "clockwise" : "counter-clockwise";
        return `${Math.abs(d).toFixed(d < 1 ? 1 : 0)}° ${dir}`;
    }

    if (searchArea) {
        $center.textContent   = fmtLatLng(searchArea.center.lat, searchArea.center.lng);
        $size.textContent     = `${fmtKm(searchArea.width_m)} × ${fmtKm(searchArea.height_m)}`;
        $rotation.textContent = fmtRotation(searchArea.rotation_deg);
        $areaKm2.textContent  = `${SHAPE.areaKm2(searchArea).toFixed(2)} km²`;
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
            "No search area was supplied. Return to the map and draw an area before running Spotfinder.";
        $run.disabled = true;
        $run.setAttribute("aria-disabled", "true");
        $run.title = "Draw a search area on the map first";
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
        $run.disabled = running || !searchArea;
        $rerun.disabled = running;
        if (running) {
            $run.classList.add("pending");
            $runLabel.textContent = "Running…";
        } else {
            $run.classList.remove("pending");
            $runLabel.textContent = currentRun ? "Run again" : "Run Spotfinder";
        }
    }

    function fmtRemaining(ms) {
        // Smooth the noisy estimates the algorithm produces in its
        // early ticks: cap at a sane max, round to whole seconds at the
        // low end and whole minutes at the high end.
        if (!Number.isFinite(ms) || ms <= 0) return "";
        const s = Math.max(1, Math.round(ms / 1000));
        if (s < 90) return `~${s}s remaining`;
        const m = Math.floor(s / 60);
        const rem = s % 60;
        if (m < 10) return `~${m}m ${rem}s remaining`;
        return `~${m}m remaining`;
    }

    function onProgress(pct, label, remainingMs) {
        const clamped = Math.max(0, Math.min(100, pct));
        $progressFill.style.width = `${clamped.toFixed(1)}%`;
        $progressPct.textContent  = `${Math.round(clamped)}%`;
        if (label) $progressLabel.textContent = label;
        if ($progressEta) {
            // Pct < 1 produces wildly unstable estimates (the elapsed-
            // time-extrapolation formula divides by pct), so we hide
            // the hint until the algorithm has actually started doing work.
            // We also hide it as soon as we cross 99% — at that point
            // "Done in a moment" is more honest than "~1s remaining".
            const eta = (clamped >= 1 && clamped < 99)
                      ? fmtRemaining(remainingMs)
                      : "";
            $progressEta.textContent = eta || "This will take a few seconds.";
        }
    }

    async function run() {
        if (!searchArea || inFlight) return;
        inFlight = true;
        setRunButton(true);
        setResultsState("running");
        onProgress(0, "Starting…");
        try {
            const result = await window.FishFinderSpotfinder.run(
                { search_area: searchArea, params: {} },   // params reserved for the future tuning UI
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
        // the map after clicking through. Each entry surfaces the region
        // class (pinnacle / ledge / …) and any NMS-suppressed sibling
        // classes as small chips, so the reader can read what KIND of
        // feature each spot is at a glance.
        const esc = (s) => String(s).replace(/[&<>"']/g, c => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
        const top = result.spots.slice(0, 5);
        $topSpotsList.innerHTML = top.map((s, i) => {
            const cls = (s.features && s.features.class) || null;
            const tags = (s.features && Array.isArray(s.features.secondary_tags))
                       ? s.features.secondary_tags : [];
            // Class + secondary tags ride INSIDE the coord cell so we
            // don't have to redo the four-column grid in style.css. The
            // chip flow is line-wrapping by default; on narrow viewports
            // the second line just spans under the coordinates.
            const classChip = cls
                ? ` <span class="sf-top-spot-class">${esc(cls)}</span>` : "";
            const tagChips = tags.length
                ? ` <span class="sf-top-spot-tags">+ ${tags.map(esc).join(", ")}</span>`
                : "";
            return `
            <li class="sf-top-spot">
                <span class="sf-top-spot-rank">#${i + 1}</span>
                <span class="sf-top-spot-coord">${s.lat.toFixed(4)}, ${s.lng.toFixed(4)}${classChip}${tagChips}</span>
                <span class="sf-top-spot-depth">${s.depth_m.toFixed(0)} m</span>
                <span class="sf-top-spot-score">${(s.score * 100).toFixed(0)}%</span>
            </li>`;
        }).join("");

        $viewMap.href = `/?run=${encodeURIComponent(result.run_id)}`;
    }

    $run.addEventListener("click", run);
    $rerun.addEventListener("click", run);
    $retry.addEventListener("click", run);
})();
