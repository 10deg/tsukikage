/* Harness: run the whole page in Node against a stub DOM and a canned server.
 *
 * test_dom.js proves the markup and the script agree on names; test_projection.js
 * proves the geometry.  Neither of them *runs* the page, so neither would notice
 * a control wired to a function that throws, a tab that hides the wrong pane, or
 * a request whose body is missing a field -- which is most of what can go wrong
 * when eleven new controls are added at once.
 *
 * So this one boots the real app.js: it reads index.html for every control and
 * its default value, stands up just enough DOM to let init() run, answers the
 * JSON API from a table, and then drives the page the way a user would --
 * pressing buttons, moving sliders, picking a landform out of the search box --
 * checking both what it drew and what it asked the server for.
 *
 *   node web/test_page.js
 */
'use strict';

const { createRunner, loadApp, readStatic, evalIn } = require('./harness');

const html = readStatic('index.html');

/* The names this harness drives, gathered out of app.js by the shared loader. */
const EXPORTS = ['state', 'views', 'setZoom', 'setResultTab', 'setTrainTab',
  'saveDisk', 'selectFeature', 'refreshLabels', 'activeClasses', 'diskPixels',
  'processingPayload', 'drawPhaseStrip', 'phaseShape', 'orientation',
  'mirroredPlan', 'buildClassToggles', 'loadFeatureIndex', 'matchFeatures',
  'updateNoiseInfo', 'LIMITS', 'SETTING_FIELDS', 'afterSelectionChange',
  'lonLatToPixel', 'cameraFieldRadiusArcsec', 'LAND_CLASSES',
  'collectSettings', 'drawMagnifier', 'magnifierView', 'splitterStart',
  'splitterCeiling', 'SPLITTERS', 'setLeftHidden', 'leftIsHidden',
  'syncKmPxPresets', 'DEFAULT_OFF_CLASSES', 'animationRequest',
  'animationRenderSize', 'animationOutputPixels', 'animationSkyKm',
  'setLabelControls', 'syncResolutionPicker', 'previewPixels',
  'applySettings', 'centrePlacement', 'setAllClasses',
  'eyepieceSettings', 'applyEyepiece', 'renderEvents', 'setProcTab',
  'buildEventKinds', 'drawViewLabels', 'labelEdits', 'labelHidden', 'labelClass',
  'resetLabelClass', 'resetAllLabelEdits', 'labelKey', 'ruler',
  'labelStyleForFrame', 'labelLockOn', 'scalePlan', 'lockedDiskPlan',
  'labelFrameFontPx', 'labelStyleFor', 'labelScreenFontPx',
  'renderPixels', 'compassDirection', 'animationAnnotationsPayload',
  'litCentreFromPixels'];

/* ── every control in the markup, with the value the browser would start with ── */

function attributes(tag) {
  const out = {};
  for (const m of tag.matchAll(/([a-zA-Z-]+)(?:="([^"]*)")?/g)) {
    if (m[1] === 'input' || m[1] === 'select') continue;
    out[m[1]] = m[2] === undefined ? '' : m[2];
  }
  return out;
}

const CONTROL = new Map();     // id -> {type, value, checked, min, max, options}
for (const m of html.matchAll(/<input\b[^>]*>/g)) {
  const attrs = attributes(m[0]);
  if (!attrs.id) continue;
  CONTROL.set(attrs.id, {
    type: attrs.type || 'text',
    value: attrs.value !== undefined ? attrs.value : '',
    checked: 'checked' in attrs,
    min: attrs.min, max: attrs.max,
    class: attrs.class || '',
  });
}
/* Buttons are not form controls, but the page looks them up by class -- and by
   `data-`, for the ones that carry their own argument (the time nudges). */
for (const m of html.matchAll(/<button\b[^>]*>/g)) {
  const attrs = attributes(m[0]);
  if (!attrs.id) continue;
  const data = {};
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('data-')) data[key.slice(5)] = value;
  }
  CONTROL.set(attrs.id, { type: 'button', tag: 'button', value: '',
                          class: attrs.class || '', data });
}
for (const m of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
  /* Each option as the browser would hand it over: value, visible text and
     whether it is disabled.  The page rewrites some of them at run time (the
     物理分辨率 menu carries the measured terrain floor), so the harness has to
     keep the text, not just the value. */
  const options = [...m[2].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map((o) => ({
    value: (o[1].match(/value="([^"]*)"/) || [])[1] || o[2].trim(),
    textContent: o[2].trim(),
    disabled: /\bdisabled\b/.test(o[1]),
  }));
  const selected = (m[2].match(/value="([^"]*)"[^>]*selected/) || [])[1];
  CONTROL.set(m[1], {
    type: 'select', options,
    value: selected !== undefined ? selected : (options[0] ? options[0].value : ''),
  });
}

/* ── a DOM stub rich enough for the page, and no richer ──────────────────── */

const listeners = new Map();    // "element:event" -> [handler]
let elementUid = 0;             // every element gets its own key, ids or not
const documentListeners = new Map();
const registry = new Map();     // id -> element
const created = [];             // every element createElement has made

const CTX_METHODS = [
  'clearRect', 'fillRect', 'strokeRect', 'drawImage', 'save', 'restore', 'beginPath',
  'closePath', 'moveTo', 'lineTo', 'arc', 'fill', 'stroke', 'setTransform', 'transform',
  'translate', 'rotate', 'scale', 'fillText', 'strokeText', 'setLineDash', 'measureText',
  'getImageData', 'putImageData', 'createLinearGradient', 'createRadialGradient',
  'createImageData', 'clip', 'rect', 'ellipse', 'bezierCurveTo', 'quadraticCurveTo',
];
function context2d() {
  const drawn = { fills: [], strokes: [], texts: [], images: [], paths: 0, transforms: [] };
  const noop = () => {};
  const ctx = { canvas: null, drawn, fillStyle: '', strokeStyle: '', lineWidth: 1,
                font: '', textAlign: '', textBaseline: '', globalAlpha: 1,
                lineCap: '', imageSmoothingEnabled: true, imageSmoothingQuality: '' };
  for (const name of CTX_METHODS) ctx[name] = noop;
  ctx.clearRect = () => { drawn.fills.length = 0; drawn.strokes.length = 0;
                          drawn.texts.length = 0; drawn.images.length = 0; };
  ctx.beginPath = () => { drawn.paths += 1; };
  ctx.fill = () => drawn.fills.push(ctx.fillStyle);
  ctx.stroke = () => drawn.strokes.push(ctx.strokeStyle);
  ctx.fillText = (text, x, y) => drawn.texts.push({ text, x, y, colour: ctx.fillStyle });
  ctx.drawImage = (img, ...rest) => drawn.images.push({ img, rest });
  ctx.setTransform = (...args) => drawn.transforms.push(args);
  ctx.rotate = (angle) => drawn.transforms.push(['rotate', angle]);
  ctx.translate = (x, y) => drawn.transforms.push(['translate', x, y]);
  ctx.measureText = (text) => ({ width: String(text).length * 8 });
  ctx.getImageData = (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h });
  ctx.createImageData = (w, h) => ({
    data: new Uint8ClampedArray((w || 0) * (h || 0) * 4), width: w, height: h,
  });
  ctx.putImageData = () => {};
  ctx.createLinearGradient = () => ({ addColorStop: noop });
  ctx.createRadialGradient = () => ({ addColorStop: noop });
  return ctx;
}

function matches(element, selector) {
  // A selector list: `snapshotDefaults()` asks for `input, select`.
  if (selector.includes(',')) {
    return selector.split(',').some((part) => matches(element, part.trim()));
  }
  if (selector === 'input' || selector === 'select') {
    return String(element.tagName).toLowerCase() === selector;
  }
  if (selector === '[data-zoom]') return element.dataset && element.dataset.zoom !== undefined;
  if (selector === '[data-nudge]') {
    return element.dataset && element.dataset.nudge !== undefined;
  }
  // A bare class selector: the dividers look their panel up this way.
  if (/^\.[\w-]+$/.test(selector)) {
    return String(element.className || '').split(/\s+/).includes(selector.slice(1));
  }
  // The two tab strips are looked up as `#strip .tab`.
  if (selector === '#train-tabs .tab') return String(element.id).startsWith('train-tab-');
  if (selector === '#proc-tabs .tab') return String(element.id).startsWith('proc-tab-');
  if (selector === '#result-tabs .tab') return String(element.id).startsWith('tab-');
  if (selector === '#result-tabs .tab') return element.dataset && element.dataset.tab !== undefined;
  if (selector === '#train-tabs .tab') return element.dataset && element.dataset.train !== undefined;
  if (selector === '.splitter') return element.className === 'splitter';
  if (selector === 'button.reset') {
    return String(element.tagName).toLowerCase() === 'button' &&
      String(element.className || '').split(/\s+/).includes('reset');
  }
  if (selector === '.cls-chip') return element.className === 'cls-chip';
  if (selector === '#event-kinds .mark-chip') {
    return String(element.id).startsWith('event-chip-');
  }
  if (selector === '#event-kinds .mark-chip input') {
    return element.type === 'radio' && String(element.id).startsWith('event-kind-');
  }
  if (selector === '#label-classes input[type="checkbox"]' ||
      selector === '#label-classes input') {
    return element.type === 'checkbox' && String(element.id).startsWith('class-');
  }
  if (selector === '.view-stack .zoom') return element.dataset && element.dataset.zoom !== undefined;
  if (selector === '[title]' || selector === '[placeholder]') return false;
  if (selector === '.tab') return element.dataset && (element.dataset.tab !== undefined ||
                                                     element.dataset.train !== undefined);
  return false;
}

function makeElement(id, spec) {
  const info = spec || CONTROL.get(id) || { type: 'text', value: '' };
  const element = {
    _uid: (elementUid += 1),
    id,
    tagName: info.tag ? info.tag.toUpperCase() : (info.type === 'select' ? 'SELECT' : 'INPUT'),
    type: info.type,
    value: info.value,
    checked: info.checked,
    min: info.min, max: info.max,
    disabled: false,
    hidden: false,
    textContent: '',
    innerHTML: '',
    className: info.class || '',
    options: (info.options || []).map((option) =>
      (typeof option === 'string' ? { value: option } : Object.assign({}, option))),
    selectedOptions: [{ textContent: '100%' }],
    // `data-` attributes from the markup, so a button that carries its own
    // argument (the time nudges) reads the same in here as in a browser.
    dataset: Object.assign({}, info.data || {}),
    style: { setProperty() {} },
    attributes: {},
    children: [],
    clientWidth: 560,
    clientHeight: 560,
    width: 560,
    height: 560,
    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      toggle(name, on) { if (on === undefined ? !this._set.has(name) : on) this._set.add(name);
                         else this._set.delete(name); },
      contains(name) { return this._set.has(name); },
    },
    addEventListener(name, handler) {
      const key = `${this._uid}:${name}`;
      if (!listeners.has(key)) listeners.set(key, []);
      listeners.get(key).push(handler);
    },
    removeEventListener() {},
    dispatchEvent(event) { return this.fire(event.type, event); },
    fire(name, event) {
      const handlers = listeners.get(`${this._uid}:${name}`) || [];
      for (const handler of handlers) handler(event || { type: name, preventDefault() {},
                                                         stopPropagation() {} });
      return handlers.length;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; },
    removeAttribute(name) { delete this.attributes[name]; },
    appendChild(child) {
      this.children.push(child);
      // A real <select> adopts an appended <option>, and a menu the page builds
      // at run time has to be visible to `select.options` or every check of it
      // reads an empty list.
      if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
        this.options.push(child);
      }
      return child;
    },
    append(...nodes) { for (const node of nodes) this.children.push(node); },
    insertBefore(child) { this.children.push(child); return child; },
    removeChild() {},
    querySelector(selector) {
      if (selector.startsWith('[data-zoom-')) {
        const kind = selector.slice(1, -1);
        return makeElement(`${this.dataset.zoom}-${kind}`);
      }
      if (selector === 'input') return this.children.find((c) => c.tagName === 'INPUT') || null;
      return null;
    },
    querySelectorAll(selector) {
    return [...registry.values()].filter((el) => matches(el, selector));
  },
    getBoundingClientRect() {
      const width = this.clientWidth || 560;
      const height = this.clientHeight || 560;
      return { left: 0, top: 0, right: width, bottom: height, width, height };
    },
    getContext() {
      if (!this._ctx) { this._ctx = context2d(); this._ctx.canvas = this; }
      return this._ctx;
    },
    setPointerCapture() {}, releasePointerCapture() {}, focus() {}, blur() {},
    click() { this.fire('click'); },
    toDataURL() { return 'data:image/png;base64,AAAA'; },
    contains() { return false; },
  };
  if (id) registry.set(id, element);
  return element;
}

/* The elements that only exist because the markup says so: the tab strips and
   the zoom clusters, which the page looks up by selector rather than by id. */
for (const m of html.matchAll(/data-tab="([^"]+)"/g)) {
  const el = makeElement(`tab-${m[1]}`);
  el.dataset.tab = m[1];
}
for (const m of html.matchAll(/data-proc="([^"]+)"/g)) {
  const el = makeElement(`proc-tab-${m[1]}`);
  el.dataset.proc = m[1];
  el.tagName = 'BUTTON';
  registry.set(el.id, el);
}
for (const m of html.matchAll(/data-train="([^"]+)"/g)) {
  const el = makeElement(`train-tab-${m[1]}`);
  el.dataset.train = m[1];
}
for (const name of ['disk', 'raw', 'proc']) {
  const el = makeElement(`zoom-${name}`);
  el.dataset.zoom = name;
  el.querySelector = (selector) => makeElement(`${name}-${selector.slice(1, -1)}`);
}
for (const el of html.matchAll(/class="splitter[^"]*"/g)) {
  const node = makeElement(`splitter-${el.index}`);
  node.className = 'splitter';
  node.dataset.target = 'left';
}
/* The panels a divider measures before it starts moving.  The harness has
   no layout engine, so they are given sizes here; what `splitterStart()` reads
   is exactly what the browser would report for them. */
for (const [name, width, height] of [['col-left', 300, 900], ['col-right', 620, 900],
                                     ['view-disk', 520, 520], ['view-info', 600, 300],
                                     ['views', 560, 900], ['panes', 620, 900],
                                     ['pane-a', 280, 900]]) {
  const node = makeElement(`probe-${name}`);
  node.className = name;
  node.clientWidth = width;
  node.clientHeight = height;
}

const document = {
  body: makeElement('body'),
  documentElement: { style: { setProperty() {} } },
  getElementById(id) {
    if (registry.has(id) && registry.get(id).id === id) return registry.get(id);
    // An element the page built itself (a class chip) is registered under the
    // id it was given after creation, which is the only way to find it.
    const built = created.find((el) => el.id === id);
    if (built) { registry.set(id, built); return built; }
    if (!registry.has(id)) registry.set(id, makeElement(id));
    return registry.get(id);
  },
  createElement(tag) {
    const element = makeElement('', { type: tag === 'canvas' ? 'canvas' : 'text', value: '' });
    element.tagName = tag.toUpperCase();
    element.id = '';
    created.push(element);
    return element;
  },
  /* The page builds the class chips and then looks them up by selector, so a
     lookup has to see both the parsed markup and what the page created. */
  querySelectorAll(selector) {
    // An element the page has not touched yet does not exist in here: this DOM
    // makes them on demand, from the markup.  A selector query is a touch --
    // 范围微调 is wired by asking for every `[data-nudge]` button at once, and
    // without this the two rows the page had never looked up individually were
    // simply invisible.
    if (selector === '[data-nudge]') {
      for (const [id, info] of CONTROL) {
        if (info.data && info.data.nudge !== undefined && !registry.has(id)) {
          registry.set(id, makeElement(id));
        }
      }
    }
    const seen = new Set();
    const out = [];
    for (const el of [...registry.values(), ...created]) {
      if (seen.has(el) || !matches(el, selector)) continue;
      seen.add(el);
      out.push(el);
    }
    return out;
  },
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  addEventListener(name, handler) {
    if (!documentListeners.has(name)) documentListeners.set(name, []);
    documentListeners.get(name).push(handler);
  },
  dispatchEvent(event) {
    for (const handler of documentListeners.get(event.type) || []) handler(event);
  },
  createTreeWalker() { return { nextNode: () => false }; },
  createTextNode(text) { return { nodeValue: text }; },
};

/* ── the canned server ───────────────────────────────────────────────────── */

const requests = [];
const diskImage = 'data:image/png;base64,disk';
const regionImage = 'data:image/png;base64,region';
/* One picture per radiance component: sunlight and earthshine are twelve
   magnitudes apart, so a component request is a different exposure of the same
   frame -- which is what makes "did the page hand back the last component's
   bytes?" a question the harness can actually answer. */
const sunlitImage = 'data:image/png;base64,sunlit';
const earthshineImage = 'data:image/png;base64,earthshine';

function componentImage(body) {
  return body.component === 'sunlit' ? sunlitImage
    : body.component === 'earthshine' ? earthshineImage : regionImage;
}

const FEATURES = [
  { en: 'Mare Imbrium', zh: '雨海', lat: 34.72, lon: -14.91, km: 1145.53, class: 'mare' },
  { en: 'Clavius', zh: '克拉维斯', lat: -58.4, lon: -14.4, km: 231, class: 'crater' },
  { en: 'Rima Hyginus', zh: '许癸努斯月溪', lat: 7.4, lon: 7.8, km: 220, class: 'rima' },
];

function planFor(body) {
  const width = (body.preview && body.preview.width) || 900;
  return {
    frame: { width, height: width, kind: body.frame },
    font_px: body.font_px || 13,
    language: body.language || 'zh',
    items: [{
      name_en: 'Mare Imbrium', name_zh: '雨海', kind: 'Mare', class: 'mare',
      x: width * 0.3, y: width * 0.4, radius_px: width * 0.1, illuminated: true,
      label: '雨海', labelled: true, label_x: width * 0.34, label_y: width * 0.42,
      label_w: 40, label_h: 16, leader: false, outline: [[0, 0], [10, 0], [10, 10]],
    }],
    counts: { labelled: 1, outlined: 1, in_frame: 1, classes: { mare: 1 } },
  };
}

function respond(url, body) {
  if (url === '/api/health') {
    return { ok: true, limits: { preview_px: 5000, output_px: 8192, disk_px: 10000,
                                 min_px: 64, terrain_km_per_px: 0.118,
                                 label_outline_px: 9 } };
  }
  if (url === '/api/features') {
    return { features: FEATURES, count: FEATURES.length,
             classes: [{ key: 'mare', zh: '月海', en: 'Mare', count: 23 },
                       { key: 'crater', zh: '环形坑', en: 'Crater', count: 8716 }] };
  }
  if (url === '/api/settings/list') {
    return { dir: '/tmp', files: [
      { name: 'default', bytes: 4096, saved_utc: '2026-05-25T07:30:00+00:00' },
      { name: 'default2', bytes: 4096, saved_utc: '2026-05-25T07:30:00+00:00' },
    ] };
  }
  if (url === '/api/settings/load') {
    // A file the page itself wrote: it carries the annotation switch too, so
    // applying it does not read as "turn the labels off".
    return { name: body.name,
             settings: { 'label-density': 0.42, 'text-bold': true, annotate: true } };
  }
  if (url === '/api/disk') {
    const width = (body.preview && body.preview.width) || 1000;
    const asked = body.earthshine || {};
    /* The gain map only comes back when the request asked for HDR: it is a
       second picture, and a page that is not saving one should not pay for it. */
    const hdr = body.hdr && body.hdr.on ? {
      gain: 'data:image/png;base64,gain',
      metadata: { GainMapMin: '0', GainMapMax: '3', Gamma: '1',
                  OffsetSDR: '0.015625', OffsetHDR: '0.015625' },
      width, height: width, stops: body.hdr.stops,
    } : undefined;
    return { image: diskImage, hdr, geometry: {
      when_utc: '2026-05-25T07:30:00+00:00', distance_km: 384400,
      angular_diameter_deg: 0.5135, disk_radius_fraction: 0.47,
      libration_lon_deg: -4.7, libration_lat_deg: 1.4,
      sub_solar_lon_deg: -6.5, sub_solar_lat_deg: 1.5,
      illuminated_percent: 69.6, bright_limb_position_angle_deg: 89.4,
      phase_angle_deg: 47.9, moon_magnitude: -11.46,
      render_seconds: 0.6, bands: 16, band_workers: 8, image_width: width,
      /* The server reports what the frame was drawn with, so the stub echoes
         the request back -- which is also what makes the readout testable. */
      earthshine: {
        earthshine_on: asked.on !== false,
        earthshine_ratio: 4.55e-5,
        earthshine_magnitude: -15.88, earthshine_apparent_magnitude: -0.58,
        earth_illuminated_percent: 60.5,
        earth_phase_angle_deg: 77.9, earth_angular_diameter_deg: 1.95,
        earth_distance_km: 375624,
        earth_geometric_albedo: asked.earth_geometric_albedo || 0.367,
        earthshine_gain: asked.gain === undefined ? 1 : asked.gain,
        earthshine_tint: asked.tint || '0.87,0.96,1.17',
      },
      report: { state: { phase: { label: '上弦月 / First Quarter', age_days: 8.48 },
                         libration: { colongitude_deg: 45.2,
                                      sub_earth_lon_deg: -4.7, sub_earth_lat_deg: 1.4,
                                      subsolar_lon_deg: -6.5, subsolar_lat_deg: 1.5 },
                         orbit: {},
                         angular_diameter_arcsec: 1793.5,
                         geo_distance_km: 384400, topo_distance_km: 384300,
                         elongation_deg: 89.4, elevation_deg: 57.08, azimuth_deg: 180.3 } },
    } };
  }
  if (url === '/api/annotations') return planFor(body);
  if (url === '/api/region') {
    const image = componentImage(body);
    return { render_id: '7', image, image_raw: image,
             info: { component: body.component || 'image',
                     image: { shape: [400, 400] }, region: { ground_sample_km: 0.2,
                     emergence_deg: 3, solar_elevation_deg: 20 },
                     earthshine: { earthshine_ratio: 4.55e-5, earthshine_magnitude: -15.88,
                                   earthshine_apparent_magnitude: -0.58,
                                   earth_illuminated_percent: 60.5,
                                   earth_phase_angle_deg: 77.9,
                                   earth_geometric_albedo: 0.367, earthshine_gain: 1 },
                     train: { plate_scale_arcsec: 0.2, telescope: { name: 'SCT' },
                              camera: { name: 'IMX' }, field_deg: [0.2, 0.2] },
                     instrument: { fwhm_total_arcsec: 1.1, sharpen_sigma_arcsec: 0.3 } } };
  }
  if (url === '/api/process') {
    const image = componentImage(body);
    return { info: { component: body.component || 'image', image: { shape: [400, 400] },
                     instrument: {} },
             image, image_raw: body.raw ? image : undefined };
  }
  if (url === '/api/settings/save') return { saved: { name: 'x', bytes: 10 }, files: [] };
  if (url === '/api/animation') return { id: '3', state: 'running', total: 8 };
  if (url.startsWith('/api/animation/')) {
    return { id: '3', state: 'done', frames: 8, total: 8, progress: 1,
             seconds: 0.67, fps: 12, url: '/api/animation/3/file' };
  }
  if (url === '/api/hdr/jpeg') {
    return { image: 'data:image/jpeg;base64,hdr',
             hdr: { bytes: 100, gain_offset: 10, gain_length: 20 } };
  }
  return { error: 'no route ' + url };
}

const fetchCalls = [];
async function fetchStub(url, options) {
  const body = options && options.body ? JSON.parse(options.body) : {};
  fetchCalls.push({ url, body });
  const payload = respond(url, body);
  return {
    ok: !payload.error,
    status: payload.error ? 404 : 200,
    statusText: 'OK',
    json: async () => payload,
  };
}

/* ── the page, running ───────────────────────────────────────────────────── */

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }

class FakeImage {
  constructor() { this.width = 0; this.height = 0; }
  set src(value) {
    this._src = value;
    const size = value === diskImage ? 1200 : 400;
    this.width = size;
    this.height = size;
    setTimeout(() => { if (this.onload) this.onload(); }, 0);
  }
  get src() { return this._src; }
}

/* The layout variables the dividers read, as the stylesheet sets them.
   `setProperty` on the root is a no-op in this harness, so a drag can be driven
   but not remembered -- which is all these checks need. */
const ROOT_STYLE = { '--splitter': '6px', '--w-left': '300px', '--w-right': '620px',
                     '--disk-w': '1fr', '--raw-h': '1fr' };

const sandbox = {
  document,
  window: {
    addEventListener() {},
    devicePixelRatio: 1,
    localStorage: { getItem: () => null, setItem() {} },
    ResizeObserver: ResizeObserverStub,
  },
  ResizeObserver: ResizeObserverStub,
  getComputedStyle: (element) => ({
    getPropertyValue: (name) => {
      if (element === document.documentElement) return ROOT_STYLE[name] || '';
      return '';
    },
  }),
  performance: { now: () => Date.now() },
  console: { log: console.log, info: console.info, warn: console.warn,
             error: (...args) => consoleErrors.push(args.join(' ')) },
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: fetchStub,
  Image: FakeImage,
  Event: class { constructor(type) { this.type = type; } },
  AbortController: class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  },
  Blob: class { constructor(parts, options) { this.parts = parts; this.type = options && options.type; } },
  URL: { createObjectURL: () => 'blob:stub' },
  atob: (text) => Buffer.from(text, 'base64').toString('binary'),
  btoa: (text) => Buffer.from(text, 'binary').toString('base64'),
  Uint8ClampedArray, ArrayBuffer, DataView, Uint8Array,
  NodeFilter: { SHOW_TEXT: 4 },
};
sandbox.window.document = document;
const consoleErrors = [];
const { check, finish } = createRunner();
const T = loadApp(sandbox, EXPORTS);
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const set = (id, value) => {
  const el = document.getElementById(id);
  el.value = String(value);
  if (el.type === 'checkbox') el.checked = Boolean(value);
  return el;
};
const fire = (id, name, event) => document.getElementById(id).fire(name, event);

(async function main() {
  console.log('boot');
  /* DOMContentLoaded is what the page waits for; the harness dispatches it the
     way the browser would, to the listener the script registered. */
  document.dispatchEvent({ type: 'DOMContentLoaded' });
  await tick();
  await tick();
  check('init() runs without throwing', true);
  check('it asked the server what it may ask for',
    fetchCalls.some((c) => c.url === '/api/health'), JSON.stringify(fetchCalls.map((c) => c.url)));
  check('it rendered the disk', fetchCalls.some((c) => c.url === '/api/disk'));
  check('it listed the settings directory',
    fetchCalls.some((c) => c.url === '/api/settings/list'));
  check('the geometry from /api/disk landed in the state',
    T.state.geometry && T.state.geometry.angular_diameter_deg > 0.5);
  check('the preview canvas took the rendered size',
    document.getElementById('disk').width === 1200);
  check('the class switches were built from the class list',
    document.querySelectorAll('.cls-chip').length === T.LAND_CLASSES.length,
    document.querySelectorAll('.cls-chip').length);
  /* The columns are part of "how the page looks": a settings file has to carry
     the widths the reader dragged, and put them back on import. */
  {
    const saved = T.collectSettings();
    check('the settings file carries the layout',
      saved.layout && ['left', 'right', 'disk', 'raw', 'annot_open']
        .every((key) => saved.layout[key] !== undefined),
      JSON.stringify(saved.layout));
    const used = T.applySettings({ layout: { left: 300, right: 480, disk: 520, raw: 260,
                                            annot_open: true } });
    check('and importing one applies it', used >= 5, String(used));
    check('including the fold of the annotation panel',
      document.getElementById('annot-detail').hidden === false,
      String(document.getElementById('annot-detail').hidden));
    T.setLabelControls(false);
    check('which starts folded', document.getElementById('annot-detail').hidden === true);
  }

  check('every class switch is a settings field too',
    T.SETTING_FIELDS.includes('class-mare') && T.SETTING_FIELDS.includes('class-rima'));
  /* The date field is filled from the time zone field beside it, not from the
     browser's own idea of local time (which read as UTC+0 at UTC+8). */
  {
    const wall = new Date(Date.now() + Number(document.getElementById('tz').value) * 3600e3)
      .toISOString().slice(0, 16);
    const shown = document.getElementById('when').value;
    const gap = Math.abs(Date.parse(shown + ':00Z') - Date.parse(wall + ':00Z'));
    check('the date field starts at the zone the field beside it names',
      gap <= 60 * 1000, `${shown} vs ${wall}`);
  }

  console.log('');
  console.log('方位角 in words as well as degrees');
  /* The compass word is generated, not markup: the readout's English switch
     cannot translate a word the script produced, so it has to be re-rendered. */
  {
    check('the example the reader asked for is ESE',
      T.compassDirection(112.5, 'zh') === '东南偏东',
      T.compassDirection(112.5, 'zh'));
    check('due north and due west are the words themselves',
      T.compassDirection(0, 'zh') === '北' && T.compassDirection(270, 'zh') === '西');
    check('and it wraps past 360°',
      T.compassDirection(350, 'zh') === '北' && T.compassDirection(-10, 'zh') === '北');
    check('the readout prints the degree and the direction together',
      /^180\.30°（南）$/.test(document.getElementById('mi-az').textContent),
      document.getElementById('mi-az').textContent);
    fire('moon-lang', 'click');
    await tick();
    check('the English readout switches the word too',
      /^180\.30° \(S\)$/.test(document.getElementById('mi-az').textContent),
      document.getElementById('mi-az').textContent);
    fire('moon-lang', 'click');
    await tick();
  }

  console.log('');
  console.log('the resolution menus');
  /* Every size either menu offers has to be one the page can actually ask for:
     a preset that gets clamped is a menu entry that lies. */
  /* The two resolution menus are `<select>`s now: a datalist on a text input
     showed no arrow, so the presets were invisible. */
  const presets = (id) => {
    const block = html.match(new RegExp('<select[^>]*id="' + id + '"[\\s\\S]*?</select>'));
    return [...(block ? block[0] : '').matchAll(/<option value="(\d+)"/g)]
      .map((m) => Number(m[1]));
  };
  const previewMenu = presets('preview-preset');
  const diskMenu = presets('disk-preset');
  check('every menu offers a way in by hand',
    (html.match(/<option value="custom">/g) || []).length === 5,
    (html.match(/<option value="custom">/g) || []).length);
  check('the preview menu offers the sizes the page may ask for',
    previewMenu.length >= 8 && previewMenu.every((px) => px <= T.LIMITS.preview_px),
    previewMenu.join(','));
  check('and includes 5000, the size that moved the cap',
    previewMenu.includes(5000) && T.LIMITS.preview_px >= 5000, T.LIMITS.preview_px);
  check('the save menu stays inside the disk cap',
    diskMenu.length >= 8 && diskMenu.every((px) => px <= T.LIMITS.disk_px),
    diskMenu.join(','));
  /* The full disk and the close-ups save through the same list of sizes: two
     menus that disagreed about what "2000" means would be two products. */
  const regionMenu = presets('out-preset');
  check('the two save menus offer the same sizes',
    regionMenu.join(',') === diskMenu.join(','),
    `${regionMenu.join(',')} vs ${diskMenu.join(',')}`);
  check('and it is the list the page was asked for',
    diskMenu.join(',') === '480,600,800,900,1000,1200,1400,1500,1600,1800,1920,2000,2160,2400,3840,4000,5000',
    diskMenu.join(','));
  check('the saving row says what a pixel is worth on the ground',
    /^px（[\d.]+ (km|m)）$/.test(document.getElementById('disk-scale').textContent),
    document.getElementById('disk-scale').textContent);

  console.log('');
  console.log('the two resolution pickers');
  /* A datalist on a text input showed no arrow and no menu, so the presets were
     invisible; the menu is a real <select> and the field follows it. */
  const previewPreset = document.getElementById('preview-preset');
  const previewField = document.getElementById('preview-px');
  previewPreset.value = '2000';
  previewPreset.fire('change');
  check('picking a preview preset fills the number the page reads',
    previewField.value === '2000' && T.previewPixels() === 2000,
    `${previewField.value} / ${T.previewPixels()}`);
  // The change re-renders, on the same debounce the other observer fields use.
  await new Promise((resolve) => setTimeout(resolve, 500));
  check('and that is what the next render asks for',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.preview.width === 2000,
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.preview.width);
  previewPreset.value = 'custom';
  previewPreset.fire('change');
  check('choosing 自定义 reveals the field instead of hiding it',
    previewField.hidden === false);
  previewField.value = '1234';
  T.syncResolutionPicker('preview-px', 'preview-preset');
  check('and a number that is not on the menu keeps it on 自定义',
    previewPreset.value === 'custom', previewPreset.value);
  previewField.value = '1500';
  T.syncResolutionPicker('preview-px', 'preview-preset');
  check('while one that is on the menu selects it again',
    previewPreset.value === '1500' && previewField.hidden === true,
    `${previewPreset.value} / hidden=${previewField.hidden}`);
  previewPreset.value = '1000';
  previewPreset.fire('change');
  await tick();

  console.log('');
  console.log('folding the landform panel away');
  T.setLabelControls(false);
  check('the overlays and the style controls fold away',
    document.getElementById('annot-detail').hidden === true &&
    document.getElementById('label-more').getAttribute('aria-expanded') === 'false',
    document.getElementById('label-more').textContent);
  check('but the search and the switch itself stay',
    document.getElementById('annotate').hidden === false &&
    document.getElementById('feature-search').hidden === false);
  T.setLabelControls(true);
  check('and unfold again',
    document.getElementById('annot-detail').hidden === false &&
    document.getElementById('label-more').getAttribute('aria-expanded') === 'true');

  console.log('');
  console.log('hiding the left column');
  T.setLeftHidden(true);
  check('the left column can be dragged away', T.leftIsHidden() === true);
  T.setLeftHidden(false, 280);
  check('and the divider it left behind brings it back',
    T.leftIsHidden() === false);

  console.log('');
  console.log('the instrument tabs');
  check('the telescope side is the one on screen',
    document.getElementById('train-telescope').hidden === false &&
    document.getElementById('train-camera').hidden === true);
  T.setTrainTab('camera');
  check('switching to the camera swaps the panes',
    document.getElementById('train-telescope').hidden === true &&
    document.getElementById('train-camera').hidden === false);
  T.setTrainTab('telescope');
  // The tabs are wired by a loop in init(); a throw earlier in init() used to
  // leave them dead, so press them the way a reader does.
  fire('train-tab-telescope', 'click');
  check('the telescope tab responds to a click',
    document.getElementById('train-telescope').hidden === false);
  fire('train-tab-camera', 'click');
  check('and so does the camera tab',
    document.getElementById('train-camera').hidden === false &&
    document.getElementById('train-telescope').hidden === true);
  fire('train-tab-telescope', 'click');

  console.log('');
  console.log('back focus scales the field');
  set('aperture', 203.2);
  set('focal-ratio', 10);
  set('focal-length', '');
  set('barlow', 1);
  set('back-focus', 1);
  set('sensor-w', 3008);
  set('sensor-h', 3008);
  set('pixel-um', 3.76);
  const plain = evalIn(sandbox, 'cameraFieldRadiusArcsec()');
  set('back-focus', 1.1);
  const stretched = evalIn(sandbox, 'cameraFieldRadiusArcsec()');
  check('a back-focus ratio above 1 shrinks the field in proportion',
    Math.abs(plain / stretched - 1.1) < 1e-9, `${plain} -> ${stretched}`);
  set('back-focus', 1);

  console.log('');
  console.log('earthshine travels with the render');
  /* Earthshine is computed from the ephemeris, so the page must not invent it:
     it sends the gain it owns and quotes back the numbers the render reports.
     There is no on/off switch any more -- the sky that is there is computed. */
  set('earthshine-gain', 4);
  set('earth-geometric-albedo', 0.3);
  set('earth-samples', 21);
  /* The tint is left set for the rest of the run, because the export checks
     below need a non-default value to prove the save path carries it at all. */
  set('earthshine-tint', '1.2,1.0,0.7');
  fire('earthshine-gain', 'change');
  await tick();
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  const diskBody = fetchCalls.filter((c) => c.url === '/api/disk').pop().body;
  check('the disk request carries the earthshine block',
    diskBody.earthshine && diskBody.earthshine.on === true &&
    diskBody.earthshine.gain === 4 &&
    Math.abs(diskBody.earthshine.earth_geometric_albedo - 0.3) < 1e-12 &&
    diskBody.earthshine.earth_samples === 21 &&
    diskBody.earthshine.tint === '1.2,1.0,0.7',
    JSON.stringify(diskBody.earthshine));
  check('and the optical choices travel with it, not buried in the preview',
    diskBody.photometric_model === 'lunar_lambert' && diskBody.sun_samples === 1,
    JSON.stringify({ model: diskBody.photometric_model, sun: diskBody.sun_samples }));
  // Sky light is per-point and only shows up in shadow, so it must reach the
  // render as a number -- and a blank field has to mean "off", not NaN.
  set('sky-ambient', 1.5);
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  check('the sky-ambient control reaches the render',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.sky_ambient === 1.5,
    JSON.stringify(fetchCalls.filter((c) => c.url === '/api/disk').pop().body.sky_ambient));
  set('sky-ambient', '');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  check('and a blank ambient means off',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.sky_ambient === 0,
    JSON.stringify(fetchCalls.filter((c) => c.url === '/api/disk').pop().body.sky_ambient));
  set('sky-ambient', 0);
  /* The earthshine line lives in the full disk's footer now, beside the
     picture's own size and scale, and it no longer carries the ratio.  Its
     second number is 地照亮度: how bright the Moon's *night* side is in our own
     sky, lit only by the Earth.  For a round it was labelled 地球（月面所见）--
     the Earth in the Moon's sky -- which is the *source* of the earthshine and
     a different body's magnitude entirely. */
  const readout = document.getElementById('disk-info-earthshine').textContent;
  check('the full disk foot quotes the ephemeris the frame was drawn with',
    /地照亮度 -0\.58 mag/.test(readout) && !/反照率/.test(readout), readout);
  check('and 地照亮度 is the Moon\'s night side, not the Earth',
    !/地球/.test(readout) && !/月面所见/.test(readout),
    readout + ' / ' + document.getElementById('disk-info-earthshine').title);
  check('with the Moon magnitude beside them',
    /月球亮度 -11\.46 mag/.test(readout), readout);
  check('and the ratio is gone rather than moved along',
    !/亮度比/.test(readout), readout);
  const foot = document.getElementById('disk-info-size').textContent;
  check('and the size and the ground scale share the line',
    /\d+×\d+ px/.test(foot) && /1 px = /.test(foot), foot);
  // The rest of the page is left asking for the ephemeris default, so that the
  // carefully-worded fallback is what the other checks run against.  A blank
  // albedo is not zero: it means "the default", and the block carries null
  // rather than a number the server would have to guess.
  set('earth-geometric-albedo', '');
  set('earthshine-gain', '');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  const blank = fetchCalls.filter((c) => c.url === '/api/disk').pop().body.earthshine;
  check('a blank albedo asks for the ephemeris default',
    blank.earth_geometric_albedo === null && blank.gain === 1,
    JSON.stringify(blank));

  /* 标尺 belongs to the picture, not to the labels: it has to be on the full
     disk with every name switched off and no selection dragged.  Both halves
     of that were wrong -- the early return skipped it, and the scale it asked
     for came from a selection that need not exist.  It is now one of the
     几何标注 switches, so what has to hold is that switching *it* on draws it
     while every other switch and every name stays off. */
  {
    for (const name of ['terminator', 'poles', 'limb', 'equator', 'libration',
                        'subsolar', 'nearside', 'ruler']) {
      set('mark-' + name, false);
    }
    const savedPlan = T.state.labels.disk;
    T.state.labels.disk = null;
    const layer = document.getElementById('labels-disk');
    const drawn = layer.getContext().drawn;
    drawn.texts.length = 0;
    T.drawViewLabels('disk');
    const without = drawn.texts.length;
    set('mark-ruler', true);
    drawn.texts.length = 0;
    T.drawViewLabels('disk');
    const texts = drawn.texts.map((entry) => entry.text);
    check('the full disk carries its ruler with every label switched off',
      texts.some((text) => /^\d+(\.\d+)? (km|m)$/.test(text)), texts.join(' | '));
    check('and not a pixel of it without the switch',
      without === 0, `${without} texts`);
    set('mark-ruler', false);
    T.state.labels.disk = savedPlan;
  }

  console.log('');
  console.log('the full disk save');
  set('disk-px', 4096);
  set('disk-annotated', false);
  T.state.labelsOn = false;
  const before = fetchCalls.filter((c) => c.url === '/api/disk').length;
  await T.saveDisk();
  await tick();
  const diskCalls = fetchCalls.filter((c) => c.url === '/api/disk');
  check('saving the disk renders it at its own resolution',
    diskCalls.length === before + 1 &&
    diskCalls[diskCalls.length - 1].body.preview.width === 4096,
    JSON.stringify(diskCalls[diskCalls.length - 1].body.preview));
  check('and the export carries the light, not just the size',
    diskCalls[diskCalls.length - 1].body.earthshine &&
    diskCalls[diskCalls.length - 1].body.earthshine.earthshine_tint === undefined &&
    diskCalls[diskCalls.length - 1].body.earthshine.tint === '1.2,1.0,0.7' &&
    diskCalls[diskCalls.length - 1].body.photometric_model === 'lunar_lambert',
    JSON.stringify(diskCalls[diskCalls.length - 1].body.earthshine));
  await T.saveDisk();
  check('and renders it once, not once per press',
    fetchCalls.filter((c) => c.url === '/api/disk').length === before + 1,
    fetchCalls.filter((c) => c.url === '/api/disk').length);
  // The earthshine checkbox is a different render, not a relabelled file: the
  // cache key has to include it or the second press hands back the first.
  set('disk-earthshine', false);
  fire('disk-earthshine', 'change');
  check('unticking earthshine says what the next save will write',
    /阳光/.test(document.getElementById('status').textContent),
    document.getElementById('status').textContent);
  await T.saveDisk();
  await tick();
  const componentCalls = fetchCalls.filter((c) => c.url === '/api/disk');
  check('saving without earthshine re-renders and asks for the sunlit component',
    componentCalls.length === before + 2 &&
    componentCalls[componentCalls.length - 1].body.component === 'sunlit',
    `${componentCalls.length - before} calls, ` +
    JSON.stringify(componentCalls[componentCalls.length - 1].body.component));
  set('disk-earthshine', true);

  console.log('');
  console.log('the bright-face centring');
  {
    /* A picture whose answer is known without a browser: an 8×8 probe, the
       right half lit at full brightness, the whole frame inside the disk. */
    const probe = 8;
    const pixels = (lit) => {
      const data = new Uint8ClampedArray(probe * probe * 4);
      for (let y = 0; y < probe; y += 1) {
        for (let x = 0; x < probe; x += 1) {
          const value = lit(x, y) ? 255 : 0;
          const i = 4 * (y * probe + x);
          data[i] = data[i + 1] = data[i + 2] = value;
          data[i + 3] = 255;
        }
      }
      return data;
    };
    const right = T.litCentreFromPixels(pixels((x) => x >= probe / 2), probe, 0.5);
    check('the whole disk centre is the frame centre',
      right && Math.abs(right.diskX - probe / 2) < 1e-9 &&
      Math.abs(right.diskY - probe / 2) < 1e-9, JSON.stringify(right));
    check('the lit centroid is the right half\'s own centre',
      right && Math.abs(right.litX - 6) < 1e-9, JSON.stringify(right && right.litX));
    check('the new centre is half way from the disk to the lit centroid',
      right && Math.abs(right.x - 5) < 1e-9 &&
      Math.abs((right.x - right.diskX) - 0.5 * (right.litX - right.diskX)) < 1e-9,
      JSON.stringify(right));
    check('and the bright side is not followed vertically',
      right && Math.abs(right.y - probe / 2) < 1e-9, JSON.stringify(right && right.y));

    const top = T.litCentreFromPixels(pixels((x, y) => y < probe / 2), probe, 0.5);
    check('a top-lit frame moves sideways only, not up',
      top && Math.abs(top.x - probe / 2) < 1e-9 &&
      Math.abs(top.y - probe / 2) < 1e-9 && top.litY < 3,
      JSON.stringify(top));

    const full = T.litCentreFromPixels(pixels(() => true), probe, 0.5);
    check('a uniformly lit disk stays where the whole disk is',
      full && Math.abs(full.x - probe / 2) < 1e-9, JSON.stringify(full));

    const black = T.litCentreFromPixels(new Uint8ClampedArray(probe * probe * 4),
      probe, 0.5);
    check('a black frame has no centre to guess at', black === null, String(black));
  }

  console.log('');
  console.log('landform labels');
  fire('annotate', 'click');
  await tick();
  const annotationCalls = fetchCalls.filter((c) => c.url === '/api/annotations');
  /* The regression this guards: the request goes out, the plan comes back, and
     nothing is painted.  Asserting the *request* is not the same as asserting
     the reader sees a name on the picture. */
  {
    const layer = document.getElementById('labels-disk');
    const texts = layer._ctx ? layer._ctx.drawn.texts : [];
    check('and the names actually reach the disk layer',
      texts.length > 0, JSON.stringify(texts.slice(0, 4)));
  }
  check('turning the labels on asks for the disk frame',
    annotationCalls.length > 0 && annotationCalls[annotationCalls.length - 1].body.frame === 'disk',
    JSON.stringify(annotationCalls.map((c) => c.body.frame)));
  set('class-rima', false);
  fire('class-rima', 'change');
  await tick();
  const after = fetchCalls.filter((c) => c.url === '/api/annotations').pop();
  check('switching a class off re-asks without it',
    Array.isArray(after.body.classes) && !after.body.classes.includes('rima'),
    JSON.stringify(after.body.classes));
  check('and the rest are still asked for', after.body.classes.includes('mare'));
  set('label-shadow', true);
  fire('label-shadow', 'change');
  await tick();
  check('the night-side switch is sent to the server',
    fetchCalls.filter((c) => c.url === '/api/annotations').pop().body.only_illuminated === false);
  set('label-shadow', false);
  fire('label-shadow', 'change');
  await tick();
  // 彩色 is the switch that used to be 统一配色 (one colour), with the sense
  // turned round: ticked = every class keeps its colour, unticked = one colour.
  check('彩色 on gives each class its own colour',
    evalIn(sandbox, 'labelStyleFor("disk").uniform') === false);
  set('label-colour', false);
  fire('label-colour', 'change');
  await tick();
  check('彩色 off collapses every label to one colour',
    evalIn(sandbox, 'labelStyleFor("disk").uniform') === true,
    evalIn(sandbox, 'labelStyleFor("disk").uniform'));
  set('label-colour', true);
  fire('label-colour', 'change');
  await tick();
  /* 标注点描边 is the marker's own rim: it used to be the text stroke in
     disguise, with a fixed width.  Both halves have to reach the painter. */
  set('marker-stroke', '#ff0000');
  set('marker-stroke-width', 0.6);
  fire('marker-stroke', 'change');
  await tick();
  {
    const style = T.labelStyleFor('disk');
    check('the marker rim has its own colour and width',
      style.markerStrokeColor === '#ff0000' &&
      style.markerStrokeWidth > 0 &&
      style.markerStrokeWidth < T.labelStyleFor('disk').strokeWidth,
      `${style.markerStrokeColor} / ${style.markerStrokeWidth}`);
    check('and the default is thinner than the old fixed rim',
      (() => {
        set('marker-stroke-width', 1);
        fire('marker-stroke-width', 'change');
        const s = T.labelStyleFor('disk');
        return s.markerStrokeWidth * T.labelScreenFontPx('disk') /
          Math.max(1e-9, T.labelFrameFontPx('disk')) <= 1.001;
      })(),
      String(T.labelStyleFor('disk').markerStrokeWidth));
  }
  set('marker-stroke', '#0c0e14');
  set('marker-stroke-width', 1);
  fire('marker-stroke', 'change');

  console.log('');
  console.log('the geometric overlays need no server');
  /* These are the page's own geometry -- the terminator, the limb, the poles,
     the equator, the libration point -- so switching one on must redraw the
     disk and ask the server for nothing at all.  The regression they guard
     against is a control that is wired to a request, or to nothing. */
  const annotationsBeforeMarks = fetchCalls.filter((c) => c.url === '/api/annotations').length;
  const labelLayer = document.getElementById('labels-disk');
  for (const id of ['mark-terminator', 'mark-poles', 'mark-limb', 'mark-equator',
                    'mark-libration', 'mark-subsolar', 'mark-nearside']) {
    set(id, true);
    fire(id, 'change');
  }
  await tick();
  check('every overlay switch draws the disk',
    labelLayer._ctx && labelLayer._ctx.drawn.paths > 0,
    labelLayer._ctx ? labelLayer._ctx.drawn.paths : 'no context');
  check('and none of them asks the server for anything',
    fetchCalls.filter((c) => c.url === '/api/annotations').length === annotationsBeforeMarks,
    `${fetchCalls.filter((c) => c.url === '/api/annotations').length} / ${annotationsBeforeMarks}`);
  check('the status line names the one that was switched on',
    /已标注/.test(document.getElementById('status').textContent),
    document.getElementById('status').textContent);
  check('the page knows an overlay is on (so an export keeps a canvas)',
    evalIn(sandbox, 'marksWanted()') === true);
  set('mark-nearside', false);
  fire('mark-nearside', 'change');
  set('mark-terminator', false);
  fire('mark-terminator', 'change');
  set('mark-poles', false);
  fire('mark-poles', 'change');
  set('mark-limb', false);
  fire('mark-limb', 'change');
  set('mark-equator', false);
  fire('mark-equator', 'change');
  set('mark-libration', false);
  fire('mark-libration', 'change');
  set('mark-subsolar', false);
  fire('mark-subsolar', 'change');
  check('and with all of them off an export can go straight out',
    evalIn(sandbox, 'marksWanted()') === false);

  console.log('');
  console.log('the landform search');
  await T.loadFeatureIndex();
  await tick();
  check('the catalogue is fetched once',
    fetchCalls.filter((c) => c.url === '/api/features').length === 1);
  /* The box is placed through the *disk*, so the size comes from the drag and
     from the physical-resolution row; the typed fields are a centre.  Typing
     one therefore has to leave the size alone -- which is what the check below
     the search is about. */
  T.state.selection = null;
  set('sel-lon', 10); set('sel-lat', 10);
  fire('sel-lon', 'change');
  const radiusBefore = T.state.selection.radiusFraction;
  check('typing a centre opens a box of the default size',
    radiusBefore > 0 && radiusBefore < 0.5, radiusBefore);
  set('feature-search', '雨海');
  fire('feature-search', 'change');
  await tick();
  check('picking a landform moves the selection box to it',
    Math.abs(T.state.selection.lon + 14.91) < 1e-9 &&
    Math.abs(T.state.selection.lat - 34.72) < 1e-9,
    JSON.stringify(T.state.selection));
  check('and keeps the size it had',
    T.state.selection.radiusFraction === radiusBefore, T.state.selection.radiusFraction);
  check('the status line reports where it is',
    /34\.720°N/.test(document.getElementById('status').textContent) &&
    /14\.910°W/.test(document.getElementById('status').textContent),
    document.getElementById('status').textContent);
  check('a search matches the English name too',
    T.matchFeatures('clavius').length === 1);

  console.log('');
  console.log('generating a close-up refreshes its labels');
  /* The first press has to render at the 物理分辨率 the reader chose.  It used
     to render an aiming preview of the box (a few hundred pixels) and only the
     second press, 更新模拟, honoured the setting -- which read as "the highest
     resolution is ignored the first time". */
  set('out-kmpx-preset', '0.25');
  fire('out-kmpx-preset', 'change');
  const chosen = T.renderPixels();
  /* The regression this catches: acceptRender used to call a dead helper that
     threw, so every render after the first left the page half-updated -- the
     resolution line stale and, because the throw happened before the label
     refresh, the *old* frame's names drawn on the new picture. */
  const annotationsBefore = fetchCalls.filter((c) => c.url === '/api/annotations').length;
  fire('generate', 'click');
  await new Promise((resolve) => setTimeout(resolve, 400));
  const regionCall = fetchCalls.filter((c) => c.url === '/api/region').length;
  const annotationCalls2 = fetchCalls.filter((c) => c.url === '/api/annotations');
  check('生成模拟 rendered a close-up', regionCall >= 1, regionCall);
  {
    const first = fetchCalls.filter((c) => c.url === '/api/region').pop();
    check('the first press renders at the chosen physical resolution',
      first.body.output.width === chosen.width &&
      first.body.output.height === chosen.height,
      `${first.body.output.width}×${first.body.output.height} vs ` +
      `${chosen.width}×${chosen.height}`);
  }
  /* Once there is a frame to update, the same button offers to update it --
     under the name the status line uses when it asks the reader to press it. */
  check('the button becomes 更新模拟 after the first render',
    document.getElementById('generate').textContent.trim() === '更新模拟',
    document.getElementById('generate').textContent);

  /* The label panel's two new buttons: every class at once, and a reset that
     puts every annotation setting back where it started. */
  const boxes = () => {
    const byId = new Map();
    for (const box of document.querySelectorAll('#label-classes input')) byId.set(box.id, box);
    return [...byId.values()];
  };
  /* The families the page starts with switched off (plus 月溪, which an earlier
     check in this file turns off and does not turn back on). */
  // Read the key from the id: an earlier check wrote a boolean into one of the
  // stub's value fields, which a browser would not do.
  const offByDefault = boxes().filter((box) => !box.checked)
    .map((box) => String(box.id).replace('class-', '')).sort();
  check('the classes start with the big families off and the rest on',
    boxes().length === T.LAND_CLASSES.length && boxes().length >= 20 &&
    offByDefault.join(',') ===
      'basin,dome,imp,impact,other,pit,pyroclastic,rima,statio',
    `${boxes().length} classes, off: ${offByDefault.join(',')}`);
  /* The row is ordered by what is on to begin with: a switch that shows nothing
     until a close-up should not be the first thing in the row.  Read the order
     off the host's children -- that is the order they were appended in, which
     is the order a browser lays out. */
  const rowOrder = document.getElementById('label-classes').children
    .map((chip) => String(chip.children[0].id).replace('class-', ''));
  // Against the *page's* defaults, not the current state: an earlier check in
  // this file switches 月溪 off and leaves it off.
  const defaults = T.DEFAULT_OFF_CLASSES;
  const firstOff = rowOrder.findIndex((key) => defaults.includes(key));
  check('and the switches that start off are last in the row',
    firstOff > 0 &&
    rowOrder.slice(0, firstOff).every((key) => !defaults.includes(key)) &&
    rowOrder.slice(firstOff).every((key) => defaults.includes(key)),
    rowOrder.join(','));
  // Another check upstream left one class off, so start from a known state.
  T.setAllClasses(true);
  check('with every class on the button offers the other way',
    boxes().every((box) => box.checked) &&
    document.getElementById('classes-all').textContent === '全不选',
    document.getElementById('classes-all').textContent);
  fire('classes-all', 'click');
  check('全不选 turns them all off and says so',
    boxes().every((box) => !box.checked) &&
    document.getElementById('classes-all').textContent === '全选',
    document.getElementById('classes-all').textContent);
  fire('classes-all', 'click');
  check('and pressing it again turns them all on',
    boxes().every((box) => box.checked) &&
    document.getElementById('classes-all').textContent === '全不选',
    document.getElementById('classes-all').textContent);
  set('label-density', 0.9);
  set('label-font', 22);
  document.getElementById('mark-equator').checked = true;
  fire('label-reset', 'click');
  /* A reset restores the page's *default* class set, eight families off and the
     rest on -- not "everything on", which would be a different page. */
  {
    const off = boxes().filter((box) => !box.checked)
      .map((box) => String(box.id).replace('class-', '')).sort();
    check('标注设置复位 brings back the default class set',
      off.join(',') === 'basin,dome,imp,impact,other,pit,pyroclastic,statio',
      off.join(','));
  }
  /* Touching a class or an overlay brings the label switch up with it: the
     change would be invisible otherwise. */
  {
    T.state.labelsOn = false;
    document.getElementById('annotate').setAttribute('aria-pressed', 'false');
    set('class-mare', false);
    fire('class-mare', 'change');
    check('changing a class switches the labels on',
      T.state.labelsOn === true &&
      document.getElementById('annotate').getAttribute('aria-pressed') === 'true');
    T.state.labelsOn = false;
    document.getElementById('annotate').setAttribute('aria-pressed', 'false');
    set('mark-equator', true);
    fire('mark-equator', 'change');
    check('and so does switching a geometric overlay on',
      T.state.labelsOn === true,
      String(T.state.labelsOn));
    set('mark-equator', false);
    fire('mark-equator', 'change');
    T.setAllClasses(true);
  }
  await new Promise((resolve) => setTimeout(resolve, 30));
  check('标注设置复位 puts the density and the overlays back',
    document.getElementById('label-density').value === '0.5' &&
    document.getElementById('label-font').value === '' &&
    document.getElementById('mark-equator').checked === false,
    `${document.getElementById('label-density').value} / ` +
    `${document.getElementById('label-font').value} / ` +
    `${document.getElementById('mark-equator').checked}`);
  check('and asked for the names on the new frame',
    annotationCalls2.length > annotationsBefore &&
    annotationCalls2[annotationCalls2.length - 1].body.frame === 'region',
    JSON.stringify(annotationCalls2.map((c) => c.body.frame)));
  /* 事件预报: the second tab of the processing column. */
  // The stub's registry keeps what createElement made, and the page builds the
  // chips once at init and again here, so count the distinct ids.
  const kindChips = () => {
    const byId = new Map();
    for (const chip of document.querySelectorAll('#event-kinds .mark-chip')) {
      byId.set(chip.id, chip);
    }
    return [...byId.values()];
  };
  fire('proc-tab-events', 'click');
  await new Promise((resolve) => setTimeout(resolve, 80));
  check('the forecasting pane is the one on screen',
    document.getElementById('proc-events').hidden === false &&
    document.getElementById('proc-animation').hidden === true);
  check('the kind switches are single-choice pills',
    kindChips().length === 7 && T.state.eventKind === 'full_moon',
    String(kindChips().length));
  const eventBody = fetchCalls.filter((c) => c.url === '/api/events').pop();
  check('and opening it asks the server for this year',
    !!eventBody && eventBody.body.kind === 'full_moon' &&
    /^\d{4}-01-01/.test(eventBody.body.start),
    JSON.stringify(eventBody && eventBody.body));
  check('the range boxes default to the current year',
    /^\d{4}-01-01T00:00$/.test(document.getElementById('events-start').value) &&
    /^\d{4}-12-31T23:59$/.test(document.getElementById('events-end').value),
    document.getElementById('events-start').value);
  {
    // 同步所选时间: the range box copies the instant the disk is modelling.
    set('when', '2026-05-25T15:30');
    set('events-start', '2026-01-01T00:00');
    fire('events-start-sync', 'click');
    check('同步所选时间 copies the observer instant into the range',
      document.getElementById('events-start').value === '2026-05-25T15:30',
      document.getElementById('events-start').value);
    set('events-end', '2026-01-02T00:00');
    fire('events-end-sync', 'click');
    check('and the same for 结束',
      document.getElementById('events-end').value === '2026-05-25T15:30',
      document.getElementById('events-end').value);
  }
  {
    const before = document.getElementById('when').value;
    document.getElementById('event-list').innerHTML = '';
    T.renderEvents([
      { kind: 'full_moon', label: '满月', when_utc: '2026-05-01T12:00:00+00:00',
        altitude_deg: 40.2, detail: '地心距 390,000 km', observable: true },
      { kind: 'full_moon', label: '满月', when_utc: '2026-06-01T03:00:00+00:00',
        altitude_deg: -12.0, detail: '地心距 400,000 km', observable: false },
    ]);
    // The stub's registry keeps every element createElement made, so take the
    // two rows this render appended.
    const rows = [...new Set(document.querySelectorAll('.event-row'))].slice(-2);
    check('a row per event, greyed when it is below the horizon',
      rows.length === 2 && rows[0].dataset.observable === 'true' &&
      rows[1].dataset.observable === 'false',
      `${rows.length} rows`);
    rows[0].fire('dblclick', { type: 'dblclick' });
    check('and double-clicking sets the time',
      document.getElementById('when').value !== before &&
      /^\d{4}-05-01T/.test(document.getElementById('when').value),
      `${before} -> ${document.getElementById('when').value}`);
    document.getElementById('when').value = before;
  }
  fire('proc-tab-events', 'click');
  check('and switching back brings the forecast out again',
    document.getElementById('proc-events').hidden === false &&
    document.getElementById('proc-animation').hidden === true);

  /* 动画: the column's second pane, a background job, and the same /api/cancel.
     后期处理 is no longer a tab at all -- it is a section of its own under the
     telescope, so it is never hidden and never takes the column's space. */
  fire('proc-tab-animation', 'click');
  check('后期处理 is its own always-visible section now',
    !document.getElementById('proc-controls').hidden &&
    !document.getElementById('proc-panel').hidden);
  check('and the column holds the forecast and the animation',
    document.getElementById('proc-animation').hidden === false &&
    document.getElementById('proc-events').hidden === true);
  check('and the animation range opens on a window around now',
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(document.getElementById('anim-start').value) &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(document.getElementById('anim-end').value),
    document.getElementById('anim-start').value);
  check('its resolution menu is the full disk own table of steps',
    document.getElementById('anim-preset').options.length ===
    document.getElementById('disk-preset').options.length,
    `${document.getElementById('anim-preset').options.length} vs ` +
    `${document.getElementById('disk-preset').options.length}`);
  check('时间步长 is a list of common steps that can still be typed into',
    /id="anim-step"[^>]*list="anim-step-presets"/.test(html) &&
    ['1', '5', '10', '15', '30', '60']
      .every((value) => new RegExp(`<option value="${value}"></option>`).test(html)),
    'datalist');
  {
    /* 地貌 in the animation pane: the request has to carry the projection
       knobs, and HDR has to give way because a name is an sRGB mark. */
    set('anim-annotated', true);
    fire('anim-annotated', 'change');
    const ann = T.animationAnnotationsPayload();
    check('地貌 sends a complete annotation block',
      ann.on === true && (ann.classes === null || Array.isArray(ann.classes)) &&
      ann.font_px > 0 && ann.class_colors && 'crater' in ann.class_colors &&
      ann.marker_stroke === '#0c0e14' && ann.marker_stroke_width === 1,
      JSON.stringify(ann));
    check('and it is the same block the run will post',
      T.animationRequest().annotations.on === true);
    check('地貌 turns HDR off and disables it',
      document.getElementById('anim-hdr').disabled === true &&
      document.getElementById('anim-hdr').checked === false);
    set('anim-annotated', false);
    fire('anim-annotated', 'change');
    check('turning it off gives HDR back',
      document.getElementById('anim-hdr').disabled === false);
  }
  {
    const before = fetchCalls.filter((c) => c.url === '/api/animation').length;
    fire('anim-run', 'click');
    await new Promise((resolve) => setTimeout(resolve, 60));
    const calls = fetchCalls.filter((c) => c.url === '/api/animation');
    const body = calls[calls.length - 1].body;
    check('开始动画 posts the job to /api/animation',
      calls.length === before + 1 && !!body.start && !!body.end &&
      body.step_seconds > 0 && body.output.width > 0 &&
      !!body.earthshine && !!body.hdr && body.hdr.on === false,
      JSON.stringify({ start: body.start, end: body.end,
                       step: body.step_seconds, width: body.output.width }));
    await new Promise((resolve) => setTimeout(resolve, 900));
    check('the page polls it and reports the frames',
      fetchCalls.some((c) => c.url === '/api/animation/3') &&
      /8\/8|完成/.test(document.getElementById('anim-status').textContent),
      document.getElementById('anim-status').textContent);
    check('and the progress is a bar, with the count under it',
      document.getElementById('anim-progress').hidden === false &&
      document.getElementById('anim-progress-bar').style.width === '100.0%' &&
      !/\d{2}:\d{2}/.test(document.getElementById('anim-status').textContent),
      `${document.getElementById('anim-progress-bar').style.width} / ` +
      document.getElementById('anim-status').textContent);
    const cancels = fetchCalls.filter((c) => c.url === '/api/cancel').length;
    fire('anim-cancel', 'click');
    await new Promise((resolve) => setTimeout(resolve, 30));
    check('终止 goes through the same /api/cancel as a single frame',
      fetchCalls.filter((c) => c.url === '/api/cancel').length === cancels + 1);
    check('and a finished run puts 生成动画 back on the button',
      document.getElementById('anim-run').textContent.trim() === '生成动画',
      document.getElementById('anim-run').textContent);

    /* 立即保存: while the run is going the same button stops after the frame
       in flight and keeps the frames already rendered; it is a different
       endpoint from 终止, which throws the file away. */
    fire('anim-run', 'click');
    await new Promise((resolve) => setTimeout(resolve, 60));
    check('while a run is going the button says 立即保存',
      document.getElementById('anim-run').textContent.trim() === '立即保存',
      document.getElementById('anim-run').textContent);
    const finishes = fetchCalls.filter((c) => /\/finish$/.test(c.url)).length;
    fire('anim-run', 'click');
    await new Promise((resolve) => setTimeout(resolve, 60));
    check('pressing it asks the job to keep what it has',
      fetchCalls.filter((c) => /\/finish$/.test(c.url)).length === finishes + 1 &&
      /\/finish$/.test(fetchCalls.filter((c) => /\/finish$/.test(c.url)).pop().url),
      JSON.stringify(fetchCalls.filter((c) => /\/finish$/.test(c.url)).map((c) => c.url)));
    await new Promise((resolve) => setTimeout(resolve, 900));
    check('and the button goes back to 生成动画 afterwards',
      document.getElementById('anim-run').textContent.trim() === '生成动画',
      document.getElementById('anim-run').textContent);
  }
  fire('proc-tab-processing', 'click');

  /* 带 HDR 的整月面保存: JPEG only, and written by the server, because a gain
     map is a second JPEG spliced inside the first and no canvas can do that. */
  set('disk-format', 'png');
  fire('disk-format', 'change');
  check('the HDR switch stays hidden while the format is not JPEG',
    document.getElementById('disk-hdr-label').hidden === true);
  set('disk-format', 'jpg');
  fire('disk-format', 'change');
  check('and appears when it is',
    document.getElementById('disk-hdr-label').hidden === false &&
    document.getElementById('disk-hdr-stops-label').hidden === true);
  set('disk-hdr', true);
  fire('disk-hdr', 'change');
  check('with its range beside it once it is on',
    document.getElementById('disk-hdr-stops-label').hidden === false);
  {
    T.state.diskExport = null;
    set('disk-px', 64);
    await T.saveDisk();
    await tick();
    const request = fetchCalls.filter((c) => c.url === '/api/disk').pop();
    check('the disk render is asked for the gain map',
      !!request.body.hdr && request.body.hdr.on === true &&
      request.body.hdr.stops === 3, JSON.stringify(request.body.hdr));
    const encode = fetchCalls.filter((c) => c.url === '/api/hdr/jpeg').pop();
    check('and the composed base and the gain map go to the encoder',
      !!encode && /^data:image\/png/.test(encode.body.base) &&
      /^data:image\/png/.test(encode.body.gain) &&
      !!encode.body.metadata && Array.isArray(encode.body.shift),
      JSON.stringify(encode && Object.keys(encode.body)));
  }
  set('disk-hdr', false);
  fire('disk-hdr', 'change');
  set('disk-format', 'png');
  fire('disk-format', 'change');
  T.state.diskExport = null;
  T.state.diskHdr = null;

  /* 目镜: a drawn mask over the processed picture, and only over that one. */
  T.setTrainTab('eyepiece');
  check('the train strip has three panes now',
    document.getElementById('train-eyepiece').hidden === false &&
    document.getElementById('train-telescope').hidden === true &&
    document.getElementById('train-camera').hidden === true);
  {
    const before = T.eyepieceSettings();
    check('the mask is off until it is asked for', before.on === false,
      JSON.stringify(before));
    set('eyepiece-on', true);
    set('eyepiece-falloff', 2.4);
    set('eyepiece-distortion', 0.1);
    const wanted = T.eyepieceSettings();
    check('and then carries the falloff, the field and the distortion',
      wanted.on && Math.abs(wanted.falloff - 2.4) < 1e-9 &&
      Math.abs(wanted.field - 1) < 1e-9 &&
      Math.abs(wanted.distortion - 0.1) < 1e-9, JSON.stringify(wanted));
    const target = document.createElement('canvas');
    target.width = 64;
    target.height = 64;
    const touched = T.applyEyepiece(target);
    check('applying it returns the same canvas, painted',
      touched === target && touched.width === 64, String(touched.width));
    set('eyepiece-on', false);
  }
  /* 目镜视场遮罩 is a switch, and it has to do something when it is thrown: for
     a round it was the one control in that pane that did nothing (the fields
     under it did, which is how a dead switch can sit there looking fine).  It
     also has to show itself, and the mask is drawn on the processed frame --
     so turning it on from 几何模拟 brings that pane forward. */
  T.setResultTab('raw');
  {
    // The mask is drawn, not simulated, so throwing the switch may not start
    // anything: it used to call scheduleOutputRender(), which is a ray march
    // half a second later -- the glass appeared behind a 生成中 overlay, and a
    // 2400 px frame cost seconds of rendering to draw something the renderer
    // never sees.
    const before = fetchCalls.length;
    set('eyepiece-on', true);
    fire('eyepiece-on', 'change');
    check('ticking 目镜视场遮罩 shows the pane it is drawn on',
      T.state.resultTab === 'proc', T.state.resultTab);
    // Past the 550 ms debounce, so a scheduled render would have gone out.
    await new Promise((resolve) => setTimeout(resolve, 800));
    check('and it does not ask the server for anything',
      fetchCalls.length === before,
      fetchCalls.slice(before).map((call) => call.url).join(' '));
    set('eyepiece-on', false);
    fire('eyepiece-on', 'change');
    check('and unticking it leaves the panes where they are',
      T.state.resultTab === 'proc' && T.eyepieceSettings().on === false,
      T.state.resultTab);
  }

  /* Every axis control goes back to the value the markup gave it on a
     double-click, and the change has to reach the server the way a drag does. */
  {
    set('gain', 2.4);
    fire('gain', 'input');
    set('sharpen-radius', 5.5);
    fire('sharpen-radius', 'dblclick');
    set('label-density', 1);
    fire('label-density', 'dblclick');
    // reprocess() is debounced by 90 ms, so the request the double-click caused
    // is only in the log once that has run.
    await new Promise((resolve) => setTimeout(resolve, 250));
    const sent = fetchCalls.filter((c) => c.url === '/api/process').pop();
    check('double-clicking a slider puts it back where the page found it',
      document.getElementById('sharpen-radius').value === '1.2' &&
      document.getElementById('label-density').value === '0.5' &&
      (!sent || sent.body.processing.sharpen_radius_px === 1.2),
      `${document.getElementById('sharpen-radius').value} / ` +
      `${document.getElementById('label-density').value} / ` +
      `${sent && sent.body.processing.sharpen_radius_px}`);
    set('gain', 1);
    fire('gain', 'input');
    set('label-density', 0.5);
  }

  /* A class switch is a "start again" for that family: the names the reader
     dragged or put away in it come back where the catalogue puts them, while
     the other classes keep their edits. */
  {
    T.labelEdits.disk['Tycho'] = { dx: 12, dy: -8 };
    T.labelClass.disk['Tycho'] = 'crater';
    T.labelHidden.disk.add('Copernicus');
    T.labelClass.disk['Copernicus'] = 'crater';
    T.labelEdits.disk['Mare Imbrium'] = { dx: 3, dy: 3 };
    T.labelClass.disk['Mare Imbrium'] = 'mare';
    set('class-crater', false);
    fire('class-crater', 'change');
    set('class-crater', true);
    fire('class-crater', 'change');
    check('switching a class back on gives its own names back',
      !T.labelEdits.disk['Tycho'] && !T.labelHidden.disk.has('Copernicus'),
      JSON.stringify(Object.keys(T.labelEdits.disk)));
    check('and leaves the other classes alone',
      T.labelEdits.disk['Mare Imbrium'] !== undefined,
      JSON.stringify(Object.keys(T.labelEdits.disk)));

    /* 标注设置复位 throws away every manual edit, in both pictures, and puts
       the ruler back where it starts -- it is the same kind of work. */
    T.labelEdits.region['Clavius'] = { dx: -4, dy: 4 };
    T.labelClass.region['Clavius'] = 'crater';
    T.labelHidden.region.add('Plato');
    T.ruler.x = 0.5;
    T.ruler.km = 777;
    fire('label-reset', 'click');
    check('标注设置复位 clears the manual edits as well',
      !Object.keys(T.labelEdits.disk).length && !Object.keys(T.labelEdits.region).length &&
      !T.labelHidden.region.size && T.ruler.km === 0 && T.ruler.x === 0.06,
      JSON.stringify({ disk: T.labelEdits.disk, region: T.labelEdits.region,
                       km: T.ruler.km, x: T.ruler.x }));
  }

  /* The two panel resets use the same snapshot: what the markup said. */
  set('roughness', 30);
  set('noise-poisson', 0.7);
  fire('output-reset', 'click');
  fire('proc-reset', 'click');
  await new Promise((resolve) => setTimeout(resolve, 60));
  check('模拟设置 / 后期处理 resets put their panels back',
    document.getElementById('roughness').value === '0' &&
    document.getElementById('noise-poisson').value === '0' &&
    document.getElementById('photometric-model').value === 'lunar_lambert',
    `${document.getElementById('roughness').value} / ` +
    `${document.getElementById('noise-poisson').value} / ` +
    `${document.getElementById('photometric-model').value}`);
  check('and every reset button is the word Reset, not a glyph',
    [...document.querySelectorAll('button.reset')].length >= 4 &&
    [...document.querySelectorAll('button.reset')]
      .every((button) => button.textContent.trim() === 'Reset' &&
        !/<svg/.test(button.innerHTML)),
    [...document.querySelectorAll('button.reset')]
      .map((button) => button.textContent).join('|'));
  check('the render id travels with the label request',
    annotationCalls2[annotationCalls2.length - 1].body.render_id === T.state.renderId,
    annotationCalls2[annotationCalls2.length - 1].body.render_id);
  // The page reports its own failures in the status line, not on the console,
  // so that line is where a half-finished render shows up.
  const status = document.getElementById('status').textContent;
  check('the page did not report an error while rendering',
    !/失败|not defined|undefined|is not a/.test(status), status);
  check('the readout under the picture was written',
    document.getElementById('result-info').textContent.length > 4,
    document.getElementById('result-info').textContent);

  console.log('');
  console.log('the live close-up is locked to the box');
  /* The regression: it used to follow the pointer, and the pointer was mapped
     to image pixels as if the picture filled its box corner to corner -- so its
     contents were offset from the cursor as well as moving with it. */
  const magnifyCanvas = document.getElementById('magnify');
  await T.drawMagnifier();
  const caption = document.getElementById('magnify-info').textContent;
  check('the window names the two sides of the frame',
    /\d+\.\d″ × \d+\.\d″/.test(caption) && /1 px =/.test(caption), caption);
  check('and says nothing about a zoom mode there is no longer',
    !/框选预览|整个月面|显示 \d|半径/.test(caption), caption);
  check('and it drew the picture into the canvas',
    magnifyCanvas._ctx && magnifyCanvas._ctx.drawn.images.length > 0);
  const captionBefore = caption;
  const body = document.getElementById('body-disk');
  body.fire('pointermove', { clientX: 300, clientY: 210, preventDefault() {} });
  await tick();
  await T.drawMagnifier();
  check('moving the pointer does not change it',
    document.getElementById('magnify-info').textContent === captionBefore,
    document.getElementById('magnify-info').textContent);
  /* The window frames the whole box whatever the render's own scale, which is
     the one mode left: a 200 px window over a box 100 image pixels across is
     two screen pixels per image pixel, and the caption has to say so. */
  /* The box is measured in canvas pixels and drawn from image pixels.  They are
     the same size in normal running; when they are not, the window used to crop
     a patch of Moon beside the one the box encloses. */
  {
    const image = T.state.diskImage;
    const scale = document.getElementById('disk').width / image.width;
    const centre = T.magnifierView(300, 300).centreX;
    document.getElementById('disk').width = Math.round(image.width * 2);
    const doubled = T.magnifierView(300, 300).centreX;
    check('the window follows the frame size, not just the canvas size',
      Math.abs(doubled / centre - scale * 0.5) < 0.02 || doubled > centre,
      `${centre} -> ${doubled} (ratio ${scale})`);
    document.getElementById('disk').width = image.width;
  }

  /* The window shows the box's own frame, so its size follows the box's shape:
     a square box gets a square window, a 2:1 box gets a 2:1 window. */
  const square = T.magnifierView(300, 300);
  check('the window takes the shape of the box it is showing',
    Math.abs(square.width / square.height - square.halfW / square.halfH) < 0.02,
    `${square.width}×${square.height}`);
  check('and it fits inside the room it was given',
    square.width <= 300 && square.height <= 300, `${square.width}×${square.height}`);
  /* A wide box in a shallow room: the window keeps the box's shape and only
     the scale changes -- it is never squashed into the room. */
  const aspect = T.state.selection.aspect;
  T.state.selection = { ...T.state.selection, aspect: 0.4 };
  const wide = T.magnifierView(300, 100);
  check('a wide box gets a wide window rather than a squeezed one',
    Math.abs(wide.width / wide.height - 1 / 0.4) < 0.05 && wide.height <= 100,
    `${wide.width}×${wide.height}`);
  T.state.selection = { ...T.state.selection, aspect };
  /* Rotation is the *frame's*, so the drawing turns the image into the box's
     axes rather than turning the window's contents. */
  T.state.selection = { ...T.state.selection, rotationDeg: 30 };
  T.drawMagnifier();
  const turn = (magnifyCanvas._ctx.drawn.transforms || [])
    .filter((entry) => entry[0] === 'rotate').pop();
  /* A reset the reader can see: it has to move the window *and* the box on the
     full disk, which is the thing they were looking at when they pressed it. */
  T.state.selection = { ...T.state.selection, rotationDeg: 40 };
  const diskCtx = document.getElementById('overlay')._ctx;
  const draws = diskCtx.drawn.transforms.length;
  const redraws = T.state.selection.rotationDeg;
  fire('sel-reset-rot', 'click');
  // drawSelection() records the transform it draws the box under, and returns
  // before that when there is no box: more entries is a box redrawn.
  check('旋转归零 redraws the box on the full disk too',
    redraws === 40 && T.state.selection.rotationDeg === 0 &&
    diskCtx.drawn.transforms.length > draws,
    `was ${redraws}, now ${T.state.selection.rotationDeg}, ` +
    `${draws} -> ${diskCtx.drawn.transforms.length}`);
  check('the window is drawn in the box frame, so it redraws when the box turns',
    turn && Math.abs(turn[1] - 30 * Math.PI / 180) < 1e-9, JSON.stringify(turn));
  T.state.selection = { ...T.state.selection, rotationDeg: 0 };
  T.drawMagnifier();

  console.log('');
  console.log('orientation and noise go through /api/process');
  T.state.renderId = '7';
  T.state.resultImage = new FakeImage();
  set('image-rotation', 30);
  fire('image-rotation', 'input');
  await new Promise((resolve) => setTimeout(resolve, 150));
  const processed = fetchCalls.filter((c) => c.url === '/api/process').pop();
  check('the rotation reaches the server', processed && processed.body.processing.rotation_deg === 30,
    processed && JSON.stringify(processed.body.processing));
  check('and the raw frame is asked for in the same round trip', processed.body.raw === true);
  fire('mirror-v', 'click');
  await new Promise((resolve) => setTimeout(resolve, 150));
  check('the mirror reaches the server',
    fetchCalls.filter((c) => c.url === '/api/process').pop().body.processing.mirror === 'vertical',
    fetchCalls.filter((c) => c.url === '/api/process').pop().body.processing.mirror);
  check('and the icon says it is on',
    document.getElementById('mirror-v').getAttribute('aria-pressed') === 'true');
  fire('mirror-h', 'click');
  check('picking the other one turns the first off',
    document.getElementById('mirror-v').getAttribute('aria-pressed') === 'false' &&
    document.getElementById('mirror-h').getAttribute('aria-pressed') === 'true');
  /* 后期处理's own 复位 has to put both the angle and the mirror back -- and the
     fields are what it has to move, because they are what the next request is
     built from.  The little ↺ that used to sit beside 旋转 is gone: the mirror
     moved down to the action row, and one reset for the whole column is what a
     reader reaching for "put the picture back" actually wants. */
  set('image-rotation', 42);
  fire('image-rotation', 'input');
  fire('proc-reset', 'click');
  await new Promise((resolve) => setTimeout(resolve, 150));
  const afterReset = fetchCalls.filter((c) => c.url === '/api/process').pop().body.processing;
  check('复位 puts the angle back and clears the mirror',
    afterReset.rotation_deg === 0 && afterReset.mirror === 'none' &&
    document.getElementById('image-rotation').value === '0' &&
    document.getElementById('image-rotation-num').value === '0.0',
    JSON.stringify({ deg: afterReset.rotation_deg, mirror: afterReset.mirror,
                     slider: document.getElementById('image-rotation').value,
                     typed: document.getElementById('image-rotation-num').value }));
  set('noise-poisson', 0.5);
  fire('noise-poisson', 'input');
  await new Promise((resolve) => setTimeout(resolve, 150));
  const noisy = fetchCalls.filter((c) => c.url === '/api/process').pop().body.processing;
  check('the Poisson slider becomes a full-well count',
    noisy.noise_photons > 1000 && noisy.noise_sigma === 0, JSON.stringify(noisy));
  set('noise-gaussian', 0.5);
  fire('noise-gaussian', 'input');
  await new Promise((resolve) => setTimeout(resolve, 150));
  const grainy = fetchCalls.filter((c) => c.url === '/api/process').pop().body.processing;
  check('the Gaussian slider becomes a sigma', Math.abs(grainy.noise_sigma - 0.03) < 1e-9,
    grainy.noise_sigma);
  // The noise sliders are left at zero for the checks that follow: the Poisson
  // count is drawn from a seeded generator, and a frame with grain in it is not
  // the frame the next block compares against.
  fire('proc-reset', 'click');
  await tick();
  set('image-rotation', 0);
  set('noise-poisson', 0);

  console.log('');
  console.log('分量出图 for the close-up');
  /* Sunlight and earthshine are about twelve magnitudes apart, so a component is
     a different exposure of the frame already rendered -- not a relabelled file.
     Three things have to hold: the request says which half it wants, the picture
     that comes back is that half, and switching back does not leave the previous
     component's bytes on the save button. */
  const regionCallsBefore = fetchCalls.filter((c) => c.url === '/api/region').length;
  T.setResultTab('raw');
  set('region-earthshine', false);
  fire('region-earthshine', 'change');
  await new Promise((resolve) => setTimeout(resolve, 150));
  const componentCall = fetchCalls.filter((c) => c.url === '/api/process').pop();
  check('unticking earthshine re-asks for the frame that is already rendered',
    componentCall.body.component === 'sunlit' && componentCall.body.render_id === '7',
    JSON.stringify({ component: componentCall.body.component,
                     render_id: componentCall.body.render_id }));
  check('and no ray march was asked for',
    fetchCalls.filter((c) => c.url === '/api/region').length === regionCallsBefore,
    `${fetchCalls.filter((c) => c.url === '/api/region').length} / ${regionCallsBefore}`);
  check('and the picture that comes back is that half, not the last one',
    T.state.downloads.proc === sunlitImage, T.state.downloads.proc);
  check('the geometry pane is the same exposure of the same frame',
    T.state.downloads.raw === sunlitImage, T.state.downloads.raw);
  check('and the status line says so',
    /阳光分量/.test(document.getElementById('status').textContent),
    document.getElementById('status').textContent);
  check('the readout names the component it is describing',
    /阳光分量/.test(document.getElementById('result-info').textContent),
    document.getElementById('result-info').textContent);
  // The link follows the pane on screen, so on the geometry tab it must be the
  // *raw* half of this frame -- not the raw half of the previous one, which is
  // what it was before the link was moved after both pictures were in place.
  const link = document.getElementById('download');
  check('the link follows the pane on screen, component included',
    link.href === sunlitImage && link.download === 'moon-geometry-sunlit.png',
    `${link.href} / ${link.download}`);
  T.setResultTab('proc');
  check('and the processed picture is named after the component as well',
    link.href === sunlitImage && link.download === 'moon-processed-sunlit.png',
    `${link.href} / ${link.download}`);
  set('region-earthshine', true);
  fire('region-earthshine', 'change');
  await new Promise((resolve) => setTimeout(resolve, 150));
  check('switching back to 完整 hands back the whole picture, not the component',
    T.state.downloads.proc === regionImage && T.state.downloads.raw === regionImage,
    `${T.state.downloads.proc} / ${T.state.downloads.raw}`);
  check('and the request says so',
    fetchCalls.filter((c) => c.url === '/api/process').pop().body.component === 'image');
  check('and the file name loses the component',
    link.download === 'moon-processed.png', link.download);
  // A fresh render carries the choice as well: /api/region is what 生成模拟
  // calls, and it produces both panes in one march.
  set('region-earthshine', false);
  fire('generate', 'click');
  await new Promise((resolve) => setTimeout(resolve, 500));
  const regionBody = fetchCalls.filter((c) => c.url === '/api/region').pop().body;
  check('a fresh close-up render asks for the chosen component',
    regionBody.component === 'sunlit', JSON.stringify(regionBody.component));
  check('and both panes show that component',
    T.state.downloads.proc === sunlitImage && T.state.downloads.raw === sunlitImage,
    `${T.state.downloads.proc} / ${T.state.downloads.raw}`);
  set('region-earthshine', true);
  fire('region-earthshine', 'change');
  await new Promise((resolve) => setTimeout(resolve, 150));
  check('and the page is back to the whole picture for what follows',
    T.state.downloads.proc === regionImage, T.state.downloads.proc);

  console.log('');
  console.log('the two close-ups share a zoom');
  T.setResultTab('raw');
  T.setZoom('raw', 3);
  check('zooming the geometry pane zooms the processed one',
    T.views.raw.zoom === 3 && T.views.proc.zoom === 3,
    `${T.views.raw.zoom} / ${T.views.proc.zoom}`);
  T.setZoom('proc', 1);
  check('and the other way round',
    T.views.raw.zoom === 1 && T.views.proc.zoom === 1);
  check('the full disk keeps its own zoom', T.views.disk.zoom === 1);

  console.log('');
  console.log('the mini phase');
  const phase = document.getElementById('phase-canvas');
  check('the strip drew the lit disk', phase._ctx && phase._ctx.drawn.fills.length > 0,
    phase._ctx ? JSON.stringify(phase._ctx.drawn.fills) : 'no context');
  check('it is labelled with the phase the report gives',
    /上弦月|First Quarter/.test(document.getElementById('phase-name').textContent),
    document.getElementById('phase-name').textContent);
  check('and with the illuminated percentage, in the grid',
    /69\.6/.test(document.getElementById('phase-illum').textContent),
    document.getElementById('phase-illum').textContent);
  // The strip used to describe the drawing (bright limb angle, sub-solar point)
  // and now describes the observation: the lunar-calendar date, and where the
  // Moon is in the reader's own sky.
  check('and with the lunar-calendar date, in Chinese',
    /农历 初九/.test(document.getElementById('phase-lunar').textContent),
    document.getElementById('phase-lunar').textContent);
  check('with the age beside it',
    /月龄 8\.48/.test(document.getElementById('phase-age').textContent),
    document.getElementById('phase-age').textContent);
  check('and with the apparent size in arcminutes alone',
    document.getElementById('phase-diameter').textContent === '29.89′',
    document.getElementById('phase-diameter').textContent);
  check('and the readout carries both distances behind their own marks',
    /id="mi-dist-geo"/.test(html) && /id="mi-dist-topo"/.test(html) &&
    /class="ref ref-geo"[^>]*><span class="zh">地</.test(html));
  check('and the strip has no distance row of its own',
    !/id="phase-distance"/.test(html));

  check('and with the elongation from the Sun',
    /89\.4/.test(document.getElementById('phase-elongation').textContent),
    document.getElementById('phase-elongation').textContent);
  check('and with the altitude and azimuth',
    /57\.1/.test(document.getElementById('phase-horizon').textContent) &&
    /180\.3/.test(document.getElementById('phase-horizon').textContent),
    document.getElementById('phase-horizon').textContent);
  check('and with the colongitude',
    /45\.2/.test(document.getElementById('phase-colong').textContent),
    document.getElementById('phase-colong').textContent);
  T.state.geometry.bright_limb_position_angle_deg = 270;
  T.drawPhaseStrip();
  check('a waning limb redraws without throwing', true);
  T.state.geometry.bright_limb_position_angle_deg = 89.4;

  console.log('');
  console.log('a divider starts from where the panel actually is');
  /* The regression: the disk and the info row default to `1fr`, and the code
     read the CSS variable with parseFloat -- so "1fr" became 1 and the first
     press on the divider snapped the panel to its 140 px minimum before the
     drag even began.  What it has to read is the panel's drawn size. */
  check('the disk divider reads the drawn width, not "1fr"',
    T.splitterStart(T.SPLITTERS.disk) === 520, T.splitterStart(T.SPLITTERS.disk));
  check('and the horizontal one reads the panel height',
    T.splitterStart(T.SPLITTERS.raw) === 300, T.splitterStart(T.SPLITTERS.raw));
  check('the side columns read their own widths',
    T.splitterStart(T.SPLITTERS.left) === 300 &&
    T.splitterStart(T.SPLITTERS.right) === 620);
  /* The disk shares the centre grid with the two close-ups, so its ceiling is
     that grid minus the splitter minus the close-ups' own minimum -- measured
     against the window instead, a drag could push them off the page. */
  check('the disk divider stops where the close-ups begin',
    T.splitterCeiling(T.SPLITTERS.disk) === 560 - 6 - 140,
    T.splitterCeiling(T.SPLITTERS.disk));
  /* The two parameter columns share the right-hand column, and the bar between
     them sizes the *left* one. */
  check('the two parameter columns have their own divider',
    !!(T.SPLITTERS.panes && T.SPLITTERS.panes.var === '--w-pane-a') &&
    T.splitterStart(T.SPLITTERS.panes) === 280,
    String(T.splitterStart(T.SPLITTERS.panes)));
  check('and it stops where the other column begins',
    T.splitterCeiling(T.SPLITTERS.panes) === 620 - 6 - 200,
    String(T.splitterCeiling(T.SPLITTERS.panes)));

  console.log('');
  console.log('时间微调 nine buttons and 同步设备时间');
  {
    /* The nudges fire a change on the instant field, and auto-refresh would
       re-render the disk on each one; the checks below count fetches. */
    set('auto-refresh', false);
    set('when', '2026-05-25T15:30');
    fire('nudge-fwd-hour', 'click');
    check('+1h moves the instant by an hour',
      document.getElementById('when').value === '2026-05-25T16:30',
      document.getElementById('when').value);
    fire('nudge-back-minute', 'click');
    check('and -1m comes back by a minute',
      document.getElementById('when').value === '2026-05-25T16:29',
      document.getElementById('when').value);
    set('when', '2026-01-31T12:00');
    // The coarsest step on 观测信息 is a week, not a calendar month: a month
    // takes the phase back to where it started, and the calendar-month
    // arithmetic (clamped to the shorter month's end) belongs to the event
    // range's own row, where both ends move together.
    fire('nudge-fwd-week', 'click');
    check('+1w is seven days',
      document.getElementById('when').value === '2026-02-07T12:00',
      document.getElementById('when').value);
    fire('nudge-back-week', 'click');
    check('and -1w comes back seven days',
      document.getElementById('when').value === '2026-01-31T12:00',
      document.getElementById('when').value);
    set('when', '2026-12-31T23:30');
    fire('nudge-fwd-day', 'click');
    check('+1d rolls over the year boundary',
      document.getElementById('when').value === '2027-01-01T23:30',
      document.getElementById('when').value);
    fire('sync-now', 'click');
    check('同步设备时间 writes the clock the zone field names',
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(document.getElementById('when').value),
      document.getElementById('when').value);
    // 时区 always shows which side of Greenwich it is on.
    set('tz', '8');
    fire('tz', 'change');
    check('时区 shows its own sign, +8 rather than 8',
      document.getElementById('tz').value === '+8',
      document.getElementById('tz').value);
    set('tz', '-5');
    fire('tz', 'change');
    check('and for the west too',
      document.getElementById('tz').value === '-5',
      document.getElementById('tz').value);
    set('tz', '8');
    fire('tz', 'change');
    set('when', '2026-05-25T15:30');
    set('auto-refresh', true);
  }

  console.log('');
  console.log('范围微调 moves the end it sits under');
  {
    set('auto-refresh', false);
    // 事件预报 has no nudge row any more: its two ends are typed, and the eight
    // decade/month buttons that used to slide the whole window are gone with
    // it.  The animation's rows are the ones left, and each moves its own end.
    set('anim-start', '2026-05-25T15:30');
    set('anim-end', '2026-05-25T15:40');
    fire('anim-nudge-fwd-quarter', 'click');
    check('生成动画 +15m moves the start and leaves the end alone',
      document.getElementById('anim-start').value === '2026-05-25T15:45' &&
      document.getElementById('anim-end').value === '2026-05-25T15:40',
      `${document.getElementById('anim-start').value} … ` +
      `${document.getElementById('anim-end').value}`);
    fire('anim-nudge-back-day', 'click');
    check('and -1d goes back a day from wherever it now is',
      document.getElementById('anim-start').value === '2026-05-24T15:45',
      document.getElementById('anim-start').value);
    /* The animation has a row under 开始 *and* one under 结束, and each moves
       its own end: pressing +1h under 结束 asks for a longer animation, not for
       the same one shown an hour later. */
    set('anim-start', '2026-05-25T15:30');
    set('anim-end', '2026-05-25T15:40');
    fire('anim-end-nudge-fwd-hour', 'click');
    check('the row under 结束 moves only the end',
      document.getElementById('anim-start').value === '2026-05-25T15:30' &&
      document.getElementById('anim-end').value === '2026-05-25T16:40',
      `${document.getElementById('anim-start').value} … ` +
      `${document.getElementById('anim-end').value}`);
    fire('anim-end-nudge-back-hour', 'click');
    set('auto-refresh', true);
  }

  console.log('');
  console.log('生成动画: 物理分辨率与后期处理');
  {
    /* Two sizes, one question each: 物理分辨率 says how finely the scene is
       sampled, 分辨率 how large the file is.  With 原生 they are the same
       number and nothing is resampled. */
    set('anim-scope', 'disk');
    fire('anim-scope', 'change');
    set('anim-px', 1200);
    fire('anim-px', 'change');
    set('anim-kmpx-preset', 'native');
    fire('anim-kmpx-preset', 'change');
    check('原生 renders at the size the file is written at',
      T.animationRenderSize().width === 1200 && T.animationOutputPixels() === 1200,
      `${JSON.stringify(T.animationRenderSize())} vs ${T.animationOutputPixels()}`);
    check('and the request says so',
      T.animationRequest().output.width === 1200 &&
      T.animationRequest().output.height === 1200 &&
      JSON.stringify(T.animationRequest().scale_to) === '[1200,1200]',
      JSON.stringify(T.animationRequest().output) + ' ' +
      JSON.stringify(T.animationRequest().scale_to));

    /* A coarse plate scale resolves less than the file's size, so the render
       gets smaller and the server scales it up -- that is the whole point of
       having the two numbers. */
    const kmpx = T.animationSkyKm() / 300;
    set('anim-kmpx-preset', 'custom');
    fire('anim-kmpx-preset', 'change');
    set('anim-kmpx', kmpx);
    fire('anim-kmpx', 'change');
    const request = T.animationRequest();
    check('a coarse 物理分辨率 renders smaller than the file',
      request.output.width < 1200 && request.output.width >= 64 &&
      request.scale_to[0] === 1200,
      `${request.output.width} -> ${JSON.stringify(request.scale_to)}`);
    check('and the pane says what it is doing',
      /渲染 \d+ px → 输出 1200 px/.test(
        document.getElementById('anim-scale-note').textContent),
      document.getElementById('anim-scale-note').textContent);

    /* 后期处理 ends in a display image, so there is no linear frame left for
       an HDR file: the switch disables the other one instead of quietly
       ignoring it. */
    set('anim-hdr', true);
    set('anim-process', true);
    fire('anim-process', 'change');
    check('应用后期处理 turns HDR off and disables it',
      document.getElementById('anim-hdr').disabled === true &&
      document.getElementById('anim-hdr').checked === false,
      `${document.getElementById('anim-hdr').disabled}`);
    check('and it travels with the request',
      T.animationRequest().apply_processing === true &&
      T.animationRequest().hdr.on === false &&
      typeof T.animationRequest().processing === 'object' &&
      typeof T.animationRequest().atmosphere === 'object',
      JSON.stringify(T.animationRequest().apply_processing));
    set('anim-process', false);
    fire('anim-process', 'change');
    check('turning it off gives HDR back',
      document.getElementById('anim-hdr').disabled === false);
    set('anim-kmpx-preset', 'native');
    fire('anim-kmpx-preset', 'change');
  }

  console.log('');
  console.log('物理分辨率：菜单档位跟着本地数据下限走');
  {
    /* The regression: the 0.118 km/px row is the *published* LOLA pixel size,
       the floor the page enforces is the measured one (0.1185 here).  Picking
       the row clamped the number, the clamped number matched no option, and
       the menu fell to 自定义 with a second field beside it. */
    const saved = T.LIMITS.terrain_km_per_px;
    T.LIMITS.terrain_km_per_px = 0.11845058759433393;
    T.syncKmPxPresets();
    const select = document.getElementById('out-kmpx-preset');
    const lola = [...select.options].find((option) => /LOLA/.test(option.textContent || ''));
    check('the LOLA row carries the measured floor, not the published 0.118',
      !!lola && lola.value === '0.1185', JSON.stringify(lola && lola.value));
    set('out-kmpx-preset', lola.value);
    fire('out-kmpx-preset', 'change');
    check('picking it leaves one field and keeps the menu on the row',
      document.getElementById('out-kmpx').hidden === true && select.value === '0.1185',
      `${document.getElementById('out-kmpx').hidden} / ${select.value}`);
    const fine = [...select.options].find((option) => option.value === '0.06');
    check('a row finer than the data is disabled rather than silently rewritten',
      !!fine && fine.disabled === true, JSON.stringify(fine && fine.disabled));
    set('out-kmpx-preset', 'disk');
    fire('out-kmpx-preset', 'change');
    T.LIMITS.terrain_km_per_px = saved;
  }

  console.log('');
  console.log('the ruler is an object on the picture');
  /* It used to be a caption in the corner: fixed place, fixed length, drawn in
     screen pixels so an export got a differently-sized one.  It is now one of
     the 几何标注 switches, with a place on the *frame*, a length in kilometres,
     a drag on each end, and a × of its own. */
  {
    set('mark-ruler', true);
    fire('mark-ruler', 'change');
    const drawn = document.getElementById('labels-disk')._ctx.drawn;
    check('switching it on draws a length on the Moon',
      drawn.texts.some((entry) => /^\d+(\.\d+)? (km|m)$/.test(entry.text)),
      JSON.stringify(drawn.texts.map((entry) => entry.text).slice(-3)));
    const bar = evalIn(sandbox, 'rulerGeometry(diskGeom(), 1000, 1000)');
    check('and the bar is a length in kilometres, placed on the frame',
      bar && bar.length > 8 && bar.left >= 0 && bar.top > 0,
      JSON.stringify(bar && { left: bar.left, top: bar.top, length: bar.length }));
    const before = bar.length;
    evalIn(sandbox, 'ruler.km = ruler.km * 2');
    const after = evalIn(sandbox, 'rulerGeometry(diskGeom(), 1000, 1000).length');
    check('a longer ruler is a longer bar', Math.abs(after / before - 2) < 1e-9,
      `${before} -> ${after}`);
    check('and the length it prints is the one it is',
      evalIn(sandbox, 'rulerText(ruler.km)') !== evalIn(sandbox, 'rulerText(ruler.km / 2)'));
    /* The × is the switch: pressing it has to leave the control off, or the
       reader is looking at a button that disagrees with the picture.  (The chip
       it greys is found with `closest`, which the DOM stub does not have, so
       what is asserted here is the state `syncMarkChips` reads.) */
    evalIn(sandbox, 'ruler.selected = true');
    T.state.labelsOn = true;
    evalIn(sandbox, 'hideBadgeTarget("ruler", {})');
    check('the × takes the ruler off the picture',
      document.getElementById('mark-ruler').checked === false &&
      evalIn(sandbox, 'rulerOn()') === false);
  }

  console.log('');
  console.log('a name can be nudged and put away, one at a time');
  {
    T.state.labelsOn = true;
    T.state.labels.disk = { frame: { width: 1000, height: 1000 }, font_px: 13,
      items: [
        { name_en: 'Clavius', name_zh: '克拉维斯', label: '克拉维斯', class: 'crater',
          x: 400, y: 400, radius_px: 20, labelled: true, label_x: 420, label_y: 395,
          label_w: 60, label_h: 14, leader: false, outline: null },
        { name_en: 'Plato', name_zh: '柏拉图', label: '柏拉图', class: 'crater',
          x: 600, y: 300, radius_px: 18, labelled: true, label_x: 620, label_y: 295,
          label_w: 50, label_h: 14, leader: false, outline: null },
      ] };
    const moved = evalIn(sandbox, '(() => { labelEdits.disk.Clavius = { dx: 12, dy: -7 };'
      + ' const plan = applyLabelEdits(state.labels.disk, "disk");'
      + ' const item = plan.items.find((one) => one.name_en === "Clavius");'
      + ' return [item.x, item.y, item.label_x]; })()');
    check('a nudge moves the marker of that one name',
      moved[0] === 412 && moved[1] === 393, JSON.stringify(moved));
    check('and its text box with it, or the leader would miss',
      moved[2] === 432, JSON.stringify(moved));
    const hidden = evalIn(sandbox, '(() => { labelHidden.disk.add("Plato");'
      + ' const plan = applyLabelEdits(state.labels.disk, "disk");'
      + ' return plan.items.map((one) => one.name_en); })()');
    check('a name put away is left out of the plan, and only that one',
      hidden.length === 1 && hidden[0] === 'Clavius', JSON.stringify(hidden));
    check('and the edit is keyed by the name, not by the translated caption',
      evalIn(sandbox, 'labelKey(state.labels.disk.items[0])') === 'Clavius');
    evalIn(sandbox, 'delete labelEdits.disk.Clavius; labelHidden.disk.clear()');
  }

  console.log('');
  console.log('the close-up has its own 地貌 switch');
  {
    const layer = document.getElementById('labels-raw');
    set('download-annotated', true);
    fire('download-annotated', 'change');
    T.state.labels.region = { frame: { width: 800, height: 800 }, font_px: 13,
      items: [{ name_en: 'Clavius', name_zh: '克拉维斯', label: '克拉维斯',
                class: 'crater', x: 400, y: 400, radius_px: 20, labelled: true,
                label_x: 420, label_y: 395, label_w: 60, label_h: 14,
                leader: false, outline: null }] };
    T.drawViewLabels('raw');
    const withNames = layer._ctx.drawn.texts.length;
    set('download-annotated', false);
    fire('download-annotated', 'change');
    T.drawViewLabels('raw');
    const withoutNames = layer._ctx.drawn.texts.length;
    T.drawViewLabels('disk');
    const diskTexts = document.getElementById('labels-disk')._ctx.drawn.texts.length;
    check('unticking 地貌 takes the names off the close-up',
      withNames > 0 && withoutNames === 0, `${withNames} -> ${withoutNames}`);
    check('and leaves the full disk exactly as it was', diskTexts > 0, diskTexts);
    set('download-annotated', true);
    fire('download-annotated', 'change');
    T.state.labelsOn = false;
  }

  console.log('');
  console.log('the ruler and a name answer the mouse');
  /* The handlers, not just the functions behind them: a drag is a press, some
     moves and a release, and what this guards against is a gesture that throws
     on the first move or that leaves the drag armed.  The coordinates go
     through the page's own frame -> device mapping, which is the one the
     handlers hit-test with. */
  {
    set('mark-ruler', true);
    fire('mark-ruler', 'change');
    const before = { x: evalIn(sandbox, 'ruler.x'), km: evalIn(sandbox, 'ruler.km') };
    const toClient = (fx, fy) => evalIn(sandbox,
      `(() => { const m = imageToScreen("disk", ${fx}, ${fy});`
      + ' return [m.x, m.y]; })()');
    const point = (fx, fy) => {
      const [x, y] = toClient(fx, fy);
      return { clientX: x, clientY: y, button: 0, pointerId: 1,
               preventDefault() {}, target: null };
    };
    // `fire(id, name)` is for controls; a pointer gesture needs its own event,
    // so these go straight at the element.  (A dropped event has no clientX and
    // every hit test then fails silently -- which is what the first version of
    // this check did.)
    const dispatch = (name, fx, fy) =>
      document.getElementById('body-disk').fire(name, point(fx, fy));
    const bar = evalIn(sandbox, 'rulerGeometry(diskGeom(), DISK.width, DISK.height)');
    dispatch('pointerdown', bar.left + bar.length / 2, bar.top);
    check('pressing the bar starts a ruler drag (not a selection box)',
      evalIn(sandbox, 'overlay.drag && overlay.drag.kind') === 'ruler' &&
      T.state.dragging === null,
      evalIn(sandbox, 'overlay.drag && overlay.drag.kind'));
    const mid = evalIn(sandbox, 'DISK.width * 0.05');
    dispatch('pointermove', bar.left + bar.length / 2 + mid, bar.top);
    dispatch('pointerup', bar.left + bar.length / 2 + mid, bar.top);
    check('dragging it moves it and ends the drag',
      evalIn(sandbox, 'overlay.drag') === null &&
      evalIn(sandbox, 'ruler.x') > before.x,
      `${before.x} -> ${evalIn(sandbox, 'ruler.x')}`);
    check('and a move leaves the length alone',
      Math.abs(evalIn(sandbox, 'ruler.km') - before.km) < 1e-9,
      `${before.km} -> ${evalIn(sandbox, 'ruler.km')}`);
    const tip = evalIn(sandbox, 'rulerGeometry(diskGeom(), DISK.width, DISK.height)');
    dispatch('pointerdown', tip.right, tip.top);
    dispatch('pointermove', tip.right + mid, tip.top);
    dispatch('pointerup', tip.right + mid, tip.top);
    check('dragging an end makes it longer',
      evalIn(sandbox, 'ruler.km') > before.km,
      `${before.km} -> ${evalIn(sandbox, 'ruler.km')}`);

    /* A name: the press has to take the overlay, and the drag has to land in
       `labelEdits` under the feature's own name. */
    T.state.labelsOn = true;
    T.state.labels.disk = { frame: { width: 1000, height: 1000 }, font_px: 13,
      items: [{ name_en: 'Plato', name_zh: '柏拉图', label: '柏拉图', class: 'crater',
                x: 500, y: 500, radius_px: 20, labelled: true, label_x: 520,
                label_y: 495, label_w: 50, label_h: 14, leader: false,
                outline: null }] };
    T.drawViewLabels('disk');
    dispatch('pointerdown', 500, 500);
    check('pressing a name takes the press',
      evalIn(sandbox, 'overlay.drag && overlay.drag.kind') === 'label',
      evalIn(sandbox, 'overlay.drag && overlay.drag.kind'));
    dispatch('pointermove', 500 + mid, 500 + mid);
    dispatch('pointerup', 500 + mid, 500 + mid);
    const edit = evalIn(sandbox, 'labelEdits.disk.Plato');
    check('and dragging it records a nudge under its own name',
      edit && edit.dx > 0 && edit.dy > 0, JSON.stringify(edit));
    check('the picture really moved with it',
      evalIn(sandbox, 'applyLabelEdits(state.labels.disk, "disk")'
        + '.items[0].x') > 500);
    evalIn(sandbox, 'delete labelEdits.disk.Plato; ruler.selected = false;'
      + ' overlay.selected = null; state.labelsOn = false');
  }

  console.log('');
  console.log('the close-up form and the save row');
  {
    /* 视场半径 is gone: the box is drawn on the Moon or sized by the
       physical-resolution row, and a third way to say how big it is was one
       control too many. */
    check('the field radius is not in the form',
      !/id="sel-radius"/.test(require('fs').readFileSync(
        require('path').join(__dirname, 'static', 'index.html'), 'utf8')));
    check('and the rotation sits where it was',
      !!document.getElementById('sel-rotation'));
    check('with the way back to the last rendered field beside it',
      !!document.getElementById('sel-sync-last'));
    set('save-format', 'jpg');
    check('the save row defaults to JPG',
      document.getElementById('save-format').value === 'jpg');
    /* 保存图像 writes whichever pane is on screen, and the two panes are two
       different pictures: the geometry render and the processed one. */
    T.state.downloads.raw = 'data:image/png;base64,cmF3';
    T.state.downloads.proc = 'data:image/png;base64,cHJvYw==';
    T.state.labelsOn = false;
    T.setResultTab('raw');
    check('几何模拟 saves the geometry render',
      String(document.getElementById('download').href).indexOf('cmF3') >= 0,
      String(document.getElementById('download').href));
    T.setResultTab('proc');
    check('后期成片 saves the processed one',
      String(document.getElementById('download').href).indexOf('cHJvYw') >= 0,
      String(document.getElementById('download').href));
    T.setResultTab('raw');
  }

  console.log('');
  console.log('the settings file carries the new controls');
  const values = evalIn(sandbox, 'collectSettings()');
  for (const id of ['disk-px', 'disk-format', 'image-rotation', 'image-rotation-num',
                    'noise-poisson', 'noise-gaussian', 'label-shadow', 'back-focus',
                    'label-colour', 'earthshine-gain', 'diffraction',
                    'earth-geometric-albedo', 'earth-samples', 'region-earthshine',
                    'out-px', 'out-ref', 'out-kmpx', 'out-kmpx-preset',
                    'mark-equator', 'mark-nearside', 'disk-earthshine']) {
    check('settings record ' + id, values[id] !== undefined);
  }
  check('and the class switches', values['class-basin'] !== undefined);
  check('and the mirror, which is two latches rather than a value',
    values['image-mirror'] === 'none' || values['image-mirror'] === 'vertical' ||
    values['image-mirror'] === 'horizontal', values['image-mirror']);

  console.log('');
  console.log('导出 asks where to put the file');
  /* 导出 wrote its own copy into the page's settings directory and handed the
     browser a download.  It now opens the browser's own save dialog first, so
     the folder and the file name are the reader's; where there is no such
     dialog it still downloads, because that is what every browser can do. */
  {
    const written = [];
    let asked = null;
    const downloads = () => created.filter((el) => el.download).length;
    sandbox.window.showSaveFilePicker = async (options) => {
      asked = options;
      return {
        name: options.suggestedName,
        createWritable: async () => ({
          write: async (blob) => { written.push(blob); },
          close: async () => {},
        }),
      };
    };
    set('settings-name', '对话框测试');
    const saves = fetchCalls.filter((c) => c.url === '/api/settings/save').length;
    const downloadsBefore = downloads();
    fire('settings-save', 'click');
    await tick(); await tick(); await tick();
    check('the dialog is offered the name in the field',
      asked && asked.suggestedName === '对话框测试.conf',
      JSON.stringify(asked && asked.suggestedName));
    check('and the file is offered as the settings format',
      !!asked && asked.types[0].accept['application/json'].includes('.conf'),
      JSON.stringify(asked && asked.types));
    check('the file the reader picked is the one written',
      written.length === 1 && String(written[0].parts[0]).includes('"schema"'),
      `${written.length} writes`);
    check('and it still keeps its own copy for the list',
      fetchCalls.filter((c) => c.url === '/api/settings/save').length === saves + 1);
    check('with nothing downloaded behind the dialog', downloads() === downloadsBefore,
      `${downloads()} downloads`);
    check('and the status line names the file',
      /对话框测试\.conf/.test(document.getElementById('status').textContent),
      document.getElementById('status').textContent);

    // The same button, in a browser without the dialog.
    delete sandbox.window.showSaveFilePicker;
    set('settings-name', '下载回退');
    fire('settings-save', 'click');
    await tick(); await tick(); await tick();
    check('without a dialog the file goes out as a download',
      downloads() === downloadsBefore + 1, `${downloads()} downloads`);
  }

  console.log('');
  console.log('a name picked out of the list applies that file');
  /* The name field is both "what 导出 writes" and "what 导入 reads": picking a
     saved name and then having to press a button that opens a *file chooser*
     was the confusing part. */
  {
    fire('settings-name', 'focus');
    await tick();
    // The list is a datalist the page fills at run time, so the harness reads
    // what it appended rather than a selector query.
    const names = [...new Set(document.getElementById('settings-files').children
      .map((option) => option.value))];
    check('the saved files are in the list', names.length === 2, names.join(','));
    const loads = () => fetchCalls.filter((c) => c.url === '/api/settings/load').length;
    const before = loads();
    set('settings-name', names[1]);
    fire('settings-name', 'change');
    await tick(); await tick();
    const load = fetchCalls.filter((c) => c.url === '/api/settings/load').pop();
    check('picking one applies it at once, with no second press',
      loads() === before + 1 && load.body.name === names[1],
      JSON.stringify(load && load.body));
    check('and its parameters land in the controls',
      Number(document.getElementById('label-density').value) === 0.42 &&
      document.getElementById('text-bold').checked === true,
      `${document.getElementById('label-density').value} / ` +
      `${document.getElementById('text-bold').checked}`);
    // A name the server does not have is a name for 导出 to create.
    const beforeNew = loads();
    set('settings-name', '还没保存过的名字');
    fire('settings-name', 'change');
    await tick(); await tick();
    check('a name that is not a saved file is left alone', loads() === beforeNew,
      `${loads()} / ${beforeNew}`);
    // Enter is the same gesture as clicking the name in the list.
    set('settings-name', names[0]);
    fire('settings-name', 'keydown', { key: 'Enter', preventDefault() {} });
    await tick(); await tick();
    check('and Enter applies it too', loads() === beforeNew + 1, `${loads()}`);
  }

  console.log('');
  console.log('锁定布局 holds the names still while the type changes');
  /* The avoidance algorithm is a function of the text size, so making the type
     bigger used to redistribute every name.  With the lock on, the plan stays
     exactly as it is and only the glyphs change; anything that changes *which*
     names are being placed is still a fresh layout. */
  {
    if (!T.state.labelsOn) { fire('annotate', 'click'); await tick(); await tick(); }
    set('label-font', '');
    fire('label-font', 'change');
    await tick(); await tick();
    check('there is a layout on screen to lock', !!T.state.labels.disk);
    const annotations = () => fetchCalls.filter((c) => c.url === '/api/annotations').length;
    set('label-lock', true);
    fire('label-lock', 'change');
    await tick();
    check('the lock is a switch the page can read', T.labelLockOn() === true);
    const frozen = T.state.labels.disk;
    const beforeFont = annotations();
    set('label-font', 22);
    fire('label-font', 'change');
    await tick(); await tick();
    check('changing the font does not ask for a new layout',
      annotations() === beforeFont, `${annotations()} / ${beforeFont}`);
    check('the plan is the very same one, so no name moved',
      T.state.labels.disk === frozen && frozen.font_px !== undefined);
    check('while the type follows the field, not the plan',
      Math.abs(T.labelStyleForFrame('disk').fontPx - T.labelFrameFontPx('disk')) < 1e-9 &&
      T.labelStyleFor('disk').fontPx === undefined,
      `${T.labelStyleForFrame('disk').fontPx} / ${frozen.font_px}`);
    /* 刷新字体: the third option between "leave it alone" and "lay it out
       again".  It re-derives the plan's font for the current magnification and
       touches nothing else -- not a box, not a hidden name, not a nudge the
       reader made by hand. */
    {
      T.labelEdits.disk['Clavius'] = { dx: 5, dy: -3 };
      const itemsBefore = T.state.labels.disk.items;
      const callsBefore = annotations();
      fire('label-refresh-font', 'click');
      await tick();
      check('刷新字体 changes the plan font and nothing else',
        T.state.labels.disk.items === itemsBefore &&
        Math.abs(T.state.labels.disk.font_px - T.labelFrameFontPx('disk')) < 1e-9,
        `${T.state.labels.disk.font_px} / ${T.labelFrameFontPx('disk')}`);
      check('and leaves a hand-placed name exactly where it was',
        T.labelEdits.disk['Clavius'] &&
        T.labelEdits.disk['Clavius'].dx === 5 && T.labelEdits.disk['Clavius'].dy === -3,
        JSON.stringify(T.labelEdits.disk['Clavius']));
      check('without asking the server for a new layout',
        annotations() === callsBefore, `${annotations()} / ${callsBefore}`);
      delete T.labelEdits.disk['Clavius'];
    }
    check('and an unlocked style still leaves the size to the plan',
      (set('label-lock', false), fire('label-lock', 'change'),
       evalIn(sandbox, 'labelStyleFor("disk").fontPx') === undefined));
    set('label-lock', true);
    fire('label-lock', 'change');
    await tick();
    const beforeClass = annotations();
    set('class-rima', true);
    fire('class-rima', 'change');
    await tick(); await tick();
    check('a change of what is labelled still re-lays out when locked',
      annotations() > beforeClass, `${annotations()} / ${beforeClass}`);
    check('and that new layout becomes the locked one',
      T.state.labels.disk !== frozen);
    set('class-rima', false);
    fire('class-rima', 'change');
    await tick(); await tick();
    const beforeUnlock = annotations();
    const planBeforeUnlock = T.state.labels.disk;
    set('label-lock', false);
    fire('label-lock', 'change');
    await tick(); await tick();
    check('unlocking does not re-lay out or move a single name',
      annotations() === beforeUnlock &&
      T.state.labels.disk === planBeforeUnlock && T.labelLockOn() === false,
      `${annotations()} / ${beforeUnlock}`);
    set('label-font', '');
    fire('label-font', 'change');
    await tick();
  }

  console.log('');
  console.log('a locked layout scales to the export');
  /* The export re-lays out for its own resolution -- unless the reader locked
     the arrangement, in which case it is scaled, or the file would carry a
     different map from the one on screen. */
  {
    const plan = {
      frame: { width: 1000, height: 1000 },
      font_px: 13,
      items: [{ x: 100, y: 200, radius_px: 8, label_x: 110, label_y: 194,
                label_w: 40, label_h: 16, label: '雨海', labelled: true,
                outline: [[0, 0], [10, 0]] }],
    };
    const scaled = T.scalePlan(plan, 2);
    check('every length in the plan scales, the outline included',
      scaled.items[0].x === 200 && scaled.items[0].label_w === 80 &&
      scaled.items[0].label_h === 32 && scaled.items[0].radius_px === 16 &&
      scaled.font_px === 26 && scaled.frame.width === 2000 &&
      scaled.items[0].outline[1][0] === 20,
      JSON.stringify(scaled.items[0]));
    check('and the original is left alone', plan.items[0].x === 100);
    check('a plan with no label box keeps its nulls',
      T.scalePlan({ frame: plan.frame, font_px: 13,
                    items: [{ x: 1, y: 2, label_x: null, label_y: null }] }, 2)
        .items[0].label_x === null);

    // The export path itself: the frozen layout comes back scaled to the
    // export's resolution, and a frame nobody locked is never reused.
    set('label-lock', true);
    fire('label-lock', 'change');
    await tick();
    if (!T.state.labelsOn) { fire('annotate', 'click'); await tick(); await tick(); }
    set('label-font', '');
    fire('label-font', 'change');
    await tick(); await tick();
    const onScreen = T.state.labels.disk;
    if (!onScreen) {
      check('the frozen plan is available to the export', false, 'no plan on screen');
    } else {
      const width = 2400;
      const forExport = T.lockedDiskPlan(T.state.diskWhen, width);
      const k = width / 1200;                    // the preview canvas is 1200 px
      check('the export gets the locked layout, scaled, not a new one',
        !!forExport && forExport.items.length === onScreen.items.length &&
        Math.abs(forExport.items[0].x - onScreen.items[0].x * k) < 1e-9 &&
        Math.abs(forExport.font_px - onScreen.font_px * k) < 1e-9,
        forExport ? `${forExport.items[0].x} vs ${onScreen.items[0].x * k}` : 'null');
      check('and another instant is laid out afresh instead',
        T.lockedDiskPlan({ at: '2030-01-01T00:00:00' }, width) === null);
    }
    set('label-lock', false);
    fire('label-lock', 'change');
    await tick(); await tick();
  }

  console.log('');
  console.log('the phase name hides a joke');
  {
    /* Eleven taps in quick succession and the page snows.  The count is a time
       window, so taps inside one run of the harness are "consecutive" by
       construction -- which is exactly the case that has to work. */
    const layers = () => document.body.children
      .filter((el) => String(el.className).split(/\s+/).includes('moonfall'));
    const before = layers().length;
    for (let i = 0; i < 10; i += 1) fire('phase-name', 'click');
    check('ten taps on the phase name are still just taps',
      layers().length === before, String(layers().length));
    fire('phase-name', 'click');
    check('the eleventh brings the snow', layers().length === before + 1);
    const layer = layers().pop();
    check('with a whole sky of flakes', layer && layer.children.length >= 100,
      layer ? String(layer.children.length) : 'no layer');
    check('and a character on each one',
      layer && layer.children.every((f) => String(f.textContent).length > 0));
    /* The counter resets, so the joke can be told twice. */
    for (let i = 0; i < 10; i += 1) fire('phase-name', 'click');
    check('and the count starts over afterwards',
      layers().length === before + 1, String(layers().length));
    fire('phase-name', 'click');
    check('so it can snow again', layers().length === before + 2);
  }

  console.log('');
  console.log('no unexpected exceptions');
  check('nothing was written to the console as an error',
    !consoleErrors.length, consoleErrors.join(' | '));

  finish();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
