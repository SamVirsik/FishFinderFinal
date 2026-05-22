# FishFinder Rework Plan

Two-part rework, scoped to:

1. Make the Flask server behave like a standard long-running app by default; auto-shutdown becomes opt-in.
2. Consolidate every fact about external bathymetry sources into a single registry file; downstream code (Python + browser) reads from it.

Out of scope (per the brief): wire format, worker/renderer, analysis registry, and the dead-code cleanup from `AUDIT_REPORT.md`.

---

## Pre-flight: what matches the brief, what doesn't

I read `app.py`, `src/LayerGeneration.py`, the heartbeat / prefetch sections of `static/map.js`, `templates/map.html`, and re-read `AUDIT_REPORT.md`. Everything in the brief lines up with the current code, with two minor adjustments worth flagging before I touch anything:

- The brief says the heartbeat lives at `app.py:120-133`. Correct — `_heartbeat_watcher` is exactly there, and `HEARTBEAT_TIMEOUT_S = 3.0` at L43.
- The brief says the client heartbeat is "around L80-95" of `static/map.js`. It's at L79-95 (`_pingHeartbeat`, sendBeacon-based, fires once at load and then every 1 s). I will NOT touch the client — the endpoint contract is unchanged (POST → 204).
- The brief calls out prefetch finding #13 (z=23 cap). The current cap is hardcoded at `static/map.js:879` (`zoom + 1 <= 23`) and the server cap is `z > 22` at `LayerGeneration.py:286`. Part 2's per-source max-zoom data naturally subsumes this if the client respects it during prefetch — I'll do that.
- `_SOURCE_SPEC` is at `LayerGeneration.py:115-154`, `_source` at L159-160, `_fetch_raster_bytes` at L167-197, `fetch_tile_raster` at L278-301 (the brief said L294 — that's where the cache_dir leak lives). All aligned with the audit's finding #7.

No conflicts. Proceeding.

---

## Part 1 — Server lifecycle

### Env var vs CLI flag

**Choice: env var (`FISHFINDER_AUTOSHUTDOWN=1`).** One-line rationale (will go into a code comment): an env var travels through whatever launcher the user happens to be using (PowerShell `$env:`, `python app.py` directly, IDE run config, Docker `-e`) without requiring `app.py` to grow an `argparse` block for a single flag. The current `if __name__ == '__main__'` block doesn't parse args today, so introducing argparse for one flag is more surface area than a `os.environ.get` check. If we ever grow real CLI args we can wrap both.

### Behavior matrix

| Mode | Trigger | `/heartbeat` endpoint | Watchdog thread | Kill condition |
|---|---|---|---|---|
| Default (long-running) | nothing | 204, no-op | not started | never |
| Auto-shutdown | `FISHFINDER_AUTOSHUTDOWN=1` | 204, records ping count + timestamp | started | ≥2 pings received AND >10 s since last |

The two-ping minimum is what the brief asks for. It prevents a single curl probe from arming the watchdog. The 10 s timeout (bumped from 3 s) gives a real browser plenty of headroom on a slow tab restore.

### Code changes (Part 1)

- `app.py`
  - Read `FISHFINDER_AUTOSHUTDOWN` once at module import; store as `_AUTOSHUTDOWN`.
  - Bump `HEARTBEAT_TIMEOUT_S` to `10.0`. Add `HEARTBEAT_MIN_PINGS = 2`.
  - `heartbeat()` always returns 204; only updates the counter/timestamp when `_AUTOSHUTDOWN` is on. (No reason to touch shared state when it's never read.)
  - `_heartbeat_watcher()` checks `_heartbeat_count >= HEARTBEAT_MIN_PINGS` before the timeout comparison.
  - In `__main__`, only spawn the watcher when `_AUTOSHUTDOWN` is true. Print a one-line notice in that mode (`"auto-shutdown armed: 10s idle after 2 pings"`) so it's obvious what mode the process is running in.

- `static/map.js`: no change. Still pings every 1 s; the server simply ignores pings in default mode.

- `CLAUDE.md`: no change in this PR. (Touching docs is out of scope per the brief's "what NOT to change". I'll flag in the summary if you want a follow-up to refresh the doc.)

---

## Part 2 — Data-source registry

### File path: `src/data_sources.py`

One flat module at the top of `src/`. A `sources/` subpackage would imply multiple files; this is one registry, one file. Sibling to `LayerGeneration.py` which consumes it.

### Data structure: dataclasses

```python
@dataclass(frozen=True)
class DataSource:
    id: str                       # URL token, e.g. "dem-tiles"
    display_name: str             # dropdown label
    url: str                      # NOAA ImageServer endpoint
    pixel_type: str = "F32"
    image_sr: int = 3857
    nodata: float | None = None   # source-specific sentinel
    rendering_rule: str | None = None
    extra_params: dict = field(default_factory=dict)
    min_zoom: int = 0
    max_zoom: int = 22            # server-level XYZ cap
    cache_dir_name: str | None = None  # defaults to `id`; decoupled so we can rename `id` without invalidating disk cache
    enabled: bool = True
    hidden: bool = False          # show in dropdown?
    experimental: bool = False    # tag in UI
    timeout_s: float = 30.0
    notes: str = ""               # free-form, surfaced in dropdown tooltip if non-empty

SOURCES: tuple[DataSource, ...] = (
    DataSource(id="dem-tiles", display_name="Default DEM Mosaic — broad coverage",
               url="https://gis.ngdc.noaa.gov/.../DEM_tiles_mosaic/ImageServer/exportImage",
               nodata=-9999),
    ...
)

DEFAULT_SOURCE_ID = "dem-tiles"

def get_source(source_id: str) -> DataSource | None: ...
def all_visible_sources() -> list[DataSource]: ...   # for dropdown
def to_client_dict(s: DataSource) -> dict: ...        # what /sources serializes
```

Frozen dataclasses because: type-checked field names, immutable (no risk of mutating a registry entry by accident), trivially serializable for the `/sources` endpoint. One source per entry — readable, diffable, easy to add to.

`cache_dir_name` decouples the on-disk path from the URL token. That's the audit's finding #7 fix: the cache directory is always derived from the resolved registry entry, not from the URL string the client sent. If the client sends `?source=typo`, `get_source` returns `None` and the request 400s — no cache leak possible.

### Downstream wiring

- **`src/LayerGeneration.py`**
  - Delete `_SOURCE_SPEC`, `DEFAULT_SOURCE`, `_source`.
  - `_fetch_raster_bytes(source_obj, bbox, size)` takes the resolved `DataSource` and builds NOAA params from its fields. (Less stringly-typed than today.)
  - `_decode_raster(raw, expected, source_obj)` uses `source_obj.nodata` directly.
  - `fetch_tile_raster(source_id, ...)` calls `get_source(source_id)`. If `None`, return a new sentinel (e.g. `('unknown_source', source_id)` or raise a small custom exception) so the Flask layer can map it to 400 instead of 503.
  - cache_dir uses `source.cache_dir_name`, never `source_id`.
  - Use `source.max_zoom` for the bounds check instead of the hardcoded `z > 22`. (Default 22 keeps current behavior for sources where I don't set it explicitly.)

- **`app.py`**
  - `/raster/...` handler distinguishes "unknown source" (400) from "transient fetch failure" (503). The wire-format response is unchanged.
  - New `GET /sources` → JSON: list of `{id, display_name, default, max_zoom, min_zoom, experimental, notes}` for every visible enabled source. Default source flagged.

- **`templates/map.html`** (dropdown population)
  - **Choice: JSON endpoint (`/sources`), populated on page load.** Reason: the dropdown lives inside a `{% for layer in map_layers %}` block, which already complicates server-side iteration. More importantly, the client needs the per-source `max_zoom` for prefetch capping anyway, so we're already shipping JSON. One transport, one source of truth on the client side. Server-render of the `<option>` list would force me to push the same data through Jinja AND a separate context endpoint.
  - The hardcoded `<option>` list at L106-113 is replaced with a single empty `<select>` that the client fills on load. While that fetch is in flight, the select is disabled — render takes ~10 ms over loopback, so no visible flash.

- **`static/map.js`**
  - On boot, `fetch('/sources')` → populate dropdowns → enable controls. Stash the source list as `sourcesById` keyed by `id`.
  - Prefetch uses `sourcesById[cfg.source].max_zoom` instead of the hardcoded `23`. Naturally fixes audit finding #13. Same for ring/parent tiers (clamp to `[min_zoom, max_zoom]`).
  - No other client changes — apply pipeline, layer-swap, worker protocol, wire format all untouched.

### Migration of existing entries

The seven current sources move into `SOURCES` with their existing identifiers as both `id` and `cache_dir_name` (so on-disk caches stay valid). NOAA URLs, nodata sentinels, and rendering rules port one-for-one from `_SOURCE_SPEC`.

I'll set per-source `max_zoom` conservatively: 22 for everything for now. (Different NOAA mosaics actually peak at different native resolutions, but I don't want to introduce zoom-cap regressions in this PR — better to land the registry first, then tune per-source caps in a follow-up if you want.)

---

## File-by-file diff scope

| File | Change |
|---|---|
| `app.py` | Auto-shutdown gated on env var; tighter watchdog; new `/sources` endpoint; 400 vs 503 for unknown source. |
| `src/LayerGeneration.py` | Drop `_SOURCE_SPEC`; consume `DataSource`; cache_dir from registry; per-source max_zoom. |
| `src/data_sources.py` | **New file.** Registry + accessors. |
| `templates/map.html` | Replace hardcoded `<option>` list with empty `<select>`; populated on load. |
| `static/map.js` | Boot-time `/sources` fetch; `sourcesById`; prefetch uses per-source `max_zoom`. |

Nothing else.

---

## Open questions for you

1. **Auto-shutdown notice in default mode.** Should default-mode `python app.py` print a one-liner reminding the user that auto-shutdown is opt-in (`"auto-shutdown disabled (set FISHFINDER_AUTOSHUTDOWN=1 to enable)"`)? I lean **no** — the current banner is one line ("FishFinder running at …") and I'd rather keep it that way. The auto-shutdown mode banner is enough.

2. **Per-source `max_zoom` calibration.** I'm defaulting everything to 22 to avoid behavioral regressions. Do you want me to set tighter caps based on what each NOAA mosaic actually supports (`fknms-multibeam` is genuinely the only one that delivers detail past z=18; `dem-global` saturates much earlier), or leave that as a follow-up?

3. **Unknown-source response code.** Brief says 400. Confirming you want a JSON error body (`{"error": "unknown source: foo"}`) vs an empty 400. I'll do empty 400 to match the existing terse `('', 503)` style unless you say otherwise.

4. **`/sources` cache headers.** Treat it as ~static (cache 1 hour) or no-cache (always fresh from registry)? Lean **no-cache** during the rework so registry edits take effect on next reload without the user wondering why the dropdown is stale.

---

## What I'll do after you approve

1. Implement Part 1 (smaller, lower risk — lands first).
2. Implement Part 2 (registry, then `LayerGeneration` rewire, then `app.py` endpoint, then HTML/JS).
3. Smoke-test: start the server, hit `/sources`, load the page, confirm dropdown populates, confirm a tile fetch still returns the binary blob, confirm `/raster/typo-source/...` returns 400, confirm default-mode server stays alive after a curl probe.
4. Write the change summary you asked for.
