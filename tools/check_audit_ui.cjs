/* Real pointer and checkout regressions. Use tools/ui_test_server.py, not production. */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const profile = {name:'Тестовый покупатель',phone:'+79990000000',city:'Москва',address:'',entrance:'',note:'',deliver:'СДЭК'};
const cart = [{id:'tee-sila-i-chest',size:'M',qty:1}];
function signedData(id) {
  const fields = {auth_date: String(Math.floor(Date.now()/1000)), user: JSON.stringify({id,first_name:`UI ${id}`})};
  const key = crypto.createHmac('sha256','WebAppData').update('ui-test-token').digest();
  const hash = crypto.createHmac('sha256',key).update(Object.keys(fields).sort().map(k=>`${k}=${fields[k]}`).join('\n')).digest('hex');
  return new URLSearchParams({...fields,hash}).toString();
}
module.exports = async function ({browser,check,errors,out,enter}) {
  async function closeContext(c) {
    // Stop intercepting new polls, then drain active route.fetch callbacks.
    // Disposing their API request context early causes an unhandled teardown
    // rejection, unrelated to the assertions; do not ignore handler errors.
    for (const page of c.pages()) await page.unrouteAll({behavior:'wait'});
    await c.unrouteAll({behavior:'wait'});
    await c.close();
  }
  async function context() {
    const c = await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
    await c.route('https://telegram.org/**', r=>r.fulfill({status:200,body:''}));
    return c;
  }
  async function pageFor(c, {user=990,seed=true,savedProfile=profile}={}) {
    const p = await c.newPage();
    p.on('pageerror',e=>errors.push(e.message));
    await p.addInitScript(({user,seed,cart,profile,initData})=>{
      if (user !== null) window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:user,first_name:`UI ${user}`}},ready(){},expand(){}}};
      const scope = user === null ? 'preview' : `user:${user}`;
      if (seed && !sessionStorage.getItem(`seed:${scope}`)) {
        sessionStorage.setItem(`seed:${scope}`,'1');
        localStorage.setItem(`vorozhbitov_v2:${scope}:vorozhbitov_cart`, JSON.stringify(cart));
        localStorage.setItem(`vorozhbitov_v2:${scope}:vorozhbitov_profile`, JSON.stringify(profile));
      }
    }, {user,seed,cart,profile:savedProfile,initData:user === null ? '' : signedData(user)});
    return p;
  }
  await check('cart close hit target is bounded; real coordinate clicks change quantity and remove', async()=>{
    const c=await context(), p=await pageFor(c);
    try {
      await enter(p);await p.locator('#cartButton').click();
      const plus=p.locator('#cartContent [data-qty="plus"]');await plus.scrollIntoViewIfNeeded();
      const box=await plus.boundingBox();
      const hit=await p.evaluate(box=>{
        const node=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2);
        const pseudo=getComputedStyle(document.querySelector('#cartModal .sheet-close'),'::after');
        return {action:node.closest('[data-qty]')?.dataset.qty,width:parseFloat(pseudo.width),height:parseFloat(pseudo.height)};
      },box);
      assert.equal(hit.action,'plus');assert.ok(hit.width<=56 && hit.height<=56);
      await p.mouse.click(box.x+box.width/2,box.y+box.height/2);
      assert.equal(await p.locator('#cartModal').isVisible(),true);
      assert.equal(await p.evaluate(()=>VorozhbitovShop.state.cart[0].qty),2);
      await p.locator('#cartContent [data-qty="minus"]').click();
      assert.equal(await p.evaluate(()=>VorozhbitovShop.state.cart[0].qty),1);
      await p.screenshot({path:path.join(out,'fixed-cart-hit-target.png')});
      await p.locator('#cartContent [data-remove-key]').click();
      assert.equal(await p.evaluate(()=>VorozhbitovShop.state.cart.length),0);
    } finally {await closeContext(c);}
  });
  await check('lost checkout responses and reload reuse one real server order and payment', async()=>{
    const c=await context(), p=await pageFor(c), requests=[],receipts=[];
    try {
      await p.route('**/api/checkout',async r=>{
        requests.push(r.request().postDataJSON());
        // The real helper commits to the fixture DB, then its response is lost.
        const response=await r.fetch();const receipt=await response.json();
        receipts.push({status:response.status(),...receipt});
        await r.fulfill({status:503,contentType:'application/json',body:'{"error":"Имитация потерянного ответа"}'});
      });
      await enter(p);await p.locator('#cartButton').click();
      await p.locator('#checkoutAddress').fill('Тестовая улица, 1');await p.locator('#checkoutConsent').check();
      for(let i=0;i<2;i++) {
        await p.locator('#submitOrder').click();await p.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);
        assert.equal(await p.locator('#checkoutNote').inputValue(),'');
      }
      await enter(p);await p.locator('#cartButton').click();await p.locator('#checkoutConsent').check();
      await p.locator('#submitOrder').click();await p.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);
      assert.equal(requests.length,3);assert.deepEqual(requests[0],requests[1]);assert.deepEqual(requests[0],requests[2]);
      assert.ok(receipts.every(row=>row.ok), JSON.stringify(receipts));
      assert.equal(new Set(receipts.map(row=>row.payment_id)).size,1);
      assert.ok(receipts.every(row=>row.order_ids.length===1 && row.order_ids[0]===receipts[0].order_ids[0]));
      const orders=await p.evaluate(async()=>{
        const r=await fetch('/api/my-orders',{headers:{'X-Telegram-Init-Data':Telegram.WebApp.initData}});return r.json();
      });
      assert.equal(orders.orders.length,1);assert.equal(orders.orders[0].payment_id,receipts[0].payment_id);
      fs.writeFileSync(path.join(out,'checkout-retry.json'),JSON.stringify({requests,receipts},null,2));
    } finally {await closeContext(c);}
  });
  await check('Stars widget success waits for server and review removes payment actions', async()=>{
    const c=await context(), p=await pageFor(c,{user:991});
    try {
      await enter(p);
      await p.evaluate(()=>{Telegram.WebApp.openInvoice=(_url,callback)=>callback('paid')});
      await p.locator('#cartButton').click();await p.locator('#checkoutConsent').check();await p.locator('#submitOrder').click();
      await p.locator('#payModal:not(.hidden)').waitFor();await p.locator('[data-pay-method="stars"]').click();
      assert.equal(await p.locator('#successToast').isVisible(),false);
      assert.match(await p.locator('#toast').textContent(),/Ждём подтверждения/);
      await p.route('**/api/my-orders',async r=>{
        const response=await r.fetch(),data=await response.json();
        for(const row of data.orders){row.payment_status='review_required';row.payment_attention='на сверке';row.can_pay=false;row.can_cancel=false;}
        await r.fulfill({response,json:data});
      });
      await p.locator('[data-pay-method="stars"]').click();await p.locator('#payModal').waitFor({state:'hidden'});
      assert.equal(await p.locator('#successToast').isVisible(),false);
      assert.match(await p.locator('#toast').textContent(),/на сверке/);
    } finally {await closeContext(c);}
  });
  await check('decimal price labels never drive totals or numeric sorting', async()=>{
    const c=await context(), p=await pageFor(c);
    try {
      await p.route('**/api/catalog',async r=>{
        const response=await r.fetch(),body=await response.json();
        const tee=body.products.find(row=>row.id==='tee-sila-i-chest');
        tee.price='4 900,50 ₽';assert.equal(tee.price_rub,4900);
        const tag=body.products.find(row=>row.id==='tag-sila-i-chest');tag.price='6 000 ₽';tag.price_rub=6000;
        await r.fulfill({response,json:body});
      });
      await enter(p);await p.locator('#sortSelect').selectOption('price-asc');
      assert.equal(await p.locator('.product-card').first().getAttribute('data-product-id'),'tee-sila-i-chest');
      await p.locator('#cartButton').click();assert.equal((await p.locator('#cartTotal').textContent()).replace(/\D/g,''),'4900');
      await p.locator('#cartContent [data-qty="plus"]').click();assert.equal((await p.locator('#cartTotal').textContent()).replace(/\D/g,''),'9800');
    } finally {await closeContext(c);}
  });
  await check('shared origin isolates Telegram users, preview and legacy profile; clear is owner scoped', async()=>{
    const c=await context();
    try {
      const preview=await pageFor(c,{user:null,savedProfile:{...profile,name:'Аноним',address:'Анонимный адрес'}});
      await preview.addInitScript(()=>localStorage.setItem('vorozhbitov_profile',JSON.stringify({name:'Старый профиль',phone:'+79997777777'})));
      await enter(preview);await preview.close();
      const a=await pageFor(c,{user:420,seed:false});await enter(a);await a.locator('#profileButton').click();
      assert.equal(await a.locator('#profilePhone').inputValue(),'');assert.equal(await a.locator('#profileAddress').inputValue(),'');
      await a.locator('#profileName').fill('Покупатель А');await a.locator('#profilePhone').fill('+79990000011');await a.locator('#profileAddress').fill('Адрес А');await a.locator('#saveProfile').click();await a.close();
      const b=await pageFor(c,{user:421,seed:false});await enter(b);await b.locator('#profileButton').click();
      assert.equal(await b.locator('#profilePhone').inputValue(),'');assert.equal(await b.locator('#profileAddress').inputValue(),'');
      assert.equal(await b.evaluate(()=>VorozhbitovShop.state.cart.length),0);
      await b.locator('#profilePhone').fill('+79990000022');await b.locator('#saveProfile').click();await b.close();
      const again=await pageFor(c,{user:420,seed:false});await enter(again);await again.locator('#profileButton').click();
      assert.equal(await again.locator('#profilePhone').inputValue(),'+79990000011');
      again.once('dialog',dialog=>dialog.accept());await again.locator('#clearLocalData').click();
      await again.waitForFunction(()=>window.VorozhbitovShop && VorozhbitovShop.state.profile.phone==='');
      assert.equal(await again.evaluate(()=>JSON.parse(localStorage.getItem('vorozhbitov_v2:user:421:vorozhbitov_profile')).phone),'+79990000022');
      assert.equal(await again.evaluate(()=>localStorage.getItem('vorozhbitov_profile')),null);
    } finally {await closeContext(c);}
  });
};

module.exports.signedData = signedData;
