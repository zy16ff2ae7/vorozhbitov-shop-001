const test = require('node:test');
const assert = require('node:assert/strict');
const { createSync, canonical } = require('./miniapp/cart-sync.js');
const copy = x => JSON.parse(JSON.stringify(x));
const items = [{ product_id: 'tee', size: 'M', quantity: 1, person: '' }];
function fixture({ local = [], remote = { revision: 0, items: [] }, base = null, pending = null } = {}) {
  const f = { local: copy(local), remote: copy(remote), base: copy(base), pending, posts: [], mode: '', operations: new Map() };
  f.request = async (method, body) => {
    if (method === 'GET') return { cart: copy(f.remote) };
    f.posts.push(copy(body));
    if (f.operations.has(body.operation_id)) return { cart: copy(f.remote) };
    if (body.revision !== f.remote.revision) throw Object.assign(new Error('Корзина изменилась'), {code:'cart_conflict',cart:copy(f.remote),status:409});
    f.operations.set(body.operation_id, copy(body));
    f.remote = { revision: f.remote.revision + 1, items: canonical(body.items) };
    return { cart: copy(f.remote) };
  };
  f.make = () => createSync({ request: (...args) => f.request(...args), getLocal: () => f.local,
    setLocal: x => { f.local = copy(x); }, readBase: () => f.base, writeBase: x => { f.base = copy(x); },
    pending: () => f.pending, notify: mode => { f.mode = mode; } });
  f.sync = f.make(); return f;
}

test('a native cart hydrates an empty Mini App without a write', async () => {
  const f = fixture({remote:{revision:3,items}}); await f.sync.load();
  assert.deepEqual(f.local,items); assert.equal(f.posts.length,0); assert.equal(f.mode,'ready');
});
test('an unsynced old local cart initializes only an empty revision-zero account', async () => {
  const f = fixture({local:items}); await f.sync.load();
  assert.equal(f.posts.length,1); assert.equal(f.remote.revision,1); assert.equal(f.remote.items[0].quantity,1);
});
test('unchanged device cache adopts newer native changes', async () => {
  const f = fixture({local:items,base:{revision:1,items},remote:{revision:2,items:[]}});
  await f.sync.load(); assert.deepEqual(f.local,[]); assert.equal(f.posts.length,0);
});
test('offline edits and native edits conflict instead of silently replacing either', async () => {
  const changed = [{...items[0],quantity:2}];
  const f = fixture({local:changed,base:{revision:1,items},remote:{revision:2,items:[]}});
  await f.sync.load(); assert.equal(f.mode,'conflict'); assert.deepEqual(f.local,changed); assert.equal(f.posts.length,0);
  await assert.rejects(f.sync.beforeCheckout({items:changed}), /изменилась/);
  await f.sync.load('remote'); assert.deepEqual(f.local,[]); assert.equal(f.mode,'ready');
});
test('explicit keep-local CAS uses the conflict actually displayed, not a fresh unseen revision', async () => {
  const changed = [{...items[0],quantity:2}];
  const f = fixture({local:changed,base:{revision:1,items},remote:{revision:2,items:[]}});
  await f.sync.load(); f.remote={revision:3,items:[{...items[0],quantity:3}]};
  await assert.rejects(f.sync.load('local'), /изменилась/);
  assert.equal(f.remote.items[0].quantity,3); assert.equal(f.posts.at(-1).revision,2);
});
test('lost cart reply retries the same durable operation after reload', async () => {
  const f = fixture({local:items}); const original=f.request; let lost=true;
  f.request=async (...args) => { const result=await original(...args); if(args[0]==='POST'&&lost){lost=false;throw new TypeError('lost response')} return result; };
  await assert.rejects(f.sync.load()); assert.equal(f.remote.revision,1); assert.ok(f.base.mutation);
  f.sync=f.make(); await f.sync.load(); assert.equal(f.remote.revision,1); assert.deepEqual(f.posts[0],f.posts[1]); assert.equal(f.mode,'ready');
});
test('replaying a committed cart write never overwrites a subsequent device edit', async () => {
  const f=fixture({local:items}); const original=f.request; let lost=true;
  f.request=async(...args)=>{const result=await original(...args); if(args[0]==='POST'&&lost){lost=false;throw new TypeError('lost')}return result};
  await assert.rejects(f.sync.load()); f.remote={revision:2,items:[{...items[0],size:'L'}]};
  f.sync=f.make(); await assert.rejects(f.sync.load()); assert.equal(f.mode,'conflict'); assert.equal(f.remote.items[0].size,'L');
});
test('checkout pending freezes cart hydration and keeps its original revision', async () => {
  const payload={items,customer:{phone:'fake'}};
  const f=fixture({local:items,base:{revision:1,items},remote:{revision:2,items:[]},pending:{fingerprint:JSON.stringify(payload),request_id:'stable',cart_revision:1}});
  await f.sync.load(); assert.equal(f.mode,'frozen'); assert.deepEqual(f.local,items);
  assert.equal(await f.sync.beforeCheckout(payload),1); assert.equal(f.posts.length,0);
});
test('legacy pending without a cart revision keeps its original transport contract', async () => {
  const payload={items}; const f=fixture({local:items,remote:{revision:12,items:[]},pending:{fingerprint:JSON.stringify(payload),request_id:'old'}});
  await f.sync.load(); assert.equal(await f.sync.beforeCheckout(payload),undefined); assert.equal(f.posts.length,0);
});
test('checkout success fetches remote state and never sends a stale clear', async () => {
  const f=fixture({local:items});await f.sync.load();
  f.local=[]; f.remote={revision:3,items:[{...items[0],size:'L'}]};
  await f.sync.afterCheckout(); assert.equal(f.local[0].size,'L'); assert.equal(f.posts.length,1);
});
test('concurrent UI edits are serialized to the latest quantity', async () => {
  const f=fixture(); await f.sync.load(); const request=f.request;let release;
  f.request=(method,body)=>method==='POST'?new Promise(resolve=>{release=async()=>resolve(await request(method,body))}):request(method,body);
  f.local=items;const first=f.sync.changed(); f.local=[{...items[0],quantity:2}];
  const second=f.sync.changed(); await release();
  await new Promise(resolve=>setImmediate(resolve)); await release(); await Promise.all([first,second]);
  assert.equal(f.remote.items[0].quantity,2); assert.deepEqual(f.posts.map(x=>x.revision),[0,1]);
});
test('a slow older GET cannot restore stale cart data over a successful write', async () => {
  const f=fixture({local:items});const original=f.request,gets=[];
  f.request=(method,body)=>method==='GET'?new Promise(resolve=>gets.push(resolve)):original(method,body);
  const old=f.sync.load(),newer=f.sync.load(); gets[1]({cart:{revision:0,items:[]}}); await newer;
  assert.equal(f.remote.revision,1); gets[0]({cart:{revision:0,items:[]}}); await old;
  assert.deepEqual(f.local,items); assert.equal(f.base.revision,1);
});
test('a definitively invalid cart request can be corrected without replaying it forever', async () => {
  const f=fixture();await f.sync.load();const request=f.request;let invalid=true;
  f.request=async(...args)=>{if(invalid&&args[0]==='POST'){invalid=false;throw Object.assign(new Error('unavailable'),{status:400})}return request(...args)};
  f.local=items;await assert.rejects(f.sync.changed()); assert.equal(f.base.mutation,null);
  f.local=[{...items[0],size:'L'}]; await f.sync.changed(); assert.equal(f.remote.items[0].size,'L');
});

test('after checkout a newer unsynced local edit is kept, not hydrated to empty',async()=>{
  const f=fixture({local:items});await f.sync.load();
  f.local=[{...items[0],quantity:2}];f.remote={revision:2,items:[]};
  await f.sync.afterCheckout(true);
  assert.equal(f.local[0].quantity,2);assert.equal(f.mode,'conflict');assert.equal(f.posts.length,1);
});

test('promotion metadata is live owner state, never an inferred or persisted discount',async()=>{
  const promotion={code:'TEST10',promotion_id:1,promotion_version:1,native_checkout:true};
  const f=fixture({remote:{revision:2,items,promotion}});await f.sync.load();
  assert.deepEqual(f.sync.remote().promotion,promotion);assert.equal(f.posts.length,0);
  assert.equal(Object.hasOwn(f.base,'promotion'),false);
  f.remote={revision:3,items,promotion:null};await f.sync.load();
  assert.equal(f.sync.remote().promotion,null);assert.equal(f.posts.length,0);
});
test('new native promotion never overwrites the CAS version of an old lost checkout',async()=>{
  const payload={items};const f=fixture({local:items,base:{revision:1,items},remote:{revision:4,items,promotion:{code:'NEW',native_checkout:true}},pending:{fingerprint:JSON.stringify(payload),request_id:'old',cart_revision:1}});
  await f.sync.load();assert.equal(f.mode,'frozen');assert.equal(await f.sync.beforeCheckout(payload),1);
  assert.equal(f.posts.length,0);assert.equal(f.sync.remote().promotion.code,'NEW');
});
