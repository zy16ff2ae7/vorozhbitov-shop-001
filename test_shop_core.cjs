const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('./miniapp/shop-core.js');

test('limited editions remain available when they have sizes', () => {
  assert.equal(core.available({badge:'ЛИМИТ', stock_label:'Осталось мало', price:'4 900 ₽', sizes:['M']}), true);
  assert.equal(core.available({price:'4 900 ₽', sizes:[]}), false);
  assert.equal(core.available({active:false, price:'4 900 ₽', sizes:['M']}), false);
  assert.equal(core.available({gift:true, price:'0 ₽', sizes:['ONE SIZE']}), false);
});

test('browser and server share one whole-ruble price contract', () => {
  assert.equal(core.priceNumber('4 900 ₽'), 4900);
  assert.equal(core.priceNumber('4 900.00 ₽'), 4900);
  assert.equal(core.priceNumber('от 3 200 руб'), 3200);
  for (const value of ['4 900,50 ₽', '4 900.51 ₽', '4.900 ₽', '1 200 - 1 500 ₽']) {
    assert.ok(Number.isNaN(core.priceNumber(value)), value);
  }
});

test('size filter includes all actual sizes, including XXL and one-size', () => {
  assert.deepEqual(core.sizesFor([{price:'1 ₽',sizes:['XXL','S','M']},{price:'1 ₽',sizes:['ОДИН','S']},{active:false,price:'1 ₽',sizes:['XS']},{gift:true,price:'0 ₽',sizes:['ONE SIZE']}]), ['S','M','XXL','ОДИН']);
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
