"""Private-chat product discovery, not checkout or an AI recommendation engine."""
from __future__ import annotations

import hashlib
import math

from commerce_bot import button, esc
from discovery_store import STOCK_NAMES, SORT_NAMES, LIST_LIMITS, stock_state
from payments import format_rub

LIST_NAMES = {"saved": "Избранное в боте", "compare": "Сравнение вещей"}
AXES = ["Обзор", "Цена товаров", "Материал", "Посадка", "Размеры и наличие", "Описание и детали"]


def short(value, limit=80):
    value = str(value)
    return esc(value[:limit]) + ("…" if len(value) > limit else "")


class DiscoveryBot:
    def __init__(self, bot):
        self.bot, self.db, self.ui, self.catalog = bot, bot.db, bot.commerce, bot.catalog

    def action(self, uid, label, kind, **data):
        token = self.db.create_chat_action(uid, "discovery." + kind, data)
        return button(label, "d:" + token.split(":", 1)[1])

    def screen(self, chat, uid, title, text, rows, *, pause=True):
        if pause: self.ui.pause(uid)
        self.ui.screen(chat, uid, title, text, rows)

    def product_buttons(self, uid, pid):
        account, kinds = self.db.discovery_membership(uid, pid)
        return [[self.action(uid, "★ Убрать из избранного" if "saved" in kinds else "☆ В избранное бота", "pin", product_id=pid, kind_name="saved", add="saved" not in kinds, generation=account["generation"], revision=account["saved_revision"]),
                 self.action(uid, "В сравнении · открыть" if "compare" in kinds else "+ Сравнить", "open_compare" if "compare" in kinds else "pin", product_id=pid, kind_name="compare", add=True, generation=account["generation"], revision=account["compare_revision"])],
                [self.action(uid, "Характеристики полностью", "facts", product_id=pid, page=0), button("Подбор / фильтры", "d:browse")]]

    @staticmethod
    def description(filters, categories):
        values = []
        if filters["scope"] is not None: values.append("Совпадения по словам из описаний")
        if filters["category"]: values.append("Раздел: " + categories.get(filters["category"], "больше не доступен"))
        if filters["size"]: values.append("Размер: " + filters["size"])
        if filters["min_price"] is not None: values.append("От " + format_rub(filters["min_price"]))
        if filters["max_price"] is not None: values.append("До " + format_rub(filters["max_price"]))
        values += [STOCK_NAMES[filters["stock"]], SORT_NAMES[filters["sort"]]]
        return "\n".join(esc(x) for x in values)

    def browse(self, chat, uid, page=0):
        self.db.expire_reservations()
        data = self.db.browse_discovery(uid, self.catalog, page)
        s = data["session"]
        text = "<b>Найди свою вещь.</b>\n" + self.description(s["filters"], data["categories"])
        text += f"\n\nНайдено: <b>{data['total']}</b>. Цена и наличие проверяются по текущим данным."
        if data["stale"]: text += "\n\n<b>Описание или состав витрины изменились.</b> Повтори текстовый поиск. Старые совпадения не выдаём за актуальные."
        elif not data["items"]: text += "\n\nПод эти условия ничего нет. Можно изменить фильтры или начать новый поиск; другой размер не подставляем автоматически."
        rows = [[button("Найти словами…", "d:search"), button("Фильтры", "d:filters")]]
        for p in data["items"]:
            text += f"\n\n<b>{short(p['name'], 65)}</b> · {esc(p['price_label'])}\n{esc(STOCK_NAMES[p['stock']])}"
            rows.append([self.action(uid, p["name"][:45] + " · " + p["price_label"], "product", product_id=p["product_id"])])
        nav = []
        if data["page"]: nav.append(button("← Предыдущие", f"d:browse:{data['page'] - 1}"))
        if data["has_more"]: nav.append(button("Ещё вещи →", f"d:browse:{data['page'] + 1}"))
        if nav: rows.append(nav)
        rows += [[self.action(uid, "Сбросить подбор", "reset", generation=s["generation"], revision=s["revision"]), button("Избранное / сравнение", "d:lists")],
                 [button("Корзина", "c:cart"), button("Главная", "c:home")]]
        text += "\n\nПоиск не резервирует вещи. Параметры живут до 24 часов; исходный текст не записываем в БД."
        self.screen(chat, uid, "ВЫПУСК / ПОДБОР", text, rows)

    def filters(self, chat, uid, field=None, page=0):
        data = self.db.browse_discovery(uid, self.catalog)
        s, f = data["session"], data["session"]["filters"]
        meta = {"generation": s["generation"], "revision": s["revision"]}
        if field is None:
            text = "<b>Твой подбор</b>\n" + self.description(f, data["categories"])
            text += "\n\nСначала выбери размер, чтобы наличие относилось именно к нему. Без размера проверяется доступность вещи в целом. Неизвестный остаток не равен нулю."
            rows = [[button("Размер", "d:filter:size:0"), button("Наличие", "d:filter:stock:0")],
                    [button("Раздел витрины", "d:filter:category:0"), button("Сортировка", "d:filter:sort:0")],
                    [button("Цена от…", "d:filter:min_price:0"), button("Цена до…", "d:filter:max_price:0")],
                    [self.action(uid, "Сбросить цену", "filter", changes={"min_price": None, "max_price": None}, **meta)],
                    [button("Показать результат", "d:browse"), button("Найти словами", "d:search")]]
        else:
            options = {
                "size": [(None, "Любой размер")] + [(s, s) for s in data["sizes"]],
                "category": [(None, "Все разделы")] + list(data["categories"].items()),
                "stock": list(STOCK_NAMES.items()), "sort": list(SORT_NAMES.items()),
                "min_price": [(None, "Без нижней границы")] + [(p, "От " + format_rub(p)) for p in data["prices"]],
                "max_price": [(None, "Без верхней границы")] + [(p, "До " + format_rub(p)) for p in data["prices"]],
            }
            if field not in options: raise ValueError("Неизвестный фильтр.")
            values = options[field]
            page = max(0, min(page, max(0, (len(values) - 1) // 6)))
            text = "<b>Выбери условие.</b>\nРазмеры, разделы и ценовые подсказки берём из действующей витрины. Для своей цены: /find футболка до 5000. Границы включаются в диапазон."
            rows = [[self.action(uid, ("✓ " if value == f[field] else "") + label, "filter", changes={field: value}, **meta)] for value, label in values[page * 6:(page + 1) * 6]]
            nav = []
            if page: nav.append(button("← Назад", f"d:filter:{field}:{page - 1}"))
            if (page + 1) * 6 < len(values): nav.append(button("Ещё →", f"d:filter:{field}:{page + 1}"))
            if nav: rows.append(nav)
            rows.append([button("← Все фильтры", "d:filters")])
        self.screen(chat, uid, "ПОДБОР / ФИЛЬТРЫ", text, rows)

    def prompt(self, chat, uid, *, resume=False):
        s = self.db.discovery_session(uid)
        if resume:
            state = self.db.get_state(uid)
            self.db._discovery_version(s, state[1]["generation"], state[1]["revision"])
        self.db.set_state(uid, "disc_input", {"generation": s["generation"], "revision": s["revision"]})
        self.screen(chat, uid, "ПОДБОР / ПОИСК", "<b>Какую вещь ищем?</b>\nНапример: чёрная футболка M до 5000 в наличии.\n\nМожно название, слова из материала/описания, один размер, «от»/«до» в целых рублях. Это прозрачный поиск по каталогу, не AI-стилист: неподтверждённое условие не отбрасываем молча.\n\nОдно сообщение до 160 символов. Не присылай телефон, адрес или банковские данные. В БД сам текст не сохраняем; параметры и совпадения — до 24 часов. На паузе сообщения не попадают в поиск.",
            [[button("Кнопки фильтров", "d:filters"), button("Пауза · главная", "c:home")]], pause=False)

    def product(self, chat, uid, pid):
        p = self.db.discovery_product(uid, pid, self.catalog)
        text = f"<b>{short(p['name'])}</b>\n{esc(p['price_label'])}\n\n{short(p['description'], 450)}\n\n"
        for s in p["sizes"][:10]: text += f"{esc(s['size'])} · " + ("уточняем" if s["available"] is None else f"{s['available']} шт. свободно") + "\n"
        if len(p["sizes"]) > 10: text += "Остальные размеры — в полных характеристиках.\n"
        text += "\nДобавление в корзину ещё не создаёт резерв и оплату."
        rows = []
        if p["price_rub"] is not None: rows.append([button("Выбрать размер →", f"c:sizes:{pid}")])
        rows += self.product_buttons(uid, pid)
        rows += [[button("Уведомления о вещи", "a:item:" + pid)]]
        rows += [[button("Фото и детали", f"product:{pid}"), button("Ждать размер", f"wait:{pid}")],
                 [button("← К результатам", "d:browse"), button("Корзина", "c:cart")]]
        self.screen(chat, uid, "ПОДБОР / ВЕЩЬ", text, rows)

    def facts(self, chat, uid, pid, page=0):
        p = self.db.discovery_product(uid, pid, self.catalog)
        variants = "\n".join(s["size"] + " · " + ("наличие уточняется" if s["available"] is None else str(s["available"]) + " шт. свободно") for s in p["sizes"])
        plain = p["name"] + "\n" + p["price_label"] + "\n\nМатериал: " + (p["material"] or "не указан в каталоге") + "\nПосадка: " + (p["fit"] or "не указана в каталоге")
        plain += "\n\n" + p["description"] + "\n\n" + "\n".join(p["details"]) + "\n\nРазмеры и наличие:\n" + variants
        plain += "\n\nТочные замеры модели не подключены к этому экрану. Посадку по росту/весу не угадываем. Характеристики — сведения продавца в каталоге, не независимая экспертиза."
        pages = max(1, math.ceil(len(plain) / 650)); page = max(0, min(page, pages - 1))
        rows = []
        nav = []
        if page: nav.append(self.action(uid, "← Предыдущая часть", "facts", product_id=pid, page=page - 1))
        if page + 1 < pages: nav.append(self.action(uid, "Дальше по характеристикам →", "facts", product_id=pid, page=page + 1))
        if nav: rows.append(nav)
        rows.append([self.action(uid, "← Вещь", "product", product_id=pid), button("Вопрос о размере", "o:support")])
        self.screen(chat, uid, "ВЕЩЬ / ХАРАКТЕРИСТИКИ", f"<b>Полный текст · {page + 1}/{pages}</b>\n\n{esc(plain[page * 650:(page + 1) * 650])}", rows)

    def consent(self, chat, uid, next_item=None):
        account = self.db.discovery_account(uid)
        text = "<b>Сохранять выбранные вещи в аккаунте бота?</b>\nИзбранное и сравнение будут связаны с твоим Telegram ID и доступны после перезапуска. Сохраняем только ссылки на товары, не поисковые сообщения. Избранное Mini App хранится отдельно и сюда автоматически не переносится.\n\nЭто отдельное разрешение на списки — не согласие на рекламу, не подписка на наличие и не согласие на сохранение контактов. Само сохранение списков не включает уведомления; отдельная настройка — /alerts. В настройках можно удалить оба списка и отключить хранение."
        data = {"enabled": True, "generation": account["generation"]}
        if next_item:
            data["next_item"] = next_item
            text += "\n\nПосле подтверждения добавим выбранную вещь в " + ("избранное." if next_item["kind_name"] == "saved" else "сравнение.")
        rows = [[self.action(uid, "Разрешить и добавить вещь" if next_item else "Разрешить хранение списков", "consent", **data)], [button("Без сохранения · подбор", "d:browse")]]
        self.screen(chat, uid, "СПИСКИ / СОГЛАСИЕ", text, rows)

    def lists(self, chat, uid):
        account = self.db.discovery_account(uid)
        if not account["enabled"]: return self.consent(chat, uid)
        self.screen(chat, uid, "АККАУНТ / ВЫБРАННЫЕ ВЕЩИ", "<b>Не потерять выбор.</b>\nИзбранное — до 50 вещей; сравнение — до трёх. Цена, наличие и публикация перечитываются из каталога. Список не резервирует товар.\n\nЭто серверные списки бота. Локальное избранное Mini App автоматически сюда не переносится. Списки не включают уведомления или рекламу. Конкретные сигналы настраиваются отдельно через /alerts.",
            [[button("Избранное в боте", "d:saved"), button("Сравнение вещей", "d:compare")], [button("Управление хранением", "d:settings"), button("Подбор", "d:browse")]])

    def settings(self, chat, uid):
        a = self.db.discovery_account(uid)
        if not a["enabled"]: return self.consent(chat, uid)
        self.screen(chat, uid, "СПИСКИ / ХРАНЕНИЕ", "<b>Хранение избранного и сравнения включено.</b>\nСписок видишь только ты. Владелец магазина не получает особую кнопку для чтения твоего списка.\n\nОтключение удалит оба списка, текущие параметры поиска и ссылки старых действий выбора. Корзина, профиль, покупки и платёжная история не изменятся. Финансовый/служебный аудит и резервные копии не стираются этой кнопкой. Отдельные подписки на сигналы здесь не отключаются — управление через /alerts.",
            [[self.action(uid, "Удалить выбор и отключить…", "forget_check", generation=a["generation"])], [button("← Мои списки", "d:lists")]])

    def saved(self, chat, uid, page=0):
        if not self.db.discovery_account(uid)["enabled"]: return self.consent(chat, uid)
        data = self.db.collection_view(uid, "saved", self.catalog, page)
        a = data["account"]
        text = f"<b>Избранное в боте · {data['total']}/50</b>\nСохранено в твоём аккаунте. Не является резервом."
        rows = []
        for index, p in enumerate(data["items"], data["page"] * 5 + 1):
            text += f"\n\n{index:02d} · <b>{short(p['name'], 65)}</b>"
            if p["active"]:
                text += f"\n{esc(p['price_label'])} · {esc(STOCK_NAMES[stock_state(p['sizes'])])}"
                rows.append([self.action(uid, f"Открыть вещь {index}", "product", product_id=p["product_id"])])
            rows.append([self.action(uid, f"Убрать из избранного {index}", "pin", product_id=p["product_id"], kind_name="saved", add=False, generation=a["generation"], revision=a["saved_revision"])])
        if not data["items"]: text += "\n\nПока пусто. Сохрани вещь кнопкой ☆ в карточке."
        nav = []
        if data["page"]: nav.append(button("← Назад", f"d:saved:{data['page'] - 1}"))
        if data["has_more"]: nav.append(button("Ещё избранное →", f"d:saved:{data['page'] + 1}"))
        if nav: rows.append(nav)
        rows += [[button("Подбор", "d:browse"), button("Сравнение", "d:compare")], [button("Мои уведомления", "a:home")], [button("Настройки списков", "d:settings"), button("Главная", "c:home")]]
        self.screen(chat, uid, "АККАУНТ / ИЗБРАННОЕ", text, rows)

    def compare(self, chat, uid, axis=0):
        if not self.db.discovery_account(uid)["enabled"]: return self.consent(chat, uid)
        data = self.db.collection_view(uid, "compare", self.catalog)
        axis = max(0, min(axis, len(AXES) - 1)); a = data["account"]
        text = f"<b>Сравнение · {data['total']}/3</b>\n{AXES[axis]}\n"
        if data["total"] < 2: text += "\nДобавь хотя бы две вещи кнопкой «+ Сравнить» в карточке."
        rows = []
        for index, p in enumerate(data["items"], 1):
            text += f"\n\n<b>{index:02d} / {short(p['name'], 65)}</b>\n"
            if p["active"]:
                values = [p["price_label"] + " · " + STOCK_NAMES[stock_state(p["sizes"])], p["price_label"] + " · без доставки",
                          p["material"] or "Нет данных о материале", p["fit"] or "Нет данных о посадке",
                          "; ".join(s["size"] + ": " + ("уточняем" if s["available"] is None else str(s["available"]) + " шт.") for s in p["sizes"]),
                          p["description"] + "\n" + "; ".join(p["details"])]
                text += short(values[axis], 480)
                rows.append([self.action(uid, f"Открыть вещь {index}", "product", product_id=p["product_id"]), self.action(uid, f"Полные данные {index}", "facts", product_id=p["product_id"], page=0)])
            else: text += "Недоступна. Характеристики скрытой карточки не показываются."
            rows.append([self.action(uid, f"Убрать из сравнения {index}", "pin", product_id=p["product_id"], kind_name="compare", add=False, generation=a["generation"], revision=a["compare_revision"])])
        text += "\n\nСравнение не выбирает победителя и не обещает посадку. Разные типы вещей могут быть несопоставимы. Длинные сведения показаны сокращённо; полный текст — в «Полные данные»."
        rows += [[button("← Другой параметр", f"d:compare:{(axis - 1) % len(AXES)}"), button(AXES[(axis + 1) % len(AXES)] + " →", f"d:compare:{(axis + 1) % len(AXES)}")],
                 [button("Добавить из подбора", "d:browse"), button("Избранное", "d:saved")], [button("Настройки списков", "d:settings"), button("Главная", "c:home")]]
        self.screen(chat, uid, "ВЫПУСК / СРАВНЕНИЕ", text, rows)

    def dispatch(self, chat, uid, token):
        kind, d = self.db.chat_action(uid, token)
        if not kind.startswith("discovery."): raise ValueError("Это другая кнопка.")
        kind = kind.removeprefix("discovery.")
        if kind == "product": self.product(chat, uid, d["product_id"])
        elif kind == "facts": self.facts(chat, uid, d["product_id"], d.get("page", 0))
        elif kind == "open_compare": self.compare(chat, uid)
        elif kind in {"filter", "reset"}:
            self.db.filter_discovery(uid, d.get("changes", {}), d["generation"], d["revision"], token, self.catalog, reset=kind == "reset")
            self.browse(chat, uid)
        elif kind == "pin":
            account = self.db.discovery_account(uid)
            if not account["enabled"]:
                if not d["add"]: raise ValueError("Хранение списков отключено.")
                if account["generation"] != d["generation"]: raise ValueError("Старый выбор больше не действует. Открой вещь заново.")
                self.db.discovery_product(uid, d["product_id"], self.catalog)
                self.consent(chat, uid, {"product_id": d["product_id"], "kind_name": d["kind_name"]})
                return
            self.db.change_collection(uid, d["kind_name"], d["product_id"], d["add"], d["generation"], d["revision"], token, self.catalog)
            self.saved(chat, uid) if d["kind_name"] == "saved" else self.compare(chat, uid)
        elif kind == "consent":
            with self.catalog.lock, self.db.commerce_transaction():
                is_new = self.db.service_result(uid, token) is None
                if is_new and d.get("next_item"): self.db.discovery_product(uid, d["next_item"]["product_id"], self.catalog)
                account = self.db.discovery_consent(uid, d["enabled"], d["generation"], token)
                if is_new and d.get("next_item"):
                    n = d["next_item"]
                    self.db.change_collection(uid, n["kind_name"], n["product_id"], True, account["generation"], account[n["kind_name"] + "_revision"], token + "-item", self.catalog)
            if d.get("next_item", {}).get("kind_name") == "saved": self.saved(chat, uid)
            elif d.get("next_item", {}).get("kind_name") == "compare": self.compare(chat, uid)
            else: self.lists(chat, uid)
        elif kind == "forget_check":
            self.screen(chat, uid, "СПИСКИ / ПОДТВЕРЖДЕНИЕ", "<b>Удалить избранное, сравнение и параметры поиска?</b>\nСписки будут очищены, хранение отключено. Старое согласие и старые кнопки не восстановят удалённый выбор. Корзина и покупки останутся.", [[self.action(uid, "Да, удалить и отключить", "consent", enabled=False, generation=d["generation"])], [button("Оставить списки", "d:lists")]])
        else: raise ValueError("Кнопка больше не поддерживается.")

    def error(self, chat, uid, exc, *, pause_other=True):
        state = self.db.get_state(uid)
        # A rejected search must not leave a contact/support/finance wizard
        # collecting the user's next attempt. Only the current discovery input
        # stays open for an explicit retry; unrelated work is paused, not lost.
        keep_input = state and state[0] == "disc_input" and not state[1].get("paused")
        self.screen(chat, uid, "ПОДБОР / НУЖНО ВНИМАНИЕ", esc(str(exc)), [[button("Новый поиск", "d:search"), button("Актуальный подбор", "d:browse")], [button("Мои списки", "d:lists"), button("Главная", "c:home")]], pause=pause_other and not keep_input)

    def handle_callback(self, chat, user, data):
        if not data.startswith("d:"): return False
        uid = int(user["id"])
        try:
            p = data.split(":")
            if p[1] == "a": self.dispatch(chat, uid, p[2])
            elif p[1] == "browse": self.browse(chat, uid, int(p[2]) if len(p) > 2 else 0)
            elif p[1] == "search": self.prompt(chat, uid)
            elif p[1] == "filters": self.filters(chat, uid)
            elif p[1] == "filter": self.filters(chat, uid, p[2], int(p[3]))
            elif p[1] == "lists": self.lists(chat, uid)
            elif p[1] == "settings": self.settings(chat, uid)
            elif p[1] == "saved": self.saved(chat, uid, int(p[2]) if len(p) > 2 else 0)
            elif p[1] == "compare": self.compare(chat, uid, int(p[2]) if len(p) > 2 else 0)
            else: raise ValueError("Открой актуальный подбор.")
        except (ValueError, PermissionError, KeyError, IndexError, TypeError) as exc: self.error(chat, uid, exc)
        return True

    def resume(self, chat, uid):
        state = self.db.get_state(uid)
        if not state or not state[0].startswith("disc_"): return False
        try: self.prompt(chat, uid, resume=True)
        except (ValueError, PermissionError, KeyError) as exc: self.error(chat, uid, exc)
        return True

    def handle_message(self, chat, user, message):
        uid = int(user["id"])
        text = str(message.get("text", "")).strip()
        command = (text.split(maxsplit=1)[0] if text else "").split("@", 1)[0].lower()
        mid = message.get("message_id")
        operation = f"disc-input:{chat}:{mid}"
        query_hash = hashlib.sha256(text.encode()).hexdigest()
        prior = self.db.service_result(uid, operation) if type(mid) is int and mid > 0 else None
        if prior and prior["kind"] == "discovery_input":
            try: self.db._service_request(uid, operation, "discovery_input", [query_hash])
            except ValueError as exc: self.error(chat, uid, exc, pause_other=False)
            return True  # Never feed an old search into a newer contact/support draft.
        routes = {"/browse": "d:browse", "/saved": "d:saved", "/compare": "d:compare"}
        if command in routes or (command == "/find" and len(text.split(maxsplit=1)) == 1):
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
            return self.handle_callback(chat, user, routes.get(command, "d:search"))
        state = self.db.get_state(uid)
        if state and state[0].startswith("disc_") and (command == "/cancel" or text.lower() in {"отмена", "cancel"}):
            self.db.clear_state(uid); self.browse(chat, uid); return True
        direct = command == "/find"
        if not direct and (not state or state[0] != "disc_input" or command.startswith("/")): return False
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
        try:
            if not direct and state[1].get("paused"):
                self.ui.home(chat, uid); return True
            if type(mid) is not int or mid < 1: raise ValueError("Пришли поиск новым сообщением Telegram.")
            with self.catalog.lock, self.db.commerce_transaction():
                _, fp = self.db._service_request(uid, operation, "discovery_input", [query_hash])
                s = self.db.discovery_session(uid) if direct else state[1]
                query = text.split(maxsplit=1)[1] if direct else text
                self.db.search_discovery(uid, query, s["generation"], s["revision"], operation + "-query", self.catalog)
                current = self.db.get_state(uid)
                if current and current[0] == "disc_input" and current[1].get("generation") == s["generation"] and current[1].get("revision") == s["revision"]:
                    self.db.clear_state(uid)
                self.db._service_done(uid, operation, "discovery_input", fp, 0)
            self.browse(chat, uid)
        except (ValueError, PermissionError, KeyError, TypeError) as exc: self.error(chat, uid, exc)
        return True
