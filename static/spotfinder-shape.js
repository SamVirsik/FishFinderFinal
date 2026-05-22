// Spotfinder geometry helpers.
//
// A SearchArea is a (possibly rotated) rectangle on the map. It carries
// both the rich shape (4 corners + center + width/height in metres +
// rotation) and the axis-aligned bbox that contains the rotated rect,
// so downstream code that only cares about a coarse extent (storage
// indexes, fallback queries, fit-to-area) doesn't have to recompute it.
//
// All rotation math is done in Web Mercator metres around the
// rectangle's projected centre — the spec explicitly authorises this
// approximation at the Keys' latitudes. The corner lat/lngs are then
// computed via the Mercator inverse, so the corners are the truth on
// the map regardless of which projection a caller later re-derives.
//
// rotation_deg uses navigational convention: 0 = "top edge points
// north" (axis-aligned), measured clockwise (so 90° means top edge
// points east). The internal Mercator math treats clockwise rotation
// in screen coordinates (where y grows downward), which is the same
// sign convention.

(() => {
    const WEB_MERCATOR_HALF = 20037508.342789244;
    const DEG2RAD = Math.PI / 180;
    const RAD2DEG = 180 / Math.PI;

    function lngToMercatorX(lng) {
        return lng * WEB_MERCATOR_HALF / 180;
    }
    function latToMercatorY(lat) {
        // Clamp so log(tan(...)) doesn't blow up at the poles. The Keys
        // are nowhere near them but the helper is general-purpose.
        const clamped = Math.max(-85.0511, Math.min(85.0511, lat));
        return Math.log(Math.tan((90 + clamped) * Math.PI / 360))
             / (Math.PI / 180) * WEB_MERCATOR_HALF / 180;
    }
    function mercatorXToLng(mx) {
        return mx * 180 / WEB_MERCATOR_HALF;
    }
    function mercatorYToLat(my) {
        return Math.atan(Math.exp(my * Math.PI / WEB_MERCATOR_HALF))
             * 360 / Math.PI - 90;
    }

    /** Forward-project lat/lng to Web Mercator metres. */
    function project(lat, lng) {
        return { x: lngToMercatorX(lng), y: latToMercatorY(lat) };
    }
    /** Inverse-project Web Mercator metres to lat/lng. */
    function unproject(x, y) {
        return { lat: mercatorYToLat(y), lng: mercatorXToLng(x) };
    }

    /**
     * Mercator metres at a given latitude are stretched by 1/cos(lat)
     * relative to true ground metres. To convert a Mercator-space
     * dimension to its true ground length, multiply by cos(lat).
     */
    function mercatorScaleAt(lat) {
        return Math.cos(lat * DEG2RAD);
    }

    /**
     * Rotate a point (dx, dy) around the origin by `deg` degrees
     * clockwise. dx points east, dy points north (i.e. Mercator
     * orientation, NOT screen orientation). With clockwise rotation
     * from north:
     *   - At 0°: (dx, dy) is unchanged.
     *   - At 90°: a point at "top" (dy > 0) ends up "east" (dx > 0).
     */
    function rotateClockwise(dx, dy, deg) {
        const t = deg * DEG2RAD;
        const c = Math.cos(t);
        const s = Math.sin(t);
        // Clockwise rotation in (east, north) coords:
        //   new_dx =  dx*cos + dy*sin
        //   new_dy = -dx*sin + dy*cos
        return { dx: dx * c + dy * s, dy: -dx * s + dy * c };
    }

    /** Smallest signed difference between two angles (degrees, ±180). */
    function angleDiff(a, b) {
        let d = (a - b + 540) % 360 - 180;
        if (d <= -180) d += 360;
        return d;
    }

    /**
     * Build a SearchArea from a center, ground dimensions (metres),
     * and rotation (deg, clockwise from north). The 4 corners are in
     * order TL → TR → BR → BL relative to the un-rotated rectangle
     * (TL/TR being the "top" edge, opposite the rotation handle).
     */
    function buildSearchArea(centerLat, centerLng, widthM, heightM, rotationDeg) {
        const center = { lat: centerLat, lng: centerLng };
        const c = project(centerLat, centerLng);
        const scale = mercatorScaleAt(centerLat);
        // Convert true ground metres to Mercator-space metres at this
        // latitude. Mercator stretches by 1/cos(lat), so we divide by
        // the scale factor when going IN.
        const hwMerc = (widthM  / 2) / Math.max(1e-9, scale);
        const hhMerc = (heightM / 2) / Math.max(1e-9, scale);

        // Local-frame corners (east, north) of the un-rotated rect.
        // TL = -hw, +hh ; TR = +hw, +hh ; BR = +hw, -hh ; BL = -hw, -hh
        const local = [
            { dx: -hwMerc, dy: +hhMerc },
            { dx: +hwMerc, dy: +hhMerc },
            { dx: +hwMerc, dy: -hhMerc },
            { dx: -hwMerc, dy: -hhMerc },
        ];

        const corners = local.map((p) => {
            const r = rotateClockwise(p.dx, p.dy, rotationDeg);
            return unproject(c.x + r.dx, c.y + r.dy);
        });

        return {
            corners,
            center,
            width_m:      widthM,
            height_m:     heightM,
            rotation_deg: rotationDeg,
            bbox:         bboxFromCorners(corners),
        };
    }

    /** AABB containing the 4 corners. */
    function bboxFromCorners(corners) {
        let n = -Infinity, s = +Infinity, e = -Infinity, w = +Infinity;
        for (const p of corners) {
            if (p.lat > n) n = p.lat;
            if (p.lat < s) s = p.lat;
            if (p.lng > e) e = p.lng;
            if (p.lng < w) w = p.lng;
        }
        return { north: n, south: s, east: e, west: w };
    }

    /**
     * Synthesize a SearchArea from an old axis-aligned bbox
     * (rotation = 0). Width/height are computed from the bbox using
     * the centre latitude's Mercator scale, so the resulting metric
     * dimensions match what the new code would have generated.
     */
    function searchAreaFromBbox(bbox) {
        const centerLat = (bbox.north + bbox.south) / 2;
        const centerLng = (bbox.east  + bbox.west)  / 2;
        const scale = mercatorScaleAt(centerLat);
        // Mercator widths/heights of the bbox.
        const merW = lngToMercatorX(bbox.east) - lngToMercatorX(bbox.west);
        const merH = latToMercatorY(bbox.north) - latToMercatorY(bbox.south);
        // True ground metres = Mercator metres × cos(lat) at the centre.
        const widthM  = Math.max(0, merW * scale);
        const heightM = Math.max(0, merH * scale);
        return buildSearchArea(centerLat, centerLng, widthM, heightM, 0);
    }

    /**
     * Normalise whatever the caller might have stored or passed:
     *   - a SearchArea already → returned as-is (bbox patched if missing)
     *   - a BoundingBox (old shape) → synthesised with rotation = 0
     *   - null/undefined → null
     */
    function coerceSearchArea(input) {
        if (!input || typeof input !== "object") return null;
        if (input.corners && input.center && Number.isFinite(input.rotation_deg)) {
            // Looks like a SearchArea. Patch a missing bbox so callers
            // can rely on it being present.
            if (!input.bbox) input.bbox = bboxFromCorners(input.corners);
            return input;
        }
        if (Number.isFinite(input.north) && Number.isFinite(input.south)
            && Number.isFinite(input.east) && Number.isFinite(input.west)) {
            return searchAreaFromBbox(input);
        }
        return null;
    }

    /**
     * Compute area in km² for a rectangle. Trivially width × height
     * since both are in ground metres.
     */
    function areaKm2(area) {
        return (area.width_m * area.height_m) / 1_000_000;
    }

    /** Encode a SearchArea as URL query params (compact form). */
    function encodeForUrl(area) {
        return {
            // Centre + dimensions + rotation are the minimal set that
            // round-trips back to the same SearchArea via buildSearchArea.
            clat: area.center.lat.toFixed(7),
            clng: area.center.lng.toFixed(7),
            w_m:  area.width_m.toFixed(2),
            h_m:  area.height_m.toFixed(2),
            r:    area.rotation_deg.toFixed(3),
        };
    }

    /**
     * Parse a URLSearchParams into a SearchArea. Supports both the new
     * (clat/clng/w_m/h_m/r) and old (n/s/e/w) shapes. Returns null if
     * neither shape is valid.
     */
    function decodeFromUrl(params) {
        const clat = parseFloat(params.get("clat"));
        const clng = parseFloat(params.get("clng"));
        const w_m  = parseFloat(params.get("w_m"));
        const h_m  = parseFloat(params.get("h_m"));
        const r    = parseFloat(params.get("r"));
        if (Number.isFinite(clat) && Number.isFinite(clng)
            && Number.isFinite(w_m) && Number.isFinite(h_m)
            && Number.isFinite(r)
            && clat >= -90 && clat <= 90
            && clng >= -180 && clng <= 180
            && w_m > 0 && h_m > 0) {
            return buildSearchArea(clat, clng, w_m, h_m, r);
        }
        // Legacy bbox form (URLs bookmarked before the rotation rework).
        const n = parseFloat(params.get("n"));
        const s = parseFloat(params.get("s"));
        const e = parseFloat(params.get("e"));
        const w = parseFloat(params.get("w"));
        if (Number.isFinite(n) && Number.isFinite(s)
            && Number.isFinite(e) && Number.isFinite(w)
            && n > s && n <= 90 && s >= -90
            && e <= 180 && w >= -180) {
            return searchAreaFromBbox({ north: n, south: s, east: e, west: w });
        }
        return null;
    }


    window.FishFinderSpotfinderShape = {
        WEB_MERCATOR_HALF,
        project, unproject,
        mercatorScaleAt,
        rotateClockwise,
        angleDiff,
        buildSearchArea,
        bboxFromCorners,
        searchAreaFromBbox,
        coerceSearchArea,
        areaKm2,
        encodeForUrl,
        decodeFromUrl,
    };
})();
