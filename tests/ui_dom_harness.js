#!/usr/bin/env node
/**
 * Ghost-DOM harness for the REAL FinOps Dashboard dashboard scripts.
 *
 * Previously the UI "state is preserved on error" property was checked against a hand-written copy of
 * `isNonStateErrorPayload` / `renderState` living inside the test harness, so it could (and did) drift
 * from finops_dashboard/ui/template.py. This harness instead:
 *   1. loads the HTML produced by `render_dashboard_html()`,
 *   2. runs every inline <script> (theme pre-paint, dashboard_enhancements.js, the main dashboard script)
 *      inside a `vm` context backed by a minimal dependency-free DOM stub,
 *   3. drives the real `renderState`, `isNonStateErrorPayload` and `triggerApi` functions with error
 *      payloads, malformed states and stubbed `fetch` responses,
 *   4. asserts that `currentState` and the rendered DOM text are preserved (or rolled back + repainted),
 *      and that the shared classifier produced the expected VL-* banner code.
 *
 * Usage: node tests/ui_dom_harness.js <rendered.html> <good_state.json> [iterations]
 * Prints a single JSON report line and exits non-zero on any failed check.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');

const [, , htmlPath, statePath, itersArg] = process.argv;
if (!htmlPath || !statePath) {
  process.stderr.write('usage: ui_dom_harness.js <rendered.html> <good_state.json> [iterations]\n');
  process.exit(2);
}
const ITERATIONS = Math.max(1, parseInt(itersArg || '1', 10) || 1);
const html = fs.readFileSync(htmlPath, 'utf8');
const goodStateJson = fs.readFileSync(statePath, 'utf8');

// ---------------------------------------------------------------------------
// Ghost DOM: just enough browser surface for the dashboard scripts to load and render.
// ---------------------------------------------------------------------------
const noop = function () {};
const elementsById = new Map();
const consoleWarnings = [];

function makeClassList() {
  const set = new Set();
  return {
    add: function () { Array.prototype.forEach.call(arguments, function (c) { set.add(String(c)); }); },
    remove: function () { Array.prototype.forEach.call(arguments, function (c) { set.delete(String(c)); }); },
    toggle: function (c, force) {
      const has = set.has(c);
      const want = force === undefined ? !has : !!force;
      if (want) set.add(c); else set.delete(c);
      return want;
    },
    contains: function (c) { return set.has(c); },
    replace: function (a, b) { if (set.has(a)) { set.delete(a); set.add(b); } },
    get length() { return set.size; },
    toString: function () { return Array.from(set).join(' '); },
    forEach: function (fn) { set.forEach(fn); },
    _set: set,
  };
}

function queryOne(sel) {
  const m = /^#([\w-]+)$/.exec(String(sel || '').trim());
  if (m) return getById(m[1]);
  return makeEl('div');
}

// Real constructors so `child instanceof Node` / `instanceof HTMLElement` behave like a browser.
function GhostNode() {}
GhostNode.ELEMENT_NODE = 1; GhostNode.TEXT_NODE = 3; GhostNode.DOCUMENT_NODE = 9; GhostNode.DOCUMENT_FRAGMENT_NODE = 11;
function GhostElement() {}
GhostElement.prototype = Object.create(GhostNode.prototype);
GhostElement.prototype.constructor = GhostElement;

function makeTextNode(text) {
  const t = Object.create(GhostNode.prototype);
  t.nodeType = 3; t.textContent = String(text); t.data = String(text); t.nodeValue = String(text);
  t.parentNode = null; t.parentElement = null;
  return t;
}

function makeEl(tag, id) {
  const el = Object.create(GhostElement.prototype);
  Object.assign(el, {
    nodeType: 1,
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    textContent: '',
    innerHTML: '',
    innerText: '',
    value: '',
    className: '',
    hidden: false,
    disabled: false,
    checked: false,
    open: false,
    title: '',
    href: '',
    src: '',
    type: '',
    name: '',
    placeholder: '',
    dataset: {},
    style: { setProperty: noop, removeProperty: noop, getPropertyValue: function () { return ''; } },
    children: [],
    childNodes: [],
    options: [],
    rows: [],
    cells: [],
    tBodies: [],
    files: [],
    attributes: {},
    parentNode: null,
    parentElement: null,
    firstChild: null,
    lastChild: null,
    nextSibling: null,
    previousSibling: null,
    nextElementSibling: null,
    previousElementSibling: null,
    firstElementChild: null,
    lastElementChild: null,
    offsetWidth: 800, offsetHeight: 400, clientWidth: 800, clientHeight: 400,
    scrollWidth: 800, scrollHeight: 400, scrollTop: 0, scrollLeft: 0, offsetTop: 0, offsetLeft: 0,
    selectedIndex: 0, selectionStart: 0, selectionEnd: 0,
    isConnected: true,
  });
  el.classList = makeClassList();
  el.addEventListener = noop;
  el.removeEventListener = noop;
  el.dispatchEvent = function () { return true; };
  el.appendChild = function (c) {
    el.children.push(c);
    el.childNodes.push(c);
    if (c && typeof c === 'object') { c.parentNode = el; c.parentElement = el; }
    el.firstChild = el.childNodes[0] || null;
    el.lastChild = c;
    el.firstElementChild = el.children[0] || null;
    el.lastElementChild = c;
    return c;
  };
  el.append = function () { Array.prototype.forEach.call(arguments, function (c) { el.appendChild(c); }); };
  el.prepend = function () { Array.prototype.forEach.call(arguments, function (c) { el.children.unshift(c); el.childNodes.unshift(c); }); };
  el.insertBefore = function (c) { return el.appendChild(c); };
  el.replaceChildren = function () {
    el.children = []; el.childNodes = [];
    el.firstChild = null; el.lastChild = null; el.firstElementChild = null; el.lastElementChild = null;
    Array.prototype.forEach.call(arguments, function (c) { el.appendChild(c); });
  };
  el.removeChild = function (c) {
    el.children = el.children.filter(function (x) { return x !== c; });
    el.childNodes = el.childNodes.filter(function (x) { return x !== c; });
    el.firstChild = el.childNodes[0] || null;
    el.lastChild = el.childNodes[el.childNodes.length - 1] || null;
    el.firstElementChild = el.children[0] || null;
    el.lastElementChild = el.children[el.children.length - 1] || null;
    return c;
  };
  el.remove = noop; el.replaceWith = noop;
  el.insertAdjacentElement = noop; el.insertAdjacentHTML = noop; el.insertAdjacentText = noop;
  el.querySelector = queryOne;
  el.querySelectorAll = function () { return []; };
  el.getElementsByTagName = function () { return []; };
  el.getElementsByClassName = function () { return []; };
  el.closest = function () { return null; };
  el.matches = function () { return false; };
  el.contains = function () { return false; };
  el.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(el.attributes, k) ? el.attributes[k] : null; };
  el.setAttribute = function (k, v) { el.attributes[k] = String(v); if (k === 'id') el.id = String(v); };
  el.removeAttribute = function (k) { delete el.attributes[k]; };
  el.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(el.attributes, k); };
  el.toggleAttribute = noop;
  el.getBoundingClientRect = function () { return { top: 0, left: 0, right: 800, bottom: 400, width: 800, height: 400, x: 0, y: 0 }; };
  el.getClientRects = function () { return []; };
  el.focus = noop; el.blur = noop; el.click = noop; el.select = noop; el.scrollIntoView = noop; el.scrollTo = noop;
  el.setSelectionRange = noop; el.setPointerCapture = noop; el.releasePointerCapture = noop;
  el.showModal = noop; el.close = noop; el.show = noop;
  el.requestFullscreen = function () { return Promise.resolve(); };
  el.animate = function () { return { cancel: noop, finished: Promise.resolve() }; };
  el.getContext = function () { return new Proxy({}, { get: function () { return noop; } }); };
  el.toDataURL = function () { return 'data:,'; };
  el.toBlob = noop;
  el.cloneNode = function () { return makeEl(el.tagName); };
  el.add = function (opt) { el.options.push(opt); };
  el.insertRow = function () { const r = makeEl('tr'); el.rows.push(r); r.insertCell = function () { const c = makeEl('td'); r.cells.push(c); return c; }; return r; };
  el.deleteRow = noop;
  return el;
}

function getById(id) {
  if (!elementsById.has(id)) elementsById.set(id, makeEl('div', id));
  return elementsById.get(id);
}

function makeStorage() {
  const m = new Map();
  return {
    getItem: function (k) { return m.has(k) ? m.get(k) : null; },
    setItem: function (k, v) { m.set(k, String(v)); },
    removeItem: function (k) { m.delete(k); },
    clear: function () { m.clear(); },
    key: function (i) { return Array.from(m.keys())[i] || null; },
    get length() { return m.size; },
  };
}

const document = {
  nodeType: 9,
  body: makeEl('body'),
  documentElement: makeEl('html'),
  head: makeEl('head'),
  title: '',
  cookie: '',
  readyState: 'complete',
  hidden: false,
  visibilityState: 'visible',
  activeElement: null,
  currentScript: null,
  fullscreenElement: null,
  fullscreenEnabled: false,
  referrer: '',
  fonts: { ready: Promise.resolve(), load: function () { return Promise.resolve(); } },
  getElementById: getById,
  querySelector: queryOne,
  querySelectorAll: function () { return []; },
  getElementsByTagName: function () { return []; },
  getElementsByClassName: function () { return []; },
  createElement: function (tag) { return makeEl(tag); },
  createElementNS: function (ns, tag) { return makeEl(tag); },
  createTextNode: makeTextNode,
  createDocumentFragment: function () { return makeEl('fragment'); },
  createEvent: function () { return { initEvent: noop }; },
  createRange: function () { return { selectNodeContents: noop, selectNode: noop }; },
  getSelection: function () { return { removeAllRanges: noop, addRange: noop, toString: function () { return ''; } }; },
  addEventListener: noop,
  removeEventListener: noop,
  dispatchEvent: function () { return true; },
  hasFocus: function () { return true; },
  execCommand: function () { return true; },
  exitFullscreen: function () { return Promise.resolve(); },
  elementFromPoint: function () { return null; },
};
document.body.contains = function () { return true; };

class GhostObserver { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }
class GhostEvent {
  constructor(type, init) { this.type = type; this.defaultPrevented = false; Object.assign(this, init || {}); }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() {}
  stopImmediatePropagation() {}
}
class GhostCustomEvent extends GhostEvent { constructor(type, init) { super(type, init); this.detail = init && init.detail; } }

const sandbox = {
  console: {
    log: noop, info: noop, debug: noop,
    warn: function () { consoleWarnings.push(Array.prototype.map.call(arguments, String).join(' ').slice(0, 200)); },
    error: function () { consoleWarnings.push(Array.prototype.map.call(arguments, String).join(' ').slice(0, 200)); },
  },
  document: document,
  localStorage: makeStorage(),
  sessionStorage: makeStorage(),
  location: {
    href: 'http://127.0.0.1:8080/', origin: 'http://127.0.0.1:8080', protocol: 'http:', host: '127.0.0.1:8080',
    hostname: '127.0.0.1', port: '8080', pathname: '/', search: '', hash: '',
    reload: noop, assign: noop, replace: noop, toString: function () { return 'http://127.0.0.1:8080/'; },
  },
  history: { replaceState: noop, pushState: noop, back: noop, state: null },
  navigator: { userAgent: 'node-ghost-dom', language: 'en-US', onLine: true, clipboard: { writeText: function () { return Promise.resolve(); } }, sendBeacon: function () { return true; } },
  screen: { width: 1440, height: 900 },
  innerWidth: 1440, innerHeight: 900, devicePixelRatio: 1, scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
  matchMedia: function () { return { matches: false, media: '', addEventListener: noop, removeEventListener: noop, addListener: noop, removeListener: noop }; },
  getComputedStyle: function () { return { getPropertyValue: function () { return ''; }, fontSize: '14px' }; },
  requestAnimationFrame: function (cb) { return setTimeout(cb, 0); },
  cancelAnimationFrame: clearTimeout,
  setTimeout: setTimeout, clearTimeout: clearTimeout,
  setInterval: function () { return 0; }, clearInterval: noop,  // disabled so the harness can exit
  fetch: function () { return new Promise(function () {}); },     // default: never resolves, never touches the network
  alert: noop, confirm: function () { return false; }, prompt: function () { return null; },
  open: function () { return null; }, print: noop, scrollTo: noop, scroll: noop, focus: noop, blur: noop, close: noop,
  getSelection: function () { return { toString: function () { return ''; }, removeAllRanges: noop }; },
  addEventListener: noop, removeEventListener: noop, dispatchEvent: function () { return true; }, postMessage: noop,
  performance: { now: function () { return Date.now(); }, mark: noop, measure: noop },
  URL: URL, URLSearchParams: URLSearchParams, TextEncoder: TextEncoder, TextDecoder: TextDecoder, Blob: Blob,
  structuredClone: structuredClone, queueMicrotask: queueMicrotask, crypto: globalThis.crypto,
  atob: function (s) { return Buffer.from(s, 'base64').toString('binary'); },
  btoa: function (s) { return Buffer.from(s, 'binary').toString('base64'); },
  Event: GhostEvent, CustomEvent: GhostCustomEvent, MessageEvent: GhostEvent, KeyboardEvent: GhostEvent, MouseEvent: GhostEvent, PointerEvent: GhostEvent,
  IntersectionObserver: GhostObserver, ResizeObserver: GhostObserver, MutationObserver: GhostObserver,
  Node: GhostNode,
  HTMLElement: GhostElement, Element: GhostElement, SVGElement: GhostElement, HTMLInputElement: GhostElement, HTMLSelectElement: GhostElement,
  Image: function () { this.onload = null; this.onerror = null; },
  XMLHttpRequest: function () { this.open = noop; this.send = noop; this.setRequestHeader = noop; },
  DOMParser: function () { this.parseFromString = function () { return { documentElement: makeEl('svg'), body: makeEl('body'), querySelector: function () { return null; } }; }; },
  FileReader: function () { this.readAsText = noop; this.readAsDataURL = noop; },
  FormData: function () { this.append = noop; },
  AbortController: function () { this.signal = { aborted: false, addEventListener: noop }; this.abort = noop; },
  ClipboardItem: function () {},
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
sandbox.top = sandbox;
sandbox.parent = sandbox;   // parent === window -> isEmbedded() === false (full dashboard mode)
sandbox.frames = sandbox;
sandbox.frameElement = null;
document.defaultView = sandbox;

const ctx = vm.createContext(sandbox);

// ---------------------------------------------------------------------------
// Load every inline script from the rendered HTML, in document order.
// ---------------------------------------------------------------------------
const scriptBodies = [];
const scriptRe = /<script([^>]*)>([\s\S]*?)<\/script>/g;
let sm;
while ((sm = scriptRe.exec(html)) !== null) {
  if (/\bsrc\s*=/.test(sm[1])) continue;  // external scripts are not part of the self-contained page
  scriptBodies.push(sm[2]);
}
const loadErrors = [];
scriptBodies.forEach(function (body, idx) {
  try {
    vm.runInContext(body, ctx, { filename: 'dashboard-inline-script-' + (idx + 1) + '.js' });
  } catch (err) {
    loadErrors.push('script#' + (idx + 1) + ': ' + (err && err.stack ? err.stack.split('\n').slice(0, 3).join(' | ') : String(err)));
  }
});

function inCtx(code) { return vm.runInContext(code, ctx); }

const failures = [];
let checks = 0;
function check(cond, label) {
  checks += 1;
  if (!cond) failures.push(label);
}

// ---------------------------------------------------------------------------
// Scenarios against the REAL functions.
// ---------------------------------------------------------------------------
async function main() {
  check(loadErrors.length === 0, 'all inline scripts load without throwing: ' + loadErrors.join(' || '));
  check(inCtx("typeof renderState === 'function'"), 'real renderState is defined');
  check(inCtx("typeof renderStateUnsafe === 'function'"), 'real renderStateUnsafe (commit-after-render body) is defined');
  check(inCtx("typeof isNonStateErrorPayload === 'function'"), 'real isNonStateErrorPayload is defined');
  check(inCtx("typeof triggerApi === 'function'"), 'real triggerApi is defined');
  check(inCtx("typeof VL === 'object' && typeof VL.classifyError === 'function'"), 'VL.classifyError available to the page');
  if (failures.length) return;

  sandbox.__goodJson = goodStateJson;
  inCtx('var __good = JSON.parse(__goodJson);');
  const goodName = inCtx('String(__good.active_agent.display_name)');

  // 1. Positive control: a valid state is committed and painted.
  inCtx('renderState(__good);');
  check(inCtx('currentState === __good'), 'valid state is committed to currentState');
  check(inCtx("document.getElementById('agentTitleText').textContent") === goodName, 'valid state paints the agent title');

  // 2. Error payloads must never replace the state or repaint the title (real gate + real classifier).
  const errorPayloads = [
    { label: 'VL-INPUT-422 validation', expectCode: 'VL-INPUT-422',
      payload: { status: 'ERROR', error_kind: 'VALIDATION_ERROR', error_code: 'VL-INPUT-422', is_code_bug: false, state_preserved: true, errors: ['baseline_val: must be a finite number'], what_to_do: 'Fix it.' } },
    { label: 'VL-LIVE-GUARD', expectCode: 'VL-LIVE-GUARD',
      payload: { status: 'DISABLED_IN_LIVE_MODE', reason: 'Synthetic anomaly injection is disabled when connected to a live Google Cloud project.' } },
    { label: 'VL-CODE-500', expectCode: 'VL-CODE-500',
      payload: { status: 'ERROR', error_kind: 'INTERNAL_CODE_ERROR', error_code: 'VL-CODE-500', is_code_bug: true, state_preserved: true, where: 'POST /api/recompute_finops', request_id: 'req-deadbeef', exception_type: 'KeyError', user_message: 'boom', what_to_do: 'Share Request ID req-deadbeef', diagnostic_cmd: 'gcloud logging read ...' } },
    { label: 'INVALID_REQUEST', expectCode: null,
      payload: { status: 'INVALID_REQUEST', reason: 'resource_name must look like projects/.../engines/...' } },
    { label: 'dead-letter invalid_payload (gate condition the old harness copy lacked)', expectCode: null,
      payload: { error: 'invalid_payload', errors: ['agent_name: required'], dead_letter: { source: '/api/decorator_ingest', values_logged: false } } },
    { label: 'upstream PERMISSION_DENIED notice', expectCode: 'VL-GCP-PERMISSION_DENIED',
      payload: { status: 'PERMISSION_DENIED', user_message: 'BigQuery access denied', what_to_do: 'Grant roles/bigquery.dataViewer' } },
    { label: 'bare ERROR', expectCode: null, payload: { status: 'ERROR' } },
  ];
  for (let it = 0; it < ITERATIONS; it++) {
    const ep = errorPayloads[it % errorPayloads.length];
    sandbox.__payloadJson = JSON.stringify(ep.payload);
    inCtx("document.getElementById('apiNotice').dataset.errorCode = ''; renderState(JSON.parse(__payloadJson));");
    check(inCtx('currentState === __good'), ep.label + ': currentState untouched');
    check(inCtx("document.getElementById('agentTitleText').textContent") === goodName, ep.label + ': title untouched');
    check(inCtx('isNonStateErrorPayload(JSON.parse(__payloadJson)) === true'), ep.label + ': real gate classifies as non-state payload');
    const code = inCtx("String(document.getElementById('apiNotice').dataset.errorCode || '')");
    if (ep.expectCode) check(code === ep.expectCode, ep.label + ': banner code ' + code + ' === ' + ep.expectCode);
    else check(code.length > 0, ep.label + ': some VL-* banner code shown (' + code + ')');
  }

  // 2b. Action receipts (not errors, not states) must never be committed either: structural guard.
  sandbox.__payloadJson = JSON.stringify({ status: 'OK', mode: 'incremental', rows_affected: 42 });
  inCtx("document.getElementById('apiNotice').textContent = ''; renderState(JSON.parse(__payloadJson));");
  check(inCtx('currentState === __good'), 'action receipt: currentState untouched');
  check(inCtx("document.getElementById('apiNotice').textContent.length > 0"), 'action receipt: shown as a notice');
  check(inCtx("document.getElementById('agentTitleText').textContent") === goodName, 'action receipt: title untouched');

  // 3. Malformed states that throw half-way through the real renderer: roll back AND repaint.
  const malformed = [
    { label: 'parameter without unit (p.unit.startsWith throws)', mutate: "__bad.active_agent.display_name = 'Renamed During Crash'; __bad.active_agent.parameters[0].unit = null;" },
    { label: 'health_status null (.includes throws)', mutate: "__bad.active_agent.display_name = 'Renamed During Crash'; __bad.active_agent.health_status = null;" },
    { label: 'parameters not an array (.slice throws)', mutate: "__bad.active_agent.display_name = 'Renamed During Crash'; __bad.active_agent.parameters = 'nope';" },
  ];
  for (let it = 0; it < ITERATIONS; it++) {
    const mc = malformed[it % malformed.length];
    inCtx('var __bad = JSON.parse(__goodJson); ' + mc.mutate + " document.getElementById('apiNotice').dataset.errorCode = '';");
    let threw = false;
    try { inCtx('renderState(__bad);'); } catch (e) { threw = true; }
    check(!threw, mc.label + ': renderState must not propagate the exception');
    check(inCtx('currentState === __good'), mc.label + ': currentState rolled back to last good state');
    check(inCtx("document.getElementById('apiNotice').dataset.errorCode") === 'VL-UI-RUNTIME', mc.label + ': VL-UI-RUNTIME banner shown');
    check(inCtx("document.getElementById('apiNotice').dataset.isCodeBug") === 'true', mc.label + ': flagged as code bug, not user error');
    check(inCtx("document.getElementById('agentTitleText').textContent") === goodName, mc.label + ': title repainted from last good state (not the half-rendered one)');
  }

  // 4. The real triggerApi() with stubbed fetch responses (no network).
  function stubFetch(status, payload, reject) {
    sandbox.fetch = function () {
      if (reject) return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve({ ok: status >= 200 && status < 300, status: status, json: function () { return Promise.resolve(payload); } });
    };
  }
  const apiCases = [
    { label: 'triggerApi 422 validation', status: 422, expectCode: 'VL-INPUT-422', payload: errorPayloads[0].payload },
    { label: 'triggerApi live guard (HTTP 200 body)', status: 200, expectCode: 'VL-LIVE-GUARD', payload: errorPayloads[1].payload },
    { label: 'triggerApi 500 code bug', status: 500, expectCode: 'VL-CODE-500', payload: errorPayloads[2].payload, expectRequestId: 'req-deadbeef' },
    { label: 'triggerApi network failure', status: 0, expectCode: 'VL-NET-ERR', reject: true },
    { label: 'triggerApi HTTP 503 with non-JSON body', status: 503, expectCode: 'VL-HTTP-503', payload: null },
  ];
  for (let it = 0; it < ITERATIONS; it++) {
    const ac = apiCases[it % apiCases.length];
    stubFetch(ac.status, ac.payload, ac.reject);
    inCtx("document.getElementById('apiNotice').dataset.errorCode = ''; document.getElementById('apiNotice').dataset.requestId = '';");
    await inCtx("triggerApi('/api/recompute_finops', {calls_per_hr: 1})");
    check(inCtx('currentState === __good'), ac.label + ': currentState untouched');
    const code = inCtx("String(document.getElementById('apiNotice').dataset.errorCode || '')");
    check(code === ac.expectCode, ac.label + ': banner code ' + code + ' === ' + ac.expectCode);
    if (ac.expectRequestId) check(inCtx("document.getElementById('apiNotice').dataset.requestId") === ac.expectRequestId, ac.label + ': request id surfaced');
  }
  // Positive control through triggerApi: a 200 state payload IS applied.
  inCtx("var __next = JSON.parse(__goodJson); __next.active_agent.display_name = 'Applied Via Api';");
  sandbox.fetch = function () { return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(inCtx('__next')); } }); };
  await inCtx("triggerApi('/api/select_agent', {agent_id: __next.active_agent.agent_id})");
  check(inCtx("document.getElementById('agentTitleText').textContent") === 'Applied Via Api', 'triggerApi applies a healthy 200 state payload');
  check(inCtx('currentState === __next'), 'triggerApi commits the healthy state');
  inCtx('renderState(__good);');  // restore for any later checks

  // 5. Verify every dashboard button handler works in both standalone and embedded modes.
  let createdBlobs = [];
  let printCount = 0;
  const origCreateObjUrl = URL.createObjectURL;
  const origRevokeObjUrl = URL.revokeObjectURL;
  URL.createObjectURL = function (blob) { createdBlobs.push(blob); return 'blob:finops-test-' + createdBlobs.length; };
  URL.revokeObjectURL = noop;
  sandbox.print = function () { printCount += 1; };

  // Populate exportPdfBtn with an SVG icon + text node just like the template HTML
  inCtx("var _eb = document.getElementById('exportPdfBtn'); _eb.replaceChildren(document.createElement('svg'), document.createTextNode(' Export PDF'));");
  const pdfOut = inCtx('exportDashboardPdf()');
  check(createdBlobs.length === 1, 'exportDashboardPdf creates a downloadable PDF Blob in standalone mode');
  check(pdfOut && typeof pdfOut.pdf === 'string' && pdfOut.pdf.startsWith('%PDF-1.4\n'), 'exportDashboardPdf returns valid %PDF-1.4 document');
  check(inCtx("document.getElementById('exportPdfBtn').firstElementChild && document.getElementById('exportPdfBtn').firstElementChild.tagName === 'SVG'"), 'exportDashboardPdf flash preserves SVG icon inside #exportPdfBtn');
  inCtx('exportDashboardPdf({shiftKey: true})');
  check(printCount === 1, 'exportDashboardPdf({shiftKey: true}) invokes window.print()');

  // Basic view: Antigravity (7) is a top-level basic tab, so it opens without Advanced mode (advanced tabs fall back).
  sandbox.fetch = function () { return Promise.reject(new Error('offline test')); };
  let syncedUrl = '';
  sandbox.history.replaceState = function (s, t, u) { syncedUrl = String(u); };
  check(inCtx('isAdvancedMode() === false'), 'dashboard starts in the basic view');
  inCtx('switchTab(7)');
  check(inCtx('currentTabIndex') === 7, 'switchTab(7) opens the Antigravity tab in the basic view');
  check(inCtx("document.getElementById('tabBtn7').classList.contains('active') && !document.getElementById('tabPanel7').classList.contains('hidden')"),
        'Antigravity tab button is active and its panel is shown');
  check(inCtx("document.getElementById('tabPanel3').classList.contains('hidden') && document.getElementById('tabPanel6').classList.contains('hidden')"),
        'other tab panels are hidden while the Antigravity tab is open');
  // The moved panel still renders from state.antigravity_finops while its tab is visible (first view and refresh).
  const agyRendered = "(function (a) { if (!a) return true;" +
    " var rows = function (id, list) { return document.getElementById(id).children.length === Math.max(1, (list || []).length); };" +
    " return document.getElementById('agyFinopsKpis').children.length > 0 && rows('agyPoolBody', a.pools) &&" +
    " rows('agyModelsBody', a.by_model_effort) && rows('agyUsersBody', a.by_user); })(currentState && currentState.antigravity_finops)";
  check(inCtx(agyRendered), 'Antigravity KPIs and tables are painted on the visible tab 7');
  inCtx('renderState(__good);');
  check(inCtx('currentTabIndex') === 7 && inCtx(agyRendered), 'a state refresh repaints the Antigravity panel and keeps tab 7 open');
  check(/[?&]tab=7(&|$)/.test(syncedUrl), 'URL / Copy link keeps the Antigravity tab (' + syncedUrl + ')');
  inCtx('switchTab(1)');
  check(inCtx('currentTabIndex') === 6, 'advanced-only tab 1 falls back to Overview in the basic view');
  check(inCtx("tabFromSearch('?tab=antigravity') === 7 && tabFromSearch('?w=24&tab=Antigravity') === 7"), '?tab=antigravity deep-links to tab 7');
  check(inCtx("tabFromSearch('?tab=7') === 7 && tabFromSearch('?tab=3') === 3 && tabFromSearch('?tab=nope') === null && tabFromSearch('') === null"),
        'numeric ?tab= deep links are unchanged and unknown names are ignored');
  sandbox.history.replaceState = noop;

  // Enable Advanced mode and test tab switching across all 8 tabs (0..7)
  inCtx('toggleAdvancedMode()');
  check(inCtx('isAdvancedMode() === true'), 'toggleAdvancedMode enables advanced mode');
  for (let t = 0; t <= 7; t++) {
    inCtx('switchTab(' + t + ')');
    check(inCtx('currentTabIndex') === t, 'switchTab(' + t + ') activates tab ' + t);
  }

  // Display mode & Telemetry Validator drawer
  inCtx('toggleDisplayMode()');
  check(inCtx("currentDisplayMode === 'fullscreen'"), 'toggleDisplayMode switches to fullscreen');
  inCtx('toggleDisplayMode()');
  check(inCtx("currentDisplayMode === 'pip'"), 'toggleDisplayMode returns to pip');
  inCtx("document.getElementById('telemetryValidatorDrawer').classList.add('hidden')");
  inCtx('toggleTelemetryValidatorDrawer()');
  check(inCtx("!document.getElementById('telemetryValidatorDrawer').classList.contains('hidden')"), 'toggleTelemetryValidatorDrawer opens drawer');
  inCtx('toggleTelemetryValidatorDrawer()');
  check(inCtx("document.getElementById('telemetryValidatorDrawer').classList.contains('hidden')"), 'toggleTelemetryValidatorDrawer closes drawer');

  // Prompt Cache X-Ray: Load example -> Run X-Ray -> Copy rewrite
  inCtx('pxLoadExample()');
  check(inCtx("document.getElementById('pxPrev').value.length > 40 && document.getElementById('pxCurr').value.length > 40"), 'pxLoadExample populates both prompt textareas immediately');
  await inCtx('runPromptXray()');
  check(inCtx("!document.getElementById('pxResult').classList.contains('hidden')"), 'runPromptXray renders X-Ray result using client-side fallback when offline');
  await inCtx('pxCopyRewrite()');
  check(inCtx("document.getElementById('pxCopyBtn').textContent === 'Copied'"), 'pxCopyRewrite copies rewritten prompt');

  // Embedded mode tests (simulate Gemini Enterprise iframe host)
  const hostMessages = [];
  sandbox.parent = {
    postMessage: function (msg) {
      hostMessages.push(msg);
    },
  };
  check(inCtx('isEmbedded() === true'), 'isEmbedded() is true when window.parent !== window');

  // Embedded Export PDF
  const embPdf = inCtx('exportDashboardPdf()');
  check(embPdf && embPdf.pdf.startsWith('%PDF-1.4\n'), 'exportDashboardPdf builds %PDF-1.4 in embedded mode');
  check(createdBlobs.length === 2, 'exportDashboardPdf triggers Blob PDF download in embedded mode');

  // Embedded What-If Project button (model_switch & cache_share)
  inCtx("document.getElementById('wiKind').value = 'model_switch'; document.getElementById('wiFrom').value = 'gemini-2.5-pro'; document.getElementById('wiTo').value = 'gemini-3.8-flash'; document.getElementById('wiShare').value = '50';");
  await inCtx('runLiveWhatIf()');
  check(inCtx("document.getElementById('liveWhatIfResult').children.length >= 2"), 'runLiveWhatIf computes model_switch projection in embedded mode');
  inCtx("document.getElementById('wiKind').value = 'cache_share'; onLiveWhatIfKind(); document.getElementById('wiModel').value = 'gemini-3.8-flash'; document.getElementById('wiCache').value = '75';");
  await inCtx('runLiveWhatIf()');
  check(inCtx("document.getElementById('liveWhatIfResult').children.length >= 2"), 'runLiveWhatIf computes cache_share projection in embedded mode');

  // Embedded FinOps Ledger Recompute button
  await inCtx('recomputeFinopsLedger()');
  check(inCtx("document.getElementById('finopsRecomputeBtn').textContent.indexOf('Recomputed') === 0"), 'recomputeFinopsLedger updates ledger and button state in embedded mode');

  // Embedded Telemetry Validation Audit button
  await inCtx('runTelemetryValidationAudit(true)');
  check(inCtx("document.getElementById('runLlmJudgeAuditBtn').textContent.indexOf('Audit Verified') === 0"), 'runTelemetryValidationAudit verifies telemetry in embedded mode');

  // Embedded GE Mart Refresh button
  await inCtx('refreshGeMartNow()');
  check(inCtx("document.getElementById('geMartRefreshBtn').textContent.indexOf('Mart Synced') === 0"), 'refreshGeMartNow syncs mart in embedded mode');

  // Embedded Optimizer platform selector & Reset
  await inCtx("triggerApi('/api/select_optimizer', {platform_id: 'vertex_prompt_opt'})");
  check(inCtx("currentState.optimizer_platforms.active_platform_id === 'vertex_prompt_opt'"), 'select_optimizer switches platform in embedded mode');

  sandbox.parent = sandbox;
  URL.createObjectURL = origCreateObjUrl;
  URL.revokeObjectURL = origRevokeObjUrl;
}

main().then(function () {
  const report = {
    ok: failures.length === 0,
    checks: checks,
    failed: failures.length,
    failures: failures.slice(0, 25),
    inline_scripts_loaded: scriptBodies.length - loadErrors.length,
    inline_scripts_total: scriptBodies.length,
    console_warnings: consoleWarnings.length,
    iterations: ITERATIONS,
  };
  process.stdout.write(JSON.stringify(report) + '\n');
  process.exit(failures.length === 0 ? 0 : 1);
}).catch(function (err) {
  process.stdout.write(JSON.stringify({ ok: false, checks: checks, failed: failures.length + 1, failures: failures.concat(['harness crashed: ' + (err && err.stack ? err.stack : String(err))]) }) + '\n');
  process.exit(1);
});
