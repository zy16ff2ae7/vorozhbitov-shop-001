"""Repeat/promotion acceptance; synthetic catalog/counts and no provider network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import hashlib
import hmac
import html
import json
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from bot import Database,start_health_server
from commerce_store import CartConflict
from growth_store import PromotionError,allocation,promo_code,quote_fingerprint,validate_rules
import test_commerce as commerce
import test_operations as operations

TEE='tee-sila-i-chest';TAG='tag-sila-i-chest'


class GrowthTests(unittest.TestCase):
    checkout=commerce.CommerceTests.checkout
    confirm_stars=commerce.CommerceTests.confirm_stars
    profile=commerce.CommerceTests.profile
    set_count=commerce.CommerceTests.set_count
    callback=commerce.CommerceTests.callback
    panel=commerce.CommerceTests.panel
    press=commerce.CommerceTests.press
    message=operations.OperationsTests.message

    def setUp(self):
        operations.OperationsTests.setUp(self)
        self.db.set_staff_role(9001,9005,'finance',**self.owner)
        self.serial=0

    def key(self):
        self.serial+=1;return 'growth-test-'+str(self.serial)

    def records(self,table): return [dict(r) for r in self.db.connection().execute('SELECT * FROM '+table)]

    def money(self):
        return {t:self.records(t) for t in ('payments','orders','purchases','stock_items','stock_reservations','stock_movements','payment_receipts','payment_invoices','payment_invoice_attempts','notifications','promotion_redemptions')}

    def rules(self,**changes):
        now=int(time.time())
        return {'kind':'percent','value':10,'max_discount':1000,'min_subtotal':0,'categories':[],
                'starts_at':now-60,'ends_at':now+3600,'total_limit':10,'user_limit':1,**changes}

    def campaign(self,code='TEST10',active=True,**rules):
        p=self.db.create_promotion(9001,code,self.rules(**rules),self.key(),self.catalog,**self.owner)
        if active: p=self.db.set_promotion_active(9001,p['promotion_id'],True,p['version'],self.key(),**self.owner)
        return p

    def cart(self,items=None,uid=420):
        return self.db.replace_cart(uid,items if items is not None else [{'product_id':TEE,'size':'M','quantity':1}],self.db.cart(uid)['revision'],self.key(),self.catalog)

    def select(self,code='TEST10',uid=420):
        q=self.db.promotion_candidate(uid,code,self.catalog)
        return self.db.select_promotion(uid,q['promotion_id'],q['promotion_version'],q['cart_revision'],quote_fingerprint(q),self.key(),self.catalog)

    def payload_with_promo(self,uid=420):
        self.profile(uid)
        q,token=self.db.growth_checkout_preview(uid,self.catalog)
        return {**self.payload,'request_id':self.key(),'cart_revision':q['cart_revision'],'expected_total':q['total_rub'],
                'items':self.db.canonical_cart(q['lines']),**({'promotion_token':token} if token else {})}

    def promo_checkout(self,uid=420):
        return self.checkout(self.payload_with_promo(uid),user={'id':uid})

    def buy(self,items=None,paid=True):
        r=self.checkout({**self.payload,'request_id':self.key(),**({'items':items} if items is not None else {})})
        self.assertTrue(r['ok'],r)
        if paid: self.confirm_stars(r,self.key())
        return r

    def repeat(self,r,key=None):
        q=self.db.repeat_preview(420,r['purchase_id'],self.catalog)
        return self.db.repeat_purchase(420,r['purchase_id'],quote_fingerprint(q),key or self.key(),self.catalog)

    def test_schema_additive_idempotent_no_campaign_default_or_financial_mutation(self):
        r=self.buy();before=self.money()
        self.db.init_growth_store();self.db.init_growth_store()
        self.assertEqual(self.money(),before);self.assertEqual(self.records('promotions'),[])
        self.assertIsNone(self.db.purchase_view(r['purchase_id'],user_id=420)['promotion'])
        self.assertEqual(list(self.db.connection().execute('PRAGMA foreign_key_check')),[])

    def test_code_validation_case_normalization_and_no_unicode_guessing(self):
        self.assertEqual(promo_code(' test-10 '),'TEST-10')
        for x in ('ab','ЯТЕСТ10','TE ST','<script>','X'*25,123,None,'+79990000000'):
            with self.subTest(value=x),self.assertRaises(ValueError): promo_code(x)

    def test_rules_reject_extra_fields_bad_types_unlimited_and_impossible_bounds(self):
        for change in ({'kind':'free'},{'value':100},{'value':True},{'max_discount':0},{'min_subtotal':-1},
                       {'total_limit':0},{'user_limit':11},{'user_limit':1.5},{'categories':['hidden']},
                       {'categories':['drop','access']},{'starts_at':False},{'ends_at':0},
                       {'ends_at':int(time.time())+367*86400},{'bonus':100}):
            with self.subTest(change=change),self.assertRaises(ValueError): validate_rules(self.rules(**change),self.catalog)
        with self.assertRaises(ValueError): validate_rules(self.rules(kind='fixed',value=10,max_discount=20),self.catalog)

    def test_only_owner_may_create_or_control_campaigns_even_with_a_stolen_button(self):
        p=self.campaign(active=False);self.callback('g:campaign:'+str(p['promotion_id']),9001)
        token=self.press('Включить промокод',9001)
        confirm=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Да, включить' in b['text'])
        for uid in (420,421,9002,9003,9004,9005):
            with self.subTest(uid=uid),self.assertRaises(PermissionError): self.db.create_promotion(uid,'STAFF'+str(uid),self.rules(),self.key(),self.catalog,**self.owner)
            self.callback(confirm,uid);self.assertFalse(self.db.promotion(p['promotion_id'])['active'])
        self.callback(confirm,9001);self.assertTrue(self.db.promotion(p['promotion_id'])['active'])

    def test_campaign_creation_is_inactive_unique_and_idempotent(self):
        rules=self.rules();before=self.money()
        p=self.db.create_promotion(9001,'hello',rules,'same-create',self.catalog,**self.owner)
        again=self.db.create_promotion(9001,'HELLO',rules,'same-create',self.catalog,**self.owner)
        self.assertEqual(p,again);self.assertFalse(p['active']);self.assertEqual(self.money(),before)
        with self.assertRaises(ValueError): self.db.create_promotion(9001,'HELLO',rules,self.key(),self.catalog,**self.owner)
        with self.assertRaises(ValueError): self.db.create_promotion(9001,'HELLO',{**rules,'value':20},'same-create',self.catalog,**self.owner)
        self.assertEqual(len(self.records('promotions')),1)

    def test_old_activation_repeat_cannot_reenable_a_stopped_campaign(self):
        p=self.campaign(active=False)
        self.db.set_promotion_active(9001,p['promotion_id'],True,0,'on',**self.owner)
        stopped=self.db.set_promotion_active(9001,p['promotion_id'],False,1,'off',**self.owner)
        self.assertEqual(self.db.set_promotion_active(9001,p['promotion_id'],True,0,'on',**self.owner),stopped)
        with self.assertRaises(CartConflict): self.db.set_promotion_active(9001,p['promotion_id'],True,0,'new-old',**self.owner)
        self.assertEqual(self.db.promotion(p['promotion_id'])['rules'],p['rules'])

    def test_campaign_creation_concurrency_has_one_unique_winner(self):
        rules=self.rules()
        def create(i):
            try:
                self.db.create_promotion(9001,'ONECODE',rules,'concurrent-'+str(i),self.catalog,**self.owner);return True
            except ValueError: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(create,[1,2]))
        self.assertEqual(sum(results),1);self.assertEqual(len(self.records('promotions')),1)

    def test_discount_allocation_conserves_integer_money_and_positive_line_prices(self):
        random.seed(733)
        for _ in range(250):
            amounts=[random.randint(1,10000) for _ in range(random.randint(1,20))]
            capacity=sum(x-1 for x in amounts)
            if not capacity: continue
            discount=random.randint(1,capacity);parts=allocation(amounts,discount)
            self.assertEqual(sum(parts),discount);self.assertTrue(all(0<=d<a for d,a in zip(parts,amounts)))
            self.assertEqual(parts,allocation(amounts,discount))
        self.assertEqual(allocation([1,1,100],99),[0,0,99])
        for amounts,d in (([1],1),([100],100),([100],0),([True,10],1),([100],1.5)):
            with self.assertRaises(ValueError): allocation(amounts,d)

    def test_percentage_budget_uses_only_matching_category_and_has_a_cap(self):
        self.campaign(max_discount=300,min_subtotal=5000,categories=['drop'])
        self.cart([{'product_id':TEE,'size':'M','quantity':1},{'product_id':TAG,'size':'ONE SIZE','quantity':2}])
        with self.assertRaises(PromotionError): self.select()
        self.cart([{'product_id':TEE,'size':'M','quantity':2},{'product_id':TAG,'size':'ONE SIZE','quantity':1}])
        self.select();q=self.db.growth_quote(420,self.catalog)
        self.assertEqual((q['subtotal_rub'],q['discount_rub'],q['total_rub']),(11700,300,11400))
        self.assertEqual(next(x for x in q['lines'] if x['product_id']==TAG)['discount_rub'],0)

    def test_fixed_discount_and_round_down_percentage_are_exact(self):
        self.catalog.get(TEE)['price']='99 ₽';self.cart()
        self.campaign(value=33,max_discount=100)
        self.assertEqual(self.db.promotion_candidate(420,'TEST10',self.catalog)['discount_rub'],32)
        self.campaign('FIXED',kind='fixed',value=20,max_discount=20)
        self.assertEqual(self.db.promotion_candidate(420,'FIXED',self.catalog)['total_rub'],79)

    def test_excessive_fixed_discount_is_rejected_not_silently_capped(self):
        self.campaign(kind='fixed',value=5000,max_discount=5000);self.cart();before=self.money()
        with self.assertRaises(PromotionError): self.select()
        self.assertIsNone(self.db.cart(420)['promotion']);self.assertEqual(self.money(),before)

    def test_start_inclusive_end_exclusive_and_unknown_price_never_zero_sale(self):
        now=int(time.time());self.campaign(starts_at=now+100,ends_at=now+200);self.cart()
        with self.assertRaises(PromotionError): self.select()
        with patch('growth_store.time.time',return_value=now+100): self.select()
        with patch('growth_store.time.time',return_value=now+200):
            with self.assertRaises(PromotionError): self.db.growth_quote(420,self.catalog)
        self.catalog.get(TEE)['price']='unknown'
        with patch('growth_store.time.time',return_value=now+150):
            with self.assertRaises(ValueError): self.db.growth_quote(420,self.catalog)

    def test_preview_selection_and_remove_are_not_money_or_contact_consent(self):
        self.campaign();self.cart();before=self.money()
        self.message('/promo TEST10');self.assertIsNone(self.db.cart(420)['promotion'])
        self.press('Применить к корзине');self.assertIsNotNone(self.db.cart(420)['promotion'])
        self.assertFalse(self.db.has_consent(420));self.assertEqual(self.money(),before)
        self.message('/promo');self.press('Снять промокод');self.assertIsNotNone(self.db.cart(420)['promotion'])
        self.press('Да, снять');self.assertIsNone(self.db.cart(420)['promotion']);self.assertEqual(self.money(),before)

    def test_selection_is_cart_cas_not_toggle_and_only_one_code(self):
        p=self.campaign();self.campaign('OTHER',value=20);self.cart()
        q=self.db.promotion_candidate(420,'TEST10',self.catalog);args=(420,p['promotion_id'],p['version'],q['cart_revision'],quote_fingerprint(q),'same-select',self.catalog)
        self.db.select_promotion(*args);self.select('OTHER');new=self.db.cart(420)
        self.db.select_promotion(*args);self.assertEqual(self.db.cart(420),new)
        with self.assertRaises(CartConflict): self.db.select_promotion(*args[:5],'stale-select',self.catalog)
        self.assertEqual(len(self.records('cart_promotions')),1)

    def test_cart_edits_keep_code_but_invalidate_old_price_confirmation(self):
        self.campaign();self.cart();self.select();payload=self.payload_with_promo()
        self.cart([{'product_id':TEE,'size':'M','quantity':2}])
        self.assertIsNotNone(self.db.cart(420)['promotion'])
        self.assertFalse(self.checkout(payload)['ok']);self.assertEqual(self.records('payments'),[])
        result=self.promo_checkout();self.assertTrue(result['ok'],result);self.assertEqual(result['amount_rub'],8820)

    def test_clearing_cart_removes_code_and_replay_does_not_restore_it(self):
        self.campaign();self.cart();self.select();self.cart([])
        self.assertIsNone(self.db.cart(420)['promotion']);self.assertEqual(self.records('promotion_redemptions'),[])

    def test_old_native_quote_rejects_price_category_visibility_or_state_change(self):
        for change in ('price','category','hide','stop'):
            with self.subTest(change=change):
                # A fresh independent fixture per mutation, no compensating writes.
                test=GrowthTests();test.setUp()
                try:
                    p=test.campaign();test.cart();test.select();payload=test.payload_with_promo()
                    if change=='price': test.catalog.get(TEE)['price']='5100 ₽'
                    elif change=='category': test.catalog.get(TEE)['category']='access'
                    elif change=='hide': test.catalog.get(TEE)['active']=False
                    else: test.db.set_promotion_active(9001,p['promotion_id'],False,p['version'],test.key(),**test.owner)
                    self.assertFalse(test.checkout(payload)['ok']);self.assertEqual(test.records('payments'),[])
                    self.assertIsNotNone(test.db.cart(420)['promotion'])
                finally: test.doCleanups()

    def test_stolen_or_expired_native_price_token_cannot_authorize_another_checkout(self):
        self.campaign(user_limit=2);self.cart();self.cart(uid=421);self.select();self.select(uid=421)
        payload=self.payload_with_promo();self.profile(421)
        self.assertFalse(self.checkout(payload,user={'id':421})['ok'])
        self.db.connection().execute('UPDATE chat_actions SET expires_at=0 WHERE token=?',(payload['promotion_token'],))
        self.assertFalse(self.checkout(payload)['ok']);self.assertEqual(self.records('promotion_redemptions'),[])

    def test_legacy_or_browser_checkout_cannot_silently_drop_selected_promotion(self):
        self.campaign();self.cart();self.select()
        result=self.checkout({**self.payload,'request_id':self.key()})
        self.assertFalse(result['ok']);self.assertEqual(result['code'],'promotion_requires_chat')
        self.assertEqual(self.records('payments'),[]);self.assertIsNotNone(self.db.cart(420)['promotion'])
        for fields in ({'promo_code':'TEST10'},{'discount_rub':490},{'promotion':{'total_rub':1}}):
            r=self.checkout({**self.payload,'request_id':self.key(),**fields});self.assertFalse(r['ok'])

    def test_unselected_fake_token_or_discount_never_lowers_full_price(self):
        self.cart()
        self.assertFalse(self.checkout({**self.payload,'promotion_token':'0'*16})['ok'])
        self.assertFalse(self.checkout({**self.payload,'discount':4899})['ok'])
        r=self.checkout({**self.payload,'request_id':self.key()});self.assertTrue(r['ok'],r);self.assertEqual(r['amount_rub'],4900)

    def test_checkout_snapshot_net_line_sum_invoice_and_original_catalog_agree(self):
        self.campaign();self.cart([{'product_id':TEE,'size':'M','quantity':1},{'product_id':TAG,'size':'ONE SIZE','quantity':1}]);self.select()
        result=self.promo_checkout();self.assertTrue(result['ok'],result)
        self.assertEqual(result['amount_rub'],6120);self.assertEqual(result['amount_stars'],3060)
        self.assertEqual(sum(r['amount_rub'] for r in self.records('orders')),6120)
        self.assertEqual(sorted(r['amount_rub'] for r in self.records('orders')),[1710,4410])
        discount=result['promotion'];self.assertEqual((discount['subtotal_rub'],discount['discount_rub'],discount['total_rub']),(6800,680,6120))
        self.assertEqual(sum(x['discount_rub'] for x in discount['allocations']),680)
        self.assertIsNone(self.db.cart(420)['promotion']);self.assertTrue(self.db.payment_is_payable(result['payment_id']))
        self.assertEqual(self.catalog.get(TEE)['price'],'4 900 ₽')
        self.assertEqual(self.api.create_invoice_link.call_args.args[0]['prices'][0]['amount'],3060)

    def test_duplicate_checkout_reuses_one_discount_even_after_stop_and_cart_edit(self):
        p=self.campaign();self.cart();self.select();payload=self.payload_with_promo()
        first=self.checkout(payload);self.assertTrue(first['ok'],first)
        self.cart([{'product_id':TAG,'size':'ONE SIZE','quantity':1}]);cart=self.db.cart(420)
        self.db.set_promotion_active(9001,p['promotion_id'],False,p['version'],self.key(),**self.owner)
        again=self.checkout(payload);self.assertEqual(again['payment_id'],first['payment_id']);self.assertEqual(again['promotion'],first['promotion'])
        self.assertEqual(self.db.cart(420),cart);self.assertEqual(len(self.records('promotion_redemptions')),1)
        self.assertFalse(self.checkout({**payload,'expected_total':1})['ok']);self.assertEqual(len(self.records('payments')),1)

    def test_confirmed_payment_and_conflicting_repeat_keep_discount_immutable(self):
        self.campaign();self.cart();self.select();r=self.promo_checkout();discount=copy.deepcopy(r['promotion'])
        self.confirm_stars(r,'discount-charge');self.confirm_stars(r,'discount-charge')
        self.db.mark_payment_paid(r['payment_id'],'stars','discount-charge',amount_minor=r['amount_stars']+1,currency='XTR',payer_id=420)
        self.assertEqual(self.db.purchase_promotion(r['payment_id']),discount)
        self.assertEqual(len(self.records('payment_receipts')),1);self.assertTrue(self.db.payment_attention(r['payment_id']))
        self.assertEqual(self.db.get_payment(r['payment_id'])['amount_rub'],4410)

    def test_cancellation_never_releases_promotion_quota_or_changes_old_sum(self):
        self.campaign();self.cart();self.select();r=self.promo_checkout()
        self.db.set_order_status(r['order_ids'][0],'cancelled',customer_id=420)
        self.cart()
        with self.assertRaises(PromotionError): self.select()
        self.assertEqual(self.db.get_payment(r['payment_id'])['amount_rub'],4410)
        self.assertEqual(len(self.records('promotion_redemptions')),1)

    def test_expiration_late_payment_keeps_discount_and_refund_guard(self):
        self.campaign();self.cart();self.select();r=self.promo_checkout()
        self.db.connection().execute('UPDATE stock_reservations SET expires_at=0');self.db.expire_reservations()
        self.confirm_stars(r,'late-discount')
        self.assertEqual(self.db.get_payment(r['payment_id'])['status'],'refund_required')
        self.assertEqual(self.db.purchase_promotion(r['payment_id'])['total_rub'],4410)
        self.assertEqual(self.records('stock_reservations')[0]['state'],'released')
        self.cart()
        with self.assertRaises(PromotionError): self.select()

    def test_global_last_use_is_atomic_between_two_customers(self):
        self.campaign(total_limit=1)
        payloads={}
        for uid in (420,421): self.cart(uid=uid);self.select(uid=uid);payloads[uid]=self.payload_with_promo(uid)
        barrier=threading.Barrier(2)
        def buy(uid):
            barrier.wait()
            try: return self.checkout(payloads[uid],user={'id':uid})
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(buy,(420,421)))
        self.assertEqual(sum(r['ok'] for r in results),1)
        self.assertEqual(len(self.records('promotion_redemptions')),1);self.assertEqual(len(self.records('payments')),1)
        loser=421 if results[0]['ok'] else 420
        self.assertTrue(self.db.cart(loser)['items']);self.assertIsNotNone(self.db.cart(loser)['promotion'])

    def test_quota_and_stop_are_rechecked_inside_purchase_transaction(self):
        p=self.campaign();self.cart();self.select();payload=self.payload_with_promo()
        original=self.db.create_checkout
        def stop_then_commit(*a,**kw):
            self.db.set_promotion_active(9001,p['promotion_id'],False,p['version'],'just-stopped',**self.owner)
            return original(*a,**kw)
        with patch.object(self.db,'create_checkout',side_effect=stop_then_commit): r=self.checkout(payload)
        self.assertFalse(r['ok']);self.assertEqual(self.records('payments'),[]);self.assertIsNotNone(self.db.cart(420)['promotion'])

    def test_out_of_stock_rolls_back_cart_selection_discount_and_all_money(self):
        self.campaign();self.cart();self.select();payload=self.payload_with_promo();self.set_count(0)
        before=self.money();cart=self.db.cart(420)
        r=self.checkout(payload);self.assertFalse(r['ok']);self.assertEqual(self.money(),before);self.assertEqual(self.db.cart(420),cart)

    def test_redemption_and_outbox_failure_roll_back_entire_purchase(self):
        self.bot.settings=replace(self.settings,manager_chat_id=9001)
        self.campaign();self.cart();self.select();payload=self.payload_with_promo()
        before=self.money();cart=self.db.cart(420)
        for method in ('record_promotion','enqueue_message'):
            with self.subTest(method=method),patch.object(self.db,method,side_effect=sqlite3.OperationalError('disk')):
                with self.assertRaises(sqlite3.OperationalError): self.checkout(payload)
            self.assertEqual(self.money(),before);self.assertEqual(self.db.cart(420),cart)
        self.assertTrue(self.checkout(payload)['ok']);self.assertEqual(len(self.records('promotion_redemptions')),1)

    def test_restarted_database_keeps_campaign_selection_and_immutable_discount(self):
        self.campaign(user_limit=2);self.cart();self.select()
        db=Database(self.db.path);self.addCleanup(db.close_current)
        self.assertEqual(db.growth_quote(420,self.catalog)['total_rub'],4410)
        r=self.promo_checkout();db2=Database(self.db.path);self.addCleanup(db2.close_current)
        self.assertEqual(db2.purchase_promotion(r['payment_id']),r['promotion']);self.assertEqual(db2.promotion_usage(1,420)['user'],1)

    def test_native_owner_wizard_publishes_only_after_two_distinct_confirmations(self):
        self.message('/newpromo',9001)
        self.message('NEW10',9001);self.press('Процент',9001);self.message('10',9001);self.message('500',9001)
        self.message('1900',9001);self.press('Весь каталог',9001);self.press('Сейчас',9001);self.press('7 дней',9001)
        self.message('10',9001);self.message('1',9001)
        self.assertEqual(self.records('promotions'),[])
        self.press('Создать выключенным',9001);self.assertFalse(self.records('promotions')[0]['active'])
        self.press('Включить промокод',9001);self.assertFalse(self.records('promotions')[0]['active'])
        token=self.press('Да, включить',9001);self.callback(token,9001)
        self.assertTrue(self.records('promotions')[0]['active']);self.assertEqual(len(self.records('promotions')),1)

    def test_native_promo_pause_resume_cancel_and_no_raw_invalid_text(self):
        self.campaign();self.cart();self.message('/promo');self.press('Ввести промокод')
        self.message('/menu');self.message('PRIVATE DATA +79995553322')
        self.assertNotIn('PRIVATE DATA','\n'.join(self.db.connection().iterdump()))
        self.message('/resume');self.message('TEST10');self.press('Применить к корзине')
        self.assertIsNotNone(self.db.cart(420)['promotion']);self.assertIsNone(self.db.get_state(420))
        self.message('/promo');self.press('Ввести промокод');self.message('/cancel')
        self.assertIsNone(self.db.get_state(420));self.assertIsNotNone(self.db.cart(420)['promotion'])

    def test_direct_promo_success_or_error_pauses_other_private_work(self):
        self.campaign();self.cart()
        for code in ('TEST10','WRONGCODE'):
            self.db.set_state(420,'ops_input',{'token':'untouched','field':'body'})
            self.message('/promo '+code)
            state=self.db.get_state(420);self.assertTrue(state[1]['paused']);self.assertEqual(state[1]['token'],'untouched')
            self.message('NEXT RETRY IS NOT SUPPORT')
            self.assertNotIn('NEXT RETRY','\n'.join(self.db.connection().iterdump()))

    def test_processed_promo_and_owner_inputs_never_enter_new_contact_draft(self):
        self.campaign();self.cart();self.message('/promo TEST10',mid=171)
        self.db.set_state(420,'commerce_field',{'field':'address','next':'profile'});state=self.db.get_state(420)
        self.message('/promo TEST10',mid=171);self.assertEqual(self.db.get_state(420),state)
        self.message('OTHER SAME ID',mid=171);self.assertEqual(self.db.get_state(420),state)
        self.assertNotIn('OTHER SAME ID','\n'.join(self.db.connection().iterdump()))
        self.message('/newpromo',9001);self.message('OWNER10',9001,mid=172)
        self.db.set_state(9001,'fin_input',{'token':'keep'});state=self.db.get_state(9001)
        self.message('OWNER10',9001,mid=172);self.assertEqual(self.db.get_state(9001),state)

    def test_stale_owner_step_and_expired_wizard_do_not_publish(self):
        self.message('/newpromo',9001);self.message('EXPIRY',9001)
        token=self.press('Процент',9001)
        self.callback(token,9001);self.assertEqual(self.db.get_state(9001)[1]['field'],'value')
        self.message('/newpromo',9001);state=self.db.get_state(9001)
        self.callback(token,9001);self.assertEqual(self.db.get_state(9001),state)
        self.db.set_state(9001,'growth_create',{**state[1],'expires_at':0})
        self.message('TOO-LATE',9001);self.assertEqual(self.records('promotions'),[])

    def test_promotion_callbacks_are_owner_scoped_and_private(self):
        self.campaign();self.cart();self.message('/promo TEST10')
        token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Применить к корзине' in b['text'])
        self.callback(token,421);self.assertIsNone(self.db.cart(420)['promotion'])
        self.api.reset_mock();self.callback(token,420,private=False);self.api.send_message.assert_not_called()
        self.assertIsNone(self.db.cart(420)['promotion'])
        self.callback(token,420);self.assertIsNotNone(self.db.cart(420)['promotion'])

    def test_native_discounted_checkout_preview_and_final_purchase_match(self):
        self.campaign();self.cart();self.profile();self.message('/promo TEST10');self.press('Применить к корзине')
        self.press('Оформить покупку');text=self.panel()[0]
        self.assertIn('4 410 ₽',text);self.assertIn('−490 ₽',text)
        token=self.press('Всё верно');self.callback(token)
        self.assertEqual(len(self.records('payments')),1);self.assertEqual(len(self.records('promotion_redemptions')),1)
        self.message('/purchases');self.press('№0001');self.assertIn('−490 ₽',self.panel()[0])

    def test_repeat_merges_current_cart_at_current_price_without_any_new_money(self):
        r=self.buy();self.cart([{'product_id':TAG,'size':'ONE SIZE','quantity':1}]);self.catalog.get(TEE)['price']='5300 ₽'
        before=self.money();profile=self.db.account_profile(420)
        q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);self.assertEqual(q['total_rub'],7200)
        result=self.repeat(r)
        self.assertEqual(len(result['items']),2);self.assertEqual(self.money(),before);self.assertEqual(self.db.account_profile(420),profile)

    def test_repeat_clears_new_personalization_and_promotion_not_current_contacts(self):
        r=self.buy([{'product_id':TAG,'size':'ONE SIZE','quantity':1,'person':'00123'}])
        self.campaign();self.cart();self.select();self.profile()
        profile=self.db.account_profile(420);self.repeat(r)
        tag=next(x for x in self.db.cart(420)['items'] if x['product_id']==TAG)
        self.assertEqual(tag['person'],'');self.assertIsNone(self.db.cart(420)['promotion']);self.assertEqual(self.db.account_profile(420),profile)
        self.assertEqual(self.db.orders_for_payment(r['payment_id'])[0]['person'],'00123')

    def test_repeat_requires_owner_paid_uncontested_source_not_a_staff_shortcut(self):
        r=self.buy(paid=False)
        for uid in (420,421,9001,9005):
            with self.subTest(uid=uid),self.assertRaises(ValueError): self.db.repeat_preview(uid,r['purchase_id'],self.catalog)
        self.confirm_stars(r,'first-money')
        self.db.mark_payment_paid(r['payment_id'],'stars','different-money',amount_minor=r['amount_stars'],currency='XTR',payer_id=420)
        with self.assertRaises(ValueError): self.db.repeat_preview(420,r['purchase_id'],self.catalog)
        self.assertEqual(self.db.cart(420)['items'],[])

    def test_repeat_hidden_removed_size_invalid_price_and_unknown_stock_are_all_or_nothing(self):
        for changed in ('hidden','size','price','unknown','zero'):
            test=GrowthTests();test.setUp()
            try:
                r=test.buy();test.cart([{'product_id':TAG,'size':'ONE SIZE','quantity':1}]);before=test.db.cart(420)
                if changed=='hidden': test.catalog.get(TEE)['active']=False
                elif changed=='size': test.catalog.get(TEE)['sizes']=['S']
                elif changed=='price': test.catalog.get(TEE)['price']='bad'
                elif changed=='unknown': test.db.connection().execute('UPDATE stock_items SET on_hand=NULL WHERE product_id=?',(TEE,))
                else: test.set_count(0)
                with self.subTest(changed=changed),self.assertRaises(ValueError): test.db.repeat_preview(420,r['purchase_id'],test.catalog)
                self.assertEqual(test.db.cart(420),before)
            finally: test.doCleanups()

    def test_repeat_checks_combined_quantity_per_sku_and_does_not_shrink_order(self):
        r=self.buy();self.set_count(1);self.cart();before=self.db.cart(420)
        with self.assertRaises(ValueError): self.db.repeat_preview(420,r['purchase_id'],self.catalog)
        self.assertEqual(self.db.cart(420),before)
        self.cart([{'product_id':TEE,'size':'M','quantity':20}]);self.set_count(100)
        with self.assertRaises(ValueError): self.db.repeat_preview(420,r['purchase_id'],self.catalog)

    def test_repeat_stale_price_stock_cart_or_source_version_requires_new_preview(self):
        r=self.buy();q=self.db.repeat_preview(420,r['purchase_id'],self.catalog)
        self.catalog.get(TEE)['price']='5100 ₽'
        with self.assertRaises(CartConflict): self.db.repeat_purchase(420,r['purchase_id'],quote_fingerprint(q),self.key(),self.catalog)
        self.assertEqual(self.db.cart(420)['items'],[])
        q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);self.set_count(555)
        with self.assertRaises(CartConflict): self.db.repeat_purchase(420,r['purchase_id'],quote_fingerprint(q),self.key(),self.catalog)
        q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);self.cart()
        with self.assertRaises(CartConflict): self.db.repeat_purchase(420,r['purchase_id'],quote_fingerprint(q),self.key(),self.catalog)

    def test_repeat_retry_is_idempotent_after_newer_cart_and_after_restart(self):
        r=self.buy();q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);expected=quote_fingerprint(q)
        self.db.repeat_purchase(420,r['purchase_id'],expected,'repeat-same',self.catalog)
        self.cart([{'product_id':TAG,'size':'ONE SIZE','quantity':2}]);current=self.db.cart(420)
        db=Database(self.db.path);self.addCleanup(db.close_current)
        db.repeat_purchase(420,r['purchase_id'],expected,'repeat-same',self.catalog)
        self.assertEqual(db.cart(420),current);self.assertEqual(len(self.records('payments')),1)

    def test_repeat_rollback_keeps_original_cart_and_selected_coupon(self):
        r=self.buy();self.campaign();self.cart();self.select();q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);cart=self.db.cart(420)
        with patch.object(self.db,'_service_done',side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): self.db.repeat_purchase(420,r['purchase_id'],quote_fingerprint(q),'rollback-repeat',self.catalog)
        self.assertEqual(self.db.cart(420),cart);self.assertIsNone(self.db.service_result(420,'rollback-repeat'))
        self.assertEqual(len(self.records('payments')),1)

    def test_repeat_concurrent_confirmations_do_not_double_add(self):
        r=self.buy();q=self.db.repeat_preview(420,r['purchase_id'],self.catalog);expected=quote_fingerprint(q)
        def repeat(i):
            try: self.db.repeat_purchase(420,r['purchase_id'],expected,'race-repeat-'+str(i),self.catalog);return True
            except CartConflict: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(repeat,[1,2]))
        self.assertEqual(sum(results),1);self.assertEqual(self.db.cart(420)['items'][0]['quantity'],1)

    def test_repeat_uses_new_full_price_not_historical_discount(self):
        self.campaign();self.cart();self.select();r=self.promo_checkout();self.confirm_stars(r,'old-discount')
        self.repeat(r);self.assertIsNone(self.db.cart(420)['promotion'])
        self.assertEqual(self.db.growth_quote(420,self.catalog)['total_rub'],4900)
        new=self.checkout(self.payload_with_promo());self.assertTrue(new['ok'],new)
        self.assertNotEqual(new['payment_id'],r['payment_id']);self.assertEqual(new['amount_rub'],4900)
        self.assertEqual(self.db.purchase_promotion(r['payment_id'])['total_rub'],4410)

    def test_native_repeat_is_explicit_scoped_and_never_a_checkout(self):
        r=self.buy();before=self.money()
        self.message('/purchases');self.press('№0001');self.press('Повторить состав')
        self.assertEqual(self.db.cart(420)['items'],[])
        token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Добавить состав' in b['text'])
        self.callback(token,421);self.assertEqual(self.db.cart(420)['items'],[])
        self.callback(token);self.callback(token);self.assertEqual(self.db.cart(420)['items'][0]['quantity'],1)
        self.assertEqual(self.money(),before)

    def test_ui_escapes_catalog_data_and_keeps_telegram_lengths(self):
        self.catalog.get(TEE)['name']='<script> & 😀' * 8
        r=self.buy();self.callback('g:repeat:'+str(r['purchase_id']))
        self.assertNotIn('<script>',self.panel()[0])
        self.campaign();self.cart();self.message('/promo TEST10')
        for call in self.api.mock_calls:
            if call[0] not in {'send_message','edit_message_text'}: continue
            text=html.unescape(re.sub(r'<[^>]*>','',call.args[-2]));self.assertLessEqual(len(text.encode('utf-16-le'))//2,3900)
            markup=call.args[-1]
            if not isinstance(markup,dict): continue
            for row in markup.get('inline_keyboard',[]):
                for b in row:
                    if b.get('callback_data'): self.assertLessEqual(len(b['callback_data'].encode()),64)

    def test_pruning_removes_expired_wizard_not_campaigns_or_discount_history(self):
        self.campaign();self.cart();self.select();r=self.promo_checkout();before=self.money()
        self.db.set_state(9001,'growth_create',{'expires_at':0})
        self.message('/promo TEST10',mid=273)  # Empty cart: no successful raw-text marker.
        self.db.prune_growth()
        self.assertIsNone(self.db.get_state(9001));self.assertEqual(self.money(),before)
        self.assertEqual(len(self.records('promotions')),1);self.assertEqual(len(self.records('promotion_redemptions')),1)

    def test_cart_api_is_owner_scoped_read_only_for_promotion_and_blocks_legacy_checkout(self):
        self.campaign();self.cart();self.select()
        request=self.http()
        status,data=request('/api/cart');self.assertEqual(status,200);self.assertEqual(data['cart']['promotion']['code'],'TEST10')
        for uid in (421,9001):
            _,data=request('/api/cart?user_id=420',uid=uid);self.assertIsNone(data['cart']['promotion'])
        status,data=request('/api/checkout',data={**self.payload,'request_id':self.key()})
        self.assertEqual(status,400);self.assertEqual(data['code'],'promotion_requires_chat');self.assertEqual(self.records('payments'),[])
        self.assertEqual(request('/api/cart',valid=False)[0],401)

    def test_discount_allocation_keeps_distinct_personalizations_and_one_physical_reservation(self):
        self.campaign(max_discount=1000)
        self.cart([{'product_id':TAG,'size':'ONE SIZE','quantity':1,'person':'1'},
                   {'product_id':TAG,'size':'ONE SIZE','quantity':1,'person':'00234'},
                   {'product_id':TEE,'size':'M','quantity':2}]);self.select()
        r=self.promo_checkout();self.assertTrue(r['ok'],r);self.assertEqual(r['amount_rub'],12600)
        lines=self.db.orders_for_payment(r['payment_id'])
        self.assertEqual({x['person'] for x in lines if x['product_id']==TAG},{'1','00234'})
        self.assertEqual(len(r['promotion']['allocations']),3)
        self.assertEqual(sum(x['discount_rub'] for x in r['promotion']['allocations']),1000)
        self.assertEqual(len(self.records('stock_reservations')),2)
        self.assertEqual(self.db.stock_item(TAG,'ONE SIZE')['reserved'],2)

    def test_repeat_can_merge_into_twenty_unique_lines_without_counting_raw_duplicates(self):
        r=self.buy();candidate=copy.deepcopy(self.catalog.data)
        for i in range(19):
            p=copy.deepcopy(candidate['products'][0]);p.update(id='growth-fixture-'+str(i),name='Только тест '+str(i),sizes=['S'],price='100 ₽')
            candidate['products'].append(p)
        self.catalog.save(candidate);self.db.sync_inventory(self.catalog)
        self.db.connection().execute("UPDATE stock_items SET on_hand=100 WHERE product_id LIKE 'growth-fixture-%'")
        self.cart([{'product_id':TEE,'size':'M','quantity':1}]+[{'product_id':'growth-fixture-'+str(i),'size':'S','quantity':1} for i in range(19)])
        self.repeat(r);cart=self.db.cart(420)
        self.assertEqual(len(cart['items']),20);self.assertEqual(next(x for x in cart['items'] if x['product_id']==TEE)['quantity'],2)
        self.assertEqual(len(self.records('payments')),1)

    def test_tampered_prepared_discount_quote_is_rejected_before_money_or_cart_mutation(self):
        self.campaign();self.cart();self.select();payload=self.payload_with_promo();before=self.money();cart=self.db.cart(420)
        original=self.db.create_checkout
        def tamper(*a,**kw):
            kw['promotion']={**kw['promotion'],'subtotal_rub':kw['promotion']['subtotal_rub']+1}
            return original(*a,**kw)
        with patch.object(self.db,'create_checkout',side_effect=tamper): result=self.checkout(payload)
        self.assertFalse(result['ok']);self.assertEqual(self.money(),before);self.assertEqual(self.db.cart(420),cart)

    def http(self):
        server=start_health_server(0,self.catalog,self.settings,self.db,self.api)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        root='http://127.0.0.1:'+str(server.server_address[1])
        def request(path,uid=420,data=None,valid=True):
            fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':uid})}
            secret=hmac.new(b'WebAppData',self.settings.token.encode(),hashlib.sha256).digest()
            fields['hash']=hmac.new(secret,'\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(),hashlib.sha256).hexdigest()
            req=urllib.request.Request(root+path,headers={'X-Telegram-Init-Data':urllib.parse.urlencode(fields) if valid else 'invalid','Content-Type':'application/json'},data=json.dumps(data).encode() if data is not None else None)
            try: response=urllib.request.urlopen(req,timeout=10)
            except urllib.error.HTTPError as exc: response=exc
            with response:
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                return response.status,json.load(response)
        return request


if __name__=='__main__': unittest.main()
