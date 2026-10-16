/* Shared plumbing for the three Node harnesses: test runner and app loader.
 *
 * Each harness used to define its own `check()`, its own pass/fail counters and
 * its own copy of the "append an export block, run app.js in a vm, read the
 * exports back" dance.  The counters agreed but the runners did not -- one
 * printed the detail for a falsy value, the other only for a truthy one, so the
 * same failing assertion produced different output depending on which file ran
 * it.  One runner, one output format.
 *
 *   node web/test_dom.js
 *   node web/test_page.js
 *   node web/test_projection.js
 *
 * Nothing here is part of the page: `static/` is what the browser loads, and
 * this file is only ever read by Node.
 */
'use strict';

const path = require('path');
const vm = require('vm');

const STATIC = path.join(__dirname, 'static');

/**
 * A test runner that prints as it goes and exits with the right status.
 *
 * `verbose` prints a line for every check; the default prints only a summary,
 * which is what the DOM harness wants for its long list of mechanical checks.
 */
function createRunner(options) {
  const opts = options || {};
  let passed = 0;
  let failed = 0;

  const check = (name, condition, detail) => {
    if (condition) {
      passed++;
      if (opts.verbose !== false) console.log('  ok   ' + name);
    } else {
      failed++;
      // A falsy detail is information too -- `0`, `''` and `false` are the
      // values most worth seeing when an assertion about numbers fails.
      const suffix = detail === undefined ? '' : '  ' + detail;
      console.log('  FAIL ' + name + suffix);
    }
  };

  const near = (a, b, tol) => Math.abs(a - b) <= (tol === undefined ? 1e-6 : tol);

  /** Print the summary and exit; `label` is a closing line, if wanted. */
  const finish = (label) => {
    if (label) console.log('\n' + label);
    console.log(passed + ' passed, ' + failed + ' failed');
    process.exit(failed ? 1 : 0);
  };

  return { check, near, finish, counts: () => ({ passed, failed }) };
}

/**
 * Load `static/app.js` in a sandbox and hand back the names it exports.
 *
 * `app.js` is written for a browser and declares everything at top level, so a
 * `vm` context sees its identifiers directly; the appended block gathers the
 * ones a harness needs into one object.  `names` is checked first, because a
 * typo in a harness's list would otherwise surface as a confusing failure much
 * later -- and listing a name twice would silently do nothing.
 */
function loadApp(sandbox, names, options) {
  const opts = options || {};
  const file = opts.file || path.join(STATIC, 'app.js');
  const fs = require('fs');

  const seen = new Set();
  for (const name of names) {
    if (seen.has(name)) {
      throw new Error(`harness export list names ${name} twice`);
    }
    seen.add(name);
  }

  const source = fs.readFileSync(file, 'utf8') +
    '\n;globalThis.__t = {' + names.join(', ') + '};';

  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: file });
  const exports = sandbox.__t;
  for (const name of names) {
    if (exports[name] === undefined) {
      // A name that exists but is legitimately undefined is indistinguishable
      // from a missing one here; that is acceptable, because every name on
      // every current list is a function or an object.
      throw new Error(`app.js does not define ${name}`);
    }
  }
  return exports;
}

/** Read one of the page's static files. */
function readStatic(name) {
  return require('fs').readFileSync(path.join(STATIC, name), 'utf8');
}

/** Evaluate an expression inside a sandbox created by :func:`loadApp`. */
function evalIn(sandbox, expression) {
  return vm.runInContext(expression, sandbox);
}

module.exports = { createRunner, loadApp, readStatic, evalIn, STATIC };
