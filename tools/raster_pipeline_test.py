"""
Offline tests for the server-side raster pipeline.

No network: every test monkeypatches `src.LayerGeneration._fetch_raster_bytes`
(or the lower-level `_session.get` for the retry tests) so the fetch/decode/
cache/format logic is exercised deterministically. These cover exactly the
invariants the live diagnostics can't pin down repeatably:

  - cold fetch decodes + disk-caches a data tile
  - warm fetch reads from disk without re-hitting the network
  - all-nodata tiles are NOT persisted and stay retryable (the "grey box that
    never recovers" bug)
  - undecodable / RGB bodies are rejected and NOT persisted
  - source-specific nodata sentinels round-trip to NaN
  - the binary wire format the server packs is the one the worker unpacks
  - unknown source ids are rejected (no silent dem-tiles fallback / cache mislabel)
  - a broad zoom-out that transiently returns empty does not poison a later
    local high-zoom view of the same source
  - transient NOAA failures (5xx / conn error / 429) are retried; deterministic
    failures (4xx, 200-non-image) are not

Run:  python -m pytest tools/raster_pipeline_test.py -v
"""

import io
import math
import os
import struct
import sys
import tempfile

import numpy as np
import pytest
import tifffile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import LayerGeneration as LG
from src.LayerGeneration import (
    UNKNOWN_SOURCE,
    fetch_tile_raster,
    _decode_raster,
    _fetch_raster_bytes,
)
from src.data_sources import get_source


# ─── TIFF fixture builders (stand in for NOAA exportImage responses) ──────

def tiff_bytes(arr):
    """Encode a 2-D float32 array as a TIFF, as NOAA would return."""
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.asarray(arr, dtype=np.float32))
    return buf.getvalue()


def depth_grid(size, fill=-20.0):
    """A realistic all-data depth tile (negative metres)."""
    g = np.full((size, size), fill, dtype=np.float32)
    # add some variation so it isn't a constant
    g += np.linspace(0, -5, size, dtype=np.float32)[None, :]
    return g


def nodata_grid(size, sentinel):
    return np.full((size, size), sentinel, dtype=np.float32)


def rgb_bytes(size):
    """A 3-band RGB preview — what a mis-configured ImageServer returns."""
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.zeros((size, size, 3), dtype=np.uint8))
    return buf.getvalue()


SIZE = LG.OUTPUT_TILE_PX + 2 * LG.BUFFER_PX   # the fetch grid size the code asks for


# ─── Helpers ──────────────────────────────────────────────────────────────

def patch_fetch(monkeypatch, producer):
    """Replace the network call with a function of (source, bbox, size)."""
    calls = {"n": 0}

    def fake(source, bbox, size_px):
        calls["n"] += 1
        return producer(source, bbox, size_px)

    monkeypatch.setattr(LG, "_fetch_raster_bytes", fake)
    return calls


# ─── Decode-level invariants ──────────────────────────────────────────────

def test_nodata_sentinel_roundtrips_to_nan():
    src = get_source("dem-all")          # nodata = -9999
    arr = _decode_raster(tiff_bytes(nodata_grid(SIZE, -9999)), SIZE, src)
    assert arr is not None
    assert np.isnan(arr).all(), "sentinel -9999 must become NaN, not deep water"


def test_large_magnitude_sentinel_masked_without_explicit_nodata():
    src = get_source("dem-all")
    g = depth_grid(SIZE)
    g[0, 0] = 1e6                         # generic large sentinel
    arr = _decode_raster(tiff_bytes(g), SIZE, src)
    assert np.isnan(arr[0, 0])
    assert not np.isnan(arr[10, 10])


def test_rgb_preview_rejected():
    src = get_source("multibeam")
    assert _decode_raster(rgb_bytes(SIZE), SIZE, src) is None


def test_garbage_bytes_rejected():
    src = get_source("dem-all")
    assert _decode_raster(b"not a tiff", SIZE, src) is None


def test_offsize_response_resized():
    src = get_source("dem-all")
    arr = _decode_raster(tiff_bytes(depth_grid(SIZE - 7)), SIZE, src)
    assert arr is not None and arr.shape == (SIZE, SIZE)


def test_offsize_resize_does_not_smear_sentinel_into_fake_depth():
    """An off-size grid that is half real depth (~-20 m) and half -9999 must
    resize to NaN-or-real only — never an interpolated mid-range fake depth
    at the coverage boundary (the resize-before-mask edge artifact)."""
    src = get_source("dem-all")
    n = SIZE - 5
    g = np.full((n, n), -20.0, dtype=np.float32)
    g[:, n // 2:] = -9999.0          # right half is no-coverage
    arr = _decode_raster(tiff_bytes(g), SIZE, src)
    valid = arr[~np.isnan(arr)]
    # Every surviving value must be near the real depth, not a -9999↔-20 blend.
    assert valid.size > 0
    assert valid.min() > -100.0, \
        f"sentinel smeared into a fake depth: min={valid.min()}"
    # The no-coverage half must be NaN, not fake water.
    assert np.isnan(arr[:, -1]).all()


# ─── Cache behavior ───────────────────────────────────────────────────────

def test_cold_then_warm_uses_disk_not_network(monkeypatch):
    calls = patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
    with tempfile.TemporaryDirectory() as tmp:
        r1 = fetch_tile_raster("dem-all", 256, 13, 2000, 3500, raster_root=tmp)
        assert r1 is not None and calls["n"] == 1
        # tile written to disk
        assert any(f.endswith(".tiff") for _, _, fs in os.walk(tmp) for f in fs)
        # warm: no new network call
        r2 = fetch_tile_raster("dem-all", 256, 13, 2000, 3500, raster_root=tmp)
        assert r2 is not None and calls["n"] == 1, "warm read must not hit network"


def test_all_nodata_not_persisted_and_retryable(monkeypatch):
    # First response: a transient all-nodata blip. Second: real data.
    state = {"first": True}

    def producer(s, b, n):
        if state["first"]:
            state["first"] = False
            return tiff_bytes(nodata_grid(n, -9999))
        return tiff_bytes(depth_grid(n))

    patch_fetch(monkeypatch, producer)
    with tempfile.TemporaryDirectory() as tmp:
        r1 = fetch_tile_raster("dem-all", 256, 13, 10, 10, raster_root=tmp)
        # empty grid: returned as data array but all-NaN, and NOT written to disk
        assert r1 is not None and np.isnan(r1[0]).all()
        assert not any(f.endswith(".tiff")
                       for _, _, fs in os.walk(tmp) for f in fs), \
            "all-nodata tile must not be persisted (else it greys forever)"
        # next visit recovers real data
        r2 = fetch_tile_raster("dem-all", 256, 13, 10, 10, raster_root=tmp)
        assert r2 is not None and not np.isnan(r2[0]).all(), "tile must recover"


def test_undecodable_not_persisted(monkeypatch):
    patch_fetch(monkeypatch, lambda s, b, n: b"garbage")
    with tempfile.TemporaryDirectory() as tmp:
        assert fetch_tile_raster("dem-all", 256, 13, 1, 1, raster_root=tmp) is None
        assert not any(f.endswith(".tiff")
                       for _, _, fs in os.walk(tmp) for f in fs)


def test_legacy_poisoned_disk_entry_treated_as_miss(monkeypatch):
    """An all-nodata .tiff already on disk (written by an older build) must be
    ignored and re-fetched, not served as a permanent grey tile."""
    src = get_source("dem-all")
    src_size = max(LG.OUTPUT_TILE_PX, 256)
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = os.path.join(tmp, src.cache_key, str(src_size))
        os.makedirs(cache_dir)
        # plant a poisoned all-nodata cache file
        with open(os.path.join(cache_dir, "13_5_5.tiff"), "wb") as f:
            f.write(tiff_bytes(nodata_grid(src_size + 2 * LG.BUFFER_PX, -9999)))
        patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
        r = fetch_tile_raster("dem-all", 256, 13, 5, 5, raster_root=tmp)
        assert r is not None and not np.isnan(r[0]).all(), \
            "poisoned disk entry must be re-fetched, not served"


# ─── Source resolution ────────────────────────────────────────────────────

def test_unknown_source_rejected(monkeypatch):
    patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
    with tempfile.TemporaryDirectory() as tmp:
        assert fetch_tile_raster("totally-bogus", 256, 13, 1, 1,
                                 raster_root=tmp) is UNKNOWN_SOURCE


def test_zoom_out_of_source_range_returns_none(monkeypatch):
    patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
    # crm-mosaic max_zoom = 15
    with tempfile.TemporaryDirectory() as tmp:
        assert fetch_tile_raster("crm-mosaic", 256, 20, 1, 1,
                                 raster_root=tmp) is None


def test_broad_zoomout_empty_does_not_poison_local_view(monkeypatch):
    """The historic worst-case: a wide zoom-out fires bursty requests, one
    returns a transient empty for a tile that really has data, and a later
    local high-zoom view of that source must still be correct."""
    def producer(s, b, n):
        # zoom-out tiles (we can't see z here, so make ALL early calls empty)
        return tiff_bytes(nodata_grid(n, -9999))

    calls = patch_fetch(monkeypatch, producer)
    with tempfile.TemporaryDirectory() as tmp:
        # broad zoom-out: a pile of empties, none persisted
        for x in range(8):
            fetch_tile_raster("dem-all", 256, 6, x, 20, raster_root=tmp)
        assert not any(f.endswith(".tiff")
                       for _, _, fs in os.walk(tmp) for f in fs)
        # now upstream "recovers"; a local z15 tile must read real data
        calls_before = calls["n"]
        patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
        r = fetch_tile_raster("dem-all", 256, 15, 9000, 14000, raster_root=tmp)
        assert r is not None and not np.isnan(r[0]).all()


# ─── Wire-format agreement (server pack ⇄ worker unpack) ───────────────────

def test_wire_format_roundtrip(monkeypatch):
    """Pack exactly as app.serve_raster, unpack exactly as analyses-worker.js."""
    patch_fetch(monkeypatch, lambda s, b, n: tiff_bytes(depth_grid(n)))
    with tempfile.TemporaryDirectory() as tmp:
        arr, cellsize_m, buffer_px = fetch_tile_raster(
            "dem-all", 256, 13, 100, 100, raster_root=tmp)
        h, w = arr.shape
        arr32 = np.ascontiguousarray(arr, dtype=np.float32)
        blob = struct.pack('<IIfI', w, h, float(cellsize_m), int(buffer_px)) \
            + arr32.tobytes(order='C')

        # worker side
        rw = struct.unpack('<I', blob[0:4])[0]
        rh = struct.unpack('<I', blob[4:8])[0]
        rcell = struct.unpack('<f', blob[8:12])[0]
        rbuf = struct.unpack('<I', blob[12:16])[0]
        body = np.frombuffer(blob, dtype='<f4', count=rw * rh, offset=16)
        assert (rw, rh, rbuf) == (w, h, buffer_px)
        assert math.isclose(rcell, float(cellsize_m), rel_tol=1e-5)
        assert body.size == w * h
        assert rbuf == LG.BUFFER_PX
        # cellsize must be the Mercator-corrected ground distance (positive, sane)
        assert 0 < rcell < 1000


# ─── Retry / backoff (patch the session, not the fetch helper) ─────────────

class FakeResp:
    def __init__(self, status, content=b"", content_type="image/tiff",
                 headers=None):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type, **(headers or {})}


def test_transient_5xx_is_retried_then_succeeds(monkeypatch):
    src = get_source("dem-all")
    seq = [FakeResp(503), FakeResp(503),
           FakeResp(200, tiff_bytes(depth_grid(SIZE)))]
    n = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        r = seq[n["i"]]
        n["i"] += 1
        return r

    monkeypatch.setattr(LG._session, "get", fake_get)
    monkeypatch.setattr(LG.time, "sleep", lambda s: None)   # don't actually wait
    out = _fetch_raster_bytes(src, (0, 0, 1, 1), SIZE)
    assert out is not None and n["i"] == 3, "should retry past two 503s"


def test_connection_error_is_retried(monkeypatch):
    import requests
    src = get_source("dem-all")
    n = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        n["i"] += 1
        if n["i"] < 3:
            raise requests.ConnectionError("reset")
        return FakeResp(200, tiff_bytes(depth_grid(SIZE)))

    monkeypatch.setattr(LG._session, "get", fake_get)
    monkeypatch.setattr(LG.time, "sleep", lambda s: None)
    assert _fetch_raster_bytes(src, (0, 0, 1, 1), SIZE) is not None
    assert n["i"] == 3


def test_429_honors_retry_after_capped(monkeypatch):
    src = get_source("dem-all")
    slept = []
    seq = [FakeResp(429, headers={"Retry-After": "999"}),
           FakeResp(200, tiff_bytes(depth_grid(SIZE)))]
    n = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        r = seq[n["i"]]; n["i"] += 1; return r

    monkeypatch.setattr(LG._session, "get", fake_get)
    monkeypatch.setattr(LG.time, "sleep", lambda s: slept.append(s))
    assert _fetch_raster_bytes(src, (0, 0, 1, 1), SIZE) is not None
    assert slept and slept[0] == LG.NOAA_RETRY_AFTER_CAP_S, \
        "a huge Retry-After must be capped so the tile stays responsive"


def test_4xx_not_retried(monkeypatch):
    src = get_source("dem-all")
    n = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        n["i"] += 1
        return FakeResp(404, b"<html>not found</html>", "text/html")

    monkeypatch.setattr(LG._session, "get", fake_get)
    monkeypatch.setattr(LG.time, "sleep", lambda s: None)
    assert _fetch_raster_bytes(src, (0, 0, 1, 1), SIZE) is None
    assert n["i"] == 1, "a deterministic 404 must not be retried"


def test_200_non_image_not_retried(monkeypatch):
    src = get_source("dem-all")
    n = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        n["i"] += 1
        return FakeResp(200, b'{"error":"bad"}', "application/json")

    monkeypatch.setattr(LG._session, "get", fake_get)
    monkeypatch.setattr(LG.time, "sleep", lambda s: None)
    assert _fetch_raster_bytes(src, (0, 0, 1, 1), SIZE) is None
    assert n["i"] == 1, "a 200 error page is deterministic — no retry"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
