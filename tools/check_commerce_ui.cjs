/* Shared cart/profile checks against the ephemeral authenticated HTTP fixture. */
const assert=require('node:assert/strict');
const {signedData}=require('./check_audit_ui.cjs');
module.exports=async function({browser,check,errors,enter}){
  const root=process.env.SHOP_UI_URL||'http://127.0.0.1:4173';
  const uid=100000+Math.floor(Date.now()/1000)%1000000;
  const auth={'X-Telegram-Init-Data':signedData(uid),'Content-Type':'application/json'};
  const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
  await context.route('https://telegram.org/**',r=>r.fulfill({status:200,body:''}));
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(({uid,initData})=>{window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:uid,first_name:'Telegram name'}},ready(){},expand(){}}};},{uid,initData:auth['X-Telegram-Init-Data']});
  const get=async path=>(await context.request.get(root+path,{headers:auth})).json();
  const post=async(path,data)=>{const response=await context.request.post(root+path,{headers:auth,data});assert.equal(response.status(),200,await response.text());return response.json()};
  const line={product_id:'tee-sila-i-chest',size:'M',quantity:1};
  try{
    await check('shared account: server cart and service-consented profile hydrate a clean device',async()=>{
      const current=await get('/api/cart');
      await post('/api/cart',{revision:current.cart.revision,operation_id:'seed-'+uid,items:[line]});
      await post('/api/account',{consent:true,profile:{name:'Получатель из бота',phone:'+79990000000',city:'Москва',address:'Тестовый ПВЗ',deliver:'Согласовать с менеджером'}});
      await enter(page);await page.locator('#cartButton').click();
      await page.waitForFunction(()=>VorozhbitovShop.state.cart.length===1&&VorozhbitovShop.state.profile.phone==='+79990000000');
      assert.equal(await page.locator('#checkoutName').inputValue(),'Получатель из бота');
      assert.equal(await page.locator('#checkoutCity').inputValue(),'Москва');
      assert.equal(await page.locator('#checkoutConsent').isChecked(),false);
      assert.equal(await page.locator('#deliverRow .active').textContent(),'Согласовать с менеджером');
    });
    await check('shared cart: quantity edit persists, concurrent bot edit is not overwritten',async()=>{
      let response=page.waitForResponse(r=>r.url().endsWith('/api/cart')&&r.request().method()==='POST');
      await page.locator('#cartContent [data-qty="plus"]').click();await response;
      let current=await get('/api/cart');assert.equal(current.cart.items[0].quantity,2);
      await post('/api/cart',{revision:current.cart.revision,operation_id:'native-'+uid,items:[{...line,quantity:5}]});
      response=page.waitForResponse(r=>r.url().endsWith('/api/cart')&&r.status()===409);
      await page.locator('#cartContent [data-qty="plus"]').click();await response;
      await page.locator('#cartSync[data-state="conflict"]').waitFor();
      assert.equal((await get('/api/cart')).cart.items[0].quantity,5);
      assert.equal(await page.evaluate(()=>VorozhbitovShop.state.cart[0].qty),3);
      await page.locator('#cartFromBot').click();
      await page.waitForFunction(()=>VorozhbitovShop.state.cart[0].qty===5);
      assert.equal((await get('/api/cart')).cart.items[0].quantity,5);
    });
    await check('account cache clear does not immediately restore consented server PII',async()=>{
      await page.locator('[data-close="cartModal"]').click();await page.locator('#profileButton').click();
      page.once('dialog',dialog=>dialog.accept());await page.locator('#clearLocalData').click();
      await page.waitForFunction(()=>location.hash==='#local-only'&&window.VorozhbitovShop&&VorozhbitovShop.state.profile.phone==='');
      await page.locator('#enterShop').click();await page.locator('#welcome').waitFor({state:'hidden'});
      await page.locator('#profileButton').click();
      assert.equal(await page.locator('#profilePhone').inputValue(),'');
      assert.equal((await get('/api/account')).profile.phone,'+79990000000');
      await page.locator('#profileFromBot').click();
      await page.waitForFunction(()=>document.querySelector('#profilePhone').value==='+79990000000');
    });
    await check('older checkout response preserves a newer unsynced cart from another window',async()=>{
      const current=await get('/api/cart');
      await post('/api/cart',{revision:current.cart.revision,operation_id:'before-submit-'+uid,items:[line]});
      await page.locator('[data-close="profileModal"]').click();await page.locator('#cartButton').click();
      await page.locator('#cartFromBot').click();
      await page.waitForFunction(()=>VorozhbitovShop.state.cart[0]?.qty===1&&document.querySelector('#cartSync').dataset.state==='ready');
      const other=await context.newPage();await other.goto(root+'/health');
      await page.route('**/api/checkout',async route=>{
        const accepted=await route.fetch();assert.equal(accepted.status(),200);
        await other.evaluate(uid=>{
          const prefix=`vorozhbitov_v2:user:${uid}:`;
          localStorage.setItem(prefix+'vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:2}]));
          localStorage.setItem(prefix+'vorozhbitov_cart_intent',JSON.stringify('another-window-intent'));
        },uid);
        await route.fulfill({response:accepted});
      });
      await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();
      await page.locator('#payModal').waitFor({state:'visible'});
      await page.waitForFunction(()=>VorozhbitovShop.state.cart[0]?.qty===2&&document.querySelector('#cartSync').dataset.state==='conflict');
      assert.equal(await other.evaluate(uid=>JSON.parse(localStorage.getItem(`vorozhbitov_v2:user:${uid}:vorozhbitov_cart`))[0].qty,uid),2);
      assert.deepEqual((await get('/api/cart')).cart.items,[]);
      assert.equal((await get('/api/my-orders')).purchases.length,1);
      await other.close();
    });
  }finally{await context.close()}
};
