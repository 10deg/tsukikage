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
  'setExperimentSort',
  'saveDisk', 'selectFeature', 'refreshLabels', 'activeClasses', 'diskPixels',
  'processingPayload', 'drawPhaseStrip', 'phaseShape', 'orientation',
  'mirroredPlan', 'buildClassToggles', 'loadFeatureIndex', 'matchFeatures',
  'updateNoiseInfo', 'LIMITS', 'SETTING_FIELDS', 'afterSelectionChange',
  'lonLatToPixel', 'cameraFieldRadiusArcsec', 'LAND_CLASSES',
  'collectSettings', 'drawMagnifier', 'magnifierView', 'splitterStart',
  'splitterCeiling', 'SPLITTERS', 'setLeftHidden', 'leftIsHidden',
  'syncKmPxPresets', 'terrainLimitLabel', 'clearActivePatch', 'setActivePatch',
  'syncTerrainSourceFromRegion', 'selectionInsidePatch',
  'DEFAULT_OFF_CLASSES', 'animationRequest',
  'animationRenderSize', 'animationOutputPixels', 'animationSkyKm',
  'setLabelControls', 'syncResolutionPicker', 'previewPixels',
  'applySettings', 'centrePlacement', 'setAllClasses',
  'eyepieceSettings', 'applyEyepiece', 'renderEvents', 'setProcTab',
  'buildEventKinds', 'drawViewLabels', 'labelEdits', 'labelHidden', 'labelClass',
  'resetLabelClass', 'resetAllLabelEdits', 'labelKey', 'ruler',
  'labelStyleForFrame', 'labelLockOn', 'scalePlan', 'scalePlanAxes',
  'fitPlanToFrame', 'lockedDiskPlan', 'relatedNameFor',
  'labelFrameFontPx', 'labelStyleFor', 'labelScreenFontPx',
  'renderPixels', 'compassDirection', 'animationAnnotationsPayload',
  'selectionCorrected', 'effectiveSelectionArcsec', 'effectiveSelectionAspect',
  'correctedBoundaryPixels',
  'litCentreFromPixels', 'skyAutoCloseup', 'skyTargetView', 'skyGrowAspect',
  'imageToScreen',
  'skyFitAspectFromAz', 'skyEdgeAt', 'skyPlaceLabels', 'skySeries',
  'skyLabelItems', 'saveSkyPng', 'fillMoonInfo', 'eclipseReadout',
  'skyPaintEclipseMoon', 'openEventWindow', 'closeEventWindow',
  'eventCsvText', 'eventsFromCsv', 'eventRowFromForecast',
  'eventColumnsFor', 'eventCellText', 'eventMetric', 'renderEventHeader',
  'curveSeries', 'curveSeriesSpecs', 'curveTargetView', 'curveOptions',
  'curveClampEarthView', 'curveStyleFor', 'curveApplyLimits',
  'curveAddTextBox', 'curveResetView', 'curveResetFormat',
  'openCurve', 'closeCurve',
  'saveCurvePng', 'drawCurve',
  'renderEventWindowRows', 'paintCoordReadout', 'addManualMark',
  'manualHitTest', 'clearManualMarks', 'overlay'];

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
  // `[data-x="y"]`: the page groups its nudge rows with `data-range="anim-start"`
  // and `data-range="sky"`, so the harness has to answer this selector or the
  // wiring those rows depend on is silently never exercised.
  {
    const attribute = /^\[data-([\w-]+)="([^"]*)"\]$/.exec(selector);
    if (attribute) {
      const key = attribute[1].replace(/-([a-z])/g, (_m, chr) => chr.toUpperCase());
      return Boolean(element.dataset) && element.dataset[key] === attribute[2];
    }
  }
  // A bare class selector: the dividers look their panel up this way.
  if (/^\.[\w-]+$/.test(selector)) {
    return String(element.className || '').split(/\s+/).includes(selector.slice(1));
  }
  // The two tab strips are looked up as `#strip .tab`.
  if (selector === '#train-tabs .tab') return String(element.id).startsWith('train-tab-');
  if (selector === '#proc-tabs .tab') return String(element.id).startsWith('proc-tab-');
  if (selector === '#proc-image-tabs .tab') {
    return String(element.id).startsWith('proc-image-tab-');
  }
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
  if (selector === '[data-curve-nudge]') {
    return element.dataset && (element.dataset.curveNudge !== undefined ||
      element.dataset['curve-nudge'] !== undefined);
  }
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
    // A real element's innerHTML assignment replaces what is inside it; the
    // stub has to do the same, or `menu.innerHTML = ''` leaves the previous
    // options in `menu.options` and every re-copy of a ladder stacks another
    // copy on top of the last one.
    get innerHTML() { return this._innerHTML === undefined ? '' : this._innerHTML; },
    set innerHTML(value) {
      this._innerHTML = String(value);
      if (String(value) === '') {
        this.children.length = 0;
        if (this.tagName === 'SELECT') {
          this.options.length = 0;
          this.value = '';
        }
      }
    },
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
      // A real <select>.options includes the options inside its <optgroups>:
      // the page groups 数据源 into 本地/在线, and a stub that only counted
      // direct children would call that menu empty.
      if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
        this.options.push(child);
      }
      if (this.tagName === 'SELECT' && child.tagName === 'OPTGROUP') {
        for (const option of child.children) {
          if (option.tagName === 'OPTION') this.options.push(option);
        }
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
for (const m of html.matchAll(/data-image="([^"]+)"/g)) {
  const el = makeElement(`proc-image-tab-${m[1]}`);
  el.dataset.image = m[1];
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

/** The attributes the markup puts on an element, as the browser would have them.
 *
 *  The harness builds elements on demand from the HTML string, so without this
 *  a `data-range="sky"` button exists but carries no dataset, and any wiring
 *  that walks `[data-range=...]` finds nothing -- a test that then "passes"
 *  proves only that nothing was clicked. */
function applyHtmlAttributes(element, id) {
  if (!id) return;
  const at = html.indexOf(`id="${id}"`);
  if (at < 0) return;
  const open = html.lastIndexOf('<', at);
  const close = html.indexOf('>', at);
  if (open < 0 || close < 0) return;
  const tag = html.slice(open, close + 1);
  for (const match of tag.matchAll(/data-([\w-]+)="([^"]*)"/g)) {
    const key = match[1].replace(/-([a-z])/g, (_m, chr) => chr.toUpperCase());
    element.dataset[key] = match[2];
  }
  const klass = /\bclass="([^"]*)"/.exec(tag);
  if (klass && !element.className) element.className = klass[1];
  // Latch buttons carry their state in aria-pressed, not in a value; the
  // page reads it back on the very first check, before any test has fired.
  const pressed = /\baria-pressed="([^"]*)"/.exec(tag);
  if (pressed) element.setAttribute('aria-pressed', pressed[1]);
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
    if (!registry.has(id)) {
      const element = makeElement(id);
      applyHtmlAttributes(element, id);
      registry.set(id, element);
    }
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
    return { ok: true, limits: { preview_px: 3000, output_px: 10000, disk_px: 10000,
                                 min_px: 64, terrain_km_per_px: 0.11845058759433393,
                                 label_outline_px: 9, cpu_count: 8 },
             data_sources: SOURCES };
  }
  if (url === '/api/source') {
    const picked = SOURCES.sources.find((item) => item.id === body.id) || SOURCES.sources[1];
    return { selected: picked.id, sources: SOURCES.sources,
             floor_km_per_px: picked.km_per_px, pyramid: 'LDEM_118m.tif' };
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
      /* The server computes the Chinese calendar from the ephemeris.  This
         label is the real one for that instant; an age-based estimate would
         say 初九, so a page that ignores this field fails the check below. */
      lunar: { year: 2026, month: 4, day: 9, is_leap: false,
               label_zh: '四月初九', label_latin: 'M04D09',
               civil_date: '2026-05-25' },
      angular_diameter_deg: 0.5135, disk_radius_fraction: 0.47,
      viewpoint: (body.when && body.when.viewpoint) || 'earth',
      // The real payload carries the distance the scale is derived from; the
      // stub used to omit it, which made every 同步全月面 resolution read null.
      distance_km: 384400,
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
  if (url === '/api/crescents') {
    /* Three sessions, thinnest first, exactly as the server sorts them.  The
       page must not re-sort: the order is the answer. */
    return {
      items: [
        { local_date: '2026-03-19', night: '蛾眉月', session: 'sunset',
          best_utc: '2026-03-19T10:04:00Z', best_local: '2026-03-19T18:04:00',
          window_start_utc: '2026-03-19T10:04:00Z',
          window_end_utc: '2026-03-19T10:14:00Z',
          illumination_percent: 0.21, elongation_deg: 4.3,
          moon_elevation_deg: 3.3, sun_elevation_deg: -0.4,
          age_days: 0.42, nights_from_new_moon: 0, photo_score: 0.2,
          yallop: 'not_visible' },
        { local_date: '2026-04-17', night: '残月', session: 'sunrise',
          best_utc: '2026-04-16T21:22:00Z', best_local: '2026-04-17T05:22:00',
          window_start_utc: '2026-04-16T21:07:00Z',
          window_end_utc: '2026-04-16T21:37:00Z',
          illumination_percent: 0.60, elongation_deg: 8.1,
          moon_elevation_deg: 5.3, sun_elevation_deg: -1.2,
          age_days: -1.1, nights_from_new_moon: 0, photo_score: 0.3,
          yallop: 'marginal' },
        { local_date: '2026-05-17', night: '蛾眉月', session: 'sunset',
          best_utc: '2026-05-17T10:56:00Z', best_local: '2026-05-17T18:56:00',
          window_start_utc: '2026-05-17T10:11:00Z',
          window_end_utc: '2026-05-17T11:16:00Z',
          illumination_percent: 0.79, elongation_deg: 9.2,
          moon_elevation_deg: 5.8, sun_elevation_deg: -1.0,
          age_days: 0.9, nights_from_new_moon: 1, photo_score: 0.35,
          yallop: 'marginal' },
      ],
    };
  }
  if (url === '/api/curve') {
    if (body.kind === 'sun8') {
      const samples = [];
      for (let index = 0; index < 31; index += 1) {
        const u = index / 30;
        samples.push({
          when_utc: new Date(Date.UTC(2026, 0, 1 + index, 4, 0, 0)).toISOString(),
          local_time: `2026-01-${String(1 + index).padStart(2, '0')} 12:00:00 CST`,
          day_of_year: index + 1, month: 1 + Math.floor(index / 10),
          ra_hours: 18 + 4 * Math.sin(2 * Math.PI * u),
          declination_deg: -23 + 46 * u,
          azimuth_deg: 140 + 60 * Math.sin(2 * Math.PI * u),
          elevation_deg: 20 + 40 * Math.cos(2 * Math.PI * u),
          equation_of_time_minutes: 0,
        });
      }
      return {
        kind: 'sun8', site: { lat: 30, lon: 120 }, tz_offset_hours: 8,
        start_utc: '2026-01-01T00:00:00+00:00',
        end_utc: '2027-01-01T00:00:00+00:00',
        local_hour: 12, local_minute: 0, days: 365, drift_minutes: 0,
        samples,
        extremes: { azimuth_min_deg: 80, azimuth_max_deg: 200,
                    elevation_min_deg: -20, elevation_max_deg: 60,
                    declination_min_deg: -23, declination_max_deg: 23,
                    equation_of_time_min_minutes: -14,
                    equation_of_time_max_minutes: 16 },
      };
    }
    if (body.kind === 'altitude' || body.kind === 'diameter'
        || body.kind === 'libration' || body.kind === 'geometry') {
      const samples = [];
      const count = 48;
      for (let index = 0; index < count; index += 1) {
        const u = index / (count - 1);
        samples.push({
          when_utc: new Date(Date.UTC(2026, 0, 1 + index * 2, 0, 0, 0)).toISOString(),
          local_time: `2026-01-${String(1 + Math.floor(index / 12)).padStart(2, '0')} `
            + `${String((index * 2) % 24).padStart(2, '0')}:00:00 CST`,
          elapsed_hours: index * 2,
          day_of_year: 1 + Math.floor(index / 12), month: 1,
          phase_angle_deg: 180 * (1 - u),
          illumination_percent: 100 * u,
          waxing: true,
          azimuth_deg: 90 + 180 * u,
          elevation_deg: 50 * Math.sin(2 * Math.PI * u),
          angular_diameter_arcsec: 1950 + 80 * Math.cos(2 * Math.PI * u),
          angular_diameter_deg: 0.54,
          distance_km: 384400 - 20000 * Math.cos(2 * Math.PI * u),
          libration_lon_deg: 7 * Math.sin(2 * Math.PI * u),
          libration_lat_deg: 6 * Math.cos(2 * Math.PI * u),
        });
      }
      return {
        kind: body.kind, site: { lat: 30, lon: 120 }, tz_offset_hours: 8,
        start_utc: '2026-01-01T00:00:00+00:00',
        end_utc: '2026-01-05T00:00:00+00:00',
        local_hour: 0, local_minute: 0, days: 4, step_minutes: 120,
        drift_minutes: 0, samples,
        extremes: {
          elevation_min_deg: -50, elevation_max_deg: 50,
          azimuth_min_deg: 90, azimuth_max_deg: 270,
          angular_diameter_min_arcsec: 1870,
          angular_diameter_max_arcsec: 2030,
          libration_lon_min_deg: -7, libration_lon_max_deg: 7,
          libration_lat_min_deg: -6, libration_lat_max_deg: 6,
          phase_angle_min_deg: 0, phase_angle_max_deg: 180,
        },
      };
    }
    if (body.kind === 'earthshine') {
      const models = {
        simulation: { label: '自定义（当前模拟设置）', albedo: 0.367, phase: 'lambert', g: -0.33 },
        allen: { label: 'Allen 1976 · 0.367 · Lambert', albedo: 0.367, phase: 'lambert', g: -0.33 },
        vband: { label: 'V 波段 · 0.434 · Lambert', albedo: 0.434, phase: 'lambert', g: -0.33 },
        robinson: { label: 'Robinson 2026 · 0.242 · Lambert', albedo: 0.242, phase: 'lambert', g: -0.33 },
        robinson_hg: { label: 'Robinson 2026 · 0.23 · HG 相位', albedo: 0.23, phase: 'hg', g: -0.33 },
      };
      const modelKey = models[body.earthshine_model] ? body.earthshine_model : 'simulation';
      const model = models[modelKey];
      const samples = [];
      for (let index = 0; index <= 36; index += 1) {
        const phase = 180 - index * 5;
        const illum = 50 * (1 + Math.cos(phase * Math.PI / 180));
        const magnitude = phase === 0 ? null
          : -2.7 + 0.11 * (180 - phase) + 0.0008 * (180 - phase) ** 2;
        samples.push({
          phase_angle_deg: phase, illumination_percent: illum,
          earth_phase_angle_deg: 180 - phase,
          earthshine_ratio: 1e-4 * Math.max(0.001, 1 - phase / 200),
          moon_magnitude: -12.73,
          moon_apparent_magnitude: -12.73 + 0.03 * phase,
          earthshine_apparent_magnitude: magnitude,
          moon_to_earthshine_ratio: phase === 0
            ? null : 2.9 * Math.pow(10, phase / 40),
        });
      }
      return {
        kind: 'earthshine', when_utc: '2026-05-25T07:30:00+00:00',
        site: { lat: 30, lon: 120 }, tz_offset_hours: 8,
        earth_model: modelKey,
        earth_model_label: model.label,
        earth_phase_model: model.phase,
        earth_phase_g: model.g,
        earth_geometric_albedo: model.albedo,
        distance_km: 384400, earth_distance_km: 384400,
        samples,
        current: {
          phase_angle_deg: 47.9, illumination_percent: 69.6,
          earth_phase_angle_deg: 132.1, earthshine_ratio: 4e-5,
          moon_magnitude: -12.73, moon_apparent_magnitude: -11.3,
          earthshine_apparent_magnitude: 1.2,
          moon_to_earthshine_ratio: 4.3e4,
        },
      };
    }
    const samples = [];
    for (let index = 0; index < 30; index += 1) {
      const u = index / 29;
      samples.push({
        when_utc: new Date(Date.UTC(2026, 0, 1 + index, 13, 0, 0)
          + index * 50.47 * 60000).toISOString(),
        local_time: `2026-01-${String(1 + index).padStart(2, '0')} 21:`
          + `${String(Math.round((index * 50.47) % 60)).padStart(2, '0')}:00 CST`,
        day_of_year: index + 1, month: 1,
        ra_hours: (1 + 23.5 * u + 1.5 * Math.sin(2 * Math.PI * u)) % 24,
        declination_deg: 28 * Math.sin(2 * Math.PI * u),
        azimuth_deg: 90 + 50 * u + 6 * Math.sin(2 * Math.PI * u),
        elevation_deg: 40 + 30 * Math.sin(2 * Math.PI * u),
        phase_angle_deg: 180 * u,
        illumination_percent: 100 * (0.5 + 0.5 * Math.cos(Math.PI * u)),
        waxing: u < 0.5,
      });
    }
    return {
      kind: 'moon8', site: { lat: 30, lon: 120 }, tz_offset_hours: 8,
      start_utc: '2026-01-01T00:00:00+00:00',
      end_utc: '2026-01-31T00:00:00+00:00',
      local_hour: 21, local_minute: 0, days: 30, drift_minutes: 50.47,
      samples,
      extremes: { ra_min_hours: 1.0, ra_max_hours: 24.0,
                  declination_min_deg: -28, declination_max_deg: 28,
                  elevation_min_deg: 10, elevation_max_deg: 70 },
    };
  }
  if (url === '/api/sky') {
    /* A high Moon far from a Sun already below the horizon: the exact case the
       window's automatic close-up exists for.  The tracks are a plain parabola,
       which is all the drawing code needs to be driven through every branch. */
    const start = Date.parse('2026-05-25T06:30:00Z');
    const samples = [];
    for (let index = 0; index <= 48; index += 1) {
      const t = start + index * 10 * 60 * 1000;
      const u = index / 48;
      samples.push({
        when_utc: new Date(t).toISOString(),
        moon_azimuth_deg: 96 + 74 * u,
        moon_elevation_deg: 46 + 14 * Math.sin(Math.PI * u),
        sun_azimuth_deg: 300 + 26 * u,
        sun_elevation_deg: -24 - 8 * u,
      });
    }
    return {
      when_utc: '2026-05-25T07:30:00+00:00',
      site: { lat: 30, lon: 120, name: 'observer' },
      tz_offset_hours: 8,
      span_hours: Number(body.span_hours) || 6,
      step_seconds: 120,
      samples,
      moon: { azimuth_deg: 133, elevation_deg: 60, range_km: 384400 },
      sun: { azimuth_deg: 313, elevation_deg: -28, range_km: 151000000 },
      geometry: {
        elongation_deg: 120.0, illumination_percent_geo: 88.0,
        illumination_percent_topo: 87.8, phase_angle_geo_deg: 35.0,
        moon_age_days: 12.1, angular_diameter_arcsec: 1800,
        crescent_width_arcsec: 1580, bright_limb_angle_deg: 300,
      },
    };
  }
  if (url === '/api/annotations') return planFor(body);
  if (url === '/api/mesh') {
    return {
      info: {
        lat_deg: Number((body.selection || {}).lat ?? 0),
        lon_deg: Number((body.selection || {}).lon ?? 0),
        width_km: 100.0, height_km: 80.0, relief_km: 2.0,
        long_side_mm: 100.0, short_side_mm: 80.0,
        n_faces: 1000, stl_bytes: 50084, terrain_limited: false,
      },
      stl: 'data:model/stl;base64,AAAA',
    };
  }
  if (url === '/api/region') {
    const image = componentImage(body);
    return { render_id: '7', image, image_raw: image,
             info: { component: body.component || 'image',
                     image: { shape: [400, 400] }, region: {
                       ground_sample_km: 0.2, emergence_deg: 3,
                       solar_elevation_deg: 20, width: 400, height: 400,
                       lon_deg: Number((body.selection || {}).lon ?? 0),
                       lat_deg: Number((body.selection || {}).lat ?? 0),
                       span_km: 200,
                       rotation_deg: Number((body.selection || {}).rotation_deg ?? 0),
                       sub_observer_lon_deg: -4.738,
                       sub_observer_lat_deg: 1.377,
                       observer_distance_km: 384400,
                       angular_diameter_deg: 0.5122145,
                     },
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
  if (url === '/api/surface-sun') {
    const start = Date.parse(body.start);
    const end = Date.parse(body.end);
    const step = (Number(body.step_minutes) || 10) * 60000;
    const samples = [];
    for (let stamp = start, index = 0; stamp <= end; stamp += step, index += 1) {
      samples.push({
        when_utc: new Date(stamp).toISOString(),
        local_time: new Date(stamp).toISOString().slice(0, 19) + ' UTC',
        lon_deg: body.moon_lon, lat_deg: body.moon_lat,
        altitude_deg: 30 + 20 * Math.sin(index / 4),
        azimuth_deg: (90 + index * 6) % 360,
        incidence_deg: 60 - 20 * Math.sin(index / 4),
        subsolar_lon_deg: 0, subsolar_lat_deg: 0,
        distance_km: 149000000, light_time_s: 497,
        illuminated: 30 + 20 * Math.sin(index / 4) > 0,
      });
    }
    return { kind: 'surface_sun', site: { lat: 30, lon: 120 },
             moon_lon: body.moon_lon, moon_lat: body.moon_lat,
             start: body.start, end: body.end,
             step_minutes: body.step_minutes, samples };
  }
  if (url === '/api/settings/save') return { saved: { name: 'x', bytes: 10 }, files: [] };
  if (url === '/api/patch') {
    return { id: '1', product: body.product, name: 'patch',
             label: 'SLDEM2015 512 ppd · 辉夜号立体填缝',
             km_per_px: 0.0592,
             bounds: [345.0, 356.0, 48.0, 55.0], megabytes: 140.9 };
  }
  if (/^\/api\/patch\/\d+$/.test(url)) {
    return { id: '1', state: 'done', progress: 1, message: '4777×7375 px',
             label: 'SLDEM2015 512 ppd · 辉夜号立体填缝', km_per_px: 0.0592,
             bytes: 140000000, bounds: [345, 356, 48, 55], error: null };
  }
  if (url === '/api/animation') {
    if (body.preview) {
      return { preview: { image: 'data:image/png;base64,preview', width: 96, height: 72,
                          when_utc: '2026-05-25T15:30:00+00:00', seconds: 0.4 } };
    }
    return { id: '3', state: 'running', total: 8 };
  }
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

/* What /api/health says about elevation products: two local rasters at 59 m and
   118 m, plus one online product that is not on this machine. */
const SOURCES = {
  sources: [
    { id: '/kernels/ldem_512.tif', name: 'ldem_512.tif', km_per_px: 0.0592252938,
      metres_per_px: 59.2, pixels_per_degree: 512, bytes: 34e9, mtime: 1 },
    { id: '/kernels/LDEM_118m.tif', name: 'LDEM_118m.tif', km_per_px: 0.11845058759433393,
      metres_per_px: 118.5, pixels_per_degree: 256, bytes: 8.5e9, mtime: 1 },
  ],
  selected: '/kernels/LDEM_118m.tif',
  floor_km_per_px: 0.11845058759433393,
  online: [
    { id: 'sldem2015-512', label: 'SLDEM2015 512 ppd', pixels_per_degree: 512,
      metres_per_px: 59.2, available: false, note: '需要拼接', fetch: '见文档' },
  ],
};

const fetchCalls = [];
async function fetchStub(url, options) {
  const body = options && options.body ? JSON.parse(options.body) : {};
  fetchCalls.push({ url, body });
  // The STL path deliberately falls back to the JSON endpoint in this harness
  // (a 404 from the binary endpoint), which is the old-server compatibility
  // path.  A GLB request gets a binary response with its info header.
  if (url === '/api/moon/glb') {
    const info = {
      format: 'glb', units: 'm', scope: 'moon',
      diameter_mm: 200.0, diameter_km: 3474.8,
      resolution: 1024, n_vertices: 462241, n_faces: 921600,
      has_texture: true, texture_size: [2048, 1024],
    };
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: { get: (name) => (name === 'X-Moonlib-Mesh-Info'
        ? encodeURIComponent(JSON.stringify(info)) : null) },
      blob: async () => ({ size: 29322016, type: 'model/gltf-binary' }),
    };
  }
  if (url === '/api/moon/blender') {
    const info = {
      format: 'blend', units: 'm', scope: 'moon',
      diameter_mm: 200.0, diameter_km: 3474.8,
      resolution: 1024, has_texture: true,
      scenes: ['Current phase'],
    };
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: { get: (name) => (name === 'X-Moonlib-Mesh-Info'
        ? encodeURIComponent(JSON.stringify(info)) : null) },
      blob: async () => ({ size: 36257834, type: 'application/x-blender' }),
    };
  }
  if (url === '/api/mesh/blender') {
    const selection = body.selection || {};
    const info = {
      lat_deg: Number(selection.lat ?? 0), lon_deg: Number(selection.lon ?? 0),
      width_km: 100.0, height_km: 80.0, relief_km: 2.0,
      long_side_mm: 100.0, short_side_mm: 80.0, n_faces: 1000,
      format: 'blend', units: 'm', has_texture: true,
      solar_altitude_deg: 42.92, solar_azimuth_deg: 65.39,
      solar_illuminated: true,
      scenes: ['Studio', 'Lunar Sun', 'Lunar Terminator'],
    };
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: { get: (name) => (name === 'X-Moonlib-Mesh-Info'
        ? encodeURIComponent(JSON.stringify(info)) : null) },
      blob: async () => ({ size: 456789, type: 'application/x-blender' }),
    };
  }
  if (url === '/api/mesh/file' && body.mesh && body.mesh.format === 'glb') {
    const selection = body.selection || {};
    const info = {
      lat_deg: Number(selection.lat ?? 0), lon_deg: Number(selection.lon ?? 0),
      width_km: 100.0, height_km: 80.0, relief_km: 2.0,
      long_side_mm: 100.0, short_side_mm: 80.0, n_faces: 1000,
      format: 'glb', units: 'm', sample_km: 0.1,
      terrain_source: 'SLDEM2015_512.tif',
      terrain_native_km_per_pixel: 0.0592,
    };
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: { get: (name) => (name === 'X-Moonlib-Mesh-Info'
        ? encodeURIComponent(JSON.stringify(info)) : null) },
      blob: async () => ({ size: 123456, type: 'model/gltf-binary' }),
    };
  }
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
  /* 并行数 now starts on 自动: /api/health spells the host's own core count into
     the field, and the *next* render carries it.  (The very first frame is asked
     for before the health reply lands, so it still goes out as "let the library"
     -- which is the same number of workers.) */
  check('并行数 starts on the host\'s own core count',
    document.getElementById('parallel').value === String(T.LIMITS.cpu_count),
    document.getElementById('parallel').value);
  set('parallel', T.LIMITS.cpu_count);
  fire('parallel', 'change');
  // 并行数 is read when a render is asked for, so ask for one.
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 600));
  check('and the render request carries that ceiling',
    fetchCalls.filter((call) => call.url === '/api/disk').pop().body.parallel
      === T.LIMITS.cpu_count,
    JSON.stringify(fetchCalls.filter((call) => call.url === '/api/disk').pop().body.parallel));
  check('并行数 explains itself on hover rather than in a visible hint',
    /渲染按横带分给多个线程/.test(document.getElementById('parallel').title) &&
    /8 核/.test(document.getElementById('parallel').title) &&
    ![...document.querySelectorAll('p.hint')].some(
      (p) => p.textContent.includes('渲染按横带')));
  check('the preview canvas took the rendered size',
    document.getElementById('disk').width === 1200);
  check('the class switches were built from the class list',
    document.querySelectorAll('.cls-chip').length === T.LAND_CLASSES.length,
    document.querySelectorAll('.cls-chip').length);
  const lunar100Chip = [...document.querySelectorAll('.cls-chip')]
    .find((chip) => chip.children[0] && chip.children[0].id === 'class-lunar100');
  check('the Lunar 100 switch explains its first unannotatable entries on hover',
    !!lunar100Chip && /前 3 项/.test(lunar100Chip.title) &&
    /月球本体/.test(lunar100Chip.title) &&
    /Sky & Telescope/.test(lunar100Chip.title),
    lunar100Chip ? lunar100Chip.title : 'no chip');
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
  /* 5 of them: 模拟设置's km/px, the two size menus, 标注, and the disk preset --
     plus the animation's own km/px row, which is marked data-own so re-copying
     the ladder cannot duplicate it. */
  check('every menu offers a way in by hand',
    (html.match(/<option value="custom"/g) || []).length === 6,
    (html.match(/<option value="custom"/g) || []).length);
  check('the preview menu offers the sizes the page may ask for',
    previewMenu.length >= 7 && previewMenu.every((px) => px <= T.LIMITS.preview_px),
    previewMenu.join(','));
  check('and tops out at 3000, the preview cap',
    previewMenu.includes(3000) && !previewMenu.includes(4000) &&
    !previewMenu.includes(5000) && T.LIMITS.preview_px === 3000,
    T.LIMITS.preview_px);
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
  console.log('the far-side viewpoint');
  /* 视角 chooses the camera, not the observer: the site still travels for the
     real Moon report, but the disk request must say which side it is about.
     It is a small latch on the full-disk title bar, not a left-column field. */
  const viewpointButton = document.getElementById('viewpoint');
  check('the camera starts on the near side',
    viewpointButton.getAttribute('aria-pressed') === 'false');
  fire('viewpoint', 'click');
  await new Promise((resolve) => setTimeout(resolve, 500));
  const farBody = fetchCalls.filter((call) => call.url === '/api/disk').pop().body;
  check('the far-side request carries the viewpoint',
    farBody.when.viewpoint === 'far_side', JSON.stringify(farBody.when));
  check('and still carries the observing site for the Moon report',
    farBody.when.observer.lat === 30 && farBody.when.observer.lon === 120);
  check('the title-bar button is latched',
    viewpointButton.getAttribute('aria-pressed') === 'true');
  check('and the disk readout names the far side',
    /月背/.test(document.getElementById('disk-info-size').textContent));
  fire('viewpoint', 'click');
  await new Promise((resolve) => setTimeout(resolve, 500));
  check('the near-side latch goes away again',
    viewpointButton.getAttribute('aria-pressed') === 'false');
  check('and the request goes back to earth',
    fetchCalls.filter((call) => call.url === '/api/disk').pop().body.when.viewpoint
      === 'earth');

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
  // 黑白地貌 is a render mode, not a display filter: the flag has to travel
  // with the light block so the DEM-only frame is actually drawn that way.
  set('surface-texture', 'relief');
  fire('surface-texture', 'change');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  check('the surface-texture choice reaches the render',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.surface_texture === false,
    JSON.stringify(fetchCalls.filter((c) => c.url === '/api/disk').pop().body.surface_texture));
  set('surface-texture', 'texture');
  fire('surface-texture', 'change');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  check('and switching it back asks for the colour mosaic again',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.surface_texture === true);
  set('terrain-denoise', '2');
  fire('terrain-denoise', 'change');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));
  check('the DEM denoise strength reaches the render',
    fetchCalls.filter((c) => c.url === '/api/disk').pop().body.terrain_denoise === 2);
  set('terrain-denoise', '0');
  fire('terrain-denoise', 'change');
  fire('refresh', 'click');
  await new Promise((resolve) => setTimeout(resolve, 200));

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
    check('the master label switch no longer hides the geometric overlays',
      texts.length > 0 && without === 0 && T.state.labelsOn === false,
      texts.join(' | '));
    document.getElementById('annotate').setAttribute('aria-pressed', 'true');
    T.state.labelsOn = true;
    drawn.texts.length = 0;
    T.drawViewLabels('disk');
    const withSwitch = drawn.texts.map((entry) => entry.text);
    check('and the ruler is still drawn with the switch on',
      withSwitch.some((text) => /^\d+(\.\d+)? (km|m)$/.test(text)),
      withSwitch.join(' | '));
    T.state.labelsOn = false;
    document.getElementById('annotate').setAttribute('aria-pressed', 'false');
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
  set('label-satellites', false);
  fire('label-satellites', 'change');
  await tick();
  check('卫星坑 off is sent to the server as its own switch',
    fetchCalls.filter((c) => c.url === '/api/annotations').pop()
      .body.satellites === false);
  set('label-satellites', true);
  fire('label-satellites', 'change');
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
  console.log('the 3D model export');
  fire('proc-image-tab-model', 'click');
  check('the 3D pane opens in the imaging/post-processing strip',
    document.getElementById('proc-model').hidden === false &&
    document.getElementById('proc-imaging').hidden === true &&
    document.getElementById('proc-post').hidden === true);
  check('a selected box enables the export',
    document.getElementById('mesh-export').disabled === false);
  set('mesh-size-mm', 1000);
  fire('mesh-size-mm', 'change');
  {
    const planText = document.getElementById('mesh-plan').textContent;
    const match = planText.match(/长边尺寸 (\d+) mm 时匹配当前最高分辨率/);
    check('the plan says how large the model must be to match the source at 768',
      /模型 1000 ×/.test(planText) && /采样 768/.test(planText) &&
      /1 mm = /.test(planText) &&
      !!match && Number(match[1]) > 1000,
      planText);
  }
  set('mesh-size-mm', 100);
  fire('mesh-size-mm', 'change');
  fire('mesh-export', 'click');
  await tick();
  await tick();
  const meshCall = fetchCalls.filter((call) => call.url === '/api/mesh').pop();
  check('the export sends the selection and its aspect',
    !!meshCall && meshCall.body.selection.lon === T.state.selection.lon &&
    typeof meshCall.body.selection.aspect === 'number',
    meshCall ? JSON.stringify(meshCall.body.selection) : 'no call');
  check('and the printing parameters',
    !!meshCall && meshCall.body.mesh.long_side_mm === 100 &&
    meshCall.body.mesh.base_depth_km === 2 &&
    meshCall.body.mesh.vertical_exaggeration === 1 &&
    meshCall.body.mesh.resolution_mode === 'auto' &&
    meshCall.body.mesh.resolution >= 2 &&
    meshCall.body.mesh.resolution <= 768 &&
    meshCall.body.mesh.projection === true,
    meshCall ? JSON.stringify(meshCall.body.mesh) : 'no call');
  check('the pane reports what was exported',
    /已导出/.test(document.getElementById('mesh-status').textContent) &&
    /100\.0×80\.0 km/.test(document.getElementById('mesh-status').textContent),
    document.getElementById('mesh-status').textContent);
  set('mesh-corrected', true);
  fire('mesh-corrected', 'change');
  check('the corrected box locks the projection switch on',
    document.getElementById('mesh-corrected').checked === true &&
    document.getElementById('mesh-projection').checked === true &&
    document.getElementById('mesh-projection').disabled === true);
  check('the page knows the box is the corrected one',
    T.selectionCorrected() === true);
  const correctedOutline = T.correctedBoundaryPixels();
  check('its irregular boundary has points',
    Array.isArray(correctedOutline) && correctedOutline.length > 8);
  let correctedJump = 0;
  if (correctedOutline) {
    for (let index = 1; index < correctedOutline.length; index += 1) {
      correctedJump = Math.max(correctedJump, Math.hypot(
        correctedOutline[index].x - correctedOutline[index - 1].x,
        correctedOutline[index].y - correctedOutline[index - 1].y));
    }
  }
  check('the corrected boundary is one clean loop, not a zig-zag',
    correctedJump < 2, correctedJump);
  {
    const baseSelection = { ...T.state.selection };
    let rotatedClosed = true;
    let resizedClosed = true;
    for (const deg of [0, 30, 90, 150]) {
      T.state.selection = { ...baseSelection, rotationDeg: deg };
      if (!T.correctedBoundaryPixels()) rotatedClosed = false;
    }
    for (const factor of [0.4, 1.5]) {
      T.state.selection = {
        ...baseSelection,
        radiusFraction: baseSelection.radiusFraction * factor,
      };
      if (!T.correctedBoundaryPixels()) resizedClosed = false;
    }
    T.state.selection = baseSelection;
    check('rotating the box keeps the corrected shape', rotatedClosed);
    check('resizing the box keeps the corrected shape', resizedClosed);
  }
  check('and it has a usable effective frame',
    T.effectiveSelectionArcsec() > 0 && T.effectiveSelectionAspect() > 0);
  fire('mesh-export', 'click');
  await tick();
  await tick();
  const correctedCall = fetchCalls.filter((call) => call.url === '/api/mesh').pop();
  check('the corrected export says so in the request',
    !!correctedCall && correctedCall.body.selection.corrected === true,
    correctedCall ? JSON.stringify(correctedCall.body.selection) : 'no call');
  set('mesh-corrected', false);
  fire('mesh-corrected', 'change');
  check('switching it off unlocks the projection switch again',
    document.getElementById('mesh-projection').disabled === false);
  set('mesh-projection', false);
  fire('mesh-projection', 'change');
  fire('mesh-export', 'click');
  await tick();
  await tick();
  const plainCall = fetchCalls.filter((call) => call.url === '/api/mesh').pop();
  check('turning projection off is sent to the server',
    !!plainCall && plainCall.body.mesh.projection === false,
    plainCall ? JSON.stringify(plainCall.body.mesh) : 'no call');
  set('mesh-projection', true);
  fire('mesh-projection', 'change');
  fire('mesh-export-glb', 'click');
  await tick();
  await tick();
  const glbCall = fetchCalls.filter((call) => call.url === '/api/mesh/file').pop();
  check('the GLB export uses the binary endpoint',
    !!glbCall && glbCall.body.mesh.format === 'glb',
    glbCall ? JSON.stringify(glbCall.body.mesh) : 'no call');
  check('the GLB export reports its file and leaves a download link',
    /已导出/.test(document.getElementById('mesh-status').textContent) &&
    /\.glb/.test(document.getElementById('mesh-status').textContent) &&
    document.getElementById('mesh-download').hidden === false &&
    /\.glb$/.test(document.getElementById('mesh-download').download),
    document.getElementById('mesh-status').textContent + ' / ' +
    document.getElementById('mesh-download').download);
  fire('mesh-export-blend', 'click');
  await tick();
  await tick();
  const blendCall = fetchCalls.filter((call) => call.url === '/api/mesh/blender').pop();
  check('the Blender preview uses the current mesh selection',
    !!blendCall && blendCall.body.mesh.format === 'glb' &&
    blendCall.body.mesh.blend_light === 'phase' &&
    Number.isFinite(Number(blendCall.body.selection.lat)),
    blendCall ? JSON.stringify(blendCall.body.mesh) : 'no call');
  check('the Blender preview reports its file and leaves a .blend link',
    /已生成/.test(document.getElementById('mesh-status').textContent) &&
    /\.blend/.test(document.getElementById('mesh-status').textContent) &&
    document.getElementById('mesh-download').hidden === false &&
    /\.blend$/.test(document.getElementById('mesh-download').download),
    document.getElementById('mesh-status').textContent + ' / ' +
    document.getElementById('mesh-download').download);
  set('mesh-scope', 'moon');
  fire('mesh-scope', 'change');
  check('the whole-Moon scope shows its size/resolution controls and hides STL',
    document.getElementById('mesh-selection-controls').hidden === true &&
    document.getElementById('mesh-moon-controls').hidden === false &&
    document.getElementById('mesh-lighting-row').hidden === true &&
    document.getElementById('mesh-export').hidden === true &&
    /全月面/.test(document.getElementById('mesh-plan').textContent) &&
    /直径 200 mm/.test(document.getElementById('mesh-plan').textContent) &&
    /分辨率 1024/.test(document.getElementById('mesh-plan').textContent),
    document.getElementById('mesh-plan').textContent);
  set('mesh-moon-size-mm', 400);
  set('mesh-moon-resolution', 512);
  fire('mesh-moon-size-mm', 'change');
  fire('mesh-moon-resolution', 'change');
  check('the whole-Moon plan follows the diameter and resolution',
    /直径 400 mm/.test(document.getElementById('mesh-plan').textContent) &&
    /分辨率 512/.test(document.getElementById('mesh-plan').textContent),
    document.getElementById('mesh-plan').textContent);
  fire('mesh-export-glb', 'click');
  await tick();
  await tick();
  const moonGlbCall = fetchCalls.filter((call) => call.url === '/api/moon/glb').pop();
  check('the whole-Moon GLB export sends the chosen size and resolution',
    !!moonGlbCall && moonGlbCall.body.moon.size_mm === 400 &&
    moonGlbCall.body.moon.resolution === 512,
    moonGlbCall ? JSON.stringify(moonGlbCall.body) : 'no call');
  check('the whole-Moon GLB reports its texture and leaves a .glb link',
    /全月面/.test(document.getElementById('mesh-status').textContent) &&
    /\.glb/.test(document.getElementById('mesh-status').textContent) &&
    document.getElementById('mesh-download').hidden === false &&
    /\.glb$/.test(document.getElementById('mesh-download').download),
    document.getElementById('mesh-status').textContent + ' / ' +
    document.getElementById('mesh-download').download);
  fire('mesh-export-blend', 'click');
  await tick();
  await tick();
  const moonBlendCall = fetchCalls.filter((call) => call.url === '/api/moon/blender').pop();
  check('the whole-Moon Blender scene uses the same size and resolution',
    !!moonBlendCall && moonBlendCall.body.moon.size_mm === 400 &&
    moonBlendCall.body.moon.resolution === 512 &&
    !('blend_light' in moonBlendCall.body.moon),
    moonBlendCall ? JSON.stringify(moonBlendCall.body) : 'no call');
  check('the whole-Moon Blender scene is current-phase only',
    /已生成/.test(document.getElementById('mesh-status').textContent) &&
    /当前月相光照/.test(document.getElementById('mesh-status').textContent) &&
    /\.blend$/.test(document.getElementById('mesh-download').download),
    document.getElementById('mesh-status').textContent + ' / ' +
    document.getElementById('mesh-download').download);
  set('mesh-scope', 'selection');
  fire('mesh-scope', 'change');
  check('returning to the selection restores its controls, lighting and STL',
    document.getElementById('mesh-selection-controls').hidden === false &&
    document.getElementById('mesh-moon-controls').hidden === true &&
    document.getElementById('mesh-lighting-row').hidden === false &&
    document.getElementById('mesh-export').hidden === false &&
    /模型/.test(document.getElementById('mesh-plan').textContent),
    document.getElementById('mesh-plan').textContent);
  fire('proc-image-tab-imaging', 'click');
  check('the 3D page closes again',
    document.getElementById('proc-model').hidden === true &&
    document.getElementById('proc-imaging').hidden === false);

  console.log('');
  console.log('generating a close-up refreshes its labels');
  /* A capped render must keep the box's proportions.  Clamping both sides to
     the cap independently turned a 2:1 frame into a square 4096×4096, so the
     saved picture was squeezed and the rendered field no longer matched the
     box. */
  {
    const savedSelection = T.state.selection;
    const savedKmPx = document.getElementById('out-kmpx').value;
    T.state.selection = {
      lon: 0, lat: 0, radiusFraction: 0.5, rotationDeg: 0, aspect: 0.5,
    };
    document.getElementById('out-kmpx').value = '0.0592';
    const capped = T.renderPixels();
    check('a capped render keeps a wide box 2:1 instead of becoming square',
      capped.width >= capped.height * 1.99 &&
      Math.max(capped.width, capped.height) <= T.LIMITS.output_px,
      JSON.stringify(capped));
    T.state.selection = savedSelection;
    document.getElementById('out-kmpx').value = savedKmPx;
  }
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
  function echoToAnimShadow() {
    const before = fetchCalls.filter((c) => c.url === '/api/animation').length;
    set('shadow-steps', '256');
    fire('shadow-steps', 'change');
    const body = T.animationRequest();
    check('and the animation carries it too',
      body.output.shadow_steps === 256,
      JSON.stringify(body.output.shadow_steps));
    if (fetchCalls.filter((c) => c.url === '/api/animation').length !== before) {
      throw new Error('animationRequest must not render by itself');
    }
  }

  /* 阴影精度: 自动 leaves the key out (the server's per-kind policy decides),
     a chosen number travels with the close-up request. */
  {
    const auto = fetchCalls.filter((c) => c.url === '/api/region').pop();
    check('阴影精度 自动 leaves the step budget to the server',
      auto.body.output.shadow_steps === undefined,
      JSON.stringify(auto.body.output));
    set('shadow-steps', '512');
    fire('shadow-steps', 'change');
    fire('generate', 'click');
    await new Promise((resolve) => setTimeout(resolve, 600));
    const chosenCall = fetchCalls.filter((c) => c.url === '/api/region').pop();
    check('and a chosen 阴影精度 travels with the request',
      chosenCall.body.output.shadow_steps === 512,
      JSON.stringify(chosenCall.body.output.shadow_steps));
    echoToAnimShadow();
    set('shadow-steps', 'auto');
    fire('shadow-steps', 'change');
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
      'basin,dome,imp,impact,libration,lunar100,other,pit,pyroclastic,rima,statio',
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
      off.join(',') === 'basin,dome,imp,impact,libration,lunar100,other,pit,pyroclastic,statio',
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
    check('but switching a geometric overlay on does not turn the names on',
      T.state.labelsOn === false,
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
  /* 11 event kinds plus 极低照明度, which shares the range boxes and the list
     but is a crescent-session report rather than an instant list. */
  check('the kind switches are single-choice pills',
    kindChips().length === 12 && T.state.eventKind === 'full_moon',
    String(kindChips().length));
  /* 极低照明度: same range boxes, a different report -- sessions ranked by how
     thin they are, not instants ranked by date. */
  {
    const callsBefore = fetchCalls.filter((c) => c.url === '/api/crescents').length;
    fire('event-kind-crescent', 'change');
    await new Promise((resolve) => setTimeout(resolve, 80));
    const call = fetchCalls.filter((c) => c.url === '/api/crescents').pop();
    check('极低照明度 asks the crescent endpoint',
      fetchCalls.filter((c) => c.url === '/api/crescents').length === callsBefore + 1 &&
      !!call && !!call.body.start && !!call.body.end && call.body.limit === 15,
      call ? JSON.stringify({ start: call.body.start, limit: call.body.limit }) : 'no call');
    // The harness has no container-selector engine: the rows are the list's own
    // children, in the order the page appended them (date/night, thinness,
    // geometry, best + range).
    const rows = document.getElementById('event-list').children
      .filter((el) => String(el.className).includes('crescent-row'));
    const cell = (row, index) => String((row.children[index] || {}).textContent || '');
    check('and lists one row per session', rows.length === 3, String(rows.length));
    check('thinnest first, in the server\'s order',
      /0\.21/.test(cell(rows[0], 1)) && /0\.60/.test(cell(rows[1], 1))
      && /0\.79/.test(cell(rows[2], 1)),
      rows.map((row) => cell(row, 1)).join(' | '));
    check('each row says which night and which half of the day',
      /蛾眉月/.test(cell(rows[0], 0)) && /残月/.test(cell(rows[1], 0))
      && rows[1].dataset.night === 'waning',
      rows.map((row) => cell(row, 0)).join(' | '));
    check('with the best instant and the recommended range',
      /最佳 18:04/.test(cell(rows[0], 3)) && /推荐 18:04–18:14/.test(cell(rows[0], 3)),
      cell(rows[0], 3));
    check('and the geometry the choice was made on',
      /距角 4\.3°/.test(cell(rows[0], 2)) && /高度 3\.3°/.test(cell(rows[0], 2)),
      cell(rows[0], 2));
    fire('event-kind-full_moon', 'change');
    await new Promise((resolve) => setTimeout(resolve, 60));
  }

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
  console.log('');
  console.log('the event table window and its CSV');
  {
    const eventTable = [
      T.eventRowFromForecast({
        kind: 'xvl', label: '月面 X / V（Blanchinus B）',
        when_utc: '2026-05-20T19:00:00+00:00', altitude_deg: 31.5,
        detail: '太阳高度 0–1.5°　持续 196 min　高度 +31.5°', observable: true,
        window_start_utc: '2026-05-20T19:00:00+00:00',
        window_end_utc: '2026-05-20T22:16:00+00:00',
      }),
      T.eventRowFromForecast({
        kind: 'full_moon', label: '满月',
        when_utc: '2026-05-01T12:00:00+00:00', altitude_deg: -12.0,
        detail: '地心距 400,000 km', observable: false,
      }),
    ];
    T.state.eventTable = eventTable;
    T.state.eventImported = false;
    T.openEventWindow();
    check('the table window opens',
      document.getElementById('event-window-backdrop').hidden === false);
    const body = document.getElementById('event-window-rows');
    check('one row per event',
      body.children.length === 2, String(body.children.length));
    const xvRow = [...body.children].find(
      (row) => row.children[1].textContent.includes('X / V'));
    // X/V has its own column set: date, event, best, start, end, duration,
    // altitude, ... -- the first six cells are the window, not the phase table.
    check('the X/V row carries its own window columns',
      !!xvRow && xvRow.children[0].textContent === '05-21'
      && xvRow.children[1].textContent.includes('X / V')
      && xvRow.children[2].textContent === '05-21 03:00'
      && xvRow.children[3].textContent === '05-21 03:00'
      && xvRow.children[4].textContent.includes('05-21')
      && xvRow.children[5].textContent.includes('196 min')
      && xvRow.children[6].textContent.includes('31.5'),
      xvRow ? [...xvRow.children].map((cell) => cell.textContent).join(' | ') : 'no row');
    T.state.eventSort = { key: 'altitude', dir: 1 };
    T.renderEventWindowRows();
    check('clicking a column sorts by it',
      body.children[0].children[6].textContent.includes('-12.0'),
      body.children[0].children[6].textContent);
    T.state.eventSort = { key: 'altitude', dir: -1 };
    T.renderEventWindowRows();
    check('and the same column flips direction',
      body.children[0].children[6].textContent.includes('31.5'),
      body.children[0].children[6].textContent);
    T.state.eventSort = { key: 'date', dir: 1 };
    T.renderEventWindowRows();
    const before = document.getElementById('when').value;
    xvRow.fire('dblclick', { type: 'dblclick' });
    check('double-clicking a table row sets the main time',
      document.getElementById('when').value !== before
      && document.getElementById('when').value === '2026-05-21T03:00',
      document.getElementById('when').value);
    document.getElementById('when').value = before;

    const csv = T.eventCsvText(eventTable);
    check('the CSV starts with the documented header',
      csv.startsWith('kind,label,when_utc,altitude_deg,observable,'));
    const back = T.eventsFromCsv(csv);
    check('the CSV round-trips',
      back.length === 2 && back[0].kind === 'xvl'
      && back[0].window_end_utc === '2026-05-20T22:16:00.000Z'
      && back[1].observable === false,
      JSON.stringify(back[0]));
    const quoted = T.eventsFromCsv(
      'kind,label,when_utc,altitude_deg,observable,window_start_utc,'
      + 'window_end_utc,detail\r\n'
      + 'xvl,"X, V",2026-05-20T19:00:00Z,0,true,,,'
      + '"太阳高度 0–1.5°，持续 196 min"\r\n');
    check('quoted commas and Chinese survive the import',
      quoted.length === 1 && quoted[0].label === 'X, V'
      && quoted[0].detail.includes('，'),
      JSON.stringify(quoted));
    const broken = (() => {
      try { T.eventsFromCsv('date,title\n2026-01-01,x'); return 'no throw'; }
      catch (error) { return String(error.message); }
    })();
    check('a file without when_utc is refused', /when_utc/.test(broken), broken);

    // A phase row gets a different set: no start/end/duration, and no
    // earthshine column for a full moon.  The values come from metrics, so a
    // CSV imported without them still shows the fallback em dashes.
    T.state.eventTable = [T.eventRowFromForecast({
      kind: 'full_moon', label: '满月',
      when_utc: '2026-05-01T12:00:00+00:00', altitude_deg: 40.2,
      detail: '视直径 33′ 00.0″', observable: true,
      metrics: { age_days: 14.2, lunar: '八月十五', phase_angle_deg: 5.0,
                 illumination_percent: 99.8, diameter_arcmin: 33.0,
                 altitude_deg: 40.2, azimuth_deg: 180.0,
                 distance_km: 384400, magnitude: -12.7,
                 surface_brightness: 3.4, earthshine_magnitude: null },
    })];
    T.renderEventWindowRows();
    const phaseHead = document.getElementById('event-window-head');
    const phaseLabels = [...phaseHead.children].map((cell) => cell.textContent);
    check('a phase row gets the phase column set plus the note column',
      phaseHead.children.length === 14 &&
      phaseLabels.includes('月龄') && phaseLabels.includes('农历') &&
      phaseLabels.includes('亮度') && phaseLabels.includes('月面亮度') &&
      phaseLabels.includes('备注') &&
      !phaseLabels.includes('地照亮度') && !phaseLabels.includes('最佳时刻'),
      phaseLabels.join(' / '));
    const phaseCells = [...body.children[0].children].map((cell) => cell.textContent);
    check('and the metric columns are formatted for a reader',
      phaseCells[3] === '14.20 d' && phaseCells[4] === '八月十五' &&
      phaseCells[7] === '33.00′' && phaseCells[8] === '+40.2°' &&
      phaseCells[10] === '384,400 km' && phaseCells[11] === '-12.70 mag' &&
      phaseCells[12] === '3.40 mag/″²' &&
      phaseCells[13] === '视直径 33′ 00.0″',
      phaseCells.join(' | '));
    T.state.eventTable = eventTable;
    T.renderEventWindowRows();

    fire('event-window-export', 'click');
    check('exporting reports the file',
      document.getElementById('event-window-status').textContent.includes('已导出'),
      document.getElementById('event-window-status').textContent);
    fire('event-window-close', 'click');
    check('and the table closes',
      document.getElementById('event-window-backdrop').hidden === true);
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
  fire('proc-image-tab-post', 'click');
  check('the processing section has 成像模拟 and 后期处理 pages',
    document.getElementById('proc-imaging').hidden === true &&
    document.getElementById('proc-post').hidden === false &&
    !document.getElementById('mirror-h').hidden &&
    !document.getElementById('refresh-image-proc').hidden);
  fire('proc-image-tab-imaging', 'click');
  check('and the imaging page comes back',
    document.getElementById('proc-imaging').hidden === false &&
    document.getElementById('proc-post').hidden === true);
  fire('diffraction-toggle', 'click');
  check('衍射环 itself opens the PSF parameters',
    document.getElementById('diffraction-toggle')
      .getAttribute('aria-expanded') === 'true' &&
    document.getElementById('diffraction-psf').hidden === false);
  fire('diffraction-toggle', 'click');
  check('and clicking it again folds them away',
    document.getElementById('diffraction-toggle')
      .getAttribute('aria-expanded') === 'false' &&
    document.getElementById('diffraction-psf').hidden === true);
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
  check('时间步长 always shows every common step, with a custom field',
    /id="anim-step"[^>]*>/.test(html) &&
    ['1', '5', '10', '15', '30', '60']
      .every((value) => new RegExp(`<option value="${value}"`).test(html)) &&
    /<option value="custom">自定义…<\/option>/.test(html) &&
    /id="anim-step-custom"/.test(html),
    'select + custom');
  check('the plan line gives the clip duration from frames and fps',
    /共 \d+ 帧，[\d.]+ s/.test(document.getElementById('anim-span').textContent),
    document.getElementById('anim-span').textContent);
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
    /* 单帧预览: the same request with `preview`, answered by one finished frame,
       saved as a picture -- so the reader can see the opener without waiting
       out the run. */
    const previewBefore = fetchCalls.filter((c) => c.url === '/api/animation').length;
    fire('anim-preview', 'click');
    await new Promise((resolve) => setTimeout(resolve, 80));
    const previewCall = fetchCalls.filter((c) => c.url === '/api/animation').pop();
    check('单帧预览 asks the animation route for one frame',
      fetchCalls.filter((c) => c.url === '/api/animation').length === previewBefore + 1 &&
      previewCall.body.preview === true &&
      !!previewCall.body.start && !!previewCall.body.end,
      JSON.stringify({ preview: previewCall.body.preview }));
    check('the frame it comes back with is shown in the pane',
      document.getElementById('anim-preview-box').hidden === false &&
      /^data:image\/png/.test(document.getElementById('anim-preview-image').src),
      `${document.getElementById('anim-preview-box').hidden} / ` +
      document.getElementById('anim-preview-image').src);
    check('and captioned with its size and instant',
      /96×72/.test(document.getElementById('anim-preview-note').textContent) &&
      /2026-05-25/.test(document.getElementById('anim-preview-note').textContent),
      document.getElementById('anim-preview-note').textContent);

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
     so turning it on from 高分辨率模拟 brings that pane forward. */
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
      document.getElementById('sharpen-radius').value === '1' &&
      document.getElementById('label-density').value === '0.5' &&
      (!sent || sent.body.processing.sharpen_radius_px === 1),
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
  check('and every reset button is the word 复位, not a glyph',
    [...document.querySelectorAll('button.reset')].length >= 4 &&
    [...document.querySelectorAll('button.reset')]
      .every((button) => button.textContent.trim() === '复位' &&
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
  /* 经纬度：a floating readout in the picture's bottom-right corner, not a
     cell in the footer.  It follows the pointer, and a selected landform pins
     it to that feature's own marker until the selection goes. */
  body.fire('pointermove', { clientX: 280, clientY: 280, preventDefault() {} });
  const coordCell = document.getElementById('coord-hud-disk');
  check('the full-disk HUD shows the pointer coordinate',
    coordCell.hidden === false &&
    /经度 \d+\.\d+°[EW]/.test(coordCell.textContent) &&
    /纬度 \d+\.\d+°[NS]/.test(coordCell.textContent),
    coordCell.textContent);
  check('the coordinate no longer shares the footer line',
    document.getElementById('disk-info').textContent.indexOf('经度') < 0);
  {
    const plan = T.state.labels.disk;
    const item = plan && plan.items && plan.items.find((entry) =>
      Number.isFinite(Number(entry.lon_deg)));
    if (item) {
      T.overlay.selected = { kind: 'label', view: 'disk', key: T.labelKey(item) };
      T.paintCoordReadout('disk');
      const pinned = coordCell.textContent;
      body.fire('pointermove', { clientX: 100, clientY: 100, preventDefault() {} });
      check('a selected landform pins the HUD to its marker',
        /选中地貌/.test(pinned) &&
        coordCell.textContent === pinned &&
        coordCell.textContent.includes(Math.abs(Number(item.lon_deg)).toFixed(2)),
        pinned + ' / ' + coordCell.textContent);
      T.overlay.selected = null;
      T.paintCoordReadout('disk');
      check('and the pointer coordinate returns after deselecting',
        !/选中地貌/.test(coordCell.textContent), coordCell.textContent);
    }
  }
  /* The close-ups carry the same readout, in their own corners. */
  {
    const rawBody = document.getElementById('body-raw');
    rawBody.fire('pointermove', { clientX: 280, clientY: 280,
                                 preventDefault() {} });
    const rawHud = document.getElementById('coord-hud-raw');
    const procHud = document.getElementById('coord-hud-proc');
    check('the close-up HUD reads the frame back to lon/lat',
      rawHud.hidden === false && /经度 \d+\.\d+°[EW]/.test(rawHud.textContent) &&
      /纬度 \d+\.\d+°[NS]/.test(rawHud.textContent),
      rawHud.textContent);
    check('and the other tab already carries the same readout',
      procHud.textContent === rawHud.textContent, procHud.textContent);
  }
  /* Ctrl+click anchors: a red caption, a number, and one clear button. */
  {
    const before = T.state.manualMarks.length;
    T.addManualMark('disk', { clientX: 280, clientY: 280,
                              preventDefault() {} });
    check('Ctrl+click adds a manual anchor',
      T.state.manualMarks.length === before + 1 &&
      T.state.manualMarks[T.state.manualMarks.length - 1].text === '标记点1',
      JSON.stringify(T.state.manualMarks.slice(-1)));
    const anchor = T.state.manualMarks[T.state.manualMarks.length - 1];
    const bodyDisk = document.getElementById('body-disk');
    const pinned = T.state.manualLayout.disk
      .find((item) => item.mark.id === anchor.id);
    check('the anchor has a caption to press', !!pinned,
      JSON.stringify(T.state.manualLayout.disk));
    // Re-selecting via the caption used to pull the dot onto the pointer.  A
    // press and a one-pixel tremor must leave both the pin and the anchor
    // where they were.
    const map = T.imageToScreen('disk', 0, 0);
    const toClient = (fx, fy) => ({
      x: (map.x + fx * map.scaleX) / map.dpr,
      y: (map.y + fy * map.scaleY) / map.dpr,
    });
    const press = toClient(pinned.label_x + pinned.label_w - 3,
                           pinned.label_y + 5);
    bodyDisk.fire('pointerdown', {
      button: 0, pointerId: 7, clientX: press.x, clientY: press.y,
      preventDefault() {},
    });
    bodyDisk.fire('pointermove', {
      clientX: press.x + 1, clientY: press.y + 1, preventDefault() {},
    });
    bodyDisk.fire('pointerup', {});
    const afterSelect = T.state.manualLayout.disk
      .find((item) => item.mark.id === anchor.id);
    check('re-selecting a manual anchor does not move it',
      Math.abs(afterSelect.x - pinned.x) < 1
      && Math.abs(afterSelect.y - pinned.y) < 1,
      `${pinned.x},${pinned.y} -> ${afterSelect.x},${afterSelect.y}`);
    check('and the press selected that anchor',
      T.overlay.selected && T.overlay.selected.kind === 'manual'
      && T.overlay.selected.id === anchor.id,
      JSON.stringify(T.overlay.selected));
    check('the redraw kept the selected ring on the anchor',
      document.getElementById('labels-disk').getContext('2d').drawn.strokes
        .includes('rgba(255, 214, 102, .95)'));
    // A real drag moves the pin by the pointer's displacement, not to the
    // pointer: the caption's own offset is still the grab offset.
    bodyDisk.fire('pointerdown', {
      button: 0, pointerId: 8, clientX: press.x, clientY: press.y,
      preventDefault() {},
    });
    bodyDisk.fire('pointermove', {
      clientX: press.x + 20, clientY: press.y + 10, preventDefault() {},
    });
    bodyDisk.fire('pointerup', {});
    const afterDrag = T.state.manualLayout.disk
      .find((item) => item.mark.id === anchor.id);
    const wantedX = 20 * map.dpr / map.scaleX;
    const wantedY = 10 * map.dpr / map.scaleY;
    check('dragging it still moves it by the drag distance',
      Math.abs(afterDrag.x - pinned.x - wantedX) < 2
      && Math.abs(afterDrag.y - pinned.y - wantedY) < 2,
      `${pinned.x},${pinned.y} -> ${afterDrag.x},${afterDrag.y}`
      + ` (wanted +${wantedX},+${wantedY})`);
    document.getElementById('manual-clear').fire('click');
    check('清空手动标记 removes them all',
      T.state.manualMarks.length === 0 && T.state.manualSeq === 0,
      JSON.stringify(T.state.manualMarks));
  }
  /* The A/a switch and 针对选框标注 are latches, not form fields. */
  {
    const toggle = document.getElementById('label-satellite');
    check('the satellite switch starts on A', toggle.textContent === 'A' &&
      toggle.getAttribute('aria-pressed') === 'true', toggle.textContent);
    toggle.fire('click');
    check('and becomes a lower-case a', toggle.textContent === 'a' &&
      toggle.getAttribute('aria-pressed') === 'false', toggle.textContent);
    toggle.fire('click');
    const savedSelection = T.state.selection;
    T.state.selection = { lon: 0, lat: 0, radiusFraction: 0.1,
                          rotationDeg: 0, aspect: 1 };
    document.getElementById('label-focus').fire('click');
    check('针对选框标注 turns on for a box',
      document.getElementById('label-focus').getAttribute('aria-pressed') === 'true');
    await new Promise((resolve) => setTimeout(resolve, 80));
    const last = fetchCalls.filter((c) => c.url === '/api/annotations').pop();
    check('and the request carries the focus box',
      !!last && !!last.body.focus && last.body.focus.radius_deg > 0,
      JSON.stringify(last && last.body.focus));
    document.getElementById('label-focus').fire('click');
    T.state.selection = savedSelection;
  }
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
  check('the raw frame is not re-sent when nothing about it changed',
    processed.body.raw === false);
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
  T.setZoom('disk', 0.05);
  check('the full disk can zoom out to 10%',
    T.views.disk.zoom === 0.1, String(T.views.disk.zoom));
  T.setZoom('disk', 1);
  T.setZoom('raw', 0.05);
  check('the close-ups still stop at 100%',
    T.views.raw.zoom === 1, String(T.views.raw.zoom));
  T.setZoom('raw', 1);

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
  check('and with the lunar-calendar date the server computed',
    /农历 四月初九/.test(document.getElementById('phase-lunar').textContent),
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
    /* The ladder is 模拟设置's own table, not a shorter hand-written one: the
       reader picks between the same steps in both panes, including 同步全月面. */
    {
      const values = (id) => [...document.getElementById(id).options]
        .map((option) => option.value);
      const simulation = values('out-kmpx-preset');
      const animation = values('anim-kmpx-preset').filter((value) => value !== 'native');
      const restore = document.getElementById('anim-kmpx-preset').value;
      check('the animation menu carries the same ladder as 模拟设置',
        JSON.stringify(animation) === JSON.stringify(simulation),
        `${animation.join(',')} vs ${simulation.join(',')}`);
      const customs = values('anim-kmpx-preset').filter((v) => v === 'custom');
      check('with exactly one 自定义 row, however often the ladder is recopied',
        customs.length === 1, String(customs.length));
      // 同步全月面 is a real choice here too: the whole disk's own scale.  On a
      // disk frame that is the output size by definition, so the check is run
      // on a close-up, where 3 km/px is far coarser than the file's 1200 px.
      const scope = document.getElementById('anim-scope').value;
      set('anim-scope', 'region');
      fire('anim-scope', 'change');
      set('anim-kmpx-preset', 'disk');
      fire('anim-kmpx-preset', 'change');
      const synced = T.animationRenderSize();
      check('同步全月面 resolves to the disk frame\'s own scale',
        !!synced && synced.width < 300 && T.animationOutputPixels() === 1200,
        `${JSON.stringify(synced)} out=${T.animationOutputPixels()} ` +
        `sky=${T.animationSkyKm()} km`);
      set('anim-scope', scope);
      fire('anim-scope', 'change');
      set('anim-kmpx-preset', restore);
      fire('anim-kmpx-preset', 'change');
      check('and putting the menu back restores the coarse render',
        T.animationRenderSize().width < 1200, JSON.stringify(T.animationRenderSize()));
      check('and the animation no longer dictates the shadow budget',
        T.animationRequest().output.shadow_steps === undefined,
        JSON.stringify(T.animationRequest().output));
    }

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
    const limitRow = [...select.options]
      .find((option) => /上限/.test(option.textContent || ''));
    check('the limit row carries the measured floor, not the published 0.118',
      !!limitRow && limitRow.value === '0.1185', JSON.stringify(limitRow && limitRow.value));
    check('and names the selected product',
      !!limitRow && /LOLA/.test(limitRow.textContent),
      limitRow && limitRow.textContent);
    set('out-kmpx-preset', limitRow.value);
    fire('out-kmpx-preset', 'change');
    check('picking it leaves one field and keeps the menu on the row',
      document.getElementById('out-kmpx').hidden === true && select.value === '0.1185',
      `${document.getElementById('out-kmpx').hidden} / ${select.value}`);
    const fine = [...select.options].find((option) => option.value === '0.06');
    check('a row finer than the data is disabled rather than silently rewritten',
      !!fine && fine.disabled === true, JSON.stringify(fine && fine.disabled));
    const savedSources = T.LIMITS.data_sources;
    const savedPatch = T.LIMITS.active_patch;
    T.LIMITS.active_patch = null;
    T.LIMITS.data_sources = {
      sources: [{ id: 'sldem', name: 'SLDEM2015_512.tif' }], selected: 'sldem',
    };
    check('the limit label follows an SLDEM selection',
      T.terrainLimitLabel() === 'SLDEM 上限', T.terrainLimitLabel());
    T.LIMITS.data_sources = {
      sources: [{ id: 'lola', name: 'LDEM_118m.tif' }], selected: 'lola',
    };
    check('and says LOLA for a LOLA product',
      T.terrainLimitLabel() === 'LOLA 上限', T.terrainLimitLabel());
    T.LIMITS.data_sources = savedSources;
    T.LIMITS.active_patch = savedPatch;

    set('out-kmpx-preset', 'disk');
    fire('out-kmpx-preset', 'change');
    T.LIMITS.terrain_km_per_px = saved;
  }

  console.log('');
  console.log('区域高清补片：只下选框盖住的那一块');
  {
    const select = document.getElementById('patch-product');
    check('the patch menu lists the online products',
      [...select.options].some((o) => /SLDEM2015/.test(o.textContent)),
      [...select.options].map((o) => o.textContent).join(' | '));
    set('patch-product', 'sldem2015-512');
    const before = fetchCalls.filter((c) => c.url === '/api/patch').length;
    fire('patch-download', 'click');
    await new Promise((resolve) => setTimeout(resolve, 120));
    const call = fetchCalls.filter((c) => c.url === '/api/patch').pop();
    check('下载此区域 posts the selection, not the whole Moon',
      fetchCalls.filter((c) => c.url === '/api/patch').length === before + 1 &&
      call.body.product === 'sldem2015-512' &&
      !!call.body.selection && !!call.body.when,
      JSON.stringify({ product: call.body.product,
                       selection: call.body.selection && Object.keys(call.body.selection) }));
    await new Promise((resolve) => setTimeout(resolve, 2000));
    check('and the pane reports where the patch landed',
      /就绪|MB/.test(document.getElementById('patch-note').textContent),
      document.getElementById('patch-note').textContent);

    /* The regression this round fixes: the renderer had already switched to
       the patch, but 数据源 still named the old global product and 物理分辨率
       still had the 60 m row greyed out.  Both menus now follow the source the
       frame was actually drawn from. */
    const source = document.getElementById('data-source');
    const patchOption = source.options.find((option) => option.value === source.value);
    check('数据源 names the patch that the close-up is now made of',
      source.value === 'region:patch' &&
      /SLDEM2015/.test((patchOption && patchOption.textContent) || ''),
      `${source.value} / ${patchOption && patchOption.textContent}`);
    const fine = [...document.getElementById('out-kmpx-preset').options]
      .find((option) => option.value === '0.06');
    check('物理分辨率 opens the 60 m row the patch can honour',
      !!fine && fine.disabled === false, JSON.stringify(fine && fine.disabled));
    check('and the published floor follows the patch, not the old source',
      Math.abs(T.LIMITS.terrain_km_per_px - 0.0592) < 1e-9,
      T.LIMITS.terrain_km_per_px);
    check('the note says the frame is on a patch and names the fallback',
      /区域补片/.test(document.getElementById('data-source-note').textContent) &&
      /全局数据源/.test(document.getElementById('data-source-note').textContent),
      document.getElementById('data-source-note').textContent);

    /* The production path is a render, not the download job: /api/region says
       which terrain it sampled, and that is what the menus must follow. */
    T.syncTerrainSourceFromRegion({
      terrain_km_per_pixel: 0.0592, terrain_regional: true,
      terrain_source_name: 'sldem2015-512_e5p17_n18p68',
      terrain_bounds: [0, 11, 13.5, 23.8],
    });
    check('a patch render is also named in 数据源',
      source.value === 'region:sldem2015-512_e5p17_n18p68', source.value);
    T.syncTerrainSourceFromRegion({
      terrain_km_per_pixel: 0.11845058759433393, terrain_regional: false,
      terrain_source_name: 'LDEM_118m.tif',
    });
    check('a global render clears the patch group again',
      source.value === '/kernels/LDEM_118m.tif', source.value);

    T.clearActivePatch();
    check('leaving the patch restores the global source and its floor',
      source.value === '/kernels/LDEM_118m.tif' &&
      Math.abs(T.LIMITS.terrain_km_per_px - 0.11845058759433393) < 1e-9,
      `${source.value} / ${T.LIMITS.terrain_km_per_px}`);
  }

  console.log('');
  console.log('数据源：选了更细的产品，物理分辨率才放开更细的档位');
  {
    const select = document.getElementById('data-source');
    const options = [...select.options];
    const fine = () => [...document.getElementById('out-kmpx-preset').options]
      .find((option) => option.value === '0.06');
    check('the menu lists every local product',
      options.some((o) => /ldem_512/.test(o.textContent)) &&
      options.some((o) => /LDEM_118m/.test(o.textContent)),
      options.map((o) => o.textContent).join(' | '));
    check('and names the one the frames are made of',
      select.value === '/kernels/LDEM_118m.tif', select.value);
    check('the online product is listed and cannot be picked',
      options.some((o) => /SLDEM2015/.test(o.textContent) && o.disabled === true),
      options.filter((o) => /SLDEM2015/.test(o.textContent))
        .map((o) => `${o.textContent}:${o.disabled}`).join(' | '));
    check('at 118 m the 60 m row is greyed out',
      !!fine() && fine().disabled === true, !!(fine() && fine().disabled));

    set('data-source', '/kernels/ldem_512.tif');
    fire('data-source', 'change');
    await new Promise((resolve) => setTimeout(resolve, 80));
    const call = fetchCalls.filter((c) => c.url === '/api/source').pop();
    check('picking one asks the server to switch',
      !!call && call.body.id === '/kernels/ldem_512.tif', JSON.stringify(call && call.body));
    check('the floor follows the new product',
      Math.abs(T.LIMITS.terrain_km_per_px - 0.0592252938) < 1e-9,
      T.LIMITS.terrain_km_per_px);
    check('and the 60 m row lights up', fine().disabled === false, fine().disabled);
    check('the note says what is now in use, and what the disk still uses',
      /59\.2 m\/px/.test(document.getElementById('data-source-note').textContent) &&
      /整月面仍用 LDEM_118m\.tif/.test(document.getElementById('data-source-note').textContent),
      document.getElementById('data-source-note').textContent);

    set('data-source', '/kernels/LDEM_118m.tif');
    fire('data-source', 'change');
    await new Promise((resolve) => setTimeout(resolve, 80));
    check('switching back greys the finer rows again',
      fine().disabled === true && Math.abs(T.LIMITS.terrain_km_per_px - 0.11845058759433393) < 1e-9,
      `${fine().disabled} / ${T.LIMITS.terrain_km_per_px}`);
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
    T.overlay.selected = { kind: 'label', view: 'disk', key: 'Clavius' };
    check('a selected crater asks the plan for its family',
      T.relatedNameFor('disk') === 'Clavius', String(T.relatedNameFor('disk')));
    T.overlay.selected = null;
    check('and an empty selection asks for none',
      T.relatedNameFor('disk') === null);
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
    check('高分辨率模拟 saves the geometry render',
      String(document.getElementById('download').href).indexOf('cmF3') >= 0,
      String(document.getElementById('download').href));
    T.setResultTab('proc');
    check('后期处理 saves the processed one',
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
                    'out-px', 'out-ref', 'out-kmpx', 'out-kmpx-preset', 'surface-texture', 'terrain-denoise',
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

    // A saved close-up is the render at another pixel count.  The plan on hand
    // may still belong to the previous frame's resolution; fitting it to the
    // picture before painting is what keeps every name on its own ground.
    const stale = {
      frame: { width: 400, height: 400 },
      font_px: 10,
      items: [{ x: 100, y: 80, radius_px: 4, label_x: 108, label_y: 72,
                label_w: 32, label_h: 12, label: '阿贝', labelled: true,
                outline: [[100, 80], [140, 80]] }],
    };
    const fitted = T.fitPlanToFrame(stale, 800, 800);
    check('a plan from another resolution is fitted to the saved picture',
      fitted.frame.width === 800 && fitted.frame.height === 800 &&
      fitted.font_px === 20 && fitted.items[0].x === 200 &&
      fitted.items[0].label_x === 216 && fitted.items[0].label_w === 64 &&
      fitted.items[0].outline[1][0] === 280,
      JSON.stringify(fitted.items[0]));
    const oblong = T.scalePlanAxes(stale, 2, 4);
    check('a non-square export scales each axis on its own',
      oblong.items[0].x === 200 && oblong.items[0].y === 320 &&
      oblong.items[0].label_w === 64 && oblong.items[0].label_h === 48 &&
      oblong.frame.height === 1600,
      JSON.stringify(oblong.items[0]));
    check('a plan already at the right size is left alone',
      T.fitPlanToFrame(stale, 400, 400) === stale);

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
    /* Five taps in quick succession and the page snows.  The count is a time
       window, so taps inside one run of the harness are "consecutive" by
       construction -- which is exactly the case that has to work. */
    const layers = () => document.body.children
      .filter((el) => String(el.className).split(/\s+/).includes('moonfall'));
    const before = layers().length;
    for (let i = 0; i < 4; i += 1) fire('phase-name', 'click');
    check('four taps on the phase name are still just taps',
      layers().length === before, String(layers().length));
    fire('phase-name', 'click');
    check('the fifth brings the snow', layers().length === before + 1);
    const layer = layers().pop();
    check('with a whole sky of flakes', layer && layer.children.length >= 100,
      layer ? String(layer.children.length) : 'no layer');
    check('and a character on each one',
      layer && layer.children.every((f) => String(f.textContent).length > 0));
    /* The counter resets, so the joke can be told twice. */
    for (let i = 0; i < 4; i += 1) fire('phase-name', 'click');
    check('and the count starts over afterwards',
      layers().length === before + 1, String(layers().length));
    fire('phase-name', 'click');
    check('so it can snow again', layers().length === before + 2);
  }

  console.log('');
  console.log('the sky position window');
  {
    const canvas = document.getElementById('sky-canvas');
    const ctx = canvas.getContext('2d');
    ctx.drawn.texts.length = 0;
    fire('sky-open', 'click');
    await tick();
    await tick();
    const calls = fetchCalls.filter((call) => call.url === '/api/sky');
    check('the button asks for the tracks', calls.length === 1,
      String(calls.length));
    check('the request is centred on the page instant',
      !!calls[0] && calls[0].body.when.observer.lat === Number(document.getElementById('lat').value)
      && calls[0].body.span_hours === 6,
      calls[0] ? JSON.stringify(calls[0].body) : 'no call');
    check('the samples land in the state',
      !!T.state.sky && T.state.sky.samples.length === 49);
    /* 这张图有自己的时刻：打开时从主界面取，之后由它自己的日期/时间/时区与
       ± 一排步长驱动。 */
    {
      const mainWhen = document.getElementById('when').value;
      check('the window starts on the page instant',
        document.getElementById('sky-date').value === mainWhen.slice(0, 10) &&
        document.getElementById('sky-time').value === mainWhen.slice(11, 16) &&
        Number(document.getElementById('sky-tz').value)
          === Number(document.getElementById('tz').value),
        `${document.getElementById('sky-date').value} ` +
        `${document.getElementById('sky-time').value} ` +
        `${document.getElementById('sky-tz').value}`);

      const before = fetchCalls.filter((call) => call.url === '/api/sky').length;
      set('sky-time', '21:45');
      fire('sky-time', 'change');
      await tick();
      const moved = fetchCalls.filter((call) => call.url === '/api/sky').pop();
      check('changing its time asks the server for that moment',
        fetchCalls.filter((call) => call.url === '/api/sky').length === before + 1 &&
        /T21:45/.test(moved.body.when.at) === false &&
        Math.abs(Date.parse(moved.body.when.at + 'Z')
                 - Date.parse(`${document.getElementById('sky-date').value}T21:45:00Z`)
                 + Number(document.getElementById('sky-tz').value) * 3600000) < 1000,
        moved.body.when.at);

      const nudges = fetchCalls.filter((call) => call.url === '/api/sky').length;
      const stepButton = document.getElementById('sky-nudge-fwd-hour');
      check('the row of ± buttons exists', !!stepButton);
      fire('sky-nudge-fwd-hour', 'click');
      await tick();
      check('and +1h moves the window an hour, not the page',
        fetchCalls.filter((call) => call.url === '/api/sky').length === nudges + 1 &&
        document.getElementById('sky-time').value === '22:45' &&
        document.getElementById('when').value === mainWhen,
        `${document.getElementById('sky-time').value} / ` +
        `${document.getElementById('when').value}`);

      fire('sky-now', 'click');
      await tick();
      check('此刻 puts it back on the page instant',
        document.getElementById('sky-time').value === mainWhen.slice(11, 16),
        document.getElementById('sky-time').value);
      set('sky-time', mainWhen.slice(11, 16));
    }
    check('the floating window opens', document.getElementById('sky-backdrop').hidden === false);
    check('the close-up is off until the reader asks for it',
      document.getElementById('sky-closeup').checked === false
      && document.getElementById('sky-mode').textContent === '地平线视图',
      `${document.getElementById('sky-closeup').checked} / `
      + document.getElementById('sky-mode').textContent);
    check('the view starts locked',
      document.getElementById('sky-lock-view').getAttribute('aria-pressed') === 'true',
      String(document.getElementById('sky-lock-view').getAttribute('aria-pressed'))
      + ' / ' + String(T.state.skyViewLocked));
    set('sky-closeup', true);
    fire('sky-closeup', 'change');
    check('turning it on gives the close-up chart',
      document.getElementById('sky-mode').textContent === '轨迹特写',
      document.getElementById('sky-mode').textContent);
    const view = T.state.skyView;
    const layout = T.state.skyLayout;
    check('the chart keeps the same scale in both axes',
      !!view && !!layout
      && Math.abs((view.azMax - view.azMin) / (view.elMax - view.elMin)
                   - layout.w / layout.h) < 1e-6,
      view ? JSON.stringify({ view, layout }) : 'no view');
    check('the plot edges are hit targets',
      T.skyEdgeAt(layout.x + 2, layout.y + layout.h / 2, layout) === 'left'
      && T.skyEdgeAt(layout.x + layout.w - 2, layout.y + layout.h / 2, layout) === 'right'
      && T.skyEdgeAt(layout.x + layout.w / 2, layout.y + 2, layout) === 'top');
    check('time labels are on the canvas',
      ctx.drawn.texts.some((text) => /^\d\d:\d\d$/.test(text.text)));

    // Labels are a switch, and the switch has to reach the paint.
    ctx.drawn.texts.length = 0;
    set('sky-labels', false);
    fire('sky-labels', 'change');
    check('turning labels off removes the clock text',
      !ctx.drawn.texts.some((text) => /^\d\d:\d\d$/.test(text.text)));
    set('sky-labels', true);
    fire('sky-labels', 'change');

    // So is the elongation line.
    ctx.drawn.texts.length = 0;
    set('sky-elongation', false);
    fire('sky-elongation', 'change');
    check('turning the elongation line off removes its label',
      !ctx.drawn.texts.some((text) => text.text.includes('距角')));
    set('sky-elongation', true);
    fire('sky-elongation', 'change');

    // 锁定视角 is on by default: a nudge may move the Moon, not the frame.
    const lockedView = JSON.stringify(T.state.skyView);
    fire('sky-nudge-fwd-hour', 'click');
    await tick(); await tick();
    check('a locked view survives a time step',
      JSON.stringify(T.state.skyView) === lockedView,
      `${lockedView} -> ${JSON.stringify(T.state.skyView)}`);
    fire('sky-lock-view', 'click');
    check('the view lock can be released',
      document.getElementById('sky-lock-view').getAttribute('aria-pressed') === 'false'
      && T.state.skyView !== null);
    set('sky-time', document.getElementById('when').value.slice(11, 16));
    fire('sky-time', 'change');
    await tick();

    // The close-up is a checked choice, not a command: switching it off brings
    // the horizon back for the same samples.
    set('sky-closeup', false);
    fire('sky-closeup', 'change');
    check('switching the close-up off restores the horizon view',
      document.getElementById('sky-mode').textContent === '地平线视图'
      && ctx.drawn.texts.some((text) => text.text === '地平线'),
      document.getElementById('sky-mode').textContent);

    // 同步到主界面: both the wall clock and the offset have to move.
    const savedMain = document.getElementById('when').value;
    const savedTz = document.getElementById('tz').value;
    set('sky-time', '21:45');
    fire('sky-sync-main', 'click');
    check('同步到主界面 copies the sky instant and zone',
      document.getElementById('when').value
        === document.getElementById('sky-date').value + 'T21:45'
      && Number(document.getElementById('tz').value)
        === Number(document.getElementById('sky-tz').value),
      `${document.getElementById('when').value} / ${document.getElementById('tz').value}`);
    document.getElementById('when').value = savedMain;
    document.getElementById('tz').value = savedTz;

    T.saveSkyPng();
    check('the whole window can be saved as a PNG',
      document.getElementById('sky-summary').textContent.includes('已保存'),
      document.getElementById('sky-summary').textContent);

    fire('sky-close', 'click');
    check('the close button hides the window',
      document.getElementById('sky-backdrop').hidden === true);
  }


  console.log('');
  console.log('the curve window');
  {
    const ctx = document.getElementById('curve-canvas').getContext('2d');
    ctx.drawn.texts.length = 0;
    fire('curve-open', 'click');
    await tick();
    await tick();
    const calls = fetchCalls.filter((call) => call.url === '/api/curve');
    check('the curve button asks for the Moon figure-eight',
      calls.length === 1 && calls[0].body.kind === 'moon8' &&
      calls[0].body.drift_minutes === 50.47,
      calls[0] ? JSON.stringify(calls[0].body) : 'no call');
    check('the curve window opens on the page date',
      document.getElementById('curve-backdrop').hidden === false
      && document.getElementById('curve-date').value ===
        document.getElementById('when').value.slice(0, 10),
      `${document.getElementById('curve-backdrop').hidden} / `
      + document.getElementById('curve-date').value);
    check('the samples land in the state',
      !!T.state.curve && T.state.curve.samples.length === 30);
    check('both axes are labelled',
      ctx.drawn.texts.some((text) => text.text.includes('方位角'))
      && ctx.drawn.texts.some((text) => text.text.includes('高度角')));
    check('the summary names the span and the lunar-day drift',
      /30 点/.test(document.getElementById('curve-summary').textContent) &&
      /每天推迟 50\.5 分钟/.test(document.getElementById('curve-summary').textContent),
      document.getElementById('curve-summary').textContent);

    // 选择本月 / 选择本年: the page's simulated date defines the range.
    fire('curve-this-month', 'click');
    await tick(); await tick();
    const monthBody = fetchCalls.filter((call) => call.url === '/api/curve').pop().body;
    check('选择本月 sets the first day and the month length',
      document.getElementById('curve-date').value === '2026-05-01' &&
      Number(monthBody.days) === 31,
      `${document.getElementById('curve-date').value} / ${monthBody.days}`);
    fire('curve-this-year', 'click');
    await tick(); await tick();
    const yearBody = fetchCalls.filter((call) => call.url === '/api/curve').pop().body;
    check('选择本年 sets January 1 and the year length',
      document.getElementById('curve-date').value === '2026-01-01' &&
      Number(yearBody.days) === 365,
      `${document.getElementById('curve-date').value} / ${yearBody.days}`);

    set('curve-kind', 'sun8');
    fire('curve-kind', 'change');
    await tick(); await tick();
    const sunCalls = fetchCalls.filter((call) => call.url === '/api/curve');
    check('switching to the Sun asks for a solar figure-eight',
      sunCalls[sunCalls.length - 1].body.kind === 'sun8' &&
      sunCalls[sunCalls.length - 1].body.days === 365,
      JSON.stringify(sunCalls[sunCalls.length - 1].body));
    check('the solar chart is labelled as the Sun',
      document.getElementById('curve-mode').textContent === '太阳8字');
    ctx.drawn.texts.length = 0;
    T.drawCurve();
    check('the solar chart carries the sky axes',
      ctx.drawn.texts.some((text) => text.text.includes('方位角')) &&
      ctx.drawn.texts.some((text) => text.text.includes('高度角')));

    set('curve-show-twilight', true);
    fire('curve-show-twilight', 'change');
    check('天光 is available on a horizon chart',
      T.curveOptions().twilight === true);
    set('curve-kind', 'earthshine');
    fire('curve-kind', 'change');
    fire('curve-kind', 'change');
    await tick();
    await tick();
    check('地照亮度 has no 天光 even if the switch is still ticked',
      T.curveOptions().twilight === false &&
      document.getElementById('curve-show-twilight').checked === true);
    set('curve-show-twilight', false);
    const earthCalls = fetchCalls.filter((call) => call.url === '/api/curve');
    check('switching the type asks for the earthshine curve',
      earthCalls.length >= 1 &&
      earthCalls[earthCalls.length - 1].body.kind === 'earthshine',
      String(earthCalls.length));
    check('the earthshine controls replace the date controls',
      document.getElementById('curve-when').hidden === true &&
      document.getElementById('curve-earth').hidden === false);
    check('the axes are locked by default',
      document.getElementById('curve-lock-axes').checked === true);
    check('earthshine starts with brighter magnitudes at the top',
      T.curveOptions().reverseY === true);
    check('the phase axis cannot leave 0–180°',
      (() => {
        const v = T.curveClampEarthView(
          { xMin: -40, xMax: 240, yMin: -4, yMax: 12 },
          { kind: 'earthshine', xaxis: 'phase' });
        return v.xMin === 0 && v.xMax === 180;
      })());
    ctx.drawn.texts.length = 0;
    T.drawCurve();
    check('the earthshine chart labels both quantities',
      ctx.drawn.texts.some((text) => text.text.includes('相位角'))
      && ctx.drawn.texts.some((text) => text.text.includes('照明率'))
      && ctx.drawn.texts.some((text) => text.text.includes('地照亮度')));
    set('curve-xaxis', 'illum');
    fire('curve-xaxis', 'change');
    ctx.drawn.texts.length = 0;
    T.drawCurve();
    check('the x-axis can be illumination instead of phase angle',
      ctx.drawn.texts.some((text) => text.text.includes('照明率 (%)')));

    // Earth-albedo / phase-function presets for the earthshine curve.
    const modelSelect = document.getElementById('curve-earth-model');
    const modelValues = modelSelect.options.map((option) => option.value);
    check('the earthshine chart offers several Earth models',
      ['simulation', 'allen', 'vband', 'robinson', 'robinson_hg']
        .every((key) => modelValues.includes(key)),
      modelValues.join(','));
    set('curve-earth-model', 'vband');
    fire('curve-earth-model', 'change');
    await tick(); await tick();
    const vbandBody = fetchCalls.filter((call) => call.url === '/api/curve').pop().body;
    check('choosing the V-band model sends it to the server',
      vbandBody.earthshine_model === 'vband'
      && T.state.curve.earth_model === 'vband'
      && /V 波段/.test(document.getElementById('curve-summary').textContent),
      JSON.stringify(vbandBody));
    set('curve-earth-model', 'robinson_hg');
    fire('curve-earth-model', 'change');
    await tick(); await tick();
    const hgBody = fetchCalls.filter((call) => call.url === '/api/curve').pop().body;
    check('choosing the Robinson HG model switches the phase function',
      hgBody.earthshine_model === 'robinson_hg'
      && T.state.curve.earth_phase_model === 'hg'
      && /HG/.test(document.getElementById('curve-summary').textContent));
    set('curve-earth-model', 'simulation');
    fire('curve-earth-model', 'change');
    await tick(); await tick();

    // The Moon's year button is gone; the Sun keeps it.
    set('curve-kind', 'moon8');
    fire('curve-kind', 'change');
    await tick(); await tick();
    check('the Moon has no 选择本年 button',
      document.getElementById('curve-this-year').hidden === true);
    set('curve-show-phase', true);
    fire('curve-show-phase', 'change');
    check('the Moon can draw each point as a phase icon',
      T.curveOptions().showPhase === true);

    // Time nudges: the row below the fields walks the start of the span.
    const beforeNudge = document.getElementById('curve-time').value;
    const nudgeButton = document.getElementById('curve-nudge-fwd-hour');
    check('the curve has +1h nudge buttons', !!nudgeButton);
    nudgeButton.click();
    await tick(); await tick();
    check('+1h moves the curve start by an hour',
      document.getElementById('curve-time').value !== beforeNudge,
      `${beforeNudge} -> ${document.getElementById('curve-time').value}`);

    // Dragging a movable chart label stores its position.
    T.drawCurve();
    const badgeBox = T.state.curveTextBoxes.find((box) => box.key === 'badge');
    check('the chart badge is a draggable label', !!badgeBox);
    document.getElementById('curve-canvas').fire('pointerdown', {
      clientX: badgeBox.x + 2, clientY: badgeBox.y + 2,
      preventDefault() {}, stopPropagation() {},
    });
    document.dispatchEvent({ type: 'pointermove',
      clientX: badgeBox.x + 70, clientY: badgeBox.y + 28 });
    document.dispatchEvent({ type: 'pointerup' });
    check('dragging a label stores its new position',
      !!T.state.curveText.badge,
      JSON.stringify(T.state.curveText));

    // Double-clicking an axis title asks for a new one.
    T.drawCurve();
    let promptDefault = '';
    sandbox.window.prompt = (_label, current) => {
      promptDefault = current;
      return '自定义 X 轴';
    };
    const xTitle = T.state.curveTitleBoxes.find((box) => box.key === 'x');
    document.getElementById('curve-canvas').fire('dblclick', {
      clientX: xTitle && xTitle.x + 2, clientY: xTitle && xTitle.y + 2,
    });
    check('double-clicking the X axis title offers its default wording',
      /方位角/.test(promptDefault), promptDefault);
    check('double-clicking the X axis title edits it',
      T.state.curveTitles.x === '自定义 X 轴', String(T.state.curveTitles.x));
    sandbox.window.prompt = (_label, current) => {
      promptDefault = current;
      return '自定义主标题';
    };
    fire('curve-heading', 'dblclick', { preventDefault() {} });
    check('the main-title prompt starts from the default chart title',
      /月亮8字/.test(promptDefault), promptDefault);
    check('double-clicking the main title edits it',
      document.getElementById('curve-heading').textContent === '自定义主标题',
      document.getElementById('curve-heading').textContent);
    sandbox.window.prompt = undefined;

    // Earthshine: 显示当前 and the movable 当前 label.
    set('curve-kind', 'earthshine');
    fire('curve-kind', 'change');
    await tick(); await tick();
    const currentCtx = document.getElementById('curve-canvas').getContext('2d');
    currentCtx.drawn.texts.length = 0;
    set('curve-show-current', false);
    fire('curve-show-current', 'change');
    check('显示当前 can hide the current marker',
      !currentCtx.drawn.texts.some((text) => text.text === '当前'));
    set('curve-show-current', true);
    fire('curve-show-current', 'change');

    // 彩色 merged into the point style's 自动上色, and the default point is white.
    set('curve-kind', 'sun8');
    fire('curve-kind', 'change');
    await tick(); await tick();
    check('the default sun point colour is white',
      T.curveStyleFor('sun8', 'track').pointColor === '#ffffff');
    check('自动上色 is the only colour switch left',
      !!document.getElementById('curve-series-point-auto'));

    // Earthshine: the brightness-ratio overlay is an optional second line.
    set('curve-kind', 'earthshine');
    fire('curve-kind', 'change');
    await tick(); await tick();
    set('curve-show-ratio', true);
    fire('curve-show-ratio', 'change');
    const ratioSpecs = T.curveSeriesSpecs(T.state.curve, T.curveOptions());
    check('the earthshine chart can add the brightness-ratio line',
      ratioSpecs.length === 2 && ratioSpecs[1].key === 'ratio' &&
      ratioSpecs[1].points.some((point) => Number.isFinite(point.y)));
    const ratioCtx = document.getElementById('curve-canvas').getContext('2d');
    ratioCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('the ratio overlay carries its own axis and title',
      ratioCtx.drawn.texts.some((text) => text.text.includes('月面 / 地照亮度比')));
    set('curve-series', 'ratio');
    fire('curve-series', 'change');
    set('curve-series-color', '#ff8800');
    fire('curve-series-color', 'change');
    const ratioStyle = T.curveStyleFor('earthshine', 'ratio');
    check('the ratio line can be selected and styled without jumping back',
      document.getElementById('curve-series').value === 'ratio'
      && T.state.curveSeriesKey === 'ratio'
      && ratioStyle.lineColor === '#ff8800');
    check('the ratio label is an individual movable legend row',
      T.state.curveTextBoxes.some((box) => box.key === 'legend:ratio'));
    const ratioLabel = T.state.curveTextBoxes.find(
      (box) => box.key === 'legend:ratio');
    document.getElementById('curve-canvas').fire('pointerdown', {
      clientX: ratioLabel.x + 2, clientY: ratioLabel.y + 2,
      preventDefault() {}, stopPropagation() {},
    });
    document.dispatchEvent({ type: 'pointermove',
      clientX: ratioLabel.x + 42, clientY: ratioLabel.y + 18 });
    document.dispatchEvent({ type: 'pointerup' });
    check('the ratio label can be moved on its own',
      !!T.state.curveText['legend:ratio'],
      JSON.stringify(T.state.curveText['legend:ratio']));
    sandbox.window.prompt = () => '自定义亮度比';
    const movedRatioLabel = T.state.curveTextBoxes.find(
      (box) => box.key === 'legend:ratio');
    document.getElementById('curve-canvas').fire('dblclick', {
      clientX: movedRatioLabel.x + 2, clientY: movedRatioLabel.y + 2,
    });
    check('double-clicking the ratio label edits its wording',
      T.state.curveLabels['legend:ratio'] === '自定义亮度比',
      String(T.state.curveLabels['legend:ratio']));
    const rightTitle = T.state.curveTitleBoxes.find((box) => box.key === 'right');
    sandbox.window.prompt = () => '自定义右轴';
    document.getElementById('curve-canvas').fire('dblclick', {
      clientX: rightTitle.x + 2, clientY: rightTitle.y + 2,
    });
    check('double-clicking the right axis title edits it',
      T.state.curveTitles.right === '自定义右轴',
      String(T.state.curveTitles.right));
    sandbox.window.prompt = undefined;

    // The range panel chooses which line's axis is being pinned.
    const rangeSelect = document.getElementById('curve-range-series');
    const rangeValues = rangeSelect.options.map((option) => option.value);
    check('the range panel offers every overlaid line',
      rangeValues.includes('earthshine') && rangeValues.includes('ratio'),
      rangeValues.join(','));
    set('curve-range-series', 'ratio');
    fire('curve-range-series', 'change');
    check('selecting the ratio line updates the range target',
      T.state.curveRangeSeriesKey === 'ratio');
    set('curve-xmin', 0);
    set('curve-xmax', 180);
    set('curve-ymin', 1);
    set('curve-ymax', 1e12);
    T.curveApplyLimits();
    check('the right ratio axis can be pinned independently',
      T.state.curveRatioRange
      && Math.abs(T.state.curveRatioRange.min) < 1e-9
      && Math.abs(T.state.curveRatioRange.max - 12) < 1e-9,
      JSON.stringify(T.state.curveRatioRange));
    set('curve-range-series', 'earthshine');
    fire('curve-range-series', 'change');
    check('the range target can switch back to the left axis',
      T.state.curveRangeSeriesKey === 'earthshine');
    T.curveResetView();
    check('复位视角 also releases the pinned ratio range',
      T.state.curveRatioRange === null);
    set('curve-show-ratio', false);
    fire('curve-show-ratio', 'change');

    // Per-line styling: hiding the line must leave the points alone.
    set('curve-kind', 'moon8');
    fire('curve-kind', 'change');
    await tick(); await tick();
    const trackStyle = T.curveStyleFor('moon8', 'track');
    set('curve-series-line', false);
    fire('curve-series-line', 'change');
    check('a line can be hidden while its data points stay',
      trackStyle.line === false && trackStyle.points === true);
    set('curve-series-line', true);
    set('curve-series-color', '#123456');
    set('curve-series-width', 3.2);
    set('curve-series-dash', 'dotted');
    set('curve-series-point-size', 5);
    set('curve-series-point-color', '#abcdef');
    set('curve-series-point-auto', false);
    set('curve-series-point-shape', 'diamond');
    set('curve-series-point-hollow', true);
    for (const id of ['curve-series-line', 'curve-series-color',
                      'curve-series-width', 'curve-series-dash',
                      'curve-series-point-size', 'curve-series-point-color',
                      'curve-series-point-auto', 'curve-series-point-shape',
                      'curve-series-point-hollow']) {
      fire(id, 'change');
    }
    check('each line keeps its own colour, width, dash and point format',
      trackStyle.lineColor === '#123456' && trackStyle.lineWidth === 3.2
      && trackStyle.dash === 'dotted' && trackStyle.pointSize === 5
      && trackStyle.pointColor === '#abcdef' && trackStyle.pointAuto === false
      && trackStyle.pointShape === 'diamond' && trackStyle.pointHollow === true,
      JSON.stringify(trackStyle));

    // New layout: secondary controls live in two toggled panels.
    check('the style and range panels start tucked away',
      document.getElementById('curve-style').hidden === true
      && document.getElementById('curve-limits').hidden === true);
    fire('curve-style-toggle', 'click');
    check('the style button opens its panel',
      document.getElementById('curve-style').hidden === false
      && document.getElementById('curve-style-toggle')
        .getAttribute('aria-pressed') === 'true');
    fire('curve-limits-toggle', 'click');
    check('opening the range panel closes the style panel',
      document.getElementById('curve-style').hidden === true
      && document.getElementById('curve-limits').hidden === false);
    fire('curve-limits-toggle', 'click');

    // One geometry chart, with the reader choosing both axes.
    set('curve-kind', 'geometry');
    fire('curve-kind', 'change');
    await tick(); await tick();
    const geoBody = fetchCalls.filter((call) => call.url === '/api/curve').pop().body;
    check('the geometry chart asks for lunar geometry samples',
      geoBody.kind === 'geometry' && geoBody.step_minutes === 120 &&
      T.state.curve.kind === 'geometry');
    check('the geometry chart offers 选择本年',
      document.getElementById('curve-this-year').hidden === false);
    set('curve-xaxis', 'phase');
    set('curve-yaxis', 'altitude');
    fire('curve-xaxis', 'change');
    fire('curve-yaxis', 'change');
    const geoCtx = document.getElementById('curve-canvas').getContext('2d');
    geoCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('the geometry chart labels the chosen axes',
      geoCtx.drawn.texts.some((text) => text.text.includes('高度角'))
      && geoCtx.drawn.texts.some((text) => text.text.includes('相位角')));
    const geoSettings = T.curveOptions();
    const geoSpecs = T.curveSeriesSpecs(T.state.curve, geoSettings);
    check('the geometry chart plots the chosen quantities',
      geoSpecs.length === 1 && geoSpecs[0].key === 'geometry'
      && geoSpecs[0].points.length > 0,
      JSON.stringify(geoSettings));
    set('curve-xaxis', 'time');
    fire('curve-xaxis', 'change');
    geoCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('the geometry chart can plot against time',
      geoCtx.drawn.texts.some((text) => text.text.includes('时间')));
    set('curve-yaxis', 'diameter');
    fire('curve-yaxis', 'change');
    geoCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('the apparent-diameter quantity can be the Y axis',
      geoCtx.drawn.texts.some((text) => text.text.includes('视直径')));
    set('curve-reverse-x', true);
    fire('curve-reverse-x', 'change');
    set('curve-reverse-y', true);
    fire('curve-reverse-y', 'change');
    check('both axes can be reversed',
      T.curveOptions().reverseX === true && T.curveOptions().reverseY === true);
    set('curve-reverse-x', false);
    fire('curve-reverse-x', 'change');
    set('curve-reverse-y', false);
    fire('curve-reverse-y', 'change');

    // Libration is now only the longitude-latitude phase plot.
    set('curve-kind', 'libration');
    fire('curve-kind', 'change');
    await tick(); await tick();
    check('the libration chart is the phase plot only',
      document.getElementById('curve-axis').hidden === true);
    const librationSpecs = T.curveSeriesSpecs(T.state.curve, T.curveOptions());
    check('the libration phase plot uses longitude and latitude as x and y',
      librationSpecs.length === 1 && librationSpecs[0].key === 'phase'
      && librationSpecs[0].points.every((point) => Number.isFinite(point.x)
        && Number.isFinite(point.y)));
    const librationCtx = document.getElementById('curve-canvas').getContext('2d');
    librationCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('the libration phase plot labels both axes',
      librationCtx.drawn.texts.some((text) => text.text.includes('天平动经度'))
      && librationCtx.drawn.texts.some((text) => text.text.includes('天平动纬度')));

    // Manual axis limits and a manually added text box.
    set('curve-kind', 'geometry');
    fire('curve-kind', 'change');
    await tick(); await tick();
    set('curve-xaxis', 'phase');
    set('curve-yaxis', 'altitude');
    fire('curve-xaxis', 'change');
    fire('curve-yaxis', 'change');
    T.drawCurve();
    set('curve-xmin', 20);
    set('curve-xmax', 120);
    set('curve-ymin', -5);
    set('curve-ymax', 5);
    T.curveApplyLimits();
    check('manual X/Y limits are applied to the view',
      T.state.curveView.xMin <= 20 && T.state.curveView.xMax >= 120
      && T.state.curveView.yMin === -5 && T.state.curveView.yMax === 5,
      JSON.stringify(T.state.curveView));
    T.curveResetView();

    sandbox.window.prompt = () => '测试文本框';
    T.curveAddTextBox();
    check('a text box can be added to the chart',
      T.state.curveCustomTexts.length === 1
      && T.state.curveCustomTexts[0].text === '测试文本框');
    sandbox.window.prompt = undefined;

    // Every drawn annotation has a close button at its top-right.
    T.drawCurve();
    const hideBadge = T.state.curveTextBoxes.find((box) => box.key === 'badge');
    check('a movable annotation has a close button',
      !!hideBadge && !!hideBadge.close);
    document.getElementById('curve-canvas').fire('pointerdown', {
      clientX: hideBadge.close.x + 2, clientY: hideBadge.close.y + 2,
      preventDefault() {}, stopPropagation() {},
    });
    check('the close button hides the annotation',
      T.state.curveHidden.badge === true);
    const hiddenCtx = document.getElementById('curve-canvas').getContext('2d');
    hiddenCtx.drawn.texts.length = 0;
    T.drawCurve();
    check('a hidden annotation is not drawn',
      !T.state.curveTextBoxes.some((box) => box.key === 'badge'));
    T.curveResetFormat();
    check('复位格式 restores hidden annotations',
      !T.state.curveHidden.badge);
    T.drawCurve();
    const badgeAgain = T.state.curveTextBoxes.find((box) => box.key === 'badge');
    document.getElementById('curve-canvas').fire('pointerdown', {
      clientX: badgeAgain.close.x + 2, clientY: badgeAgain.close.y + 2,
      preventDefault() {}, stopPropagation() {},
    });
    T.curveStyleFor('geometry', 'geometry').lineWidth = 6;
    T.state.curveText.header = { fx: 0.2, fy: 0.2 };
    T.curveResetFormat();
    check('复位格式 restores the default styles and every annotation',
      T.state.curveHidden.badge !== true
      && Object.keys(T.state.curveHidden).length === 0
      && T.state.curveCustomTexts.length === 1
      && T.state.curveCustomTexts[0].text === '测试文本框'
      && T.state.curveText.header === undefined
      && T.curveStyleFor('geometry', 'geometry').lineWidth === 1.4,
      JSON.stringify({ hidden: T.state.curveHidden, text: T.state.curveText }));

    // The main title is an in-chart label and can be dragged like any other.
    T.drawCurve();
    const headerBox = T.state.curveTextBoxes.find((box) => box.key === 'header');
    check('the main title is a draggable chart label', !!headerBox);
    document.getElementById('curve-canvas').fire('pointerdown', {
      clientX: headerBox.x + 2, clientY: headerBox.y + 2,
      preventDefault() {}, stopPropagation() {},
    });
    document.dispatchEvent({ type: 'pointermove',
      clientX: headerBox.x + 50, clientY: headerBox.y + 24 });
    document.dispatchEvent({ type: 'pointerup' });
    check('dragging the main title stores its position',
      !!T.state.curveText.header, JSON.stringify(T.state.curveText));

    set('curve-kind', 'moon8');
    fire('curve-kind', 'change');
    await tick(); await tick();
    const beforeSave = created.length;
    T.saveCurvePng();
    const exportCanvas = created.slice(beforeSave).reverse()
      .find((el) => String(el.tagName).toUpperCase() === 'CANVAS');
    check('the saved PNG keeps the on-screen chart aspect',
      !!exportCanvas && Math.abs(
        exportCanvas.width / exportCanvas.height
        - document.getElementById('curve-canvas').getBoundingClientRect().width
        / document.getElementById('curve-canvas').getBoundingClientRect().height)
      < 0.01,
      exportCanvas ? `${exportCanvas.width}x${exportCanvas.height}` : 'no canvas');
    const exportTexts = exportCanvas
      ? exportCanvas.getContext('2d').drawn.texts.map((text) => text.text) : [];
    check('the saved PNG carries the chart title and axes, not an export-only summary',
      exportTexts.filter((text) => text.includes('月亮8字')).length === 2
      && exportTexts.some((text) => text.includes('方位角'))
      && !exportTexts.some((text) => text.includes('个样本')),
      exportTexts.join(' | '));
    check('the curve window can be saved as a PNG',
      document.getElementById('curve-summary').textContent.includes('已保存'),
      document.getElementById('curve-summary').textContent);

    /* 白色 flips the chart paper and changes its own label to 黑色; the
       animation panel has icon controls and a speed ladder. */
    fire('curve-white-toggle', 'click');
    check('白色 flips the chart paper and offers 黑色 back',
      T.state.curveWhite === true
      && document.getElementById('curve-white-toggle').textContent === '黑色');
    fire('curve-white-toggle', 'click');
    check('clicking 黑色 goes back to white paper',
      T.state.curveWhite === false
      && document.getElementById('curve-white-toggle').textContent === '白色');
    fire('curve-white-toggle', 'click');
    fire('curve-animation-toggle', 'click');
    fire('curve-reset-all', 'click');
    check('全部复位 brings the paper, panels and view back together',
      T.state.curveWhite === false &&
      !T.state.curvePanels.animation &&
      document.getElementById('curve-animation').hidden === true);
    fire('curve-animation-toggle', 'click');
    check('the animation panel opens',
      document.getElementById('curve-animation').hidden === false);
    fire('curve-anim-play', 'click');
    check('显示动画 starts the reveal', T.state.curveAnimation.playing === true);
    fire('curve-anim-pause' in document ? 'curve-anim-pause' : 'curve-anim-play', 'click');
    check('播放/暂停 toggles it back', T.state.curveAnimation.playing === false);
    fire('curve-anim-rewind', 'click');
    check('退回开头 puts the cursor at zero',
      T.state.curveAnimation.cursor === 0 && T.state.curveAnimation.started === true);
    fire('curve-anim-slow', 'click');
    check('慢速 steps the speed down', T.state.curveAnimation.speed === 0.5);
    fire('curve-anim-fast', 'click');
    check('快速 steps it back to 1×', T.state.curveAnimation.speed === 1);
    check('时间步长 offers the requested ladder',
      ['t:1', 't:10', 't:15', 't:30', 't:60', 't:600', 't:900',
       't:1800', 't:3600', 't:86400']
        .every((value) => [...document.getElementById('curve-anim-step').options]
          .some((option) => option.value === value)));

    fire('curve-close', 'click');
    /* 实验功能: the little button under the phase strip opens a window with a
       chart and a table for one lunar surface point. */
    fire('experiment-open', 'click');
    await tick(); await tick();
    check('the experiment window opens from the phase strip',
      document.getElementById('experiment-backdrop').hidden === false);
    const expCall = fetchCalls.filter((call) => call.url === '/api/surface-sun').pop();
    check('it asks the core endpoint for the lunar point',
      !!expCall && expCall.body.moon_lon !== undefined
      && expCall.body.moon_lat !== undefined && !!expCall.body.start
      && !!expCall.body.end,
      expCall && JSON.stringify(expCall.body));
    check('the table is filled from the reply',
      document.getElementById('experiment-rows').children.length > 0,
      String(document.getElementById('experiment-rows').children.length));
    check('the chart has something to draw',
      !!T.state.experiment && T.state.experiment.samples.length > 0);
    {
      const expCtx = document.getElementById('experiment-canvas').getContext('2d');
      const texts = expCtx.drawn.texts.map((entry) => entry.text);
      check('the left chart shows its coordinate ticks and axis titles',
        texts.some((text) => text.includes('太阳高度')) &&
        texts.some((text) => /^\d{2}:\d{2}$/.test(text)) &&
        texts.some((text) => /°$/.test(text)),
        texts.slice(0, 8).join(' | '));
      check('the x ticks carry the date as well as the clock',
        texts.some((text) => /^\d{2}-\d{2}$/.test(text)),
        texts.filter((text) => /^\d{2}-\d{2}$/.test(text)).slice(0, 4).join(' | '));
      check('the horizon is the green line',
        expCtx.drawn.strokes.includes('#4ade80'),
        expCtx.drawn.strokes.slice(-8).join(' | '));
      const layout = T.state.experimentLayout;
      const beforeX = T.state.experimentView.xMin;
      fire('experiment-canvas', 'pointerdown', {
        button: 0, pointerId: 1, clientX: layout.x + 1,
        clientY: layout.y + layout.h / 2, preventDefault() {},
      });
      fire('experiment-canvas', 'pointermove', {
        clientX: layout.x + 40, clientY: layout.y + layout.h / 2,
        preventDefault() {},
      });
      fire('experiment-canvas', 'pointerup', {});
      check('dragging the left border changes the X range',
        T.state.experimentView.xMin > beforeX,
        `${beforeX} -> ${T.state.experimentView.xMin}`);
      fire('experiment-reset-view', 'click');
      check('坐标复位 puts the automatic X range back',
        Math.abs(T.state.experimentView.xMin) < 1e-9,
        String(T.state.experimentView.xMin));
      const beforePan = T.state.experimentView.xMin;
      fire('experiment-canvas', 'pointerdown', {
        button: 0, pointerId: 2, clientX: layout.x + layout.w / 2,
        clientY: layout.y + layout.h / 2, preventDefault() {},
      });
      fire('experiment-canvas', 'pointermove', {
        clientX: layout.x + layout.w / 2 + 40,
        clientY: layout.y + layout.h / 2,
        preventDefault() {},
      });
      fire('experiment-canvas', 'pointerup', {});
      check('dragging the chart pans it',
        T.state.experimentView.xMin < beforePan - 0.1,
        `${beforePan} -> ${T.state.experimentView.xMin}`);
    }
    check('坐标复位 is in the window header, not the range panel',
      (() => {
        const head = html.indexOf('id="experiment-head"');
        const end = html.indexOf('</header>', head);
        const reset = html.indexOf('id="experiment-reset-view"');
        return head >= 0 && end > head && reset > head && reset < end
          && !/experiment-range-reset/.test(html);
      })());
    check('同步选中时间到主界面 sits right of 取主界面时刻',
      html.indexOf('id="experiment-to-main"')
        > html.indexOf('id="experiment-from-main"'));
    {
      const body = document.getElementById('experiment-rows');
      const rowNode = body.children[0];
      const chosen = T.state.experiment.samples
        .find((row) => row.when_utc === rowNode.dataset.when);
      rowNode.fire('click');
      check('clicking a table row selects it',
        !!chosen && T.state.experimentSelected
        && T.state.experimentSelected.when_utc === chosen.when_utc
        && rowNode.classList.contains('selected'),
        JSON.stringify(T.state.experimentSelected));
      check('the chart marks the selected row',
        document.getElementById('experiment-canvas').getContext('2d')
          .drawn.strokes.includes('#ffd666')
        && document.getElementById('experiment-canvas').getContext('2d')
          .drawn.strokes.includes('#8fb8ff'));
      const offset = Number(document.getElementById('tz').value) || 0;
      const wanted = new Date(Date.parse(chosen.when_utc) + offset * 3600000)
        .toISOString().slice(0, 16);
      const auto = document.getElementById('auto-refresh');
      const wasAuto = auto.checked;
      auto.checked = false;
      fire('experiment-to-main', 'click');
      auto.checked = wasAuto;
      check('同步选中时间到主界面 writes that row\'s instant',
        document.getElementById('when').value === wanted,
        `${document.getElementById('when').value} / ${wanted}`);
    }
    check('its range opens with concrete start and end instants',
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(
        document.getElementById('experiment-start').value) &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(
        document.getElementById('experiment-end').value),
      `${document.getElementById('experiment-start').value} / `
      + document.getElementById('experiment-end').value);
    fire('experiment-step-toggle', 'click');
    check('时间步长 is a collapsible control',
      document.getElementById('experiment-step-wrap').hidden === false &&
      document.getElementById('experiment-step-toggle')
        .getAttribute('aria-expanded') === 'true');
    check('时间步长 opens a nudge ladder, not a step menu',
      (html.match(/data-range="experiment"/g) || []).length === 8 &&
      !/<select[^>]*id="experiment-step"/.test(html));
    fire('experiment-range-toggle', 'click');
    check('坐标 opens the axis-range fields',
      document.getElementById('experiment-range').hidden === false &&
      document.getElementById('experiment-xmin').value !== '' &&
      !!document.getElementById('experiment-apply-range') &&
      !!document.getElementById('experiment-reverse-x') &&
      !!document.getElementById('experiment-reverse-y'));
    T.setExperimentSort('altitude');
    check('表格 headers sort the rows and remember the direction',
      T.state.experimentSort.key === 'altitude' &&
      T.state.experimentSort.dir === 1,
      JSON.stringify(T.state.experimentSort));
    T.setExperimentSort('altitude');
    check('clicking the same header again reverses it',
      T.state.experimentSort.dir === -1,
      JSON.stringify(T.state.experimentSort));
    fire('experiment-close', 'click');
    check('and it closes', document.getElementById('experiment-backdrop').hidden === true);

    check('the curve close button hides the window',
      document.getElementById('curve-backdrop').hidden === true);
  }

  console.log('');
  console.log('a lunar eclipse appears without a switch');
  {
    const eclipse = {
      kind: 'total', active: true, label: '月全食',
      label_en: 'Total lunar eclipse', magnitude: 1.19,
      penumbral_magnitude: 2.24, separation_deg: 0.3,
      moon_radius_deg: 0.25, umbra_radius_deg: 0.66,
      penumbra_radius_deg: 1.18,
    };
    const geometry = {
      eclipse,
      report: {
        state: { phase: { label: '满月' }, libration: {}, orbit: {} },
        rise_set: {}, transit: {},
      },
      utc_offset_hours: 8,
    };
    T.fillMoonInfo(geometry);
    check('the 月球信息 column grows a 月食 row',
      document.getElementById('mi-eclipse-dt').hidden === false);
    check('and names the eclipse and its magnitude',
      /月全食.*食分 1\.19/.test(document.getElementById('mi-eclipse-dd').textContent),
      document.getElementById('mi-eclipse-dd').textContent);
    T.state.moonLang = 'en';
    T.fillMoonInfo(geometry);
    check('the row follows the readout language',
      /Total lunar eclipse.*mag 1\.19/
        .test(document.getElementById('mi-eclipse-dd').textContent),
      document.getElementById('mi-eclipse-dd').textContent);
    T.state.moonLang = 'zh';
    T.fillMoonInfo({ ...geometry, eclipse: { kind: 'none', active: false } });
    check('an ordinary instant hides the row again',
      document.getElementById('mi-eclipse-dt').hidden === true &&
      document.getElementById('mi-eclipse-dd').hidden === true);

    const ctx = document.getElementById('sky-canvas').getContext('2d');
    ctx.drawn.fills.length = 0;
    T.skyPaintEclipseMoon(ctx, 100, 100, 11, eclipse, { x: 120, y: 100 });
    check('the chart moon carries a copper umbra in totality',
      ctx.drawn.fills.includes('rgba(74, 22, 12, .42)'),
      ctx.drawn.fills.join(' | '));
    ctx.drawn.fills.length = 0;
    T.skyPaintEclipseMoon(ctx, 100, 100, 11,
      { ...eclipse, kind: 'partial' }, { x: 120, y: 100 });
    check('and a dark bite in a partial eclipse',
      ctx.drawn.fills.includes('rgba(18, 7, 5, .9)'),
      ctx.drawn.fills.join(' | '));
  }

  console.log('');
  console.log('the sky chart keeps its geometry honest');
  {
    const high = {
      samples: [{ moon_elevation_deg: 50 }, { moon_elevation_deg: 60 }],
      geometry: { elongation_deg: 120 },
      moon: { azimuth_deg: 100, elevation_deg: 60 },
      sun: { azimuth_deg: 300, elevation_deg: -20 },
    };
    check('a high Moon far from the Sun is the close-up case',
      T.skyAutoCloseup(high) === true);
    check('a low Moon is not',
      T.skyAutoCloseup({ ...high, moon: { azimuth_deg: 100, elevation_deg: 5 },
        samples: [{ moon_elevation_deg: 5 }, { moon_elevation_deg: 12 }] }) === false);

    const grown = { azMin: 0, azMax: 100, elMin: 0, elMax: 20 };
    T.skyGrowAspect(grown, 2);
    check('growing to an aspect ratio keeps both axes equal',
      Math.abs((grown.azMax - grown.azMin) / (grown.elMax - grown.elMin) - 2) < 1e-9,
      JSON.stringify(grown));
    const fitted = { azMin: 0, azMax: 100, elMin: 0, elMax: 20 };
    T.skyFitAspectFromAz(fitted, 2);
    check('fitting from an azimuth range does too',
      Math.abs((fitted.azMax - fitted.azMin) / (fitted.elMax - fitted.elMin) - 2) < 1e-9,
      JSON.stringify(fitted));

    const labelCtx = document.createElement('canvas').getContext('2d');
    labelCtx.font = '11px sans-serif';
    const items = [
      { x: 100, y: 100, text: '12:00' },
      { x: 104, y: 102, text: '12:10' },
      { x: 108, y: 98, text: '12:20' },
    ];
    const placed = T.skyPlaceLabels(labelCtx, items,
      { x: 0, y: 0, w: 200, h: 200 }, []);
    let overlaps = false;
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        const a = placed[i].box;
        const b = placed[j].box;
        const width = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
        const height = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
        if (width > 0 && height > 0) overlaps = true;
      }
    }
    check('labels that start on top of one another are pushed apart',
      placed.length === 3 && !overlaps, JSON.stringify(placed));
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
