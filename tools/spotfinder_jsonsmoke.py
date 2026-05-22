"""
End-to-end JSON-serialization smoke test for the new spotfinder output.

Drives the pipeline directly against a synthetic raster (no NOAA fetch),
then runs each component of the result through `json.dumps(allow_nan=False)`
so we'd catch any stray NaN/Inf that would crash the NDJSON streaming on
real data. Also exercises the centerline + composite emission paths.
"""

import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spotfinder import (   # noqa: E402
    CLASS_NAMES, DEFAULT_PARAMS, LINEAR_CLASSES,
    _classify, _composite_pass, _compute_centerline_px,
    _derivative_stack, _extract_regions, _nms_regions,
    _paint_score_raster, _pixel_to_lat_lng, _preprocess,
    _px_polyline_to_latlng, _region_polygon_px, _render_heatmap_png,
    _safe_finite, _score_regions,
)


def build_test_raster():
    h, w = 240, 240
    rng = np.random.RandomState(7)
    elev = np.full((h, w), -28.0, dtype=np.float32)
    elev += rng.normal(scale=0.05, size=(h, w)).astype(np.float32)
    # Centered pinnacle.
    ys = np.arange(h)[:, None]; xs = np.arange(w)[None, :]
    elev += 3.5 * np.exp(-((ys - h // 2) ** 2 + (xs - w // 2) ** 2) / (2.0 * 6.0 ** 2))
    # Long diagonal ridge.
    for k in range(80):
        cy = 40 + k; cx = 40 + k
        elev += 2.0 * np.exp(-(((ys - cy) ** 2 + (xs - cx) ** 2)) / (2.0 * 3.0 ** 2))
    # A nearby second peak so a composite has something to cluster.
    elev += 2.5 * np.exp(-((ys - 130) ** 2 + (xs - 130) ** 2) / (2.0 * 6.0 ** 2))
    return elev


def fake_search_area():
    # 720 m × 720 m at the centre of the Keys.
    center = {"lat": 24.7, "lng": -81.1}
    half = 360.0 / 111_320.0
    half_lng = half / math.cos(math.radians(center["lat"]))
    bbox = {
        "north": center["lat"] + half,
        "south": center["lat"] - half,
        "east":  center["lng"] + half_lng,
        "west":  center["lng"] - half_lng,
    }
    corners = [
        {"lat": bbox["north"], "lng": bbox["west"]},
        {"lat": bbox["north"], "lng": bbox["east"]},
        {"lat": bbox["south"], "lng": bbox["east"]},
        {"lat": bbox["south"], "lng": bbox["west"]},
    ]
    return {
        "corners": corners,
        "center":  center,
        "width_m":  720.0,
        "height_m": 720.0,
        "rotation_deg": 0.0,
        "bbox":     bbox,
    }


def main():
    elev = build_test_raster()
    valid = np.ones_like(elev, dtype=bool)
    cell = 3.0

    params = dict(DEFAULT_PARAMS)
    elev_p, valid_p = _preprocess(elev, valid, params)
    stack = _derivative_stack(elev_p, valid_p, cell, cell, params)
    class_map = _classify(stack, valid_p, params)
    regions = _extract_regions(class_map, valid_p, params)
    regions = _score_regions(regions, elev_p, valid_p, stack, cell, cell, params)
    regions = [r for r in regions if r.get("score", 0.0) >= params["score_threshold"]]
    regions = sorted(regions, key=lambda r: -r["score"])[:params["max_regions"]]
    regions = _nms_regions(regions, params)
    composites = _composite_pass(regions, cell, cell, params)

    for r in regions:
        r["centerline_px"] = (_compute_centerline_px(r["ys"], r["xs"])
                              if r["class_id"] in LINEAR_CLASSES else None)

    area = fake_search_area()
    score_raster = _paint_score_raster(regions, elev_p.shape)
    heatmap_url = _render_heatmap_png(score_raster, valid_p, area)
    assert heatmap_url.startswith("data:image/png;base64,"), "heatmap PNG malformed"

    spots_out = []
    regions_out = []
    for r in regions:
        cy = float(r["ys"].mean()); cx = float(r["xs"].mean())
        lat, lng = _pixel_to_lat_lng(cy, cx, elev_p.shape, area)
        polygon = _px_polyline_to_latlng(_region_polygon_px(r), elev_p.shape, area)
        cl_px = r["centerline_px"]
        cl = _px_polyline_to_latlng(cl_px, elev_p.shape, area) if cl_px else None
        ymin, xmin, yend, xend = r["bbox_px"]
        bb_lat0, bb_lng0 = _pixel_to_lat_lng(ymin, xmin, elev_p.shape, area)
        bb_lat1, bb_lng1 = _pixel_to_lat_lng(yend - 1, xend - 1, elev_p.shape, area)
        regions_out.append({
            "id":             r["id"],
            "class":          r["class"],
            "score":          float(r["score"]),
            "centroid":       {"lat": float(lat), "lng": float(lng)},
            "polygon":        polygon,
            "centerline":     cl,
            "bbox": {
                "north": max(bb_lat0, bb_lat1),
                "south": min(bb_lat0, bb_lat1),
                "east":  max(bb_lng0, bb_lng1),
                "west":  min(bb_lng0, bb_lng1),
            },
            "metrics": {
                "relief_m":         _safe_finite(r.get("relief_m")),
                "local_percentile": _safe_finite(r.get("local_percentile")),
                "mean_depth_m":     _safe_finite(r.get("mean_depth_m")),
                "shape_score":      _safe_finite(r.get("shape_score")),
                "depth_fit":        _safe_finite(r.get("depth_fit")),
                "isolation_m":      None if r.get("isolation_m") is None
                                          else _safe_finite(r.get("isolation_m")),
                "area_cells":       int(r["area_cells"]),
            },
            "secondary_tags": list(r.get("secondary_tags", [])),
            "confidence":     _safe_finite(r.get("confidence", 0.5)),
        })
        spots_out.append({
            "id":      r["id"],
            "lat":     float(lat),
            "lng":     float(lng),
            "depth_m": _safe_finite(-r.get("mean_depth_m", 0.0)),
            "score":   float(r["score"]),
            "features": {
                "class": r["class"],
                "secondary_tags": list(r.get("secondary_tags", [])),
            },
        })
    composites_out = []
    for c in composites:
        polygon = [
            {"lat": float(lat), "lng": float(lng)}
            for (lat, lng) in (_pixel_to_lat_lng(row, col, elev_p.shape, area)
                               for (row, col) in c["hull_px"])
        ]
        composites_out.append({
            "id":         c["id"],
            "member_ids": list(c["member_ids"]),
            "score":      float(c["score"]),
            "polygon":    polygon,
        })

    result = {
        "run_id":          "sf-" + uuid.uuid4().hex[:12],
        "timestamp":       datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "search_area":     area,
        "params":          params,
        "heatmap_png_url": heatmap_url,
        "heatmap_corners": [{"lat": c["lat"], "lng": c["lng"]} for c in area["corners"]],
        "spots":           spots_out,
        "regions":         regions_out,
        "composites":      composites_out,
        "manifest": {
            "data_source":  "synthetic",
            "resolution_m": round(cell, 2),
            "cell_count":   int(valid_p.sum()),
            "runtime_ms":   1,
            "rotation_deg": 0.0,
        },
        "bbox":           dict(area["bbox"]),
        "heatmap_bounds": dict(area["bbox"]),
    }

    # The Flask layer uses allow_nan=False — any NaN/Inf would crash there.
    encoded = json.dumps(result, allow_nan=False)
    print(f"OK: result encodes to {len(encoded):,} bytes JSON")
    print(f"OK: {len(regions_out)} regions, {len(composites_out)} composites, "
          f"{len(spots_out)} spots")
    classes = {}
    for r in regions_out:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    print(f"OK: class breakdown {classes}")
    print(f"OK: centerlines on linear regions: "
          f"{sum(1 for r in regions_out if r['centerline']) }")


if __name__ == "__main__":
    main()
