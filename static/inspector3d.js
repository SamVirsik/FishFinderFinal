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
    const DEFAULT_EXAGGERATION = 8;      // higher than 2D map default
    const DEFAULT_LIGHT_DEG   = 315;     // azimuth (0 = N, 90 = E, 315 = NW)

    // Maximum area the user can draw. Past this we show a warning and
    // refuse — 3D detail is the point, so keep the rectangle small.
    // 25 km² ≈ 5 km × 5 km, generous for "inspector" use.
    const MAX_AREA_KM2 = 25;

    // ─── Module state ──────────────────────────────────────────────
    let drawMode = false;                // true while waiting for a drag
    let drawState = null;                 // active drag: { x0, y0, x1, y1 }
    let drawOverlaySvg = null;            // SVG live preview
    let drawHintEl = null;                // top-of-screen hint pill

    // Open-modal session state. Null when the modal is closed.
    //
    // {
    //   bbox, sourceId, analysisKey, param, paramExtra,
    //   raster:      { w, h, cellsize_m, data: Float32Array },
    //   detail, exaggeration, lightDeg, fetchSize,
    //   widthM, heightM,        // bbox dimensions in true ground metres
    //   renderer, scene, camera, controls,
    //   mesh, geometry, material, texture,
    //   light, ambient,
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
    }

    function updateDrawOverlay() {
        if (!drawOverlaySvg) return;
        const rect = drawOverlaySvg.querySelector("rect");
        if (!drawState) {
            rect.setAttribute("width", "0");
            rect.setAttribute("height", "0");
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

        // Three.js textures expect Y up by default (the bottom row of
        // the image is at v=0). Our raster is row-major top-to-bottom
        // (north at row 0), so we flip vertically here once at bake time
        // rather than depending on texture.flipY (which doesn't reliably
        // re-flip after the canvas is drawn).
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        const img = new ImageData(rgba, w, h);
        ctx.putImageData(img, 0, 0);
        // Flip vertically by drawing the canvas onto itself with
        // transform. Cheaper than another temp canvas allocation.
        // We instead flip via the texture's UVs in setupMesh().
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

        // Camera positioned at a 45° elevation looking down at the
        // surface from the southwest. Distance scaled to the largest
        // bbox dimension so the whole rectangle is in view at boot.
        const span = Math.max(s.widthM, s.heightM);
        const distance = span * 1.4;
        const fov = 45;
        s.camera = new THREE.PerspectiveCamera(fov, W / H,
                                                Math.max(0.1, span / 1000),
                                                span * 20);
        s.camera.position.set(span * 0.6, distance * 0.7, span * 0.8);
        s.camera.lookAt(0, 0, 0);

        s.ambient = new THREE.AmbientLight(0xb0c4d4, 0.45);
        s.scene.add(s.ambient);

        s.light = new THREE.DirectionalLight(0xfff2d6, 1.05);
        positionLight(s.light, s.lightDeg, span);
        s.scene.add(s.light);

        s.controls = new THREE.OrbitControls(s.camera, s.renderer.domElement);
        s.controls.enableDamping = true;
        s.controls.dampingFactor = 0.08;
        s.controls.minDistance = span * 0.05;
        s.controls.maxDistance = span * 6;
        s.controls.maxPolarAngle = Math.PI * 0.49;  // never below horizon
        s.controls.target.set(0, 0, 0);
        s.controls.update();

        // Resize handler — bound here so close() can remove it cleanly.
        s.onResize = () => {
            if (!session) return;
            const ww = wrap.clientWidth;
            const hh = wrap.clientHeight;
            if (ww === 0 || hh === 0) return;
            s.renderer.setSize(ww, hh, false);
            s.camera.aspect = ww / hh;
            s.camera.updateProjectionMatrix();
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
            lightDeg:     DEFAULT_LIGHT_DEG,
            fetchSize:    DEFAULT_FETCH_SIZE,
            widthM, heightM,
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

        // Build scene + mesh. Hide the loading text once the renderer
        // is on screen.
        try {
            setupScene(s);
            setupMesh(s);
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
        if (s.controls) s.controls.dispose();
        if (s.mesh && s.scene) s.scene.remove(s.mesh);
        if (s.geometry) s.geometry.dispose();
        if (s.material) s.material.dispose();
        if (s.texture) s.texture.dispose();
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
        const $exag   = $("inspect3d-exag");
        const $exagV  = $("inspect3d-exag-val");
        const $det    = $("inspect3d-detail");
        const $detV   = $("inspect3d-detail-val");
        const $light  = $("inspect3d-light");
        const $lightV = $("inspect3d-light-val");
        if ($exag)   $exag.value  = String(s.exaggeration);
        if ($exagV)  $exagV.textContent = `${s.exaggeration}×`;
        if ($det)    $det.value   = String(s.detail);
        if ($detV)   $detV.textContent = `${s.detail}²`;
        if ($light)  $light.value = String(s.lightDeg);
        if ($lightV) $lightV.textContent = `${s.lightDeg}°`;
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
        const $det     = $("inspect3d-detail");
        const $detV    = $("inspect3d-detail-val");
        const $light   = $("inspect3d-light");
        const $lightV  = $("inspect3d-light-val");
        const $reset   = $("inspect3d-reset");

        if ($close)    $close.addEventListener("click", close);
        if ($backdrop) $backdrop.addEventListener("click", close);

        if ($exag) $exag.addEventListener("input", (e) => {
            const v = parseInt(e.target.value, 10);
            if ($exagV) $exagV.textContent = `${v}×`;
            const s = session;
            if (!s || !s.geometry || !s.raster) return;
            s.exaggeration = v;
            setHeightsOnGeometry(s.geometry, s.raster, s.detail,
                                  s.exaggeration, s.widthM, s.heightM);
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
            const span = Math.max(s.widthM, s.heightM);
            const distance = span * 1.4;
            s.camera.position.set(span * 0.6, distance * 0.7, span * 0.8);
            s.controls.target.set(0, 0, 0);
            s.controls.update();
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
