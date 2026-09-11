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

test('blocked storage still reuses the in-session request after network failure',async()=>{
  const bodies=[];
  const client=createClient({fetch:async(_,o)=>{bodies.push(JSON.parse(o.body));throw new TypeError('offline')},
    read:()=>({fingerprint:'old',request_id:'web-old'}),write:()=>{throw new Error('denied')}});
  await assert.rejects(client.submit(payload,'signed','42'));await assert.rejects(client.submit(payload,'signed','42'));
  assert.deepEqual(bodies[0],bodies[1]);
});
test('an older success cannot clear a newer unresolved checkout',async()=>{
  const releases=[];
  const f=fixture(()=>new Promise(resolve=>releases.push(resolve)));
  const old=f.client.submit(payload,'signed','42');
  const newer=f.client.submit({...payload,items:[{product_id:'tag',size:'ONE SIZE'}]},'signed','42');
  const pending=f.storage.get('vorozhbitov_pending_checkout:42');
  releases[0](ok());await old;
  assert.deepEqual(f.storage.get('vorozhbitov_pending_checkout:42'),pending);
  releases[1](ok());await newer;assert.equal(f.storage.get('vorozhbitov_pending_checkout:42'),null);
});

test('checkout revision is frozen with the retry key even if remote cart changed', async()=>{
  const bodies=[];const f=fixture(async(_,options)=>{bodies.push(JSON.parse(options.body));throw new TypeError('lost')});
  await assert.rejects(f.client.submit(payload,'signed','42',3));
  const restarted=createClient(f.options);
  await assert.rejects(restarted.submit(payload,'signed','42',4));
  assert.deepEqual(bodies[0],bodies[1]);assert.equal(bodies[1].cart_revision,3);
});
test('legacy pending checkout never acquires new sync metadata on retry', async()=>{
  const bodies=[];const f=fixture(async(_,options)=>{bodies.push(JSON.parse(options.body));return ok()});
  f.storage.set('vorozhbitov_pending_checkout:42',{fingerprint:JSON.stringify(payload),request_id:'legacy'});
  await f.client.submit(payload,'signed','42',8);
  assert.equal(bodies[0].request_id,'legacy');assert.equal(Object.hasOwn(bodies[0],'cart_revision'),false);
});
test('successfully missing pending storage does not resurrect stale memory',async()=>{
  const f=fixture(async()=>{throw new TypeError('offline')});
  await assert.rejects(f.client.submit(payload,'signed','42',1));
  f.storage.delete('vorozhbitov_pending_checkout:42');
  assert.equal(f.client.peek('42'),null);
});

test('an explicit stale cart revision rejection can be resolved with a new intent',async()=>{
  const bodies=[];let stale=true;
  const f=fixture(async(_,options)=>{bodies.push(JSON.parse(options.body));return stale?{status:400,ok:false,json:async()=>({error:'Cart changed',code:'cart_revision_conflict'})}:ok()});
  await assert.rejects(f.client.submit(payload,'signed','42',1));
  assert.equal(f.client.peek('42'),null);stale=false;
  await f.client.submit(payload,'signed','42',3);
  assert.notEqual(bodies[0].request_id,bodies[1].request_id);assert.equal(bodies[1].cart_revision,3);
});

test('explicit promotion rejection retires only an uncommitted matching retry',async()=>{
  for(const code of ['promotion_requires_chat','promotion_changed']){
    const f=fixture(async()=>({status:400,ok:false,json:async()=>({error:'Check code in bot',code})}));
    await assert.rejects(f.client.submit(payload,'signed','42',3),/Check code/);
    assert.equal(f.client.peek('42'),null);
  }
});
test('a delayed promotion rejection cannot erase another newer unresolved checkout',async()=>{
  const releases=[];const f=fixture(()=>new Promise(resolve=>releases.push(resolve)));
  const old=f.client.submit(payload,'signed','42',3);
  const newer=f.client.submit({...payload,items:[{product_id:'tag',size:'ONE SIZE'}]},'signed','42',4);
  const pending=f.client.peek('42');
  releases[0]({status:400,ok:false,json:async()=>({error:'Code changed',code:'promotion_changed'})});
  await assert.rejects(old);assert.deepEqual(f.client.peek('42'),pending);
  releases[1](ok());await newer;
});
