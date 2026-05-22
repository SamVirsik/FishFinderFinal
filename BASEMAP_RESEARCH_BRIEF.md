# FishFinder — Basemap Source Research Brief

You are being asked to research candidate **free satellite/aerial basemap
providers** that can be added to a small open-source web mapping app called
**FishFinder**. Your output will be handed back to a Claude Code agent for
implementation, so concrete URLs, attribution strings, and key-acquisition
instructions matter more than narrative.

---

## 1. Context

FishFinder is a Flask + ArcGIS JS API web app for visualising NOAA
bathymetric (sea-floor depth) data, primarily over the **Florida Keys** and
US coastal waters, used for fishing and small-boat navigation. Depth data
is rendered as a semi-transparent overlay on top of a basemap. The basemap
provides the visual context — coastline, land features, and satellite
imagery of the surrounding terrain and water — beneath the bathymetry.

**The problem:** the current Esri "Satellite" basemap is noticeably lower
resolution than Google Maps' satellite imagery in many areas, especially
**over water and along remote coastlines** — exactly the contexts where
FishFinder users spend their time. The user wants to add 2–4 additional
free satellite/aerial basemap options so they can pick the sharpest imagery
for their region.

---

## 2. Current state

### 2.1 Basemaps wired up today

Six Esri basemaps, all invoked by string ID via the ArcGIS JS API's built-in
basemap registry:

| Button label | ID                  |
| ------------ | ------------------- |
| Dark         | `dark-gray-vector`  |
| Streets      | `streets-vector`    |
| Satellite    | `satellite`         |
| Hybrid       | `hybrid`            |
| Oceans       | `oceans`            |
| Topographic  | `topo-vector`       |

Defined in `templates/map.html`:

```html
<div class="basemap-grid" id="basemap-grid">
    <button class="basemap-btn active" data-basemap="dark-gray-vector">Dark</button>
    <button class="basemap-btn" data-basemap="streets-vector">Streets</button>
    <button class="basemap-btn" data-basemap="satellite">Satellite</button>
    <button class="basemap-btn" data-basemap="hybrid">Hybrid</button>
    <button class="basemap-btn" data-basemap="oceans">Oceans</button>
    <button class="basemap-btn" data-basemap="topo-vector">Topographic</button>
</div>
```

### 2.2 How they're invoked

ArcGIS JS API 4.26. The map is created with a basemap-ID string, and the
switcher swaps it by string assignment. From `static/map.js`:

```js
// map construction
const map = new EsriMap({
    basemap: "dark-gray-vector",
    layers: [markerLayer, measureLayer],
});

// basemap switch handler
$basemapGrid.addEventListener("click", (e) => {
    const btn = e.target.closest(".basemap-btn[data-basemap]");
    if (!btn) return;
    const id = btn.dataset.basemap;
    if (!id || map.basemap === id) return;
    map.basemap = id;
    $basemapGrid.querySelectorAll(".basemap-btn")
        .forEach(b => b.classList.toggle("active", b === btn));
});
```

So today the basemap value is **always an Esri built-in ID**. To add
non-Esri sources, the switcher needs to support an object form: either a
full `Basemap` instance composed of one or more `WebTileLayer` /
`VectorTileLayer` / `TileLayer` instances (custom XYZ providers), or a
`Basemap.fromId(...)`-style helper. The implementer will likely build a
small map of `id → Basemap factory` and have the click handler invoke the
factory rather than assigning a string.

### 2.3 Where basemap tiles flow

Basemap tiles go **direct from the user's browser to the provider's CDN**.
The Flask backend (`app.py`, `src/LayerGeneration.py`) only proxies the
**bathymetry raster** tiles (`/raster/<source>/<res>/{z}/{x}/{y}.bin`). It
does **not** proxy basemap tiles and there is no infrastructure to do so.

Implication: any candidate provider must permit browser-side, client-direct
tile fetches under its terms (most XYZ tile providers do; some — notably
Google and Mapbox — restrict this to a specific SDK or require domain
allowlisting on an API key).

### 2.4 Pattern to mirror for the registry

Bathymetry data sources were recently consolidated into a single
authoritative registry (`src/data_sources.py`) using a frozen `@dataclass`
with fields like `id`, `display_name`, `url`, `min_zoom`, `max_zoom`,
`notes`. The browser fetches `/sources` to populate its dropdown. The user
would like the basemap solution to follow a similar shape **where
practical** — a single source-of-truth list of basemap entries that the UI
reads from — though basemap entries differ because tiles go direct to the
provider, so the structure will need fields like `tile_url_template`,
`attribution`, `requires_api_key`, `max_zoom`, `tile_size`, etc. You do
**not** need to design the registry; just be aware that the implementer
will be constructing one.

---

## 3. Requirements for new sources

### Hard requirements (must-have)

1. **Free** for personal/small-project use of a fishing tool. If a provider
   offers a "free tier" with a monthly request cap (e.g. MapTiler's
   100K/month), call out the exact limit.
2. **Accessible via a standard tile API** — XYZ tiles
   (`/{z}/{x}/{y}.png|jpg`), WMTS, or similar — that works with the ArcGIS
   JS API's `WebTileLayer` (or `TileLayer` for an ArcGIS REST service)
   without writing a custom protocol or running tiles through a proxy.
3. **No API key required, OR a free key that's easy to obtain** (email
   signup, dashboard click-through — not enterprise sales).
4. **Terms permit use in a public-facing web mapping app** for a small
   non-commercial fishing tool. Anything ambiguous on this, flag it.
5. **Coverage of US coastlines**, especially the **Florida Keys, Gulf of
   Mexico, US Atlantic, and US Pacific** coasts. Global is fine; coverage
   gaps over US waters disqualify a source for this use case.
6. **EPSG:3857 / Web Mercator** projection (matches the rest of the app).

### Soft preferences (rank candidates on these)

- **Higher effective resolution than Esri Satellite over water and remote
  coastlines** — this is the entire point.
- **Recent imagery** (≤ ~5 years preferred). Sentinel-2 is acceptable but
  call out its native ~10 m/pixel limit honestly.
- **Responsive tile servers without aggressive rate-limiting** for typical
  pan/zoom usage.
- **Imagery that visually differentiates** — e.g. a true-color aerial
  source and a Sentinel-2 cloudless mosaic provide different value than two
  near-identical Esri-blended sources.

---

## 4. What to research and return

For **each candidate source** you identify, return:

- **Provider name** and what the imagery is (satellite vs. aerial, native
  resolution, vintage, who collected it).
- **Tile URL template**, ready to paste into a `WebTileLayer`, e.g.
  `https://{s}.tile.example.com/{z}/{x}/{y}.jpg` (use `{subDomain}` if
  ArcGIS's convention applies; note explicitly which).
- **Required attribution string** (exact text the provider mandates) and
  whether it must be visible on-map vs. acceptable in an attribution panel.
- **API key requirements**: required? Free tier? Signup URL? Any
  rate/domain/referrer restrictions on the free tier?
- **Coverage area and resolution notes** — global / US-only / regional;
  any known higher-res zones; max effective resolution over water.
- **Max zoom level** the tiles support.
- **Tile size** (256 vs. 512).
- **Projection** (confirm EPSG:3857; flag and explain if anything else).
- **License / terms-of-use summary** in 1–2 sentences, plus a link to the
  authoritative terms page. Flag any prohibition on commercial use,
  redistribution, caching, or web-app use.
- **Honest fit assessment** — which requirements from §3 does this meet,
  and which does it fall short on? Don't bury the gotchas.
- **Recommended `display_name`** for the UI dropdown (short, like the
  existing labels — "Dark", "Satellite", "Hybrid").

Then provide a **ranked recommendation** of the **2–4 sources** that
should actually be added, with a one-paragraph justification per
recommendation. A short list of strong options beats a long list of
mediocre ones.

Finish with a **"things I'm not sure about"** section listing anything you
couldn't verify with confidence (e.g. "MapTiler's attribution-placement
rules for free-tier customers — I couldn't find an unambiguous policy
page") so the user knows what to verify before implementation.

---

## 5. Sources to specifically investigate

Evaluate each of these on the §3 requirements — don't include any by
default. Some have known terms issues for this kind of use case; flag them
honestly.

- **Esri World Imagery** (the current default, for baseline comparison)
- **Mapbox Satellite** / Mapbox Satellite Streets
- **MapTiler Satellite**
- **Google Maps Tiles API** (formerly Maps Static / 2D Tiles API)
- **Bing Maps imagery** (now Microsoft Maps)
- **Sentinel-2 cloudless mosaics** (e.g. EOX Sentinel-2 cloudless, Sentinel
  Hub free tier)
- **NASA GIBS** (Global Imagery Browse Services)
- **USGS imagery services** (USGS Imagery Topo, The National Map)
- **NAIP** — US National Agriculture Imagery Program (highest-res US aerial)
- **OpenAerialMap**

Don't limit yourself to the list if you know of another solid free-tier
provider — but evaluate it against the same criteria.

Notable gotchas to actively check:
- **Google and Bing** terms often restrict use to their own SDK/widget, or
  require billing-enabled accounts even on "free" tiers — verify carefully.
- **Sentinel-2** is ~10 m/pixel native — better than nothing over water but
  no match for true aerial sources at high zoom.
- **NAIP** is excellent US aerial imagery (often 60 cm or better) but is
  **CONUS-only and land-only** — useless over water, so its value here is
  for coastline detail, not water.
- **MapTiler / Mapbox** free tiers have monthly request limits; document them.

---

## 6. Format for your response

Structure your output so it can be **pasted directly into a follow-up
implementation prompt** for a Claude Code agent. Use this rough shape:

```
# Basemap candidates — findings

## Summary
- Recommended additions (ranked): A, B, C
- Brief rationale for each

## Detailed evaluations
### <Provider name>
- Imagery: ...
- Tile URL: ...
- Attribution: "..."
- API key: ...
- Max zoom / tile size / projection: ...
- License summary: ... (link)
- Fit: meets X, Y; falls short on Z
- Recommended display_name: "..."

### <next provider>
...

## Sources rejected (and why)
- Bullet list of providers from §5 you evaluated and rejected, with a
  one-line reason each.

## Open questions / things to verify
- ...
```

Concrete is better than vague. "API key required — get one at
<URL>, free tier 100K requests/month" beats "API key needed (free)".

---

## 7. What NOT to do

- **Don't write any code.** Research and write-up only. The implementer
  will translate your findings into a registry and a `WebTileLayer`
  factory.
- **Don't suggest paid services**, enterprise plans, or anything requiring
  a sales contact.
- **Don't recommend anything whose terms forbid use in a public web
  mapping app**, even if technically reachable.
- **Don't pad the list** with options that clearly fail the requirements
  just to look thorough. A "rejected — and why" bullet is fine; a full
  evaluation of a clear non-starter is wasted words.
- **Don't hide gotchas to make a recommendation look stronger.** Rate
  limits, attribution rules, key requirements, coverage gaps — surface
  them. The implementer needs to know about them before integration, not
  after.
- **Don't recommend a source whose imagery you can't verify is actually
  better than Esri Satellite over water in the Florida Keys area.** That's
  the whole reason for this exercise.
