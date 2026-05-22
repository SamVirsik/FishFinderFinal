"""
Synthetic smoke test for src.spotfinder.

Runs the algorithm against three hand-built bathymetry rasters:

  1. Deep flat + a single small hump  — expect 1 pinnacle/ridge region.
  2. Deep flat + a long ledge         — expect 1 ledge with a centerline.
  3. Reef-like dense Gaussian bumps   — expect the strongest bumps to win
     (local-percentile suppresses the weaker ones in a busy field).

Each scenario prints the region classes + scores so a human can eyeball
that the algorithm is behaving sanely. No assertions: this is a dev
smoke test, not a CI check.
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spotfinder import (    # noqa: E402
    DEFAULT_PARAMS,
    _preprocess, _derivative_stack, _classify, _extract_regions,
    _score_regions, _nms_regions, _composite_pass,
)


def gaussian_2d(h, w, cy, cx, amp, sigma):
    ys = np.arange(h)[:, None]
    xs = np.arange(w)[None, :]
    return amp * np.exp(-((ys - cy) ** 2 + (xs - cx) ** 2) / (2.0 * sigma ** 2))


def scenario(label, build_elev, params_override=None):
    h, w = 200, 200
    cell = 3.0   # metres per cell
    elev = build_elev(h, w).astype(np.float32)
    valid = np.ones((h, w), dtype=bool)

    params = dict(DEFAULT_PARAMS)
    if params_override:
        params.update(params_override)

    elev_p, valid_p = _preprocess(elev, valid, params)
    stack = _derivative_stack(elev_p, valid_p, cell, cell, params)
    class_map = _classify(stack, valid_p, params)
    regions = _extract_regions(class_map, valid_p, params)
    regions = _score_regions(regions, elev_p, valid_p, stack, cell, cell, params)
    regions = [r for r in regions if r.get("score", 0.0) >= params["score_threshold"]]
    regions = sorted(regions, key=lambda r: -r["score"])[:params["max_regions"]]
    regions = _nms_regions(regions, params)
    composites = _composite_pass(regions, cell, cell, params)

    print(f"\n=== {label} ===")
    print(f"  cells: {h}x{w}, elev range [{elev.min():.2f}, {elev.max():.2f}]")
    print(f"  regions surfaced: {len(regions)}, composites: {len(composites)}")
    for r in regions[:12]:
        ymin, xmin, yend, xend = r["bbox_px"]
        print(f"    {r['class']:9s} score={r['score']:.3f} "
              f"relief={r.get('relief_m', 0):.2f}m "
              f"local_pct={r.get('local_percentile', 0):.2f} "
              f"shape={r.get('shape_score', 0):.2f} "
              f"area={r['area_cells']:4d}  bbox=({ymin},{xmin},{yend},{xend}) "
              f"tags={r.get('secondary_tags', [])}")


def build_flat_with_hump(h, w):
    elev = np.full((h, w), -30.0, dtype=np.float32)
    elev += np.random.RandomState(0).normal(scale=0.05, size=(h, w)).astype(np.float32)
    elev += gaussian_2d(h, w, h // 2, w // 2, amp=3.0, sigma=5.0)
    return elev


def build_flat_with_ledge(h, w):
    # A flat shelf at -20 m sloping down to a deeper basin at -50 m through
    # a narrow ledge. Ledge is at column ~100.
    elev = np.zeros((h, w), dtype=np.float32)
    for x in range(w):
        if x < 95:
            elev[:, x] = -20.0
        elif x > 105:
            elev[:, x] = -50.0
        else:
            t = (x - 95) / 10.0
            elev[:, x] = -20.0 - 30.0 * t
    elev += np.random.RandomState(0).normal(scale=0.05, size=(h, w)).astype(np.float32)
    return elev


def build_reef_field(h, w):
    rng = np.random.RandomState(42)
    elev = np.full((h, w), -25.0, dtype=np.float32)
    # 12 random bumps of varying amplitude.
    for _ in range(12):
        cy = rng.randint(20, h - 20)
        cx = rng.randint(20, w - 20)
        amp = float(rng.uniform(1.0, 4.5))
        sigma = float(rng.uniform(4.0, 8.0))
        elev += gaussian_2d(h, w, cy, cx, amp=amp, sigma=sigma)
    elev += rng.normal(scale=0.05, size=(h, w)).astype(np.float32)
    return elev


if __name__ == "__main__":
    scenario("Flat + single hump", build_flat_with_hump)
    scenario("Flat + long ledge",  build_flat_with_ledge)
    scenario("Reef-like field",    build_reef_field)
