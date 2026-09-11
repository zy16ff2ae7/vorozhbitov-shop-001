"""After-sale operations. No carrier or payment-provider I/O.

Quotes are versioned agreements, NOT payments. Delivery fees in this release
are either covered by the shop or payable directly to the carrier. Support
resolutions never mark a refund paid, clear financial review, or restock goods.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from decimal import Decimal

from commerce_store import CartConflict, encode, now_iso

CARRIERS = {"sdek": "СДЭК", "yandex": "Яндекс Доставка", "post": "Почта России", "pickup": "Самовывоз"}
TOPICS = {"question": "Вопрос", "delivery": "Доставка / адрес", "size": "Размер / посадка", "exchange": "Обмен", "return": "Возврат товара", "payment": "Оплата / сверка"}
SENSITIVE_TOPICS = {"delivery", "exchange", "return", "payment"}
TICKET_LABELS = {"open": "ждёт команды", "waiting_customer": "ждём ответ покупателя", "resolved": "обращение закрыто"}
SHIPMENT_LABELS = {"not_sent": "ещё не передано", "in_transit": "передано перевозчику", "pickup_ready": "ожидает выдачи", "delivered": "вручение зафиксировано"}


def clean_text(value, limit=1200, *, minimum=1):
    if not isinstance(value, str):
        raise ValueError("Нужен текст.")
    value = value.strip()
    if not minimum <= len(value) <= limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"Нужен текст от {minimum} до {limit} символов без управляющих знаков.")
    return value


def delivery_minor(value: str) -> int:
    """Exact RUB cents, including an explicitly confirmed zero; no float math."""
    value = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", value):
        raise ValueError("Сумма: число без пробелов и знака валюты, например 350 или 350,50.")
    return int(Decimal(value) * 100)


def delivery_money(minor: int) -> str:
    return f"{minor // 100:,}".replace(",", " ") + (f",{minor % 100:02d}" if minor % 100 else "") + " ₽"


class OperationsStore:
    def init_operations_store(self):
        conn = self.connection()
        if "operations_managed" not in {r[1] for r in conn.execute("PRAGMA table_info(purchases)")}:
            conn.execute("ALTER TABLE purchases ADD COLUMN operations_managed INTEGER NOT NULL DEFAULT 0")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS delivery_quotes (
                quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id),
                actor_id INTEGER NOT NULL, carrier TEXT NOT NULL,
                destination TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>=0),
                billing TEXT NOT NULL CHECK(billing IN ('carrier','shop')),
                eta TEXT NOT NULL, basis TEXT NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('offered','accepted','declined','superseded')),
                accepted_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_plans (
                purchase_id INTEGER PRIMARY KEY REFERENCES purchases(purchase_id),
                current_quote_id INTEGER REFERENCES delivery_quotes(quote_id),
                state TEXT NOT NULL DEFAULT 'not_sent' CHECK(state IN ('not_sent','in_transit','pickup_ready','delivered')),
                tracking TEXT NOT NULL DEFAULT '', issue TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0, dispatched_at TEXT, delivered_at TEXT,
                delivered_source TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id),
                actor_id INTEGER NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_events_purchase ON delivery_events(purchase_id,event_id);
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                purchase_id INTEGER REFERENCES purchases(purchase_id),
                topic TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','waiting_customer','resolved')),
                assigned_to INTEGER, blocks_dispatch INTEGER NOT NULL DEFAULT 0 CHECK(blocks_dispatch IN (0,1)),
                version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_owner ON support_tickets(user_id,ticket_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_purchase ON support_tickets(purchase_id,status);
            CREATE TABLE IF NOT EXISTS support_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(ticket_id),
                actor_id INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('customer','staff','system')),
                internal INTEGER NOT NULL DEFAULT 0 CHECK(internal IN (0,1)),
                body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_support_messages ON support_messages(ticket_id,message_id);
            CREATE TABLE IF NOT EXISTS service_requests (
                actor_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
                kind TEXT NOT NULL, fingerprint TEXT NOT NULL, result_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(actor_id,operation_id)
            );
            CREATE TABLE IF NOT EXISTS service_drafts (
                token TEXT PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES users(user_id),
                kind TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL
            );
        """)
        self.init_ticket_alerts()

    def init_purchase_operations(self, purchase_id):
        self.connection().execute("INSERT OR IGNORE INTO delivery_plans(purchase_id,updated_at) VALUES (?,?)", (purchase_id, now_iso()))

    def _service_purchase(self, purchase_id, actor_id, permission=None, owner_ids=frozenset()):
        if permission:
            self.require_staff(actor_id, permission, owner_ids)
        if type(purchase_id) is not int or not 1 <= purchase_id <= 2**63 - 1:
            raise ValueError("Покупка не найдена.")
        row = self.connection().execute("SELECT * FROM purchases WHERE purchase_id=?", (purchase_id,)).fetchone()
        if not row or (not permission and row["user_id"] != actor_id):
            raise ValueError("Покупка не найдена.")
        return row

    def _service_request(self, actor_id, operation_id, kind, payload):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-zA-Z0-9:_-]{1,80}", operation_id):
            raise ValueError("Нужен корректный ключ действия.")
        fp = hashlib.sha256(encode([kind, payload]).encode()).hexdigest()
        row = self.connection().execute("SELECT * FROM service_requests WHERE actor_id=? AND operation_id=?", (actor_id, operation_id)).fetchone()
        if row and (row["kind"] != kind or row["fingerprint"] != fp):
            raise ValueError("Этот ключ уже использован с другим содержимым.")
        return (row["result_id"] if row else None), fp

    def _service_done(self, actor, operation, kind, fingerprint, result):
        self.connection().execute("INSERT INTO service_requests VALUES (?,?,?,?,?,?)", (actor, operation, kind, fingerprint, result, now_iso()))

    def service_result(self, actor, operation):
        row = self.connection().execute("SELECT kind,result_id FROM service_requests WHERE actor_id=? AND operation_id=?", (actor, operation)).fetchone()
        return dict(row) if row else None

    def _delivery_event(self, purchase, actor, kind, data, source="staff"):
        self.connection().execute("INSERT INTO delivery_events(purchase_id,actor_id,source,kind,data,created_at) VALUES (?,?,?,?,?,?)", (purchase, actor, source, kind, encode(data), now_iso()))
        # Only references/statuses in the technical staff audit, not addresses or ticket bodies.
        self.audit_staff(actor, "delivery." + kind, str(purchase), None, {k: v for k, v in data.items() if k in {"quote_id", "state", "ticket_id"}})

    def _delivery_notice(self, purchase, operation, text):
        self.enqueue_message(f"delivery:{purchase['purchase_id']}:{operation}", purchase["user_id"], text,
            {"inline_keyboard": [[{"text": "Доставка / условия", "callback_data": f"o:delivery:{purchase['purchase_id']}"}]]})

    def _plan(self, purchase_id):
        row = self.connection().execute("SELECT * FROM delivery_plans WHERE purchase_id=?", (purchase_id,)).fetchone()
        return dict(row) if row else {"purchase_id": purchase_id, "version": 0, "state": "not_sent", "current_quote_id": None,
                                    "tracking": "", "issue": "", "dispatched_at": None, "delivered_at": None, "delivered_source": None}

    @staticmethod
    def _plan_version(plan, version):
        if type(version) is not int or version != plan["version"]:
            raise CartConflict("Доставка уже изменена. Обнови карточку и проверь условия.")

    def _quote_live(self, plan):
        row = self.connection().execute("SELECT * FROM delivery_quotes WHERE quote_id=?", (plan["current_quote_id"],)).fetchone()
        if not row or row["status"] != "accepted" or row["expires_at"] <= time.time():
            raise ValueError("Нужны действующие условия доставки, подтверждённые покупателем.")
        return row

    def _can_offer(self, purchase, plan):
        payment = self.get_payment(purchase["payment_id"])
        if payment["status"] not in {"pending", "paid"} or self.payment_attention(purchase["payment_id"]):
            raise ValueError("Сначала нужна финансовая сверка покупки.")
        if purchase["fulfillment"] in {"cancelled", "expired", "completed"} or plan["state"] != "not_sent":
            raise ValueError("После передачи или завершения условия не меняются. Создай обращение.")
        if payment["status"] == "pending" and not self.payment_is_payable(purchase["payment_id"]):
            raise ValueError("Резерв покупки уже не действует.")
        self.require_consent(purchase["user_id"])

    def propose_delivery(self, actor_id, purchase_id, terms, version, operation_id, *, owner_ids=frozenset(), ttl=86400):
        if not isinstance(terms, dict): raise ValueError("Нужны условия доставки.")
        carrier = terms.get("carrier")
        amount, billing = terms.get("amount_minor"), terms.get("billing")
        if carrier not in CARRIERS or billing not in {"carrier", "shop"}:
            raise ValueError("Выбери перевозчика и способ расчёта доставки.")
        if type(amount) is not int or not 0 <= amount <= 99_999_999:
            raise ValueError("Стоимость доставки — целое количество копеек от 0 до 99999999.")
        if (billing == "shop" and amount != 0) or (carrier == "pickup" and (amount != 0 or billing != "shop")):
            raise ValueError("Магазин покрывает доставку / самовывоз: доплата покупателя должна быть 0.")
        if type(ttl) is not int or not 300 <= ttl <= 7 * 86400:
            raise ValueError("Срок предложения — от 5 минут до 7 дней.")
        data = {"carrier": carrier, "amount_minor": amount, "billing": billing,
                "destination": clean_text(terms.get("destination"), 240, minimum=3),
                "eta": clean_text(terms.get("eta"), 100, minimum=3), "basis": clean_text(terms.get("basis"), 160, minimum=3)}
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, actor_id, "shipping.quote", owner_ids)
            prior, fp = self._service_request(actor_id, operation_id, "quote", [purchase_id, data, version, ttl])
            if prior is not None: return prior
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            self._can_offer(purchase, plan)
            self.init_purchase_operations(purchase_id)
            conn.execute("UPDATE delivery_quotes SET status='superseded' WHERE quote_id=?", (plan["current_quote_id"],))
            qid = conn.execute("""INSERT INTO delivery_quotes(purchase_id,actor_id,carrier,destination,amount_minor,billing,eta,basis,expires_at,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,'offered',?)""", (purchase_id, actor_id, carrier, data["destination"], amount, billing, data["eta"], data["basis"], time.time() + ttl, now_iso())).lastrowid
            conn.execute("UPDATE delivery_plans SET current_quote_id=?,version=version+1,updated_at=? WHERE purchase_id=?", (qid, now_iso(), purchase_id))
            conn.execute("UPDATE purchases SET operations_managed=1 WHERE purchase_id=?", (purchase_id,))
            self._delivery_event(purchase_id, actor_id, "offered", {"quote_id": qid})
            self._delivery_notice(purchase, f"quote:{qid}", f"Для покупки №{purchase_id:04d} подготовлены условия доставки. Проверь адрес, стоимость, способ расчёта и срок в карточке. Это не новый счёт и не списание.")
            self._service_done(actor_id, operation_id, "quote", fp, qid)
            return qid

    def answer_delivery(self, user_id, purchase_id, quote_id, version, accept, operation_id):
        if type(quote_id) is not int or not 1 <= quote_id <= 2**63 - 1:
            raise ValueError("Предложение не найдено.")
        if type(accept) is not bool:
            raise ValueError("Нужно явное решение покупателя.")
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, user_id)
            self.require_consent(user_id)
            prior, fp = self._service_request(user_id, operation_id, "quote_answer", [purchase_id, quote_id, version, accept])
            if prior is not None: return prior
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            self._can_offer(purchase, plan)
            quote = conn.execute("SELECT * FROM delivery_quotes WHERE quote_id=? AND purchase_id=?", (quote_id, purchase_id)).fetchone()
            if not quote or plan["current_quote_id"] != quote_id or quote["status"] != "offered" or quote["expires_at"] <= time.time():
                raise ValueError("Предложение устарело. Нужны новые условия от менеджера.")
            status = "accepted" if accept else "declined"
            conn.execute("UPDATE delivery_quotes SET status=?,accepted_at=? WHERE quote_id=?", (status, now_iso() if accept else None, quote_id))
            conn.execute("UPDATE delivery_plans SET version=version+1,updated_at=? WHERE purchase_id=?", (now_iso(), purchase_id))
            self._delivery_event(purchase_id, user_id, status, {"quote_id": quote_id}, "customer")
            self._service_done(user_id, operation_id, "quote_answer", fp, purchase_id)
            self._delivery_notice(purchase, f"answer:{quote_id}:{status}", f"Покупка №{purchase_id:04d}: условия доставки {'подтверждены' if accept else 'отклонены'}. {'Доставка не оплачена этим действием.' if accept else 'Передача заблокирована до нового согласования.'}")
            self.enqueue_message(f"delivery:team:{quote_id}:{status}", quote["actor_id"],
                f"Покупка №{purchase_id:04d}: покупатель {'принял' if accept else 'отклонил'} условия доставки. Подробности в рабочей карточке.",
                {"inline_keyboard": [[{"text": "Открыть доставку", "callback_data": f"o:workdelivery:{purchase_id}"}]]})
            return purchase_id

    def dispatch_blockers(self, purchase_id):
        return [r[0] for r in self.connection().execute("SELECT ticket_id FROM support_tickets WHERE purchase_id=? AND status<>'resolved' AND blocks_dispatch=1 ORDER BY ticket_id", (purchase_id,))]

    @staticmethod
    def tracking_code(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,47}", value.strip()):
            raise ValueError("Трек: 4–48 латинских букв, цифр или дефисов. Не ссылка.")
        return value.strip().upper()

    def dispatch_delivery(self, actor_id, purchase_id, version, tracking, operation_id, *, owner_ids=frozenset()):
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, actor_id, "shipping.dispatch", owner_ids)
            prior, fp = self._service_request(actor_id, operation_id, "dispatch", [purchase_id, version, tracking])
            if prior is not None: return prior
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            if purchase["assigned_to"] != actor_id and self.staff_role(actor_id, owner_ids) != "owner":
                raise PermissionError("Сначала возьми покупку в работу или получи её из общей очереди.")
            payment = self.get_payment(purchase["payment_id"])
            if payment["status"] != "paid" or self.payment_attention(purchase["payment_id"]):
                raise ValueError("Передача запрещена без подтверждённой оплаты или при финансовой сверке.")
            if purchase["fulfillment"] != "ready" or plan["state"] != "not_sent":
                raise ValueError("Сначала подготовь всю покупку. Повторная передача запрещена.")
            if self.dispatch_blockers(purchase_id):
                raise ValueError("Передача приостановлена обращением покупателя. Сначала согласуй решение.")
            quote = self._quote_live(plan)
            pickup = quote["carrier"] == "pickup"
            code = "" if pickup else self.tracking_code(tracking)
            if pickup and tracking:
                raise ValueError("Для самовывоза трек не нужен.")
            state = "pickup_ready" if pickup else "in_transit"
            conn.execute("UPDATE delivery_plans SET state=?,tracking=?,dispatched_at=?,version=version+1,updated_at=? WHERE purchase_id=?", (state, code, now_iso(), now_iso(), purchase_id))
            self._delivery_event(purchase_id, actor_id, "dispatched", {"state": state, "quote_id": quote["quote_id"], "tracking": code})
            self._service_done(actor_id, operation_id, "dispatch", fp, purchase_id)
            self._delivery_notice(purchase, f"dispatch:{plan['version']}", f"Покупка №{purchase_id:04d}: команда отметила {'готовность к самовывозу' if pickup else 'передачу перевозчику'}. Данные и трек — в карточке. Это ручная отметка, не ответ API перевозчика.")
            return purchase_id

    def correct_tracking(self, actor_id, purchase_id, version, tracking, reason, operation_id, *, owner_ids=frozenset()):
        code, reason = self.tracking_code(tracking), clean_text(reason, 240, minimum=3)
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, actor_id, "shipping.quote", owner_ids)
            prior, fp = self._service_request(actor_id, operation_id, "tracking", [purchase_id, version, code, reason])
            if prior is not None: return prior
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            if plan["state"] != "in_transit": raise ValueError("Трек меняется только до вручения отправленной покупки.")
            conn.execute("UPDATE delivery_plans SET tracking=?,version=version+1,updated_at=? WHERE purchase_id=?", (code, now_iso(), purchase_id))
            self._delivery_event(purchase_id, actor_id, "tracking_corrected", {"previous": plan["tracking"], "tracking": code, "reason": reason})
            self._service_done(actor_id, operation_id, "tracking", fp, purchase_id)
            self._delivery_notice(purchase, f"tracking:{plan['version']}", f"Для покупки №{purchase_id:04d} команда исправила трек-номер. Новый номер и причина — в карточке доставки.")
            return purchase_id

    def mark_received(self, actor_id, purchase_id, version, operation_id, *, staff=False, owner_ids=frozenset()):
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, actor_id, "shipping.dispatch" if staff else None, owner_ids)
            prior, fp = self._service_request(actor_id, operation_id, "received", [purchase_id, version, staff])
            if prior is not None: return prior
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            if plan["state"] not in {"in_transit", "pickup_ready"}:
                raise ValueError("Сначала нужна фактическая передача или подготовка самовывоза. Повторное вручение запрещено.")
            if staff and purchase["assigned_to"] != actor_id and self.staff_role(actor_id, owner_ids) != "owner":
                raise PermissionError("Сначала возьми покупку в работу.")
            source = "staff" if staff else "customer"
            conn.execute("UPDATE delivery_plans SET state='delivered',delivered_at=?,delivered_source=?,version=version+1,updated_at=? WHERE purchase_id=?", (now_iso(), source, now_iso(), purchase_id))
            self._delivery_event(purchase_id, actor_id, "received", {"state": "delivered"}, source)
            # Preserve real delivery facts even if a financial anomaly arrived
            # after dispatch, but never revive cancelled/refund/review fulfillment.
            payment = self.get_payment(purchase["payment_id"])
            if payment["status"] == "paid" and not self.payment_attention(purchase["payment_id"]) and purchase["fulfillment"] == "ready":
                conn.execute("UPDATE purchases SET fulfillment='completed',version=version+1 WHERE purchase_id=?", (purchase_id,))
                conn.execute("UPDATE orders SET status='completed' WHERE payment_id=?", (purchase["payment_id"],))
                self.audit_staff(actor_id, "purchase.received", str(purchase_id), "ready", "completed")
            self._service_done(actor_id, operation_id, "received", fp, purchase_id)
            self._delivery_notice(purchase, f"received:{plan['version']}", f"По покупке №{purchase_id:04d} {'команда отметила вручение' if staff else 'ты подтвердил получение'}. Если есть проблема, открой обращение. Статус возврата денег этим не меняется.")
            return purchase_id

    def require_operations_completion(self, purchase):
        if purchase["operations_managed"] and self._plan(purchase["purchase_id"])["state"] != "delivered":
            raise ValueError("Сначала зафиксируй вручение в карточке доставки. Сборка не равна доставке.")

    def delivery_view(self, actor_id, purchase_id, *, staff=False, owner_ids=frozenset()):
        purchase = self._service_purchase(purchase_id, actor_id, "shipping.read" if staff else None, owner_ids)
        role = self.staff_role(actor_id, owner_ids) if staff else "customer"
        plan = self._plan(purchase_id)
        row = self.connection().execute("SELECT * FROM delivery_quotes WHERE quote_id=?", (plan["current_quote_id"],)).fetchone()
        quote = None
        if row:
            quote = {k: row[k] for k in ("quote_id", "carrier", "amount_minor", "billing", "eta", "expires_at", "status", "accepted_at")}
            quote["carrier_label"] = CARRIERS.get(row["carrier"], row["carrier"])
            quote["amount_label"] = delivery_money(row["amount_minor"])
            if row["status"] in {"offered", "accepted"} and row["expires_at"] <= time.time() and plan["state"] == "not_sent":
                quote["status"] = "expired"
            if role in {"customer", "manager", "owner"}:
                quote.update(destination=row["destination"], basis=row["basis"])
        payment = self.get_payment(purchase["payment_id"])
        attention = self.payment_attention(purchase["payment_id"])
        editable = plan["state"] == "not_sent" and purchase["fulfillment"] not in {"cancelled", "expired", "completed"} and payment["status"] in {"paid", "pending"} and not attention
        if payment["status"] == "pending" and not self.payment_is_payable(purchase["payment_id"]): editable = False
        blockers = self.dispatch_blockers(purchase_id) if plan["state"] == "not_sent" and purchase["fulfillment"] not in {"completed", "cancelled", "expired"} else []
        return {**{k: plan[k] for k in ("purchase_id", "version", "state", "tracking", "issue", "dispatched_at", "delivered_at", "delivered_source")},
                "state_label": SHIPMENT_LABELS[plan["state"]], "quote": quote, "operations_managed": bool(purchase["operations_managed"]),
                "goods_amount_minor": payment["amount_rub"] * 100, "budget_minor": payment["amount_rub"] * 100 + quote["amount_minor"] if quote else None,
                "can_offer": editable, "can_answer": bool(editable and quote and quote["status"] == "offered"),
                "can_dispatch": bool(editable and quote and quote["status"] == "accepted" and purchase["fulfillment"] == "ready" and payment["status"] == "paid" and not self.dispatch_blockers(purchase_id)),
                "can_receive": plan["state"] in {"in_transit", "pickup_ready"}, "payment_attention": attention,
                "blocking_tickets": blockers,
                "events": [{"kind": r["kind"], "source": r["source"], "created_at": r["created_at"],
                            "data": {k: v for k, v in json.loads(r["data"]).items() if role in {"customer", "manager", "owner"} or k in {"quote_id", "state", "ticket_id", "tracking"}}} for r in self.connection().execute("SELECT * FROM delivery_events WHERE purchase_id=? ORDER BY event_id DESC LIMIT 8", (purchase_id,))]}

    def delivery_queue(self, actor_id, bucket="agreement", offset=0, *, owner_ids=frozenset()):
        self.require_staff(actor_id, "shipping.read", owner_ids)
        conditions = {"agreement": "d.state='not_sent' AND (q.quote_id IS NULL OR q.status<>'accepted' OR q.expires_at<=?)",
                      "ready": "d.state='not_sent' AND q.status='accepted' AND q.expires_at>? AND u.fulfillment='ready'",
                      "sent": "d.state IN ('in_transit','pickup_ready') AND ?>=0", "done": "d.state='delivered' AND ?>=0"}
        if bucket not in conditions: raise ValueError("Неизвестная очередь доставки.")
        rows = self.connection().execute(f"""SELECT u.purchase_id FROM delivery_plans d JOIN purchases u ON u.purchase_id=d.purchase_id
            JOIN payments p ON p.payment_id=u.payment_id LEFT JOIN delivery_quotes q ON q.quote_id=d.current_quote_id
            WHERE ({conditions[bucket]}) AND (d.state<>'not_sent' OR (u.fulfillment NOT IN ('cancelled','expired','completed') AND p.status IN ('pending','paid')))
            ORDER BY u.purchase_id DESC LIMIT 6 OFFSET ?""", (time.time(), max(0, min(10000, offset))))
        return [self.delivery_view(actor_id, r[0], staff=True, owner_ids=owner_ids) for r in list(rows)]

    def _ticket(self, actor, ticket_id, staff=False, owner_ids=frozenset(), permission="support.read"):
        if staff: self.require_staff(actor, permission, owner_ids)
        if type(ticket_id) is not int or not 1 <= ticket_id <= 2**63 - 1: raise ValueError("Обращение не найдено.")
        row = self.connection().execute("SELECT * FROM support_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if not row or (not staff and row["user_id"] != actor): raise ValueError("Обращение не найдено.")
        return row

    def _ticket_message(self, ticket, actor, source, body, internal=False):
        return self.connection().execute("INSERT INTO support_messages(ticket_id,actor_id,source,internal,body,created_at) VALUES (?,?,?,?,?,?)", (ticket, actor, source, int(internal), body, now_iso())).lastrowid

    def _notify_ticket_team(self, ticket_id, operation):
        ticket = self.connection().execute("SELECT * FROM support_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        # No message body/contacts in group notifications. The staff callback
        # still requires a private chat and a role at the time it is opened.
        target = ticket["assigned_to"]
        self.connection().execute("INSERT OR IGNORE INTO ticket_alerts(ticket_id,operation_id,assigned_to) VALUES (?,?,?)", (ticket_id, operation, target))

    def init_ticket_alerts(self):
        self.connection().execute("""CREATE TABLE IF NOT EXISTS ticket_alerts (
            ticket_id INTEGER NOT NULL REFERENCES support_tickets(ticket_id), operation_id TEXT NOT NULL,
            assigned_to INTEGER, queued_at TEXT, PRIMARY KEY(ticket_id,operation_id))""")

    def queue_ticket_alerts(self, owner_ids=frozenset(), manager_chat_id=None):
        # Called by the bot worker with trusted settings, never user-supplied recipients.
        with self.commerce_transaction() as conn:
            for row in list(conn.execute("SELECT * FROM ticket_alerts WHERE queued_at IS NULL LIMIT 50")):
                targets = [row["assigned_to"]] if row["assigned_to"] and self.staff_role(row["assigned_to"], owner_ids) in {"manager", "support", "owner"} else []
                if not targets: targets = [manager_chat_id] if manager_chat_id else list(owner_ids)
                if not targets: continue  # Leave durable pending alert for configuration/recovery.
                for target in targets:
                    self.enqueue_message(f"ticket:{row['ticket_id']}:{row['operation_id']}:{target}", target,
                        f"Обращение №{row['ticket_id']:04d}: есть обновление. Открой /desk в личном чате с ботом. Текст доступен только команде поддержки.",
                        {"inline_keyboard": [[{"text": "Рабочая карточка", "callback_data": f"o:workticket:{row['ticket_id']}"}]]})
                conn.execute("UPDATE ticket_alerts SET queued_at=? WHERE ticket_id=? AND operation_id=?", (now_iso(), row["ticket_id"], row["operation_id"]))

    def create_ticket(self, user_id, topic, body, operation_id, purchase_id=None):
        if topic not in TOPICS: raise ValueError("Выбери тему обращения.")
        body = clean_text(body)
        with self.commerce_transaction() as conn:
            self.require_consent(user_id)
            if purchase_id is not None: self._service_purchase(purchase_id, user_id)
            prior, fp = self._service_request(user_id, operation_id, "ticket_new", [purchase_id, topic, body])
            if prior is not None: return prior
            if conn.execute("SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND status<>'resolved'", (user_id,)).fetchone()[0] >= 10:
                raise ValueError("У тебя уже 10 открытых обращений. Продолжи подходящее, не создавая дубль.")
            tid = conn.execute("INSERT INTO support_tickets(user_id,purchase_id,topic,status,blocks_dispatch,created_at,updated_at) VALUES (?,?,?,'open',?,?,?)",
                               (user_id, purchase_id, topic, int(purchase_id is not None and topic in SENSITIVE_TOPICS), now_iso(), now_iso())).lastrowid
            self._ticket_message(tid, user_id, "customer", body)
            self._service_done(user_id, operation_id, "ticket_new", fp, tid)
            self.event(user_id, "support_created", {"ticket_id": tid, "topic": topic, "purchase_id": purchase_id})
            self._notify_ticket_team(tid, "created")
            return tid

    def claim_ticket(self, actor, ticket_id, operation_id, *, release=False, version=None, owner_ids=frozenset()):
        with self.commerce_transaction() as conn:
            row = self._ticket(actor, ticket_id, True, owner_ids, "support.claim")
            kind = "ticket_release" if release else "ticket_claim"
            prior, fp = self._service_request(actor, operation_id, kind, [ticket_id, release, version])
            if prior is not None: return prior
            if release:
                if self.staff_role(actor, owner_ids) != "owner" and row["assigned_to"] != actor: raise PermissionError("Чужое назначение снимает владелец.")
                if row["version"] != version: raise CartConflict("Обращение изменилось. Обнови карточку.")
            elif row["assigned_to"] not in {None, actor}:
                raise ValueError("Обращение уже взял другой сотрудник.")
            if row["status"] == "resolved": raise ValueError("Обращение уже закрыто.")
            target = None if release else actor
            conn.execute("UPDATE support_tickets SET assigned_to=?,version=version+1,updated_at=? WHERE ticket_id=?", (target, now_iso(), ticket_id))
            self.audit_staff(actor, kind, str(ticket_id), row["assigned_to"], target)
            self._service_done(actor, operation_id, kind, fp, ticket_id)
            return ticket_id

    def reply_ticket(self, actor, ticket_id, body, version, operation_id, *, staff=False, internal=False, resolve=False, owner_ids=frozenset()):
        body = clean_text(body)
        if type(internal) is not bool or type(resolve) is not bool: raise ValueError("Некорректное действие.")
        if internal and (not staff or resolve): raise ValueError("Внутренняя заметка не закрывает обращение и не является ответом покупателю.")
        with self.commerce_transaction() as conn:
            row = self._ticket(actor, ticket_id, staff, owner_ids, "support.write")
            if not staff: self.require_consent(actor)
            prior, fp = self._service_request(actor, operation_id, "ticket_reply", [ticket_id, body, version, staff, internal, resolve])
            if prior is not None: return prior
            if type(version) is not int or row["version"] != version: raise CartConflict("В обращении есть новое действие. Прочитай его и ответь заново.")
            if staff:
                if row["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner": raise PermissionError("Сначала возьми обращение в работу.")
                if resolve and row["blocks_dispatch"]: self.require_staff(actor, "support.resolve_sensitive", owner_ids)
                if row["status"] == "resolved" and not internal: raise ValueError("Обращение закрыто. Его может возобновить покупатель.")
            if row["status"] == "resolved" and resolve: raise ValueError("Обращение уже закрыто.")
            if conn.execute("SELECT COUNT(*) FROM support_messages WHERE ticket_id=?", (ticket_id,)).fetchone()[0] >= 1000:
                raise ValueError("Достигнут предел переписки. Создай новое обращение.")
            new_status = row["status"] if internal else "resolved" if resolve else "waiting_customer" if staff else "open"
            mid = self._ticket_message(ticket_id, actor, "staff" if staff else "customer", body, internal)
            conn.execute("UPDATE support_tickets SET status=?,version=version+1,updated_at=? WHERE ticket_id=?", (new_status, now_iso(), ticket_id))
            self._service_done(actor, operation_id, "ticket_reply", fp, ticket_id)
            self.audit_staff(actor, "support.note" if internal else "support.resolve" if resolve else "support.reply", str(ticket_id), row["status"], {"status": new_status, "message_id": mid})
            if not internal:
                if staff:
                    self.enqueue_message(f"support:{ticket_id}:message:{mid}", row["user_id"],
                        f"По обращению №{ticket_id:04d} {'есть решение команды' if resolve else 'пришёл ответ команды'}. Открой переписку. Закрытие обращения само по себе не означает возврат денег.",
                        {"inline_keyboard": [[{"text": "Прочитать ответ", "callback_data": f"o:ticket:{ticket_id}"}]]})
                else: self._notify_ticket_team(ticket_id, f"message:{mid}")
            return ticket_id

    def report_delivery_issue(self, actor, purchase_id, version, body, operation_id, *, owner_ids=frozenset()):
        body = clean_text(body, 400, minimum=3)
        with self.commerce_transaction() as conn:
            purchase = self._service_purchase(purchase_id, actor, "shipping.dispatch", owner_ids)
            prior, fp = self._service_request(actor, operation_id, "shipment_issue", [purchase_id, version, body])
            if prior is not None: return prior
            if purchase["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner":
                raise PermissionError("Сначала возьми покупку в работу.")
            plan = self._plan(purchase_id)
            self._plan_version(plan, version)
            if plan["state"] == "not_sent": raise ValueError("Проблема отправления фиксируется после передачи. До неё используй согласование и поддержку.")
            self.require_consent(purchase["user_id"])
            conn.execute("UPDATE delivery_plans SET issue=?,version=version+1,updated_at=? WHERE purchase_id=?", (body, now_iso(), purchase_id))
            tid = conn.execute("INSERT INTO support_tickets(user_id,purchase_id,topic,status,created_at,updated_at) VALUES (?,?,'delivery','open',?,?)", (purchase["user_id"], purchase_id, now_iso(), now_iso())).lastrowid
            self._ticket_message(tid, actor, "staff", "Команда сообщила о проблеме отправления: " + body)
            self._delivery_event(purchase_id, actor, "problem", {"ticket_id": tid})
            self._service_done(actor, operation_id, "shipment_issue", fp, tid)
            self._notify_ticket_team(tid, "created")
            self._delivery_notice(purchase, f"problem:{plan['version']}", f"По отправлению покупки №{purchase_id:04d} команда сообщила о проблеме. Создано обращение №{tid:04d}; его можно продолжить в разделе поддержки.")
            return tid

    def ticket_view(self, actor, ticket_id, *, staff=False, owner_ids=frozenset(), page=0, include_messages=True):
        row = self._ticket(actor, ticket_id, staff, owner_ids)
        page = max(0, min(10000, page))
        messages = list(self.connection().execute("SELECT * FROM support_messages WHERE ticket_id=? AND (?=1 OR internal=0) ORDER BY message_id DESC LIMIT 5 OFFSET ?", (ticket_id, int(staff), page * 4))) if include_messages else []
        sensitive = bool(row["blocks_dispatch"] and row["status"] != "resolved")
        blocks = sensitive
        if row["purchase_id"]:
            purchase = self.connection().execute("SELECT fulfillment FROM purchases WHERE purchase_id=?", (row["purchase_id"],)).fetchone()
            blocks = sensitive and self._plan(row["purchase_id"])["state"] == "not_sent" and purchase["fulfillment"] not in {"completed", "cancelled", "expired"}
        return {"ticket_id": ticket_id, "purchase_id": row["purchase_id"], "topic": row["topic"], "topic_label": TOPICS[row["topic"]],
                "status": row["status"], "status_label": TICKET_LABELS[row["status"]], "version": row["version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"], "blocks_dispatch": blocks, "requires_manager": sensitive,
                **({"assigned_to": row["assigned_to"], "user_id": row["user_id"]} if staff else {}),
                "has_more": len(messages) > 4,
                "messages": [{"message_id": m["message_id"], "source": m["source"], "body": m["body"], "created_at": m["created_at"],
                              **({"internal": bool(m["internal"]), "actor_id": m["actor_id"]} if staff else {})} for m in reversed(messages[:4])]}

    def tickets_list(self, actor, *, staff=False, bucket="open", offset=0, purchase_id=None, owner_ids=frozenset()):
        if staff: self.require_staff(actor, "support.read", owner_ids)
        if bucket not in {"open", "mine", "resolved", "all"}: raise ValueError("Неизвестная очередь обращений.")
        where, args = ["1=1"], []
        if not staff: where.append("user_id=?"); args.append(actor)
        if purchase_id is not None:
            self._service_purchase(purchase_id, actor, "orders.read" if staff else None, owner_ids)
            where.append("purchase_id=?"); args.append(purchase_id)
        if bucket == "resolved": where.append("status='resolved'")
        elif bucket != "all": where.append("status<>'resolved'")
        if bucket == "mine" and staff: where.append("assigned_to=?"); args.append(actor)
        rows = list(self.connection().execute("SELECT ticket_id FROM support_tickets WHERE " + " AND ".join(where) + " ORDER BY ticket_id DESC LIMIT 6 OFFSET ?", (*args, max(0, min(10000, offset)))))
        # Lists do not carry message bodies or internal notes.
        return [{k: v for k, v in self.ticket_view(actor, row[0], staff=staff, owner_ids=owner_ids, include_messages=False).items() if k not in {"messages", "has_more"}} for row in rows]

    def _draft_access(self, actor, kind, payload, owner_ids):
        if kind == "ticket_new":
            self.require_consent(actor)
            if payload.get("purchase_id") is not None: self._service_purchase(payload["purchase_id"], actor)
        elif kind == "ticket_reply":
            ticket = self._ticket(actor, payload["ticket_id"], bool(payload.get("staff")), owner_ids, "support.write")
            if payload.get("staff") and ticket["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner":
                raise PermissionError("Сначала возьми обращение в работу.")
            if payload.get("staff") and payload.get("resolve") and ticket["blocks_dispatch"]:
                self.require_staff(actor, "support.resolve_sensitive", owner_ids)
            if not payload.get("staff"): self.require_consent(actor)
        elif kind in {"quote", "dispatch", "tracking", "shipment_issue"}:
            p = self._service_purchase(payload["purchase_id"], actor, "shipping.quote" if kind in {"quote", "tracking"} else "shipping.dispatch", owner_ids)
            self.require_consent(p["user_id"])
            if kind in {"dispatch", "shipment_issue"} and p["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner":
                raise PermissionError("Сначала возьми покупку в работу.")
        elif kind == "finance_note":
            # A stale draft may still be read/copied by its authorised author;
            # committing it always rechecks the evidence version in FinanceStore.
            self.require_finance_note(actor, payload["case_id"], owner_ids)
        else: raise ValueError("Неизвестный черновик.")

    def save_service_draft(self, actor, kind, payload, *, owner_ids=frozenset()):
        with self.commerce_transaction() as conn:
            self._draft_access(actor, kind, payload, owner_ids)
            token = secrets.token_hex(8)
            conn.execute("INSERT INTO service_drafts VALUES (?,?,?,?,?)", (token, actor, kind, encode(payload), time.time() + 900))
            return token

    def service_draft(self, actor, token, *, owner_ids=frozenset()):
        row = self.connection().execute("SELECT * FROM service_drafts WHERE token=? AND actor_id=?", (token, actor)).fetchone()
        if not row or row["expires_at"] <= time.time(): raise ValueError("Черновик устарел. Открой действие заново.")
        data = json.loads(row["payload"])
        self._draft_access(actor, row["kind"], data, owner_ids)
        return row["kind"], data

    def clear_service_draft_state(self, actor, token):
        with self.commerce_transaction() as conn:
            row = conn.execute("SELECT state,data FROM states WHERE user_id=?", (actor,)).fetchone()
            if row and row["state"].startswith(("ops_", "fin_")) and json.loads(row["data"]).get("token") == token:
                conn.execute("DELETE FROM states WHERE user_id=?", (actor,))

    def discard_service_draft(self, actor, token):
        with self.commerce_transaction() as conn:
            result = self.service_result(actor, token)
            if result:
                return result["kind"] == "discarded"  # Never undo an already committed action.
            row = conn.execute("SELECT token FROM service_drafts WHERE token=? AND actor_id=?", (token, actor)).fetchone()
            if not row:
                raise ValueError("Черновик не найден или уже очищен.")
            conn.execute("DELETE FROM service_drafts WHERE token=? AND actor_id=?", (token, actor))
            self._service_done(actor, token, "discarded", hashlib.sha256(b"discarded").hexdigest(), 0)
            self.clear_service_draft_state(actor, token)
            return True

    def prune_operations(self):
        with self.commerce_transaction() as conn:
            conn.execute("DELETE FROM service_drafts WHERE expires_at<?", (time.time() - 86400,))
