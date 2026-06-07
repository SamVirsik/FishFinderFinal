"""
Live raster-pipeline diagnostics.

Drives the REAL production fetch path (`src.LayerGeneration.fetch_tile_raster`)
against the live NOAA ImageServer endpoints, for every registered source,
across several Florida Keys regions and zoom levels. Reports, per probe:

  - outcome: OK (data) / EMPTY (all-nodata, i.e. no coverage here) /
             FAIL (503-class: NOAA error, decode reject, timeout) /
             UNKNOWN (source id not in registry) / SKIP (zoom out of source range)
  - cold latency (network fetch, fresh temp disk cache)
  - warm latency (disk-cache hit, no network)
  - nodata fraction over the *visible* (un-buffered) tile region
  - depth range (min/median/max of valid samples, in metres)
  - wire-format header sanity (width/height/cellsize/buffer_px)

It also validates that the server's binary wire format round-trips: the
header packed by `app.serve_raster` is unpacked here exactly as the browser
worker (`static/analyses-worker.js`) does, so a server/client format drift
shows up as a decode mismatch.

SANDBOX NOTE
------------
This dev box sits behind a TLS-MITM proxy whose CA is not in certifi, so a
normal HTTPS request to NOAA fails cert verification. We relax verification
**here only** (`_session.verify = False`) so the diagnostic can exercise the
live endpoints. Production code (`src/LayerGeneration.py`) keeps verification
ON — do not copy this flag into it. On a normally-trusted network this flag
is a no-op.

Usage:
    python tools/raster_diagnostics.py                 # all sources, default grid
    python tools/raster_diagnostics.py --source dem-all --source bag-bathymetry
    python tools/raster_diagnostics.py --zooms 11 13 15 --json out.json
"""

import argparse
import json
import math
import os
import statistics
import struct
import sys
import tempfile
import time
import warnings

import numpy as np

# Windows consoles default to cp1252; the report uses a few box-drawing
# glyphs. Force UTF-8 so the rollup doesn't crash on encode.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make `src` importable when run from the repo root or tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import LayerGeneration as LG          # noqa: E402
from src.data_sources import SOURCES, get_source  # noqa: E402
from src.LayerGeneration import UNKNOWN_SOURCE, fetch_tile_raster  # noqa: E402


# Representative Florida Keys probe points (lat, lon). Chosen to exercise
# nearshore reef, mid-channel, offshore deep wall, and the Tortugas.
REGIONS = {
    "key-west-reef":   (24.45, -81.80),
    "sombrero-reef":   (24.62, -81.11),
    "alligator-reef":  (24.85, -80.62),
    "molasses-reef":   (25.01, -80.38),
    "fl-straits-deep": (24.10, -80.40),
    "dry-tortugas":    (24.63, -82.87),
}

DEFAULT_ZOOMS = [9, 11, 13, 15]


def lonlat_to_tile(lon, lat, z):
    """Standard slippy-map tile (x, y) for a lon/lat at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    return x, y


def _inner_view(arr, buffer_px):
    """Crop the buffer margin so stats match what the user actually sees."""
    if buffer_px > 0 and arr.shape[0] > 2 * buffer_px:
        return arr[buffer_px:-buffer_px, buffer_px:-buffer_px]
    return arr


def _verify_wire_format(arr, cellsize_m, buffer_px):
    """Pack the header the way app.serve_raster does and unpack it the way
    the browser worker does. Returns None if consistent, else an error str."""
    h, w = arr.shape
    arr32 = np.ascontiguousarray(arr, dtype=np.float32)
    header = struct.pack('<IIfI', w, h, float(cellsize_m), int(buffer_px))
    body = arr32.tobytes(order='C')
    blob = header + body
    # Unpack exactly like analyses-worker.js fetchRaster().
    rw = struct.unpack('<I', blob[0:4])[0]
    rh = struct.unpack('<I', blob[4:8])[0]
    rcell = struct.unpack('<f', blob[8:12])[0]
    rbuf = struct.unpack('<I', blob[12:16])[0]
    expect_body = rw * rh * 4
    if rw != w or rh != h:
        return f"dim mismatch {rw}x{rh} != {w}x{h}"
    if rbuf != buffer_px:
        return f"buffer mismatch {rbuf} != {buffer_px}"
    if not math.isclose(rcell, float(cellsize_m), rel_tol=1e-5):
        return f"cellsize mismatch {rcell} != {cellsize_m}"
    if len(blob) - 16 != expect_body:
        return f"body length {len(blob)-16} != {expect_body}"
    return None


def probe(source_id, region, lat, lon, z, resolution):
    """One (source, region, zoom) probe. Returns a result dict."""
    src = get_source(source_id)
    out = {
        "source": source_id, "region": region, "z": z,
        "outcome": None, "cold_ms": None, "warm_ms": None,
        "nodata_frac": None, "depth_min": None, "depth_med": None,
        "depth_max": None, "cellsize_m": None, "wire": None, "note": "",
    }
    if src is None:
        out["outcome"] = "UNKNOWN"
        return out
    if z < src.min_zoom or z > src.max_zoom:
        out["outcome"] = "SKIP"
        out["note"] = f"z{z} outside [{src.min_zoom},{src.max_zoom}]"
        return out

    x, y = lonlat_to_tile(lon, lat, z)

    with tempfile.TemporaryDirectory() as tmp:
        # Cold: fresh disk cache → forces a network fetch.
        t0 = time.perf_counter()
        res_cold = fetch_tile_raster(source_id, resolution, z, x, y,
                                     raster_root=tmp)
        cold_ms = (time.perf_counter() - t0) * 1000.0
        out["cold_ms"] = round(cold_ms, 1)

        if res_cold is UNKNOWN_SOURCE:
            out["outcome"] = "UNKNOWN"
            return out
        if res_cold is None:
            out["outcome"] = "FAIL"
            out["note"] = "fetch/decode returned None (503-class)"
            return out

        arr, cellsize_m, buffer_px = res_cold

        # Warm: same tile, same temp dir → disk-cache hit (if it was data).
        t1 = time.perf_counter()
        res_warm = fetch_tile_raster(source_id, resolution, z, x, y,
                                     raster_root=tmp)
        out["warm_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
        # An all-nodata tile is intentionally NOT persisted, so its "warm"
        # call still hits the network — flag that so the timing isn't misread.
        warm_was_disk = res_warm is not None and not np.isnan(res_warm[0]).all()

    inner = _inner_view(arr, buffer_px)
    total = inner.size
    nod = int(np.isnan(inner).sum())
    out["nodata_frac"] = round(nod / total, 4) if total else 1.0
    out["cellsize_m"] = round(float(cellsize_m), 3)
    out["wire"] = _verify_wire_format(arr, cellsize_m, buffer_px) or "ok"

    if nod == total:
        out["outcome"] = "EMPTY"
        if not warm_was_disk:
            out["note"] = "all-nodata, not persisted (retryable) — correct"
        return out

    valid = inner[~np.isnan(inner)]
    out["depth_min"] = round(float(np.min(valid)), 1)
    out["depth_med"] = round(float(np.median(valid)), 1)
    out["depth_max"] = round(float(np.max(valid)), 1)
    out["outcome"] = "OK"
    if not warm_was_disk:
        out["note"] = "WARN: data tile not disk-persisted"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", dest="sources",
                    help="source id (repeatable); default = all registered")
    ap.add_argument("--region", action="append", dest="regions",
                    help="region key (repeatable); default = all")
    ap.add_argument("--zooms", type=int, nargs="+", default=DEFAULT_ZOOMS)
    ap.add_argument("--resolution", type=int, default=256)
    ap.add_argument("--insecure", action="store_true", default=True,
                    help="disable TLS verify (sandbox proxy workaround; on by default)")
    ap.add_argument("--json", help="also write raw results to this JSON file")
    args = ap.parse_args()

    if args.insecure:
        warnings.filterwarnings("ignore")
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
        # Relax verification on the production session — SANDBOX ONLY.
        LG._session.verify = False

    source_ids = args.sources or [s.id for s in SOURCES]
    region_keys = args.regions or list(REGIONS.keys())

    results = []
    print(f"\nFishFinder raster diagnostics — {len(source_ids)} sources × "
          f"{len(region_keys)} regions × {len(args.zooms)} zooms\n")
    hdr = (f"{'source':<16} {'region':<16} {'z':>3} {'outcome':<8} "
           f"{'cold':>7} {'warm':>7} {'nodata':>7} {'depth(m) min/med/max':<24} "
           f"{'cell_m':>7} wire")
    print(hdr)
    print("-" * len(hdr))

    for sid in source_ids:
        for rk in region_keys:
            lat, lon = REGIONS[rk]
            for z in args.zooms:
                r = probe(sid, rk, lat, lon, z, args.resolution)
                results.append(r)
                depth = ("-" if r["depth_min"] is None
                         else f"{r['depth_min']}/{r['depth_med']}/{r['depth_max']}")
                cold = "-" if r["cold_ms"] is None else f"{r['cold_ms']:.0f}ms"
                warm = "-" if r["warm_ms"] is None else f"{r['warm_ms']:.0f}ms"
                nod = "-" if r["nodata_frac"] is None else f"{r['nodata_frac']*100:.0f}%"
                cell = "-" if r["cellsize_m"] is None else f"{r['cellsize_m']:.1f}"
                wire = r["wire"] or "-"
                print(f"{sid:<16} {rk:<16} {z:>3} {r['outcome']:<8} "
                      f"{cold:>7} {warm:>7} {nod:>7} {depth:<24} {cell:>7} {wire}"
                      + (f"  [{r['note']}]" if r["note"] else ""))

    # Per-source rollup.
    print("\n── per-source rollup ──")
    for sid in source_ids:
        rs = [r for r in results if r["source"] == sid]
        n_ok = sum(1 for r in rs if r["outcome"] == "OK")
        n_empty = sum(1 for r in rs if r["outcome"] == "EMPTY")
        n_fail = sum(1 for r in rs if r["outcome"] == "FAIL")
        n_skip = sum(1 for r in rs if r["outcome"] == "SKIP")
        oks = [r["cold_ms"] for r in rs if r["outcome"] == "OK" and r["cold_ms"]]
        med_cold = round(statistics.median(oks), 0) if oks else "-"
        wire_bad = [r["wire"] for r in rs if r["wire"] not in (None, "ok")]
        flag = "  <-- ALL PROBES FAILED" if (n_ok == 0 and n_empty == 0 and n_fail) else ""
        print(f"{sid:<16} OK={n_ok:<3} EMPTY={n_empty:<3} FAIL={n_fail:<3} "
              f"SKIP={n_skip:<3} med_cold={med_cold}ms"
              + (f" WIRE_ERRORS={wire_bad}" if wire_bad else "") + flag)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
