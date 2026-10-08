import { encodePhoto, photoFailureCode, photoFailureNotice } from './photo.js';

export const initialState = {
  status: '正在进入对话…',
  phase: 'idle',
  question: '',
  partial: '',
  answer: '',
  notice: '',
  active: false,
  buildTag: 'fix-0930d',
  diagnostic: '',
  networkObservation: '',
  selfCheck: '',
  getDiag: '',
};

// Deliberately narrow: flag explicit memory emphasis, not ordinary memory policy.
// False leaves the server's native Codex memory and background summaries intact.
export function explicitRemember(message) {
  const text = message.trim();
  if (/(?:了吗|了么|了没|没有|没|了吧)[？?。！!\s]*$/u.test(text)) return false;
  return /^(?:(?:请|麻烦你?|帮我|请帮我)\s*)?(?:记住|记下)(?:[：:，,\s]|这|我|今天|明天|以后|下次|刚才|以下|一下)/u.test(text) ||
    /^(?:(?:请|麻烦你?|帮我|请帮我)\s*)?记录(?:这件事|一下|以下内容)[：:，,\s]?/u.test(text);
}

function requestId() {
  // An idempotency identifier, not an authentication credential.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (part) => {
    const value = Math.floor(Math.random() * 16);
    return (part === 'x' ? value : (value & 3) | 8).toString(16);
  });
}

export function validateConfig(config) {
  if (!config || typeof config.endpoint !== 'string' || !config.endpoint ||
      typeof config.token !== 'string' || !config.token) {
    return '尚未连接私人助手，请先配置接入地址和专用密钥。';
  }
  // No query/fragment credentials; the endpoint is the complete /v1/chat URL.
  if (!/^https:\/\/[^/?#\s@]+(?::\d+)?\/[^?#\s]*\/chat$/.test(config.endpoint) ||
      /[\r\n]/.test(config.token)) {
    return '接入配置有误，请检查 HTTPS 地址和专用密钥。';
  }
  return '';
}

export function isExitCommand(message) {
  return /^(?:退出(?:巴巴塔)?|结束(?:本次)?(?:对话|聊天)|停止(?:对话|聊天)|再见|回到乐奇|切换乐奇)$/u
    .test(message.trim().replace(/[。！!，,？?\s]+$/u, ''));
}

// Maps a raw fetch-stage error to a fixed, non-sensitive category string.
// Never exposes error.name, error.message, response body, or credentials.
function classifyFetchError(errorText) {
  if (/timeout|timed.?out|超时/i.test(errorText)) return 'timeout';
  if (/certificate|\bTLS\b|\bSSL\b|证书/i.test(errorText)) return 'tls';
  if (/network|dns|resolve|connect|网络/i.test(errorText)) return 'network';
  return 'request';
}

// Raw host details are inspected locally; only fixed hints and bounded own integers leave this helper.
const own = (value, key) => value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, key);
const read = (value, key) => { try { return own(value, key) ? value[key] : undefined; } catch (_) { return undefined; } };
const text = value => typeof value === 'string' && value.length <= 4096 ? value : '';
export function safeNetworkObservation(error) {
  const raw = text(read(error, 'message'));
  let embedded = null;
  const prefix = 'Request failed: ';
  const payload = raw.startsWith(prefix) ? raw.slice(prefix.length).trim() : '';
  if (payload.startsWith('{') && payload.endsWith('}')) {
    try { const parsed = JSON.parse(payload); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) embedded = parsed; } catch (_) {}
  }
  const detail = text(read(embedded, 'errMsg')) || text(read(embedded, 'message')) || text(read(error, 'errMsg')) || raw;
  const hints = [
    ['DOMAIN_HINT', /(?:url|domain|域名).{0,40}(?:not\s+(?:allowed|in)|disallowed|allowlist|whitelist|白名单|不合法|不允许|未配置|未登记|未备案|禁止)|(?:allowlist|whitelist).{0,40}(?:reject|denied|deny)/i],
    ['PERMISSION_HINT', /permission.{0,24}(?:denied|not granted|missing|required)|not authorized|access denied|无权限|权限.{0,12}(?:不足|拒绝|未授权|未声明)/i],
    ['TLS_HINT', /certificate|\bTLS\b|\bSSL\b|certpath|证书/i],
    ['OFFLINE_HINT', /no network|network (?:is )?unavailable|network unreachable|not connected|offline|无网络|未联网|网络不可用|未连接网络/i],
    ['DNS_HINT', /\bDNS\b|ENOTFOUND|EAI_AGAIN|unknownhost|unable to resolve|could not resolve|name resolution|域名解析/i],
    ['ABORT_HINT', /AbortError|aborted|cancelled|canceled|已取消|取消请求/i],
    ['TIMEOUT_HINT', /TimeoutError|timed?\s*out|timeout|超时/i],
  ];
  const reason = (hints.find(([, pattern]) => pattern.test(detail)) || ['OTHER'])[0];
  const codes = {};
  for (const key of ['code', 'errorCode', 'errno']) {
    for (const source of [error, embedded]) {
      const value = read(source, key);
      if (typeof value === 'number' && Number.isSafeInteger(value) && Math.abs(value) <= 1000000) { codes[key] = value; break; }
    }
  }
  return { reason, wrappedObject: !!embedded, codes };
}
export function formatNetworkObservation(error) {
  const observed = safeNetworkObservation(error);
  const codes = observed.codes;
  return `${observed.reason}/W${observed.wrappedObject ? '1' : '0'}/C${codes.code ?? '-'}/E${codes.errorCode ?? '-'}/N${codes.errno ?? '-'}`;
}

// Diagnostic output contains fixed enums only; never serializes an error.
export function formatRequestDiagnostic(error, info) {
  let name, code;
  try { name = error && error.name; code = error && error.code; } catch (_) {}
  const names = { Error: 'ERR', TypeError: 'TYPE', AbortError: 'ABT', TimeoutError: 'TIME',
    NetworkError: 'NET', SecurityError: 'SEC', ReferenceError: 'REF', InvalidStateError: 'STATE',
    NotSupportedError: 'UNSUP', SyntaxError: 'SYN' };
  const safeName = typeof name === 'string' && Object.prototype.hasOwnProperty.call(names, name) ? names[name] : 'OTHER';
  const safeCode = name === 'AbortError' && code === 20 ? '20' : '-';
  const step = ['S', 'T', 'L', 'O', 'F', 'H', 'J'].includes(info.step) ? info.step : 'U';
  const ms = info.elapsedMs;
  const elapsed = typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0 ? 'U'
    : ms < 1000 ? '0' : ms < 5000 ? '1' : ms < 15000 ? '2' : ms < 60000 ? '3' : '4';
  const aborted = info.aborted === true ? '1' : info.aborted === false ? '0' : '?';
  const cancel = ['N', 'D', 'L'].includes(info.cancelReason) ? info.cancelReason : 'U';
  return `${step}${info.fetchInvoked === true ? '1' : '0'}/${safeName}/C${safeCode}/L${elapsed}/D${info.ownDeadline === true ? '1' : '0'}/A${aborted}/R${cancel}`;
}

// All recording belongs to a visible conversation. Host/API rejection remains
// visible; a voice-wakeup event can explicitly retry without a screen button.
export function createAssistant({ config, update, runtime }) {
  let visible = false;
  let active = false;
  let destroyed = false;
  let closing = false;
  let generation = 0;
  let phase = 'idle';
  let failures = 0;
  let silences = 0;
  let recognition = null;
  let recognitionTimer = null;
  let nextTimer = null;
  let request = null;
  const consumedActions = new Set();
  let speech = null;
  let connectionNotice = '';
  let connectionDiagnostic = '';
  let connectionObservation = '';
  const now = () => { try { return typeof runtime.now === 'function' ? runtime.now() : Date.now(); } catch (_) { return NaN; } };

  const valid = (id) => visible && active && !destroyed && !closing && generation === id;
  const display = (patch) => { if (!destroyed) update(patch); };
  function showRequestNotice(uncertainMemory = false) {
    display({ diagnostic: connectionDiagnostic, networkObservation: connectionObservation, notice: [connectionNotice, uncertainMemory
      ? '上一条重点保存结果未确认，可先问是否已经记住。' : ''].filter(Boolean).join(' ') });
  }
  function setPhase(value, status, patch = {}) {
    phase = value;
    display({ phase, status, active, ...patch });
  }
  function clearTimer(name) {
    if (name === 'recognition' && recognitionTimer !== null) {
      runtime.clearTimeout(recognitionTimer);
      recognitionTimer = null;
    }
    if (name === 'next' && nextTimer !== null) {
      runtime.clearTimeout(nextTimer);
      nextTimer = null;
    }
  }
  function stopRecognition() {
    clearTimer('recognition');
    const old = recognition;
    recognition = null;
    if (!old) return;
    old.onstart = old.onresult = old.onerror = old.onend = null;
    try { old.abort(); } catch (_) {}
  }
  function cancelOwnedWork() {
    clearTimer('next');
    stopRecognition();
    if (request) {
      request.cancelReason = 'L';
      // Explicitly clear the request timeout timer before aborting.
      if (request.timeoutId !== null && request.timeoutId !== undefined) {
        runtime.clearTimeout(request.timeoutId);
        request.timeoutId = null;
      }
      if (request.cameraTimer !== null && request.cameraTimer !== undefined) {
        runtime.clearTimeout(request.cameraTimer); request.cameraTimer = null;
      }
      try { request.cameraAbort?.abort(); } catch (_) {}
      try { request.abort.abort(); } catch (_) {}
      if (request.cancelWait) request.cancelWait();
    }
    request = null;
    if (speech) speech.finish('cancelled');
  }
  function suspend(status) {
    ++generation;
    active = false;
    cancelOwnedWork();
    setPhase('paused', status, { partial: '' });
  }
  function finishConversation() {
    if (destroyed || closing) return;
    // Closing the host window is asynchronous. This page instance must never
    // restart recording while waiting for onHide/onUnload or a stray onShow.
    closing = true;
    visible = false;
    suspend('本次对话已结束。');
    if (runtime.finish) { try { runtime.finish(); } catch (_) {} }
  }
  function scheduleListening(delay, id) {
    clearTimer('next');
    if (!valid(id)) return;
    nextTimer = runtime.setTimeout(() => {
      nextTimer = null;
      if (valid(id)) beginListening();
    }, delay);
  }
  function retry(status, id, silence = false) {
    if (!valid(id)) return;
    const count = silence ? ++silences : ++failures;
    if (count >= 3) {
      suspend(silence
        ? '暂时没有听到你说话，已停止收音。说"乐奇"可继续。'
        : '语音连接连续失败，已停止收音。说"乐奇"可重试。');
      return;
    }
    setPhase('waiting', status, { partial: '' });
    scheduleListening(Math.min(4000, 1000 * (2 ** (count - 1))), id);
  }
  function microphoneFailure(error, id) {
    if (!valid(id)) return;
    stopRecognition();
    const detail = String(error && (error.error || error.name) || '') + ' ' +
      String(error && error.message || '');
    if (/not.allowed|permission|requires|interactive|gesture|lifetime|cut|NotSupported|unavailable|shared.*format/i.test(detail)) {
      suspend('未能开启语音。请说"乐奇"开始对话；若仍失败，请检查麦克风授权。');
    } else {
      retry('这次没有听清，稍后继续听。', id, /no.speech/i.test(detail));
    }
  }

  function beginListening() {
    const id = generation;
    if (!valid(id) || recognition || request || speech) return;
    clearTimer('next');
    let current;
    try {
      current = runtime.createRecognition();
      recognition = current;
      current.lang = 'zh-CN';
      current.continuous = false;
      current.interimResults = true;
      const finals = [];
      const own = () => valid(id) && recognition === current;
      current.onstart = () => {
        if (own()) setPhase('listening', '我在听，请说。');
      };
      current.onresult = (event) => {
        if (!own()) return;
        const results = event.results || [];
        let partial = '';
        let hasFinal = false;
        for (let index = 0; index < results.length; index += 1) {
          const result = results[index];
          const text = result && result[0] && result[0].transcript;
          if (typeof text !== 'string') continue;
          if (result.isFinal) { finals[index] = text; hasFinal = true; }
          else partial += text;
        }
        if (!hasFinal) { display({ partial }); return; }
        const message = finals.filter(Boolean).join(' ').trim();
        // Some hosts do not promptly deliver onend after a final result. Stop
        // the microphone here and submit once, using this ASR object's identity.
        stopRecognition();
        if (!message) { retry('没有听清，稍后继续听。', id, true); return; }
        silences = 0;
        if (isExitCommand(message)) { finishConversation(); return; }
        void submit(message);
      };
      current.onerror = (event) => { if (own()) microphoneFailure(event, id); };
      current.onend = () => {
        if (!own()) return;
        stopRecognition();
        retry('暂时没有听到内容，稍后继续听。', id, true);
      };
      setPhase('starting', '正在开启语音…', { partial: '' });
      const starting = current.start();
      // The documented API is event-based. Also handle a host returning a
      // thenable so an asynchronous start rejection cannot leave us listening.
      if (starting && typeof starting.catch === 'function') {
        starting.catch((error) => { if (own()) microphoneFailure(error, id); });
      }
      if (own()) recognitionTimer = runtime.setTimeout(() => {
        if (!own()) return;
        stopRecognition();
        retry('这一轮已结束，稍后继续听。', id, true);
      }, 30000);
    } catch (error) { microphoneFailure(error, id); }
  }

  function speak(text, id) {
    return new Promise((resolve) => {
      const owned = { settled: false, player: null, task: null, abort: null, timer: null };
      owned.finish = (result) => {
        if (owned.settled) return;
        owned.settled = true;
        if (owned.timer !== null) runtime.clearTimeout(owned.timer);
        const audio = owned.player && owned.player.audioPlayer;
        if (audio) {
          try { audio.offEnded(owned.onEnded); } catch (_) {}
          try { audio.offError(owned.onError); } catch (_) {}
        }
        // Unregister callbacks before stopping so cancellation cannot reopen ASR.
        if (owned.player) {
          try { owned.player.stop(); } catch (_) {}
          try { owned.player.destroy(); } catch (_) {}
        }
        if (owned.task) { try { owned.task.abort(); } catch (_) {} }
        if (owned.abort) { try { owned.abort.abort(); } catch (_) {} }
        if (speech === owned) speech = null;
        resolve(result);
      };
      speech = owned;
      owned.onEnded = () => owned.finish(valid(id) ? 'ended' : 'cancelled');
      owned.onError = () => owned.finish('failed');
      owned.timer = runtime.setTimeout(() => owned.finish('failed'), 120000);
      try {
        owned.abort = runtime.createAbortHandle();
        Promise.resolve(runtime.synthesize(runtime.createUtterance(text), { signal: owned.abort.signal }))
          .then((task) => {
            if (task.finished) task.finished.catch(() => owned.finish('failed'));
            if (owned.settled || !valid(id)) { task.abort(); owned.finish('cancelled'); return; }
            owned.task = task;
            owned.player = runtime.createPlayer(task);
            const audio = owned.player.audioPlayer;
            if (!audio || typeof audio.onEnded !== 'function' || typeof audio.onError !== 'function') {
              throw new Error('Playback completion is unavailable');
            }
            audio.onEnded(owned.onEnded);
            audio.onError(owned.onError);
            owned.player.play();
          }).catch(() => owned.finish('failed'));
      } catch (_) { owned.finish('failed'); }
    });
  }

  async function submit(rawMessage) {
    const id = generation;
    if (!valid(id) || request || speech || recognition) return false;
    const message = typeof rawMessage === 'string' ? rawMessage.trim() : '';
    if (!message) return false;
    if (isExitCommand(message)) { finishConversation(); return true; }
    if (message.length > 32000) {
      retry('这一段太长，请分成几次告诉我。', id);
      return false;
    }
    clearTimer('next');
    // The flag requests extra emphasis; every turn still follows server memory policy.
    const owned = { abort: null, timeoutId: null, timedOut: false, cancelReason: 'N',
      cameraAbort: null, cameraTimer: null, actionExpired: false, actionId: null,
      remember: explicitRemember(message), requestId: requestId(), sessionId: null };
    const startedAt = now();
    const totalTimeout = config.timeoutMs || 120000;
    let requestStep = 'S';
    let fetchInvoked = false;
    request = owned;
    setPhase('thinking', '我听到了，正在处理…', { question: message, partial: '', answer: '' });
    let failureStage = 'setup';
    let httpStatus = null;
    let errorCategory = 'setup';
    const cancelled = new Promise((_, reject) => {
      owned.cancelWait = () => reject(new Error('request-cancelled'));
    });
    // Setup or synchronous host errors may occur before the first await.
    cancelled.catch(() => {});
    const remaining = () => {
      const elapsed = now() - startedAt;
      return Number.isFinite(elapsed) ? Math.max(0, totalTimeout - Math.max(0, elapsed)) : totalTimeout;
    };
    const ensureCurrent = () => {
      if (!valid(id) || request !== owned) throw new Error('request-cancelled');
      if (owned.timedOut || remaining() <= 0) {
        owned.timedOut = true;
        owned.cancelReason = 'D';
        throw new Error('request-timeout');
      }
    };
    const waitCurrent = async pending => {
      const result = await Promise.race([pending, cancelled]);
      ensureCurrent();
      return result;
    };
    // Both POST stages share the original deadline and credential destination.
    const postJson = async (endpoint, payload) => {
      ensureCurrent();
      failureStage = 'request';
      httpStatus = null;
      requestStep = 'L';
      console.info('[巴巴塔] fetch 发起', { stage: failureStage });
      requestStep = 'O';
      const options = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.token}` },
        body: JSON.stringify(payload), timeout: Math.max(1, remaining()), signal: owned.abort.signal,
      };
      requestStep = 'F';
      fetchInvoked = true;
      const response = await waitCurrent(runtime.fetch(endpoint, options));
      const receivedAt = now();
      requestStep = 'H';
      failureStage = 'http';
      if (response && Number.isInteger(response.status) && response.status >= 100 && response.status <= 599) httpStatus = response.status;
      requestStep = 'L';
      console.info('[巴巴塔] fetch 响应', { stage: failureStage, httpStatus });
      requestStep = 'H';
      if (!response || !response.ok) throw new Error('http');
      failureStage = 'response';
      requestStep = 'J';
      const data = await waitCurrent(response.json());
      return { data, receivedAt };
    };
    try {
      owned.abort = runtime.createAbortHandle();
      failureStage = 'request';
      requestStep = 'T';
      owned.timeoutId = runtime.setTimeout(() => {
        if (!valid(id) || request !== owned) return;
        owned.timeoutId = null;
        owned.timedOut = true;
        owned.cancelReason = 'D';
        try { owned.abort.abort(); } catch (_) {}
        try { owned.cameraAbort?.abort(); } catch (_) {}
        owned.cancelWait();
      }, totalTimeout);
      requestStep = 'O';
      owned.sessionId = config.sessionId || 'voice-memory';
      let { data, receivedAt } = await postJson(config.endpoint, {
        message, session_id: owned.sessionId, request_id: owned.requestId,
        remember: owned.remember, capabilities: { camera: true },
      });
      if (data && data.action != null) {
        failureStage = 'action';
        const action = data.action;
        const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
        if (data.reply !== '' || !action || typeof action !== 'object' || Array.isArray(action) ||
            action.type !== 'take_photo' || typeof action.id !== 'string' || !uuid.test(action.id) ||
            data.session_id !== owned.sessionId || data.request_id !== owned.requestId ||
            !Number.isInteger(action.expires_in_ms) || action.expires_in_ms <= 0 || action.expires_in_ms > 60000 ||
            !Number.isFinite(receivedAt) || consumedActions.has(action.id.toLowerCase())) throw new Error('invalid-action');
        const actionExpiresAt = receivedAt + action.expires_in_ms;
        const ensureAction = () => {
          ensureCurrent();
          if (owned.actionExpired || !Number.isFinite(now()) || now() >= actionExpiresAt) {
            owned.actionExpired = true;
            throw { photoKind: 'timeout' };
          }
        };
        ensureAction();
        // Consume before calling the host; repeated responses cannot reshoot.
        consumedActions.add(action.id.toLowerCase());
        owned.actionId = action.id;
        const resultBody = { session_id: owned.sessionId, request_id: owned.requestId, action_id: action.id };
        try {
          failureStage = 'photo';
          setPhase('capturing', '正在拍摄一张照片…');
          ensureAction();
          if (typeof runtime.takePhoto !== 'function') throw { photoKind: 'unavailable' };
          owned.cameraAbort = runtime.createAbortHandle();
          const actionRemaining = actionExpiresAt - now();
          const totalRemaining = remaining();
          const captureTimeout = Math.min(30000, actionRemaining, totalRemaining);
          const captureDeadline = new Promise((_, reject) => {
            owned.cameraTimer = runtime.setTimeout(() => {
              if (!valid(id) || request !== owned || owned.cameraTimer === null) return;
              owned.cameraTimer = null;
              if (captureTimeout >= actionRemaining || now() >= actionExpiresAt) owned.actionExpired = true;
              if (captureTimeout >= totalRemaining) { owned.timedOut = true; owned.cancelReason = 'D'; }
              reject({ photoKind: 'timeout' });
              try { owned.cameraAbort?.abort(); } catch (_) {}
            }, captureTimeout);
          });
          const captured = await waitCurrent(Promise.race([
            runtime.takePhoto(owned.cameraAbort.signal), captureDeadline,
          ]));
          ensureAction();
          resultBody.image = encodePhoto(captured);
          ensureAction();
          resultBody.status = 'ok';
        } catch (error) {
          ensureAction();
          resultBody.status = 'error';
          resultBody.error_code = photoFailureCode(error);
        } finally {
          if (owned.cameraTimer !== null) { runtime.clearTimeout(owned.cameraTimer); owned.cameraTimer = null; }
          try { owned.cameraAbort?.abort(); } catch (_) {}
          owned.cameraAbort = null;
        }
        ensureAction();
        setPhase('uploading', resultBody.status === 'ok' ? '正在上传照片，等待分析…' : '未能拍照，正在告知私人助手…');
        ({ data } = await postJson(config.endpoint.replace(/\/chat$/, '/device-result'), resultBody));
        // The continuation may only finish this turn, never request another shot.
        if (!data || data.action != null || (data.request_id != null && data.request_id !== owned.requestId) ||
            (data.session_id != null && data.session_id !== owned.sessionId)) {
          failureStage = 'action';
          throw new Error('invalid-device-result');
        }
      }
      if (!data || typeof data.reply !== 'string' || !data.reply.trim()) throw new Error('invalid-reply');
      ensureCurrent();
      // Clear the timeout timer on success path.
      if (owned.timeoutId !== null) { runtime.clearTimeout(owned.timeoutId); owned.timeoutId = null; }
      request = null;
      connectionNotice = '';
      connectionDiagnostic = '';
      connectionObservation = '';
      setPhase('speaking', '正在回答…', { answer: data.reply.trim(), notice: '', diagnostic: '', networkObservation: '' });
      const result = await speak(data.reply.trim(), id);
      if (!valid(id)) return false;
      if (result === 'ended') {
        failures = 0;
        setPhase('waiting', '我说完了，接着说就好。');
        scheduleListening(350, id);
      } else if (result === 'failed') {
        retry('已收到文字回复，播报失败；稍后继续听。', id);
      }
      return result === 'ended';
    } catch (error) {
      if (!valid(id) || request !== owned) return false;
      // Clear the timeout timer on error path.
      if (owned.timeoutId !== null) { runtime.clearTimeout(owned.timeoutId); owned.timeoutId = null; }
      request = null;
      // Fetch rejection cannot distinguish opening the connection from waiting
      // for a reply. Never expose raw errors, response bodies or credentials.
      if (owned.timedOut) {
        errorCategory = 'timeout';
      } else if (failureStage === 'request') {
        let errorText = '';
        try { errorText = text(error && error.name) + ' ' + text(error && error.message); } catch (_) {}
        errorCategory = classifyFetchError(errorText);
      } else if (failureStage === 'http') {
        errorCategory = 'http-error';
      } else if (failureStage === 'response') {
        errorCategory = 'parse-error';
      }
      connectionObservation = formatNetworkObservation(error);
      connectionDiagnostic = formatRequestDiagnostic(error, { step: requestStep, fetchInvoked,
        elapsedMs: now() - startedAt, ownDeadline: owned.timedOut,
        aborted: owned.abort && owned.abort.signal && owned.abort.signal.aborted, cancelReason: owned.cancelReason });
      try { console.info('[巴巴塔] fetch 失败', { stage: failureStage, httpStatus, errorCategory, diagnostic: connectionDiagnostic }); } catch (_) {}
      connectionNotice = owned.timedOut
        ? (failureStage === 'photo' ? '拍照等待超时，本次未上传。' : '连接或等待回复超时，尚未取得可用响应。')
        : owned.actionExpired
        ? '拍照指令已过期，本次未上传。'
        : failureStage === 'action'
        ? '服务器拍照指令或最终回复无效，请重新提问。'
        : failureStage === 'photo'
        ? photoFailureNotice(error)
        : failureStage === 'setup'
        ? '连接初始化失败，请检查眼镜运行时版本。'
        : failureStage === 'request'
          ? (errorCategory === 'timeout'
            ? '连接或等待回复超时，尚未取得可用响应。'
            : errorCategory === 'tls'
              ? '安全连接未完成，请检查证书或 TLS 连接。'
              : errorCategory === 'network'
                ? '网络连接未完成，尚未取得可用响应。'
                : '连接或等待回复未完成，尚未取得可用响应。')
        : failureStage === 'http'
          ? (owned.actionId && httpStatus === 413 ? '照片过大，服务器未接受本次照片。'
            : owned.actionId && (httpStatus === 415 || httpStatus === 422) ? '服务器未能读取这张照片，请重新拍摄。'
            : httpStatus === null ? 'HTTP 状态无效，私人助手未成功响应。' : `HTTP ${httpStatus}，私人助手未成功响应。`)
          : '响应解析失败，未收到有效的文字回复。';
      // Keep the specific failure reason on the first-screen status so it is
      // not overwritten by a subsequent beginListening/retry status.
      if (httpStatus === 401 || httpStatus === 403) {
        suspend('私人助手连接未获授权，请检查接入配置。');
      } else {
        suspend(`${connectionNotice}说"乐奇"可重试。`);
      }
      showRequestNotice(owned.remember);
      // Page owns this independent probe: a paused voice turn can still diagnose.
      if (failureStage !== 'photo' && typeof runtime.runGetDiagnostic === 'function') {
        try { runtime.runGetDiagnostic(); } catch (_) {}
      }
      return false;
    }
  }

  function activate(message) {
    if (destroyed || closing || !visible) return false;
    const interruptedWrite = request && request.remember;
    ++generation;
    active = false;
    cancelOwnedWork();
    failures = 0;
    silences = 0;
    const error = validateConfig(config);
    if (error) { setPhase('paused', error); return false; }
    active = true;
    if (interruptedWrite) showRequestNotice(true);
    if (typeof message === 'string' && message.trim()) return submit(message);
    beginListening();
    return true;
  }
  function hide() {
    const interruptedWrite = request && request.remember;
    visible = false;
    suspend(closing ? '本次对话已结束。' : '已停止收音。重新进入后继续。');
    if (interruptedWrite) showRequestNotice(true);
  }
  return {
    submit,
    show(message) { if (destroyed || closing || visible) return false; visible = true; return activate(message); },
    wakeup() { return activate(); },
    finish: finishConversation,
    hide,
    destroy() { hide(); destroyed = true; },
  };
}
