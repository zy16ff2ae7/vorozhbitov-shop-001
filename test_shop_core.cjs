const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('./miniapp/shop-core.js');

test('limited editions remain available when they have sizes', () => {
  assert.equal(core.available({badge:'ЛИМИТ', stock_label:'Осталось мало', sizes:['M']}), true);
  assert.equal(core.available({sizes:[]}), false);
  assert.equal(core.available({active:false, sizes:['M']}), false);
});

test('size filter includes all actual sizes, including XXL and one-size', () => {
  assert.deepEqual(core.sizesFor([{sizes:['XXL','S','M']},{sizes:['ОДИН','S']},{active:false,sizes:['XS']}]), ['S','M','XXL','ОДИН']);
});

test('corrupt persisted data cannot break startup', () => {
  for (const value of [null, 0, 'oops', {}, false]) {
    assert.deepEqual(core.cleanCart(value), []);
    assert.deepEqual(core.stringList(value), []);
    assert.equal(core.cleanProfile(value).deliver, 'СДЭК');
  }
  assert.equal(core.cleanProfile({name:{},phone:900,city:'Москва'}).name, '');
  assert.equal(core.cleanProfile({name:{},phone:900,city:'Москва'}).city, 'Москва');
});

test('cart respects server quantity limit and merges duplicate persisted rows', () => {
  assert.deepEqual(core.cleanCart([{id:'tee',size:'M',qty:18},{id:'tee',size:'M',qty:8}]), [{id:'tee',size:'M',qty:20,key:'tee::M',person:''}]);
  assert.equal(core.cleanCart([{id:'tee',size:'M',qty:-2},{id:'tee',size:'M',qty:'x'}]).length, 0);
});

test('personalized products keep independent quantities', () => {
  const rows = core.cleanCart([{id:'tag',size:'ОДИН',qty:2,person:'001'},{id:'tag',size:'ОДИН',qty:1,person:'002'}]);
  assert.equal(rows.length, 2);
  assert.notEqual(rows[0].key, rows[1].key);
});

test('catalog count has correct Russian forms', () => {
  assert.equal(core.productCount(1), '01 ВЕЩЬ');
  assert.equal(core.productCount(3), '03 ВЕЩИ');
  assert.equal(core.productCount(11), '11 ВЕЩЕЙ');
  assert.equal(core.productCount(21), '21 ВЕЩЬ');
});

function fakeStorage() {
  const entries = new Map();
  return {get length(){return entries.size},key:i=>[...entries.keys()][i],getItem:k=>entries.get(k)??null,setItem:(k,v)=>entries.set(k,v),removeItem:k=>entries.delete(k)};
}
test('only the server price number is billable; decimal labels never become digits',()=>{
  const Core=require('./miniapp/shop-core.js');
  const product={active:true,sizes:['M'],price:'4 900,50 ₽',price_rub:4900};
  assert.equal(Core.priceRub(product),4900);assert.equal(Core.orderable(product),true);
  for(const value of [undefined,'4900',NaN,Infinity,0,-1,4900.5]){
    assert.equal(Core.priceRub({...product,price_rub:value}),0);
    assert.equal(Core.orderable({...product,price_rub:value}),false);
  }
});
test('personal caches are isolated by owner and never inherit shared or preview profiles',()=>{
  const Core=require('./miniapp/shop-core.js'), raw=fakeStorage();
  const preview=Core.createOwnedStorage(()=>raw,'preview'),a=Core.createOwnedStorage(()=>raw,'user:42'),b=Core.createOwnedStorage(()=>raw,'user:43');
  raw.setItem('vorozhbitov_profile',JSON.stringify({phone:'legacy'}));
  preview.write('vorozhbitov_profile',{phone:'anonymous'});
  assert.equal(a.read('vorozhbitov_profile'),null);
  for(const key of ['profile','cart','orders','saved','viewed']){
    a.write('vorozhbitov_'+key,{private:'A'});assert.equal(b.read('vorozhbitov_'+key),null);
  }
  b.write('vorozhbitov_profile',{phone:'B'});assert.equal(a.clear(),true);
  assert.equal(a.read('vorozhbitov_profile'),null);assert.deepEqual(b.read('vorozhbitov_profile'),{phone:'B'});
  assert.equal(raw.getItem('vorozhbitov_profile'),null);
  assert.deepEqual(preview.read('vorozhbitov_profile'),{phone:'anonymous'});
});
test('only an owner-qualified legacy pending checkout migrates',()=>{
  const {createOwnedStorage}=require('./miniapp/shop-core.js'), raw=fakeStorage();
  const key='vorozhbitov_pending_checkout:42', pending={fingerprint:'{}',request_id:'web-old'};
  raw.setItem(key,JSON.stringify(pending));raw.setItem('vorozhbitov_profile','{"phone":"private"}');
  const a=createOwnedStorage(()=>raw,'user:42'),b=createOwnedStorage(()=>raw,'user:43');
  assert.equal(b.read(key),null);assert.deepEqual(a.read(key),pending);
  assert.equal(raw.getItem(key),null);assert.equal(a.read('vorozhbitov_profile'),null);
  a.write(key,null);assert.equal(a.read(key),null);
});
test('denied and quota-limited storage preserve the latest session state',()=>{
  const {createOwnedStorage}=require('./miniapp/shop-core.js'),raw=fakeStorage();
  const a=createOwnedStorage(()=>raw,'user:42');a.write('pending',{id:'old'});
  raw.setItem=()=>{throw new Error('quota')};a.write('pending',{id:'new'});
  assert.deepEqual(a.read('pending'),{id:'new'});
  const denied=createOwnedStorage(()=>{throw new Error('denied')},'user:42');
  denied.write('pending',{id:'session'});assert.deepEqual(denied.read('pending'),{id:'session'});
});
test('payment UI waits for server settlement and prioritizes review over paid order rows',()=>{
  const {paymentOutcome}=require('./miniapp/shop-core.js');
  assert.equal(paymentOutcome([{payment_id:'p',status:'awaiting_payment',payment_status:'pending'}],'p'),'pending');
  assert.equal(paymentOutcome([{payment_id:'p',status:'confirmed',payment_status:'paid'}],'p'),'paid');
  assert.equal(paymentOutcome([{payment_id:'p',status:'paid',payment_status:'refund_required'}],'p'),'review');
  assert.equal(paymentOutcome([{payment_id:'p',status:'paid',payment_status:'paid',payment_attention:'extra charge'}],'p'),'review');
  assert.equal(paymentOutcome([{payment_id:'p',status:'awaiting_payment',payment_status:'review_required'}],'p'),'review');
  assert.equal(paymentOutcome([{payment_id:'old',status:'paid',payment_status:'paid'}],'new'),'pending');
});

test('unknown stock stays a draft, never a confirmed availability claim',()=>{
  const product={active:true,sizes:['M'],price_rub:4900,inventory:{M:{available:null}}};
  assert.equal(core.available(product),false);assert.equal(core.orderable(product),true);
  assert.equal(core.stockLabel(product,'M'),'Наличие уточняется');
  assert.equal(core.stockLabel({...product,inventory:{M:{available:0}}},'M'),'нет в наличии');
  assert.equal(core.stockLabel({...product,inventory:{M:{available:1}}},'M'),'доступно 1 шт.');
  assert.equal(core.paymentOutcome([{payment_id:'p',payment_status:'expired'}],'p'),'cancelled');
});

test('an older checkout success keeps a newer local cart or pending request',()=>{
  const cart=[{id:'tee',size:'M',qty:1}];
  assert.equal(core.keepNewerCart(cart,cart,null,'a','a'),false);
  assert.equal(core.keepNewerCart(cart,[{...cart[0],qty:2}],null,'a','a'),true);
  assert.equal(core.keepNewerCart(cart,cart,{request_id:'newer'},'a','a'),true);
  assert.equal(core.keepNewerCart(cart,cart,null,'a','b'),true);
});
