/* Native controller end-to-end in the isolated Telegram simulator, no providers. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const root = process.env.SHOP_BOT_DEMO_URL || 'http://127.0.0.1:4174';
const out = path.resolve(process.env.SHOP_UI_OUTPUT || 'ui-checks');
fs.mkdirSync(out,{recursive:true});
let browser; const checks=[], errors=[];
async function check(name, action) { await action(); checks.push({name,passed:true});console.log('PASS',name); }
(async()=>{
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_EXECUTABLE_PATH?{executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(root);await page.locator('.bubble').first().waitFor();
  const state = async uid => (await page.request.get(`${root}/demo/api/state?actor=${uid}`)).json();
  const click=async(name,exact=false)=>{
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('#thread').getByRole('button',{name,exact}).last().click();
    assert.equal((await done).status(),200);
  };
  const command=async text=>{
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('#messageInput').fill(text);await page.locator('#sendMessage').click();await done;
  };
  const role=async uid=>{
    const done=page.waitForResponse(r=>r.url().endsWith(`/demo/api/state?actor=${uid}`));
    await page.locator(`[data-actor="${uid}"]`).click();await done;
  };
  async function chooseTee(size){await command('/menu');await click('Смотреть выпуск');await click(/^СИЛА И ЧЕСТЬ ·/);await click('Выбрать размер');await click(size,true);}
  async function enterCustomer(){
    await click('Оформить покупку');await click('Согласен');
    for(let i=0;i<4;i++){
      const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
      await page.locator('#exampleInput').click();await done;
    }
    await click('СДЭК',true);
  }
  await check('native chat: multi-item cart, personalization, consent and one purchase',async()=>{
    assert.equal((await state(420)).purchases,0,'Use a fresh bot_demo process for this test');
    await page.screenshot({path:path.join(out,'bot-demo-home.png'),fullPage:true});
    await chooseTee('M');await click('+ Добавить вещь');
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('[data-callback="c:product:tag-sila-i-chest"]').last().click();await done;
    await click('Выбрать размер');await click('ONE SIZE',true);
    await click('Персонализация 2');await command('00123');
    await enterCustomer();await click('Всё верно');
    const s=await state(420);assert.equal(s.purchases,1);assert.equal(s.reserved,2);assert.equal(s.stock.available,0);assert.equal(s.paid,0);
    assert.ok(s.messages.some(m=>m.text.includes('персонализация 00123')));
    await page.screenshot({path:path.join(out,'bot-demo-purchase.png'),fullPage:true});
  });
  await check('native chat: second buyer cannot purchase the last reserved size',async()=>{
    await role(421);await chooseTee('M');await enterCustomer();await click('Всё верно');
    const s=await state(421);assert.equal(s.purchases,1);assert.equal(s.stock.reserved,1);
    assert.ok(s.messages.some(m=>m.text.includes('доступно 0 шт.')));
  });
  await check('native chat: verified payment and assembly cannot skip delivery',async()=>{
    await role(420);await command('/purchases');await click(/^№0001/);await click('Выбрать оплату');await click('Звёзды Telegram',true);
    let done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('.invoice-button').last().click();await done;
    assert.equal((await state(420)).paid,1);assert.equal((await state(420)).reserved,0);
    await role(9003);await command('/team');await click('К сборке',true);await click(/^№0001/);await click('Взять в работу');
    for(const stage of ['собираем','готов к выдаче']){await click('Следующий этап · '+stage);await click('Да, этап выполнен');}
    assert.equal((await state(9003)).delivered,0);
    assert.ok(!(await state(9003)).messages.at(-1).markup.inline_keyboard.flat().some(b=>b.text.includes('Следующий этап · завершён')));
    await page.screenshot({path:path.join(out,'bot-demo-manager.png'),fullPage:true});
  });
  await check('native chat: exact versioned delivery quote and buyer acceptance',async()=>{
    await click('Доставка / трек / вручение');await click('Предложить новые условия');await click('СДЭК',true);await click('Покупатель платит перевозчику');
    await command('Демо-ПВЗ, Тестовая улица, 1');await command('350,50');await command('2–4 дня после передачи');await command('Вымышленный тариф только для демо');
    await click('Проверено · отправить условия');
    assert.ok((await state(9003)).messages.at(-1).text.includes('350,50 ₽'));
    await role(420);await command('/purchases');await click(/^№0001/);await click('Доставка / условия / трек');
    await click('Условия подходят · принять');
    assert.ok((await state(420)).messages.some(m=>m.text.includes('подтверждено покупателем')));
    await page.screenshot({path:path.join(out,'bot-demo-delivery-quote.png'),fullPage:true});
  });
  await check('native chat: support queue, private note, public reply and customer withdrawal',async()=>{
    await click('Вопрос / изменить адрес');await click('Доставка / адрес',true);await command('Проверьте, пожалуйста, согласованный ПВЗ.');await click('Отправить сообщение',true);
    assert.equal((await state(420)).tickets,1);
    assert.ok((await state(420)).messages.at(-1).text.includes('Передача приостановлена'));
    await role(9004);await command('/desk');await click(/^№0001/);await click('Взять обращение в работу');
    await click('Внутренняя заметка');await command('ВНУТРЕННЯЯ ДЕМО ЗАМЕТКА: сверить накладную.');await click('Сохранить заметку',true);
    assert.ok(!(await state(9004)).messages.at(-1).markup.inline_keyboard.flat().some(b=>b.text.includes('Ответить и закрыть')));
    await click('Ответить покупателю');await command('Проверили: в предложении указан согласованный демо-ПВЗ. Если всё верно, можно отозвать запрос.');await click('Отправить сообщение',true);
    await page.screenshot({path:path.join(out,'bot-demo-support.png'),fullPage:true});
    await role(420);await command('/tickets');await click(/^№0001/);
    assert.ok(!(await state(420)).messages.some(m=>m.text.includes('ВНУТРЕННЯЯ ДЕМО ЗАМЕТКА')));
    await click('Вопрос решён / отозвать');await command('Подтверждаю этот ПВЗ. Изменение не нужно.');await click('Подтвердить закрытие',true);
    assert.equal((await state(420)).tickets,0);assert.equal((await state(420)).paid,1);
  });
  await check('native chat: manual dispatch, tracking correction and actual receipt',async()=>{
    await role(9003);await command('/shipping');await click('К передаче',true);await click(/^№0001/);await click('Передано перевозчику');
    await command('DEMO123456');await click('Факт проверен · зафиксировать');assert.equal((await state(9003)).shipments,1);
    await click('Исправить трек с причиной');await command('DEMO123457');await command('В тестовом номере была опечатка.');await click('Исправить и уведомить',true);
    await role(420);await command('/purchases');await click(/^№0001/);await click('Доставка / условия / трек');
    assert.ok((await state(420)).messages.at(-1).text.includes('DEMO123457'));
    await click('Вещи у меня · подтвердить');await click('Да, вручение состоялось',true);
    assert.equal((await state(420)).delivered,1);assert.equal((await state(420)).shipments,0);
    await page.screenshot({path:path.join(out,'bot-demo-delivered.png'),fullPage:true});
    await click('← Покупка',true);assert.ok((await state(420)).messages.at(-1).text.includes('Завершена'));
  });
  await check('native chat: reopening after receipt never undelivers or refunds the purchase',async()=>{
    await command('/tickets');await click(/^№0001/);await click('Возобновить обращение');await command('Дополнительный вопрос после получения.');await click('Отправить сообщение',true);
    const s=await state(420);assert.equal(s.tickets,1);assert.equal(s.delivered,1);assert.equal(s.paid,1);
    assert.ok(!s.messages.at(-1).text.includes('Передача приостановлена'));
  });
  await check('native chat: expiration and delayed payment require refund, never fulfillment',async()=>{
    await role(421);await command('/cart');await click('Размер 1');await click('S',true);await click('Оформить покупку');await click('Всё верно');
    await click('Выбрать оплату');await click('Звёзды Telegram',true);
    let done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('#expireReservations').click();await done;
    done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('.late-button').last().click();await done;
    const s=await state(421);assert.equal(s.paid,1);assert.equal(s.reserved,0);assert.equal(s.receipts[0].status,'refund_required');
    await role(9003);await command('/team');await click('Сверка / возврат');await click(/^№0002/);
    await page.screenshot({path:path.join(out,'bot-demo-review.png'),fullPage:true});
  });
  await check('native demo is usable on mobile without horizontal overflow',async()=>{
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});await role(9002);await command('/stock');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      const buttons=await page.locator('#thread .inline-button').all();
      for(const button of buttons){const box=await button.boundingBox();if(box)assert.ok(box.height>=37);}
      if(width===390)await page.screenshot({path:path.join(out,'bot-demo-mobile.png'),fullPage:true});
    }
  });
  await page.setViewportSize({width:1440,height:1000});
  const scenario=async kind=>{
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator(`[data-finance="${kind}"]`).click();
    assert.equal((await done).status(),200);
  };
  const lastPanel=async(uid=9005)=>(await state(uid)).messages.filter(m=>!m.human).at(-1);
  const callback=async(uid,data)=>{
    const r=await page.request.post(`${root}/demo/api/action`,{data:{actor:uid,callback:data}});
    assert.equal(r.status(),200);return r.json();
  };
  let conflictCase, attemptCase;
  const privateFinanceNote='ТОЛЬКО ФИНАНСАМ: <img src=x onerror="globalThis.__financeInjected=1"> & сверка ещё не завершена.';
  await check('native finance: orphan charge, structured-only workflow and explicit control deadline',async()=>{
    await role(9005);await command('/finance');
    assert.ok((await lastPanel()).text.includes('ФИНАНСЫ / ПУЛЬТ'));
    await page.screenshot({path:path.join(out,'bot-demo-finance-home.png'),fullPage:true});
    const before=await state(9005);
    await scenario('orphan');await command('/finance');await click('Все задачи',true);await click(/^F\d+ · открыть разбор$/);
    let p=await lastPanel();assert.ok(p.text.includes('Связанная покупка не найдена'));assert.ok(p.text.includes('мин. ед. UNK'));
    await click('Взять финансовый разбор',true);
    p=await lastPanel();assert.ok(!p.markup.inline_keyboard.flat().some(b=>/заметка|Закрыть только/.test(b.text)));
    await click('Срок контроля');await click('Контроль через 1 ч');await click('Подтвердить срок контроля',true);
    p=await lastPanel();assert.ok(!p.text.includes('Контроль: не назначен'));
    const after=await state(9005);assert.equal(after.purchases,before.purchases);assert.equal(after.paid,before.paid);assert.equal(after.reserved,before.reserved);
    assert.equal(after.finance_cases,before.finance_cases+1);
    await page.screenshot({path:path.join(out,'bot-demo-finance-orphan.png'),fullPage:true});
  });
  await check('native finance: conflicting duplicate is not new money and notes stay private',async()=>{
    const before=await state(9005);
    await scenario('conflict');await command('/finance');await click('Все задачи',true);await click(/^F\d+ · открыть разбор$/);
    let p=await lastPanel();conflictCase=Number(p.text.match(/РАЗБОР F(\d+)/)[1]);
    assert.ok(p.text.includes('не несколько поступлений'));
    assert.ok(!p.markup.inline_keyboard.flat().some(b=>b.text.includes('Закрыть только')));
    await click('Взять финансовый разбор',true);await click('Внутренняя заметка');await command(privateFinanceNote);await click('Сохранить только заметку',true);
    assert.equal((await state(9005)).paid,before.paid);assert.equal((await state(9005)).delivered,before.delivered);
    await click(/^Заметка #/);
    assert.ok((await lastPanel()).text.includes('&lt;img'));
    assert.equal(await page.evaluate(()=>globalThis.__financeInjected),undefined);
    for(const uid of [420,421,9002,9003,9004])assert.ok(!(await state(uid)).messages.some(m=>m.text.includes('ТОЛЬКО ФИНАНСАМ')));
    await page.screenshot({path:path.join(out,'bot-demo-finance-note.png'),fullPage:true});
  });
  await check('native finance: own payment history redacts staff and foreign payer details',async()=>{
    await role(420);await command('/purchases');await click(/^№0001/);await click('История оплаты',true);
    let p=await lastPanel(420);assert.ok(p.text.includes('ПОКУПКА / ИСТОРИЯ ОПЛАТЫ'));
    assert.ok(p.text.includes('сведения проверяются'));assert.ok(p.text.includes('нет подтверждения выполненного возврата'));
    assert.ok(!/demo-charge-|ТОЛЬКО ФИНАНСАМ|payer_id|provider_id/.test(p.text));
    await page.screenshot({path:path.join(out,'bot-demo-payment-history.png'),fullPage:true});
    const other=await callback(421,'f:history:1:0');assert.ok(other.messages.at(-1).text.includes('Покупка не найдена'));
    await role(9003);await command('/finance');assert.ok((await lastPanel(9003)).text.includes('Нет прав'));
    await role(9005);await command(`/finance case ${conflictCase}`);assert.ok((await lastPanel()).text.includes('РАЗБОР F'));
  });
  await check('native finance: saved invoice invalidates stale action and closes task without paying',async()=>{
    const before=await state(9005);
    await scenario('attempt');await command('/finance');await click('Зависшие счета',true);await click(/^F\d+ · открыть разбор$/);
    let p=await lastPanel();attemptCase=Number(p.text.match(/РАЗБОР F(\d+)/)[1]);
    assert.ok(p.text.includes('результат создания неизвестен'));
    await click('Взять финансовый разбор',true);await click('Срок контроля');await click('Контроль через 4 ч');
    p=await lastPanel();const stale=p.markup.inline_keyboard.flat().find(b=>b.text==='Подтвердить срок контроля').callback_data;
    await scenario('issued');
    const rejected=await callback(9005,stale);assert.ok(rejected.messages.at(-1).text.includes('изменились'));
    await command(`/finance case ${attemptCase}`);await click('Закрыть только задачу');await click('Подтвердить рабочий этап',true);
    p=await lastPanel();assert.ok(p.text.includes('задача разбора закрыта'));
    const after=await state(9005);assert.equal(after.paid,before.paid);assert.equal(after.purchases,before.purchases+1);assert.equal(after.reserved,before.reserved+1);
    assert.equal(after.finance_closed,1);
    await page.screenshot({path:path.join(out,'bot-demo-finance-closed.png'),fullPage:true});
  });
  await check('native financial cards and controls are usable at 320/390/768 pixels',async()=>{
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});await role(9005);await command(`/finance case ${conflictCase}`);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      const buttons=await page.locator('#thread .bubble').last().locator('.inline-button').all();
      for(const button of buttons){const box=await button.boundingBox();if(box)assert.ok(box.height>=37);}
      if(width===390)await page.screenshot({path:path.join(out,'bot-demo-finance-mobile.png'),fullPage:false});
    }
  });
  await page.setViewportSize({width:1440,height:1000});
  let discoveryMoney, oldSelection;
  const assertNoPurchase=async()=>{
    const s=await state(420);
    assert.deepEqual([s.purchases,s.paid,s.reserved,s.delivered],discoveryMoney);
  };
  await check('native discovery: words, exact budget and same-variant availability',async()=>{
    await role(420);const s=await state(420);discoveryMoney=[s.purchases,s.paid,s.reserved,s.delivered];
    await command('/find чёрная футболка S до 5000 в наличии');
    let p=await lastPanel(420);assert.ok(p.text.includes('Найдено: <b>1</b>'));assert.ok(p.text.includes('Размер: S'));
    await page.screenshot({path:path.join(out,'bot-demo-discovery-search.png'),fullPage:true});
    await click('Фильтры',true);await click('Размер',true);await click('M',true);
    assert.ok((await lastPanel(420)).text.includes('Найдено: <b>0</b>'),'M is consumed; S availability must not satisfy M');
    await click('Сбросить подбор',true);assert.ok((await lastPanel(420)).text.includes('Найдено: <b>2</b>'));
    await assertNoPurchase();
  });
  await check('native discovery: favorite consent, persistence and no implicit checkout',async()=>{
    await click(/^СИЛА И ЧЕСТЬ ·/);await click('☆ В избранное бота',true);
    assert.equal((await state(420)).list_storage,false);assert.equal((await state(420)).saved_count,0);
    assert.ok((await lastPanel(420)).text.includes('не согласие на рекламу'));
    await click('Разрешить и добавить вещь',true);
    assert.equal((await state(420)).saved_count,1);assert.equal((await state(420)).list_storage,true);
    await command('/menu');await command('/saved');assert.ok((await lastPanel(420)).text.includes('СИЛА И ЧЕСТЬ'));
    await page.screenshot({path:path.join(out,'bot-demo-discovery-saved.png'),fullPage:true});
    await assertNoPurchase();
  });
  await check('native discovery: comparison uses live catalog facts without choosing a winner',async()=>{
    await click('Открыть вещь 1',true);await click('+ Сравнить',true);
    assert.equal((await state(420)).compare_count,1);
    await click('Добавить из подбора',true);await click(/^ЖЕТОН ·/);await click('+ Сравнить',true);
    assert.equal((await state(420)).compare_count,2);
    await click('Цена товаров →',true);await click('Материал →',true);
    const p=await lastPanel(420);assert.ok(p.text.includes('100% хлопок'));assert.ok(p.text.includes('Нержавеющая сталь'));assert.ok(p.text.includes('не выбирает победителя'));
    await page.screenshot({path:path.join(out,'bot-demo-discovery-compare.png'),fullPage:true});
    await click('Открыть вещь 2',true);
    oldSelection=(await lastPanel(420)).markup.inline_keyboard.flat().find(b=>b.text==='☆ В избранное бота').callback_data;
    await assertNoPurchase();
  });
  await check('native discovery: private lists, explicit erasure and retired action tokens',async()=>{
    for(const uid of [421,9001]){
      await role(uid);await command('/saved');
      assert.equal((await state(uid)).saved_count,0);assert.equal((await state(uid)).compare_count,0);
      assert.ok((await lastPanel(uid)).text.includes('СПИСКИ / СОГЛАСИЕ'));
    }
    await role(420);await command('/saved');await click('Настройки списков',true);await click('Удалить выбор и отключить');
    assert.equal((await state(420)).saved_count,1,'erasure must require confirmation');
    await click('Да, удалить и отключить',true);
    let s=await state(420);assert.equal(s.saved_count,0);assert.equal(s.compare_count,0);assert.equal(s.list_storage,false);
    s=await callback(420,oldSelection);assert.equal(s.saved_count,0);assert.equal(s.list_storage,false);
    await assertNoPurchase();
  });
  await check('native discovery screens remain readable and usable on mobile',async()=>{
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});await role(420);await command('/find чёрная футболка S до 5000');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      for(const b of await page.locator('#thread .bubble').last().locator('.inline-button').all()){
        const box=await b.boundingBox();if(box)assert.ok(box.height>=37);
      }
      if(width===390)await page.screenshot({path:path.join(out,'bot-demo-discovery-mobile.png'),fullPage:false});
    }
    await assertNoPurchase();
  });
  await page.setViewportSize({width:1440,height:1000});
  let repeatToken, couponCheckoutToken;
  await check('native promotions: owner creates an inactive campaign, then explicitly activates',async()=>{
    await role(9001);await command('/newpromo');await command('DEMO10');
    await click('Процент',true);await command('10');await command('500');await command('1900');
    await click('Выпуск',true);await click('Сейчас',true);await click('7 дней от начала',true);
    await command('2');await command('1');
    assert.equal((await state(9001)).promotion_count,0);
    await page.screenshot({path:path.join(out,'bot-demo-promotion-rules.png'),fullPage:true});
    await click('Создать выключенным',true);
    assert.equal((await state(9001)).promotions_active,0);assert.equal((await state(9001)).promotion_count,1);
    await click('Включить промокод');assert.equal((await state(9001)).promotions_active,0);
    await click('Да, включить',true);assert.equal((await state(9001)).promotions_active,1);
    assert.equal((await state(9001)).promotion_uses,0);assert.equal((await state(9001)).purchases,3);
  });
  await check('native repeat: financial exception is blocked; a new paid source is explicit',async()=>{
    await role(420);await command('/purchases');await click(/^№0001/);
    assert.ok(!(await lastPanel(420)).markup.inline_keyboard.flat().some(b=>b.text.includes('Повторить состав')));
    const denied=await callback(420,'g:repeat:1');assert.equal(denied.cart_quantity,0);assert.equal(denied.purchases,3);
    assert.ok((await lastPanel(420)).text.includes('без финансовых исключений'));
    await chooseTee('S');await click('+ Добавить вещь');await click(/^ЖЕТОН ·/);
    await click('Выбрать размер');await click('ONE SIZE',true);await click('Персонализация 2');await command('00234');
    await click('Оформить покупку');await click('Всё верно');
    await click('Выбрать оплату');await click('Звёзды Telegram',true);
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('.invoice-button').last().click();await done;
    const s=await state(420);assert.equal(s.purchases,4);assert.equal(s.paid,2);assert.equal(s.promotion_uses,0);
  });
  await check('native repeat: preview merges the cart, strips old personalization, no new payment',async()=>{
    await chooseTee('XL');assert.equal((await state(420)).cart_quantity,1);
    await command('/purchases');await click(/^№0004/);await click('Повторить состав');
    assert.equal((await state(420)).cart_quantity,1);
    const p=await lastPanel(420);assert.ok(p.text.includes('11 700 ₽'));assert.ok(p.text.includes('не заменяя'));
    repeatToken=p.markup.inline_keyboard.flat().find(b=>b.text==='Добавить состав в корзину').callback_data;
    await page.screenshot({path:path.join(out,'bot-demo-repeat-preview.png'),fullPage:true});
    await callback(421,repeatToken);assert.equal((await state(420)).cart_quantity,1);
    await click('Добавить состав в корзину',true);await callback(420,repeatToken);
    const s=await state(420);assert.equal(s.cart_quantity,3);assert.equal(s.purchases,4);assert.equal(s.paid,2);assert.equal(s.reserved,1);
    assert.ok(!(await lastPanel(420)).text.includes('00234'));
  });
  await check('native promotions: confirmed discount and retry create one immutable discounted purchase',async()=>{
    await command('/promo DEMO10');assert.equal((await state(420)).cart_promotion,null);
    assert.ok((await lastPanel(420)).text.includes('11 200 ₽'));
    await click('Применить к корзине',true);assert.equal((await state(420)).cart_promotion.code,'DEMO10');
    assert.equal((await state(420)).promotion_uses,0);
    await click('Оформить покупку');assert.ok((await lastPanel(420)).text.includes('−500 ₽'));
    await page.screenshot({path:path.join(out,'bot-demo-promotion-checkout.png'),fullPage:true});
    couponCheckoutToken=(await lastPanel(420)).markup.inline_keyboard.flat().find(b=>b.text.startsWith('Всё верно')).callback_data;
    await click('Всё верно');await callback(420,couponCheckoutToken);
    const s=await state(420);assert.equal(s.purchases,5);assert.equal(s.paid,2);assert.equal(s.promotion_uses,1);assert.equal(s.discount_rub,500);assert.equal(s.cart_promotion,null);
    assert.ok((await lastPanel(420)).text.includes('11 200 ₽'));
  });
  await check('native promotions: stopping new uses never reprices an issued purchase or refunds money',async()=>{
    await role(9001);await command('/promos');await click(/^DEMO10 ·/);await click('Остановить новые применения');
    assert.equal((await state(9001)).promotions_active,1);await click('Да, остановить',true);
    assert.equal((await state(9001)).promotions_active,0);assert.equal((await state(9001)).promotion_uses,1);
    await role(420);await command('/purchases');await click(/^№0005/);
    assert.ok((await lastPanel(420)).text.includes('11 200 ₽'));await click('Выбрать оплату');await click('Звёзды Telegram',true);
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator('.invoice-button').last().click();await done;
    const s=await state(420);assert.equal(s.paid,3);assert.equal(s.promotion_uses,1);assert.equal(s.discount_rub,500);
    await command('/purchases');await click(/^№0005/);
    await page.screenshot({path:path.join(out,'bot-demo-discounted-purchase.png'),fullPage:true});
  });
  await check('native growth: campaign ACL and mobile screens at 320/390/768 pixels',async()=>{
    await role(9003);await command('/promos');assert.ok(!(await lastPanel(9003)).markup.inline_keyboard.flat().some(b=>b.text.includes('Новый промокод')));
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});await role(9001);await command('/promos');await click(/^DEMO10 ·/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      for(const b of await page.locator('#thread .bubble').last().locator('.inline-button').all()){
        const box=await b.boundingBox();if(box)assert.ok(box.height>=37);
      }
      if(width===390)await page.screenshot({path:path.join(out,'bot-demo-promotion-mobile.png'),fullPage:false});
    }
  });
  await page.setViewportSize({width:1440,height:1000});
  const alertBusiness=await state(420);
  const unchangedAlerts=async()=>{
    const s=await state(420);for(const k of ['purchases','paid','reserved','delivered','promotion_uses','discount_rub'])assert.equal(s[k],alertBusiness[k],k+' changed by a product alert');
  };
  const signal=async kind=>{
    const done=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
    await page.locator(`[data-signal="${kind}"]`).click();assert.equal((await done).status(),200);
  };
  const alertSetup=async uid=>{
    await role(uid);await command('/alerts');await click('Настроить уведомление');await click('СИЛА И ЧЕСТЬ',true);
  };
  let stockWatchA,stockWatchB,oldAlertConsent,priceWatch;
  await check('native alerts: separate one-shot consent, no favorite import or implicit notification',async()=>{
    for(const uid of [420,421]){
      await alertSetup(uid);await click('Появление размера');await click(/^XXL ·/);
      const p=await lastPanel(uid);assert.ok(p.text.includes('ОТДЕЛЬНОЕ СОГЛАСИЕ'));assert.ok(p.text.includes('30 дней'));
      assert.equal((await state(uid)).alerts_active,0);
      const consent=p.markup.inline_keyboard.flat().find(b=>b.text==='Да, уведомить один раз').callback_data;
      if(uid===420){
        await callback(421,consent);assert.equal((await state(420)).alerts_active,0);
        await page.screenshot({path:path.join(out,'bot-demo-alerts-consent.png'),fullPage:true});
      }else oldAlertConsent=consent;
      await click('Да, уведомить один раз',true);
      const id=Number((await lastPanel(uid)).text.match(/Подписка #(\d+)/)[1]);
      if(uid===420)stockWatchA=id;else stockWatchB=id;
      assert.equal((await state(uid)).alerts_active,1);assert.equal((await state(uid)).alerts_sent,0);
    }
    await unchangedAlerts();
  });
  await check('native alerts: counted one-unit restock sends only the oldest signal and reserves nothing',async()=>{
    await role(9002);await command('/stock');await click(/СИЛА И ЧЕСТЬ.*XXL/);
    await click('Ввести подтверждённое количество');await command('1');
    assert.equal((await state(420)).alerts_sent,0);
    await click('Пересчёт верен · сохранить',true);
    await role(9001);await signal('check');
    const a=await state(420),b=await state(421);
    assert.equal(a.alerts_sent,1);assert.equal(a.alerts_active,0);assert.equal(b.alerts_sent,0);assert.equal(b.alerts_active,1);
    const m=a.messages.find(m=>m.text.includes('ОДНОКРАТНОЕ УВЕДОМЛЕНИЕ'));
    assert.ok(m.text.includes('Размер XXL'));assert.ok(m.text.includes('не резерв'));assert.ok(m.text.includes('Проверено:'));
    await role(420);await page.screenshot({path:path.join(out,'bot-demo-alerts-stock.png'),fullPage:true});
    await role(9001);await signal('check');assert.equal((await state(421)).alerts_sent,0);
    await unchangedAlerts();
  });
  await check('native alerts: one-step unsubscribe, owner isolation and retired subscription buttons',async()=>{
    await callback(9001,'a:watch:'+stockWatchB);assert.ok((await lastPanel(9001)).text.includes('не найдена'));
    await role(421);await command('/alerts');await click(new RegExp('Открыть подписку #0*'+stockWatchB+'$'));
    await callback(420,'a:off:'+stockWatchB);assert.equal((await state(421)).alerts_active,1);
    await click('Отключить эту подписку',true);assert.equal((await state(421)).alerts_active,0);
    await callback(421,oldAlertConsent);assert.equal((await state(421)).alerts_active,0);
    await unchangedAlerts();
  });
  await check('native alerts: real lower catalog price and an explicitly simulated lost acknowledgement',async()=>{
    await alertSetup(421);await click('Снижение цены');await click(/^Не выше /);
    assert.equal((await state(421)).alerts_active,0);await click('Да, уведомить один раз',true);
    priceWatch=Number((await lastPanel(421)).text.match(/Подписка #(\d+)/)[1]);
    await role(9001);await signal('uncertain');await signal('price_drop');
    const s=await state(421);assert.equal(s.alerts_uncertain,1);assert.equal(s.alerts_sent,0);
    const m=s.messages.find(m=>m.text.includes('ОДНОКРАТНОЕ УВЕДОМЛЕНИЕ'));
    assert.ok(m.text.includes('4 400 ₽'));assert.ok(m.text.includes('4 900 ₽'));assert.ok(m.text.includes('Наличие размера этим сигналом не подтверждается'));
    await unchangedAlerts();
  });
  await check('native alerts: unknown outcome is never auto-retried; global opt-out erases private watch records',async()=>{
    const count=()=>state(421).then(s=>s.messages.filter(m=>m.text.includes('ОДНОКРАТНОЕ УВЕДОМЛЕНИЕ')).length);
    const before=await count();await signal('check');await signal('check');assert.equal(await count(),before);
    await role(421);await command('/alerts');await click('История',true);await click(new RegExp('Открыть подписку #0*'+priceWatch+'$'));
    assert.ok((await lastPanel(421)).text.includes('Сообщение могло прийти'));
    await page.screenshot({path:path.join(out,'bot-demo-alerts-uncertain.png'),fullPage:true});
    await command('/alertsoff');let s=await state(421);assert.equal(s.alerts_active,0);assert.equal(s.alerts_uncertain,0);
    await callback(421,oldAlertConsent);s=await state(421);assert.equal(s.alerts_active,0);assert.equal(s.alerts_uncertain,0);
    await unchangedAlerts();
  });
  await check('native product alert screens and controls remain usable at 320/390/768 pixels',async()=>{
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});await role(420);await command('/alerts');await click('История',true);
      await click(new RegExp('Открыть подписку #0*'+stockWatchA+'$'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      for(const b of await page.locator('#thread .bubble').last().locator('.inline-button').all()){
        const box=await b.boundingBox();if(box)assert.ok(box.height>=37);
      }
      if(width===390)await page.screenshot({path:path.join(out,'bot-demo-alerts-mobile.png'),fullPage:false});
    }
    await unchangedAlerts();
  });
  assert.deepEqual(errors,[]);
})().catch(error=>{errors.push(error.message);console.error(error);process.exitCode=1}).finally(async()=>{
  fs.writeFileSync(path.join(out,'bot-demo-report.json'),JSON.stringify({checks,errors},null,2));
  if(browser)await browser.close();
});
