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

from bot import (
    BASE_DIR,
    BrandBot,
    Catalog,
    Database,
    RateLimiter,
    REF_RE,
    Settings,
    TelegramAPI,
    ensure_catalog_exists,
    normalize_phone,
    person_label,
    sanitize_personalization,
    slugify,
    start_health_server,
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

            def send_media_group(self, chat_id, photos, caption=""):
                self.albums.append(photos)
                return {"message_id": 1}

            def send_photo(self, chat_id, photo, caption, reply_markup=None):
                self.photos.append(photo)
                return {"message_id": 1}

            def send_message(self, chat_id, text, reply_markup=None):
                return {"message_id": 1}

        with tempfile.TemporaryDirectory() as directory:
            api = FakeAPI()
            bot, _ = self._teaser_bot(directory, api)
            bot.show_product(1, 1, "tag-sila-i-chest")
            self.assertEqual(len(api.albums), 1, "карточка ушла без альбома")
            self.assertGreaterEqual(len(api.albums[0]), 2)
            self.assertEqual(api.photos, [], "альбом отправлен, одиночное фото лишнее")
            for url in api.albums[0]:
                self.assertTrue(url.startswith("https://"), f"нелокальный адрес обязателен: {url}")

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
            self.assertIn("Профиль", api.sent[-1][1])

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
        for selector in (".welcome-lede", ".welcome-drop", ".welcome-facts", ".welcome-close"):
            self.assertIn(selector, styles, f"нет стилей для {selector}")

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


if __name__ == "__main__":
    unittest.main()
