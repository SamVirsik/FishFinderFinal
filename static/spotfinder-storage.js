// Spotfinder run persistence.
//
// Stored as a single JSON array in localStorage so we can list all runs
// without iterating storage keys. Each entry is a full SpotfinderResult
// (see spotfinder-runner.js) — the heatmap data URL rides along with
// the rest, which is the simplest way to survive a refresh without a
// background blob registry. Total budget on most browsers is ~5 MB; at
// ~100-300 KB per stub heatmap that comfortably holds dozens of runs.
//
// Quota errors are caught and surfaced so the caller can show a real
// error toast instead of crashing the page. We do not auto-evict —
// silently dropping the user's older results would be worse than
// telling them storage is full.
//
// TODO: PNG heatmaps inflate localStorage fast. When this becomes a real
// constraint, migrate the heatmaps to IndexedDB (or a backend) keyed by
// run_id and keep only the lightweight metadata + spots in this JSON.
// The UI contract (getAllRuns/getRun/saveRun/deleteRun) stays the same.

(() => {
    const KEY         = "fishfinder.spotfinder.runs.v1";
    const ACTIVE_KEY  = "fishfinder.spotfinder.active.v1";
    const OPACITY_KEY = "fishfinder.spotfinder.opacity.v1";

    function _loadAll() {
        try {
            const raw = localStorage.getItem(KEY);
            if (!raw) return [];
            const arr = JSON.parse(raw);
            if (!Array.isArray(arr)) return [];
            return arr.map(_migrateRun).filter(Boolean);
        } catch (err) {
            console.warn("[spotfinder-storage] failed to read runs:", err);
            return [];
        }
    }

    /**
     * One-way migration applied on every read. Pre-rotation-rework runs
     * only have an axis-aligned `bbox` + `heatmap_bounds`. We
     * synthesise a SearchArea (rotation = 0, corners from the bbox)
     * and a matching `heatmap_corners` so the rotated overlay code
     * path doesn't need to special-case old data — callers can always
     * rely on `search_area.corners` and `heatmap_corners` being there.
     *
     * Done at read time (not migrated and re-saved) so we never
     * silently mutate the user's localStorage without an explicit
     * write op. saveRun() naturally re-saves in the new shape the
     * next time the user runs anything on that area.
     */
    function _migrateRun(run) {
        if (!run || !run.run_id) return null;
        const SHAPE = window.FishFinderSpotfinderShape;
        if (!run.search_area) {
            // Old shape: pre-rotation. bbox is the AABB which equals
            // the corners at rotation 0.
            const area = SHAPE && SHAPE.coerceSearchArea(run.bbox);
            if (area) {
                run.search_area = area;
                run.bbox = { ...area.bbox };
            }
        } else if (SHAPE) {
            // Already new shape, but bbox could be missing if a
            // version of the code wrote search_area without the
            // backwards-compat mirror.
            run.search_area = SHAPE.coerceSearchArea(run.search_area);
            if (run.search_area && !run.bbox) {
                run.bbox = { ...run.search_area.bbox };
            }
        }
        if (!run.heatmap_corners && run.search_area) {
            run.heatmap_corners = run.search_area.corners.map(
                (c) => ({ lat: c.lat, lng: c.lng })
            );
        }
        if (!run.heatmap_bounds && run.search_area) {
            run.heatmap_bounds = { ...run.search_area.bbox };
        }
        return run;
    }

    function _saveAll(arr) {
        // May throw QuotaExceededError. Caller's responsibility.
        localStorage.setItem(KEY, JSON.stringify(arr));
    }

    /** @returns {Array<object>} newest first. */
    function getAllRuns() {
        return _loadAll();
    }

    /** @returns {object|null} */
    function getRun(runId) {
        return _loadAll().find(r => r && r.run_id === runId) || null;
    }

    /**
     * Insert or update a run. Returns `{ ok: true }` on success,
     * `{ ok: false, error }` on quota/serialization failure.
     */
    function saveRun(result) {
        if (!result || !result.run_id) {
            return { ok: false, error: new Error("missing run_id") };
        }
        const all = _loadAll();
        const idx = all.findIndex(r => r && r.run_id === result.run_id);
        if (idx >= 0) all[idx] = result;
        else          all.unshift(result);
        try {
            _saveAll(all);
            return { ok: true };
        } catch (err) {
            return { ok: false, error: err };
        }
    }

    function deleteRun(runId) {
        const next = _loadAll().filter(r => r && r.run_id !== runId);
        try {
            _saveAll(next);
            return { ok: true };
        } catch (err) {
            return { ok: false, error: err };
        }
    }

    // ─── Active-run set ─────────────────────────────────────
    // Which run_ids are currently overlaid on the map. Separate from the
    // runs array so a delete of a run still leaves the active set valid
    // (we filter against existing runs on read).
    function getActiveRunIds() {
        try {
            const raw = localStorage.getItem(ACTIVE_KEY);
            if (!raw) return [];
            const arr = JSON.parse(raw);
            return Array.isArray(arr) ? arr.filter(x => typeof x === "string") : [];
        } catch (err) {
            console.warn("[spotfinder-storage] failed to read active set:", err);
            return [];
        }
    }
    function setActiveRunIds(ids) {
        const safe = Array.isArray(ids)
            ? ids.filter(x => typeof x === "string")
            : [];
        try {
            localStorage.setItem(ACTIVE_KEY, JSON.stringify(safe));
            return { ok: true };
        } catch (err) {
            return { ok: false, error: err };
        }
    }


    // ─── Global heatmap opacity ─────────────────────────────
    // One slider in the runs modal applies to every active heatmap. Stored
    // as a 0..1 float so map.js can multiply directly without re-scaling.
    function getGlobalOpacity() {
        try {
            const raw = localStorage.getItem(OPACITY_KEY);
            if (raw == null) return 0.75;
            const n = parseFloat(raw);
            return Number.isFinite(n) && n >= 0 && n <= 1 ? n : 0.75;
        } catch {
            return 0.75;
        }
    }
    function setGlobalOpacity(value) {
        const n = parseFloat(value);
        if (!Number.isFinite(n)) return { ok: false, error: new Error("bad value") };
        const clamped = Math.max(0, Math.min(1, n));
        try {
            localStorage.setItem(OPACITY_KEY, String(clamped));
            return { ok: true };
        } catch (err) {
            return { ok: false, error: err };
        }
    }


    window.FishFinderSpotfinderStorage = {
        getAllRuns,
        getRun,
        saveRun,
        deleteRun,
        getActiveRunIds,
        setActiveRunIds,
        getGlobalOpacity,
        setGlobalOpacity,
    };
})();
