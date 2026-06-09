"""Black-box runtime security suite: HTTP probes against the live app.

This is the broad companion to `test_backlog.py`. It exercises the running
Flask app through the session `base_url` fixture in `security/conftest.py`,
which binds Werkzeug to a random **loopback** port. Every request below goes
to that loopback base URL via `requests` — no external host is ever contacted,
by construction (the fixture hard-codes 127.0.0.1 and asserts it at import).

SAFETY (read before adding anything here)
------------------------------------------
This runs on a developer's local machine against a local dev server. Every
test is deliberately gentle:

  - **Sequential only.** No threads, no concurrency, no async, no tight loops.
  - **Small, bounded request counts.** The whole module issues well under the
    app's own per-route rate budgets, so it never resembles a load test.
  - **No expensive backend work, ever.** The two routes that *can* do real
    work — the tile proxy (`/raster/...`) and Spotfinder (`/spotfinder/run`) —
    are only ever hit with inputs that short-circuit BEFORE any NOAA fetch,
    numpy compute, or disk write:
      * tile proxy: unknown source, out-of-range param, or out-of-source-zoom
        — each returns 400/503 from a guard that runs before `_load_or_fetch`.
      * Spotfinder: only malformed/empty `search_area`, rejected by
        `_validate_search_area` before `run_spotfinder` is ever called.
    No test sends a request that would decode a real raster.

  The whole module is intended to finish in a few seconds.

RATE-LIMIT TOLERANCE
--------------------
The app's in-memory rate limiter is keyed by client IP, and the whole suite
runs from 127.0.0.1, so requests across all integration tests share one
counter set. The high-volume routes here (`/raster`, the GET pages) sit far
under their limits, but `/spotfinder/run` carries a deliberately tight budget
("10 per minute; 3 per 10 seconds") that `test_backlog.py` also draws on.
Spotfinder probes below therefore accept HTTP 429 as a valid *clean rejection*
outcome alongside 400/422 — a rate-limited request is still "not a 500 and no
leak," which is the property under test. They never retry or loop.

Tests asserting a hardening fix that is not yet implemented are marked
`@pytest.mark.xfail(reason='fix not yet implemented', strict=False)` — never
skipped — so a future fix flips them to XPASS and the report stays an honest
backlog.
"""

import os

import pytest
import requests

# Per-request ceiling, matched to the app's NOAA HTTP timeout so a slow
# response surfaces as a clean failure instead of hanging the suite.
_TIMEOUT_S = 30

# A handful of hostile parameter values reused across the input-validation
# tests. None of these should ever reach expensive backend work, and none
# should ever produce a 500.
_LONG_STRING = "A" * 256
_HOSTILE_STRINGS = (
    "-1",                       # negative where a positive is expected
    "not-an-int",               # text where a number is expected
    _LONG_STRING,               # 256-char overlong value
    "🐟🌊emoji",                  # unicode / emoji
    "' OR 1=1 --",              # SQL-injection shape (app uses no SQL, but probe anyway)
    "../../etc/passwd",         # path-traversal shape
    "%2e%2e%2f%2e%2e%2fetc",    # encoded traversal shape
)

# Substrings whose presence in a response body would mean the server leaked
# an internal stack trace, source path, or dependency internals. Checked
# case-insensitively for the first entry; the rest are matched verbatim.
_LEAK_MARKERS = (
    "traceback (most recent call",
    'File "',
    "site-packages",
    "LayerGeneration",
    "spotfinder.py",
    "/numpy/",
    "werkzeug.exceptions",
    os.sep + "src" + os.sep,
)


def _assert_no_leak(resp):
    """Fail if a response body leaks a stack trace, source path, or internals.

    debug=False means Flask should return generic error bodies; this catches a
    regression that flips the debugger back on or otherwise spills internals.
    """
    body = resp.text or ""
    lowered = body.lower()
    assert "traceback (most recent call" not in lowered, (
        f"response body leaks a Python traceback:\n{body[:400]}"
    )
    for marker in _LEAK_MARKERS[1:]:
        assert marker not in body, (
            f"response body leaks internal detail {marker!r}:\n{body[:400]}"
        )


def _assert_not_500(resp):
    """A malformed request must never crash the server."""
    assert resp.status_code != 500, (
        f"server returned 500 (expected a clean 4xx); body:\n{resp.text[:400]}"
    )
    assert resp.status_code < 500 or resp.status_code == 503, (
        f"server returned an unexpected 5xx {resp.status_code}; 503 is the only "
        f"acceptable 5xx (transient-upstream signal). Body:\n{resp.text[:400]}"
    )


# ===========================================================================
# 1. Input validation — /raster tile route
# ===========================================================================

def test_raster_unknown_or_hostile_source_rejected_cleanly(base_url):
    """Hostile `source` values are rejected with a clean 400, never a 500.

    The source segment is the one free-form string in the tile URL. Each
    hostile value (overlong, unicode, SQLi-shaped, traversal-shaped) resolves
    through `get_source` to "unknown source" and returns 400 *before* any NOAA
    fetch — so this probes the validation boundary without doing real work.
    Path-separator payloads change the URL shape and may 404 at the router
    instead; both 400 and 404 are clean rejections, only 500 is a failure.
    """
    for src in _HOSTILE_STRINGS:
        resp = requests.get(
            f"{base_url}/raster/{src}/256/8/70/119.bin", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code in (400, 404), (
            f"source={src!r} returned {resp.status_code}; expected a clean "
            "400/404 rejection"
        )
        _assert_no_leak(resp)


def test_raster_non_integer_path_params_do_not_500(base_url):
    """Non-integer values in the int path segments are rejected, never 500.

    `resolution`/`z`/`x`/`y` use Flask `<int:>` converters, so negative,
    decimal, or textual values fail to match the route and return 404. The
    point is that a malformed numeric segment can never surface as a 500 deep
    in the fetch/decode code. Each is a single request that never reaches NOAA.
    """
    bad_urls = (
        "/raster/dem-all/-1/8/70/119.bin",       # negative resolution
        "/raster/dem-all/abc/8/70/119.bin",      # textual resolution
        "/raster/dem-all/256/8.5/70/119.bin",    # decimal zoom
        "/raster/dem-all/256/8/-5/119.bin",      # negative x
        "/raster/dem-all/256/8/70/" + _LONG_STRING + ".bin",  # overlong y
    )
    for url in bad_urls:
        resp = requests.get(f"{base_url}{url}", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code in (400, 404), (
            f"{url} returned {resp.status_code}; expected 400/404"
        )
        _assert_no_leak(resp)


def test_raster_out_of_range_zoom_and_resolution_rejected(base_url):
    """Out-of-range `resolution` and `zoom` are rejected by the param guard.

    `_validate_tile_request` runs first in the view, so an oversized resolution
    (2049 > the 2048 cap) or an absurd zoom (99 > the 24 cap) returns a 400
    that names the offending field, with no NOAA fetch. This proves the bounds
    guard fires before any fetch — the gentle way to confirm clamping/rejection.
    """
    over_res = requests.get(
        f"{base_url}/raster/dem-all/2049/8/70/119.bin", timeout=_TIMEOUT_S)
    assert over_res.status_code == 400, (
        f"resolution=2049 (over cap) returned {over_res.status_code}, want 400"
    )
    assert "resolution" in over_res.text.lower()
    _assert_no_leak(over_res)

    zero_res = requests.get(
        f"{base_url}/raster/dem-all/0/8/70/119.bin", timeout=_TIMEOUT_S)
    assert zero_res.status_code == 400, (
        f"resolution=0 (under min) returned {zero_res.status_code}, want 400"
    )

    over_zoom = requests.get(
        f"{base_url}/raster/dem-all/256/99/1/1.bin", timeout=_TIMEOUT_S)
    assert over_zoom.status_code == 400, (
        f"zoom=99 (over cap) returned {over_zoom.status_code}, want 400"
    )
    assert "zoom" in over_zoom.text.lower()
    _assert_no_leak(over_zoom)


def test_raster_tile_xy_out_of_grid_rejected(base_url):
    """Tile x/y beyond the grid for the given zoom is rejected with a 400.

    At zoom z there are only 2**z tiles per axis; `_validate_tile_request`
    rejects coordinates past `(1<<z)-1`. At z=2 the max index is 3, so x=999
    is out of grid and returns 400 before any fetch. Confirms the per-zoom
    coordinate clamp, gently.
    """
    resp = requests.get(
        f"{base_url}/raster/dem-all/256/2/999/999.bin", timeout=_TIMEOUT_S)
    assert resp.status_code == 400, (
        f"x/y=999 at z=2 returned {resp.status_code}, want 400"
    )
    _assert_no_leak(resp)


def test_raster_path_traversal_in_source_does_not_escape(base_url):
    """Path-traversal payloads in the source segment never read host files.

    The disk cache path is built from the *resolved* registry entry's
    `cache_key`, never from the raw request string, and an unresolved source
    returns 400/404. A literal `../../etc/passwd` style source must not return
    file contents or a 500 — just a clean rejection with no leaked path.
    """
    for src in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//....//"):
        resp = requests.get(
            f"{base_url}/raster/{src}/256/8/70/119.bin", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code in (400, 404), (
            f"traversal source {src!r} returned {resp.status_code}"
        )
        assert "root:" not in resp.text, "response appears to contain /etc/passwd"
        _assert_no_leak(resp)


# ===========================================================================
# 2. Input validation — /raster/inspect (bbox raster route)
# ===========================================================================

def test_inspect_missing_params_rejected(base_url):
    """`/raster/inspect` with no query params returns a clean 400.

    Every coordinate is required and parsed up front; a bare request fails the
    float() parse and returns 400 before any bbox math or fetch. Single request.
    """
    resp = requests.get(f"{base_url}/raster/inspect", timeout=_TIMEOUT_S)
    assert resp.status_code == 400, f"got {resp.status_code}, want 400"
    _assert_no_leak(resp)


def test_inspect_non_numeric_coords_rejected(base_url):
    """Non-numeric / hostile bbox coordinates are rejected with a 400.

    `n/s/e/w` are float-parsed; text, SQLi shapes, unicode, and overlong values
    all fail the parse (or the finite/range check) and return 400 before
    `fetch_bbox_raster` runs — so no NOAA call regardless of the `source`.
    """
    for val in _HOSTILE_STRINGS:
        resp = requests.get(
            f"{base_url}/raster/inspect",
            params={"source": "dem-all", "n": val, "s": "24.0",
                    "e": "-80.0", "w": "-81.0", "size": "256"},
            timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code == 400, (
            f"n={val!r} returned {resp.status_code}, want 400"
        )
        _assert_no_leak(resp)


def test_inspect_out_of_range_and_degenerate_bbox_rejected(base_url):
    """Out-of-range latitudes and degenerate boxes are rejected with a 400.

    A latitude of 999 is outside [-90, 90]; a north<=south or east<=west box is
    degenerate. Both are caught before any fetch, returning 400 — confirming
    geographic bounds clamping without touching NOAA.
    """
    out_of_range = requests.get(
        f"{base_url}/raster/inspect",
        params={"source": "dem-all", "n": "999", "s": "24",
                "e": "-80", "w": "-81", "size": "256"},
        timeout=_TIMEOUT_S)
    assert out_of_range.status_code == 400, (
        f"lat=999 returned {out_of_range.status_code}, want 400"
    )

    degenerate = requests.get(
        f"{base_url}/raster/inspect",
        params={"source": "dem-all", "n": "24", "s": "25",  # north < south
                "e": "-81", "w": "-80", "size": "256"},      # east < west
        timeout=_TIMEOUT_S)
    assert degenerate.status_code == 400, (
        f"degenerate bbox returned {degenerate.status_code}, want 400"
    )
    _assert_no_leak(degenerate)


def test_inspect_size_out_of_range_rejected(base_url):
    """`size` beyond the 64..2048 window is rejected before any allocation.

    The size cap bounds the response array (2048² F32 = 16 MB). A value past
    the ceiling (2049) or below the floor (1) is rejected with a 400 *before*
    `fetch_bbox_raster`, so an attacker can't request an absurd grid and the
    server never allocates one. Probes both boundaries; no fetch occurs.
    """
    for size in ("2049", "999999", "1", "0"):
        resp = requests.get(
            f"{base_url}/raster/inspect",
            params={"source": "dem-all", "n": "25", "s": "24",
                    "e": "-80", "w": "-81", "size": size},
            timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code == 400, (
            f"size={size} returned {resp.status_code}, want 400"
        )
        _assert_no_leak(resp)


# ===========================================================================
# 3. HTTP method enforcement
# ===========================================================================

def test_get_routes_reject_wrong_method(base_url):
    """GET-only routes answer 405 (not 500) to a POST.

    Sending the wrong verb must be rejected by Flask's method router before the
    view runs — so even the tile/inspect routes are probed safely here (a POST
    never reaches their fetch code). 405 is the contract; a 500 would mean the
    method check was bypassed.
    """
    get_only = (
        "/", "/map", "/sources", "/basemaps", "/terms", "/privacy",
        "/spotfinder", "/api/session-token", "/raster/inspect",
        "/raster/dem-all/256/8/70/119.bin",
    )
    for path in get_only:
        resp = requests.post(f"{base_url}{path}", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code == 405, (
            f"POST {path} returned {resp.status_code}, want 405"
        )
        _assert_no_leak(resp)


def test_post_routes_reject_wrong_method(base_url):
    """POST-only routes answer 405 (not 500) to a GET.

    `/spotfinder/run` and `/heartbeat` only accept POST. A GET is rejected at
    the method router before the view, so neither the Spotfinder generator nor
    the heartbeat state is touched. Two single requests.
    """
    for path in ("/spotfinder/run", "/heartbeat"):
        resp = requests.get(f"{base_url}{path}", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code == 405, (
            f"GET {path} returned {resp.status_code}, want 405"
        )
        _assert_no_leak(resp)


# ===========================================================================
# 4. Security headers on every kind of response
# ===========================================================================

def test_security_headers_present_on_all_response_types(base_url):
    """CSP, X-Frame-Options, and X-Content-Type-Options are on every response.

    The headers are attached in a single global `after_request` handler, so the
    test deliberately spans response *kinds* — 200 HTML, 200 JSON, a 400
    validation error, a 503 no-coverage signal, and a 404 — to prove the
    handler fires for error responses too, not just the happy path. Each probe
    short-circuits before any NOAA work (the 503 is an out-of-source-zoom tile,
    not a real fetch). Eight endpoints, well over the five-endpoint minimum.
    """
    endpoints = (
        ("/", 200),
        ("/map", 200),
        ("/sources", 200),
        ("/basemaps", 200),
        ("/api/session-token", 200),
        ("/raster/dem-all/256/2/999/999.bin", 400),   # validation error
        ("/raster/dem-global/256/12/1/1.bin", 503),    # out-of-source-zoom
        ("/this-route-does-not-exist", 404),           # router 404
    )
    for path, expected in endpoints:
        resp = requests.get(f"{base_url}{path}", timeout=_TIMEOUT_S)
        assert resp.status_code == expected, (
            f"{path} returned {resp.status_code}, expected {expected}"
        )
        for header in ("Content-Security-Policy", "X-Frame-Options",
                       "X-Content-Type-Options"):
            assert header in resp.headers, (
                f"{path} ({resp.status_code}) is missing the {header} header"
            )
        assert resp.headers["X-Content-Type-Options"].lower() == "nosniff", (
            f"{path}: X-Content-Type-Options is not 'nosniff'"
        )


# ===========================================================================
# 5. Error handling — no internal detail leaks
# ===========================================================================

def test_404_response_is_generic_with_no_leak(base_url):
    """A 404 returns a generic body with no stack trace or source paths.

    With debug=False, an unknown route must produce Flask's generic 404, never
    the interactive debugger or a path-bearing traceback. Single request.
    """
    resp = requests.get(
        f"{base_url}/no/such/route/{_LONG_STRING}", timeout=_TIMEOUT_S)
    assert resp.status_code == 404
    _assert_no_leak(resp)


def test_400_error_body_does_not_leak_internals(base_url):
    """A 400 validation error returns a clean message, not internals.

    The bad-bbox 400 from `/raster/inspect` should carry only the short
    `{"error": ...}` JSON the view emits — no traceback, module name, or file
    path. Single request, no fetch.
    """
    resp = requests.get(
        f"{base_url}/raster/inspect",
        params={"source": "dem-all", "n": "bad", "s": "24",
                "e": "-80", "w": "-81"},
        timeout=_TIMEOUT_S)
    assert resp.status_code == 400
    _assert_no_leak(resp)


def test_assorted_garbage_paths_never_500_or_leak(base_url):
    """A spray of malformed URLs all resolve to clean 4xx, never 5xx/leak.

    Odd path shapes (empty segments, deep nesting, encoded junk) should be
    handled by the router as 404/400, never surface a 500, and never echo a
    traceback or path. A few single requests, none reaching backend work.
    """
    garbage = (
        "/raster",
        "/raster/",
        "/raster//256/8/70/119.bin",
        "/raster/dem-all/256/8/70",            # too few segments
        "/%00%01%02",
        "/api/session-token/extra/segments",
    )
    for path in garbage:
        resp = requests.get(f"{base_url}{path}", timeout=_TIMEOUT_S)
        _assert_not_500(resp)
        assert resp.status_code in (400, 404, 405), (
            f"{path} returned {resp.status_code}; want a clean 4xx"
        )
        _assert_no_leak(resp)


# ===========================================================================
# 6. Cache behavior
# ===========================================================================

def test_registry_routes_are_no_store(base_url):
    """`/sources` and `/basemaps` set `Cache-Control: no-store`.

    These registry listings are meant to reflect live edits on a hard reload,
    so they explicitly opt out of the browser HTTP cache. Asserting the policy
    here guards against a regression that would let a stale source list persist.
    """
    for path in ("/sources", "/basemaps"):
        resp = requests.get(f"{base_url}{path}", timeout=_TIMEOUT_S)
        assert resp.status_code == 200
        cc = resp.headers.get("Cache-Control", "").lower()
        assert "no-store" in cc, (
            f"{path} Cache-Control is {cc!r}; expected no-store"
        )


def test_transient_no_coverage_tile_is_not_cached(base_url):
    """A 503 no-coverage tile carries no day-long cache directive.

    Only data-bearing tiles get `max-age=86400`; a 503 (here forced cheaply by
    an out-of-source-zoom request, no NOAA call) must not be cacheable, so a
    transient empty can never get pinned in the browser cache. Asserts the 503
    response has no `max-age` cache directive.
    """
    resp = requests.get(
        f"{base_url}/raster/dem-global/256/12/1/1.bin", timeout=_TIMEOUT_S)
    assert resp.status_code == 503
    assert "max-age" not in resp.headers.get("Cache-Control", "").lower(), (
        "a 503 no-coverage tile is advertising a long max-age cache lifetime"
    )


# ===========================================================================
# 7. Spotfinder-specific probes
#
# All payloads are malformed search areas that `_validate_search_area` rejects
# BEFORE `run_spotfinder` is invoked, so no NOAA fetch or numpy compute ever
# runs. Because `/spotfinder/run` shares a tight per-IP rate budget with the
# rate-limit test in test_backlog.py, these accept 429 as a valid clean
# rejection alongside 400/422 — see the module docstring. They never retry.
# ===========================================================================

def _spotfinder_reject_ok(resp):
    """A Spotfinder probe is OK if it cleanly rejects and leaks nothing."""
    _assert_not_500(resp)
    assert resp.status_code in (400, 422, 429), (
        f"Spotfinder probe returned {resp.status_code}; expected a clean "
        "400/422 rejection (or 429 if rate-limited)"
    )
    _assert_no_leak(resp)


def test_spotfinder_missing_search_area_rejected(base_url):
    """A POST with no `search_area` is rejected before any compute.

    An empty JSON object fails `_validate_search_area` and returns 400 (or 429
    if the shared limiter has already tripped) — never a 500 and never a
    streamed error after work began. Single request.
    """
    resp = requests.post(f"{base_url}/spotfinder/run", json={},
                         timeout=_TIMEOUT_S)
    _spotfinder_reject_ok(resp)


def test_spotfinder_malformed_bodies_rejected(base_url):
    """A small batch of malformed Spotfinder bodies all reject cleanly.

    Covers the task's Spotfinder cases without ever doing real work:
      - non-object body (JSON array)
      - search_area not an object
      - missing bbox
      - out-of-range coordinates (lat 999)
      - oversized search dimension (width_m past the 200 km cap)
      - degenerate / too-few GeoJSON corners
    Each is rejected by `_validate_search_area` before `run_spotfinder`. The
    batch is kept tiny and 429-tolerant because `/spotfinder/run` is the
    tightest-budgeted route in the app (see module docstring).
    """
    base_area = {
        "bbox": {"north": 25.0, "south": 24.0, "east": -80.0, "west": -81.0},
        "center": {"lat": 24.5, "lng": -80.5},
        "width_m": 1000.0, "height_m": 1000.0, "rotation_deg": 0.0,
        "corners": [{"lat": 24.0, "lng": -81.0}, {"lat": 25.0, "lng": -81.0},
                    {"lat": 25.0, "lng": -80.0}, {"lat": 24.0, "lng": -80.0}],
    }
    payloads = [
        [1, 2, 3],                                              # not an object
        {"search_area": "not-a-dict"},                          # area not object
        {"search_area": {**base_area, "bbox": None}},           # missing bbox
        {"search_area": {**base_area,
                         "bbox": {"north": 999.0, "south": 24.0,
                                  "east": -80.0, "west": -81.0}}},  # bad coords
        {"search_area": {**base_area, "width_m": 5_000_000.0}},  # oversized
        {"search_area": {**base_area, "corners": [{"lat": 24, "lng": -81}]}},  # <3 corners
    ]
    for payload in payloads:
        resp = requests.post(f"{base_url}/spotfinder/run", json=payload,
                             timeout=_TIMEOUT_S)
        _spotfinder_reject_ok(resp)


def test_spotfinder_malformed_geojson_text_rejected(base_url):
    """A non-JSON request body is rejected, not crashed.

    The view uses `get_json(silent=True)`, so a body that isn't JSON yields
    None and returns a clean 400 "must be a JSON object" (or 429). Confirms a
    junk body can't raise a 500. Single request.
    """
    resp = requests.post(
        f"{base_url}/spotfinder/run",
        data="this is not json {{{",
        headers={"Content-Type": "application/json"},
        timeout=_TIMEOUT_S)
    _spotfinder_reject_ok(resp)


# ===========================================================================
# 8. Auth / session boundaries
#
# NOT APPLICABLE AS A GATE: FishFinder has no login, no user accounts, and no
# server-side sessions. No route is access-gated, so there is nothing that
# *should* return 401/403 — a test asserting that would be manufacturing a
# boundary the app doesn't have. What we CAN meaningfully assert is that the
# app is not accidentally trusting client-supplied cookies and that its one
# token endpoint leaks nothing. Those two properties are tested below.
# ===========================================================================

def test_forged_session_cookie_is_ignored_not_errored(base_url):
    """A forged/garbage Cookie header is ignored, never trusted or crashed.

    The app issues no session cookie and gates nothing on one, so a request
    carrying an attacker-controlled cookie must be handled exactly like one
    without it — a normal 200, no 500, no behavior change. Confirms there is no
    hidden cookie-trusting code path. Single request.
    """
    resp = requests.get(
        f"{base_url}/map",
        headers={"Cookie": "session=forged." + _LONG_STRING + "; admin=1"},
        timeout=_TIMEOUT_S)
    assert resp.status_code == 200, (
        f"forged cookie changed the response to {resp.status_code}"
    )
    _assert_no_leak(resp)


def test_session_token_endpoint_exposes_only_a_token(base_url):
    """`/api/session-token` returns just an opaque token, nothing sensitive.

    The terms-gate token is a per-process random hex string used only to detect
    server restarts. The endpoint must expose that single field and nothing
    else (no secret key, no config, no internal state). Single request.
    """
    resp = requests.get(f"{base_url}/api/session-token", timeout=_TIMEOUT_S)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"token"}, (
        f"session-token endpoint exposes unexpected fields: {set(body.keys())}"
    )
    assert isinstance(body["token"], str) and body["token"], (
        "session token is missing or not a string"
    )
    _assert_no_leak(resp)


# ===========================================================================
# 9. Body-size limit (DoS guard)
# ===========================================================================

def test_oversized_request_body_is_rejected(base_url):
    """A POST body past the 512 KB `MAX_CONTENT_LENGTH` cap is refused.

    Flask rejects a body over the configured cap with 413 before the view reads
    it, so a multi-megabyte POST can't be buffered into memory. This sends a
    ~600 KB body of repeated padding (well over the 512 KB cap, but tiny in
    absolute terms and a single sequential request) and asserts a clean 413
    (or 429 if the shared Spotfinder limiter has already tripped) — never a 500.
    The body is invalid JSON anyway, so even if it were read it would 400, not
    do work.
    """
    oversized = "x" * (600 * 1024)
    resp = requests.post(
        f"{base_url}/spotfinder/run",
        data=oversized,
        headers={"Content-Type": "application/json"},
        timeout=_TIMEOUT_S)
    _assert_not_500(resp)
    assert resp.status_code in (413, 400, 429), (
        f"oversized body returned {resp.status_code}; expected 413 (or a clean "
        "400/429)"
    )
    _assert_no_leak(resp)
