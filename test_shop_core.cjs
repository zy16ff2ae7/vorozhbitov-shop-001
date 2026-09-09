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
