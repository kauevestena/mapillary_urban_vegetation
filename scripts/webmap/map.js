/* Shared code of the city maps and the index map (MapLibre GL JS). */

const BASEMAPS = {
  positron: { label: "Positron", url: "https://tiles.openfreemap.org/styles/positron", dark: false },
  dark: { label: "Dark", url: "https://tiles.openfreemap.org/styles/dark", dark: true },
  liberty: { label: "Regular", url: "https://tiles.openfreemap.org/styles/liberty", dark: false },
};
const DEFAULT_BASEMAP = "positron";

// Vegetation percent classes, one green hue from low to high vegetation
// (validated ordinal ramps: darker = more vegetation on light basemaps,
// brighter = more vegetation on the dark one).
const VEGETATION_BREAKS = [10, 20, 30, 40];
const VEGETATION_RAMPS = {
  light: ["#74b85b", "#529e3f", "#378329", "#22691a", "#114f0c"],
  dark: ["#2f7d2c", "#46983c", "#62b24f", "#82c96a", "#a8dd8f"],
};
const VEGETATION_LABELS = ["< 10%", "10–20%", "20–30%", "30–40%", "≥ 40%"];
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
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
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
  // at phone width, keep the attribution folded into its (i) button so it
  // does not run under the info panel
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

function vegetationColor(themeName) {
  const ramp = VEGETATION_RAMPS[themeName];
  const expression = ["step", ["get", "veg"], ramp[0]];
  VEGETATION_BREAKS.forEach((limit, i) => expression.push(limit, ramp[i + 1]));
  return expression;
}

function renderLegend(themeName) {
  const legend = document.getElementById("legend");
  legend.replaceChildren(el("h2", {}, "Vegetation in the image"));
  VEGETATION_RAMPS[themeName].forEach((color, i) => {
    const row = el("div", { class: "legend-row" });
    const swatch = el("span", { class: "legend-swatch" });
    swatch.style.background = color;
    row.append(swatch, el("span", {}, VEGETATION_LABELS[i]));
    legend.appendChild(row);
  });
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

function popupContent(properties, withLink) {
  const box = el("div");
  box.append(el("div", { class: "popup-value" }, `${formatNumber(properties.veg, 1)}% vegetation`));
  box.append(el("div", { class: "muted" }, `Captured ${properties.date || "–"}`));
  if (properties.h !== null && properties.h !== undefined) {
    box.append(el("div", { class: "muted" }, `Altitude ${formatNumber(properties.h, 1)} m`));
  }
  if (withLink) {
    const link = el("a", { href: `https://www.mapillary.com/app/?pKey=${encodeURIComponent(properties.id)}`, target: "_blank", rel: "noopener" }, "Open the image on Mapillary ↗");
    const row = el("div");
    row.append(link);
    box.append(row);
  }
  return box;
}

async function initCityMap(config) {
  let basemap = storedBasemap();
  applyTheme(basemap);
  renderCityStats(config);
  renderLegend(theme(basemap));
  setupInfoToggle();

  const [points, boundary] = await Promise.all([
    fetch("points.geojson").then((r) => r.json()),
    fetch("boundary.geojson").then((r) => r.json()),
  ]);

  const map = createMap(basemap, { bounds: config.bounds, fitBoundsOptions: { padding: 40 } });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

  function addLayers() {
    const themeName = theme(basemap);
    if (!map.getSource("boundary")) map.addSource("boundary", { type: "geojson", data: boundary });
    if (!map.getSource("points")) map.addSource("points", { type: "geojson", data: points });
    map.addLayer({
      id: "boundary-line",
      type: "line",
      source: "boundary",
      paint: { "line-color": themeName === "dark" ? "#c3c2b7" : "#52514e", "line-width": 1.5, "line-opacity": 0.7, "line-dasharray": [3, 2] },
    });
    map.addLayer({
      id: "points",
      type: "circle",
      source: "points",
      paint: {
        "circle-color": vegetationColor(themeName),
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 14, 4, 17, 7],
        "circle-stroke-color": SURFACES[themeName],
        "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 14, 1, 17, 2],
      },
    });
  }

  map.on("style.load", addLayers);

  const hover = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
  map.on("mousemove", "points", (event) => {
    map.getCanvas().style.cursor = "pointer";
    const feature = event.features[0];
    hover.setLngLat(feature.geometry.coordinates).setDOMContent(popupContent(feature.properties, false)).addTo(map);
  });
  map.on("mouseleave", "points", () => {
    map.getCanvas().style.cursor = "";
    hover.remove();
  });
  map.on("click", "points", (event) => {
    hover.remove();
    const feature = event.features[0];
    new maplibregl.Popup({ offset: 8 })
      .setLngLat(feature.geometry.coordinates)
      .setDOMContent(popupContent(feature.properties, true))
      .addTo(map);
  });

  buildSwitcher(basemap, (name) => {
    basemap = name;
    storeBasemap(name);
    applyTheme(name);
    renderLegend(theme(name));
    map.setStyle(BASEMAPS[name].url);  // style.load re-adds the data layers
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
