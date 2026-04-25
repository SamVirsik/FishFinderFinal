map_layers.forEach((layer) => {
    const resolutionSlider = document.getElementById(`resolution-${layer.id}`);
    const resolutionValue = document.getElementById(`res-value-${layer.id}`);

    resolutionSlider.addEventListener("input", () => {
        resolutionValue.textContent = resolutionSlider.value;
    });
});

baseurl = window.location.origin;

require([
    "esri/Map",
    "esri/views/MapView",
    "esri/layers/WebTileLayer",
    "esri/geometry/SpatialReference",
    "esri/geometry/projection",
    "esri/geometry/Point"         // <-- NEW
], function (
  Map, MapView, WebTileLayer, SpatialReference, projection,
  Point                          // <-- NEW
) {
    // Ensure the projection module is loaded
    projection.load().then(() => {
        console.log("Projection module loaded.");
    }).catch((err) => {
        console.error("Error loading projection module:", err);
    });

    // Helper function to create the WebTileLayer
    function createDepthTileLayer() {
        return new WebTileLayer({
            urlTemplate: baseurl + "/tile/{level}_{row}_{col}",
            opacity: document.getElementById(`opacity-0`).value/100.0, // You can set default opacity here
            copyright: "© FishFinder"
        });
      
    }

    // 1. Create the initial layer
    let depthTileLayer = createDepthTileLayer();

    // 2. Create the map
    const map = new Map({
      basemap: "streets-navigation-vector",
      layers: [depthTileLayer]
    });

    // 3. Create the MapView
    const view = new MapView({
    container: "map",
    map: map,
    center: [-98.5795, 39.8283], // Continental US
    zoom: 4,
    });

    // ────────────────────────────────────────────────────────────────
    // keep map UI below the blue header (≈ 60 px high)
    // ────────────────────────────────────────────────────────────────
    const HEADER = 60;                       // px – adjust if you change the header height
    view.padding = { top: HEADER, left: 0, right: 0, bottom: 0 };

    // if the popup (or anything else) rewrites padding, reinstate the baseline
    view.watch("padding", (p) => {
    if (p.top < HEADER) {
        view.padding = { ...p, top: HEADER };
    }
    });

    //------------------------------------------------------------------
    //  CLICK-FOR-DEPTH
    //------------------------------------------------------------------
    view.on("click", async (event) => {
        // 1.  Geographic coordinates
        const lat = event.mapPoint.latitude;
        const lon = event.mapPoint.longitude;

        // 2.  Open popup immediately so user sees feedback
        view.popup.open({
            location: event.mapPoint,
            title: "Fetching depth…",
            content: `Lat: ${lat.toFixed(5)}°, Lon: ${lon.toFixed(5)}°`,
        });

        try {
            // 3.  Ask Flask for depth
            const resp = await fetch(`${baseurl}/depth/${lat}/${lon}`);
            const data = await resp.json();

            // 4.  Replace popup content
            // 4.  Replace popup content (feet + copy-able coords)
            const depthM = data.depth_meters;
            const depthFt =
                depthM === null || depthM === undefined
                    ? null
                    : depthM * 3.28084;          // meters ➜ feet
    
            const depthTxt = depthFt === null ? "No data"
                              : `${depthFt.toFixed(1)} ft`;
    
            // One line for copy/paste: lat, lon
            const coordLine = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    
            view.popup.content = `
                <b>Coordinates:</b> ${coordLine}<br>
                <b>Depth:</b> ${depthTxt}
            `;
            view.popup.title = "Location info";
        } catch (e) {
            console.error(e);
            view.popup.content += "<br><span style='color:red'>Error loading depth</span>";
        }
    });

    const geocodeUrl = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer";

    async function moveMapTo(placeName) {
        const geocodeUrl = `https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?SingleLine=${encodeURIComponent(placeName)}&f=json`;

        try {
            const response = await fetch(geocodeUrl);
            const data = await response.json();

            if (data.candidates && data.candidates.length > 0) {
                const candidate = data.candidates[0];
                const location = candidate.location;

                if (candidate.extent) {
                    const extent = {
                        xmin: parseFloat(candidate.extent.xmin),
                        ymin: parseFloat(candidate.extent.ymin),
                        xmax: parseFloat(candidate.extent.xmax),
                        ymax: parseFloat(candidate.extent.ymax),
                        spatialReference: { wkid: 4326 }
                    };

                    console.log("Using dynamic extent:", extent);

                    // Calculate zoom level based on extent
                    const zoom = calculateZoomFromExtent(extent, view.width, view.height);
                    console.log("Calculated Zoom:", zoom);

                    // Move the map to the center with calculated zoom
                    const center = [
                        (extent.xmin + extent.xmax) / 2,
                        (extent.ymin + extent.ymax) / 2
                    ];
                    await view.goTo({ center, zoom }, { animate: true });
                } else {
                    console.log("Extent unavailable. Using center fallback.");
                    const center = [location.x, location.y];
                    await view.goTo({ center, zoom: 12 }, { animate: true });
                }
            } else {
                console.warn("No valid location found.");
            }
        } catch (error) {
            console.error("Error fetching coordinates:", error);
        }
    }
    // Search functionality placeholder
    const searchInput = document.getElementById("search-input");
    const searchButton = document.getElementById("search-button");

    searchButton.addEventListener("click", () => {
        const searchText = searchInput.value;
        moveMapTo(searchText);
    });
    document.getElementById("search-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") { // Detect Enter key
            const placeName = event.target.value;
            if (placeName.trim() !== "") {
                moveMapTo(placeName); // Trigger the search
            }
        }
    });

    //---------------------------------------------------------------
    // GO TO SPOT - modal logic
    //---------------------------------------------------------------
    const gotoBtn     = document.getElementById("goto-spot-btn");
    const gotoDialog  = document.getElementById("goto-dialog");
    const gotoLatIn   = document.getElementById("goto-lat");
    const gotoLonIn   = document.getElementById("goto-lon");
    const gotoSubmit  = document.getElementById("goto-submit");
    const gotoCancel  = document.getElementById("goto-cancel");

    // open the modal
    gotoBtn.addEventListener("click", () => gotoDialog.showModal());
    // close without doing anything
    gotoCancel.addEventListener("click", () => gotoDialog.close());

    // handle “Go”
    gotoSubmit.addEventListener("click", async () => {
    const lat = parseFloat(gotoLatIn.value);
    const lon = parseFloat(gotoLonIn.value);
    if (Number.isNaN(lat) || Number.isNaN(lon)) {
        alert("Please enter valid numbers for both latitude and longitude.");
        return;
    }
    gotoDialog.close();

    // 1.  Move the map
    await view.goTo({ center: [lon, lat], zoom: Math.max(view.zoom, 10) });

    // 2.  Show the same popup you use on right-click
    const point = new Point({ longitude: lon, latitude: lat });
    view.popup.open({
        location: point,
        title: "Fetching depth…",
        content: `Lat: ${lat.toFixed(5)}°, Lon: ${lon.toFixed(5)}°`
    });

    try {
        const resp  = await fetch(`${baseurl}/depth/${lat}/${lon}`);
        const data  = await resp.json();
        const dM    = data.depth_meters;
        const dFt   = dM == null ? null : dM * 3.28084;
        const depth = dFt == null ? "No data" : `${dFt.toFixed(1)} ft`;
        const line  = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

        view.popup.title   = "Location info";
        view.popup.content = `<b>Coordinates:</b> ${line}<br><b>Depth:</b> ${depth}`;
    } catch (err) {
        console.error(err);
        view.popup.content += "<br><span style='color:red'>Error loading depth</span>";
    }
    });

    // Optional tile-load handlers
    depthTileLayer.on("tile-load-error", (event) => {
      console.error("Tile load error:", event.error);
    });
    depthTileLayer.on("tile-load", (event) => {
      console.log("Tile loaded:", event.tileInfo);
    });

    // Function to convert latitude/longitude to tile coordinates
    function latLonToTile(lat, lon, zoom) {
        const n = Math.pow(2, zoom);
        const x = Math.floor((lon + 180) / 360 * n);
        const y = Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * n);
        return { x, y };
    }

    // Function to convert tile coordinates back to latitude/longitude
    function tileToLatLon(x, y, zoom) {
        const n = Math.pow(2, zoom);
        const lon = x / n * 360.0 - 180.0;
        const lat_rad = Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n)));
        const lat = lat_rad * 180.0 / Math.PI;
        return { lat, lon };
    }
    
    function reloadLayer() {
        projection.load()
        const spatialReference = new SpatialReference({ wkid: 4326 });
        // NEW — which bathymetry source to hit
        const datasource = document.getElementById("data-source-dropdown-0").value;
        const zoomLevel = view.zoom; // Get the current zoom level
        const extent = projection.project(view.extent, spatialReference); // Get the current map extent
        
        console.log(`Current extent: ${JSON.stringify(extent)}`);
        console.log(`Zoom level: ${zoomLevel}`);

        // Convert the extent corners to tile coordinates
        const topLeft = latLonToTile(extent.ymax, extent.xmin, zoomLevel);
        const bottomRight = latLonToTile(extent.ymin, extent.xmax, zoomLevel);

        console.log(`Top-left tile: ${JSON.stringify(topLeft)}`);
        console.log(`Bottom-right tile: ${JSON.stringify(bottomRight)}`);

        // Determine the smallest tile that covers the entire view extent
        const minTileX = Math.min(topLeft.x, bottomRight.x);
        const maxTileX = Math.max(topLeft.x, bottomRight.x);
        const minTileY = Math.min(topLeft.y, bottomRight.y);
        const maxTileY = Math.max(topLeft.y, bottomRight.y);

        console.log(`Tile range: X(${minTileX} to ${maxTileX}), Y(${minTileY} to ${maxTileY})`);

        // Get the layer number, row, and column of the smallest tile
        const layerNumber = zoomLevel;
        const row = minTileY;
        const column = minTileX;

        console.log(`Layer: ${layerNumber}, Row: ${row}, Column: ${column}`);

        // Convert tile coordinates back to latitude/longitude for extent
        const topLeftCoords = tileToLatLon(minTileX, minTileY, layerNumber);
        const bottomRightCoords = tileToLatLon(maxTileX + 1, maxTileY + 1, layerNumber);

        const extentData = {
            lonmin: topLeftCoords.lon,
            lonmax: bottomRightCoords.lon,
            latmin: bottomRightCoords.lat,
            latmax: topLeftCoords.lat
        };
        const extent_string = JSON.stringify(extentData);

        fetch("/reload-layer/"
          + extent_string + "_"
          + document.getElementById("resolution-0").value + "_"
          + document.getElementById("analysis-dropdown-0").value + "_"
          + document.getElementById("smoothness-0").value + "_"
          + document.getElementById("layer-width-dropdown-0").value + "_"
          + datasource);
        // Swap in a fresh tile layer so the browser drops cached tiles.
        map.removeAll();
        depthTileLayer = createDepthTileLayer();
        map.add(depthTileLayer);
    }
    map_layers.forEach((layer) => {
        document.getElementById(`reload-layer-button-${layer['id']}`).addEventListener("click", () => {
            reloadLayer();
        });
        document.getElementById(`opacity-${layer['id']}`).addEventListener("input", (event) => {
            depthTileLayer.opacity = event.target.value / 100.0;
        });
    });
  });

// Function to calculate zoom level based on extent
function calculateZoomFromExtent(extent, mapWidth, mapHeight) {
    // World scale for Web Mercator (at zoom level 0)
    const WORLD_WIDTH = 360; // Degrees in WGS84
    const TILE_SIZE = 256;   // Pixels per tile

    // Width and height of the extent in degrees
    const extentWidth = extent.xmax - extent.xmin;
    const extentHeight = extent.ymax - extent.ymin;

    // Scale to fit extent width and height into map's dimensions
    const scaleX = mapWidth / (extentWidth * TILE_SIZE / WORLD_WIDTH);
    const scaleY = mapHeight / (extentHeight * TILE_SIZE / WORLD_WIDTH);

    // Calculate zoom level
    const scale = Math.min(scaleX, scaleY);
    const zoom = Math.log2(scale);
    return Math.floor(zoom)-0.5; // Use floor for discrete zoom levels
}

document.getElementById("toggle-sidebar").addEventListener("click", function () {
    const sidebar = document.getElementById("sidebar");
    const toggleButton = document.getElementById("toggle-sidebar");
    sidebar.classList.toggle("hidden");
    toggleButton.classList.toggle("hidden");
});