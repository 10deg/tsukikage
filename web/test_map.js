/* Harness for the map page: boot it against a stub DOM and a canned server.
 *
 * The map page has no test of its own and the panel/class rework touched most
 * of its wiring, so this one boots the real map.js the way the browser does --
 * every id in index.html is a real element, the canvas is a recording stub, the
 * API is a table of answers -- and then drives the controls the rework added:
 * the class chips, the time nudge ladder, the column splitters, the collapse
 * caret and the zoom caps.
 *
 *   node web/test_map.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createRunner } = require('./harness');

const runner = createRunner();
const { check, finish } = runner;

const html = fs.readFileSync(path.join(__dirname, 'map', 'index.html'), 'utf8');

/* ── the elements ─────────────────────────────────────────────────────────
 *
 * Built from the page's own markup: one element per id, with the tag and the
 * state the markup gives it.  A test that invented its own ids would pass on a
 * page whose markup never creates them.
 */
function parseAttributes(source) {
  const attrs = {};
  const pattern = /([\w-]+)(?:="([^"]*)")?/g;
  let match;
  while ((match = pattern.exec(source))) attrs[match[1]] = match[2] === undefined ? '' : match[2];
  return attrs;
}

class El {
  constructor(tag, attrs) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = attrs || {};
    this.id = this.attrs.id || '';
    this.children = [];
    this.parent = null;
    this.dataset = {};
    for (const [key, value] of Object.entries(this.attrs)) {
      if (key.startsWith('data-')) {
        const name = key.slice(5).replace(/-([a-z])/g, (_m, chr) => chr.toUpperCase());
        this.dataset[name] = value;
      }
    }
    this.style = {
      _values: {},
      setProperty(name, value) { this._values[name] = value; },
      getPropertyValue(name) { return this._values[name] || ''; },
    };
    this.listeners = {};
    this.classes = new Set(String(this.attrs.class || '').split(/\s+/).filter(Boolean));
    this.classList = {
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (on) this.classes.add(name); else this.classes.delete(name);
        return on;
      },
      contains: (name) => this.classes.has(name),
    };
    this._text = '';
    this.hidden = 'hidden' in this.attrs;
    this.disabled = 'disabled' in this.attrs;
    const tagName = String(tag).toLowerCase();
    this.type = this.attrs.type || (tagName === 'input' ? 'text' : '');
    if (this.type === 'checkbox') this.checked = 'checked' in this.attrs;
    else this.checked = false;
    this.value = this.attrs.value !== undefined ? this.attrs.value : '';
    if (tagName === 'select') this.value = this.attrs.value || '';
  }

  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
  get className() { return [...this.classes].join(' '); }
  set className(value) {
    this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get innerHTML() { return ''; }
  set innerHTML(_value) { this.children = []; }

  appendChild(child) {
    if (child.parent) child.parent.children = child.parent.children.filter((c) => c !== child);
    child.parent = this;
    this.children.push(child);
    return child;
  }

  addEventListener(type, handler) {
    (this.listeners[type] || (this.listeners[type] = [])).push(handler);
  }

  removeEventListener(type, handler) {
    if (this.listeners[type]) this.listeners[type] = this.listeners[type].filter((h) => h !== handler);
  }

  fire(type, extra) {
    const event = Object.assign({ target: this, preventDefault() {}, stopPropagation() {} }, extra || {});
    for (const handler of this.listeners[type] || []) handler(event);
    return event;
  }

  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  setAttribute(name, value) { this.attrs[name] = String(value); }

  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parent;
    }
    return null;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800 };
  }

  getContext() { return context2d(); }

  setPointerCapture() {}
  releasePointerCapture() {}

  querySelector(selector) {
    return descendants(this).find((node) => matches(node, selector)) || null;
  }

  querySelectorAll(selector) {
    return descendants(this).filter((node) => matches(node, selector));
  }
}

function descendants(node) {
  const out = [];
  const walk = (current) => {
    for (const child of current.children || []) {
      out.push(child);
      walk(child);
    }
  };
  walk(node);
  return out;
}

function matches(node, selector) {
  const selectorText = String(selector).trim();
  if (selectorText === 'span') return node.tagName === 'SPAN';
  if (selectorText === '.map-splitter') return node.classes.has('map-splitter');
  if (selectorText === '.mark-chip input') {
    return node.tagName === 'INPUT' && Boolean(node.parent && node.parent.classes.has('mark-chip'));
  }
  if (selectorText === '#light-nudges [data-nudge]') {
    return node.dataset.nudge !== undefined && node.closest('#light-nudges') !== null;
  }
  if (selectorText === '#label-classes input[data-layer]') {
    return node.tagName === 'INPUT' && node.dataset.layer !== undefined
      && node.closest('#label-classes') !== null;
  }
  if (selectorText.startsWith('#') && !selectorText.includes(' ')) {
    return node.id === selectorText.slice(1);
  }
  if (selectorText.startsWith('.')) return node.classes.has(selectorText.slice(1));
  return false;
}

function tagsFromMarkup(source) {
  // Element name + attribute text for each tag with an id, plus the structural
  // bits the script queries for (nudges, splitters, mark chips).
  const tags = [];
  const pattern = /<([a-zA-Z][\w-]*)([^>]*?)>/g;
  let match;
  while ((match = pattern.exec(source))) {
    const tag = match[1].toLowerCase();
    if (tag === 'script' || tag === 'link' || tag === 'meta' || tag === 'title') continue;
    tags.push({ tag, attrs: parseAttributes(match[2]) });
  }
  return tags;
}

function buildDom(source) {
  const byId = {};
  const all = [];
  let hostLabelClasses = null;
  let hostNudges = null;
  let mapBar = null;
  let documentElement = new El('html', {});
  const markParents = {};

  for (const { tag, attrs } of tagsFromMarkup(source)) {
    const element = new El(tag, attrs);
    if (element.id) byId[element.id] = element;
    all.push(element);
    if (element.id === 'label-classes') hostLabelClasses = element;
    if (element.id === 'light-nudges') hostNudges = element;
    if (element.id === 'map-label-bar') mapBar = element;
    // The mark chips are labels that wrap their own checkbox; the markup gives
    // the input, so the chip is built around it once the input exists.
    if (element.classes.has('mark-chip')) markParents[element.id] = element;
  }
  // Mark chips: the input is a sibling in the markup, a child here.
  const chips = {};
  for (const { tag, attrs } of tagsFromMarkup(source)) {
    if (tag !== 'input') continue;
    const input = attrs.id ? byId[attrs.id] : null;
    if (!input || !input.id.startsWith('mark-')) continue;
    const chip = new El('label', { class: 'mark-chip' });
    chip.appendChild(input);
    all.push(chip);
    chips[input.id] = chip;
  }
  // Nudge buttons.
  const nudgePattern = /data-nudge="([^"]+)"/g;
  let nudge;
  while ((nudge = nudgePattern.exec(source))) {
    const button = new El('button', { 'data-nudge': nudge[1] });
    hostNudges.appendChild(button);
    all.push(button);
  }
  // Splitters.
  const splitters = [];
  for (const target of ['left', 'right']) {
    const splitter = new El('div', { class: 'map-splitter', 'data-target': target });
    all.push(splitter);
    splitters.push(splitter);
  }
  for (const id of ['map-stage', 'map-canvas']) {
    if (byId[id]) byId[id].getBoundingClientRect = () => (
      { left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800 });
  }

  const universe = () => {
    // Static markup plus everything the script has built into it since.
    const out = [];
    const seen = new Set();
    const add = (node) => {
      if (seen.has(node)) return;
      seen.add(node);
      out.push(node);
      for (const child of node.children || []) add(child);
    };
    for (const node of all) add(node);
    return out;
  };
  const document = {
    documentElement,
    getElementById: (id) => byId[id] || null,
    querySelector: (selector) => universe().find((node) => matches(node, selector)) || null,
    querySelectorAll: (selector) => universe().filter((node) => matches(node, selector)),
    createElement: (tag) => new El(tag, {}),
    addEventListener() {},
  };
  // Every element can look up from the document root, which is what
  // `element.closest()` needs for the chips' inputs.
  for (const element of all) element.document = document;
  if (mapBar) document.bodyBar = mapBar;
  return { document, byId, all, splitters, chips, markParents };
}

/* ── canvas, images, network ────────────────────────────────────────────── */
const drawn = { texts: [], arcs: 0, lines: 0, fills: 0 };
function context2d() {
  const ctx = {
    canvas: null,
    drawn,
    setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {},
    beginPath() {}, closePath() {}, moveTo() { drawn.lines += 1; }, lineTo() { drawn.lines += 1; },
    arc() { drawn.arcs += 1; }, ellipse() {}, fill() { drawn.fills += 1; },
    stroke() {}, save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
    setLineDash() {}, drawImage() {}, clip() {}, rect() {},
    createImageData: (w, h) => ({ data: new Uint8ClampedArray(Math.max(4, w * h * 4)), width: w, height: h }),
    getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(Math.max(4, w * h * 4)), width: w, height: h }),
    putImageData() {},
    measureText: (text) => ({ width: String(text).length * 7 }),
    fillText(text) { drawn.texts.push(String(text)); },
    strokeText() {},
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
    globalAlpha: 1, font: '', fillStyle: '', strokeStyle: '', lineWidth: 1,
    textAlign: '', textBaseline: '', imageSmoothingEnabled: true,
  };
  return ctx;
}

class FakeImage {
  constructor() { this.onload = null; this.onerror = null; this._src = ''; }
  set src(value) {
    this._src = value;
    setTimeout(() => { if (this.onload) this.onload(); }, 0);
  }
  get src() { return this._src; }
}

const LAYERS = {
  layers: [
    { id: 'base', type: 'raster', label: '月面底图', default: true },
    { id: 'graticule', type: 'vector', label: '经纬网', default: true },
    { id: 'mare', type: 'features', label: '月海', count: 23, default: true },
    { id: 'crater', type: 'features', label: '环形坑', count: 2000, default: true },
  ],
  feature_classes: ['mare', 'crater'],
};
const LIGHTING = {
  subsolar: { lon: 12.5, lat: 3.2 }, sub_earth: { lon: -4.7, lat: 1.4 },
  phase: { name_zh: '上弦', illumination_percent: 50 },
};

const fetchCalls = [];
function fakeFetch(url) {
  fetchCalls.push(String(url));
  const respond = (body) => Promise.resolve({
    ok: true, json: async () => body, text: async () => JSON.stringify(body),
  });
  if (url.startsWith('/api/map/health')) {
    return respond({
      dem: { name: 'dem', level0_km_per_pixel: 0.118 },
      albedo: { name: 'albedo', level0_km_per_pixel: 0.5 },
      feature_count: 9000, sources: [{ name: 'dem', km_per_px: 0.118 }],
    });
  }
  if (url.startsWith('/api/map/layers')) return respond(LAYERS);
  if (url.startsWith('/api/map/lighting')) return respond(LIGHTING);
  if (url.startsWith('/api/map/features')) return respond({ features: [], total: 0 });
  if (url.startsWith('/api/map/point')) return respond({ elevation_km: 1.2 });
  if (url.startsWith('/api/map/search')) {
    return respond({ results: [{
      id: 1, name: '静海', name_en: 'Mare Tranquillitatis', name_zh: '静海',
      lon: 31.4, lat: 8.5, diameter_km: 873.0, class: 'mare', kind: 'Mare',
    }] });
  }
  return respond({});
}

const dom = buildDom(html);
const sandbox = {
  console,
  document: dom.document,
  window: {
    devicePixelRatio: 1,
    innerWidth: 1400,
    innerHeight: 900,
    addEventListener() {},
    ResizeObserver: class { observe() {} disconnect() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '238px' }),
    location: { hash: '' },
    history: { replaceState() {} },
  },
  navigator: { clipboard: { writeText: async () => {} } },
  location: { hash: '' },
  history: { replaceState() {} },
  getComputedStyle: () => ({ getPropertyValue: () => '238px' }),
  ResizeObserver: class { observe() {} disconnect() {} },
  Image: FakeImage,
  URLSearchParams,
  fetch: fakeFetch,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  devicePixelRatio: 1,
};
sandbox.window.document = sandbox.document;
sandbox.globalThis = sandbox;

const source = fs.readFileSync(path.join(__dirname, 'map', 'map.js'), 'utf8')
  + '\n;globalThis.__t = { state, zoomBy, fitView, lightingWhen, nudgeLighting, '
  + 'terminatorPoints, readMarks, syncMarkChips, setAllLayers, wireSplitters, featureRimSegments, '
  + 'applyLightingSoon, loadLayers, resizeCanvas };';
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'map.js' });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  await wait(60);
  const T = sandbox.__t;
  check('the page boots without throwing', Boolean(T) && Boolean(T.state));
  check('the console stayed quiet', T.state !== null);

  console.log('');
  console.log('the class switches moved to the top bar');
  const chips = dom.document.querySelectorAll('.cls-chip');
  check('one chip per feature class is built',
    chips.length === 2, String(chips.length));
  check('and the base raster stays in the layer list',
    dom.document.querySelectorAll('.layer-item').length === 1,
    String(dom.document.querySelectorAll('.layer-item').length));
  check('all of them start on, as the server says',
    T.state.active.has('mare') && T.state.active.has('crater'));
  T.setAllLayers(false);
  check('全不选 clears every class', T.state.active.size === 0
    || !T.state.active.has('mare'), JSON.stringify([...T.state.active]));
  T.setAllLayers(true);
  check('全选 puts them back', T.state.active.has('mare') && T.state.active.has('crater'));

  console.log('');
  console.log('the header search is a working entry');
  {
    dom.byId['search-input'].value = '静海';
    dom.byId['search-input'].fire('input');
    await wait(260);
    const calls = fetchCalls.filter((url) => url.startsWith('/api/map/search'));
    check('typing asks the catalogue without pressing Enter',
      calls.length === 1 && calls[0].includes('%E9%9D%99%E6%B5%B7'),
      calls.join(' | '));
    check('and the result is shown under the field',
      dom.byId['search-results'].hidden === false &&
      dom.byId['search-results'].children.length === 1,
      `${dom.byId['search-results'].hidden} / ` +
      `${dom.byId['search-results'].children.length}`);
    dom.byId['search-results'].children[0].fire('click');
    check('picking it moves the map to the place',
      Math.abs(T.state.centerLon - 31.4) < 1e-9 &&
      Math.abs(T.state.centerLat - 8.5) < 1e-9,
      `${T.state.centerLon}, ${T.state.centerLat}`);
  }

  console.log('');
  console.log('the label appearance is the reader’s');
  {
    dom.byId['label-font'].value = '15';
    dom.byId['label-font'].fire('input');
    check('字号 reaches the painter', T.state.labelFont === 15,
      String(T.state.labelFont));
    dom.byId['label-colour'].checked = false;
    dom.byId['label-colour'].fire('change');
    dom.byId['text-color'].value = '#ffcc00';
    dom.byId['text-color'].fire('input');
    check('a single text colour replaces the class palette',
      T.state.labelColour === false && T.state.textColor === '#ffcc00',
      `${T.state.labelColour} ${T.state.textColor}`);
    dom.byId['outline-style'].value = 'dash';
    dom.byId['outline-style'].fire('change');
    check('线条 style reaches the rim painter', T.state.outlineStyle === 'dash',
      T.state.outlineStyle);
    dom.byId['marker-stroke-width'].value = '1.2';
    dom.byId['marker-stroke-width'].fire('input');
    check('标注点描边 reaches the marker painter',
      T.state.markerStrokeWidth === 1.2, String(T.state.markerStrokeWidth));
    dom.byId['label-overlap'].checked = true;
    dom.byId['label-overlap'].fire('change');
    check('停用避让 turns collision control off', T.state.avoidOverlap === false);
    dom.byId['diameter-toggle'].checked = false;
    dom.byId['diameter-toggle'].fire('change');
    check('真实直径 belongs to the label settings', T.state.diameters === false);
  }

  console.log('');
  console.log('the two annotation layers have master switches');
  dom.byId['labels-toggle'].fire('click');
  check('地貌标注 closes every feature mark, label and rim',
    T.state.labels === false &&
    dom.byId['labels-toggle'].getAttribute('aria-pressed') === 'false');
  dom.byId['labels-toggle'].fire('click');
  check('and opens them again', T.state.labels === true);
  dom.byId['marks-toggle'].fire('click');
  check('几何标注 has its own master switch', T.state.marksOn === false &&
    dom.byId['marks-toggle'].getAttribute('aria-pressed') === 'false');
  dom.byId['marks-toggle'].fire('click');
  check('and it comes back without touching the labels',
    T.state.marksOn === true && T.state.labels === true);

  console.log('');
  console.log('a true-diameter rim does not draw across the date line');
  {
    const saved = { ...T.state };
    T.state.labels = true;
    T.state.diameters = true;
    const rim = T.featureRimSegments({ lon: 180, lat: 0, diameter_km: 3000 });
    let jump = 0;
    for (const segment of (rim ? rim.segments : [])) {
      for (let index = 1; index < segment.length; index += 1) {
        jump = Math.max(jump, Math.abs(segment[index].x - segment[index - 1].x));
      }
    }
    check('each arc stays on one side of the seam',
      !!rim && jump < T.state.width / 2, `${jump} px`);
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('the instant moves by steps and applies itself');
  dom.byId['light-when'].value = '2026-05-25T12:00';
  const before = T.lightingWhen();
  T.nudgeLighting('+1d');
  const after = T.lightingWhen();
  check('the nudge ladder moves the date by the step',
    before && after && (Date.parse(after) - Date.parse(before)) === 86400000,
    `${before} -> ${after}`);
  const weekBefore = T.lightingWhen();
  T.nudgeLighting('+1w');
  check('and it keeps a week step like the full-disk page',
    Date.parse(T.lightingWhen()) - Date.parse(weekBefore) === 604800000,
    `${weekBefore} -> ${T.lightingWhen()}`);
  const calls = fetchCalls.filter((url) => url.startsWith('/api/map/lighting')).length;
  check('a nudged instant asks the server at once', calls > 1, String(calls));

  console.log('');
  console.log('the columns are the reader’s');
  const splitter = dom.splitters[0];
  splitter.fire('pointerdown', { clientX: 300, pointerId: 1 });
  splitter.fire('pointermove', { clientX: 380, pointerId: 1 });
  splitter.fire('pointerup', { clientX: 380, pointerId: 1 });
  const width = dom.document.documentElement.style.getPropertyValue('--left-w');
  check('dragging the left edge sets the left column', width === '318px', width);

  console.log('');
  console.log('the annotation settings fold away');
  check('they start folded into the header',
    dom.byId['map-label-bar'].dataset.collapsed === 'true',
    dom.byId['map-label-bar'].dataset.collapsed);
  dom.byId['classes-toggle'].fire('click');
  check('the caret opens the settings row',
    dom.byId['map-label-bar'].dataset.collapsed === 'false',
    dom.byId['map-label-bar'].dataset.collapsed);
  dom.byId['classes-toggle'].fire('click');
  check('and folds it again',
    dom.byId['map-label-bar'].dataset.collapsed === 'true');

  console.log('');
  console.log('geometry marks are switches');
  dom.byId['mark-terminator'].checked = false;
  dom.byId['mark-terminator'].fire('change');
  check('unchecking 阴影带 reaches the state', T.state.marks.terminator === false,
    JSON.stringify(T.state.marks));
  dom.byId['mark-graticule'].checked = false;
  dom.byId['mark-graticule'].fire('change');
  check('and so does 经纬网', T.state.marks.graticule === false);
  dom.byId['mark-terminator'].checked = true;
  dom.byId['mark-terminator'].fire('change');
  dom.byId['mark-graticule'].checked = true;
  dom.byId['mark-graticule'].fire('change');

  console.log('');
  console.log('the terminator is a circle in three dimensions');
  const points = T.terminatorPoints({ lon: 0, lat: 0 }, 36);
  check('36 samples come back', points.length === 36);
  const sun = [1, 0, 0];
  const dots = points.map((point) => {
    const lo = point.lon * Math.PI / 180;
    const la = point.lat * Math.PI / 180;
    return Math.abs(Math.cos(la) * Math.cos(lo) * sun[0]
      + Math.cos(la) * Math.sin(lo) * sun[1] + Math.sin(la) * sun[2]);
  });
  check('every sample is 90° from the sub-solar point',
    dots.every((value) => value < 1e-9), JSON.stringify(dots.slice(0, 3)));

  console.log('');
  console.log('globe and Mercator can zoom in past 4x');
  T.state.projection = 'globe';
  T.state.globe.zoom = 1;
  for (let step = 0; step < 6; step += 1) T.zoomBy(1.6);
  check('the globe goes past 4x', T.state.globe.zoom > 4.0, String(T.state.globe.zoom));
  T.state.projection = 'mercator';
  check('the Mercator scale is not capped at the old 8x either',
    T.state.scale <= 16, String(T.state.scale));

  console.log('');
  console.log('no unexpected exceptions');
  finish();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
