"""Isolated tests of one-shot subscriptions, live eligibility and send ambiguity."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import hashlib
import hmac
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock,patch

from alerts_store import TERMS_VERSION,WATCH_TTL,QUEUE_TTL,SEND_LEASE,policy_fingerprint,product_identity
from bot import BrandBot,Database,Settings,TelegramAPI,start_health_server
from commerce_store import CartConflict
import test_commerce as commerce
import test_operations as operations

TEE='tee-sila-i-chest';TAG='tag-sila-i-chest'


class AlertsTests(unittest.TestCase):
    callback=commerce.CommerceTests.callback
    panel=commerce.CommerceTests.panel
    press=commerce.CommerceTests.press
    message=operations.OperationsTests.message
    set_count=commerce.CommerceTests.set_count
    checkout=commerce.CommerceTests.checkout
    confirm_stars=commerce.CommerceTests.confirm_stars

    def setUp(self):
        operations.OperationsTests.setUp(self)
        self.settings=replace(self.settings,product_alerts_enabled=True,product_alerts_policy_url='https://example.test/product-alerts')
        self.bot=BrandBot(self.settings,self.api,self.db,self.catalog)
        self.api.send_message_once.return_value={'message_id':987}
        self.db.set_staff_role(9001,9005,'finance',**self.owner)
        self.db.upsert_user({'id':422,'first_name':'Another test buyer'})
        self.set_count(0);self.serial=0

    @property
    def policy(self): return self.settings.alerts_policy_url()

    def key(self): self.serial+=1;return 'alert-test-'+str(self.serial)
    def records(self,table): return [dict(r) for r in self.db.connection().execute('SELECT * FROM '+table)]
    def money(self):
        return {t:self.records(t) for t in ('payments','orders','purchases','stock_items','stock_reservations','stock_movements','payment_receipts','payment_invoices','payment_invoice_attempts','notifications','promotion_redemptions','cart_lines')}
    def sub(self,uid=420,kind='stock',pid=TEE,size='M',limit=None,operation=None):
        c=self.db.alert_candidate(uid,kind,pid,'' if kind=='price' else size,limit,self.catalog)
        return self.db.subscribe_alert(uid,c,operation or self.key(),self.catalog,policy_url=self.policy)
    def price(self,value): self.catalog.get(TEE)['price']=str(value)+' ₽'
    def scan(self,**kw): return self.db.scan_product_alerts(self.catalog,policy_url=self.policy,**kw)
    def claim(self,**kw): return self.db.claim_product_alert(self.catalog,policy_url=self.policy,**kw)
    def watch(self,w): return self.db.alert_watch(w['user_id'],w['watch_id'],self.catalog)
    def finish(self,c,**kw): return self.db.finish_product_alert(c['delivery_id'],c['claim_token'],**kw)
    def offall(self,uid=420): return self.db.stop_all_alerts(uid,self.db.alert_account(uid)['generation'],self.key())

    def extra(self,count):
        candidate=copy.deepcopy(self.catalog.data)
        for i in range(count):
            p=copy.deepcopy(candidate['products'][0]);p.update(id='alert-fixture-'+str(i),name='Только тест '+str(i),sizes=['M'],price='100 ₽')
            candidate['products'].append(p)
        self.catalog.save(candidate);self.db.sync_inventory(self.catalog)
        self.db.connection().execute("UPDATE stock_items SET on_hand=0 WHERE product_id LIKE 'alert-fixture-%'")

    def test_schema_is_additive_no_legacy_waitlist_or_favorites_auto_import(self):
        self.db.add_to_waitlist(420,self.catalog.get(TEE),'M')
        a=self.db.discovery_consent(420,True,0,'allow-saved')
        self.db.change_collection(420,'saved',TEE,True,a['generation'],a['saved_revision'],'pin',self.catalog)
        before=self.money();self.db.init_alerts_store();self.db.init_alerts_store()
        self.assertEqual(self.records('product_watches'),[]);self.assertEqual(self.records('product_alert_deliveries'),[])
        self.assertEqual(self.records('product_alert_accounts'),[]);self.assertEqual(self.money(),before)
        self.assertEqual(self.db.connection().execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_feature_default_off_and_policy_requires_explicit_https(self):
        with patch('bot.load_dotenv'),patch.dict(os.environ,{},clear=True):
            self.assertFalse(Settings.from_env().product_alerts_enabled)
        for value in ('maybe','0','false'):
            with patch('bot.load_dotenv'),patch.dict(os.environ,{'PRODUCT_ALERTS_ENABLED':value},clear=True): self.assertFalse(Settings.from_env().product_alerts_enabled)
        for url in ('','http://example.test','https://','javascript:alert(1)','https://u:p@example.test','https://example.test/\npolicy',None):
            with self.subTest(url=url),self.assertRaises(ValueError): policy_fingerprint(url)
        self.assertEqual(len(policy_fingerprint(self.policy)),64)

    def test_disabled_delivery_and_missing_policy_never_create_subscriptions_or_send(self):
        self.bot.settings=replace(self.settings,product_alerts_enabled=False)
        self.callback('a:stock:'+TEE);self.assertIn('ещё не включил',self.panel()[0]);self.assertEqual(self.records('product_watches'),[])
        self.bot.settings=replace(self.settings,product_alerts_policy_url='',privacy_url='')
        self.callback('a:price:'+TEE);self.assertIn('HTTPS',self.panel()[0]);self.assertEqual(self.records('product_watches'),[])
        self.bot.settings=self.settings;self.sub();self.set_count(1);self.scan()
        self.bot.settings=replace(self.settings,product_alerts_enabled=False);self.bot.alerts.tick();self.api.send_message_once.assert_not_called()
        self.message('/alertsoff');self.assertEqual(self.records('product_watches'),[])

    def test_native_stock_consent_is_explicit_and_not_contact_marketing_or_lists_consent(self):
        self.callback('wait:'+TEE);self.press('M ·')
        self.assertIn('ОТДЕЛЬНОЕ СОГЛАСИЕ',self.panel()[0]);self.assertEqual(self.records('product_watches'),[])
        token=self.press('Да, уведомить один раз');self.callback(token)
        self.assertEqual(len(self.records('product_watches')),1);self.assertFalse(self.db.has_consent(420))
        self.assertFalse(self.db.discovery_account(420)['enabled']);self.assertTrue(self.db.get_user(420)['service_only'])
        self.assertNotIn(420,self.db.broadcast_audience('all'))
        self.db.set_consent(420)  # An old contact flow may not broaden this choice.
        self.assertTrue(self.db.get_user(420)['service_only']);self.assertFalse(self.db.broadcast_recipient_current(420))
        self.assertEqual(self.records('waitlist'),[]);self.api.send_message_once.assert_not_called()

    def test_legacy_wait_buttons_and_restock_never_bypass_new_consent(self):
        self.callback('wsize:'+TEE+':M');self.assertEqual(self.db.stats()['waitlist'],1)
        self.assertEqual(self.records('product_watches'),[]);self.assertIn('ОТДЕЛЬНОЕ СОГЛАСИЕ',self.panel()[0])
        self.set_count(1);self.api.reset_mock()
        self.assertEqual(self.bot.notify_waitlist(TEE,'M'),0);self.api.send_message.assert_not_called();self.api.send_message_once.assert_not_called()
        self.message('/restock '+TEE+' M',9001)
        self.assertIn('больше не рассылает',self.api.send_message.call_args.args[1])
        self.assertEqual(self.records('product_alert_deliveries'),[])

    def test_conditions_require_exact_size_known_price_and_strict_integer_threshold(self):
        for size in ('S','WRONG','',False):
            with self.subTest(size=size),self.assertRaises(ValueError): self.sub(size=size)
        for limit in (None,0,-1,True,4900,5000,4500.5,'4500'):
            with self.subTest(limit=limit),self.assertRaises(ValueError): self.sub(kind='price',limit=limit)
        for value in ('0 ₽','1 ₽','Цена по запросу','4900 - 6000 ₽'):
            self.catalog.get(TEE)['price']=value
            with self.subTest(price=value),self.assertRaises(ValueError): self.sub(kind='price',limit=1)

    def test_price_baseline_is_canonical_rubles_not_a_label_badge_or_a_coupon(self):
        self.catalog.get(TEE)['price']='4 900,49 ₽'
        w=self.sub(kind='price',limit=4800);self.assertEqual(w['anchor_rub'],4900)
        self.catalog.get(TEE)['stock_label']='Всё по 99 ₽';self.catalog.get(TEE)['badge']='Скидка 90%'
        self.assertEqual(self.scan(),0);self.assertIsNone(self.claim())
        self.price(4800);self.assertEqual(self.scan(),1)
        c=self.claim();self.assertEqual(c['product']['price_rub'],4800)

    def test_price_increase_never_moves_the_baseline_or_creates_a_fake_drop(self):
        w=self.sub(kind='price',limit=4500)
        for price in (5300,5000,4900,4800): self.price(price);self.assertEqual(self.scan(),0)
        self.assertEqual(self.watch(w)['anchor_rub'],4900)
        self.price(4500);self.assertEqual(self.scan(),1);self.bot.alerts.tick()
        self.assertEqual(self.watch(w)['state'],'sent');self.assertIn('4 900 ₽',self.api.send_message_once.call_args.args[1])

    def test_price_notification_does_not_imply_size_availability_or_reserve(self):
        self.sub(kind='price',limit=4400);self.price(4400);before=self.money();self.bot.alerts.tick()
        text=self.api.send_message_once.call_args.args[1]
        self.assertIn('Наличие размера этим сигналом не подтверждается',text);self.assertIn('не резерв',text)
        self.assertEqual(self.money(),before);self.assertEqual(self.records('notifications'),[])
        self.catalog.get(TEE)['price']='не указана'
        self.bot.settings=replace(self.settings,product_alerts_enabled=False)
        self.callback('a:live:'+TEE)
        self.assertIn('Цена уточняется',self.panel()[0])
        self.assertFalse(any('Выбрать размер' in b['text'] for row in self.panel()[1]['inline_keyboard'] for b in row))

    def test_unknown_zero_and_another_size_do_not_trigger_stock_signal(self):
        w=self.sub();self.catalog.get(TEE)['stock_label']='1000 штук в наличии'
        for value in (None,0,None):
            self.db.connection().execute('UPDATE stock_items SET on_hand=? WHERE product_id=? AND size=?',(value,TEE,'M'))
            self.assertEqual(self.scan(),0)
        self.assertEqual(self.watch(w)['state'],'watching');self.assertIsNone(self.claim())
        self.set_count(1);self.assertEqual(self.scan(),1);self.bot.alerts.tick();self.assertEqual(self.watch(w)['state'],'sent')

    def test_one_free_unit_notifies_only_oldest_eligible_subscriber_without_reserving(self):
        watches=[self.sub(uid) for uid in (420,421,422)]
        self.set_count(1);before=self.money();self.assertEqual(self.scan(),1);self.bot.alerts.tick()
        self.assertEqual(self.api.send_message_once.call_args.args[0],420)
        self.assertEqual([self.watch(w)['state'] for w in watches],['sent','watching','watching'])
        self.assertEqual(self.money(),before)
        self.scan();self.bot.alerts.tick();self.assertEqual(self.api.send_message_once.call_count,1)
        self.set_count(2);self.bot.alerts.tick();self.assertEqual(self.api.send_message_once.call_count,2)
        self.assertEqual(self.api.send_message_once.call_args.args[0],421)

    def test_stock_shrink_before_send_cancels_surplus_queued_signals(self):
        watches=[self.sub(uid) for uid in (420,421,422)]
        self.set_count(3);self.assertEqual(self.scan(),3);self.set_count(1)
        c=self.claim();self.assertEqual(c['watch']['user_id'],420);self.finish(c,message_id=987)
        self.assertIsNone(self.claim());self.assertEqual([self.watch(w)['state'] for w in watches],['sent','watching','watching'])
        self.assertEqual(sum(r['state']=='queued' for r in self.records('product_alert_deliveries')),0)

    def test_stock_lost_before_attempt_rearms_watch_without_retrying_a_sent_message(self):
        w=self.sub();self.set_count(1);self.scan();self.set_count(0)
        self.assertIsNone(self.claim());self.assertEqual(self.watch(w)['state'],'watching')
        self.set_count(1);self.bot.alerts.tick();self.assertEqual(self.watch(w)['state'],'sent')
        self.set_count(0);self.scan();self.set_count(5);self.bot.alerts.tick();self.api.send_message_once.assert_called_once()
        self.assertEqual([r['state'] for r in self.records('product_alert_deliveries')],['cancelled','sent'])

    def test_price_recovery_or_invalid_price_before_send_rearms_without_sending_stale_price(self):
        w=self.sub(kind='price',limit=4500);self.price(4500);self.scan()
        self.price(4600);self.assertIsNone(self.claim());self.assertEqual(self.watch(w)['state'],'watching')
        self.price(4400);self.scan();self.catalog.get(TEE)['price']='unknown'
        self.assertIsNone(self.claim());self.price(4300);self.bot.alerts.tick()
        self.assertIn('4 300 ₽',self.api.send_message_once.call_args.args[1]);self.api.send_message_once.assert_called_once()

    def test_cancellation_of_queued_oldest_allows_another_consenting_subscriber(self):
        first=self.sub();second=self.sub(421);self.set_count(1);self.scan()
        self.db.stop_alert(420,first['watch_id'],self.key());self.bot.alerts.tick()
        self.assertEqual(self.watch(first)['state'],'cancelled');self.assertEqual(self.watch(second)['state'],'sent')
        self.assertEqual(self.api.send_message_once.call_args.args[0],421)

    def test_repeated_subscription_request_preserves_deadline_and_never_toggles(self):
        c=self.db.alert_candidate(420,'stock',TEE,'M',None,self.catalog)
        w=self.db.subscribe_alert(420,c,'same',self.catalog,policy_url=self.policy)
        again=self.db.subscribe_alert(420,c,'same',self.catalog,policy_url=self.policy)
        self.assertEqual(w,again)
        with self.assertRaises(ValueError): self.db.subscribe_alert(420,c,'another',self.catalog,policy_url=self.policy)
        with self.assertRaises(ValueError): self.db.subscribe_alert(420,{**c,'size':'XL'},'same',self.catalog,policy_url=self.policy)
        self.assertEqual(len(self.records('product_watches')),1)

    def test_stale_price_or_identity_at_consent_cannot_change_agreed_conditions(self):
        c=self.db.alert_candidate(420,'price',TEE,'',4500,self.catalog);self.price(4700)
        with self.assertRaises(CartConflict): self.db.subscribe_alert(420,c,self.key(),self.catalog,policy_url=self.policy)
        c=self.db.alert_candidate(420,'price',TEE,'',4500,self.catalog);self.catalog.get(TEE)['description']+=' new text'
        with self.assertRaises(CartConflict): self.db.subscribe_alert(420,c,self.key(),self.catalog,policy_url=self.policy)
        self.assertEqual(self.records('product_watches'),[])

    def test_one_active_watch_per_condition_and_old_stop_cannot_cancel_new_one(self):
        first=self.sub();self.db.stop_alert(420,first['watch_id'],'stop-first');new=self.sub()
        self.assertNotEqual(new['watch_id'],first['watch_id'])
        self.db.stop_alert(420,first['watch_id'],'stop-first');self.assertEqual(self.watch(new)['state'],'watching')
        self.assertEqual(self.watch(first)['state'],'cancelled')

    def test_twenty_watch_limit_is_enforced_with_one_concurrent_last_slot(self):
        self.extra(21)
        for i in range(19): self.sub(pid='alert-fixture-'+str(i))
        def subscribe(i):
            try:
                c=self.db.alert_candidate(420,'stock','alert-fixture-'+str(i),'M',None,self.catalog)
                self.db.subscribe_alert(420,c,'concurrent-'+str(i),self.catalog,policy_url=self.policy);return True
            except ValueError: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(subscribe,[19,20]))
        self.assertEqual(sum(results),1);self.assertEqual(self.db.alert_list(420,self.catalog)['total'],20)

    def test_stop_all_erases_preferences_not_favorites_profile_cart_or_business(self):
        self.sub();self.sub(kind='price',limit=4500)
        self.db.set_state(420,'ops_preview',{'token':'private-keep'})
        a=self.db.discovery_consent(420,True,0,'saved-consent')
        self.db.change_collection(420,'saved',TEE,True,a['generation'],a['saved_revision'],'save-item',self.catalog)
        before=self.money();favorites=self.records('discovery_items');state=self.db.get_state(420)
        self.offall();self.assertEqual(self.records('product_watches'),[]);self.assertEqual(self.records('product_alert_deliveries'),[])
        self.assertFalse(self.db.alert_account(420)['enabled']);self.assertEqual(self.money(),before)
        self.assertEqual(self.records('discovery_items'),favorites);self.assertEqual(self.db.get_state(420),state)

    def test_old_unprocessed_consent_and_replayed_opt_out_do_not_affect_reenabled_account(self):
        old=self.db.alert_candidate(420,'stock',TEE,'M',None,self.catalog)
        self.db.stop_all_alerts(420,0,'off-zero');self.assertTrue(self.db.get_user(420)['service_only']);self.assertFalse(self.db.has_consent(420));new=self.sub()
        with self.assertRaises(CartConflict): self.db.subscribe_alert(420,old,'old-consent',self.catalog,policy_url=self.policy)
        self.db.stop_all_alerts(420,0,'off-zero')
        self.assertTrue(self.db.alert_account(420)['enabled']);self.assertEqual(self.watch(new)['state'],'watching')

    def test_unsubscribe_before_dispatch_prevents_network_even_when_event_was_queued(self):
        self.sub();self.set_count(1);self.scan();self.offall();self.bot.alerts.tick()
        self.api.send_message_once.assert_not_called();self.assertEqual(self.records('product_alert_deliveries'),[])

    def test_unsubscribe_during_started_send_does_not_recreate_deleted_preferences_on_ack(self):
        self.sub();self.set_count(1)
        def in_flight(*args): self.offall();return {'message_id':345}
        self.api.send_message_once.side_effect=in_flight
        self.bot.alerts.tick();self.api.send_message_once.assert_called_once()
        self.assertEqual(self.records('product_watches'),[]);self.assertEqual(self.records('product_alert_deliveries'),[])
        self.assertFalse(self.db.alert_account(420)['enabled']);self.assertEqual(len(self.db.alert_account(420)['attempt_times']),1)

    def test_hidden_product_changed_description_or_removed_size_invalidates_consent(self):
        for change in ('hidden','description','size'):
            case=AlertsTests();case.setUp()
            try:
                w=case.sub();case.set_count(1);case.scan()
                if change=='hidden': case.catalog.get(TEE)['active']=False
                elif change=='description': case.catalog.get(TEE)['description']+=' Changed'
                else: case.catalog.get(TEE)['sizes']=['S']
                case.bot.alerts.tick();case.api.send_message_once.assert_not_called()
                v=case.watch(w);self.assertEqual(v['state'],'changed');self.assertFalse(v['visible'])
                case.callback('a:watch:'+str(w['watch_id']));self.assertNotIn('СИЛА И ЧЕСТЬ',case.panel()[0])
            finally: case.doCleanups()

    def test_policy_or_terms_change_invalidates_unsent_consent(self):
        w=self.sub();self.set_count(1);self.scan()
        self.bot.settings=replace(self.settings,product_alerts_policy_url='https://example.test/new-policy')
        self.bot.alerts.tick();self.assertEqual(self.watch(w)['state'],'changed');self.api.send_message_once.assert_not_called()
        self.set_count(0);c=self.db.alert_candidate(420,'stock',TEE,'M',None,self.catalog)
        with self.assertRaises(ValueError): self.db.subscribe_alert(420,c,self.key(),self.catalog,policy_url=self.policy,terms_version='old')

    def test_republication_does_not_resurrect_a_retired_watch(self):
        w=self.sub();self.catalog.set_active(TEE,False);self.scan();self.catalog.set_active(TEE,True)
        self.set_count(1);self.bot.alerts.tick();self.assertEqual(self.watch(w)['state'],'changed');self.api.send_message_once.assert_not_called()

    def test_watch_expires_at_thirty_days_and_does_not_renew_on_navigation(self):
        w=self.sub();self.db.alert_list(420,self.catalog);self.assertEqual(self.watch(w)['expires_at'],w['expires_at'])
        self.set_count(1);self.scan(now=w['expires_at']);self.assertEqual(self.watch(w)['state'],'expired')
        self.assertIsNone(self.claim());self.assertEqual(w['expires_at']-w['created_at'],WATCH_TTL)

    def test_queued_signal_expiry_is_rechecked_and_can_queue_fresh_before_any_attempt(self):
        w=self.sub(kind='price',limit=4500);self.price(4500);now=time.time();self.scan(now=now)
        self.assertIsNone(self.claim(now=now+QUEUE_TTL));self.assertEqual(self.watch(w)['state'],'watching')
        self.scan(now=now+QUEUE_TTL+1);self.assertIsNotNone(self.claim(now=now+QUEUE_TTL+1))
        self.assertEqual(len(self.records('product_alert_deliveries')),2)

    def test_two_attempts_per_rolling_day_and_five_minute_gap_are_persisted(self):
        self.sub();self.sub(kind='price',limit=4500);self.sub(kind='price',pid=TAG,limit=1800)
        self.price(4400);self.catalog.get(TAG)['price']='1700 ₽';self.set_count(1);now=time.time();self.scan(now=now)
        a=self.claim(now=now);self.assertIsNotNone(a);self.finish(a,message_id=1,now=now)
        self.assertIsNone(self.claim(now=now+299));b=self.claim(now=now+300);self.assertIsNotNone(b);self.finish(b,message_id=2,now=now+300)
        self.assertIsNone(self.claim(now=now+301));self.assertIsNone(self.claim(now=now+86399))
        self.scan(now=now+86401);self.assertIsNotNone(self.claim(now=now+86401))

    def test_pacing_cannot_be_bypassed_by_delete_reenable_or_restart(self):
        self.sub();self.set_count(1);self.bot.alerts.tick();times=self.db.alert_account(420)['attempt_times'];self.offall()
        self.set_count(0);w=self.sub();self.set_count(1)
        db=Database(self.db.path);self.addCleanup(db.close_current)
        self.assertEqual(db.alert_account(420)['attempt_times'],times)
        db.scan_product_alerts(self.catalog,policy_url=self.policy);self.assertIsNone(db.claim_product_alert(self.catalog,policy_url=self.policy))
        self.assertEqual(self.watch(w)['state'],'watching')

    def test_blocked_user_never_sends_and_unblocking_does_not_restore_retired_watch(self):
        w=self.sub();self.set_count(1);self.scan();self.db.mark_blocked(420);self.bot.alerts.tick()
        self.assertEqual(self.watch(w)['state'],'blocked');self.api.send_message_once.assert_not_called()
        self.db.upsert_user({'id':420});self.bot.alerts.tick();self.api.send_message_once.assert_not_called()

    def test_forbidden_send_retires_other_active_subscriptions_without_retry(self):
        first=self.sub();second=self.sub(kind='price',limit=4000);self.set_count(1)
        error=RuntimeError('No delivery');error.telegram_status=403;self.api.send_message_once.side_effect=error
        self.bot.alerts.tick();self.assertEqual(self.watch(first)['state'],'uncertain');self.assertEqual(self.watch(second)['state'],'blocked')
        self.assertTrue(self.db.get_user(420)['is_blocked']);self.bot.alerts.tick();self.api.send_message_once.assert_called_once()

    def test_ambiguous_network_outcome_is_not_retried_or_reported_as_sent(self):
        w=self.sub();self.set_count(1);self.api.send_message_once.side_effect=urllib.error.URLError('lost reply')
        result=self.bot.alerts.tick();self.assertEqual(result['uncertain'],1);self.assertEqual(self.watch(w)['state'],'uncertain')
        self.scan();self.bot.alerts.tick();self.api.send_message_once.assert_called_once()
        self.callback('a:watch:'+str(w['watch_id']));self.assertIn('Сообщение могло прийти',self.panel()[0])

    def test_invalid_telegram_result_is_uncertain_never_claimed_delivered(self):
        for response in (None,True,{}, {'message_id':True},{'message_id':0},{'message_id':1,'chat':{'id':421}}):
            case=AlertsTests();case.setUp()
            try:
                w=case.sub();case.set_count(1);case.api.send_message_once.return_value=response;case.bot.alerts.tick()
                self.assertEqual(case.watch(w)['state'],'uncertain');case.api.send_message_once.assert_called_once()
            finally: case.doCleanups()

    def test_crashed_sending_lease_becomes_uncertain_and_never_retries_after_restart(self):
        w=self.sub();self.set_count(1);now=time.time();self.scan(now=now);c=self.claim(now=now)
        db=Database(self.db.path);self.addCleanup(db.close_current)
        db.scan_product_alerts(self.catalog,policy_url=self.policy,now=now+SEND_LEASE+1)
        self.assertEqual(self.watch(w)['state'],'uncertain');self.assertIsNone(db.claim_product_alert(self.catalog,policy_url=self.policy,now=now+SEND_LEASE+1))
        self.assertTrue(db.finish_product_alert(c['delivery_id'],c['claim_token'],message_id=77,now=now+SEND_LEASE+2))
        self.assertEqual(self.watch(w)['state'],'sent')

    def test_late_or_foreign_ack_cannot_overwrite_cancelled_event_or_new_consent(self):
        w=self.sub();self.set_count(1);self.scan();c=self.claim()
        self.assertFalse(self.db.finish_product_alert(c['delivery_id'],'wrong',message_id=1))
        self.db.stop_alert(420,w['watch_id'],self.key());self.assertFalse(self.finish(c,message_id=2))
        self.assertEqual(self.watch(w)['state'],'cancelled')

    def test_multiple_workers_claim_one_attempt_and_one_credit(self):
        self.sub();self.set_count(1);self.scan()
        barrier=threading.Barrier(2)
        def claim(_):
            barrier.wait()
            try: return self.claim()
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: claims=list(pool.map(claim,[1,2]))
        self.assertEqual(sum(c is not None for c in claims),1);self.assertEqual(len(self.db.alert_account(420)['attempt_times']),1)
        self.assertEqual(self.records('product_alert_stock')[0]['credits'],0)

    def test_no_catalog_or_database_lock_held_during_network_and_private_draft_is_untouched(self):
        self.sub();self.set_count(1);self.db.set_state(420,'commerce_field',{'field':'address','next':'profile'});state=self.db.get_state(420)
        def send(*_):
            self.assertFalse(self.db.connection().in_transaction)
            def probe():
                try:
                    with self.catalog.lock,self.db.commerce_transaction(): return True
                finally: self.db.close_current()
            with ThreadPoolExecutor(max_workers=1) as pool: self.assertTrue(pool.submit(probe).result(timeout=3))
            return {'message_id':11}
        self.api.send_message_once.side_effect=send;self.bot.alerts.tick()
        self.assertEqual(self.db.get_state(420),state);self.assertEqual(self.records('product_watches')[0]['state'],'sent')

    def test_queue_failure_rolls_back_observation_and_watch_status(self):
        w=self.sub();self.set_count(1);before=self.records('product_alert_stock');original=self.db._queue_product_alert
        def fail(*a,**kw): original(*a,**kw);raise sqlite3.OperationalError('disk')
        with patch.object(self.db,'_queue_product_alert',side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError): self.scan()
        self.assertEqual(self.records('product_alert_deliveries'),[]);self.assertEqual(self.records('product_alert_stock'),before)
        self.assertEqual(self.watch(w)['state'],'watching')

    def test_db_ack_failure_after_actual_send_never_retries_the_network(self):
        w=self.sub();self.set_count(1)
        with patch.object(self.db,'finish_product_alert',side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): self.bot.alerts.tick()
        self.bot.alerts.tick();self.api.send_message_once.assert_called_once()
        self.db.prune_alerts(now=time.time()+SEND_LEASE+1);self.assertEqual(self.watch(w)['state'],'uncertain')

    def test_shutdown_stops_new_network_attempts(self):
        self.sub();self.set_count(1);self.bot.alerts.tick(stop_requested=lambda:True)
        self.api.send_message_once.assert_not_called();self.assertEqual(self.records('product_alert_deliveries')[0]['state'],'queued')

    def test_delivery_has_no_side_effects_on_existing_paid_purchase_invoices_cart_or_stock(self):
        r=self.checkout({**self.payload,'items':[{'product_id':TEE,'size':'S','quantity':1}]});self.assertTrue(r['ok'],r)
        self.confirm_stars(r,'unchanged-paid')
        self.db.replace_cart(420,[{'product_id':TAG,'size':'ONE SIZE','quantity':1}],0,'cart',self.catalog)
        self.sub();self.set_count(1);before=self.money();self.bot.alerts.tick();self.assertEqual(self.money(),before)
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'],'paid')

    def test_privacy_no_staff_shortcut_for_list_watch_cancellation_or_action(self):
        self.callback('a:stock:'+TEE);self.press('M ·');token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Да, уведомить' in b['text'])
        self.callback(token,9001);self.assertEqual(self.records('product_watches'),[])
        self.callback(token);w=self.records('product_watches')[0]
        for uid in (421,9001,9002,9003,9004,9005):
            with self.subTest(uid=uid),self.assertRaises(ValueError): self.db.alert_watch(uid,w['watch_id'],self.catalog)
            with self.assertRaises(ValueError): self.db.stop_alert(uid,w['watch_id'],self.key())
            self.assertEqual(self.db.alert_list(uid,self.catalog)['items'],[])
        self.assertEqual(self.watch(w)['state'],'watching')

    def test_group_callbacks_never_subscribe_and_huge_ids_do_not_crash(self):
        self.callback('a:stock:'+TEE);self.press('M ·');token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Да, уведомить' in b['text'])
        self.api.reset_mock();self.callback(token,private=False);self.api.send_message.assert_not_called();self.assertEqual(self.records('product_watches'),[])
        self.callback('a:watch:'+'9'*45);self.assertIn('НУЖНО ВНИМАНИЕ',self.panel()[0])

    def test_owner_diagnostics_have_counts_but_no_customer_watch_contents(self):
        self.sub();self.message('/alertdesk',9001);text=self.panel()[0]
        self.assertNotIn(TEE,text);self.assertNotIn('СИЛА И ЧЕСТЬ',text);self.assertNotIn('420',text)
        for uid in (420,9002,9003,9004,9005):
            with self.assertRaises(PermissionError): self.db.alert_health(uid,**self.owner)

    def test_native_price_input_pause_resume_cancel_and_explicit_consent(self):
        self.callback('a:price:'+TEE);self.press('Указать свой порог');self.message('/menu')
        self.message('PRIVATE PHONE +79995553322');self.assertNotIn('PRIVATE PHONE','\n'.join(self.db.connection().iterdump()))
        self.message('/resume');self.message('4500');self.assertEqual(self.records('product_watches'),[])
        self.assertIn('4 500 ₽',self.panel()[0]);self.press('Да, уведомить один раз')
        self.assertEqual(self.records('product_watches')[0]['limit_rub'],4500)
        self.callback('a:price:'+TAG);self.press('Указать свой порог');self.message('/cancel');self.assertIsNone(self.db.get_state(420))
        self.callback('a:price:'+TAG);self.press('Указать свой порог');self.message('/menu')
        self.catalog.get(TAG)['price']='1800 ₽';state=self.db.get_state(420)
        self.message('/resume');self.assertEqual(self.db.get_state(420),state)
        self.assertIn('изменились',self.panel()[0])

    def test_processed_price_message_and_opt_out_never_enter_new_private_work(self):
        self.callback('a:price:'+TEE);self.press('Указать свой порог');self.message('4500',mid=700)
        self.db.set_state(420,'commerce_field',{'field':'address','next':'profile'});before=self.db.get_state(420)
        self.message('4500',mid=700);self.message('CHANGED SAME UPDATE',mid=700);self.assertEqual(self.db.get_state(420),before)
        self.message('/alertsoff',mid=701);w=self.sub();self.db.set_state(420,'ops_input',{'field':'body','token':'keep'});before=self.db.get_state(420)
        self.message('/alertsoff',mid=701);self.assertEqual(self.db.get_state(420),before);self.assertEqual(self.watch(w)['state'],'watching')
        self.assertNotIn('CHANGED SAME UPDATE','\n'.join(self.db.connection().iterdump()))

    def test_own_price_input_refuses_invalid_values_without_saving_raw_text(self):
        self.callback('a:price:'+TEE);self.press('Указать свой порог')
        for text in ('-100','4500.50','0','4900','CARD 4242 4242','телефон +79990000000'):
            self.message(text);self.assertEqual(self.records('product_watches'),[]);self.assertEqual(self.db.get_state(420)[0],'watch_price')
            self.assertNotIn(text,'\n'.join(self.db.connection().iterdump())) if text.startswith(('CARD','телефон')) else None
        self.message('4500');self.assertIn('ОТДЕЛЬНОЕ СОГЛАСИЕ',self.panel()[0])

    def test_old_policy_consent_button_and_expired_input_do_not_subscribe(self):
        self.callback('a:price:'+TEE);self.press('Любое снижение');token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Да, уведомить' in b['text'])
        self.bot.settings=replace(self.settings,product_alerts_policy_url='https://example.test/updated');self.callback(token)
        self.assertEqual(self.records('product_watches'),[])
        self.callback('a:price:'+TEE);self.press('Указать свой порог');s=self.db.get_state(420)
        self.db.set_state(420,s[0],{**s[1],'expires_at':0});self.message('4500');self.assertEqual(self.records('product_watches'),[])

    def test_deleting_discovery_favorites_does_not_modify_independent_alert_consent(self):
        w=self.sub();a=self.db.discovery_consent(420,True,0,'consent');self.db.discovery_consent(420,False,a['generation'],'forget')
        self.assertEqual(self.watch(w)['state'],'watching');self.assertTrue(self.db.alert_account(420)['enabled'])

    def test_catalog_unicode_is_escaped_and_telegram_utf16_callback_limits_hold(self):
        self.catalog.get(TEE)['name']='<img src=x> & 😀'*20
        self.callback('a:stock:'+TEE);self.press('M ·');self.press('Да, уведомить один раз');self.set_count(1);self.bot.alerts.tick()
        for call in self.api.mock_calls:
            if call[0] not in {'send_message','edit_message_text','send_message_once'}: continue
            text=call.args[-2];self.assertNotIn('<img',text)
            plain=html.unescape(re.sub(r'<[^>]*>','',text));self.assertLessEqual(len(plain.encode('utf-16-le'))//2,3900)
            markup=call.args[-1]
            if isinstance(markup,dict):
                for row in markup.get('inline_keyboard',[]):
                    for b in row:
                        if b.get('callback_data'): self.assertLessEqual(len(b['callback_data'].encode()),64)

    def test_transport_makes_one_http_attempt_even_on_urLError_or_rate_limit(self):
        api=TelegramAPI('only-a-public-unit-test-token')
        for error in (urllib.error.URLError('timeout'),urllib.error.HTTPError('https://example.test',429,'rate limited',{},io.BytesIO(b'{}'))):
            with patch('urllib.request.urlopen',side_effect=error) as opening,patch('time.sleep') as sleeping:
                with self.assertRaises(Exception): api.send_message_once(420,'Test')
                self.assertEqual(opening.call_count,1);sleeping.assert_not_called()

    def test_transport_validates_structured_success_and_does_not_truncate_html(self):
        api=TelegramAPI('public-test')
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"ok":true,"result":{"message_id":12,"chat":{"id":420}}}'
        with patch('urllib.request.urlopen',return_value=response) as opening:
            self.assertEqual(api.send_message_once(420,'<b>Text</b>')['message_id'],12)
            self.assertEqual(opening.call_args.kwargs['timeout'],20)
        with patch('urllib.request.urlopen') as opening:
            with self.assertRaises(ValueError): api.send_message_once(420,'😀'*2000)
            opening.assert_not_called()

    def test_terminal_history_is_pruned_but_account_generation_and_rate_timestamps_remain(self):
        w=self.sub();self.set_count(1);self.bot.alerts.tick();a=self.db.alert_account(420)
        self.db.prune_alerts(now=time.time()+31*86400)
        self.assertEqual(self.records('product_watches'),[]);self.assertEqual(self.records('product_alert_deliveries'),[])
        self.assertEqual(self.db.alert_account(420)['attempt_times'],a['attempt_times']);self.assertEqual(self.records('product_alert_stock'),[])

    def test_http_waitlist_remains_scoped_and_cannot_forge_alert_consent(self):
        request=self.http();body={'product_id':TEE,'size':'M','alert_consent':True,'notify':True}
        self.assertEqual(request('/api/waitlist',body,valid=False)[0],401)
        status,data=request('/api/waitlist',body);self.assertEqual(status,200);self.assertTrue(data['alert_consent_required'])
        status,data=request('/api/waitlist',body);self.assertFalse(data['added'])
        self.assertEqual(self.db.stats()['waitlist'],1);self.assertEqual(self.records('product_watches'),[])
        self.assertEqual(request('/api/alerts?user_id=420')[0],404)
        self.api.send_message_once.assert_not_called()

    def test_throttled_oldest_does_not_hold_the_only_stock_signal_slot(self):
        old=self.sub();other=self.sub(421);self.sub(kind='price',limit=4500)
        self.price(4500);self.set_count(1);now=time.time();self.scan(now=now)
        first=self.claim(now=now);self.assertEqual(first['watch']['kind'],'price');self.finish(first,message_id=1,now=now)
        self.scan(now=now+1);second=self.claim(now=now+1)
        self.assertIsNotNone(second);self.assertEqual(second['watch']['watch_id'],other['watch_id'])
        self.assertEqual(self.watch(old)['state'],'watching')

    def test_ready_delivery_after_two_hundred_throttled_accounts_is_not_starved(self):
        now=time.time()
        for i in range(201):
            uid=10000+i;self.db.upsert_user({'id':uid,'first_name':'Synthetic pacing fixture'})
            self.sub(uid,kind='price',limit=4500)
        self.price(4500);self.scan(limit=1000,now=now+1)
        self.db.connection().execute("UPDATE product_alert_accounts SET attempt_times=?,next_attempt_at=? WHERE user_id>=10000 AND user_id<10200",(json.dumps([now-400,now-100]),now+86000))
        c=self.claim(now=now+1);self.assertIsNotNone(c);self.assertEqual(c['watch']['user_id'],10200)

    def test_legacy_interest_request_is_service_only_even_for_a_previously_captured_broadcast(self):
        self.assertIn(420,self.db.broadcast_audience('all'))
        self.db.add_to_waitlist(420,self.catalog.get(TEE),'M')
        self.assertNotIn(420,self.db.broadcast_audience('all'))
        self.db.set_consent(420);self.assertTrue(self.db.get_user(420)['service_only'])
        self.api.reset_mock();self.bot._run_broadcast(9001,9001,'Only synthetic marketing','all',[420])
        self.assertFalse(any(c.args[0]==420 for c in self.api.send_message.call_args_list))
        self.api.send_message_once.assert_not_called()

    def http(self):
        server=start_health_server(0,self.catalog,self.settings,self.db,self.api)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url='http://127.0.0.1:'+str(server.server_address[1])
        def request(path,data=None,valid=True):
            fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':420})}
            secret=hmac.new(b'WebAppData',self.settings.token.encode(),hashlib.sha256).digest()
            fields['hash']=hmac.new(secret,'\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(),hashlib.sha256).hexdigest()
            req=urllib.request.Request(url+path,headers={'X-Telegram-Init-Data':urllib.parse.urlencode(fields) if valid else 'invalid','Content-Type':'application/json'},data=json.dumps(data).encode() if data is not None else None)
            try: response=urllib.request.urlopen(req,timeout=10)
            except urllib.error.HTTPError as exc: response=exc
            with response:
                text=response.read().decode()
                try: value=json.loads(text)
                except ValueError: value={}
                return response.status,value
        return request


if __name__=='__main__': unittest.main()
