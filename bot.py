#!/usr/bin/env python3
"""Telegram storefront bot for a small clothing brand.

Thematic build: bold brand voice, hard contextual CTAs, channel-first funnel,
referral/giveaway viral loop, size waitlist, segmented broadcasts.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import csv
from collections import OrderedDict
import gzip
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
import shutil
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
ONLINE_PAYMENTS_ENABLED = False  # Временно: оформление через чат магазина.

def order_manager_username() -> str:
    username = os.getenv("ORDER_MANAGER_USERNAME", "").strip().lstrip("@")
    return username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else ""



class RateLimiter:
    """Скользящее окно на память процесса.

    Хватает для одного инстанса за реверс-прокси: защищает от заваливания
    заказами и от перебора отмен по украденному initData. Для нескольких
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
            if key in self.hits:
                # Store the pruned list back: keeps prefix expiry correct and
                # memory bounded even for keys that only get rejected.
                self.hits[key] = recent
            if len(recent) >= self.limit:
                return False
            if key not in self.hits and len(self.hits) >= self.max_keys:
                # Full table: evict the least recently active key instead of
                # rejecting every new customer until entries expire on their own.
                self.hits.popitem(last=False)
            recent.append(stamp)
            self.hits[key] = recent
            self.hits.move_to_end(key)
            return True


# Заказ — дорогая операция (счёт у провайдера, сообщения). Чтение своих
# заказов опрашивается витриной раз в 4 секунды, поэтому лимит выше.
CHECKOUT_LIMITER = RateLimiter(limit=8, window_seconds=60)
READ_LIMITER = RateLimiter(limit=60, window_seconds=60)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".webm", ".mov", ".mp3", ".m4a", ".ogg", ".wav"}
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
REF_RE = re.compile(r"^ref(\d{3,15})$")
# Telegram отказывается принимать файлы крупнее 50 МБ загрузкой по HTTP.
TEASER_UPLOAD_LIMIT = 50 * 1024 * 1024
BOT_SHORT_DESCRIPTION = "Сила и честь. Одежда. Закрытый выпуск."
BOT_DESCRIPTION = (
    "Фирменная одежда «СИЛА И ЧЕСТЬ». Первый дроп: выбирай вещь и размер, "
    "собирай заказ в витрине. Наличие, доставку и оплату подтвердим в чате."
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


def club_now() -> datetime:
    """Current moment for club/drop windows in the brand's main timezone."""
    try:
        tz = ZoneInfo("Europe/Moscow")
    except Exception:  # deployment without tzdata
        tz = timezone(timedelta(hours=3))
    return datetime.now(tz)


def club_today() -> str:
    """Club day boundary follows the brand's Europe/Moscow audience."""
    return club_now().date().isoformat()


def parse_drop_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=club_now().tzinfo)
    return moment


def _drop_moment_state(value: Any) -> tuple[datetime | None, bool]:
    """(moment, invalid): invalid=True when a non-empty value cannot be parsed."""
    text = str(value or "").strip()
    if not text:
        return None, False
    moment = parse_drop_moment(text)
    return moment, moment is None


def drop_gate_error(event: dict[str, Any] | None, early_access: bool, now: datetime | None = None) -> str:
    """Server-side drop window for web checkout; empty dates mean open."""
    config = event if isinstance(event, dict) else {}
    moment = now or club_now()
    ends, ends_bad = _drop_moment_state(config.get("ends_at"))
    starts, starts_bad = _drop_moment_state(config.get("starts_at"))
    early_starts, early_bad = _drop_moment_state(config.get("early_starts_at"))
    if ends_bad or starts_bad or early_bad:
        # A mistyped drop date must close sales, not open them to everyone.
        LOG.error("Дата дропа не разбирается (ends=%r starts=%r early=%r) — продажи закрыты",
                  config.get("ends_at"), config.get("starts_at"), config.get("early_starts_at"))
        return "Дроп временно недоступен. Напиши менеджеру."
    if ends and moment > ends:
        return "Дроп закрыт. Этот выпуск завершён."
    if starts and moment < starts:
        if early_access and early_starts and moment >= early_starts:
            return ""
        return "Дроп ещё не открыт. Ранний доступ — у участников клуба, закрывших все 7 дней."
    return ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
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


DEFAULT_CUSTOM_EMOJI_IDS: dict[str, str] = {
    "sword": "5397587797198548248",
    "seal": "5397773979735860878",
    "drop": "5397890321809975052",
    "orders": "5398051142565409387",
    "account": "5397592036331272320",
    "support": "5397723367841247818",
    "cart": "5397789480272833723",
    "ruler": "5398090325552048708",
    "pay": "5397616552004592288",
    "looks": "5397818424057447266",
    "channel": "5397772717015479661",
    "back": "5397775272521019200",
    "catalog": "5397587797198548248",
    "size": "5398090325552048708",
    "patch": "5398090325552048708",
    "tag": "5397592036331272320",
}


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
    gift_product_id: str = "tag-sila-i-chest"
    custom_emoji_pack: str = "sila_chest_merch"
    custom_emoji_ids: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CUSTOM_EMOJI_IDS))
    inline_emoji_enabled: bool = False

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
            brand_name=os.getenv("BRAND_NAME", "СИЛА И ЧЕСТЬ").strip() or "СИЛА И ЧЕСТЬ",
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
            gift_product_id=os.getenv("GIFT_PRODUCT_ID", "tag-sila-i-chest").strip(),
            custom_emoji_pack=os.getenv("CUSTOM_EMOJI_PACK", "sila_chest_merch").strip(),
            custom_emoji_ids=json.loads(os.getenv("CUSTOM_EMOJI_IDS", "{}")) if os.getenv("CUSTOM_EMOJI_IDS", "").strip().startswith("{") else dict(DEFAULT_CUSTOM_EMOJI_IDS),
            inline_emoji_enabled=os.getenv("INLINE_CUSTOM_EMOJI_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
        )


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.products_by_id: dict[str, dict[str, Any]] = {}
        # /reload и мастер /add подменяют каталог из потока опроса Telegram,
        # пока потоки HTTP отдают /api/catalog и проверяют заказы.
        self.lock = threading.RLock()
        self._snap_date = ""
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
            stock = product.get("stock_by_size")
            if stock is not None:
                if not isinstance(stock, dict):
                    raise ValueError(f"stock_by_size must be an object for {product_id}")
                known_sizes = {str(size) for size in product.get("sizes", [])}
                for size, count in stock.items():
                    if str(size) not in known_sizes:
                        raise ValueError(f"stock_by_size key {size!r} is not a size of {product_id}")
                    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100000:
                        raise ValueError(f"stock_by_size[{size!r}] must be an int 0..100000 for {product_id}")
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

    def save(self, data: dict[str, Any] | None = None) -> None:
        """Atomically persist *data* without changing the in-memory catalog.

        A candidate is serialized and fsynced before ``os.replace``.  The
        caller swaps ``self.data`` only after this method returns, so a failed
        write cannot leave memory and disk describing different assortments.
        Unique temporary names also keep concurrent processes from clobbering
        each other's partially written temp file.
        """
        with self.lock:
            source = self.data if data is None else data
            payload = json.dumps(source, ensure_ascii=False, indent=2) + "\n"
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            finally:
                tmp.unlink(missing_ok=True)
            # The rename itself is already durable enough for normal operation;
            # directory fsync adds crash consistency on filesystems that allow it.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                LOG.debug("Catalog directory fsync is unavailable", exc_info=True)

    def unique_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        index = 2
        while candidate in self.products_by_id:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _commit(self, candidate: dict[str, Any]) -> None:
        """Persist first, then atomically publish the same parsed candidate."""
        with self.lock:
            data, products = self.parse(candidate)
            self.save(data)
            self.data = data
            self.products_by_id = products
            self.maybe_snapshot()

    def maybe_snapshot(self) -> None:
        """Раз в сутки кладём копию каталога в backups и держим последние семь.

        Каталог — файловая база вещей: правка владельцем или сбой диска не
        должны стоить ассортимента. Снапшот снимается после успешной записи
        и никогда не мешает основной работе: любой сбой копии тихо логируется.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if self._snap_date == today:
            return
        with self.lock:
            if self._snap_date == today:
                return
            try:
                backup_dir = self.path.parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = backup_dir / f"catalog-{today}.json"
                if not target.exists():
                    shutil.copyfile(self.path, target)
                keep = sorted(backup_dir.glob("catalog-*.json"))
                for old in keep[:-7]:
                    old.unlink(missing_ok=True)
                # Set the watermark only after a usable snapshot exists.  A
                # transient disk error will therefore be retried on next save.
                self._snap_date = today
            except OSError:
                LOG.warning("Снапшот каталога не снялся", exc_info=True)

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

    def update_product(self, product_id: str, **changes: Any) -> bool:
        """Точечная правка вещи: валидация та же, что при публикации."""
        with self.lock:
            products = []
            found = False
            for product in self.data["products"]:
                item = dict(product)
                if str(item.get("id")) == product_id:
                    item.update(changes)
                    found = True
                products.append(item)
            if not found:
                return False
            self._commit({**self.data, "products": products})
            return True

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


class PaymentMismatch(ValueError):
    """Signed callback does not match the immutable order total/currency."""


class InvoiceMismatch(ValueError):
    """Provider invoice is not one of the invoices issued for this payment."""


class GiftUnavailable(ValueError):
    """The automatic gift was sold out between catalog read and checkout."""


class SizeUnavailable(ValueError):
    """A tracked size sold out between catalog read and checkout."""


def size_stock_totals(products: Iterable[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """{(product_id, size): total} for products that declare stock_by_size.

    Products without the field stay unlimited (current behaviour); declaring
    counts in the catalog turns on atomic per-size reservation for them.
    """
    totals: dict[tuple[str, str], int] = {}
    for product in products:
        raw = product.get("stock_by_size")
        if not isinstance(raw, dict):
            continue
        for size, count in raw.items():
            try:
                total = int(count)
            except (TypeError, ValueError):
                continue
            if total >= 0:
                totals[(str(product.get("id")), str(size))] = total
    return totals


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.local = threading.local()
        self.invoice_locks = [threading.Lock() for _ in range(64)]
        self._backup_lock = threading.Lock()
        self._backup_date = ""
        self._prune_date = ""
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

    def backup_database(self, retention: int = 7) -> Path | None:
        """Create one consistent online SQLite backup per UTC day.

        The watermark advances only after fsync + atomic rename.  A transient
        failure is logged and retried by the maintenance loop instead of
        silently disabling backups for the rest of the day.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if self._backup_date == today:
            return None
        with self._backup_lock:
            if self._backup_date == today:
                return None
            backup_dir = self.path.parent / "backups"
            target = backup_dir / f"bot-{today}.sqlite3"
            tmp = backup_dir / f".bot-{today}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    destination = sqlite3.connect(tmp)
                    try:
                        self.connection().backup(destination)
                    finally:
                        destination.close()
                    with open(tmp, "rb") as handle:
                        os.fsync(handle.fileno())
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, target)
                    # Persist the directory entry as well as the file contents.
                    directory_fd = os.open(backup_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                copies = sorted(backup_dir.glob("bot-*.sqlite3"))
                for old in copies[:-max(1, int(retention))]:
                    old.unlink(missing_ok=True)
                self._backup_date = today
                LOG.info("SQLite backup is ready: %s", target)
                return target
            except (OSError, sqlite3.Error):
                LOG.warning("SQLite backup failed; will retry", exc_info=True)
                return None
            finally:
                tmp.unlink(missing_ok=True)

    def prune_operational_data(self, now: datetime | None = None) -> dict[str, int] | None:
        """Apply bounded retention without deleting financial/audit history.

        High-volume browsing events and delivered notification envelopes are
        operational data, not order ledger.  Payment, consent, refund, admin,
        referral and order events deliberately stay untouched.  Undelivered
        notifications are never discarded: a stuck queue must be fixed/alerted,
        not silently lost.
        """
        moment = now or datetime.now(timezone.utc)
        today = moment.date().isoformat()
        if self._prune_date == today:
            return None
        cutoff_notifications = (moment - timedelta(days=30)).isoformat(timespec="seconds")
        cutoff_events = (moment - timedelta(days=90)).isoformat(timespec="seconds")
        routine_events = (
            "catalog_open", "category_open", "checkout_step", "interest_set",
            "lookbook_open", "product_open", "size_guide_open", "start",
            "support_open", "teaser_open", "webapp_profile", "webapp_waitlist",
            "waitlist_remove_miss",
        )
        placeholders = ",".join("?" for _ in routine_events)
        try:
            conn = self.connection()
            delivered = conn.execute(
                "DELETE FROM notifications WHERE delivered_at IS NOT NULL AND delivered_at<?",
                (cutoff_notifications,),
            ).rowcount
            methods = conn.execute(
                "DELETE FROM payment_methods WHERE valid_until<?",
                (moment.timestamp() - 86400,),
            ).rowcount
            events = conn.execute(
                f"DELETE FROM events WHERE created_at<? AND event IN ({placeholders})",
                (cutoff_events, *routine_events),
            ).rowcount
            self._prune_date = today
            result = {"notifications": delivered, "payment_methods": methods, "events": events}
            if any(result.values()):
                LOG.info("Operational retention applied: %s", result)
            return result
        except sqlite3.Error:
            LOG.warning("Operational retention failed; will retry", exc_info=True)
            return None

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
            CREATE TABLE IF NOT EXISTS club_members (
                user_id INTEGER PRIMARY KEY,
                started_on TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS club_checkins (
                user_id INTEGER NOT NULL,
                day_date TEXT NOT NULL,
                task_index INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, day_date),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
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
            CREATE TABLE IF NOT EXISTS payment_invoices (
                provider TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                payment_id TEXT NOT NULL,
                amount_rub INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                created_at TEXT NOT NULL,
                PRIMARY KEY(provider, invoice_id)
            );
            CREATE TABLE IF NOT EXISTS provider_transactions (
                provider TEXT NOT NULL,
                provider_tx_id TEXT NOT NULL,
                payment_id TEXT NOT NULL,
                amount_rub INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(provider, provider_tx_id)
            );
            CREATE TABLE IF NOT EXISTS inventory (
                product_id TEXT PRIMARY KEY,
                total INTEGER NOT NULL CHECK(total >= 0),
                available INTEGER NOT NULL CHECK(available >= 0 AND available <= total),
                updated_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS size_stock (
                product_id TEXT NOT NULL,
                size TEXT NOT NULL,
                total INTEGER NOT NULL CHECK(total >= 0),
                available INTEGER NOT NULL CHECK(available >= 0 AND available <= total),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (product_id, size)
            );
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                deliver TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                items_json TEXT NOT NULL DEFAULT '[]',
                total_rub INTEGER NOT NULL DEFAULT 0,
                gift_wrap TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                order_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS community_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                product_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
            CREATE INDEX IF NOT EXISTS idx_users_interest ON users(interest);
            CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payments(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_invoices_payment ON payment_invoices(payment_id, provider);
            CREATE INDEX IF NOT EXISTS idx_transactions_payment ON provider_transactions(payment_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
            """
        )
        club_columns = {row[1] for row in conn.execute("PRAGMA table_info(club_members)")}
        if "reminder_hour" not in club_columns:
            conn.execute("ALTER TABLE club_members ADD COLUMN reminder_hour INTEGER")
        if "last_reminded_on" not in club_columns:
            conn.execute("ALTER TABLE club_members ADD COLUMN last_reminded_on TEXT NOT NULL DEFAULT ''")
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
        for name in ("customer_name", "city", "address", "entrance", "deliver"):
            if name not in order_columns:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id)")
        payment_columns = {row[1] for row in conn.execute("PRAGMA table_info(payments)")}
        for name, definition in {
            "gift_product_id": "TEXT NOT NULL DEFAULT ''",
            "gift_name": "TEXT NOT NULL DEFAULT ''",
            "gift_qty": "INTEGER NOT NULL DEFAULT 0",
            "gift_serial": "INTEGER",
            "gift_released_at": "TEXT",
            "refunded_at": "TEXT",
            "refund_actor": "INTEGER",
            "provider_refund_id": "TEXT NOT NULL DEFAULT ''",
            "refund_amount_rub": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in payment_columns:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_active_gift_serial "
            "ON payments(gift_product_id, gift_serial) "
            "WHERE gift_serial IS NOT NULL AND gift_released_at IS NULL"
        )
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
        allowed_status = {"new", "awaiting_payment", "paid", "confirmed", "shipped", "received", "completed", "cancelled"}
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
            profile = conn.execute(
                "SELECT first_name, city, address, entrance, deliver FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO orders(
                    request_id, user_id, product_id, product_name, size, phone,
                    quantity, note, status, payment_id, amount_rub, created_at,
                    customer_name, city, address, entrance, deliver
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(profile["first_name"] or "") if profile else "",
                    str(profile["city"] or "") if profile else "",
                    str(profile["address"] or "") if profile else "",
                    str(profile["entrance"] or "") if profile else "",
                    str(profile["deliver"] or "") if profile else "",
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

    def create_payment(
        self, payment_id: str, user_id: int, amount_rub: int, amount_stars: int = 0,
        *, gift: dict[str, Any] | None = None,
    ) -> None:
        gift = gift or {}
        self.connection().execute(
            """
            INSERT INTO payments(
                payment_id, user_id, amount_rub, amount_stars, status, created_at,
                gift_product_id, gift_name, gift_qty, gift_serial
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                payment_id, user_id, max(0, int(amount_rub)), max(0, int(amount_stars)), utc_now(),
                str(gift.get("product_id") or ""), str(gift.get("name") or ""),
                1 if gift.get("product_id") else 0,
                int(gift["serial"]) if gift.get("serial") is not None else None,
            ),
        )

    def _next_gift_serial(self, product_id: str, total: int) -> int | None:
        """Smallest free number in the limited run; caller holds the write lock."""
        used = {
            int(row[0]) for row in self.connection().execute(
                "SELECT gift_serial FROM payments WHERE gift_product_id=? "
                "AND gift_serial IS NOT NULL AND gift_released_at IS NULL",
                (product_id,),
            )
        }
        return next((serial for serial in range(1, total + 1) if serial not in used), None)

    def _backfill_gift_serials(self, product_id: str, total: int) -> None:
        """Assign real run numbers to pre-migration reservations exactly once."""
        conn = self.connection()
        owns_transaction = not conn.in_transaction
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                "SELECT payment_id FROM payments WHERE gift_product_id=? AND gift_qty>0 "
                "AND gift_serial IS NULL AND gift_released_at IS NULL ORDER BY created_at, payment_id",
                (product_id,),
            ))
            for row in rows:
                serial = self._next_gift_serial(product_id, total)
                if serial is None:
                    break
                conn.execute(
                    "UPDATE payments SET gift_serial=? WHERE payment_id=? AND gift_serial IS NULL",
                    (serial, row["payment_id"]),
                )
            if owns_transaction:
                conn.execute("COMMIT")
        except Exception:
            if owns_transaction and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def gift_for_user(self, user_id: int) -> sqlite3.Row | None:
        return self.connection().execute(
            "SELECT gift_product_id, gift_name, gift_serial, created_at FROM payments WHERE user_id=? "
            "AND gift_product_id!='' AND gift_qty>0 AND gift_released_at IS NULL "
            "ORDER BY created_at DESC, payment_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def gift_status(self, product: dict[str, Any] | None) -> dict[str, Any] | None:
        """Public automatic-gift descriptor with the atomically tracked balance."""
        if not product or not product.get("gift"):
            return None
        try:
            total = max(0, int(product.get("gift_stock", 0)))
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        product_id = str(product["id"])
        self._backfill_gift_serials(product_id, total)
        row = self.connection().execute(
            "SELECT total, available FROM inventory WHERE product_id=?", (product_id,)
        ).fetchone()
        available = int(row["available"]) if row else total
        return {
            "product_id": product_id,
            "name": str(product["name"]),
            "image": str(product.get("image") or ""),
            "stock": total,
            "remaining": max(0, min(available, total)),
            "available": available > 0,
        }

    def _reserve_gift(self, gift: dict[str, Any] | None) -> dict[str, Any] | None:
        if not gift:
            return None
        product_id = str(gift.get("product_id") or "")
        total = int(gift.get("stock") or 0)
        if not product_id or total <= 0:
            return None
        now = utc_now()
        conn = self.connection()
        conn.execute(
            "INSERT OR IGNORE INTO inventory(product_id, total, available, updated_at) VALUES (?, ?, ?, ?)",
            (product_id, total, total, now),
        )
        cursor = conn.execute(
            "UPDATE inventory SET available=available-1, updated_at=? "
            "WHERE product_id=? AND available>0",
            (now, product_id),
        )
        if cursor.rowcount != 1:
            raise GiftUnavailable("Подарки первого выпуска только что закончились. Обнови витрину — основную вещь всё ещё можно купить.")
        self._backfill_gift_serials(product_id, total)
        serial = self._next_gift_serial(product_id, total)
        if serial is None:
            raise GiftUnavailable("Свободные номера подарков закончились. Обнови витрину или напиши менеджеру.")
        return {
            "product_id": product_id, "name": str(gift.get("name") or "Подарок"),
            "stock": total, "serial": serial, "serial_label": f"{serial:05d}",
        }

    def _release_gift(self, payment_id: str) -> bool:
        """Return a reserved gift once; safe to call from any cancellation path."""
        conn = self.connection()
        row = conn.execute(
            "SELECT gift_product_id, gift_qty, gift_released_at FROM payments WHERE payment_id=?",
            (payment_id,),
        ).fetchone()
        if not row or not row["gift_product_id"] or int(row["gift_qty"] or 0) <= 0 or row["gift_released_at"]:
            return False
        now = utc_now()
        conn.execute(
            "UPDATE inventory SET available=MIN(total, available+?), updated_at=? WHERE product_id=?",
            (int(row["gift_qty"]), now, row["gift_product_id"]),
        )
        conn.execute(
            "UPDATE payments SET gift_released_at=? WHERE payment_id=? AND gift_released_at IS NULL",
            (now, payment_id),
        )
        self.event(None, "gift_released", {"payment_id": payment_id, "product_id": row["gift_product_id"]})
        return True

    # -- Per-size stock (opt-in via catalog stock_by_size) --------------------

    def _sync_size_stock_rows(self, totals: dict[tuple[str, str], int]) -> None:
        """Seed rows and follow catalog total changes (restock / correction)."""
        conn = self.connection()
        now = utc_now()
        for (product_id, size), total in totals.items():
            conn.execute(
                "INSERT OR IGNORE INTO size_stock(product_id, size, total, available, updated_at) VALUES (?, ?, ?, ?, ?)",
                (product_id, size, total, total, now),
            )
            row = conn.execute(
                "SELECT total, available FROM size_stock WHERE product_id=? AND size=?",
                (product_id, size),
            ).fetchone()
            if row and int(row["total"]) != total:
                available = max(0, min(total, int(row["available"]) + (total - int(row["total"]))))
                conn.execute(
                    "UPDATE size_stock SET total=?, available=?, updated_at=? WHERE product_id=? AND size=?",
                    (total, available, now, product_id, size),
                )

    def sync_size_stock(self, products: list[dict[str, Any]]) -> None:
        totals = size_stock_totals(products)
        if not totals:
            return
        conn = self.connection()
        owns = not conn.in_transaction
        try:
            if owns:
                conn.execute("BEGIN IMMEDIATE")
            self._sync_size_stock_rows(totals)
            if owns:
                conn.execute("COMMIT")
        except Exception:
            if owns and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def size_stock_snapshot(self, products: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
        """Remaining counts for tracked sizes; seeds rows on first read."""
        totals = size_stock_totals(products)
        if not totals:
            return {}
        self.sync_size_stock(products)
        conn = self.connection()
        snapshot: dict[tuple[str, str], int] = {}
        for row in conn.execute("SELECT product_id, size, available FROM size_stock"):
            key = (str(row["product_id"]), str(row["size"]))
            if key in totals:
                snapshot[key] = max(0, int(row["available"]))
        return snapshot

    def _reserve_size_stock(self, lines: list[dict[str, Any]], totals: dict[tuple[str, str], int]) -> None:
        """Atomically take tracked sizes inside the checkout transaction."""
        if not totals:
            return
        wanted: dict[tuple[str, str], int] = {}
        names: dict[tuple[str, str], str] = {}
        for line in lines:
            key = (str(line["product_id"]), str(line["size"]))
            if key in totals:
                wanted[key] = wanted.get(key, 0) + max(1, int(line["quantity"]))
                names[key] = str(line.get("name") or line["product_id"])
        if not wanted:
            return
        self._sync_size_stock_rows(totals)
        conn = self.connection()
        now = utc_now()
        for (product_id, size), quantity in wanted.items():
            changed = conn.execute(
                "UPDATE size_stock SET available=available-?, updated_at=? "
                "WHERE product_id=? AND size=? AND available>=?",
                (quantity, now, product_id, size, quantity),
            )
            if changed.rowcount != 1:
                raise SizeUnavailable(
                    f"Размер {size} вещи «{names[(product_id, size)]}» только что закончился. Выбери другой размер."
                )

    def _release_size_stock_lines(self, lines: list[dict[str, Any]]) -> None:
        """Return quantities for cancelled order lines (tracked sizes only)."""
        conn = self.connection()
        now = utc_now()
        for line in lines:
            conn.execute(
                "UPDATE size_stock SET available=MIN(total, available+?), updated_at=? "
                "WHERE product_id=? AND size=?",
                (max(1, int(line["quantity"])), now, str(line["product_id"]), str(line["size"])),
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
        gift: dict[str, Any] | None = None,
        stock_totals: dict[tuple[str, str], int] | None = None,
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
            profile = conn.execute(
                "SELECT first_name, city, address, entrance, deliver FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            customer = {
                key: str(profile[key] or "") if profile else ""
                for key in ("first_name", "city", "address", "entrance", "deliver")
            }
            # Reservation, payment, order lines, receipt and outbox share one
            # SQLite transaction: a failed checkout cannot consume a gift.
            self._reserve_size_stock(lines, stock_totals or {})
            reserved_gift = self._reserve_gift(gift)
            self.create_payment(payment_id, user_id, total, amount_stars, gift=reserved_gift)
            order_ids = []
            for index, line in enumerate(lines):
                cursor = conn.execute(
                    """INSERT INTO orders(request_id, user_id, product_id, product_name, size,
                       phone, quantity, note, status, payment_id, amount_rub, created_at,
                       customer_name, city, address, entrance, deliver)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"checkout:{payment_id}:{index}", user_id, line["product_id"], line["name"],
                     line["size"], line["phone"], line["quantity"], line["note"][:400],
                     payment_id, line["amount_rub"], now, customer["first_name"],
                     customer["city"], customer["address"], customer["entrance"], customer["deliver"]),
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
                "gift": ({
                    "product_id": reserved_gift["product_id"], "name": reserved_gift["name"],
                    "quantity": 1, "serial": reserved_gift["serial"],
                    "serial_label": reserved_gift["serial_label"],
                } if reserved_gift else None),
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

    def expire_payments(self, ttl_seconds: int = 3600) -> int:
        """Expire stale pending invoices and return their gift reservations."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(60, int(ttl_seconds)))).isoformat()
        conn = self.connection()
        expired = 0
        owns_transaction = not conn.in_transaction
        try:
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                "SELECT payment_id, user_id FROM payments WHERE status='pending' AND created_at<?",
                (cutoff,),
            ))
            for row in rows:
                invalid = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE payment_id=? AND status!='awaiting_payment'",
                    (row["payment_id"],),
                ).fetchone()[0]
                if invalid:
                    continue
                now = utc_now()
                changed = conn.execute(
                    "UPDATE payments SET status='expired' WHERE payment_id=? AND status='pending'",
                    (row["payment_id"],),
                )
                if changed.rowcount != 1:
                    continue
                stock_lines = [
                    {"product_id": item["product_id"], "size": item["size"], "quantity": item["quantity"]}
                    for item in conn.execute(
                        "SELECT product_id, size, quantity FROM orders WHERE payment_id=? AND status='awaiting_payment'",
                        (row["payment_id"],),
                    )
                ]
                conn.execute(
                    "UPDATE orders SET status='cancelled' WHERE payment_id=? AND status='awaiting_payment'",
                    (row["payment_id"],),
                )
                self._release_size_stock_lines(stock_lines)
                conn.execute("DELETE FROM payment_methods WHERE payment_id=?", (row["payment_id"],))
                self._release_gift(str(row["payment_id"]))
                self.event(int(row["user_id"]), "payment_expired", {"payment_id": row["payment_id"]})
                expired += 1
            if owns_transaction:
                conn.execute("COMMIT")
            return expired
        except Exception:
            if owns_transaction and conn.in_transaction:
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
        self.expire_payments()
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

    def register_invoice(self, payment_id: str, provider: str, invoice: dict[str, str], amount_rub: int) -> None:
        """Bind every issued provider invoice to the immutable local payment."""
        invoice_id = str(invoice.get("id") or "").strip()
        if not invoice_id:
            raise RuntimeError(f"{provider} returned an invoice without an id")
        payment = self.get_payment(payment_id)
        if not payment or int(payment["amount_rub"]) != int(amount_rub):
            raise PaymentMismatch("Счёт не совпадает с сохранённой покупкой.")
        self.connection().execute(
            "INSERT INTO payment_invoices(provider, invoice_id, payment_id, amount_rub, currency, created_at) "
            "VALUES (?, ?, ?, ?, 'RUB', ?)",
            (provider[:32], invoice_id[:80], payment_id, int(amount_rub), utc_now()),
        )

    def record_provider_payment(
        self, payment_id: str, provider: str, provider_tx_id: str,
        amount_rub: int, currency: str, payload_hash: str, *,
        queue_notifications: Callable[[str], None] | None = None,
        queue_overpayment: Callable[[str, str], None] | None = None,
    ) -> str:
        """Record one signed provider transaction and settle it at most once.

        Unlike a single mutable ``payments.provider_id``, this ledger survives
        two different invoices paid for the same checkout.  A replay is a
        duplicate; a new transaction after settlement is an overpayment and
        never advances fulfilment for a second time.
        """
        if not provider_tx_id:
            raise PaymentMismatch("В подтверждении оплаты нет номера счёта.")
        if currency.upper() != "RUB" or amount_rub <= 0:
            raise PaymentMismatch("Валюта или сумма оплаты не поддерживается.")
        conn = self.connection()
        now = utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM provider_transactions WHERE provider=? AND provider_tx_id=?",
                (provider, provider_tx_id),
            ).fetchone()
            if existing:
                if (str(existing["payment_id"]) != payment_id
                        or int(existing["amount_rub"]) != amount_rub
                        or str(existing["currency"]) != currency.upper()):
                    raise PaymentMismatch("Номер транзакции уже привязан к другой оплате.")
                conn.execute("COMMIT")
                return "duplicate"
            payment = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            if not payment:
                conn.execute("COMMIT")
                return "unknown"
            expected_invoices = {
                str(row[0]) for row in conn.execute(
                    "SELECT invoice_id FROM payment_invoices WHERE payment_id=? AND provider=?",
                    (payment_id, provider),
                )
            }
            if expected_invoices and provider_tx_id not in expected_invoices:
                raise InvoiceMismatch("Провайдер подтвердил счёт, который не выдавался для этой покупки.")
            if int(payment["amount_rub"]) != amount_rub:
                raise PaymentMismatch("Сумма подтверждения не совпадает с покупкой.")
            if str(payment["status"]) in {"paid", "refund_required", "refunded"}:
                conn.execute(
                    "INSERT INTO provider_transactions VALUES (?, ?, ?, ?, ?, ?, 'overpayment', ?)",
                    (provider, provider_tx_id, payment_id, amount_rub, currency.upper(), payload_hash, now),
                )
                self.event(
                    int(payment["user_id"]), "payment_overpayment",
                    {"payment_id": payment_id, "provider": provider, "provider_tx_id": provider_tx_id,
                     "amount_rub": amount_rub, "currency": currency.upper()},
                )
                if queue_overpayment:
                    queue_overpayment(payment_id, provider_tx_id)
                conn.execute("COMMIT")
                return "overpayment"
            conn.execute(
                "INSERT INTO provider_transactions VALUES (?, ?, ?, ?, ?, ?, 'processing', ?)",
                (provider, provider_tx_id, payment_id, amount_rub, currency.upper(), payload_hash, now),
            )
            changed = self.mark_payment_paid(
                payment_id, provider, provider_tx_id, queue_notifications=queue_notifications,
            )
            status = str(self.get_payment(payment_id)["status"])
            conn.execute(
                "UPDATE provider_transactions SET status=? WHERE provider=? AND provider_tx_id=?",
                (status, provider, provider_tx_id),
            )
            conn.execute("COMMIT")
            return status if changed else "duplicate"
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def provider_transactions(self, payment_id: str) -> list[sqlite3.Row]:
        return list(self.connection().execute(
            "SELECT * FROM provider_transactions WHERE payment_id=? ORDER BY created_at, provider_tx_id",
            (payment_id,),
        ))

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
                    SET status=?, method=?, provider_id=?, paid_at=?,
                        refund_amount_rub=CASE WHEN ?='refund_required' THEN amount_rub ELSE refund_amount_rub END
                    WHERE payment_id=?
                    """,
                    (target_status, str(method or row["method"] or "")[:32], str(provider_id or "")[:80], now,
                     target_status, payment_id),
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

    def club_state(self, user_id: int) -> dict[str, Any]:
        """Club progress for one member: join date plus one check-in per day."""
        conn = self.connection()
        member = conn.execute(
            "SELECT user_id, started_on, created_at, reminder_hour FROM club_members WHERE user_id=?",
            (user_id,),
        ).fetchone()
        checkins = [
            {"date": str(row[0]), "task_index": int(row[1])}
            for row in conn.execute(
                "SELECT day_date, task_index FROM club_checkins WHERE user_id=? ORDER BY day_date",
                (user_id,),
            )
        ]
        active_members = int(conn.execute("SELECT COUNT(*) FROM club_members").fetchone()[0] or 0)
        total = len(checkins)
        reminder_hour = int(member[3]) if member and member[3] is not None else None
        return {
            "joined": member is not None,
            "started_on": str(member[1]) if member else "",
            "checkins": checkins,
            "total": total,
            "completed": total >= 7,
            "early_access": total >= 7,
            "reminder_hour": reminder_hour,
            "active_members": active_members,
            # The mini app must judge "today" by the server day (Europe/Moscow),
            # not the device timezone, or streaks drift around midnight.
            "today": club_now().date().isoformat(),
        }

    def club_members_count(self) -> int:
        return int(self.connection().execute("SELECT COUNT(*) FROM club_members").fetchone()[0] or 0)

    def club_set_reminder(self, user_id: int, hour: int | None) -> dict[str, Any]:
        safe_hour = int(hour) if isinstance(hour, int) and 0 <= int(hour) <= 23 else None
        self.club_join(user_id)
        self.connection().execute(
            "UPDATE club_members SET reminder_hour=?, updated_at=? WHERE user_id=?",
            (safe_hour, utc_now(), user_id),
        )
        return self.club_state(user_id)

    def due_club_reminders(self, today: str, hour: int) -> list[int]:
        conn = self.connection()
        rows = conn.execute(
            """
            SELECT m.user_id
            FROM club_members m
            WHERE m.reminder_hour = ?
              AND COALESCE(m.last_reminded_on, '') != ?
              AND (SELECT COUNT(*) FROM club_checkins c WHERE c.user_id = m.user_id) < 7
              AND NOT EXISTS (
                  SELECT 1 FROM club_checkins c2 WHERE c2.user_id = m.user_id AND c2.day_date = ?
              )
            LIMIT 50
            """,
            (hour, today, today),
        ).fetchall()
        user_ids = [int(r[0]) for r in rows]
        for uid in user_ids:
            conn.execute(
                "UPDATE club_members SET last_reminded_on=? WHERE user_id=?",
                (today, uid),
            )
        return user_ids

    def verify_serial(self, raw_code: str) -> dict[str, Any]:
        cleaned = re.sub(r"[^0-9]", "", str(raw_code or ""))
        if not cleaned or len(cleaned) > 8:
            return {"ok": False, "error": "Введи номер жетона или заказа (например, 00001 или 47)."}
        num = int(cleaned)
        if num <= 0:
            return {"ok": False, "error": "Номер должен быть больше нуля."}
        if num == 1:
            return {
                "ok": True,
                "valid": True,
                "serial_label": "00001",
                "edition": "ВЫПУСК 001 · ЭТАЛОН",
                "model_name": "СИЛА И ЧЕСТЬ · ЭКЗЕМПЛЯР ОСНОВАТЕЛЯ",
                "status_label": "Подлинник № 00001 · закреплён за основателем бренда",
                "issued_date": "2026",
            }
        conn = self.connection()
        gift_row = conn.execute(
            """
            SELECT p.payment_id, p.created_at, o.product_name
            FROM payments p
            LEFT JOIN orders o ON o.payment_id = p.payment_id
            WHERE p.gift_serial = ? AND p.gift_released_at IS NULL
            ORDER BY p.created_at DESC LIMIT 1
            """,
            (num,),
        ).fetchone()
        if gift_row:
            return {
                "ok": True,
                "valid": True,
                "serial_label": f"{num:05d}",
                "edition": "ВЫПУСК 001 · ПЕРВАЯ СОТНЯ",
                "model_name": str(gift_row["product_name"] or "СИЛА И ЧЕСТЬ"),
                "status_label": "Подлинник зарегистрирован в реестре выпуска",
                "issued_date": str(gift_row["created_at"] or "")[:10],
            }
        order_row = conn.execute(
            """
            SELECT id, product_name, status, created_at
            FROM orders
            WHERE id = ? AND status != 'cancelled'
            LIMIT 1
            """,
            (num,),
        ).fetchone()
        if order_row:
            return {
                "ok": True,
                "valid": True,
                "serial_label": f"{num:05d}",
                "edition": "ВЫПУСК 001",
                "model_name": str(order_row["product_name"] or "СИЛА И ЧЕСТЬ"),
                "status_label": f"Заказ №{int(order_row['id'])} подтверждён в реестре магазина",
                "issued_date": str(order_row["created_at"] or "")[:10],
            }
        if 1 <= num <= 100:
            return {
                "ok": True,
                "valid": False,
                "available": True,
                "serial_label": f"{num:05d}",
                "message": f"Номер № {num:05d} из первой сотни ещё не выдан — закрепляется при подтверждении заказа.",
            }
        return {
            "ok": True,
            "valid": False,
            "available": False,
            "serial_label": f"{num:05d}",
            "message": "Номер не числится в реестре первого выпуска.",
        }

    def create_lead(
        self,
        user: dict[str, Any] | None,
        customer: dict[str, Any],
        lines: list[dict[str, Any]],
        total_rub: int,
        gift_wrap: str = "",
    ) -> int:
        uid = int(user["id"]) if user and user.get("id") else None
        uname = str((user or {}).get("username") or "")[:64]
        fname = str(customer.get("name") or (user or {}).get("first_name") or "")[:80]
        cur = self.connection().execute(
            """
            INSERT INTO leads(
                user_id, username, first_name, phone, city, deliver, address,
                items_json, total_rub, gift_wrap, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                uid,
                uname,
                fname,
                str(customer.get("phone") or "")[:40],
                str(customer.get("city") or "")[:80],
                str(customer.get("deliver") or "")[:40],
                str(customer.get("address") or "")[:200],
                compact_json(lines[:20]),
                max(0, int(total_rub or 0)),
                str(gift_wrap or "")[:160],
                utc_now(),
            ),
        )
        return int(cur.lastrowid)

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.connection().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def mark_lead_confirmed(self, lead_id: int, order_id: int) -> None:
        self.connection().execute(
            "UPDATE leads SET status='confirmed', order_id=? WHERE id=?",
            (order_id, lead_id),
        )

    def create_community_submission(
        self, user: dict[str, Any], file_id: str, caption: str
    ) -> int:
        cur = self.connection().execute(
            """
            INSERT INTO community_submissions(user_id, username, first_name, file_id, caption, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                int(user["id"]),
                str(user.get("username") or "")[:64],
                str(user.get("first_name") or "")[:80],
                str(file_id)[:256],
                str(caption or "")[:400],
                utc_now(),
            ),
        )
        return int(cur.lastrowid)

    def get_community_submission(self, sub_id: int) -> sqlite3.Row | None:
        return self.connection().execute(
            "SELECT * FROM community_submissions WHERE id=?", (sub_id,)
        ).fetchone()

    def set_community_submission_status(self, sub_id: int, status: str, product_id: str = "") -> None:
        self.connection().execute(
            "UPDATE community_submissions SET status=?, product_id=? WHERE id=?",
            (status, product_id, sub_id),
        )

    def miniapp_funnel_stats(self) -> dict[str, Any]:
        conn = self.connection()
        counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT event, COUNT(*) FROM events
            WHERE event LIKE 'miniapp_%'
            GROUP BY event
            """
        ):
            counts[str(row[0])] = int(row[1])
        size_counts: dict[str, int] = {}
        for (raw_payload,) in conn.execute(
            "SELECT payload FROM events WHERE event='miniapp_size_select' ORDER BY id DESC LIMIT 500"
        ):
            try:
                sz = str(json.loads(raw_payload or "{}").get("size") or "").strip().upper()
                if sz:
                    size_counts[sz] = size_counts.get(sz, 0) + 1
            except Exception:
                pass
        top_sizes = sorted(size_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        leads_total = int(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] or 0)
        leads_confirmed = int(conn.execute("SELECT COUNT(*) FROM leads WHERE status='confirmed'").fetchone()[0] or 0)
        return {
            "opens": counts.get("miniapp_open", 0),
            "product_views": counts.get("miniapp_product_view", 0),
            "compares": counts.get("miniapp_compare_open", 0),
            "bundles": counts.get("miniapp_bundle_add", 0),
            "stories": counts.get("miniapp_story_share", 0),
            "dm_clicks": counts.get("miniapp_manager_click", 0),
            "leads_total": leads_total,
            "leads_confirmed": leads_confirmed,
            "top_sizes": top_sizes,
            "club_members": self.club_members_count(),
        }

    def club_join(self, user_id: int, today: str | None = None) -> dict[str, Any]:
        day = today or club_today()
        now = utc_now()
        self.connection().execute(
            """
            INSERT INTO club_members(user_id, started_on, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (user_id, day, now, now),
        )
        return self.club_state(user_id)

    def club_checkin(self, user_id: int, today: str | None = None) -> dict[str, Any]:
        """Record today's task. Idempotent per day; auto-joins on first check-in."""
        day = today or club_today()
        state = self.club_join(user_id, day)
        if state["completed"] or any(row["date"] == day for row in state["checkins"]):
            return state
        self.connection().execute(
            """
            INSERT OR IGNORE INTO club_checkins(user_id, day_date, task_index, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, day, min(int(state["total"]), 6), utc_now()),
        )
        self.connection().execute(
            "UPDATE club_members SET updated_at=? WHERE user_id=?", (utc_now(), user_id)
        )
        return self.club_state(user_id)

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

    def refunds_open(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.payment_id, p.amount_rub, p.refund_amount_rub, p.created_at, o.id AS order_id, "
                "o.product_name, o.size, o.user_id FROM payments p "
                "JOIN orders o ON o.payment_id = p.payment_id "
                "WHERE p.status='refund_required' ORDER BY p.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def refunds_done(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.payment_id, p.amount_rub, p.refund_amount_rub, p.paid_at, o.id AS order_id, "
                "o.product_name, o.user_id FROM payments p "
                "JOIN orders o ON o.payment_id = p.payment_id "
                "WHERE p.status='refunded' ORDER BY p.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def payments_pending(self, limit: int = 8) -> list[dict[str, Any]]:
        """Оплаты в статусе pending: покупка, сумма, кто и когда начал."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.payment_id, p.amount_rub, p.created_at, MIN(o.id) AS order_id, "
                "COUNT(o.id) AS lines, GROUP_CONCAT(o.product_name, ', ') AS names, "
                "o.user_id FROM payments p "
                "JOIN orders o ON o.payment_id = p.payment_id "
                "WHERE p.status='pending' AND o.status='awaiting_payment' "
                "GROUP BY p.payment_id ORDER BY p.created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_payment_refunded(
        self, payment_id: str, actor_id: int | None = None, provider_refund_id: str = "",
    ) -> bool:
        """Complete only an actually required refund, exactly once."""
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE payments SET status='refunded', refunded_at=?, refund_actor=?, provider_refund_id=? "
                "WHERE payment_id=? AND status='refund_required'",
                (utc_now(), actor_id, str(provider_refund_id or "")[:80], payment_id),
            )
            if cursor.rowcount != 1:
                conn.execute("COMMIT")
                return False
            payment = self.get_payment(payment_id)
            if payment:
                self.event(
                    int(payment["user_id"]), "payment_refunded",
                    {"payment_id": payment_id, "actor_id": actor_id, "provider_refund_id": provider_refund_id},
                )
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE payment_id=? AND status!='cancelled'", (payment_id,)
                ).fetchone()[0]
                if not remaining:
                    self._release_gift(payment_id)
            conn.execute("COMMIT")
            return True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

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
        allowed = {"new", "awaiting_payment", "paid", "confirmed", "shipped", "received", "completed", "cancelled"}
        transitions = {
            "new": {"confirmed", "cancelled", "awaiting_payment"},
            "awaiting_payment": {"paid", "cancelled"},
            "paid": {"confirmed", "cancelled"},
            "confirmed": {"shipped", "completed", "cancelled"},
            "shipped": {"received"},
            "received": set(),
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
                        # One payment is one fulfilment/refund unit.  Cancelling
                        # a single line of a paid cart must not create an
                        # ambiguous partial refund while sibling lines stay paid.
                        siblings = list(conn.execute(
                            "SELECT id, status FROM orders WHERE payment_id=?", (row["payment_id"],)
                        ))
                        if any(item["status"] in {"shipped", "received", "completed", "cancelled"} for item in siblings):
                            raise ValueError("Общая оплата уже частично закрыта. Для частичного возврата нужна ручная сверка с менеджером.")
                        for item in siblings:
                            if item["id"] != order_id:
                                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (item["id"],))
                                self.event(row["user_id"], "order_status_changed",
                                           {"order_id": item["id"], "status": "cancelled"})
                        conn.execute(
                            "UPDATE payments SET status='refund_required', refund_amount_rub=amount_rub WHERE payment_id=?",
                            (row["payment_id"],),
                        )
                        self.event(row["user_id"], "payment_refund_required",
                                   {"payment_id": row["payment_id"], "amount_rub": int(payment["amount_rub"]), "scope": "full"})
                        if queue_paid:
                            queue_paid(row["payment_id"])
                    conn.execute("DELETE FROM payment_methods WHERE payment_id=?", (row["payment_id"],))
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
            if status == "cancelled":
                # Return tracked sizes: this order plus any siblings the
                # payment branches above cancelled in the same transaction.
                # The current==status early return keeps this exactly-once.
                if row["payment_id"]:
                    released = [
                        {"product_id": item["product_id"], "size": item["size"], "quantity": item["quantity"]}
                        for item in conn.execute(
                            "SELECT product_id, size, quantity FROM orders WHERE payment_id=? AND status='cancelled'",
                            (row["payment_id"],),
                        )
                    ]
                else:
                    released = [{"product_id": row["product_id"], "size": row["size"], "quantity": row["quantity"]}]
                self._release_size_stock_lines(released)
            conn.execute(
                "INSERT INTO events(user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                (row["user_id"], "order_status_changed", compact_json({"order_id": order_id, "status": status}), utc_now()),
            )
            if status == "cancelled" and row["payment_id"]:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE payment_id=? AND status!='cancelled'",
                    (row["payment_id"],),
                ).fetchone()[0]
                if not remaining:
                    self._release_gift(str(row["payment_id"]))
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
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def download_file(self, file_id: str, dest: Path, max_bytes: int = 12 * 1024 * 1024) -> bool:
        info = self.call("getFile", {"file_id": file_id}, timeout=25)
        file_path = str((info or {}).get("file_path") or "").lstrip("/")
        if not file_path or ".." in file_path:
            return False
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with urllib.request.urlopen(url, timeout=30) as resp, tmp.open("wb") as out:
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    tmp.unlink(missing_ok=True)
                    return False
                out.write(chunk)
        tmp.replace(dest)
        return True

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
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {details}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result.get("result")

    def send_animation_file(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        """Upload a local GIF/MP4 as sendAnimation."""
        caption = fit_html_text(caption, CAPTION_LIMIT - 24)
        content = path.read_bytes()
        filename = path.name or "animation.mp4"
        mime = mimetypes.guess_type(filename)[0] or "video/mp4"
        boundary = "----VorozhbitovAnim7MA4YWxk"
        fields: dict[str, str] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))
        crlf = b"\r\n"
        b_boundary = boundary.encode("ascii")
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    b"--" + b_boundary + crlf,
                    f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf + crlf,
                    value.encode("utf-8"),
                    crlf,
                ]
            )
        chunks.extend(
            [
                b"--" + b_boundary + crlf,
                f'Content-Disposition: form-data; name="animation"; filename="{filename}"'.encode("utf-8") + crlf,
                f"Content-Type: {mime}".encode("utf-8") + crlf + crlf,
                content,
                crlf,
                b"--" + b_boundary + b"--" + crlf,
            ]
        )
        request = urllib.request.Request(
            self.base_url + "sendAnimation",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {details}") from exc
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


def inline_keyboard(rows: Iterable[Iterable[Any]]) -> dict[str, Any]:
    keyboard = []
    for row in rows:
        buttons = []
        for item in row:
            if isinstance(item, dict):
                buttons.append(item)
                continue
            label = item[0]
            target = item[1]
            icon_id = item[2] if len(item) > 2 else ""
            label = button_label(label)
            btn: dict[str, Any] = {"text": label}
            if icon_id:
                btn["icon_custom_emoji_id"] = str(icon_id)
            if target.startswith("webapp:"):
                btn["web_app"] = {"url": target.removeprefix("webapp:")}
            elif target.startswith(("https://", "http://", "tg://")):
                btn["url"] = target
            else:
                btn["callback_data"] = target
            buttons.append(btn)
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
    "catalog": "⚔️",   # 01_sword.png — Клинок бренда «СИЛА И ЧЕСТЬ»
    "sword": "⚔️",     # 01_sword.png
    "seal": "🛡️",      # 02_seal.png — Нагрудная печать
    "drop": "👕",      # 03_drop.png — Оверсайз футболка дропа
    "orders": "📦",    # 04_orders.png — Бокс первого дропа / Снабжение
    "account": "🪪",   # 05_profile.png — Стальной армейский жетон
    "tag": "🪪",       # 05_profile.png
    "support": "💬",   # 06_support.png — Штаб-связь
    "cart": "🛒",      # 07_cart.png — Кофр заказа
    "ruler": "🏷️",     # 08_ruler.png — Тканый шеврон у подола / Замеры
    "size": "🏷️",      # 08_ruler.png
    "patch": "🏷️",     # 08_ruler.png
    "pay": "💳",       # 09_card.png — Титан-карта
    "looks": "🗡️",     # 10_lookbook.png — Спина с клинком
    "lookbook": "🗡️",  # 10_lookbook.png — Лукбук / спина с клинком
    "channel": "📢",   # 11_channel.png — Тактический сигнал канала
    "back": "⬅️",      # 12_back.png — Уставной шеврон возврата
    "gift": "🎁",
    "search": "🔎",
    "film": "🎬",
    "wait": "⏳",
    "fabric": "🧵",
    "cut": "✂️",
    "stock": "🏷️",
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
    "outside": "↗",
    "ok": "✅",
    "cancel": "✖",
}


def icon(kind: str, label: str) -> str:
    """Подпись кнопки: знак из общего словаря + короткий текст."""
    symbol = ICON.get(kind, "")
    return f"{symbol} {label}".strip() if symbol else label


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


def buyer_reply_keyboard(
    webapp_url: str = "",
    is_admin: bool = False,
    custom_emoji_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Постоянная нижняя клавиатура покупателя: строгий стиль СИЛА И ЧЕСТЬ."""
    emojis = custom_emoji_ids or {}
    rows: list[list[dict[str, Any]]] = []
    if webapp_url.startswith("https://"):
        btn: dict[str, Any] = {"text": "Открыть витрину", "web_app": {"url": webapp_url}, "style": "primary"}
        if emojis.get("cart"):
            btn["icon_custom_emoji_id"] = str(emojis["cart"])
        rows.append([btn])

    def _reply_btn(label: str, icon_key: str) -> dict[str, Any]:
        icon_id = emojis.get(icon_key)
        if icon_id:
            # Кастомный эмодзи рисуется Telegram на самой кнопке;
            # текст кнопки не должен дублировать стандартный эмодзи.
            return {"text": label, "icon_custom_emoji_id": str(icon_id)}
        fallback = ICON.get(icon_key, "")
        text = f"{fallback} {label}".strip() if fallback else label
        return {"text": text}

    b_drop = _reply_btn("Первый дроп", "catalog" if "catalog" in emojis else "sword")
    b_acc = _reply_btn("Личный кабинет", "account")
    b_chan = _reply_btn("Канал", "channel")
    b_seal = _reply_btn("О бренде", "seal")
    b_sup = _reply_btn("Поддержка", "support")

    rows.extend([
        [b_drop, b_acc],
        [b_chan, b_seal],
    ])

    if is_admin:
        rows.append([b_sup, {"text": "⚙️ Управление магазином"}])
    else:
        rows.append([b_sup])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "СИЛА И ЧЕСТЬ",
    }


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


SUPPORT_FAQ_DEFAULT: list[dict[str, str]] = [
    {"key": "where", "label": "Где моя покупка",
     "answer": "Заказы сейчас оформляются в чате магазина. Чтобы узнать статус, напиши менеджеру и укажи детали своего заказа."},
    {"key": "pay", "label": "Как оплатить",
     "answer": "Выбери вещь, отправь подготовленный заказ в чат и согласуй доступный способ оплаты с менеджером."},
    {"key": "refund", "label": "Возврат",
     "answer": "Для оформления возврата напиши менеджеру, укажи детали заказа и причину. Адрес и дальнейшие действия согласуем в чате."},
]

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
    "shipped": "передана в доставку",
    "received": "получена",
    "completed": "завершена",
    "cancelled": "отменена",
}

# Mini App говорит «заказ», а native-карточка — «покупка»: не смешиваем род.
PUBLIC_ORDER_STATUS_LABELS = {
    "new": "новый",
    "awaiting_payment": "ждёт оплаты",
    "paid": "оплачен",
    "confirmed": "подтверждён",
    "shipped": "передан в доставку",
    "received": "получен",
    "completed": "завершён",
    "cancelled": "отменён",
}

ORDER_STATUS_MESSAGES = {
    "paid": "Оплата прошла. Дальше менеджер проверит наличие и согласует доставку.",
    "confirmed": "Наличие подтверждено. Собираем твою вещь к отправке.",
    "shipped": "Заказ передан в доставку. Если выбран СДЭК и доступен трек-номер, менеджер отправит его отдельно.",
    "received": "Заказ отмечен полученным. Спасибо, что выбрал «Силу и честь».",
    "completed": "Покупка завершена. Спасибо, что выбрал этот выпуск.",
    "cancelled": "Покупка отменена. Если остались вопросы — напиши менеджеру.",
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
    "shipped": "собрана",
    "received": "собрана",
    "completed": "собрана",
    "cancelled": "не собираем",
}

ORDER_DELIVERY_LINES = {
    "new": "согласуем после оплаты",
    "awaiting_payment": "согласуем после оплаты",
    "paid": "условия согласует менеджер",
    "confirmed": "готовим к передаче в доставку",
    "shipped": "передана в доставку",
    "received": "получена",
    "completed": "покупка завершена",
    "cancelled": "не отправляем",
}

PUBLIC_DELIVERY_LINES = {
    **ORDER_DELIVERY_LINES,
    "shipped": "передан в доставку",
    "received": "получен",
    "completed": "заказ завершён",
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
    if state == "expired":
        return "счёт истёк"
    if state == "refunded":
        return "возврат отмечен"
    if status in {"new", "awaiting_payment"}:
        return f"ждёт оплаты · {amount}" if amount else "ждёт оплаты"
    if status in {"paid", "confirmed", "shipped", "received", "completed"}:
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
    """Команды команды. Вешаются отдельным scope — покупатель их не видит.

    В меню только уникальные входы: дубли кнопок пульта (/admin, /stats,
    /orders, /add, /access, /draws) из экрана убраны, но сами команды
    работают — кто привык печатать, не потеряет привычку.
    """
    return [
        {"command": "start", "description": "Главное меню"},
        {"command": "broadcast", "description": "Рассылка: /broadcast текст"},
        {"command": "reports", "description": "Журнал рассылок"},
        {"command": "money", "description": "Деньги: выручка и чек"},
        {"command": "digest", "description": "Дайджест: что сегодня"},
        {"command": "shelf", "description": "Витрина: скрыть и показать"},
        {"command": "refunds", "description": "Возвраты: причины и статус"},
        {"command": "again", "description": "Повторить прошлую рассылку"},
        {"command": "faqs", "description": "Ответы поддержки: набор"},
        {"command": "unpaid", "description": "Ждут оплаты: напомнить"},
        {"command": "grant", "description": "Назначить админа: /grant id"},
        {"command": "help", "description": "Как это работает"},
    ]


def make_chat_menu_button(webapp_url: str = "") -> dict[str, Any]:
    if webapp_url and webapp_url.startswith("https://"):
        return {
            "type": "web_app",
            "text": "Витрина",
            "web_app": {"url": webapp_url},
        }
    return {"type": "commands"}


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
        "status_label": PUBLIC_ORDER_STATUS_LABELS.get(status, status),
        "product_id": str(row["product_id"]) if "product_id" in keys else "",
        "product_name": str(row["product_name"]),
        "size": str(row["size"]),
        "quantity": int(row["quantity"] or 1),
        "created_at": str(row["created_at"]),
        "can_cancel": status in {"new", "awaiting_payment"},
        "can_pay": can_pay,
        "payment_id": payment_id,
        "amount_rub": amount,
        "amount_label": format_rub(amount) if amount else "",
        "phone": str(row["phone"] or "") if "phone" in keys else "",
        "customer_name": str(row["customer_name"] or "") if "customer_name" in keys else "",
        "city": str(row["city"] or "") if "city" in keys else "",
        "address": str(row["address"] or "") if "address" in keys else "",
        "entrance": str(row["entrance"] or "") if "entrance" in keys else "",
        "delivery_method": str(row["deliver"] or "") if "deliver" in keys else "",
        "legacy_details": (
            "" if any(str(row[key] or "").strip() for key in ("city", "address", "entrance", "deliver") if key in keys)
            else str(row["note"] or "") if "note" in keys else ""
        ),
        "fulfilment_label": ORDER_WORK_LINES.get(status, status),
        "delivery_status_label": PUBLIC_DELIVERY_LINES.get(status, status),
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


PRODUCT_EMOJI_KINDS = {
    "tee-sila-i-chest": "product_sila",
    "tee-vzglyad": "product_vzglyad",
    "tag-sila-i-chest": "product_tag",
}
PRODUCT_EMOJI_FALLBACK = {"product_sila": "👕", "product_vzglyad": "👁", "product_tag": "🏷"}


def inline_emoji_kind(button: dict[str, Any]) -> str:
    """Choose a brand icon for every button without changing its action."""
    if button.get("web_app"):
        return "cart"
    action = str(button.get("callback_data") or "").lower()
    label = str(button.get("text") or "").lower()
    if action.startswith("product:"):
        product_kind = PRODUCT_EMOJI_KINDS.get(action.split(":", 1)[1])
        if product_kind:
            return product_kind
    for prefixes, kind in (
        (("search", "find"), "search"),
        (("gift", "present"), "gift"),
        (("wsize", "msize", "wait", "size", "fit", "guide"), "size"),
        (("pay", "payment", "refund"), "pay"),
        (("cart", "basket", "checkout"), "cart"),
        (("delivery", "shipping", "track"), "delivery"),
        (("order", "my_orders"), "orders"),
        (("support", "help"), "support"),
        (("account", "profile"), "account"),
        (("size", "fit", "guide"), "size"),
        (("product", "catalog", "cat:", "drop"), "drop"),
        (("channel",), "channel"),
        (("back", "menu", "home"), "back"),
    ):
        if action.startswith(prefixes):
            return kind
    for words, kind in (
        (("поиск", "найти", "искать"), "search"),
        (("подарок", "подарки", "жетон"), "gift"),
        (("оплат", "платеж", "платёж", "возврат средств"), "pay"),
        (("корзин", "оформить", "магазин"), "cart"),
        (("достав", "отправ", "трек"), "delivery"),
        (("заказ", "покупк"), "orders"),
        (("поддерж", "менеджер", "помощ"), "support"),
        (("профил", "кабинет"), "account"),
        (("размер", "замер"), "size"),
        (("каталог", "витрин", "вещ", "товар"), "drop"),
        (("канал",), "channel"),
        (("назад", "главное меню"), "back"),
    ):
        if any(word in label for word in words):
            return kind
    symbol_kinds = [(ICON[key], kind) for key, kind in (
        ("drop", "drop"), ("orders", "orders"), ("account", "account"),
        ("support", "support"), ("cart", "cart"), ("pay", "pay"),
        ("back", "back"), ("delivery", "delivery"), ("channel", "channel"),
        ("ruler", "tag"), ("catalog", "sword"))]
    return next((kind for symbol, kind in symbol_kinds if label.startswith(symbol.lower())), "sword")


def add_inline_emoji(markup: dict[str, Any] | None, ids: dict[str, str]) -> dict[str, Any] | None:
    """Add configured icons while preserving actions and the caller's markup."""
    if not isinstance(markup, dict) or not isinstance(markup.get("inline_keyboard"), list):
        return markup
    result = dict(markup)
    rows = []
    for row in markup["inline_keyboard"]:
        buttons = []
        for original in row:
            button = dict(original)
            if not button.get("icon_custom_emoji_id"):
                kind = inline_emoji_kind(button)
                value = str(ids.get(kind) or ids.get("sword") or "")
                if value.isdigit():
                    button["icon_custom_emoji_id"] = value
                    label = str(button.get("text") or "")
                    for symbol in sorted(set(ICON.values()), key=len, reverse=True):
                        if label.startswith(symbol + " "):
                            button["text"] = label[len(symbol):].strip()
                            break
            buttons.append(button)
        rows.append(buttons)
    result["inline_keyboard"] = rows
    return result


def without_button_icons(markup: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(markup, dict):
        return markup
    result = dict(markup)
    for key in ("inline_keyboard", "keyboard"):
        if isinstance(markup.get(key), list):
            result[key] = []
            for row in markup[key]:
                cleaned = []
                for button in row:
                    if not isinstance(button, dict):
                        cleaned.append(button)
                        continue
                    plain = {k: v for k, v in button.items() if k != "icon_custom_emoji_id"}
                    if button.get("icon_custom_emoji_id") and button.get("web_app") and plain.get("text") == "ОТКРЫТЬ МАГАЗИН":
                        plain["text"] = "ОТКРЫТЬ МАГАЗИН ↗"
                    cleaned.append(plain)
                result[key].append(cleaned)
    return result



class ScreenSender:
    """Прокси к API: первый экран после нажатия правит то сообщение, чью кнопку нажали.

    Экраны бота отправляют сообщения через ``api.send_message`` в 144 местах,
    поэтому перехват живёт здесь, а не в каждом вызове. Захват одноразовый и
    только для того же чата: всё, что уходит дальше (уведомления менеджеру,
    счета, подтверждения покупки), отправляется как раньше.
    """

    def __init__(self, api: Any, take_edit_target: Callable[[int], int | None], inline_ids: dict[str, str] | None = None):
        self._api = api
        self._take_edit_target = take_edit_target
        self._inline_ids = inline_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def _send_photo_with_icon_fallback(self, method: str, chat_id: int, photo: Any,
                                       caption: str, reply_markup: dict[str, Any] | None) -> Any:
        send = getattr(self._api, method)
        original_markup = reply_markup
        if self._inline_ids is not None:
            reply_markup = add_inline_emoji(reply_markup, self._inline_ids)
        try:
            return send(chat_id, photo, caption, reply_markup)
        except RuntimeError as exc:
            error = str(exc).lower()
            if self._inline_ids is None or not any(key in error for key in ("custom emoji", "custom_emoji", "button_icon")):
                raise
            fallback = without_button_icons(reply_markup)
            if fallback == reply_markup:
                raise
            LOG.warning("Telegram rejected custom button icons on welcome photo; retrying without icons")
            return send(chat_id, photo, caption, without_button_icons(original_markup))

    def send_photo(self, chat_id: int, photo: str, caption: str, reply_markup: dict[str, Any] | None = None) -> Any:
        return self._send_photo_with_icon_fallback("send_photo", chat_id, photo, caption, reply_markup)

    def send_photo_file(self, chat_id: int, photo: Path, caption: str = "", reply_markup: dict[str, Any] | None = None) -> Any:
        return self._send_photo_with_icon_fallback("send_photo_file", chat_id, photo, caption, reply_markup)

    def send_animation_file(self, chat_id: int, animation: Path, caption: str = "", reply_markup: dict[str, Any] | None = None) -> Any:
        return self._send_photo_with_icon_fallback("send_animation_file", chat_id, animation, caption, reply_markup)

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> Any:
        original_markup = reply_markup
        if self._inline_ids is not None:
            reply_markup = add_inline_emoji(reply_markup, self._inline_ids)
        message_id = self._take_edit_target(chat_id)
        if message_id and self._api.edit_message(chat_id, message_id, text, reply_markup):
            return {"message_id": message_id, "chat": {"id": chat_id}, "edited": True}
        try:
            return self._api.send_message(chat_id, text, reply_markup)
        except RuntimeError as exc:
            error = str(exc).lower()
            if self._inline_ids is None or not any(key in error for key in ("custom emoji", "custom_emoji", "button_icon")):
                raise
            fallback = without_button_icons(reply_markup)
            if fallback == reply_markup:
                raise
            LOG.warning("Telegram rejected custom button icons; retrying with plain button labels")
            return self._api.send_message(chat_id, text, without_button_icons(original_markup))


class BrandBot:
    def __init__(self, settings: Settings, api: TelegramAPI, db: Database, catalog: Catalog):
        self.settings = settings
        self.db = db
        self.catalog = catalog
        self.bot_username: str = ""
        # Экран, открытый нажатием, меняется на месте: прокси перехватывает
        # первое сообщение экрана и правит то сообщение, чью кнопку нажали.
        self._screen_edit: tuple[int, int] | None = None
        self.api = ScreenSender(api, self.take_screen_edit, settings.custom_emoji_ids if settings.inline_emoji_enabled else None)

    def emoji(self, kind: str, fallback: str = "") -> str:
        """Возвращает кастомный эмодзи Telegram (<tg-emoji>) или стандартный символ."""
        emoji_id = self.settings.custom_emoji_ids.get(kind)
        if not emoji_id:
            raw_ids = self.db.kv_get("custom_emoji_ids")
            if raw_ids:
                try:
                    ids = json.loads(raw_ids)
                    if isinstance(ids, dict):
                        emoji_id = ids.get(kind)
                except Exception:
                    pass
        fb = fallback or ICON.get(kind, "")
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>'
        return fb

    def show_emoji_pack(self, chat_id: int) -> None:
        """Показать актуальный набор анимированных значков и ссылку на него."""
        pack_link = f"https://t.me/addemoji/{self.settings.custom_emoji_pack}"
        text = (
            f"<b>ЭМОДЗИ-ПАК «{esc(self.settings.brand_name)}»</b>\n\n"
            "11 анимированных значков для магазина: вещи, заказы, персонаж, "
            "поддержка, оплата, доставка, жетон, канал и фирменный знак.\n\n"
            "Добавь набор в Telegram по кнопке ниже."
        )
        markup = inline_keyboard([
            [(f"Установить эмодзи-пак {ICON['outside']}", pack_link)],
            [(f"{ICON['back']} Главное меню", "menu")]
        ])
        self.api.send_message(chat_id, text, markup)

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
        """Send flagship Drop 001 campaign key-visual on /start (no gif/animation)."""
        founder_photo = self.local_asset_path("assets/client/bot-welcome-drop001-20261112c.jpg")
        if founder_photo and founder_photo.is_file():
            try:
                self._send_welcome_file(chat_id, founder_photo, caption, keyboard)
                return
            except Exception:
                LOG.exception("Flagship welcome photo unavailable; using existing welcome")

        asset = self.catalog.data.get("brand", {}).get("welcome_image") or "assets/welcome-sword-logo.jpg"
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

    def _send_welcome_animation(self, chat_id: int, anim_path: Path, caption: str, keyboard: dict[str, Any] | None) -> None:
        stat = anim_path.stat()
        cache_key = "welcome_anim_file_id:" + hashlib.sha256(
            compact_json([str(anim_path), str(stat.st_mtime_ns), str(stat.st_size)]).encode()
        ).hexdigest()
        cached = self.db.kv_get(cache_key)
        if cached:
            try:
                payload = {
                    "chat_id": chat_id,
                    "animation": cached,
                    "caption": fit_html_text(caption, CAPTION_LIMIT - 24),
                    "parse_mode": "HTML",
                }
                if keyboard:
                    ids = getattr(self.api, "_inline_ids", None)
                    payload["reply_markup"] = add_inline_emoji(keyboard, ids) if ids else keyboard
                self.api.call("sendAnimation", payload)
                return
            except Exception:
                LOG.info("Cached welcome anim file_id rejected, re-uploading")
                self.db.kv_delete(cache_key)

        result = self.api.send_animation_file(chat_id, anim_path, caption, keyboard)
        if isinstance(result, dict):
            anim = result.get("animation")
            if isinstance(anim, dict) and anim.get("file_id"):
                self.db.kv_set(cache_key, anim["file_id"])

    def _send_welcome_file(self, chat_id: int, photo: Path, caption: str, keyboard: dict[str, Any] | None) -> None:
        stat = photo.stat()
        cache_key = "welcome_file_id:" + hashlib.sha256(
            compact_json([str(photo), str(stat.st_mtime_ns), str(stat.st_size)]).encode()
        ).hexdigest()
        plain_caption = re.sub(r'</?tg-emoji(?:\s+emoji-id="[0-9]+")?>', "", caption)
        cached = self.db.kv_get(cache_key)
        if cached:
            for candidate in dict.fromkeys((caption, plain_caption)):
                try:
                    self.api.send_photo(chat_id, cached, candidate, keyboard)
                    return
                except Exception:
                    LOG.info("Cached welcome photo send attempt failed; retrying fallback")
            self.db.kv_delete(cache_key)
        result = None
        last_exc: Exception | None = None
        for candidate in dict.fromkeys((caption, plain_caption)):
            try:
                result = self.api.send_photo_file(chat_id, photo, candidate, keyboard)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                LOG.warning("Welcome photo upload failed with custom emoji; retrying plain caption")
        if last_exc is not None:
            raise last_exc
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

    def send_product_video(self, chat_id: int, user_id: int, product_id: str) -> None:
        """Отправить студийное 360°-видео вещи в чат с кэшем file_id в SQLite."""
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Её сняли с продажи или уже разобрали.")
            return
        self.db.event(user_id, "product_open", {"product_id": product_id, "media": "video"})
        video_asset = str(product.get("video") or "").strip()
        video = self.local_asset_path(video_asset)
        poster = self.local_asset_path(product.get("card_image") or product.get("image"))
        verb = "предзаказ" if product.get("badge") == "ПРЕДЗАКАЗ" or "предзаказ" in str(product.get("stock_label", "")).lower() else "заказ"
        order_label = "ОФОРМИТЬ ПРЕДЗАКАЗ" if verb == "предзаказ" else "ОФОРМИТЬ ЗАКАЗ"
        rows: list[list[tuple[str, str]]] = []
        if not product.get("gift") and self.price_is_payable(product):
            rows.append([(order_label, f"order_direct:{product_id}")])
            rows.append([(icon("size", "Таблица размеров"), f"size_guide:product:{product_id}")])
        rows.extend(nav_rows((str(product["name"]), f"product:{product_id}")))
        keyboard = inline_keyboard(rows)
        caption = (
            f"<b>{esc(str(product['name']))} · 360° ОБЗОР</b>\n"
            f"{esc(str(product['price']))} · {esc(str(product.get('stock_label') or 'Первый выпуск'))}"
        )
        if video and video.is_file() and video.stat().st_size <= TEASER_UPLOAD_LIMIT:
            stat = video.stat()
            sig = compact_json([video_asset, str(stat.st_mtime_ns), str(stat.st_size)])
            cache_key = "pvid_file_id:" + hashlib.sha256(sig.encode()).hexdigest()
            cached = self.db.kv_get(cache_key)
            if cached:
                try:
                    self.api.send_video(chat_id, cached, caption, keyboard)
                    return
                except Exception:
                    LOG.info("Cached product video file_id rejected, re-uploading")
                    self.db.kv_delete(cache_key)
            try:
                result = self.api.send_video(
                    chat_id,
                    video,
                    caption,
                    keyboard,
                    thumbnail=poster if poster and poster.is_file() else None,
                )
                self._remember_teaser(result, cache_key)
                return
            except Exception:
                LOG.exception("Could not send product 360 video for %s", product_id)
        public_vid = self.public_asset_url(video_asset)
        if public_vid.startswith("https://"):
            try:
                self.api.send_video(chat_id, public_vid, caption, keyboard)
                return
            except Exception:
                LOG.info("Public product video URL rejected for %s", product_id)
        self.api.send_message(
            chat_id,
            caption + "\n\nВидео открывается внутри витрины магазина.",
            keyboard,
        )

    # ------------------------------------------------------------------ menus

    def buyer_reply_keyboard(self, chat_id: int | None = None) -> dict[str, Any]:
        """Постоянная нижняя клавиатура покупателя под брендинг СИЛА И ЧЕСТЬ."""
        is_adm = bool(chat_id and self.is_admin(int(chat_id)))
        return buyer_reply_keyboard(
            self.settings.webapp_url,
            is_admin=is_adm,
            custom_emoji_ids=self.settings.custom_emoji_ids if self.settings.inline_emoji_enabled else {},
        )

    def main_menu(self, chat_id: int | None = None) -> dict[str, Any]:
        """Главное меню: магазин, дроп, покупки, кабинет и контакты.

        ``chat_id`` в личке совпадает с ``user_id``, поэтому по нему же
        поднимаем незавершённую оплату — отдельный параметр не нужен.
        Незакрытый счёт — единственная причина показать кнопку вне сетки:
        главное действие всегда широкое и всегда сверху.
        """
        rows: list[list[Any]] = []
        if self.settings.webapp_url.startswith("https://"):
            shop_button: dict[str, Any] = {"text": "ОТКРЫТЬ ВИТРИНУ · 3D ↗", "web_app": {"url": self.settings.webapp_url}, "style": "primary"}
            cart_emoji = str(self.settings.custom_emoji_ids.get("cart") or "")
            if self.settings.inline_emoji_enabled and cart_emoji.isdigit():
                shop_button["text"] = "ОТКРЫТЬ ВИТРИНУ · 3D"
                shop_button["icon_custom_emoji_id"] = cart_emoji
            rows.append([shop_button])
        draft = self.db.pending_payment(chat_id) if chat_id and ONLINE_PAYMENTS_ENABLED else None
        if draft:
            payment_id, amount = draft
            label = f"Продолжить оплату · {format_rub(amount)}" if amount else "Продолжить оплату"
            rows.append([(f"{ICON['pay']} {label} →", f"draft:{payment_id}")])
        ce = self.settings.custom_emoji_ids if self.settings.inline_emoji_enabled else {}
        sila = self.catalog.get("tee-sila-i-chest")
        vzglyad = self.catalog.get("tee-vzglyad")
        dog_tag = self.catalog.get("tag-sila-i-chest")
        quick_products: list[tuple[str, str, str]] = []
        if sila:
            quick_products.append((f"▪ Сила и честь · {sila.get('price', '4 200 ₽')}", "product:tee-sila-i-chest", ce.get("product_sila") or ce.get("sword", "")))
        if vzglyad:
            quick_products.append((f"▪ Взгляд · {vzglyad.get('price', '3 900 ₽')}", "product:tee-vzglyad", ce.get("product_vzglyad") or ce.get("sword", "")))
        if quick_products:
            rows.append(quick_products)
        second_row: list[tuple[str, str, str]] = []
        if dog_tag:
            second_row.append(("🎖 Жетон · подарок", "product:tag-sila-i-chest", ce.get("product_tag") or ce.get("tag", "")))
        second_row.append((icon("size", "Размеры S–3XL"), "size_guide", ce.get("size", "")))
        rows.append(second_row)
        rows.extend(
            [
                [(icon("catalog", "Первый дроп"), "cat:drop", ce.get("catalog") or ce.get("sword", "")),
                 ("▶ Тизер выпуска", "teaser", ce.get("looks", ""))],
                [(icon("orders", "Мои покупки"), "my_orders", ce.get("orders", "")),
                 (icon("account", "Кабинет"), "account", ce.get("account", ""))],
                [(icon("seal", "О бренде"), "about", ce.get("seal", ""))],
            ]
        )
        channel_link = self.channel_link()
        contacts = [(icon("support", "Поддержка"), "support", ce.get("support", ""))]
        if channel_link:
            contacts.insert(0, (icon("channel", "Канал ↗"), channel_link, ce.get("channel", "")))
        rows.append(contacts)
        if chat_id and self.is_admin(int(chat_id)):
            # Владелец и админы видят вход в пульт прямо из /start:
            # управление магазином — такое же главное окно, как каталог.
            rows.append([(icon("tools", "Управление магазином"), "adm:panel")])
        markup = inline_keyboard(rows)
        for row in markup["inline_keyboard"]:
            for button in row:
                if button.get("icon_custom_emoji_id") and not button.get("web_app"):
                    # Telegram draws the brand icon; avoid a second emoji in its label.
                    button["text"] = button["text"].partition(" ")[2]
        return markup

    def menu_text(self, hint: str = "") -> str:
        """Заголовок главного меню: один на весь экран, без повторов бренда."""
        text = (
            f"<b>{esc(self.settings.brand_name)}</b>\n"
            "Первый выпуск · одежда с характером.\n\n"
            "Магазин — вещи и размеры. Кабинет — твои покупки.\n"
            "Нужен ориентир? Открой размеры или помощь."
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
            self.send_menu(chat_id, f"{reason}\nПопробуй ещё раз или открой помощь.")
        except Exception as exc:
            LOG.error("Recovery screen failed for chat %s: %s", chat_id, exc)

    def interest_menu(self) -> dict[str, Any]:
        """Первый вопрос новому покупателю: те же две колонки, что и в главном меню."""
        options = [(category["name"], f"intr:{category['id']}") for category in self.catalog.categories[:6]]
        rows = option_rows(options)
        rows.append([("Пока не знаю", "intr:skip")])
        return inline_keyboard(rows)

    def show_about(self, chat_id: int) -> None:
        seal_mark = self.emoji("sword", "⚔") if self.settings.inline_emoji_enabled else "⚔"
        tag_mark = self.emoji("tag", "🎖") if self.settings.inline_emoji_enabled else "🎖"
        self.api.send_message(
            chat_id,
            f"{seal_mark} <b>О БРЕНДЕ · {esc(self.settings.brand_name)}</b>\n\n"
            "Бренд Никиты Ворожбитова. Городская форма для тех, кто выбирает дисциплину, слово и характер каждый день.\n\n"
            "<b>ВЫПУСК 001:</b>\n"
            "• <b>«СИЛА И ЧЕСТЬ»</b> — плотный чёрный хлопок 240–260 г/м², уставной шрифт на груди, шеврон НВ у горловины и состаренный стальной клинок вдоль спины.\n"
            "• <b>«ВЗГЛЯД»</b> — принт на груди и круг из 10 принципов братства вокруг монограммы НВ с мечом на спине (предзаказ).\n"
            f"• {tag_mark} <b>Стальной жетон № 00001–00100</b> — лазерная гравировка с двух сторон, в подарок первой сотне заказов первого дропа.\n\n"
            "Обе футболки можно покрутить в интерактивном <b>3D 360°</b> прямо в витрине."
            + (f"\n\n{ICON['support']} Связь с менеджером: @{esc(self.settings.support_username)}" if self.settings.support_username else "")
            + self.cta("channel"),
            inline_keyboard(self.channel_rows() + nav_rows(("Главное меню", "menu"))),
        )

    def account_menu(self, user_id: int) -> dict[str, Any]:
        """Кабинет покупателя: покупки и лист ожидания."""
        rows = [
            [(icon("orders", "Мои покупки"), "my_orders")],
        ]
        waiting = self.db.waitlist_for_user(user_id)
        if waiting:
            label = "Жду размер" if len(waiting) == 1 else f"Жду размер ({len(waiting)})"
            rows.append([(icon("wait", label), "waits")])
        rows.extend(self.channel_rows())
        rows.extend(nav_rows(("Главное меню", "menu")))
        return inline_keyboard(rows)

    def account_text(self, user_id: int) -> str:
        user = self.db.get_user(user_id)
        phone = str(user["phone"]) if user and user["phone"] else ""
        orders = self.db.orders_for_user(user_id)
        orders_count = len(orders)
        gift = self.db.gift_for_user(user_id)
        if gift is not None and gift["gift_serial"] is None:
            self.db.gift_status(self.catalog.get(str(gift["gift_product_id"])))
            gift = self.db.gift_for_user(user_id)
        lines = ["<b>МОЙ КАБИНЕТ</b>", ""]
        if gift is not None and gift["gift_serial"] is not None:
            lines.append(f"🎖 Жетон: №{int(gift['gift_serial']):05d} · серия первого дропа")
        else:
            lines.append("🎖 Жетон: серийный номер закрепляется за подарком в заказе")
        lines.append(f"{ICON['notice']} Номер: {esc(phone) if phone else 'не указан'}")
        lines.append(f"{ICON['orders']} Покупок: {orders_count}")
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

    def faq_items(self) -> list[dict[str, str]]:
        """Быстрые ответы поддержки: набор живёт в kv, владелец правит его в /faqs."""
        raw = self.db.kv_get("support_faqs")
        if raw:
            try:
                items = json.loads(raw)
                if isinstance(items, list):
                    return [i for i in items if isinstance(i, dict) and i.get("key")]
            except ValueError:
                LOG.warning("support_faqs kv is broken, falling back to default")
        return [dict(i) for i in SUPPORT_FAQ_DEFAULT]

    def save_faq_items(self, items: list[dict[str, str]]) -> None:
        self.db.kv_set("support_faqs", compact_json(items))

    def support_menu(self) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = [
            [(icon("question", str(item.get("label"))[:24]),
              self.payment_question_url() if item.get("key") == "pay" and self.payment_question_url()
              else f"faq:{item.get('key')}")
             for item in self.faq_items()[:4]],
        ]
        if self.settings.manager_chat_id or self.settings.support_username:
            rows.append([(icon("orders", "Вопрос по покупке"), "ask")])
        rows.append([(icon("question", "Как это работает"), "help")])
        if self.settings.support_username:
            rows.append([(f"Написать менеджеру {ICON['outside']}", f"https://t.me/{self.settings.support_username}")])
        rows.extend(nav_rows(("Главное меню", "menu")))
        return inline_keyboard(rows)

    def payment_question_url(self) -> str:
        """Open a draft question; the customer decides whether to send it."""
        account = order_manager_username()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", account):
            return ""
        question = urllib.parse.quote("Здравствуйте! Подскажите, пожалуйста, как я могу оплатить заказ.", safe="")
        return f"https://t.me/{account}?text={question}"

    def support_text(self) -> str:
        lines = ["<b>ПОДДЕРЖКА</b>", ""]
        if self.settings.manager_chat_id:
            lines.append("Отвечаем в этом чате.")
            lines.append("Номер покупки называть не нужно — выбери её кнопкой, контекст уйдёт вместе с вопросом.")
        elif self.settings.support_username:
            lines.append(f"Менеджер: @{esc(self.settings.support_username)}")
        else:
            lines.append("Прямая связь с менеджером пока не подключена. Короткие ответы — в разделах ниже.")
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

        if self.is_admin(user_id):
            try:
                self.api.call(
                    "setMyCommands",
                    {"commands": staff_commands(), "scope": {"type": "chat", "chat_id": chat_id}},
                )
                self.api.call(
                    "setChatMenuButton",
                    {"chat_id": chat_id, "menu_button": {"type": "commands"}},
                )
            except Exception as exc:
                LOG.debug("Could not set staff commands for %s on start: %s", chat_id, exc)

        # Reply and inline keyboards require separate Telegram messages.
        # Set the persistent navigation first, then show the welcome card.
        if chat_id == user_id:
            self.api.send_message(chat_id, "Меню магазина всегда под рукой.", self.buyer_reply_keyboard(chat_id))
        if source == "submit_look":
            self.db.set_state(user_id, "awaiting_community_photo", {})
            self.api.send_message(
                chat_id,
                "<b>РАЗДЕЛ «НА СВОИХ» · СИЛА И ЧЕСТЬ</b>\n\n"
                "Пришли сюда <b>одним сообщением</b> свою фотографию в футболке бренда, а в подписи укажи:\n"
                "• Город\n• Модель и размер (например: <i>СИЛА И ЧЕСТЬ, размер L, 184 см / 86 кг</i>)\n\n"
                "После проверки модератором кадр сразу появится в живой галерее Mini App.\n"
                "Чтобы выйти — напиши <code>отмена</code>.",
            )
            return
        menu = self.main_menu(chat_id)
        brand = esc(self.settings.brand_name)
        seal_mark = self.emoji("sword", "⚔") if self.settings.inline_emoji_enabled else "⚔"
        sila_mark = self.emoji("product_sila", "▪") if self.settings.inline_emoji_enabled else "▪"
        vzglyad_mark = self.emoji("product_vzglyad", "▪") if self.settings.inline_emoji_enabled else "▪"
        tag_mark = self.emoji("tag", "🎖") if self.settings.inline_emoji_enabled else "🎖"
        text = (
            f"{seal_mark} <b>{brand} · ВЫПУСК 001</b>\n"
            f"{name}, ты на закрытой территории бренда Никиты Ворожбитова.\n\n"
            "<blockquote>Не на словах. Одежда как внутренний устав — плотный хлопок 240 г/м², выверенный крой и первый тираж без повторов.</blockquote>\n\n"
            f"{sila_mark} <b>«СИЛА И ЧЕСТЬ»</b> — 4 200 ₽ · клинок вдоль спины\n"
            f"{vzglyad_mark} <b>«ВЗГЛЯД»</b> — 3 900 ₽ · 10 принципов · предзаказ\n"
            f"{tag_mark} <b>Стальной жетон № 00001–00100</b> — в подарок первой сотне\n\n"
            "Крути обе модели в <b>3D 360°</b> на витрине или открой вещь сразу кнопкой ниже."
        )
        self.send_welcome(chat_id, text, menu)

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
        rows = [[(f"{p['name']} · {'подарок' if p.get('gift') else p['price']}", f"product:{p['id']}")] for p in products]
        if category_id == "drop":
            rows.append([("▶ Тизер выпуска 001", "teaser")])
        rows.extend(nav_rows(("Витрина", "catalog")))
        paid_count = sum(1 for p in products if not p.get("gift"))
        gift_count = sum(1 for p in products if p.get("gift"))
        count_label = (
            f"{paid_count} {things_word(paid_count)} + жетон в подарок"
            if paid_count and gift_count
            else f"{len(products)} {things_word(len(products))}"
        )
        self.api.send_message(
            chat_id,
            f"<b>{esc(title.upper())}</b> · {count_label}\n\n"
            "Нажми на вещь — покажем студийные детали, состав и размеры S–3XL.",
            inline_keyboard(rows),
        )

    def product_card_text(self, product: dict[str, Any]) -> str:
        """Карточка вещи: название → цена → сведения → варианты.

        Структура одинаковая у всех вещей, поэтому покупатель читает карточку,
        а не разбирается в ней заново на каждом товаре.
        """
        lines = [f"<b>{esc(str(product['name']).upper())}</b>"]
        price = f"<b>{'БЕСПЛАТНО' if product.get('gift') else esc(str(product['price']))}</b>"
        badge = str(product.get("badge") or "").strip()
        if badge:
            price += f" · {esc(badge)}"
        lines.append(price)
        if product.get("gift"):
            gift = self.db.gift_status(product)
            if gift:
                lines.append("Подарок из первой сотни к первому дропу · наличие уточняется.")
        elif not self.price_is_payable(product):
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
        media_note = str(product.get("media_note") or "").strip()
        if media_note:
            lines.extend(["", esc(media_note)])
        signature = str(product.get("signature") or "").strip()
        if signature:
            lines.extend(["", esc(signature)])
        return "\n".join(lines)

    def product_card_compact(self, product: dict[str, Any]) -> str:
        """Short product caption, with the details in the storefront."""
        title = esc(str(product.get("name") or "ВЕЩЬ").upper())
        kind = PRODUCT_EMOJI_KINDS.get(str(product.get("id") or ""))
        if kind:
            fallback = PRODUCT_EMOJI_FALLBACK[kind]
            mark = self.emoji(kind, fallback) if self.settings.inline_emoji_enabled else fallback
            title = f"{mark} {title}"
        price = "В ПОДАРОК" if product.get("gift") else esc(str(product.get("price") or "Цена уточняется"))
        badge = str(product.get("badge") or "").strip()
        lines = [f"<b>{title}</b>", f"<b>{price}</b>" + (f"  ·  {esc(badge)}" if badge else "")]
        description = " ".join(str(product.get("description") or "").split())
        if description:
            if len(description) > 260:
                description = description[:257].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
            lines.extend(["", esc(description)])
        material = " ".join(str(product.get("material") or "").split())
        fit = " ".join(str(product.get("fit") or "").split())
        sizes = " · ".join(str(size) for size in product.get("sizes") or [])
        if material or fit:
            lines.extend(["", esc("  ·  ".join(x for x in (material, fit) if x))])
        if sizes:
            lines.append(f"Размеры: {esc(sizes)}")
        signature = " ".join(str(product.get("signature") or "").split())
        if signature:
            lines.extend(["", esc(signature[:140])])
        return fit_html_text("\n".join(lines), 950)

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
        text = self.product_card_compact(product)
        verb = "предзаказ" if product.get("badge") == "ПРЕДЗАКАЗ" or "предзаказ" in str(product.get("stock_label", "")).lower() else "заказ"
        order_label = "ОФОРМИТЬ ПРЕДЗАКАЗ" if verb == "предзаказ" else "ОФОРМИТЬ ЗАКАЗ"

        if product.get("gift"):
            rows = [[(icon("catalog", "Выбрать вещь из выпуска"), "cat:drop")]]
        elif self.price_is_payable(product):
            rows = [
                [(order_label, f"order_direct:{product_id}")],
            ]
            extra_row: list[tuple[str, str]] = []
            if str(product.get("video") or "").strip():
                extra_row.append((icon("looks", "360°-видео"), f"pvid:{product_id}"))
            extra_row.append((icon("size", "Размеры"), f"size_guide:product:{product_id}"))
            rows.append(extra_row)
            rows.append([(icon("wait", "Ждать размер"), f"wait:{product_id}")])
        else:
            rows = [
                [(icon("support", "Написать менеджеру →"), "support")],
                [(icon("wait", "Ждать размер"), f"wait:{product_id}")],
            ]
        if self.is_admin(user_id):
            rows.append([(icon("tools", "Править вещь"), f"eedit:{product_id}")])
        keyboard = inline_keyboard(
            rows + nav_rows((category_title, f"cat:{product['category']}"))
        )
        # В чат отправляем одно главное фото, как просила заказчица. Полная
        # предметная галерея, 360° и видео остаются внутри Mini App.
        shots = self.product_shots(product)
        photo = self.public_asset_url(product.get("bot_image")) or (shots[0][0] if shots else self.public_asset_url(product.get("photo_url") or product.get("image")))
        plain_text = re.sub(r'</?tg-emoji(?:\s+emoji-id="[0-9]+")?>', '', text)
        if photo:
            sent = False
            for caption in dict.fromkeys((text, plain_text)):
                try:
                    self.api.send_photo(chat_id, photo, caption, keyboard)
                    sent = True
                    break
                except Exception:
                    LOG.warning("Product photo could not be sent; retrying without custom emoji", exc_info=True)
            if sent:
                # A previous text menu cannot be edited into a photo. Retire
                # only its inline controls after the new card is delivered.
                target = self.take_screen_edit(chat_id)
                if target:
                    try:
                        self.api.call("editMessageReplyMarkup", {
                            "chat_id": chat_id, "message_id": target,
                            "reply_markup": {"inline_keyboard": []},
                        })
                    except Exception:
                        LOG.info("Could not remove old product navigation", exc_info=True)
                return
        self.api.send_message(chat_id, plain_text, keyboard)

    def choose_size(self, chat_id: int, user_id: int, product_id: str) -> None:
        product = self.catalog.get(product_id)
        if not product:
            self.product_missing(chat_id, "Её сняли с продажи или уже разобрали.")
            return
        if product.get("gift"):
            self.show_product(chat_id, user_id, product_id)
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [[(size, f"size:{product_id}:{size}") for size in sizes[index:index + 4]] for index in range(0, len(sizes), 4)]
        rows.extend(nav_rows((str(product["name"]), f"product:{product_id}")))
        self.api.send_message(
            chat_id,
            f"<b>РАЗМЕР</b>\n\n{esc(str(product['name']))} · {esc(str(product['price']))}\n"
            "Какой размер берёшь?",
            inline_keyboard(rows),
        )

    def choose_manager_size(self, chat_id: int, user_id: int, product_id: str) -> None:
        """Main checkout: size first, then a prefilled manager chat, never an invoice."""
        product = self.catalog.get(product_id)
        if not product or product.get("gift"):
            self.product_missing(chat_id, "Эту вещь нельзя оформить отдельно.")
            return
        if not order_manager_username():
            # Do not silently turn a manager CTA into a payment flow.
            self.api.send_message(
                chat_id,
                "<b>МЕНЕДЖЕР ПОКА НЕ ПОДКЛЮЧЁН</b>\n\n"
                "Заказ пока не создан. Контакт магазина скоро появится здесь.",
                inline_keyboard(nav_rows((str(product["name"]), f"product:{product_id}"))),
            )
            return
        sizes = [str(size) for size in product["sizes"]]
        rows = [
            [(size, f"msize:{product_id}:{size}") for size in sizes[index:index + 4]]
            for index in range(0, len(sizes), 4)
        ]
        rows.append([(icon("size", "Таблица размеров"), f"size_guide:order_direct:{product_id}")])
        rows.extend(nav_rows((str(product["name"]), f"product:{product_id}")))
        self.api.send_message(
            chat_id,
            f"<b>ОФОРМИТЬ ЗАКАЗ</b>\n\n{esc(str(product['name']))}\n"
            "Выбери размер. Затем откроется Telegram-чат с менеджером и готовым текстом заказа.",
            inline_keyboard(rows),
        )

    def open_manager_order(self, chat_id: int, user_id: int, product_id: str, size: str) -> None:
        product = self.catalog.get(product_id)
        if not product or product.get("gift") or size not in {str(item) for item in product.get("sizes", [])}:
            self.product_missing(chat_id, "Размер или вещь уже недоступны.")
            return
        if not order_manager_username():
            self.choose_manager_size(chat_id, user_id, product_id)
            return
        user = self.db.get_user(user_id)
        phone = str(user["phone"] or "") if user else ""
        verb = "предзаказ" if product.get("badge") == "ПРЕДЗАКАЗ" or "предзаказ" in str(product.get("stock_label", "")).lower() else "заказ"
        gift = self.gift_for_checkout()
        gift_line = "• Подарок: ЖЕТОН (первые 100 жетонов к дропу 001, со 2-го дропа в подарок не будет)\n" if gift else ""
        message = (
            f"Здравствуйте! Хочу оформить {verb}:\n"
            f"• {product['name']} · размер {size} · 1 шт. · {product['price']}\n"
            f"{gift_line}"
            f"Телефон: {phone or 'укажу в чате'}"
        )
        order_url = (
            f"https://t.me/{order_manager_username()}?text="
            f"{urllib.parse.quote(message, safe='')}"
        )
        self.api.send_message(
            chat_id,
            f"<b>ЗАКАЗ ГОТОВ К ОТПРАВКЕ</b>\n\n{esc(str(product['name']))} · размер {esc(size)} · 1 шт.\n\n"
            "Перейди к менеджеру, проверь текст и отправь его в чат. "
            "Без сообщения заказ не создан; онлайн-счёт и списание здесь не выполняются.",
            inline_keyboard(
                [[("ОФОРМИТЬ ЗАКАЗ У МЕНЕДЖЕРА ↗", order_url)]]
                + nav_rows((str(product["name"]), f"product:{product_id}"))
            ),
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
        if product.get("gift"):
            self.api.send_message(
                chat_id,
                "Этот жетон не продаётся отдельно. Добавим его бесплатно к заказу, пока есть номера из первой сотни.",
                inline_keyboard(nav_rows((str(product["name"]), f"product:{product_id}"))),
            )
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
        return (self.settings.brand_name or "СИЛА И ЧЕСТЬ")[:32]

    def gift_for_checkout(self) -> dict[str, Any] | None:
        gift = self.db.gift_status(self.catalog.get(self.settings.gift_product_id))
        return gift if gift and gift["available"] else None

    def build_pay_methods(self, payment_id: str, amount_rub: int, description: str) -> list[dict[str, str]]:
        self.db.expire_payments()
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
                self.db.register_invoice(payment_id, "lava", invoice, amount_rub)
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
                self.db.register_invoice(payment_id, "crypto", invoice, amount_rub)
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
        if payment["gift_product_id"] and not payment["gift_released_at"]:
            lines.append(f"• Подарок: {esc(payment['gift_name'])} · {int(payment['gift_qty'] or 1)} шт. · 0 ₽" + (f" · №{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else ""))
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
                + [[(icon("support", "Написать менеджеру"), "support")]]
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

    def notify_overpayment(self, payment_id: str, provider_tx_id: str, *, queue_only: bool = False) -> None:
        payment = self.db.get_payment(payment_id)
        if not payment:
            return
        user_id = int(payment["user_id"])
        amount = format_rub(int(payment["amount_rub"]))
        self.deliver_message(
            f"overpayment:{payment_id}:{provider_tx_id}:customer", user_id,
            "<b>ПОХОЖЕ, ОПЛАТА ПОВТОРИЛАСЬ</b>\n\n"
            f"По покупке пришло ещё одно подтверждение на {esc(amount)}. "
            "Заказ не дублируем. Менеджер сверит обе транзакции и свяжется по поводу возврата лишней суммы.",
            self.main_menu(user_id), queue_only=True,
        )
        if self.settings.manager_chat_id:
            self.deliver_message(
                f"overpayment:{payment_id}:{provider_tx_id}:manager", self.settings.manager_chat_id,
                f"<b>ПЕРЕПЛАТА · {esc(payment_id)}</b>\nКлиент: {user_id}\n"
                f"Новая транзакция: <code>{esc(provider_tx_id)}</code>\nСумма: {esc(amount)}\n"
                "Заказ повторно не выполнен. Нужна сверка и возврат лишней транзакции.",
                queue_only=True,
            )
        if not queue_only:
            self.flush_notifications()

    def handle_pre_checkout(self, query: dict[str, Any]) -> None:
        if not ONLINE_PAYMENTS_ENABLED:
            query_id = str(query.get("id", ""))
            if query_id:
                self.api.answer_pre_checkout(query_id, False, "Онлайн-оплата временно недоступна. Свяжись с магазином.")
            return
        self.db.expire_payments()
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
        if product.get("gift"):
            self.show_product(chat_id, user_id, str(product["id"]))
            return
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
        self.api.send_message(chat_id, "Покупка собрана. Осталось оплатить.", self.buyer_reply_keyboard(chat_id))
        fingerprint = hashlib.sha256(compact_json([product["id"], size, phone]).encode()).hexdigest()
        try:
            receipt, created = self.db.create_checkout(
                user_id, f"bot:{request_id}", fingerprint,
                [{"product_id": product["id"], "name": product["name"], "size": size, "phone": phone,
                  "quantity": 1, "note": "", "amount_rub": amount, "person": "", "person_label": ""}],
                stars_amount(amount, self.settings.stars_rub_per_star) if self.settings.stars_enabled else 0,
                queue_notifications=lambda receipt: self.queue_checkout_notifications(receipt, user_id, phone, "", chat_id, "бота"),
                gift=self.gift_for_checkout(),
                stock_totals=size_stock_totals([product]),
            )
        except (GiftUnavailable, SizeUnavailable) as exc:
            self.api.send_message(chat_id, esc(str(exc)), self.main_menu(chat_id))
            return
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
        lines = [line]
        if receipt.get("gift"):
            lines.append(f"• Подарок: {esc(receipt['gift']['name'])} · 1 шт. · 0 ₽ · №{int(receipt['gift']['serial']):05d}")
        self.offer_payment(chat_id, payment_id, amount, lines, source="бота")

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
            "Номер сохранён для заказов и связи по доставке." + self.cta("channel"),
            self.buyer_reply_keyboard(chat_id),
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
        ("Доставка", ("shipped",)),
        ("Завершены", ("received", "completed")),
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
        ]
        if payment is not None and payment["gift_product_id"] and not payment["gift_released_at"]:
            lines.append(f"Подарок в заказе: {esc(payment['gift_name'])} · 0 ₽" + (f" · №{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else ""))
        lines.append("")
        lines.extend(order_state_lines(str(row["status"]), payment, amount))
        keys = set(row.keys())
        has_snapshot = any(str(row[key] or "").strip() for key in ("customer_name", "city", "address", "entrance", "deliver") if key in keys)
        if has_snapshot:
            contact = str(row["phone"] or "").strip()
            if contact:
                lines.extend(["", f"{ICON['account']} Телефон: {esc(contact)}"])
            delivery = str(row["deliver"] or "").strip() if "deliver" in keys else ""
            address = " · ".join(
                str(row[key] or "").strip() for key in ("city", "address", "entrance")
                if key in keys and str(row[key] or "").strip()
            )
            if delivery:
                lines.append(f"{ICON['delivery']} Способ: {esc(delivery)}")
            if address:
                lines.append(f"{ICON['delivery']} Адрес: {esc(address)}")
        else:
            # Старые заказы хранили адрес одной строкой в note — не теряем его.
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
        if ONLINE_PAYMENTS_ENABLED and payable and payment_id:
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
        if payment["gift_product_id"] and not payment["gift_released_at"]:
            lines.append(f"• Подарок: {esc(payment['gift_name'])} · {int(payment['gift_qty'] or 1)} шт. · 0 ₽" + (f" · №{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else ""))
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
        self.db.expire_payments()
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
        if order_manager_username():
            ordering = "Выбери вещь и размер, открой чат и отправь подготовленный заказ."
        else:
            ordering = "Оформление временно недоступно: подключаем чат для заказов. Корзина в миниаппе сохраняется."
        return ("<b>КАК ЭТО РАБОТАЕТ</b>\n\n" + ordering
                + "\n\nВ карточке вещи есть размеры и сведения о материале. "
                  "Онлайн-оплата сейчас недоступна. Сроки и стоимость доставки уточняются.")

    def show_size_guide(self, chat_id: int, user_id: int, origin: str = "") -> None:
        """Сетка размеров. «Назад» ведёт туда, откуда экран открыли."""
        self.db.event(user_id, "size_guide_open", {"origin": origin[:40]})
        text = (
            "<b>ТАБЛИЦА РАЗМЕРОВ</b>\n\n"
            "Ширина изделия по груди / рост / ориентир по весу:\n\n"
            "<b>S</b> · 57 см · 165–176 см · 60–74 кг (грудь 88–96)\n"
            "<b>M</b> · 60 см · 174–182 см · 72–84 кг (грудь 96–104)\n"
            "<b>L</b> · 63 см · 180–188 см · 82–94 кг (грудь 104–112)\n"
            "<b>XL</b> · 66 см · 186–194 см · 92–106 кг (грудь 112–120)\n"
            "<b>2XL / XXL</b> · 69 см · 190–200 см · 102–118 кг (грудь 120–128)\n"
            "<b>3XL</b> · 72 см · 196–206 см · 114–136 кг (грудь 128–136)\n\n"
            "• <b>СИЛА И ЧЕСТЬ</b> — прямой свободный крой.\n"
            "• <b>ВЗГЛЯД</b> — прилегающий крой по фигуре (если хочешь свободнее, бери на размер выше)."
        )
        back: tuple[str, str] | None = None
        target_product: dict[str, Any] | None = None
        if origin.startswith("product:"):
            product = self.catalog.get_any(origin.split(":", 1)[1])
            if product:
                back = (str(product["name"]), origin)
                if product.get("active", True) and not product.get("gift"):
                    target_product = product
        elif origin.startswith("order_direct:"):
            product = self.catalog.get_any(origin.split(":", 1)[1])
            if product:
                back = ("Выбор размера", f"order_direct:{product['id']}")
                if product.get("active", True) and not product.get("gift"):
                    target_product = product
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
        if target_product is not None and not ONLINE_PAYMENTS_ENABLED and order_manager_username():
            pid = str(target_product["id"])
            sizes = [str(s) for s in target_product.get("sizes", [])]
            for index in range(0, len(sizes), 4):
                rows.append([(f"Выбрать {s}", f"msize:{pid}:{s}") for s in sizes[index:index + 4]])
        elif back is None:
            rows.append([(icon("catalog", "Каталог"), "catalog"), (icon("support", "Поддержка"), "support")])
        self.api.send_message(chat_id, text, inline_keyboard(rows + nav_rows(back)))

    def show_support(self, chat_id: int, user_id: int) -> None:
        self.db.event(user_id, "support_open")
        self.api.send_message(chat_id, self.support_text(), self.support_menu())

    def show_faq(self, chat_id: int, key: str, user_id: int | None = None) -> None:
        if key == "pay":
            rows = []
            if self.payment_question_url():
                rows.append([(icon("support", "Узнать об оплате ↗"), self.payment_question_url())])
            self.api.send_message(chat_id, "<b>КАК ОПЛАТИТЬ</b>\n\nУсловия оплаты согласуем в Telegram. Открой чат, проверь готовый вопрос и отправь его.", inline_keyboard(rows + nav_rows(("Поддержка", "support"))))
            return
        item = next((i for i in self.faq_items() if str(i.get("key")) == key), None)
        if not item:
            self.show_support(chat_id, user_id or chat_id)
            return
        self.api.send_message(
            chat_id,
            f"<b>{esc(str(item.get('label')).upper())}</b>\n\n{esc(str(item.get('answer')))}",
            inline_keyboard([
                [(icon("support", "← Поддержка"), "support")],
                nav_rows(("Главное меню", "menu"))[0],
            ]),
        )

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
                + (f"\n\nНапиши {esc(contact)} — номер покупки уже перед тобой."
                   if self.settings.support_username else
                   "\n\nПрямая связь с менеджером пока не подключена. Номер покупки сохранён; попробуй открыть поддержку позже."),
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
            "confirmed": ("Передать в доставку", "shipped"),
            "shipped": ("Отметить получение", "received"),
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
                    [[(f"{ICON['orders']} Открыть покупку №{order_id} →", f"ord:{order_id}")],
                     [(icon("support", "Написать менеджеру"), "support")]] + nav_rows()
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
        funnel = self.db.miniapp_funnel_stats()
        paid_amount = int(stats.get("paid_amount") or 0)
        sizes_line = (
            ", ".join(f"{esc(sz)} ×{cnt}" for sz, cnt in funnel["top_sizes"])
            if funnel["top_sizes"]
            else "пока нет выборов"
        )
        text = (
            "<b>СВОДКА МАГАЗИНА</b>\n\n"
            f"{ICON['pay']} Оплачено покупок: {stats['paid']}\n"
            f"{ICON['pay']} Получено: {esc(format_rub(paid_amount))}\n"
            f"{ICON['pay']} Ждут оплаты: {stats['awaiting_payment']}\n"
            f"{ICON['cancel']} Требуют возврата: {stats['refunds']}\n\n"
            f"{ICON['orders']} Покупок всего: {stats['orders']} · сегодня: {stats['orders_today']}\n"
            f"{ICON['wait']} В листе ожидания: {stats['waitlist']}\n"
            f"{ICON['trophy']} Приведено друзей: {stats['referrals']}\n\n"
            "<b>ВОРОНКА MINI APP</b>\n"
            f"• Открытий витрины: {funnel['opens']} · карточек: {funnel['product_views']}\n"
            f"• Сравнений кроя: {funnel['compares']} · комплектов: {funnel['bundles']} · сторис: {funnel['stories']}\n"
            f"• Переходов к менеджеру: {funnel['dm_clicks']} · заявок-страховок: {funnel['leads_total']} (подтв. {funnel['leads_confirmed']})\n"
            f"• Топ размеров: {sizes_line}\n"
            f"• Клуб «7 дней слова»: {funnel['club_members']} участн.\n\n"
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
            "WHERE user_id=? AND status IN ('paid', 'confirmed', 'shipped', 'received', 'completed')",
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
            inline_keyboard([[(icon("stats", "Сводка"), "adm:summary")],
                             [(icon("pay", "Ждут оплаты →"), "unpaid")]]
                            + staff_nav_rows()))

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
        ]
        if payment is not None and payment["gift_product_id"] and not payment["gift_released_at"]:
            lines.append(f"Подарок: {esc(payment['gift_name'])} · {int(payment['gift_qty'] or 1)} шт. · 0 ₽" + (f" · №{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else ""))
        lines.append("")
        lines.extend(order_state_lines(status, payment, int(row["amount_rub"] or 0)))
        keys = set(row.keys())
        has_snapshot = any(str(row[key] or "").strip() for key in ("city", "address", "entrance", "deliver") if key in keys)
        if has_snapshot:
            delivery = str(row["deliver"] or "").strip() if "deliver" in keys else ""
            address = " · ".join(
                str(row[key] or "").strip() for key in ("city", "address", "entrance")
                if key in keys and str(row[key] or "").strip()
            )
            if delivery:
                lines.append(f"{ICON['delivery']} Способ: {esc(delivery)}")
            if address:
                lines.append(f"{ICON['delivery']} Адрес: {esc(address)}")
        else:
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
        # Нав-ряд остаётся последним: клиент встаёт перед ним, а не поверх выхода.
        rows.insert(max(0, len(rows) - 1),
                    [{"text": icon("account", "Клиент →"),
                      "callback_data": f"aclient:{int(user_id)}"}])
        return {"inline_keyboard": rows}

    EDIT_FIELDS = {
        "price": ("Цену", "Цену? Формат как при добавлении: 4 900 или 4 900 - 5 200."),
        "sizes": ("Остатки", "Размеры в наличии через запятую? Например: S, M, L."),
        "desc": ("Описание", "Новое описание? Покупатель видит его в карточке целиком."),
    }

    def admin_edit_product(self, chat_id: int, product_id: str, saved: str = "") -> None:
        product = self.catalog.get(product_id) or next(
            (p for p in self.catalog.data.get("products", [])
             if str(p.get("id")) == product_id), None)
        if not product:
            self.api.send_message(chat_id, "Вещь не найдена — править нечего.")
            return
        sizes = ", ".join(str(size) for size in product.get("sizes") or [])
        description = " ".join(str(product.get("description") or "").split())
        self.api.send_message(
            chat_id,
            f"<b>ПРАВКА ВЕЩИ</b>\n\n{esc(str(product.get('name')))}\n"
            + (f"{ICON['ok']} {esc(saved)}\n" if saved else "")
            + f"\n{ICON['pay']} Цена: {esc(str(product.get('price')))}"
            + f"\n{ICON['size']} Размеры: {esc(sizes)}"
            + f"\n{ICON['receipt']} Описание: {esc(description[:120])}"
            + ("…" if len(description) > 120 else ""),
            inline_keyboard(
                [[(icon("pay", "Цена"), f"ped:{product_id}:price"),
                  (icon("size", "Остатки"), f"ped:{product_id}:sizes")],
                 [(icon("receipt", "Описание"), f"ped:{product_id}:desc")],
                 [(icon("catalog", "← Вещь"), f"product:{product_id}"),
                  (icon("stock", "Витрина"), "adm:shelf")]]
            ),
        )

    def handle_product_edit_text(self, chat_id: int, user_id: int, text: str) -> bool:
        """Один текстовый шаг правки: значение проверяем до записи в каталог."""
        state = self.db.get_state(user_id)
        if not state or state[0] != "prod_edit":
            return False
        pid = str(state[1].get("pid") or "")
        field = str(state[1].get("field") or "")
        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменили.", remove_keyboard())
            self.admin_edit_product(chat_id, pid)
            return True
        value = " ".join(text.split())
        if field == "sizes":
            sizes = [part.strip() for part in value.split(",") if part.strip()]
            if not sizes or len(sizes) > 8 or any(len(part) > 12 for part in sizes):
                self.api.send_message(
                    chat_id,
                    "Размеры — список через запятую, до 8 штук, каждый не длиннее 12 знаков.\n"
                    "Например: S, M, L, XL.",
                )
                return True
            change: dict[str, Any] = {"sizes": sizes}
            label = f"Размеры: {', '.join(sizes)}"
        elif field == "price":
            if not value or len(value) > 40:
                self.api.send_message(chat_id, "Цена — короткая строка: 4 900 или 4 900 - 5 200.")
                return True
            change = {"price": value}
            label = f"Цена: {value}"
        elif field == "desc":
            if not value or len(value) > 600:
                self.api.send_message(chat_id, "Описание — до 600 знаков: оно целиком в карточке.")
                return True
            change = {"description": value}
            label = "Описание обновлено"
        else:
            self.db.clear_state(user_id)
            return True
        try:
            ok = self.catalog.update_product(pid, **change)
        except (ValueError, OSError) as exc:
            self.catalog_refused(chat_id, "ПРАВКА НЕ СОХРАНИЛАСЬ", exc,
                                  "Файл каталога цел, витрина работает. Значение не записано.")
            return True
        if not ok:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Вещь не найдена — правка не сохранилась.")
            return True
        self.db.clear_state(user_id)
        self.db.event(user_id, "product_edited", {"product_id": pid, "field": field})
        self.admin_edit_product(chat_id, pid, label)
        return True

    def admin_unpaid(self, chat_id: int) -> None:
        """Брошенные оплаты: кого позвать доплатить, не листая покупки."""
        rows = self.db.payments_pending()
        lines = ["<b>ЖДУТ ОПЛАТЫ</b>"]
        buttons: list[list[tuple[str, str]]] = []
        if not rows:
            lines.append("\nВсе начатые оплаты закрыты — звать некого.")
        for row in rows:
            pid = str(row["payment_id"])
            stamp = self.db.kv_get(f"nudge_at:{pid}")
            lines.append(
                f"\n{ICON['pay']} Покупка №{int(row['order_id'])} · "
                f"{esc(str(row['names'])[:28])} · {format_rub(int(row['amount_rub']))}\n"
                f"Начата {self._ago(row['created_at'])} назад."
                + (f" Напомнили {self._ago(stamp)} назад." if stamp else "")
            )
            if len(buttons) < 4:
                buttons.append([(icon("channel", "Напомнить →"), f"nudge:{pid}")])
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(buttons + staff_nav_rows()))

    @staticmethod
    def _ago(stamp: str) -> str:
        try:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp))
        except ValueError:
            return "неизвестно"
        hours = max(0, int(delta.total_seconds() // 3600))
        if hours < 1:
            minutes = max(1, int(delta.total_seconds() // 60))
            return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"
        if hours < 24:
            return f"{hours} {plural(hours, 'час', 'часа', 'часов')}"
        days = hours // 24
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"

    def nudge_payment(self, chat_id: int, user_id: int, payment_id: str) -> None:
        stamp = self.db.kv_get(f"nudge_at:{payment_id}")
        if stamp:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
            if delta.total_seconds() < 12 * 3600:
                self.api.send_message(
                    chat_id,
                    f"Уже напомнили {self._ago(stamp)} назад.\n"
                    "Повторно — через 12 часов, чтобы не давить на человека.",
                    inline_keyboard([[(icon("pay", "← Ждут оплаты"), "unpaid")]]),
                )
                return
        rows = self.db.orders_for_payment(payment_id)
        if not rows:
            self.admin_unpaid(chat_id)
            return
        target = int(rows[0]["user_id"])
        lines = [f"• {esc(row['product_name'])} · {esc(row['size'])} · "
                 f"{int(row['quantity'] or 1)} шт." for row in rows]
        self.db.kv_set(f"nudge_at:{payment_id}", utc_now())
        self.db.event(target, "payment_nudge", {"payment_id": payment_id, "by": user_id})
        self.api.send_message(
            target,
            f"<b>ПОКУПКА №{int(rows[0]['id'])} ЖДЁТ ОПЛАТЫ</b>\n\n"
            + "\n".join(lines)
            + f"\n\n{esc(format_rub(int(rows[0]['amount_rub'])))}. Место в очереди и "
            "размеры держим за вами — оплата займёт минуту.",
            inline_keyboard(
                [[(icon("pay", "Оплатить →"), f"draft:{payment_id}")]]
                + [[(icon("orders", "Мои покупки"), "my_orders")]]
            ),
        )
        self.api.send_message(
            chat_id, "Напомнил покупателю: ушла карточка с кнопкой оплаты.",
            inline_keyboard([[(icon("pay", "← Ждут оплаты"), "unpaid")]]),
        )

    def admin_faqs(self, chat_id: int) -> None:
        """Набор быстрых ответов: правка и удаление по кнопке, добавление сверху."""
        items = self.faq_items()
        lines = ["<b>ОТВЕТЫ ПОДДЕРЖКИ</b>",
                 "\nЭти кнопки видит покупатель в экране поддержки."]
        rows: list[list[tuple[str, str]]] = []
        if not items:
            lines.append("\nНабор пуст — добавь первый ответ.")
        for item in items:
            lines.append(f"\n{ICON['question']} {esc(str(item.get('label')))}")
            if len(rows) < 4:
                rows.append([
                    (icon("tools", "Править"), f"faqedit:{item.get('key')}"),
                    (icon("cancel", "Убрать"), f"faqdel:{item.get('key')}"),
                ])
        rows.append([(icon("stock", "Добавить ответ"), "faqadd")])
        self.api.send_message(chat_id, "\n".join(lines),
                              inline_keyboard(rows + staff_nav_rows()))

    def announce_product(self, chat_id: int, user_id: int, product_id: str) -> None:
        """Анонс новинки: текст собирается из карточки и уходит в черновик рассылки."""
        product = self.catalog.get(product_id) or next(
            (p for p in self.catalog.data.get("products", [])
             if str(p.get("id")) == product_id), None)
        if not product:
            self.api.send_message(chat_id, "Вещь не найдена — анонс не собрался.")
            return
        sizes = ", ".join(str(size) for size in product.get("sizes") or []) or "размеры уточним"
        description = " ".join(str(product.get("description") or "").split())[:160]
        try:
            price_label = "БЕСПЛАТНО · в подарок" if product.get("gift") else format_rub(parse_price_strict(product.get("price")))
        except PriceError:
            price_label = str(product.get("price") or "цена уточняется")
        text = (f"Новинка: {product.get('name')}\n"
                f"{price_label}\n"
                f"Размеры: {sizes}\n"
                + (f"{description}\n" if description else "")
                + "\nСмотри в витрине — тираж маленький.")
        self.db.set_state(user_id, "broadcast_pending", {"text": text})
        self.ask_broadcast_segment(chat_id, text)

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
                f"{esc(str(row['product_name'])[:24])} · {format_rub(int(row['refund_amount_rub'] or row['amount_rub']))}"
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
                             f"{format_rub(int(row['refund_amount_rub'] or row['amount_rub']))} · {esc(reason)}")
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
        elif command == "/unpaid":
            self.admin_unpaid(chat_id)
        elif command == "/faqs":
            self.admin_faqs(chat_id)
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

    def handle_faq_text(self, chat_id: int, user_id: int, text: str) -> bool:
        """Шаги набора ответов: название и текст ответа, отмена словом «отмена»."""
        state = self.db.get_state(user_id)
        if not state or state[0] not in ("faq_add", "faq_edit"):
            return False
        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменили.", remove_keyboard())
            self.admin_faqs(chat_id)
            return True
        mode, data = state[0], dict(state[1])
        step = str(data.get("step") or "label")
        value = " ".join(text.split())[:160]
        if not value:
            self.api.send_message(chat_id, "Пусто не сохраняю — напиши текст.")
            return True
        items = self.faq_items()
        if mode == "faq_add":
            if step == "label":
                self.db.set_state(user_id, "faq_add", {**data, "step": "answer", "label": value})
                self.api.send_message(chat_id, f"Текст ответа «{esc(value)}»? Покупатель увидит его целиком.")
                return True
            seq = int(self.db.kv_get("faq_seq") or 0) + 1
            self.db.kv_set("faq_seq", str(seq))
            items.append({"key": f"faq-{seq}", "label": str(data.get("label")), "answer": value})
            self.save_faq_items(items)
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, f"Ответ «{esc(str(data.get('label')))}» в наборе.")
            self.admin_faqs(chat_id)
            return True
        key = str(data.get("key") or "")
        target = next((i for i in items if str(i.get("key")) == key), None)
        if not target:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Этого ответа уже нет в наборе.")
            self.admin_faqs(chat_id)
            return True
        if step == "label":
            self.db.set_state(user_id, "faq_edit", {**data, "step": "answer", "label": value})
            self.api.send_message(chat_id, f"Новый текст ответа для «{esc(value)}»?")
            return True
        target["label"] = str(data.get("label"))
        target["answer"] = value
        self.save_faq_items(items)
        self.db.clear_state(user_id)
        self.api.send_message(chat_id, f"Ответ «{esc(target['label'])}» обновлён.")
        self.admin_faqs(chat_id)
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
                 [(icon("channel", "Анонс →"), f"announce:{product['id']}")],
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
        if receipt.get("gift"):
            order_lines.append(f"• Подарок: {esc(receipt['gift']['name'])} · 1 шт. · 0 ₽ · №{int(receipt['gift']['serial']):05d}")
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
        if not ONLINE_PAYMENTS_ENABLED:
            return {"ok": False, "error": "Онлайн-оплата временно недоступна. Оформи заказ через чат магазина."}
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
            gift=self.gift_for_checkout(),
            stock_totals=size_stock_totals([item[0] for item in valid_items]),
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
        self.db.expire_payments()
        payment = self.db.get_payment(payment_id)
        if not payment or int(payment["user_id"]) != int(user_id):
            return {"ok": False, "error": "Оплата не найдена."}
        if not self.db.payment_is_payable(payment_id):
            return {"ok": False, "error": "Оплата уже неактуальна. Проверь мои заказы."}
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
            "gift": ({
                "product_id": payment["gift_product_id"], "name": payment["gift_name"],
                "quantity": int(payment["gift_qty"] or 1),
                "serial": int(payment["gift_serial"]) if payment["gift_serial"] is not None else None,
                "serial_label": f"{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else "",
            } if payment["gift_product_id"] and not payment["gift_released_at"] else None),
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
        except GiftUnavailable as exc:
            return {"ok": False, "error": str(exc), "code": "gift_unavailable"}
        except SizeUnavailable as exc:
            return {"ok": False, "error": str(exc), "code": "size_unavailable"}
        except (ValueError, PriceError) as exc:
            return {"ok": False, "error": str(exc)}

    def _checkout_web_payload(
        self,
        user: dict[str, Any],
        payload: dict[str, Any],
        notify_user: int | None = None,
    ) -> dict[str, Any]:
        if payload.get("consent") is not True:
            return {"ok": False, "error": "Без согласия на обработку данных заказ принять нельзя."}
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        phone = normalize_phone(str(customer.get("phone", "")))
        if not phone:
            return {"ok": False, "error": "Проверь номер телефона в витрине и отправь заказ ещё раз."}
        deliver = str(customer.get("deliver") or "СДЭК").strip()
        if deliver not in {"СДЭК", "Яндекс"}:
            return {"ok": False, "error": "Выбери доставку СДЭК или Яндекс. Офлайн-магазина и самовывоза нет."}
        customer = {**customer, "deliver": deliver}
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 20:
            return {"ok": False, "error": "В заказе нет вещей или их слишком много. Проверь корзину."}
        valid_items: list[tuple[dict[str, Any], str, int, str]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return {"ok": False, "error": "Некорректная позиция в заказе. Обнови корзину и попробуй снова."}
            product = self.catalog.get(str(raw_item.get("product_id", "")))
            size = str(raw_item.get("size", ""))
            if not product or product.get("gift") or size not in {str(item) for item in product.get("sizes", [])}:
                return {"ok": False, "error": "Одна из вещей или размеров уже недоступна. Обнови корзину: заказ не создан."}
            quantity = raw_item.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 20:
                return {"ok": False, "error": "Количество в каждой позиции должно быть целым числом от 1 до 20."}
            person = sanitize_personalization(product, raw_item.get("person"))
            valid_items.append((product, size, quantity, person))
        if not valid_items:
            return {"ok": False, "error": "Некоторые вещи уже закончились. Обнови витрину и выбери снова."}
        for product, _, quantity, _ in valid_items:
            self.line_amount(product, quantity)
        with self.catalog.lock:
            drop_event = dict(self.catalog.data.get("drop_event") or {})
        gate = drop_gate_error(
            drop_event,
            bool(self.db.club_state(int(user["id"]))["early_access"]),
        )
        if gate:
            return {"ok": False, "error": gate, "code": "drop_closed"}
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
    NON_EDITABLE_CALLBACKS = ("pay:", "pvid:", "teaser")

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
        if not ONLINE_PAYMENTS_ENABLED and data.startswith(("pay:", "want:", "size:", "draft:")):
            if data.startswith("want:"):
                self.choose_manager_size(chat_id, user_id, data.split(":", 1)[1])
            else:
                self.show_faq(chat_id, "pay", user_id)
            return True
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
            t_rows: list[list[tuple[str, str]]] = []
            sila = self.catalog.get("tee-sila-i-chest")
            vzgl = self.catalog.get("tee-vzglyad")
            pair: list[tuple[str, str]] = []
            if sila:
                pair.append((f"▪ Сила и честь · {sila.get('price', '4 200 ₽')}", "product:tee-sila-i-chest"))
            if vzgl:
                pair.append((f"▪ Взгляд · {vzgl.get('price', '3 900 ₽')}", "product:tee-vzglyad"))
            if pair:
                t_rows.append(pair)
            else:
                t_rows.append([(icon("catalog", "Каталог"), "catalog")])
            t_rows.extend(nav_rows())
            keyboard = inline_keyboard(t_rows)
            if not self.send_teaser(chat_id, keyboard=keyboard):
                self.api.send_message(
                    chat_id,
                    "Ролик сейчас не открывается. Загляни в витрину — там он лежит целиком."
                    + self.cta("drop"),
                    keyboard,
                )
        elif data.startswith("pvid:"):
            self.send_product_video(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("order_direct:"):
            self.choose_manager_size(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("msize:"):
            parts = data.split(":", 2)
            if len(parts) < 3:
                return False
            self.open_manager_order(chat_id, user_id, parts[1], parts[2])
        elif data == "about":
            self.show_about(chat_id)
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
        elif data.startswith("faq:") and data != "faqadd":
            key = data.split(":", 1)[1]
            self.show_faq(chat_id, key, user_id)
            return True
        elif data == "faqadd" and self.is_admin(user_id):
            self.db.set_state(user_id, "faq_add", {"step": "label"})
            self.api.send_message(chat_id, "Название кнопки? Например: «Доставка».")
            return True
        elif data.startswith("faqedit:") and self.is_admin(user_id):
            key = data.split(":", 1)[1]
            item = next((i for i in self.faq_items() if str(i.get("key")) == key), None)
            if not item:
                self.admin_faqs(chat_id)
                return True
            self.db.set_state(user_id, "faq_edit", {"key": key, "step": "label"})
            self.api.send_message(
                chat_id,
                f"Новое название кнопки для ответа? Сейчас: {esc(str(item.get('label')))}.",
            )
            return True
        elif data.startswith("faqdel:") and self.is_admin(user_id):
            key = data.split(":", 1)[1]
            items = [i for i in self.faq_items() if str(i.get("key")) != key]
            self.save_faq_items(items)
            self.admin_faqs(chat_id)
            return True
        elif data.startswith("eedit:") and self.is_admin(user_id):
            self.admin_edit_product(chat_id, data.split(":", 1)[1])
            return True
        elif data.startswith("ped:") and self.is_admin(user_id):
            _, pid, field = (data.split(":") + ["", "", ""])[:3]
            if field in self.EDIT_FIELDS:
                self.db.set_state(user_id, "prod_edit", {"pid": pid, "field": field})
                self.api.send_message(chat_id, self.EDIT_FIELDS[field][1])
            return True
        elif data == "unpaid" and self.is_admin(user_id):
            self.admin_unpaid(chat_id)
            return True
        elif data.startswith("nudge:") and self.is_admin(user_id):
            self.nudge_payment(chat_id, user_id, data.split(":", 1)[1])
            return True
        elif data.startswith("announce:") and self.is_admin(user_id):
            self.announce_product(chat_id, user_id, data.split(":", 1)[1])
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
            if self.db.mark_payment_refunded(pid, actor_id=user_id):
                self.admin_refunds(chat_id)
            else:
                self.api.send_message(
                    chat_id,
                    "Возврат не отмечен: оплата либо уже закрыта, либо не требовала возврата. Обновляю список.",
                )
                self.admin_refunds(chat_id)
            return True
        elif data.startswith("wnotify:") and self.is_admin(user_id):
            parts = data.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                return False
            self.restock_reply(chat_id, parts[1], parts[2])
        elif data.startswith("lead_confirm:") and (self.is_admin(user_id) or chat_id == self.settings.manager_chat_id):
            try:
                lead_id = int(data.split(":", 1)[1])
            except ValueError:
                return False
            self.confirm_manager_lead(chat_id, lead_id)
        elif data.startswith("comm_pub:") and (self.is_admin(user_id) or chat_id == self.settings.manager_chat_id):
            parts = data.split(":", 2)
            if len(parts) != 3:
                return False
            try:
                sub_id = int(parts[1])
            except ValueError:
                return False
            self.publish_community_submission(chat_id, sub_id, parts[2])
        elif data.startswith("comm_rej:") and (self.is_admin(user_id) or chat_id == self.settings.manager_chat_id):
            try:
                sub_id = int(data.split(":", 1)[1])
            except ValueError:
                return False
            self.reject_community_submission(chat_id, sub_id)
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

    def handle_reply_button(self, chat_id: int, user_id: int, text: str) -> bool:
        """Обработка кнопок постоянного меню покупателя (ReplyKeyboardMarkup)."""
        raw = text.strip()
        lower = raw.lower()
        clean = re.sub(r'^[^\w\s]+', '', raw).strip().lower()

        # 1. Открытие витрины (если веб-клиент прислал текст вместо native WebApp)
        if clean in {"открыть витрину", "витрина", "открыть витрину ↗", "открыть приложение", "приложение", "миниапп", "магазин"}:
            self.db.clear_state(user_id)
            if self.settings.webapp_url.startswith("https://"):
                self.api.send_message(
                    chat_id,
                    "<b>ВИТРИНА</b>\n\nОткрой онлайн-витрину по кнопке ниже:",
                    inline_keyboard([[(f"Открыть витрину {ICON['outside']}", f"webapp:{self.settings.webapp_url}")]])
                )
            else:
                self.show_catalog(chat_id, user_id)
            return True

        # 2. Первый дроп
        if clean in {"первый дроп", "дроп", "вещи"}:
            self.db.clear_state(user_id)
            self.show_category(chat_id, user_id, "drop")
            return True

        # 2.1 Каталог / Витрина
        if clean in {"каталог", "витрина каталога", "товары"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "⚔️ Меню обновлено на «СИЛА И ЧЕСТЬ».", self.buyer_reply_keyboard(chat_id))
            self.show_catalog(chat_id, user_id)
            return True

        # 2.2 Канал бренда
        if clean in {"канал", "канал бренда"}:
            self.db.clear_state(user_id)
            link = self.channel_link()
            if link:
                self.api.send_message(
                    chat_id,
                    f"<b>КАНАЛ БРЕНДА</b>\n\nВсе новости, дропы и закрытые анонсы выходят в нашем канале.\n\nПодписывайся: {link}",
                    inline_keyboard(self.channel_rows() + nav_rows(("Главное меню", "menu"))),
                )
            else:
                self.api.send_message(chat_id, "Канал бренда скоро появится.", inline_keyboard(nav_rows(("Главное меню", "menu"))))
            return True

        # 2.3 О бренде
        if clean in {"о бренде", "бренд", "о нас"}:
            self.db.clear_state(user_id)
            self.show_about(chat_id)
            return True

        # 2.4 Размеры и сетка
        if clean in {"размеры", "размер", "таблица размеров", "замеры", "сетка"}:
            self.db.clear_state(user_id)
            self.show_size_guide(chat_id, user_id)
            return True

        # 2.5 Доставка и сроки
        if clean in {"доставка", "сроки", "сроки доставки", "как доставляете", "сдэк", "яндекс", "куда доставка"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id,
                "<b>ДОСТАВКА</b>\n\n"
                "Отправляем СДЭК и Яндексом. Сборка заказа — 1–2 дня; "
                "для предзаказа — после готовности вещи. Стоимость и срок доставки согласуем при заказе.",
                inline_keyboard([[(icon("catalog", "К вещам"), "cat:drop")]] + nav_rows(("Главная", "menu"))))
            return True

        # 2.6 Отзывы и образы
        if clean in {"отзывы", "отзыв", "где отзывы", "фото покупателей"}:
            self.db.clear_state(user_id)
            self.api.send_message(
                chat_id,
                "<b>ОТЗЫВЫ И ФОТО ПОКУПАТЕЛЕЙ</b>\n\n"
                "Живые фотографии посадки, распаковки и отзывы ребят публикуются в нашем канале бренда и в комментариях.\n\n"
                "Смотри образы в лукбуке или переходи в канал:",
                inline_keyboard([
                    [(icon("lookbook", "Смотреть лукбук"), "lookbook")],
                    *self.channel_rows(),
                    nav_rows(("Главное меню", "menu"))[0]
                ])
            )
            return True

        # 2.7 Материалы и качество
        if clean in {"качество", "материал", "материалы", "состав", "хлопок", "ткань", "стирка", "как стирать"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id,
                "<b>МАТЕРИАЛ И УХОД</b>\n\n"
                "Футболка «СИЛА И ЧЕСТЬ»: 95% хлопок, 5% лайкра; плотность 240 г/м². "
                "Состав других вещей смотри в их карточках.\n\n"
                "Стирай вещь вывернутой наизнанку на деликатном режиме до 30 °C. "
                "Суши естественным способом и не гладь по принту. Если ярлык содержит другие указания, следуй ему.",
                inline_keyboard([[(icon("catalog", "К вещам"), "cat:drop")]] + nav_rows(("Главная", "menu"))))
            return True

        # 3. Мои покупки
        if clean in {"мои покупки", "покупки", "мои заказы", "заказы", "статус", "статус заказа", "где заказ", "где покупка", "трек", "трек-номер"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "📦 Твои покупки:", self.buyer_reply_keyboard(chat_id))
            self.show_my_orders(chat_id, user_id)
            return True

        # 4. Фильм выпуска / Ролик / Тизер
        if clean in {"фильм выпуска", "тизер выпуска", "фильм", "ролик", "видео", "тизер"}:
            self.db.clear_state(user_id)
            self.db.event(user_id, "teaser_open")
            self.api.send_message(
                chat_id,
                "⚔️ <b>СИЛА И ЧЕСТЬ</b>\nОбновили меню чата на актуальное.",
                self.buyer_reply_keyboard(chat_id),
            )
            keyboard = inline_keyboard([[(icon("catalog", "Первый дроп"), "drop")]] + nav_rows())
            if not self.send_teaser(chat_id, keyboard=keyboard):
                self.api.send_message(
                    chat_id,
                    "Ролик сейчас не открывается. Загляни в витрину — там он лежит целиком."
                    + self.cta("drop"),
                    keyboard,
                )
            return True

        # 5. Образы / Лукбук
        if clean in {"образы", "лукбук", "фото"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "⚔️ Меню обновлено на «СИЛА И ЧЕСТЬ».", self.buyer_reply_keyboard(chat_id))
            self.show_lookbook(chat_id, user_id)
            return True

        # 6. Личный кабинет / Профиль
        if clean in {"личный кабинет", "кабинет", "профиль"}:
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, self.account_text(user_id), self.account_menu(user_id))
            return True

        # 7. Поддержка
        if clean in {"поддержка", "помощь", "менеджер", "связь", "помоги", "не работает", "не загружается", "проблема", "ошибка"}:
            self.db.clear_state(user_id)
            self.show_support(chat_id, user_id)
            return True

        # 7.1 Эмодзи-пак
        if clean in {"эмодзи", "эмодзи-пак", "пак", "стикеры"}:
            self.db.clear_state(user_id)
            self.show_emoji_pack(chat_id)
            return True

        # 7.2 Оплата
        if clean in {"оплата", "как оплатить", "способы оплаты"}:
            self.db.clear_state(user_id)
            self.show_faq(chat_id, "pay")
            return True

        # 7.3 Возврат
        if clean in {"возврат", "обмен", "как вернуть"}:
            self.db.clear_state(user_id)
            self.show_faq(chat_id, "refund")
            return True

        # 8. Управление магазином (админ)
        if clean in {"управление магазином", "управление", "админка", "панель"} and self.is_admin(user_id):
            self.db.clear_state(user_id)
            self.admin_command(chat_id, user_id, "/panel")
            return True

        return False

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
        # Админ-команда /setvoice (ответом на голосовое или аудио) и /clearvoice
        if text in ("/setvoice", "/clearvoice") and self.is_admin(user_id):
            self.handle_founder_voice_command(chat_id, message, clear=(text == "/clearvoice"))
            return
        # Приём фото покупателя в раздел «На своих»
        current_state = self.db.get_state(user_id)
        if current_state and current_state[0] == "awaiting_community_photo" and message.get("photo"):
            self.receive_community_photo(chat_id, user, message)
            return
        if self.db.get_state(user_id) and self.handle_add_product_text(chat_id, user_id, text):
            return
        if self.handle_faq_text(chat_id, user_id, text):
            return
        if self.handle_product_edit_text(chat_id, user_id, text):
            return
        if text.startswith("/") and self.admin_command(chat_id, user_id, text):
            return
        if text.startswith("/") and self.user_command(chat_id, user_id, text):
            return
        if text.lower() in ("отмена", "cancel"):
            self.db.clear_state(user_id)
            self.api.send_message(chat_id, "Отменили.", self.buyer_reply_keyboard(chat_id))
            self.send_menu(chat_id)
            return
        if self.handle_reply_button(chat_id, user_id, text):
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
            self.buyer_reply_keyboard(chat_id),
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
        if name == "awaiting_community_photo":
            self.api.send_message(
                chat_id,
                "Жду именно <b>фотографию</b> (не как файл, а обычным фото) с подписью города и размера.\n"
                "Для выхода напиши <code>отмена</code>.",
            )
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
        elif command in {"/emoji", "/emojipack", "/stickers"}:
            self.show_emoji_pack(chat_id)
        elif command == "/help":
            self.api.send_message(
                chat_id, self.help_text(), inline_keyboard(nav_rows(("Поддержка", "support")))
            )
        else:
            return False
        return True

    def handle_founder_voice_command(self, chat_id: int, message: dict[str, Any], clear: bool = False) -> None:
        if clear:
            with self.catalog.lock:
                data = dict(self.catalog.data)
                media = dict(data.get("media") or {})
                media.pop("founder_voice", None)
                data["media"] = media
                self.catalog.save(data)
            self.api.send_message(chat_id, "Голосовое обращение основателя убрано из манифеста.")
            return
        reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        voice_obj = reply.get("voice") or reply.get("audio")
        if not isinstance(voice_obj, dict) or not voice_obj.get("file_id"):
            self.api.send_message(
                chat_id,
                "Ответь командой <code>/setvoice</code> на голосовое сообщение (или аудио) — "
                "оно сразу появится в блоке «Слово основателя» в Mini App.\n"
                "Убрать запись: <code>/clearvoice</code>.",
            )
            return
        ext = ".ogg" if reply.get("voice") else ".mp3"
        rel_path = f"assets/audio/founder-voice{ext}"
        dest = BASE_DIR / "miniapp" / rel_path
        if not self.api.download_file(str(voice_obj["file_id"]), dest, max_bytes=8 * 1024 * 1024):
            self.api.send_message(chat_id, "Не удалось скачать аудиофайл из Telegram.")
            return
        with self.catalog.lock:
            data = dict(self.catalog.data)
            media = dict(data.get("media") or {})
            media["founder_voice"] = rel_path
            data["media"] = media
            self.catalog.save(data)
        self.api.send_message(chat_id, "Готово — голосовое слово основателя подключено в Mini App.")

    def receive_community_photo(self, chat_id: int, user: dict[str, Any], message: dict[str, Any]) -> None:
        photos = message.get("photo")
        if not isinstance(photos, list) or not photos:
            return
        best = photos[-1]
        file_id = str(best.get("file_id") or "")
        if not file_id:
            return
        caption = str(message.get("caption") or "").strip()
        sub_id = self.db.create_community_submission(user, file_id, caption)
        self.db.clear_state(int(user["id"]))
        self.api.send_message(
            chat_id,
            "<b>КАДР ПРИНЯТ НА ПРОВЕРКУ</b>\n\n"
            "Спасибо, свой. Как только модератор одобрит фото, пришлём уведомление, "
            "и образ сразу появится в галерее «На своих».",
            self.buyer_reply_keyboard(chat_id),
        )
        targets = set(self.settings.admin_ids)
        if self.settings.manager_chat_id:
            targets.add(self.settings.manager_chat_id)
        who = f"@{user['username']}" if user.get("username") else str(user.get("first_name") or user["id"])
        mod_caption = (
            f"<b>ФОТО В «НА СВОИХ» · ЗАЯВКА №{sub_id}</b>\n"
            f"От: {esc(who)} (ID <code>{int(user['id'])}</code>)\n"
            + (f"Подпись: {esc(caption)}\n" if caption else "Подпись не указана\n")
            + "\nВыбери модель, чтобы сразу опубликовать кадр в Mini App:"
        )
        markup = inline_keyboard([
            [("✅ В «Свои» · СИЛА И ЧЕСТЬ", f"comm_pub:{sub_id}:tee-sila-i-chest")],
            [("✅ В «Свои» · ВЗГЛЯД", f"comm_pub:{sub_id}:tee-vzglyad")],
            [("✖ Отклонить", f"comm_rej:{sub_id}")],
        ])
        for target in targets:
            try:
                self.api.send_photo(target, file_id, mod_caption, markup)
            except Exception as exc:
                LOG.warning("Could not send community submission %s to %s: %s", sub_id, target, exc)

    def publish_community_submission(self, chat_id: int, sub_id: int, product_id: str) -> None:
        sub = self.db.get_community_submission(sub_id)
        if not sub:
            self.api.send_message(chat_id, "Заявка не найдена.")
            return
        if str(sub["status"]) == "published":
            self.api.send_message(chat_id, f"Заявка №{sub_id} уже опубликована.")
            return
        rel = f"assets/community/look-{sub_id}-{int(time.time())}.jpg"
        dest = BASE_DIR / "miniapp" / rel
        if not self.api.download_file(str(sub["file_id"]), dest):
            self.api.send_message(chat_id, "Не удалось сохранить фото с серверов Telegram.")
            return
        product = self.catalog.get_any(product_id)
        raw_cap = str(sub["caption"] or "").strip()
        city = str(sub["first_name"] or "СВОЙ ЧЕЛОВЕК").upper()
        fit_note = raw_cap[:140] if raw_cap else "Образ покупателя первого выпуска"
        entry = {
            "image": rel,
            "name": str(sub["first_name"] or "Участник дропа"),
            "caption": str((product or {}).get("name") or "СИЛА И ЧЕСТЬ"),
            "city": city,
            "fit_note": fit_note,
            "product_id": product_id if product else "tee-sila-i-chest",
        }
        with self.catalog.lock:
            data = dict(self.catalog.data)
            comm = list(data.get("community") or [])
            comm.insert(0, entry)
            data["community"] = comm[:24]
            self.catalog.save(data)
        self.db.set_community_submission_status(sub_id, "published", product_id)
        self.api.send_message(chat_id, f"✅ Кадр №{sub_id} опубликован в разделе «На своих» Mini App.")
        try:
            self.api.send_message(
                int(sub["user_id"]),
                "<b>ТВОЙ КАДР В «НА СВОИХ» ✓</b>\n\n"
                "Модератор одобрил фото — оно уже в живой галерее Mini App СИЛА И ЧЕСТЬ.",
            )
        except Exception:
            pass

    def reject_community_submission(self, chat_id: int, sub_id: int) -> None:
        sub = self.db.get_community_submission(sub_id)
        if not sub:
            self.api.send_message(chat_id, "Заявка не найдена.")
            return
        self.db.set_community_submission_status(sub_id, "rejected")
        self.api.send_message(chat_id, f"Заявка №{sub_id} отклонена.")

    def confirm_manager_lead(self, chat_id: int, lead_id: int) -> None:
        lead = self.db.get_lead(lead_id)
        if not lead:
            self.api.send_message(chat_id, "Заявка не найдена.")
            return
        if str(lead["status"]) == "confirmed" and lead["order_id"]:
            self.api.send_message(chat_id, f"Заявка №{lead_id} уже подтверждена (Заказ №{int(lead['order_id'])}).")
            return
        uid = lead["user_id"]
        if not uid:
            self.db.mark_lead_confirmed(lead_id, 0)
            self.api.send_message(chat_id, f"Заявка №{lead_id} отмечена подтверждённой (клиент открывал витрину из браузера без Telegram ID).")
            return
        try:
            items = json.loads(str(lead["items_json"] or "[]"))
        except Exception:
            items = []
        created_ids: list[int] = []
        for idx, item in enumerate(items if isinstance(items, list) and items else [{"id": "tee-sila-i-chest", "size": "L", "qty": 1}]):
            prod = self.catalog.get_any(str(item.get("id") or "tee-sila-i-chest")) or {
                "id": str(item.get("id") or "tee-sila-i-chest"),
                "name": str(item.get("name") or "СИЛА И ЧЕСТЬ"),
                "price": "4 200 ₽",
            }
            qty = max(1, int(item.get("qty") or 1))
            try:
                unit_rub = parse_price_strict(prod.get("price"))
            except Exception:
                unit_rub = 0
            oid, _ = self.db.create_order(
                request_id=f"lead-{lead_id}-{idx}",
                user_id=int(uid),
                product=prod,
                size=str(item.get("size") or "L"),
                phone=str(lead["phone"] or ""),
                quantity=qty,
                note=str(lead["gift_wrap"] or ""),
                status="confirmed",
                amount_rub=unit_rub * qty,
            )
            created_ids.append(oid)
        first_oid = created_ids[0] if created_ids else 0
        self.db.mark_lead_confirmed(lead_id, first_oid)
        nums = ", ".join(f"№{i}" for i in created_ids)
        self.api.send_message(
            chat_id,
            f"✅ Заявка №{lead_id} зарегистрирована ({nums}). Покупателю открыта цифровая «Карта владельца».",
        )
        try:
            self.api.send_message(
                int(uid),
                f"<b>ЗАКАЗ {nums} ПОДТВЕРЖДЁН ✓</b>\n\n"
                "Менеджер зарегистрировал твой заказ в реестре выпуска 001. "
                "В профиле Mini App теперь доступна именная <b>«Карта владельца»</b>.",
            )
        except Exception:
            pass

    def flush_club_reminders(self) -> None:
        now_msk = club_now()
        today = now_msk.date().isoformat()
        hour = now_msk.hour
        due_users = self.db.due_club_reminders(today, hour)
        if not due_users:
            return
        bot_uname = str(getattr(self, "bot_username", "") or self.db.kv_get("bot_username") or "SilaIChestBot")
        club_url = f"https://t.me/{bot_uname}?startapp=club"
        markup = {"inline_keyboard": [[{"text": "⚔ Отметить день в клубе", "url": club_url}]]}
        for uid in due_users:
            state = self.db.club_state(uid)
            day_num = min(int(state.get("total") or 0) + 1, 7)
            self.db.enqueue_message(
                f"club-rem:{uid}:{today}",
                uid,
                f"<b>7 ДНЕЙ СЛОВА · ДЕНЬ 0{day_num}</b>\n\n"
                "Дисциплина проверяется без зрителей. Закрой сегодняшнее задание и поставь отметку в клубе.",
                markup,
            )

    def handle_inline_query(self, iq: dict[str, Any]) -> None:
        qid = str(iq.get("id") or "")
        if not qid:
            return
        query = str(iq.get("query") or "").strip().lower()
        origin = self.settings.public_origin() or "https://sila.theatralka.ru"
        bot_uname = str(getattr(self, "bot_username", "") or self.db.kv_get("bot_username") or "SilaIChestBot")
        cards = [
            (
                "tee-sila-i-chest",
                "Футболка «СИЛА И ЧЕСТЬ» · 4 200 ₽",
                "Прямой свободный крой · Хлопок пенье 250 г/м² · Клинок НВ вдоль спины",
                f"{origin}/assets/client/sila-collage-20261111c.jpg",
                f"<b>СИЛА И ЧЕСТЬ · ВЫПУСК 001</b>\nПрямой свободный крой · Хлопок 250 г/м² · 4 200 ₽\nСтальной жетон № 00001–00100 — в подарок первой сотне.",
            ),
            (
                "tee-vzglyad",
                "Футболка «ВЗГЛЯД» · 3 900 ₽",
                "Прилегающий атлетический крой · 10 принципов на спине · Предзаказ",
                f"{origin}/assets/client/vzglyad-collage-20261111c.jpg",
                f"<b>ВЗГЛЯД · ВЫПУСК 001</b>\nПрилегающий атлетический крой · Хлопок 250 г/м² · 3 900 ₽\nПредзаказ первого дропа бренда Никиты Ворожбитова.",
            ),
            (
                "tag-sila-i-chest",
                "Стальной жетон № 00001–00100 · Подарок",
                "Шлифованная сталь 2 мм на шариковой цепи · Не продаётся отдельно",
                f"{origin}/assets/client/tag-collage-20261111c.jpg",
                f"<b>ЗНАК СВОИХ · ЖЕТОН ПЕРВОЙ СОТНИ</b>\nШлифованная сталь 2 мм с гравировкой номера · 0 ₽ к заказу из первой сотни.",
            ),
            (
                "drop",
                "Витрина «СИЛА И ЧЕСТЬ» · Первый дроп 001",
                "Две футболки. Два характера. Одежда Никиты Ворожбитова.",
                f"{origin}/assets/client/og-preview-20261112c.jpg",
                f"<b>СИЛА И ЧЕСТЬ · ПЕРВЫЙ ДРОП 001</b>\nДве футболки. Два характера. Первый выпуск бренда Никиты Ворожбитова.",
            ),
        ]
        results = []
        for cid, title, desc, photo_url, cap in cards:
            if query and query not in title.lower() and query not in desc.lower():
                continue
            start_arg = cid if cid != "drop" else ""
            deep = f"https://t.me/{bot_uname}" + (f"?startapp={start_arg}" if start_arg else "")
            results.append({
                "type": "photo",
                "id": cid,
                "photo_url": photo_url,
                "thumbnail_url": photo_url,
                "title": title,
                "description": desc,
                "caption": cap,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[{"text": "Открыть в СИЛА И ЧЕСТЬ ↗", "url": deep}]]},
            })
        try:
            self.api.call("answerInlineQuery", {"inline_query_id": qid, "results": results[:10], "cache_time": 60})
        except Exception as exc:
            LOG.warning("answerInlineQuery failed: %s", exc)

    def handle_update(self, update: dict[str, Any]) -> bool:
        try:
            if "pre_checkout_query" in update:
                self.handle_pre_checkout(update["pre_checkout_query"])
            elif "inline_query" in update:
                self.handle_inline_query(update["inline_query"])
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
    server_version = "SilaChest/1"
    sys_version = ""
    # StreamRequestHandler applies this to the socket: a client that connects
    # and never finishes the request must not hold a worker thread forever.
    timeout = 30

    # Витрина живёт внутри Telegram и подключает только свои файлы плюс SDK
    # telegram.org. CSP — второй рубеж: в app.js много innerHTML, и если где-то
    # однажды забудут escapeHTML, инлайновый скрипт всё равно не выполнится.
    CSP_BASE = (
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://*.telegram.org https://telegram.org; "
        "media-src 'self'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    # Боевой режим пускает витрину в рамку Telegram и разрешает витрине ('self')
    # встраивать собственный 3D-обзор (assets/vzglyad-3d/index-*.html).
    CSP_FRAME_TELEGRAM = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"

    def _csp(self) -> str:
        settings = self.settings
        if settings and settings.token:
            return f"{self.CSP_BASE}; {self.CSP_FRAME_TELEGRAM}"
        return self.CSP_BASE

    _COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")

    def _write(self, body: bytes, content_type: str, status: int = 200, cache_control: str = "no-cache", etag: str = "") -> None:
        encoding = ""
        if (
            status == 200
            and len(body) > 1024
            and content_type.startswith(self._COMPRESSIBLE)
            and "gzip" in (self.headers.get("Accept-Encoding") or "")
        ):
            body = gzip.compress(body, compresslevel=6, mtime=0)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        if etag and not encoding:
            self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self._csp())
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _write_file(self, path: Path, content_type: str, cache_control: str, size: int, etag: str) -> None:
        """Stream a large static file instead of loading it whole into RAM."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self._csp())
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile, 256 * 1024)

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
        if length < 0 or length > 32768:
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
            self._json({"error": "Открой витрину из бота — тогда подтянутся заказы."}, 401)
            return
        if not self._rate_ok(READ_LIMITER, "my-orders", user):
            return
        self.db.expire_payments()
        rows = self.db.orders_for_user(int(user["id"]), 20)
        orders = []
        seen_gift_payments: set[str] = set()
        for row in rows:
            order = public_order_payload(row)
            payment = None
            if row["payment_id"]:
                payment = self.db.get_payment(row["payment_id"])
                order["payment_status"] = payment["status"] if payment else "missing"
                order["payment_method"] = str(payment["method"] or "") if payment else ""
                order["payment_method_label"] = (
                    PAYMENT_METHOD_LABELS.get(str(payment["method"] or ""), "уточняется")
                    if payment else "уточняется"
                )
                order["can_pay"] = self.db.payment_is_payable(row["payment_id"])
                if payment and payment["status"] == "refund_required":
                    order["status_label"] += " · требуется возврат"
                    order["can_cancel"] = False
                if (payment and payment["gift_product_id"] and not payment["gift_released_at"]
                        and row["payment_id"] not in seen_gift_payments):
                    if payment["gift_serial"] is None and self.catalog:
                        self.db.gift_status(self.catalog.get(str(payment["gift_product_id"])))
                        payment = self.db.get_payment(row["payment_id"])
                    order["gift"] = {
                        "product_id": payment["gift_product_id"], "name": payment["gift_name"],
                        "quantity": int(payment["gift_qty"] or 1),
                        "serial": int(payment["gift_serial"]) if payment["gift_serial"] is not None else None,
                        "serial_label": f"{int(payment['gift_serial']):05d}" if payment["gift_serial"] is not None else "",
                    }
                    seen_gift_payments.add(str(row["payment_id"]))
            order["payment_label"] = payment_state_line(
                str(row["status"]), payment, int(row["amount_rub"] or 0)
            )
            orders.append(order)
        self._json({"orders": orders, "source": "bot"})

    def _cancel_my_order(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"error": "В предпросмотре заказы не отменяются."}, 400)
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
            self._json({"error": "Некорректный заказ."}, 400)
            return
        order = self.db.get_order(order_id)
        if not order or int(order["user_id"]) != int(user["id"]):
            self._json({"error": "Заказ не найден."}, 404)
            return
        try:
            updated, changed = self.db.set_order_status(order_id, "cancelled", customer_id=int(user["id"]))
        except ValueError:
            self._json({"error": "Этот заказ уже нельзя отменить."}, 409)
            return
        if not changed:
            self._json({"error": "Заказ уже отменён.", "order": public_order_payload(updated or order)}, 409)
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

    def _mark_paid(
        self, payment_id: str, method: str, provider_id: str,
        amount_rub: int, currency: str, raw_body: bytes,
    ) -> str:
        if not self.db or not payment_id:
            return "unknown"
        bot = self._bot()
        result = self.db.record_provider_payment(
            payment_id, method, provider_id, amount_rub, currency,
            hashlib.sha256(raw_body).hexdigest(),
            queue_notifications=(lambda pid: bot.notify_paid(pid, queue_only=True)) if bot else None,
            queue_overpayment=(lambda pid, tx: bot.notify_overpayment(pid, tx, queue_only=True)) if bot else None,
        )
        if bot and result != "duplicate":
            bot.flush_notifications()
        return result

    def _provider_webhook_response(
        self, payment_id: str, provider: str, invoice_id: str,
        amount_rub: int, currency: str, raw_body: bytes,
    ) -> None:
        try:
            result = self._mark_paid(payment_id, provider, invoice_id, amount_rub, currency, raw_body)
        except InvoiceMismatch as exc:
            LOG.warning("Rejected mismatched %s invoice for %s: %s", provider, payment_id, exc)
            self._json({"error": "invoice mismatch"}, 409)
            return
        except PaymentMismatch as exc:
            LOG.warning("Rejected mismatched %s payment for %s: %s", provider, payment_id, exc)
            self._json({"error": "amount or currency mismatch"}, 422)
            return
        if result == "unknown":
            self._json({"error": "unknown payment"}, 404)
            return
        response: dict[str, Any] = {"ok": True}
        if result in {"duplicate", "overpayment"}:
            response[result] = True
        self._json(response)

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
        if not self.db or not self.catalog:
            self._json({"error": "Сервис временно недоступен."}, 503)
            return
        user = self._webapp_user()
        if not self._rate_ok(CHECKOUT_LIMITER, "waitlist", user):
            return
        payload = self._read_json_body() or {}
        pid = str(payload.get("product_id") or "")
        product = self.catalog.get(pid) or self.catalog.get_any(pid)
        size = str(payload.get("size") or "")
        if not product or size not in {str(item) for item in product.get("sizes", [])}:
            self._json({"error": "Вещь или размер недоступны."}, 400)
            return
        if not user:
            # Гость в браузере вне Telegram: фиксируем интерес в воронке и подсказываем бота
            self.db.event(None, "miniapp_waitlist_guest", {"product_id": pid, "size": size})
            self._json({"ok": True, "added": True, "via": "guest"})
            return
        self.db.upsert_user(user)
        added = self.db.add_to_waitlist(int(user["id"]), product, size)
        self.db.event(int(user["id"]), "miniapp_waitlist", {"product_id": pid, "size": size})
        self._json({"ok": True, "added": added, "via": "bot"})

    def _club_get(self) -> None:
        active_members = self.db.club_members_count() if self.db else 0
        empty = {"joined": False, "started_on": "", "checkins": [], "total": 0,
                 "completed": False, "early_access": False, "reminder_hour": None,
                 "active_members": active_members}
        if not self.db or not self.settings or not self.settings.token:
            self._json({"ok": True, "club": empty, "source": "preview"})
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота — тогда подтянется клуб."}, 401)
            return
        if not self._rate_ok(READ_LIMITER, "club", user):
            return
        self._json({"ok": True, "club": self.db.club_state(int(user["id"])), "source": "bot"})

    def _club_post(self) -> None:
        if not self.db or not self.settings or not self.settings.token:
            self._json({"error": "Открой витрину из бота, чтобы вступить в клуб."}, 401)
            return
        user = self._webapp_user()
        if not user:
            self._json({"error": "Открой витрину из бота, чтобы вступить в клуб."}, 401)
            return
        if not self._rate_ok(CHECKOUT_LIMITER, "club", user):
            return
        payload = self._read_json_body() or {}
        action = str(payload.get("action") or "")
        if action not in {"join", "checkin", "reminder"}:
            self._json({"error": "Неизвестное действие клуба."}, 400)
            return
        self.db.upsert_user(user)
        user_id = int(user["id"])
        if action == "reminder":
            raw_hour = payload.get("hour")
            hour = int(raw_hour) if isinstance(raw_hour, int) and not isinstance(raw_hour, bool) and 0 <= raw_hour <= 23 else None
            state = self.db.club_set_reminder(user_id, hour)
            self.db.event(user_id, "club_reminder", {"hour": hour})
        else:
            state = self.db.club_join(user_id) if action == "join" else self.db.club_checkin(user_id)
            self.db.event(user_id, f"club_{action}", {"total": state["total"]})
        self._json({"ok": True, "club": state, "source": "bot"})

    def _verify_serial(self) -> None:
        if not self.db:
            self._json({"ok": False, "error": "Реестр временно недоступен."}, 503)
            return
        if not self._rate_ok(READ_LIMITER, "verify-serial"):
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw_code = (qs.get("code") or [""])[0]
        result = self.db.verify_serial(raw_code)
        self._json(result, 200 if result.get("ok") else 400)

    def _track_event(self) -> None:
        if not self.db:
            self._json({"ok": True})
            return
        user = self._webapp_user()
        if not self._rate_ok(READ_LIMITER, "event", user):
            return
        payload = self._read_json_body() or {}
        raw_name = re.sub(r"[^a-z0-9_]", "", str(payload.get("event") or "").lower())[:36]
        allowed = {
            "open", "welcome_done", "product_view", "size_select",
            "compare_open", "bundle_add", "story_share", "manager_click",
        }
        if raw_name not in allowed:
            self._json({"ok": False}, 400)
            return
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        clean_meta = {
            str(k)[:24]: str(v)[:48]
            for k, v in list(meta.items())[:6]
            if isinstance(k, str) and isinstance(v, (str, int, float, bool))
        }
        uid = int(user["id"]) if user and user.get("id") else None
        self.db.event(uid, f"miniapp_{raw_name}", clean_meta)
        self._json({"ok": True})

    def _lead_capture(self) -> None:
        if not self.db or not self.settings:
            self._json({"ok": False}, 503)
            return
        user = self._webapp_user()
        if not self._rate_ok(CHECKOUT_LIMITER, "lead", user):
            return
        payload = self._read_json_body() or {}
        customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        raw_lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
        if not raw_lines:
            self._json({"ok": False, "error": "empty"}, 400)
            return
        clean_lines = []
        for item in raw_lines[:15]:
            if not isinstance(item, dict):
                continue
            clean_lines.append({
                "id": str(item.get("id") or "")[:48],
                "name": str(item.get("name") or "Вещь")[:80],
                "size": str(item.get("size") or "")[:16],
                "qty": max(1, min(20, int(item.get("qty") or 1))),
                "price": str(item.get("price") or "")[:32],
            })
        if not clean_lines:
            self._json({"ok": False, "error": "empty"}, 400)
            return
        if user:
            self.db.upsert_user(user)
        total_rub = max(0, int(payload.get("total_rub") or 0))
        gift_wrap = str(payload.get("gift_wrap") or "")[:160]
        bundle_label = str(payload.get("bundle_label") or "")[:80]
        wrap_summary = " · ".join(x for x in (bundle_label, gift_wrap) if x)
        lead_id = self.db.create_lead(user, customer, clean_lines, total_rub, wrap_summary)
        uid = int(user["id"]) if user and user.get("id") else None
        self.db.event(uid, "miniapp_manager_click", {"lead_id": lead_id, "total_rub": total_rub})
        if self.api and self.settings.manager_chat_id:
            who_parts = []
            if user and user.get("username"):
                who_parts.append(f"@{esc(str(user['username']))}")
            elif user and user.get("id"):
                who_parts.append(f'<a href="tg://user?id={int(user["id"])}">{esc(str(user.get("first_name") or "Покупатель"))}</a>')
            if customer.get("name"):
                who_parts.append(esc(str(customer["name"])[:60]))
            if customer.get("phone"):
                who_parts.append(esc(str(customer["phone"])[:32]))
            who_line = " · ".join(who_parts) or "Гость из браузера"
            lines_txt = "\n".join(
                f"• {esc(it['name'])} · размер <b>{esc(it['size'])}</b> · {it['qty']} шт."
                for it in clean_lines
            )
            delivery_txt = " · ".join(
                esc(str(customer.get(k) or "").strip()[:100])
                for k in ("deliver", "city", "address")
                if str(customer.get(k) or "").strip()
            )
            msg = (
                f"<b>⚡ ЗАЯВКА ИЗ MINI APP №{lead_id}</b>\n"
                f"Клиент открыл чат с менеджером:\n"
                f"👤 {who_line}\n\n"
                f"{lines_txt}\n"
                + (f"🎁 {esc(wrap_summary)}\n" if wrap_summary else "")
                + f"💰 Итого за вещи: <b>{esc(format_rub(total_rub))}</b>\n"
                + (f"🚚 {delivery_txt}\n" if delivery_txt else "")
                + "\n<i>Страховка лида: если покупатель ещё не нажал «Отправить» в личке, контакт уже здесь.</i>"
            )
            markup = (
                inline_keyboard([[("🪪 Подтвердить заказ и выдать Карту", f"lead_confirm:{lead_id}")]])
                if uid else None
            )
            try:
                self.api.send_message(self.settings.manager_chat_id, msg, markup)
            except Exception as exc:
                LOG.warning("Could not notify manager about lead %s: %s", lead_id, exc)
        self._json({"ok": True, "lead_id": lead_id})

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
            self._json({"error": "Пустая корзина."}, 400)
            return
        bot = self._bot()
        if not bot:
            self._json({"error": "Оплата сейчас недоступна."}, 503)
            return
        result = bot.checkout_web_payload(user, payload, notify_user=int(user["id"]))
        if not result.get("ok"):
            self._json(
                {"error": result.get("error") or "Не получилось принять заказ.", "code": result.get("code", "")},
                409 if result.get("code") in ("gift_unavailable", "size_unavailable") else 400,
            )
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
        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status = str(inner.get("status") or inner.get("invoice_status") or payload.get("status") or "").lower()
        if status not in {"success", "paid", "completed", "successed"}:
            self._json({"ok": True, "ignored": True})
            return
        payment_id = str(inner.get("order_id") or inner.get("orderId") or inner.get("custom_id") or "")
        invoice_id = str(inner.get("invoice_id") or inner.get("invoiceId") or inner.get("id") or "")
        shop_id = str(inner.get("shop_id") or inner.get("shopId") or "")
        if shop_id and shop_id != settings.lava_shop_id:
            self._json({"error": "shop mismatch"}, 422)
            return
        # Lava invoices are created in RUB.  If a callback explicitly names a
        # currency it must agree; an absent legacy field means the invoice's RUB.
        currency = str(inner.get("currency") or inner.get("fiat") or "RUB").upper()
        try:
            amount_rub = parse_price_strict(inner.get("sum") if inner.get("sum") is not None else inner.get("amount"))
        except PriceError:
            self._json({"error": "valid invoice amount required"}, 422)
            return
        self._provider_webhook_response(payment_id, "lava", invoice_id, amount_rub, currency, raw)

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
        invoice_status = str(inner.get("status") or "paid").lower()
        currency_type = str(inner.get("currency_type") or "").lower()
        currency = str(inner.get("fiat") or "").upper()
        if invoice_status != "paid" or currency_type != "fiat" or currency != "RUB":
            self._json({"error": "paid fiat RUB invoice required"}, 422)
            return
        try:
            # ``amount`` is the fiat invoice amount. ``paid_amount`` is the
            # crypto asset amount and must never be compared with RUB.
            amount_rub = parse_price_strict(inner.get("amount"))
        except PriceError:
            self._json({"error": "valid invoice amount required"}, 422)
            return
        self._provider_webhook_response(payment_id, "crypto", invoice_id, amount_rub, currency, raw)

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("Storefront GET failed: %s", self.path)
            try:
                self._json({"error": "server_error"}, 500)
            except Exception:
                pass

    def _handle_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            checks: dict[str, str] = {}
            healthy = True
            if self.db:
                try:
                    self.db.connection().execute("SELECT 1").fetchone()
                    checks["database"] = "ok"
                except sqlite3.Error:
                    healthy = False
                    checks["database"] = "error"
                    LOG.warning("Health check: SQLite is unavailable", exc_info=True)
            if self.catalog:
                try:
                    with self.catalog.lock:
                        loaded = isinstance(self.catalog.data.get("products"), list)
                    checks["catalog"] = "ok" if loaded else "error"
                    healthy = healthy and loaded
                except (KeyError, TypeError, AttributeError):
                    healthy = False
                    checks["catalog"] = "error"
                    LOG.warning("Health check: catalog is unavailable", exc_info=True)
            self._json(
                {"status": "ok" if healthy else "error", "service": "vorozhbitov-shop", "checks": checks},
                200 if healthy else 503,
            )
            return
        if path == "/api/my-orders":
            self._my_orders()
            return
        if path == "/api/club":
            self._club_get()
            return
        if path == "/api/verify-serial":
            self._verify_serial()
            return
        if path == "/api/emoji-pack":
            pack_name = self.settings.custom_emoji_pack if self.settings else "sila_chest_merch"
            payload = {
                "pack": pack_name,
                "link": f"https://t.me/addemoji/{pack_name}",
                "icons": [
                    {"key": "sword", "file": "01_sword.png", "emoji": "⚔️", "label": "Клинок"},
                    {"key": "seal", "file": "02_seal.png", "emoji": "🛡️", "label": "Печать"},
                    {"key": "drop", "file": "03_drop.png", "emoji": "👕", "label": "Дроп 001"},
                    {"key": "orders", "file": "04_orders.png", "emoji": "📦", "label": "Бокс дропа"},
                    {"key": "profile", "file": "05_profile.png", "emoji": "🪪", "label": "Стальной жетон"},
                    {"key": "support", "file": "06_support.png", "emoji": "💬", "label": "Штаб-связь"},
                    {"key": "cart", "file": "07_cart.png", "emoji": "🛒", "label": "Кофр заказа"},
                    {"key": "ruler", "file": "08_ruler.png", "emoji": "🏷️", "label": "Патч подола"},
                    {"key": "card", "file": "09_card.png", "emoji": "💳", "label": "Титан-карта"},
                    {"key": "lookbook", "file": "10_lookbook.png", "emoji": "🗡️", "label": "Спина с клинком"},
                    {"key": "channel", "file": "11_channel.png", "emoji": "📢", "label": "Сигнал канала"},
                    {"key": "back", "file": "12_back.png", "emoji": "⬅️", "label": "Шеврон назад"}
                ]
            }
            if self.settings and self.settings.inline_emoji_enabled:
                payload["icons"] = [
                    {"key": "catalog", "file": "01-catalog.webm", "emoji": "👕", "label": "Первый дроп"},
                    {"key": "orders", "file": "02-orders.webm", "emoji": "📦", "label": "Мои покупки"},
                    {"key": "account", "file": "03-account.webm", "emoji": "🤖", "label": "Кабинет"},
                    {"key": "support", "file": "04-support.webm", "emoji": "💬", "label": "Поддержка"},
                    {"key": "pay", "file": "05-pay.webm", "emoji": "💳", "label": "Оплата"},
                    {"key": "back", "file": "06-back.webm", "emoji": "⬅️", "label": "Назад"},
                    {"key": "cart", "file": "07-cart.webm", "emoji": "🛍️", "label": "Магазин"},
                    {"key": "delivery", "file": "08-delivery.webm", "emoji": "🚚", "label": "Доставка"},
                    {"key": "tag", "file": "09-gift.webm", "emoji": "🏷️", "label": "Жетон"},
                    {"key": "sword", "file": "10-brand.webm", "emoji": "⚔️", "label": "О бренде"},
                    {"key": "channel", "file": "11-channel.webm", "emoji": "📣", "label": "Канал"},
                ]
            self._write(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
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
                    "brand": catalog.data.get("brand", {"name": settings.brand_name if settings else "СИЛА И ЧЕСТЬ"}),
                    "channel_url": settings.channel_url if settings else "",
                    "privacy_url": settings.privacy_url if settings else "",
                    "support_username": settings.support_username if settings else "",
                    "order_manager_username": order_manager_username(),
                    "bot_username": (self.db.kv_get("bot_username") if self.db else "") or "SilaIChestBot",
                    "products": self._stamp_product_media([
                        product for product in catalog.data.get("products", []) if product.get("active", True)
                    ]),
                    "upcoming_products": self._stamp_product_media([
                        product for product in catalog.data.get("products", [])
                        if not product.get("active", True) and product.get("upcoming", False)
                    ]),
                    "categories": catalog.categories,
                    "lookbook": self._stamp_collection(catalog.data.get("lookbook", []), ("image",)),
                    "drop_event": catalog.data.get("drop_event", {}),
                    "film": self._stamp_collection(catalog.data.get("film", []), ("image", "video", "poster")),
                    "club": catalog.data.get("club", {}),
                    "club_stats": {"active_members": self.db.club_members_count() if self.db else 0},
                    "community": self._stamp_collection(catalog.data.get("community", []), ("image", "video")),
                    "media": self._stamp_media(catalog.data.get("media", {})),
                }
                gift_product = catalog.get(settings.gift_product_id) if settings else None
                gift = self.db.gift_status(gift_product) if self.db else None
                if gift is None and gift_product and gift_product.get("gift"):
                    gift = {
                        "product_id": gift_product["id"], "name": gift_product["name"],
                        "image": gift_product.get("image", ""), "stock": int(gift_product.get("gift_stock", 0)),
                        "remaining": int(gift_product.get("gift_stock", 0)), "available": True,
                    }
                payload["gift"] = gift
            if self.db:
                # Tracked sizes: hide sold-out ones and expose exact remains.
                try:
                    snapshot = self.db.size_stock_snapshot(payload["products"])
                except Exception:
                    LOG.warning("size_stock snapshot failed", exc_info=True)
                    snapshot = None
                for item in payload["products"]:
                    totals = item.get("stock_by_size")
                    if not isinstance(totals, dict):
                        continue
                    if snapshot is None:
                        # Unknown stock is not the original catalog stock and must
                        # never be advertised as currently available.
                        item["sizes"] = []
                        item.pop("stock_by_size", None)
                        item["stock_check_unavailable"] = True
                        continue
                    remaining = {
                        str(size): snapshot.get((str(item.get("id")), str(size)), 0)
                        for size in totals
                    }
                    item["stock_by_size"] = remaining
                    item["sizes"] = [size for size in item.get("sizes", []) if remaining.get(str(size), 1) > 0]
            self._write(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/app/":
            self.send_response(301)
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
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        if candidate.name != "index.html" and self._has_current_asset_version(candidate):
            cache = "public, max-age=31536000, immutable"
        elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
            cache = "public, max-age=3600"
        elif suffix in {".woff2", ".woff", ".ttf", ".glb"} or candidate.name.endswith(".bundle.js"):
            # Шрифты, 3D-модели и версионированные бандлы держим в кеше долго.
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-cache"
        # WebView Telegram держит app.js и styles.css в кеше и после правок
        # показывает старую версию. Клеим метку версии по времени файла —
        # поэтому index/styles штампуются и не участвуют в ETag/стриме.
        stamped = candidate.name in ("index.html", "styles.css")
        etag = ""
        try:
            stat = candidate.stat()
        except OSError:
            self._not_found()
            return
        if not stamped:
            etag = f'"{int(stat.st_mtime):x}-{stat.st_size:x}"'
            if (self.headers.get("If-None-Match") or "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if stat.st_size > 512 * 1024 and not content_type.startswith(("text/", "application/javascript")):
                self._write_file(candidate, content_type, cache, stat.st_size, etag)
                return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._not_found()
            return
        if stamped:
            body = self._stamp_fonts(self._stamp_assets(body))
        self._write(body, content_type, 200, cache, etag)

    def _stamp_collection(self, items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in items:
            stamped = dict(item)
            for k in keys:
                v = item.get(k)
                if isinstance(v, str) and v:
                    stamped[k] = self._stamp_media({"value": v})["value"]
            result.append(stamped)
        return result

    def _stamp_product_media(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Версионировать товарные media URL, не меняя каталог в памяти.

        Telegram WebView и браузеры кешируют изображения независимо от JS.
        Привязка URL к mtime не даёт продолжать запрашивать старый/отсутствующий
        кадр после публикации обновлённой серии с тем же именем.
        """
        stamped_products: list[dict[str, Any]] = []
        for product in products:
            stamped = dict(product)
            for key in ("image", "images", "source_images", "spin", "video", "card_image"):
                value = product.get(key)
                if isinstance(value, str):
                    stamped[key] = self._stamp_media({"value": value})["value"]
                elif isinstance(value, list):
                    stamped[key] = [
                        self._stamp_media({"value": item})["value"] if isinstance(item, str) else item
                        for item in value
                    ]
            stamped_products.append(stamped)
        return stamped_products

    def _stamp_media(self, media: dict[str, Any]) -> dict[str, Any]:
        """Добавить ?v=<время правки> к роликам и постерам из блока media.

        Видео отдаётся с ``max-age=86400``, а имя файла не меняется: заменив
        ``teaser.mp4``, мы сутки показывали бы всем старую копию из кеша
        браузера. Версия в адресе делает подмену мгновенной.
        """
        stamped: dict[str, Any] = {}
        for key, value in media.items():
            # В media лежат не только URL, но и настройки вроде
            # welcome_motion=False. Их нельзя превращать в строку с ?v=.
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
        """Версионировать клиентский код, CSS и все статические медиа-ассеты в HTML."""
        text = body.decode("utf-8", "replace")
        for name in (
            "app.js",
            "checkout.js",
            "shop-core.js",
            "viewer3d.js",
            "telegram-web-app.js",
            "styles.css",
            "polish.css",
            "readability-20261108.css",
            "editorial-20261112d.css",
            "hero-founder-20261107.webp",
            "hero-founder.webp",
        ):
            asset = self.static_root / name
            try:
                version = self._asset_version(asset)
            except OSError:
                continue
            text = re.sub(
                rf'(href|src)="{re.escape(name)}(?:\?[^"]*)?"',
                rf'\1="{name}?v={version}"',
                text,
            )

        def _stamp_match(m: re.Match[str]) -> str:
            attr = m.group(1)
            quote = m.group(2)
            rel = m.group(3)
            close = m.group(4)
            if "?v=" in rel:
                return m.group(0)
            candidate = self.static_root / rel
            try:
                version = int(candidate.stat().st_mtime)
                return f"{attr}={quote}{rel}?v={version}{close}"
            except OSError:
                return m.group(0)

        text = re.sub(r"""(src|href|poster)=(["'])(assets/[^"'?]+|hero-founder[^"'?]*)(["'])""", _stamp_match, text)

        # Динамический OpenGraph / Twitter превью под текущий публичный домен (sila.theatralka.ru или .nip.io)
        raw_host = ""
        if hasattr(self, "headers") and self.headers is not None:
            raw_host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip().lower()
        host_only = raw_host.split(":")[0]
        origin = ""
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,120}[a-z0-9])?", host_only) and host_only not in {"127.0.0.1", "localhost"}:
            origin = f"https://{host_only}"
        elif getattr(self, "settings", None) and str(self.settings.webapp_url).startswith("https://"):
            parsed_webapp = urllib.parse.urlsplit(self.settings.webapp_url)
            if parsed_webapp.netloc:
                origin = f"https://{parsed_webapp.netloc}"
        if origin:
            og_asset = self.static_root / "assets/client/og-preview-20261112c.jpg"
            try:
                og_ver = f"?v={int(og_asset.stat().st_mtime)}"
            except OSError:
                og_ver = ""
            text = re.sub(
                r'(<meta\s+property="og:url"\s+content=")https://[^"]+(")',
                rf"\g<1>{origin}/\g<2>",
                text,
            )
            text = re.sub(
                r'(<meta\s+(?:property="og:image"|name="twitter:image")\s+content=")https://[^"/]+/(assets/[^"?]+)(?:\?[^"]*)?(")',
                rf"\g<1>{origin}/\g<2>{og_ver}\g<3>",
                text,
            )
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
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("Storefront POST failed: %s", self.path)
            try:
                self._json({"error": "server_error"}, 500)
            except Exception:
                pass

    def _handle_post(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/my-orders/cancel":
            self._cancel_my_order()
            return
        if path == "/api/waitlist":
            self._waitlist()
            return
        if path == "/api/club":
            self._club_post()
            return
        if path == "/api/event":
            self._track_event()
            return
        if path == "/api/lead":
            self._lead_capture()
            return
        if path in {"/api/checkout", "/api/pay"} and not ONLINE_PAYMENTS_ENABLED:
            self._json({"ok": False, "error": "Онлайн-оплата временно недоступна. Оформи заказ через чат магазина."}, 409)
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
    host = os.getenv("HOST", os.getenv("BIND_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="storefront-server", daemon=True).start()
    return server


def notification_loop(bot: BrandBot) -> None:
    try:
        while not STOP_EVENT.is_set():
            try:
                bot.db.expire_payments()
                bot.db.backup_database()
                bot.db.prune_operational_data()
                bot.flush_club_reminders()
                bot.flush_notifications()
            except Exception:
                LOG.exception("Notification retry failed")
            STOP_EVENT.wait(5)
    finally:
        bot.db.close_current()


def polling_loop(api: TelegramAPI, bot: BrandBot) -> None:
    offset = 0
    try:
        # Survive restarts: Telegram keeps updates ~24h, so starting from 0
        # after an OOM/redeploy would replay a day of callbacks and checkouts.
        offset = max(0, int(bot.db.kv_get("updates_offset") or 0))
    except Exception:
        offset = 0
    retry_delay = 1
    while not STOP_EVENT.is_set():
        try:
            updates = api.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 10,
                    "allowed_updates": ["message", "callback_query", "pre_checkout_query", "inline_query"],
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
                    new_offset = max(offset, raw_id + 1)
                    if new_offset != offset:
                        offset = new_offset
                        try:
                            bot.db.kv_set("updates_offset", str(offset))
                        except Exception:
                            LOG.warning("Не удалось сохранить updates_offset", exc_info=True)
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
        try:
            db.sync_size_stock(catalog.data.get("products", []))
        except Exception:
            LOG.warning("Не удалось синхронизировать остатки по размерам", exc_info=True)
        api = TelegramAPI(settings.token)
    except Exception as exc:
        LOG.error("Startup failed: %s", exc)
        return 1
    health_server = start_health_server(settings.health_port, catalog, settings, db, api)
    try:
        identity = api.call("getMe")
        api.call("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        LOG.error("Startup failed: %s", exc)
        health_server.shutdown()
        health_server.server_close()
        return 1
    bot = BrandBot(settings, api, db, catalog)
    bot.bot_username = str(identity.get("username") or "")
    if bot.bot_username:
        try:
            db.kv_set("bot_username", bot.bot_username)
        except Exception:
            pass
    LOG.info("Starting @%s for brand %s", bot.bot_username, settings.brand_name)
    try:
        # Системная кнопка «Меню» открывает команды бота: покупатель видит свой
        # короткий список, команда — служебный. Витрина остаётся кнопкой в чате.
        default_menu_button = make_chat_menu_button(settings.webapp_url)
        api.call("setMyCommands", {"commands": buyer_commands()})
        api.call("setChatMenuButton", {"menu_button": default_menu_button})
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
                LOG.info("Pre-set staff commands for %s deferred (chat not started yet): %s", admin_id, exc)
    except Exception as exc:
        LOG.warning("Could not set Telegram commands or menu button: %s", exc)
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
