#!/usr/bin/env python3
"""Telegram storefront bot for a small clothing brand.

Thematic build: bold brand voice, hard contextual CTAs, channel-first funnel,
referral/giveaway viral loop, size waitlist, segmented broadcasts.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import csv
from collections import OrderedDict
import hashlib
import hmac
import html
import io
import ipaddress
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from payments import (
    PriceError,
    create_crypto_invoice,
    create_lava_invoice,
    format_rub,
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
        self.hits: OrderedDict[str, list[float]] = OrderedDict()
        self.max_keys = 4096
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        stamp = now if now is not None else time.monotonic()
        edge = stamp - self.window
        with self.lock:
            # Keys are ordered by last accepted hit, so expired entries are a prefix.
            while self.hits and next(iter(self.hits.values()))[-1] <= edge:
                self.hits.popitem(last=False)
            recent = [hit for hit in self.hits.get(key, ()) if hit > edge]
            if len(recent) >= self.limit:
                return False
            if key not in self.hits and len(self.hits) >= self.max_keys:
                return False
            recent.append(stamp)
            self.hits[key] = recent
            self.hits.move_to_end(key)
            return True


# Заявка — дорогая операция (счёт у провайдера, сообщения). Чтение своих
# заявок опрашивается витриной раз в 4 секунды, поэтому лимит выше.
CHECKOUT_LIMITER = RateLimiter(limit=8, window_seconds=60)
READ_LIMITER = RateLimiter(limit=60, window_seconds=60)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".webm", ".mov"}
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
REF_RE = re.compile(r"^ref(\d{3,15})$")
# Telegram отказывается принимать файлы крупнее 50 МБ загрузкой по HTTP.
TEASER_UPLOAD_LIMIT = 50 * 1024 * 1024
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
    trusted_proxy_ips: frozenset[str] = frozenset()

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
            trusted_proxy_ips=frozenset(str(ipaddress.ip_address(item.strip()))
                                        for item in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if item.strip()),
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
        data, products = self.parse(raw)
        with self.lock:
            self.data = data
            self.products_by_id = products

    def parse(self, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Проверить каталог и вернуть готовые структуры, ничего не меняя.

        Проверка отделена от записи намеренно: сначала убеждаемся, что каталог
        целый, и только потом подменяем его в памяти и на диске. Иначе неудачная
        публикация оставляла испорченный файл, с которым бот не запускался, а
        память и диск расходились.
        """
        if not isinstance(raw.get("categories"), list) or not isinstance(raw.get("products"), list):
            raise ValueError("catalog.json must contain categories and products arrays")
        category_ids: set[str] = set()
        for category in raw["categories"]:
            if not isinstance(category, dict) or not all(isinstance(category.get(key), str) for key in ("id", "name")):
                raise ValueError(f"Invalid category: {category}")
            category_id = category["id"]
            if not category_id or ":" in category_id or len(category_id.encode("utf-8")) > MAX_CATEGORY_ID_BYTES:
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
            if not product_id or ":" in product_id or len(product_id.encode("utf-8")) > MAX_PRODUCT_ID_BYTES:
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
                if ":" in str(size) or len(callback.encode("utf-8")) > CALLBACK_DATA_LIMIT:
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
        return raw, products

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
        # Каталог — это склад. Без fsync обрыв питания может оставить переименование
        # с пустым файлом внутри, и бот больше не запустится: каталог не прочитается.
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def unique_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        index = 2
        while candidate in self.products_by_id:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _commit(self, candidate: dict[str, Any]) -> None:
        """Проверить кандидата и только потом записать его в память и на диск."""
        data, products = self.parse(candidate)
        self.data = data
        self.products_by_id = products
        self.save()

    def add_category(self, name: str) -> str:
        """Создать раздел и вернуть его id."""
        with self.lock:
            base = slugify(name, "category")
            existing = {category["id"] for category in self.data["categories"]}
            category_id = base
            index = 2
            while category_id in existing:
                category_id = f"{base}-{index}"
                index += 1
            self._commit({
                **self.data,
                "categories": [*self.data["categories"], {"id": category_id, "name": name}],
            })
            return category_id

    def add_product(self, product: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            product = dict(product)
            product["id"] = self.unique_id(str(product["name"]))
            product.setdefault("active", True)
            product.setdefault("photo_url", "")
            self._commit({**self.data, "products": [*self.data["products"], product]})
            return product

    def set_active(self, product_id: str, active: bool) -> bool:
        with self.lock:
            products = []
            found = False
            for product in self.data["products"]:
                item = dict(product)
                if str(item.get("id")) == product_id:
                    item["active"] = active
                    found = True
                products.append(item)
            if not found:
                return False
            self._commit({**self.data, "products": products})
            return True


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.local = threading.local()
        self.invoice_locks = [threading.Lock() for _ in range(64)]
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
                notified_at TEXT,
                UNIQUE(user_id, product_id, size)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS checkouts (
                user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                receipt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS payment_methods (
                payment_id TEXT PRIMARY KEY,
                methods TEXT NOT NULL,
                valid_until REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                markup TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
            CREATE INDEX IF NOT EXISTS idx_users_interest ON users(interest);
            CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payments(user_id, status);
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
        waitlist_columns = {row[1] for row in conn.execute("PRAGMA table_info(waitlist)")}
        if "notified_at" not in waitlist_columns:
            conn.execute("ALTER TABLE waitlist ADD COLUMN notified_at TEXT")

    def upsert_user(self, telegram_user: dict[str, Any], source: str | None = None) -> tuple[bool, int | None]:
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = self._upsert_user(telegram_user, source)
            conn.execute("COMMIT")
            return result
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _upsert_user(self, telegram_user: dict[str, Any], source: str | None = None) -> tuple[bool, int | None]:
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
                    # Колонки NOT NULL: явный null из подписанного initData или
                    # из служебного сообщения не должен ронять запрос целиком.
                    str(telegram_user.get("first_name") or ""),
                    str(telegram_user.get("last_name") or ""),
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
                    str(telegram_user.get("first_name") or ""),
                    str(telegram_user.get("last_name") or ""),
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

    def checkout_receipt(self, user_id: int, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        row = self.connection().execute(
            "SELECT fingerprint, receipt FROM checkouts WHERE user_id=? AND request_id=?",
            (user_id, request_id),
        ).fetchone()
        if not row:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("Эта покупка уже принята с другим составом. Собери новую.")
        return json.loads(row["receipt"])

    def create_checkout(
        self, user_id: int, request_id: str, fingerprint: str,
        lines: list[dict[str, Any]], amount_stars: int,
        queue_notifications: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Commit one immutable receipt, payment and all order lines together."""
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = self.checkout_receipt(user_id, request_id, fingerprint)
            if previous is not None:
                conn.execute("COMMIT")
                return previous, False
            if not lines or any(int(line["amount_rub"]) <= 0 for line in lines):
                raise ValueError("Цена вещи недоступна. Обнови витрину или напиши менеджеру.")
            payment_id = secrets.token_hex(12)
            total = sum(int(line["amount_rub"]) for line in lines)
            now = utc_now()
            self.create_payment(payment_id, user_id, total, amount_stars)
            order_ids = []
            for index, line in enumerate(lines):
                cursor = conn.execute(
                    """INSERT INTO orders(request_id, user_id, product_id, product_name, size,
                       phone, quantity, note, status, payment_id, amount_rub, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)""",
                    (f"checkout:{payment_id}:{index}", user_id, line["product_id"], line["name"],
                     line["size"], line["phone"], line["quantity"], line["note"][:400],
                     payment_id, line["amount_rub"], now),
                )
                order_ids.append(int(cursor.lastrowid))
                self.event(user_id, "order_created", {"order_id": order_ids[-1], "product_id": line["product_id"]})
            # Размер получен — ждать больше нечего. Иначе следующий /restock
            # напишет человеку о размере, который он уже купил.
            self.drop_fulfilled_waitlist(user_id, lines)
            receipt = {
                "payment_id": payment_id, "amount_rub": total, "amount_label": format_rub(total),
                "amount_stars": amount_stars, "order_ids": order_ids,
                "lines": [{key: line[key] for key in ("name", "size", "quantity", "person", "person_label")} for line in lines],
            }
            conn.execute("INSERT INTO checkouts VALUES (?, ?, ?, ?, ?)",
                         (user_id, request_id, fingerprint, compact_json(receipt), now))
            self.clear_state(user_id)
            if queue_notifications:
                queue_notifications(receipt)
            conn.execute("COMMIT")
            return receipt, True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def payment_is_payable(self, payment_id: str) -> bool:
        row = self.connection().execute(
            """SELECT p.status, p.amount_rub, COUNT(o.id) AS items,
               COALESCE(SUM(o.amount_rub), 0) AS total,
               SUM(CASE WHEN o.status='awaiting_payment' AND o.amount_rub>0 THEN 0 ELSE 1 END) AS invalid
               FROM payments p LEFT JOIN orders o ON o.payment_id=p.payment_id
               WHERE p.payment_id=? GROUP BY p.payment_id""", (payment_id,),
        ).fetchone()
        return bool(row and row["status"] == "pending" and row["items"]
                    and not row["invalid"] and row["amount_rub"] == row["total"])

    def pending_payment(self, user_id: int, limit: int = 5) -> tuple[str, int] | None:
        """Незакрытая оплата пользователя — для кнопки «Продолжить оплату».

        Возвращает ``(payment_id, amount_rub)`` только если счёт ещё можно
        оплатить: отменённые и уже оплаченные покупки в меню не поднимаются.
        """
        rows = self.connection().execute(
            """SELECT payment_id, amount_rub FROM payments
               WHERE user_id=? AND status='pending'
               ORDER BY created_at DESC, payment_id DESC LIMIT ?""",
            (user_id, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            if self.payment_is_payable(str(row["payment_id"])):
                return str(row["payment_id"]), int(row["amount_rub"] or 0)
        return None

    def enqueue_message(self, key: str, chat_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
        self.connection().execute(
            "INSERT OR IGNORE INTO notifications(notification_id, chat_id, body, markup) VALUES (?, ?, ?, ?)",
            (key, chat_id, text, compact_json(markup) if markup else None),
        )

    def claim_notifications(self, now: float, key: str | None = None) -> list[sqlite3.Row]:
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                """SELECT * FROM notifications WHERE delivered_at IS NULL AND next_attempt_at<=?
                   AND (? IS NULL OR notification_id=?) ORDER BY next_attempt_at LIMIT 1""", (now, key, key),
            ))
            for row in rows:
                conn.execute("UPDATE notifications SET next_attempt_at=?, attempts=attempts+1 WHERE notification_id=?",
                             (now + 300, row["notification_id"]))
            conn.execute("COMMIT")
            return rows
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

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

    def mark_payment_paid(self, payment_id: str, method: str = "", provider_id: str = "", *, queue_notifications: Callable[[str], None] | None = None) -> bool:
        """Idempotent: pending → paid, matching orders awaiting_payment → paid."""
        conn = self.connection()
        now = utc_now()
        owns_transaction = not conn.in_transaction
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            if not row:
                if owns_transaction:
                    conn.execute("COMMIT")
                return False
            already = str(row["status"]) in {"paid", "refund_required"}
            needs_refund = not already and not self.payment_is_payable(payment_id)
            target_status = "refund_required" if needs_refund else "paid"
            if not already:
                conn.execute(
                    """
                    UPDATE payments
                    SET status=?, method=?, provider_id=?, paid_at=?
                    WHERE payment_id=?
                    """,
                    (target_status, str(method or row["method"] or "")[:32], str(provider_id or "")[:80], now, payment_id),
                )
            for order in conn.execute(
                "SELECT id, user_id, status FROM orders WHERE payment_id=?", (payment_id,)
            ):
                if already or needs_refund or str(order["status"]) != "awaiting_payment":
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
                    (row["user_id"], "payment_refund_required" if needs_refund else "payment_paid", compact_json({"payment_id": payment_id, "method": method}), now),
                )
            if not already and queue_notifications:
                queue_notifications(payment_id)
            if owns_transaction:
                conn.execute("COMMIT")
            return not already
        except Exception:
            if owns_transaction and conn.in_transaction:
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

    def claim_waitlist(self, product_id: str, size: str) -> list[int]:
        """Забрать неоповещённых из листа ожидания — по одному разу на запись.

        Отметка ставится до отправки и внутри одной транзакции, поэтому
        повторный или одновременный ``/restock`` не превращается в поток
        одинаковых сообщений одним и тем же людям.
        """
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                "SELECT user_id, id FROM waitlist WHERE product_id=? AND size=? AND notified_at IS NULL",
                (product_id, size),
            ))
            if rows:
                conn.execute(
                    "UPDATE waitlist SET notified_at=? WHERE product_id=? AND size=? AND notified_at IS NULL",
                    (utc_now(), product_id, size),
                )
            conn.execute("COMMIT")
            return [(row[0], row[1]) for row in rows]
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def release_waitlist(self, user_ids: list[int], product_id: str, size: str) -> None:
        """Снять отметку, если сообщение не ушло: человек всё ещё ждёт размер."""
        if not user_ids:
            return
        self.connection().executemany(
            "UPDATE waitlist SET notified_at=NULL WHERE user_id=? AND product_id=? AND size=?",
            [(user_id, product_id, size) for user_id in user_ids],
        )

    def drop_fulfilled_waitlist(self, user_id: int, lines: list[dict[str, Any]]) -> None:
        """Убрать из листа ожидания то, что человек только что купил."""
        pairs = [
            (user_id, str(line.get("product_id", "")), str(line.get("size", "")))
            for line in lines
            if line.get("product_id") and line.get("size")
        ]
        if pairs:
            self.connection().executemany(
                "DELETE FROM waitlist WHERE user_id=? AND product_id=? AND size=?", pairs
            )

    def waitlist_for_user(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """Что ждёт этот человек: без этого подписаться можно, а выйти нельзя."""
        return list(self.connection().execute(
            "SELECT id, product_id, product_name, size, notified_at FROM waitlist "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, int(limit))),
        ))

    def remove_waitlist_entry(self, entry_id: int, user_id: int) -> bool:
        """Снять запись — только свою: чужой лист ожидания кнопкой не трогается."""
        cursor = self.connection().execute(
            "DELETE FROM waitlist WHERE id=? AND user_id=?", (entry_id, user_id)
        )
        return cursor.rowcount > 0

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

    def is_admin_id(self, user_id: int) -> bool:
        row = self.connection().execute(
            "SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def add_admin(self, user_id: int, added_by: int) -> None:
        self.connection().execute(
            "INSERT INTO admins(user_id, added_by, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING", (user_id, added_by, utc_now()))

    def remove_admin(self, user_id: int) -> None:
        self.connection().execute("DELETE FROM admins WHERE user_id=?", (user_id,))

    def admin_rows(self) -> list[sqlite3.Row]:
        return list(self.connection().execute(
            """
            SELECT a.user_id, a.added_at, u.username, u.first_name
            FROM admins a LEFT JOIN users u ON u.user_id = a.user_id
            ORDER BY a.user_id
            """))

    def money_stats(self) -> dict[str, int]:
        """Деньги отдельно от работы: всего, сегодня, за неделю, средний чек."""
        conn = self.connection()
        today = utc_now()[:10]
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()[:10]
        paid = "SELECT COALESCE(SUM(amount_rub), 0), COUNT(*) FROM payments WHERE status='paid'"
        total, count = conn.execute(paid).fetchone()
        return {
            "paid_total": int(total or 0),
            "paid_count": int(count or 0),
            "paid_today": int(conn.execute(
                "SELECT COALESCE(SUM(amount_rub), 0) FROM payments "
                "WHERE status='paid' AND substr(paid_at, 1, 10)=?", (today,)).fetchone()[0] or 0),
            "paid_week": int(conn.execute(
                "SELECT COALESCE(SUM(amount_rub), 0) FROM payments "
                "WHERE status='paid' AND substr(paid_at, 1, 10)>=?", (week,)).fetchone()[0] or 0),
            "refunds": int(conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status='refund_required'").fetchone()[0] or 0),
        }

    def waitlist_unnotified(self) -> list[sqlite3.Row]:
        return list(self.connection().execute(
            "SELECT product_id, product_name, size, COUNT(*) AS c FROM waitlist "
            "WHERE notified_at IS NULL GROUP BY product_id, size ORDER BY c DESC"))

    def orders_new(self) -> int:
        row = self.connection().execute(
            "SELECT COUNT(*) FROM orders WHERE status='new'").fetchone()
        return int(row[0] or 0)

    def recent_broadcasts(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.connection().execute(
            "SELECT created_at, payload FROM events WHERE event='broadcast_sent' "
            "ORDER BY id DESC LIMIT ?", (limit,)))

    def recent_draws(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.connection().execute(
            "SELECT created_at, payload FROM events WHERE event='giveaway_drawn' "
            "ORDER BY id DESC LIMIT ?", (limit,)))

    def set_order_note(self, order_id: int, note: str) -> None:
        with self.lock, self.connection() as conn:
            conn.execute("UPDATE orders SET note=? WHERE id=?",
                         (str(note or "")[:400], order_id))

    def refunds_open(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.payment_id, p.amount_rub, p.created_at, o.id AS order_id, "
                "o.product_name, o.size, o.user_id FROM payments p "
                "JOIN orders o ON o.payment_id = p.payment_id "
                "WHERE p.status='refund_required' ORDER BY p.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def refunds_done(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.payment_id, p.amount_rub, p.paid_at, o.id AS order_id, "
                "o.product_name, o.user_id FROM payments p "
                "JOIN orders o ON o.payment_id = p.payment_id "
                "WHERE p.status='refunded' ORDER BY p.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def kv_get(self, key: str) -> str:
        row = self.connection().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def kv_set(self, key: str, value: str) -> None:
        self.connection().execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utc_now()),
        )

    def kv_delete(self, key: str) -> None:
        self.connection().execute("DELETE FROM kv WHERE key = ?", (key,))

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
            # Сводка владельца различает деньги и работу, поэтому считаем отдельно.
            "awaiting_payment": conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status='pending'"
            ).fetchone()[0],
            "paid": conn.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0],
            "paid_amount": conn.execute(
                "SELECT COALESCE(SUM(amount_rub), 0) FROM payments WHERE status='paid'"
            ).fetchone()[0],
            "refunds": conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status='refund_required'"
            ).fetchone()[0],
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

    def set_order_status(self, order_id: int, status: str, *, customer_id: int | None = None, queue_paid: Callable[[str], None] | None = None) -> tuple[sqlite3.Row | None, bool]:
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
            if customer_id is not None:
                if int(row["user_id"]) != customer_id or status != "cancelled":
                    raise ValueError("Покупка не найдена.")
                if current not in {"new", "awaiting_payment", "cancelled"}:
                    raise ValueError("После оплаты отмена возможна только через менеджера.")
            if current == status:
                conn.execute("COMMIT")
                return row, False
            if status not in transitions.get(current, set()):
                conn.execute("COMMIT")
                raise ValueError(f"Invalid order transition: {current} -> {status}")
            if status == "paid" and row["payment_id"]:
                if not self.payment_is_payable(row["payment_id"]):
                    raise ValueError("Эта оплата требует сверки с менеджером.")
                self.mark_payment_paid(row["payment_id"], "manual", queue_notifications=queue_paid)
                conn.execute("COMMIT")
                return self.get_order(order_id), True
            if status == "cancelled" and row["payment_id"]:
                payment = self.get_payment(row["payment_id"])
                if payment:
                    if customer_id is not None and payment["status"] != "pending":
                        raise ValueError("Оплата уже обработана. Напиши менеджеру.")
                    if payment["status"] == "pending":
                        siblings = list(conn.execute("SELECT id, status FROM orders WHERE payment_id=?", (row["payment_id"],)))
                        if any(item["status"] != "awaiting_payment" for item in siblings):
                            raise ValueError("Состав оплаты изменился. Напиши менеджеру.")
                        for item in siblings:
                            if item["id"] != order_id:
                                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (item["id"],))
                                self.event(row["user_id"], "order_status_changed", {"order_id": item["id"], "status": "cancelled"})
                        conn.execute("UPDATE payments SET status='cancelled' WHERE payment_id=?", (row["payment_id"],))
                    elif payment["status"] == "paid":
                        conn.execute("UPDATE payments SET status='refund_required' WHERE payment_id=?", (row["payment_id"],))
                        self.event(row["user_id"], "payment_refund_required", {"payment_id": row["payment_id"]})
                        if queue_paid:
                            queue_paid(row["payment_id"])
                    conn.execute("DELETE FROM payment_methods WHERE payment_id=?", (row["payment_id"],))
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
        text = fit_html_text(text, MESSAGE_TEXT_LIMIT - 96)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def edit_message(self, chat_id: int, message_id: int, text: str,
                     reply_markup: dict[str, Any] | None = None) -> bool:
        """Поменять экран на месте. ``False`` — значит, правка не удалась.

        Так навигация не превращает чат в ленту из девяти сообщений: экран, чью
        кнопку нажали, становится следующим экраном. Не выходит — отправим новым
        сообщением, поэтому отказ здесь не ошибка, а сигнал вызывающему.
        """
        if not message_id:
            return False
        if reply_markup is not None and "inline_keyboard" not in reply_markup:
            # editMessageText принимает только inline-клавиатуру: обычная
            # клавиатура («Отправить номер») или её снятие требуют нового сообщения.
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": fit_html_text(text, MESSAGE_TEXT_LIMIT - 96),
            "parse_mode": "HTML",
            # Пустая inline-клавиатура снимает старые кнопки экрана.
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        try:
            self.call("editMessageText", payload)
        except RuntimeError as exc:
            if "message is not modified" in str(exc).lower():
                return True  # экран тот же — для человека ничего не изменилось
            LOG.info("Экран не правится на месте (chat %s, message %s): %s", chat_id, message_id, exc)
            return False
        return True

    def send_photo(self, chat_id: int, photo: str, caption: str, reply_markup: dict[str, Any] | None = None) -> Any:
        caption = fit_html_text(caption, CAPTION_LIMIT - 24)
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
        caption = fit_html_text(caption, CAPTION_LIMIT - 24)
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

    def send_video(
        self,
        chat_id: int,
        video: str | Path,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
        thumbnail: Path | None = None,
        width: int = 0,
        height: int = 0,
        duration: int = 0,
    ) -> Any:
        """Send a video: a ``file_id``/HTTPS URL as JSON, a local ``Path`` as multipart.

        Uploading a few megabytes on every ``/start`` is wasteful, so callers
        cache the returned ``file_id`` and pass it next time.
        """
        caption = fit_html_text(caption, CAPTION_LIMIT - 24)
        fields: dict[str, str] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        for name, value in (("width", width), ("height", height), ("duration", duration)):
            if value:
                fields[name] = str(int(value))
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))

        if not isinstance(video, Path):
            payload: dict[str, Any] = dict(fields)
            payload["chat_id"] = chat_id
            payload["video"] = str(video)
            payload["supports_streaming"] = True
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return self.call("sendVideo", payload)

        boundary = "----VorozhbitovVideo7MA4YWxk"
        files: list[tuple[str, Path]] = [("video", video)]
        if thumbnail is not None and thumbnail.is_file():
            fields["thumbnail"] = "attach://thumb"
            files.append(("thumb", thumbnail))
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
        for name, path in files:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode(),
                    f"Content-Type: {mime}\r\n\r\n".encode(),
                    path.read_bytes(),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self.base_url + "sendVideo",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result.get("result")

    def send_media_group(self, chat_id: int, photos: list[str], caption: str = "", labels: list[str] | None = None) -> Any:
        """Альбом. ``labels`` — честные подписи кадров, если они есть в каталоге."""
        media = []
        for index, photo in enumerate(photos[:10]):
            item: dict[str, Any] = {"type": "photo", "media": photo}
            label = str(labels[index]).strip() if labels and index < len(labels) else ""
            if label:
                item["caption"] = esc(label)
                item["parse_mode"] = "HTML"
            elif index == 0 and caption:
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


# Ошибки Telegram, после которых повторять доставку бессмысленно.
BLOCKED_DELIVERY_MARKERS = (
    "bot was blocked",
    "blocked by the user",
    "user is deactivated",
    "chat not found",
    "have no rights to send a message",
    "bot can't initiate conversation",
)
UNDELIVERABLE_MARKERS = (
    "message is too long",
    "can't parse entities",
    "can't parse message entities",
    "wrong type of the web page content",
)

MESSAGE_TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
BUTTON_TEXT_LIMIT = 64
_HTML_TAG_RE = re.compile(
    r"</?(b|i|u|s|span|code|pre|a|em|strong|blockquote|tg-spoiler)\b[^>]*>"
)


def fit_html_text(text: Any, limit: int) -> str:
    """Укорачивает HTML-текст под лимит Telegram, не ломая разметку.

    Обрезка посреди тега или экранированной сущности (``&amp;`` → ``&am``)
    даёт «Can't parse entities»: Telegram отклоняет сообщение целиком, а очередь
    доставки повторяет попытку без конца. Поэтому режем по границе строки или
    тега и закрываем то, что осталось незакрытым.
    """
    value = str(text or "")
    if len(value) <= limit:
        return value
    reserve = 48  # многоточие и закрывающие теги
    cut = value[:max(1, limit - reserve)]
    newline = cut.rfind("\n")
    if newline >= limit // 2:
        cut = cut[:newline]
    else:
        opened, closed = cut.rfind("<"), cut.rfind(">")
        if opened > closed:
            cut = cut[:opened]  # не резать внутри тега
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:amp + 10]:
        cut = cut[:amp]  # не рвать сущность
    stack: list[str] = []
    for match in _HTML_TAG_RE.finditer(cut):
        tag, name = match.group(0), match.group(1)
        if tag.startswith("</"):
            if name in stack:
                stack.remove(name)
        elif not tag.endswith("/>"):
            stack.append(name)
    tail = "".join(f"</{name}>" for name in reversed(stack))
    return f"{cut.rstrip()}…{tail}"[:limit]


def button_label(text: Any, limit: int = BUTTON_TEXT_LIMIT) -> str:
    """Подпись кнопки в пределах лимита Telegram.

    Длинное название вещи из каталога ломает не кнопку, а всю клавиатуру:
    сообщение с подписью длиннее 64 символов Telegram отклоняет целиком.
    """
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[:limit - 1].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut + "…"


def inline_keyboard(rows: Iterable[Iterable[tuple[str, str]]]) -> dict[str, Any]:
    keyboard = []
    for row in rows:
        buttons = []
        for label, target in row:
            label = button_label(label)
            if target.startswith("webapp:"):
                buttons.append({"text": label, "web_app": {"url": target.removeprefix("webapp:")}})
                continue
            key = "url" if target.startswith(("https://", "http://", "tg://")) else "callback_data"
            buttons.append({"text": label, key: target})
        keyboard.append(buttons)
    return {"inline_keyboard": keyboard}


def consent_keyboard() -> dict[str, Any]:
    """Согласие выбирается кнопкой — и только так: текстом его не принять."""
    return inline_keyboard(
        [[(f"{ICON['ok']} Согласен", "consent:yes")], [(f"{ICON['cancel']} Не сейчас", "consent:no")]]
    )


def contact_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": "Отправить номер", "request_contact": True}], [{"text": "Отмена"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard() -> dict[str, Any]:
    return {"remove_keyboard": True}


# ---------------------------------------------------------------------------
# Native-меню Telegram: словарь знаков, каркас экрана, карточка покупки.
#
# Правило простое: один смысл — один знак. «📦» всегда про покупки, «📐» —
# про размер, «←» — назад, «↗» — уходим из чата. Цвет и форму кнопок рисует
# сам Telegram, поэтому красота здесь — из структуры и коротких подписей,
# а не из оформления.
# ---------------------------------------------------------------------------

ICON = {
    "catalog": "🛍",
    "looks": "📷",
    "film": "🎬",
    "orders": "📦",
    "account": "👤",
    "support": "💬",
    "pay": "💳",
    "size": "📐",
    "wait": "⏳",
    "channel": "📣",
    "fabric": "🧵",
    "cut": "✂️",
    "ruler": "📏",
    "stock": "🏷",
    "delivery": "🚚",
    "receipt": "🧾",
    "question": "❓",
    "notice": "🔔",
    "link": "🔗",
    "info": "ℹ️",
    "stats": "📊",
    "trophy": "🏆",
    "export": "📤",
    "reload": "🔄",
    "tools": "🛠",
    "home": "🏠",
    "back": "←",
    "outside": "↗",
    "ok": "✅",
    "cancel": "✖",
}


def icon(kind: str, label: str) -> str:
    """Подпись кнопки: знак из общего словаря + короткий текст."""
    return f"{ICON[kind]} {label}"


def nav_rows(back: tuple[str, str] | None = None) -> list[list[tuple[str, str]]]:
    """Нижняя строка экрана: «Назад» и «Главная».

    «Назад» ведёт туда, откуда человек действительно пришёл, а «Главная»
    всегда последняя кнопка последней строки — её не приходится искать.
    Если назад и есть главное меню, вторая кнопка не добавляется.
    """
    if not back:
        return [[(icon("home", "Главное меню"), "menu")]]
    label, target = back
    if target == "menu":
        return [[(f"{ICON['back']} {label}", target)]]
    return [[(f"{ICON['back']} {label}", target), (icon("home", "Главная"), "menu")]]


def channel_target(url: Any) -> str:
    """Ссылка на канал, если по ней действительно есть канал.

    Пустое значение и дефолтный ``https://t.me/`` делают кнопку «Канал» битой:
    Telegram отклоняет такое сообщение целиком, и покупатель не получает ни
    экрана, ни кнопки — вместо «пустой витрины» выходит тишина.
    """
    value = str(url or "").strip()
    if value.startswith("tg://"):
        return value if "domain=" in value else ""
    if not value.startswith(("https://", "http://")):
        return ""
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if not host:
        return ""
    if host in {"t.me", "telegram.me", "telegram.dog"} and not parsed.path.strip("/"):
        return ""
    return value


def staff_nav_rows() -> list[list[tuple[str, str]]]:
    """Нижний ряд служебного экрана: обратно в пульт или в режим покупателя.

    Без него справочные экраны команды заканчивались тупиком — вернуться
    можно было только командой с клавиатуры.
    """
    return [[(icon("tools", "Управление"), "adm:panel"), (icon("account", "Режим покупателя"), "menu")]]


def option_rows(options: Iterable[tuple[str, str]], per_row: int = 2) -> list[list[tuple[str, str]]]:
    """Равноправные варианты — в несколько колонок.

    Приём один и для главного меню, и для разделов витрины, и для служебных
    списков: выбор не меняется, а экран становится вдвое короче.
    """
    items = list(options)
    width = max(1, int(per_row))
    return [items[index:index + width] for index in range(0, len(items), width)]


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русская считалка: 1 вещь, 2 вещи, 5 вещей."""
    if 11 <= count % 100 <= 14:
        return many
    tail = count % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def things_word(count: int) -> str:
    """«1 вещь · 2 вещи · 5 вещей» — для подписей разделов витрины."""
    return plural(count, "вещь", "вещи", "вещей")


SEGMENT_LABELS = {
    "consent": "Дали согласие",
    "contacts": "С номером",
    "buyers": "Уже покупали",
    "fresh": "Новые без номера",
    "all": "Вся база",
}


def audience_label(segment: str, categories: Iterable[dict[str, str]]) -> str:
    """Название сегмента для владельца: без служебных кодов вида interest:hoodie."""
    if segment in SEGMENT_LABELS:
        return SEGMENT_LABELS[segment]
    if segment.startswith("interest:"):
        wanted = segment.split(":", 1)[1]
        name = next((str(item["name"]) for item in categories if item["id"] == wanted), "")
        return f"Интерес: {name}" if name else "Интерес: раздел удалён"
    return "Сегмент не распознан"


REFUND_REASON_LABELS = {
    "size": "Не подошёл размер",
    "changed": "Покупатель передумал",
    "defect": "Брак или дефект",
    "other": "Другое",
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
    "completed": "Покупка закрыта. Спасибо, что выбрал этот выпуск.",
    "cancelled": "Покупка отменена. Если это ошибка — собери новую из карточки вещи.",
}

PAYMENT_METHOD_LABELS = {
    "lava": "карта / СБП",
    "crypto": "крипта",
    "stars": "звёзды Telegram",
    "manual": "подтверждена менеджером",
}

# Карточка покупки отвечает на три вопроса отдельными строками: деньги,
# работа, отправка. Сборка и доставка описывают реальный этап, а не
# выдуманный номер накладной: источника трекинга у нас нет.
ORDER_WORK_LINES = {
    "new": "не начата",
    "awaiting_payment": "не начата",
    "paid": "проверяем наличие",
    "confirmed": "собираем к отправке",
    "completed": "собрана",
    "cancelled": "не собираем",
}

ORDER_DELIVERY_LINES = {
    "new": "согласуем после оплаты",
    "awaiting_payment": "согласуем после оплаты",
    "paid": "условия согласует менеджер",
    "confirmed": "готовим к отправке",
    "completed": "покупка закрыта",
    "cancelled": "не отправляем",
}


def payment_state_line(status: str, payment: Any, amount_rub: int) -> str:
    """Строка «Оплата» для карточки покупки — только по платёжной записи.

    Статус покупки здесь не источник правды: «подтверждена» возможна и до
    оплаты, поэтому деньги смотрим в payments.
    """
    amount = format_rub(amount_rub) if amount_rub > 0 else ""
    if payment is None:
        # Счёта в боте нет: состояние денег неизвестно, придумывать его нельзя.
        return "отменена" if status == "cancelled" else "уточняется"
    state = str(payment["status"])
    if state == "paid":
        method = PAYMENT_METHOD_LABELS.get(str(payment["method"] or ""), "")
        paid = f", {method}" if method else ""
        return f"оплачена · {amount}{paid}" if amount else f"оплачена{paid}"
    if state == "refund_required":
        return "требует возврата — менеджер свяжется"
    if state == "cancelled":
        return "отменена"
    if status in {"new", "awaiting_payment"}:
        return f"ждёт оплаты · {amount}" if amount else "ждёт оплаты"
    if status in {"paid", "confirmed", "completed"}:
        return f"оплачена · {amount}" if amount else "оплачена"
    return "отменена"


def order_state_lines(status: str, payment: Any, amount_rub: int) -> list[str]:
    """Три строки состояния покупки: деньги, сборка, доставка."""
    return [
        f"{ICON['pay']} Оплата: {esc(payment_state_line(status, payment, amount_rub))}",
        f"{ICON['fabric']} Сборка: {esc(ORDER_WORK_LINES.get(status, status))}",
        f"{ICON['delivery']} Доставка: {esc(ORDER_DELIVERY_LINES.get(status, status))}",
    ]


def buyer_commands() -> list[dict[str, str]]:
    """Команды покупателя — их открывает системная кнопка «Меню»."""
    return [
        {"command": "start", "description": "Главное меню"},
        {"command": "catalog", "description": "Каталог вещей"},
        {"command": "orders", "description": "Мои покупки"},
        {"command": "support", "description": "Поддержка"},
        {"command": "help", "description": "Как это работает"},
    ]


def staff_commands() -> list[dict[str, str]]:
    """Команды команды. Вешаются отдельным scope — покупатель их не видит."""
    return [
        {"command": "start", "description": "Главное меню"},
        {"command": "admin", "description": "Управление магазином"},
        {"command": "stats", "description": "Сводка"},
        {"command": "orders", "description": "Все покупки"},
        {"command": "add", "description": "Добавить вещь"},
        {"command": "broadcast", "description": "Рассылка: /broadcast текст"},
        {"command": "access", "description": "Доступ команды"},
        {"command": "draws", "description": "История розыгрышей"},
        {"command": "reports", "description": "Журнал рассылок"},
        {"command": "money", "description": "Деньги: выручка и чек"},
        {"command": "digest", "description": "Дайджест: что сегодня"},
        {"command": "shelf", "description": "Витрина: скрыть и показать"},
        {"command": "refunds", "description": "Возвраты: причины и статус"},
        {"command": "again", "description": "Повторить прошлую рассылку"},
        {"command": "grant", "description": "Назначить админа: /grant id"},
        {"command": "help", "description": "Как это работает"},
    ]


# Кнопка «Меню» в шапке чата показывает команды бота. Витрина остаётся
# отдельной кнопкой внутри чата — системная кнопка не обязана уводить из диалога.
CHAT_MENU_BUTTON: dict[str, Any] = {"type": "commands"}

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


def person_label(product: dict[str, Any]) -> str:
    config = product.get("personalization")
    if isinstance(config, dict) and config.get("label"):
        return str(config["label"])
    return "ПЕРСОНАЛИЗАЦИЯ"


def sanitize_personalization(product: dict[str, Any], raw: Any) -> str:
    """Проверить персонализацию (номер жетона) по правилам из каталога.

    Витрине доверять нельзя: паттерн и сам факт поддержки поля берём из
    серверного каталога, а не из присланного заказа.
    """
    config = product.get("personalization")
    if not isinstance(config, dict):
        return ""
    value = str(raw or "").strip()[:32]
    if not value:
        return ""
    pattern = str(config.get("pattern") or "")
    if pattern:
        try:
            if not re.fullmatch(pattern, value):
                return ""
        except re.error:
            LOG.warning("Некорректный шаблон персонализации у товара %s", product.get("id"))
            return ""
    return value


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

# Состояния, в которых бот ждёт номер телефона текстом или кнопкой «Поделиться».
# Всё остальное — экраны с кнопками: текст там номером не считается.
PHONE_STATES = frozenset({"awaiting_consent", "awaiting_profile_phone", "awaiting_order_phone"})
# Шаг 1 — раздел, дальше ADD_STEPS, последний — проверка перед публикацией.
ADD_TOTAL = len(ADD_STEPS) + 2

# Пределы ввода мастера: названия попадают в кнопки (лимит Telegram 64 знака),
# описание — в карточку вещи, которая обязана влезть в одно сообщение (4096).
# Callback кнопки размера собран как «size:<id вещи>:<размер>» и обязан войти
# в 64 байта. id вещи занимает до MAX_PRODUCT_ID_BYTES, поэтому на размер
# остаётся MAX_SIZE_BYTES — и считать это надо по настоящему id, а не по
# заглушке: прежняя проверка «size:x:…» пропускала размеры, из-за которых
# публикация портила catalog.json и бот не запускался после перезапуска.
CALLBACK_DATA_LIMIT = 64
MAX_PRODUCT_ID_BYTES = 40
MAX_CATEGORY_ID_BYTES = 48
MAX_SIZE_BYTES = CALLBACK_DATA_LIMIT - len("size:") - MAX_PRODUCT_ID_BYTES - len(":")

ADD_NAME_LIMIT = 48
ADD_CATEGORY_LIMIT = 32
ADD_DESCRIPTION_LIMIT = 1200


def add_step_text(position: int, question: str, hint: str = "") -> str:
    """Экран мастера добавления вещи: один заголовок и счётчик шагов.

    Владелец проходит мастера редко, поэтому без «шаг 3 из 7» он не понимает,
    сколько ещё вводить и можно ли бросить на полпути.
    """
    text = f"<b>НОВАЯ ВЕЩЬ · ШАГ {position}/{ADD_TOTAL}</b>\n\n{question}"
    return f"{text}\n\n{hint}" if hint else text


class ScreenSender:
    """Прокси к API: первый экран после нажатия правит то сообщение, чью кнопку нажали.

    Экраны бота отправляют сообщения через ``api.send_message`` в 144 местах,
    поэтому перехват живёт здесь, а не в каждом вызове. Захват одноразовый и
    только для того же чата: всё, что уходит дальше (уведомления менеджеру,
    счета, подтверждения покупки), отправляется как раньше.
    """

    def __init__(self, api: Any, take_edit_target: Callable[[int], int | None]):
        self._api = api
        self._take_edit_target = take_edit_target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> Any:
        message_id = self._take_edit_target(chat_id)
        if message_id and self._api.edit_message(chat_id, message_id, text, reply_markup):
            return {"message_id": message_id, "chat": {"id": chat_id}, "edited": True}
        return self._api.send_message(chat_id, text, reply_markup)


class BrandBot:
    def __init__(self, settings: Settings, api: TelegramAPI, db: Database, catalog: Catalog):
        self.settings = settings
        self.db = db
        self.catalog = catalog
        self.bot_username: str = ""
        # Экран, открытый нажатием, меняется на месте: прокси перехватывает
        # первое сообщение экрана и правит то сообщение, чью кнопку нажали.
        self._screen_edit: tuple[int, int] | None = None
        self.api = ScreenSender(api, self.take_screen_edit)

    # ------------------------------------------------------------------ utils

    def flush_notifications(self, now: float | None = None, key: str | None = None) -> None:
        stamp = time.time() if now is None else now
        for _ in range(10):
            rows = self.db.claim_notifications(stamp, key)
            if not rows:
                break
            row = rows[0]
            try:
                if row["notification_id"].startswith("offer:"):
                    payment_id = row["notification_id"].split(":")[1]
                    if not self.db.payment_is_payable(payment_id):
                        self.db.connection().execute("UPDATE notifications SET delivered_at=? WHERE notification_id=?",
                                                     (utc_now(), row["notification_id"]))
                        continue
                self.api.send_message(row["chat_id"], row["body"], json.loads(row["markup"]) if row["markup"] else None)
            except Exception as exc:
                reason = str(exc).lower()
                notification_id = row["notification_id"]
                if any(marker in reason for marker in BLOCKED_DELIVERY_MARKERS):
                    # Собеседник заблокировал бота: помечаем, чтобы рассылки и
                    # уведомления его больше не трогали, и не повторяем вечно.
                    chat_id = int(row["chat_id"])
                    if chat_id > 0:
                        self.db.mark_blocked(chat_id)
                    self.db.connection().execute(
                        "UPDATE notifications SET delivered_at=? WHERE notification_id=?",
                        (utc_now(), notification_id),
                    )
                    LOG.warning("Notification dropped, chat %s is unavailable: %s", chat_id, notification_id)
                    continue
                if any(marker in reason for marker in UNDELIVERABLE_MARKERS):
                    # Такое сообщение не доставится никогда: повтор — это только
                    # вечный лог и лишние запросы к Telegram.
                    self.db.connection().execute(
                        "UPDATE notifications SET delivered_at=? WHERE notification_id=?",
                        (utc_now(), notification_id),
                    )
                    LOG.error("Notification undeliverable, dropped: %s — %s", notification_id, exc)
                    continue
                LOG.warning("Notification delivery deferred: %s", notification_id, exc_info=True)
                delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
                self.db.connection().execute("UPDATE notifications SET next_attempt_at=? WHERE notification_id=?",
                                             (stamp + delay, row["notification_id"]))
            else:
                self.db.connection().execute("UPDATE notifications SET delivered_at=? WHERE notification_id=?",
                                             (utc_now(), row["notification_id"]))

    def deliver_message(self, key: str, chat_id: int, text: str, markup: dict[str, Any] | None = None, *, queue_only: bool = False) -> None:
        self.db.enqueue_message(key, chat_id, text, markup)
        if not queue_only:
            self.flush_notifications(key=key)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids or self.db.is_admin_id(user_id)

    def is_owner(self, user_id: int) -> bool:
        """Владелец — из настройки ADMIN_IDS: только он раздаёт и снимает доступ."""
        return user_id in self.settings.admin_ids

    def cta(self, kind: str = "menu") -> str:
        brand = esc(self.settings.brand_name)
        if kind == "drop":
            return "\n\nСмотри выпуск — тираж маленький, потом не будет."
        if kind == "channel":
            if not self.channel_link():
                # Обещать канал, которого нет, — значит оставить человека в тупике.
                return ""
            return f"\n\nНовости — в канале «{brand}». Там узнаёшь первым."
        if kind == "size":
            return "\n\nНет размера? Нажми «Ждать размер» — напишем, когда вернётся."
        return "\n\nОткрой витрину и смотри, что осталось."

    def channel_link(self) -> str:
        """Ссылка на канал бренда: пустая строка, если канал не настроен."""
        return channel_target(self.settings.channel_url)

    def channel_rows(self) -> list[list[tuple[str, str]]]:
        """Ряд с кнопкой канала — только когда канал действительно существует."""
        link = self.channel_link()
        return [[(icon("channel", "Канал"), link)]] if link else []

    def price_is_payable(self, product: dict[str, Any]) -> bool:
        """По этой вещи можно выставить счёт: цена разбирается и больше нуля."""
        try:
            return self.line_amount(product, 1) > 0
        except PriceError:
            return False

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
        """Send the catalog's current greeting image, with a text fallback.

        Обложка уходит на каждый /start, поэтому после первой загрузки
        переиспользуем file_id Telegram: повтор стоит один лёгкий запрос,
        а не мегабайты аплоада.
        """
        asset = self.catalog.data.get("brand", {}).get("welcome_image") or "assets/welcome.jpg"
        photo = self.local_asset_path(asset)
        photo_url = self.public_asset_url(asset) if photo or str(asset).startswith("https://") else ""
        try:
            if photo and photo.is_file():
                self._send_welcome_file(chat_id, photo, caption, keyboard)
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

    def _send_welcome_file(self, chat_id: int, photo: Path, caption: str, keyboard: dict[str, Any] | None) -> None:
        stat = photo.stat()
        cache_key = "welcome_file_id:" + hashlib.sha256(
            compact_json([str(photo), str(stat.st_mtime_ns), str(stat.st_size)]).encode()
        ).hexdigest()
        cached = self.db.kv_get(cache_key)
        if cached:
            try:
                self.api.send_photo(chat_id, cached, caption, keyboard)
                return
            except Exception:
                # Telegram забыл копию — убираем её и загружаем заново.
                LOG.info("Cached welcome file_id rejected, re-uploading")
                self.db.kv_delete(cache_key)
        result = self.api.send_photo_file(chat_id, photo, caption, keyboard)
        if isinstance(result, dict):
            sizes = result.get("photo") or []
            file_id = next((item.get("file_id") for item in reversed(sizes) if item.get("file_id")), "")
            if file_id:
                self.db.kv_set(cache_key, str(file_id))

    @staticmethod
    def local_asset_path(value: Any) -> Path | None:
        """Resolve a catalog asset without allowing uploads outside miniapp."""
        parsed = urllib.parse.urlsplit(str(value or ""))
        if parsed.scheme or parsed.netloc or not parsed.path:
            return None
        root = (BASE_DIR / "miniapp").resolve()
        candidate = (root / urllib.parse.unquote(parsed.path).lstrip("/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def send_teaser(self, chat_id: int, caption: str = "", keyboard: dict[str, Any] | None = None) -> bool:
        """Send the drop teaser, reusing Telegram's own copy after the first upload.

        Order of preference: cached ``file_id`` → public URL from ``media`` →
        local upload. A stale ``file_id`` is dropped and the send retried, and
        any failure stays silent: the teaser is a bonus, not the conversation.
        """
        media = self.catalog.data.get("media", {}) if self.catalog else {}
        if not caption:
            title = str(media.get("teaser_title") or "СИЛА И ЧЕСТЬ")
            subtitle = str(media.get("teaser_caption") or "")
            caption = f"<b>{esc(title)}</b>" + (f"\n{esc(subtitle)}" if subtitle else "")

        video_asset = str(media.get("teaser") or "assets/video/teaser.mp4")
        video = self.local_asset_path(video_asset)
        poster = self.local_asset_path(media.get("teaser_thumbnail"))
        story_url = str(media.get("teaser_story_url") or "").strip()
        if not story_url and video_asset.startswith("https://"):
            story_url = video_asset
        source = [video_asset, story_url]
        if video and video.is_file():
            stat = video.stat()
            source.extend((str(stat.st_mtime_ns), str(stat.st_size)))
        cache_key = "teaser_file_id:" + hashlib.sha256(compact_json(source).encode()).hexdigest()
        cached = self.db.kv_get(cache_key)
        if cached:
            try:
                self.api.send_video(chat_id, cached, caption, keyboard)
                return True
            except Exception:
                # Telegram забывает file_id при смене бота или после чистки — шлём заново.
                LOG.info("Cached teaser file_id rejected, re-uploading")
                self.db.kv_delete(cache_key)

        if story_url.startswith("https://"):
            try:
                result = self.api.send_video(chat_id, story_url, caption, keyboard)
                self._remember_teaser(result, cache_key)
                return True
            except Exception:
                LOG.info("Teaser URL rejected, falling back to local upload")

        if video and video.is_file() and video.stat().st_size <= TEASER_UPLOAD_LIMIT:
            try:
                result = self.api.send_video(
                    chat_id,
                    video,
                    caption,
                    keyboard,
                    thumbnail=poster if poster and poster.is_file() else None,
                )
                self._remember_teaser(result, cache_key)
                return True
            except Exception:
                LOG.exception("Could not send teaser video")
        return False

    def _remember_teaser(self, result: Any, cache_key: str) -> None:
        """Cache the file_id so the next send costs one API call, not 3 MB."""
        if isinstance(result, dict):
            video = result.get("video")
            if isinstance(video, dict) and video.get("file_id"):
                self.db.kv_set(cache_key, str(video["file_id"]))

    # ------------------------------------------------------------------ menus

    def main_menu(self, chat_id: int | None = None) -> dict[str, Any]:
        """Главное меню покупателя: шесть разделов в две колонки.

        ``chat_id`` в личке совпадает с ``user_id``, поэтому по нему же
        поднимаем незавершённую оплату — отдельный параметр не нужен.
        Незакрытый счёт — единственная причина показать кнопку вне сетки:
        главное действие всегда широкое и всегда сверху.
        """
        rows: list[list[tuple[str, str]]] = []
        draft = self.db.pending_payment(chat_id) if chat_id else None
        if draft:
            payment_id, amount = draft
            label = f"Продолжить оплату · {format_rub(amount)}" if amount else "Продолжить оплату"
            rows.append([(f"{ICON['pay']} {label} →", f"draft:{payment_id}")])
        rows.extend(
            [
                [(icon("catalog", "Каталог"), "catalog"), (icon("orders", "Мои покупки"), "my_orders")],
                [(icon("looks", "Образы"), "lookbook"), (icon("film", "Ролик"), "teaser")],
                [(icon("account", "Кабинет"), "account"), (icon("support", "Поддержка"), "support")],
            ]
        )
        if chat_id and self.is_admin(int(chat_id)):
            # Владелец и админы видят вход в пульт прямо из /start:
            # управление магазином — такое же главное окно, как каталог.
            rows.append([(icon("tools", "Управление магазином"), "adm:panel")])
        if self.settings.webapp_url.startswith("https://"):
            rows.append([(f"Открыть витрину {ICON['outside']}", f"webapp:{self.settings.webapp_url}")])
        return inline_keyboard(rows)

    def menu_text(self, hint: str = "") -> str:
        """Заголовок главного меню: один на весь экран, без повторов бренда."""
        text = (
            f"<b>{esc(self.settings.brand_name)}</b>\n"
            "Выбери вещь или открой свою покупку."
        )
        return f"{text}\n\n{hint}" if hint else text

    def send_menu(self, chat_id: int, hint: str = "") -> None:
        """Главное меню отдельным сообщением — стабильная точка возврата."""
        self.api.send_message(chat_id, self.menu_text(hint), self.main_menu(chat_id))

    def stale(self, chat_id: int, reason: str) -> None:
        """Старая или несуществующая кнопка: объясняем и открываем актуальное меню."""
        self.send_menu(chat_id, f"{reason}\nОткрыл актуальное меню.")

    def answer_press(self, callback_id: str, text: str = "") -> None:
        """Снять «часики» с кнопки. Отказ Telegram не должен мешать экрану."""
        try:
            self.api.answer_callback(callback_id, text)
        except Exception as exc:
            LOG.warning("Could not answer callback %s: %s", callback_id, exc)

    def recover(self, chat_id: int, reason: str) -> None:
        """Сбой не должен выглядеть как молчание: объясняем и открываем меню.

        Человек нажал или написал — значит, он ждёт ответа. Если обработка
        упала, экран с кнопками важнее точной причины: причина уходит в лог,
        а собеседник получает выход, а не тишину до следующего /start.
        """
        try:
            self.send_menu(chat_id, f"{reason}\nПопробуй ещё раз или напиши менеджеру.")
        except Exception as exc:
            LOG.error("Recovery screen failed for chat %s: %s", chat_id, exc)

    def interest_menu(self) -> dict[str, Any]:
        """Первый вопрос новому покупателю: те же две колонки, что и в главном меню."""
        options = [(category["name"], f"intr:{category['id']}") for category in self.catalog.categories[:6]]
        rows = option_rows(options)
        rows.append([("Пока не знаю", "intr:skip")])
        return inline_keyboard(rows)

    def account_menu(self, user_id: int) -> dict[str, Any]:
        """Кабинет покупателя: свои данные, приоритет, бренд и канал."""
        rows = [
            [(icon("notice", "Узнать первым"), "profile"), (icon("link", "Привести друга"), "referral")],
            [(icon("size", "Подобрать размер"), "size_guide:account"), (icon("info", "О бренде"), "about")],
        ]
        waiting = self.db.waitlist_for_user(user_id)
        if waiting:
            # Кнопка появляется, только когда есть что снимать: пустой раздел — шум.
            label = "Жду размер" if len(waiting) == 1 else f"Жду размер ({len(waiting)})"
            rows.append([(icon("wait", label), "waits")])
        rows.extend(self.channel_rows())
        rows.extend(nav_rows(("Главное меню", "menu")))
        return inline_keyboard(rows)

    def account_text(self, user_id: int) -> str:
        user = self.db.get_user(user_id)
        phone = str(user["phone"]) if user and user["phone"] else ""
        invited = int(user["invited_count"]) if user else 0
        threshold = max(1, self.settings.giveaway_min_invites)
        lines = ["<b>МОЙ КАБИНЕТ</b>", ""]
        lines.append(f"{ICON['notice']} Номер: {esc(phone) if phone else 'не указан'}")
        lines.append(f"{ICON['link']} Приглашено: {invited} · до приоритета: {max(0, threshold - invited)}")
        size = str(user["pref_size"]) if user and user["pref_size"] else ""
        if size:
            lines.append(f"{ICON['size']} Твой размер: {esc(size)}")
        waiting = self.db.waitlist_for_user(user_id, 3)
        if waiting:
            listed = ", ".join(esc(row["size"]) for row in waiting)
            lines.append(f"{ICON['wait']} Ждёшь: {listed} — снять можно в «Жду размер»")
        lines.append("")
        lines.append("Данные нужны только для покупки и связи по ней.")
        return "\n".join(lines)

    def support_menu(self) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = [
            [(icon("orders", "Вопрос по покупке"), "ask")],
            [(icon("question", "Как это работает"), "help")],
        ]
        if self.settings.support_username:
            rows.append([(f"Написать менеджеру {ICON['outside']}", f"https://t.me/{self.settings.support_username}")])
        rows.extend(nav_rows(("Главное меню", "menu")))
        return inline_keyboard(rows)

    def support_text(self) -> str:
        lines = ["<b>ПОДДЕРЖКА</b>", "", "Отвечаем в этом чате."]
        lines.append("Номер покупки называть не нужно — выбери её кнопкой, контекст уйдёт вместе с вопросом.")
        if self.settings.support_username:
            lines.append(f"\nСрочное: @{esc(self.settings.support_username)}")
        return "\n".join(lines)

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
            # Ролик уходит первым: он объясняет бренд лучше любого абзаца.
            self.send_teaser(chat_id)
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
            self.send_welcome(chat_id, text, self.main_menu(chat_id))

    def save_interest(self, chat_id: int, user_id: int, interest: str) -> None:
        if interest != "skip":
            self.db.set_interest(user_id, interest)
            self.db.event(user_id, "interest_set", {"interest": interest})
        text = (
            "Записал.\n\n"
            "Вещи разбирают быстро — смотри, что осталось, и бери размер, пока он есть."
            + self.cta("drop")
        )
        self.api.send_message(chat_id, text, self.main_menu(chat_id))

    def show_catalog(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "catalog_open")
        sections = [
            (category, len(self.catalog.products_for_category(category["id"])))
            for category in self.catalog.categories
        ]
        sections = [item for item in sections if item[1]]
        if not sections:
            # Пустое состояние ведёт к следующему шагу, а не оставляет в тупике.
            self.api.send_message(
                chat_id,
                "<b>ВИТРИНА</b>\n\n"
                "Вещи готовятся — в каталоге пока пусто.\n"
                + ("Канал пишет о выпуске первым, до открытия продаж." if self.channel_link()
                   else "Как только выпуск откроется, вещи появятся здесь."),
                inline_keyboard(self.channel_rows() + nav_rows(("Главное меню", "menu"))),
            )
            return
        total = sum(count for _, count in sections)
        rows = [
            [(f"{category['name']} · {count} {things_word(count)}", f"cat:{category['id']}")]
            for category, count in sections
        ]
        # Замеры держим рядом с выбором вещи, а не в общей помощи.
        rows.append([(icon("size", "Подобрать размер"), "size_guide:catalog")])
        rows.extend(nav_rows(("Главное меню", "menu")))
        self.api.send_message(
            chat_id,
            f"<b>ВИТРИНА</b> · {total} {things_word(total)}\n\n"
            "Разделы и остатки честные: что видно, то и есть.",
            inline_keyboard(rows),
        )

    def show_category(self, chat_id: int, user_id: int, category_id: str) -> None:
        products = self.catalog.products_for_category(category_id)
        self.db.event(user_id, "category_open", {"category": category_id})
        title = next(
            (str(item["name"]) for item in self.catalog.categories if item["id"] == category_id),
            "Раздел",
        )
        if not products:
            self.api.send_message(
                chat_id,
                f"<b>{esc(title.upper())}</b>\n\n"
                "Здесь пока пусто — выпуск готовится.\n"
                + ("Канал пишет первым, а не когда всё разберут." if self.channel_link()
                   else "Загляни в другие разделы — там может быть не пусто."),
                inline_keyboard(self.channel_rows() + nav_rows(("Витрина", "catalog"))),
            )
            return
        rows = [[(f"{p['name']} · {p['price']}", f"product:{p['id']}")] for p in products]
        rows.extend(nav_rows(("Витрина", "catalog")))
        self.api.send_message(
            chat_id,
            f"<b>{esc(title.upper())}</b> · {len(products)} {things_word(len(products))}\n\n"
            "Нажми на вещь — покажем цену, состав и размеры.",
            inline_keyboard(rows),
        )

    def product_card_text(self, product: dict[str, Any]) -> str:
        """Карточка вещи: название → цена → сведения → варианты.

        Структура одинаковая у всех вещей, поэтому покупатель читает карточку,
        а не разбирается в ней заново на каждом товаре.
        """
        lines = [f"<b>{esc(str(product['name']).upper())}</b>"]
        price = f"<b>{esc(str(product['price']))}</b>"
        badge = str(product.get("badge") or "").strip()
        if badge:
            price += f" · {esc(badge)}"
        lines.append(price)
        if not self.price_is_payable(product):
            # Цену в каталоге записали диапазоном или припиской: счёт по ней не
            # выставить. Говорим сразу, а не после согласия и номера телефона.
            lines.append("Цену сейчас не посчитать — оформим вручную через поддержку.")
        description = str(product.get("description") or "").strip()
        if description:
            lines.extend(["", esc(description)])
        facts: list[str] = []
        for key, kind in (("material", "fabric"), ("fit", "cut")):
            value = str(product.get(key) or "").strip()
            if value:
                facts.append(f"{ICON[kind]} {esc(value)}")
        sizes = " · ".join(str(size) for size in product.get("sizes") or [])
        if sizes:
            facts.append(f"{ICON['ruler']} Размеры: {esc(sizes)}")
        stock = str(product.get("stock_label") or "").strip()
        if stock:
            facts.append(f"{ICON['stock']} {esc(stock)}")
        if facts:
            lines.extend([""] + facts)
        details = [str(item).strip() for item in (product.get("details") or []) if str(item).strip()][:3]
        if details:
            lines.extend([""] + [f"• {esc(item)}" for item in details])
        signature = str(product.get("signature") or "").strip()
        if signature:
            lines.extend(["", esc(signature)])
        return "\n".join(lines)

    def product_missing(self, chat_id: int, reason: str) -> None:
        """Старая кнопка на снятую вещь: объясняем и ведём в живую витрину."""
        self.api.send_message(
            chat_id,
            f"<b>ВЕЩЬ НЕ НАЙДЕНА</b>\n\n{reason}\nПоказал, что есть сейчас.",
            inline_keyboard([[(icon("catalog", "Каталог"), "catalog")]] + nav_rows()),
        )

    def product_shots(self, product: dict[str, Any]) -> list[tuple[str, str]]:
        """Кадры вещи с подписями из каталога — без выдуманных «вид сзади»."""
        labels = [str(item or "").strip() for item in (product.get("image_labels") or [])]
        shots: list[tuple[str, str]] = []
        for index, shot in enumerate(list(product.get("images") or [])[:6]):
            url = self.public_asset_url(shot)
            if not url:
                continue
            shots.append((url, labels[index] if index < len(labels) else ""))
        return shots

    def send_card_photo(self, chat_id: int, photo: str, text: str, keyboard: dict[str, Any]) -> None:
        """Карточка с фото. Подпись Telegram режет на 1024 знаках — не теряем текст."""
        if len(text) <= 1000:
            self.api.send_photo(chat_id, photo, text, keyboard)
            return
        self.api.send_photo(chat_id, photo, text[:1000].rsplit("\n", 1)[0])
        self.api.send_message(chat_id, text, keyboard)

    def show_product(self, chat_id: int, user_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Её сняли с продажи или уже разобрали.")
            return
        self.db.event(user_id, "product_open", {"product_id": product_id})
        category_title = next(
            (str(item["name"]) for item in self.catalog.categories if item["id"] == product["category"]),
            "Каталог",
        )
        text = self.product_card_text(product)
        primary = (
            [("Выбрать размер →", f"want:{product_id}")]
            if self.price_is_payable(product)
            else [(icon("support", "Написать менеджеру →"), "support")]
        )
        keyboard = inline_keyboard(
            [
                # Главное действие — отдельной широкой строкой, остальное ниже.
                primary,
                [(icon("wait", "Ждать размер"), f"wait:{product_id}"),
                 (icon("size", "Замеры"), f"size_guide:product:{product_id}")],
            ]
            + nav_rows((category_title, f"cat:{product['category']}"))
        )
        # Вещь показываем со всех сторон: альбом с честными подписями кадров,
        # затем карточка с кнопками — текст в альбоме читается хуже.
        shots = self.product_shots(product)
        if len(shots) > 1:
            try:
                self.api.send_media_group(
                    chat_id,
                    [url for url, _ in shots],
                    "",
                    [label for _, label in shots],
                )
                self.api.send_message(chat_id, text, keyboard)
                return
            except Exception:
                LOG.exception("Failed to send product album, falling back to a single photo")
        photo = shots[0][0] if shots else self.public_asset_url(product.get("photo_url") or product.get("image"))
        if photo:
            self.send_card_photo(chat_id, photo, text, keyboard)
        else:
            self.api.send_message(chat_id, text, keyboard)

    def choose_size(self, chat_id: int, user_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Её сняли с продажи или уже разобрали.")
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [[(size, f"size:{product_id}:{size}") for size in sizes[index:index + 4]] for index in range(0, len(sizes), 4)]
        rows.append([(icon("size", "Подобрать размер"), f"size_guide:want:{product_id}")])
        rows.extend(nav_rows((str(product["name"]), f"product:{product_id}")))
        self.api.send_message(
            chat_id,
            f"<b>РАЗМЕР</b>\n\n{esc(str(product['name']))} · {esc(str(product['price']))}\n"
            "Какой размер берёшь?",
            inline_keyboard(rows),
        )

    def ask_waitlist_size(self, chat_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Её сняли с продажи или уже разобрали.")
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [[(size, f"wsize:{product_id}:{size}") for size in sizes[index:index + 4]] for index in range(0, len(sizes), 4)]
        rows.extend(nav_rows((str(product["name"]), f"product:{product_id}")))
        self.api.send_message(
            chat_id,
            f"<b>ЖДЁМ РАЗМЕР</b>\n\n{esc(str(product['name']))}\n"
            "Какой размер ждёшь? Вернётся — напишем первыми, раньше канала.",
            inline_keyboard(rows),
        )

    def select_size(self, chat_id: int, user_id: int, product_id: str, size: str, request_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product or size not in [str(item) for item in product["sizes"]]:
            self.product_missing(chat_id, "Этот вариант уже разобрали.")
            return
        if not self.price_is_payable(product):
            self.api.send_message(
                chat_id,
                f"<b>ЦЕНА НЕДСТУПНА</b>\n\n"
                f"{esc(str(product['name']))} · размер {esc(size)}\n"
                "Цена этой вещи записана так, что счёт по ней не выставить.\n"
                "Напиши менеджеру — оформим вручную, без потери места в очереди.",
                inline_keyboard(
                    [[(icon("support", "Поддержка →"), "support")],
                     [(icon("wait", "Ждать размер"), f"wait:{product_id}")]]
                    + nav_rows((str(product["name"]), f"product:{product_id}"))
                ),
            )
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

    def confirm_waitlist(self, chat_id: int, user_id: int, product_id: str, size: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Вещь больше недоступна.")
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
        self.api.send_message(chat_id, text + self.cta("channel"), self.main_menu(chat_id))

    def show_my_waits(self, chat_id: int, user_id: int) -> None:
        """Что человек ждёт и как перестать: подписаться можно было и раньше, выйти — нет."""
        rows = self.db.waitlist_for_user(user_id)
        if not rows:
            self.api.send_message(
                chat_id,
                "<b>ТЫ НИЧЕГО НЕ ЖДЁШЬ</b>\n\n"
                "Лист ожидания пуст. Нужный размер ищи в витрине — он там появится.",
                inline_keyboard([[(icon("catalog", "Открыть витрину"), "catalog")]]
                                + nav_rows(("Кабинет", "account"))),
            )
            return
        lines = ["<b>ЧТО Я ЖДУ</b>", "", "Напишем, как только размер вернётся.", ""]
        buttons: list[list[tuple[str, str]]] = []
        for row in rows:
            lines.append(f"• {esc(row['product_name'])} · размер {esc(row['size'])}")
            buttons.append([(icon("cancel", f"Не ждать: {row['product_name']} · {row['size']}"),
                             f"wstop:{int(row['id'])}")])
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(buttons + nav_rows(("Кабинет", "account"))))

    def stop_waiting(self, chat_id: int, user_id: int, raw_entry_id: str) -> None:
        entry_id = int(raw_entry_id) if str(raw_entry_id).isdigit() else 0
        if not entry_id or not self.db.remove_waitlist_entry(entry_id, user_id):
            # Записи уже нет: размер купили или сняли раньше. Экран всё равно
            # показываем заново — молчание человек принял бы за сбой.
            self.db.event(user_id, "waitlist_remove_miss", {"entry_id": entry_id})
            self.show_my_waits(chat_id, user_id)
            return
        self.db.event(user_id, "waitlist_remove", {"entry_id": entry_id})
        self.show_my_waits(chat_id, user_id)

    def line_amount(self, product: dict[str, Any], quantity: int) -> int:
        amount = parse_price_strict(product.get("price"))
        if amount <= 0:
            raise PriceError("Цена должна быть больше нуля")
        return amount * max(1, int(quantity or 1))

    def pay_title(self) -> str:
        return (self.settings.brand_name or "ВОРОЖБИТОВ")[:32]

    def build_pay_methods(self, payment_id: str, amount_rub: int, description: str) -> list[dict[str, str]]:
        lock = self.db.invoice_locks[hash(payment_id) % len(self.db.invoice_locks)]
        with lock:
            if not self.db.payment_is_payable(payment_id):
                return []
            payment = self.db.get_payment(payment_id)
            if not payment or int(payment["amount_rub"]) != amount_rub:
                return []
            cached = self.db.connection().execute(
                "SELECT methods FROM payment_methods WHERE payment_id=? AND valid_until>?", (payment_id, time.time()),
            ).fetchone()
            if cached:
                return json.loads(cached["methods"])
            methods = self._create_pay_methods(payment_id, amount_rub, description)
            if not self.db.payment_is_payable(payment_id):
                return []
            if methods:
                self.db.connection().execute(
                    "INSERT OR REPLACE INTO payment_methods VALUES (?, ?, ?)",
                    (payment_id, compact_json(methods), time.time() + 1800),
                )
            return methods

    def _create_pay_methods(
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
            payment = self.db.get_payment(payment_id)
            stars = int(payment["amount_stars"] or 0) if payment else 0
            if stars <= 0:
                return methods
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
            if not url:
                return methods
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

    def pay_keyboard(self, payment_id: str, chat_id: int | None = None) -> dict[str, Any] | None:
        """Способы оплаты в две колонки и возврат к покупке на стабильном месте."""
        methods: list[tuple[str, str]] = []
        if self.settings.lava_ready():
            methods.append((icon("pay", "Карта / СБП"), f"pay:{payment_id}:lava"))
        if self.settings.crypto_ready():
            methods.append((icon("pay", "Крипта"), f"pay:{payment_id}:crypto"))
        if self.settings.stars_enabled:
            methods.append((icon("pay", "Звёзды"), f"pay:{payment_id}:stars"))
        if not methods:
            return None
        rows = option_rows(methods)
        rows.extend(nav_rows(("Мои покупки", "my_orders")))
        return inline_keyboard(rows)

    def pay_methods_text(self) -> str:
        """Чем можно оплатить сейчас: неподключённые способы не обещаем."""
        names: list[str] = []
        if self.settings.lava_ready():
            names.append("карта / СБП")
        if self.settings.crypto_ready():
            names.append("крипта")
        if self.settings.stars_enabled:
            names.append("звёзды Telegram")
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " или " + names[-1]

    def pay_fallback_keyboard(self, payment_id: str = "") -> dict[str, Any]:
        """Куда идти, когда оплатить не вышло: назад к счёту, в поддержку и домой.

        Без этого ряда отказ способа оплаты оставлял покупателя в тупике:
        сообщение приходило вообще без кнопок.
        """
        rows: list[list[tuple[str, str]]] = []
        if payment_id:
            rows.append([(icon("pay", "К способам оплаты"), f"draft:{payment_id}")])
        rows.append([(icon("support", "Поддержка"), "support")])
        rows.extend(nav_rows(("Мои покупки", "my_orders")))
        return inline_keyboard(rows)

    def offer_payment(
        self,
        chat_id: int,
        payment_id: str,
        amount_rub: int,
        order_lines: list[str],
        source: str = "витрины",
        *, queue_only: bool = False,
    ) -> None:
        keyboard = self.pay_keyboard(payment_id, chat_id)
        methods = self.pay_methods_text()
        tail = (
            f"\n\nОплати сейчас — {methods}. Деньги списываются сразу. "
            "Если размера нет — возврат через менеджера."
            if methods else
            "\n\nОплата в боте пока не подключена. Напиши менеджеру — "
            "оформим вручную и сохраним место в очереди."
        )
        if keyboard is None:
            keyboard = self.pay_fallback_keyboard()
        text = (
            f"<b>ПОКУПКА ПРИНЯТА · {esc(format_rub(amount_rub))}</b>\n\n"
            "Собрано из " + esc(source) + ":\n"
            + "\n".join(order_lines)
            + tail
        )
        self.deliver_message(f"offer:{payment_id}:{chat_id}", chat_id, text, keyboard, queue_only=queue_only)

    def notify_paid(self, payment_id: str, chat_id: int | None = None, *, queue_only: bool = False) -> None:
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
            "<b>ОПЛАТА ПРОШЛА</b>\n\n"
            + ("\n".join(lines) if lines else "Покупка оплачена.")
            + f"\n\n{esc(ORDER_STATUS_MESSAGES['paid'])}"
        )
        if payment["status"] == "refund_required":
            text = ("<b>ОПЛАТА ТРЕБУЕТ ВОЗВРАТА</b>\n\n"
                    "Покупка отменена или её состав изменился. Деньги не пропадают: "
                    "менеджер проверит поступление и свяжется по поводу возврата.")
        # После денег следующий шаг один — открыть карточку покупки.
        if orders and payment["status"] == "paid":
            keyboard = inline_keyboard(
                [[(f"{ICON['orders']} Открыть покупку №{int(orders[0]['id'])} →", f"ord:{int(orders[0]['id'])}")]]
                + nav_rows()
            )
        else:
            keyboard = self.main_menu(target)
        self.deliver_message(f"paid:{payment_id}:{payment['status']}:{target}", target, text, keyboard, queue_only=True)
        if self.settings.manager_chat_id:
            label = "ТРЕБУЕТСЯ ВОЗВРАТ" if payment["status"] == "refund_required" else "ПОЛУЧЕНА"
            self.deliver_message(
                f"paid:{payment_id}:{payment['status']}:manager", self.settings.manager_chat_id,
                f"<b>Оплата {esc(payment_id)} · {label}</b>\nКлиент: {user_id}\n"
                + ("\n".join(lines) + "\n" if lines else "")
                + f"Сумма: {esc(format_rub(int(payment['amount_rub'])))}\nСпособ: {esc(payment['method'] or '—')}",
                queue_only=True,
            )
        if not queue_only:
            self.flush_notifications()

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
        if (not payment or not self.db.payment_is_payable(payload)
                or currency != "XTR" or int((query.get("from") or {}).get("id") or 0) != payment["user_id"]):
            try:
                self.api.answer_pre_checkout(query_id, False, "Оплата уже неактуальна.")
            except Exception:
                LOG.exception("answerPreCheckoutQuery failed")
            return
        if currency == "XTR":
            expected = int(payment["amount_stars"] or 0)
            if expected <= 0 or total != expected:
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
            self.api.send_message(chat_id, "Оплата пришла, но покупка не найдена. Напиши менеджеру.", self.main_menu(chat_id))
            return
        if self.db.mark_payment_paid(payload, "stars", charge,
                                     queue_notifications=lambda payment_id: self.notify_paid(payment_id, queue_only=True)):
            self.flush_notifications()
            self.db.event(user_id, "stars_paid", {"payment_id": payload})

    def start_method_pay(self, chat_id: int, user_id: int, payment_id: str, method: str) -> None:
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != user_id:
            self.api.send_message(
                chat_id, "Оплата не найдена.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        if not self.db.payment_is_payable(payment_id):
            self.open_draft(chat_id, user_id, payment_id)
            return
        amount = int(payment["amount_rub"] or 0)
        description = f"{self.settings.brand_name}: оплата {payment_id}"
        if method == "stars":
            if not self.settings.stars_enabled:
                self.api.send_message(
                    chat_id, "Звёзды сейчас выключены — выбери другой способ.",
                    self.pay_fallback_keyboard(payment_id),
                )
                return
            stars = int(payment["amount_stars"] or 0)
            if stars <= 0:
                self.api.send_message(
                    chat_id, "Для этой покупки оплата звёздами недоступна — выбери другой способ.",
                    self.pay_fallback_keyboard(payment_id),
                )
                return
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
                self.api.send_message(
                    chat_id, "Не получилось выставить счёт в звёздах. Попробуй ещё раз или выбери другой способ.",
                    self.pay_fallback_keyboard(payment_id),
                )
            return
        methods = self.build_pay_methods(payment_id, amount, description)
        found = next((item for item in methods if item["id"] == method and item.get("url")), None)
        if not found:
            self.api.send_message(
                chat_id, "Этот способ сейчас недоступен. Выбери другой или напиши менеджеру.",
                self.pay_fallback_keyboard(payment_id),
            )
            return
        self.api.send_message(
            chat_id,
            f"<b>{esc(found['title'])}</b>\n\nСсылка на оплату ниже. "
            "После перевода статус в «Мои покупки» станет «оплачена».",
            inline_keyboard(
                [[(f"Оплатить {ICON['outside']}", found["url"])]]
                + nav_rows(("Мои покупки", "my_orders"))
            ),
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
        legacy = self.db.connection().execute(
            "SELECT id FROM orders WHERE user_id=? AND request_id=?", (user_id, request_id),
        ).fetchone()
        if legacy:
            self.api.send_message(
                chat_id,
                f"Покупка №{legacy['id']} уже принята — новую не создаём.",
                inline_keyboard([[(f"{ICON['orders']} Открыть покупку №{legacy['id']} →", f"ord:{legacy['id']}")]]
                                + nav_rows(("Мои покупки", "my_orders"))),
            )
            return
        try:
            amount = self.line_amount(product, 1)
        except PriceError:
            self.api.send_message(chat_id, "Цена вещи недоступна. Напиши менеджеру.", self.main_menu(chat_id))
            return
        self.api.send_message(chat_id, "Покупка собрана. Осталось оплатить.", remove_keyboard())
        fingerprint = hashlib.sha256(compact_json([product["id"], size, phone]).encode()).hexdigest()
        receipt, created = self.db.create_checkout(
            user_id, f"bot:{request_id}", fingerprint,
            [{"product_id": product["id"], "name": product["name"], "size": size, "phone": phone,
              "quantity": 1, "note": "", "amount_rub": amount, "person": "", "person_label": ""}],
            stars_amount(amount, self.settings.stars_rub_per_star) if self.settings.stars_enabled else 0,
            queue_notifications=lambda receipt: self.queue_checkout_notifications(receipt, user_id, phone, "", chat_id, "бота"),
        )
        self.flush_notifications()
        order_id = receipt["order_ids"][0]
        payment_id = receipt["payment_id"]
        amount = receipt["amount_rub"]
        if not created:
            self.api.send_message(
                chat_id,
                f"Покупка №{order_id} уже принята — новую не создаём.",
                inline_keyboard([[(f"{ICON['orders']} Открыть покупку №{order_id} →", f"ord:{order_id}")]]
                                + nav_rows(("Мои покупки", "my_orders"))),
            )
            return
        self.db.event(user_id, "checkout_step", {"step": "payment", "payment_id": payment_id})
        line = f"• {esc(product['name'])} · {esc(size)} · 1 шт."
        self.offer_payment(chat_id, payment_id, amount, [line], source="бота")

    def request_profile(self, chat_id: int, user_id: int) -> None:
        self.ask_phone(chat_id, user_id, "awaiting_profile_phone", {})

    def consent_text(self) -> str:
        text = (
            "<b>ШАГ 1 ИЗ 2 · СОГЛАСИЕ</b>\n\n"
            "Чтобы принять покупку и писать про выпуск, нужен номер и согласие "
            "на обработку данных и сообщения от бренда.\n\n"
            "Данные только для покупки и связи по ней. Отписаться можно в любой момент."
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
                "<b>ШАГ 2 ИЗ 2 · НОМЕР</b>\n\n"
                "Оставь номер — подтвердим наличие и доставку. Оплата сразу после оформления.\n\n"
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
            consent_keyboard(),
        )

    def accept_consent(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "awaiting_consent":
            self.stale(chat_id, "Не нашёл, к чему относилось это согласие.")
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
            "<b>ШАГ 2 ИЗ 2 · НОМЕР</b>\n\nПринято. Теперь нужен номер."
            if next_state == "awaiting_order_phone"
            else "<b>ШАГ 2 ИЗ 2 · НОМЕР</b>\n\nПринято. Напишем про выпуск и про твой размер."
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
                "Без согласия покупку принять нельзя — номер хранить не имеем права.\n\n"
                "Витрина открыта. Если передумаешь — кнопка ниже." + self.cta("channel"),
                self.main_menu(chat_id),
            )
        else:
            self.api.send_message(
                chat_id,
                "Хорошо, номер не сохраняем."
                + (" Новые вещи всё равно выходят в канале — там ничего не пропустишь."
                   if self.channel_link() else
                   " Новые вещи появятся в витрине — заходи, когда будет интересно.")
                + self.cta("channel"),
                self.main_menu(chat_id),
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
                consent_keyboard(),
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
        self.send_menu(chat_id)

    def show_referral(self, chat_id: int, user_id: int) -> None:
        user = self.db.get_user(user_id)
        invited = int(user["invited_count"]) if user else 0
        threshold = max(1, self.settings.giveaway_min_invites)
        link = self.ref_link(user_id)
        text = (
            "<b>ПРИВЕДИ ДРУГА</b>\n\n"
            f"{ICON['link']} Приглашено: <b>{invited}</b>\n"
            f"{ICON['notice']} До приоритета: <b>{max(0, threshold - invited)}</b>\n\n"
            "Кидаешь ссылку другу — он заходит в бота — ты поднимаешься в списке. "
            f"Набрал {threshold} — приоритет на следующий выпуск и участие в розыгрыше."
        )
        if link:
            text += f"\n\nТвоя ссылка:\n<code>{esc(link)}</code>"
        rows: list[list[tuple[str, str]]] = []
        if link:
            rows.append([(f"Отправить другу {ICON['outside']}",
                          f"https://t.me/share/url?url={urllib.parse.quote(link, safe='')}")])
        rows.extend(nav_rows(("Кабинет", "account")))
        self.api.send_message(chat_id, text, inline_keyboard(rows))

    def show_lookbook(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "lookbook_open")
        shots: list[tuple[str, str]] = []
        for item in self.catalog.data.get("lookbook", []):
            photo = self.public_asset_url(item.get("photo_url") or item.get("image"))
            if photo:
                shots.append((photo, str(item.get("caption") or "").strip()))
        if not shots:
            self.api.send_message(
                chat_id,
                "<b>ОБРАЗЫ</b>\n\nПервые кадры снимаются. "
                + ("Они выйдут в канале раньше открытого выпуска." if self.channel_link()
                   else "Как только появятся — выложим их здесь."),
                inline_keyboard(self.channel_rows() + nav_rows(("Главное меню", "menu"))),
            )
            return
        try:
            self.api.send_media_group(
                chat_id,
                [photo for photo, _ in shots[:10]],
                "<b>ОБРАЗЫ</b>",
                [label for _, label in shots[:10]],
            )
        except Exception:
            LOG.exception("Failed to send lookbook album")
            self.api.send_message(
                chat_id,
                "Не получилось отправить альбом.\n"
                + ("Загляни в канал — там всё выложим." if self.channel_link()
                   else "Попробуй открыть образы ещё раз чуть позже."),
                inline_keyboard(self.channel_rows() + nav_rows()),
            )
            return
        self.api.send_message(
            chat_id,
            "Понравилось? Тогда не жди — размеры разбирают." + self.cta("drop"),
            inline_keyboard([[(icon("catalog", "Каталог"), "catalog")]] + nav_rows()),
        )

    def restock_reply(self, chat_id: int, product_id: str, size: str) -> None:
        """Ответ на ресток: из команды или из кнопки листа ожидания."""
        delivered = self.notify_waitlist(product_id, size)
        if delivered:
            self.api.send_message(
                chat_id, f"Уведомлено по листу ожидания: {delivered}.",
                inline_keyboard(staff_nav_rows()))
            return
        waiting = len(self.db.waitlist_user_ids(product_id, size))
        if waiting:
            text = (
                f"Никого не оповестил: все {waiting} уже получали сообщение об этом размере.\n"
                "Одна запись листа ожидания — одно сообщение, иначе люди тонут в повторах."
            )
        else:
            text = "По этому размеру никто не ждёт — оповещать некого."
        self.api.send_message(chat_id, text, inline_keyboard(staff_nav_rows()))

    def notify_waitlist(self, product_id: str, size: str) -> int:
        """Оповестить тех, кто ждёт размер. Каждая запись срабатывает один раз."""
        delivered = 0
        product = self.catalog.get(product_id)
        if not product:
            return 0
        failed: list[int] = []
        for recipient, entry_id in self.db.claim_waitlist(product_id, size):
            try:
                self.api.send_message(
                    recipient,
                    f"<b>РАЗМЕР ВЕРНУЛСЯ</b>\n\n{esc(product['name'])} — размер {esc(size)} снова в наличии.\n"
                    "Бери сейчас: размер могут разобрать быстро.",
                    inline_keyboard([
                        [("Забрать размер →", f"want:{product_id}")],
                        [(icon("cancel", f"Больше не ждать {esc(size)}"), f"wstop:{entry_id}")],
                    ]),
                )
                delivered += 1
                time.sleep(0.04)
            except Exception as exc:
                failed.append(recipient)
                LOG.warning("Waitlist notify failed for %s: %s", recipient, exc)
        # Не дошло — значит, человек ещё не оповещён: вернём ему место в очереди.
        self.db.release_waitlist(failed, product_id, size)
        return delivered

    # Группы списка покупок: один порядок и одни названия на всех экранах.
    ORDER_GROUPS = (
        ("Ждут оплаты", ("new", "awaiting_payment")),
        ("В работе", ("paid", "confirmed")),
        ("Завершены", ("completed",)),
        ("Отменены", ("cancelled",)),
    )

    def show_my_orders(self, chat_id: int, user_id: int) -> None:
        """Список покупок одним сообщением: чат не тонет в десятке карточек."""
        orders = self.db.orders_for_user(user_id)
        if not orders:
            self.api.send_message(
                chat_id,
                "<b>МОИ ПОКУПКИ</b>\n\n"
                "Покупок пока нет. Выбери вещь в каталоге — статус появится здесь.",
                inline_keyboard([[(icon("catalog", "Каталог"), "catalog")]] + nav_rows()),
            )
            return
        lines = ["<b>МОИ ПОКУПКИ</b>"]
        for title, statuses in self.ORDER_GROUPS:
            group = [row for row in orders if str(row["status"]) in statuses]
            if not group:
                continue
            lines.append("")
            lines.append(f"<b>{title}</b>")
            for row in group:
                amount = int(row["amount_rub"] or 0)
                tail = f" · {format_rub(amount)}" if amount else ""
                lines.append(
                    f"№{int(row['id'])} · {esc(row['product_name'])} · {esc(row['size'])}"
                    f" · {int(row['quantity'] or 1)} шт.{tail}"
                )
        rows: list[list[tuple[str, str]]] = []
        # Главное действие — самое срочное: сначала то, что ждёт денег.
        urgent = next((row for row in orders if str(row["status"]) in {"new", "awaiting_payment"}), orders[0])
        rows.append([(f"{ICON['orders']} Открыть покупку №{int(urgent['id'])} →", f"ord:{int(urgent['id'])}")])
        # Одна покупка — одна кнопка: дублировать её номером не нужно.
        if len(orders) > 1:
            for index in range(0, len(orders), 3):
                rows.append([(f"№{int(row['id'])}", f"ord:{int(row['id'])}") for row in orders[index:index + 3]])
        rows.extend(nav_rows(("Главное меню", "menu")))
        self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(rows))

    def order_card_text(self, row: Any, payment: Any) -> str:
        """Карточка покупки: номер → состав → три состояния → дата."""
        amount = int(row["amount_rub"] or 0)
        lines = [
            f"<b>ПОКУПКА №{int(row['id'])}</b>",
            f"{esc(row['product_name'])} · размер {esc(row['size'])} · {int(row['quantity'] or 1)} шт.",
            "",
        ]
        lines.extend(order_state_lines(str(row["status"]), payment, amount))
        note = str(row["note"] or "").strip()
        if note:
            lines.append("")
            lines.append(f"{ICON['receipt']} {esc(note[:200])}")
        lines.append("")
        lines.append(f"Оформлена {esc(self.format_date(row['created_at']))}")
        if payment is not None and payment["paid_at"]:
            lines.append(f"Оплачена {esc(self.format_date(payment['paid_at']))}")
        return "\n".join(lines)

    @staticmethod
    def format_date(value: Any) -> str:
        """Дата из базы (она в UTC) в коротком виде: 12.09.2026 14:05."""
        raw = str(value or "")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw[:16] or "—"
        return stamp.strftime("%d.%m.%Y %H:%M")

    def order_keyboard(self, row: Any, *, payable: bool) -> dict[str, Any]:
        """Кнопки покупки: одно главное действие и только допустимые остальные."""
        status = str(row["status"])
        order_id = int(row["id"])
        payment_id = str(row["payment_id"] or "")
        rows: list[list[tuple[str, str]]] = []
        if payable and payment_id:
            amount = int(row["amount_rub"] or 0)
            label = f"Оплатить {format_rub(amount)}" if amount else "Оплатить"
            rows.append([(f"{ICON['pay']} {label} →", f"draft:{payment_id}")])
        secondary: list[tuple[str, str]] = []
        if payment_id and len(self.db.orders_for_payment(payment_id)) > 1:
            secondary.append((icon("receipt", "Состав покупки"), f"receipt:{payment_id}"))
        secondary.append((icon("support", "Вопрос"), f"ask:{order_id}"))
        rows.append(secondary)
        if status in {"new", "awaiting_payment"}:
            rows.append([(icon("cancel", "Отменить покупку"), f"ucancel:{order_id}")])
        rows.extend(nav_rows(("Мои покупки", "my_orders")))
        return inline_keyboard(rows)

    def show_order(self, chat_id: int, user_id: int, order_id: int) -> None:
        row = self.db.get_order(order_id)
        if not row or int(row["user_id"]) != user_id:
            self.api.send_message(
                chat_id,
                "<b>ПОКУПКА НЕ НАЙДЕНА</b>\n\nНомер неверный или это чужая покупка.\nПоказал твой список.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        payment_id = str(row["payment_id"] or "")
        payment = self.db.get_payment(payment_id) if payment_id else None
        payable = bool(payment_id) and self.db.payment_is_payable(payment_id)
        self.api.send_message(
            chat_id,
            self.order_card_text(row, payment),
            self.order_keyboard(row, payable=payable),
        )

    def show_receipt(self, chat_id: int, user_id: int, payment_id: str) -> None:
        """Состав общей оплаты: все позиции и одна сумма."""
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != user_id:
            self.api.send_message(
                chat_id,
                "<b>СОСТАВ НЕ НАЙДЕН</b>\n\nЭта оплата не твоя или уже не существует.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        orders = self.db.orders_for_payment(payment_id)
        lines = ["<b>СОСТАВ ПОКУПКИ</b>", ""]
        total = 0
        for row in orders:
            amount = int(row["amount_rub"] or 0)
            total += amount
            lines.append(
                f"• {esc(row['product_name'])} · {esc(row['size'])} · {int(row['quantity'] or 1)} шт."
                + (f" · {format_rub(amount)}" if amount else "")
            )
        lines.extend(["", f"Итого: <b>{esc(format_rub(total))}</b>"])
        first = orders[0] if orders else None
        keyboard = (
            self.order_keyboard(first, payable=self.db.payment_is_payable(payment_id))
            if first is not None
            else inline_keyboard(nav_rows(("Мои покупки", "my_orders")))
        )
        self.api.send_message(chat_id, "\n".join(lines), keyboard)

    def open_draft(self, chat_id: int, user_id: int, payment_id: str) -> None:
        """Продолжить оплату: тот же счёт, без нового, и с проверкой актуальности."""
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != user_id:
            self.api.send_message(
                chat_id,
                "<b>ОПЛАТА НЕ НАЙДЕНА</b>\n\nСчёт не твой или уже удалён.\nПоказал актуальный список.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        if not self.db.payment_is_payable(payment_id):
            state = str(payment["status"])
            reason = {
                "paid": "Эта покупка уже оплачена.",
                "cancelled": "Эта покупка отменена — деньги не списываем.",
                "refund_required": "По этой покупке нужен возврат: менеджер свяжется.",
            }.get(state, "Данные изменились: состав или сумма больше не совпадают.")
            self.api.send_message(
                chat_id,
                f"<b>ОПЛАТА НЕАКТУАЛЬНА</b>\n\n{reason}\nОткрыл актуальную карточку.",
                self.order_keyboard(self.db.orders_for_payment(payment_id)[0], payable=False)
                if self.db.orders_for_payment(payment_id)
                else inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        amount = int(payment["amount_rub"] or 0)
        lines = [
            f"• {esc(row['product_name'])} · {esc(row['size'])} · {int(row['quantity'] or 1)} шт."
            for row in self.db.orders_for_payment(payment_id)
        ]
        keyboard = self.pay_keyboard(payment_id, chat_id)
        head = f"<b>ОПЛАТА · {esc(format_rub(amount))}</b>\n\n" + ("\n".join(lines) if lines else "")
        if keyboard is None:
            # Способов нет совсем (свежий .env без платёжек): не зовём выбирать
            # из пустого списка и не оставляем человека без единой кнопки.
            text = (
                head + "\n\nОплата в боте пока не подключена — счёт выставить нельзя.\n"
                "Напиши менеджеру: оформим вручную и сохраним место в очереди."
            )
            keyboard = self.pay_fallback_keyboard()
        else:
            text = (
                head + "\n\nВыбери способ. Ссылка откроется отдельным сообщением, "
                "статус покупки обновится сам."
            )
        self.api.send_message(chat_id, text, keyboard)

    def cancel_own_order(self, chat_id: int, user_id: int, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != user_id:
            self.api.send_message(
                chat_id,
                "<b>ПОКУПКА НЕ НАЙДЕНА</b>\n\nНомер неверный или это чужая покупка.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows()),
            )
            return
        try:
            updated, changed = self.db.set_order_status(order_id, "cancelled", customer_id=user_id)
        except ValueError:
            self.api.send_message(
                chat_id,
                "Эту покупку уже нельзя отменить здесь — напиши менеджеру, решим вручную.",
                self.order_keyboard(order, payable=False)
                if order["payment_id"] else inline_keyboard(nav_rows(("Мои покупки", "my_orders"))),
            )
            return
        if not changed:
            self.api.send_message(chat_id, "Покупка уже отменена.", inline_keyboard(nav_rows(("Мои покупки", "my_orders"))))
            return
        self.api.send_message(
            chat_id,
            f"<b>ПОКУПКА №{order_id} ОТМЕНЕНА</b>\n\n"
            "Все позиции общей оплаты отменены, деньги не списываем.\n"
            "Новую можно собрать из каталога.",
            inline_keyboard(
                [[(icon("catalog", "Каталог"), "catalog"), (icon("orders", "Мои покупки"), "my_orders")]]
                + nav_rows()
            ),
        )
        if self.settings.manager_chat_id:
            try:
                self.api.send_message(
                    self.settings.manager_chat_id,
                    f"Клиент отменил покупку №{order_id}: {esc(updated['product_name'] if updated else '')}.",
                )
            except Exception as exc:
                LOG.warning("Could not notify manager about cancel %s: %s", order_id, exc)

    def help_text(self) -> str:
        brand = esc(self.settings.brand_name)
        extra = ""
        if self.settings.support_username:
            extra = f"\n\nВопросы: @{esc(self.settings.support_username)}"
        return (
            f"<b>КАК ЭТО РАБОТАЕТ</b>\n\n"
            f"{brand}\n"
            "1. Выбираешь вещь в каталоге.\n"
            "2. Берёшь размер и оставляешь номер.\n"
            "3. Оплачиваешь: карта / СБП, крипта или звёзды Telegram.\n\n"
            f"{ICON['wait']} Нет размера — «Ждать размер»: напишем, когда вернётся.\n"
            f"{ICON['orders']} Статус покупки — в «Мои покупки»: оплата, сборка, доставка.\n"
            f"{ICON['pay']} Деньги списываются сразу. Если размера нет — возврат через менеджера."
            f"{extra}"
        )

    def show_size_guide(self, chat_id: int, user_id: int, origin: str = "") -> None:
        """Сетка размеров. «Назад» ведёт туда, откуда экран открыли."""
        self.db.event(user_id, "size_guide_open", {"origin": origin[:40]})
        text = (
            "<b>КАК ВЗЯТЬ СВОЙ РАЗМЕР</b>\n\n"
            f"{ICON['ruler']} S — рост 164–172, грудь 112\n"
            f"{ICON['ruler']} M — рост 172–178, грудь 118\n"
            f"{ICON['ruler']} L — рост 178–186, грудь 124\n"
            f"{ICON['ruler']} XL — рост 186–194, грудь 130\n\n"
            "Посадка свободная. Между двумя — бери больший, если хочешь объём.\n"
            "Не уверен — выбери «Вопрос по покупке» в поддержке, подскажем."
            + self.cta("size")
        )
        back: tuple[str, str] | None = None
        if origin.startswith("product:"):
            product = self.catalog.get_any(origin.split(":", 1)[1])
            if product:
                back = (str(product["name"]), origin)
        elif origin == "catalog":
            back = ("Витрина", "catalog")
        elif origin == "account":
            back = ("Кабинет", "account")
        elif origin.startswith("want:"):
            product = self.catalog.get_any(origin.split(":", 1)[1])
            if product:
                back = ("Размер", f"want:{product['id']}")
        elif origin.startswith("size:"):
            product = self.catalog.get_any(origin.split(":", 1)[1])
            if product:
                back = ("Размер", f"want:{product['id']}")
        rows: list[list[tuple[str, str]]] = []
        if back is None:
            rows.append([(icon("catalog", "Каталог"), "catalog"), (icon("support", "Поддержка"), "support")])
        self.api.send_message(chat_id, text, inline_keyboard(rows + nav_rows(back)))

    def show_support(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "support_open")
        self.api.send_message(chat_id, self.support_text(), self.support_menu())

    def support_orders(self, chat_id: int, user_id: int) -> None:
        """Вопрос по конкретной покупке: номер и состав не спрашиваем заново."""
        orders = self.db.orders_for_user(user_id)
        if not orders:
            self.api.send_message(
                chat_id,
                "<b>ВОПРОС ПО ПОКУПКЕ</b>\n\n"
                "Покупок пока нет, поэтому передавать нечего.\n"
                "Общий вопрос можно задать прямо здесь — ответим в этом чате.",
                inline_keyboard(
                    [[(icon("catalog", "Каталог"), "catalog"), (icon("question", "Как это работает"), "help")]]
                    + nav_rows(("Поддержка", "support"))
                ),
            )
            return
        rows = [
            [(f"№{int(row['id'])} · {esc(row['product_name'])}", f"ask:{int(row['id'])}")]
            for row in orders[:8]
        ]
        rows.extend(nav_rows(("Поддержка", "support")))
        self.api.send_message(
            chat_id,
            "<b>ПО КАКОЙ ПОКУПКЕ ВОПРОС?</b>\n\n"
            "Выбери покупку — менеджер увидит номер, состав и статус, переспрашивать не придётся.",
            inline_keyboard(rows),
        )

    def forward_support(self, chat_id: int, user_id: int, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != user_id:
            self.api.send_message(
                chat_id,
                "Покупку не нашёл. Открой «Мои покупки» и выбери её из списка.",
                inline_keyboard([[(icon("orders", "Мои покупки"), "my_orders")]] + nav_rows(("Поддержка", "support"))),
            )
            return
        self.db.event(user_id, "support_request", {"order_id": order_id})
        payment = self.db.get_payment(str(order["payment_id"] or "")) if order["payment_id"] else None
        if not self.settings.manager_chat_id:
            contact = f"@{self.settings.support_username}" if self.settings.support_username else "менеджеру"
            self.api.send_message(
                chat_id,
                f"<b>ПОКУПКА №{order_id}</b>\n\n"
                + "\n".join(order_state_lines(str(order["status"]), payment, int(order["amount_rub"] or 0)))
                + f"\n\nОтдельного канала поддержки сейчас нет — напиши {esc(contact)}, "
                "номер покупки уже перед тобой.",
                inline_keyboard(nav_rows(("Поддержка", "support"))),
            )
            return
        try:
            self.api.send_message(
                self.settings.manager_chat_id,
                f"<b>ВОПРОС ПО ПОКУПКЕ №{order_id}</b>\n"
                f"Клиент: {user_id}\n"
                f"{esc(order['product_name'])} · размер {esc(order['size'])} · {int(order['quantity'] or 1)} шт.\n"
                + "\n".join(order_state_lines(str(order["status"]), payment, int(order["amount_rub"] or 0)))
                + "\n\nОтветь клиенту в его чат — контекст он уже выбрал.",
            )
        except Exception as exc:
            LOG.warning("Could not forward support request for %s: %s", order_id, exc)
            self.api.send_message(
                chat_id,
                "Не получилось передать вопрос менеджеру. Попробуй ещё раз чуть позже.",
                inline_keyboard([[(icon("support", "Повторить"), f"ask:{order_id}")]]
                                + nav_rows(("Поддержка", "support"))),
            )
            return
        self.api.send_message(
            chat_id,
            f"<b>ВОПРОС ПЕРЕДАН</b>\n\n"
            f"Покупка №{order_id} у менеджера вместе с составом и статусом.\n"
            "Ответ придёт в этот чат.",
            inline_keyboard(
                [[(f"{ICON['orders']} Открыть покупку №{order_id}", f"ord:{order_id}")]]
                + nav_rows(("Поддержка", "support"))
            ),
        )

    # ------------------------------------------------------------------ admin

    def order_status_keyboard(self, order_id: int, status: str) -> dict[str, Any] | None:
        """Карточка покупки у команды: одно главное действие и отмена рядом.

        Главное действие — следующий реальный этап, поэтому кнопки меняются
        вместе со статусом, а не висят все сразу.
        """
        rows: list[list[tuple[str, str]]] = []
        primary = {
            "new": ("Подтвердить наличие", "confirmed"),
            "awaiting_payment": ("Отметить оплату", "paid"),
            "paid": ("Подтвердить наличие", "confirmed"),
            "confirmed": ("Собрана, завершить", "completed"),
        }.get(status)
        if primary:
            rows.append([(f"{primary[0]} →", f"order:{order_id}:{primary[1]}")])
        if status in {"new", "awaiting_payment", "paid", "confirmed"}:
            rows.append([(icon("cancel", "Отменить покупку"), f"order:{order_id}:cancelled")])
        rows.append([(f"{ICON['back']} Покупки", "adm:orders"), (icon("tools", "Управление"), "adm:panel")])
        return inline_keyboard(rows) if rows else None

    def update_order_status(self, chat_id: int, order_id: int, status: str) -> None:
        try:
            order, changed = self.db.set_order_status(
                order_id, status, queue_paid=lambda payment_id: self.notify_paid(payment_id, queue_only=True)
            )
            self.flush_notifications()
        except ValueError as exc:
            self.api.send_message(chat_id, f"Не могу изменить покупку: {esc(exc)}")
            return
        if not order:
            self.api.send_message(chat_id, "Покупка не найдена.")
            return
        label = ORDER_STATUS_LABELS.get(status, status)
        if not changed:
            self.api.send_message(chat_id, f"Покупка №{order_id} уже имеет статус «{esc(label)}».")
            return
        try:
            self.api.send_message(
                int(order["user_id"]),
                f"<b>ПОКУПКА №{order_id} · {esc(label).upper()}</b>\n\n"
                f"{esc(order['product_name'])} · размер {esc(order['size'])} · {int(order['quantity'] or 1)} шт.\n"
                f"{esc(ORDER_STATUS_MESSAGES.get(status, 'Статус покупки обновлён.'))}",
                inline_keyboard(
                    [[(f"{ICON['orders']} Открыть покупку №{order_id} →", f"ord:{order_id}")]] + nav_rows()
                ),
            )
        except Exception as exc:
            LOG.warning("Could not notify user %s about order %s: %s", order["user_id"], order_id, exc)
        self.api.send_message(
            chat_id,
            f"Покупка №{order_id}: статус «{esc(label)}» сохранён.",
            self.order_card_markup(order_id, status, int(order["user_id"])),
        )

    def admin_panel(self, chat_id: int) -> None:
        """Пульт владельца: сводка сверху, разделы в две колонки, выход в режим покупателя.

        Список команд шире кнопок: /broadcast, /restock, /giveaway, /hide, /show,
        /export и /reload остаются командами, чтобы не раздувать панель.
        """
        stats = self.db.stats()
        text = (
            "<b>УПРАВЛЕНИЕ МАГАЗИНОМ</b>\n\n"
            f"{ICON['orders']} Покупок: {stats['orders']} · сегодня: {stats['orders_today']}\n"
            f"{ICON['pay']} Ждут оплаты: {stats['awaiting_payment']} · оплачено: {stats['paid']}\n"
            f"{ICON['wait']} Ждут размер: {stats['waitlist']}\n\n"
            "Рассылка: <code>/broadcast текст</code> · вернуть размер: <code>/restock id размер</code>"
        )
        self.api.send_message(
            chat_id,
            text,
            inline_keyboard(
                [
                    [(f"{ICON['stats']} Сводка →", "adm:summary")],
                    [(icon("orders", "Покупки"), "adm:orders"), (icon("wait", "Лист ожидания"), "adm:waitlist")],
                    [(icon("catalog", "Добавить вещь"), "adm:add"), (icon("trophy", "Топ рефералов"), "adm:top")],
                    [(icon("account", "Доступ команды"), "adm:access"), (icon("trophy", "Розыгрыши"), "adm:draws")],
                    [(icon("account", "Режим покупателя"), "menu")],
                ]
            ),
        )

    def admin_summary(self, chat_id: int) -> None:
        """Сводка без смешения показателей: деньги, работа и база отдельно."""
        stats = self.db.stats()
        paid_amount = int(stats.get("paid_amount") or 0)
        text = (
            "<b>СВОДКА МАГАЗИНА</b>\n\n"
            f"{ICON['pay']} Оплачено покупок: {stats['paid']}\n"
            f"{ICON['pay']} Получено: {esc(format_rub(paid_amount))}\n"
            f"{ICON['pay']} Ждут оплаты: {stats['awaiting_payment']}\n"
            f"{ICON['cancel']} Требуют возврата: {stats['refunds']}\n\n"
            f"{ICON['orders']} Покупок всего: {stats['orders']} · сегодня: {stats['orders_today']}\n"
            f"{ICON['wait']} В листе ожидания: {stats['waitlist']}\n"
            f"{ICON['trophy']} Приведено друзей: {stats['referrals']}\n\n"
            f"{ICON['account']} В базе: {stats['users']} · с номером: {stats['contacts']} · "
            f"с согласием: {stats['consents']}"
        )
        self.api.send_message(
            chat_id,
            text,
            inline_keyboard(
                [[(icon("pay", "Деньги →"), "adm:money"),
                  (icon("notice", "Что сегодня"), "adm:digest")]]
                + [[(icon("orders", "Покупки"), "adm:orders")]]
                + [[(icon("tools", "Управление"), "adm:panel"), (icon("account", "Режим покупателя"), "menu")]]
            ),
        )

    def giveaway_threshold(self) -> int:
        """Порог приоритета: владелец меняет его из бота, kv переживает перезапуск."""
        raw = self.db.kv_get("giveaway_min_invites")
        return int(raw) if raw.strip().isdigit() else self.settings.giveaway_min_invites

    def admin_customer(self, chat_id: int, user_id: int) -> None:
        """Клиент целиком: контакт, согласие, деньги и привод — из карточки покупки."""
        user = self.db.get_user(user_id)
        if not user:
            self.api.send_message(chat_id, "Клиент не найден в базе.",
                                  inline_keyboard(staff_nav_rows()))
            return
        who = self._who(user_id)
        row = self.db.connection().execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub), 0) AS s FROM orders "
            "WHERE user_id=? AND status IN ('paid', 'confirmed', 'completed')",
            (user_id,)).fetchone()
        waits = self.db.connection().execute(
            "SELECT COUNT(*) FROM waitlist WHERE user_id=? AND notified_at IS NULL",
            (user_id,)).fetchone()[0]
        lines = [
            "<b>КЛИЕНТ</b>", "",
            f"{ICON['account']} {esc(who)} · id {user_id}",
            f"{ICON['support']} {esc(str(user['phone'] or 'номера нет'))}",
            f"{ICON['ok']} Согласие: {esc(str(user['consent_at'] or 'нет')[:10])}",
            f"{ICON['pay']} Оплаченных покупок: {int(row['c'])} · на {esc(format_rub(int(row['s'])))}",
            f"{ICON['trophy']} Привёл приглашённых: {int(user['invited_count'] or 0)}",
        ]
        if waits:
            lines.append(f"{ICON['wait']} Ждёт размеры: {int(waits)}")
        self.api.send_message(
            chat_id, "\n".join(lines),
            inline_keyboard([[(icon("orders", "Покупки"), "adm:orders")]] + staff_nav_rows()))

    def admin_shelf(self, chat_id: int) -> None:
        """Витрина кнопками: скрыть и показать вещь без команд /hide и /show."""
        lines = ["<b>ВИТРИНА</b>", ""]
        buttons: list[list[tuple[str, str]]] = []
        for category in self.catalog.categories:
            for product in self.catalog.data.get("products", []):
                if str(product.get("category")) != str(category.get("id")):
                    continue
                pid = str(product.get("id"))
                name = str(product.get("name", pid))[:16]
                active = bool(product.get("active", True))
                lines.append(f"{'🛍' if active else ICON['cancel']} {esc(name)} — "
                             f"{'в витрине' if active else 'скрыта'}")
                if len(buttons) < 6:
                    label = f"Скрыть {name}" if active else f"Показать {name}"
                    buttons.append([(icon("stock", label), f"shelf:{pid}")])
        if not buttons:
            lines.append("В каталоге пусто: добавьте вещь через /add.")
        else:
            lines.append("")
            lines.append("Кнопка меняет вещь местами: витрина и скрытие.")
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(buttons + staff_nav_rows()))

    def admin_money(self, chat_id: int) -> None:
        """Экран денег: владелец смотрит выручку без сводки-простыни."""
        money = self.db.money_stats()
        avg = money["paid_total"] // money["paid_count"] if money["paid_count"] else 0
        lines = [
            "<b>ДЕНЬГИ</b>", "",
            f"{ICON['pay']} Получено всего: {esc(format_rub(money['paid_total']))}",
            f"{ICON['pay']} Сегодня: {esc(format_rub(money['paid_today']))}",
            f"{ICON['pay']} За 7 дней: {esc(format_rub(money['paid_week']))}",
            f"{ICON['receipt']} Средний чек: {esc(format_rub(avg))} · оплачено {money['paid_count']}",
        ]
        if money["refunds"]:
            lines.append(f"{ICON['notice']} Возвратов к вниманию: {money['refunds']}")
        self.api.send_message(
            chat_id, "\n".join(lines),
            inline_keyboard([[(icon("stats", "Сводка"), "adm:summary")]] + staff_nav_rows()))

    def admin_digest(self, chat_id: int) -> None:
        """Что сегодня хозяйству: подтверждения, оплаты, дефицит, возвраты."""
        stats = self.db.stats()
        waits = self.db.waitlist_unnotified()
        lines = [
            "<b>ЧТО СЕГОДНЯ</b>", "",
            f"{ICON['orders']} Покупок сегодня: {stats['orders_today']} · "
            f"новых без подтверждения: {self.db.orders_new()}",
            f"{ICON['pay']} Ждут оплаты: {stats['awaiting_payment']} · "
            f"оплачено всего: {stats['paid']}",
        ]
        if waits:
            sizes = ", ".join(f"{esc(str(r['size']))} ×{int(r['c'])}" for r in waits[:4])
            lines.append(f"{ICON['wait']} Вернуть размеры: {sizes}")
        else:
            lines.append(f"{ICON['wait']} Лист ожидания чист: дефицита нет")
        if stats["refunds"]:
            lines.append(f"{ICON['notice']} Возвраты: {stats['refunds']} — посмотрите")
        else:
            lines.append("Возвратов нет.")
        self.api.send_message(
            chat_id, "\n".join(lines),
            inline_keyboard([[(icon("stats", "Сводка"), "adm:summary")]] + staff_nav_rows()))

    def admin_orders(self, chat_id: int) -> None:
        """Покупки у команды одним списком: карточка с действием — по номеру.

        Раньше команда получала заголовок и до десяти полных карточек подряд,
        поэтому в чате нельзя было найти ни своё же сообщение, ни конец списка.
        """
        orders = self.db.recent_orders()
        if not orders:
            self.api.send_message(
                chat_id,
                "<b>ПОКУПОК ПОКА НЕТ</b>\n\nКак только оформят первую — она появится здесь.",
                inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
            )
            return
        lines = ["<b>ПОКУПКИ</b>", "", "Показаны последние · открой номер, чтобы сменить этап."]
        for title, statuses in self.ORDER_GROUPS:
            group = [row for row in orders if str(row["status"]) in statuses]
            if not group:
                continue
            lines.append("")
            lines.append(f"<b>{title}</b>")
            for row in group:
                amount = int(row["amount_rub"] or 0)
                who = f"@{row['username']}" if row["username"] else str(row["first_name"] or row["user_id"])
                lines.append(
                    f"№{int(row['id'])} · {esc(row['product_name'])} · {esc(row['size'])}"
                    + (f" · {format_rub(amount)}" if amount else "")
                    + f" · {esc(who)}"
                )
        rows = option_rows(((f"№{int(row['id'])}", f"aord:{int(row['id'])}") for row in orders), 3)
        rows.append([(icon("tools", "Управление"), "adm:panel")])
        self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(rows))

    def admin_order_card_text(self, row: Any) -> str:
        """Карточка покупки у команды: клиент, состав, деньги и этап."""
        status = str(row["status"])
        payment = self.db.get_payment(str(row["payment_id"] or "")) if row["payment_id"] else None
        who = f"@{row['username']}" if row["username"] else str(row["first_name"] or "")
        lines = [
            f"<b>ПОКУПКА №{int(row['id'])} · {esc(ORDER_STATUS_LABELS.get(status, status)).upper()}</b>",
            f"{esc(row['product_name'])} · размер {esc(row['size'])} · {int(row['quantity'] or 1)} шт.",
            f"{ICON['account']} Клиент: {esc(who or str(row['user_id']))} · {esc(row['phone'] or 'номер не указан')}",
            "",
        ]
        lines.extend(order_state_lines(status, payment, int(row["amount_rub"] or 0)))
        note = str(row["note"] or "").strip()
        if note:
            lines.append("")
            lines.append(f"{ICON['receipt']} {esc(note[:200])}")
        lines.extend(["", f"Оформлена {esc(self.format_date(row['created_at']))}"])
        return "\n".join(lines)

    def admin_order_card(self, chat_id: int, order_id: int) -> None:
        row = self.db.get_order(order_id)
        if not row:
            self.api.send_message(
                chat_id,
                "<b>ПОКУПКА НЕ НАЙДЕНА</b>\n\nНомер неверный или покупку уже удалили.",
                inline_keyboard([[(icon("orders", "Покупки"), "adm:orders")]]),
            )
            return
        self.api.send_message(
            chat_id, self.admin_order_card_text(row),
            self.order_card_markup(order_id, str(row["status"]), int(row["user_id"])),
        )

    def order_card_markup(self, order_id: int, status: str, user_id: int) -> dict[str, Any]:
        """Клавиатура карточки покупки: действия статуса, затем клиент, нав-ряд последний."""
        markup = self.order_status_keyboard(order_id, status) or {}
        rows = [list(r) for r in markup.get("inline_keyboard", [])]
        # Нав-ряд остаётся последним: заметка и клиент встают перед ним.
        rows.insert(max(0, len(rows) - 1),
                    [{"text": icon("account", "Клиент →"),
                      "callback_data": f"aclient:{int(user_id)}"}])
        rows.insert(max(0, len(rows) - 1),
                    [{"text": icon("receipt", "Заметка"),
                      "callback_data": f"anote:{int(order_id)}"}])
        return {"inline_keyboard": rows}

    def admin_refunds(self, chat_id: int) -> None:
        """Возвраты очередью: причина и отметка «выполнен» — кнопками."""
        open_rows = self.db.refunds_open()
        lines: list[str] = ["<b>ВОЗВРАТЫ</b>"]
        rows: list[list[tuple[str, str]]] = []
        if not open_rows:
            lines.append("\nОчередь пуста: возвраты не требуются.")
        for row in open_rows:
            pid = str(row["payment_id"])
            reason = self.db.kv_get(f"rreason:{pid}")
            lines.append(
                f"\n{ICON['cancel']} Покупка №{int(row['order_id'])} · "
                f"{esc(str(row['product_name'])[:24])} · {format_rub(int(row['amount_rub']))}"
                + (f"\n{ICON['tools']} Причина: {esc(reason)}" if reason
                   else "\nПричина не указана — выбери кнопкой.")
            )
            if len(rows) < 4:
                rows.append([
                    (icon("tools", "Причина →"), f"rreasons:{pid}"),
                    (icon("ok", "Возврат выполнен"), f"rdone:{pid}"),
                ])
        done = self.db.refunds_done()
        if done:
            lines.append(f"\n{ICON['ok']} Выполнено недавно:")
            for row in done:
                pid = str(row["payment_id"])
                reason = self.db.kv_get(f"rreason:{pid}") or "без причины"
                lines.append(f"№{int(row['order_id'])} · "
                             f"{format_rub(int(row['amount_rub']))} · {esc(reason)}")
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(rows + staff_nav_rows()))

    def admin_refund_reasons(self, chat_id: int, payment_id: str) -> None:
        rows = [[(label, f"rreason:{payment_id}:{key}")
                 for key, label in list(REFUND_REASON_LABELS.items())[i:i + 2]]
                for i in (0, 2)]
        rows.append([(icon("cancel", "← Возвраты"), "rlist")])
        self.api.send_message(
            chat_id,
            f"<b>ПРИЧИНА ВОЗВРАТА</b>\n\nОплата {esc(payment_id[:12])}.\n"
            "Причина попадёт в экран возвратов и в журнал.",
            inline_keyboard(rows),
        )

    def admin_command(self, chat_id: int, user_id: int, text: str) -> bool:
        if not self.is_admin(user_id):
            return False
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command in {"/stats", "/summary"}:
            self.admin_summary(chat_id)
        elif command == "/orders":
            self.admin_orders(chat_id)
        elif command == "/broadcast":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/broadcast текст рассылки</code>")
            else:
                self.db.set_state(user_id, "broadcast_pending", {"text": argument})
                self.ask_broadcast_segment(chat_id, argument)
        elif command == "/restock":
            parts = argument.split()
            if len(parts) != 2:
                self.api.send_message(chat_id, "Использование: <code>/restock id_товара размер</code>")
            else:
                self.restock_reply(chat_id, parts[0], parts[1])
        elif command == "/waitlist":
            rows = self.db.waitlist_rows()
            if not rows:
                self.api.send_message(
                    chat_id,
                    "<b>ЛИСТ ОЖИДАНИЯ ПУСТ</b>\n\nНикто не ждёт размер — значит, дефицита нет.",
                    inline_keyboard(staff_nav_rows()),
                )
            else:
                def notified_at(row: sqlite3.Row) -> str:
                    # str(None) — это строка "None", то есть истина: без `or ""`
                    # экран показал бы оповещёнными всех, кто ещё ждёт.
                    return str(row["notified_at"] or "") if "notified_at" in row.keys() else ""

                notified = sum(1 for row in rows if notified_at(row))
                head = f"Ждут размер: {len(rows)}"
                if notified:
                    head += f" · из них оповещены: {notified}"
                lines = ["<b>ЛИСТ ОЖИДАНИЯ</b>", "", head, ""]
                for row in rows:
                    who = f"@{row['username']}" if row["username"] else str(row["first_name"] or "")
                    tail = " · оповещён" if notified_at(row) else ""
                    lines.append(
                        f"• {esc(row['product_name'])} · {esc(row['size'])} · "
                        f"{esc(who or str(row['user_id']))}{tail}"
                    )
                lines.extend(["", "Написать им, когда размер вернётся: кнопкой ниже "
                              "или <code>/restock id размер</code>.",
                              "Повторно тем же людям не пишем — одна запись даёт одно сообщение."])
                rows: list[list[tuple[str, str]]] = []
                for row in self.db.waitlist_unnotified()[:3]:
                    name = str(row["product_name"])[:14]
                    rows.append([(icon("channel", f"Вернуть {row['size']} · {name}"),
                                  f"wnotify:{row['product_id']}:{row['size']}")])
                rows.append(staff_nav_rows()[0])
                self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(rows))
        elif command == "/top":
            rows = self.db.top_referrers()
            if not rows:
                self.api.send_message(
                    chat_id,
                    "<b>ПОКА ПУСТО</b>\n\nНикто никого не привёл — приглашения ещё не сработали.",
                    inline_keyboard(staff_nav_rows()),
                )
            else:
                threshold = max(1, self.giveaway_threshold())
                lines = ["<b>ТОП ПРИГЛАШЕНИЙ</b>", "", f"Приоритет дают с {threshold} "
                         + plural(threshold, "приглашённого", "приглашённых", "приглашённых"), ""]
                for index, row in enumerate(rows, start=1):
                    who = f"@{row['username']}" if row["username"] else str(row["first_name"] or "")
                    lines.append(f"{index}. {esc(who or str(row['user_id']))} — {int(row['invited_count'])}")
                lines.extend(["", "Выбрать победителей: <code>/giveaway N</code>"])
                self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(staff_nav_rows()))
        elif command == "/giveaway":
            count = int(argument) if argument.isdigit() else 1
            pool = self.db.giveaway_pool(max(1, self.giveaway_threshold()))
            if not pool:
                self.api.send_message(
                    chat_id,
                    f"<b>РОЗЫГРЫШ ПУСТ</b>\n\n"
                    f"Никто ещё не набрал {self.settings.giveaway_min_invites} приглашённых.",
                    inline_keyboard(staff_nav_rows()),
                )
            else:
                winners = random.sample(pool, min(count, len(pool)))
                # Розыгрыш нигде больше не виден: без записи нельзя ни проверить
                # прошлый тираж, ни понять, почему человек выиграл дважды.
                self.db.event(user_id, "giveaway_drawn",
                              {"winners": list(winners), "pool": len(pool), "count": count})
                lines = [f"<b>Победители ({len(winners)})</b>", ""]
                for winner_id in winners:
                    user = self.db.get_user(winner_id)
                    username = f"@{user['username']}" if user and user["username"] else str(winner_id)
                    lines.append(f"{esc(username)} — id {winner_id}")
                    try:
                        # Победителю сказано «напиши менеджеру» — значит, нужна
                        # и кнопка: сообщение без выхода заканчивается тупиком.
                        self.api.send_message(
                            winner_id,
                            "<b>Ты в розыгрыше</b>\n\n"
                            "Ссылка сработала. Напиши менеджеру — заберёшь вещь из выпуска первым.",
                            inline_keyboard([[(icon("support", "Написать менеджеру →"), "support")],
                                             [(icon("catalog", "Смотреть витрину"), "catalog")]]),
                        )
                    except Exception:
                        LOG.warning("Could not notify winner %s", winner_id)
                self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(staff_nav_rows()))
        elif command in ("/add", "/new"):
            self.start_add_product(chat_id, user_id)
        elif command == "/hide":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/hide id_товара</code>")
            else:
                try:
                    hidden = self.catalog.set_active(argument, False)
                except (ValueError, OSError) as exc:
                    self.catalog_refused(chat_id, "ВЕЩЬ НЕ СКРЫТА", exc)
                else:
                    self.api.send_message(
                        chat_id,
                        f"Вещь {esc(argument)} скрыта из витрины." if hidden else "Не нашёл такой id.",
                        inline_keyboard(staff_nav_rows()),
                    )
        elif command == "/show":
            if not argument:
                self.api.send_message(chat_id, "Использование: <code>/show id_товара</code>")
            else:
                try:
                    shown = self.catalog.set_active(argument, True)
                except (ValueError, OSError) as exc:
                    self.catalog_refused(chat_id, "ВЕЩЬ НЕ ВОЗВРАЩЕНА", exc)
                else:
                    self.api.send_message(
                        chat_id,
                        f"Вещь {esc(argument)} снова в витрине." if shown else "Не нашёл такой id.",
                        inline_keyboard(staff_nav_rows()),
                    )
        elif command == "/access":
            self.admin_access(chat_id)
        elif command == "/draws":
            self.admin_draws(chat_id)
        elif command == "/reports":
            self.admin_reports(chat_id)
        elif command == "/money":
            self.admin_money(chat_id)
        elif command == "/digest":
            self.admin_digest(chat_id)
        elif command == "/shelf":
            self.admin_shelf(chat_id)
        elif command == "/refunds":
            self.admin_refunds(chat_id)
        elif command == "/again":
            raw = self.db.kv_get("last_broadcast")
            try:
                saved = json.loads(raw) if raw else {}
            except ValueError:
                saved = {}
            text_last = str(saved.get("text") or "").strip()
            if not text_last:
                self.api.send_message(
                    chat_id,
                    "Прошлой рассылки нет — повторять нечего.\n"
                    "Собери новую: <code>/broadcast текст</code>.",
                    inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
                )
            else:
                segment = str(saved.get("segment") or "all")
                self.db.set_state(user_id, "broadcast_pending",
                                  {"text": text_last, "segment": segment})
                self.preview_broadcast(chat_id, user_id, segment)
        elif command == "/grant":
            self.grant_admin(chat_id, user_id, argument)
        elif command == "/revoke":
            target = self._resolve_user(argument)
            if target is None:
                self.api.send_message(chat_id, "Использование: <code>/revoke id или @username</code>",
                                      inline_keyboard(staff_nav_rows()))
            else:
                self.revoke_admin(chat_id, user_id, target)
        elif command == "/export":
            self.api.send_document(chat_id, "users.csv", self.db.export_users_csv(), "Экспорт базы")
        elif command == "/reload":
            try:
                self.catalog.reload()
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self.catalog_refused(
                    chat_id, "КАТАЛОГ НЕ ПЕРЕЧИТАН", exc,
                    "Файл не прошёл проверку, поэтому работаю со старым каталогом: "
                    "витрина цела. Поправь catalog.json и повтори /reload.",
                )
            else:
                self.api.send_message(chat_id, "Каталог перечитан без перезапуска.",
                                      inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]))
        elif command in ("/admin", "/panel"):
            self.admin_panel(chat_id)
        else:
            return False
        return True

    # ------------------------------------------------- добавление товара

    def start_add_product(self, chat_id: int, user_id: int) -> None:
        self.db.set_state(user_id, "admin_add", {"step": "category"})
        rows = option_rows((category["name"], f"addcat:{category['id']}") for category in self.catalog.categories)
        rows.append([(icon("catalog", "Новый раздел"), "addcat:new")])
        rows.append([(icon("cancel", "Отмена"), "admin:cancel")])
        self.api.send_message(
            chat_id,
            add_step_text(1, "Куда её положим?",
                          "Дальше спросим название, цену, размеры и описание — по одному шагу."),
            inline_keyboard(rows),
        )

    def pick_add_category(self, chat_id: int, user_id: int, category_id: str) -> None:
        if category_id == "new":
            self.db.set_state(user_id, "admin_add", {"step": "new_category"})
            self.api.send_message(
                chat_id,
                "<b>НОВЫЙ РАЗДЕЛ</b>\n\nНазвание? Например: Верхняя одежда",
            )
            return
        if not any(category["id"] == category_id for category in self.catalog.categories):
            self.api.send_message(
                chat_id,
                "Такой категории нет.",
                inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
            )
            return
        self.db.set_state(user_id, "admin_add", {"step": "name", "category": category_id})
        self.api.send_message(chat_id, add_step_text(2, ADD_STEPS[0][1]))

    def handle_order_note_text(self, chat_id: int, user_id: int, text: str) -> bool:
        """Заметка к покупке: один текстовый шаг, дальше снова карточка."""
        state = self.db.get_state(user_id)
        if not state or state[0] != "order_note":
            return False
        order_id = int(state[1].get("order") or 0)
        self.db.clear_state(user_id)
        note = "" if text.lower() in ("без заметки", "-") else text
        self.db.set_order_note(order_id, note)
        self.admin_order_card(chat_id, order_id)
        return True

    def handle_add_product_text(self, chat_id: int, user_id: int, text: str) -> bool:
        state = self.db.get_state(user_id)
        if not state or state[0] != "admin_add":
            return False
        data = dict(state[1])
        step = str(data.get("step", ""))

        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(
                chat_id,
                "Черновик выброшен.",
                inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]] + nav_rows()),
            )
            return True
        if text.startswith("/") and text.lower() != "/skip":
            # не путаем команды администратора с ответами мастера
            return False

        if step == "new_category":
            name = text.strip()
            if not name:
                return True
            if len(name) > ADD_CATEGORY_LIMIT:
                self.api.send_message(
                    chat_id,
                    f"Название раздела — максимум {ADD_CATEGORY_LIMIT} знака: оно попадает в кнопки.\n"
                    "Напиши короче.",
                )
                return True
            try:
                category_id = self.catalog.add_category(name)
            except (ValueError, OSError) as exc:
                self.catalog_refused(chat_id, "РАЗДЕЛ НЕ СОЗДАН", exc)
                return True
            data = {"step": "name", "category": category_id}
            self.db.set_state(user_id, "admin_add", data)
            self.api.send_message(
                chat_id,
                f"Раздел «{esc(name)}» создан.\n\n{add_step_text(2, ADD_STEPS[0][1])}",
            )
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
        if step == "name" and len(value) > ADD_NAME_LIMIT:
            self.api.send_message(
                chat_id,
                f"Название длиннее {ADD_NAME_LIMIT} знаков — в кнопки витрины оно не влезет.\n"
                "Сократи до сути, подробности допишешь в описание.",
            )
            return True
        if step == "description" and len(value) > ADD_DESCRIPTION_LIMIT:
            self.api.send_message(
                chat_id,
                f"Описание длиннее {ADD_DESCRIPTION_LIMIT} знаков — карточка вещи не влезет "
                "в одно сообщение Telegram.\nСократи: две-четыре фразы читают, простыню нет.",
            )
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
            # Считаем по худшему случаю: id вещи занимает до 40 байт, и если
            # проверить с заглушкой, callback при публикации выйдет за 64 байта.
            too_long = next((size for size in sizes
                             if ":" in size or len(size.encode("utf-8")) > MAX_SIZE_BYTES), None)
            if too_long is not None:
                self.api.send_message(
                    chat_id,
                    f"Размер «{esc(too_long)}» не влезет в кнопку: максимум {MAX_SIZE_BYTES} байт "
                    "(кириллическая буква занимает два).\n\n"
                    "Пиши коротко: <code>S, M, XL</code>, <code>ONE SIZE</code>, <code>48-50</code>. "
                    "Двоеточие нельзя — из него собран callback кнопки.",
                )
                return True
            data["sizes"] = sizes
        else:
            data[step] = value

        position = ADD_KEYS.index(step) + 1
        if position < len(ADD_STEPS):
            data["step"] = ADD_KEYS[position]
            self.db.set_state(user_id, "admin_add", data)
            self.api.send_message(chat_id, add_step_text(position + 2, ADD_STEPS[position][1]))
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
        self.show_add_confirm(chat_id, product)
        return True

    def show_add_confirm(self, chat_id: int, product: dict[str, Any]) -> None:
        """Последний шаг мастера: та же карточка, что увидит покупатель.

        Расхождений между черновиком и витриной не остаётся, а правки не
        приходится держать в уме. Экран показывается и повторно — если владелец
        вместо кнопки написал текст.
        """
        category = next(
            (str(item["name"]) for item in self.catalog.categories if item["id"] == product.get("category")),
            "Каталог",
        )
        self.api.send_message(
            chat_id,
            add_step_text(ADD_TOTAL, "Проверь — так карточку увидит покупатель.")
            + f"\n\n{ICON['catalog']} Раздел: {esc(category)}\n\n"
            + self.product_card_text(product),
            inline_keyboard(
                [[(icon("catalog", "Опубликовать →"), "add:publish")],
                 [(icon("cancel", "Отмена"), "admin:cancel")]]
            ),
        )

    def catalog_refused(self, chat_id: int, heading: str, exc: Exception, tail: str = "") -> None:
        """Каталог отверг изменение — говорим вслух.

        Исключение из каталога гасится на уровне обработки обновления, поэтому
        без явного ответа админ видит тишину и не знает, что ничего не случилось.
        """
        LOG.error("%s: каталог отверг изменение: %s", heading, exc, exc_info=True)
        lines = [f"<b>{heading}</b>", "", f"Причина: {esc(str(exc))}", ""]
        lines.append(tail or "Файл каталога не тронут, витрина работает как прежде.")
        self.api.send_message(
            chat_id,
            "\n".join(lines),
            inline_keyboard([[(icon("stock", "Новая вещь"), "adm:add"),
                              (icon("tools", "Управление"), "adm:panel")]]),
        )

    def publish_product(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "admin_add" or not state[1].get("preview"):
            self.api.send_message(chat_id, "Черновик не найден. Начни заново: /add")
            return
        try:
            product = self.catalog.add_product(dict(state[1]["preview"]))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self.catalog_refused(
                chat_id, "ВЕЩЬ НЕ ОПУБЛИКОВАНА", exc,
                "Файл каталога цел, витрина работает. Черновик не опубликован — "
                "начни заново и напиши значения короче.",
            )
            return
        self.db.clear_state(user_id)
        self.db.event(user_id, "product_added", {"product_id": product["id"]})
        self.api.send_message(
            chat_id,
            f"<b>ВЕЩЬ ОПУБЛИКОВАНА</b>\n\n"
            f"{esc(product['name'])}\n"
            f"id: <code>{esc(product['id'])}</code>\n\n"
            "Она уже в витрине. Скрыть — <code>/hide id</code>, вернуть — <code>/show id</code>.",
            inline_keyboard(
                [[(icon("catalog", "Смотреть в витрине →"), f"product:{product['id']}")],
                 [(icon("stock", "Добавить ещё"), "adm:add"), (icon("tools", "Управление"), "adm:panel")]]
            ),
        )

    def segment_keyboard(self) -> dict[str, Any]:
        """Кому пишем: сегменты в две колонки, отмена — отдельной строкой."""
        options = [(label, f"seg:{key}") for key, label in SEGMENT_LABELS.items()]
        options += [
            (f"Интерес: {category['name']}", f"seg:interest:{category['id']}")
            for category in self.catalog.categories[:6]
        ]
        rows = option_rows(options)
        rows.append([(icon("cancel", "Отмена"), "admin:cancel")])
        return inline_keyboard(rows)

    def _who(self, user_id: int) -> str:
        user = self.db.get_user(user_id)
        if user and user["username"]:
            return f"@{user['username']}"
        if user and user["first_name"]:
            return str(user["first_name"])
        return str(user_id)

    def admin_access(self, chat_id: int) -> None:
        """Кто в доступе: владельцы из настройки и назначенные админы."""
        lines = ["<b>ДОСТУП КОМАНДЫ</b>", ""]
        for owner_id in sorted(self.settings.admin_ids):
            lines.append(f"{ICON['ok']} {esc(self._who(owner_id))} — владелец")
        rows: list[list[tuple[str, str]]] = []
        for row in self.db.admin_rows():
            who = self._who(int(row["user_id"]))
            lines.append(f"{ICON['tools']} {esc(who)} — админ")
            rows.append([(icon("cancel", f"Снять {who}"), f"access:revoke:{int(row['user_id'])}")])
        if rows:
            lines.append("\nСнять — кнопкой или <code>/revoke id</code>.")
        else:
            lines.append("\nАдминов пока нет. Назначить: <code>/grant id или @username</code>.")
        rows.append(staff_nav_rows()[0])
        self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(rows))

    def _resolve_user(self, argument: str) -> int | None:
        argument = argument.strip().lstrip("@")
        if argument.isdigit():
            return int(argument)
        if not argument:
            return None
        row = self.db.connection().execute(
            "SELECT user_id FROM users WHERE lower(username)=lower(?)", (argument,)).fetchone()
        return int(row["user_id"]) if row else None

    def grant_admin(self, chat_id: int, user_id: int, argument: str) -> None:
        if not self.is_owner(user_id):
            self.api.send_message(chat_id, "Назначать админов может только владелец.",
                                  inline_keyboard(staff_nav_rows()))
            return
        target = self._resolve_user(argument)
        if not argument:
            self.api.send_message(chat_id, "Использование: <code>/grant id или @username</code>",
                                  inline_keyboard(staff_nav_rows()))
            return
        if target is None or self.db.get_user(target) is None:
            self.api.send_message(
                chat_id, "Такого человека нет в базе: назначить можно того, "
                         "кто уже писал боту.", inline_keyboard(staff_nav_rows()))
            return
        if target in self.settings.admin_ids:
            self.api.send_message(chat_id, f"{esc(self._who(target))} — владелец, он уже в доступе.",
                                  inline_keyboard(staff_nav_rows()))
            return
        if self.db.is_admin_id(target):
            self.api.send_message(chat_id, f"{esc(self._who(target))} уже админ.",
                                  inline_keyboard(staff_nav_rows()))
            return
        self.db.add_admin(target, user_id)
        self.db.event(user_id, "admin_granted", {"target": target})
        self.api.send_message(chat_id, f"{ICON['ok']} {esc(self._who(target))} — теперь админ: "
                                       "пульт и покупки команды открыты.",
                              inline_keyboard(staff_nav_rows()))
        self.admin_access(chat_id)

    def revoke_admin(self, chat_id: int, user_id: int, target: int) -> None:
        if not self.is_owner(user_id):
            self.api.send_message(chat_id, "Снимать доступ может только владелец.",
                                  inline_keyboard(staff_nav_rows()))
            return
        if not self.db.is_admin_id(target):
            self.api.send_message(chat_id, f"{esc(self._who(target))} не админ — снимать нечего.",
                                  inline_keyboard(staff_nav_rows()))
            return
        self.db.remove_admin(target)
        self.db.event(user_id, "admin_revoked", {"target": target})
        self.api.send_message(chat_id, f"{ICON['cancel']} Доступ снят: {esc(self._who(target))}.",
                              inline_keyboard(staff_nav_rows()))
        self.admin_access(chat_id)

    def admin_reports(self, chat_id: int) -> None:
        """Журнал рассылок: отчёты потока видны и после перезапуска бота."""
        rows = self.db.recent_broadcasts(5)
        if not rows:
            self.api.send_message(
                chat_id, "<b>ЖУРНАЛ РАССЫЛОК</b>\n\nРассылок ещё не было.\n"
                         "<code>/broadcast текст</code> — и отчёт ляжет сюда.",
                inline_keyboard(staff_nav_rows()))
            return
        lines = ["<b>ЖУРНАЛ РАССЫЛОК</b>", ""]
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            label = audience_label(str(payload.get("segment", "all")), self.catalog.categories)
            lines.append(f"{esc(str(row['created_at'])[:16])} · {esc(label)} · "
                         f"доставлено {payload.get('delivered', 0)}, ошибок {payload.get('failed', 0)}")
        self.api.send_message(chat_id, "\n".join(lines), inline_keyboard(staff_nav_rows()))

    def admin_draws(self, chat_id: int) -> None:
        """История розыгрышей: тираж без записи нельзя проверить постфактум."""
        draws = self.db.recent_draws(5)
        if not draws:
            self.api.send_message(
                chat_id, "<b>РОЗЫГРЫШИ</b>\n\nТиражей ещё не было.\n"
                         "<code>/giveaway N</code> выберет победителей из топа приглашений.",
                inline_keyboard(staff_nav_rows()))
            return
        threshold = self.giveaway_threshold()
        lines = ["<b>РОЗЫГРЫШИ</b>", ""]
        for row in draws:
            payload = json.loads(row["payload"] or "{}")
            winners = payload.get("winners") or []
            names = ", ".join(esc(self._who(int(w))) for w in winners) or "—"
            lines.append(f"{esc(str(row['created_at'])[:16])} · из {payload.get('pool', 0)}: {names}")
        lines.append(f"\nПорог приоритета: {threshold} приглашённых.")
        rows = [[(icon("cancel", "Порог −"), "draw:down"), (icon("ok", "Порог +"), "draw:up")]]
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(rows + staff_nav_rows()))

    def ask_broadcast_segment(self, chat_id: int, text: str) -> None:
        """Кому пишем: экран показывается и повторно, если ответили текстом."""
        self.api.send_message(
            chat_id,
            f"<b>КОМУ ПИШЕМ?</b>\n\n{esc(text)}",
            self.segment_keyboard(),
        )

    def preview_broadcast(self, chat_id: int, user_id: int, segment: str) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "broadcast_pending":
            self.api.send_message(
                chat_id,
                "Черновик рассылки не найден — он живёт до первой отправки.\n"
                "Собери новый: <code>/broadcast текст</code>.",
                inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
            )
            return
        text = str(state[1].get("text", "")).strip()
        audience = self.db.broadcast_audience(segment)
        self.db.set_state(user_id, "broadcast_pending", {"text": text, "segment": segment})
        label = audience_label(segment, self.catalog.categories)
        self.api.send_message(
            chat_id,
            f"<b>ПРЕДПРОСМОТР РАССЫЛКИ</b>\n\n{esc(text)}\n\n"
            f"{ICON['account']} Аудитория: {esc(label)}\n"
            f"{ICON['channel']} Получателей: {len(audience)}",
            inline_keyboard(
                [[(icon("channel", "Отправить →"), "admin:broadcast_confirm")],
                 [(icon("cancel", "Отмена"), "admin:cancel")]]
            ),
        )

    def confirm_broadcast(self, chat_id: int, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "broadcast_pending":
            self.api.send_message(
                chat_id,
                "Черновик рассылки не найден — отправлять нечего.",
                inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
            )
            return
        text = str(state[1].get("text", "")).strip()
        segment = str(state[1].get("segment", "all"))
        self.db.clear_state(user_id)
        audience = self.db.broadcast_audience(segment)
        if not audience:
            self.api.send_message(
                chat_id,
                "В этом сегменте никого нет — рассылка не ушла.\nВыбери другой сегмент.",
                self.segment_keyboard(),
            )
            return
        self.db.kv_set("last_broadcast", compact_json({"text": text, "segment": segment}))
        # Рассылка идёт в отдельном потоке: раньше она выполнялась прямо в
        # цикле опроса и на 10 000 получателей морозила бота примерно на 7 минут.
        thread = threading.Thread(
            target=self._run_broadcast,
            args=(chat_id, user_id, text, segment, audience),
            name="broadcast",
            daemon=True,
        )
        thread.start()
        people = plural(len(audience), "получатель", "получателя", "получателей")
        self.api.send_message(
            chat_id,
            f"Рассылка пошла: {len(audience)} {people}.\n"
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
                    self.api.send_message(recipient, esc(text), self.main_menu(recipient))
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
                self.api.send_message(
                    chat_id,
                    f"<b>РАССЫЛКА ГОТОВА</b>\n\nДоставлено: {delivered}. Ошибок: {failed}.",
                    inline_keyboard([[(icon("tools", "Управление"), "adm:panel")]]),
                )
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
            "<b>ПРОФИЛЬ СОХРАНЁН</b>\n\n"
            + (esc(summary) if summary else "Контакт записан. Следующая покупка подставит эти данные.")
            + "\n\nМожно сразу смотреть выпуск.",
            self.main_menu(chat_id),
        )

    def handle_web_waitlist(self, chat_id: int, user: dict[str, Any], payload: dict[str, Any]) -> None:
        """Accept a size waitlist request from the Mini App."""
        user_id = int(user["id"])
        product = self.catalog.get(str(payload.get("product_id", "")))
        size = str(payload.get("size", ""))
        if not product or size not in {str(item) for item in product.get("sizes", [])}:
            self.api.send_message(chat_id, "Этот размер уже не ждётся — вещь снята или размер неверный.", self.main_menu(chat_id))
            return
        added = self.db.add_to_waitlist(user_id, product, size)
        self.db.event(user_id, "webapp_waitlist", {"product_id": product["id"], "size": size})
        if added:
            self.api.send_message(
                chat_id,
                f"Записал из витрины: <b>{esc(product['name'])}</b>, размер {esc(size)}.\n"
                "Вернётся — напишем первыми.",
                self.main_menu(chat_id),
            )
        else:
            self.api.send_message(
                chat_id,
                f"Этот размер уже в листе ожидания: <b>{esc(product['name'])}</b> · {esc(size)}.",
                self.main_menu(chat_id),
            )

    def queue_checkout_notifications(
        self, receipt: dict[str, Any], user_id: int, phone: str, note: str,
        notify_user: int | None, source: str = "витрины",
    ) -> None:
        payment_id = receipt["payment_id"]
        order_lines = [f"• {esc(line['name'])} · {esc(line['size'])} · {line['quantity']} шт."
                       + (f" · {esc(line['person_label'])} {esc(line['person'])}" if line['person'] else "")
                       for line in receipt["lines"]]
        if notify_user:
            self.offer_payment(notify_user, payment_id, receipt["amount_rub"], order_lines, source, queue_only=True)
        if self.settings.manager_chat_id:
            self.db.enqueue_message(
                f"checkout:{payment_id}:manager", self.settings.manager_chat_id,
                f"<b>НОВАЯ ПОКУПКА ИЗ {esc(source).upper()}</b>\nКлиент: {user_id}\nТелефон: {esc(phone)}\n"
                f"{esc(note or 'Адрес не указан')}\nК оплате: {esc(receipt['amount_label'])}\n"
                + "\n".join(order_lines),
            )

    def accept_checkout(
        self,
        user: dict[str, Any],
        customer: dict[str, Any],
        phone: str,
        valid_items: list[tuple[dict[str, Any], str, int, str]],
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
        request_base, fingerprint = self.checkout_identity(payload)
        note = self.customer_note(customer)
        lines = []
        for product, size, quantity, person in valid_items:
            line_note = f"{note} · {person_label(product)}: {person}" if person else note
            lines.append({"product_id": product["id"], "name": product["name"], "size": size,
                          "phone": phone, "quantity": quantity, "note": line_note,
                          "amount_rub": self.line_amount(product, quantity), "person": person,
                          "person_label": person_label(product) if person else ""})
        total = sum(line["amount_rub"] for line in lines)
        stars = stars_amount(total, self.settings.stars_rub_per_star) if self.settings.stars_enabled else 0
        receipt, created = self.db.create_checkout(
            user_id, request_base, fingerprint, lines, stars,
            queue_notifications=lambda receipt: self.queue_checkout_notifications(receipt, user_id, phone, note, notify_user),
        )
        payment_id = receipt["payment_id"]
        total = receipt["amount_rub"]
        description = f"{self.settings.brand_name}: оплата {payment_id}"
        methods = self.build_pay_methods(payment_id, total, description)
        return {"ok": True, **receipt, "methods": methods,
                "status": "awaiting_payment" if self.db.payment_is_payable(payment_id) else self.db.get_payment(payment_id)["status"]}

    @staticmethod
    def checkout_identity(payload: dict[str, Any]) -> tuple[str, str]:
        raw = str(payload.get("request_id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", raw):
            raise ValueError("Некорректный номер покупки. Обнови витрину.")
        body = {key: value for key, value in payload.items() if key != "request_id"}
        fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return raw, fingerprint

    def resume_payment(self, user_id: int, payment_id: str) -> dict[str, Any]:
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != int(user_id):
            return {"ok": False, "error": "Оплата не найдена."}
        if not self.db.payment_is_payable(payment_id):
            return {"ok": False, "error": "Оплата уже неактуальна. Проверь мои заявки."}
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

    def checkout_web_payload(self, user: dict[str, Any], payload: dict[str, Any], notify_user: int | None = None) -> dict[str, Any]:
        try:
            request_id, fingerprint = self.checkout_identity(payload)
            previous = self.db.checkout_receipt(int(user["id"]), request_id, fingerprint)
            if previous:
                payment_id = previous["payment_id"]
                payable = self.db.payment_is_payable(payment_id)
                return {"ok": True, **previous,
                        "status": "awaiting_payment" if payable else self.db.get_payment(payment_id)["status"],
                        "methods": self.build_pay_methods(payment_id, previous["amount_rub"], f"{self.settings.brand_name}: оплата {payment_id}") if payable else []}
            legacy_prefix = request_id + "-"
            legacy = self.db.connection().execute(
                "SELECT id FROM orders WHERE user_id=? AND substr(request_id, 1, ?)=? LIMIT 1",
                (int(user["id"]), len(legacy_prefix), legacy_prefix),
            ).fetchone()
            if legacy:
                return {"ok": False, "error": "Эта покупка уже принята ранее. Открой «Мои покупки» для оплаты или связи с менеджером."}
            return self._checkout_web_payload(user, payload, notify_user)
        except (ValueError, PriceError) as exc:
            return {"ok": False, "error": str(exc)}

    def _checkout_web_payload(
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
        valid_items: list[tuple[dict[str, Any], str, int, str]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return {"ok": False, "error": "Некорректная позиция в заявке. Обнови корзину и попробуй снова."}
            product = self.catalog.get(str(raw_item.get("product_id", "")))
            size = str(raw_item.get("size", ""))
            if not product or size not in {str(item) for item in product.get("sizes", [])}:
                return {"ok": False, "error": "Одна из вещей или размеров уже недоступна. Обнови корзину: заявка не создана."}
            quantity = raw_item.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 20:
                return {"ok": False, "error": "Количество в каждой позиции должно быть целым числом от 1 до 20."}
            person = sanitize_personalization(product, raw_item.get("person"))
            valid_items.append((product, size, quantity, person))
        if not valid_items:
            return {"ok": False, "error": "Некоторые вещи уже закончились. Обнови витрину и выбери снова."}
        for product, _, quantity, _ in valid_items:
            self.line_amount(product, quantity)
        self.db.upsert_user(user)
        return self.accept_checkout(user, customer, phone, valid_items, payload, notify_user=notify_user)

    def handle_web_app_data(self, chat_id: int, user: dict[str, Any], raw_data: str) -> None:
        """Accept an order sent by Telegram Web App ``sendData``.

        The browser is never trusted with the price or product details: only
        active product ids and sizes from the server-side catalog are accepted.
        This keeps the miniapp convenient while the bot remains the source of
        truth for the order and manager notification.
        """
        try:
            payload = json.loads(raw_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.api.send_message(chat_id, "Не получилось прочитать покупку из витрины. Открой её ещё раз.", self.main_menu(chat_id))
            return
        if not isinstance(payload, dict):
            self.api.send_message(chat_id, "Неизвестный формат покупки. Открой витрину заново.", self.main_menu(chat_id))
            return
        if payload.get("type") == "waitlist":
            self.handle_web_waitlist(chat_id, user, payload)
            return
        if payload.get("type") == "profile":
            self.handle_web_profile(chat_id, user, payload)
            return
        if payload.get("type") != "order":
            self.api.send_message(chat_id, "Неизвестный формат покупки. Открой витрину заново.", self.main_menu(chat_id))
            return
        result = self.checkout_web_payload(user, payload, notify_user=chat_id)
        self.flush_notifications()
        if not result.get("ok"):
            self.api.send_message(chat_id, str(result.get("error") or "Не получилось принять покупку."), self.main_menu(chat_id))

    # Оплата остаётся в истории: счета, отказы и подтверждения ищут позже,
    # поэтому такие экраны не сворачиваются в одно меняющееся сообщение.
    NON_EDITABLE_CALLBACKS = ("pay:",)

    def take_screen_edit(self, chat_id: int) -> int | None:
        """Отдать и погасить цель правки: одно нажатие — один экран на месте."""
        target = self._screen_edit
        self._screen_edit = None
        if target and target[0] == int(chat_id):
            return target[1]
        return None

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data", "")
        user = callback["from"]
        chat = callback["message"]["chat"]
        chat_id = chat["id"]
        user_id = int(user["id"])
        if chat.get("type") != "private":
            self.answer_press(callback_id, "Открой бота в личных сообщениях")
            return
        self.answer_press(callback_id)
        pressed = callback["message"].get("message_id")
        if (isinstance(pressed, int) and not isinstance(pressed, bool) and pressed > 0
                and not data.startswith(self.NON_EDITABLE_CALLBACKS)):
            self._screen_edit = (int(chat_id), pressed)
        try:
            self.db.upsert_user(user)
            if self.route_callback(callback_id, chat_id, user_id, data):
                return
            # Кнопка устарела или пришла из старого сообщения: не молчим и не
            # пугаем ошибкой, а открываем актуальный экран.
            self.stale(chat_id, "Эта кнопка устарела.")
        except Exception:
            LOG.exception("Callback %r failed for user %s", data, user_id)
            self.recover(chat_id, "Экран не открылся.")
        finally:
            self._screen_edit = None

    def route_callback(self, callback_id: str, chat_id: int, user_id: int, data: str) -> bool:
        """Разбор нажатия. Возвращает False, если кнопка боту неизвестна."""
        if data.startswith("pay:"):
            parts = data.split(":")
            if len(parts) == 3:
                self.start_method_pay(chat_id, user_id, parts[1], parts[2])
                return True
            return False
        if data == "menu":
            self.send_menu(chat_id)
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
            parts = data.split(":", 2)
            if len(parts) < 3:
                return False
            self.select_size(chat_id, user_id, parts[1], parts[2], f"callback:{callback_id}")
        elif data.startswith("wait:"):
            self.ask_waitlist_size(chat_id, data.split(":", 1)[1])
        elif data.startswith("wsize:"):
            parts = data.split(":", 2)
            if len(parts) < 3:
                return False
            self.confirm_waitlist(chat_id, user_id, parts[1], parts[2])
        elif data == "waits":
            self.show_my_waits(chat_id, user_id)
        elif data.startswith("wstop:"):
            self.stop_waiting(chat_id, user_id, data.split(":", 1)[1])
        elif data == "account":
            self.api.send_message(chat_id, self.account_text(user_id), self.account_menu(user_id))
        elif data == "support":
            self.show_support(chat_id, user_id)
        elif data == "help":
            self.api.send_message(
                chat_id, self.help_text(), inline_keyboard(nav_rows(("Поддержка", "support")))
            )
        elif data == "ask":
            self.support_orders(chat_id, user_id)
        elif data.startswith("ask:"):
            try:
                self.forward_support(chat_id, user_id, int(data.split(":", 1)[1]))
            except ValueError:
                return False
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
        elif data == "teaser":
            self.db.event(user_id, "teaser_open")
            keyboard = inline_keyboard([[(icon("catalog", "Каталог"), "catalog")]] + nav_rows())
            if not self.send_teaser(chat_id, keyboard=keyboard):
                self.api.send_message(
                    chat_id,
                    "Ролик сейчас не открывается. Загляни в витрину — там он лежит целиком."
                    + self.cta("drop"),
                    keyboard,
                )
        elif data == "about":
            self.api.send_message(
                chat_id,
                f"<b>О БРЕНДЕ</b>\n\n"
                f"{esc(self.settings.brand_name)}\n"
                "Сила и честь. Одежда для своих: плотная, честная, без лишнего шума. "
                "Малые тиражи, городская форма, выпуски без повторов."
                + (f"\n\n{ICON['support']} Вопросы: @{esc(self.settings.support_username)}" if self.settings.support_username else "")
                + self.cta("channel"),
                inline_keyboard(self.channel_rows() + nav_rows(("Кабинет", "account"))),
            )
        elif data == "size_guide" or data.startswith("size_guide:"):
            self.show_size_guide(chat_id, user_id, data.split(":", 1)[1] if ":" in data else "")
        elif data == "my_orders":
            self.show_my_orders(chat_id, user_id)
        elif data.startswith("ord:"):
            try:
                self.show_order(chat_id, user_id, int(data.split(":", 1)[1]))
            except ValueError:
                return False
        elif data.startswith("receipt:"):
            self.show_receipt(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("draft:"):
            self.open_draft(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("ucancel:"):
            try:
                self.cancel_own_order(chat_id, user_id, int(data.split(":", 1)[1]))
            except ValueError:
                self.api.send_message(
                    chat_id, "Некорректный номер покупки.", self.main_menu(chat_id)
                )
        elif data.startswith("seg:") and self.is_admin(user_id):
            self.preview_broadcast(chat_id, user_id, data.split(":", 1)[1])
        elif data == "admin:broadcast_confirm" and self.is_admin(user_id):
            self.confirm_broadcast(chat_id, user_id)
        elif data.startswith("order:") and self.is_admin(user_id):
            parts = data.split(":", 2)
            if len(parts) < 3:
                return False
            try:
                self.update_order_status(chat_id, int(parts[1]), parts[2])
            except (TypeError, ValueError):
                self.api.send_message(chat_id, "Некорректная команда для покупки.")
        elif data == "admin:cancel" and self.is_admin(user_id):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменено.")
        elif data.startswith("aord:") and self.is_admin(user_id):
            try:
                self.admin_order_card(chat_id, int(data.split(":", 1)[1]))
            except ValueError:
                self.stale(chat_id, "Эта кнопка устарела.")
        elif data.startswith("access:revoke:") and self.is_owner(user_id):
            try:
                self.revoke_admin(chat_id, user_id, int(data.split(":")[2]))
            except (IndexError, ValueError):
                return False
        elif data.startswith("aclient:") and self.is_admin(user_id):
            try:
                self.admin_customer(chat_id, int(data.split(":", 1)[1]))
            except (IndexError, ValueError):
                return False
        elif data.startswith("shelf:") and self.is_admin(user_id):
            pid = data.split(":", 1)[1]
            product = next(
                (pr for pr in self.catalog.data.get("products", []) if str(pr.get("id")) == pid), None)
            if product is None:
                self.stale(chat_id, "Такой вещи нет в каталоге.")
            else:
                self.catalog.set_active(pid, not bool(product.get("active", True)))
                self.admin_shelf(chat_id)
        elif data in {"draw:up", "draw:down"} and self.is_owner(user_id):
            step = 1 if data == "draw:up" else -1
            new = min(10, max(1, self.giveaway_threshold() + step))
            self.db.kv_set("giveaway_min_invites", str(new))
            self.admin_draws(chat_id)
        elif data.startswith("anote:") and self.is_admin(user_id):
            order_id = int(data.split(":", 1)[1])
            self.db.set_state(user_id, "order_note", {"order": order_id})
            self.api.send_message(
                chat_id,
                f"Заметка к покупке №{order_id}? Ответь текстом — сохраню в карточку.\n"
                "Пришли «без заметки», чтобы стереть текущую.",
            )
            return True
        elif data == "rlist" and self.is_admin(user_id):
            self.admin_refunds(chat_id)
            return True
        elif data.startswith("rreasons:") and self.is_admin(user_id):
            self.admin_refund_reasons(chat_id, data.split(":", 1)[1])
            return True
        elif data.startswith("rreason:") and self.is_admin(user_id):
            _, pid, key = (data.split(":", 2) + ["", "", ""])[:3]
            label = REFUND_REASON_LABELS.get(key, "")
            if label:
                self.db.kv_set(f"rreason:{pid}", label)
            self.admin_refunds(chat_id)
            return True
        elif data.startswith("rdone:") and self.is_admin(user_id):
            pid = data.split(":", 1)[1]
            with self.db.lock, self.db.connection() as conn:
                conn.execute("UPDATE payments SET status='refunded' WHERE payment_id=?",
                             (pid,))
            self.db.event(chat_id, "payment_refunded", {"payment_id": pid})
            self.admin_refunds(chat_id)
            return True
        elif data.startswith("wnotify:") and self.is_admin(user_id):
            parts = data.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                return False
            self.restock_reply(chat_id, parts[1], parts[2])
        elif data.startswith("adm:") and self.is_admin(user_id):
            action = data.split(":", 1)[1]
            if action == "add":
                self.start_add_product(chat_id, user_id)
            elif action in {"summary", "stats"}:
                self.admin_summary(chat_id)
            elif action == "money":
                self.admin_money(chat_id)
            elif action == "digest":
                self.admin_digest(chat_id)
            elif action == "access":
                self.admin_access(chat_id)
            elif action == "draws":
                self.admin_draws(chat_id)
            elif action in {"panel", "orders", "waitlist", "top", "export", "reload"}:
                self.admin_command(chat_id, user_id, f"/{action}")
            else:
                return False
        else:
            return False
        return True

    def handle_message(self, message: dict[str, Any]) -> None:
        """Обработка сообщения: сбой объясняем экраном, а не тишиной."""
        try:
            self.route_message(message)
        except Exception:
            LOG.exception("Failed to handle message")
            chat = message.get("chat") if isinstance(message, dict) else None
            chat = chat if isinstance(chat, dict) else {}
            chat_id = chat.get("id")
            if isinstance(chat_id, int) and str(chat.get("type") or "private") == "private":
                self.recover(chat_id, "Сообщение не обработалось.")

    def route_message(self, message: dict[str, Any]) -> None:
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
        if self.handle_order_note_text(chat_id, user_id, text):
            return
        if text.startswith("/") and self.admin_command(chat_id, user_id, text):
            return
        if text.startswith("/") and self.user_command(chat_id, user_id, text):
            return
        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменили.", remove_keyboard())
            self.send_menu(chat_id)
            return
        state = self.db.get_state(user_id)
        phone: str | None = None
        contact = message.get("contact")
        if contact and int(contact.get("user_id", user_id)) == user_id:
            phone = normalize_phone(str(contact.get("phone_number", "")))
        elif state and state[0] in PHONE_STATES:
            phone = normalize_phone(text)
        if phone:
            self.save_phone_and_continue(chat_id, user_id, phone)
            return
        if state and state[0] == "awaiting_consent":
            # Здесь ждут не номер, а решение кнопкой: ответ без кнопок был тупиком.
            self.api.send_message(
                chat_id,
                "Сначала согласие — оно выбирается кнопкой ниже.\n"
                "Номер пришли следом: сохраним сразу после «Согласен».",
                consent_keyboard(),
            )
            return
        if state and state[0] in PHONE_STATES:
            self.api.send_message(
                chat_id,
                "Номер не распознан. Формат: +79991234567\n"
                "Или нажми «Отправить номер» ниже — так без опечаток.",
                contact_keyboard(),
            )
            return
        if state:
            self.answer_button_state(chat_id, user_id, state)
            return
        self.api.send_message(
            chat_id,
            "Нажми кнопки ниже — так быстрее. Каталог, покупки и поддержка там." + self.cta("drop"),
            self.main_menu(chat_id),
        )

    def answer_button_state(self, chat_id: int, user_id: int, state: tuple[str, dict[str, Any]]) -> None:
        """Текст пришёл там, где ждут кнопку: показываем тот же экран заново.

        Раньше любое неизвестное состояние считалось ожиданием номера, поэтому
        владелец посреди мастера или рассылки на каждое сообщение получал
        «Номер не распознан» и не мог понять, где он застрял.
        """
        name = str(state[0])
        data = dict(state[1] or {})
        if name == "admin_add":
            step = str(data.get("step", ""))
            if step == "new_category":
                self.api.send_message(
                    chat_id,
                    "<b>НОВЫЙ РАЗДЕЛ</b>\n\nНазвание вводится текстом. Например: Верхняя одежда",
                )
            elif step == "confirm" and isinstance(data.get("preview"), dict):
                self.show_add_confirm(chat_id, data["preview"])
            else:
                self.start_add_product(chat_id, user_id)
            return
        if name == "broadcast_pending":
            text = str(data.get("text", "")).strip()
            if text:
                self.ask_broadcast_segment(chat_id, text)
                return
        # Состояние неизвестное или пустое: не держим человека в подвешенном.
        self.db.clear_state(user_id)
        self.stale(chat_id, "Не понял, на каком шаге мы остановились.")

    def user_command(self, chat_id: int, user_id: int, text: str) -> bool:
        """Команды покупателя: их показывает системная кнопка «Меню».

        Команды — быстрый вход, а не обязательное знание: всё то же самое
        доступно кнопками главного меню.
        """
        command = text.split()[0].lower().split("@")[0] if text.split() else ""
        if command in {"/menu", "/start"}:
            self.send_menu(chat_id)
        elif command == "/catalog":
            self.show_catalog(chat_id, user_id)
        elif command in {"/orders", "/purchases"}:
            self.show_my_orders(chat_id, user_id)
        elif command == "/account":
            self.api.send_message(chat_id, self.account_text(user_id), self.account_menu(user_id))
        elif command == "/support":
            self.show_support(chat_id, user_id)
        elif command == "/help":
            self.api.send_message(
                chat_id, self.help_text(), inline_keyboard(nav_rows(("Поддержка", "support")))
            )
        else:
            return False
        return True

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
        """Only configured proxies may supply a single, overwritten X-Real-IP."""
        peer = str(self.client_address[0]) if self.client_address else "unknown"
        if self.settings and peer in self.settings.trusted_proxy_ips:
            try:
                return str(ipaddress.ip_address(self.headers.get("X-Real-IP", "")))
            except ValueError:
                pass
        return peer

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
        orders = []
        for row in rows:
            order = public_order_payload(row)
            if row["payment_id"]:
                payment = self.db.get_payment(row["payment_id"])
                order["payment_status"] = payment["status"] if payment else "missing"
                order["can_pay"] = self.db.payment_is_payable(row["payment_id"])
                if payment and payment["status"] == "refund_required":
                    order["status_label"] += " · требуется возврат"
                    order["can_cancel"] = False
            orders.append(order)
        self._json({"orders": orders, "source": "bot"})

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
            updated, changed = self.db.set_order_status(order_id, "cancelled", customer_id=int(user["id"]))
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
                    f"Клиент отменил покупку №{order_id}: {esc(updated['product_name'] if updated else '')}.",
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
        bot = self._bot()
        self.db.mark_payment_paid(payment_id, method, provider_id,
                                  queue_notifications=(lambda pid: bot.notify_paid(pid, queue_only=True)) if bot else None)
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

    def _waitlist(self) -> None:
        if not self.db or not self.catalog or not self.settings or not self.settings.token:
            self._json({"error": "Открой витрину из бота."}, 401)
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота."}, 401)
            return
        if not self._rate_ok(CHECKOUT_LIMITER, "waitlist", user):
            return
        payload = self._read_json_body() or {}
        product = self.catalog.get(str(payload.get("product_id") or ""))
        size = str(payload.get("size") or "")
        if not product or size not in {str(item) for item in product.get("sizes", [])}:
            self._json({"error": "Вещь или размер недоступны."}, 400)
            return
        self.db.upsert_user(user)
        added = self.db.add_to_waitlist(int(user["id"]), product, size)
        self._json({"ok": True, "added": added})

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
                    "media": self._stamp_media(catalog.data.get("media", {})),
                }
            self._write(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/app/":
            self.send_response(308)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        relative = path.lstrip("/")
        if relative in ("", "app"):
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
        suffix = candidate.suffix.lower()
        if suffix in VIDEO_SUFFIXES:
            self._serve_video(candidate)
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._not_found()
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        if candidate.name in ("index.html", "styles.css"):
            # WebView Telegram держит app.js и styles.css в кеше и после правок
            # показывает старую версию. Клеим метку версии по времени файла.
            body = self._stamp_fonts(self._stamp_assets(body))
        if candidate.name != "index.html" and self._has_current_asset_version(candidate):
            cache = "public, max-age=31536000, immutable"
        elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
            cache = "public, max-age=3600"
        elif suffix in {".woff2", ".woff", ".ttf"}:
            # Шрифты неизменяемы и весят больше всего — держим их в кеше долго.
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-cache"
        self._write(body, content_type, 200, cache)

    def _stamp_media(self, media: dict[str, Any]) -> dict[str, Any]:
        """Добавить ?v=<время правки> к роликам и постерам из блока media.

        Видео отдаётся с ``max-age=86400``, а имя файла не меняется: заменив
        ``teaser.mp4``, мы сутки показывали бы всем старую копию из кеша
        браузера. Версия в адресе делает подмену мгновенной.
        """
        stamped: dict[str, Any] = {}
        for key, value in media.items():
            if not isinstance(value, str) or not value or "?" in value or "://" in value:
                stamped[key] = value
                continue
            candidate = self.static_root / value.lstrip("/")
            try:
                version = int(candidate.stat().st_mtime)
            except OSError:
                stamped[key] = value
                continue
            stamped[key] = f"{value}?v={version}"
        return stamped

    def _stamp_assets(self, body: bytes) -> bytes:
        """Версионировать клиентский код и CSS вместе с его шрифтами."""
        text = body.decode("utf-8", "replace")
        for name in ("app.js", "checkout.js", "shop-core.js", "viewer3d.js", "styles.css", "refinement.css"):
            asset = self.static_root / name
            try:
                version = self._asset_version(asset)
            except OSError:
                continue
            text = text.replace(f'href="{name}"', f'href="{name}?v={version}"')
            text = text.replace(f'src="{name}"', f'src="{name}?v={version}"')
        return text.encode("utf-8")

    def _asset_version(self, asset: Path) -> str:
        if asset.name == "styles.css":
            # Замена шрифта меняет адрес CSS, даже если сам CSS не редактировали.
            return hashlib.sha256(self._stamp_fonts(asset.read_bytes())).hexdigest()[:16]
        return str(int(asset.stat().st_mtime))

    def _has_current_asset_version(self, asset: Path) -> bool:
        versions = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("v")
        if not versions:
            return False
        try:
            return versions == [self._asset_version(asset)]
        except OSError:
            return False

    def _stamp_fonts(self, body: bytes) -> bytes:
        """Использовать одинаковые версии шрифтов в preload и @font-face."""
        text = body.decode("utf-8", "replace")
        for asset in sorted((self.static_root / "fonts").glob("*.woff2")):
            name = "fonts/" + asset.name
            try:
                version = int(asset.stat().st_mtime)
            except OSError:
                continue
            text = text.replace(f'"{name}"', f'"{name}?v={version}"')
        return text.encode("utf-8")

    def _serve_video(self, candidate: Path) -> None:
        """Отдать видео с поддержкой HTTP Range.

        Без 206 ``<video>`` в iOS/Safari не стартует: первый запрос там —
        ``Range: bytes=0-1``. Тело читается кусками, чтобы ролик не оседал
        в памяти целиком на каждый запрос.
        """
        try:
            size = candidate.stat().st_size
        except OSError:
            self._not_found()
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "video/mp4"
        cache = ("public, max-age=31536000, immutable"
                 if self._has_current_asset_version(candidate) else "public, max-age=86400")
        start, end = 0, size - 1
        partial = False
        raw_range = (self.headers.get("Range") or "").strip()
        if raw_range.startswith("bytes="):
            spec = raw_range[6:].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if not first:
                    # Суффиксный диапазон: последние N байт.
                    length = int(last)
                    if length <= 0:
                        raise ValueError
                    start = max(0, size - length)
                else:
                    start = int(first)
                    if last:
                        end = min(int(last), size - 1)
                if start > end or start >= size:
                    raise ValueError
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self._csp())
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        remaining = length
        try:
            with candidate.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Обычное дело: плеер перемотал и оборвал текущий запрос.
            LOG.debug("Клиент закрыл соединение при отдаче видео")
        except OSError:
            LOG.warning("Не удалось отдать видео %s", candidate.name, exc_info=True)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/my-orders/cancel":
            self._cancel_my_order()
            return
        if path == "/api/waitlist":
            self._waitlist()
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


def notification_loop(bot: BrandBot) -> None:
    try:
        while not STOP_EVENT.is_set():
            try:
                bot.flush_notifications()
            except Exception:
                LOG.exception("Notification retry failed")
            STOP_EVENT.wait(5)
    finally:
        bot.db.close_current()


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
            if not isinstance(updates, list):
                # Ответ не похож на список обновлений. Разбирать его как
                # обновления нельзя: цикл спотыкался бы об одно и то же вечно.
                LOG.warning("getUpdates вернул %s вместо списка", type(updates).__name__)
                updates = []
            for update in updates:
                if not isinstance(update, dict):
                    LOG.warning("Пропущено обновление не-словарь: %.120r", update)
                    continue
                try:
                    if not bot.handle_update(update):
                        LOG.warning("Skipping failed update %s", update.get("update_id"))
                except Exception:
                    # handle_update ловит свои ошибки сам; это страховка от сбоя
                    # вокруг него. Без неё смещение не сдвинется и бот будет до
                    # перезапуска перезапрашивать одно отравленное обновление.
                    LOG.exception("Update %s raised outside handler, skipping", update.get("update_id"))
                raw_id = update.get("update_id")
                if isinstance(raw_id, bool):
                    raw_id = None
                elif isinstance(raw_id, str) and raw_id.isdigit():
                    raw_id = int(raw_id)
                if isinstance(raw_id, int):
                    offset = max(offset, raw_id + 1)
                else:
                    LOG.warning("Обновление без update_id пропущено: %.120r", update)
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
        LOG.warning("MANAGER_CHAT_ID не задан — уведомления о покупках и вопросы никуда не уйдут")
    try:
        ensure_catalog_exists(settings.catalog_path, BASE_DIR / "catalog.json")
        catalog = Catalog(settings.catalog_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        # Частый случай после ручной правки файла. Говорим прямо, что чинить:
        # иначе оператор видит только «Startup failed» и не понимает, где причина.
        LOG.error("catalog.json не прошёл проверку (%s): %s. Исправь файл — без этого бот не запустится.",
                  settings.catalog_path, exc)
        return 2
    try:
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
        # Системная кнопка «Меню» открывает команды бота: покупатель видит свой
        # короткий список, команда — служебный. Витрина остаётся кнопкой в чате.
        api.call("setMyCommands", {"commands": buyer_commands()})
        api.call("setChatMenuButton", {"menu_button": CHAT_MENU_BUTTON})
        try:
            api.call("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION})
            api.call("setMyDescription", {"description": BOT_DESCRIPTION})
        except Exception as exc:
            LOG.warning("Could not set bot profile description: %s", exc)
        for admin_id in settings.admin_ids:
            try:
                api.call(
                    "setMyCommands",
                    {"commands": staff_commands(), "scope": {"type": "chat", "chat_id": admin_id}},
                )
                api.call(
                    "setChatMenuButton",
                    {"chat_id": admin_id, "menu_button": CHAT_MENU_BUTTON},
                )
            except Exception as exc:
                LOG.warning("Could not set staff commands for %s: %s", admin_id, exc)
    except Exception as exc:
        LOG.warning("Could not set Telegram commands or menu button: %s", exc)
    health_server = start_health_server(settings.health_port, catalog, settings, db, api)
    threading.Thread(target=notification_loop, args=(bot,), name="notification-worker", daemon=True).start()

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
