import test from 'node:test';
import assert from 'node:assert/strict';
import { capturePhoto, encodePhoto, photoFailureCode, photoFailureNotice, MAX_PHOTO_BYTES } from '../lib/photo.js';

test('camera result errors use a fixed enum without retaining host details', () => {
  for (const photoKind of ['unavailable', 'permission', 'interaction', 'capture', 'cancelled', 'format', 'invalid', 'size', 'timeout']) {
    assert.equal(photoFailureCode({ photoKind, message: 'private details' }), photoKind);
  }
  for (const error of [new Error('private details'), { photoKind: 'secret-token' }, null, { get photoKind() { throw new Error('hidden'); } }]) {
    assert.equal(photoFailureCode(error), 'capture');
  }
});

test('base64 matches binary data including padding and multiple bounded blocks', () => {
  for (const length of [1, 2, 3, 4, 20000]) {
    const bytes = Uint8Array.from({ length }, (_, i) => i % 256);
    assert.equal(encodePhoto({ data: bytes.buffer, mimeType: 'image/jpeg' }).data_base64, Buffer.from(bytes).toString('base64'));
  }
});

test('bad format, empty data, and oversized photo never get encoded', () => {
  for (const result of [
    { data: new ArrayBuffer(1), mimeType: 'image/webp' },
    { data: new ArrayBuffer(0), mimeType: 'image/jpeg' },
    { data: new ArrayBuffer(MAX_PHOTO_BYTES + 1), mimeType: 'image/jpeg' },
    { data: 'not-binary', mimeType: 'image/jpeg' },
  ]) assert.throws(() => encodePhoto(result));
});

test('0.18 Promise camera uses one foreground still capture with bounded quality', async () => {
  const photo = { data: new ArrayBuffer(1), mimeType: 'image/jpeg' };
  let options, calls = 0;
  const host = { media: { createCameraContext() { return { takePhoto(value) { options = value; calls++; return Promise.resolve(photo); } }; } } };
  assert.equal(await capturePhoto(host), photo);
  assert.equal(calls, 1);
  assert.equal(options.quality, 'normal');
  assert.equal(options.mode, 'telephoto');
  assert.equal(options.enableSystemPreview, false);
});

test('callback camera and duplicate Promise resolution still capture only once', async () => {
  const photo = { data: new ArrayBuffer(1), mimeType: 'image/png' };
  let calls = 0;
  const host = { createCameraContext() { return { takePhoto(options) { calls++; options.success(photo); return Promise.resolve({ wrong: true }); } }; } };
  assert.equal(await capturePhoto(host), photo);
  assert.equal(calls, 1);
});

test('abort rejects pending capture and ignores callbacks without a second capture', async () => {
  let callback, calls = 0;
  const host = { createCameraContext() { return { takePhoto(options) { calls++; callback = options.success; } }; } };
  const abort = new AbortController();
  const pending = capturePhoto(host, abort.signal);
  abort.abort();
  callback({ data: new ArrayBuffer(1), mimeType: 'image/jpeg' });
  await assert.rejects(pending, error => error.photoKind === 'cancelled');
  assert.equal(calls, 1);
  await assert.rejects(capturePhoto(host, abort.signal), error => error.photoKind === 'cancelled');
  assert.equal(calls, 1);
});

test('permission and unsupported host failures expose fixed categories only', async () => {
  await assert.rejects(capturePhoto(null), error => error.photoKind === 'unavailable');
  await assert.rejects(capturePhoto({ createCameraContext() { throw new Error('Permission denied: private detail'); } }), error => error.photoKind === 'permission' && !error.message.includes('private detail'));
});

test('interaction gate failure is separate from camera permission and retains no host detail', async () => {
  let capturedError;
  try {
    await capturePhoto({ media: { createCameraContext() { return { takePhoto() { throw new Error('Interactive call required: private detail'); } }; } } });
  } catch (error) { capturedError = error; }
  assert.equal(capturedError.photoKind, 'interaction');
  assert.match(photoFailureNotice(capturedError), /真机确认/);
  assert.doesNotMatch(photoFailureNotice(capturedError), /private detail|相机权限/);
});
