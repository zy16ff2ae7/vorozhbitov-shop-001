/* Local browser regressions. Requires Playwright plus installed Chrome. */
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
async function enter(page,wait=true){await page.goto('http://127.0.0.1:4173');await page.locator('#enterShop').click();await page.locator('#welcome').waitFor({state:'hidden'});if(wait)await page.locator('#productGrid[aria-busy="false"]').waitFor({state:'attached'})}
async function shot(page,name,options={}){await page.screenshot({path:path.join(out,name+'.png'),...options})}
(async()=>{
 browser=await chromium.launch({headless:true,channel:'chrome'});
 const {page,context}=await make();await enter(page);
 await check('3 products and mobile primary action',async()=>{
  assert.equal(await page.locator('.product-card').count(),3);
  assert.equal(await page.locator('#productCount').textContent(),'03 ВЕЩИ');
  const box=await page.locator('#heroProductButton').boundingBox();assert.ok(box.y+box.height<780);
  assert.equal(await page.locator('#welcomeVideo').getAttribute('src'),null);await shot(page,'mobile-home');
 });
 await check('available limited stock and XXL filter',async()=>{
  await page.locator('[data-scroll="catalog"]').first().click();await page.locator('#filterToggle').click();await page.locator('[data-stock="available"]').click();
  assert.equal(await page.locator('[data-product-id="tee-sila-i-chest"]').count(),1);
  await page.locator('[data-size-filter="XXL"]').click();assert.equal(await page.locator('.product-card').count(),3);
  await page.locator('#resetFilters').click();assert.equal(await page.locator('.product-card').count(),3);await page.locator('#filterToggle').click();
  await page.locator('#catalog').screenshot({path:path.join(out,'mobile-catalog.png')});
 });
 await check('360 controls, nested photo zoom and keyboard focus',async()=>{
  await page.locator('[data-product-id="tee-sila-i-chest"] .product-open').click();await page.waitForFunction(()=>document.querySelector('.spin-frame').complete);
  assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.frames.length),8);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.raf),0);await shot(page,'mobile-product');
  assert.equal(await page.locator('.spin-frame').count(),2);await page.waitForFunction(()=>document.querySelector('.spin-frame-next').complete);
  await page.emulateMedia({reducedMotion:'no-preference'});const blend=await page.evaluate(()=>{const v=VorozhbitovShop.state.viewer;v.index=.97;v.show();return Number(document.querySelector('.spin-frame-next').style.opacity)});
  assert.ok(blend>0&&blend<1);await page.evaluate(()=>{const v=VorozhbitovShop.state.viewer;v.index=0;v.show()});await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.locator('.spin-frame-current').evaluate(img=>getComputedStyle(img).objectFit),'cover');assert.equal(await page.locator('.spin-frame-current').evaluate(img=>img.style.transform),'');
  await page.locator('#view3d').focus();await page.keyboard.press('ArrowRight');assert.equal(await page.locator('#mediaPosition').textContent(),'02 / 08');
  await page.locator('#mediaNext').click();assert.equal(await page.locator('#mediaPosition').textContent(),'03 / 08');await page.locator('#spinToggle').click();
  await page.locator('#zoomProduct').click();assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.alive),false);assert.equal(await page.locator('#productModal').getAttribute('aria-hidden'),'true');
  await page.locator('#lightboxZoom').click();await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'lightboxViewport');
  await page.keyboard.press('ArrowRight');await page.waitForFunction(()=>document.querySelector('#lightboxViewport').scrollLeft>0);
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'zoomProduct');await page.locator('#spinToggle').click();await page.locator('#stagePhoto').click();
  assert.equal(await page.locator('#galleryDots button').count(),5);await page.locator('[data-gallery="1"]').click();assert.match(await page.locator('#sheetImage').getAttribute('src'),/tee-sich-03/);
  await page.locator('[data-gallery="4"]').click();await shot(page,'mobile-detail');
  await page.locator('#stageVideo').click();await page.waitForFunction(()=>document.querySelector('#sheetVideo').readyState>=1);assert.match(await page.locator('#sheetVideo').getAttribute('src'),/tee-sila-i-chest-360-v5\.mp4\?v=/);assert.ok(Math.abs(await page.locator('#sheetVideo').evaluate(video=>video.duration)-8)<0.05);await shot(page,'mobile-black-video');await page.locator('#stagePhoto').click();
  await page.locator('#addToCartButton').click();await page.locator('[data-size="XXL"]').click();
  assert.equal(await page.locator('[data-size="XXL"]').getAttribute('aria-pressed'),'true');
 });
 await check('cart and preview checkout retain contents',async()=>{
  await page.locator('#addToCartButton').click();await page.locator('#cartModal:not(.hidden)').waitFor();await page.locator('#cartContent [data-qty="plus"]').click();assert.equal((await page.locator('#cartTotal').textContent()).replace(/\s/g,' '),'9 800 ₽');
  await page.locator('#checkoutName').fill('Тест');await page.locator('#checkoutPhone').fill('+79990000000');await page.locator('#checkoutCity').fill('Москва');await page.locator('#checkoutAddress').fill('Тестовый адрес');await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();
  await page.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.cart[0].qty),2);assert.match(await page.locator('#toast').textContent(),/бот|Telegram/);await shot(page,'mobile-cart');await page.locator('[data-close="cartModal"]').click();
 });
 await check('search and profile',async()=>{
  await page.locator('#searchToggle').click();await page.locator('#searchInput').fill('один в поле');assert.equal(await page.locator('.product-card').count(),1);assert.equal(await page.locator('[data-product-id="tee-odin-v-pole-voin"]').count(),1);await page.keyboard.press('Escape');await page.locator('#profileButton').click();assert.equal(await page.locator('#profileAddress').inputValue(),'Тестовый адрес');await page.keyboard.press('Escape');
 });
 await context.close();
 await check('white tee gallery, video, 360 and preview checkout',async()=>{
  const{page,context}=await make();await enter(page);
  await page.locator('[data-product-id="tee-odin-v-pole-voin"] .product-open').click();
  assert.equal(await page.locator('#sheetTitle').textContent(),'ОДИН В ПОЛЕ ВОИН');
  assert.equal(await page.evaluate(()=>VorozhbitovShop.state.viewer.frames.length),8);
  await page.locator('#stagePhoto').click();assert.equal(await page.locator('#galleryDots button').count(),5);
  await page.locator('[data-gallery="4"]').click();assert.match(await page.locator('#sheetImage').getAttribute('src'),/odin-voin-detail-v3/);
  await page.locator('#stageVideo').click();await page.waitForFunction(()=>document.querySelector('#sheetVideo').readyState>=1);
  assert.match(await page.locator('#sheetVideo').getAttribute('src'),/tee-odin-v-pole-voin-360-v8\.mp4\?v=/);
  assert.ok(Math.abs(await page.locator('#sheetVideo').evaluate(video=>video.duration)-8)<0.05);await shot(page,'mobile-white-video');
  await page.locator('#stage3d').click();await page.waitForFunction(()=>{const img=document.querySelector('.spin-frame-current');return img.complete&&img.naturalWidth===1080&&img.naturalHeight===590});
  assert.equal(await page.locator('.spin-frame-next').evaluate(img=>getComputedStyle(img).objectFit),'cover');await shot(page,'mobile-white-product');
  for(const [index,label] of [[3,'04'],[4,'05'],[5,'06']]){
   await page.evaluate(index=>{const viewer=VorozhbitovShop.state.viewer;viewer.setRotating(false);viewer.index=index;viewer.show()},index);
   await page.waitForFunction(label=>{const img=document.querySelector('.spin-frame-current');return img.complete&&img.naturalWidth===1080&&img.getAttribute('src').includes(`odin-voin-${label}-v8.jpg`)},label);
   await shot(page,`mobile-white-back-${label}`);
  }
  await page.locator('#addToCartButton').click();await page.locator('[data-size="M"]').click();await page.locator('#addToCartButton').click();
  await page.locator('#cartModal:not(.hidden)').waitFor();assert.match(await page.locator('#cartContent').textContent(),/ОДИН В ПОЛЕ ВОИН/);
  assert.equal((await page.locator('#cartTotal').textContent()).replace(/\s/g,' '),'4 900 ₽');
  await page.locator('#checkoutName').fill('Тест');await page.locator('#checkoutPhone').fill('+79990000000');await page.locator('#checkoutCity').fill('Москва');await page.locator('#checkoutAddress').fill('Тестовый адрес');await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();
  await page.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);assert.match(await page.locator('#toast').textContent(),/бот|Telegram/);await shot(page,'mobile-white-checkout');
  await context.close();
 });
 for(const width of [320,360,768,1440]){
  const{page,context}=await make(width);await enter(page);await check('layout '+width+'px',async()=>{
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);await shot(page,'home-'+width);if(width===1440)await shot(page,'desktop-full',{fullPage:true});
   await page.locator('#heroProductButton').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);if(width===1440)await shot(page,'desktop-product');
  });await context.close();
 }
 await check('catalog outage retry has no fabricated products',async()=>{
  const{page,context}=await make();let calls=0;await page.route('**/api/catalog',r=>++calls===1?r.fulfill({status:503,body:'{}'}):r.continue());await enter(page);
  assert.equal(await page.locator('.product-card').count(),0);assert.equal(await page.locator('#catalogError').isVisible(),true);await page.locator('#retryCatalog').click();await page.waitForFunction(()=>VorozhbitovShop.state.catalogReady);assert.equal(await page.locator('.product-card').count(),3);await context.close();
 });
 await check('slow catalog refreshes open cart and protects unavailable items',async()=>{
  const{page,context}=await make();let release;const ready=new Promise(r=>release=r);
  await page.addInitScript(()=>localStorage.setItem('vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:1},{id:'missing',size:'M',qty:1}])));
  await page.route('**/api/catalog',async r=>{await ready;await r.continue()});await enter(page,false);await page.locator('#cartButton').click();assert.match(await page.locator('#cartContent').textContent(),/Сверяем/);
  release();await page.waitForFunction(()=>VorozhbitovShop.state.catalogReady);assert.equal(await page.locator('.cart-line').count(),2);assert.equal(await page.locator('#submitOrder').isDisabled(),true);
  await page.locator('.is-unavailable [data-remove-key]').click();assert.equal(await page.locator('#submitOrder').isDisabled(),false);assert.equal((await page.locator('#cartTotal').textContent()).replace(/\s/g,' '),'4 900 ₽');await context.close();
 });
 await check('malformed stored data does not crash startup',async()=>{
  const{page,context}=await make();await page.addInitScript(()=>{for(const key of ['cart','saved','viewed','profile','orders'])localStorage.setItem('vorozhbitov_'+key,'{"broken":true}')});await enter(page);await page.locator('#profileButton').click();assert.equal(await page.locator('#profileName').inputValue(),'');await context.close();
 });
 await check('pending checkout prevents cart changes and second request',async()=>{
  const{page,context}=await make();let release,calls=0;const ready=new Promise(r=>release=r);
  await page.addInitScript(()=>{window.Telegram={WebApp:{initData:'local-test',initDataUnsafe:{user:{id:123}},ready(){},expand(){}}};localStorage.setItem('vorozhbitov_cart',JSON.stringify([{id:'tee-sila-i-chest',size:'M',qty:1}]));localStorage.setItem('vorozhbitov_profile',JSON.stringify({name:'Тест',phone:'+79990000000',city:'Москва'}))});
  await page.route('**/api/checkout',async r=>{calls++;await ready;await r.fulfill({status:503,contentType:'application/json',body:'{"error":"Тестовый отказ"}'})});await enter(page);await page.locator('#cartButton').click();await page.locator('#checkoutConsent').check();await page.locator('#submitOrder').click();await page.waitForFunction(()=>VorozhbitovShop.state.checkoutPending);
  assert.equal(await page.locator('#cartContent [data-qty="plus"]').isDisabled(),true);assert.equal(await page.locator('#submitOrder').isDisabled(),true);await page.locator('[data-close="cartModal"]').click();await page.locator('#cartButton').click();assert.equal(await page.locator('#submitOrder').isDisabled(),true);
  release();await page.waitForFunction(()=>!VorozhbitovShop.state.checkoutPending);assert.equal(calls,1);assert.equal(await page.locator('#submitOrder').isDisabled(),false);assert.equal(await page.evaluate(()=>VorozhbitovShop.state.cart[0].qty),1);await context.close();
 });
 assert.deepEqual(errors,[]);
})().catch(e=>{errors.push(e.message);console.error(e);process.exitCode=1}).finally(async()=>{
 try { fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({checks:results,errors},null,2)); }
 finally { if(browser)await browser.close(); }
});
