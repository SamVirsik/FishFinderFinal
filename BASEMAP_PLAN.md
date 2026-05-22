# Basemap registry — implementation plan

Plan-only. No code is being written until this is approved.

---

## 0. Decisions to confirm before I start coding

A. **Initial basemap on cold load.** The brief says "Initialize the map
   immediately with a hardcoded Esri default basemap (`'satellite'`)."
   Today the hardcoded default is `"dark-gray-vector"` (Dark is also the
   button marked `active` in `templates/map.html`). Following the brief
   literally changes the cold-load default from Dark → Satellite. I will
   do that — please confirm; if it should stay Dark, swap the two literals
   in §6 and §2.

B. **Default-basemap selection in the registry.** The bathymetry registry
   exposes a `default` field at the response top level. The brief does
   *not* ask for one on `/basemaps`. I'll omit it: the client picks its
   initial basemap from its own hardcoded literal (per §A) and the grid
   just renders whatever the server returns, in order. Speak up if you
   want a server-driven `default` field too.

C. **Position of new entries in the grid.** I'll append the three new
   buttons after the six Esri ones, in the order MapTiler → USGS →
   Sentinel-2. Tell me if you want them interleaved (e.g. all
   satellite-like buttons grouped).

---

## 1. File-by-file diff sketch

| File                       | Change                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `src/basemap_sources.py`   | **New.** Frozen `@dataclass BasemapSource`, module-level `BASEMAPS` tuple with nine entries (six Esri + USGS + EOX + MapTiler), `list_basemaps()` function that filters MapTiler when `MAPTILER_API_KEY` is unset and substitutes the env var into its `value`. Mirrors the shape of `src/data_sources.py` (frozen dataclass, module-level tuple, `to_client_dict`-style serialiser).                          |
| `app.py`                   | Add `from src.basemap_sources import list_basemaps`. Add a `GET /basemaps` route that returns `{"basemaps": list_basemaps()}` with `Cache-Control: no-store` (same policy as `/sources` — the registry is small and we want hard reloads to pick up edits).                                                                                                                                                   |
| `templates/map.html`       | Replace the six hardcoded `<button>` elements inside `#basemap-grid` with an empty container. The surrounding `<label>` and `.field-hint` stay; only the grid contents are removed.                                                                                                                                                                                                                          |
| `static/map.js`            | (a) Kick off `fetch("/basemaps")` at module load, parallel with `_sourcesReady`. (b) Add `"esri/Basemap"`, `"esri/layers/WebTileLayer"`, `"esri/layers/TileLayer"` to the `require([...])` list. (c) Keep `new EsriMap({ basemap: "satellite", ... })` synchronous (per §0.A). (d) Replace the basemap-click handler with a `setBasemap(entry)` factory and a renderer that builds the grid from the fetched registry (or a hardcoded six-Esri fallback on failure). |
| `README.md`                | Add a short "Basemap providers" section: how to set `MAPTILER_API_KEY`; that the MapTiler key ships to the browser and should be domain-restricted in MapTiler Cloud; free-tier limit (100K tile requests + 5K sessions/month, whichever first); Sentinel-2 Cloudless 2024 is CC BY-NC-SA, non-commercial only.                                                                                               |

Not touched: `src/data_sources.py`, `src/LayerGeneration.py`, the worker, the `/raster` wire format, the bathymetry pipeline, any of the pending audit items.

---

## 2. Exact `@dataclass` definition (src/basemap_sources.py)

```python
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BasemapSource:
    """One basemap entry surfaced in the Basemap button grid.

    Three provider flavours are supported:
      - "esri":        `value` is a built-in Esri basemap ID string
                       (e.g. "satellite"). Client assigns it directly to
                       `map.basemap` — Esri handles attribution.
      - "xyz":         `value` is an XYZ tile URL template using ArcGIS
                       WebTileLayer tokens ({level}/{col}/{row}). Server
                       substitutes any {ENV_VAR} placeholders before
                       returning to the client. Client wraps it in a
                       WebTileLayer inside a Basemap.
      - "arcgis_rest": `value` is an ArcGIS REST MapServer / ImageServer
                       service URL. Client wraps it in a TileLayer; the
                       service supplies its own tile info (size, max zoom).
    """
    id: str
    display_name: str
    provider: str                              # "esri" | "xyz" | "arcgis_rest"
    value: str
    attribution: str = ""                      # "" is correct for Esri entries
    max_zoom: Optional[int] = None             # None → use provider default
    tile_size: Optional[int] = None            # None → use provider default
    tooltip: Optional[str] = None              # used as button title attr
    env_required: Optional[str] = None         # if set, entry is dropped when env unset
    env_substitute_token: Optional[str] = None # placeholder to replace with env value
```

The registry tuple has nine entries. Sketched (only the new ones shown in
full; Esri entries are one-liners):

```python
BASEMAPS: tuple = (
    # -- Esri built-ins (unchanged behavior) -----------------------------
    BasemapSource("dark-gray-vector", "Dark",        "esri", "dark-gray-vector"),
    BasemapSource("streets-vector",   "Streets",     "esri", "streets-vector"),
    BasemapSource("satellite",        "Satellite",   "esri", "satellite"),
    BasemapSource("hybrid",           "Hybrid",      "esri", "hybrid"),
    BasemapSource("oceans",           "Oceans",      "esri", "oceans"),
    BasemapSource("topo-vector",      "Topographic", "esri", "topo-vector"),

    # -- MapTiler Satellite (gated on MAPTILER_API_KEY) ------------------
    BasemapSource(
        id="maptiler_satellite",
        display_name="MapTiler Sat",
        provider="xyz",
        value=("https://api.maptiler.com/tiles/satellite-v2/"
               "{level}/{col}/{row}.jpg?key={MAPTILER_API_KEY}"),
        attribution="© MapTiler © OpenStreetMap contributors",
        max_zoom=20,
        tile_size=512,
        env_required="MAPTILER_API_KEY",
        env_substitute_token="{MAPTILER_API_KEY}",
    ),

    # -- USGS Aerial (NAIP) ----------------------------------------------
    BasemapSource(
        id="usgs_aerial",
        display_name="USGS Aerial",
        provider="arcgis_rest",
        value=("https://basemap.nationalmap.gov/arcgis/rest/"
               "services/USGSImageryOnly/MapServer"),
        attribution="USDA-FSA-NAIP, USGS, The National Map",
        tooltip="High-res aerial; open water shows as blank.",
        # max_zoom / tile_size left None — service supplies its own tile info
    ),

    # -- EOX Sentinel-2 Cloudless 2024 (CC BY-NC-SA) ---------------------
    BasemapSource(
        id="eox_s2_cloudless_2024",
        display_name="Sentinel-2",
        provider="xyz",
        # NOTE the {level}/{row}/{col} order — EOX puts row before col.
        value=("https://tiles.maps.eox.at/wmts/1.0.0/"
               "s2cloudless-2024_3857/default/g/{level}/{row}/{col}.jpg"),
        attribution=("Sentinel-2 cloudless - https://s2maps.eu by EOX IT "
                     "Services GmbH (Contains modified Copernicus Sentinel "
                     "data 2024)"),
        max_zoom=14,
    ),
)
```

`list_basemaps()` walks the tuple, drops entries with an unset
`env_required` env var, performs the `env_substitute_token` replacement on
the matching entry, and returns a `list[dict]` shaped per §3 (no
server-only fields like `env_required` / `env_substitute_token` leak to
the wire).

---

## 3. Exact `/basemaps` JSON shape

`Content-Type: application/json`, `Cache-Control: no-store`, HTTP 200.

```json
{
  "basemaps": [
    {
      "id": "dark-gray-vector",
      "display_name": "Dark",
      "provider": "esri",
      "value": "dark-gray-vector",
      "attribution": "",
      "max_zoom": null,
      "tile_size": null,
      "tooltip": null
    },
    {
      "id": "maptiler_satellite",
      "display_name": "MapTiler Sat",
      "provider": "xyz",
      "value": "https://api.maptiler.com/tiles/satellite-v2/{level}/{col}/{row}.jpg?key=REAL_KEY_SUBSTITUTED_HERE",
      "attribution": "© MapTiler © OpenStreetMap contributors",
      "max_zoom": 20,
      "tile_size": 512,
      "tooltip": null
    },
    {
      "id": "usgs_aerial",
      "display_name": "USGS Aerial",
      "provider": "arcgis_rest",
      "value": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer",
      "attribution": "USDA-FSA-NAIP, USGS, The National Map",
      "max_zoom": null,
      "tile_size": null,
      "tooltip": "High-res aerial; open water shows as blank."
    }
  ]
}
```

Notes:
- Order in the response is the order rendered in the grid.
- `max_zoom` and `tile_size` are `null` when the client should defer to
  the provider's default (Esri's basemap definition, or the ArcGIS REST
  service's published tile info).
- The MapTiler entry only appears when `MAPTILER_API_KEY` is set.

---

## 4. `setBasemap(entry)` factory — pseudocode

`Basemap`, `WebTileLayer`, `TileLayer` are added to the existing
`require([...])` so they're already loaded by the time anyone clicks a
button. No dynamic AMD loading.

```js
// One-time class refs captured from the require() callback args.
// `Basemap`, `WebTileLayer`, `TileLayer` available in this scope.

function setBasemap(entry) {
    if (entry.provider === "esri") {
        // Existing behavior — Esri owns the layer set + attribution.
        map.basemap = entry.value;
        return;
    }

    if (entry.provider === "xyz") {
        const layerOpts = {
            urlTemplate: entry.value,
            copyright:   entry.attribution,
        };
        if (entry.tile_size != null) {
            // 512 vs the WebTileLayer default of 256. Build a TileInfo
            // with the right tile size so ArcGIS doesn't fetch double the
            // rows/cols it should.
            layerOpts.tileInfo = TileInfo.create({
                spatialReference: SpatialReference.WebMercator,
                size: entry.tile_size,
            });
        }
        const layer = new WebTileLayer(layerOpts);
        if (entry.max_zoom != null) layer.maxZoom = entry.max_zoom;
        map.basemap = new Basemap({ baseLayers: [layer] });
        return;
    }

    if (entry.provider === "arcgis_rest") {
        // TileLayer reads tile size + max zoom from the service description,
        // so we deliberately do NOT clobber them from the registry.
        const layer = new TileLayer({
            url:       entry.value,
            copyright: entry.attribution,
        });
        map.basemap = new Basemap({ baseLayers: [layer] });
        return;
    }

    console.warn("[basemap] unknown provider:", entry.provider, entry);
}
```

How copyright is wired: every non-Esri layer sets `copyright: entry.attribution`. ArcGIS's built-in attribution control (already enabled via `ui: { components: ["zoom", "attribution"] }` at view construction) reads this and displays it when the layer is active. Esri entries leave `attribution: ""` because Esri's basemap registry supplies its own copyright text.

The grid renderer builds buttons from the registry and wires clicks:

```js
function renderBasemapGrid(entries) {
    $basemapGrid.innerHTML = "";
    for (const entry of entries) {
        const btn = document.createElement("button");
        btn.className = "basemap-btn";
        btn.dataset.basemapId = entry.id;
        btn.textContent = entry.display_name;
        if (entry.tooltip) btn.title = entry.tooltip;
        if (entry.id === currentBasemapId) btn.classList.add("active");
        $basemapGrid.appendChild(btn);
    }
}

$basemapGrid.addEventListener("click", (e) => {
    const btn = e.target.closest(".basemap-btn[data-basemap-id]");
    if (!btn) return;
    const id = btn.dataset.basemapId;
    if (id === currentBasemapId) return;
    const entry = basemapsById[id];
    if (!entry) return;
    setBasemap(entry);
    currentBasemapId = id;
    $basemapGrid.querySelectorAll(".basemap-btn")
        .forEach(b => b.classList.toggle("active", b === btn));
});
```

---

## 5. Behavior when `MAPTILER_API_KEY` is unset

End-to-end:

1. `list_basemaps()` (server) walks the registry. The MapTiler entry has
   `env_required="MAPTILER_API_KEY"`. `os.environ.get("MAPTILER_API_KEY")`
   returns `None` (or empty string). The entry is dropped from the
   returned list. No log line, no error — this is normal.
2. The `/basemaps` response contains **8** entries: 6 Esri + USGS + EOX.
   Schema-wise indistinguishable from the 9-entry case; the client doesn't
   need to special-case anything.
3. The client renders 8 buttons. The MapTiler button literally does not
   exist in the DOM, so no broken-key tile request, no console warning
   referencing it, no dead button to confuse the user.
4. If the user later sets the env var and reloads, the server now returns
   9 entries and the MapTiler button appears.

Failure modes intentionally avoided:
- We do NOT emit a MapTiler entry with the placeholder unsubstituted.
- We do NOT emit a MapTiler entry with the key visibly redacted.
- We do NOT render a disabled MapTiler button.

---

## 6. Boot sequence (numbered)

Constraint from the prior regression in `static/map.js`: the AMD `require`
callback CANNOT be async, and map construction MUST NOT be chained off
the registry fetch. Order:

1. **Module load (top of `static/map.js`, outside any require callback).**
   Kick off two fetches in parallel:
   - `_sourcesReady = fetch("/sources", ...)` (existing — untouched).
   - `_basemapsReady = fetch("/basemaps", ...)` (new). On non-OK / network
     error / parse error, `.catch()` resolves to a hardcoded fallback
     `{ basemaps: [<six Esri entries>] }` and `console.warn`s.
3. **AMD load.** `require([...])` (extended to also import `Basemap`,
   `WebTileLayer`, `TileLayer`) runs in parallel with both fetches.
4. **`require` callback fires (synchronous, NOT async).** It constructs
   the `Map` and `MapView` immediately, with a hardcoded literal default
   basemap: `new EsriMap({ basemap: "satellite", ... })`. See §0.A — this
   is the only point in the boot sequence where the initial basemap is
   pinned. At this instant the user already sees the basemap rendering;
   nothing about the basemap grid blocks first paint.
5. **`_basemapsReady.then(...)`.** When the registry arrives (or the
   fallback resolves), the client builds `basemapsById`, calls
   `renderBasemapGrid(entries)`, and marks the entry whose `id` matches
   the current basemap (`"satellite"`) as `active`. Until this resolves,
   the grid is empty — the map itself is fully usable.
6. **Subsequent clicks.** Handled by the grid click listener, which calls
   `setBasemap(entry)` (§4). This path NEVER awaits the network for
   anything other than the basemap tiles themselves.

Failure paths:
- `/basemaps` 404 / 500 / hang / CORS → fallback list of six Esri entries.
  Map is still alive (it was built on step 4), all six fallback buttons
  work via the `"esri"` branch of `setBasemap`.
- The Flask server being down at page load is the realistic scenario for
  the verification checklist's "stop Flask, reload" item — same
  resolution.

---

## 7. Verification I'll run after the code lands

Same as the brief's checklist verbatim. Two specifics worth pre-flagging:

- I'll inspect a live MapTiler request in DevTools Network to confirm the
  substituted key is present (not the literal `{MAPTILER_API_KEY}`) and
  that the `{level}/{col}/{row}` order produces a 200, not a 404.
- I'll inspect an EOX request to confirm the `{level}/{row}/{col}`
  (row-before-col) order — this is the specific gotcha the brief flagged
  and the easiest one to silently get wrong.

---

Awaiting review. Will not write any code until you give the go-ahead, and
will sanity-check decisions §0.A / §0.B / §0.C against your reply.
