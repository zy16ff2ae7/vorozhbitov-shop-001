"""Acceptance regressions for AUDIT-2026-09-10. No real payments or external I/O."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import bot as app
import payments
import test_regressions as fixtures
from bot import BrandBot, Catalog, Database, polling_loop, start_health_server


class AuditFixTests(unittest.TestCase):
    setUp = fixtures.CheckoutRegressionTests.setUp
    checkout = fixtures.CheckoutRegressionTests.checkout
    confirm_stars = fixtures.CheckoutRegressionTests.confirm_stars

    def records(self, table):
        return list(self.db.connection().execute(f'SELECT * FROM {table}'))

    def issue(self, receipt, method, identity, ref=None):
        self.db.save_invoice(receipt['payment_id'], method, identity, receipt['amount_rub'] * 100,
                             'RUB', f'https://pay.example/{identity}', external_ref=ref)

    def receive(self, receipt, method, identity, **overrides):
        values = {'amount_minor': receipt['amount_stars'] if method == 'stars' else receipt['amount_rub'] * 100,
                  'currency': 'XTR' if method == 'stars' else 'RUB',
                  'payer_id': 420 if method == 'stars' else None,
                  'queue_notifications': lambda pid: self.bot.notify_paid(pid, queue_only=True),
                  'queue_review': self.bot.notify_payment_review}
        values.update(overrides)
        return self.db.mark_payment_paid(receipt['payment_id'], method, identity, **values)

    def stars_update(self, receipt, identity='stars-charge'):
        return {'update_id': 700, 'message': {'chat': {'id': 420, 'type': 'private'}, 'from': self.user,
            'successful_payment': {'invoice_payload': receipt['payment_id'], 'currency': 'XTR',
                'total_amount': receipt['amount_stars'], 'telegram_payment_charge_id': identity}}}

    def test_distinct_charges_preserved_without_second_fulfillment(self):
        self.bot.settings = replace(self.settings, manager_chat_id=9001)
        receipt = self.checkout()
        before_notifications = len(self.records('notifications'))
        first = 'lava-' + 'a' * 250
        self.issue(receipt, 'lava', first)
        self.issue(receipt, 'crypto', 'crypto-2')
        self.issue(receipt, 'lava', 'lava-3')
        for method, identity in [('lava', first), ('crypto', 'crypto-2'), ('lava', 'lava-3'), ('stars', 'stars-4')]:
            self.assertTrue(self.receive(receipt, method, identity))
            self.assertFalse(self.receive(receipt, method, identity))
        rows = self.records('payment_receipts')
        self.assertEqual([row['status'] for row in rows], ['applied'] + ['refund_required'] * 3)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['provider_id'], first)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'paid')
        self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'paid')
        self.assertEqual(len(self.records('notifications')) - before_notifications, 8)
        self.assertEqual(self.db.connection().execute("SELECT COUNT(*) FROM events WHERE event='payment_paid'").fetchone()[0], 1)

    def test_same_charge_cannot_pay_another_order_and_conflict_is_durable(self):
        receipt = self.checkout()
        other = self.checkout({**self.payload, 'request_id': 'other-order'})
        self.receive(receipt, 'stars', 'charge')
        for _ in range(2):
            self.assertFalse(self.receive(other, 'stars', 'charge'))
        self.assertEqual(len(self.records('payment_receipts')), 1)
        self.assertEqual(len(self.records('payment_receipt_conflicts')), 1)
        self.assertEqual(self.db.get_payment(other['payment_id'])['status'], 'pending')

    def test_signed_invoice_reference_mismatch_quarantines_canonical_payment(self):
        receipt = self.checkout()
        other = self.checkout({**self.payload, 'request_id': 'other-order'})
        self.issue(receipt, 'lava', 'issued', ref='provider-reference')
        self.receive(other, 'lava', 'issued')
        row = self.records('payment_receipts')[0]
        self.assertEqual(row['payment_id'], receipt['payment_id'])
        self.assertIn('invoice_reference_mismatch', row['reason'])
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'review_required')
        self.assertEqual(self.db.get_payment(other['payment_id'])['status'], 'pending')

    def test_unknown_invoice_blocks_payment_and_is_not_bootstrapped_from_webhook(self):
        receipt = self.checkout()
        self.receive(receipt, 'lava', 'never-issued')
        self.assertEqual(self.records('payment_receipts')[0]['status'], 'review_required')
        self.assertEqual(self.records('payment_invoices'), [])
        self.assertFalse(self.db.payment_is_payable(receipt['payment_id']))
        self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'awaiting_payment')
        self.assertEqual(len(self.records('notifications')), 1)

    def test_stars_success_rechecks_owner_amount_and_currency(self):
        for index, changes in enumerate([{'total_amount': 1}, {'currency': 'USD'}, {'payer_id': 421}]):
            with self.subTest(changes=changes):
                receipt = self.checkout({**self.payload, 'request_id': f'mismatch-{index}'})
                update = self.stars_update(receipt, f'charge-{index}')
                payer = changes.pop('payer_id', 420)
                update['message']['successful_payment'].update(changes)
                self.bot.handle_successful_payment(420, payer, update['message']['successful_payment'], queue_only=True)
                self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'review_required')
                self.assertEqual(self.db.get_order(receipt['order_ids'][0])['status'], 'awaiting_payment')

    def test_ledger_outbox_and_orders_rollback_in_outer_transaction(self):
        receipt = self.checkout()
        conn = self.db.connection()
        conn.execute('BEGIN IMMEDIATE')
        self.receive(receipt, 'stars', 'charge')
        self.assertTrue(conn.in_transaction)
        conn.execute('ROLLBACK')
        self.assertEqual(self.records('payment_receipts'), [])
        self.assertEqual(self.records('notifications'), [])
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'pending')

    def test_review_outbox_failure_rolls_back_receipt_and_can_retry(self):
        receipt = self.checkout()
        self.receive(receipt, 'stars', 'first')
        with self.assertRaises(RuntimeError):
            self.receive(receipt, 'stars', 'second', queue_review=Mock(side_effect=RuntimeError('disk')))
        self.assertEqual(len(self.records('payment_receipts')), 1)
        self.assertTrue(self.receive(receipt, 'stars', 'second'))
        self.assertEqual(len(self.records('payment_receipts')), 2)

    def test_parallel_distinct_receipts_settle_once(self):
        receipt = self.checkout()
        def pay(index):
            try:
                return self.receive(receipt, 'stars', f'charge-{index}')
            finally:
                self.db.close_current()
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertTrue(all(executor.map(pay, range(4))))
        self.assertEqual(len(self.records('payment_receipts')), 4)
        self.assertEqual(sum(row['status'] == 'applied' for row in self.records('payment_receipts')), 1)

    def test_poll_offset_waits_for_durable_payment_enqueue(self):
        receipt = self.checkout()
        update = self.stars_update(receipt)
        offsets = []
        stop = threading.Event()
        def get_updates(method, payload, **kwargs):
            offsets.append(payload['offset'])
            if len(offsets) == 3:
                stop.set()
                return []
            return [update]
        enqueue = self.db.enqueue_payment_update
        failures = [True]
        def temporarily_unavailable(event):
            if failures:
                failures.pop()
                raise sqlite3.OperationalError('disk unavailable')
            enqueue(event)
        with patch.object(self.api, 'call', side_effect=get_updates), patch.object(self.db, 'enqueue_payment_update', side_effect=temporarily_unavailable), patch('bot.STOP_EVENT', stop), patch.object(stop, 'wait'), patch.object(self.bot, 'handle_update') as dispatch:
            polling_loop(self.api, self.bot)
        self.assertEqual(offsets, [0, 0, 701])
        dispatch.assert_not_called()
        self.assertEqual(len(self.records('payment_inbox')), 1)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'pending')
        self.bot.flush_payment_updates()
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'paid')

    def test_payment_inbox_does_not_store_phone_or_profile_before_consent(self):
        self.db.upsert_user(self.user)
        self.assertFalse(self.db.has_consent(420))
        update = self.stars_update({'payment_id': 'unmatched', 'amount_stars': 1})
        update['message']['from']['phone'] = '+79995553311'
        update['message']['contact'] = {'phone_number': '+79995553311'}
        update['message']['successful_payment']['order_info'] = {'phone_number': '+79995553311', 'name': 'Private Name'}
        self.db.enqueue_payment_update(update)
        raw = self.records('payment_inbox')[0]['payload']
        for text in ('79995553311', 'order_info', 'Private Name', 'contact'):
            self.assertNotIn(text, raw)
        self.assertEqual(set(json.loads(raw)), {'invoice_payload', 'currency', 'total_amount', 'telegram_payment_charge_id'})

    def test_inbox_failure_after_ledger_work_rolls_back_and_recovers_after_restart(self):
        receipt = self.checkout()
        update = self.stars_update(receipt)
        self.db.enqueue_payment_update(update)
        real = self.bot.handle_successful_payment
        def crash(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('crash before processed_at')
        with patch.object(self.bot, 'handle_successful_payment', side_effect=crash):
            self.bot.flush_payment_updates()
        self.assertEqual(self.records('payment_receipts'), [])
        self.assertEqual(self.records('notifications'), [])
        self.assertIsNone(self.records('payment_inbox')[0]['processed_at'])
        self.db.close_current()
        restarted = Database(self.settings.database_path)
        self.addCleanup(restarted.close_current)
        bot = BrandBot(self.settings, self.api, restarted, self.catalog)
        bot.flush_payment_updates(now=time.time() + 4000)
        self.assertEqual(restarted.get_payment(receipt['payment_id'])['status'], 'paid')
        self.api.send_message.assert_not_called()  # notification network is independent
        self.assertEqual(restarted.connection().execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

    def test_receipt_committed_before_inbox_ack_is_replayed_without_duplicates(self):
        receipt = self.checkout()
        update = self.stars_update(receipt)
        self.bot.handle_successful_payment(420, 420, update['message']['successful_payment'], queue_only=True)
        self.db.enqueue_payment_update(update)
        self.db.enqueue_payment_update(update)
        self.bot.flush_payment_updates()
        self.assertEqual(len(self.records('payment_receipts')), 1)
        self.assertEqual(len(self.records('notifications')), 1)
        self.assertIsNotNone(self.records('payment_inbox')[0]['processed_at'])

    def test_creation_outcome_unknown_does_not_expose_or_reissue_invoice(self):
        self.bot.settings = replace(self.settings, crypto_pay_token='test-only')
        with patch('bot.create_crypto_invoice', return_value={'id': 'provider-created', 'url': 'https://pay.example/hidden'}) as create, patch.object(self.db, 'save_invoice', side_effect=RuntimeError('DB write failed')):
            receipt = self.checkout()
            self.assertFalse(any(method['id'] == 'crypto' for method in receipt['methods']))
            self.db.connection().execute('DELETE FROM payment_methods')
            self.bot.build_pay_methods(receipt['payment_id'], receipt['amount_rub'], 'retry')
            self.assertEqual(create.call_count, 1)
        self.assertEqual(self.records('payment_invoices'), [])
        self.assertEqual(self.records('payment_invoice_attempts')[0]['status'], 'uncertain')

    def test_crypto_invoice_identity_persists_and_reuses_after_method_cache_expiry(self):
        self.bot.settings = replace(self.settings, crypto_pay_token='test-only')
        now = time.time()
        with patch('bot.create_crypto_invoice', return_value={'id': 'issued', 'url': 'https://pay.example/issued'}) as create:
            receipt = self.checkout()
            self.assertEqual(self.db.get_invoice('crypto', 'issued')['amount_minor'], receipt['amount_rub'] * 100)
            with patch('time.time', return_value=now + 1900):
                self.bot.build_pay_methods(receipt['payment_id'], receipt['amount_rub'], 'retry')
            self.assertEqual(create.call_count, 1)

    def test_expired_crypto_invoice_is_reissued_with_separate_identity(self):
        self.bot.settings = replace(self.settings, crypto_pay_token='test-only')
        now = time.time()
        with patch('bot.create_crypto_invoice', side_effect=[{'id': 'old', 'url': 'https://pay.example/old'}, {'id': 'new', 'url': 'https://pay.example/new'}]):
            receipt = self.checkout()
            # An invoice can expire before a still-live stock reservation.
            # Do not lengthen a real reservation just to issue another invoice.
            self.db.connection().execute("UPDATE payment_invoices SET expires_at=?", (now + 100,))
            with patch('time.time', return_value=now + 1900):
                methods = self.bot.build_pay_methods(receipt['payment_id'], receipt['amount_rub'], 'retry')
        self.assertEqual(len(self.records('payment_invoices')), 2)
        self.assertTrue(any(method['url'].endswith('/new') for method in methods))
        self.receive(receipt, 'crypto', 'old')
        self.receive(receipt, 'crypto', 'new')
        self.assertEqual([row['status'] for row in self.records('payment_receipts')], ['applied', 'refund_required'])

    def test_migration_is_additive_idempotent_and_never_trusts_old_invoice_urls(self):
        receipt = self.checkout()
        self.db.connection().execute("UPDATE payments SET status='paid', method='stars', provider_id='historical' WHERE payment_id=?", (receipt['payment_id'],))
        pending = self.checkout({**self.payload, 'request_id': 'legacy-pending'})
        self.db.connection().execute('UPDATE payment_methods SET methods=? WHERE payment_id=?',
            (json.dumps([{'id': 'lava', 'url': 'https://pay.example/legacy'}]), pending['payment_id']))
        for table in ('payment_receipt_conflicts', 'payment_receipts', 'payment_invoices', 'payment_invoice_attempts', 'payment_inbox'):
            self.db.connection().execute(f'DROP TABLE {table}')
        self.db.close_current()
        for _ in range(2):
            db = Database(self.settings.database_path)
            try:
                rows = list(db.connection().execute('SELECT * FROM payment_receipts'))
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['source'], rows[0]['status']), ('legacy', 'legacy_unreconciled'))
                self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
                self.assertIsNone(db.reserve_invoice(pending['payment_id'], 'lava', pending['amount_rub'] * 100))
            finally:
                db.close_current()

    def serve(self):
        settings = replace(self.settings, lava_shop_id='shop', lava_secret_key='secret', lava_hook_key='hook', crypto_pay_token='token')
        server = start_health_server(0, self.catalog, settings, self.db, self.api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def webhook(self, origin, method, body, signature=None):
        raw = json.dumps(body, ensure_ascii=False).encode()
        key = b'hook' if method == 'lava' else hashlib.sha256(b'token').digest()
        sign = signature or hmac.new(key, raw, hashlib.sha256).hexdigest()
        header = 'Signature' if method == 'lava' else 'Crypto-Pay-API-Signature'
        request = urllib.request.Request(f'{origin}/api/payments/{method}', data=raw,
                                        headers={header: sign, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_http_lava_valid_and_invalid_signed_receipts(self):
        origin = self.serve()
        for index, (identity, amount, currency, expected) in enumerate([
            ('good', '4900.00', 'RUB', 'paid'), ('under', '1.00', 'RUB', 'review_required'),
            ('foreign', '4900.00', 'USD', 'review_required'), ('missing-currency', '4900.00', None, 'paid'),
            ('unknown', '4900.00', 'RUB', 'review_required')]):
            with self.subTest(identity=identity):
                receipt = self.checkout({**self.payload, 'request_id': f'lava-{index}'})
                if identity != 'unknown':
                    self.issue(receipt, 'lava', identity)
                body = {'invoice_id': identity, 'order_id': receipt['payment_id'], 'status': 'success', 'amount': amount}
                if currency is not None:
                    body['currency'] = currency
                self.assertEqual(self.webhook(origin, 'lava', body)[0], 200)
                self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], expected)
                self.assertEqual(self.webhook(origin, 'lava', body)[0], 200)
        self.assertEqual(len(self.records('payment_receipts')), 5)

    def test_http_crypto_requires_paid_event_status_fiat_and_invoice(self):
        origin = self.serve()
        for index, (change, expected) in enumerate([
            ({}, 'paid'), ({'update_type': None}, 'pending'), ({'status': 'active'}, 'pending'),
            ({'amount': '1'}, 'review_required'), ({'fiat': 'USD'}, 'review_required'),
            ({'currency_type': 'crypto', 'asset': 'USDT'}, 'review_required')]):
            with self.subTest(change=change):
                receipt = self.checkout({**self.payload, 'request_id': f'crypto-{index}'})
                identity = f'invoice-{index}'
                self.issue(receipt, 'crypto', identity)
                inner = {'invoice_id': identity, 'payload': receipt['payment_id'], 'status': 'paid',
                         'currency_type': 'fiat', 'fiat': 'RUB', 'amount': '4900.00'}
                inner.update({key: value for key, value in change.items() if key != 'update_type'})
                body = {'update_type': change.get('update_type', 'invoice_paid'), 'payload': inner}
                self.assertEqual(self.webhook(origin, 'crypto', body)[0], 200)
                self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], expected)

    def test_http_bad_signature_never_records_money_and_db_failure_requests_retry(self):
        origin = self.serve()
        receipt = self.checkout()
        self.issue(receipt, 'lava', 'issued')
        body = {'invoice_id': 'issued', 'order_id': receipt['payment_id'], 'status': 'success', 'amount': '4900'}
        self.assertEqual(self.webhook(origin, 'lava', body, 'bad-signature')[0], 403)
        with patch.object(self.db, 'mark_payment_paid', side_effect=sqlite3.OperationalError('disk')):
            self.assertEqual(self.webhook(origin, 'lava', body)[0], 503)
        self.assertEqual(self.records('payment_receipts'), [])
        self.assertEqual(self.webhook(origin, 'lava', body)[0], 200)

    def test_phone_is_absent_from_all_tables_until_consent_then_must_be_resent(self):
        self.db.upsert_user(self.user)
        self.db.set_state(420, 'awaiting_profile_phone', {})
        self.bot.save_phone_and_continue(420, 420, '+79995553311')
        self.assertNotIn('79995553311', '\n'.join(self.db.connection().iterdump()))
        self.bot.accept_consent(420, 420)
        self.assertIsNone(self.db.get_user(420)['phone'])
        self.bot.save_phone_and_continue(420, 420, '+79995553311')
        self.assertEqual(self.db.get_user(420)['phone'], '+79995553311')

    def test_direct_profile_and_phone_writes_require_consent(self):
        self.db.upsert_user(self.user)
        with self.assertRaises(ValueError):
            self.db.set_phone(420, '+79995553311')
        with self.assertRaises(ValueError):
            self.db.set_profile(420, {'phone': '+79995553311', 'address': 'Private'})
        for consent in (False, 'true', None):
            self.bot.handle_web_profile(420, self.user, {'consent': consent, 'profile': {'phone': '+79995553311', 'address': 'Private'}})
        self.assertNotIn('79995553311', '\n'.join(self.db.connection().iterdump()))
        self.assertIsNone(self.db.get_user(420)['address'])

    def test_legacy_preconsent_state_phone_is_scrubbed_on_upgrade(self):
        self.db.upsert_user(self.user)
        self.db.set_state(420, 'awaiting_consent', {'next_state': 'awaiting_order_phone',
            'next_data': {'phone': '+79995553311', 'product_id': 'tee-sila-i-chest', 'size': 'M'}})
        self.db.close_current()
        db = Database(self.settings.database_path)
        self.addCleanup(db.close_current)
        state = db.get_state(420)
        self.assertNotIn('phone', state[1]['next_data'])
        self.assertEqual(state[1]['next_data']['size'], 'M')

    def temp_catalog(self):
        path = Path(self.temp.name) / 'catalog.json'
        path.write_text(Path(self.settings.catalog_path).read_text())
        return Catalog(path)

    def test_invalid_real_callback_never_changes_catalog_memory_or_file(self):
        catalog = self.temp_catalog()
        before = catalog.path.read_bytes()
        memory = copy.deepcopy(catalog.data)
        product = copy.deepcopy(catalog.data['products'][0])
        product.update(name='x' * 32, sizes=['y' * 27])
        with self.assertRaises(ValueError):
            catalog.add_product(product)
        self.assertEqual(catalog.path.read_bytes(), before)
        self.assertEqual(catalog.data, memory)
        self.assertEqual(Catalog(catalog.path).data, memory)

    def test_wsize_utf8_limit_is_checked_not_only_short_size_callback(self):
        catalog = self.temp_catalog()
        product = copy.deepcopy(catalog.data['products'][0])
        product.update(name='x' * 32, sizes=['y' * 26])  # size=64 bytes, wsize=65
        with self.assertRaises(ValueError):
            catalog.add_product(product)
        product.update(name='x' * 32, sizes=['Ж' * 13])
        with self.assertRaises(ValueError):
            catalog.add_product(product)

    def test_catalog_atomic_replace_failure_keeps_old_view_and_cleans_temp(self):
        catalog = self.temp_catalog()
        before = catalog.path.read_bytes()
        with patch('bot.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                catalog.set_active('tee-sila-i-chest', False)
        self.assertTrue(catalog.get('tee-sila-i-chest')['active'])
        self.assertEqual(catalog.path.read_bytes(), before)
        self.assertEqual(list(catalog.path.parent.glob('catalog.json.*.tmp')), [])

    def test_catalog_concurrent_additions_remain_valid_and_unique(self):
        catalog = self.temp_catalog()
        product = copy.deepcopy(catalog.data['products'][0])
        before_count = len(catalog.data['products'])
        product['name'] = 'a' * 40
        with ThreadPoolExecutor(max_workers=4) as executor:
            added = list(executor.map(lambda _: catalog.add_product(product), range(12)))
        self.assertEqual(len({row['id'] for row in added}), 12)
        self.assertEqual(len(Catalog(catalog.path).data['products']), before_count + 12)
        self.assertTrue(all(len(f"wsize:{row['id']}:XXL".encode()) <= 64 for row in added))

    def test_existing_legacy_phone_is_not_reused_without_consent(self):
        self.db.upsert_user(self.user)
        self.db.connection().execute("UPDATE users SET phone='+79995553311' WHERE user_id=420")
        self.bot.select_size(420, 420, 'tee-sila-i-chest', 'M', 'legacy-phone')
        self.assertEqual(self.records('orders'), [])
        self.assertEqual(len(self.db.cart(420)['items']), 1)
        self.assertFalse(self.db.has_consent(420))
        self.assertIsNone(self.db.get_state(420))
        # Selecting a size no longer starts a one-line purchase or asks a phone.
        self.bot.commerce.checkout(420, 420)
        self.assertEqual(self.db.get_state(420)[0], 'commerce_consent')
        self.assertNotIn('phone', self.db.get_state(420)[1])

    def test_admin_wizard_rejects_actual_long_callback_and_publish_is_safe(self):
        self.bot.catalog = catalog = self.temp_catalog()
        self.db.upsert_user(self.user)
        data = {'step': 'sizes', 'name': 'x' * 32, 'price': '1 ₽', 'category': catalog.categories[0]['id']}
        self.db.set_state(420, 'admin_add', data)
        self.assertTrue(self.bot.handle_add_product_text(420, 420, 'y' * 27))
        self.assertEqual(self.db.get_state(420)[1]['step'], 'sizes')
        before = catalog.path.read_bytes()
        product = {**catalog.data['products'][0], 'name': 'x' * 32, 'sizes': ['y' * 27]}
        self.db.set_state(420, 'admin_add', {'step': 'preview', 'preview': product})
        self.bot.publish_product(420, 420)
        self.assertEqual(catalog.path.read_bytes(), before)
        self.assertIn('не опубликован', self.api.send_message.call_args.args[1])

    def test_new_charge_after_legacy_settlement_requires_review_not_assumed_refund(self):
        receipt = self.checkout()
        self.db.connection().execute("UPDATE payments SET status='paid', method='stars', provider_id='old-truncated-id' WHERE payment_id=?", (receipt['payment_id'],))
        self.db.init_payment_store()
        self.assertTrue(self.receive(receipt, 'stars', 'old-truncated-id-with-full-suffix'))
        row = self.records('payment_receipts')[-1]
        self.assertEqual((row['status'], row['reason']), ('review_required', 'legacy_settlement'))
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['provider_id'], 'old-truncated-id')

    def test_duplicate_legacy_charge_associations_survive_as_migration_conflicts(self):
        first = self.checkout()
        second = self.checkout({**self.payload, 'request_id': 'legacy-other'})
        self.db.connection().execute("UPDATE payments SET status='paid', method='stars', provider_id='old-collision'")
        self.db.init_payment_store()
        self.db.init_payment_store()
        self.assertEqual(len(self.records('payment_receipts')), 1)
        self.assertEqual(len(self.records('payment_receipt_conflicts')), 1)
        self.assertEqual(self.records('notifications'), [])
        self.assertEqual(len(self.records('payments')), 2)

    def test_payment_inbox_cannot_acknowledge_an_uncommitted_outer_transaction(self):
        receipt = self.checkout()
        self.db.connection().execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(RuntimeError):
                self.db.enqueue_payment_update(self.stars_update(receipt))
        finally:
            self.db.connection().execute('ROLLBACK')
        self.assertEqual(self.records('payment_inbox'), [])
        self.assertEqual(self.db.connection().execute('PRAGMA synchronous').fetchone()[0], 2)

    def test_api_price_number_and_label_follow_checkout_rounding(self):
        self.catalog.get('tee-sila-i-chest')['price'] = '4 900,50 ₽'
        product = next(row for row in self.catalog.public_products() if row['id'] == 'tee-sila-i-chest')
        self.assertEqual((product['price_rub'], product['price']), (4900, '4 900 ₽'))
        self.assertEqual(self.checkout()['amount_rub'], product['price_rub'])


class AdapterFixTests(unittest.TestCase):
    def test_lava_signature_matches_exact_transmitted_utf8_bytes(self):
        response = Mock()
        response.read.return_value = json.dumps({'data': {'id': 'issued', 'url': 'https://pay.example/issued'}}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('payments.urllib.request.urlopen', return_value=response) as send:
            payments.create_lava_invoice('shop', 'outgoing-key', 'unique-ref', 4900, 'Футболка «Сила»')
        wire = send.call_args.args[0]
        self.assertEqual(wire.get_header('Signature'), hmac.new(b'outgoing-key', wire.data, hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(wire.data)['comment'], 'Футболка «Сила»')
        self.assertEqual(json.loads(wire.data)['sum'], 4900)

    def test_minor_units_are_exact_and_reject_nonfinite_or_fractional_stars(self):
        self.assertEqual(payments.minor_units('4900.50', 'RUB'), 490050)
        self.assertEqual(payments.minor_units('2450', 'XTR'), 2450)
        for value, currency in [('NaN', 'RUB'), ('Infinity', 'RUB'), ('0.001', 'RUB'), ('1.5', 'XTR'), (True, 'XTR'), (None, 'RUB'), ('-1', 'RUB')]:
            with self.subTest(value=value, currency=currency), self.assertRaises(ValueError):
                payments.minor_units(value, currency)

    def test_adapters_reject_invoice_without_identity(self):
        with patch('payments._http_json', return_value={'data': {'url': 'https://pay.example/lava'}}):
            with self.assertRaises(RuntimeError):
                payments.create_lava_invoice('shop', 'secret', 'ref', 4900, 'test')
        with patch('payments._http_json', return_value={'result': {'bot_invoice_url': 'https://pay.example/crypto'}}):
            with self.assertRaises(RuntimeError):
                payments.create_crypto_invoice('token', 4900, 'test', 'ref')


if __name__ == '__main__':
    unittest.main()
