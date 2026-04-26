"""
FishFinder Flask backend.

Three endpoints, all stateless:

    /raster/<source>/<res>/{z}/{x}/{y}.bin
        Raw float32 elevation grid for one tile (16-byte header + body).
        The browser's render worker decodes this and runs the chosen
        analysis on the client — the server never colourises.

    /depth/<lat>/<lon>?source=<src>
        Depth (metres) at a single point, for click-for-depth.

    /map  (or /)
        The single-page viewer.

The tile-PNG colorization path that used to live here was deleted: every
analysis now runs in a Web Worker on the client, so the only thing the
server has to do is hand over float32 grids and cache them on disk under
img/raster/. See `static/map.js` and `static/analyses-worker.js`.
"""

import struct

import numpy as np
from flask import Flask, Response, jsonify, render_template, request

from src.LayerGeneration import (
    DEFAULT_SOURCE,
    fetch_tile_raster,
    sample_depth,
)


RASTER_DIR = 'img/raster'

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
        return ('', 204)

    arr, cellsize_m, buffer_px = result
    h, w = arr.shape
    arr32 = np.ascontiguousarray(arr, dtype=np.float32)

    header = struct.pack('<IIfI', w, h, float(cellsize_m), int(buffer_px))
    body = arr32.tobytes(order='C')
    return Response(header + body,
                    mimetype='application/octet-stream',
                    headers={'Cache-Control': 'public, max-age=86400',
                             'Content-Length': str(len(header) + len(body))})


@app.route('/depth/<lat>/<lon>')
def depth(lat, lon):
    """Depth in metres at a click point. Source comes from a query param."""
    source = request.args.get('source', DEFAULT_SOURCE)
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return jsonify({'error': 'Bad coordinates'}), 400

    return jsonify({
        'lat': lat_f,
        'lon': lon_f,
        'source': source,
        'depth_meters': sample_depth(source, lat_f, lon_f),
    })


@app.route('/map')
def map_page():
    return render_template('map.html', map_layers=[{'id': 0}])


@app.route('/')
def index():
    return map_page()


if __name__ == '__main__':
    # Threaded so the rAF-driven tile bursts from the browser can fan out
    # across NOAA fetches in parallel (bounded by the semaphore in
    # LayerGeneration.py). debug=False so we don't pay the per-request
    # reloader overhead — turn it back on by hand if you're hacking on
    # template/python and want auto-reload.
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
