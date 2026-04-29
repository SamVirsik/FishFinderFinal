// Client-side bathymetric analyses — runs inside the render Web Worker.
//
// Each algorithm takes a buffered Float32Array elevation grid (NOAA
// convention: positive = above sea level, negative = below; NaN = nodata,
// pre-zeroed by the caller) plus a parallel Uint8Array nodata mask, and
// writes RGBA bytes into a Uint8ClampedArray destination buffer.
//
// These are 1:1 ports of src/analyses.py. Keep them aligned visually if
// you change either side. The Python copy is only used by view_spot.py;
// the live web viewer runs every analysis in a worker (analyses-worker.js)
// so that the main thread never blocks on render math — scroll stays at
// 60 Hz no matter how many tiles are repainting.
//
// `self` works in both Window and Worker globals, so this script can be
// loaded via `<script>` or `importScripts` interchangeably.

(function () {
    const M_TO_FT = 3.28084;


    // ─── Reusable scratch buffers ───────────────────────────────────────
    //
    // Every analysis call previously allocated several Float32Arrays from
    // scratch (gradient: 2; gaussianBlur: 2; hillshade: z + out; depthBand
    // index: 1; etc.). At 288×288 px per tile that is ~330 KB per buffer —
    // for color-relief alone roughly 1.3 MB allocated and dropped per
    // tile. Multiplied across visible tiles, prefetch tiers, and
    // visualization/zoom toggles that adds up to tens of MB churned per
    // second of interactive use. The resulting GC sawtooth is what made
    // the worker stall between renders during sustained sessions and tile
    // slots stay blank longer than they should.
    //
    // Safety: the worker dispatches one analysis call at a time (the only
    // async boundaries are network fetch and createImageBitmap, both of
    // which sit OUTSIDE the analysis function). JavaScript does not
    // preempt synchronous code, so a single shared scratch pool is safe
    // even with concurrent handleRender invocations interleaving at the
    // await points.
    const _scratch = Object.create(null);
    function _f32(name, n) {
        let buf = _scratch[name];
        if (!buf || buf.length < n) {
            buf = new Float32Array(n);
            _scratch[name] = buf;
        }
        return buf;
    }
    function _i32(name, n) {
        let buf = _scratch[name];
        if (!buf || buf.length < n) {
            buf = new Int32Array(n);
            _scratch[name] = buf;
        }
        return buf;
    }

    // ─── Palettes (1:1 with src/analyses.py) ────────────────────────────

    // Land = transparent. Water = increasing depth.
    const DEPTH_PALETTE = new Uint8Array([
          0,   0,   0,   0,
        212, 255,   1, 255,  190, 255,   1, 255,
        166, 255,  17, 255,  139, 255,  17, 255,  115, 255,  49, 255,
         83, 255,  84, 255,   53, 255, 114, 255,   53, 255, 147, 255,
         53, 255, 199, 255,   17, 255, 220, 255,    0, 255, 249, 255,
          0, 245, 249, 255,    0, 226, 249, 255,    0, 212, 242, 255,
          0, 198, 242, 255,    0, 182, 242, 255,    0, 167, 242, 255,
          0, 150, 242, 255,    0, 128, 242, 255,    0, 110, 218, 255,
          0,  88, 218, 255,    0,  61, 215, 255,    0,  30, 215, 255,
          0,   0, 207, 255,    0,   0, 187, 255,    0,   0, 163, 255,
          0,   0, 140, 255,    0,   0, 128, 255,    0,   0, 108, 255,
          0,   0,  88, 255,
    ]);
    const DEPTH_PALETTE_LEN = DEPTH_PALETTE.length / 4;

    // Same palette but land reads as brown (used by colour-relief, where the
    // hillshade is going to darken everything anyway).
    const LAND_DEPTH_PALETTE = new Uint8Array(DEPTH_PALETTE);
    LAND_DEPTH_PALETTE[0] = 139; LAND_DEPTH_PALETTE[1] = 69;
    LAND_DEPTH_PALETTE[2] = 19;  LAND_DEPTH_PALETTE[3] = 255;

    // Slope: cool green → yellow → red → purple as steepness rises.
    const SLOPE_PALETTE = new Uint8Array([
        220, 255, 220, 255,  184, 255, 184, 255,
        140, 255, 140, 255,  100, 255, 100, 255,
        255, 255, 100, 255,  255, 230,  50, 255,
        255, 200,   0, 255,  255, 160,   0, 255,
        255, 120,   0, 255,  255,  80,   0, 255,
        255,  40,   0, 255,  220,   0, 120, 255,
        160,   0, 180, 255,
    ]);
    const SLOPE_PALETTE_LEN = SLOPE_PALETTE.length / 4;

    // Per-band thresholds for the fishing-spot detector. Shallower spots
    // need less slope to count as "fishy"; deep water needs more. Mirrors
    // _SPOT_DEPTH_BREAKS_FT / _SPOT_SLOPE_THRESH_DEG in analyses.py.
    const SPOT_DEPTH_BREAKS_FT = [0.0, 30.0, 80.0, 200.0, 600.0, Infinity];
    const SPOT_SLOPE_THRESH_DEG = [3.0, 5.0, 8.0, 12.0, 18.0];

    // Viridis colour-map approximation (Kenneth Moreland polynomial fit).
    // Visually indistinguishable from matplotlib's viridis at PNG-compressed
    // tile sizes; saves embedding a 256-entry table.
    function viridis(t) {
        if (t < 0) t = 0; else if (t > 1) t = 1;
        const r = 0.2777 + t*(0.1051 + t*(-0.3309 + t*(-4.6342 + t*(6.2283 + t*(4.7764 + t*-5.4355)))));
        const g = 0.0054 + t*(1.4046 + t*( 0.2148 + t*(-5.7991 + t*(14.1799 + t*(-13.7451 + t*4.6459)))));
        const b = 0.3341 + t*(1.3846 + t*( 0.0951 + t*(-19.3324 + t*(56.6906 + t*(-65.3530 + t*26.3124)))));
        return [
            Math.max(0, Math.min(255, (r * 255) | 0)),
            Math.max(0, Math.min(255, (g * 255) | 0)),
            Math.max(0, Math.min(255, (b * 255) | 0)),
        ];
    }

    // Pre-baked viridis lookup so the hot loop is a single table read.
    const VIRIDIS_LUT = (() => {
        const lut = new Uint8Array(256 * 3);
        for (let i = 0; i < 256; i++) {
            const [r, g, b] = viridis(i / 255);
            lut[i*3]     = r;
            lut[i*3 + 1] = g;
            lut[i*3 + 2] = b;
        }
        return lut;
    })();


    // ─── Numerical helpers ──────────────────────────────────────────────

    // numpy-style central-difference gradient. Edges use one-sided diff so
    // shape matches input. Output dz/dx and dz/dy are in metres-per-metre
    // (true gradient, since cellsize_m carries the Mercator-corrected
    // ground sample distance).
    function gradient(elev, w, h, cellsize) {
        const n = w * h;
        const dzdx = _f32('gx', n);
        const dzdy = _f32('gy', n);
        const inv2c = 1 / (2 * cellsize);
        const invc = 1 / cellsize;

        for (let y = 0; y < h; y++) {
            const row = y * w;
            // x = 0
            dzdx[row] = (elev[row + 1] - elev[row]) * invc;
            // 1 ≤ x ≤ w-2
            for (let x = 1; x < w - 1; x++) {
                dzdx[row + x] = (elev[row + x + 1] - elev[row + x - 1]) * inv2c;
            }
            // x = w-1
            dzdx[row + w - 1] = (elev[row + w - 1] - elev[row + w - 2]) * invc;
        }

        // y direction
        for (let x = 0; x < w; x++) {
            dzdy[x] = (elev[w + x] - elev[x]) * invc;
            for (let y = 1; y < h - 1; y++) {
                dzdy[y * w + x] = (elev[(y + 1) * w + x] - elev[(y - 1) * w + x]) * inv2c;
            }
            dzdy[(h - 1) * w + x] = (elev[(h - 1) * w + x] - elev[(h - 2) * w + x]) * invc;
        }
        return { dzdx, dzdy };
    }

    // Hillshade: 0..1 illumination layer. Negates elev so that for ocean
    // tiles deep water reads as "high terrain" — matching analyses.py.
    function hillshade(elev, w, h, cellsize, exaggeration) {
        const n = w * h;
        const z = _f32('hsZ', n);
        for (let i = 0; i < n; i++) z[i] = -elev[i] * exaggeration;
        const { dzdx, dzdy } = gradient(z, w, h, cellsize);

        const az = (315.0 * Math.PI) / 180.0;
        const zenith = ((90.0 - 45.0) * Math.PI) / 180.0;
        const cosZen = Math.cos(zenith);
        const sinZen = Math.sin(zenith);

        const out = _f32('hsOut', n);
        for (let i = 0; i < n; i++) {
            const sx = dzdx[i];
            const sy = dzdy[i];
            const slope = Math.atan(Math.hypot(sx, sy));
            let aspect = Math.atan2(sy, -sx);
            if (aspect < 0) aspect += 2 * Math.PI;
            let h_ = cosZen * Math.cos(slope)
                   + sinZen * Math.sin(slope) * Math.cos(az - aspect);
            if (h_ < 0) h_ = 0; else if (h_ > 1) h_ = 1;
            out[i] = h_;
        }
        return out;
    }

    // Slope angle in degrees from horizontal.
    function slopeDegrees(elev, w, h, cellsize) {
        const n = w * h;
        const { dzdx, dzdy } = gradient(elev, w, h, cellsize);
        const out = _f32('slpOut', n);
        const radToDeg = 180.0 / Math.PI;
        for (let i = 0; i < n; i++) {
            out[i] = Math.atan(Math.hypot(dzdx[i], dzdy[i])) * radToDeg;
        }
        return out;
    }

    // Single-axis box blur with running-sum sliding window. O(N) regardless
    // of radius — that's the whole point. Edge handling = 'nearest' (clamp).
    function boxBlurPass(src, dst, w, h, radius, axis) {
        const stride  = axis === 0 ? 1 : w;       // step along axis
        const lineLen = axis === 0 ? w : h;        // length along axis
        const lineCount = axis === 0 ? h : w;      // perpendicular count
        const lineStep  = axis === 0 ? w : 1;
        const denom = 1 / (2 * radius + 1);

        for (let line = 0; line < lineCount; line++) {
            const base = line * lineStep;
            // Seed sum with the initial window, clamping out-of-bounds.
            let sum = 0;
            for (let i = -radius; i <= radius; i++) {
                let p = i;
                if (p < 0) p = 0; else if (p >= lineLen) p = lineLen - 1;
                sum += src[base + p * stride];
            }
            for (let i = 0; i < lineLen; i++) {
                dst[base + i * stride] = sum * denom;
                // Slide: drop the leftmost, add the next-rightmost (clamped).
                let outIdx = i - radius;
                let inIdx  = i + radius + 1;
                if (outIdx < 0) outIdx = 0;
                if (inIdx  >= lineLen) inIdx = lineLen - 1;
                sum += src[base + inIdx * stride] - src[base + outIdx * stride];
            }
        }
    }

    // Three iterated box blurs ≈ Gaussian (central-limit-theorem
    // approximation). O(N) per pixel total, independent of sigma. The
    // visual difference vs a true Gaussian on a depth grid is well below
    // colour-band quantisation, so we use it everywhere.
    //
    // The blur output ('bb') is intentionally a different scratch slot
    // from any intermediate the caller might still hold (e.g. gradient
    // outputs), so the returned buffer can be safely read by the caller
    // even if the next analysis call recycles the 'ba' temp.
    function gaussianBlur(src, w, h, sigma) {
        const n = w * h;
        if (sigma <= 0.5) {
            const c = _f32('bb', n);
            for (let i = 0; i < n; i++) c[i] = src[i];
            return c;
        }
        const radius = Math.max(1, Math.round(sigma));
        const a = _f32('ba', n);
        const b = _f32('bb', n);
        // 3 passes, each separable into horizontal + vertical.
        boxBlurPass(src, a, w, h, radius, 0); boxBlurPass(a, b, w, h, radius, 1);
        boxBlurPass(b, a, w, h, radius, 0); boxBlurPass(a, b, w, h, radius, 1);
        boxBlurPass(b, a, w, h, radius, 0); boxBlurPass(a, b, w, h, radius, 1);
        return b;
    }


    // ─── Analyses ───────────────────────────────────────────────────────

    // Each analysis writes RGBA bytes into `out` (length 4*w*h). The caller
    // applies the nodata mask to the alpha channel afterwards.

    function colorRelief(elev, nodata, w, h, cellsize, param, out) {
        const exaggeration = Math.max(1.0, param);
        const hs = hillshade(elev, w, h, cellsize, exaggeration);
        const ambient = 0.35;

        for (let i = 0; i < elev.length; i++) {
            const e = elev[i];
            // Slot 0 = land (brown); slots 1..N = water depth bands of 4 ft.
            let slot;
            if (e >= 0) {
                slot = 0;
            } else {
                const depth_ft = -e * M_TO_FT;
                slot = (depth_ft / 4.0) | 0;
                slot = slot + 1;
                if (slot < 1) slot = 1;
                else if (slot > DEPTH_PALETTE_LEN - 1) slot = DEPTH_PALETTE_LEN - 1;
            }
            const p = slot * 4;
            const lit = ambient + hs[i] * (1.0 - ambient);
            const di = i * 4;
            out[di]     = LAND_DEPTH_PALETTE[p]     * lit;
            out[di + 1] = LAND_DEPTH_PALETTE[p + 1] * lit;
            out[di + 2] = LAND_DEPTH_PALETTE[p + 2] * lit;
            out[di + 3] = LAND_DEPTH_PALETTE[p + 3];
        }
    }

    function depthSmooth(elev, nodata, w, h, cellsize, param, out) {
        const max_ft = Math.max(10.0, param);
        const inv_max_ft = 1.0 / max_ft;
        for (let i = 0; i < elev.length; i++) {
            const e = elev[i];
            const di = i * 4;
            if (e >= 0) {
                out[di] = 0; out[di+1] = 0; out[di+2] = 0; out[di+3] = 0;
            } else {
                const depth_ft = -e * M_TO_FT;
                let t = depth_ft * inv_max_ft;
                if (t < 0) t = 0; else if (t > 1) t = 1;
                const li = ((t * 255) | 0) * 3;
                out[di]     = VIRIDIS_LUT[li];
                out[di + 1] = VIRIDIS_LUT[li + 1];
                out[di + 2] = VIRIDIS_LUT[li + 2];
                out[di + 3] = 255;
            }
        }
    }

    // Compute the depth-band index grid (used by both depthBands and
    // fishingSpots). Slot 0 reserved for land/transparent.
    function depthBandIndex(elev, w, h, band_ft) {
        const idx = _i32('idx', w * h);
        const inv_band = 1.0 / Math.max(band_ft, 1e-3);
        for (let i = 0; i < elev.length; i++) {
            const e = elev[i];
            if (e >= 0) {
                idx[i] = 0;
            } else {
                let s = ((-e * M_TO_FT) * inv_band | 0) + 1;
                if (s < 1) s = 1;
                else if (s > DEPTH_PALETTE_LEN - 1) s = DEPTH_PALETTE_LEN - 1;
                idx[i] = s;
            }
        }
        return idx;
    }

    function depthBands(elev, nodata, w, h, cellsize, param, out) {
        const band_ft = Math.max(0.5, param);
        const idx = depthBandIndex(elev, w, h, band_ft);

        // Fill base palette colour for each pixel.
        for (let i = 0; i < idx.length; i++) {
            const p = idx[i] * 4;
            const di = i * 4;
            out[di]     = DEPTH_PALETTE[p];
            out[di + 1] = DEPTH_PALETTE[p + 1];
            out[di + 2] = DEPTH_PALETTE[p + 2];
            out[di + 3] = DEPTH_PALETTE[p + 3];
        }
        // Black contour wherever the band index changes (right or below).
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i = y * w + x;
                let edge = false;
                if (x < w - 1 && idx[i] !== idx[i + 1]) edge = true;
                else if (y < h - 1 && idx[i] !== idx[i + w]) edge = true;
                if (edge) {
                    const di = i * 4;
                    out[di] = 0; out[di+1] = 0; out[di+2] = 0; out[di+3] = 255;
                }
            }
        }
    }

    function hillshadeOnly(elev, nodata, w, h, cellsize, param, out) {
        const exaggeration = Math.max(1.0, param);
        const hs = hillshade(elev, w, h, cellsize, exaggeration);
        for (let i = 0; i < hs.length; i++) {
            const v = (hs[i] * 255) | 0;
            const di = i * 4;
            out[di] = v; out[di+1] = v; out[di+2] = v; out[di+3] = 255;
        }
    }

    function slopeAnalysis(elev, nodata, w, h, cellsize, param, out) {
        const max_deg = Math.max(2.0, param);
        const deg = slopeDegrees(elev, w, h, cellsize);
        const inv = (SLOPE_PALETTE_LEN - 1) / max_deg;
        for (let i = 0; i < deg.length; i++) {
            let s = (deg[i] * inv) | 0;
            if (s < 0) s = 0;
            else if (s > SLOPE_PALETTE_LEN - 1) s = SLOPE_PALETTE_LEN - 1;
            const p = s * 4;
            const di = i * 4;
            out[di]     = SLOPE_PALETTE[p];
            out[di + 1] = SLOPE_PALETTE[p + 1];
            out[di + 2] = SLOPE_PALETTE[p + 2];
            // Mute slope on land slightly (let basemap show through).
            out[di + 3] = elev[i] >= 0 ? 80 : 255;
        }
    }

    function aspectAnalysis(elev, nodata, w, h, cellsize, param, out) {
        const min_slope_deg = Math.max(0.5, param);
        const { dzdx, dzdy } = gradient(elev, w, h, cellsize);
        const radToDeg = 180 / Math.PI;
        const sat_inv = 1.0 / (min_slope_deg * 4.0);
        const val = 0.95;
        for (let i = 0; i < elev.length; i++) {
            const sx = dzdx[i];
            const sy = dzdy[i];
            const slope_deg = Math.atan(Math.hypot(sx, sy)) * radToDeg;
            let asp = Math.atan2(-sx, sy) * radToDeg;
            asp = ((asp % 360) + 360) % 360;
            const hue = asp / 360.0;
            let sat = slope_deg * sat_inv;
            if (sat < 0) sat = 0; else if (sat > 1) sat = 1;

            // HSV → RGB inlined.
            const hh = hue * 6.0;
            const ii = Math.floor(hh) % 6;
            const f  = hh - Math.floor(hh);
            const p_ = val * (1.0 - sat);
            const q_ = val * (1.0 - sat * f);
            const t_ = val * (1.0 - sat * (1.0 - f));
            let r, g, b;
            switch (ii) {
                case 0: r = val; g = t_;  b = p_; break;
                case 1: r = q_;  g = val; b = p_; break;
                case 2: r = p_;  g = val; b = t_; break;
                case 3: r = p_;  g = q_;  b = val; break;
                case 4: r = t_;  g = p_;  b = val; break;
                default: r = val; g = p_;  b = q_;
            }
            const di = i * 4;
            out[di]     = (r * 255) | 0;
            out[di + 1] = (g * 255) | 0;
            out[di + 2] = (b * 255) | 0;
            // Below threshold or on land → fully transparent.
            out[di + 3] = (slope_deg < min_slope_deg || elev[i] >= 0) ? 0 : 255;
        }
    }

    function roughness(elev, nodata, w, h, cellsize, param, out) {
        const feature_scale_m = Math.max(2.0, param);
        const sigma_px = Math.max(0.5, feature_scale_m / cellsize);
        const bg = gaussianBlur(elev, w, h, sigma_px);
        const full_white = Math.max(0.05 * feature_scale_m, 1e-3);
        const inv_full = 1.0 / full_white;
        for (let i = 0; i < elev.length; i++) {
            const r = Math.abs(elev[i] - bg[i]);
            let v = r * inv_full;
            if (v < 0) v = 0; else if (v > 1) v = 1;
            const g = (v * 255) | 0;
            const di = i * 4;
            out[di] = g; out[di+1] = g; out[di+2] = g; out[di+3] = 255;
        }
    }

    function fishingSpots(elev, nodata, w, h, cellsize, param, out) {
        const band_ft = Math.max(1.0, param);
        const idx = depthBandIndex(elev, w, h, band_ft);
        const slope_deg = slopeDegrees(elev, w, h, cellsize);

        // depth-band-aware spot mask. Reused scratch — must explicitly write
        // every cell (including land) since the buffer is no longer fresh.
        const spot = _f32('spot', elev.length);
        for (let i = 0; i < elev.length; i++) {
            const e = elev[i];
            if (e >= 0) { spot[i] = 0.0; continue; }
            const dft = -e * M_TO_FT;
            let thresh;
            if (dft < SPOT_DEPTH_BREAKS_FT[1]) thresh = SPOT_SLOPE_THRESH_DEG[0];
            else if (dft < SPOT_DEPTH_BREAKS_FT[2]) thresh = SPOT_SLOPE_THRESH_DEG[1];
            else if (dft < SPOT_DEPTH_BREAKS_FT[3]) thresh = SPOT_SLOPE_THRESH_DEG[2];
            else if (dft < SPOT_DEPTH_BREAKS_FT[4]) thresh = SPOT_SLOPE_THRESH_DEG[3];
            else thresh = SPOT_SLOPE_THRESH_DEG[4];
            spot[i] = slope_deg[i] >= thresh ? 1.0 : 0.0;
        }
        // Slight dilation so single-pixel ridges become visible marks
        // (matches the gaussian_filter > 0.3 step in analyses.py).
        const spotBlur = gaussianBlur(spot, w, h, 1.0);

        for (let i = 0; i < elev.length; i++) {
            const di = i * 4;
            if (spotBlur[i] > 0.3) {
                out[di] = 255; out[di+1] = 30; out[di+2] = 255; out[di+3] = 255;
            } else {
                const p = idx[i] * 4;
                out[di]     = DEPTH_PALETTE[p];
                out[di + 1] = DEPTH_PALETTE[p + 1];
                out[di + 2] = DEPTH_PALETTE[p + 2];
                out[di + 3] = DEPTH_PALETTE[p + 3];
            }
        }
    }


    // ─── Public dispatch ────────────────────────────────────────────────

    self.FFAnalyses = {
        'color-relief':  colorRelief,
        'depth':         depthSmooth,
        'depth-bands':   depthBands,
        'hillshade':     hillshadeOnly,
        'slope':         slopeAnalysis,
        'aspect':        aspectAnalysis,
        'roughness':     roughness,
        'fishing-spots': fishingSpots,
    };
})();
