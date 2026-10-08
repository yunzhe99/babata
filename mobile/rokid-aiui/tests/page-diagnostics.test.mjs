import test from 'node:test';
import assert from 'node:assert/strict';
import pageDefinition from '../pages/memory/index.js';
import config from '../config.js';
import wx from 'wx';

const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Real page/controller, synthetic host APIs only. No real network or audio.
function fixture(t, { fetch: fetchResult, message, brokenTimer = false } = {}) {
  const saved = {};
  const timers = new Map(), requests = [], controllers = [], recognitions = [], patches = [];
  let timerId = 0, closed = 0, speechCalls = 0;
  const NativeAbortController = globalThis.AbortController;
  const oldConfig = { ...config };
  Object.assign(config, { endpoint: 'https://fixture.invalid/v1/chat', token: 'test-only', sessionId: 'voice-memory', timeoutMs: 1234 });
  const globals = {
    AbortController: class extends NativeAbortController { constructor() { super(); controllers.push(this); } },
    setTimeout(callback, ms) {
      if (brokenTimer && (ms === 200 || ms === 3000)) throw new TypeError('synthetic-private-timer');
      const id = ++timerId; timers.set(id, { callback, ms }); return id;
    },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      requests.push({ url, ...options });
      return fetchResult ? fetchResult(url, options) : Promise.reject(new TypeError('synthetic-private-fetch'));
    },
    SpeechRecognition: class {
      constructor() { this.aborted = false; recognitions.push(this); }
      start() { this.onstart?.(); }
      abort() { this.aborted = true; }
      result(text) { const result = [{ transcript: text }]; result.isFinal = true; this.onresult?.({ resultIndex: 0, results: [result] }); }
    },
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    speechSynthesis: { synthesize() { speechCalls++; throw new Error('unexpected TTS'); } },
    SpeechAudioPlayer: class { constructor() { throw new Error('unexpected playback'); } },
    window: { close() { closed++; } },
  };
  for (const [key, value] of Object.entries(globals)) {
    saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const page = { ...pageDefinition, data: { ...pageDefinition.data }, setData(patch) { patches.push({ ...patch }); Object.assign(this.data, patch); } };
  page.onLoad(message === undefined ? {} : { message });
  t.after(() => {
    page.onUnload();
    Object.assign(config, oldConfig);
    for (const [key, descriptor] of Object.entries(saved)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  });
  function fireTimer(ms) {
    const item = [...timers].find(([, timer]) => timer.ms === ms);
    assert.ok(item, `missing ${ms}ms timer`);
    timers.delete(item[0]); item[1].callback();
  }
  function wakeup(key = false) {
    let prevented = 0;
    const event = { code: 'Enter', preventDefault() { prevented++; } };
    if (key) page.onKeyUp(event); else page.onVoiceWakeup(event);
    return prevented;
  }
  return { page, timers, requests, controllers, recognitions, patches, fireTimer, wakeup,
    get closed() { return closed; }, get speechCalls() { return speechCalls; } };
}

test('page starts speech before self-check timers fire and self-check cannot block the entry', (t) => {
  const f = fixture(t); f.page.onShow();
  assert.equal(f.page.data.buildTag, 'photo-1001p');
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.page.data.phase, 'listening');
  assert.match(f.page.data.selfCheck, /计时待验/);
  assert.equal(f.controllers.length, 1);
  assert.equal(f.controllers[0].signal.aborted, true);
  f.page.onShow();
  assert.equal(f.controllers.length, 1);
  assert.equal(f.recognitions.length, 1);
  f.fireTimer(200);
  assert.equal(f.page.data.selfCheck, '取消✓、计时✓');
  assert.equal([...f.timers.values()].some((timer) => [200, 3000].includes(timer.ms)), false);
});

test('self-check timer failures leave the regular speech entry running', (t) => {
  const f = fixture(t, { brokenTimer: true }); f.page.onShow();
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.page.data.phase, 'listening');
  assert.equal(f.page.data.selfCheck, '取消✓、计时未完成');
  assert.equal(f.page.selfCheckOwner, null);
});

test('page passes the imported wx module to capture without reading global wx', async (t) => {
  const originalMedia = Object.getOwnPropertyDescriptor(wx, 'media');
  const originalGlobal = Object.getOwnPropertyDescriptor(globalThis, 'wx');
  let captures = 0;
  Object.defineProperty(wx, 'media', { configurable: true, value: {
    createCameraContext() { return { takePhoto() {
      captures++;
      return Promise.resolve({ data: new Uint8Array([0xff, 0xd8, 0xff]).buffer, mimeType: 'image/jpeg' });
    } }; },
  } });
  Object.defineProperty(globalThis, 'wx', { configurable: true, get() { throw new Error('global wx must not be accessed'); } });
  t.after(() => {
    if (originalMedia) Object.defineProperty(wx, 'media', originalMedia); else delete wx.media;
    if (originalGlobal) Object.defineProperty(globalThis, 'wx', originalGlobal); else delete globalThis.wx;
  });
  const f = fixture(t, { message: '看看这个', fetch: async (url, options) => {
    if (url.endsWith('/device-result')) return new Promise(() => {});
    const body = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => ({ reply: '', request_id: body.request_id, session_id: body.session_id,
      action: { type: 'take_photo', id: '8793a3c1-bdf9-47b7-9f32-2963a8322faa', expires_in_ms: 60000 } }) };
  } });
  f.page.onShow();
  await flush();
  assert.equal(captures, 1);
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[0].url, 'https://fixture.invalid/v1/chat');
  assert.equal(f.requests[1].url, 'https://fixture.invalid/v1/device-result');
  assert.deepEqual(JSON.parse(f.requests[1].body).image, { data_base64: '/9j/', mime_type: 'image/jpeg' });
});

test('temporary key diagnostics are bounded and do not intercept unknown buttons or trigger upload', (t) => {
  const f = fixture(t); f.page.onShow();
  let prevented = 0;
  const event = { code: 'GlobalHook', preventDefault() { prevented++; },
    get key() { throw new Error('must not read key characters'); },
    get location() { throw new Error('must not read position'); } };
  f.page.onKeyDown(event);
  assert.equal(f.page.data.keyDiag, 'K:D/GlobalHook');
  f.page.onKeyUp(event);
  assert.equal(f.page.data.keyDiag, 'K:U/GlobalHook');
  assert.equal(prevented, 0);
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.requests.length, 0);
  f.page.onKeyDown({ code: 'Camera\n\u202e<>'.repeat(10) });
  assert.match(f.page.data.keyDiag, /^K:D\/[A-Za-z0-9_.:-]{1,24}$/);
  f.page.onKeyDown({ get code() { throw new Error('host getter unavailable'); } });
  assert.equal(f.page.data.keyDiag, 'K:D/-');
});

test('key diagnostic keeps Enter recovery and Backspace exit; hidden events do not update it', (t) => {
  const f = fixture(t); f.page.onShow();
  assert.equal(f.wakeup(true), 1);
  assert.equal(f.page.data.keyDiag, 'K:U/Enter');
  assert.equal(f.recognitions.length, 2);
  assert.equal(f.recognitions[0].aborted, true);
  let prevented = 0;
  f.page.onKeyUp({ code: 'Backspace', preventDefault() { prevented++; } });
  assert.equal(prevented, 0);
  assert.equal(f.page.shown, false);
  assert.equal(f.recognitions[1].aborted, true);
  assert.equal(f.page.data.keyDiag, 'K:U/Backspace');
  f.page.onKeyDown({ code: 'Camera' });
  f.page.onKeyUp({ code: 'Enter' });
  assert.equal(f.page.data.keyDiag, 'K:U/Backspace');
  assert.equal(f.recognitions.length, 2);
  f.page.onShow();
  assert.equal(f.page.data.keyDiag, '');
});

test('POST keeps a fresh signal while diagnostic GET is bare and does not reuse self-check options', async (t) => {
  const f = fixture(t, { message: '记住这件事', fetch: (_url, options) => {
    if (options?.method === 'POST') {
      assert.equal(options.signal.aborted, false);
      return Promise.reject(new TypeError('synthetic-private-fetch'));
    }
    assert.equal(options, undefined);
    return Promise.resolve({ status: 405, json() { throw new Error('must not read body'); } });
  } });
  f.page.onShow(); await flush();
  assert.equal(f.page.data.phase, 'paused');
  assert.equal(f.page.data.getDiag, '联网405');
  assert.equal(f.requests.length, 2);
  const [post, get] = f.requests;
  assert.equal(post.method, 'POST');
  assert.equal(post.url, config.endpoint); assert.equal(get.url, post.url);
  assert.deepEqual(Object.keys(get), ['url']);
  const selfCheckSignal = f.controllers.find((controller) => controller.signal.aborted).signal;
  assert.notEqual(post.signal, selfCheckSignal);
  assert.equal(f.controllers.length, 2, 'only the POST and the separate self-check create controllers');
  assert.equal(f.speechCalls, 0); assert.equal(f.recognitions.length, 0);
  assert.match(f.page.data.notice, /重点保存结果未确认/);
  assert.ok(f.page.data.diagnostic.startsWith('F1/TYPE/'));
  assert.equal(f.page.data.buildTag, 'photo-1001p', 'controller state updates cannot restore its older initial tag');
});

test('self-check owns both timers; hidden or unloaded callbacks cannot write or revive the page', (t) => {
  const f = fixture(t); f.page.onShow();
  const oldCallbacks = [...f.timers.values()].filter((timer) => [200, 3000].includes(timer.ms));
  f.page.onHide(); const patches = f.patches.length;
  for (const timer of oldCallbacks) timer.callback();
  assert.equal(f.patches.length, patches); assert.equal(f.timers.size, 0);
  f.page.onShow();
  assert.equal(f.recognitions.length, 2); assert.equal(f.controllers.length, 2);
  const newCallbacks = [...f.timers.values()].filter((timer) => [200, 3000].includes(timer.ms));
  f.page.onUnload(); const afterUnload = f.patches.length;
  for (const timer of newCallbacks) timer.callback();
  f.page.onShow();
  assert.equal(f.patches.length, afterUnload); assert.equal(f.recognitions.length, 2);
});

test('self-check fallback and success callbacks commit at most once', (t) => {
  const f = fixture(t); f.page.onShow();
  const success = [...f.timers.values()].find((timer) => timer.ms === 200).callback;
  f.fireTimer(3000);
  assert.equal(f.page.data.selfCheck, '取消✓、计时未完成');
  const patches = f.patches.length;
  success();
  assert.equal(f.patches.length, patches);
  assert.equal(f.page.selfCheckOwner, null);
});

test('a pending bare GET locally settles in 12 seconds; late HTTP cannot change it', async (t) => {
  const pending = deferred(); const f = fixture(t, { fetch: () => pending.promise });
  f.page.onShow();
  const running = f.page._runGetDiagnostic(); let settled = false;
  running.then(() => { settled = true; });
  f.fireTimer(12000); await flush();
  assert.equal(settled, true); assert.equal(await running, '联网超时');
  assert.deepEqual(f.requests, [{ url: config.endpoint }], 'no abort signal was passed to the underlying GET');
  assert.equal(f.page.getDiagOwner, null);
  assert.equal([...f.timers.values()].some((timer) => timer.ms === 12000), false);
  assert.equal(f.page.data.getDiag, '联网超时');
  const patches = f.patches.length;
  pending.resolve({ status: 405 }); await flush();
  assert.equal(f.patches.length, patches); assert.equal(f.page.data.getDiag, '联网超时');
  assert.equal(f.recognitions.length, 1); assert.equal(f.speechCalls, 0);
});

test('GET sync throws settle immediately, clear the deadline and hide sensitive error text', async (t) => {
  const f = fixture(t, { fetch: () => { throw new TypeError(`Authorization: Bearer ${config.token}`); } });
  f.page.onShow();
  assert.equal(await f.page._runGetDiagnostic(), '联网未完成：错误内容已隐藏');
  assert.equal(f.page.getDiagOwner, null);
  assert.equal([...f.timers.values()].some((timer) => timer.ms === 12000), false);
  assert.equal(JSON.stringify(f.page.data).includes(config.token), false);
  assert.equal(JSON.stringify(f.page.data).includes('Authorization'), false);
});

for (const action of ['hide', 'unload', 'finish', 'wakeup', 'enter', 'back']) {
  test(`GET cancellation on ${action} settles, clears timers and ignores queued callback/late result`, async (t) => {
    const pending = deferred(); const f = fixture(t, { fetch: () => pending.promise });
    f.page.onShow();
    const selfCallbacks = [...f.timers.values()].filter((timer) => [200, 3000].includes(timer.ms));
    const running = f.page._runGetDiagnostic();
    const deadline = [...f.timers.values()].find((timer) => timer.ms === 12000).callback;
    if (action === 'hide') f.page.onHide();
    else if (action === 'unload') f.page.onUnload();
    else if (action === 'finish') f.recognitions[0].result('退出');
    else if (action === 'back') f.page.onKeyUp({ code: 'Backspace' });
    else assert.equal(f.wakeup(action === 'enter'), 1);
    assert.equal(await running, null);
    assert.deepEqual(f.requests, [{ url: config.endpoint }], 'local cancellation does not claim to abort the bare GET');
    assert.equal(f.page.getDiagOwner, null); assert.equal(f.page.selfCheckOwner, null);
    if (action === 'wakeup' || action === 'enter') {
      assert.equal(f.page.data.getDiag, ''); assert.equal(f.page.data.selfCheck, '');
    }
    assert.equal([...f.timers.values()].some((timer) => [200, 3000, 12000].includes(timer.ms)), false);
    const patches = f.patches.length, asrCount = f.recognitions.length;
    deadline(); for (const timer of selfCallbacks) timer.callback();
    pending.resolve({ status: 405 }); await flush();
    assert.equal(f.patches.length, patches); assert.equal(f.recognitions.length, asrCount);
    assert.equal(f.speechCalls, 0); assert.equal(f.requests.length, 1);
    if (action === 'finish') {
      assert.equal(f.closed, 1); f.page.onShow(); assert.equal(f.recognitions.length, asrCount);
    }
  });
}

test('GET quota is consumed on start and stays consumed after wakeup; only actual hide/show renews it', async (t) => {
  const pending = deferred(); let getCount = 0;
  const f = fixture(t, { fetch: (_url, options) => {
    if (options?.method === 'POST') return Promise.reject(new TypeError('synthetic-private-fetch'));
    return ++getCount === 1 ? pending.promise : Promise.resolve({ status: 405 });
  } });
  f.page.onShow();
  f.recognitions.at(-1).result('第一句'); await flush();
  assert.equal(f.page.data.phase, 'paused'); assert.equal(getCount, 1);
  assert.equal(await f.page._runGetDiagnostic(), null);
  f.page.onShow(); f.wakeup();
  f.recognitions.at(-1).result('第二句'); await flush();
  assert.equal(getCount, 1);
  f.page.onHide(); f.page.onShow();
  f.recognitions.at(-1).result('第三句'); await flush();
  assert.equal(getCount, 2); assert.equal(f.page.data.getDiag, '联网405');
  const patches = f.patches.length;
  pending.resolve({ status: 401 }); await flush();
  assert.equal(f.patches.length, patches); assert.equal(f.page.data.getDiag, '联网405');
  assert.equal(f.requests.filter((request) => request.method === 'POST').length, 3);
  assert.equal(f.speechCalls, 0);
});

for (const result of [405, 401, 999, '405', 'reject']) {
  test(`GET result ${result} is passive and never changes the chat failure or restarts audio`, async (t) => {
    const f = fixture(t, { message: '普通问题', fetch: (_url, options) => {
      if (options?.method === 'POST' || result === 'reject') return Promise.reject(new Error('synthetic-private-fetch'));
      return Promise.resolve({ status: result, json() { throw new Error('must not read body'); } });
    } });
    f.page.onShow(); await flush();
    assert.equal(f.page.data.phase, 'paused');
    assert.match(f.page.data.notice, /连接或等待回复/);
    assert.equal(f.page.data.getDiag, [405, 401].includes(result) ? `联网${result}`
      : result === 'reject' ? '联网未完成：synthetic-private-fetch' : '联网未完成');
    assert.equal(f.recognitions.length, 0); assert.equal(f.speechCalls, 0);
    assert.equal(f.requests.length, 2);
  });
}
