"""Private-chat delivery agreements and support desk on the shared store."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from commerce_bot import button, esc
from operations_store import CARRIERS, TOPICS, clean_text, delivery_minor, delivery_money

QUOTE_STATUS = {"offered": "нужно твоё решение", "accepted": "подтверждено покупателем", "declined": "отклонено", "superseded": "заменено новым предложением", "expired": "срок истёк — нужно новое согласование"}
EVENT_NAMES = {"offered": "Подготовлены условия", "accepted": "Условия приняты", "declined": "Условия отклонены", "dispatched": "Передача / готовность самовывоза", "received": "Вручение", "tracking_corrected": "Исправление трека", "problem": "Сообщение о проблеме"}
INPUT_LABELS = {"destination": "Адрес / пункт выдачи", "amount_minor": "Доплата покупателя за доставку", "eta": "Оценка срока", "basis": "Источник стоимости", "tracking": "Трек-номер", "reason": "Причина исправления", "body": "Текст обращения / ответа"}


def short(value, limit=460):
    """Bound escaped HTML, without cutting through an entity or losing stored text."""
    text = str(value)
    while len(esc(text)) > limit - 2:
        text = text[:-1]
    return esc(text) + ("…" if text != str(value) else "")


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m %H:%M UTC")


class OperationsBot:
    def __init__(self, bot):
        self.bot, self.db, self.ui = bot, bot.db, bot.commerce
        self.owners = bot.settings.admin_ids

    def action(self, uid, label, kind, **data):
        token = self.db.create_chat_action(uid, "ops." + kind, data)
        return button(label, "o:" + token.split(":", 1)[1])

    def screen(self, chat, uid, title, text, rows):
        self.ui.screen(chat, uid, title, text, rows)

    def consent(self, chat, uid, purchase_id=None):
        if purchase_id is not None: self.db._service_purchase(purchase_id, uid)
        self.db.set_state(uid, "ops_consent", {"purchase_id": purchase_id})
        rows = [[self.action(uid, "Согласен · перейти к обращению", "consent", purchase_id=purchase_id)], [button("Не сейчас", "c:home")]]
        if self.bot.settings.privacy_url:
            rows.insert(0, [{"text": "Политика обработки данных ↗", "url": self.bot.settings.privacy_url}])
        self.screen(chat, uid, "ПОДДЕРЖКА / СОГЛАСИЕ", "<b>Переписка — только для решения твоего вопроса.</b>\nСохраним обращение и ответы, связь с покупкой и необходимые сведения для обслуживания. Их увидят уполномоченные сотрудники поддержки и менеджеры. До согласия текст в обращение не записываем.\n\nЭто не подписка на рекламу. Не присылай пароли, коды входа или данные банковской карты.", rows)

    def support(self, chat, uid, purchase_id=None):
        if not self.db.has_consent(uid):
            self.consent(chat, uid, purchase_id)
            return
        if purchase_id is not None: self.db._service_purchase(purchase_id, uid)
        text = "<b>Давай решим вопрос.</b>\nОтвет останется в этой переписке, а не потеряется среди личных сообщений менеджеру."
        if purchase_id is not None: text += f"\n\nПо покупке <b>№{purchase_id:04d}</b>. Её состав и оплата уже связаны с обращением."
        text += "\n\nВыбери тему. Перед отправкой будет проверка текста. Возврат товара — запрос на согласование, а не автоматический возврат денег."
        rows = [[self.action(uid, label, "new", purchase_id=purchase_id, topic=topic)] for topic, label in TOPICS.items()]
        rows.append([button("Мои обращения", "o:tickets:all:0"), button("← Покупка" if purchase_id else "← Главная", f"c:purchase:{purchase_id}" if purchase_id else "c:home")])
        self.screen(chat, uid, "ПОДДЕРЖКА / НОВОЕ ОБРАЩЕНИЕ", text, rows)

    def delivery(self, chat, uid, pid, staff=False):
        self.db.expire_reservations()
        p = self.db.delivery_view(uid, pid, staff=staff, owner_ids=self.owners)
        q = p["quote"]
        text = f"<b>ПОКУПКА №{pid:04d} / ДОСТАВКА</b>\n{esc(p['state_label'].capitalize())}\n\n"
        if q:
            text += f"<b>Предложение #{q['quote_id']} · {esc(q['carrier_label'])}</b>\n{esc(QUOTE_STATUS[q['status']])}\n"
            if "destination" in q: text += f"{esc(q['destination'])}\n"
            text += f"\nДоплата покупателя: <b>{esc(q['amount_label'])}</b>\n"
            text += "Расчёт: напрямую перевозчику, не через счёт за товары.\n" if q["billing"] == "carrier" else "Расчёт: магазин покрывает доставку / самовывоз без доплаты.\n"
            text += f"Оценка срока: {esc(q['eta'])}\n"
            if "basis" in q: text += f"Источник стоимости: {esc(q['basis'])}\n"
            text += f"Согласовать и передать до {stamp(q['expires_at'])}.\n"
            text += f"\nТовары: {delivery_money(p['goods_amount_minor'])}\nОбщий бюджет с этой доставкой: <b>{delivery_money(p['budget_minor'])}</b>. Это не сумма нового счёта.\n"
        else:
            text += "<b>Условия ещё не согласованы.</b>\nМенеджер проверит стоимость у перевозчика и предложит адрес/ПВЗ, способ расчёта и оценку срока. Тариф не придумываем."
        if not p["operations_managed"]: text += "\n\nИсторическая покупка: достоверной истории доставки в этой системе нет."
        if p["tracking"]: text += f"\n\nТрек: <code>{esc(p['tracking'])}</code>\nНомер введён командой. Проверяй его на официальном сайте перевозчика."
        if p["blocking_tickets"]: text += "\n\n<b>Передача приостановлена.</b> Открыто обращение по условиям/оплате/возврату: " + ", ".join(f"№{t:04d}" for t in p["blocking_tickets"])
        if p["payment_attention"]: text += "\n\n<b>Есть финансовая сверка.</b> Переписка не снимает её автоматически."
        if p["issue"]: text += "\n\nПоследняя отметка о проблеме: " + short(p["issue"], 300)
        if p["events"]:
            text += "\n\n<b>Последние события</b>"
            for e in reversed(p["events"][:3]):
                text += f"\n{esc(e['created_at'][5:16].replace('T', ' '))} UTC · {EVENT_NAMES.get(e['kind'], 'Обновление')} · {'покупатель' if e['source'] == 'customer' else 'команда'}"
        text += "\n\nРучной учёт: бот не получает события API перевозчика и не списывает деньги за доставку."
        rows = []
        back = f"o:workdelivery:{pid}" if staff else f"o:delivery:{pid}"
        if staff:
            if self.ui.allowed(uid, "shipping.quote") and p["can_offer"]:
                rows.append([self.action(uid, "Предложить новые условия…", "quote_start", purchase_id=pid, version=p["version"])])
            if self.ui.allowed(uid, "shipping.dispatch"):
                if p["can_dispatch"]:
                    rows.append([self.action(uid, "Готово к самовывозу…" if q["carrier"] == "pickup" else "Передано перевозчику…", "dispatch_start", purchase_id=pid, version=p["version"])])
                if p["can_receive"]:
                    rows.append([self.action(uid, "Зафиксировать вручение…", "receive_check", purchase_id=pid, version=p["version"], staff=True)])
                if p["state"] != "not_sent": rows.append([self.action(uid, "Сообщить о проблеме…", "issue_start", purchase_id=pid, version=p["version"])])
            if p["state"] == "in_transit" and self.ui.allowed(uid, "shipping.quote"):
                rows.append([self.action(uid, "Исправить трек с причиной…", "tracking_start", purchase_id=pid, version=p["version"])])
            rows.append([button("← Рабочая покупка", f"c:work:{pid}"), button("Очередь доставки", "o:shipqueue:agreement:0")])
        else:
            if p["can_answer"]:
                params = dict(purchase_id=pid, quote_id=q["quote_id"], version=p["version"])
                rows.append([self.action(uid, "Условия подходят · принять", "answer", accept=True, **params), self.action(uid, "Не подходят", "answer", accept=False, **params)])
            if p["can_receive"]:
                rows.append([self.action(uid, "Вещи у меня · подтвердить…", "receive_check", purchase_id=pid, version=p["version"], staff=False)])
            rows.append([button("Вопрос / изменить адрес", f"o:support:{pid}"), button("← Покупка", f"c:purchase:{pid}")])
        if q: rows.append([button("История условий", ("o:workhistory:" if staff else "o:history:") + str(pid))])
        rows.append([button("Обновить доставку", back)])
        self.screen(chat, uid, "КОМАНДА / ЛОГИСТИКА" if staff else "ПОКУПКА / ДОСТАВКА", text, rows)

    def history(self, chat, uid, pid, staff=False, page=0):
        self.db._service_purchase(pid, uid, "shipping.read" if staff else None, self.owners)
        role = self.ui.role(uid) if staff else "customer"
        quotes = list(self.db.connection().execute("SELECT * FROM delivery_quotes WHERE purchase_id=? ORDER BY quote_id DESC LIMIT 2 OFFSET ?", (pid, max(0, min(page, 10000)))))
        text = f"<b>Покупка №{pid:04d} / версии условий</b>\nНовые условия не исправляют старые согласия.\n"
        for q in quotes[:1]:
            text += f"\n<b>#{q['quote_id']} · {CARRIERS[q['carrier']]}</b> · {delivery_money(q['amount_minor'])}\n{esc(QUOTE_STATUS[q['status']])}"
            text += "\n" + ("Расчёт напрямую перевозчику" if q["billing"] == "carrier" else "Магазин покрывает / без доплаты")
            text += "\nОценка срока: " + esc(q["eta"]) + "\nДействовало до " + stamp(q["expires_at"])
            if role in {"customer", "manager", "owner"}:
                text += "\n" + esc(q["destination"]) + "\nИсточник: " + esc(q["basis"])
            text += "\n" + ("Покупатель подтвердил " + esc(q["accepted_at"][:16].replace("T", " ")) + " UTC" if q["accepted_at"] else "Принятие не зафиксировано") + "\n"
        prefix = "o:workhistory" if staff else "o:history"
        rows = []
        if page: rows.append([button("← Новее", f"{prefix}:{pid}:{page - 1}")])
        if len(quotes) == 2: rows.append([button("Раньше →", f"{prefix}:{pid}:{page + 1}")])
        rows.append([button("← Доставка", f"o:{'workdelivery' if staff else 'delivery'}:{pid}")])
        self.screen(chat, uid, "ДОСТАВКА / ИСТОРИЯ", text, rows)

    def shipqueue(self, chat, uid, bucket="agreement", page=0):
        self.db.expire_reservations()
        plans = self.db.delivery_queue(uid, bucket, max(0, page) * 6, owner_ids=self.owners)
        rows = [[button(f"№{p['purchase_id']:04d} · {p['state_label']}" + (" · пауза" if p["blocking_tickets"] else ""), f"o:workdelivery:{p['purchase_id']}")] for p in plans]
        if page: rows.append([button("← Новее", f"o:shipqueue:{bucket}:{page - 1}")])
        if len(plans) == 6: rows.append([button("Ещё →", f"o:shipqueue:{bucket}:{page + 1}")])
        rows += [[button("Согласование", "o:shipqueue:agreement:0"), button("К передаче", "o:shipqueue:ready:0")],
                 [button("В пути / выдача", "o:shipqueue:sent:0"), button("Вручено", "o:shipqueue:done:0")], [button("← Рабочее место", "c:team")]]
        self.screen(chat, uid, "КОМАНДА / ДОСТАВКА", "<b>Согласовать → подготовить → передать → вручить.</b>\n" + ("Выбери покупку. Финансовая сверка и открытые блокирующие обращения проверяются при передаче." if plans else "В выбранной очереди пока нет покупок."), rows)

    def tickets(self, chat, uid, staff=False, bucket="all", page=0):
        rows_data = self.db.tickets_list(uid, staff=staff, bucket=bucket, offset=max(0, page) * 6, owner_ids=self.owners)
        prefix = "o:desk" if staff else "o:tickets"
        rows = [[button(f"№{t['ticket_id']:04d} · {t['topic_label']} · {t['status_label']}", f"o:{'workticket' if staff else 'ticket'}:{t['ticket_id']}")] for t in rows_data]
        if page: rows.append([button("← Новее", f"{prefix}:{bucket}:{page - 1}")])
        if len(rows_data) == 6: rows.append([button("Ещё →", f"{prefix}:{bucket}:{page + 1}")])
        if staff:
            rows += [[button("Открытые", "o:desk:open:0"), button("Мои в работе", "o:desk:mine:0"), button("Закрытые", "o:desk:resolved:0")], [button("← Рабочее место", "c:team")]]
        else:
            rows += [[button("Новое обращение", "o:support"), button("← Главная", "c:home")]]
        self.screen(chat, uid, "КОМАНДА / ПОДДЕРЖКА" if staff else "ПОДДЕРЖКА / МОИ ОБРАЩЕНИЯ", "<b>Ни один вопрос не должен потеряться.</b>\n" + ("Выбери переписку. Внутренние заметки видит только команда поддержки." if staff else "Выбери переписку или задай вопрос по своей покупке.") + ("\n\nВ этой очереди пока пусто." if not rows_data else ""), rows)

    def ticket(self, chat, uid, tid, staff=False, page=0):
        t = self.db.ticket_view(uid, tid, staff=staff, owner_ids=self.owners, page=page)
        text = f"<b>ОБРАЩЕНИЕ №{tid:04d} / {esc(t['topic_label'])}</b>\n{esc(t['status_label'].capitalize())}"
        if t["purchase_id"]: text += f"\nПокупка №{t['purchase_id']:04d}"
        if staff: text += "\nОтветственный: " + str(t["assigned_to"] or "общая очередь")
        if t["blocks_dispatch"]: text += "\n<b>Передача приостановлена.</b> Закрыть такую паузу может менеджер/владелец или сам покупатель, отозвав запрос."
        if t["requires_manager"] and not t["blocks_dispatch"]: text += "\nРешение согласует менеджер. Уже состоявшаяся передача не отменяется обращением."
        rows = []
        for m in t["messages"]:
            label = "Внутренняя заметка" if m.get("internal") else "Покупатель" if m["source"] == "customer" else "Команда"
            text += f"\n\n<b>{label} · {esc(m['created_at'][5:16].replace('T', ' '))} UTC</b>\n{short(m['body'])}"
            rows.append([button(f"Читать целиком · сообщение {m['message_id']}", f"o:message:{tid}:{m['message_id']}:{int(staff)}:0")])
        prefix = "o:workticket" if staff else "o:ticket"
        if page: rows.append([button("← Новые сообщения", f"{prefix}:{tid}:{page - 1}")])
        if t["has_more"]: rows.append([button("Ранние сообщения →", f"{prefix}:{tid}:{page + 1}")])
        params = dict(ticket_id=tid, version=t["version"], staff=staff)
        if staff:
            if self.ui.allowed(uid, "support.claim") and not t["assigned_to"] and t["status"] != "resolved":
                rows.append([self.action(uid, "Взять обращение в работу", "claim", **params)])
            can_write = self.ui.allowed(uid, "support.write") and (t["assigned_to"] == uid or self.ui.role(uid) == "owner")
            if can_write:
                if t["status"] != "resolved": rows.append([self.action(uid, "Ответить покупателю…", "reply_start", internal=False, resolve=False, **params)])
                rows.append([self.action(uid, "Внутренняя заметка…", "reply_start", internal=True, resolve=False, **params)])
                if t["status"] != "resolved" and (not t["requires_manager"] or self.ui.allowed(uid, "support.resolve_sensitive")):
                    rows.append([self.action(uid, "Ответить и закрыть…", "reply_start", internal=False, resolve=True, **params)])
            if t["assigned_to"] and (t["assigned_to"] == uid or self.ui.role(uid) == "owner") and t["status"] != "resolved":
                rows.append([self.action(uid, "Вернуть в общую очередь…", "release_check", **params)])
        else:
            rows.append([self.action(uid, "Возобновить обращение…" if t["status"] == "resolved" else "Дополнить / ответить…", "reply_start", internal=False, resolve=False, **params)])
            if t["status"] != "resolved": rows.append([self.action(uid, "Вопрос решён / отозвать…", "reply_start", internal=False, resolve=True, **params)])
        if t["purchase_id"]: rows.append([button("Связанная покупка", f"c:{'work' if staff else 'purchase'}:{t['purchase_id']}")])
        rows.append([button("Обновить", f"{prefix}:{tid}"), button("← Очередь" if staff else "← Мои обращения", "o:desk:open:0" if staff else "o:tickets:all:0")])
        text += "\n\nЗакрытие обращения ≠ возврат денег. Финансовый статус — в покупке."
        self.screen(chat, uid, "КОМАНДА / ПЕРЕПИСКА" if staff else "ПОДДЕРЖКА / ПЕРЕПИСКА", text, rows)

    def message_view(self, chat, uid, tid, mid, staff, page):
        self.db._ticket(uid, tid, staff, self.owners)
        m = self.db.connection().execute("SELECT * FROM support_messages WHERE message_id=? AND ticket_id=? AND (?=1 OR internal=0)", (mid, tid, int(staff))).fetchone()
        if not m: raise ValueError("Сообщение не найдено.")
        pages = math.ceil(len(m["body"]) / 450)
        page = max(0, min(page, pages - 1))
        rows = []
        if page: rows.append([button("← Начало", f"o:message:{tid}:{mid}:{int(staff)}:{page - 1}")])
        if page + 1 < pages: rows.append([button("Читать дальше →", f"o:message:{tid}:{mid}:{int(staff)}:{page + 1}")])
        rows.append([button("← Переписка", f"o:{'workticket' if staff else 'ticket'}:{tid}")])
        self.screen(chat, uid, "ПЕРЕПИСКА / СООБЩЕНИЕ", f"<b>{'Внутренняя заметка · ' if m['internal'] else ''}№{tid:04d} · {page + 1}/{pages}</b>\n\n" + esc(m["body"][page * 450:(page + 1) * 450]), rows)

    def begin(self, chat, uid, kind, payload, field):
        token = self.db.save_service_draft(uid, kind, payload, owner_ids=self.owners)
        self.db.set_state(uid, "ops_input", {"token": token, "field": field})
        self.prompt(chat, uid)

    def prompt(self, chat, uid):
        state = self.db.get_state(uid)
        kind, data = self.db.service_draft(uid, state[1]["token"], owner_ids=self.owners)
        field = state[1]["field"]
        hints = {"destination": "Точный адрес или ПВЗ. Для самовывоза — адрес точки. Не меняет исходный снимок покупки.",
                 "amount_minor": "Только проверенная стоимость к оплате напрямую перевозчику. Рубли, например 350 или 350,50. Не оценка «на глаз» и не доплата на счёт магазина.",
                 "eta": "Например: 2–4 дня после передачи. Это оценка, не гарантированная дата.",
                 "basis": "На чём основана стоимость: кабинет перевозчика, расчёт оператора или решение магазина покрыть доставку. Никаких секретов и ссылок с токенами.",
                 "tracking": "Номер, не ссылка: 4–48 латинских букв, цифр или дефисов. Проверь по документам передачи.",
                 "reason": "Почему исправляем ранее выданный трек? Причина будет видна покупателю.",
                 "body": "Одно сообщение до 1200 символов. Без паролей, данных карты и кодов доступа. После ввода будет проверка, не немедленная отправка."}
        if kind == "shipment_issue": hints["body"] = "Что произошло с отправлением? До 400 символов. После подтверждения покупатель увидит сообщение, а команда — отдельное обращение. Не отмечай возврат денег выполненным без финансового подтверждения."
        if kind == "ticket_reply" and data.get("internal"): hints["body"] = "Внутренняя заметка, до 1200 символов. Покупатель её не увидит и уведомление не получит. Не записывай пароли или данные карты."
        if data.get("resolve"): hints["body"] += "\n\nУкажи итог / причину отзыва. Подтверждение закроет обращение и снимет его паузу отправки, но не вернёт деньги."
        self.screen(chat, uid, "ДОСТАВКА / ВВОД" if kind in {"quote", "dispatch", "tracking", "shipment_issue"} else "ПОДДЕРЖКА / ТЕКСТ", f"<b>{INPUT_LABELS[field]}</b>\n{hints[field]}\n\nЧерновик доступен 15 минут. На паузе новые сообщения к нему не добавляются.", [[button("Пауза · главная", "c:home")]])

    def preview(self, chat, uid, token):
        kind, data = self.db.service_draft(uid, token, owner_ids=self.owners)
        if kind == "quote":
            t = data["terms"]
            body = f"<b>Новые условия / покупка №{data['purchase_id']:04d}</b>\n{CARRIERS[t['carrier']]}\n{esc(t['destination'])}\n\nДоплата: <b>{delivery_money(t['amount_minor'])}</b>\n" + ("Напрямую перевозчику" if t["billing"] == "carrier" else "Магазин покрывает доставку / бесплатно")
            body += f"\nОценка срока: {esc(t['eta'])}\nИсточник: {esc(t['basis'])}\n\nПредложение действительно 24 часа после публикации. Покупатель должен отдельно принять его; прежнее согласие не переносится. Сумма оплаты товаров не изменится.\n\nТы проверил стоимость и адрес?"
            label = "Проверено · отправить условия"
        elif kind == "dispatch":
            body = f"<b>Покупка №{data['purchase_id']:04d}</b>\n" + ("Вещи физически готовы к выдаче по согласованному адресу самовывоза?" if not data.get("tracking") else f"Вещи действительно переданы перевозчику?\nТрек: <code>{esc(data['tracking'])}</code>")
            body += "\n\nФиксируем ручное событие команды. После передачи адрес и стоимость нельзя тихо менять. Проверим оплату, сборку, согласие и блокирующие обращения."
            label = "Факт проверен · зафиксировать"
        elif kind == "tracking":
            body = f"<b>Исправить трек</b>\n{esc(data['tracking'])}\nПричина: {esc(data['reason'])}\n\nПокупатель получит уведомление. Прежний номер останется в истории."
            label = "Исправить и уведомить"
        else:
            title = "Внутренняя заметка · только команде" if data.get("internal") else "Ответ и закрытие обращения" if data.get("resolve") else "Сообщение о проблеме отправления" if kind == "shipment_issue" else "Сообщение в поддержку" if kind == "ticket_new" else "Ответ в обращение"
            body = f"<b>{title}</b>\n\n{short(data['body'], 2500)}"
            if len(esc(data["body"])) > 2498: body += "\n\nПоказано начало. Будет сохранён весь введённый текст."
            body += "\n\n" + ("Покупатель это не увидит." if data.get("internal") else "Отправить этот текст? Контактные данные видит только уполномоченная команда, не группа уведомлений.")
            if data.get("resolve"): body += "\nОбращение будет закрыто, его пауза отправки снята. Это не подтверждение возврата денег."
            label = "Сохранить заметку" if data.get("internal") else "Подтвердить закрытие" if data.get("resolve") else "Отправить сообщение"
        rows = [[self.action(uid, label, "send", token=token)], [self.action(uid, "Отменить этот черновик", "discard", token=token), button("Пауза · главная", "c:home")]]
        if kind in {"ticket_new", "ticket_reply", "shipment_issue"}:
            rows.insert(1, [self.action(uid, "Проверить полный текст", "draft_read", token=token, page=0)])
        self.screen(chat, uid, "ПРОВЕРКА / БЕЗ СЛУЧАЙНЫХ ДЕЙСТВИЙ", body, rows)

    def send_draft(self, chat, uid, token):
        prior = self.db.service_result(uid, token)
        if prior:
            kind, result = prior["kind"], prior["result_id"]
            if kind == "discarded":
                raise ValueError("Черновик отменён. Старое подтверждение больше не отправит его.")
            # Re-check ACL even when a processed draft has already been pruned.
            if kind == "shipment_issue":
                row = self.db.connection().execute("SELECT purchase_id FROM support_tickets WHERE ticket_id=?", (result,)).fetchone()
                self.delivery(chat, uid, row[0], staff=True)
            elif kind in {"ticket_new", "ticket_reply"}:
                row = self.db.connection().execute("SELECT user_id FROM support_tickets WHERE ticket_id=?", (result,)).fetchone()
                self.ticket(chat, uid, result, staff=bool(row and row[0] != uid))
            else:
                pid = result
                if kind == "quote": pid = self.db.connection().execute("SELECT purchase_id FROM delivery_quotes WHERE quote_id=?", (result,)).fetchone()[0]
                self.delivery(chat, uid, pid, staff=True)
            return
        with self.db.commerce_transaction():
            kind, data = self.db.service_draft(uid, token, owner_ids=self.owners)
            if kind == "quote":
                self.db.propose_delivery(uid, data["purchase_id"], data["terms"], data["version"], token, owner_ids=self.owners)
            elif kind == "dispatch": self.db.dispatch_delivery(uid, data["purchase_id"], data["version"], data.get("tracking", ""), token, owner_ids=self.owners)
            elif kind == "tracking": self.db.correct_tracking(uid, data["purchase_id"], data["version"], data["tracking"], data["reason"], token, owner_ids=self.owners)
            elif kind == "shipment_issue":
                tid = self.db.report_delivery_issue(uid, data["purchase_id"], data["version"], data["body"], token, owner_ids=self.owners)
            elif kind == "ticket_new": tid = self.db.create_ticket(uid, data["topic"], data["body"], token, data.get("purchase_id"))
            elif kind == "ticket_reply":
                tid = self.db.reply_ticket(uid, data["ticket_id"], data["body"], data["version"], token, staff=data["staff"], internal=data["internal"], resolve=data["resolve"], owner_ids=self.owners)
            self.db.clear_service_draft_state(uid, token)
        if kind in {"ticket_new", "ticket_reply"}: self.ticket(chat, uid, tid, staff=bool(data.get("staff")))
        else: self.delivery(chat, uid, data["purchase_id"], staff=True)

    def dispatch_action(self, chat, uid, token):
        kind, d = self.db.chat_action(uid, token)
        if not kind.startswith("ops."): raise ValueError("Неизвестное действие поддержки.")
        kind = kind[4:]
        if kind == "consent":
            with self.db.commerce_transaction():
                prior, fp = self.db._service_request(uid, token, "service_consent", [True])
                if prior is None:
                    self.db.set_consent(uid, service_only=True)
                    state = self.db.get_state(uid)
                    if state and state[0] == "ops_consent": self.db.clear_state(uid)
                    self.db._service_done(uid, token, "service_consent", fp, 0)
            if prior is not None and self.db.get_state(uid): self.ui.home(chat, uid)
            else: self.support(chat, uid, d.get("purchase_id"))
        elif kind == "new": self.begin(chat, uid, "ticket_new", d, "body")
        elif kind == "answer":
            self.db.answer_delivery(uid, d["purchase_id"], d["quote_id"], d["version"], d["accept"], token)
            self.delivery(chat, uid, d["purchase_id"])
        elif kind == "quote_start":
            self.db.require_staff(uid, "shipping.quote", self.owners)
            self.db._service_purchase(d["purchase_id"], uid, "shipping.quote", self.owners)
            rows = [[self.action(uid, name, "quote_carrier", **d, carrier=c)] for c, name in CARRIERS.items()]
            rows.append([button("← Доставка", f"o:workdelivery:{d['purchase_id']}")])
            self.screen(chat, uid, "ДОСТАВКА / СПОСОБ", "Выбери реальный способ передачи. Затем проверим адрес, стоимость и срок. Никаких автоматических тарифов.", rows)
        elif kind == "quote_carrier":
            self.db.require_staff(uid, "shipping.quote", self.owners)
            if d["carrier"] == "pickup":
                self.begin(chat, uid, "quote", {"purchase_id": d["purchase_id"], "version": d["version"], "terms": {"carrier": "pickup", "billing": "shop", "amount_minor": 0}}, "destination")
            else:
                self.screen(chat, uid, "ДОСТАВКА / РАСЧЁТ", "<b>Кто оплачивает доставку?</b>\nВ этом выпуске нет отдельного счёта на доставку магазину. Только прямой расчёт с перевозчиком или доставка за счёт магазина.",
                    [[self.action(uid, "Покупатель платит перевозчику", "quote_billing", **d, billing="carrier")], [self.action(uid, "Магазин покрывает · доплата 0 ₽", "quote_billing", **d, billing="shop")]])
        elif kind == "quote_billing":
            self.begin(chat, uid, "quote", {"purchase_id": d["purchase_id"], "version": d["version"], "terms": {"carrier": d["carrier"], "billing": d["billing"], **({"amount_minor": 0} if d["billing"] == "shop" else {})}}, "destination")
        elif kind in {"dispatch_start", "tracking_start", "issue_start"}:
            p = self.db.delivery_view(uid, d["purchase_id"], staff=True, owner_ids=self.owners)
            dk = {"dispatch_start": "dispatch", "tracking_start": "tracking", "issue_start": "shipment_issue"}[kind]
            if dk == "dispatch" and not p["can_dispatch"]: raise ValueError("Передача пока запрещена. Проверь оплату, сборку, согласие и обращения.")
            if dk == "dispatch" and p["quote"]["carrier"] == "pickup":
                token = self.db.save_service_draft(uid, dk, {**d, "tracking": ""}, owner_ids=self.owners)
                self.db.set_state(uid, "ops_preview", {"token": token})
                self.preview(chat, uid, token)
            else: self.begin(chat, uid, dk, d, "body" if dk == "shipment_issue" else "tracking")
        elif kind == "receive_check":
            self.db.delivery_view(uid, d["purchase_id"], staff=d["staff"], owner_ids=self.owners)
            self.screen(chat, uid, "ДОСТАВКА / ВРУЧЕНИЕ", "<b>Вещи действительно у покупателя?</b>\n" + ("Это будет ручная отметка команды, не сообщение перевозчика." if d["staff"] else "Подтверждай только после фактического получения, не после появления трека.") + "\n\nЭто не лишает возможности обратиться по обмену, возврату или качеству.", [[self.action(uid, "Да, вручение состоялось", "receive", **d)], [button("Нет · назад", f"o:{'workdelivery' if d['staff'] else 'delivery'}:{d['purchase_id']}")]])
        elif kind == "receive":
            self.db.mark_received(uid, d["purchase_id"], d["version"], token, staff=d["staff"], owner_ids=self.owners)
            self.delivery(chat, uid, d["purchase_id"], d["staff"])
        elif kind == "claim":
            self.db.claim_ticket(uid, d["ticket_id"], token, owner_ids=self.owners)
            self.ticket(chat, uid, d["ticket_id"], staff=True)
        elif kind == "release_check":
            self.db._ticket(uid, d["ticket_id"], True, self.owners, "support.claim")
            self.screen(chat, uid, "ПОДДЕРЖКА / ПЕРЕДАЧА", "Вернуть обращение в общую очередь? Переписка и пауза отправки сохранятся.", [[self.action(uid, "Да, вернуть в очередь", "release", **d)], [button("Оставить", f"o:workticket:{d['ticket_id']}")]])
        elif kind == "release":
            self.db.claim_ticket(uid, d["ticket_id"], token, release=True, version=d["version"], owner_ids=self.owners)
            self.ticket(chat, uid, d["ticket_id"], staff=True)
        elif kind == "reply_start": self.begin(chat, uid, "ticket_reply", d, "body")
        elif kind == "send": self.send_draft(chat, uid, d["token"])
        elif kind == "discard":
            if self.db.discard_service_draft(uid, d["token"]): self.ui.home(chat, uid)
            else: self.send_draft(chat, uid, d["token"])
        elif kind == "draft_read":
            _, data = self.db.service_draft(uid, d["token"], owner_ids=self.owners)
            page = max(0, min(d["page"], math.ceil(len(data["body"]) / 450) - 1))
            rows = []
            if page: rows.append([self.action(uid, "← Начало", "draft_read", token=d["token"], page=page - 1)])
            if (page + 1) * 450 < len(data["body"]): rows.append([self.action(uid, "Дальше →", "draft_read", token=d["token"], page=page + 1)])
            rows.append([self.action(uid, "← К подтверждению", "draft_preview", token=d["token"])])
            self.screen(chat, uid, "ЧЕРНОВИК / ПОЛНЫЙ ТЕКСТ", esc(data["body"][page * 450:(page + 1) * 450]), rows)
        elif kind == "draft_preview": self.preview(chat, uid, d["token"])
        else: raise ValueError("Действие устарело.")

    def resume(self, chat, uid):
        state = self.db.get_state(uid)
        if not state or not state[0].startswith("ops_"): return False
        self.db.set_state(uid, state[0], {k: v for k, v in state[1].items() if k != "paused"})
        if state[0] == "ops_consent": self.consent(chat, uid, state[1].get("purchase_id"))
        elif state[0] == "ops_input": self.prompt(chat, uid)
        else: self.preview(chat, uid, state[1]["token"])
        return True

    def handle_callback(self, chat, user, data):
        if not data.startswith("o:"): return False
        uid = int(user["id"])
        try:
            p = data.split(":")
            if p[1] != "a": self.ui.pause(uid)
            if p[1] == "a": self.dispatch_action(chat, uid, p[2])
            elif p[1] == "support": self.support(chat, uid, int(p[2]) if len(p) > 2 else None)
            elif p[1] in {"delivery", "workdelivery"}: self.delivery(chat, uid, int(p[2]), staff=p[1] == "workdelivery")
            elif p[1] in {"history", "workhistory"}: self.history(chat, uid, int(p[2]), p[1] == "workhistory", int(p[3]) if len(p) > 3 else 0)
            elif p[1] in {"ticket", "workticket"}: self.ticket(chat, uid, int(p[2]), staff=p[1] == "workticket", page=int(p[3]) if len(p) > 3 else 0)
            elif p[1] == "shipqueue": self.shipqueue(chat, uid, p[2], int(p[3]))
            elif p[1] in {"tickets", "desk"}: self.tickets(chat, uid, p[1] == "desk", p[2], int(p[3]))
            elif p[1] == "message": self.message_view(chat, uid, int(p[2]), int(p[3]), p[4] == "1", int(p[5]))
            else: raise ValueError("Кнопка устарела. Открой раздел заново.")
        except (ValueError, PermissionError, IndexError, KeyError, TypeError) as exc:
            self.error(chat, uid, exc)
        return True

    def error(self, chat, uid, exc):
        rows = [[button("Мои обращения", "o:tickets:all:0"), button("Главная", "c:home")]]
        if self.ui.role(uid): rows.insert(0, [button("← Рабочее место", "c:team")])
        if self.db.get_state(uid): rows.insert(0, [button("Продолжить черновик", "c:resume")])
        self.screen(chat, uid, "НУЖНО ВНИМАНИЕ", esc(str(exc)), rows)

    def handle_message(self, chat, user, message):
        uid = int(user["id"])
        text = str(message.get("text", "")).strip()
        command = text.split(" ", 1)[0].split("@", 1)[0].lower()
        routes = {"/support": "o:support", "/tickets": "o:tickets:all:0", "/desk": "o:desk:open:0", "/shipping": "o:shipqueue:agreement:0"}
        if command in routes:
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
            return self.handle_callback(chat, user, routes[command])
        state = self.db.get_state(uid)
        if state and state[0].startswith("ops_") and (command == "/cancel" or text.lower() in {"отмена", "cancel"}):
            token = state[1].get("token")
            if token:
                try:
                    if not self.db.discard_service_draft(uid, token):
                        self.send_draft(chat, uid, token)
                        return True
                except ValueError:
                    self.db.clear_state(uid)
            else: self.db.clear_state(uid)
            self.ui.home(chat, uid, new=True)
            return True
        if not state or not state[0].startswith("ops_") or command.startswith("/"): return False
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
        try:
            if state[1].get("paused"):
                self.ui.home(chat, uid)
                return True
            if state[0] != "ops_input":
                self.resume(chat, uid)
                return True
            if type(message.get("message_id")) is not int or message["message_id"] < 1:
                raise ValueError("У сообщения нет идентификатора Telegram. Пришли текст новым сообщением.")
            operation = f"input:{chat}:{message['message_id']}"
            with self.db.commerce_transaction():
                previous, fp = self.db._service_request(uid, operation, "input", [text])
                if previous is None:
                    kind, data = self.db.service_draft(uid, state[1]["token"], owner_ids=self.owners)
                    field = state[1]["field"]
                    if field == "amount_minor": value = delivery_minor(text)
                    elif field == "tracking": value = self.db.tracking_code(text)
                    else: value = clean_text(text, {"destination": 240, "eta": 100, "basis": 160, "reason": 240}.get(field, 400 if kind == "shipment_issue" else 1200), minimum=3 if field != "body" else 1)
                    if kind == "quote":
                        data["terms"][field] = value
                        next_field = {"destination": "eta" if data["terms"]["billing"] == "shop" else "amount_minor", "amount_minor": "eta", "eta": "basis", "basis": None}[field]
                    else:
                        data[field] = value
                        next_field = "reason" if kind == "tracking" and field == "tracking" else None
                    token = self.db.save_service_draft(uid, kind, data, owner_ids=self.owners)
                    self.db.set_state(uid, "ops_input" if next_field else "ops_preview", {"token": token, **({"field": next_field} if next_field else {})})
                    self.db._service_done(uid, operation, "input", fp, 0)
            self.resume(chat, uid)
        except (ValueError, PermissionError, KeyError, TypeError) as exc:
            self.error(chat, uid, exc)
        return True
