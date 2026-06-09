"""Black-box integration backlog: runtime security checks over HTTP.

These probe the LIVE app through the session `base_url` fixture in
`security/conftest.py`, which binds Flask to a random **loopback** port.
Every request in this module goes to that loopback base URL via the
`requests` library — no external host is ever contacted, by construction
(the fixture hard-codes 127.0.0.1 and asserts it at import).

SAFETY
------
This file runs on a developer's local machine against a local dev server.
Every test is deliberately gentle:
  - sequential only — no threads, no concurrency, no async
  - a tiny number of requests (the whole module issues well under 30, and
    the one looping test breaks the instant it sees a 429)
  - no endpoint that would trigger a heavy NOAA fetch, a large disk write,
    or multi-megacell compute. The two routes that *could* (the tile proxy
    and Spotfinder) are exercised only via inputs that short-circuit BEFORE
    any expensive work (out-of-zoom-range source; empty Spotfinder body).

Tests that assert a hardening fix which is not yet implemented are marked
`@pytest.mark.xfail(reason='fix not yet implemented')` — never skipped — so
a future fix flips them to XPASS and the report stays an honest backlog.
"""

import requests

import pytest

# Per-request ceiling. Matches the app's own NOAA HTTP_TIMEOUT_S so a slow
# response surfaces as a clean failure rather than hanging the suite.
_TIMEOUT_S = 30

# Hard cap on the rate-limit probe loop. The route we hit trips its limit far
# sooner than this (see the test), so the loop breaks early; the cap is only a
# safety backstop so a misconfigured server can never turn this into a flood.
_MAX_RATE_LIMIT_REQUESTS = 30


def test_rate_limited_route_returns_429_before_30_requests(base_url):
    """A rate-limited route starts returning HTTP 429 within 30 sequential hits.

    Target: POST /spotfinder/run with an empty JSON body. Two properties make
    this both meaningful and gentle:
      - The route carries an explicit tight limit ("3 per 10 seconds"), so the
        4th sequential request is rejected with 429 by Flask-Limiter *before*
        the view runs.
      - The first few requests that DO reach the view hand it `{}`, which fails
        the search-area validation and returns 400 immediately — no NOAA fetch,
        no numpy, no disk write. So nothing expensive ever executes.

    The loop is sequential and breaks the moment a 429 appears, so in practice
    it issues ~4 requests; the 30-cap is only a backstop.
    """
    saw_429_at = None
    for i in range(1, _MAX_RATE_LIMIT_REQUESTS + 1):
        resp = requests.post(f"{base_url}/spotfinder/run", json={},
                             timeout=_TIMEOUT_S)
        if resp.status_code == 429:
            saw_429_at = i
            break

    assert saw_429_at is not None, (
        "no HTTP 429 within 30 sequential requests to /spotfinder/run — the "
        "route's rate limit is not triggering"
    )
    assert saw_429_at < _MAX_RATE_LIMIT_REQUESTS, (
        f"429 only appeared on request #{saw_429_at}; the limit must trip "
        "before the 30th request"
    )


@pytest.mark.xfail(reason='fix not yet implemented')
def test_resolution_param_is_clamped_or_rejected(base_url):
    """An oversized raster `resolution=2048` is not honored at face value.

    A hardened server lowers the resolution cap and rejects 2048 with a 400
    that names `resolution`, *before* it ever looks at the source or fires a
    NOAA fetch (resolution is the first thing `_validate_tile_request` checks).

    To stay gentle this uses `dem-global` (max_zoom=11) at z=12: the tile route
    short-circuits to 503 on the out-of-zoom check WITHOUT any NOAA call, so a
    server that still treats 2048 as valid is observable (it returns 503, not a
    resolution-rejection 400) without doing any heavy work. Single request.
    """
    resp = requests.get(
        f"{base_url}/raster/dem-global/2048/12/1/1.bin", timeout=_TIMEOUT_S)

    assert resp.status_code == 400, (
        f"resolution=2048 was accepted at face value (got {resp.status_code}, "
        "expected a 400 rejecting the oversized resolution)"
    )
    assert "resolution" in resp.text.lower(), (
        "a 400 was returned but it does not name `resolution`; the oversized "
        "resolution is not the thing being rejected"
    )


@pytest.mark.xfail(reason='fix not yet implemented')
def test_heartbeat_endpoint_is_not_exposed(base_url):
    """/heartbeat is not reachable — it returns 404 or 405.

    The heartbeat keepalive backs the opt-in auto-shutdown watchdog; it should
    not be an exposed, callable endpoint in a deployed server. Today POST
    /heartbeat answers 204 (exposed), so this fails until the route is removed
    or gated. A single POST is harmless: in default mode it touches no state.
    """
    resp = requests.post(f"{base_url}/heartbeat", timeout=_TIMEOUT_S)

    assert resp.status_code in (404, 405), (
        f"/heartbeat is exposed (returned {resp.status_code}); it should be "
        "404 or 405"
    )


@pytest.mark.xfail(reason='fix not yet implemented')
def test_session_cookie_has_httponly_and_samesite(base_url):
    """Any Set-Cookie the app emits carries HttpOnly and SameSite.

    HttpOnly keeps a session cookie out of reach of injected JS; SameSite is a
    CSRF backstop. The app currently issues no Set-Cookie at all, so there is
    no hardened cookie to observe — this fails until session cookies exist and
    are configured with both flags. Single request.
    """
    resp = requests.get(f"{base_url}/map", timeout=_TIMEOUT_S)

    set_cookie = resp.headers.get("Set-Cookie", "")
    assert set_cookie, (
        "no Set-Cookie header on the response — no hardened session cookie is "
        "being issued"
    )
    lowered = set_cookie.lower()
    assert "httponly" in lowered, "Set-Cookie is missing the HttpOnly flag"
    assert "samesite" in lowered, "Set-Cookie is missing the SameSite flag"


def test_csp_header_present_on_main_page(base_url):
    """The main page response carries a Content-Security-Policy header.

    CSP is the browser-enforced backstop against injected-script execution and
    data exfiltration. It is attached globally in an after_request handler, so
    the main viewer page must always carry it. Single request.
    """
    resp = requests.get(f"{base_url}/map", timeout=_TIMEOUT_S)

    assert "Content-Security-Policy" in resp.headers, (
        "no Content-Security-Policy header on the main page response"
    )
