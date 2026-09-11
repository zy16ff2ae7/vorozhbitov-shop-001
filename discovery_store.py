"""Native discovery: short-lived structured searches and consented private lists.

No raw search text, provider calls, checkout, stock reservation or marketing.
Prices come from Catalog.public_products(), availability from stock_items only.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata

from commerce_store import CartConflict, encode, now_iso

SESSION_TTL = 86400
LIST_LIMITS = {"saved": 50, "compare": 3}
STOCK_NAMES = {"any": "Любое наличие", "available": "Есть свободный остаток", "out": "Нет в наличии", "unknown": "Наличие уточняется"}
SORT_NAMES = {"catalog": "Порядок выпуска", "price_asc": "Сначала дешевле", "price_desc": "Сначала дороже"}
DEFAULT_FILTERS = {"category": None, "size": None, "min_price": None, "max_price": None, "stock": "any", "sort": "catalog", "scope": None, "text_digest": None}


def norm(value):
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def term(word):
    word = norm(word)
    aliases = {"tee": "футболка", "tshirt": "футболка", "hoodie": "худи", "black": "черный", "white": "белый", "tag": "жетон"}
    word = aliases.get(word, word)
    for suffix in ("ыми", "ими", "ого", "ему", "ому", "ами", "ями", "ая", "яя", "ую", "юю", "ые", "ие", "ый", "ий", "ой", "ов", "ом", "ам", "ах", "ью", "а", "я", "ы", "и", "у", "е", "ь"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def text_fields(product):
    fields = [product.get(k, "") for k in ("name", "description", "material", "fit")]
    details = product.get("details")
    if isinstance(details, list): fields += [x for x in details if isinstance(x, str)]
    return [x for x in fields if isinstance(x, str)]


def catalog_snapshot(catalog):
    with catalog.lock:
        return catalog.public_products(), {c["id"]: c["name"] for c in catalog.categories}


def text_digest(products, categories):
    # Prices and stock stay live. Text/visibility changes invalidate lexical
    # matches rather than silently reusing an old "black tee" as a red product.
    values = [(p["id"], p["category"], text_fields(p)) for p in products]
    return hashlib.sha256(encode([values, categories]).encode()).hexdigest()


def parse_search(query, products, categories):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160 or any(ord(c) < 32 for c in query):
        raise ValueError("Поиск: одно сообщение до 160 символов. Например: чёрная футболка M до 5000.")
    q = norm(query.strip()).rstrip(" .!?")
    if re.search(r"(?<!\w)-", q): raise ValueError("Минус и исключения не угадываем. Используй положительные слова и целые цены «от/до».")
    if re.search(r"\d[.,]\d", q): raise ValueError("В фильтре цены используй целые рубли, без дробей и валютных конвертаций.")
    data = dict(DEFAULT_FILTERS)
    pattern = r"\b(до|от)\s*(\d{1,3}(?: \d{3})+|\d+)\s*(?:₽|руб(?:лей|ля|ль)?\b\.?)?"
    def price(match):
        key = "max_price" if match[1] == "до" else "min_price"
        value = int(match[2].replace(" ", ""))
        if data[key] is not None or not 0 <= value <= 10_000_000:
            raise ValueError("Укажи одну нижнюю и/или верхнюю границу от 0 до 10000000 ₽.")
        data[key] = value
        return " "
    q = re.sub(pattern, price, q)
    if data["min_price"] is not None and data["max_price"] is not None and data["min_price"] > data["max_price"]:
        raise ValueError("Нижняя граница цены больше верхней.")
    for phrase, stock in (("нет в наличии", "out"), ("наличие уточняется", "unknown"), ("в наличии", "available")):
        if phrase in q:
            if data["stock"] != "any": raise ValueError("Выбери один режим наличия.")
            data["stock"] = stock
            q = q.replace(phrase, " ")
    sizes = sorted({str(s) for p in products for s in p["sizes"]}, key=len, reverse=True)
    for size in sizes:
        pattern = r"(?<![\w])" + re.escape(norm(size)) + r"(?![\w])"
        if re.search(pattern, q):
            if data["size"] is not None: raise ValueError("В одном поиске выбери один размер.")
            data["size"] = size
            q = re.sub(pattern, " ", q)
    # No fuzzy dropping of an unrecognised condition (e.g. a colour we cannot
    # verify). Also refuse punctuation/URLs instead of retaining personal input.
    if re.search(r"[^\w\s\-«»\"']", q): raise ValueError("Используй слова из описания, один размер, «от/до» и целые рубли. Текст запроса не сохраняется.")
    words = re.findall(r"[\w]+", q)
    if set(words) & {"не", "нет", "без", "кроме", "или", "дороже", "дешевле", "от", "до"}:
        raise ValueError("Отрицания и сложные условия не угадываем. Используй положительное описание, «от/до» и кнопки фильтров.")
    stop = {"хочу", "найди", "покажи", "пожалуйста", "только", "размер", "размера", "размере", "и"}
    wanted = {term(w) for w in words if w not in stop}
    documents = {p["id"]: {term(w) for w in re.findall(r"[\w]+", " ".join(text_fields(p) + [categories.get(p["category"], "")]))} for p in products}
    known = set().union(*documents.values()) if documents else set()
    if wanted - known:
        raise ValueError("Не все условия найдены в описаниях действующей витрины. Не буду угадывать. Попробуй название/материал, размер и «до 5000», либо кнопки фильтров.")
    if not wanted and data == DEFAULT_FILTERS:
        raise ValueError("Добавь название вещи, материал, размер, цену или условие наличия.")
    if wanted:
        matches = [p["id"] for p in products if wanted <= documents[p["id"]]]
        if len(matches) > 1000: raise ValueError("Слишком много совпадений. Уточни описание вещи.")
        data["scope"] = matches
        data["text_digest"] = text_digest(products, categories)
    return data


def item_view(product, inventory):
    sizes = [{"size": s, "available": inventory.get(product["id"], {}).get(s, {}).get("available")} for s in dict.fromkeys(product["sizes"])]
    price = product.get("price_rub")
    return {"product_id": product["id"], "name": product["name"], "category": product["category"],
            "price_rub": price if type(price) is int and price > 0 else None,
            "price_label": product["price"], "sizes": sizes, "active": True,
            "description": product["description"], "material": product.get("material") if isinstance(product.get("material"), str) else "",
            "fit": product.get("fit") if isinstance(product.get("fit"), str) else "",
            "details": [s for s in product.get("details", []) if isinstance(s, str)] if isinstance(product.get("details"), list) else []}


def stock_state(variants):
    if any(s["available"] is not None and s["available"] > 0 for s in variants): return "available"
    if not variants or any(s["available"] is None for s in variants): return "unknown"
    return "out"


class DiscoveryStore:
    def init_discovery_store(self):
        self.connection().executescript("""
            CREATE TABLE IF NOT EXISTS discovery_sessions (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                generation TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                filters TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_accounts (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                generation INTEGER NOT NULL DEFAULT 0, consent_at TEXT,
                saved_revision INTEGER NOT NULL DEFAULT 0, compare_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discovery_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES discovery_accounts(user_id),
                kind TEXT NOT NULL CHECK(kind IN ('saved','compare')),
                product_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(user_id,kind,product_id)
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_items_owner ON discovery_items(user_id,kind,item_id);
            CREATE INDEX IF NOT EXISTS idx_discovery_session_expiry ON discovery_sessions(expires_at);
        """)

    def _discovery_user(self, user_id):
        if type(user_id) is not int or user_id <= 0 or not self.get_user(user_id):
            raise PermissionError("Открой бота в своём личном чате.")

    def discovery_session(self, user_id):
        self._discovery_user(user_id)
        with self.commerce_transaction() as conn:
            row = conn.execute("SELECT * FROM discovery_sessions WHERE user_id=?", (user_id,)).fetchone()
            if not row or row["expires_at"] <= time.time():
                conn.execute("INSERT INTO discovery_sessions VALUES (?,?,0,?,?) ON CONFLICT(user_id) DO UPDATE SET generation=excluded.generation,revision=0,filters=excluded.filters,expires_at=excluded.expires_at",
                    (user_id, secrets.token_hex(8), encode(DEFAULT_FILTERS), time.time() + SESSION_TTL))
                row = conn.execute("SELECT * FROM discovery_sessions WHERE user_id=?", (user_id,)).fetchone()
            return {**dict(row), "filters": json.loads(row["filters"])}

    @staticmethod
    def _discovery_version(row, generation, revision):
        if generation != row["generation"] or type(revision) is not int or revision != row["revision"]:
            raise CartConflict("Выбор уже изменился или истёк. Открой актуальный подбор, не старую кнопку.")

    def _save_discovery_session(self, user_id, generation, revision, data, operation_id, intent=None):
        with self.commerce_transaction() as conn:
            current = self.discovery_session(user_id)
            if generation != current["generation"]: raise CartConflict("Сессия поиска истекла. Открой подбор заново.")
            previous, fingerprint = self._service_request(user_id, operation_id, "discovery_search", [generation, revision, data if intent is None else intent])
            if previous is not None: return current
            self._discovery_version(current, generation, revision)
            conn.execute("UPDATE discovery_sessions SET filters=?,revision=revision+1,expires_at=? WHERE user_id=?", (encode(data), time.time() + SESSION_TTL, user_id))
            self._service_done(user_id, operation_id, "discovery_search", fingerprint, revision + 1)
            return self.discovery_session(user_id)

    def search_discovery(self, user_id, query, generation, revision, operation_id, catalog):
        with catalog.lock:
            products, categories = catalog_snapshot(catalog)
            data = parse_search(query, products, categories)
            # Only the interpreted filters and matching catalog IDs persist.
            return self._save_discovery_session(user_id, generation, revision, data, operation_id, {"query_hash": hashlib.sha256(query.strip().encode()).hexdigest()})

    def filter_discovery(self, user_id, changes, generation, revision, operation_id, catalog, *, reset=False):
        with catalog.lock, self.commerce_transaction():
            row = self.discovery_session(user_id)
            products, categories = catalog_snapshot(catalog)
            if type(reset) is not bool: raise ValueError("Некорректный сброс фильтра.")
            if not isinstance(changes, dict) or set(changes) - {"category", "size", "min_price", "max_price", "stock", "sort"}:
                raise ValueError("Некорректные фильтры.")
            data = dict(DEFAULT_FILTERS) if reset else {**row["filters"], **changes}
            sizes = {s for p in products for s in p["sizes"]}
            if data["category"] is not None and data["category"] not in {p["category"] for p in products}: raise ValueError("Категория больше не представлена в витрине.")
            if data["size"] is not None and data["size"] not in sizes: raise ValueError("Размер больше не представлен в витрине.")
            if data["stock"] not in STOCK_NAMES or data["sort"] not in SORT_NAMES: raise ValueError("Неизвестный режим фильтра.")
            for key in ("min_price", "max_price"):
                if data[key] is not None and (type(data[key]) is not int or not 0 <= data[key] <= 10_000_000): raise ValueError("Цена фильтра — целые рубли от 0 до 10000000.")
            if data["min_price"] is not None and data["max_price"] is not None and data["min_price"] > data["max_price"]: raise ValueError("Нижняя граница выше верхней. Сначала сбрось цену.")
            return self._save_discovery_session(user_id, generation, revision, data, operation_id, {"changes": changes, "reset": reset})

    def browse_discovery(self, user_id, catalog, page=0):
        with catalog.lock, self.commerce_transaction():
            session = self.discovery_session(user_id)
            products, categories = catalog_snapshot(catalog)
            f = session["filters"]
            stale = f["scope"] is not None and f["text_digest"] != text_digest(products, categories)
            inventory = self.inventory_view()
            found = []
            for p in products if not stale else []:
                if f["scope"] is not None and p["id"] not in f["scope"]: continue
                if f["category"] and f["category"] != p["category"]: continue
                if f["size"] and f["size"] not in p["sizes"]: continue
                item = item_view(p, inventory)
                price = item["price_rub"]
                if f["min_price"] is not None and (price is None or price < f["min_price"]): continue
                if f["max_price"] is not None and (price is None or price > f["max_price"]): continue
                variants = [s for s in item["sizes"] if not f["size"] or s["size"] == f["size"]]
                item["stock"] = stock_state(variants)
                if f["stock"] != "any" and item["stock"] != f["stock"]: continue
                found.append(item)
            if f["sort"] != "catalog":
                found.sort(key=lambda p: (p["price_rub"] is None, (p["price_rub"] or 0) * (-1 if f["sort"] == "price_desc" else 1)))
            page = max(0, min(int(page), max(0, (len(found) - 1) // 5)))
            return {"session": session, "items": found[page * 5:(page + 1) * 5], "total": len(found), "page": page, "has_more": (page + 1) * 5 < len(found),
                    "stale": stale, "categories": {k: v for k, v in categories.items() if any(p["category"] == k for p in products)},
                    "sizes": list(dict.fromkeys(s for p in products for s in p["sizes"])),
                    "prices": sorted({p["price_rub"] for p in products if p["price_rub"] > 0})}

    def discovery_product(self, user_id, product_id, catalog):
        self._discovery_user(user_id)
        with catalog.lock:
            products, _ = catalog_snapshot(catalog)
            product = next((p for p in products if p["id"] == product_id), None)
            if not product: raise ValueError("Вещь снята с витрины. Старые кнопки не вернут её в продажу.")
            return item_view(product, self.inventory_view())

    def discovery_account(self, user_id):
        self._discovery_user(user_id)
        row = self.connection().execute("SELECT * FROM discovery_accounts WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {"user_id": user_id, "enabled": 0, "generation": 0, "consent_at": None, "saved_revision": 0, "compare_revision": 0}

    def discovery_consent(self, user_id, enabled, generation, operation_id):
        if type(enabled) is not bool or type(generation) is not int: raise ValueError("Нужно явное решение о хранении списков.")
        with self.commerce_transaction() as conn:
            account = self.discovery_account(user_id)
            previous, fp = self._service_request(user_id, operation_id, "discovery_consent", [enabled, generation])
            if previous is not None:
                if bool(account["enabled"]) != enabled or account["generation"] != previous: raise ValueError("Решение уже изменено. Старая кнопка его не восстановит.")
                return account
            if account["generation"] != generation: raise CartConflict("Настройка хранения уже изменилась. Открой актуальный экран.")
            conn.execute("INSERT OR IGNORE INTO discovery_accounts(user_id) VALUES (?)", (user_id,))
            conn.execute("UPDATE discovery_accounts SET enabled=?,generation=generation+1,consent_at=?,saved_revision=saved_revision+1,compare_revision=compare_revision+1 WHERE user_id=?", (int(enabled), now_iso() if enabled else None, user_id))
            if not enabled:
                conn.execute("DELETE FROM discovery_items WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM discovery_sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM states WHERE user_id=? AND substr(state,1,5)='disc_'", (user_id,))
                # Retire personalised action payloads immediately. Old callback
                # tokens no longer carry deleted preferences after withdrawal.
                conn.execute("DELETE FROM chat_actions WHERE user_id=? AND kind LIKE 'discovery.%'", (user_id,))
            self._service_done(user_id, operation_id, "discovery_consent", fp, generation + 1)
            self.event(user_id, "discovery_storage_enabled" if enabled else "discovery_storage_disabled")
            return self.discovery_account(user_id)

    def _collection_access(self, user_id, kind, generation=None):
        if kind not in LIST_LIMITS: raise ValueError("Неизвестный список.")
        account = self.discovery_account(user_id)
        if not account["enabled"]: raise PermissionError("Сначала разреши хранение избранного и сравнения. Подписка на уведомления не включается.")
        if generation is not None and (type(generation) is not int or account["generation"] != generation): raise CartConflict("Согласие на списки изменилось. Старая кнопка недействительна.")
        return account

    def change_collection(self, user_id, kind, product_id, add, generation, revision, operation_id, catalog):
        if type(add) is not bool or not isinstance(product_id, str) or not 1 <= len(product_id.encode()) <= 40:
            raise ValueError("Некорректное действие со списком.")
        with catalog.lock, self.commerce_transaction() as conn:
            account = self._collection_access(user_id, kind, generation)
            previous, fp = self._service_request(user_id, operation_id, "discovery_list", [kind, product_id, add, generation, revision])
            if previous is not None: return account
            if type(revision) is not int or revision != account[kind + "_revision"]: raise CartConflict("Список изменился. Обнови его перед действием.")
            exists = conn.execute("SELECT 1 FROM discovery_items WHERE user_id=? AND kind=? AND product_id=?", (user_id, kind, product_id)).fetchone()
            if add:
                if not catalog.get(product_id): raise ValueError("Вещь больше не доступна в витрине.")
                if not exists and conn.execute("SELECT COUNT(*) FROM discovery_items WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()[0] >= LIST_LIMITS[kind]:
                    raise ValueError(f"В этом списке максимум {LIST_LIMITS[kind]} вещей. Сначала убери одну.")
                conn.execute("INSERT OR IGNORE INTO discovery_items(user_id,kind,product_id,created_at) VALUES (?,?,?,?)", (user_id, kind, product_id, now_iso()))
            else: conn.execute("DELETE FROM discovery_items WHERE user_id=? AND kind=? AND product_id=?", (user_id, kind, product_id))
            conn.execute(f"UPDATE discovery_accounts SET {kind}_revision={kind}_revision+1 WHERE user_id=?", (user_id,))
            self._service_done(user_id, operation_id, "discovery_list", fp, 0)
            self.event(user_id, "discovery_list_changed", {"kind": kind, "added": add})
            return self.discovery_account(user_id)

    def collection_view(self, user_id, kind, catalog, page=0):
        with catalog.lock, self.commerce_transaction() as conn:
            account = self._collection_access(user_id, kind)
            rows = list(conn.execute("SELECT product_id FROM discovery_items WHERE user_id=? AND kind=? ORDER BY item_id", (user_id, kind)))
            products, _ = catalog_snapshot(catalog)
            lookup = {p["id"]: p for p in products}
            inventory = self.inventory_view()
            page = max(0, min(int(page), max(0, (len(rows) - 1) // 5)))
            selected = rows if kind == "compare" else rows[page * 5:(page + 1) * 5]
            return {"account": account, "kind": kind, "page": page, "total": len(rows), "has_more": (page + 1) * 5 < len(rows),
                    "items": [item_view(lookup[r[0]], inventory) if r[0] in lookup else {"product_id": r[0], "name": "Вещь снята с витрины", "active": False} for r in selected]}

    def discovery_membership(self, user_id, product_id):
        account = self.discovery_account(user_id)
        rows = self.connection().execute("SELECT kind FROM discovery_items WHERE user_id=? AND product_id=?", (user_id, product_id)) if account["enabled"] else []
        return account, {r[0] for r in rows}

    def prune_discovery(self):
        with self.commerce_transaction() as conn:
            conn.execute("DELETE FROM discovery_sessions WHERE expires_at<?", (time.time(),))
            conn.execute("DELETE FROM service_requests WHERE kind IN ('discovery_input','discovery_search') AND julianday(created_at)<julianday('now','-7 days')")
