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
const MAX_ZOOM = 8;
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
  libration: '#c7d8e6',
  other: '#b9bec9',
};

const POINT_CLASSES = new Set(['statio', 'lunar100', 'libration', 'impact', 'albedo', 'imp', 'other']);

const $ = (id) => document.getElementById(id);

function featureName(feature) {
  if (!feature) return '';
  // The Lunar 100 is published in English; its number and name stay English in
  // both language modes.
  if (feature.class === 'lunar100') {
    return feature.name_en || feature.name || '';
  }
  if (state.language === 'en') {
    return feature.name_en || feature.name_zh || feature.name || '';
  }
  return feature.name_zh || feature.name_en || feature.name || '';
}

function layerLabel(layer) {
  if (!layer) return '';
  return state.language === 'en' ? (layer.label_en || layer.label) : layer.label;
}

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
  language: 'zh',
  density: {},
  projection: 'equirect',
  lighting: null,
  lightingOn: true,
  shadowOn: true,
  lightingKey: '',
  globe: { lon: 0, lat: 0, zoom: 1 },
  sourceCanvas: null,
  sourcePixels: null,
  sourceKey: '',
  sourceLoadingKey: '',
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

function globeRadius() {
  return Math.min(state.width, state.height) * 0.44 * state.globe.zoom;
}

function globeBasis() {
  const lon = state.globe.lon * Math.PI / 180;
  const lat = state.globe.lat * Math.PI / 180;
  const up = [Math.cos(lat) * Math.cos(lon), Math.cos(lat) * Math.sin(lon), Math.sin(lat)];
  const east = [-Math.sin(lon), Math.cos(lon), 0];
  const north = [
    -Math.sin(lat) * Math.cos(lon),
    -Math.sin(lat) * Math.sin(lon),
    Math.cos(lat),
  ];
  return { up, east, north };
}

function mercatorY(lat) {
  const clamped = clamp(lat, -85, 85) * Math.PI / 180;
  return Math.log(Math.tan(Math.PI / 4 + clamped / 2));
}

function lonlatToScreen(lon, lat) {
  if (state.projection === 'globe') {
    const { up, east, north } = globeBasis();
    const lo = lon * Math.PI / 180;
    const la = lat * Math.PI / 180;
    const v = [
      Math.cos(la) * Math.cos(lo),
      Math.cos(la) * Math.sin(lo),
      Math.sin(la),
    ];
    const x = v[0] * east[0] + v[1] * east[1] + v[2] * east[2];
    const y = v[0] * north[0] + v[1] * north[1] + v[2] * north[2];
    const z = v[0] * up[0] + v[1] * up[1] + v[2] * up[2];
    const radius = globeRadius();
    if (z <= 0) return { x: -10000, y: -10000, visible: false };
    return {
      x: state.width / 2 + x * radius,
      y: state.height / 2 - y * radius,
      visible: true,
    };
  }
  if (state.projection === 'mercator') {
    const world = worldSize();
    const pxPerDeg = (world.w / 360) * state.scale;
    const pxPerRad = pxPerDeg * 180 / Math.PI;
    return {
      x: state.width / 2 + (lon - state.centerLon) * pxPerDeg,
      y: state.height / 2 - (mercatorY(lat) - mercatorY(state.centerLat)) * pxPerRad,
      visible: true,
    };
  }
  const point = lonlatToWorld(lon, lat);
  return worldToScreen(point.x, point.y);
}

function screenToLonLat(sx, sy) {
  if (state.projection === 'globe') {
    const radius = globeRadius();
    if (!(radius > 0)) return null;
    const nx = (sx - state.width / 2) / radius;
    const ny = (state.height / 2 - sy) / radius;
    const rho = Math.hypot(nx, ny);
    if (rho > 1) return null;
    const nz = Math.sqrt(Math.max(0, 1 - rho * rho));
    const { up, east, north } = globeBasis();
    const v = [
      nx * east[0] + ny * north[0] + nz * up[0],
      nx * east[1] + ny * north[1] + nz * up[1],
      nx * east[2] + ny * north[2] + nz * up[2],
    ];
    return {
      lon: Math.atan2(v[1], v[0]) * 180 / Math.PI,
      lat: Math.asin(clamp(v[2], -1, 1)) * 180 / Math.PI,
    };
  }
  if (state.projection === 'mercator') {
    const world = worldSize();
    const pxPerDeg = (world.w / 360) * state.scale;
    const pxPerRad = pxPerDeg * 180 / Math.PI;
    if (!(pxPerDeg > 0)) return null;
    const lon = state.centerLon + (sx - state.width / 2) / pxPerDeg;
    const m = mercatorY(state.centerLat) - (sy - state.height / 2) / pxPerRad;
    const lat = (2 * Math.atan(Math.exp(m)) - Math.PI / 2) * 180 / Math.PI;
    return { lon: clamp(lon, -180, 180), lat: clamp(lat, -85, 85) };
  }
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
  if (state.projection === 'globe') {
    state.globe = { lon: 0, lat: 0, zoom: 1 };
    state.centerLon = 0;
    state.centerLat = 0;
    render();
    scheduleFeatures();
    scheduleHash();
    return;
  }
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
  if (state.projection === 'globe') {
    state.globe.zoom = clamp(state.globe.zoom * 2 ** (nextZoom - state.zoom), 0.45, 4.0);
    state.zoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
    render();
    scheduleFeatures();
    return;
  }
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
  if (state.projection === 'globe') {
    state.globe.zoom = clamp(state.globe.zoom * factor, 0.45, 4.0);
    render();
    scheduleFeatures();
    return;
  }
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
  if (state.active.has('base')) {
    if (state.projection === 'equirect') drawTiles();
    else drawProjectedBase();
  }
  if (state.active.has('graticule')) drawGraticule();
  drawLightingOverlay();
  drawFeatures();
  drawSelectionRing();
  drawScaleBar();
  const zoomLabel = $('zoom-label');
  if (zoomLabel) {
    // Magnification relative to the z0 tile grid: each zoom step doubles the
    // number of map pixels on screen, so this is the factor a reader means by
    // "how far in am I", not the raw tile-pixel scale.
    const magnification = state.projection === 'globe'
      ? state.globe.zoom
      : state.scale * 2 ** (state.zoom + 1);
    zoomLabel.textContent = `zoom${state.zoom} · ${magnification.toFixed(1)}x`;
  }
}

function drawBackground() {
  ctx.fillStyle = '#05070b';
  ctx.fillRect(0, 0, state.width, state.height);
}

function tileKey(z, x, y) {
  return z + '/' + x + '/' + y;
}

function tileCacheKey(z, x, y) {
  return tileKey(z, x, y) + '|' + (state.lightingKey || 'plain');
}

function tileEntry(z, x, y) {
  const key = tileCacheKey(z, x, y);
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
  const light = state.lightingKey
    ? `?light=${encodeURIComponent(state.lightingKey)}` : '';
  entry.image.src = `/api/map/tile/${z}/${x}/${y}.png${light}`;
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
    const entry = state.tiles.get(tileCacheKey(z, parentX, parentY));
    if (!entry || !entry.ready) continue;
    const sx = ((x - parentX * span) / span) * TILE;
    const sy = ((y - parentY * span) / span) * TILE;
    const sw = TILE / span;
    ctx.drawImage(entry.image, sx, sy, sw, sw, screenX, screenY, size, size);
    return true;
  }
  return false;
}

function utcNowLocalInput() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}T`
    + `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}`;
}

function unitVector(lon, lat) {
  const lo = lon * Math.PI / 180;
  const la = lat * Math.PI / 180;
  return [Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)];
}

function terminatorLat(lon, subsolar) {
  const sun = unitVector(subsolar.lon, subsolar.lat);
  const lo = lon * Math.PI / 180;
  const a = sun[0] * Math.cos(lo) + sun[1] * Math.sin(lo);
  const b = sun[2];
  if (Math.abs(a) < 1e-12 && Math.abs(b) < 1e-12) return null;
  let lat = Math.atan2(-a, b) * 180 / Math.PI;
  if (lat > 90) lat -= 180;
  if (lat < -90) lat += 180;
  return lat;
}

function drawLightPoint(lon, lat, colour, label) {
  const p = lonlatToScreen(lon, lat);
  if (p.visible === false) return;
  if (p.x < -40 || p.y < -40 || p.x > state.width + 40 || p.y > state.height + 40) return;
  ctx.save();
  ctx.strokeStyle = colour;
  ctx.fillStyle = colour;
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(p.x - 7, p.y); ctx.lineTo(p.x + 7, p.y);
  ctx.moveTo(p.x, p.y - 7); ctx.lineTo(p.x, p.y + 7);
  ctx.stroke();
  ctx.font = '10px "PingFang SC", system-ui, sans-serif';
  ctx.textBaseline = 'bottom';
  ctx.fillText(label, p.x + 8, p.y - 6);
  ctx.restore();
}

function drawLightingOverlay() {
  const light = state.lighting;
  if (!state.lightingOn || !light) return;
  const subsolar = light.subsolar;
  const points = [];
  for (let lon = -180; lon <= 180; lon += 2) {
    const lat = terminatorLat(lon, subsolar);
    if (lat !== null) points.push({ lon, lat });
  }
  if (state.shadowOn && state.projection !== 'globe' && points.length > 2) {
    const edge = subsolar.lat >= 0 ? -90 : 90;
    const p0 = lonlatToScreen(points[0].lon, points[0].lat);
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(p0.x, p0.y);
    for (const point of points.slice(1)) {
      const p = lonlatToScreen(point.lon, point.lat);
      ctx.lineTo(p.x, p.y);
    }
    const last = points[points.length - 1];
    const bottom = lonlatToScreen(last.lon, edge);
    ctx.lineTo(bottom.x, bottom.y);
    const firstEdge = lonlatToScreen(points[0].lon, edge);
    ctx.lineTo(firstEdge.x, firstEdge.y);
    ctx.closePath();
    ctx.fillStyle = 'rgba(0, 0, 0, .30)';
    ctx.fill();
    ctx.restore();
  }
  if (points.length > 2) {
    ctx.save();
    ctx.beginPath();
    let started = false;
    for (const point of points) {
      const p = lonlatToScreen(point.lon, point.lat);
      if (p.visible === false) { started = false; continue; }
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    ctx.strokeStyle = 'rgba(240, 196, 120, .9)';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    ctx.restore();
  }
  drawLightPoint(0, 90, '#9ecbff', 'N');
  drawLightPoint(0, -90, '#9ecbff', 'S');
  drawLightPoint(subsolar.lon, subsolar.lat, '#ffd166', '日下点');
  if (light.sub_earth) {
    drawLightPoint(light.sub_earth.lon, light.sub_earth.lat, '#7ae0d0', '天平动点');
  }
}

async function loadLighting() {
  const params = new URLSearchParams();
  const when = $('light-when').value;
  if (when) params.set('when', when.length === 16 ? `${when}:00Z` : `${when}Z`);
  params.set('lat', $('light-lat').value || '0');
  params.set('lon', $('light-lon').value || '0');
  const response = await fetch('/api/map/lighting?' + params.toString());
  if (!response.ok) throw new Error(await response.text());
  state.lighting = await response.json();
  state.lightingOn = $('light-on').checked;
  state.shadowOn = $('shadow-on').checked;
  const subsolar = state.lighting.subsolar;
  state.lightingKey = state.lightingOn
    ? `${subsolar.lon.toFixed(3)},${subsolar.lat.toFixed(3)}` : '';
  state.tiles.clear();
  const readout = $('light-readout');
  if (readout) {
    const phase = state.lighting.phase || {};
    const phaseName = state.language === 'en'
      ? (phase.name_en || phase.name_zh || '')
      : (phase.name_zh || phase.name_en || '');
    readout.textContent = `${phaseName} ${Number(phase.illumination_percent || 0).toFixed(1)}%`
      + ` · 日下点 ${Number(subsolar.lon).toFixed(1)}° / ${Number(subsolar.lat).toFixed(1)}°`;
  }
  render();
}

function ensureProjectionSource() {
  const key = state.lightingKey || 'plain';
  if (state.sourceCanvas && state.sourceKey === key) return;
  if (state.sourceLoadingKey === key) return;
  state.sourceLoadingKey = key;
  state.sourceKey = key;
  state.sourceCanvas = null;
  state.sourcePixels = null;
  const canvas = document.createElement('canvas');
  canvas.width = 2048;
  canvas.height = 1024;
  const source = canvas.getContext('2d');
  source.fillStyle = '#05070b';
  source.fillRect(0, 0, canvas.width, canvas.height);
  const light = state.lightingKey
    ? `?light=${encodeURIComponent(state.lightingKey)}` : '';
  let done = 0;
  const total = 8 * 4;
  const finish = () => {
    done += 1;
    if (done < total) return;
    if (state.sourceKey !== key) return;
    state.sourceLoadingKey = null;
    state.sourceCanvas = canvas;
    try {
      state.sourcePixels = source.getImageData(0, 0, canvas.width, canvas.height);
    } catch (error) {
      state.sourcePixels = null;
    }
    render();
  };
  for (let y = 0; y < 4; y += 1) {
    for (let x = 0; x < 8; x += 1) {
      const image = new Image();
      image.onload = () => {
        source.drawImage(image, x * TILE, y * TILE);
        finish();
      };
      image.onerror = finish;
      image.src = `/api/map/tile/2/${x}/${y}.png${light}`;
    }
  }
}

function drawProjectedBase() {
  ensureProjectionSource();
  if (!state.sourceCanvas || !state.sourcePixels) return;
  const width = Math.max(1, Math.round(state.width));
  const height = Math.max(1, Math.round(state.height));
  const source = state.sourcePixels.data;
  const sourceWidth = state.sourcePixels.width;
  const sourceHeight = state.sourcePixels.height;
  const image = ctx.createImageData(width, height);
  const out = image.data;
  const background = [5, 7, 11, 255];
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const geo = screenToLonLat(x + 0.5, y + 0.5);
      const offset = (y * width + x) * 4;
      if (!geo) {
        out[offset] = background[0];
        out[offset + 1] = background[1];
        out[offset + 2] = background[2];
        out[offset + 3] = 255;
        continue;
      }
      const u = Math.max(0, Math.min(1, (geo.lon + 180) / 360));
      const v = Math.max(0, Math.min(1, (90 - geo.lat) / 180));
      const sx = Math.min(sourceWidth - 1, Math.floor(u * sourceWidth));
      const sy = Math.min(sourceHeight - 1, Math.floor(v * sourceHeight));
      const sourceOffset = (sy * sourceWidth + sx) * 4;
      out[offset] = source[sourceOffset];
      out[offset + 1] = source[sourceOffset + 1];
      out[offset + 2] = source[sourceOffset + 2];
      out[offset + 3] = 255;
    }
  }
  const off = document.createElement('canvas');
  off.width = width;
  off.height = height;
  off.getContext('2d').putImageData(image, 0, 0);
  ctx.drawImage(off, 0, 0);
  if (state.projection === 'globe') {
    ctx.save();
    ctx.strokeStyle = 'rgba(160, 190, 235, .28)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(state.width / 2, state.height / 2, globeRadius(), 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

function drawGraticule() {
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
    let started = false;
    ctx.beginPath();
    for (let lat = -90; lat <= 90; lat += 5) {
      const p = lonlatToScreen(lon, lat);
      if (p.visible === false) { started = false; continue; }
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
    const label = lonlatToScreen(lon, 0);
    if (label.visible !== false && label.x > -20 && label.x < state.width + 20) {
      ctx.fillText(`${Math.abs(lon)}°${lon < 0 ? 'W' : lon > 0 ? 'E' : ''}`,
        label.x + 3, Math.max(4, label.y + 2));
    }
  }
  for (let lat = -80; lat <= 80; lat += step) {
    let started = false;
    ctx.beginPath();
    for (let lon = -180; lon <= 180; lon += 5) {
      const p = lonlatToScreen(lon, lat);
      if (p.visible === false) { started = false; continue; }
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
    const label = lonlatToScreen(-178, lat);
    if (label.visible !== false && label.y > -20 && label.y < state.height + 20) {
      ctx.fillText(`${Math.abs(lat)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`,
        5, label.y + 2);
    }
  }
  ctx.restore();
}

/* ── features ───────────────────────────────────────────────────────────── */

function activeFeatureClasses() {
  const known = new Set((state.layers && state.layers.feature_classes) || []);
  return [...state.active].filter((key) => known.has(key));
}

/** A deterministic per-class density gate, so a class does not flicker. */
function densityPasses(feature) {
  const density = state.density[feature.class] ?? 1;
  if (density >= 1) return true;
  if (density <= 0) return false;
  const hash = ((Number(feature.id) + 1) * 2654435761) >>> 0;
  return (hash % 1000) < density * 1000;
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
  let west;
  let east;
  let north;
  let south;
  if (state.projection === 'globe') {
    // A corner of the canvas is outside the sphere, so there is no single
    // lon/lat rectangle to ask for; the whole globe is the safe viewport.
    west = -180;
    east = 180;
    south = -90;
    north = 90;
  } else {
    const westPoint = screenToLonLat(0, 0);
    const eastPoint = screenToLonLat(state.width, state.height);
    west = westPoint ? westPoint.lon : -180;
    east = eastPoint ? eastPoint.lon : 180;
    north = westPoint ? westPoint.lat : 90;
    south = eastPoint ? eastPoint.lat : -90;
  }
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
  let radius;
  if (state.projection === 'globe') {
    radius = Math.max(2.2, Math.min(48,
      (diameter / 3474.8) * globeRadius() * 2));
  } else {
    const apparent = kmPerPixel > 0 ? diameter / kmPerPixel : 0;
    radius = Math.max(2.2, Math.min(48, apparent / 2));
  }
  return {
    screen,
    radius,
    apparent: radius,
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
    if (!densityPasses(feature)) continue;
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
  if (feature.class === 'statio' || feature.class === 'impact'
      || ((feature.class === 'lunar100' || feature.class === 'libration')
          && !state.diameters)) {
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
  const text = featureName(feature);
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
    if (!densityPasses(feature)) continue;
    const pixels = featurePixels(feature);
    if (pixels.screen.visible === false) continue;
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
    title.textContent = featureName(feature);
    const cls = document.createElement('div');
    cls.className = 'class';
    const layer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === feature.class);
    cls.textContent = layer ? layerLabel(layer) : feature.class;
    const facts = document.createElement('dl');
    const nameFacts = feature.class === 'lunar100'
      ? [['英文', feature.name_en || '—']]
      : [['中文', feature.name_zh || '—'], ['英文', feature.name_en || '—']];
    for (const [key, value] of [
      ...nameFacts,
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
  if (state.projection === 'globe') {
    state.globe.lon = feature.lon;
    state.globe.lat = feature.lat;
    state.globe.zoom = Math.max(1.4, state.globe.zoom);
  }
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
    text.textContent = layerLabel(layer);
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
    if (layer.type === 'features') {
      if (!(layer.id in state.density)) state.density[layer.id] = 1;
      const density = document.createElement('input');
      density.type = 'range';
      density.className = 'layer-density';
      density.min = '0';
      density.max = '1';
      density.step = '0.05';
      density.value = String(state.density[layer.id]);
      density.title = `${layerLabel(layer)} 密度`;
      density.setAttribute('aria-label', `${layerLabel(layer)} 密度`);
      density.addEventListener('click', (event) => event.stopPropagation());
      density.addEventListener('pointerdown', (event) => event.stopPropagation());
      density.addEventListener('input', () => {
        state.density[layer.id] = Number(density.value);
        render();
      });
      label.appendChild(density);
    }
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

function refreshLayerLanguage() {
  for (const input of document.querySelectorAll('#layer-list input[data-layer]')) {
    const layer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === input.dataset.layer);
    if (!layer) continue;
    const item = input.closest('.layer-item');
    if (!item) continue;
    const text = item.querySelector('.layer-label');
    if (text) text.textContent = layerLabel(layer);
    const density = item.querySelector('.layer-density');
    if (density) {
      const title = `${layerLabel(layer)} 密度`;
      density.title = title;
      density.setAttribute('aria-label', title);
    }
  }
  if (state.selected) selectFeature(state.selected);
  else render();
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
    left.textContent = featureName(row);
    const right = document.createElement('small');
    const rowLayer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === row.class);
    right.textContent = `${rowLayer ? layerLabel(rowLayer) : row.class} · `
      + formatCoord(row.lon, row.lat);
    item.appendChild(left);
    item.appendChild(right);
    const choose = () => {
      box.hidden = true;
      $('search-input').value = featureName(row);
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
  const dragGeo = screenToLonLat(point.x, point.y);
  state.drag = {
    x: event.clientX, y: event.clientY,
    lon: state.centerLon, lat: state.centerLat,
    geo: dragGeo,
    globeLon: state.globe.lon,
    globeLat: state.globe.lat,
  };
  stage.classList.add('dragging');
  stage.setPointerCapture(event.pointerId);
  event.preventDefault();
});

stage.addEventListener('pointermove', (event) => {
  const point = stagePoint(event);
  const geo = screenToLonLat(point.x, point.y);
  if (geo) updatePositionReadout(geo.lon, geo.lat, point);
  if (state.drag) {
    const dx = event.clientX - state.drag.x;
    const dy = event.clientY - state.drag.y;
    if (state.projection === 'globe') {
      const radius = Math.max(1, globeRadius());
      state.globe.lon = ((state.drag.globeLon - dx / radius * 90 + 180) % 360) - 180;
      state.globe.lat = clamp(state.drag.globeLat + dy / radius * 90, -89, 89);
    } else if (state.projection === 'mercator') {
      if (geo && state.drag.geo) {
        state.centerLon = clamp(
          state.drag.lon - (geo.lon - state.drag.geo.lon), -180, 180);
        state.centerLat = clamp(
          state.drag.lat - (geo.lat - state.drag.geo.lat), -85, 85);
      }
    } else {
      const world = worldSize();
      state.centerLon = state.drag.lon - dx / state.scale / world.w * 360;
      state.centerLat = state.drag.lat + dy / state.scale / world.h * 180;
      clampCenter();
    }
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
  title.textContent = featureName(feature);
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
  $('name-language').addEventListener('change', (event) => {
    state.language = event.target.value === 'en' ? 'en' : 'zh';
    refreshLayerLanguage();
  });
  $('projection').addEventListener('change', (event) => {
    state.projection = ['mercator', 'globe'].includes(event.target.value)
      ? event.target.value : 'equirect';
    state.tiles.clear();
    render();
    scheduleFeatures();
  });
  $('light-apply').addEventListener('click', () => {
    loadLighting().catch((error) => {
      const readout = $('light-readout');
      if (readout) readout.textContent = String(error.message || error);
    });
  });
  $('light-now').addEventListener('click', () => {
    $('light-when').value = utcNowLocalInput();
    $('light-apply').click();
  });
  $('light-on').addEventListener('change', () => {
    if ($('light-on').checked) {
      loadLighting().catch(() => {});
    } else {
      state.lightingOn = false;
      state.lightingKey = '';
      state.tiles.clear();
      render();
    }
  });
  $('shadow-on').addEventListener('change', () => {
    state.shadowOn = $('shadow-on').checked;
    render();
  });
  $('light-when').value = utcNowLocalInput();
  loadLighting().catch(() => {});
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
