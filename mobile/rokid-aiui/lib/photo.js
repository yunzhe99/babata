export const MAX_PHOTO_BYTES = 6 * 1024 * 1024;

const photoError = (kind) => Object.assign(new Error('Photo operation failed'), { photoKind: kind });

// No btoa/Buffer assumption on glasses. Encode bounded blocks rather than
// spreading a multi-megabyte image into function arguments.
export function encodePhoto(result) {
  const data = result && result.data;
  const mimeType = result && result.mimeType;
  if (mimeType !== 'image/jpeg' && mimeType !== 'image/png') throw photoError('format');
  if (!(data instanceof ArrayBuffer) || !data.byteLength) throw photoError('invalid');
  if (data.byteLength > MAX_PHOTO_BYTES) throw photoError('size');
  const bytes = new Uint8Array(data);
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const chunks = [];
  let chunk = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = bytes[i + 1] || 0, c = bytes[i + 2] || 0;
    chunk += alphabet[a >> 2] + alphabet[((a & 3) << 4) | (b >> 4)] +
      (i + 1 < bytes.length ? alphabet[((b & 15) << 2) | (c >> 6)] : '=') +
      (i + 2 < bytes.length ? alphabet[c & 63] : '=');
    if (chunk.length >= 16384) { chunks.push(chunk); chunk = ''; }
  }
  if (chunk) chunks.push(chunk);
  return { data_base64: chunks.join(''), mime_type: mimeType };
}

// AIUI 0.18 documents wx.media's Promise API; older hosts expose the flat wx
// callback API. Use exactly one invocation and accept either completion form.
// The one-shot camera API has no documented physical abort. Abort only discards
// late results and prevents upload; this wrapper never opens a media stream.
export function capturePhoto(host, signal) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      try { signal?.removeEventListener('abort', abort); } catch (_) {}
      if (error) reject(error); else resolve(result);
    };
    const abort = () => finish(photoError('cancelled'));
    if (signal?.aborted) { abort(); return; }
    try { signal?.addEventListener('abort', abort, { once: true }); } catch (_) {}
    const failure = (error) => {
      let detail = '';
      try { detail = `${error?.name || ''} ${error?.errMsg || ''} ${error?.message || ''}`; } catch (_) {}
      finish(photoError(/interacti|gesture|user.?activation|交互/i.test(detail) ? 'interaction'
        : /permission|denied|not.?allowed|权限|拒绝/i.test(detail) ? 'permission' : 'capture'));
    };
    try {
      const owner = host?.media && typeof host.media.createCameraContext === 'function' ? host.media : host;
      if (!owner || typeof owner.createCameraContext !== 'function') { finish(photoError('unavailable')); return; }
      const camera = owner.createCameraContext();
      if (!camera || typeof camera.takePhoto !== 'function') { finish(photoError('unavailable')); return; }
      const result = camera.takePhoto({
        quality: 'normal', mode: 'telephoto', enableSystemPreview: false,
        success: (value) => finish(null, value), fail: failure,
      });
      if (result && typeof result.then === 'function') result.then(value => finish(null, value), failure);
    } catch (error) { failure(error); }
  });
}

export function photoFailureCode(error) {
  const allowed = ['unavailable', 'permission', 'interaction', 'capture', 'cancelled', 'format', 'invalid', 'size', 'timeout'];
  try { return allowed.includes(error && error.photoKind) ? error.photoKind : 'capture'; } catch (_) { return 'capture'; }
}

export function photoFailureNotice(error) {
  const messages = {
    permission: '未获得相机权限，请在设备或 Rokid App 中允许巴巴塔使用相机。',
    interaction: '系统未允许这次拍照触发，本次未上传；语音触发是否受支持还需真机确认。',
    unavailable: '当前运行环境没有可用的拍照接口。',
    format: '照片格式不支持，请使用 JPEG 或 PNG。',
    invalid: '相机没有返回有效照片，本次未上传。',
    size: '照片超过 6 MiB，本次未上传，请降低拍照质量后重试。',
    capture: '这次未能拍照，本次未上传。',
    cancelled: '拍照已取消，本次未上传。',
    timeout: '拍照等待超时，本次未上传。',
  };
  return messages[error && error.photoKind] || messages.capture;
}
