"""Native Telegram shopping/operations screens. No Telegram credentials or I/O on import.

Persistent, owner-bound action tokens keep callbacks short and stale buttons safe.
Checkout previews freeze the cart version, price and customer snapshot; receipts
remain in the original payment ledger. The simulator uses this same controller.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone

from commerce_store import CartConflict, FULFILLMENT_LABELS, ROLE_PERMISSIONS, encode
from payments import format_rub, PriceError


ROLE_NAMES = {"owner": "Владелец", "manager": "Менеджер", "warehouse": "Склад", "support": "Поддержка", "finance": "Финансы"}
FIELD_NAMES = {"name": "Имя получателя", "phone": "Телефон", "city": "Город", "address": "Адрес или пункт выдачи", "note": "Комментарий"}


def esc(value) -> str:
    return html.escape(str(value))


def button(text: str, callback: str) -> dict:
    if not 1 <= len(callback.encode("utf-8")) <= 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    return {"text": text, "callback_data": callback}


class CommerceBot:
    def __init__(self, bot):
        self.bot, self.db, self.api, self.catalog = bot, bot.db, bot.api, bot.catalog
        self.owners = bot.settings.admin_ids

    def action(self, user_id: int, text: str, kind: str, **data) -> dict:
        return button(text, self.db.create_chat_action(user_id, kind, data))

    def role(self, user_id: int) -> str:
        return self.db.staff_role(user_id, self.owners)

    def allowed(self, user_id: int, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role(user_id), set())

    def screen(self, chat_id: int, user_id: int, title: str, text: str, rows: list, *, new: bool = False) -> None:
        body = f"<b>{esc(self.bot.settings.brand_name)}</b>\n<code>{esc(title)}</code>\n\n{text}"
        if len(html.unescape(re.sub(r"<[^>]*>", "", body)).encode("utf-16-le")) // 2 > 3900:
            raise ValueError("Экран слишком большой. Открой другую страницу.")
        markup = {"inline_keyboard": rows}
        panel = self.db.connection().execute("SELECT * FROM chat_panels WHERE user_id=? AND chat_id=?", (user_id, chat_id)).fetchone()
        if panel and not new:
            try:
                self.api.edit_message_text(chat_id, panel["message_id"], body, markup)
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                # A deleted/uneditable panel is harmless: open a fresh screen.
        result = self.api.send_message(chat_id, body, markup)
        if isinstance(result, dict) and type(result.get("message_id")) is int:
            self.db.connection().execute("INSERT INTO chat_panels VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id",
                                         (user_id, chat_id, result["message_id"]))

    def pause(self, user_id: int) -> None:
        state = self.db.get_state(user_id)
        if state and state[0].startswith(("commerce_", "ops_", "fin_", "disc_", "growth_", "watch_")):
            self.db.set_state(user_id, state[0], {**state[1], "paused": True})

    def home(self, chat_id: int, user_id: int, *, new=False) -> None:
        self.pause(user_id)
        cart = self.db.cart(user_id)
        count = sum(x["quantity"] for x in cart["items"])
        recent = self.db.purchases_for_user(user_id)
        text = "<b>Твой выпуск. Твой выбор.</b>\nСобери вещи прямо здесь — без переходов и повторного ввода данных."
        if count:
            text += f"\n\nВ корзине <b>{count} шт.</b> Сохранили состав и размеры."
        if recent:
            text += f"\nПоследняя покупка: <b>№{recent[0]['number']}</b> · {esc(recent[0]['status_label'])}."
        rows = [[button("Смотреть выпуск", "c:catalog"), button(f"Корзина · {count}", "c:cart")],
                [button("Мои покупки", "c:orders"), button("Мой профиль", "c:profile")],
                [button("Размеры и посадка", "size_guide"), button("Поддержка в боте", "o:support")],
                [button("Мои обращения", "o:tickets:all:0"), button("Помощь", "c:help")]]
        rows.insert(1, [button("Подобрать вещь", "d:browse"), button("Избранное / сравнение", "d:lists")])
        if self.db.get_state(user_id):
            rows.insert(0, [button("Продолжить с места остановки →", "c:resume")])
        url = self.bot.settings.webapp_url
        if url.startswith("https://"):
            rows.append([{"text": "Открыть Mini App ↗", "web_app": {"url": url}}])
        rows.append([button("Мои уведомления", "a:home"), button("Бренд / образы / клуб", "c:more")])
        if self.role(user_id):
            rows.append([button(f"Рабочее место · {ROLE_NAMES[self.role(user_id)]}", "c:team")])
        self.screen(chat_id, user_id, "МАГАЗИН / ГЛАВНАЯ", text, rows, new=new)

    def catalog_view(self, chat_id, user_id, page=0):
        products = self.catalog.public_products()
        page = max(0, min(page, max(0, (len(products) - 1) // 6)))
        selected = products[page * 6:page * 6 + 6]
        rows = [[button(f"{p['name'][:45]} · {p['price']}", f"c:product:{p['id']}")] for p in selected]
        if page:
            rows.append([button("← Предыдущие", f"c:catalog:{page - 1}")])
        if len(products) > (page + 1) * 6:
            rows.append([button("Ещё вещи →", f"c:catalog:{page + 1}")])
        rows.append([button("Поиск / фильтры", "d:browse"), button("Избранное", "d:saved")])
        rows.append([button("Корзина", "c:cart"), button("Главная", "c:home")])
        self.db.event(user_id, "chat_catalog_open", {"page": page})
        self.screen(chat_id, user_id, "МАГАЗИН / ВЫПУСК", "<b>Выбирай свою вещь.</b>\nВ карточке — размеры, подтверждённое наличие и добавление в общую корзину." if selected else "Выпуск готовится. В корзине и покупках ничего не потеряется.", rows)

    def product_view(self, chat_id, user_id, product_id):
        product = self.catalog.get(product_id)
        if not product:
            raise ValueError("Вещь больше не доступна. Открой выпуск заново.")
        inventory = self.db.inventory_view().get(product_id, {})
        variants = []
        for size in product["sizes"]:
            free = inventory.get(size, {}).get("available")
            variants.append(f"{esc(size)} · " + ("уточняем" if free is None else f"{free} шт." if free else "нет в наличии"))
        text = f"<b>{esc(product['name'][:100])}</b>\n{esc(format_rub(self.bot.line_amount(product, 1)))}\n\n{esc(product['description'][:750])}\n\n" + "\n".join(variants[:12])
        rows = [[button("Выбрать размер →", f"c:sizes:{product_id}")],
                [button("Фото и детали", f"product:{product_id}"), button("Ждать размер", f"wait:{product_id}")],
                [button("← Выпуск", "c:catalog"), button("Корзина", "c:cart")]]
        rows[2:2] = self.bot.discovery.product_buttons(user_id, product_id)
        rows.append([button("Уведомления о вещи", "a:item:" + product_id)])
        self.screen(chat_id, user_id, "ВЫПУСК / ВЕЩЬ", text, rows)

    def sizes_view(self, chat_id, user_id, product_id, *, line_id=None, revision=None):
        product = self.catalog.get(product_id)
        if not product:
            raise ValueError("Вещь больше не доступна.")
        cart = self.db.cart(user_id)
        buttons = []
        for size in product["sizes"]:
            kind = "swap" if line_id is not None else "add"
            buttons.append(self.action(user_id, size, kind, product_id=product_id, size=size,
                                       revision=cart["revision"] if revision is None else revision, line_id=line_id))
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([button("← Корзина" if line_id else "← Вещь", "c:cart" if line_id else f"c:product:{product_id}")])
        self.screen(chat_id, user_id, "ВЕЩЬ / РАЗМЕР", f"<b>{esc(product['name'][:100])}</b>\nВыбери размер. Сначала добавим в корзину — это ещё не покупка и не резерв.", rows)

    def cart_view(self, chat_id, user_id, page=0, notice=""):
        cart = self.db.cart(user_id)
        items = cart["items"]
        page = max(0, min(page, max(0, (len(items) - 1) // 4)))
        total, invalid = 0, False
        for item in items:
            product = self.catalog.get(item["product_id"])
            try:
                if not product or item["size"] not in product["sizes"]:
                    raise PriceError("Недоступен")
                total += self.bot.line_amount(product, item["quantity"])
            except PriceError:
                invalid = True
        text = (esc(notice) + "\n\n") if notice else ""
        rows = []
        inventory = self.db.inventory_view()
        for index, item in enumerate(items[page * 4:page * 4 + 4], page * 4 + 1):
            product = self.catalog.get_any(item["product_id"])
            name = product["name"] if product else item["product_id"]
            free = inventory.get(item["product_id"], {}).get(item["size"], {}).get("available")
            stock = "наличие уточняется" if free is None else f"доступно {free} шт." if free < item["quantity"] else "наличие подтверждено"
            text += f"<b>{index:02d} / {esc(name[:70])}</b>\n{esc(item['size'])} · {item['quantity']} шт. · {stock}"
            if item["person"]:
                text += f"\nПерсонализация: {esc(item['person'])}"
            text += "\n\n"
            params = {"line_id": item["line_id"], "revision": cart["revision"], "page": page}
            rows.append([self.action(user_id, f"− {index}", "quantity", quantity=item["quantity"] - 1, **params),
                         self.action(user_id, f"+ {index}", "quantity", quantity=min(20, item["quantity"] + 1), **params),
                         self.action(user_id, f"Убрать {index}", "quantity", quantity=0, **params)])
            edits = [self.action(user_id, f"Размер {index}", "sizes", **params)]
            if product and product.get("personalization"):
                edits.append(self.action(user_id, f"Персонализация {index}", "person", **params))
            rows.append(edits)
        if not items:
            text += "<b>Здесь начинается твой комплект.</b>\nДобавь вещь из выпуска. Корзина сохранится, даже если закроешь Telegram."
        else:
            text += "<b>Товары: " + ("нужно обновить недоступные позиции" if invalid else esc(format_rub(total))) + "</b>\n"
            text += "Доставка отдельно согласуется с менеджером и в этот счёт не входит.\nРезерв на 60 минут — после подтверждения покупки."
            pages = []
            if page:
                pages.append(button("←", f"c:cart:{page - 1}"))
            if len(items) > (page + 1) * 4:
                pages.append(button("Ещё позиции →", f"c:cart:{page + 1}"))
            if pages:
                rows.append(pages)
            if cart["promotion"]:
                text += "\n\nПромокод: <b>" + esc(cart["promotion"]["code"]) + "</b>\n"
                try:
                    text += self.bot.growth.totals(self.db.growth_quote(user_id, self.catalog))
                except ValueError as exc:
                    invalid = True
                    text += "⚠ " + esc(str(exc))
            if not invalid:
                rows.append([button("Оформить покупку →", "c:checkout")])
            rows.append([button("Промокод / правила", "g:promo")])
            rows.append([self.action(user_id, "Очистить корзину…", "clear_confirm", revision=cart["revision"])])
        rows.append([button("+ Добавить вещь", "c:catalog"), button("Главная", "c:home")])
        self.screen(chat_id, user_id, f"МАГАЗИН / КОРЗИНА · {sum(x['quantity'] for x in items)} ШТ.", text, rows)

    def mutate(self, user_id, kind, data, token):
        cart = self.db.cart(user_id)
        items = [{k: v for k, v in x.items() if k != "line_id"} for x in cart["items"]]
        # Persisted absolute operations plus CAS, not an unguarded += 1. A
        # repeated old callback must never become a second mutation.
        seen = self.db.connection().execute("SELECT 1 FROM cart_mutations WHERE user_id=? AND operation_id=?", (user_id, "chat-" + token)).fetchone()
        if seen:
            return cart
        if cart["revision"] != data["revision"]:
            raise CartConflict("Корзина уже изменена. Нажми «Корзина» и продолжи с актуального состава.")
        if kind == "add":
            items.append({"product_id": data["product_id"], "size": data["size"], "quantity": 1, "person": ""})
        elif kind == "clear":
            items = []
        else:
            index = next((i for i, x in enumerate(cart["items"]) if x["line_id"] == data["line_id"]), None)
            if index is None:
                raise ValueError("Позиция больше не в корзине.")
            if kind == "quantity":
                if data["quantity"] == 0:
                    items.pop(index)
                else:
                    items[index]["quantity"] = data["quantity"]
            elif kind == "swap":
                items[index]["size"] = data["size"]
            elif kind == "personalize":
                items[index]["person"] = data["person"]
        result = self.db.replace_cart(user_id, items, cart["revision"], "chat-" + token, self.catalog)
        self.db.clear_state(user_id)
        return result

    def add_variant(self, chat_id, user_id, product_id, size, request_id):
        import hashlib
        token = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        self.mutate(user_id, "add", {"product_id": product_id, "size": size, "revision": self.db.cart(user_id)["revision"]}, token)
        self.cart_view(chat_id, user_id, notice="Добавили в корзину. Оплата — только после проверки покупки.")

    def profile_view(self, chat_id, user_id):
        profile = self.db.account_profile(user_id)
        text = "<b>Данные для следующей покупки.</b>\nИзменение профиля не меняет уже оформленные покупки.\n\n"
        for key, label in FIELD_NAMES.items():
            text += f"{label}: {esc(profile.get(key) or 'не указано')}\n"
        text += f"Доставка: {esc(profile.get('deliver') or 'не выбрана')}"
        rows = [[button("Изменить данные", "c:profile:edit"), button("Доставка", "c:delivery:profile")],
                [button("← Главная", "c:home"), button("Корзина", "c:cart")]]
        self.screen(chat_id, user_id, "АККАУНТ / ПРОФИЛЬ", text, rows)

    def consent(self, chat_id, user_id, next_kind):
        self.db.set_state(user_id, "commerce_consent", {"next": next_kind})
        text = "Для оформления и связи по покупке нужны имя, телефон и адрес. Они сохранятся в профиле и снимке покупки. До согласия контактные данные не записываем.\n\nЭто согласие на обслуживание покупки, не подписка на рекламные рассылки."
        rows = [[self.action(user_id, "Согласен · продолжить", "consent", next=next_kind)], [button("Не сейчас · главная", "c:home")]]
        if self.bot.settings.privacy_url:
            rows.insert(0, [{"text": "Политика обработки данных ↗", "url": self.bot.settings.privacy_url}])
        self.screen(chat_id, user_id, "ОФОРМЛЕНИЕ / СОГЛАСИЕ", text, rows)

    def ask_field(self, chat_id, user_id, field, next_kind, *, new=False):
        if not self.db.has_consent(user_id):
            self.consent(chat_id, user_id, next_kind)
            return
        self.db.set_state(user_id, "commerce_field", {"field": field, "next": next_kind})
        hints = {"phone": "Например, +79991234567. Можно прислать свой контакт вложением Telegram.",
                 "address": "Улица и дом либо адрес пункта выдачи. Детали доставки отдельно согласует менеджер.",
                 "note": "Напиши пожелания. Один дефис — удалить комментарий."}
        step = f"ДАННЫЕ {('name', 'phone', 'city', 'address').index(field) + 1}/5 / " if next_kind == "checkout" and field != "note" else "ПРОФИЛЬ / "
        self.screen(chat_id, user_id, step + FIELD_NAMES[field].upper(),
                    f"<b>{FIELD_NAMES[field]}</b>\nОтправь одним сообщением.\n{hints.get(field, '')}",
                    [[button("← Изменить другой пункт", "c:fields"), button("Пауза · главная", "c:home")]], new=new)

    def fields_view(self, chat_id, user_id, next_kind="profile"):
        if not self.db.has_consent(user_id):
            self.consent(chat_id, user_id, next_kind)
            return
        rows = [[self.action(user_id, label, "field", field=key, next=next_kind)] for key, label in FIELD_NAMES.items()]
        rows.append([button("← Профиль", "c:profile"), button("К оформлению →", "c:checkout")])
        self.screen(chat_id, user_id, "ПРОФИЛЬ / РЕДАКТИРОВАНИЕ", "Что меняем? Данные уже созданной покупки останутся прежними.", rows)

    def delivery_view(self, chat_id, user_id, next_kind):
        if not self.db.has_consent(user_id):
            self.consent(chat_id, user_id, next_kind)
            return
        rows = [[self.action(user_id, mode, "delivery", deliver=mode, next=next_kind)] for mode in ("СДЭК", "Яндекс Доставка", "Согласовать с менеджером")]
        rows.append([button("← Профиль", "c:profile")])
        self.screen(chat_id, user_id, "ДАННЫЕ 5/5 / ДОСТАВКА" if next_kind == "checkout" else "ПРОФИЛЬ / ДОСТАВКА", "<b>Как удобнее получить?</b>\nВыбор — пожелание. Доступность, тариф и срок подтвердит менеджер. Сейчас оплачиваются только товары; без согласования доставки покупка не отправляется.", rows)

    def checkout(self, chat_id, user_id):
        cart = self.db.cart(user_id)
        if not cart["items"]:
            self.cart_view(chat_id, user_id)
            return
        if not self.db.has_consent(user_id):
            self.consent(chat_id, user_id, "checkout")
            return
        profile = self.db.account_profile(user_id)
        for field in ("name", "phone", "city", "address"):
            if not profile.get(field):
                self.ask_field(chat_id, user_id, field, "checkout")
                return
        if not profile.get("deliver"):
            self.delivery_view(chat_id, user_id, "checkout")
            return
        quote, promotion_token = self.db.growth_checkout_preview(user_id, self.catalog)
        if quote["cart_revision"] != cart["revision"]:
            raise CartConflict("Корзина изменилась. Проверь её перед оформлением.")
        prices = {(x["product_id"], x["size"], x["person"]): x for x in quote["lines"]}
        total, text = quote["total_rub"], "<b>Проверь состав и данные.</b>\n\n"
        for item in cart["items"]:
            product = self.catalog.get(item["product_id"])
            if not product or item["size"] not in product["sizes"]:
                raise ValueError("В корзине есть недоступная вещь. Удали или замени её.")
            amount = prices[(item["product_id"], item["size"], item["person"])]["subtotal_rub"]
            text += f"{esc(product['name'][:45])} · {esc(item['size'])} × {item['quantity']} · {esc(format_rub(amount))}"
            if item["person"]:
                text += f" · {esc(item['person'])}"
            text += "\n"
        if promotion_token:
            text += "\nПромокод <b>" + esc(quote["code"]) + "</b>\n" + self.bot.growth.totals(quote)
            text += "\nПрименение расходуется при создании покупки; отмена его не вернёт.\n"
        text += f"\n<b>Итого за товары: {esc(format_rub(total))}</b>\n\n{esc(profile['name'])} · {esc(profile['phone'])}\n{esc(profile['city'])}, {esc(profile['address'])}\n{esc(profile['deliver'])}"
        if profile.get("note"):
            text += f"\nКомментарий: {esc(profile['note'])}"
        text += "\n\nДоставка в сумму не входит: её стоимость и срок отдельно согласует менеджер до отправки. Персонализацию подтвердим до изготовления.\n\nПосле подтверждения — резерв на 60 минут и выбор оплаты. Наличие проверим атомарно. Само подтверждение деньги не списывает."
        payload = {"type": "order", "consent": True, "customer": {k: v for k, v in profile.items() if k != "consent"},
                   "items": [{k: v for k, v in x.items() if k != "line_id"} for x in cart["items"]],
                   "cart_revision": cart["revision"], "expected_total": total}
        if promotion_token:
            payload["promotion_token"] = promotion_token
        token = self.db.create_checkout_draft(user_id, payload)
        self.db.set_state(user_id, "commerce_preview", {"token": token})
        self.screen(chat_id, user_id, "ОФОРМЛЕНИЕ / ПРОВЕРКА", text,
                    [[button("Всё верно · создать покупку →", f"c:confirm:{token}")],
                     [button("Изменить данные", "c:profile:edit"), button("Изменить состав", "c:cart")]])

    def orders_view(self, chat_id, user_id, page=0):
        self.db.expire_reservations()
        purchases = self.db.purchases_for_user(user_id, max(0, page) * 6)[:6]
        rows = [[button(f"№{x['number']} · {format_rub(x['amount_rub'])} · {x['status_label']}", f"c:purchase:{x['purchase_id']}")] for x in purchases]
        if page:
            rows.append([button("← Новее", f"c:orders:{page - 1}")])
        if len(purchases) == 6:
            rows.append([button("Раньше →", f"c:orders:{page + 1}")])
        rows.append([button("Архив старых заявок", "c:legacy")])
        rows.append([button("← Главная", "c:home"), button("Корзина", "c:cart")])
        self.screen(chat_id, user_id, "АККАУНТ / МОИ ПОКУПКИ", "<b>Одна покупка — все её вещи.</b>\nВыбери карточку: состав, оплата и этап работы в одном месте." if purchases else "Здесь появятся твои покупки. Корзина ещё не заказ — её можно спокойно менять.", rows)

    def purchase_view(self, chat_id, user_id, purchase_id, *, staff=False):
        self.db.expire_reservations()
        p = self.db.purchase_view(purchase_id, **({"actor_id": user_id, "owner_ids": self.owners} if staff else {"user_id": user_id}))
        text = f"<b>ПОКУПКА №{p['number']}</b>\n{esc(p['status_label'].capitalize())}\n\n"
        for item in p["lines"]:
            text += f"{esc(item['name'][:50])} · {esc(item['size'])} × {item['quantity']} · {esc(format_rub(item['amount_rub']))}"
            if item["person"]:
                text += f" · персонализация {esc(item['person'])}"
            text += "\n"
        if p.get("promotion"):
            text += "\nПромокод <b>" + esc(p["promotion"]["code"]) + "</b>\n" + self.bot.growth.totals(p["promotion"])
        text += f"\n<b>Товары: {esc(format_rub(p['amount_rub']))}</b>"
        if p["payment_attention"]:
            text += f"\n\n⚠ {esc(p['payment_attention'])}. Повторно не оплачивай."
        if p["reserved_until"]:
            minutes = max(0, int((p["reserved_until"] - time.time()) / 60))
            text += f"\nРезерв: ещё около {minutes} мин."
        if p.get("customer"):
            c = p["customer"]
            text += f"\n\n{esc(c.get('name', 'Получатель'))} · {esc(c.get('phone', ''))}\n{esc(c.get('city', ''))} {esc(c.get('address', ''))}\n{esc(c.get('deliver', ''))}"
            if c.get("note"):
                text += f"\n{esc(c['note'][:240])}"
        if p["legacy"]:
            text += "\n\nАрхивная покупка: складской резерв не восстановлен из старых данных. Нужна ручная сверка выделенного товара."
        rows = []
        if staff:
            text += "\n\nОтветственный: " + (f"{p['assigned_to']}" if p["assigned_to"] else "не назначен")
            if self.allowed(user_id, "orders.claim") and p["assigned_to"] is None:
                rows.append([self.action(user_id, "Взять в работу", "claim", purchase_id=purchase_id)])
            if (self.allowed(user_id, "orders.write") or self.allowed(user_id, "orders.pack")) and p["payment_status"] == "paid" and not p["payment_attention"]:
                target = {"new": "packing", "packing": "ready", "ready": "completed"}.get(p["fulfillment"])
                if target == "completed" and p["operations_managed"]:
                    target = None  # Receipt is recorded from the delivery card, not an assembly button.
                if target:
                    rows.append([self.action(user_id, "Следующий этап · " + FULFILLMENT_LABELS[target], "stage_confirm", purchase_id=purchase_id, target=target, version=p["version"])])
            if p["assigned_to"] is not None and (p["assigned_to"] == user_id or self.role(user_id) == "owner"):
                rows.append([self.action(user_id, "Вернуть в общую очередь…", "release_confirm", purchase_id=purchase_id, version=p["version"])])
            if self.allowed(user_id, "finance.read"):
                rows.append([button("Финансовый журнал покупки", f"f:purchase:{purchase_id}:0")])
            if self.allowed(user_id, "shipping.read"):
                rows.append([button("Доставка / трек / вручение", f"o:workdelivery:{purchase_id}")])
            if self.allowed(user_id, "support.read"):
                rows.append([button("Очередь поддержки", "o:desk:open:0")])
            rows.append([button("Обновить", f"c:work:{purchase_id}"), button("← Очередь", "c:queue:all:0")])
        else:
            if p["can_pay"]:
                rows.append([self.action(user_id, "Выбрать оплату →", "pay", purchase_id=purchase_id)])
            if p["can_cancel"]:
                rows.append([self.action(user_id, "Отменить неоплаченную покупку…", "cancel_confirm", purchase_id=purchase_id)])
            if p["payment_status"] == "paid" and not p["payment_attention"] and p["fulfillment"] not in {"cancelled", "expired"}:
                rows.append([button("Повторить состав…", f"g:repeat:{purchase_id}")])
            rows.append([button("История оплаты", f"f:history:{purchase_id}:0")])
            rows.append([button("Доставка / условия / трек", f"o:delivery:{purchase_id}")])
            rows.append([button("Вопрос по покупке", f"o:support:{purchase_id}")])
            rows.append([button("Обновить", f"c:purchase:{purchase_id}"), button("Помощь", "c:help")])
            rows.append([button("← Мои покупки", "c:orders"), button("Главная", "c:home")])
        self.screen(chat_id, user_id, "КОМАНДА / ПОКУПКА" if staff else "АККАУНТ / ПОКУПКА", text, rows)

    def team_view(self, chat_id, user_id):
        self.db.require_staff(user_id, "orders.read", self.owners)
        text = f"<b>{ROLE_NAMES[self.role(user_id)]} / рабочее место</b>\nПокупки целиком, отдельный статус денег и сборки, один ответственный. Оплаченные вещи не возвращаются в остаток автоматически."
        rows = [[button("К сборке", "c:queue:work:0"), button("Ждут оплаты", "c:queue:pay:0")],
                [button("Сверка / возврат", "c:queue:review:0"), button("Все покупки", "c:queue:all:0")]]
        if self.role(user_id) == "finance":
            text = "<b>Финансы / рабочее место</b>\nПоступления и финансовые исключения, ответственные и внутренние сроки. Покупки доступны без контактного снимка. Доставка, склад, поддержка и исполнение возвратов этой роли недоступны."
            rows = [[button("Все покупки · без контактов", "c:queue:all:0")]]
        if self.allowed(user_id, "finance.read"):
            rows.insert(0, [button("Финансы · поступления и разбор", "f:home")])
        if self.allowed(user_id, "shipping.read"):
            rows.append([button("Доставка · операционная очередь", "o:shipqueue:agreement:0")])
        if self.allowed(user_id, "support.read"):
            rows.append([button("Поддержка · обращения и ответы", "o:desk:open:0")])
        if self.allowed(user_id, "inventory.read"):
            rows.append([button("Склад · остатки и резервы", "c:stock:0")])
        if self.allowed(user_id, "alerts.monitor"):
            rows.append([button("Уведомления · состояние доставки", "a:health")])
        if self.allowed(user_id, "promotions.write"):
            rows.append([button("Промокоды · правила и лимиты", "g:campaigns:0")])
        if self.allowed(user_id, "staff.write"):
            rows.append([button("Команда / роли", "c:staff"), button("Журнал действий", "c:audit")])
            rows.append([button("Прежние инструменты владельца", "adm:panel")])
        rows.append([button("← Магазин", "c:home")])
        self.screen(chat_id, user_id, "КОМАНДА / ПУЛЬТ", text, rows)

    def queue_view(self, chat_id, user_id, bucket, page):
        self.db.expire_reservations()
        purchases = self.db.staff_purchases(user_id, bucket, max(0, page) * 8, owner_ids=self.owners)
        rows = [[button(f"№{x['number']} · {format_rub(x['amount_rub'])} · {x['status_label']}", f"c:work:{x['purchase_id']}")] for x in purchases]
        if page:
            rows.append([button("← Новее", f"c:queue:{bucket}:{page - 1}")])
        if len(purchases) == 8:
            rows.append([button("Ещё →", f"c:queue:{bucket}:{page + 1}")])
        rows.append([button("← Рабочее место", "c:team")])
        self.screen(chat_id, user_id, "КОМАНДА / ОЧЕРЕДЬ", "Выбери покупку. Повторное нажатие не повторяет её сборку." if purchases else "<b>В этой очереди пока пусто.</b>\nНовые покупки появятся здесь автоматически.", rows)

    def stock_view(self, chat_id, user_id, page=0):
        self.db.require_staff(user_id, "inventory.read", self.owners)
        self.db.sync_inventory(self.catalog)
        stocks = list(self.db.connection().execute("SELECT * FROM stock_items ORDER BY sku_id LIMIT 5 OFFSET ?", (max(0, page) * 5,)))
        text = "<b>Только подтверждённые количества.</b>\nНеизвестно ≠ ноль. Вводим товар для продажи, исключая уже оплаченные и выделенные историческим заказам единицы.\n\n"
        rows = []
        for row in stocks:
            product = self.catalog.get_any(row["product_id"])
            name = product["name"] if product else row["product_id"]
            free = "?" if row["on_hand"] is None else str(row["on_hand"] - row["reserved"])
            archived = "архив · " if not self.catalog.get(row["product_id"]) else ""
            rows.append([button(f"#{row['sku_id']} · {archived}{name[:25]} · {row['size']} · {free} шт.", f"c:sku:{row['sku_id']}")])
        if page:
            rows.append([button("←", f"c:stock:{page - 1}")])
        if len(stocks) == 5:
            rows.append([button("Ещё варианты →", f"c:stock:{page + 1}")])
        rows.append([button("← Рабочее место", "c:team")])
        self.screen(chat_id, user_id, "КОМАНДА / СКЛАД", text, rows)

    def sku_view(self, chat_id, user_id, sku_id):
        self.db.require_staff(user_id, "inventory.read", self.owners)
        row = self.db.stock_by_id(sku_id)
        if not row:
            raise ValueError("Вариант не найден.")
        product = self.catalog.get_any(row["product_id"])
        text = f"<b>{esc((product['name'] if product else row['product_id'])[:80])} · {esc(row['size'])}</b>\n\n"
        text += f"<code>SKU {row['sku_id']} / {esc(row['product_id'])}</code>\n\n"
        text += f"Учётное количество: {row['on_hand'] if row['on_hand'] is not None else 'не подтверждено'}\nВ активных резервах: {row['reserved']}\nСвободно: {row['on_hand'] - row['reserved'] if row['on_hand'] is not None else 'неизвестно'}\nВерсия: {row['version']}"
        text += "\n\nНовый счёт резервирует товар. Подтверждённая оплата списывает его ровно один раз. Истечение или неоплаченная отмена освобождает резерв; возврат денег не означает возврат вещи на склад."
        rows = []
        if self.allowed(user_id, "inventory.write"):
            rows.append([self.action(user_id, "Ввести подтверждённое количество…", "count_input", sku_id=sku_id, version=row["version"])])
        rows.append([button("Обновить", f"c:sku:{sku_id}"), button("← Склад", "c:stock:0")])
        self.screen(chat_id, user_id, "СКЛАД / ВАРИАНТ", text, rows)

    def staff_view(self, chat_id, user_id):
        self.db.require_staff(user_id, "staff.write", self.owners)
        text = "<b>Доступ по задаче, не по чату уведомлений.</b>\nВладелец задаётся в ADMIN_IDS. Менеджер — покупки; склад — остатки и сборка; поддержка — обращения и ответы без контактного снимка покупки; финансы — поступления, расхождения и сроки разбора без склада и доставки.\n\n"
        for row in self.db.connection().execute("SELECT user_id,role FROM staff_roles ORDER BY user_id LIMIT 25"):
            text += f"{row['user_id']} · {ROLE_NAMES.get(row['role'], row['role'])}\n"
        text += "\nИзменить: <code>/role TELEGRAM_ID manager</code>\nРоли: <code>manager</code>, <code>warehouse</code>, <code>support</code>, <code>finance</code>. Отозвать: <code>none</code>. Сотрудник сначала должен открыть бота."
        self.screen(chat_id, user_id, "КОМАНДА / ДОСТУПЫ", text, [[button("← Рабочее место", "c:team")]])

    def audit_view(self, chat_id, user_id):
        self.db.require_staff(user_id, "audit.read", self.owners)
        text = "<b>Последние 12 изменений</b>\nКто, что и когда изменил. Полный журнал хранится в базе.\n\n"
        for row in self.db.connection().execute("SELECT * FROM staff_audit ORDER BY audit_id DESC LIMIT 12"):
            text += f"{esc(row['created_at'])}\n{row['actor_id']} · {esc(row['action'])} · {esc(row['entity'])}\n\n"
        self.screen(chat_id, user_id, "КОМАНДА / АУДИТ", text, [[button("← Рабочее место", "c:team")]])

    def resume(self, chat_id, user_id):
        if self.bot.alerts.resume(chat_id, user_id):
            return
        if self.bot.growth.resume(chat_id, user_id):
            return
        if self.bot.discovery.resume(chat_id, user_id):
            return
        if self.bot.finance.resume(chat_id, user_id):
            return
        if self.bot.operations.resume(chat_id, user_id):
            return
        state = self.db.get_state(user_id)
        if not state or not state[0].startswith("commerce_"):
            self.cart_view(chat_id, user_id, notice="Состав сохранён. Можно продолжить оформление.")
        elif state[0] == "commerce_field":
            self.ask_field(chat_id, user_id, state[1]["field"], state[1]["next"], new=True)
        elif state[0] == "commerce_consent":
            self.consent(chat_id, user_id, state[1]["next"])
        elif state[0] == "commerce_preview":
            self.checkout(chat_id, user_id)  # New explicit preview, never auto-submit.
        elif state[0] == "commerce_count":
            self.db.clear_state(user_id)
            self.sku_view(chat_id, user_id, state[1]["sku_id"])
        else:
            self.db.clear_state(user_id)
            self.cart_view(chat_id, user_id, notice="Корзина сохранена. Для складского или персонального ввода открой пункт заново.")

    def dispatch_action(self, chat_id, user, token):
        uid = int(user["id"])
        kind, data = self.db.chat_action(uid, token)
        if kind in {"add", "quantity", "swap", "clear"}:
            self.mutate(uid, kind, data, token)
            self.cart_view(chat_id, uid, data.get("page", 0), "Сохранено.")
        elif kind == "clear_confirm":
            self.screen(chat_id, uid, "КОРЗИНА / ОЧИСТКА", "Убрать все вещи из корзины? Оформленные покупки не изменятся.",
                [[self.action(uid, "Да, очистить", "clear", revision=data["revision"])], [button("Оставить вещи", "c:cart")]])
        elif kind in {"sizes", "person"}:
            cart = self.db.cart(uid)
            if cart["revision"] != data["revision"]:
                raise CartConflict("Корзина изменилась. Открой её заново.")
            line = next((x for x in cart["items"] if x["line_id"] == data["line_id"]), None)
            if not line:
                raise ValueError("Позиция не найдена.")
            if kind == "sizes":
                self.sizes_view(chat_id, uid, line["product_id"], line_id=line["line_id"], revision=cart["revision"])
            else:
                self.db.set_state(uid, "commerce_person", {**data, "product_id": line["product_id"], "token": token})
                self.screen(chat_id, uid, "КОРЗИНА / ПЕРСОНАЛИЗАЦИЯ", "Отправь номер жетона: от 1 до 5 цифр. Один дефис — убрать пожелание. Свободный номер подтвердит менеджер до изготовления.", [[button("← Корзина", "c:cart")]])
        elif kind == "consent":
            self.db.set_consent(uid, service_only=True)
            self.db.event(uid, "commerce_service_consent")
            self.db.clear_state(uid)
            self.checkout(chat_id, uid) if data["next"] == "checkout" else self.fields_view(chat_id, uid)
        elif kind == "field":
            self.ask_field(chat_id, uid, data["field"], data["next"])
        elif kind == "delivery":
            self.db.require_consent(uid)
            self.db.set_profile(uid, {**self.db.account_profile(uid), "deliver": data["deliver"]})
            self.checkout(chat_id, uid) if data["next"] == "checkout" else self.profile_view(chat_id, uid)
        elif kind in {"cancel_confirm", "cancel", "pay"}:
            p = self.db.purchase_view(data["purchase_id"], user_id=uid)
            if kind == "cancel_confirm":
                self.screen(chat_id, uid, "ПОКУПКА / ОТМЕНА", f"Отменить <b>всю покупку №{p['number']}</b>? Резерв всех её вещей освободится. Оплаченную покупку эта кнопка не отменяет.",
                    [[self.action(uid, "Подтвердить отмену", "cancel", purchase_id=p["purchase_id"])], [button("Оставить покупку", f"c:purchase:{p['purchase_id']}")]])
            elif kind == "cancel":
                self.db.set_order_status(p["id"], "cancelled", customer_id=uid)
                self.purchase_view(chat_id, uid, p["purchase_id"])
            else:
                result = self.bot.resume_payment(uid, p["payment_id"])
                if not result.get("ok"):
                    raise ValueError(result.get("error", "Оплата недоступна."))
                methods = result.get("methods", [])
                rows = [[button(m.get("label", m.get("title", m["id"])), f"pay:{p['payment_id']}:{m['id']}")] for m in methods]
                rows.append([button("← Покупка", f"c:purchase:{p['purchase_id']}")])
                self.screen(chat_id, uid, "ПОКУПКА / ОПЛАТА", f"<b>№{p['number']} · {esc(format_rub(p['amount_rub']))}</b>\nВыбери доступный метод. Успех подтверждает сервер, не закрытие окна оплаты." if methods else "Нет доступного способа оплаты. Напиши менеджеру. Резерв не продлевается автоматически.", rows)
        elif kind == "claim":
            self.db.perform_staff_action(uid, token, lambda: self.db.claim_purchase(uid, data["purchase_id"], owner_ids=self.owners))
            self.purchase_view(chat_id, uid, data["purchase_id"], staff=True)
        elif kind in {"release_confirm", "release"}:
            self.db.require_staff(uid, "orders.claim", self.owners)
            if kind == "release_confirm":
                self.screen(chat_id, uid, "КОМАНДА / ПЕРЕДАЧА", "Снять ответственного и вернуть покупку в общую очередь? Этап и оплата не изменятся.",
                    [[self.action(uid, "Да, передать в очередь", "release", **data)], [button("Оставить", f"c:work:{data['purchase_id']}")]])
            else:
                self.db.perform_staff_action(uid, token, lambda: self.db.release_purchase(uid, data["purchase_id"], data["version"], owner_ids=self.owners))
                self.purchase_view(chat_id, uid, data["purchase_id"], staff=True)
        elif kind in {"stage_confirm", "stage"}:
            self.db.require_staff(uid, "orders.pack" if self.role(uid) == "warehouse" else "orders.write", self.owners)
            if kind == "stage_confirm":
                self.screen(chat_id, uid, "КОМАНДА / ПОДТВЕРЖДЕНИЕ", f"Покупка №{data['purchase_id']:04d}: перевести на этап «{FULFILLMENT_LABELS[data['target']]}»? Клиент получит уведомление. Это фактический этап работы, а не статус перевозчика.",
                    [[self.action(uid, "Да, этап выполнен", "stage", **data)], [button("← Карточка", f"c:work:{data['purchase_id']}")]])
            else:
                self.db.perform_staff_action(uid, token, lambda: self.db.advance_purchase(uid, data["purchase_id"], data["target"], data["version"], owner_ids=self.owners))
                self.purchase_view(chat_id, uid, data["purchase_id"], staff=True)
        elif kind in {"count_input", "count"}:
            self.db.require_staff(uid, "inventory.write", self.owners)
            if kind == "count_input":
                self.db.set_state(uid, "commerce_count", data)
                self.screen(chat_id, uid, "СКЛАД / ПЕРЕСЧЁТ", "Отправь подтверждённое количество для продажи, <b>включая активные неоплаченные резервы</b>. Уже оплаченные и выделенные старым заказам вещи не включай.\n\nЦелое число, 0 допустим. Затем будет отдельное подтверждение.", [[button("Отмена · вариант", f"c:sku:{data['sku_id']}")]])
            else:
                self.db.perform_staff_action(uid, token, lambda: self.db.set_stock(uid, data["sku_id"], data["quantity"], data["version"], owner_ids=self.owners))
                self.db.clear_state(uid)
                self.sku_view(chat_id, uid, data["sku_id"])
        elif kind == "role":
            self.db.perform_staff_action(uid, token, lambda: self.db.set_staff_role(uid, data["target_id"], data["role"], owner_ids=self.owners))
            self.staff_view(chat_id, uid)
        else:
            raise ValueError("Действие больше не поддерживается. Открой главную.")

    def handle_callback(self, chat_id, user, data):
        aliases = {"menu": "c:home", "catalog": "c:catalog", "my_orders": "c:orders"}
        data = aliases.get(data, data)
        if data.startswith("want:"):
            data = "c:sizes:" + data.split(":", 1)[1]
        if not data.startswith("c:"):
            return False
        uid = int(user["id"])
        try:
            parts = data.split(":")
            route = parts[1]
            if route in {"home", "cart", "catalog", "orders", "profile", "team", "stock", "sku"}:
                self.pause(uid)
            if route == "a": self.dispatch_action(chat_id, user, parts[2])
            elif route == "home": self.home(chat_id, uid)
            elif route == "cart": self.cart_view(chat_id, uid, int(parts[2]) if len(parts) > 2 else 0)
            elif route == "catalog": self.catalog_view(chat_id, uid, int(parts[2]) if len(parts) > 2 else 0)
            elif route == "product": self.product_view(chat_id, uid, parts[2])
            elif route == "sizes": self.sizes_view(chat_id, uid, parts[2])
            elif route == "profile": self.fields_view(chat_id, uid) if len(parts) > 2 else self.profile_view(chat_id, uid)
            elif route == "fields": self.fields_view(chat_id, uid)
            elif route == "delivery": self.delivery_view(chat_id, uid, parts[2])
            elif route == "resume": self.resume(chat_id, uid)
            elif route == "checkout": self.checkout(chat_id, uid)
            elif route == "confirm":
                previous = self.db.connection().execute("SELECT receipt FROM checkouts WHERE user_id=? AND request_id=?", (uid, "chat-" + parts[2])).fetchone()
                if previous:
                    self.purchase_view(chat_id, uid, json.loads(previous[0])["purchase_id"])
                    return True
                payload = self.db.checkout_draft(uid, parts[2])
                result = self.bot.checkout_web_payload(user, payload)
                if not result.get("ok"):
                    raise ValueError(result.get("error", "Не получилось принять покупку."))
                self.purchase_view(chat_id, uid, result["purchase_id"])
            elif route == "orders": self.orders_view(chat_id, uid, int(parts[2]) if len(parts) > 2 else 0)
            elif route == "purchase": self.purchase_view(chat_id, uid, int(parts[2]))
            elif route == "legacy": self.bot.show_my_orders(chat_id, uid)
            elif route == "team": self.team_view(chat_id, uid)
            elif route == "queue": self.queue_view(chat_id, uid, parts[2], int(parts[3]))
            elif route == "work": self.purchase_view(chat_id, uid, int(parts[2]), staff=True)
            elif route == "stock": self.stock_view(chat_id, uid, int(parts[2]))
            elif route == "sku": self.sku_view(chat_id, uid, int(parts[2]))
            elif route == "staff": self.staff_view(chat_id, uid)
            elif route == "audit": self.audit_view(chat_id, uid)
            elif route == "more": self.screen(chat_id, uid, "МАГАЗИН / ЕЩЁ", "Посмотреть образы, познакомиться с брендом и пригласить друга.",
                [[button("Образы", "lookbook"), button("Ролик", "teaser")], [button("О бренде", "about"), button("Пригласить друга", "referral")], [button("← Главная", "c:home")]])
            elif route == "help":
                rows = [[button("Поддержка в боте", "o:support"), button("Мои обращения", "o:tickets:all:0")], [button("Мои покупки", "c:orders"), button("Главная", "c:home")]]
                if self.bot.settings.support_username:
                    rows.insert(0, [{"text": "Написать менеджеру ↗", "url": "https://t.me/" + self.bot.settings.support_username.lstrip("@")}])
                self.screen(chat_id, uid, "МАГАЗИН / ПОМОЩЬ", "<b>Покупка без потери контекста.</b>\n/cart — корзина\n/purchases — покупки и оплата\n/profile — твои данные\n/resume — продолжить\n/menu — главная\n\nДо оплаты можно отменить всю покупку. После оплаты доставка, обмен, возврат и вопросы — в разделе «Поддержка в боте». Текст и ответы сохранятся в обращении. /support — новое, /tickets — мои обращения. Не оплачивай старые счета после окончания резерва.\n\nОтсутствующий остаток — не повод списывать деньги: сначала подтвердим наличие.", rows)
            else: raise ValueError("Кнопка устарела. Открой /menu.")
        except (ValueError, PermissionError, PriceError, IndexError) as exc:
            rows = [[button("Корзина", "c:cart"), button("Мои покупки", "c:orders")], [button("Главная", "c:home")]]
            if self.role(uid):
                rows.insert(0, [button("← Рабочее место", "c:team")])
            self.screen(chat_id, uid, "НУЖНО ВНИМАНИЕ", esc(str(exc)), rows)
        return True

    def handle_message(self, chat_id, user, message):
        uid = int(user["id"])
        text = str(message.get("text", "")).strip()
        command = text.split(" ", 1)[0].split("@", 1)[0].lower()
        routes = {"/menu": "c:home", "/cart": "c:cart", "/purchases": "c:orders", "/profile": "c:profile", "/resume": "c:resume", "/team": "c:team", "/admin": "c:team", "/stock": "c:stock:0", "/help": "c:help"}
        if command in routes:
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
            return self.handle_callback(chat_id, user, routes[command])
        if text.lower() in {"отмена", "cancel", "/cancel"}:
            self.db.clear_state(uid)
            self.home(chat_id, uid, new=True)
            return True
        if command.startswith("/") and command != "/role" and self.bot.is_admin(uid) and self.bot.admin_command(chat_id, uid, text):
            return True
        state = self.db.get_state(uid)
        if command != "/role" and not (state and state[0].startswith("commerce_")):
            return False
        # Text/contact input is a new turn in the conversation: the answer
        # belongs below it, not in an earlier panel above the user's message.
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
        try:
            if command == "/role":
                self.db.require_staff(uid, "staff.write", self.owners)
                _, target, role = text.split()
                if not target.isdecimal() or role not in {"manager", "warehouse", "support", "finance", "none"}:
                    raise ValueError("Формат: /role TELEGRAM_ID manager|warehouse|support|finance|none")
                self.screen(chat_id, uid, "КОМАНДА / ПОДТВЕРЖДЕНИЕ", f"Пользователь {int(target)}: назначить роль {esc(role)}? Старые кнопки после отзыва доступа работать не будут.",
                    [[self.action(uid, "Подтвердить права", "role", target_id=int(target), role=role)], [button("Отмена · команда", "c:staff")]], new=True)
            elif state[1].get("paused"):
                self.home(chat_id, uid, new=True)
            elif state[0] == "commerce_field":
                self.db.require_consent(uid)
                field = state[1]["field"]
                if field == "phone":
                    from bot import normalize_phone
                    contact = message.get("contact")
                    if contact:
                        if contact.get("user_id") != uid:
                            raise ValueError("Нужен твой контакт, не контакт другого человека.")
                        text = str(contact.get("phone_number", ""))
                    text = normalize_phone(text)
                    if not text:
                        raise ValueError("Проверь номер. Пример: +79991234567.")
                limit = {"name": 80, "phone": 32, "city": 80, "address": 200, "note": 200}[field]
                if not text or len(text) > limit or any(ord(c) < 32 for c in text):
                    raise ValueError(f"Отправь один текст длиной до {limit} символов.")
                profile = self.db.account_profile(uid)
                profile[field] = "" if field == "note" and text == "-" else text
                self.db.set_profile(uid, profile)
                self.db.clear_state(uid)
                if state[1]["next"] == "checkout":
                    self.checkout(chat_id, uid)
                else:
                    self.profile_view(chat_id, uid)
            elif state[0] == "commerce_person":
                person = "" if text == "-" else text
                self.mutate(uid, "personalize", {**state[1], "person": person}, state[1]["token"])
                self.cart_view(chat_id, uid, notice="Пожелание сохранено. Номер подтвердит менеджер.")
            elif state[0] == "commerce_count":
                self.db.require_staff(uid, "inventory.write", self.owners)
                if not text.isdecimal() or not 0 <= int(text) <= 1_000_000:
                    raise ValueError("Нужен целый остаток от 0 до 1000000.")
                data = state[1]
                self.screen(chat_id, uid, "СКЛАД / ПОДТВЕРЖДЕНИЕ", f"Установить учётное количество <b>{int(text)} шт.</b>?\nТы исключил уже оплаченные и выделенные старым заказам вещи и включил активные неоплаченные резервы. Изменение попадёт в журнал.",
                    [[self.action(uid, "Пересчёт верен · сохранить", "count", sku_id=data["sku_id"], version=data["version"], quantity=int(text))], [button("Отмена · вариант", f"c:sku:{data['sku_id']}")]], new=True)
                self.pause(uid)
            else:
                self.resume(chat_id, uid)
        except (ValueError, PermissionError, PriceError) as exc:
            self.screen(chat_id, uid, "ПРОВЕРЬ ДАННЫЕ", esc(str(exc)), [[button("Продолжить", "c:resume"), button("Главная", "c:home")]], new=True)
        return True
