"""Regression coverage for the 2026-09-08 production audit (no external network)."""
import copy
from dataclasses import replace
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bot import BrandBot, Catalog, Database, RateLimiter, Settings, STOP_EVENT, polling_loop, start_health_server


class CheckoutRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(
            token='audit-test-token', admin_ids=frozenset(), channel_url='https://t.me/test',
            webapp_url='https://example.com', manager_chat_id=None, brand_name='Test',
            support_username='', database_path=root / 'test.sqlite',
            catalog_path=Path(__file__).with_name('catalog.json'), health_port=0,
            giveaway_min_invites=3, privacy_url='',
        )
        self.db = Database(self.settings.database_path)
        self.addCleanup(self.db.close_current)
        self.api = Mock()
        self.api.create_invoice_link.return_value = 'https://t.me/invoice/mock'
        self.catalog = Catalog(self.settings.catalog_path)
        self.bot = BrandBot(self.settings, self.api, self.db, self.catalog)
        self.user = {'id': 420, 'first_name': 'Test'}
        self.payload = {'request_id': 'checkout-test', 'consent': True,
                        'customer': {'phone': '+79990000000', 'city': 'Test'},
                        'items': [{'product_id': 'tee-sila-i-chest', 'size': 'M', 'quantity': 1}]}

    def checkout(self, payload=None, user=None, notify=None):
        return self.bot.checkout_web_payload(user or self.user, payload or self.payload, notify_user=notify)

    def test_retry_returns_same_receipt_and_payment(self):
        first = self.checkout()
        again = self.checkout()
        self.assertTrue(again['ok'])
        self.assertEqual(first['payment_id'], again['payment_id'])
        self.assertEqual(first['order_ids'], again['order_ids'])
        self.assertEqual(self.db.stats()['orders'], 1)
        self.assertEqual(self.api.create_invoice_link.call_count, 1)

    def test_retry_uses_original_price_after_catalog_change(self):
        first = self.checkout()
        self.catalog.get('tee-sila-i-chest')['price'] = '6 900 ₽'
        self.assertEqual(self.checkout()['amount_rub'], first['amount_rub'])

    def test_modified_request_with_same_key_is_rejected(self):
        self.checkout()
        changed = copy.deepcopy(self.payload)
        changed['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1})
        self.assertFalse(self.checkout(changed)['ok'])
        self.assertEqual(self.db.stats()['orders'], 1)
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM payments').fetchone()[0], 1)

    def test_same_request_key_is_scoped_to_user(self):
        first = self.checkout()
        second = self.checkout(user={'id': 421, 'first_name': 'Other'})
        self.assertTrue(second['ok'])
        self.assertNotEqual(first['payment_id'], second['payment_id'])

    def test_payment_write_failure_rolls_back_all_orders(self):
        with patch.object(self.db, 'create_payment', side_effect=RuntimeError('storage failure')):
            with self.assertRaises(RuntimeError):
                self.checkout()
        self.assertEqual(self.db.stats()['orders'], 0)
        self.assertTrue(self.checkout()['ok'])

    def test_invalid_price_rejects_entire_checkout(self):
        self.catalog.get('tee-sila-i-chest')['price'] = 'bad price'
        payload = copy.deepcopy(self.payload)
        payload['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1})
        self.assertFalse(self.checkout(payload)['ok'])
        self.assertEqual(self.db.stats()['orders'], 0)

    def test_zero_price_is_not_a_paid_product(self):
        self.catalog.get('tee-sila-i-chest')['price'] = '0 ₽'
        self.assertFalse(self.checkout()['ok'])

    def test_unavailable_line_rejects_whole_cart_without_partial_payment(self):
        for invalid in [None, {'product_id': 'missing', 'size': 'M'},
                        {'product_id': 'tee-sila-i-chest', 'size': 'INVALID'}]:
            with self.subTest(invalid=invalid):
                payload = copy.deepcopy(self.payload)
                payload['items'].append(invalid)
                self.assertFalse(self.checkout(payload)['ok'])
                self.assertEqual(self.db.stats()['orders'], 0)
                self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM payments').fetchone()[0], 0)
        self.api.create_invoice_link.assert_not_called()

    def test_invalid_quantity_is_rejected_instead_of_silently_changed(self):
        for quantity in [0, -1, 21, 1.5, True, 'bad', '2']:
            with self.subTest(quantity=quantity):
                payload = copy.deepcopy(self.payload)
                payload['items'][0]['quantity'] = quantity
                self.assertFalse(self.checkout(payload)['ok'])
                self.assertEqual(self.db.stats()['orders'], 0)

    def test_maximum_quantity_is_charged_exactly(self):
        payload = copy.deepcopy(self.payload)
        payload['items'][0]['quantity'] = 20
        result = self.checkout(payload)
        self.assertTrue(result['ok'])
        self.assertEqual(result['amount_rub'], 4900 * 20)

    def test_paid_order_cannot_be_cancelled_by_customer(self):
        receipt = self.checkout()
        self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge')
        self.bot.cancel_own_order(420, 420, receipt['order_ids'][0])
        self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'paid')

    def test_cancel_invalidates_whole_payment_and_late_payment_needs_refund(self):
        payload = copy.deepcopy(self.payload)
        payload['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1})
        receipt = self.checkout(payload)
        self.bot.cancel_own_order(420, 420, receipt['order_ids'][0])
        self.assertTrue(all(r['status'] == 'cancelled' for r in self.db.orders_for_payment(receipt['payment_id'])))
        self.assertFalse(self.bot.resume_payment(420, receipt['payment_id'])['ok'])
        self.bot.handle_pre_checkout({'id': 'test', 'invoice_payload': receipt['payment_id'],
                                     'currency': 'XTR', 'total_amount': receipt['amount_stars'], 'from': self.user})
        self.assertFalse(self.api.answer_pre_checkout.call_args.args[1])
        self.assertTrue(self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'late-charge'))
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'refund_required')
        self.assertFalse(self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'late-charge'))
        self.bot.notify_paid(receipt['payment_id'])
        self.assertIn('возврат', self.api.send_message.call_args.args[1].lower())

    def test_notification_error_does_not_break_checkout_and_can_retry(self):
        self.api.send_message.side_effect = RuntimeError('Telegram unavailable')
        receipt = self.checkout(notify=420)
        self.assertTrue(receipt['ok'])
        self.bot.flush_notifications()
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 1)
        self.api.send_message.side_effect = None
        self.bot.flush_notifications(now=time.time() + 1000)
        self.assertEqual(self.db.connection().execute(
            'SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 0)
        self.assertEqual(self.checkout()['payment_id'], receipt['payment_id'])

    def test_parallel_retries_create_one_payment(self):
        def submit(_):
            try:
                return self.checkout()
            finally:
                self.db.close_current()
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(submit, range(4)))
        self.assertTrue(all(r['ok'] for r in receipts))
        self.assertEqual(len({r['payment_id'] for r in receipts}), 1)
        self.assertEqual(self.db.stats()['orders'], 1)

    def test_manual_payment_settles_all_positions(self):
        payload = copy.deepcopy(self.payload)
        payload['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1})
        receipt = self.checkout(payload)
        self.bot.update_order_status(1, receipt['order_ids'][0], 'paid')
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'paid')
        self.assertTrue(all(row['status'] == 'paid' for row in self.db.orders_for_payment(receipt['payment_id'])))
        self.assertFalse(self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'later-callback'))
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'paid')

    def test_crash_after_checkout_commit_keeps_notification_jobs(self):
        with patch.object(self.bot, 'build_pay_methods', side_effect=RuntimeError('crash after commit')):
            with self.assertRaises(RuntimeError):
                self.checkout(notify=420)
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        restarted = BrandBot(self.settings, self.api, self.db, self.catalog)
        self.assertTrue(restarted.checkout_web_payload(self.user, self.payload)['ok'])
        restarted.flush_notifications()
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 0)

    def test_paid_notification_is_queued_before_postcommit_delivery(self):
        receipt = self.checkout()
        info = {'invoice_payload': receipt['payment_id'], 'currency': 'XTR',
                'total_amount': receipt['amount_stars'], 'telegram_payment_charge_id': 'charge'}
        with patch.object(self.bot, 'flush_notifications', side_effect=RuntimeError('crash after commit')):
            with self.assertRaises(RuntimeError):
                self.bot.handle_successful_payment(420, 420, info)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'paid')
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 1)
        self.bot.flush_notifications()
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 0)

    def test_late_line_failure_rolls_back_payment_receipt_and_outbox(self):
        payload = copy.deepcopy(self.payload)
        payload['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1})
        self.db.connection().executescript("""CREATE TRIGGER reject_hoodie BEFORE INSERT ON orders
            WHEN NEW.product_id='tag-sila-i-chest' BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.checkout(payload, notify=420)
        for table in ('orders', 'payments', 'checkouts', 'notifications'):
            self.assertEqual(self.db.connection().execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.db.connection().execute('DROP TRIGGER reject_hoodie')
        self.assertTrue(self.checkout(payload)['ok'])

    def test_legacy_request_does_not_create_another_checkout(self):
        self.db.upsert_user(self.user)
        self.db.create_payment('legacy', 420, 4900, 2450)
        self.db.create_order('checkout-test-1-tee-sila-i-chest-M', 420, self.catalog.get('tee-sila-i-chest'),
                             'M', '+79990000000', status='awaiting_payment', payment_id='legacy', amount_rub=4900)
        response = self.checkout()
        self.assertFalse(response['ok'])
        self.assertIn('Мои покупки', response['error'])
        self.assertEqual(self.db.stats()['orders'], 1)

    def test_legacy_chat_callback_does_not_duplicate_order(self):
        self.db.upsert_user(self.user)
        self.db.create_payment('legacy-chat', 420, 4900, 2450)
        self.db.create_order('telegram-callback-123', 420, self.catalog.get('tee-sila-i-chest'),
                             'M', '+79990000000', status='awaiting_payment', payment_id='legacy-chat', amount_rub=4900)
        self.bot.finish_order(420, 420, self.catalog.get('tee-sila-i-chest'), 'M', '+79990000000', 'telegram-callback-123')
        self.assertEqual(self.db.stats()['orders'], 1)
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM payments').fetchone()[0], 1)

    def test_stars_amount_survives_config_change_and_cache_expiry(self):
        receipt = self.checkout()
        self.bot.settings = replace(self.settings, stars_rub_per_star=7)
        self.db.connection().execute('DELETE FROM payment_methods')
        self.bot.resume_payment(420, receipt['payment_id'])
        self.assertEqual(self.api.create_invoice_link.call_args.args[0]['prices'][0]['amount'], receipt['amount_stars'])

    def test_line_snapshot_is_the_only_source_of_payment_total(self):
        # The first pass validates, the second captures the actual immutable price.
        with patch.object(self.bot, 'line_amount', side_effect=[4900, 9900]):
            receipt = self.checkout()
        self.assertEqual(receipt['amount_rub'], 9900)
        self.assertEqual(receipt['amount_stars'], 4950)
        self.assertEqual(self.api.create_invoice_link.call_args.args[0]['prices'][0]['amount'], 4950)

    def test_additive_migration_preserves_existing_payment_and_orders(self):
        receipt = self.checkout()
        for table in ('checkouts', 'payment_methods', 'notifications'):
            self.db.connection().execute(f'DROP TABLE {table}')
        self.db.close_current()
        reopened = Database(self.settings.database_path)
        try:
            self.assertEqual(reopened.get_payment(receipt['payment_id'])['amount_rub'], receipt['amount_rub'])
            self.assertEqual(len(reopened.orders_for_payment(receipt['payment_id'])), 1)
            for table in ('checkouts', 'payment_methods', 'notifications'):
                self.assertEqual(reopened.connection().execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        finally:
            reopened.close_current()

    def test_invalid_checkout_inputs_never_create_orders(self):
        invalid = [dict(self.payload, consent=False), dict(self.payload, customer={'phone': '123'}),
                   dict(self.payload, items=[]), dict(self.payload, items='not-a-list'),
                   dict(self.payload, items=[None]),
                   dict(self.payload, items=[{'product_id': 'tee-sila-i-chest', 'size': 'INVALID'}])]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertFalse(self.checkout(payload)['ok'])
                self.assertEqual(self.db.stats()['orders'], 0)

    def test_customer_cannot_cancel_another_owners_order(self):
        receipt = self.checkout()
        with self.assertRaises(ValueError):
            self.db.set_order_status(receipt['order_ids'][0], 'cancelled', customer_id=421)
        self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'awaiting_payment')

    def test_admin_cancellation_of_paid_order_queues_refund_notice(self):
        self.bot.settings = replace(self.settings, manager_chat_id=1)
        receipt = self.checkout(notify=420)
        self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge')
        self.bot.update_order_status(1, receipt['order_ids'][0], 'cancelled')
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'refund_required')
        messages = [call.args[1] for call in self.api.send_message.call_args_list]
        self.assertTrue(any('ВОЗВРАТ' in text for text in messages))
        self.assertTrue(any('возврата' in text for text in messages))
        status, data = self.http('/api/my-orders')
        self.assertEqual(status, 200)
        self.assertIn('возврат', json.loads(data)['orders'][0]['status_label'])

    def test_cancelled_checkout_does_not_deliver_stale_payment_offer(self):
        receipt = self.checkout(notify=420)
        self.db.set_order_status(receipt['order_ids'][0], 'cancelled', customer_id=420)
        self.bot.flush_notifications()
        self.api.send_message.assert_not_called()
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM notifications WHERE delivered_at IS NULL').fetchone()[0], 0)

    def test_notification_enqueue_failure_rolls_back_checkout(self):
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('outbox storage unavailable')):
            with self.assertRaises(RuntimeError):
                self.checkout(notify=420)
        self.assertEqual(self.db.stats()['orders'], 0)
        self.assertEqual(self.db.connection().execute('SELECT COUNT(*) FROM payments').fetchone()[0], 0)
        self.assertTrue(self.checkout(notify=420)['ok'])

    def test_payment_notification_write_failure_rolls_back_payment_status(self):
        receipt = self.checkout()
        def fail(_):
            raise RuntimeError('outbox storage unavailable')
        with self.assertRaises(RuntimeError):
            self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge', queue_notifications=fail)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'pending')
        self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'awaiting_payment')
        self.assertTrue(self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge'))
        self.assertFalse(self.db.mark_payment_paid('missing', 'stars', 'missing-charge'))

    def test_cancel_racing_payment_has_consistent_outcome(self):
        from threading import Barrier
        receipt = self.checkout()
        barrier = Barrier(2)
        def pay():
            try:
                barrier.wait()
                self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge')
            finally:
                self.db.close_current()
        def cancel():
            try:
                barrier.wait()
                try:
                    self.db.set_order_status(receipt['order_ids'][0], 'cancelled', customer_id=420)
                except ValueError:
                    pass  # Payment won the transaction; customer cancellation must fail.
            finally:
                self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(pay), pool.submit(cancel)]
            for future in futures:
                future.result(timeout=5)
        pair = (self.db.get_payment(receipt['payment_id'])['status'], self.db.get_order(receipt['order_ids'][0])['status'])
        self.assertIn(pair, {('paid', 'paid'), ('refund_required', 'cancelled')})

    def test_precheckout_rejects_wrong_user_currency_or_amount(self):
        receipt = self.checkout()
        query = {'id': 'pcq', 'invoice_payload': receipt['payment_id'], 'currency': 'XTR',
                 'total_amount': receipt['amount_stars'], 'from': self.user}
        for change in ({'from': {'id': 421}}, {'currency': 'RUB'}, {'total_amount': 1}, {'invoice_payload': 'missing'}):
            self.bot.handle_pre_checkout(dict(query, **change))
            self.assertFalse(self.api.answer_pre_checkout.call_args.args[1])

    def http(self, path, body=None, signed=True):
        if not hasattr(self, 'server'):
            self.server = start_health_server(0, self.catalog, self.settings, self.db, self.api)
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        headers = {'Content-Type': 'application/json'}
        if signed:
            auth = {'auth_date': str(int(time.time())), 'user': json.dumps(self.user)}
            secret = hmac.new(b'WebAppData', self.settings.token.encode(), hashlib.sha256).digest()
            auth['hash'] = hmac.new(secret, '\n'.join(f'{k}={auth[k]}' for k in sorted(auth)).encode(), hashlib.sha256).hexdigest()
            headers['X-Telegram-Init-Data'] = urllib.parse.urlencode(auth)
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}',
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def test_http_cancel_rejects_paid_order(self):
        receipt = self.checkout()
        self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'charge')
        status, _ = self.http('/api/my-orders/cancel', {'order_id': receipt['order_ids'][0]})
        self.assertEqual(status, 409)

    def test_waitlist_http_is_authenticated_and_idempotent(self):
        body = {'product_id': 'tee-sila-i-chest', 'size': 'M'}
        self.assertEqual(self.http('/api/waitlist', body, signed=False)[0], 401)
        self.assertEqual(self.http('/api/waitlist', body)[0], 200)
        self.assertEqual(self.http('/api/waitlist', body)[0], 200)
        self.assertEqual(self.db.stats()['waitlist'], 1)

    def test_app_slash_redirects_to_working_assets(self):
        status, body = self.http('/app/', signed=False)
        self.assertEqual(status, 200)
        self.assertIn(b'app.js', body)
        for path in ('/app.js', '/styles.css', '/viewer3d.js'):
            self.assertEqual(self.http(path, signed=False)[0], 200)
        with urllib.request.urlopen(f'http://127.0.0.1:{self.server.server_port}/app/', timeout=5) as response:
            self.assertEqual(urllib.parse.urlparse(response.url).path, '/')


class LimiterRegressionTests(unittest.TestCase):
    def test_expired_keys_are_reclaimed(self):
        limiter = RateLimiter(2, 60)
        for i in range(5000):
            limiter.allow(str(i), now=0)
        limiter.allow('fresh', now=1000)
        self.assertLessEqual(len(limiter.hits), 1)

    def test_forwarded_headers_require_an_explicit_trusted_proxy(self):
        from bot import StorefrontHandler
        handler = object.__new__(StorefrontHandler)
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = {'X-Forwarded-For': '203.0.113.1', 'X-Real-IP': '203.0.113.2'}
        handler.settings = Mock(trusted_proxy_ips=frozenset())
        self.assertEqual(handler._client_key(), '127.0.0.1')
        handler.settings.trusted_proxy_ips = frozenset({'127.0.0.1'})
        self.assertEqual(handler._client_key(), '203.0.113.2')
        handler.headers['X-Real-IP'] = 'invalid, value'
        self.assertEqual(handler._client_key(), '127.0.0.1')

    def test_active_key_count_is_bounded(self):
        limiter = RateLimiter(2, 60)
        for i in range(10000):
            limiter.allow(str(i), now=0)
        self.assertLessEqual(len(limiter.hits), 4096)


if __name__ == '__main__':
    unittest.main()


class WaitlistRegressionTests(unittest.TestCase):
    """Лист ожидания под нагрузкой: гонки, сбой отправки и старая схема базы."""

    PRODUCT = 'tee-sila-i-chest'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(
            token='audit-test-token', admin_ids=frozenset({1}), channel_url='https://t.me/test',
            webapp_url='https://example.com', manager_chat_id=None, brand_name='Test',
            support_username='', database_path=root / 'test.sqlite',
            catalog_path=Path(__file__).with_name('catalog.json'), health_port=0,
            giveaway_min_invites=3, privacy_url='',
        )
        self.db = Database(self.settings.database_path)
        self.addCleanup(self.db.close_current)
        self.api = Mock()
        self.api.create_invoice_link.return_value = 'https://t.me/invoice/mock'
        self.catalog = Catalog(self.settings.catalog_path)
        self.bot = BrandBot(self.settings, self.api, self.db, self.catalog)

    def waiter(self, user_id, size='M'):
        self.db.upsert_user({'id': user_id, 'first_name': f'U{user_id}'})
        self.assertTrue(self.db.add_to_waitlist(user_id, self.catalog.get(self.PRODUCT), size))

    def pings(self, user_id):
        return sum(1 for call in self.api.send_message.call_args_list
                   if call.args and call.args[0] == user_id and 'РАЗМЕР ВЕРНУЛСЯ' in str(call.args[1]))

    def test_buying_the_size_removes_the_waitlist_entry(self):
        self.waiter(420, 'M')
        self.bot.checkout_web_payload({'id': 420, 'first_name': 'Test'}, {
            'request_id': 'wait-1', 'consent': True,
            'customer': {'phone': '+79990000000', 'city': 'Test'},
            'items': [{'product_id': self.PRODUCT, 'size': 'M', 'quantity': 1}]})
        self.assertEqual(self.db.waitlist_user_ids(self.PRODUCT, 'M'), [])
        self.assertEqual(self.db.waitlist_user_ids(self.PRODUCT, 'S'), [])

    def test_repeat_restock_notifies_once(self):
        self.waiter(421)
        self.assertEqual(self.bot.notify_waitlist(self.PRODUCT, 'M'), 1)
        self.assertEqual(self.bot.notify_waitlist(self.PRODUCT, 'M'), 0)
        self.assertEqual(self.pings(421), 1)

    def test_concurrent_restock_sends_one_message_per_waiter(self):
        """Шесть одновременных /restock давали 48 одинаковых сообщений восьмерым."""
        waiters = list(range(430, 438))
        for uid in waiters:
            self.waiter(uid, 'S')
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.bot.notify_waitlist(self.PRODUCT, 'S'), range(6)))
        self.assertEqual(sum(results), len(waiters))
        for uid in waiters:
            self.assertEqual(self.pings(uid), 1, f'{uid} получил больше одного сообщения')

    def test_failed_notification_keeps_the_waiter_queued(self):
        self.waiter(441); self.waiter(442)
        # Считаем только реально ушедшие сообщения: Mock помнит и неудачные вызовы.
        delivered = []
        failing = {441}

        def send(chat_id, text, reply_markup=None):
            if chat_id in failing:
                raise RuntimeError('Telegram HTTP 429: too many requests')
            delivered.append((chat_id, text))
            return {'ok': True}

        self.api.send_message.side_effect = send
        self.assertEqual(self.bot.notify_waitlist(self.PRODUCT, 'M'), 1)
        failing.clear()
        self.assertEqual(self.bot.notify_waitlist(self.PRODUCT, 'M'), 1, 'недошедшее сообщение повторили')
        pings = lambda uid: sum(1 for chat_id, text in delivered
                                if chat_id == uid and 'РАЗМЕР ВЕРНУЛСЯ' in text)
        self.assertEqual(pings(441), 1)
        self.assertEqual(pings(442), 1, 'доставленного раньше не дублировали')

    def test_old_database_gains_the_notified_at_column(self):
        legacy = Path(self.temp.name) / 'legacy.sqlite'
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """CREATE TABLE waitlist (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                 product_id TEXT NOT NULL, product_name TEXT NOT NULL, size TEXT NOT NULL,
                 created_at TEXT NOT NULL, UNIQUE(user_id, product_id, size));
               INSERT INTO waitlist(user_id, product_id, product_name, size, created_at)
                 VALUES (5, 'tee', 'СИЛА', 'M', '2026-01-01');"""
        )
        conn.commit(); conn.close()
        migrated = Database(legacy)
        self.addCleanup(migrated.close_current)
        columns = [row[1] for row in migrated.connection().execute('PRAGMA table_info(waitlist)')]
        self.assertIn('notified_at', columns)
        self.assertEqual(migrated.connection().execute('SELECT COUNT(*) FROM waitlist').fetchone()[0], 1)
        self.assertEqual(migrated.claim_waitlist('tee', 'M'), [(5, 1)])
        self.assertEqual(migrated.claim_waitlist('tee', 'M'), [])


class PollingLoopRegressionTests(unittest.TestCase):
    """Очередь обновлений: одно отравленное обновление не должно останавливать бота."""

    class FakeAPI:
        def __init__(self, batches):
            self.batches, self.offsets = list(batches), []

        def call(self, method, payload=None, timeout=None):
            self.offsets.append(payload.get('offset'))
            if not self.batches:
                STOP_EVENT.set()
                return []
            return self.batches.pop(0)

    class FakeBot:
        def __init__(self, poison=()):
            self.seen, self.poison = [], set(poison)

        def handle_update(self, update):
            uid = update.get('update_id') if isinstance(update, dict) else None
            self.seen.append(uid)
            if uid in self.poison:
                raise RuntimeError('сбой вокруг обработчика')
            return True

    def poll(self, batches, poison=()):
        STOP_EVENT.clear()
        self.addCleanup(STOP_EVENT.clear)
        api = self.FakeAPI(batches)
        bot = self.FakeBot(poison)
        polling_loop(api, bot)
        return api, bot

    def test_poison_update_is_skipped_and_the_queue_moves_on(self):
        """Без пропуска смещения бот вечно перезапрашивал бы одно обновление."""
        batch = [{'update_id': 1}, {'update_id': 2}, {'update_id': 3},
                 {'message': {}}, 'не словарь', {'update_id': '5'}]
        api, bot = self.poll([batch, [{'update_id': 9}], []], poison={2})
        self.assertEqual(bot.seen, [1, 2, 3, None, '5', 9])
        self.assertGreaterEqual(api.offsets[-1], 6)

    def test_unexpected_getupdates_payload_does_not_stop_polling(self):
        api, bot = self.poll([{'unexpected': 'dict вместо списка'}, [{'update_id': 7}], []])
        self.assertEqual(bot.seen, [7])
        self.assertGreaterEqual(api.offsets[-1], 8)


class OwnerAccessRegressionTests(unittest.TestCase):
    """Владелец раздаёт и снимает доступ команды; история розыгрышей проверяема."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(
            token='owner-test-token', admin_ids=frozenset({1}), channel_url='https://t.me/test',
            webapp_url='https://example.com', manager_chat_id=None, brand_name='Test',
            support_username='', database_path=root / 'test.sqlite',
            catalog_path=Path(__file__).with_name('catalog.json'), health_port=0,
            giveaway_min_invites=3, privacy_url='',
        )
        self.db = Database(self.settings.database_path)
        self.addCleanup(self.db.close_current)
        self.api = Mock()
        self.bot = BrandBot(self.settings, self.api, self.db, Catalog(self.settings.catalog_path))

    def texts(self):
        return " ".join(str(call.args[1]) for call in self.api.send_message.call_args_list)

    def test_owner_grants_admin_and_admin_cannot_grant(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 7, 'username': 'member', 'first_name': 'Мира'})
        self.db.upsert_user({'id': 8, 'username': 'newbie', 'first_name': 'Ника'})
        self.assertFalse(self.bot.is_admin(7))
        self.assertTrue(self.bot.admin_command(1, 1, '/grant 7'))
        self.assertTrue(self.db.is_admin_id(7))
        self.assertTrue(self.bot.is_admin(7))
        self.assertFalse(self.bot.is_owner(7))
        self.assertIn('теперь админ', self.texts())
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 7, '/grant 8'))
        self.assertFalse(self.db.is_admin_id(8))
        self.assertIn('только владелец', self.texts())

    def test_unknown_stranger_cannot_be_granted(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.assertTrue(self.bot.admin_command(1, 1, '/grant 999'))
        self.assertFalse(self.db.is_admin_id(999))
        self.assertIn('нет в базе', self.texts())

    def test_revoke_by_button_is_owner_only(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 7, 'username': 'member', 'first_name': 'Мира'})
        self.db.add_admin(7, 1)
        self.assertFalse(self.bot.route_callback('cb1', 1, 7, 'access:revoke:7'))
        self.assertTrue(self.db.is_admin_id(7))
        self.assertTrue(self.bot.route_callback('cb2', 1, 1, 'access:revoke:7'))
        self.assertFalse(self.db.is_admin_id(7))
        self.assertIn('Доступ снят', self.texts())

    def test_access_screen_lists_owner_and_admins(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 7, 'username': 'member', 'first_name': 'Мира'})
        self.db.add_admin(7, 1)
        self.assertTrue(self.bot.admin_command(1, 1, '/access'))
        text = self.texts()
        self.assertIn('ДОСТУП КОМАНДЫ', text)
        self.assertIn('@owner — владелец', text)
        self.assertIn('@member — админ', text)

    def test_panel_offers_access_and_draws_within_five_rows(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.bot.admin_panel(1)
        markup = self.api.send_message.call_args_list[-1].args[2]['inline_keyboard']
        targets = [button['callback_data'] for row in markup for button in row]
        self.assertIn('adm:access', targets)
        self.assertIn('adm:draws', targets)
        self.assertLessEqual(len(markup), 5, markup)

    def test_draws_screen_shows_last_draw_and_empty_state(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 2, 'username': 'two', 'first_name': 'Рина'})
        self.assertTrue(self.bot.admin_command(1, 1, '/draws'))
        self.assertIn('Тиражей ещё не было', self.texts())
        self.db.event(1, 'giveaway_drawn', {'winners': [2], 'pool': 1, 'count': 1})
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 1, '/draws'))
        self.assertIn('@two', self.texts())

    def test_start_menu_gives_staff_the_management_window(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 7, 'username': 'member', 'first_name': 'Мира'})
        self.db.upsert_user({'id': 9, 'username': 'buyer', 'first_name': 'Боря'})
        self.db.add_admin(7, 1)
        for who, expected in ((1, True), (7, True), (9, False)):
            markup = self.bot.main_menu(who)['inline_keyboard']
            targets = [b['callback_data'] for row in markup for b in row]
            self.assertEqual('adm:panel' in targets, expected, who)

    def test_reports_screen_lists_sent_broadcasts(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.assertTrue(self.bot.admin_command(1, 1, '/reports'))
        self.assertIn('Рассылок ещё не было', " ".join(
            str(c.args[1]) for c in self.api.send_message.call_args_list))
        self.db.event(1, 'broadcast_sent', {'delivered': 8, 'failed': 0, 'segment': 'all'})
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 1, '/reports'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('доставлено 8', text)

    def test_summary_offers_money_and_digest_entries(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.bot.admin_summary(1)
        markup = self.api.send_message.call_args_list[-1].args[2]['inline_keyboard']
        targets = [b['callback_data'] for row in markup for b in row]
        self.assertIn('adm:money', targets)
        self.assertIn('adm:digest', targets)

    def test_money_screen_shows_revenue_and_average_check(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        from bot import utc_now
        self.db.connection().execute(
            "INSERT INTO payments(payment_id, user_id, amount_rub, amount_stars, status, "
            "method, created_at, paid_at) VALUES ('p1', 5, 4900, 0, 'paid', 'stars', ?, ?)",
            (utc_now(), utc_now()))
        self.assertTrue(self.bot.admin_command(1, 1, '/money'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('ДЕНЬГИ', text)
        self.assertIn('4 900', text)
        self.assertIn('Средний чек', text)

    def test_digest_lists_deficit_sizes_and_clean_states(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 5, 'username': 'waiter', 'first_name': 'Ждан'})
        from bot import utc_now
        self.db.connection().execute(
            "INSERT INTO waitlist(user_id, product_id, product_name, size, created_at) "
            "VALUES (5, 'tee-sila-i-chest', 'Футболка «Сила и честь»', 'XXL', ?)", (utc_now(),))
        self.assertTrue(self.bot.admin_command(1, 1, '/digest'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('ЧТО СЕГОДНЯ', text)
        self.assertIn('XXL ×1', text)
        self.assertIn('Возвратов нет.', text)

    def test_waitlist_screen_restocks_in_one_tap(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 5, 'username': 'waiter', 'first_name': 'Ждан'})
        self.db.set_consent(5)
        from bot import utc_now
        self.db.connection().execute(
            "INSERT INTO waitlist(user_id, product_id, product_name, size, created_at) "
            "VALUES (5, 'tee-sila-i-chest', 'Футболка «Сила и честь»', 'XXL', ?)", (utc_now(),))
        self.assertTrue(self.bot.admin_command(1, 1, '/waitlist'))
        markup = self.api.send_message.call_args_list[-1].args[2]['inline_keyboard']
        targets = [b['callback_data'] for row in markup for b in row]
        self.assertIn('wnotify:tee-sila-i-chest:XXL', targets)
        self.api.send_message.reset_mock()
        self.assertFalse(self.bot.route_callback('cb1', 1, 5, 'wnotify:tee-sila-i-chest:XXL'))
        self.assertTrue(self.bot.route_callback('cb2', 1, 1, 'wnotify:tee-sila-i-chest:XXL'))
        texts = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('РАЗМЕР ВЕРНУЛСЯ', texts)
        self.assertIn('Уведомлено по листу ожидания: 1', texts)
        self.assertEqual(self.db.waitlist_unnotified(), [])

    def test_order_card_links_customer_screen_with_money(self):
        import sqlite3 as _sql
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 5, 'username': 'client', 'first_name': 'Кира'})
        self.db.set_phone(5, '+79995556677')
        from bot import utc_now
        self.db.connection().execute(
            "INSERT INTO orders(user_id, product_id, product_name, size, quantity, "
            "amount_rub, status, created_at) VALUES (5, 'tee', 'Футболка', 'L', 1, 4900, 'paid', ?)",
            (utc_now(),))
        order_id = self.db.connection().execute("SELECT id FROM orders ORDER BY id DESC").fetchone()[0]
        self.bot.admin_order_card(1, order_id)
        markup = self.api.send_message.call_args_list[-1].args[2]['inline_keyboard']
        targets = [b['callback_data'] for row in markup for b in row]
        self.assertIn('aclient:5', targets)
        self.assertEqual(targets[-1], 'menu')
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.route_callback('cb', 1, 1, 'aclient:5'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('КЛИЕНТ', text)
        self.assertIn('+79995556677', text)
        self.assertIn('4 900', text)

    def test_shelf_button_hides_and_shows_product(self):
        import shutil
        from bot import Catalog
        catalog_copy = Path(self.temp.name) / 'catalog.json'
        shutil.copy(Path(__file__).with_name('catalog.json'), catalog_copy)
        self.bot.catalog = Catalog(catalog_copy)
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.assertTrue(self.bot.admin_command(1, 1, '/shelf'))
        self.assertIsNotNone(self.bot.catalog.get('tee-sila-i-chest'))
        self.assertTrue(self.bot.route_callback('cb1', 1, 1, 'shelf:tee-sila-i-chest'))
        self.assertIsNone(self.bot.catalog.get('tee-sila-i-chest'))
        self.assertTrue(self.bot.route_callback('cb2', 1, 1, 'shelf:tee-sila-i-chest'))
        self.assertIsNotNone(self.bot.catalog.get('tee-sila-i-chest'))

    def test_draw_threshold_buttons_are_owner_only(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 7, 'username': 'member', 'first_name': 'Мира'})
        self.db.add_admin(7, 1)
        self.db.event(1, 'giveaway_drawn', {'winners': [1], 'pool': 1, 'count': 1})
        self.assertFalse(self.bot.route_callback('cb1', 1, 7, 'draw:up'))
        self.assertEqual(self.bot.giveaway_threshold(), 3)
        self.assertTrue(self.bot.route_callback('cb2', 1, 1, 'draw:up'))
        self.assertEqual(self.bot.giveaway_threshold(), 4)
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 7, '/top'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Приоритет дают с 4', text)

    def _paid_order_with_payment(self):
        import sqlite3 as _sql
        from bot import utc_now
        self.db.upsert_user({'id': 5, 'username': 'client', 'first_name': 'Кира'})
        conn = self.db.connection()
        conn.execute(
            "INSERT INTO payments(payment_id, user_id, amount_rub, status, method, created_at, paid_at) "
            "VALUES ('pay-t1', 5, 4900, 'paid', 'card', ?, ?)", (utc_now(), utc_now()))
        conn.execute(
            "INSERT INTO orders(user_id, product_id, product_name, size, quantity, "
            "amount_rub, status, payment_id, created_at) "
            "VALUES (5, 'tee', 'Футболка', 'L', 1, 4900, 'paid', 'pay-t1', ?)", (utc_now(),))
        return conn.execute("SELECT id FROM orders ORDER BY id DESC").fetchone()[0]

    def test_order_note_button_saves_and_shows(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        order_id = self._paid_order_with_payment()
        self.assertTrue(self.bot.route_callback('cb1', 1, 1, f'anote:{order_id}'))
        self.assertTrue(self.bot.handle_order_note_text(1, 1, 'Забрать в субботу'))
        self.assertEqual(self.db.get_order(order_id)['note'], 'Забрать в субботу')
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Забрать в субботу', text)
        self.api.send_message.reset_mock()
        self.bot.db.set_state(1, 'order_note', {'order': order_id})
        self.assertTrue(self.bot.handle_order_note_text(1, 1, 'без заметки'))
        self.assertEqual(self.db.get_order(order_id)['note'], '')

    def test_refund_reason_buttons_and_done(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        order_id = self._paid_order_with_payment()
        self.assertTrue(self.bot.route_callback('cb1', 1, 1, f'order:{order_id}:cancelled'))
        open_rows = self.db.refunds_open()
        self.assertEqual(len(open_rows), 1)
        pid = open_rows[0]['payment_id']
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 1, '/refunds'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Причина не указана', text)
        self.assertTrue(self.bot.route_callback('cb2', 1, 1, f'rreasons:{pid}'))
        self.assertTrue(self.bot.route_callback('cb3', 1, 1, f'rreason:{pid}:size'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Не подошёл размер', text)
        self.assertTrue(self.bot.route_callback('cb4', 1, 1, f'rdone:{pid}'))
        self.assertEqual(self.db.refunds_open(), [])
        self.assertEqual(self.db.money_stats()['refunds'], 0)
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Выполнено недавно', text)

    def test_again_repeats_last_broadcast(self):
        self.db.upsert_user({'id': 1, 'username': 'owner', 'first_name': 'Owner'})
        self.db.upsert_user({'id': 5, 'username': 'client', 'first_name': 'Кира'})
        self.db.set_phone(5, '+79995556677')
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 1, '/again'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Прошлой рассылки нет', text)
        self.db.set_state(1, 'broadcast_pending', {'text': 'Скидка до вечера', 'segment': 'all'})
        self.bot.confirm_broadcast(1, 1)
        import time as _t
        _t.sleep(0.4)
        self.assertIn('Скидка до вечера', self.db.kv_get('last_broadcast'))
        self.api.send_message.reset_mock()
        self.assertTrue(self.bot.admin_command(1, 1, '/again'))
        text = " ".join(str(c.args[1]) for c in self.api.send_message.call_args_list)
        self.assertIn('Скидка до вечера', text)
        self.assertIn('ПРЕДПРОСМОТР', text)
