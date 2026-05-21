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

import logging
import os
import struct
import threading
import time

import flask.cli
import numpy as np
from flask import Flask, Response, jsonify, render_template

from src.LayerGeneration import UNKNOWN_SOURCE, fetch_tile_raster
from src.data_sources import (
    DEFAULT_SOURCE_ID,
    all_visible_sources,
    to_client_dict,
)


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

app = Flask(__name__)


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
def serve_raster(source, resolution, z, x, y):
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

    header = struct.pack('<IIfI', w, h, float(cellsize_m), int(buffer_px))
    body = arr32.tobytes(order='C')
    return Response(header + body,
                    mimetype='application/octet-stream',
                    headers={'Cache-Control': 'public, max-age=86400',
                             'Content-Length': str(len(header) + len(body))})


@app.route('/map')
def map_page():
    return render_template('map.html', map_layers=[{'id': 0}])


@app.route('/')
def index():
    return map_page()


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


@app.route('/heartbeat', methods=['POST'])
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
        print(f'auto-shutdown armed: {HEARTBEAT_TIMEOUT_S:.0f}s idle '
              f'after {HEARTBEAT_MIN_PINGS} pings', flush=True)
    print('FishFinder running at http://127.0.0.1:8080', flush=True)
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
