# FishFinder Audit Report

Read-only investigation. No code changes. Findings below; review and approve
fixes in a follow-up.

## Summary

The viewer's hot path (tile fetch → worker render → canvas swap) is in good
shape: the architecture is consistent with the design notes in `CLAUDE.md`,
the wire format / cache layers / layer-swap invariants all line up between
client and server, and the cached `dem-tiles` source served tiles cleanly
under test. The damage is concentrated at the edges: a Reload button whose
on-screen description contradicts what it now does, several stale or wrong
mentions in `CLAUDE.md` and `.gitignore`, a near-dead Python file
(`src/FishFinderTools.py`) that imports and references symbols that no
longer exist, a handful of unreferenced assets in `static/images/` and
`img/`, and a server-side NOAA fetcher that — in this sandbox — fails
uniformly with SSL cert errors (likely an environment/cert-bundle problem,
not a code bug, but it means I could not exercise any non-cached source
end-to-end). The UI was exercised by HTTP probes + static review only; I
had no browser tool available, so search/measure/depth-card UX was
verified by reading the wiring, not by clicking.

## What works (verified)

- **Build & dependency install.** `pip install -r requirements.txt` succeeds.
  Only delta on a fresh-ish Python 3.12 box: it upgrades Flask 3.0.3 → 3.1.3
  and Werkzeug 3.0.4 → 3.1.8 to satisfy `flask>=3.1.0`. No build step beyond pip.
- **Server startup.** `python app.py` prints `FishFinder running at
  http://127.0.0.1:8080` and serves immediately. No warnings on import.
- **Static endpoints.** `/`, `/map`, `/static/map.js`, `/static/analyses.js`,
  `/static/analyses-worker.js`, `/static/style.css`, `/static/main.js`,
  `POST /heartbeat` — all return 200/204 with sensible payload sizes.
- **`/raster` happy path.** `/raster/dem-tiles/256/10/280/442.bin` returns
  331,792 bytes in ~200 ms (16-byte header + 288×288 × float32 = 331,792).
  Header layout matches the wire spec in `app.py:73-98`.
- **Server bounds checks.** `z=23` returns 503 (matches
  `LayerGeneration.py:286` cap). `z=-1` returns 404 (Flask `<int:>`
  converter rejects negatives).
- **Heartbeat shutdown loop.** Watchdog at `app.py:120-133` correctly stays
  silent until the first `/heartbeat` arrives, then self-terminates ~3s
  after the last ping — verified by curling once and watching the process
  exit on the next tick.
- **Dict/registry alignment across the four code paths.** Visualization
  options in `templates/map.html:88-95`, `ANALYSES` UI map in
  `static/map.js:101-158`, `self.FFAnalyses` in `static/analyses.js:488-497`,
  and `ANALYSES` in `src/analyses.py:357-366` all have the same eight keys.
  Bathymetry source options in `templates/map.html:106-113` align with
  `_SOURCE_SPEC` in `src/LayerGeneration.py:115-154`.
- **Layer-swap invariants in code.** `applyConfig` (`static/map.js:625-688`)
  correctly bumps `pendingApplyId`, tears down stale pending layers, and
  the `.catch` branch on `whenLayerView` deliberately does **not** clear
  the globals — the in-line comment at L676-687 explains the orphan-layer
  bug that would otherwise occur. Looks right.
- **Worker resilience.** `resetWorker` (`static/map.js:208-213`) drains the
  `pending` map as `'error'` before terminating, so a crash doesn't leave
  dangling `fetchTile` promises (the failure mode the original rework was
  written to fix).
- **Wire format consistency.** Server packs `<IIfI` (`app.py:93`); worker
  reads in the same order (`static/analyses-worker.js:67-72`).

## What's broken

### High

1. **Reload button copy contradicts behavior.** `templates/map.html:156`
   (`title="Clear caches and re-render…"`) and `templates/map.html:164`
   (`<p>Clears caches and re-renders the visible tiles…</p>`) both promise
   cache-clearing. The implementation at `static/map.js:1019-1080`
   intentionally does not clear caches anymore — the in-line comment at
   `static/map.js:1028-1040` explicitly explains why
   (`canvasCache` wipe is "pointless", `inflightCanvas` wipe is "unsafe",
   worker restart was "what was breaking the view on every press"). Severity
   high because a user with a stuck tile will press this button trusting
   the copy, get an Apply-equivalent re-render, and conclude the bug is
   worse than it is.
   *Repro:* hover the button → tooltip lies; read the hint underneath →
   same lie. Press button → no cache state changes.
   *Fix approach:* rewrite the title attribute and hint to something like
   "Re-render the visible tiles with the current settings" (no mention of
   caches). The button is still a useful manual-reset.

2. **`src/FishFinderTools.py` `__main__` block is broken.**
   `src/FishFinderTools.py:161-308` calls `roughDivide` (L204), `oneFootSteps`
   (L208), `contourMapV1` (L210), `contourMapV1General` (L212), `contourMapV2`
   (L219), `get2GPS` (L245) — none of which are defined in the file or
   imported. Running `python src/FishFinderTools.py` and choosing any
   analysis path crashes with `NameError`. Severity high if anyone actually
   tries to run it; in practice nobody can, because the script is never
   referenced. Severity drops to "embarrassing" once you know that.
   *Fix approach:* delete the file. `view_spot.py` imports
   `zeroToOneFifty` from it (L24) but never calls the symbol — only
   `color_relief` from `src/analyses.py` is used. Removing the import in
   `view_spot.py` and deleting `FishFinderTools.py` outright drops 308
   lines of dead code.

### Medium

3. **Stale data sources can't be tested in this sandbox — NOAA SSL fails.**
   Every fresh fetch (any uncached `(source, z, x, y)`) raised
   `SSLCertVerificationError: unable to get local issuer certificate`. Only
   tiles already on disk under `img/raster/dem-tiles/256/…` could be served.
   Looks environmental (Windows trust store / `certifi` mismatch — `requests`
   2.32.3 + certifi 2026.4.22 is current), but the symptom matters because
   `_fetch_raster_bytes` (`src/LayerGeneration.py:167-197`) has no
   diagnostic surfacing: the user sees a 503 in the browser and a single
   `[NOAA] dem-all: request failed: …SSLCertVerificationError…` line in
   the terminal, nothing more. Severity medium because (a) the actual fix
   is a one-line `verify=` or env var, not code, and (b) the prod user
   probably never hits it — but a fresh dev box absolutely will.
   *Fix approach:* either accept this as user-env and document in the
   README ("if you see SSL errors, install certifi / set
   `REQUESTS_CA_BUNDLE`"), or have `LayerGeneration` log a one-time
   suggestion when it sees `SSLError` specifically.

4. **`/heartbeat` watchdog kills the server on any single-shot client.**
   `app.py:120-133`: once `_last_heartbeat` becomes non-None, the watcher
   exits after `HEARTBEAT_TIMEOUT_S = 3.0`. This is intentional for the
   browser-close case, but it makes the server **un-usable from `curl`,
   Postman, scripts, or `httpx` smoke tests** — every probe-and-go pattern
   causes the server to terminate 3 seconds later. I tripped it three
   times while writing this report. Severity medium because the design
   is documented and largely correct, but the safety net is mis-sized:
   one ping + 3 s of silence shouldn't be the trigger.
   *Fix approach:* require at least *two* pings before arming the kill
   switch, or bump the timeout to ~10 s, or guard with an env var so the
   shutdown can be opted out for testing.

5. **Outdated/wrong claims in `CLAUDE.md`.**
   - L44: `app.py` listed as exposing `/raster, /depth, /map, /.` — `/depth`
     does not exist (only `/raster`, `/`, `/map`, `/heartbeat`).
   - The endpoint list also omits `/heartbeat` entirely even though L80-95
     in `map.js` explains why it exists.
   - L73 still describes `img/blank.png` as "Unused (kept for backwards
     compatibility)" — fine on its own, but `img/_blank.png` also exists
     on disk (gitignored) and nothing references either. The earlier
     `_blank.png` was likely just a renamed artifact.
   *Fix approach:* edit `CLAUDE.md` to remove `/depth`, add `/heartbeat`,
   delete the `blank.png` entry from the layout block, then delete both
   PNGs from disk.

6. **`.gitignore` references paths the app no longer uses.**
   `.gitignore:9` (`flask_session/`), `:12-13` (`img/tile/`, `img/noaa/`).
   These are leftover from the pre-rework architecture where the server
   colourised PNGs. Harmless but noisy.
   *Fix approach:* drop the three stale entries.

7. **Unknown bathymetry-source name silently falls back to default but
   disk-caches under the wrong name.** `_source` (`LayerGeneration.py:159-160`)
   returns `dem-tiles` for any unknown name; but `fetch_tile_raster`
   (`LayerGeneration.py:294`) builds the cache_dir from the *requested*
   `data_source`, not the resolved spec. Result: `/raster/typo-source/…`
   serves dem-tiles data but caches the bytes under `img/raster/typo-source/`.
   I verified the silent fallback returned 503 only because of the SSL
   issue; on a working network it would 200 with mis-labelled cache.
   *Repro:* `GET /raster/foo/256/10/280/442.bin` in an environment with
   NOAA reachable — observe new directory created at `img/raster/foo/256/`.
   *Fix approach:* in `_source`, return `None` on miss and have
   `fetch_tile_raster` short-circuit with a 4xx; or normalise the source
   name to the resolved spec's identifier before computing `cache_dir`.

8. **ArcGIS popup hidden via CSS rather than the SDK option.**
   `static/style.css:927-930` uses
   `.esri-popup { display: none !important; }`. Should be
   `popupEnabled: false` on the `MapView` constructor (or `view.popup
   = null` after init). The CSS approach works but leaves the popup
   instance alive, fires events on click, and only suppresses paint —
   a subclass that listens to `popup.visibleChanged` would still see
   activity. Severity is low in *this* app (no such listeners exist) —
   bumping to medium because it's a generic foot-gun and trivial to fix.
   *Fix approach:* delete the CSS rule and pass `popupEnabled: false`
   in the `MapView` config near `static/map.js:524-530`.

### Low

9. **`view_spot.py` imports a symbol it never uses.** `view_spot.py:24` does
   `from src.FishFinderTools import zeroToOneFifty`, but only `color_relief`
   (imported on the next line) is actually called (`view_spot.py:108`).
   The unused import keeps the broken `FishFinderTools.py` alive and is
   the only thing preventing its deletion.
   *Fix approach:* delete L24. Then `FishFinderTools.py` has no importers
   and can also go.

10. **Dead assets in `static/images/`.** `bay_area_heatmap.png`,
    `sam_engel_headshot.jpeg`, `sam_virsik_headshot.jpeg` — none are
    referenced from any HTML/CSS/JS. Repo grep finds zero hits. Three
    files, ≈ a few hundred KB.
    *Fix approach:* delete the files (and probably the empty directory).

11. **`img/blank.png` and `img/_blank.png` are unreferenced.** No code path
    uses either. `CLAUDE.md` calls `blank.png` out as legacy. `_blank.png`
    is gitignored (per `.gitignore:15`) but exists on disk.
    *Fix approach:* delete both. Re-confirm by grep first.

12. **`requirements.txt` includes `pandas` (and `matplotlib`) that the
    runtime app no longer needs.** `pandas` is only imported by
    `src/FishFinderTools.py` (dead). `matplotlib` is imported by
    `src/analyses.py` (used by `view_spot.py`) and `view_spot.py` itself —
    that's offline preview only. The server (`app.py` →
    `src/LayerGeneration.py`) and the browser don't need either.
    *Fix approach:* if you keep `view_spot.py` as a developer-only tool,
    leave `matplotlib`; either way, drop `pandas` from the file once
    `FishFinderTools.py` is gone. Bonus: `requirements.txt` could split
    into runtime + tooling extras.

13. **Prefetch can request `z=23` which the server rejects.** `runPrefetch`
    in `map.js:893-917` builds the `"z+1"` tier under `zoom + 1 <= 23`,
    so when the user is at zoom 22 the prefetch issues z=23 requests
    that `fetch_tile_raster` then drops with a `None` → 503. The worker
    flags those as `'error'` so they don't poison the cache, but it's
    wasted work and a console-warn each. Severity is genuinely low —
    z=22 in the Florida Keys is street-level zoom — but easy fix.
    *Fix approach:* change the constant on `map.js:879` from `23` to `22`,
    matching the server's `z > 22` cutoff in `LayerGeneration.py:286`.

14. **`static/main.js` is a placeholder.** Single comment line. It's
    loaded by `templates/index.html:33` for every page. The browser
    fetches 40 bytes for a no-op script tag. Harmless but worth
    deleting if the placeholder isn't going to grow into something.

15. **`src/__init__.py` is a single empty line.** Empty `__init__.py`
    files are fine in modern Python, but worth noting. Leave it.

16. **`README.md` is two characters (`# FishFinder` + blank).** A user
    landing on the repo gets no quickstart, no link to the live demo,
    no run instructions. The CLI quickstart in `CLAUDE.md` (`pip install
    -r requirements.txt; python app.py`) would be a perfectly good
    starter README.

## What's half-built

- **`view_spot.py`** is described as a developer preview tool, but the
  hard-coded coordinates (`view_spot.py:35-36`) and `PIXEL_WIDTH` (`:46`)
  imply it's meant to be edited inline. There's no CLI flag, no env, no
  config file. Functional but not really "finished" in a usable-by-others
  sense.
- **`static/main.js`** — the comment `// Reserved for future global UI
  logic.` strongly suggests "I intended to put X here later." Either fill
  it or remove it.

## Suggested next steps (prioritized)

| # | Change | Effort | Why |
|---|---|---|---|
| 1 | Fix Reload-button title + hint copy in `templates/map.html` | 5 min | User-facing lie. Highest "wrong/effort" ratio. |
| 2 | Delete `src/FishFinderTools.py`, drop unused import from `view_spot.py`, drop `pandas` from `requirements.txt` | 15 min | Eliminates 308 lines of broken dead code and one runtime dep. |
| 3 | Delete unreferenced assets (`static/images/*`, `img/blank.png`, `img/_blank.png`) | 5 min | Tidy. |
| 4 | Patch `CLAUDE.md`: remove `/depth`, add `/heartbeat`, remove `blank.png` mention | 5 min | Doc lies are subtle bug-multipliers. |
| 5 | Remove stale `.gitignore` entries (`flask_session/`, `img/tile/`, `img/noaa/`) | 2 min | Cosmetic. |
| 6 | Convert popup-hiding from CSS to `popupEnabled: false` | 5 min | Cleaner / future-proof. |
| 7 | Tighten the heartbeat watchdog (require 2 pings before arming, or bump timeout to 10 s) | 15 min | Makes the server usable from non-browser clients. |
| 8 | Fix unknown-source disk-cache leak (`_source` → 4xx on miss) | 15 min | Removes a silent foot-gun. |
| 9 | Cap prefetch at `z ≤ 22` to match server | 2 min | Eliminate guaranteed-503 prefetch requests. |
| 10 | Promote `CLAUDE.md` quickstart into `README.md` | 10 min | Open-repo first impression. |
| 11 | Optional: surface SSL/cert errors with a one-time hint in `LayerGeneration` | 30 min | Saves a fresh dev's first hour. |

Cumulative effort if you take all of them: under two hours. The first six
are mechanical and could land in a single PR with no behavioral risk.

## Open questions

1. **Reload button intent.** Now that the implementation no longer wipes
   caches, do you want to (a) just relabel it ("Re-render visible tiles"),
   or (b) add a separate "Hard reset" path that *does* wipe `canvasCache`
   and `rasterCache` for users who genuinely need the escape hatch? The
   current code's defense for not wiping them is that the supersession
   pipeline already handles the cases that motivated wiping — but if
   there's a remaining "GPU got into a weird state" scenario, a hard-
   reset that re-spawns the worker via `resetWorker()` *might* still be
   wanted as a button rather than only an internal recovery.
2. **`view_spot.py` future.** Is this still actively used? If not, the
   simplest tidy-up of `src/analyses.py` would also drop `matplotlib`
   (the JS side has a polynomial viridis approximation already, so the
   only consumer of `plt.get_cmap('viridis')` is `view_spot`).
3. **Unknown bathymetry source — should it be 400 or a fallback?** Today
   it silently falls back to `dem-tiles` (and writes a confusingly-named
   cache dir). Preference: hard-fail at 400 ("unknown source"), or
   continue to fall back but log a warning and cache under the resolved
   name? Either is correct; current behavior is the worst of both.
4. **Heartbeat-driven auto-shutdown.** Do you still want this on by
   default, or hide it behind `FISHFINDER_AUTOSHUTDOWN=1`? The current
   default is great for normal "close tab → server gone" UX but actively
   hostile to integration testing / curl probing / running behind a
   process supervisor.
5. **README.** Want me to write a real one as part of follow-up, or are
   you keeping the repo intentionally bare?
