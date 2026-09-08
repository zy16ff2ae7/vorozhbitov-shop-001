"""Прогон основных сценариев бота без обращения к Telegram.

Запуск:
    python3 smoke_test.py

Подменяет сеть на записывающую заглушку и печатает, что бот «отправил».
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import bot as bot_module
from bot import BrandBot, Catalog, Database, Settings, TelegramAPI

SENT: list[tuple[str, dict]] = []


class FakeAPI(TelegramAPI):
    def __init__(self) -> None:  # noqa: D107
        self.base_url = "https://api.telegram.org/botfake/"

    def call(self, method: str, payload: dict | None = None, timeout: int = 70):
        SENT.append((method, payload or {}))
        if method == "sendMessage":
            return {"message_id": len(SENT)}
        if method == "sendPhoto":
            return {"message_id": len(SENT)}
        if method == "sendMediaGroup":
            return [{"message_id": len(SENT)}]
        return True


def show(limit: int = 400) -> None:
    for method, payload in SENT:
        text = payload.get("text") or payload.get("caption") or ""
        markup = payload.get("reply_markup", {})
        buttons: list[str] = []
        for row in markup.get("inline_keyboard", []) or []:
            buttons.extend(button["text"] for button in row)
        print(f"→ {method}: {text[:limit]!r}")
        if buttons:
            print(f"    кнопки: {' | '.join(buttons)}")


def callback(user_id: int, data: str) -> dict:
    return {
        "update_id": len(SENT),
        "callback_query": {
            "id": f"cb{len(SENT)}",
            "data": data,
            "from": {"id": user_id, "username": f"user{user_id}", "first_name": "Никита"},
            "message": {"chat": {"id": user_id, "type": "private"}},
        },
    }


def message(user_id: int, text: str) -> dict:
    return {
        "update_id": len(SENT),
        "message": {
            "message_id": len(SENT),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "username": f"user{user_id}", "first_name": "Никита"},
            "text": text,
        },
    }


def main() -> int:
    workdir = Path(tempfile.mkdtemp())
    # работаем на копии каталога, чтобы прогон не испортил боевой catalog.json
    catalog_path = workdir / "catalog.json"
    shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
    settings = Settings(
        token="fake",
        admin_ids=frozenset({1}),
        channel_url="https://t.me/+XufFz8GGR0o3Njky",
        webapp_url="https://shop.example.com",
        manager_chat_id=1,
        brand_name="ВОРОЖБИТОВ | ОДЕЖДА",
        support_username="",
        database_path=workdir / "data/bot.sqlite3",
        catalog_path=catalog_path,
        health_port=8080,
        giveaway_min_invites=3,
        privacy_url="https://telegra.ph/vorozhbitov-privacy",
    )
    api = FakeAPI()
    db = Database(settings.database_path)
    catalog = Catalog(settings.catalog_path)
    brand_bot = BrandBot(settings, api, db, catalog)
    brand_bot.bot_username = "vorozhbitov_shop_bot"

    steps = [
        ("Старт нового пользователя", message(500, "/start")),
        ("Выбор интереса", callback(500, "intr:hoodie")),
        ("Открытие каталога", callback(500, "catalog")),
        ("Категория выпуска", callback(500, "cat:drop")),
        ("Карточка товара", callback(500, "product:drop-tee-001")),
        ("Выбор размера", callback(500, "size:drop-tee-001:L")),
        ("Запрос согласия перед контактом", callback(500, "consent:yes")),
        ("Клиент прислал номер", message(500, "+79991234567")),
        ("Второй клиент зашёл", message(700, "/start")),
        ("Интерес пропустил", callback(700, "intr:skip")),
        ("Второй клиент выбрал размер", callback(700, "size:drop-hoodie-001:M")),
        ("Второй клиент отказал в согласии", callback(700, "consent:no")),
        ("Реферальная механика", callback(500, "referral")),
        ("Лист ожидания", callback(500, "wsize:drop-hoodie-001:XL")),
        ("Lookbook", callback(500, "lookbook")),
        ("Панель администратора", message(1, "/admin")),
        ("Статистика", message(1, "/stats")),
        ("Заявки", message(1, "/orders")),
        ("Подтверждение заявки", callback(1, "order:1:confirmed")),
        ("Завершение заявки", callback(1, "order:1:completed")),
        ("Второй пользователь по реф-ссылке", message(600, "/start ref500")),
        ("Топ рефералов", message(1, "/top")),
        ("Черновик рассылки", message(1, "/broadcast ВЫПУСК СЕГОДНЯ В 19:00 — размеры разберут за час")),
        ("Выбор сегмента", callback(1, "seg:interest:hoodie")),
        ("Подтверждение рассылки", callback(1, "admin:broadcast_confirm")),
        ("Админ добавляет вещь", message(1, "/add")),
        ("Категория новой вещи", callback(1, "addcat:access")),
        ("Название", message(1, "DROP 002 CAP")),
        ("Цена", message(1, "3 900 ₽")),
        ("Размеры", message(1, "ONE SIZE")),
        ("Описание", message(1, "Кепка второго выпуска. Мелкая партия, добивать не будем.")),
        ("Фото пропустили", message(1, "/skip")),
        ("Публикация", callback(1, "add:publish")),
        ("Скрываем вещь из витрины", message(1, "/hide drop-002-cap")),
    ]

    for title, update in steps:
        print(f"\n=== {title} ===")
        SENT.clear()
        brand_bot.handle_update(update)
        show()

    print("\n=== Итоговая статистика ===")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
