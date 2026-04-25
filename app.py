import atexit
import json
import math
import os
import shutil
import threading
import time
from math import ceil, floor

from flask import Flask, jsonify, render_template, send_file
from flask_session import Session
from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.LayerGeneration import LayerGenerator

app = Flask(__name__)

app.config["SESSION_PERMANENT"] = True
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

layer_gen = LayerGenerator()
layer_gen.resolution = 128

with app.app_context():
    if os.path.exists('img/tile'):
        shutil.rmtree('img/tile/')
    os.makedirs('img/tile', exist_ok=True)
    Session.tile_events = {}
    Session.reload = threading.Event()
    Session.reload.set()
    Session.tile_events_lock = threading.Lock()

@app.route('/reload-layer/<string:coord_bounds>_<string:res>_<string:analysis>_<string:smoothness>_<string:width>_<string:source>')
def reload_layer(coord_bounds, res, analysis, smoothness, width, source):
    Session.reload.clear()
    try:
        if os.path.exists('img/tile'):
            shutil.rmtree('img/tile/')
        os.makedirs('img/tile', exist_ok=True)
    except:
        pass

    print("Reloading layer with", coord_bounds, res, analysis, smoothness, width)
    layer_gen.set_resolution(int(res))
    layer_gen.set_analysis(analysis)
    layer_gen.set_roll(int(smoothness))
    layer_gen.set_width(float(width))
    layer_gen.set_data_source(source)
    extent = json.loads(coord_bounds)
    layer_gen.set_gps_bounds(extent)
    Session.reload.set()
    
    return "OK"

def tile_to_lat_lon(tile_x, tile_y, zoom):
    n = 2.0 ** zoom
    lon_min = tile_x / n * 360.0 - 180.0
    lon_max = (tile_x + 1) / n * 360.0 - 180.0
    lat_rad_max = math.atan(math.sinh(math.pi * (1 - 2 * tile_y / n)))
    lat_rad_min = math.atan(math.sinh(math.pi * (1 - 2 * (tile_y + 1) / n)))
    lat_min = math.degrees(lat_rad_min)
    lat_max = math.degrees(lat_rad_max)
    return lon_min, lon_max, lat_min, lat_max

def lat_lon_to_tile(lat, lon, zoom):
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y

def test_info_image(text, output_name):
    # Create a square image
    width, height = int(1.69*256)-1,255  # Define the size of the square image
    img = Image.new('RGB', (width, height), color='red')

    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    draw.text((0, 0), text, (255, 255, 255), font=font)

    square_size = width // 4
    square_x0 = (width - square_size) // 2
    square_y0 = (height - square_size) // 2
    square_x1 = square_x0 + square_size
    square_y1 = square_y0 + square_size
    draw.rectangle([square_x0, square_y0, square_x1, square_y1], outline="blue", fill="blue")

    border_size = 1
    img_with_border = ImageOps.expand(img, border=border_size, fill='black')
    img_with_border.save(output_name, format="PNG")
    return output_name

def crop_image(img_name, x_min=0, x_max=1, y_min=0, y_max=1, output_name="img/temp.png"):
    img = Image.open(img_name)
    width, height = img.size

    x_min_px = floor(x_min * width)
    x_max_px = ceil(x_max * width)
    y_min_px = floor(y_min * height)
    y_max_px = ceil(y_max * height)

    img = img.crop((x_min_px, y_min_px, x_max_px, y_max_px))
    img.save(output_name, format="PNG")
    return output_name

def anti_alias(img):
    return img.resize((4*img.width, 4*img.height), Image.Resampling.LANCZOS)

@app.route('/tile/<int:level>_<int:row>_<int:col>')
def serve_tile(level, row, col):
    start_time = time.time()
    Session.reload.wait()
    filename = f"img/tile/{level}_{row}_{col}.png"
    if os.path.exists(filename):
        print("Serving cached tile", level, row, col, "Time taken:", time.time() - start_time)
        return send_file(filename, mimetype="image/png")

    parent_level = level
    parent_row = row
    parent_col = col

    parent_filename, x_min, x_max, y_min, y_max, output_name = generate_tile(level, row, col, parent_level, parent_row, parent_col)

    if parent_filename == "":
        return send_file("img/blank.png", mimetype="image/png")

    crop_image(parent_filename, x_min, x_max, y_min, y_max, output_name)

    print("Serving generated tile", level, row, col, "Time taken:", time.time() - start_time)
    return send_file(output_name, mimetype="image/png")

def generate_tile(level, row, col, parent_level, parent_row, parent_col):
    if parent_level < 0:
        print("Serving blank image - level too low.")
        return "", 0, 0, 0, 0, ""

    lon_min, lon_max, lat_min, lat_max = tile_to_lat_lon(col, row, level)
    parent_lon_min, parent_lon_max, parent_lat_min, parent_lat_max = tile_to_lat_lon(parent_col, parent_row, parent_level)

    x_min = (lon_min - parent_lon_min) / (parent_lon_max - parent_lon_min)
    x_max = (lon_max - parent_lon_min) / (parent_lon_max - parent_lon_min)
    y_min = 1 - (lat_max - parent_lat_min) / (parent_lat_max - parent_lat_min)
    y_max = 1 - (lat_min - parent_lat_min) / (parent_lat_max - parent_lat_min)

    parent_filename = f"img/tile/{parent_level}_{parent_row}_{parent_col}.png"
    output_name = f"img/tile/{level}_{row}_{col}_temp.png"

    if not os.path.exists(parent_filename):
        extent = {
            "lonmin": parent_lon_min, "lonmax": parent_lon_max,
            "latmin": parent_lat_min, "latmax": parent_lat_max,
        }
        layer_gen.set_gps_bounds(extent)
        layer_gen.set_resolution()
        os.makedirs(f"img/noaa/{layer_gen.analysis_method}", exist_ok=True)
        cache_file = f"img/noaa/{layer_gen.analysis_method}/{parent_level}_{parent_row}_{parent_col}.tiff"
        image = layer_gen.load_data(cache_file=cache_file)
        if image is None:
            print("Serving blank image - NOAA error")
            return "", 0, 0, 0, 0, ""
        image.save(parent_filename, format="PNG")
    return parent_filename, x_min, x_max, y_min, y_max, output_name

@app.route('/map')
def map_page():
    return render_template('map.html', map_layers=[{"id":0}])

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
    """
    Lightweight JSON API:
        GET /depth/25.778/-80.123
      → {"lat":25.778,"lon":-80.123,"depth_meters":-3.6}
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return jsonify({"error": "Bad coordinates"}), 400

    depth_m = layer_gen.sample_depth(lat_f, lon_f)
    return jsonify({
        "lat": lat_f,
        "lon": lon_f,
        "depth_meters": depth_m,
    })

def cleanup_on_exit():
    try:
        if os.path.exists('img/tile'):
            shutil.rmtree('img/tile')
            print("Removed img/tile directory.")
    except Exception as e:
        print(f"Error deleting img/tile: {e}")

    try:
        for root, dirs, files in os.walk('img/noaa'):
            for file in files:
                if file.lower().endswith('.tiff'):
                    file_path = os.path.join(root, file)
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        print(f"Couldn't delete {file_path}: {e}")
    except Exception as e:
        print(f"Error cleaning up img/noaa/: {e}")

atexit.register(cleanup_on_exit)


if __name__ == '__main__':
    app.run(debug=True, port=8080, threaded=True)

