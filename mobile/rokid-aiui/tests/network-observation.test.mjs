import test from 'node:test';
import assert from 'node:assert/strict';
import { safeNetworkObservation, formatNetworkObservation } from '../lib/assistant.js';

const wrapped = (details, ownFields = {}) => Object.assign(
  new Error(`Request failed: ${JSON.stringify(details)}`), ownFields,
);
const empty = { reason: 'OTHER', wrappedObject: false, codes: {} };
const safeFormat = /^(?:DOMAIN_HINT|PERMISSION_HINT|TLS_HINT|OFFLINE_HINT|DNS_HINT|ABORT_HINT|TIMEOUT_HINT|OTHER)\/W[01]\/C(?:-|-?\d+)\/E(?:-|-?\d+)\/N(?:-|-?\d+)$/;

test('wrapped native failures become fixed hints and retain only supported numeric fields', () => {
  const cases = [
    ['url not in domain list', 'DOMAIN_HINT'],
    ['permission denied for network', 'PERMISSION_HINT'],
    ['SSL certificate verify failed', 'TLS_HINT'],
    ['network is unavailable', 'OFFLINE_HINT'],
    ['UnknownHostException', 'DNS_HINT'],
    ['AbortError: cancelled', 'ABORT_HINT'],
    ['TimeoutError: deadline', 'TIMEOUT_HINT'],
  ];
  for (const [errMsg, reason] of cases) {
    const error = wrapped({ errMsg, code: 17, errorCode: -1001, errno: -42, status: 503 });
    assert.deepEqual(safeNetworkObservation(error), {
      reason, wrappedObject: true, codes: { code: 17, errorCode: -1001, errno: -42 },
    });
    assert.equal(formatNetworkObservation(error), `${reason}/W1/C17/E-1001/N-42`);
  }
});

test('throwing getters and revoked proxies cannot escape the safe observation boundary', () => {
  const hostile = Object.defineProperties({}, {
    message: { get() { throw new Error('PRIVATE_GETTER_MESSAGE'); } },
    errMsg: { get() { throw new Error('PRIVATE_GETTER_DETAIL'); } },
    code: { get() { throw new Error('PRIVATE_GETTER_CODE'); } },
    errorCode: { value: 13 },
  });
  assert.deepEqual(safeNetworkObservation(hostile), { ...empty, codes: { errorCode: 13 } });
  assert.equal(formatNetworkObservation(hostile), 'OTHER/W0/C-/E13/N-');

  const { proxy, revoke } = Proxy.revocable({}, {});
  revoke();
  assert.deepEqual(safeNetworkObservation(proxy), empty);
  assert.equal(formatNetworkObservation(proxy), 'OTHER/W0/C-/E-/N-');
});

test('inherited messages and codes are ignored while own fields remain usable', () => {
  const inherited = Object.create({ message: 'permission denied', errMsg: 'SSL failure', code: 20, errno: -42 });
  assert.deepEqual(safeNetworkObservation(inherited), empty);
  inherited.message = 'Request failed: {"errMsg":"generic","code":17}';
  inherited.errorCode = 0;
  assert.deepEqual(safeNetworkObservation(inherited), {
    reason: 'OTHER', wrappedObject: true, codes: { code: 17, errorCode: 0 },
  });
  assert.equal(formatNetworkObservation(inherited), 'OTHER/W1/C17/E0/N-');
});

test('the 4096-character limit is inclusive and oversized messages are discarded', () => {
  const base = 'Request failed: ' + JSON.stringify({ errMsg: 'permission denied ', code: 17 });
  const message = 'Request failed: ' + JSON.stringify({
    errMsg: 'permission denied ' + 'x'.repeat(4096 - base.length), code: 17,
  });
  assert.equal(message.length, 4096);
  assert.deepEqual(safeNetworkObservation(new Error(message)), {
    reason: 'PERMISSION_HINT', wrappedObject: true, codes: { code: 17 },
  });
  assert.deepEqual(safeNetworkObservation(new Error(message + ' ')), empty);
  assert.deepEqual(safeNetworkObservation({ errMsg: 'permission denied ' + 'x'.repeat(4096) }), empty);
});

test('malformed and non-object wrappers do not supply codes or claim successful decoding', () => {
  for (const message of [
    'Request failed: {"errMsg":"generic", code:17}',
    'Request failed: [{"code":17}]',
    'Request failed: null',
    'Request failed: 17',
    'Request failed: "generic"',
    '{"message":"generic","code":17}',
    'Different prefix: {"message":"generic","code":17}',
  ]) {
    const error = new Error(message);
    assert.deepEqual(safeNetworkObservation(error), empty, message);
    assert.equal(formatNetworkObservation(error), 'OTHER/W0/C-/E-/N-');
  }
  for (const error of [null, undefined, 'permission denied', 17, true]) {
    assert.deepEqual(safeNetworkObservation(error), empty);
  }
});

test('multiple keywords select the declared hint priority without returning raw detail', () => {
  const cases = [
    ['url not allowed; permission denied; SSL; offline; DNS; aborted; timeout', 'DOMAIN_HINT'],
    ['permission denied; SSL; offline; DNS; aborted; timeout', 'PERMISSION_HINT'],
    ['SSL; offline; DNS; aborted; timeout', 'TLS_HINT'],
    ['offline; DNS; aborted; timeout', 'OFFLINE_HINT'],
    ['DNS; aborted; timeout', 'DNS_HINT'],
    ['aborted; timeout', 'ABORT_HINT'],
    ['timeout', 'TIMEOUT_HINT'],
  ];
  for (const [errMsg, reason] of cases) {
    assert.equal(safeNetworkObservation(wrapped({ errMsg })).reason, reason);
  }
});

test('wrapped details take precedence over outer detail using bounded string values only', () => {
  const cases = [
    [wrapped({ errMsg: 'permission denied', message: 'SSL failure' }, { errMsg: 'offline' }), 'PERMISSION_HINT'],
    [wrapped({ errMsg: '', message: 'SSL failure' }, { errMsg: 'offline' }), 'TLS_HINT'],
    [wrapped({ errMsg: 17, message: {} }, { errMsg: 'offline' }), 'OFFLINE_HINT'],
    [Object.assign(new Error('DNS lookup failed'), { errMsg: 'aborted' }), 'ABORT_HINT'],
  ];
  for (const [error, reason] of cases) assert.equal(safeNetworkObservation(error).reason, reason);
});

test('numeric fields require bounded safe integers and preserve zero and signed endpoints', () => {
  const accepted = wrapped({ code: 0, errorCode: 1000000, errno: -1000000 });
  assert.deepEqual(safeNetworkObservation(accepted).codes, { code: 0, errorCode: 1000000, errno: -1000000 });
  assert.equal(formatNetworkObservation(accepted), 'OTHER/W1/C0/E1000000/N-1000000');
  for (const value of [NaN, Infinity, -Infinity, 20.1, 1000001, -1000001, Number.MAX_SAFE_INTEGER + 1, '20', true, null, 20n, {}]) {
    const error = Object.assign(new Error('generic'), { code: value, errorCode: value, errno: value });
    assert.deepEqual(safeNetworkObservation(error).codes, {});
    assert.equal(formatNetworkObservation(error), 'OTHER/W0/C-/E-/N-');
  }
  assert.deepEqual(safeNetworkObservation(wrapped({ code: '17', errorCode: 1.5, errno: 1000001 })).codes, {});
});

test('valid outer numeric codes override wrapped codes independently and invalid values fall back', () => {
  const error = wrapped({ code: 17, errorCode: -1001, errno: -42 }, {
    code: 0, errorCode: 'PRIVATE_STRING_CODE', errno: Infinity,
  });
  assert.deepEqual(safeNetworkObservation(error).codes, { code: 0, errorCode: -1001, errno: -42 });
  assert.equal(formatNetworkObservation(error), 'OTHER/W1/C0/E-1001/N-42');
});

test('sentinels in wrapped and outer errors never appear in the observation or formatted output', () => {
  const sentinel = 'SENTINEL_KEEP_PRIVATE_8Z4Q';
  const error = wrapped({
    errMsg: `permission denied ${sentinel}`,
    message: sentinel,
    code: sentinel,
    errorCode: 17,
    errno: `17 ${sentinel}`,
    url: `https://example.invalid/${sentinel}?token=${sentinel}`,
    body: sentinel,
    headers: { Authorization: `Bearer ${sentinel}` },
  }, { name: sentinel, stack: sentinel, code: sentinel, token: sentinel });
  const observed = safeNetworkObservation(error);
  const formatted = formatNetworkObservation(error);
  assert.deepEqual(observed, { reason: 'PERMISSION_HINT', wrappedObject: true, codes: { errorCode: 17 } });
  assert.match(formatted, safeFormat);
  assert.equal(JSON.stringify(observed).includes(sentinel), false);
  assert.equal(formatted.includes(sentinel), false);
  assert.doesNotMatch(formatted, /https:|Bearer|Authorization|token|headers|body/);
});
