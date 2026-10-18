/* Lunar simulator — front end.
 *
 * Two jobs:
 *   1. keep the full-disk preview in step with the observer inputs, and let the
 *      user drag a rotatable box on it;
 *   2. turn that box plus the instrument parameters into one render request.
 *
 * The box is stored in *selenographic* terms (centre lon/lat, angular radius,
 * rotation) rather than in canvas pixels, so it survives a change of preview
 * size or instant.  Converting between the two needs the disk's orthographic
 * projection, which is the only geometry this file knows about.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const DISK = $('disk');
const OVERLAY = $('overlay');
const STACK = $('disk-stack');
const RAW = $('raw');
const RESULT = $('result');
const MIN_ZOOM = 1, MAX_ZOOM = 8;

const state = {
  geometry: null,      // last /api/disk response geometry
  diskImage: null,     // the full-disk preview
  rawImage: null,      // geometry only, no post-processing
  resultImage: null,   // after post-processing
  selection: null,     // {lon, lat, radiusFraction, rotationDeg}
  lastRenderSelection: null,   // the box the last close-up render was made with
  dragging: null,      // in-progress selection edit
  panning: null,       // in-progress pan
  renderId: null,
  trainScale: 0,
  labelsOn: false,     // show named landforms on the pictures
  labels: { disk: null, region: null },   // last plans, in frame pixels
  diskWhen: null,      // the instant the *displayed* preview was rendered for
  downloads: { disk: null, raw: null, proc: null },  // what each target last made
  diskExport: null,    // last full-disk export render: {key, url, geometry}
  earthshine: null,    // earthshine block of the last close-up render
  features: null,      // the searchable landform index, fetched once
  moonLang: 'zh',      // the Moon readout's own language
  animationId: null,   // the background animation job being polled, if any
  animationRunning: false, // the job is still producing frames
  sky: null,           // last /api/sky payload
  skySeries: null,     // samples with azimuth unwrapped for drawing
  skyView: null,       // {azMin, azMax, elMin, elMax} or null for auto-fit
  skyLayout: null,     // plot rectangle of the last paint, in canvas pixels
  skyProjection: null, // az/el <-> canvas mapping of the last paint
  skySeq: 0,           // guards a stale /api/sky reply
  skyOpen: false,
  skyCloseupTouched: false, // the reader has overruled the automatic close-up
  skyWindow: null,     // {left, top, width, height} of the floating window
  skyDrag: null,       // in-progress window or plot drag
  skyHoverEdge: null,  // plot edge under the pointer, for the cursor and grips
};

/* One zoom state per view, in CSS transform units.  Zooming is a transform on
   the stack, so the canvases are not redrawn and the selection maths -- which
   measures the element -- keeps working. */
const views = {
  disk: { stage: null, body: null, canvas: null, zoom: 1, panX: 0, panY: 0 },
  raw:  { stage: null, body: null, canvas: null, zoom: 1, panX: 0, panY: 0 },
  proc: { stage: null, body: null, canvas: null, zoom: 1, panX: 0, panY: 0 },
};

/* ── landform classes ──────────────────────────────────────────────────────
 *
 * The categories are the translation table's own sheets -- 环形坑, 月溪, 月谷,
 * 月海 … -- with one size split the table does not make: the moonlib_s catalogue
 * calls a crater wider than 300 km a basin.  The keys must stay in step with
 * `moonlib_s.annotations.FEATURE_CLASSES`; /api/features reports the same list
 * with its counts, and `checkClasses()` below complains in the console if the
 * two ever drift apart.
 *
 * The colours are the restraint: nothing saturated, nothing that competes with
 * the grey of the surface, and every one of them light enough to read over the
 * dark maria as well as the bright highlands.  The marker keeps its dark rim,
 * so a label stays legible on whatever it lands on.
 */
const LAND_CLASSES = [
  /* The order is the library's FEATURE_CLASSES, and a test compares the two
     lists key by key: adding a family here without adding it there (or in a
     different place) fails the build rather than the reader's eyes. */
  { key: 'basin',        zh: '大型撞击盆地',           en: 'Impact basin',         color: '#e6b566' },
  { key: 'mare',         zh: '月海',                 en: 'Mare',                 color: '#86b3a8' },
  { key: 'crater',       zh: '环形坑',               en: 'Crater',               color: '#a8c6e8' },
  { key: 'lacus',        zh: '月湖',                 en: 'Lacus',                color: '#8fc6cc' },
  { key: 'sinus',        zh: '月湾',                 en: 'Sinus',                color: '#8fa8dc' },
  { key: 'palus',        zh: '月沼',                 en: 'Palus',                color: '#a9b47a' },
  { key: 'rima',         zh: '月溪',                 en: 'Rima',                 color: '#e09a9a' },
  { key: 'vallis',       zh: '月谷',                 en: 'Vallis',               color: '#c8a0d4' },
  { key: 'mons',         zh: '山脉',                 en: 'Mons',                 color: '#dcc98a' },
  { key: 'dome',         zh: '穹丘 (Domes)',         en: 'Dome',                 color: '#c9b98a' },
  { key: 'dorsum',       zh: '山脊',                 en: 'Dorsum',               color: '#bfa886' },
  { key: 'rupes',        zh: '峭壁',                 en: 'Rupes',                color: '#d4a179' },
  { key: 'catena',       zh: '坑链',                 en: 'Catena',               color: '#9aa8cc' },
  { key: 'promontorium', zh: '海角',                 en: 'Promontorium',         color: '#9ed4b4' },
  { key: 'statio',       zh: '着陆点',               en: 'Statio',               color: '#e9a86b' },
  { key: 'pit',          zh: 'Lunar pits',           en: 'Lunar pit',            color: '#9fd0c0' },
  { key: 'pyroclastic',  zh: '火成碎屑 (Pyroclastic)', en: 'Pyroclastic',        color: '#d9a0b4' },
  { key: 'imp',          zh: '不规则月海斑块 (IMP)',  en: 'Irregular mare patch', color: '#bcd0a0' },
  { key: 'impact',       zh: '撞击闪光 (NELIOTA)',    en: 'NELIOTA impact',       color: '#f0c0a0' },
  { key: 'albedo',       zh: '反射率特征点',          en: 'Albedo feature',       color: '#c9c9dc' },
  { key: 'other',        zh: '其他',                 en: 'Other',                color: '#b9bec9' },
];

/* Switched off to begin with: these families are either catalogues of their own
   (the atlas's domes, pits and pyroclastic areas), a list of *instants* rather
   than of places (the NELIOTA impact flashes), or -- in the case of 着陆点 -- a
   set of small sites that only mean something once you are looking at a
   landing region up close.  A first look at the Moon is not covered in them.
   `resetClasses()` reads this list, so "复位" and the first paint agree. */
const DEFAULT_OFF_CLASSES = ['basin', 'dome', 'pit', 'pyroclastic', 'imp', 'impact',
                             'statio', 'other'];

/* The classes in the order the switches show them: the ones that are on to
 * begin with first, the ones that start switched off after them.
 *
 * LAND_CLASSES follows the library's own order key by key (a test compares the
 * two lists), and that order is geological -- basins, maria, craters, then the
 * catalogue families.  What a reader wants at the front of the row is what they
 * will actually use, so the *display* order is derived here instead of by
 * reordering the library. */
function orderedClasses() {
  return LAND_CLASSES
    .map((entry, index) => ({ entry, index }))
    .sort((left, right) => (
      (DEFAULT_OFF_CLASSES.includes(left.entry.key) ? 1 : 0)
      - (DEFAULT_OFF_CLASSES.includes(right.entry.key) ? 1 : 0)
    ) || left.index - right.index)
    .map((item) => item.entry);
}

const CLASS_COLORS = Object.fromEntries(LAND_CLASSES.map((c) => [c.key, c.color]));
const CLASS_LABELS = Object.fromEntries(LAND_CLASSES.map((c) => [c.key, c.zh]));


/* The Moon's overlay is deliberately NOT inside the transformed element.  If it
   were, zooming would magnify the selection's lines and handle along with the
   picture; keeping it outside means the rectangle stays one pixel wide and the
   handle a constant size however far you zoom in, and the drawing code just
   maps image coordinates through the same transform the canvas uses. */
function diskTransform() {
  const v = views.disk;
  return { zoom: v.zoom, panX: v.panX, panY: v.panY };
}

/* Where the disk canvas actually sits inside the view body.  The stage is a
   fitted, centred box within the body, and the canvas is zoomed about that
   box's centre -- so mapping an image point to the screen has to go through the
   stage's offset first. */
function stageFrame() {
  const body = $('body-disk').getBoundingClientRect();
  const stage = STACK.getBoundingClientRect();
  return {
    bodyW: Math.max(1, body.width),
    bodyH: Math.max(1, body.height),
    stageW: Math.max(1, stage.width),
    stageH: Math.max(1, stage.height),
    offsetX: stage.left - body.left,
    offsetY: stage.top - body.top,
  };
}

/** Image pixels (on the preview) -> overlay device pixels. */
function imageToOverlay(ix, iy) {
  const f = stageFrame();
  const { zoom, panX, panY } = diskTransform();
  const bx = ix * f.stageW / Math.max(1, DISK.width);
  const by = iy * f.stageH / Math.max(1, DISK.height);
  const cssX = (bx - f.stageW / 2) * zoom + f.stageW / 2 + panX + f.offsetX;
  const cssY = (by - f.stageH / 2) * zoom + f.stageH / 2 + panY + f.offsetY;
  const scale = OVERLAY.width / f.bodyW;
  return { x: cssX * scale, y: cssY * scale, scale };
}

/* ── helpers ─────────────────────────────────────────────────────── */

function num(id) {
  const raw = $(id).value.trim();
  return raw === '' ? null : Number(raw);
}
function fmt(x, digits = 2) {
  return (x === null || x === undefined || Number.isNaN(x)) ? '—' : x.toFixed(digits);
}

/** An arcsecond value, with the same angle in arcminutes in brackets.
 *
 * The Moon's apparent size is the one number an observer compares between
 * perigee and apogee, and it is quoted in arcminutes in every almanac -- while
 * the difference that matters is in the arcseconds.  Both, then: "1841.5″
 * （30.69′）". */
function arcsecText(arcsec, digits = 1) {
  if (arcsec === null || arcsec === undefined || Number.isNaN(arcsec)) return '—';
  return `${arcsec.toFixed(digits)}″（${(arcsec / 60).toFixed(2)}′）`;
}
function setStatus(text, kind) {
  const el = $('status');
  el.textContent = text;
  // The class list is rebuilt rather than appended to: writing className
  // outright used to drop the ones the markup gave it, and the line floated out
  // of place after the first message.  `status-line` is the layout, `status`
  // and the kind are the colour.
  el.className = 'hint status-line status' + (kind ? ' ' + kind : '');
}
function busy(id, on) { $(id).classList.toggle('on', on); }

async function post(path, body, signal) {
  const response = await fetch(path, {
    signal,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || response.statusText);
  return payload;
}

/* ── request assembly ────────────────────────────────────────────── */

function whenPayload() {
  const local = $('when').value;               // "YYYY-MM-DDTHH:MM"
  const offset = Number($('tz').value);
  let at = null;
  if (local) {
    // Interpret the typed time as local time at the given offset, and send UTC.
    const asUtc = Date.parse(local + ':00Z');
    at = new Date(asUtc - offset * 3600 * 1000).toISOString().slice(0, 19);
  }
  return {
    at,
    tz_offset_hours: offset,
    observer: { lat: Number($('lat').value), lon: Number($('lon').value) },
  };
}

function trainPayload() {
  return {
    telescope: {
      aperture_mm: num('aperture'),
      focal_ratio: num('focal-ratio'),
      focal_length_mm: num('focal-length'),
      barlow: num('barlow'),
      back_focus: num('back-focus'),
      obstruction: num('obstruction'),
    },
    camera: {
      width_px: num('sensor-w'),
      height_px: num('sensor-h'),
      pixel_um: num('pixel-um'),
      color: $('sensor-color').value === 'color',
    },
  };
}

function atmospherePayload() {
  // 叠加优化 used to ride along here as ``frame_selection_gain``: the site's
  // seeing divided by a "how many of the best frames went into this" factor.
  // The control is gone -- two knobs for one width, and the one the reader sets
  // says 视宁度 -- so the frame now carries the seeing it was asked for.  The
  // model still has the parameter, and this is it at its neutral value.
  return { seeing: num('seeing') };
}

/* ── orientation and noise ─────────────────────────────────────────────────
 *
 * Both are properties of the picture rather than of the geometry, so they go to
 * /api/process with the rest of the processing options.  Orientation is the
 * camera's: a star diagonal mirrors, a rotator rolls, and the page shows the
 * same frame in both panes, so the server applies it to the raw one too.
 *
 * The two noise sliders are positions on a perceptual axis rather than
 * parameters, because "1 000 000 electrons of full well" is not something a
 * reader can picture:
 *
 *   poisson  t = 0 is off; otherwise full well = 10^(6.6 - 5.2t) electrons,
 *            which is a relative fluctuation of 0.05% rising to 20%.
 *   gaussian t = 0 is off; otherwise read noise = 6% of full scale at most.
 */
const NOISE_POISSON_RANGE = [6.6, 5.2];   // log10 electrons at t=0 and t=1
const NOISE_GAUSSIAN_MAX = 0.06;          // sigma at t=1, in signal units

function poissonPhotons() {
  const t = Number($('noise-poisson').value) || 0;
  if (t <= 0) return null;
  return Math.round(Math.pow(10, NOISE_POISSON_RANGE[0] - NOISE_POISSON_RANGE[1] * t));
}

function gaussianSigma() {
  const t = Number($('noise-gaussian').value) || 0;
  return t <= 0 ? 0 : t * NOISE_GAUSSIAN_MAX;
}

/** Which mirror is on, from the two icon buttons.  Mutually exclusive: an
 *  image is flipped left-right or up-down, not both at once. */
function mirrorMode() {
  const h = $('mirror-h');
  const v = $('mirror-v');
  if (h && h.getAttribute('aria-pressed') === 'true') return 'horizontal';
  if (v && v.getAttribute('aria-pressed') === 'true') return 'vertical';
  return 'none';
}

function setMirrorMode(mode) {
  const wanted = mode === 'horizontal' || mode === 'vertical' ? mode : 'none';
  for (const [id, value] of [['mirror-h', 'horizontal'], ['mirror-v', 'vertical']]) {
    const button = $(id);
    if (button) button.setAttribute('aria-pressed', String(wanted === value));
  }
}

function orientation() {
  return {
    rotation_deg: num('image-rotation') || 0,
    mirror: mirrorMode(),
  };
}

function processingPayload() {
  const orient = orientation();
  return {
    blur_strength: num('blur-strength'),
    // 衍射环: how much of the telescope's ring pattern to convolve in.
    diffraction: num('diffraction') || 0,
    // 锐化半径 is stated in the frame's own pixels; the server turns it into
    // arcseconds with the plate scale it already reports for this render.
    sharpen_radius_px: num('sharpen-radius'),
    sharpen_strength: num('sharpen-strength'),
    saturation: num('saturation'),
    gain: num('gain'),
    offset: num('offset'),
    gamma: num('gamma'),
    rotation_deg: orient.rotation_deg,
    mirror: orient.mirror,
    noise_photons: poissonPhotons(),
    noise_sigma: gaussianSigma(),
  };
}

function selectionPayload() {
  const s = state.selection;
  if (!s) return null;
  return {
    lon: s.lon,
    lat: s.lat,
    fov_radius_arcsec: selectionArcsec(),
    rotation_deg: s.rotationDeg || 0,
  };
}

/** How earthshine is treated in a render.
 *
 *  Nearly all of this is the ephemeris: the Earth's phase, distance and hence
 *  the earthshine-to-sunlight ratio are computed from the instant, so the page
 *  only chooses whether that light is drawn and with what deliberate boost.
 *  A blank albedo means "the ephemeris default"; a gain above 1 is an HDR
 *  composite, not a different sky. */
function earthshinePayload() {
  const albedo = num('earth-geometric-albedo');
  const gain = num('earthshine-gain');
  const samples = num('earth-samples');
  return {
    // Always on: the Earth's phase, distance and hence the earthshine ratio
    // are the ephemeris's business, and a switch that says "do not compute the
    // sky that is there" is a switch nobody needs.  The gain is the knob.
    on: true,
    gain: (gain === null || gain < 0) ? 1 : gain,
    earth_geometric_albedo: (albedo === null || albedo <= 0 || albedo > 1) ? null : albedo,
    earth_samples: samples === null ? 13 : Math.max(1, Math.round(samples)),
    tint: $('earthshine-tint').value.trim(),
  };
}

/** The optical knobs that describe the *light*, as one block.
 *
 *  They travel together because they all belong to the frame rather than to
 *  the moment: the preview, the close-up and the full-disk export each have to
 *  send the same ones, or the saved file would be a differently lit picture
 *  than the one on screen.  They are spread into the request body rather than
 *  nested, so a stored request stays readable. */
function lightPayload() {
  const sky = num('sky-ambient');
  const rough = num('roughness');
  return {
    photometric_model: $('photometric-model').value,
    sun_samples: Number($('sun-samples').value) || 1,
    sky_ambient: sky === null || sky < 0 ? 0 : sky,
    roughness_deg: rough === null || rough < 0 ? 0 : rough,
    earthshine: earthshinePayload(),
  };
}

/** The compute budget, carried beside the light block in every render request.
 *
 * It is not a light setting, so it has its own block: the server reads it as a
 * per-request ceiling on the band threads, which lets two requests with
 * different budgets run without either one writing an environment variable
 * the other can see.  Blank means "let the library decide".
 */
/** 阴影精度: the close-up's shadow-step budget, or null for the server default.
 *
 *  A shadow edge at a grazing sun is quantised to the step length, so this is a
 *  real quality knob -- but only for the *edges*.  The texture inside a lit
 *  slope is terrain and albedo data at the finest level, and no step budget
 *  changes it; the tooltip says so rather than letting a reader expect
 *  otherwise.
 */
function shadowStepsPayload() {
  const select = $('shadow-steps');
  if (!select || select.value === 'auto') return null;
  const value = Number(select.value);
  return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
}

function computePayload() {
  const value = num('parallel');
  return {
    parallel: value === null || !Number.isFinite(value) || value < 1
      ? null : Math.round(value),
  };
}
/** One line describing the earthshine a frame was actually drawn with.
 *
 *  *block* is either the ephemeris block the last render reported (which
 *  carries ``earthshine_ratio``) or, before the first render, the page's own
 *  request block -- in which case the line says what the *next* render will
 *  use and makes clear that the ephemeris numbers are still to come. */
/** One gain, two fields: copy the value that was just typed into the other.
 *
 * The full disk's save row and the light section both set the earthshine gain,
 * and two fields showing different numbers for one parameter would be a bug the
 * reader cannot see. */
/** One gain, four fields: copy the value that was just typed into the others.
 *
 * The light section, the full disk's save row, the close-up's save row and the
 * animation all set the earthshine gain, and fields showing different numbers
 * for one parameter would be a bug the reader cannot see. */
function syncEarthshineGain(id) {
  const source = $(id);
  if (!source) return;
  for (const other of ['earthshine-gain', 'disk-earthshine-gain',
                       'region-earthshine-gain', 'anim-earthshine-gain']) {
    if (other === id) continue;
    const target = $(other);
    if (target) target.value = source.value;
  }
}

/** Every control's value as the page was loaded.
 *
 * Captured before anything touches the page, so "复位" restores what the markup
 * said rather than a second list of defaults that would drift from it.
 */
const DEFAULTS = {};

function snapshotDefaults() {
  // Read from the page's own list of controls rather than from a DOM sweep:
  // the list is the inventory the settings file already uses, so a control that
  // can be saved is a control that can be reset, and nothing else is touched.
  const ids = [...SETTING_FIELDS, ...OUTPUT_FIELDS, ...PROC_FIELDS, ...LABEL_FIELDS];
  for (const id of ids) {
    const element = $(id);
    if (!element) continue;
    DEFAULTS[id] = element.type === 'checkbox' ? element.checked : element.value;
  }
}

/** Put a named group of controls back to those values.  Returns how many. */
function resetFields(ids) {
  let count = 0;
  for (const id of ids) {
    if (!(id in DEFAULTS)) continue;
    const element = $(id);
    if (!element) continue;
    if (element.type === 'checkbox') element.checked = DEFAULTS[id];
    else element.value = DEFAULTS[id];
    if (element.type === 'range') element.dispatchEvent(new Event('input'));
    count += 1;
  }
  return count;
}

/**
 * Double-clicking a control puts it back to the value the page started with.
 *
 * A slider is a position on an axis, and the interesting part of that axis is
 * usually a narrow band somewhere inside it: after a drag, the way back to
 * "where it was" is a guess, and going to the end is not it (模糊强度 starts at
 * 1, not at 0).  One reset button per slider would be a dozen buttons down the
 * column; a double-click is the gesture people already try, and it costs the
 * panel no height at all.
 *
 * Both events are dispatched, because the page listens for both: `input` while
 * a slider is being dragged, `change` when a number has been typed.  So a
 * double-click behaves exactly like having moved the control there by hand --
 * including the side effects (视宁度's slider and field follow each other, the
 * label density re-plans the names).
 */
function wireDoubleClickReset(ids) {
  for (const id of ids) {
    const element = $(id);
    if (!element || !(id in DEFAULTS)) continue;
    element.addEventListener('dblclick', () => {
      if (element.type === 'checkbox') element.checked = DEFAULTS[id];
      else element.value = DEFAULTS[id];
      element.dispatchEvent(new Event('input'));
      element.dispatchEvent(new Event('change'));
    });
  }
}

/** The sliders a double-click resets: 后期处理's axis controls, and the label
 *  density, which is the same kind of thing on the other side of the page. */
const RESETTABLE_SLIDERS = [
  'seeing', 'seeing-num', 'blur-strength', 'diffraction', 'sharpen-radius',
  'sharpen-strength', 'saturation', 'gain', 'offset', 'gamma',
  'noise-poisson', 'noise-gaussian', 'image-rotation', 'image-rotation-num',
  'label-density',
];

/** 模拟设置: the resolution and the light. */
const OUTPUT_FIELDS = [
  'out-kmpx-preset', 'out-kmpx', 'out-scale', 'photometric-model', 'sun-samples',
  'roughness', 'sky-ambient', 'earthshine-gain', 'earth-geometric-albedo',
  'earth-samples', 'earthshine-tint', 'out-samples', 'parallel', 'light-advanced',
];

/** 后期处理: the atmosphere, the instrument and the sensor. */
const PROC_FIELDS = [
  'seeing', 'seeing-num', 'blur-strength', 'diffraction',
  'sharpen-radius', 'sharpen-strength', 'saturation', 'gain', 'offset', 'gamma',
  'noise-poisson', 'noise-gaussian', 'image-rotation', 'image-rotation-num',
  'eyepiece-on', 'eyepiece-falloff', 'eyepiece-vignette', 'eyepiece-distortion',
  'eyepiece-mask', 'events-start', 'events-end',
];

/** The announce annotations panel's own controls. */
const LABEL_FIELDS = [
  'label-density', 'label-font', 'label-language', 'label-colour', 'label-shadow',
  'label-lock',
  'text-bold', 'text-color', 'text-stroke', 'text-stroke-width',
  'marker-stroke', 'marker-stroke-width',
  'outline-style', 'outline-width', 'outline-color',
];

/** The current instant as the wall clock of the page's own time zone. */
function wallClockNow() {
  const offset = Number($('tz').value);
  const hours = Number.isFinite(offset) ? offset : 0;
  return new Date(Date.now() + hours * 3600 * 1000).toISOString().slice(0, 16);
}

/** 时区 always shows its sign: ``+8`` rather than ``8``.
 *
 * Next to a UTC label a bare 8 reads like a fact about the machine; the sign is
 * what says which side of Greenwich the reader is on.  The field is a text input
 * for exactly that reason -- a number input cannot display its own plus sign --
 * so the up/down keys it used to get for free are wired back by hand. */
function formatTimezone() {
  const field = $('tz');
  if (!field) return;
  const raw = Number(String(field.value).trim());
  const hours = Number.isFinite(raw) ? Math.max(-12, Math.min(14, Math.round(raw))) : 0;
  field.value = (hours >= 0 ? '+' : '') + String(hours);
}

/** One hour up or down, from the arrow keys the text field no longer gives us. */
function stepTimezone(delta) {
  const field = $('tz');
  if (!field) return;
  const hours = Number(field.value) || 0;
  field.value = String(Math.max(-12, Math.min(14, hours + delta)));
  formatTimezone();
  field.dispatchEvent(new Event('change'));
}

/** Add whole calendar months, clamping the day to the end of the target month.
 *
 * "One month later" from 31 January is 28 (or 29) February, not 2 or 3 March:
 * `Date.setUTCMonth` rolls the overflow forward, which turns a nudge meant to
 * walk the phases into a jump of a different length every time.
 */
function addMonths(timestamp, delta) {
  const date = new Date(timestamp);
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + delta);
  const last = new Date(Date.UTC(
    date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, last));
  return date.getTime();
}

/** Split a nudge step like ``-1d`` into its amount and its unit.
 *
 * Lower case ``m`` is **minutes** and upper case ``M`` is **months**, which is
 * the only way the two can be told apart at a glance; ``w`` is weeks, ``y``
 * years.  The two rows that nudge a *range* use the coarse units (w/M/y/10y)
 * and the two that nudge an observing instant use the fine ones (m/h/d). */
function parseStep(step) {
  const match = /^([+-]?)(\d+)([mhdwMy])$/.exec(String(step || ''));
  if (!match) return null;
  return {
    amount: Number(match[2]) * (match[1] === '-' ? -1 : 1),
    unit: match[3],
  };
}

/** One instant, moved by one step.  Returns ``YYYY-MM-DDTHH:MM`` or null. */
function shiftInstant(value, step) {
  const parsed = parseStep(step);
  if (!parsed) return null;
  const base = Date.parse(String(value || '') + ':00Z');
  if (!Number.isFinite(base)) return null;
  const { amount, unit } = parsed;
  let next = base;
  if (unit === 'M') {
    next = addMonths(base, amount);
  } else if (unit === 'y') {
    next = addMonths(base, 12 * amount);
  } else {
    const milliseconds = { m: 60000, h: 3600000, d: 86400000, w: 604800000 }[unit];
    if (!milliseconds) return null;
    next = base + amount * milliseconds;
  }
  return new Date(next).toISOString().slice(0, 16);
}

/** Move the observer's instant by one nudge step: ``-1m``/``+1h``/``+1d``/``+1M``.
 *
 * The four sizes are the four a reader actually reaches for: a minute to walk
 * the terminator across a feature, an hour and a day to follow the phase, and a
 * calendar month to jump a lunation. */
function nudgeTime(step) {
  const field = $('when');
  if (!field) return;
  const next = shiftInstant(field.value || wallClockNow(), step);
  if (!next) return;
  setWhenField(next);
  setStatus(`时间 ${step} → ${field.value}`);
}

/** Move a whole range: the fields it names, together, so nothing else changes.
 *
 * Moving one end of a window would silently change how much time is being
 * searched, which is the one thing a reader is not asking for when they nudge a
 * range.  Both callers now name a single field (生成动画's two rows sit under
 * 开始 and 结束 and each moves its own end), so the plural is what the contract
 * is: a row may move one field or several, and it moves them together.
 *
 * 事件预报 used to be the plural case -- eight decade/month/week buttons that
 * slid the whole window.  It has none any more: its two ends are typed, and a
 * search window is not something a reader walks a decade at a time. */
function nudgeRange(step, ids, label) {
  const fields = ids.map((id) => $(id)).filter(Boolean);
  if (fields.length !== ids.length) return;
  const next = fields.map((field) => shiftInstant(field.value, step));
  if (next.some((value) => !value)) return;
  fields.forEach((field, index) => { field.value = next[index]; });
  setStatus(`${label} ${step} → ${next.join(' … ')}`);
}

/** Wire every 时间微调 button on the page.
 *
 *  Each one carries `data-nudge="±<n><unit>"` and `data-range`; the range says
 *  which field(s) it moves.  生成动画 has two rows, one under 开始 and one under
 *  结束: a reader pressing "+1h" under 结束 is asking for a longer animation,
 *  not for the same animation shown an hour later. */
function wireRangeNudges() {
  const ranges = {
    'anim-start': { ids: ['anim-start'], label: '动画开始' },
    'anim-end': { ids: ['anim-end'], label: '动画结束' },
  };
  for (const button of document.querySelectorAll('[data-nudge]')) {
    const range = ranges[button.dataset.range];
    if (!range) continue;
    button.addEventListener('click', () => {
      nudgeRange(button.dataset.nudge, range.ids, range.label);
      // 生成动画's own rows: the span line is the answer to "and how long is
      // that now", so it follows every way the range can be moved.
      if (range.ids.some((id) => id.startsWith('anim-'))) refreshAnimationPlan();
    });
  }
}

/** The animation's 物理分辨率 menu: 原生, the copied ladder, or 自定义. */
function wireAnimKmPx() {
  const select = $('anim-kmpx-preset');
  const input = $('anim-kmpx');
  if (!select || !input) return;
  select.addEventListener('change', () => {
    if (select.value === 'custom') {
      input.hidden = false;
      if (input.focus) input.focus();
      if (input.select) input.select();
      updateAnimScale();
      return;
    }
    input.hidden = true;
    updateAnimScale();
  });
  input.addEventListener('input', updateAnimScale);
  input.addEventListener('change', () => {
    const value = num('anim-kmpx');
    if (value === null || value <= 0) {
      select.value = 'native';
      input.hidden = true;
    }
    updateAnimScale();
  });
}

/** Write the observer's instant and let the page react as if it were typed. */
function setWhenField(value) {
  const field = $('when');
  if (!field) return;
  field.value = value;
  field.dispatchEvent(new Event('change'));
}

/** 同步设备时间: the instant now, in the time zone the field beside it names. */
function syncDeviceTime() {
  setWhenField(wallClockNow());
  const offset = Number($('tz').value) || 0;
  setStatus(`已同步设备时间：${$('when').value}（UTC${offset >= 0 ? '+' : ''}${offset}）`);
}

/** Redraw the earthshine line after a control changed.
 *
 * The ephemeris numbers arrive with the next render, so this keeps the *last
 * reported* block rather than describing the controls back at the reader: the
 * line belongs to the picture, and blanking it on every keystroke would make
 * it look like part of the form.  Before the first render there is nothing to
 * keep, and the line is empty. */
function updateEarthshineInfo() {
  const reported = state.earthshine ||
    (state.geometry && state.geometry.earthshine);
  describeEarthshine(reported || earthshinePayload(), state.geometry || {});
}

/** The earthshine line: the two magnitudes that describe the light, in the
 *  full disk's own footer.
 *
 *  It used to be a footnote under 模拟设置, which put a measurement of the
 *  *frame* in the middle of the controls that produced it.  The old line also
 *  carried the ratio, the Earth's phase angle, its illuminated fraction, the
 *  gain and the tint; the ratio is one more number to read past, and the rest
 *  are inputs the reader typed or that the picture already shows.
 *
 *  **地照亮度 is the Moon's night side, not the Earth.**  The number this line
 *  prints is the apparent magnitude of the earthshine-lit part of the Moon as
 *  it appears in *our* sky (``earthshine_apparent_magnitude``); it was
 *  mislabelled for a round with the magnitude of the Earth in the Moon's sky,
 *  which is a different quantity about a different body.  The Earth's own
 *  magnitude is not printed anywhere -- it is the *source* of the earthshine,
 *  and it is a property of the sky rather than of the picture.
 */
function describeEarthshine(block, context) {
  const el = $('disk-info-earthshine');
  if (!el) return;
  // Nothing to say before a render: the line carries measurements, and a
  // placeholder that lists the settings back at the reader is not one.
  if (!block || block.earthshine_ratio === undefined) { el.textContent = ''; return; }
  const parts = [];
  if (block.earthshine_on === false) parts.push('地照已关闭（未画）');
  // The Moon's own magnitude is the library's (moonlib_s.moonview.moon_magnitude,
  // Allen's fit); the page only prints it.  The albedo is an input the reader
  // typed, so it is not repeated here.
  const ctx = context || {};
  const magnitude = Number(ctx.moon_magnitude);
  if (Number.isFinite(magnitude)) parts.push(`月球亮度 ${magnitude.toFixed(2)} mag`);
  const ashen = Number(block.earthshine_apparent_magnitude);
  if (Number.isFinite(ashen)) parts.push(`地照亮度 ${ashen.toFixed(2)} mag`);
  el.textContent = parts.join('　');
  el.title = '地照亮度：月面夜侧（被地球照亮的那一部分）在地球天空里的星等。';
}

/* ── drawing the disk ────────────────────────────────────────────── */

/* Orthographic projection, the same one the renderer uses.
 * Screen: x right, y down.  North up, selenographic east to the right.
 *
 * `disk_radius_fraction` is the Moon's radius as a fraction of the frame's
 * shorter side, and `angular_diameter_deg` is its apparent size; together they
 * give the pixels-per-degree the selection box is measured against. */
function diskGeom() {
  const g = state.geometry;
  const w = DISK.width, h = DISK.height;
  const radius = g.disk_radius_fraction * Math.min(w, h);
  return {
    cx: w / 2, cy: h / 2, radius,
    subLon: g.libration_lon_deg, subLat: g.libration_lat_deg,
    angularRadiusDeg: g.angular_diameter_deg / 2,
  };
}

/** Angular radius of the current selection, in arcseconds. */
function selectionArcsec() {
  const s = state.selection;
  if (!s || !state.geometry) return null;
  return s.radiusFraction * (state.geometry.angular_diameter_deg / 2) * 3600;
}

/* ── the camera, and the projection both halves have to agree on ───────────
 *
 * The picture is not an orthographic drawing of a sphere: it is a *photograph*
 * of one, taken from 384 400 km away, and the renderer marches rays from that
 * finite distance.  An orthographic mapping puts a point at ``sin c`` times the
 * limb's radius; the real one puts it at ``sin c / (1 - (R/d) cos c)``, which
 * is 0.45% further out in the middle of the disk and the same at the edge.
 *
 * Half a pixel on a 1000 px preview, but the *labels* come from the server,
 * which projects exactly, and the geometric overlays are drawn here -- so the
 * two would sit on slightly different pictures, worst where the surface is
 * least foreshortened.  Everything below therefore uses the renderer's own
 * gnomonic form, and the round trip pixel -> lon/lat -> pixel is exact.
 */

/** The camera's Moon-fixed basis: forward, right and up, as unit vectors.
 *
 * ``forward`` runs from the observer towards the Moon's centre, so it is the
 * negative of the sub-observer direction; ``up`` is the rotation axis projected
 * across it, which is what puts selenographic north up in the picture. */
function cameraBasis(g) {
  const lat = g.subLat * Math.PI / 180, lon = g.subLon * Math.PI / 180;
  const forward = [
    -Math.cos(lat) * Math.cos(lon),
    -Math.cos(lat) * Math.sin(lon),
    -Math.sin(lat),
  ];
  // up = pole - (pole . forward) forward
  const dotPole = forward[2];
  const up = unit3([-dotPole * forward[0], -dotPole * forward[1],
                    1 - dotPole * forward[2]]);
  const right = unit3(cross3(forward, up));
  return { forward, right, up };
}

function dot3(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** The frame's pixel scale in the renderer's own terms.
 *
 * ``k`` is sin(the Moon's angular radius) = R/d, and ``s`` is its cosine, so
 * ``s/k`` is the tangent the ray grid is built from. */
function cameraScale(g) {
  const k = Math.sin(g.angularRadiusDeg * Math.PI / 180);
  const s = Math.sqrt(Math.max(1e-12, 1 - k * k));
  return { k, s, tangent: s / Math.max(k, 1e-12) };
}

/** Canvas pixel -> selenographic lon/lat, or null if outside the disk.
 *
 * The inverse of the renderer's ray: build the ray through the pixel, meet it
 * with the sphere of reference radius, and read the coordinates of the hit. */
function pixelToLonLat(px, py) {
  const g = diskGeom();
  const { radius } = g;
  const { k, s } = cameraScale(g);
  const { forward, right, up } = cameraBasis(g);
  // The renderer indexes pixels from (n-1)/2; a pixel index is a *canvas*
  // coordinate 0.5 further along, which is the +0.5 folded into g.cx.
  const pixelScale = (k / s) / Math.max(1e-9, radius);
  const column = (px - g.cx) * pixelScale;
  const row = (g.cy - py) * pixelScale;
  const direction = unit3([
    forward[0] + column * right[0] + row * up[0],
    forward[1] + column * right[1] + row * up[1],
    forward[2] + column * right[2] + row * up[2],
  ]);
  // The observer sits at -d * forward, with d = R/k.
  const distance = 1 / Math.max(k, 1e-12);            // in units of R
  const b = -distance * dot3(direction, forward);
  const disc = b * b - (distance * distance - 1);
  if (!(disc > 0)) return null;
  const t = -b - Math.sqrt(disc);
  if (!(t > 0)) return null;
  const point = [
    -distance * forward[0] + t * direction[0],
    -distance * forward[1] + t * direction[1],
    -distance * forward[2] + t * direction[2],
  ];
  const [lon, lat] = lonLatFromVector(point);
  return { lon, lat };
}

/** Selenographic lon/lat -> canvas pixel, or null if on the far side. */
function lonLatToPixel(lonDeg, latDeg) {
  const point = projectSelenographic(lonDeg, latDeg, diskGeom());
  return point.far ? null : { x: point.x, y: point.y };
}

function drawDisk() {
  const ctx = DISK.getContext('2d');
  ctx.clearRect(0, 0, DISK.width, DISK.height);
  if (state.diskImage) {
    // 边缘平滑 rides on the draw, so the names and the selection below are not
    // caught by it -- and the canvas a save is composed from is not this one.
    const smooth = diskSmoothPx();
    ctx.filter = smooth > 0 ? `blur(${smooth}px)` : 'none';
    ctx.drawImage(state.diskImage, 0, 0, DISK.width, DISK.height);
    ctx.filter = 'none';
  }
  drawViewLabels('disk');
  drawSelection();
}

/* ── the selection overlay ─────────────────────────────────────────────────
 *
 * Drawn on its own canvas, sized in *device* pixels to the displayed area.
 * Putting it on the Moon image instead would scale every line and handle by the
 * ratio of the display size to the 560 px preview, which is what made the
 * rectangle look blurry and the handle look oversized.
 */

function syncOverlay() {
  const dpr = window.devicePixelRatio || 1;
  const rect = $('body-disk').getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (OVERLAY.width !== width || OVERLAY.height !== height) {
    OVERLAY.width = width;
    OVERLAY.height = height;
  }
  return { dpr, rect, width, height };
}

/* The rotate handle, in CSS pixels on screen. */
const HANDLE_RADIUS = 8;
const HANDLE_GAP = 20;

/* Locking is only meaningful once the camera is fully specified, because the
   locked size *is* the camera's field of view. */
function lockActive() {
  return $('sel-locked').checked && cameraFieldRadiusArcsec() > 0;
}

/** Half the diagonal of the camera's field, in arcseconds, or 0 if unknown.
 *
 * The magnification is the Barlow *times* the back-focus ratio: both scale the
 * effective focal length, and both therefore scale the frame the sensor covers.
 */
function cameraFieldRadiusArcsec() {
  const w = num('sensor-w'), h = num('sensor-h'), p = num('pixel-um');
  let focal = num('focal-length');
  if (!focal) {
    const aperture = num('aperture'), ratio = num('focal-ratio');
    if (!aperture || !ratio) return 0;
    focal = aperture * ratio;
  }
  if (!w || !h || !p || !focal) return 0;
  const barlow = num('barlow') || 1;
  const backFocus = num('back-focus') || 1;
  const plate = 206264.806 * (p * 1e-3) / (focal * barlow * backFocus);
  return 0.5 * Math.hypot(w * plate, h * plate);
}

/** Height / width of the camera's own frame, or 1 when it is not known. */
function cameraAspect() {
  const w = num('sensor-w'), h = num('sensor-h');
  if (!w || !h) return 1;
  return h / w;
}

/** Height / width of the selection box.
 *
 * The box has an aspect of its own (`selection.aspect`), which is what makes a
 * free box free: with both locks off, dragging one edge changes that dimension
 * alone.  The camera lock is the one thing that overrides it -- a box locked to
 * the sensor must keep the sensor's proportions.
 */
function shapeAspect() {
  if (lockActive()) return cameraAspect();
  const s = state.selection;
  const aspect = s && Number(s.aspect);
  return Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
}

/** The whole camera frame in arcseconds: (width, height) of the sensor's field.
 *
 * The plate scale turns the sensor's millimetres into sky, and the two sides
 * are independent -- the diagonal is not a side, which the old lock pretended
 * it was (a 3:2 sensor got a box 1.5x too large in both directions).
 */
function cameraFieldArcsec() {
  const w = num('sensor-w'), h = num('sensor-h'), p = num('pixel-um');
  let focal = num('focal-length');
  if (!focal) {
    const aperture = num('aperture'), ratio = num('focal-ratio');
    if (!aperture || !ratio) return null;
    focal = aperture * ratio;
  }
  if (!w || !h || !p || !focal) return null;
  const barlow = num('barlow') || 1;
  const backFocus = num('back-focus') || 1;
  const plate = 206264.806 * (p * 1e-3) / (focal * barlow * backFocus);
  return { width: w * plate, height: h * plate };
}

/** Make the selection the size of the camera's field, when the lock is on.
 *
 * The radius the server wants is the frame's *shorter* side, so that is what
 * the fraction is measured from; the aspect carries the other one. */
function applyLock() {
  if (!lockActive() || !state.geometry) return;
  const field = cameraFieldArcsec();
  const full = (state.geometry.angular_diameter_deg / 2) * 3600;
  if (!field || !(full > 0)) return;
  const shortHalf = 0.5 * Math.min(field.width, field.height);
  state.selection = {
    lon: state.selection ? state.selection.lon : state.geometry.libration_lon_deg,
    lat: state.selection ? state.selection.lat : state.geometry.libration_lat_deg,
    radiusFraction: Math.min(1.4, shortHalf / full),
    rotationDeg: state.selection ? state.selection.rotationDeg : 0,
    aspect: field.height / field.width,
  };
}

/** Say whether the lock can do anything, and refuse to arm it if it cannot. */

/* The box is drawn under ctx.rotate(-rotation), which maps a point (u, v) of
   the box's own frame to the screen as
       screen = (u cos r + v sin r, -u sin r + v cos r)
   The inverse is therefore (dx cos r - dy sin r, dx sin r + dy cos r).
   Both directions live here and nowhere else: the hit test and the resize used
   to carry their own copies with the sign flipped, which is why corners stopped
   responding as soon as the box was rotated. */
function boxToScreen(box, u, v) {
  const c = Math.cos(box.rotation), sn = Math.sin(box.rotation);
  return { x: box.centre.x + u * c + v * sn, y: box.centre.y - u * sn + v * c };
}

function screenToBox(box, dx, dy) {
  const c = Math.cos(box.rotation), sn = Math.sin(box.rotation);
  return { u: dx * c - dy * sn, v: dx * sn + dy * c };
}

/* Geometry of the selection box in *preview-image* pixels.  One definition,
   used by the drawing, the hit test and the rotate handle, so they cannot
   disagree. */
function boxGeometry() {
  const s = state.selection;
  if (!s || !state.geometry) return null;
  const { radius } = diskGeom();
  const centre = lonLatToPixel(s.lon, s.lat);
  if (!centre) return null;
  // `radiusFraction` is the frame's *shorter* half-side -- the number the
  // server's fov_radius means -- and the aspect turns it into the two sides.
  const aspect = shapeAspect();
  const shortHalf = Math.max(6, s.radiusFraction * radius);
  const halfW = aspect >= 1 ? shortHalf : shortHalf / aspect;
  const halfH = shortHalf * Math.max(1, aspect);
  return {
    centre, halfW, halfH,
    rotation: (s.rotationDeg || 0) * Math.PI / 180,
    handleRadius: HANDLE_RADIUS,
    // The handle sits above the top edge, clear of the corner ticks.
    handleDistance: halfH + HANDLE_GAP,
  };
}

/* ── what "下载 PNG" downloads ─────────────────────────────────────────────
 *
 * The three pictures do not share output parameters: the preview has a pixel
 * count, the close-ups have width/height/sampling.  Choosing a target therefore
 * also decides which of those controls are live, and the link follows whatever
 * that picture last produced -- so the button never downloads a picture from a
 * different tab than the one it says it will.
 */
const DOWNLOAD_TARGETS = {
  disk: { file: 'moon-disk' },
  raw: { file: 'moon-geometry' },
  proc: { file: 'moon-processed' },
};

function downloadTarget() {
  return activeTarget();     // whichever tab is on screen
}

/* ── 分量: which radiance the close-up shows and saves ─────────────────────
 *
 * The full disk has had this since it got its own header: sunlight and
 * earthshine differ by about twelve magnitudes, so exporting one of them means
 * exposing *that* half rather than metering the scene for the sunlit surface.
 * The close-up cache keeps the same two halves apart, so the two panes show the
 * chosen component and the save writes what is on screen -- no ray march, and
 * no way for the file to be a different picture than the one being looked at.
 */
const COMPONENT_NAMES = { image: '完整画面', sunlit: '阳光分量', earthshine: '地照分量' };

/** 地照 ticked = the whole picture, unticked = the sunlit half alone. */
function regionComponent() {
  const box = $('region-earthshine');
  return box && !box.checked ? 'sunlit' : 'image';
}

/** The stem a saved picture gets: which component it is, is part of what it is. */
function downloadFileName(target) {
  const component = target === 'disk' ? 'image' : regionComponent();
  return DOWNLOAD_TARGETS[target].file + (component === 'image' ? '' : '-' + component);
}

/** Point the download link at the chosen picture, if it exists yet. */
function updateDownloadLink() {
  const target = downloadTarget();
  const link = $('download');
  const url = state.downloads[target];
  // Always in the row: a save button that appears only after a render reads as
  // a save button that does not exist.  Before that it says so instead.
  link.hidden = false;
  link.setAttribute('aria-disabled', url ? 'false' : 'true');
  link.title = url ? '保存这一张' : '先点「生成模拟」，才有图可存';
  if (!url) {
    link.removeAttribute('href');
    return;
  }
  link.href = url;
  // The link's own href is always the PNG the server sent; the extension has to
  // say so, or the browser saves a file the system cannot open.
  link.download = downloadFileName(target) + '.png';
}

/* ── resolution: preview and the close-up render ───────────────────────────
 *
 * Two sizes, deliberately separate, because they cost very different things:
 *
 *   **the whole-disk preview** (预览 px, in the disk's own title bar) is how
 *   sharp the aiming picture is.  It is re-rendered on every change of time or
 *   place, so it is capped to keep that responsive.
 *   **the close-up render** (生成模拟 / 更新模拟) is the size the 采样 ″/px
 *   setting and the selection imply -- the frame the settings describe, from
 *   the very first press.  It is allowed to be slow; the reader chose the
 *   scale.
 *
 * The pixel count and the plate scale say the same thing twice, so they are
 * kept in step: type a scale and the pixels follow, type pixels and the scale
 * (and the kilometres of Moon each pixel covers) is reported back.
 */
//: Output pixels are the user's call -- the pane asks for exactly what is typed
//: and the server renders it -- so the only ceiling is the one the server
//: publishes as a memory guard, not a clamp the page applies quietly.
const LIMITS = {
  //: The preview's own cap.  It is re-rendered on every change of time or
  //: place, but 5000 px is offered as a deliberate choice, so it has to be
  //: allowed rather than silently clamped.
  preview_px: 5000,
  output_px: 8192,
  //: The full disk has its own ceiling, far above the preview's: it is rendered
  //: when the save button is pressed, not on every keystroke.
  disk_px: 10000,
  //: Apparent radius at which the server also draws a feature's rim.  Sent
  //: explicitly so an export at another resolution can scale it with the rest.
  label_outline_px: 9,
  // Filled in from /api/health: the local LOLA DEM's pixel size. Rendering
  // finer than this repeats the same terrain sample, so it is a hard floor
  // rather than a suggestion.
  terrain_km_per_px: 0.118,
  //: Frames one animation may hold; the server refuses more, so the span line
  //: says so before the button is pressed.
  animation_max_frames: 4000,
  //: What the host reports, so the 并行数 field cannot promise more threads
  //: than the machine has.  Filled in from /api/health.
  cpu_count: 8,
  //: What /api/health said about the elevation products (see dataSources.js
  //: section below).  Null until the first reply.
  data_sources: null,
};

/** The plate scale the terrain data can actually support, in arcseconds. */
function minArcsecPerPixel() {
  const distance = (state.geometry && state.geometry.distance_km) || 384400;
  return LIMITS.terrain_km_per_px * 206264.806 / distance;
}

/** Ground kilometres per pixel for a plate scale in arcseconds. */
function kilometresPerPixel(arcsecPerPixel) {
  const distance = (state.geometry && state.geometry.distance_km) || 384400;
  return (arcsecPerPixel / 206264.806) * distance;
}
//: Seconds per (pixel x samples^2), seeded from the documented 1200px figure and
//: replaced by what this machine actually measures on the first render.
const DEFAULT_SECONDS_PER_COST = 1.15e-6;

/** A number kept inside a range, for the parameters that are not pixels. */
function clamp(value, low, high) {
  const number = Number(value);
  if (!Number.isFinite(number)) return low;
  return Math.min(high, Math.max(low, number));
}

function clampPx(value, low, high) {
  const number = Number(value);
  if (!Number.isFinite(number)) return low;
  return Math.max(low, Math.min(high, Math.round(number)));
}

/** The whole-disk preview size the user typed, inside the server's cap. */
function previewPixels() {
  return clampPx(num('preview-px') || 1000, 240, LIMITS.preview_px);
}

/** The size the *saved* full disk is rendered at -- its own number, up to the
 *  server's much higher ceiling for this one picture. */
function diskPixels() {
  return clampPx(num('disk-px') || 2400, 240, LIMITS.disk_px);
}

/* ── the two resolution pickers ────────────────────────────────────────────
 *
 * A preset menu and a field you can type in, which is what the two disk
 * resolutions are: `<input list=…>` looked like neither -- a datalist shows no
 * arrow until you type, so the menu was invisible and the field looked like the
 * only way in.  The `<select>` is the menu; the text field is the number the
 * rest of the page reads, and it appears when 自定义 is picked.
 */
function syncResolutionPicker(inputId, selectId) {
  const input = $(inputId);
  const select = $(selectId);
  if (!input || !select) return;
  const value = String(num(inputId) === null ? '' : num(inputId));
  const known = [...select.options].some((option) => option.value === value);
  select.value = known ? value : 'custom';
  input.hidden = select.value !== 'custom';
}

function wireResolutionPicker(inputId, selectId) {
  const input = $(inputId);
  const select = $(selectId);
  if (!input || !select) return;
  syncResolutionPicker(inputId, selectId);
  select.addEventListener('change', () => {
    if (select.value === 'custom') {
      input.hidden = false;
      if (input.focus) input.focus();
      if (input.select) input.select();
      return;
    }
    input.value = select.value;
    input.hidden = true;
    // The field is the authority; the menu is only a way to fill it in.
    input.dispatchEvent(new Event('input'));
    input.dispatchEvent(new Event('change'));
  });
  // Typing a number that happens to be on the menu puts the menu back on it.
  input.addEventListener('change', () => syncResolutionPicker(inputId, selectId));
}

/** Aspect of the selection box (height / width); 1 when nothing is selected. */
function selectionAspect() {
  return state.selection ? shapeAspect() : 1;
}

/** Arcseconds across the selection, which is the frame's shorter side. */
function selectionSpanArcsec() {
  const radius = selectionArcsec();
  return radius === null ? null : 2 * radius;
}

/** The size the *file* is saved at: one number, referred to one side.
 *
 * Two fields meant two chances to disagree with the selection's shape; one
 * field plus 长边/短边 means the aspect is always the box's own, and the only
 * choice left is which side the number is about.
 */
function outputPixels() {
  const a = Math.max(1e-6, selectionAspect());     // height / width
  const typed = clampPx(num('out-px') || 1200, 64, LIMITS.output_px);
  const reference = $('out-ref') && $('out-ref').value === 'short' ? 'short' : 'long';
  const portrait = a >= 1;
  let width;
  let height;
  if (reference === 'long') {
    if (portrait) { height = typed; width = typed / a; }
    else { width = typed; height = typed * a; }
  } else if (portrait) { width = typed; height = typed * a; }
  else { height = typed; width = typed / a; }
  return {
    width: Math.max(16, Math.round(width)),
    height: Math.max(16, Math.round(height)),
  };
}

/** The size of the *render*, which follows from the plate scale.
 *
 * Nothing here is typed: the resolution pane says how much Moon one pixel
 * covers, and the frame that implies is computed -- shorter side across the
 * selection, longer side by the selection's aspect. The save pane then rescales
 * this picture to whatever pixel count the file wants.
 */
/** The largest frame the page will ask the server for from a plate scale.
 *
 * ``LIMITS.output_px`` (8192) is what the server accepts; a frame that size is
 * minutes of marching and gigabytes of linear radiance, and one press of a
 * resolution menu is not a good enough reason to spend that.  The derived size
 * stops here, the reader is told, and the typed 保存分辨率 can still ask for a
 * big *file* -- that scaling happens on the finished picture and costs nothing.
 */
const RENDER_PX_CAP = 4096;

function renderPixels() {
  const span = selectionSpanArcsec();
  const scale = num('out-scale');
  const aspect = selectionAspect();
  if (!span || !scale) {
    const fallback = 1200;
    return aspect >= 1
      ? { width: fallback, height: Math.round(fallback * aspect) }
      : { width: Math.round(fallback / aspect), height: fallback };
  }
  const cap = Math.min(LIMITS.output_px, RENDER_PX_CAP);
  const wanted = Math.round(span / Math.max(scale, minArcsecPerPixel()));
  const short = clampPx(wanted, 64, cap);
  const long = clampPx(short * Math.max(aspect, 1 / aspect), 64, cap);
  state.renderClamped = wanted > cap;
  return aspect >= 1 ? { width: short, height: long } : { width: long, height: short };
}

/** Keep the two pixel counts in the selection's proportions while locked. */
function syncOutputScale(source) {
  const span = selectionSpanArcsec();
  const distance = (state.geometry && state.geometry.distance_km) || 384400;
  const floorKm = LIMITS.terrain_km_per_px;
  const floorArcsec = minArcsecPerPixel();
  let clamped = false;
  if (source === 'kmpx') {
    let kmpx = num('out-kmpx');
    if (kmpx) {
      if (kmpx < floorKm) { kmpx = floorKm; $('out-kmpx').value = kmpx.toFixed(4); clamped = true; }
      $('out-scale').value = (kmpx * 206264.806 / distance).toFixed(4);
    }
  } else {
    let scale = num('out-scale');
    if (scale) {
      if (scale < floorArcsec) { scale = floorArcsec; $('out-scale').value = scale.toFixed(4); clamped = true; }
      $('out-kmpx').value = kilometresPerPixel(scale).toFixed(4);
    }
  }
  state.scaleClamped = clamped;
  updateSaveInfo();
}

/** What the save controls will produce: format, and pixels after scaling. */
const SAVE_FORMATS = {
  png: { extension: 'png', type: 'image/png', label: 'PNG' },
  jpg: { extension: 'jpg', type: 'image/jpeg', label: 'JPG' },
  tif: { extension: 'tif', type: 'image/tiff', label: 'TIF' },
};

/** The one line the save pane has to keep: what a saved pixel covers.
 *
 * The render is defined by a physical scale, so its own pixel count is an
 * intermediate nobody asked for; what matters is where the *file* ends up --
 * saving smaller than the render means each saved pixel covers more Moon, and
 * that number is what has to be shown.
 */
/** A ground scale in the unit that suits it: metres below a kilometre.
 *
 * "0.118 km 月面" is harder to read than "118 m 月面", and a 118 m DEM limit is
 * exactly where the two units meet, so the unit is chosen rather than fixed.
 */
/** One pixel's worth of ground, as a value with its unit and nothing else.
 *
 * "…372 m" beside a "px" a few characters to the left already says what the
 * number is about; the word 月面 was noise in every row it appeared in. */
function groundScaleText(km) {
  if (!Number.isFinite(km)) return '—';
  if (km < 1) return `${(km * 1000).toFixed(km < 0.1 ? 1 : 0)} m`;
  return `${km.toFixed(km < 10 ? 3 : 2)} km`;
}

function updateSaveInfo() {
  const { width, height } = outputPixels();
  const span = selectionSpanArcsec();
  const el = $('save-info');
  if (!el) return;
  if (!span) { el.textContent = 'px'; return; }
  // The same shape the full disk's row uses for its own scale — "px（0.06 km）"
  // — because the two rows are meant to read as one row of one kind.  The
  // arcseconds are in the tooltip: they are the answer to a rarer question.
  const arcsec = span / Math.min(width, height);
  const ground = groundScaleText(kilometresPerPixel(arcsec));
  el.textContent = `px（${ground}）`;
  el.title = `1 px = ${arcsec.toFixed(3)}″ = ${ground}；`
    + '保存文件里一个像素对应天上的多少角秒、月面上的多少公里';
}

/** Baseline 8-bit RGB TIFF: uncompressed, one strip, little-endian.
 *
 * Browsers encode PNG and JPEG and nothing else, so the third format the page
 * offers is written here rather than left out.  Twelve IFD entries and one
 * block of pixels is the whole of it -- no compression, no tiling, no colour
 * management -- which is what makes it short enough to be worth having and
 * readable everywhere.  Kept free of the DOM so the bytes can be checked.
 */
function encodeTiff(rgba, width, height) {
  const pixels = width * height * 3;
  const ifdOffset = 8 + pixels;
  const entries = 12;
  const ifdSize = 2 + entries * 12 + 4;
  const extra = ifdOffset + ifdSize;          // bits(6+2 pad) + xres(8) + yres(8)
  const buffer = new ArrayBuffer(extra + 24);
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  view.setUint16(0, 0x4949, true);            // "II": little-endian
  view.setUint16(2, 42, true);
  view.setUint32(4, ifdOffset, true);
  for (let source = 0, target = 8; source < rgba.length; source += 4, target += 3) {
    bytes[target] = rgba[source];
    bytes[target + 1] = rgba[source + 1];
    bytes[target + 2] = rgba[source + 2];
  }
  view.setUint16(ifdOffset, entries, true);
  let at = ifdOffset + 2;
  const field = (tag, type, count, value) => {
    view.setUint16(at, tag, true);
    view.setUint16(at + 2, type, true);
    view.setUint32(at + 4, count, true);
    if (type === 3) {                         // SHORT: value in the first half
      view.setUint16(at + 8, value, true);
      view.setUint16(at + 10, 0, true);
    } else {
      view.setUint32(at + 8, value, true);
    }
    at += 12;
  };
  const bitsAt = extra;
  const xResAt = extra + 8;
  const yResAt = extra + 16;
  field(256, 4, 1, width);                    // ImageWidth
  field(257, 4, 1, height);                   // ImageLength
  field(258, 3, 3, bitsAt);                   // BitsPerSample -> 8,8,8
  field(259, 3, 1, 1);                        // Compression: none
  field(262, 3, 1, 2);                        // Photometric: RGB
  field(273, 4, 1, 8);                        // StripOffsets
  field(277, 3, 1, 3);                        // SamplesPerPixel
  field(278, 4, 1, height);                   // RowsPerStrip
  field(279, 4, 1, pixels);                   // StripByteCounts
  field(282, 5, 1, xResAt);                   // XResolution
  field(283, 5, 1, yResAt);                   // YResolution
  field(296, 3, 1, 2);                        // ResolutionUnit: inch
  view.setUint32(at, 0, true);                // no next IFD
  view.setUint16(bitsAt, 8, true);
  view.setUint16(bitsAt + 2, 8, true);
  view.setUint16(bitsAt + 4, 8, true);
  view.setUint16(bitsAt + 6, 0, true);
  view.setUint32(xResAt, 72, true);
  view.setUint32(xResAt + 4, 1, true);
  view.setUint32(yResAt, 72, true);
  view.setUint32(yResAt + 4, 1, true);
  return buffer;
}

function updateSamplesInfo() {
  const samples = Number($('out-samples').value) || 1;
  $('out-samples').title = `${samples}×${samples} = 每像素 ${samples * samples} 条光线`;
}

/** Re-render the close-up after the output parameters are edited by hand.
 *
 *  Typing a resolution is a statement about the picture, so the picture has to
 *  follow: the raw render is regenerated at the new size shortly after the last
 *  keystroke, and the button stays for when you want it immediately. */
let outputTimer = null;

function scheduleOutputRender() {
  if (!state.selection || !state.rawImage) return;
  clearTimeout(outputTimer);
  // Above the cap the reader has to ask for it: a menu selection that quietly
  // starts a minutes-long, gigabyte render is how the server gets wedged.
  const { width, height } = renderPixels();
  if (state.renderClamped) {
    setStatus(`按这个采样率要 ${width}×${height} px（已到上限），` +
      '点「更新图像」才会渲染。', 'busy');
    return;
  }
  setStatus('模拟设置已改，稍后按新分辨率重新渲染…', 'busy');
  outputTimer = setTimeout(() => { renderOutput(); }, 550);
}

/** The bar is an estimate: the render is one request, so the page cannot see
 *  inside it.  It is driven by the measured speed of earlier renders. */
let renderTimer = null;

/** Whether the countdown is printed beside the elapsed time.
 *
 * `secondsPerCost` is learned from finished renders, and at the finest
 * resolutions the figure it predicts ran far above what the render actually
 * took -- a countdown that overshoots by minutes reads as a hang, which is
 * worse than no number at all.  The measurement, the learning, the bar and the
 * text are all still here; only the printed estimate is withheld.  Set this to
 * true to bring it back.
 */
const SHOW_RENDER_ESTIMATE = false;

/** The progress readout lives in the busy overlay over the picture, and the
 *  overlay is what appears and disappears -- there is nothing to show or hide
 *  beyond it. */
function showProgress(on) {
  const note = $('render-status');
  if (note) note.hidden = !on;
}

/** Both panes carry the readout, and both are written on every tick: the two
 *  are shown one at a time, and a render that reports itself in only one of
 *  them goes quiet the moment the reader switches tabs. */
const PROGRESS_BARS = ['render-progress-bar', 'render-progress-proc-bar'];
const PROGRESS_NOTES = ['render-status', 'render-status-copy'];

function setProgressBar(fraction) {
  for (const id of PROGRESS_BARS) {
    const bar = $(id);
    if (bar) bar.style.width = (fraction * 100).toFixed(1) + '%';
  }
}

function setProgressNote(text) {
  for (const id of PROGRESS_NOTES) {
    const note = $(id);
    if (note) note.textContent = text;
  }
}

function startRenderProgress(pixels, samples, label) {
  const cost = Math.max(1, pixels * samples * samples);
  const seconds = Math.max(0.5, cost * (state.secondsPerCost || DEFAULT_SECONDS_PER_COST));
  const started = performance.now();
  $('render-progress').classList.add('on');
  showProgress(true);
  clearInterval(renderTimer);
  const tick = () => {
    const elapsed = (performance.now() - started) / 1000;
    setProgressBar(Math.min(0.95, elapsed / seconds));
    // Two numbers and no words: what is running is on the button, and the
    // pass it is on ("选框预览", "保存渲染") is not something a reader waiting
    // ten seconds needs told twice.  The second number rides behind a switch
    // while it is unreliable at the finest scales; see SHOW_RENDER_ESTIMATE.
    setProgressNote(SHOW_RENDER_ESTIMATE
      ? `已用 ${elapsed.toFixed(1)} s　预计 ${seconds.toFixed(1)} s`
      : `已用 ${elapsed.toFixed(1)} s`);
  };
  tick();
  renderTimer = setInterval(tick, 120);
  return { started, label };
}

function finishRenderProgress(handle, pixels, samples, note) {
  clearInterval(renderTimer);
  const elapsed = (performance.now() - handle.started) / 1000;
  state.secondsPerCost = elapsed / Math.max(1, pixels * samples * samples);
  $('render-progress').classList.add('on');
  setProgressBar(1);
  setProgressNote(`已用 ${elapsed.toFixed(1)} s`);
  setTimeout(() => {
    $('render-progress').classList.remove('on');
    setProgressBar(0);
    // And the whole readout goes away again: an idle progress bar is a line of
    // the header spent saying nothing.
    showProgress(false);
  }, 1200);
}

/* The selection is red: it is the one thing on the picture the user put there,
   and red is the only hue the Moon itself never shows.  A dark pass underneath
   keeps it visible over the bright highlands as well as the dark maria. */
const SELECTION_COLOR = '#ff5a52';
const SELECTION_SHADOW = 'rgba(10, 12, 16, .65)';

function drawSelection() {
  const { width, height } = syncOverlay();
  const ctx = OVERLAY.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const box = boxGeometry();
  if (!box) return;
  const dpr = window.devicePixelRatio || 1;
  // Everything below is in *screen* pixels: the rectangle must not grow when
  // the picture is magnified.
  const px = dpr;
  const centre = imageToOverlay(box.centre.x, box.centre.y);
  const halfW = imageToOverlay(box.halfW, 0).x - imageToOverlay(0, 0).x;
  const halfH = imageToOverlay(0, box.halfH).y - imageToOverlay(0, 0).y;

  ctx.save();
  ctx.translate(centre.x, centre.y);
  ctx.rotate(-box.rotation);
  for (const pass of [
    { color: SELECTION_SHADOW, width: 3.2 * px },
    { color: SELECTION_COLOR, width: 1.5 * px },
  ]) {
    ctx.lineWidth = pass.width;
    ctx.strokeStyle = pass.color;
    ctx.setLineDash([7 * px, 5 * px]);
    ctx.strokeRect(-halfW, -halfH, halfW * 2, halfH * 2);
    ctx.setLineDash([]);
    const tick = 4 * px;
    ctx.fillStyle = pass.color;
    for (const [dx, dy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      ctx.fillRect(dx * halfW - tick / 2, dy * halfH - tick / 2, tick, tick);
    }
  }
  ctx.restore();

  // The handle rides on the box's own "up" axis.  A ring around a ring said
  // little and cost a lot of ink; this is a thin stem with a rotation glyph,
  // which reads as "turn me" without drawing a second circle over the picture.
  const gap = HANDLE_GAP * px;
  const radius = HANDLE_RADIUS * px;
  const topLocal = boxToScreen(box, 0, -box.halfH);
  const top = imageToOverlay(topLocal.x, topLocal.y);
  const dxu = top.x - centre.x, dyu = top.y - centre.y;
  const len = Math.max(1e-6, Math.hypot(dxu, dyu));
  const hx = top.x + (dxu / len) * gap;
  const hy = top.y + (dyu / len) * gap;

  ctx.save();
  // Just the glyph: the filled disc that used to sit under it read as a black
  // blob over the picture.  The glyph is U+21BB -- an arrow that comes back on
  // itself is what "rotate" looks like, and it is a character rather than a
  // drawing, so it is the same shape in every browser.  A dark outline keeps it
  // legible over a bright highland without painting anything behind it.
  ctx.font = `${Math.round(radius * 1.9)}px "PingFang SC", system-ui, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 3 * px;
  ctx.strokeStyle = SELECTION_SHADOW;
  ctx.strokeText('↻', hx, hy + radius * 0.06);
  ctx.fillStyle = SELECTION_COLOR;
  ctx.fillText('↻', hx, hy + radius * 0.06);
  ctx.restore();
}

/* ── landform labels ───────────────────────────────────────────────────────
 *
 * One click asks the server which named landforms are inside the frame on
 * screen and paints them over it.  Nothing is recognised: a simulation knows
 * its own camera, so the server projects the IAU gazetteer through that camera
 * and this file only draws the boxes it is handed.
 *
 * The names go on a layer of their own -- one per picture, outside the CSS
 * transform, sized in *device* pixels and redrawn whenever the zoom changes.
 * Drawing them into the picture's canvas instead meant rasterising text at the
 * preview's resolution (560 px across) and letting the browser stretch it to
 * the screen, so zooming in magnified the pixels instead of the type: exactly
 * the blur this layer exists to remove.  The two close-ups share one answer
 * because they share a camera.
 */

/* Density is one axis, not three settings: the slider runs from "a few large
   features" to "as many as fit", and both the label budget and the smallest
   radius worth a marker move together.  The numbers are the server's two
   knobs, so the control stays fine-grained without any new API. */
const DENSITY_RANGE = {
  labels: [12, 60],
  min_radius_px: [4.5, 1.5],
};

/** The label layer of each view, drawn over its picture. */
const LABEL_LAYERS = { disk: 'labels-disk', raw: 'labels-raw', proc: 'labels-proc' };

function densitySettings() {
  const raw = Number($('label-density').value);
  const at = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0.5;
  const between = (pair) => pair[0] + at * (pair[1] - pair[0]);
  return {
    labels: Math.round(between(DENSITY_RANGE.labels)),
    min_radius_px: Number(between(DENSITY_RANGE.min_radius_px).toFixed(2)),
  };
}

function updateDensityLabel() {
  const settings = densitySettings();
  $('density-value').textContent = String(settings.labels);
}

/** Which name the labels carry: the official Chinese one, English, or both. */
function labelLanguage() {
  const value = $('label-language').value;
  return value === 'en' || value === 'both' ? value : 'zh';
}

/** The one button that turns every class on or off.
 *
 * Fifteen switches is fifteen decisions before anything can be seen; the usual
 * wish is "all of them" or "none of them, I only want the geometric lines".
 * The button says which way it will go, so a press is never a surprise.
 */
function updateClassesAllButton() {
  const button = $('classes-all');
  if (!button) return;
  const boxes = [...document.querySelectorAll('#label-classes input[type="checkbox"]')];
  const allOn = boxes.length > 0 && boxes.every((box) => box.checked);
  button.textContent = allOn ? '全不选' : '全选';
  button.title = allOn ? '把所有地貌分类都关掉' : '把所有地貌分类都打开';
}

/** Put the class switches back to what the page starts with.
 *
 * Not "all on": eight families are off by default, and a reset that switched
 * them on would be a different page from the one that was just opened.
 */
function resetClasses() {
  for (const box of document.querySelectorAll('#label-classes input[type="checkbox"]')) {
    const key = String(box.id).replace('class-', '');
    const want = !DEFAULT_OFF_CLASSES.includes(key);
    if (box.checked === want) continue;
    box.checked = want;
    box.dispatchEvent(new Event('change'));
  }
  updateClassesAllButton();
}

/** The label switch follows a manual change of what is drawn.
 *
 * Touching a class chip or a geometric overlay is a statement that the reader
 * wants to see the labels; with the switch off, the change would be invisible
 * and look broken.
 */
function ensureLabelsOn() {
  if (!state.labelsOn) toggleLabels();
}

function setAllClasses(on) {
  for (const box of document.querySelectorAll('#label-classes input[type="checkbox"]')) {
    if (box.checked === on) continue;
    box.checked = on;
    box.dispatchEvent(new Event('change'));
  }
  updateClassesAllButton();
}

/** The classes whose switch is on.  The server filters on this list. */
function activeClasses() {
  const boxes = document.querySelectorAll('#label-classes input[type="checkbox"]');
  const keys = [];
  for (const box of boxes) if (box.checked) keys.push(box.value);
  return keys.length === boxes.length ? null : keys;   // null = everything
}

/** The switches themselves, built from LAND_CLASSES rather than written out.
 *
 *  Sixteen chips of markup would be sixteen places to forget when a class is
 *  added, and the palette has to live beside the keys anyway: this way the row,
 *  the colours the painter uses and the request the server gets are one list.
 */
function buildClassToggles() {
  const host = $('label-classes');
  if (!host) return;
  host.innerHTML = '';
  for (const entry of orderedClasses()) {
    const chip = document.createElement('label');
    chip.className = 'cls-chip';
    chip.dataset.on = DEFAULT_OFF_CLASSES.includes(entry.key) ? 'false' : 'true';
    chip.title = `${entry.zh} / ${entry.en}`;
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = !DEFAULT_OFF_CLASSES.includes(entry.key);
    box.id = 'class-' + entry.key;
    box.value = entry.key;
    const dot = document.createElement('span');
    dot.className = 'cls-dot';
    dot.style.color = entry.color;
    const text = document.createElement('span');
    text.textContent = entry.zh;
    const count = document.createElement('em');
    count.dataset.count = entry.key;
    count.textContent = '';
    chip.append(box, dot, text, count);
    box.addEventListener('change', () => {
      chip.dataset.on = box.checked ? 'true' : 'false';
      updateClassesAllButton();
      ensureLabelsOn();
      // Switching a family back on is also "show me these again": the names the
      // reader had dragged or put away in it come back where the catalogue puts
      // them.  Only that family -- the other classes keep their edits.
      if (box.checked) resetLabelClass(entry.key);
      refreshLabelPlans();
    });
    host.appendChild(chip);
  }
  // The class keys are parameters too, so the settings file has to carry them.
  for (const entry of LAND_CLASSES) SETTING_FIELDS.push('class-' + entry.key);
}

/** Put the catalogue's own counts on the chips, once /api/features has arrived. */
function fillClassCounts(classes) {
  if (!classes) return;
  const byKey = Object.fromEntries(classes.map((entry) => [entry.key, entry.count]));
  for (const chip of document.querySelectorAll('.cls-chip em')) {
    const count = byKey[chip.dataset.count];
    chip.textContent = count ? String(count) : '';
  }
}

/** Screen pixels (CSS) that one frame pixel covers in a view right now.
 *
 * A view whose tab is not on screen measures zero -- the element is hidden, so
 * its box is empty -- and a scale of zero would turn the font size into
 * nonsense (a 12 px label divided by 0.001 is a 12 000 px label, which the
 * server then clamps to a picture full of giant type).  So a hidden view asks
 * its partner instead: the two close-ups are the same frame in the same box.
 */
function measuredScreenScale(viewName) {
  const anchor = imageToScreen(viewName, 0, 0);
  const step = imageToScreen(viewName, 1, 1);
  if (!anchor || !step) return 0;
  const scale = (step.x - anchor.x) / anchor.dpr;
  return scale > 1e-3 ? scale : 0;
}

function screenScale(viewName) {
  const own = measuredScreenScale(viewName);
  if (own) return own;
  const view = views[viewName];
  if (view && view.link) {
    const partner = measuredScreenScale(view.link);
    if (partner) return partner;
  }
  return 1;      // nothing is displayed: fall back to 1:1, not to 1:1000
}

/** Label size in *screen* pixels: what the reader actually sees.
 *
 * Measuring the window rather than the picture is the point.  A size taken from
 * the render's own pixels would shrink on screen as the preview resolution
 * rose -- at 2000 px the same 13 px label would be four screen pixels tall --
 * so the numbers here are the ones a person reads off the screen, and the
 * frame-pixel size the server lays out with is derived from them. */
function labelScreenFontPx(viewName) {
  const typed = num('label-font');
  if (typed !== null && typed >= 6) return Math.min(64, typed);
  const view = views[viewName];
  const rect = view && view.body ? view.body.getBoundingClientRect() : null;
  const short = rect ? Math.max(160, Math.min(rect.width, rect.height)) : 560;
  return Math.max(10, Math.min(28, Math.round(short * 0.021)));
}

/** The same size in the frame's pixels, which is what the server lays out in. */
function labelFrameFontPx(viewName) {
  return Math.max(4, Math.min(200, labelScreenFontPx(viewName) / screenScale(viewName)));
}

/** Colours, stroke and line style, converted to the frame's pixels.
 *
 * Everything the user types is in screen pixels for the same reason the font
 * size is: it is read off the screen.  Dividing by the view's scale puts it in
 * the units the plan is laid out in -- and the export, which draws at the
 * frame's own resolution, can use the same numbers unchanged.
 *
 * The per-class colours ride along here rather than in the plan: which colour a
 * category gets is a presentation decision, so the server never hears about it
 * and restyling costs no round trip.
 */
function labelStyleFor(viewName, frameFactor = 1) {
  const scale = screenScale(viewName) / Math.max(1e-6, frameFactor);
  const mode = $('outline-style').value;
  const strokeWidth = Number($('text-stroke-width').value);
  const markerWidth = Number($('marker-stroke-width').value);
  const outlineWidth = Number($('outline-width').value);
  return {
    outlineWidth: (outlineWidth > 0 ? Math.min(4, outlineWidth) : 1) / scale,
    outlineStyle: mode,
    outlineColor: $('outline-color').value,
    dash: mode === 'dash' ? [6, 4] : mode === 'dot' ? [1.5, 3] : [],
    textColor: $('text-color').value,
    strokeColor: $('text-stroke').value,
    strokeWidth: Math.max(0, Number.isFinite(strokeWidth) ? strokeWidth : 2) / scale,
    // The marker's rim is its own line: the text stroke is about legibility,
    // this one only has to hold the dot apart from the ground under it.
    markerStrokeColor: $('marker-stroke').value,
    markerStrokeWidth: Math.max(0, Math.min(3, Number.isFinite(markerWidth) ? markerWidth : 1)) / scale,
    bold: $('text-bold').checked,
    classColors: CLASS_COLORS,
    // 彩色 on = every label takes its own class's colour; off = one colour for
    // the lot, which is the "one colour" switch the reader reaches for when the
    // palette is in the way.
    uniform: !$('label-colour').checked,
  };
}

/** The colour one item is drawn in: its class, or the single colour asked for. */
function itemColor(item, style) {
  if (style.uniform) return style.textColor;
  return (style.classColors && style.classColors[item.class]) || style.textColor;
}

/** The style a view's labels are painted with, font size included.
 *
 * Unlocked, the plan's own ``font_px`` is the size its boxes were laid out for
 * and nothing else should be used.  Locked, the boxes stay where they are and
 * the type follows the 字号 field: the number is converted to the target frame's
 * pixels the same way the request's own ``font_px`` is (screen pixels over the
 * view's scale, times the frame factor an export is scaling by).
 */
function labelStyleForFrame(viewName, frameFactor = 1) {
  const style = labelStyleFor(viewName, frameFactor);
  if (labelLockOn()) {
    style.fontPx = labelFrameFontPx(viewName) * Math.max(1e-6, frameFactor);
  }
  return style;
}

/** Where a view's picture sits on screen, for mapping image pixels onto it.
 *
 * Measured from the picture's own box rather than recomputed from the zoom and
 * pan, so it holds for both kinds of view: the Moon zooms its canvas inside an
 * untransformed stack, the close-ups zoom the stage itself. */
function imageToScreen(viewName, ix, iy) {
  const view = views[viewName];
  const dpr = window.devicePixelRatio || 1;
  if (!view || !view.canvas || !view.body) return null;
  const body = view.body.getBoundingClientRect();
  const rect = view.canvas.getBoundingClientRect();
  const width = Math.max(1, view.canvas.width);
  const height = Math.max(1, view.canvas.height);
  const scaleX = rect.width / width;
  const scaleY = rect.height / height;
  return {
    x: (rect.left - body.left + ix * scaleX) * dpr,
    y: (rect.top - body.top + iy * scaleY) * dpr,
    scaleX: scaleX * dpr,
    scaleY: scaleY * dpr,
    dpr,
  };
}

/** Size a view's label layer to its body, in device pixels. */
function syncLabelLayer(viewName) {
  const canvas = $(LABEL_LAYERS[viewName]);
  const body = views[viewName] && views[viewName].body;
  if (!canvas || !body) return null;
  const dpr = window.devicePixelRatio || 1;
  const rect = body.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return canvas;
}

/** Paint one plan into a context scaled so that one unit is one frame pixel.
 *
 * Kept free of the DOM on purpose: this is the half of the label drawing that
 * can be exercised in Node, and `web/test_projection.js` does exactly that with
 * a recording context.  *style* is passed in rather than read from the page so
 * the painter stays a function of its arguments.
 */
function paintLabels(ctx, plan, style) {
  if (!ctx || !plan || !plan.items || !plan.items.length) return;
  // The plan carries the size its boxes were laid out for; a *locked* layout is
  // painted at the size the reader is asking for now, so the style may override
  // it.  Everything below that is a length -- markers, leader lines, the stroke
  // an unstyled caller gets -- follows the same number, which is what makes a
  // locked layout grow in type without moving.
  const fontPx = (style && style.fontPx) || plan.font_px || 13;
  const ink = style || {};
  const rim = {
    width: ink.outlineWidth === undefined ? 1 : ink.outlineWidth,
    style: ink.outlineStyle || 'solid',
    color: ink.outlineColor || 'rgba(180, 214, 255, .85)',
    dash: ink.dash || [],
  };
  const textColor = ink.textColor || '#eef2ff';
  const strokeColor = ink.strokeColor || 'rgba(12, 14, 20, .92)';
  const strokeWidth = ink.strokeWidth === undefined ? fontPx * 0.28 : ink.strokeWidth;
  // A marker's rim has its own colour and width.  The fallbacks are the old
  // fixed rims, so a caller that paints without a style still draws what it
  // always did.
  const markerStrokeColor = ink.markerStrokeColor || strokeColor;
  const markerStrokeWidth = ink.markerStrokeWidth === undefined
    ? Math.max(1, fontPx * 0.1) : ink.markerStrokeWidth;
  const marker = Math.max(1.4, fontPx * 0.16);
  // A feature on the night side is not really observable -- it is drawn because
  // it was asked for, and half-faded so it cannot be mistaken for a lit one.
  const alphaFor = (item) => (item.illuminated === false ? 0.5 : 1);
  const strokeRing = (item) => {
    ctx.beginPath();
    item.outline.forEach(([x, y], index) => {
      if (index) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
    });
    ctx.closePath();
    ctx.stroke();
  };
  ctx.save();
  ctx.font = `${ink.bold ? 'bold ' : ''}${fontPx}px "PingFang SC", ` +
    `"Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  // Rims first, so a marker or a name is never hidden under an outline.  The
  // outline is the one thing here that claims a *size*, so it is drawn twice: a
  // dark pass for contrast on bright terrain, a coloured one on top to read
  // against the shadows.  The dark pass is what makes a pale class colour work
  // over a bright highland as well as over a dark mare.
  if (rim.style !== 'none') {
    ctx.lineWidth = Math.max(0.4, rim.width + 1.1);
    ctx.setLineDash((rim.dash || []).map((value) => value * rim.width));
    for (const item of plan.items) {
      if (!item.outline || !item.outline.length) continue;
      ctx.globalAlpha = alphaFor(item);
      ctx.strokeStyle = 'rgba(12, 14, 20, .55)';
      strokeRing(item);
    }
    ctx.lineWidth = Math.max(0.4, rim.width);
    for (const item of plan.items) {
      if (!item.outline || !item.outline.length) continue;
      ctx.globalAlpha = alphaFor(item);
      // The rim carries the class colour too; the picker is the override for
      // when the reader wants one colour for everything.
      ctx.strokeStyle = ink.uniform ? rim.color : itemColor(item, ink);
      strokeRing(item);
    }
    ctx.setLineDash([]);
  }
  for (const item of plan.items) {
    ctx.globalAlpha = alphaFor(item);
    const colour = itemColor(item, ink);
    ctx.beginPath();
    ctx.arc(item.x, item.y, marker, 0, Math.PI * 2);
    ctx.fillStyle = colour;
    ctx.fill();
    if (markerStrokeWidth > 0) {
      ctx.lineWidth = markerStrokeWidth;
      ctx.strokeStyle = markerStrokeColor;
      ctx.stroke();
    }
  }
  for (const item of plan.items) {
    if (!item.labelled) continue;
    ctx.globalAlpha = alphaFor(item);
    const colour = itemColor(item, ink);
    const left = item.label_x;
    const top = item.label_y;
    const right = left + item.label_w;
    const bottom = top + item.label_h;
    const midY = 0.5 * (top + bottom);
    if (item.leader) {
      ctx.beginPath();
      ctx.moveTo(item.x, item.y);
      ctx.lineTo(Math.min(Math.max(item.x, left), right),
                 Math.min(Math.max(item.y, top), bottom));
      ctx.strokeStyle = colour;
      ctx.lineWidth = Math.max(1, fontPx * 0.09);
      ctx.stroke();
    }
    if (strokeWidth > 0) {
      ctx.lineWidth = strokeWidth;
      ctx.strokeStyle = strokeColor;
      ctx.strokeText(item.label, left + 3, midY);
    }
    ctx.fillStyle = colour;
    ctx.fillText(item.label, left + 3, midY);
  }
  ctx.restore();
}

/* ── geometric overlays on the full disk ───────────────────────────────────
 *
 * The named-landform labels need the server's gazetteer; these do not.  The
 * terminator, the limb, the equator, the poles and the two sub-points are all
 * geometry this page already holds -- which is why they are drawn here, can be
 * switched on and off without a round trip, and can be composed into an export
 * at whatever resolution that export is rendered at.
 *
 * Everything is in *frame* pixels, like the labels, so the overlay keeps its
 * proportions to the picture at any size.  A curve is broken where it passes
 * behind the Moon (that part of it is not on the visible hemisphere); a single
 * point that is behind it is drawn at its mirror position through the disk and
 * says so -- the "relative position" of a pole the libration has turned away.
 */

/** The overlay switches, in the order the panel lists them. */
const DISK_MARKS = ['terminator', 'poles', 'limb', 'equator', 'libration',
                    'subsolar', 'nearside', 'ruler'];

const MARK_CURVE = '#8fd0ff';
const MARK_POINT = '#ffd666';

/** How each geometric overlay is drawn: its colour, and whether it is worth a
 *  caption on the picture.
 *
 * One table because three things have to agree -- the line, the caption and the
 * colour the switch turns when it is on -- and the switches are coloured from
 * here rather than from a second list that could drift.
 *
 * ``text: false`` is for the three marks that are their own legend: the shadow
 * band and the limb are unmistakable curves, and the zero point is a dot at the
 * centre of the grid.  Naming them cost a label's worth of clutter over the
 * picture and said nothing the switch had not already said.
 */
const DISK_MARK_STYLES = {
  terminator: { colour: '#ffb454', text: false },
  nearside:   { colour: '#9ece6a', text: true },
  equator:    { colour: '#7aa2f7', text: true },
  limb:       { colour: MARK_CURVE, text: false },
  poles:      { colour: MARK_POINT, text: true },
  libration:  { colour: '#c8a0d4', text: true },
  subsolar:   { colour: '#ff9d7a', text: true },
  ruler:      { colour: '#eef2ff', text: true },
};

/** Whether any of them is on.  Used to decide if an export needs a canvas. */
function marksWanted() {
  return DISK_MARKS.some((name) => {
    const box = $('mark-' + name);
    return !!(box && box.checked);
  });
}

/** The disk's geometry in any frame size: an export is the preview's geometry
 *  measured in its own pixels.  Null before the first render. */
function diskGeomFor(width, height) {
  const g = state.geometry;
  if (!g) return null;
  const w = Math.max(1, width), h = Math.max(1, height);
  return {
    cx: w / 2, cy: h / 2,
    radius: g.disk_radius_fraction * Math.min(w, h),
    subLon: g.libration_lon_deg, subLat: g.libration_lat_deg,
    angularRadiusDeg: g.angular_diameter_deg / 2,
  };
}

/** Frame pixels of a selenographic point, far side included.
 *
 * The renderer's own projection (see :func:`cameraBasis`), minus the refusal: a
 * point behind the limb is drawn at the position of its mirror image through
 * the disk plane, and `far` says that is what happened. */
function projectSelenographic(lonDeg, latDeg, geom) {
  const g = geom || diskGeom();
  const { k, s } = cameraScale(g);
  const { forward, right, up } = cameraBasis(g);
  const n = moonVector(lonDeg, latDeg);
  const depth = dot3(n, forward);
  const far = depth >= 0;
  // A point behind the limb is drawn where its *mirror image through the disk
  // plane* lands: the same place seen from the other side, which is a real
  // point on the near hemisphere and so a real place on the picture.  The
  // mirror is `n - 2 (n . forward) forward`; under the old orthographic mapping
  // it projected to the same pixel as the far point itself, which is why the
  // reflection was invisible there and is not here.
  const shown = far
    ? [n[0] - 2 * depth * forward[0],
       n[1] - 2 * depth * forward[1],
       n[2] - 2 * depth * forward[2]]
    : n;
  const divisor = 1 + k * dot3(shown, forward);
  return {
    x: g.cx + g.radius * s * dot3(shown, right) / divisor,
    y: g.cy - g.radius * s * dot3(shown, up) / divisor,
    far,
  };
}

/** Unit vector in the Moon-fixed frame for a selenographic point. */
function moonVector(lonDeg, latDeg) {
  const lat = latDeg * Math.PI / 180, lon = lonDeg * Math.PI / 180;
  return [Math.cos(lat) * Math.cos(lon), Math.cos(lat) * Math.sin(lon), Math.sin(lat)];
}

function cross3(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function unit3(v) {
  const norm = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / norm, v[1] / norm, v[2] / norm];
}

function lonLatFromVector(v) {
  const u = unit3(v);
  return [Math.atan2(u[1], u[0]) * 180 / Math.PI,
          Math.asin(Math.max(-1, Math.min(1, u[2]))) * 180 / Math.PI];
}

/** Samples of the small circle *distanceDeg* from a selenographic point.
 *
 * 90° is the one every overlay here needs: the terminator is 90° from the
 * sub-solar point, the equator is 90° from the pole, and the near-side boundary
 * is 90° from the zero point of the grid.
 */
function circleFromPole(poleLon, poleLat, distanceDeg, samples = 180) {
  const pole = moonVector(poleLon, poleLat);
  const helper = Math.abs(pole[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const e1 = unit3(cross3(pole, helper));
  const e2 = cross3(pole, e1);
  const turn = distanceDeg * Math.PI / 180;
  const c = Math.cos(turn), s = Math.sin(turn);
  const points = [];
  for (let i = 0; i <= samples; i += 1) {
    const theta = 2 * Math.PI * i / samples;
    const ct = Math.cos(theta), st = Math.sin(theta);
    points.push(lonLatFromVector([
      c * pole[0] + s * (ct * e1[0] + st * e2[0]),
      c * pole[1] + s * (ct * e1[1] + st * e2[1]),
      c * pole[2] + s * (ct * e1[2] + st * e2[2]),
    ]));
  }
  return points;
}

/** The two overlay colours: curves stay cold, points take the page's gold. */


function markFont(ctx, fontPx) {
  ctx.font = `${fontPx}px "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", ` +
    'system-ui, sans-serif';
}

/** Text with a dark stroke, the way the landform labels are drawn. */
function markText(ctx, x, y, text, fontPx, colour, align) {
  ctx.save();
  markFont(ctx, fontPx);
  ctx.textAlign = align || 'left';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = Math.max(2, fontPx * 0.3);
  ctx.strokeStyle = 'rgba(10, 12, 16, .88)';
  ctx.strokeText(text, x, y);
  ctx.fillStyle = colour;
  ctx.fillText(text, x, y);
  ctx.restore();
}

/** Stroke one sampled selenographic curve, broken at the far-side limb. */
function markCurve(ctx, points, geom, options) {
  const o = options || {};
  ctx.save();
  ctx.beginPath();
  let pen = false;
  for (const [lon, lat] of points) {
    const p = projectSelenographic(lon, lat, geom);
    if (p.far) { pen = false; continue; }
    if (pen) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y);
    pen = true;
  }
  ctx.setLineDash(o.dash || []);
  ctx.lineWidth = (o.width || 1.2) + 1.1;
  ctx.strokeStyle = 'rgba(10, 12, 16, .62)';   // contrast pass, over bright terrain
  ctx.stroke();
  ctx.lineWidth = o.width || 1.2;
  ctx.strokeStyle = o.color || MARK_CURVE;
  ctx.stroke();
  ctx.restore();
}

/** One marked point, with its name beside it. */
function markPoint(ctx, lon, lat, text, fontPx, geom, colour) {
  const p = projectSelenographic(lon, lat, geom);
  const radius = Math.max(2, fontPx * 0.3);
  const ink = colour || MARK_POINT;
  ctx.save();
  ctx.beginPath();
  ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
  ctx.lineWidth = Math.max(1, fontPx * 0.13);
  ctx.strokeStyle = ink;
  if (p.far) ctx.setLineDash([fontPx * 0.28, fontPx * 0.24]);
  ctx.stroke();
  ctx.setLineDash([]);
  if (!p.far) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, Math.max(1, radius * 0.34), 0, Math.PI * 2);
    ctx.fillStyle = ink;
    ctx.fill();
  }
  // A point the libration has turned away is drawn at its relative position,
  // and the label has to say so: the ring alone would read as "it is here".
  // *text* may be null: the zero point is a dot and nothing more.
  if (text) {
    markText(ctx, p.x + radius + fontPx * 0.4, p.y,
      p.far ? `${text}（背面）` : text, fontPx, ink);
  }
  ctx.restore();
  return p;
}

/** Where a curve reaches its highest visible point: where its name goes. */
function topOfCurve(points, geom) {
  let best = null;
  for (const [lon, lat] of points) {
    const p = projectSelenographic(lon, lat, geom);
    if (p.far) continue;
    if (!best || p.y < best.y) best = p;
  }
  return best;
}

/** A longitude as it is written on the disk: 6.5°E, 4.7°W. */
function lonText(deg) {
  return `${Math.abs(deg).toFixed(1)}°${deg >= 0 ? 'E' : 'W'}`;
}

function latText(deg) {
  return `${Math.abs(deg).toFixed(1)}°${deg >= 0 ? 'N' : 'S'}`;
}

/** Paint every switched-on overlay of the full disk.
 *
 * *fontPx* is in the frame pixels the context is currently scaled to, so the
 * same call serves the preview and a 6000 px export.
 */
function drawDiskMarks(ctx, fontPx, geom) {
  const g = geom || diskGeom();
  if (!g || !state.geometry) return;
  const on = (name) => {
    const box = $('mark-' + name);
    return !!(box && box.checked);
  };
  if (!DISK_MARKS.some((name) => on(name))) return;
  const font = Math.max(8, Number(fontPx) || 12);
  const view = state.geometry;
  ctx.save();
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  /* The terminator: the Sun's elevation is zero along it, so the shadow band
     of every crater on the far side of it starts here. */
  if (on('terminator')) {
    const points = circleFromPole(view.sub_solar_lon_deg, view.sub_solar_lat_deg, 90);
    markCurve(ctx, points, g, {
      color: DISK_MARK_STYLES.terminator.colour, width: Math.max(1, font * 0.11),
      dash: [font * 0.7, font * 0.45],
    });
  }

  /* The near-side boundary: 90° from the zero point, i.e. the limb as it would
     be with no libration at all.  What shows between this curve and the real
     limb is the far-side ground the libration has just turned into view. */
  if (on('nearside')) {
    const points = circleFromPole(0, 0, 90);
    markCurve(ctx, points, g, {
      color: DISK_MARK_STYLES.nearside.colour, width: Math.max(1, font * 0.1),
      dash: [font * 0.5, font * 0.4],
    });
    const top = topOfCurve(points, g);
    if (top) {
      markText(ctx, top.x + font * 0.4, top.y + font * 1.0, '近地侧边界',
        font * 0.92, DISK_MARK_STYLES.nearside.colour);
    }
  }

  /* The equator, which is also the line the libration in latitude rocks about. */
  if (on('equator')) {
    const points = circleFromPole(0, 90, 90);
    markCurve(ctx, points, g, {
      color: DISK_MARK_STYLES.equator.colour, width: Math.max(1, font * 0.1), dash: [],
    });
    const top = topOfCurve(points, g);
    if (top) {
      markText(ctx, top.x + font * 0.5, top.y, '赤道', font * 0.92,
        DISK_MARK_STYLES.equator.colour);
    }
  }

  /* The limb: the circle the disk actually ends at. */
  if (on('limb')) {
    ctx.save();
    ctx.beginPath();
    ctx.arc(g.cx, g.cy, g.radius, 0, Math.PI * 2);
    ctx.setLineDash([font * 0.5, font * 0.4]);
    ctx.lineWidth = Math.max(1, font * 0.11) + 1.1;
    ctx.strokeStyle = 'rgba(10, 12, 16, .62)';
    ctx.stroke();
    ctx.lineWidth = Math.max(1, font * 0.11);
    ctx.strokeStyle = MARK_CURVE;
    ctx.stroke();
    ctx.restore();
  }

  /* The two ends of the axis, each drawn where it is -- or, when the libration
     has tipped it behind the limb, where it would be seen through the disk. */
  if (on('poles')) {
    markPoint(ctx, 0, 90, '北极 N', font * 0.92, g);
    markPoint(ctx, 0, -90, '南极 S', font * 0.92, g);
  }

  /* The zero point of the grid and the libration point: the first is fixed on
     the Moon, the second is whatever the Moon happens to be showing us, and
     the distance between them on the picture *is* the libration.  When the
     libration is nearly zero they are the same place, and two labels on top of
     each other would be worse than one. */
  if (on('libration')) {
    const zero = projectSelenographic(0, 0, g);
    const together = Math.hypot(zero.x - g.cx, zero.y - g.cy) <= font * 1.2;
    const where = `${lonText(g.subLon)} / ${latText(g.subLat)}`;
    // The zero point is a dot: it is the origin the other marks are read
    // against, and "零点 0°/0°" told the reader nothing they could not see.
    // The libration point keeps its caption, because *where* it is is the whole
    // content of that mark.
    markPoint(ctx, 0, 0, null, font * 0.92, g, DISK_MARK_STYLES.libration.colour);
    // Name only: the numbers were a readout, and the readout is on the left of
    // the page where it can be read without hunting for it on a picture.
    if (together) {
      markText(ctx, g.cx + font * 0.6, g.cy - font * 0.7, '天平动点',
        font * 0.92, MARK_POINT);
    } else {
      markPoint(ctx, g.subLon, g.subLat, '天平动点', font * 0.92, g);
    }
  }

  /* The day point: where the Sun is overhead.  Its distance from the centre of
     the disk is the phase geometry the picture is lit by. */
  if (on('subsolar')) {
    markPoint(ctx, view.sub_solar_lon_deg, view.sub_solar_lat_deg,
      '日下点', font * 0.92, g, DISK_MARK_STYLES.subsolar.colour);
  }

  /* The ruler.  Drawn here rather than on the label layer in screen pixels,
     because it is part of the *picture*: an export at 6000 px has to carry it
     at the same place and the same length, which only works in frame pixels. */
  drawRuler(ctx, font, g);

  ctx.restore();
}

/* ── orientation of a labelled frame ───────────────────────────────────────
 *
 * A mirror is applied to the *plan* rather than to the canvas, because canvas
 * would mirror the type with it and a mirrored name is unreadable.  Mirroring
 * the anchor instead puts the box where the feature now is and leaves the text
 * the right way round -- what every map does with a reversed image.
 *
 * Rotation needs no such trick: turning a picture turns its labels with it, so
 * that one is a canvas rotation at draw time (see drawViewLabels), which also
 * carries the rims and the leaders along.
 */
function mirroredPlan(plan, mode) {
  if (!plan || (mode !== 'horizontal' && mode !== 'vertical')) return plan;
  const w = plan.frame.width;
  const h = plan.frame.height;
  const flip = (x, y) => (mode === 'horizontal' ? [w - x, y] : [x, h - y]);
  const items = plan.items.map((item) => {
    const [x, y] = flip(item.x, item.y);
    return {
      ...item,
      x,
      y,
      label_x: item.label_x === null ? null
        : (mode === 'horizontal' ? w - (item.label_x + item.label_w) : item.label_x),
      label_y: item.label_y === null ? null
        : (mode === 'vertical' ? h - (item.label_y + item.label_h) : item.label_y),
      outline: item.outline ? item.outline.map(([ox, oy]) => flip(ox, oy)) : null,
    };
  });
  return { ...plan, items };
}

/** Orientation **of the processed close-up**, in degrees and a mode.
 *
 * The geometry pane is the frame as it was marched: turning it would mean the
 * two panes no longer show the same pixels, and its labels are laid out in the
 * server's own frame.  So the rotation and the mirror belong to 后期处理
 * alone -- the server skips them for the raw half, and the page does not turn
 * or mirror the labels of the geometry pane either. */
function frameOrientation() {
  return {
    rotation: (Number($('image-rotation').value) || 0) * Math.PI / 180,
    mirror: mirrorMode(),
  };
}

/** The plan a view shows: the close-ups share the region's answer.
 *
 *  Two things are layered on the server's plan here rather than asked of it.
 *  The reader's own nudges (a name dragged to where it belongs on *this* frame)
 *  and the names they have put away are properties of the picture on screen --
 *  the gazetteer has not moved -- so they are applied to the plan as it is
 *  drawn, and a re-render keeps them by name.
 */
function planFor(viewName) {
  const plan = applyLabelEdits(state.labels[viewName === 'disk' ? 'disk' : 'region'],
                              viewName);
  // Only 后期处理 carries the orientation; 高分辨率模拟 is the frame itself.
  if (!plan || viewName !== 'proc') return plan;
  return mirroredPlan(plan, frameOrientation().mirror);
}

/** The close-ups' own 地貌 switch: the checkbox in the save row.
 *
 *  It is one switch with one meaning -- "the landform names belong in this
 *  picture" -- so unticking it takes them off 几何模拟 and 后期成片 as well as
 *  out of the saved file.  The full disk has its own switch and its own export,
 *  and neither touches the other. */
function regionLabelsWanted(viewName) {
  if (viewName === 'disk') return true;
  const box = $('download-annotated');
  return !box || box.checked;
}

/** Redraw one view's labels at the resolution it is being looked at.
 *
 * The full disk carries the geometric overlays as well; they need no plan, so
 * they are drawn even when the landform names are switched off. */
function drawViewLabels(viewName) {
  const canvas = syncLabelLayer(viewName);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const plan = regionLabelsWanted(viewName) ? planFor(viewName) : null;
  const marks = viewName === 'disk' && marksWanted() && state.geometry;
  const hasNames = !!(plan && plan.items && plan.items.length);
  if (!hasNames && !marks) return;
  const frame = (plan && plan.frame) || { width: DISK.width, height: DISK.height };
  const anchor = imageToScreen(viewName, 0, 0);
  const corner = imageToScreen(viewName, frame.width, frame.height);
  if (!anchor || !corner) return;
  const scaleX = (corner.x - anchor.x) / Math.max(1, frame.width);
  const scaleY = (corner.y - anchor.y) / Math.max(1, frame.height);
  if (!(scaleX > 0) || !(scaleY > 0)) return;
  // One setTransform does the whole mapping, so the painter below can work in
  // frame pixels and the type still lands on device pixels at its true size.
  ctx.setTransform(scaleX, 0, 0, scaleY, anchor.x, anchor.y);
  if (viewName === 'proc') {
    const turn = frameOrientation().rotation;
    if (turn) {
      // The same pivot the server rotates the picture about: the middle of the
      // pixel grid, (n-1)/2, not the n/2 boundary between two middle pixels.
      const cx = 0.5 * (frame.width - 1), cy = 0.5 * (frame.height - 1);
      ctx.translate(cx, cy);
      ctx.rotate(-turn);        // canvas y runs down, so a screen turn is negated
      ctx.translate(-cx, -cy);
    }
  }
  if (hasNames) paintLabels(ctx, plan, labelStyleForFrame(viewName));
  if (marks) {
    // The overlay is sized like the labels: the plan's own frame-pixel font
    // when there is one, and what the window would ask for when there is not.
    const fontPx = plan && plan.font_px ? plan.font_px : labelFrameFontPx('disk');
    drawDiskMarks(ctx, fontPx, diskGeomFor(frame.width, frame.height));
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  // The handles of whatever is selected -- the ruler's cross and a nudged
  // label's -- are drawn in *device* pixels, last, so they stay one size
  // however far the picture is zoomed.
  drawSelectionHandles(ctx, viewName);
}

/** Redraw every label layer; called whenever a picture moves or zooms. */
function drawAllLabels() {
  for (const name of Object.keys(views)) drawViewLabels(name);
}

/** Redraw one view and whichever other view shows the same frame.
 *
 *  The two close-ups are one camera, so a name nudged on 几何模拟 has to move on
 *  后期成片 as well -- but the full disk has nothing to do with it, and a drag
 *  that repainted all three layers sixty times a second would be paying for a
 *  picture nobody is looking at. */
function drawViewAndTwin(viewName) {
  drawViewLabels(viewName);
  const twin = views[viewName] && views[viewName].link;
  if (twin) drawViewLabels(twin);
}

/* ── the reader's own edits to a picture's annotations ─────────────────────
 *
 * Two of them, and both are properties of the *picture* rather than of the
 * catalogue: a name nudged to where it reads better on this frame, and a name
 * put away because it is in the way.  The server's plan is a layout, not a
 * verdict, so the edits live here, keyed by the feature's own name, and are
 * applied as the plan is drawn -- which is what makes them survive a re-render,
 * a change of language and an export at another resolution.
 */
const labelEdits = { disk: {}, region: {} };        // key -> {dx, dy}, frame px
const labelHidden = { disk: new Set(), region: new Set() };
/* key -> the class the name was wearing when it was edited.  Kept beside the
   edit because the *plan* is not always there to be asked: a name nudged on a
   full disk is not in a close-up's plan, and a class switch has to be able to
   put its own names back without the frame they were moved on. */
const labelClass = { disk: {}, region: {} };

/** What an edit is keyed by: the IAU name, not the caption.
 *
 * The caption is translated, so keying on it would lose every nudge the moment
 * the reader switched the language, and two classes can share a short name. */
function labelKey(item) {
  return item.name_en || item.name_zh || item.label || `${item.x},${item.y}`;
}

function labelStore(viewName) {
  return viewName === 'disk' ? 'disk' : 'region';
}

/** Remember which class a nudge or a hide belonged to. */
function noteLabelClass(viewName, key, className) {
  const store = labelStore(viewName);
  if (className) labelClass[store][key] = className;
}

/** Forget every manual edit of one class, in both pictures.
 *
 * A class switch is also a "start again" for that family: switching a family
 * off and back on is the reader saying "I want to see *these* again", and
 * handing back the positions they had dragged around, and the names they had
 * put away, answers a different question.  Returns how many edits went.
 */
function resetLabelClass(className) {
  if (!className) return 0;
  let count = 0;
  for (const store of ['disk', 'region']) {
    for (const [key, value] of Object.entries(labelClass[store])) {
      if (value !== className) continue;
      delete labelClass[store][key];
      if (labelEdits[store][key]) { delete labelEdits[store][key]; count += 1; }
      if (labelHidden[store].delete(key)) count += 1;
    }
  }
  return count;
}

/** Every manual edit, in both pictures -- what 标注设置复位 throws away.
 *
 * The names and their positions are the reader's own work, and until now the
 * only way to get a label back was to remember which one had been dragged and
 * drag it roughly back.  A reset button that leaves the edits behind is not a
 * reset button.
 */
function resetAllLabelEdits() {
  let count = 0;
  for (const store of ['disk', 'region']) {
    count += Object.keys(labelEdits[store]).length + labelHidden[store].size;
    labelEdits[store] = {};
    labelHidden[store] = new Set();
    labelClass[store] = {};
  }
  // 标尺 is the same kind of thing: a mark the reader put somewhere by hand.
  resetRuler();
  return count;
}

/** A plan with the reader's nudges applied and their hidden names dropped. */
function applyLabelEdits(plan, viewName) {
  if (!plan || !plan.items || !plan.items.length) return plan;
  const store = labelStore(viewName);
  const edits = labelEdits[store];
  const hidden = labelHidden[store];
  if (!Object.keys(edits).length && !hidden.size) return plan;
  const items = [];
  for (const item of plan.items) {
    const key = labelKey(item);
    if (hidden.has(key)) continue;
    const edit = edits[key];
    if (!edit || (!edit.dx && !edit.dy)) { items.push(item); continue; }
    items.push({
      ...item,
      x: item.x + edit.dx,
      y: item.y + edit.dy,
      label_x: item.label_x === null || item.label_x === undefined
        ? item.label_x : item.label_x + edit.dx,
      label_y: item.label_y === null || item.label_y === undefined
        ? item.label_y : item.label_y + edit.dy,
      outline: item.outline
        ? item.outline.map(([x, y]) => [x + edit.dx, y + edit.dy])
        : item.outline,
    });
  }
  return { ...plan, items };
}

/** frame pixels -> device pixels, including 后期成片's own rotation.
 *
 * The rotation is applied to the canvas when the names are painted, so a hit
 * test that ignored it would grab the wrong label on a turned picture -- and
 * only there, which is the sort of bug that survives a casual look. */
function frameToDevice(viewName, map, frame, fx, fy) {
  let x = fx, y = fy;
  if (viewName === 'proc') {
    const turn = frameOrientation().rotation;
    if (turn) {
      const cx = 0.5 * (frame.width - 1), cy = 0.5 * (frame.height - 1);
      const cos = Math.cos(-turn), sin = Math.sin(-turn);
      const dx = x - cx, dy = y - cy;
      x = cx + dx * cos - dy * sin;
      y = cy + dx * sin + dy * cos;
    }
  }
  return { x: map.x + x * map.scaleX, y: map.y + y * map.scaleY };
}

/** A device-pixel move, back in the *plan's* own axes.
 *
 * The frame relations the plan is drawn through are applied here in reverse,
 * and there is one subtlety in that: the mirror is applied to the *plan*, so
 * `planFor` hands back coordinates that are already mirrored -- which is why
 * :func:`frameToDevice` needs only the rotation -- while an edit is stored
 * against the unmirrored plan (`applyLabelEdits` runs before `mirroredPlan`).
 * A drag therefore has to be unmirrored and a hit test must not be, and getting
 * that backwards moves the name the other way on 后期成片 only. */
function deviceToFrameDelta(viewName, map, dx, dy) {
  let fx = dx / Math.max(1e-6, map.scaleX);
  let fy = dy / Math.max(1e-6, map.scaleY);
  if (viewName === 'proc') {
    const orientation = frameOrientation();
    if (orientation.rotation) {
      const cos = Math.cos(orientation.rotation);
      const sin = Math.sin(orientation.rotation);
      const x = fx * cos - fy * sin;
      const y = fx * sin + fy * cos;
      fx = x; fy = y;
    }
    if (orientation.mirror === 'horizontal') fx = -fx;
    else if (orientation.mirror === 'vertical') fy = -fy;
  }
  return { dx: fx, dy: fy };
}

/** Where the pointer is, in the label layer's own device pixels. */
function overlayPoint(viewName, event) {
  const view = views[viewName];
  if (!view || !view.body) return null;
  const rect = view.body.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  return {
    x: (event.clientX - rect.left) * dpr,
    y: (event.clientY - rect.top) * dpr,
    dpr,
  };
}

/* The × a selected thing carries.  One size on screen whatever the zoom: a
   handle that scaled with the picture would be unusable at 8×.
 *
 * Its disc is deliberately see-through: at .82 it read as a hole punched in the
 * Moon, which is the one thing a control that closes a *label* should not look
 * like.  It only has to make the two strokes legible over both a bright mare
 * and a shadowed floor, and the pink ring does that on its own -- so what is
 * left is a 45% wash dark enough for contrast and light enough to keep whatever
 * is under it readable. */
const HANDLE_BADGE_PX = 9;
const BADGE_FILL = 'rgba(12, 14, 20, .45)';

/** The ruler: a picture element with a place, a length and a switch.
 *
 * It used to be a fixed caption in the bottom-left corner: a number the reader
 * could not move out of the way of the very limb they were measuring, and one
 * whose length was chosen for them.  It is now an object on the frame -- drag
 * it, drag either end -- and its *position* is a fraction of the frame so an
 * export at another size puts it exactly where the preview shows it. */
const ruler = {
  x: 0.06,        // left end, as a fraction of the frame's width
  y: 0.94,        // the bar's own height, as a fraction of the frame's height
  km: 0,          // length in kilometres; chosen the first time it is switched on
};

/** Put the ruler back in the corner it starts in, at the length the frame asks
 *  for.  Called by 标注设置复位: the bar is dragged by hand, so its place is an
 *  edit like any other, and a reset that left it wherever it was left is not
 *  one. */
function resetRuler() {
  ruler.x = 0.06;
  ruler.y = 0.94;
  ruler.km = 0;
  ruler.selected = false;
}

function rulerOn() {
  const box = $('mark-ruler');
  return !!(box && box.checked);
}

/** Kilometres of Moon per pixel of a frame with this geometry.
 *
 * Measured from the geometry rather than from the picture on screen, because an
 * export is composed at another size and the ruler has to measure the same Moon
 * there. */
function diskKmPerFramePixel(geom) {
  const g = state.geometry;
  if (!g || !geom || !(geom.radius > 0)) return null;
  const st = (g.report || {}).state || {};
  const diameterKm = st.moon_radius_km
    ? st.moon_radius_km * 2
    : (g.angular_diameter_deg * Math.PI / 180) * g.distance_km;
  return diameterKm / (2 * geom.radius);
}

/** The bar's place and length, in frame pixels. */
function rulerGeometry(geom, frameWidth, frameHeight) {
  const kmPerPx = diskKmPerFramePixel(geom);
  if (!kmPerPx || !(ruler.km > 0)) return null;
  const width = Math.max(1, frameWidth || 2 * geom.cx);
  const height = Math.max(1, frameHeight || 2 * geom.cy);
  const left = ruler.x * width;
  const top = ruler.y * height;
  const length = Math.max(8, ruler.km / kmPerPx);
  return { left, top, length, kmPerPx, right: left + length, width, height, geom };
}

/** A round length that fills about a tenth of the frame. */
function defaultRulerKm(geom, frameWidth) {
  const kmPerPx = diskKmPerFramePixel(geom);
  if (!kmPerPx) return 100;
  const want = 0.12 * Math.max(1, frameWidth) * kmPerPx;
  const power = Math.pow(10, Math.floor(Math.log10(Math.max(want, 1e-6))));
  for (const step of [1, 2, 5, 10]) {
    if (step * power >= want * 0.75) return step * power;
  }
  return 10 * power;
}

function rulerText(km) {
  if (!(km > 0)) return '—';
  if (km >= 100) return `${Math.round(km)} km`;
  if (km >= 10) return `${km.toFixed(1)} km`;
  if (km >= 1) return `${km.toFixed(2)} km`;
  return `${Math.round(km * 1000)} m`;
}

function drawRuler(ctx, fontPx, geom) {
  if (!rulerOn() || !state.geometry) return;
  const frameWidth = 2 * geom.cx;
  const frameHeight = 2 * geom.cy;
  if (!(ruler.km > 0)) ruler.km = defaultRulerKm(geom, frameWidth);
  const bar = rulerGeometry(geom, frameWidth, frameHeight);
  if (!bar) return;
  const font = Math.max(8, Number(fontPx) || 12);
  const tick = font * 0.5;
  ctx.save();
  ctx.lineCap = 'butt';
  for (const pass of [
    { colour: 'rgba(10, 12, 16, .66)', width: Math.max(1, font * 0.12) + 1.6 },
    { colour: '#eef2ff', width: Math.max(1, font * 0.12) },
  ]) {
    ctx.strokeStyle = pass.colour;
    ctx.lineWidth = pass.width;
    ctx.beginPath();
    ctx.moveTo(bar.left, bar.top - tick);
    ctx.lineTo(bar.left, bar.top + tick);
    ctx.moveTo(bar.left, bar.top);
    ctx.lineTo(bar.right, bar.top);
    ctx.moveTo(bar.right, bar.top - tick);
    ctx.lineTo(bar.right, bar.top + tick);
    ctx.stroke();
  }
  markText(ctx, bar.left, bar.top - tick - font * 0.8, rulerText(ruler.km),
    font * 0.92, '#eef2ff', 'left');
  ctx.restore();
}

/** Which label is under *point*, latest drawn first (that is what is on top). */
function labelHitTest(viewName, point) {
  if (!regionLabelsWanted(viewName)) return null;
  const plan = planFor(viewName);
  const map = imageToScreen(viewName, 0, 0);
  if (!plan || !plan.items || !plan.items.length || !map) return null;
  const frame = plan.frame || { width: DISK.width, height: DISK.height };
  const slack = 5 * point.dpr;
  // The marker's radius follows the font it was painted at, so the hit test has
  // to ask the same question the painter did -- a locked layout is painted at
  // the size the field says, not the one its boxes were laid out for.
  const fontPx = labelLockOn()
    ? labelFrameFontPx(viewName) : (plan.font_px || 13);
  const marker = Math.max(1.4, fontPx * 0.16) * Math.max(map.scaleX, map.scaleY);
  for (let index = plan.items.length - 1; index >= 0; index -= 1) {
    const item = plan.items[index];
    const at = frameToDevice(viewName, map, frame, item.x, item.y);
    if (Math.hypot(point.x - at.x, point.y - at.y) <= marker + slack) return item;
    if (item.label_x === null || item.label_x === undefined) continue;
    const corner = frameToDevice(viewName, map, frame, item.label_x, item.label_y);
    const width = item.label_w * map.scaleX;
    const height = item.label_h * map.scaleY;
    if (point.x >= corner.x - slack && point.x <= corner.x + width + slack
        && point.y >= corner.y - slack && point.y <= corner.y + height + slack) {
      return item;
    }
  }
  return null;
}

/** Where a selected label's × sits, in device pixels. */
function labelBadge(viewName, item) {
  const plan = planFor(viewName);
  const map = imageToScreen(viewName, 0, 0);
  if (!plan || !map) return null;
  const frame = plan.frame || { width: DISK.width, height: DISK.height };
  const dpr = window.devicePixelRatio || 1;
  const gap = (HANDLE_BADGE_PX + 5) * dpr;
  const anchor = item.label_x === null || item.label_x === undefined
    ? { x: item.x, y: item.y }
    : { x: item.label_x + item.label_w, y: item.label_y };
  const at = frameToDevice(viewName, map, frame, anchor.x, anchor.y);
  return { x: at.x + gap, y: at.y - gap, radius: HANDLE_BADGE_PX * dpr };
}

/** Where the selected ruler's × sits, in device pixels. */
function rulerBadge() {
  if (!state.geometry) return null;
  const geom = diskGeom();
  const bar = rulerGeometry(geom, DISK.width, DISK.height);
  const map = imageToScreen('disk', 0, 0);
  if (!bar || !map) return null;
  const dpr = window.devicePixelRatio || 1;
  const gap = (HANDLE_BADGE_PX + 5) * dpr;
  const frame = { width: DISK.width, height: DISK.height };
  const at = frameToDevice('disk', map, frame, bar.right, bar.top);
  return { x: at.x + gap, y: at.y - gap, radius: HANDLE_BADGE_PX * dpr,
           kind: 'ruler' };
}

/** The × buttons drawn on top of a label layer, one per selected thing. */
function drawSelectionHandles(ctx, viewName) {
  const badges = [];
  if (viewName === 'disk' && ruler.selected && rulerOn()) {
    const badge = rulerBadge();
    if (badge) badges.push(badge);
  }
  const chosen = overlay.selected;
  if (chosen && chosen.kind === 'label' && chosen.view === viewName) {
    const plan = planFor(viewName);
    const item = plan && plan.items
      ? plan.items.find((entry) => labelKey(entry) === chosen.key) : null;
    if (item) {
      const badge = labelBadge(viewName, item);
      if (badge) badges.push(badge);
    }
  }
  for (const badge of badges) {
    ctx.save();
    ctx.beginPath();
    ctx.arc(badge.x, badge.y, badge.radius, 0, Math.PI * 2);
    ctx.fillStyle = BADGE_FILL;
    ctx.fill();
    ctx.lineWidth = Math.max(1, badge.radius * 0.16);
    ctx.strokeStyle = '#ff8a80';
    ctx.stroke();
    const arm = badge.radius * 0.44;
    ctx.beginPath();
    ctx.moveTo(badge.x - arm, badge.y - arm);
    ctx.lineTo(badge.x + arm, badge.y + arm);
    ctx.moveTo(badge.x + arm, badge.y - arm);
    ctx.lineTo(badge.x - arm, badge.y + arm);
    ctx.strokeStyle = '#ffd9d6';
    ctx.lineWidth = Math.max(1.2, badge.radius * 0.24);
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.restore();
  }
}

/* Which thing on a picture is selected, and the drag in progress.  One at a
   time: the × belongs to the thing the reader just touched. */
const overlay = { selected: null, drag: null };

/* Selecting is not a toggle: the same press that selects a thing also starts
   dragging it, so "press again to deselect" would take the × away halfway
   through a nudge.  Empty picture deselects. */
function selectOverlay(what) {
  overlay.selected = what;
}

/** The ruler's grab targets, in device pixels: either end, or the bar. */
function rulerHitTest(point) {
  if (!rulerOn() || !state.geometry) return null;
  const geom = diskGeom();
  const bar = rulerGeometry(geom, DISK.width, DISK.height);
  const map = imageToScreen('disk', 0, 0);
  if (!bar || !map) return null;
  const frame = { width: DISK.width, height: DISK.height };
  const left = frameToDevice('disk', map, frame, bar.left, bar.top);
  const right = frameToDevice('disk', map, frame, bar.right, bar.top);
  const grab = 9 * point.dpr;
  if (Math.hypot(point.x - left.x, point.y - left.y) <= grab) return 'ruler-left';
  if (Math.hypot(point.x - right.x, point.y - right.y) <= grab) return 'ruler-right';
  const spanX = right.x - left.x, spanY = right.y - left.y;
  const lengthSq = Math.max(1e-6, spanX * spanX + spanY * spanY);
  const t = Math.max(0, Math.min(1,
    ((point.x - left.x) * spanX + (point.y - left.y) * spanY) / lengthSq));
  const near = Math.hypot(point.x - (left.x + t * spanX), point.y - (left.y + t * spanY));
  return near <= grab ? 'ruler-move' : null;
}

function overlayDown(viewName, event) {
  const point = overlayPoint(viewName, event);
  if (!point) return false;
  // A press on a × takes the thing away; it does not start a drag.  The
  // ruler's × only exists on the full disk, where the ruler is.
  const badges = viewName === 'disk'
    ? [rulerBadge(), labelBadgeFor(viewName)]
    : [labelBadgeFor(viewName)];
  for (const badge of badges) {
    if (!badge) continue;
    if (Math.hypot(point.x - badge.x, point.y - badge.y) <= badge.radius + 3 * point.dpr) {
      hideBadgeTarget(badge.kind, badge);
      return true;
    }
  }
  const rulerHit = viewName === 'disk' ? rulerHitTest(point) : null;
  if (rulerHit) {
    const bar = rulerGeometry(diskGeom(), DISK.width, DISK.height);
    selectOverlay({ kind: 'ruler' });
    ruler.selected = true;
    overlay.drag = {
      kind: 'ruler', mode: rulerHit, startKm: ruler.km, startX: ruler.x,
      startY: ruler.y, start: point, bar,
    };
    drawDisk();
    return true;
  }
  const item = labelHitTest(viewName, point);
  if (item) {
    const key = labelKey(item);
    selectOverlay({ kind: 'label', view: viewName, key });
    noteLabelClass(viewName, key, item.class);
    overlay.drag = {
      kind: 'label', view: viewName, key, start: point,
      edit: { ...(labelEdits[labelStore(viewName)][key] || { dx: 0, dy: 0 }) },
    };
    drawAllLabels();
    return true;
  }
  // A press on empty picture puts the × away again.
  if (overlay.selected) {
    overlay.selected = null;
    ruler.selected = false;
    drawAllLabels();
  }
  return false;
}

/** The × of the selected label of this view, tagged with what it removes. */
function labelBadgeFor(viewName) {
  const chosen = overlay.selected;
  if (!chosen || chosen.kind !== 'label' || chosen.view !== viewName) return null;
  const plan = planFor(viewName);
  const item = plan && plan.items
    ? plan.items.find((entry) => labelKey(entry) === chosen.key) : null;
  if (!item) return null;
  const badge = labelBadge(viewName, item);
  return badge ? { ...badge, kind: 'label', view: viewName, key: chosen.key } : null;
}

function hideBadgeTarget(kind, badge) {
  if (kind === 'ruler') {
    const box = $('mark-ruler');
    if (box) box.checked = false;
    ruler.selected = false;
    overlay.selected = null;
    syncMarkChips();
    drawAllLabels();
    setStatus('标尺已隐藏');
    return;
  }
  const store = labelStore(badge.view);
  const plan = planFor(badge.view);
  const item = plan && plan.items
    ? plan.items.find((entry) => labelKey(entry) === badge.key) : null;
  noteLabelClass(badge.view, badge.key, item && item.class);
  labelHidden[store].add(badge.key);
  overlay.selected = null;
  drawAllLabels();
  setStatus('这一条地貌标注已临时隐藏（重新排版、关掉它的分类或点「复位」可恢复）');
}

function overlayMove(event) {
  const drag = overlay.drag;
  if (!drag) return false;
  const point = overlayPoint(drag.kind === 'ruler' ? 'disk' : drag.view, event);
  if (!point) return false;
  const map = imageToScreen(drag.kind === 'ruler' ? 'disk' : drag.view, 0, 0);
  if (!map) return false;
  const frame = drag.kind === 'ruler'
    ? { width: DISK.width, height: DISK.height }
    : (planFor(drag.view) || {}).frame || { width: DISK.width, height: DISK.height };
  const delta = deviceToFrameDelta(
    drag.kind === 'ruler' ? 'disk' : drag.view,
    map, point.x - drag.start.x, point.y - drag.start.y);
  if (drag.kind === 'ruler') {
    const bar = drag.bar;
    if (!bar) return true;
    if (drag.mode === 'ruler-move') {
      ruler.x = Math.min(1, Math.max(0, drag.startX + delta.dx / bar.width));
      ruler.y = Math.min(1, Math.max(0, drag.startY + delta.dy / bar.height));
    } else if (drag.mode === 'ruler-right') {
      const km = (bar.length + delta.dx) * bar.kmPerPx;
      ruler.km = Math.max(bar.kmPerPx * 8, km);
    } else {
      // The left end moves and the right end stays: the bar keeps its far tip,
      // which is what makes a two-ended drag feel like a measurement.
      const km = bar.length - delta.dx;
      ruler.km = Math.max(bar.kmPerPx * 8, km * bar.kmPerPx);
      ruler.x = Math.min(1, Math.max(0, drag.startX + (delta.dx / bar.width)));
    }
    drawAllLabels();
    return true;
  }
  const store = labelStore(drag.view);
  labelEdits[store][drag.key] = {
    dx: drag.edit.dx + delta.dx,
    dy: drag.edit.dy + delta.dy,
  };
  drawViewAndTwin(drag.view);
  return true;
}

function overlayUp() {
  if (!overlay.drag) return false;
  const drag = overlay.drag;
  overlay.drag = null;
  if (drag.kind === 'ruler') {
    setStatus(`标尺：${rulerText(ruler.km)}（拖动可改位置与长度）`);
  } else {
    setStatus('地貌标注已微调位置（重新排版后仍按名字保留）');
  }
  return true;
}

/** Draw a picture plus a plan into a canvas at an explicit size.
 *
 * One function for both exports, because the two differ only in where the
 * numbers come from: the close-ups render at one size and are saved at another
 * (so the canvas scales the picture), while the full disk is rendered at the
 * size it is saved at (so the scale is 1 and the *style* carries the ratio
 * instead).  The labels go on after the picture is at its final size, which is
 * what keeps their proportions right at any of them.
 *
 * *marks* is the full disk's geometric overlay ({fontPx} when it is wanted):
 * it is drawn in the same frame pixels as the plan, so an export at 6000 px
 * carries the same overlay at the same relative size as the preview.
 *
 * *smooth* is 边缘平滑 in the render's own pixels, applied to the picture
 * before it is scaled and before anything is drawn on it: the aliasing it
 * removes is in the render, and the names must not be caught by it.
 */
function composeImage(source, width, height, plan, style, orientation, marks, lens,
                      smooth = 0) {
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width));
  canvas.height = Math.max(1, Math.round(height));
  const ctx = canvas.getContext('2d');
  const picture = smoothImage(source, smooth);
  const scale = canvas.width / Math.max(1, picture.width);
  ctx.imageSmoothingEnabled = scale < 1;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(picture, 0, 0, picture.width, picture.height, 0, 0,
    canvas.width, canvas.height);
  const hasPlan = plan && plan.items && plan.items.length;
  // *lens* is the eyepiece: applied last, over the picture and over the names,
  // because it is the glass the reader is looking through.  The full disk has
  // none -- nobody holds an eyepiece up to the whole Moon.
  if (!hasPlan && !marks) return lens ? applyEyepiece(canvas) : canvas;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  if (hasPlan) {
    const turned = orientation && orientation.mirror
      ? mirroredPlan(plan, orientation.mirror) : plan;
    if (turned !== plan) plan = turned;
    if (orientation && orientation.rotation) {
      const cx = 0.5 * (plan.frame.width - 1), cy = 0.5 * (plan.frame.height - 1);
      ctx.translate(cx, cy);
      ctx.rotate(-orientation.rotation);
      ctx.translate(-cx, -cy);
    }
    paintLabels(ctx, plan, style);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
  }
  if (marks) {
    drawDiskMarks(ctx, marks.fontPx || 12, diskGeomFor(canvas.width, canvas.height));
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  return lens ? applyEyepiece(canvas) : canvas;
}

/** Encode a finished canvas in one of the three formats the page offers. */
function canvasBlob(canvas, format) {
  if (format.type === 'image/tiff') {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    return new Blob([encodeTiff(data, canvas.width, canvas.height)], { type: format.type });
  }
  return dataUrlToBlob(canvas.toDataURL(format.type, 0.95));
}

/** Hand a URL to the browser as a download. */
function downloadUrl(url, name) {
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
}

/** The file the save button hands over: the picture at the save size, in the
 *  chosen format, with the labels drawn when they are wanted.
 *
 * The render is one resolution and the *saved* file is another, on purpose: the
 * output pane says how much Moon to compute, and the save controls say how big
 * the file should be, so a 1200 px render can leave as a 2400 px PNG.  The
 * scaling happens here, once, on the finished picture -- it costs nothing
 * compared with the ray march and never changes what was computed.
 *
 * The plan is already in the frame's pixels and the style has been converted to
 * them, so painting after the same scale keeps the labels proportional to the
 * picture at any size.
 */
function savedImageBlob(target, callback) {
  const view = target === 'proc' ? 'proc' : target === 'raw' ? 'raw' : 'disk';
  const source = target === 'disk' ? state.diskImage
    : target === 'raw' ? state.rawImage : state.resultImage;
  if (!source) { callback(null, null); return; }
  const format = SAVE_FORMATS[$('save-format').value] || SAVE_FORMATS.png;
  const wanted = target === 'disk' ? source : outputPixels();
  const wantedLabels = target === 'disk' ? $('disk-annotated').checked : $('download-annotated').checked;
  const annotate = wantedLabels && state.labelsOn;
  const plan = annotate ? planFor(view) : null;
  const marks = target === 'disk' && wantedLabels && marksWanted()
    ? { fontPx: plan && plan.font_px ? plan.font_px : 12 } : null;
  const canvas = composeImage(source, wanted.width, wanted.height, plan,
    labelStyleForFrame(view), target === 'proc' ? frameOrientation() : null,
    marks, target === 'proc');
  callback(canvasBlob(canvas, format), format);
}

/** Which radiance the full disk is saved as.
 *
 *  The old control was a three-way 分量 switch (完整 / 地照 / 阳光); the one the
 *  reader actually reaches for is whether the earthshine is in the file, so it
 *  is a checkbox: ticked = the whole picture, unticked = the sunlit half only,
 *  each exposed over its own range (the two are twelve magnitudes apart).
 */
function diskComponent() {
  const box = $('disk-earthshine');
  return box && !box.checked ? 'sunlit' : 'image';
}

/** 保存图像: render the disk at *its own* resolution, then save it.
 *
 * The full disk used to leave through the close-up's save pane, which meant its
 * size was whatever the preview happened to be and its format was the close-up
 * one's.  It has its own header controls now, and the render they ask for is
 * cached: pressing save twice on the same instant and size costs one render, not
 * two.  Nothing else on the page is touched -- the preview keeps its own, much
 * smaller, size. */
async function saveDisk() {
  const width = diskPixels();
  $('disk-px').value = String(width);
  const when = whenPayload();
  const light = lightPayload();
  const compute = computePayload();
  const component = diskComponent();
  const hdr = diskHdrRequest();
  // The cache key is every input the render depends on -- the moment, the size,
  // the light and the compute budget.  Keying only on the moment used to mean
  // that switching the Earth off and pressing save handed back the *old*
  // preview's bytes, because "nothing had changed".
  const key = JSON.stringify([width, when, light, compute, component, hdr]);
  let source = state.diskExport && state.diskExport.key === key ? state.diskExport : null;
  if (!source) {
    busy('disk-busy', true);
    $('disk-save').disabled = true;
    const progress = startRenderProgress(width * width, 1, `整月面 ${width} px`);
    setStatus(`按 ${width}×${width} px 渲染整月面…`, 'busy');
    try {
      const payload = await post('/api/disk', {
        when,
        ...light,
        ...compute,
        component,
        ...(hdr ? { hdr } : {}),
        preview: { width, samples: 1, shadow_steps: 64 },
      });
      finishRenderProgress(progress, width * width, 1, `${width}×${width} px`);
      source = {
        key, url: payload.image, image: await loadImage(payload.image),
        // The geometry belongs to *this* render, not to whatever the preview
        // was last aimed at: its disk_radius_fraction is what tells the
        // centring how much of the frame the whole Moon covers.
        geometry: payload.geometry || null,
      };
      state.diskExport = source;
      // The gain map travels with the render it belongs to.  It is a statement
      // about the scene, so only the renderer can make it; the page's job is
      // to draw the names on the SDR picture and hand both to the encoder.
      state.diskHdr = payload.hdr || null;
      describeEarthshine(payload.geometry && payload.geometry.earthshine,
        payload.geometry || {});
    } catch (error) {
      clearInterval(renderTimer);
      $('render-progress').classList.remove('on');
      setProgressBar(0);
      showProgress(false);
      setStatus('整月面渲染失败：' + String(error.message || error), 'error');
      return;
    } finally {
      busy('disk-busy', false);
      $('disk-save').disabled = false;
    }
  }
  const format = SAVE_FORMATS[$('disk-format').value] || SAVE_FORMATS.png;
  const annotate = $('disk-annotated').checked && (state.labelsOn || marksWanted());
  const suffix = component === 'image' ? '' : '-' + component;
  const componentName = component === 'sunlit' ? '只出阳光分量' : '';
  const name = `moon-disk${suffix}-${width}.${format.extension}`;
  // 以亮面为中心: only meaningful without earthshine (with it the whole disk is
  // lit and the centroid is the middle anyway), and only when the reader asks.
  // The new centre is the whole disk outline pulled halfway towards the lit
  // side's brightness centroid, and only sideways: see litCentre().
  let centred = null;
  let shift = null;
  if ($('disk-centre-lit').checked && !annotate) {
    const fraction = source.geometry && source.geometry.disk_radius_fraction;
    const centre = litCentre(source.image, fraction);
    if (centre) {
      const shiftX = Math.round(centre.x - source.image.width / 2);
      if (shiftX) {
        centred = centreOnLit(source.image, shiftX, 0);
        shift = { dx: shiftX, dy: 0 };
        setStatus(`已按全月面轮廓与亮面重心平移 ${-shiftX} px（只做水平移动）`);
      }
    }
  }
  // 边缘平滑 belongs to the file as much as to the preview; it is applied here
  // rather than in the renderer because it is a property of the picture, and
  // the *same* value is already on screen.
  const smooth = diskSmoothPx();
  if (centred && !hdr) {
    downloadUrl(URL.createObjectURL(canvasBlob(smoothImage(centred, smooth), format)), name);
    setStatus(`已保存 ${name}（${width}×${width} px，以亮面为中心` +
      `${componentName ? '，' + componentName : ''}）`);
    return;
  }
  if (!centred && !annotate && !hdr && !smooth && format.type === 'image/png') {
    // Nothing to draw on it: the render *is* the file, at full resolution and
    // without passing through a canvas that a 10 000 px picture may not fit in.
    downloadUrl(source.url, name);
    setStatus(`已保存 ${name}（${width}×${width} px${componentName ? '，' + componentName : ''}）`);
    return;
  }
  let plan = null;
  if (state.labelsOn) {
    // A locked layout is not re-laid-out at the export's resolution: it is
    // scaled, so the file carries the arrangement the reader locked rather than
    // the one the avoidance algorithm would choose for a bigger frame.
    plan = lockedDiskPlan(when, width);
    if (plan) {
      setStatus(`正在按 ${width} px 缩放锁定的地貌标注…`, 'busy');
    } else {
      setStatus(`正在按 ${width} px 重排地貌标注…`, 'busy');
      // The reader's own nudges and put-away names travel to the file: the export
      // is composed from the server's plan, not from the screen, so without this
      // a name dragged into place would jump back in the saved picture.
      plan = await diskLabelsAt(width, when);
    }
    plan = applyLabelEdits(plan, 'disk');
  }
  try {
    const canvas = centred || composeImage(source.image, width, width, plan,
      labelStyleForFrame('disk', width / Math.max(1, DISK.width)), null,
      annotate ? { fontPx: plan ? plan.font_px : 12 } : null, false, smooth);
    if (hdr) {
      // The gain map comes from the render that made this frame.  If it is not
      // here, say *that* rather than posting a request the server can only
      // reject with a message about the other field.
      if (!state.diskHdr || !state.diskHdr.gain) {
        throw new Error('这一帧没有带回 HDR 增益图，请重新渲染后再保存');
      }
      setStatus(`正在写 HDR 的 ${name}…`, 'busy');
      const encoded = await post('/api/hdr/jpeg', {
        base: canvas.toDataURL('image/png'),
        gain: state.diskHdr.gain,
        metadata: state.diskHdr.metadata,
        shift: shift ? [shift.dx, shift.dy] : [0, 0],
        quality: 95,
      });
      downloadUrl(encoded.image, name);
      setStatus(`已保存 ${name}（${width}×${width} px，HDR` +
        `${annotate ? '，含标注' : ''}${componentName ? '，' + componentName : ''}）`);
      return;
    }
    downloadUrl(URL.createObjectURL(canvasBlob(canvas, format)), name);
    setStatus(`已保存 ${name}（${width}×${width} px${annotate ? '，含标注' : ''}` +
      `${componentName ? '，' + componentName : ''}）`);
  } catch (error) {
    setStatus('合成失败：' + String(error.message || error) +
      '　可以调小保存分辨率或关掉「地貌」再试。', 'error');
  }
}

/** The HDR block for a disk render, or null when the file is not a JPEG HDR.
 *
 * A gain map *is* a second JPEG inside the first; PNG and TIFF carry their
 * extra bits their own way, and neither of those is this one. */
function diskHdrRequest() {
  const box = $('disk-hdr');
  if (!box || !box.checked) return null;
  const format = SAVE_FORMATS[$('disk-format').value] || SAVE_FORMATS.png;
  if (format.extension !== 'jpg') return null;
  return { on: true, stops: num('disk-hdr-stops') || 3 };
}

/** The HDR switch appears only for JPEG, and its range only when it is on. */
function updateDiskHdrControls() {
  const format = SAVE_FORMATS[$('disk-format').value] || SAVE_FORMATS.png;
  const jpeg = format.extension === 'jpg';
  const on = jpeg && $('disk-hdr').checked;
  $('disk-hdr-label').hidden = !jpeg;
  $('disk-hdr-stops-label').hidden = !on;
}

/* How far the saved frame is moved from the whole-disk centre towards the lit
 * centroid.  Half gives the two equal weight: the disk outline says where the
 * whole Moon is (it is 94% of the frame, so it is most of what the picture
 * contains), and the brightness distribution says where its light is.  Centring
 * on the lit centroid alone pushed a crescent's disk almost out of the frame;
 * centring on the disk alone left the crescent at the edge. */
const LIT_CENTRE_MIX = 0.5;

/* Brightness below this (0..255) is background, not moonlight.  Subtracting it
 * keeps the black corners from voting on where the lit side is. */
const LIT_CENTRE_NOISE = 4;

/** The whole-disk centre and the brightness-weighted lit centre, in pixels.
 *
 * Returns null when there is nothing lit to speak of, which is the honest
 * answer for a frame that is black: centring on an arbitrary pixel would be
 * worse than leaving the picture alone.
 *
 * *diskRadiusFraction* is ``/api/disk``'s own radius as a fraction of the
 * frame's shorter side.  The whole-disk renderer aims the Moon's centre at the
 * centre of the frame, so that is the outline's centre; the fraction is only
 * needed to know how much of the frame the disk covers.  The 0.47 fallback is
 * the renderer's default (``disk_fraction`` 0.94), not a guess at this frame.
 */
function litCentre(image, diskRadiusFraction) {
  if (!image || !image.width) return null;
  const canvas = document.createElement('canvas');
  const probe = 256;                       // a quarter-megapixel is plenty
  canvas.width = probe;
  canvas.height = probe;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(image, 0, 0, probe, probe);
  let data;
  try {
    data = ctx.getImageData(0, 0, probe, probe).data;
  } catch (error) {
    return null;                           // a tainted canvas, in theory only
  }
  const centre = litCentreFromPixels(data, probe, diskRadiusFraction);
  if (!centre) return null;
  const scaleX = image.width / probe;
  const scaleY = image.height / probe;
  return {
    x: centre.x * scaleX,
    y: centre.y * scaleY,
    litX: centre.litX * scaleX,
    litY: centre.litY * scaleY,
    diskX: centre.diskX * scaleX,
    diskY: centre.diskY * scaleY,
    lit: centre.lit,
  };
}

/** The arithmetic behind :func:`litCentre`, on a pre-sampled RGBA probe.
 *
 * Kept separate from the canvas plumbing so the Node harness can feed it a
 * picture it knows the answer for.  Coordinates are probe pixels, with the
 * frame's centre at ``probe / 2``.
 *
 * The lit centroid is weighted by brightness; the disk outline contributes its
 * own centre.  The two get equal weight (``LIT_CENTRE_MIX``), so the frame is
 * aimed halfway between where the whole Moon is and where its light is.  Only
 * the horizontal component moves -- a phase offset is left/right, and following
 * a slightly off-centre libration up or down only un-centres the whole Moon.
 */
function litCentreFromPixels(data, probe, diskRadiusFraction) {
  const size = Math.max(1, Math.round(probe));
  if (!data || data.length < size * size * 4) return null;
  const asked = Number(diskRadiusFraction);
  const radiusFraction = Number.isFinite(asked) && asked > 0
    ? Math.max(0.05, Math.min(0.5, asked)) : 0.47;
  const centre = size / 2;
  const radius = radiusFraction * size;
  let litSum = 0, litX = 0, litY = 0, peak = 0;
  let diskCount = 0, diskX = 0, diskY = 0;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const px = x + 0.5, py = y + 0.5;
      if ((px - centre) ** 2 + (py - centre) ** 2 <= radius * radius) {
        diskCount += 1;
        diskX += px;
        diskY += py;
      }
      const i = 4 * (y * size + x);
      const value = (data[i] + data[i + 1] + data[i + 2]) / 3;
      peak = Math.max(peak, value);
      const weight = Math.max(0, value - LIT_CENTRE_NOISE) / 255;
      litSum += weight;
      litX += weight * px;
      litY += weight * py;
    }
  }
  if (!litSum || peak < 6) return null;
  const diskCentreX = diskCount ? diskX / diskCount : centre;
  const diskCentreY = diskCount ? diskY / diskCount : centre;
  const litCentreX = litX / litSum;
  const litCentreY = litY / litSum;
  return {
    x: diskCentreX + LIT_CENTRE_MIX * (litCentreX - diskCentreX),
    y: diskCentreY,                      // horizontal displacement only
    litX: litCentreX,
    litY: litCentreY,
    diskX: diskCentreX,
    diskY: diskCentreY,
    lit: litSum / (size * size),
  };
}

/** Move a picture so its chosen centre sits in the middle, on black.
 *
 * The Moon is not always in the middle of its own frame: a crescent sits off to
 * one side of the disk the frame was aimed at, and a saved picture with two
 * thirds of it empty is not what the reader asked for.  The background is
 * painted black rather than left transparent, because that is what the frame
 * *is* -- the sky the Moon is in.
 */
function centreOnLit(image, shiftX, shiftY) {
  const canvas = document.createElement('canvas');
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#000000';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(image, -shiftX, -shiftY);
  return canvas;
}

/** data: URLs are what the canvas hands back; a Blob is what a download wants. */
function dataUrlToBlob(url) {
  const [head, body] = String(url).split(',');
  const mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/png';
  if (!/;base64/.test(head)) {
    return new Blob([decodeURIComponent(body)], { type: mime });
  }
  const binary = atob(body);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mime });
}

/* ── the live close-up ─────────────────────────────────────────────────────
 *
 * It is locked to the **selection box**: the window shows the ground the box
 * encloses, and it changes when the box does -- never when the mouse moves.
 * Two reasons, and the second is why it looked wrong before: it is a preview of
 * what 生成模拟 will render, so it has to be the same patch of ground; and a
 * window that follows the pointer cannot be read while the pointer is what you
 * are moving.  (The old one also mapped the cursor straight to image pixels,
 * ignoring that the picture is centred in its box and can be zoomed, so its
 * contents were offset from the cursor by the stage's own offset.)
 *
 * The window always frames the box: the box *is* the frame about to be
 * rendered, and a choice between "the box" and "one image pixel per screen
 * pixel" was a choice nobody wanted to make twice.  The caption says what a
 * screen pixel is worth on the ground, which is the one number that is not
 * visible in the picture itself.
 */
const MAGNIFY_FIT_MARGIN = 1.08;   // a little air round the box when fitting it

/** Where the close-up window is looking, and how big that window is.
 *
 * The window shows the **selection box's own frame**: the ground the box
 * encloses, seen along the box's axes, so a rotated box gives an upright
 * picture -- which is what the render will produce.  Turning the *picture*
 * instead (rotating a square crop about its centre) showed the same patch of
 * Moon tilted, which is not the frame anybody asked for.
 *
 * Its shape follows the box's: a box that is twice as wide as it is tall gets
 * a window twice as wide as it is tall.  Squeezing every frame into a square
 * made the preview lie about the picture the reader was about to get.
 */
function magnifierView(availW, availH) {
  const source = state.diskImage;
  const box = boxGeometry();
  // The box is in *canvas* pixels and the picture is drawn from *image* pixels.
  // The two are the same size in normal running, but nothing enforces it, and a
  // mismatch used to shift the window onto a different part of the Moon; the
  // ratio costs one division and removes the class of bug.
  const ratio = source && DISK.width ? source.width / DISK.width : 1;
  const centreX = box ? box.centre.x * ratio : (source ? source.width / 2 : 0);
  const centreY = box ? box.centre.y * ratio : (source ? source.height / 2 : 0);
  const halfW = box ? box.halfW * ratio : Math.max(1, (source ? source.width : availW) / 2);
  const halfH = box ? box.halfH * ratio : Math.max(1, (source ? source.height : availH) / 2);
  // A little air round the frame, so the edges of it are inside the window.
  const margin = MAGNIFY_FIT_MARGIN;
  const width = 2 * halfW * margin;
  const height = 2 * halfH * margin;
  const scale = Math.max(0.02, Math.min((availW - 2) / width, (availH - 2) / height));
  return {
    fit: true, box, centreX, centreY, halfW, halfH,
    rotation: box ? box.rotation : 0,
    scale,                                   // screen pixels per image pixel
    width: Math.max(24, Math.round(width * scale)),
    height: Math.max(24, Math.round(height * scale)),
  };
}

function drawMagnifier() {
  const panel = $('magnifier');
  if (!panel) return;
  const source = state.diskImage;
  const canvas = $('magnify');
  const info = $('magnify-info');
  const dpr = window.devicePixelRatio || 1;
  // The room the window may use: what is left of the pane after the caption
  // and the typed fields below it.
  const box = panel.getBoundingClientRect ? panel.getBoundingClientRect() : null;
  const availW = Math.max(60, (box && box.width ? box.width : 240) - 8);
  const availH = Math.max(60, (box && box.height ? box.height : 240) - 26);
  const view = magnifierView(availW, availH);
  canvas.width = Math.round(view.width * dpr);
  canvas.height = Math.round(view.height * dpr);
  canvas.style.width = view.width + 'px';
  canvas.style.height = view.height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#07090d';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!source) {
    if (info) info.textContent = '等全月面渲染出来…';
    return;
  }
  // The frame the render will produce, drawn upright: image pixels -> box
  // axes, scaled to the window.
  ctx.save();
  ctx.imageSmoothingEnabled = view.scale < 1;
  ctx.translate(canvas.width / 2, canvas.height / 2);
  ctx.scale(view.scale * dpr, view.scale * dpr);
  // Canvas y runs down, so mapping the box's own axes onto the screen takes
  // the same sign the selection box is drawn with, the other way round.
  ctx.rotate(view.rotation);
  ctx.translate(-view.centreX, -view.centreY);
  ctx.drawImage(source, 0, 0);
  ctx.restore();
  if (!info) return;
  const kmPerImage = kilometresPerImagePixel();
  const parts = [];
  if (view.box && state.geometry) {
    // The frame's own width and height, which is what a reader compares with
    // the sensor's.
    const perPixel = (state.geometry.angular_diameter_deg / 2) * 3600
      / Math.max(1, DISK.width * state.geometry.disk_radius_fraction);
    parts.push(`${(2 * view.halfW * perPixel).toFixed(1)}″ × ` +
      `${(2 * view.halfH * perPixel).toFixed(1)}″`);
    // And the same frame in kilometres: the angular pair says what the sensor
    // covers, this says how much Moon it landed on -- which is the number a
    // reader checks a mosaic or a feature's diameter against.
    if (kmPerImage) {
      parts.push(`${groundScaleText(2 * view.halfW * kmPerImage)} × ` +
        `${groundScaleText(2 * view.halfH * kmPerImage)}`);
    }
  } else if (state.selection) {
    // A box can be off the disk -- a searched feature on the far side -- and
    // then there is nothing under it to magnify; say so rather than showing the
    // whole Moon and letting the caption call it the selection.
    parts.push('选框在月面之外');
  }
  if (kmPerImage) {
    const km = kmPerImage / view.scale;
    parts.push(km < 1
      ? `1 px = ${(km * 1000).toFixed(km < 0.1 ? 1 : 0)} m`
      : `1 px = ${km.toFixed(3)} km`);
  }
  info.textContent = parts.join('　·　');
}

/** Kilometres of Moon per *image* pixel, from the selection's own geometry.
 *
 * The selection has an angular radius and a pixel radius, and the two together
 * are a scale: no other measurement is needed, and it stays right when the
 * window is resized or the picture is re-rendered at another resolution.
 */
function kilometresPerImagePixel() {
  const box = boxGeometry();
  const arcsec = selectionArcsec();
  if (!box || !arcsec || !box.halfW) return null;
  const distance = (state.geometry && state.geometry.distance_km) || 384400;
  return (arcsec / 206264.806) * distance / box.halfW;
}

/* ── landform search ───────────────────────────────────────────────────────
 *
 * The catalogue is fetched once and filtered in the page: 9 000 rows is a
 * fraction of a second over loopback and it makes the drop-down instant, which
 * is the whole point of a search box.  Picking a result does not re-render
 * anything -- it moves the selection box, and the box is a set of selenographic
 * numbers, so a name that is 60° away lands exactly where the gazetteer says.
 */

/** Fetch the searchable catalogue, once.  Returns null if it cannot be had.
 *
 *  The count of what was loaded is not reported anywhere: the drop-down shows
 *  the names themselves, and a line saying how many there are is a line the
 *  reader has to skip past on the way to the box.  Only a failure speaks up. */
async function loadFeatureIndex() {
  if (state.features) return state.features;
  try {
    const response = await fetch('/api/features');
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || response.statusText);
    state.features = payload.features || [];
    for (const feature of state.features) {
      feature.label = feature.zh ? `${feature.zh}（${feature.en}）` : feature.en;
      feature.haystack = `${feature.zh} ${feature.en}`.toLowerCase();
    }
    state.featureByLabel = new Map(state.features.map((f) => [f.label, f]));
    fillClassCounts(payload.classes);
    // A class key the server knows and the page does not would silently never be
    // filtered; the two lists are meant to be the same list.
    const known = new Set(LAND_CLASSES.map((entry) => entry.key));
    const extra = (payload.classes || []).map((entry) => entry.key)
      .filter((key) => !known.has(key));
    if (extra.length) console.info('[landforms] classes the page does not draw:', extra);
    return state.features;
  } catch (error) {
    state.features = null;
    setStatus('载入地貌库失败：' + String(error.message || error), 'error');
    return null;
  }
}

/** The rows that match a query, best (shortest name) first. */
function matchFeatures(query, limit = 24) {
  const needle = String(query || '').trim().toLowerCase();
  if (!needle || !state.features) return [];
  const hits = [];
  for (const feature of state.features) {
    if (feature.haystack.includes(needle)) hits.push(feature);
    if (hits.length > 400) break;
  }
  hits.sort((a, b) => {
    const aHead = a.haystack.startsWith(needle) ? 0 : 1;
    const bHead = b.haystack.startsWith(needle) ? 0 : 1;
    if (aHead !== bHead) return aHead - bHead;
    return a.label.length - b.label.length;
  });
  return hits.slice(0, limit);
}

function fillFeatureOptions(query) {
  const list = $('feature-options');
  list.innerHTML = '';
  for (const feature of matchFeatures(query)) {
    const option = document.createElement('option');
    option.value = feature.label;
    option.label = `${feature.km} km · ${CLASS_LABELS[feature.class] || feature.class}`;
    list.appendChild(option);
  }
}

/** Move the selection box to a landform, keeping the size it already had.
 *
 *  What moved and where it landed is a status line, not a caption under the
 *  search box: the answer belongs with the other answers about the picture, and
 *  the panel is read top-down while the box moves on the disk. */
function selectFeature(feature) {
  if (!state.geometry) { setStatus('等整月面渲染出来再选地貌', 'error'); return; }
  const previous = state.selection;
  const radiusFraction = previous ? previous.radiusFraction : 0.08;
  const rotationDeg = previous ? (previous.rotationDeg || 0) : 0;
  state.selection = { lon: feature.lon, lat: feature.lat, radiusFraction, rotationDeg };
  syncSelectionInputs();
  drawDisk();
  afterSelectionChange();
  const visible = lonLatToPixel(feature.lon, feature.lat) !== null;
  const lonText = `${Math.abs(feature.lon).toFixed(3)}°${feature.lon >= 0 ? 'E' : 'W'}`;
  const latText = `${Math.abs(feature.lat).toFixed(3)}°${feature.lat >= 0 ? 'N' : 'S'}`;
  setStatus(`选框已移到 ${feature.zh || feature.en}（${feature.en}）` +
    `　${latText} / ${lonText}　直径 ${feature.km} km　` +
    `${CLASS_LABELS[feature.class] || feature.class}` +
    (visible ? '' : '　·　该地貌当前在月面背面，选框落在盘外'));
}

function wireFeatureSearch() {
  const input = $('feature-search');
  if (!input) return;
  let loaded = false;
  const ensure = async () => {
    if (loaded) return;
    loaded = true;
    await loadFeatureIndex();
  };
  input.addEventListener('focus', ensure);
  input.addEventListener('input', async () => {
    await ensure();
    fillFeatureOptions(input.value);
  });
  const commit = () => {
    const text = input.value.trim();
    if (!text) return;
    const exact = state.featureByLabel && state.featureByLabel.get(text);
    const hits = exact ? [exact] : matchFeatures(text, 1);
    if (hits.length) selectFeature(hits[0]);
  };
  input.addEventListener('change', commit);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
  });
}

/* ── the mini phase ────────────────────────────────────────────────────────
 *
 * A schematic of the same geometry the full disk is rendered from, drawn the
 * way moonlib_s.plotting.moon_phase_patch draws it: the terminator is the
 * semi-ellipse whose axis is cos(phase angle), the lit limb is the other half
 * of the circle, and the whole shape is turned so its bright side points where
 * the Sun is.  `bright_limb_position_angle_deg` is measured from image north
 * towards image east, so the screen angle counter-clockwise from +x is
 * 90 - PA -- and the canvas, whose y runs down, turns by the negative of that.
 */
/** The lit part of the disk, as a closed polygon centred on the origin.
 *
 * Ported from moonlib_s.plotting.moon_phase_patch, which is where the shape comes
 * from: the outer edge is half the limb, the inner edge is the terminator, and
 * the terminator projects to a semi-ellipse whose axis is cos(phase angle) --
 * ``-(2f - 1)`` for an illuminated fraction *f*, measured from the dark side.
 * The two degenerate cases are the check on the sign: f = 1 closes the far half
 * of the same circle, so the polygon is the whole disk, and f = 0 folds it onto
 * the near half, so the polygon has no area.
 *
 * *positionAngle* is the bright limb's position angle in **radians**, measured
 * from image north towards image east, and the shape is turned so its lit side
 * points there.  Returns points in canvas coordinates -- y downwards -- and the
 * caller draws them; keeping the geometry here is what makes it testable
 * without a canvas.
 */
function phaseShape(fraction, positionAngle, radius, steps = 48) {
  const f = Math.max(0, Math.min(1, fraction));
  const points = [];
  const turn = positionAngle - Math.PI / 2;   // screen angle 90° - PA, negated
  const cos = Math.cos(turn), sin = Math.sin(turn);
  const place = (x, y) => points.push([x * cos - y * sin, x * sin + y * cos]);
  const terminator = -(2 * f - 1);
  for (let i = 0; i <= steps; i += 1) {
    const theta = -Math.PI / 2 + Math.PI * i / steps;
    place(radius * Math.cos(theta), radius * Math.sin(theta));
  }
  for (let i = steps; i >= 0; i -= 1) {
    const theta = -Math.PI / 2 + Math.PI * i / steps;
    place(radius * terminator * Math.cos(theta), radius * Math.sin(theta));
  }
  return points;
}

/** Area of a closed polygon, by the shoelace formula. */
function polygonArea(points) {
  let total = 0;
  for (let i = 0; i < points.length; i += 1) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    total += x1 * y2 - x2 * y1;
  }
  return Math.abs(total) / 2;
}

/** The word on every reset button.
 *
 * It was two arrows chasing each other round a circle.  A glyph has to be
 * taught -- "what does this circle do?" -- while the word says it before the
 * reader has to work anything out, and at 8 px it fits inside the same button
 * height the icon needed.  See `button.reset` in the stylesheet.
 */
const RESET_LABEL = 'Reset';

/** Every button that resets something. */
const RESET_BUTTONS = ['label-reset', 'sel-reset-rot', 'output-reset', 'proc-reset'];

const PHASE_EXPORT_PX = 600;

const PHASE_CANVAS_PX = 72;

/** The approximate lunar-calendar day for an age in days, as a day number.
 *
 * The same banded approximation moonlib_s.phases.lunar_day_label uses, because
 * the page has to agree with the library's own reports: the true Chinese
 * calendar day depends on the civil new-moon date and can differ by one, which
 * is why this is a display label and nothing else is derived from it.
 */
function lunarDayNumber(ageDays) {
  const age = Number(ageDays);
  if (!Number.isFinite(age)) return null;
  const magnitude = Math.abs(age);
  // Waxing: day 1 is the day the new moon falls on.  Waning (a negative age,
  // i.e. still before the conjunction): count back from 三十.
  const day = age >= 0 ? Math.floor(magnitude) + 1 : 30 - Math.floor(magnitude);
  return Math.max(1, Math.min(30, day));
}

/** A lunar-calendar day in Chinese: 初一 … 初十, 十一 … 十九, 二十, 廿一 … 廿九, 三十. */
const LUNAR_DIGITS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];

function lunarDayName(day) {
  if (!day) return null;
  if (day <= 10) return '初' + LUNAR_DIGITS[day - 1];
  if (day < 20) return '十' + LUNAR_DIGITS[day - 11];
  if (day === 20) return '二十';
  if (day < 30) return '廿' + LUNAR_DIGITS[day - 21];
  return '三十';
}

/** The numbers that go with the little phase picture.
 *
 * The old strip showed the bright limb's position angle and the sub-solar
 * point -- both of which describe the drawing rather than the observation.  A
 * reader wants the date in the lunar calendar and where the Moon is: how big,
 * how far, how far from the Sun, and how high in their own sky.
 */
function fillPhaseFacts() {
  const g = state.geometry || {};
  const rep = g.report || {};
  const st = rep.state || {};
  const phase = st.phase || {};
  const lib = st.libration || {};
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  // The lunar date is the *Chinese calendar*, computed by the server from the
  // same ephemeris as everything else: a month begins on the Beijing civil day
  // that holds the new moon, and a leap month is inserted by the 中气 rule.  The
  // page used to estimate it from the Moon's age, which is off by a day
  // whenever the conjunction falls late in the evening.
  const lunar = g.lunar;
  const lunarName = lunar
    ? (lunar.label_zh || lunarDayName(lunar.day))
    : null;
  set('phase-lunar', lunarName
    ? `农历 ${lunarName}`
    : (lunarDayNumber(phase.age_days) === null
      ? '—' : `农历 ${lunarDayName(lunarDayNumber(phase.age_days))}`));
  set('phase-illum', `${fmt(g.illuminated_percent, 1)}%`);
  // Arcminutes only: the strip is a glance, and the arcsecond figure beside it
  // was four more digits for a number nobody reads there.
  set('phase-diameter', st.angular_diameter_arcsec === undefined
    ? '—' : `${(st.angular_diameter_arcsec / 60).toFixed(2)}′`);
  // The two distances, marked 地 / 站 exactly as the Moon readout marks them:
  // they differ by about a third of a percent and the marks are what says which
  // is which.
  set('phase-dist-geo', fmt(st.geo_distance_km, 0));
  set('phase-dist-topo', fmt(st.topo_distance_km, 0));
  set('phase-elongation', st.elongation_deg === undefined
    ? '—' : `${fmt(st.elongation_deg, 1)}°`);
  set('phase-horizon', st.elevation_deg === undefined
    ? '—' : `${fmt(st.elevation_deg, 1)}° / ${fmt(st.azimuth_deg, 1)}°`);
  set('phase-colong', lib.colongitude_deg === undefined
    ? '—' : `${fmt(lib.colongitude_deg, 1)}°`);
}

/** Paint one phase disc into a context, at whatever size it is given.
 *
 * Split out of the strip so the reader can have the same picture at 600x600:
 * a thumbnail is for the column, and this is the picture the column shows.
 */
function paintPhase(ctx, canvas, geometry) {
  const cx = canvas.width / 2, cy = canvas.height / 2;
  const radius = 0.44 * Math.min(canvas.width, canvas.height);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#12161d';        // the night side, as the dark-adapted eye sees it
  ctx.fill();
  if (!geometry) return;
  const fraction = Math.max(0, Math.min(1, (geometry.illuminated_percent || 0) / 100));
  const positionAngle = (geometry.bright_limb_position_angle_deg || 0) * Math.PI / 180;
  if (fraction > 1e-4) {
    ctx.save();
    ctx.translate(cx, cy);
    ctx.beginPath();
    phaseShape(fraction, positionAngle, radius).forEach(([x, y], index) => {
      if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    });
    ctx.closePath();
    ctx.fillStyle = '#e4e7ec';
    ctx.fill();
    ctx.restore();
  }
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(230, 232, 238, .22)';
  ctx.lineWidth = Math.max(1, canvas.width / 200);
  ctx.stroke();
}

/** The strip's own thumbnail, at column size. */
function drawPhaseStrip() {
  const canvas = $('phase-canvas');
  if (!canvas) return;
  const geometry = state.geometry;
  const dpr = window.devicePixelRatio || 1;
  // The *CSS* size is fixed here and the backing store follows it: reading the
  // layout size on every redraw would grow the canvas by the device ratio each
  // time, because an attribute width is itself a layout width until CSS says
  // otherwise.
  const size = PHASE_CANVAS_PX;
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);
  canvas.style.width = size + 'px';
  canvas.style.height = size + 'px';
  const ctx = canvas.getContext('2d');
  paintPhase(ctx, canvas, geometry);

  if (!geometry) return;
  const report = geometry.report || {};
  const phase = (report.state || {}).phase || {};
  $('phase-name').textContent = phase.label || '月相';
  // Age and the lunar-calendar date sit together in the title: they are the
  // same fact told two ways, and the date is the one a Chinese reader plans by.
  $('phase-age').textContent = `月龄 ${fmt(phase.age_days, 2)} d`;
  fillPhaseFacts();
}


/* ── 天空位置示意图 ────────────────────────────────────────────────────────
 *
 * The window is a canvas rather than a second matplotlib figure: every track,
 * label and axis limit has to answer the mouse, and a page that already knows
 * how to draw on a canvas has no reason to ship a picture of a chart.  The
 * data is the same ``az_el`` the library's own plot uses; the server returns
 * the samples, this code decides how to read them.
 *
 * Equal scale in both axes is the one geometric promise the view makes.  A
 * degree of azimuth and a degree of altitude are the same size on the page,
 * so a track's shape is true wherever it is dragged to.
 */

const SKY_MIN_WIDTH = 520;
const SKY_MIN_HEIGHT = 400;
const SKY_MARGIN = { left: 58, right: 16, top: 16, bottom: 42 };
const SKY_DEFAULT_STEP_SECONDS = 120;

/** The short way round the circle, in degrees: [-180, 180). */
function skyWrap180(degrees) {
  return ((Number(degrees) + 180) % 360 + 360) % 360 - 180;
}

function skyClamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

/** The chart's own box, inside whatever area the canvas has. */
function skyPlotLayout(area) {
  const width = Math.max(10, area.w - SKY_MARGIN.left - SKY_MARGIN.right);
  const height = Math.max(10, area.h - SKY_MARGIN.top - SKY_MARGIN.bottom);
  return {
    x: area.x + SKY_MARGIN.left,
    y: area.y + SKY_MARGIN.top,
    w: width,
    h: height,
  };
}

function skyCanvasMetrics() {
  const canvas = $('sky-canvas');
  const rect = canvas && canvas.getBoundingClientRect
    ? canvas.getBoundingClientRect() : null;
  return {
    canvas,
    width: Math.max(320, (rect && rect.width) || 760),
    height: Math.max(240, (rect && rect.height) || 520),
    dpr: window.devicePixelRatio || 1,
  };
}

function skyOptions() {
  const step = Number($('sky-label-step').value);
  return {
    labels: $('sky-labels').checked,
    labelStep: Number.isFinite(step) && step > 0 ? step : 30,
    elongation: $('sky-elongation').checked,
    closeup: $('sky-closeup').checked,
  };
}

/** Does the Sun belong in the frame at all? */
function skySunRelevant(data, closeup) {
  if (closeup || !data || !data.sun || !data.moon) return false;
  const sun = data.sun;
  const gap = Math.abs(skyWrap180(sun.azimuth_deg - data.moon.azimuth_deg));
  return sun.elevation_deg > -18.0 && gap < 125.0;
}

/** Would a full horizon view be the wrong picture?
 *
 * The case the reader named: a high Moon far from the Sun makes the pair span
 * most of the sky, and drawing both forces the Moon's own track into a thin
 * band.  A close-up of the Moon's track, with the Sun reduced to a direction
 * arrow, is more use than a technically complete chart.
 */
function skyAutoCloseup(data) {
  const samples = (data && data.samples) || [];
  if (!samples.length) return false;
  const geometry = (data && data.geometry) || {};
  const elongation = Number(geometry.elongation_deg) || 0;
  const moon = (data && data.moon) || { azimuth_deg: 0, elevation_deg: 0 };
  const sun = (data && data.sun) || { azimuth_deg: 0, elevation_deg: 0 };
  const gap = Math.abs(skyWrap180(sun.azimuth_deg - moon.azimuth_deg));
  // The *current* altitude is what decides whether the full horizon view is
  // useful.  Using the minimum over the whole span would turn the close-up off
  // as soon as the Moon sets later in the window, even while it is high now.
  return (moon.elevation_deg > 28.0 && (elongation > 65.0 || gap > 110.0))
    || (moon.elevation_deg > 14.0 && gap > 150.0);
}

/** The samples, with azimuth unwrapped around the Moon's own anchor.
 *
 * A track that crosses north is continuous data with a 359 -> 1 jump in it.
 * Unwrapping against the anchor keeps the plotted line joined; the tick labels
 * put the compass names back on the numbers. */
function skyUnwrapAround(values, centre, anchor) {
  const out = new Array(values.length);
  if (!values.length) return out;
  const index = Math.max(0, Math.min(values.length - 1, centre));
  out[index] = anchor;
  for (let i = index + 1; i < values.length; i += 1) {
    out[i] = out[i - 1] + skyWrap180(values[i] - values[i - 1]);
  }
  for (let i = index - 1; i >= 0; i -= 1) {
    out[i] = out[i + 1] + skyWrap180(values[i] - values[i + 1]);
  }
  return out;
}

function skySeries(data) {
  const samples = (data && data.samples) || [];
  if (!samples.length) return [];
  const centre = Math.floor((samples.length - 1) / 2);
  const moonRaw = samples.map((sample) => sample.moon_azimuth_deg);
  const sunRaw = samples.map((sample) => sample.sun_azimuth_deg);
  const moonAz = skyUnwrapAround(
    moonRaw, centre, data.moon ? data.moon.azimuth_deg : moonRaw[centre]);
  const sunAz = skyUnwrapAround(
    sunRaw, centre, data.sun ? data.sun.azimuth_deg : sunRaw[centre]);
  return samples.map((sample, index) => ({
    t: Date.parse(sample.when_utc),
    moonAz: moonAz[index],
    moonEl: sample.moon_elevation_deg,
    sunAz: sunAz[index],
    sunEl: sample.sun_elevation_deg,
  }));
}

function skyTargetView(data, closeup) {
  const series = state.skySeries && state.skySeries.length
    ? state.skySeries : skySeries(data);
  if (!series.length) {
    return { azMin: 0, azMax: 90, elMin: 0, elMax: 60 };
  }
  // A close-up is about the part of the track the Moon is actually above the
  // horizon for.  If the whole span is below it (or the Moon never rises),
  // fall back to the whole series rather than drawing an empty frame.
  let visible = series;
  if (closeup) {
    const above = series.filter((sample) => sample.moonEl > 0.0);
    if (above.length) visible = above;
  }
  let azMin = Infinity, azMax = -Infinity;
  let elMin = Infinity, elMax = -Infinity;
  for (const sample of visible) {
    azMin = Math.min(azMin, sample.moonAz);
    azMax = Math.max(azMax, sample.moonAz);
    elMin = Math.min(elMin, sample.moonEl);
    elMax = Math.max(elMax, sample.moonEl);
  }
  if (!closeup && skySunRelevant(data, false)) {
    for (const sample of series) {
      azMin = Math.min(azMin, sample.sunAz);
      azMax = Math.max(azMax, sample.sunAz);
    }
    elMin = Math.min(elMin, 0.0);
    elMax = Math.max(elMax, 10.0);
  } else if (!closeup) {
    // The horizon is worth keeping even when the Sun is far away or well below
    // it: without it the chart stops being a horizon view.
    elMin = Math.min(elMin, 0.0);
    elMax = Math.max(elMax, 10.0);
  }
  let azSpan = Math.max(0.5, azMax - azMin);
  let elSpan = Math.max(0.5, elMax - elMin);
  const azPad = Math.max(2.0, azSpan * 0.08);
  const elPad = Math.max(2.0, elSpan * 0.10);
  azMin -= azPad;
  azMax += azPad;
  elMin -= elPad;
  elMax += elPad;
  if (azMax - azMin < 10.0) {
    const middle = 0.5 * (azMin + azMax);
    azMin = middle - 5.0;
    azMax = middle + 5.0;
  }
  if (elMax - elMin < 8.0) {
    const middle = 0.5 * (elMin + elMax);
    elMin = middle - 4.0;
    elMax = middle + 4.0;
  }
  return { azMin, azMax, elMin, elMax };
}

/** Make the view aspect match the plot by *growing* the short axis.
 *
 * Used when a view is first fitted: growing cannot crop a track that was just
 * measured. */
function skyGrowAspect(view, aspect) {
  const ratio = Math.max(1e-4, aspect);
  const azSpan = Math.max(1e-4, view.azMax - view.azMin);
  const elSpan = Math.max(1e-4, view.elMax - view.elMin);
  if (azSpan / elSpan > ratio) {
    const middle = 0.5 * (view.elMin + view.elMax);
    const wanted = azSpan / ratio;
    view.elMin = middle - 0.5 * wanted;
    view.elMax = middle + 0.5 * wanted;
  } else {
    const middle = 0.5 * (view.azMin + view.azMax);
    const wanted = elSpan * ratio;
    view.azMin = middle - 0.5 * wanted;
    view.azMax = middle + 0.5 * wanted;
  }
}

/** Set the vertical span from the horizontal one, keeping the centre. */
function skyFitAspectFromAz(view, aspect) {
  const ratio = Math.max(1e-4, aspect);
  const azSpan = Math.max(0.5, view.azMax - view.azMin);
  const elSpan = Math.max(0.5, azSpan / ratio);
  const middle = 0.5 * (view.elMin + view.elMax);
  view.elMin = middle - 0.5 * elSpan;
  view.elMax = middle + 0.5 * elSpan;
}

/** Set the horizontal span from the vertical one, keeping the centre. */
function skyFitAspectFromEl(view, aspect) {
  const ratio = Math.max(1e-4, aspect);
  const elSpan = Math.max(0.5, view.elMax - view.elMin);
  const azSpan = Math.max(0.5, elSpan * ratio);
  const middle = 0.5 * (view.azMin + view.azMax);
  view.azMin = middle - 0.5 * azSpan;
  view.azMax = middle + 0.5 * azSpan;
}

function skyEnsureView(plot, data, options) {
  let view = state.skyView;
  const aspect = Math.max(0.1, plot.w / Math.max(1, plot.h));
  if (!view) {
    view = skyTargetView(data, options.closeup);
    skyGrowAspect(view, aspect);
    state.skyView = view;
  } else if (Math.abs((view.azMax - view.azMin)
                      / Math.max(1e-6, view.elMax - view.elMin) - aspect) > 1e-3) {
    // A window resize changes the plot's shape, not the reader's choice of
    // centre.  Grow the short axis rather than fitting from one of them: a
    // fit would crop whichever track this resized view was framing.
    skyGrowAspect(view, aspect);
  }
  return view;
}

function skyProjection(plot, view) {
  const azSpan = Math.max(1e-6, view.azMax - view.azMin);
  const elSpan = Math.max(1e-6, view.elMax - view.elMin);
  return {
    x: (az) => plot.x + (az - view.azMin) / azSpan * plot.w,
    y: (el) => plot.y + (view.elMax - el) / elSpan * plot.h,
    az: (px) => view.azMin + (px - plot.x) / Math.max(1, plot.w) * azSpan,
    el: (py) => view.elMax - (py - plot.y) / Math.max(1, plot.h) * elSpan,
  };
}

function niceSkyStep(span, target) {
  const raw = Math.max(1e-6, span / Math.max(2, target));
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const scaled = raw / power;
  const multiplier = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return multiplier * power;
}

function skyAzText(degrees) {
  const normal = ((Math.round(degrees) % 360) + 360) % 360;
  const words = { 0: '北', 90: '东', 180: '南', 270: '西' };
  return words[normal] ? `${normal}° ${words[normal]}` : `${normal}°`;
}

function skyElText(degrees) {
  return `${Math.round(degrees)}°`;
}

function skyTwilight(data) {
  const sunEl = data && data.sun ? data.sun.elevation_deg : -18.0;
  return skyClamp((sunEl + 18.0) / 18.0, 0.0, 1.0);
}

function skyBandColor(elevation, twilight) {
  if (elevation < 0.0) return '#080a0f';
  const high = skyClamp(elevation / 70.0, 0.0, 1.0);
  const warm = skyClamp(twilight, 0.0, 1.0);
  const red = Math.round((16 + 34 * warm) * (1 - high) + 7 * high);
  const green = Math.round((20 + 18 * warm) * (1 - high) + 11 * high);
  const blue = Math.round((32 + 12 * warm) * (1 - high) + 23 * high);
  return `rgb(${red},${green},${blue})`;
}

/** A clock label in the page's own time zone. */
function skyClockText(localMilliseconds) {
  const date = new Date(localMilliseconds);
  const hours = String(date.getUTCHours()).padStart(2, '0');
  const minutes = String(date.getUTCMinutes()).padStart(2, '0');
  return `${hours}:${minutes}`;
}

function skyLocalText(iso, offsetHours, withDate) {
  const stamp = Date.parse(iso);
  if (!Number.isFinite(stamp)) return '—';
  const date = new Date(stamp + (Number(offsetHours) || 0) * 3600000);
  const clock = `${String(date.getUTCHours()).padStart(2, '0')}:`
    + `${String(date.getUTCMinutes()).padStart(2, '0')}`;
  if (!withDate) return clock;
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const day = String(date.getUTCDate()).padStart(2, '0');
  return `${month}-${day} ${clock}`;
}

function skyInterpolate(series, stamp) {
  if (!series.length) return null;
  if (stamp <= series[0].t) return series[0];
  const last = series[series.length - 1];
  if (stamp >= last.t) return last;
  let low = 0;
  let high = series.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (series[middle].t <= stamp) low = middle;
    else high = middle;
  }
  const a = series[low];
  const b = series[high];
  const fraction = (stamp - a.t) / Math.max(1, b.t - a.t);
  const mix = (left, right) => left + (right - left) * fraction;
  return {
    moonAz: mix(a.moonAz, b.moonAz),
    moonEl: mix(a.moonEl, b.moonEl),
    sunAz: mix(a.sunAz, b.sunAz),
    sunEl: mix(a.sunEl, b.sunEl),
  };
}

/** Times worth labelling, aligned to round local clock times. */
function skyLabelItems(data, options, view, projection, plot) {
  if (!options.labels) return [];
  const series = state.skySeries && state.skySeries.length
    ? state.skySeries : skySeries(data);
  if (!series.length) return [];
  const stepMilliseconds = Math.max(1, options.labelStep) * 60000;
  const offset = (Number(data.tz_offset_hours) || 0) * 3600000;
  const start = series[0].t;
  const end = series[series.length - 1].t;
  let moment = Math.ceil((start + offset) / stepMilliseconds) * stepMilliseconds - offset;
  const raw = [];
  for (; moment <= end + 1; moment += stepMilliseconds) {
    const point = skyInterpolate(series, moment);
    const x = projection.x(point.moonAz);
    const y = projection.y(point.moonEl);
    if (x < plot.x + 2 || x > plot.x + plot.w - 2
        || y < plot.y + 2 || y > plot.y + plot.h - 2) continue;
    raw.push({ x, y, text: skyClockText(moment + offset) });
  }
  // A five-minute interval across a long span is a wall of numbers; keep the
  // requested interval when it fits and thin by a whole multiple when it does
  // not.  The labels still tell the truth about the instant under them.
  const stride = Math.max(1, Math.ceil(raw.length / 24));
  return raw.filter((item, index) => index % stride === 0);
}

function skyBoxOverlap(a, b) {
  const width = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
  const height = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return width * height;
}

/** Place each label near its point, avoiding every box already placed. */
function skyPlaceLabels(ctx, items, plot, obstacles) {
  const boxes = (obstacles || []).slice();
  const placed = [];
  const offsets = [
    [0, -14], [0, 15], [18, 0], [-18, 0],
    [16, -13], [-16, -13], [16, 13], [-16, 13],
    [0, -28], [0, 29], [34, 0], [-34, 0],
    [0, -42], [0, 43], [50, 0], [-50, 0],
  ];
  for (const item of items) {
    const width = ctx.measureText(item.text).width + 8;
    const height = 15;
    let best = null;
    for (const [dx, dy] of offsets) {
      const box = {
        x: item.x + dx - width / 2,
        y: item.y + dy - height / 2,
        w: width,
        h: height,
      };
      let outside = 0;
      outside += Math.max(0, plot.x + 2 - box.x);
      outside += Math.max(0, box.x + box.w - (plot.x + plot.w - 2));
      outside += Math.max(0, plot.y + 2 - box.y);
      outside += Math.max(0, box.y + box.h - (plot.y + plot.h - 2));
      let score = outside * 1000 + Math.hypot(dx, dy) * 0.15;
      for (const other of boxes) score += skyBoxOverlap(box, other);
      if (!best || score < best.score) best = { box, score };
      if (score < 0.5) break;
    }
    if (!best) continue;
    boxes.push(best.box);
    placed.push({ text: item.text, x: item.x, y: item.y, box: best.box });
  }
  return placed;
}

function skyPaintMoon(ctx, x, y, radius, fraction, angle) {
  ctx.save();
  ctx.translate(x, y);
  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#151a23';
  ctx.fill();
  if (fraction > 1e-4) {
    ctx.beginPath();
    phaseShape(fraction, angle, radius, 30).forEach(([px, py], index) => {
      if (index) ctx.lineTo(px, py); else ctx.moveTo(px, py);
    });
    ctx.closePath();
    ctx.fillStyle = '#e8ecf5';
    ctx.fill();
  }
  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(230,232,238,.38)';
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.restore();
}

function skyRayLength(x, y, dx, dy, plot) {
  let distance = Infinity;
  if (dx > 1e-9) distance = Math.min(distance, (plot.x + plot.w - x) / dx);
  if (dx < -1e-9) distance = Math.min(distance, (plot.x - x) / dx);
  if (dy > 1e-9) distance = Math.min(distance, (plot.y + plot.h - y) / dy);
  if (dy < -1e-9) distance = Math.min(distance, (plot.y - y) / dy);
  return Math.max(0, distance);
}

function skyPaintEdgeHandles(ctx, plot, hovered) {
  const edges = [
    { name: 'left', x1: plot.x, y1: plot.y + plot.h / 2 - 13,
      x2: plot.x, y2: plot.y + plot.h / 2 + 13 },
    { name: 'right', x1: plot.x + plot.w, y1: plot.y + plot.h / 2 - 13,
      x2: plot.x + plot.w, y2: plot.y + plot.h / 2 + 13 },
    { name: 'top', x1: plot.x + plot.w / 2 - 13, y1: plot.y,
      x2: plot.x + plot.w / 2 + 13, y2: plot.y },
    { name: 'bottom', x1: plot.x + plot.w / 2 - 13, y1: plot.y + plot.h,
      x2: plot.x + plot.w / 2 + 13, y2: plot.y + plot.h },
  ];
  for (const edge of edges) {
    ctx.beginPath();
    ctx.moveTo(edge.x1, edge.y1);
    ctx.lineTo(edge.x2, edge.y2);
    ctx.strokeStyle = hovered === edge.name
      ? 'rgba(122,162,247,.95)' : 'rgba(122,162,247,.45)';
    ctx.lineWidth = hovered === edge.name ? 3 : 2;
    ctx.stroke();
  }
}

/** Everything inside the canvas.  Returns the plot rectangle. */
function paintSkyChart(ctx, area, data, view, options) {
  const plot = skyPlotLayout(area);
  const projection = skyProjection(plot, view);
  const series = state.skySeries && state.skySeries.length
    ? state.skySeries : skySeries(data);
  const twilight = skyTwilight(data);
  const closeup = options.closeup;

  ctx.save();
  ctx.fillStyle = '#07090d';
  ctx.fillRect(area.x, area.y, area.w, area.h);

  ctx.save();
  ctx.beginPath();
  ctx.rect(plot.x, plot.y, plot.w, plot.h);
  ctx.clip();

  // Sky above the horizon, ground below it.  The bands are drawn rather than a
  // gradient because the page's own twilight colours are a ladder of a few
  // stages, not a linear ramp.
  const bands = 96;
  for (let index = 0; index < bands; index += 1) {
    const low = view.elMin + (view.elMax - view.elMin) * index / bands;
    const high = view.elMin + (view.elMax - view.elMin) * (index + 1) / bands;
    const top = projection.y(high);
    const bottom = projection.y(low);
    ctx.fillStyle = skyBandColor(0.5 * (low + high), twilight);
    ctx.fillRect(plot.x, top, plot.w, Math.max(1, bottom - top + 0.6));
  }
  // A close-up deliberately has no horizon: its whole point is the Moon's own
  // arc, and a ground line would claim a context the reader asked to drop.
  if (!closeup) {
    if (view.elMin < 0 && view.elMax > 0) {
      const horizon = projection.y(0);
      ctx.fillStyle = 'rgba(4,6,10,.88)';
      ctx.fillRect(plot.x, horizon, plot.w, plot.y + plot.h - horizon);
    }

    // Twilight and horizon lines.
    for (const level of [-18, -12, -6]) {
      if (level <= view.elMin || level >= view.elMax) continue;
      const y = projection.y(level);
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.w, y);
      ctx.strokeStyle = 'rgba(136,153,170,.22)';
      ctx.lineWidth = 0.7;
      ctx.setLineDash([2, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (view.elMin < 0 && view.elMax > 0) {
      const y = projection.y(0);
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.w, y);
      ctx.strokeStyle = 'rgba(150,165,185,.58)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.fillStyle = 'rgba(170,184,204,.68)';
      ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText('地平线', plot.x + 5, y - 3);
    }
  }

  // Grid.
  const azStep = niceSkyStep(view.azMax - view.azMin, Math.max(3, plot.w / 95));
  const elStep = niceSkyStep(view.elMax - view.elMin, Math.max(3, plot.h / 58));
  ctx.lineWidth = 0.6;
  for (let az = Math.ceil(view.azMin / azStep) * azStep;
       az <= view.azMax + 1e-9; az += azStep) {
    const x = projection.x(az);
    const normal = ((Math.round(az) % 360) + 360) % 360;
    const cardinal = normal % 90 === 0;
    ctx.strokeStyle = cardinal ? 'rgba(140,158,184,.26)' : 'rgba(120,134,156,.14)';
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();
  }
  for (let el = Math.ceil(view.elMin / elStep) * elStep;
       el <= view.elMax + 1e-9; el += elStep) {
    const y = projection.y(el);
    ctx.strokeStyle = Math.abs(el) < 1e-9
      ? 'rgba(150,165,185,.32)' : 'rgba(120,134,156,.14)';
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
  }

  // Sun's track.  It is a dashed line because it is context, not the subject.
  const sunRelevant = skySunRelevant(data, closeup);
  if (sunRelevant && series.length > 1) {
    ctx.beginPath();
    series.forEach((sample, index) => {
      const x = projection.x(sample.sunAz);
      const y = projection.y(sample.sunEl);
      if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    });
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = 'rgba(255,176,64,.72)';
    ctx.lineWidth = 1.1;
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Moon's track: a wide, faint halo under a thin bright line.
  if (series.length > 1) {
    for (const pass of [{ width: 7, colour: 'rgba(223,228,238,.12)' },
                        { width: 2, colour: '#dfe4ee' }]) {
      ctx.beginPath();
      series.forEach((sample, index) => {
        const x = projection.x(sample.moonAz);
        const y = projection.y(sample.moonEl);
        if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      ctx.strokeStyle = pass.colour;
      ctx.lineWidth = pass.width;
      ctx.stroke();
    }
  }

  const moonPoint = {
    x: projection.x(data.moon.azimuth_deg),
    y: projection.y(data.moon.elevation_deg),
  };
  const sunPoint = {
    x: projection.x(data.sun.azimuth_deg),
    y: projection.y(data.sun.elevation_deg),
  };
  const elongation = Number((data.geometry || {}).elongation_deg) || 0;
  const fraction = skyClamp(
    (Number((data.geometry || {}).illumination_percent_topo) || 0) / 100, 0, 1);
  const sunAngle = Math.atan2(sunPoint.x - moonPoint.x, -(sunPoint.y - moonPoint.y));

  // The line between the two bodies: one line at the chart's own instant,
  // labelled with the separation it measures.  It is deliberately the only
  // one -- a line per tick would be a comb, not an annotation.
  let elongationBox = null;
  if (options.elongation) {
    ctx.beginPath();
    ctx.moveTo(moonPoint.x, moonPoint.y);
    ctx.lineTo(sunPoint.x, sunPoint.y);
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = 'rgba(122,162,247,.62)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    const middle = {
      x: 0.5 * (moonPoint.x + sunPoint.x),
      y: 0.5 * (moonPoint.y + sunPoint.y),
    };
    if (middle.x > plot.x + 4 && middle.x < plot.x + plot.w - 4
        && middle.y > plot.y + 4 && middle.y < plot.y + plot.h - 4) {
      const text = `距角 ${elongation.toFixed(1)}°`;
      ctx.font = '10.5px -apple-system, "PingFang SC", sans-serif';
      const width = ctx.measureText(text).width + 10;
      const height = 15;
      elongationBox = { x: middle.x - width / 2, y: middle.y - height / 2,
                        w: width, h: height };
      ctx.fillStyle = 'rgba(9,12,18,.74)';
      ctx.fillRect(elongationBox.x, elongationBox.y, width, height);
      ctx.fillStyle = 'rgba(160,187,240,.95)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, middle.x, middle.y);
    }
  }

  // Sun marker, when it is in the view; otherwise the direction arrow.
  if (sunRelevant && sunPoint.x > plot.x - 12 && sunPoint.x < plot.x + plot.w + 12
      && sunPoint.y > plot.y - 12 && sunPoint.y < plot.y + plot.h + 12) {
    ctx.beginPath();
    ctx.arc(sunPoint.x, sunPoint.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffd166';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(sunPoint.x, sunPoint.y, 9, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(255,209,102,.30)';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // Moon marker: the phase is the one the sky actually shows, with the bright
  // limb turned towards the Sun *on the chart*.
  if (moonPoint.x > plot.x - 20 && moonPoint.x < plot.x + plot.w + 20
      && moonPoint.y > plot.y - 20 && moonPoint.y < plot.y + plot.h + 20) {
    skyPaintMoon(ctx, moonPoint.x, moonPoint.y, 7.5, fraction, sunAngle);
  }

  // In the close-up the Sun is reduced to a direction: an arrow from the Moon
  // towards it, clipped at the frame, with the compass direction on its tip.
  if (closeup) {
    let dx = sunPoint.x - moonPoint.x;
    let dy = sunPoint.y - moonPoint.y;
    const length = Math.hypot(dx, dy);
    if (length > 1) {
      dx /= length;
      dy /= length;
      const reach = skyRayLength(moonPoint.x, moonPoint.y, dx, dy, plot);
      const draw = Math.max(26, Math.min(82, reach - 10));
      const startX = moonPoint.x + dx * 13;
      const startY = moonPoint.y + dy * 13;
      const endX = moonPoint.x + dx * draw;
      const endY = moonPoint.y + dy * draw;
      ctx.beginPath();
      ctx.moveTo(startX, startY);
      ctx.lineTo(endX, endY);
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = 'rgba(255,176,64,.8)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.setLineDash([]);
      // Arrowhead.
      const wing = 5;
      const angle = Math.atan2(dy, dx);
      ctx.beginPath();
      ctx.moveTo(endX, endY);
      ctx.lineTo(endX - wing * Math.cos(angle - 0.45),
                 endY - wing * Math.sin(angle - 0.45));
      ctx.moveTo(endX, endY);
      ctx.lineTo(endX - wing * Math.cos(angle + 0.45),
                 endY - wing * Math.sin(angle + 0.45));
      ctx.strokeStyle = '#ffb040';
      ctx.lineWidth = 1.4;
      ctx.stroke();
      const label = `太阳 ${skyAzText(data.sun.azimuth_deg)}`;
      ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
      const width = ctx.measureText(label).width;
      const textX = skyClamp(endX + dx * 7, plot.x + 3, plot.x + plot.w - width - 3);
      const textY = skyClamp(endY + dy * 7, plot.y + 10, plot.y + plot.h - 4);
      ctx.fillStyle = 'rgba(9,12,18,.68)';
      ctx.fillRect(textX - 2, textY - 8, width + 4, 13);
      ctx.fillStyle = '#ffc266';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, textX, textY);
    }
  }

  ctx.restore();  // clip

  // Axes: ticks outside the plot, grid inside it.
  ctx.font = '10.5px -apple-system, "PingFang SC", sans-serif';
  ctx.fillStyle = '#8b93a3';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let az = Math.ceil(view.azMin / azStep) * azStep;
       az <= view.azMax + 1e-9; az += azStep) {
    const x = projection.x(az);
    ctx.fillText(skyAzText(az), x, plot.y + plot.h + 5);
  }
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let el = Math.ceil(view.elMin / elStep) * elStep;
       el <= view.elMax + 1e-9; el += elStep) {
    const y = projection.y(el);
    ctx.fillText(skyElText(el), plot.x - 7, y);
  }
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillStyle = '#98a0b0';
  ctx.font = '10.5px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText('方位角 (°)   0 = 北 · 90 = 东', plot.x + plot.w / 2,
               plot.y + plot.h + SKY_MARGIN.bottom - 2);
  ctx.save();
  ctx.translate(plot.x - 42, plot.y + plot.h / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('高度角 (°)', 0, 0);
  ctx.restore();
  ctx.strokeStyle = 'rgba(150,165,185,.34)';
  ctx.lineWidth = 1;
  ctx.strokeRect(plot.x, plot.y, plot.w, plot.h);

  // Labels last, so their boxes sit above every line they describe.
  const obstacles = [];
  if (moonPoint.x > plot.x && moonPoint.x < plot.x + plot.w
      && moonPoint.y > plot.y && moonPoint.y < plot.y + plot.h) {
    obstacles.push({ x: moonPoint.x - 10, y: moonPoint.y - 10, w: 20, h: 20 });
  }
  if (sunRelevant && sunPoint.x > plot.x && sunPoint.x < plot.x + plot.w
      && sunPoint.y > plot.y && sunPoint.y < plot.y + plot.h) {
    obstacles.push({ x: sunPoint.x - 10, y: sunPoint.y - 10, w: 20, h: 20 });
  }
  if (elongationBox) obstacles.push(elongationBox);
  obstacles.push({ x: plot.x + 3, y: plot.y + 3, w: 116, h: 22 });
  const labelItems = skyLabelItems(data, options, view, projection, plot);
  if (labelItems.length) {
    ctx.font = '11px -apple-system, "PingFang SC", sans-serif';
    const placed = skyPlaceLabels(ctx, labelItems, plot, obstacles);
    for (const label of placed) {
      const box = label.box;
      const nearestX = skyClamp(label.x, box.x, box.x + box.w);
      const nearestY = skyClamp(label.y, box.y, box.y + box.h);
      ctx.beginPath();
      ctx.moveTo(label.x, label.y);
      ctx.lineTo(nearestX, nearestY);
      ctx.strokeStyle = 'rgba(136,153,170,.5)';
      ctx.lineWidth = 0.7;
      ctx.stroke();
      ctx.fillStyle = 'rgba(11,14,20,.78)';
      ctx.fillRect(box.x, box.y, box.w, box.h);
      ctx.fillStyle = '#aeb7c7';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label.text, box.x + box.w / 2, box.y + box.h / 2);
    }
  }

  // The mode badge, and the edge grips that say the axes can be re-ranged.
  const badge = closeup ? '轨迹特写' : '地平线视图';
  ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
  const badgeWidth = ctx.measureText(badge).width + 14;
  ctx.fillStyle = 'rgba(122,162,247,.10)';
  ctx.fillRect(plot.x + 4, plot.y + 4, badgeWidth, 17);
  ctx.strokeStyle = 'rgba(122,162,247,.55)';
  ctx.lineWidth = 0.8;
  ctx.strokeRect(plot.x + 4, plot.y + 4, badgeWidth, 17);
  ctx.fillStyle = '#9db8f5';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(badge, plot.x + 4 + badgeWidth / 2, plot.y + 12.5);

  if (options.interactive !== false) {
    skyPaintEdgeHandles(ctx, plot, state.skyHoverEdge);
  }

  ctx.restore();
  return { plot, projection };
}

/** The canvas, at its on-screen size. */
function drawSky() {
  if (!state.sky) return;
  const metrics = skyCanvasMetrics();
  if (!metrics.canvas) return;
  const { canvas, width, height, dpr } = metrics;
  const pixelWidth = Math.round(width * dpr);
  const pixelHeight = Math.round(height * dpr);
  // Assigning width/height clears the backing store, so only do it when the
  // layout really changed; a wheel tick or a hover redraw should not pay for
  // reallocating a megapixel canvas.
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  // The CSS says `width: 100%; height: 100%`; writing the measured size back
  // would pin the canvas at its old size and stop it following a window drag.
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const options = skyOptions();
  const plot = skyPlotLayout({ x: 0, y: 0, w: width, h: height });
  const view = skyEnsureView(plot, state.sky, options);
  const painted = paintSkyChart(ctx, { x: 0, y: 0, w: width, h: height },
                                state.sky, view, options);
  state.skyLayout = painted.plot;
  state.skyProjection = painted.projection;
  $('sky-empty').hidden = true;
  $('sky-mode').textContent = options.closeup ? '轨迹特写' : '地平线视图';
  fillSkySummary(state.sky);
}

function fillSkySummary(data) {
  if (!data) return;
  const summary = $('sky-summary');
  if (!summary) return;
  const offset = Number(data.tz_offset_hours) || 0;
  const local = skyLocalText(data.when_utc, offset, true);
  const moon = data.moon || {};
  const sun = data.sun || {};
  const geometry = data.geometry || {};
  const illum = Number(geometry.illumination_percent_topo
                       || geometry.illumination_percent_geo || 0);
  const elong = Number(geometry.elongation_deg) || 0;
  summary.textContent = `${local} · 月亮 ${fmt(moon.elevation_deg, 1)}° / `
    + `方位 ${fmt(moon.azimuth_deg, 1)}° · 照亮 ${illum.toFixed(1)}% · `
    + `距角 ${elong.toFixed(1)}° · 太阳高度 ${fmt(sun.elevation_deg, 1)}°`;
}

/** The sky window's own instant, as its three fields hold it. */
function skyFieldsValue() {
  const date = $('sky-date').value;
  if (!date) return '';
  const time = $('sky-time').value || '00:00';
  return `${date}T${time.slice(0, 5)}`;
}

/** Write an instant (``YYYY-MM-DDTHH:MM``) into the window's fields. */
function setSkyFields(localValue, offsetHours) {
  if (!localValue) return;
  const [date, time = '00:00'] = String(localValue).split('T');
  $('sky-date').value = date;
  $('sky-time').value = time.slice(0, 5);
  const offset = Number(offsetHours);
  if (Number.isFinite(offset)) $('sky-tz').value = String(offset);
}

/** Seed the window from the main page: the reader's own clock, until they
 *  move it.  Called when the window opens and by 此刻. */
function skyFieldsFromMain() {
  setSkyFields($('when').value, Number($('tz').value) || 0);
}

/** What the window asks the server for.
 *
 *  Its own date, time and zone -- the sky picture answers "where was the Moon
 *  at *this* moment", and the moment a reader wants to look at is often not the
 *  one the main page is showing (a sunset last week, a conjunction tonight).
 *  The observer's position still comes from the main page: one place, many
 *  times. */
function skyWhenPayload() {
  const local = skyFieldsValue();
  const offset = Number($('sky-tz').value);
  const tz = Number.isFinite(offset) ? offset : 0;
  let at = null;
  if (local) {
    const asUtc = Date.parse(local + ':00Z');
    if (Number.isFinite(asUtc)) {
      at = new Date(asUtc - tz * 3600 * 1000).toISOString().slice(0, 19);
    }
  }
  return {
    at,
    tz_offset_hours: tz,
    observer: { lat: Number($('lat').value), lon: Number($('lon').value) },
  };
}

/** Walk the window's instant by one nudge step, then redraw. */
function nudgeSky(step) {
  const next = shiftInstant(skyFieldsValue() || $('when').value, step);
  if (!next) return;
  setSkyFields(next);
  refreshSky();
}

/** Ask the server for the tracks the window is centred on. */
async function refreshSky() {
  const sequence = ++state.skySeq;
  $('sky-empty').hidden = false;
  $('sky-empty').textContent = '正在计算天空位置…';
  const span = skyClamp(Number($('sky-span').value) || 6, 0.25, 24);
  $('sky-span').value = String(span);
  try {
    const payload = await post('/api/sky', {
      when: skyWhenPayload(),
      span_hours: span,
      step_seconds: SKY_DEFAULT_STEP_SECONDS,
    });
    if (sequence !== state.skySeq) return;
    state.sky = payload;
    state.skySeries = skySeries(payload);
    if (!state.skyCloseupTouched) {
      $('sky-closeup').checked = skyAutoCloseup(payload);
    }
    state.skyView = null;
    drawSky();
  } catch (error) {
    if (sequence !== state.skySeq) return;
    state.sky = null;
    $('sky-empty').hidden = false;
    $('sky-empty').textContent = '天空位置计算失败：'
      + String((error && error.message) || error);
  }
}

function skyWindowBox() {
  const viewportWidth = window.innerWidth || 1280;
  const viewportHeight = window.innerHeight || 800;
  return { viewportWidth, viewportHeight };
}

function applySkyWindow(rect) {
  const box = skyWindowBox();
  // On a phone the viewport is narrower than the desktop minimum: the minimum
  // follows the viewport down rather than forcing the window off the screen.
  const minWidth = Math.min(SKY_MIN_WIDTH, Math.max(300, box.viewportWidth - 16));
  const minHeight = Math.min(SKY_MIN_HEIGHT, Math.max(260, box.viewportHeight - 16));
  const width = skyClamp(rect.width, minWidth, Math.max(minWidth, box.viewportWidth - 16));
  const height = skyClamp(rect.height, minHeight, Math.max(minHeight, box.viewportHeight - 16));
  const left = skyClamp(rect.left, 8, Math.max(8, box.viewportWidth - width - 8));
  const top = skyClamp(rect.top, 8, Math.max(8, box.viewportHeight - height - 8));
  const windowNode = $('sky-window');
  windowNode.style.left = left + 'px';
  windowNode.style.top = top + 'px';
  windowNode.style.width = width + 'px';
  windowNode.style.height = height + 'px';
  state.skyWindow = { left, top, width, height };
}

function positionSkyWindow(reset) {
  const box = skyWindowBox();
  if (reset || !state.skyWindow) {
    const maxWidth = Math.min(1180, Math.max(300, box.viewportWidth - 16));
    const maxHeight = Math.min(820, Math.max(260, box.viewportHeight - 16));
    const width = Math.round(skyClamp(box.viewportWidth * 0.78,
      Math.min(640, maxWidth), maxWidth));
    const height = Math.round(skyClamp(box.viewportHeight * 0.78,
      Math.min(480, maxHeight), maxHeight));
    state.skyWindow = {
      left: Math.round((box.viewportWidth - width) / 2),
      top: Math.round((box.viewportHeight - height) / 2),
      width,
      height,
    };
  }
  applySkyWindow(state.skyWindow);
}

function openSky() {
  state.skyOpen = true;
  // First open: start from the main page's instant.  Later opens keep whatever
  // the reader last set, because the window's time is its own.
  if (!skyFieldsValue()) skyFieldsFromMain();
  $('sky-backdrop').hidden = false;
  positionSkyWindow(false);
  $('sky-close').focus();
  refreshSky();
}

function closeSky() {
  state.skyOpen = false;
  state.skySeq += 1;
  state.skyDrag = null;
  state.skyHoverEdge = null;
  $('sky-backdrop').hidden = true;
  $('sky-open').focus();
}

/* ── mouse: window edges, plot edges, pan and zoom ─────────────────────── */

function skyCanvasPoint(event) {
  const canvas = $('sky-canvas');
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX || 0) - rect.left,
    y: (event.clientY || 0) - rect.top,
  };
}

function skyEdgeAt(x, y, plot) {
  const limit = 7;
  const distances = [
    ['left', Math.abs(x - plot.x)],
    ['right', Math.abs(x - (plot.x + plot.w))],
    ['top', Math.abs(y - plot.y)],
    ['bottom', Math.abs(y - (plot.y + plot.h))],
  ];
  let best = null;
  for (const [name, distance] of distances) {
    if (distance > limit) continue;
    if (!best || distance < best.distance) best = { name, distance };
  }
  if (!best) return null;
  if ((best.name === 'left' || best.name === 'right')
      && (y < plot.y || y > plot.y + plot.h)) return null;
  if ((best.name === 'top' || best.name === 'bottom')
      && (x < plot.x || x > plot.x + plot.w)) return null;
  return best.name;
}

function skyCanvasDown(event) {
  if (!state.sky || !state.skyLayout) return;
  event.preventDefault();
  const point = skyCanvasPoint(event);
  const plot = state.skyLayout;
  const edge = skyEdgeAt(point.x, point.y, plot);
  const inside = point.x >= plot.x && point.x <= plot.x + plot.w
    && point.y >= plot.y && point.y <= plot.y + plot.h;
  if (edge) {
    state.skyDrag = { kind: 'edge', edge, start: point, view: { ...state.skyView } };
  } else if (inside) {
    state.skyDrag = { kind: 'pan', start: point, view: { ...state.skyView } };
  }
}

function skyCanvasHover(event) {
  if (!state.skyLayout || state.skyDrag) return;
  const point = skyCanvasPoint(event);
  const edge = skyEdgeAt(point.x, point.y, state.skyLayout);
  if (edge !== state.skyHoverEdge) {
    state.skyHoverEdge = edge;
    drawSky();
  }
  const canvas = $('sky-canvas');
  canvas.style.cursor = edge
    ? (edge === 'left' || edge === 'right' ? 'ew-resize' : 'ns-resize')
    : 'crosshair';
}

function skyDragPlot(event) {
  const drag = state.skyDrag;
  if (!drag || !state.skyLayout) return;
  if (drag.kind !== 'edge' && drag.kind !== 'pan') return;
  const point = skyCanvasPoint(event);
  const plot = state.skyLayout;
  const view = drag.view;
  if (drag.kind === 'pan') {
    const projection = skyProjection(plot, view);
    const azAtStart = projection.az(drag.start.x);
    const elAtStart = projection.el(drag.start.y);
    const azNow = projection.az(point.x);
    const elNow = projection.el(point.y);
    const azShift = azAtStart - azNow;
    const elShift = elAtStart - elNow;
    state.skyView = {
      azMin: view.azMin + azShift,
      azMax: view.azMax + azShift,
      elMin: view.elMin + elShift,
      elMax: view.elMax + elShift,
    };
    drawSky();
    return;
  }
  const azSpan = view.azMax - view.azMin;
  const elSpan = view.elMax - view.elMin;
  const updated = { ...view };
  const dx = point.x - drag.start.x;
  const dy = point.y - drag.start.y;
  if (drag.edge === 'left') {
    updated.azMin = view.azMin + dx / Math.max(1, plot.w) * azSpan;
    updated.azMin = Math.min(updated.azMin, updated.azMax - 0.5);
    skyFitAspectFromAz(updated, plot.w / Math.max(1, plot.h));
  } else if (drag.edge === 'right') {
    updated.azMax = view.azMax + dx / Math.max(1, plot.w) * azSpan;
    updated.azMax = Math.max(updated.azMax, updated.azMin + 0.5);
    skyFitAspectFromAz(updated, plot.w / Math.max(1, plot.h));
  } else if (drag.edge === 'top') {
    updated.elMax = view.elMax - dy / Math.max(1, plot.h) * elSpan;
    updated.elMax = Math.max(updated.elMax, updated.elMin + 0.5);
    skyFitAspectFromEl(updated, plot.w / Math.max(1, plot.h));
  } else if (drag.edge === 'bottom') {
    updated.elMin = view.elMin - dy / Math.max(1, plot.h) * elSpan;
    updated.elMin = Math.min(updated.elMin, updated.elMax - 0.5);
    skyFitAspectFromEl(updated, plot.w / Math.max(1, plot.h));
  }
  state.skyView = updated;
  drawSky();
}

function skyCanvasWheel(event) {
  if (!state.sky || !state.skyLayout) return;
  event.preventDefault();
  const point = skyCanvasPoint(event);
  const plot = state.skyLayout;
  if (point.x < plot.x || point.x > plot.x + plot.w
      || point.y < plot.y || point.y > plot.y + plot.h) return;
  const view = state.skyView;
  const projection = skyProjection(plot, view);
  const azAt = projection.az(point.x);
  const elAt = projection.el(point.y);
  const scale = event.deltaY > 0 ? 1.16 : 1 / 1.16;
  const azSpan = (view.azMax - view.azMin) * scale;
  const elSpan = (view.elMax - view.elMin) * scale;
  // One factor for both axes, so a zoom never breaks the equal-scale promise;
  // below half a degree there is nothing left to resolve, so stop there.
  if (azSpan < 0.5 || elSpan < 0.5) return;
  const fx = (point.x - plot.x) / Math.max(1, plot.w);
  const fy = (point.y - plot.y) / Math.max(1, plot.h);
  state.skyView = {
    azMin: azAt - fx * azSpan,
    azMax: azAt + (1 - fx) * azSpan,
    elMin: elAt - (1 - fy) * elSpan,
    elMax: elAt + fy * elSpan,
  };
  drawSky();
}

function skyCanvasDoubleClick() {
  state.skyView = null;
  drawSky();
}

/* ── save the whole window as a PNG ───────────────────────────────────── */

function skyCanvasBlob(canvas, callback) {
  if (typeof canvas.toBlob === 'function') {
    canvas.toBlob((blob) => callback(blob), 'image/png');
    return;
  }
  const url = canvas.toDataURL('image/png');
  const comma = url.indexOf(',');
  const binary = atob(url.slice(comma + 1));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  callback(new Blob([bytes], { type: 'image/png' }));
}

/** The window's own chrome, plus the chart, at the requested resolution.
 *
 * The controls are drawn as a summary rather than as widgets: what a saved
 * picture needs is the state they are in, not eight form fields. */
function paintSkyExport(ctx, width, height, data, view, options) {
  ctx.fillStyle = '#171a21';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#39404e';
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, width - 1, height - 1);

  const offset = Number(data.tz_offset_hours) || 0;
  const local = skyLocalText(data.when_utc, offset, true);
  const geometry = data.geometry || {};
  const illum = Number(geometry.illumination_percent_topo
                       || geometry.illumination_percent_geo || 0);
  const elong = Number(geometry.elongation_deg) || 0;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#e6e8ee';
  ctx.font = '600 15px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText('天空位置示意图', 16, 24);
  ctx.fillStyle = '#98a0b0';
  ctx.font = '11px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText(`${local} · 照亮 ${illum.toFixed(1)}% · 距角 ${elong.toFixed(1)}°`, 16, 42);
  // The close affordance is part of the window; the saved picture keeps it so
  // the image reads as this window rather than as a loose chart.
  ctx.textAlign = 'right';
  ctx.fillStyle = '#98a0b0';
  ctx.font = '16px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText('×', width - 16, 27);
  ctx.textAlign = 'left';

  const mode = options.closeup ? '轨迹特写' : '地平线视图';
  ctx.fillStyle = '#7aa2f7';
  ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText(`${mode} · 时间标注 ${options.labels ? `${options.labelStep} min` : '关'}`
    + ` · 距角连线 ${options.elongation ? '开' : '关'}`, 16, 60);

  const area = { x: 12, y: 72, w: width - 24, h: height - 72 - 34 };
  const plot = skyPlotLayout(area);
  const exportView = { ...view };
  skyGrowAspect(exportView, plot.w / Math.max(1, plot.h));
  paintSkyChart(ctx, area, data, exportView,
                { ...options, view: exportView, interactive: false });

  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = '#98a0b0';
  ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
  ctx.fillText(fillSkySummaryText(data), 16, height - 17, width * 0.62);
  ctx.textAlign = 'right';
  ctx.fillText('实线 月亮 · 虚线 太阳 · 蓝色虚线 距角',
               width - 16, height - 17, width * 0.34);
}

function fillSkySummaryText(data) {
  const offset = Number(data.tz_offset_hours) || 0;
  const local = skyLocalText(data.when_utc, offset, true);
  const moon = data.moon || {};
  const sun = data.sun || {};
  const geometry = data.geometry || {};
  const illum = Number(geometry.illumination_percent_topo
                       || geometry.illumination_percent_geo || 0);
  const elong = Number(geometry.elongation_deg) || 0;
  return `${local} · 月亮 ${fmt(moon.elevation_deg, 1)}° / 方位 `
    + `${fmt(moon.azimuth_deg, 1)}° · 照亮 ${illum.toFixed(1)}% · `
    + `距角 ${elong.toFixed(1)}° · 太阳高度 ${fmt(sun.elevation_deg, 1)}°`;
}

function saveSkyPng() {
  if (!state.sky) {
    $('sky-empty').hidden = false;
    $('sky-empty').textContent = '还没有天空数据可保存。';
    return;
  }
  const rect = $('sky-window').getBoundingClientRect();
  const baseWidth = Math.max(480, rect.width || 960);
  const baseHeight = Math.max(380, rect.height || 680);
  const target = skyClamp(Number($('sky-save-px').value) || 2400, 320, 10000);
  $('sky-save-px').value = String(Math.round(target));
  const reference = $('sky-save-ref').value === 'short' ? 'short' : 'long';
  const longSide = Math.max(baseWidth, baseHeight);
  const shortSide = Math.min(baseWidth, baseHeight);
  const scale = target / (reference === 'short' ? shortSide : longSide);
  let width = Math.round(baseWidth * scale);
  let height = Math.round(baseHeight * scale);
  const ceiling = 10000;
  if (Math.max(width, height) > ceiling) {
    const shrink = ceiling / Math.max(width, height);
    width = Math.round(width * shrink);
    height = Math.round(height * shrink);
  }
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(320, width);
  canvas.height = Math.max(240, height);
  const ctx = canvas.getContext('2d');
  ctx.scale(canvas.width / baseWidth, canvas.height / baseHeight);
  const options = skyOptions();
  const plot = skyPlotLayout({ x: 0, y: 0, w: baseWidth, h: baseHeight });
  const view = skyEnsureView(plot, state.sky, options);
  paintSkyExport(ctx, baseWidth, baseHeight, state.sky, view, options);
  const offset = Number(state.sky.tz_offset_hours) || 0;
  const stamp = skyLocalText(state.sky.when_utc, offset, true).replace(/[: ]/g, '-');
  const name = `moon-sky-${stamp}.png`;
  skyCanvasBlob(canvas, (blob) => {
    if (!blob) return;
    downloadUrl(URL.createObjectURL(blob), name);
    $('sky-summary').textContent = `已保存 ${canvas.width}×${canvas.height} px · ${name}`;
  });
}

/* ── window resizing and dragging ──────────────────────────────────────── */

function skyResizeStart(event, direction) {
  event.preventDefault();
  const box = state.skyWindow || { left: 0, top: 0, width: 800, height: 600 };
  state.skyDrag = {
    kind: 'resize',
    direction,
    startX: event.clientX || 0,
    startY: event.clientY || 0,
    box: { ...box },
  };
}

function skyHeadDown(event) {
  const target = event.target || {};
  if (String(target.tagName || '').toUpperCase() === 'BUTTON') return;
  if (!state.skyWindow) return;
  event.preventDefault();
  state.skyDrag = {
    kind: 'move',
    startX: event.clientX || 0,
    startY: event.clientY || 0,
    box: { ...state.skyWindow },
  };
}

function skyPointerMove(event) {
  const drag = state.skyDrag;
  if (!drag) return;
  if (drag.kind === 'edge' || drag.kind === 'pan') {
    skyDragPlot(event);
    return;
  }
  if (drag.kind === 'resize') {
    const dx = (event.clientX || 0) - drag.startX;
    const dy = (event.clientY || 0) - drag.startY;
    const box = { ...drag.box };
    if (drag.direction.includes('e')) box.width += dx;
    if (drag.direction.includes('w')) { box.width -= dx; box.left += dx; }
    if (drag.direction.includes('s')) box.height += dy;
    if (drag.direction.includes('n')) { box.height -= dy; box.top += dy; }
    const viewport = skyWindowBox();
    if (box.width < SKY_MIN_WIDTH) {
      if (drag.direction.includes('w')) box.left -= SKY_MIN_WIDTH - box.width;
      box.width = SKY_MIN_WIDTH;
    }
    if (box.height < SKY_MIN_HEIGHT) {
      if (drag.direction.includes('n')) box.top -= SKY_MIN_HEIGHT - box.height;
      box.height = SKY_MIN_HEIGHT;
    }
    box.width = Math.min(box.width, viewport.viewportWidth - 8);
    box.height = Math.min(box.height, viewport.viewportHeight - 8);
    applySkyWindow(box);
    drawSky();
  } else if (drag.kind === 'move') {
    const dx = (event.clientX || 0) - drag.startX;
    const dy = (event.clientY || 0) - drag.startY;
    applySkyWindow({
      left: drag.box.left + dx,
      top: drag.box.top + dy,
      width: drag.box.width,
      height: drag.box.height,
    });
  }
}

function skyPointerUp() {
  state.skyDrag = null;
}

/** An adaptive scale bar in the top-right of the disk, when labels are on.
 *
 * Drawn in screen pixels rather than frame pixels: a ruler whose length changes
 * with the render resolution would be measuring the wrong thing, and this one
 * has to stay a fixed, readable size while still saying the truth about the
 * picture underneath it.
 */
/** Ask for the names in one frame; null when the request cannot be served. */
async function fetchLabels(frame, body) {
  try {
    return await post('/api/annotations', body);
  } catch (error) {
    if (frame === 'disk') {
      setStatus('地貌标注失败：' + String(error.message || error), 'error');
    }
    return null;
  }
}

/** Everything about *what* to label, as opposed to which frame it lands in.
 *
 * The class switches and the night-side switch live here rather than in the
 * plan, so changing either is one more request and nothing else: the frame is
 * the same frame, and only the selection of names in it has moved.
 */
function labelRequestBase() {
  const density = densitySettings();
  return {
    labels: density.labels,
    min_radius_px: density.min_radius_px,
    outline_min_radius_px: LIMITS.label_outline_px,
    language: labelLanguage(),
    classes: activeClasses(),
    only_illuminated: !$('label-shadow').checked,
  };
}

/** Is the reader holding the layout still while they tune it?
 *
 * The avoidance algorithm is a function of the text size: a bigger font means
 * bigger boxes, which means a different set of names fits and a different
 * arrangement of the ones that do.  That is the right answer to "lay these out
 * for me" and the wrong answer to "make the type bigger", so the lock freezes
 * the layout and lets the type move inside it. */
function labelLockOn() {
  const box = $('label-lock');
  return !!(box && box.checked);
}

/** What the layout of a view's plan is a function of -- deliberately *not* the
 *  font size.
 *
 * While the lock is on, a refresh whose key matches the key the plan was laid
 * out with keeps that plan.  Anything that changes *which* names are being
 * placed (the frame itself, the classes, the language, the density, the
 * day/night switch) changes the key and earns a fresh layout, which then
 * becomes the locked one.  A change of 字号, of the window size or of the zoom
 * does not, which is the whole point of the switch.
 */
function labelLayoutKey(viewName) {
  const frame = viewName === 'disk'
    ? `${DISK.width}|${JSON.stringify(state.diskWhen)}`
    : `${state.renderId}`;
  return JSON.stringify([frame, labelRequestBase()]);
}

/** The frame each frozen layout belongs to, as of the request that made it. */
const labelLockKeys = { disk: null, region: null };

/** The same layout at another resolution: every length in it times *k*.
 *
 * A locked layout is not re-laid-out for an export -- it *is* the layout the
 * reader chose -- so it scales with the picture, exactly as the names on a
 * paper map do.  Every field that carries a length is listed here on purpose:
 * the frame and the plan's font are lengths too.
 */
function scalePlan(plan, k) {
  if (!plan || !(k > 0) || k === 1) return plan;
  const frame = plan.frame || { width: DISK.width, height: DISK.height };
  return {
    ...plan,
    frame: { ...frame, width: frame.width * k, height: frame.height * k },
    font_px: (plan.font_px || 13) * k,
    items: (plan.items || []).map((item) => ({
      ...item,
      x: item.x * k,
      y: item.y * k,
      radius_px: (item.radius_px || 0) * k,
      label_x: item.label_x === null || item.label_x === undefined
        ? item.label_x : item.label_x * k,
      label_y: item.label_y === null || item.label_y === undefined
        ? item.label_y : item.label_y * k,
      label_w: (item.label_w || 0) * k,
      label_h: (item.label_h || 0) * k,
      outline: item.outline
        ? item.outline.map(([x, y]) => [x * k, y * k]) : item.outline,
    })),
  };
}

/** The frozen full-disk layout, when it describes the frame being exported.
 *
 * The plan is in the *preview's* pixels, so the caller scales it to the export;
 * if the reader has moved the clock since the lock was taken, the frozen layout
 * belongs to a frame nobody is looking at and the answer is ``null`` -- a fresh
 * request is the honest thing there.
 */
function lockedDiskPlan(when, width) {
  if (!labelLockOn() || !state.labels.disk) return null;
  if (labelLockKeys.disk !== labelLayoutKey('disk')) return null;
  if (JSON.stringify(when) !== JSON.stringify(state.diskWhen)) return null;
  return scalePlan(state.labels.disk, width / Math.max(1, DISK.width));
}

/** The plan for the full disk at an arbitrary render size.
 *
 * The frame's pixel counts scale with the picture, so every length the server
 * lays out with has to scale too -- otherwise a 10 000 px export would carry
 * the same absolute font, marker floor and rim threshold as the 1000 px
 * preview and come out looking like a different, much busier map.
 */
async function diskLabelsAt(width, when) {
  const scale = width / Math.max(1, DISK.width);
  const base = labelRequestBase();
  return await fetchLabels('disk', {
    // The instant the frame was rendered for, not whatever the observer fields
    // say now: an export rendered at 09:00 must not carry 08:00's names.
    when: when || state.diskWhen,
    frame: 'disk',
    preview: { width },
    ...base,
    min_radius_px: base.min_radius_px * scale,
    outline_min_radius_px: base.outline_min_radius_px * scale,
    font_px: labelFrameFontPx('disk') * scale,
  });
}

/** Re-ask for the names on both frames, and repaint.  Called on every change
 *  that can move a name: a new render, a new class switch, a new language.
 *
 * Two of these can be in flight at once -- pressing a class switch while a
 * render is landing is the ordinary case -- so each call takes a ticket and
 * only the newest one is allowed to write.  Without that, the slower answer
 * wins and the picture ends up captioned for the frame before it.
 *
 * With the layout locked, a view whose key still matches keeps the plan it has:
 * the request that would move the names is the one thing the lock is for.  Each
 * landing plan records the key it was laid out for, so unlocking -- or any
 * change that alters *what* is being placed -- re-flows immediately.
 */
let labelSeq = 0;

async function refreshLabels() {
  const seq = ++labelSeq;
  if (!state.labelsOn) {
    state.labels.disk = null;
    state.labels.region = null;
    labelLockKeys.disk = null;
    labelLockKeys.region = null;
    drawAllLabels();
    return;
  }
  const locked = labelLockOn();
  if (state.diskImage && state.diskWhen) {
    const key = labelLayoutKey('disk');
    if (!(locked && state.labels.disk && labelLockKeys.disk === key)) {
      const plan = await diskLabelsAt(DISK.width);
      if (seq !== labelSeq) return;
      if (plan) {
        state.labels.disk = plan;
        labelLockKeys.disk = key;
      }
    }
    drawViewLabels('disk');
  }
  if (state.rawImage && state.renderId) {
    const key = labelLayoutKey('region');
    if (!(locked && state.labels.region && labelLockKeys.region === key)) {
      const base = labelRequestBase();
      const plan = await fetchLabels('region', {
        frame: 'region',
        render_id: state.renderId,
        ...base,
        font_px: labelFrameFontPx('raw'),
      });
      if (seq !== labelSeq) return;
      if (plan) {
        state.labels.region = plan;
        labelLockKeys.region = key;
      }
    }
    drawViewLabels('raw');
    drawViewLabels('proc');
  }
}

/** Style-only changes never need the server: the plan is geometry. */
function refreshLabelPlans() {
  drawAllLabels();
  drawMagnifier();
  if (state.labelsOn) refreshLabels();
}

/** Re-derive the adaptive type size without moving a single name.
 *
 *  The plan's own ``font_px`` is the size its boxes were laid out for.  When
 *  the window (or the zoom) changes, the *paint* follows the new scale, but an
 *  unlocked plan keeps drawing at the old size -- so a picture magnified to read
 *  a crater ends up with type that no longer matches what the reader is doing.
 *  Re-laying out would move every name, including the ones they placed by hand.
 *  This is the third option: keep the plan -- positions, hidden names, the
 *  avoidance result -- and change only the number the glyphs are drawn at.
 */
function refreshLabelFontSizes() {
  if (!state.labelsOn) {
    setStatus('先打开「月面地貌标注」，再刷新字体', 'error');
    return;
  }
  let updated = 0;
  if (state.labels.disk) {
    state.labels.disk = {
      ...state.labels.disk,
      font_px: labelFrameFontPx('disk'),
    };
    updated += 1;
  }
  if (state.labels.region) {
    state.labels.region = {
      ...state.labels.region,
      font_px: labelFrameFontPx('raw'),
    };
    updated += 1;
  }
  drawAllLabels();
  drawMagnifier();
  setStatus(updated
    ? `标注字体已按当前视口刷新（${Math.round(labelScreenFontPx('disk'))} px），布局未动`
    : '还没有标注可刷新：先打开「月面地貌标注」');
}

function toggleLabels() {
  state.labelsOn = !state.labelsOn;
  $('annotate').setAttribute('aria-pressed', state.labelsOn ? 'true' : 'false');
  if (!state.labelsOn) {
    state.labels.disk = null;
    state.labels.region = null;
    drawAllLabels();
    setStatus('已关闭月面地貌标注');
    return;
  }
  setStatus('正在投影月面地貌…', 'busy');
  // A label refresh that fails used to do so silently: the switch went on, the
  // status line kept saying it was working, and the picture stayed bare.  Say
  // what happened instead -- and say it when the answer is "nothing to label",
  // which is a result rather than a failure.
  refreshLabels()
    .then(() => {
      const plan = state.labels[state.resultTab === 'proc' ? 'region' : 'disk']
        || state.labels.disk;
      const count = plan && plan.items
        ? plan.items.filter((item) => item.labelled).length : 0;
      if (!state.labels.disk) { setStatus('就绪'); return; }
      setStatus(count > 0 ? '就绪'
        : '地貌标注：本帧没有符合条件的名字，可以把「密度」往右拉', 'error');
    })
    .catch((error) => setStatus('地貌标注失败：' + String(error.message || error), 'error'));
}

/* ── mouse interaction on the disk ───────────────────────────────── */

/* Cursor -> preview-image pixels.  The stack and the Moon canvas share a box,
   so one rect serves for both. */
function canvasPoint(event) {
  const body = $('body-disk').getBoundingClientRect();
  const f = stageFrame();
  const { zoom, panX, panY } = diskTransform();
  // Undo the stage offset, the pan and the zoom about the stage centre, then
  // map back to preview-image pixels.
  const cssX = event.clientX - body.left - f.offsetX;
  const cssY = event.clientY - body.top - f.offsetY;
  const bx = (cssX - panX - f.stageW / 2) / zoom + f.stageW / 2;
  const by = (cssY - panY - f.stageH / 2) / zoom + f.stageH / 2;
  return { x: bx * DISK.width / f.stageW, y: by * DISK.height / f.stageH };
}

/** True when the gesture should move the view rather than edit the selection. */
function wantsPan(event) {
  return event.button === 1 || event.shiftKey;
}

$('body-disk').addEventListener('pointerdown', (event) => {
  if (!state.geometry) return;
  // The preview controls and the zoom cluster float over the picture; a press
  // on one of them is a press on a control, not the start of a selection box.
  const target = event.target;
  if (target && target.closest && target.closest('.view-tools, .zoom')) return;
  if (wantsPan(event)) {
    state.panning = { x: event.clientX, y: event.clientY };
    $('body-disk').setPointerCapture(event.pointerId);
    $('body-disk').style.cursor = 'grabbing';
    event.preventDefault();
    return;
  }
  // A press on the ruler or on a name is a press on *that*, not the start of a
  // new selection box: the box is drawn anywhere on the disk, so without this
  // the two gestures would be the same gesture.
  if (overlayDown('disk', event)) {
    $('body-disk').setPointerCapture(event.pointerId);
    event.preventDefault();
    return;
  }
  const p = canvasPoint(event);
  const { cx, cy, radius } = diskGeom();
  const hit = state.selection ? hitTest(p) : null;

  if (hit === 'rotate') {
    // Angle from the box centre to the pointer, so the handle tracks it.
    const centre = lonLatToPixel(state.selection.lon, state.selection.lat);
    state.dragging = {
      mode: 'rotate', start: { ...state.selection },
      grabAngle: Math.atan2(p.y - centre.y, p.x - centre.x),
    };
  } else if (hit === 'corner') {
    // Resizing pivots on the corner diagonally opposite the one grabbed, which
    // is what makes a corner drag feel like a corner drag.  (It used to measure
    // from the *Moon's* centre, so the box jumped to the distance between the
    // cursor and the middle of the disk.)
    const box = boxGeometry();
    const grabbed = screenToBox(box, p.x - box.centre.x, p.y - box.centre.y);
    // The anchor is the corner diagonally opposite the grabbed one.
    const anchor = boxToScreen(
      box, -Math.sign(grabbed.u) * box.halfW, -Math.sign(grabbed.v) * box.halfH
    );
    state.dragging = {
      mode: 'corner',
      start: { ...state.selection },
      anchor,
      rotation: box.rotation,
    };
  } else if (hit && hit.startsWith('edge')) {
    // The frame is frozen at the press: the pointer's own movement is what
    // sizes the box, and re-measuring it mid-drag would chase its own tail.
    const box = boxGeometry();
    state.dragging = {
      mode: hit,
      start: { ...state.selection },
      frame: { centre: box.centre, rotation: box.rotation,
               halfW: box.halfW, halfH: box.halfH },
    };
  } else if (hit === 'move') {
    state.dragging = { mode: 'move', origin: p, start: { ...state.selection } };
  } else {
    const target = pixelToLonLat(p.x, p.y);
    if (!target) return;
    // A fresh box grows outwards from where the drag started; it must not
    // follow the cursor, or the selection slides across the Moon as you draw it.
    // Corner to corner, the way every other rectangle tool works: the press
    // fixes one corner and the drag sweeps out the opposite one.
    state.dragging = { mode: 'new', anchorPx: p };
    state.selection = {
      lon: target.lon, lat: target.lat, radiusFraction: 0.01, rotationDeg: 0,
      aspect: lockActive() ? cameraAspect() : 1,
    };
  }
  $('body-disk').setPointerCapture(event.pointerId);
  drawDisk();
  event.preventDefault();
});

$('body-disk').addEventListener('pointermove', (event) => {
  if (state.panning) {
    panBy('disk', event.clientX - state.panning.x, event.clientY - state.panning.y);
    state.panning = { x: event.clientX, y: event.clientY };
    event.preventDefault();
    return;
  }
  if (overlayMove(event)) { event.preventDefault(); return; }
  const drag = state.dragging;
  if (!drag) {
    updateCursor(event);
    return;
  }
  const p = canvasPoint(event);
  const { cx, cy, radius } = diskGeom();
  const start = drag.start;

  if (drag.mode === 'rotate') {
    const centre = lonLatToPixel(start.lon, start.lat);
    const angle = Math.atan2(p.y - centre.y, p.x - centre.x);
    let deg = start.rotationDeg - (angle - drag.grabAngle) * 180 / Math.PI;
    deg = ((deg % 360) + 540) % 360 - 180;
    state.selection = { ...start, rotationDeg: deg };
    $('sel-rotation').value = deg.toFixed(1);
  } else if (drag.mode === 'new') {
    // The rectangle spanned by the anchor and the cursor, in image pixels.
    let halfW = Math.abs(p.x - drag.anchorPx.x) / 2;
    let halfH = Math.abs(p.y - drag.anchorPx.y) / 2;
    // A drawn box keeps the shape it was drawn with -- it is only the camera
    // lock that forces a proportion on it.
    if (lockActive()) {
      const aspect = cameraAspect();
      if (halfH > halfW * aspect) halfW = halfH / Math.max(aspect, 1e-6);
      else halfH = halfW * aspect;
    }
    halfW = Math.max(6, halfW);
    halfH = Math.max(6, halfH);
    const midX = (p.x + drag.anchorPx.x) / 2;
    const midY = (p.y + drag.anchorPx.y) / 2;
    const mid = pixelToLonLat(midX, midY);
    if (mid) {
      state.selection = {
        lon: mid.lon, lat: mid.lat,
        radiusFraction: Math.max(0.004, Math.min(1.4, Math.min(halfW, halfH) / radius)),
        rotationDeg: 0,
        aspect: halfH / Math.max(1e-6, halfW),
      };
    }
  } else if (drag.mode.startsWith('edge')) {
    const next = resizeByEdge(drag, p, radius);
    if (next) state.selection = next;
  } else if (drag.mode === 'corner') {
    // Work in the box's own frame so a rotated box resizes along its own axes.
    // A corner keeps the aspect: it scales the box, it does not reshape it.
    const aspect = shapeAspect();
    const frame = { centre: { x: 0, y: 0 }, rotation: drag.rotation };
    const local = screenToBox(frame, p.x - drag.anchor.x, p.y - drag.anchor.y);
    let halfW = Math.abs(local.u) / 2;
    let halfH = Math.abs(local.v) / 2;
    if (halfH > halfW * aspect) halfW = halfH / Math.max(aspect, 1e-6);
    else halfH = halfW * aspect;
    // The anchor stays put, so the centre is half way along the new diagonal.
    const mid = boxToScreen(
      { centre: drag.anchor, rotation: drag.rotation },
      Math.sign(local.u) * halfW, Math.sign(local.v) * halfH
    );
    const midLonLat = pixelToLonLat(mid.x, mid.y);
    if (midLonLat) {
      state.selection = {
        ...start,
        lon: midLonLat.lon, lat: midLonLat.lat,
        radiusFraction: Math.max(0.004,
          Math.min(1.4, Math.min(halfW, halfH) / radius)),
        aspect,
      };
    }
  } else {
    const target = pixelToLonLat(p.x, p.y);
    const origin = pixelToLonLat(drag.origin.x, drag.origin.y);
    if (target && origin) {
      state.selection = {
        ...start,
        lon: wrap180(start.lon + (target.lon - origin.lon)),
        lat: Math.max(-89.9, Math.min(89.9, start.lat + (target.lat - origin.lat))),
      };
    }
  }
  syncSelectionInputs();
  drawDisk();
  drawMagnifier();          // the window is locked to the box, so it follows it
  event.preventDefault();
});

function endDrag(event) {
  if (state.panning) {
    state.panning = null;
    $('body-disk').style.cursor = '';
    return;
  }
  if (overlayUp()) { drawAllLabels(); return; }
  if (!state.dragging) return;
  state.dragging = null;
  syncSelectionInputs();
  drawDisk();
  afterSelectionChange();
}

$('body-disk').addEventListener('pointerup', endDrag);
$('body-disk').addEventListener('pointercancel', endDrag);

/** *p* is in preview-image pixels; the comparison happens on screen, so the
 *  grab targets stay the same size however far the picture is zoomed. */
function hitTest(p) {
  const box = boxGeometry();
  if (!box) return null;
  const dpr = window.devicePixelRatio || 1;
  const centre = imageToOverlay(box.centre.x, box.centre.y);
  const halfW = imageToOverlay(box.halfW, 0).x - imageToOverlay(0, 0).x;
  const halfH = imageToOverlay(0, box.halfH).y - imageToOverlay(0, 0).y;
  const here = imageToOverlay(p.x, p.y);

  const gap = HANDLE_GAP * dpr;
  const radius = HANDLE_RADIUS * dpr;
  const topLocal = boxToScreen(box, 0, -box.halfH);
  const top = imageToOverlay(topLocal.x, topLocal.y);
  const dxu = top.x - centre.x, dyu = top.y - centre.y;
  const len = Math.max(1e-6, Math.hypot(dxu, dyu));
  const hx = top.x + (dxu / len) * gap;
  const hy = top.y + (dyu / len) * gap;
  if (Math.hypot(here.x - hx, here.y - hy) <= radius + 6 * dpr) return 'rotate';

  const local = screenToBox(box, here.x - centre.x, here.y - centre.y);
  const u = local.u, v = local.v;
  const slack = 8 * dpr;
  const near = (a, b) => Math.abs(a - b) < 10 * dpr + slack;
  const insideU = Math.abs(u) <= halfW + slack;
  const insideV = Math.abs(v) <= halfH + slack;
  if (near(Math.abs(u), halfW) && near(Math.abs(v), halfH)) return 'corner';
  // One side at a time: dragging an edge changes that dimension alone, which is
  // the only way to get a box that is not the shape it started as.
  if (near(Math.abs(u), halfW) && insideV) return u > 0 ? 'edge-r' : 'edge-l';
  if (near(Math.abs(v), halfH) && insideU) return v > 0 ? 'edge-b' : 'edge-t';
  if (Math.abs(u) < halfW && Math.abs(v) < halfH) return 'move';
  return null;
}

/** Resize the box by one edge: that side follows the pointer, the opposite one
 *  stays where it is, so the centre moves by half of what the side does. */
function resizeByEdge(drag, p, radiusPx) {
  const frame = drag.frame;
  const local = screenToBox(frame, p.x - frame.centre.x, p.y - frame.centre.y);
  const minHalf = 6;
  let halfW = frame.halfW;
  let halfH = frame.halfH;
  let shiftU = 0;
  let shiftV = 0;
  if (drag.mode === 'edge-r' || drag.mode === 'edge-l') {
    const edge = drag.mode === 'edge-r' ? local.u : -local.u;
    const half = Math.max(minHalf, 0.5 * (edge + frame.halfW));
    shiftU = (edge - frame.halfW) / 2;
    halfW = half;
  } else {
    const edge = drag.mode === 'edge-b' ? local.v : -local.v;
    const half = Math.max(minHalf, 0.5 * (edge + frame.halfH));
    shiftV = (edge - frame.halfH) / 2;
    halfH = half;
  }
  const centre = boxToScreen(frame, shiftU, shiftV);
  const mid = pixelToLonLat(centre.x, centre.y);
  if (!mid) return null;
  return {
    ...drag.start,
    lon: mid.lon,
    lat: mid.lat,
    radiusFraction: Math.max(0.004,
      Math.min(1.4, Math.min(halfW, halfH) / radiusPx)),
    aspect: halfH / Math.max(1e-6, halfW),
  };
}

function updateCursor(event) {
  const p = canvasPoint(event);
  // The ruler and the names are grabbed in device pixels, like the × handles
  // they carry, so the pointer says what it can do wherever the picture is
  // zoomed to.
  const point = overlayPoint('disk', event);
  const rulerHit = point ? rulerHitTest(point) : null;
  if (rulerHit) {
    $('body-disk').style.cursor =
      rulerHit === 'ruler-move' ? 'move' : 'ew-resize';
    return;
  }
  if (point && labelHitTest('disk', point)) {
    $('body-disk').style.cursor = 'move';
    return;
  }
  const hit = state.selection ? hitTest(p) : null;
  const cursors = {
    rotate: 'grab', corner: 'nwse-resize', move: 'move',
    'edge-l': 'ew-resize', 'edge-r': 'ew-resize',
    'edge-t': 'ns-resize', 'edge-b': 'ns-resize',
  };
  $('body-disk').style.cursor = cursors[hit] || 'crosshair';
}

function wrap180(x) {
  while (x > 180) x -= 360;
  while (x < -180) x += 360;
  return x;
}

function syncSelectionInputs() {
  const s = state.selection;
  if (!s) {
    $('sel-lon').value = ''; $('sel-lat').value = '';
    $('sel-rotation').value = '0';
    return;
  }
  $('sel-lon').value = s.lon.toFixed(3);
  $('sel-lat').value = s.lat.toFixed(3);
  $('sel-rotation').value = (s.rotationDeg || 0).toFixed(1);
}

/* ── 同步选框到此视场 ──────────────────────────────────────────────────────
 *
 * A close-up is aimed by dragging a box, and the moment the render lands the
 * reader starts exploring: they search a name, drag the corner, try a different
 * crater -- and the frame they actually spent two minutes rendering is gone
 * with no way back to it.  So the box the last render was made with is kept,
 * and one button puts it back.  It is deliberately *not* a history: "the one I
 * just made" is the frame being referred to, and a list of twelve would be a
 * different feature with a different name.
 */
function rememberRenderSelection() {
  const selection = state.selection;
  if (!selection) return;
  state.lastRenderSelection = { ...selection };
  updateSelectionSyncButton();
}

/** Where the remembered box is, in the words the readout uses. */
function describeSelection(selection) {
  if (!selection) return '';
  const arcsec = state.geometry
    ? selection.radiusFraction * (state.geometry.angular_diameter_deg / 2) * 3600
    : null;
  const radius = arcsec === null ? '' : `，半径 ${arcsec.toFixed(1)}″`;
  return `${selection.lon.toFixed(3)}°, ${selection.lat.toFixed(3)}°${radius}`;
}

function sameSelection(left, right) {
  if (!left || !right) return false;
  return Math.abs(left.lon - right.lon) < 1e-9
    && Math.abs(left.lat - right.lat) < 1e-9
    && Math.abs(left.radiusFraction - right.radiusFraction) < 1e-9
    && Math.abs((left.rotationDeg || 0) - (right.rotationDeg || 0)) < 1e-9;
}

/** The button is greyed until there is a frame to go back to -- and, once the
 *  box *is* that frame, it says so rather than doing nothing quietly. */
function updateSelectionSyncButton() {
  const button = $('sel-sync-last');
  if (!button) return;
  const saved = state.lastRenderSelection;
  button.disabled = !saved;
  const back = sameSelection(saved, state.selection);
  button.setAttribute('aria-pressed', String(!!saved && back));
  button.title = saved
    ? (back ? '选框就是上一次生成模拟时的设置'
      : `把选框复位到 ${describeSelection(saved)}（上一次生成模拟时用的设置）`)
    : '还没有生成过模拟：先生成一帧近景，这里就会记住那次用的选框';
}

function syncBackToLastSelection() {
  const saved = state.lastRenderSelection;
  if (!saved) return;
  state.selection = { ...saved };
  syncSelectionInputs();
  afterSelectionChange();
  updateSelectionSyncButton();
  setStatus(`选框已复位到上一次生成模拟时的设置（${describeSelection(saved)}）`);
}

/** Re-derive the box from the numeric inputs, for typed edits.
 *
 * The size is no longer one of them.  A field radius was the third way to say
 * how big the box is -- after drawing it and after the physical-resolution row,
 * which is the one that also says what a pixel is worth -- and the one place it
 * was *needed* was nowhere.  Typing a centre still moves the box, at the size it
 * already has; with no box at all it opens one at a modest default. */
function selectionFromInputs() {
  const lon = num('sel-lon'), lat = num('sel-lat');
  if (lon === null || lat === null || !state.geometry) {
    state.selection = null;
    return;
  }
  const kept = state.selection;
  state.selection = {
    lon, lat,
    radiusFraction: kept ? kept.radiusFraction : 0.08,
    rotationDeg: (kept && kept.rotationDeg) || 0,
    aspect: (kept && kept.aspect) || cameraAspect(),
  };
}

/* ── server calls ────────────────────────────────────────────────── */

let diskSeq = 0;
let diskRetry = false;

async function refreshDisk() {
  const seq = ++diskSeq;
  busy('disk-busy', true);
  setStatus('渲染月面…', 'busy');
  try {
    const when = whenPayload();
    const payload = await post('/api/disk', {
      when,
      ...lightPayload(),
      ...computePayload(),
      preview: {
        width: previewPixels(),
        samples: previewSamples(),
        shadow_steps: 24,
      },
    });
    if (seq !== diskSeq) return;              // a newer request won
    state.geometry = payload.geometry;
    state.diskWhen = when;                    // labels describe *this* preview
    state.diskImage = await loadImage(payload.image);
    state.diskExport = null;                  // the cached export is another instant
    state.downloads.disk = payload.image;
    updateDownloadLink();
    // The canvas has to learn the new frame's size *before* anything is drawn
    // from it: the close-up window works in the frame's pixels, so drawing it
    // against the previous size showed a patch of Moon beside the one the box
    // encloses -- until some later redraw put the two back in step.
    sizeCanvas(DISK, state.diskImage);
    fitCanvas(views.disk);
    drawDisk();
    drawMagnifier();
    drawPhaseStrip();
    describeDisk(payload.geometry);
    updateDiskScale();
    syncKmPxToDisk();
    setStatus('就绪');
    await refreshLabels();
  } catch (error) {
    setStatus(String(error.message || error), 'error');
    // The first request of a session can meet a server that is still coming up
    // (a restart, a browser that opened on a stale tab).  One quiet retry is
    // cheaper than a page that looks broken until someone presses a button.
    if (!state.diskImage && !diskRetry) {
      diskRetry = true;
      setTimeout(refreshDisk, 2500);
    }
  } finally {
    if (seq === diskSeq) busy('disk-busy', false);
  }
}

/** The one line under the full disk: how big the picture is, what a pixel is
 *  worth on the ground, the earthshine it was drawn with, and the two gestures
 *  that are not obvious -- the last of which is pushed to the right edge, out
 *  of the way of the numbers.
 *
 *  The render's own timing and thread count used to be here; they describe the
 *  machine rather than the picture, and nobody aims a selection with them. */
function describeDisk(g) {
  fillMoonInfo(g);
  const px = state.diskImage ? `${state.diskImage.width}×${state.diskImage.height} px` : '—';
  const kmPerPx = diskKmPerPixel();
  $('disk-info-size').textContent = `${px}　` +
    (kmPerPx ? `1 px = ${groundScaleText(kmPerPx)}` : '');
  describeEarthshine(g.earthshine, g);
}

/** Kilometres of lunar surface per pixel of the *whole disk* picture.
 *
 * The disk covers the Moon's apparent diameter, so the scale is the linear
 * diameter over the disc's own pixel diameter -- no selection needed, which is
 * what makes it the right number for the whole-disk preview. */
function diskKmPerPixel(width) {
  const g = state.geometry;
  if (!g) return null;
  const px = width ? Number(width) : (state.diskImage ? state.diskImage.width : DISK.width);
  // One measurement, in one place: the disk frame is square, so a width *is* a
  // geometry, and the ruler and the footer cannot disagree about the scale.
  return diskKmPerFramePixel(diskGeomFor(px, px));
}

/** Keep the 物理分辨率 menu honest about what this machine can actually do.
 *
 * Two things are wrong with a menu written by hand against a published figure:
 *
 * 1. The 0.118 km/px row is the *documented* LOLA pixel size, but the floor the
 *    page enforces comes from the raster that is on disk (`terrain_km_per_px`),
 *    and that is usually a shade coarser (0.1185 here).  Picking 0.118 then
 *    clamped the number to the floor, the clamped value matched no option, and
 *    the menu fell to 自定义 with a second field beside it -- the bug where the
 *    input "turns into two inputs".
 * 2. The finer rows (30 m, 60 m) are below that floor as well, so they could
 *    never be honoured.  They are disabled rather than silently rewritten: an
 *    option that quietly does something else is worse than one that says it
 *    cannot.
 */
function syncKmPxPresets() {
  const select = $('out-kmpx-preset');
  const floor = Number(LIMITS.terrain_km_per_px);
  if (!select || !Number.isFinite(floor) || floor <= 0) return;
  for (const option of [...select.options]) {
    if (option.value === 'disk' || option.value === 'custom') continue;
    const value = Number(option.value);
    if (!Number.isFinite(value)) continue;
    // The LOLA row is the one labelled as the limit; it carries the measured
    // one from here on.
    if (/LOLA/.test(String(option.textContent || ''))) {
      option.value = floor.toFixed(4);
      option.textContent = `${option.value} km/px · LOLA 上限`;
      continue;
    }
    const tooFine = value < floor - 1e-9;
    option.disabled = tooFine;
    const base = String(option.textContent || '').split('（')[0];
    option.textContent = tooFine
      ? `${base}（本地数据下限 ${floor.toFixed(3)}，不可用）`
      : base;
  }
}

/* ── 数据源 / data source ────────────────────────────────────────────── */

/** "59.2 m/px（512 ppd）": one string for a product's resolution. */
function dataSourceText(source) {
  const metres = source.metres_per_px || source.km_per_px * 1000;
  const ppd = source.pixels_per_degree ? ` · ${source.pixels_per_degree} ppd` : '';
  return `${metres.toFixed(metres < 100 ? 1 : 0)} m/px${ppd}`;
}

/** The paragraph under the menu: what the current choice is, and what it costs
 *  if it is not on this machine yet.
 *
 *  A menu that only names products leaves the reader guessing which one the
 *  picture is made of; this line is where the answer goes, and it is also where
 *  an undownloaded source says how to get it. */
function updateDataSourceNote() {
  const select = $('data-source');
  const note = $('data-source-note');
  if (!select || !note) return;
  const payload = LIMITS.data_sources;
  const value = select.value;
  if (value && value.startsWith('online:')) {
    const entry = (payload && payload.online || [])
      .find((item) => `online:${item.id}` === value);
    note.textContent = entry ? `${entry.note} 下载：${entry.fetch}` : '';
    return;
  }
  const source = (payload && payload.sources || []).find((item) => item.id === value);
  if (!source) { note.textContent = ''; return; }
  // Where the two pictures come from, said plainly: a finer product sharpens
  // close-ups immediately, while the full disk keeps its existing pyramid (at
  // the 10000 px ceiling it is already sampling a 474 m level, so a finer
  // product would change nothing there).
  const pyramid = payload && payload.pyramid ? `；整月面仍用 ${payload.pyramid}` : '';
  note.textContent = `${dataSourceText(source)} · 近景与分辨率上限 ` +
    `${source.km_per_px.toFixed(4)} km/px${pyramid}`;
}

/** Build the 数据源 menu from what the server can actually draw.
 *
 *  Two groups, and the difference between them is the point: 本地 entries are
 *  files this machine parsed as a global raster, so their resolution is
 *  *measured*; 在线 entries are products the project knows how to fetch but
 *  does not have.  The second group is disabled rather than hidden -- seeing
 *  "SLDEM2015 · 59 m（未下载）" beside the fetch command is what turns an
 *  abstract recommendation into a next step.
 */
function renderDataSources(payload) {
  const select = $('data-source');
  if (!select || !payload) return;
  const sources = payload.sources || [];
  const selected = payload.selected || (sources[0] && sources[0].id) || '';
  // A menu with no products is a menu that explains itself instead: an empty
  // <select> beside "数据源" reads as a broken control, not as a missing file.
  select.textContent = '';
  if (!sources.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '（没有找到全球高程栅格）';
    option.disabled = true;
    select.appendChild(option);
    select.disabled = true;
    updateDataSourceNote();
    return;
  }
  select.disabled = false;
  const local = document.createElement('optgroup');
  local.label = '本地';
  for (const source of sources) {
    const option = document.createElement('option');
    option.value = source.id;
    option.textContent = `${source.name} · ${dataSourceText(source)}`;
    option.title = `${source.id}\n${(source.bytes / 1e9).toFixed(1)} GB`;
    local.appendChild(option);
  }
  select.appendChild(local);
  if (payload.online && payload.online.length) {
    const group = document.createElement('optgroup');
    group.label = '在线（需下载）';
    for (const entry of payload.online) {
      const option = document.createElement('option');
      option.value = `online:${entry.id}`;
      option.disabled = true;
      option.textContent = `${entry.label} · ${dataSourceText(entry)}（按选框下载）`;
      option.title = entry.note || '';
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  select.value = selected;
  updateDataSourceNote();
  renderPatchProducts(payload.online || []);
}

/** The 区域高清补片 menu: the online products, and what a patch would cost. */
function renderPatchProducts(online) {
  const select = $('patch-product');
  if (!select) return;
  select.textContent = '';
  for (const entry of online) {
    const option = document.createElement('option');
    option.value = entry.id;
    option.textContent = `${entry.label} · ${entry.metres_per_px} m`;
    option.title = entry.note;
    select.appendChild(option);
  }
  if (!online.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '（没有可下载的在线产品）';
    option.disabled = true;
    select.appendChild(option);
  }
  select.disabled = !online.length;
}

/** 下载此区域: fetch a patch of the online product covering the selection.
 *
 *  The whole point is that the download follows the *selection*: a close-up of
 *  one crater needs a few degrees, not a 34 GB global product.  The server
 *  measures the frame's real footprint (a limb view is foreshortened) and adds
 *  the same margin the window cutter uses, so what arrives is exactly what the
 *  renderer will ask for.
 */
async function downloadPatch() {
  const product = $('patch-product') ? $('patch-product').value : '';
  if (!product) return;
  const selection = selectionPayload();
  if (!selection) {
    setStatus('先在地图上框选一块区域，再下载这一块的高清数据。', 'error');
    return;
  }
  const button = $('patch-download');
  const note = $('patch-note');
  if (button) button.disabled = true;
  if (note) note.textContent = '正在测量选框覆盖的地面…';
  setStatus('区域补片：正在计算范围…', 'busy');
  try {
    const job = await post('/api/patch', {
      product,
      when: whenPayload(),
      selection,
      output: { width: 600, height: 600 },
    });
    if (note) {
      note.textContent = `已排入队列：约 ${job.megabytes} MB，` +
        `经度 ${job.bounds[0].toFixed(2)}–${job.bounds[1].toFixed(2)}°、` +
        `纬度 ${job.bounds[2].toFixed(2)}–${job.bounds[3].toFixed(2)}°`;
    }
    let state = null;
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      state = await getJson(`/api/patch/${job.id}`);
      if (note) {
        const pct = Math.round((state.progress || 0) * 100);
        note.textContent = `${state.message || ''}（${pct}%）`;
      }
      if (state.state !== 'running') break;
    }
    if (state.state === 'done') {
      if (note) {
        note.textContent = `补片就绪：${state.message}，` +
          `${(state.bytes / 1e6).toFixed(1)} MB。近景会自动改用它。`;
      }
      setStatus('区域补片：就绪，重新渲染近景…', 'ok');
      // Redraw the close-up against the new patch.  A failure here must not
      // overwrite the message that says the download succeeded.
      try {
        const px = previewPixels();
        await renderRegion(px, px, 1, null);
      } catch (error) {
        setStatus('补片已就绪，但近景重渲染失败：' + String(error.message || error),
                  'error');
      }
    } else {
      if (note) note.textContent = state.error || '下载失败';
      setStatus('区域补片下载失败', 'error');
    }
  } catch (error) {
    if (note) note.textContent = String(error.message || error);
    setStatus('区域补片下载失败：' + String(error.message || error), 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

/** Switch the product every later frame is drawn from.
 *
 *  The reply carries the new floor, and the floor is what decides which
 *  物理分辨率 rows are live: 118 m can honour 0.118 km/px and nothing finer, so
 *  the 0.06 row only lights up once a 59 m source is the one in use. */
async function chooseDataSource(value) {
  const select = $('data-source');
  if (!select || !value || value.startsWith('online:')) {
    // An online row is a note about how to get a product, not a choice: put the
    // menu back where it was so it keeps naming what the frame is made of.
    renderDataSources(LIMITS.data_sources || { sources: [] });
    return;
  }
  select.disabled = true;
  setStatus('切换数据源…', 'busy');
  try {
    const payload = await post('/api/source', { id: value });
    LIMITS.terrain_km_per_px =
      Number(payload.floor_km_per_px) || LIMITS.terrain_km_per_px;
    const previous = LIMITS.data_sources || { sources: [], online: [] };
    LIMITS.data_sources = {
      ...previous,
      sources: payload.sources || [],
      selected: payload.selected || null,
      floor_km_per_px: payload.floor_km_per_px,
      pyramid: payload.pyramid || previous.pyramid,
    };
    renderDataSources(LIMITS.data_sources);
    syncKmPxPresets();
    // The ladder changed (the LOLA row now carries the measured floor, too-fine
    // rows are disabled), so the animation's copy of it is rebuilt: a copy made
    // once at start-up would keep offering rows this machine cannot honour.
    copyPresetOptions('out-kmpx-preset', 'anim-kmpx-preset', true);
    syncCopiedPresets('out-kmpx-preset', 'anim-kmpx-preset');
    $('out-kmpx').min = String(LIMITS.terrain_km_per_px);
    $('out-kmpx').title =
      `最细 ${LIMITS.terrain_km_per_px.toFixed(4)} km/px（当前数据源的分辨率）`;
    $('out-scale').min = String(minArcsecPerPixel().toFixed(4));
    setStatus(`数据源：近景可画到 ${(payload.floor_km_per_px * 1000).toFixed(1)} m/px`,
              'ok');
    // The full disk is *not* redrawn: it is made from its own pyramid, which
    // this switch deliberately leaves alone.  What moves is the close-up
    // window's terrain and the floor under 物理分辨率, so the numbers that read
    // those are refreshed and nothing else is.
    refreshAnimationPlan();
    updateDataSourceNote();
  } catch (error) {
    setStatus('切换数据源失败：' + String(error.message || error), 'error');
    updateDataSourceNote();
  } finally {
    select.disabled = false;
  }
}

/** 物理分辨率 starts synced to the full disk's own scale, and stays there until
 *  the reader picks another preset or types a number. */
function syncKmPxToDisk() {
  const select = $('out-kmpx-preset');
  const input = $('out-kmpx');
  if (!select || !input || select.value !== 'disk') return;
  const km = diskKmPerPixel(previewPixels());
  if (!km) return;
  const value = km.toFixed(4);
  if (input.value === value) return;
  input.value = value;
  input.hidden = true;
  syncOutputScale('kmpx');
}

/** Set while a preset is filling 物理分辨率 in, so the menu is not re-decided. */
let kmpxFromPreset = false;

/** The saving resolution's physical scale, in brackets after its unit:
 *  ``分辨率 3000 px（0.33 km/px）``.  The number is the whole point of asking
 *  for a size, and this is where it is read. */
function updateDiskScale() {
  const el = $('disk-scale');
  if (!el) return;
  const km = diskKmPerPixel(diskPixels());
  // The unit and the scale are one span -- "px（0.33 km）" -- so they can never
  // end up on different lines or a row apart.
  if (!km) { el.textContent = 'px'; return; }
  const text = km < 1
    ? `${(km * 1000).toFixed(km < 0.1 ? 1 : 0)} m`
    : `${km.toFixed(km < 10 ? 3 : 2)} km`;
  el.textContent = `px（${text}）`;
}

/** Size a canvas so its *element* box has the image's aspect ratio.
 *
 * The canvas backing store is the image's own pixel size; CSS then stretches it
 * to fill the element.  Matching the element's aspect-ratio to the image is what
 * keeps that stretch from distorting, and it is also what makes
 * getBoundingClientRect() an exact basis for turning cursor positions into
 * selenographic coordinates. */
function sizeCanvas(canvas, img) {
  canvas.width = img.width;
  canvas.height = img.height;
  canvas.style.aspectRatio = img.width + ' / ' + img.height;
}

/* ── settings files ────────────────────────────────────────────────────────
 *
 * Everything the page can be told lives in a form control, so a setting file is
 * just those controls written out by id -- no separate schema to drift out of
 * step with the page.  Two consequences worth knowing: a file records the
 * *selection box* as typed numbers rather than as a selection object, and it
 * deliberately leaves out anything derived (the moon readout, the last render).
 * Loading applies the values, then re-runs the same sync the controls would
 * have triggered, so nothing downstream keeps a stale copy.
 */

/** Every control that is a parameter of the page, in a stable order.
 *
 * The landform class switches are appended by buildClassToggles(): they are
 * generated from LAND_CLASSES, so listing them here as well would be a second
 * place to keep in step. */
const SETTING_FIELDS = [
  'when', 'tz', 'lat', 'lon', 'auto-refresh',
  'sel-lon', 'sel-lat', 'sel-rotation', 'sel-locked',
  'manual-train', 'aperture', 'focal-ratio', 'focal-length', 'barlow',
  'back-focus', 'obstruction',
  'sensor-w', 'sensor-h', 'pixel-um', 'sensor-color',
  'preview-px', 'disk-px', 'disk-format', 'disk-annotated', 'disk-earthshine',
  'disk-earthshine-gain',
  'photometric-model', 'sun-samples', 'sky-ambient', 'roughness',
  'data-source',
  'earthshine-gain', 'earth-geometric-albedo', 'earth-samples',
  'earthshine-tint',
  'out-px', 'out-ref', 'out-scale', 'out-kmpx', 'out-samples', 'parallel',
  'sky-span', 'sky-label-step', 'sky-labels', 'sky-elongation', 'sky-closeup',
  'sky-save-ref', 'sky-save-px',
  'seeing', 'seeing-num',
  'blur-strength', 'diffraction', 'sharpen-radius', 'sharpen-strength',
  'saturation', 'gain', 'offset', 'gamma',
  'image-rotation', 'image-rotation-num',
  'noise-poisson', 'noise-gaussian',
  'annotate', 'label-language', 'label-density', 'label-font',
  'label-shadow', 'label-colour', 'label-lock',
  'text-color', 'text-stroke', 'text-stroke-width', 'text-bold',
  'marker-stroke', 'marker-stroke-width',
  'outline-color', 'outline-width', 'outline-style',
  'mark-terminator', 'mark-poles', 'mark-limb', 'mark-equator',
  'mark-libration', 'mark-subsolar', 'mark-nearside',
  'download-annotated', 'save-format', 'region-earthshine', 'region-earthshine-gain',
  'disk-centre-lit',
  'disk-hdr', 'disk-hdr-stops',
  'anim-start', 'anim-end', 'anim-step', 'anim-step-unit', 'anim-fps',
  'anim-px', 'anim-ref', 'anim-scope', 'anim-earthshine', 'anim-earthshine-gain',
  'anim-annotated', 'anim-label-lock',
  'anim-samples', 'anim-timestamp', 'anim-timestamp-corner',
  'anim-timestamp-margin',
  'anim-hdr', 'anim-hdr-stops',
  'out-kmpx-preset', 'out-preset',
];

/** The columns as the reader has them: the panel widths and the fold.
 *
 * These are as much a part of "how the page looks" as the sliders are -- the
 * left column can be hidden, the disk column dragged, the close-ups resized --
 * so a settings file carries them and an import puts them back.  Written as
 * pixels, which is what the CSS variables hold once anything has been dragged;
 * on import `diskWidthTouched` is set so the disk keeps the width it was given
 * instead of being re-fitted to the window.
 */
function layoutSettings() {
  // Both readings are best-effort: the variables are what a drag writes, the
  // elements are what the layout actually gave, and a page in a state where
  // neither can be read simply records nothing.
  const cssValue = (name) => {
    try {
      const value = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue(name));
      return Number.isFinite(value) ? Math.round(value) : null;
    } catch (error) {
      return null;
    }
  };
  const measure = (selector) => {
    const node = document.querySelector(selector);
    if (!node || typeof node.getBoundingClientRect !== 'function') return null;
    const box = node.getBoundingClientRect();
    return box && box.width ? Math.round(box.width) : null;
  };
  const pick = (variable, selector) => {
    const value = cssValue(variable);
    return value === null ? measure(selector) : value;
  };
  const detail = $('annot-detail');
  const left = document.querySelector('.col-left');
  return {
    left: pick('--w-left', '.col-left'),
    right: pick('--w-right', '.col-right'),
    disk: pick('--disk-w', '.view-disk'),
    raw: pick('--raw-h', '.view-info'),
    left_hidden: !!(left && left.hidden),
    annot_open: detail ? !detail.hidden : true,
  };
}

/** Put a saved layout back.  Returns how many values it used. */
function applyLayoutSettings(layout) {
  if (!layout || typeof layout !== 'object') return 0;
  const root = document.documentElement;
  let applied = 0;
  for (const [key, variable] of [['left', '--w-left'], ['right', '--w-right'],
                                 ['disk', '--disk-w'], ['raw', '--raw-h']]) {
    const value = Number(layout[key]);
    if (!Number.isFinite(value) || value <= 0) continue;
    root.style.setProperty(variable, Math.round(value) + 'px');
    if (variable === '--disk-w') diskWidthTouched = true;
    applied += 1;
  }
  if (layout.left_hidden !== undefined) {
    // Through the same door the divider uses, so the bar and the column agree.
    setLeftHidden(Boolean(layout.left_hidden), Number(layout.left) || undefined);
    applied += 1;
  }
  if (layout.annot_open !== undefined) {
    setLabelControls(Boolean(layout.annot_open));
    applied += 1;
  }
  return applied;
}

function collectSettings() {
  const values = {};
  for (const id of SETTING_FIELDS) {
    const element = $(id);
    if (!element) continue;
    values[id] = element.type === 'checkbox' ? element.checked : element.value;
  }
  // The annotation switch is a latch on a <button>, not a form control: a
  // button's value is always the empty string, so collecting it verbatim would
  // record "off" however the page looked.  The mirror is a pair of such latches
  // and is recorded the way the old select recorded it.
  values['annotate'] = state.labelsOn;
  values['image-mirror'] = mirrorMode();
  // The columns are not form fields, so they travel in their own block.
  values['layout'] = layoutSettings();
  return values;
}

/** Put a loaded file back into the controls, then rebuild every derived state. */
function applySettings(values) {
  let applied = 0;
  for (const id of SETTING_FIELDS) {
    if (values[id] === undefined) continue;
    const element = $(id);
    if (!element) continue;
    if (element.type === 'checkbox') element.checked = Boolean(values[id]);
    else element.value = values[id];
    applied++;
    // Range inputs drive their <output> spans from an input listener; a value
    // written straight into the control would leave the number behind.
    if (element.type === 'range') element.dispatchEvent(new Event('input'));
  }
  // A stored 轨迹特写 choice is the reader's, not the automatic heuristic's.
  if (values['sky-closeup'] !== undefined) state.skyCloseupTouched = true;
  applySensorType();
  applyLock();
  updateLockHint();
  // *From* the inputs, not into them: the file's sel-lon/sel-lat were just
  // written there, and syncing the other way would overwrite them with whatever
  // the page happened to have selected.
  selectionFromInputs();
  syncSelectionInputs();
  updateSamplesInfo();
  syncOutputScale('pixels');
  updateNoiseInfo();
  syncClassChips();
  syncMarkChips();
  setMirrorMode(values['image-mirror'] || 'none');
  syncEarthshineGain('earthshine-gain');
  syncResolutionPicker('preview-px', 'preview-preset');
  syncResolutionPicker('disk-px', 'disk-preset');
  syncResolutionPicker('out-px', 'out-preset');
  syncResolutionPicker('anim-px', 'anim-preset');
  // The data source is a server-side choice wearing a <select>'s clothes: the
  // file records the product, and putting it back means asking the server, not
  // just writing the value.
  if (values['data-source'] && $('data-source').value !== values['data-source']) {
    $('data-source').value = values['data-source'];
    chooseDataSource(values['data-source']);
  }
  updateDiskScale();
  // A stored file may turn HDR on and set the format to JPEG at once, so the
  // switch has to follow the format it was loaded with.  The zone may also be
  // stored as a plain "8", which is shown with its sign again.
  updateDiskHdrControls();
  // The animation pane has two mutual exclusions of its own (后期处理 / 地貌
  // against HDR), and a stored file can tick both sides at once.
  updateAnimProcess();
  refreshAnimationPlan();
  formatTimezone();
  applied += applyLayoutSettings(values['layout']);

  // The annotation toggle is a latch, not a value: mirror it and re-ask.
  const wanted = $('annotate').getAttribute('aria-pressed') === 'true';
  const shouldLabel = values['annotate'] === true || values['annotate'] === 'true';
  if (wanted !== shouldLabel) {
    state.labelsOn = !shouldLabel;      // toggleLabels flips it back
    toggleLabels();
  } else {
    refreshLabels();
  }
  return applied;
}

/** Read a settings file the reader picked, and apply it.
 *
 * The file is a page of parameters in the same shape `/api/settings/save`
 * writes -- `{schema, saved_utc, settings: {...}}` -- so importing one is the
 * same `applySettings()` the server's own files go through, with no request.
 */
async function importSettingsFile() {
  const picker = $('settings-pick');
  const file = picker && picker.files && picker.files[0];
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    const values = payload.settings || payload;
    const count = applySettings(values);
    $('settings-name').value = file.name.replace(/\.(conf|json)$/i, '');
    setStatus(`已导入 ${file.name}：应用了 ${count} 项参数，正在重算…`, 'busy');
    await refreshDisk();
    if (state.renderId) reprocess(0);
    setStatus(`已导入 ${file.name}`);
  } catch (error) {
    setStatus('导入失败：' + String(error.message || error), 'error');
  } finally {
    // So picking the same file twice fires 'change' again.
    picker.value = '';
  }
}

/** The saved settings, as the options of the name field's own datalist. */
let knownSettings = [];                 // the names the server last listed

async function refreshSettingsList() {
  try {
    const payload = await post('/api/settings/list', {});
    const files = payload.files || [];
    knownSettings = files.map((file) => file.name);
    const list = $('settings-files');
    if (!list) return;
    list.innerHTML = '';
    for (const file of files) {
      const option = document.createElement('option');
      option.value = file.name;
      list.appendChild(option);
    }
  } catch (error) {
    setStatus('读取设置目录失败：' + String(error.message || error), 'error');
  }
}

/** Apply the settings file the name field names, when the server has one.
 *
 * The field is two things at once -- the name 导出 writes and the file 导入
 * reads -- so picking one of the datalist's names is already the whole request:
 * asking the reader to press 导入 afterwards only gave them a chance to look at
 * the name and wonder whether it had done anything.  A name the server does not
 * have is a name for 导出 to create, and gets no reaction at all.
 */
async function applyNamedSettings() {
  const name = $('settings-name').value.trim();
  if (!name) return;
  if (!knownSettings.includes(name)) {
    await refreshSettingsList();
    if (!knownSettings.includes(name)) return;
  }
  await loadSettings();
}

/** The reader's own save dialog: which folder, and what to call the file.
 *
 * Returns the file handle they picked, ``null`` when they cancelled, and
 * ``undefined`` when this browser has no such dialog (Safari, Firefox, or a
 * page that is not in a secure context).  The caller turns ``undefined`` into
 * the download every browser can do, so nothing here is required to work.
 *
 * It has to be called *before* any await: the picker needs the click's own user
 * activation, and a round trip to the server spends it.
 */
async function askSaveFile(suggestedName, description) {
  if (typeof window === 'undefined' || typeof window.showSaveFilePicker !== 'function') {
    return undefined;
  }
  try {
    return await window.showSaveFilePicker({
      suggestedName,
      types: [{
        description,
        accept: { 'application/json': ['.conf', '.json'] },
      }],
    });
  } catch (error) {
    // AbortError is the reader closing the dialog; anything else means the
    // browser would not open one, which is what the fallback is for.
    return error && error.name === 'AbortError' ? null : undefined;
  }
}

async function saveSettings() {
  const name = $('settings-name').value.trim();
  if (!name) {
    setStatus('请先给设置文件起个名字', 'error');
    return;
  }
  const settings = collectSettings();
  const asText = (stamp) => JSON.stringify({
    schema: 'moonlib_s-web-settings/1',
    saved_utc: stamp,
    settings,
  }, null, 2);
  const handle = await askSaveFile(`${name}.conf`, '月球网页设置文件');
  if (handle === null) {
    setStatus('已取消导出');
    return;
  }
  try {
    const payload = await post('/api/settings/save', {
      name, settings,
    });
    $('settings-name').value = payload.saved.name;
    await refreshSettingsList();
    const text = asText(payload.saved.saved_utc || new Date().toISOString());
    const size = `${(payload.saved.bytes / 1024).toFixed(1)} KB`;
    if (handle) {
      const stream = await handle.createWritable();
      await stream.write(new Blob([text], { type: 'application/json' }));
      await stream.close();
      setStatus(`已导出 ${handle.name}（${size}）`);
      return;
    }
    // No dialog in this browser: the file still leaves, as a download into
    // whatever folder it puts downloads in.  The server's own settings
    // directory is the page's list, not the reader's disk, so both are written.
    downloadUrl(URL.createObjectURL(new Blob([text], { type: 'application/json' })),
      `${payload.saved.name}.conf`);
    setStatus(`已导出 ${payload.saved.name}.conf（${size}）`);
  } catch (error) {
    setStatus('导出失败：' + String(error.message || error), 'error');
  }
}

async function loadSettings() {
  const name = $('settings-name').value.trim();
  if (!name) {
    setStatus('先填一份设置文件的名字', 'error');
    return;
  }
  try {
    const payload = await post('/api/settings/load', { name });
    const count = applySettings(payload.settings || {});
    $('settings-name').value = payload.name;
    setStatus(`已导入 ${payload.name}.conf：应用了 ${count} 项参数，正在重算…`, 'busy');
    await refreshDisk();
    if (state.renderId) reprocess(0);
    setStatus(`已导入 ${payload.name}.conf`);
  } catch (error) {
    setStatus('导入失败：' + String(error.message || error), 'error');
  }
}

/* ── views and zoom ──────────────────────────────────────────────────────── */

/** Fit a view's stage inside its body, preserving aspect, and centre it.
 *
 * The stage is what gets explicit pixels; the canvases inside it are told to
 * fill it.  Sizing the canvas directly instead left the wrapper with no size of
 * its own, and an absolutely positioned child of a zero-size box collapses --
 * which is why the Moon kept escaping its frame. */
function fitCanvas(view) {
  const { body, canvas, stage } = view;
  const target = stage || canvas;
  if (!canvas.width || !body || !target) return;
  const rect = body.getBoundingClientRect();
  const pad = 8;
  const availableW = Math.max(1, rect.width - pad);
  const availableH = Math.max(1, rect.height - pad);
  const scale = Math.min(availableW / canvas.width, availableH / canvas.height);
  const w = Math.max(1, Math.round(canvas.width * scale));
  const h = Math.max(1, Math.round(canvas.height * scale));
  target.style.width = w + 'px';
  target.style.height = h + 'px';
  target.style.left = Math.round((rect.width - w) / 2) + 'px';
  target.style.top = Math.round((rect.height - h) / 2) + 'px';
  if (view.zoom <= 1) { view.panX = 0; view.panY = 0; }
}

function clampPan(view) {
  if (!view.body || view.zoom <= 1) { view.panX = 0; view.panY = 0; return; }
  const rect = view.body.getBoundingClientRect();
  // How far the magnified picture overhangs its box, in CSS pixels.
  const slackX = Math.max(0, (view.zoom - 1) * rect.width / 2);
  const slackY = Math.max(0, (view.zoom - 1) * rect.height / 2);
  view.panX = Math.max(-slackX, Math.min(slackX, view.panX));
  view.panY = Math.max(-slackY, Math.min(slackY, view.panY));
}

/* The two close-ups are one picture at two stages of processing, so they zoom
   together: looking at the same crater in both is the whole point of the pair,
   and it used to reset to 100% every time the tab changed.  The full disk keeps
   its own zoom -- it is a different picture with a different purpose. */
function zoomGroup(name) {
  const view = views[name];
  const other = view && view.link ? views[view.link] : null;
  return other && other !== view ? [view, other] : [view];
}

function applyZoom(view) {
  clampPan(view);
  // For the Moon, only the image canvas is transformed: the overlay must stay
  // at a constant scale so the selection keeps its line width and handle size.
  const target = view.name === 'disk' ? view.canvas : (view.stage || view.canvas);
  if (target) {
    target.style.transform =
      `translate(${view.panX}px, ${view.panY}px) scale(${view.zoom})`;
  }
  const label = document.querySelector(`[data-zoom="${view.name}"] [data-zoom-label]`);
  if (label) label.textContent = Math.round(view.zoom * 100) + '%';
  // The labels are drawn at screen resolution, so any change of scale has to
  // redraw them: leaving them alone would stretch the old raster again, which
  // is the blur this layer was introduced to remove.
  drawViewLabels(view.name);
  if (view.name === 'disk') drawSelection();
}

function setZoom(name, value) {
  const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
  for (const view of zoomGroup(name)) {
    view.zoom = zoom;
    applyZoom(view);
  }
}

function panBy(name, dx, dy) {
  for (const view of zoomGroup(name)) {
    view.panX += dx;
    view.panY += dy;
    applyZoom(view);
  }
}

function fitAll() {
  for (const name of Object.keys(views)) {
    const view = views[name];
    if (view.canvas) fitCanvas(view);
    clampPan(view);
    applyZoom(view);
  }
  drawDisk();
  drawAllLabels();
  drawPhaseStrip();
  drawMagnifier();
}

/* The left column mirrors moonlib_s.moon_report.  Times are shown in the
   observer's own zone, which is the offset the page sends up. */
function localTime(iso, offsetHours) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const d = new Date(t + offsetHours * 3600 * 1000);
  return d.toISOString().slice(11, 16);
}

/* ── compass words for the azimuth ─────────────────────────────────────────
 *
 * An azimuth in degrees is the measurement; the direction is what a reader
 * actually plans by.  Sixteen points is the resolution of the instrument a
 * person carries in their head, and it is enough that two directions a quarter
 * of an hour apart are different words.  The Chinese names read as "east of
 * south-east", which is exactly what the number says; the English ones are the
 * aviation abbreviations. */
const COMPASS_ZH = ['北', '北偏东', '东北', '东北偏东', '东', '东南偏东',
                    '东南', '东南偏南', '南', '南偏西', '西南', '西南偏西',
                    '西', '西北偏西', '西北', '西北偏北'];
const COMPASS_EN = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

/** The 16-point compass name of an azimuth measured from north through east. */
function compassDirection(azimuthDeg, language) {
  const value = Number(azimuthDeg);
  if (!Number.isFinite(value)) return null;
  const index = ((Math.round(value / 22.5) % 16) + 16) % 16;
  return language === 'en' ? COMPASS_EN[index] : COMPASS_ZH[index];
}

function fillMoonInfo(g) {
  const rep = g.report || {};
  const snapshot = rep.state || {};
  const phase = snapshot.phase || {};
  const lib = snapshot.libration || {};
  const orbit = snapshot.orbit || {};
  const rs = rep.rise_set || {};
  const tr = rep.transit || {};
  const tz = g.utc_offset_hours === undefined ? 8 : g.utc_offset_hours;
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };

  set('mi-phase-name', phase.label || '—');
  set('mi-illum', fmt(phase.illuminated_percent_topo !== undefined
    ? phase.illuminated_percent_topo : g.illuminated_percent, 2) + ' %');
  set('mi-phase', fmt(phase.phase_angle_topo_deg !== undefined
    ? phase.phase_angle_topo_deg : g.phase_angle_deg, 2) + '°');
  set('mi-age', fmt(phase.age_days, 2) + ' d');
  set('mi-lib', `${fmt(lib.libration_lon_deg, 2)}° / ${fmt(lib.libration_lat_deg, 2)}°`);
  set('mi-subsolar', `${fmt(lib.subsolar_lon_deg, 2)}° / ${fmt(lib.subsolar_lat_deg, 2)}°`);
  set('mi-colong', fmt(lib.colongitude_deg, 2) + '°');
  // The two distances, each behind its own 地/站 mark: they differ by about a
  // third of a percent, and the marks say which is which without a header row.
  set('mi-dist-geo', fmt(snapshot.geo_distance_km, 0));
  set('mi-dist-topo', fmt(snapshot.topo_distance_km, 0));
  set('mi-diam', arcsecText(snapshot.angular_diameter_arcsec, 1));
  set('mi-rad', arcsecText(snapshot.angular_radius_arcsec, 1));
  set('mi-alt', fmt(snapshot.elevation_deg, 2) + '°');
  // The direction is the same number in the words a reader navigates by.  It
  // follows the readout's own language switch, which is why fillMoonInfo() is
  // re-run when that switch moves.
  const direction = compassDirection(snapshot.azimuth_deg, state.moonLang);
  set('mi-az', direction === null
    ? '—'
    : `${fmt(snapshot.azimuth_deg, 2)}°${state.moonLang === 'en' ? ' ' : ''}` +
      `${state.moonLang === 'en' ? `(${direction})` : `（${direction}）`}`);
  set('mi-ra', fmt(snapshot.ra_hours, 4) + ' h');
  set('mi-dec', fmt(snapshot.dec_deg, 3) + '°');
  set('mi-elong', fmt(snapshot.elongation_deg, 2) + '°');
  set('mi-sunalt', fmt(snapshot.sun_elevation_deg, 2) + '°');
  set('mi-earth', g.earthshine_magnitudes_below_sunlight === undefined ? '—'
    : `${fmt(g.earth_illuminated_percent, 1)} %　${fmt(g.earthshine_magnitudes_below_sunlight, 2)} mag`);
  set('mi-vel', fmt(snapshot.velocity_kms, 3) + ' km/s');
  set('mi-light', fmt(snapshot.light_time_s, 3) + ' s');
  set('mi-rise', localTime(rs.rise_utc, tz));
  set('mi-transit', localTime(rs.transit_utc, tz));
  set('mi-transit-alt', fmt(tr.elevation_deg, 2) + '°');
  set('mi-set', localTime(rs.set_utc, tz));
  set('mi-nextrise', localTime(rs.next_rise_utc, tz));
}

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = dataUrl;
  });
}

/* Sliders run against the server's cached linear frame, so a drag costs
   milliseconds instead of a ray march.  Debounced because a range input fires
   on every pixel of travel. */
let processTimer = null;
let processSeq = 0;

function reprocess(delay) {
  if (!state.renderId || !state.resultImage) return;
  clearTimeout(processTimer);
  processTimer = setTimeout(runProcess, delay === undefined ? 90 : delay);
}

/* The 后期处理 bar.  There is no per-step progress to report -- the server runs
   the whole chain in one call -- so it fills over the time the last one took,
   which is honest about "about this long" and never parks at a made-up number.
   No words under it: the button beside it already says what is running. */
let procProgressTimer = null;

function startProcessProgress() {
  const bar = $('proc-progress');
  const inner = $('proc-progress-bar');
  if (!bar || !inner) return null;
  const started = performance.now();
  const expected = Math.max(200, Number(state.processMs) || 450);
  clearInterval(procProgressTimer);
  bar.hidden = false;
  inner.style.width = '4%';
  const tick = () => {
    const elapsed = performance.now() - started;
    inner.style.width = `${Math.min(95, 100 * elapsed / expected).toFixed(1)}%`;
  };
  procProgressTimer = setInterval(tick, 90);
  return { started };
}

function finishProcessProgress(handle) {
  const bar = $('proc-progress');
  const inner = $('proc-progress-bar');
  clearInterval(procProgressTimer);
  procProgressTimer = null;
  if (handle) state.processMs = performance.now() - handle.started;
  if (!bar || !inner) return;
  inner.style.width = '100%';
  setTimeout(() => {
    if (procProgressTimer) return;      // another pass has started meanwhile
    bar.hidden = true;
    inner.style.width = '0%';
  }, 320);
}

async function runProcess() {
  const seq = ++processSeq;
  const progress = startProcessProgress();
  try {
    const payload = await post('/api/process', {
      render_id: state.renderId,
      // The component travels with every process call, so a slider move keeps
      // showing the half that was picked instead of quietly reverting to the
      // whole picture.
      component: regionComponent(),
      telescope: trainPayload().telescope,
      camera: trainPayload().camera,
      atmosphere: atmospherePayload(),
      processing: processingPayload(),
      // The geometry pane carries the same orientation, and re-asking for it
      // here is what keeps a rotation from being a re-render.
      raw: true,
    });
    if (seq !== processSeq) return;
    state.resultImage = await loadImage(payload.image);
    state.downloads.proc = payload.image;
    sizeCanvas(RESULT, state.resultImage);
    fitCanvas(views.proc);
    if (payload.image_raw) {
      state.rawImage = await loadImage(payload.image_raw);
      state.downloads.raw = payload.image_raw;
      sizeCanvas(RAW, state.rawImage);
      fitCanvas(views.raw);
      drawRaw();
    }
    // After *both* pictures are in place, not between them: the link follows
    // whichever pane is on screen, and refreshing it while the raw half was
    // still the previous frame's left it pointing at bytes nobody is looking at
    // -- which is how a component switch could hand back the old component.
    updateDownloadLink();
    drawResult();
    describeResult(payload.info, null);
    // A component is worth saying out loud: the picture on screen is not the
    // whole frame, and it is the one the save button will write.
    const component = regionComponent();
    setStatus(component === 'image'
      ? '就绪'
      : `就绪（近景为${COMPONENT_NAMES[component]}，按它自己的范围单独曝光）`);
  } catch (error) {
    if (error.stale) { setStatus('参数已变，请点「更新模拟」', 'error'); return; }
    setStatus(String(error.message || error), 'error');
  } finally {
    finishProcessProgress(progress);
  }
}

/** Render the close-up once, at an explicit pixel size. */
async function renderRegion(width, height, samples, label) {
  const train = trainPayload();
  // Remembered before the request, not after: what the button has to give back
  // is the box this render was *asked* for, whatever comes of the request.
  rememberRenderSelection();
  const progress = startRenderProgress(width * height, samples, label);
  // Filling a number in wrongly should cost a click, not a minute: the request
  // is abortable and the button becomes "终止" while it runs.
  const controller = new AbortController();
  state.renderAbort = controller;
  setGenerateLabel('终止');
  try {
    const payload = await post('/api/region', {
      when: whenPayload(),
      component: regionComponent(),
      telescope: train.telescope,
      camera: train.camera,
      atmosphere: atmospherePayload(),
      processing: processingPayload(),
      ...lightPayload(),
      ...computePayload(),
      output: {
        width,
        height,
        samples,
        arcsec_per_pixel: null,
        photometric_model: $('photometric-model').value,
        sun_samples: Number($('sun-samples').value) || 1,
        ...(shadowStepsPayload() === null ? {} : { shadow_steps: shadowStepsPayload() }),
      },
      selection: selectionPayload(),
    }, controller.signal);
    finishRenderProgress(progress, width * height, samples,
      `${payload.info.image.shape[1]}×${payload.info.image.shape[0]} px`);
    return payload;
  } catch (error) {
    clearInterval(renderTimer);
    $('render-progress').classList.remove('on');
    setProgressBar(0);
    setProgressNote(controller.signal.aborted
      ? '已终止' : '渲染失败：' + String(error.message || error));
    showProgress(false);
    throw error;
  } finally {
    state.renderAbort = null;
    setGenerateLabel('更新模拟');
  }
}

/** 生成模拟: the first press renders the frame the settings ask for.
 *
 *  It used to render an aiming preview sized to the box (a few hundred pixels,
 *  one sample), so a reader who had just chosen the finest 物理分辨率 saw a
 *  fast, coarse frame and only got the chosen resolution on the *second* press
 *  (更新模拟).  The resolution pane is a statement about the picture, and the
 *  first click has to honour it: there is no cheaper answer that is also the
 *  answer to the question.
 */
async function generate() {
  await renderOutput();
}

/** 更新图像: the real render, at the size the save row asks for. */
async function renderOutput() {
  if (!state.selection) {
    setStatus('先在「全月面」上拖一个选框，或点一个地名', 'error');
    return;
  }
  const { width, height } = renderPixels();
  const samples = Number($('out-samples').value) || 1;
  busy('result-busy', true);
  busy('raw-busy', true);
  setStatus(state.renderClamped
    ? `按上限渲染 ${width}×${height} px（地形数据到不了更细）…`
    : `按保存分辨率渲染 ${width}×${height} px…`, 'busy');
  try {
    const payload = await renderRegion(width, height, samples, '保存渲染');
    await acceptRender(payload);
    setStatus(`出图完成：${width}×${height} px，每像素 ${samples}×${samples} 条光线。`);
  } catch (error) {
    setStatus(String(error.message || error), 'error');
  } finally {
    busy('result-busy', false);
    busy('raw-busy', false);
  }
}

/** Put a finished close-up render on the page, whichever size it came at. */
async function acceptRender(payload) {
  state.renderId = payload.render_id;
  state.rawImage = await loadImage(payload.image_raw);
  state.resultImage = await loadImage(payload.image);
  sizeCanvas(RAW, state.rawImage);
  sizeCanvas(RESULT, state.resultImage);
  fitCanvas(views.raw);
  fitCanvas(views.proc);
  drawRaw();
  drawResult();
  describeResult(payload.info, null);
  state.downloads.raw = payload.image_raw;
  state.downloads.proc = payload.image;
  // The file's pixel count is the user's number; all this does is keep it a
  // size that still makes sense for the frame that has just arrived.
  updateSaveInfo();
  // The resolution line and the save line both describe the picture that just
  // arrived.
  syncOutputScale('pixels');
  updateDownloadLink();
  updateSaveInfo();
  if (state.labelsOn) await refreshLabels();
}

function drawResult() {
  const ctx = RESULT.getContext('2d');
  ctx.clearRect(0, 0, RESULT.width, RESULT.height);
  if (state.resultImage) ctx.drawImage(state.resultImage, 0, 0, RESULT.width, RESULT.height);
  // 目镜 applied after the picture and *before* the labels: the names are on the
  // eyepiece's field, not painted over its barrel.
  if (state.resultImage && eyepieceSettings().on) {
    const lens = document.createElement('canvas');
    lens.width = RESULT.width;
    lens.height = RESULT.height;
    const lensCtx = lens.getContext('2d');
    lensCtx.drawImage(RESULT, 0, 0);
    applyEyepiece(lens);
    ctx.clearRect(0, 0, RESULT.width, RESULT.height);
    ctx.drawImage(lens, 0, 0);
  }
  drawViewLabels('proc');
}

function drawRaw() {
  const ctx = RAW.getContext('2d');
  ctx.clearRect(0, 0, RAW.width, RAW.height);
  if (state.rawImage) ctx.drawImage(state.rawImage, 0, 0, RAW.width, RAW.height);
  drawViewLabels('raw');
}

/* Both endpoints describe a frame, but they do not have to carry every field:
   /api/process reports on an already-rendered frame, so anything it omits must
   simply be left as it was rather than blowing up the readout. */
function describeResult(info, ms) {
  const inst = info.instrument || {};
  const r = info.region;
  const tr = info.train;
  // The instrument's own numbers are not printed anywhere any more; the plate
  // scale is kept because the derived hints use it.
  if (tr) state.trainScale = tr.plate_scale_arcsec;
  const shape = (info.image && info.image.shape) || [0, 0];
  const bits = shape[0] ? [`${shape[1]}×${shape[0]} px`] : [];
  // The server echoes which radiance the frame was drawn in; the readout says so
  // rather than leaving the shape line to describe a component as if it were the
  // whole picture.
  const component = info.component || 'image';
  if (component !== 'image') bits.push(`${COMPONENT_NAMES[component]}（单独曝光）`);
  const orient = orientation();
  if (orient.rotation_deg) bits.push(`旋转 ${orient.rotation_deg.toFixed(1)}°`);
  if (orient.mirror !== 'none') {
    bits.push(orient.mirror === 'horizontal' ? '左右镜像' : '上下镜像');
  }
  const photons = poissonPhotons();
  const sigma = gaussianSigma();
  if (photons) bits.push(`泊松噪声 ${photons.toExponential(1)} e⁻ 满阱`);
  if (sigma) bits.push(`读出噪声 ${(sigma * 100).toFixed(1)}%`);
  if (r) {
    bits.push(`${fmt(r.ground_sample_km * 1000, 0)} m/px`,
              `出射角 ${fmt(r.emergence_deg, 1)}°`,
              `日高 ${fmt(r.solar_elevation_deg, 1)}°`);
  }
  // Earthshine is a property of the *light*, so the close-up reports what it
  // was drawn with; a later reprocess (which never re-marches) must not blank
  // the line, so the last render's block is remembered.
  if (info.earthshine) state.earthshine = info.earthshine;
  if (state.earthshine) {
    describeEarthshine(state.earthshine, state.geometry || {});
  }
  // 锐化半径 is quoted in the frame's own pixels, and the arcseconds beside it
  // are what the mask works out to on the sky -- the control is in pixels, so
  // the readout leads with pixels too.
  bits.push(`系统 FWHM ${fmt(inst.fwhm_total_arcsec, 3)}″`,
            `锐化半径 ${fmt(inst.sharpen_sigma_px, 2)} px（${fmt(inst.sharpen_sigma_arcsec, 3)}″）`);
  if (ms !== null && ms !== undefined) bits.push(`前端 ${(ms / 1000).toFixed(1)} s`);
  if (info.process_seconds !== undefined) bits.push(`后期 ${(info.process_seconds * 1000).toFixed(0)} ms`);
  $('result-info').textContent = bits.join('　');
}

/* ── wiring ──────────────────────────────────────────────────────── */

/** Wire one <input range> to the <output> that reads it out.
 *
 * Every id here is looked up defensively: the page has grown and lost controls,
 * and a list that names one that is no longer in the markup used to throw
 * inside `init()` -- which stopped the *initial render* along with everything
 * after it.  A missing control is a control nobody can use; it is not a reason
 * for the page to stop working.
 */
function bindOutputs() {
  for (const [id, digits] of [['blur-strength', 2], ['sharpen-radius', 1],
                              ['sharpen-strength', 2], ['saturation', 2],
                              ['gain', 2], ['offset', 2], ['gamma', 2]]) {
    const input = $(id), out = $(id + '-out');
    if (!input || !out) continue;
    const update = () => { out.textContent = Number(input.value).toFixed(digits); };
    input.addEventListener('input', update);
    update();
  }
}

/** Say whether the lock can do anything, and enable it only when it can. */
function updateLockHint() {
  const radio = cameraFieldRadiusArcsec();
  const box = $('sel-locked');
  const wanted = box.checked;
  const ok = radio > 0;
  if (wanted && !ok) {
    box.checked = false;
    return false;
  }
  return wanted;
}

/** A monochrome sensor has no colour to keep, so the switch stops being a
 *  choice; otherwise the switch is the authority and saturation follows it. */
function applySensorType() {
  // A monochrome sensor has no colour to keep, so saturation stops being a
  // choice.  The 彩色 checkbox that used to sit beside it is gone: it was a
  // second copy of the camera's own 类型 menu, and beside a slider it read as
  // that slider's switch.
  const mono = $('sensor-color').value === 'mono';
  if (mono) {
    $('saturation').value = '0';
    $('saturation-out').textContent = '0.00';
  }
  $('saturation').disabled = mono;
  updateDerivedHints();
}

/** Keep the lock and the sensor's own consequences in step.
 *
 * The two paragraphs that used to be printed here (the equivalent focal length,
 * the seeing budget, the sharpening scale) were explaining numbers the fields
 * already say; the lock is the one thing that has a side effect worth running.
 */
function updateDerivedHints() {
  updateLockHint();
  applyLock();
}

/** Ask the server for its hard pixel caps, so the page can promise them. */
async function loadLimits() {
  try {
    const response = await fetch('/api/health');
    const payload = await response.json();
    if (payload && payload.limits) {
      LIMITS.preview_px = Number(payload.limits.preview_px) || LIMITS.preview_px;
      LIMITS.output_px = Number(payload.limits.output_px) || LIMITS.output_px;
      LIMITS.disk_px = Number(payload.limits.disk_px) || LIMITS.disk_px;
      LIMITS.terrain_km_per_px =
        Number(payload.limits.terrain_km_per_px) || LIMITS.terrain_km_per_px;
      // The server knows which build it is; the header prints what it says.
      if (payload.limits.version) {
        $('app-version').textContent = `v${payload.limits.version}`;
      }
      if (payload.limits.label_outline_px) {
        LIMITS.label_outline_px = Number(payload.limits.label_outline_px);
      }
      if (payload.limits.animation_max_frames) {
        LIMITS.animation_max_frames = Number(payload.limits.animation_max_frames);
        updateAnimSpan();
      }
      if (payload.limits.cpu_count) {
        LIMITS.cpu_count = Math.max(1, Number(payload.limits.cpu_count) || 1);
        const parallel = $('parallel');
        if (parallel) {
          parallel.max = String(LIMITS.cpu_count);
          const wanted = Number(parallel.value);
          if (!Number.isFinite(wanted) || wanted < 1) {
            // 自动: the machine's own core count, spelled out rather than left
            // as a blank that only the tooltip explains.
            parallel.value = String(LIMITS.cpu_count);
          } else if (wanted > LIMITS.cpu_count) {
            parallel.value = String(LIMITS.cpu_count);
          }
          parallel.title = '渲染按横带分给多个线程；调小可以给主机留出余量，调大则更快。'
            + `（本机 ${LIMITS.cpu_count} 核；留空表示全部使用）`;
        }
      }
      if (payload.data_sources) {
        LIMITS.data_sources = payload.data_sources;
        renderDataSources(payload.data_sources);
      }
      $('out-kmpx').min = String(LIMITS.terrain_km_per_px);
      $('out-kmpx').title = `最细 ${LIMITS.terrain_km_per_px.toFixed(3)} km/px（本地 LOLA 的分辨率）`;
      $('out-scale').min = String(minArcsecPerPixel().toFixed(4));
      // The menu's own rows have to agree with that floor -- and so does the
      // animation's copy of them.
      syncKmPxPresets();
      copyPresetOptions('out-kmpx-preset', 'anim-kmpx-preset', true);
      syncCopiedPresets('out-kmpx-preset', 'anim-kmpx-preset');
      // The two disk resolutions are type-or-pick fields, not spinners: the
      // published cap goes in the title (and is clamped on read) rather than on
      // a `max` attribute a text input has no use for.
      $('preview-px').title =
        `瞄准用的预览分辨率，上限 ${LIMITS.preview_px} px；可从下拉里挑，也可直接填`;
      $('disk-px').title =
        `保存整月面的分辨率，上限 ${LIMITS.disk_px} px（服务端硬限制）；可从下拉里挑，也可直接填`;
      $('out-px').max = String(LIMITS.output_px);
      $('preview-px').value = String(previewPixels());
      $('disk-px').value = String(diskPixels());
      syncResolutionPicker('preview-px', 'preview-preset');
      syncResolutionPicker('disk-px', 'disk-preset');
      syncResolutionPicker('out-px', 'out-preset');
      updateDiskScale();
      syncOutputScale('pixels');
    }
  } catch (error) {
    // Limits are a nicety: the defaults are the same numbers the server uses.
  }
}

/** The result window is tabbed: geometry and processed share one box, and the
 *  active tab is also what "保存图像" saves. */
function activeTarget() {
  return state.resultTab === 'proc' ? 'proc' : 'raw';
}

/** The instrument pane is tabbed too: one instrument, two sides of it. */
/** The two panes the right-hand column still tabs between: the forecast and the
 *  animation.
 *
 *  后期处理 used to be the third, and is not one any more: it is a section of
 *  its own under the telescope, because post-processing a frame is the step
 *  after taking it and a tab put it behind a click in another column. */
function setProcTab(name) {
  state.procTab = name === 'animation' ? 'animation' : 'events';
  for (const button of document.querySelectorAll('#proc-tabs .tab')) {
    button.setAttribute('aria-pressed', String(button.dataset.proc === state.procTab));
  }
  $('proc-events').hidden = state.procTab !== 'events';
  $('proc-animation').hidden = state.procTab !== 'animation';
  if (state.procTab === 'events') {
    fillEventRange();
    loadEvents();
  }
  // Opening 生成动画 fills its range if it is still empty, and the span line has
  // to be computed from whatever that left behind: a line that only moves when
  // a field is touched would sit there saying "先填开始与结束时间" with both
  // boxes filled in.
  if (state.procTab === 'animation') {
    fillAnimationRange();
    refreshAnimationPlan();
  }
}

/** The animation's default range: one night either side of the current time.
 *
 *  A lunation is the honest default for "watch the Moon turn", but it is also
 *  thousands of frames at any useful step, so the page opens on a range that
 *  renders in a couple of minutes and lets the reader widen it.
 */
function fillAnimationRange() {
  const start = $('anim-start'), end = $('anim-end');
  if (!start || !end || start.value) return;
  const now = new Date();
  const tz = Number($('tz').value) || 0;
  const local = new Date(Date.now() + tz * 3600 * 1000);
  const at = (offsetHours) => new Date(local.getTime() + offsetHours * 3600 * 1000)
    .toISOString().slice(0, 16);
  start.value = at(-6);
  end.value = at(6);
}

/** The default range: this calendar year, which is what a diary is for. */
function fillEventRange() {
  const start = $('events-start'), end = $('events-end');
  if (!start || !end) return;
  if (!start.value) {
    const now = new Date();
    const tz = Number($('tz').value) || 0;
    const local = new Date(Date.now() + tz * 3600 * 1000);
    const first = `${local.getUTCFullYear()}-01-01T00:00`;
    const last = `${local.getUTCFullYear()}-12-31T23:59`;
    start.value = first;
    end.value = last;
  }
}

/** 同步所选时间: copy the instant the full disk is modelling into a range box.
 *
 * The two range boxes are local wall clock, and so is the observer's own field,
 * so this is a copy and not a conversion -- which is the point: a reader who has
 * just found an interesting instant should not have to type it twice.
 * *reload* asks the server again, which only the event list needs: the range is
 * part of its request, while the animation reads it when 生成动画 is pressed. */
function syncRangeTime(fieldId, label, reload) {
  const field = $(fieldId);
  if (!field) return;
  const source = $('when').value;
  if (!source) {
    setStatus('全月面的日期时间还是空的，先设一个时刻', 'error');
    return;
  }
  field.value = source;
  if (reload) loadEvents();
  setStatus(`${label}已同步为 ${source}`);
}

/** The kind switches: one at a time, so they are radio buttons in pills. */
function buildEventKinds() {
  const host = $('event-kinds');
  if (!host) return;
  host.innerHTML = '';
  const kinds = [
    ['full_moon', '满月'], ['supermoon', '超级月亮'], ['micromoon', '最小月亮'],
    ['highest_full', '最高满月'], ['highest_quarter', '最高弦月'],
    ['lunar_eclipse', '月食'], ['xvl', '月面 X/V/L'],
    // Not an EVENT_KINDS entry: a crescent window is a session, not an instants
    // list, and it is ranked by how thin it is rather than by date.  It shares
    // the same range boxes and the same list, because a reader asking "when"
    // does not care which of the two answered.
    ['crescent', '极低照明度'],
  ];
  for (const [key, label] of kinds) {
    const chip = document.createElement('label');
    chip.className = 'mark-chip';
    chip.id = 'event-chip-' + key;
    chip.dataset.on = 'false';
    const box = document.createElement('input');
    box.type = 'radio';
    box.name = 'event-kind';
    box.id = 'event-kind-' + key;
    box.value = key;
    const span = document.createElement('span');
    span.textContent = label;
    chip.append(box, span);
    box.addEventListener('change', () => {
      state.eventKind = key;
      syncEventKinds();
      loadEvents();
    });
    host.appendChild(chip);
  }
  state.eventKind = state.eventKind || 'full_moon';
  syncEventKinds();
}

function syncEventKinds() {
  for (const chip of document.querySelectorAll('#event-kinds .mark-chip')) {
    const box = chip.querySelector ? chip.querySelector('input') : null;
    const on = !!box && box.value === state.eventKind;
    if (box) box.checked = on;
    chip.dataset.on = on ? 'true' : 'false';
  }
}

/** Ask the server for one kind inside the range, and list what comes back.
 *
 * The list is the answer: no sentence above it explaining what a list is.
 */
async function loadEvents() {
  const host = $('event-list');
  if (!host) return;
  const start = $('events-start').value;
  const end = $('events-end').value;
  if (!start || !end) return;
  host.innerHTML = '';
  if (state.eventKind === 'crescent') {
    try {
      const payload = await post('/api/crescents', {
        when: whenPayload(), start, end, limit: 15,
      });
      renderCrescents(payload.items || []);
    } catch (error) {
      const row = document.createElement('div');
      row.className = 'event-row';
      row.textContent = '极低照明度预报失败：' + String(error.message || error);
      host.appendChild(row);
    }
    return;
  }
  try {
    const payload = await post('/api/events', {
      when: whenPayload(), kind: state.eventKind, start, end,
    });
    renderEvents(payload.events || []);
  } catch (error) {
    const row = document.createElement('div');
    row.className = 'event-row';
    row.textContent = '事件预报失败：' + String(error.message || error);
    host.appendChild(row);
  }
}

/** 极低照明度: one row per crescent session, thinnest first.
 *
 * The columns are the four questions a reader asks in order -- which night,
 * how thin, how far from the Sun and how high, and exactly when to shoot --
 * so the list needs no header row and no sentence above it.  Sorting is by
 * illumination, so the first row is the hardest crescent in the range; that
 * is the whole answer, and the date is deliberately not the sort key.
 */
function renderCrescents(items) {
  const host = $('event-list');
  if (!host) return;
  host.innerHTML = '';
  const tz = Number($('tz').value) || 0;
  const clock = (iso) => {
    const stamp = Date.parse(iso);
    if (!Number.isFinite(stamp)) return '—';
    return new Date(stamp + tz * 3600 * 1000).toISOString().slice(11, 16);
  };
  const day = (iso) => new Date(Date.parse(iso) + tz * 3600 * 1000)
    .toISOString().slice(5, 10);
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'event-row crescent-row';
    row.dataset.night = item.night === '残月' ? 'waning' : 'waxing';
    row.title = `${item.session === 'sunset' ? '日落' : '日出'}时段；双击把时间设到最佳时刻`;
    const head = document.createElement('b');
    head.textContent = `${day(item.best_utc)} ${item.night}`;
    const thin = document.createElement('span');
    thin.className = 'crescent-thin';
    thin.textContent = `照亮 ${fmt(item.illumination_percent, 2)}%`;
    const shape = document.createElement('span');
    shape.textContent = `距角 ${fmt(item.elongation_deg, 1)}°　高度 ${fmt(item.moon_elevation_deg, 1)}°`;
    const best = document.createElement('span');
    best.className = 'crescent-best';
    best.textContent = `最佳 ${clock(item.best_utc)}　推荐 `
      + `${clock(item.window_start_utc)}–${clock(item.window_end_utc)}`;
    row.append(head, thin, shape, best);
    row.addEventListener('dblclick', () => {
      const stamp = Date.parse(item.best_utc);
      if (!Number.isFinite(stamp)) return;
      $('when').value = new Date(stamp + tz * 3600 * 1000)
        .toISOString().slice(0, 16);
      if ($('auto-refresh').checked) refreshDisk();
      setStatus(`时间已设为 ${$('when').value}（${item.night}，照亮 `
        + `${fmt(item.illumination_percent, 2)}%）`);
    });
    host.appendChild(row);
  }
}

/** The list itself.  One row per instant, local time, grey when it is down. */
function renderEvents(events) {
  const host = $('event-list');
  host.innerHTML = '';
  const tz = Number($('tz').value) || 0;
  for (const event of events) {
    const row = document.createElement('div');
    row.className = 'event-row';
    row.dataset.observable = event.observable ? 'true' : 'false';
    row.title = '双击把时间设到这一刻';
    const shape = new Date(Date.parse(event.when_utc) + tz * 3600 * 1000);
    const when = document.createElement('b');
    when.textContent = shape.toISOString().slice(0, 16).replace('T', ' ');
    const detail = document.createElement('span');
    // The altitude is one of the numbers in the detail and the row is greyed
    // when it is below the horizon, so a sentence saying so as well was a
    // third copy of the same fact.
    detail.textContent = `${event.label}　${event.detail}`;
    row.append(when, detail);
    row.addEventListener('dblclick', () => {
      $('when').value = shape.toISOString().slice(0, 16);
      state.eventKind = event.kind;
      syncEventKinds();
      if ($('auto-refresh').checked) refreshDisk();
      setStatus(`时间已设为 ${$('when').value}（${event.label}）`);
    });
    host.appendChild(row);
  }
}

/* ── animation: many renders, one MP4 ──────────────────────────────────
 *
 * The page *starts* the job and then polls it.  A sequence of renders is
 * minutes of work, so the request that starts it cannot wait for the answer;
 * and 终止 goes through the same /api/cancel as a single frame, because the
 * server holds one cancel flag for whatever it is tracing.
 */

/** One GET, for the progress the animation reports.
 *
 * The rest of the page only ever posts, so this lives with the animation
 * rather than in the fetch block. */
async function getJson(path) {
  const response = await fetch(path);
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

/** The full disk's list of resolution steps, copied into another menu.
 *
 * One table, two pickers: a second copy in the markup is the thing that drifts
 * the first time a step is added or the ceiling moves.
 */
/** Re-point a *copied* ladder at its source menu without rebuilding it.
 *
 *  `copyPresetOptions` runs once, at startup, so without this the animation's
 *  物理分辨率 menu would keep the disabled rows of whatever data source the page
 *  happened to have then -- the very rows a source switch is supposed to light
 *  up.  Matching on value keeps the target's own leading rows (原生, 自定义)
 *  untouched.
 */
function syncCopiedPresets(sourceId, targetId) {
  const source = $(sourceId), target = $(targetId);
  if (!source || !target) return;
  const byValue = new Map([...source.options].map((option) => [option.value, option]));
  for (const option of [...target.options]) {
    const match = byValue.get(option.value);
    if (!match) continue;
    option.disabled = !!match.disabled;
    option.textContent = match.textContent;
  }
}

function copyPresetOptions(sourceId, targetId, keep) {
  const source = $(sourceId), target = $(targetId);
  if (!source || !target) return;
  // The target's *own* rows (marked data-own in the markup: the animation's
  // 原生) stay in front of the copied ones.  Everything else is a previous
  // copy and is dropped first -- without that, calling this again after
  // 模拟设置 learns a new floor from /api/health would stack a second copy of
  // the whole ladder on top of the first.
  const own = keep ? [...target.options].filter((option) => option.dataset
    && option.dataset.own === '1') : [];
  const current = target.value;
  const owned = new Set(own.map((option) => option.value));
  const rows = [...own];
  for (const option of source.options) {
    // One 自定义, not two: the target already has the row that reveals its own
    // input, and the copied one would be a second control for the same number.
    if (owned.has(option.value)) continue;
    rows.push(option);
  }
  target.innerHTML = '';
  for (const option of rows) {
    const copy = document.createElement('option');
    copy.value = option.value;
    copy.textContent = option.textContent || String(option.value);
    copy.disabled = !!option.disabled;
    if (option.dataset && option.dataset.own) copy.dataset.own = '1';
    target.appendChild(copy);
  }
  // Keep what the reader had selected if it still exists; 原生 otherwise.
  const values = [...target.options].map((option) => option.value);
  target.value = values.includes(current) ? current : (keep ? 'native' : '1000');
}

/** ``YYYY-MM-DDTHH:MM`` read as the page's own wall clock, named in UTC.
 *
 * The range boxes are local, the library wants instants: the same conversion
 * ``whenPayload`` does for the observer's own time. */
function localToUtc(value) {
  if (!value) return '';
  const offset = Number($('tz').value) || 0;
  const asUtc = Date.parse(value + ':00Z');
  if (!Number.isFinite(asUtc)) return '';
  return new Date(asUtc - offset * 3600 * 1000).toISOString().slice(0, 19);
}

/** Everything the animation endpoint needs, assembled the way /api/disk is. */
/** How large the file's frames are: the one number the reader types. */
/** The full-disk preview's per-pixel sampling: n x n rays per pixel.
 *
 *  It used to be a constant 1, which left the Moon's limb stair-stepped while
 *  the close-up pane had the control in 高级设置.  The cost is n squared, which
 *  is why the default is still 1. */
function previewSamples() {
  const value = Number(($('preview-samples') || {}).value);
  return Number.isFinite(value) && value >= 1 ? Math.min(4, Math.round(value)) : 1;
}

/** 边缘平滑: a sub-pixel blur of the full disk, in the *render's own* pixels.
 *
 *  It exists for one thing: the stair-stepping along the limb, where a ray
 *  either hits the Moon or misses it and the answer changes by a whole pixel
 *  from one column to the next.  That is an artefact at the smallest scale the
 *  picture has, so the levels are all around one pixel and none of them is
 *  allowed to be a "make it look soft" control -- a 4 px blur was, and it hid
 *  the terminator it was supposed to leave alone.
 *
 *  The value is applied to the preview *and* to the file, by the same code
 *  path, so what the reader sees on the disk is what a save writes.  It is a
 *  blur of the finished picture rather than a render option: the ray march is
 *  unchanged, and the labels and the selection overlay are drawn after it, so
 *  they stay sharp.
 */
function diskSmoothPx() {
  const value = Number(($('preview-blur') || {}).value);
  // 2.0 is the ceiling, and it is the *server's* ceiling too (see
  // encode.MAX_SMOOTH_PX): past it this is not antialiasing any more, it is a
  // soft-focus filter, and the measured curve says the edge stops matching the
  // picture before that anyway.
  return Number.isFinite(value) && value > 0 ? Math.min(2, value) : 0;
}

/** A copy of *source* blurred by 边缘平滑, or *source* itself when it is off.
 *
 *  Blurring before any scaling is the point: the aliasing is in the render's
 *  pixels, so a 1200 px render saved at 2400 px must be smoothed at 1200, not
 *  after the upscale. */
function smoothImage(source, px) {
  if (!(px > 0) || !source || !source.width) return source;
  const out = document.createElement('canvas');
  out.width = source.width;
  out.height = source.height;
  const ctx = out.getContext('2d');
  // Black first.  A blur pulls in whatever is outside its own edge, and outside
  // a fresh canvas that is *transparent* -- so a 1 px soft rim would appear
  // along the frame's border, and a PNG saved with it would show a grey edge on
  // any background but black.  The frame's background is black already, so
  // saying so costs nothing and the softening stays where it was asked for.
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.filter = `blur(${px}px)`;
  ctx.drawImage(source, 0, 0);
  ctx.filter = 'none';
  return out;
}

/** The animation frame's proportions (height / width).
 *
 *  A whole-disk frame is square; a close-up is the selection's own shape, the
 *  same shape 生成模拟 renders it in.  An animation of a 3:1 box that came out
 *  square would be a different patch of ground from the one the box encloses --
 *  which is exactly what the reader draws the box to decide. */
function animationAspect() {
  if ($('anim-scope').value !== 'region') return 1;
  return Math.max(1e-3, selectionAspect());
}

/** A frame whose *shorter* side is *shortPx*, in the given proportions. */
function frameFromShortSide(shortPx, aspect) {
  const a = Math.max(1e-6, aspect);          // height / width
  return a >= 1
    ? { width: Math.round(shortPx), height: Math.round(shortPx * a) }
    : { width: Math.round(shortPx / a), height: Math.round(shortPx) };
}

/** How many pixels the reader asked for, and which side they meant by it. */
function animationOutputPixels() {
  const px = num('anim-px');
  const cap = Number(LIMITS.disk_px) || 4096;
  return clampPx(Math.round(px === null ? 1000 : px), 64, cap);
}

/** The file's frame size: one number, referred to one side.
 *
 *  The same rule the close-up's save row uses (``outputPixels``), for the same
 *  reason: the aspect is the selection's own, so the only thing left to say is
 *  which side the number is about. */
function animationOutputSize() {
  const typed = animationOutputPixels();
  const aspect = animationAspect();
  const long = !($('anim-ref') && $('anim-ref').value === 'short');
  // The typed number is one side; the short side is what the aspect arithmetic
  // below is anchored on, so convert once and let the helper do the rest.
  const shortSide = long ? typed * Math.min(1, aspect) : typed;
  return frameFromShortSide(shortSide, aspect);
}

/** The animation's 物理分辨率, or null for 原生 (render at the output size). */
function animationKmPerPixel() {
  const select = $('anim-kmpx-preset');
  if (!select || select.value === 'native') return null;
  let value;
  if (select.value === 'custom') {
    value = num('anim-kmpx');
  } else if (select.value === 'disk') {
    // 同步全月面, with the same meaning it has in 模拟设置: the scale the
    // whole-disk frame is drawn at, read off the disk geometry rather than
    // typed here.  Without this the row silently meant 原生.
    const output = animationOutputSize();
    value = diskKmPerPixel(Math.min(output.width, output.height));
  } else {
    value = Number(select.value);
  }
  const floor = Number(LIMITS.terrain_km_per_px);
  if (Number.isFinite(floor) && floor > 0 && Number.isFinite(value) && value < floor) {
    value = floor;
  }
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** How much sky the animation's frame covers, in kilometres across.
 *
 *  A whole-disk frame covers the disk frame the preview shows; a close-up
 *  covers the selection's own field of view.  Both are read off the geometry
 *  the last render reported. */
function animationSkyKm() {
  const g = state.geometry;
  if (!g) return null;
  if ($('anim-scope').value === 'region') {
    const radius = selectionArcsec();
    if (radius === null) return null;
    return (2 * radius / 206264.806) * g.distance_km;
  }
  const perPixel = diskKmPerPixel(1000);
  return perPixel === null ? null : perPixel * 1000;
}

/** The size the frames are *rendered* at, before being scaled to the file.
 *
 *  This is the whole point of having the two numbers: a reader who wants a
 *  1 km/px animation written at 2000 px is asking for a 400 px render blown up,
 *  not for four million rays, and the renderer should not be told otherwise.
 *  The plate scale is stated across the *shorter* side (the same reading the
 *  close-up's resolution pane takes), so that side is what the arithmetic
 *  starts from and the other follows the selection's shape. */
function animationRenderSize() {
  const output = animationOutputSize();
  const kmpx = animationKmPerPixel();
  if (kmpx === null) return output;
  const sky = animationSkyKm();
  if (sky === null) return output;
  const shortSide = clampPx(Math.round(sky / kmpx), 64, RENDER_PX_CAP);
  return frameFromShortSide(shortSide, animationAspect());
}

/** ``1000×1000`` for a square frame, ``1400×900`` when it is not. */
function sizeText(size) {
  return size.width === size.height
    ? `${size.width} px`
    : `${size.width}×${size.height} px`;
}

/** Say what the two sizes will be, because only one of them is on screen. */
function updateAnimScale() {
  const note = $('anim-scale-note');
  const render = animationRenderSize();
  const output = animationOutputSize();
  const kmpx = animationKmPerPixel();
  const sky = animationSkyKm();
  const same = render.width === output.width && render.height === output.height;
  // The plate scale is the same on both axes, so the shorter side is the one to
  // quote: it is the side the reader typed against.
  const shortSide = Math.min(render.width, render.height);
  const perPixel = sky === null ? null : sky / shortSide;
  if (note) {
    if (kmpx === null || same) {
      // Only the scale is worth a line here.  The size is on the 分辨率 field
      // beside it and in the button's own tooltip, and a sentence repeating it
      // ("渲染 1000 px，按原样写入") was the one thing in this pane that said
      // nothing the reader could not already see.
      note.textContent = perPixel === null ? '' : `约 ${perPixel < 1
        ? `${(perPixel * 1000).toFixed(0)} m`
        : `${perPixel.toFixed(2)} km`}/px`;
    } else {
      note.textContent =
        `渲染 ${sizeText(render)} → 输出 ${sizeText(output)}`;
    }
  }
  const button = $('anim-run');
  if (button) {
    button.title = kmpx === null
      ? `按 ${sizeText(render)} 渲染并输出`
      : `按 ${kmpx} km/px 渲染（${sizeText(render)}），再缩放到 ${sizeText(output)}`;
  }
  syncAnimPlanVisibility();
}

/** One line, two answers: the scale and the run's length share the bottom row.
 *  Hide it only when neither has anything to say. */
function syncAnimPlanVisibility() {
  const plan = $('anim-plan');
  if (!plan) return;
  const scale = $('anim-scale-note');
  const span = $('anim-span');
  plan.hidden = !((scale && scale.textContent) || (span && span.textContent));
}

/** 应用后期处理 ends in a display image, so there is no linear frame left for
 *  an HDR file -- the switch says so rather than quietly ignoring one of them.
 *  「地貌」 has the same problem for the same reason: a name is an sRGB mark,
 *  not scene-linear light, so it cannot be written into a linear HDR frame. */
function updateAnimProcess() {
  const box = $('anim-process');
  const hdr = $('anim-hdr');
  const labels = $('anim-annotated');
  if (!box || !hdr) return;
  const blocked = !!box.checked || !!(labels && labels.checked);
  hdr.disabled = blocked;
  if (blocked) hdr.checked = false;
}

/** Everything the animation needs to draw landform names on its frames.
 *
 *  The page owns the *style* (colours, line widths, category switches); the
 *  server owns the projection, because only it has the catalogue and each
 *  frame's camera.  Widths travel as they are read on screen and are scaled to
 *  the frame by the server against its own font size, exactly as the still
 *  export does through `labelStyleFor()`. */
function animationAnnotationsPayload() {
  const box = $('anim-annotated');
  if (!box || !box.checked) return { on: false };
  const output = animationOutputSize();
  const shortSide = Math.max(1, Math.min(output.width, output.height));
  const density = densitySettings();
  const scale = shortSide / 1000;
  return {
    on: true,
    lock: !!$('anim-label-lock').checked,
    language: labelLanguage(),
    classes: activeClasses(),
    only_illuminated: !$('label-shadow').checked,
    labels: density.labels,
    min_radius_px: density.min_radius_px * scale,
    outline_min_radius_px: (Number(LIMITS.label_outline_px) || 9) * scale,
    // The same 2.1 % of the short side the page uses for an untyped 字号.
    font_px: Math.max(8, Math.min(160, Math.round(shortSide * 0.021))),
    text_color: $('text-color').value,
    text_stroke: $('text-stroke').value,
    text_stroke_width: num('text-stroke-width'),
    marker_stroke: $('marker-stroke').value,
    marker_stroke_width: num('marker-stroke-width'),
    bold: $('text-bold').checked,
    outline_color: $('outline-color').value,
    outline_width: num('outline-width'),
    outline_style: $('outline-style').value,
    uniform: !$('label-colour').checked,
    class_colors: CLASS_COLORS,
  };
}

function animationRequest() {
  const step = num('anim-step');
  const unit = Number($('anim-step-unit').value) || 1;
  // 时间步长 is a typable field, so "abc" is possible; send 0 and let the
  // request be refused instead of letting NaN become the server's 60 s default.
  const stepSeconds = (step !== null && Number.isFinite(step) && step > 0)
    ? step * unit : 0;
  const gain = num('anim-earthshine-gain');
  const render = animationRenderSize();
  const output = animationOutputSize();
  const train = trainPayload();
  return {
    when: whenPayload(),
    start: localToUtc($('anim-start').value),
    end: localToUtc($('anim-end').value),
    step_seconds: stepSeconds,
    fps: num('anim-fps') || 12,
    output: {
      width: render.width,
      height: render.height,
      samples: Number($('anim-samples').value) || 1,
      // 阴影精度 is the one setting here that the reader chooses explicitly; when
      // it is 自动 the key is left out and the server picks the budget for the
      // kind of frame it is about to draw (close-up 256, whole disk 64), which
      // is the same policy the stills use.
      ...(shadowStepsPayload() === null ? {} : { shadow_steps: shadowStepsPayload() }),
    },
    // The file's size, applied by the server after the frame is rendered (and
    // after 后期处理, which is an angular operation and belongs at the render's
    // own scale).
    scale_to: [output.width, output.height],
    // No `smooth_px`: the panel's 极小尺度平滑 control is gone.  The server
    // still accepts the field and defaults it to 0, and the library still
    // implements the sub-pixel blur, so the capability stays available to
    // anything that asks for it directly -- the page just no longer offers it.
    timestamp: $('anim-timestamp').checked ? {
      corner: $('anim-timestamp-corner').value,
      margin_px: num('anim-timestamp-margin') === null ? 16 : num('anim-timestamp-margin'),
      // The zone the page is showing its own times in: the stamp has to agree
      // with the boxes above it, not with the viewer's operating system.
      utc_offset_hours: Number($('tz').value) || 0,
    } : null,
    apply_processing: $('anim-process').checked,
    annotations: animationAnnotationsPayload(),
    telescope: train.telescope,
    camera: train.camera,
    atmosphere: atmospherePayload(),
    processing: processingPayload(),
    ...lightPayload(),
    ...computePayload(),
    // The animation's own earthshine switch and gain: an animation is the one
    // case where a run of frames wants less light than the still beside it.
    earthshine: {
      ...earthshinePayload(),
      on: $('anim-earthshine').checked,
      gain: (gain === null || gain < 0) ? 1 : gain,
    },
    hdr: {
      on: $('anim-hdr').checked,
      stops: num('anim-hdr-stops') || 3,
    },
    region: $('anim-scope').value === 'region' ? selectionPayload() : null,
  };
}

/** A duration in the largest unit that still says something useful.
 *
 *  5400 s is "1 h 30 m", not "5400 s" and not "90 m": the two units that mean
 *  the same thing to everyone are hours and minutes over a lunar session, and
 *  days once the run crosses one.  The smaller unit is dropped when it is zero,
 *  because "2 h 0 m" reads as a measurement rather than as a round number. */
function durationText(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days} d${hours ? ` ${hours} h` : ''}`;
  if (hours) return `${hours} h${minutes ? ` ${minutes} m` : ''}`;
  if (minutes) return `${minutes} m${secs ? ` ${secs} s` : ''}`;
  return `${secs} s`;
}

/** How long the run will be and how many frames it will take.
 *
 *  Read off the two time boxes and the step, so it is right before the button
 *  is pressed -- "how long is this going to be" is the one thing a reader wants
 *  to know before spending an hour of ray marching on it.  The frame count is
 *  the server's own arithmetic (inclusive of both ends, floor + 1), so the
 *  number here is the number the job will report. */
function updateAnimSpan() {
  const el = $('anim-span');
  if (!el) return;
  const start = localToUtc($('anim-start').value);
  const end = localToUtc($('anim-end').value);
  const step = (num('anim-step') || 0) * (Number($('anim-step-unit').value) || 1);
  if (!start || !end) {
    el.textContent = '先填开始与结束时间，这里会算出跨度与帧数。';
    if (el.dataset) el.dataset.kind = '';
    syncAnimPlanVisibility();
    return;
  }
  const span = (Date.parse(end + 'Z') - Date.parse(start + 'Z')) / 1000;
  if (!(span >= 0)) {
    el.textContent = '结束时间早于开始时间。';
    if (el.dataset) el.dataset.kind = 'error';
    syncAnimPlanVisibility();
    return;
  }
  if (!(step > 0)) {
    el.textContent = '时间步长要大于 0。';
    if (el.dataset) el.dataset.kind = 'error';
    syncAnimPlanVisibility();
    return;
  }
  const frames = Math.floor(span / step + 1e-9) + 1;
  const cap = Number(LIMITS.animation_max_frames) || 0;
  const over = cap > 0 && frames > cap;
  el.textContent = `时长${durationText(span)}，共 ${frames} 帧` +
    (over ? `（超过上限 ${cap} 帧，先调大步长或缩短范围）` : '');
  if (el.dataset) el.dataset.kind = over ? 'error' : '';
  syncAnimPlanVisibility();
}

/** Everything the animation pane can say before it starts.
 *
 *  One line with two answers -- the ground scale and the run's length -- at the
 *  bottom of the pane, where a reader looks before pressing the button.  They
 *  are refreshed together because they read the same controls. */
function refreshAnimationPlan() {
  updateAnimScale();
  updateAnimSpan();
}

function setAnimationStatus(text, kind) {
  const el = $('anim-status');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
  el.dataset.kind = kind || '';
}

/** What the one animation button says, and therefore what it will do.
 *
 *  While a run is going the same key means "stop after the frame being traced
 *  and keep what is already rendered" -- the reader who has just watched the
 *  opener they wanted has no use for the remaining forty minutes of frames.
 *  A run that finished by itself puts the button back to 生成动画. */
function setAnimationRunLabel(running) {
  const button = $('anim-run');
  if (!button) return;
  if (running) {
    button.textContent = '立即保存';
    button.title = '停止生成后续帧，把已经生成的帧保存为一个动画文件';
    button.dataset.mode = 'save';
  } else {
    button.textContent = '生成动画';
    button.title = '';
    button.dataset.mode = 'run';
  }
}

function setAnimationRunning(running) {
  state.animationRunning = !!running;
  setAnimationRunLabel(!!running);
  const button = $('anim-run');
  if (button) button.disabled = false;
}

let animationTimer = null;

async function startAnimation() {
  const body = animationRequest();
  if (!body.start || !body.end) {
    setAnimationStatus('先填开始与结束时间。', 'error');
    return;
  }
  if (!(body.step_seconds > 0)) {
    setAnimationStatus('时间步长要大于 0。', 'error');
    return;
  }
  if ($('anim-scope').value === 'region' && !body.region) {
    setAnimationStatus('先在月面上框选一块区域，再选「当前选框」。', 'error');
    return;
  }
  stopAnimationTimer();
  $('anim-run').disabled = true;
  setStatus('动画：正在计算帧数…', 'busy');
  try {
    const payload = await post('/api/animation', body);
    state.animationId = payload.id;
    setAnimationRunning(true);
    setAnimationProgress(0, `0/${payload.total} 帧（0%）`, '正在渲染第一帧…');
    setStatus(`动画：共 ${payload.total} 帧`, 'busy');
    animationTimer = setInterval(pollAnimation, 700);
  } catch (error) {
    setAnimationRunning(false);
    setAnimationProgress(null, '');
    setAnimationStatus('动画启动失败：' + String(error.message || error), 'error');
    setStatus('动画启动失败', 'error');
  }
}

/** A file name for the preview: the instant it shows, to the minute.
 *
 *  The same frame rendered twice an hour apart must not become two files with
 *  the same name, and the instant is the one thing about it that cannot be read
 *  off the picture once the stamp is turned off.
 */
function animationPreviewName(whenUtc) {
  const stamp = String(whenUtc || '').replace(/[^0-9]/g, '').slice(0, 12);
  return `moon-anim-preview-${stamp || 'frame1'}.png`;
}

/** 单帧预览: the run's first frame, finished as the file's frames are, saved.
 *
 *  It goes to /api/animation with `preview` set rather than to a second route,
 *  so the preview is made of the same parameters the run will use -- including
 *  the ones that would be an error for a run (an empty selection, a zero step),
 *  which are therefore errors here too, before anything is rendered.
 */
async function previewAnimationFrame() {
  const body = { ...animationRequest(), preview: true };
  if (!body.start || !body.end) {
    setAnimationStatus('先填开始与结束时间。', 'error');
    return;
  }
  if (!(body.step_seconds > 0)) {
    setAnimationStatus('时间步长要大于 0。', 'error');
    return;
  }
  if ($('anim-scope').value === 'region' && !body.region) {
    setAnimationStatus('先在月面上框选一块区域，再选「当前选框」。', 'error');
    return;
  }
  const button = $('anim-preview');
  if (button) button.disabled = true;
  setAnimationStatus('正在渲染第一帧…', 'busy');
  setStatus('动画预览：正在渲染第一帧…', 'busy');
  try {
    const payload = await post('/api/animation', body);
    const preview = payload.preview;
    if (!preview || !preview.image) throw new Error('服务器没有返回预览图');
    const image = $('anim-preview-image');
    if (image) image.src = preview.image;
    const box = $('anim-preview-box');
    if (box) box.hidden = false;
    const note = $('anim-preview-note');
    if (note) {
      note.textContent = `${preview.width}×${preview.height} px · ${preview.when_utc}` +
        ` · ${Number(preview.seconds || 0).toFixed(1)} s`;
    }
    downloadUrl(preview.image, animationPreviewName(preview.when_utc));
    setAnimationStatus(`首帧已保存（${preview.width}×${preview.height} px）`, 'ok');
    setStatus('动画预览：首帧已保存');
  } catch (error) {
    setAnimationStatus('首帧预览失败：' + String(error.message || error), 'error');
    setStatus('动画预览失败', 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

/** 立即保存: ask the job to stop after the frame it is tracing and encode the
 *  frames it already has.  The cancel flag is *not* used for this -- that one
 *  interrupts the frame in flight and throws the file away, which is 终止. */
async function finishAnimation() {
  const button = $('anim-run');
  if (!state.animationId || !state.animationRunning) return;
  if (button) button.disabled = true;
  setAnimationStatus('正在保存已生成的帧…', 'busy');
  setStatus('动画：正在保存已生成的帧…', 'busy');
  try {
    await post(`/api/animation/${state.animationId}/finish`, {});
  } catch (error) {
    if (button) button.disabled = false;
    setAnimationStatus('保存请求失败：' + String(error.message || error), 'error');
    setStatus('保存请求失败', 'error');
  }
}

function stopAnimationTimer() {
  if (animationTimer !== null) {
    clearInterval(animationTimer);
    animationTimer = null;
  }
}

/** The bar and the count under it; the frame's own instant goes in the tooltip.
 *
 * A fraction with the instant in brackets would be two things in one line, and
 * the instant changes faster than anyone reads it -- but it is exactly what a
 * reader wants when they point at the bar and ask "which frame is this?".
 * *fraction* null hides the bar again (a job that never started). */
function setAnimationProgress(fraction, text, note) {
  const bar = $('anim-progress');
  const inner = $('anim-progress-bar');
  if (bar) {
    if (fraction === null) {
      bar.hidden = true;
    } else {
      bar.hidden = false;
      bar.classList.add('on');
      bar.title = note || '';
    }
  }
  if (inner && fraction !== null) {
    const clamped = Math.max(0, Math.min(1, fraction));
    inner.style.width = (clamped * 100).toFixed(1) + '%';
  }
  setAnimationStatus(text || '', fraction === null ? '' : 'busy');
}

async function pollAnimation() {
  if (!state.animationId) return;
  let job;
  try {
    job = await getJson(`/api/animation/${state.animationId}`);
  } catch (error) {
    stopAnimationTimer();
    setAnimationRunning(false);
    setAnimationProgress(null, '');
    setAnimationStatus('动画查询失败：' + String(error.message || error), 'error');
    return;
  }
  const done = job.frames || 0;
  if (job.state === 'running') {
    const fraction = job.total ? done / job.total : 0;
    setAnimationProgress(fraction,
      `${done}/${job.total} 帧（${Math.round(100 * fraction)}%）`, job.message);
    setStatus(`动画：${done}/${job.total} 帧`, 'busy');
    return;
  }
  stopAnimationTimer();
  setAnimationRunning(false);
  if (job.state === 'done') {
    // A run stopped with 立即保存 is finished with fewer frames than it planned;
    // the bar and the line say so rather than pretending the whole range was
    // rendered.
    const partial = job.total && done < job.total;
    setAnimationProgress(partial && job.total ? done / job.total : 1,
      `${job.frames}/${job.total} 帧${partial ? '（已提前保存）' : '（100%）'}`, '完成');
    setStatus(partial ? '已保存已生成的帧，正在下载…' : '动画已生成，正在下载…');
    downloadUrl(job.url, `moon-animation-${job.id}.mp4`);
    return;
  }
  if (job.state === 'cancelled') {
    setAnimationProgress(null, '');
    setAnimationStatus('已终止。', '');
    setStatus('动画已终止');
    return;
  }
  setAnimationProgress(null, '');
  setAnimationStatus('动画失败：' + (job.error || job.message || ''), 'error');
  setStatus('动画失败', 'error');
}

async function cancelAnimation() {
  try {
    await post('/api/cancel', {});
  } catch (error) {
    setAnimationStatus('终止请求失败：' + String(error.message || error), 'error');
    return;
  }
  setAnimationStatus('正在终止…', 'busy');
  setStatus('正在终止动画…', 'busy');
}

function setTrainTab(name) {
  state.trainTab = ['camera', 'eyepiece'].includes(name) ? name : 'telescope';
  for (const button of document.querySelectorAll('#train-tabs .tab')) {
    button.setAttribute('aria-pressed', String(button.dataset.train === state.trainTab));
  }
  $('train-telescope').hidden = state.trainTab !== 'telescope';
  $('train-camera').hidden = state.trainTab !== 'camera';
  $('train-eyepiece').hidden = state.trainTab !== 'eyepiece';
}

/** The eyepiece mask, as numbers.  Zero strength means "no mask at all". */
function eyepieceSettings() {
  const on = $('eyepiece-on').checked;
  // The field is the short side of the frame, always: an eyepiece shows a
  // circle, and the picture it is held up to is the frame the telescope
  // delivered, so the circle is the largest one that fits.
  return {
    on,
    field: 1,
    falloff: clamp(num('eyepiece-falloff') ?? 1.6, 0.1, 6),
    vignette: clamp(num('eyepiece-vignette') ?? 0.22, 0, 1),
    distortion: clamp(num('eyepiece-distortion') ?? 0, -0.5, 0.5),
    mask: clamp(num('eyepiece-mask') ?? 1, 0, 1),
  };
}

/** Paint the eyepiece over a finished picture, in place.
 *
 * The mask is drawn, not simulated: the render already produced the frame the
 * eyepiece is being held up to, and what an eyepiece adds to it is geometry --
 * a field stop, a fall-off towards the edge and, if the reader wants it, the
 * barrel distortion that makes a wide field look like a wide field.  Doing it
 * here means it applies to the preview, the saved file and every animation
 * frame from one piece of code.
 */
function applyEyepiece(canvas) {
  const view = eyepieceSettings();
  if (!view.on) return canvas;
  const ctx = canvas.getContext('2d');
  const width = canvas.width, height = canvas.height;
  const cx = width / 2, cy = height / 2;
  const short = Math.min(width, height);
  if (view.distortion) {
    // Barrel/pincushion: a radial remap of the picture, read back and written
    // out once.  Bilinear, because the source is a picture and not a signal.
    //
    // The loop is written the way it is because it runs once per *pixel*: a
    // 2400 px frame is 5.8 million of them, so a helper function per sample
    // (there used to be one, allocated inside the inner loop) costs more than
    // the arithmetic does.  Everything that does not depend on x is hoisted out
    // of the row, the four weights are computed once for the three channels,
    // and the typed arrays are read through locals -- the same picture, several
    // times faster, and it is the difference between a slider that responds and
    // one that appears to hang.
    const source = ctx.getImageData(0, 0, width, height);
    const out = ctx.createImageData(width, height);
    const radius = 0.5 * Math.hypot(width, height);
    const k = view.distortion;
    const src = source.data;
    const dst = out.data;
    const stride = width * 4;
    for (let y = 0; y < height; y += 1) {
      const dy = (y - cy) / radius;
      const dy2 = dy * dy;
      const row = y * stride;
      for (let x = 0; x < width; x += 1) {
        const dx = (x - cx) / radius;
        const scale = 1 + k * (dx * dx + dy2);
        const sx = cx + dx * scale * radius;
        const sy = cy + dy * scale * radius;
        const target = row + x * 4;
        if (!(sx >= 0 && sy >= 0 && sx < width - 1 && sy < height - 1)) {
          dst[target + 3] = 0;
          continue;
        }
        const x0 = sx | 0, y0 = sy | 0;
        const fx = sx - x0, fy = sy - y0;
        const w00 = (1 - fx) * (1 - fy), w10 = fx * (1 - fy);
        const w01 = (1 - fx) * fy, w11 = fx * fy;
        const top = y0 * stride + x0 * 4;
        const bottom = top + stride;
        dst[target] = src[top] * w00 + src[top + 4] * w10 +
          src[bottom] * w01 + src[bottom + 4] * w11;
        dst[target + 1] = src[top + 1] * w00 + src[top + 5] * w10 +
          src[bottom + 1] * w01 + src[bottom + 5] * w11;
        dst[target + 2] = src[top + 2] * w00 + src[top + 6] * w10 +
          src[bottom + 2] * w01 + src[bottom + 6] * w11;
        dst[target + 3] = 255;
      }
    }
    ctx.putImageData(out, 0, 0);
  }
  const fieldRadius = 0.5 * short * view.field;
  const soft = Math.max(1e-3, view.vignette) * fieldRadius;
  // A radial gradient does the whole job: opaque black outside the field stop,
  // a fall-off inside it, nothing in the middle.  The *shape* of that fall-off
  // is the reader's: `falloff` is the exponent of the radial profile, so 1 is
  // the straight ramp an eyepiece roughly has and larger values keep the middle
  // clear and drop away at the edge.  Drawn as a handful of stops, because a
  // canvas gradient has no curve of its own.
  const inner = Math.max(0, fieldRadius - soft);
  const gradient = ctx.createRadialGradient(cx, cy, inner, cx, cy, fieldRadius);
  const STEPS = 8;
  for (let step = 0; step <= STEPS; step += 1) {
    const fraction = step / STEPS;
    const alpha = Math.min(1, view.mask * Math.pow(fraction, view.falloff) +
      view.mask * 0.06);
    gradient.addColorStop(Math.min(1, fraction), `rgba(0, 0, 0, ${alpha})`);
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, width, height);
  ctx.arc(cx, cy, fieldRadius, 0, Math.PI * 2, true);   // the outside
  ctx.fillStyle = `rgba(0, 0, 0, ${view.mask})`;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx, cy, fieldRadius, 0, Math.PI * 2);
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.restore();
  return canvas;
}

/** Say what the two noise sliders actually amount to. */
/** The two noise sliders say what they amount to, in their own readouts. */
function updateNoiseInfo() {
  const photons = poissonPhotons();
  const sigma = gaussianSigma();
  $('noise-poisson-out').textContent = photons
    ? (100 / Math.sqrt(photons)).toFixed(100 / Math.sqrt(photons) < 1 ? 2 : 1) + '%'
    : '关';
  $('noise-gaussian-out').textContent = sigma > 0
    ? (sigma * 100).toFixed(1) + '%' : '关';
}

/** Fold the landform panel's appearance controls away, or bring them back.
 *
 * The button, the caret and the row are one control: with the row hidden the
 * panel is a single switch, which is how it is used most of the time. */
function setLabelControls(open) {
  const detail = $('annot-detail');
  const caret = $('label-more');
  if (!detail || !caret) return;
  detail.hidden = !open;
  caret.setAttribute('aria-expanded', String(!!open));
  caret.textContent = open ? '▾' : '▸';
  caret.title = open ? '收起几何标注与外观设置，只留开关与搜索'
    : '展开几何标注与外观设置';
}

/** The chip highlight follows the checkbox, including after a settings load. */
function syncClassChips() {
  for (const chip of document.querySelectorAll('.cls-chip')) {
    const box = chip.querySelector('input');
    if (box) chip.dataset.on = box.checked ? 'true' : 'false';
  }
}

/** The same for the geometric overlays, whose boxes are pills too. */
function syncMarkChips() {
  for (const name of DISK_MARKS) {
    const box = $('mark-' + name);
    const chip = box && box.closest ? box.closest('.mark-chip') : null;
    if (!chip) continue;
    chip.dataset.on = box.checked ? 'true' : 'false';
    // On, the switch is written in the colour of the line it drew: the picture
    // and the row of switches then name the same thing the same way.
    const caption = chip.querySelector ? chip.querySelector('span') : null;
    const style = DISK_MARK_STYLES[name];
    if (caption && caption.style) {
      caption.style.color = box.checked && style ? style.colour : '';
    }
  }
}

function setResultTab(tab) {
  state.resultTab = tab === 'proc' ? 'proc' : 'raw';
  for (const button of document.querySelectorAll('#result-tabs .tab')) {
    button.setAttribute('aria-pressed', String(button.dataset.tab === state.resultTab));
  }
  $('body-raw').hidden = state.resultTab !== 'raw';
  $('body-proc').hidden = state.resultTab !== 'proc';
  for (const span of document.querySelectorAll('.view-stack .zoom')) {
    span.hidden = span.dataset.zoom !== state.resultTab;
  }
  fitCanvas(views[state.resultTab]);
  drawAllLabels();
  updateDownloadLink();
}

/** The one render button's label: 生成模拟 at first, 更新模拟 afterwards, and
 *  终止 while a render is running.  One button, because the second press is the
 *  same wish as the first -- "draw this frame at the size I asked for". */
function setGenerateLabel(text) {
  const button = $('generate');
  if (!button) return;
  button.textContent = text;
  button.classList.toggle('aborting', text === '终止');
}

/** Redraw everything that follows the selection, magnifier included. */
function afterSelectionChange() {
  syncOutputScale('pixels');
  updateSaveInfo();
  updateSelectionSyncButton();
  // The box on the full disk first: it is what the reader just changed, and a
  // reset that moves the close-up window but leaves the box where it was is a
  // page arguing with itself.
  drawDisk();
  drawMagnifier();
  if (state.labelsOn) refreshLabels();
}

/** The Moon readout switches language on its own; the rest of the page does not.
 *
 * A page-wide language switch meant every label had to be translated twice and
 * kept in step, and the glossary of terms that came with it was a second thing
 * to maintain.  The readout is the one block where the English term is worth
 * having next to the Chinese one, so it is the one block that carries both and
 * the one block that switches.
 */
function setMoonLanguage(language) {
  state.moonLang = language === 'en' ? 'en' : 'zh';
  const list = $('moon-info');
  if (list) list.classList.toggle('lang-en', state.moonLang === 'en');
  // The heading is the name of the block, not a row of it: it stays Chinese.
  const button = $('moon-lang');
  if (button) {
    button.textContent = state.moonLang === 'en' ? '中' : 'EN';
    button.setAttribute('aria-pressed', String(state.moonLang === 'en'));
  }
  if (window.localStorage) window.localStorage.setItem('moon-readout-lang', state.moonLang);
  // The azimuth's compass word is generated, not markup, so it has to be
  // written again in the new language -- the `.zh`/`.en` spans cannot switch a
  // word the script produced.
  if (state.geometry) fillMoonInfo(state.geometry);
  // The little phase strip prints a lunar-calendar date, which is a word.
  drawPhaseStrip();
}

function autoRefresh() {
  if (!$('auto-refresh').checked) return;
  clearTimeout(autoRefresh.timer);
  autoRefresh.timer = setTimeout(refreshDisk, 400);
}

/* Drag a divider to resize a panel.
 *
 * Every divider writes one CSS custom property; the grid does the rest.  The
 * axis is what differs: the two outer bars are anchored to opposite edges of
 * the window, and the centre pair split a box whose origin is its top-left. */
/* Minimum width the centre column keeps, so no drag can squeeze the pictures
   out of existence or push a side column off the page -- and the minimum the
   stack of close-ups keeps inside it, and the one the right-hand parameter
   column keeps beside the one it shares the column with. */
const CENTRE_MIN = 300;
const STACK_MIN = 140;
const PANE_MIN = 200;

const SPLITTERS = {
  //  The left column may be dragged all the way out: 0 is a legal width, and
  //  the bar it leaves behind clicks it back into view.
  left:  { var: '--w-left',  axis: 'x', sign: +1, min: 0, measure: 'col-left' },
  right: { var: '--w-right', axis: 'x', sign: -1, min: 220, measure: 'col-right' },
  disk:  { var: '--disk-w',  axis: 'x', sign: +1, min: 140, measure: 'view-disk' },
  raw:   { var: '--raw-h',   axis: 'y', sign: +1, min: 110, measure: 'view-info' },
  //  The divider between the two parameter columns: 模拟设置 on the left,
  //  后期处理 / 事件预报 / 生成动画 on the right.
  panes: { var: '--w-pane-a', axis: 'x', sign: +1, min: 180, measure: 'pane-a' },
};

/** How big the thing this divider sizes is *right now*, in pixels.
 *
 * Not the CSS variable: two of these default to `1fr`, and `parseFloat("1fr")`
 * is 1 -- so the first press snapped the panel to its minimum and the drag then
 * carried on from there.  Measure the element instead; the variable is only the
 * fallback for a bar whose panel has no size yet.
 */
function splitterStart(spec) {
  const element = document.querySelector('.' + spec.measure);
  if (element) {
    // Zero is a real answer: the left column can be hidden, and a drag has to
    // start from where the panel is rather than from the minimum.
    const rect = element.getBoundingClientRect();
    return spec.axis === 'x' ? rect.width : rect.height;
  }
  const current = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue(spec.var));
  return Number.isFinite(current) && current > 1 ? current : spec.min;
}

/* ── hiding the left column ────────────────────────────────────────────────
 *
 * Dragged below its floor, the column snaps shut and the divider it leaves
 * behind becomes the handle that opens it again: a click restores the width it
 * had.  Nothing else on the page moves, so a reader who wants the whole window
 * for the pictures can have it and still get the readout back.
 */
const LEFT_SNAP_PX = 90;
let leftWidthOpen = 300;

function setLeftHidden(hidden, width) {
  const root = document.documentElement;
  const layout = document.getElementById('layout');
  if (hidden) {
    const current = parseFloat(getComputedStyle(root).getPropertyValue('--w-left'));
    if (Number.isFinite(current) && current > LEFT_SNAP_PX) leftWidthOpen = current;
    root.style.setProperty('--w-left', '0px');
  } else {
    root.style.setProperty('--w-left', Math.round(width || leftWidthOpen) + 'px');
  }
  if (layout) layout.classList.toggle('left-hidden', !!hidden);
  fitAll();
}

function leftIsHidden() {
  const layout = document.getElementById('layout');
  return !!(layout && layout.classList.contains('left-hidden'));
}

/** The largest value this divider may take, measured from the live layout.
 *
 * The two outer bars divide the window, so their ceiling is what is left after
 * the other column and the centre's own minimum.  The centre's two bars are
 * different: the disk and the stack of close-ups share one grid, so the disk's
 * ceiling is that grid minus the stack's minimum -- measuring it against the
 * window instead let a drag push the close-ups off the page.
 */
/** The box of one element, or an empty box when it is not on the page yet. */
function elementBox(selector) {
  const element = document.querySelector(selector);
  return element ? element.getBoundingClientRect() : { width: 0, height: 0 };
}

function splitterCeiling(spec) {
  const root = document.documentElement;
  const read = (name) => parseFloat(getComputedStyle(root).getPropertyValue(name)) || 0;
  const gap = read('--splitter') || 6;
  const grid = elementBox('.views');
  if (spec.axis === 'y') return Math.max(spec.min, grid.height - CENTRE_MIN);
  if (spec.var === '--disk-w') {
    // The disk shares this grid with the close-ups; the stack keeps its floor.
    // Measured on the grid rather than on the column, so the column's own
    // padding is not handed to the picture.
    const width = grid.width || elementBox('.col-centre').width;
    return Math.max(spec.min, width - gap - STACK_MIN);
  }
  if (spec.var === '--w-pane-a') {
    // The two parameter columns share the right-hand column; each keeps a floor
    // it can still be read at.
    const panes = elementBox('.panes');
    const width = panes.width || elementBox('.col-right').width;
    return Math.max(spec.min, width - gap - PANE_MIN);
  }
  const total = document.getElementById('layout').getBoundingClientRect().width;
  const others = spec.var === '--w-left' ? read('--w-right') : read('--w-left');
  return Math.max(spec.min, total - others - CENTRE_MIN - gap);
}

/** Keep the disk inside its grid when a *side* column is dragged wider. */
function clampDiskWidth() {
  const view = document.querySelector('.view-disk');
  if (!view) return;
  const ceiling = splitterCeiling(SPLITTERS.disk);
  if (view.getBoundingClientRect().width > ceiling + 1) {
    document.documentElement.style.setProperty('--disk-w', Math.round(ceiling) + 'px');
  }
}

function installSplitters() {
  const root = document.documentElement;
  for (const bar of document.querySelectorAll('.splitter')) {
    const spec = SPLITTERS[bar.dataset.target];
    if (!spec) continue;
    bar.addEventListener('pointerdown', (event) => {
      const startPos = spec.axis === 'x' ? event.clientX : event.clientY;
      const start = splitterStart(spec);
      if (spec.var === '--disk-w') diskWidthTouched = true;
      bar.classList.add('dragging');
      bar.setPointerCapture(event.pointerId);

      let moved = false;
      const move = (moveEvent) => {
        const pos = spec.axis === 'x' ? moveEvent.clientX : moveEvent.clientY;
        const delta = (pos - startPos) * spec.sign;
        if (Math.abs(pos - startPos) > 3) moved = true;
        const ceiling = splitterCeiling(spec);
        let next = Math.max(spec.min, Math.min(ceiling, start + delta));
        if (spec.var === '--w-left' && next < LEFT_SNAP_PX) {
          setLeftHidden(true);
          return;
        }
        if (spec.var === '--w-left') setLeftHidden(false, next);
        next = Math.max(spec.min, Math.min(splitterCeiling(spec), next));
        root.style.setProperty(spec.var, next + 'px');
        if (spec.var !== '--disk-w') clampDiskWidth();
        fitAll();
      };
      const up = () => {
        bar.classList.remove('dragging');
        bar.removeEventListener('pointermove', move);
        bar.removeEventListener('pointerup', up);
        // A press on the bar of a hidden column is a click, not a drag: it
        // brings the column back.
        if (!moved && spec.var === '--w-left' && leftIsHidden()) {
          setLeftHidden(false, leftWidthOpen);
          setStatus('左栏已显示');
          return;
        }
        fitAll();
      };
      bar.addEventListener('pointermove', move);
      bar.addEventListener('pointerup', up);
      event.preventDefault();
    });
  }
}

/* ── the full disk is square until the reader says otherwise ───────────────
 *
 * The Moon is a circle, so a window that is not square around it wastes space:
 * the picture either floats in a wider frame or is clipped by a taller one.
 * The centre column's height is fixed by the window, so the disk's width is the
 * one number that can be chosen to make the *picture* square -- but the disk's
 * header wraps, so the height the picture gets depends on that width in turn.
 * Two or three passes of the same measurement settle it.
 */
let diskWidthTouched = false;

/** The width the full disk's save row needs to stay on one line.
 *
 * Its children are all short labels and small controls that do not wrap inside
 * themselves, so their own boxes add up to the width the row wants; the layout
 * only ever gives it less than that when the column is too narrow.
 */
function saveBarWidth() {
  const bar = $('disk-save-bar');
  if (!bar || !bar.children) return 0;
  const style = getComputedStyle(bar);
  const gap = parseFloat(style.columnGap) || 8;
  const padding = (parseFloat(style.paddingLeft) || 0) +
    (parseFloat(style.paddingRight) || 0);
  let total = 0;
  let count = 0;
  for (const child of bar.children) {
    const childStyle = getComputedStyle(child);
    if (child.hidden || childStyle.display === 'none') continue;
    const width = child.getBoundingClientRect().width;
    if (!width) continue;
    total += width;
    count += 1;
  }
  return count ? Math.ceil(total + gap * (count - 1) + padding) : 0;
}

/** Where the full disk's column ends by default: the middle of the page.
 *
 * The picture itself stays square (that is the canvas's business), so this is
 * about the *column*: the disk takes the page's left half, and the close-ups
 * behind it get what is left over.  It is a default, not a rule -- the divider
 * still moves it, and once the reader has dragged it, `diskWidthTouched` keeps
 * their width.
 */
function centrePlacement() {
  const layout = $('layout');
  if (!layout) return 0;
  const box = layout.getBoundingClientRect();
  if (!box.width) return 0;
  const bar = document.querySelector('.splitter');
  const divider = bar && bar.getBoundingClientRect
    ? bar.getBoundingClientRect().width || 6 : 6;
  const left = splitterStart(SPLITTERS.left);
  return Math.round(box.width / 2 - left - divider);
}

function fitDiskColumn() {
  if (diskWidthTouched) return;
  const root = document.documentElement;
  const view = document.querySelector('.view-disk');
  const body = $('body-disk');
  if (!view || !body) return;
  for (let pass = 0; pass < 4; pass += 1) {
    const viewBox = view.getBoundingClientRect();
    const bodyBox = body.getBoundingClientRect();
    if (!viewBox.width || !bodyBox.width) return;
    // Everything in the figure that is not the picture.
    const chrome = viewBox.width - bodyBox.width;
    // The page's middle is what the disk's edge aims at; the save row wants to
    // stay on one line, and the ceiling is what the centre grid can give.
    const wanted = Math.max(centrePlacement(), saveBarWidth());
    const target = Math.max(SPLITTERS.disk.min,
      Math.min(splitterCeiling(SPLITTERS.disk), wanted));
    if (Math.abs(target - Math.round(bodyBox.width + chrome)) <= 1) return;
    root.style.setProperty('--disk-w', target + 'px');
  }
}

/* ── 彩蛋：连着点月相名，天上就下东西 ─────────────────────────────────
 *
 * Five quick taps on the phase name in the strip and the page snows for five
 * seconds.  It is a joke, so it is deliberately self-contained: an overlay that
 * takes no pointer events, its own timer, and no state kept between runs.
 *
 * "Consecutive" is a time window rather than a session: taps more than a second
 * apart start the count again, or a reader who happened to click the name once
 * a day would set it off sooner or later without meaning to.
 */
const MOONFALL_CHARS = ['✦', '✧', '★', '☆', '·', '⋆', '☾', '☽', '✩', '⁂'];
const MOONFALL_TAPS = 5;        // five quick taps
const MOONFALL_GAP_MS = 1000;   // a slower tap starts a new run
const MOONFALL_MS = 5000;       // how long the snow lasts
const MOONFALL_FLAKES = 120;

let moonfallTaps = 0;
let moonfallLast = 0;
let moonfallTimer = null;

function moonfall() {
  const layer = document.createElement('div');
  layer.className = 'moonfall';
  for (let i = 0; i < MOONFALL_FLAKES; i += 1) {
    const flake = document.createElement('span');
    flake.textContent = MOONFALL_CHARS[i % MOONFALL_CHARS.length];
    flake.style.left = `${(Math.random() * 100).toFixed(2)}%`;
    flake.style.fontSize = `${(9 + Math.random() * 17).toFixed(1)}px`;
    flake.style.setProperty('--peak', (0.4 + Math.random() * 0.6).toFixed(2));
    flake.style.animationDuration = `${(2.2 + Math.random() * 2.6).toFixed(2)}s`;
    flake.style.animationDelay = `${(Math.random() * 1.8).toFixed(2)}s`;
    layer.appendChild(flake);
  }
  document.body.appendChild(layer);
  clearTimeout(moonfallTimer);
  // Removal has to be defensive: a stub DOM without Element.remove() would make
  // this timer throw five seconds later, which reads as a page error.
  moonfallTimer = setTimeout(() => {
    if (typeof layer.remove === 'function') layer.remove();
    else if (layer.parentNode) layer.parentNode.removeChild(layer);
  }, MOONFALL_MS);
}

function countMoonfallTap() {
  const now = Date.now();
  moonfallTaps = now - moonfallLast > MOONFALL_GAP_MS ? 1 : moonfallTaps + 1;
  moonfallLast = now;
  if (moonfallTaps < MOONFALL_TAPS) return;
  moonfallTaps = 0;
  if (window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    setStatus('下雪了——不过系统要求减少动态效果，就不下了');
    return;
  }
  moonfall();
}

function init() {
  // Before anything edits the page: what the markup says the values are.
  snapshotDefaults();
  for (const id of RESET_BUTTONS) {
    const button = $(id);
    if (!button) continue;
    button.textContent = RESET_LABEL;
    // The word is the button's whole content, so the title is what explains
    // *what* it resets; the accessible name follows the title.
    if (!button.getAttribute('aria-label')) {
      button.setAttribute('aria-label', button.title || '复位');
    }
  }
  // The instant now, written as the wall clock of the zone the field beside it
  // says: `getTimezoneOffset()` is the *browser's* idea of local time and has
  // no business deciding what the page's time zone field means.
  $('when').value = wallClockNow();

  bindOutputs();
  wireDoubleClickReset(RESETTABLE_SLIDERS);
  wireResolutionPicker('preview-px', 'preview-preset');
  // 采样 changes the picture, so it re-renders like a size change does.
  $('preview-samples').addEventListener('change', () => {
    setStatus(`采样改为 ${$('preview-samples').value}×，重新渲染月面…`, 'busy');
    refreshDisk();
  });
  // 边缘平滑 changes only what the disk *looks* like at the smallest scale, so
  // it is a redraw and never a render: a reader comparing limbs should not pay
  // a ray march for it.
  $('preview-blur').addEventListener('change', () => {
    drawDisk();
    const px = diskSmoothPx();
    setStatus(px > 0 ? `圆盘边缘平滑 ${px} px（预览与保存的文件同一个值）`
      : '边缘平滑已关闭');
  });
  wireResolutionPicker('disk-px', 'disk-preset');
  wireResolutionPicker('out-px', 'out-preset');
  // The animation's menu is the full disk's own table of steps, copied before
  // anything wires the pair up.
  copyPresetOptions('disk-preset', 'anim-preset');
  wireResolutionPicker('anim-px', 'anim-preset');
  $('anim-px').addEventListener('input', refreshAnimationPlan);
  // The animation's 物理分辨率 menu leads with 原生 and ends with 自定义; the
  // ladder in between is 模拟设置's own table, copied so the two agree about
  // what this machine's rasters can actually resolve (including the LOLA floor,
  // which that table learns from /api/health).
  copyPresetOptions('out-kmpx-preset', 'anim-kmpx-preset', true);
  wireAnimKmPx();
  refreshAnimationPlan();
  updateAnimProcess();
  $('anim-process').addEventListener('change', updateAnimProcess);
  $('anim-annotated').addEventListener('change', () => {
    updateAnimProcess();
    setStatus($('anim-annotated').checked
      ? `动画将带月面地貌标注（${$('anim-label-lock').checked ? '锁定第一帧布局' : '逐帧重排'}）`
      : '动画不带地貌标注');
  });
  $('anim-label-lock').addEventListener('change', () => {
    setStatus($('anim-label-lock').checked
      ? '动画标注布局已锁定：整段沿用第一帧的排法'
      : '动画标注将逐帧重新排版');
  });
  updateDiskHdrControls();
  applySensorType();
  applyLock();
  updateLockHint();
  buildClassToggles();

  refreshSettingsList();
  setMoonLanguage(window.localStorage &&
    window.localStorage.getItem('moon-readout-lang') === 'en' ? 'en' : 'zh');
  $('moon-lang').addEventListener('click', () => {
    setMoonLanguage(state.moonLang === 'en' ? 'zh' : 'en');
  });
  loadLimits();
  // The landform index is 900 KB and 60 ms over loopback; fetching it while the
  // first render is still running is what makes the search box instant and puts
  // the per-class counts on the switches.
  loadFeatureIndex();
  updateSamplesInfo();
  updateDensityLabel();
  updateNoiseInfo();
  updateEarthshineInfo();
  syncOutputScale('pixels');
  updateSaveInfo();
  updateDiskScale();
  drawMagnifier();
  drawPhaseStrip();
  setTrainTab('telescope');

  views.disk.name = 'disk';
  views.disk.stage = STACK;      // the stack holds the Moon and its overlay
  views.disk.body = $('body-disk');
  views.disk.canvas = DISK;
  views.raw.name = 'raw';
  views.raw.stage = RAW;         // a lone canvas is its own stage
  views.raw.body = $('body-raw');
  views.raw.canvas = RAW;
  views.proc.name = 'proc';
  views.proc.stage = RESULT;
  views.proc.body = $('body-proc');
  views.proc.canvas = RESULT;
  // The two close-ups are one frame: zooming either zooms both.
  views.raw.link = 'proc';
  views.proc.link = 'raw';

  for (const box of document.querySelectorAll('[data-zoom]')) {
    const name = box.dataset.zoom;
    box.querySelector('[data-zoom-in]').addEventListener('click',
      () => setZoom(name, views[name].zoom * 1.25));
    box.querySelector('[data-zoom-out]').addEventListener('click',
      () => setZoom(name, views[name].zoom / 1.25));
    box.querySelector('[data-zoom-fit]').addEventListener('click',
      () => { setZoom(name, 1); fitCanvas(views[name]); drawDisk(); });
  }
  // Wheel zooms whichever picture the pointer is over; dragging pans.  On the
  // Moon a plain drag edits the selection, so panning there uses shift or the
  // middle button instead.
  for (const name of Object.keys(views)) {
    const body = views[name].body;
    if (!body) continue;
    body.addEventListener('wheel', (event) => {
      event.preventDefault();
      setZoom(name, views[name].zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12));
    }, { passive: false });
    if (name === 'disk') continue;
    body.classList.add('grabbable');
    let last = null;
    body.addEventListener('pointerdown', (event) => {
      // The close-ups carry names too, and a name is dragged the same way here
      // as on the Moon: a press on one edits *it*, a press anywhere else pans.
      if (!event.shiftKey && overlayDown(name, event)) {
        body.setPointerCapture(event.pointerId);
        event.preventDefault();
        return;
      }
      last = { x: event.clientX, y: event.clientY };
      body.classList.add('grabbing');
      body.setPointerCapture(event.pointerId);
    });
    body.addEventListener('pointermove', (event) => {
      if (overlayMove(event)) { event.preventDefault(); return; }
      if (!last) return;
      panBy(name, event.clientX - last.x, event.clientY - last.y);
      last = { x: event.clientX, y: event.clientY };
    });
    const stop = () => {
      overlayUp();
      last = null;
      body.classList.remove('grabbing');
    };
    body.addEventListener('pointerup', stop);
    body.addEventListener('pointercancel', stop);
  }
  installSplitters();

  $('refresh').addEventListener('click', refreshDisk);
  $('generate').addEventListener('click', async () => {
    if (state.renderAbort) {
      // Aborting the fetch only stops the waiting; the server has to be told as
      // well, or it marches the whole frame for nobody and the next press adds
      // a second one beside it.  *Awaited*, because the press after this one is
      // usually 生成模拟 again: the stop has to be on the wire before the new
      // frame asks for the render slot, or the new frame is the one that gets
      // told "上一张还在渲染".
      state.renderAbort.abort();
      try {
        await post('/api/cancel', {});
      } catch (error) {
        setStatus('终止请求失败：' + String(error.message || error), 'error');
      }
      return;
    }
    if (state.renderId) renderOutput(); else generate();
  });
  $('annotate').addEventListener('click', toggleLabels);
  // The whole appearance row folds away behind one button: the switch is what
  // is used every time, the ten controls behind it are not.
  $('label-more').addEventListener('click', () => {
    setLabelControls($('annot-detail').hidden);
  });
  // The phase disc is a picture too: the strip shows it at column size, and
  // clicking asks for it at the size a reader would put in a document.
  $('phase-canvas').addEventListener('click', () => {
    if (!state.geometry) { setStatus('等整月面渲染出来再保存月相图', 'error'); return; }
    const canvas = document.createElement('canvas');
    canvas.width = PHASE_EXPORT_PX;
    canvas.height = PHASE_EXPORT_PX;
    paintPhase(canvas.getContext('2d'), canvas, state.geometry);
    const when = ($('when').value || '').replace(/[:T-]/g, '').slice(0, 12) || 'now';
    const name = `moon-phase-${when}.png`;
    const url = canvas.toDataURL('image/png');
    downloadUrl(url, name);
    setStatus(`已保存 ${name}（${PHASE_EXPORT_PX}×${PHASE_EXPORT_PX} px）`);
  });
  // The phase name is the easter egg's handle; see countMoonfallTap().
  $('phase-name').addEventListener('click', countMoonfallTap);
  // 模拟设置 / 后期处理: the same idea for the two panels of parameters.  The
  // fields are written back and then the page follows them exactly as if the
  // reader had typed each one.
  for (const button of document.querySelectorAll('#proc-tabs .tab')) {
    button.addEventListener('click', () => setProcTab(button.dataset.proc));
  }
  buildEventKinds();
  for (const id of ['events-start', 'events-end']) {
    const field = $(id);
    if (field) field.addEventListener('change', loadEvents);
  }
  $('output-reset').addEventListener('click', () => {
    resetFields(OUTPUT_FIELDS);
    $('out-kmpx').hidden = $('out-kmpx-preset').value !== 'custom';
    syncKmPxToDisk();
    syncOutputScale('pixels');
    updateSamplesInfo();
    updateEarthshineInfo();
    if (state.renderId) reprocess(0);
    refreshDisk();
    setStatus('模拟设置已复位');
  });
  // The eyepiece is drawn, not rendered, so a change here only needs the
  // picture re-composed.  目镜视场遮罩 itself is in the list: it was the one
  // control in this pane that did nothing when it was pressed -- the parameters
  // under it did, which is how a switch can look broken and its sliders look
  // fine at the same time.
  for (const id of ['eyepiece-on', 'eyepiece-falloff', 'eyepiece-vignette',
                    'eyepiece-distortion', 'eyepiece-mask']) {
    const field = $(id);
    if (field) {
      field.addEventListener('change', () => {
        // Redraw only.  This used to call scheduleOutputRender() as well, which
        // is a *ray march*: the glass went on half a second late, behind a
        // "生成中" overlay, and cost a fresh render of the frame -- at 2400 px,
        // seconds of it -- for something the module's own docstring says
        // outright is drawn rather than simulated.  Nothing about the eyepiece
        // reaches the renderer, so nothing about it may start one.
        drawResult();
        // Turning it on while 几何模拟 is the visible pane would look like
        // nothing happened: the mask is drawn on the processed frame.
        if (id === 'eyepiece-on' && field.checked) {
          setResultTab('proc');
          setStatus('目镜视场遮罩已开（画在「后期成片」上）');
        } else if (id === 'eyepiece-on') {
          setStatus('目镜视场遮罩已关');
        }
      });
    }
  }
  $('proc-reset').addEventListener('click', () => {
    resetFields(PROC_FIELDS);
    setMirrorMode('none');
    updateNoiseInfo();
    reprocess(0);
    setStatus('后期处理已复位');
  });
  $('classes-all').addEventListener('click', () => {
    const boxes = [...document.querySelectorAll('#label-classes input[type="checkbox"]')];
    setAllClasses(!(boxes.length && boxes.every((box) => box.checked)));
  });
  // 标注设置复位: the switches, the density, the caption style, the geometric
  // overlays *and the reader's own edits* -- the names they dragged, the ones
  // they put away, and where they left the ruler -- back to the values the page
  // starts with.  Anything a reader has spent an afternoon on is one press away
  // from gone, so it is the small ↺ beside the panel and not a button of its
  // own; and a reset that kept the edits would leave the one thing the reader
  // was trying to undo.
  $('label-reset').addEventListener('click', () => {
    const edited = resetAllLabelEdits();
    resetFields(LABEL_FIELDS);
    for (const name of DISK_MARKS) {
      const box = $('mark-' + name);
      if (box) box.checked = false;
    }
    resetClasses();
    syncMarkChips();
    updateDensityLabel();
    refreshLabelPlans();
    setStatus(edited
      ? `标注设置已复位（连同 ${edited} 处手动改动）`
      : '标注设置已复位');
  });
  // Folded to start with: the panel exists for the reader who goes looking for
  // a named crater, and the picture is what everyone else came for.
  setLabelControls(false);
  for (const id of ['label-density', 'label-language']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      if (state.labelsOn) {
        setStatus(id === 'label-language' ? '正在切换地名语言…' : '正在重排月面地貌…', 'busy');
        refreshLabels().then(() => setStatus('就绪'));
      }
    });
  }
  // The night-side switch changes *what* is labelled, so it is a request; the
  // class switches do the same and were wired in buildClassToggles().
  $('label-shadow').addEventListener('change', () => {
    if (!state.labelsOn) return;
    setStatus($('label-shadow').checked ? '把夜面的地貌也标出来…' : '只标受光面…', 'busy');
    refreshLabels().then(() => setStatus('就绪'));
  });
  // This one lives in the processing group, so it is always the close-up.
  $('refresh-image-proc').addEventListener('click', () => reprocess(0));
  // 保存图像: its own resolution, its own format, its own render.  The physical
  // scale beside the field follows what is typed, not what was last rendered.
  $('disk-save').addEventListener('click', () => { saveDisk(); });
  // HDR is a JPEG-only affair, so the switch follows the format menu.
  $('disk-format').addEventListener('change', updateDiskHdrControls);
  $('disk-hdr').addEventListener('change', updateDiskHdrControls);
  // The animation: start it, poll it, and stop it through the same /api/cancel
  // a single frame's 终止 uses.
  $('anim-run').addEventListener('click', () => {
    if (state.animationRunning) finishAnimation(); else startAnimation();
  });
  $('anim-cancel').addEventListener('click', () => { cancelAnimation(); });
  // 单帧预览: one frame, finished like the file's, handed over as a picture.
  $('anim-preview').addEventListener('click', () => { previewAnimationFrame(); });
  // 数据源: which product the frames are made of.  It moves the finest scale the
  // page can offer, so the 物理分辨率 rows are re-decided with the switch.
  $('data-source').addEventListener('change', () => {
    chooseDataSource($('data-source').value);
  });
  // 区域高清补片: download only the ground the selection covers.
  $('patch-download').addEventListener('click', () => { downloadPatch(); });
  // 时间微调：八个步长按钮加中间的「同步设备时间」，都在观测信息里。最粗的一档
  // 是一周：月相一个月走一圈，"把同一相位往前挪一周"比"挪一个月"更接近读者
  // 真正想做的事（后者几乎总是让月相回到原样）。
  for (const id of ['nudge-back-week', 'nudge-back-day', 'nudge-back-hour',
                    'nudge-back-minute', 'nudge-fwd-minute', 'nudge-fwd-hour',
                    'nudge-fwd-day', 'nudge-fwd-week']) {
    const button = $(id);
    if (!button) continue;
    button.addEventListener('click', () => nudgeTime(button.dataset.nudge));
  }
  $('sync-now').addEventListener('click', syncDeviceTime);
  // 时区显示符号：输入框是 text，所以 ↑↓ 要自己接回来；规整（补符号、取整、夹取）
  // 由下面那条 change 处理，免得两处各写一遍。
  $('tz').addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    stepTimezone(event.key === 'ArrowUp' ? 1 : -1);
  });
  // 范围微调：开始与结束一起挪，窗口长度不变。按 data-range 接线而不是按 id
  // 一个个列：动画那一栏在「开始」和「结束」下面各有一排，做的是同一件事。
  wireRangeNudges();
  // 同步所选时间：把全月面正在算的那个时刻抄进范围框。
  $('events-start-sync').addEventListener('click', () => {
    syncRangeTime('events-start', '开始', loadEvents);
  });
  $('events-end-sync').addEventListener('click', () => {
    syncRangeTime('events-end', '结束', loadEvents);
  });
  $('anim-start-sync').addEventListener('click', () => {
    syncRangeTime('anim-start', '动画开始');
    refreshAnimationPlan();
  });
  $('anim-end-sync').addEventListener('click', () => {
    syncRangeTime('anim-end', '动画结束');
    refreshAnimationPlan();
  });
  // 生成动画's own fields: everything that changes the frame's size, its shape
  // or the length of the run says so straight away, before anything is pressed.
  for (const id of ['anim-start', 'anim-end', 'anim-step', 'anim-step-unit',
                    'anim-fps', 'anim-ref', 'anim-scope', 'anim-samples']) {
    const field = $(id);
    if (!field) continue;
    field.addEventListener('input', refreshAnimationPlan);
    field.addEventListener('change', refreshAnimationPlan);
  }
  $('disk-px').addEventListener('input', updateDiskScale);
  $('disk-px').addEventListener('change', () => {
    const px = diskPixels();
    $('disk-px').value = String(px);
    updateDiskScale();
    setStatus(`保存分辨率 ${px}×${px} px，点「保存图像」时才开始渲染`);
  });
  // The full disk's earthshine is a checkbox now: ticked = the whole picture,
  // unticked = the sunlit half only.  Either way the render is a different
  // exposure, so the cached export is a different file.
  $('disk-earthshine').addEventListener('change', () => {
    setStatus($('disk-earthshine').checked
      ? '保存整月面将输出完整画面（阳光 + 地照，各自单独曝光）'
      : '保存整月面只输出阳光分量（按它自己的范围单独曝光）');
  });
  // Pixels and plate scale are two views of one number, so each keeps the
  // other, and both keep the ground kilometres, up to date.
  // The save pixel counts describe the file, not the render, so editing them
  // rescales nothing but the file and never re-runs the ray march.
  // 长边/短边 says which side the number is about; the other side follows the
  // selection's aspect, which is why there is no second field.
  $('out-px').addEventListener('change', () => {
    $('out-px').value = String(num('out-px') === null
      ? outputPixels().width : clampPx(num('out-px'), 64, LIMITS.output_px));
    updateSaveInfo();
  });
  $('out-ref').addEventListener('change', updateSaveInfo);
  // The resolution *does* describe the render, so it re-renders.
  for (const id of ['out-scale', 'out-kmpx']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      syncOutputScale(id === 'out-kmpx' ? 'kmpx' : 'scale');
      if (state.scaleClamped) {
        setStatus(`已夹到本地 LOLA 的分辨率下限：${LIMITS.terrain_km_per_px.toFixed(3)} km/px`, 'error');
      }
      scheduleOutputRender();
    });
  }
  // 物理分辨率: 同步全月面, a preset, or a typed number.
  $('out-kmpx-preset').addEventListener('change', () => {
    const select = $('out-kmpx-preset');
    const field = $('out-kmpx');
    const value = select.value;
    if (value === 'custom') {
      field.hidden = false;
      if (field.focus) field.focus();
      return;
    }
    field.hidden = true;
    const km = value === 'disk' ? diskKmPerPixel(previewPixels()) : Number(value);
    if (!Number.isFinite(km) || km <= 0) return;
    field.value = km.toFixed(4);
    if (value === 'disk') {
      // "Follow the full disk" is a mode, not a number: the field is filled in
      // so the request has something to send, but the menu stays where the
      // reader put it.  Dispatching `change` here used to bounce the menu to
      // 自定义 and put a *second* field on screen beside the menu.
      syncOutputScale('kmpx');
      scheduleOutputRender();
      return;
    }
    // A preset is a *choice*, so the menu keeps it even when the number is
    // clamped to the floor on the way through: only a typed number re-decides
    // which row the menu is on.
    kmpxFromPreset = true;
    try {
      field.dispatchEvent(new Event('change'));
    } finally {
      kmpxFromPreset = false;
    }
  });
  $('out-kmpx').addEventListener('change', () => {
    if (kmpxFromPreset) return;
    const select = $('out-kmpx-preset');
    // While the menu says 同步全月面 the number beside it is derived, not typed:
    // it changes with every preview render and is not a request to leave the mode.
    if (select.value === 'disk') { $('out-kmpx').hidden = true; return; }
    const km = num('out-kmpx');
    const known = km === null ? null : String(Number(km.toFixed(4)));
    const option = known && [...select.options].find((entry) => entry.value === known);
    select.value = option ? known : 'custom';
    if (!option) $('out-kmpx').hidden = false;
  });
  // Earthshine is a property of the *light*, so changing it changes the frame:
  // the same instant rendered with the Earth switched off is a different sky.
  // It follows the same rule as the other optical options -- edit, then update.
  for (const id of ['earthshine-gain', 'earth-geometric-albedo',
                    'earth-samples', 'earthshine-tint']) {
    const element = $(id);
    if (!element) continue;
    const field = element;
    if (!field) continue;
    field.addEventListener('input', updateEarthshineInfo);
    field.addEventListener('change', () => {
      updateEarthshineInfo();
      setStatus('地照已改，点「更新月面」重渲染');
    });
  }
  // 地照增益 has a second field in the full disk's own row, because that is
  // where a reader stands when deciding how much earthshine the picture should
  // carry.  The two fields are one number: typing in either writes the other,
  // and the disk is re-rendered from the value that was typed.
  for (const id of ['earthshine-gain', 'disk-earthshine-gain',
                    'region-earthshine-gain', 'anim-earthshine-gain']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('input', () => {
      syncEarthshineGain(id);
      updateEarthshineInfo();
    });
    element.addEventListener('change', () => {
      syncEarthshineGain(id);
      updateEarthshineInfo();
      // The animation's own gain is not the still picture's: changing it must
      // not re-render the disk the reader is looking at.
      if (id !== 'anim-earthshine-gain') refreshDisk();
    });
  }
  // The disk's own save controls (resolution, earthshine, format) were wired
  // above, with the rest of the full-disk row.
  $('save-format').addEventListener('change', updateSaveInfo);
  // 地貌 in the close-up's row is the close-ups' own switch: unticking it takes
  // the names off 几何模拟 and 后期成片 as well as out of the file, and leaves
  // the full disk (which has its own) exactly as it was.
  $('download-annotated').addEventListener('change', () => {
    updateSaveInfo();
    drawAllLabels();
    setStatus($('download-annotated').checked
      ? '近景图的月面地貌标注已打开'
      : '近景图的月面地貌标注已关闭（全月面不受影响）');
  });
  // 分量出图 for the close-up: both halves are already in the server's cache, so
  // asking for one is a /api/process round trip against that cache -- no ray
  // march -- and the panes become the pictures a save will write.  Nothing else
  // would do: re-rendering would march for a frame that has not changed, and
  // relabelling the file would hand back the previous component's bytes.
  $('region-earthshine').addEventListener('change', () => {
    const name = COMPONENT_NAMES[regionComponent()];
    updateSaveInfo();
    if (!state.renderId || !state.resultImage) {
      setStatus(`近景出图将保存${name}，先点「生成模拟」`);
      return;
    }
    setStatus(`正在按${name}单独曝光…`, 'busy');
    reprocess(0);
  });
  // Dragging only moves the number; the layout is recomputed when the slider is
  // released, because every step changes how many boxes have to fit.
  $('label-density').addEventListener('input', updateDensityLabel);
  $('label-density').addEventListener('change', () => {
    updateDensityLabel();
    const settings = densitySettings();
    setStatus(`密度 ${settings.labels} 条（最小半径 ${settings.min_radius_px} px），重新排版…`, 'busy');
    if (state.labelsOn) refreshLabels();
  });
  $('out-samples').addEventListener('change', () => {
    updateSamplesInfo();
    scheduleOutputRender();
  });
  // The frame font is a *layout* input to the server, not a canvas style: the
  // plan is laid out for one text size, so a new size is a new request.  With
  // the layout locked it is neither -- the plan stays, the type changes -- and
  // refreshLabels() is what knows the difference.
  $('label-font').addEventListener('change', () => {
    if (!state.labelsOn) { drawAllLabels(); return; }
    setStatus(labelLockOn() ? '字号已改，布局仍锁定…' : '字号已改，正在重排月面地貌…', 'busy');
    refreshLabels().then(() => setStatus('就绪'));
  });
  // 刷新字体 is deliberately *not* a re-layout: it is the button for "the
  // picture is bigger now, make the type fit it again" without throwing away
  // the arrangement (or the hand-placed names) the reader already has.
  $('label-refresh-font').addEventListener('click', refreshLabelFontSizes);
  // Locking takes the layouts that are already on screen as the frozen ones, so
  // the very next font change is a restyle rather than a re-layout.  Unlocking
  // only *arms* the layout for the next change: it does not re-flow anything by
  // itself, because flipping a switch must not move the names under the pointer.
  $('label-lock').addEventListener('change', () => {
    if (labelLockOn()) {
      labelLockKeys.disk = state.labels.disk ? labelLayoutKey('disk') : null;
      labelLockKeys.region = state.labels.region ? labelLayoutKey('region') : null;
      drawAllLabels();
      setStatus(state.labels.disk
        ? '已锁定当前标注布局：改字号只改变字形大小，位置与避让不再重排'
        : '已锁定标注布局：下一版标注排好后不再移动');
      return;
    }
    labelLockKeys.disk = null;
    labelLockKeys.region = null;
    drawAllLabels();
    setStatus(state.labelsOn
      ? '已解锁标注布局（当前这一版不动，下次条件变化时才重排）'
      : '已解锁标注布局');
  });
  $('preview-px').addEventListener('change', () => {
    $('preview-px').value = String(previewPixels());
    setStatus('预览分辨率已改，点「更新月面」重渲染');
  });
  // Colours, stroke and line style are client-side: the plan carries the
  // geometry, so a restyle is a redraw, never a round trip.
  for (const id of ['outline-width', 'outline-style', 'outline-color', 'label-colour',
                    'text-color', 'text-stroke', 'text-stroke-width',
                    'marker-stroke', 'marker-stroke-width', 'text-bold']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      drawAllLabels();
      drawMagnifier();
      setStatus(id.startsWith('text-') || id.startsWith('marker-') || id === 'text-bold'
        ? '文字样式已更新' : '轮廓样式已更新');
    });
  }
  // The geometric overlays are pure page-side geometry too: switching one on
  // redraws the disk's label layer, and an export that follows picks them up
  // from the same switches.  Each is a pill whose filled state says whether it
  // is on, so the checkbox itself stays out of the layout.
  for (const id of DISK_MARKS.map((name) => 'mark-' + name)) {
    const box = $(id);
    box.addEventListener('change', () => {
      syncMarkChips();
      // A geometric overlay draws on the label layer, so switching one on with
      // the label layer off used to change nothing at all.
      ensureLabelsOn();
      drawViewLabels('disk');
      const names = { 'mark-terminator': '阴影带曲线', 'mark-poles': '南北极',
        'mark-limb': '外缘轮廓圆', 'mark-equator': '赤道', 'mark-libration': '中央零点与天平动点',
        'mark-subsolar': '日下点', 'mark-nearside': '近地侧边界', 'mark-ruler': '标尺' };
      if (id === 'mark-ruler') {
        // Switching it off is also what the × does, so the selection goes with
        // it: a hidden ruler with a live × beside it would be a ghost.
        ruler.selected = false;
        if (box.checked && !(ruler.km > 0) && state.geometry) {
          ruler.km = defaultRulerKm(diskGeom(), DISK.width);
        }
        drawViewLabels('disk');
      }
      setStatus(`${box.checked ? '已标注' : '已取消'}${names[id] || '几何标注'}`);
    });
  }
  syncMarkChips();
  // Orientation and noise are picture operations, so they go through the
  // processing path -- no ray march, and the zoom on both panes is untouched.
  // The angle has a slider and a typed field, and the two keep each other.
  const setRotation = (deg, from) => {
    const value = Math.max(-180, Math.min(180, Number(deg) || 0));
    if (from !== 'slider') $('image-rotation').value = String(value);
    if (from !== 'number') $('image-rotation-num').value = value.toFixed(1);
  };
  $('image-rotation').addEventListener('input', () => {
    setRotation($('image-rotation').value, 'slider');
    reprocess();
  });
  $('image-rotation-num').addEventListener('change', () => {
    setRotation($('image-rotation-num').value, 'number');
    reprocess(0);
  });
  for (const id of ['noise-poisson', 'noise-gaussian']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('input', () => { updateNoiseInfo(); reprocess(); });
  }
  for (const id of ['mirror-h', 'mirror-v']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('click', () => {
      const wanted = element.getAttribute('aria-pressed') === 'true' ? 'none'
        : (id === 'mirror-h' ? 'horizontal' : 'vertical');
      setMirrorMode(wanted);
      reprocess(0);
      setStatus(wanted === 'none' ? '镜像已关闭' : `已${wanted === 'horizontal' ? '左右' : '上下'}镜像`);
    });
  }
  $('download').addEventListener('click', (event) => {
    const target = downloadTarget();
    const plain = state.downloads[target];
    if (!plain) return;
    const format = SAVE_FORMATS[$('save-format').value] || SAVE_FORMATS.png;
    const wantsLabels = $('download-annotated').checked && state.labelsOn;
    // Only the untouched PNG can go straight out as the link's own href; any
    // other format has to be encoded, and the labels drawn, in a canvas.
    if (!wantsLabels && format.type === 'image/png') return;
    event.preventDefault();
    savedImageBlob(target, (blob, format) => {
      if (!blob) { setStatus('这张图还没生成', 'error'); return; }
      const base = downloadFileName(target);
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${base}.${format.extension}`;
      link.click();
      setStatus(`已保存 ${link.download}`);
    });
  });
  // 导出 writes the name that is in the field into web/settings/; 导入 opens a
  // file chooser and reads the .conf the reader picks -- the server's own
  // settings are the field's datalist, so a name can still be typed.
  $('settings-save').addEventListener('click', saveSettings);
  $('settings-load').addEventListener('click', () => {
    const picker = $('settings-pick');
    if (!picker) return;
    // showPicker() is the modern way and the only one that opens the dialog in
    // some browsers; a click on the (invisible but rendered) input is the
    // fallback, and it is not an error for the first to be refused.
    try {
      if (picker.showPicker) picker.showPicker();
      else picker.click();
    } catch (error) {
      picker.click();
    }
  });
  $('settings-pick').addEventListener('change', importSettingsFile);
  $('settings-name').addEventListener('focus', () => refreshSettingsList());
  // Picking a name out of the list applies it there and then; Enter does the
  // same, so the field behaves the same whether the name was clicked or typed.
  $('settings-name').addEventListener('change', () => { applyNamedSettings(); });
  $('settings-name').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    applyNamedSettings();
  });
  $('sel-sync-last').addEventListener('click', syncBackToLastSelection);
  updateSelectionSyncButton();
  $('sel-reset-rot').addEventListener('click', () => {
    if (state.selection) state.selection.rotationDeg = 0;
    syncSelectionInputs();
    // Same path an edit on the disk takes: the box, the window that shows it
    // and the readouts all follow one change.
    afterSelectionChange();
  });
  // Fine control: the typed angle is authoritative and pushes into the box.
  $('sel-rotation').addEventListener('input', () => {
    if (state.selection) state.selection.rotationDeg = num('sel-rotation') || 0;
    drawDisk();
    drawMagnifier();
  });
  // The zone is a whole number of hours: whatever was typed or stepped is
  // rounded, clamped and given its sign before it becomes a request field, so
  // "8.5" cannot reach the ephemeris, "-99" cannot become a 99-hour day, and
  // the field reads "UTC+8" rather than "UTC 8".
  $('tz').addEventListener('change', () => {
    const wanted = Math.round(Number($('tz').value));
    const offset = Number.isFinite(wanted) ? Math.max(-12, Math.min(14, wanted)) : 8;
    $('tz').value = (offset >= 0 ? '+' : '') + String(offset);
    autoRefresh();
    setStatus(`时区已改为 UTC${offset >= 0 ? '+' : ''}${offset}，重新渲染`);
  });
  for (const id of ['when', 'lat', 'lon', 'preview-px']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      autoRefresh();
      setStatus('观测信息已改，全月面会自动重渲染');
    });
  }
  for (const id of ['aperture', 'focal-ratio', 'focal-length', 'barlow', 'back-focus',
                    'obstruction', 'sensor-w', 'sensor-h', 'pixel-um',
                    'sensor-color']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      applySensorType();
      applyLock();
      syncSelectionInputs();
      drawDisk();
      reprocess(200);
    });
  }
  $('sel-locked').addEventListener('change', () => {
    const ok = updateLockHint();
    if (ok) { applyLock(); syncSelectionInputs(); drawDisk(); }
  });

  // Everything in the processing group is an array operation on the cached
  // frame: no re-render, so these can fire freely.
  $('diffraction').addEventListener('input', () => {
    const value = Number($('diffraction').value) || 0;
    $('diffraction-out').textContent = value > 0 ? `${(value * 100).toFixed(0)}%` : '关';
    reprocess();
  });
  // 视宁度 has a slider and a typed field, and the two keep each other -- the
  // same pair the rotation angle uses, for the same reason: 0.85″ is a number a
  // reader knows, and a 0–4″ track cannot hit it.
  const setSeeing = (value, from) => {
    const clamped = Math.max(0, Math.min(4, Number(value) || 0));
    if (from !== 'slider') $('seeing').value = String(clamped);
    if (from !== 'number') $('seeing-num').value = clamped.toFixed(2);
  };
  $('seeing').addEventListener('input', () => {
    setSeeing($('seeing').value, 'slider');
    reprocess();
  });
  $('seeing-num').addEventListener('change', () => {
    setSeeing($('seeing-num').value, 'number');
    reprocess(0);
  });
  for (const id of ['blur-strength', 'sharpen-radius', 'sharpen-strength',
                    'saturation', 'gain', 'offset', 'gamma']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('input', () => { reprocess(); });
  }

  // Output size and selection change what has to be ray-marched.
  for (const id of ['out-px', 'out-ref', 'out-scale', 'out-samples']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => setStatus('参数已变，请点「更新模拟」'));
  }
  for (const id of ['sel-lon', 'sel-lat']) {
    const element = $(id);
    if (!element) continue;
    element.addEventListener('change', () => {
      selectionFromInputs();
      drawDisk();
      afterSelectionChange();
      setStatus('选框已改，请点「更新模拟」');
    });
  }
  // Choosing to trust the typed instrument parameters is also what fixes the
  // selection's shape to that camera's frame.
  $('manual-train').addEventListener('change', () => {
    $('sel-locked').checked = $('manual-train').checked;
    const ok = updateLockHint();
    if (ok) applyLock();
    syncSelectionInputs();
    drawDisk();
  });
  // The magnifier, the search box, the instrument tabs and the result tabs are
  // wired here, with everything else: they were defined but never attached,
  // which is why the floating window ignored the mouse and the tabs could not
  // be clicked.
  wireFeatureSearch();
  setResultTab('raw');
  setTrainTab('telescope');
  for (const button of document.querySelectorAll('#result-tabs .tab')) {
    button.addEventListener('click', () => {
      setResultTab(button.dataset.tab);
      setStatus(button.dataset.tab === 'proc' ? '后期成片' : '几何模拟');
    });
  }
  for (const button of document.querySelectorAll('#train-tabs .tab')) {
    button.addEventListener('click', () => setTrainTab(button.dataset.train));
  }
  // 天空位置示意图: the door, the window, and the controls inside it.
  $('sky-open').addEventListener('click', openSky);
  $('sky-close').addEventListener('click', closeSky);
  $('sky-refresh').addEventListener('click', refreshSky);
  $('sky-reset').addEventListener('click', () => {
    state.skyView = null;
    drawSky();
  });
  $('sky-span').addEventListener('change', refreshSky);
  // 这张图有自己的时刻：日期、时间、时区改了就重算，± 一组按钮把它当钟表走。
  for (const id of ['sky-date', 'sky-time', 'sky-tz']) {
    $(id).addEventListener('change', refreshSky);
  }
  $('sky-now').addEventListener('click', () => {
    skyFieldsFromMain();
    refreshSky();
  });
  for (const button of document.querySelectorAll('[data-range="sky"]')) {
    button.addEventListener('click', () => nudgeSky(button.dataset.nudge));
  }
  for (const id of ['sky-label-step', 'sky-labels', 'sky-elongation']) {
    $(id).addEventListener('change', drawSky);
  }
  $('sky-closeup').addEventListener('change', () => {
    state.skyCloseupTouched = true;
    state.skyView = null;
    drawSky();
  });
  $('sky-save').addEventListener('click', saveSkyPng);
  $('sky-canvas').addEventListener('pointerdown', skyCanvasDown);
  $('sky-canvas').addEventListener('pointermove', skyCanvasHover);
  $('sky-canvas').addEventListener('pointerleave', () => {
    state.skyHoverEdge = null;
    drawSky();
  });
  $('sky-canvas').addEventListener('wheel', skyCanvasWheel, { passive: false });
  $('sky-canvas').addEventListener('dblclick', skyCanvasDoubleClick);
  $('sky-head').addEventListener('pointerdown', skyHeadDown);
  for (const handle of document.querySelectorAll('.sky-resize')) {
    handle.addEventListener('pointerdown', (event) => {
      skyResizeStart(event, handle.dataset.skyResize || 'se');
    });
  }
  document.addEventListener('pointermove', skyPointerMove);
  document.addEventListener('pointerup', skyPointerUp);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.skyOpen) closeSky();
  });
  // 并行数 is a per-render ceiling, not a preference that needs a re-render
  // by itself: the next render reads it.  Saying so is enough.
  $('parallel').addEventListener('change', () => {
    const value = num('parallel');
    setStatus(value === null
      ? '并行数：自动（全部核心），下一次渲染生效'
      : `并行数：${Math.round(value)}，下一次渲染生效`);
  });

  // A window resize is the one event that changes the height the disk has to
  // fill, so the square is measured again -- unless the reader has already
  // chosen a width by dragging the divider, in which case theirs stands.
  window.addEventListener('resize', () => {
    fitAll();
    fitDiskColumn();
    if (state.skyOpen) {
      positionSkyWindow(false);
      drawSky();
    }
  });
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(() => fitAll());
    for (const name of Object.keys(views)) {
      if (views[name].body) observer.observe(views[name].body);
    }
  }
  // The first layout is complete by now (every element is in the document), so
  // the disk starts square rather than at whatever 1fr happened to give.  The
  // fit is a nicety and the render is not: if measuring ever goes wrong, the
  // picture still has to appear.
  try {
    fitDiskColumn();
  } catch (error) {
    console.error('[layout] the disk column could not be fitted:', error);
  }

  refreshDisk();
}

/** Anything that escapes a handler lands here rather than in a console nobody
 *  is looking at: the page has one line for "what just happened", and a silent
 *  failure is worse than a noisy one. */
window.addEventListener('error', (event) => {
  const message = event && event.message ? event.message : '未知错误';
  setStatus('页面脚本出错：' + message, 'error');
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event && event.reason ? event.reason : '';
  setStatus('请求失败：' + String((reason && reason.message) || reason), 'error');
});

document.addEventListener('DOMContentLoaded', init);
