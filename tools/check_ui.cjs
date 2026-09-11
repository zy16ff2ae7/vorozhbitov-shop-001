/* Browser regressions. Requires Playwright and Chromium. No real payments. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const out=path.resolve(process.env.SHOP_UI_OUTPUT||'ui-checks');
fs.mkdirSync(out,{recursive:true});
const results=[],errors=[];let browser;
async function check(name,run){await run();results.push({name,passed:true});console.log('PASS',name)}
async function make(width=390){
  const context=await browser.newContext({viewport:{width,height:844},reducedMotion:'reduce'});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://telegram.org/**',r=>r.fulfill({status:200,body:''}));
  return{page,context};
}
async function enter(page,wait=true){await page.goto(process.env.SHOP_UI_URL||'http://127.0.0.1:4173');await page.locator('#enterShop').click();await page.locator('#welcome').waitFor({state:'hidden'});if(wait)await page.locator('#productGrid[aria-busy="false"]').waitFor({state:'attached'})}
async function shot(page,name,options={}){await page.screenshot({path:path.join(out,name+'.png'),...options})}
(async()=>{
 browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_EXECUTABLE_PATH?{executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH}:{})});
 const {page,context}=await make();await enter(page);
 await check('2 products and mobile primary action',async()=>{
  assert.equal(await page.locator('.product-card').count(),2);
  assert.equal(await page.locator('#productCount').textContent(),'02 ВЕЩИ');
  const box=await page.locator('#heroProductButton').boundingBox();assert.ok(box.y+box.height<780);
  assert.equal(await page.locator('#welcomeVideo').getAttribute('src'),null);await shot(page,'mobile-home');
 });
 await check('available limited stock and XXL filter',async()=>{
  await page.locator('[data-scroll="catalog"]').first().click();await page.locator('#filterToggle').click();await page.locator('[data-stock="available"]').click();
  assert.equal(await page.locator('[data-product-id="tee-sila-i-chest"]').count(),1);
  await page.locator('[data-size-filter="XXL"]').click();assert.equal(await page.locator('.product-card').count(),1);
  await page.locator('#resetFilters').click();assert.equal(await page.locator('.product-card').count(),2);await page.locator('#filterToggle').click();
  await page.locator('#catalog').screenshot({path:path.join(out,'mobile-catalog.png')});
 });
 await check('360 controls, nested photo zoom and keyboard focus',async()=>{
  await page.locator('[data-product-id="tee-sila-i-chest"] .product-open').click();await page.waitForFunction(()=>document.querySelector('.spin-frame').complete);
  assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.frames.length),8);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.raf),0);await shot(page,'mobile-product');
  await page.locator('#view3d').focus();await page.keyboard.press('ArrowRight');assert.equal(await page.locator('#mediaPosition').textContent(),'02 / 08');
  await page.locator('#mediaNext').click();assert.equal(await page.locator('#mediaPosition').textContent(),'03 / 08');await page.locator('#spinToggle').click();
  await page.locator('#zoomProduct').click();assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.alive),false);assert.equal(await page.locator('#productModal').getAttribute('aria-hidden'),'true');
  await page.locator('#lightboxZoom').click();await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'lightboxViewport');
  await page.keyboard.press('ArrowRight');await page.waitForFunction(()=>document.querySelector('#lightboxViewport').scrollLeft>0);
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'zoomProduct');await page.locator('#spinToggle').click();await page.locator('#stagePhoto').click();
  assert.equal(await page.locator('#galleryDots button').count(),7);await page.locator('[data-gallery="1"]').click();assert.match(await page.locator('#sheetImage').getAttribute('src'),/tee-sich-05/);
  await page.locator('[data-gallery="6"]').click();await shot(page,'mobile-detail');await page.locator('#addToCartButton').click();await page.locator('[data-size="XXL"]').click();
  assert.equal(await page.locator('[data-size="XXL"]').getAttribute('aria-pressed'),'true');await page.locator('#sizeGuideButton').click();await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'sizeGuideButton');
 });
 await check('cart and preview checkout retain contents',async()=>{
  await page.locator('#addToCartButton').click();await page.locator('#cartModal:not(.hidden)').waitFor();await page.locator('#cartContent [data-qty="plus"]').click();assert.equal((await page.locator('#cartTotal').textContent()).replace(/\s/g,' '),'9 800 ₽');
  await page.locator('#checkoutName').fill('Тест');await page.locator('#checkoutPhone').fill('+79990000000');await page.locator('#checkoutCity').fill('Москва');await page.locator('#checkoutAddress').fill('Тестовый адрес');await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();
  await page.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.cart[0].qty),2);assert.match(await page.locator('#toast').textContent(),/бот|Telegram/);await shot(page,'mobile-cart');await page.locator('[data-close="cartModal"]').click();
 });
 await check('saved, search and profile',async()=>{
  await page.locator('[data-save-id="tee-sila-i-chest"]').click();await page.locator('#savedButton').click();assert.equal(await page.locator('.product-card').count(),1);await page.locator('#clearView').click();
  await page.locator('#searchToggle').click();await page.locator('#searchInput').fill('жетон');assert.equal(await page.locator('.product-card').count(),1);await page.keyboard.press('Escape');await page.locator('#profileButton').click();assert.equal(await page.locator('#profileAddress').inputValue(),'Тестовый адрес');await page.keyboard.press('Escape');
 });
 await context.close();
 for(const width of [320,360,768,1440]){
  const{page,context}=await make(width);await enter(page);await check('layout '+width+'px',async()=>{
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);await shot(page,'home-'+width);if(width===1440)await shot(page,'desktop-full',{fullPage:true});
   await page.locator('#heroProductButton').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);if(width===1440)await shot(page,'desktop-product');
  });await context.close();
 }
 await check('catalog outage retry has no fabricated products',async()=>{
  const{page,context}=await make();let calls=0;await page.route('**/api/catalog',r=>++calls===1?r.fulfill({status:503,body:'{}'}):r.continue());await enter(page);
  assert.equal(await page.locator('.product-card').count(),0);assert.equal(await page.locator('#catalogError').isVisible(),true);await page.locator('#retryCatalog').click();await page.waitForFunction(()=>VorozhbitovShop.state.catalogReady);assert.equal(await page.locator('.product-card').count(),2);await context.close();
 });
 await check('slow catalog refreshes open cart and protects unavailable items',async()=>{
  const{page,context}=await make();let release;const ready=new Promise(r=>release=r);
  await page.addInitScript(()=>localStorage.setItem('vorozhbitov_v2:preview:vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:1},{id:'missing',size:'M',qty:1}])));
  await page.route('**/api/catalog',async r=>{await ready;await r.continue()});await enter(page,false);await page.locator('#cartButton').click();assert.match(await page.locator('#cartContent').textContent(),/Сверяем/);
  release();await page.waitForFunction(()=>VorozhbitovShop.state.catalogReady);assert.equal(await page.locator('.cart-line').count(),2);assert.equal(await page.locator('#submitOrder').isDisabled(),true);
  await page.locator('.is-unavailable [data-remove-key]').click();assert.equal(await page.locator('#submitOrder').isDisabled(),false);assert.equal((await page.locator('#cartTotal').textContent()).replace(/\s/g,' '),'4 900 ₽');await context.close();
 });
 await check('malformed stored data does not crash startup',async()=>{
  const{page,context}=await make();await page.addInitScript(()=>{for(const key of ['cart','saved','viewed','profile','orders'])localStorage.setItem('vorozhbitov_v2:preview:vorozhbitov_'+key,'{"broken":true}')});await enter(page);await page.locator('#profileButton').click();assert.equal(await page.locator('#profileName').inputValue(),'');await context.close();
 });
 await check('pending checkout prevents cart changes and second request',async()=>{
  const{page,context}=await make();let release,calls=0;const ready=new Promise(r=>release=r);
  await page.addInitScript(initData=>{window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:123}},ready(){},expand(){}}};localStorage.setItem('vorozhbitov_v2:user:123:vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:1}]));localStorage.setItem('vorozhbitov_v2:user:123:vorozhbitov_profile',JSON.stringify({name:'Тест',phone:'+79990000000',city:'Москва'}))},require('./check_audit_ui.cjs').signedData(123));
  await page.route('**/api/checkout',async r=>{calls++;await ready;await r.fulfill({status:503,contentType:'application/json',body:'{"error":"Тестовый отказ"}'})});await enter(page);await page.locator('#cartButton').click();await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();await page.waitForFunction(()=>VorozhbitovShop.state.checkoutPending);
  assert.equal(await page.locator('#cartContent [data-qty="plus"]').isDisabled(),true);assert.equal(await page.locator('#submitOrder').isDisabled(),true);await page.locator('[data-close="cartModal"]').click();await page.locator('#cartButton').click();assert.equal(await page.locator('#submitOrder').isDisabled(),true);
  release();await page.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);assert.equal(calls,1);assert.equal(await page.locator('#submitOrder').isDisabled(),false);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.cart[0].qty),1);await context.close();
 });
 await require('./check_audit_ui.cjs')({browser,check,errors,out,enter});
 await require('./check_commerce_ui.cjs')({browser,check,errors,out,enter});
 await require('./check_operations_ui.cjs')({browser,check,errors,out,enter});
 await require('./check_growth_ui.cjs')({browser,check,errors,out,enter});
 await require('./check_alerts_ui.cjs')({browser,check,errors,out,enter});
 assert.deepEqual(errors,[]);
})().catch(e=>{errors.push(e.message);console.error(e);process.exitCode=1}).finally(async()=>{
 try { fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({checks:results,errors},null,2)); }
 finally { if(browser)await browser.close(); }
});
