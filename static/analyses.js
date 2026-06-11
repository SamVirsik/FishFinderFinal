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

    // ─── Tuning constants (easy to adjust) ──────────────────────────────
    // texture-relief
    const TEXTURE_VERT_EXAG = 5;     // fixed hillshade exaggeration
    const TEXTURE_K         = 0.6;   // detail-modulation strength
    // structure (curvature)
    const STRUCT_VERT_EXAG     = 5;     // base hillshade exaggeration
    const STRUCT_CURV_AMP      = 2.0;   // m — curvature that saturates the ramp
    const STRUCT_BASE_CONTRAST = 0.7;   // base hillshade contrast under colour
    const STRUCT_ALPHA         = 0.85;  // max curvature-colour opacity
    const STRUCT_CONCAVE = [40, 110, 215];  // blue — holes/channels (L>0)
    const STRUCT_CONVEX  = [210, 70, 50];   // red  — humps/ledges  (L<0)
    // spot-score
    const SPOT_FEATURE_M     = 40;   // fixed roughness feature scale (m)
    const SPOT_TOL_FT        = 12;   // depth tolerance around target (ft)
    const SPOT_R_WEIGHT      = 0.6;  // roughness weight in score
    const SPOT_S_WEIGHT      = 0.4;  // slope weight in score
    const SPOT_SLOPE_MAX_DEG = 45;   // slope that maps to S=1
    // depth-contours
    const CONTOUR_FALLBACK_MAXFT = 300;          // when a tile has no water
    const CONTOUR_LINE = [20, 30, 40];           // dark contour-line colour
    const CONTOUR_MIX  = 0.7;                     // line/fill blend toward line


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

    // Inferno colour-map approximation (Mikhailov 6th-order polynomial fit).
    // Used by spot-score: dark/purple = low, bright yellow = high.
    function inferno(t) {
        if (t < 0) t = 0; else if (t > 1) t = 1;
        const r = -0.000219 + t*(0.106513 + t*(11.602493 + t*(-41.703996 + t*(77.162936 + t*(-71.319428 + t*25.131126)))));
        const g =  0.001651 + t*(0.563956 + t*(-3.972854 + t*( 17.436399 + t*(-33.402359 + t*( 32.626064 + t*-12.242669)))));
        const b = -0.019481 + t*(3.932712 + t*(-15.942394 + t*( 44.354145 + t*(-81.807309 + t*( 73.209520 + t*-23.070325)))));
        return [
            Math.max(0, Math.min(255, (r * 255) | 0)),
            Math.max(0, Math.min(255, (g * 255) | 0)),
            Math.max(0, Math.min(255, (b * 255) | 0)),
        ];
    }

    const INFERNO_LUT = (() => {
        const lut = new Uint8Array(256 * 3);
        for (let i = 0; i < 256; i++) {
            const [r, g, b] = inferno(i / 255);
            lut[i*3]     = r;
            lut[i*3 + 1] = g;
            lut[i*3 + 2] = b;
        }
        return lut;
    })();

    // GLSL-style smoothstep: 0 below e0, 1 above e1, Hermite ramp between.
    function smoothstep(e0, e1, x) {
        let t = (x - e0) / (e1 - e0);
        if (t < 0) t = 0; else if (t > 1) t = 1;
        return t * t * (3 - 2 * t);
    }


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

    function colorRelief(elev, nodata, w, h, cellsize, param, out, paramExtra) {
        const exaggeration = Math.max(1.0, param);
        const hs = hillshade(elev, w, h, cellsize, exaggeration);
        const ambient = 0.35;

        // Depth-range rescaling: slot 1 is the shallowest colour and slot
        // (PALETTE_LEN - 1) is the deepest. Depths outside the user range
        // clamp to those endpoints (matches the spec).
        const usable = DEPTH_PALETTE_LEN - 1;          // 36 water-depth colours
        const minFt = paramExtra && Number.isFinite(paramExtra.minDepthFt)
            ? paramExtra.minDepthFt : 0.0;
        const maxFt = paramExtra && Number.isFinite(paramExtra.maxDepthFt)
            ? paramExtra.maxDepthFt : 140.0;
        const rangeFt = Math.max(1e-3, maxFt - minFt);
        const invRange = 1.0 / rangeFt;

        for (let i = 0; i < elev.length; i++) {
            const e = elev[i];
            // Slot 0 = land (brown); slots 1..usable = water depth bands.
            let slot;
            if (e >= 0) {
                slot = 0;
            } else {
                const depth_ft = -e * M_TO_FT;
                const t = (depth_ft - minFt) * invRange;
                if (t <= 0) slot = 1;
                else if (t >= 1) slot = usable;
                else slot = 1 + Math.min(usable - 1, (t * usable) | 0);
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

    // texture-relief — detail-enhanced shaded relief. The hillshade carries
    // the macro form; a high-pass residual (depth minus its blurred self at
    // the chosen feature scale) modulates the luminance so wrecks, ledges
    // and rubble pop without losing the overall shape. Warm-grey tint.
    function textureRelief(elev, nodata, w, h, cellsize, param, out) {
        const featureScale = Math.max(5.0, param);
        const sigma = Math.max(0.5, featureScale / cellsize);
        const bg = gaussianBlur(elev, w, h, sigma);                 // 'bb'
        const hs = hillshade(elev, w, h, cellsize, TEXTURE_VERT_EXAG); // 'hsOut'
        const invNorm = 1.0 / Math.max(0.05 * featureScale, 1e-3);
        for (let i = 0; i < elev.length; i++) {
            let Dn = (elev[i] - bg[i]) * invNorm;
            if (Dn < -1) Dn = -1; else if (Dn > 1) Dn = 1;
            let lum = hs[i] * (1 + TEXTURE_K * Dn);
            if (lum < 0) lum = 0; else if (lum > 1) lum = 1;
            const v = lum * 255;
            const di = i * 4;
            out[di]     = v;
            out[di + 1] = v * 0.97;
            out[di + 2] = v * 0.92;
            out[di + 3] = 255;
        }
    }

    // structure — diverging concave/convex (curvature) map over a faint
    // hillshade. Blue = concave (holes, channels), red = convex (humps,
    // ledges). Flat ground stays near-transparent so the relief reads
    // through. Replaces the old slope view.
    function structure(elev, nodata, w, h, cellsize, param, out) {
        const featureScale = Math.max(5.0, param);
        const sigma = Math.max(0.5, featureScale / cellsize);
        const zb = gaussianBlur(elev, w, h, sigma);                 // 'bb'
        const hs = hillshade(elev, w, h, cellsize, STRUCT_VERT_EXAG); // 'hsOut'
        const invc2 = 1.0 / (cellsize * cellsize);
        const ampScale = (featureScale * featureScale) / STRUCT_CURV_AMP;
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i  = y * w + x;
                const xl = x > 0     ? i - 1 : i;
                const xr = x < w - 1 ? i + 1 : i;
                const yu = y > 0     ? i - w : i;
                const yd = y < h - 1 ? i + w : i;
                // Laplacian of elevation (+up). Concave (valley) → positive.
                const L = (zb[yu] + zb[yd] + zb[xl] + zb[xr] - 4 * zb[i]) * invc2;
                let t = L * ampScale;
                if (t < -1) t = -1; else if (t > 1) t = 1;
                const baseGrey = hs[i] * STRUCT_BASE_CONTRAST * 255;
                const a = Math.abs(t) * STRUCT_ALPHA;
                const col = t >= 0 ? STRUCT_CONCAVE : STRUCT_CONVEX;
                const di = i * 4;
                out[di]     = col[0] * a + baseGrey * (1 - a);
                out[di + 1] = col[1] * a + baseGrey * (1 - a);
                out[di + 2] = col[2] * a + baseGrey * (1 - a);
                out[di + 3] = 255;
            }
        }
    }

    // spot-score — the headline fusion. Roughness + slope, gated by a
    // Gaussian preference for a reachable target depth, painted in inferno.
    // Low scores fade to transparent so the basemap reads through; bright
    // yellow marks the most promising structure at the depth you want.
    function spotScore(elev, nodata, w, h, cellsize, param, out) {
        const target = Math.max(10.0, param);
        const sigma = Math.max(0.5, SPOT_FEATURE_M / cellsize);
        const bg  = gaussianBlur(elev, w, h, sigma);     // 'bb'
        const deg = slopeDegrees(elev, w, h, cellsize);  // 'slpOut'
        const invR = 1.0 / Math.max(0.05 * SPOT_FEATURE_M, 1e-3);
        const invTol = 1.0 / SPOT_TOL_FT;
        const invSlope = 1.0 / SPOT_SLOPE_MAX_DEG;
        for (let i = 0; i < elev.length; i++) {
            const di = i * 4;
            const e = elev[i];
            if (e >= 0) {  // land (nodata is punched to alpha 0 by the caller)
                out[di] = 0; out[di+1] = 0; out[di+2] = 0; out[di+3] = 0;
                continue;
            }
            const depth_ft = -e * M_TO_FT;
            let R = Math.abs(e - bg[i]) * invR; if (R > 1) R = 1;
            let S = deg[i] * invSlope;          if (S > 1) S = 1;
            const d = (depth_ft - target) * invTol;
            const P = Math.exp(-(d * d));
            let score = (SPOT_R_WEIGHT * R + SPOT_S_WEIGHT * S) * P;
            if (score < 0) score = 0; else if (score > 1) score = 1;
            const li = ((score * 255) | 0) * 3;
            out[di]     = INFERNO_LUT[li];
            out[di + 1] = INFERNO_LUT[li + 1];
            out[di + 2] = INFERNO_LUT[li + 2];
            out[di + 3] = smoothstep(0.2, 0.5, score) * 255;
        }
    }

    // depth-contours — calm chart view. Smooth viridis depth fill (auto-
    // scaled to the deepest water in the tile) with dark contour lines drawn
    // wherever the contour band changes. Merges the old depth + depth-bands.
    function depthContours(elev, nodata, w, h, cellsize, param, out) {
        const interval = Math.max(1.0, param);
        const n = w * h;

        // One reduce pass for the per-tile depth ceiling (NaN nodata is
        // pre-zeroed → land/nodata read as depth 0 and don't inflate it).
        let maxDepth = 0;
        for (let i = 0; i < n; i++) {
            const e = elev[i];
            if (e < 0) { const dft = -e * M_TO_FT; if (dft > maxDepth) maxDepth = dft; }
        }
        if (maxDepth < 1e-3) maxDepth = CONTOUR_FALLBACK_MAXFT;
        const invMax = 1.0 / maxDepth;
        const invInterval = 1.0 / interval;

        // Band index per pixel (land = -1) so edges read across the shoreline.
        const band = _i32('cBand', n);
        for (let i = 0; i < n; i++) {
            const e = elev[i];
            band[i] = e < 0 ? Math.floor(-e * M_TO_FT * invInterval) : -1;
        }

        const keep = 1 - CONTOUR_MIX;
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const i = y * w + x;
                const di = i * 4;
                const e = elev[i];
                if (e >= 0) {  // land transparent
                    out[di] = 0; out[di+1] = 0; out[di+2] = 0; out[di+3] = 0;
                    continue;
                }
                let t = -e * M_TO_FT * invMax; if (t > 1) t = 1;
                const li = ((t * 255) | 0) * 3;
                let r = VIRIDIS_LUT[li], g = VIRIDIS_LUT[li + 1], b = VIRIDIS_LUT[li + 2];
                const b0 = band[i];
                let edge = false;
                if (x < w - 1 && band[i + 1] !== b0) edge = true;
                else if (y < h - 1 && band[i + w] !== b0) edge = true;
                if (edge) {
                    r = r * keep + CONTOUR_LINE[0] * CONTOUR_MIX;
                    g = g * keep + CONTOUR_LINE[1] * CONTOUR_MIX;
                    b = b * keep + CONTOUR_LINE[2] * CONTOUR_MIX;
                }
                out[di] = r; out[di + 1] = g; out[di + 2] = b; out[di + 3] = 255;
            }
        }
    }

    // ─── Public dispatch ────────────────────────────────────────────────

    self.FFAnalyses = {
        'color-relief':    colorRelief,
        'texture-relief':  textureRelief,
        'structure':       structure,
        'spot-score':      spotScore,
        'depth-contours':  depthContours,
    };
})();
