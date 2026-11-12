import json
from pathlib import Path
import tempfile
import unittest
from bot import BrandBot, Catalog, Database, TelegramAPI
from testkit import make_settings

class RecordingAPI(TelegramAPI):
    def __init__(self):
        super().__init__('fake')
        self.calls=[]
        self.fail=None
    def call(self, method, payload=None, timeout=70):
        self.calls.append((method,payload))
        if self.fail and method == self.fail[0]: raise RuntimeError(self.fail[1])
        return {'message_id':7}

class MenuCardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=RecordingAPI()
        self.db=Database(Path(self.temp.name)/'db.sqlite')
        self.settings=make_settings(self.db.path,brand_name='СИЛА И ЧЕСТЬ',support_username='manager')
        self.bot=BrandBot(self.settings,self.api,self.db,Catalog(Path(__file__).with_name('catalog.json')))
        self.user={'id':500,'first_name':'Никита <>&','username':'test'}
        self.db.upsert_user(self.user)
    def tearDown(self):
        self.db.connection().close()
        self.temp.cleanup()
    def buttons(self,card):
        return [b for block in card['blocks'] if block['type']=='buttons' for b in block['buttons']]
    def test_start_uses_one_rich_card_and_removes_old_reply_keyboard(self):
        self.bot.start(500,self.user)
        self.assertEqual([m for m,_ in self.api.calls],['sendRichMessage'])
        payload=self.api.calls[-1][1]
        self.assertEqual(payload['reply_markup'],{'remove_keyboard':True})
        self.assertEqual(payload['rich_message']['blocks'][0]['type'],'photo')
        self.assertIn('Никита <>&',json.dumps(payload,ensure_ascii=False))
    def test_navigation_and_admin_visibility(self):
        buttons=self.buttons(self.bot.menu_card(500))
        targets={b.get('callback_data') for b in buttons}
        self.assertTrue({'cat:drop','my_orders','account','about','support'}<=targets)
        self.assertNotIn('adm:panel',targets)
        self.assertTrue(any('web_app' in b for b in buttons))
        self.db.upsert_user({'id':1,'first_name':'Admin'})
        self.assertIn('adm:panel',{b.get('callback_data') for b in self.buttons(self.bot.menu_card(1))})
    def test_exact_order_count_is_not_limited_to_recent_orders(self):
        product=self.bot.catalog.products[0] if hasattr(self.bot.catalog,'products') else next(iter(self.bot.catalog.products_by_id.values()))
        for n in range(12):self.db.create_order(f'case-{n}',500,product,'M','+79990000000')
        self.assertEqual(self.db.order_count_for_user(500),12)
        self.assertIn('12',json.dumps(self.bot.menu_card(500)))
        self.assertEqual(self.db.order_count_for_user(501),0)
    def test_pending_payment_stays_available(self):
        self.db.pending_payment=lambda user_id: ('p123',4900)
        self.assertIn('draft:p123',{b.get('callback_data') for b in self.buttons(self.bot.menu_card(500))})
    def test_return_edits_same_message(self):
        self.bot._screen_edit=(500,123)
        self.bot.send_menu(500)
        self.assertEqual(self.api.calls[0][0],'editMessageText')
        self.assertEqual(self.api.calls[0][1]['message_id'],123)
    def test_known_api_rejection_uses_standard_menu(self):
        self.api.fail=('sendRichMessage','Telegram HTTP 404: method not found')
        fallback=[]
        self.bot.send_welcome=lambda *args,**kwargs:fallback.append((args,kwargs))
        self.bot.send_menu(500)
        self.assertEqual(len(fallback),1)
        self.assertTrue(fallback[0][1]['menu_card'])
    def test_network_failure_does_not_send_duplicate(self):
        self.api.fail=('sendRichMessage','Telegram request failed: timed out')
        with self.assertRaises(RuntimeError):self.bot.send_menu(500)
        self.assertEqual(len(self.api.calls),1)
    def test_no_empty_channel_link(self):
        self.bot.channel_link=lambda:''
        self.assertTrue(all(b.get('url')!='' for b in self.buttons(self.bot.menu_card(500))))

if __name__=='__main__':unittest.main()
