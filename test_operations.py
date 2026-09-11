"""Offline after-sale acceptance: agreements, dispatch, support, ACL and UI."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import html
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from bot import Database, start_health_server
from commerce_store import CartConflict
from operations_store import delivery_minor, delivery_money
import test_commerce as commerce


class OperationsTests(unittest.TestCase):
    checkout = commerce.CommerceTests.checkout
    confirm_stars = commerce.CommerceTests.confirm_stars
    records = commerce.CommerceTests.records
    callback = commerce.CommerceTests.callback
    panel = commerce.CommerceTests.panel
    press = commerce.CommerceTests.press
    profile = commerce.CommerceTests.profile

    def setUp(self):
        commerce.CommerceTests.setUp(self)
        self.msg_id = 1000
        self.owner = dict(owner_ids=self.settings.admin_ids)
        for uid, role in ((9002, 'warehouse'), (9003, 'manager'), (9004, 'support'), (9005, 'support')):
            self.db.upsert_user({'id': uid, 'first_name': role})
            self.db.set_staff_role(9001, uid, role, **self.owner)
        self.terms = {'carrier': 'sdek', 'destination': 'Приватный ПВЗ, Тестовая улица 10',
                      'amount_minor': 35050, 'billing': 'carrier', 'eta': '2–4 дня после передачи',
                      'basis': 'Только тестовый подтверждённый тариф'}

    def message(self, text='', uid=420, mid=None):
        self.msg_id += 1
        self.assertTrue(self.bot.handle_update({'message': {'message_id': mid or self.msg_id, 'from': {'id': uid},
            'chat': {'id': uid, 'type': 'private'}, 'text': text}}))

    def buy(self, paid=False, ready=False):
        receipt = self.checkout()
        self.assertTrue(receipt['ok'], receipt)
        pid = receipt['purchase_id']
        if paid: self.confirm_stars(receipt, 'operations-charge')
        if ready:
            self.db.claim_purchase(9003, pid)
            for stage in ('packing', 'ready'):
                v = self.db.purchase_view(pid, user_id=420)['version']
                self.db.advance_purchase(9003, pid, stage, v)
        return receipt, pid

    def propose(self, pid, operation='quote-one', terms=None, actor=9003):
        version = self.db.delivery_view(420, pid)['version']
        return self.db.propose_delivery(actor, pid, terms or self.terms, version, operation, **self.owner)

    def accept(self, pid, operation='accept-one', decision=True):
        p = self.db.delivery_view(420, pid)
        return self.db.answer_delivery(420, pid, p['quote']['quote_id'], p['version'], decision, operation)

    def ship(self, pid, operation='dispatch-one'):
        p = self.db.delivery_view(420, pid)
        return self.db.dispatch_delivery(9003, pid, p['version'], '' if p['quote']['carrier'] == 'pickup' else 'TEST123456', operation)

    def ticket(self, topic='question', pid=None, operation='new-ticket', body='Проверочный текст обращения'):
        self.db.set_consent(420, service_only=True)
        return self.db.create_ticket(420, topic, body, operation, pid)

    def reply(self, tid, actor=420, body='Согласованное решение', operation='reply-one', staff=False, **kwargs):
        t = self.db.ticket_view(actor, tid, staff=staff, **self.owner)
        return self.db.reply_ticket(actor, tid, body, t['version'], operation, staff=staff, **self.owner, **kwargs)

    def test_new_purchase_has_empty_plan_not_fictional_tariff(self):
        _, pid = self.buy()
        p = self.db.delivery_view(420, pid)
        self.assertTrue(p['operations_managed'])
        self.assertIsNone(p['quote'])
        self.assertIsNone(p['budget_minor'])
        self.assertFalse(p['can_dispatch'])
        self.assertEqual(p['state'], 'not_sent')
        self.assertEqual(self.records('delivery_events'), [])

    def test_phase_one_migration_keeps_old_purchase_unmanaged_and_has_no_invented_history(self):
        r, pid = self.buy()
        before = dict(self.db.get_payment(r['payment_id']))
        conn = self.db.connection()
        conn.execute('DELETE FROM delivery_plans')
        conn.execute('ALTER TABLE purchases DROP COLUMN operations_managed')
        restored = Database(self.db.path)
        self.addCleanup(restored.close_current)
        plan = restored.delivery_view(420, pid)
        self.assertFalse(plan['operations_managed'])
        self.assertIsNone(plan['quote'])
        self.assertEqual(restored.connection().execute('SELECT COUNT(*) FROM delivery_plans').fetchone()[0], 0)
        self.assertEqual(dict(restored.get_payment(r['payment_id'])), before)
        self.assertEqual(restored.connection().execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(list(restored.connection().execute('PRAGMA foreign_key_check')), [])

    def test_quote_and_draft_require_consent_and_staff_role(self):
        _, pid = self.buy()
        for actor in (420, 421, 9002, 9004):
            with self.assertRaises(PermissionError): self.propose(pid, actor=actor)
        self.db.connection().execute('UPDATE users SET consent_at=NULL WHERE user_id=420')
        with self.assertRaises(ValueError): self.propose(pid)
        with self.assertRaises(ValueError):
            self.db.save_service_draft(9003, 'quote', {'purchase_id': pid}, **self.owner)
        self.assertEqual(self.records('delivery_quotes'), [])

    def test_exact_delivery_cents_zero_and_bad_numeric_values(self):
        for text, minor in [('0', 0), ('0,01', 1), ('350.50', 35050), ('999999.99', 99999999)]:
            self.assertEqual(delivery_minor(text), minor)
        self.assertEqual(delivery_money(35050), '350,50 ₽')
        for value in ['NaN', 'Infinity', '-1', '1e2', '.5', '5.', '1.123', '1 000', 'true', '1000000']:
            with self.assertRaises(ValueError): delivery_minor(value)
        _, pid = self.buy()
        for amount in [None, True, -1, 0.5, float('nan'), float('inf'), 100000000, '35050']:
            with self.assertRaises(ValueError): self.propose(pid, terms={**self.terms, 'amount_minor': amount})
        with self.assertRaises(ValueError): self.propose(pid, terms={**self.terms, 'billing': 'shop'})
        with self.assertRaises(ValueError): self.propose(pid, terms={**self.terms, 'billing': 'merchant'})
        qid = self.propose(pid, terms={**self.terms, 'amount_minor': 0, 'billing': 'shop'})
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['quote_id'], qid)
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['amount_minor'], 0)

    def test_delivery_never_changes_goods_payment_or_customer_snapshot(self):
        r, pid = self.buy()
        before = dict(self.db.get_payment(r['payment_id']))
        customer = self.db.purchase_view(pid, user_id=420)['customer']
        self.propose(pid)
        self.accept(pid)
        self.assertEqual(dict(self.db.get_payment(r['payment_id'])), before)
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['customer'], customer)
        self.assertEqual(self.db.delivery_view(420, pid)['budget_minor'], before['amount_rub'] * 100 + 35050)
        self.assertEqual(self.records('payment_receipts'), [])

    def test_quotes_keep_old_terms_and_acceptance_when_replaced(self):
        _, pid = self.buy()
        old = self.propose(pid)
        self.accept(pid)
        new = self.propose(pid, 'quote-two', {**self.terms, 'destination': 'Другой ПВЗ', 'amount_minor': 12345})
        q = self.db.connection().execute('SELECT * FROM delivery_quotes WHERE quote_id=?', (old,)).fetchone()
        self.assertEqual(q['destination'], self.terms['destination'])
        self.assertEqual(q['amount_minor'], 35050)
        self.assertIsNotNone(q['accepted_at'])
        self.assertEqual(q['status'], 'superseded')
        current = self.db.delivery_view(420, pid)
        self.assertEqual(current['quote']['quote_id'], new)
        self.assertEqual(current['quote']['status'], 'offered')
        self.assertFalse(current['can_dispatch'])

    def test_stale_foreign_or_expired_offer_cannot_be_accepted(self):
        _, pid = self.buy()
        old = self.propose(pid)
        oldplan = self.db.delivery_view(420, pid)
        with self.assertRaises(ValueError): self.db.answer_delivery(421, pid, old, oldplan['version'], True, 'foreign')
        with self.assertRaises(ValueError): self.db.answer_delivery(420, pid, True, oldplan['version'], True, 'bool-id')
        with self.assertRaises(ValueError): self.db.answer_delivery(420, pid, old, oldplan['version'], 'true', 'bool-answer')
        self.propose(pid, 'replacement')
        with self.assertRaises(CartConflict): self.db.answer_delivery(420, pid, old, oldplan['version'], True, 'stale')
        self.db.connection().execute('UPDATE delivery_quotes SET expires_at=0')
        with self.assertRaises(ValueError): self.accept(pid)
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['status'], 'expired')

    def test_decline_requires_fresh_terms_and_has_no_hidden_charge(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid, decision=False)
        with self.assertRaises(ValueError): self.ship(pid)
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['status'], 'declined')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')

    def test_proposal_operation_is_actor_scoped_idempotent_and_fingerprinted(self):
        _, pid = self.buy()
        qid = self.propose(pid)
        self.assertEqual(self.db.propose_delivery(9003, pid, self.terms, 0, 'quote-one'), qid)
        with self.assertRaises(ValueError): self.db.propose_delivery(9003, pid, {**self.terms, 'amount_minor': 1}, 0, 'quote-one')
        with self.assertRaises(CartConflict): self.db.propose_delivery(9003, pid, self.terms, 0, 'new-op')
        self.assertEqual(len(self.records('delivery_quotes')), 1)
        self.db.set_staff_role(9001, 9003, 'none', **self.owner)
        with self.assertRaises(PermissionError): self.db.propose_delivery(9003, pid, self.terms, 0, 'quote-one')

    def test_duplicate_answers_do_not_repeat_customer_or_team_notifications(self):
        _, pid = self.buy()
        qid = self.propose(pid)
        self.accept(pid)
        count = len(self.records('notifications'))
        self.db.answer_delivery(420, pid, qid, 1, True, 'accept-one')
        self.assertEqual(len(self.records('notifications')), count)
        self.assertEqual(len([e for e in self.records('delivery_events') if e['kind'] == 'accepted']), 1)

    def test_cannot_complete_through_native_or_legacy_shortcut_before_receipt(self):
        r, pid = self.buy(paid=True, ready=True)
        p = self.db.purchase_view(pid, user_id=420)
        with self.assertRaises(ValueError): self.db.advance_purchase(9003, pid, 'completed', p['version'])
        with self.assertRaises(ValueError): self.db.set_order_status(r['order_ids'][0], 'completed')
        self.callback(f'c:work:{pid}', 9003)
        self.assertNotIn('Следующий этап · завершён', json.dumps(self.panel()[1], ensure_ascii=False))
        self.assertEqual(self.db.get_order(r['order_ids'][0])['status'], 'confirmed')

    def test_dispatch_rechecks_paid_ready_agreement_assignment_and_acl(self):
        r, pid = self.buy()
        self.propose(pid)
        self.accept(pid)
        version = self.db.delivery_view(420, pid)['version']
        with self.assertRaises(PermissionError): self.db.dispatch_delivery(9004, pid, version, 'TEST123', 'deny-support')
        with self.assertRaises(PermissionError): self.db.dispatch_delivery(9002, pid, version, 'TEST123', 'not-assigned')
        with self.assertRaises(ValueError): self.db.dispatch_delivery(9001, pid, version, 'TEST123', 'unpaid', **self.owner)
        self.confirm_stars(r, 'operations-charge')
        self.db.claim_purchase(9003, pid)
        with self.assertRaises(ValueError): self.ship(pid)
        for target in ('packing', 'ready'):
            self.db.advance_purchase(9003, pid, target, self.db.purchase_view(pid, user_id=420)['version'])
        self.ship(pid)
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'in_transit')

    def test_expired_accepted_quote_requires_renewal_at_dispatch(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.db.connection().execute('UPDATE delivery_quotes SET expires_at=0')
        with self.assertRaises(ValueError): self.ship(pid)

    def test_tracking_must_be_a_number_not_a_url_or_markup(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        v = self.db.delivery_view(420, pid)['version']
        for code in ['https://example.com/track', '<script>', '1', 'номер1234', '1234 5678', 'a' * 49, '1234\n5678']:
            with self.assertRaises(ValueError): self.db.dispatch_delivery(9003, pid, v, code, 'bad-code')
        self.assertEqual(self.db.tracking_code(' abcd-1234 '), 'ABCD-1234')
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'not_sent')

    def test_dispatch_concurrency_has_one_fact_and_one_notification(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        v = self.db.delivery_view(420, pid)['version']
        barrier = threading.Barrier(2)
        def attempt(op):
            barrier.wait()
            try:
                self.db.dispatch_delivery(9003, pid, v, 'TEST123', op)
                return True
            except CartConflict: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ('first-ship', 'second-ship'))), 1)
        self.assertEqual(len([x for x in self.records('delivery_events') if x['kind'] == 'dispatched']), 1)
        self.assertEqual(len([x for x in self.records('notifications') if ':dispatch:' in x['notification_id']]), 1)

    def test_dispatch_retry_after_role_revoke_cannot_send_again(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        v = self.db.delivery_view(420, pid)['version']
        self.ship(pid)
        self.db.dispatch_delivery(9003, pid, v, 'TEST123456', 'dispatch-one')
        self.db.set_staff_role(9001, 9003, 'none', **self.owner)
        with self.assertRaises(PermissionError): self.db.dispatch_delivery(9003, pid, v, 'TEST123456', 'dispatch-one')
        self.assertEqual(len([x for x in self.records('delivery_events') if x['kind'] == 'dispatched']), 1)

    def test_after_dispatch_address_and_fee_cannot_change_tracking_needs_reason(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.ship(pid)
        with self.assertRaises(ValueError): self.propose(pid, 'silent-requote')
        v = self.db.delivery_view(420, pid)['version']
        with self.assertRaises(ValueError): self.db.correct_tracking(9003, pid, v, 'NEW12345', '', 'bad-fix')
        with self.assertRaises(PermissionError): self.db.correct_tracking(9002, pid, v, 'NEW12345', 'Опечатка', 'bad-role')
        self.db.correct_tracking(9003, pid, v, 'NEW12345', 'Опечатка при вводе', 'track-fix')
        p = self.db.delivery_view(420, pid)
        self.assertEqual(p['tracking'], 'NEW12345')
        self.assertEqual(p['events'][0]['data']['previous'], 'TEST123456')
        self.assertEqual(p['events'][0]['data']['reason'], 'Опечатка при вводе')

    def test_received_by_buyer_completes_all_lines_exactly_once(self):
        self.payload['items'].append({'product_id': 'tag-sila-i-chest', 'size': 'ONE SIZE', 'quantity': 2})
        _, pid = self.buy(paid=True, ready=True)
        with self.assertRaises(ValueError): self.db.mark_received(420, pid, 0, 'early')
        self.propose(pid)
        self.accept(pid)
        self.ship(pid)
        v = self.db.delivery_view(420, pid)['version']
        with self.assertRaises(ValueError): self.db.mark_received(421, pid, v, 'foreign-receipt')
        self.db.mark_received(420, pid, v, 'received')
        self.db.mark_received(420, pid, v, 'received')
        self.assertEqual(self.db.delivery_view(420, pid)['delivered_source'], 'customer')
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['fulfillment'], 'completed')
        self.assertEqual({r['status'] for r in self.records('orders')}, {'completed'})
        self.assertEqual(len([x for x in self.records('delivery_events') if x['kind'] == 'received']), 1)
        with self.assertRaises(ValueError): self.db.mark_received(420, pid, v + 1, 'double-receipt')

    def test_manual_pickup_is_free_and_not_an_otp_verified_event(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid, terms={**self.terms, 'carrier': 'pickup', 'billing': 'shop', 'amount_minor': 0})
        self.accept(pid)
        self.ship(pid)
        p = self.db.delivery_view(420, pid)
        self.assertEqual(p['state'], 'pickup_ready')
        self.assertEqual(p['tracking'], '')
        self.db.mark_received(9003, pid, p['version'], 'manual-receipt', staff=True)
        p = self.db.delivery_view(420, pid)
        self.assertEqual(p['delivered_source'], 'staff')
        self.assertEqual(p['state'], 'delivered')

    def test_financial_review_blocks_dispatch_and_survives_ticket_resolution(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'operations-charge', amount_minor=1, currency='XTR', payer_id=420)
        tid = self.ticket('payment', pid)
        self.db.claim_ticket(9003, tid, 'claim')
        self.reply(tid, 9003, staff=True, resolve=True)
        self.assertTrue(self.db.payment_attention(r['payment_id']))
        with self.assertRaises(ValueError): self.ship(pid)

    def test_actual_receipt_keeps_financial_exception_instead_of_overwriting_it(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.ship(pid)
        self.db.set_order_status(r['order_ids'][0], 'cancelled')
        p = self.db.delivery_view(420, pid)
        self.db.mark_received(420, pid, p['version'], 'received-after-cancel')
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'delivered')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'refund_required')
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['fulfillment'], 'cancelled')
        self.assertEqual({r['status'] for r in self.records('orders')}, {'cancelled'})

    def test_delivery_and_ticket_mutations_roll_back_outbox_and_receipts_together(self):
        _, pid = self.buy(paid=True, ready=True)
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.propose(pid)
        self.assertEqual(self.records('delivery_quotes'), [])
        self.assertEqual(self.records('delivery_events'), [])
        self.assertIsNone(self.db.service_result(9003, 'quote-one'))
        self.propose(pid)
        self.accept(pid)
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.ship(pid)
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'not_sent')
        self.assertIsNone(self.db.service_result(9003, 'dispatch-one'))
        self.ship(pid)
        p = self.db.delivery_view(420, pid)
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.db.mark_received(420, pid, p['version'], 'receive-fail')
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'in_transit')
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['fulfillment'], 'ready')
        with patch.object(self.db, '_notify_ticket_team', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.ticket()
        self.assertEqual(self.records('support_tickets'), [])
        self.assertEqual(self.records('support_messages'), [])
        self.assertIsNone(self.db.service_result(420, 'new-ticket'))

    def test_customer_and_staff_delivery_views_keep_contact_scope(self):
        _, pid = self.buy()
        self.propose(pid)
        for actor in (9002, 9004):
            data = self.db.delivery_view(actor, pid, staff=True)
            self.assertNotIn('destination', data['quote'])
            self.assertNotIn('basis', data['quote'])
            self.assertNotIn(self.terms['destination'], json.dumps(data, ensure_ascii=False))
            self.assertNotIn('customer', self.db.purchase_view(pid, actor_id=actor))
        self.assertEqual(self.db.delivery_view(9003, pid, staff=True)['quote']['destination'], self.terms['destination'])
        with self.assertRaises(ValueError): self.db.delivery_view(421, pid)
        with self.assertRaises(PermissionError): self.db.delivery_view(421, pid, staff=True)

    def test_ticket_creation_cannot_save_preconsent_or_foreign_purchase_text(self):
        secret = '+79998887766 НЕСОГЛАСОВАННЫЙ ТЕКСТ'
        with self.assertRaises(ValueError): self.db.create_ticket(420, 'question', secret, 'forbidden')
        with self.assertRaises(ValueError): self.db.save_service_draft(420, 'ticket_new', {'body': secret})
        self.assertNotIn(secret, '\n'.join(self.db.connection().iterdump()))
        _, pid = self.buy()
        self.db.set_consent(421, service_only=True)
        with self.assertRaises(ValueError): self.db.create_ticket(421, 'return', secret, 'foreign', pid)
        self.assertEqual(self.records('support_tickets'), [])

    def test_ticket_idempotency_is_scoped_and_does_not_reopen_closed_ticket(self):
        tid = self.ticket()
        self.reply(tid, resolve=True)
        self.assertEqual(self.ticket(), tid)
        self.assertEqual(self.db.ticket_view(420, tid)['status'], 'resolved')
        with self.assertRaises(ValueError): self.ticket(body='Изменённый текст')
        self.db.set_consent(421, service_only=True)
        other = self.db.create_ticket(421, 'question', 'Другое обращение', 'new-ticket')
        self.assertNotEqual(other, tid)

    def test_sensitive_tickets_hold_dispatch_until_explicit_resolution_or_withdrawal(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        for topic in ('delivery', 'exchange', 'return', 'payment'):
            tid = self.ticket(topic, pid, 'ticket-' + topic)
            self.assertIn(tid, self.db.dispatch_blockers(pid))
            with self.assertRaises(ValueError): self.ship(pid)
            self.db.claim_ticket(9004, tid, 'claim-' + topic)
            with self.assertRaises(PermissionError): self.reply(tid, 9004, staff=True, resolve=True, operation='close-' + topic)
            self.reply(tid, resolve=True, operation='withdraw-' + topic)
        self.assertEqual(self.db.dispatch_blockers(pid), [])
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')
        self.ship(pid)

    def test_after_dispatch_return_case_is_not_a_fictional_shipping_pause(self):
        _, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.ship(pid)
        tid = self.ticket('return', pid)
        ticket = self.db.ticket_view(420, tid)
        self.assertFalse(ticket['blocks_dispatch'])
        self.assertTrue(ticket['requires_manager'])
        self.assertEqual(self.db.delivery_view(420, pid)['blocking_tickets'], [])
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'in_transit')
        self.db.claim_ticket(9004, tid, 'claim')
        self.callback(f'o:workticket:{tid}', 9004)
        self.assertNotIn('Ответить и закрыть', json.dumps(self.panel()[1], ensure_ascii=False))
        with self.assertRaises(PermissionError): self.reply(tid, 9004, staff=True, resolve=True)

    def test_customer_reply_reopens_own_ticket_and_restores_dispatch_hold(self):
        _, pid = self.buy()
        tid = self.ticket('return', pid)
        self.reply(tid, resolve=True)
        self.assertFalse(self.db.ticket_view(420, tid)['blocks_dispatch'])
        self.reply(tid, body='Проблема осталась', operation='reopen')
        self.assertEqual(self.db.ticket_view(420, tid)['status'], 'open')
        self.assertTrue(self.db.ticket_view(420, tid)['blocks_dispatch'])
        with self.assertRaises(ValueError): self.reply(tid, 421)

    def test_support_reply_requires_assignment_and_rechecks_role(self):
        tid = self.ticket()
        with self.assertRaises(PermissionError): self.reply(tid, 9004, staff=True)
        self.db.claim_ticket(9004, tid, 'claim')
        self.reply(tid, 9004, staff=True)
        self.assertEqual(self.db.ticket_view(420, tid)['status'], 'waiting_customer')
        with self.assertRaises(PermissionError): self.reply(tid, 9005, staff=True)
        self.db.set_staff_role(9001, 9004, 'warehouse', **self.owner)
        self.assertIsNone(self.db.ticket_view(9003, tid, staff=True)['assigned_to'])
        with self.assertRaises(PermissionError): self.db.reply_ticket(9004, tid, 'Согласованное решение', 1, 'reply-one', staff=True)
        self.assertEqual(len(self.db.ticket_view(420, tid)['messages']), 2)

    def test_two_staff_members_cannot_claim_same_ticket(self):
        tid = self.ticket()
        barrier = threading.Barrier(2)
        def claim(uid):
            barrier.wait()
            try:
                self.db.claim_ticket(uid, tid, 'claim-op')
                return True
            except ValueError: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, (9004, 9005))), 1)

    def test_release_and_old_claim_replay_cannot_reassign_a_ticket(self):
        tid = self.ticket()
        self.db.claim_ticket(9004, tid, 'old-claim')
        t = self.db.ticket_view(9004, tid, staff=True)
        with self.assertRaises(PermissionError): self.db.claim_ticket(9005, tid, 'steal', release=True, version=t['version'])
        self.db.claim_ticket(9001, tid, 'owner-release', release=True, version=t['version'], **self.owner)
        self.db.claim_ticket(9005, tid, 'new-claim')
        self.db.claim_ticket(9004, tid, 'old-claim')
        self.db.claim_ticket(9001, tid, 'owner-release', release=True, version=t['version'], **self.owner)
        self.assertEqual(self.db.ticket_view(9005, tid, staff=True)['assigned_to'], 9005)

    def test_internal_notes_are_not_customer_messages_notifications_or_audit_payloads(self):
        tid = self.ticket()
        self.db.claim_ticket(9004, tid, 'claim')
        before = len(self.records('notifications'))
        note = 'ВНУТРЕННИЙ СЕКРЕТ +79990004444'
        self.reply(tid, 9004, note, staff=True, internal=True)
        self.assertEqual(len(self.records('notifications')), before)
        self.assertNotIn(note, json.dumps(self.db.ticket_view(420, tid), ensure_ascii=False))
        self.assertIn(note, json.dumps(self.db.ticket_view(9003, tid, staff=True), ensure_ascii=False))
        for table in ('staff_audit', 'events', 'chat_actions', 'states', 'service_requests', 'notifications'):
            self.assertNotIn(note, str([tuple(r) for r in self.records(table)]), table)
        mid = self.records('support_messages')[-1]['message_id']
        self.callback(f'o:message:{tid}:{mid}:0:0')
        self.assertIn('Сообщение не найдено', self.panel()[0])
        self.assertNotIn(note, self.panel()[0])
        with self.assertRaises(PermissionError): self.db.ticket_view(9002, tid, staff=True)

    def test_reply_cas_and_outbox_failure_do_not_lose_messages(self):
        tid = self.ticket()
        self.db.claim_ticket(9004, tid, 'claim')
        self.reply(tid, operation='new-customer')
        with self.assertRaises(CartConflict): self.db.reply_ticket(9004, tid, 'Старый ответ', 1, 'stale-reply', staff=True)
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.reply(tid, 9004, staff=True)
        self.assertEqual(len(self.records('support_messages')), 2)
        self.assertIsNone(self.db.service_result(9004, 'reply-one'))
        self.assertEqual(self.db.ticket_view(420, tid)['status'], 'open')

    def test_team_alerts_are_durable_and_never_include_customer_text_in_group(self):
        tid = self.ticket(body='ЛИЧНЫЙ ВОПРОС +79990008888')
        self.assertEqual(len(self.records('ticket_alerts')), 1)
        self.db.queue_ticket_alerts()
        self.assertIsNone(self.records('ticket_alerts')[0]['queued_at'])
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        self.assertIsNone(self.records('ticket_alerts')[0]['queued_at'])
        self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        group = [r for r in self.records('notifications') if r['chat_id'] == -100123]
        self.assertEqual(len(group), 1)
        self.assertIn(f'№{tid:04d}', group[0]['body'])
        self.assertNotIn('ЛИЧНЫЙ', group[0]['body'])
        self.assertNotIn('+7999', group[0]['body'])
        self.callback(f'o:workticket:{tid}', 9004, private=False)
        self.api.answer_callback.assert_called_with('test-callback', 'Открой бота в личных сообщениях')

    def test_assigned_notifications_go_to_current_authorized_staff(self):
        tid = self.ticket()
        self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        self.db.claim_ticket(9004, tid, 'claim')
        self.reply(tid)
        self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        self.assertTrue(any(r['chat_id'] == 9004 for r in self.records('notifications')))
        self.reply(tid, operation='second-message')
        self.db.set_staff_role(9001, 9004, 'none', **self.owner)
        count = len([r for r in self.records('notifications') if r['chat_id'] == 9004])
        self.db.queue_ticket_alerts(self.settings.admin_ids, -100123)
        self.assertEqual(len([r for r in self.records('notifications') if r['chat_id'] == 9004]), count)

    def test_dispatch_problem_is_public_case_not_a_fake_refund_or_carrier_event(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid)
        self.accept(pid)
        self.ship(pid)
        v = self.db.delivery_view(420, pid)['version']
        with self.assertRaises(PermissionError):
            self.db.report_delivery_issue(9002, pid, v, 'Нет назначения', 'wrong-assignee')
        tid = self.db.report_delivery_issue(9003, pid, v, 'Проверяем местонахождение отправления', 'shipment-problem')
        again = self.db.report_delivery_issue(9003, pid, v, 'Проверяем местонахождение отправления', 'shipment-problem')
        self.assertEqual(tid, again)
        self.assertEqual(self.db.ticket_view(420, tid)['purchase_id'], pid)
        self.assertEqual(self.db.delivery_view(420, pid)['events'][0]['source'], 'staff')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'in_transit')

    def test_notification_invalidates_older_panel_so_navigation_stays_below_it(self):
        self.callback('c:home')
        self.assertEqual(len(self.records('chat_panels')), 1)
        self.db.enqueue_message('test-service-notice', 420, 'Новое событие по покупке')
        self.bot.flush_notifications()
        self.assertEqual(self.records('chat_panels'), [])
        before = self.api.send_message.call_count
        self.callback('c:home')
        self.assertEqual(self.api.send_message.call_count, before + 1)

    def test_native_ticket_consent_preview_and_pause_do_not_collect_unsolicited_text(self):
        self.message('/support')
        self.assertEqual(self.db.get_state(420)[0], 'ops_consent')
        self.message('DO NOT STORE +79995556666')
        self.assertNotIn('DO NOT STORE', '\n'.join(self.db.connection().iterdump()))
        consent = self.press('Согласен')
        self.assertEqual(self.db.get_user(420)['service_only'], 1)
        self.press('Вопрос')
        draft = self.db.get_state(420)[1]['token']
        self.callback(consent)
        self.assertEqual(self.db.get_state(420)[1]['token'], draft)
        self.message('/menu')
        self.message('PAUSED PRIVATE +79997778888')
        self.assertNotIn('PAUSED PRIVATE', '\n'.join(self.db.connection().iterdump()))
        self.message('/resume')
        self.message('Проверить статус покупки')
        self.assertEqual(self.db.get_state(420)[0], 'ops_preview')
        self.assertEqual(self.records('support_tickets'), [])
        token = self.press('Отправить сообщение')
        self.assertEqual(len(self.records('support_tickets')), 1)
        self.callback(token)
        self.assertEqual(len(self.records('support_messages')), 1)
        self.assertIn('Проверить статус покупки', self.panel()[0])

    def test_native_quote_wizard_uses_exact_cents_private_drafts_and_real_message_dedup(self):
        _, pid = self.buy()
        self.callback(f'o:workdelivery:{pid}', 9003)
        self.press('Предложить новые условия', 9003)
        self.press('СДЭК', 9003)
        self.press('Покупатель платит', 9003)
        self.message(self.terms['destination'], 9003, mid=99)
        self.assertEqual(self.db.get_state(9003)[1]['field'], 'amount_minor')
        self.message(self.terms['destination'], 9003, mid=99)
        self.assertEqual(self.db.get_state(9003)[1]['field'], 'amount_minor')
        self.message('350,50', 9003)
        self.message('2–4 дня', 9003)
        self.message('Подтверждённый тестовый тариф', 9003)
        self.assertEqual(self.records('delivery_quotes'), [])
        for table in ('states', 'chat_actions', 'staff_audit', 'events', 'service_requests'):
            self.assertNotIn(self.terms['destination'], str([tuple(r) for r in self.records(table)]))
        self.press('Проверено · отправить условия', 9003)
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['amount_minor'], 35050)
        self.callback(f'o:delivery:{pid}')
        self.press('Условия подходят')
        self.assertEqual(self.db.delivery_view(420, pid)['quote']['status'], 'accepted')

    def test_native_confirmation_is_owner_bound_and_rechecks_revoked_role(self):
        tid = self.ticket()
        self.callback(f'o:workticket:{tid}', 9004)
        self.press('Взять обращение', 9004)
        self.press('Ответить покупателю', 9004)
        self.message('Ответ без скрытого возврата', 9004)
        token = [b['callback_data'] for r in self.panel()[1]['inline_keyboard'] for b in r if 'Отправить сообщение' in b['text']][0]
        self.callback(token, 420)
        self.assertEqual(len(self.records('support_messages')), 1)
        self.db.set_staff_role(9001, 9004, 'none', **self.owner)
        self.callback(token, 9004)
        self.assertEqual(len(self.records('support_messages')), 1)
        self.assertNotIn('Ответ без скрытого возврата', self.panel()[0])

    def test_processed_native_draft_recovers_without_resending_even_after_pruning(self):
        self.profile()
        self.message('/support')
        self.press('Вопрос')
        self.message('Текст обращения')
        token = self.press('Отправить сообщение')
        self.db.connection().execute('DELETE FROM service_drafts')
        self.callback(token)
        self.assertIn('ОБРАЩЕНИЕ №0001', self.panel()[0])
        self.assertEqual(len(self.records('support_messages')), 1)

    def test_cancelled_preview_cannot_be_published_by_a_delayed_old_confirmation(self):
        self.profile()
        self.message('/support')
        self.press('Вопрос')
        self.message('CANCELLED-PRIVATE-TEXT')
        token = self.db.get_state(420)[1]['token']
        old = [b['callback_data'] for r in self.panel()[1]['inline_keyboard'] for b in r if 'Отправить сообщение' in b['text']][0]
        self.press('Отменить этот черновик')
        self.callback(old)
        self.assertEqual(self.records('support_tickets'), [])
        self.assertEqual(self.db.service_result(420, token)['kind'], 'discarded')
        self.assertNotIn('CANCELLED-PRIVATE-TEXT', '\n'.join(self.db.connection().iterdump()))
        with self.assertRaises(ValueError): self.db.create_ticket(420, 'question', 'CANCELLED-PRIVATE-TEXT', token)
        self.message('/support')
        self.press('Вопрос')
        self.message('SECOND-CANCELLED-TEXT')
        token = self.db.get_state(420)[1]['token']
        self.message('/cancel')
        self.assertEqual(self.db.service_result(420, token)['kind'], 'discarded')
        self.assertIsNone(self.db.get_state(420))

    def test_old_draft_submission_does_not_erase_a_newer_active_draft(self):
        self.profile()
        self.message('/support')
        self.press('Вопрос')
        self.message('Первый текст')
        old = [b['callback_data'] for r in self.panel()[1]['inline_keyboard'] for b in r if 'Отправить сообщение' in b['text']][0]
        self.message('/support')
        self.press('Размер / посадка')
        self.message('Более новый текст')
        current = self.db.get_state(420)
        self.callback(old)
        self.assertEqual(self.db.get_state(420), current)
        self.assertEqual(len(self.records('support_tickets')), 1)
        self.assertEqual(self.records('support_messages')[0]['body'], 'Первый текст')

    def test_expired_draft_is_not_submitted_and_pruning_keeps_real_records(self):
        self.profile()
        tid = self.ticket()
        token = self.db.save_service_draft(420, 'ticket_new', {'topic': 'question', 'body': 'ПРИВАТНЫЙ ЧЕРНОВИК'})
        self.db.connection().execute('UPDATE service_drafts SET expires_at=0')
        with self.assertRaises(ValueError): self.db.service_draft(420, token)
        self.db.prune_operations()
        self.assertEqual(self.records('service_drafts'), [])
        self.assertEqual(self.db.ticket_view(420, tid)['ticket_id'], tid)
        self.assertIsNotNone(self.db.service_result(420, 'new-ticket'))

    def test_pagination_and_html_escaping_do_not_hide_full_messages(self):
        tid = self.ticket(body='<script>alert(1)</script> ' * 40)
        for n in range(7): self.reply(tid, body=f'Сообщение {n} & ещё', operation=f'reply-{n}')
        first = self.db.ticket_view(420, tid)
        second = self.db.ticket_view(420, tid, page=1)
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(len(first['messages']), 4)
        self.assertEqual(len(second['messages']), 4)
        self.callback(f'o:ticket:{tid}:1')
        self.assertNotIn('<script>', self.panel()[0])
        mid = second['messages'][0]['message_id']
        self.callback(f'o:message:{tid}:{mid}:0:0')
        self.assertIn('&lt;script&gt;', self.panel()[0])
        for c in self.api.mock_calls:
            if c[0] not in {'send_message', 'edit_message_text'}: continue
            plain = html.unescape(re.sub('<[^>]*>', '', c.args[-2]))
            self.assertLessEqual(len(plain.encode('utf-16-le')) // 2, 3900)
            for row in c.args[-1]['inline_keyboard']:
                for b in row:
                    if 'callback_data' in b: self.assertLessEqual(len(b['callback_data'].encode()), 64)

    def test_draft_full_preview_has_all_1200_characters_without_publication(self):
        self.profile()
        self.message('/support')
        self.press('Вопрос')
        body = '<' * 1190 + 'FINAL_TEXT'
        self.message(body)
        self.assertEqual(len(body), 1200)
        self.press('Проверить полный текст')
        self.press('Дальше')
        self.press('Дальше')
        self.assertIn('FINAL_TEXT', self.panel()[0])
        self.assertEqual(self.records('support_messages'), [])

    def test_service_api_is_signed_customer_only_and_does_not_accept_staff_flags(self):
        request = self.api_client()
        self.assertEqual(request(valid=False)[0], 401)
        self.assertEqual(request(data={'action': 'ticket_new', 'topic': 'question', 'text': 'Не сохранять', 'operation_id': 'no-consent'})[0], 400)
        self.assertEqual(request(data={'action': 'consent', 'consent': 'true', 'operation_id': 'consent'})[0], 400)
        self.assertEqual(request(data={'action': 'consent', 'consent': True, 'operation_id': 'consent'})[0], 200)
        body = {'action': 'ticket_new', 'topic': 'question', 'text': 'API: собственное обращение', 'operation_id': 'create'}
        status, result = request(data=body)
        self.assertEqual(status, 200)
        tid = result['ticket']['ticket_id']
        self.assertEqual(request(data=body)[1]['ticket']['ticket_id'], tid)
        self.assertEqual(request('?ticket_id=' + str(tid), uid=421)[0], 400)
        self.assertEqual(request('?ticket_id=' + str(tid) + '&staff=1', uid=9001)[0], 400)
        self.assertEqual(request(data={**body, 'staff': True})[0], 400)
        self.assertEqual(request(data={'action': 'quote', 'purchase_id': 1, 'operation_id': 'privileged'}, uid=9001)[0], 400)
        self.db.claim_ticket(9004, tid, 'claim')
        self.reply(tid, 9004, 'СКРЫТАЯ ЗАМЕТКА', staff=True, internal=True)
        status, result = request('?ticket_id=' + str(tid))
        self.assertEqual(status, 200)
        self.assertNotIn('СКРЫТАЯ', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['user_id'], 420)
        reply = {'action': 'ticket_reply', 'ticket_id': tid, 'text': 'Ответ покупателя', 'version': result['ticket']['version'], 'operation_id': 'api-reply'}
        self.assertEqual(request(data=reply)[0], 200)
        self.assertEqual(request(data={**reply, 'operation_id': 'stale'})[0], 409)
        self.assertEqual(request(data={**reply, 'internal': True})[0], 400)
        self.assertEqual(request(data=reply)[0], 200)

    def test_service_api_delivery_is_same_agreement_and_cannot_confirm_for_another_buyer(self):
        request = self.api_client()
        r, pid = self.buy(paid=True, ready=True)
        qid = self.propose(pid)
        status, result = request('?purchase_id=' + str(pid))
        self.assertEqual(status, 200)
        self.assertEqual(result['delivery']['quote']['destination'], self.terms['destination'])
        self.assertEqual(request('?purchase_id=' + str(pid), uid=421)[0], 400)
        body = {'action': 'delivery_answer', 'purchase_id': pid, 'quote_id': qid, 'version': result['delivery']['version'], 'accept': True, 'operation_id': 'api-accept'}
        self.assertEqual(request(data=body, uid=421)[0], 400)
        self.assertEqual(request(data=body)[0], 200)
        self.assertEqual(request(data=body)[0], 200)
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('DB unavailable')):
            self.assertEqual(request(data={'action': 'ticket_new', 'topic': 'question', 'text': 'Unused', 'operation_id': 'safe-no-send'})[0], 200)  # Durable alert intent, not provider I/O.
        self.ship(pid)
        v = self.db.delivery_view(420, pid)['version']
        body = {'action': 'received', 'purchase_id': pid, 'version': v, 'operation_id': 'api-received'}
        self.assertEqual(request(data=body)[0], 200)
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')
        self.assertEqual(self.db.purchase_view(pid, user_id=420)['fulfillment'], 'completed')

    def test_service_api_storage_failure_503_can_retry_identically(self):
        request = self.api_client()
        self.profile()
        body = {'action': 'ticket_new', 'topic': 'question', 'text': 'Retry safely', 'operation_id': 'retry'}
        with patch.object(self.db, '_notify_ticket_team', side_effect=RuntimeError('disk unavailable')):
            self.assertEqual(request(data=body)[0], 503)
        self.assertEqual(self.records('support_messages'), [])
        self.assertEqual(request(data=body)[0], 200)
        self.assertEqual(request(data=body)[0], 200)
        self.assertEqual(len(self.records('support_tickets')), 1)

    def api_client(self):
        server = start_health_server(0, self.catalog, self.settings, self.db, self.api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_address[1])
        def request(query='', uid=420, data=None, valid=True):
            fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
            secret = hmac.new(b'WebAppData', self.settings.token.encode(), hashlib.sha256).digest()
            fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(url + '/api/service' + query, headers={'X-Telegram-Init-Data': urllib.parse.urlencode(fields) if valid else 'invalid', 'Content-Type': 'application/json'}, data=json.dumps(data).encode() if data is not None else None)
            try: response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc: response = exc
            with response:
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                return response.status, json.load(response)
        return request


if __name__ == '__main__':
    unittest.main()
