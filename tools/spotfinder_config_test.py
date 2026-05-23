"""
Unit tests for the configurable-algorithm logic in src.spotfinder:

  - resolve_config(): environment → param overrides, structure-type
    validation + class mapping, source passthrough, defaults + fallbacks.
  - _extract_regions(): the structure-type filter (only selected classes
    are extracted).

Pure-Python — no NOAA fetch, no network. Runs two ways:

    python tools/spotfinder_config_test.py     # standalone, exit 1 on fail
    pytest tools/spotfinder_config_test.py      # if pytest is installed

(The repo has no pytest harness today; the standalone runner is the one
the build script invokes.)
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spotfinder import (   # noqa: E402
    DEFAULT_ENVIRONMENT, DEFAULT_PARAMS, DEFAULT_STRUCTURE_TYPES,
    DEFAULT_SIZE_RANGES, SIZE_FT_ABS_MAX, SIZE_MEASURE,
    ENVIRONMENT_MODES, STRUCTURE_TYPES,
    CLASS_CHANNEL, CLASS_LEDGE, CLASS_PINNACLE,
    _extract_regions, _filter_regions_by_size, _region_size_ft,
    resolve_config, resolve_size_ranges,
)


# ── resolve_config: environment ───────────────────────────────────────

def test_environment_overrides_applied():
    cfg = resolve_config({"config": {"environment": "reef"}})
    assert cfg["environment"] == "reef"
    for key, val in ENVIRONMENT_MODES["reef"]["param_overrides"].items():
        assert cfg["params"][key] == val, f"{key} not applied"


def test_reef_is_stricter_than_flat():
    reef = resolve_config({"config": {"environment": "reef"}})["params"]
    flat = resolve_config({"config": {"environment": "flat"}})["params"]
    # The whole point of the modes: reef demands more prominence and a
    # higher score floor than flat. If a future tweak inverts these, this
    # test fails loudly.
    assert reef["tpi_small_threshold_m"] > flat["tpi_small_threshold_m"]
    assert reef["tpi_large_threshold_m"] > flat["tpi_large_threshold_m"]
    assert reef["score_threshold"] > flat["score_threshold"]
    assert reef["relief_scale_m"] > flat["relief_scale_m"]


def test_unknown_environment_falls_back():
    cfg = resolve_config({"config": {"environment": "atlantis"}})
    assert cfg["environment"] == DEFAULT_ENVIRONMENT


def test_explicit_params_override_environment():
    # The low-level `params` channel still wins over a mode's override, so a
    # future tuning UI / power user can pin an individual knob.
    cfg = resolve_config({
        "config": {"environment": "reef"},
        "params": {"score_threshold": 0.99},
    })
    assert cfg["params"]["score_threshold"] == 0.99


# ── resolve_config: structure types ───────────────────────────────────

def test_structure_types_filtered_and_mapped():
    cfg = resolve_config({"config": {"structure_types": ["ledge", "bogus"]}})
    assert cfg["structure_types"] == ["ledge"]
    assert cfg["selected_classes"] == set(STRUCTURE_TYPES["ledge"]["classes"])


def test_empty_structure_types_defaults_to_all():
    cfg = resolve_config({"config": {"structure_types": []}})
    assert set(cfg["structure_types"]) == set(DEFAULT_STRUCTURE_TYPES)


def test_mound_maps_to_ridge_class():
    cfg = resolve_config({"config": {"structure_types": ["mound"]}})
    assert cfg["selected_classes"] == set(STRUCTURE_TYPES["mound"]["classes"])


# ── resolve_config: defaults + source ─────────────────────────────────

def test_default_config_is_reef_all_types():
    cfg = resolve_config({})
    assert cfg["environment"] == DEFAULT_ENVIRONMENT == "reef"
    assert set(cfg["structure_types"]) == set(DEFAULT_STRUCTURE_TYPES)
    assert cfg["source_id"] is None


def test_source_passthrough():
    assert resolve_config(
        {"config": {"source": "bag-bathymetry"}})["source_id"] == "bag-bathymetry"
    assert resolve_config({"config": {"source": ""}})["source_id"] is None
    assert resolve_config({"config": {}})["source_id"] is None


def test_malformed_payload_is_safe():
    # A non-dict payload must not blow up — defaults all the way down.
    cfg = resolve_config(None)
    assert cfg["environment"] == DEFAULT_ENVIRONMENT
    assert set(cfg["structure_types"]) == set(DEFAULT_STRUCTURE_TYPES)


# ── _extract_regions: structure-type filter ───────────────────────────

def test_extract_regions_respects_selected_classes():
    # Paint a class map: a pinnacle blob (top-left) and a ledge blob
    # (bottom-right), both well above min_region_area_cells.
    h = w = 60
    class_map = np.zeros((h, w), dtype=np.int8)
    class_map[5:20, 5:20]   = CLASS_PINNACLE
    class_map[35:55, 35:55] = CLASS_LEDGE
    valid = np.ones((h, w), dtype=bool)
    params = dict(DEFAULT_PARAMS)

    only_ledge = _extract_regions(class_map, valid, params, {CLASS_LEDGE})
    assert only_ledge, "expected at least one ledge region"
    assert all(r["class_id"] == CLASS_LEDGE for r in only_ledge)

    both = _extract_regions(class_map, valid, params,
                            {CLASS_LEDGE, CLASS_PINNACLE})
    assert {r["class_id"] for r in both} == {CLASS_LEDGE, CLASS_PINNACLE}

    # None => all surfaced classes (back-compat for old callers / tests).
    all_default = _extract_regions(class_map, valid, params, None)
    assert {r["class_id"] for r in all_default} == {CLASS_PINNACLE, CLASS_LEDGE}


# ── run_spotfinder: end-to-end wiring (stubbed I/O) ───────────────────

def _fake_area():
    # 720 m square at the centre of the Keys, axis-aligned, so the rotated
    # mask covers the whole stubbed raster.
    import math
    center = {"lat": 24.7, "lng": -81.1}
    half = 360.0 / 111_320.0
    half_lng = half / math.cos(math.radians(center["lat"]))
    bbox = {"north": center["lat"] + half, "south": center["lat"] - half,
            "east": center["lng"] + half_lng, "west": center["lng"] - half_lng}
    corners = [{"lat": bbox["north"], "lng": bbox["west"]},
               {"lat": bbox["north"], "lng": bbox["east"]},
               {"lat": bbox["south"], "lng": bbox["east"]},
               {"lat": bbox["south"], "lng": bbox["west"]}]
    return {"corners": corners, "center": center,
            "width_m": 720.0, "height_m": 720.0,
            "rotation_deg": 0.0, "bbox": bbox}


def _fake_raster():
    # Deep flat with a sharp central pinnacle and a deep pit (hole).
    h = w = 240
    ys = np.arange(h)[:, None]
    xs = np.arange(w)[None, :]
    elev = np.full((h, w), -28.0, dtype=np.float32)
    elev += 4.0 * np.exp(-((ys - 70) ** 2 + (xs - 70) ** 2) / (2.0 * 6.0 ** 2))
    elev -= 4.0 * np.exp(-((ys - 170) ** 2 + (xs - 170) ** 2) / (2.0 * 6.0 ** 2))
    return elev


class _FakeSource:
    id = "bag-bathymetry"
    display_name = "Fake BAG"


def _run_end_to_end(config):
    """Drive run_spotfinder with NOAA stubbed out. Returns the result dict."""
    import src.spotfinder as sf

    saved_resolve = sf._resolve_source
    saved_fetch = sf._fetch_full_raster
    try:
        sf._resolve_source = lambda area, emit, pref=None: (_FakeSource(), 3.0, 0.99)
        sf._fetch_full_raster = lambda source, area, res: (_fake_raster(), 3.0, 3.0)
        events = list(sf.run_spotfinder({"search_area": _fake_area(),
                                         "config": config}))
    finally:
        sf._resolve_source = saved_resolve
        sf._fetch_full_raster = saved_fetch

    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"unexpected error event: {errors}"
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1, "expected exactly one result event"
    return results[0]["result"]


def test_run_spotfinder_echoes_config():
    res = _run_end_to_end({"environment": "flat",
                           "structure_types": ["pinnacle", "hole"],
                           "source": "bag-bathymetry"})
    assert res["config"]["environment"] == "flat"
    assert set(res["config"]["structure_types"]) == {"pinnacle", "hole"}
    # SOURCE reflects what was actually used (the stubbed source id).
    assert res["config"]["source"] == "bag-bathymetry"
    assert res["manifest"]["source_id"] == "bag-bathymetry"
    assert res["manifest"]["environment"] == "flat"


def test_run_spotfinder_structure_filter_excludes_unselected():
    # Ask for pinnacles only — the synthetic pit must NOT appear as a hole.
    res = _run_end_to_end({"environment": "flat",
                           "structure_types": ["pinnacle"]})
    classes = {r["class"] for r in res["regions"]}
    assert "hole" not in classes, f"hole leaked through filter: {classes}"


# ── resolve_config: size ranges ───────────────────────────────────────

def test_size_ranges_default_is_full_per_type_map():
    sr = resolve_config({})["size_ranges"]
    assert set(sr.keys()) == set(DEFAULT_SIZE_RANGES.keys())
    for k, v in DEFAULT_SIZE_RANGES.items():
        assert sr[k]["min_ft"] == v["min_ft"]
        assert sr[k]["max_ft"] == v["max_ft"]


def test_size_ranges_partial_override_keeps_other_defaults():
    sr = resolve_config(
        {"config": {"size_ranges": {"pinnacle": {"min_ft": 8, "max_ft": 40}}}}
    )["size_ranges"]
    assert sr["pinnacle"] == {"min_ft": 8.0, "max_ft": 40.0}
    # An untouched type still gets its default.
    assert sr["hole"]["min_ft"] == DEFAULT_SIZE_RANGES["hole"]["min_ft"]
    assert sr["hole"]["max_ft"] == DEFAULT_SIZE_RANGES["hole"]["max_ft"]


def test_size_ranges_inverted_min_max_swapped():
    sr = resolve_config(
        {"config": {"size_ranges": {"hole": {"min_ft": 200, "max_ft": 50}}}}
    )["size_ranges"]
    assert sr["hole"]["min_ft"] == 50.0
    assert sr["hole"]["max_ft"] == 200.0


def test_size_ranges_garbage_value_falls_back():
    sr = resolve_config(
        {"config": {"size_ranges": {"ledge": {"min_ft": "abc"}}}}
    )["size_ranges"]
    assert sr["ledge"]["min_ft"] == DEFAULT_SIZE_RANGES["ledge"]["min_ft"]
    assert sr["ledge"]["max_ft"] == DEFAULT_SIZE_RANGES["ledge"]["max_ft"]


def test_size_ranges_clamped_to_absolute_bounds():
    sr = resolve_config(
        {"config": {"size_ranges": {"saddle": {"min_ft": -10, "max_ft": 99999}}}}
    )["size_ranges"]
    assert sr["saddle"]["min_ft"] == 0.0
    assert sr["saddle"]["max_ft"] == SIZE_FT_ABS_MAX


# ── _filter_regions_by_size: the post-classification size gate ─────────

def _block_region(class_id, h_cells, w_cells):
    """A solid rectangular footprint of (h_cells × w_cells) pixels at (0,0)."""
    ys, xs = np.mgrid[0:h_cells, 0:w_cells]
    return {
        "class_id": class_id,
        "ys": ys.ravel(),
        "xs": xs.ravel(),
        "bbox_px": (0, 0, h_cells, w_cells),
    }


def test_filter_drops_oversize_keeps_in_range():
    ranges = resolve_size_ranges({})   # defaults: pinnacle 5–60 ft
    small = _block_region(CLASS_PINNACLE, 10, 10)   # ~42 ft longest extent
    big   = _block_region(CLASS_PINNACLE, 25, 25)   # ~111 ft longest extent
    kept = _filter_regions_by_size([small, big], ranges, 1.0, 1.0)
    assert any(r is small for r in kept), "in-range pinnacle should survive"
    assert not any(r is big for r in kept), "oversize pinnacle should be dropped"
    # Every region is annotated, kept or not.
    assert small["size_measure"] == "longest"
    assert big["size_ft"] > ranges["pinnacle"]["max_ft"]


def test_filter_uses_width_not_length_for_channel():
    ranges = resolve_size_ranges({})   # channel default 10–100 ft
    strip = _block_region(CLASS_CHANNEL, 120, 10)   # long + thin
    longest = _region_size_ft(strip, 1.0, 1.0, "longest")
    width   = _region_size_ft(strip, 1.0, 1.0, "width")
    assert width < longest, "width must be the short cross-feature dimension"
    # Length blows past the max, but width sits inside it — so a channel that
    # would be dropped under 'longest' is kept under 'width'.
    assert longest > ranges["channel"]["max_ft"]
    assert ranges["channel"]["min_ft"] <= width <= ranges["channel"]["max_ft"]
    kept = _filter_regions_by_size([strip], ranges, 1.0, 1.0)
    assert any(r is strip for r in kept)
    assert strip["size_measure"] == "width"
    assert SIZE_MEASURE["channel"] == "width"


# ── run_spotfinder: size filter end-to-end ────────────────────────────

def test_run_spotfinder_echoes_size_ranges():
    res = _run_end_to_end({
        "structure_types": ["pinnacle", "hole"],
        "size_ranges": {"pinnacle": {"min_ft": 3, "max_ft": 800}},
    })
    sr = res["config"]["size_ranges"]
    assert set(sr.keys()) == set(DEFAULT_SIZE_RANGES.keys())
    assert sr["pinnacle"] == {"min_ft": 3.0, "max_ft": 800.0}


def test_run_spotfinder_size_filter_annotates_and_filters():
    # Wide-open ranges: the synthetic pinnacle + pit survive and each region
    # carries the size it was measured at.
    wide = _run_end_to_end({
        "environment": "flat",
        "structure_types": ["pinnacle", "hole"],
        "size_ranges": {"pinnacle": {"min_ft": 0, "max_ft": 5000},
                        "hole":     {"min_ft": 0, "max_ft": 5000}},
    })
    assert wide["regions"], "wide-open size range should keep regions"
    for r in wide["regions"]:
        assert r["metrics"]["size_ft"] > 0
        assert r["metrics"]["size_measure"] in ("longest", "width")

    # An impossibly tight max drops everything — the gate is real.
    tight = _run_end_to_end({
        "environment": "flat",
        "structure_types": ["pinnacle", "hole"],
        "size_ranges": {"pinnacle": {"min_ft": 0, "max_ft": 1},
                        "hole":     {"min_ft": 0, "max_ft": 1}},
    })
    assert tight["regions"] == [], "a 1 ft max should drop all regions"


def _run_all():
    fns = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as exc:   # noqa: BLE001 — report, don't crash the run
            failed += 1
            print(f"  FAIL {name}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
