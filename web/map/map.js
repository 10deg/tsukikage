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
//: Bumped with shadow/render changes so a browser does not keep yesterday's
//: tile after a restart.
const TILE_VERSION = '182';
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
const LABEL_TIER0 = new Set(['basin', 'lunar100', 'libration']);
const LABEL_TIER1 = new Set(['mare']);
const LABEL_TIER2 = new Set(['lacus', 'sinus', 'palus', 'rima', 'vallis',
  'mons', 'dorsum', 'rupes', 'catena', 'promontorium', 'albedo']);
const LARGE_CRATER_KM = 100;
const LARGE_SATELLITE_KM = 30;

//: 真实直径 circles are meaningful for these classes only.  A landing site is
//: a crosshair, a dorsum or rima is a line, and a small dome is a point: drawing
//: a true-diameter circle for those reads as a measured rim that is not there.
const RIM_CLASSES = new Set(['crater', 'basin', 'mare', 'lacus', 'sinus']);
//: Region names sit across their formation, like the full-disk page, instead of
//: hanging off a marker.
const REGION_LABEL_CLASSES = new Set(['basin', 'mare', 'lacus', 'sinus']);

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
  marksOn: true,
  diameters: true,
  labelFont: 0,
  labelColour: true,
  textColor: '#eef2ff',
  textStroke: '#030509',
  textStrokeWidth: 1.2,
  markerStroke: '#030509',
  markerStrokeWidth: 0.6,
  textBold: false,
  outlineColor: '#b4d6ff',
  outlineWidth: 1,
  outlineStyle: 'solid',
  avoidOverlap: true,
  brightness: 1,
  earthshine: true,
  shadowBias: 0,
  zoom: 0,
  scale: 1,
  centerLon: 0,
  centerLat: 0,
  mercY: 0,
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
  lightingKey: '',
  marks: { terminator: true, poles: true, subsolar: true, libration: true,
           equator: true, graticule: true },
  globe: { lon: 0, lat: 0, zoom: 1 },
  sourceCanvas: null,
  sourcePixels: null,
  sourceKey: '',
  sourceReadyKey: '',
  sourceLoadingKey: '',
  sourceBounds: null,
  projectedBase: null,
  sourceZoom: 0,
  photos: [],
  photoOrder: [],
  photoFilters: { dateStart: '', dateEnd: '', ageMin: null, ageMax: null,
                  features: [], query: '' },
  photoDragId: '',
  photoMask: null,
  labelHits: [],
  manualMarks: [],
  manualSeq: 0,
  manualDrag: null,
  manualLayout: [],
  photo: null,
  photoActive: new Set(),
  photoOverlays: new Map(),
  photoGLTextures: new Map(),
  photoVisible: true,
  photoDirectory: '',
  photoOpacity: 0.85,
  drag: null,
  pointer: null,
  pointSeq: 0,
  pointTimer: null,
  hashTimer: null,
  fitted: false,
  fullscreen: false,
};

/* ── view maths ─────────────────────────────────────────────────────────── */

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

/** Longitude on [-180, 180): the map has no left or right edge. */
function wrapLon(lon) {
  const value = ((Number(lon) + 180) % 360 + 360) % 360 - 180;
  return value === -180 ? -180 : value;
}

/** Latitude on [-limit, limit): crossing a pole re-enters from the other one. */
function wrapLat(lat, limit = 90) {
  const period = limit * 2;
  return ((Number(lat) + limit) % period + period) % period - limit;
}

function mercatorYToLat(y) {
  return (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
}

function mercatorPeriod() {
  return 2 * mercatorY(85);
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
    const dm = mercatorY(lat) - state.mercY;
    let dLon = lon - state.centerLon;
    dLon = ((dLon + 180) % 360 + 360) % 360 - 180;
    return {
      x: state.width / 2 + dLon * pxPerDeg,
      y: state.height / 2 - dm * pxPerRad,
      visible: true,
    };
  }
  const world = worldSize();
  const cam = camera();
  const point = lonlatToWorld(lon, lat);
  let dx = point.x - cam.x;
  dx = ((dx + world.w / 2) % world.w + world.w) % world.w - world.w / 2;
  const dy = point.y - cam.y;
  return {
    x: dx * state.scale + state.width / 2,
    y: dy * state.scale + state.height / 2,
    visible: true,
  };
}

/** Project a longitude without wrapping it back into [-180, 180].
 *
 * The true-diameter rim of a feature that straddles the dateline belongs on
 * one side of the picture, not as two half-rings at both edges.  The raw
 * projection lets that one ring continue past the canvas edge, where the
 * canvas clips it naturally.
 */
function lonlatToScreenRaw(lon, lat) {
  if (state.projection === 'mercator') {
    const world = worldSize();
    const pxPerDeg = (world.w / 360) * state.scale;
    const pxPerRad = pxPerDeg * 180 / Math.PI;
    const dm = mercatorY(lat) - state.mercY;
    return {
      x: state.width / 2 + (lon - state.centerLon) * pxPerDeg,
      y: state.height / 2 - dm * pxPerRad,
      visible: true,
    };
  }
  const world = worldSize();
  const cam = camera();
  const point = lonlatToWorld(lon, lat);
  return {
    x: (point.x - cam.x) * state.scale + state.width / 2,
    y: (point.y - cam.y) * state.scale + state.height / 2,
    visible: true,
  };
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
    const lon = wrapLon(state.centerLon + (sx - state.width / 2) / pxPerDeg);
    const m = state.mercY - (sy - state.height / 2) / pxPerRad;
    const period = mercatorPeriod();
    if (m < -period / 2 || m > period / 2) return null;
    return { lon, lat: clamp(mercatorYToLat(m), -85, 85) };
  }
  const world = worldSize();
  const cam = camera();
  const x = cam.x + (sx - state.width / 2) / state.scale;
  const y = cam.y + (sy - state.height / 2) / state.scale;
  return {
    lon: wrapLon(x / world.w * 360 - 180),
    lat: clamp(90 - y / world.h * 180, -90, 90),
  };
}

/** Keep the camera on the repeating world.

 * The map is a 2:1 rectangle that now repeats in both directions: dragging past
 * a seam continues into the next copy instead of hitting a hard edge.  The
 * centre is stored wrapped so every projection, feature query and hash keeps
 * talking about a canonical coordinate, while the drawing code chooses the
 * copy nearest the camera.
 */
function clampCenter() {
  state.centerLon = wrapLon(state.centerLon);
  if (state.projection === 'globe') {
    state.centerLat = clamp(Number(state.centerLat) || 0, -89, 89);
    return;
  }
  const world = worldSize();
  const halfH = state.height / (2 * state.scale);
  if (state.projection === 'mercator') {
    // Vertical panning stops at the Mercator edge; only longitude repeats.
    const pxPerRad = (world.w / 360) * state.scale * 180 / Math.PI;
    const period = mercatorPeriod();
    const half = halfH / Math.max(pxPerRad, 1e-9);
    let m = mercatorY(clamp(Number(state.centerLat) || 0, -85, 85));
    if (period <= 2 * half) m = 0;
    else m = clamp(m, -period / 2 + half, period / 2 - half);
    state.mercY = m;
    state.centerLat = mercatorYToLat(m);
    return;
  }
  const rows = world.rows;
  let cy = (90 - (Number(state.centerLat) || 0)) / 180 * world.h;
  if (world.h <= 2 * halfH) cy = world.h / 2;
  else cy = clamp(cy, halfH, world.h - halfH);
  state.centerLat = 90 - cy / world.h * 180;
  state.mercY = 0;
}

function fitView() {
  if (state.projection === 'globe') {
    state.globe = { lon: 0, lat: 0, zoom: 1 };
    state.centerLon = 0;
    state.centerLat = 0;
    state.mercY = 0;
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
  state.mercY = 0;
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

/** Put the projection point under the zoom pivot back under it.

 * The pivot is a screen position; after the scale changes the centre has to
 * move by the same projected offset.  Mercator cannot reuse the equirect
 * formula: its vertical coordinate is a logarithm, so the offset is measured in
 * mercator units, not degrees.
 */
function recenterAfterZoom(geo, pivot) {
  if (!geo) return;
  const world = worldSize();
  if (state.projection === 'mercator') {
    const pxPerDeg = (world.w / 360) * state.scale;
    const pxPerRad = pxPerDeg * 180 / Math.PI;
    state.centerLon = wrapLon(geo.lon - (pivot.x - state.width / 2) / pxPerDeg);
    const m = state.mercY + (pivot.y - state.height / 2) / pxPerRad;
    state.centerLat = clamp(mercatorYToLat(m), -85, 85);
    state.mercY = mercatorY(state.centerLat);
    return;
  }
  const point = lonlatToWorld(geo.lon, geo.lat);
  state.centerLon = wrapLon(((point.x - (pivot.x - state.width / 2) / state.scale)
    / world.w) * 360 - 180);
  state.centerLat = clamp(90 - ((point.y - (pivot.y - state.height / 2)
    / state.scale) / world.h) * 180, -90, 90);
}

function zoomTo(nextZoom, anchor) {
  if (state.projection === 'globe') {
    state.globe.zoom = clamp(state.globe.zoom * 2 ** (nextZoom - state.zoom), 0.45, 16.0);
    state.zoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
    render();
    scheduleFeatures();
    return;
  }
  const pivot = anchor || { x: state.width / 2, y: state.height / 2 };
  const geo = screenToLonLat(pivot.x, pivot.y);
  state.zoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
  recenterAfterZoom(geo, pivot);
  clampCenter();
  render();
  scheduleFeatures();
  scheduleHash();
}

function zoomBy(factor, anchor) {
  if (state.projection === 'globe') {
    state.globe.zoom = clamp(state.globe.zoom * factor, 0.45, 16.0);
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
  state.scale = clamp(nextScale, 0.18, 16);
  recenterAfterZoom(geo, pivot);
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

/* ── personal photo library ───────────────────────────────────────────────
 *
 * A MoonLabeler v2 project contains the camera geometry and the 2×3 matrix
 * that maps the projection plane to the original photo.  The map can therefore
 * inverse-project each screen pixel onto the Moon and forward-project it into
 * the photo, without running recognition or importing MoonLabeler itself.
 */
function dot3(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function moonToPhoto(project, latDeg, lonDeg, heightKm = 0) {
  if (!project || !project.geometry || !project.matrix) return null;
  const geometry = project.geometry;
  const matrix = project.matrix;
  if (!Array.isArray(matrix) || matrix.length !== 2) return null;
  const lat = latDeg * Math.PI / 180;
  const lon = lonDeg * Math.PI / 180;
  const radius = 1 + (Number(heightKm) || 0) / 1737.4;
  const v = [
    radius * Math.cos(lat) * Math.cos(lon),
    radius * Math.cos(lat) * Math.sin(lon),
    radius * Math.sin(lat),
  ];
  const q = 1737.4 / Number(geometry.distance_km);
  if (!(q > 0) || !Number.isFinite(q)) return null;
  const depth = dot3(v, geometry.observer);
  if (!(depth > q * dot3(v, v))) return null;
  const denom = 1 - q * depth;
  if (!(denom > 1e-9)) return null;
  const u = dot3(v, geometry.right) / denom;
  const w = -dot3(v, geometry.north) / denom;
  const [a, b, tx] = matrix[0];
  const [c, d, ty] = matrix[1];
  let x = a * u + b * w + tx;
  let y = c * u + d * w + ty;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const recognition = project.automatic_recognition || {};
  const lens = recognition.lens_distortion;
  if (lens) {
    const scale = Number(lens.scale);
    const k = Number(lens.k || 0);
    if (scale > 0 && k) {
      const dx = (x - Number(lens.center[0])) / scale;
      const dy = (y - Number(lens.center[1])) / scale;
      const factor = k * (dx * dx + dy * dy) * scale;
      x += factor * dx;
      y += factor * dy;
    }
  }
  let width = 0;
  let height = 0;
  if (Array.isArray(project.image_size) && project.image_size.length === 2) {
    width = Number(project.image_size[0]);
    height = Number(project.image_size[1]);
  } else if (state.photo && Array.isArray(state.photo.image_size)) {
    width = Number(state.photo.image_size[0]);
    height = Number(state.photo.image_size[1]);
  }
  if (width > 0 && height > 0) {
    if (!(x >= 0 && x < width && y >= 0 && y < height)) return null;
  }
  return { x, y };
}

function photoToSphere(project, x, y) {
  if (!project || !project.matrix || !project.geometry) return null;
  const matrix = project.matrix;
  const [a, b, tx] = matrix[0];
  const [c, d, ty] = matrix[1];
  const det = a * d - b * c;
  if (!(Math.abs(det) > 1e-10)) return null;
  const u = (d * (x - tx) - b * (y - ty)) / det;
  const w = (-c * (x - tx) + a * (y - ty)) / det;
  const geometry = project.geometry;
  const distance = Number(geometry.distance_km) / 1737.4;
  if (!(distance > 1)) return null;
  const ray = [
    -geometry.observer[0] + (u * geometry.right[0] - w * geometry.north[0]) / distance,
    -geometry.observer[1] + (u * geometry.right[1] - w * geometry.north[1]) / distance,
    -geometry.observer[2] + (u * geometry.right[2] - w * geometry.north[2]) / distance,
  ];
  const discriminant = distance * distance
    - dot3(ray, ray) * (distance * distance - 1);
  if (!(discriminant >= 0)) return null;
  const length = (distance * distance - 1) / (distance + Math.sqrt(discriminant));
  const v = [
    distance * geometry.observer[0] + length * ray[0],
    distance * geometry.observer[1] + length * ray[1],
    distance * geometry.observer[2] + length * ray[2],
  ];
  return {
    lon: Math.atan2(v[1], v[0]) * 180 / Math.PI,
    lat: Math.asin(Math.max(-1, Math.min(1, v[2]))) * 180 / Math.PI,
  };
}

function locatePhoto() {
  if (!state.photo || !state.photo.project) {
    setStatus('还没有选中的照片', 'error');
    return;
  }
  const project = state.photo.project;
  const size = Array.isArray(project.image_size) ? project.image_size : null;
  const point = size
    ? photoToSphere(project, Number(size[0]) / 2, Number(size[1]) / 2)
    : null;
  if (!point) {
    setStatus('照片中心落在月球外', 'error');
    return;
  }
  if (state.projection === 'globe') {
    state.globe.lon = point.lon;
    state.globe.lat = point.lat;
  } else {
    state.centerLon = point.lon;
    state.centerLat = point.lat;
    clampCenter();
  }
  render();
  scheduleFeatures();
  scheduleHash();
}

function samplePhoto(pixels, width, height, x, y) {
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  if (x0 < 0 || y0 < 0 || x0 >= width - 1 || y0 >= height - 1) return null;
  const fx = x - x0;
  const fy = y - y0;
  const base = (y0 * width + x0) * 4;
  const next = base + 4;
  const below = base + width * 4;
  const belowNext = below + 4;
  const data = pixels;
  const out = [0, 0, 0, 0];
  for (let channel = 0; channel < 4; channel += 1) {
    const top = data[base + channel] * (1 - fx) + data[next + channel] * fx;
    const bottom = data[below + channel] * (1 - fx)
      + data[belowNext + channel] * fx;
    out[channel] = top * (1 - fy) + bottom * fy;
  }
  return out;
}

function setupPhotoPixels(image) {
  try {
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const pctx = canvas.getContext('2d');
    pctx.drawImage(image, 0, 0);
    const data = pctx.getImageData(0, 0, image.width, image.height);
    return { width: data.width, height: data.height, data: data.data };
  } catch (error) {
    return null;
  }
}

function ensurePhotoOverlay(entry) {
  const id = entry.id;
  let record = state.photoOverlays.get(id);
  if (record) {
    record.entry = entry;
    if (record.image || record.loading) return record;
  } else {
    record = { entry, meta: null, image: null, pixels: null, loading: true };
    state.photoOverlays.set(id, record);
  }
  record.loading = true;
  fetch('/api/map/photos/' + encodeURIComponent(id) + '/overlay')
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'overlay failed');
      if (state.photoOverlays.get(id) !== record) return;
      record.meta = payload;
      const image = new Image();
      image.onload = () => {
        if (state.photoOverlays.get(id) !== record) return;
        record.image = image;
        record.loading = false;
        render();
      };
      image.onerror = () => {
        record.loading = false;
        setStatus('照片贴图载入失败：' + (entry.name || id));
      };
      image.src = payload.url;
    })
    .catch((error) => {
      record.loading = false;
      setStatus('照片贴图生成失败：' + String(error.message || error));
    });
  return record;
}

function overlayPixels(record) {
  if (!record.image) return null;
  if (!record.pixels) record.pixels = setupPhotoPixels(record.image);
  return record.pixels;
}

function syncPhotoOrder() {
  const available = new Set(state.photos.map((entry) => entry.id));
  const kept = state.photoOrder.filter((id) => available.has(id));
  const seen = new Set(kept);
  const fresh = state.photos
    .map((entry) => entry.id)
    .filter((id) => !seen.has(id));
  state.photoOrder = [...fresh, ...kept];
}

function orderedPhotos() {
  syncPhotoOrder();
  return state.photoOrder
    .map((id) => state.photos.find((entry) => entry.id === id))
    .filter(Boolean);
}

function orderedActivePhotos() {
  return orderedPhotos().filter((entry) => state.photoActive.has(entry.id));
}

function activeOverlayRecords() {
  const records = [];
  for (const entry of orderedActivePhotos()) {
    const record = state.photoOverlays.get(entry.id);
    if (record && record.meta && record.image) records.push(record);
  }
  return records;
}

function movePhoto(entry, delta) {
  const index = state.photoOrder.indexOf(entry.id);
  if (index < 0) return;
  const next = Math.max(0, Math.min(state.photoOrder.length - 1, index + delta));
  if (next === index) return;
  const [id] = state.photoOrder.splice(index, 1);
  state.photoOrder.splice(next, 0, id);
  rebuildPhotoList();
  render();
}

function drawPhotoTextures() {
  const world = worldSize();
  const cam = camera();
  for (const record of activeOverlayRecords().reverse()) {
    const bounds = record.meta.bounds;
    if (!bounds) continue;
    // Photo bounds are a rectangle in the world, not two independently
    // wrapped points: a whole-disk photo starts at -180 and ends at +180,
    // which project to the same screen x if each corner is wrapped alone.  Use
    // the unwrapped span and choose the copy nearest the camera.
    const x0 = (bounds.west + 180) / 360 * world.w;
    const x1 = (bounds.east + 180) / 360 * world.w;
    const y0 = (90 - bounds.north) / 180 * world.h;
    const y1 = (90 - bounds.south) / 180 * world.h;
    const spanX = x1 - x0;
    const spanY = y1 - y0;
    if (!(spanX > 0 && spanY > 0)) continue;
    let dx = (x0 + x1) / 2 - cam.x;
    dx = ((dx + world.w / 2) % world.w + world.w) % world.w - world.w / 2;
    const dy = (y0 + y1) / 2 - cam.y;
    const width = spanX * state.scale;
    const height = spanY * state.scale;
    const left = state.width / 2 + dx * state.scale - width / 2;
    const top = state.height / 2 + dy * state.scale - height / 2;
    ctx.save();
    ctx.globalAlpha = state.photoOpacity;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(record.image, left, top, width, height);
    ctx.restore();
  }
}

function drawProjectedPhotoTextures() {
  const records = activeOverlayRecords().reverse();
  if (!records.length) return;
  const loaded = [];
  for (const record of records) {
    const pixels = overlayPixels(record);
    if (pixels) loaded.push({ record, pixels });
  }
  if (!loaded.length) return;
  const width = Math.max(1, Math.round(state.width));
  const height = Math.max(1, Math.round(state.height));
  const image = ctx.createImageData(width, height);
  const out = image.data;
  let written = 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const geo = screenToLonLat(x + 0.5, y + 0.5);
      if (!geo) continue;
      for (const { record, pixels } of loaded) {
        const bounds = record.meta.bounds;
        const u = (geo.lon - bounds.west) / Math.max(1e-9, bounds.east - bounds.west);
        const v = (bounds.north - geo.lat) / Math.max(1e-9, bounds.north - bounds.south);
        if (u < 0 || u > 1 || v < 0 || v > 1) continue;
        const sx = Math.min(pixels.width - 1, Math.floor(u * pixels.width));
        const sy = Math.min(pixels.height - 1, Math.floor(v * pixels.height));
        const sourceOffset = (sy * pixels.width + sx) * 4;
        if (pixels.data[sourceOffset + 3] <= 0) continue;
        const offset = (y * width + x) * 4;
        out[offset] = pixels.data[sourceOffset];
        out[offset + 1] = pixels.data[sourceOffset + 1];
        out[offset + 2] = pixels.data[sourceOffset + 2];
        out[offset + 3] = pixels.data[sourceOffset + 3];
        written += 1;
        break;
      }
    }
  }
  if (!written) return;
  const off = document.createElement('canvas');
  off.width = width;
  off.height = height;
  off.getContext('2d').putImageData(image, 0, 0);
  ctx.save();
  ctx.globalAlpha = state.photoOpacity;
  ctx.drawImage(off, 0, 0);
  ctx.restore();
}

/* ── GPU photo overlay ───────────────────────────────────────────────────
 *
 * The projected PNG cache is a fallback.  When WebGL is available the original
 * photo is uploaded once and a fragment shader walks the same MoonLabeler
 * geometry for every screen pixel.  The detail therefore comes from the full
 * photo texture at the screen's own resolution -- no 256 px tile, no fixed
 * px/degree cache -- while the CPU only sets uniforms and issues one quad.
 */
let photoGL = null;
let photoGLFailed = false;

const PHOTO_VERTEX = `
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
`;

const PHOTO_FRAGMENT = `
precision highp float;
uniform vec2 u_resolution;
uniform vec2 u_view;
uniform float u_dpr;
uniform int u_projection;
uniform vec2 u_center;
uniform float u_px_per_deg;
uniform float u_radius;
uniform vec3 u_globe_up;
uniform vec3 u_globe_east;
uniform vec3 u_globe_north;
uniform vec3 u_observer;
uniform vec3 u_right;
uniform vec3 u_north;
uniform float u_distance;
uniform vec3 u_matrix0;
uniform vec3 u_matrix1;
uniform vec2 u_image_size;
uniform vec4 u_lens;
uniform int u_has_lens;
uniform sampler2D u_photo;
uniform sampler2D u_mask;
uniform int u_has_mask;
uniform float u_opacity;

const float PI = 3.141592653589793;

float mercator_y(float lat_deg) {
  float lat = clamp(lat_deg, -85.0, 85.0) * PI / 180.0;
  return log(tan(PI * 0.25 + lat * 0.5));
}

void main() {
  vec2 p = vec2(gl_FragCoord.x, u_resolution.y - gl_FragCoord.y) / u_dpr;
  float lon;
  float lat;
  if (u_projection == 0) {
    lon = mod(u_center.x + (p.x - u_view.x * 0.5) / u_px_per_deg + 180.0,
              360.0) - 180.0;
    lat = clamp(u_center.y - (p.y - u_view.y * 0.5) / u_px_per_deg,
                -90.0, 90.0);
  } else if (u_projection == 1) {
    float px_per_rad = u_px_per_deg * 180.0 / PI;
    float m = mercator_y(u_center.y)
      - (p.y - u_view.y * 0.5) / px_per_rad;
    lon = mod(u_center.x + (p.x - u_view.x * 0.5) / u_px_per_deg + 180.0,
              360.0) - 180.0;
    lat = clamp((2.0 * atan(exp(m)) - PI * 0.5) * 180.0 / PI,
                -85.0, 85.0);
  } else {
    vec2 d = vec2(p.x - u_view.x * 0.5, u_view.y * 0.5 - p.y) / u_radius;
    if (dot(d, d) > 1.0) discard;
    float nz = sqrt(max(0.0, 1.0 - dot(d, d)));
    vec3 v = u_globe_up * nz + u_globe_east * d.x + u_globe_north * d.y;
    lon = atan(v.y, v.x) * 180.0 / PI;
    lat = asin(clamp(v.z, -1.0, 1.0)) * 180.0 / PI;
  }

  float lat_r = radians(lat);
  float lon_r = radians(lon);
  vec3 v = vec3(cos(lat_r) * cos(lon_r),
                cos(lat_r) * sin(lon_r),
                sin(lat_r));
  float q = 1737.4 / u_distance;
  float depth = dot(v, u_observer);
  if (depth <= q * dot(v, v)) discard;
  float denom = 1.0 - q * depth;
  if (denom <= 1e-9) discard;
  float u = dot(v, u_right) / denom;
  float w = -dot(v, u_north) / denom;
  vec2 px = vec2(dot(u_matrix0, vec3(u, w, 1.0)),
                 dot(u_matrix1, vec3(u, w, 1.0)));
  if (u_has_lens == 1) {
    vec2 d = (px - u_lens.xy) / u_lens.z;
    float factor = u_lens.w * dot(d, d) * u_lens.z;
    px += factor * d;
  }
  if (px.x < 0.0 || px.y < 0.0
      || px.x >= u_image_size.x || px.y >= u_image_size.y) discard;
  vec2 uv = vec2(px.x / u_image_size.x, 1.0 - px.y / u_image_size.y);
  if (u_has_mask == 1 && texture2D(u_mask, uv).r < 0.5) discard;
  vec4 tex = texture2D(u_photo, uv);
  if (tex.a <= 0.0) discard;
  gl_FragColor = vec4(tex.rgb, tex.a * u_opacity);
}
`;

function compilePhotoShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function initPhotoGL() {
  if (photoGL) return photoGL;
  if (photoGLFailed) return null;
  const canvas = document.createElement('canvas');
  let gl = null;
  try {
    gl = canvas.getContext('webgl', {
      alpha: true, premultipliedAlpha: false, antialias: false,
    }) || canvas.getContext('experimental-webgl');
  } catch (error) {
    gl = null;
  }
  if (!gl || typeof gl.createShader !== 'function') {
    photoGLFailed = true;
    return null;
  }
  const vertex = compilePhotoShader(gl, gl.VERTEX_SHADER, PHOTO_VERTEX);
  const fragment = compilePhotoShader(gl, gl.FRAGMENT_SHADER, PHOTO_FRAGMENT);
  if (!vertex || !fragment) {
    photoGLFailed = true;
    return null;
  }
  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    photoGLFailed = true;
    return null;
  }
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER,
                new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
                gl.STATIC_DRAW);
  const attrib = gl.getAttribLocation(program, 'a_pos');
  const names = [
    'u_resolution', 'u_view', 'u_dpr', 'u_projection', 'u_center',
    'u_px_per_deg', 'u_radius', 'u_globe_up', 'u_globe_east',
    'u_globe_north', 'u_observer', 'u_right', 'u_north', 'u_distance',
    'u_matrix0', 'u_matrix1', 'u_image_size', 'u_lens', 'u_has_lens',
    'u_photo', 'u_mask', 'u_has_mask', 'u_opacity',
  ];
  const uniforms = {};
  for (const name of names) uniforms[name] = gl.getUniformLocation(program, name);
  photoGL = { canvas, gl, program, buffer, attrib, uniforms };
  return photoGL;
}

function uploadPhotoTexture(record) {
  const gl = photoGL && photoGL.gl;
  if (!gl || !record.image) return;
  let source = record.image;
  const max = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 4096;
  if (source.width > max || source.height > max) {
    const scale = Math.min(max / source.width, max / source.height);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(source.width * scale));
    canvas.height = Math.max(1, Math.round(source.height * scale));
    canvas.getContext('2d').drawImage(source, 0, 0,
                                      canvas.width, canvas.height);
    source = canvas;
  }
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  record.texture = texture;
}

function uploadPhotoMaskTexture(record) {
  const gl = photoGL && photoGL.gl;
  if (!gl) return;
  if (!record.maskImage) {
    record.maskTexture = null;
    return;
  }
  let source = record.maskImage;
  const max = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 4096;
  if (source.width > max || source.height > max) {
    const scale = Math.min(max / source.width, max / source.height);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(source.width * scale));
    canvas.height = Math.max(1, Math.round(source.height * scale));
    canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
    source = canvas;
  }
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  record.maskTexture = texture;
}

function ensurePhotoGL(entry) {
  if (!initPhotoGL()) return false;
  const project = entry.project || {};
  const refinement = (project.automatic_recognition || {}).refinement || {};
  const surface = project.projection_surface
    || (refinement.relief_aware === true ? 'terrain' : 'sphere');
  if (surface === 'terrain') return false;
  const id = entry.id;
  const maskUrl = entry.mask_url || null;
  let record = state.photoGLTextures.get(id);
  if (record && record.maskUrl === maskUrl
      && (record.texture || record.loading)) {
    record.entry = entry;
    return true;
  }
  if (!record || record.maskUrl !== maskUrl) {
    record = { entry, image: null, texture: null, maskImage: null,
               maskTexture: null, maskUrl, loading: true };
    state.photoGLTextures.set(id, record);
  } else {
    record.loading = true;
  }
  let imageDone = false;
  let maskDone = !maskUrl;
  const maybeFinish = () => {
    if (!imageDone || !maskDone) return;
    if (state.photoGLTextures.get(id) !== record) return;
    uploadPhotoTexture(record);
    uploadPhotoMaskTexture(record);
    record.loading = false;
    render();
  };
  const image = new Image();
  image.onload = () => {
    if (state.photoGLTextures.get(id) !== record) return;
    record.image = image;
    imageDone = true;
    maybeFinish();
  };
  image.onerror = () => {
    record.loading = false;
    setStatus('照片载入失败：' + (entry.name || id));
  };
  image.src = entry.image_url;
  if (maskUrl) {
    const mask = new Image();
    mask.onload = () => {
      if (state.photoGLTextures.get(id) !== record) return;
      record.maskImage = mask;
      maskDone = true;
      maybeFinish();
    };
    mask.onerror = () => {
      maskDone = true;
      maybeFinish();
    };
    mask.src = maskUrl;
  }
  return true;
}

function drawWebGLPhotoOverlay() {
  const gl = photoGL && photoGL.gl;
  if (!gl) return false;
  const records = [];
  for (const entry of orderedActivePhotos().reverse()) {
    const record = state.photoGLTextures.get(entry.id);
    if (record && record.texture) records.push(record);
  }
  if (!records.length) return false;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.round(state.width * dpr));
  const height = Math.max(1, Math.round(state.height * dpr));
  if (photoGL.canvas.width !== width || photoGL.canvas.height !== height) {
    photoGL.canvas.width = width;
    photoGL.canvas.height = height;
  }
  gl.viewport(0, 0, width, height);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.useProgram(photoGL.program);
  gl.bindBuffer(gl.ARRAY_BUFFER, photoGL.buffer);
  gl.enableVertexAttribArray(photoGL.attrib);
  gl.vertexAttribPointer(photoGL.attrib, 2, gl.FLOAT, false, 0, 0);
  gl.enable(gl.BLEND);
  // Alpha must accumulate as over, not as srcA*srcA + dstA*(1-srcA);
  // the latter makes a single 85% photo render at 72%.
  gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA,
                       gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.DEPTH_TEST);
  const u = photoGL.uniforms;
  const world = worldSize();
  const pxPerDeg = (world.w / 360) * state.scale;
  const basis = globeBasis();
  gl.uniform2f(u.u_resolution, width, height);
  gl.uniform2f(u.u_view, state.width, state.height);
  gl.uniform1f(u.u_dpr, dpr);
  gl.uniform1i(u.u_projection, state.projection === 'globe' ? 2
    : state.projection === 'mercator' ? 1 : 0);
  gl.uniform2f(u.u_center, state.centerLon, state.centerLat);
  gl.uniform1f(u.u_px_per_deg, pxPerDeg);
  gl.uniform1f(u.u_radius, globeRadius());
  gl.uniform3fv(u.u_globe_up, basis.up);
  gl.uniform3fv(u.u_globe_east, basis.east);
  gl.uniform3fv(u.u_globe_north, basis.north);
  gl.uniform1f(u.u_opacity, state.photoOpacity);
  for (const record of records) {
    const project = record.entry.project;
    const geometry = project.geometry;
    const matrix = project.matrix;
    const size = project.image_size;
    const lens = (project.automatic_recognition || {}).lens_distortion;
    gl.uniform3fv(u.u_observer, geometry.observer);
    gl.uniform3fv(u.u_right, geometry.right);
    gl.uniform3fv(u.u_north, geometry.north);
    gl.uniform1f(u.u_distance, Number(geometry.distance_km));
    gl.uniform3fv(u.u_matrix0, matrix[0]);
    gl.uniform3fv(u.u_matrix1, matrix[1]);
    gl.uniform2f(u.u_image_size, Number(size[0]), Number(size[1]));
    gl.uniform1i(u.u_has_lens, lens ? 1 : 0);
    if (lens) {
      gl.uniform4f(u.u_lens, Number(lens.center[0]), Number(lens.center[1]),
                   Number(lens.scale), Number(lens.k || 0));
    } else {
      gl.uniform4f(u.u_lens, 0, 0, 1, 0);
    }
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, record.texture);
    gl.uniform1i(u.u_photo, 0);
    gl.uniform1i(u.u_has_mask, record.maskTexture ? 1 : 0);
    if (record.maskTexture) {
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, record.maskTexture);
      gl.uniform1i(u.u_mask, 1);
    }
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
  ctx.save();
  ctx.globalAlpha = 1;
  ctx.drawImage(photoGL.canvas, 0, 0, state.width, state.height);
  ctx.restore();
  return true;
}

function photoFallbackOnly() {
  for (const entry of orderedActivePhotos()) {
    const project = entry.project || {};
    const refinement = (project.automatic_recognition || {}).refinement || {};
    const surface = project.projection_surface
      || (refinement.relief_aware === true ? 'terrain' : 'sphere');
    if (surface === 'terrain') return true;
  }
  return false;
}

function drawPhotoOverlay() {
  if (!state.photoVisible || !state.photoActive.size) return;
  if (photoFallbackOnly()) {
    for (const entry of orderedActivePhotos()) ensurePhotoOverlay(entry);
    if (state.projection === 'equirect') drawPhotoTextures();
    else drawProjectedPhotoTextures();
    return;
  }
  if (initPhotoGL()) {
    if (drawWebGLPhotoOverlay()) return;
  }
  if (state.projection === 'equirect') drawPhotoTextures();
  else drawProjectedPhotoTextures();
}

function photoFeatureNames(entry) {
  const names = [];
  for (const feature of entry.covered_features || []) {
    if (feature.name_en) names.push(feature.name_en);
    if (feature.name_zh) names.push(feature.name_zh);
  }
  return names;
}

function photoFeatureLabel(feature) {
  return state.language === 'en'
    ? (feature.name_en || feature.name_zh || '')
    : (feature.name_zh || feature.name_en || '');
}

function photoHasFeature(entry, name) {
  if (!name) return false;
  const wanted = String(name).toLowerCase();
  return (entry.covered_features || []).some((feature) => (
    String(feature.name_en || '').toLowerCase().includes(wanted)
    || String(feature.name_zh || '').toLowerCase().includes(wanted)
  ));
}

function photoMatchesFilters(entry) {
  const filters = state.photoFilters;
  const date = entry.date || '';
  if (filters.dateStart && (!date || date < filters.dateStart)) return false;
  if (filters.dateEnd && (!date || date > filters.dateEnd)) return false;
  const age = (entry.age_days === null || entry.age_days === undefined)
    ? NaN : Number(entry.age_days);
  if (filters.ageMin !== null || filters.ageMax !== null) {
    if (!Number.isFinite(age)) return false;
    if (filters.ageMin !== null && age < filters.ageMin) return false;
    if (filters.ageMax !== null && age > filters.ageMax) return false;
  }
  if (filters.query) {
    const query = filters.query.toLowerCase();
    if (!photoFeatureNames(entry).some((name) => name.toLowerCase().includes(query))) {
      return false;
    }
  }
  if (filters.features.length) {
    return filters.features.some((name) => photoHasFeature(entry, name));
  }
  return true;
}

function filteredPhotos() {
  syncPhotoOrder();
  return orderedPhotos().filter(photoMatchesFilters);
}

function updatePhotoFeatureOptions() {
  const list = $('photo-feature-options');
  if (!list) return;
  list.innerHTML = '';
  const seen = new Set();
  const names = [];
  for (const entry of state.photos) {
    for (const feature of entry.covered_features || []) {
      const name = feature.name_en || feature.name_zh;
      if (!name || seen.has(name)) continue;
      seen.add(name);
      names.push({ name, label: photoFeatureLabel(feature) });
    }
  }
  names.sort((left, right) => left.label.localeCompare(right.label, 'zh-Hans-CN'));
  for (const item of names) {
    const option = document.createElement('option');
    option.value = item.name;
    option.label = item.label;
    list.appendChild(option);
  }
}

function renderFilterChips() {
  const host = $('photo-filter-chips');
  if (!host) return;
  host.innerHTML = '';
  for (const name of state.photoFilters.features) {
    const chip = document.createElement('span');
    chip.className = 'filter-chip';
    const label = document.createElement('span');
    label.textContent = name;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = '移除这个筛选';
    remove.addEventListener('click', () => {
      state.photoFilters.features = state.photoFilters.features
        .filter((value) => value !== name);
      renderFilterChips();
      rebuildPhotoList();
    });
    chip.appendChild(label);
    chip.appendChild(remove);
    host.appendChild(chip);
  }
}

function addPhotoFeatureFilter(name) {
  const text = String(name || '').trim();
  if (!text) return;
  if (!state.photoFilters.features.includes(text)) {
    state.photoFilters.features.push(text);
  }
  renderFilterChips();
  rebuildPhotoList();
}

function rebuildPhotoList() {
  const list = $('photo-list');
  if (!list) return;
  list.textContent = '';
  updatePhotoFeatureOptions();
  const all = orderedPhotos();
  const entries = all.filter(photoMatchesFilters);
  const count = $('photo-count');
  if (count) {
    count.textContent = state.photos.length
      ? (entries.length !== state.photos.length
        ? `${entries.length}/${state.photos.length}`
        : (state.photoActive.size
          ? `${state.photoActive.size}/${state.photos.length}` : `${state.photos.length}`))
      : '';
  }
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'photo-empty';
    empty.textContent = all.length ? '没有匹配的照片' : '暂无照片';
    list.appendChild(empty);
    return;
  }
  for (const entry of entries) {
    const active = state.photoActive.has(entry.id);
    const selected = Boolean(state.photo && state.photo.id === entry.id);
    const item = document.createElement('div');
    item.className = 'photo-item' + (active ? ' active' : '')
      + (selected ? ' selected' : '');
    item.dataset.photoId = entry.id;
    item.title = entry.name || entry.id;
    item.setAttribute('aria-pressed', String(active));
    item.setAttribute('aria-selected', String(selected));
    item.draggable = true;
    item.addEventListener('click', () => selectPhoto(entry));
    const thumb = document.createElement('img');
    thumb.className = 'photo-thumb';
    thumb.alt = '';
    thumb.src = entry.thumb_url || entry.image_url;
    const meta = document.createElement('div');
    meta.className = 'photo-meta';
    const name = document.createElement('div');
    name.className = 'photo-name';
    name.textContent = entry.name || entry.id;
    const sub = document.createElement('div');
    sub.className = 'photo-sub';
    const size = Array.isArray(entry.image_size) ? entry.image_size.join('×') : '';
    const age = Number(entry.age_days);
    sub.textContent = [
      size,
      entry.date || '',
      Number.isFinite(age) ? `月龄 ${age.toFixed(1)} d` : '',
      entry.covered_features && entry.covered_features.length
        ? `${entry.covered_features.length} 个地貌` : '',
      entry.mask_url ? '蒙版' : '',
    ].filter(Boolean).join(' · ');
    meta.appendChild(name);
    meta.appendChild(sub);
    const mark = document.createElement('button');
    mark.className = 'photo-mark';
    mark.type = 'button';
    mark.title = active ? '隐藏这张照片' : '显示这张照片';
    mark.setAttribute('aria-pressed', String(active));
    mark.textContent = active ? '✓' : '○';
    mark.addEventListener('click', (event) => {
      event.stopPropagation();
      togglePhotoVisible(entry);
    });
    const remove = document.createElement('button');
    remove.className = 'photo-remove';
    remove.type = 'button';
    remove.title = '从照片库移除';
    remove.textContent = '×';
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deletePhoto(entry);
    });
    item.appendChild(thumb);
    item.appendChild(meta);
    item.appendChild(mark);
    item.appendChild(remove);
    item.addEventListener('dragstart', (event) => {
      state.photoDragId = entry.id;
      item.classList.add('dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        try { event.dataTransfer.setData('text/plain', entry.id); } catch (error) { /* noop */ }
      }
    });
    item.addEventListener('dragend', () => {
      state.photoDragId = '';
      item.classList.remove('dragging');
      for (const node of list.querySelectorAll('.photo-item')) {
        node.classList.remove('drop-before', 'drop-after');
      }
    });
    item.addEventListener('dragover', (event) => {
      if (!state.photoDragId || state.photoDragId === entry.id) return;
      event.preventDefault();
      const rect = item.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      item.classList.toggle('drop-before', !after);
      item.classList.toggle('drop-after', after);
    });
    item.addEventListener('dragleave', () => {
      item.classList.remove('drop-before', 'drop-after');
    });
    item.addEventListener('drop', (event) => {
      if (!state.photoDragId || state.photoDragId === entry.id) return;
      event.preventDefault();
      const rect = item.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      reorderPhoto(state.photoDragId, entry.id, after);
      state.photoDragId = '';
      for (const node of list.querySelectorAll('.photo-item')) {
        node.classList.remove('drop-before', 'drop-after');
      }
    });
    list.appendChild(item);
  }
}

/** Insert one photo before or after another, in the layer order. */
function reorderPhoto(dragId, targetId, after) {
  const order = [...state.photoOrder];
  const from = order.indexOf(dragId);
  if (from < 0) return;
  order.splice(from, 1);
  let to = order.indexOf(targetId);
  if (to < 0) to = order.length;
  else if (after) to += 1;
  order.splice(to, 0, dragId);
  state.photoOrder = order;
  rebuildPhotoList();
  render();
}

function ensurePhotoForDisplay(entry) {
  if (!ensurePhotoGL(entry)) ensurePhotoOverlay(entry);
}

function selectPhoto(entry) {
  if (!entry) return;
  // Selecting is not showing.  The row highlight says which photo the mask
  // editor and 定位 act on; the ☑ control on the right is the only way to put
  // the photo on the map.  Mixing the two made a mask session impossible: the
  // act of choosing the photo also hid it.
  state.photo = entry;
  if (state.photoActive.has(entry.id)) ensurePhotoForDisplay(entry);
  rebuildPhotoList();
}

function togglePhotoVisible(entry) {
  if (!entry) return;
  if (state.photoActive.has(entry.id)) {
    state.photoActive.delete(entry.id);
  } else {
    state.photoActive.add(entry.id);
    if (!state.photoOrder.includes(entry.id)) state.photoOrder.unshift(entry.id);
    ensurePhotoForDisplay(entry);
  }
  rebuildPhotoList();
  render();
}

async function loadPhotoLibrary() {
  const response = await fetch('/api/map/photos');
  if (!response.ok) throw new Error(await response.text());
  const payload = await response.json();
  state.photos = Array.isArray(payload.photos) ? payload.photos : [];
  state.photoDirectory = String(payload.directory || state.photoDirectory || '');
  syncPhotoOrder();
  const available = new Set(state.photos.map((entry) => entry.id));
  for (const id of [...state.photoActive]) {
    if (!available.has(id)) {
      state.photoActive.delete(id);
      state.photoOverlays.delete(id);
    }
  }
  for (const entry of state.photos) {
    if (state.photoActive.has(entry.id)) ensurePhotoForDisplay(entry);
  }
  if (state.photo) {
    const current = state.photos.find((item) => item.id === state.photo.id);
    state.photo = current || null;
  }
  if (!state.photo && state.photoActive.size) {
    const remaining = [...state.photoActive];
    const lastId = remaining[remaining.length - 1];
    state.photo = state.photos.find((item) => item.id === lastId) || null;
  }
  rebuildPhotoList();
  render();
}

function deletePhoto(entry) {
  fetch('/api/map/photos/' + encodeURIComponent(entry.id), { method: 'DELETE' })
    .then((response) => {
      if (!response.ok) throw new Error('remove failed');
      state.photoActive.delete(entry.id);
      state.photoOverlays.delete(entry.id);
      state.photoGLTextures.delete(entry.id);
      if (state.photo && state.photo.id === entry.id) state.photo = null;
      return loadPhotoLibrary();
    })
    .catch((error) => setStatus('移除照片失败：' + String(error.message || error)));
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('read failed'));
    reader.readAsDataURL(file);
  });
}

async function addPhotoFiles(files) {
  const list = Array.from(files || []);
  const projects = list.filter((file) => /\.(moonlabel|json)$/i.test(file.name));
  const images = list.filter((file) => /^image\//.test(file.type));
  if (!projects.length) {
    setStatus('请选择一个 .moonlabel 工程文件', 'error');
    return;
  }
  if (!images.length) {
    setStatus('工程旁边需要一张原照片', 'error');
    return;
  }
  const used = new Set();
  const imported = [];
  for (const projectFile of projects) {
    let project;
    try {
      project = JSON.parse(await projectFile.text());
    } catch (error) {
      setStatus('工程不是有效 JSON：' + projectFile.name, 'error');
      return;
    }
    const basename = String(project.image_path || '').split(/[\\/]/).pop().toLowerCase();
    let imageFile = images.find((file) => !used.has(file)
      && file.name.toLowerCase() === basename);
    if (!imageFile) imageFile = images.find((file) => !used.has(file));
    if (!imageFile) {
      setStatus('找不到工程对应的照片：' + projectFile.name, 'error');
      return;
    }
    used.add(imageFile);
    try {
      const imageData = await readFileAsDataURL(imageFile);
      const response = await fetch('/api/map/photos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project,
          image: imageData,
          name: imageFile.name,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'import failed');
      imported.push(payload);
    } catch (error) {
      setStatus('照片导入失败：' + String(error.message || error), 'error');
      return;
    }
  }
  await loadPhotoLibrary();
  let focus = null;
  for (const entry of imported) {
    const summary = state.photos.find((item) => item.id === entry.id);
    if (!summary) continue;
    state.photoActive.add(summary.id);
    ensurePhotoForDisplay(summary);
    focus = summary;
  }
  if (focus) state.photo = focus;
  rebuildPhotoList();
  render();
  setStatus(imported.length > 1
    ? `已加入 ${imported.length} 张照片` : '照片已加入库');
}

function setRightTab(panel) {
  const photos = panel === 'photos';
  const settings = panel === 'settings';
  for (const [id, active] of [['tab-selection', !photos && !settings],
                               ['tab-photos', photos],
                               ['tab-settings', settings]]) {
    const button = $(id);
    if (button) button.setAttribute('aria-pressed', String(active));
  }
  const selection = $('right-selection');
  const library = $('right-photos');
  const data = $('right-settings');
  if (selection) selection.hidden = photos || settings;
  if (library) library.hidden = !photos;
  if (data) data.hidden = !settings;
  if (photos) rebuildPhotoList();
  if (settings) {
    loadDataStatus().catch((error) => {
      const note = $('data-download-status');
      if (note) note.textContent = String(error.message || error);
    });
  }
}

function renderDataStatus(payload) {
  const dem = $('data-dem');
  const state = $('data-state');
  if (dem) dem.textContent = payload.dem || '—';
  if (state) {
    state.textContent = payload.fallback ? '未下载 DEM（平坦底图）' : '本地 DEM';
  }
  const button = $('data-download');
  const note = $('data-download-status');
  const download = payload.download || {};
  if (button) button.disabled = Boolean(download.running);
  if (note) {
    note.textContent = download.running
      ? (download.last_line || '下载中…')
      : (download.error || download.last_line || '');
  }
  if (download.running) setTimeout(loadDataStatus, 2000);
}

async function loadDataStatus() {
  const response = await fetch('/api/map/data');
  if (!response.ok) throw new Error(await response.text());
  renderDataStatus(await response.json());
}

function wireDataSettings() {
  if ($('data-refresh')) {
    $('data-refresh').addEventListener('click', () => {
      loadDataStatus().catch((error) => {
        const note = $('data-download-status');
        if (note) note.textContent = String(error.message || error);
      });
    });
  }
  if ($('data-download')) {
    $('data-download').addEventListener('click', () => {
      fetch('/api/map/data/download', { method: 'POST' })
        .then((response) => response.json().then((body) => ({ response, body })))
        .then(({ response, body }) => {
          if (!response.ok) throw new Error(body.error || 'download failed');
          renderDataStatus(body);
        })
        .catch((error) => {
          const note = $('data-download-status');
          if (note) note.textContent = String(error.message || error);
        });
    });
  }
}

function wireRightTabs() {
  if ($('tab-selection')) {
    $('tab-selection').addEventListener('click', () => setRightTab('selection'));
  }
  if ($('tab-photos')) {
    $('tab-photos').addEventListener('click', () => setRightTab('photos'));
  }
  if ($('tab-settings')) {
    $('tab-settings').addEventListener('click', () => setRightTab('settings'));
  }
  wireDataSettings();
}

/* ── photo mask editor ───────────────────────────────────────────────────
 *
 * A photo can carry a hand-drawn mask: white means "this part of the picture
 * is real sky", transparent means "leave it out".  The mask is stored in the
 * photograph's own pixel grid, so it follows the calibration into every
 * projection without a second registration.
 */
function maskEditorEntry() {
  if (state.photo) return state.photo;
  const active = orderedActivePhotos();
  if (active.length) return active[0];
  const filtered = filteredPhotos();
  return filtered.length ? filtered[0] : null;
}

function openPhotoMask() {
  const entry = maskEditorEntry();
  if (!entry) {
    setStatus('先选一张照片，再画它的蒙版', 'error');
    return;
  }
  $('photo-mask-backdrop').hidden = false;
  resetMaskWindowSize();
  $('mask-title').textContent = `蒙版 · ${entry.name || entry.id}`;
  const image = new Image();
  image.onload = () => setupMaskEditor(entry, image);
  image.onerror = () => {
    setStatus('照片载入失败：' + (entry.name || entry.id), 'error');
    $('photo-mask-backdrop').hidden = true;
  };
  image.src = entry.image_url;
}

function setupMaskEditor(entry, image) {
  const maxSide = 1600;
  const scale = Math.min(1, maxSide / Math.max(image.width, image.height));
  const maskW = Math.max(1, Math.round(image.width * scale));
  const maskH = Math.max(1, Math.round(image.height * scale));
  const mask = document.createElement('canvas');
  mask.width = maskW;
  mask.height = maskH;
  const maskCtx = mask.getContext('2d');
  maskCtx.fillStyle = '#ffffff';
  maskCtx.fillRect(0, 0, maskW, maskH);
  const display = $('photo-mask-canvas');
  const stageBox = display && display.parentElement
    ? display.parentElement.getBoundingClientRect() : null;
  const availableW = stageBox && stageBox.width > 40
    ? stageBox.width - 8 : Math.min(920, Math.max(320, window.innerWidth - 100));
  const availableH = stageBox && stageBox.height > 40
    ? stageBox.height - 8 : window.innerHeight - 230;
  const displayScale = Math.min(1, availableW / image.width,
                                 availableH / image.height);
  const displayW = Math.max(1, Math.round(image.width * displayScale));
  const displayH = Math.max(1, Math.round(image.height * displayScale));
  display.width = displayW;
  display.height = displayH;
  const cursor = $('photo-mask-cursor');
  if (cursor) {
    cursor.width = displayW;
    cursor.height = displayH;
  }
  state.photoMask = {
    entry, image, mask, maskCtx, maskW, maskH,
    display, cursor, displayW, displayH, mode: 'hide', zoom: 1, drawing: false,
    last: null, lastEvent: null, dirty: false,
  };
  applyMaskZoom();
  const finish = () => {
    renderMaskEditor();
    if (entry.mask_url) {
      const maskImage = new Image();
      maskImage.onload = () => {
        if (!state.photoMask || state.photoMask.entry.id !== entry.id) return;
        maskCtx.clearRect(0, 0, maskW, maskH);
        maskCtx.drawImage(maskImage, 0, 0, maskW, maskH);
        // Stored masks are greyscale (white = visible); the editor keeps its
        // own copy as an alpha channel so painting and erasing are exact.
        const data = maskCtx.getImageData(0, 0, maskW, maskH);
        const pixels = data.data;
        for (let index = 0; index < pixels.length; index += 4) {
          const grey = pixels[index];
          pixels[index] = 255;
          pixels[index + 1] = 255;
          pixels[index + 2] = 255;
          pixels[index + 3] = grey;
        }
        maskCtx.putImageData(data, 0, 0);
        renderMaskEditor();
      };
      maskImage.src = entry.mask_url;
    }
  };
  finish();
}

function applyMaskZoom() {
  const editor = state.photoMask;
  if (!editor) return;
  const zoom = Math.max(1, Math.min(4, Number(editor.zoom) || 1));
  editor.zoom = zoom;
  const wrap = $('photo-mask-wrap');
  if (wrap) {
    wrap.style.width = `${Math.max(1, Math.round(editor.displayW * zoom))}px`;
    wrap.style.height = `${Math.max(1, Math.round(editor.displayH * zoom))}px`;
  }
}

function resetMaskWindowSize() {
  const win = $('mask-window');
  if (!win) return;
  win.style.position = '';
  win.style.left = '';
  win.style.top = '';
  win.style.width = '';
  win.style.height = '';
  win.style.maxWidth = '';
  win.style.maxHeight = '';
}

/** Drag any edge or corner of the mask dialog, not just the native corner. */
function wireMaskResize() {
  const win = $('mask-window');
  if (!win) return;
  for (const handle of win.querySelectorAll('[data-mask-resize]')) {
    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = win.getBoundingClientRect();
      const direction = handle.dataset.maskResize || '';
      const startX = event.clientX;
      const startY = event.clientY;
      const start = {
        left: rect.left, top: rect.top, width: rect.width, height: rect.height,
      };
      win.style.position = 'fixed';
      win.style.left = `${start.left}px`;
      win.style.top = `${start.top}px`;
      win.style.width = `${start.width}px`;
      win.style.height = `${start.height}px`;
      win.style.maxWidth = 'none';
      win.style.maxHeight = 'none';
      if (handle.setPointerCapture) handle.setPointerCapture(event.pointerId);
      const move = (moveEvent) => {
        const dx = moveEvent.clientX - startX;
        const dy = moveEvent.clientY - startY;
        let left = start.left;
        let top = start.top;
        let width = start.width;
        let height = start.height;
        if (direction.includes('e')) width = start.width + dx;
        if (direction.includes('s')) height = start.height + dy;
        if (direction.includes('w')) {
          width = start.width - dx;
          left = start.left + dx;
        }
        if (direction.includes('n')) {
          height = start.height - dy;
          top = start.top + dy;
        }
        width = Math.max(420, Math.min(window.innerWidth - 16, width));
        height = Math.max(320, Math.min(window.innerHeight - 16, height));
        if (direction.includes('w')) left = Math.max(4, start.left + start.width - width);
        if (direction.includes('n')) top = Math.max(4, start.top + start.height - height);
        win.style.left = `${left}px`;
        win.style.top = `${top}px`;
        win.style.width = `${width}px`;
        win.style.height = `${height}px`;
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  }
}

function renderMaskEditor() {
  const editor = state.photoMask;
  if (!editor) return;
  const ctx = editor.display.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, editor.displayW, editor.displayH);
  // Show the photograph through its own mask, so a hidden patch is genuinely
  // absent rather than merely tinted.  The red veil below marks that patch.
  const visible = document.createElement('canvas');
  visible.width = editor.displayW;
  visible.height = editor.displayH;
  const visibleCtx = visible.getContext('2d');
  visibleCtx.drawImage(editor.image, 0, 0, editor.displayW, editor.displayH);
  visibleCtx.globalCompositeOperation = 'destination-in';
  visibleCtx.drawImage(editor.mask, 0, 0, editor.displayW, editor.displayH);
  ctx.drawImage(visible, 0, 0);
  const dim = document.createElement('canvas');
  dim.width = editor.displayW;
  dim.height = editor.displayH;
  const dimCtx = dim.getContext('2d');
  dimCtx.fillStyle = 'rgba(216, 48, 48, .30)';
  dimCtx.fillRect(0, 0, dim.width, dim.height);
  dimCtx.globalCompositeOperation = 'destination-out';
  dimCtx.drawImage(editor.mask, 0, 0, dim.width, dim.height);
  ctx.drawImage(dim, 0, 0);
}

/** Draw the circular brush footprint, or clear it when the pointer leaves. */
function drawMaskCursor(event) {
  const editor = state.photoMask;
  if (!editor || !editor.cursor) return;
  const rect = editor.display.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return;
  editor.lastEvent = event;
  const ctx = editor.cursor.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, editor.cursor.width, editor.cursor.height);
  const scale = editor.cursor.width / rect.width;
  const x = (event.clientX - rect.left) * scale;
  const y = (event.clientY - rect.top) * scale;
  if (x < 0 || y < 0 || x > editor.cursor.width || y > editor.cursor.height) {
    return;
  }
  const size = Number($('photo-mask-size') && $('photo-mask-size').value) || 28;
  const radius = Math.max(2, (size / 2) * scale);
  const hideArea = editor.mode === 'hide';
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = hideArea ? 'rgba(255, 70, 60, .16)' : 'rgba(255, 255, 255, .10)';
  ctx.fill();
  ctx.lineWidth = 2.2;
  ctx.strokeStyle = 'rgba(8, 11, 16, .78)';
  ctx.stroke();
  ctx.lineWidth = 1.2;
  ctx.strokeStyle = hideArea ? '#ff5a52' : '#f2f6ff';
  ctx.stroke();
  ctx.restore();
}

function clearMaskCursor() {
  const editor = state.photoMask;
  if (!editor || !editor.cursor) return;
  const ctx = editor.cursor.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, editor.cursor.width, editor.cursor.height);
  editor.lastEvent = null;
}

function maskPointerPoint(event) {
  const editor = state.photoMask;
  if (!editor) return null;
  const rect = editor.display.getBoundingClientRect();
  const x = (event.clientX - rect.left) / Math.max(1e-6, rect.width) * editor.maskW;
  const y = (event.clientY - rect.top) / Math.max(1e-6, rect.height) * editor.maskH;
  if (x < 0 || y < 0 || x > editor.maskW || y > editor.maskH) return null;
  return { x, y };
}

function paintMaskStroke(from, to) {
  const editor = state.photoMask;
  if (!editor) return;
  const ctx = editor.maskCtx;
  const size = Number($('photo-mask-size') && $('photo-mask-size').value) || 28;
  const width = Math.max(1, size * editor.maskW / Math.max(1, editor.displayW));
  ctx.save();
  ctx.globalCompositeOperation = editor.mode === 'hide'
    ? 'destination-out' : 'source-over';
  ctx.strokeStyle = '#ffffff';
  ctx.fillStyle = '#ffffff';
  ctx.lineWidth = width;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(to.x, to.y, width / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  editor.dirty = true;
  renderMaskEditor();
}

function setMaskMode(mode) {
  if (!state.photoMask) return;
  state.photoMask.mode = mode === 'show' ? 'show' : 'hide';
  $('mask-pen').setAttribute('aria-pressed',
    String(state.photoMask.mode === 'hide'));
  $('mask-eraser').setAttribute('aria-pressed',
    String(state.photoMask.mode === 'show'));
  if (state.photoMask.lastEvent) drawMaskCursor(state.photoMask.lastEvent);
}

function closePhotoMask() {
  clearMaskCursor();
  state.photoMask = null;
  $('photo-mask-backdrop').hidden = true;
}

function resetMaskEditor() {
  const editor = state.photoMask;
  if (!editor) return;
  editor.maskCtx.globalCompositeOperation = 'source-over';
  editor.maskCtx.fillStyle = '#ffffff';
  editor.maskCtx.fillRect(0, 0, editor.maskW, editor.maskH);
  editor.dirty = true;
  renderMaskEditor();
}

async function savePhotoMask() {
  const editor = state.photoMask;
  if (!editor) return;
  const output = document.createElement('canvas');
  output.width = editor.maskW;
  output.height = editor.maskH;
  const ctx = output.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, output.width, output.height);
  ctx.globalCompositeOperation = 'destination-in';
  ctx.drawImage(editor.mask, 0, 0);
  const data = ctx.getImageData(0, 0, output.width, output.height);
  const pixels = data.data;
  for (let index = 0; index < pixels.length; index += 4) {
    const alpha = pixels[index + 3];
    pixels[index] = alpha;
    pixels[index + 1] = alpha;
    pixels[index + 2] = alpha;
    pixels[index + 3] = 255;
  }
  ctx.putImageData(data, 0, 0);
  const entry = editor.entry;
  try {
    const response = await fetch(
      '/api/map/photos/' + encodeURIComponent(entry.id) + '/mask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: output.toDataURL('image/png') }),
      });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'mask failed');
    applyPhotoMaskChange(entry.id, payload.mask_url || true);
    closePhotoMask();
    setStatus('蒙版已保存：' + (entry.name || entry.id));
  } catch (error) {
    setStatus('蒙版保存失败：' + String(error.message || error), 'error');
  }
}

async function clearPhotoMask() {
  const editor = state.photoMask;
  if (!editor) return;
  try {
    const response = await fetch(
      '/api/map/photos/' + encodeURIComponent(editor.entry.id) + '/mask',
      { method: 'DELETE' });
    if (!response.ok) throw new Error('delete failed');
    const entry = editor.entry;
    applyPhotoMaskChange(entry.id, null);
    resetMaskEditor();
    setStatus('蒙版已清除：' + (entry.name || entry.id));
  } catch (error) {
    setStatus('清除蒙版失败：' + String(error.message || error), 'error');
  }
}

/** Drop every cached projection of one photo after its mask changed. */
function applyPhotoMaskChange(id, maskUrl) {
  const entry = state.photos.find((item) => item.id === id);
  if (entry) entry.mask_url = maskUrl;
  state.photoOverlays.delete(id);
  state.photoGLTextures.delete(id);
  for (const item of orderedActivePhotos()) {
    if (item.id === id) ensurePhotoForDisplay(item);
  }
  render();
}

function wirePhotoMask() {
  if ($('photo-mask')) {
    $('photo-mask').addEventListener('click', openPhotoMask);
  }
  if ($('photo-mask-close')) {
    $('photo-mask-close').addEventListener('click', closePhotoMask);
  }
  if ($('photo-mask-save')) {
    $('photo-mask-save').addEventListener('click', savePhotoMask);
  }
  if ($('photo-mask-reset')) {
    $('photo-mask-reset').addEventListener('click', resetMaskEditor);
  }
  if ($('photo-mask-clear')) {
    $('photo-mask-clear').addEventListener('click', clearPhotoMask);
  }
  if ($('mask-pen')) {
    $('mask-pen').addEventListener('click', () => setMaskMode('hide'));
  }
  if ($('mask-eraser')) {
    $('mask-eraser').addEventListener('click', () => setMaskMode('show'));
  }
  if ($('photo-mask-size')) {
    $('photo-mask-size').addEventListener('input', () => {
      if (state.photoMask && state.photoMask.lastEvent) {
        drawMaskCursor(state.photoMask.lastEvent);
      }
    });
  }
  wireMaskResize();
  if ($('photo-mask-canvas')) {
    const stage = $('photo-mask-canvas').parentElement
      && $('photo-mask-canvas').parentElement.parentElement;
    if (stage) {
      stage.addEventListener('wheel', (event) => {
        const editor = state.photoMask;
        if (!editor) return;
        event.preventDefault();
        const previous = editor.zoom || 1;
        const next = Math.max(1, Math.min(4,
          previous * Math.exp(-event.deltaY * 0.0015)));
        if (Math.abs(next - previous) < 0.001) return;
        const rect = editor.display.getBoundingClientRect();
        const anchorX = (event.clientX - rect.left) / Math.max(1, rect.width);
        const anchorY = (event.clientY - rect.top) / Math.max(1, rect.height);
        editor.zoom = next;
        applyMaskZoom();
        const after = editor.display.getBoundingClientRect();
        stage.scrollLeft += after.left + anchorX * after.width - event.clientX;
        stage.scrollTop += after.top + anchorY * after.height - event.clientY;
      }, { passive: false });
    }
  }
  const display = $('photo-mask-canvas');
  if (display) {
    display.addEventListener('pointerdown', (event) => {
      if (!state.photoMask) return;
      const point = maskPointerPoint(event);
      if (!point) return;
      drawMaskCursor(event);
      state.photoMask.drawing = true;
      state.photoMask.last = point;
      paintMaskStroke(point, point);
      display.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    display.addEventListener('pointerenter', (event) => {
      drawMaskCursor(event);
    });
    display.addEventListener('pointermove', (event) => {
      if (!state.photoMask) return;
      drawMaskCursor(event);
      if (!state.photoMask.drawing) return;
      const point = maskPointerPoint(event);
      if (!point) return;
      paintMaskStroke(state.photoMask.last || point, point);
      state.photoMask.last = point;
      event.preventDefault();
    });
    const stop = () => {
      if (!state.photoMask) return;
      state.photoMask.drawing = false;
      state.photoMask.last = null;
    };
    display.addEventListener('pointerup', stop);
    display.addEventListener('pointercancel', stop);
    display.addEventListener('pointerleave', () => { stop(); clearMaskCursor(); });
  }
}

function wirePhotoLibrary() {
  if ($('photo-add') && $('photo-files')) {
    $('photo-add').addEventListener('click', () => $('photo-files').click());
    $('photo-files').addEventListener('change', () => {
      addPhotoFiles($('photo-files').files);
      $('photo-files').value = '';
    });
  }
  if ($('photo-locate')) {
    $('photo-locate').addEventListener('click', locatePhoto);
  }
  if ($('photo-refresh')) {
    $('photo-refresh').addEventListener('click', () => {
      loadPhotoLibrary().catch((error) => setStatus(
        '照片库刷新失败：' + String(error.message || error)));
    });
  }
  if ($('photo-opacity')) {
    state.photoOpacity = Number($('photo-opacity').value) || 0.85;
    $('photo-opacity').addEventListener('input', () => {
      state.photoOpacity = Number($('photo-opacity').value);
      render();
    });
  }
  if ($('photo-visible')) {
    $('photo-visible').addEventListener('change', () => {
      state.photoVisible = $('photo-visible').checked;
      render();
    });
  }
  const today = new Date().toISOString().slice(0, 10);
  const fiftyYearsAgo = new Date(Date.UTC(
    new Date().getUTCFullYear() - 50, new Date().getUTCMonth(),
    new Date().getUTCDate())).toISOString().slice(0, 10);
  if ($('photo-filter-date-start')) {
    $('photo-filter-date-start').value = fiftyYearsAgo;
    state.photoFilters.dateStart = fiftyYearsAgo;
  }
  if ($('photo-filter-date-end')) {
    $('photo-filter-date-end').value = today;
    state.photoFilters.dateEnd = today;
  }
  for (const [id, key] of [['photo-filter-date-start', 'dateStart'],
                           ['photo-filter-date-end', 'dateEnd']]) {
    if (!$(id)) continue;
    $(id).addEventListener('change', () => {
      state.photoFilters[key] = $(id).value || '';
      rebuildPhotoList();
    });
  }
  for (const [id, key] of [['photo-filter-age-min', 'ageMin'],
                           ['photo-filter-age-max', 'ageMax']]) {
    if (!$(id)) continue;
    $(id).addEventListener('input', () => {
      const value = Number($(id).value);
      state.photoFilters[key] = $(id).value === '' || !Number.isFinite(value)
        ? null : value;
      rebuildPhotoList();
    });
  }
  if ($('photo-filter-feature')) {
    $('photo-filter-feature').addEventListener('input', () => {
      state.photoFilters.query = $('photo-filter-feature').value.trim();
      rebuildPhotoList();
    });
    $('photo-filter-feature').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      addPhotoFeatureFilter($('photo-filter-feature').value);
      $('photo-filter-feature').value = '';
      state.photoFilters.query = '';
    });
  }
  if ($('photo-filter-add')) {
    $('photo-filter-add').addEventListener('click', () => {
      addPhotoFeatureFilter($('photo-filter-feature').value);
      $('photo-filter-feature').value = '';
      state.photoFilters.query = '';
    });
  }
  if ($('photo-filter-use-selection')) {
    $('photo-filter-use-selection').addEventListener('click', () => {
      const feature = state.selected;
      if (!feature) {
        setStatus('先在图上选中一个地貌', 'error');
        return;
      }
      addPhotoFeatureFilter(feature.name_en || feature.name_zh);
      setStatus(`照片筛选已加入：${featureName(feature)}`);
    });
  }
  if ($('photo-directory')) {
    $('photo-directory').addEventListener('click', () => {
      const answer = window.prompt('照片库目录', state.photoDirectory || '');
      if (answer === null || !String(answer).trim()) return;
      fetch('/api/map/photos/directory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: String(answer).trim() }),
      }).then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || '目录修改失败');
        state.photoDirectory = body.directory || state.photoDirectory;
        state.photo = null;
        state.photoActive.clear();
        return loadPhotoLibrary();
      }).then(() => setStatus(`照片库目录：${state.photoDirectory}`))
        .catch((error) => setStatus('照片库目录修改失败：' + String(error.message || error), 'error'));
    });
  }
  if ($('photo-filter-clear')) {
    $('photo-filter-clear').addEventListener('click', () => {
      state.photoFilters = { dateStart: '', dateEnd: '', ageMin: null,
                             ageMax: null, features: [], query: '' };
      for (const id of ['photo-filter-date-start', 'photo-filter-date-end',
                        'photo-filter-feature']) {
        if ($(id)) $(id).value = '';
      }
      for (const id of ['photo-filter-age-min', 'photo-filter-age-max']) {
        if ($(id)) $(id).value = '';
      }
      renderFilterChips();
      rebuildPhotoList();
    });
  }
  wirePhotoMask();
}

function render() {
  ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  ctx.clearRect(0, 0, state.width, state.height);
  drawBackground();
  if (state.active.has('base')) {
    if (state.projection === 'equirect') drawTiles();
    else drawProjectedBase();
  }
  drawPhotoOverlay();
  if (state.marks.graticule) drawGraticule();
  drawLightingOverlay();
  drawFeatures();
  drawManualMarks();
  drawScaleBar();
  const zoomLabel = $('zoom-label');
  if (zoomLabel) {
    // Magnification relative to the z0 tile grid: each zoom step doubles the
    // number of map pixels on screen, so this is the factor a reader means by
    // "how far in am I", not the raw tile-pixel scale.
    const magnification = state.projection === 'globe'
      ? state.globe.zoom
      : state.scale * 2 ** (state.zoom + 1);
    zoomLabel.textContent = `z${state.zoom} · ${magnification.toFixed(1)}x`;
  }
}

function drawBackground() {
  ctx.fillStyle = '#05070b';
  ctx.fillRect(0, 0, state.width, state.height);
}

function tileKey(z, x, y) {
  return z + '/' + x + '/' + y;
}

function tileToneKey() {
  return `${state.earthshine ? 1 : 0},${(Number(state.shadowBias) || 0).toFixed(3)}`;
}

function tileQuery() {
  const params = new URLSearchParams();
  const lightKey = tileLightKey();
  if (lightKey) params.set('light', lightKey);
  params.set('v', TILE_VERSION);
  if (!state.earthshine) params.set('earth', '0');
  const bias = Number(state.shadowBias) || 0;
  if (Math.abs(bias) > 0.0005) params.set('shadow', bias.toFixed(3));
  return '?' + params.toString();
}

function clearBaseCaches() {
  state.tiles.clear();
  state.sourceCanvas = null;
  state.sourcePixels = null;
  state.sourceKey = '';
  state.sourceReadyKey = '';
  state.sourceLoadingKey = '';
}

function tileCacheKey(z, x, y) {
  return tileKey(z, x, y) + '|' + (tileLightKey() || 'plain')
    + '|' + tileToneKey();
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
  entry.image.src = `/api/map/tile/${z}/${x}/${y}.png${tileQuery()}`;
  return entry;
}

function drawTiles() {
  const world = worldSize();
  const cam = camera();
  const left = cam.x - state.width / (2 * state.scale);
  const top = cam.y - state.height / (2 * state.scale);
  const right = cam.x + state.width / (2 * state.scale);
  const bottom = cam.y + state.height / (2 * state.scale);
  const x0 = Math.floor(left / TILE);
  const x1 = Math.ceil(right / TILE);
  const y0 = Math.max(0, Math.floor(top / TILE));
  const y1 = Math.min(world.rows - 1, Math.ceil(bottom / TILE));
  ctx.save();
  const brightness = Math.max(0.2, Math.min(3, Number(state.brightness) || 1));
  const dim = Math.max(0, 1 - brightness);
  const lift = Math.max(0, brightness - 1);
  // Canvas filters were not visibly changing the tiles in this browser, so
  // brightness is a guaranteed composite pass over each tile rectangle: a
  // black veil dims, an additive white veil lifts.  Labels and photos are
  // painted after this and stay untouched.
  const toneTile = (x, y, size) => {
    if (dim > 0.001) {
      ctx.fillStyle = `rgba(0, 0, 0, ${Math.min(0.85, dim)})`;
      ctx.fillRect(x, y, size, size);
    } else if (lift > 0.001) {
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      ctx.fillStyle = `rgba(255, 255, 255, ${Math.min(0.65, lift * 0.55)})`;
      ctx.fillRect(x, y, size, size);
      ctx.restore();
    }
  };
  for (let y = y0; y <= y1; y += 1) {
    const screenY = (y * TILE - cam.y) * state.scale + state.height / 2;
    for (let sx = x0; sx <= x1; sx += 1) {
      const x = ((sx % world.cols) + world.cols) % world.cols;
      const entry = tileEntry(state.zoom, x, y);
      const screenX = (sx * TILE - cam.x) * state.scale + state.width / 2;
      const size = TILE * state.scale;
      if (entry.ready) {
        // One pixel of bleed: fractional tile sizes otherwise leave a dark
        // hairline at every tile edge, which reads as a second, dimmer grid.
        ctx.drawImage(entry.image, screenX, screenY, size + 1, size + 1);
        toneTile(screenX, screenY, size);
      } else {
        const drew = drawAncestor(x, y, screenX, screenY, size);
        if (drew) {
          toneTile(screenX, screenY, size);
        } else {
          // No ancestor is cached yet: ask for one.  A dark square that fills
          // in later reads as a block of different brightness next to its
          // loaded neighbours; a coarse picture stretched over the hole reads
          // as a picture being sharpened.
          if (state.zoom > MIN_ZOOM) {
            const parentCols = 2 ** state.zoom;
            tileEntry(state.zoom - 1,
              ((Math.floor(x / 2) % parentCols) + parentCols) % parentCols,
              Math.floor(y / 2));
          }
          ctx.fillStyle = '#0a0f17';
          ctx.fillRect(screenX, screenY, size, size);
          toneTile(screenX, screenY, size);
        }
      }
    }
  }
  ctx.restore();
}

function drawAncestor(x, y, screenX, screenY, size) {
  for (let depth = 1; depth <= state.zoom; depth += 1) {
    const z = state.zoom - depth;
    const span = 2 ** depth;
    const cols = 2 ** (z + 1);
    const parentX = ((Math.floor(x / span) % cols) + cols) % cols;
    const parentY = Math.floor(y / span);
    const entry = state.tiles.get(tileCacheKey(z, parentX, parentY));
    if (!entry || !entry.ready) continue;
    const sx = ((x - parentX * span) / span) * TILE;
    const sy = ((y - parentY * span) / span) * TILE;
    const sw = TILE / span;
    ctx.drawImage(entry.image, sx, sy, sw, sw, screenX, screenY,
      size + 1, size + 1);
    return true;
  }
  return false;
}

function lightZoneOffset() {
  const value = Number($('light-tz') && $('light-tz').value);
  return Number.isFinite(value) ? value : 0;
}

/** Set the date/time controls to "now" in the chosen observer time zone. */
function setLightNow() {
  const now = new Date(Date.now() + lightZoneOffset() * 3600 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  $('light-when').value = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-`
    + `${pad(now.getUTCDate())}T${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}`;
}

/** The selected local date/time + zone, as an ISO UTC instant. */
function lightingWhen() {
  const value = $('light-when') && $('light-when').value;
  if (!value) return '';
  const local = Date.parse(`${value.length === 16 ? `${value}:00` : value}Z`);
  if (!Number.isFinite(local)) return '';
  return new Date(local - lightZoneOffset() * 3600 * 1000)
    .toISOString().slice(0, 19) + 'Z';
}

function tileLightKey() {
  // 月面光照 decides whether the tiles are shaded.  The terminator overlay is
  // a separate switch; the old 夜面压暗 layer has been removed.
  return state.lightingOn ? state.lightingKey : '';
}

function unitVector(lon, lat) {
  const lo = lon * Math.PI / 180;
  const la = lat * Math.PI / 180;
  return [Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)];
}

function cross3(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
          a[0] * b[1] - a[1] * b[0]];
}

/** Points on the terminator, sampled as a circle in three dimensions.
 *
 * The terminator is the great circle whose plane is perpendicular to the Sun's
 * direction.  Sampling it in longitude -- the old way, one point per degree --
 * crowds the samples at the poles and, on the globe, turns the near-limb part
 * of the curve into long straight chords between sparse projections.  A circle
 * in three dimensions is evenly sampled in *screen* distance in every
 * projection, so the dashed line stays a smooth arc.
 */
function terminatorPoints(subsolar, count = 480) {
  const sun = unitVector(subsolar.lon, subsolar.lat);
  const helper = Math.abs(sun[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const raw = cross3(sun, helper);
  const norm = Math.hypot(raw[0], raw[1], raw[2]) || 1;
  const e1 = raw.map((value) => value / norm);
  const e2 = cross3(sun, e1);
  const points = [];
  for (let index = 0; index < count; index += 1) {
    const angle = 2 * Math.PI * index / count;
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    const v = [
      c * e1[0] + s * e2[0],
      c * e1[1] + s * e2[1],
      c * e1[2] + s * e2[2],
    ];
    points.push({
      lon: Math.atan2(v[1], v[0]) * 180 / Math.PI,
      lat: Math.asin(clamp(v[2], -1, 1)) * 180 / Math.PI,
    });
  }
  return points;
}

/** One parallel of latitude, broken where it goes behind the globe. */
function drawParallel(latDeg, colour, dash = [4, 4], width = 1) {
  ctx.save();
  ctx.beginPath();
  let started = false;
  for (let lon = -180; lon <= 180; lon += 2) {
    const point = lonlatToScreen(lon, latDeg);
    if (point.visible === false) { started = false; continue; }
    if (!started) { ctx.moveTo(point.x, point.y); started = true; }
    else ctx.lineTo(point.x, point.y);
  }
  ctx.strokeStyle = colour;
  ctx.lineWidth = width;
  ctx.setLineDash(dash);
  ctx.stroke();
  ctx.restore();
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
  const marks = state.marks;
  // Geometric marks first: they are geometry, not lighting, so they stay when
  // the shaded tiles are switched off.
  if (state.marksOn) {
    if (marks.equator && !state.marks.graticule) {
      drawParallel(0, 'rgba(150, 200, 255, .55)');
    }
    if (marks.poles) {
      drawLightPoint(0, 90, '#9ecbff', 'N');
      drawLightPoint(0, -90, '#9ecbff', 'S');
    }
  }
  if (!light) return;
  const subsolar = light.subsolar;

  if (!state.marksOn) return;
  if (marks.terminator) {
    const curve = terminatorPoints(subsolar);
    ctx.save();
    ctx.beginPath();
    let started = false;
    for (const point of curve) {
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

  if (marks.subsolar) {
    drawLightPoint(subsolar.lon, subsolar.lat, '#ffd166', '日下点');
  }
  if (marks.libration && light.sub_earth) {
    drawLightPoint(light.sub_earth.lon, light.sub_earth.lat, '#7ae0d0', '天平动点');
  }
}

async function loadLighting() {
  const params = new URLSearchParams();
  const when = lightingWhen();
  if (when) params.set('when', when);
  params.set('lat', $('light-lat').value || '0');
  params.set('lon', $('light-lon').value || '0');
  const response = await fetch('/api/map/lighting?' + params.toString());
  if (!response.ok) throw new Error(await response.text());
  state.lighting = await response.json();
  state.lightingOn = $('light-on').checked;
  const subsolar = state.lighting.subsolar;
  state.lightingKey = state.lightingOn
    ? `${subsolar.lon.toFixed(3)},${subsolar.lat.toFixed(3)}` : '';
  state.tiles.clear();
  const phase = state.lighting.phase || {};
  const phaseName = state.language === 'en'
    ? (phase.name_en || phase.name_zh || '')
    : (phase.name_zh || phase.name_en || '');
  const set = (id, text) => {
    const node = $(id);
    if (node) node.textContent = text;
  };
  const moon = state.lighting.moon || {};
  const subEarth = state.lighting.sub_earth || {};
  set('mi-map-phase', phaseName || '—');
  set('mi-map-illum', `${Number(phase.illumination_percent || 0).toFixed(1)}%`);
  set('mi-map-phase-angle', `${Number(phase.phase_angle_deg || 0).toFixed(2)}°`);
  set('mi-map-age', `${Number(phase.age_days || 0).toFixed(2)} d`);
  set('mi-map-libration', `${Number(subEarth.lon || 0).toFixed(2)}° / ${Number(subEarth.lat || 0).toFixed(2)}°`);
  set('mi-map-subsolar', `${Number(subsolar.lon).toFixed(2)}° / ${Number(subsolar.lat).toFixed(2)}°`);
  set('mi-map-colong', `${Number(state.lighting.colongitude || 0).toFixed(2)}°`);
  set('mi-map-dist', `${Number(moon.distance_km || 0).toFixed(0)} / ${Number(moon.topo_distance_km || 0).toFixed(0)} km`);
  set('mi-map-diam', `${Number(moon.angular_diameter_arcsec || 0).toFixed(1)}″`);
  set('mi-map-alt', `${Number(moon.elevation_deg || 0).toFixed(2)}°`);
  set('mi-map-az', `${Number(moon.azimuth_deg || 0).toFixed(2)}°`);
  set('mi-map-ra', `${Number(moon.ra_hours || 0).toFixed(3)} h`);
  set('mi-map-dec', `${Number(moon.dec_deg || 0).toFixed(2)}°`);
  set('mi-map-elong', `${Number(moon.elongation_deg || 0).toFixed(2)}°`);
  set('mi-map-sunalt', `${Number(moon.sun_elevation_deg || 0).toFixed(2)}°`);
  set('mi-map-vel', `${Number(moon.velocity_kms || 0).toFixed(3)} km/s`);
  set('mi-map-light', `${Number(moon.light_time_s || 0).toFixed(2)} s`);
  render();
}

/** A high-resolution source window for a projected view.
 *
 * The equirect map can ask the tile pyramid for the exact zoom under the
 * pointer.  Mercator and the globe first composite an equirect texture; a
 * whole-sphere z4 texture made those modes visibly softer than the equirect
 * map.  At close zooms the visible region is small, so fetch only its tiles
 * (z7 uses the same SLDEM-backed high-detail tiles) and remember the bounds
 * to sample them.  At low zooms the full z4 sphere is still the right answer.
 */
function projectionSourceWindow() {
  if (state.projection === 'equirect') return null;
  // The globe's wheel changes globe.zoom, not state.zoom; both have to decide
  // when the equirect texture stops being detailed enough.
  const viewZoom = state.projection === 'globe'
    // One screen pixel is about 6.8 Z screen px per degree on the globe, and a
    // z tile carries 1.42 * 2^z px per degree: z ~ log2(Z) + 2.3.  Using the
    // stale equirect level instead kept the sphere on the whole-world z4
    // texture at every magnification.
    ? Math.log2(Math.max(0.45, state.globe.zoom)) + 3.0
    : state.zoom;
  if (viewZoom < 5) return null;
  let west = 180;
  let east = -180;
  let south = 90;
  let north = -90;
  let valid = 0;
  for (let row = 0; row <= 6; row += 1) {
    for (let col = 0; col <= 6; col += 1) {
      const geo = screenToLonLat(
        state.width * col / 6, state.height * row / 6);
      if (!geo) continue;
      valid += 1;
      west = Math.min(west, geo.lon);
      east = Math.max(east, geo.lon);
      south = Math.min(south, geo.lat);
      north = Math.max(north, geo.lat);
    }
  }
  if (valid < 5 || !(east > west) || !(north > south)
      || east - west > 300) return null;
  const lonPad = Math.max(0.3, (east - west) * 0.08);
  const latPad = Math.max(0.3, (north - south) * 0.08);
  west = Math.max(-180, west - lonPad);
  east = Math.min(180, east + lonPad);
  south = Math.max(-85, south - latPad);
  north = Math.min(85, north + latPad);

  let z = Math.min(8, Math.max(5, Math.round(viewZoom)));
  while (z >= 5) {
    const cols = 2 ** (z + 1);
    const rows = 2 ** z;
    const tw = 360 / cols;
    const th = 180 / rows;
    let x0 = Math.floor((west + 180) / tw);
    let x1 = Math.floor((east + 180 - 1e-9) / tw);
    let y0 = Math.floor((90 - north) / th);
    let y1 = Math.floor((90 - south - 1e-9) / th);
    x0 = clamp(x0, 0, cols - 1);
    x1 = clamp(x1, x0, cols - 1);
    y0 = clamp(y0, 0, rows - 1);
    y1 = clamp(y1, y0, rows - 1);
    const across = x1 - x0 + 1;
    const down = y1 - y0 + 1;
    if (across <= 16 && down <= 16 && across * down <= 96) {
      return {
        z, x0, x1, y0, y1,
        west: -180 + x0 * tw,
        east: -180 + (x1 + 1) * tw,
        north: 90 - y0 * th,
        south: 90 - (y1 + 1) * th,
      };
    }
    z -= 1;
  }
  return null;
}

function ensureProjectionSource() {
  const window = projectionSourceWindow();
  const sourceZoom = window ? window.z : 4;
  const x0 = window ? window.x0 : 0;
  const y0 = window ? window.y0 : 0;
  const cols = window ? window.x1 - window.x0 + 1 : 2 ** (sourceZoom + 1);
  const rows = window ? window.y1 - window.y0 + 1 : 2 ** sourceZoom;
  const key = [
    tileLightKey() || 'plain', tileToneKey(), state.projection, sourceZoom,
    window ? `${window.x0},${window.y0},${window.x1},${window.y1}` : 'world',
  ].join('|');
  if (state.sourceCanvas && state.sourceReadyKey === key) return;
  if (state.sourceLoadingKey === key) return;
  state.sourceLoadingKey = key;
  state.sourceKey = key;
  // Keep the previous texture on screen until the new one is assembled.  The
  // old code dropped it the instant the camera moved, so every pan in Mercator
  // flashed the stage to black while the window was fetched again.
  state.sourceZoom = sourceZoom;
  const canvas = document.createElement('canvas');
  canvas.width = cols * TILE;
  canvas.height = rows * TILE;
  const source = canvas.getContext('2d');
  source.fillStyle = '#05070b';
  source.fillRect(0, 0, canvas.width, canvas.height);
  const query = tileQuery();
  let done = 0;
  const total = cols * rows;
  const finish = () => {
    done += 1;
    if (done < total) return;
    if (state.sourceKey !== key) return;
    state.sourceLoadingKey = null;
    state.sourceCanvas = canvas;
    state.sourceReadyKey = key;
    state.sourceBounds = window ? {
      west: window.west, east: window.east,
      north: window.north, south: window.south,
    } : null;
    try {
      state.sourcePixels = source.getImageData(0, 0, canvas.width, canvas.height);
    } catch (error) {
      state.sourcePixels = null;
    }
    render();
  };
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const image = new Image();
      const url = `/api/map/tile/${sourceZoom}/${x0 + col}/${y0 + row}.png${query}`;
      let attempts = 0;
      image.onload = () => {
        source.drawImage(image, col * TILE, row * TILE);
        finish();
      };
      image.onerror = () => {
        // One retry, then give up.  A dropped request used to leave its cell
        // as the dark fill for the life of the texture -- one black block
        // among sharp neighbours, which reads as a seam.
        attempts += 1;
        if (attempts <= 1) {
          setTimeout(() => {
            image.src = url + (url.includes('?') ? '&' : '?') + 'retry=' + attempts;
          }, 350);
          return;
        }
        finish();
      };
      image.src = url;
    }
  }
}

function drawProjectedBase() {
  ensureProjectionSource();
  if (!state.sourceCanvas || !state.sourcePixels) return;
  const width = Math.max(1, Math.round(state.width));
  const height = Math.max(1, Math.round(state.height));
  const cacheKey = [
    state.projection, state.centerLon, state.centerLat, state.mercY,
    state.globe.lon, state.globe.lat, state.globe.zoom,
    width, height, state.scale, state.sourceReadyKey,
    state.brightness,
  ].join('|');
  if (state.projectedBase && state.projectedBase.key === cacheKey) {
    ctx.drawImage(state.projectedBase.canvas, 0, 0);
    drawGlobeOutline();
    return;
  }
  const source = state.sourcePixels.data;
  const sourceWidth = state.sourcePixels.width;
  const sourceHeight = state.sourcePixels.height;
  const image = ctx.createImageData(width, height);
  const out = image.data;
  const background = [5, 7, 11, 255];
  const gain = Math.max(0.2, Math.min(3, Number(state.brightness) || 1));
  const bounds = state.sourceBounds;
  if (state.projection === 'mercator') {
    // Mercator is separable: longitude depends only on x and the log-tangent
    // latitude only on y.  Precomputing both columns avoids a full inverse
    // projection (and its trigonometry) at every output pixel.
    const world = worldSize();
    const pxPerDeg = (world.w / 360) * state.scale;
    const pxPerRad = pxPerDeg * 180 / Math.PI;
    const period = mercatorPeriod();
    const sx = new Int32Array(width);
    const uValid = new Uint8Array(width);
    for (let x = 0; x < width; x += 1) {
      const lon = wrapLon(state.centerLon + (x + 0.5 - width / 2) / pxPerDeg);
      const u = bounds
        ? (lon - bounds.west) / Math.max(1e-9, bounds.east - bounds.west)
        : (lon + 180) / 360;
      if (u >= 0 && u <= 1) {
        uValid[x] = 1;
        sx[x] = Math.min(sourceWidth - 1, Math.floor(u * sourceWidth));
      }
    }
    for (let y = 0; y < height; y += 1) {
      const row = y * width * 4;
      const m = state.mercY - (y + 0.5 - height / 2) / pxPerRad;
      const lat = m < -period / 2 || m > period / 2
        ? NaN : mercatorYToLat(m);
      const v = Number.isFinite(lat)
        ? (bounds
          ? (bounds.north - lat) / Math.max(1e-9, bounds.north - bounds.south)
          : (90 - lat) / 180)
        : -1;
      const sy = v >= 0 && v <= 1
        ? Math.min(sourceHeight - 1, Math.floor(v * sourceHeight)) : -1;
      for (let x = 0; x < width; x += 1) {
        const offset = row + x * 4;
        if (!uValid[x] || sy < 0) {
          out[offset] = background[0];
          out[offset + 1] = background[1];
          out[offset + 2] = background[2];
        } else {
          const sourceOffset = (sy * sourceWidth + sx[x]) * 4;
          out[offset] = Math.min(255, source[sourceOffset] * gain);
          out[offset + 1] = Math.min(255, source[sourceOffset + 1] * gain);
          out[offset + 2] = Math.min(255, source[sourceOffset + 2] * gain);
        }
        out[offset + 3] = 255;
      }
    }
  } else {
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
        const u = bounds
          ? (geo.lon - bounds.west) / Math.max(1e-9, bounds.east - bounds.west)
          : (geo.lon + 180) / 360;
        const v = bounds
          ? (bounds.north - geo.lat) / Math.max(1e-9, bounds.north - bounds.south)
          : (90 - geo.lat) / 180;
        if (u < 0 || u > 1 || v < 0 || v > 1) {
          out[offset] = background[0];
          out[offset + 1] = background[1];
          out[offset + 2] = background[2];
          out[offset + 3] = 255;
          continue;
        }
        const sx = Math.min(sourceWidth - 1, Math.floor(u * sourceWidth));
        const sy = Math.min(sourceHeight - 1, Math.floor(v * sourceHeight));
        const sourceOffset = (sy * sourceWidth + sx) * 4;
        out[offset] = Math.min(255, source[sourceOffset] * gain);
        out[offset + 1] = Math.min(255, source[sourceOffset + 1] * gain);
        out[offset + 2] = Math.min(255, source[sourceOffset + 2] * gain);
        out[offset + 3] = 255;
      }
    }
  }
  const off = document.createElement('canvas');
  off.width = width;
  off.height = height;
  off.getContext('2d').putImageData(image, 0, 0);
  state.projectedBase = { key: cacheKey, canvas: off };
  ctx.drawImage(off, 0, 0);
  drawGlobeOutline();
}

function drawGlobeOutline() {
  if (state.projection !== 'globe') return;
  ctx.save();
  ctx.strokeStyle = 'rgba(160, 190, 235, .28)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(state.width / 2, state.height / 2, globeRadius(), 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

function graticuleStep() {
  // Deliberately much coarser than the old 30°/10°/5° ladder: the grid is an
  // orientation aid, not a second set of landform labels, and it was reading
  // as one.  The globe uses its own magnification, not the 2D tile zoom.
  if (state.projection === 'globe') {
    const zoom = state.globe.zoom;
    return zoom >= 8 ? 10 : zoom >= 4 ? 15 : zoom >= 2 ? 30 : 60;
  }
  return state.zoom >= 5 ? 10 : state.zoom >= 3 ? 30 : 60;
}

/** The parallels worth drawing at a given step, symmetric about the equator. */
function graticuleParallels(step) {
  if (step >= 60) return [-60, 0, 60];
  if (step >= 30) return [-60, -30, 0, 30, 60];
  if (step >= 15) {
    return Array.from({ length: 11 }, (_, index) => -75 + index * 15);
  }
  return Array.from({ length: 17 }, (_, index) => -80 + index * 10);
}

/** Graticule labels: edge ticks on the flat maps, beside the lines on the globe. */
function drawGraticule() {
  if (!state.marksOn || !state.marks.graticule) return;
  const step = graticuleStep();
  const globe = state.projection === 'globe';
  const font = '9px "PingFang SC", system-ui, sans-serif';
  // Neutral grey, not a class colour: the orange grid competed with the
  // landform captions and repeated the warm tones of basins and maria.
  const line = 'rgba(150, 156, 166, .26)';
  const ink = 'rgba(224, 229, 237, .70)';
  ctx.save();
  ctx.font = font;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  const strokeLine = () => {
    // One pass: the old dark underlay plus light core read as two grids.
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    ctx.stroke();
  };
  const label = (text, x, y) => {
    ctx.lineWidth = 2.0;
    ctx.strokeStyle = 'rgba(8, 11, 16, .86)';
    ctx.strokeText(text, x, y);
    ctx.fillStyle = ink;
    ctx.fillText(text, x, y);
  };
  for (let lon = -180; lon <= 180; lon += step) {
    let started = false;
    ctx.beginPath();
    for (let lat = -90; lat <= 90; lat += 5) {
      const p = lonlatToScreen(lon, lat);
      if (p.visible === false) { started = false; continue; }
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    strokeLine();
    if (globe) {
      const p = lonlatToScreen(lon, 0);
      if (p.visible !== false && p.x > -20 && p.x < state.width + 20) {
        label(`${Math.abs(lon)}°${lon < 0 ? 'W' : lon > 0 ? 'E' : ''}`,
          p.x + 3, Math.max(4, p.y + 2));
      }
    } else {
      // Flat maps keep the longitude numbers on the lower frame edge.
      const p = lonlatToScreen(lon, -85);
      if (p.visible !== false && p.x > 12 && p.x < state.width - 12) {
        label(`${Math.abs(lon)}°${lon < 0 ? 'W' : lon > 0 ? 'E' : ''}`,
          p.x + 3, state.height - 14);
      }
    }
  }
  for (const lat of graticuleParallels(step)) {
    let started = false;
    ctx.beginPath();
    for (let lon = -180; lon <= 180; lon += 5) {
      const p = lonlatToScreen(lon, lat);
      if (p.visible === false) { started = false; continue; }
      if (!started) { ctx.moveTo(p.x, p.y); started = true; }
      else ctx.lineTo(p.x, p.y);
    }
    strokeLine();
    if (globe) {
      // On the sphere the parallel is a curve; put its number beside the
      // middle of the visible arc, not on the window frame.
      let at = null;
      let best = Infinity;
      for (let lon = -180; lon <= 180; lon += 4) {
        const p = lonlatToScreen(lon, lat);
        if (p.visible === false) continue;
        if (p.y < -20 || p.y > state.height + 20) continue;
        if (p.x < 24 || p.x > state.width - 24) continue;
        const distance = Math.abs(p.x - state.width / 2);
        if (distance < best) { best = distance; at = p; }
      }
      if (at) {
        label(`${Math.abs(lat)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`,
          at.x + 3, at.y + 2);
      }
    } else {
      const p = lonlatToScreen(-178, lat);
      if (p.visible !== false && p.y > -20 && p.y < state.height + 20) {
        label(`${Math.abs(lat)}°${lat < 0 ? 'S' : lat > 0 ? 'N' : ''}`,
          5, p.y + 2);
      }
    }
  }
  ctx.restore();
}

/* ── features ───────────────────────────────────────────────────────────── */

function activeFeatureClasses() {
  const known = new Set((state.layers && state.layers.feature_classes) || []);
  return [...state.active].filter((key) => known.has(key));
}

/** Avoidance is switched off automatically once any class is over 100%. */
function labelAvoidanceEnabled() {
  if (!state.avoidOverlap) return false;
  return !Object.values(state.density || {}).some((value) => Number(value) > 1);
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
    // A vertical wrap makes the top corner colder than the bottom one.  The
    // server's query is a rectangle, so ask for the whole meridian band and let
    // the painter choose the visible copy.
    if (north < south) { north = 90; south = -90; }
  }
  const query = new URLSearchParams({
    west: String(west), east: String(east),
    south: String(south), north: String(north),
    classes: classes.join(','),
    zoom: String(state.zoom),
    limit: '5000',
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
  const diameter = Math.max(0, Number(feature.diameter_km) || 0);
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
    diameter,
    marker: Math.max(1.7, Math.min(4, radius * 0.5)),
  };
}

function insideScreen(point, margin) {
  const pad = margin || 12;
  return point.x >= -pad && point.x <= state.width + pad
    && point.y >= -pad && point.y <= state.height + pad;
}

/** Font size for the map's names, or the reader's typed value. */
function labelFontPx() {
  const typed = Number(state.labelFont);
  return Number.isFinite(typed) && typed >= 6 ? Math.min(64, typed) : 11;
}

/** A class colour, or one text colour for every name. */
function labelColourFor(feature) {
  if (!state.labelColour) return state.textColor || '#eef2ff';
  return LAYER_COLORS[feature.class] || '#c9d4e6';
}

/** The line dash for 线条. */
function outlineDash() {
  if (state.outlineStyle === 'dash') return [6, 4];
  if (state.outlineStyle === 'dot') return [1.5, 3];
  return [];
}

/** Priority tier for one map caption; see the class constants above. */
function labelTier(feature) {
  const key = feature.class;
  if (LABEL_TIER0.has(key)) return 0;
  if (LABEL_TIER1.has(key)) return 1;
  if (LABEL_TIER2.has(key)) return 2;
  const diameter = Number(feature.diameter_km) || 0;
  if (key === 'crater') {
    const satellite = /\s[A-Z]{1,2}$/.test(String(feature.name_en || ''));
    if (satellite) return diameter >= LARGE_SATELLITE_KM ? 3 : 4;
    return diameter >= LARGE_CRATER_KM ? 2 : 3;
  }
  return 5;
}

function drawFeatures() {
  state.labelHits = [];
  if (!state.labels) return;
  const classes = new Set(activeFeatureClasses());
  if (!classes.size) return;
  const items = [];
  for (const feature of state.features) {
    if (!classes.has(feature.class)) continue;
    const tier = labelTier(feature);
    // Tier 0 is an overlay list: density never hides it.
    if (tier !== 0 && !densityPasses(feature)) continue;
    const pixels = featurePixels(feature);
    if (!insideScreen(pixels.screen, 30)) continue;
    items.push({ feature, ...pixels, tier });
  }
  items.sort((left, right) => left.tier - right.tier
    || right.feature.diameter_km - left.feature.diameter_km);
  for (const item of items) drawFeatureMarker(item);
  const occupied = [];
  // Higher tiers reserve their captions first.  Within a tier, larger features
  // go first and avoid only the captions already placed in that or a higher
  // tier, so one weight class does not erase the rest of its own class.
  for (const item of items) {
    if (item.tier === 0) continue;
    const box = placeFeatureLabel(item, occupied);
    if (!box) continue;
    state.labelHits.push({
      feature: item.feature,
      left: box.left, top: box.top, right: box.right, bottom: box.bottom,
    });
    // Tier 0 does not reserve space; normal labels do.
    if (labelAvoidanceEnabled()) occupied.push(box);
  }
  // Tier 0 overlays after everything else, with no collision test at all.
  for (const item of items) {
    if (item.tier !== 0) continue;
    const box = placeFeatureLabel(item, [], true);
    if (!box) continue;
    state.labelHits.push({
      feature: item.feature,
      left: box.left, top: box.top, right: box.right, bottom: box.bottom,
    });
  }
}

/** The true-diameter circle on the sphere, projected for this map view. */
function featureRimSegments(feature) {
  const diameter = Number(feature.diameter_km) || 0;
  if (!(diameter > 0)) return null;
  const angular = (0.5 * diameter) / 1737.4;
  const centre = unitVector(feature.lon, feature.lat);
  const helper = Math.abs(centre[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const e1raw = [
    centre[1] * helper[2] - centre[2] * helper[1],
    centre[2] * helper[0] - centre[0] * helper[2],
    centre[0] * helper[1] - centre[1] * helper[0],
  ];
  const e1norm = Math.hypot(e1raw[0], e1raw[1], e1raw[2]) || 1;
  const e1 = e1raw.map((value) => value / e1norm);
  const e2 = [
    centre[1] * e1[2] - centre[2] * e1[1],
    centre[2] * e1[0] - centre[0] * e1[2],
    centre[0] * e1[1] - centre[1] * e1[0],
  ];
  const cosR = Math.cos(angular);
  const sinR = Math.sin(angular);
  const globe = state.projection === 'globe';
  // Pick the longitude copy nearest the middle of the view.  For a flat map
  // the rim is then built in unwrapped longitude, so it stays one continuous
  // ring; the half outside the canvas is simply clipped, never mirrored to the
  // other edge as a second half-ring.
  let lon0 = feature.lon;
  if (!globe) {
    // Match the copy the marker and caption use: lonlatToScreen wraps +180
    // to −180, while the raw projection does not.  Pick the raw longitude
    // whose centre lands on that same side of the picture.
    const centreScreen = lonlatToScreen(feature.lon, feature.lat);
    let best = Infinity;
    for (const candidate of [feature.lon - 360, feature.lon, feature.lon + 360]) {
      const point = lonlatToScreenRaw(candidate, feature.lat);
      const distance = Math.abs(point.x - centreScreen.x);
      if (distance < best) {
        best = distance;
        lon0 = candidate;
      }
    }
  }
  const segments = [];
  let current = [];
  let previous = null;
  const finish = () => { if (current.length >= 2) segments.push(current); current = []; };
  for (let index = 0; index <= 72; index += 1) {
    const theta = 2 * Math.PI * (index % 72) / 72;
    const c = Math.cos(theta);
    const s = Math.sin(theta);
    const v = [
      cosR * centre[0] + sinR * (c * e1[0] + s * e2[0]),
      cosR * centre[1] + sinR * (c * e1[1] + s * e2[1]),
      cosR * centre[2] + sinR * (c * e1[2] + s * e2[2]),
    ];
    let lon = Math.atan2(v[1], v[0]) * 180 / Math.PI;
    const lat = Math.asin(Math.max(-1, Math.min(1, v[2]))) * 180 / Math.PI;
    if (!globe) {
      while (lon - lon0 > 180) lon -= 360;
      while (lon - lon0 < -180) lon += 360;
    }
    const p = globe ? lonlatToScreen(lon, lat) : lonlatToScreenRaw(lon, lat);
    if (p.visible === false) { finish(); previous = null; continue; }
    // Only the sphere can hide a point behind the limb; on a flat map the raw
    // projection deliberately keeps points outside the frame instead.
    if (globe && previous && Math.abs(p.x - previous.x) > state.width * 0.5) {
      finish();
    }
    current.push(p);
    previous = p;
  }
  finish();
  return segments.length ? { segments, closed: segments.length === 1 } : null;
}

function drawFeatureMarker(item) {
  const { feature, screen, radius } = item;
  const colour = labelColourFor(feature);
  const fontPx = labelFontPx();
  let rimDrawn = false;
  const highlight = state.selected === feature;
  ctx.save();
  // 真实直径: only the classes whose edge is a real, measurable rim get this
  // circle.  A point feature keeps its dot/cross.
  if (state.diameters && RIM_CLASSES.has(feature.class)
      && radius > 3 && state.outlineStyle !== 'none') {
    const rim = featureRimSegments(feature);
    if (rim) {
      ctx.strokeStyle = state.outlineColor || colour;
      ctx.globalAlpha = highlight ? 0.95 : 0.62;
      ctx.lineWidth = Math.max(0.6, Number(state.outlineWidth) || 1)
        + (highlight ? 1.0 : 0);
      ctx.setLineDash(outlineDash());
      ctx.beginPath();
      for (const segment of rim.segments) {
        ctx.moveTo(segment[0].x, segment[0].y);
        for (let index = 1; index < segment.length; index += 1) {
          ctx.lineTo(segment[index].x, segment[index].y);
        }
        if (rim.closed) ctx.closePath();
      }
      ctx.stroke();
      ctx.setLineDash([]);
      rimDrawn = true;
    }
  }
  if (feature.class === 'statio') {
    // A landing site is a crosshair, like the full-disk page: the diamond was
    // the old "point class" marker and read as a measured shape it never had.
    const half = Math.max(4, fontPx * 0.3) * (highlight ? 1.35 : 1);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 1;
    ctx.lineWidth = Math.max(1, fontPx * 0.06) + (highlight ? 1 : 0);
    ctx.shadowColor = highlight ? colour : 'transparent';
    ctx.shadowBlur = highlight ? 7 : 0;
    ctx.beginPath();
    ctx.moveTo(screen.x - half, screen.y);
    ctx.lineTo(screen.x + half, screen.y);
    ctx.moveTo(screen.x, screen.y - half);
    ctx.lineTo(screen.x, screen.y + half);
    ctx.stroke();
    ctx.restore();
    return;
  }
  if (rimDrawn || REGION_LABEL_CLASSES.has(feature.class)) {
    ctx.restore();
    return;
  }
  ctx.beginPath();
  ctx.arc(screen.x, screen.y, item.marker * (highlight ? 1.8 : 1), 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.globalAlpha = 1;
  ctx.shadowColor = highlight ? colour : 'transparent';
  ctx.shadowBlur = highlight ? 7 : 0;
  ctx.fill();
  const markerStroke = Number(state.markerStrokeWidth) || 0;
  if (markerStroke >= 0.6) {
    ctx.globalAlpha = 0.9;
    ctx.lineWidth = Math.min(3, markerStroke) + (highlight ? 0.8 : 0);
    ctx.strokeStyle = state.markerStroke || '#030509';
    ctx.stroke();
  }
  ctx.restore();
}

function placeFeatureLabel(item, occupied, force = false) {
  const { feature, screen, radius } = item;
  const colour = labelColourFor(feature);
  const text = featureName(feature);
  if (!text) return null;
  const pointVisible = POINT_CLASSES.has(feature.class) && state.zoom >= 2;
  const visible = force || state.zoom >= 3 || feature.diameter_km >= 45
    || pointVisible;
  if (!visible) return null;
  const fontPx = labelFontPx() + (feature.class === 'mare' ? 2 : 0);
  const highlight = state.selected === feature;
  const bold = (state.textBold || highlight) ? '600 ' : '';
  ctx.save();
  ctx.font = `${bold}${fontPx}px "PingFang SC", system-ui, sans-serif`;
  ctx.textBaseline = 'middle';
  const width = ctx.measureText(text).width;
  const height = fontPx * 1.35;
  // A name sits just outside the marker, or outside the true-diameter ring
  // when that ring is actually drawn.  Using the *apparent* radius when the
  // ring is off pushed names of large maria a whole feature away from their
  // point.
  const ringDrawn = state.diameters && radius > 3 && state.outlineStyle !== 'none';
  const gap = ringDrawn ? radius : Math.max(4, item.marker || 4);
  const attempts = REGION_LABEL_CLASSES.has(feature.class)
    ? [
      { x: screen.x, y: screen.y, align: 'center' },
      { x: screen.x, y: screen.y - height * 0.9, align: 'center' },
      { x: screen.x, y: screen.y + height * 0.9, align: 'center' },
      { x: screen.x + gap + 6, y: screen.y, align: 'left' },
    ]
    : [
      { x: screen.x + gap + 6, y: screen.y, align: 'left' },
      { x: screen.x - gap - 6, y: screen.y, align: 'right' },
      { x: screen.x, y: screen.y - gap - height * 0.7, align: 'center' },
      { x: screen.x, y: screen.y + gap + height * 0.7, align: 'center' },
    ];
  for (const attempt of attempts) {
    const left = attempt.align === 'left' ? attempt.x
      : attempt.align === 'right' ? attempt.x - width : attempt.x - width / 2;
    const box = { left: left - 2, top: attempt.y - height / 2,
                  right: left + width + 2, bottom: attempt.y + height / 2 };
    if (box.left < 2 || box.right > state.width - 2
        || box.top < 2 || box.bottom > state.height - 2) continue;
    if (!force && labelAvoidanceEnabled()) {
      const collision = occupied.some((other) => !(box.right < other.left
        || box.left > other.right || box.bottom < other.top || box.top > other.bottom));
      if (collision) continue;
    }
    ctx.textAlign = attempt.align;
    const strokeWidth = Number(state.textStrokeWidth) || 0;
    if (strokeWidth > 0) {
      ctx.lineWidth = Math.min(6, strokeWidth);
      ctx.strokeStyle = state.textStroke || 'rgba(3, 5, 9, .92)';
      ctx.strokeText(text, attempt.x, attempt.y);
    }
    ctx.fillStyle = highlight ? '#ffffff' : colour;
    if (highlight) {
      ctx.shadowColor = colour;
      ctx.shadowBlur = 8;
    }
    ctx.fillText(text, attempt.x, attempt.y);
    ctx.restore();
    return box;
  }
  if (force) {
    // The overlay list may have nowhere collision-free; clamp a centered
    // caption into the frame anyway, because the whole point is all of them.
    const left = Math.min(Math.max(2, screen.x - width / 2), state.width - width - 2);
    const top = Math.min(Math.max(2, screen.y - height / 2), state.height - height - 2);
    ctx.textAlign = 'left';
    const strokeWidth = Number(state.textStrokeWidth) || 0;
    if (strokeWidth > 0) {
      ctx.lineWidth = Math.min(6, strokeWidth);
      ctx.strokeStyle = state.textStroke || 'rgba(3, 5, 9, .92)';
      ctx.strokeText(text, left, top + height / 2);
    }
    ctx.fillStyle = colour;
    ctx.fillText(text, left, top + height / 2);
    ctx.restore();
    return { left, top, right: left + width, bottom: top + height };
  }
  ctx.restore();
  return null;
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
  if (best) return best;
  for (let index = state.labelHits.length - 1; index >= 0; index -= 1) {
    const hit = state.labelHits[index];
    if (sx >= hit.left - 2 && sx <= hit.right + 2
        && sy >= hit.top - 2 && sy <= hit.bottom + 2) {
      return hit.feature;
    }
  }
  return null;
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
  state.active.clear();
  // The raster is one layer and one switch; there is no layer list left to
  // build.  The feature classes still come from the server.
  if (!$('base-on') || $('base-on').checked) state.active.add('base');
  buildClassChips(payload);
  updateLayerHint();
  render();
  refreshFeatures();
}

/** The class switches, in the top bar: the full-disk page's chips. */
function buildClassChips(payload) {
  const host = $('label-classes');
  if (!host) return;
  host.innerHTML = '';
  for (const layer of payload.layers || []) {
    if (layer.type !== 'features') continue;
    if (!(layer.id in state.density)) {
      state.density[layer.id] = Number($('label-density') && $('label-density').value) || 1;
    }
    const chip = document.createElement('label');
    chip.className = 'cls-chip';
    chip.title = `${layerLabel(layer)}${layer.count !== undefined
      ? ` · ${layer.count} 个` : ''}`;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = Boolean(layer.default);
    input.dataset.layer = layer.id;
    if (input.checked) state.active.add(layer.id);
    const swatch = document.createElement('i');
    swatch.style.background = layerColour(layer);
    const text = document.createElement('span');
    text.textContent = layerLabel(layer);
    chip.appendChild(input);
    chip.appendChild(swatch);
    chip.appendChild(text);
    if (layer.count !== undefined) {
      const count = document.createElement('small');
      count.textContent = layer.count.toLocaleString();
      chip.appendChild(count);
    }
    chip.dataset.on = String(input.checked);
    input.addEventListener('change', () => {
      chip.dataset.on = String(input.checked);
      if (input.checked) state.active.add(layer.id);
      else state.active.delete(layer.id);
      updateLayerHint();
      scheduleFeatures();
      render();
    });
    host.appendChild(chip);
  }
  updateLayerHint();
}

function updateLayerHint() {
  // Kept as the one place the class count would be shown; the layer list that
  // used to carry it is gone, and the header already states the count.
}

function refreshLayerLanguage() {
  for (const input of document.querySelectorAll('#label-classes input[data-layer]')) {
    const layer = (state.layers && state.layers.layers || [])
      .find((item) => item.id === input.dataset.layer);
    if (!layer) continue;
    const chip = input.closest('.cls-chip');
    if (!chip) continue;
    const text = chip.querySelector('span');
    if (text) text.textContent = layerLabel(layer);
    chip.title = `${layerLabel(layer)}${layer.count !== undefined
      ? ` · ${layer.count} 个` : ''}`;
  }
  if (state.selected) selectFeature(state.selected);
  else render();
}

function setAllLayers(on) {
  for (const input of document.querySelectorAll('#label-classes input[data-layer]')) {
    input.checked = on;
    const chip = input.closest('.cls-chip');
    if (chip) chip.dataset.on = String(on);
    if (on) state.active.add(input.dataset.layer);
    else state.active.delete(input.dataset.layer);
  }
  updateLayerHint();
  scheduleFeatures();
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
  if ($('map-version') && health.version) {
    $('map-version').textContent = String(health.version);
  }
  const dem = health.dem || {};
  const albedo = health.albedo || {};
  const detail = health.detail_dem || null;
  $('fact-dem').textContent = dem.name
    ? `${dem.name} · ${formatKm(dem.level0_km_per_pixel)}`
      + (detail && detail.name
        ? `　高倍 ${detail.name} · ${formatKm(detail.km_per_px)}` : '')
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

/* ── manual anchors ────────────────────────────────────────────────────────
 *
 * Ctrl/⌘-click drops a numbered anchor, exactly like the full-disk page.  The
 * caption is the reader's: double-click renames it, dragging moves it.  The
 * anchors are stored by lon/lat, so they stay on the ground through pans and
 * projection changes.
 */
const MANUAL_COLOUR = '#ff5a52';
const MANUAL_LIMIT = 10;

function drawManualMarks() {
  state.manualLayout = [];
  if (!state.labels || !state.manualMarks.length) return;
  const fontPx = labelFontPx();
  ctx.save();
  ctx.font = `${fontPx}px "PingFang SC", system-ui, sans-serif`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  for (const mark of state.manualMarks) {
    if (!Number.isFinite(mark.lon) || !Number.isFinite(mark.lat)) continue;
    const point = lonlatToScreen(mark.lon, mark.lat);
    if (point.visible === false) continue;
    if (!insideScreen(point, 40)) continue;
    const radius = Math.max(2.2, fontPx * 0.15);
    const width = Math.max(fontPx * 1.4, ctx.measureText(mark.text).width) + 7;
    const height = fontPx * 1.35;
    let labelX = point.x + radius + 5;
    if (labelX + width > state.width - 2) {
      labelX = point.x - radius - 5 - width;
    }
    labelX = Math.max(2, Math.min(state.width - width - 2, labelX));
    let labelY = point.y - height / 2;
    labelY = Math.max(2, Math.min(state.height - height - 2, labelY));
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = MANUAL_COLOUR;
    ctx.fill();
    const markerStroke = Number(state.markerStrokeWidth) || 0;
    if (markerStroke >= 0.6) {
      ctx.lineWidth = markerStroke;
      ctx.strokeStyle = state.markerStroke || '#030509';
      ctx.stroke();
    }
    ctx.lineWidth = Math.max(1.4, fontPx * 0.22);
    ctx.strokeStyle = 'rgba(12, 14, 20, .9)';
    ctx.strokeText(mark.text, labelX + 3, labelY + height / 2);
    ctx.fillStyle = MANUAL_COLOUR;
    ctx.fillText(mark.text, labelX + 3, labelY + height / 2);
    state.manualLayout.push({
      mark, x: point.x, y: point.y, radius,
      left: labelX, top: labelY, right: labelX + width, bottom: labelY + height,
    });
  }
  ctx.restore();
}

function manualAt(sx, sy) {
  for (let index = state.manualLayout.length - 1; index >= 0; index -= 1) {
    const item = state.manualLayout[index];
    if (Math.hypot(sx - item.x, sy - item.y) <= item.radius + 5) return item;
    if (sx >= item.left - 2 && sx <= item.right + 2
        && sy >= item.top - 2 && sy <= item.bottom + 2) return item;
  }
  return null;
}

function addManualMarkAt(geo) {
  if (!state.labels) {
    setStatus('先打开月面地貌标注，再添加手动标记', 'error');
    return null;
  }
  if (!geo) {
    setStatus('这里不在月面上，取不到经纬度', 'error');
    return null;
  }
  if (state.manualMarks.length >= MANUAL_LIMIT) {
    setStatus(`手动标记最多 ${MANUAL_LIMIT} 个`, 'error');
    return null;
  }
  const used = new Set(state.manualMarks.map((mark) => mark.number));
  let number = 1;
  while (used.has(number)) number += 1;
  state.manualSeq += 1;
  const mark = {
    id: state.manualSeq, number, lon: geo.lon, lat: geo.lat,
    text: `标记点${number}`,
  };
  state.manualMarks.push(mark);
  updateManualCount();
  render();
  setStatus(`已添加手动标记：${formatCoord(mark.lon, mark.lat)}`);
  return mark;
}

function updateManualCount() {
  const el = $('manual-count');
  if (el) el.textContent = state.manualMarks.length
    ? `${state.manualMarks.length}/${MANUAL_LIMIT}` : '';
}

function clearManualMarks() {
  if (!state.manualMarks.length) {
    setStatus('还没有手动标记');
    return;
  }
  const count = state.manualMarks.length;
  state.manualMarks = [];
  state.manualSeq = 0;
  updateManualCount();
  render();
  setStatus(`已清空 ${count} 个手动标记`);
}

function renameManualMark(item) {
  const next = window.prompt ? window.prompt('手动标记的文字', item.mark.text) : null;
  if (next === null || next === undefined) return;
  const text = String(next).trim().slice(0, 40);
  if (!text) {
    setStatus('标记文字不能为空', 'error');
    return;
  }
  item.mark.text = text;
  render();
  setStatus(`手动标记已改名为「${text}」`);
}

/* ── interaction ────────────────────────────────────────────────────────── */

function stagePoint(event) {
  const rect = stage.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

stage.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  if (event.target && event.target.closest
      && event.target.closest('button')) return;
  const point = stagePoint(event);
  if (event.ctrlKey || event.metaKey) {
    addManualMarkAt(screenToLonLat(point.x, point.y));
    event.preventDefault();
    return;
  }
  const manual = manualAt(point.x, point.y);
  if (manual) {
    state.manualDrag = { id: manual.mark.id };
    stage.setPointerCapture(event.pointerId);
    setStatus(`${manual.mark.text}：${formatCoord(manual.mark.lon, manual.mark.lat)}`);
    event.preventDefault();
    return;
  }
  const hit = featureAt(point.x, point.y);
  if (hit) {
    selectFeature(hit);
    event.preventDefault();
    return;
  }
  const dragGeo = screenToLonLat(point.x, point.y);
  state.drag = {
    x: event.clientX, y: event.clientY,
    lon: state.centerLon, lat: state.centerLat, mercY: state.mercY,
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
  if (state.manualDrag) {
    const geo = screenToLonLat(point.x, point.y);
    if (geo) {
      const mark = state.manualMarks.find((item) => item.id === state.manualDrag.id);
      if (mark) {
        mark.lon = geo.lon;
        mark.lat = geo.lat;
        updatePositionReadout(geo.lon, geo.lat, point);
        render();
        setStatus(`${mark.text}：${formatCoord(mark.lon, mark.lat)}`);
      }
    }
    event.preventDefault();
    return;
  }
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
      const world = worldSize();
      const pxPerDeg = (world.w / 360) * state.scale;
      const pxPerRad = pxPerDeg * 180 / Math.PI;
      state.centerLon = wrapLon(state.drag.lon - dx / pxPerDeg);
      state.centerLat = clamp(
        mercatorYToLat(state.drag.mercY + dy / pxPerRad), -85, 85);
      state.mercY = mercatorY(state.centerLat);
    } else {
      const world = worldSize();
      state.centerLon = wrapLon(
        state.drag.lon - dx / state.scale / world.w * 360);
      state.centerLat = clamp(
        state.drag.lat + dy / state.scale / world.h * 180, -90, 90);
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
  if (state.manualDrag) {
    state.manualDrag = null;
    if (event && event.pointerId !== undefined) {
      try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* noop */ }
    }
    render();
    return;
  }
  if (!state.drag) return;
  state.drag = null;
  stage.classList.remove('dragging');
  if (event && event.pointerId !== undefined) {
    try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* noop */ }
  }
  render();
  scheduleFeatures();
}

stage.addEventListener('pointerup', endDrag);
stage.addEventListener('pointercancel', endDrag);
stage.addEventListener('pointerleave', () => {
  hideTooltip();
  $('map-coords').textContent = '—';
});

stage.addEventListener('dblclick', (event) => {
  const point = stagePoint(event);
  const manual = manualAt(point.x, point.y);
  if (manual) {
    renameManualMark(manual);
    return;
  }
  const feature = featureAt(point.x, point.y);
  if (!feature) return;
  const text = `${featureName(feature)}　${formatCoord(feature.lon, feature.lat)}`;
  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    setStatus('剪贴板不可用', 'error');
    return;
  }
  navigator.clipboard.writeText(text)
    .then(() => setStatus(`已复制：${text}`))
    .catch(() => setStatus('复制失败', 'error'));
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
  if (Number.isFinite(scale) && scale > 0) state.scale = clamp(scale, 0.18, 16);
  return true;
}

/* ── wiring ─────────────────────────────────────────────────────────────── */

/** The geometric-overlay switches, read once and then kept in state. */
function readMarks() {
  const read = (id, fallback) => {
    const input = $(id);
    return input ? input.checked : fallback;
  };
  state.marks = {
    terminator: read('mark-terminator', true),
    poles: read('mark-poles', true),
    equator: read('mark-equator', true),
    subsolar: read('mark-subsolar', true),
    libration: read('mark-libration', true),
    graticule: read('mark-graticule', true),
  };
}

function syncMarkChips() {
  for (const input of document.querySelectorAll('.mark-chip input')) {
    const chip = input.closest('.mark-chip');
    if (chip) chip.dataset.on = String(input.checked);
  }
}

/** Re-ask the light with a short delay: dragging a number is one request. */
let lightingTimer = null;
function applyLightingSoon(immediate) {
  if (lightingTimer) clearTimeout(lightingTimer);
  const run = () => {
    lightingTimer = null;
    loadLighting().catch((error) => {
      setStatus('光照计算失败：' + String(error.message || error));
    });
  };
  if (immediate) run();
  else lightingTimer = setTimeout(run, 220);
}

/** Move the instant by one step of the nudge ladder (-1d, +15m, …). */
function nudgeLighting(step) {
  const match = /^([+-])(\d+)([mhdw])$/.exec(String(step));
  if (!match) return;
  const when = lightingWhen();
  if (!when) return;
  const unit = match[3] === 'm' ? 60
    : match[3] === 'h' ? 3600
      : match[3] === 'd' ? 86400 : 604800;
  const delta = Number(match[2]) * (match[1] === '-' ? -1 : 1) * unit * 1000;
  const local = new Date(Date.parse(when) + delta + lightZoneOffset() * 3600 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  $('light-when').value = `${local.getUTCFullYear()}-`
    + `${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`
    + `T${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}`;
  applyLightingSoon(true);
}

/** Drag a column edge: the two panels are the reader's, not a fixed grid. */
function splitterPanel(target) {
  return target === 'right' ? $('map-right') : $('map-left');
}

/** Collapse or restore one side panel; 0 width is a hidden panel. */
function setPanelCollapsed(target, hidden, width) {
  const panel = splitterPanel(target);
  const variable = target === 'right' ? '--right-w' : '--left-w';
  const fallback = target === 'right' ? 286 : 250;
  const root = document.documentElement;
  if (panel) panel.dataset.collapsed = String(!!hidden);
  root.style.setProperty(variable,
    hidden ? '0px' : `${Math.round(width || fallback)}px`);
  syncCollapseHandle(target);
}

/** Give a collapsed edge a chevron handle; restore it with click or Enter. */
function syncCollapseHandle(target) {
  const panel = splitterPanel(target);
  const collapsed = !!(panel && panel.dataset.collapsed === 'true');
  for (const splitter of document.querySelectorAll('.map-splitter')) {
    if (splitter.dataset.target !== target) continue;
    splitter.dataset.collapsed = String(collapsed);
    splitter.title = collapsed
      ? `点击显示${target === 'right' ? '右栏' : '左栏'}`
      : `拖动调整${target === 'right' ? '右栏' : '左栏'}宽度`;
    splitter.setAttribute('role', collapsed ? 'button' : 'separator');
    splitter.setAttribute('tabindex', collapsed ? '0' : '-1');
  }
}

function wireSplitters() {
  for (const splitter of document.querySelectorAll('.map-splitter')) {
    splitter.addEventListener('pointerdown', (event) => {
      const target = splitter.dataset.target === 'right' ? 'right' : 'left';
      const variable = target === 'right' ? '--right-w' : '--left-w';
      const panel = splitterPanel(target);
      const root = document.documentElement;
      const wasCollapsed = panel && panel.dataset.collapsed === 'true';
      const fallback = target === 'right' ? 286 : 250;
      const parsed = parseFloat(getComputedStyle(root).getPropertyValue(variable));
      const startWidth = wasCollapsed ? fallback
        : (Number.isFinite(parsed) ? parsed : fallback);
      if (wasCollapsed && panel) {
        // A click on the edge handle is enough to bring a hidden column back;
        // requiring a 170-pixel drag from a five-pixel strip was the reason
        // the collapsed panels looked unrecoverable.
        panel.dataset.collapsed = 'false';
        root.style.setProperty(variable, `${fallback}px`);
        syncCollapseHandle(target);
      }
      const startX = event.clientX;
      const limit = Math.max(200, window.innerWidth - 420);
      splitter.classList.add('dragging');
      try { splitter.setPointerCapture(event.pointerId); } catch (error) { /* noop */ }
      const move = (moveEvent) => {
        const delta = moveEvent.clientX - startX;
        const raw = target === 'left' ? startWidth + delta : startWidth - delta;
        if (raw < 90) {
          // Dragging past the edge is how a side panel is put away.
          if (panel) panel.dataset.collapsed = 'true';
          root.style.setProperty(variable, '0px');
          syncCollapseHandle(target);
          return;
        }
        if (panel) panel.dataset.collapsed = 'false';
        root.style.setProperty(variable,
          `${Math.round(clamp(raw, 170, limit))}px`);
        syncCollapseHandle(target);
      };
      const up = () => {
        splitter.classList.remove('dragging');
        splitter.removeEventListener('pointermove', move);
        splitter.removeEventListener('pointerup', up);
        resizeCanvas();
      };
      splitter.addEventListener('pointermove', move);
      splitter.addEventListener('pointerup', up);
      splitter.addEventListener('keydown', (keyEvent) => {
        if (keyEvent.key !== 'Enter' && keyEvent.key !== ' ') return;
        if (panel && panel.dataset.collapsed === 'true') {
          setPanelCollapsed(target, false, target === 'right' ? 286 : 250);
          resizeCanvas();
          keyEvent.preventDefault();
        }
      });
      event.preventDefault();
    });
    syncCollapseHandle(splitter.dataset.target === 'right' ? 'right' : 'left');
  }
}

/** The hemisphere letter beside the map's observer fields follows the sign. */
function updateMapHemispheres() {
  const lat = Number($('light-lat') && $('light-lat').value);
  const lon = Number($('light-lon') && $('light-lon').value);
  if ($('map-lat-hemi')) $('map-lat-hemi').textContent = lat < 0 ? 'S' : 'N';
  if ($('map-lon-hemi')) $('map-lon-hemi').textContent = lon < 0 ? 'W' : 'E';
}

/** Hide every piece of page chrome except the map stage itself. */
function setMapFullscreen(on) {
  state.fullscreen = !!on;
  if (document.body) document.body.classList.toggle('map-fullscreen', state.fullscreen);
  if ($('map-exit-fullscreen')) $('map-exit-fullscreen').hidden = !state.fullscreen;
  if ($('map-fullscreen')) {
    $('map-fullscreen').setAttribute('aria-pressed', String(state.fullscreen));
  }
  // The stage's rectangle changes with the chrome, so the canvas and the
  // projection both have to be measured again.
  resizeCanvas();
  render();
  scheduleFeatures();
  if (state.fullscreen) setStatus('全屏地图');
}

function wire() {
  $('zoom-in').addEventListener('click', () => zoomBy(1.6));
  $('zoom-out').addEventListener('click', () => zoomBy(1 / 1.6));
  $('fit').addEventListener('click', fitView);
  if ($('manual-clear')) {
    $('manual-clear').addEventListener('click', clearManualMarks);
  }
  updateManualCount();
  if ($('map-fullscreen')) {
    $('map-fullscreen').addEventListener('click', () => setMapFullscreen(true));
  }
  if ($('map-exit-fullscreen')) {
    $('map-exit-fullscreen').addEventListener('click', () => setMapFullscreen(false));
  }
  $('search-form').addEventListener('submit', runSearch);
  // Search-as-you-type: the catalogue is small and already in memory on the
  // server, so waiting for Enter made a working search look broken.  An empty
  // box closes the list; one character is enough to ask.
  let searchTimer = null;
  let composing = false;
  const searchSoon = () => {
    clearTimeout(searchTimer);
    const text = $('search-input').value.trim();
    if (!text) {
      $('search-results').hidden = true;
      return;
    }
    searchTimer = setTimeout(() => runSearch(), 180);
  };
  $('search-input').addEventListener('compositionstart', () => { composing = true; });
  $('search-input').addEventListener('compositionend', () => {
    composing = false;
    searchSoon();
  });
  $('search-input').addEventListener('input', () => {
    // An IME's intermediate pinyin is not a query: wait for the committed
    // characters, or a Chinese search fires a burst of misses first.
    if (!composing) searchSoon();
  });
  $('search-input').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') $('search-results').hidden = true;
  });
  $('classes-all').addEventListener('click', () => setAllLayers(true));
  $('classes-none').addEventListener('click', () => setAllLayers(false));
  const setLabels = (on) => {
    state.labels = on;
    $('labels-toggle').setAttribute('aria-pressed', String(on));
    render();
    updateFeatureCount();
  };
  $('labels-toggle').addEventListener('click', () => setLabels(!state.labels));
  const setMarksOn = (on) => {
    state.marksOn = !!on;
    $('marks-toggle').setAttribute('aria-pressed', String(state.marksOn));
    const panel = $('marks-toggle').closest('.geometry-panel');
    if (panel) panel.dataset.on = String(state.marksOn);
    render();
  };
  $('marks-toggle').addEventListener('click', () => setMarksOn(!state.marksOn));
  $('marks-toggle').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setMarksOn(!state.marksOn);
    }
  });
  setMarksOn(true);
  if ($('moon-info-toggle')) {
    const setMoonInfoOpen = (open) => {
      const panel = $('moon-info');
      if (panel) panel.dataset.collapsed = String(!open);
      $('moon-info-toggle').setAttribute('aria-expanded', String(!!open));
    };
    const toggleMoonInfo = () => {
      const panel = $('moon-info');
      setMoonInfoOpen(panel && panel.dataset.collapsed === 'true');
    };
    $('moon-info-toggle').addEventListener('click', (event) => {
      event.stopPropagation();
      toggleMoonInfo();
    });
    $('moon-info-heading').addEventListener('click', toggleMoonInfo);
    $('moon-info-heading').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      toggleMoonInfo();
    });
    setMoonInfoOpen(false);
  }
  if ($('label-density')) {
    const density = $('label-density');
    const applyDensity = () => {
      const value = Number(density.value);
      if ($('density-value')) {
        $('density-value').textContent = `${Math.round(value * 100)}%`;
      }
      for (const layer of (state.layers && state.layers.layers) || []) {
        if (layer.type === 'features') state.density[layer.id] = value;
      }
      render();
    };
    // 100% is a detent: one rightward drag stops there, and a second drag is
    // needed to pass it.  Above 100% the labels stop avoiding each other.
    let densityDetentSeen = false;
    let densityLocked = false;
    density.addEventListener('pointerdown', () => { densityLocked = false; });
    density.addEventListener('input', () => {
      let value = Number(density.value);
      if (value > 1 && !densityDetentSeen) {
        // The first attempt to cross 100% stops on the line.  The flag is not
        // reset by that release: the next deliberate drag is allowed through.
        value = 1;
        densityDetentSeen = true;
        densityLocked = true;
      } else if (value > 1 && densityLocked) {
        value = 1;
      }
      if (value !== Number(density.value)) density.value = String(value);
      applyDensity();
    });
    density.addEventListener('change', () => {
      densityLocked = false;
      applyDensity();
    });
    applyDensity();
  }
  $('classes-toggle').addEventListener('click', () => {
    const bar = $('map-label-bar');
    if (!bar) return;
    const collapsed = bar.dataset.collapsed === 'true';
    bar.dataset.collapsed = String(!collapsed);
    $('classes-toggle').setAttribute('aria-expanded', String(collapsed));
  });
  $('name-language').addEventListener('change', (event) => {
    state.language = event.target.value === 'en' ? 'en' : 'zh';
    refreshLayerLanguage();
  });
  // 标注外观: the same handful of choices the full-disk page exposes.  Every
  // one of them is a redraw, not a re-fetch: the catalogue did not change.
  const styleControls = [
    ['label-font', 'labelFont', 'number'],
    ['label-colour', 'labelColour', 'check'],
    ['text-color', 'textColor', 'text'],
    ['text-stroke', 'textStroke', 'text'],
    ['text-stroke-width', 'textStrokeWidth', 'number'],
    ['marker-stroke', 'markerStroke', 'text'],
    ['marker-stroke-width', 'markerStrokeWidth', 'number'],
    ['text-bold', 'textBold', 'check'],
    ['outline-color', 'outlineColor', 'text'],
    ['outline-width', 'outlineWidth', 'number'],
    ['outline-style', 'outlineStyle', 'text'],
    ['label-overlap', 'avoidOverlap', 'avoid'],
  ];
  for (const [id, key, kind] of styleControls) {
    const input = $(id);
    if (!input) continue;
    const apply = () => {
      if (kind === 'check') state[key] = !!input.checked;
      else if (kind === 'avoid') state.avoidOverlap = !input.checked;
      else if (kind === 'number') {
        const value = Number(input.value);
        state[key] = Number.isFinite(value) && value > 0 ? value : 0;
      } else state[key] = String(input.value || '');
      render();
    };
    input.addEventListener('input', apply);
    input.addEventListener('change', apply);
  }
  $('projection').addEventListener('change', (event) => {
    state.projection = ['mercator', 'globe'].includes(event.target.value)
      ? event.target.value : 'equirect';
    // A projection change reinterprets the camera's vertical coordinate.
    // Mercator stores it as a log tangent, so the shared centre has to be
    // carried into that axis before the first frame is drawn.
    if (state.projection === 'mercator') {
      state.mercY = mercatorY(clamp(state.centerLat, -85, 85));
    }
    clampCenter();
    state.tiles.clear();
    state.sourceCanvas = null;
    state.sourcePixels = null;
    state.sourceKey = '';
    state.sourceReadyKey = '';
    render();
    scheduleFeatures();
  });

  // Observation: every edit is the request; there is no apply button left.
  updateMapHemispheres();
  for (const id of ['light-when', 'light-tz', 'light-lat', 'light-lon']) {
    const input = $(id);
    if (!input) continue;
    input.addEventListener('change', () => {
      updateMapHemispheres();
      applyLightingSoon(true);
    });
    input.addEventListener('input', () => {
      updateMapHemispheres();
      applyLightingSoon(false);
    });
  }
  for (const button of document.querySelectorAll('#light-nudges [data-nudge]')) {
    button.addEventListener('click', () => nudgeLighting(button.dataset.nudge));
  }
  $('light-now').addEventListener('click', () => {
    setLightNow();
    applyLightingSoon(true);
  });
  $('light-from-map').addEventListener('click', () => {
    $('light-lat').value = state.centerLat.toFixed(3);
    $('light-lon').value = state.centerLon.toFixed(3);
    updateMapHemispheres();
    applyLightingSoon(true);
  });

  $('light-on').addEventListener('change', () => {
    const chip = $('light-on').closest('.switch-chip');
    if (chip) chip.dataset.on = String($('light-on').checked);
    if ($('light-on').checked) {
      applyLightingSoon(true);
    } else {
      state.lightingOn = false;
      state.lightingKey = '';
      state.tiles.clear();
      state.sourceCanvas = null;
      state.sourcePixels = null;
      state.sourceKey = '';
      state.sourceReadyKey = '';
      render();
    }
  });

  for (const input of document.querySelectorAll('.mark-chip input')) {
    input.addEventListener('change', () => {
      readMarks();
      syncMarkChips();
      render();
    });
  }
  readMarks();
  syncMarkChips();
  wireSplitters();
  wirePhotoLibrary();
  wireRightTabs();

  if ($('light-tz')) {
    $('light-tz').value = String(-new Date().getTimezoneOffset() / 60);
  }
  setLightNow();
  applyLightingSoon(true);
  $('diameter-toggle').addEventListener('change', (event) => {
    state.diameters = event.target.checked;
    render();
  });
  if ($('base-on')) {
    $('base-on').addEventListener('change', () => {
      const label = $('base-on').closest('.switch-chip');
      if (label) label.dataset.on = String($('base-on').checked);
      if ($('base-on').checked) state.active.add('base');
      else state.active.delete('base');
      render();
    });
  }
  if ($('base-brightness')) {
    $('base-brightness').addEventListener('input', (event) => {
      state.brightness = Number(event.target.value) || 1;
      render();
    });
  }
  if ($('earthshine-on')) {
    $('earthshine-on').addEventListener('change', () => {
      const label = $('earthshine-on').closest('.switch-chip');
      if (label) label.dataset.on = String($('earthshine-on').checked);
      state.earthshine = $('earthshine-on').checked;
      clearBaseCaches();
      render();
    });
  }
  if ($('shadow-bias')) {
    let shadowTimer = null;
    const applyShadow = () => {
      state.shadowBias = Number($('shadow-bias').value) || 0;
      if (shadowTimer) clearTimeout(shadowTimer);
      shadowTimer = setTimeout(() => {
        shadowTimer = null;
        clearBaseCaches();
        render();
      }, 180);
    };
    $('shadow-bias').addEventListener('input', applyShadow);
    $('shadow-bias').addEventListener('change', applyShadow);
  }
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
    else if (event.key === 'Escape') {
      $('search-results').hidden = true;
      if (state.fullscreen) setMapFullscreen(false);
    }
  });
  window.addEventListener('resize', () => { resizeCanvas(); syncSplitters(); });
  if (window.ResizeObserver) {
    new ResizeObserver(() => resizeCanvas()).observe(stage);
  }
}

function syncSplitters() {
  const root = document.documentElement;
  const limit = Math.max(200, window.innerWidth - 420);
  for (const [variable, target, fallback] of [
    ['--left-w', 'left', 250], ['--right-w', 'right', 286],
  ]) {
    const width = parseFloat(getComputedStyle(root).getPropertyValue(variable));
    const panel = splitterPanel(target);
    const collapsed = panel && panel.dataset.collapsed === 'true';
    if (collapsed || width === 0) {
      if (panel) panel.dataset.collapsed = 'true';
      root.style.setProperty(variable, '0px');
    } else if (Number.isFinite(width)) {
      root.style.setProperty(variable, `${Math.round(clamp(width, 170, limit))}px`);
    } else {
      root.style.setProperty(variable, `${fallback}px`);
    }
    syncCollapseHandle(target);
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
    try { await loadPhotoLibrary(); } catch (error) { /* older server */ }
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
