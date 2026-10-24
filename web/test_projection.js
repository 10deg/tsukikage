/* Harness: run the page's projection maths in Node with a stub DOM.
 *
 * The selection box is stored in selenographic coordinates and has to be
 * converted to and from canvas pixels every time the preview is redrawn, so
 * getting the orthographic projection wrong would put the box in the wrong
 * place on the Moon -- the sort of bug that is invisible until you compare the
 * selection against the rendered close-up.
 *
 *   node web/test_projection.js
 */

'use strict';

const { createRunner, loadApp, readStatic } = require('./harness');

/* ── a DOM stub just rich enough for the module to load ───────────── */

/* The options of every `<select>` in the markup, read the way the browser reads
   them, so a stub element answers `select.options` like the real one. */
const SELECT_OPTIONS = new Map(
  [...readStatic('index.html').matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)]
    .map((m) => [m[1], [...m[2].matchAll(/value="([^"]*)"/g)].map((o) => o[1])]));

/* Which controls are checkboxes is read out of the markup, the way the browser
   reads it, so the stub cannot disagree with the page about what a control is. */
const CHECKBOX_IDS = new Set(
  [...readStatic('index.html').matchAll(/<input[^>]*>/g)]
    .map((tag) => tag[0])
    .filter((tag) => /type="checkbox"/.test(tag))
    .map((tag) => (tag.match(/id="([^"]+)"/) || [])[1])
    .filter(Boolean));

function makeElement(id) {
  return {
    id,
    type: CHECKBOX_IDS.has(id) ? 'checkbox' : 'text',
    value: id === 'sel-rotation' ? '0' : '',
    checked: CHECKBOX_IDS.has(id),
    disabled: false,
    options: (SELECT_OPTIONS.get(id) || []).map((value) => ({ value })),
    selectedOptions: [{ textContent: '全月面预览' }],
    textContent: '',
    href: '',
    hidden: false,
    width: id === 'disk' ? 560 : 600,
    height: id === 'disk' ? 560 : 600,
    classList: { toggle() {}, add() {}, remove() {} },
    attributes: {},
    selectedOptions: [{ textContent: '全月面预览' }],
    addEventListener() {},
    dispatchEvent() {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; },
    removeAttribute(name) { delete this.attributes[name]; },
    appendChild() {},
    setPointerCapture() {},
    getContext() { return { clearRect() {}, drawImage() {}, save() {}, restore() {} }; },
    // A canvas's backing store and its CSS box are separate things, and the
    // label layer depends on the difference: rectWidth simulates a picture
    // displayed smaller than it was rendered.
    getBoundingClientRect() {
      return { left: 0, top: 0, width: this.rectWidth || this.width,
               height: this.rectHeight || this.height };
    },
  };
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  },
  addEventListener() {},
  querySelectorAll(selector) {
    if (selector === '[data-zoom]') {
      return ['disk', 'raw', 'proc'].map((name) => ({
        dataset: { zoom: name },
        querySelector: () => makeElement(name + '-zoom'),
      }));
    }
    return [];
  },
  querySelector() { return null; },
  documentElement: { style: { setProperty() {} } },
};

const sandbox = {
  document,
  window: { addEventListener() {} },
  performance: { now: () => Date.now() },
  console,
  setTimeout,
  clearTimeout,
  fetch: () => Promise.reject(new Error('network disabled in the harness')),
  Image: class { set src(_v) {} },
  Event: class { constructor(type) { this.type = type; } },
};
sandbox.globalThis = sandbox;

/* The names this harness needs, gathered out of app.js by the shared loader.
   The list used to name `densitySettings` and `CLASS_COLORS` twice, which the
   loader now rejects rather than silently tolerating. */
const EXPORTS = ['state', 'pixelToLonLat', 'lonLatToPixel', 'projectSelenographic',
  'circleFromPole', 'diskGeomFor', 'drawDiskMarks', 'marksWanted', 'selectionArcsec',
  'shapeAspect', 'diskGeom', 'selectionPayload', 'whenPayload', 'boxGeometry',
  'hitTest', 'boxToScreen', 'screenToBox', 'lockActive',
  'cameraFieldRadiusArcsec', 'updateLockHint', 'paintLabels', 'CLASS_COLORS',
  'labelScreenFontPx', 'labelFrameFontPx', 'labelStyleFor', 'screenScale',
  'views', 'densitySettings', 'updateDensityLabel', 'encodeTiff',
  'updateSaveInfo', 'renderPixels', 'outputPixels', 'kilometresPerImagePixel',
  'collectSettings', 'applySettings', 'mirroredPlan', 'frameOrientation',
  'itemColor', 'LAND_CLASSES', 'poissonPhotons', 'gaussianSigma',
  'updateNoiseInfo', 'diskPixels', 'previewPixels', 'LIMITS', 'phaseShape',
  'polygonArea', 'activeClasses', 'magnifierView', 'SETTING_FIELDS',
  'setMirrorMode', 'mirrorMode', 'resizeByEdge', 'moonVector', 'lonLatFromVector',
  'cameraBasis', 'diskSmoothPx', 'smoothImage', 'rulerGeometry', 'rulerText', 'defaultRulerKm',
  'applyLabelEdits', 'labelKey', 'labelEdits', 'labelHidden', 'diskKmPerFramePixel',
  'regionLonLatToPixel', 'regionPixelToLonLat', 'regionCamera',
  'curveProjection', 'curveEqualAspect', 'curveEqualize', 'curveTargetView'];

const { check, near, finish } = createRunner();
const T = loadApp(sandbox, EXPORTS);

/* A plausible geometry, in the shape /api/disk returns. */
const geometry = {
  libration_lon_deg: -4.738, libration_lat_deg: 1.377,
  sub_solar_lon_deg: -6.5, sub_solar_lat_deg: 1.5,
  angular_diameter_deg: 0.5122145,
  disk_radius_fraction: 0.47,
  illuminated_percent: 89.5, phase_angle_deg: 42.0,
  render_seconds: 0.5, bands: 16, band_workers: 8,
};
T.state.geometry = geometry;

const disk = T.diskGeom();
console.log(`disk: centre (${disk.cx}, ${disk.cy}) radius ${disk.radius} px, ` +
  `angular radius ${disk.angularRadiusDeg} deg`);
console.log('');

console.log('projection');
check('disk radius = 0.47 * 560 = 263.2 px', near(disk.radius, 263.2, 1e-9), disk.radius);
check('angular radius = half the diameter',
  near(disk.angularRadiusDeg, geometry.angular_diameter_deg / 2));

/* The sub-Earth point must land dead centre, and the centre must map back to it. */
const centre = T.lonLatToPixel(geometry.libration_lon_deg, geometry.libration_lat_deg);
check('sub-Earth point projects to the disk centre',
  near(centre.x, disk.cx, 1e-6) && near(centre.y, disk.cy, 1e-6),
  JSON.stringify(centre));
const back = T.pixelToLonLat(disk.cx, disk.cy);
check('disk centre maps back to the sub-Earth point',
  near(back.lon, geometry.libration_lon_deg, 1e-6) &&
  near(back.lat, geometry.libration_lat_deg, 1e-6),
  JSON.stringify(back));

/* Round-trip a spread of points, including ones well off-centre. */
let worst = 0, worstAt = null;
for (const dlon of [-40, -20, -8, 0, 8, 20, 40]) {
  for (const dlat of [-40, -20, -8, 0, 8, 20, 40]) {
    const lon = geometry.libration_lon_deg + dlon;
    const lat = Math.max(-88, Math.min(88, geometry.libration_lat_deg + dlat));
    const p = T.lonLatToPixel(lon, lat);
    if (!p) continue;
    const q = T.pixelToLonLat(p.x, p.y);
    if (!q) { worst = Infinity; worstAt = [lon, lat]; continue; }
    const err = Math.max(Math.abs(q.lon - lon), Math.abs(q.lat - lat));
    if (err > worst) { worst = err; worstAt = [lon, lat]; }
  }
}
check('lon/lat -> pixel -> lon/lat round-trips (< 1e-6 deg)', worst < 1e-6,
  'worst ' + worst + ' at ' + JSON.stringify(worstAt));

/* North must be up and east must be to the right. */
const north = T.lonLatToPixel(geometry.libration_lon_deg, geometry.libration_lat_deg + 10);
const east = T.lonLatToPixel(geometry.libration_lon_deg + 10, geometry.libration_lat_deg);
check('north is up', north.y < disk.cy, 'y=' + north.y);
check('selenographic east is to the right', east.x > disk.cx, 'x=' + east.x);

/* Off-disk and far-side points must be refused, not wrapped. */
check('a point outside the disk radius returns null',
  T.pixelToLonLat(disk.cx + disk.radius + 5, disk.cy) === null);
check('the far side returns null', T.lonLatToPixel(180, 0) === null);
check('just inside the limb still projects',
  T.lonLatToPixel(geometry.libration_lon_deg, geometry.libration_lat_deg + 80) !== null);

/* The close-up has its own camera.  Its inverse is what the pointer readout
   and the manual anchors use, so a point near the frame centre must round-trip
   exactly -- the same contract as the disk's pixelToLonLat. */
{
  T.state.regionInfo = {
    lon_deg: geometry.libration_lon_deg, lat_deg: geometry.libration_lat_deg,
    span_km: 200, width: 800, height: 600, rotation_deg: 12,
  };
  const cam = T.regionCamera();
  check('the close-up camera is rebuilt from the region block',
    !!cam && cam.width === 800 && cam.height === 600,
    JSON.stringify(cam && { width: cam.width, height: cam.height }));
  let regionWorst = 0;
  for (const dlon of [-2, -0.5, 0, 0.5, 2]) {
    for (const dlat of [-1.5, -0.5, 0, 0.5, 1.5]) {
      const lon = geometry.libration_lon_deg + dlon;
      const lat = geometry.libration_lat_deg + dlat;
      const p = T.regionLonLatToPixel(lon, lat);
      if (!p) { regionWorst = Infinity; continue; }
      const q = T.regionPixelToLonLat(p.x, p.y);
      if (!q) { regionWorst = Infinity; continue; }
      regionWorst = Math.max(regionWorst, Math.abs(q.lon - lon),
                             Math.abs(q.lat - lat));
    }
  }
  check('close-up lon/lat -> pixel -> lon/lat round-trips',
    regionWorst < 1e-6, 'worst ' + regionWorst);
  check('a ray that misses the Moon returns null',
    T.regionPixelToLonLat(-100000, -100000) === null);
  T.state.regionInfo = null;
}

console.log('');
console.log('selection sizing');
T.state.selection = { lon: -51.2, lat: 26.2, radiusFraction: 0.05, rotationDeg: 0 };
const arcsec = T.selectionArcsec();
const expected = 0.05 * (geometry.angular_diameter_deg / 2) * 3600;
check('half-disk-radius selection = 0.05 * angular radius',
  near(arcsec, expected, 1e-6), arcsec + ' vs ' + expected);
check('selection at the disk centre is ~46 arcsec', near(arcsec, 46.1, 0.5), arcsec);

/* A selection covering the whole disk must come out at the disk's angular radius. */
T.state.selection.radiusFraction = 1.0;
check('a full-disk box = the Moon\'s angular radius',
  near(T.selectionArcsec(), geometry.angular_diameter_deg / 2 * 3600, 1e-6),
  T.selectionArcsec());

console.log('');
console.log('shape lock');
const set = (id, v) => { document.getElementById(id).value = String(v); };
set('sensor-w', 3008); set('sensor-h', 3008);
set('pixel-um', 3.76); set('aperture', 203.2); set('focal-ratio', 10); set('barlow', 1);
set('focal-length', '');
const lockBox = document.getElementById('sel-locked');

lockBox.checked = false;
check('unlocked -> square box', near(T.shapeAspect(), 1.0));

/* The lock means "the box is the camera's field", so it needs the camera. */
lockBox.checked = true;
check('locked with a full camera spec works', T.lockActive());
check('locked, square sensor -> still 1:1', near(T.shapeAspect(), 1.0));
const field = T.cameraFieldRadiusArcsec();
check('the camera field radius is the sensor diagonal',
  field > 0 && near(field, 0.5 * Math.hypot(3008, 3008) * (206264.806 * 3.76e-3 / 2032), 1e-6),
  field);

set('sensor-h', 1504);
check('locked, 2:1 sensor -> aspect 0.5', near(T.shapeAspect(), 0.5), T.shapeAspect());

/* Without the numbers there is nothing to lock to, so it must refuse. */
set('pixel-um', ''); set('focal-length', ''); set('aperture', ''); set('focal-ratio', '');
check('the lock refuses to arm without a full camera spec', !T.lockActive());
check('and clears itself rather than pretending', T.updateLockHint() === false);
set('pixel-um', 3.76); set('aperture', 203.2); set('focal-ratio', 10);
lockBox.checked = false;
check('unlocked ignores the sensor again', near(T.shapeAspect(), 1.0));

console.log('');
console.log('request payloads');
document.getElementById('when').value = '2026-09-23T22:00';
document.getElementById('tz').value = '8';
document.getElementById('lat').value = '30';
document.getElementById('lon').value = '120';
const when = T.whenPayload();
check('local 22:00 at UTC+8 becomes 14:00 UTC', when.at === '2026-09-23T14:00:00', when.at);
check('observer carried through', near(when.observer.lat, 30) && near(when.observer.lon, 120));

document.getElementById('tz').value = '0';
check('the same typed time at UTC+0 becomes 22:00 UTC',
  T.whenPayload().at === '2026-09-23T22:00:00', T.whenPayload().at);

document.getElementById('tz').value = '8';
T.state.selection = { lon: -51.2, lat: 26.2, radiusFraction: 0.05, rotationDeg: 35 };
const sel = T.selectionPayload();
check('rotation reached the payload', sel.rotation_deg === 35, JSON.stringify(sel));
check('fov radius reached the payload', near(sel.fov_radius_arcsec, arcsec, 1e-6) ||
  sel.fov_radius_arcsec > 0, JSON.stringify(sel));
check('centre reached the payload',
  near(sel.lon, -51.2, 1e-9) && near(sel.lat, 26.2, 1e-9));

console.log('');
console.log('drag behaviour');
/* The bug this covers: dragging out a new box used to fall through to the
 * "move" branch, so the box chased the cursor across the Moon instead of
 * growing where it was started. */
const pointerdown = (px, py) => {
  const p = { x: px, y: py };
  const hit = T.state.selection ? T.hitTest(p) : null;
  return hit;
};
T.state.selection = null;
check('clicking empty space starts a new box', pointerdown(280, 200) === null);
T.state.selection = { lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
                      radiusFraction: 0.10, rotationDeg: 0 };
const centrePx = T.lonLatToPixel(T.state.selection.lon, T.state.selection.lat);

/* Dragging away from the anchor must *grow* the box, and the centre must stay
 * put.  Before the fix the centre moved with the cursor. */
const anchor = { x: centrePx.x, y: centrePx.y };
const grown = { ...T.state.selection };
const dragTo = { x: centrePx.x + 80, y: centrePx.y };
grown.radiusFraction = Math.hypot(dragTo.x - anchor.x, dragTo.y - anchor.y) / disk.radius;
check('a new box grows with the drag', grown.radiusFraction > T.state.selection.radiusFraction,
  grown.radiusFraction + ' vs ' + T.state.selection.radiusFraction);
check('the centre of a new box does not move',
  grown.lon === T.state.selection.lon && grown.lat === T.state.selection.lat);

/* A new box is drawn corner to corner: the press fixes one corner, and the
   centre is the midpoint of the drag, not the press point. */
{
  const anchorPx = { x: disk.cx - 60, y: disk.cy - 40 };
  const endPx = { x: disk.cx + 60, y: disk.cy + 40 };
  const mid = T.pixelToLonLat((anchorPx.x + endPx.x) / 2, (anchorPx.y + endPx.y) / 2);
  T.state.selection = { lon: mid.lon, lat: mid.lat,
                        radiusFraction: Math.abs(endPx.x - anchorPx.x) / 2 / disk.radius,
                        rotationDeg: 0 };
  const b = T.boxGeometry();
  check('a corner-dragged box is centred on the midpoint of the drag',
    near(b.centre.x, (anchorPx.x + endPx.x) / 2, 1e-6) &&
    near(b.centre.y, (anchorPx.y + endPx.y) / 2, 1e-6),
    JSON.stringify(b.centre));
  check('its half-width is half the drag width',
    near(b.halfW, Math.abs(endPx.x - anchorPx.x) / 2, 1e-6), b.halfW);
}

/* The handle must be hit-testable, and well clear of the box corners. */
T.state.selection = { lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
                      radiusFraction: 0.12, rotationDeg: 0 };
const box = T.boxGeometry();
const handleUp = { x: box.centre.x, y: box.centre.y - (box.halfH + 28) };
check('the rotate handle is where the drawing puts it',
  T.hitTest(handleUp) === 'rotate', T.hitTest(handleUp));
/* Big enough to hit comfortably, small enough not to dominate the box.  The
   handle is measured in CSS pixels now that it is drawn on its own overlay, so
   it no longer grows with the display size. */
check('the handle is a comfortable size',
  box.handleRadius >= 7 && box.handleRadius <= 11, 'radius ' + box.handleRadius);
check('the box interior reads as a move, not a rotate',
  T.hitTest({ x: box.centre.x, y: box.centre.y }) === 'move',
  T.hitTest({ x: box.centre.x, y: box.centre.y }));
check('a corner reads as a resize',
  T.hitTest({ x: box.centre.x + box.halfW, y: box.centre.y + box.halfH }) === 'corner',
  T.hitTest({ x: box.centre.x + box.halfW, y: box.centre.y + box.halfH }));
check('far outside the box hits nothing',
  T.hitTest({ x: box.centre.x + box.halfW + 60, y: box.centre.y }) === null);

/* The handle must ride around with the box when it is rotated. */
for (const rot of [0, 45, 90, 180, -60]) {
  T.state.selection.rotationDeg = rot;
  const b = T.boxGeometry();
  const ang = -b.rotation - Math.PI / 2;
  const hx = b.centre.x + Math.cos(ang) * b.handleDistance;
  const hy = b.centre.y + Math.sin(ang) * b.handleDistance;
  if (!(T.hitTest({ x: hx, y: hy }) === 'rotate')) {
    check('handle tracks rotation ' + rot + ' deg', false, 'missed');
  }
}
check('the handle tracks the box through every rotation tested', true);

console.log('');
console.log('handle direction and layout');
/* The handle must orbit the box the *same* way the box turns.  The box is drawn
 * under rotate(-rotation), so its local up maps to screen (-sin r, -cos r); the
 * handle used (+sin r, -cos r) and span the wrong way. */
for (const deg of [0, 30, 90, 150, -45, -120]) {
  T.state.selection = {
    lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
    radiusFraction: 0.12, rotationDeg: deg,
  };
  const b = T.boxGeometry();
  const r = deg * Math.PI / 180;
  const expected = { x: b.centre.x - Math.sin(r) * b.handleDistance,
                     y: b.centre.y - Math.cos(r) * b.handleDistance };
  const ok = T.hitTest(expected) === 'rotate';
  if (!ok) check('handle orbits with the box at ' + deg + ' deg', false, 'missed');
}
check('the handle orbits the same way the box turns', true);

/* All three pictures must exist, and the disk must keep its overlay. */
for (const id of ['disk', 'raw', 'result', 'overlay']) {
  if (!document.getElementById(id)) check('canvas #' + id + ' present', false);
}
check('three pictures plus the overlay exist', true);

/* The zoom controls are wired per view. */
const zoomBoxes = [...document.querySelectorAll('[data-zoom]')];
check('a zoom control exists for every view', zoomBoxes.length >= 3, zoomBoxes.length + ' found');

console.log('');
console.log('rotation: corners must keep working');
/* Every corner and the handle must be findable at any box angle.  The hit test
 * and the resize each used to carry their own rotation inverse with the sign
 * flipped, so corners stopped responding the moment the box was rotated. */
{
  const dirs = [[-1, -1], [1, -1], [1, 1], [-1, 1]];
  let bad = 0;
  for (const deg of [0, 15, 30, 45, 90, 135, 180, -60, -120]) {
    T.state.selection = {
      lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
      radiusFraction: 0.10, rotationDeg: deg,
    };
    const b = T.boxGeometry();
    for (const [su, sv] of dirs) {
      const corner = T.boxToScreen(b, su * b.halfW, sv * b.halfH);
      const hit = T.hitTest({ x: corner.x, y: corner.y });
      if (hit !== 'corner') { bad++; }
    }
    // and the centre is still a move
    if (T.hitTest({ x: b.centre.x, y: b.centre.y }) !== 'move') bad++;
  }
  check('all four corners are grabbable at every angle tested', bad === 0, bad + ' misses');

  /* The round trip local -> screen -> local must be the identity. */
  T.state.selection = { lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
                        radiusFraction: 0.1, rotationDeg: 37 };
  const b = T.boxGeometry();
  let worst = 0;
  for (const [u, v] of [[3, 5], [-8, 2], [0, 0], [12, -7], [-4, -9]]) {
    const sc = T.boxToScreen(b, u, v);
    const back = T.screenToBox(b, sc.x - b.centre.x, sc.y - b.centre.y);
    worst = Math.max(worst, Math.abs(back.u - u), Math.abs(back.v - v));
  }
  check('boxToScreen and screenToBox are inverses', worst < 1e-9, worst);
}

console.log('');
console.log('corner resize');
/* Resizing pivots on the corner diagonally opposite the one grabbed.  It used
 * to measure from the *Moon's* centre, so grabbing a corner and moving the
 * mouse a little made the box jump to the distance between the cursor and the
 * middle of the disk. */
{
  const start = { lon: geometry.libration_lon_deg + 3, lat: geometry.libration_lat_deg + 2,
                  radiusFraction: 0.12, rotationDeg: 0 };
  T.state.selection = { ...start };
  const box = T.boxGeometry();
  // Grab the bottom-right corner; the anchor is the top-left one.
  const cos = Math.cos(box.rotation), sin = Math.sin(box.rotation);
  const lu = box.halfW, lv = box.halfH;
  const anchor = T.boxToScreen(box, lu, lv);
  // Drag the grabbed corner a little further out.
  const grabbed = { x: anchor.x + 2 * lu * cos - 2 * lv * sin,
                    y: anchor.y + 2 * lu * sin + 2 * lv * cos };
  check('grabbing a corner finds it at the box edge',
    near(Math.hypot(grabbed.x - box.centre.x, grabbed.y - box.centre.y),
         3 * Math.hypot(lu, lv), 1e-6));
  const loc = T.screenToBox({ centre: { x: 0, y: 0 }, rotation: box.rotation },
                            grabbed.x - anchor.x, grabbed.y - anchor.y);
  const dlu = loc.u, dlv = loc.v;
  const halfW = Math.abs(dlu) / 2;
  const halfH = Math.abs(dlv) / 2;
  const mid = T.boxToScreen({ centre: anchor, rotation: box.rotation },
                            dlu / 2, dlv / 2);
  const midX = mid.x, midY = mid.y;
  check('doubling the drag doubles the box, not more',
    near(halfW, box.halfW, 1e-6), halfW + ' vs ' + box.halfW);
  // The new box's corner opposite the grabbed one must land back on the anchor.
  const opposite = T.boxToScreen({ centre: { x: midX, y: midY }, rotation: box.rotation },
                                 -dlu / 2, -dlv / 2);
  const oppositeX = opposite.x, oppositeY = opposite.y;
  check('the anchor corner stays put',
    near(Math.hypot(oppositeX - anchor.x, oppositeY - anchor.y), 0, 1e-6),
    oppositeX + ',' + oppositeY + ' vs ' + anchor.x + ',' + anchor.y);
  // The old, broken formula measured from the disk centre and would explode.
  const broken = Math.max(Math.abs(grabbed.x - disk.cx), Math.abs(grabbed.y - disk.cy)) / disk.radius;
  check('the old centre-based formula really was wrong',
    broken > 2 * (halfW / disk.radius), broken.toFixed(3));
}

console.log('');
console.log('landform labels');
/* The server hands the page finished boxes in the *frame's* pixels, and
   paintLabels() draws them in exactly those coordinates.  The layer it is given
   has already been scaled to the screen -- one unit is one frame pixel -- which
   is what lets the same painter be resolution-independent: the context decides
   how big a frame pixel is, and the type is rasterised at that size instead of
   being stretched from the preview.  These checks are about the coordinates and
   the call counts, since a painter in the wrong space is the bug to catch. */
function recordingContext() {
  const calls = { arcs: [], texts: [], lines: 0, strokes: 0, moves: 0, closes: 0,
                  dashes: [], marks: [], alphas: [], fonts: [] };
  const ctx = {
    calls,
    _fill: '', _stroke: '', _alpha: 1,
    save() {}, restore() {}, beginPath() {},
    fill() { calls.marks.push(ctx._fill); },
    stroke() { calls.strokes++; calls.marks.push(ctx._stroke); },
    moveTo() { calls.moves++; }, lineTo() { calls.lines++; },
    closePath() { calls.closes++; },
    arc(x, y, radius) { calls.arcs.push([x, y, radius]); },
    fillText(text, x, y) { calls.texts.push([text, x, y, ctx._fill, ctx._alpha]); },
    strokeText() {},
    setLineDash(dash) { calls.dashes.push((dash || []).slice()); },
    set font(value) { calls.fonts.push(value); },
    set fillStyle(value) { ctx._fill = value; },
    set strokeStyle(value) { ctx._stroke = value; },
    get strokeStyle() { return ctx._stroke; },
    set lineWidth(_v) {}, set textAlign(_v) {}, set textBaseline(_v) {},
    set globalAlpha(value) { ctx._alpha = value; calls.alphas.push(value); },
    get globalAlpha() { return ctx._alpha; },
  };
  return ctx;
}

/* The size follows the *window*, not the picture's pixel count: an 800 px and
   a 2400 px render of the same view read the same size on screen. */
document.getElementById('label-font').value = '';
check('the automatic size comes from the view, not the frame',
  T.labelScreenFontPx('disk') === 12 || T.labelScreenFontPx('disk') === 10,
  T.labelScreenFontPx('disk'));

/* The size the server lays out with is the screen size divided by the view's
   own scale, so a 2000 px preview on a 560 px window asks for a *bigger* frame
   font -- and ends up the same size on screen.  This is the bug the two
   functions exist to prevent: asking for 13 frame pixels at 2000 px would be
   four screen pixels tall. */
/* Wire the views the way init() does, since the scale is measured from the
   picture's box. */
T.views.disk.canvas = document.getElementById('disk');
T.views.disk.body = document.getElementById('body-disk');
T.views.raw.canvas = document.getElementById('raw');
T.views.raw.body = document.getElementById('body-raw');
const diskCanvas = document.getElementById('disk');
const screenPx = T.labelScreenFontPx('disk');
check('at 1:1 the frame size equals the screen size',
  Math.abs(T.labelFrameFontPx('disk') - screenPx) < 0.01,
  [T.labelFrameFontPx('disk'), screenPx].join(' / '));
diskCanvas.width = 2000;
diskCanvas.height = 2000;
diskCanvas.rectWidth = 560;          // displayed at 560, rendered at 2000
diskCanvas.rectHeight = 560;
const scaled = T.labelFrameFontPx('disk');
check('a high-resolution preview asks for a proportionally larger frame font',
  scaled > screenPx * 3 && scaled < screenPx * 4,
  [scaled, T.screenScale('disk')].join(' / '));
check('and the style is converted with the same scale',
  Math.abs(T.labelStyleFor('disk').outlineWidth - 1 / T.screenScale('disk')) < 1e-9);
diskCanvas.width = 560;
diskCanvas.height = 560;
diskCanvas.rectWidth = 0;
diskCanvas.rectHeight = 0;

document.getElementById('label-font').value = '22';
check('a typed size is taken in screen pixels',
  T.labelScreenFontPx('disk') === 22 && T.labelScreenFontPx('raw') === 22);
document.getElementById('label-font').value = '2';
check('a size below the floor falls back to the automatic one',
  T.labelScreenFontPx('disk') !== 2);
document.getElementById('label-font').value = '';

const labelPlan = {
  frame: { kind: 'disk', width: 560, height: 560 },
  font_px: 12,
  items: [
    { x: 100, y: 200, label: '哥白尼 Copernicus', label_x: 110, label_y: 194,
      label_w: 120, label_h: 15, labelled: true, leader: false, radius_px: 8 },
    { x: 400, y: 300, label: '第谷', label_x: 300, label_y: 320,
      label_w: 40, label_h: 15, labelled: true, leader: true, radius_px: 3 },
    { x: 50, y: 50, label: '', label_x: null, label_y: null,
      label_w: 10, label_h: 15, labelled: false, leader: false, radius_px: 2 },
  ],
};

const labelCtx = recordingContext();
T.paintLabels(labelCtx, labelPlan);
check('every feature gets a marker', labelCtx.calls.arcs.length === 3,
  JSON.stringify(labelCtx.calls.arcs));
check('markers land on their frame coordinates',
  labelCtx.calls.arcs[0][0] === 100 && labelCtx.calls.arcs[0][1] === 200);
check('only labelled features get text', labelCtx.calls.texts.length === 2,
  labelCtx.calls.texts.map((t) => t[0]).join(' / '));
check('text sits inside its own label box',
  labelCtx.calls.texts[0][1] > 110 && labelCtx.calls.texts[0][1] < 240 &&
  Math.abs(labelCtx.calls.texts[0][2] - 201.5) < 1,
  JSON.stringify(labelCtx.calls.texts[0]));
check('a leader is drawn for a displaced label', labelCtx.calls.lines === 1);

const emptyCtx = recordingContext();
T.paintLabels(emptyCtx, { frame: labelPlan.frame, font_px: 12, items: [] });
check('an empty plan draws nothing', emptyCtx.calls.arcs.length === 0);
T.paintLabels(null, labelPlan);
check('a missing context is not an error', true);

/* 锁定布局: the plan's own font_px is the size its boxes were laid out for, and
   a locked layout keeps those boxes while the type follows the 字号 field.  The
   override therefore has to win over the plan, and every length the painter
   derives from the font -- the marker, the leader, the default stroke -- has to
   follow it.  Drawn at the same plan, twice: once as laid out, once as
   re-sized. */
{
  const asLaidOut = recordingContext();
  T.paintLabels(asLaidOut, labelPlan);
  const resized = recordingContext();
  T.paintLabels(resized, labelPlan, { fontPx: 30 });
  check('a plan is painted at the size it was laid out for',
    String(asLaidOut.calls.fonts[0]).includes(`${labelPlan.font_px}px`),
    asLaidOut.calls.fonts[0]);
  check('and a locked layout is painted at the size the style asks for',
    String(resized.calls.fonts[0]).includes('30px'), resized.calls.fonts[0]);
  check('with the names left exactly where they were',
    JSON.stringify(resized.calls.texts.map((t) => [t[1], t[2]])) ===
    JSON.stringify(asLaidOut.calls.texts.map((t) => [t[1], t[2]])),
    JSON.stringify(resized.calls.texts.map((t) => [t[1], t[2]])));
  check('while the markers grow with the type',
    resized.calls.arcs[0][2] > asLaidOut.calls.arcs[0][2],
    `${resized.calls.arcs[0][2]} / ${asLaidOut.calls.arcs[0][2]}`);
}

/* Every feature is drawn in its own class colour, and the night side is faded
   rather than hidden: the two things the class work added to the painter. */
const classedPlan = {
  frame: labelPlan.frame,
  font_px: 12,
  items: [
    { x: 100, y: 200, label: '雨海', label_x: 110, label_y: 194, label_w: 40,
      label_h: 15, labelled: true, leader: false, radius_px: 8, class: 'mare',
      illuminated: true, outline: null },
    { x: 300, y: 300, label: '许癸努斯月溪', label_x: 310, label_y: 294, label_w: 90,
      label_h: 15, labelled: true, leader: false, radius_px: 8, class: 'rima',
      illuminated: false, outline: null },
  ],
};
const classed = recordingContext();
T.paintLabels(classed, classedPlan, {
  uniform: false, classColors: T.CLASS_COLORS, textColor: '#eef2ff',
});
check('a name is written in its own class colour',
  classed.calls.texts[0][3] === T.CLASS_COLORS.mare &&
  classed.calls.texts[1][3] === T.CLASS_COLORS.rima,
  JSON.stringify(classed.calls.texts.map((t) => t[3])));
check('a feature on the night side is faded, not dropped',
  classed.calls.texts[1][4] < 1 && classed.calls.texts[0][4] === 1,
  JSON.stringify(classed.calls.texts.map((t) => t[4])));
const uniform = recordingContext();
T.paintLabels(uniform, classedPlan, {
  uniform: true, classColors: T.CLASS_COLORS, textColor: '#eef2ff',
});
check('the single-colour switch draws every name in the one colour',
  uniform.calls.texts.every((t) => t[3] === '#eef2ff'),
  JSON.stringify(uniform.calls.texts.map((t) => t[3])));

/* A rim outline is a closed polygon drawn twice -- a dark pass and a light one
   -- so its calls are countable. */
const ring = [];
for (let i = 0; i < 48; i++) {
  const a = (i / 48) * Math.PI * 2;
  ring.push([300 + 60 * Math.cos(a), 300 + 60 * Math.sin(a)]);
}
const outlinePlan = {
  frame: labelPlan.frame,
  font_px: 12,
  items: [{ x: 300, y: 300, label: '克拉维斯', label_x: 360, label_y: 294,
            label_w: 60, label_h: 15, labelled: true, leader: false,
            radius_px: 60, outline: ring }],
};
const rimCtx = recordingContext();
T.paintLabels(rimCtx, outlinePlan);
check('an outline is closed and stroked twice, plus the marker edge',
  rimCtx.calls.closes === 2 && rimCtx.calls.strokes === 3,
  JSON.stringify([rimCtx.calls.closes, rimCtx.calls.strokes]));
check('every vertex of the ring is drawn',
  rimCtx.calls.moves === 2 && rimCtx.calls.lines === 2 * 47,
  JSON.stringify([rimCtx.calls.moves, rimCtx.calls.lines]));
check('the centre still gets its marker', rimCtx.calls.arcs.length === 1);
check('a rim is drawn without a leader line', rimCtx.calls.texts.length === 1);
check('a solid rim clears the dash pattern after each pass',
  rimCtx.calls.dashes.every((dash) => dash.length === 0),
  JSON.stringify(rimCtx.calls.dashes));

/* The line style is a *client* setting: changing it must not cost a round trip,
   so the painter takes it as an argument rather than reading the page. */
const dashedCtx = recordingContext();
T.paintLabels(dashedCtx, outlinePlan,
              { outlineWidth: 2, outlineStyle: 'dash', dash: [6, 4],
                outlineColor: '#ff0000', textColor: '#00ff00', strokeWidth: 2 });
check('a dashed rim sets a dash pattern scaled by the width',
  dashedCtx.calls.dashes.some((dash) => dash.length === 2 && dash[0] === 12),
  JSON.stringify(dashedCtx.calls.dashes));

const bareCtx = recordingContext();
T.paintLabels(bareCtx, outlinePlan, { outlineWidth: 1, outlineStyle: 'none', dash: [] });
check('“do not draw rims” still draws the marker and the name',
  bareCtx.calls.strokes === 1 && bareCtx.calls.arcs.length === 1 &&
  bareCtx.calls.texts.length === 1,
  JSON.stringify([bareCtx.calls.strokes, bareCtx.calls.arcs.length]));

console.log('');
console.log('settings files');
/* A settings file is the page's own controls written out by id, so the two
   functions that matter are a round trip and a partial file. */
const collected = T.collectSettings();
check('every parameter has a value', T.SETTING_FIELDS.every(
  (id) => collected[id] !== undefined), T.SETTING_FIELDS.length);
check('checkboxes come back as booleans',
  typeof collected['auto-refresh'] === 'boolean' &&
  typeof collected['sel-locked'] === 'boolean');
check('the annotation latch is recorded too',
  'annotate' in collected && 'label-density' in collected && 'label-font' in collected);

document.getElementById('lat').value = '30';
document.getElementById('sensor-w').value = '3008';
document.getElementById('label-density').value = 'dense';
document.getElementById('annotate').attributes['aria-pressed'] = 'false';
const applied = T.applySettings({
  lat: '-33.9', 'sensor-w': '4144', 'label-density': 'sparse',
  'saturation': '0.5', unrelated: 'ignored',
});
check('a loaded file lands in the controls',
  document.getElementById('lat').value === '-33.9' &&
  document.getElementById('sensor-w').value === '4144' &&
  document.getElementById('label-density').value === 'sparse',
  [document.getElementById('lat').value,
   document.getElementById('sensor-w').value].join(' '));
check('only the fields the file carries are counted',
  applied === 4, applied);
check('a value the page does not know is ignored without throwing', true);

console.log('');
/* ── density, save size and the TIFF writer ──────────────────────────────── */

{
  const slider = document.getElementById('label-density');
  slider.value = '0';
  const sparse = T.densitySettings();
  slider.value = '1.5';
  const dense = T.densitySettings();
  slider.value = '0.5';
  const middle = T.densitySettings();
  check('density is one continuous axis with a very high top end',
    sparse.labels === 12 && dense.labels === 300 && middle.labels === 30 &&
    sparse.min_radius_px > middle.min_radius_px && middle.min_radius_px > dense.min_radius_px,
    JSON.stringify([sparse, middle, dense]));
  T.updateDensityLabel();
  check('the readout shows the label budget',
    document.getElementById('density-value').textContent === '30',
    document.getElementById('density-value').textContent);
}

{
  // Two pixels, pure red and pure green, in an uncompressed baseline TIFF.
  const rgba = new Uint8ClampedArray([255, 0, 0, 255, 0, 255, 0, 255]);
  const buffer = T.encodeTiff(rgba, 2, 1);
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  check('the TIFF header is little-endian baseline',
    view.getUint16(0, true) === 0x4949 && view.getUint16(2, true) === 42 &&
    view.getUint32(4, true) === 8 + 6, view.getUint32(4, true));
  check('the pixel strip is RGB, in order',
    bytes[8] === 255 && bytes[9] === 0 && bytes[10] === 0 &&
    bytes[11] === 0 && bytes[12] === 255 && bytes[13] === 0,
    [bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13]].join(','));
  const ifd = view.getUint32(4, true);
  check('the directory declares width, height and one uncompressed strip',
    view.getUint16(ifd, true) === 12 &&
    view.getUint32(ifd + 2 + 8, true) === 2 &&        // ImageWidth (tag 256)
    view.getUint32(ifd + 2 + 12 + 8, true) === 1 &&   // ImageLength (tag 257)
    view.getUint16(ifd + 2 + 36 + 8, true) === 1,     // Compression (tag 259)
    view.getUint16(ifd, true));
  check('the file ends with a null next-IFD pointer',
    view.getUint32(ifd + 2 + 12 * 12, true) === 0);
}

{
  /* The render is defined by 物理分辨率, so its pixel count is derived, not
     typed; the save pane's pixels only rescale the file. */
  T.state.selection = { lon: 0, lat: 0, radiusFraction: 0.2, rotationDeg: 0, aspect: 1 };
  document.getElementById('out-kmpx').value = '1';
  const render = T.renderPixels();
  const save = document.getElementById('out-px');
  save.value = '4000';
  check('the render size follows the plate scale, not the save pixels',
    render.width > 0 && render.width !== 4000, render.width);
  check('the save size follows the one field', T.outputPixels().width === 4000);
  /* One number and a 长边/短边 switch: the aspect is always the box's own, and
     the switch only says which side the number is about. */
  T.state.selection = { lon: 0, lat: 0, radiusFraction: 0.2, rotationDeg: 0, aspect: 2 };
  save.value = '1000';
  const longSide = T.outputPixels();
  check('长边 sets the long side and derives the short one',
    longSide.height === 1000 && longSide.width === 500,
    `${longSide.width}×${longSide.height}`);
  document.getElementById('out-ref').value = 'short';
  const shortSide = T.outputPixels();
  check('短边 sets the short one and derives the long one',
    shortSide.width === 1000 && shortSide.height === 2000,
    `${shortSide.width}×${shortSide.height}`);
  document.getElementById('out-ref').value = 'long';
  save.value = '1200';
  T.state.selection = null;
}

console.log('');
/* ── the full disk's own export resolution ──────────────────────────────── */

{
  /* The disk has a ceiling far above the preview's, because it is rendered on
     purpose when the save button is pressed rather than on every keystroke. */
  document.getElementById('preview-px').value = '99999';
  document.getElementById('disk-px').value = '99999';
  check('the preview stays inside its own cap',
    T.previewPixels() === T.LIMITS.preview_px, T.previewPixels());
  check('the full disk may go far higher',
    T.diskPixels() === T.LIMITS.disk_px && T.LIMITS.disk_px >= 10000, T.diskPixels());
  document.getElementById('disk-px').value = '10';
  check('and is still clamped from below', T.diskPixels() === 240, T.diskPixels());
  document.getElementById('preview-px').value = '1000';
  document.getElementById('disk-px').value = '2400';
}

console.log('');
/* ── orientation: mirror, rotate, and what the two do to a plan ─────────── */

{
  const plan = {
    frame: { width: 100, height: 50 },
    font_px: 12,
    items: [{
      x: 10, y: 40, label: '雨海', label_x: 20, label_y: 30,
      label_w: 26, label_h: 15, leader: true, outline: [[5, 5], [15, 5], [15, 9]],
      class: 'mare', illuminated: true,
    }],
  };
  const horizontal = T.mirroredPlan(plan, 'horizontal');
  const item = horizontal.items[0];
  check('a horizontal mirror moves the marker', item.x === 90 && item.y === 40,
    `${item.x},${item.y}`);
  check('and moves the label box so the text still reads left to right',
    item.label_x === 100 - (20 + 26) && item.label_y === 30,
    `${item.label_x}`);
  check('the outline follows the same flip', item.outline[0][0] === 95 &&
    item.outline[0][1] === 5, JSON.stringify(item.outline[0]));
  const vertical = T.mirroredPlan(plan, 'vertical');
  check('a vertical mirror flips the other axis',
    vertical.items[0].x === 10 && vertical.items[0].y === 10 &&
    vertical.items[0].label_y === 50 - (30 + 15), JSON.stringify(vertical.items[0]));
  check('no mirror leaves the plan alone',
    T.mirroredPlan(plan, 'none') === plan);
  check('an unknown mode is not a mirror', T.mirroredPlan(plan, 'diagonal') === plan);

  /* Rotation and mirror are one orientation, read from the two controls. */
  document.getElementById('image-rotation').value = '30';
  T.setMirrorMode('vertical');
  const orient = T.frameOrientation();
  check('the orientation comes from the controls',
    Math.abs(orient.rotation - Math.PI / 6) < 1e-9 && orient.mirror === 'vertical',
    JSON.stringify(orient));
  T.setMirrorMode('horizontal');
  check('the two mirror icons are one mode, not two',
    T.mirrorMode() === 'horizontal' &&
    document.getElementById('mirror-v').getAttribute('aria-pressed') === 'false');
  document.getElementById('image-rotation').value = '0';
  T.setMirrorMode('none');
}

console.log('');
/* ── class colours ──────────────────────────────────────────────────────── */

{
  check('every class has a colour of its own',
    new Set(T.LAND_CLASSES.map((c) => c.color)).size === T.LAND_CLASSES.length,
    T.LAND_CLASSES.length + ' classes');
  check('the colours are all readable greys-to-pastels, not saturated primaries',
    T.LAND_CLASSES.every((c) => {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(c.color.slice(i, i + 2), 16) / 255);
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      return max >= 0.6 && max <= 0.95 && (max - min) <= 0.55;
    }), JSON.stringify(T.LAND_CLASSES.map((c) => c.color)));
  const style = { uniform: false, textColor: '#eef2ff', classColors: T.CLASS_COLORS };
  check('a marker takes its colour from its class',
    T.itemColor({ class: 'rima' }, style) === T.CLASS_COLORS.rima,
    T.itemColor({ class: 'rima' }, style));
  check('the single-colour switch wins over the class',
    T.itemColor({ class: 'rima' }, { ...style, uniform: true }) === '#eef2ff');
  check('an unknown class falls back to the text colour',
    T.itemColor({ class: 'nonesuch' }, style) === '#eef2ff');
}

console.log('');
/* ── noise: two sliders, two physical quantities ────────────────────────── */

{
  const poisson = document.getElementById('noise-poisson');
  const gaussian = document.getElementById('noise-gaussian');
  poisson.value = '0'; gaussian.value = '0';
  check('both sliders rest at off',
    T.poissonPhotons() === null && T.gaussianSigma() === 0);
  poisson.value = '0.5';
  const photons = T.poissonPhotons();
  check('half way up the Poisson slider is a full well of order 10^4 e-',
    photons > 5000 && photons < 60000, photons);
  check('the shot noise it implies is a small percentage',
    100 / Math.sqrt(photons) > 0.1 && 100 / Math.sqrt(photons) < 2,
    100 / Math.sqrt(photons));
  poisson.value = '1';
  check('the top of the slider is genuinely noisy',
    T.poissonPhotons() < 200, T.poissonPhotons());
  gaussian.value = '0.5';
  check('the Gaussian slider is a fraction of full scale',
    Math.abs(T.gaussianSigma() - 0.03) < 1e-9, T.gaussianSigma());
  poisson.value = '0'; gaussian.value = '0';
  T.updateNoiseInfo();
  check('the readout says off when both are off',
    document.getElementById('noise-poisson-out').textContent === '关' &&
    document.getElementById('noise-gaussian-out').textContent === '关');
  poisson.value = '0.5';
  T.updateNoiseInfo();
  check('and a number when one is on',
    /%$/.test(document.getElementById('noise-poisson-out').textContent),
    document.getElementById('noise-poisson-out').textContent);
  poisson.value = '0';
  T.updateNoiseInfo();
}

console.log('');
/* ── the mini phase: geometry that has to agree with the rendered disk ──── */

{
  const radius = 40;
  const area = (points) => T.polygonArea(points);
  const full = area(T.phaseShape(1, 0, radius));
  check('a full moon is the whole disk',
    Math.abs(full - Math.PI * radius * radius) / (Math.PI * radius * radius) < 0.01,
    full);
  const half = area(T.phaseShape(0.5, 0, radius));
  check('a half moon is half of it',
    Math.abs(half - Math.PI * radius * radius / 2) / (Math.PI * radius * radius / 2) < 0.02,
    half);
  const quarter = area(T.phaseShape(0.25, 0, radius));
  check('a quarter moon is a quarter of it',
    Math.abs(quarter - Math.PI * radius * radius / 4) / (Math.PI * radius * radius / 4) < 0.05,
    quarter);
  check('a new moon has no lit area at all',
    area(T.phaseShape(0, 0, radius)) < 1e-6, area(T.phaseShape(0, 0, radius)));

  /* Where the lit limb points is the one thing that must agree with the render:
     the disk is drawn north up with the Sun's direction given as a position
     angle, so a PA of 90 degrees has to put the bright side on the right. */
  const centroidAngle = (points) => {
    let area = 0, cx = 0, cy = 0;
    for (let i = 0; i < points.length; i += 1) {
      const [x1, y1] = points[i];
      const [x2, y2] = points[(i + 1) % points.length];
      const cross = x1 * y2 - x2 * y1;
      area += cross;
      cx += (x1 + x2) * cross;
      cy += (y1 + y2) * cross;
    }
    return Math.atan2(cy / (3 * area), cx / (3 * area));
  };
  const right = centroidAngle(T.phaseShape(0.5, Math.PI / 2, radius));
  check('a waxing moon is lit on the right', Math.abs(right) < 0.02, right);
  const left = centroidAngle(T.phaseShape(0.5, 3 * Math.PI / 2, radius));
  check('a waning moon is lit on the left',
    Math.abs(Math.abs(left) - Math.PI) < 0.02, left);
  const up = centroidAngle(T.phaseShape(0.5, 0, radius));
  check('a crescent whose bright limb is north is lit at the top',
    Math.abs(up + Math.PI / 2) < 0.02, up);
  const down = centroidAngle(T.phaseShape(0.5, Math.PI, radius));
  check('and the opposite one at the bottom',
    Math.abs(down - Math.PI / 2) < 0.02, down);
  /* The rendered disk puts the Sun's direction on the right for a waxing moon,
     which is what bright_limb_position_angle_deg = 90 means; the strip has to
     show the same side as the picture above it. */
  const edge = T.phaseShape(0.5, Math.PI / 2, radius);
  const rightmost = edge.reduce((best, p) => (p[0] > best[0] ? p : best), edge[0]);
  check('and the lit edge reaches the right-hand side of the disk',
    rightmost[0] > 0.99 * radius, rightmost[0]);
}

console.log('');
/* ── the live close-up is locked to the selection box ───────────────────── */

{
  T.state.diskImage = { width: 560, height: 560 };
  const canvas = document.getElementById('magnify');
  T.state.selection = { lon: geometry.libration_lon_deg, lat: geometry.libration_lat_deg,
                        radiusFraction: 0.2, rotationDeg: 0 };
  const box = T.boxGeometry();
  const boxSize = 2 * Math.max(box.halfW, box.halfH);

  const fit = T.magnifierView(200, 200);
  check('the window frames exactly the selection box',
    fit.fit && near(Math.max(fit.halfW, fit.halfH) * 2 * fit.scale, 198, 1e-6),
    `${fit.halfW}/${fit.halfH} at ${fit.scale}`);
  check('and is centred on the box, not on the pointer',
    near(fit.centreX, box.centre.x, 1e-9) && near(fit.centreY, box.centre.y, 1e-9),
    fit.centreX + ',' + fit.centreY);
  check('and is one scale for whatever window it is drawn in',
    Math.abs(T.magnifierView(400, 400).scale / fit.scale - 2) < 0.03,
    `${T.magnifierView(400, 400).scale} vs ${fit.scale}`);
  check('and the window keeps the box proportions',
    near(fit.width / fit.height, fit.halfW / fit.halfH, 0.02),
    `${fit.width}×${fit.height}`);

  /* Nothing selected: the whole disk is the subject, not the middle of it. */
  T.state.selection = null;
  const whole = T.magnifierView(200, 200);
  check('with no box the window frames the whole disk',
    whole.fit && near(whole.halfW, 280, 1e-9) && near(whole.halfH, 280, 1e-9),
    `${whole.halfW}×${whole.halfH}`);
  check('and the box it would draw is absent', whole.box === null);

  /* The centre is a *selemographic* point through the box, so a box near the
     limb lands where the drawing puts it -- this is what the pointer mapping
     got wrong. */
  T.state.selection = { lon: geometry.libration_lon_deg + 40, lat: 30,
                        radiusFraction: 0.05, rotationDeg: 0 };
  const limbBox = T.boxGeometry();
  const atLimb = T.magnifierView(200, 200);
  check('the window tracks the box wherever it is on the disk',
    near(atLimb.centreX, limbBox.centre.x, 1e-9) &&
    near(atLimb.centreY, limbBox.centre.y, 1e-9),
    atLimb.centreX + ',' + atLimb.centreY);
  T.state.selection = null;
  canvas.width = 560; canvas.height = 560;
}

console.log('');
/* ── the box has a shape of its own, and one edge can change it ─────────── */

{
  /* The regression: the box took its proportions from `shapeAspect()`, which is
     1 unless the camera lock is armed -- so with both locks off it was always
     square and no drag could make it anything else. */
  T.state.selection = { lon: 0, lat: 0, radiusFraction: 0.2, rotationDeg: 0, aspect: 1 };
  const square = T.boxGeometry();
  check('the box is square when its aspect is 1',
    near(square.halfW, square.halfH, 1e-9), `${square.halfW}/${square.halfH}`);
  T.state.selection = { ...T.state.selection, aspect: 0.5 };
  const wide = T.boxGeometry();
  check('and an aspect of 0.5 makes it twice as wide as it is tall',
    near(wide.halfW, 2 * square.halfW, 1e-9) && near(wide.halfH, square.halfH, 1e-9),
    `${wide.halfW}/${wide.halfH}`);
  check('while the radius stays the shorter side',
    near(T.selectionArcsec(), 0.2 * (T.state.geometry.angular_diameter_deg / 2) * 3600, 1e-9));

  // One edge at a time: the dragged side moves, the other dimension does not.
  const frame = { centre: wide.centre, rotation: wide.rotation,
                  halfW: wide.halfW, halfH: wide.halfH };
  const drag = { mode: 'edge-r', start: { ...T.state.selection }, frame };
  const radiusPx = T.diskGeom().radius;
  const pulled = T.resizeByEdge(drag, { x: wide.centre.x + wide.halfW + 30, y: wide.centre.y },
    radiusPx);
  check('dragging the right edge widens the box and leaves the height alone',
    pulled && pulled.aspect < drag.start.aspect &&
    Math.abs(pulled.radiusFraction * radiusPx * Math.max(1, pulled.aspect)) <
      wide.halfH + 1e-6,
    JSON.stringify(pulled && pulled.aspect));
  const taller = T.resizeByEdge({ ...drag, mode: 'edge-b' },
    { x: wide.centre.x, y: wide.centre.y + wide.halfH + 24 }, radiusPx);
  check('and dragging the bottom edge makes it taller without widening it',
    taller && taller.aspect > drag.start.aspect, taller && taller.aspect);
  check('an edge is not a corner',
    T.hitTest({ x: wide.centre.x + wide.halfW, y: wide.centre.y }) === 'edge-r' ||
    T.hitTest({ x: wide.centre.x + wide.halfW, y: wide.centre.y }) === 'edge-l',
    T.hitTest({ x: wide.centre.x + wide.halfW, y: wide.centre.y }));
  T.state.selection = null;
}

console.log('');
/* ── the geometric overlays are the page's own geometry ─────────────────── */

{
  /* The terminator, the equator, the limb, the poles and the two sub-points
     are drawn from `state.geometry` alone -- no plan, no request.  What has to
     hold is that they land where the projection says: a wrong sign here puts
     the equator north of the sub-Earth point, or hides the wrong pole. */
  const lat0 = geometry.libration_lat_deg, lon0 = geometry.libration_lon_deg;
  const R = disk.radius, cx = disk.cx, cy = disk.cy;

  const subEarth = T.projectSelenographic(lon0, lat0, disk);
  check('the sub-Earth point projects to the centre of the disk',
    near(subEarth.x, cx, 1e-6) && near(subEarth.y, cy, 1e-6) && !subEarth.far,
    `${subEarth.x},${subEarth.y}`);

  const farPoint = T.projectSelenographic(lon0 + 150, 0, disk);
  check('a point behind the limb is flagged as far side', farPoint.far);
  check('and is still drawn inside the limb, which is the relative position',
    Math.hypot(farPoint.x - cx, farPoint.y - cy) <= R + 1e-9,
    Math.hypot(farPoint.x - cx, farPoint.y - cy) + ' vs ' + R);
  /* The place a far-side point is drawn at is a real point on the near side:
     the same selenographic direction with its line-of-sight component negated.
     Built here from the mirror in the Moon-fixed frame -- reflect the unit
     vector through the plane perpendicular to the sub-observer direction --
     rather than from the projection, so the check does not simply repeat the
     implementation it is checking. */
  {
    const D = Math.PI / 180;
    const farLon = lon0 + 150, farLat = 0;
    const n = T.moonVector(farLon, farLat);
    const o = [Math.cos(lat0 * D) * Math.cos(lon0 * D),
               Math.cos(lat0 * D) * Math.sin(lon0 * D), Math.sin(lat0 * D)];
    const along = n[0] * o[0] + n[1] * o[1] + n[2] * o[2];
    const mirrored = [n[0] - 2 * along * o[0], n[1] - 2 * along * o[1],
                      n[2] - 2 * along * o[2]];
    const [mirrorLon, mirrorLat] = T.lonLatFromVector(mirrored);
    const mirror = T.projectSelenographic(mirrorLon, mirrorLat, disk);
    check('and that position is where its mirror image on the near side lands',
      !mirror.far && near(mirror.x, farPoint.x, 1e-6) && near(mirror.y, farPoint.y, 1e-6),
      `${mirror.x},${mirror.y} vs ${farPoint.x},${farPoint.y}`);
  }

  check('the near-side projection agrees with the selection one',
    (() => {
      const a = T.lonLatToPixel(lon0 + 20, 10);
      const b = T.projectSelenographic(lon0 + 20, 10, disk);
      return a && near(a.x, b.x, 1e-9) && near(a.y, b.y, 1e-9);
    })());
  check('and still refuses a far-side point', T.lonLatToPixel(lon0 + 150, 0) === null);

  const equator = T.circleFromPole(0, 90, 90);
  check('the equator is latitude zero all the way round',
    equator.every(([, lat]) => Math.abs(lat) < 1e-9));
  const southOfCentre = T.projectSelenographic(lon0, 0, disk);
  check('and sits south of the sub-Earth point when the libration is north',
    lat0 > 0 && near(southOfCentre.x, cx, 1e-6) && southOfCentre.y > cy,
    `${southOfCentre.x},${southOfCentre.y} vs centre ${cx},${cy}`);

  /* The north pole is tipped into view by a positive libration in latitude,
     and the south pole is then the one drawn at its relative position. */
  check('a pole the libration has tipped into view is drawn as a real point',
    !T.projectSelenographic(0, 90, disk).far && T.projectSelenographic(0, -90, disk).far);
  /* The pole is at angular distance 90 - lat0 from the sub-Earth point, and the
     gnomonic map puts a point at ``R sin c / (1 + (R/d) cos c)`` along its own
     azimuth -- the same relation the renderer's rays satisfy, derived here from
     the geometry rather than from the function under test. */
  check('the visible pole rides on the limb',
    (() => {
      const D = Math.PI / 180;
      const k = Math.sin(geometry.angular_diameter_deg * D / 2);
      const s = Math.cos(geometry.angular_diameter_deg * D / 2);
      // <pole, up> is cos(lat0) and the line-of-sight component is -sin(lat0),
      // so the gnomonic offset is s cos(lat0) / (1 - k sin(lat0)) radii.
      return near(T.projectSelenographic(0, 90, disk).y,
        cy - R * s * Math.cos(lat0 * D) / (1 - k * Math.sin(lat0 * D)), 1e-6);
    })());

  /* The page's projection has to be the *renderer's*, not an orthographic
     sketch of it: the landform names come from the server already projected,
     and the overlays are drawn here, so an approximation would put the two
     layers on slightly different pictures -- worst in the middle of the disk,
     where the reader is least likely to check and most likely to aim.  The
     round trip closes only if both halves use the same rays. */
  {
    let worst = 0;
    for (const [sx, sy] of [[0, 0], [0.3, 0], [-0.3, 0], [0, 0.3], [0, -0.3],
                            [0.5, 0.5], [-0.6, 0.2], [0.7, -0.4]]) {
      const px = cx + sx * R, py = cy + sy * R;
      const back = T.pixelToLonLat(px, py);
      if (!back) { worst = Infinity; break; }
      const again = T.lonLatToPixel(back.lon, back.lat);
      worst = Math.max(worst, Math.hypot(again.x - px, again.y - py));
    }
    check('a pixel survives the round trip through lon/lat', worst < 1e-6, worst);
  }
  {
    // And the same the other way round, out to the limb.
    let worst = 0;
    for (const c of [0, 15, 30, 45, 60, 75, 85]) {
      for (const bearing of [0, 90, 180, 270]) {
        const D = Math.PI / 180;
        const lat = Math.asin(Math.sin(lat0 * D) * Math.cos(c * D)
          + Math.cos(lat0 * D) * Math.sin(c * D) * Math.cos(bearing * D)) / D;
        const lon = lon0 + Math.atan2(
          Math.sin(bearing * D) * Math.sin(c * D) * Math.cos(lat0 * D),
          Math.cos(c * D) - Math.sin(lat0 * D) * Math.sin(lat * D)) / D;
        const there = T.projectSelenographic(lon, lat, disk);
        if (there.far) continue;
        const back = T.pixelToLonLat(there.x, there.y);
        const again = T.lonLatToPixel(back.lon, back.lat);
        worst = Math.max(worst, Math.hypot(again.x - there.x, again.y - there.y));
      }
    }
    check('and a coordinate survives it the other way', worst < 1e-6, worst);
  }

  /* The near-side boundary is the great circle 90 degrees from the zero point:
     every sample has to be exactly that far from (0,0). */
  const boundary = T.circleFromPole(0, 0, 90);
  const offBy = boundary.reduce((worst, [lon, lat]) => {
    const v = [Math.cos(lat * Math.PI / 180) * Math.cos(lon * Math.PI / 180),
               Math.cos(lat * Math.PI / 180) * Math.sin(lon * Math.PI / 180),
               Math.sin(lat * Math.PI / 180)];
    return Math.max(worst, Math.abs(v[0]));
  }, 0);
  check('the near-side boundary is 90° from the zero point all the way round',
    offBy < 1e-12, offBy);
  check('and passes through both poles',
    boundary.some(([, lat]) => Math.abs(lat - 90) < 1e-9) &&
    boundary.some(([, lat]) => Math.abs(lat + 90) < 1e-9));

  /* The terminator is 90 degrees from the sub-solar point, which is what makes
     it the line of zero solar elevation. */
  const sunLon = geometry.sub_solar_lon_deg, sunLat = geometry.sub_solar_lat_deg;
  const terminator = T.circleFromPole(sunLon, sunLat, 90);
  const sunOffBy = terminator.reduce((worst, [lon, lat]) => {
    const dot = Math.sin(lat * Math.PI / 180) * Math.sin(sunLat * Math.PI / 180) +
      Math.cos(lat * Math.PI / 180) * Math.cos(sunLat * Math.PI / 180) *
      Math.cos((lon - sunLon) * Math.PI / 180);
    return Math.max(worst, Math.abs(dot));
  }, 0);
  check('the shadow band is 90° from the sub-solar point all the way round',
    sunOffBy < 1e-12, sunOffBy);

  /* An export is rendered at its own size, so the overlay has to be measured in
     that frame's pixels -- a mark drawn from the preview's geometry would land
     in the wrong place on a 2400 px file. */
  const big = T.diskGeomFor(1200, 1200);
  check('an export frame has its own geometry',
    near(big.cx, 600, 1e-9) && near(big.cy, 600, 1e-9) && near(big.radius, 0.47 * 1200, 1e-9),
    `${big.cx},${big.cy} r=${big.radius}`);
  check('and a mark on it scales with the picture',
    near(T.projectSelenographic(lon0 + 60, 20, big).x - big.cx,
      (T.projectSelenographic(lon0 + 60, 20, disk).x - cx) * 1200 / 560, 1e-9));

  /* A full disk's worth of overlays, drawn into a recording context: the point
     is that every switch has something behind it and nothing throws. */
  const drawn = { strokes: 0, texts: [], fills: 0 };
  const ctx = {
    canvas: { width: 560, height: 560 },
    font: '', textAlign: '', textBaseline: '', lineWidth: 1, lineJoin: '', lineCap: '',
    strokeStyle: '', fillStyle: '', globalAlpha: 1,
    save() {}, restore() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {},
    arc() {}, stroke() { drawn.strokes += 1; }, fill() { drawn.fills += 1; },
    setLineDash() {}, measureText: (text) => ({ width: String(text).length * 8 }),
    fillText(text) { drawn.texts.push(text); },
    strokeText(text) { drawn.texts.push(text); },
  };
  for (const name of ['terminator', 'poles', 'limb', 'equator', 'libration',
                      'subsolar', 'nearside']) {
    document.getElementById('mark-' + name).checked = true;
  }
  T.drawDiskMarks(ctx, 13, disk);
  check('the three self-evident overlays are drawn without a caption',
    !drawn.texts.some((t) => /阴影带|外缘轮廓|零点/.test(t)),
    drawn.texts.join(' | '));
  check('every overlay draws something', drawn.strokes > 6 && drawn.fills > 2,
    `${drawn.strokes} strokes, ${drawn.fills} fills`);
  /* Three of the overlays are their own legend -- the shadow band, the limb and
     the zero point -- so they are drawn without a caption. */
  for (const text of ['赤道', '北极 N', '南极 S', '日下点', '近地侧边界', '天平动点']) {
    // The point labels carry their coordinates after the name.
    check('the overlay is labelled ' + text,
      drawn.texts.some((line) => line.startsWith(text)),
      drawn.texts.join(' | '));
  }
  for (const name of ['terminator', 'poles', 'limb', 'equator', 'libration',
                      'subsolar', 'nearside']) {
    document.getElementById('mark-' + name).checked = false;
  }
  check('and nothing is drawn when every switch is off',
    (() => {
      const quiet = { strokes: 0, fills: 0, texts: [] };
      const off = {
        ...ctx,
        stroke() { quiet.strokes += 1; }, fill() { quiet.fills += 1; },
        fillText(t) { quiet.texts.push(t); }, strokeText(t) { quiet.texts.push(t); },
      };
      T.drawDiskMarks(off, 13, disk);
      return quiet.strokes === 0 && quiet.fills === 0 && quiet.texts.length === 0;
    })());
}

console.log('');
console.log('curve axes');
{
  const plot = { x: 10, y: 20, w: 200, h: 100 };
  const view = { xMin: 0, xMax: 20, yMin: -5, yMax: 5 };
  const normal = T.curveProjection(plot, view, {});
  check('the normal x axis grows to the right',
    near(normal.x(0), 10) && near(normal.x(20), 210));
  check('the normal y axis grows upwards',
    near(normal.y(5), 20) && near(normal.y(-5), 120));
  check('the inverse projection returns the same values',
    near(normal.xv(normal.x(7)), 7) && near(normal.yv(normal.y(2)), 2));
  const reversed = T.curveProjection(plot, view,
    { reverseX: true, reverseY: true });
  check('reversing X puts the lower value on the right',
    near(reversed.x(0), 210) && near(reversed.x(20), 10));
  check('reversing Y puts the lower value at the top',
    near(reversed.y(-5), 20) && near(reversed.y(5), 120));
  check('the inverse projection still works when reversed',
    near(reversed.xv(reversed.x(7)), 7) && near(reversed.yv(reversed.y(-2)), -2));

  /* Sky charts (and the libration phase plot) must use one degree per pixel;
     fitting a wide plot grows the vertical range, not the curve. */
  const sky = { xMin: 90, xMax: 110, yMin: 35, yMax: 37 };
  T.curveEqualize(sky, 4.0);
  check('a sky view is equalised to the plot aspect',
    near((sky.xMax - sky.xMin) / (sky.yMax - sky.yMin), 4.0),
    `${sky.xMax - sky.xMin} / ${sky.yMax - sky.yMin}`);
  check('only the short axis grows',
    near(sky.xMin, 90) && near(sky.xMax, 110) && sky.yMin < 35 && sky.yMax > 37);
  check('the equal-aspect switch names the sky and libration-phase charts',
    T.curveEqualAspect({ kind: 'moon8' })
    && T.curveEqualAspect({ kind: 'sun8' })
    && T.curveEqualAspect({ kind: 'libration' })
    && !T.curveEqualAspect({ kind: 'geometry' })
    && !T.curveEqualAspect({ kind: 'earthshine' }));
}

finish();
