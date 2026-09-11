"""Ephemeral HTTP fixture for browser tests: no .env and NO Telegram/provider I/O.

python3 tools/ui_test_server.py --port 4173
The public token below is a test fixture, never a production credential.
"""
import argparse
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot import BrandBot, Catalog, Database, Settings, start_health_server
from commerce_test_support import seed_test_inventory
from growth_store import quote_fingerprint


class OfflineTelegram:
    def create_invoice_link(self, payload):
        return 'https://t.me/invoice/test-fixture-not-payable'

    def send_message(self, *args, **kwargs):
        return {'message_id': 1}


# Fixed, publicly known IDs and synthetic data for read-only operations browser
# checks. These records are never imported by runtime or copied into Docker.
OPERATIONS_USER = 730420


def seed_operations_fixture(db, settings, catalog, api):
    owner, support = 7309001, 7309004
    owners = frozenset({owner})
    for uid in (OPERATIONS_USER, owner, support):
        db.upsert_user({'id': uid, 'first_name': 'Operations UI fixture'})
    db.set_staff_role(owner, support, 'support', owner_ids=owners)
    receipt = BrandBot(settings, api, db, catalog).checkout_web_payload({'id': OPERATIONS_USER}, {
        'request_id': 'operations-ui-fixture', 'consent': True,
        'customer': {'phone': '+79990000000', 'city': 'Тестовый город', 'address': 'Только вымышленный ПВЗ'},
        'items': [{'product_id': 'tee-sila-i-chest', 'size': 'M', 'quantity': 1}]})
    assert receipt['ok'], receipt
    pid = receipt['purchase_id']
    db.mark_payment_paid(receipt['payment_id'], 'stars', 'ui-operations-test-charge',
        amount_minor=receipt['amount_stars'], currency='XTR', payer_id=OPERATIONS_USER)
    for stage in ('packing', 'ready'):
        db.advance_purchase(owner, pid, stage, db.purchase_view(pid, user_id=OPERATIONS_USER)['version'], owner_ids=owners)
    quote = db.propose_delivery(owner, pid, {'carrier': 'sdek', 'destination': 'Тестовый ПВЗ, Вымышленная улица 10',
        'amount_minor': 35050, 'billing': 'carrier', 'eta': '2–4 дня после передачи', 'basis': 'Явный тестовый тариф, не расчёт перевозчика'}, 0, 'ui-quote', owner_ids=owners)
    db.answer_delivery(OPERATIONS_USER, pid, quote, 1, True, 'ui-accept')
    tid = db.create_ticket(OPERATIONS_USER, 'delivery', 'Проверьте, пожалуйста, вымышленный ПВЗ. <img src=x onerror="window.__serviceXSS=1">', 'ui-ticket', pid)
    db.claim_ticket(support, tid, 'ui-claim')
    db.reply_ticket(support, tid, 'INTERNAL-UI-SECRET: only authorized staff', 1, 'ui-note', staff=True, internal=True)
    db.reply_ticket(support, tid, 'PUBLIC-UI-REPLY: проверим адрес перед отправкой.', 2, 'ui-reply', staff=True)


def seed_growth_fixture(db, settings, catalog, api):
    """Public, synthetic coupon and lost-reply fixtures; no extra HTTP controls."""
    owner=7309001;owners=frozenset({owner});now=int(time.time())
    p=db.create_promotion(owner,'UI10',{'kind':'percent','value':10,'max_discount':500,
        'min_subtotal':0,'categories':[],'starts_at':now-60,'ends_at':now+86400,
        'total_limit':5,'user_limit':1},'ui-promo-create',catalog,owner_ids=owners)
    p=db.set_promotion_active(owner,p['promotion_id'],True,p['version'],'ui-promo-on',owner_ids=owners)
    for uid in (730520,730521):
        db.upsert_user({'id':uid,'first_name':'Growth UI fixture'})
        if uid==730521:
            payload={'type':'order','customer':{'name':'Старый тест','phone':'+79990000000',
                'city':'Тестовый город','address':'Вымышленный ПВЗ','entrance':'','deliver':'СДЭК','note':''},
                'consent':True,'items':[{'product_id':'tee-sila-i-chest','size':'M','quantity':1}],
                'request_id':'growth-ui-original'}
            r=BrandBot(settings,api,db,catalog).checkout_web_payload({'id':uid},payload)
            assert r['ok'],r  # Pretend only its response was lost, not its commit.
        db.replace_cart(uid,[{'product_id':'tee-sila-i-chest','size':'M','quantity':1}],db.cart(uid)['revision'],'ui-new-cart',catalog)
        q=db.promotion_candidate(uid,'UI10',catalog)
        db.select_promotion(uid,p['promotion_id'],p['version'],q['cart_revision'],quote_fingerprint(q),'ui-select-promo',catalog)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4173)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with tempfile.TemporaryDirectory(prefix='shop-ui-test-') as directory:
        settings = Settings(token='ui-test-token', admin_ids=frozenset(), channel_url='https://t.me/test',
            webapp_url='', manager_chat_id=None, brand_name='Test shop', support_username='',
            database_path=Path(directory) / 'test.sqlite', catalog_path=ROOT / 'catalog.json',
            health_port=args.port, giveaway_min_invites=3, privacy_url='')
        db = Database(settings.database_path)
        catalog = Catalog(settings.catalog_path)
        seed_test_inventory(db, catalog, quantity=10000)
        api = OfflineTelegram()
        seed_operations_fixture(db, settings, catalog, api)
        seed_growth_fixture(db, settings, catalog, api)
        server = start_health_server(args.port, catalog, settings, db, api)
        # Public fake data only: let Arena embed the offline test preview.
        server.RequestHandlerClass._csp = lambda self: self.CSP_BASE
        print(f'Offline UI test fixture: http://0.0.0.0:{args.port}', flush=True)
        try:
            stopped.wait()
        finally:
            server.shutdown()
            server.server_close()
            db.close_current()
