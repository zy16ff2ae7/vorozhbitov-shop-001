const test = require('node:test');
const assert = require('node:assert/strict');
const { createClient } = require('./miniapp/checkout.js');
function fixture(fetch) {
  const storage = new Map();
  const options = { fetch, read: key => storage.get(key), write: (key, value) => storage.set(key, value) };
  return { storage, options, client: createClient(options) };
}
const payload = { consent: true, customer: { phone: '+79990000000' }, items: [{ product_id: 'tee', size: 'M' }] };
const ok = () => ({ ok: true, status: 200, json: async () => ({ ok: true, payment_id: 'payment' }) });
test('network failure retains payload key across reload and retry', async () => {
  const bodies = [];
  const f = fixture(async (_, options) => { bodies.push(JSON.parse(options.body)); if (bodies.length === 1) throw new TypeError('offline'); return ok(); });
  await assert.rejects(f.client.submit(payload, 'signed', '42'), /Корзина сохранена/);
  const restarted = createClient(f.options);
  assert.equal((await restarted.submit(payload, 'fresh-signed', '42')).payment_id, 'payment');
  assert.equal(bodies[0].request_id, bodies[1].request_id);
  assert.equal(f.storage.get('vorozhbitov_pending_checkout:42'), null);
});
test('unauthorized checkout retains pending request and never reports success', async () => {
  const f = fixture(async () => ({ status: 401, ok: false, json: async () => ({}) }));
  await assert.rejects(f.client.submit(payload, 'expired', '42'), /Сессия истекла/);
  assert.ok(f.storage.get('vorozhbitov_pending_checkout:42'));
});
test('missing Telegram session does not submit a demo order', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return ok(); });
  await assert.rejects(f.client.submit(payload, ''), /Открой витрину/);
  assert.equal(calls, 0);
});
test('waitlist uses authenticated HTTP and rejects unconfirmed response', async () => {
  const f = fixture(async (url, options) => {
    assert.equal(url, '/api/waitlist');
    assert.equal(options.headers['X-Telegram-Init-Data'], 'signed');
    return { status: 400, ok: false, json: async () => ({ error: 'Размер недоступен' }) };
  });
  await assert.rejects(f.client.waitlist({ product_id: 'tee', size: 'M' }, 'signed'), /Размер недоступен/);
});
test('changed cart gets a distinct key and users do not share pending checkouts', async () => {
  const bodies = [];
  const f = fixture(async (_, options) => { bodies.push(JSON.parse(options.body)); throw new TypeError('offline'); });
  await assert.rejects(f.client.submit(payload, 'signed', '42'));
  await assert.rejects(f.client.submit({ ...payload, items: [{ product_id: 'hoodie', size: 'M' }] }, 'signed', '42'));
  await assert.rejects(f.client.submit(payload, 'signed', '43'));
  assert.equal(new Set(bodies.map(x => x.request_id)).size, 3);
});
test('timeout aborts request and keeps its key for retry', async () => {
  const f = fixture((_, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError')));
  }));
  const client = createClient({ ...f.options, timeoutMs: 5 });
  await assert.rejects(client.submit(payload, 'signed', '42'), /Корзина сохранена/);
  assert.ok(f.storage.get('vorozhbitov_pending_checkout:42'));
});
