/* Shared code of the city maps and the index map (MapLibre GL JS). */

const BASEMAPS = {
  positron: { label: "Positron", url: "https://tiles.openfreemap.org/styles/positron", dark: false },
  dark: { label: "Dark", url: "https://tiles.openfreemap.org/styles/dark", dark: true },
  liberty: { label: "Regular", url: "https://tiles.openfreemap.org/styles/liberty", dark: false },
};
const DEFAULT_BASEMAP = "positron";

// Metric configurations and color ramps
const METRIC_CONFIGS = {
  veg_median: { label: "Median vegetation", unit: "%", type: "percent" },
  veg_mean: { label: "Mean vegetation", unit: "%", type: "percent" },
  veg_min: { label: "Minimum vegetation", unit: "%", type: "percent" },
  veg_max: { label: "Maximum vegetation", unit: "%", type: "percent" },
  veg_std: { label: "Standard deviation", unit: "%", type: "std" },
  veg_iqr: { label: "Interquartile range (IQR)", unit: "%", type: "std" },
  h_median: { label: "Median altitude", unit: " m", type: "height" },
  count: { label: "Mapillary photo count", unit: "", type: "count" },
};

const METRIC_PALETTES = {
  percent: {
    stops: [
      [0, "#ffffe5"],
      [5, "#f7fcb9"],
      [10, "#d9f0a3"],
      [15, "#addd8e"],
      [20, "#78c679"],
      [27, "#41ab5d"],
      [35, "#238443"],
      [42, "#006837"],
      [50, "#004529"],
    ],
    gradient: "linear-gradient(to right, #ffffe5, #f7fcb9, #d9f0a3, #addd8e, #78c679, #41ab5d, #238443, #006837, #004529)",
    ticks: [
      { pos: "0%", text: "0%" },
      { pos: "30%", text: "15%" },
      { pos: "60%", text: "30%" },
      { pos: "100%", text: "≥ 50%" },
    ],
  },
  std: {
    stops: [
      [0, "#ffffd9"],
      [3, "#edf8b1"],
      [6, "#c7e9b4"],
      [9, "#7fcdbb"],
      [13, "#41b6c4"],
      [17, "#1d91c0"],
      [21, "#225ea8"],
      [25, "#0c2c84"],
    ],
    gradient: "linear-gradient(to right, #ffffd9, #edf8b1, #c7e9b4, #7fcdbb, #41b6c4, #1d91c0, #225ea8, #0c2c84)",
    ticks: [
      { pos: "0%", text: "0%" },
      { pos: "36%", text: "9%" },
      { pos: "68%", text: "17%" },
      { pos: "100%", text: "≥ 25%" },
    ],
  },
  count: {
    stops: [
      [1, "#ffffb2"],
      [5, "#fed976"],
      [15, "#feb24c"],
      [30, "#fd8d3c"],
      [50, "#f03b20"],
      [75, "#bd0026"],
      [100, "#800026"],
    ],
    gradient: "linear-gradient(to right, #ffffb2, #fed976, #feb24c, #fd8d3c, #f03b20, #bd0026, #800026)",
    ticks: [
      { pos: "0%", text: "1" },
      { pos: "30%", text: "30" },
      { pos: "60%", text: "60" },
      { pos: "100%", text: "≥ 100" },
    ],
  },
  height: {
    stops: [
      [200, "#f7fcf5"],
      [250, "#e5f5e0"],
      [300, "#c7e9c0"],
      [350, "#a1d99b"],
      [400, "#74c476"],
      [500, "#41ab5d"],
      [650, "#238b45"],
      [800, "#006d2c"],
      [1000, "#00441b"],
    ],
    gradient: "linear-gradient(to right, #f7fcf5, #c7e9c0, #74c476, #31a354, #006d2c, #00441b)",
    ticks: [
      { pos: "0%", text: "200m" },
      { pos: "35%", text: "400m" },
      { pos: "70%", text: "700m" },
      { pos: "100%", text: "≥ 1000m" },
    ],
  },
};

const UNSURVEYED_COLOR = {
  light: "#a8a7a0",
  dark: "#545450",
};

const SURFACES = { light: "#fcfcfb", dark: "#1a1a19" };

const ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> ' +
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap contributors</a> · ' +
  'images and segmentation: <a href="https://www.mapillary.com" target="_blank">Mapillary</a>';

function storedBasemap() {
  try {
    const value = localStorage.getItem("basemap");
    return value in BASEMAPS ? value : DEFAULT_BASEMAP;
  } catch (e) {
    return DEFAULT_BASEMAP;
  }
}

function storeBasemap(name) {
  try {
    localStorage.setItem("basemap", name);
  } catch (e) {
    /* private mode: not remembered */
  }
}

function theme(name) {
  return BASEMAPS[name].dark ? "dark" : "light";
}

function applyTheme(name) {
  document.documentElement.dataset.theme = theme(name);
}

function el(tag, attributes = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

/* The basemap switcher; calls onChange(name) when another basemap is chosen. */
function buildSwitcher(current, onChange) {
  const container = document.getElementById("basemaps");
  container.replaceChildren();
  for (const [name, basemap] of Object.entries(BASEMAPS)) {
    const button = el("button", { type: "button", "aria-pressed": String(name === current) }, basemap.label);
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-pressed") === "true") return;
      for (const other of container.querySelectorAll("button")) other.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
      onChange(name);
    });
    container.appendChild(button);
  }
}

function createMap(basemap, options) {
  const map = new maplibregl.Map({
    container: "map",
    style: BASEMAPS[basemap].url,
    attributionControl: { compact: true, customAttribution: ATTRIBUTION },
    ...options,
  });
  if (window.matchMedia("(max-width: 600px)").matches) {
    map.once("load", () => {
      const attribution = map.getContainer().querySelector(".maplibregl-ctrl-attrib");
      if (attribution) attribution.classList.remove("maplibregl-compact-show");
    });
  }
  return map;
}

/* Collapsible info panel at phone width (collapsed at first, to show the map). */
function setupInfoToggle() {
  const info = document.getElementById("info");
  const toggle = info.querySelector(".toggle-info");
  if (!toggle) return;
  const setCollapsed = (collapsed) => {
    info.classList.toggle("collapsed", collapsed);
    toggle.textContent = collapsed ? "Details" : "Hide";
    toggle.setAttribute("aria-expanded", String(!collapsed));
  };
  setCollapsed(window.matchMedia("(max-width: 600px)").matches);
  toggle.addEventListener("click", () => setCollapsed(!info.classList.contains("collapsed")));
}

// ---------------------------------------------------------------------------
// City map
// ---------------------------------------------------------------------------

function metricColorExpression(metricName, themeName, hasCountCheck = true) {
  const config = METRIC_CONFIGS[metricName] || METRIC_CONFIGS.veg_median;
  const palette = METRIC_PALETTES[config.type] || METRIC_PALETTES.percent;
  const nullColor = UNSURVEYED_COLOR[themeName];

  const interpExpr = [
    "interpolate",
    ["linear"],
    ["to-number", ["get", metricName], 0],
  ];
  palette.stops.forEach(([limit, color]) => interpExpr.push(limit, color));

  if (!hasCountCheck) {
    return [
      "case",
      ["==", ["get", metricName], null],
      nullColor,
      interpExpr,
    ];
  }

  return [
    "case",
    ["any", ["==", ["coalesce", ["get", "count"], 0], 0], ["==", ["get", metricName], null]],
    nullColor,
    interpExpr,
  ];
}

function renderLegend(themeName, metricName = "veg_median") {
  const legend = document.getElementById("legend");
  if (!legend) return;
  const config = METRIC_CONFIGS[metricName] || METRIC_CONFIGS.veg_median;
  const palette = METRIC_PALETTES[config.type] || METRIC_PALETTES.percent;

  legend.replaceChildren(el("h2", {}, config.label));

  // Continuous gradient bar with ticks (no discrete breaks)
  const rampBox = el("div", { class: "legend-continuous" });
  const bar = el("div", { class: "legend-bar" });
  bar.style.background = palette.gradient;
  rampBox.appendChild(bar);

  const ticksRow = el("div", { class: "legend-ticks" });
  palette.ticks.forEach((tick) => {
    const tickEl = el("span", { class: "legend-tick" }, tick.text);
    tickEl.style.left = tick.pos;
    ticksRow.appendChild(tickEl);
  });
  rampBox.appendChild(ticksRow);
  legend.appendChild(rampBox);

  // Unsurveyed street indicator
  const unRow = el("div", { class: "legend-row legend-unvisited-row" });
  const unSwatch = el("span", { class: "legend-swatch unvisited" });
  unSwatch.style.background = UNSURVEYED_COLOR[themeName];
  unRow.append(unSwatch, el("span", { class: "muted" }, "No Mapillary photos"));
  legend.appendChild(unRow);
}

function renderCityStats(config) {
  const stats = config.stats;
  document.getElementById("city-name").textContent = config.name;
  document.getElementById("city-full-name").textContent = config.display_name || "";
  const list = document.getElementById("stats");
  const rows = [
    ["Images", formatNumber(stats.images)],
    ["Median vegetation", `${formatNumber(stats.vegetation_median, 1)}%`],
    ["Mean vegetation", `${formatNumber(stats.vegetation_mean, 1)}%`],
    ["Captured", stats.date_min === stats.date_max ? stats.date_min : `${stats.date_min} – ${stats.date_max}`],
    ["Tiles done", `${formatNumber(stats.tiles_completed)} / ${formatNumber(stats.tiles_total)}`],
    ["Updated", stats.updated || "–"],
  ];
  list.replaceChildren();
  for (const [term, value] of rows) list.append(el("dt", {}, term), el("dd", {}, value));
}

function segmentPopupContent(p, currentMetric) {
  const box = el("div", { class: "segment-popup" });
  const title = p.name && p.name !== "None" ? p.name : "Unnamed street";
  box.append(el("div", { class: "popup-title" }, title));

  const meta = [];
  if (p.highway && p.highway !== "None") meta.push(p.highway);
  if (p.length_m) meta.push(`${formatNumber(p.length_m)} m`);
  if (meta.length) box.append(el("div", { class: "muted popup-meta" }, meta.join(" · ")));

  const count = Number(p.count) || 0;
  if (count === 0) {
    box.append(el("div", { class: "popup-nodata" }, "No Mapillary photos on this segment yet"));
    return box;
  }

  const activeConf = METRIC_CONFIGS[currentMetric] || METRIC_CONFIGS.veg_median;
  const activeVal = p[currentMetric];
  box.append(
    el("div", { class: "popup-value" }, `${formatNumber(activeVal, 1)}${activeConf.unit} ${activeConf.label.toLowerCase()}`)
  );
  box.append(
    el("div", { class: "muted popup-sub" }, `${formatNumber(count)} photos · ${formatNumber(p.image_density, 1)} photos / 100m`)
  );

  const table = el("table", { class: "popup-stats-table" });
  const hRow =
    p.h_median !== null && p.h_median !== undefined && !Number.isNaN(Number(p.h_median))
      ? `<tr><td>Altitude:</td><td colspan="3">${formatNumber(p.h_median, 1)} m</td></tr>`
      : "";
  table.innerHTML = `
    <tbody>
      <tr><td>Median:</td><td><strong>${formatNumber(p.veg_median, 1)}%</strong></td><td>Mean:</td><td>${formatNumber(p.veg_mean, 1)}%</td></tr>
      <tr><td>Min:</td><td>${formatNumber(p.veg_min, 1)}%</td><td>Max:</td><td>${formatNumber(p.veg_max, 1)}%</td></tr>
      <tr><td>Std dev:</td><td>${formatNumber(p.veg_std, 1)}%</td><td>IQR:</td><td>${formatNumber(p.veg_iqr, 1)}%</td></tr>
      <tr><td>Q1:</td><td>${formatNumber(p.veg_q1, 1)}%</td><td>Q3:</td><td>${formatNumber(p.veg_q3, 1)}%</td></tr>
      <tr><td>Skewness:</td><td>${formatNumber(p.veg_skew, 2)}</td><td>Kurtosis:</td><td>${formatNumber(p.veg_kurt, 2)}</td></tr>
      ${hRow}
    </tbody>
  `;
  box.append(table);
  return box;
}

function pointPopupContent(properties, withLink) {
  const box = el("div");
  box.append(el("div", { class: "popup-value" }, `${formatNumber(properties.veg, 1)}% vegetation`));
  box.append(el("div", { class: "muted" }, `Captured ${properties.date || "–"}`));
  if (properties.h !== null && properties.h !== undefined) {
    box.append(el("div", { class: "muted" }, `Altitude ${formatNumber(properties.h, 1)} m`));
  }
  if (withLink) {
    const link = el(
      "a",
      { href: `https://www.mapillary.com/app/?pKey=${encodeURIComponent(properties.id)}`, target: "_blank", rel: "noopener" },
      "Open the image on Mapillary ↗"
    );
    const row = el("div");
    row.append(link);
    box.append(row);
  }
  return box;
}

async function initCityMap(config) {
  let basemap = storedBasemap();
  let currentMetric = "veg_median";
  applyTheme(basemap);
  renderCityStats(config);
  renderLegend(theme(basemap), currentMetric);
  setupInfoToggle();

  const fetchJson = (url) =>
    fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);

  const [points, boundary, segments, voronoi] = await Promise.all([
    fetchJson("points.geojson"),
    fetchJson("boundary.geojson"),
    fetchJson("segments.geojson"),
    fetchJson("voronoi.geojson"),
  ]);

  const hasSegments = !!segments && segments.features && segments.features.length > 0;
  const hasVoronoi = !!voronoi && voronoi.features && voronoi.features.length > 0;
  const hasPoints = !!points && points.features && points.features.length > 0;

  // Setup layer checkboxes
  const chkSegments = document.getElementById("layer-segments");
  const chkVoronoi = document.getElementById("layer-voronoi");
  const chkPoints = document.getElementById("layer-points");
  const metricSelect = document.getElementById("metric-select");

  if (chkSegments) chkSegments.disabled = !hasSegments;
  if (chkVoronoi) chkVoronoi.disabled = !hasVoronoi;
  if (chkPoints) chkPoints.disabled = !hasPoints;

  // If segments exist, segments is active layer by default and points off
  if (hasSegments) {
    if (chkSegments) chkSegments.checked = true;
    if (chkPoints) chkPoints.checked = false;
  } else {
    if (chkSegments) chkSegments.checked = false;
    if (chkPoints) chkPoints.checked = true;
  }

  const map = createMap(basemap, { bounds: config.bounds, fitBoundsOptions: { padding: 40 } });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

  function addLayers() {
    const themeName = theme(basemap);

    if (boundary && !map.getSource("boundary")) {
      map.addSource("boundary", { type: "geojson", data: boundary });
      map.addLayer({
        id: "boundary-line",
        type: "line",
        source: "boundary",
        paint: {
          "line-color": themeName === "dark" ? "#c3c2b7" : "#52514e",
          "line-width": 1.5,
          "line-opacity": 0.7,
          "line-dasharray": [3, 2],
        },
      });
    }

    if (hasVoronoi && !map.getSource("voronoi")) {
      map.addSource("voronoi", { type: "geojson", data: voronoi });
      map.addLayer({
        id: "voronoi-fill",
        type: "fill",
        source: "voronoi",
        layout: { visibility: chkVoronoi && chkVoronoi.checked ? "visible" : "none" },
        paint: {
          "fill-color": metricColorExpression(currentMetric, themeName),
          "fill-opacity": [
            "case",
            ["==", ["coalesce", ["get", "count"], 0], 0],
            0.05,
            0.35,
          ],
        },
      });
      map.addLayer({
        id: "voronoi-line",
        type: "line",
        source: "voronoi",
        layout: { visibility: chkVoronoi && chkVoronoi.checked ? "visible" : "none" },
        paint: {
          "line-color": themeName === "dark" ? "#666660" : "#999990",
          "line-width": 0.6,
          "line-opacity": 0.4,
        },
      });
    }

    if (hasSegments && !map.getSource("segments")) {
      map.addSource("segments", { type: "geojson", data: segments });
      map.addLayer({
        id: "segments-line",
        type: "line",
        source: "segments",
        layout: {
          visibility: chkSegments && chkSegments.checked ? "visible" : "none",
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": metricColorExpression(currentMetric, themeName),
          "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 13, 2.5, 15, 4.5, 17, 7.5],
          "line-opacity": [
            "case",
            ["==", ["coalesce", ["get", "count"], 0], 0],
            0.35,
            0.95,
          ],
        },
      });
    }

    if (hasPoints && !map.getSource("points")) {
      map.addSource("points", { type: "geojson", data: points });
      map.addLayer({
        id: "points",
        type: "circle",
        source: "points",
        layout: { visibility: chkPoints && chkPoints.checked ? "visible" : "none" },
        paint: {
          "circle-color": metricColorExpression("veg", themeName, false),
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 14, 4, 17, 7],
          "circle-stroke-color": SURFACES[themeName],
          "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 14, 1, 17, 2],
        },
      });
    }
  }

  map.on("style.load", addLayers);

  // Dynamic metric selection handler
  function updateMetricStyles() {
    const themeName = theme(basemap);
    const expr = metricColorExpression(currentMetric, themeName);
    if (map.getLayer("segments-line")) {
      map.setPaintProperty("segments-line", "line-color", expr);
    }
    if (map.getLayer("voronoi-fill")) {
      map.setPaintProperty("voronoi-fill", "fill-color", expr);
    }
    renderLegend(themeName, currentMetric);
  }

  if (metricSelect) {
    metricSelect.addEventListener("change", (e) => {
      currentMetric = e.target.value;
      updateMetricStyles();
    });
  }

  // Layer toggle event listeners
  if (chkSegments) {
    chkSegments.addEventListener("change", (e) => {
      if (map.getLayer("segments-line")) {
        map.setLayoutProperty("segments-line", "visibility", e.target.checked ? "visible" : "none");
      }
    });
  }
  if (chkVoronoi) {
    chkVoronoi.addEventListener("change", (e) => {
      const vis = e.target.checked ? "visible" : "none";
      if (map.getLayer("voronoi-fill")) map.setLayoutProperty("voronoi-fill", "visibility", vis);
      if (map.getLayer("voronoi-line")) map.setLayoutProperty("voronoi-line", "visibility", vis);
    });
  }
  if (chkPoints) {
    chkPoints.addEventListener("change", (e) => {
      if (map.getLayer("points")) {
        map.setLayoutProperty("points", "visibility", e.target.checked ? "visible" : "none");
      }
    });
  }

  // Interactive Popups
  const hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });

  // Hover on segments
  map.on("mousemove", "segments-line", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const feat = e.features[0];
    hoverPopup.setLngLat(e.lngLat).setDOMContent(segmentPopupContent(feat.properties, currentMetric)).addTo(map);
  });
  map.on("mouseleave", "segments-line", () => {
    map.getCanvas().style.cursor = "";
    hoverPopup.remove();
  });
  map.on("click", "segments-line", (e) => {
    hoverPopup.remove();
    const feat = e.features[0];
    new maplibregl.Popup({ offset: 8 })
      .setLngLat(e.lngLat)
      .setDOMContent(segmentPopupContent(feat.properties, currentMetric))
      .addTo(map);
  });

  // Click on Voronoi polygons
  map.on("mousemove", "voronoi-fill", (e) => {
    // If segments line is visible and hovered, segments take priority
    if (chkSegments && chkSegments.checked) return;
    map.getCanvas().style.cursor = "pointer";
    const feat = e.features[0];
    hoverPopup.setLngLat(e.lngLat).setDOMContent(segmentPopupContent(feat.properties, currentMetric)).addTo(map);
  });
  map.on("mouseleave", "voronoi-fill", () => {
    if (chkSegments && chkSegments.checked) return;
    map.getCanvas().style.cursor = "";
    hoverPopup.remove();
  });
  map.on("click", "voronoi-fill", (e) => {
    if (chkSegments && chkSegments.checked) return;
    hoverPopup.remove();
    const feat = e.features[0];
    new maplibregl.Popup({ offset: 8 })
      .setLngLat(e.lngLat)
      .setDOMContent(segmentPopupContent(feat.properties, currentMetric))
      .addTo(map);
  });

  // Points popups
  map.on("mousemove", "points", (event) => {
    map.getCanvas().style.cursor = "pointer";
    const feature = event.features[0];
    hoverPopup.setLngLat(feature.geometry.coordinates).setDOMContent(pointPopupContent(feature.properties, false)).addTo(map);
  });
  map.on("mouseleave", "points", () => {
    map.getCanvas().style.cursor = "";
    hoverPopup.remove();
  });
  map.on("click", "points", (event) => {
    hoverPopup.remove();
    const feature = event.features[0];
    new maplibregl.Popup({ offset: 8 })
      .setLngLat(feature.geometry.coordinates)
      .setDOMContent(pointPopupContent(feature.properties, true))
      .addTo(map);
  });

  buildSwitcher(basemap, (name) => {
    basemap = name;
    storeBasemap(name);
    applyTheme(name);
    renderLegend(theme(name), currentMetric);
    map.setStyle(BASEMAPS[name].url);
  });
}

// ---------------------------------------------------------------------------
// Index map
// ---------------------------------------------------------------------------

function renderCityList(cities) {
  const list = document.getElementById("cities");
  list.replaceChildren();
  document.getElementById("city-count").textContent =
    cities.length === 1 ? "1 city" : `${cities.length} cities`;
  for (const city of cities) {
    const item = el("li");
    item.append(el("a", { href: `./${city.slug}/` }, city.name));
    const s = city.stats;
    item.append(el("div", { class: "muted" },
      `${formatNumber(s.images)} images · median vegetation ${formatNumber(s.vegetation_median, 1)}% · ` +
      `${formatNumber(s.tiles_completed)} / ${formatNumber(s.tiles_total)} tiles`));
    list.appendChild(item);
  }
}

async function initIndexMap() {
  let basemap = storedBasemap();
  applyTheme(basemap);
  setupInfoToggle();

  const [cities, boundaries] = await Promise.all([
    fetch("cities.json").then((r) => r.json()),
    fetch("cities.geojson").then((r) => r.json()),
  ]);
  renderCityList(cities);

  const options = {};
  if (cities.length) {
    const b = cities.map((c) => c.bounds);
    options.bounds = [
      Math.min(...b.map((x) => x[0])), Math.min(...b.map((x) => x[1])),
      Math.max(...b.map((x) => x[2])), Math.max(...b.map((x) => x[3])),
    ];
    options.fitBoundsOptions = { padding: 80, maxZoom: 10 };
  } else {
    options.center = [0, 20];
    options.zoom = 1;
  }
  const map = createMap(basemap, options);
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

  function addLayers() {
    const accent = theme(basemap) === "dark" ? "#82c96a" : "#22691a";
    if (!map.getSource("cities")) map.addSource("cities", { type: "geojson", data: boundaries });
    map.addLayer({ id: "cities-fill", type: "fill", source: "cities", paint: { "fill-color": accent, "fill-opacity": 0.15 } });
    map.addLayer({ id: "cities-line", type: "line", source: "cities", paint: { "line-color": accent, "line-width": 2 } });
  }
  map.on("style.load", addLayers);

  map.on("click", "cities-fill", (event) => {
    window.location.href = `./${event.features[0].properties.slug}/`;
  });
  map.on("mouseenter", "cities-fill", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "cities-fill", () => { map.getCanvas().style.cursor = ""; });

  // labeled markers (HTML, independent of the basemap's fonts)
  for (const city of cities) {
    const marker = el("div", { class: "city-marker", title: `Open the map of ${city.name}` }, city.name);
    marker.addEventListener("click", () => { window.location.href = `./${city.slug}/`; });
    new maplibregl.Marker({ element: marker }).setLngLat(city.center).addTo(map);
  }

  buildSwitcher(basemap, (name) => {
    basemap = name;
    storeBasemap(name);
    applyTheme(name);
    map.setStyle(BASEMAPS[name].url);
  });
}
