#!/usr/bin/env python3
"""Предпросмотр native-меню Telegram без токена бота.

Прогоняет настоящие экраны бота через записывающую заглушку Telegram API и
рисует их так, как их увидит покупатель в чате: пузыри сообщений и ряды
inline-кнопок. Это инструмент проверки меню, а не часть продукта.

Запуск:
    python3 tools/menu_preview.py                # напечатать HTML в stdout
    python3 tools/menu_preview.py --serve 8099   # открыть живой предпросмотр
"""

from __future__ import annotations

import html
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from bot import BrandBot, Catalog, Database, Settings, TelegramAPI  # noqa: E402
from payments import parse_price_strict  # noqa: E402

BUYER = {"id": 500, "username": "buyer", "first_name": "Никита"}
OWNER = {"id": 1, "username": "owner", "first_name": "Владелец"}
MANAGER_CHAT = 900


class RecordingAPI(TelegramAPI):
    """Пишет всё, что бот «отправил», вместо похода в Telegram."""

    def __init__(self) -> None:
        super().__init__("preview-token")
        self.items: list[dict] = []

    def _add(self, kind: str, chat_id: int, text: str = "", markup=None, media=None) -> dict:
        item = {"kind": kind, "chat": chat_id, "text": text, "markup": markup or {}, "media": media or []}
        self.items.append(item)
        return item

    def send_message(self, chat_id, text, reply_markup=None):
        self._add("text", chat_id, text, reply_markup)
        return {"message_id": len(self.items)}

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        """Правка экрана на месте: в предпросмотре это тот же пузырь чата."""
        if reply_markup is not None and "inline_keyboard" not in reply_markup:
            return False
        self._add("text", chat_id, text, reply_markup)
        return True

    def send_photo(self, chat_id, photo, caption, reply_markup=None):
        self._add("photo", chat_id, caption, reply_markup, [str(photo)])
        return {"message_id": len(self.items), "photo": [{"file_id": "preview"}]}

    def send_photo_file(self, chat_id, path, caption="", reply_markup=None):
        self._add("photo", chat_id, caption, reply_markup, [Path(path).name])
        return {"message_id": len(self.items), "photo": [{"file_id": "preview"}]}

    def send_video(self, chat_id, video, caption="", reply_markup=None, **kwargs):
        self._add("video", chat_id, caption, reply_markup, [str(video)])
        return {"message_id": len(self.items), "video": {"file_id": "preview"}}

    def send_media_group(self, chat_id, photos, caption="", labels=None):
        labels = labels or []
        self._add("album", chat_id, caption, None,
                  [f"{Path(str(photo)).name}" + (f" — {labels[index]}" if index < len(labels) and labels[index] else "")
                   for index, photo in enumerate(photos)])
        return {"message_id": len(self.items)}

    def send_invoice(self, chat_id, payload):
        self._add("invoice", chat_id, f"{payload.get('title', '')} · {payload.get('prices', [{}])[0].get('amount', 0)} зв.")
        return {"message_id": len(self.items)}

    def send_document(self, chat_id, filename, content, caption=""):
        self._add("text", chat_id, f"{caption} · {filename}")
        return {"message_id": len(self.items)}

    def create_invoice_link(self, payload):
        return "https://t.me/invoice/preview"

    def answer_callback(self, callback_id, text=""):
        return None

    def call(self, method, payload=None, timeout=70):
        return True


def build(workspace: Path) -> tuple[BrandBot, Database, RecordingAPI]:
    catalog_path = workspace / "catalog.json"
    shutil.copy(BASE_DIR / "catalog.json", catalog_path)
    settings = Settings(
        token="preview",
        admin_ids=frozenset({1}),
        channel_url="https://t.me/channel",
        webapp_url="https://shop.example/app",
        manager_chat_id=MANAGER_CHAT,
        brand_name="ВОРОЖБИТОВ",
        support_username="vorozhbitov_shop",
        database_path=workspace / "preview.sqlite3",
        catalog_path=catalog_path,
        health_port=8099,
        giveaway_min_invites=3,
        privacy_url="https://telegra.ph/privacy",
    )
    api = RecordingAPI()
    db = Database(settings.database_path)
    bot = BrandBot(settings, api, db, Catalog(catalog_path))
    bot.bot_username = "vorozhbitov_shop_bot"
    return bot, db, api


def purchase(db: Database, user_id: int, product_id: str, size: str, quantity: int = 1) -> dict:
    catalog = Catalog(BASE_DIR / "catalog.json")
    product = catalog.get(product_id)
    amount = parse_price_strict(product["price"]) * quantity
    receipt, _ = db.create_checkout(
        user_id, f"preview-{user_id}-{product_id}-{size}-{quantity}", "preview-fingerprint",
        [{"product_id": product["id"], "name": product["name"], "size": size, "phone": "+79991234567",
          "quantity": quantity, "note": "Могилёв · СДЭК · подъезд 2", "amount_rub": amount,
          "person": "", "person_label": ""}],
        0,
    )
    return receipt


def screens() -> list[tuple[str, str, list[dict]]]:
    """Прогоняет экраны и возвращает (раздел, подпись, сообщения)."""
    groups: list[tuple[str, str, list[dict]]] = []

    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)

        def shoot(section: str, note: str, action) -> None:
            bot, db, api = build(workspace)
            db.upsert_user(BUYER)
            db.upsert_user(OWNER)
            action(bot, db)
            groups.append((section, note, api.items))

        shoot("Первое знакомство", "ролик выпуска, обложка бренда и вопрос про интерес",
              lambda bot, db: bot.start(500, BUYER))

        def main_menu(bot, db):
            bot.send_menu(500)

        shoot("Главное меню", "шесть разделов в две колонки, витрина — отдельной кнопкой", main_menu)

        def draft_menu(bot, db):
            purchase(db, 500, "tee-sila-i-chest", "L")
            bot.send_menu(500)

        shoot("Главное меню с незакрытой оплатой", "главное действие — отдельная широкая кнопка сверху", draft_menu)

        shoot("Витрина", "разделы с реальным количеством вещей, замеры рядом",
              lambda bot, db: bot.show_catalog(500, 500))
        shoot("Раздел витрины", "короткие подписи вещей и возврат назад",
              lambda bot, db: bot.show_category(500, 500, "drop"))
        shoot("Карточка вещи", "альбом с честными подписями кадров и одна структура текста",
              lambda bot, db: bot.show_product(500, 500, "tee-sila-i-chest"))
        shoot("Выбор размера", "варианты, замеры и возврат к вещи",
              lambda bot, db: bot.choose_size(500, 500, "tee-sila-i-chest"))
        shoot("Замеры", "«Назад» ведёт туда, откуда экран открыли",
              lambda bot, db: bot.route_callback("cb", 500, 500, "size_guide:product:tee-sila-i-chest"))

        def two_purchases(bot, db):
            purchase(db, 500, "tee-sila-i-chest", "L")
            purchase(db, 500, "tag-sila-i-chest", "ONE SIZE")
            bot.show_my_orders(500, 500)

        shoot("Мои покупки", "один список с группами вместо десятка сообщений", two_purchases)

        def purchase_card(bot, db):
            receipt = purchase(db, 500, "tee-sila-i-chest", "L")
            bot.show_order(500, 500, receipt["order_ids"][0])

        shoot("Карточка покупки", "деньги, сборка и доставка — тремя строками, одно главное действие", purchase_card)

        def paid_card(bot, db):
            receipt = purchase(db, 500, "tee-sila-i-chest", "L")
            db.mark_payment_paid(receipt["payment_id"], "stars", "preview-charge")
            db.set_order_status(receipt["order_ids"][0], "confirmed")
            bot.show_order(500, 500, receipt["order_ids"][0])

        shoot("Карточка оплаченной покупки", "кнопка оплаты исчезает вместе с возможностью оплатить", paid_card)

        def draft_pay(bot, db):
            receipt = purchase(db, 500, "tee-sila-i-chest", "L")
            bot.open_draft(500, 500, receipt["payment_id"])

        shoot("Продолжить оплату", "тот же счёт, способы в две колонки", draft_pay)

        shoot("Мой кабинет", "свои данные и приоритет, ничего лишнего",
              lambda bot, db: bot.route_callback("cb", 500, 500, "account"))
        shoot("Поддержка", "вопрос по покупке без повторного ввода номера",
              lambda bot, db: bot.show_support(500, 500))

        def support_flow(bot, db):
            receipt = purchase(db, 500, "tee-sila-i-chest", "L")
            bot.forward_support(500, 500, receipt["order_ids"][0])

        shoot("Вопрос ушёл менеджеру", "слева — чат менеджера, справа — подтверждение покупателю", support_flow)
        shoot("Пустая корзина покупок", "пустое состояние ведёт в каталог",
              lambda bot, db: bot.show_my_orders(500, 500))
        shoot("Старая кнопка", "снятая вещь и неизвестная кнопка не пугают ошибкой",
              lambda bot, db: (bot.route_callback("cb", 500, 500, "product:honor-hoodie"),
                               bot.route_callback("cb", 500, 500, "legacy:unknown")))
        shoot("Пульт владельца", "сводка сверху, разделы по два, выход в режим покупателя",
              lambda bot, db: bot.admin_panel(1))
        shoot("Сводка", "деньги, работа и база не смешаны",
              lambda bot, db: bot.admin_summary(1))

        def staff_orders(bot, db):
            purchase(db, 500, "tee-sila-i-chest", "L")
            working = purchase(db, 500, "tag-sila-i-chest", "ONE SIZE")
            db.mark_payment_paid(working["payment_id"], "stars", "preview-charge")
            db.set_order_status(working["order_ids"][0], "confirmed")
            bot.admin_orders(1)

        shoot("Покупки у команды", "один список вместо десяти карточек подряд", staff_orders)

        def staff_order_card(bot, db):
            receipt = purchase(db, 500, "tee-sila-i-chest", "L")
            bot.admin_order_card(1, receipt["order_ids"][0])

        shoot("Карточка покупки у команды", "клиент, деньги, этап и одно главное действие", staff_order_card)

        def add_wizard(bot, db):
            bot.start_add_product(1, 1)
            bot.route_callback("cb", 1, 1, "addcat:access")
            for answer in ("DROP 002 CAP", "3 900 ₽", "ONE SIZE",
                           "Кепка второго выпуска. Мелкая партия, добивать не будем.", "/skip"):
                bot.handle_add_product_text(1, 1, answer)

        shoot("Мастер «Новая вещь»", "счётчик шагов, а в конце — та же карточка, что увидит покупатель", add_wizard)

        def staff_reference(bot, db):
            db.add_to_waitlist(500, bot.catalog.get("tee-sila-i-chest"), "XL")
            db.upsert_user({"id": 600, "username": "friend", "first_name": "Друг"}, "ref500")
            bot.admin_command(1, 1, "/waitlist")
            bot.admin_command(1, 1, "/top")

        shoot("Справочные экраны команды", "лист ожидания и топ приглашений — с выходом обратно в пульт",
              staff_reference)
    return groups


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px 64px; background: #0e1621; color: #f2f5f8;
  font: 15px/1.45 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
header { max-width: 1180px; margin: 0 auto 28px; }
h1 { font-size: 22px; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 6px; }
header p { margin: 0; color: #8ea3b5; font-size: 14px; }
.grid { max-width: 1180px; margin: 0 auto; display: grid; gap: 20px;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); align-items: start; }
section { background: #101c29; border: 1px solid #1d2b3a; border-radius: 14px; padding: 14px; }
section h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em; margin: 0 0 4px; color: #64a8dc; }
section p.note { margin: 0 0 12px; color: #8ea3b5; font-size: 12.5px; }
.bubble { background: #182533; border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; }
.bubble.manager { background: #1d2b3a; border-left: 3px solid #64a8dc; }
.bubble .who { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: #7f96aa; margin-bottom: 6px; }
.bubble .text { white-space: pre-wrap; word-break: break-word; }
.bubble .media { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.bubble .media span { background: #0e1621; border: 1px solid #26384a; border-radius: 8px;
                      padding: 4px 7px; font-size: 11.5px; color: #9fb3c4; }
b { color: #fff; }
.kb { display: grid; gap: 6px; }
.kb .row { display: grid; gap: 6px; grid-auto-flow: column; grid-auto-columns: 1fr; }
.kb button {
  background: #2b5278; color: #eaf3fb; border: 0; border-radius: 9px; padding: 9px 8px;
  font: inherit; font-size: 13.5px; text-align: center; cursor: default;
  overflow-wrap: anywhere; hyphens: auto;
}
.kb button.wide { grid-column: 1 / -1; }
.kb button.primary { background: #3a6ea5; font-weight: 600; }
.kb button.nav { background: #22364a; color: #b9cbdb; }
code { background: #0e1621; padding: 1px 5px; border-radius: 6px; font-size: 12.5px; }
"""

JS = ""


def render_buttons(markup: dict) -> str:
    """Ряды inline-кнопок: одна кнопка в строке рисуется широкой, как в Telegram."""
    rows = markup.get("inline_keyboard") or []
    if not rows:
        return ""
    out = ['<div class="kb">']
    for row in rows:
        out.append('<div class="row">')
        for button in row:
            label = str(button.get("text", ""))
            if label.startswith(("\u2190", "\U0001f3e0")):
                kind = "nav"
            elif label.endswith("\u2192"):
                kind = "primary"
            else:
                kind = ""
            classes = " ".join(part for part in (kind, "wide" if len(row) == 1 else "") if part)
            out.append(f'<button class="{classes}">{html.escape(label)}</button>')
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def render_group(section: str, note: str, items: list[dict]) -> str:
    bubbles = []
    for item in items:
        who = {500: "бот → покупатель", 1: "бот → владелец", MANAGER_CHAT: "бот → менеджер"}.get(item["chat"], f"чат {item['chat']}")
        media = "".join(f"<span>{html.escape(str(entry))}</span>" for entry in item["media"])
        text = item["text"] or ""
        body = f'<div class="media">{media}</div>' if media else ""
        if text:
            body += f'<div class="text">{text.replace(chr(10), "<br>")}</div>'
        body += render_buttons(item["markup"])
        bubbles.append(
            f'<div class="bubble{" manager" if item["chat"] == MANAGER_CHAT else ""}">'
            f'<div class="who">{html.escape(who)}</div>{body}</div>'
        )
    return (
        f"<section><h2>{html.escape(section)}</h2>"
        f'<p class="note">{html.escape(note)}</p>{"".join(bubbles)}</section>'
    )


def render_page() -> str:
    groups = screens()
    body = "".join(render_group(section, note, items) for section, note, items in groups)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ВОРОЖБИТОВ — меню в чате Telegram</title>
<style>{CSS}</style></head>
<body>
<header>
<h1>Меню в чате Telegram</h1>
<p>Экраны собраны настоящим кодом бота (<code>bot.py</code>) через записывающую заглушку API —
так их увидит покупатель. Кнопки не нажимаются: это предпросмотр структуры и подписей.
Правила — в <code>TELEGRAM-MENU.md</code>.</p>
</header>
<div class="grid">{body}</div>
<script>{JS}</script>
</body></html>
"""


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in {"/", "/index.html"}:
            self.send_response(404)
            self.end_headers()
            return
        body = render_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def main(argv: list[str]) -> int:
    if "--serve" in argv:
        port = int(argv[argv.index("--serve") + 1]) if len(argv) > argv.index("--serve") + 1 else 8099
        server = ThreadingHTTPServer(("0.0.0.0", port), PreviewHandler)
        print(f"Предпросмотр меню: http://0.0.0.0:{port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    sys.stdout.write(render_page())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
