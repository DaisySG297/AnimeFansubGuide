/**
 * diagnose-live.js — load the REAL live page in headless Chrome and capture
 * console output + uncaught JS errors + a screenshot.
 *
 * Unlike the other probe-*.js scripts this does NOT stub anything or rebuild
 * the page from local files: it points Chrome at http://127.0.0.1:7788/ and
 * injects a collector through a temporary wrapper served from the real origin
 * path, so every relative <script src> and fetch() resolves as it does for the
 * user.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const CACHE = path.join(ROOT, '.cache');
if (!fs.existsSync(CACHE)) fs.mkdirSync(CACHE, { recursive: true });

// Locate Chrome: honour CHROME_PATH, else try the usual Windows install spots.
const CHROME = process.env.CHROME_PATH || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA,
    'Google/Chrome/Application/chrome.exe'),
  '/usr/bin/google-chrome',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean).find((p) => fs.existsSync(p));
if (!CHROME) {
  console.error('Chrome not found. Set CHROME_PATH to your chrome executable.');
  process.exit(1);
}
const LIVE_URL = process.argv[2] || 'http://127.0.0.1:7788/';
const W = 1512, H = 950;

// Collector: runs before everything else, records errors and late-firing
// exceptions, then flushes a report into <title> so --dump-dom can read it.
const COLLECTOR = `
<script>
window.__DIAG__ = { errs: [], logs: [], loaded: [] };
window.addEventListener('error', function (e) {
  var t = e && e.target;
  if (t && (t.tagName === 'SCRIPT' || t.tagName === 'LINK' || t.tagName === 'IMG')) {
    window.__DIAG__.errs.push('RESOURCE FAIL: <' + t.tagName.toLowerCase() +
      '> ' + (t.src || t.href));
  } else {
    window.__DIAG__.errs.push('JS ERROR: ' + (e.message || '') +
      (e.filename ? ' @ ' + e.filename.split('/').pop() + ':' + e.lineno : ''));
  }
}, true);
window.addEventListener('unhandledrejection', function (e) {
  window.__DIAG__.errs.push('REJECTION: ' + (e.reason && (e.reason.message || e.reason)));
});
(function () {
  var _err = console.error, _warn = console.warn;
  console.error = function () { window.__DIAG__.logs.push('console.error: ' +
    Array.prototype.join.call(arguments, ' ')); return _err.apply(console, arguments); };
  console.warn = function () { window.__DIAG__.logs.push('console.warn: ' +
    Array.prototype.join.call(arguments, ' ')); return _warn.apply(console, arguments); };
})();
</script>
`;

const REPORT = `
<script>
(function () {
  var views = ['push', 'live', 'audit', 'rem', 'old'];
  var d = window.__DIAG__;
  var out = { views: {} };
  var i = 0;
  function snap(v) {
    try {
      var el = document.querySelector('.view[data-view="' + v + '"]');
      out.views[v] = {
        present: !!el,
        visible: !!(el && !el.classList.contains('hidden')),
        children: el ? el.children.length : -1,
        text: el ? (el.innerText || '').replace(/\s+/g, ' ').trim().length : -1,
        head: el ? (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60) : ''
      };
    } catch (e) { out.views[v] = { error: e.message }; }
  }
  function step() {
    if (i >= views.length) { flush(); return; }
    var v = views[i++];
    try {
      var btn = document.querySelector('.vt[data-view="' + v + '"]');
      if (btn) { btn.click(); } else { d.errs.push('NO TAB: ' + v); }
    } catch (e) { d.errs.push('switch ' + v + ': ' + e.message); }
    setTimeout(function () { snap(v); step(); }, 700);
  }
  function flush() {
    var t = document.createElement('title');
    t.textContent = '__DIAGREPORT__' + JSON.stringify({
      errs: d.errs, logs: d.logs.slice(0, 15), views: out.views,
      seasonLoaded: typeof window.SEASON_DATA !== 'undefined'
    }) + '__END__';
    document.head.appendChild(t);
  }
  setTimeout(step, 1500);
})();
</script>
`;

// Fetch the live HTML over HTTP, splice the collector in, and write a wrapper
// file that Chrome loads from a file:// URL with a <base> pointing back at the
// real origin so relative resources still resolve.
const http = require('http');
function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let b = '';
      res.setEncoding('utf8');
      res.on('data', (c) => (b += c));
      res.on('end', () => resolve(b));
    }).on('error', reject);
  });
}

(async () => {
  const html = await get(LIVE_URL);
  console.log('fetched live page bytes:', html.length);

  // record every external script the page asks for
  const srcs = [...html.matchAll(/<script[^>]+src=["']([^"']+)["']/g)].map((m) => m[1]);
  console.log('external scripts in page:', srcs.join(', ') || '(none)');

  for (const s of srcs) {
    if (/^https?:/.test(s)) continue;
    try {
      const body = await get(new globalThis.URL(s, LIVE_URL).href);
      console.log('  ' + s + ' -> ' + body.length + ' bytes  OK');
    } catch (e) {
      console.log('  ' + s + ' -> FAILED (' + e.message + ')');
    }
  }

  let out = html
    .replace(/<head([^>]*)>/i, '<head$1><base href="' + LIVE_URL + '">' + COLLECTOR)
    .replace(/<\/body>/i, REPORT + '\n</body>');
  const f = path.join(CACHE, 'diag-live.html');
  fs.writeFileSync(f, out, 'utf8');

  const args = [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--window-size=' + W + ',' + H, '--virtual-time-budget=15000',
    '--no-first-run', '--disable-extensions',
    '--user-data-dir=' + path.join(os.tmpdir(), 'afg-diag-live'),
  ];
  const fileUrl = 'file:///' + f.replace(/\\/g, '/');

  try {
    execFileSync(CHROME, args.concat([
      '--screenshot=' + path.join(CACHE, 'diag-live.png'), fileUrl]),
      { stdio: 'ignore', timeout: 90000 });
    console.log('screenshot: ' + path.join(CACHE, 'diag-live.png'));
  } catch (e) { console.log('screenshot failed:', e.message); }

  let dom = '';
  try {
    dom = execFileSync(CHROME, args.concat(['--dump-dom', fileUrl]),
      { encoding: 'utf8', maxBuffer: 1 << 28, stdio: ['ignore', 'pipe', 'ignore'], timeout: 90000 });
  } catch (e) { dom = String(e.stdout || ''); }

  const m = dom.match(/__DIAGREPORT__([\s\S]*?)__END__/);
  if (!m) { console.log('NO DIAG RESULT'); return; }
  const j = JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>'));
  console.log('\n=== errors ===');
  console.log(j.errs.length ? j.errs.join('\n') : '(none)');
  console.log('\n=== console.error / warn ===');
  console.log(j.logs.length ? j.logs.join('\n') : '(none)');
  console.log('\n=== season-data.js loaded: ' + j.seasonLoaded + ' ===');
  console.log('\n=== per-view render ===');
  const order = ['push', 'live', 'audit', 'rem', 'old'];
  for (const v of order) {
    const r = (j.views || {})[v] || {};
    console.log('  ' + v.padEnd(6) +
      ' children=' + String(r.children).padStart(3) +
      ' text=' + String(r.text).padStart(6) +
      (r.visible ? ' [visible]' : ' [hidden]') +
      (r.head ? '  "' + r.head + '"' : '') +
      (r.error ? '  ERR:' + r.error : ''));
  }
  console.log('\nverdict: ' + (j.errs.length === 0 ? 'PASS — no JS errors'
    : 'FAIL — ' + j.errs.length + ' error(s)'));
})();
