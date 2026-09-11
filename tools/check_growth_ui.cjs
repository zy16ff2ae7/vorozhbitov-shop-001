/* Native-only coupon selection must be visible, never dropped by a companion. */
const assert=require('node:assert/strict');
const path=require('node:path');
const {signedData}=require('./check_audit_ui.cjs');
module.exports=async function({browser,check,errors,out,enter}){
  const root=process.env.SHOP_UI_URL||'http://127.0.0.1:4173';
  const headers=uid=>({'X-Telegram-Init-Data':signedData(uid),'Content-Type':'application/json'});
  async function contextFor(uid){
    const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
    await context.route('https://telegram.org/**',r=>r.fulfill({status:200,body:''}));
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(({uid,initData})=>{window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:uid}},ready(){},expand(){},close(){window.__closedToBot=true;}}};},{uid,initData:signedData(uid)});
    return {context,page};
  }
  const uid=730520;const {context,page}=await contextFor(uid);
  const get=async(resource,user=uid)=>{
    const r=await context.request.get(root+'/api/'+resource,{headers:headers(user)});assert.equal(r.status(),200);return r.json();
  };
  try{
    await check('Mini App shows the native promo and blocks a new undiscounted checkout on mobile',async()=>{
      await enter(page);await page.waitForFunction(()=>VorozhbitovShop.state.cart.length===1);
      await page.locator('#cartButton').click();await page.locator('#cartPromotion').waitFor({state:'visible'});
      assert.ok((await page.locator('#cartPromotionText').textContent()).includes('UI10'));
      assert.equal(await page.locator('#submitOrder').isDisabled(),true);
      assert.ok(!await page.evaluate(()=>JSON.stringify({...localStorage}).includes('UI10')),'code must be live server state, not a local discount');
      for(const width of [320,390,768]){
        await page.setViewportSize({width,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
        assert.ok((await page.locator('#cartPromotionChat').boundingBox()).height>=44);
      }
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(out,'growth-mini-promo.png'),fullPage:false});
      await page.locator('#cartPromotionChat').click();assert.equal(await page.evaluate(()=>window.__closedToBot),true);
    });
    await check('native promo companion: signed legacy request is rejected, foreign users see no code',async()=>{
      const r=await context.request.post(root+'/api/checkout',{headers:headers(uid),data:{type:'order',request_id:'ui-legacy-promo-attempt',consent:true,
        customer:{phone:'+79990000000',city:'Тестовый город'},items:[{product_id:'tee-sila-i-chest',size:'M',quantity:1}]}});
      assert.equal(r.status(),400);assert.equal((await r.json()).code,'promotion_requires_chat');
      assert.equal((await get('purchases')).purchases.length,0);
      assert.equal((await get('cart')).cart.promotion.code,'UI10');
      for(const other of [730522,7309001])assert.equal((await get('cart?user_id='+uid,other)).cart.promotion,null);
    });
    await check('explicit cart clear removes promo and a fresh unpromoted cart can be checked out',async()=>{
      let cart=(await get('cart')).cart;
      let r=await context.request.post(root+'/api/cart',{headers:headers(uid),data:{items:[],revision:cart.revision,operation_id:'ui-explicit-clear-code-cart'}});
      assert.equal(r.status(),200);cart=(await r.json()).cart;assert.equal(cart.promotion,null);
      r=await context.request.post(root+'/api/cart',{headers:headers(uid),data:{items:[{product_id:'tee-sila-i-chest',size:'M',quantity:1}],revision:cart.revision,operation_id:'ui-new-unpromoted-cart'}});
      assert.equal(r.status(),200);
      await page.locator('#cartFromBot').click();await page.locator('#cartPromotion').waitFor({state:'hidden'});
      assert.equal(await page.locator('#submitOrder').isDisabled(),false);assert.equal((await get('purchases')).purchases.length,0);
    });
  }finally{await context.unrouteAll({behavior:'wait'});await context.close();}

  const lostUid=730521;const lost=await contextFor(lostUid);
  try{
    await check('new native promo does not block recovery of a committed older full-price checkout',async()=>{
      const payload={type:'order',customer:{name:'Старый тест',phone:'+79990000000',city:'Тестовый город',address:'Вымышленный ПВЗ',entrance:'',deliver:'СДЭК',note:''},consent:true,items:[{product_id:'tee-sila-i-chest',size:'M',quantity:1}]};
      await lost.page.addInitScript(({uid,payload})=>{
        const prefix=`vorozhbitov_v2:user:${uid}:`;
        localStorage.setItem(prefix+'vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:1}]));
        localStorage.setItem(prefix+'vorozhbitov_profile',JSON.stringify(payload.customer));
        localStorage.setItem(prefix+`vorozhbitov_pending_checkout:${uid}`,JSON.stringify({fingerprint:JSON.stringify(payload),request_id:'growth-ui-original'}));
      },{uid:lostUid,payload});
      const before=await (await lost.context.request.get(root+'/api/purchases',{headers:headers(lostUid)})).json();
      assert.equal(before.purchases.length,1);const old=before.purchases[0];
      await enter(lost.page);await lost.page.locator('#cartButton').click();
      await lost.page.locator('#cartPromotion').waitFor({state:'visible'});
      await lost.page.locator('#checkoutConsent').check();assert.equal(await lost.page.locator('#submitOrder').isDisabled(),false);
      const response=lost.page.waitForResponse(r=>r.url().endsWith('/api/checkout'));
      await lost.page.locator('#submitOrder').click();const result=await (await response).json();
      assert.equal(result.payment_id,old.payment_id);assert.equal(result.amount_rub,4900);
      const after=await (await lost.context.request.get(root+'/api/purchases',{headers:headers(lostUid)})).json();
      assert.equal(after.purchases.length,1);
      const current=await (await lost.context.request.get(root+'/api/cart',{headers:headers(lostUid)})).json();
      assert.equal(current.cart.promotion.code,'UI10');assert.equal(current.cart.items.length,1);
    });
  }finally{await lost.context.unrouteAll({behavior:'wait'});await lost.context.close();}
};
