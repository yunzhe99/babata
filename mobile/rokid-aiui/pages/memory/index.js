import wx from 'wx';
import config from '../../config.js';
import { createAssistant, initialState } from '../../lib/assistant.js';
import { capturePhoto } from '../../lib/photo.js';

// Only the credential-free diagnostic GET may show a short sanitized reason.
// Never stringify an error object, inspect its stack, or inspect POST details.
function getErrorSummary(error) {
  let detail = '';
  if (typeof error === 'string') detail = error;
  else {
    try { const value = error.message; if (typeof value === 'string') detail = value; } catch (_) {}
    if (!detail) { try { const value = error.errMsg; if (typeof value === 'string') detail = value; } catch (_) {} }
  }
  if (!detail) return '未提供可显示错误';
  // Bound work on untrusted strings before any replacement or formatting.
  if (detail.length > 2048) return '错误内容已隐藏';
  let token = '';
  try { const value = config.token; if (typeof value === 'string') token = value; } catch (_) {}
  if (token) detail = detail.split(token).join('[已隐藏]');
  // Remove controls before keyword checks so they cannot split sensitive labels.
  detail = detail.replace(/[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF]/g, '');
  if (/auth|token|cookie|password|secret|api[\s_-]*key|headers?|body|bearer|credential|密码|密钥|令牌|凭证|请求头|请求体/i.test(detail)) {
    return '错误内容已隐藏';
  }
  detail = detail
    .replace(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[邮箱已隐藏]')
    .replace(/[a-z][a-z0-9+.-]*:\/\/[^\s<>"'`]+/gi, '[地址已隐藏]')
    .replace(/\[[0-9a-f:.]+(?:%[^\]\s]+)?\](?::\d+)?/gi, '[地址已隐藏]')
    .replace(/(^|[^0-9a-z])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]+(?:%[0-9a-z._-]+)?(?=$|[^0-9a-z])/gi, '$1[地址已隐藏]')
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/g, '[地址已隐藏]')
    .replace(/\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?::\d+)?(?:[/?#][^\s]*)?/gi, '[地址已隐藏]')
    .replace(/\s+/g, ' ').trim();
  return detail ? Array.from(detail).slice(0, 90).join('') : '未提供可显示错误';
}

export default {
  data: { ...initialState, buildTag: 'photo-1001p', keyDiag: '' },
  onLoad(query) {
    this.initialQuery = query || {};
    this.initialQueryConsumed = false;
    this.shown = false;
    this.closed = false;
    this.selfCheckDone = false;
    this.getDiagDone = false;
    this.selfCheckOwner = null;
    this.getDiagOwner = null;
    this.assistant = createAssistant({
      config,
      update: (patch) => this.setData(patch),
      runtime: {
        fetch: (url, options) => fetch(url, options),
        takePhoto: (signal) => capturePhoto(wx, signal),
        now: () => Date.now(),
        createAbortHandle: () => {
          if (typeof AbortController !== 'function') throw new Error('Cancellation unavailable');
          const controller = new AbortController();
          return { signal: controller.signal, abort: () => controller.abort() };
        },
        createRecognition: () => new SpeechRecognition(),
        createUtterance: (text) => new SpeechSynthesisUtterance(text),
        synthesize: (utterance, options) => speechSynthesis.synthesize(utterance, options),
        createPlayer: (task) => new SpeechAudioPlayer(task),
        setTimeout: (callback, ms) => setTimeout(callback, ms),
        clearTimeout: (id) => clearTimeout(id),
        runGetDiagnostic: () => this._runGetDiagnostic(),
        finish: () => {
          this.closed = true;
          this.shown = false;
          this._cancelDiagnostics();
          // Local resources are already stopped before the host close request.
          try {
            if (typeof window !== 'undefined' && typeof window.close === 'function') {
              window.close();
              return;
            }
          } catch (_) {}
          try { if (typeof this.finish === 'function') this.finish(); } catch (_) {}
        },
      },
    });
  },
  onShow() {
    if (!this.assistant || this.shown || this.closed) return;
    this.shown = true;
    this.selfCheckDone = false;
    this.getDiagDone = false;
    this.setData({ selfCheck: '', getDiag: '', keyDiag: '' });
    const message = this.initialQueryConsumed ? '' : this.initialQuery.message;
    this.initialQueryConsumed = true;
    // Neither timer self-check nor diagnostic network awaits can block speech.
    void this.assistant.show(message);
    if (this.shown && !this.closed) this._runSelfCheck();
  },
  _runSelfCheck() {
    if (!this.shown || this.closed || this.selfCheckDone) return;
    this.selfCheckDone = true;
    const owned = { timer: null, fallback: null };
    this.selfCheckOwner = owned;
    let cancelOk = false;
    try {
      const controller = new AbortController();
      if (controller.signal.aborted === false) {
        controller.abort();
        cancelOk = controller.signal.aborted === true;
      }
    } catch (_) {}
    const valid = () => this.shown && !this.closed && this.selfCheckOwner === owned;
    const finish = (timerOk) => {
      if (!valid()) return;
      this._clearSelfCheck();
      this.setData({ selfCheck: `${cancelOk ? '取消✓' : '取消失败'}、${timerOk ? '计时✓' : '计时未完成'}` });
    };
    this.setData({ selfCheck: `${cancelOk ? '取消✓' : '取消失败'}、计时待验` });
    try {
      owned.timer = setTimeout(() => finish(true), 200);
      owned.fallback = setTimeout(() => finish(false), 3000);
    } catch (_) { finish(false); }
  },
  _clearSelfCheck() {
    const owned = this.selfCheckOwner;
    this.selfCheckOwner = null;
    if (!owned) return;
    for (const id of [owned.timer, owned.fallback]) {
      if (id !== null) { try { clearTimeout(id); } catch (_) {} }
    }
  },
  // One credential-free GET per actual foreground entry. A 405 proves an HTTP
  // response only; it does not verify the authenticated POST or model reply.
  _runGetDiagnostic() {
    if (!this.shown || this.closed || this.getDiagDone) return Promise.resolve(null);
    this.getDiagDone = true; // Consume allowance at start, including failure/cancel.
    const owned = { timer: null, settled: false, finish: null };
    const promise = new Promise((resolve) => {
      owned.finish = (result, publish = true) => {
        if (owned.settled) return;
        const current = this.getDiagOwner === owned;
        owned.settled = true;
        if (current) this.getDiagOwner = null;
        if (owned.timer !== null) { try { clearTimeout(owned.timer); } catch (_) {} owned.timer = null; }
        // A bare GET cannot be aborted here: settle locally and ignore late results.
        if (publish && current && this.shown && !this.closed) this.setData({ getDiag: result });
        resolve(result);
      };
    });
    this.getDiagOwner = owned;
    this.setData({ getDiag: '联网检测中…' });
    try {
      owned.timer = setTimeout(() => owned.finish('联网超时'), 12000);
      // Deliberately bare: isolate native fetch from option/signal compatibility.
      Promise.resolve(fetch(config.endpoint)).then((response) => {
        if (owned.settled) return;
        const status = response && response.status;
        owned.finish(Number.isInteger(status) && status >= 100 && status <= 599
          ? `联网${status}` : '联网未完成');
      }).catch((error) => {
        if (!owned.settled) owned.finish(`联网未完成：${getErrorSummary(error)}`);
      });
    } catch (error) { owned.finish(`联网未完成：${getErrorSummary(error)}`); }
    return promise;
  },
  _clearGetDiag() {
    const owned = this.getDiagOwner;
    if (owned) owned.finish(null, false);
  },
  _cancelDiagnostics(publish = false) {
    const patch = {};
    if (this.selfCheckOwner) patch.selfCheck = '';
    if (this.getDiagOwner) patch.getDiag = '';
    this._clearSelfCheck();
    this._clearGetDiag();
    // A foreground retry must not leave cancelled checks looking pending.
    if (publish && this.shown && !this.closed && Object.keys(patch).length) this.setData(patch);
  },
  onVoiceWakeup(event) {
    if (this.shown && this.assistant && this.assistant.wakeup()) {
      this._cancelDiagnostics(true);
      event.preventDefault();
    }
  },
  _recordKeyDiagnostic(event, phase) {
    if (!this.shown || this.closed) return undefined;
    let code;
    try { code = event && event.code; } catch (_) {}
    // Temporary, page-only probe. Never retain key characters, position, or the
    // event object; unknown device codes do not authorize a camera action.
    const safeCode = typeof code === 'string' ? code.slice(0, 24).replace(/[^A-Za-z0-9_.:-]/g, '_') : '';
    this.setData({ keyDiag: `K:${phase}/${safeCode || '-'}` });
    return code;
  },
  onKeyDown(event) {
    this._recordKeyDiagnostic(event, 'D');
  },
  onKeyUp(event) {
    const code = this._recordKeyDiagnostic(event, 'U');
    if (code === 'Enter') {
      this.onVoiceWakeup(event);
    } else if (code === 'Backspace') {
      this.onHide();
    }
  },
  onHide() {
    this.shown = false;
    this._cancelDiagnostics();
    if (this.assistant) this.assistant.hide();
  },
  onUnload() {
    this.closed = true;
    this.shown = false;
    this._cancelDiagnostics();
    if (this.assistant) this.assistant.destroy();
  },
};
