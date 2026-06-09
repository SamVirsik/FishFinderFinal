"""
FishFinder Flask backend.

Two endpoints, plus the single-page viewer:

    /raster/<source>/<res>/{z}/{x}/{y}.bin
        Raw float32 elevation grid for one tile (16-byte header + body).
        The browser's render worker decodes this and runs the chosen
        analysis on the client — the server never colourises. The same
        endpoint backs the click-for-depth lookup: the worker reads the
        cached grid and returns the value at the clicked sub-tile pixel,
        so the depth always matches the data on screen (no NOAA mosaic-
        rule ambiguity from a separate identify/getSamples path).

    /map  (or /)
        The single-page viewer.

The tile-PNG colorization path that used to live here was deleted: every
analysis now runs in a Web Worker on the client, so the only thing the
server has to do is hand over float32 grids and cache them on disk under
img/raster/. See `static/map.js` and `static/analyses-worker.js`.
"""

import json
import logging
import math
import os
import re
import secrets
import struct
import sys
import threading
import time
import uuid

import flask.cli
import numpy as np
from flask import Flask, Response, g, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from src.LayerGeneration import (
    UNKNOWN_SOURCE,
    fetch_bbox_raster,
    fetch_tile_raster,
)
from src.basemap_sources import list_basemaps
from src.data_sources import (
    DEFAULT_SOURCE_ID,
    all_visible_sources,
    to_client_dict,
)
from src.spotfinder import run_spotfinder


RASTER_DIR = 'img/raster'

# Auto-shutdown is opt-in. Default behavior is a normal long-running Flask
# server — `curl`, integration tests, and process supervisors all work
# unmodified, and a probe doesn't trigger a 3-second exit countdown.
#
# Set FISHFINDER_AUTOSHUTDOWN=1 to arm the watchdog. When armed, the page's
# 1 Hz POSTs to /heartbeat are counted; we only start watching for silence
# AFTER receiving at least HEARTBEAT_MIN_PINGS (so a single curl probe can't
# trip it), and we exit if HEARTBEAT_TIMEOUT_S elapses without a ping. The
# 10 s timeout gives a real browser plenty of headroom on tab restore.
_AUTOSHUTDOWN = os.environ.get('FISHFINDER_AUTOSHUTDOWN') == '1'
HEARTBEAT_TIMEOUT_S = 10.0
HEARTBEAT_MIN_PINGS = 2
HEARTBEAT_CHECK_INTERVAL_S = 0.5
_last_heartbeat = None
_heartbeat_count = 0
_heartbeat_lock = threading.Lock()

# Quiet the dev server. Werkzeug logs every request at INFO and prints its
# own startup banner; Flask's CLI prints a separate banner. Bumping the
# logger to ERROR + monkey-patching the Flask banner suppresses both, so
# the terminal stays clean during normal operation.
logging.getLogger('werkzeug').setLevel(logging.ERROR)
flask.cli.show_server_banner = lambda *args, **kwargs: None

# Application logger. Routed to stdout with a bare message format so the
# startup banner and the spotfinder stream-guard diagnostic read exactly as
# the previous bare-print output did. propagate=False keeps these off the root
# handler so they aren't duplicated to stderr.
logger = logging.getLogger("fishfinder")
if not logger.handlers:
    _log_handler = logging.StreamHandler(sys.stdout)
    _log_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_log_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

app = Flask(__name__)

# Reject oversized request bodies before they're read into memory. The only
# route that accepts a body is /spotfinder/run, whose payload is a small JSON
# config (search area + tuning params); 512 KB is far more than that ever
# needs and stops a multi-megabyte POST from being buffered.
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024

# Session-cookie hardening. Flask's default is already HttpOnly, but set it
# explicitly so an XSS payload can never read the signed session cookie via
# document.cookie, and so the safe value is pinned against a future regression.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config["SESSION_COOKIE_SECURE"] = True

# Signing key for the session cookie. Sourced strictly from the environment
# with no hardcoded or generated fallback: a missing key must fail loud at
# startup rather than silently signing sessions with a guessable or
# per-process-random value. We check explicitly so the failure is an
# actionable message instead of a bare KeyError.
if not os.environ.get("SECRET_KEY"):
    raise RuntimeError(
        "The session signing key is missing, so the app refuses to start "
        "(sessions must never be signed with a guessable or per-process-random "
        "key). Set it in the environment before launching — the app reads "
        "os.environ['SECRET_KEY']. Generate a value with Python's "
        "secrets.token_hex(32). See the 'Running it' section of CLAUDE.md for "
        "ready-to-paste PowerShell and bash one-liners."
    )
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]


# ---------------------------------------------------------------------------
# Rate limiting (Flask-Limiter).
#
# Keyed by client IP. In-memory storage is intentional: this is a
# single-process dev/desktop server (app.run, threaded=True), so there is no
# second worker to share counters with. If this is ever fronted by gunicorn
# with >1 worker, point storage_uri at redis/memcached so the limits are
# global rather than per-worker.
#
# Budgets are tuned to the app's real traffic shape:
#   - tile proxy (/raster/...) is bursty — a single pan can request dozens of
#     tiles, and the prefetch loop warms more — so it gets the largest budget.
#   - /spotfinder/run is the expensive compute path (multi-megacell numpy +
#     several NOAA fetches), so it gets the tightest budget.
#   - the heartbeat keepalive fires ~1 Hz by design and is exempted.
#   - Flask's built-in static endpoint is exempted (page loads pull many
#     static assets and they're cheap to serve).
# ---------------------------------------------------------------------------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["240 per minute", "4000 per hour"],
    storage_uri="memory://",
    strategy="fixed-window",
    headers_enabled=True,   # emit RateLimit-* + Retry-After so clients can back off
)
limiter.init_app(app)


@limiter.request_filter
def _exempt_static():
    """Skip rate limiting for Flask's static-file endpoint."""
    return request.endpoint == 'static'


# Unique per-process token, regenerated every time the server boots. The
# map's terms gate stores the token it accepted under alongside acceptance;
# on each page load the client compares its stored token to this live one
# (via GET /api/session-token). A mismatch — which always happens after a
# restart — invalidates the stored acceptance and re-shows the gate.
_SESSION_TOKEN = uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Content Security Policy + companion security headers.
#
# The policy is allow-listed to exactly the origins the viewer loads from:
#   - ArcGIS JS API (js.arcgis.com) for the map engine, its blob-spawned
#     Web Workers, and its bundled marker/icon assets.
#   - unpkg.com for three.js + OrbitControls (the 3D inspector).
#   - Google Fonts (fonts.googleapis.com stylesheet + fonts.gstatic.com files).
#   - basemap.nationalmap.gov for USGS basemap tiles (fetched directly by the
#     ArcGIS TileLayer in the browser).
#   - 'self' for our own static JS/CSS, the analyses Web Worker, and every
#     /raster, /sources, /basemaps, /spotfinder XHR (NOAA is proxied through
#     this server, so the browser never talks to NOAA directly — no NOAA
#     origin is needed in connect-src).
#
# Our own inline <script> blocks (the terms-gate bootstrap, the map_layers
# injection, the landing-page animation flag) are authorised with a fresh
# per-request nonce rather than 'unsafe-inline', so an injected inline script
# can't execute. style-src keeps 'unsafe-inline' because the ArcGIS API sets
# element style attributes at runtime, which nonces can't cover.
# ---------------------------------------------------------------------------

@app.before_request
def _make_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def _inject_csp_nonce():
    # Exposes {{ csp_nonce }} to every template so inline <script> tags can
    # carry the matching nonce.
    return {'csp_nonce': getattr(g, 'csp_nonce', '')}


def _content_security_policy(nonce):
    return "; ".join([
        "default-src 'self'",
        ("script-src 'self' 'nonce-{nonce}' blob: "
         "https://js.arcgis.com https://unpkg.com").format(nonce=nonce),
        ("style-src 'self' 'unsafe-inline' "
         "https://js.arcgis.com https://fonts.googleapis.com"),
        "img-src 'self' data: blob: https://js.arcgis.com https://basemap.nationalmap.gov",
        "font-src 'self' data: https://fonts.gstatic.com https://js.arcgis.com",
        ("connect-src 'self' blob: "
         "https://js.arcgis.com https://basemap.nationalmap.gov"),
        "worker-src 'self' blob:",
        "child-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'self'",
        "form-action 'self'",
    ])


@app.after_request
def _security_headers(resp):
    """Attach CSP + companion hardening headers to every response."""
    resp.headers['Content-Security-Policy'] = _content_security_policy(
        getattr(g, 'csp_nonce', ''))
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    return resp


# ---------------------------------------------------------------------------
# Input validation helpers.
#
# Every value that crosses from the network into the analysis/fetch code is
# validated here first, so a malformed request returns a clear 400 instead of
# surfacing deep in numpy/NOAA code as a 500 (or, worse, an unbounded fetch).
# ---------------------------------------------------------------------------

# Tile-pyramid + raster sizing bounds. resolution feeds the NOAA fetch grid
# dimension; z/x/y are standard XYZ tile coords. Flask's <int:> converters
# already reject negatives and non-digits, so these only need upper bounds.
_MAX_TILE_ZOOM = 24
_MIN_RASTER_RES = 1
_MAX_RASTER_RES = 512

# Geographic bounds (WGS84 degrees) and the largest search box we'll accept.
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0
_MAX_SEARCH_DIM_M = 200_000.0   # 200 km on a side — far past any real draw.


def _finite(v):
    """True only for a real (non-NaN, non-inf) float."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _valid_lat(v):
    return _finite(v) and _LAT_MIN <= float(v) <= _LAT_MAX


def _valid_lng(v):
    return _finite(v) and _LNG_MIN <= float(v) <= _LNG_MAX


def _validate_tile_request(resolution, z, x, y):
    """Bounds-check XYZ tile params. Returns an error string or None."""
    if not (_MIN_RASTER_RES <= resolution <= _MAX_RASTER_RES):
        return f"resolution out of range ({_MIN_RASTER_RES}..{_MAX_RASTER_RES})"
    if z > _MAX_TILE_ZOOM:
        return f"zoom out of range (0..{_MAX_TILE_ZOOM})"
    # At zoom z there are 2**z tiles per axis; reject coords outside the grid.
    max_index = (1 << z) - 1
    if x > max_index or y > max_index:
        return f"tile x/y out of range for zoom {z} (0..{max_index})"
    return None


def _validate_search_area(area):
    """Validate the Spotfinder `search_area` payload. Returns error str or None.

    Mirrors exactly the fields run_spotfinder consumes: bbox (n/s/e/w),
    center (lat/lng), width_m / height_m, rotation_deg, and the corners list.
    Anything non-numeric, out of geographic range, degenerate, or absurdly
    large is rejected with a specific message before any NOAA fetch fires.
    """
    if not isinstance(area, dict):
        return "search_area must be an object"

    bbox = area.get("bbox")
    if not isinstance(bbox, dict):
        return "search_area.bbox is required"
    for k in ("north", "south", "east", "west"):
        if k not in bbox:
            return f"search_area.bbox.{k} is required"
    if not (_valid_lat(bbox["north"]) and _valid_lat(bbox["south"])):
        return "bbox north/south must be valid latitudes"
    if not (_valid_lng(bbox["east"]) and _valid_lng(bbox["west"])):
        return "bbox east/west must be valid longitudes"
    if not (float(bbox["north"]) > float(bbox["south"])
            and float(bbox["east"]) > float(bbox["west"])):
        return "bbox must have north > south and east > west"

    center = area.get("center")
    if not isinstance(center, dict):
        return "search_area.center is required"
    if not (_valid_lat(center.get("lat")) and _valid_lng(center.get("lng"))):
        return "center lat/lng must be valid coordinates"

    for k in ("width_m", "height_m"):
        v = area.get(k)
        if not _finite(v) or not (0.0 < float(v) <= _MAX_SEARCH_DIM_M):
            return f"{k} must be a positive number under {int(_MAX_SEARCH_DIM_M)} m"

    if not _finite(area.get("rotation_deg")):
        return "rotation_deg must be a number"

    corners = area.get("corners")
    if not isinstance(corners, (list, tuple)) or len(corners) < 3:
        return "search_area.corners must list at least 3 points"
    for c in corners:
        if not isinstance(c, dict) or not (_valid_lat(c.get("lat"))
                                           and _valid_lng(c.get("lng"))):
            return "every corner must have a valid lat/lng"
    return None


# ---------------------------------------------------------------------------
# Raw raster route — float32 depth grid for client-side analysis rendering.
#
# Wire format (little-endian, 16-byte header + body):
#   u32   width   — always == height; equals max(OUTPUT_TILE_PX, res) + 2*buf
#   u32   height
#   f32   cellsize_m — true ground sample distance, Mercator-corrected
#   u32   buffer_px  — pixels of overdraw on every edge (client crops)
#   body  width*height float32, row-major, NaN = nodata
#
# Tiles are cached aggressively in the browser (Cache-Control: 1 day) and on
# disk by `_load_or_fetch`. Once a tile has been seen at a (source, res) the
# only cost on revisit is bandwidth.
# ---------------------------------------------------------------------------

@app.route('/raster/<string:source>/<int:resolution>'
           '/<int:z>/<int:x>/<int:y>.bin')
@limiter.limit("600 per minute")
def serve_raster(source, resolution, z, x, y):
    err = _validate_tile_request(resolution, z, x, y)
    if err is not None:
        return jsonify({"error": err}), 400
    result = fetch_tile_raster(source, resolution, z, x, y,
                               raster_root=RASTER_DIR)
    if result is UNKNOWN_SOURCE:
        # Hard reject for typo'd / unregistered source IDs. The previous
        # behavior was to silently fall back to dem-tiles and cache the
        # bytes under the bogus name — fixed by the registry rewrite.
        return jsonify({"error": f"unknown source: {source}"}), 400
    if result is None:
        # 503 (not 204) is deliberate: the worker treats 204 as "NOAA
        # legitimately confirmed no coverage here" and caches a permanent
        # blank canvas for that URL. A transient NOAA failure used to land
        # on the same path, locking the tile as a forever-blank for the
        # session. 5xx flows through as a transient error in the worker,
        # is NOT cached, and the next pan/zoom retries cleanly. Genuine
        # no-coverage now flows through HTTP 200 with all-NaN data and is
        # detected as `empty` inside the worker.
        return ('', 503)

    arr, cellsize_m, buffer_px = result
    h, w = arr.shape
    arr32 = np.ascontiguousarray(arr, dtype=np.float32)

    # Don't let the browser HTTP cache hold an all-nodata tile for a day:
    # an empty grid can be a transient NOAA blip rather than genuine
    # no-coverage, and a day-long cached empty would keep painting grey
    # across reloads even after upstream recovers. Data tiles keep the
    # 1-day cache; empties are revalidated on the next visit. (The disk
    # cache already refuses to persist empties — see _load_or_fetch.)
    all_nodata = bool(np.isnan(arr).all())
    cache_control = 'no-store' if all_nodata else 'public, max-age=86400'

    header = struct.pack('<IIfI', w, h, float(cellsize_m), int(buffer_px))
    body = arr32.tobytes(order='C')
    return Response(header + body,
                    mimetype='application/octet-stream',
                    headers={'Cache-Control': cache_control,
                             'Content-Length': str(len(header) + len(body))})


# ---------------------------------------------------------------------------
# Inspector raster route — one float32 grid for a user-defined bbox.
#
# Same wire format as /raster/<src>/<res>/{z}/{x}/{y}.bin (16-byte header
# + body), but the bbox is arbitrary lon/lat rather than tile-aligned.
# buffer_px is always 0 — the inspector doesn't run gradient analyses
# across tile seams, so it doesn't need the edge overdraw.
#
# No caching (browser or disk): each rectangle is unique enough that a
# cache hit is unlikely and the disk bloat would add up over a session
# of exploring multiple areas.
# ---------------------------------------------------------------------------

@app.route('/raster/inspect')
@limiter.limit("60 per minute")
def serve_inspect_raster():
    source = (request.args.get('source') or '').strip()
    try:
        north = float(request.args.get('n', ''))
        south = float(request.args.get('s', ''))
        east  = float(request.args.get('e', ''))
        west  = float(request.args.get('w', ''))
        size  = int(request.args.get('size', '512'))
    except (TypeError, ValueError):
        return jsonify({"error": "missing or invalid query params"}), 400

    # Reject NaN/inf and out-of-range coordinates before any bbox math.
    if not (_valid_lat(north) and _valid_lat(south)
            and _valid_lng(east) and _valid_lng(west)):
        return jsonify({"error": "coordinates out of range"}), 400
    if not (north > south and east > west):
        return jsonify({"error": "invalid bbox"}), 400
    # Size cap: 2048×2048 float32 = 16 MB per response, plenty of headroom
    # for the highest realistic detail setting and still safe for memory.
    if size < 64 or size > 2048:
        return jsonify({"error": "size out of range (64..2048)"}), 400

    result = fetch_bbox_raster(source, west, south, east, north, size)
    if result is UNKNOWN_SOURCE:
        return jsonify({"error": f"unknown source: {source}"}), 400
    if result is None:
        return ('', 503)

    arr, cellsize_m = result
    h, w = arr.shape
    arr32 = np.ascontiguousarray(arr, dtype=np.float32)
    header = struct.pack('<IIfI', w, h, float(cellsize_m), 0)
    body = arr32.tobytes(order='C')
    return Response(header + body,
                    mimetype='application/octet-stream',
                    headers={'Cache-Control': 'no-store',
                             'Content-Length': str(len(header) + len(body))})


@app.route('/map')
def map_page():
    return render_template('map.html', map_layers=[{'id': 0}])


@app.route('/')
def index():
    return render_template('landing.html')


@app.route('/data')
def data_page():
    return render_template('data.html')


@app.route('/pricing')
def pricing_page():
    return render_template('pricing.html')


@app.route('/about')
def about_page():
    return render_template('about.html')


def _parse_legal(text):
    """Turn a plain-text legal document into a list of typed blocks for
    the template to render.

    The source .txt files are hard-wrapped prose with blank-line
    separators between blocks — but a "blank" line may contain stray
    whitespace, so we can't just split on '\\n\\n'. We group runs of
    non-blank lines into blocks, then classify each:

      - heading : a lone numbered section line, e.g. "8. ACCEPTABLE USE"
      - meta    : the "Last updated: …" line
      - list    : a block whose lines are "- " bullets (wrapped
                  continuation lines fold into the current item)
      - para    : everything else; wrapped lines join with spaces

    The document's own leading title line (FISHFINDER — …) is dropped:
    the template renders the page title itself, so keeping it would
    duplicate the heading.
    """
    lines = text.replace('\r\n', '\n').split('\n')
    raw_blocks, cur = [], []
    for ln in lines:
        if ln.strip() == '':
            if cur:
                raw_blocks.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        raw_blocks.append(cur)

    blocks = []
    for i, blk in enumerate(raw_blocks):
        if any(l.lstrip().startswith('- ') for l in blk):
            items = []
            for l in blk:
                s = l.strip()
                if s.startswith('- '):
                    items.append(s[2:].strip())
                elif items:            # wrapped continuation of prior bullet
                    items[-1] += ' ' + s
                else:
                    items.append(s)
            blocks.append({'type': 'list', 'items': items})
            continue

        joined = ' '.join(l.strip() for l in blk)
        if i == 0 and re.match(r'^FISHFINDER\b', joined, re.I):
            continue                   # drop the doc's own title line
        if len(blk) == 1 and re.match(r'^\d+\.\s+\S', joined):
            blocks.append({'type': 'heading', 'text': joined})
        elif joined.lower().startswith('last updated'):
            blocks.append({'type': 'meta', 'text': joined})
        else:
            blocks.append({'type': 'para', 'text': joined})
    return blocks


def _render_legal(filename, title):
    """Read a plain-text legal document, parse it into blocks, and render
    it in the shared legal template."""
    path = os.path.join(os.path.dirname(__file__), 'legal', filename)
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return render_template('legal.html', title=title,
                           blocks=_parse_legal(text))


@app.route('/terms')
def terms_page():
    return _render_legal('terms.txt', 'Terms of Service')


@app.route('/privacy')
def privacy_page():
    return _render_legal('privacy.txt', 'Privacy Policy')


@app.route('/spotfinder')
def spotfinder_page():
    # Bounding box is carried via query params (n/s/e/w) — keeps the
    # page bookmarkable and surviveable across reloads. The template
    # parses + validates client-side; the server just renders the shell.
    return render_template('spotfinder.html')


@app.route('/api/session-token')
def session_token():
    # The map's terms gate ties acceptance to this token (see _SESSION_TOKEN
    # near the top of this file). The browser stores the token it accepted
    # under; when it no longer matches the live one — i.e. the server has
    # restarted — the gate re-shows and forces re-acceptance.
    return jsonify({'token': _SESSION_TOKEN})


@app.route('/spotfinder/run', methods=['POST'])
@limiter.limit("10 per minute; 3 per 10 seconds")
def spotfinder_run():
    """Streaming Spotfinder execution.

    The algorithm is a generator that yields progress / result / error
    events as plain dicts. We re-emit them as newline-delimited JSON
    (one event per line) so the browser can update its UI live using
    fetch() + ReadableStream — no SSE wire format, no polling.

    The Flask request body is the JSON-encoded SpotfinderInput
    (search_area + params), exactly the same shape the old in-browser
    stub took. The response body is a stream of newline-delimited JSON
    events with `Content-Type: application/x-ndjson`.

    Streaming responses run inside Flask's WSGI iterable so each
    `yield` flushes to the wire as soon as the generator produces it.
    Network buffering on the proxy side can defer the flush, but the
    Flask dev server (threaded=True) flushes immediately, which is what
    matters for local development.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    # Validate the search area up front so a malformed box returns a clean 400
    # rather than streaming a generic error event after work has begun. The
    # tuning `config`/`params` are validated/clamped defensively inside
    # resolve_config(), so they don't need a gate here.
    area_err = _validate_search_area(payload.get("search_area"))
    if area_err is not None:
        return jsonify({"error": area_err}), 400

    def stream():
        try:
            for event in run_spotfinder(payload):
                yield json.dumps(event, allow_nan=False) + "\n"
        except Exception as e:
            # Last-resort guard: the generator itself raises an
            # error event for SpotfinderError, but a programmer error
            # in this module would still bubble through here.
            logger.error(f"[spotfinder] stream guard caught: {e!r}")
            yield json.dumps({
                "type": "error",
                "message": f"Spotfinder failed: {e.__class__.__name__}",
            }) + "\n"

    return Response(
        stream(),
        mimetype='application/x-ndjson',
        headers={
            # Hard-disable caching: progress streams are inherently per-request.
            'Cache-Control': 'no-store',
            # Hint to any intermediaries not to buffer (nginx in particular
            # holds chunked responses by default).
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/sources')
def list_sources():
    """Bathymetry-source registry, browser-facing subset.

    The dropdown in `templates/map.html` is empty in source; the client
    populates it from this endpoint on load. Same data also feeds the
    per-source max-zoom clamp used by the prefetch loop.

    No-cache while the registry is in flux — we want a hard reload to
    pick up any registry edit without the browser serving a stale list.
    """
    body = {
        "default": DEFAULT_SOURCE_ID,
        "sources": [to_client_dict(s) for s in all_visible_sources()],
    }
    resp = jsonify(body)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/basemaps')
def list_basemaps_route():
    """Basemap registry, browser-facing.

    `templates/map.html` ships an empty `#basemap-grid` container; the
    client populates it from this endpoint at boot. Same no-cache policy
    as `/sources` — a hard reload should pick up registry edits.

    Note: this response does NOT carry a `default` field. The client
    pins its cold-load basemap with a hardcoded literal so the map can
    render before any HTTP round-trip lands. Don't add one — see the
    docstring in `src/basemap_sources.py`.
    """
    resp = jsonify({"basemaps": list_basemaps()})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/heartbeat', methods=['POST'])
@limiter.exempt
def heartbeat():
    """Browser keepalive ping.

    Always 204 so the client contract is identical in both modes. We only
    touch the shared state when the watchdog is armed — no point paying
    lock contention on every ping in default mode.
    """
    if _AUTOSHUTDOWN:
        global _last_heartbeat, _heartbeat_count
        with _heartbeat_lock:
            _last_heartbeat = time.monotonic()
            _heartbeat_count += 1
    return ('', 204)


def _heartbeat_watcher():
    """Background daemon: shut the process down once heartbeats stop.

    Two guards before we ever exit:
      - At least HEARTBEAT_MIN_PINGS pings must have arrived. A single
        curl probe is not enough to arm the kill switch.
      - At least HEARTBEAT_TIMEOUT_S must have elapsed since the most
        recent ping.
    """
    while True:
        time.sleep(HEARTBEAT_CHECK_INTERVAL_S)
        with _heartbeat_lock:
            last = _last_heartbeat
            count = _heartbeat_count
        if count < HEARTBEAT_MIN_PINGS or last is None:
            continue
        if time.monotonic() - last > HEARTBEAT_TIMEOUT_S:
            os._exit(0)


if __name__ == '__main__':
    # Threaded so the rAF-driven tile bursts from the browser can fan out
    # across NOAA fetches in parallel (bounded by the semaphore in
    # LayerGeneration.py). debug=False so we don't pay the per-request
    # reloader overhead — turn it back on by hand if you're hacking on
    # template/python and want auto-reload.
    if _AUTOSHUTDOWN:
        threading.Thread(target=_heartbeat_watcher, daemon=True).start()
        logger.info(f'auto-shutdown armed: {HEARTBEAT_TIMEOUT_S:.0f}s idle '
                    f'after {HEARTBEAT_MIN_PINGS} pings')
    logger.info('FishFinder running at http://127.0.0.1:8080')
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
