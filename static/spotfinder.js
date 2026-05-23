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


    // ─── Algorithm configuration ───────────────────────────
    // SF_CONFIG is the single client-side source of truth for the two
    // configuration axes; it mirrors ENVIRONMENT_MODES / STRUCTURE_TYPES in
    // src/spotfinder.py. To add a mode or structure type, add a row here
    // and the matching backend entry — nothing else on this page changes.
    // Structure colours match CLASS_RGB in static/map.js so the chips on
    // this page read the same as the markers + polygons on the map.
    const SF_CONFIG = {
        environments: [
            { key: "reef", label: "Reef", default: true,
              hint: "Stricter. The reef floor is already textured, so only "
                  + "features that clearly dominate their surroundings count." },
            { key: "flat", label: "Flat bottom",
              hint: "Looser. Surfaces subtle structure that stands out "
                  + "against otherwise featureless ground." },
        ],
        // key must match a STRUCTURE_TYPES key on the backend. defMin/defMax
        // mirror DEFAULT_SIZE_RANGES in src/spotfinder.py; `measure` mirrors
        // SIZE_MEASURE ("longest" = longest footprint extent, "width" =
        // cross-feature width — see resolve_size_ranges). `uiMax` is the
        // slider's top end (also clamps the numeric inputs); pick it above the
        // default max so there's room to widen a range per trip.
        structures: [
            { key: "pinnacle", label: "Pinnacle",     color: "rgb(255,130,90)",
              measure: "longest", defMin:  5, defMax:  60, uiMax: 200 },
            { key: "mound",    label: "Mound / hump", color: "rgb(255,200,90)",
              measure: "width",   defMin: 20, defMax: 150, uiMax: 400 },
            { key: "ledge",    label: "Ledge",        color: "rgb(255,220,130)",
              measure: "width",   defMin:  5, defMax:  80, uiMax: 300 },
            { key: "saddle",   label: "Saddle",       color: "rgb(200,140,255)",
              measure: "longest", defMin: 20, defMax: 200, uiMax: 400 },
            { key: "hole",     label: "Hole",         color: "rgb(120,180,255)",
              measure: "longest", defMin: 10, defMax: 150, uiMax: 400 },
            { key: "channel",  label: "Channel",      color: "rgb(90,200,255)",
              measure: "width",   defMin: 10, defMax: 100, uiMax: 300 },
        ],
    };

    // Slider granularity + smallest allowed gap between the two handles (ft).
    const SIZE_STEP_FT = 5;
    const SIZE_MIN_SPAN_FT = 5;
    // Short label for the per-row measure tag.
    const MEASURE_LABEL = { longest: "longest extent", width: "width" };

    const $cfgSource     = document.getElementById("sf-cfg-source");
    const $cfgSourceHint = document.getElementById("sf-cfg-source-hint");
    const $cfgEnv        = document.getElementById("sf-cfg-environment");
    const $cfgEnvHint    = document.getElementById("sf-cfg-environment-hint");
    const $cfgStructs    = document.getElementById("sf-cfg-structures");
    const $cfgStructHint = document.getElementById("sf-cfg-structures-hint");
    const $cfgTag        = document.getElementById("sf-cfg-tag");

    const $sizeRows    = document.getElementById("sf-cfg-size-rows");
    const $sizeToggle  = document.getElementById("sf-size-toggle");
    const $sizeBody    = document.getElementById("sf-size-body");
    const $sizeSummary = document.getElementById("sf-size-summary");
    const $sizeReset   = document.getElementById("sf-size-reset");

    // The source the user had active on the map, carried in the URL.
    const requestedSource = params.get("src");

    // Environment dropdown.
    for (const e of SF_CONFIG.environments) {
        const opt = document.createElement("option");
        opt.value = e.key;
        opt.textContent = e.label;
        if (e.default) opt.selected = true;
        $cfgEnv.appendChild(opt);
    }
    function syncEnvHint() {
        const e = SF_CONFIG.environments.find(x => x.key === $cfgEnv.value);
        $cfgEnvHint.textContent = e ? e.hint : "";
    }

    // Structure-type checkboxes — all on by default.
    for (const s of SF_CONFIG.structures) {
        const label = document.createElement("label");
        label.className = "sf-check checked";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = s.key;
        cb.checked = true;
        const dot = document.createElement("span");
        dot.className = "sf-check-dot";
        dot.style.background = s.color;
        const text = document.createElement("span");
        text.className = "sf-check-text";
        text.textContent = s.label;
        label.append(cb, dot, text);
        $cfgStructs.appendChild(label);
    }

    // ─── Per-type size-range rows ──────────────────────────
    // One dual-handle slider + numeric min/max (ft) per structure type. Rows
    // for all six types are built once; a row is hidden when its type is
    // unchecked above. The slider markup reuses the Layer-Controls depth-range
    // pattern (.depth-range-slider) so it reads identically to the rest of the
    // app. State lives in the inputs themselves; readConfig() collects them.
    const sizeRowState = {};   // key → { row, minRange, maxRange, minNum, maxNum, spec }

    function clampStep(v) {
        return Math.round(v / SIZE_STEP_FT) * SIZE_STEP_FT;
    }
    // Normalise a (lo, hi) pair to [0, uiMax], step-aligned, with lo + span
    // <= hi. `changed` says which handle the user just moved, so we push the
    // OTHER one out of the way rather than fighting the drag.
    function clampPair(lo, hi, uiMax, changed) {
        lo = Math.max(0, Math.min(uiMax, clampStep(lo)));
        hi = Math.max(0, Math.min(uiMax, clampStep(hi)));
        if (changed === "min") {
            if (hi < lo + SIZE_MIN_SPAN_FT) hi = Math.min(uiMax, lo + SIZE_MIN_SPAN_FT);
            if (lo > hi) lo = hi;
        } else {
            if (lo > hi - SIZE_MIN_SPAN_FT) lo = Math.max(0, hi - SIZE_MIN_SPAN_FT);
            if (hi < lo) hi = lo;
        }
        return [lo, hi];
    }
    function writeRow(st, lo, hi) {
        st.minRange.value = String(lo);
        st.maxRange.value = String(hi);
        st.minNum.value   = String(lo);
        st.maxNum.value   = String(hi);
    }
    function onSizeInput(key, which, ev) {
        const st = sizeRowState[key];
        if (!st) return;
        let lo = parseInt(st.minRange.value, 10);
        let hi = parseInt(st.maxRange.value, 10);
        const v = parseInt(ev.target.value, 10);
        if (Number.isFinite(v)) {
            if (which === "min") lo = v; else hi = v;
        }
        if (!Number.isFinite(lo)) lo = st.spec.defMin;
        if (!Number.isFinite(hi)) hi = st.spec.defMax;
        [lo, hi] = clampPair(lo, hi, st.spec.uiMax, which);
        writeRow(st, lo, hi);
        updateSizeSummary();
    }

    for (const s of SF_CONFIG.structures) {
        const row = document.createElement("div");
        row.className = "sf-size-row";
        row.dataset.key = s.key;
        row.innerHTML = `
            <div class="sf-size-row-head">
                <span class="sf-check-dot" style="background:${s.color}"></span>
                <span class="sf-size-row-label">${s.label}</span>
                <span class="sf-size-row-measure">${MEASURE_LABEL[s.measure] || ""}</span>
            </div>
            <div class="sf-size-controls">
                <div class="depth-range-slider sf-size-slider">
                    <input type="range" class="slider depth-range-input sf-size-min"
                           min="0" max="${s.uiMax}" step="${SIZE_STEP_FT}"
                           value="${s.defMin}"
                           aria-label="${s.label} minimum size (feet)">
                    <input type="range" class="slider depth-range-input sf-size-max"
                           min="0" max="${s.uiMax}" step="${SIZE_STEP_FT}"
                           value="${s.defMax}"
                           aria-label="${s.label} maximum size (feet)">
                </div>
                <div class="sf-size-inputs">
                    <input type="number" class="sf-size-num sf-size-num-min"
                           min="0" max="${s.uiMax}" step="${SIZE_STEP_FT}"
                           value="${s.defMin}"
                           aria-label="${s.label} minimum size (feet)">
                    <span class="sf-size-dash">–</span>
                    <input type="number" class="sf-size-num sf-size-num-max"
                           min="0" max="${s.uiMax}" step="${SIZE_STEP_FT}"
                           value="${s.defMax}"
                           aria-label="${s.label} maximum size (feet)">
                    <span class="sf-size-unit">ft</span>
                </div>
            </div>`;
        $sizeRows.appendChild(row);

        const st = {
            row,
            spec:     s,
            minRange: row.querySelector(".sf-size-min"),
            maxRange: row.querySelector(".sf-size-max"),
            minNum:   row.querySelector(".sf-size-num-min"),
            maxNum:   row.querySelector(".sf-size-num-max"),
        };
        sizeRowState[s.key] = st;

        // Sliders clamp live as they drag. Numerics normalise on change
        // (blur / Enter) only — clamping every keystroke would fight someone
        // typing "120" (it'd reformat at "1", then "12", …).
        st.minRange.addEventListener("input",  (e) => onSizeInput(s.key, "min", e));
        st.maxRange.addEventListener("input",  (e) => onSizeInput(s.key, "max", e));
        st.minNum.addEventListener("change", (e) => onSizeInput(s.key, "min", e));
        st.maxNum.addEventListener("change", (e) => onSizeInput(s.key, "max", e));
    }

    function isRowDefault(s) {
        const st = sizeRowState[s.key];
        return parseInt(st.minRange.value, 10) === s.defMin
            && parseInt(st.maxRange.value, 10) === s.defMax;
    }
    function updateSizeSummary() {
        const n = SF_CONFIG.structures.filter((s) => !isRowDefault(s)).length;
        $sizeSummary.textContent = n === 0 ? "Defaults" : `${n} customized`;
        $sizeSummary.classList.toggle("sf-size-summary-on", n > 0);
    }
    // A size row is shown only while its structure type is targeted. Unchecked
    // → the row greys out and stops responding (still in the DOM so its value
    // is preserved if the user re-checks the type).
    function syncSizeRowVisibility() {
        const on = new Set(selectedStructures());
        for (const s of SF_CONFIG.structures) {
            const st = sizeRowState[s.key];
            const enabled = on.has(s.key);
            st.row.classList.toggle("disabled", !enabled);
            for (const el of [st.minRange, st.maxRange, st.minNum, st.maxNum]) {
                el.disabled = !enabled;
            }
        }
    }
    function resetSizeRanges() {
        for (const s of SF_CONFIG.structures) {
            writeRow(sizeRowState[s.key], s.defMin, s.defMax);
        }
        updateSizeSummary();
    }
    // Collect every type's current range (all six, regardless of selection),
    // so a saved run records the complete size configuration — matching the
    // backend, which also keeps a full per-type map.
    function collectSizeRanges() {
        const out = {};
        for (const s of SF_CONFIG.structures) {
            const st = sizeRowState[s.key];
            out[s.key] = {
                min_ft: parseInt(st.minRange.value, 10),
                max_ft: parseInt(st.maxRange.value, 10),
            };
        }
        return out;
    }

    $sizeToggle.addEventListener("click", () => {
        const open = $sizeToggle.getAttribute("aria-expanded") === "true";
        $sizeToggle.setAttribute("aria-expanded", open ? "false" : "true");
        $sizeBody.hidden = open;
    });
    $sizeReset.addEventListener("click", resetSizeRanges);

    function selectedStructures() {
        return Array.from($cfgStructs.querySelectorAll("input:checked"))
                    .map(c => c.value);
    }
    function syncStructuresValidity() {
        const n = selectedStructures().length;
        for (const lbl of $cfgStructs.querySelectorAll(".sf-check")) {
            lbl.classList.toggle("checked", lbl.querySelector("input").checked);
        }
        if (n === 0) {
            $cfgStructHint.textContent =
                "Select at least one structure type to run.";
            $cfgStructHint.classList.add("sf-hint-warn");
        } else {
            $cfgStructHint.textContent =
                "Results are limited to the structure types you select. "
              + "At least one is required.";
            $cfgStructHint.classList.remove("sf-hint-warn");
        }
    }
    function syncCfgTag() {
        const e = SF_CONFIG.environments.find(x => x.key === $cfgEnv.value);
        const n = selectedStructures().length;
        $cfgTag.textContent = `${e ? e.label : "—"} · ${n} type${n === 1 ? "" : "s"}`;
        $cfgTag.classList.remove("muted");
    }

    $cfgEnv.addEventListener("change", () => { syncEnvHint(); syncCfgTag(); });
    $cfgStructs.addEventListener("change", () => {
        syncStructuresValidity();
        syncCfgTag();
        syncSizeRowVisibility();
        updateRunGate();
    });

    // Source dropdown — populated from /sources (same registry the map
    // dropdown uses), with the map's active source preselected.
    fetch("/sources", { cache: "no-store" })
        .then(r => r.ok ? r.json()
                        : Promise.reject(new Error(`/sources HTTP ${r.status}`)))
        .then((payload) => {
            const list = payload.sources || [];
            $cfgSource.innerHTML = "";
            const auto = document.createElement("option");
            auto.value = "";
            auto.textContent = "Auto — best available coverage";
            $cfgSource.appendChild(auto);
            for (const s of list) {
                const opt = document.createElement("option");
                opt.value = s.id;
                opt.textContent = s.experimental
                    ? `${s.display_name} (experimental)` : s.display_name;
                if (s.notes) opt.title = s.notes;
                $cfgSource.appendChild(opt);
            }
            if (requestedSource && list.some(s => s.id === requestedSource)) {
                $cfgSource.value = requestedSource;
                $cfgSourceHint.textContent =
                    "Using the source you had active on the map. "
                  + "Change it if you like.";
            } else if (payload.default && list.some(s => s.id === payload.default)) {
                $cfgSource.value = payload.default;
            } else {
                $cfgSource.value = "";
            }
        })
        .catch((err) => {
            console.warn("[spotfinder] failed to load /sources:", err);
            $cfgSource.innerHTML =
                '<option value="" selected>Auto — best available coverage</option>';
            $cfgSourceHint.textContent =
                "Couldn't load the source list — Spotfinder will auto-pick.";
        });

    function readConfig() {
        return {
            environment:     $cfgEnv.value,
            structure_types: selectedStructures(),
            source:          $cfgSource.value || null,
            size_ranges:     collectSizeRanges(),
        };
    }
    function canRun() {
        return !!searchArea && selectedStructures().length > 0;
    }

    syncEnvHint();
    syncStructuresValidity();
    syncCfgTag();
    syncSizeRowVisibility();
    updateSizeSummary();

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
        $run.disabled = !canRun();
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
        $run.disabled = running || !canRun();
        $rerun.disabled = running;
        if (running) {
            $run.classList.add("pending");
            $runLabel.textContent = "Running…";
        } else {
            $run.classList.remove("pending");
            $runLabel.textContent = currentRun ? "Run again" : "Run Spotfinder";
        }
    }

    // Re-evaluate the Run gate after a config change (e.g. all structure
    // types deselected → Run disabled). No-op mid-run so we don't re-enable
    // the button while a run is in flight.
    function updateRunGate() {
        if (!inFlight) setRunButton(false);
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
                // `config` carries the user-facing axes (environment +
                // structure types + source). `params` stays the low-level
                // per-knob override channel for a future tuning UI.
                { search_area: searchArea, params: {}, config: readConfig() },
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
