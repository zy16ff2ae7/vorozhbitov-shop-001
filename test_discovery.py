"""Offline discovery acceptance; all extra products and counts are test fixtures."""
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
from commerce_store import CartConflict
from discovery_store import DEFAULT_FILTERS, parse_search, catalog_snapshot, stock_state
import test_commerce as commerce
import test_operations as operations

TEE = 'tee-sila-i-chest'
TAG = 'tag-sila-i-chest'


class DiscoveryTests(unittest.TestCase):
    callback = commerce.CommerceTests.callback
    panel = commerce.CommerceTests.panel
    press = commerce.CommerceTests.press
    message = operations.OperationsTests.message
    checkout = commerce.CommerceTests.checkout
    confirm_stars = commerce.CommerceTests.confirm_stars
    profile = commerce.CommerceTests.profile
    set_count = commerce.CommerceTests.set_count

    def setUp(self):
        commerce.CommerceTests.setUp(self)
        self.msg_id = 1000
        self.owner = {'owner_ids': self.settings.admin_ids}

    def records(self, table):
        return [dict(r) for r in self.db.connection().execute('SELECT * FROM ' + table)]

    def parse(self, query):
        products, categories = catalog_snapshot(self.catalog)
        return parse_search(query, products, categories)

    def search(self, query, uid=420, key='search-one'):
        s = self.db.discovery_session(uid)
        return self.db.search_discovery(uid, query, s['generation'], s['revision'], key, self.catalog)

    def filters(self, changes, uid=420, key='filter-one', **kwargs):
        s = self.db.discovery_session(uid)
        return self.db.filter_discovery(uid, changes, s['generation'], s['revision'], key, self.catalog, **kwargs)

    def browse(self, uid=420, page=0): return self.db.browse_discovery(uid, self.catalog, page)

    def enable(self, uid=420, key='consent-one'):
        a = self.db.discovery_account(uid)
        return self.db.discovery_consent(uid, True, a['generation'], key)

    def pin(self, pid=TEE, kind='saved', uid=420, add=True, key='pin-one'):
        a = self.db.discovery_account(uid)
        return self.db.change_collection(uid, kind, pid, add, a['generation'], a[kind + '_revision'], key, self.catalog)

    def snapshot_money(self):
        return {t: self.records(t) for t in ('payments','orders','purchases','stock_items','stock_reservations','stock_movements','payment_receipts','payment_invoices','payment_invoice_attempts','notifications')}

    def extra(self, count):
        candidate = copy.deepcopy(self.catalog.data)
        for i in range(count):
            p = copy.deepcopy(candidate['products'][0])
            p.update(id='discovery-fixture-' + str(i), name='Вымышленная вещь ' + str(i), price=f'{100 + i} ₽', description='Только тестовый товар', sizes=['S'])
            candidate['products'].append(p)
        self.catalog.save(candidate)
        self.db.sync_inventory(self.catalog)  # New counts intentionally stay unknown.

    def test_schema_is_additive_idempotent_and_not_an_implicit_consent(self):
        before = self.snapshot_money()
        self.db.init_discovery_store(); self.db.init_discovery_store()
        self.assertEqual(self.records('discovery_accounts'), [])
        self.assertEqual(self.records('discovery_items'), [])
        self.assertFalse(self.db.discovery_account(420)['enabled'])
        self.assertEqual(self.snapshot_money(), before)
        self.assertEqual(list(self.db.connection().execute('PRAGMA foreign_key_check')), [])

    def test_russian_phrase_parses_words_size_budget_and_stock(self):
        d = self.parse('Чёрная футболка M до 5 000 ₽ в наличии')
        self.assertEqual(d['size'], 'M'); self.assertEqual(d['max_price'], 5000)
        self.assertEqual(d['stock'], 'available'); self.assertEqual(d['scope'], [TEE])
        self.assertNotIn('query', d)
        self.assertEqual(self.parse('хочу черную футболку XL от 1900 до 4900')['scope'], [TEE])
        self.assertEqual(self.parse('black tee XXL')['size'], 'XXL')
        self.assertEqual(self.parse('жетон ONE SIZE до 1900')['scope'], [TAG])

    def test_search_has_no_hidden_product_or_unverified_colour_fallback(self):
        for query in ('фиолетовая футболка M', 'hoodie', 'контакты +79990000000', 'футболка https://evil.test', 'не чёрная футболка', 'футболка или жетон', 'без принта'):
            with self.subTest(query=query), self.assertRaises(ValueError): self.search(query)
        self.assertEqual(self.db.discovery_session(420)['filters'], DEFAULT_FILTERS)
        self.assertNotIn('фиолетовая', '\n'.join(self.db.connection().iterdump()))

    def test_invalid_numeric_queries_do_not_guess_price_or_currency(self):
        for query in ('до -100', 'до 1.5', 'до 1,50', 'до 10 USD', 'до 10000001', 'от 5000 до 1900', 'до 5000 до 6000', 'M XL', 'S\nM', '', 'x' * 161):
            with self.subTest(query=query), self.assertRaises(ValueError): self.parse(query)
        self.assertEqual(self.parse('до 0')['max_price'], 0)
        self.assertEqual(self.parse('от 1 до 10000000')['min_price'], 1)

    def test_lexical_and_not_or_matching_has_honest_no_results(self):
        self.search('футболка сталь')
        self.assertEqual(self.browse()['total'], 0)
        self.assertFalse(self.browse()['stale'])
        self.search('хлопок', key='cotton')
        self.assertEqual([p['product_id'] for p in self.browse()['items']], [TEE])

    def test_size_and_stock_conditions_apply_to_the_same_variant(self):
        self.set_count(0, size='M'); self.set_count(3, size='S')
        self.search('футболка M в наличии')
        self.assertEqual(self.browse()['total'], 0)
        self.search('футболка S в наличии', key='size-S')
        self.assertEqual(self.browse()['total'], 1)
        self.search('футболка M нет в наличии', key='sold-out')
        self.assertEqual(self.browse()['total'], 1)

    def test_unknown_stock_never_comes_from_badges_or_is_treated_as_zero(self):
        self.db.connection().execute('UPDATE stock_items SET on_hand=NULL WHERE product_id=?', (TEE,))
        self.catalog.get(TEE)['stock_label'] = 'В наличии 999 штук!'
        self.search('футболка M в наличии'); self.assertEqual(self.browse()['total'], 0)
        self.search('футболка M нет в наличии', key='not-zero'); self.assertEqual(self.browse()['total'], 0)
        self.search('футболка M наличие уточняется', key='unknown'); self.assertEqual(self.browse()['total'], 1)
        self.assertEqual(stock_state([{'available': None},{'available': 0}]), 'unknown')
        self.assertEqual(stock_state([{'available': None},{'available': 2}]), 'available')

    def test_prices_are_live_and_sort_does_not_parse_labels_or_invalid_price_as_zero(self):
        self.filters({'sort': 'price_asc'})
        self.assertEqual([p['product_id'] for p in self.browse()['items']], [TAG,TEE])
        self.catalog.get(TAG)['price'] = '12 000 ₽'
        self.assertEqual([p['product_id'] for p in self.browse()['items']], [TEE,TAG])
        self.catalog.get(TAG)['price'] = 'not a price'
        self.filters({'sort': 'price_desc'}, key='desc')
        self.assertEqual([p['product_id'] for p in self.browse()['items']], [TEE,TAG])
        self.assertIsNone(self.browse()['items'][-1]['price_rub'])
        self.filters({'max_price': 100000}, key='budget')
        self.assertEqual([p['product_id'] for p in self.browse()['items']], [TEE])

    def test_lexical_scope_stales_on_text_visibility_or_new_products_not_price_stock(self):
        self.search('черная футболка')
        self.catalog.get(TEE)['price'] = '6 000 ₽'; self.set_count(0)
        self.assertFalse(self.browse()['stale']); self.assertEqual(self.browse()['items'][0]['price_rub'], 6000)
        self.catalog.get(TEE)['description'] = 'Белая футболка нового описания'
        self.assertTrue(self.browse()['stale']); self.assertEqual(self.browse()['total'], 0)
        self.filters({}, key='reset', reset=True)
        self.assertFalse(self.browse()['stale']); self.assertEqual(self.browse()['total'], 2)
        self.search('жетон', key='tag'); self.extra(1)
        self.assertTrue(self.browse()['stale'])

    def test_price_only_search_remains_live_without_lexical_snapshot(self):
        self.search('до 2000')
        self.assertIsNone(self.db.discovery_session(420)['filters']['scope'])
        self.assertEqual(self.browse()['total'], 1)
        self.extra(2)
        self.assertFalse(self.browse()['stale']); self.assertEqual(self.browse()['total'], 3)

    def test_search_parameters_are_per_user_and_raw_text_never_persists(self):
        query = 'Покажи пожалуйста ЧЁРНУЮ футболку M до 5000'
        self.search(query)
        self.assertNotIn(query, '\n'.join(self.db.connection().iterdump()))
        self.assertEqual(self.db.discovery_session(421)['filters'], DEFAULT_FILTERS)
        for table in ('events','states','chat_actions','service_requests'):
            self.assertNotIn('ЧЁРНУЮ', json.dumps(self.records(table), ensure_ascii=False))
        self.assertEqual(self.browse(420)['total'], 1); self.assertEqual(self.browse(421)['total'], 2)

    def test_session_expiry_has_new_generation_and_cannot_reuse_old_callback(self):
        s = self.search('M')
        self.db.connection().execute('UPDATE discovery_sessions SET expires_at=0 WHERE user_id=420')
        new = self.db.discovery_session(420)
        self.assertNotEqual(s['generation'], new['generation'])
        self.assertEqual(new['filters'], DEFAULT_FILTERS)
        with self.assertRaises(CartConflict): self.db.filter_discovery(420, {'size':'S'}, s['generation'], s['revision'], 'expired-filter', self.catalog)

    def test_filters_use_cas_and_processed_repeat_does_not_overwrite_newer_choice(self):
        old = self.db.discovery_session(420)
        self.db.filter_discovery(420, {'size':'M'}, old['generation'], old['revision'], 'size', self.catalog)
        self.filters({'stock':'available'}, key='stock')
        current = copy.deepcopy(self.db.discovery_session(420))
        self.db.filter_discovery(420, {'size':'M'}, old['generation'], old['revision'], 'size', self.catalog)
        self.assertEqual(self.db.discovery_session(420), current)
        with self.assertRaises(CartConflict): self.db.filter_discovery(420, {'size':'S'}, old['generation'], old['revision'], 'other-old', self.catalog)
        with self.assertRaises(ValueError): self.db.filter_discovery(420, {'size':'L'}, old['generation'], old['revision'], 'size', self.catalog)

    def test_invalid_filter_keys_values_and_booleans_rejected(self):
        for changes in ({'scope':[TEE]}, {'size':'NOT-SIZE'}, {'category':'hoodie'}, {'max_price':True}, {'max_price':1.5}, {'min_price':-1}, {'stock':'fake'}, {'sort':'sales'}, {'user_id':421}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.filters(changes)
        self.assertEqual(self.db.discovery_session(420)['filters'], DEFAULT_FILTERS)

    def test_filter_concurrency_has_one_winner(self):
        s = self.db.discovery_session(420)
        def change(size):
            try:
                self.db.filter_discovery(420, {'size':size}, s['generation'], s['revision'], 'size-' + size, self.catalog)
                return True
            except CartConflict: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(change, ['M','S']))
        self.assertEqual(sum(results), 1)

    def test_default_facets_and_pagination_include_only_active_products(self):
        data = self.browse()
        self.assertEqual(data['total'], 2); self.assertEqual(set(data['categories']), {'drop','access'})
        self.assertEqual(data['prices'], [1900,4900])
        self.extra(9)
        ids=[]
        for page in range(3): ids.extend(p['product_id'] for p in self.browse(page=page)['items'])
        self.assertEqual(len(ids), 11); self.assertEqual(len(set(ids)), 11)
        self.assertFalse(self.browse(page=2)['has_more'])

    def test_lists_require_separate_consent_not_contact_or_marketing_consent(self):
        self.profile()
        with self.assertRaises(PermissionError): self.pin()
        with self.assertRaises(PermissionError): self.db.collection_view(420,'saved',self.catalog)
        self.assertEqual(self.records('discovery_items'), [])
        self.enable(421)
        self.assertFalse(self.db.has_consent(421))
        self.pin(uid=421)
        self.assertEqual(self.db.collection_view(421,'saved',self.catalog)['total'], 1)
        self.assertEqual(self.records('notifications'), [])

    def test_consent_and_first_pin_are_one_native_transaction(self):
        self.callback('c:product:' + TEE); self.press('☆ В избранное')
        self.assertIn('СПИСКИ / СОГЛАСИЕ', self.panel()[0])
        self.assertEqual(self.records('discovery_items'), [])
        token = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if 'Разрешить и добавить' in b['text'])
        with patch.object(self.db, 'change_collection', side_effect=RuntimeError('disk')):
            # Call controller directly so the exception is observable rather than logged by handle_update.
            with self.assertRaises(RuntimeError): self.bot.discovery.dispatch(420,420,token.split(':')[-1])
        self.assertFalse(self.db.discovery_account(420)['enabled'])
        self.callback(token)
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'], 1)
        self.callback(token)
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'], 1)

    def test_hidden_product_before_consent_prevents_implicit_enable_or_add(self):
        self.callback('c:product:' + TEE); self.press('☆ В избранное')
        self.catalog.get(TEE)['active'] = False
        self.press('Разрешить и добавить')
        self.assertFalse(self.db.discovery_account(420)['enabled'])
        self.assertEqual(self.records('discovery_items'), [])

    def test_private_lists_do_not_follow_a_staff_role_or_other_user(self):
        self.enable(); self.pin()
        for actor in (421,9001,9002,9003):
            self.enable(actor, key='actor-' + str(actor))
            self.assertEqual(self.db.collection_view(actor,'saved',self.catalog)['items'], [])
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'], 1)
        self.assertEqual(len(self.records('discovery_items')), 1)

    def test_list_retries_are_idempotent_not_toggle_and_wrong_body_is_rejected(self):
        a=self.enable(); v=a['saved_revision']
        self.db.change_collection(420,'saved',TEE,True,a['generation'],v,'pin',self.catalog)
        self.pin(TAG,key='tag')
        self.db.change_collection(420,'saved',TEE,True,a['generation'],v,'pin',self.catalog)
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'], 2)
        with self.assertRaises(ValueError): self.db.change_collection(420,'saved',TEE,False,a['generation'],v,'pin',self.catalog)
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'], 2)

    def test_saved_and_compare_have_independent_revisions(self):
        a=self.enable()
        self.pin(TEE,kind='compare')
        self.db.change_collection(420,'saved',TAG,True,a['generation'],a['saved_revision'],'saved-after-compare',self.catalog)
        self.assertEqual(self.db.collection_view(420,'compare',self.catalog)['total'],1)
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'],1)

    def test_compare_limit_is_atomic_under_competing_third_additions(self):
        self.extra(2); self.enable(); self.pin(TEE,'compare'); self.pin(TAG,'compare',key='second')
        a=self.db.discovery_account(420)
        def add(pid):
            try:
                self.db.change_collection(420,'compare',pid,True,a['generation'],a['compare_revision'],pid,self.catalog)
                return True
            except ValueError: return False
            finally: self.db.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(add,['discovery-fixture-0','discovery-fixture-1']))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.db.collection_view(420,'compare',self.catalog)['total'],3)
        with self.assertRaises(ValueError): self.pin('discovery-fixture-1' if results[0] else 'discovery-fixture-0','compare',key='fourth')

    def test_favorite_limit_fifty_does_not_partially_write(self):
        self.extra(51); self.enable()
        for i in range(50): self.pin('discovery-fixture-'+str(i),key='favorite-'+str(i))
        a=self.db.discovery_account(420)
        with self.assertRaises(ValueError): self.pin('discovery-fixture-50',key='over-limit')
        self.assertEqual(self.db.discovery_account(420),a)
        self.assertEqual(len(self.records('discovery_items')),50)
        self.assertIsNone(self.db.service_result(420,'over-limit'))

    def test_hidden_saved_item_can_be_removed_without_disclosing_hidden_metadata(self):
        self.enable(); self.pin()
        self.catalog.get(TEE)['active']=False
        item=self.db.collection_view(420,'saved',self.catalog)['items'][0]
        self.assertFalse(item['active']); self.assertNotIn('price_rub',item); self.assertNotIn('description',item)
        self.pin(add=False,key='remove-hidden')
        self.assertEqual(self.db.collection_view(420,'saved',self.catalog)['total'],0)
        with self.assertRaises(ValueError): self.pin(key='old-published-card')

    def test_withdrawal_clears_choices_and_old_buttons_but_preserves_cart_profile_money(self):
        self.enable(); self.pin(); self.pin(TAG,'compare',key='compare')
        self.search('черная футболка M')
        self.profile(); self.db.replace_cart(420,[{'product_id':TEE,'size':'M','person':'','quantity':1}],0,'cart',self.catalog)
        cart=self.db.cart(420); profile=self.db.account_profile(420); money=self.snapshot_money()
        self.callback('c:product:' + TAG)
        old = next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if '☆ В избранное' in b['text'])
        a=self.db.discovery_account(420)
        self.db.discovery_consent(420,False,a['generation'],'withdraw')
        self.assertEqual(self.records('discovery_items'),[]); self.assertEqual(self.records('discovery_sessions'),[])
        self.assertEqual(self.db.cart(420),cart); self.assertEqual(self.db.account_profile(420),profile); self.assertEqual(self.snapshot_money(),money)
        self.callback(old); self.assertEqual(self.records('discovery_items'),[])
        self.assertEqual([r for r in self.records('chat_actions') if r['kind'].startswith('discovery.')],[])

    def test_old_consent_and_unprocessed_pin_cannot_resurrect_after_reenable(self):
        before=self.db.discovery_account(420)
        a=self.enable(key='original-consent')
        self.db.discovery_consent(420,False,a['generation'],'withdraw')
        self.enable(key='new-consent')
        with self.assertRaises(ValueError): self.db.discovery_consent(420,True,before['generation'],'original-consent')
        with self.assertRaises(CartConflict): self.db.change_collection(420,'saved',TEE,True,a['generation'],a['saved_revision'],'old-unprocessed-pin',self.catalog)
        self.assertEqual(self.records('discovery_items'),[])

    def test_stale_unprocessed_consent_is_rejected_after_explicit_disable(self):
        self.db.discovery_consent(420,False,0,'disabled-before-start')
        with self.assertRaises(CartConflict): self.db.discovery_consent(420,True,0,'stale-first-enable')
        self.assertFalse(self.db.discovery_account(420)['enabled'])

    def test_collection_and_consent_failures_rollback_side_effects_and_markers(self):
        self.enable(); a=self.db.discovery_account(420)
        with patch.object(self.db,'_service_done',side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError): self.pin()
        self.assertEqual(self.records('discovery_items'),[]); self.assertEqual(self.db.discovery_account(420),a)
        self.pin()
        before=self.records('discovery_items')
        with patch.object(self.db,'event',side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError): self.db.discovery_consent(420,False,a['generation'],'atomic-forget')
        self.assertEqual(self.records('discovery_items'),before); self.assertTrue(self.db.discovery_account(420)['enabled'])

    def test_list_and_search_restart_recover_without_marketplace_side_effects(self):
        self.enable(); self.pin(); self.pin(TAG,'compare',key='compare'); self.search('M до 5000')
        before=self.snapshot_money()
        restarted=Database(self.db.path); self.addCleanup(restarted.close_current)
        self.assertEqual(restarted.collection_view(420,'saved',self.catalog)['items'][0]['product_id'],TEE)
        self.assertEqual(restarted.browse_discovery(420,self.catalog)['total'],1)
        self.assertEqual(self.snapshot_money(),before)

    def test_all_discovery_actions_never_create_payment_reservation_or_notification(self):
        before=self.snapshot_money()
        self.search('черная футболка M до 4900'); self.enable(); self.pin(); self.pin(TEE,'compare',key='compare'); self.pin(TAG,'compare',key='compare-tag')
        self.callback('d:compare'); self.callback('d:saved'); self.callback('d:settings'); self.press('Удалить выбор'); self.press('Да, удалить и отключить')
        self.assertEqual(self.snapshot_money(),before)

    def test_native_filter_roundtrip_uses_current_results_and_existing_cart_flow(self):
        self.message('/browse'); self.press('Фильтры'); self.press('Размер'); self.press('M')
        self.assertIn('Размер: M',self.panel()[0])
        self.assertIn('Найдено: <b>1</b>',self.panel()[0])
        self.press('СИЛА И ЧЕСТЬ'); self.press('Выбрать размер'); self.press('M')
        self.assertEqual(self.db.cart(420)['items'][0]['size'],'M')
        self.assertEqual(self.records('payments'),[]); self.assertEqual(self.records('stock_reservations'),[])

    def test_native_search_text_pause_resume_cancel_and_no_raw_log(self):
        self.message('/find'); self.message('/menu'); self.message('PAUSED PRIVATE +79993332211')
        self.assertNotIn('PAUSED PRIVATE','\n'.join(self.db.connection().iterdump()))
        self.message('/resume'); self.message('Чёрную футболку M до 5000')
        self.assertIn('Найдено: <b>1</b>',self.panel()[0]); self.assertIsNone(self.db.get_state(420))
        self.assertNotIn('Чёрную футболку','\n'.join(self.db.connection().iterdump()))
        self.message('/find'); self.message('/cancel'); self.assertIsNone(self.db.get_state(420))
        self.assertEqual(self.db.discovery_session(420)['filters']['size'],'M')

    def test_processed_search_update_never_enters_a_new_contact_or_service_draft(self):
        self.message('/find черная футболка',mid=771)
        self.profile(); self.db.set_state(420,'commerce_field',{'field':'address','next':'profile'})
        before=self.db.get_state(420)
        self.message('/find черная футболка',mid=771)
        self.assertEqual(self.db.get_state(420),before)
        self.assertEqual(self.db.account_profile(420)['address'],'Тестовый ПВЗ')
        self.message('OTHER BODY SAME ID',mid=771)
        self.assertEqual(self.db.get_state(420),before)
        self.assertNotIn('OTHER BODY','\n'.join(self.db.connection().iterdump()))

    def test_direct_search_does_not_erase_an_unrelated_private_draft(self):
        for query in ('/find M', '/find фиолетовая футболка', '/find\tM', '/find\nS'):
            with self.subTest(query=query):
                self.db.set_state(420,'ops_preview',{'token':'other-private-token'})
                self.message(query)
                state=self.db.get_state(420)
                self.assertEqual(state[0],'ops_preview'); self.assertEqual(state[1]['token'],'other-private-token'); self.assertTrue(state[1]['paused'])
                self.message('PRIVATE RETRY NOT AN ADDRESS OR SUPPORT MESSAGE')
                self.assertEqual(self.db.get_state(420),state)
                self.assertNotIn('PRIVATE RETRY','\n'.join(self.db.connection().iterdump()))
        self.message('/find')
        self.message('фиолетовая футболка')
        self.assertEqual(self.db.get_state(420)[0],'disc_input')
        self.assertFalse(self.db.get_state(420)[1].get('paused'))
        self.message('футболка M')
        self.assertIn('Найдено: <b>1</b>',self.panel()[0])
        self.assertIsNone(self.db.get_state(420))

    def test_foreign_callback_and_group_chat_cannot_change_owning_users_lists(self):
        self.enable(); self.callback('c:product:' + TEE)
        token=next(b['callback_data'] for row in self.panel()[1]['inline_keyboard'] for b in row if '☆ В избранное' in b['text'])
        self.callback(token,421); self.assertEqual(self.records('discovery_items'),[])
        self.api.reset_mock(); self.callback(token,420,private=False)
        self.api.send_message.assert_not_called(); self.assertEqual(self.records('discovery_items'),[])
        self.callback(token,420); self.assertEqual(len(self.records('discovery_items')),1)

    def test_invalid_price_card_and_unknown_characteristics_have_no_fake_facts(self):
        self.catalog.get(TEE)['material'] = None; self.catalog.get(TEE)['fit'] = {'bad':'shape'}; self.catalog.get(TEE)['price']='bad'
        p=self.db.discovery_product(420,TEE,self.catalog)
        self.assertEqual(p['material'],''); self.assertEqual(p['fit'],''); self.assertIsNone(p['price_rub'])
        self.bot.discovery.product(420,420,TEE)
        self.assertIn('Цена уточняется',self.panel()[0])
        self.assertFalse(any('Выбрать размер' in b['text'] for row in self.panel()[1]['inline_keyboard'] for b in row))

    def test_full_facts_and_compare_are_escaped_paginated_and_within_telegram_limits(self):
        self.catalog.get(TEE)['description']='Начало <script> & ' + '😀&<>' * 1200 + ' КОНЕЦ'
        self.enable(); self.pin(TEE,'compare'); self.pin(TAG,'compare',key='tag-compare')
        self.callback('d:compare:5')
        self.assertNotIn('<script>',self.panel()[0])
        self.press('Полные данные 1')
        seen=self.panel()[0]
        while any('Дальше по характеристикам' in b['text'] for row in self.panel()[1]['inline_keyboard'] for b in row):
            self.press('Дальше по характеристикам'); seen += self.panel()[0]
        self.assertIn('КОНЕЦ',seen); self.assertIn('Точные замеры',seen)
        for call in self.api.mock_calls:
            if call[0] not in {'send_message','edit_message_text'}: continue
            text=html.unescape(re.sub(r'<[^>]*>','',call.args[-2]))
            self.assertLessEqual(len(text.encode('utf-16-le'))//2,3900)
            for row in call.args[-1]['inline_keyboard']:
                for b in row:
                    if b.get('callback_data'): self.assertLessEqual(len(b['callback_data'].encode()),64)

    def test_comparison_reloads_prices_stock_hidden_items_and_facts(self):
        self.enable(); self.pin(TEE,'compare'); self.pin(TAG,'compare',key='second')
        self.catalog.get(TEE)['price']='7000 ₽'; self.set_count(0)
        self.catalog.get(TAG)['active']=False
        self.callback('d:compare:1')
        text=self.panel()[0]
        self.assertIn('7 000 ₽',text); self.assertNotIn('Нержавеющая',text); self.assertIn('снята с витрины',text)
        self.assertEqual(self.db.collection_view(420,'compare',self.catalog)['total'],2)

    def test_pruning_only_removes_expired_search_data_not_lists_or_money(self):
        self.enable(); self.pin(); self.search('M')
        self.db.connection().execute('UPDATE discovery_sessions SET expires_at=0')
        self.db.connection().execute("UPDATE service_requests SET created_at='2020-01-01' WHERE kind='discovery_search'")
        self.db.prune_discovery()
        self.assertEqual(self.records('discovery_sessions'),[])
        self.assertEqual(len(self.records('discovery_items')),1)
        self.assertIsNotNone(self.db.service_result(420,'pin-one'))
        self.assertIsNone(self.db.service_result(420,'search-one'))

    def test_no_wishlist_import_from_browser_or_customer_api_staff_flags(self):
        self.enable(); self.pin()
        api=operations.OperationsTests.api_client(self)
        for uid in (420,9001):
            status, data=api('?saved=1&user_id=420&staff=1',uid=uid)
            self.assertEqual(status,200); self.assertNotIn('discovery_items',data); self.assertNotIn(TEE,json.dumps(data))
            status,_=api(uid=uid,data={'action':'discovery_list','kind':'saved','product_id':TAG,'operation_id':'not-an-api'})
            self.assertEqual(status,400)
        self.assertEqual(len(self.records('discovery_items')),1)


if __name__ == '__main__': unittest.main()
