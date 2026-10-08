import test from 'node:test';
import assert from 'node:assert/strict';
import pageDefinition from '../pages/memory/index.js';
import { createAssistant, explicitRemember, isExitCommand, validateConfig, formatRequestDiagnostic } from '../lib/assistant.js';

const config = { endpoint: 'https://personal.example/v1/chat', token: 'test-only', sessionId: 'voice-memory' };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function fixture(overrides = {}, chosenConfig = config) {
  const state = {};
  const requests = [];
  const aborts = [];
  const timers = new Map();
  const recognitions = [];
  const tasks = [];
  const players = [];
  let timerId = 0;
  let closed = 0;
  const runtime = {
    fetch: async (url, options) => { requests.push({ url, ...options }); return { ok: true, json: async () => ({ reply: '这是后端的实际回复。' }) }; },
    createAbortHandle: () => {
      const controller = new AbortController();
      const handle = { signal: controller.signal, abort() { controller.abort(); } };
      aborts.push(handle);
      return handle;
    },
    createRecognition: () => {
      const recognition = {
        started: false, aborted: false,
        start() { this.started = true; this.onstart?.(); },
        abort() { this.aborted = true; },
        result(text, final = true) {
          const result = [{ transcript: text }]; result.isFinal = final;
          this.onresult?.({ resultIndex: 0, results: [result] });
        },
      };
      recognitions.push(recognition);
      return recognition;
    },
    createUtterance: (text) => ({ text }),
    synthesize: async () => {
      const task = { aborted: false, abort() { this.aborted = true; }, finished: Promise.resolve({ duration: 1 }) };
      tasks.push(task);
      return task;
    },
    createPlayer: () => {
      const audioPlayer = {
        ended: new Set(), errors: new Set(),
        onEnded(cb) { this.ended.add(cb); }, offEnded(cb) { this.ended.delete(cb); },
        onError(cb) { this.errors.add(cb); }, offError(cb) { this.errors.delete(cb); },
      };
      const player = {
        audioPlayer, played: false, stopped: false, destroyed: false,
        play() { assert.equal(audioPlayer.ended.size, 1); this.played = true; },
        stop() { this.stopped = true; for (const cb of audioPlayer.ended) cb(); },
        destroy() { this.destroyed = true; },
        end() { for (const cb of [...audioPlayer.ended]) cb(); },
        error() { for (const cb of [...audioPlayer.errors]) cb(); },
      };
      players.push(player);
      return player;
    },
    setTimeout: (callback, ms) => { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    finish: () => { closed += 1; },
    ...overrides,
  };
  const assistant = createAssistant({ config: chosenConfig, runtime, update: (patch) => Object.assign(state, patch) });
  const fireTimer = (ms) => {
    const item = [...timers].find(([, timer]) => timer.ms === ms);
    assert.ok(item, `missing ${ms}ms timer`);
    timers.delete(item[0]); item[1].callback();
  };
  return { assistant, state, requests, aborts, timers, recognitions, tasks, players, fireTimer, get closed() { return closed; } };
}

async function completeReply(f) {
  await flush();
  f.players.at(-1).end();
  await flush();
}

const actionId = '8793a3c1-bdf9-47b7-9f32-2963a8322faa';
const photo = () => ({ data: new Uint8Array([0xff, 0xd8, 0xff]).buffer, mimeType: 'image/jpeg' });
const response = data => ({ ok: true, status: 200, json: async () => data });
const cameraAction = (body, overrides = {}) => ({
  reply: '', session_id: body.session_id, request_id: body.request_id,
  action: { type: 'take_photo', id: actionId, expires_in_ms: 60000 }, ...overrides,
});
function actionFixture(overrides = {}, chosenConfig = config) {
  const requests = [];
  let clock = 0;
  const f = fixture({ now: () => clock, takePhoto: async () => photo(), ...overrides,
    fetch: (url, options) => {
      requests.push({ url, ...options });
      const body = JSON.parse(options.body);
      if (overrides.fetch) return overrides.fetch(url, options, body);
      return Promise.resolve(response(url.endsWith('/chat') ? cameraAction(body) : { reply: '看到了杯子。', request_id: body.request_id, session_id: body.session_id }));
    },
  }, chosenConfig);
  f.requests = requests;
  f.setNow = value => { clock = value; };
  return f;
}

test('any ASR words go to chat first; text alone never starts a camera', async () => {
  for (const message of ['拍照', '拍照告诉我，眼前是什么', '看看这个', '拍照记住这是我的杯子', '不要拍照', '拍照但不要上传', '这个功能怎么用']) {
    let captures = 0;
    const f = fixture({ takePhoto: async () => { captures++; return photo(); } });
    f.assistant.show();
    f.recognitions[0].result(message);
    await flush();
    assert.equal(captures, 0, message);
    assert.equal(f.requests.length, 1, message);
    assert.equal(f.requests[0].url, config.endpoint);
    const body = JSON.parse(f.requests[0].body);
    assert.equal(body.message, message);
    assert.deepEqual(body.capabilities, { camera: true });
    assert.equal('image' in body, false);
    f.assistant.hide();
  }
});

test('server action turns arbitrary ASR into one photo result in the original request and session', async () => {
  for (const message of ['拍照告诉我，眼前是什么', '看看这个', '记住：这是我的新杯子。']) {
    let captures = 0;
    const f = actionFixture({ takePhoto: async () => { captures++; return photo(); } });
    f.assistant.show();
    const asr = f.recognitions[0], oldResult = asr.onresult;
    asr.result(message);
    oldResult({ results: [Object.assign([{ transcript: message }], { isFinal: true })] });
    await flush();
    assert.equal(captures, 1, message);
    assert.equal(f.requests.length, 2);
    assert.equal(f.requests[0].url, config.endpoint);
    assert.equal(f.requests[1].url, 'https://personal.example/v1/device-result');
    const chat = JSON.parse(f.requests[0].body), result = JSON.parse(f.requests[1].body);
    assert.equal(chat.message, message);
    assert.equal(chat.remember, explicitRemember(message));
    assert.deepEqual(result, { session_id: chat.session_id, request_id: chat.request_id,
      action_id: actionId, image: { data_base64: '/9j/', mime_type: 'image/jpeg' }, status: 'ok' });
    assert.equal(f.requests[1].headers.Authorization, 'Bearer test-only');
    await completeReply(f); f.fireTimer(350);
    assert.equal(f.recognitions.length, 2);
    f.assistant.hide();
  }
});

test('invalid, unknown or mismatched actions never invoke the camera or send a result', async () => {
  const malformed = [
    value => { value.action.type = 'record_video'; },
    value => { value.action.id = 'not-a-uuid'; },
    value => { value.action.id = null; },
    value => { value.action = []; },
    value => { value.request_id = '8a1b4d21-e1c9-477c-8bb2-af055518452d'; },
    value => { value.session_id = 'another-session'; },
    value => { delete value.request_id; },
    value => { value.reply = 'unexpected simultaneous reply'; },
    ...[0, -1, 60001, 1.5, '60000'].map(ttl => value => { value.action.expires_in_ms = ttl; }),
  ];
  for (const mutate of malformed) {
    let captures = 0;
    const f = actionFixture({ takePhoto: async () => { captures++; return photo(); },
      fetch: async (_url, _options, body) => { const data = cameraAction(body); mutate(data); return response(data); },
    });
    await f.assistant.show('看看这个');
    assert.equal(captures, 0);
    assert.equal(f.requests.length, 1);
    assert.equal(f.state.phase, 'paused');
    assert.equal(f.players.length, 0);
  }
});

test('a consumed action ID cannot take a second photo in the same visible session', async () => {
  let captures = 0;
  const f = actionFixture({ takePhoto: async () => { captures++; return photo(); } });
  f.assistant.show('看看这个'); await completeReply(f); f.fireTimer(350);
  f.recognitions[0].result('再看看'); await flush();
  assert.equal(captures, 1);
  assert.equal(f.requests.length, 3);
  assert.equal(f.state.phase, 'paused');
});

test('device result accepts only a final reply, never another action or mismatched result', async () => {
  for (const followup of [
    body => cameraAction(body),
    body => ({ ...cameraAction(body), reply: '还要拍一次' }),
    body => ({ reply: '错会话', session_id: 'wrong', request_id: body.request_id }),
    body => ({ reply: '错请求', session_id: body.session_id, request_id: 'wrong' }),
  ]) {
    let captures = 0;
    const f = actionFixture({ takePhoto: async () => { captures++; return photo(); },
      fetch: async (url, _options, body) => response(url.endsWith('/chat') ? cameraAction(body) : followup(body)),
    });
    await f.assistant.show('看看这个');
    assert.equal(captures, 1); assert.equal(f.requests.length, 2);
    assert.equal(f.players.length, 0); assert.equal(f.state.phase, 'paused');
  }
});

test('camera and encoding failures report only fixed error codes to the server', async () => {
  const cases = [
    ...['permission', 'interaction', 'capture', 'cancelled', 'timeout'].map(code => [code, async () => { throw { photoKind: code, message: 'private host detail' }; }]),
    ['unavailable', undefined],
    ['capture', async () => { throw new Error('private host detail'); }],
    ['format', async () => ({ ...photo(), mimeType: 'image/webp' })],
    ['invalid', async () => ({ data: new ArrayBuffer(0), mimeType: 'image/jpeg' })],
    ['size', async () => ({ data: new ArrayBuffer(6 * 1024 * 1024 + 1), mimeType: 'image/jpeg' })],
  ];
  for (const [code, takePhoto] of cases) {
    const f = actionFixture({ takePhoto });
    f.assistant.show('看看这个'); await flush();
    const body = JSON.parse(f.requests[1].body);
    assert.equal(body.status, 'error'); assert.equal(body.error_code, code);
    assert.equal('image' in body, false);
    assert.deepEqual(Object.keys(body).sort(), ['action_id', 'error_code', 'request_id', 'session_id', 'status']);
    assert.doesNotMatch(JSON.stringify(f.requests) + JSON.stringify(f.state), /private host detail/);
    assert.equal(f.state.answer, '看到了杯子。'); // Only the server's actual final reply is shown.
    f.assistant.hide();
  }
});

test('hide during camera capture aborts and discards late photos without device-result', async () => {
  const pending = deferred(); let signal;
  const f = actionFixture({ takePhoto: value => { signal = value; return pending.promise; } });
  const running = f.assistant.show('看看这个'); await flush();
  assert.equal(f.state.phase, 'capturing');
  f.assistant.hide();
  assert.equal(signal.aborted, true); assert.equal(f.timers.size, 0);
  assert.equal(await running, false);
  pending.resolve(photo()); await flush();
  assert.equal(f.requests.length, 1); assert.equal(f.players.length, 0);
});

test('a late action after exit or wakeup cannot access the camera', async () => {
  for (const cancel of ['finish', 'wakeup']) {
    const pending = deferred(); let chat, captures = 0;
    const f = actionFixture({ takePhoto: async () => { captures++; return photo(); },
      fetch: (_url, _options, body) => { chat = body; return pending.promise; },
    });
    const running = f.assistant.show('看看这个');
    f.assistant[cancel]();
    pending.resolve(response(cameraAction(chat))); await running;
    assert.equal(captures, 0); assert.equal(f.requests.length, 1); assert.equal(f.players.length, 0);
    f.assistant.hide();
  }
});

test('action lifetime includes JSON decoding delay and rejects expiry before capture', async () => {
  const pending = deferred(); let chat, captures = 0;
  const f = actionFixture({ takePhoto: async () => { captures++; return photo(); },
    fetch: async (_url, _options, body) => { chat = body; return { ok: true, status: 200, json: () => pending.promise }; },
  });
  const running = f.assistant.show('看看这个'); await flush();
  f.setNow(60000); pending.resolve(cameraAction(chat));
  await running;
  assert.equal(captures, 0); assert.equal(f.requests.length, 1);
  assert.match(f.state.status, /过期/); assert.equal(f.timers.size, 0);
});

test('expired capture ends a host that ignores abort and never uploads its late photo', async () => {
  const pending = deferred(); let signal;
  const f = actionFixture({ takePhoto: value => { signal = value; return pending.promise; },
    fetch: async (_url, _options, body) => response(cameraAction(body, { action: { type: 'take_photo', id: actionId, expires_in_ms: 1000 } })),
  });
  const running = f.assistant.show('看看这个'); await flush();
  f.setNow(1000); f.fireTimer(1000);
  assert.equal(await running, false);
  assert.equal(signal.aborted, true); assert.equal(f.requests.length, 1);
  assert.match(f.state.status, /过期/);
  pending.resolve(photo()); await flush();
  assert.equal(f.requests.length, 1); assert.equal(f.players.length, 0); assert.equal(f.timers.size, 0);
});

test('camera watchdog reports timeout within the action lifetime and ignores a later photo', async () => {
  const pending = deferred(); let signal;
  const f = actionFixture({ takePhoto: value => { signal = value; return pending.promise; } });
  f.assistant.show('看看这个'); await flush();
  f.setNow(30000); f.fireTimer(30000); await flush();
  assert.equal(signal.aborted, true);
  assert.equal(f.requests.length, 2);
  assert.equal(JSON.parse(f.requests[1].body).error_code, 'timeout');
  pending.resolve(photo()); await flush();
  assert.equal(f.requests.length, 2);
  await completeReply(f); f.assistant.hide();
});

test('camera watchdog reports timeout even when native abort immediately rejects as cancelled', async () => {
  const f = actionFixture({ takePhoto: signal => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject({ photoKind: 'cancelled' }), { once: true });
  }) });
  f.assistant.show('看看这个'); await flush();
  f.setNow(30000); f.fireTimer(30000); await flush();
  assert.equal(JSON.parse(f.requests[1].body).error_code, 'timeout');
  f.assistant.hide();
});

test('total deadline is not renewed by action/capture and ends even a nonsettling host', async () => {
  const pending = deferred();
  const f = actionFixture({ takePhoto: () => pending.promise });
  const running = f.assistant.show('看看这个');
  const originalTimer = [...f.timers].find(([, timer]) => timer.ms === 120000);
  await flush();
  assert.equal(f.timers.get(originalTimer[0]), originalTimer[1]);
  f.setNow(120000); f.fireTimer(120000);
  assert.equal(await running, false);
  assert.equal(f.requests.length, 1); assert.equal(f.state.phase, 'paused');
  assert.match(f.state.status, /超时/); assert.equal(f.timers.size, 0);
  pending.resolve(photo()); await flush(); assert.equal(f.requests.length, 1);
});

test('exit during device-result discards its final reply and does not reopen microphone', async () => {
  const pending = deferred();
  const f = actionFixture({ fetch: async (url, _options, body) => url.endsWith('/chat') ? response(cameraAction(body)) : pending.promise });
  const running = f.assistant.show('看看这个'); await flush();
  assert.equal(f.requests.length, 2);
  f.assistant.finish();
  assert.equal(await running, false);
  pending.resolve(response({ reply: '迟到照片回复' })); await flush();
  assert.equal(f.players.length, 0); assert.equal(f.recognitions.length, 0); assert.equal(f.timers.size, 0);
});

test('foreground entry starts one ASR; repeated show does not restart it', () => {
  const f = fixture();
  f.assistant.show(); f.assistant.show();
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.recognitions[0].started, true);
  assert.equal(f.recognitions[0].continuous, false);
  assert.equal(f.state.phase, 'listening');
});

test('final ASR submits once without waiting for onend; partial text never uploads', async () => {
  const f = fixture(); f.assistant.show();
  const asr = f.recognitions[0];
  const oldResult = asr.onresult; const oldEnd = asr.onend;
  asr.result('我正在说', false);
  assert.equal(f.requests.length, 0);
  asr.result('上次那个决定为什么这样做？');
  oldResult({ results: [Object.assign([{ transcript: '重复' }], { isFinal: true })] }); oldEnd();
  assert.equal(asr.aborted, true);
  assert.equal(f.requests.length, 1);
  const body = JSON.parse(f.requests[0].body);
  assert.equal(body.message, '上次那个决定为什么这样做？');
  assert.equal(body.remember, false);
  assert.equal(body.session_id, 'voice-memory');
  assert.equal('user_id' in body, false);
  assert.match(body.request_id, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  await completeReply(f);
});

test('HTTP and synthesis completion do not reopen mic; actual playback end does once', async () => {
  const f = fixture();
  const running = f.assistant.show('问题');
  await flush();
  assert.equal(f.recognitions.length, 0);
  assert.equal(f.state.phase, 'speaking');
  await f.tasks[0].finished;
  assert.equal(f.recognitions.length, 0);
  assert.equal(await f.assistant.submit('重复'), false);
  const oldEnded = [...f.players[0].audioPlayer.ended][0];
  f.players[0].end(); oldEnded();
  await running;
  assert.equal(f.players[0].destroyed, true);
  assert.equal(f.recognitions.length, 0);
  f.fireTimer(350);
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.state.phase, 'listening');
});

test('voice turns preserve ordinary messages and flag only explicit memory emphasis', async () => {
  const f = fixture(); f.assistant.show();
  f.recognitions[0].result('记住：测试标记是青石。');
  await completeReply(f); f.fireTimer(350);
  f.recognitions[1].result('你记住了吗？');
  await completeReply(f); f.fireTimer(350);
  f.recognitions[2].result('我今天准备继续整理这个项目。');
  await completeReply(f); f.fireTimer(350);
  assert.equal(f.recognitions.length, 4);
  assert.deepEqual(f.requests.map((r) => JSON.parse(r.body).remember), [true, false, false]);
  const ordinary = JSON.parse(f.requests[2].body);
  assert.equal(ordinary.message, '我今天准备继续整理这个项目。');
  assert.equal(ordinary.session_id, 'voice-memory');
  // This is only an emphasis flag; the backend owns ordinary memory/summarization.
  assert.deepEqual(Object.keys(ordinary).sort(), ['capabilities', 'message', 'remember', 'request_id', 'session_id']);
});

test('hide aborts HTTP and a late answer cannot enter the new foreground session', async () => {
  const waiting = deferred();
  const f = fixture({ fetch: () => waiting.promise });
  const result = f.assistant.show('记住这件事');
  f.assistant.hide();
  assert.equal(f.aborts[0].signal.aborted, true);
  assert.match(f.state.notice, /结果未确认/);
  f.assistant.show();
  waiting.resolve({ ok: true, json: async () => ({ reply: '迟到的回复' }) });
  await result;
  assert.equal(f.state.answer, '');
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.state.phase, 'listening');
  assert.equal(f.players.length, 0);
});

test('hide cancels ASR, listeners and pending listening timers', async () => {
  const f = fixture(); f.assistant.show();
  const old = f.recognitions[0]; const result = old.onresult;
  f.assistant.hide();
  assert.equal(old.aborted, true);
  result({ results: [Object.assign([{ transcript: '迟到' }], { isFinal: true })] });
  assert.equal(f.requests.length, 0);
  assert.equal(f.timers.size, 0);
  f.assistant.show('问题'); await completeReply(f);
  assert.equal(f.timers.size, 1);
  const delayed = [...f.timers.values()][0].callback;
  f.assistant.hide(); delayed();
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.timers.size, 0);
});

test('hide during synthesis aborts late tasks without playing', async () => {
  const pending = deferred();
  const f = fixture({ synthesize: () => pending.promise });
  const running = f.assistant.show('问题'); await flush();
  f.assistant.hide();
  const task = { aborted: false, abort() { this.aborted = true; }, finished: Promise.resolve() };
  pending.resolve(task); await running; await flush();
  assert.equal(task.aborted, true);
  assert.equal(f.players.length, 0);
  assert.equal(f.timers.size, 0);
});

test('hide during playback unregisters before stop and ignores late ended events', async () => {
  const f = fixture(); const running = f.assistant.show('问题'); await flush();
  const player = f.players[0]; const ended = [...player.audioPlayer.ended][0];
  f.assistant.hide(); ended(); await running;
  assert.equal(player.stopped, true); assert.equal(player.destroyed, true);
  assert.equal(player.audioPlayer.ended.size, 0);
  assert.equal(f.recognitions.length, 0); assert.equal(f.timers.size, 0);
});

test('explicit voice wakeup interrupts playback and starts one new owned ASR', async () => {
  const f = fixture(); const running = f.assistant.show('问题'); await flush();
  const oldEnded = [...f.players[0].audioPlayer.ended][0];
  f.assistant.wakeup();
  const oldAsr = f.recognitions[0];
  f.assistant.wakeup(); oldEnded(); await running;
  assert.equal(oldAsr.aborted, true);
  assert.equal(f.players[0].destroyed, true);
  assert.equal(f.recognitions.filter((asr) => !asr.aborted).length, 1);
  assert.equal(f.state.phase, 'listening');
});

test('three silent rounds have backoff and stop; no empty request is sent', () => {
  const f = fixture(); f.assistant.show();
  f.recognitions[0].onend(); f.fireTimer(1000);
  f.recognitions[1].onend(); f.fireTimer(2000);
  f.recognitions[2].onend();
  assert.equal(f.state.active, false);
  assert.equal(f.requests.length, 0);
  assert.equal(f.timers.size, 0);
  f.assistant.wakeup();
  assert.equal(f.state.active, true); assert.equal(f.recognitions.length, 4);
});

test('ASR timeout is bounded; activation and permission errors stop with voice fallback', () => {
  const f = fixture(); f.assistant.show(); f.fireTimer(30000);
  assert.equal(f.recognitions[0].aborted, true);
  assert.equal(f.state.phase, 'waiting');
  f.fireTimer(1000);
  f.recognitions[1].onerror({ error: 'not-allowed' });
  assert.match(f.state.status, /乐奇/);
  assert.equal(f.state.active, false);
  assert.equal(f.timers.size, 0);
});

test('a host returning an asynchronously rejected ASR start is handled', async () => {
  let aborted = false;
  const f = fixture({ createRecognition: () => ({
    start() { return Promise.reject(new Error('not-allowed')); },
    abort() { aborted = true; },
  }) });
  f.assistant.show(); await flush();
  assert.equal(aborted, true);
  assert.equal(f.state.active, false);
  assert.equal(f.timers.size, 0);
  assert.match(f.state.status, /乐奇/);
});

test('network timeout pauses without resending an explicit remember request; 401 stops the loop', async () => {
  let calls = 0;
  const f = fixture({ fetch: async () => { calls++; throw new Error('timeout'); } });
  await f.assistant.show('记住这件事');
  assert.match(f.state.notice, /结果未确认/);
  assert.equal(f.state.phase, 'paused');
  assert.equal(f.timers.size, 0);
  assert.equal(f.recognitions.length, 0);
  f.assistant.wakeup();
  assert.equal(calls, 1); assert.equal(f.recognitions.length, 1);
  const rejected = fixture({ fetch: async () => ({ ok: false, status: 401 }) });
  await rejected.assistant.show('问题');
  assert.equal(rejected.state.active, false); assert.equal(rejected.timers.size, 0);
});

test('configured request deadline aborts a pending HTTP request and ends the turn without resending', async () => {
  const pending = deferred(); let signal, calls = 0;
  const timeoutMs = 1234;
  const f = fixture({ fetch: (_url, options) => {
    calls++; signal = options.signal;
    signal.addEventListener('abort', () => pending.reject(new DOMException('Request aborted', 'AbortError')), { once: true });
    return pending.promise;
  } }, { ...config, timeoutMs });
  const running = f.assistant.show('问题');
  assert.equal(signal.aborted, false);
  f.fireTimer(timeoutMs);
  assert.equal(signal.aborted, true);
  assert.equal(await running, false);
  assert.notEqual(f.state.phase, 'thinking');
  assert.equal(calls, 1);
  assert.equal(f.players.length, 0);
  assert.equal([...f.timers.values()].some((timer) => timer.ms === timeoutMs), false);
  f.assistant.destroy();
});

test('request deadline remains the same while response JSON is pending and aborts body reading', async () => {
  const body = deferred(); let signal, reading = false;
  const timeoutMs = 1234;
  const f = fixture({ fetch: async (_url, options) => {
    signal = options.signal;
    signal.addEventListener('abort', () => body.reject(new DOMException('Body reading aborted', 'AbortError')), { once: true });
    return { ok: true, status: 200, json() { reading = true; return body.promise; } };
  } }, { ...config, timeoutMs });
  const running = f.assistant.show('问题');
  const originalTimer = [...f.timers].find(([, timer]) => timer.ms === timeoutMs);
  assert.ok(originalTimer, 'request must install its configured deadline');
  await flush();
  assert.equal(reading, true);
  assert.equal(signal.aborted, false);
  assert.equal(f.timers.get(originalTimer[0]), originalTimer[1], 'receiving HTTP must not replace the original deadline');
  f.fireTimer(timeoutMs);
  assert.equal(signal.aborted, true);
  assert.equal(await running, false);
  assert.notEqual(f.state.phase, 'thinking');
  assert.equal(f.players.length, 0);
  f.assistant.destroy();
});

test('request deadline rejects late JSON even when body reading ignores abort', async () => {
  const body = deferred(); let signal;
  const timeoutMs = 1234;
  const f = fixture({ fetch: async (_url, options) => {
    signal = options.signal;
    return { ok: true, status: 200, json: () => body.promise };
  } }, { ...config, timeoutMs });
  const running = f.assistant.show('问题');
  await flush();
  f.fireTimer(timeoutMs);
  assert.equal(signal.aborted, true);
  body.resolve({ reply: '超时后才到达的回复' });
  assert.equal(await running, false);
  assert.equal(f.state.phase, 'paused');
  assert.match(f.state.notice, /超时/);
  assert.match(f.state.status, /超时/);
  assert.equal(f.state.answer, '');
  assert.equal(f.tasks.length, 0);
  assert.equal(f.players.length, 0);
  assert.equal(f.timers.size, 0);
});

test('exit clears request deadline and ignores queued timeout callbacks and late HTTP or JSON results', async () => {
  for (const phase of ['http', 'json']) {
    const pending = deferred(); const timeoutMs = 1234;
    const response = { ok: true, status: 200, json: async () => ({ reply: '迟到的回复' }) };
    const f = fixture({ fetch: () => phase === 'http' ? pending.promise : Promise.resolve({ ...response, json: () => pending.promise }) }, { ...config, timeoutMs });
    const running = f.assistant.show('问题');
    await flush();
    const deadline = [...f.timers.values()].find((timer) => timer.ms === timeoutMs);
    assert.ok(deadline, `${phase} wait must retain the request deadline`);
    f.assistant.finish();
    const ended = { ...f.state };
    assert.equal(f.aborts[0].signal.aborted, true);
    assert.equal(f.timers.size, 0);
    deadline.callback();
    pending.resolve(phase === 'http' ? response : { reply: '迟到的回复' });
    assert.equal(await running, false);
    assert.deepEqual(f.state, ended);
    assert.equal(f.players.length, 0);
    assert.equal(f.timers.size, 0);
  }
});

test('valid reply clears request deadline before speech playback completes', async () => {
  const timeoutMs = 1234;
  const f = fixture({}, { ...config, timeoutMs });
  const running = f.assistant.show('问题');
  assert.equal([...f.timers.values()].filter((timer) => timer.ms === timeoutMs).length, 1);
  await flush();
  assert.equal(f.state.phase, 'speaking');
  assert.equal(f.aborts[0].signal.aborted, false);
  assert.equal([...f.timers.values()].some((timer) => timer.ms === timeoutMs), false);
  assert.equal(f.players[0].played, true);
  assert.equal(f.players[0].stopped, false);
  assert.ok(f.timers.size > 0, 'the speech watchdog may remain after the HTTP deadline is cleared');
  f.players[0].end();
  assert.equal(await running, true);
  f.assistant.destroy();
});

test('connection notices identify safe request, HTTP and response stages without exposing error data', async () => {
  const secret = 'private-error-body-or-token';
  const cases = [
    [async () => { throw new Error(secret); }, /连接或等待回复/],
    [async () => ({ ok: false, status: 502, json() { throw new Error(secret); } }), /HTTP 502/],
    [async () => ({ ok: false, status: secret }), /HTTP 状态无效/],
    [async () => ({ ok: true, status: 200, json: async () => { throw new Error(secret); } }), /响应解析失败/],
    [async () => ({ ok: true, status: 200, json: async () => ({ reply: '', body: secret }) }), /响应解析失败/],
  ];
  for (const [fetch, expected] of cases) {
    const f = fixture({ fetch });
    await f.assistant.show('问题');
    assert.match(f.state.notice, expected);
    assert.equal(JSON.stringify(f.state).includes(secret), false);
    assert.equal(f.players.length, 0);
  }
});

test('connection notice survives ASR errors and a new request until a valid current reply arrives', async () => {
  const nextReply = deferred(); let attempts = 0;
  const f = fixture({ fetch: () => ++attempts === 1 ? Promise.reject(new Error('timeout')) : nextReply.promise });
  await f.assistant.show('问题');
  const notice = f.state.notice;
  assert.equal(f.state.phase, 'paused');
  assert.equal(f.timers.size, 0);
  f.assistant.wakeup();
  f.recognitions[0].onerror({ error: 'not-allowed' });
  assert.match(f.state.status, /未能开启语音/);
  assert.equal(f.state.notice, notice);
  f.assistant.wakeup();
  f.recognitions[1].result('下一句问题');
  assert.equal(f.state.phase, 'thinking');
  assert.equal(f.state.notice, notice);
  nextReply.resolve({ ok: true, status: 200, json: async () => ({ reply: '有效回复' }) });
  await flush();
  assert.equal(f.state.notice, '');
  assert.equal(f.state.answer, '有效回复');
  await completeReply(f);
});

test('a cancelled request cannot clear the current connection failure notice with a late success', async () => {
  const oldReply = deferred(); let attempts = 0;
  const f = fixture({ fetch: () => ++attempts === 1 ? oldReply.promise : Promise.reject(new Error('timeout')) });
  const oldTurn = f.assistant.show('旧问题');
  f.assistant.wakeup(); f.recognitions[0].result('新问题');
  await flush(); const notice = f.state.notice;
  assert.match(notice, /连接或等待回复/);
  oldReply.resolve({ ok: true, json: async () => ({ reply: '迟到的旧回复' }) });
  await oldTurn;
  assert.equal(f.state.notice, notice);
  assert.equal(f.players.length, 0);
});

test('TTS error is visible, cleans its player and returns with backoff', async () => {
  const f = fixture(); const running = f.assistant.show('问题'); await flush();
  f.players[0].error(); await running;
  assert.match(f.state.status, /播报失败/);
  assert.equal(f.players[0].destroyed, true);
  assert.equal(f.recognitions.length, 0);
  f.fireTimer(1000); assert.equal(f.recognitions.length, 1);
});

test('abort-constructor errors are caught and do not leave a hidden busy state', async () => {
  const f = fixture({ createAbortHandle() { throw new Error('missing API'); } });
  await f.assistant.show('问题');
  assert.equal(f.state.phase, 'paused');
  assert.equal(f.requests.length, 0);
  assert.equal(f.state.notice, '连接初始化失败，请检查眼镜运行时版本。');
  assert.equal(JSON.stringify(f.state).includes('missing API'), false);
});

test('exit is a complete voice command; it stops locally before asking host to close', () => {
  const f = fixture(); f.assistant.show();
  f.recognitions[0].result('退出。');
  assert.equal(f.closed, 1); assert.equal(f.state.active, false);
  assert.equal(f.recognitions[0].aborted, true);
  assert.equal(f.timers.size, 0); assert.equal(f.requests.length, 0);
  assert.equal(isExitCommand('退出是什么意思'), false);
  assert.equal(isExitCommand('请记住退出暗号'), false);
});

test('explicit exit is terminal despite wakeups, late ASR events and host hide/show before unload', () => {
  const f = fixture(); f.assistant.show();
  const asr = f.recognitions[0];
  const lateError = asr.onerror, lateStart = asr.onstart, lateEnd = asr.onend;
  asr.result('退出');
  assert.equal(f.assistant.wakeup(), false);
  assert.equal(f.assistant.show(), false);
  lateError({ error: 'not-allowed' }); lateStart(); lateEnd();
  f.assistant.hide();
  assert.equal(f.assistant.show(), false);
  f.assistant.finish();
  assert.equal(f.state.status, '本次对话已结束。');
  assert.equal(f.recognitions.length, 1);
  assert.equal(f.closed, 1);
  assert.equal(f.timers.size, 0);
});

test('page reentry consumes initial question only once and wakeup suppresses host default', () => {
  const calls = []; let prevented = 0;
  const page = { ...pageDefinition, setData() {}, _runSelfCheck() {}, shown: false, initialQueryConsumed: false, initialQuery: { message: '第一句' }, assistant: {
    show: (message) => calls.push(['show', message]), wakeup: () => calls.push(['wakeup']), hide: () => calls.push(['hide']), destroy: () => {},
  } };
  page.onShow(); page.onShow(); page.onHide(); page.onShow();
  page.onVoiceWakeup({ keyword: 'clickAiAssist', preventDefault() { prevented++; } });
  assert.deepEqual(calls, [['show', '第一句'], ['hide'], ['show', ''], ['wakeup']]);
  assert.equal(prevented, 1);
});

test('page leaves host wakeups and Enter alone while hidden, closed or unloaded', () => {
  let attempted = 0, prevented = 0;
  const event = { code: 'Enter', preventDefault() { prevented++; } };
  const hidden = { ...pageDefinition, shown: false, assistant: { wakeup() { attempted++; return true; } } };
  hidden.onVoiceWakeup(event); hidden.onKeyUp(event);
  assert.equal(attempted, 0);
  const f = fixture(); f.assistant.show(); f.assistant.finish();
  const closed = { ...pageDefinition, setData() {}, shown: true, assistant: f.assistant };
  closed.onVoiceWakeup(event); closed.onKeyUp(event);
  closed.onUnload(); closed.onVoiceWakeup(event); closed.onKeyUp(event);
  assert.equal(prevented, 0);
  assert.equal(f.recognitions.length, 1);
});

test('unconfigured clients never open microphone; explicit remember flag and HTTPS checks remain strict', () => {
  const f = fixture({}, { endpoint: '', token: '' }); f.assistant.show();
  assert.equal(f.recognitions.length, 0); assert.equal(f.state.active, false);
  for (const input of ['记住：青石', '请记住这件事', '帮我记下明天的事', '记录这件事']) assert.equal(explicitRemember(input), true, input);
  for (const input of ['你还记得什么', '你记住了吗', '记住是什么意思', '我今天很累', '记住我的名字了吗？', '记住这件事没有？', '记下我刚才说的话了吗？']) assert.equal(explicitRemember(input), false, input);
  assert.match(validateConfig({ ...config, endpoint: 'http://personal.example/v1/chat' }), /配置有误/);
  assert.equal(validateConfig(config), '');
});


test('diagnostic uses only whitelisted names, confirmed code 20 and coarse elapsed bins', () => {
  const base = { step: 'F', fetchInvoked: true, elapsedMs: 0, ownDeadline: false, aborted: false, cancelReason: 'N' };
  assert.equal(formatRequestDiagnostic({ name: 'AbortError', code: 20 }, base), 'F1/ABT/C20/L0/D0/A0/RN');
  const secret = 'never-display-this-private-value';
  const output = formatRequestDiagnostic({ name: secret, code: 987654, message: secret, stack: secret }, base);
  assert.equal(output, 'F1/OTHER/C-/L0/D0/A0/RN');
  assert.equal(output.includes(secret), false);
  for (const [elapsedMs, bin] of [[0, '0'], [999, '0'], [1000, '1'], [4999, '1'], [5000, '2'], [14999, '2'], [15000, '3'], [59999, '3'], [60000, '4'], [NaN, 'U'], [-1, 'U']]) {
    assert.match(formatRequestDiagnostic(new Error('hidden'), { ...base, elapsedMs }), new RegExp('/L' + bin + '/'));
  }
});

test('diagnostic distinguishes setup, timer, options, fetch and JSON failures', async () => {
  const cases = [
    [{ createAbortHandle() { throw new ReferenceError('private details'); } }, config, 'S0/REF'],
    [{ setTimeout() { throw new Error('private details'); } }, config, 'T0/ERR'],
    [{}, { ...config, get sessionId() { throw new TypeError('private details'); } }, 'O0/TYPE'],
    [{ fetch: async () => { throw new TypeError('private details'); } }, config, 'F1/TYPE'],
    [{ fetch: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('private details'); } }) }, config, 'J1/SYN'],
  ];
  for (const [overrides, chosenConfig, expected] of cases) {
    const f = fixture({ ...overrides, now: () => 0 }, chosenConfig);
    await f.assistant.show('问题');
    assert.ok(f.state.diagnostic.startsWith(expected + '/'), f.state.diagnostic);
    assert.equal(f.state.phase, 'paused');
    assert.equal(JSON.stringify(f.state).includes('private details'), false);
    f.assistant.destroy();
  }
});

test('diagnostic remains visible when request logging itself throws, before or after fetch', async () => {
  for (const failedLog of [1, 2]) {
    let logs = 0; const saved = console.info;
    console.info = () => { if (++logs >= failedLog) throw new TypeError('private log details'); };
    let f;
    try {
      f = fixture({ now: () => 0 });
      await f.assistant.show('问题');
      assert.ok(f.state.diagnostic.startsWith(failedLog === 1 ? 'L0/TYPE/' : 'L1/TYPE/'));
      assert.equal(f.requests.length, failedLog === 1 ? 0 : 1);
      assert.equal(f.state.phase, 'paused');
      assert.equal(JSON.stringify(f.state).includes('private log details'), false);
    } finally { console.info = saved; f?.assistant.destroy(); }
  }
});

test('diagnostic separates unsolicited AbortError from the owned request deadline', async () => {
  const unsolicited = fixture({ fetch: async () => { throw new DOMException('private abort details', 'AbortError'); }, now: () => 0 });
  await unsolicited.assistant.show('问题');
  assert.equal(unsolicited.state.diagnostic, 'F1/ABT/C20/L0/D0/A0/RN');
  const pending = deferred(); let time = 0;
  const f = fixture({ now: () => time, fetch: (_url, options) => {
    options.signal.addEventListener('abort', () => pending.reject(new DOMException('private deadline details', 'AbortError')), { once: true });
    return pending.promise;
  } }, { ...config, timeoutMs: 1234 });
  const running = f.assistant.show('问题');time = 1234;f.fireTimer(1234);await running;
  assert.equal(f.state.diagnostic, 'F1/ABT/C20/L1/D1/A1/RD');
  assert.equal(JSON.stringify(f.state).includes('private deadline details'), false);
});

test('diagnostic persists during an explicit retry and clears on the next valid reply', async () => {
  let calls = 0;
  const f = fixture({ now: () => 0, fetch: async () => {
    if (++calls === 1) throw new TypeError('private failure details');
    return { ok: true, status: 200, json: async () => ({ reply: '正常回复' }) };
  } });
  await f.assistant.show('问题');const diagnostic = f.state.diagnostic;
  f.assistant.wakeup();assert.equal(f.state.diagnostic, diagnostic);
  f.recognitions[0].result('下一句');await flush();
  assert.equal(f.state.diagnostic, '');await completeReply(f);f.assistant.destroy();
});


test('standard HTTPS443 endpoint validates and an empty credential blocks ASR/POST', () => {
  const endpoint = 'https://192.0.2.10/v1/chat';
  assert.equal(validateConfig({ ...config, endpoint }), '');
  const f = fixture({}, { endpoint, token: '', sessionId: 'voice-memory', timeoutMs: 120000 });
  f.assistant.show('do not transmit this startup message');
  assert.equal(f.recognitions.length, 0);
  assert.equal(f.requests.length, 0);
  assert.equal(f.players.length, 0);
  assert.equal(f.state.active, false);
  assert.match(f.state.status, /尚未连接/);
});
