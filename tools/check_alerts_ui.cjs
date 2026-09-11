/* A Mini App interest request is not implicit consent to automated messages. */
const assert=require('node:assert/strict');
const path=require('node:path');
const {signedData}=require('./check_audit_ui.cjs');
module.exports=async function({browser,check,errors,out,enter}){
  const uid=740420,initData=signedData(uid);
  const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
  await context.route('https://telegram.org/**',r=>r.fulfill({status:200,body:''}));
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(({uid,initData})=>{
    window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:uid}},ready(){},expand(){},close(){window.__alertSetupClosed=true;}}};
    localStorage.setItem(`vorozhbitov_v2:user:${uid}:vorozhbitov_waitlist`,'{"malformed":true}');
  },{uid,initData});
  try{
    await check('Mini App waitlist requires separate native alert consent and makes no message promise',async()=>{
      await enter(page);await page.locator('[data-product-id="tee-sila-i-chest"] .product-open').click();await page.locator('[data-size="M"]').click();
      assert.ok((await page.locator('.waitlist-consent-note').textContent()).includes('не включает сообщения'));
      let done=page.waitForResponse(r=>r.url().endsWith('/api/waitlist'));
      await page.locator('#waitlistButton').click();let response=await done;
      assert.equal(response.status(),200);let body=await response.json();assert.equal(body.added,true);assert.equal(body.alert_consent_required,true);
      await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('уведомления не включены'));
      done=page.waitForResponse(r=>r.url().endsWith('/api/waitlist'));
      await page.locator('#waitlistButton').click();body=await (await done).json();assert.equal(body.added,false);assert.equal(body.alert_consent_required,true);
      await page.locator('#waitlistSetup').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(out,'alerts-mini-consent.png'),fullPage:false});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.ok((await page.locator('#waitlistSetup').boundingBox()).height>=44);
      await page.locator('#waitlistSetup').click();assert.equal(await page.evaluate(()=>window.__alertSetupClosed),true);
    });
  }finally{await context.unrouteAll({behavior:'wait'});await context.close();}
};
