const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('miniapp/service.js', 'utf8');
const escapeHTML = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setup(request) {
  const container = {html:'', buttons:[], set innerHTML(v){this.html=v;this.buttons=[]}, get innerHTML(){return this.html}, append(b){this.buttons.push(b)}, insertAdjacentHTML(_,s){this.html+=s}, replaceChildren(){this.html='';this.buttons=[]}};
  const window = {};
  vm.runInNewContext(source, {window, document:{createElement(){return {addEventListener(_, fn){this.run=fn}}}}, Intl, Date, Number, String, encodeURIComponent});
  return {container, view:window.ShopService.create({container, request, escapeHTML})};
}
const ticket = {ticket_id:1, topic_label:'Вопрос', status_label:'Открыто', purchase_id:null, messages:[{source:'customer', created_at:'2026-09-11T12:00:00+00:00', body:'PUBLIC TEXT'}], has_more:false};
const plan = {purchase_id:1, state_label:'ещё не передано', quote:null, budget_minor:null, blocking_tickets:[], events:[], operations_managed:true};

test('service view does not invent a tariff when no agreement exists', async()=>{
  const {view,container} = setup(async()=>({delivery:plan}));
  await view.load('delivery',1);
  assert.match(container.html,/Условия ещё не согласованы/);
  assert.doesNotMatch(container.html,/Общий бюджет/);
});
test('service view preserves an explicit zero fee and separates the goods invoice',async()=>{
  const {view,container}=setup(async()=>({delivery:{...plan, budget_minor:490000, quote:{quote_id:1,status:'accepted',carrier_label:'Самовывоз',amount_label:'0 ₽',amount_minor:0,billing:'shop',eta:'После подготовки',basis:'Выдача без доплаты',destination:'Тестовая точка',expires_at:1800000000}}}));
  await view.load('delivery',1);
  assert.match(container.html,/0 ₽/);assert.match(container.html,/Это не новый счёт/);assert.match(container.html,/без доплаты/);
});
test('support message and delivery exception text are HTML escaped',async()=>{
  const {view,container}=setup(async()=>({ticket:{...ticket,messages:[{...ticket.messages[0],body:'<img src=x onerror=alert(1)> "PRIVATE"'}]}}));
  await view.load('ticket',1);
  assert.doesNotMatch(container.html,/<img /);assert.match(container.html,/&lt;img /);
  assert.match(container.html,/&quot;PRIVATE&quot;/);
});
test('a slower older request cannot replace the newly selected thread',async()=>{
  let release;
  const pending=new Promise(r=>release=r);
  const {view,container}=setup(path=>path.includes('purchase_id')?pending:Promise.resolve({ticket}));
  const old=view.load('delivery',1);
  await view.load('ticket',1);
  release({delivery:plan});await old;
  assert.match(container.html,/PUBLIC TEXT/);assert.doesNotMatch(container.html,/Условия ещё/);
});
test('closing a service view erases its DOM and discards an in-flight PII response',async()=>{
  let release;const pending=new Promise(r=>release=r);
  const {view,container}=setup(()=>pending);
  const load=view.load('ticket',1);view.clear();release({ticket});await load;
  assert.equal(container.html,'');assert.equal(container.buttons.length,0);
});
test('server error is escaped and an explicit retry makes a fresh read',async()=>{
  let count=0;
  const {view,container}=setup(async()=>{if(!count++)throw Error('<b>offline</b>');return {ticket}});
  await view.load('ticket',1);
  assert.match(container.html,/&lt;b&gt;offline/);
  await container.buttons.find(b=>b.textContent==='Повторить загрузку').run();
  assert.match(container.html,/PUBLIC TEXT/);assert.equal(count,2);
});
test('support history navigation uses server pagination and refreshes the current page',async()=>{
  const paths=[];
  const {view,container}=setup(async path=>{paths.push(path);return {ticket:{...ticket,has_more:true}}});
  await view.load('ticket',1);
  await container.buttons.find(b=>b.textContent==='Ранние сообщения →').run();
  await view.refresh();
  assert.equal(paths[1],'/api/service?ticket_id=1&page=1');assert.equal(paths[2],paths[1]);
  assert.doesNotMatch(source,/localStorage|sessionStorage|method\s*:\s*['"]POST/);
});
