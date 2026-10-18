/* Static check that the page's three files agree with each other.
 *
 * Cheap, and it catches the class of mistake that otherwise only shows up as a
 * control that silently does nothing: an id the script reaches for that the
 * markup does not define, an output span that no input drives, or a class with
 * no rule behind it.
 *
 *   node web/test_dom.js
 */
'use strict';
const { createRunner, readStatic } = require('./harness');

const html = readStatic('index.html');
const css = readStatic('style.css');
const jsRaw = readStatic('app.js');
/* Strip comments before looking for calls: prose mentions of a function name
   are not calls, and "acos(rho)" inside an explanatory comment is not code. */
const js = jsRaw.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');

/* Every way the script names an element: $('x'), num('x'), busy('x'), and the
   string arrays that drive the sliders. */
const referenced = new Set();
for (const re of [/\$\('([^']+)'\)/g, /\bnum\('([^']+)'\)/g, /\bbusy\('([^']+)'/g]) {
  for (const m of js.matchAll(re)) referenced.add(m[1]);
}
for (const m of js.matchAll(/\[\s*'([a-z-]+)',\s*\d\]/g)) {
  referenced.add(m[1]);
  referenced.add(m[1] + '-out');
}
for (const m of js.matchAll(/'([a-z-]+-out)'/g)) referenced.add(m[1]);

/* This file's checks are mechanical and numerous, so it prints only failures
   and the summary; the other two harnesses print every check. */
const { check, finish } = createRunner({ verbose: false });

/* First, is this even the file it claims to be?  A misplaced write once left
   the JavaScript sitting in index.html, and every id check then failed for a
   reason that had nothing to do with ids. */
check('index.html really is HTML',
  html.trimStart().startsWith('<!DOCTYPE html>') && html.trimEnd().endsWith('</html>'));
check('style.css really is CSS', css.includes('{') && !css.includes('function '));
check('app.js really is JavaScript', !jsRaw.trimStart().startsWith('<!DOCTYPE'));
check('the page loads the script it was written against',
  html.includes('<script src="/static/app.js">'));

const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

/* The wiring lists -- `for (const id of ['a', 'b']) { $(id)... }` -- reach for
   their elements through a variable, so the check above cannot see them.  One
   of those lists kept naming `earthshine` after its checkbox was removed, and
   the `null.addEventListener` that followed stopped `init()` before the first
   render.  Same class of mistake, same check. */
{
  const listed = new Set();
  for (const m of js.matchAll(/for \(const id of \[([^\]]+)\]\)/g)) {
    for (const name of m[1].matchAll(/'([a-z][\w-]*)'/g)) listed.add(name[1]);
  }
  const unknown = [...listed].filter((id) => !htmlIds.has(id));
  check('every id in a wiring list exists in the markup',
    unknown.length === 0, unknown.join(', '));
}

const missing = [...referenced].filter((id) => !htmlIds.has(id));
check('every id the script uses exists in the markup', missing.length === 0, missing.join(', '));

/* And no id is defined twice: `getElementById` would hand back the first one
   and the second copy would be a control that silently does nothing.  A
   duplicated light control (日面采样 / 粗糙度 / 环境补光, left behind when
   they moved into 高级设置) is exactly how this was found. */
const idCounts = new Map();
for (const m of html.matchAll(/\bid="([^"]+)"/g)) {
  idCounts.set(m[1], (idCounts.get(m[1]) || 0) + 1);
}
const duplicated = [...idCounts].filter(([, count]) => count > 1).map(([id]) => id);
check('no id appears twice in the markup', duplicated.length === 0, duplicated.join(', '));

/* Each range input that has an output must have a matching *-out span, and the
   other way round, or a slider would show a number that never updates. */
const ranges = [...html.matchAll(/<input[^>]*type="range"[^>]*id="([^"]+)"/g)].map((m) => m[1]);
const orphanRanges = ranges.filter((id) => !htmlIds.has(id + '-out') && !js.includes(`'${id}'`));
check('every slider is wired', orphanRanges.length === 0, orphanRanges.join(', '));

/* Only real <output> elements, not any id that happens to end in "-out". */
const outputs = [...html.matchAll(/<output[^>]*\bid="([^"]+)"/g)].map((m) => m[1]);
const orphanOutputs = outputs.filter((id) => !js.includes(`'${id.slice(0, -4)}'`));
check('every <output> is driven by an input', orphanOutputs.length === 0, orphanOutputs.join(', '));

/* Classes with no rule are dead weight, unless they are pure JS hooks. */
const jsHooks = new Set(['on', 'busy', 'status']);
const htmlClasses = new Set(
  [...html.matchAll(/\bclass="([^"]+)"/g)].flatMap((m) => m[1].split(/\s+/)).filter(Boolean)
);
const cssClasses = new Set([...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1]));
const styled = new Set([...cssClasses].filter((c) => htmlClasses.has(c)));
const unstyled = [...htmlClasses].filter((c) => !styled.has(c) && !jsHooks.has(c));
check('every class has a rule', unstyled.length === 0, unstyled.join(', '));

/* The other direction: a rule for a class nothing ever wears.
 *
 * This is the half that was missing, and it is the half that rots.  A class the
 * markup dropped looks exactly like a class the script adds at run time, so the
 * test that a class name is *somewhere* is too weak; what makes a rule dead is
 * that no element can have the class.  The script's string literals are the
 * closest cheap approximation of "what it can put on an element": a class it
 * builds with string concatenation will still appear as a fragment there.
 *
 * Two rules were dead when this was added -- `.actions .spacer`, and a whole
 * `.actions` block whose live sibling is `.col-actions` -- and neither was
 * visible without it. */
const jsStrings = [...js.matchAll(/'([^'\n]*)'|"([^"\n]*)"/g)]
  .map((m) => (m[1] === undefined ? m[2] : m[1])).join(' ');
const jsMentions = new Set(jsStrings.match(/[A-Za-z][\w-]*/g) || []);
const deadRules = [...cssClasses]
  .filter((c) => !htmlClasses.has(c) && !jsMentions.has(c) && !jsHooks.has(c))
  .sort();
check('no rule for a class nothing wears', deadRules.length === 0, deadRules.join(', '));

/* The script must not reference a helper it never defines. */
const defined = new Set([
  ...[...js.matchAll(/function\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  ...[...js.matchAll(/const\s+([A-Za-z_$][\w$]*)\s*=/g)].map((m) => m[1]),
  ...[...js.matchAll(/let\s+([A-Za-z_$][\w$]*)\s*=/g)].map((m) => m[1]),
]);
const called = new Set([...js.matchAll(/\b([a-z][\w$]*)\s*\(/g)].map((m) => m[1]));
/* Arrow-function parameters look like calls to nothing. */
for (const m of js.matchAll(/\(([^)]*)\)\s*=>/g)) {
  for (const part of m[1].split(',')) {
    // Nested parentheses leave a leading '(' on the first parameter.
    const name = part.trim().replace(/^[^A-Za-z_$]+/, '').replace(/[^\w$].*$/, '');
    if (/^[a-z][\w$]*$/.test(name)) defined.add(name);
  }
}
const builtins = new Set([
  'if', 'for', 'while', 'switch', 'catch', 'return', 'function', 'typeof', 'fetch',
  'Number', 'String', 'Boolean', 'Math', 'JSON', 'Date', 'Object', 'Array', 'Set',
  'Map', 'Promise', 'require', 'parseInt', 'parseFloat', 'isNaN', 'setTimeout',
  'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  'console', 'Image', 'Uint8Array',
  // CSS functions appear as `name(...)` inside strings.
  'rgba', 'rgb', 'hsla', 'hsl', 'blur',
  // Browser globals the harness has no stub for.
  'getComputedStyle', 'ResizeObserver', 'requestAnimationFrame',
  // Browser globals the save path uses; `callback` is a parameter name the
  // regex reads as a call because it is invoked.
  'atob', 'btoa', 'decodeURIComponent', 'encodeURIComponent', 'Blob',
  'URL', 'DataView', 'ArrayBuffer', 'Uint8Array', 'callback',
]);
const undefinedCalls = [...called].filter(
  (name) => !defined.has(name) && !builtins.has(name) && !js.includes(`.${name}(`)
);
check('no call to an undefined local function', undefinedCalls.length === 0, undefinedCalls.join(', '));

/* Both canvases the script grabs must exist. */
check('both canvases present', htmlIds.has('disk') && htmlIds.has('result'));
check('a download link exists', htmlIds.has('download'));

/* The processing controls must go to /api/process, which works on the server's
   cached frame; only the geometry controls may trigger a re-render. */
const processEditors = ['blur-strength', 'sharpen-radius', 'sharpen-strength',
                        'saturation', 'gain', 'offset', 'gamma', 'seeing',
                        'seeing-num'];
const reprocessIds = (js.match(/reprocess/g) || []).length;
check('sliders are routed through reprocess()', reprocessIds >= 3, reprocessIds + ' mentions');
check('/api/process is used', js.includes("'/api/process'"));
for (const id of processEditors) {
  if (!htmlIds.has(id)) check('processing control ' + id + ' exists', false);
}
check('every processing control exists', processEditors.every((i) => htmlIds.has(i)));
check('the selection has its own overlay canvas', htmlIds.has('overlay'));
// 彩色 beside 饱和度 is gone: it was a second copy of the camera's own 类型
// menu, and next to a slider it read as that slider's switch.
check('the colour switch is gone rather than moved',
  !htmlIds.has('colour') && !htmlIds.has('frame-gain'));
check('chips were removed', !html.includes('class="chip'));

/* Names the reader asked for by name, and that a tidy-up would rename by
   accident: the third tab and its button are both 生成动画, the HDR switch is
   "HDR" rather than "带 HDR", and the time nudges are labelled -1M/-1m/+1d. */
check('the animation pane and its button are both 生成动画',
  html.includes('data-proc="animation" aria-pressed="false">生成动画<') &&
  html.includes('id="anim-run">生成动画 '));
/* 时间步长 offers a ladder of common steps and still accepts any number: a
   datalist is the only native control that is both. */
check('时间步长 is a typable field with a list of common steps',
  /id="anim-step"[^>]*list="anim-step-presets"/.test(html) &&
  ['1', '5', '10', '15', '30', '60']
    .every((value) => new RegExp(`<option value="${value}"></option>`).test(html)));
/* 地貌搜索 is an always-visible entry point now: it sits in the label bar, and
   the geometric marks take its old place four to a row. */
check('the landform search lives in the always-visible label bar',
  /<div class="label-bar"[\s\S]*?id="feature-search"[\s\S]*?<div class="annot-detail"/
    .test(html));
check('the geometric marks lay out four to a row',
  /\.disk-head \.mark-bar\s*\{[^}]*grid-template-columns:\s*repeat\(4/
    .test(css));
/* 锐化半径 is an unsharp mask, and past 4 px it stops sharpening and starts
   redrawing large-scale shading; the slider's own range says so. */
check('锐化半径 stops at 4 px',
  /id="sharpen-radius"[^>]*min="0"[^>]*max="4"/.test(html));
/* 单帧预览 is a smaller button to the right of 终止: it answers "what does the
   first frame look like" without waiting for the whole run. */
check('单帧预览 comes right after 终止',
  /id="anim-cancel">终止<\/button>[\s\S]{0,600}?id="anim-preview"/.test(html) &&
  /class="ghost mini"\s+id="anim-preview"/.test(html));
/* 数据源 is the one control in 高级设置 that decides what the rest can do, so it
   sits above the knobs rather than beside them. */
check('数据源 is the first field in the advanced block',
  /<summary>高级设置[\s\S]{0,900}?id="data-source"/.test(html) &&
  /id="data-source-note"/.test(html));
/* 下载 is two characters, not a paragraph: the button keeps its own width and
   its text on one line, and the product menu beside it gives ground instead. */
check('区域高清补片的下载 button cannot be squeezed into a vertical label',
  /#patch-download\s*\{[^}]*flex:\s*0 0 auto/.test(css) &&
  /#patch-download\s*\{[^}]*white-space:\s*nowrap/.test(css) &&
  /#patch-product\s*\{[^}]*flex:\s*1 1 auto/.test(css));
/* 并行数: a real per-render ceiling, not a label.  It is the same kind of
   number as 每像素采样, so it shares that grid row instead of getting a
   full-width row whose explanatory paragraph cost another line. */
check('the advanced block carries a CPU worker control',
  htmlIds.has('parallel') && /id="parallel"[^>]*min="1"/.test(html));
check('并行数 shares the 每像素采样 row',
  (() => {
    const from = html.indexOf('id="out-samples"');
    const to = html.indexOf('id="parallel"');
    const between = from >= 0 && to > from ? html.slice(from, to) : '';
    return between.length > 0 && !between.includes('</div>')
      && !between.includes('wide-field');
  })());
check('the 并行数 explanation is a tooltip, not a paragraph',
  /<label title="[^"]*渲染按横带分给多个线程[^"]*">\s*并行数/
    .test(html) &&
  !html.includes('<p class="hint">渲染按横带分给多个线程'));
/* The sky window is a real canvas with a close button in its own header. */
check('the sky window has a canvas, a close button and its own save row',
  htmlIds.has('sky-canvas') && htmlIds.has('sky-close') &&
  htmlIds.has('sky-save') && htmlIds.has('sky-label-step') &&
  /id="sky-open"[^>]*>模拟天空位置</.test(html));
/* 饱和度 may now be pushed above 1; the cap is 3 in the markup, and the
   library's own finish_image() was extended to honour it. */
check('saturation can be boosted above 1',
  /id="saturation"[^>]*max="3"/.test(html));
/* The reset glyph became the word Reset.  It has to stay within the height the
   13 px icon needed: same line box, smaller type. */
check('reset buttons are the word Reset, set small',
  /button\.reset\s*\{[^}]*font-size:\s*9px/.test(css) &&
  /button\.reset\s*\{[^}]*line-height:\s*17px/.test(css) &&
  !/button\.reset\s*\{[^}]*width:\s*100%/.test(css));

check('the HDR switches are labelled HDR, not 带 HDR',
  !html.includes('带 HDR') && (html.match(/<span>HDR<\/span>/g) || []).length === 2);
check('every nudge button is labelled with its own step',
  (() => {
    const nudges = [...html.matchAll(/data-nudge="([^"]+)"[\s\S]{0,160}?>([^<\n]*)</g)]
      .map((m) => [m[1], m[2].trim()]);
    // 8 for the observer's instant, 16 for the animation (a row under 开始 *and*
    // one under 结束) and 8 inside the sky window, which walks its own instant.
    // 事件预报 has none any more -- its two ends are typed, and a whole row of
    // decade/month steps above them was eight buttons to move a window nobody
    // moves that way.
    return nudges.length === 32 && nudges.every(([step, text]) => step === text);
  })());
check('and every range nudge says which range it moves',
  (() => {
    const rows = [...html.matchAll(/data-nudge="[^"]*"[\s\S]{0,160}?data-range="([\w-]+)"/g)]
      .map((m) => m[1]);
    const counts = rows.reduce((all, one) => {
      all[one] = (all[one] || 0) + 1;
      return all;
    }, {});
    // The animation's two rows are *not* one range: the row under 开始 moves
    // the start and the row under 结束 moves the end.
    return (!counts.events && counts['anim-start'] === 8
            && counts['anim-end'] === 8 && counts['sky'] === 8);
  })());
/* Two rows of nudges, each with the steps that row is about: the observer's
   instant (w/d/h/m) and a range in the animation (d/h/15m/m). */
check('and each row offers the steps it was asked for',
  ['-1w', '-1d', '-1h', '-1m', '+1m', '+1h', '+1d', '+1w']
    .every((step) => html.includes(`data-nudge="${step}"`)) &&
  ['-15m', '+15m'].every((step) => html.includes(`data-nudge="${step}"`)));

/* Every field the readout reaches for must be optional-safe: the two endpoints
   do not have to carry identical payloads, and a missing one must degrade the
   caption rather than throw. */
{
  const body = js.slice(js.indexOf('function describeResult('));
  const fn = body.slice(0, body.indexOf('\n}\n') + 3);
  const optional = [...fn.matchAll(/\bif\s*\(\s*(\w+)\s*\)/g)].map((m) => m[1]);
  check('describeResult guards its optional fields',
    optional.includes('tr') && optional.includes('r'), optional.join(','));
  check('and guards the image shape',
    fn.includes('info.image && info.image.shape'));
}

/* The build number doubles as the way to the repository, so the markup has to
 * carry the link: the script only fills in the text. */
{
  const anchor = html.match(/<a\b[^>]*id="app-version"[^>]*>/);
  check('the version readout is a link to the repository',
    !!anchor && anchor[0].includes('github.com/10deg/tsukikage'),
    anchor ? anchor[0] : 'no <a id="app-version">');
  check('and it opens in a new tab without handing over the opener',
    !!anchor && anchor[0].includes('target="_blank"')
      && anchor[0].includes('rel="noopener"'));
}

/* The phase name is the easter egg's handle: it needs the rule that says it is
 * clickable, and the overlay needs to stay out of the way of the pointer. */
{
  const fall = css.slice(css.indexOf('.moonfall'));
  check('#phase-name is marked clickable', /#phase-name\s*\{[^}]*cursor:\s*pointer/.test(css));
  check('the snow does not eat the clicks underneath',
    /\.moonfall\s*\{[^}]*pointer-events:\s*none/.test(css));
  check('and it is taken out of the flow', fall.includes('position: fixed'));
}

/* Three layers float over every picture -- the landform names, the selection
 * box, and the veil a render puts up -- and the order between them is the whole
 * of their interaction.  A name drawn over the box hid the handle the reader
 * was dragging, which is what these checks are here to keep fixed. */
const layerZ = (selector) => {
  for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    // The selector text of a rule runs over the lines above its brace; the last
    // line is the one that names it.  An exact match, because `.busy` is also
    // the last word of `.status-line.busy`, which is a different rule.
    const names = rule[1].split(',').map((part) => part.trim().split('\n').pop().trim());
    if (!names.includes(selector)) continue;
    const found = rule[2].match(/z-index:\s*(-?\d+)/);
    if (found) return Number(found[1]);
  }
  return null;
};
{
  const labels = layerZ('.view-body > canvas.labels');
  const overlay = layerZ('.view-body > #overlay');
  const veil = layerZ('.busy');
  check('the selection box is drawn above the landform names',
    labels !== null && overlay !== null && overlay > labels,
    `overlay ${overlay} vs labels ${labels}`);
  check('and the render veil covers both',
    veil !== null && veil > overlay, `veil ${veil} vs overlay ${overlay}`);
  check("while the picture's own controls stay on top of the veil",
    layerZ('.view-tools') >= veil);
}

finish('all checks passed');
