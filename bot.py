#!/usr/bin/env python3
"""Telegram storefront bot for a small clothing brand.

Thematic build: bold brand voice, hard contextual CTAs, channel-first funnel,
referral/giveaway viral loop, size waitlist, segmented broadcasts.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from payments import (
    PriceError,
    create_crypto_invoice,
    create_lava_invoice,
    format_rub,
    parse_price_rub,
    parse_price_strict,
    stars_amount,
    verify_crypto_webhook,
    verify_lava_webhook,
)

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("brand_bot")
STOP_EVENT = threading.Event()


class RateLimiter:
    """Скользящее окно на память процесса.

    Хватает для одного инстанса за реверс-прокси: защищает от заваливания
    заявками и от перебора отмен по украденному initData. Для нескольких
    инстансов счётчик нужно вынести в общее хранилище.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        stamp = now if now is not None else time.monotonic()
        edge = stamp - self.window
        with self.lock:
            recent = [hit for hit in self.hits.get(key, ()) if hit > edge]
            allowed = len(recent) < self.limit
            if allowed:
                recent.append(stamp)
            self.hits[key] = recent
            if len(self.hits) > 4096:
                # Не даём словарю расти бесконечно от разовых посетителей.
                for stale_key in [k for k, v in self.hits.items() if not v]:
                    self.hits.pop(stale_key, None)
            return allowed


# Заявка — дорогая операция (счёт у провайдера, сообщения). Чтение своих
# заявок опрашивается витриной раз в 4 секунды, поэтому лимит выше.
CHECKOUT_LIMITER = RateLimiter(limit=8, window_seconds=60)
READ_LIMITER = RateLimiter(limit=60, window_seconds=60)
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
REF_RE = re.compile(r"^ref(\d{3,15})$")
WELCOME_PHOTO = BASE_DIR / "miniapp" / "assets" / "welcome.jpg"
BOT_SHORT_DESCRIPTION = "Сила и честь. Одежда. Закрытый выпуск."
BOT_DESCRIPTION = (
    "Закрытая витрина ВОРОЖБИТОВ. Малые тиражи, честный крой. "
    "Смотри выпуск, бери размер, оплачивай сразу: карта, крипта или звёзды."
)

# Attention markers used across the funnel. Kept in one place so the tone stays
# consistent and can be softened without hunting through the code.
FIRE = "\U0001F525"
BOLT = "⚡️"
BOX = "\U0001F4E6"
POINT = "\U0001F449"
CROWN = "\U0001F451"
SIREN = "\U0001F6A8"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_phone(value: str) -> str | None:
    phone = re.sub(r"[^0-9+]", "", value.strip())
    if phone.startswith("8") and len(re.sub(r"\D", "", phone)) == 11:
        phone = "+7" + phone[1:]
    elif not phone.startswith("+"):
        phone = "+" + phone
    return phone if PHONE_RE.fullmatch(phone) else None


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_catalog_exists(path: Path, seed_path: Path) -> bool:
    """Create a mutable catalog from the packaged seed on first start.

    Returns True when a new catalog was created. O_EXCL keeps parallel starts
    from overwriting a catalog another process has already initialized.
    """
    if path.exists():
        return False
    if not seed_path.exists():
        raise FileNotFoundError(f"Catalog not found: {path}; seed not found: {seed_path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = seed_path.read_bytes()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    return True


def esc(value: Any) -> str:
    return html.escape(str(value))


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", " ": "-", "_": "-",
}


def slugify(value: str, fallback: str = "item") -> str:
    """Build a callback-safe slug from a product name."""
    raw = value.strip().lower()
    slug = "".join(TRANSLIT.get(char, char) for char in raw)
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")[:32]
    return slug or fallback


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    channel_url: str
    webapp_url: str
    manager_chat_id: int | None
    brand_name: str
    support_username: str
    database_path: Path
    catalog_path: Path
    health_port: int
    giveaway_min_invites: int
    privacy_url: str
    lava_shop_id: str = ""
    lava_secret_key: str = ""
    lava_hook_key: str = ""
    crypto_pay_token: str = ""
    stars_enabled: bool = True
    stars_rub_per_star: float = 2.0

    def public_origin(self) -> str:
        parsed = urllib.parse.urlparse(self.webapp_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return ""

    def lava_ready(self) -> bool:
        return bool(self.lava_shop_id and self.lava_secret_key)

    def crypto_ready(self) -> bool:
        return bool(self.crypto_pay_token)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        # Никаких боевых ID по умолчанию: без .env бот не должен молча раздавать
        # админские права и слать чужие телефоны с адресами в захардкоженный чат.
        admin_ids = frozenset(
            int(item.strip())
            for item in os.getenv("ADMIN_IDS", "").split(",")
            if item.strip().isdigit()
        )
        manager_raw = os.getenv("MANAGER_CHAT_ID", "").strip()
        try:
            stars_rate = float(os.getenv("STARS_RUB_PER_STAR", "2") or "2")
        except ValueError:
            stars_rate = 2.0
        stars_flag = os.getenv("STARS_ENABLED", "1").strip().lower()
        return cls(
            token=token,
            admin_ids=admin_ids,
            channel_url=os.getenv("CHANNEL_URL", "https://t.me/").strip(),
            webapp_url=os.getenv("WEBAPP_URL", "").strip(),
            manager_chat_id=int(manager_raw) if manager_raw.lstrip("-").isdigit() else None,
            brand_name=os.getenv("BRAND_NAME", "ВОРОЖБИТОВ").strip() or "ВОРОЖБИТОВ",
            support_username=os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@"),
            database_path=BASE_DIR / os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
            catalog_path=BASE_DIR / os.getenv("CATALOG_PATH", "data/catalog.json"),
            health_port=int(os.getenv("PORT", "8080")),
            giveaway_min_invites=int(os.getenv("GIVEAWAY_MIN_INVITES", "3")),
            privacy_url=os.getenv("PRIVACY_URL", "").strip(),
            lava_shop_id=os.getenv("LAVA_SHOP_ID", "").strip(),
            lava_secret_key=os.getenv("LAVA_SECRET_KEY", "").strip(),
            lava_hook_key=(os.getenv("LAVA_HOOK_KEY") or os.getenv("LAVA_ADDITIONAL_KEY") or "").strip(),
            crypto_pay_token=os.getenv("CRYPTO_PAY_TOKEN", "").strip(),
            stars_enabled=stars_flag not in {"0", "false", "no", "off"},
            stars_rub_per_star=stars_rate if stars_rate > 0 else 2.0,
        )


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.products_by_id: dict[str, dict[str, Any]] = {}
        # /reload и мастер /add подменяют каталог из потока опроса Telegram,
        # пока потоки HTTP отдают /api/catalog и проверяют заявки.
        self.lock = threading.RLock()
        self.reload()

    def reload(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw.get("categories"), list) or not isinstance(raw.get("products"), list):
            raise ValueError("catalog.json must contain categories and products arrays")
        category_ids: set[str] = set()
        for category in raw["categories"]:
            if not isinstance(category, dict) or not all(isinstance(category.get(key), str) for key in ("id", "name")):
                raise ValueError(f"Invalid category: {category}")
            category_id = category["id"]
            if not category_id or ":" in category_id or len(category_id.encode("utf-8")) > 48:
                raise ValueError(f"Category id is not callback-safe: {category_id}")
            if category_id in category_ids:
                raise ValueError(f"Duplicate category id: {category_id}")
            category_ids.add(category_id)
        products: dict[str, dict[str, Any]] = {}
        for product in raw["products"]:
            required = {"id", "category", "name", "price", "sizes", "description"}
            if not isinstance(product, dict) or not required.issubset(product):
                raise ValueError(f"Product is missing fields: {product}")
            product_id = str(product["id"])
            if not product_id or ":" in product_id or len(product_id.encode("utf-8")) > 40:
                raise ValueError(f"Product id is not callback-safe: {product_id}")
            if product_id in products:
                raise ValueError(f"Duplicate product id: {product_id}")
            if product["category"] not in category_ids:
                raise ValueError(f"Unknown category for product {product_id}: {product['category']}")
            if not all(isinstance(product.get(key), str) for key in ("name", "price", "description")):
                raise ValueError(f"Product text fields must be strings: {product_id}")
            if not isinstance(product["sizes"], list) or not product["sizes"]:
                raise ValueError(f"Product sizes must be a non-empty array: {product_id}")
            for size in product["sizes"]:
                callback = f"size:{product_id}:{size}"
                if ":" in str(size) or len(callback.encode("utf-8")) > 64:
                    raise ValueError(f"Size is not callback-safe for {product_id}: {size}")
            photo_url = str(product.get("photo_url", ""))
            if photo_url and not photo_url.startswith(("https://", "http://")):
                raise ValueError(f"photo_url must be HTTP(S) for {product_id}")
            try:
                parse_price_strict(product["price"])
            except PriceError:
                # Не роняем каталог целиком: товар покажем, но счёт по нему не
                # выставится (parse_price_rub вернёт 0), поэтому шумим в лог.
                LOG.error(
                    "Цена товара %s не разбирается: %r — оплата по нему работать не будет",
                    product_id,
                    product["price"],
                )
            products[product_id] = product
        raw.setdefault("lookbook", [])
        # Обе структуры собраны целиком — подменяем их разом, чтобы читатель
        # никогда не увидел новый data со старым индексом товаров.
        with self.lock:
            self.data = raw
            self.products_by_id = products

    @property
    def categories(self) -> list[dict[str, str]]:
        return self.data["categories"]

    @property
    def lookbook(self) -> list[dict[str, str]]:
        return [item for item in self.data.get("lookbook", []) if str(item.get("photo_url", "")).startswith("http")]

    def products_for_category(self, category: str) -> list[dict[str, Any]]:
        return [p for p in self.data["products"] if p["category"] == category and p.get("active", True)]

    def get(self, product_id: str) -> dict[str, Any] | None:
        product = self.products_by_id.get(product_id)
        return product if product and product.get("active", True) else None

    def get_any(self, product_id: str) -> dict[str, Any] | None:
        return self.products_by_id.get(product_id)

    def save(self) -> None:
        with self.lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def unique_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        index = 2
        while candidate in self.products_by_id:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def add_product(self, product: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            product = dict(product)
            product["id"] = self.unique_id(str(product["name"]))
            product.setdefault("active", True)
            product.setdefault("photo_url", "")
            self.data["products"].append(product)
            self.save()
            self.reload()
            return product

    def set_active(self, product_id: str, active: bool) -> bool:
        with self.lock:
            for product in self.data["products"]:
                if str(product.get("id")) == product_id:
                    product["active"] = active
                    self.save()
                    self.reload()
                    return True
            return False


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.local = threading.local()
        self._initialize()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self.local.conn = conn
        return conn

    def close_current(self) -> None:
        """Закрыть соединение текущего потока.

        ``ThreadingHTTPServer`` создаёт поток на каждый запрос, а соединение
        живёт в ``threading.local``. Без явного закрытия дескрипторы копятся до
        ближайшей сборки мусора: 300 коротких потоков давали 254 открытых fd.
        Долгоживущие потоки (опрос Telegram) свои соединения не трогают.
        """
        conn = getattr(self.local, "conn", None)
        if conn is None:
            return
        self.local.conn = None
        try:
            conn.close()
        except sqlite3.Error:
            LOG.debug("Не удалось закрыть соединение SQLite", exc_info=True)

    def _initialize(self) -> None:
        conn = self.connection()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                phone TEXT,
                source TEXT,
                interest TEXT,
                referrer_id INTEGER,
                invited_count INTEGER NOT NULL DEFAULT 0,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS states (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                size TEXT NOT NULL,
                phone TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                size TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, product_id, size)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount_rub INTEGER NOT NULL DEFAULT 0,
                amount_stars INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                method TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
            CREATE INDEX IF NOT EXISTS idx_users_interest ON users(interest);
            """
        )
        order_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        if "request_id" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN request_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_request_id ON orders(request_id)")
        if "quantity" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        if "note" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "payment_id" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_id TEXT NOT NULL DEFAULT ''")
        if "amount_rub" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN amount_rub INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id)")
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "referrer_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        if "invited_count" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN invited_count INTEGER NOT NULL DEFAULT 0")
        if "consent_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN consent_at TEXT")
        if "city" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN city TEXT")
        if "address" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN address TEXT")
        if "entrance" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN entrance TEXT")
        if "deliver" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN deliver TEXT")
        if "pref_size" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN pref_size TEXT")
        if "height" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN height TEXT")
        if "comment" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN comment TEXT")
        if "profile_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN profile_at TEXT")

    def upsert_user(self, telegram_user: dict[str, Any], source: str | None = None) -> tuple[bool, int | None]:
        """Create or refresh a user. Returns (is_new, referrer_id).

        A referral is attributed only once: the first ref-source wins, so a
        repeated /start cannot inflate somebody's invite counter.
        """
        now = utc_now()
        user_id = int(telegram_user["id"])
        conn = self.connection()
        existing = conn.execute("SELECT user_id, referrer_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        is_new = existing is None
        referrer: int | None = None
        if source:
            match = REF_RE.match(source.strip())
            if match:
                candidate = int(match.group(1))
                if candidate != user_id:
                    referrer = candidate
        if not is_new and existing["referrer_id"] is not None:
            referrer = None
        if is_new:
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name, last_name, source, referrer_id, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    telegram_user.get("username"),
                    telegram_user.get("first_name", ""),
                    telegram_user.get("last_name", ""),
                    source,
                    referrer,
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE users SET
                    username=?,
                    first_name=?,
                    last_name=?,
                    source=COALESCE(users.source, ?),
                    referrer_id=COALESCE(users.referrer_id, ?),
                    last_seen=?,
                    is_blocked=0
                WHERE user_id=?
                """,
                (
                    telegram_user.get("username"),
                    telegram_user.get("first_name", ""),
                    telegram_user.get("last_name", ""),
                    source,
                    referrer,
                    now,
                    user_id,
                ),
            )
        if referrer:
            conn.execute("UPDATE users SET invited_count=invited_count+1 WHERE user_id=?", (referrer,))
        return is_new, referrer

    def set_phone(self, user_id: int, phone: str) -> None:
        self.connection().execute("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))

    def set_interest(self, user_id: int, interest: str) -> None:
        self.connection().execute("UPDATE users SET interest=? WHERE user_id=?", (interest, user_id))

    def set_consent(self, user_id: int) -> None:
        self.connection().execute("UPDATE users SET consent_at=? WHERE user_id=?", (utc_now(), user_id))

    def set_profile(self, user_id: int, profile: dict[str, Any]) -> None:
        self.connection().execute(
            """
            UPDATE users SET
                first_name=COALESCE(NULLIF(?, ''), first_name),
                phone=COALESCE(NULLIF(?, ''), phone),
                city=?,
                address=?,
                entrance=?,
                deliver=?,
                pref_size=?,
                height=?,
                comment=?,
                profile_at=?
            WHERE user_id=?
            """,
            (
                str(profile.get("name") or "")[:80],
                str(profile.get("phone") or "")[:32],
                str(profile.get("city") or "")[:80],
                str(profile.get("address") or "")[:200],
                str(profile.get("entrance") or "")[:80],
                str(profile.get("deliver") or "")[:40],
                str(profile.get("size") or "")[:16],
                str(profile.get("height") or "")[:8],
                str(profile.get("note") or "")[:200],
                utc_now(),
                user_id,
            ),
        )

    def has_consent(self, user_id: int) -> bool:
        row = self.connection().execute("SELECT consent_at FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["consent_at"])

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.connection().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def set_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self.connection().execute(
            """
            INSERT INTO states(user_id, state, data, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data, updated_at=excluded.updated_at
            """,
            (user_id, state, compact_json(data or {}), utc_now()),
        )

    def get_state(self, user_id: int) -> tuple[str, dict[str, Any]] | None:
        row = self.connection().execute("SELECT state, data FROM states WHERE user_id=?", (user_id,)).fetchone()
        return (row["state"], json.loads(row["data"])) if row else None

    def clear_state(self, user_id: int) -> None:
        self.connection().execute("DELETE FROM states WHERE user_id=?", (user_id,))

    def create_order(
        self,
        request_id: str,
        user_id: int,
        product: dict[str, Any],
        size: str,
        phone: str,
        quantity: int = 1,
        note: str = "",
        status: str = "new",
        payment_id: str = "",
        amount_rub: int = 0,
    ) -> tuple[int, bool]:
        conn = self.connection()
        now = utc_now()
        safe_note = str(note or "")[:400]
        allowed_status = {"new", "awaiting_payment", "paid", "confirmed", "completed", "cancelled"}
        safe_status = status if status in allowed_status else "new"
        safe_payment = str(payment_id or "")[:32]
        try:
            safe_amount = max(0, int(amount_rub))
        except (TypeError, ValueError):
            safe_amount = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id FROM orders WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return int(existing["id"]), False
            try:
                safe_quantity = max(1, min(int(quantity), 20))
            except (TypeError, ValueError):
                safe_quantity = 1
            cursor = conn.execute(
                """
                INSERT INTO orders(
                    request_id, user_id, product_id, product_name, size, phone,
                    quantity, note, status, payment_id, amount_rub, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    str(product["id"]),
                    product["name"],
                    size,
                    phone,
                    safe_quantity,
                    safe_note,
                    safe_status,
                    safe_payment,
                    safe_amount,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid)
            conn.execute("DELETE FROM states WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                (user_id, "order_created", compact_json({"order_id": order_id, "product_id": product["id"]}), now),
            )
            conn.execute("COMMIT")
            return order_id, True
        except sqlite3.IntegrityError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            existing = conn.execute("SELECT id FROM orders WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return int(existing["id"]), False
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def create_payment(self, payment_id: str, user_id: int, amount_rub: int, amount_stars: int = 0) -> None:
        self.connection().execute(
            """
            INSERT INTO payments(payment_id, user_id, amount_rub, amount_stars, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (payment_id, user_id, max(0, int(amount_rub)), max(0, int(amount_stars)), utc_now()),
        )

    def get_payment(self, payment_id: str) -> sqlite3.Row | None:
        return self.connection().execute(
            "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone()

    def orders_for_payment(self, payment_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection().execute(
                "SELECT * FROM orders WHERE payment_id=? ORDER BY id", (payment_id,)
            )
        )

    def mark_payment_paid(self, payment_id: str, method: str = "", provider_id: str = "") -> bool:
        """Idempotent: pending → paid, matching orders awaiting_payment → paid."""
        conn = self.connection()
        now = utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            if not row:
                conn.execute("COMMIT")
                return False
            already = str(row["status"]) == "paid"
            if not already:
                conn.execute(
                    """
                    UPDATE payments
                    SET status='paid', method=?, provider_id=?, paid_at=?
                    WHERE payment_id=?
                    """,
                    (str(method or row["method"] or "")[:32], str(provider_id or "")[:80], now, payment_id),
                )
            for order in conn.execute(
                "SELECT id, user_id, status FROM orders WHERE payment_id=?", (payment_id,)
            ):
                if str(order["status"]) != "awaiting_payment":
                    continue
                conn.execute("UPDATE orders SET status='paid' WHERE id=?", (order["id"],))
                conn.execute(
                    "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        order["user_id"],
                        "order_status_changed",
                        compact_json({"order_id": int(order["id"]), "status": "paid"}),
                        now,
                    ),
                )
            if not already:
                conn.execute(
                    "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                    (row["user_id"], "payment_paid", compact_json({"payment_id": payment_id, "method": method}), now),
                )
            conn.execute("COMMIT")
            return not already
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def add_to_waitlist(self, user_id: int, product: dict[str, Any], size: str) -> bool:
        conn = self.connection()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO waitlist(user_id, product_id, product_name, size, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, str(product["id"]), product["name"], size, utc_now()),
        )
        return cursor.rowcount > 0

    def waitlist_rows(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.connection().execute(
                """
                SELECT w.*, u.username, u.first_name, u.phone
                FROM waitlist w JOIN users u ON u.user_id=w.user_id
                ORDER BY w.id DESC LIMIT ?
                """,
                (limit,),
            )
        )

    def waitlist_user_ids(self, product_id: str, size: str) -> list[int]:
        return [
            row[0]
            for row in self.connection().execute(
                "SELECT user_id FROM waitlist WHERE product_id=? AND size=?", (product_id, size)
            )
        ]

    def event(self, user_id: int | None, event: str, payload: dict[str, Any] | None = None) -> None:
        self.connection().execute(
            "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event, compact_json(payload or {}), utc_now()),
        )

    def stats(self) -> dict[str, int]:
        conn = self.connection()
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL").fetchone()[0],
            "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "orders_today": conn.execute(
                "SELECT COUNT(*) FROM orders WHERE substr(created_at, 1, 10)=?", (utc_now()[:10],)
            ).fetchone()[0],
            "referrals": conn.execute("SELECT COALESCE(SUM(invited_count), 0) FROM users").fetchone()[0],
            "waitlist": conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0],
            "consents": conn.execute("SELECT COUNT(*) FROM users WHERE consent_at IS NOT NULL").fetchone()[0],
        }

    def recent_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.connection().execute(
                """
                SELECT o.*, u.username, u.first_name
                FROM orders o JOIN users u ON u.user_id=o.user_id
                ORDER BY o.id DESC LIMIT ?
                """,
                (limit,),
            )
        )

    def orders_for_user(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.connection().execute(
                "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, max(1, min(int(limit), 30))),
            )
        )

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.connection().execute(
            """
            SELECT o.*, u.username, u.first_name
            FROM orders o JOIN users u ON u.user_id=o.user_id
            WHERE o.id=?
            """,
            (order_id,),
        ).fetchone()

    def set_order_status(self, order_id: int, status: str) -> tuple[sqlite3.Row | None, bool]:
        allowed = {"new", "awaiting_payment", "paid", "confirmed", "completed", "cancelled"}
        transitions = {
            "new": {"confirmed", "cancelled", "awaiting_payment"},
            "awaiting_payment": {"paid", "cancelled"},
            "paid": {"confirmed", "cancelled"},
            "confirmed": {"completed", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        if status not in allowed:
            raise ValueError(f"Unsupported order status: {status}")
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None, False
            current = str(row["status"])
            if current == status:
                conn.execute("COMMIT")
                return row, False
            if status not in transitions.get(current, set()):
                conn.execute("COMMIT")
                raise ValueError(f"Invalid order transition: {current} -> {status}")
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
            conn.execute(
                "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                (row["user_id"], "order_status_changed", compact_json({"order_id": order_id, "status": status}), utc_now()),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return self.get_order(order_id), True

    def top_referrers(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.connection().execute(
                """
                SELECT user_id, username, first_name, invited_count
                FROM users WHERE invited_count > 0
                ORDER BY invited_count DESC, user_id LIMIT ?
                """,
                (limit,),
            )
        )

    def broadcast_audience(self, segment: str) -> list[int]:
        conn = self.connection()
        if segment == "consent":
            rows = conn.execute("SELECT user_id FROM users WHERE is_blocked=0 AND consent_at IS NOT NULL")
        elif segment == "contacts":
            rows = conn.execute("SELECT user_id FROM users WHERE is_blocked=0 AND phone IS NOT NULL")
        elif segment == "buyers":
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM orders WHERE user_id IN (SELECT user_id FROM users WHERE is_blocked=0)"
            )
        elif segment == "fresh":
            rows = conn.execute(
                "SELECT user_id FROM users WHERE is_blocked=0 AND phone IS NULL ORDER BY created_at DESC LIMIT 500"
            )
        elif segment.startswith("interest:"):
            interest = segment.split(":", 1)[1]
            rows = conn.execute("SELECT user_id FROM users WHERE is_blocked=0 AND interest=?", (interest,))
        else:
            rows = conn.execute("SELECT user_id FROM users WHERE is_blocked=0")
        return [row[0] for row in rows]

    def active_user_ids(self) -> list[int]:
        return [row[0] for row in self.connection().execute("SELECT user_id FROM users WHERE is_blocked=0")]

    def giveaway_pool(self, min_invites: int) -> list[int]:
        return [
            row[0]
            for row in self.connection().execute(
                "SELECT user_id FROM users WHERE is_blocked=0 AND invited_count >= ?", (min_invites,)
            )
        ]

    def mark_blocked(self, user_id: int) -> None:
        self.connection().execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))

    def export_users_csv(self) -> bytes:
        def safe_cell(value: Any) -> Any:
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "user_id",
                "username",
                "first_name",
                "last_name",
                "phone",
                "interest",
                "source",
                "referrer_id",
                "invited_count",
                "consent_at",
                "created_at",
                "last_seen",
            ]
        )
        for row in self.connection().execute(
            """
            SELECT user_id, username, first_name, last_name, phone, interest, source,
                   referrer_id, invited_count, consent_at, created_at, last_seen
            FROM users ORDER BY created_at
            """
        ):
            writer.writerow([safe_cell(value) for value in tuple(row)])
        return output.getvalue().encode("utf-8-sig")


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 70) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(
                self.base_url + method,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 3:
                    retry_after = 1
                    try:
                        retry_after = int(json.loads(details).get("parameters", {}).get("retry_after", 1))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        retry_after = 1
                    time.sleep(min(max(retry_after, 1), 30))
                    continue
                raise RuntimeError(f"Telegram HTTP {exc.code}: {details}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                time.sleep(1 + attempt)
                continue
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return result.get("result")
        raise RuntimeError(f"Telegram request failed: {last_error}")

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> Any:
        if len(text) > 4000:
            text = text[:3990] + "…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def send_photo(self, chat_id: int, photo: str, caption: str, reply_markup: dict[str, Any] | None = None) -> Any:
        if len(caption) > 1024:
            caption = caption[:1010] + "…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendPhoto", payload)

    def send_photo_file(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        """Upload a local JPEG/PNG as sendPhoto — works even if WEBAPP_URL is unreachable."""
        if len(caption) > 1024:
            caption = caption[:1010] + "…"
        content = path.read_bytes()
        filename = path.name or "photo.jpg"
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        boundary = "----VorozhbitovPhoto7MA4YWxk"
        fields: dict[str, str] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            self.base_url + "sendPhoto",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result.get("result")

    def send_media_group(self, chat_id: int, photos: list[str], caption: str = "") -> Any:
        media = []
        for index, photo in enumerate(photos[:10]):
            item: dict[str, Any] = {"type": "photo", "media": photo}
            if index == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        return self.call("sendMediaGroup", {"chat_id": chat_id, "media": media})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def answer_pre_checkout(self, query_id: str, ok: bool = True, error_message: str = "") -> Any:
        payload: dict[str, Any] = {"pre_checkout_query_id": query_id, "ok": bool(ok)}
        if not ok and error_message:
            payload["error_message"] = error_message[:200]
        return self.call("answerPreCheckoutQuery", payload)

    def create_invoice_link(self, payload: dict[str, Any]) -> str:
        return str(self.call("createInvoiceLink", payload) or "")

    def send_invoice(self, chat_id: int, payload: dict[str, Any]) -> Any:
        body = {"chat_id": chat_id, **payload}
        return self.call("sendInvoice", body)

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> Any:
        boundary = "----WorkBuddyBoundary7MA4YWxk"
        fields = {"chat_id": str(chat_id), "caption": caption}
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: text/csv\r\n\r\n",
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            self.base_url + "sendDocument",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result.get("result")


def inline_keyboard(rows: Iterable[Iterable[tuple[str, str]]]) -> dict[str, Any]:
    keyboard = []
    for row in rows:
        buttons = []
        for label, target in row:
            if target.startswith("webapp:"):
                buttons.append({"text": label, "web_app": {"url": target.removeprefix("webapp:")}})
                continue
            key = "url" if target.startswith(("https://", "http://", "tg://")) else "callback_data"
            buttons.append({"text": label, key: target})
        keyboard.append(buttons)
    return {"inline_keyboard": keyboard}


def contact_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": "Отправить номер", "request_contact": True}], [{"text": "Отмена"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard() -> dict[str, Any]:
    return {"remove_keyboard": True}


SEGMENT_LABELS = {
    "consent": "Дали согласие",
    "contacts": "С номером",
    "buyers": "Уже оставляли заявку",
    "fresh": "Новые без номера",
    "all": "Вся база",
}

ORDER_STATUS_LABELS = {
    "new": "новая",
    "awaiting_payment": "ждёт оплаты",
    "paid": "оплачена",
    "confirmed": "подтверждена",
    "completed": "завершена",
    "cancelled": "отменена",
}

ORDER_STATUS_MESSAGES = {
    "paid": "Оплата прошла. Менеджер подтвердит наличие и доставку.",
    "confirmed": "Наличие подтверждено. Собираем к отправке.",
    "completed": "Заявка закрыта. Спасибо, что выбрал этот выпуск.",
    "cancelled": "Заявка отменена. Если это ошибка — собери новую из карточки вещи.",
}

# Витрину открывают и оформляют заказ за один заход. Сутки — запас на «свернул
# и вернулся»; двое суток давали слишком длинное окно для перехваченного initData.
WEBAPP_INIT_MAX_AGE = 60 * 60 * 24


def webapp_user_from_init_data(token: str, init_data: str, now: int | None = None) -> dict[str, Any] | None:
    """Validate Telegram Mini App initData and return the signed user, or None."""
    if not token or not init_data or len(init_data) > 4096:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received = str(parsed.pop("hash", "")).lower()
    if len(received) != 64 or any(char not in "0123456789abcdef" for char in received):
        return None
    data_check = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(parsed.get("auth_date", ""))
    except (TypeError, ValueError):
        return None
    stamp = int(now if now is not None else time.time())
    if auth_date > stamp + 60 or stamp - auth_date > WEBAPP_INIT_MAX_AGE:
        return None
    try:
        user = json.loads(parsed.get("user") or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(user, dict) or not str(user.get("id", "")).lstrip("-").isdigit():
        return None
    user["id"] = int(user["id"])
    return user


def public_order_payload(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["status"])
    keys = set(row.keys())
    payment_id = str(row["payment_id"] or "") if "payment_id" in keys else ""
    try:
        amount = int(row["amount_rub"] or 0) if "amount_rub" in keys else 0
    except (TypeError, ValueError):
        amount = 0
    can_pay = status == "awaiting_payment" and bool(payment_id)
    return {
        "id": int(row["id"]),
        "status": status,
        "status_label": ORDER_STATUS_LABELS.get(status, status),
        "product_name": str(row["product_name"]),
        "size": str(row["size"]),
        "quantity": int(row["quantity"] or 1),
        "created_at": str(row["created_at"]),
        "can_cancel": status in {"new", "awaiting_payment"},
        "can_pay": can_pay,
        "payment_id": payment_id,
        "amount_label": format_rub(amount) if amount else "",
    }

# Шаги мастера добавления товара: что спрашиваем и в каком порядке.
ADD_STEPS = [
    ("name", "Название вещи? Например: Сила и честь"),
    ("price", "Цена? Например: 9 900 ₽"),
    ("sizes", "Размеры через запятую. Например: S, M, L, XL"),
    ("description", "Описание — одна-две фразы, чем вещь цепляет."),
    ("photo_url", "Ссылка на фото (http или https). Или /skip — добавить позже."),
]
ADD_KEYS = [key for key, _ in ADD_STEPS]


class BrandBot:
    def __init__(self, settings: Settings, api: TelegramAPI, db: Database, catalog: Catalog):
        self.settings = settings
        self.api = api
        self.db = db
        self.catalog = catalog
        self.bot_username: str = ""

    # ------------------------------------------------------------------ utils

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def cta(self, kind: str = "menu") -> str:
        brand = esc(self.settings.brand_name)
        if kind == "drop":
            return "\n\nСмотри выпуск — тираж маленький, потом не будет."
        if kind == "channel":
            return f"\n\nНовости — в канале «{brand}». Там узнаёшь первым."
        if kind == "size":
            return "\n\nНет размера? Нажми «Ждать размер» — напишем, когда вернётся."
        return "\n\nОткрой витрину и смотри, что осталось."

    def ref_link(self, user_id: int) -> str:
        if not self.bot_username:
            return ""
        return f"https://t.me/{self.bot_username}?start=ref{user_id}"

    def public_asset_url(self, value: Any) -> str:
        """Turn a Mini App asset path into a public Telegram-readable URL."""
        asset = str(value or "").strip()
        if asset.startswith(("https://", "http://")):
            return asset
        if asset and self.settings.webapp_url.startswith("https://"):
            parsed = urllib.parse.urlparse(self.settings.webapp_url)
            base = f"{parsed.scheme}://{parsed.netloc}/"
            return urllib.parse.urljoin(base, asset.lstrip("/"))
        return ""

    def send_welcome(self, chat_id: int, caption: str, keyboard: dict[str, Any] | None) -> None:
        """Greeting with the gym portrait. Falls back to text if Telegram rejects the photo."""
        photo_url = self.public_asset_url("assets/welcome.jpg")
        try:
            if WELCOME_PHOTO.is_file():
                self.api.send_photo_file(chat_id, WELCOME_PHOTO, caption, keyboard)
                return
            if photo_url:
                self.api.send_photo(chat_id, photo_url, caption, keyboard)
                return
        except Exception:
            LOG.exception("Could not send welcome photo")
            if photo_url:
                try:
                    self.api.send_photo(chat_id, photo_url, caption, keyboard)
                    return
                except Exception:
                    LOG.exception("Welcome photo URL fallback failed")
        self.api.send_message(chat_id, caption, keyboard)

    # ------------------------------------------------------------------ menus

    def main_menu(self) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = [
            [("Смотреть выпуск", "catalog"), ("Образы", "lookbook")],
            [("Подобрать размер", "size_guide"), ("О бренде", "about")],
            [("Канал", self.settings.channel_url)],
            [("Узнать первым", "profile"), ("Привести друга", "referral")],
            [("Мои заявки", "my_orders")],
        ]
        if self.settings.webapp_url.startswith("https://"):
            rows.insert(0, [("Открыть витрину", f"webapp:{self.settings.webapp_url}")])
        return inline_keyboard(rows)

    def interest_menu(self) -> dict[str, Any]:
        rows = [[(category["name"], f"intr:{category['id']}")] for category in self.catalog.categories[:6]]
        rows.append([("Пока не знаю", "intr:skip")])
        return inline_keyboard(rows)

    # ------------------------------------------------------------------ flows

    def start(self, chat_id: int, user: dict[str, Any], payload: str = "") -> None:
        source = payload[:64] if payload else None
        is_new, referrer = self.db.upsert_user(user, source)
        user_id = int(user["id"])
        self.db.event(user_id, "start", {"source": source, "is_new": is_new})
        if referrer:
            self.db.event(referrer, "referral_joined", {"user_id": user_id})
            try:
                self.api.send_message(
                    referrer,
                    "По твоей ссылке зашёл новый человек. Так и набирается приоритет на выпуск.",
                )
            except Exception:
                LOG.debug("Could not notify referrer %s", referrer)
        name = esc(user.get("first_name") or "друг")

        if is_new:
            text = (
                f"<b>{esc(self.settings.brand_name)}</b>\n\n"
                f"{name}, ты на закрытой территории.\n"
                "Здесь первыми разбирают выпуск. Тираж маленький, очереди нет — "
                "берёт тот, кто успел.\n\n"
                "<b>Что тебе ближе?</b> Отметишь — будем писать по делу, не всем подряд."
            )
            self.send_welcome(chat_id, text, self.interest_menu())
        else:
            text = (
                f"<b>{esc(self.settings.brand_name)}</b>\n\n"
                f"{name}, ты уже в базе. Новые вещи видишь раньше остальных."
                + self.cta("drop")
            )
            self.send_welcome(chat_id, text, self.main_menu())

    def save_interest(self, chat_id: int, user_id: int, interest: str) -> None:
        if interest != "skip":
            self.db.set_interest(user_id, interest)
            self.db.event(user_id, "interest_set", {"interest": interest})
        text = (
            "Записал.\n\n"
            "Вещи разбирают быстро — смотри, что осталось, и бери размер, пока он есть."
            + self.cta("drop")
        )
        self.api.send_message(chat_id, text, self.main_menu())

    def show_catalog(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "catalog_open")
        rows = [[(category["name"], f"cat:{category['id']}")] for category in self.catalog.categories]
        rows.append([("Главное меню", "menu")])
        self.api.send_message(
            chat_id, "<b>Витрина</b>\n\nВыбирай, что смотреть. Остатки честные.", inline_keyboard(rows)
        )

    def show_category(self, chat_id: int, user_id: int, category_id: str) -> None:
        products = self.catalog.products_for_category(category_id)
        self.db.event(user_id, "category_open", {"category": category_id})
        if not products:
            self.api.send_message(
                chat_id,
                "Здесь пока пусто. Выпуск готовится.\n"
                "Подпишись на канал — узнаешь первым, а не когда всё разберут."
                + self.cta("channel"),
                inline_keyboard([[("Канал", self.settings.channel_url)], [("Назад", "catalog")]]),
            )
            return
        rows = [[(f"{p['name']} — {p['price']}", f"product:{p['id']}")] for p in products]
        rows.append([("Назад к витрине", "catalog")])
        self.api.send_message(chat_id, "<b>В наличии</b> — бери, пока есть:", inline_keyboard(rows))

    def show_product(self, chat_id: int, user_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.api.send_message(chat_id, "Вещь снята с продажи или уже разобрана.", self.main_menu())
            return
        self.db.event(user_id, "product_open", {"product_id": product_id})
        sizes = " · ".join(esc(str(size)) for size in product["sizes"])
        caption = (
            f"<b>{esc(product['name'])}</b>\n"
            f"<b>{esc(str(product['price']))}</b>\n\n"
            f"{esc(product['description'])}\n\n"
            f"Размеры: {sizes}"
            + self.cta("size")
        )
        keyboard = inline_keyboard(
            [
                [("Выбрать размер", f"want:{product_id}")],
                [("Ждать размер", f"wait:{product_id}")],
                [("Назад", f"cat:{product['category']}")],
            ]
        )
        photo = self.public_asset_url(product.get("photo_url") or product.get("image"))
        if photo:
            self.api.send_photo(chat_id, photo, caption, keyboard)
        else:
            self.api.send_message(chat_id, caption, keyboard)

    def choose_size(self, chat_id: int, user_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.api.send_message(chat_id, "Вещь больше недоступна.", self.main_menu())
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [[(size, f"size:{product_id}:{size}") for size in sizes[index:index + 4]] for index in range(0, len(sizes), 4)]
        rows.append([("Назад", f"product:{product_id}")])
        self.api.send_message(chat_id, "Какой размер берёшь?", inline_keyboard(rows))

    def select_size(self, chat_id: int, user_id: int, product_id: str, size: str, request_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product or size not in [str(item) for item in product["sizes"]]:
            self.api.send_message(chat_id, "Этот вариант уже разобрали.", self.main_menu())
            return
        user = self.db.get_user(user_id)
        if user and user["phone"]:
            self.finish_order(chat_id, user_id, product, size, user["phone"], request_id)
            return
        self.ask_phone(
            chat_id,
            user_id,
            "awaiting_order_phone",
            {"product_id": product_id, "size": size, "request_id": request_id},
        )

    def ask_waitlist_size(self, chat_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.api.send_message(chat_id, "Вещь больше недоступна.", self.main_menu())
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [[(size, f"wsize:{product_id}:{size}") for size in sizes[index:index + 4]] for index in range(0, len(sizes), 4)]
        rows.append([("Назад", f"product:{product_id}")])
        self.api.send_message(
            chat_id,
            "Какой размер ждёшь?\nВернётся — напишем первыми, раньше канала.",
            inline_keyboard(rows),
        )

    def confirm_waitlist(self, chat_id: int, user_id: int, product_id: str, size: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.api.send_message(chat_id, "Вещь больше недоступна.", self.main_menu())
            return
        added = self.db.add_to_waitlist(user_id, product, size)
        self.db.event(user_id, "waitlist_add", {"product_id": product_id, "size": size})
        if added:
            text = (
                f"Записал: <b>{esc(product['name'])}</b>, размер {esc(size)}.\n"
                "Как только вернётся — напишем раньше всех."
            )
        else:
            text = f"Этот размер уже в листе: <b>{esc(product['name'])}</b> · {esc(size)}."
        self.api.send_message(chat_id, text + self.cta("channel"), self.main_menu())

    def new_payment_id(self) -> str:
        return secrets.token_hex(6)

    def line_amount(self, product: dict[str, Any], quantity: int) -> int:
        return parse_price_rub(product.get("price")) * max(1, int(quantity or 1))

    def pay_title(self) -> str:
        return (self.settings.brand_name or "ВОРОЖБИТОВ")[:32]

    def build_pay_methods(
        self,
        payment_id: str,
        amount_rub: int,
        description: str,
    ) -> list[dict[str, str]]:
        methods: list[dict[str, str]] = []
        if amount_rub <= 0:
            return methods
        origin = self.settings.public_origin()
        hook_lava = f"{origin}/api/payments/lava" if origin.startswith("https://") else ""
        success = origin or (f"https://t.me/{self.bot_username}" if self.bot_username else "")
        title = self.pay_title()
        if self.settings.lava_ready():
            try:
                invoice = create_lava_invoice(
                    self.settings.lava_shop_id,
                    self.settings.lava_secret_key,
                    payment_id,
                    amount_rub,
                    description,
                    hook_url=hook_lava,
                    success_url=success,
                )
                methods.append({
                    "id": "lava",
                    "title": "Карта / СБП",
                    "hint": "Мир, Visa, СБП",
                    "url": invoice["url"],
                    "kind": "link",
                })
            except Exception:
                LOG.warning("Lava invoice failed", exc_info=True)
        if self.settings.crypto_ready():
            try:
                paid_url = success if str(success).startswith("https://") else ""
                invoice = create_crypto_invoice(
                    self.settings.crypto_pay_token,
                    amount_rub,
                    description,
                    payment_id,
                    paid_btn_url=paid_url,
                )
                methods.append({
                    "id": "crypto",
                    "title": "Крипта",
                    "hint": "USDT и другие монеты",
                    "url": invoice["url"],
                    "kind": "link",
                })
            except Exception:
                LOG.warning("Crypto invoice failed", exc_info=True)
        if self.settings.stars_enabled:
            stars = stars_amount(amount_rub, self.settings.stars_rub_per_star)
            url = ""
            try:
                url = self.api.create_invoice_link(
                    {
                        "title": title,
                        "description": description[:255],
                        "payload": payment_id[:128],
                        "currency": "XTR",
                        "prices": [{"label": title, "amount": stars}],
                    }
                )
            except Exception:
                LOG.warning("Stars invoice link failed", exc_info=True)
            methods.append(
                {
                    "id": "stars",
                    "title": "Звёзды Telegram",
                    "hint": f"{stars} зв. · внутри Telegram",
                    "url": url,
                    "kind": "invoice",
                    "stars": str(stars),
                }
            )
        return methods

    def pay_keyboard(self, payment_id: str) -> dict[str, Any] | None:
        rows: list[list[tuple[str, str]]] = []
        if self.settings.lava_ready():
            rows.append([("Карта / СБП", f"pay:{payment_id}:lava")])
        if self.settings.crypto_ready():
            rows.append([("Крипта", f"pay:{payment_id}:crypto")])
        if self.settings.stars_enabled:
            rows.append([("Звёзды Telegram", f"pay:{payment_id}:stars")])
        if not rows:
            return None
        rows.append([("Мои заявки", "my_orders")])
        return inline_keyboard(rows)

    def offer_payment(
        self,
        chat_id: int,
        payment_id: str,
        amount_rub: int,
        order_lines: list[str],
        source: str = "витрины",
    ) -> None:
        keyboard = self.pay_keyboard(payment_id) or self.main_menu()
        text = (
            f"<b>Заявка из {esc(source)} принята</b>\n\n"
            + "\n".join(order_lines)
            + f"\n\nК оплате: <b>{esc(format_rub(amount_rub))}</b>\n"
            "Оплати сейчас — карта, СБП, крипта или звёзды Telegram. "
            "Деньги списываются сразу. Если размера нет — возврат через менеджера."
        )
        self.api.send_message(chat_id, text, keyboard)

    def notify_paid(self, payment_id: str, chat_id: int | None = None) -> None:
        payment = self.db.get_payment(payment_id)
        if not payment:
            return
        orders = self.db.orders_for_payment(payment_id)
        user_id = int(payment["user_id"])
        target = chat_id if chat_id is not None else user_id
        lines = [
            f"• {esc(row['product_name'])} · {esc(row['size'])} · {int(row['quantity'] or 1)} шт."
            for row in orders
        ]
        text = (
            "<b>Оплата прошла</b>\n\n"
            + ("\n".join(lines) if lines else "Заявка оплачена.")
            + f"\n\n{esc(ORDER_STATUS_MESSAGES['paid'])}"
        )
        try:
            self.api.send_message(target, text, self.main_menu())
        except Exception:
            LOG.warning("Could not notify user about payment %s", payment_id)
        if self.settings.manager_chat_id:
            try:
                self.api.send_message(
                    self.settings.manager_chat_id,
                    f"<b>Оплата {esc(payment_id)}</b>\n"
                    f"Клиент: {user_id}\n"
                    + ("\n".join(lines) + "\n" if lines else "")
                    + f"Сумма: {esc(format_rub(int(payment['amount_rub'])))}\n"
                    f"Способ: {esc(payment['method'] or '—')}",
                )
            except Exception as exc:
                LOG.warning("Could not notify manager about payment %s: %s", payment_id, exc)

    def handle_pre_checkout(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id", ""))
        payload = str(query.get("invoice_payload", ""))
        currency = str(query.get("currency", ""))
        try:
            total = int(query.get("total_amount") or 0)
        except (TypeError, ValueError):
            total = 0
        payment = self.db.get_payment(payload)
        if not query_id:
            return
        if not payment or str(payment["status"]) == "paid":
            try:
                self.api.answer_pre_checkout(query_id, False, "Оплата уже неактуальна.")
            except Exception:
                LOG.exception("answerPreCheckoutQuery failed")
            return
        if currency == "XTR":
            expected = int(payment["amount_stars"] or 0)
            if expected and total != expected:
                try:
                    self.api.answer_pre_checkout(query_id, False, "Сумма не совпадает.")
                except Exception:
                    LOG.exception("answerPreCheckoutQuery failed")
                return
        try:
            self.api.answer_pre_checkout(query_id, True)
        except Exception:
            LOG.exception("answerPreCheckoutQuery failed")

    def handle_successful_payment(self, chat_id: int, user_id: int, info: dict[str, Any]) -> None:
        payload = str(info.get("invoice_payload", ""))
        charge = str(info.get("telegram_payment_charge_id") or info.get("provider_payment_charge_id") or "")
        if not payload:
            self.api.send_message(chat_id, "Оплата пришла, но заявка не найдена. Напиши менеджеру.", self.main_menu())
            return
        self.db.mark_payment_paid(payload, "stars", charge)
        self.notify_paid(payload, chat_id)
        self.db.event(user_id, "stars_paid", {"payment_id": payload})

    def start_method_pay(self, chat_id: int, user_id: int, payment_id: str, method: str) -> None:
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != user_id:
            self.api.send_message(chat_id, "Оплата не найдена.", self.main_menu())
            return
        if str(payment["status"]) == "paid":
            self.api.send_message(chat_id, "Эта заявка уже оплачена.", self.main_menu())
            return
        amount = int(payment["amount_rub"] or 0)
        description = f"{self.settings.brand_name}: оплата {payment_id}"
        if method == "stars":
            if not self.settings.stars_enabled:
                self.api.send_message(chat_id, "Звёзды сейчас выключены.")
                return
            stars = int(payment["amount_stars"] or stars_amount(amount, self.settings.stars_rub_per_star))
            try:
                self.api.send_invoice(
                    chat_id,
                    {
                        "title": self.pay_title(),
                        "description": description[:255],
                        "payload": payment_id[:128],
                        "currency": "XTR",
                        "prices": [{"label": self.pay_title(), "amount": stars}],
                    },
                )
            except Exception:
                LOG.exception("sendInvoice stars failed")
                self.api.send_message(chat_id, "Не получилось выставить счёт в звёздах. Попробуй ещё раз.")
            return
        methods = self.build_pay_methods(payment_id, amount, description)
        found = next((item for item in methods if item["id"] == method and item.get("url")), None)
        if not found:
            self.api.send_message(chat_id, "Этот способ сейчас недоступен. Выбери другой или напиши менеджеру.")
            return
        self.api.send_message(
            chat_id,
            "Ссылка на оплату. После перевода статус в «Мои заявки» станет «оплачена».",
            inline_keyboard([[("Оплатить", found["url"])]]),
        )

    def finish_order(
        self,
        chat_id: int,
        user_id: int,
        product: dict[str, Any],
        size: str,
        phone: str,
        request_id: str,
    ) -> None:
        amount = self.line_amount(product, 1)
        payment_id = self.new_payment_id() if amount else ""
        status = "awaiting_payment" if amount else "new"
        order_id, created = self.db.create_order(
            request_id,
            user_id,
            product,
            size,
            phone,
            status=status,
            payment_id=payment_id,
            amount_rub=amount,
        )
        if not created:
            self.api.send_message(chat_id, f"Заявка №{order_id} уже принята.", self.main_menu())
            return
        if payment_id:
            self.db.create_payment(
                payment_id,
                user_id,
                amount,
                stars_amount(amount, self.settings.stars_rub_per_star) if self.settings.stars_enabled else 0,
            )
        line = f"• {esc(product['name'])} · {esc(size)} · 1 шт."
        if amount:
            self.api.send_message(chat_id, "Заявка собрана.", remove_keyboard())
            self.offer_payment(chat_id, payment_id, amount, [line], source="бота")
        else:
            self.api.send_message(
                chat_id,
                f"<b>Заявка №{order_id} принята</b>\n\n"
                f"{esc(product['name'])}\n"
                f"Размер: {esc(size)}\n"
                f"Стоимость: {esc(str(product['price']))}\n\n"
                "Менеджер напишет про наличие и доставку.",
                remove_keyboard(),
            )
            self.api.send_message(chat_id, "Пока ждёшь — посмотри, что ещё осталось:" + self.cta("drop"), self.main_menu())
        notify_chat = self.settings.manager_chat_id
        if notify_chat:
            user = self.db.get_user(user_id)
            username = f"@{user['username']}" if user and user["username"] else str(user_id)
            try:
                self.api.send_message(
                    notify_chat,
                    f"<b>Новая заявка №{order_id}</b>\n"
                    f"Клиент: {esc(username)}\n"
                    f"Вещь: {esc(product['name'])}\n"
                    f"Размер: {esc(size)} · 1 шт.\n"
                    f"Телефон: {esc(phone)}\n"
                    f"К оплате: {esc(format_rub(amount))}"
                    + (f"\nОплата: {esc(payment_id)}" if payment_id else ""),
                )
            except Exception as exc:
                LOG.warning("Could not notify manager about order %s: %s", order_id, exc)

    def request_profile(self, chat_id: int, user_id: int) -> None:
        self.ask_phone(chat_id, user_id, "awaiting_profile_phone", {})

    def consent_text(self) -> str:
        text = (
            "<b>Перед номером — одно согласие</b>\n\n"
            "Чтобы принять заявку и писать про выпуск, нужен номер и согласие "
            "на обработку данных и сообщения от бренда.\n\n"
            "Данные только для заявки и связи по ней. Отписаться можно в любой момент."
        )
        if self.settings.privacy_url:
            text += f"\n\nПолитика: {esc(self.settings.privacy_url)}"
        return text

    def ask_phone(self, chat_id: int, user_id: int, next_state: str, next_data: dict[str, Any]) -> None:
        """Ask for a phone number, but demand consent first if we do not have it yet."""
        if self.db.has_consent(user_id):
            self.db.set_state(user_id, next_state, next_data)
            self.api.send_message(
                chat_id,
                "Оставь номер — подтвердим наличие и доставку. Оплата сразу после заявки.\n\n"
                "Можно кнопкой или цифрами, например +79991234567.",
                contact_keyboard(),
            )
            return
        self.db.set_state(
            user_id,
            "awaiting_consent",
            {"next_state": next_state, "next_data": next_data},
        )
        self.api.send_message(
            chat_id,
            self.consent_text(),
            inline_keyboard([[("Согласен", "consent:yes")], [("Не сейчас", "consent:no")]]),
        )

    def accept_consent(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "awaiting_consent":
            self.api.send_message(chat_id, "Не нашёл, к чему это относилось. Зайди заново.", self.main_menu())
            return
        next_state = str(state[1].get("next_state", "awaiting_profile_phone"))
        next_data = dict(state[1].get("next_data") or {})
        self.db.set_consent(user_id)
        self.db.event(user_id, "consent_given")
        pending_phone = str(next_data.pop("phone", "") or "")
        if pending_phone:
            self.db.set_state(user_id, next_state, next_data)
            self.save_phone_and_continue(chat_id, user_id, pending_phone)
            return
        self.db.set_state(user_id, next_state, next_data)
        hint = (
            "Принято. Теперь нужен номер."
            if next_state == "awaiting_order_phone"
            else "Принято. Напишем про выпуск и про твой размер."
        )
        self.api.send_message(chat_id, hint, contact_keyboard())

    def decline_consent(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        was_order = bool(state and state[0] == "awaiting_consent"
                         and state[1].get("next_state") == "awaiting_order_phone")
        self.db.clear_state(user_id)
        if was_order:
            self.api.send_message(
                chat_id,
                "Без согласия заявку принять нельзя — номер хранить не имеем права.\n\n"
                "Витрина открыта. Если передумаешь — кнопка ниже." + self.cta("channel"),
                self.main_menu(),
            )
        else:
            self.api.send_message(
                chat_id,
                "Хорошо, номер не сохраняем. Новые вещи всё равно выходят в канале — там ничего не пропустишь."
                + self.cta("channel"),
                self.main_menu(),
            )

    def save_phone_and_continue(self, chat_id: int, user_id: int, phone: str) -> None:
        if not self.db.has_consent(user_id):
            state = self.db.get_state(user_id)
            next_state = "awaiting_profile_phone"
            next_data: dict[str, Any] = {}
            if state and state[0] == "awaiting_consent":
                next_state = str(state[1].get("next_state") or next_state)
                next_data = dict(state[1].get("next_data") or {})
            elif state:
                next_state = state[0]
                next_data = dict(state[1] or {})
            next_data["phone"] = phone
            self.db.set_state(user_id, "awaiting_consent", {"next_state": next_state, "next_data": next_data})
            self.api.send_message(
                chat_id,
                "Сначала подтверди согласие — без него номер сохранить нельзя.\n\n" + self.consent_text(),
                inline_keyboard([[("Согласен", "consent:yes")], [("Не сейчас", "consent:no")]]),
            )
            return
        self.db.set_phone(user_id, phone)
        state = self.db.get_state(user_id)
        if state and state[0] == "awaiting_order_phone":
            product = self.catalog.get(str(state[1].get("product_id", "")))
            size = str(state[1].get("size", ""))
            request_id = str(state[1].get("request_id", f"phone:{user_id}:{product['id'] if product else ''}:{size}"))
            if product:
                self.finish_order(chat_id, user_id, product, size, phone, request_id)
                return
        self.db.clear_state(user_id)
        self.db.event(user_id, "contact_saved")
        self.api.send_message(
            chat_id,
            "Номер в базе. Теперь ты в списке тех, кто узнаёт первым." + self.cta("channel"),
            remove_keyboard(),
        )
        self.api.send_message(chat_id, "Главное меню:", self.main_menu())

    def show_referral(self, chat_id: int, user_id: int) -> None:
        user = self.db.get_user(user_id)
        invited = int(user["invited_count"]) if user else 0
        threshold = max(1, self.settings.giveaway_min_invites)
        link = self.ref_link(user_id)
        text = (
            "<b>Приведи друга — забери выпуск первым</b>\n\n"
            f"Приглашено: <b>{invited}</b>\n"
            f"До приоритета: <b>{max(0, threshold - invited)}</b>\n\n"
            "Кидаешь ссылку другу — он заходит в бота — ты поднимаешься в списке. "
            f"Набрал {threshold} — приоритет на следующий выпуск и участие в розыгрыше."
        )
        if link:
            text += f"\n\nТвоя ссылка:\n<code>{esc(link)}</code>"
        self.api.send_message(
            chat_id,
            text,
            inline_keyboard(
                [
                    [("Отправить другу", f"https://t.me/share/url?url={urllib.parse.quote(link or '', safe='')}")],
                    [("Главное меню", "menu")],
                ]
            ),
        )

    def show_lookbook(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "lookbook_open")
        photos = [
            photo
            for item in self.catalog.data.get("lookbook", [])
            if (photo := self.public_asset_url(item.get("photo_url") or item.get("image")))
        ]
        if not photos:
            self.api.send_message(
                chat_id,
                "<b>Образы</b>\n\nПервые кадры снимаются. Они выйдут в канале раньше открытого выпуска."
                + self.cta("channel"),
                inline_keyboard([[("Канал", self.settings.channel_url)], [("Главное меню", "menu")]]),
            )
            return
        try:
            self.api.send_media_group(chat_id, photos, "<b>Образы</b>")
        except Exception:
            LOG.exception("Failed to send lookbook album")
            self.api.send_message(chat_id, "Не получилось отправить альбом. Загляни в канал — там всё выложим.", self.main_menu())
            return
        self.api.send_message(chat_id, "Понравилось? Тогда не жди — размеры разбирают." + self.cta("drop"), self.main_menu())

    def notify_waitlist(self, product_id: str, size: str) -> int:
        """Admin-triggered ping for a restocked size. Returns number of notified users."""
        delivered = 0
        product = self.catalog.get(product_id)
        if not product:
            return 0
        for recipient in self.db.waitlist_user_ids(product_id, size):
            try:
                self.api.send_message(
                    recipient,
                    f"<b>Размер вернулся</b>\n\n{esc(product['name'])} — размер {esc(size)} снова в наличии.\n"
                    "Бери сейчас: размер могут разобрать быстро.",
                    inline_keyboard([[("Забрать", f"want:{product_id}")]]),
                )
                delivered += 1
                time.sleep(0.04)
            except Exception as exc:
                LOG.warning("Waitlist notify failed for %s: %s", recipient, exc)
        return delivered

    def show_my_orders(self, chat_id: int, user_id: int) -> None:
        rows = self.db.orders_for_user(user_id)
        if not rows:
            self.api.send_message(
                chat_id,
                "Заявок пока нет. Выбери вещь в витрине — здесь появится статус.",
                self.main_menu(),
            )
            return
        self.api.send_message(chat_id, "<b>Твои заявки</b>")
        for row in rows:
            status = str(row["status"])
            label = ORDER_STATUS_LABELS.get(status, status)
            body = (
                f"<b>№{row['id']} · {esc(label)}</b>\n"
                f"{esc(row['product_name'])} · размер {esc(row['size'])} · {int(row['quantity'] or 1)} шт."
            )
            keyboard = None
            if status in {"new", "awaiting_payment"}:
                rows = []
                if status == "awaiting_payment" and str(row["payment_id"] or ""):
                    pay_id = str(row["payment_id"])
                    if self.settings.lava_ready():
                        rows.append([("Карта / СБП", f"pay:{pay_id}:lava")])
                    if self.settings.crypto_ready():
                        rows.append([("Крипта", f"pay:{pay_id}:crypto")])
                    if self.settings.stars_enabled:
                        rows.append([("Звёзды Telegram", f"pay:{pay_id}:stars")])
                rows.append([("Отменить заявку", f"ucancel:{row['id']}")])
                keyboard = inline_keyboard(rows)
            self.api.send_message(chat_id, body, keyboard)

    def cancel_own_order(self, chat_id: int, user_id: int, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != user_id:
            self.api.send_message(chat_id, "Заявка не найдена.", self.main_menu())
            return
        try:
            updated, changed = self.db.set_order_status(order_id, "cancelled")
        except ValueError:
            self.api.send_message(chat_id, "Эту заявку уже нельзя отменить — напиши менеджеру.", self.main_menu())
            return
        if not changed:
            self.api.send_message(chat_id, "Заявка уже отменена.", self.main_menu())
            return
        self.api.send_message(
            chat_id,
            f"Заявка №{order_id} отменена. Если передумаешь — собери новую из витрины.",
            self.main_menu(),
        )
        if self.settings.manager_chat_id:
            try:
                self.api.send_message(
                    self.settings.manager_chat_id,
                    f"Клиент отменил заявку №{order_id}: {esc(updated['product_name'] if updated else '')}.",
                )
            except Exception as exc:
                LOG.warning("Could not notify manager about cancel %s: %s", order_id, exc)

    def help_text(self) -> str:
        brand = esc(self.settings.brand_name)
        extra = ""
        if self.settings.support_username:
            extra = f"\nВопросы: @{esc(self.settings.support_username)}"
        return (
            f"<b>{brand}</b>\n\n"
            "Как это работает:\n"
            "1. Смотри витрину или выпуск в боте.\n"
            "2. Выбери размер и оплати заявку.\n"
            "3. Карта / СБП, крипта или звёзды Telegram.\n\n"
            "Нет размера — «Ждать размер»: напишем, когда вернётся.\n"
            "Деньги списываются сразу. Если размера нет — возврат через менеджера."
            f"{extra}"
        )

    # ------------------------------------------------------------------ admin

    def order_status_keyboard(self, order_id: int, status: str) -> dict[str, Any] | None:
        rows: list[list[tuple[str, str]]] = []
        if status in {"new", "paid"}:
            rows.append([("Подтвердить", f"order:{order_id}:confirmed")])
            rows.append([("Отменить", f"order:{order_id}:cancelled")])
        elif status == "awaiting_payment":
            rows.append([("Отметить оплату", f"order:{order_id}:paid")])
            rows.append([("Отменить", f"order:{order_id}:cancelled")])
        elif status == "confirmed":
            rows.append([("Завершить", f"order:{order_id}:completed")])
            rows.append([("Отменить", f"order:{order_id}:cancelled")])
        return inline_keyboard(rows) if rows else None

    def update_order_status(self, chat_id: int, order_id: int, status: str) -> None:
        try:
            order, changed = self.db.set_order_status(order_id, status)
        except ValueError as exc:
            self.api.send_message(chat_id, f"Не могу изменить заявку: {esc(exc)}")
            return
        if not order:
            self.api.send_message(chat_id, "Заявка не найдена.")
            return
        label = ORDER_STATUS_LABELS.get(status, status)
        if not changed:
            self.api.send_message(chat_id, f"Заявка #{order_id} уже имеет статус «{esc(label)}».")
            return
        try:
            self.api.send_message(
                int(order["user_id"]),
                f"<b>Заявка №{order_id}: {esc(label)}</b>\n\n"
                f"{esc(order['product_name'])} · размер {esc(order['size'])} · {int(order['quantity'] or 1)} шт.\n"
                f"{esc(ORDER_STATUS_MESSAGES.get(status, 'Статус заявки обновлён.'))}",
                self.main_menu(),
            )
        except Exception as exc:
            LOG.warning("Could not notify user %s about order %s: %s", order["user_id"], order_id, exc)
        self.api.send_message(chat_id, f"Заявка №{order_id}: статус «{esc(label)}» сохранён.")

    def admin_panel(self, chat_id: int) -> None:
        self.api.send_message(
            chat_id,
            "<b>Управление</b>\n\nЧто делаем?",
            inline_keyboard(
                [
                    [("Статистика", "adm:stats"), ("Заявки", "adm:orders")],
                    [("Добавить вещь", "adm:add"), ("Лист ожидания", "adm:waitlist")],
                    [("Топ рефералов", "adm:top"), ("Экспорт базы", "adm:export")],
                    [("Перечитать каталог", "adm:reload")],
                ]
            ),
        )

    def admin_command(self, chat_id: int, user_id: int, text: str) -> bool:
        if not self.is_admin(user_id):
            return False
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command == "/stats":
            stats = self.db.stats()
            self.api.send_message(
                chat_id,
                "<b>Статистика</b>\n\n"
                f"В базе: {stats['users']}\n"
                f"С контактом: {stats['contacts']}\n"
                f"Заявок всего: {stats['orders']}\n"
                f"Заявок сегодня: {stats['orders_today']}\n"
                f"Приведено друзей: {stats['referrals']}\n"
                f"В листе ожидания: {stats['waitlist']}\n"
                f"Дали согласие: {stats['consents']}",
            )
        elif command == "/orders":
            orders = self.db.recent_orders()
            if not orders:
                self.api.send_message(chat_id, "Заявок пока нет.")
            else:
                self.api.send_message(chat_id, "<b>Последние заявки</b>")
                for row in orders:
                    username = f"@{row['username']}" if row["username"] else row["first_name"]
                    status = str(row["status"])
                    label = ORDER_STATUS_LABELS.get(status, status)
                    text = (
                        f"<b>№{row['id']} · {esc(label)}</b>\n"
                        f"{esc(row['product_name'])} · размер {esc(row['size'])} · {int(row['quantity'] or 1)} шт.\n"
                        f"Клиент: {esc(username or str(row['user_id']))}\n"
                        f"Телефон: {esc(row['phone'])}"
                        + (f"\n{esc(row['note'])}" if str(row["note"] or "").strip() else "")
                    )
                    self.api.send_message(chat_id, text, self.order_status_keyboard(int(row["id"]), status))
        elif command == "/broadcast":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/broadcast текст рассылки</code>")
            else:
                self.db.set_state(user_id, "broadcast_pending", {"text": argument})
                self.api.send_message(
                    chat_id,
                    f"<b>Кому пишем?</b>\n\n{esc(argument)}",
                    self.segment_keyboard(),
                )
        elif command == "/restock":
            parts = argument.split()
            if len(parts) != 2:
                self.api.send_message(chat_id, "Использование: <code>/restock id_товара размер</code>")
            else:
                delivered = self.notify_waitlist(parts[0], parts[1])
                self.api.send_message(chat_id, f"Уведомлено по листу ожидания: {delivered}.")
        elif command == "/waitlist":
            rows = self.db.waitlist_rows()
            if not rows:
                self.api.send_message(chat_id, "Лист ожидания пуст.")
            else:
                lines = ["<b>Ждут размер</b>", ""]
                for row in rows:
                    username = f"@{row['username']}" if row["username"] else row["first_name"]
                    lines.append(f"{esc(row['product_name'])} · {esc(row['size'])} · {esc(username or row['user_id'])}")
                self.api.send_message(chat_id, "\n".join(lines))
        elif command == "/top":
            rows = self.db.top_referrers()
            if not rows:
                self.api.send_message(chat_id, "Никто пока никого не привёл.")
            else:
                lines = ["<b>Топ по приведённым друзьям</b>", ""]
                for index, row in enumerate(rows, start=1):
                    username = f"@{row['username']}" if row["username"] else row["first_name"]
                    lines.append(f"{index}. {esc(username or row['user_id'])} — {row['invited_count']}")
                self.api.send_message(chat_id, "\n".join(lines))
        elif command == "/giveaway":
            count = int(argument) if argument.isdigit() else 1
            pool = self.db.giveaway_pool(self.settings.giveaway_min_invites)
            if not pool:
                self.api.send_message(
                    chat_id,
                    f"Никто ещё не набрал {self.settings.giveaway_min_invites} приглашённых.",
                )
            else:
                winners = random.sample(pool, min(count, len(pool)))
                lines = [f"<b>Победители ({len(winners)})</b>", ""]
                for winner_id in winners:
                    user = self.db.get_user(winner_id)
                    username = f"@{user['username']}" if user and user["username"] else str(winner_id)
                    lines.append(f"{esc(username)} — id {winner_id}")
                    try:
                        self.api.send_message(
                            winner_id,
                            "<b>Ты в розыгрыше</b>\n\n"
                            "Ссылка сработала. Напиши менеджеру — заберёшь вещь из выпуска первым.",
                        )
                    except Exception:
                        LOG.warning("Could not notify winner %s", winner_id)
                self.api.send_message(chat_id, "\n".join(lines))
        elif command in ("/add", "/new"):
            self.start_add_product(chat_id, user_id)
        elif command == "/hide":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/hide id_товара</code>")
            elif self.catalog.set_active(argument, False):
                self.api.send_message(chat_id, f"Вещь {esc(argument)} скрыта из витрины.")
            else:
                self.api.send_message(chat_id, "Не нашёл такой id.")
        elif command == "/show":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/show id_товара</code>")
            elif self.catalog.set_active(argument, True):
                self.api.send_message(chat_id, f"Вещь {esc(argument)} снова в витрине.")
            else:
                self.api.send_message(chat_id, "Не нашёл такой id.")
        elif command == "/export":
            self.api.send_document(chat_id, "users.csv", self.db.export_users_csv(), "Экспорт базы")
        elif command == "/reload":
            self.catalog.reload()
            self.api.send_message(chat_id, "Каталог перечитан без перезапуска.")
        elif command in ("/admin", "/panel"):
            self.admin_panel(chat_id)
        else:
            return False
        return True

    # ------------------------------------------------- добавление товара

    def start_add_product(self, chat_id: int, user_id: int) -> None:
        self.db.set_state(user_id, "admin_add", {"step": "category"})
        rows = [[(category["name"], f"addcat:{category['id']}")] for category in self.catalog.categories]
        rows.append([("Новая категория", "addcat:new")])
        rows.append([("Отмена", "admin:cancel")])
        self.api.send_message(chat_id, "Новая вещь. Куда её положим?", inline_keyboard(rows))

    def pick_add_category(self, chat_id: int, user_id: int, category_id: str) -> None:
        if category_id == "new":
            self.db.set_state(user_id, "admin_add", {"step": "new_category"})
            self.api.send_message(chat_id, "Название новой категории? Например: Верхняя одежда")
            return
        if not any(category["id"] == category_id for category in self.catalog.categories):
            self.api.send_message(chat_id, "Такой категории нет.", self.main_menu())
            return
        self.db.set_state(user_id, "admin_add", {"step": "name", "category": category_id})
        self.api.send_message(chat_id, ADD_STEPS[0][1])

    def handle_add_product_text(self, chat_id: int, user_id: int, text: str) -> bool:
        state = self.db.get_state(user_id)
        if not state or state[0] != "admin_add":
            return False
        data = dict(state[1])
        step = str(data.get("step", ""))

        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Черновик выброшен.", self.main_menu())
            return True
        if text.startswith("/") and text.lower() != "/skip":
            # не путаем команды администратора с ответами мастера
            return False

        if step == "new_category":
            name = text.strip()
            if not name:
                return True
            base = slugify(name, "category")
            category_id = base
            index = 2
            with self.catalog.lock:
                existing = {category["id"] for category in self.catalog.categories}
                while category_id in existing:
                    category_id = f"{base}-{index}"
                    index += 1
                self.catalog.data["categories"].append({"id": category_id, "name": name})
                self.catalog.save()
                self.catalog.reload()
            data = {"step": "name", "category": category_id}
            self.db.set_state(user_id, "admin_add", data)
            self.api.send_message(chat_id, f"Категория «{esc(name)}» создана.\n\n{ADD_STEPS[0][1]}")
            return True

        if step not in ADD_KEYS:
            return False
        value = text.strip()
        if step == "photo_url" and value.lower() in ("/skip", "пропустить"):
            value = ""
        elif step == "photo_url" and value and not value.startswith(("https://", "http://")):
            self.api.send_message(chat_id, "Нужна ссылка http или https. Или напиши /skip.")
            return True
        elif not value:
            self.api.send_message(chat_id, "Пусто не подойдёт. Напиши ещё раз или «Отмена».")
            return True
        if step == "price":
            # Цена уходит прямо в счёт, поэтому неоднозначный ввод отклоняем
            # здесь, а не превращаем в случайную сумму на этапе оплаты.
            try:
                rubles = parse_price_strict(value)
            except PriceError:
                self.api.send_message(
                    chat_id,
                    "Не понял цену. Нужно одно число, например: <code>9 900 ₽</code>\n\n"
                    "Диапазоны («1 200 - 1 500»), скидки в скобках и текст не подойдут — "
                    "из такой строки нельзя посчитать сумму счёта.",
                )
                return True
            if rubles <= 0:
                self.api.send_message(chat_id, "Цена должна быть больше нуля. Напиши ещё раз.")
                return True
            value = format_rub(rubles)
        if step == "sizes":
            sizes = [item.strip() for item in re.split(r"[,;/]", value) if item.strip()]
            if not sizes:
                self.api.send_message(chat_id, "Ни одного размера не распознал. Формат: S, M, L, XL")
                return True
            if any(":" in size or len(f"size:x:{size}".encode("utf-8")) > 64 for size in sizes):
                self.api.send_message(chat_id, "Слишком длинный размер или двоеточие. Напиши короче.")
                return True
            data["sizes"] = sizes
        else:
            data[step] = value

        position = ADD_KEYS.index(step) + 1
        if position < len(ADD_STEPS):
            data["step"] = ADD_KEYS[position]
            self.db.set_state(user_id, "admin_add", data)
            self.api.send_message(chat_id, ADD_STEPS[position][1])
            return True

        product = {
            "category": data.get("category", ""),
            "name": data.get("name", ""),
            "price": data.get("price", ""),
            "sizes": data.get("sizes", []),
            "description": data.get("description", ""),
            "photo_url": data.get("photo_url", ""),
            "active": True,
        }
        data["preview"] = product
        data["step"] = "confirm"
        self.db.set_state(user_id, "admin_add", data)
        sizes = " · ".join(esc(size) for size in product["sizes"])
        self.api.send_message(
            chat_id,
            f"<b>Проверь перед публикацией</b>\n\n"
            f"<b>{esc(product['name'])}</b>\n<b>{esc(product['price'])}</b>\n\n"
            f"{esc(product['description'])}\n\nРазмеры: {sizes}",
            inline_keyboard([[("Опубликовать", "add:publish")], [("Отмена", "admin:cancel")]]),
        )
        return True

    def publish_product(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "admin_add" or not state[1].get("preview"):
            self.api.send_message(chat_id, "Черновик не найден. Начни заново: /add")
            return
        product = self.catalog.add_product(dict(state[1]["preview"]))
        self.db.clear_state(user_id)
        self.db.event(user_id, "product_added", {"product_id": product["id"]})
        self.api.send_message(
            chat_id,
            f"Опубликовано: <b>{esc(product['name'])}</b>\n"
            f"id: <code>{esc(product['id'])}</code>\n\n"
            "Вещь уже в витрине. Скрыть — <code>/hide id</code>.",
            inline_keyboard([[("Смотреть в витрине", f"product:{product['id']}")], [("Панель", "adm:panel")]]),
        )

    def segment_keyboard(self) -> dict[str, Any]:
        rows = [[(label, f"seg:{key}")] for key, label in SEGMENT_LABELS.items()]
        for category in self.catalog.categories[:6]:
            rows.append([(f"Интерес: {category['name']}", f"seg:interest:{category['id']}")])
        rows.append([("Отмена", "admin:cancel")])
        return inline_keyboard(rows)

    def preview_broadcast(self, chat_id: int, user_id: int, segment: str) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "broadcast_pending":
            self.api.send_message(chat_id, "Черновик рассылки не найден.")
            return
        text = str(state[1].get("text", "")).strip()
        audience = self.db.broadcast_audience(segment)
        self.db.set_state(user_id, "broadcast_pending", {"text": text, "segment": segment})
        label = SEGMENT_LABELS.get(segment, segment)
        self.api.send_message(
            chat_id,
            f"<b>Предпросмотр</b>\n\n{esc(text)}\n\nАудитория: {label}\nПолучателей: {len(audience)}",
            inline_keyboard([[("Отправить", "admin:broadcast_confirm")], [("Отмена", "admin:cancel")]]),
        )

    def confirm_broadcast(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "broadcast_pending":
            self.api.send_message(chat_id, "Черновик рассылки не найден.")
            return
        text = str(state[1].get("text", "")).strip()
        segment = str(state[1].get("segment", "all"))
        self.db.clear_state(user_id)
        audience = self.db.broadcast_audience(segment)
        if not audience:
            self.api.send_message(chat_id, "В этом сегменте никого нет.")
            return
        # Рассылка идёт в отдельном потоке: раньше она выполнялась прямо в
        # цикле опроса и на 10 000 получателей морозила бота примерно на 7 минут.
        thread = threading.Thread(
            target=self._run_broadcast,
            args=(chat_id, user_id, text, segment, audience),
            name="broadcast",
            daemon=True,
        )
        thread.start()
        self.api.send_message(
            chat_id,
            f"Рассылка пошла: {len(audience)} получателей.\n"
            "Бот продолжает отвечать — отчёт придёт сюда, когда закончим.",
        )

    def _run_broadcast(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        segment: str,
        audience: list[int],
    ) -> None:
        delivered = 0
        failed = 0
        try:
            for recipient in audience:
                if STOP_EVENT.is_set():
                    LOG.warning("Рассылка прервана остановкой бота: доставлено %s", delivered)
                    break
                try:
                    self.api.send_message(recipient, esc(text), self.main_menu())
                    delivered += 1
                    time.sleep(0.04)
                except Exception as exc:
                    failed += 1
                    if "blocked" in str(exc).lower() or "chat not found" in str(exc).lower():
                        self.db.mark_blocked(recipient)
                    LOG.warning("Broadcast failed for %s: %s", recipient, exc)
            self.db.event(
                user_id,
                "broadcast_sent",
                {"delivered": delivered, "failed": failed, "segment": segment},
            )
            try:
                self.api.send_message(chat_id, f"Готово. Доставлено: {delivered}. Ошибок: {failed}.")
            except Exception:
                LOG.warning("Не удалось отправить отчёт о рассылке", exc_info=True)
        finally:
            # Поток рассылки живёт долго и держит собственное соединение SQLite.
            self.db.close_current()

    # ------------------------------------------------------------------ miniapp + input

    def customer_note(self, customer: dict[str, Any]) -> str:
        parts = [
            str(customer.get("name") or "").strip()[:80],
            str(customer.get("city") or "").strip()[:80],
            str(customer.get("address") or "").strip()[:200],
            str(customer.get("entrance") or "").strip()[:80],
            str(customer.get("deliver") or "").strip()[:40],
            str(customer.get("note") or "").strip()[:160],
        ]
        return " · ".join(part for part in parts if part)

    def handle_web_profile(self, chat_id: int, user: dict[str, Any], payload: dict[str, Any]) -> None:
        user_id = int(user["id"])
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else payload
        if not isinstance(profile, dict):
            profile = {}
        phone = normalize_phone(str(profile.get("phone", ""))) if profile.get("phone") else None
        if phone:
            profile = {**profile, "phone": phone}
            self.db.set_phone(user_id, phone)
        if payload.get("consent") is True:
            self.db.set_consent(user_id)
        self.db.set_profile(user_id, profile)
        self.db.event(user_id, "webapp_profile", {"city": str(profile.get("city", ""))[:80]})
        summary = self.customer_note({**profile, "name": profile.get("name") or user.get("first_name") or ""})
        self.api.send_message(
            chat_id,
            "<b>Профиль сохранён</b>\n\n"
            + (esc(summary) if summary else "Контакт записан. Следующая заявка подставит эти данные.")
            + "\n\nМожно сразу смотреть выпуск.",
            self.main_menu(),
        )

    def handle_web_waitlist(self, chat_id: int, user: dict[str, Any], payload: dict[str, Any]) -> None:
        """Accept a size waitlist request from the Mini App."""
        user_id = int(user["id"])
        product = self.catalog.get(str(payload.get("product_id", "")))
        size = str(payload.get("size", ""))
        if not product or size not in {str(item) for item in product.get("sizes", [])}:
            self.api.send_message(chat_id, "Этот размер уже не ждётся — вещь снята или размер неверный.", self.main_menu())
            return
        added = self.db.add_to_waitlist(user_id, product, size)
        self.db.event(user_id, "webapp_waitlist", {"product_id": product["id"], "size": size})
        if added:
            self.api.send_message(
                chat_id,
                f"Записал из витрины: <b>{esc(product['name'])}</b>, размер {esc(size)}.\n"
                "Вернётся — напишем первыми.",
                self.main_menu(),
            )
        else:
            self.api.send_message(
                chat_id,
                f"Этот размер уже в листе ожидания: <b>{esc(product['name'])}</b> · {esc(size)}.",
                self.main_menu(),
            )

    def accept_checkout(
        self,
        user: dict[str, Any],
        customer: dict[str, Any],
        phone: str,
        valid_items: list[tuple[dict[str, Any], str, int]],
        payload: dict[str, Any],
        notify_user: int | None = None,
    ) -> dict[str, Any]:
        user_id = int(user["id"])
        self.db.set_phone(user_id, phone)
        self.db.set_consent(user_id)
        self.db.set_profile(user_id, {**customer, "phone": phone})
        self.db.event(
            user_id,
            "webapp_order_submitted",
            {
                "city": str(customer.get("city", ""))[:160],
                "items": len(valid_items),
                "name": str(customer.get("name", ""))[:80],
            },
        )
        request_base = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("request_id", "")))[:80] or f"web-{user_id}-{int(time.time())}"
        total = sum(self.line_amount(product, quantity) for product, _, quantity in valid_items)
        payment_id = self.new_payment_id() if total else ""
        status = "awaiting_payment" if total else "new"
        created_orders: list[tuple[int, dict[str, Any], str, int]] = []
        note = self.customer_note(customer)
        for index, (product, size, quantity) in enumerate(valid_items, start=1):
            line_sum = self.line_amount(product, quantity)
            order_id, created = self.db.create_order(
                f"{request_base}-{index}-{product['id']}-{size}",
                user_id,
                product,
                size,
                phone,
                quantity,
                note,
                status=status,
                payment_id=payment_id,
                amount_rub=line_sum,
            )
            if created:
                created_orders.append((order_id, product, size, quantity))
        if not created_orders:
            return {"ok": False, "error": "Эта заявка уже была принята."}
        stars = stars_amount(total, self.settings.stars_rub_per_star) if total and self.settings.stars_enabled else 0
        if payment_id:
            self.db.create_payment(payment_id, user_id, total, stars)
        order_lines = [
            f"• {esc(product['name'])} · {esc(size)} · {quantity} шт."
            for _, product, size, quantity in created_orders
        ]
        description = f"{self.settings.brand_name}: {', '.join(product['name'] for _, product, _, _ in created_orders)[:180]}"
        methods = self.build_pay_methods(payment_id, total, description) if payment_id else []
        if notify_user:
            self.offer_payment(int(notify_user), payment_id, total, order_lines, source="витрины")
        if self.settings.manager_chat_id:
            name = str(customer.get("name", ""))[:80]
            manager_lines = [
                "<b>Новая заявка из витрины</b>",
                f"Клиент: {esc(name or user.get('first_name') or user_id)}",
                f"Телефон: {esc(phone)}",
                f"{esc(note or 'Адрес не указан')}",
                f"К оплате: {esc(format_rub(total))}",
                "",
                *order_lines,
            ]
            try:
                self.api.send_message(self.settings.manager_chat_id, "\n".join(manager_lines))
            except Exception as exc:
                LOG.warning("Could not notify manager about Web App order: %s", exc)
        return {
            "ok": True,
            "payment_id": payment_id,
            "amount_rub": total,
            "amount_label": format_rub(total),
            "amount_stars": stars,
            "methods": methods,
            "order_ids": [order_id for order_id, *_ in created_orders],
            "status": status,
            "lines": [
                {"name": str(product["name"]), "size": str(size), "quantity": int(quantity)}
                for _, product, size, quantity in created_orders
            ],
        }

    def resume_payment(self, user_id: int, payment_id: str) -> dict[str, Any]:
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != int(user_id):
            return {"ok": False, "error": "Оплата не найдена."}
        if str(payment["status"]) == "paid":
            return {"ok": False, "error": "Эта заявка уже оплачена."}
        orders = self.db.orders_for_payment(payment_id)
        amount = int(payment["amount_rub"] or 0)
        description = f"{self.settings.brand_name}: оплата {payment_id}"
        methods = self.build_pay_methods(payment_id, amount, description) if amount else []
        return {
            "ok": True,
            "payment_id": payment_id,
            "amount_rub": amount,
            "amount_label": format_rub(amount),
            "amount_stars": int(payment["amount_stars"] or 0),
            "methods": methods,
            "status": "awaiting_payment",
            "lines": [
                {
                    "name": str(row["product_name"]),
                    "size": str(row["size"]),
                    "quantity": int(row["quantity"] or 1),
                }
                for row in orders
            ],
        }

    def checkout_web_payload(
        self,
        user: dict[str, Any],
        payload: dict[str, Any],
        notify_user: int | None = None,
    ) -> dict[str, Any]:
        if payload.get("consent") is not True:
            return {"ok": False, "error": "Без согласия на обработку данных заявку принять нельзя."}
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        phone = normalize_phone(str(customer.get("phone", "")))
        if not phone:
            return {"ok": False, "error": "Проверь номер телефона в витрине и отправь заявку ещё раз."}
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 20:
            return {"ok": False, "error": "В заявке нет вещей или их слишком много. Проверь корзину."}
        valid_items: list[tuple[dict[str, Any], str, int]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            product = self.catalog.get(str(raw_item.get("product_id", "")))
            size = str(raw_item.get("size", ""))
            if not product or size not in {str(item) for item in product.get("sizes", [])}:
                continue
            try:
                quantity = max(1, min(int(raw_item.get("quantity", 1)), 20))
            except (TypeError, ValueError):
                quantity = 1
            valid_items.append((product, size, quantity))
        if not valid_items:
            return {"ok": False, "error": "Некоторые вещи уже закончились. Обнови витрину и выбери снова."}
        self.db.upsert_user(user)
        return self.accept_checkout(user, customer, phone, valid_items, payload, notify_user=notify_user)

    def handle_web_app_data(self, chat_id: int, user: dict[str, Any], raw_data: str) -> None:
        """Accept an order sent by Telegram Web App ``sendData``.

        The browser is never trusted with the price or product details: only
        active product ids and sizes from the server-side catalog are accepted.
        This keeps the miniapp convenient while the bot remains the source of
        truth for the order and manager notification.
        """
        user_id = int(user["id"])
        try:
            payload = json.loads(raw_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.api.send_message(chat_id, "Не получилось прочитать заявку из витрины. Открой её ещё раз.", self.main_menu())
            return
        if not isinstance(payload, dict):
            self.api.send_message(chat_id, "Неизвестный формат заявки. Открой витрину заново.", self.main_menu())
            return
        if payload.get("type") == "waitlist":
            self.handle_web_waitlist(chat_id, user, payload)
            return
        if payload.get("type") == "profile":
            self.handle_web_profile(chat_id, user, payload)
            return
        if payload.get("type") != "order":
            self.api.send_message(chat_id, "Неизвестный формат заявки. Открой витрину заново.", self.main_menu())
            return
        if payload.get("consent") is not True:
            self.api.send_message(chat_id, "Без согласия на обработку данных заявку принять нельзя.", self.main_menu())
            return
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        phone = normalize_phone(str(customer.get("phone", "")))
        if not phone:
            self.api.send_message(chat_id, "Проверь номер телефона в витрине и отправь заявку ещё раз.", self.main_menu())
            return
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 20:
            self.api.send_message(chat_id, "В заявке нет вещей или их слишком много. Проверь корзину.", self.main_menu())
            return

        valid_items: list[tuple[dict[str, Any], str, int]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            product = self.catalog.get(str(raw_item.get("product_id", "")))
            size = str(raw_item.get("size", ""))
            if not product or size not in {str(item) for item in product.get("sizes", [])}:
                continue
            try:
                quantity = max(1, min(int(raw_item.get("quantity", 1)), 20))
            except (TypeError, ValueError):
                quantity = 1
            valid_items.append((product, size, quantity))
        if not valid_items:
            self.api.send_message(chat_id, "Некоторые вещи уже закончились. Обнови витрину и выбери снова.", self.main_menu())
            return

        result = self.accept_checkout(user, customer, phone, valid_items, payload, notify_user=chat_id)
        if not result.get("ok"):
            self.api.send_message(chat_id, str(result.get("error") or "Не получилось принять заявку."), self.main_menu())

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data", "")
        user = callback["from"]
        chat = callback["message"]["chat"]
        chat_id = chat["id"]
        user_id = int(user["id"])
        if chat.get("type") != "private":
            self.api.answer_callback(callback_id, "Открой бота в личных сообщениях")
            return
        self.db.upsert_user(user)
        self.api.answer_callback(callback_id)

        if data.startswith("pay:"):
            parts = data.split(":")
            if len(parts) == 3:
                self.start_method_pay(chat_id, user_id, parts[1], parts[2])
            return
        if data == "menu":
            self.api.send_message(chat_id, "Главное меню:", self.main_menu())
        elif data == "catalog":
            self.show_catalog(chat_id, user_id)
        elif data.startswith("intr:"):
            self.save_interest(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("cat:"):
            self.show_category(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("product:"):
            self.show_product(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("want:"):
            self.choose_size(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("size:"):
            _, product_id, size = data.split(":", 2)
            self.select_size(chat_id, user_id, product_id, size, f"callback:{callback_id}")
        elif data.startswith("wait:"):
            self.ask_waitlist_size(chat_id, data.split(":", 1)[1])
        elif data.startswith("wsize:"):
            _, product_id, size = data.split(":", 2)
            self.confirm_waitlist(chat_id, user_id, product_id, size)
        elif data == "profile":
            self.request_profile(chat_id, user_id)
        elif data == "consent:yes":
            self.accept_consent(chat_id, user_id)
        elif data == "consent:no":
            self.decline_consent(chat_id, user_id)
        elif data == "add:publish" and self.is_admin(user_id):
            self.publish_product(chat_id, user_id)
        elif data.startswith("addcat:") and self.is_admin(user_id):
            self.pick_add_category(chat_id, user_id, data.split(":", 1)[1])
        elif data == "referral":
            self.show_referral(chat_id, user_id)
        elif data == "lookbook":
            self.show_lookbook(chat_id, user_id)
        elif data == "about":
            self.api.send_message(
                chat_id,
                f"<b>{esc(self.settings.brand_name)}</b>\n\n"
                "Сила и честь. Одежда для своих: плотная, честная, без лишнего шума. "
                "Малые тиражи, городская форма, выпуски без повторов."
                + (f"\n\nВопросы: @{esc(self.settings.support_username)}" if self.settings.support_username else "")
                + self.cta("channel"),
                self.main_menu(),
            )
        elif data == "size_guide":
            self.api.send_message(
                chat_id,
                "<b>Как взять свой размер</b>\n\n"
                "S — рост 164–172, грудь 112\n"
                "M — рост 172–178, грудь 118\n"
                "L — рост 178–186, грудь 124\n"
                "XL — рост 186–194, грудь 130\n\n"
                "Посадка свободная. Между двумя — бери больший, если хочешь объём. "
                "Не уверен — оставь заявку, менеджер подскажет." + self.cta("size"),
                self.main_menu(),
            )
        elif data == "my_orders":
            self.show_my_orders(chat_id, user_id)
        elif data.startswith("ucancel:"):
            try:
                self.cancel_own_order(chat_id, user_id, int(data.split(":", 1)[1]))
            except ValueError:
                self.api.send_message(chat_id, "Некорректная заявка.", self.main_menu())
        elif data.startswith("seg:") and self.is_admin(user_id):
            self.preview_broadcast(chat_id, user_id, data.split(":", 1)[1])
        elif data == "admin:broadcast_confirm" and self.is_admin(user_id):
            self.confirm_broadcast(chat_id, user_id)
        elif data.startswith("order:") and self.is_admin(user_id):
            try:
                _, order_id, status = data.split(":", 2)
                self.update_order_status(chat_id, int(order_id), status)
            except (TypeError, ValueError):
                self.api.send_message(chat_id, "Некорректная команда для заявки.")
        elif data == "admin:cancel" and self.is_admin(user_id):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменено.")
        elif data.startswith("adm:") and self.is_admin(user_id):
            action = data.split(":", 1)[1]
            if action == "add":
                self.start_add_product(chat_id, user_id)
            elif action == "panel":
                self.admin_panel(chat_id)
            elif action == "stats":
                self.admin_command(chat_id, user_id, "/stats")
            elif action == "orders":
                self.admin_command(chat_id, user_id, "/orders")
            elif action == "waitlist":
                self.admin_command(chat_id, user_id, "/waitlist")
            elif action == "top":
                self.admin_command(chat_id, user_id, "/top")
            elif action == "export":
                self.admin_command(chat_id, user_id, "/export")
            elif action == "reload":
                self.admin_command(chat_id, user_id, "/reload")

    def handle_message(self, message: dict[str, Any]) -> None:
        if "from" not in message or "chat" not in message:
            return
        user = message["from"]
        user_id = int(user["id"])
        chat_id = int(message["chat"]["id"])
        if message["chat"].get("type") != "private":
            if str(message.get("text", "")).startswith("/start"):
                self.api.send_message(chat_id, "Открой бота в личке — там витрина и размеры.")
            return
        if message.get("successful_payment"):
            self.db.upsert_user(user)
            self.handle_successful_payment(chat_id, user_id, message["successful_payment"])
            return
        web_app_data = message.get("web_app_data")
        if isinstance(web_app_data, dict):
            self.db.upsert_user(user)
            self.handle_web_app_data(chat_id, user, str(web_app_data.get("data", "")))
            return
        text = str(message.get("text", "")).strip()
        if text.startswith("/start"):
            # start() decides whether the user is new, so it must run the upsert itself.
            self.start(chat_id, user, text.partition(" ")[2].strip())
            return
        self.db.upsert_user(user)
        if self.db.get_state(user_id) and self.handle_add_product_text(chat_id, user_id, text):
            return
        if text.startswith("/") and self.admin_command(chat_id, user_id, text):
            return
        if text in ("/menu", "/help"):
            if text == "/help":
                self.api.send_message(chat_id, self.help_text(), self.main_menu())
            else:
                self.api.send_message(chat_id, "Главное меню:", self.main_menu())
            return
        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменили.", remove_keyboard())
            self.api.send_message(chat_id, "Главное меню:", self.main_menu())
            return
        phone: str | None = None
        contact = message.get("contact")
        if contact and int(contact.get("user_id", user_id)) == user_id:
            phone = normalize_phone(str(contact.get("phone_number", "")))
        elif self.db.get_state(user_id):
            phone = normalize_phone(text)
        if phone:
            self.save_phone_and_continue(chat_id, user_id, phone)
            return
        if self.db.get_state(user_id):
            self.api.send_message(chat_id, "Номер не распознан. Формат: +79991234567")
            return
        self.api.send_message(
            chat_id,
            "Нажми кнопки ниже — так быстрее. Витрина, размеры и выпуск там." + self.cta("drop"),
            self.main_menu(),
        )

    def handle_update(self, update: dict[str, Any]) -> bool:
        try:
            if "pre_checkout_query" in update:
                self.handle_pre_checkout(update["pre_checkout_query"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update["message"])
            return True
        except Exception:
            LOG.exception("Failed to process update %s", update.get("update_id"))
            return False


class StorefrontHandler(BaseHTTPRequestHandler):
    """Serve the Telegram Mini App and a read-only catalog endpoint.

    Keeping the storefront on the same origin as the bot's health server means
    the browser never needs to call localhost or a second private service. The
    catalog endpoint exposes only active products; product validation for orders
    still happens in ``BrandBot.handle_web_app_data``.
    """

    catalog: Catalog | None = None
    settings: Settings | None = None
    db: Database | None = None
    api: TelegramAPI | None = None
    static_root = BASE_DIR / "miniapp"

    # Витрина живёт внутри Telegram и подключает только свои файлы плюс SDK
    # telegram.org. CSP — второй рубеж: в app.js много innerHTML, и если где-то
    # однажды забудут escapeHTML, инлайновый скрипт всё равно не выполнится.
    CSP_BASE = (
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    # Боевой режим пускает витрину в рамку только Telegram. В предпросмотре без
    # токена рамку не ограничиваем, иначе локальный iframe покажет пустоту.
    CSP_FRAME_TELEGRAM = "frame-ancestors https://web.telegram.org https://*.telegram.org"

    def _csp(self) -> str:
        settings = self.settings
        if settings and settings.token:
            return f"{self.CSP_BASE}; {self.CSP_FRAME_TELEGRAM}"
        return self.CSP_BASE

    def _write(self, body: bytes, content_type: str, status: int = 200, cache_control: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self._csp())
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._write(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            "no-store",
        )

    def _not_found(self) -> None:
        self._write(b"Not found", "text/plain; charset=utf-8", 404, "no-store")

    def _client_key(self) -> str:
        """Ключ лимитера: за прокси доверяем первому адресу X-Forwarded-For."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
        return str(self.client_address[0]) if self.client_address else "unknown"

    def _too_many(self) -> None:
        self._json({"error": "Слишком часто. Подожди немного и попробуй ещё раз."}, 429)

    def _rate_ok(self, limiter: RateLimiter, scope: str, user: dict[str, Any] | None = None) -> bool:
        """Считаем по user_id, если он подписан, иначе по адресу клиента."""
        who = f"user:{user['id']}" if user else f"ip:{self._client_key()}"
        if limiter.allow(f"{scope}:{who}"):
            return True
        LOG.warning("Превышен лимит %s для %s", scope, who)
        self._too_many()
        return False

    def _webapp_user(self) -> dict[str, Any] | None:
        raw = self.headers.get("X-Telegram-Init-Data") or ""
        if not raw:
            authorization = self.headers.get("Authorization") or ""
            if authorization.lower().startswith("tma "):
                raw = authorization[4:].strip()
        settings = self.settings
        if not settings or not settings.token or not raw:
            return None
        return webapp_user_from_init_data(settings.token, raw)

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > 8192:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _my_orders(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"orders": [], "source": "preview"})
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота — тогда подтянутся заявки."}, 401)
            return
        if not self._rate_ok(READ_LIMITER, "my-orders", user):
            return
        rows = self.db.orders_for_user(int(user["id"]), 20)
        self._json({"orders": [public_order_payload(row) for row in rows], "source": "bot"})

    def _cancel_my_order(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"error": "В предпросмотре заявки не отменяются."}, 400)
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота."}, 401)
            return
        if not self._rate_ok(CHECKOUT_LIMITER, "cancel", user):
            return
        payload = self._read_json_body() or {}
        try:
            order_id = int(payload.get("order_id"))
        except (TypeError, ValueError):
            self._json({"error": "Некорректная заявка."}, 400)
            return
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != int(user["id"]):
            self._json({"error": "Заявка не найдена."}, 404)
            return
        try:
            updated, changed = self.db.set_order_status(order_id, "cancelled")
        except ValueError:
            self._json({"error": "Эту заявку уже нельзя отменить."}, 409)
            return
        if not changed:
            self._json({"error": "Заявка уже отменена.", "order": public_order_payload(updated or order)}, 409)
            return
        if self.api and self.settings.manager_chat_id:
            try:
                self.api.send_message(
                    self.settings.manager_chat_id,
                    f"Клиент отменил заявку №{order_id}: {esc(updated['product_name'] if updated else '')}.",
                )
            except Exception as exc:
                LOG.warning("Could not notify manager about miniapp cancel %s: %s", order_id, exc)
        self._json({"ok": True, "order": public_order_payload(updated or order)})

    def _bot(self) -> BrandBot | None:
        if not self.settings or not self.db or not self.catalog or not self.api:
            return None
        return BrandBot(self.settings, self.api, self.db, self.catalog)

    def _read_raw(self, limit: int = 65536) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length) if length else b""

    def _mark_paid(self, payment_id: str, method: str, provider_id: str = "") -> bool:
        if not self.db or not payment_id:
            return False
        if not self.db.get_payment(payment_id):
            return False
        changed = self.db.mark_payment_paid(payment_id, method, provider_id)
        bot = self._bot()
        if changed and bot:
            bot.notify_paid(payment_id)
        return True

    def _resume_pay(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"error": "В предпросмотре оплата недоступна."}, 400)
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота."}, 401)
            return
        if not self._rate_ok(CHECKOUT_LIMITER, "pay", user):
            return
        payload = self._read_json_body() or {}
        payment_id = str(payload.get("payment_id") or "")[:32]
        bot = self._bot()
        if not bot:
            self._json({"error": "Оплата сейчас недоступна."}, 503)
            return
        result = bot.resume_payment(int(user["id"]), payment_id)
        if not result.get("ok"):
            self._json({"error": result.get("error") or "Не получилось открыть оплату."}, 400)
            return
        self._json(result)

    def _checkout(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"error": "В предпросмотре оплата недоступна."}, 400)
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота — тогда можно оплатить."}, 401)
            return
        if not self._rate_ok(CHECKOUT_LIMITER, "checkout", user):
            return
        payload = self._read_json_body()
        if not payload:
            self._json({"error": "Пустая заявка."}, 400)
            return
        bot = self._bot()
        if not bot:
            self._json({"error": "Оплата сейчас недоступна."}, 503)
            return
        result = bot.checkout_web_payload(user, payload, notify_user=int(user["id"]))
        if not result.get("ok"):
            self._json({"error": result.get("error") or "Не получилось принять заявку."}, 400)
            return
        self._json(result)

    def _lava_webhook(self) -> None:
        settings = self.settings
        if not settings or not settings.lava_ready():
            self._json({"error": "lava off"}, 404)
            return
        raw = self._read_raw()
        if raw is None:
            self._json({"error": "bad body"}, 400)
            return
        signature = (
            self.headers.get("Signature")
            or self.headers.get("Authorization")
            or self.headers.get("X-Signature")
            or ""
        )
        if signature.lower().startswith("bearer "):
            signature = signature[7:].strip()
        key = settings.lava_hook_key or settings.lava_secret_key
        if not verify_lava_webhook(raw, signature, key):
            self._json({"error": "bad sign"}, 403)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        if not isinstance(payload, dict):
            self._json({"error": "bad json"}, 400)
            return
        status = str(payload.get("status") or payload.get("invoice_status") or "").lower()
        if status not in {"success", "paid", "completed", "successed"}:
            self._json({"ok": True, "ignored": True})
            return
        payment_id = str(payload.get("order_id") or payload.get("orderId") or payload.get("custom_id") or "")
        invoice_id = str(payload.get("invoice_id") or payload.get("id") or "")
        if not self._mark_paid(payment_id, "lava", invoice_id):
            self._json({"error": "unknown payment"}, 404)
            return
        self._json({"ok": True})

    def _crypto_webhook(self) -> None:
        settings = self.settings
        if not settings or not settings.crypto_ready():
            self._json({"error": "crypto off"}, 404)
            return
        raw = self._read_raw()
        if raw is None:
            self._json({"error": "bad body"}, 400)
            return
        signature = self.headers.get("crypto-pay-api-signature") or self.headers.get("Crypto-Pay-API-Signature") or ""
        if not verify_crypto_webhook(raw, signature, settings.crypto_pay_token):
            self._json({"error": "bad sign"}, 403)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        if not isinstance(payload, dict):
            self._json({"error": "bad json"}, 400)
            return
        update_type = str(payload.get("update_type") or "")
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        if update_type and update_type != "invoice_paid":
            self._json({"ok": True, "ignored": True})
            return
        payment_id = str(inner.get("payload") or inner.get("order_id") or "")
        invoice_id = str(inner.get("invoice_id") or "")
        if not self._mark_paid(payment_id, "crypto", invoice_id):
            self._json({"error": "unknown payment"}, 404)
            return
        self._json({"ok": True})

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._write(b'{"status":"ok","service":"vorozhbitov-shop"}', "application/json; charset=utf-8")
            return
        if path == "/api/my-orders":
            self._my_orders()
            return
        if path == "/api/catalog":
            catalog = self.catalog
            settings = self.settings
            if not catalog:
                self._not_found()
                return
            if not self._rate_ok(READ_LIMITER, "catalog"):
                return
            with catalog.lock:
                payload = {
                    "brand": catalog.data.get("brand", {"name": settings.brand_name if settings else "ВОРОЖБИТОВ"}),
                    "channel_url": settings.channel_url if settings else "",
                    "privacy_url": settings.privacy_url if settings else "",
                    "products": [
                        product for product in catalog.data.get("products", []) if product.get("active", True)
                    ],
                    "categories": catalog.categories,
                    "lookbook": catalog.data.get("lookbook", []),
                }
            self._write(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        relative = path.lstrip("/")
        if relative in ("", "app", "app/"):
            relative = "index.html"
        elif relative.startswith("miniapp/"):
            relative = relative.removeprefix("miniapp/") or "index.html"
        candidate = (self.static_root / relative).resolve()
        try:
            candidate.relative_to(self.static_root.resolve())
        except ValueError:
            self._not_found()
            return
        if not candidate.is_file():
            self._not_found()
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._not_found()
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        cache = "public, max-age=3600" if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".svg"} else "no-cache"
        self._write(body, content_type, 200, cache)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/my-orders/cancel":
            self._cancel_my_order()
            return
        if path == "/api/checkout":
            self._checkout()
            return
        if path == "/api/pay":
            self._resume_pay()
            return
        if path == "/api/payments/lava":
            self._lava_webhook()
            return
        if path == "/api/payments/crypto":
            self._crypto_webhook()
            return
        self._not_found()

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            # Флаг обязан жить ровно один запрос: если когда-нибудь включат
            # HTTP/1.1 с keep-alive, иначе следующий ответ уйдёт без тела.
            self._head_only = False

    def finish(self) -> None:
        """Отдать соединение SQLite до того, как поток запроса умрёт."""
        try:
            super().finish()
        finally:
            if self.db is not None:
                self.db.close_current()

    def log_message(self, format: str, *args: Any) -> None:
        return


# Backwards-compatible name for health-check imports in small deployments.
HealthHandler = StorefrontHandler


def start_health_server(
    port: int,
    catalog: Catalog | None = None,
    settings: Settings | None = None,
    db: Database | None = None,
    api: TelegramAPI | None = None,
) -> ThreadingHTTPServer:
    handler = type(
        "ConfiguredStorefrontHandler",
        (StorefrontHandler,),
        {"catalog": catalog, "settings": settings, "db": db, "api": api},
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, name="storefront-server", daemon=True).start()
    return server


def polling_loop(api: TelegramAPI, bot: BrandBot) -> None:
    offset = 0
    retry_delay = 1
    while not STOP_EVENT.is_set():
        try:
            updates = api.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 10,
                    "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
                },
                timeout=15,
            )
            retry_delay = 1
            for update in updates:
                if not bot.handle_update(update):
                    LOG.warning("Skipping failed update %s", update.get("update_id"))
                offset = max(offset, int(update["update_id"]) + 1)
        except Exception as exc:
            LOG.warning("Polling error: %s", exc)
            STOP_EVENT.wait(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env()
    except (TypeError, ValueError) as exc:
        LOG.error("Invalid configuration: %s", exc)
        return 2
    if not settings.token:
        LOG.error("BOT_TOKEN is not configured. Copy .env.example to .env and set the token.")
        return 2
    if not (1 <= settings.health_port <= 65535):
        LOG.error("PORT must be between 1 and 65535")
        return 2
    if not settings.channel_url.startswith(("https://", "http://", "tg://")):
        LOG.error("CHANNEL_URL must be a valid HTTP(S) or tg:// URL")
        return 2
    if not settings.admin_ids:
        LOG.warning("ADMIN_IDS пуст — админ-панель и /add никому не доступны. Задай ID в .env")
    if settings.manager_chat_id is None:
        LOG.warning("MANAGER_CHAT_ID не задан — уведомления о заявках никуда не уйдут")
    try:
        ensure_catalog_exists(settings.catalog_path, BASE_DIR / "catalog.json")
        catalog = Catalog(settings.catalog_path)
        db = Database(settings.database_path)
        api = TelegramAPI(settings.token)
        identity = api.call("getMe")
        api.call("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        LOG.error("Startup failed: %s", exc)
        return 1
    bot = BrandBot(settings, api, db, catalog)
    bot.bot_username = str(identity.get("username") or "")
    LOG.info("Starting @%s for brand %s", bot.bot_username, settings.brand_name)
    try:
        user_commands = [
            {"command": "start", "description": "Открыть витрину"},
            {"command": "menu", "description": "Главное меню"},
            {"command": "help", "description": "Как это работает"},
        ]
        api.call("setMyCommands", {"commands": user_commands})
        try:
            api.call("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION})
            api.call("setMyDescription", {"description": BOT_DESCRIPTION})
        except Exception as exc:
            LOG.warning("Could not set bot profile description: %s", exc)
        admin_commands = user_commands + [
            {"command": "admin", "description": "Управление"},
            {"command": "stats", "description": "Статистика"},
            {"command": "orders", "description": "Заявки"},
            {"command": "add", "description": "Добавить вещь"},
        ]
        for admin_id in settings.admin_ids:
            api.call(
                "setMyCommands",
                {"commands": admin_commands, "scope": {"type": "chat", "chat_id": admin_id}},
            )
        if settings.webapp_url.startswith("https://"):
            api.call(
                "setChatMenuButton",
                {
                    "menu_button": {
                        "type": "web_app",
                        "text": "Витрина",
                        "web_app": {"url": settings.webapp_url},
                    }
                },
            )
    except Exception as exc:
        LOG.warning("Could not set Telegram commands or menu button: %s", exc)
    health_server = start_health_server(settings.health_port, catalog, settings, db, api)

    def stop(*_: Any) -> None:
        STOP_EVENT.set()
        health_server.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        polling_loop(api, bot)
    finally:
        health_server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
