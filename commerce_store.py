"""Trading core: account carts, inventory, reservations, purchases and staff ACL.

No network calls. Database provides connection(), get_payment(), event(),
enqueue_message(), payment_is_payable() and the immutable checkout ledger.
Inventory starts UNKNOWN; catalog labels are never interpreted as quantities.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import time
from typing import Any

MAX_CART_LINES = 20
MAX_QUANTITY = 20
ROLE_PERMISSIONS = {
    "finance": {"orders.read", "finance.read", "finance.work"},
    "owner": {"alerts.monitor", "promotions.write", "orders.read", "orders.write", "orders.claim", "inventory.read", "inventory.write", "staff.write", "audit.read", "shipping.read", "shipping.quote", "shipping.dispatch", "support.read", "support.write", "support.claim", "support.resolve_sensitive", "finance.read", "finance.work"},
    "manager": {"orders.read", "orders.write", "orders.claim", "inventory.read", "shipping.read", "shipping.quote", "shipping.dispatch", "support.read", "support.write", "support.claim", "support.resolve_sensitive"},
    "warehouse": {"orders.read", "orders.claim", "orders.pack", "inventory.read", "inventory.write", "shipping.read", "shipping.dispatch"},
    "support": {"orders.read", "shipping.read", "support.read", "support.write", "support.claim"},
}
FULFILLMENT_LABELS = {"new": "ждёт сборки", "packing": "собираем", "ready": "готов к выдаче", "completed": "завершён", "cancelled": "отменён", "expired": "резерв истёк"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CartConflict(ValueError):
    pass


class StockError(ValueError):
    pass


class CommerceStore:
    @contextmanager
    def commerce_transaction(self):
        conn = self.connection()
        nested = conn.in_transaction
        savepoint = "commerce_" + secrets.token_hex(6)
        conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute(f"RELEASE {savepoint}" if nested else "COMMIT")
        except BaseException:
            if conn.in_transaction:
                if nested:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                else:
                    conn.execute("ROLLBACK")
            raise

    def init_commerce_store(self) -> None:
        self.connection().executescript("""
            CREATE TABLE IF NOT EXISTS shopping_carts (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cart_lines (
                line_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES shopping_carts(user_id) ON DELETE CASCADE,
                product_id TEXT NOT NULL, size TEXT NOT NULL, person TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 20),
                UNIQUE(user_id, product_id, size, person)
            );
            CREATE TABLE IF NOT EXISTS cart_mutations (
                user_id INTEGER NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(user_id, operation_id)
            );
            CREATE TABLE IF NOT EXISTS stock_items (
                sku_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL, size TEXT NOT NULL,
                on_hand INTEGER CHECK(on_hand IS NULL OR on_hand >= reserved),
                reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
                version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                UNIQUE(product_id, size)
            );
            CREATE TABLE IF NOT EXISTS purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT NOT NULL UNIQUE REFERENCES payments(payment_id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                stock_managed INTEGER NOT NULL DEFAULT 1,
                fulfillment TEXT NOT NULL DEFAULT 'new',
                customer TEXT NOT NULL DEFAULT '{}',
                assigned_to INTEGER, version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock_reservations (
                payment_id TEXT NOT NULL REFERENCES payments(payment_id),
                sku_id INTEGER NOT NULL REFERENCES stock_items(sku_id),
                quantity INTEGER NOT NULL CHECK(quantity>0), expires_at REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('held','consumed','released')),
                PRIMARY KEY(payment_id, sku_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_due ON stock_reservations(state, expires_at);
            CREATE TABLE IF NOT EXISTS stock_movements (
                operation_id TEXT PRIMARY KEY, sku_id INTEGER NOT NULL REFERENCES stock_items(sku_id),
                actor_id INTEGER, kind TEXT NOT NULL, quantity INTEGER NOT NULL,
                on_hand_before INTEGER, on_hand_after INTEGER, reserved_before INTEGER NOT NULL,
                reserved_after INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staff_roles (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id), role TEXT NOT NULL,
                granted_by INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staff_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER NOT NULL,
                action TEXT NOT NULL, entity TEXT NOT NULL, before_data TEXT NOT NULL,
                after_data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_actions (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id),
                kind TEXT NOT NULL, data TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_actions_expiry ON chat_actions(expires_at);
            CREATE TABLE IF NOT EXISTS chat_action_results (
                token TEXT PRIMARY KEY REFERENCES chat_actions(token) ON DELETE CASCADE,
                completed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_panels (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id), chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkout_drafts (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id),
                payload TEXT NOT NULL, expires_at REAL NOT NULL, created_at TEXT NOT NULL
            );
        """)
        # Group historical lines without changing their amounts/statuses and
        # without inventing stock allocations. Staff explicitly counts FREE stock
        # after reconciling these legacy commitments (see operating guide).
        self.connection().execute("""INSERT OR IGNORE INTO purchases
            (payment_id, user_id, stock_managed, fulfillment, created_at)
            SELECT p.payment_id, p.user_id, 0,
                CASE WHEN SUM(o.status NOT IN ('cancelled','completed'))=0
                     THEN CASE WHEN SUM(o.status='completed')>0 THEN 'completed' ELSE 'cancelled' END
                     ELSE 'new' END, p.created_at
            FROM payments p JOIN orders o ON o.payment_id=p.payment_id
            JOIN users u ON u.user_id=p.user_id GROUP BY p.payment_id
            HAVING COUNT(DISTINCT o.user_id)=1 AND MIN(o.user_id)=p.user_id""")

    def sync_inventory(self, catalog) -> None:
        # Additive, no quantities taken from JSON, no updates to counted SKUs.
        with catalog.lock:
            variants = [(p["id"], size) for p in catalog.data["products"] for size in p["sizes"]]
        with self.commerce_transaction() as conn:
            conn.executemany("INSERT OR IGNORE INTO stock_items(product_id,size,updated_at) VALUES (?,?,?)",
                             [(pid, size, now_iso()) for pid, size in variants])

    def account_profile(self, user_id: int) -> dict[str, Any]:
        row = self.get_user(user_id)
        consent = bool(row and row["consent_at"])
        return {"consent": consent, **{key: (str(row[column] or "") if consent else "") for key, column in {
            "name": "customer_name", "phone": "phone", "city": "city", "address": "address",
            "entrance": "entrance", "deliver": "deliver", "size": "pref_size", "height": "height", "note": "comment"}.items()}}

    def stock_item(self, product_id: str, size: str):
        return self.connection().execute("SELECT * FROM stock_items WHERE product_id=? AND size=?", (product_id, size)).fetchone()

    def stock_by_id(self, sku_id: int):
        return self.connection().execute("SELECT * FROM stock_items WHERE sku_id=?", (sku_id,)).fetchone()

    def inventory_view(self) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in self.connection().execute("SELECT * FROM stock_items"):
            available = None if row["on_hand"] is None else row["on_hand"] - row["reserved"]
            result.setdefault(row["product_id"], {})[row["size"]] = {
                "sku_id": row["sku_id"], "available": available,
                "status": "unknown" if available is None else "available" if available > 0 else "sold_out",
            }
        return result

    def staff_role(self, user_id: int, owner_ids=frozenset()) -> str:
        if type(user_id) is not int or user_id <= 0:
            return ""
        if user_id in owner_ids:
            return "owner"
        row = self.connection().execute("SELECT role FROM staff_roles WHERE user_id=?", (user_id,)).fetchone()
        return row["role"] if row and row["role"] in {"manager", "warehouse", "support", "finance"} else ""

    def require_staff(self, actor_id: int, permission: str, owner_ids=frozenset()) -> str:
        role = self.staff_role(actor_id, owner_ids)
        if permission not in ROLE_PERMISSIONS.get(role, set()):
            raise PermissionError("Нет прав на это действие.")
        return role

    def audit_staff(self, actor_id: int, action: str, entity: str, before: Any, after: Any) -> None:
        self.connection().execute("INSERT INTO staff_audit(actor_id,action,entity,before_data,after_data,created_at) VALUES (?,?,?,?,?,?)",
                                  (actor_id, action, entity, encode(before), encode(after), now_iso()))

    def set_staff_role(self, actor_id: int, target_id: int, role: str, *, owner_ids=frozenset()) -> None:
        with self.commerce_transaction() as conn:
            self.require_staff(actor_id, "staff.write", owner_ids)
            if target_id in owner_ids or role not in {"manager", "warehouse", "support", "finance", "none"}:
                raise ValueError("Владельцы задаются только в ADMIN_IDS. Выбери роль сотрудника.")
            if not self.get_user(target_id):
                raise ValueError("Сотрудник сначала должен открыть бота и нажать /start.")
            before = self.staff_role(target_id, owner_ids)
            if role == "none":
                conn.execute("DELETE FROM staff_roles WHERE user_id=?", (target_id,))
            else:
                conn.execute("INSERT INTO staff_roles VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,granted_by=excluded.granted_by,updated_at=excluded.updated_at",
                             (target_id, role, actor_id, now_iso()))
            if role not in {"manager", "support"}:
                conn.execute("UPDATE support_tickets SET assigned_to=NULL,version=version+1,updated_at=? WHERE assigned_to=?", (now_iso(), target_id))
            if before == "finance" and role != "finance":
                self.release_finance_actor(actor_id, target_id)
            self.audit_staff(actor_id, "staff.role", str(target_id), before, role)

    def set_stock(self, actor_id: int, sku_id: int, quantity: int, expected_version: int, *, owner_ids=frozenset(), reason: str = "Инвентаризация") -> None:
        if type(quantity) is not int or not 0 <= quantity <= 1_000_000:
            raise ValueError("Нужен целый остаток от 0 до 1000000.")
        with self.commerce_transaction() as conn:
            self.require_staff(actor_id, "inventory.write", owner_ids)
            row = self.stock_by_id(sku_id)
            if not row:
                raise ValueError("Вариант не найден.")
            if row["version"] != expected_version:
                raise CartConflict("Остаток изменился. Открой вариант заново и пересчитай количество.")
            if quantity < row["reserved"]:
                raise ValueError(f"Уже зарезервировано {row['reserved']} шт. Нельзя указать меньше.")
            conn.execute("UPDATE stock_items SET on_hand=?,version=version+1,updated_at=? WHERE sku_id=?", (quantity, now_iso(), sku_id))
            operation = "count:" + secrets.token_hex(12)
            conn.execute("INSERT INTO stock_movements VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (operation, sku_id, actor_id, "count", quantity, row["on_hand"], quantity,
                          row["reserved"], row["reserved"], reason[:240], now_iso()))
            self.audit_staff(actor_id, "inventory.count", str(sku_id), {"on_hand": row["on_hand"], "reserved": row["reserved"]}, {"on_hand": quantity, "reason": reason[:240]})

    def cart(self, user_id: int) -> dict[str, Any]:
        row = self.connection().execute("SELECT revision FROM shopping_carts WHERE user_id=?", (user_id,)).fetchone()
        lines = list(self.connection().execute("SELECT * FROM cart_lines WHERE user_id=? ORDER BY line_id", (user_id,)))
        return {"user_id": user_id, "revision": row["revision"] if row else 0,
                "promotion": self.cart_promotion(user_id),
                "items": [{"line_id": r["line_id"], "product_id": r["product_id"], "size": r["size"],
                           "quantity": r["quantity"], "person": r["person"]} for r in lines]}

    @staticmethod
    def canonical_cart(items: Any, catalog=None) -> list[dict[str, Any]]:
        if not isinstance(items, list) or len(items) > MAX_CART_LINES:
            raise ValueError("В корзине может быть до 20 позиций.")
        merged: dict[tuple[str, str, str], int] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Некорректная позиция корзины.")
            pid, size, person = item.get("product_id"), item.get("size"), item.get("person", "")
            qty = item.get("quantity")
            if not all(isinstance(x, str) for x in (pid, size, person)) or not pid or not size or len(pid) > 80 or len(size) > 64 or len(person) > 32:
                raise ValueError("Некорректный вариант товара.")
            if type(qty) is not int or not 1 <= qty <= MAX_QUANTITY:
                raise ValueError("Количество должно быть целым числом от 1 до 20.")
            if catalog is not None:
                product = catalog.get(pid)
                if not product or size not in product["sizes"]:
                    raise ValueError("Товар или размер больше не доступен. Обнови корзину.")
                config = product.get("personalization") or {}
                if person and (not config or not re.fullmatch(config.get("pattern") or r".{1,32}", person)):
                    raise ValueError("Проверь персонализацию товара.")
            key = (pid, size, person)
            merged[key] = merged.get(key, 0) + qty
            if merged[key] > MAX_QUANTITY:
                raise ValueError("Не более 20 одинаковых позиций в корзине.")
        return [{"product_id": k[0], "size": k[1], "person": k[2], "quantity": v} for k, v in sorted(merged.items())]

    def replace_cart(self, user_id: int, items: list, expected_revision: int, operation_id: str, catalog) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", operation_id):
            raise ValueError("Обнови корзину: неверная версия запроса.")
        with catalog.lock, self.commerce_transaction() as conn:
            fingerprint = hashlib.sha256(encode([expected_revision, items]).encode()).hexdigest()
            previous = conn.execute("SELECT fingerprint FROM cart_mutations WHERE user_id=? AND operation_id=?", (user_id, operation_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Ключ изменения уже использован с другим содержимым.")
                return self.cart(user_id)
            current = self.cart(user_id)
            if current["revision"] != expected_revision:
                raise CartConflict("Корзина изменилась в другом окне. Обнови её перед продолжением.")
            clean = self.canonical_cart(items)
            existing_keys = {(x["product_id"], x["size"], x["person"]): x["quantity"] for x in current["items"]}
            for item in clean:
                old_qty = existing_keys.get((item["product_id"], item["size"], item["person"]), 0)
                product = catalog.get(item["product_id"])
                if (not product or item["size"] not in product["sizes"]) and item["quantity"] <= old_qty:
                    continue  # Remove stale lines one at a time without trapping the cart.
                self.canonical_cart([item], catalog)
            conn.execute("INSERT OR IGNORE INTO shopping_carts VALUES (?,0,?)", (user_id, now_iso()))
            # Keep stable line IDs for unchanged variants; old callbacks also
            # carry a version, so a removed line can never target another owner.
            desired = {(x["product_id"], x["size"], x["person"]): x for x in clean}
            for row in current["items"]:
                if (row["product_id"], row["size"], row["person"]) not in desired:
                    conn.execute("DELETE FROM cart_lines WHERE user_id=? AND line_id=?", (user_id, row["line_id"]))
            for item in clean:
                conn.execute("""INSERT INTO cart_lines(user_id,product_id,size,person,quantity) VALUES (?,?,?,?,?)
                    ON CONFLICT(user_id,product_id,size,person) DO UPDATE SET quantity=excluded.quantity""",
                    (user_id, item["product_id"], item["size"], item["person"], item["quantity"]))
            conn.execute("UPDATE shopping_carts SET revision=revision+1,updated_at=? WHERE user_id=?", (now_iso(), user_id))
            conn.execute("INSERT INTO cart_mutations VALUES (?,?,?,?)", (user_id, operation_id, fingerprint, time.time()))
            if not clean:
                conn.execute("DELETE FROM cart_promotions WHERE user_id=?", (user_id,))
            self.event(user_id, "cart_updated", {"revision": expected_revision + 1, "lines": len(clean)})
            return self.cart(user_id)

    def clear_checked_out_cart(self, user_id: int, lines: list, expected_revision: int | None) -> int | None:
        current = self.cart(user_id)
        if expected_revision is not None:
            if type(expected_revision) is not int or current["revision"] != expected_revision:
                error = CartConflict("Корзина уже изменена. Обнови её и проверь состав.")
                error.checkout_revision_stale = True
                raise error
            if self.canonical_cart(current["items"]) != self.canonical_cart(lines):
                raise CartConflict("Состав покупки не совпадает с корзиной. Обнови её.")
        elif self.canonical_cart(current["items"]) != self.canonical_cart(lines):
            return None  # An old client may order independently; do not delete another draft.
        self.connection().execute("DELETE FROM cart_lines WHERE user_id=?", (user_id,))
        self.connection().execute("DELETE FROM cart_promotions WHERE user_id=?", (user_id,))
        self.connection().execute("UPDATE shopping_carts SET revision=revision+1,updated_at=? WHERE user_id=?", (now_iso(), user_id))
        return current["revision"] + 1

    def create_chat_action(self, user_id: int, kind: str, data: dict, ttl: int = 1200) -> str:
        token = secrets.token_hex(8)
        self.connection().execute("INSERT INTO chat_actions VALUES (?,?,?,?,?)", (token, user_id, kind, encode(data), time.time() + ttl))
        return "c:a:" + token

    def chat_action(self, user_id: int, token: str) -> tuple[str, dict]:
        row = self.connection().execute("SELECT * FROM chat_actions WHERE token=? AND user_id=? AND expires_at>?", (token, user_id, time.time())).fetchone()
        if not row:
            raise ValueError("Кнопка устарела. Открой нужный раздел заново.")
        return row["kind"], json.loads(row["data"])

    def perform_staff_action(self, user_id: int, token: str, operation) -> bool:
        """Commit a privileged action and its replay marker in one transaction."""
        with self.commerce_transaction() as conn:
            self.chat_action(user_id, token)
            if conn.execute("SELECT 1 FROM chat_action_results WHERE token=?", (token,)).fetchone():
                return False
            operation()  # Only DB staff operations; never provider/Telegram I/O.
            conn.execute("INSERT INTO chat_action_results VALUES (?,?)", (token, time.time()))
            return True

    def create_checkout_draft(self, user_id: int, payload: dict) -> str:
        self.require_consent(user_id)
        token = secrets.token_hex(8)
        body = {**payload, "request_id": "chat-" + token}
        self.connection().execute("INSERT INTO checkout_drafts VALUES (?,?,?,?,?)", (token, user_id, encode(body), time.time() + 900, now_iso()))
        return token

    def checkout_draft(self, user_id: int, token: str) -> dict:
        row = self.connection().execute("SELECT * FROM checkout_drafts WHERE token=? AND user_id=?", (token, user_id)).fetchone()
        if not row:
            raise ValueError("Предпросмотр не найден. Открой корзину.")
        payload = json.loads(row["payload"])
        # A processed draft is always recoverable, even after its preview TTL.
        previous = self.connection().execute("SELECT 1 FROM checkouts WHERE user_id=? AND request_id=?", (user_id, payload["request_id"])).fetchone()
        if not previous and row["expires_at"] <= time.time():
            raise ValueError("Предпросмотр устарел. Проверь сумму и состав ещё раз.")
        return payload

    def hold_stock(self, payment_id: str, lines: list, ttl: int = 3600) -> float:
        if type(ttl) is not int or not 300 <= ttl <= 7200:
            raise ValueError("Недопустимый срок резерва.")
        # Caller owns the checkout transaction: all lines either reserve or roll back.
        self.expire_reservations()
        quantities = defaultdict(int)
        for line in lines:
            quantities[(line["product_id"], line["size"])] += line["quantity"]
        deadline = time.time() + ttl
        for (pid, size), quantity in sorted(quantities.items()):
            row = self.stock_item(pid, size)
            if not row or row["on_hand"] is None:
                raise StockError(f"Наличие размера {size} ещё не подтверждено. Напиши менеджеру или выбери «Ждать размер». Деньги не списаны.")
            if row["on_hand"] - row["reserved"] < quantity:
                raise StockError(f"Для размера {size} доступно {row['on_hand'] - row['reserved']} шт. Обнови корзину. Деньги не списаны.")
            self.connection().execute("UPDATE stock_items SET reserved=reserved+?,version=version+1,updated_at=? WHERE sku_id=?", (quantity, now_iso(), row["sku_id"]))
            self.connection().execute("INSERT INTO stock_reservations VALUES (?,?,?,?,'held')", (payment_id, row["sku_id"], quantity, deadline))
            self.connection().execute("INSERT INTO stock_movements VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"hold:{payment_id}:{row['sku_id']}", row["sku_id"], None, "hold", quantity, row["on_hand"], row["on_hand"], row["reserved"], row["reserved"] + quantity, payment_id, now_iso()))
        return deadline

    def payment_stock_ok(self, payment_id: str) -> bool:
        purchase = self.connection().execute("SELECT stock_managed FROM purchases WHERE payment_id=?", (payment_id,)).fetchone()
        if not purchase or not purchase["stock_managed"]:
            return True  # Preserve old liabilities; legacy orders have no invented reserve.
        held = list(self.connection().execute("SELECT * FROM stock_reservations WHERE payment_id=?", (payment_id,)))
        expected = defaultdict(int)
        for row in self.connection().execute("SELECT product_id,size,quantity FROM orders WHERE payment_id=?", (payment_id,)):
            sku = self.stock_item(row["product_id"], row["size"])
            if not sku:
                return False
            expected[sku["sku_id"]] += row["quantity"]
        return bool(held and all(r["state"] == "held" and r["expires_at"] > time.time() for r in held)
                    and {r["sku_id"]: r["quantity"] for r in held} == dict(expected))

    def finish_stock_reservation(self, payment_id: str, *, consumed: bool) -> None:
        # Called in the SAME transaction as payment/cancellation, never restock a
        # consumed unit on refund: physical return must be inspected/counted.
        conn = self.connection()
        for reservation in list(conn.execute("SELECT * FROM stock_reservations WHERE payment_id=? AND state='held'", (payment_id,))):
            sku = self.stock_by_id(reservation["sku_id"])
            qty = reservation["quantity"]
            after = sku["on_hand"] - qty if consumed else sku["on_hand"]
            kind = "consume" if consumed else "release"
            conn.execute("UPDATE stock_items SET on_hand=?,reserved=reserved-?,version=version+1,updated_at=? WHERE sku_id=?", (after, qty, now_iso(), sku["sku_id"]))
            conn.execute("UPDATE stock_reservations SET state=? WHERE payment_id=? AND sku_id=?", ("consumed" if consumed else "released", payment_id, sku["sku_id"]))
            conn.execute("INSERT INTO stock_movements VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{kind}:{payment_id}:{sku['sku_id']}", sku["sku_id"], None, kind, qty, sku["on_hand"], after, sku["reserved"], sku["reserved"] - qty, payment_id, now_iso()))

    def expire_reservations(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        with self.commerce_transaction() as conn:
            due = list(conn.execute("""SELECT DISTINCT p.payment_id,p.user_id,p.status,u.purchase_id FROM payments p
                JOIN stock_reservations r ON r.payment_id=p.payment_id JOIN purchases u ON u.payment_id=p.payment_id
                WHERE p.status IN ('pending','review_required') AND r.state='held' AND r.expires_at<=?""", (stamp,)))
            for row in due:
                self.finish_stock_reservation(row["payment_id"], consumed=False)
                conn.execute("UPDATE payments SET status='expired' WHERE payment_id=? AND status='pending'", (row["payment_id"],))
                conn.execute("UPDATE orders SET status='cancelled' WHERE payment_id=? AND status='awaiting_payment'", (row["payment_id"],))
                conn.execute("UPDATE purchases SET fulfillment='expired',version=version+1 WHERE payment_id=?", (row["payment_id"],))
                conn.execute("DELETE FROM payment_methods WHERE payment_id=?", (row["payment_id"],))
                self.event(row["user_id"], "reservation_expired", {"payment_id": row["payment_id"]})
                self.enqueue_message(f"reservation:{row['payment_id']}:expired", row["user_id"],
                    (f"Резерв по покупке №{row['purchase_id']:04d} закончился. Не используй старые ссылки на оплату. "
                     + ("Поступление остаётся на сверке: не плати повторно, дождись менеджера." if row["status"] == "review_required" else "Проверь наличие и собери покупку заново.")),
                    {"inline_keyboard": [[{"text": "Мои покупки", "callback_data": "c:orders"}]]})
            return len(due)

    def purchase_view(self, purchase_id: int, *, user_id: int | None = None, actor_id: int | None = None, owner_ids=frozenset()) -> dict:
        if actor_id is not None:
            role = self.require_staff(actor_id, "orders.read", owner_ids)
        elif user_id is None:
            raise PermissionError("Нужен владелец покупки.")
        else:
            role = "customer"
        row = self.connection().execute("SELECT * FROM purchases WHERE purchase_id=?", (purchase_id,)).fetchone()
        if not row or (actor_id is None and row["user_id"] != user_id):
            raise ValueError("Покупка не найдена.")
        payment = self.get_payment(row["payment_id"])
        orders = self.orders_for_payment(row["payment_id"])
        if not orders:
            raise ValueError("У покупки нет позиций. Нужна сверка.")
        pstatus = payment["status"]
        status = "awaiting_payment" if pstatus == "pending" else pstatus
        if pstatus == "paid":
            status = {"new": "paid", "packing": "confirmed", "ready": "confirmed", "completed": "completed", "cancelled": "cancelled"}.get(row["fulfillment"], "paid")
        labels = {"awaiting_payment": "ждёт оплаты", "paid": "оплачена · ждёт сборки", "confirmed": FULFILLMENT_LABELS.get(row["fulfillment"], "подтверждена"), "completed": "завершена", "cancelled": "отменена", "expired": "резерв истёк", "review_required": "поступление на сверке", "refund_required": "требуется возврат"}
        reservations = list(self.connection().execute("SELECT expires_at,state FROM stock_reservations WHERE payment_id=?", (row["payment_id"],)))
        attention = self.payment_attention(row["payment_id"])
        dto = {"purchase_id": purchase_id, "number": f"{purchase_id:04d}", "id": orders[0]["id"],
               "payment_id": row["payment_id"], "status": status, "status_label": labels.get(status, status),
               "payment_status": pstatus, "payment_attention": attention, "fulfillment": row["fulfillment"],
               "can_pay": self.payment_is_payable(row["payment_id"]), "can_cancel": pstatus == "pending" and row["fulfillment"] == "new",
               "created_at": row["created_at"], "amount_rub": payment["amount_rub"], "amount_label": f"{payment['amount_rub']:,} ₽".replace(",", " "), "amount_stars": payment["amount_stars"],
               "quantity": sum(o["quantity"] for o in orders), "size": "", "product_name": " + ".join(o["product_name"] for o in orders),
               "lines": [{"name": o["product_name"], "size": o["size"], "quantity": o["quantity"], "amount_rub": o["amount_rub"], "person": o["person"]} for o in orders],
               "reserved_until": min((r["expires_at"] for r in reservations if r["state"] == "held"), default=None),
               "legacy": not row["stock_managed"], "operations_managed": bool(row["operations_managed"]), "assigned_to": row["assigned_to"], "version": row["version"]}
        dto["promotion"] = self.purchase_promotion(row["payment_id"])
        if role in {"customer", "owner", "manager"}:
            customer = json.loads(row["customer"])
            if not customer and role != "customer":
                customer = {"phone": orders[0]["phone"], "note": orders[0]["note"]}
            dto["customer"] = customer
        return dto

    def purchases_for_user(self, user_id: int, offset: int = 0) -> list[dict]:
        ids = list(self.connection().execute("SELECT purchase_id FROM purchases WHERE user_id=? ORDER BY purchase_id DESC LIMIT 20 OFFSET ?", (user_id, max(0, offset))))
        return [self.purchase_view(r[0], user_id=user_id) for r in ids]

    def staff_purchases(self, actor_id: int, bucket: str = "all", offset: int = 0, *, owner_ids=frozenset()) -> list[dict]:
        self.require_staff(actor_id, "orders.read", owner_ids)
        conditions = {"all": "1=1", "pay": "p.status='pending'", "work": "p.status='paid' AND u.fulfillment NOT IN ('completed','cancelled')", "review": "p.status IN ('review_required','refund_required') OR EXISTS(SELECT 1 FROM payment_receipts r WHERE r.payment_id=p.payment_id AND (r.status IN ('review_required','refund_required') OR EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id)))", "done": "u.fulfillment IN ('completed','cancelled','expired')"}
        if bucket not in conditions:
            raise ValueError("Неизвестная очередь.")
        rows = self.connection().execute(f"SELECT u.purchase_id FROM purchases u JOIN payments p ON p.payment_id=u.payment_id WHERE {conditions[bucket]} ORDER BY u.purchase_id DESC LIMIT 8 OFFSET ?", (max(0, offset),))
        return [self.purchase_view(r[0], actor_id=actor_id, owner_ids=owner_ids) for r in list(rows)]

    def claim_purchase(self, actor_id: int, purchase_id: int, *, owner_ids=frozenset()) -> None:
        with self.commerce_transaction() as conn:
            self.require_staff(actor_id, "orders.claim", owner_ids)
            row = conn.execute("SELECT assigned_to FROM purchases WHERE purchase_id=?", (purchase_id,)).fetchone()
            if not row:
                raise ValueError("Покупка не найдена.")
            if row[0] == actor_id:
                return
            if row[0] is not None:
                raise ValueError("Другой сотрудник уже взял покупку в работу.")
            conn.execute("UPDATE purchases SET assigned_to=?,version=version+1 WHERE purchase_id=?", (actor_id, purchase_id))
            self.audit_staff(actor_id, "purchase.claim", str(purchase_id), None, actor_id)

    def release_purchase(self, actor_id: int, purchase_id: int, expected_version: int, *, owner_ids=frozenset()) -> None:
        with self.commerce_transaction() as conn:
            role = self.require_staff(actor_id, "orders.claim", owner_ids)
            row = conn.execute("SELECT * FROM purchases WHERE purchase_id=?", (purchase_id,)).fetchone()
            if not row:
                raise ValueError("Покупка не найдена.")
            if row["assigned_to"] is None:
                return
            if role != "owner" and row["assigned_to"] != actor_id:
                raise PermissionError("Передать чужую покупку может только владелец.")
            if row["version"] != expected_version:
                raise CartConflict("Карточка изменилась. Обнови её перед передачей.")
            conn.execute("UPDATE purchases SET assigned_to=NULL,version=version+1 WHERE purchase_id=?", (purchase_id,))
            self.audit_staff(actor_id, "purchase.release", str(purchase_id), row["assigned_to"], None)

    def advance_purchase(self, actor_id: int, purchase_id: int, target: str, expected_version: int, *, owner_ids=frozenset()) -> None:
        if target not in {"packing", "ready", "completed"}:
            raise ValueError("Неизвестный этап.")
        with self.commerce_transaction() as conn:
            role = self.staff_role(actor_id, owner_ids)
            self.require_staff(actor_id, "orders.pack" if role == "warehouse" else "orders.write", owner_ids)
            row = conn.execute("SELECT * FROM purchases WHERE purchase_id=?", (purchase_id,)).fetchone()
            if not row:
                raise ValueError("Покупка не найдена.")
            if row["fulfillment"] == target:
                return
            if row["version"] != expected_version:
                raise CartConflict("Покупка уже изменена. Обнови карточку.")
            if row["assigned_to"] != actor_id and role != "owner":
                raise PermissionError("Сначала возьми покупку в работу.")
            payment = self.get_payment(row["payment_id"])
            if payment["status"] != "paid" or self.payment_attention(row["payment_id"]):
                raise ValueError("Сначала нужна подтверждённая оплата без финансовой сверки.")
            if target == "completed":
                self.require_operations_completion(row)
            transitions = {"new": "packing", "packing": "ready", "ready": "completed"}
            if transitions.get(row["fulfillment"]) != target:
                raise ValueError("Сначала заверши предыдущий этап.")
            conn.execute("UPDATE purchases SET fulfillment=?,version=version+1 WHERE purchase_id=?", (target, purchase_id))
            conn.execute("UPDATE orders SET status=? WHERE payment_id=?", ("completed" if target == "completed" else "confirmed", row["payment_id"]))
            self.audit_staff(actor_id, "purchase.stage", str(purchase_id), row["fulfillment"], target)
            self.enqueue_message(f"purchase:{purchase_id}:{target}", row["user_id"],
                f"Покупка №{purchase_id:04d}: {FULFILLMENT_LABELS[target]}. Подробности — в карточке покупки.",
                {"inline_keyboard": [[{"text": "Открыть покупку", "callback_data": f"c:purchase:{purchase_id}"}]]})

    def prune_commerce(self) -> None:
        stamp = time.time()
        with self.commerce_transaction() as conn:
            conn.execute("DELETE FROM chat_actions WHERE expires_at<?", (stamp - 86400,))
            conn.execute("DELETE FROM cart_mutations WHERE created_at<?", (stamp - 7 * 86400,))
            # Contact snapshots in abandoned previews are not kept indefinitely.
            conn.execute("DELETE FROM checkout_drafts WHERE expires_at<?", (stamp - 7 * 86400,))
