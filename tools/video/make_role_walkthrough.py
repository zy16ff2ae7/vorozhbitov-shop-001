#!/usr/bin/env python3
"""Видео для заказчика: как меню в Telegram работает у покупателя и у команды.

В кадре нет ничего выдуманного: сценарий гоняет живой ``bot.py`` через
записывающую заглушку API, поэтому каждое сообщение, кнопка, альбом и счёт —
те самые, что приходят в Telegram. Экраны, которые бот правит на месте
(``editMessageText``), в кадре тоже меняются на месте, а оплата и клавиатура
«Отправить номер» уходят новыми сообщениями — как в бою.

Обе роли снимаются одним прогоном по общей базе: покупка и лист ожидания из
роли покупателя видны затем в роли команды (пульт, /restock, /waitlist, топ).

Нужны Pillow и ffmpeg (в песочнице — ``/tmp/venv``). Съёмка:

    python3 tools/video/make_role_walkthrough.py --out video
    python3 tools/video/make_role_walkthrough.py --roles buyer --out video

Получаются ``video/buyer.mp4``, ``video/team.mp4`` и склейка
``video/vorozhbitov-roles.mp4``. Файлы не коммитятся: это артефакт для
заказчика, а не код магазина.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))
sys.path.insert(0, str(BASE_DIR / "tools" / "video"))

from bot import BrandBot, Catalog, Database, Settings, TelegramAPI  # noqa: E402
import chat_render as ui  # noqa: E402

FPS = 30
TITLE_SECONDS = 2.0
MANAGER_CHAT = 900


# ------------------------------------------------------------------ запись

class Recorder(TelegramAPI):
    """Пишет всё, что бот «отправил», и действия человека одной лентой."""

    def __init__(self) -> None:
        super().__init__("video-token")
        self.events: list[dict[str, Any]] = []
        self.muted = False
        self._next_id = 100
        self._markup: dict[tuple[int, int], dict[str, Any]] = {}

    def mark(self, event: dict[str, Any]) -> None:
        """Действие человека: нажатие кнопки или своё сообщение."""
        if not self.muted:
            self.events.append(event)

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _add(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.muted:
            self.events.append(event)
        return event

    def find_button(self, chat_id: int, data: str) -> tuple[int, int] | None:
        """Сообщение и номер кнопки, где сейчас живёт ``data``."""
        for (chat, message_id), markup in reversed(list(self._markup.items())):
            if chat != chat_id:
                continue
            index = 0
            for row in (markup or {}).get("inline_keyboard", []):
                for button in row:
                    if button.get("callback_data") == data:
                        return message_id, index
                    index += 1
        return None

    def find_label(self, chat_id: int, fragment: str) -> tuple[int, int, str] | None:
        for (chat, message_id), markup in reversed(list(self._markup.items())):
            if chat != chat_id:
                continue
            index = 0
            for row in (markup or {}).get("inline_keyboard", []):
                for button in row:
                    if fragment in str(button.get("text", "")):
                        return message_id, index, str(button.get("callback_data", ""))
                    index += 1
        return None

    # API бота ----------------------------------------------------------
    def send_message(self, chat_id, text, reply_markup=None):
        message_id = self._id()
        self._markup[(chat_id, message_id)] = reply_markup or {}
        self._add({"kind": "text", "chat": chat_id, "id": message_id, "text": text,
                   "markup": reply_markup or {}})
        return {"message_id": message_id, "chat": {"id": chat_id}}

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        if reply_markup is not None and "inline_keyboard" not in reply_markup:
            return False
        self._markup[(chat_id, message_id)] = reply_markup or {}
        self._add({"kind": "edit", "chat": chat_id, "id": message_id, "text": text,
                   "markup": reply_markup or {}})
        return True

    def send_photo(self, chat_id, photo, caption, reply_markup=None):
        message_id = self._id()
        self._markup[(chat_id, message_id)] = reply_markup or {}
        self._add({"kind": "photo", "chat": chat_id, "id": message_id, "media": [str(photo)],
                   "text": caption, "markup": reply_markup or {}})
        return {"message_id": message_id, "photo": [{"file_id": "video"}]}

    def send_photo_file(self, chat_id, path, caption="", reply_markup=None):
        message_id = self._id()
        self._markup[(chat_id, message_id)] = reply_markup or {}
        self._add({"kind": "photo", "chat": chat_id, "id": message_id, "media": [str(path)],
                   "text": caption, "markup": reply_markup or {}})
        return {"message_id": message_id, "photo": [{"file_id": "video"}]}

    def send_video(self, chat_id, video, caption="", reply_markup=None, thumbnail=None,
                   width=0, height=0, duration=0):
        message_id = self._id()
        self._markup[(chat_id, message_id)] = reply_markup or {}
        poster = str(thumbnail) if thumbnail else str(video)
        self._add({"kind": "video", "chat": chat_id, "id": message_id, "media": [poster],
                   "text": caption, "markup": reply_markup or {}})
        return {"message_id": message_id, "video": {"file_id": "video"}}

    def send_media_group(self, chat_id, photos, caption="", labels=None):
        message_id = self._id()
        self._add({"kind": "album", "chat": chat_id, "id": message_id,
                   "media": [str(photo) for photo in photos], "labels": labels or [],
                   "text": caption, "markup": {}})
        return {"message_id": message_id}

    def send_document(self, chat_id, filename, content, caption=""):
        message_id = self._id()
        self._add({"kind": "document", "chat": chat_id, "id": message_id,
                   "media": [str(filename)], "text": caption, "markup": {}})
        return {"message_id": message_id}

    def send_invoice(self, chat_id, payload):
        message_id = self._id()
        prices = payload.get("prices") or [{}]
        amount = int(prices[0].get("amount", 0) or 0)
        currency = str(payload.get("currency") or "RUB")
        shown = f"{amount} зв." if currency == "XTR" else f"{amount / 100:,.0f} {currency}".replace(",", " ")
        self._add({"kind": "invoice", "chat": chat_id, "id": message_id,
                   "media": [str(payload.get("title", "")), shown], "text": "", "markup": {}})
        return {"message_id": message_id}

    def create_invoice_link(self, payload):
        return "https://t.me/invoice/video"

    def answer_callback(self, callback_id, text=""):
        return None

    def answer_pre_checkout(self, query_id, ok=True, error_message=""):
        return None

    def call(self, method, payload=None, timeout=70):
        return True


# ------------------------------------------------------------------ съёмка

class Director:
    """Ведёт роль: свои сообщения, нажатия кнопок и служебные события."""

    def __init__(self, bot: BrandBot, rec: Recorder, chat: int, user: dict[str, Any]):
        self.bot = bot
        self.rec = rec
        self.chat = chat
        self.user = user
        self._update = 10_000 + chat

    def _feed(self, payload: dict[str, Any]) -> None:
        self._update += 1
        payload.setdefault("update_id", self._update)
        if not self.bot.handle_update(payload):
            raise SystemExit(f"бот не обработал обновление: {payload}")

    def say(self, text: str, contact_phone: str | None = None) -> None:
        message: dict[str, Any] = {
            "message_id": 1, "date": 0, "chat": {"id": self.chat, "type": "private"},
            "from": self.user, "text": text}
        if contact_phone:
            message["contact"] = {"user_id": self.chat, "phone_number": contact_phone}
            message["text"] = contact_phone
        self.rec.mark({"kind": "out", "chat": self.chat, "text": text,
                       "contact": bool(contact_phone)})
        self._feed({"message": message})

    def _press(self, message_id: int, index: int, data: str) -> None:
        self.rec.mark({"kind": "tap", "chat": self.chat, "id": message_id, "index": index})
        self._feed({"callback_query": {
            "id": f"cb{self._update}", "chat_instance": "video", "data": data, "from": self.user,
            "message": {"message_id": message_id, "chat": {"id": self.chat, "type": "private"}}}})

    def tap(self, data: str) -> None:
        found = self.rec.find_button(self.chat, data)
        if not found:
            raise SystemExit(f"кнопка {data!r} не найдена на экранах роли")
        self._press(*found, data)

    def tap_label(self, fragment: str) -> None:
        found = self.rec.find_label(self.chat, fragment)
        if not found:
            raise SystemExit(f"кнопка {fragment!r} не найдена на экранах роли")
        message_id, index, data = found
        self._press(message_id, index, data)

    def pay_success(self, payment_id: str, stars: int) -> None:
        self._feed({"message": {
            "message_id": 2, "date": 0, "chat": {"id": self.chat, "type": "private"},
            "from": self.user,
            "successful_payment": {"invoice_payload": payment_id, "currency": "XTR",
                                   "total_amount": stars,
                                   "telegram_payment_charge_id": "video-charge"}}})


# ------------------------------------------------------------------ роли

BUYER = {"id": 500, "username": "buyer", "first_name": "Никита"}
BOSS = {"id": 1, "username": "boss", "first_name": "Шеф"}
GUEST = {"id": 501, "username": "friend", "first_name": "Гоша"}


def build_bot(workspace: Path) -> tuple[BrandBot, Database, Recorder]:
    catalog_path = workspace / "catalog.json"
    shutil.copy(BASE_DIR / "catalog.json", catalog_path)
    settings = Settings(
        token="video", admin_ids=frozenset({1}), channel_url="https://t.me/vorozhbitov",
        webapp_url="https://shop.example/app", manager_chat_id=MANAGER_CHAT,
        brand_name="ВОРОЖБИТОВ", support_username="vorozhbitov_shop",
        database_path=workspace / "video.sqlite3", catalog_path=catalog_path,
        health_port=8098, giveaway_min_invites=3, privacy_url="https://telegra.ph/privacy",
    )
    rec = Recorder()
    db = Database(settings.database_path)
    bot = BrandBot(settings, rec, db, Catalog(catalog_path))
    bot.bot_username = "vorozhbitov_shop_bot"
    return bot, db, rec


def run_buyer(director: Director, db: Database) -> None:
    director.say("/start")
    director.tap("intr:drop")
    director.tap("catalog")
    director.tap("cat:drop")
    director.tap("product:tee-sila-i-chest")
    director.tap("want:tee-sila-i-chest")
    director.tap("size:tee-sila-i-chest:L")
    director.tap("consent:yes")
    director.say("Отправить номер телефона", contact_phone="+79991234567")
    payment = next(row["payment_id"] for row in db.connection().execute(
        "SELECT payment_id FROM payments ORDER BY rowid DESC LIMIT 1"))
    stars = int(db.get_payment(payment)["amount_stars"] or 0)
    director.tap(f"pay:{payment}:stars")
    director.pay_success(payment, stars)
    director.tap("my_orders")
    order = next(row["id"] for row in db.connection().execute(
        "SELECT id FROM orders ORDER BY id DESC LIMIT 1"))
    director.tap(f"ord:{order}")
    director.tap("menu")
    director.tap("catalog")
    director.tap("cat:drop")
    director.tap("product:tee-sila-i-chest")
    director.tap("wait:tee-sila-i-chest")
    director.tap("wsize:tee-sila-i-chest:XXL")
    director.tap("account")
    director.tap("waits")
    entry = next(row["id"] for row in db.connection().execute(
        "SELECT id FROM waitlist ORDER BY id DESC LIMIT 1"))
    director.tap(f"wstop:{entry}")


def run_team(bot: BrandBot, db: Database, rec: Recorder, director: Director) -> None:
    # Тихо готовим живые данные: вторая покупка и приведённые друзья, чтобы
    # пульт, сводка и топ в кадре не были пустыми заглушками.
    rec.muted = True
    try:
        db.upsert_user({"id": 2, "username": "ref", "first_name": "Рина"})
        for guest in range(3, 7):
            # source — отдельный аргумент: именно он засчитывает приглашение.
            db.upsert_user({"id": guest, "username": f"g{guest}", "first_name": "Гость"},
                           source="ref002")
        guest_director = Director(bot, rec, 501, GUEST)
        guest_director.say("/start")
        guest_director.tap("intr:drop")
        guest_director.tap("catalog")
        guest_director.tap("cat:drop")
        guest_director.tap("product:tee-sila-i-chest")
        guest_director.tap("want:tee-sila-i-chest")
        guest_director.tap("size:tee-sila-i-chest:M")
        guest_director.tap("consent:yes")
        guest_director.say("Отправить номер телефона", contact_phone="+79990001122")
    finally:
        rec.muted = False

    director.say("/panel")
    director.tap("adm:orders")
    order = next(row["id"] for row in db.connection().execute(
        "SELECT id FROM orders WHERE status IN ('new', 'awaiting_payment') LIMIT 1"))
    director.tap(f"aord:{order}")
    director.tap(f"order:{order}:paid")
    director.tap(f"order:{order}:confirmed")
    director.tap(f"order:{order}:completed")
    director.tap("adm:panel")
    director.tap("adm:add")
    director.tap("addcat:drop")
    for step in ("Футболка дисциплина", "5 400", "S, M, L", "Плотная футболка второго тиража.", "/skip"):
        director.say(step)
    director.tap("add:publish")
    director.tap("adm:panel")
    director.tap("adm:waitlist")
    director.say("/restock tee-sila-i-chest XXL")
    director.tap("adm:panel")


def wait_event(rec: Recorder, chat: int, fragment: str, timeout: float = 15.0) -> None:
    """Ждём асинхронное событие: отчёт рассылки приходит из отдельного потока."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in reversed(rec.events):
            if event.get("chat") == chat and fragment in str(event.get("text", "")):
                return
        time.sleep(0.05)
    raise SystemExit(f"событие «{fragment}» не появилось за {timeout} с")


def run_owner(bot: BrandBot, db: Database, rec: Recorder, director: Director) -> None:
    # Пульт владельца: деньги, аудитория и рассылка — то, что команда не трогает.
    director.say("/panel")
    director.tap("adm:summary")
    director.tap("adm:panel")
    director.say("/broadcast Футболка «Сила и честь»: размер L — последние штуки. Кто ждал — забирайте.")
    director.tap("seg:all")
    director.tap("admin:broadcast_confirm")
    wait_event(rec, director.chat, "РАССЫЛКА ГОТОВА")
    director.tap("adm:panel")
    director.say("/giveaway 1")
    director.tap("adm:panel")
    director.tap("adm:top")
    director.tap("adm:panel")
    director.tap("adm:draws")
    director.tap("adm:panel")
    director.say("/grant 501")
    director.tap("access:revoke:501")
    director.say("/export")
    director.say("/reload")
    director.tap_label("Режим покупателя")


# ------------------------------------------------------------------ лента

TAG_STRIP = re.compile(r"</?[a-z]+[^>]*>", re.IGNORECASE)


def visible_length(text: str) -> int:
    plain = TAG_STRIP.sub("", text or "")
    return len("".join(ch for ch in plain if not ui.is_icon_char(ch)))


def dwell(event: dict[str, Any]) -> float:
    """Сколько экран держится в кадре: хватает прочитать, но не заскучать."""
    kind = event["kind"]
    if kind == "out":
        return min(1.6, max(0.8, 0.6 + visible_length(event["text"]) / 50))
    if kind in ("photo", "video"):
        return 2.2
    if kind == "album":
        return 2.6
    if kind == "invoice":
        return 2.2
    if kind == "document":
        return 1.8
    return min(3.4, max(1.2, 0.85 + visible_length(event["text"]) / 48))


def layer_for(event: dict[str, Any], clock: str) -> ui.Layer:
    kind = event["kind"]
    if kind == "out":
        if event.get("contact"):
            return ui.render_contact("Никита", event["text"], clock)
        return ui.render_text_bubble(event["text"], None, True, clock)
    if kind in ("text", "edit"):
        return ui.render_text_bubble(event["text"], event["markup"], False, clock)
    if kind in ("photo", "video", "album", "invoice", "document"):
        return ui.render_media_bubble(kind, event["media"], event["text"], event["markup"], clock)
    raise SystemExit(f"неизвестное событие {kind}")


def build_scene(title: str, status: str, avatar: str, events: list[dict[str, Any]],
                focus: int) -> ui.Scene:
    messages: dict[int, ui.Message] = {}
    order: list[ui.Message] = []
    typings: list[tuple[float, float]] = []
    taps: list[tuple[float, float, int, int]] = []
    keyboards: list[tuple[float, float | None, list[list[dict[str, Any]]]]] = []
    current_keyboard: list[list[dict[str, Any]]] = []
    keyboard_since: float | None = None
    outgoing_key = 1_000_000

    def note_keyboard(moment: float, markup: dict[str, Any]) -> None:
        nonlocal current_keyboard, keyboard_since
        if "keyboard" in (markup or {}):
            rows = ui.reply_rows(markup)
        elif (markup or {}).get("remove_keyboard"):
            rows = []
        else:
            return
        if keyboard_since is not None:
            keyboards.append((keyboard_since, moment, current_keyboard))
        current_keyboard = rows
        keyboard_since = moment

    moment = 0.9
    clock_minute = 41
    last_kind = ""

    for event in events:
        if event.get("chat") != focus:
            continue
        kind = event["kind"]
        clock = f"20:{clock_minute:02d}"
        if kind == "tap":
            taps.append((moment, moment + 0.42, event["id"], event["index"]))
            moment += 0.42
            last_kind = kind
            continue
        if kind == "out":
            moment += 0.16
            outgoing_key += 1
            message = ui.Message(outgoing_key, True, moment, clock)
            message.add(moment, layer_for(event, clock))
            order.append(message)
            moment += dwell(event)
            clock_minute += 1
            last_kind = kind
            continue
        if kind == "edit":
            moment += 0.22
            message = messages.get(event["id"])
            if message is None:
                continue
            message.add(moment, layer_for(event, clock))
            note_keyboard(moment, event["markup"])
            moment += dwell(event)
            clock_minute += 1
            last_kind = kind
            continue
        typing = 0.35 if last_kind in ("text", "photo", "album", "video", "invoice", "document") else 0.6
        typings.append((moment, moment + typing))
        moment += typing
        message = ui.Message(event["id"], False, moment, clock)
        message.add(moment, layer_for(event, clock))
        messages[event["id"]] = message
        order.append(message)
        note_keyboard(moment, event.get("markup", {}))
        moment += dwell(event)
        clock_minute += 1
        last_kind = kind

    if keyboard_since is not None:
        keyboards.append((keyboard_since, None, current_keyboard))
    return ui.Scene(title, status, avatar, order, typings=typings, taps=taps,
                    keyboards=keyboards, duration=moment + 2.4)


# ------------------------------------------------------------------ кодирование

def ffmpeg_binary() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise SystemExit("нужен ffmpeg: поставьте imageio-ffmpeg или ffmpeg в PATH")


class Title:
    """Заставка роли: одно слово, без пояснений."""

    def __init__(self, word: str):
        self.word = word
        self.duration = TITLE_SECONDS

    def frame(self, t: float):
        return ui.title_frame(self.word, t / self.duration)


def encode(units: list[Any], out: Path) -> None:
    binary = ffmpeg_binary()
    command = [binary, "-y", "-loglevel", "error", "-f", "rawvideo", "-vcodec", "rawvideo",
               "-s", f"{ui.W}x{ui.H}", "-pix_fmt", "rgb24", "-r", str(FPS), "-i", "-",
               "-an", "-vcodec", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
    assert process.stdin
    for unit in units:
        frames = int(round(unit.duration * FPS))
        for index in range(frames):
            frame = unit.frame(index / FPS)
            process.stdin.write(frame.tobytes())
        process.stdin.flush()
    process.stdin.close()
    error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.wait() != 0:
        raise SystemExit(f"ffmpeg упал: {error[-800:]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="video", help="куда положить ролики")
    parser.add_argument("--roles", default="all", choices=["all", "buyer", "team", "owner"])
    parser.add_argument("--frames", metavar="DIR", default="",
                        help="не кодировать, а сохранить проверочные кадры сцен")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="video-"))

    bot, db, rec = build_bot(workspace)
    run_buyer(Director(bot, rec, 500, BUYER), db)
    buyer_end = len(rec.events)
    run_team(bot, db, rec, Director(bot, rec, 1, BOSS))
    team_end = len(rec.events)
    run_owner(bot, db, rec, Director(bot, rec, 1, BOSS))
    buyer_events = rec.events[:buyer_end]
    team_events = rec.events[buyer_end:team_end]
    owner_events = rec.events[team_end:]

    if args.frames:
        frames_dir = Path(args.frames)
        frames_dir.mkdir(parents=True, exist_ok=True)
        for name, events, focus in (("buyer", buyer_events, 500), ("team", team_events, 1),
                                      ("owner", owner_events, 1)):
            scene = build_scene("ВОРОЖБИТОВ", "бот", "assets/bot-avatar.jpg", events, focus)
            for moment in (6.0, 14.0, 26.0, 40.0, 58.0, max(6.0, scene.duration - 3.0)):
                if moment < scene.duration:
                    scene.frame(moment).save(str(frames_dir / f"{name}-{moment:04.1f}.png"))
            print(f"{name}: длительность {scene.duration:.1f} с, сообщений {len(scene.messages)}")
        return

    made: list[Path] = []
    if args.roles in ("all", "buyer"):
        scene = build_scene("ВОРОЖБИТОВ", "бот", "assets/bot-avatar.jpg", buyer_events, 500)
        path = out_dir / "buyer.mp4"
        encode([Title("ПОКУПАТЕЛЬ"), scene], path)
        made.append(path)
    if args.roles in ("all", "team"):
        scene = build_scene("ВОРОЖБИТОВ", "бот · команда", "assets/bot-avatar.jpg", team_events, 1)
        path = out_dir / "team.mp4"
        encode([Title("КОМАНДА"), scene], path)
        made.append(path)
    if args.roles in ("all", "owner"):
        scene = build_scene("ВОРОЖБИТОВ", "бот · владелец", "assets/bot-avatar.jpg", owner_events, 1)
        path = out_dir / "owner.mp4"
        encode([Title("ВЛАДЕЛЕЦ"), scene], path)
        made.append(path)
    if args.roles == "all" and all((out_dir / n).exists()
                                   for n in ("buyer.mp4", "team.mp4", "owner.mp4")):
        binary = ffmpeg_binary()
        list_file = workspace / "concat.txt"
        list_file.write_text(
            "".join(f"file '{(out_dir / n).resolve()}'\n"
                    for n in ("buyer.mp4", "team.mp4", "owner.mp4")), encoding="utf-8")
        combined = (out_dir / "vorozhbitov-roles.mp4").resolve()
        subprocess.run([binary, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(list_file.resolve()), "-c", "copy", "-movflags", "+faststart",
                        str(combined)], check=True)
        made.append(combined)

    db.close_current()
    shutil.rmtree(workspace, ignore_errors=True)
    for path in made:
        print(f"готово: {path} · {path.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()
