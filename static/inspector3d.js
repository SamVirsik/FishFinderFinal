// FishFinder 3D inspector — high-resolution Three.js height-field
// rendering of a user-defined bbox.
//
// Loaded into the main page (not the render worker). Public API:
//
//   window.FishFinderInspector3D.open({
//     bbox:        { north, south, east, west },  // degrees
//     sourceId:    "dem-all",                      // data source registry id
//     analysisKey: "color-relief",                 // 2D analysis to texture with
//     param:       8,                              // analysis param (scalar)
//     paramExtra:  { minDepthFt, maxDepthFt }      // analysis-specific extras
//   })
//
//   window.FishFinderInspector3D.close()
//
// PIPELINE (one open() call):
//   1. Show the modal in "loading…" state.
//   2. Fetch one float32 raster covering the bbox from /raster/inspect
//      at fetchSize×fetchSize (default 512 — high detail, sub-second
//      response for small areas).
//   3. Build an indexed PlaneGeometry sized to the bbox's true ground
//      width/height in metres. detail² vertices, each Z = elev × exag.
//   4. Run the chosen 2D analysis on the raster grid into a fetchSize²
//      RGBA buffer, wrap it as a CanvasTexture, apply to the mesh.
//   5. Add a directional + ambient light, set up OrbitControls, animate.
//
// Detail / exaggeration / light-angle sliders mutate the live scene
// without re-fetching the raster:
//   - Exaggeration: rewrite only Z values on the existing geometry's
//     position buffer (fast).
//   - Detail:       rebuild geometry from the cached raster (no fetch).
//   - Light angle:  re-position the existing directional light.
//
// Close: tear down the renderer, scene, geometry, texture; null out
// the cached raster. WebGL contexts are an OS-level resource and the
// browser caps them — leaving one alive across many open/close cycles
// will eventually hit "too many contexts".

(function () {
    const baseurl = window.location.origin;

    // ─── Tunables ──────────────────────────────────────────────────
    const DEFAULT_FETCH_SIZE  = 512;     // NxN raster resolution
    const MAX_FETCH_SIZE      = 1024;    // server cap is 2048; we stop earlier
    const DEFAULT_DETAIL      = 384;     // vertex grid per side
    const DEFAULT_EXAGGERATION = 15;     // start dramatic; small areas need it
    const MAX_EXAGGERATION    = 100;
    const AUTO_TARGET_FRACTION = 0.25;   // terrain Y range as fraction of max(W,H)
    const DEFAULT_LIGHT_DEG   = 315;     // azimuth (0 = N, 90 = E, 315 = NW)

    // Skirt (the wall that turns the height-field into a solid block).
    const SKIRT_PAD_FRACTION  = 0.10;    // base plane below deepest point
    const SKIRT_COLOR         = 0x2c3038;

    // Depth axis (the depth ruler at the NW corner).
    const AXIS_COLOR          = 0x9eb6d0;

    // Default framing — camera orbits the geometry centre at this
    // azimuth/elevation, distance computed to fit the bounding sphere.
    const FRAME_AZIMUTH_DEG   = 35;      // 0 = look along -Z (north), positive = swing east
    const FRAME_ELEVATION_DEG = 30;      // above the horizon
    const FRAME_PADDING       = 1.15;    // ~15% margin around the bounding sphere

    // Real-world human height used by the on-screen scale legend.
    const FIGURE_HEIGHT_M     = 1.7;

    // Pixel-height clamps for the scale legend. Below the min it
    // disappears against the terrain; above the max it dominates the
    // viewport. When clamped at the max we annotate the label.
    const LEGEND_MIN_PX       = 12;
    const LEGEND_MAX_VH_FRAC  = 0.70;

    // Maximum area the user can draw. Past this we show a warning and
    // refuse — 3D detail is the point, so keep the rectangle small.
    // 25 km² ≈ 5 km × 5 km, generous for "inspector" use.
    const MAX_AREA_KM2 = 25;

    // ─── Module state ──────────────────────────────────────────────
    let drawMode = false;                // true while waiting for a drag
    let drawState = null;                 // active drag: { x0, y0, x1, y1 }
    let drawOverlaySvg = null;            // SVG live preview
    let drawHintEl = null;                // top-of-screen hint pill
    let drawDimEl = null;                 // floating cursor-anchored dimensions badge

    // Open-modal session state. Null when the modal is closed.
    //
    // {
    //   bbox, sourceId, analysisKey, param, paramExtra,
    //   raster:      { w, h, cellsize_m, data: Float32Array },
    //   detail, exaggeration, lightDeg, fetchSize,
    //   autoMode,               // true = exag is auto-computed each VE update
    //   widthM, heightM,        // bbox dimensions in true ground metres
    //   terrainMinElevM, terrainMaxElevM,  // raster z extremes (unexag, m)
    //   baseY,                  // scene-Y of the skirt's bottom plane
    //   renderer, scene, camera, controls,
    //   mesh, geometry, material, texture,
    //   skirt, skirtGeom, skirtMat,  // dark walls + bottom cap
    //   axis,                    // Group: depth ruler line + sprite labels
    //   light, ambient,
    //   showFigure,             // toggle state for the screen-space legend
    //   legendEl, legendGraphicEl, legendLabelEl,  // DOM nodes
    //   raf,                    // current animation frame id
    //   onResize,               // bound resize handler
    // }
    let session = null;

    // Hooks installed by map.js so we can route drag events through
    // the view without bypassing ArcGIS. See registerHost().
    let host = null;


    // ─── DOM lookups (lazy) ────────────────────────────────────────
    function $(id) { return document.getElementById(id); }


    // ─── Drag overlay ──────────────────────────────────────────────
    // We track the drag in screen pixels via plain pointer events on a
    // full-screen overlay div. Using ArcGIS's view.on("drag") here would
    // collide with the Spotfinder drag handler (they'd both claim the
    // event and one would silently lose). A separate top-level overlay
    // sidesteps that — we only install it when draw mode is armed and
    // remove it as soon as the user releases or cancels.

    function ensureDrawOverlay() {
        if (drawOverlaySvg) return;
        drawOverlaySvg = document.createElementNS(
            "http://www.w3.org/2000/svg", "svg");
        drawOverlaySvg.setAttribute("class", "inspect3d-draw-overlay");
        const rect = document.createElementNS(
            "http://www.w3.org/2000/svg", "rect");
        drawOverlaySvg.appendChild(rect);
        document.body.appendChild(drawOverlaySvg);

        drawHintEl = document.createElement("div");
        drawHintEl.className = "inspect3d-draw-hint";
        drawHintEl.textContent =
            "Click and drag to define the 3D inspection area. ESC to cancel.";
        document.body.appendChild(drawHintEl);

        drawDimEl = document.createElement("div");
        drawDimEl.className = "inspect3d-draw-dim";
        document.body.appendChild(drawDimEl);
    }

    function removeDrawOverlay() {
        if (drawOverlaySvg) {
            drawOverlaySvg.remove();
            drawOverlaySvg = null;
        }
        if (drawHintEl) {
            drawHintEl.remove();
            drawHintEl = null;
        }
        if (drawDimEl) {
            drawDimEl.remove();
            drawDimEl = null;
        }
    }

    // Format a metres distance as "420 m" or "1.2 km" depending on
    // magnitude. Same rounding policy as fmtKm but a hair tighter on
    // the sub-1 km boundary for readability while dragging.
    function fmtDragDist(m) {
        if (!Number.isFinite(m) || m < 0) return "—";
        if (m < 1000) return `${Math.round(m / 5) * 5} m`;
        if (m < 10000) return `${(m / 1000).toFixed(2)} km`;
        return `${(m / 1000).toFixed(1)} km`;
    }

    function updateDrawOverlay() {
        if (!drawOverlaySvg) return;
        const rect = drawOverlaySvg.querySelector("rect");
        if (!drawState) {
            rect.setAttribute("width", "0");
            rect.setAttribute("height", "0");
            if (drawDimEl) drawDimEl.classList.remove("visible");
            return;
        }
        const { x0, y0, x1, y1 } = drawState;
        const x = Math.min(x0, x1);
        const y = Math.min(y0, y1);
        const w = Math.abs(x1 - x0);
        const h = Math.abs(y1 - y0);
        rect.setAttribute("x", x);
        rect.setAttribute("y", y);
        rect.setAttribute("width", w);
        rect.setAttribute("height", h);

        // Live dimensions readout, anchored to the cursor (the moving
        // corner of the drag). Convert the screen rect to a lat/lng
        // bbox via the host hook, then to true-ground metres.
        if (!drawDimEl) return;
        if (w < 4 || h < 4) {
            drawDimEl.classList.remove("visible");
            return;
        }
        let widthM = NaN, heightM = NaN;
        if (host && typeof host.screenRectToBbox === "function") {
            const bbox = host.screenRectToBbox(drawState);
            if (bbox) {
                const m = bboxToMetres(bbox);
                widthM = m.widthM;
                heightM = m.heightM;
            }
        }
        if (!Number.isFinite(widthM) || !Number.isFinite(heightM)) {
            drawDimEl.classList.remove("visible");
            return;
        }
        drawDimEl.textContent =
            `${fmtDragDist(widthM)} × ${fmtDragDist(heightM)}`;
        // Anchor near the moving corner with a small offset so the badge
        // doesn't sit underneath the cursor. Flip to the left/above when
        // it would otherwise spill off the viewport edge.
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        // Measure once so the flip math knows the badge dimensions.
        drawDimEl.classList.add("visible");
        const bw = drawDimEl.offsetWidth || 80;
        const bh = drawDimEl.offsetHeight || 24;
        const offset = 14;
        let bx = x1 + offset;
        let by = y1 + offset;
        if (bx + bw > vw - 4) bx = x1 - offset - bw;
        if (by + bh > vh - 4) by = y1 - offset - bh;
        drawDimEl.style.left = `${Math.max(4, bx)}px`;
        drawDimEl.style.top  = `${Math.max(4, by)}px`;
    }


    // ─── Toast (warning) ───────────────────────────────────────────
    function showToast(message, ms = 2800) {
        const t = document.createElement("div");
        t.className = "inspect3d-toast";
        t.textContent = message;
        document.body.appendChild(t);
        // Force layout so the transition fires.
        requestAnimationFrame(() => t.classList.add("visible"));
        setTimeout(() => {
            t.classList.remove("visible");
            setTimeout(() => t.remove(), 220);
        }, ms);
    }


    // ─── Bbox geometry helpers ─────────────────────────────────────
    // The /raster/inspect server uses Web Mercator metres for its NOAA
    // queries, but for the 3D scene we want true ground metres so the
    // surface preserves real proportions. We compute the cos(centerLat)
    // correction here on the client too.
    const R = 6378137.0;
    function bboxToMetres(bbox) {
        const cosLat = Math.cos(((bbox.north + bbox.south) / 2)
                                 * Math.PI / 180);
        const widthM = (bbox.east  - bbox.west)  * (Math.PI / 180) * R * cosLat;
        const heightM = (bbox.north - bbox.south) * (Math.PI / 180) * R;
        return { widthM, heightM };
    }
    function bboxAreaKm2(bbox) {
        const { widthM, heightM } = bboxToMetres(bbox);
        return (widthM / 1000) * (heightM / 1000);
    }
    function fmtKm(m) {
        if (m < 1000) return `${m.toFixed(0)} m`;
        return `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
    }


    // ─── Raster fetch ──────────────────────────────────────────────
    async function fetchInspectRaster(sourceId, bbox, sizePx) {
        const q = new URLSearchParams({
            source: sourceId,
            n: bbox.north.toFixed(6),
            s: bbox.south.toFixed(6),
            e: bbox.east.toFixed(6),
            w: bbox.west.toFixed(6),
            size: String(sizePx),
        });
        const url = `${baseurl}/raster/inspect?${q.toString()}`;
        const resp = await fetch(url, { cache: 'no-store' });
        if (!resp.ok) {
            throw new Error(`raster fetch failed: HTTP ${resp.status}`);
        }
        const buf = await resp.arrayBuffer();
        const dv = new DataView(buf);
        const w        = dv.getUint32(0, true);
        const h        = dv.getUint32(4, true);
        const cellsize_m = dv.getFloat32(8, true);
        // header byte 12 is buffer_px — always 0 here, ignored.
        const data = new Float32Array(buf, 16, w * h);
        return { w, h, cellsize_m, data };
    }


    // ─── Analysis → texture ────────────────────────────────────────
    // We run the same FFAnalyses functions as the 2D worker (analyses.js
    // attaches them to `self`, which here is `window`). NaN-nodata gets
    // alpha=0 so empty pixels in the bbox show as transparent dark
    // patches rather than misleading "shallow" colours.
    function buildTextureCanvas(raster, analysisKey, param, paramExtra) {
        const { w, h, cellsize_m, data } = raster;
        const n = w * h;

        // Coerce NaN → 0 with a parallel nodata mask (matches the
        // worker's pre-analysis step).
        const elev = new Float32Array(n);
        const mask = new Uint8Array(n);
        for (let i = 0; i < n; i++) {
            const v = data[i];
            if (Number.isNaN(v)) { elev[i] = 0; mask[i] = 1; }
            else                  { elev[i] = v; mask[i] = 0; }
        }

        const rgba = new Uint8ClampedArray(4 * n);
        const fn = (window.FFAnalyses && window.FFAnalyses[analysisKey])
                || (window.FFAnalyses && window.FFAnalyses['color-relief']);
        if (fn) {
            fn(elev, mask, w, h, cellsize_m, param, rgba, paramExtra);
            // Punch alpha=0 on nodata (matches worker).
            for (let i = 0; i < n; i++) {
                if (mask[i]) rgba[i * 4 + 3] = 0;
            }
        } else {
            // Fallback if analyses.js failed to load — plain dark fill so
            // the user still sees the relief from lighting.
            for (let i = 0; i < n; i++) {
                rgba[i * 4]     = 60;
                rgba[i * 4 + 1] = 80;
                rgba[i * 4 + 2] = 120;
                rgba[i * 4 + 3] = mask[i] ? 0 : 255;
            }
        }

        // Build the texture canvas as-drawn (raster row 0 = north at the
        // top). Three.js samples textures with v=0 at the bottom, so the
        // north/south orientation is corrected downstream by texture.flipY
        // in setupMesh — we deliberately do NOT pre-flip the pixels here.
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        const img = new ImageData(rgba, w, h);
        ctx.putImageData(img, 0, 0);
        return canvas;
    }


    // ─── Geometry: indexed plane sampling the raster ──────────────
    function buildHeightFieldGeometry(raster, detail, exaggeration,
                                       widthM, heightM) {
        const THREE = window.THREE;
        const cols = detail;     // x
        const rows = detail;     // y
        const geom = new THREE.PlaneGeometry(widthM, heightM,
                                              cols - 1, rows - 1);
        // PlaneGeometry lies in the X/Y plane with +Z up. We want a flat
        // top-down "world": surface is X/Z plane, Y is up. Rotate -90°
        // about X so Y becomes up.
        geom.rotateX(-Math.PI / 2);
        setHeightsOnGeometry(geom, raster, detail, exaggeration,
                              widthM, heightM);
        return geom;
    }

    // Sample the raster at vertex positions and write Y values into the
    // geometry's position buffer. Called both at initial bake AND on
    // exaggeration changes (to avoid rebuilding the whole geometry).
    function setHeightsOnGeometry(geom, raster, detail, exaggeration,
                                   widthM, heightM) {
        const { w, h, data } = raster;
        const pos = geom.attributes.position;
        // The plane is laid out as a rows×cols grid of vertices, row-major
        // top → bottom (after the rotateX, +Z = south, -Z = north).
        // PlaneGeometry's vertex order: y goes from +halfH to -halfH,
        // x goes from -halfW to +halfW. After rotateX(-π/2), the original
        // +Y becomes -Z (north up if we keep that convention). We sample
        // the raster at the matching (row, col) so the surface stays
        // visually aligned with what the 2D map would show.
        const cols = detail, rows = detail;
        for (let r = 0; r < rows; r++) {
            // r=0 → top of plane → north → raster row 0.
            const ry = r / (rows - 1);
            const rasterRow = Math.min(h - 1, Math.floor(ry * h));
            for (let c = 0; c < cols; c++) {
                const rx = c / (cols - 1);
                const rasterCol = Math.min(w - 1, Math.floor(rx * w));
                let v = data[rasterRow * w + rasterCol];
                if (Number.isNaN(v)) v = 0;
                // Index of this vertex in the position buffer.
                const vi = r * cols + c;
                pos.setY(vi, v * exaggeration);
            }
        }
        pos.needsUpdate = true;
        geom.computeVertexNormals();
    }


    // ─── Terrain stats + auto exaggeration ─────────────────────────
    function computeTerrainStats(raster) {
        const { data } = raster;
        let minE = Infinity, maxE = -Infinity;
        for (let i = 0; i < data.length; i++) {
            const v = data[i];
            if (!Number.isFinite(v)) continue;
            if (v < minE) minE = v;
            if (v > maxE) maxE = v;
        }
        if (!Number.isFinite(minE)) { minE = 0; maxE = 0; }
        return { minE, maxE };
    }

    // Auto exag: make the terrain's Z range ≈ AUTO_TARGET_FRACTION of
    // the longer XY dimension. Small areas with mild relief naturally
    // need higher values; deep canyons over wide areas land near 1×.
    function computeAutoExag(s) {
        const range = s.terrainMaxElevM - s.terrainMinElevM;
        if (!Number.isFinite(range) || range <= 0) return DEFAULT_EXAGGERATION;
        const xy = Math.max(s.widthM, s.heightM);
        const targetZ = xy * AUTO_TARGET_FRACTION;
        const raw = targetZ / range;
        return Math.max(1, Math.min(MAX_EXAGGERATION, Math.round(raw)));
    }

    // Compute the Y position of the skirt's base plane (and the bottom
    // of the depth axis). A little below the deepest exaggerated point.
    function computeBaseY(s) {
        const minY = s.terrainMinElevM * s.exaggeration;
        const maxY = s.terrainMaxElevM * s.exaggeration;
        const pad = Math.max((maxY - minY) * SKIRT_PAD_FRACTION, 0.5);
        return minY - pad;
    }


    // ─── Skirt (extruded walls + bottom cap) ───────────────────────
    // Turns the height field from a paper-thin sheet into a solid
    // relief diorama. Walls connect each top-edge vertex straight down
    // to the base plane; bottom cap is a single quad. Uses DoubleSide
    // so winding doesn't have to be perfect.
    function buildSkirtGeometry(s) {
        const THREE = window.THREE;
        const cols = s.detail, rows = s.detail;
        const pos = s.geometry.attributes.position;
        const baseY = s.baseY;
        const halfW = s.widthM / 2;
        const halfH = s.heightM / 2;

        // Pre-allocate: 4 edges × (count-1) quads × 6 indices, plus 1
        // bottom quad. 4 walls share vertex counts (cols == rows here).
        const positions = [];
        const indices = [];

        function pushVert(x, y, z) {
            const i = positions.length / 3;
            positions.push(x, y, z);
            return i;
        }
        function pushQuad(a, b, c, d) {
            indices.push(a, b, c, a, c, d);
        }

        // Walk one edge and build a strip of quads down to baseY.
        function buildEdge(getVi) {
            const n = cols;  // == rows
            // Build alternating top + bottom rows so we share verts
            // across adjacent quads.
            let prevTop = -1, prevBot = -1;
            for (let i = 0; i < n; i++) {
                const vi = getVi(i);
                const x = pos.getX(vi), y = pos.getY(vi), z = pos.getZ(vi);
                const top = pushVert(x, y,     z);
                const bot = pushVert(x, baseY, z);
                if (i > 0) pushQuad(prevTop, top, bot, prevBot);
                prevTop = top; prevBot = bot;
            }
        }

        // North (r=0), c increasing.
        buildEdge(i => 0 * cols + i);
        // East (c=cols-1), r increasing.
        buildEdge(i => i * cols + (cols - 1));
        // South (r=rows-1), c decreasing.
        buildEdge(i => (rows - 1) * cols + ((cols - 1) - i));
        // West (c=0), r decreasing.
        buildEdge(i => ((rows - 1) - i) * cols + 0);

        // Bottom cap — a single quad at baseY.
        const bA = pushVert(-halfW, baseY, -halfH);
        const bB = pushVert(+halfW, baseY, -halfH);
        const bC = pushVert(+halfW, baseY, +halfH);
        const bD = pushVert(-halfW, baseY, +halfH);
        pushQuad(bA, bD, bC, bB);

        const g = new THREE.BufferGeometry();
        g.setAttribute('position',
                       new THREE.Float32BufferAttribute(positions, 3));
        g.setIndex(indices);
        g.computeVertexNormals();
        return g;
    }

    function setupSkirt(s) {
        const THREE = window.THREE;
        s.skirtGeom = buildSkirtGeometry(s);
        s.skirtMat = new THREE.MeshStandardMaterial({
            color: SKIRT_COLOR,
            roughness: 0.95,
            metalness: 0.0,
            side: THREE.DoubleSide,
            flatShading: true,
        });
        s.skirt = new THREE.Mesh(s.skirtGeom, s.skirtMat);
        s.scene.add(s.skirt);
    }

    function rebuildSkirt(s) {
        if (!s.skirt) return;
        const old = s.skirtGeom;
        s.skirtGeom = buildSkirtGeometry(s);
        s.skirt.geometry = s.skirtGeom;
        if (old) old.dispose();
    }


    // ─── Depth axis (the side ruler) ───────────────────────────────
    // A vertical line at the NW corner with ticks at "nice" depth
    // intervals in feet, plus a "Surface" label at y=0. Provides
    // depth context independent of camera framing or exag.
    function pickTickStepFt(maxDepthFt) {
        if (maxDepthFt <= 0) return 10;
        const targetSteps = 5;
        const raw = maxDepthFt / targetSteps;
        const exp = Math.floor(Math.log10(raw));
        const f = raw / Math.pow(10, exp);
        let nice;
        if (f < 1.5)      nice = 1;
        else if (f < 3.5) nice = 2;
        else if (f < 7.5) nice = 5;
        else              nice = 10;
        return Math.max(1, nice * Math.pow(10, exp));
    }

    function makeAxisLabelCanvas(text) {
        const measure = document.createElement("canvas").getContext("2d");
        measure.font = "bold 28px Inter, sans-serif";
        const w = Math.ceil(measure.measureText(text).width + 24);
        const h = 44;
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.font = "bold 28px Inter, sans-serif";
        ctx.textBaseline = "middle";
        ctx.textAlign = "center";
        ctx.fillStyle = "rgba(7, 8, 12, 0.82)";
        ctx.fillRect(0, 0, w, h);
        ctx.strokeStyle = "rgba(158, 182, 208, 0.55)";
        ctx.lineWidth = 1.5;
        ctx.strokeRect(0.75, 0.75, w - 1.5, h - 1.5);
        ctx.fillStyle = "#dbe6f3";
        ctx.fillText(text, w / 2, h / 2 + 1);
        return canvas;
    }

    function setupDepthAxis(s) {
        const THREE = window.THREE;
        s.axis = new THREE.Group();
        s.scene.add(s.axis);
        rebuildDepthAxis(s);
    }

    function rebuildDepthAxis(s) {
        if (!s.axis) return;
        const THREE = window.THREE;

        // Wipe previous children — their geometries/materials all need
        // to be disposed because we rebuild from scratch on every exag
        // change (tick positions in scene-Y depend on exag).
        while (s.axis.children.length) {
            const c = s.axis.children.pop();
            if (c.geometry) c.geometry.dispose();
            if (c.material) {
                if (c.material.map) c.material.map.dispose();
                c.material.dispose();
            }
        }

        const halfW = s.widthM / 2;
        const halfH = s.heightM / 2;
        const span = Math.max(s.widthM, s.heightM);
        const margin = span * 0.05;
        const tickLen = margin * 0.6;
        // Place the axis just outside the NW corner.
        const x = -halfW - margin;
        const z = -halfH - margin;

        const exag = s.exaggeration;
        const yBottom = s.baseY;
        const topPadM = Math.max((s.terrainMaxElevM - s.terrainMinElevM) * 0.05,
                                  1);
        const yTop = Math.max(0, s.terrainMaxElevM * exag) + topPadM * exag;

        const maxDepthM = Math.max(0, -s.terrainMinElevM);
        const maxDepthFt = maxDepthM * 3.28084;
        const stepFt = pickTickStepFt(maxDepthFt);

        // Build line segments: main axis + ticks.
        const seg = [];
        seg.push(new THREE.Vector3(x, yBottom, z),
                 new THREE.Vector3(x, yTop,    z));
        // Surface tick (depth 0).
        seg.push(new THREE.Vector3(x, 0, z),
                 new THREE.Vector3(x - tickLen, 0, z - tickLen));
        const deepestY = s.terrainMinElevM * exag;
        // Deepest tick.
        if (maxDepthFt > 0.5) {
            seg.push(new THREE.Vector3(x, deepestY, z),
                     new THREE.Vector3(x - tickLen, deepestY, z - tickLen));
        }
        // Intermediate ticks.
        const interTicks = [];
        for (let dFt = stepFt; dFt < maxDepthFt - stepFt * 0.5; dFt += stepFt) {
            const dM = dFt / 3.28084;
            const yT = -dM * exag;
            seg.push(new THREE.Vector3(x, yT, z),
                     new THREE.Vector3(x - tickLen * 0.7,
                                       yT,
                                       z - tickLen * 0.7));
            interTicks.push({ ft: dFt, y: yT });
        }
        const lineGeom = new THREE.BufferGeometry().setFromPoints(seg);
        const lineMat  = new THREE.LineBasicMaterial({ color: AXIS_COLOR });
        const lines = new THREE.LineSegments(lineGeom, lineMat);
        lines.renderOrder = 3;
        s.axis.add(lines);

        // Labels.
        const labelHeight = span * 0.025;
        function addLabel(text, yPos) {
            const canvas = makeAxisLabelCanvas(text);
            const tex = new THREE.CanvasTexture(canvas);
            if (THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
            tex.needsUpdate = true;
            const mat = new THREE.SpriteMaterial({
                map: tex,
                transparent: true,
                depthTest: false,
            });
            const sprite = new THREE.Sprite(mat);
            const aspect = canvas.width / canvas.height;
            sprite.scale.set(labelHeight * aspect, labelHeight, 1);
            sprite.position.set(
                x - tickLen - labelHeight * aspect * 0.55,
                yPos,
                z - tickLen,
            );
            sprite.renderOrder = 4;
            s.axis.add(sprite);
        }
        addLabel("Surface", 0);
        for (const t of interTicks) addLabel(`${t.ft} ft`, t.y);
        if (maxDepthFt > 0.5) {
            addLabel(`${Math.round(maxDepthFt)} ft`, deepestY);
        }
    }


    // Sample the raster at a scene-space XZ position. Used by VE update
    // to snap the orbit target to the new exaggerated seafloor at the
    // panned XZ. Clamps to the bbox so panning past the edge stays on
    // the nearest cell instead of dropping to zero / NaN.
    function sampleElevAtSceneXZ(s, sceneX, sceneZ) {
        if (!s.raster) return 0;
        const halfW = s.widthM / 2;
        const halfH = s.heightM / 2;
        let fracX = (sceneX + halfW) / s.widthM;
        let fracZ = (sceneZ + halfH) / s.heightM;
        fracX = Math.max(0, Math.min(1, fracX));
        fracZ = Math.max(0, Math.min(1, fracZ));
        const { w, h, data } = s.raster;
        const r = Math.min(h - 1, Math.floor(fracZ * h));
        const c = Math.min(w - 1, Math.floor(fracX * w));
        const v = data[r * w + c];
        if (Number.isFinite(v)) return v;
        return 0;
    }

    // Centroid of the exaggerated *terrain* alone (no skirt, no y=0).
    // Used as the orbit pivot so zoom-in moves toward the seafloor —
    // `computeSceneBoundingBox` includes y=0 and baseY, which biases
    // the centre off the terrain at high exag and pulls the camera
    // into open space on zoom.
    function computeTerrainCenter(s) {
        const THREE = window.THREE;
        s.geometry.computeBoundingBox();
        const c = new THREE.Vector3();
        s.geometry.boundingBox.getCenter(c);
        return c;
    }


    // ─── Apply an exaggeration change ──────────────────────────────
    // Single chokepoint for VE updates: triggered by the slider, by the
    // Auto button, and by Auto on initial open. Recomputes heights,
    // rebuilds the skirt + axis, re-snaps the orbit target to the
    // (now stretched) seafloor at the user's current pan XZ, and
    // resizes the scale legend.
    function applyExaggeration(s) {
        if (!s.geometry || !s.raster) return;
        setHeightsOnGeometry(s.geometry, s.raster, s.detail,
                              s.exaggeration, s.widthM, s.heightM);
        s.baseY = computeBaseY(s);
        rebuildSkirt(s);
        rebuildDepthAxis(s);
        // Pivot Y has to follow the stretched terrain or zoom drifts
        // off into open space. Preserve the user's panned XZ; only Y
        // moves. recenterCameraTo translates camera+target together so
        // the viewpoint angle/distance is unchanged.
        if (s.camera && s.controls) {
            const THREE = window.THREE;
            const tx = s.controls.target.x;
            const tz = s.controls.target.z;
            const ty = sampleElevAtSceneXZ(s, tx, tz) * s.exaggeration;
            recenterCameraTo(s, new THREE.Vector3(tx, ty, tz));
        }
        updateLegend(s);
    }


    // ─── Mesh setup ────────────────────────────────────────────────
    function setupMesh(s) {
        const THREE = window.THREE;
        s.geometry = buildHeightFieldGeometry(
            s.raster, s.detail, s.exaggeration, s.widthM, s.heightM);

        const canvas = buildTextureCanvas(
            s.raster, s.analysisKey, s.param, s.paramExtra);
        s.texture = new THREE.CanvasTexture(canvas);
        s.texture.colorSpace = THREE.SRGBColorSpace || s.texture.colorSpace;
        s.texture.flipY = true;  // raster row 0 = north = +Z side of plane
        s.texture.magFilter = THREE.LinearFilter;
        s.texture.minFilter = THREE.LinearMipmapLinearFilter;
        s.texture.generateMipmaps = true;
        s.texture.anisotropy = Math.min(8, s.renderer.capabilities.getMaxAnisotropy());
        s.texture.needsUpdate = true;

        s.material = new THREE.MeshStandardMaterial({
            map: s.texture,
            transparent: true,
            roughness: 0.78,
            metalness: 0.0,
            side: THREE.DoubleSide,
        });
        s.mesh = new THREE.Mesh(s.geometry, s.material);
        s.scene.add(s.mesh);
    }


    // ─── Scene + renderer setup ────────────────────────────────────
    function setupScene(s) {
        const THREE = window.THREE;
        const wrap = $("inspect3d-canvas-wrap");

        const W = wrap.clientWidth;
        const H = wrap.clientHeight;

        s.renderer = new THREE.WebGLRenderer({
            antialias: true,
            alpha: false,
            powerPreference: "high-performance",
        });
        s.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
        s.renderer.setSize(W, H, false);
        // Output colour space — Three.js r149 calls it outputEncoding,
        // newer is outputColorSpace. Set both defensively.
        if ('outputColorSpace' in s.renderer) {
            s.renderer.outputColorSpace = THREE.SRGBColorSpace;
        } else if ('outputEncoding' in s.renderer) {
            s.renderer.outputEncoding = THREE.sRGBEncoding;
        }
        wrap.appendChild(s.renderer.domElement);

        s.scene = new THREE.Scene();
        s.scene.background = new THREE.Color(0x07080c);

        // Initial camera. The actual position + look target are written
        // by frameCamera() once the geometry exists; the constructor
        // values just give it a sane aspect/FOV so frameCamera can read
        // them back. Near/far are placeholders, also rewritten later.
        const span = Math.max(s.widthM, s.heightM);
        const fov = 45;
        s.camera = new THREE.PerspectiveCamera(fov, W / H,
                                                Math.max(0.1, span / 1000),
                                                span * 50);

        s.ambient = new THREE.AmbientLight(0xb0c4d4, 0.45);
        s.scene.add(s.ambient);

        s.light = new THREE.DirectionalLight(0xfff2d6, 1.05);
        positionLight(s.light, s.lightDeg, span);
        s.scene.add(s.light);

        s.controls = new THREE.OrbitControls(s.camera, s.renderer.domElement);
        s.controls.enableDamping = true;
        s.controls.dampingFactor = 0.08;
        s.controls.minDistance = span * 0.02;
        s.controls.maxDistance = span * 10;
        // No polar-angle clamp — user can orbit fully, including under the
        // terrain to see the underside of the relief block.
        s.controls.minPolarAngle = 0;
        s.controls.maxPolarAngle = Math.PI;

        // Pan + zoom-to-cursor. screenSpacePanning=false keeps pan in the
        // world horizontal (XZ) plane — screen-space panning on a tilted
        // camera is disorienting on a height-field. zoomToCursor (r146+)
        // dollies toward the world point under the cursor instead of the
        // screen centre.
        s.controls.enablePan = true;
        s.controls.screenSpacePanning = false;
        if ('zoomToCursor' in s.controls) s.controls.zoomToCursor = true;
        // Default OrbitControls binds MIDDLE to DOLLY; remap to PAN so
        // middle-drag pans (matches DCC / GIS convention). RIGHT already
        // pans by default.
        s.controls.mouseButtons = {
            LEFT:   THREE.MOUSE.ROTATE,
            MIDDLE: THREE.MOUSE.PAN,
            RIGHT:  THREE.MOUSE.PAN,
        };

        // Trackpad-friendly pan: hold Shift to make left-drag pan.
        // OrbitControls reads mouseButtons.LEFT on pointerdown, so we
        // swap the binding on shift-down and restore on shift-up.
        s.onShiftDown = (e) => {
            if (e.key === 'Shift' && s.controls) {
                s.controls.mouseButtons.LEFT = THREE.MOUSE.PAN;
            }
        };
        s.onShiftUp = (e) => {
            if (e.key === 'Shift' && s.controls) {
                s.controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
            }
        };
        window.addEventListener('keydown', s.onShiftDown);
        window.addEventListener('keyup',   s.onShiftUp);

        // The 'change' event fires on every controls.update(). The
        // legend depends on the full camera pose (pan / rotate / zoom
        // all change how 1.7 m projects), so re-size it on each event
        // — but cheaply: updateLegend is a couple of matrix multiplies
        // and one style write, well under a frame.
        s.onControlsChange = () => {
            if (!session || session !== s) return;
            updateLegend(s);
        };
        s.controls.addEventListener('change', s.onControlsChange);

        // Resize handler — bound here so close() can remove it cleanly.
        s.onResize = () => {
            if (!session) return;
            const ww = wrap.clientWidth;
            const hh = wrap.clientHeight;
            if (ww === 0 || hh === 0) return;
            s.renderer.setSize(ww, hh, false);
            s.camera.aspect = ww / hh;
            s.camera.updateProjectionMatrix();
            updateLegend(s);
        };
        window.addEventListener("resize", s.onResize);
    }

    // Map azimuth (degrees, 0=N, 90=E) + an elevation of ~55° to a
    // directional-light position vector at `span * 1.5` distance from
    // the origin.
    function positionLight(light, azimuthDeg, span) {
        const az  = (azimuthDeg + 180) * Math.PI / 180;   // viewing convention
        const el  = 55 * Math.PI / 180;
        const d   = span * 1.5;
        const r   = d * Math.cos(el);
        light.position.set(
            r * Math.sin(az),
            d * Math.sin(el),
            r * Math.cos(az),
        );
        light.target.position.set(0, 0, 0);
    }


    // ─── Camera framing ────────────────────────────────────────────
    // Aim the camera at the *actual* centre of the mesh's bounding box
    // and back it off far enough to fit the bounding sphere (with
    // padding) into the current FOV.
    //
    // This matters because the mesh's Y range is the elevation values
    // (negative for sub-sea depths) — not centred on Y=0. An older
    // version of this file targeted the origin, which left the seafloor
    // dangling below the camera target and pushed the geometry into the
    // bottom-left of the frame. Targeting the bbox centre fixes that.
    function frameCamera(s) {
        const THREE = window.THREE;
        if (!s.geometry || !s.camera || !s.controls) return;

        // Pivot = centroid of the exaggerated *terrain* bbox so zoom-in
        // tracks the seafloor (not a point biased toward y=0).
        const target = computeTerrainCenter(s);

        // Framing radius uses terrain + skirt (and y=0 for the Surface
        // tick) so nothing visible clips out of view.
        const bb = computeSceneBoundingBox(s);
        const sphere = new THREE.Sphere();
        bb.getBoundingSphere(sphere);
        // Guard against a degenerate (zero-radius) sphere when the
        // raster is completely flat — fall back to the bbox half-span.
        const span = Math.max(s.widthM, s.heightM);
        const r = Math.max(sphere.radius, span * 0.5) * FRAME_PADDING;

        const fovV = s.camera.fov * Math.PI / 180;
        const aspect = s.camera.aspect || 1;
        const fovH = 2 * Math.atan(Math.tan(fovV / 2) * aspect);
        // Smaller FOV is the binding constraint when fitting a sphere.
        const fovMin = Math.min(fovV, fovH);
        const dist = r / Math.sin(fovMin / 2);

        const az = FRAME_AZIMUTH_DEG * Math.PI / 180;
        const el = FRAME_ELEVATION_DEG * Math.PI / 180;
        s.camera.position.set(
            target.x + dist * Math.cos(el) * Math.sin(az),
            target.y + dist * Math.sin(el),
            target.z + dist * Math.cos(el) * Math.cos(az),
        );

        s.controls.target.copy(target);

        // Resize near/far around the new distance so a deep + steeply
        // exaggerated mesh doesn't get clipped on either end.
        s.camera.near = Math.max(0.05, dist / 5000);
        s.camera.far  = Math.max(span * 50, dist * 10);
        s.camera.updateProjectionMatrix();
        s.controls.update();
        // Reset View changes the camera pose; the on-screen legend
        // depends on it.
        updateLegend(s);
    }

    // Compute the bounding box used for framing — includes the terrain,
    // y=0 (where the depth axis's Surface tick sits), and the skirt's
    // base (s.baseY). The raw geometry bbox alone leaves the Surface
    // tick off-screen at high exag because the seafloor's Y range may
    // not span 0.
    function computeSceneBoundingBox(s) {
        const THREE = window.THREE;
        s.geometry.computeBoundingBox();
        const bb = s.geometry.boundingBox.clone();
        bb.min.y = Math.min(bb.min.y, 0, s.baseY ?? bb.min.y);
        bb.max.y = Math.max(bb.max.y, 0);
        return bb;
    }

    // VE-change camera update: translate position + target by the delta
    // between the old and new bbox centres so the orbit angle and
    // distance are preserved. Keeps the user's current viewpoint while
    // the terrain reshapes itself underneath. Near/far are tightened to
    // the new distance so a tall stretched mesh isn't clipped.
    function recenterCameraTo(s, newCenter) {
        const offset = s.camera.position.clone().sub(s.controls.target);
        s.controls.target.copy(newCenter);
        s.camera.position.copy(newCenter).add(offset);
        const dist = s.camera.position.distanceTo(s.controls.target);
        const span = Math.max(s.widthM, s.heightM);
        s.camera.near = Math.max(0.05, dist / 5000);
        s.camera.far  = Math.max(span * 50, dist * 10);
        s.camera.updateProjectionMatrix();
        s.controls.update();
    }


    // ─── Scale legend (screen-space DOM overlay) ───────────────────
    // The legend is NOT a 3D object — it lives in the DOM, pinned to the
    // bottom-left of the viewport. We size the silhouette + bar each
    // frame to whatever 1.7 m projects to at the terrain centroid for
    // the current camera + exaggeration. That makes it grow when the
    // user zooms in (where contour features get bigger too) and shrink
    // on zoom-out, so a side-by-side comparison stays meaningful.
    //
    // Projection: take two world-space points (centroid_x, centroid_y,
    // centroid_z) and the same point 1.7×exag metres higher, project
    // both through the camera, take the pixel distance between them.
    function setupLegend(s) {
        s.legendEl        = $("inspect3d-legend");
        s.legendGraphicEl = $("inspect3d-legend-graphic");
        s.legendLabelEl   = $("inspect3d-legend-label");
        if (s.legendEl) {
            s.legendEl.classList.toggle("hidden", !s.showFigure);
        }
        updateLegend(s);
    }

    function updateLegend(s) {
        if (!s.legendEl || !s.legendGraphicEl || !s.legendLabelEl) return;
        if (!s.showFigure) {
            s.legendEl.classList.add("hidden");
            return;
        }
        s.legendEl.classList.remove("hidden");

        if (!s.camera || !s.renderer || !s.geometry) {
            // Not enough state yet; leave the last size in place.
            return;
        }

        const THREE = window.THREE;
        const centroid = computeTerrainCenter(s);
        const above = new THREE.Vector3(
            centroid.x,
            centroid.y + FIGURE_HEIGHT_M * s.exaggeration,
            centroid.z,
        );
        // .project() needs the camera's matrices to be current. The
        // controls.update() inside the render loop handles that, but
        // when we tick from an event (controls.change, exaggeration
        // slider) the camera state is also current — OrbitControls
        // mutates camera.matrix* synchronously.
        const pNDC  = centroid.clone().project(s.camera);
        const pHigh = above.project(s.camera);
        const viewH = s.renderer.domElement.clientHeight || 1;

        // NDC y is in [-1, 1] with +y up. Pixel distance from a Δy in
        // NDC is |Δy| * viewportHeight / 2.
        let px = Math.abs(pNDC.y - pHigh.y) * viewH * 0.5;
        if (!Number.isFinite(px)) px = LEGEND_MIN_PX;

        const maxPx = Math.max(LEGEND_MIN_PX,
                                Math.floor(viewH * LEGEND_MAX_VH_FRAC));
        let clipped = false;
        if (px < LEGEND_MIN_PX) px = LEGEND_MIN_PX;
        if (px > maxPx) { px = maxPx; clipped = true; }

        s.legendGraphicEl.style.height = `${Math.round(px)}px`;
        const exagTxt = `${s.exaggeration}×`;
        const baseLbl = `1.7 m · 5'7" @ ${exagTxt}`;
        s.legendLabelEl.textContent = clipped ? `${baseLbl} (clipped)` : baseLbl;
    }


    // ─── Animation loop ────────────────────────────────────────────
    function startLoop(s) {
        function tick() {
            if (!session || session !== s) return;
            s.controls.update();
            s.renderer.render(s.scene, s.camera);
            s.raf = requestAnimationFrame(tick);
        }
        s.raf = requestAnimationFrame(tick);
    }


    // ─── Public: open / close ──────────────────────────────────────
    async function open(opts) {
        if (session) close();   // single-instance modal

        if (!window.THREE) {
            showToast("Three.js failed to load — 3D inspector unavailable.");
            return;
        }

        // Bbox sanity / size guard.
        const area = bboxAreaKm2(opts.bbox);
        if (!Number.isFinite(area) || area <= 0) {
            showToast("Invalid rectangle.");
            return;
        }
        if (area > MAX_AREA_KM2) {
            showToast(
                `Rectangle too large (${area.toFixed(1)} km²). ` +
                `Max ${MAX_AREA_KM2} km² for the 3D inspector — draw a smaller area.`,
                4000,
            );
            return;
        }

        const { widthM, heightM } = bboxToMetres(opts.bbox);

        const s = {
            bbox:        opts.bbox,
            sourceId:    opts.sourceId,
            analysisKey: opts.analysisKey,
            param:       opts.param,
            paramExtra:  opts.paramExtra || null,
            raster:      null,
            detail:       DEFAULT_DETAIL,
            exaggeration: DEFAULT_EXAGGERATION,
            autoMode:     true,
            lightDeg:     DEFAULT_LIGHT_DEG,
            fetchSize:    DEFAULT_FETCH_SIZE,
            widthM, heightM,
            terrainMinElevM: 0,
            terrainMaxElevM: 0,
            baseY:        0,
            showFigure:   true,
        };
        session = s;

        // Reflect modal UI before async work — the user sees immediate
        // feedback that their drag landed.
        showModal();
        setStatus("Loading high-resolution bathymetry…", false);
        updateExtentReadout(s);
        resetControlsToDefaults(s);

        // Fetch the raster, then build the scene. If the user closes the
        // modal mid-fetch, `session` is null and we bail.
        let raster;
        try {
            raster = await fetchInspectRaster(
                s.sourceId, s.bbox, s.fetchSize);
        } catch (err) {
            if (session !== s) return;
            console.error("[inspector3d] fetch failed:", err);
            setStatus(
                "Failed to load bathymetry for this area. " +
                "The data source may not have coverage here.", true);
            return;
        }
        if (session !== s) return;
        s.raster = raster;

        // Now that we have the raster, compute the elevation extremes
        // (needed by auto-exag, the skirt base, and the depth axis).
        const stats = computeTerrainStats(s.raster);
        s.terrainMinElevM = stats.minE;
        s.terrainMaxElevM = stats.maxE;
        if (s.autoMode) {
            s.exaggeration = computeAutoExag(s);
        }
        s.baseY = computeBaseY(s);
        // Reflect the auto-computed value back to the slider UI.
        resetControlsToDefaults(s);

        // Build scene + mesh. Hide the loading text once the renderer
        // is on screen.
        try {
            setupScene(s);
            setupMesh(s);
            setupSkirt(s);
            setupDepthAxis(s);
            setupLegend(s);
            // Frame after everything is in the scene so the camera fits
            // the actual mesh bbox (not the placeholder constructor pose).
            frameCamera(s);
        } catch (err) {
            console.error("[inspector3d] scene build failed:", err);
            setStatus(`3D render failed: ${err.message || err}`, true);
            return;
        }
        setStatus(null, false);
        startLoop(s);
    }

    function close() {
        const s = session;
        session = null;          // signals the animation loop to bail
        hideModal();
        if (!s) return;

        if (s.raf) cancelAnimationFrame(s.raf);
        if (s.onResize) window.removeEventListener("resize", s.onResize);
        if (s.onShiftDown) window.removeEventListener("keydown", s.onShiftDown);
        if (s.onShiftUp)   window.removeEventListener("keyup",   s.onShiftUp);
        if (s.controls && s.onControlsChange) {
            s.controls.removeEventListener("change", s.onControlsChange);
        }
        if (s.controls) s.controls.dispose();
        if (s.mesh && s.scene) s.scene.remove(s.mesh);
        if (s.geometry) s.geometry.dispose();
        if (s.material) s.material.dispose();
        if (s.texture) s.texture.dispose();
        if (s.skirt && s.scene) s.scene.remove(s.skirt);
        if (s.skirtGeom) s.skirtGeom.dispose();
        if (s.skirtMat) s.skirtMat.dispose();
        if (s.axis && s.scene) {
            s.scene.remove(s.axis);
            for (const c of s.axis.children) {
                if (c.geometry) c.geometry.dispose();
                if (c.material) {
                    if (c.material.map) c.material.map.dispose();
                    c.material.dispose();
                }
            }
        }
        // Hide the legend overlay so it doesn't sit on top of the
        // basemap underneath the modal until the next open() reshows it.
        if (s.legendEl) s.legendEl.classList.add("hidden");
        if (s.renderer) {
            // Forces the WebGL context to release immediately — important
            // because the browser caps concurrent contexts.
            s.renderer.dispose();
            const dom = s.renderer.domElement;
            if (dom && dom.parentNode) dom.parentNode.removeChild(dom);
        }
        // Drop the raster reference — large Float32Array, ~1-4 MB.
        s.raster = null;
    }


    // ─── Modal show/hide + status text ─────────────────────────────
    function showModal() {
        const m = $("inspect3d-modal");
        if (!m) return;
        m.classList.remove("hidden");
        m.setAttribute("aria-hidden", "false");
    }
    function hideModal() {
        const m = $("inspect3d-modal");
        if (!m) return;
        m.classList.add("hidden");
        m.setAttribute("aria-hidden", "true");
        // Also clear the canvas-wrap children so a future open() starts
        // from a clean slate.
        const wrap = $("inspect3d-canvas-wrap");
        if (wrap) {
            // Remove any canvas elements while leaving the status div in place.
            for (const child of Array.from(wrap.children)) {
                if (child.tagName === "CANVAS") child.remove();
            }
        }
        setStatus("Loading high-resolution bathymetry…", false);
    }
    function setStatus(msg, isError) {
        const el = $("inspect3d-status");
        if (!el) return;
        if (msg == null) { el.classList.add("hidden"); return; }
        el.classList.remove("hidden");
        el.classList.toggle("error", !!isError);
        el.textContent = msg;
    }

    function updateExtentReadout(s) {
        const el = $("inspect3d-extent");
        if (!el) return;
        const area = bboxAreaKm2(s.bbox);
        el.textContent =
            `${fmtKm(s.widthM)} × ${fmtKm(s.heightM)}` +
            ` · ${area.toFixed(area < 1 ? 2 : 1)} km²` +
            ` · centre ${((s.bbox.north + s.bbox.south) / 2).toFixed(4)}°, ` +
            `${((s.bbox.east + s.bbox.west) / 2).toFixed(4)}°`;
    }


    // ─── Controls ──────────────────────────────────────────────────
    function resetControlsToDefaults(s) {
        const $exag    = $("inspect3d-exag");
        const $exagV   = $("inspect3d-exag-val");
        const $auto    = $("inspect3d-auto-btn");
        const $det     = $("inspect3d-detail");
        const $detV    = $("inspect3d-detail-val");
        const $light   = $("inspect3d-light");
        const $lightV  = $("inspect3d-light-val");
        const $figure  = $("inspect3d-show-figure");
        if ($exag)   $exag.value  = String(s.exaggeration);
        if ($exagV)  $exagV.textContent = `${s.exaggeration}×`;
        if ($auto)   $auto.classList.toggle("active", !!s.autoMode);
        if ($det)    $det.value   = String(s.detail);
        if ($detV)   $detV.textContent = `${s.detail}²`;
        if ($light)  $light.value = String(s.lightDeg);
        if ($lightV) $lightV.textContent = `${s.lightDeg}°`;
        if ($figure) $figure.checked = !!s.showFigure;
    }


    // ─── Public: rectangle-draw mode ───────────────────────────────
    // Called by map.js when the FAB is clicked. While armed, a
    // crosshair cursor sits over the map and the first mousedown
    // anywhere outside the chrome starts the drag.

    function isOverChrome(target) {
        if (!target) return false;
        // Anything inside a top-level panel/button bar should not start a
        // draw — the user is interacting with the controls, not the map.
        return target.closest(
            ".panel, .topbar, .esri-ui, .inspect3d-modal, " +
            "#spotfinder-fab, #inspect3d-fab, #sf-overlay-toggle-fab, " +
            "#depth-card, .panel-toggle"
        );
    }

    function startDrawMode() {
        if (drawMode) return;
        drawMode = true;
        const ws = document.querySelector(".workspace");
        if (ws) ws.classList.add("inspect3d-drawing");
        const fab = $("inspect3d-fab");
        if (fab) fab.classList.add("active");
        ensureDrawOverlay();
        // Pointer handlers stay attached to window so the user can
        // release outside the map without losing the up event.
        window.addEventListener("pointerdown", onPointerDown, true);
        window.addEventListener("pointermove", onPointerMove, true);
        window.addEventListener("pointerup",   onPointerUp,   true);
        window.addEventListener("keydown",     onKeyDown,     true);
    }

    function exitDrawMode() {
        if (!drawMode) return;
        drawMode = false;
        drawState = null;
        const ws = document.querySelector(".workspace");
        if (ws) ws.classList.remove("inspect3d-drawing");
        const fab = $("inspect3d-fab");
        if (fab) fab.classList.remove("active");
        removeDrawOverlay();
        window.removeEventListener("pointerdown", onPointerDown, true);
        window.removeEventListener("pointermove", onPointerMove, true);
        window.removeEventListener("pointerup",   onPointerUp,   true);
        window.removeEventListener("keydown",     onKeyDown,     true);
    }

    function onPointerDown(e) {
        if (!drawMode) return;
        if (e.button !== 0) return;
        if (isOverChrome(e.target)) return;
        // Capture so subsequent move/up land on us even if the cursor
        // leaves the original element (e.g. moves over the ArcGIS
        // canvas, which has its own listeners).
        e.preventDefault();
        e.stopPropagation();
        drawState = { x0: e.clientX, y0: e.clientY,
                      x1: e.clientX, y1: e.clientY };
        updateDrawOverlay();
    }

    function onPointerMove(e) {
        if (!drawMode || !drawState) return;
        e.preventDefault();
        e.stopPropagation();
        drawState.x1 = e.clientX;
        drawState.y1 = e.clientY;
        updateDrawOverlay();
    }

    function onPointerUp(e) {
        if (!drawMode) return;
        if (!drawState) { /* click without drag — just stay in draw mode */ return; }
        e.preventDefault();
        e.stopPropagation();
        const ds = drawState;
        drawState = null;
        const dx = Math.abs(ds.x1 - ds.x0);
        const dy = Math.abs(ds.y1 - ds.y0);
        // Tiny drag = treated as a click — keep draw mode armed so the
        // user can try again. (Avoids opening a useless modal for a
        // single-pixel rectangle.)
        if (dx < 8 || dy < 8) {
            updateDrawOverlay();
            return;
        }
        // Convert screen rectangle → lat/lng bbox via the host hook.
        const bbox = host && host.screenRectToBbox(ds);
        exitDrawMode();
        if (!bbox) {
            showToast("Couldn't convert that rectangle to map coordinates.");
            return;
        }
        // Hand off to the host so it can supply the live config
        // (source/analysis/param) and call open() on us.
        if (host && host.onRectangleDrawn) host.onRectangleDrawn(bbox);
    }

    function onKeyDown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            if (drawState) {
                drawState = null;
                updateDrawOverlay();
            }
            exitDrawMode();
        }
    }


    // ─── Modal-level key handler ───────────────────────────────────
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (!session) return;
        const m = $("inspect3d-modal");
        if (!m || m.classList.contains("hidden")) return;
        e.preventDefault();
        close();
    });


    // ─── Controls wiring (delegated to once per page load) ─────────
    function wireControlsOnce() {
        if (wireControlsOnce._done) return;
        wireControlsOnce._done = true;

        const $close   = $("inspect3d-close");
        const $backdrop = $("inspect3d-backdrop");
        const $exag    = $("inspect3d-exag");
        const $exagV   = $("inspect3d-exag-val");
        const $auto    = $("inspect3d-auto-btn");
        const $det     = $("inspect3d-detail");
        const $detV    = $("inspect3d-detail-val");
        const $light   = $("inspect3d-light");
        const $lightV  = $("inspect3d-light-val");
        const $reset   = $("inspect3d-reset");
        const $figure  = $("inspect3d-show-figure");

        if ($close)    $close.addEventListener("click", close);
        if ($backdrop) $backdrop.addEventListener("click", close);

        if ($exag) $exag.addEventListener("input", (e) => {
            const v = parseInt(e.target.value, 10);
            if ($exagV) $exagV.textContent = `${v}×`;
            const s = session;
            if (!s || !s.geometry || !s.raster) return;
            // User dragged the slider — they're overriding Auto.
            if (s.autoMode) {
                s.autoMode = false;
                if ($auto) $auto.classList.remove("active");
            }
            s.exaggeration = v;
            applyExaggeration(s);
        });

        if ($auto) $auto.addEventListener("click", () => {
            const s = session;
            if (!s) return;
            // Toggle. When turning Auto on, recompute and apply
            // immediately so the user sees the change.
            s.autoMode = !s.autoMode;
            $auto.classList.toggle("active", s.autoMode);
            if (s.autoMode && s.raster) {
                s.exaggeration = computeAutoExag(s);
                if ($exag)  $exag.value = String(s.exaggeration);
                if ($exagV) $exagV.textContent = `${s.exaggeration}×`;
                applyExaggeration(s);
            }
        });

        // Detail slider commits on `change` (release) not `input` — a
        // mid-drag rebuild stalls the frame loop and the UI feels janky.
        if ($det) {
            $det.addEventListener("input", (e) => {
                const v = parseInt(e.target.value, 10);
                if ($detV) $detV.textContent = `${v}²`;
            });
            $det.addEventListener("change", (e) => {
                const v = parseInt(e.target.value, 10);
                const s = session;
                if (!s || !s.raster) return;
                s.detail = v;
                rebuildGeometry(s);
                // Skirt walls reference edge vertices of the height
                // field — a new vertex count means a new skirt.
                rebuildSkirt(s);
            });
        }

        if ($light) $light.addEventListener("input", (e) => {
            const v = parseInt(e.target.value, 10);
            if ($lightV) $lightV.textContent = `${v}°`;
            const s = session;
            if (!s || !s.light) return;
            s.lightDeg = v;
            positionLight(s.light, s.lightDeg,
                          Math.max(s.widthM, s.heightM));
        });

        if ($reset) $reset.addEventListener("click", () => {
            const s = session;
            if (!s || !s.controls) return;
            // Same framing the modal lands on at open — bounding-box
            // centre, fitted distance, default azimuth/elevation.
            frameCamera(s);
        });

        if ($figure) $figure.addEventListener("change", (e) => {
            const s = session;
            const on = !!e.target.checked;
            if (!s) return;
            s.showFigure = on;
            updateLegend(s);
        });
    }

    function rebuildGeometry(s) {
        if (!s.mesh) return;
        const THREE = window.THREE;
        const oldGeom = s.geometry;
        s.geometry = buildHeightFieldGeometry(
            s.raster, s.detail, s.exaggeration, s.widthM, s.heightM);
        s.mesh.geometry = s.geometry;
        if (oldGeom) oldGeom.dispose();
    }


    // ─── Public registration hook ──────────────────────────────────
    // map.js calls this once at boot to install the screen→bbox helper
    // and the post-draw callback. Keeping the geometry math in map.js
    // means this module doesn't have to depend on the ArcGIS API
    // directly — it stays a pure Three.js + DOM module.
    function registerHost(h) {
        host = h;
        wireControlsOnce();
    }


    // ─── Module export ─────────────────────────────────────────────
    window.FishFinderInspector3D = {
        open,
        close,
        startDrawMode,
        exitDrawMode,
        registerHost,
    };

    // Auto-wire controls when DOM is ready — the FAB click handler in
    // map.js calls startDrawMode(), which doesn't depend on host being
    // registered, but the modal Close button does.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wireControlsOnce);
    } else {
        wireControlsOnce();
    }
})();
