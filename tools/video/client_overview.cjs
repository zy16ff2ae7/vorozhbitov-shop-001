/* Client-facing video. Drives the real offline demo; never fabricates bot panels.
 * Requires a FRESH tools/bot_demo.py on VIDEO_DEMO_URL (default port 4175).
 * VIDEO_DRY=1 rehearses quickly without recording; final output stays outside Git.
 */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'../..');
const OUT=path.resolve(process.env.VIDEO_WORK||'/home/user/.cache/bot-video');
const URL=process.env.VIDEO_DEMO_URL||'http://127.0.0.1:4175';
const DRY=process.env.VIDEO_DRY==='1';
const PACE=Number(process.env.VIDEO_PACE||(DRY?'0.03':'1'));
fs.mkdirSync(OUT,{recursive:true});
let browser,context,page,started=0,video;
const timeline=[],errors=[];
const wait=ms=>page.waitForTimeout(Math.max(30,ms*PACE));
const time=()=>Math.round((Date.now()-started)/10)/100;
function note(kind,data){const row={at:time(),kind,...data};timeline.push(row);console.log(JSON.stringify(row));}
async function caption(text){await page.locator('#videoCaption').evaluate((el,t)=>{el.textContent=t;el.animate([{opacity:0,transform:'translateY(6px)'},{opacity:1,transform:'translateY(0)'}],{duration:240,fill:'both'});},text);note('caption',{text});}
async function chapter(number,kicker,title,deck,pill){
  await page.evaluate(({number,kicker,title,deck,pill})=>{
    document.body.dataset.videoChapter=number;
    document.querySelector('#videoChapter').textContent=String(number).padStart(2,'0')+' / '+kicker;
    document.querySelector('#videoTitle').textContent=title;
    document.querySelector('#videoDeck').textContent=deck;
    document.querySelector('#videoPill').textContent=pill;
    document.querySelector('#videoProgress i').style.width=(number/8*100)+'%';
    for(const selector of ['#videoChapter','#videoTitle','#videoDeck','#videoPill'])document.querySelector(selector).animate([{opacity:0,transform:'translateX(-10px)'},{opacity:1,transform:'translateX(0)'}],{duration:430,fill:'both'});
  },{number,kicker,title,deck,pill});
  note('chapter',{number,kicker,title,deck,pill});await wait(500);
}
async function point(locator){
  await locator.scrollIntoViewIfNeeded();
  const b=await locator.boundingBox();assert.ok(b,'Visible target required');
  await page.mouse.move(b.x+b.width*.65,b.y+b.height*.5,{steps:DRY?1:18});
  await locator.evaluate(el=>el.classList.add('videoHighlight'));await wait(250);
}
async function clickLocator(locator,hold=650){
  await point(locator);
  const response=page.waitForResponse(r=>r.url().endsWith('/demo/api/action'));
  await locator.click();const r=await response;assert.equal(r.status(),200,await r.text());
  await r.finished();await wait(hold);
  await page.evaluate(()=>document.querySelectorAll('.videoHighlight').forEach(e=>e.classList.remove('videoHighlight')));
}
async function click(name,exact=false,hold=650){
  const target=page.locator('#thread').getByRole('button',{name,exact}).last();
  await clickLocator(target,hold);note('click',{label:String(name)});
}
async function role(uid){
  const el=page.locator(`[data-actor="${uid}"]`);await point(el);
  const response=page.waitForResponse(r=>r.url().endsWith('/demo/api/state?actor='+uid));
  await el.click();const r=await response;assert.equal(r.status(),200);await r.finished();await wait(700);
  await page.evaluate(()=>document.querySelectorAll('.videoHighlight').forEach(e=>e.classList.remove('videoHighlight')));
  note('role',{uid});
}
async function command(text,hold=650,type=true){
  const el=page.locator('#messageInput');await point(el);await el.fill('');
  if(type&&!DRY)await el.pressSequentially(text,{delay:Math.max(12,25*PACE)});else await el.fill(text);
  await clickLocator(page.locator('#sendMessage'),hold);note('message',{text});
}
async function example(hold=400){await clickLocator(page.locator('#exampleInput'),hold);}
async function state(uid=420){const r=await page.request.get(URL+'/demo/api/state?actor='+uid);assert.equal(r.status(),200);return r.json();}
async function panel(uid=420){return (await state(uid)).messages.filter(m=>!m.human).at(-1);}
async function shot(name){if(DRY)await page.evaluate(()=>{document.getAnimations().forEach(a=>a.finish());document.querySelectorAll('.vRipple').forEach(e=>e.remove());const c=document.querySelector('#videoCursor');if(c)c.style.opacity='0';});await page.screenshot({path:path.join(OUT,name+'.png')});note('screenshot',{name});}
async function installPresentation(){
  await page.addStyleTag({path:path.join(__dirname,'client_overview.css')});
  await page.evaluate(()=>{
    const rail=document.createElement('aside');rail.id='videoRail';
    rail.innerHTML='<div id="videoBrand"><span class="vmark">V</span><div>ВОРОЖБИТОВ<small>СИЛА И ЧЕСТЬ</small></div></div><div id="videoChapter"></div><h1 id="videoTitle"></h1><p id="videoDeck"></p><div id="videoPill"></div>';
    const roles=document.querySelector('.role-switch');roles.querySelector('h2').textContent='РОЛИ В ДЕМОНСТРАЦИИ';rail.append(roles);document.body.append(rail);
    const top=document.createElement('header');top.id='videoTop';top.innerHTML='<span>ФУНКЦИОНАЛЬНЫЙ ВИДЕООБЗОР</span><span id="videoBadge">ТЕСТОВЫЕ ДАННЫЕ · БЕЗ РЕАЛЬНЫХ СПИСАНИЙ</span>';document.body.append(top);
    const caption=document.createElement('div');caption.id='videoCaption';document.body.append(caption);
    const progress=document.createElement('div');progress.id='videoProgress';progress.innerHTML='<i></i>';document.body.append(progress);
    const foot=document.createElement('div');foot.id='videoFoot';foot.textContent='WEB-СИМУЛЯТОР · РЕАЛЬНЫЕ ОБРАБОТЧИКИ БОТА';document.body.append(foot);
    const cursor=document.createElement('div');cursor.id='videoCursor';document.body.append(cursor);
    document.addEventListener('mousemove',e=>{cursor.style.left=e.clientX+'px';cursor.style.top=e.clientY+'px';});
    document.addEventListener('pointerdown',e=>{const r=document.createElement('div');r.className='vRipple';r.style.left=e.clientX+'px';r.style.top=e.clientY+'px';document.body.append(r);setTimeout(()=>r.remove(),800);});
    const curtain=document.createElement('div');curtain.id='videoCurtain';
    curtain.innerHTML='<span class="vmark">V</span><div class="wordmark">ВОРОЖБИТОВ<small>СИЛА И ЧЕСТЬ</small></div><div id="curtainKicker">МАГАЗИН БРЕНДА / TELEGRAM</div><h1 id="curtainTitle">Магазин.\nВ одном\nдиалоге.</h1><p id="curtainDeck">От выбора вещи до работы команды.\nОбзор реализованной логики бота.</p><div id="curtainMeta">Браузерная демонстрация, не запись боевого Telegram.\nДанные и платежи — тестовые.</div><div id="curtainVisual"><div class="vProduct tee"><img src="/shop-assets/assets/spin/tee-sich-01-v2.jpg"><span>01 / ФУТБОЛКА <b>СИЛА И ЧЕСТЬ</b></span></div><div class="vProduct tag"><img src="/shop-assets/assets/spin/tag-sich-01.jpg"><span>02 / ЖЕТОН <b>ДЕТАЛЬ БРЕНДА</b></span></div></div>';
    document.body.append(curtain);
  });
  await page.evaluate(()=>document.fonts.ready);
  await page.waitForFunction(()=>[...document.querySelectorAll('#curtainVisual img')].every(i=>i.complete&&i.naturalWidth>0));
}
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||'/tmp/chromium',args:['--no-sandbox','--disable-dev-shm-usage']});
  context=await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1,bypassCSP:true,reducedMotion:'no-preference',...(!DRY?{recordVideo:{dir:path.join(OUT,'raw'),size:{width:1920,height:1080}}}:{})});
  page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',e=>errors.push(e.message));
  await page.goto(URL);await page.locator('.bubble').first().waitFor();assert.equal((await state()).purchases,0,'Use a fresh demo database');
  video=page.video();await installPresentation();if(DRY)await page.addStyleTag({content:'*{transition:none!important;animation:none!important;scroll-behavior:auto!important}'});started=Date.now();note('intro',{});
  await shot('00-title');await wait(6600);
  await page.locator('#videoCurtain').evaluate(el=>el.classList.add('off'));await wait(800);

  await chapter(1,'ВЫБОР ВЕЩИ','Найти.\nСравнить.\nСохранить.','Каталог, размеры и цена —\nпрямо в диалоге.\nТолько доступные данные.','Основной интерфейс — чат');
  await caption('Начинаем с поиска: название, размер, бюджет и подтверждённое наличие.');
  await command('/find футболка S до 5000 в наличии',3100);
  assert.ok((await panel()).text.includes('Найдено: <b>1</b>'));await shot('01-search');
  await click(/^СИЛА И ЧЕСТЬ ·/,false,1800);
  await caption('Избранное хранится отдельно — только после разрешения покупателя.');
  await click('☆ В избранное бота',true,2000);await click('Разрешить и добавить вещь',true,900);
  await click('Открыть вещь 1',true);await click('+ Сравнить',true);
  await click('Добавить из подбора',true);await click('Сбросить подбор',true);await click(/^ЖЕТОН ·/);await click('+ Сравнить',true);
  await click('Цена товаров →',true);await click('Материал →',true,2200);
  await caption('Сравнение по фактам: хлопок и сталь, размеры, посадка и текущая цена.');
  assert.equal((await state()).compare_count,2);await shot('02-comparison');await wait(2400);

  await chapter(2,'КОРЗИНА И ПОКУПКА','Оформить.\nБез лишних\nпереходов.','Корзина сохраняет состав.\nДанные получателя —\nпосле отдельного согласия.','Корзина ещё не резерв');
  await caption('Добавляем футболку размера S. Просмотр и корзина не блокируют склад.');
  await command('/menu',300,false);await click('Смотреть выпуск');await click(/^СИЛА И ЧЕСТЬ ·/);
  await click('Выбрать размер');await click('S',true,1600);assert.equal((await state()).purchases,0);
  await click('Оформить покупку',false,1700);await click('Согласен',false,600);
  await caption('Вводим только тестовые данные. Контактные поля не запрашиваются до согласия.');
  for(let i=0;i<4;i++)await example(480);
  await click('СДЭК',true,2800);assert.ok((await panel()).text.includes('4 900 ₽'));await shot('03-checkout');
  await caption('Сначала — проверка состава и суммы. Доставка согласуется отдельно.');await wait(2200);
  await click('Всё верно',false,1700);assert.equal((await state()).purchases,1);assert.equal((await state()).reserved,1);
  await caption('После подтверждения появляется одна покупка и резерв на 60 минут.');await wait(2100);
  await click('Выбрать оплату');await click('Звёзды Telegram',true,1200);
  await caption('Здесь имитируется поступление. Реальные деньги не списываются.');
  await clickLocator(page.locator('.invoice-button').last(),1500);
  assert.equal((await state()).paid,1);assert.equal((await state()).reserved,0);
  await command('/purchases',300,false);await click(/^№0001/,false,2300);await shot('04-paid');

  await chapter(3,'РАБОТА КОМАНДЫ','Один заказ.\nОдин\nответственный.','Команда видит ту же покупку.\nОплата, сборка и вручение —\nразные состояния.','Права проверяются по роли');
  await caption('Менеджер получает покупку целиком и берёт её в работу.');
  await role(9003);await command('/team',350,false);await click('К сборке',true);await click(/^№0001/);
  await click('Взять в работу',false,1300);
  for(const stage of ['собираем','готов к выдаче']){await click('Следующий этап · '+stage);await click('Да, этап выполнен',false,1100);}
  assert.equal((await state()).delivered,0);await shot('05-assembly');
  await caption('Готовность к выдаче не означает вручение: доставку нельзя перескочить.');await wait(2000);

  await chapter(4,'ДОСТАВКА И ПОДДЕРЖКА','Согласовать.\nПередать.\nПолучить.','Стоимость и ПВЗ подтверждает\nпокупатель. Вопросы и ответы\nостаются связанными с заказом.','Не выдумываем тариф перевозчика');
  await click('Доставка / трек / вручение');await click('Предложить новые условия');await click('СДЭК',true);await click('Покупатель платит перевозчику');
  for(let i=0;i<4;i++)await example(420);
  await caption('В демо введён тестовый тариф: 350,50 ₽ напрямую перевозчику, отдельно от товаров.');
  await wait(2200);await click('Проверено · отправить условия',false,700);
  await role(420);await command('/purchases',300,false);await click(/^№0001/);await click('Доставка / условия / трек',false,2100);
  await click('Условия подходят · принять',false,1800);await shot('06-delivery');
  await caption('Покупатель принимает условия. Обращение можно открыть в том же диалоге.');
  await click('Вопрос / изменить адрес');await click('Вопрос',true);await command('Подскажите, как получить мою покупку?',450);
  await click('Отправить сообщение',true,450);
  await role(9004);await command('/desk',350,false);await click(/^№0001/);await click('Взять обращение в работу');
  await click('Ответить и закрыть');await command('ПВЗ согласован. После передачи в карточке появится трек.',450);
  await click('Подтвердить закрытие',true,1800);
  assert.equal((await state()).tickets,0);await shot('07-support');
  await caption('Поддержка отвечает по существу. Закрытие обращения не меняет оплату.');await wait(1400);
  await role(9003);await command('/shipping',300,false);await click('К передаче',true);await click(/^№0001/);
  await click('Передано перевозчику');await command('DEMO123456',350);await click('Факт проверен · зафиксировать',false,700);
  await role(420);await command('/purchases',300,false);await click(/^№0001/);await click('Доставка / условия / трек');
  await click('Вещи у меня · подтвердить');await click('Да, вручение состоялось',true,1800);
  assert.equal((await state()).delivered,1);await caption('Вручение фиксируется отдельным подтверждением. Трек и события здесь тестовые.');await wait(2000);

  await chapter(5,'ФИНАНСОВЫЙ КОНТРОЛЬ','Деньги —\nотдельно\nот сборки.','Финансовая роль видит\nпоступления и связанные\nс ними покупки.','Рабочий статус не подтверждает деньги');
  await role(9005);await command('/finance',2200,false);await shot('08-finance');
  await caption('Финансовый пульт отделён от склада. Возврат денег не выполняется кнопкой статуса.');
  await click('Журнал поступлений',true,2900);await shot('09-receipts');
  assert.equal((await state()).paid,1);await wait(1000);

  await chapter(6,'ПОВТОР И ПРОМОКОДЫ','Вернуться.\nПроверить.\nПовторить.','Повтор переносит состав\nв актуальную корзину.\nСтарый платёж не используется.','Новая покупка — только явно');
  await caption('Повторяем оплаченную покупку. Цена, размер и остаток проверяются заново.');
  await role(420);await command('/purchases',350,false);await click(/^№0001/);
  await click('Повторить состав',false,2600);await shot('10-repeat');await click('Добавить состав в корзину',true,1500);
  assert.equal((await state()).purchases,1);assert.equal((await state()).cart_quantity,1);
  await caption('Владелец задаёт правила промокода. Новый код создаётся выключенным.');
  await role(9001);await command('/newpromo',350,false);await command('DEMO10',250);
  await click('Процент',true,220);await command('10',220);await command('500',220);await command('1900',220);
  await click('Выпуск',true,220);await click('Сейчас',true,220);await click('7 дней от начала',true,220);
  await command('10',220);await command('1',2000);await shot('11-promo-rules');
  await click('Создать выключенным',true,700);assert.equal((await state()).promotions_active,0);
  await click('Включить промокод',false,500);await click('Да, включить',true,500);
  await role(420);await command('/promo DEMO10',2300);
  assert.ok((await panel()).text.includes('4 410 ₽'));await caption('Промокод показывает скидку и новый итог. Прежняя покупка остаётся на старую сумму.');
  await click('Применить к корзине',true,1000);await click('Оформить покупку',false,3000);
  assert.equal((await state()).purchases,1);await shot('12-discount');

  await chapter(7,'ОТДЕЛЬНОЕ СОГЛАСИЕ','Нужный сигнал.\nБез лишних\nсообщений.','Размер и снижение цены —\nразные разовые условия.\nНичего не включается из избранного.','Отписка одной командой /alertsoff');
  await caption('Уведомление о размере — отдельное согласие на один сигнал, не резерв.');
  await command('/alerts',450,false);await click('Настроить уведомление');await click('СИЛА И ЧЕСТЬ',true);await click('Появление размера');
  await click(/^XXL ·/,false,2000);assert.equal((await state()).alerts_active,0);await shot('13-alert-consent');
  await click('Да, уведомить один раз',true,700);
  await role(9002);await command('/stock',350,false);await click(/СИЛА И ЧЕСТЬ.*XXL/);
  await click('Ввести подтверждённое количество');await command('1',350);await click('Пересчёт верен · сохранить',true,800);
  await caption('Подтверждённое наличие запускает однократный сигнал. Покупка сама не создаётся.');
  await wait(2100);await role(420);
  await page.waitForFunction(()=>document.querySelector('#thread').textContent.includes('ОДНОКРАТНОЕ УВЕДОМЛЕНИЕ'),null,{timeout:12000});
  assert.equal((await state()).alerts_sent,1);await wait(2400);await shot('14-alert-sent');
  await command('/alertsoff',2000,false);assert.equal((await state()).alerts_active,0);
  await caption('Подписки можно отключить и удалить. Уже начатое сообщение может дойти.');await wait(1400);

  note('outro',{});
  await page.evaluate(()=>{
    const curtain=document.querySelector('#videoCurtain');curtain.classList.add('outro');curtain.classList.remove('off');
    document.querySelector('#curtainKicker').textContent='ОТ ДЕМОНСТРАЦИИ — К ЗАПУСКУ';
    document.querySelector('#curtainTitle').textContent='Логика работает.\nСледующий шаг —\nбоевой контур.';
    document.querySelector('#curtainDeck').textContent='Развернуть постоянный сервер и настоящего Telegram-бота.\nПодтвердить реальные остатки, условия и пройти проверки интеграций.';
    document.querySelector('#curtainMeta').textContent='Показана работа тестового контура. Реальные платежи и отправления не выполнялись.';
    document.querySelector('#videoProgress i').style.width='100%';
    document.querySelector('#videoCursor').style.display='none';
  });
  await wait(800);await shot('15-finale');await wait(8200);
  const final=await state();assert.equal(final.purchases,1);assert.equal(final.paid,1);assert.equal(final.delivered,1);assert.equal(final.promotion_uses,0);
  assert.deepEqual(errors,[]);note('complete',{state:{purchases:final.purchases,paid:final.paid,delivered:final.delivered,cart_quantity:final.cart_quantity},duration:time()});
})().catch(async e=>{
  errors.push(e.message);console.error(e);
  if(page)try{await page.screenshot({path:path.join(OUT,'failure.png')});}catch{}
  process.exitCode=1;
}).finally(async()=>{
  const result={dry:DRY,pace:PACE,root:URL,timeline,errors};
  if(context)await context.close();
  if(video)result.raw_video=await video.path();
  if(browser)await browser.close();
  fs.writeFileSync(path.join(OUT,'capture.json'),JSON.stringify(result,null,2));
  console.log('Saved',path.join(OUT,'capture.json'));
});
