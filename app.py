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
from flask import Flask, Response, render_template

from src.LayerGeneration import fetch_tile_raster


RASTER_DIR = 'img/raster'

# Auto-shutdown when the browser disconnects. The page POSTs to /heartbeat
# every second while it's open; if we go HEARTBEAT_TIMEOUT_S without a ping
# AFTER having received at least one, we exit. So `python app.py` is
# self-clean: close the tab and the server is gone within ~3s.
HEARTBEAT_TIMEOUT_S = 3.0
HEARTBEAT_CHECK_INTERVAL_S = 0.5
_last_heartbeat = None
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


@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    """Browser keepalive ping. Resets the shutdown clock."""
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.monotonic()
    return ('', 204)


def _heartbeat_watcher():
    """Background daemon: shut the process down once heartbeats stop.

    Stays silent until the FIRST heartbeat arrives, so a server started
    without a browser doesn't immediately self-terminate.
    """
    while True:
        time.sleep(HEARTBEAT_CHECK_INTERVAL_S)
        with _heartbeat_lock:
            last = _last_heartbeat
        if last is None:
            continue
        if time.monotonic() - last > HEARTBEAT_TIMEOUT_S:
            os._exit(0)


if __name__ == '__main__':
    # Threaded so the rAF-driven tile bursts from the browser can fan out
    # across NOAA fetches in parallel (bounded by the semaphore in
    # LayerGeneration.py). debug=False so we don't pay the per-request
    # reloader overhead — turn it back on by hand if you're hacking on
    # template/python and want auto-reload.
    threading.Thread(target=_heartbeat_watcher, daemon=True).start()
    print('FishFinder running at http://127.0.0.1:8080', flush=True)
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
