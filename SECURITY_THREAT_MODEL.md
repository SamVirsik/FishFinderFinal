# FishFinder — Public Internet Deployment Threat Model

**Scope:** Flask app (`app.py`), raster proxy (`src/LayerGeneration.py`), source registry (`src/data_sources.py`), Spotfinder compute path (`src/spotfinder.py`), client gate (`templates/map.html`).
**Threat model:** anonymous global attacker who has read this source, knows every endpoint, and probes continuously. Target deployment: cloud VPS, Flask behind Nginx, possibly multi-worker.

**Headline finding:** The application is currently architected for a *single-process, single-trusted-user, localhost* deployment. The code says so explicitly and repeatedly (the `app.run(host='127.0.0.1')` bind, the "single-process dev/desktop server" rate-limiter comment, the auto-shutdown watchdog). Several defenses that look adequate (rate limiting, the terms gate, the "session token") are **structurally void the moment a second worker or a real adversary is introduced.** None of them were designed against a hostile caller.

---

## 1. Authentication & Authorization Gap Analysis

**Current state:** Zero server-side authentication or authorization. Every route is anonymous. The only access "control" is:
- The first-visit **terms gate** (`templates/map.html`) — purely client-side, gated on `localStorage['ff_terms_accepted']`. An attacker calling `/raster/...` or `/spotfinder/run` with `curl` never loads the page, never touches the gate. It is **not a security control** and was never meant to be one.
- The **session token** (`/api/session-token`, `app.py:508`) — a per-process UUID whose *only* job is to re-show the terms gate after a server restart. It is not secret, not validated on any data route, and grants nothing. "Cosmetic" is accurate.

**Endpoint inventory and what each needs:**

| Endpoint | Method | Sensitivity | Recommendation |
|---|---|---|---|
| `/`, `/map`, `/spotfinder`, `/terms`, `/privacy` | GET | Public pages | No auth — but rate-limit |
| `/sources`, `/basemaps`, `/api/session-token` | GET | Public metadata | No auth |
| `/raster/<source>/<res>/<z>/<x>/<y>.bin` | GET | **Expensive proxy** (NOAA bandwidth + disk) | Needs abuse control (see §2/§6) |
| `/raster/inspect` | GET | **Expensive proxy**, up to 16 MB/response | Needs abuse control |
| `/spotfinder/run` | POST | **Most expensive** (up to 50M-cell numpy + many NOAA fetches) | Needs strongest control |
| `/heartbeat` | POST | Triggers process exit when `FISHFINDER_AUTOSHUTDOWN=1` | **Must be disabled/unreachable in prod** (see §4) |

**The real question — does this app need user identity?** Probably not, in the login sense; there is no per-user data. The actual requirement is **anonymous abuse resistance**, not authentication. So the appropriate mechanism is *not* a login system but:

1. **Anonymous signed session cookie** issued on first page load — `HttpOnly`, `Secure`, `SameSite=Lax`, signed with a server secret (Flask's `itsdangerous`/`SECRET_KEY`, or a dedicated signer). Purpose: bind expensive endpoints to "a browser that actually loaded the page," so a raw `curl` loop without a valid cookie is rejected or rate-limited far harder.
2. **A shared secret / API key** is *wrong* here — there is no trusted client to hold it; the JS ships to everyone.
3. **OAuth/JWT** is overkill — no identity to federate.

**Implementation touchpoints:**
- `app.py`: a `@app.before_request` hook that issues/validates the signed cookie, plus a `SECRET_KEY` from environment (never hardcoded).
- The expensive routes (`serve_raster`, `serve_inspect_raster`, `spotfinder_run`) gain a decorator that requires a valid session cookie and falls back to a much tighter rate bucket if absent.
- Real enforcement of "is this a browser" belongs partly at the **Nginx layer** (§4) — e.g., requiring `Referer`/`Origin` on the POST, CAPTCHA in front of `/spotfinder/run` under load.

> **Verdict:** The absence of auth is acceptable *only* for the read-only static pages. For the three expensive endpoints it is the central problem and must be replaced with an anonymous-session + abuse-budget model. A login system is the wrong tool; an anonymous signed cookie + Nginx-layer origin checks is the right one.

---

## 2. Denial of Service Attack Surface

### Per-endpoint worst-case single-request cost

| Endpoint | Compute | Memory | Bandwidth (out + NOAA-in) | Notes |
|---|---|---|---|---|
| `/raster/.../<z>/<x>/<y>.bin` | 1 NOAA fetch + tifffile decode + resize | `(res+32)²×4` bytes; **res capped at 2048 → ~17 MB** per request server-side | Out: up to ~17 MB; In: same from NOAA | `resolution` is attacker-controlled up to 2048 even though the real client only uses ~256–512. **A 2048 request is ~64× the bytes of a normal tile.** |
| `/raster/inspect` | 1 NOAA fetch + decode | `size²×4`, **size capped 2048 → 16 MB** | Out ~16 MB; In ~16 MB | No disk cache, no dedupe — *every* request hits NOAA. Purpose-built bandwidth amplifier. |
| `/spotfinder/run` | **Up to `MAX_TOTAL_CELLS=50,000,000` cells** through scipy filter stack (gaussian, uniform, maximum/minimum, label, convex hull) + **multiple NOAA fetches** (coverage probes across up to 6 sources + chunked fetches up to 4096×4096 each) | 50M cells × several float32 intermediate rasters = **hundreds of MB to >1 GB transient** | Large NOAA inbound | This is the heavyweight. A single well-formed request can pin a core for seconds and allocate ~1 GB. |
| `/heartbeat` | trivial | trivial | trivial | But see §4 — DoS-by-design when autoshutdown armed |

### 1,000 concurrent requests

- **`/spotfinder/run` × 1000:** Catastrophic. Even with the `10/min` + `3/10s` per-IP limit, **the limit is per-IP and per-worker.** 1,000 IPs (botnet, or a single host rotating through a /24) each get 3 requests per 10s = 300 concurrent heavyweight numpy jobs. Flask `threaded=True` will spawn threads without bound; the GIL serializes Python but numpy releases it, so you get genuine parallel memory pressure → **OOM-kill / swap death** long before CPU saturates. There is no global concurrency cap on Spotfinder (the `_noaa_semaphore=6` only bounds *outbound NOAA*, not inbound job admission).
- **`/raster/inspect?size=2048` × 1000:** ~16 GB of NOAA inbound + 16 GB outbound + 1000 simultaneous 16 MB allocations. The `60/min` limit is again per-IP/per-worker.
- **`/raster/...` × 1000 at `resolution=2048`:** similar amplification; disk cache helps only on repeats, and an attacker uses unique `(z,x,y)` to defeat it (§6).

### Is the current rate limiting sufficient?

**No — it is void in production, by the code's own admission.** From `app.py:91`:
> *"In-memory storage is intentional: this is a single-process dev/desktop server... If this is ever fronted by gunicorn with >1 worker, point storage_uri at redis/memcached so the limits are global rather than per-worker."*

Problems for public deployment:
1. **`storage_uri="memory://"`** — with N gunicorn workers, the effective limit is **N × the configured limit**, and a given attacker's requests are load-balanced across workers, so counters never coalesce. With 4 workers the "600/min" raster limit is really 2400/min.
2. **Per-IP keying via `get_remote_address`** — **behind Nginx this keys on the proxy's IP (or `127.0.0.1`), collapsing all clients into one bucket** unless `ProxyFix`/`X-Forwarded-For` handling is configured. Either everyone shares one limit (false positives) or, if naively trusting `X-Forwarded-For`, the header is **attacker-spoofable** → trivial limit bypass.
3. **No global admission control** — limits are per-IP only; a distributed attacker with many IPs faces no aggregate ceiling.
4. **`fixed-window` strategy** allows 2× burst at the window boundary.

### What replaces it

- **Redis-backed limiter** (`storage_uri="redis://..."`) so counters are global across workers.
- **`ProxyFix`** with a *known, fixed* number of trusted proxy hops, so the real client IP is read safely and cannot be spoofed.
- **A global concurrency semaphore on `/spotfinder/run`** (admission control: e.g., max 2–4 in-flight Spotfinder jobs process-wide; excess → `429`/queue with timeout). This is the single most important DoS fix.
- **Drastically lower the attacker-reachable ceilings:** clamp `resolution` to what the client actually uses (~512, not 2048) and `/raster/inspect` `size` likewise; lower `MAX_TOTAL_CELLS` to a value that bounds per-request RAM to a safe fraction of the box.
- **Nginx-layer rate limiting** (`limit_req`, `limit_conn`) as the first line, before Flask spends a thread.

---

## 3. Input Validation — Public Internet Standard

The validation is **genuinely good for a trusted caller** — `_finite()` rejects NaN/inf, lat/lng ranges are enforced, the Flask `<int:>` converters reject negatives/non-digits, `_validate_tile_request` bounds zoom and tile indices, `_validate_search_area` mirrors every consumed field, source IDs are rejected (no silent fallback). This is well above average. But against a hostile caller several gaps remain:

1. **`resolution` upper bound is a resource bound, not a correctness bound (`app.py:211`, `_MAX_RASTER_RES=2048`).** The real client never asks for more than a few hundred. Allowing 2048 hands the attacker a 64× memory/bandwidth amplifier on a route that *is* disk-cached — so an attacker can also **inflate the disk cache** with giant tiles (§6). Validation should reflect what the client actually emits, not the theoretical max.
2. **`/raster/inspect` arbitrary bbox + `size` is an uncached, un-deduped NOAA amplifier.** Input "validation" passes a perfectly legal request that costs 16 MB and a NOAA round-trip every single time. The validation is *correct* but the **economic** envelope it permits is hostile-unsafe. (Cross-ref §6.)
3. **`_validate_search_area` allows a 200 km × 200 km box (`_MAX_SEARCH_DIM_M`).** Combined with `MAX_TOTAL_CELLS=50M`, a hostile but *valid* payload sits right at the maximum compute envelope. The validator's job ends at "geometrically legal"; nothing caps the *cost* a legal box implies until deep inside `_fetch_full_raster` (which raises only after dimension math). An attacker tunes the box to land just under 50M cells repeatedly.
4. **`source` string for `/raster/inspect` (`app.py:374`)** is taken raw and passed to `get_source`. This is safe today (dict lookup, rejects unknown), but note it flows toward filesystem path construction in the tile path (`os.path.join(raster_root, source.cache_key, ...)`). The `cache_key` comes from the *registry*, not the request, so there is **no path traversal today** — good. This must stay true: never let the request string reach a path component.
5. **Spotfinder `params` are attacker-controlled and merged with weak clamping.** `resolve_config` lets `payload["params"]` override *any* `DEFAULT_PARAMS` key (`spotfinder.py:427`), and many are read via `int(params.get(...))` / `float(...)` downstream with no range check. An attacker can set, e.g., `scale_large` to a huge value (drives `maximum_filter`/`minimum_filter` window size → enormous compute) or pathological smoothing radii. `resolve_size_ranges` is defensively clamped; **the core `params` are not.** This is a validation gap that becomes a compute-DoS vector under hostile input.
6. **`request.get_json(silent=True)`** returns `None` on bad JSON (handled), but `MAX_CONTENT_LENGTH=512KB` is the only body-size guard — fine for the POST.

> **Verdict:** Validation is correct and type-safe but tuned for a *cooperative* client. The boundary values it permits (2048 resolution, 16 MB inspect, 50M-cell search, unbounded `params`) are each individually legal and collectively a tuned-abuse toolkit. Re-derive every limit from "what does my own JS actually send" and clamp `params` ranges.

---

## 4. Infrastructure & Deployment Attack Surface

1. **`app.run()` is the Werkzeug dev server (`app.py:663`).** It must **not** serve public traffic — it is single-purpose, not hardened, not performance-tuned, and leaks stack traces if `debug` is ever flipped. Production must use **gunicorn/uwsgi behind Nginx**. (And `host='127.0.0.1'` must stay — Nginx proxies to it; never bind Flask to `0.0.0.0`.)
2. **Server banner / version leakage.** `curl -v` against the live server will, by default, reveal:
   - **`Server: Werkzeug/x.y Python/x.y`** (dev server) or `Server: gunicorn/x.y` — both disclose stack + version. **Strip/override `Server` at Nginx** (`server_tokens off;` plus `proxy_hide_header`/`more_clear_headers`).
   - Nginx's own `Server: nginx/x.y` — set `server_tokens off`.
3. **Stack-trace exposure.** If `FLASK_DEBUG`/`debug=True` is ever set in prod, the Werkzeug interactive debugger is **remote code execution**. Ensure `debug=False` (it is) *and* that the env can't flip it. Generic 500s should be returned, not tracebacks.
4. **`/heartbeat` + `FISHFINDER_AUTOSHUTDOWN` is a remote kill switch.** When armed, anyone who can stop sending heartbeats (or a network blip) exits the process (`os._exit(0)`, `app.py:649`). Worse, it's a *liveness* mechanism designed for a single desktop user. **This must be disabled in production** (`FISHFINDER_AUTOSHUTDOWN` unset) and ideally `/heartbeat` blocked at Nginx. A process supervisor (systemd) owns lifecycle in prod, not the browser.
5. **`print()`-based logging throughout** (`LayerGeneration.py`, `spotfinder.py`). Goes to stdout, may leak operational detail; fine if captured by systemd/journald, but switch to structured logging and ensure NOAA URLs/errors aren't echoed to clients.
6. **Endpoints to block at Nginx before Flask sees them:**
   - `/heartbeat` (prod liveness is systemd's job).
   - `/api/session-token` if the terms gate is removed/reworked.
   - Apply `limit_req`/`limit_conn` to `/raster/`, `/raster/inspect`, `/spotfinder/run` at the edge.
   - Block oversized query strings / enforce `Origin`/`Referer` on the POST.
7. **TLS, HSTS, and security headers at the edge.** The app sets CSP/XCTO/XFO/Referrer-Policy/COOP (good), but **`Strict-Transport-Security` is absent** and must be added (Nginx or Flask). `Secure` cookies (§5) require TLS termination at Nginx.
8. **CORS:** none set — default same-origin. Good; keep it. Don't add permissive `Access-Control-Allow-Origin: *` to the data routes.

---

## 5. Session & State Security

**Current state:** There is no server-side session at all. The "session token" is a non-secret process UUID for re-showing the terms gate; acceptance lives in client `localStorage`. No cookies are set, so there is currently nothing to protect — but also no foundation for the anonymous-session model §1 recommends.

1. **CSRF:** `/spotfinder/run` and `/heartbeat` are state-changing-ish POSTs. Today there are **no cookies, so classic CSRF is moot** (nothing ambient to ride). **The moment you introduce the anonymous session cookie (§1), CSRF becomes live** and must be addressed:
   - `SameSite=Lax` (or `Strict`) on the cookie blocks cross-site POST riding for the common cases.
   - Add an explicit **CSRF token** (double-submit or `flask-wtf`) on `/spotfinder/run`, and/or enforce `Origin`/`Referer` allow-listing at Nginx/Flask for the POST.
2. **Cookie security flags:** when the cookie is added, it **must** be `HttpOnly` (no JS read — the app never needs to read it client-side), `Secure` (TLS-only), `SameSite=Lax`. Set `SESSION_COOKIE_*` config and a strong `SECRET_KEY` from env.
3. **CSP nonce under a reverse proxy — is it sufficient?** The nonce implementation itself is **correct**: a fresh `secrets.token_urlsafe(16)` per request (`app.py:155`), injected into templates, used in `script-src 'nonce-...'` instead of `'unsafe-inline'`. Nginx is a pass-through for the body, so the nonce survives proxying fine. Two caveats:
   - **`style-src 'unsafe-inline'` remains** (required by ArcGIS runtime styling). This is a known, accepted weakening — it permits inline-style injection, which is lower-severity than script but still enables some UI-redress/exfil-via-CSS tricks. Document it as accepted risk; it's not a proxy issue.
   - Ensure no caching layer (Nginx `proxy_cache`) ever caches an HTML page **with its nonce**, or the nonce would be reused across users and the CSP defeated. HTML pages here are dynamic and uncached by default — keep them `Cache-Control: no-store`/private at the page level if proxy caching is introduced.
4. **State integrity:** the disk raster cache and worker memory cache are unauthenticated process state; an attacker can't read them directly, but can *grow* them (§6). Not a confidentiality issue (public NOAA data), purely an availability/cost one.

---

## 6. Data Exfiltration & Abuse Risk

There is no private data to exfiltrate — all bathymetry is public NOAA data. **The asset at risk is your server's compute, bandwidth, and disk, and your standing as a NOAA client.** The proxy turns your VPS into a free, anonymous, caching NOAA amplifier.

**Abuse case — systematic full-Keys tile harvest at max resolution:**

- The Florida Keys reef tract spans roughly 24–25.5°N, −80 to −82°W. At zoom 15 that bounding box is on the order of **tens of thousands of tiles** per source; across the 7 sources and multiple resolutions, **hundreds of thousands of unique `(source, res, z, x, y)` keys.**
- Each *unique* tile = one NOAA fetch + one disk write. At `resolution=2048` each cached TIFF is large; a full harvest can write **many GB to tens of GB to disk** — and **`_load_or_fetch` never expires entries** (CLAUDE.md: "Disk raster cache never expires"). An attacker iterating unique tiles **fills the disk**, which on a VPS means the app (and possibly the OS) falls over. This is a **persistent, unbounded write primitive** exposed to anonymous callers.
- Bandwidth: each unique tile pulls full bytes from NOAA *and* serves them out. A scripted harvest is bounded only by the per-IP `600/min` raster limit — which, per §2, is **per-IP and per-worker**, so a distributed harvester or a multi-worker deployment multiplies it away.
- `/raster/inspect` is worse: **no cache at all**, so even *repeated identical* requests re-hit NOAA. A loop on one bbox is a pure pass-through flood.

**What stops them today:** Only the per-IP/per-worker rate limit and `MAX_CONTENT_LENGTH`. Both are defeated by IP rotation and/or multiple workers. **Nothing caps total disk growth, total NOAA egress, or aggregate request volume.** Effectively: nothing robust.

**What should stop them:**
- **Disk-cache quota + eviction** (LRU or size-capped) on `img/raster/` so a harvest can't exhaust the volume. Today it's "Manual / disk full."
- **Clamp `resolution`/`size`** to real client values (kills the 64× amplifier).
- **Global egress budget** to NOAA (a token-bucket on outbound fetches process-wide, not just the concurrency=6 semaphore which bounds *rate of parallelism* but not *total volume*).
- **Redis-backed, proxy-aware rate limits** (§2) so per-IP actually means per-client.
- **Bind expensive routes to the anonymous session cookie** (§1) so a cookieless `curl` harvest hits a punishing low limit.
- **Edge caching / a CDN in front of `/raster/`** so legitimate repeat tiles never reach Flask or NOAA, shrinking the attack's marginal value.
- Consider **pre-seeding the Keys tile cache** and serving the bathymetry as static tiles for the common zoom levels, removing the live-proxy attack surface for the 99% case.

---

## 7. Prioritized Remediation Backlog

| # | Vulnerability | Severity | Effort (h) | File(s) to change |
|---|---|---|---|---|
| 1 | Rate limiter is in-memory + per-worker + not proxy-aware → effectively bypassable in any multi-worker/Nginx deploy | **Critical** | 4–6 | `app.py` (limiter `storage_uri`→Redis, add `ProxyFix`), deploy config |
| 2 | No global admission/concurrency cap on `/spotfinder/run` (50M-cell numpy + many NOAA fetches) → trivial OOM DoS | **Critical** | 4–8 | `app.py` (admission semaphore), `src/spotfinder.py` (lower `MAX_TOTAL_CELLS`) |
| 3 | Dev server (`app.run`) as public server; ensure gunicorn+Nginx, `debug=False` locked, `Server` header stripped, HSTS added | **Critical** | 4–8 | deploy (gunicorn/systemd/Nginx), `app.py` |
| 4 | `/heartbeat` + `FISHFINDER_AUTOSHUTDOWN` = remote process kill switch reachable anonymously | **High** | 1–2 | `app.py` (gate/remove in prod), Nginx (block `/heartbeat`) |
| 5 | Unbounded, never-expiring disk cache writable by anonymous tile requests → disk-exhaustion DoS | **High** | 6–10 | `src/LayerGeneration.py` (`_load_or_fetch` quota/eviction) |
| 6 | `resolution` (≤2048) and `/raster/inspect` `size` (≤2048) far exceed real client use → 64× bandwidth/memory amplifier | **High** | 1–2 | `app.py` (`_MAX_RASTER_RES`, inspect size cap) |
| 7 | `/raster/inspect` uncached + un-deduped NOAA pass-through → pure amplification flood | **High** | 3–6 | `app.py`, `src/LayerGeneration.py` (cache/dedupe or tighter limit) |
| 8 | No abuse-binding on expensive routes (no anonymous signed session); cookieless `curl` floods at full budget | **High** | 6–10 | `app.py` (signed cookie issue+verify, `SECRET_KEY` from env) |
| 9 | Spotfinder `params` allow overriding any tuning knob with unclamped ranges → compute amplification | **Medium** | 2–4 | `src/spotfinder.py` (`resolve_config` range clamps) |
| 10 | No CSRF protection / cookie flags once a session cookie exists (Lax+HttpOnly+Secure, CSRF token on POST) | **Medium** | 3–5 | `app.py`, `templates/*`, Nginx (Origin check) |
| 11 | Terms gate + session token presented as gating but are client-side only / cosmetic | **Medium** | 1–2 | `templates/map.html`, `app.py` (document as non-security, or back with server check) |
| 12 | Version/stack disclosure via `Server` header; no HSTS | **Medium** | 1–2 | Nginx config, `app.py` (`_security_headers`) |
| 13 | `print()` logging may surface operational detail; ensure no NOAA error/URL reaches clients | **Low** | 2–4 | `src/LayerGeneration.py`, `src/spotfinder.py`, `app.py` |
| 14 | `style-src 'unsafe-inline'` weakens CSP (ArcGIS-required) — accepted risk, document it | **Low** | 0.5 | `app.py` (comment), docs |
| 15 | `fixed-window` rate strategy allows 2× boundary burst | **Low** | 0.5 | `app.py` (→ moving/sliding window) |

**Severity rationale:** Critical = directly enables full DoS or RCE-adjacent exposure on day one of public deployment. High = reliable resource-exhaustion/abuse with low attacker effort. Medium = requires a precondition (e.g., the cookie being added) or yields amplification rather than outright takedown. Low = hardening / defense-in-depth.

---

## Bottom line

The codebase is unusually well-documented and has real, thoughtful hardening (CSP nonces, strict input typing, source-registry validation, transient-failure handling, sentinel masking). **But every availability-class defense it has is scoped to a single trusted local user**, and the code says so. For public exposure the three Critical items — global rate limiting, Spotfinder admission control, and a real WSGI/Nginx deployment with the kill-switch disabled — are non-negotiable prerequisites, and the disk/amplifier abuse vectors (§6) follow immediately after. The auth gap is best closed not with a login system but with an anonymous signed session plus edge-layer origin/rate enforcement.

*Report only — no fixes applied.*
