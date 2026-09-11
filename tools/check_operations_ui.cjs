/* Mini App reads the very same delivery/support records as the private bot. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {signedData} = require('./check_audit_ui.cjs');
module.exports = async function ({browser, check, errors, out, enter}) {
  const root = process.env.SHOP_UI_URL || 'http://127.0.0.1:4173';
  const uid = 730420;
  const initData = signedData(uid);
  const headers = {'X-Telegram-Init-Data':initData, 'Content-Type':'application/json'};
  const context = await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
  await context.route('https://telegram.org/**', r => r.fulfill({status:200,body:''}));
  const page = await context.newPage();page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({uid,initData}) => {window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:uid,first_name:'Operations fixture'}},ready(){},expand(){}}};}, {uid,initData});
  const get = async query => {const r=await context.request.get(root+'/api/service'+query,{headers});assert.equal(r.status(),200);return r.json();};
  let pid,tid;
  try {
    await check('service API: signed owned views, no staff shortcut or other customer data',async()=>{
      const purchases=await (await context.request.get(root+'/api/purchases',{headers})).json();pid=purchases.purchases[0].purchase_id;
      tid=(await get('')).tickets[0].ticket_id;
      const noAuth=await context.request.get(root+'/api/service?purchase_id='+pid);assert.equal(noAuth.status(),401);
      const foreign=await context.request.get(root+'/api/service?ticket_id='+tid+'&staff=1',{headers:{'X-Telegram-Init-Data':signedData(730421)}});assert.equal(foreign.status(),400);
      const denied=await context.request.post(root+'/api/service',{headers,data:{action:'quote',purchase_id:pid,operation_id:'forbidden-ui'}});assert.equal(denied.status(),400);
      assert.ok(!JSON.stringify(await get('?ticket_id='+tid)).includes('INTERNAL-UI-SECRET'));
    });
    await check('Mini App delivery companion shows real quote, cents and hold, not another invoice',async()=>{
      await enter(page);await page.locator('#profileButton').click();
      await page.locator(`[data-service-purchase="${pid}"]`).click();
      await page.locator('#serviceContent .service-card').first().waitFor();
      const text=await page.locator('#serviceContent').innerText();
      assert.ok(text.includes('350,50 ₽'));assert.ok(text.includes('Напрямую перевозчику'));assert.ok(text.includes('Это не новый счёт'));
      assert.ok(text.includes('Передача приостановлена'));assert.ok(text.includes('Тестовый ПВЗ'));
      assert.equal(await page.locator('#serviceContent [data-pay-id]').count(),0);
      await page.screenshot({path:path.join(out,'service-delivery-mobile.png'),fullPage:false});
    });
    await check('Mini App thread is XSS-safe, hides internal notes and refreshes shared replies',async()=>{
      await page.locator('#serviceContent').getByRole('button',{name:'Открыть обращение №'+tid,exact:true}).click();
      await page.locator('#serviceContent .service-message').first().waitFor();
      let text=await page.locator('#serviceContent').innerText();
      assert.ok(text.includes('PUBLIC-UI-REPLY'));assert.ok(text.includes('<img src=x'));assert.ok(!text.includes('INTERNAL-UI-SECRET'));
      assert.equal(await page.locator('#serviceContent img').count(),0);
      assert.equal(await page.evaluate(()=>window.__serviceXSS),undefined);
      const ticket=(await get('?ticket_id='+tid)).ticket;
      const r=await context.request.post(root+'/api/service',{headers,data:{action:'ticket_reply',ticket_id:tid,version:ticket.version,text:'SHARED-UI-UPDATE: новое сообщение из того же аккаунта.',operation_id:'browser-shared-reply'}});
      assert.equal(r.status(),200);
      await page.locator('#serviceRefresh').click();
      await page.waitForFunction(()=>document.querySelector('#serviceContent').textContent.includes('SHARED-UI-UPDATE'));
      await page.screenshot({path:path.join(out,'service-thread-mobile.png'),fullPage:false});
      for (const width of [320,390,768]) {
        await page.setViewportSize({width,height:844});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      }
      assert.ok(!await page.evaluate(()=>JSON.stringify({...localStorage}).includes('PUBLIC-UI-REPLY')));
    });
    await check('closing the companion drops its text and cannot be undone by a stale response',async()=>{
      let unblock,started;
      const held=new Promise(r=>unblock=r), ready=new Promise(r=>started=r);
      await page.route('**/api/service?ticket_id=*', async route=>{const response=await route.fetch();started();await held;await route.fulfill({response});});
      await page.locator('#serviceRefresh').click();await ready;
      await page.locator('[data-close="serviceModal"]').click();
      const response=page.waitForResponse(r=>r.url().includes('/api/service?ticket_id='));unblock();await response;
      await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
      assert.equal(await page.locator('#serviceContent').textContent(),'');
      assert.equal(await page.locator('#serviceModal').isVisible(),false);
      assert.ok(!await page.evaluate(()=>JSON.stringify({...localStorage}).includes('SHARED-UI-UPDATE')));
    });
  } finally {await context.close();}
};
