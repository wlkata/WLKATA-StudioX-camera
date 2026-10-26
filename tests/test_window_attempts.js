const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element(id) {
  const el = {
    id: id,
    value: '0',
    disabled: false,
    textContent: '',
    style: {},
    options: [],
    attrs: {},
    listeners: {},
    appendChild: function (child) { this.options.push(child); },
    setAttribute: function (key, value) { this.attrs[key] = value; },
    getAttribute: function (key) { return this.attrs[key]; },
    removeAttribute: function (key) { delete this.attrs[key]; },
    addEventListener: function (type, fn) { this.listeners[type] = fn; }
  };
  Object.defineProperty(el, 'innerHTML', {
    set: function () { this.options = []; }
  });
  return el;
}

const select = element('cam-select');
const connectBtn = element('cam-connect');
const disconnectBtn = element('cam-disconnect');
const statusEl = element('cam-status');
const frame = element('cam-frame');
const empty = element('cam-empty');
const elements = {
  'cam-select': select,
  'cam-connect': connectBtn,
  'cam-disconnect': disconnectBtn,
  'cam-status': statusEl,
  'cam-frame': frame,
  'cam-empty': empty
};

const fetches = [];
function fakeFetch(url, options) {
  let resolve;
  let reject;
  const promise = new Promise(function (res, rej) {
    resolve = res;
    reject = rej;
  });
  fetches.push({ url: url, options: options || {}, resolve: resolve, reject: reject });
  return promise;
}

const listeners = {};
const context = {
  window: {
    STUDIOX_PORT: '5080',
    crypto: { randomUUID: function () { return 'id-' + (context._n = (context._n || 0) + 1); } },
    addEventListener: function (type, fn) { listeners[type] = fn; }
  },
  document: {
    getElementById: function (id) { return elements[id] || null; },
    createElement: function () { return element('option'); }
  },
  fetch: fakeFetch,
  Number: Number,
  isFinite: isFinite,
  JSON: JSON,
  Promise: Promise,
  console: console
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '..', 'frontend', 'window.js'), 'utf8'),
  context
);

function jsonBody(entry) {
  return JSON.parse(entry.options.body);
}

async function flush() {
  for (var i = 0; i < 6; i++) await Promise.resolve();
}

(async function () {
  const listed = fetches.shift();
  assert.ok(listed.url.indexOf('/cameras?refresh=1') !== -1);
  listed.resolve({ json: async function () { return { success: true, cameras: [{ id: 0, name: 'FaceTime' }] }; } });
  await flush();
  assert.strictEqual(statusEl.textContent, 'No camera connected');

  connectBtn.listeners.click();
  const first = fetches[fetches.length - 1];
  const firstBody = jsonBody(first);
  assert.strictEqual(first.url.endsWith('/connect'), true);
  assert.ok(firstBody.client.indexOf('window-') === 0);
  assert.strictEqual(connectBtn.disabled, true);

  disconnectBtn.listeners.click();
  const firstDisconnect = fetches[fetches.length - 1];
  assert.strictEqual(jsonBody(firstDisconnect).client, firstBody.client);
  first.resolve({ json: async function () { return { success: true, id: 0 }; } });
  await flush();
  assert.strictEqual(statusEl.textContent, 'No camera connected');
  assert.strictEqual(frame.src, undefined);

  connectBtn.listeners.click();
  const second = fetches[fetches.length - 1];
  const secondBody = jsonBody(second);
  assert.notStrictEqual(secondBody.client, firstBody.client);
  second.resolve({ json: async function () { return { success: true, id: 0 }; } });
  await flush();
  const refresh = fetches[fetches.length - 1];
  refresh.resolve({ json: async function () { return { success: true, cameras: [{ id: 0, name: 'FaceTime' }] }; } });
  await flush();
  assert.ok(statusEl.textContent.indexOf('Showing') === 0);
  assert.strictEqual(frame.src, 'http://127.0.0.1:5080/ext/camera/stream?id=0');
  assert.strictEqual(frame.attrs['data-stream'], frame.src);

  listeners.beforeunload();
  const unload = fetches[fetches.length - 1];
  assert.strictEqual(jsonBody(unload).client, secondBody.client);
  assert.strictEqual(unload.options.keepalive, true);

  console.log('window attempts ok');
})().catch(function (error) {
  console.error(error);
  process.exit(1);
});
