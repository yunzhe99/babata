import test from 'node:test';
import assert from 'node:assert/strict';
import pageDefinition from '../pages/memory/index.js';
import config from '../config.js';

const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
// Actual page/controller with only synthetic host APIs and credentials; never real network.
function fixture(t, result) {
  // Hold the clock still so the first POST keeps the full configured deadline.
  // Advancing wall time during setup must not change an exact timeout assertion.
  t.mock.method(Date, 'now', () => 1700000000000);
  const oldConfig = { ...config };
  Object.assign(config, { endpoint: 'https://fixture.invalid/v1/chat', token: 'synthetic-sensitive-value', sessionId: 'fixture-only', timeoutMs: 120000 });
  const saved = {}, timers = new Map(), calls = [], patches = [];
  let nextId = 0;
  const globals = {
    fetch(...args) { calls.push(args); return result(...args); },
    setTimeout(callback, ms) { const id = ++nextId; timers.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  for (const [key, value] of Object.entries(globals)) {
    saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const page = { ...pageDefinition, data: { ...pageDefinition.data },
    setData(patch) { patches.push(patch); Object.assign(this.data, patch); } };
  page.onLoad({});
  page.shown = true;
  t.after(() => {
    page.onUnload();
    Object.assign(config, oldConfig);
    for (const [key, descriptor] of Object.entries(saved)) Object.defineProperty(globalThis, key, descriptor);
  });
  return { page, calls, patches, timers,
    fireDeadline() {
      const entry = [...timers].find(([, timer]) => timer.ms === 12000);
      assert.ok(entry); timers.delete(entry[0]); entry[1].callback();
    } };
}

test('GET is exactly fetch(endpoint), with no option/signal/credential object or response body read', async t => {
  const f = fixture(t, () => Promise.resolve({ status: 405,
    json() { throw new Error('body must not be read'); } }));
  assert.equal(await f.page._runGetDiagnostic(), '联网405');
  assert.deepEqual(f.calls, [[config.endpoint]]);
  assert.equal(f.timers.size, 0);
  assert.equal(await f.page._runGetDiagnostic(), null);
});

test('inherited message keeps the known native provider reason on rejection', async t => {
  const error = Object.create({ message: 'Request failed: Networking provider is not registered' });
  const f = fixture(t, () => Promise.reject(error));
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：Request failed: Networking provider is not registered');
  assert.equal(f.timers.size, 0);
});

test('inherited errMsg is available for synchronous GET throws', async t => {
  const error = Object.create({ errMsg: 'Native transport is not ready' });
  const f = fixture(t, () => { throw error; });
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：Native transport is not ready');
  assert.equal(f.timers.size, 0);
});

test('exact configured token is redacted before further formatting', async t => {
  const f = fixture(t, () => Promise.reject(`Native failed ${config.token} and ${config.token}`));
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：Native failed [已隐藏] and [已隐藏]');
  assert.equal(JSON.stringify(f.patches).includes(config.token), false);
});

test('sensitive/request field names suppress the whole short detail', async t => {
  const labels = ['auth', 'token', 'cookie', 'password', 'secret', 'APIkey', 'api_key',
    'headers', 'body', 'Bearer', 'credential', '请求头', '请求体', 'a\u0000uth'];
  let label;
  const f = fixture(t, () => Promise.reject({ message: `${label}: opaque-value` }));
  for (label of labels) {
    f.page.getDiagDone = false;
    assert.equal(await f.page._runGetDiagnostic(), '联网未完成：错误内容已隐藏', label);
  }
  assert.equal(JSON.stringify(f.patches).includes('opaque-value'), false);
});

test('URLs, bare domains, IP versions, email and control characters are removed', async t => {
  const reason = 'Fail https://host.example/path 192.0.2.10:8443 [2001:db8::1]:8443 ::1 user@example.com host.example\u001b\u202e';
  const f = fixture(t, () => Promise.reject(reason));
  const result = await f.page._runGetDiagnostic();
  for (const raw of ['https://', '192.0.2.10', '2001', '::1', 'user@', 'host.example', '\u001b', '\u202e']) assert.equal(result.includes(raw), false, raw);
  assert.match(result, /地址已隐藏/); assert.match(result, /邮箱已隐藏/);
});

test('overlong raw details are hidden before truncation can discard a dangerous suffix', async t => {
  const f = fixture(t, () => Promise.reject('x'.repeat(2049) + 'headers: dangerous-value'));
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：错误内容已隐藏');
});

test('summary is limited to 90 Unicode code points', async t => {
  const f = fixture(t, () => Promise.reject('错'.repeat(150)));
  const result = await f.page._runGetDiagnostic();
  assert.equal(Array.from(result.slice('联网未完成：'.length)).length, 90);
});

test('throwing getters and object coercion never escape or access stack/JSON', async t => {
  let messageReads = 0, stackReads = 0, stringifyCalls = 0;
  const error = { get message() { messageReads++; throw new Error('getter failed'); },
    get errMsg() { throw new Error('getter failed'); },
    get stack() { stackReads++; throw new Error('no stack'); },
    toJSON() { stringifyCalls++; throw new Error('no JSON'); },
    toString() { throw new Error('no coercion'); } };
  const f = fixture(t, () => Promise.reject(error));
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：未提供可显示错误');
  assert.equal(messageReads, 1); assert.equal(stackReads, 0); assert.equal(stringifyCalls, 0);
});

for (const action of ['hide', 'unload']) {
  test(`${action} settles locally and ignores late GET without pretending to abort transport`, async t => {
    const pending = deferred(); const f = fixture(t, () => pending.promise);
    const running = f.page._runGetDiagnostic();
    const deadline = [...f.timers.values()][0].callback;
    if (action === 'hide') f.page.onHide(); else f.page.onUnload();
    assert.equal(await running, null); assert.equal(f.page.getDiagOwner, null);
    assert.deepEqual(f.calls, [[config.endpoint]]); assert.equal(f.timers.size, 0);
    const count = f.patches.length;
    deadline(); pending.resolve({ status: 405 }); await flush();
    assert.equal(f.patches.length, count);
  });
}

test('12-second deadline settles an uncooperative bare GET and ignores late rejection', async t => {
  const pending = deferred(); const f = fixture(t, () => pending.promise);
  const running = f.page._runGetDiagnostic();
  f.fireDeadline(); assert.equal(await running, '联网超时');
  assert.equal(f.timers.size, 0); assert.equal(f.page.getDiagOwner, null);
  let getterReads = 0; const count = f.patches.length;
  pending.reject({ get message() { getterReads++; return 'late native reason'; } }); await flush();
  assert.equal(getterReads, 0); assert.equal(f.patches.length, count);
});

test('GET summary stays separate from unchanged POST auth, body, signal and raw error', async t => {
  const f = fixture(t, (_url, options) => options
    ? Promise.reject(new Error('post-only-private-diagnostic'))
    : Promise.reject(new Error('Networking provider is not registered')));
  await f.page.assistant.show('普通问题'); await flush();
  const [post, get] = f.calls;
  assert.equal(post[1].method, 'POST');
  assert.equal(post[1].headers.Authorization, `Bearer ${config.token}`);
  assert.equal(JSON.parse(post[1].body).remember, false);
  assert.ok(post[1].signal); assert.equal(post[1].timeout, 120000);
  assert.deepEqual(get, [config.endpoint]);
  assert.match(f.page.data.getDiag, /Networking provider is not registered/);
  assert.equal(f.page.data.buildTag, 'photo-1001p', 'controller updates retain the page build tag');
  assert.equal(JSON.stringify(f.patches).includes('post-only-private-diagnostic'), false);
  assert.equal(JSON.stringify(f.patches).includes(config.token), false);
});
