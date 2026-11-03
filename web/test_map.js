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
const mapStyle = fs.readFileSync(path.join(__dirname, 'map', 'map.css'), 'utf8');

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
  const body = new El('body', {});
  const document = {
    body,
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
    fillText(text, x, y) { drawn.texts.push({ text: String(text), x, y }); },
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
const fetchBodies = [];
function fakeFetch(url, options) {
  fetchCalls.push(String(url));
  if (options && options.body) {
    try {
      fetchBodies.push({ url: String(url), body: JSON.parse(options.body) });
    } catch (error) {
      fetchBodies.push({ url: String(url), body: null });
    }
  }
  const respond = (body) => Promise.resolve({
    ok: true, json: async () => body, text: async () => JSON.stringify(body),
  });
  if (url.startsWith('/api/map/health')) {
    return respond({
      version: '1.1.0',
      dem: { name: 'dem', level0_km_per_pixel: 0.118 },
      albedo: { name: 'albedo', level0_km_per_pixel: 0.5 },
      feature_count: 9000, sources: [{ name: 'dem', km_per_px: 0.118 }],
    });
  }
  if (url.startsWith('/api/map/layers')) return respond(LAYERS);
  if (url.startsWith('/api/map/lighting')) return respond(LIGHTING);
  if (url.startsWith('/api/map/features')) return respond({ features: [], total: 0 });
  if (url.startsWith('/api/map/photos')) return respond({ photos: [] });
  if (url.startsWith('/api/map/data/download')) {
    return respond({ ok: true, dem: 'ldem_64.tif', fallback: false,
      download: { running: true, last_line: '准备下载…' } });
  }
  if (url.startsWith('/api/map/data')) {
    return respond({ ok: true, dem: 'ldem_64.tif', fallback: false,
      download: { running: false, last_line: '', error: '' } });
  }
  if (url.startsWith('/api/map/point')) return respond({ elevation_km: 1.2 });
  if (url.startsWith('/api/map/profile')) {
    const samples = 32;
    const heights = Array.from({ length: samples },
      (_, index) => Math.sin(index / 5) * 1.2 + 0.4);
    return respond({
      kind: 'line', samples,
      distance_km: heights.map((_, index) => index * 4),
      heights_km: heights,
      min_km: Math.min(...heights), max_km: Math.max(...heights),
      mean_km: 0.4, level: 2, seconds: 0.002,
    });
  }
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
  + 'applyLightingSoon, loadLayers, resizeCanvas, moonToPhoto, photoToSphere, drawPhotoOverlay, movePhoto, orderedPhotos, setRightTab, screenToLonLat, lonlatToScreen, filteredPhotos, reorderPhoto, rebuildPhotoList, featureAt, clampCenter, addManualMarkAt, manualAt, clearManualMarks, labelAvoidanceEnabled, densityIsMax, setMapFullscreen, graticuleStep, selectPhoto, drawGraticule, labelTier, paintGlobeBase, globeRadius, globeBasis, placeFeatureLabel, refreshFeatures, viewZoom, globeFeatureBounds, projectionSourceWindow, measure, setMapMeasure, measureDistanceKm, measureText, measureCirclePoint, measureLinePoint, measureScreen, formatCoord, projectOutlineSegments, drawFeatureOutline, textAlongPath, selectedTextHit, beginTextDrag, textDragMove, textTrackKey, selectFeature, textFollowFeature };';
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'map.js' });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  await wait(60);
  const T = sandbox.__t;
  check('the page boots without throwing', Boolean(T) && Boolean(T.state));
  check('the sealed Earth view has no menu entry',
    !/value="earth"/.test(html) && /地球视角已封存/.test(html));
  check('the console stayed quiet', T.state !== null);

  console.log('');
  console.log('the class switches sit in the expanded settings');
  {
    const topBar = html.slice(html.indexOf('id="map-label-bar"'),
      html.indexOf('id="annot-detail"'));
    check('全选/全不选 left the top line',
      !/classes-all|classes-none/.test(topBar));
    check('全选/全不选 sit at the right of the class row',
      /class="class-row"[\s\S]*?id="label-classes"[\s\S]*?class="class-actions"[\s\S]*?id="classes-all"[\s\S]*?id="classes-none"/.test(html));
  }
  const chips = dom.document.querySelectorAll('.cls-chip');
  check('one chip per feature class is built',
    chips.length === 2, String(chips.length));
  check('the base raster is a display switch, not a layer row',
    dom.document.querySelectorAll('.layer-item').length === 0
    && Boolean(dom.byId['base-on']),
    String(dom.document.querySelectorAll('.layer-item').length));
  check('the map starts at 50% label density',
    dom.byId['label-density'].value === '0.5'
    && dom.byId['density-value'].textContent === '50%',
    `${dom.byId['label-density'].value} / ${dom.byId['density-value'].textContent}`);
  check('the moon readout starts folded to its basic rows',
    dom.byId['moon-info'].dataset.collapsed === 'true'
    && dom.byId['moon-info-toggle'].getAttribute('aria-expanded') === 'false');
  dom.byId['moon-info-toggle'].fire('click');
  check('and it can be expanded',
    dom.byId['moon-info'].dataset.collapsed === 'false'
    && dom.byId['moon-info-toggle'].getAttribute('aria-expanded') === 'true');
  check('display has base brightness, shadow bias and earthshine controls',
    ['base-brightness', 'shadow-bias', 'earthshine-on']
      .every((id) => Boolean(dom.byId[id])));
  check('they start at 1, 0 and on',
    T.state.brightness === 1 && T.state.shadowBias === 0
    && T.state.earthshine === true);
  dom.byId['base-brightness'].value = '1.5';
  dom.byId['base-brightness'].fire('input');
  check('base brightness reaches the canvas state',
    T.state.brightness === 1.5, String(T.state.brightness));
  dom.byId['shadow-bias'].value = '0.12';
  dom.byId['shadow-bias'].fire('input');
  check('shadow bias reaches the canvas state',
    T.state.shadowBias === 0.12, String(T.state.shadowBias));
  check('base brightness can go brighter than the old 180% ceiling',
    Number(dom.byId['base-brightness'].getAttribute('max')) >= 3,
    dom.byId['base-brightness'].getAttribute('max'));
  dom.byId['base-brightness'].value = '2.5';
  dom.byId['base-brightness'].fire('input');
  check('and the slider actually reaches it',
    T.state.brightness === 2.5, String(T.state.brightness));
  dom.byId['base-brightness'].fire('dblclick');
  check('double-clicking base brightness restores 100%',
    dom.byId['base-brightness'].value === '1' && T.state.brightness === 1,
    `${dom.byId['base-brightness'].value} / ${T.state.brightness}`);
  dom.byId['shadow-bias'].fire('dblclick');
  check('double-clicking shadow bias restores zero',
    dom.byId['shadow-bias'].value === '0' && T.state.shadowBias === 0,
    `${dom.byId['shadow-bias'].value} / ${T.state.shadowBias}`);
  dom.byId['earthshine-on'].checked = false;
  dom.byId['earthshine-on'].fire('change');
  check('earthshine can be switched off', T.state.earthshine === false);
  dom.byId['earthshine-on'].checked = true;
  dom.byId['earthshine-on'].fire('change');
  dom.byId['base-brightness'].value = '1';
  dom.byId['base-brightness'].fire('input');
  dom.byId['shadow-bias'].value = '0';
  dom.byId['shadow-bias'].fire('input');
  check('the map version sits under the title',
    dom.byId['map-version'].textContent === '1.1.0');
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
    dom.byId['outline-toggle'].checked = false;
    dom.byId['outline-toggle'].fire('change');
    check('轮廓 belongs to the label settings', T.state.outlines === false);
    dom.byId['text-follow-toggle'].checked = true;
    dom.byId['text-follow-toggle'].fire('change');
    check('文字跟随轮廓 turns curved typesetting on', T.state.textFollow === true);
    dom.byId['round-outline-toggle'].checked = true;
    dom.byId['round-outline-toggle'].fire('change');
    check('月海使用圆形轮廓 restores the diameter circles', T.state.roundOutlines === true);
    dom.byId['linear-outline-toggle'].checked = false;
    dom.byId['linear-outline-toggle'].fire('change');
    check('显示线状地貌轮廓 hides only the open paths', T.state.linearOutlines === false);
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
    dom.byId['marks-toggle'].getAttribute('aria-pressed') === 'false'
    && dom.byId['marks-toggle'].tagName === 'H2');
  check('its title carries a border that follows the switch state',
    /#marks-toggle\s*\{[^}]*border:\s*1px solid/.test(mapStyle)
    && /#marks-toggle\[aria-pressed="false"\]\s*\{[^}]*border-color:\s*var\(--line\)/
      .test(mapStyle)
    && /#marks-toggle\[aria-pressed="false"\]\s*\{[^}]*color:\s*var\(--faint\)/
      .test(mapStyle));
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
    check('the seam-crossing rim is one continuous ring, not two halves',
      !!rim && rim.segments.length === 1 && rim.closed === true
      && jump < T.state.width / 2,
      `${rim ? rim.segments.length : 0} segments, jump ${jump} px`);
    T.state.projection = 'mercator';
    T.state.centerLat = 0;
    T.state.mercY = 0;
    const mercatorRim = T.featureRimSegments({
      lon: 180, lat: 0, diameter_km: 3000 });
    check('Mercator uses the same one-ring rule at the seam',
      !!mercatorRim && mercatorRim.segments.length === 1
      && mercatorRim.closed === true,
      `${mercatorRim ? mercatorRim.segments.length : 0} segments`);
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
  splitter.fire('pointerdown', { clientX: 300, pointerId: 2 });
  splitter.fire('pointermove', { clientX: 0, pointerId: 2 });
  splitter.fire('pointerup', { clientX: 0, pointerId: 2 });
  check('dragging the left splitter past the edge hides the left column',
    dom.byId['map-left'].dataset.collapsed === 'true'
    && dom.document.documentElement.style.getPropertyValue('--left-w') === '0px',
    dom.document.documentElement.style.getPropertyValue('--left-w'));
  check('the hidden edge grows a restore handle',
    splitter.dataset.collapsed === 'true'
    && splitter.getAttribute('role') === 'button'
    && /点击显示左栏/.test(splitter.title),
    `${splitter.dataset.collapsed} / ${splitter.getAttribute('role')} / ${splitter.title}`);
  splitter.fire('pointerdown', { clientX: 2, pointerId: 3 });
  splitter.fire('pointerup', { clientX: 2, pointerId: 3 });
  check('clicking the edge handle restores the collapsed column',
    dom.byId['map-left'].dataset.collapsed === 'false'
    && parseFloat(dom.document.documentElement.style.getPropertyValue('--left-w')) > 0,
    dom.document.documentElement.style.getPropertyValue('--left-w'));
  check('and the restore handle goes away',
    splitter.dataset.collapsed === 'false'
    && splitter.getAttribute('role') === 'separator');

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
  console.log('the globe asks for the catalogue at the magnification on screen');
  {
    const saved = {
      projection: T.state.projection, zoom: T.state.zoom, globe: T.state.globe,
      width: T.state.width, height: T.state.height, active: T.state.active,
    };
    T.state.projection = 'globe';
    T.state.width = 1200;
    T.state.height = 800;
    T.state.zoom = 1;                 // stale equirectangular level
    T.state.globe = { lon: 0, lat: 0, zoom: 8 };
    T.state.active = new Set(['crater']);
    check('the globe magnification translates to a detail level',
      Math.abs(T.viewZoom() - 6.0) < 1e-9 && T.viewZoom() > T.state.zoom,
      String(T.viewZoom()));
    await T.refreshFeatures();
    const call = fetchCalls.filter((url) => url.startsWith('/api/map/features')).pop();
    const params = new URLSearchParams(String(call).split('?')[1] || '');
    check('the feature request carries the globe detail level',
      params.get('zoom') === '6',
      String(params.get('zoom')));
    const west = Number(params.get('west'));
    const east = Number(params.get('east'));
    const span = west <= east ? east - west : 360 - west + east;
    check('and asks for the visible cap, not the whole catalogue',
      span > 0 && span < 120, `${west} .. ${east} (${span})`);
    const source = T.projectionSourceWindow();
    check('the globe base asks for a regional high-zoom source window',
      !!source && source.z >= 6 && source.east - source.west < 120,
      source ? `z${source.z} ${source.west}..${source.east}` : 'none');
    T.state.globe.zoom = 512;
    const detail = T.projectionSourceWindow();
    check('and reaches the high-resolution tile levels at extreme zoom',
      !!detail && detail.z >= 10 && detail.east - detail.west < 10,
      detail ? `z${detail.z} ${detail.west}..${detail.east}` : 'none');
    T.state.globe.zoom = 8;
    T.state.projection = 'equirect';
    T.state.density = { crater: 4 };
    await T.refreshFeatures();
    const maxCall = fetchCalls.filter((url) => url.startsWith('/api/map/features')).pop();
    const maxParams = new URLSearchParams(String(maxCall).split('?')[1] || '');
    check('MAX drops the diameter floor and raises the request ceiling',
      maxParams.get('zoom') === '8' && maxParams.get('limit') === '12000'
      && T.densityIsMax() === true,
      `${maxParams.get('zoom')} / ${maxParams.get('limit')}`);
    // A small crater at high globe magnification must be named even though the
    // equirectangular tile level is still z1: the old gate read state.zoom and
    // kept every sub-45 km caption off the sphere.
    T.state.projection = 'globe';
    T.state.density = {};
    const small = {
      feature: { class: 'crater', name_en: 'Tiny', name_zh: '小坑',
                 lon: 0, lat: 0, diameter_km: 2 },
      screen: { x: 600, y: 400 }, radius: 12, marker: 3, tier: 3,
    };
    check('a small crater is captioned once the globe is magnified',
      !!T.placeFeatureLabel(small, []));
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('a registered photo projects onto the map');
  const photoProject = {
    geometry: {
      observer: [1, 0, 0], right: [0, 1, 0], north: [0, 0, 1],
      sun: [1, 0, 0], distance_km: 384400,
    },
    matrix: [[40, 0, 50], [0, 40, 50]],
    image_size: [100, 100],
  };
  const centre = T.moonToPhoto(photoProject, 0, 0);
  check('the sub-observer point lands at the photo centre',
    centre && Math.abs(centre.x - 50) < 1e-9 && Math.abs(centre.y - 50) < 1e-9,
    JSON.stringify(centre));
  const east = T.moonToPhoto(photoProject, 0, 45);
  check('east maps with the matrix, without an extra north rotation',
    east && Math.abs(east.x - 78.375) < 1e-3
    && Math.abs(east.y - 50) < 1e-9,
    JSON.stringify(east));
  check('the far side is not projected',
    T.moonToPhoto(photoProject, 0, 180) === null);
  const back = T.photoToSphere(photoProject, 50, 50);
  check('the inverse mapping returns the sub-observer point',
    back && Math.abs(back.lon) < 1e-6 && Math.abs(back.lat) < 1e-6,
    JSON.stringify(back));
  T.state.projection = 'equirect';
  T.state.photoActive.add('0000000000000000');
  T.drawPhotoOverlay();
  check('a selected photo does not break an equirect redraw', true);
  T.state.photoActive.clear();

  console.log('');
  console.log('captions are laid out by weight tier');
  {
    const feature = (cls, diameter, name) => ({ class: cls, diameter_km: diameter, name_en: name });
    check('basins, Lunar 100 and libration targets are tier 0',
      T.labelTier(feature('basin', 200)) === 0
      && T.labelTier(feature('lunar100', 1)) === 0
      && T.labelTier(feature('libration', 1)) === 0);
    check('maria are tier 1 and the named terrain families tier 2',
      T.labelTier(feature('mare', 200)) === 1
      && T.labelTier(feature('lacus', 30)) === 2
      && T.labelTier(feature('crater', 120, 'Ptolemaeus')) === 2);
    check('ordinary craters are tier 3 and small satellites tier 4',
      T.labelTier(feature('crater', 60, 'Arago')) === 3
      && T.labelTier(feature('crater', 10, 'Arago A')) === 4);
    check('main craters outrank their satellites; other classes are tier 5',
      T.labelTier(feature('crater', 40, 'Arago B')) === 4
      && T.labelTier(feature('crater', 60, 'Arago')) === 3
      && T.labelTier(feature('dome', 20)) === 5);
  }

  console.log('');
  console.log('the map repeats east-west and stops at the poles');
  {
    const saved = { ...T.state };
    T.state.projection = 'equirect';
    T.state.centerLon = 179;
    T.state.centerLat = 89;
    T.state.width = 1200;
    T.state.height = 800;
    T.state.scale = 1;
    T.state.zoom = 0;
    T.state.mercY = 0;
    const west = T.screenToLonLat(600, 400);
    check('the centre of an equirect view is the stored coordinate',
      west && Math.abs(west.lon - 179) < 1e-6 && Math.abs(west.lat - 89) < 1e-6,
      JSON.stringify(west));
    T.state.centerLon = -179;
    const east = T.screenToLonLat(600, 400);
    check('longitude wraps across the date line',
      east && Math.abs(east.lon + 179) < 1e-6, JSON.stringify(east));
    T.state.projection = 'mercator';
    T.state.centerLon = 0;
    T.state.centerLat = 0;
    T.state.mercY = 0;
    const merc = T.screenToLonLat(600, 400);
    check('mercator keeps its centre under the pointer',
      merc && Math.abs(merc.lat) < 1e-6 && Math.abs(merc.lon) < 1e-6,
      JSON.stringify(merc));
    const beyond = T.lonlatToScreen(0, 84);
    check('a point near the pole projects in front of the camera',
      Number.isFinite(beyond.x) && Number.isFinite(beyond.y),
      JSON.stringify(beyond));
    T.state.projection = 'equirect';
    T.state.centerLon = 0;
    T.state.centerLat = 89;
    T.state.width = 1200;
    T.state.height = 200;
    T.state.scale = 1;
    T.state.zoom = 0;
    T.clampCenter();
    check('vertical panning stops at the top edge, it does not wrap',
      T.state.centerLat < 89 && T.state.centerLat > 0,
      String(T.state.centerLat));
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('the Earth view is a perspective from the Earth-Moon distance');
  {
    const saved = { ...T.state };
    T.state.projection = 'earth';
    T.state.width = 1200;
    T.state.height = 800;
    T.state.lighting = {
      moon: { distance_km: 384400 },
      sub_earth: { lon: 6, lat: 0 },
    };
    T.state.globe = { lon: 6, lat: 0, zoom: 1 };
    const centre = T.screenToLonLat(600, 400);
    check('the centre of the Earth view is the sub-Earth point',
      centre && Math.abs(centre.lon - 6) < 1e-6
      && Math.abs(centre.lat) < 1e-6,
      JSON.stringify(centre));
    const forward = T.lonlatToScreen(6, 0);
    check('a point at the sub-Earth point lands at the centre',
      Math.abs(forward.x - 600) < 1e-6 && Math.abs(forward.y - 400) < 1e-6,
      JSON.stringify(forward));
    // The visible cap is arccos(R/d) = 89.74 deg: a little more than a
    // hemisphere, which is the whole point of the finite-distance projection.
    const nearLimb = T.lonlatToScreen(95.5, 0);
    const beyondLimb = T.lonlatToScreen(96.5, 0);
    check('the Earth view sees just past the 90-degree hemisphere',
      nearLimb.visible !== false && beyondLimb.visible === false,
      JSON.stringify({ nearLimb, beyondLimb }));
    const outside = T.screenToLonLat(600 + T.globeRadius() + 10, 400);
    check('a pixel outside the limb has no ground point', outside === null);
    // Earth view keeps a fixed orientation but moves its window, so it must
    // use the whole-sphere source: a local window would go blank as the window
    // moved past its edge.
    T.state.globe.zoom = 8;
    check('Earth view keeps the whole-sphere texture at every magnification',
      T.projectionSourceWindow() === null,
      JSON.stringify(T.projectionSourceWindow()));
    // The orientation stays fixed to the sub-Earth point; a zoomed reader
    // moves the window, and the pointer inverse has to follow that window.
    T.state.globe.lon = 6;
    T.state.globe.lat = 0;
    T.state.earthPanX = 0.1;
    T.state.earthPanY = -0.05;
    const radius = T.globeRadius();
    const panned = T.lonlatToScreen(6, 0);
    check('an Earth-view window shift moves the picture, not the globe',
      Math.abs(panned.x - (600 + 0.1 * radius)) < 1e-6
      && Math.abs(panned.y - (400 + 0.05 * radius)) < 1e-6,
      JSON.stringify(panned));
    const back = T.screenToLonLat(panned.x, panned.y);
    check('and the pointer still lands on the same ground',
      back && Math.abs(back.lon - 6) < 1e-6 && Math.abs(back.lat) < 1e-6,
      JSON.stringify(back));
    T.state.earthPanX = 0;
    T.state.earthPanY = 0;
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('a Mercator zoom keeps the point under the pointer');
  {
    const saved = { ...T.state };
    const savedActive = T.state.active;
    // The base raster is irrelevant here and would start a 512-tile fetch;
    // render() still runs the rest of the frame's overlays.
    T.state.active = new Set();
    T.state.projection = 'mercator';
    T.state.width = 1200;
    T.state.height = 800;
    T.state.zoom = 3;
    T.state.scale = 1;
    T.state.centerLon = 12.5;
    T.state.centerLat = 23.4;
    T.state.mercY = 0;
    T.clampCenter();
    const anchor = { x: 900, y: 250 };
    const before = T.screenToLonLat(anchor.x, anchor.y);
    T.zoomBy(2, anchor);
    const after = T.screenToLonLat(anchor.x, anchor.y);
    check('zooming in leaves the anchored longitude alone',
      before && after && Math.abs(after.lon - before.lon) < 1e-6,
      JSON.stringify({ before, after, centre: T.state.centerLon }));
    check('zooming in leaves the anchored latitude alone (it used to move by '
      + 'the old offset)', before && after
      && Math.abs(after.lat - before.lat) < 1e-6,
      JSON.stringify({ before, after, centre: T.state.centerLat }));
    const zoomedIn = { lon: T.state.centerLon, lat: T.state.centerLat };
    T.zoomBy(0.5, anchor);
    const back = T.screenToLonLat(anchor.x, anchor.y);
    check('zooming back out returns to the same ground point',
      back && Math.abs(back.lon - before.lon) < 1e-6
      && Math.abs(back.lat - before.lat) < 1e-6,
      JSON.stringify({ before, back, centre: T.state.centerLat }));
    check('the camera centre moved to hold the pointer in place',
      Math.abs(zoomedIn.lat - before.lat) > 1e-3,
      JSON.stringify({ zoomedIn, before }));
    T.state.active = savedActive;
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('the globe basis is computed once per view');
  {
    const saved = { ...T.state };
    T.state.globe = { lon: 23.4, lat: -12.7, zoom: 1 };
    const first = T.globeBasis();
    check('the same view reuses the cached basis', T.globeBasis() === first);
    T.state.globe.lon = 24.4;
    const moved = T.globeBasis();
    check('a moved view gets a fresh basis', moved !== first
      && Math.abs(moved.up[0] - first.up[0]) > 1e-6);
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('the fast globe painter agrees with the reference projection');
  {
    const saved = { ...T.state };
    T.state.projection = 'globe';
    T.state.width = 200;
    T.state.height = 160;
    T.state.globe = { lon: 23.4, lat: -12.7, zoom: 1 };
    const width = 200;
    const height = 160;
    const sourceWidth = 512;
    const sourceHeight = 256;
    const source = new Uint8ClampedArray(sourceWidth * sourceHeight * 4);
    for (let sy = 0; sy < sourceHeight; sy += 1) {
      for (let sx = 0; sx < sourceWidth; sx += 1) {
        const offset = (sy * sourceWidth + sx) * 4;
        source[offset] = Math.round(sx / (sourceWidth - 1) * 255);
        source[offset + 1] = Math.round(sy / (sourceHeight - 1) * 255);
        source[offset + 2] = 128;
        source[offset + 3] = 255;
      }
    }
    const out = new Uint8ClampedArray(width * height * 4);
    T.paintGlobeBase(out, width, height, source, sourceWidth, sourceHeight, 1, null, 1);
    const radius = T.globeRadius();
    let checked = 0;
    const wrong = [];
    for (let y = 3; y < height; y += 7) {
      for (let x = 3; x < width; x += 7) {
        const nx = (x + 0.5 - width / 2) / radius;
        const ny = (height / 2 - (y + 0.5)) / radius;
        if (nx * nx + ny * ny > 1) continue;
        const geo = T.screenToLonLat(x + 0.5, y + 0.5);
        if (!geo) continue;
        const sx = Math.min(sourceWidth - 1,
          Math.floor(((geo.lon + 180) / 360) * sourceWidth));
        const sy = Math.min(sourceHeight - 1,
          Math.floor(((90 - geo.lat) / 180) * sourceHeight));
        const expectedR = Math.round(sx / (sourceWidth - 1) * 255);
        const expectedG = Math.round(sy / (sourceHeight - 1) * 255);
        const offset = (y * width + x) * 4;
        checked += 1;
        if (Math.abs(out[offset] - expectedR) > 2
            || Math.abs(out[offset + 1] - expectedG) > 2
            || out[offset + 3] !== 255) {
          wrong.push({ x, y,
            got: [out[offset], out[offset + 1], out[offset + 3]],
            expected: [expectedR, expectedG, 255] });
        }
      }
    }
    check('inside the disc every sampled pixel matches screenToLonLat',
      checked > 100 && wrong.length === 0,
      JSON.stringify({ checked, wrong: wrong.slice(0, 3) }));
    check('outside the disc is left transparent for the canvas background',
      out[3] === 0 && out[(width * height - 1) * 4 + 3] === 0);

    const halfWidth = 100;
    const halfHeight = 80;
    const half = new Uint8ClampedArray(halfWidth * halfHeight * 4);
    T.paintGlobeBase(half, halfWidth, halfHeight, source,
      sourceWidth, sourceHeight, 1, null, 0.5);
    let halfChecked = 0;
    const halfWrong = [];
    for (let y = 3; y < halfHeight; y += 5) {
      for (let x = 3; x < halfWidth; x += 5) {
        const cssX = (x + 0.5) / 0.5;
        const cssY = (y + 0.5) / 0.5;
        const geo = T.screenToLonLat(cssX, cssY);
        if (!geo) continue;
        const sx = Math.min(sourceWidth - 1,
          Math.floor(((geo.lon + 180) / 360) * sourceWidth));
        const sy = Math.min(sourceHeight - 1,
          Math.floor(((90 - geo.lat) / 180) * sourceHeight));
        const offset = (y * halfWidth + x) * 4;
        halfChecked += 1;
        if (Math.abs(half[offset] - Math.round(sx / (sourceWidth - 1) * 255)) > 2
            || Math.abs(half[offset + 1]
              - Math.round(sy / (sourceHeight - 1) * 255)) > 2
            || half[offset + 3] !== 255) {
          halfWrong.push({ x, y, got: [half[offset], half[offset + 1]] });
        }
      }
    }
    check('a half-resolution moving buffer lands on the same ground',
      halfChecked > 100 && halfWrong.length === 0,
      JSON.stringify({ halfChecked, wrong: halfWrong.slice(0, 3) }));
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('a caption sits just to the right of its marker');
  {
    const saved = { ...T.state };
    T.state.language = 'en';
    T.state.width = 1200;
    T.state.height = 800;
    T.state.zoom = 4;
    T.state.diameters = true;
    T.state.outlineStyle = 'solid';
    T.state.labels = true;
    T.state.avoidOverlap = true;
    T.state.density = {};
    T.state.selected = null;
    T.state.labelFont = 0;
    const feature = {
      class: 'crater', name_en: 'Big', name_zh: 'Big', lon: 0, lat: 0,
      diameter_km: 200,
    };
    const item = {
      feature, screen: { x: 600, y: 400 }, radius: 48, marker: 3, tier: 2,
    };
    const right = T.placeFeatureLabel(item, []);
    check('a point feature name is set to the right of the marker',
      right && right.left > 600 && right.left - 600 <= 16,
      JSON.stringify(right));
    const wide = {
      ...item,
      radius: 8,
      feature: { ...feature, name_en: 'A Very Long Crater Name' },
    };
    const wideRight = T.placeFeatureLabel(wide, []);
    check('a long point name still starts on the right',
      wideRight && wideRight.left > 600,
      JSON.stringify(wideRight));
    const region = {
      ...item,
      feature: { ...feature, class: 'mare', name_en: 'Big Sea' },
    };
    const centred = T.placeFeatureLabel(region, []);
    check('a region name keeps its across-the-formation slot',
      centred && Math.abs((centred.left + centred.right) / 2 - 600) < 2
      && Math.abs((centred.top + centred.bottom) / 2 - 400) < 2,
      JSON.stringify(centred));
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('mercantor has no mirrored poles and photos select without showing');
  {
    const saved = { ...T.state };
    T.state.projection = 'mercator';
    T.state.centerLon = 0;
    T.state.centerLat = 0;
    T.state.mercY = 0;
    T.state.width = 1200;
    T.state.height = 800;
    T.state.scale = 1;
    T.state.zoom = 0;
    check('the empty band above the Mercator edge is background, not a smear',
      T.screenToLonLat(600, 0) === null,
      JSON.stringify({ projection: T.state.projection,
        lat: T.state.centerLat, mercY: T.state.mercY,
        scale: T.state.scale, height: T.state.height,
        result: T.screenToLonLat(600, 0) }));
    Object.assign(T.state, saved);
    const photo = { id: 'c000000000000000', name: 'p', project: {} };
    T.state.photos = [photo];
    T.state.photoOrder = [photo.id];
    T.state.photoActive.clear();
    T.state.photo = null;
    T.state.photoFilters = { dateStart: '', dateEnd: '', ageMin: null,
                             ageMax: null, features: [], query: '' };
    T.selectPhoto(photo);
    check('selecting a photo does not put it on the map',
      T.state.photo === photo && T.state.photoActive.size === 0);
    check('the selected row gets its own highlight',
      dom.byId['photo-list'].children.some((node) => node.classes.has('selected')));
    T.state.photos = [];
    T.state.photoOrder = [];
    T.state.photo = null;
    T.state.photoActive.clear();
  }

  console.log('');
  console.log('a landform caption is clickable like its marker');
  {
    const savedLayers = T.state.layers;
    const feature = { id: 1, name_en: 'Clavius', name_zh: '克拉维斯',
                      class: 'crater', lon: 0, lat: 0, diameter_km: 231 };
    T.state.layers = { feature_classes: ['crater'] };
    T.state.active.add('crater');
    T.state.features = [];
    T.state.labelHits = [{
      feature, left: 10, top: 10, right: 90, bottom: 26,
    }];
    const hit = T.featureAt(30, 18);
    check('clicking the caption selects the feature it names',
      hit === feature, JSON.stringify(hit));
    T.state.labelHits = [];
    T.state.active.delete('crater');
    T.state.layers = savedLayers;
  }

  console.log('');
  console.log('the map layout controls');
  {
    check('the search box now searches landforms',
      dom.byId['search-input'].attrs.placeholder === '搜索地貌',
      dom.byId['search-input'].attrs.placeholder);
    check('every geometry switch starts on',
      dom.byId['mark-equator'].checked === true
      && T.state.marks.equator === true
      && ['mark-terminator', 'mark-poles', 'mark-subsolar',
          'mark-libration', 'mark-graticule'].every((id) => dom.byId[id].checked));
    const savedProjection = T.state.projection;
    const savedGlobe = T.state.globe.zoom;
    T.state.projection = 'globe';
    T.state.globe.zoom = 1;
    const coarse = T.graticuleStep();
    T.state.globe.zoom = 4;
    const fine = T.graticuleStep();
    check('the globe graticule adapts to the globe zoom, not the tile zoom',
      coarse === 60 && fine === 15, `${coarse} / ${fine}`);
    T.state.projection = savedProjection;
    T.state.globe.zoom = savedGlobe;
    T.state.marksOn = true;
    T.state.marks.graticule = true;
    T.state.width = 1200;
    T.state.height = 800;
    const mapCtx = dom.byId['map-canvas'].getContext('2d');
    mapCtx.drawn.texts.length = 0;
    T.state.projection = 'equirect';
    T.drawGraticule();
    check('flat-map latitude ticks stay on the left frame edge',
      mapCtx.drawn.texts.some((item) => item.x === 5),
      JSON.stringify(mapCtx.drawn.texts.slice(0, 3).map((i) => i.text)));
    check('flat-map longitude ticks stay on the bottom frame edge',
      mapCtx.drawn.texts.some((item) => item.y === 786),
      JSON.stringify(mapCtx.drawn.texts.slice(-3).map((i) => i.text)));
    T.state.projection = savedProjection;
    const start = dom.byId['photo-filter-date-start'].value;
    const end = dom.byId['photo-filter-date-end'].value;
    const now = new Date();
    const expectedStart = new Date(Date.UTC(now.getUTCFullYear() - 50,
      now.getUTCMonth(), now.getUTCDate())).toISOString().slice(0, 10);
    check('the photo date filter opens on the past fifty years',
      start === expectedStart && end === now.toISOString().slice(0, 10),
      `${start} .. ${end}`);
  }

  console.log('');
  console.log('the whole page can become just the map');
  {
    T.setMapFullscreen(true);
    check('全屏 hides the chrome and shows the exit control',
      T.state.fullscreen === true
      && dom.document.body.classes.has('map-fullscreen')
      && dom.byId['map-exit-fullscreen'].hidden === false);
    T.setMapFullscreen(false);
    check('退出全屏 puts the page back', T.state.fullscreen === false
      && !dom.document.body.classes.has('map-fullscreen')
      && dom.byId['map-exit-fullscreen'].hidden === true);
  }

  console.log('');
  console.log('density has a 100% detent and turns avoidance off above it');
  {
    T.state.avoidOverlap = true;
    const density = dom.byId['label-density'];
    density.value = '1.2';
    density.fire('input');
    check('the first drag stops at 100%',
      density.value === '1' && T.state.density.mare === 1);
    density.fire('change');
    check('avoidance is still on at 100%', T.labelAvoidanceEnabled() === true);
    density.value = '1.2';
    density.fire('input');
    check('a second drag passes 100%',
      density.value === '1.2' && T.state.density.mare === 1.2,
      `${density.value} / ${T.state.density.mare}`);
    check('avoidance is still on at 120%',
      T.labelAvoidanceEnabled() === true);
    density.value = '3.6';
    density.fire('input');
    check('and avoidance turns itself off above 300%',
      T.labelAvoidanceEnabled() === false);
    density.fire('change');
    density.value = '1';
    density.fire('input');
    density.fire('change');
    // 300% is the top of the slider and the value the head shows.
    density.value = '3';
    density.fire('input');
    check('density reaches 300%',
      density.value === '3' && T.state.density.mare === 3
      && dom.byId['density-value'].textContent === '300%',
      `${density.value} / ${T.state.density.mare} / `
      + dom.byId['density-value'].textContent);
    density.fire('change');
    // Pulling past 300% pauses on the 300% line, then continues to MAX.
    density.value = '3.2';
    density.fire('input');
    check('300% is a detent, not the top',
      density.value === '3' && T.state.density.mare === 3
      && dom.byId['density-value'].textContent === '300%',
      `${density.value} / ${T.state.density.mare}`);
    const featureCallsBeforeMax = fetchCalls.filter(
      (url) => String(url).startsWith('/api/map/features')).length;
    density.value = '3.6';
    density.fire('input');
    await wait(250);
    check('MAX re-asks the catalogue, not just the painter',
      fetchCalls.filter((url) => String(url).startsWith('/api/map/features')).length
      > featureCallsBeforeMax);
    check('past the detent the slider reads MAX and asks for everything',
      density.value === '4' && T.state.density.mare === 4
      && dom.byId['density-value'].textContent === 'MAX'
      && T.densityIsMax() === true,
      `${density.value} / ${T.state.density.mare} / `
      + dom.byId['density-value'].textContent);
    density.fire('change');
    density.fire('dblclick');
    check('double-clicking the density returns it to 100%',
      density.value === '1' && T.state.density.mare === 1
      && dom.byId['density-value'].textContent === '100%'
      && T.densityIsMax() === false,
      `${density.value} / ${T.state.density.mare} / `
      + dom.byId['density-value'].textContent);
    density.value = '1';
    density.fire('input');
    density.fire('change');
  }

  console.log('');
  console.log('an edge feature keeps its name');
  {
    const savedZoom = T.state.zoom;
    const savedDensity = JSON.stringify(T.state.density);
    T.state.zoom = 4;
    T.state.density = {};
    const feature = {
      class: 'crater', name_zh: '测试环形坑', name_en: 'Test', lon: 12, lat: 34,
      diameter_km: 120,
    };
    const item = {
      feature, radius: 6, marker: 3, tier: 2,
      screen: { x: T.state.width - 3, y: 4 },
    };
    const box = T.placeFeatureLabel(item, [], false);
    check('a name with no room beside its marker is clamped into the frame',
      !!box && box.left >= 0 && box.right <= T.state.width
      && box.top >= 0 && box.bottom <= T.state.height,
      JSON.stringify(box));
    T.state.zoom = savedZoom;
    T.state.density = JSON.parse(savedDensity);
  }

  console.log('');
  console.log('manual anchors live on the map');
  {
    T.state.labels = true;
    const saved = T.state.manualMarks.length;
    const mark = T.addManualMarkAt({ lon: 12.3, lat: -4.5 });
    check('Ctrl-click drops a numbered anchor', Boolean(mark)
      && T.state.manualMarks.length === saved + 1
      && mark.text === '标记点1', mark && mark.text);
    const item = T.state.manualLayout[0];
    check('the anchor can be hit-tested',
      Boolean(item) && T.manualAt(item.x, item.y) === item,
      JSON.stringify(T.state.manualLayout));
    T.clearManualMarks();
    check('清空标记 removes them', T.state.manualMarks.length === 0);
  }

  console.log('');
  console.log('the photo library filters by date, age and covered features');
  {
    const first = {
      id: 'a000000000000000', name: 'a', date: '2023-03-04', age_days: 12.2,
      covered_features: [{ name_en: 'Clavius', name_zh: '克拉维斯' }],
    };
    const second = {
      id: 'b000000000000000', name: 'b', date: '2023-11-02', age_days: 18.3,
      covered_features: [{ name_en: 'Tycho', name_zh: '第谷' }],
    };
    T.state.photos = [first, second];
    T.state.photoOrder = [first.id, second.id];
    T.state.photoFilters = { dateStart: '2023-06-01', dateEnd: '',
                             ageMin: null, ageMax: null, features: [], query: '' };
    check('a date window keeps the matching photo',
      T.filteredPhotos().map((entry) => entry.id).join(',') === second.id,
      T.filteredPhotos().map((entry) => entry.id).join(','));
    T.state.photoFilters.dateStart = '';
    T.state.photoFilters.features = ['Clavius'];
    check('a covered feature keeps the photo that reaches it',
      T.filteredPhotos().map((entry) => entry.id).join(',') === first.id);
    T.state.photoFilters.features = [];
    T.state.photoFilters.query = '第谷';
    check('typing a Chinese feature name searches the coverage',
      T.filteredPhotos().map((entry) => entry.id).join(',') === second.id);
    T.state.photoFilters.query = '';
    T.state.photoFilters.ageMin = 15;
    check('an age window keeps the older moon',
      T.filteredPhotos().map((entry) => entry.id).join(',') === second.id);
    T.reorderPhoto(first.id, second.id, true);
    check('drag order puts the dragged photo after its target',
      T.orderedPhotos().map((entry) => entry.id).join(',') ===
      `${second.id},${first.id}`,
      T.orderedPhotos().map((entry) => entry.id).join(','));
    T.state.photos = [];
    T.state.photoOrder = [];
    T.state.photoFilters = { dateStart: '', dateEnd: '', ageMin: null,
                             ageMax: null, features: [], query: '' };
  }

  console.log('');
  console.log('the photo library is a right-column tab with layer order');
  {
    const first = { id: 'a000000000000000', name: 'a' };
    const second = { id: 'b000000000000000', name: 'b' };
    T.state.photos = [first, second];
    T.state.photoOrder = [first.id, second.id];
    T.setRightTab('photos');
    check('the photo library tab shows its own pane',
      dom.byId['right-photos'].hidden === false
      && dom.byId['right-selection'].hidden === true
      && dom.byId['tab-photos'].getAttribute('aria-pressed') === 'true');
    T.movePhoto(first, 1);
    check('moving a photo down changes the layer order',
      T.orderedPhotos().map((entry) => entry.id).join(',')
      === `${second.id},${first.id}`,
      T.orderedPhotos().map((entry) => entry.id).join(','));
    T.setRightTab('selection');
    check('the selection tab comes back',
      dom.byId['right-selection'].hidden === false
      && dom.byId['right-photos'].hidden === true);
    T.setRightTab('settings');
    await wait(20);
    check('其他设置 is a third right-column tab',
      dom.byId['right-settings'].hidden === false
      && dom.byId['right-selection'].hidden === true
      && dom.byId['right-photos'].hidden === true
      && dom.byId['tab-settings'].getAttribute('aria-pressed') === 'true');
    check('the settings tab reports the current DEM and download state',
      dom.byId['data-dem'].textContent === 'ldem_64.tif'
      && dom.byId['data-state'].textContent === '本地 DEM'
      && dom.byId['data-download'].disabled === false);
    T.setRightTab('selection');
    T.state.photos = [];
    T.state.photoOrder = [];
  }

  console.log('');
  console.log('测量工具：线、圆与数值');
  {
    const button = dom.byId['map-measure'];
    check('the top bar carries a measuring button', Boolean(button));
    const topBar = html.slice(html.indexOf('<header'), html.indexOf('</header>'));
    check('and it lives in the header', /id="map-measure"/.test(topBar));
    const densityAt = html.indexOf('id="label-density"');
    const measureAt = html.indexOf('id="map-measure"');
    const fullAt = html.indexOf('id="map-fullscreen"');
    check('the measure button sits to the right of the density slider',
      densityAt > 0 && measureAt > densityAt && measureAt < fullAt,
      `${densityAt} -> ${measureAt} < ${fullAt}`);
    check('the density slider allows 300% and the MAX notch',
      /id="label-density"[^>]*max="4"/.test(html));
    check('the coordinate pair reads latitude first',
      /^34\.50°N\s+1\.20°W$/.test(T.formatCoord(-1.2, 34.5)),
      T.formatCoord(-1.2, 34.5));
    check('and arming it turns the button gold',
      /#map-measure\[aria-pressed="true"\]/.test(mapStyle));
    button.fire('click');
    check('arming it latches the button',
      button.getAttribute('aria-pressed') === 'true' && T.measure.on === true);

    const stage = dom.byId['map-stage'];
    const gesture = (type, x, y, extra) => stage.fire(type, Object.assign({
      button: 0, pointerId: 3, clientX: x, clientY: y, target: stage,
    }, extra || {}));
    gesture('pointerdown', 400, 400);
    gesture('pointermove', 800, 400);
    gesture('pointerup', 800, 400);
    check('a drag draws a line on the ground',
      T.measure.shape && T.measure.shape.kind === 'line',
      JSON.stringify(T.measure.shape));
    const km = T.measureDistanceKm(T.measure.shape.a, T.measure.shape.b);
    check('the line has a great-circle length',
      km > 100 && km < 6000, String(km));
    check('the value is written in kilometres',
      /^\d+(\.\d+)? (km|m)$/.test(T.measureText(T.measure.shape)),
      T.measureText(T.measure.shape));
    await wait(40);
    {
      const call = fetchBodies.filter((entry) => entry.url === '/api/map/profile').pop();
      check('the line asks the server for its profile',
        !!call && call.body.kind === 'line' && !!call.body.a && !!call.body.b,
        JSON.stringify(call && call.body));
      check('and the chart appears in the map stage',
        dom.byId['map-measure-chart'].hidden === false,
        String(dom.byId['map-measure-chart'].hidden));
      check('with units on both axes',
        drawn.texts.some((entry) => entry.text === '高程 km')
        && drawn.texts.some((entry) => entry.text === '距离 km'),
        JSON.stringify(drawn.texts.slice(-4).map((entry) => entry.text)));
      check('on a translucent black panel',
        /\.map-measure-chart \{[^}]*background: rgba\(0, 0, 0, \.\d+\)/.test(mapStyle));
    }
    {
      const last = drawn.texts[drawn.texts.length - 1];
      check('and painted on the map beside the line',
        last && / km$| m$/.test(last.text), JSON.stringify(last));
    }

    const end = T.measure.shape.b;
    const endAt = T.lonlatToScreen(end.lon, end.lat);
    gesture('pointerdown', endAt.x, endAt.y);
    gesture('pointermove', endAt.x + 60, endAt.y + 20);
    gesture('pointerup', endAt.x + 60, endAt.y + 20);
    check('an endpoint can be moved after the line is drawn',
      T.measure.shape.b.lon !== end.lon,
      JSON.stringify(T.measure.shape));

    // The whole line is a handle too: press anywhere on the stroke and drag.
    {
      const mid = T.measureLinePoint(T.measure.shape.a, T.measure.shape.b, 0.5);
      const at = T.lonlatToScreen(mid.lon, mid.lat);
      const before = { a: { ...T.measure.shape.a }, b: { ...T.measure.shape.b } };
      gesture('pointerdown', at.x, at.y);
      gesture('pointermove', at.x + 50, at.y + 20);
      gesture('pointerup', at.x + 50, at.y + 20);
      check('a drawn line can be dragged from its middle',
        T.measure.shape.a.lon !== before.a.lon
        && T.measure.shape.b.lon !== before.b.lon,
        JSON.stringify(T.measure.shape));
    }
    // A short measurement used to be deleted on release; only a click with no
    // drag may vanish now.
    {
      gesture('pointerdown', 400, 400);
      gesture('pointermove', 400.05, 400);
      gesture('pointerup', 400.05, 400);
      check('a very short line survives the release',
        T.measure.shape && T.measure.shape.kind === 'line'
        && T.measureDistanceKm(T.measure.shape.a, T.measure.shape.b) > 0,
        JSON.stringify(T.measure.shape));
    }

    gesture('pointerdown', 500, 300, { shiftKey: true });
    gesture('pointermove', 560, 340, { shiftKey: true });
    gesture('pointerup', 560, 340, { shiftKey: true });
    check('Shift-drag draws a circle', T.measure.shape.kind === 'circle'
      && T.measure.shape.radiusKm > 0, JSON.stringify(T.measure.shape));
    const rim = [0, 1, 2, 3].map((index) => T.measureCirclePoint(
      T.measure.shape.lon, T.measure.shape.lat, T.measure.shape.radiusKm,
      index * Math.PI / 2));
    const centre = T.measure.shape;
    const spread = Math.max(...rim.map((point) => T.measureDistanceKm(centre, point)))
      - Math.min(...rim.map((point) => T.measureDistanceKm(centre, point)));
    check('and every rim point is the same ground radius away',
      spread < 0.05, String(spread));
    check('the circle reads as a radius',
      /^\d+(\.\d+)? (km|m)$/.test(T.measureText(T.measure.shape)),
      T.measureText(T.measure.shape));
    await wait(40);
    {
      const call = fetchBodies.filter((entry) => entry.url === '/api/map/profile').pop();
      check('the circle asks for its radial mean profile',
        !!call && call.body.kind === 'circle' && call.body.radius_km > 0,
        JSON.stringify(call && call.body));
    }

    // The middle button pans even while 测量 owns the left one.
    {
      const before = { lon: T.state.centerLon, lat: T.state.centerLat };
      const shapeBefore = JSON.stringify(T.measure.shape);
      gesture('pointerdown', 400, 400, { button: 1 });
      gesture('pointermove', 470, 430, { button: 1 });
      gesture('pointerup', 470, 430, { button: 1 });
      check('the middle button pans the map while measuring',
        (T.state.centerLon !== before.lon || T.state.centerLat !== before.lat)
        && JSON.stringify(T.measure.shape) === shapeBefore,
        JSON.stringify({ before, after: { lon: T.state.centerLon, lat: T.state.centerLat } }));
    }

    button.fire('click');
    check('pressing it again clears the tool and its chart',
      button.getAttribute('aria-pressed') === 'false'
      && T.measure.on === false && T.measure.shape === null
      && dom.byId['map-measure-chart'].hidden === true);
  }

  console.log('');
  console.log('outlines are projected from the catalogue payload');
  {
    // A rille arrives as lon/lat path points; the painter needs screen space.
    const segments = T.projectOutlineSegments({
      kind: 'line', path: [[0, 0], [4, 0], [4, 4]],
    });
    check('an outline path projects to one screen segment',
      Array.isArray(segments) && segments.length === 1 && segments[0].length === 3,
      JSON.stringify(segments && segments.length));
    const closed = T.projectOutlineSegments({
      kind: 'point', ring: [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
    });
    check('a closed ring keeps its closing point',
      closed && closed[0].length === 5, JSON.stringify(closed && closed[0].length));
    check('an empty outline has nothing to paint',
      T.projectOutlineSegments({ kind: 'line', path: [] }) === null);
    const mountain = {
      feature: {
        class: 'mons', lon: 2, lat: 2, diameter_km: 100,
        outline: { kind: 'centerline', path: [[0, 0], [4, 0], [4, 4]] },
      },
      radius: 10, kmPerPixel: 1,
    };
    const saved = { ...T.state };
    T.state.projection = 'equirect';
    T.state.width = 1200;
    T.state.height = 800;
    T.state.scale = 1;
    T.state.zoom = 2;
    T.state.centerLon = 0;
    T.state.centerLat = 0;
    T.state.linearOutlines = true;
    T.state.mountainOutlines = false;
    check('山脉轮廓 can be switched off on its own',
      T.drawFeatureOutline(mountain) === false);
    T.state.mountainOutlines = true;
    check('and comes back with the switch', T.drawFeatureOutline(mountain) === true);
    T.state.linearOutlines = false;
    check('显示线状地貌轮廓 still owns every line',
      T.drawFeatureOutline(mountain) === false);
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('a selected linear label can be dragged along its own outline');
  {
    const saved = {
      selected: T.state.selected,
      labelHits: T.state.labelHits,
      textTracks: T.state.textTracks,
      labelShift: T.state.labelShift,
      textDrag: T.state.textDrag,
      textFollow: T.state.textFollow,
    };
    const feature = {
      id: 99001, name_en: 'Rima Test', class: 'rima',
      outline: { kind: 'centerline', path: [[0, 0], [1, 0]] },
    };
    const key = T.textTrackKey(feature);
    T.state.selected = feature;
    T.state.textFollow = true;
    T.state.labelHits = [{ feature, left: 0, top: 0, right: 100, bottom: 24 }];
    T.state.textTracks = {
      [key]: { start: 10, base: 10, range: 100, loop: false, tangent: 0,
               length: 160 },
    };
    T.state.labelShift = {};
    T.state.textDrag = null;
    const hit = T.selectedTextHit(20, 10);
    check('the selected label is the one the pointer grabs', hit === feature);
    const started = T.beginTextDrag({
      clientX: 20, clientY: 10, pointerId: 41,
    }, feature);
    T.textDragMove({ clientX: 40, clientY: 10 });
    check('dragging moves the caption along the trace',
      started && Math.abs(T.state.labelShift[key] - 0.2) < 1e-6
      && Math.abs(T.state.textDrag.start - 30) < 1e-6,
      JSON.stringify({ shift: T.state.labelShift[key],
        start: T.state.textDrag && T.state.textDrag.start }));
    T.state.textDrag = null;
    T.selectFeature(null);
    check('the chosen placement survives deselecting the feature',
      Math.abs(T.state.labelShift[key] - 0.2) < 1e-6,
      JSON.stringify(T.state.labelShift));
    Object.assign(T.state, saved);
  }

  console.log('');
  console.log('text-follow applies to every linear family');
  {
    const saved = T.state.textFollow;
    T.state.textFollow = true;
    const feature = (kind, className) => ({
      id: 1, class: className,
      outline: { kind, path: [[0, 0], [1, 0]] },
    });
    check('mountains, ridges, valleys and scarps get the same text path',
      ['mons', 'dorsum', 'vallis', 'rupes', 'rima']
        .every((className) => T.textFollowFeature(feature('centerline', className))),
      JSON.stringify(['mons', 'dorsum', 'vallis', 'rupes', 'rima']
        .map((className) => T.textFollowFeature(feature('centerline', className)))));
    check('a mare ring is not treated as a linear trace',
      T.textFollowFeature(feature('ring', 'mare')) === false);
    T.state.textFollow = saved;
  }

  console.log('');
  console.log('no unexpected exceptions');
  finish();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
