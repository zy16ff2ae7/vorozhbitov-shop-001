import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch
from pathlib import Path

from dataclasses import replace

from bot import (
    BASE_DIR,
    BrandBot,
    Catalog,
    CHAT_MENU_BUTTON,
    Database,
    ICON,
    RateLimiter,
    REF_RE,
    Settings,
    TelegramAPI,
    ADD_TOTAL,
    audience_label,
    buyer_commands,
    ensure_catalog_exists,
    inline_keyboard,
    nav_rows,
    normalize_phone,
    option_rows,
    order_state_lines,
    person_label,
    plural,
    sanitize_personalization,
    slugify,
    staff_commands,
    start_health_server,
    things_word,
    webapp_user_from_init_data,
)
from payments import (
    PriceError,
    parse_price_rub,
    parse_price_strict,
    stars_amount,
    verify_crypto_webhook,
    verify_lava_webhook,
)


def make_db(directory: str) -> Database:
    return Database(Path(directory) / "test.sqlite3")


def mp4_duration_seconds(path: Path) -> float:
    """Длительность mp4 из бокса mvhd — без ffmpeg, только стандартная библиотека."""
    blob = path.read_bytes()
    at = blob.find(b"mvhd")
    if at < 0 or len(blob) < at + 32:
        raise AssertionError(f"в {path.name} нет бокса mvhd")
    version = blob[at + 4]
    if version == 0:
        timescale = int.from_bytes(blob[at + 16:at + 20], "big")
        duration = int.from_bytes(blob[at + 20:at + 24], "big")
    else:
        timescale = int.from_bytes(blob[at + 20:at + 24], "big")
        duration = int.from_bytes(blob[at + 24:at + 32], "big")
    if not timescale:
        raise AssertionError(f"в {path.name} нулевой timescale")
    return duration / timescale


class BotTests(unittest.TestCase):
    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(normalize_phone("+380 99 123 45 67"), "+380991234567")
        self.assertIsNone(normalize_phone("123"))

    def test_catalog_loads_and_resolves_products(self):
        catalog = Catalog(Path(__file__).with_name("catalog.json"))
        self.assertGreaterEqual(len(catalog.categories), 1)
        self.assertIsNotNone(catalog.get("tee-sila-i-chest"))
        self.assertEqual(catalog.get("tee-sila-i-chest")["category"], "drop")
        self.assertEqual(catalog.lookbook, [])

    def test_catalog_seed_is_copied_once_to_mutable_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(__file__).with_name("catalog.json")
            target = Path(directory) / "data" / "catalog.json"
            self.assertTrue(ensure_catalog_exists(target, seed))
            self.assertEqual(target.read_bytes(), seed.read_bytes())
            target.write_text('{"custom": true}\n', encoding="utf-8")
            self.assertFalse(ensure_catalog_exists(target, seed))
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"custom": True})

    def test_catalog_seed_requires_an_existing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaises(FileNotFoundError):
                ensure_catalog_exists(base / "data/catalog.json", base / "missing.json")

    def test_default_catalog_is_in_persistent_data_directory(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CATALOG_PATH", "DATABASE_PATH", "PORT", "GIVEAWAY_MIN_INVITES"}
        }
        with patch.dict(os.environ, env, clear=True), patch("bot.load_dotenv"):
            settings = Settings.from_env()
        self.assertEqual(settings.catalog_path, BASE_DIR / "data/catalog.json")
        self.assertEqual(settings.database_path, BASE_DIR / "data/bot.sqlite3")

    def test_database_uses_wal_and_deduplicates_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 42, "username": "buyer", "first_name": "N", "last_name": ""})
            product = {"id": "test-product", "name": "Test Product"}
            order_id, created = db.create_order("callback:abc", 42, product, "M", "+79991234567")
            repeated_id, repeated_created = db.create_order("callback:abc", 42, product, "M", "+79991234567")
            self.assertTrue(created)
            self.assertFalse(repeated_created)
            self.assertEqual(order_id, repeated_id)
            self.assertEqual(db.stats()["orders"], 1)
            journal_mode = db.connection().execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")

    def test_order_status_lifecycle_and_invalid_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 42, "username": "buyer", "first_name": "N"})
            order_id, _ = db.create_order(
                "status-flow", 42, {"id": "p1", "name": "P1"}, "M", "+79991234567"
            )
            order, changed = db.set_order_status(order_id, "confirmed")
            self.assertTrue(changed)
            self.assertEqual(order["status"], "confirmed")
            repeated, repeated_changed = db.set_order_status(order_id, "confirmed")
            self.assertFalse(repeated_changed)
            self.assertEqual(repeated["status"], "confirmed")
            completed, completed_changed = db.set_order_status(order_id, "completed")
            self.assertTrue(completed_changed)
            self.assertEqual(completed["status"], "completed")
            with self.assertRaises(ValueError):
                db.set_order_status(order_id, "cancelled")
            self.assertEqual(db.get_order(order_id)["status"], "completed")
            self.assertEqual(db.set_order_status(99999, "confirmed"), (None, False))

    def test_order_can_be_cancelled_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 43, "username": "buyer2", "first_name": "B"})
            order_id, _ = db.create_order(
                "cancel-flow", 43, {"id": "p2", "name": "P2"}, "L", "+79991234568"
            )
            cancelled, changed = db.set_order_status(order_id, "cancelled")
            self.assertTrue(changed)
            self.assertEqual(cancelled["status"], "cancelled")
            with self.assertRaises(ValueError):
                db.set_order_status(order_id, "confirmed")

    def test_referral_registers_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            is_new, referrer = db.upsert_user({"id": 100, "username": "inviter", "first_name": "A"})
            self.assertTrue(is_new)
            self.assertIsNone(referrer)
            self.assertTrue(REF_RE.match("ref100"))
            is_new_guest, guest_referrer = db.upsert_user(
                {"id": 200, "username": "guest", "first_name": "B"}, source="ref100"
            )
            self.assertTrue(is_new_guest)
            self.assertEqual(guest_referrer, 100)
            # повторный /start не должен дублировать приглашённого
            db.upsert_user({"id": 200, "username": "guest", "first_name": "B"}, source="ref100")
            self.assertEqual(db.get_user(100)["invited_count"], 1)
            self.assertIn(100, db.giveaway_pool(1))
            self.assertNotIn(200, db.giveaway_pool(1))
            self.assertEqual(db.top_referrers()[0]["user_id"], 100)

    def test_self_referral_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            _, referrer = db.upsert_user({"id": 7, "username": "solo"}, source="ref7")
            self.assertIsNone(referrer)
            self.assertEqual(db.get_user(7)["invited_count"], 0)

    def test_segments_and_interest(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 1, "username": "anon"})
            db.upsert_user({"id": 2, "username": "withphone"})
            db.set_phone(2, "+79990000000")
            db.upsert_user({"id": 3, "username": "hoodie_fan"})
            db.set_interest(3, "hoodie")
            db.create_order("r1", 2, {"id": "p1", "name": "P1"}, "M", "+79990000000")
            self.assertEqual(sorted(db.broadcast_audience("all")), [1, 2, 3])
            self.assertEqual(db.broadcast_audience("contacts"), [2])
            self.assertEqual(db.broadcast_audience("buyers"), [2])
            self.assertEqual(db.broadcast_audience("interest:hoodie"), [3])
            self.assertEqual(db.broadcast_audience("interest:tee"), [])

    def test_waitlist_is_unique_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 5, "username": "waiter"})
            product = {"id": "tee-sila-i-chest", "name": "СИЛА И ЧЕСТЬ"}
            self.assertTrue(db.add_to_waitlist(5, product, "L"))
            self.assertFalse(db.add_to_waitlist(5, product, "L"))
            self.assertEqual(db.stats()["waitlist"], 1)
            self.assertEqual(db.waitlist_user_ids("tee-sila-i-chest", "L"), [5])
            self.assertEqual(db.waitlist_user_ids("tee-sila-i-chest", "M"), [])
            self.assertEqual(db.waitlist_rows()[0]["product_name"], "СИЛА И ЧЕСТЬ")

    def test_csv_export_escapes_formulas(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 7, "username": "=FORMULA", "first_name": "+SUM", "last_name": ""})
            exported = db.export_users_csv().decode("utf-8-sig")
            self.assertIn("'=FORMULA", exported)
            self.assertIn("'+SUM", exported)

    def test_slugify_and_unique_ids(self):
        self.assertEqual(slugify("DROP 002 HOODIE"), "drop-002-hoodie")
        self.assertEqual(slugify("Худи «Город»"), "hudi-gorod")
        self.assertEqual(slugify("!!!"), "item")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), path)
            catalog = Catalog(path)
            first = catalog.add_product(
                {
                    "category": "hoodie",
                    "name": "DROP 002 HOODIE",
                    "price": "1 ₽",
                    "sizes": ["M"],
                    "description": "Тест",
                }
            )
            second = catalog.add_product(
                {
                    "category": "hoodie",
                    "name": "DROP 002 HOODIE",
                    "price": "2 ₽",
                    "sizes": ["L"],
                    "description": "Тест",
                }
            )
            self.assertEqual(first["id"], "drop-002-hoodie")
            self.assertEqual(second["id"], "drop-002-hoodie-2")
            self.assertIsNotNone(catalog.get(first["id"]))
            self.assertTrue(catalog.set_active(first["id"], False))
            self.assertIsNone(catalog.get(first["id"]))
            self.assertIsNotNone(catalog.get_any(first["id"]))
            # после перечитывания с диска скрытая вещь остаётся скрытой
            reloaded = Catalog(path)
            self.assertIsNone(reloaded.get(first["id"]))

    def test_consent_is_recorded_once_and_filters_audience(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 11, "username": "a"})
            db.upsert_user({"id": 12, "username": "b"})
            self.assertFalse(db.has_consent(11))
            db.set_consent(11)
            self.assertTrue(db.has_consent(11))
            self.assertEqual(db.broadcast_audience("consent"), [11])
            self.assertEqual(sorted(db.broadcast_audience("all")), [11, 12])
            self.assertEqual(db.stats()["consents"], 1)

    def _teaser_bot(self, directory, api):
        """Стенд бота на временной базе с настоящим каталогом."""
        root = Path(directory)
        catalog_path = root / "catalog.json"
        shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
        settings = Settings(
            token="fake",
            admin_ids=frozenset(),
            channel_url="https://t.me/channel",
            webapp_url="https://shop.example/app",
            manager_chat_id=None,
            brand_name="ВОРОЖБИТОВ",
            support_username="",
            database_path=root / "bot.sqlite3",
            catalog_path=catalog_path,
            health_port=8080,
            giveaway_min_invites=3,
            privacy_url="",
        )
        db = make_db(directory)
        return BrandBot(settings, api, db, Catalog(catalog_path)), db

    def test_teaser_is_uploaded_once_and_then_reused_by_file_id(self):
        """Ролик весит мегабайты: второй раз должен уходить одним file_id."""
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.videos = []

            def send_video(self, chat_id, video, caption="", reply_markup=None,
                           thumbnail=None, width=0, height=0, duration=0):
                self.videos.append(video)
                return {"video": {"file_id": "CACHED-ID"}}

            def send_message(self, chat_id, text, reply_markup=None):
                return {"message_id": 1}

        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            bot, db = self._teaser_bot(directory, api)
            self.assertTrue(bot.send_teaser(1))
            self.assertTrue(bot.send_teaser(1))
            # Первый раз — файл с диска, второй — уже кешированный идентификатор.
            self.assertIsInstance(api.videos[0], Path)
            self.assertEqual(api.videos[1], "CACHED-ID")
            self.assertEqual(api.videos[0], bot.local_asset_path(bot.catalog.data["media"]["teaser"]))

    def test_stale_teaser_file_id_is_dropped_and_resent(self):
        """Telegram забывает file_id — бот обязан молча перезалить ролик."""
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.calls = []

            def send_video(self, chat_id, video, caption="", reply_markup=None,
                           thumbnail=None, width=0, height=0, duration=0):
                self.calls.append(video)
                if video == "DEAD-ID":
                    raise RuntimeError("Telegram API error: wrong file identifier")
                return {"video": {"file_id": "DEAD-ID" if len(self.calls) == 1 else "FRESH-ID"}}

            def send_message(self, chat_id, text, reply_markup=None):
                return {"message_id": 1}

        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            bot, db = self._teaser_bot(directory, api)
            self.assertTrue(bot.send_teaser(1))
            self.assertTrue(bot.send_teaser(1))
            self.assertTrue(bot.send_teaser(1))
            self.assertEqual(api.calls[1], "DEAD-ID")
            self.assertIsInstance(api.calls[2], Path)
            self.assertEqual(api.calls[3], "FRESH-ID")

    def test_teaser_source_changes_invalidate_telegram_copy(self):
        class FakeAPI:
            def __init__(self):
                self.videos = []

            def send_video(self, chat_id, video, *args, **kwargs):
                self.videos.append(video)
                return {"video": {"file_id": f"COPY-{len(self.videos)}"}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "miniapp").mkdir()
            first = root / "miniapp" / "first.mp4"
            second = root / "miniapp" / "second.mp4"
            first.write_bytes(b"old-cut")
            second.write_bytes(b"new-cut")
            api = FakeAPI()
            bot, db = self._teaser_bot(directory, api)
            db.kv_set("teaser_file_id", "LEGACY-COPY")
            bot.catalog.data["media"]["teaser"] = "first.mp4"
            with patch("bot.BASE_DIR", root):
                self.assertTrue(bot.send_teaser(1))
                self.assertTrue(bot.send_teaser(1))
                bot.catalog.data["media"]["teaser"] = "second.mp4"
                self.assertTrue(bot.send_teaser(1))
                second.write_bytes(b"replacement-cut")
                self.assertTrue(bot.send_teaser(1))
                self.assertEqual(api.videos, [first.resolve(), "COPY-1", second.resolve(), second.resolve()])
                bot.catalog.data["media"]["teaser"] = "../catalog.json"
                self.assertFalse(bot.send_teaser(1))
                self.assertEqual(len(api.videos), 4)

    def test_product_card_shows_the_whole_garment_as_an_album(self):
        """В чате вещь тоже показывается со всех сторон, а не одним кадром."""
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.albums = []
                self.photos = []

            def send_media_group(self, chat_id, photos, caption="", labels=None):
                self.albums.append((photos, labels or []))
                return {"message_id": 1}

            def send_photo(self, chat_id, photo, caption, reply_markup=None):
                self.photos.append(photo)
                return {"message_id": 1}

            def send_message(self, chat_id, text, reply_markup=None):
                self.messages.append((text, reply_markup))
                return {"message_id": len(self.messages)}

        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            api.messages = []
            bot, _ = self._teaser_bot(directory, api)
            bot.show_product(1, 1, "tag-sila-i-chest")
            self.assertEqual(len(api.albums), 1, "карточка ушла без альбома")
            photos, labels = api.albums[0]
            self.assertGreaterEqual(len(photos), 2)
            self.assertEqual(api.photos, [], "альбом отправлен, одиночное фото лишнее")
            for url in photos:
                self.assertTrue(url.startswith("https://"), f"нелокальный адрес обязателен: {url}")
            # Подписи кадров — из каталога: у жетона их нет, значит и выдумывать нечего.
            self.assertEqual([label for label in labels if label], [])
            # Карточка вещи уходит отдельным сообщением вместе с кнопками.
            card, keyboard = api.messages[-1]
            self.assertIn("ЖЕТОН", card)
            self.assertIn("1 900 ₽", card)
            self.assertTrue(keyboard["inline_keyboard"][-1][-1]["callback_data"] == "menu")

    def test_webapp_order_is_validated_against_catalog_and_stored(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.sent = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            user = {"id": 77, "first_name": "Buyer", "username": "buyer"}
            payload = {
                "type": "order",
                "request_id": "web-test-001",
                "consent": True,
                "customer": {"name": "Buyer", "phone": "8 (999) 123-45-67", "city": "Могилёв"},
                "items": [
                    {"product_id": "tee-sila-i-chest", "size": "M", "quantity": 2},
                ],
            }
            brand_bot.handle_update({
                "update_id": 1,
                "message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": user,
                    "web_app_data": {"data": json.dumps(payload, ensure_ascii=False)},
                },
            })
            self.assertEqual(db.stats()["orders"], 1)
            order = db.recent_orders(1)[0]
            self.assertEqual(order["product_id"], "tee-sila-i-chest")
            self.assertEqual(order["quantity"], 2)
            self.assertEqual(order["phone"], "+79991234567")
            self.assertTrue(db.has_consent(77))
            self.assertIn("витрин", api.sent[-1][1].lower())

    def test_webapp_waitlist_is_recorded(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.sent = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            user = {"id": 88, "first_name": "Waiter", "username": "waiter"}
            payload = {"type": "waitlist", "product_id": "tee-sila-i-chest", "size": "L"}
            brand_bot.handle_update({
                "update_id": 2,
                "message": {
                    "chat": {"id": 88, "type": "private"},
                    "from": user,
                    "web_app_data": {"data": json.dumps(payload, ensure_ascii=False)},
                },
            })
            self.assertEqual(db.stats()["waitlist"], 1)
            self.assertEqual(db.waitlist_user_ids("tee-sila-i-chest", "L"), [88])

    def test_invalid_catalog_category_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(
                json.dumps(
                    {
                        "categories": [{"id": "valid", "name": "Valid"}],
                        "products": [
                            {
                                "id": "item",
                                "category": "missing",
                                "name": "Item",
                                "price": "1 ₽",
                                "sizes": ["M"],
                                "description": "Description",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                Catalog(path)

    def test_phone_is_not_saved_without_consent(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.sent = []
                self.videos = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

            def send_video(self, chat_id, video, caption="", reply_markup=None,
                           thumbnail=None, width=0, height=0, duration=0):
                self.videos.append(video)
                return {"video": {"file_id": "TEST-VIDEO-ID"}}

            def send_photo_file(self, chat_id, path, caption="", reply_markup=None):
                self.sent.append((chat_id, caption, reply_markup))
                return {"message_id": len(self.sent)}

            def send_photo(self, chat_id, photo, caption, reply_markup=None):
                self.sent.append((chat_id, caption, reply_markup))
                return {"message_id": len(self.sent)}

            def answer_callback(self, callback_id, text=""):
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            user = {"id": 91, "first_name": "NoConsent", "username": "nc"}
            brand_bot.handle_update({
                "update_id": 10,
                "message": {
                    "chat": {"id": 91, "type": "private"},
                    "from": user,
                    "text": "/start",
                },
            })
            brand_bot.handle_update({
                "update_id": 11,
                "callback_query": {
                    "id": "cb-size",
                    "data": "size:tee-sila-i-chest:M",
                    "from": user,
                    "message": {"chat": {"id": 91, "type": "private"}},
                },
            })
            brand_bot.handle_update({
                "update_id": 12,
                "message": {
                    "chat": {"id": 91, "type": "private"},
                    "from": user,
                    "text": "+79991112233",
                },
            })
            self.assertFalse(db.has_consent(91))
            self.assertIsNone(db.get_user(91)["phone"])
            self.assertEqual(db.stats()["orders"], 0)
            joined = " ".join(item[1] for item in api.sent)
            self.assertIn("согласи", joined.lower())

    def test_user_can_cancel_own_new_order(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 55, "username": "own", "first_name": "O"})
            order_id, _ = db.create_order(
                "own-1", 55, {"id": "p1", "name": "P1"}, "M", "+79990001122", note="Могилёв · СДЭК"
            )
            self.assertEqual(db.orders_for_user(55)[0]["note"], "Могилёв · СДЭК")
            order, changed = db.set_order_status(order_id, "cancelled")
            self.assertTrue(changed)
            self.assertEqual(order["status"], "cancelled")

    def test_webapp_profile_is_stored(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.sent = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            user = {"id": 44, "first_name": "Nikita", "username": "nv"}
            db.upsert_user(user)
            brand_bot.handle_update({
                "update_id": 3,
                "message": {
                    "chat": {"id": 44, "type": "private"},
                    "from": user,
                    "web_app_data": {"data": json.dumps({
                        "type": "profile",
                        "consent": True,
                        "profile": {
                            "name": "Никита",
                            "phone": "8 (999) 111-22-33",
                            "city": "Могилёв",
                            "address": "ул. Ленина, 1",
                            "entrance": "подъезд 2",
                            "deliver": "СДЭК",
                            "size": "L",
                        },
                    }, ensure_ascii=False)},
                },
            })
            stored = db.get_user(44)
            self.assertEqual(stored["phone"], "+79991112233")
            self.assertEqual(stored["city"], "Могилёв")
            self.assertEqual(stored["address"], "ул. Ленина, 1")
            self.assertEqual(stored["pref_size"], "L")
            self.assertTrue(db.has_consent(44))
            self.assertIn("ПРОФИЛЬ СОХРАНЁН", api.sent[-1][1])

    def test_start_sends_welcome_photo(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                self.photos = []
                self.videos = []
                self.messages = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_video(self, chat_id, video, caption="", reply_markup=None,
                           thumbnail=None, width=0, height=0, duration=0):
                self.videos.append(video)
                return {"video": {"file_id": "TEST-VIDEO-ID"}}

            def send_photo_file(self, chat_id, path, caption="", reply_markup=None):
                self.photos.append((chat_id, Path(path), caption, reply_markup))
                return {"message_id": len(self.photos)}

            def send_photo(self, chat_id, photo, caption, reply_markup=None):
                self.photos.append((chat_id, photo, caption, reply_markup))
                return {"message_id": len(self.photos)}

            def send_message(self, chat_id, text, reply_markup=None):
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            user = {"id": 501, "first_name": "Никита", "username": "nv"}
            brand_bot.handle_update({
                "update_id": 40,
                "message": {
                    "chat": {"id": 501, "type": "private"},
                    "from": user,
                    "text": "/start",
                },
            })
            self.assertEqual(len(api.photos), 1)
            self.assertEqual(api.messages, [])
            _chat_id, path, caption, keyboard = api.photos[0]
            self.assertEqual(path, brand_bot.local_asset_path(catalog.data["brand"]["welcome_image"]))
            self.assertIn("ВОРОЖБИТОВ", caption)
            self.assertIn("закрытой территории", caption)
            buttons = [btn["text"] for row in keyboard["inline_keyboard"] for btn in row]
            self.assertTrue(buttons)
            brand_bot.handle_update({
                "update_id": 41,
                "message": {
                    "chat": {"id": 501, "type": "private"},
                    "from": user,
                    "text": "/start",
                },
            })
            self.assertEqual(len(api.photos), 2)
            self.assertIn("уже в базе", api.photos[1][2])

    def test_webapp_init_data_must_be_signed(self):
        token = "123456:TESTTOKEN"
        now = 1_700_000_000
        user = {"id": 44, "first_name": "Никита"}
        payload = {
            "auth_date": str(now),
            "query_id": "AAEtest",
            "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
        }
        data_check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        payload["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        init_data = urllib.parse.urlencode(payload)
        parsed = webapp_user_from_init_data(token, init_data, now=now)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["id"], 44)
        tampered = init_data.replace("44", "99")
        self.assertIsNone(webapp_user_from_init_data(token, tampered, now=now))
        self.assertIsNone(webapp_user_from_init_data(token, init_data, now=now + 60 * 60 * 49))

    def test_miniapp_orders_api_is_scoped_to_signed_user(self):
        def request_json(url, init_data, method="GET", body=None):
            headers = {"X-Telegram-Init-Data": init_data, "Accept": "application/json"}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        def sign(token, user_id, now):
            payload = {
                "auth_date": str(now),
                "query_id": f"q{user_id}",
                "user": json.dumps({"id": user_id, "first_name": "N"}, separators=(",", ":")),
            }
            data_check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
            secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
            payload["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
            return urllib.parse.urlencode(payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            token = "123456:SHOPTOKEN"
            settings = Settings(
                token=token,
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="https://shop.example.com",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = Database(settings.database_path)
            db.upsert_user({"id": 77, "first_name": "Buyer"})
            db.upsert_user({"id": 88, "first_name": "Other"})
            order_id, _ = db.create_order(
                "web-scope-1", 77, {"id": "tee-sila-i-chest", "name": "ФУТБОЛКА"}, "M", "+79991234567"
            )
            catalog = Catalog(catalog_path)
            server = start_health_server(0, catalog, settings, db)
            try:
                port = server.server_address[1]
                now = int(time.time())
                owner = sign(token, 77, now)
                stranger = sign(token, 88, now)
                status, payload = request_json(f"http://127.0.0.1:{port}/api/my-orders", owner)
                self.assertEqual(status, 200)
                self.assertEqual(payload["source"], "bot")
                self.assertEqual(len(payload["orders"]), 1)
                self.assertEqual(payload["orders"][0]["id"], order_id)
                self.assertEqual(payload["orders"][0]["status"], "new")
                self.assertEqual(payload["orders"][0]["status_label"], "новая")
                self.assertTrue(payload["orders"][0]["can_cancel"])
                status, payload = request_json(f"http://127.0.0.1:{port}/api/my-orders", stranger)
                self.assertEqual(status, 200)
                self.assertEqual(payload["orders"], [])
                status, payload = request_json(f"http://127.0.0.1:{port}/api/my-orders", owner + "tamper")
                self.assertEqual(status, 401)
                status, payload = request_json(
                    f"http://127.0.0.1:{port}/api/my-orders/cancel",
                    owner,
                    method="POST",
                    body={"order_id": order_id},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["order"]["status"], "cancelled")
                self.assertEqual(db.get_order(order_id)["status"], "cancelled")
                status, payload = request_json(
                    f"http://127.0.0.1:{port}/api/my-orders/cancel",
                    stranger,
                    method="POST",
                    body={"order_id": order_id},
                )
                self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_price_and_stars_math(self):
        self.assertEqual(parse_price_rub("11 900 ₽"), 11900)
        self.assertEqual(parse_price_rub(""), 0)
        self.assertEqual(stars_amount(11900, 2.0), 5950)
        self.assertEqual(stars_amount(100, 0), 50)

    def test_price_parser_accepts_real_formats(self):
        for raw, expected in [
            ("11 900 ₽", 11900),
            ("4900", 4900),
            ("от 4900", 4900),
            ("3 200 руб", 3200),
            ("1 000 000 ₽", 1000000),
            ("4 900,50 ₽", 4900),  # копейки округляются до рубля
            ("4 900.49 ₽", 4900),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_price_strict(raw), expected)

    def test_ambiguous_price_is_rejected_instead_of_overcharging(self):
        """'1 200 - 1 500 ₽' раньше склеивалось в счёт на 12 001 500 ₽."""
        for raw in [
            "1 200 - 1 500 ₽",
            "4900 (со скидкой 3900)",
            "4.900 ₽",
            "цена по запросу",
            "",
            "99999999999",
        ]:
            with self.subTest(raw=raw):
                with self.assertRaises(PriceError):
                    parse_price_strict(raw)
                # Мягкая обёртка отдаёт 0: заявка примется, но счёт не выставится.
                with self.assertLogs("brand_bot.pay", level="WARNING"):
                    self.assertEqual(parse_price_rub(raw), 0)

    def test_personalization_is_validated_against_catalog(self):
        """Номер жетона приходит из браузера, поэтому проверяется по каталогу."""
        tag = {"id": "tag", "personalization": {"label": "НОМЕР ЖЕТОНА", "pattern": r"^[0-9]{1,5}$", "optional": True}}
        plain = {"id": "tee"}
        self.assertEqual(sanitize_personalization(tag, "00063"), "00063")
        self.assertEqual(sanitize_personalization(tag, "  00063 "), "00063")
        for bad in ["ABC", "<script>", "999999999", "63; DROP", ""]:
            with self.subTest(bad=bad):
                self.assertEqual(sanitize_personalization(tag, bad), "")
        # У товара без персонализации поле игнорируется целиком.
        self.assertEqual(sanitize_personalization(plain, "00063"), "")
        self.assertEqual(person_label(tag), "НОМЕР ЖЕТОНА")

    def test_real_drop_products_are_loadable(self):
        """Оба реальных лота живы, с фото и корректной ценой."""
        catalog = Catalog(Path(__file__).with_name("catalog.json"))
        tee = catalog.get("tee-sila-i-chest")
        tag = catalog.get("tag-sila-i-chest")
        self.assertIsNotNone(tee)
        self.assertIsNotNone(tag)
        self.assertEqual(parse_price_strict(tee["price"]), 4900)
        self.assertEqual(parse_price_strict(tag["price"]), 1900)
        self.assertIn("XXL", tee["sizes"])
        self.assertEqual(tag["sizes"], ["ONE SIZE"])
        root = Path(__file__).with_name("miniapp")
        for product in (tee, tag):
            for asset in [product["image"], *product.get("images", []), *product.get("spin", [])]:
                with self.subTest(asset=asset):
                    self.assertTrue((root / asset).is_file(), f"нет файла {asset}")
        media = catalog.data.get("media", {})
        for key in ("welcome_loop", "welcome_poster", "teaser", "teaser_poster"):
            with self.subTest(key=key):
                self.assertTrue((root / media[key]).is_file(), f"нет медиа {media[key]}")

    def test_video_is_served_with_range_support(self):
        """Без 206 на Range плеер в iOS не стартует."""
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(__file__).with_name("catalog.json"))
            server = start_health_server(0, catalog, None, None, None)
            port = server.server_address[1]
            try:
                base = f"http://127.0.0.1:{port}/assets/video/welcome-loop.mp4"
                request = urllib.request.Request(base, headers={"Range": "bytes=0-1"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
                    self.assertRegex(response.headers.get("Content-Range", ""), r"^bytes 0-1/\d+$")
                    self.assertEqual(len(response.read()), 2)
                with urllib.request.urlopen(base, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "video/mp4")
                    full = len(response.read())
                self.assertGreater(full, 1000)
                bad = urllib.request.Request(base, headers={"Range": f"bytes={full + 500}-"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(bad, timeout=5)
                self.assertEqual(caught.exception.code, 416)
            finally:
                server.shutdown()
                server.server_close()

    def test_index_stamps_asset_versions(self):
        """Без метки версии WebView Telegram показывает старый app.js."""
        with tempfile.TemporaryDirectory():
            catalog = Catalog(Path(__file__).with_name("catalog.json"))
            server = start_health_server(0, catalog, None, None, None)
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    page = response.read().decode("utf-8")
                self.assertRegex(page, r'src="app\.js\?v=\d+"')
                self.assertRegex(page, r'href="styles\.css\?v=[a-f0-9]+"')
                # Файл с меткой должен нормально отдаваться.
                stamped = re.search(r'src="(app\.js\?v=\d+)"', page).group(1)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/{stamped}", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("immutable", response.headers.get("Cache-Control", ""))
                for resource in ("app.js?v=stale", "?v=123"):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{resource}", timeout=5) as response:
                        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
            finally:
                server.shutdown()
                server.server_close()

    def test_font_replacement_invalidates_css_and_keeps_preload_in_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fonts").mkdir()
            font = root / "fonts" / "brand.woff2"
            font.write_bytes(b"font-v1")
            os.utime(font, (100, 100))
            (root / "styles.css").write_text('@font-face{src:url("fonts/brand.woff2")}', encoding="utf-8")
            (root / "index.html").write_text(
                '<link rel="preload" href="fonts/brand.woff2"><link rel="stylesheet" href="styles.css">',
                encoding="utf-8",
            )
            server = start_health_server(0)
            server.RequestHandlerClass.static_root = root
            base = f"http://127.0.0.1:{server.server_address[1]}/"
            try:
                def read_page():
                    with urllib.request.urlopen(base, timeout=5) as response:
                        page = response.read().decode()
                    css_url = re.search(r'href="(styles\.css\?v=[a-f0-9]+)"', page).group(1)
                    with urllib.request.urlopen(base + css_url, timeout=5) as response:
                        self.assertIn("immutable", response.headers.get("Cache-Control", ""))
                        css = response.read().decode()
                    font_url = re.search(r'href="(fonts/brand\.woff2\?v=\d+)"', page).group(1)
                    self.assertIn(font_url, css)
                    return css_url, font_url

                old_css, old_font = read_page()
                font.write_bytes(b"font-v2")
                os.utime(font, (200, 200))
                new_css, new_font = read_page()
                self.assertNotEqual(old_css, new_css)
                self.assertNotEqual(old_font, new_font)
            finally:
                server.shutdown()
                server.server_close()

    def test_real_products_show_the_garment_itself(self):
        """В обзоре должна крутиться вещь, а не упаковка и не лайфстайл."""
        catalog = Catalog(Path(__file__).with_name("catalog.json"))
        root = Path(__file__).with_name("miniapp")
        for product_id, minimum in (("tee-sila-i-chest", 8), ("tag-sila-i-chest", 2)):
            with self.subTest(product=product_id):
                product = catalog.get(product_id)
                self.assertGreaterEqual(len(product["spin"]), minimum)
                # Обзор — только студийные кадры товара.
                for frame in product["spin"]:
                    self.assertIn("assets/spin/", frame)
                    self.assertTrue((root / frame).is_file(), f"нет кадра {frame}")
                # Упаковке в карточке товара не место.
                everything = [product["image"], *product["images"], *product["spin"]]
                self.assertFalse([a for a in everything if "pack" in a], "упаковка попала в карточку")

    def test_product_photos_are_sharp_enough_to_sell(self):
        """Heuristic detail check at a common display size; visual review is still required."""
        try:
            from PIL import Image, ImageOps
            import numpy as np
        except ImportError:  # pragma: no cover - зависит от окружения
            self.skipTest("нужны Pillow и numpy")
        catalog = Catalog(Path(__file__).with_name("catalog.json"))
        root = Path(__file__).with_name("miniapp")
        # Pixel-scale sharpness is resolution-dependent. Compare at the same
        # display size before normalizing contrast for black-on-black photos.
        threshold = 0.02
        for product_id in ("tee-sila-i-chest", "tag-sila-i-chest"):
            product = catalog.get(product_id)
            for shot in dict.fromkeys([product["image"], *product["images"], *product["spin"]]):
                path = root / shot
                if not path.is_file():
                    continue
                with self.subTest(shot=shot):
                    with Image.open(path) as image:
                        display = ImageOps.contain(image.convert("L"), (800, 800), Image.Resampling.LANCZOS)
                        grey = np.asarray(display, dtype=float)
                    laplacian = (
                        grey[:-2, 1:-1] + grey[2:, 1:-1]
                        + grey[1:-1, :-2] + grey[1:-1, 2:]
                        - 4 * grey[1:-1, 1:-1]
                    )
                    detail = laplacian.var() / max(grey.var(), 1e-6)
                    self.assertGreater(
                        detail, threshold, f"{shot} размыт — такое фото продавать нельзя"
                    )

    def test_welcome_copy_is_structured_not_a_wall_of_text(self):
        """Обращение к своим должно читаться: зачин, факты и призыв — разными блоками."""
        miniapp = Path(__file__).with_name("miniapp")
        index = (miniapp / "index.html").read_text(encoding="utf-8")
        styles = (miniapp / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="welcome-lede"', index)
        self.assertIn('class="welcome-drop"', index)
        self.assertIn('class="welcome-facts"', index)
        # Текст остаётся тем же обращением, просто разложенным по строкам.
        self.assertIn("Брат.", index)
        self.assertIn("закрытую территорию", index)
        self.assertIn("Бери размер, пока он есть.", index)
        # Стили для новых блоков должны существовать, иначе разметка развалится.
        for selector in (".welcome-lede", ".welcome-drop", ".welcome-facts"):
            self.assertIn(selector, styles, f"нет стилей для {selector}")
        # Кнопки «Пропустить» нет: вход — только через «Войти в магазин».
        self.assertNotIn("welcomeClose", index)
        self.assertNotIn("Пропустить", index)
        self.assertNotIn(".welcome-close", styles)

    def test_teaser_has_no_photo_lead(self):
        """Фильм выпуска начинается сразу с видео: фото-заставки нет ни на карточке, ни в модалке."""
        miniapp = Path(__file__).with_name("miniapp")
        index = (miniapp / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="teaserPoster"', index)
        video = next(line for line in index.splitlines() if 'id="teaserVideo"' in line)
        self.assertNotIn("poster=", video)

    def test_teaser_starts_from_zero(self):
        """Монтаж тизера начинается с видео: никаких пропусков начала в плеере."""
        app = (Path(__file__).with_name("miniapp") / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("TEASER_SKIP_SECONDS", app)
        self.assertNotIn("cueTeaserStart", app)

    def test_welcome_video_has_no_poster_flash(self):
        """Фоновое видео стартует с чёрного/титра: у video нет постера, подложка-фото скрыта при загрузке и прячется при воспроизведении."""
        miniapp = Path(__file__).with_name("miniapp")
        lines = (miniapp / "index.html").read_text(encoding="utf-8").splitlines()
        at = next(i for i, line in enumerate(lines) if 'id="welcomeVideo"' in line)
        self.assertNotIn("poster=", "\n".join(lines[at:at + 3]))
        app = (miniapp / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("video.poster = media.welcomePoster", app)
        self.assertIn('fallback.classList.toggle("hidden", canPlay || !reducedMotion())', app)
        atf = next(i for i, line in enumerate(lines) if 'id="welcomeFallback"' in line)
        self.assertIn("hidden", lines[atf])

    def test_welcome_video_starts_loading_immediately(self):
        """Фильм грузится сразу: preload, мгновенная установка src и повторные попытки запуска."""
        miniapp = Path(__file__).with_name("miniapp")
        index = (miniapp / "index.html").read_text(encoding="utf-8")
        video = next(line for line in index.splitlines() if 'id="welcomeVideo"' in line)
        self.assertIn('preload="auto"', video)
        app = (miniapp / "app.js").read_text(encoding="utf-8")
        # Запуск не ждёт каталог: сетап при инициализации, на показе и по данным каталога.
        self.assertNotIn("if (state.catalogReady) setupWelcomeVideo", app)
        self.assertGreaterEqual(app.count("setupWelcomeVideo(mediaConfig())"), 2)
        self.assertIn("setupWelcomeVideo(media)", app)
        # Источник ставится до гейта воспроизведения и явно прогружается.
        self.assertIn("video.src = welcomePlayback.source", app)
        self.assertIn("video.load();", app)
        # WebView может резать автоплей: жест, возврат во вкладку и сторожевой таймер.
        self.assertIn('"pointerdown"', app)
        self.assertIn('"visibilitychange"', app)
        self.assertIn("welcomePlayback.watchdog", app)
        self.assertIn("}, 4000);", app)

    def test_welcome_video_keeps_playing_when_catalog_restamps(self):
        """Каталог приносит тот же файл с новым ?v-штампом — идущий ролик не перезапускается."""
        app = (Path(__file__).with_name("miniapp") / "app.js").read_text(encoding="utf-8")
        self.assertIn('video.classList.contains("is-playing") || (!video.paused && video.currentTime > 0)', app)
        self.assertIn("welcomePlayback.source !== media.welcomeLoop && !playing", app)

    def test_welcome_video_autostarts_itself(self):
        """Фоновое видео включается само: атрибут autoplay в разметке, беззвучие задано и свойством
        (часть WebView для автоплея смотрит только на свойство), старт повторяется по canplay."""
        miniapp = Path(__file__).with_name("miniapp")
        video = next(line for line in (miniapp / "index.html").read_text(encoding="utf-8").splitlines() if 'id="welcomeVideo"' in line)
        self.assertIn("autoplay", video)
        self.assertIn("muted", video)
        app = (miniapp / "app.js").read_text(encoding="utf-8")
        self.assertIn("video.muted = true", app)
        self.assertIn("video.defaultMuted = true", app)
        self.assertIn('video.addEventListener("canplay"', app)

    def test_welcome_autoplay_block_released_by_first_gesture(self):
        """Запрет автоплея (строгая политика, энергосбережение iOS) — не вечная пауза:
        NotAllowedError ставит отдельный флаг blocked, первый же жест его снимает,
        а ретрай-цепочка не крутится вхолостую, пока запрет ждёт жест."""
        app = (Path(__file__).with_name("miniapp") / "app.js").read_text(encoding="utf-8")
        self.assertIn("blocked: false", app)
        self.assertIn('error.name === "NotAllowedError"', app)
        self.assertIn("welcomePlayback.blocked = true", app)
        gesture = app[app.find('"pointerdown"'):][:400]
        self.assertIn("welcomePlayback.blocked = false", gesture)
        self.assertIn("video.paused && !welcomePlayback.blocked", app)

    def test_welcome_data_saver_shows_play_button(self):
        """Экономия трафика: автозагрузка не тратит мегабайты, но кнопка «ФИЛЬМ · 25 СЕК»
        видна со значком ▷ — осознанный тап даёт согласие на загрузку и запускает показ."""
        app = (Path(__file__).with_name("miniapp") / "app.js").read_text(encoding="utf-8")
        # Кнопку больше не прячет saver-режим — только отсутствие ролика, ошибка
        # или режим без движения.
        self.assertIn('control.classList.toggle("hidden", !welcomePlayback.ready || welcomePlayback.failed || reducedMotion())', app)
        self.assertNotIn("welcomePlayback.failed || saverMode() || reducedMotion()", app)
        # Тап по кнопке — согласие, после которого гейт экономии снят.
        self.assertIn("saverMode() && !welcomePlayback.saverOk", app)
        self.assertIn("welcomePlayback.saverOk = true", app)

    def test_welcome_video_not_cropped_on_narrow_screens(self):
        """Ролик 3:4 на узком экране показан целиком (contain): иначе cover режет
        титр «ВОРОЖБИТОВ» по бокам. Чёрные поля сливаются с фоном заставки."""
        styles = (Path(__file__).with_name("miniapp") / "styles.css").read_text(encoding="utf-8")
        marker = "@media (max-aspect-ratio: 3/4)"
        at = styles.find(marker)
        self.assertNotEqual(at, -1, "нет медиа-правила под узкие экраны")
        block = styles[at:at + 240]
        self.assertIn("video.welcome-media", block)
        self.assertIn("object-fit: contain", block)

    def test_welcome_has_solid_background(self):
        """У заставки сплошной фон: пока видео грузится, магазин под ней не просвечивает."""
        styles = (Path(__file__).with_name("miniapp") / "styles.css").read_text(encoding="utf-8")
        at = styles.find(".welcome {")
        self.assertNotEqual(at, -1, "нет правила .welcome")
        self.assertIn("background: #070708", styles[at:at + 400])

    def test_welcome_video_has_no_photo_insert(self):
        """Фоновый ролик — сплошное видео: статичная фото-вставка вырезана, титр и финал на месте."""
        miniapp = Path(__file__).with_name("miniapp")
        video = miniapp / "assets/video/welcome-final-30s.mp4"
        self.assertTrue(video.is_file(), "нет файла фонового ролика")
        # Было 28.4с с 3.6с фото-вставкой, стало ~24.8с чистого видео.
        duration = mp4_duration_seconds(video)
        self.assertGreater(duration, 20.0, f"ролик обрезан слишком сильно: {duration:.1f}с")
        self.assertLess(duration, 27.0, f"фото-вставка на месте: {duration:.1f}с")
        size_mb = video.stat().st_size / 1e6
        self.assertLess(size_mb, 3.0, f"фон раздулся до {size_mb:.1f} МБ — мобильный трафик")
        blob = video.read_bytes()
        moov, mdat = blob.find(b"moov"), blob.find(b"mdat")
        self.assertNotEqual(moov, -1, "в файле нет moov")
        self.assertLess(moov, mdat, "moov после mdat — нужен -movflags +faststart")
        # Плашка честно говорит новую длительность.
        index = (miniapp / "index.html").read_text(encoding="utf-8")
        self.assertIn("ФИЛЬМ · 25 СЕК", index)
        self.assertNotIn("ФИЛЬМ · 30 СЕК", index)

    def test_media_urls_carry_a_version_so_new_cuts_are_not_cached(self):
        """Видео кешируется на сутки по неизменному имени — без версии в адресе
        пользователь после замены ролика ещё сутки видит старый монтаж."""
        catalog = Catalog(Path(__file__).with_name("catalog.json"))
        server = start_health_server(0, catalog, None, None, None)
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/catalog", timeout=5) as response:
                media = json.loads(response.read().decode("utf-8"))["media"]
            for key in ("teaser", "teaser_poster", "welcome_loop", "welcome_poster"):
                with self.subTest(key=key):
                    self.assertRegex(media[key], r"\?v=\d+$", f"{key} без версии — попадёт в кеш")
            # Подписи в media — не адреса, версией их портить нельзя.
            self.assertNotIn("?v=", media.get("teaser_title", ""))
            # Адрес с версией обязан вести к настоящему файлу, а не в 404.
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/{media['teaser']}", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "video/mp4")
                self.assertIn("immutable", response.headers.get("Cache-Control", ""))
        finally:
            server.shutdown()
            server.server_close()

    def test_teaser_video_is_the_film_cut(self):
        """Подключённый тизер со звуком, faststart и размером менее 6 МБ."""
        media = Catalog(Path(__file__).with_name("catalog.json")).data["media"]
        video = Path(__file__).with_name("miniapp") / media["teaser"]
        self.assertTrue(video.is_file(), "нет файла тизера")
        size_mb = video.stat().st_size / 1e6
        self.assertLess(size_mb, 6.0, f"тизер раздулся до {size_mb:.1f} МБ — мобильный трафик")
        blob = video.read_bytes()
        # faststart: moov обязан идти раньше mdat, иначе видео не стартует по сети.
        moov, mdat = blob.find(b"moov"), blob.find(b"mdat")
        self.assertNotEqual(moov, -1, "в файле нет moov")
        self.assertLess(moov, mdat, "moov после mdat — нужен -movflags +faststart")
        # Звуковая дорожка: барабан и хор — половина впечатления от монтажа.
        self.assertIn(b"mp4a", blob[:moov + 200_000], "в тизере нет звуковой дорожки")

    def test_welcome_screen_always_shows_on_launch(self):
        """Приветствие — визитка бренда, оно не должно пропадать после входа."""
        app_js = (Path(__file__).with_name("miniapp") / "app.js").read_text(encoding="utf-8")
        index = (Path(__file__).with_name("miniapp") / "index.html").read_text(encoding="utf-8")
        # Флаг «уже заходил» в Telegram переживает перезапуск Mini App,
        # из-за него заставка переставала показываться совсем.
        self.assertNotIn("vorozhbitov_entered", app_js)
        self.assertIn('id="welcome"', index)
        self.assertIn('id="enterShop"', index)
        # Заставка обязана подниматься над магазином и уметь закрываться.
        self.assertIn("welcome.classList.remove(\"hidden\")", app_js)
        self.assertIn("function enterShop", app_js)

    def test_rate_limiter_blocks_burst_and_recovers(self):
        limiter = RateLimiter(limit=3, window_seconds=60)
        self.assertTrue(all(limiter.allow("user:1", now=100.0) for _ in range(3)))
        self.assertFalse(limiter.allow("user:1", now=100.0))
        # Другой ключ не задет общим счётчиком.
        self.assertTrue(limiter.allow("user:2", now=100.0))
        # Окно уехало — снова можно.
        self.assertTrue(limiter.allow("user:1", now=161.0))

    def test_database_connection_is_released_per_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            seen: list[int] = []

            def work() -> None:
                try:
                    db.connection().execute("SELECT 1").fetchone()
                    seen.append(1)
                finally:
                    db.close_current()

            threads = [threading.Thread(target=work) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(seen), 20)
            # Повторный вызов на потоке без соединения не должен падать.
            db.close_current()

    def test_payment_webhooks_verify_hmac(self):
        body = b'{"order_id":"p1","status":"success"}'
        lava_sign = hmac.new(b"hook", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_lava_webhook(body, lava_sign, "hook"))
        self.assertFalse(verify_lava_webhook(body, lava_sign, "other"))
        token = "crypto-token"
        secret = hashlib.sha256(token.encode()).digest()
        crypto_sign = hmac.new(secret, body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_crypto_webhook(body, crypto_sign, token))
        self.assertFalse(verify_crypto_webhook(body, crypto_sign, "nope"))

    def test_awaiting_payment_marks_paid_and_can_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(directory)
            db.upsert_user({"id": 42, "first_name": "N"})
            db.create_payment("aa11bb22cc33", 42, 4900, 2450)
            order_id, created = db.create_order(
                "pay-1",
                42,
                {"id": "p1", "name": "P1"},
                "M",
                "+79991234567",
                status="awaiting_payment",
                payment_id="aa11bb22cc33",
                amount_rub=4900,
            )
            self.assertTrue(created)
            self.assertEqual(db.get_order(order_id)["status"], "awaiting_payment")
            self.assertTrue(db.mark_payment_paid("aa11bb22cc33", "lava", "inv-1"))
            self.assertFalse(db.mark_payment_paid("aa11bb22cc33", "lava", "inv-1"))
            self.assertEqual(db.get_order(order_id)["status"], "paid")
            self.assertEqual(db.get_payment("aa11bb22cc33")["status"], "paid")
            with self.assertRaises(ValueError):
                db.set_order_status(order_id, "completed")
            confirmed, changed = db.set_order_status(order_id, "confirmed")
            self.assertTrue(changed)
            self.assertEqual(confirmed["status"], "confirmed")

            order_id_2, _ = db.create_order(
                "pay-2",
                42,
                {"id": "p2", "name": "P2"},
                "L",
                "+79991234567",
                status="awaiting_payment",
                payment_id="deadbeefcafe",
            )
            cancelled, changed = db.set_order_status(order_id_2, "cancelled")
            self.assertTrue(changed)
            self.assertEqual(cancelled["status"], "cancelled")

    def test_checkout_api_creates_payment_and_lava_webhook_marks_paid(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                super().__init__("test-token")
                self.sent = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

        def request_json(url, init_data=None, method="GET", body=None, extra_headers=None):
            headers = {"Accept": "application/json"}
            if init_data:
                headers["X-Telegram-Init-Data"] = init_data
            if extra_headers:
                headers.update(extra_headers)
            data = None
            if body is not None:
                if isinstance(body, bytes):
                    data = body
                else:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        def sign(token, user_id, now):
            payload = {
                "auth_date": str(now),
                "query_id": f"q{user_id}",
                "user": json.dumps({"id": user_id, "first_name": "N"}, separators=(",", ":")),
            }
            data_check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
            secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
            payload["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
            return urllib.parse.urlencode(payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            token = "123456:SHOPTOKEN"
            settings = Settings(
                token=token,
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="https://shop.example.com",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
                lava_shop_id="shop",
                lava_secret_key="secret",
                lava_hook_key="hook",
                crypto_pay_token="",
                stars_enabled=True,
                stars_rub_per_star=2.0,
            )
            db = Database(settings.database_path)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            server = start_health_server(0, catalog, settings, db, api)
            try:
                port = server.server_address[1]
                now = int(time.time())
                owner = sign(token, 77, now)
                with patch("bot.create_lava_invoice", return_value={"url": "https://lava.example/pay", "id": "inv"}):
                    status, payload = request_json(
                    f"http://127.0.0.1:{port}/api/checkout",
                    owner,
                    method="POST",
                    body={
                        "type": "order",
                        "request_id": "web-pay-1",
                        "consent": True,
                        "customer": {"name": "Buyer", "phone": "8 (999) 123-45-67", "city": "Могилёв"},
                        "items": [{"product_id": "tee-sila-i-chest", "size": "M", "quantity": 1}],
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                self.assertGreater(payload["amount_rub"], 0)
                self.assertEqual(payload["status"], "awaiting_payment")
                self.assertTrue(any(item["id"] == "stars" for item in payload["methods"]))
                payment_id = payload["payment_id"]
                order = db.recent_orders(1)[0]
                self.assertEqual(order["status"], "awaiting_payment")
                self.assertEqual(order["payment_id"], payment_id)

                body = json.dumps({"order_id": payment_id, "status": "success", "invoice_id": "lava-1"}).encode("utf-8")
                signature = hmac.new(b"hook", body, hashlib.sha256).hexdigest()
                status, paid = request_json(
                    f"http://127.0.0.1:{port}/api/payments/lava",
                    method="POST",
                    body=body,
                    extra_headers={"Content-Type": "application/json", "Signature": signature},
                )
                self.assertEqual(status, 200)
                self.assertTrue(paid["ok"])
                self.assertEqual(db.get_order(order["id"])["status"], "paid")
                self.assertEqual(db.get_payment(payment_id)["method"], "lava")

                status, forbidden = request_json(
                    f"http://127.0.0.1:{port}/api/payments/lava",
                    method="POST",
                    body=body,
                    extra_headers={"Content-Type": "application/json", "Signature": "dead"},
                )
                self.assertEqual(status, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_stars_pre_checkout_and_successful_payment(self):
        class FakeAPI(TelegramAPI):
            def __init__(self):
                self.pre = []
                self.sent = []

            def create_invoice_link(self, payload):
                return "https://t.me/invoice/test"

            def answer_pre_checkout(self, query_id, ok=True, error_message=""):
                self.pre.append((query_id, ok, error_message))
                return True

            def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))
                return {"message_id": len(self.sent)}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
            settings = Settings(
                token="fake",
                admin_ids=frozenset(),
                channel_url="https://t.me/channel",
                webapp_url="",
                manager_chat_id=None,
                brand_name="ВОРОЖБИТОВ",
                support_username="",
                database_path=root / "bot.sqlite3",
                catalog_path=catalog_path,
                health_port=8080,
                giveaway_min_invites=3,
                privacy_url="",
            )
            db = make_db(directory)
            catalog = Catalog(catalog_path)
            api = FakeAPI()
            brand_bot = BrandBot(settings, api, db, catalog)
            db.upsert_user({"id": 77, "first_name": "Buyer"})
            db.create_payment("aa11bb22cc33", 77, 4900, 2450)
            db.create_order(
                "stars-1",
                77,
                {"id": "p1", "name": "P1"},
                "M",
                "+79991234567",
                status="awaiting_payment",
                payment_id="aa11bb22cc33",
                amount_rub=4900,
            )
            brand_bot.handle_update(
                {
                    "update_id": 90,
                    "pre_checkout_query": {
                        "id": "pcq-1",
                        "from": {"id": 77, "first_name": "Buyer"},
                        "currency": "XTR",
                        "total_amount": 2450,
                        "invoice_payload": "aa11bb22cc33",
                    },
                }
            )
            self.assertEqual(api.pre[-1], ("pcq-1", True, ""))
            brand_bot.handle_update(
                {
                    "update_id": 91,
                    "message": {
                        "chat": {"id": 77, "type": "private"},
                        "from": {"id": 77, "first_name": "Buyer"},
                        "successful_payment": {
                            "currency": "XTR",
                            "total_amount": 2450,
                            "invoice_payload": "aa11bb22cc33",
                            "telegram_payment_charge_id": "charge-1",
                        },
                    },
                }
            )
            self.assertEqual(db.get_payment("aa11bb22cc33")["status"], "paid")
            self.assertEqual(db.orders_for_payment("aa11bb22cc33")[0]["status"], "paid")
            self.assertIn("Оплата прошла", api.sent[-1][1])


class MenuAPI(TelegramAPI):
    """Пишет всё, что бот «отправил»: меню проверяем как текст и как кнопки."""

    def __init__(self):
        super().__init__("test-token")
        self.sent = []
        self.albums = []
        self.photos = []
        self.videos = []
        self.invoices = []
        self.documents = []
        self.pre_checkout = []
        self.calls = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    def send_photo(self, chat_id, photo, caption, reply_markup=None):
        self.photos.append((chat_id, photo, caption, reply_markup))
        return {"message_id": len(self.photos), "photo": [{"file_id": "URL-COPY"}]}

    def send_photo_file(self, chat_id, path, caption="", reply_markup=None):
        self.photos.append((chat_id, path, caption, reply_markup))
        return {"message_id": len(self.photos), "photo": [{"file_id": f"FILE-{len(self.photos)}"}]}

    def send_video(self, chat_id, video, caption="", reply_markup=None, **kwargs):
        self.videos.append(video)
        return {"video": {"file_id": "VIDEO-COPY"}}

    def send_video_file(self, chat_id, path, caption="", reply_markup=None, **kwargs):
        """Локальный ролик: без заглушки тест ушёл бы в настоящий Telegram."""
        self.videos.append(Path(path).name)
        return {"video": {"file_id": "VIDEO-FILE"}}

    def send_media_group(self, chat_id, photos, caption="", labels=None):
        self.albums.append((chat_id, photos, caption, labels or []))
        return {"message_id": len(self.albums)}

    def send_invoice(self, chat_id, payload):
        self.invoices.append(payload)
        return {"message_id": 1}

    def create_invoice_link(self, payload):
        return "https://t.me/invoice/test"

    def send_document(self, chat_id, filename, content, caption=""):
        self.documents.append((chat_id, filename, len(content), caption))
        return {"message_id": len(self.sent)}

    def answer_pre_checkout(self, query_id, ok=True, error_message=""):
        self.pre_checkout.append((query_id, ok, error_message))
        return True

    def answer_callback(self, callback_id, text=""):
        return None

    def call(self, method, payload=None, timeout=70):
        """Никакой сети в тестах меню: запоминаем вызов и отдаём пустой ответ."""
        self.calls.append((method, payload))
        return {}

    def last(self, chat_id=None):
        rows = [item for item in self.sent if chat_id is None or item[0] == chat_id]
        return rows[-1] if rows else (None, "", None)

    def markups(self):
        """Все показанные клавиатуры: из текстовых сообщений и из фото-сообщений."""
        return [item[2] for item in self.sent] + [item[3] for item in self.photos]


def button_rows(markup):
    return [[button["text"] for button in row] for row in (markup or {}).get("inline_keyboard", [])]


def button_targets(markup):
    targets = []
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            targets.append(
                button.get("callback_data")
                or button.get("url")
                or (button.get("web_app") or {}).get("url")
                or ""
            )
    return targets


def flat_buttons(markup):
    return [text for row in button_rows(markup) for text in row]


class NativeMenuTests(unittest.TestCase):
    """Native-меню Telegram: шесть разделов, единые карточки, понятная навигация."""

    USER = {"id": 500, "username": "buyer", "first_name": "Никита"}

    def _bot(self, directory, api=None, **overrides):
        root = Path(directory)
        catalog_path = root / "catalog.json"
        shutil.copy(Path(__file__).with_name("catalog.json"), catalog_path)
        settings = Settings(
            token="fake",
            admin_ids=frozenset({1}),
            channel_url="https://t.me/channel",
            webapp_url="https://shop.example/app",
            manager_chat_id=900,
            brand_name="ВОРОЖБИТОВ",
            support_username="manager",
            database_path=root / "bot.sqlite3",
            catalog_path=catalog_path,
            health_port=8080,
            giveaway_min_invites=3,
            privacy_url="",
        )
        settings = replace(settings, **overrides)
        db = make_db(directory)
        return BrandBot(settings, api or MenuAPI(), db, Catalog(catalog_path)), db

    def _purchase(self, db, user_id=500, product_id="tee-sila-i-chest", size="L", quantity=1):
        product = Catalog(Path(__file__).with_name("catalog.json")).get(product_id)
        amount = parse_price_strict(product["price"]) * quantity
        receipt, _ = db.create_checkout(
            user_id, f"req-{user_id}-{product_id}-{size}", "fp",
            [{"product_id": product["id"], "name": product["name"], "size": size,
              "phone": "+79991234567", "quantity": quantity, "note": "",
              "amount_rub": amount, "person": "", "person_label": ""}],
            0,
        )
        return receipt

    def test_main_menu_is_six_sections_in_two_columns(self):
        """Шесть разделов по два в строке: не стена из двадцати кнопок."""
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = self._bot(directory)
            markup = bot.main_menu(500)
            rows = button_rows(markup)
            sections = rows[:3]
            self.assertTrue(all(len(row) == 2 for row in sections), rows)
            self.assertEqual(
                [text for row in sections for text in row],
                ["🛍 Каталог", "📦 Мои покупки", "📷 Образы", "🎬 Ролик", "👤 Кабинет", "💬 Поддержка"],
            )
            # Витрина остаётся входом, но не занимает системную кнопку «Меню».
            self.assertEqual(rows[-1], ["Открыть витрину ↗"])
            for label in flat_buttons(markup):
                self.assertLessEqual(len(label), 26, f"длинная подпись кнопки: {label}")

    def test_menu_button_shows_commands_not_the_miniapp(self):
        """Системная кнопка «Меню» открывает команды, а не уводит из чата."""
        self.assertEqual(CHAT_MENU_BUTTON, {"type": "commands"})
        buyer = {item["command"] for item in buyer_commands()}
        staff = {item["command"] for item in staff_commands()}
        self.assertEqual(buyer, {"start", "catalog", "orders", "support", "help"})
        # Служебные команды покупатель не видит: их вешаем отдельным scope.
        self.assertFalse(buyer & {"admin", "stats", "add", "broadcast"})
        self.assertTrue({"admin", "stats", "add", "broadcast"} <= staff)
        for item in buyer_commands() + staff_commands():
            self.assertTrue(item["description"], item)
            self.assertLessEqual(len(item["description"]), 30, item)

    def test_unpaid_purchase_lifts_continue_button_to_the_top(self):
        """Незакрытая оплата — одна широкая кнопка сверху, и она исчезает после оплаты."""
        with tempfile.TemporaryDirectory() as directory:
            bot, db = self._bot(directory)
            db.upsert_user(self.USER)
            self.assertIsNone(db.pending_payment(500))
            self.assertNotIn("Продолжить оплату", flat_buttons(bot.main_menu(500)))
            receipt = self._purchase(db)
            payment_id = receipt["payment_id"]
            rows = button_rows(bot.main_menu(500))
            self.assertEqual(len(rows[0]), 1, "главное действие обязано быть отдельной строкой")
            self.assertIn("Продолжить оплату", rows[0][0])
            self.assertEqual(button_targets(bot.main_menu(500))[0], f"draft:{payment_id}")
            db.mark_payment_paid(payment_id, "stars", "charge")
            self.assertNotIn("Продолжить оплату", flat_buttons(bot.main_menu(500)))

    def test_continue_payment_reuses_the_same_invoice(self):
        """Продолжение оплаты не создаёт новый счёт и проверяет актуальность."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            receipt = self._purchase(db)
            payment_id = receipt["payment_id"]
            before = db.connection().execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            bot.open_draft(500, 500, payment_id)
            self.assertEqual(db.connection().execute("SELECT COUNT(*) FROM payments").fetchone()[0], before)
            _chat, text, markup = api.last(500)
            self.assertIn("ОПЛАТА", text)
            self.assertIn("4 900 ₽", text)
            self.assertTrue(any(target.startswith("pay:") for target in button_targets(markup)))
            # Отменённая покупка не предлагает оплату, а объясняет состояние.
            db.set_order_status(receipt["order_ids"][0], "cancelled", customer_id=500)
            bot.open_draft(500, 500, payment_id)
            _chat, text, _markup = api.last(500)
            self.assertIn("НЕАКТУАЛЬНА", text)

    def test_purchase_card_answers_three_questions(self):
        """Карточка покупки: деньги, сборка, доставка и одно допустимое действие."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            receipt = self._purchase(db)
            order_id = receipt["order_ids"][0]
            bot.show_order(500, 500, order_id)
            _chat, text, markup = api.last(500)
            self.assertIn(f"ПОКУПКА №{order_id}", text)
            for line in ("Оплата:", "Сборка:", "Доставка:"):
                self.assertIn(line, text)
            self.assertIn("ждёт оплаты", text)
            targets = button_targets(markup)
            self.assertEqual(targets[0], f"draft:{receipt['payment_id']}")
            self.assertIn(f"ucancel:{order_id}", targets)
            # Оплата доступна, только пока счёт действительно можно оплатить.
            db.mark_payment_paid(receipt["payment_id"], "stars", "charge")
            db.set_order_status(order_id, "confirmed")
            bot.show_order(500, 500, order_id)
            _chat, text, markup = api.last(500)
            self.assertIn("оплачена", text)
            self.assertIn("собираем к отправке", text)
            self.assertNotIn(f"draft:{receipt['payment_id']}", button_targets(markup))
            self.assertNotIn(f"ucancel:{order_id}", button_targets(markup))

    def test_state_lines_follow_money_not_the_status_label(self):
        """Статус «подтверждена» возможен до оплаты — строка денег это не врёт."""
        # Счёта в боте нет — значит состояние денег уточняется, а не выдумывается.
        lines = order_state_lines("confirmed", None, 0)
        self.assertIn("уточняется", lines[0])
        self.assertIn("собираем к отправке", lines[1])

        paid = {"status": "paid", "method": "stars", "paid_at": "2026-09-12T10:00:00+00:00"}
        self.assertIn("звёзды Telegram", order_state_lines("confirmed", paid, 4900)[0])
        self.assertIn("отменена", order_state_lines("cancelled", {"status": "cancelled"}, 4900)[0])

    def test_back_goes_to_the_previous_screen_and_home_is_stable(self):
        """«Назад» ведёт назад, «Главная» всегда последняя кнопка."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.show_product(500, 500, "tee-sila-i-chest")
            _chat, _text, product_markup = api.last(500)
            self.assertEqual(button_targets(product_markup)[-2:], ["cat:drop", "menu"])
            self.assertIn("← Выпуск", flat_buttons(product_markup)[-2])
            bot.show_category(500, 500, "drop")
            self.assertEqual(button_targets(api.last(500)[2])[-2:], ["catalog", "menu"])
            bot.show_catalog(500, 500)
            self.assertEqual(button_targets(api.last(500)[2])[-1:], ["menu"])
            bot.choose_size(500, 500, "tee-sila-i-chest")
            self.assertEqual(button_targets(api.last(500)[2])[-2:], ["product:tee-sila-i-chest", "menu"])

    def test_size_guide_back_depends_on_where_it_was_opened(self):
        """Замеры открываются из разных мест — «Назад» ведёт обратно, а не домой."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.route_callback("cb", 500, 500, "size_guide:product:tee-sila-i-chest")
            self.assertEqual(button_targets(api.last(500)[2])[-2:], ["product:tee-sila-i-chest", "menu"])
            bot.route_callback("cb", 500, 500, "size_guide:catalog")
            self.assertEqual(button_targets(api.last(500)[2])[-2:], ["catalog", "menu"])
            bot.route_callback("cb", 500, 500, "size_guide:account")
            self.assertEqual(button_targets(api.last(500)[2])[-2:], ["account", "menu"])
            # Без известного происхождения — честный выход в главное меню.
            bot.route_callback("cb", 500, 500, "size_guide")
            targets = button_targets(api.last(500)[2])
            self.assertIn("catalog", targets)
            self.assertEqual(targets[-1], "menu")

    def test_every_screen_has_a_way_home(self):
        """Ни один экран не оставляет покупателя без возврата."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            screens = [
                ("catalog", lambda: bot.show_catalog(500, 500)),
                ("category", lambda: bot.show_category(500, 500, "drop")),
                ("product", lambda: bot.show_product(500, 500, "tee-sila-i-chest")),
                ("size", lambda: bot.choose_size(500, 500, "tee-sila-i-chest")),
                ("orders", lambda: bot.show_my_orders(500, 500)),
                ("account", lambda: bot.route_callback("cb", 500, 500, "account")),
                ("support", lambda: bot.show_support(500, 500)),
            ]
            for name, action in screens:
                api.sent.clear()
                action()
                markup = api.last(500)[2]
                self.assertIn("menu", button_targets(markup), f"нет возврата домой: {name}")

    def test_staff_reference_screens_are_not_dead_ends(self):
        """Справочные экраны команды ведут обратно в пульт — и пустые, и с данными."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})

            for command in ("/waitlist", "/top", "/giveaway 1"):
                api.sent.clear()
                bot.admin_command(1, 1, command)
                self.assertIn("adm:panel", button_targets(api.last(1)[2]), f"тупик: {command} (пусто)")

            db.add_to_waitlist(500, bot.catalog.get("tee-sila-i-chest"), "XL")
            db.upsert_user({"id": 600, "username": "friend", "first_name": "Друг"}, "ref500")
            for command in ("/waitlist", "/top"):
                api.sent.clear()
                bot.admin_command(1, 1, command)
                text, markup = api.last(1)[1], api.last(1)[2]
                self.assertIn("adm:panel", button_targets(markup), f"тупик: {command}")
            api.sent.clear()
            bot.admin_command(1, 1, "/waitlist")
            self.assertIn("Ждут размер: 1", api.last(1)[1])
            self.assertIn("/restock", api.last(1)[1])
            api.sent.clear()
            bot.admin_command(1, 1, "/top")
            self.assertIn("@buyer", api.last(1)[1])
            self.assertIn("/giveaway", api.last(1)[1])

    def test_support_request_carries_the_purchase_context(self):
        """Вопрос по покупке уходит менеджеру с номером и статусом."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            receipt = self._purchase(db)
            order_id = receipt["order_ids"][0]
            bot.forward_support(500, 500, order_id)
            manager_messages = [item for item in api.sent if item[0] == 900]
            self.assertEqual(len(manager_messages), 1)
            text = manager_messages[0][1]
            self.assertIn(f"№{order_id}", text)
            self.assertIn("СИЛА И ЧЕСТЬ", text)
            self.assertIn("Оплата:", text)
            _chat, answer, markup = api.last(500)
            self.assertIn("ВОПРОС ПЕРЕДАН", answer)
            self.assertIn(f"ord:{order_id}", button_targets(markup))

    def test_support_without_a_manager_still_leads_somewhere(self):
        """Без manager-чата поддержка не тупик: показываем состояние и контакт."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api, manager_chat_id=None)
            db.upsert_user(self.USER)
            order_id = self._purchase(db)["order_ids"][0]
            bot.forward_support(500, 500, order_id)
            _chat, text, markup = api.last(500)
            self.assertIn(f"ПОКУПКА №{order_id}", text)
            self.assertIn("@manager", text)
            self.assertIn("menu", button_targets(markup))

    def test_staff_panel_is_compact_and_returns_to_buyer_mode(self):
        """Пульт владельца: сводка сверху, разделы по два, выход в режим покупателя."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})
            bot.admin_panel(1)
            _chat, text, markup = api.last(1)
            self.assertIn("УПРАВЛЕНИЕ МАГАЗИНОМ", text)
            rows = button_rows(markup)
            self.assertLessEqual(len(rows), 5, rows)
            self.assertEqual(len(rows[0]), 1, "сводка — отдельное главное действие")
            self.assertEqual(button_targets(markup)[0], "adm:summary")
            self.assertEqual(button_targets(markup)[-1], "menu")
            bot.admin_summary(1)
            summary = api.last(1)[1]
            self.assertIn("СВОДКА МАГАЗИНА", summary)
            self.assertNotIn("interest:", summary)

    def test_unknown_and_stale_buttons_open_the_actual_screen(self):
        """Старая кнопка не молчит и не пугает: объясняем и открываем актуальное."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            self.assertFalse(bot.route_callback("cb", 500, 500, "legacy:unknown"))
            bot.handle_update({
                "update_id": 1,
                "callback_query": {
                    "id": "cb1", "data": "legacy:unknown", "from": self.USER,
                    "message": {"chat": {"id": 500, "type": "private"}},
                },
            })
            _chat, text, markup = api.last(500)
            self.assertIn("устарела", text)
            # Актуальный экран — главное меню: разделы на месте, тупика нет.
            self.assertIn("Выбери вещь", text)
            self.assertIn("catalog", button_targets(markup))
            bot.route_callback("cb", 500, 500, "product:honor-hoodie")
            _chat, text, markup = api.last(500)
            self.assertIn("ВЕЩЬ НЕ НАЙДЕНА", text)
            self.assertIn("catalog", button_targets(markup))

    def test_empty_states_lead_to_the_next_step(self):
        """Пустая корзина покупок ведёт в каталог, пустой раздел — в канал."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.show_my_orders(500, 500)
            _chat, text, markup = api.last(500)
            self.assertIn("Покупок пока нет", text)
            self.assertIn("catalog", button_targets(markup))
            bot.show_category(500, 500, "hoodie")
            _chat, text, markup = api.last(500)
            self.assertIn("пока пусто", text)
            self.assertIn(bot.settings.channel_url, button_targets(markup))
            self.assertIn("catalog", button_targets(markup))

    def test_catalog_counts_only_live_products(self):
        """Счётчики в разделах — по действующему каталогу, пустые разделы скрыты."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.show_catalog(500, 500)
            _chat, text, markup = api.last(500)
            self.assertIn("ВИТРИНА</b> · 2 вещи", text)
            labels = flat_buttons(markup)
            self.assertIn("Выпуск · 1 вещь", labels)
            self.assertIn("Аксессуары · 1 вещь", labels)
            self.assertNotIn("Худи", " ".join(labels))
            self.assertIn("size_guide:catalog", button_targets(markup))

    def test_album_labels_come_from_the_catalog(self):
        """Подписи кадров честные: берём их из каталога, а не придумываем."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.show_product(500, 500, "tee-sila-i-chest")
            self.assertEqual(len(api.albums), 1)
            _chat, photos, _caption, labels = api.albums[0]
            self.assertEqual(len(photos), len(labels))
            self.assertEqual(labels[:3], ["Спереди", "Сзади", "Ткань и принт"])
            card = api.last(500)[1]
            self.assertIn("СИЛА И ЧЕСТЬ", card)
            self.assertIn("4 900 ₽", card)
            self.assertIn("Размеры:", card)

    def test_long_card_is_not_cut_by_the_photo_caption(self):
        """Подпись фото limitata 1024 знаками — длинную карточку режем по смыслу."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, _ = self._bot(directory, api)
            long_text = "ЗАГОЛОВОК\n" + ("строка описания вещи\n" * 90)
            bot.send_card_photo(500, "https://shop.example/app/assets/x.jpg", long_text, {"inline_keyboard": []})
            self.assertEqual(len(api.photos), 1)
            self.assertLessEqual(len(api.photos[0][2]), 1024)
            self.assertEqual(len(api.sent), 1)
            self.assertIn("строка описания вещи", api.sent[0][1])

    def test_welcome_cover_is_uploaded_once(self):
        """Обложка бренда тяжёлая: второй /start идёт кешированным file_id."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, _ = self._bot(directory, api)
            bot.send_welcome(500, "первый", None)
            bot.send_welcome(500, "второй", None)
            self.assertIsInstance(api.photos[0][1], Path)
            self.assertEqual(api.photos[1][1], "FILE-1")

    def test_buyer_commands_open_the_same_screens_as_buttons(self):
        """Команды из системной кнопки «Меню» ведут на те же экраны."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            for command, marker in (("/catalog", "ВИТРИНА"), ("/orders", "МОИ ПОКУПКИ"),
                                    ("/support", "ПОДДЕРЖКА"), ("/help", "КАК ЭТО РАБОТАЕТ")):
                api.sent.clear()
                self.assertTrue(bot.user_command(500, 500, command))
                self.assertIn(marker, api.last(500)[1])
            api.sent.clear()
            bot.user_command(500, 500, "/menu")
            self.assertIn("Выбери вещь", api.last(500)[1])
            self.assertFalse(bot.user_command(500, 500, "/broadcast текст"))

    def test_account_screen_shows_real_contact_and_priority(self):
        """Кабинет показывает то, что действительно хранится."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            bot.route_callback("cb", 500, 500, "account")
            _chat, text, markup = api.last(500)
            self.assertIn("не указан", text)
            self.assertIn("Приглашено: 0", text)
            self.assertIn("profile", button_targets(markup))
            db.set_phone(500, "+79991234567")
            bot.route_callback("cb", 500, 500, "account")
            self.assertIn("+79991234567", api.last(500)[1])

    def test_every_button_the_bot_shows_is_routable(self):
        """Мёртвых кнопок нет: любой callback-data бот умеет разобрать."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})
            receipt = self._purchase(db)
            order_id = receipt["order_ids"][0]
            bot.show_catalog(500, 500)
            bot.show_category(500, 500, "drop")
            bot.show_category(500, 500, "hoodie")
            bot.show_product(500, 500, "tee-sila-i-chest")
            bot.choose_size(500, 500, "tee-sila-i-chest")
            bot.ask_waitlist_size(500, "tee-sila-i-chest")
            bot.show_size_guide(500, 500, "size_guide:product:tee-sila-i-chest".split(":", 1)[1])
            bot.show_my_orders(500, 500)
            bot.show_order(500, 500, order_id)
            bot.show_receipt(500, 500, receipt["payment_id"])
            bot.open_draft(500, 500, receipt["payment_id"])
            bot.show_referral(500, 500)
            bot.show_lookbook(500, 500)
            bot.route_callback("cb", 500, 500, "account")
            bot.show_support(500, 500)
            bot.support_orders(500, 500)
            bot.admin_panel(1)
            bot.admin_summary(1)
            targets = {
                target
                for markup in api.markups()
                for target in button_targets(markup)
                if target and not target.startswith(("http://", "https://", "tg://"))
            }
            self.assertTrue(targets)
            dead = []
            for target in sorted(targets):
                admin_only = target.split(":", 1)[0] in {"adm", "reload", "add", "broadcast", "export", "stats"}
                user_id = 1 if admin_only else 500
                if not bot.route_callback("cb", user_id, user_id, target):
                    dead.append(target)
            self.assertEqual(dead, [], "кнопки, которые бот не разбирает")

    def test_option_rows_pairs_equal_choices(self):
        """Равноправные варианты складываются в колонки, остаток не теряется."""
        self.assertEqual(option_rows([("a", "1"), ("b", "2"), ("c", "3")]),
                         [[("a", "1"), ("b", "2")], [("c", "3")]])
        self.assertEqual(option_rows([], 3), [])
        self.assertEqual(option_rows([("a", "1")], 1), [[("a", "1")]])

    def test_staff_orders_is_one_list_and_card_opens_by_number(self):
        """Покупки у команды — один список, карточка с действием открывается по номеру."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user(self.USER)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})
            waiting = self._purchase(db, product_id="tee-sila-i-chest", size="L")
            working = self._purchase(db, product_id="tag-sila-i-chest", size="ONE SIZE")
            db.mark_payment_paid(working["payment_id"], "stars", "charge")
            db.set_order_status(working["order_ids"][0], "confirmed")
            waiting_id = waiting["order_ids"][0]

            bot.admin_orders(1)
            sent = [item for item in api.sent if item[0] == 1]
            self.assertEqual(len(sent), 1, "список покупок обязан быть одним сообщением")
            text, markup = sent[0][1], sent[0][2]
            self.assertIn("<b>ПОКУПКИ</b>", text)
            self.assertIn("<b>Ждут оплаты</b>", text)
            self.assertIn("<b>В работе</b>", text)
            self.assertIn("@buyer", text)
            targets = button_targets(markup)
            self.assertIn(f"aord:{waiting_id}", targets)
            self.assertIn(f"aord:{working['order_ids'][0]}", targets)
            self.assertIn("adm:panel", targets)
            self.assertLessEqual(max(len(row) for row in button_rows(markup)), 3)

            api.sent.clear()
            self.assertTrue(bot.route_callback("cb", 1, 1, f"aord:{waiting_id}"))
            card_text, card_markup = api.last(1)[1], api.last(1)[2]
            self.assertIn(f"ПОКУПКА №{waiting_id}", card_text)
            self.assertIn("Клиент:", card_text)
            self.assertIn("💳 Оплата:", card_text)
            # Главное действие — следующий этап, нижний ряд — как у всех экранов команды.
            self.assertIn(f"order:{waiting_id}:paid", button_targets(card_markup))
            self.assertEqual(button_rows(card_markup)[-1], ["← Покупки", "🛠 Управление"])

            # Покупателю чужая карточка недоступна: кнопка для него просто устарела.
            self.assertFalse(bot.route_callback("cb", 500, 500, f"aord:{waiting_id}"))

    def test_add_wizard_counts_steps_and_shows_the_buyer_card(self):
        """Мастер /add: счётчик шагов, разделы в колонки, проверка — настоящей карточкой."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})

            bot.start_add_product(1, 1)
            text, markup = api.last(1)[1], api.last(1)[2]
            self.assertIn(f"ШАГ 1/{ADD_TOTAL}", text)
            self.assertEqual(len(button_rows(markup)[0]), 2, "разделы витрины — в две колонки")
            self.assertEqual(button_rows(markup)[-1], ["✖ Отмена"])

            bot.route_callback("cb", 1, 1, "addcat:access")
            self.assertIn(f"ШАГ 2/{ADD_TOTAL}", api.last(1)[1])
            for answer in ("DROP 002 CAP", "3 900 ₽", "ONE SIZE", "Кепка второго выпуска.", "/skip"):
                bot.handle_add_product_text(1, 1, answer)
            text, markup = api.last(1)[1], api.last(1)[2]
            self.assertIn(f"ШАГ {ADD_TOTAL}/{ADD_TOTAL}", text)
            # Владелец видит ровно ту карточку, которую получит покупатель.
            self.assertIn("<b>DROP 002 CAP</b>", text)
            self.assertIn("<b>3 900 ₽</b>", text)
            self.assertIn("Кепка второго выпуска.", text)
            self.assertIn("📏 Размеры: ONE SIZE", text)
            self.assertEqual(button_rows(markup), [["🛍 Опубликовать →"], ["✖ Отмена"]])

            bot.route_callback("cb", 1, 1, "add:publish")
            self.assertIn("ВЕЩЬ ОПУБЛИКОВАНА", api.last(1)[1])
            self.assertEqual(button_rows(api.last(1)[2])[0], ["🛍 Смотреть в витрине →"])
            self.assertIsNotNone(bot.catalog.get("drop-002-cap"))

    def test_broadcast_segments_are_paired_and_cancellable(self):
        """Сегменты рассылки в две колонки, отмена — отдельной строкой."""
        with tempfile.TemporaryDirectory() as directory:
            api = MenuAPI()
            bot, db = self._bot(directory, api)
            db.upsert_user({"id": 1, "username": "owner", "first_name": "Owner"})
            bot.admin_command(1, 1, "/broadcast ВЫПУСК СЕГОДНЯ В 19:00")
            rows = button_rows(api.last(1)[2])
            self.assertEqual(rows[-1], ["✖ Отмена"])
            self.assertTrue(all(len(row) == 2 for row in rows[:-1]), rows)
            targets = button_targets(api.last(1)[2])
            self.assertIn("seg:all", targets)
            self.assertIn("seg:interest:hoodie", targets)

    def test_navigation_rows_never_duplicate_home(self):
        """Если назад — это и есть главное меню, второй такой кнопки не появляется."""
        self.assertEqual(button_rows(inline_keyboard(nav_rows(("Главное меню", "menu")))),
                         [["← Главное меню"]])
        rows = button_rows(inline_keyboard(nav_rows(("Витрина", "catalog"))))
        self.assertEqual(rows, [["← Витрина", "🏠 Главная"]])

    def test_plural_and_segment_labels_are_readable(self):
        self.assertEqual([things_word(count) for count in (1, 2, 5, 11, 21)],
                         ["вещь", "вещи", "вещей", "вещей", "вещь"])
        self.assertEqual(plural(1, "получатель", "получателя", "получателей"), "получатель")
        categories = [{"id": "hoodie", "name": "Худи"}]
        self.assertEqual(audience_label("interest:hoodie", categories), "Интерес: Худи")
        self.assertEqual(audience_label("contacts", categories), "С номером")
        self.assertNotIn("interest:", audience_label("interest:gone", categories))



if __name__ == "__main__":
    unittest.main()
