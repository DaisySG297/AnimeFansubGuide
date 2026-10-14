/**
 * page-check.js — static sanity pass over the panel's single-page front end.
 *
 *   node scripts/page-check.js [path/to/page.html]
 *   (default target: dist/anime-fansub-guide.html — the copy actually served)
 *
 * Checks
 *   1. every inline <script> block parses (new Function)
 *   2. identifiers belonging to REMOVED features must not reappear
 *   3. every id used by getElementById() exists in the markup
 *   4. view containers and view tabs agree
 *   5. FREE IDENTIFIERS — names called as functions that are never declared
 *      anywhere in the inline script. This is the check that catches the
 *      2026-10-14 class of bug: removing a view also removed shared globals
 *      (SEASON, esc, render) and the whole page died with ReferenceError.
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const target = process.argv[2] || path.join(ROOT, 'dist', 'anime-fansub-guide.html');
const html = fs.readFileSync(target, 'utf8');
console.log('checking: ' + target + '\n');

const problems = [];

/* ---------- 1) inline script blocks parse ---------- */
const blocks = [];
const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = re.exec(html)) !== null) blocks.push({ code: m[1], index: m.index });
console.log('inline <script> blocks: ' + blocks.length);

blocks.forEach((b, i) => {
  try { new Function(b.code); } catch (e) {
    const line = html.slice(0, b.index).split('\n').length;
    problems.push('syntax error in block #' + i + ' (starts line ' + line + '): ' + e.message);
  }
});
const js = blocks.map((b) => b.code).join('\n');
if (!problems.length) console.log('[ok] all inline scripts parse');

/* ---------- 2) removed-feature identifiers ---------- */
const DEAD = ['initLiveOnly', 'liveOnlyChip', 'TIER_ORDER', 'markTier', 'cmdText',
              'planOne', 'quickSub', 'GROUPS', 'seasonByGroup', 'liveBadge',
              'data-aq-quick'];
const deadHits = DEAD.filter((k) => new RegExp('(?<![\\w$.])' + k + '(?![\\w$])').test(js));
console.log(deadHits.length
  ? '[!!] removed identifiers referenced again: ' + deadHits.join(', ')
  : '[ok] no removed-feature identifiers');
if (deadHits.length) problems.push('removed identifiers referenced: ' + deadHits.join(', '));

/* ---------- 3) getElementById targets exist ---------- */
const ids = new Set();
let im;
const idRe = /id\s*=\s*["']([^"']+)["']/g;
while ((im = idRe.exec(html)) !== null) ids.add(im[1]);
const dyn = /id\s*=\\?["']([A-Za-z0-9_-]+)\\?["']/g;   // ids the JS builds in markup strings
while ((im = dyn.exec(js)) !== null) ids.add(im[1]);
const assign = /\.id\s*=\s*["']([A-Za-z0-9_-]+)["']/g; // node.id = "..." assignments
while ((im = assign.exec(js)) !== null) ids.add(im[1]);

const missing = new Set();
let gm;
const getRe = /getElementById\(\s*["']([^"']+)["']\s*\)/g;
while ((gm = getRe.exec(js)) !== null) if (!ids.has(gm[1])) missing.add(gm[1]);
console.log(missing.size
  ? '[!!] getElementById targets missing from markup: ' + [...missing].join(', ')
  : '[ok] every getElementById() target exists');
if (missing.size) problems.push('missing DOM ids: ' + [...missing].join(', '));

/* ---------- 4) view containers vs tabs ---------- */
const containers = [...html.matchAll(/class="[^"]*\bview\b[^"]*"[^>]*data-view="([^"]+)"/g)].map((x) => x[1]);
const tabs = [...html.matchAll(/class="[^"]*\bvt\b[^"]*"[^>]*data-view="([^"]+)"/g)].map((x) => x[1]);
const switched = [...js.matchAll(/__showView\(\s*["']([^"']+)["']/g)].map((x) => x[1])
  .concat([...js.matchAll(/data-view\s*===\s*["']([^"']+)["']/g)].map((x) => x[1]));
console.log('view containers: ' + (containers.join(', ') || '(none)'));
console.log('view tabs      : ' + (tabs.join(', ') || '(none)'));
const orphan = [...new Set(switched)].filter((v) => !containers.includes(v));
console.log(orphan.length ? '[!!] JS switches to unknown views: ' + orphan.join(', ')
  : '[ok] all switched-to views have containers');
if (orphan.length) problems.push('unknown view targets: ' + orphan.join(', '));

/* ---------- 5) free identifiers ---------- */
// Strip string / template literals, comments and regex literals so the scanner
// sees code only. Regex detection uses the "why would a / follow this token"
// heuristic: after a value-producing token it is division, otherwise a regex.
function strip(src) {
  let out = '';
  let i = 0;
  const n = src.length;
  let state = null; // 'line' | 'block' | 'sq' | 'dq' | 'tpl' | 're'
  let prev = '';
  while (i < n) {
    const c = src[i], d = src[i + 1];
    if (state === 'line') { if (c === '\n') { state = null; out += '\n'; prev = ''; } i++; continue; }
    if (state === 'block') { if (c === '*' && d === '/') { state = null; i += 2; } else i++; continue; }
    if (state === 'sq' || state === 'dq' || state === 'tpl') {
      const q = state === 'sq' ? "'" : state === 'dq' ? '"' : '`';
      if (c === '\\') { i += 2; continue; }
      if (c === q) { state = null; i++; prev = q; continue; }
      if (c === '\n' && state !== 'tpl') { state = null; out += '\n'; prev = ''; }
      i++; continue;
    }
    if (state === 're') {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') { state = null; i++; while (/[a-z]/i.test(src[i] || '')) i++; prev = '/'; continue; }
      if (c === '\n') { state = null; out += '\n'; prev = ''; continue; }
      i++; continue;
    }
    if (c === '/' && d === '/') { state = 'line'; i += 2; continue; }
    if (c === '/' && d === '*') { state = 'block'; i += 2; continue; }
    if (c === '"') { state = 'dq'; i++; continue; }
    if (c === "'") { state = 'sq'; i++; continue; }
    if (c === '`') { state = 'tpl'; i++; continue; }
    if (c === '/' && !/[\w$)\]]/.test(prev)) { state = 're'; i++; continue; }
    out += c;
    if (!/\s/.test(c)) prev = c;
    i++;
  }
  return out;
}
const code = strip(js);

const KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'new',
  'delete', 'void', 'in', 'of', 'do', 'else', 'try', 'finally', 'throw', 'case', 'break',
  'continue', 'instanceof', 'function', 'class', 'const', 'let', 'var', 'await', 'yield',
  'super', 'this', 'async', 'static', 'get', 'set', 'default', 'export', 'import', 'from',
  'as', 'with', 'debugger', 'extends', 'null', 'true', 'false', 'undefined', 'NaN']);

const BUILTINS = new Set(['console', 'document', 'window', 'globalThis', 'alert', 'confirm',
  'prompt', 'fetch', 'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval',
  'requestAnimationFrame', 'cancelAnimationFrame', 'encodeURIComponent', 'decodeURIComponent',
  'encodeURI', 'decodeURI', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'Number', 'String',
  'Boolean', 'Array', 'Object', 'Math', 'JSON', 'Date', 'RegExp', 'Error', 'TypeError',
  'RangeError', 'SyntaxError', 'Map', 'Set', 'WeakMap', 'WeakSet', 'Promise', 'Symbol', 'Proxy',
  'Reflect', 'Intl', 'BigInt', 'URL', 'URLSearchParams', 'FormData', 'Headers', 'Request',
  'Response', 'AbortController', 'EventSource', 'WebSocket', 'Blob', 'File', 'FileReader',
  'Image', 'Audio', 'Notification', 'localStorage', 'sessionStorage', 'history', 'location',
  'navigator', 'screen', 'performance', 'CustomEvent', 'Event', 'Node', 'Element', 'HTMLElement',
  'MutationObserver', 'ResizeObserver', 'IntersectionObserver', 'getComputedStyle', 'matchMedia',
  'structuredClone', 'queueMicrotask', 'atob', 'btoa', 'crypto', 'TextEncoder', 'TextDecoder',
  'XMLHttpRequest', 'Function', 'eval', 'getSelection']);

const declared = new Set();
let dm;
const declRes = [
  /function\s+([A-Za-z_$][\w$]*)/g,
  /(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g,
  /class\s+([A-Za-z_$][\w$]*)/g,
  /catch\s*\(\s*([A-Za-z_$][\w$]*)/g,
  /([A-Za-z_$][\w$]*)\s*=>/g,
];
for (const r of declRes) while ((dm = r.exec(code)) !== null) declared.add(dm[1]);

// function parameters, including nested destructuring lists
let pm;
const paramRe = /\(([^()]*)\)\s*(?:=>|\{)/g;
while ((pm = paramRe.exec(code)) !== null) {
  pm[1].split(',').forEach((p) => {
    const name = p.trim().split(/[=:]/)[0].trim().replace(/^\.\.\./, '').replace(/[{}[\]]/g, '');
    if (/^[A-Za-z_$][\w$]*$/.test(name)) declared.add(name);
  });
}

// calls: NAME(  not preceded by . or a quote (i.e. not a property access)
const called = new Map();
let cm;
const callRe = /(?<![\w$.'"`\\])([A-Za-z_$][\w$]*)\s*\(/g;
while ((cm = callRe.exec(code)) !== null) {
  const name = cm[1];
  if (KEYWORDS.has(name)) continue;
  called.set(name, (called.get(name) || 0) + 1);
}

const free = [...called.keys()].filter((n) => !declared.has(n) && !BUILTINS.has(n));

if (free.length) {
  console.log('\n[!!] called but never declared anywhere in the page:');
  free.sort((a, b) => called.get(b) - called.get(a)).forEach((n) =>
    console.log('       ' + n.padEnd(22) + called.get(n) + ' call(s)'));
  problems.push('free identifiers: ' + free.join(', '));
} else {
  console.log('[ok] no free identifiers (every called name is declared or built-in)');
}

/* ---------- 6) top-level calls must resolve to top-level declarations ----------
 * The free-identifier pass above accepts a name declared ANYWHERE, so a
 * column-0 call to a function that only exists inside an IIFE still slips
 * through (that is exactly how the stray `render();` survived). Top-level
 * calls can only see top-level (or built-in) names — check that. */
const topDecls = new Set();
const topCalls = [];
for (const ln of code.split('\n')) {
  let d;
  if ((d = ln.match(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/))) topDecls.add(d[1]);
  else if ((d = ln.match(/^(?:const|let|var)\s+([A-Za-z_$][\w$]*)/))) topDecls.add(d[1]);
  else if ((d = ln.match(/^class\s+([A-Za-z_$][\w$]*)/))) topDecls.add(d[1]);
  const c = ln.match(/^([A-Za-z_$][\w$]*)\s*\(/);
  if (c && !KEYWORDS.has(c[1])) topCalls.push(c[1]);
}
const strayTop = [...new Set(topCalls)].filter((n) => !topDecls.has(n) && !BUILTINS.has(n));
console.log(strayTop.length
  ? '[!!] top-level calls with no top-level declaration: ' + strayTop.join(', ')
  : '[ok] every top-level call resolves');
if (strayTop.length) problems.push('stray top-level calls: ' + strayTop.join(', '));

/* ---------- verdict ---------- */
console.log('\n' + (problems.length ? 'FAIL (' + problems.length + ')' : 'PASS - no structural problems'));
problems.forEach((p) => console.log('  - ' + p));
process.exit(problems.length ? 1 : 0);
