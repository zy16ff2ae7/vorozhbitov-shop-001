"""Offline acceptance of the native trading core, transactional stock and ACL."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from bot import BrandBot, Catalog, Database, start_health_server
from commerce_bot import CommerceBot
from commerce_store import CartConflict, StockError
import test_regressions as regressions


class CommerceTests(unittest.TestCase):
    checkout = regressions.CheckoutRegressionTests.checkout
    confirm_stars = regressions.CheckoutRegressionTests.confirm_stars

    def setUp(self):
        regressions.CheckoutRegressionTests.setUp(self)
        path = Path(self.temp.name) / 'commerce-catalog.json'
        shutil.copy(self.settings.catalog_path, path)
        self.settings = replace(self.settings, catalog_path=path, admin_ids=frozenset({9001}))
        self.catalog = Catalog(path)
        self.bot = BrandBot(self.settings, self.api, self.db, self.catalog)
        self.db.upsert_user(self.user)
        for uid in (421, 9001, 9002, 9003):
            self.db.upsert_user({'id': uid, 'first_name': f'User{uid}'})
        self.api.send_message.return_value = {'message_id': 18}
        self.api.edit_message_text.return_value = {'message_id': 18}

    def records(self, table):
        return list(self.db.connection().execute('SELECT * FROM ' + table))

    def set_count(self, quantity, pid='tee-sila-i-chest', size='M'):
        sku = self.db.stock_item(pid, size)
        self.db.set_stock(9001, sku['sku_id'], quantity, sku['version'], owner_ids=self.settings.admin_ids)
        return self.db.stock_item(pid, size)

    def cart(self, uid=420, items=None, revision=0, operation='op-1'):
        return self.db.replace_cart(uid, items if items is not None else self.payload['items'], revision, operation, self.catalog)

    def profile(self, uid=420):
        self.db.set_consent(uid, service_only=True)
        self.db.set_profile(uid, {'name': 'Получатель', 'phone': '+79991234567', 'city': 'Москва', 'address': 'Тестовый ПВЗ', 'deliver': 'СДЭК'})

    def callback(self, data, uid=420, private=True):
        self.assertTrue(self.bot.handle_update({'callback_query': {'id': 'test-callback', 'from': {'id': uid}, 'data': data,
            'message': {'chat': {'id': uid if private else -100, 'type': 'private' if private else 'group'}}}}))

    def message(self, text='', uid=420, contact=None):
        self.assertTrue(self.bot.handle_update({'message': {'from': {'id': uid, 'first_name': 'Telegram name'},
            'chat': {'id': uid, 'type': 'private'}, 'text': text, **({'contact': contact} if contact else {})}}))

    def panel(self):
        calls = self.api.mock_calls
        found = [x for x in calls if x[0] in {'send_message', 'edit_message_text'}]
        call = found[-1]
        return call.args[-2], call.args[-1]

    def press(self, label, uid=420):
        _, markup = self.panel()
        found = [b['callback_data'] for row in markup['inline_keyboard'] for b in row if label in b['text']]
        self.assertTrue(found, (label, markup))
        self.callback(found[0], uid)
        return found[0]

    def test_unknown_inventory_never_comes_from_catalog_labels(self):
        clean = Database(Path(self.temp.name) / 'empty.sqlite')
        self.addCleanup(clean.close_current)
        clean.sync_inventory(self.catalog)
        self.assertTrue(all(r['on_hand'] is None for r in clean.connection().execute('SELECT * FROM stock_items')))
        bot = BrandBot(self.settings, self.api, clean, self.catalog)
        result = bot.checkout_web_payload(self.user, self.payload)
        self.assertFalse(result['ok'])
        self.assertEqual(clean.stats()['orders'], 0)
        self.assertEqual(clean.connection().execute('SELECT COUNT(*) FROM payments').fetchone()[0], 0)
        self.api.create_invoice_link.assert_not_called()
        bot.finish_order(420, 420, self.catalog.get('tee-sila-i-chest'), 'M', '+79990000000', 'old-phone-wizard')
        self.assertIn('не подтвержден', self.api.send_message.call_args.args[1])
        self.assertEqual(clean.stats()['orders'], 0)

    def test_last_unit_is_reserved_by_exactly_one_concurrent_customer(self):
        self.set_count(1)
        barrier = threading.Barrier(2)
        def buy(uid):
            barrier.wait()
            try:
                return self.checkout(user={'id': uid})
            finally:
                self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, (420, 421)))
        self.assertEqual(sum(x['ok'] for x in results), 1)
        self.assertEqual(len(self.records('purchases')), 1)
        self.assertEqual(len(self.records('stock_reservations')), 1)
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 1)

    def test_personalization_lines_share_one_physical_sku(self):
        self.set_count(2, 'tag-sila-i-chest', 'ONE SIZE')
        body = {**self.payload, 'items': [{'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 2, 'person': str(x)} for x in (1, 2)]}
        self.assertFalse(self.checkout(body)['ok'])
        self.assertEqual(self.records('orders'), [])
        self.assertEqual(self.records('stock_reservations'), [])
        self.assertEqual(self.db.stock_item('tag-sila-i-chest', 'ONE SIZE')['reserved'], 0)

    def test_failed_checkout_rolls_back_earlier_sku_reservations_and_cart_clear(self):
        self.set_count(1, 'tag-sila-i-chest', 'ONE SIZE')
        self.set_count(0)
        items = [{'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1}, *self.payload['items']]
        cart = self.cart(items=items)
        result = self.checkout({**self.payload, 'items': items, 'cart_revision': cart['revision']})
        self.assertFalse(result['ok'])
        self.assertEqual(self.db.cart(420), cart)
        self.assertEqual(self.records('stock_reservations'), [])
        self.assertEqual([x for x in self.records('stock_movements') if x['kind'] == 'hold'], [])

    def test_paid_receipt_consumes_exactly_once_even_after_restart(self):
        self.set_count(2)
        receipt = self.checkout()
        self.confirm_stars(receipt, 'unique-charge')
        self.confirm_stars(receipt, 'unique-charge')
        db2 = Database(self.db.path)
        self.addCleanup(db2.close_current)
        self.assertFalse(db2.mark_payment_paid(receipt['payment_id'], 'stars', 'unique-charge', amount_minor=receipt['amount_stars'], currency='XTR', payer_id=420))
        sku = db2.stock_item('tee-sila-i-chest', 'M')
        self.assertEqual((sku['on_hand'], sku['reserved']), (1, 0))
        self.assertEqual(self.records('stock_reservations')[0]['state'], 'consumed')
        self.assertEqual(len([r for r in self.records('stock_movements') if r['kind'] == 'consume']), 1)

    def test_cancel_unpaid_releases_entire_purchase_and_is_idempotent(self):
        items = [*self.payload['items'], {'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 2}]
        receipt = self.checkout({**self.payload, 'items': items})
        self.db.set_order_status(receipt['order_ids'][0], 'cancelled', customer_id=420)
        self.db.set_order_status(receipt['order_ids'][0], 'cancelled', customer_id=420)
        self.assertEqual({r['status'] for r in self.records('orders')}, {'cancelled'})
        self.assertEqual({r['state'] for r in self.records('stock_reservations')}, {'released'})
        self.assertTrue(all(r['reserved'] == 0 for r in self.records('stock_items')))
        self.assertEqual(self.db.purchase_view(receipt['purchase_id'], user_id=420)['fulfillment'], 'cancelled')

    def test_paid_cancellation_never_invents_physical_returns(self):
        self.set_count(1)
        receipt = self.checkout()
        self.confirm_stars(receipt, 'paid-before-cancel')
        self.db.set_order_status(receipt['order_ids'][0], 'cancelled')
        sku = self.db.stock_item('tee-sila-i-chest', 'M')
        self.assertEqual((sku['on_hand'], sku['reserved']), (0, 0))
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'refund_required')

    def test_expiration_is_durable_and_notifies_once(self):
        self.set_count(1)
        receipt = self.checkout()
        self.assertEqual(self.db.expire_reservations(receipt['reserved_until'] + 1), 1)
        self.assertEqual(self.db.expire_reservations(receipt['reserved_until'] + 2), 0)
        self.assertFalse(self.db.payment_is_payable(receipt['payment_id']))
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'expired')
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 0)
        self.assertEqual(len(self.records('notifications')), 1)
        again = self.checkout()
        self.assertEqual(receipt['payment_id'], again['payment_id'])
        self.assertEqual(again['methods'], [])

    def test_late_payment_does_not_steal_last_unit_from_new_reservation(self):
        self.set_count(1)
        old = self.checkout()
        self.db.expire_reservations(old['reserved_until'] + 1)
        new = self.checkout(user={'id': 421})
        self.assertTrue(new['ok'])
        self.confirm_stars(old, 'late-charge')
        self.assertEqual(self.db.get_payment(old['payment_id'])['status'], 'refund_required')
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 1)
        self.assertTrue(self.db.payment_is_payable(new['payment_id']))
        self.assertFalse(any(r['status'] == 'paid' for r in self.db.orders_for_payment(old['payment_id'])))

    def test_late_settlement_before_expiry_worker_also_releases_stock(self):
        self.set_count(1)
        receipt = self.checkout()
        with patch('time.time', return_value=receipt['reserved_until'] + 1):
            self.confirm_stars(receipt, 'late-before-worker')
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 0)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'refund_required')

    def test_no_invoice_reissue_after_reservation_expires(self):
        receipt = self.checkout()
        calls = self.api.create_invoice_link.call_count
        with patch('time.time', return_value=receipt['reserved_until'] + 1):
            self.assertEqual(self.bot.build_pay_methods(receipt['payment_id'], receipt['amount_rub'], 'expired'), [])
        self.assertEqual(self.api.create_invoice_link.call_count, calls)

    def test_review_money_survives_reservation_expiration(self):
        receipt = self.checkout()
        self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'wrong-amount', amount_minor=1, currency='XTR', payer_id=420)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'review_required')
        self.db.expire_reservations(receipt['reserved_until'] + 1)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'review_required')
        self.assertEqual(self.records('payment_receipts')[0]['status'], 'review_required')
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 0)

    def test_expiry_queue_failure_rolls_back_counters_and_statuses(self):
        receipt = self.checkout()
        with patch.object(self.db, 'enqueue_message', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.expire_reservations(receipt['reserved_until'] + 1)
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'pending')
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['reserved'], 1)

    def test_cart_cas_idempotence_and_owner_isolation(self):
        first = self.cart()
        same = self.cart()
        self.assertEqual(first, same)
        with self.assertRaises(ValueError):
            self.cart(items=[])
        with self.assertRaises(CartConflict):
            self.cart(items=[], operation='different-op')
        other = self.cart(421, items=[])
        self.assertEqual(other['items'], [])
        self.assertEqual(self.db.cart(420), first)
        with self.assertRaises(ValueError):
            self.cart(items=[{**self.payload['items'][0], 'quantity': True}], revision=first['revision'], operation='bad-quantity')

    def test_checkout_clears_only_matched_version_and_repeated_receipt_is_stable(self):
        cart = self.cart()
        receipt = self.checkout({**self.payload, 'cart_revision': cart['revision']})
        self.assertTrue(receipt['ok'])
        self.assertEqual(self.db.cart(420)['items'], [])
        next_cart = self.cart(items=self.payload['items'], revision=self.db.cart(420)['revision'], operation='next-intent')
        retry = self.checkout({**self.payload, 'cart_revision': cart['revision']})
        self.assertEqual(receipt['payment_id'], retry['payment_id'])
        self.assertEqual(self.db.cart(420), next_cart)

    def test_two_checkouts_of_same_cart_version_cannot_create_two_purchases(self):
        cart = self.cart()
        first = self.checkout({**self.payload, 'cart_revision': cart['revision']})
        second = self.checkout({**self.payload, 'request_id': 'other-tab', 'cart_revision': cart['revision']})
        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertEqual(second['code'], 'cart_revision_conflict')
        self.assertEqual(len(self.records('purchases')), 1)

    def test_checkout_failure_after_hold_rolls_back_everything(self):
        cart = self.cart()
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('outbox failed')):
            with self.assertRaises(RuntimeError):
                self.checkout({**self.payload, 'cart_revision': cart['revision']}, notify=420)
        for table in ('payments', 'purchases', 'stock_reservations', 'orders', 'checkouts'):
            self.assertEqual(self.records(table), [], table)
        self.assertEqual(self.db.cart(420), cart)

    def test_inactive_lines_can_be_removed_individually(self):
        items = [*self.payload['items'], {'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 1}]
        cart = self.cart(items=items)
        self.catalog.set_active('tee-sila-i-chest', False)
        self.catalog.set_active('tag-sila-i-chest', False)
        next_cart = self.cart(items=items[:1], revision=cart['revision'], operation='remove-one')
        self.assertEqual(len(next_cart['items']), 1)
        with self.assertRaises(ValueError):
            self.cart(421, items=items, operation='add-hidden')

    def test_chat_action_tokens_are_short_owned_and_expire(self):
        action = self.db.create_chat_action(420, 'thing', {'product': 'я' * 40})
        self.assertLessEqual(len(action.encode()), 64)
        token = action.split(':')[-1]
        with self.assertRaises(ValueError): self.db.chat_action(421, token)
        with patch('time.time', return_value=time.time() + 2000):
            with self.assertRaises(ValueError): self.db.chat_action(420, token)

    def test_native_full_journey_edits_panel_and_commits_one_purchase(self):
        self.message('/menu')
        self.press('Смотреть выпуск')
        self.press('СИЛА И ЧЕСТЬ')
        self.press('Выбрать размер')
        add = self.press('M')
        self.callback(add)  # Same old button, a different Telegram callback event.
        self.assertEqual(self.db.cart(420)['items'][0]['quantity'], 1)
        self.press('Оформить покупку')
        self.message('+79990000000')  # unsolicited before consent, never stored
        self.assertIsNone(self.db.get_user(420)['phone'])
        self.assertNotIn('+79990000000', json.dumps([dict(x) for x in self.records('states')]))
        self.press('Согласен')
        for value in ('Получатель', '+79991234567', 'Москва', 'Тестовый ПВЗ'):
            self.message(value)
        self.press('СДЭК')
        confirm = self.press('Всё верно')
        self.callback(confirm)
        self.assertEqual(len(self.records('purchases')), 1)
        self.assertEqual(len(self.records('orders')), 1)
        self.assertEqual(self.db.cart(420)['items'], [])
        self.assertEqual(self.db.account_profile(420)['name'], 'Получатель')
        self.assertGreater(self.api.edit_message_text.call_count, 4)
        self.assertIn('ПОКУПКА №0001', self.panel()[0])

    def test_native_resume_survives_restart_without_storing_contact_in_state(self):
        self.cart()
        self.profile()
        self.bot.commerce.ask_field(420, 420, 'address', 'checkout')
        self.message('/menu')
        state = self.db.get_state(420)
        self.assertTrue(state[1]['paused'])
        restarted = Database(self.db.path)
        self.addCleanup(restarted.close_current)
        bot = BrandBot(self.settings, self.api, restarted, self.catalog)
        bot.handle_message({'from': self.user, 'chat': {'id': 420, 'type': 'private'}, 'text': '/resume'})
        self.assertEqual(restarted.get_state(420)[0], 'commerce_field')
        self.assertFalse(restarted.get_state(420)[1].get('paused'))
        self.assertNotIn('phone', restarted.get_state(420)[1])

    def test_native_quote_rejects_price_change_or_changed_cart(self):
        self.profile()
        self.cart()
        self.bot.commerce.checkout(420, 420)
        token = self.db.get_state(420)[1]['token']
        self.catalog.get('tee-sila-i-chest')['price'] = '5 900 ₽'
        self.callback('c:confirm:' + token)
        self.assertEqual(self.records('purchases'), [])
        self.assertIn('Цена изменилась', self.panel()[0])
        self.catalog.get('tee-sila-i-chest')['price'] = '4 900 ₽'
        self.cart(items=[], revision=1, operation='clear-after-preview')
        self.callback('c:confirm:' + token)
        self.assertEqual(self.records('purchases'), [])

    def test_quote_owner_and_expiration_are_enforced(self):
        self.profile()
        self.cart()
        self.bot.commerce.checkout(420, 420)
        token = self.db.get_state(420)[1]['token']
        with self.assertRaises(ValueError): self.db.checkout_draft(421, token)
        with patch('time.time', return_value=time.time() + 1000):
            self.callback('c:confirm:' + token)
        self.assertEqual(self.records('purchases'), [])

    def test_consumed_quote_can_be_recovered_after_preview_expiration(self):
        self.profile()
        self.cart()
        self.bot.commerce.checkout(420, 420)
        token = self.db.get_state(420)[1]['token']
        self.callback('c:confirm:' + token)
        with patch('time.time', return_value=time.time() + 1000):
            self.callback('c:confirm:' + token)
        self.assertEqual(len(self.records('purchases')), 1)

    def test_purchase_is_aggregate_with_immutable_customer_snapshot(self):
        body = copy.deepcopy(self.payload)
        body['customer']['address'] = 'Old address'
        body['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 2})
        receipt = self.checkout(body)
        self.db.set_profile(420, {'phone': '+79998887766', 'address': 'New address'})
        purchase = self.db.purchase_view(receipt['purchase_id'], user_id=420)
        self.assertEqual(len(purchase['lines']), 2)
        self.assertEqual(purchase['quantity'], 3)
        self.assertEqual(purchase['customer']['address'], 'Old address')
        with self.assertRaises(ValueError): self.db.purchase_view(receipt['purchase_id'], user_id=421)
        with self.assertRaises(PermissionError): self.db.purchase_view(receipt['purchase_id'])

    def test_roles_enforced_beyond_hidden_buttons_and_revoked_actions(self):
        sku = self.db.stock_item('tee-sila-i-chest', 'M')
        with self.assertRaises(PermissionError): self.db.set_stock(420, sku['sku_id'], 1, sku['version'], owner_ids=self.settings.admin_ids)
        self.db.set_staff_role(9001, 9002, 'warehouse', owner_ids=self.settings.admin_ids)
        self.assertEqual(self.db.staff_role(9002), 'warehouse')
        action = self.db.create_chat_action(9002, 'count', {'sku_id': sku['sku_id'], 'quantity': 1, 'version': sku['version']})
        self.db.set_staff_role(9001, 9002, 'none', owner_ids=self.settings.admin_ids)
        self.callback(action, 9002)
        self.assertIn('Нет прав', self.panel()[0])
        self.assertEqual(self.db.stock_item('tee-sila-i-chest', 'M')['on_hand'], 1000)
        with self.assertRaises(ValueError): self.db.set_staff_role(9001, 9001, 'none', owner_ids=self.settings.admin_ids)

    def test_notification_chat_does_not_become_owner(self):
        self.bot.settings = replace(self.settings, manager_chat_id=421)
        self.assertEqual(self.bot.commerce.role(421), '')
        self.checkout()
        body = self.records('notifications')[0]['body']
        self.assertNotIn('+79990000000', body)
        self.assertNotIn('Test', body)
        self.callback('c:team', 421)
        self.assertIn('Нет прав', self.panel()[0])

    def test_inventory_version_prevents_count_overwriting_new_reservation(self):
        sku = self.set_count(2)
        self.checkout()
        with self.assertRaises(CartConflict): self.db.set_stock(9001, sku['sku_id'], 1, sku['version'], owner_ids=self.settings.admin_ids)
        current = self.db.stock_by_id(sku['sku_id'])
        with self.assertRaises(ValueError): self.db.set_stock(9001, sku['sku_id'], 0, current['version'], owner_ids=self.settings.admin_ids)

    def test_staff_workflow_updates_whole_purchase_and_cannot_bypass_money(self):
        self.db.set_staff_role(9001, 9002, 'manager', owner_ids=self.settings.admin_ids)
        receipt = self.checkout()
        pid = receipt['purchase_id']
        self.db.claim_purchase(9002, pid)
        view = self.db.purchase_view(pid, actor_id=9002)
        with self.assertRaises(ValueError): self.db.advance_purchase(9002, pid, 'packing', view['version'])
        self.confirm_stars(receipt, 'stage-payment')
        for target in ('packing', 'ready'):
            view = self.db.purchase_view(pid, actor_id=9002)
            self.db.advance_purchase(9002, pid, target, view['version'])
            self.db.advance_purchase(9002, pid, target, view['version'])
        with self.assertRaises(ValueError):
            self.db.advance_purchase(9002, pid, 'completed', self.db.purchase_view(pid, user_id=420)['version'])
        terms = {'carrier': 'pickup', 'destination': 'Тестовая точка выдачи', 'amount_minor': 0, 'billing': 'shop',
                 'eta': 'После подготовки', 'basis': 'Явный бесплатный тестовый самовывоз'}
        quote = self.db.propose_delivery(9002, pid, terms, 0, 'workflow-delivery')
        self.db.answer_delivery(420, pid, quote, 1, True, 'workflow-accept')
        self.db.dispatch_delivery(9002, pid, 2, '', 'workflow-dispatch')
        self.db.mark_received(420, pid, 3, 'workflow-received')
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['fulfillment'], 'completed')
        self.assertEqual(len([x for x in self.records('staff_audit') if x['action'] == 'purchase.stage']), 2)
        self.assertEqual(self.db.delivery_view(420, pid)['delivered_source'], 'customer')
        self.assertEqual({x['status'] for x in self.records('orders')}, {'completed'})

    def test_two_managers_cannot_claim_same_purchase(self):
        for uid in (9002, 9003): self.db.set_staff_role(9001, uid, 'manager', owner_ids=self.settings.admin_ids)
        pid = self.checkout()['purchase_id']
        def claim(uid):
            try:
                self.db.claim_purchase(uid, pid)
                return True
            except ValueError: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, (9002, 9003))), 1)

    def test_warehouse_and_support_cannot_read_contact_snapshot(self):
        receipt = self.checkout()
        for role in ('support', 'warehouse'):
            self.db.set_staff_role(9001, 9002, role, owner_ids=self.settings.admin_ids)
            self.assertNotIn('customer', self.db.purchase_view(receipt['purchase_id'], actor_id=9002))
        self.db.set_staff_role(9001, 9002, 'support', owner_ids=self.settings.admin_ids)
        with self.assertRaises(PermissionError): self.db.claim_purchase(9002, receipt['purchase_id'])

    def test_private_chat_enforced_for_staff_cards_and_role_grants(self):
        self.callback('c:stock:0', 9001, private=False)
        self.assertFalse(self.api.send_message.called)
        self.assertFalse(self.api.edit_message_text.called)

    def test_service_consent_does_not_join_any_marketing_segment(self):
        self.checkout()
        for segment in ('all', 'consent', 'contacts', 'buyers', 'fresh', 'interest:drop'):
            self.assertNotIn(420, self.db.broadcast_audience(segment))
        self.db.set_consent(420)  # another path cannot silently broaden consent
        self.assertNotIn(420, self.db.broadcast_audience('all'))

    def test_legacy_migration_preserves_amounts_and_does_not_invent_holds(self):
        self.db.create_payment('historical', 420, 4900, 2450)
        self.db.create_order('history', 420, self.catalog.get('tee-sila-i-chest'), 'M', '+79990000000', payment_id='historical', amount_rub=4900, status='awaiting_payment')
        old_orders = [dict(r) for r in self.records('orders')]
        old_payments = [dict(r) for r in self.records('payments')]
        for _ in range(2):
            reopened = Database(self.db.path)
            reopened.close_current()
        purchases = self.records('purchases')
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0]['stock_managed'], 0)
        self.assertEqual([dict(r) for r in self.records('orders')], old_orders)
        self.assertEqual([dict(r) for r in self.records('payments')], old_payments)
        self.assertEqual(self.records('stock_reservations'), [])
        self.assertTrue(self.db.payment_is_payable('historical'))

    def test_edit_fallback_and_callback_byte_limits(self):
        self.message('/cart')
        self.api.edit_message_text.side_effect = RuntimeError('message to edit not found')
        self.message('/menu')
        self.assertGreaterEqual(self.api.send_message.call_count, 2)
        for call in self.api.mock_calls:
            if call[0] not in {'send_message', 'edit_message_text'}: continue
            self.assertLessEqual(len(call.args[-2]), 3900)
            for row in call.args[-1]['inline_keyboard']:
                for b in row:
                    if 'callback_data' in b: self.assertLessEqual(len(b['callback_data'].encode()), 64)

    def test_role_grant_replay_cannot_resurrect_a_revoked_employee(self):
        action = self.db.create_chat_action(9001, 'role', {'target_id': 9002, 'role': 'manager'})
        self.callback(action, 9001)
        self.assertEqual(self.db.staff_role(9002), 'manager')
        self.db.set_staff_role(9001, 9002, 'none', owner_ids=self.settings.admin_ids)
        self.callback(action, 9001)
        self.assertEqual(self.db.staff_role(9002), '')
        self.assertEqual(len(self.records('chat_action_results')), 1)

    def test_owner_can_release_a_revoked_assignee_without_resetting_stage(self):
        self.db.set_staff_role(9001, 9002, 'manager', owner_ids=self.settings.admin_ids)
        receipt = self.checkout()
        self.db.claim_purchase(9002, receipt['purchase_id'])
        self.db.set_staff_role(9001, 9002, 'none', owner_ids=self.settings.admin_ids)
        view = self.db.purchase_view(receipt['purchase_id'], actor_id=9001, owner_ids=self.settings.admin_ids)
        self.db.release_purchase(9001, receipt['purchase_id'], view['version'], owner_ids=self.settings.admin_ids)
        self.assertIsNone(self.db.purchase_view(receipt['purchase_id'], user_id=420)['assigned_to'])
        self.assertEqual(self.db.get_payment(receipt['payment_id'])['status'], 'pending')

    def test_checkout_button_recovers_receipt_after_preview_pii_is_pruned(self):
        self.profile()
        self.cart()
        self.bot.commerce.checkout(420, 420)
        token = self.db.get_state(420)[1]['token']
        self.callback('c:confirm:' + token)
        self.db.connection().execute('DELETE FROM checkout_drafts')
        self.callback('c:confirm:' + token)
        self.assertEqual(len(self.records('purchases')), 1)
        self.assertIn('ПОКУПКА №0001', self.panel()[0])

    def test_conflicting_receipt_is_in_staff_review_queue_and_blocks_fulfillment(self):
        receipt = self.checkout()
        self.confirm_stars(receipt, 'same-id')
        self.db.mark_payment_paid(receipt['payment_id'], 'stars', 'same-id', amount_minor=1, currency='XTR', payer_id=420)
        rows = self.db.staff_purchases(9001, 'review', owner_ids=self.settings.admin_ids)
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            self.db.set_order_status(receipt['order_ids'][0], 'confirmed')
        self.assertEqual(self.records('stock_reservations')[0]['state'], 'consumed')

    def test_mixed_owner_legacy_payment_is_not_merged_or_made_payable(self):
        self.db.create_payment('corrupt-legacy', 420, 4900, 2450)
        self.db.create_order('corrupt-old', 421, self.catalog.get('tee-sila-i-chest'), 'M', '+79990000000',
                             payment_id='corrupt-legacy', amount_rub=4900, status='awaiting_payment')
        reopened = Database(self.db.path)
        self.addCleanup(reopened.close_current)
        self.assertEqual(reopened.purchases_for_user(420), [])
        self.assertFalse(reopened.payment_is_payable('corrupt-legacy'))
        self.assertEqual(len(reopened.orders_for_user(421)), 1)

    def test_new_text_turn_opens_panel_below_input_instead_of_editing_old_question(self):
        self.profile()
        self.bot.commerce.ask_field(420, 420, 'city', 'profile')
        old_calls = self.api.send_message.call_count
        self.message('Санкт-Петербург')
        self.assertEqual(self.api.send_message.call_count, old_calls + 1)
        self.assertEqual(self.db.account_profile(420)['city'], 'Санкт-Петербург')

    def test_demo_fixture_has_no_external_network_dependency(self):
        import tempfile
        from tools.bot_demo import Demo
        with tempfile.TemporaryDirectory() as directory, patch('urllib.request.urlopen', side_effect=AssertionError('external I/O')):
            demo = Demo(directory)
            try:
                demo.act(420, {'text': '/menu'})
                demo.act(9001, {'text': '/stock'})
                self.assertEqual(demo.snapshot(420)['purchases'], 0)
            finally:
                demo.db.close_current()

    def test_authenticated_cart_profile_and_purchases_api(self):
        server = start_health_server(0, self.catalog, self.settings, self.db, self.api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_address[1])
        def request(path, uid=420, data=None, valid=True):
            fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
            secret = hmac.new(b'WebAppData', self.settings.token.encode(), hashlib.sha256).digest()
            fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(), hashlib.sha256).hexdigest()
            headers = {'X-Telegram-Init-Data': urllib.parse.urlencode(fields) if valid else 'invalid', 'Content-Type': 'application/json'}
            req = urllib.request.Request(url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
            try: response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc: response = exc
            with response:
                return response.status, json.load(response)
        self.assertEqual(request('/api/cart', valid=False)[0], 401)
        status, saved = request('/api/cart', data={'items': self.payload['items'], 'revision': 0, 'operation_id': 'api-cart', 'user_id': 421})
        self.assertEqual(status, 200)
        self.assertEqual(saved['cart']['user_id'], 420)
        self.assertEqual(request('/api/cart?user_id=420', uid=421)[1]['cart']['items'], [])
        self.assertEqual(request('/api/cart', data={'items': [], 'revision': 0, 'operation_id': 'stale'})[0], 409)
        self.assertEqual(request('/api/account', data={'profile': {'phone': '+79990000000'}, 'consent': 'yes'})[0], 400)
        self.assertIsNone(self.db.get_user(420)['phone'])
        status, profile = request('/api/account', data={'profile': {'name': 'Private', 'phone': '+79990000000'}, 'consent': True})
        self.assertEqual(status, 200)
        self.assertEqual(profile['profile']['name'], 'Private')
        self.assertEqual(request('/api/account', uid=421)[1]['profile']['phone'], '')
        receipt = self.checkout({**self.payload, 'cart_revision': saved['cart']['revision']})
        self.assertTrue(receipt['ok'])
        self.assertEqual(len(request('/api/purchases')[1]['purchases']), 1)
        self.assertEqual(request('/api/purchases', uid=421)[1]['purchases'], [])
        both = request('/api/my-orders')[1]
        self.assertEqual(len(both['orders']), 1)  # backward-compatible flat DTO
        self.assertEqual(len(both['purchases']), 1)


if __name__ == '__main__':
    unittest.main()
