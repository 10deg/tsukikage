/* The interactive lunar map.
 *
 * The page keeps its own equirectangular view: a 2:1 world whose tile level is
 * an integer and whose scale is continuous, so a wheel zoom is smooth while
 * the image is still fetched from a real tile pyramid.  Every layer below is
 * data-driven -- the server names the feature classes, the page only decides
 * how to draw them -- which is the extension point for later overlays.
 */

'use strict';

const TILE = 256;
const MIN_ZOOM = 0;
const MAX_ZOOM = 6;
const KM_PER_DEGREE = Math.PI / 180 * 1737.4;

const LAYER_COLORS = {
  basin: '#e6b566',
  mare: '#86b3a8',
  crater: '#a8c6e8',
  lacus: '#8fc6cc',
  sinus: '#8fa8dc',
  palus: '#a9b47a',
  rima: '#e09a9a',
  vallis: '#c8a0d4',
  mons: '#dcc98a',
  dome: '#c9b98a',
  dorsum: '#bfa886',
  rupes: '#d4a179',
  catena: '#9aa8cc',
  promontorium: '#9ed4b4',
  statio: '#e9a86b',
  pit: '#9fd0c0',
  pyroclastic: '#d9a0b4',
  imp: '#bcd0a0',
  impact: '#f0c0a0',
  albedo: '#c9c9dc',
  lunar100: '#d9b8f2',
  other: '#b9bec9',
};

const POINT_CLASSES = new Set(['statio', 'lunar100', 'impact', 'albedo', 'imp', 'other']);

const $ = (id) => document.getElementById(id);

const canvas = $('map-canvas');
const stage = $('map-stage');
const ctx = canvas.getContext('2d');

const state = {
  health: null,
  layers: null,
  active: new Set(),
  labels: true,
  diameters: true,
  brightness: 1,
  zoom: 0,
  scale: 1,
  centerLon: 0,
  centerLat: 0,
  width: 1,
  height: 1,
  dpr: 1,
  tiles: new Map(),
  loading: 0,
  features: [],
  featureTotal: 0,
  featureSeq: 0,
  featureTimer: null,
  selected: null,
  hover: null,
  drag: null,
  pointer: null,
  pointSeq: 0,
  pointTimer: null,
  hashTimer: null,
  fitted: false,
};

/* ── view maths ─────────────────────────────────────────────────────────── */

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function worldSize() {
  const rows = 2 ** state.zoom;
  return { cols: rows * 2, rows, w: rows * 2 * TILE, h: rows * TILE };
}

function camera() {
  const world = worldSize();
  return {
    x: (state.centerLon + 180) / 360 * world.w,
    y: (90 - state.centerLat) / 180 * world.h,
  };
}

function lonlatToWorld(lon, lat) {
  const world = worldSize();
  return {
    x: (lon + 180) / 360 * world.w,
    y: (90 - lat) / 180 * world.h,
  };
}

function worldToScreen(x, y) {
  const cam = camera();
  return {
    x: (x - cam.x) * state.scale + state.width / 2,
    y: (y - cam.y) * state.scale + state.height / 2,
  };
}

function lonlatToScreen(lon, lat) {
  const point = lonlatToWorld(lon, lat);
  return worldToScreen(point.x, point.y);
}

function screenToLonLat(sx, sy) {
  const world = worldSize();
  const cam = camera();
  const x = cam.x + (sx - state.width / 2) / state.scale;
  const y = cam.y + (sy - state.height / 2) / state.scale;
  return {
    lon: clamp(x / world.w * 360 - 180, -180, 180),
    lat: clamp(90 - y / world.h * 180, -90, 90),
  };
}

function clampCenter() {
  const world = worldSize();
  const halfW = state.width / (2 * state.scale);
  const halfH = state.height / (2 * state.scale);
  let cx = (state.centerLon + 180) / 360 * world.w;
  let cy = (90 - state.centerLat) / 180 * world.h;
  if (world.w <= halfW * 2) cx = world.w / 2;
  else cx = clamp(cx, halfW, world.w - halfW);
  if (world.h <= halfH * 2) cy = world.h / 2;
  else cy = clamp(cy, halfH, world.h - halfH);
  state.centerLon = cx / world.w * 360 - 180;
  state.centerLat = 90 - cy / world.h * 180;
}

function fitView() {
  const small = Math.min(state.width / TILE, state.height / (TILE / 2));
  state.zoom = clamp(Math.round(Math.log2(Math.max(1, small / 2))), MIN_ZOOM, 2);
  const world = worldSize();
  state.scale = Math.min(
    state.width / world.w, state.height / world.h,
  ) * 0.94;
  state.centerLon = 0;
  state.centerLat = 0;
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

function zoomTo(nextZoom, anchor) {
  const pivot = anchor || { x: state.width / 2, y: state.height / 2 };
  const geo = screenToLonLat(pivot.x, pivot.y);
  state.zoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
  const world = worldSize();
  const point = lonlatToWorld(geo.lon, geo.lat);
  state.centerLon = ((point.x - (pivot.x - state.width / 2) / state.scale)
    / world.w) * 360 - 180;
  state.centerLat = 90 - ((point.y - (pivot.y - state.height / 2) / state.scale)
    / world.h) * 180;
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

function zoomBy(factor, anchor) {
  const pivot = anchor || { x: state.width / 2, y: state.height / 2 };
  const geo = screenToLonLat(pivot.x, pivot.y);
  let nextScale = state.scale * factor;
  let nextZoom = state.zoom;
  while (nextScale >= 2 && nextZoom < MAX_ZOOM) {
    nextScale /= 2;
    nextZoom += 1;
  }
  while (nextScale < 0.5 && nextZoom > MIN_ZOOM) {
    nextScale *= 2;
    nextZoom -= 1;
  }
  state.zoom = nextZoom;
  state.scale = clamp(nextScale, 0.18, 8);
  const world = worldSize();
  const point = lonlatToWorld(geo.lon, geo.lat);
  state.centerLon = ((point.x - (pivot.x - state.width / 2) / state.scale)
    / world.w) * 360 - 180;
  state.centerLat = 90 - ((point.y - (pivot.y - state.height / 2) / state.scale)
    / world.h) * 180;
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

/* ── canvas ─────────────────────────────────────────────────────────────── */

function resizeCanvas() {
  const rect = stage.getBoundingClientRect();
  state.dpr = Math.min(2, window.devicePixelRatio || 1);
  state.width = Math.max(1, rect.width);
  state.height = Math.max(1, rect.height);
  canvas.width = Math.max(1, Math.round(state.width * state.dpr));
  canvas.height = Math.max(1, Math.round(state.height * state.dpr));
  canvas.style.width = state.width + 'px';
  canvas.style.height = state.height + 'px';
  if (!state.fitted) {
    state.fitted = true;
    fitView();
  } else {
    clampCenter();
    render();
  }
}

function render() {
  ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  ctx.clearRect(0, 0, state.width, state.height);
  drawBackground();
  if (state.active.has('base')) drawTiles();
  if (state.active.has('graticule')) drawGraticule();
  drawFeatures();
  drawSelectionRing();
  drawScaleBar();
  const zoomLabel = $('zoom-label');
  if (zoomLabel) zoomLabel.textContent = `z${state.zoom} · ${state.scale.toFixed(1)}×`;
}

function drawBackground() {
  ctx.fillStyle = '#05070b';
  ctx.fillRect(0, 0, state.width, state.height);
}

function tileKey(z, x, y) {
  return z + '/' + x + '/' + y;
}

function tileEntry(z, x, y) {
  const key = tileKey(z, x, y);
  let entry = state.tiles.get(key);
  if (entry) return entry;
  entry = { image: new Image(), ready: false, failed: false };
  state.tiles.set(key, entry);
  while (state.tiles.size > 360) {
    const oldest = state.tiles.keys().next().value;
    state.tiles.delete(oldest);
  }
  state.loading += 1;
  updateLoading();
  entry.image.onload = () => {
    entry.ready = true;
    state.loading = Math.max(0, state.loading - 1);
    updateLoading();
    render();
  };
  entry.image.onerror = () => {
    entry.failed = true;
    state.loading = Math.max(0, state.loading - 1);
    updateLoading();
  };
  entry.image.src = `/api/map/tile/${z}/${x}/${y}.png`;
  return entry;
}

function drawTiles() {
  const world = worldSize();
  const cam = camera();
  const left = cam.x - state.width / (2 * state.scale);
  const top = cam.y - state.height / (2 * state.scale);
  const right = cam.x + state.width / (2 * state.scale);
  const bottom = cam.y + state.height / (2 * state.scale);
  const x0 = Math.max(0, Math.floor(left / TILE));
  const x1 = Math.min(world.cols - 1, Math.floor(right / TILE));
  const y0 = Math.max(0, Math.floor(top / TILE));
  const y1 = Math.min(world.rows - 1, Math.floor(bottom / TILE));
  ctx.save();
  ctx.globalAlpha = state.brightness;
  for (let y = y0; y <= y1; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      const entry = tileEntry(state.zoom, x, y);
      const screenX = (x * TILE - cam.x) * state.scale + state.width / 2;
      const screenY = (y * TILE - cam.y) * state.scale + state.height / 2;
      const size = TILE * state.scale;
      if (entry.ready) {
        ctx.drawImage(entry.image, screenX, screenY, size, size);
      } else if (!drawAncestor(x, y, screenX, screenY, size)) {
        ctx.fillStyle = '#0a0f17';
        ctx.fillRect(screenX, screenY, size, size);
      }
    }
  }
  ctx.restore();
}

function drawAncestor(x, y, screenX, screenY, size) {
  for (let depth = 1; depth <= state.zoom; depth += 1) {
    const z = state.zoom - depth;
    const span = 2 ** depth;
    const parentX = Math.floor(x / span);
    const parentY = Math.floor(y / span);
    const entry = state.tiles.get(tileKey(z, parentX, parentY));
    if (!entry || !entry.ready) continue;
    const sx = ((x - parentX * span) / span) * TILE;
    const sy = ((y - parentY * span) / span) * TILE;
    const sw = TILE / span;
    ctx.drawImage(entry.image, sx, sy, sw, sw, screenX, screenY, size, size);
    return true;
  }
  return false;
}

function drawGraticule() {
  const world = worldSize();
  const cam = camera();
  const step = state.zoom >= 5 ? 1 : state.zoom >= 4 ? 2 : state.zoom >= 3 ? 5
    : state.zoom >= 2 ? 10 : 30;
  ctx.save();
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(150, 180, 220, .20)';
  ctx.fillStyle = 'rgba(190, 208, 235, .65)';
  ctx.font = '10px "PingFang SC", system-ui, sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  for (let lon = -180; lon <= 180; lon += step) {
    const point = lonlatToWorld(lon, 0);
    const screen = worldToScreen(point.x, point.y);
    if (screen.x < -20 || screen.x > state.width + 20) continue;
    ctx.beginPath();
    ctx.moveTo(screen.x, 0);
    ctx.lineTo(screen.x, state.height);
    ctx.stroke();
    ctx.fillText(`${Math.abs(lon)}°${lon < 0 ? 'W' : lon > 0 ? 'E' : ''}`,
                 screen.x + 3, 4);
  }
  ctx.textAlign = 'left';
  for (let lat = -80; lat <= 80; lat += step) {
    const point = lonlatToWorld(0, lat);
    const screen = worldToScreen(point.x, point.y);
    if (screen.y < -20 || screen.y > state.height + 20) continue;
    ctx.beginPath();
    ctx.moveTo(0, screen.y);
    ctx.lineTo(state.width, screen.y);
    ctx.stroke();
    ctx.fillText(`${Math.abs(lat)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`,
                 5, screen.y + 2);
  }
  ctx.restore();
}

/* ── features ───────────────────────────────────────────────────────────── */

function activeFeatureClasses() {
  const known = new Set((state.layers && state.layers.feature_classes) || []);
  return [...state.active].filter((key) => known.has(key));
}

function scheduleFeatures() {
  if (state.featureTimer) clearTimeout(state.featureTimer);
  state.featureTimer = setTimeout(refreshFeatures, 180);
}

async function refreshFeatures() {
  const classes = activeFeatureClasses();
  if (!classes.length) {
    state.features = [];
    state.featureTotal = 0;
    updateFeatureCount();
    render();
    return;
  }
  const westPoint = screenToLonLat(0, 0);
  const eastPoint = screenToLonLat(state.width, state.height);
  const west = westPoint.lon;
  const east = eastPoint.lon;
  const north = westPoint.lat;
  const south = eastPoint.lat;
  const query = new URLSearchParams({
    west: String(west), east: String(east),
    south: String(south), north: String(north),
    classes: classes.join(','),
    zoom: String(state.zoom),
    limit: '2400',
  });
  const seq = ++state.featureSeq;
  try {
    const response = await fetch('/api/map/features?' + query.toString());
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    if (seq !== state.featureSeq) return;
    state.features = payload.features || [];
    state.featureTotal = Number(payload.total) || state.features.length;
    updateFeatureCount();
    render();
  } catch (error) {
    if (seq !== state.featureSeq) return;
    setStatus('标注载入失败：' + String(error.message || error));
  }
}

function featurePixels(feature) {
  const screen = lonlatToScreen(feature.lon, feature.lat);
  const world = worldSize();
  const kmPerPixel = (KM_PER_DEGREE * Math.cos(feature.lat * Math.PI / 180))
    / ((world.w / 360) * state.scale);
  const diameter = state.diameters ? Math.max(0, Number(feature.diameter_km) || 0) : 0;
  const apparent = kmPerPixel > 0 ? diameter / kmPerPixel : 0;
  return {
    screen,
    radius: Math.max(2.2, Math.min(48, apparent / 2)),
    apparent,
    kmPerPixel,
  };
}

function insideScreen(point, margin) {
  const pad = margin || 12;
  return point.x >= -pad && point.x <= state.width + pad
    && point.y >= -pad && point.y <= state.height + pad;
}

function drawFeatures() {
  const classes = new Set(activeFeatureClasses());
  if (!classes.size) return;
  const items = [];
  for (const feature of state.features) {
    if (!classes.has(feature.class)) continue;
    const pixels = featurePixels(feature);
    if (!insideScreen(pixels.screen, 30)) continue;
    items.push({ feature, ...pixels });
  }
  // Big things first, so a small crater's label is not hidden under a mare.
  items.sort((left, right) => right.feature.diameter_km - left.feature.diameter_km);
  const occupied = [];
  for (const item of items) {
    drawFeatureMarker(item);
    if (state.labels) {
      const box = placeFeatureLabel(item, occupied);
      if (box) occupied.push(box);
    }
  }
}

function drawFeatureMarker(item) {
  const { feature, screen, radius, apparent } = item;
  const colour = LAYER_COLORS[feature.class] || '#c9d4e6';
  ctx.save();
  if (feature.class === 'statio' || feature.class === 'lunar100'
      || feature.class === 'impact') {
    ctx.translate(screen.x, screen.y);
    ctx.rotate(Math.PI / 4);
    ctx.strokeStyle = colour;
    ctx.fillStyle = colour;
    ctx.lineWidth = 1.2;
    ctx.globalAlpha = 0.85;
    ctx.strokeRect(-radius, -radius, radius * 2, radius * 2);
    if (feature.class === 'statio') {
      ctx.globalAlpha = 0.32;
      ctx.fillRect(-radius, -radius, radius * 2, radius * 2);
    }
    ctx.restore();
    return;
  }
  if (apparent > 3 && state.diameters) {
    ctx.beginPath();
    ctx.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = Math.max(1, Math.min(2, radius * 0.08));
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(screen.x, screen.y, Math.max(1.7, Math.min(4, radius * 0.5)),
          0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.globalAlpha = 0.9;
  ctx.fill();
  ctx.restore();
}

function placeFeatureLabel(item, occupied) {
  const { feature, screen, radius } = item;
  const colour = LAYER_COLORS[feature.class] || '#c9d4e6';
  const text = feature.name || feature.name_en;
  if (!text) return null;
  const pointVisible = POINT_CLASSES.has(feature.class) && state.zoom >= 2;
  const visible = state.zoom >= 3 || feature.diameter_km >= 90 || pointVisible;
  if (!visible) return null;
  ctx.save();
  ctx.font = '11px "PingFang SC", system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  const width = ctx.measureText(text).width;
  const height = 14;
  const attempts = [
    { x: screen.x + radius + 6, y: screen.y, align: 'left' },
    { x: screen.x - radius - 6, y: screen.y, align: 'right' },
    { x: screen.x, y: screen.y - radius - 8, align: 'center' },
    { x: screen.x, y: screen.y + radius + 8, align: 'center' },
  ];
  for (const attempt of attempts) {
    const left = attempt.align === 'left' ? attempt.x
      : attempt.align === 'right' ? attempt.x - width : attempt.x - width / 2;
    const box = { left: left - 2, top: attempt.y - height / 2,
                  right: left + width + 2, bottom: attempt.y + height / 2 };
    if (box.left < 2 || box.right > state.width - 2
        || box.top < 2 || box.bottom > state.height - 2) continue;
    const collision = occupied.some((other) => !(box.right < other.left
      || box.left > other.right || box.bottom < other.top || box.top > other.bottom));
    if (collision) continue;
    ctx.textAlign = attempt.align;
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(3, 5, 9, .92)';
    ctx.strokeText(text, attempt.x, attempt.y);
    ctx.fillStyle = colour;
    ctx.fillText(text, attempt.x, attempt.y);
    ctx.restore();
    return box;
  }
  ctx.restore();
  return null;
}

function drawSelectionRing() {
  if (!state.selected) return;
  const pixels = featurePixels(state.selected);
  if (!insideScreen(pixels.screen, 40)) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(pixels.screen.x, pixels.screen.y, Math.max(7, pixels.radius + 3),
          0, Math.PI * 2);
  ctx.strokeStyle = '#ffd27a';
  ctx.lineWidth = 1.6;
  ctx.setLineDash([4, 3]);
  ctx.stroke();
  ctx.restore();
}

function featureAt(sx, sy) {
  const classes = new Set(activeFeatureClasses());
  if (!classes.size) return null;
  let best = null;
  let bestDistance = Infinity;
  for (const feature of state.features) {
    if (!classes.has(feature.class)) continue;
    const pixels = featurePixels(feature);
    const distance = Math.hypot(pixels.screen.x - sx, pixels.screen.y - sy);
    const grab = Math.max(8, Math.min(18, pixels.radius + 4));
    if (distance <= grab && distance < bestDistance) {
      best = feature;
      bestDistance = distance;
    }
  }
  return best;
}

/* ── selection and readouts ─────────────────────────────────────────────── */

function formatCoord(lon, lat) {
  const ew = lon < 0 ? 'W' : lon > 0 ? 'E' : '';
  const ns = lat < 0 ? 'S' : lat > 0 ? 'N' : '';
  return `${Math.abs(lon).toFixed(2)}°${ew}  ${Math.abs(lat).toFixed(2)}°${ns}`;
}

function formatKm(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  if (Math.abs(number) < 1) return `${(number * 1000).toFixed(0)} m`;
  return `${number.toFixed(number < 10 ? 3 : 2)} km`;
}

function selectFeature(feature) {
  state.selected = feature;
  const panel = $('selection-info');
  if (!feature) {
    panel.className = 'selection-info empty';
    panel.textContent = '—';
  } else {
    panel.className = 'selection-info';
    panel.innerHTML = '';
    const title = document.createElement('h3');
    title.textContent = feature.name || feature.name_en;
    const cls = document.createElement('div');
    cls.className = 'class';
    const layer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === feature.class);
    cls.textContent = layer ? layer.label : feature.class;
    const facts = document.createElement('dl');
    for (const [key, value] of [
      ['英文', feature.name_en || '—'],
      ['坐标', formatCoord(feature.lon, feature.lat)],
      ['直径', formatKm(feature.diameter_km)],
      ['类型', feature.kind || '—'],
      ['Lunar 100', feature.lunar100 ? String(feature.lunar100) : '—'],
    ]) {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value;
      facts.appendChild(dt);
      facts.appendChild(dd);
    }
    panel.appendChild(title);
    panel.appendChild(cls);
    panel.appendChild(facts);
    if (feature.note) {
      const note = document.createElement('p');
      note.textContent = feature.note;
      panel.appendChild(note);
    }
  }
  render();
}

function focusFeature(feature) {
  state.zoom = Math.max(4, state.zoom);
  state.scale = 1.2;
  state.centerLon = feature.lon;
  state.centerLat = feature.lat;
  clampCenter();
  selectFeature(feature);
  render();
  scheduleFeatures();
  scheduleHash();
}

function updateFeatureCount() {
  const el = $('feature-count');
  if (el) {
    el.textContent = state.featureTotal
      ? `${state.features.length} / ${state.featureTotal} 个标注`
      : '无标注';
  }
}

function updateLoading() {
  const el = $('map-loading');
  if (el) {
    el.hidden = state.loading <= 0;
    el.textContent = `渲染中… ${state.loading}`;
  }
  if (state.loading <= 0) setStatus('就绪');
  else setStatus(`正在加载 ${state.loading} 块瓦片`);
}

function setStatus(text) {
  const el = $('status');
  if (el) el.textContent = text;
}

function updatePositionReadout(lon, lat, screen) {
  const coords = $('map-coords');
  if (coords) coords.textContent = formatCoord(lon, lat);
  $('fact-lon').textContent = `${lon.toFixed(3)}°`;
  $('fact-lat').textContent = `${lat.toFixed(3)}°`;
  $('fact-zoom').textContent = `z${state.zoom} · ${state.scale.toFixed(2)}×`;
  if (screen) state.pointer = { lon, lat, screen };
  if (state.pointTimer) clearTimeout(state.pointTimer);
  state.pointTimer = setTimeout(() => samplePoint(lon, lat), 220);
}

async function samplePoint(lon, lat) {
  const seq = ++state.pointSeq;
  try {
    const response = await fetch(
      `/api/map/point?lon=${encodeURIComponent(lon)}&lat=${encodeURIComponent(lat)}`);
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    if (seq !== state.pointSeq) return;
    $('fact-elevation').textContent = formatKm(payload.elevation_km);
  } catch (error) {
    if (seq === state.pointSeq) $('fact-elevation').textContent = '—';
  }
}

function drawScaleBar() {
  const world = worldSize();
  const kmPerPixel = (KM_PER_DEGREE * Math.cos(state.centerLat * Math.PI / 180))
    / ((world.w / 360) * state.scale);
  if (!(kmPerPixel > 0)) return;
  const target = kmPerPixel * 130;
  const power = 10 ** Math.floor(Math.log10(Math.max(1e-9, target)));
  const normalised = target / power;
  const nice = normalised >= 5 ? 5 : normalised >= 2 ? 2 : 1;
  const km = nice * power;
  const pixels = clamp(km / kmPerPixel, 24, state.width * 0.55);
  const label = km >= 1 ? `${km.toFixed(km >= 10 ? 0 : 1)} km` : `${(km * 1000).toFixed(0)} m`;
  const text = $('scale-label');
  const rule = $('scale-rule');
  if (text) text.textContent = label;
  if (rule) rule.style.width = pixels.toFixed(0) + 'px';
}

/* ── layers and data ────────────────────────────────────────────────────── */

function layerColour(layer) {
  if (layer.id === 'base') return '#d9c9a3';
  if (layer.id === 'graticule') return '#7aa2f7';
  return LAYER_COLORS[layer.id] || '#b9bec9';
}

function buildLayerList(payload) {
  state.layers = payload;
  const list = $('layer-list');
  list.innerHTML = '';
  state.active.clear();
  let group = '';
  for (const layer of payload.layers || []) {
    const kind = layer.type === 'features' ? '地貌标注' : '底图';
    if (kind !== group) {
      group = kind;
      const heading = document.createElement('div');
      heading.className = 'layer-group';
      heading.textContent = group;
      list.appendChild(heading);
    }
    const label = document.createElement('label');
    label.className = 'layer-item';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = Boolean(layer.default);
    input.dataset.layer = layer.id;
    if (input.checked) state.active.add(layer.id);
    const swatch = document.createElement('i');
    swatch.className = 'layer-swatch';
    swatch.style.background = layerColour(layer);
    const text = document.createElement('span');
    text.className = 'layer-label';
    text.textContent = layer.label;
    label.appendChild(input);
    label.appendChild(swatch);
    label.appendChild(text);
    if (layer.count !== undefined) {
      const count = document.createElement('span');
      count.className = 'layer-count';
      count.textContent = layer.count.toLocaleString();
      label.appendChild(count);
    }
    input.addEventListener('change', () => {
      if (input.checked) state.active.add(layer.id);
      else state.active.delete(layer.id);
      if (layer.type === 'features') refreshFeatures();
      else render();
      updateLayerHint();
    });
    list.appendChild(label);
  }
  updateLayerHint();
  render();
  refreshFeatures();
}

function updateLayerHint() {
  const count = activeFeatureClasses().length;
  const el = $('layer-count');
  if (el) el.textContent = count ? `${count} 层` : '';
}

function setAllLayers(on) {
  for (const input of document.querySelectorAll('#layer-list input[data-layer]')) {
    const layer = (state.layers.layers || []).find((item) => item.id === input.dataset.layer);
    if (!layer || layer.type !== 'features') continue;
    input.checked = on;
    if (on) state.active.add(layer.id);
    else state.active.delete(layer.id);
  }
  updateLayerHint();
  refreshFeatures();
  render();
}

async function loadLayers() {
  const response = await fetch('/api/map/layers');
  if (!response.ok) throw new Error(await response.text());
  buildLayerList(await response.json());
}

async function loadHealth() {
  const response = await fetch('/api/map/health');
  if (!response.ok) throw new Error(await response.text());
  state.health = await response.json();
  const health = state.health;
  const dem = health.dem || {};
  const albedo = health.albedo || {};
  $('fact-dem').textContent = dem.name
    ? `${dem.name} · ${formatKm(dem.level0_km_per_pixel)}`
    : '—';
  $('fact-albedo').textContent = albedo.name
    ? `${albedo.name} · ${formatKm(albedo.level0_km_per_pixel)}`
    : '—';
  $('fact-features').textContent = health.feature_count
    ? health.feature_count.toLocaleString() : '—';
  const source = (health.sources && health.sources[0])
    ? `${health.sources[0].name} · ${formatKm(health.sources[0].km_per_px)}`
    : (dem.name || '');
  $('source-label').textContent = source;
}

/* ── search ─────────────────────────────────────────────────────────────── */

function renderSearchResults(results) {
  const box = $('search-results');
  box.innerHTML = '';
  if (!results.length) {
    box.hidden = true;
    return;
  }
  for (const row of results) {
    const item = document.createElement('div');
    item.className = 'search-result';
    item.tabIndex = 0;
    const left = document.createElement('b');
    left.textContent = row.name || row.name_en;
    const right = document.createElement('small');
    const rowLayer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === row.class);
    right.textContent = `${rowLayer ? rowLayer.label : row.class} · `
      + formatCoord(row.lon, row.lat);
    item.appendChild(left);
    item.appendChild(right);
    const choose = () => {
      box.hidden = true;
      $('search-input').value = row.name || row.name_en;
      focusFeature(row);
    };
    item.addEventListener('click', choose);
    item.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') choose();
    });
    box.appendChild(item);
  }
  box.hidden = false;
}

async function runSearch(event) {
  if (event) event.preventDefault();
  const text = $('search-input').value.trim();
  if (!text) return;
  try {
    const response = await fetch('/api/map/search?q=' + encodeURIComponent(text));
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    renderSearchResults(payload.results || []);
    if (!(payload.results || []).length) setStatus('没有匹配的地名');
  } catch (error) {
    setStatus('搜索失败：' + String(error.message || error));
  }
}

/* ── interaction ────────────────────────────────────────────────────────── */

function stagePoint(event) {
  const rect = stage.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

stage.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  const point = stagePoint(event);
  const hit = featureAt(point.x, point.y);
  if (hit) {
    selectFeature(hit);
    event.preventDefault();
    return;
  }
  state.drag = {
    x: event.clientX, y: event.clientY,
    lon: state.centerLon, lat: state.centerLat,
  };
  stage.classList.add('dragging');
  stage.setPointerCapture(event.pointerId);
  event.preventDefault();
});

stage.addEventListener('pointermove', (event) => {
  const point = stagePoint(event);
  const geo = screenToLonLat(point.x, point.y);
  updatePositionReadout(geo.lon, geo.lat, point);
  if (state.drag) {
    const world = worldSize();
    const dx = event.clientX - state.drag.x;
    const dy = event.clientY - state.drag.y;
    state.centerLon = state.drag.lon - dx / state.scale / world.w * 360;
    state.centerLat = state.drag.lat + dy / state.scale / world.h * 180;
    clampCenter();
    render();
    scheduleFeatures();
    scheduleHash();
    hideTooltip();
    return;
  }
  const hit = featureAt(point.x, point.y);
  state.hover = hit;
  if (hit) {
    stage.style.cursor = 'pointer';
    showTooltip(hit, point);
  } else {
    stage.style.cursor = '';
    hideTooltip();
  }
});

function endDrag(event) {
  if (!state.drag) return;
  state.drag = null;
  stage.classList.remove('dragging');
  if (event && event.pointerId !== undefined) {
    try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* noop */ }
  }
}

stage.addEventListener('pointerup', endDrag);
stage.addEventListener('pointercancel', endDrag);
stage.addEventListener('pointerleave', () => {
  hideTooltip();
  $('map-coords').textContent = '—';
});

stage.addEventListener('wheel', (event) => {
  event.preventDefault();
  const point = stagePoint(event);
  zoomBy(Math.exp(-event.deltaY * 0.0016), point);
}, { passive: false });

function showTooltip(feature, point) {
  const box = $('map-tooltip');
  if (!box) return;
  box.innerHTML = '';
  const title = document.createElement('b');
  title.textContent = feature.name || feature.name_en;
  const detail = document.createElement('small');
  detail.textContent = `${feature.class} · ${formatKm(feature.diameter_km)} · `
    + formatCoord(feature.lon, feature.lat);
  box.appendChild(title);
  box.appendChild(detail);
  box.hidden = false;
  const width = box.offsetWidth || 180;
  box.style.left = Math.min(state.width - width - 8, point.x + 14) + 'px';
  box.style.top = Math.max(4, point.y + 14) + 'px';
}

function hideTooltip() {
  const box = $('map-tooltip');
  if (box) box.hidden = true;
}

/* ── url hash ───────────────────────────────────────────────────────────── */

function scheduleHash() {
  if (state.hashTimer) clearTimeout(state.hashTimer);
  state.hashTimer = setTimeout(writeHash, 250);
}

function writeHash() {
  const hash = `#${state.centerLon.toFixed(4)},${state.centerLat.toFixed(4)},`
    + `${state.zoom},${state.scale.toFixed(3)}`;
  try {
    history.replaceState(null, '', hash);
  } catch (error) { /* file:// or a locked-down host */ }
}

function readHash() {
  const raw = (window.location.hash || '').replace(/^#/, '');
  const parts = raw.split(',');
  if (parts.length < 3) return false;
  const lon = Number(parts[0]);
  const lat = Number(parts[1]);
  const zoom = Number(parts[2]);
  const scale = Number(parts[3]);
  if (![lon, lat, zoom].every(Number.isFinite)) return false;
  state.centerLon = clamp(lon, -180, 180);
  state.centerLat = clamp(lat, -90, 90);
  state.zoom = clamp(Math.round(zoom), MIN_ZOOM, MAX_ZOOM);
  if (Number.isFinite(scale) && scale > 0) state.scale = clamp(scale, 0.18, 8);
  return true;
}

/* ── wiring ─────────────────────────────────────────────────────────────── */

function wire() {
  $('zoom-in').addEventListener('click', () => zoomBy(1.6));
  $('zoom-out').addEventListener('click', () => zoomBy(1 / 1.6));
  $('fit').addEventListener('click', fitView);
  $('search-form').addEventListener('submit', runSearch);
  $('search-input').addEventListener('input', () => {
    if (!$('search-input').value.trim()) $('search-results').hidden = true;
  });
  $('layers-all').addEventListener('click', () => setAllLayers(true));
  $('layers-none').addEventListener('click', () => setAllLayers(false));
  $('labels-toggle').addEventListener('change', (event) => {
    state.labels = event.target.checked;
    render();
  });
  $('diameter-toggle').addEventListener('change', (event) => {
    state.diameters = event.target.checked;
    render();
  });
  $('base-brightness').addEventListener('input', (event) => {
    state.brightness = Number(event.target.value) || 1;
    render();
  });
  $('clear-selection').addEventListener('click', () => {
    selectFeature(null);
  });
  $('copy-coords').addEventListener('click', async () => {
    const text = state.pointer
      ? formatCoord(state.pointer.lon, state.pointer.lat)
      : formatCoord(state.centerLon, state.centerLat);
    try {
      await navigator.clipboard.writeText(text);
      setStatus('坐标已复制');
    } catch (error) {
      setStatus('复制失败');
    }
  });
  $('export-map').addEventListener('click', () => {
    const link = document.createElement('a');
    link.download = `lunar-map-${Date.now()}.png`;
    link.href = canvas.toDataURL('image/png');
    link.click();
  });
  document.addEventListener('keydown', (event) => {
    if (event.target && /input|textarea|select/i.test(event.target.tagName)) return;
    if (event.key === '+' || event.key === '=') { zoomBy(1.6); event.preventDefault(); }
    else if (event.key === '-' || event.key === '_') { zoomBy(1 / 1.6); event.preventDefault(); }
    else if (event.key === '0') { fitView(); event.preventDefault(); }
    else if (event.key === 'ArrowLeft') { panBy(-0.18, 0); event.preventDefault(); }
    else if (event.key === 'ArrowRight') { panBy(0.18, 0); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { panBy(0, 0.18); event.preventDefault(); }
    else if (event.key === 'ArrowDown') { panBy(0, -0.18); event.preventDefault(); }
    else if (event.key === 'Escape') { $('search-results').hidden = true; }
  });
  window.addEventListener('resize', resizeCanvas);
  if (window.ResizeObserver) {
    new ResizeObserver(() => resizeCanvas()).observe(stage);
  }
}

function panBy(fractionX, fractionY) {
  const world = worldSize();
  state.centerLon += fractionX * state.width / state.scale / world.w * 360;
  state.centerLat += fractionY * state.height / state.scale / world.h * 180;
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

async function boot() {
  wire();
  resizeCanvas();
  readHash();
  clampCenter();
  try {
    await Promise.all([loadHealth(), loadLayers()]);
    setStatus('就绪');
  } catch (error) {
    setStatus('本地地图服务连接失败：' + String(error.message || error));
    return;
  }
  updatePositionReadout(state.centerLon, state.centerLat);
  render();
  scheduleFeatures();
}

boot();
