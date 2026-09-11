/* A read-only companion to the native bot. No PII in browser storage, no payments. */
(function (root) {
  'use strict';
  function create({container, request, escapeHTML}) {
    let generation = 0, current = null;
    const e = value => escapeHTML(String(value ?? ''));
    const money = minor => new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB'}).format(Number(minor) / 100);
    const quoteStatus = {offered:'Ждёт твоего решения', accepted:'Подтверждено тобой', declined:'Отклонено', superseded:'Заменено новым предложением', expired:'Срок истёк · нужно новое согласование'};
    const eventNames = {offered:'Предложены условия', accepted:'Условия приняты', declined:'Условия отклонены', dispatched:'Передача / готовность выдачи', received:'Вручение', tracking_corrected:'Исправлен трек', problem:'Сообщение о проблеме'};
    const date = value => { const d = new Date(value); return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('ru-RU'); };
    function action(label, run) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'service-link'; b.textContent = label; b.addEventListener('click', run); container.append(b);
    }
    function delivery(p) {
      const q = p.quote;
      container.innerHTML = `<div class="service-kicker">ПОКУПКА №${e(String(p.purchase_id).padStart(4,'0'))}</div><h3>${e(p.state_label)}</h3>` +
        (q ? `<article class="service-card"><div class="service-tag">${e(quoteStatus[q.status] || q.status)}</div><h4>${e(q.carrier_label)}</h4><p>${e(q.destination)}</p><dl><div><dt>Доплата за доставку</dt><dd>${e(q.amount_label)}</dd></div><div><dt>Как рассчитываемся</dt><dd>${q.billing === 'carrier' ? 'Напрямую перевозчику. Не через счёт за товары.' : 'За счёт магазина / самовывоз без доплаты.'}</dd></div><div><dt>Оценка срока</dt><dd>${e(q.eta)}</dd></div><div><dt>Источник стоимости</dt><dd>${e(q.basis)}</dd></div><div><dt>Согласовать и передать до</dt><dd>${e(date(q.expires_at * 1000))}</dd></div><div><dt>Общий бюджет с доставкой</dt><dd>${e(money(p.budget_minor))}</dd></div></dl><p class="service-note">Предложение #${e(q.quote_id)}. Это не новый счёт: оплата товаров не изменена.</p></article>` :
          '<article class="service-card"><h4>Условия ещё не согласованы</h4><p>Менеджер проверит стоимость, адрес и срок. Неподтверждённого тарифа здесь не будет.</p></article>') +
        (p.tracking ? `<article class="service-card"><span class="service-kicker">ТРЕК, ВВЕДЁННЫЙ КОМАНДОЙ</span><p class="service-tracking">${e(p.tracking)}</p><small>Проверяй номер на официальном сайте перевозчика. Бот не получает события его API.</small></article>` : '') +
        (p.blocking_tickets.length ? `<p class="service-warning">Передача приостановлена обращением: ${p.blocking_tickets.map(n => '№' + e(n)).join(', ')}. Решение согласуется в поддержке.</p>` : '') +
        (p.payment_attention ? '<p class="service-warning">Есть финансовая сверка. Обращение и доставка не меняют её автоматически.</p>' : '') +
        (!p.operations_managed ? '<p class="service-note">Историческая покупка: история доставки в этой системе не восстановлена.</p>' : '') +
        (p.events.length ? '<h4 class="service-section-title">История · ручной учёт</h4><ol class="service-timeline">' + p.events.map(event => `<li><strong>${e(eventNames[event.kind] || 'Обновление')}</strong><span>${e(date(event.created_at))} · ${event.source === 'customer' ? 'покупатель' : 'команда'}</span>${event.kind === 'tracking_corrected' ? `<p>${e(event.data.tracking)} · ${e(event.data.reason)}</p>` : ''}</li>`).join('') + '</ol>' : '');
      for (const tid of p.blocking_tickets) action(`Открыть обращение №${tid}`, () => load('ticket', tid));
      action('Мои обращения →', () => load('tickets'));
    }
    function ticket(t, page) {
      container.innerHTML = `<div class="service-kicker">ОБРАЩЕНИЕ №${e(String(t.ticket_id).padStart(4,'0'))}</div><h3>${e(t.topic_label)}</h3><p class="service-tag">${e(t.status_label)}</p>` +
        (t.purchase_id ? `<p>Связано с покупкой №${e(String(t.purchase_id).padStart(4,'0'))}</p>` : '') +
        (t.blocks_dispatch ? '<p class="service-warning">Передача приостановлена до решения / отзыва обращения.</p>' : '') +
        t.messages.map(m => `<article class="service-message" data-source="${m.source === 'customer' ? 'customer' : 'staff'}"><header>${m.source === 'customer' ? 'Ты' : 'Команда'}<time>${e(date(m.created_at))}</time></header><p>${e(m.body)}</p></article>`).join('') +
        '<p class="service-note">Закрытое обращение не означает выполненный возврат денег. Ответить, отозвать или возобновить его можно в боте: /tickets.</p>';
      if (page) action('← Новые сообщения', () => load('ticket', t.ticket_id, page - 1));
      if (t.has_more) action('Ранние сообщения →', () => load('ticket', t.ticket_id, page + 1));
      if (t.purchase_id) action('Доставка по покупке →', () => load('delivery', t.purchase_id));
      action('← Мои обращения', () => load('tickets'));
    }
    async function load(kind = 'tickets', id = 0, page = 0) {
      current = {kind, id, page};
      const turn = ++generation;
      container.innerHTML = '<p class="service-note" role="status">Загружаем из бота…</p>';
      const query = kind === 'delivery' ? `?purchase_id=${encodeURIComponent(id)}` : kind === 'ticket' ? `?ticket_id=${encodeURIComponent(id)}&page=${page}` : `?offset=${page * 6}`;
      try {
        const data = await request('/api/service' + query);
        if (generation !== turn) return;
        if (kind === 'delivery') delivery(data.delivery);
        else if (kind === 'ticket') ticket(data.ticket, page);
        else {
          container.innerHTML = '<div class="service-kicker">ПОДДЕРЖКА / ТВОЯ ПЕРЕПИСКА</div><h3>На связи.</h3><p>Здесь те же обращения, что в боте. Новое — через /support, продолжить — через /tickets.</p>';
          if (!data.tickets.length) container.insertAdjacentHTML('beforeend', '<p class="service-note">На этой странице пока нет обращений. Если нужна помощь, начни диалог в боте.</p>');
          for (const t of data.tickets) action(`№${String(t.ticket_id).padStart(4,'0')} · ${t.topic_label} · ${t.status_label}`, () => load('ticket', t.ticket_id));
          if (page) action('← Новее', () => load('tickets', 0, page - 1));
          if (data.tickets.length === 6) action('Раньше →', () => load('tickets', 0, page + 1));
        }
      } catch (error) {
        if (generation !== turn) return;
        container.innerHTML = `<p class="service-warning" role="alert">${e(error.message || 'Не удалось загрузить. Открой раздел из бота.')}</p>`;
        action('Повторить загрузку', () => load(kind, id, page));
      }
    }
    function clear() { generation++; current = null; container.replaceChildren(); }
    return {load, clear, refresh: () => current ? load(current.kind, current.id, current.page) : load()};
  }
  root.ShopService = {create};
})(window);
