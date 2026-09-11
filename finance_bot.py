"""Native private-chat financial desk; never a settlement/refund API."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from commerce_bot import button, esc
from finance_store import CASE_KINDS, FINANCE_STATES, FINANCE_STEPS, METHOD_NAMES, REASON_NAMES, STATUS_NAMES, finance_money
from operations_store import clean_text

QUEUES = {"attention": "Требуют внимания", "mine": "Мои задачи", "unassigned": "Без ответственного", "overdue": "Срок наступил", "waiting": "Внешняя сверка", "receipt": "Проблемные поступления", "attempt": "Зависшие счета", "inbox": "Ошибки обработки", "closed": "Закрытые задачи", "all": "Все задачи"}
EVENTS = {"observed": "Исключение обнаружено", "evidence_changed": "Источник изменился", "claimed": "Назначен ответственный", "released": "Вернули в очередь", "role_revoked": "Права ответственного отозваны", "scheduled": "Назначен срок контроля", "deadline_cleared": "Срок явно снят", "step": "Этап разбора", "note": "Внутренняя заметка"}
FIELDS = {"provider_id": "ID у провайдера", "recorded_payment": "Записанная связь с оплатой", "claimed_ref": "Ссылка во входящем событии", "payer_id": "Плательщик Stars", "invoice_payment": "Оплата сохранённого счёта", "invoice_reference": "Ссылка сохранённого счёта", "attempt_id": "Попытка создания", "external_ref": "Отправленная ссылка", "terminal": "Попытка завершена и счёт сохранён", "issued_invoice": "Счёт с совпадающей привязкой (reference может повторяться)", "payment_id": "ID оплаты", "covered_by_receipt": "Есть отдельная задача поступления", "update_id": "Update Telegram", "attempts": "Попыток обработки", "next_attempt_at": "Следующая попытка (UTC timestamp)", "last_error": "Класс ошибки", "processed_at": "Обработан"}


def when(value):
    if value is None: return "не назначен"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return str(value)[:19].replace("T", " ") + " UTC"


class FinanceBot:
    def __init__(self, bot):
        self.bot, self.db, self.ui = bot, bot.db, bot.commerce
        self.owners = bot.settings.admin_ids

    def action(self, uid, label, kind, **data):
        token = self.db.create_chat_action(uid, "finance." + kind, data)
        return button(label, "f:" + token.split(":", 1)[1])

    def screen(self, chat, uid, title, text, rows):
        self.ui.screen(chat, uid, title, text, rows)

    def home(self, chat, uid):
        self.ui.pause(uid)
        summary = self.db.finance_summary(uid, owner_ids=self.owners)
        c = summary["cases"]
        text = ("<b>Деньги — по событиям. Разбор — по задачам.</b>\n"
                "Здесь видны поступления, расхождения и незавершённая выдача счетов. Неизвестные поступления не теряются, даже когда покупка не найдена.\n\n"
                f"Активных исключений: <b>{c['active']}</b>\nБез ответственного: <b>{c['unassigned']}</b> · срок наступил: <b>{c['overdue']}</b>\n\n"
                "<b>Рабочий этап не подтверждает деньги.</b>\nЗаметка, ожидание и закрытие задачи не снимают финансовую блокировку. Исполнение возвратов не подключено. Сроки контроля — внутренние, не обещание покупателю.\n\n"
                "Точный поиск без контактов: <code>/finance receipt 1</code>, <code>/finance case 1</code>, <code>/finance purchase 1</code>.")
        rows = [[button("Мои задачи", "f:queue:mine:0"), button("Без ответственного", "f:queue:unassigned:0")],
                [button("Проблемные поступления", "f:queue:receipt:0")],
                [button("Зависшие счета", "f:queue:attempt:0"), button("Ошибки inbox", "f:queue:inbox:0")],
                [button("Срок наступил", "f:queue:overdue:0"), button("Внешняя сверка", "f:queue:waiting:0")],
                [button("Журнал поступлений", "f:receipts:0"), button("Сводка по валютам", "f:summary:0")],
                [button("Все задачи", "f:queue:all:0"), button("Закрытые", "f:queue:closed:0")],
                [button("Обновить пульт", "f:home"), button("← Рабочее место", "c:team")]]
        self.screen(chat, uid, "ФИНАНСЫ / ПУЛЬТ", text, rows)

    def queue(self, chat, uid, bucket, page=0):
        page = max(0, min(2000, page))
        cases = self.db.finance_cases(uid, bucket, page * 5, owner_ids=self.owners)
        rows = []
        text = f"<b>{esc(QUEUES[bucket])}</b>\n"
        if not cases: text += "Сейчас в этой очереди нет задач. Данные проверяются по финансовому источнику, а не по отметке сотрудника."
        for c in cases[:5]:
            source = c["source"]
            value = finance_money(source["amount_minor"], source["currency"]) if source["amount_minor"] is not None else METHOD_NAMES.get(source["method"], "без суммы")
            text += f"\n<b>F{c['case_id']:04d} · {esc(CASE_KINDS[c['source_kind']])}</b>\n{esc(value)} · {esc(c['state_label'])}"
            if c["overdue"]: text += " · срок наступил"
            text += "\n"
            rows.append([button(f"F{c['case_id']:04d} · открыть разбор", f"f:case:{c['case_id']}")])
        nav = []
        if page: nav.append(button("← Назад", f"f:queue:{bucket}:{page - 1}"))
        if len(cases) > 5: nav.append(button("Дальше →", f"f:queue:{bucket}:{page + 1}"))
        if nav: rows.append(nav)
        rows.append([button("Обновить", f"f:queue:{bucket}:{page}"), button("← Финансовый пульт", "f:home")])
        self.screen(chat, uid, "ФИНАНСЫ / ОЧЕРЕДЬ", text, rows)

    @staticmethod
    def source_text(s):
        text = f"<b>{esc(s['title'])}</b> · {esc(METHOD_NAMES.get(s['method'], s['method'] or 'способ не указан'))}\n"
        text += f"Источник: {esc(s['source_label'])}\n"
        if s["amount_minor"] is not None: text += f"Записано: <b>{esc(finance_money(s['amount_minor'], s['currency']))}</b>\n"
        if s["expected_minor"] is not None: text += f"Ожидалось за товары: {esc(finance_money(s['expected_minor'], s['expected_currency']))}\n"
        label = STATUS_NAMES.get(s["status"], REASON_NAMES.get(s["status"], s["status"]))
        if s["kind"] == "attempt" and not s["fields"].get("terminal"):
            if s["status"] == "issued": label = "отметка issued без сохранённой привязки счёта — нужна проверка"
            if s["status"] == "failed": label = "непроверенная локальная отметка отказа"
        text += "Статус источника: <b>" + esc(label) + "</b>\n"
        if s["received_at"] is not None: text += esc(when(s["received_at"])) + "\n"
        if s["reasons"]:
            text += "\n" + "\n".join("• " + esc(REASON_NAMES.get(r, STATUS_NAMES.get(r, r))) for r in s["reasons"])
        if s["conflicts"]: text += "\n<b>Разные сведения с одним ID: это не несколько поступлений.</b>"
        if s["purchase_id"]: text += f"\n\nСвязь в журнале: покупка №{s['purchase_id']:04d}."
        else: text += "\n\n<b>Связанная покупка не найдена.</b> Запись остаётся в финансовом журнале."
        if s["payment_attention"]: text += "\nБлокировка покупки: " + esc(s["payment_attention"]) + "."
        return text

    def case(self, chat, uid, cid, page=0):
        self.ui.pause(uid)
        c = self.db.finance_case_view(uid, cid, owner_ids=self.owners, page=max(0, page))
        s = c["source"]
        text = f"<b>РАЗБОР F{cid:04d}</b> · {esc(c['state_label'])}\n"
        text += "Ответственный: " + (f"{c['assigned_to']}" if c["assigned_to"] else "не назначен")
        text += "\nКонтроль: " + esc(when(c["due_at"])) + (" · срок наступил" if c["overdue"] else "") + "\n\n"
        text += self.source_text(s)
        text += "\n\n<b>Финансовый источник отдельно от задачи.</b>\n" + ("Исключение ещё активно. Закрытие вручную запрещено." if c["active"] else "Источник больше не содержит этого исключения. Можно закрыть только задачу разбора, не подтверждая деньги.")
        rows = []
        can_work = c["state"] != "closed" and (c["assigned_to"] == uid or self.ui.role(uid) == "owner")
        meta = {"case_id": cid, "version": c["version"]}
        if c["state"] != "closed" and c["assigned_to"] is None:
            rows.append([self.action(uid, "Взять финансовый разбор", "claim", **meta)])
        if can_work:
            rows.append([self.action(uid, "Этап разбора…", "steps", **meta), self.action(uid, "Срок контроля…", "schedule", **meta)])
            if c["can_note"]: rows.append([self.action(uid, "Внутренняя заметка…", "note_start", **meta)])
            else: text += "\nНет согласованного владельца: только структурированные этапы, без свободного текста."
            if c["can_close"]: rows.append([self.action(uid, "Закрыть только задачу…", "step_check", **meta, step="closed")])
            if c["assigned_to"] is not None: rows.append([self.action(uid, "Вернуть в очередь…", "release_check", **meta)])
        rows.append([button("Реквизиты источника · приватно", f"f:evidence:case:{cid}:0")])
        if s["kind"] == "receipt" and s["conflicts"]: rows.append([button("Различающиеся повторы", f"f:conflicts:{s['source_id']}:0")])
        if s["purchase_id"]: rows.append([button("Связанная покупка", f"c:work:{s['purchase_id']}")])
        if c["events"]:
            text += "\n\n<b>Журнал работы</b>"
            for e in c["events"]:
                text += f"\n{esc(when(e['created_at']))} · {esc(EVENTS.get(e['kind'], e['kind']))}"
                if e["actor_id"]: text += f" · {e['actor_id']}"
                if e["data"].get("step"): text += " / " + esc(FINANCE_STEPS.get(e["data"]["step"], "обновление"))
                if e["kind"] == "note": rows.append([button(f"Заметка #{e['data']['note_id']} · прочитать", f"f:note:{e['data']['note_id']}:0")])
        nav = []
        if page: nav.append(button("← Новее в журнале", f"f:case:{cid}:{page - 1}"))
        if c["has_more"]: nav.append(button("Раньше в журнале →", f"f:case:{cid}:{page + 1}"))
        if nav: rows.append(nav)
        rows.append([button("Обновить карточку", f"f:case:{cid}"), button("← Финансовый пульт", "f:home")])
        self.screen(chat, uid, "ФИНАНСЫ / РАЗБОР", text, rows)

    def receipts(self, chat, uid, page=0, purchase_id=None):
        page = max(0, min(2000, page))
        records = self.db.finance_receipts(uid, page * 5, owner_ids=self.owners, purchase_id=purchase_id)
        text = "<b>Журнал записанных поступлений</b>" + (f" · покупка №{purchase_id:04d}" if purchase_id else "")
        text += "\nНе банковская выписка и не чистая выручка. Импортированные записи отмечены отдельно."
        rows = []
        if not records: text += "\n\nВ этой части журнала записей нет. Выданный счёт сам по себе не означает поступление."
        for r in records[:5]:
            text += f"\n\n<b>R{int(r['source_id']):04d} · {esc(finance_money(r['amount_minor'], r['currency']))}</b>\n{esc(METHOD_NAMES.get(r['method'], r['method']))} · {esc(STATUS_NAMES.get(r['status'], r['status']))}"
            if r["conflicts"]: text += " · есть расхождения"
            rows.append([button(f"R{int(r['source_id']):04d} · карточка поступления", f"f:receipt:{r['source_id']}")])
        route = f"f:purchase:{purchase_id}" if purchase_id else "f:receipts"
        nav = []
        if page: nav.append(button("← Новее", f"{route}:{page - 1}"))
        if len(records) > 5: nav.append(button("Ещё →", f"{route}:{page + 1}"))
        if nav: rows.append(nav)
        if purchase_id: rows.append([button("← Покупка", f"c:work:{purchase_id}")])
        rows.append([button("← Финансовый пульт", "f:home")])
        self.screen(chat, uid, "ФИНАНСЫ / ПОСТУПЛЕНИЯ", text, rows)

    def receipt(self, chat, uid, rid):
        r = self.db.finance_receipt(uid, rid, owner_ids=self.owners)
        text = f"<b>ПОСТУПЛЕНИЕ R{rid:04d}</b>\n\n" + self.source_text(r)
        text += "\n\nЗдесь нет кнопки возврата или ручного зачёта. Журнал хранит исходные события; одинаковый ID не считается новой оплатой."
        rows = [[button("Реквизиты источника · приватно", f"f:evidence:receipt:{rid}:0")]]
        if r["case_id"]: rows.insert(0, [button(f"Рабочая задача F{r['case_id']:04d}", f"f:case:{r['case_id']}")])
        if r["conflicts"]: rows.append([button("Различающиеся повторы", f"f:conflicts:{rid}:0")])
        if r["purchase_id"]: rows.append([button("Связанная покупка", f"c:work:{r['purchase_id']}")])
        rows.append([button("← Журнал поступлений", "f:receipts:0"), button("Пульт", "f:home")])
        self.screen(chat, uid, "ФИНАНСЫ / СОБЫТИЕ", text, rows)

    def plain_pages(self, chat, uid, title, plain, route, page, back, hint=""):
        count = max(1, math.ceil(len(plain) / 350))
        page = max(0, min(page, count - 1))
        text = f"<b>{esc(title)}</b> · {page + 1}/{count}\n{hint}\n\n" + esc(plain[page * 350:(page + 1) * 350])
        nav = []
        if page: nav.append(button("← Начало", f"{route}:{page - 1}"))
        if page + 1 < count: nav.append(button("Дальше →", f"{route}:{page + 1}"))
        self.screen(chat, uid, "ФИНАНСЫ / ПРИВАТНО", text, ([nav] if nav else []) + [[button("← Назад к записи", back)]])

    def evidence(self, chat, uid, kind, record_id, page):
        if kind == "case": source = self.db.finance_case_view(uid, record_id, owner_ids=self.owners)["source"]
        elif kind == "receipt": source = self.db.finance_receipt(uid, record_id, owner_ids=self.owners)
        else: raise ValueError("Неизвестный источник.")
        plain = json.dumps({FIELDS.get(k, k): v for k, v in source["fields"].items()}, ensure_ascii=False, indent=2)
        self.plain_pages(chat, uid, "Реквизиты исходной записи", plain, f"f:evidence:{kind}:{record_id}", page, f"f:{kind}:{record_id}", "Только финансовая роль и владелец. Не пересылай в общий чат. Все фрагменты доступны по страницам.")

    def conflicts(self, chat, uid, rid, page):
        self.db.finance_receipt(uid, rid, owner_ids=self.owners)
        page = max(0, min(10000, page))
        records = list(self.db.connection().execute("SELECT observed,received_at FROM payment_receipt_conflicts WHERE receipt_id=? ORDER BY received_at DESC,fingerprint LIMIT 2 OFFSET ?", (rid, page)))
        text = f"<b>R{rid:04d} · различающийся повтор #{page + 1}</b>\nНе новое поступление. Исходная запись не перезаписана.\n\n"
        if records:
            observed = json.loads(records[0]["observed"])
            safe = {k: observed[k] for k in ("claimed_ref", "amount_minor", "currency", "payer_id", "source") if k in observed}
            # Four bounded ledger fields; JSON escapes control characters.
            text += esc(when(records[0]["received_at"])) + "\n" + esc(json.dumps(safe, ensure_ascii=False, indent=2))
        else: text += "На этой странице нет повторов."
        nav = []
        if page: nav.append(button("← Новее", f"f:conflicts:{rid}:{page - 1}"))
        if len(records) > 1: nav.append(button("Ещё повтор →", f"f:conflicts:{rid}:{page + 1}"))
        self.screen(chat, uid, "ФИНАНСЫ / РАСХОЖДЕНИЕ", text, ([nav] if nav else []) + [[button("← Исходное поступление", f"f:receipt:{rid}")]])

    def summary(self, chat, uid, page):
        r = self.db.finance_summary(uid, owner_ids=self.owners)
        page = max(0, min(page, max(0, (len(r["totals"]) - 1) // 5)))
        text = "<b>Поступления по способу, валюте и состоянию</b>\nЗа всю сохранённую историю. Валюты не складываются и не конвертируются. Спорный оригинал учитывается один раз; различающиеся повторы не добавляют денег.\n"
        for t in r["totals"][page * 5:(page + 1) * 5]:
            status = "спорный оригинал" if t["status"] == "disputed" else STATUS_NAMES.get(t["status"], t["status"])
            text += f"\n<b>{esc(METHOD_NAMES.get(t['method'], t['method']))} / {esc(t['currency'])}</b> · {esc(status)}\n{t['count']} записей · {esc(t['amount_label'])}\n"
        if not r["totals"]: text += "\nВ новом журнале ещё нет проверенных событий.\n"
        text += f"\nИсторический импорт: {r['legacy_records']} записей, <b>исключены из сумм выше</b>.\nПокупок с локальным статусом «paid»: {r['paid_purchases']} (включая ручные и исторические).\n\n<b>Исполненные возвраты: нет данных.</b>\nНет интеграции исполнения возвратов, комиссий, выплат провайдера или кассовых чеков. Это не доход/прибыль и не баланс расчётного счёта."
        nav = []
        if page: nav.append(button("← Назад", f"f:summary:{page - 1}"))
        if (page + 1) * 5 < len(r["totals"]): nav.append(button("Ещё валюты / состояния →", f"f:summary:{page + 1}"))
        self.screen(chat, uid, "ФИНАНСЫ / СВОДКА", text, ([nav] if nav else []) + [[button("← Финансовый пульт", "f:home")]])

    def customer(self, chat, uid, pid, page=0):
        r = self.db.customer_finance(uid, pid, page)
        text = f"<b>ПОКУПКА №{r['number']} / ОПЛАТА</b>\nТовары: {esc(r['goods_amount_label'])}\nУчёт покупки: {esc(r['payment_status_label'])}.\n"
        if r["attention"]: text += "\n<b>Есть финансовая сверка. Не оплачивай повторно.</b>\n"
        if r["manual"]: text += "\nОплата была отмечена командой вручную — это не событие провайдера.\n"
        if r["has_legacy_records"]: text += "\nЕсть историческая запись прежней системы. Она не выдана за новое подтверждённое поступление.\n"
        if r.get("invoice_uncertain"): text += "\nВыдача одного из счетов требует проверки. Уточни статус у команды перед новым платежом.\n"
        text += "\n<b>История доступных поступлений</b>\n"
        if not r["receipts"]: text += "Для этой покупки пока нет событий, доступных в истории. Если списание уже было — обратись в поддержку, не оплачивай ещё раз.\n"
        for p in r["receipts"]:
            text += f"\n{esc(p['number'])} · {esc(p['method_label'])}\n<b>{esc(p['amount_label'])}</b> · {esc(p['status_label'])}\n{esc(when(p['received_at']))}\n"
        text += "\nИстория не является кассовым чеком. Здесь нет подтверждения выполненного возврата: его исполнение не подключено. Стоимость доставки согласуется отдельно и не входит в эти поступления за товары."
        nav = []
        if page: nav.append(button("← Новее", f"f:history:{pid}:{page - 1}"))
        if r["has_more"]: nav.append(button("Ещё поступления →", f"f:history:{pid}:{page + 1}"))
        self.screen(chat, uid, "ПОКУПКА / ИСТОРИЯ ОПЛАТЫ", text, ([nav] if nav else []) + [[button("Вопрос по оплате", f"o:support:{pid}")], [button("← Покупка", f"c:purchase:{pid}"), button("Мои покупки", "c:orders")]])

    def preview(self, chat, uid, token, page=0):
        kind, data = self.db.service_draft(uid, token, owner_ids=self.owners)
        if kind != "finance_note": raise ValueError("Это другой черновик.")
        current_state = self.db.get_state(uid)
        if current_state and current_state[1].get("token") != token: self.ui.pause(uid)
        current = self.db.require_finance_note(uid, data["case_id"], self.owners)
        stale = current["version"] != data["version"]
        if not data.get("body"):
            self.screen(chat, uid, "ФИНАНСЫ / ЗАМЕТКА", f"<b>Внутренняя заметка · F{data['case_id']:04d}</b>\nОдно сообщение до 800 символов, затем проверка перед сохранением.\n\nТолько финансовая роль и владелец. Не записывай пароли, карточные реквизиты, коды или контакты третьих лиц. Текст не подтверждает оплату и исполнение возврата.\n\nЧерновик живёт 15 минут. На паузе сообщения в него не записываются.", [[button("Пауза · главная", "c:home")], [self.action(uid, "Отменить черновик", "discard", token=token)]])
            return
        count = max(1, math.ceil(len(data["body"]) / 350))
        page = max(0, min(page, count - 1))
        rows = [] if stale else [[self.action(uid, "Сохранить только заметку", "send", token=token)]]
        if stale: rows.append([button("Открыть актуальный разбор", f"f:case:{data['case_id']}")])
        nav = []
        if page: nav.append(self.action(uid, "← Начало текста", "preview", token=token, page=page - 1))
        if page + 1 < count: nav.append(self.action(uid, "Проверить дальше →", "preview", token=token, page=page + 1))
        if nav: rows.append(nav)
        rows.append([self.action(uid, "Отменить этот черновик", "discard", token=token), button("Пауза · главная", "c:home")])
        self.screen(chat, uid, "ФИНАНСЫ / ПРОВЕРКА", f"<b>Заметка F{data['case_id']:04d} · фрагмент {page + 1}/{count}</b>\n\n{esc(data['body'][page * 350:(page + 1) * 350])}\n\n" + ("<b>Источник или задача изменились.</b> Текст сохранён в черновике для чтения и копирования, но отправить старую версию нельзя. Сверь новую карточку и подготовь новую заметку." if stale else "Будет сохранён весь текст, не только этот фрагмент. Покупатель и общий чат его не увидят. Деньги и финансовые блокировки не изменятся."), rows)

    def send_draft(self, chat, uid, token):
        self.db.require_staff(uid, "finance.work", self.owners)
        prior = self.db.service_result(uid, token)
        if prior:
            if prior["kind"] == "discarded": raise ValueError("Черновик отменён. Старое подтверждение не сохранит заметку.")
            if prior["kind"] != "finance_note": raise ValueError("Это не финансовая заметка.")
            self.case(chat, uid, prior["result_id"])
            return
        with self.db.commerce_transaction():
            kind, data = self.db.service_draft(uid, token, owner_ids=self.owners)
            if kind != "finance_note": raise ValueError("Это другой черновик.")
            cid = self.db.add_finance_note(uid, data["case_id"], data["body"], data["version"], token, owner_ids=self.owners)
            self.db.clear_service_draft_state(uid, token)
        self.case(chat, uid, cid)

    def dispatch_action(self, chat, uid, token):
        self.db.require_staff(uid, "finance.work", self.owners)
        kind, data = self.db.chat_action(uid, token)
        if not kind.startswith("finance."): raise ValueError("Это другая кнопка.")
        kind = kind.split(".", 1)[1]
        d = data
        if kind == "claim":
            self.db.claim_finance(uid, d["case_id"], d["version"], token, owner_ids=self.owners)
            self.case(chat, uid, d["case_id"])
        elif kind in {"steps", "schedule", "step_check", "due_check", "release_check", "note_start"}:
            c = self.db.finance_case_view(uid, d["case_id"], owner_ids=self.owners)
            self.db._finance_version(c, d["version"])
            self.db._finance_assigned(uid, c, self.owners)
            back = button("← Не менять · карточка", f"f:case:{d['case_id']}")
            if kind == "steps":
                rows = [[self.action(uid, label, "step_check", **d, step=step)] for step, label in FINANCE_STEPS.items() if step != "closed"]
                self.screen(chat, uid, "ФИНАНСЫ / ЭТАП", "<b>Что сейчас происходит с разбором?</b>\nСохраняется только рабочий этап. «Просмотрено» — не подтверждение оплаты. Внешние денежные действия здесь не исполняются.", rows + [[back]])
            elif kind == "schedule":
                rows = [[self.action(uid, f"Контроль через {hours} ч…", "due_check", **d, hours=hours)] for hours in (1, 4, 24)]
                if c["due_at"]: rows.append([self.action(uid, "Явно снять срок…", "due_check", **d, hours=0)])
                self.screen(chat, uid, "ФИНАНСЫ / КОНТРОЛЬ", "<b>Внутренний срок проверки</b>\nВ срок — напоминание ответственному; через час просрочки — сигнал владельцу. Если срок не задан, таймер не выдумывается. Это не обещанная дата возврата покупателю. Перенос отменяет прежние напоминания.", rows + [[back]])
            elif kind == "step_check":
                if d["step"] not in FINANCE_STEPS: raise ValueError("Неизвестный этап.")
                if d["step"] == "closed" and not c["can_close"]: raise ValueError("Исключение ещё не устранено в источнике. Закрыть задачу нельзя.")
                self.screen(chat, uid, "ФИНАНСЫ / ПОДТВЕРЖДЕНИЕ", f"<b>F{d['case_id']:04d} · {esc(FINANCE_STEPS[d['step']])}</b>\n\nПодтвердить только рабочий этап? Это не зачёт оплаты, не возврат денег и не разрешение отправки. Новое финансовое доказательство потребует обновить карточку.", [[self.action(uid, "Подтвердить рабочий этап", "step", **d)], [back]])
            elif kind == "due_check":
                if type(d["hours"]) is not int or d["hours"] not in {0, 1, 4, 24}: raise ValueError("Некорректный срок.")
                text = f"Назначить контроль через {d['hours']} ч после подтверждения? В срок — напоминание, через час просрочки — эскалация владельцу." if d["hours"] else "Снять внутренний срок? Старые напоминания будут отменены. Само исключение и задача остаются."
                self.screen(chat, uid, "ФИНАНСЫ / ПОДТВЕРЖДЕНИЕ СРОКА", f"<b>F{d['case_id']:04d}</b>\n{text}\n\nЭто внутренний контроль, не дата оплаты или возврата.", [[self.action(uid, "Подтвердить срок контроля", "due", **d)], [back]])
            elif kind == "release_check":
                self.screen(chat, uid, "ФИНАНСЫ / ПЕРЕДАЧА", f"Вернуть F{d['case_id']:04d} в общую очередь? Срок и финансовое исключение сохранятся. Действие попадёт в журнал.", [[self.action(uid, "Вернуть в очередь", "release", **d)], [back]])
            else:
                token = self.db.save_service_draft(uid, "finance_note", d, owner_ids=self.owners)
                self.db.set_state(uid, "fin_input", {"token": token})
                self.preview(chat, uid, token)
        elif kind in {"step", "due", "release"}:
            if kind == "step": self.db.finance_step(uid, d["case_id"], d["step"], d["version"], token, owner_ids=self.owners)
            elif kind == "due": self.db.schedule_finance(uid, d["case_id"], d["hours"], d["version"], token, owner_ids=self.owners)
            else: self.db.claim_finance(uid, d["case_id"], d["version"], token, release=True, owner_ids=self.owners)
            self.case(chat, uid, d["case_id"])
        elif kind == "send": self.send_draft(chat, uid, d["token"])
        elif kind == "preview": self.preview(chat, uid, d["token"], d.get("page", 0))
        elif kind == "discard":
            if self.db.discard_service_draft(uid, d["token"]): self.home(chat, uid)
            else: self.send_draft(chat, uid, d["token"])
        else: raise ValueError("Кнопка устарела.")

    def error(self, chat, uid, exc):
        rows = [[button("Главная", "c:home"), button("Мои покупки", "c:orders")]]
        if self.ui.allowed(uid, "finance.read"): rows.insert(0, [button("Финансовый пульт", "f:home")])
        if self.db.get_state(uid): rows.insert(0, [button("Продолжить черновик", "c:resume")])
        self.screen(chat, uid, "ФИНАНСЫ / НУЖНО ВНИМАНИЕ", esc(str(exc)), rows)

    def resume(self, chat, uid):
        state = self.db.get_state(uid)
        if not state or not state[0].startswith("fin_"): return False
        try:
            self.db.require_staff(uid, "finance.work", self.owners)
            self.db.set_state(uid, state[0], {k: v for k, v in state[1].items() if k != "paused"})
            self.preview(chat, uid, state[1]["token"])
        except (ValueError, PermissionError, KeyError, TypeError) as exc: self.error(chat, uid, exc)
        return True

    def handle_callback(self, chat, user, data):
        if not data.startswith("f:"): return False
        uid = int(user["id"])
        try:
            p = data.split(":")
            if p[1] != "history": self.db.require_staff(uid, "finance.read", self.owners)
            if p[1] != "a": self.ui.pause(uid)
            if p[1] == "home": self.home(chat, uid)
            elif p[1] == "a": self.dispatch_action(chat, uid, p[2])
            elif p[1] == "queue": self.queue(chat, uid, p[2], int(p[3]))
            elif p[1] == "case": self.case(chat, uid, int(p[2]), int(p[3]) if len(p) > 3 else 0)
            elif p[1] == "receipt": self.receipt(chat, uid, int(p[2]))
            elif p[1] == "receipts": self.receipts(chat, uid, int(p[2]))
            elif p[1] == "purchase": self.receipts(chat, uid, int(p[3]) if len(p) > 3 else 0, int(p[2]))
            elif p[1] == "summary": self.summary(chat, uid, int(p[2]))
            elif p[1] == "history": self.customer(chat, uid, int(p[2]), int(p[3]) if len(p) > 3 else 0)
            elif p[1] == "evidence": self.evidence(chat, uid, p[2], int(p[3]), int(p[4]))
            elif p[1] == "conflicts": self.conflicts(chat, uid, int(p[2]), int(p[3]))
            elif p[1] == "note":
                n = self.db.finance_note_view(uid, int(p[2]), owner_ids=self.owners)
                self.plain_pages(chat, uid, f"Заметка #{n['note_id']} · F{n['case_id']:04d}", n["body"], f"f:note:{n['note_id']}", int(p[3]), f"f:case:{n['case_id']}", "Внутренняя запись, не подтверждение денег.")
            else: raise ValueError("Кнопка устарела. Открой раздел заново.")
        except (ValueError, PermissionError, IndexError, KeyError, TypeError) as exc: self.error(chat, uid, exc)
        return True

    def handle_message(self, chat, user, message):
        uid = int(user["id"])
        text = str(message.get("text", "")).strip()
        command = text.split(" ", 1)[0].split("@", 1)[0].lower()
        if command == "/finance":
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
            parts = text.split()
            if len(parts) == 1: return self.handle_callback(chat, user, "f:home")
            try:
                self.db.require_staff(uid, "finance.read", self.owners)
                if len(parts) != 3 or parts[1] not in {"receipt", "case", "purchase"} or not re.fullmatch(r"[0-9]{1,19}", parts[2]):
                    raise ValueError("Поиск: /finance receipt НОМЕР, /finance case НОМЕР или /finance purchase НОМЕР. Без телефона и данных карты.")
                return self.handle_callback(chat, user, f"f:{parts[1]}:{int(parts[2])}")
            except (ValueError, PermissionError) as exc: self.error(chat, uid, exc)
            return True
        state = self.db.get_state(uid)
        if not state or not state[0].startswith("fin_"): return False
        if command == "/cancel" or text.lower() in {"отмена", "cancel"}:
            try:
                if not self.db.discard_service_draft(uid, state[1]["token"]):
                    self.send_draft(chat, uid, state[1]["token"])
                    return True
            except ValueError: self.db.clear_state(uid)
            self.ui.home(chat, uid, new=True)
            return True
        if command.startswith("/"): return False
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
        try:
            self.db.require_staff(uid, "finance.work", self.owners)
            if state[1].get("paused"):
                self.ui.home(chat, uid)
                return True
            if state[0] != "fin_input": return self.resume(chat, uid)
            if type(message.get("message_id")) is not int or message["message_id"] < 1:
                raise ValueError("Нужен идентификатор сообщения Telegram. Пришли текст новым сообщением.")
            operation = f"fin-input:{chat}:{message['message_id']}"
            with self.db.commerce_transaction():
                previous, fp = self.db._service_request(uid, operation, "finance_input", [text])
                if previous is None:
                    kind, d = self.db.service_draft(uid, state[1]["token"], owner_ids=self.owners)
                    if kind != "finance_note": raise ValueError("Это другой черновик.")
                    d["body"] = clean_text(text, 800, minimum=3)
                    token = self.db.save_service_draft(uid, "finance_note", d, owner_ids=self.owners)
                    self.db.set_state(uid, "fin_preview", {"token": token})
                    self.db._service_done(uid, operation, "finance_input", fp, 0)
            self.resume(chat, uid)
        except (ValueError, PermissionError, KeyError, TypeError) as exc: self.error(chat, uid, exc)
        return True
