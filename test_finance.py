"""Offline financial-desk acceptance: evidence, ACL, CAS, privacy and outbox.

All provider events, users, clocks and pre-upgrade rows here are synthetic.
No tests call a provider or execute a refund.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import html
import json
import re
import sqlite3
import time
import unittest
from unittest.mock import patch

from bot import Database
from commerce_store import CartConflict, ROLE_PERMISSIONS
from finance_store import finance_money
import test_commerce as commerce
import test_operations as operations


class FinanceTests(unittest.TestCase):
    checkout = commerce.CommerceTests.checkout
    confirm_stars = commerce.CommerceTests.confirm_stars
    callback = commerce.CommerceTests.callback
    panel = commerce.CommerceTests.panel
    press = commerce.CommerceTests.press
    profile = commerce.CommerceTests.profile
    message = operations.OperationsTests.message
    buy = operations.OperationsTests.buy
    propose = operations.OperationsTests.propose
    accept = operations.OperationsTests.accept
    ship = operations.OperationsTests.ship

    def setUp(self):
        operations.OperationsTests.setUp(self)
        for uid in (9005, 9006):
            self.db.upsert_user({'id': uid, 'first_name': 'Finance fixture'})
            self.db.set_staff_role(9001, uid, 'finance', **self.owner)

    def records(self, table):
        return [dict(r) for r in commerce.CommerceTests.records(self, table)]

    def case_row(self, cid=None):
        rows = self.records('finance_cases')
        return next(r for r in rows if r['case_id'] == cid) if cid else rows[-1]

    def issue(self, charge='finance-mismatch', *, payer=420):
        r = self.checkout()
        self.assertTrue(r['ok'], r)
        self.db.mark_payment_paid(r['payment_id'], 'stars', charge, amount_minor=r['amount_stars'] + 1,
            currency='XTR', payer_id=payer, queue_review=self.bot.notify_payment_review)
        return r, self.case_row()['case_id']

    def unknown(self, charge='finance-orphan', *, method='stars', currency='XTR', amount=123):
        self.db.mark_payment_paid('unbound-private-reference', method, charge,
            amount_minor=amount, currency=currency, payer_id=880099, queue_review=self.bot.notify_payment_review)
        return self.case_row()['case_id']

    def attempt(self):
        r = self.checkout()
        a = self.db.reserve_invoice(r['payment_id'], 'lava', r['amount_rub'] * 100)
        self.assertIn('attempt_id', a)
        # Clock fixture, not a production reconciliation action.
        self.db.connection().execute('UPDATE payment_invoice_attempts SET created_at=? WHERE attempt_id=?', (time.time() - 121, a['attempt_id']))
        self.db.sync_finance()
        return r, a, self.case_row()['case_id']

    def issue_invoice(self, r, a):
        self.db.save_invoice(r['payment_id'], 'lava', 'finance-known-invoice', r['amount_rub'] * 100, 'RUB',
            'https://pay.example.test/finance-invoice', external_ref=a['external_ref'], attempt_id=a['attempt_id'])

    def claim(self, cid, actor=9005, operation='finance-claim'):
        return self.db.claim_finance(actor, cid, self.case_row(cid)['version'], operation, **self.owner)

    def step(self, cid, step, operation='finance-step', actor=9005):
        return self.db.finance_step(actor, cid, step, self.case_row(cid)['version'], operation, **self.owner)

    def schedule(self, cid, hours=1, operation='finance-schedule', actor=9005):
        return self.db.schedule_finance(actor, cid, hours, self.case_row(cid)['version'], operation, **self.owner)

    def money_snapshot(self):
        return {table: self.records(table) for table in ('payments', 'payment_receipts', 'payment_receipt_conflicts',
            'payment_invoices', 'payment_invoice_attempts', 'stock_items', 'stock_reservations', 'stock_movements', 'purchases', 'orders')}

    def finance_notifications(self, kind=None):
        conn = self.db.connection()
        rows = self.records('notifications')
        if not kind: return [r for r in rows if r['notification_id'].startswith('finance:')]
        ids = {r[0] for r in conn.execute('SELECT alert_id FROM finance_alerts WHERE kind=?', (kind,))}
        return [r for r in rows if r['notification_id'].startswith('finance:') and int(r['notification_id'].split(':')[1]) in ids]

    def test_empty_migration_is_idempotent_and_does_not_invent_money(self):
        before = self.money_snapshot()
        self.db.init_finance_store(); self.db.init_finance_store(); self.db.sync_finance()
        self.assertEqual(self.money_snapshot(), before)
        self.assertEqual(self.records('finance_cases'), [])
        summary = self.db.finance_summary(9005)
        self.assertEqual(summary['totals'], [])
        self.assertIsNone(summary['executed_refunds'])
        self.assertEqual(self.db.connection().execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(list(self.db.connection().execute('PRAGMA foreign_key_check')), [])

    def test_clean_payment_has_history_but_no_exception_task(self):
        r, pid = self.buy(paid=True)
        self.db.sync_finance()
        self.assertEqual(self.records('finance_cases'), [])
        history = self.db.customer_finance(420, pid)
        self.assertEqual(len(history['receipts']), 1)
        self.assertEqual(history['receipts'][0]['status'], 'applied')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')
        self.assertIsNone(history['fiscal_document'])
        self.assertIsNone(history['executed_refunds'])

    def test_unknown_receipt_is_transactional_and_not_lost_without_purchase(self):
        cid = self.unknown()
        c = self.db.finance_case_view(9005, cid)
        self.assertTrue(c['active'])
        self.assertIsNone(c['source']['purchase_id'])
        self.assertIsNone(c['source']['owner_id'])
        self.assertIn('unknown_payment', c['source']['reasons'])
        self.assertFalse(c['can_note']); self.assertFalse(c['can_close'])
        before = (len(self.records('finance_journal')), len(self.records('finance_alerts')))
        self.db.mark_payment_paid('unbound-private-reference', 'stars', 'finance-orphan', amount_minor=123, currency='XTR', payer_id=880099)
        self.db.sync_finance()
        self.assertEqual(len(self.records('finance_cases')), 1)
        self.assertEqual((len(self.records('finance_journal')), len(self.records('finance_alerts'))), before)

    def test_finance_role_is_separate_from_manager_support_and_warehouse(self):
        _, cid = self.issue()
        for actor in (420, 421, 9002, 9003, 9004):
            for read in (lambda: self.db.finance_case_view(actor, cid), lambda: self.db.finance_receipts(actor),
                         lambda: self.db.finance_summary(actor), lambda: self.db.claim_finance(actor, cid, 0, 'denied')):
                with self.assertRaises(PermissionError): read()
        self.assertEqual(ROLE_PERMISSIONS['finance'], {'orders.read', 'finance.read', 'finance.work'})
        self.db.finance_case_view(9001, cid, **self.owner)
        self.db.finance_case_view(9005, cid)

    def test_finance_cannot_pack_ship_change_stock_roles_or_read_support(self):
        r, pid = self.buy(paid=True)
        self.assertNotIn('customer', self.db.purchase_view(pid, actor_id=9005))
        for call in (lambda: self.db.claim_purchase(9005, pid), lambda: self.db.advance_purchase(9005, pid, 'packing', 0),
                     lambda: self.db.set_stock(9005, 1, 100, 0), lambda: self.db.delivery_view(9005, pid, staff=True),
                     lambda: self.db.tickets_list(9005, staff=True), lambda: self.db.set_staff_role(9005, 9006, 'manager')):
            with self.assertRaises(PermissionError): call()
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')

    def test_every_financial_work_action_leaves_ledger_inventory_and_shipping_unchanged(self):
        r, cid = self.issue()
        before = self.money_snapshot()
        self.claim(cid); self.schedule(cid); self.step(cid, 'waiting')
        self.db.add_finance_note(9005, cid, 'Проверены сведения. Денежный результат ещё неизвестен.', self.case_row(cid)['version'], 'internal-note')
        self.step(cid, 'checked', 'checked'); self.step(cid, 'escalated', 'escalated')
        self.db.queue_finance_alerts(self.settings.admin_ids, self.settings.manager_chat_id)
        with self.assertRaises(ValueError): self.step(cid, 'closed', 'forbidden-close')
        self.assertEqual(self.money_snapshot(), before)
        self.assertTrue(self.db.payment_attention(r['payment_id']))
        self.assertFalse(self.db.payment_is_payable(r['payment_id']))
        self.assertTrue(self.records('finance_journal')); self.assertTrue(self.records('finance_alerts'))

    def test_receipt_conflict_invalidates_prepared_action_without_counting_another_payment(self):
        r, cid = self.issue()
        self.claim(cid)
        old = self.case_row(cid)['version']
        receipt = self.records('payment_receipts')[0]
        self.db.mark_payment_paid(r['payment_id'], 'stars', receipt['provider_id'],
            amount_minor=receipt['amount_minor'] + 5, currency='XTR', payer_id=420)
        self.assertGreater(self.case_row(cid)['version'], old)
        with self.assertRaises(CartConflict): self.db.schedule_finance(9005, cid, 1, old, 'stale-schedule')
        self.assertEqual(len(self.records('payment_receipts')), 1)
        self.assertEqual(len(self.records('payment_receipt_conflicts')), 1)
        self.assertEqual(self.records('payment_receipts')[0], receipt)
        self.assertIsNone(self.case_row(cid)['due_at'])

    def test_paid_original_with_conflicting_repeat_is_an_active_unclosable_case(self):
        r, _ = self.buy(paid=True)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'operations-charge', amount_minor=r['amount_stars'] + 7, currency='XTR', payer_id=421)
        cid = self.case_row()['case_id']
        self.assertEqual(self.db.finance_case_view(9005, cid)['source']['status'], 'applied')
        self.assertTrue(self.case_row(cid)['active'])
        self.claim(cid)
        with self.assertRaises(ValueError): self.step(cid, 'closed')
        self.assertTrue(self.db.payment_attention(r['payment_id']))

    def test_claim_race_has_one_assignee_and_audited_transition(self):
        _, cid = self.issue()
        version = self.case_row(cid)['version']
        def work(actor):
            try:
                self.db.claim_finance(actor, cid, version, 'parallel-claim')
                return True
            except (ValueError, PermissionError): return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(work, (9005, 9006)))
        self.assertEqual(sum(outcomes), 1)
        self.assertIn(self.case_row(cid)['assigned_to'], (9005, 9006))
        self.assertEqual(len([r for r in self.records('finance_journal') if r['kind'] == 'claimed']), 1)

    def test_mutations_are_idempotent_and_key_reuse_with_different_body_is_rejected(self):
        _, cid = self.issue()
        version = self.case_row(cid)['version']
        self.db.claim_finance(9005, cid, version, 'repeat-claim')
        before = copy.deepcopy(self.case_row(cid))
        self.db.claim_finance(9005, cid, version, 'repeat-claim')
        self.assertEqual(self.case_row(cid), before)
        version = before['version']
        self.db.add_finance_note(9005, cid, 'Приватная запись', version, 'repeat-note')
        self.db.add_finance_note(9005, cid, 'Приватная запись', version, 'repeat-note')
        with self.assertRaises(ValueError): self.db.add_finance_note(9005, cid, 'Другое содержание', version, 'repeat-note')
        self.assertEqual(len(self.records('finance_notes')), 1)
        self.assertIsNone(self.db.service_result(9006, 'repeat-note'))

    def test_work_requires_assignee_or_owner_and_release_checks_version(self):
        _, cid = self.issue()
        with self.assertRaises(PermissionError): self.step(cid, 'waiting')
        self.claim(cid)
        for call in (lambda: self.step(cid, 'waiting', actor=9006), lambda: self.schedule(cid, actor=9006),
                     lambda: self.db.add_finance_note(9006, cid, 'Без назначения', self.case_row(cid)['version'], 'alien-note'),
                     lambda: self.db.claim_finance(9006, cid, self.case_row(cid)['version'], 'alien-release', release=True)):
            with self.assertRaises(PermissionError): call()
        with self.assertRaises(CartConflict): self.db.claim_finance(9005, cid, 0, 'stale-release', release=True)
        self.db.claim_finance(9001, cid, self.case_row(cid)['version'], 'owner-release', release=True, **self.owner)
        self.assertIsNone(self.case_row(cid)['assigned_to'])
        self.assertTrue(self.case_row(cid)['active'])

    def test_role_revocation_unassigns_preserves_deadline_and_blocks_replay(self):
        _, cid = self.issue(); self.claim(cid); self.schedule(cid)
        before = self.case_row(cid)
        self.db.set_staff_role(9001, 9005, 'support', **self.owner)
        after = self.case_row(cid)
        self.assertIsNone(after['assigned_to'])
        self.assertEqual(after['due_at'], before['due_at'])
        self.assertGreater(after['due_generation'], before['due_generation'])
        with self.assertRaises(PermissionError): self.db.claim_finance(9005, cid, 0, 'finance-claim')
        with self.assertRaises(PermissionError): self.db.finance_case_view(9005, cid)
        self.assertTrue(any(r['kind'] == 'role_revoked' for r in self.records('finance_journal')))

    def test_note_requires_consent_bound_owner_and_cannot_promote_unknown_charge(self):
        cid = self.unknown(); self.claim(cid)
        with self.assertRaises(ValueError): self.db.add_finance_note(9005, cid, 'Ручная привязка недопустима', self.case_row(cid)['version'], 'bad-note')
        with self.assertRaises(ValueError): self.db.save_service_draft(9005, 'finance_note', {'case_id': cid, 'version': self.case_row(cid)['version']})
        r, other = self.issue(); self.claim(other, operation='other-claim')
        self.db.connection().execute('UPDATE users SET consent_at=NULL WHERE user_id=420')
        with self.assertRaises(ValueError): self.db.add_finance_note(9005, other, 'Без согласия нельзя', self.case_row(other)['version'], 'no-consent')
        self.assertEqual(self.records('finance_notes'), [])

    def test_unbound_external_invoice_or_wrong_stars_payer_disallows_free_note(self):
        r, cid = self.issue(payer=421)
        self.assertFalse(self.db.finance_case_view(9005, cid)['can_note'])
        self.db.mark_payment_paid(r['payment_id'], 'lava', 'unissued-lava', amount_minor=r['amount_rub'] * 100, currency='RUB')
        self.assertFalse(self.db.finance_case_view(9005, self.case_row()['case_id'])['can_note'])

    def test_private_note_only_in_private_draft_and_note_table_not_general_records(self):
        _, cid = self.issue(); self.claim(cid)
        secret = 'ПРИВАТНЫЙ-ФИН-ТЕКСТ & <b>не разметка</b>'
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message(secret, 9005)
        self.assertEqual(self.records('finance_notes'), [])
        self.press('Сохранить только заметку', 9005)
        self.assertEqual(self.records('finance_notes')[0]['body'], secret)
        for table in ('states', 'chat_actions', 'staff_audit', 'events', 'finance_journal', 'service_requests', 'notifications'):
            self.assertNotIn(secret, json.dumps(self.records(table), ensure_ascii=False), table)
        for actor in (420, 421, 9002, 9003, 9004):
            with self.assertRaises(PermissionError): self.db.finance_note_view(actor, self.records('finance_notes')[0]['note_id'])
        self.assertEqual(self.db.finance_note_view(9005, 1)['body'], secret)

    def test_finance_audit_failure_rolls_back_task_request_note_and_alert(self):
        _, cid = self.issue()
        before = {t: self.records(t) for t in ('finance_cases', 'finance_journal', 'finance_alerts', 'staff_audit', 'service_requests')}
        with patch.object(self.db, 'audit_staff', side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): self.claim(cid)
        self.assertEqual({t: self.records(t) for t in before}, before)
        self.claim(cid)
        version = self.case_row(cid)['version']
        with patch.object(self.db, '_service_done', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.db.add_finance_note(9005, cid, 'Не будет записано наполовину', version, 'atomic-note')
        self.assertEqual(self.records('finance_notes'), [])
        self.assertEqual(self.case_row(cid)['version'], version)

    def test_financial_evidence_and_case_creation_rollback_together(self):
        r = self.checkout()
        before = self.money_snapshot()
        with patch.object(self.db, '_finance_event', side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.mark_payment_paid(r['payment_id'], 'stars', 'atomic-receipt', amount_minor=r['amount_stars'] + 1, currency='XTR', payer_id=420)
        self.assertEqual(self.money_snapshot(), before)
        self.assertEqual(self.records('finance_cases'), [])
        self.assertEqual(self.records('finance_alerts'), [])

    def test_stalled_attempt_is_not_money_and_cannot_be_reissued_or_closed_by_staff(self):
        r, a, cid = self.attempt()
        c = self.db.finance_case_view(9005, cid)
        self.assertEqual(c['source_kind'], 'attempt'); self.assertTrue(c['active'])
        self.assertIsNone(c['source']['amount_minor'])
        self.assertEqual(self.db.finance_summary(9005)['totals'], [])
        self.claim(cid)
        with self.assertRaises(ValueError): self.step(cid, 'closed')
        self.assertIsNone(self.db.reserve_invoice(r['payment_id'], 'lava', r['amount_rub'] * 100))
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'pending')

    def test_young_creation_is_not_discovered_and_unknown_terminal_never_closes(self):
        r = self.checkout()
        a = self.db.reserve_invoice(r['payment_id'], 'lava', r['amount_rub'] * 100)
        self.db.sync_finance(); self.assertEqual(self.records('finance_cases'), [])
        self.db.connection().execute("UPDATE payment_invoice_attempts SET status='uncertain' WHERE attempt_id=?", (a['attempt_id'],))
        self.db.sync_finance(); cid = self.case_row()['case_id']; self.claim(cid)
        for status in ('creating', 'issued', 'failed', 'unknown-future-status'):
            # Simulated damaged/imported source: a word alone isn't a saved invoice.
            self.db.connection().execute('UPDATE payment_invoice_attempts SET status=? WHERE attempt_id=?', (status, a['attempt_id']))
            c = self.db.finance_case_view(9005, cid)
            self.assertTrue(c['active'], status)
            with self.assertRaises(ValueError): self.step(cid, 'closed', 'bad-terminal-' + status)

    def test_real_invoice_save_changes_cas_allows_clerical_close_but_never_pays(self):
        r, a, cid = self.attempt(); self.claim(cid); self.schedule(cid)
        old_version = self.case_row(cid)['version']
        self.issue_invoice(r, a)
        c = self.db.finance_case_view(9005, cid)
        self.assertTrue(c['can_close']); self.assertFalse(c['active'])
        with self.assertRaises(CartConflict): self.db.finance_step(9005, cid, 'closed', old_version, 'stale-close')
        self.step(cid, 'closed')
        self.assertEqual(self.case_row(cid)['state'], 'closed')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'pending')
        self.assertEqual(self.records('payment_receipts'), [])
        self.assertIsNone(self.db.finance_summary(9005)['executed_refunds'])

    def test_closing_invoice_task_cannot_clear_related_money_exception(self):
        r, a, cid = self.attempt(); self.claim(cid)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'separate-mismatch', amount_minor=r['amount_stars'] + 1, currency='XTR', payer_id=420)
        self.issue_invoice(r, a)
        self.step(cid, 'closed')
        self.assertTrue(self.db.payment_attention(r['payment_id']))
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'review_required')
        self.assertFalse(self.db.payment_is_payable(r['payment_id']))
        self.assertTrue(any(r['active'] for r in self.records('finance_cases') if r['source_kind'] == 'receipt'))

    def test_new_evidence_reopens_task_without_inheriting_assignment_or_old_deadline(self):
        r, a, cid = self.attempt(); self.claim(cid); self.schedule(cid); self.issue_invoice(r, a)
        self.step(cid, 'closed')
        old = self.case_row(cid)
        self.db.connection().execute("UPDATE payment_invoice_attempts SET status='uncertain' WHERE attempt_id=?", (a['attempt_id'],))
        self.db.sync_finance()
        c = self.case_row(cid)
        self.assertEqual(c['state'], 'open'); self.assertTrue(c['active'])
        self.assertIsNone(c['assigned_to']); self.assertIsNone(c['due_at'])
        self.assertGreater(c['version'], old['version'])
        with self.assertRaises(ValueError): self.db.finance_step(9001, cid, 'closed', c['version'], 'reopened-close', **self.owner)

    def test_no_deadline_no_reminders_then_explicit_due_and_owner_escalation(self):
        _, cid = self.issue(); self.claim(cid)
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=time.time() + 999999)
        self.assertEqual(self.finance_notifications('due'), [])
        self.assertEqual(self.finance_notifications('overdue_owner'), [])
        self.schedule(cid); due = self.case_row(cid)['due_at']
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due - 0.1)
        self.assertEqual(self.finance_notifications('due'), [])
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 0.1)
        self.assertEqual([n['chat_id'] for n in self.finance_notifications('due')], [9005])
        self.assertEqual(self.finance_notifications('overdue_owner'), [])
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 3601)
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 3602)
        self.assertEqual([n['chat_id'] for n in self.finance_notifications('overdue_owner')], [9001])
        self.assertEqual(len(self.finance_notifications('due')), 1)
        self.step(cid, 'escalated', 'manual-escalation')
        self.db.queue_finance_alerts(self.settings.admin_ids)
        escalation = self.finance_notifications('escalation')[0]
        self.step(cid, 'working', 'continue-investigation')
        self.assertFalse(self.db.finance_notification_current(escalation['notification_id'], 9001, self.settings.admin_ids))

    def test_reschedule_suppresses_already_queued_retry_and_uses_new_generation(self):
        _, cid = self.issue(); self.claim(cid); self.schedule(cid)
        old = self.case_row(cid); self.db.queue_finance_alerts(self.settings.admin_ids, stamp=old['due_at'] + 1)
        pending = self.finance_notifications('due')[0]
        self.assertTrue(self.db.finance_notification_current(pending['notification_id'], 9005, self.settings.admin_ids, stamp=old['due_at'] + 1))
        self.schedule(cid, 4, 'postpone')
        self.assertFalse(self.db.finance_notification_current(pending['notification_id'], 9005, self.settings.admin_ids, stamp=old['due_at'] + 1))
        self.api.reset_mock()
        self.bot.flush_notifications(now=old['due_at'] + 1, key=pending['notification_id'])
        self.api.send_message.assert_not_called()
        current = self.case_row(cid)
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=current['due_at'] + 1)
        self.assertEqual(len(self.finance_notifications('due')), 2)
        self.assertGreater(current['due_generation'], old['due_generation'])

    def test_deadline_clear_close_and_role_revoke_each_cancel_stale_reminders(self):
        for mode in ('clear', 'close', 'revoke'):
            with self.subTest(mode=mode):
                # Separate operation streams per subtest; each pending attempt is unique.
                self.payload['request_id'] = 'reminder-' + mode
                if mode == 'revoke': self.db.set_staff_role(9001, 9005, 'finance', **self.owner)
                r, a, cid = self.attempt(); self.claim(cid, operation='claim-' + mode); self.schedule(cid, operation='schedule-' + mode)
                due = self.case_row(cid)['due_at']
                self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 1)
                notification = self.finance_notifications('due')[-1]
                if mode == 'clear': self.schedule(cid, 0, 'clear-deadline')
                elif mode == 'close': self.issue_invoice(r, a); self.step(cid, 'closed', 'close-for-reminder')
                else: self.db.set_staff_role(9001, 9005, 'none', **self.owner)
                self.assertFalse(self.db.finance_notification_current(notification['notification_id'], 9005, self.settings.admin_ids, stamp=due + 2))

    def test_release_and_reassignment_redirect_scheduled_alert_without_moving_deadline(self):
        _, cid = self.issue(); self.claim(cid); self.schedule(cid)
        due = self.case_row(cid)['due_at']
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 1)
        old_notice = self.finance_notifications('due')[0]
        self.db.claim_finance(9005, cid, self.case_row(cid)['version'], 'release', release=True)
        self.claim(cid, actor=9006, operation='new-responsible')
        self.assertEqual(self.case_row(cid)['due_at'], due)
        self.assertFalse(self.db.finance_notification_current(old_notice['notification_id'], 9005, self.settings.admin_ids, stamp=due + 1))
        self.db.queue_finance_alerts(self.settings.admin_ids, stamp=due + 2)
        self.assertEqual(self.finance_notifications('due')[-1]['chat_id'], 9006)

    def test_outbox_intent_to_notification_is_atomic_and_retries_without_new_alert(self):
        _, cid = self.issue(); self.claim(cid)
        before = copy.deepcopy(self.records('finance_alerts'))
        with patch.object(self.db, 'enqueue_message', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.db.queue_finance_alerts(self.settings.admin_ids)
        self.assertEqual(self.records('finance_alerts'), before)
        self.assertEqual(self.finance_notifications(), [])
        self.db.queue_finance_alerts(self.settings.admin_ids)
        after = copy.deepcopy(self.finance_notifications())
        self.db.queue_finance_alerts(self.settings.admin_ids)
        self.assertEqual(self.finance_notifications(), after)
        n = after[0]
        self.api.send_message.side_effect = RuntimeError('Telegram unavailable')
        self.bot.flush_notifications(key=n['notification_id'])
        self.assertIsNone(next(x for x in self.finance_notifications() if x['notification_id'] == n['notification_id'])['delivered_at'])
        self.api.send_message.side_effect = None
        self.bot.flush_notifications(now=time.time() + 7200, key=n['notification_id'])
        self.assertIsNotNone(next(x for x in self.finance_notifications() if x['notification_id'] == n['notification_id'])['delivered_at'])

    def test_no_roles_or_chat_preserves_intent_until_a_route_exists(self):
        cid = self.unknown()
        self.db.connection().execute("DELETE FROM staff_roles WHERE role='finance'")
        self.db.queue_finance_alerts()
        self.assertTrue(all(r['queued_at'] is None for r in self.records('finance_alerts')))
        self.db.queue_finance_alerts(manager_chat_id=-10055)
        notice = self.finance_notifications()[0]
        self.assertEqual(notice['chat_id'], -10055)
        self.assertTrue(self.db.finance_notification_current(notice['notification_id'], -10055, manager_chat_id=-10055))
        self.assertFalse(self.db.finance_notification_current(notice['notification_id'], -9999, manager_chat_id=-10055))
        self.assertNotIn('unbound-private-reference', notice['body'])
        self.assertNotIn('finance-orphan', notice['body'])

    def test_receipt_review_and_invoice_alerts_do_not_leak_raw_ids_or_amounts_to_group(self):
        r, cid = self.issue(charge='VERY-PRIVATE-PROVIDER-ID')
        self.db.queue_finance_alerts(self.settings.admin_ids, -10055)
        for n in self.records('notifications'):
            self.assertNotIn('VERY-PRIVATE-PROVIDER-ID', n['body'])
            self.assertNotIn(r['payment_id'], n['body'])
        self.callback(f'f:receipt:{self.records("payment_receipts")[0]["receipt_id"]}', 9005)
        self.press('Реквизиты источника', 9005)
        self.assertIn('VERY-PRIVATE-PROVIDER-ID', self.panel()[0])

    def test_summary_groups_currency_method_status_counts_conflicts_once_and_excludes_legacy(self):
        r, _ = self.buy(paid=True)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'operations-charge', amount_minor=r['amount_stars'] + 5, currency='XTR', payer_id=420)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'another-charge', amount_minor=r['amount_stars'], currency='XTR', payer_id=420)
        self.unknown('rub-receipt', method='lava', currency='RUB', amount=10050)
        self.unknown('usd-receipt', method='crypto', currency='USD', amount=123)
        # Pre-upgrade imported evidence is deliberately outside verified-event totals.
        self.db.connection().execute("""INSERT INTO payment_receipts(method,provider_id,payment_id,claimed_ref,amount_minor,currency,status,reason,source,received_at)
            VALUES ('stars','imported-record',?,?,999999,'XTR','legacy_unreconciled','imported_legacy','legacy','2020-01-01')""", (r['payment_id'], r['payment_id']))
        report = self.db.finance_summary(9005)
        totals = {(t['method'], t['currency'], t['status']): t for t in report['totals']}
        self.assertEqual(totals[('stars', 'XTR', 'disputed')]['amount_minor'], r['amount_stars'])
        self.assertEqual(totals[('stars', 'XTR', 'refund_required')]['count'], 1)
        self.assertEqual(totals[('lava', 'RUB', 'review_required')]['amount_label'], '100,50 ₽')
        self.assertEqual(totals[('crypto', 'USD', 'review_required')]['amount_label'], '123 мин. ед. USD')
        self.assertEqual(sum(t['count'] for t in totals.values()), 4)
        self.assertEqual(report['legacy_records'], 1)
        self.assertIsNone(report['executed_refunds'])
        self.assertNotIn('net_income_rub', report)

    def test_money_format_never_guesses_unknown_currency_exponent_or_rounds(self):
        for amount, currency, expected in [(-1, 'RUB', '-0,01 ₽'), (1, 'RUB', '0,01 ₽'), (35050, 'RUB', '350,50 ₽'), (5, 'XTR', '5 XTR'), (100, 'UNK', '100 мин. ед. UNK')]:
            self.assertEqual(finance_money(amount, currency), expected)
        self.assertEqual(finance_money(None, 'RUB'), 'сумма не подтверждена')
        self.assertIn('мин. ед.', finance_money(9000000000000000, 'USD'))

    def test_customer_history_is_ownership_safe_even_for_finance_staff(self):
        r, pid = self.buy(paid=True)
        history = self.db.customer_finance(420, pid)
        for actor in (421, 9001, 9005):
            with self.assertRaises(ValueError): self.db.customer_finance(actor, pid)
        raw = json.dumps(history, ensure_ascii=False)
        for private in ('provider_id', 'payer_id', 'claimed_ref', 'operations-charge', r['payment_id'], '+7999', 'conflicts'):
            self.assertNotIn(private, raw)

    def test_customer_history_never_discloses_other_payer_or_unbound_external_event(self):
        r, _ = self.issue(payer=421)
        self.db.mark_payment_paid(r['payment_id'], 'lava', 'unbound-private-invoice', amount_minor=r['amount_rub'] * 100, currency='RUB')
        h = self.db.customer_finance(420, r['purchase_id'])
        self.assertEqual(h['receipts'], [])
        self.assertTrue(h['attention'])
        self.db.save_invoice(r['payment_id'], 'lava', 'unbound-private-invoice', r['amount_rub'] * 100, 'RUB', 'https://pay.example.test/bound')
        h = self.db.customer_finance(420, r['purchase_id'])
        self.assertEqual(len(h['receipts']), 1)
        self.assertEqual(h['receipts'][0]['method'], 'lava')
        self.assertNotIn('unbound-private-invoice', json.dumps(h))

    def test_customer_conflict_history_shows_generic_review_not_foreign_observation(self):
        r, pid = self.buy(paid=True)
        self.db.mark_payment_paid('FOREIGN-CLAIM-NOT-FOR-CUSTOMER', 'stars', 'operations-charge', amount_minor=777777, currency='USD', payer_id=880099)
        h = self.db.customer_finance(420, pid)
        self.assertEqual(h['receipts'][0]['status'], 'review_required')
        self.assertEqual(h['receipts'][0]['currency'], 'XTR')
        for private in ('FOREIGN-CLAIM', '777777', '880099', 'USD'):
            self.assertNotIn(private, json.dumps(h))

    def test_customer_history_pagination_and_manual_state_are_honest(self):
        r = self.checkout()
        self.db.mark_payment_paid(r['payment_id'])
        h = self.db.customer_finance(420, r['purchase_id'])
        self.assertTrue(h['manual']); self.assertEqual(h['receipts'], [])
        for i in range(7): self.db.mark_payment_paid(r['payment_id'], 'stars', 'manual-extra-' + str(i), amount_minor=r['amount_stars'], currency='XTR', payer_id=420)
        first = self.db.customer_finance(420, r['purchase_id'])
        second = self.db.customer_finance(420, r['purchase_id'], 1)
        self.assertEqual(len(first['receipts']), 4); self.assertTrue(first['has_more'])
        self.assertEqual(len(second['receipts']), 3); self.assertFalse(second['has_more'])
        self.assertFalse({r['number'] for r in first['receipts']} & {r['number'] for r in second['receipts']})

    def test_inbox_error_task_ignores_retry_counters_but_tracks_actual_processing(self):
        r = self.checkout()
        self.db.enqueue_payment_update({'update_id': 71000, 'message': {'from': {'id': 420}, 'chat': {'id': 420},
            'successful_payment': {'invoice_payload': r['payment_id'], 'telegram_payment_charge_id': 'inbox-fin-charge', 'currency': 'XTR', 'total_amount': r['amount_stars']}}})
        with patch.object(self.bot, 'handle_successful_payment', side_effect=sqlite3.OperationalError('disk')):
            self.bot.flush_payment_updates()
        c = self.case_row(); self.assertEqual(c['source_kind'], 'inbox'); cid = c['case_id']; self.claim(cid)
        version = self.case_row(cid)['version']
        self.db.connection().execute('UPDATE payment_inbox SET attempts=attempts+100,next_attempt_at=0 WHERE update_id=71000')
        self.db.sync_finance(); self.assertEqual(self.case_row(cid)['version'], version)
        self.bot.flush_payment_updates()
        after = self.db.finance_case_view(9005, cid)
        self.assertFalse(after['active']); self.assertTrue(after['can_close'])
        self.assertGreater(after['version'], version)
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'paid')
        self.step(cid, 'closed')
        self.assertEqual(len(self.records('payment_receipts')), 1)

    def test_clearing_inbox_error_text_does_not_prove_it_was_processed(self):
        self.db.enqueue_payment_update({'update_id': 71001, 'message': {'from': {'id': 420}, 'chat': {'id': 420}, 'successful_payment': {'invoice_payload': 'unknown'}}})
        self.db.connection().execute("UPDATE payment_inbox SET last_error='ValueError' WHERE update_id=71001")
        self.db.sync_finance(); cid = self.case_row()['case_id']; self.claim(cid)
        self.db.connection().execute("UPDATE payment_inbox SET last_error='' WHERE update_id=71001")
        self.db.finance_case_view(9005, cid)
        with self.assertRaises(ValueError): self.step(cid, 'closed')

    def test_preupgrade_receipts_discovered_in_bounded_batches_with_no_rewriting(self):
        # Old evidence whose workspace predates this financial module.
        for i in range(7):
            self.db.connection().execute("""INSERT INTO payment_receipts(method,provider_id,payment_id,claimed_ref,amount_minor,currency,status,reason,source,received_at)
                VALUES ('lava',?,'old-unknown','old-unknown',123,'UNK','review_required','unknown_payment','verified_event','2020-01-01')""", ('old-' + str(i),))
        before = self.money_snapshot()
        self.db.sync_finance(limit=2); self.assertEqual(len(self.records('finance_cases')), 2)
        for _ in range(5): self.db.sync_finance(limit=2)
        self.assertEqual(len(self.records('finance_cases')), 7)
        self.assertEqual(self.money_snapshot(), before)
        restored = Database(self.db.path); self.addCleanup(restored.close_current)
        restored.sync_finance()
        self.assertEqual(restored.connection().execute('SELECT COUNT(*) FROM finance_cases').fetchone()[0], 7)
        self.assertEqual(list(restored.connection().execute('PRAGMA foreign_key_check')), [])

    def test_legacy_import_not_relabelled_as_verified_money(self):
        r = self.checkout()
        # Simulate pre-ledger local paid state, not a staff UI operation.
        self.db.connection().execute("UPDATE payments SET status='paid',method='stars',provider_id='pre-upgrade' WHERE payment_id=?", (r['payment_id'],))
        self.db.init_payment_store(); self.db.sync_finance()
        report = self.db.finance_summary(9005)
        self.assertEqual(report['totals'], []); self.assertEqual(report['legacy_records'], 1)
        h = self.db.customer_finance(420, r['purchase_id'])
        self.assertTrue(h['has_legacy_records']); self.assertEqual(h['receipts'], [])
        self.assertTrue(self.case_row()['active'])

    def test_paid_order_cancel_materialises_refund_case_without_executing_refund(self):
        r, _ = self.buy(paid=True)
        order = self.records('orders')[0]
        self.db.set_order_status(order['id'], 'cancelled')
        self.assertEqual(self.case_row()['source_kind'], 'receipt')
        self.assertTrue(self.case_row()['active'])
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'], 'refund_required')
        self.assertIsNone(self.db.finance_summary(9005)['executed_refunds'])

    def test_fallback_payment_without_receipt_has_its_own_guarded_task(self):
        r = self.checkout()
        self.db.mark_payment_paid(r['payment_id'])
        self.db.set_order_status(self.records('orders')[0]['id'], 'cancelled')
        c = self.case_row(); self.assertEqual(c['source_kind'], 'payment')
        self.assertEqual(self.records('payment_receipts'), [])
        self.claim(c['case_id'])
        with self.assertRaises(ValueError): self.step(c['case_id'], 'closed')
        self.assertEqual(self.db.finance_summary(9005)['totals'], [])

    def test_sensitive_delivery_remains_blocked_after_finance_task_step(self):
        r, pid = self.buy(paid=True, ready=True)
        self.propose(pid); self.accept(pid)
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'shipping-extra-charge', amount_minor=r['amount_stars'], currency='XTR', payer_id=420)
        cid = self.case_row()['case_id']; self.claim(cid); self.step(cid, 'checked')
        self.assertFalse(self.db.delivery_view(9003, pid, staff=True)['can_dispatch'])
        with self.assertRaises(ValueError): self.ship(pid)
        self.assertEqual(self.db.delivery_view(420, pid)['state'], 'not_sent')

    def test_native_finance_command_role_grant_search_and_customer_entry(self):
        self.message('/role 9006 finance', 9001)
        self.press('Подтвердить права', 9001)
        self.message('/team', 9006); self.press('Финансы · поступления', 9006)
        self.assertIn('ФИНАНСЫ / ПУЛЬТ', self.panel()[0])
        r, cid = self.issue()
        self.message(f'/finance case {cid}', 9006)
        self.assertIn(f'F{cid:04d}', self.panel()[0])
        self.message('/finance receipt 1', 9006); self.assertIn('R0001', self.panel()[0])
        self.message(f'/finance purchase {r["purchase_id"]}', 9006)
        self.assertIn('Журнал записанных поступлений', self.panel()[0])
        self.callback(f'c:purchase:{r["purchase_id"]}', 420); self.press('История оплаты', 420)
        self.assertIn('ПОКУПКА / ИСТОРИЯ ОПЛАТЫ', self.panel()[0])

    def test_private_chat_guard_and_forged_foreign_actions(self):
        _, cid = self.issue()
        self.api.reset_mock(); self.callback(f'f:case:{cid}', 9005, private=False)
        self.api.send_message.assert_not_called()
        self.assertIn('личных', self.api.answer_callback.call_args.args[-1])
        self.callback(f'f:case:{cid}', 9005)
        token = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Взять финансовый' in b['text'])
        self.callback(token, 9006)
        self.assertIsNone(self.case_row(cid)['assigned_to'])
        self.callback(f'f:case:{cid}', 9003); self.assertIn('Нет прав', self.panel()[0])
        self.assertNotIn('finance-mismatch', self.panel()[0])

    def test_stale_native_note_confirmation_after_evidence_change_never_writes(self):
        r, cid = self.issue(); self.claim(cid)
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message('Старый текст сверки', 9005)
        old = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Сохранить только' in b['text'])
        self.db.mark_payment_paid(r['payment_id'], 'stars', 'finance-mismatch', amount_minor=r['amount_stars'] + 8, currency='XTR', payer_id=420)
        self.callback(old, 9005)
        self.assertIn('изменились', self.panel()[0])
        self.assertEqual(self.records('finance_notes'), [])

    def test_native_note_pause_resume_cancel_and_cancelled_replay(self):
        _, cid = self.issue(); self.claim(cid)
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005)
        self.message('/menu', 9005); self.message('THIS MUST NOT BE SAVED WHILE PAUSED', 9005)
        self.assertNotIn('THIS MUST NOT', json.dumps(self.records('service_drafts')))
        self.message('/resume', 9005); self.message('Проверяем, деньги не меняем', 9005)
        old = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Сохранить только' in b['text'])
        self.message('/cancel', 9005)
        self.assertIsNone(self.db.get_state(9005))
        self.callback(old, 9005)
        self.assertIn('отменён', self.panel()[0])
        self.assertEqual(self.records('finance_notes'), [])

    def test_old_note_send_does_not_delete_newer_draft_and_replay_is_safe(self):
        _, cid = self.issue(); self.claim(cid)
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message('Первый приватный текст', 9005)
        old = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Сохранить только' in b['text'])
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message('Более новый текст', 9005)
        newest = self.db.get_state(9005)[1]['token']
        self.callback(old, 9005)
        self.assertEqual(self.db.get_state(9005)[1]['token'], newest)
        self.assertEqual(self.records('finance_notes')[0]['body'], 'Первый приватный текст')
        self.callback(old, 9005); self.assertEqual(len(self.records('finance_notes')), 1)
        self.db.set_staff_role(9001, 9005, 'none', **self.owner)
        self.callback(old, 9005); self.assertIn('Нет прав', self.panel()[0])
        self.assertEqual(len(self.records('finance_notes')), 1)

    def test_native_long_note_and_provider_fields_are_complete_escaped_paginated(self):
        _, cid = self.issue(charge='provider-' + '&<>' * 150)
        self.claim(cid)
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005)
        body = 'Начало <script> & ' + '😀&<>' * 190 + ' КОНЕЦ'
        self.assertLessEqual(len(body), 800)
        self.message(body, 9005)
        self.assertNotIn('<script>', self.panel()[0])
        self.assertIn('фрагмент 1/3', self.panel()[0])
        self.press('Проверить дальше', 9005); self.press('Проверить дальше', 9005)
        self.assertIn('КОНЕЦ', self.panel()[0])
        self.press('Сохранить только заметку', 9005)
        self.assertEqual(self.records('finance_notes')[0]['body'], body)
        self.press('Заметка #', 9005)
        self.assertNotIn('<script>', self.panel()[0])
        for call in self.api.mock_calls:
            if call[0] not in {'send_message', 'edit_message_text'}: continue
            text = call.args[-2]
            plain = html.unescape(re.sub(r'<[^>]*>', '', text))
            self.assertLessEqual(len(plain.encode('utf-16-le')) // 2, 3900)
            for row in call.args[-1]['inline_keyboard']:
                for b in row:
                    if b.get('callback_data'): self.assertLessEqual(len(b['callback_data'].encode()), 64)

    def test_invalid_roles_ids_deadlines_steps_and_expired_drafts_are_rejected(self):
        _, cid = self.issue(); self.claim(cid)
        for hours in (True, -1, 2, 1.0, '4', None, 1000000):
            with self.assertRaises(ValueError): self.schedule(cid, hours, 'invalid-hours')
        for record_id in (True, -1, 0, '1', 2**63):
            with self.assertRaises(ValueError): self.db.finance_case_view(9005, record_id)
        for step in ('paid', 'refunded', 'clear', 'approved', None):
            with self.assertRaises(ValueError): self.step(cid, step, 'invalid-step')
        token = self.db.save_service_draft(9005, 'finance_note', {'case_id': cid, 'version': self.case_row(cid)['version'], 'body': 'Просроченная заметка'})
        self.db.connection().execute('UPDATE service_drafts SET expires_at=0 WHERE token=?', (token,))
        with self.assertRaises(ValueError): self.bot.finance.send_draft(9005, 9005, token)
        self.assertEqual(self.records('finance_notes'), [])

    def test_newer_draft_is_paused_after_old_send_and_stale_text_remains_readable(self):
        _, cid = self.issue(); self.claim(cid)
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message('Первый текст', 9005)
        old = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Сохранить только' in b['text'])
        self.callback(f'f:case:{cid}', 9005); self.press('Внутренняя заметка', 9005); self.message('НОВЫЙ ТЕКСТ ДЛЯ ЧТЕНИЯ', 9005)
        new = self.db.get_state(9005)[1]['token']
        self.callback(old, 9005)
        self.assertEqual(self.db.get_state(9005)[1]['token'], new)
        self.assertTrue(self.db.get_state(9005)[1]['paused'])
        self.message('/resume', 9005)
        self.assertIn('НОВЫЙ ТЕКСТ ДЛЯ ЧТЕНИЯ', self.panel()[0])
        self.assertIn('задача изменились', self.panel()[0])
        self.assertFalse(any('Сохранить только' in b['text'] for row in self.panel()[1]['inline_keyboard'] for b in row))
        self.assertEqual(len(self.records('finance_notes')), 1)

    def test_staff_finance_data_and_commands_are_not_exposed_through_service_api(self):
        r, cid = self.issue(); self.claim(cid)
        self.db.add_finance_note(9005, cid, 'PRIVATE-NATIVE-FINANCE-NOTE', self.case_row(cid)['version'], 'api-proof-note')
        api = operations.OperationsTests.api_client(self)
        for uid in (420, 9005, 9001):
            status, body = api('?case_id=' + str(cid) + '&staff=1', uid=uid)
            self.assertEqual(status, 200)
            self.assertNotIn('PRIVATE-NATIVE-FINANCE-NOTE', json.dumps(body))
            self.assertNotIn('finance-mismatch', json.dumps(body))
            status, body = api(uid=uid, data={'action': 'finance_note', 'operation_id': 'bad-http', 'case_id': cid, 'text': 'NEVER-STORE-HTTP-FINANCE'})
            self.assertEqual(status, 400)
        self.assertNotIn('NEVER-STORE-HTTP-FINANCE', '\n'.join(self.db.connection().iterdump()))
        self.assertEqual(len(self.records('finance_notes')), 1)

    def test_financial_note_limit_is_checked_before_a_partial_write(self):
        _, cid = self.issue(); self.claim(cid)
        with self.db.commerce_transaction() as conn:
            conn.executemany('INSERT INTO finance_notes(case_id,actor_id,body,created_at) VALUES (?,?,?,?)',
                [(cid, 9005, 'Synthetic quota fixture', '2026-01-01')] * 500)
        version = self.case_row(cid)['version']
        with self.assertRaises(ValueError): self.db.add_finance_note(9005, cid, 'Сверх лимита', version, 'quota-note')
        self.assertEqual(len(self.records('finance_notes')), 500)
        self.assertIsNone(self.db.service_result(9005, 'quota-note'))
        self.assertEqual(self.case_row(cid)['version'], version)

    def test_invoice_binding_and_attempt_case_update_are_one_transaction(self):
        r, a, cid = self.attempt(); self.claim(cid)
        before = self.money_snapshot()
        case = copy.deepcopy(self.case_row(cid))
        with patch.object(self.db, '_finance_event', side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): self.issue_invoice(r, a)
        self.assertEqual(self.money_snapshot(), before)
        self.assertEqual(self.case_row(cid), case)
        self.assertIsNone(self.db.get_invoice('lava', 'finance-known-invoice'))

    def test_recovery_scanner_finds_preexisting_conflict_on_applied_receipt(self):
        r, _ = self.buy(paid=True)
        receipt = self.records('payment_receipts')[0]
        with self.db.commerce_transaction() as conn:
            conn.execute('INSERT INTO payment_receipt_conflicts VALUES (?,?,?,?)',
                ('a' * 64, receipt['receipt_id'], json.dumps({'claimed_ref': 'other', 'amount_minor': 999}), '2026-01-01'))
        self.assertEqual(self.records('finance_cases'), [])
        self.db.sync_finance(limit=1)
        c = self.db.finance_case_view(9005, self.case_row()['case_id'])
        self.assertTrue(c['active']); self.assertEqual(c['source']['conflict_count'], 1)
        self.assertEqual(c['source']['status'], 'applied')
        self.assertEqual(len(self.records('payment_receipts')), 1)

    def test_report_large_integer_sum_does_not_overflow_or_turn_into_float(self):
        count, amount = 1100, 9000000000000000
        with self.db.commerce_transaction() as conn:
            conn.executemany("""INSERT INTO payment_receipts(method,provider_id,payment_id,claimed_ref,amount_minor,currency,status,source,received_at)
                VALUES ('crypto',?,'unknown','unknown',?,'UNK','review_required','verified_event','2026-01-01')""",
                [('big-int-' + str(i), amount) for i in range(count)])
        report = self.db.finance_summary(9005)
        self.assertEqual(report['totals'][0]['count'], count)
        self.assertEqual(report['totals'][0]['amount_minor'], count * amount)
        self.assertIs(type(report['totals'][0]['amount_minor']), int)
        self.assertIn('мин. ед. UNK', report['totals'][0]['amount_label'])

    def test_bounded_deadline_alert_scan_eventually_covers_all_tasks_without_repeats(self):
        for i in range(75): self.unknown('batch-orphan-' + str(i))
        with self.db.commerce_transaction() as conn:
            conn.execute("UPDATE finance_cases SET assigned_to=9005,due_at=?,due_generation=1", (time.time() - 10,))
        self.db.queue_finance_alerts(self.settings.admin_ids)
        self.assertEqual(len([r for r in self.records('finance_alerts') if r['kind'] == 'due']), 50)
        for _ in range(5): self.db.queue_finance_alerts(self.settings.admin_ids)
        due = self.finance_notifications('due')
        self.assertEqual(len(due), 75)
        self.assertEqual(len({r['notification_id'] for r in due}), 75)

    def test_successful_payment_staff_notice_is_reference_only_not_payer_or_amount(self):
        from dataclasses import replace
        self.bot.settings = replace(self.settings, manager_chat_id=-10055)
        r, _ = self.buy(paid=True)
        group = [n for n in self.records('notifications') if n['chat_id'] == -10055]
        self.assertTrue(group)
        for notice in group:
            self.assertNotIn(r['payment_id'], notice['body'])
            self.assertNotIn('operations-charge', notice['body'])
            self.assertNotIn('Клиент:', notice['body'])
            self.assertNotIn('Сумма:', notice['body'])
            self.assertIn('личном чате', notice['body'])


if __name__ == '__main__': unittest.main()
