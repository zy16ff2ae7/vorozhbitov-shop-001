(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  let actor = 420, busy = false, fingerprint = '', timer;
  const thread = $('#thread');
  function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(timer); timer = setTimeout(() => $('#toast').classList.remove('visible'), 6000); }
  function telegramText(text) {
    const parsed = new DOMParser().parseFromString(text, 'text/html');
    const fragment = document.createDocumentFragment();
    const allowed = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'CODE', 'PRE', 'BR']);
    function copy(node, parent) {
      if (node.nodeType === 3) { parent.append(document.createTextNode(node.textContent)); return; }
      if (node.nodeType !== 1) return;
      if (allowed.has(node.tagName)) {
        const clean = document.createElement(node.tagName.toLowerCase());
        for (const child of node.childNodes) copy(child, clean);
        parent.append(clean);
      } else { parent.append(document.createTextNode(node.textContent)); }
    }
    for (const child of parsed.body.childNodes) copy(child, fragment);
    return fragment;
  }
  function render(data, force = false) {
    if (data.actor !== actor) return;
    $('#sessionRole').textContent = data.role.toUpperCase();
    $('#financeLab').hidden = !data.finance_access;
    $('#financeQuick').hidden = !data.finance_access;
    $('#financeCaseCount').textContent = data.finance_cases;
    $('#financeDueCount').textContent = data.finance_overdue;
    $('#purchaseCount').textContent = data.purchases;
    $('#reserveCount').textContent = data.reserved;
    $('#paidCount').textContent = data.paid;
    $('#ticketCount').textContent = data.tickets;
    $('#shipmentCount').textContent = data.shipments;
    $('#deliveredCount').textContent = data.delivered;
    $('#alertsActive').textContent = data.alerts_active;
    $('#alertsSent').textContent = data.alerts_sent;
    $('#alertsUncertain').textContent = data.alerts_uncertain ? `Неизвестный исход: ${data.alerts_uncertain}. Автоматических повторов нет.` : '';
    $('#signalLab').hidden = !data.alerts_access;
    $('#promotionCount').textContent = data.promotion_count;
    $('#promotionUses').textContent = data.promotion_uses;
    $('#cartPromotionLabel').textContent = data.cart_promotion ? `В корзине: ${data.cart_promotion.code}. Итог проверяется при оформлении в чате.` : 'В текущей корзине промокод не выбран.';
    $('#savedCount').textContent = data.saved_count;
    $('#compareCount').textContent = data.compare_count;
    $('#listStorage').textContent = data.list_storage ? 'Списки хранятся с разрешения. Сигналы настраиваются отдельно; сохранение не создаёт резерв.' : 'Хранение списков выключено. Не подписка и не резерв.';
    $('#stockAvailable').textContent = data.stock.available;
    $('#stockReserved').textContent = data.stock.reserved;
    $('#exampleInput').hidden = !data.example;
    $('#exampleInput').textContent = data.example ? `Вставить: ${data.example}` : '';
    const receiptLabels = { applied: 'Поступление подтверждено', refund_required: 'Требуется возврат', review_required: 'Поступление на сверке' };
    $('#receipts').replaceChildren(...(data.receipts.length ? data.receipts : [{status:'empty'}]).map(row => {
      const p = document.createElement('p'); p.className = row.status === 'empty' ? 'empty' : ''; p.dataset.status = row.disputed ? 'review_required' : row.status;
      p.textContent = row.disputed ? 'Противоречивый повтор · не новая оплата' : row.source === 'legacy' ? 'Исторический импорт · не новая оплата' : receiptLabels[row.status] || 'Пока без поступлений.'; return p;
    }));
    const actions = { 'promotion.created': 'Создан выключенный промокод', 'promotion.enabled': 'Промокод включён', 'promotion.disabled': 'Новые применения остановлены', 'finance.claimed': 'Назначен финансовый разбор', 'finance.released': 'Финансовая задача возвращена', 'finance.scheduled': 'Назначен срок контроля', 'finance.deadline_cleared': 'Срок контроля снят', 'finance.note': 'Приватная финансовая заметка', 'finance.step': 'Обновлён этап разбора', 'finance.role_revoked': 'Финансовая роль отозвана', 'staff.role': 'Изменены права сотрудника', 'purchase.claim': 'Покупка взята в работу', 'purchase.stage': 'Обновлён этап покупки', 'purchase.release': 'Покупка передана в очередь', 'inventory.count': 'Подтверждён остаток', 'delivery.offered': 'Предложены условия доставки', 'delivery.accepted': 'Условия приняты покупателем', 'delivery.dispatched': 'Зафиксирована передача', 'delivery.received': 'Зафиксировано вручение', 'support.reply': 'Ответ в обращении', 'support.note': 'Внутренняя заметка', 'support.resolve': 'Обращение закрыто', 'ticket_claim': 'Обращение взято в работу' };
    $('#audit').replaceChildren(...data.audit.map(row => {
      const p = document.createElement('p'); p.textContent = `${actions[row.action] || row.action}\n${row.actor_id} → ${row.entity}`; return p;
    }));
    const next = JSON.stringify(data.messages);
    if (next === fingerprint && !force) return;
    fingerprint = next;
    const wasBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 100;
    const previousScroll = thread.scrollTop;
    thread.replaceChildren();
    for (const message of data.messages) {
      const bubble = document.createElement('article'); bubble.className = 'bubble' + (message.human ? ' human' : '');
      bubble.dataset.messageId = message.message_id;
      if (message.image?.startsWith('/shop-assets/')) { const img = document.createElement('img'); img.src = message.image; img.alt = 'Вещь из выпуска'; img.className = 'message-image'; bubble.append(img); }
      const text = document.createElement('div'); text.className = 'message-text';
      if (message.human) text.textContent = message.text;
      else text.append(telegramText(message.text));
      bubble.append(text);
      const keyboard = document.createElement('div'); keyboard.className = 'keyboard';
      for (const row of message.markup?.inline_keyboard || []) {
        const line = document.createElement('div'); line.className = 'button-row';
        for (const item of row) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'inline-button'; button.textContent = item.text; button.dataset.callback = item.callback_data || '';
          button.addEventListener('click', () => item.callback_data ? send({callback:item.callback_data,message_id:message.message_id}) : toast('Внешние ссылки в демо не открываются. Всё оформление работает здесь.'));
          line.append(button);
        }
        keyboard.append(line);
      }
      if (keyboard.children.length) bubble.append(keyboard);
      if (message.invoice) {
        const pay = document.createElement('button'); pay.type = 'button'; pay.className = 'invoice-button'; pay.textContent = 'Подтвердить тестовую оплату · 0 реальных списаний';
        pay.addEventListener('click', () => send({pay:message.invoice})); bubble.append(pay);
        const late = document.createElement('button'); late.type = 'button'; late.className = 'late-button'; late.textContent = 'Тест: поступление после истечения резерва';
        late.addEventListener('click', () => send({pay:message.invoice,late:true})); bubble.append(late);
      }
      const meta = document.createElement('div'); meta.className = 'message-meta'; meta.textContent = message.human ? 'отправлено ✓' : (message.edited ? 'обновлено · ' : '') + 'бот'; bubble.append(meta);
      thread.append(bubble);
    }
    if (force) {
      const latest = thread.lastElementChild;
      if (latest) thread.scrollTop += latest.getBoundingClientRect().top - thread.getBoundingClientRect().top - 12;
    } else if (wasBottom) thread.scrollTop = thread.scrollHeight;
    else thread.scrollTop = previousScroll;
  }
  async function load(force = false) {
    const requestedActor = actor;
    try {
      const response = await fetch(`/demo/api/state?actor=${requestedActor}`); const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      render(data, force);
    } catch (error) { toast(error.message || 'Демо временно недоступно.'); }
  }
  async function send(action) {
    if (busy) return;
    busy = true; $('#sendMessage').disabled = true;
    try {
      const response = await fetch('/demo/api/action', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({actor,...action}) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      render(data, true);
    } catch (error) { toast(error.message || 'Не удалось отправить.'); }
    finally { busy = false; $('#sendMessage').disabled = false; }
  }
  document.querySelectorAll('[data-actor]').forEach(button => button.addEventListener('click', () => {
    if (busy) return; actor = Number(button.dataset.actor); fingerprint = '';
    document.querySelectorAll('[data-actor]').forEach(b => { b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', String(b === button)); });
    load(true);
  }));
  document.querySelectorAll('[data-command]').forEach(b => b.addEventListener('click', () => send({text:b.dataset.command})));
  $('#composer').addEventListener('submit', event => { event.preventDefault(); const text = $('#messageInput').value.trim(); if (text && !busy) { $('#messageInput').value = ''; send({text}); } });
  $('#exampleInput').addEventListener('click', () => send({example:true}));
  document.querySelectorAll('[data-finance]').forEach(button => button.addEventListener('click', () => send({finance:button.dataset.finance})));
  document.querySelectorAll('[data-signal]').forEach(button => button.addEventListener('click', () => send({signal:button.dataset.signal})));
  $('#expireReservations').addEventListener('click', () => send({expire:true}));
  load(true);
  setInterval(() => { if (!busy && !document.hidden) load(); }, 4000);
})();
