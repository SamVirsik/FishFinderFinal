import atexit
import json
import math
import os
import shutil
import threading
import time

from flask import Flask, jsonify, render_template, send_file

from src.LayerGeneration import LayerGenerator


TILE_DIR = 'img/tile'
NOAA_DIR = 'img/noaa'
BLANK_TILE = 'img/blank.png'

app = Flask(__name__)
layer_gen = LayerGenerator()
layer_gen.set_resolution(128)

# Tile requests block on `_reload_event` so a reload that's clearing the cache
# never serves a half-stale tile.
_reload_event = threading.Event()
_reload_event.set()


def _reset_tile_cache():
    if os.path.exists(TILE_DIR):
        shutil.rmtree(TILE_DIR, ignore_errors=True)
    os.makedirs(TILE_DIR, exist_ok=True)


with app.app_context():
    _reset_tile_cache()


# --------------------------------------------------------------------------
# Mercator <-> tile helpers
# --------------------------------------------------------------------------

def tile_to_lat_lon(tile_x, tile_y, zoom):
    n = 2.0 ** zoom
    lon_min = tile_x / n * 360.0 - 180.0
    lon_max = (tile_x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 *  tile_y      / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (tile_y + 1) / n))))
    return lon_min, lon_max, lat_min, lat_max


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route('/reload-layer/<string:coord_bounds>_<string:res>_<string:analysis>'
           '_<string:smoothness>_<string:width>_<string:source>')
def reload_layer(coord_bounds, res, analysis, smoothness, width, source):
    _reload_event.clear()
    try:
        _reset_tile_cache()
        layer_gen.set_resolution(int(res))
        layer_gen.set_analysis(analysis)
        layer_gen.set_roll(int(smoothness))
        layer_gen.set_width(float(width))
        layer_gen.set_data_source(source)
        layer_gen.set_gps_bounds(json.loads(coord_bounds))
        print(f"[reload] {analysis} from {source} @ {res}px (width={width})")
    finally:
        _reload_event.set()
    return "OK"


@app.route('/tile/<int:level>_<int:row>_<int:col>')
def serve_tile(level, row, col):
    start = time.time()
    _reload_event.wait()

    tile_path = f"{TILE_DIR}/{level}_{row}_{col}.png"
    if os.path.exists(tile_path):
        print(f"[tile] cached {level}_{row}_{col} ({time.time() - start:.2f}s)")
        return send_file(tile_path, mimetype="image/png")

    if level < 0:
        return send_file(BLANK_TILE, mimetype="image/png")

    lon_min, lon_max, lat_min, lat_max = tile_to_lat_lon(col, row, level)
    extent = {"lonmin": lon_min, "lonmax": lon_max,
              "latmin": lat_min, "latmax": lat_max}

    # Snapshot config so concurrent tile requests don't race on shared state.
    cfg = layer_gen.snapshot(gps=extent)

    raster_dir = f"{NOAA_DIR}/{cfg.data_source}"
    os.makedirs(raster_dir, exist_ok=True)
    cache_file = f"{raster_dir}/{level}_{row}_{col}.tiff"

    image = layer_gen.render(cfg, cache_file=cache_file)
    if image is None:
        return send_file(BLANK_TILE, mimetype="image/png")

    image.save(tile_path, format="PNG")
    print(f"[tile] generated {level}_{row}_{col} ({time.time() - start:.2f}s)")
    return send_file(tile_path, mimetype="image/png")


@app.route('/map')
def map_page():
    return render_template('map.html', map_layers=[{"id": 0}])


@app.route("/about-us")
def about_us():
    return render_template("about_us.html", section="about")


@app.route("/services")
def services():
    return render_template("about_us.html", section="services")


@app.route("/team")
def team():
    return render_template("about_us.html", section="team")


@app.route("/contact-us")
def contact():
    return render_template("about_us.html", section="contact")


@app.route('/')
def index():
    return map_page()


@app.route("/depth/<lat>/<lon>")
def depth(lat, lon):
    """GET /depth/25.778/-80.123 -> {"lat":..., "lon":..., "depth_meters":...}"""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return jsonify({"error": "Bad coordinates"}), 400

    return jsonify({
        "lat": lat_f,
        "lon": lon_f,
        "depth_meters": layer_gen.sample_depth(lat_f, lon_f),
    })


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------

def cleanup_on_exit():
    if os.path.exists(TILE_DIR):
        try:
            shutil.rmtree(TILE_DIR)
            print(f"Removed {TILE_DIR}")
        except OSError as e:
            print(f"Error deleting {TILE_DIR}: {e}")

    if os.path.exists(NOAA_DIR):
        for root, _dirs, files in os.walk(NOAA_DIR):
            for fname in files:
                if fname.lower().endswith('.tiff'):
                    path = os.path.join(root, fname)
                    try:
                        os.remove(path)
                    except OSError as e:
                        print(f"Couldn't delete {path}: {e}")


atexit.register(cleanup_on_exit)


if __name__ == '__main__':
    app.run(debug=True, port=8080, threaded=True)
