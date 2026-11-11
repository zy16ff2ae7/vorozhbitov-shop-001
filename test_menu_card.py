from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from bot import BrandBot, Catalog, Database, TelegramAPI
from testkit import make_settings

class RecordingAPI(TelegramAPI):
    def __init__(self):super().__init__('fake');self.calls=[];self.videos=[]
    def call(self,method,payload=None,timeout=70):self.calls.append((method,payload));return {'message_id':7}
    def send_video(self,chat_id,video,caption='',reply_markup=None,**kw):
        self.videos.append((chat_id,video,caption,reply_markup))
        return {'video':{'file_id':'VID-CACHED-360'}}

class InlineMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.api=RecordingAPI();self.db=Database(Path(self.temp.name)/'db')
        self.bot=BrandBot(make_settings(self.db.path,brand_name='СИЛА И ЧЕСТЬ'),self.api,self.db,Catalog(Path(__file__).with_name('catalog.json')))
        self.user={'id':500,'first_name':'Никита'};self.db.upsert_user(self.user)
    def tearDown(self):self.db.connection().close();self.temp.cleanup()
    def buttons(self):return [b for row in self.bot.main_menu(500)['inline_keyboard'] for b in row]
    def test_start_attaches_inline_buttons_to_original_welcome(self):
        self.bot.send_welcome=Mock();self.bot.start(500,self.user)
        args=self.bot.send_welcome.call_args.args
        self.assertIn('закрытой территории',args[1]);self.assertIn('inline_keyboard',args[2]);self.assertNotIn('keyboard',args[2])
    def test_all_buyer_routes_and_primary_webapp(self):
        buttons=self.buttons();self.assertIn('web_app',buttons[0])
        self.assertEqual(
            {b['callback_data'] for b in buttons if 'callback_data' in b},
            {'product:tee-sila-i-chest','product:tee-vzglyad','product:tag-sila-i-chest','size_guide','cat:drop','teaser','my_orders','account','about','support'},
        )
    def test_brand_icon_is_not_duplicated_in_text(self):
        self.bot.settings=make_settings(self.db.path,brand_name='СИЛА И ЧЕСТЬ',inline_emoji_enabled=True,custom_emoji_ids={'orders':'5409075992901756978'})
        buttons=self.buttons();orders=next(b for b in buttons if b.get('callback_data')=='my_orders')
        self.assertEqual(orders.get('icon_custom_emoji_id'),'5409075992901756978');self.assertEqual(orders['text'],'Мои покупки')
    def test_pending_payment_preserved(self):
        import bot as bot_module
        prev=bot_module.ONLINE_PAYMENTS_ENABLED
        try:
            bot_module.ONLINE_PAYMENTS_ENABLED=True
            self.db.pending_payment=lambda user_id:('p123',4900)
            self.assertIn('draft:p123',{b.get('callback_data') for b in self.buttons()})
        finally:
            bot_module.ONLINE_PAYMENTS_ENABLED=prev
    def test_admin_panel_not_exposed(self):
        self.assertNotIn('adm:panel',{b.get('callback_data') for b in self.buttons()})
        self.assertIn('adm:panel',{b.get('callback_data') for row in self.bot.main_menu(1)['inline_keyboard'] for b in row})
    def test_return_uses_standard_inline_message_and_edit(self):
        self.bot.send_menu(500);method,payload=self.api.calls[-1]
        self.assertEqual(method,'sendMessage');self.assertIn('inline_keyboard',payload['reply_markup'])
        self.bot._screen_edit=(500,123);self.bot.send_menu(500)
        self.assertEqual(self.api.calls[-1][0],'editMessageText')
        self.assertNotIn('rich_message',self.api.calls[-1][1])
    def test_empty_channel_is_omitted(self):
        self.bot.channel_link=lambda:''
        self.assertFalse(any('url' in b for b in self.buttons()))
    def test_product_360_video_and_size_guide_keep_manager_redirect(self):
        import os
        prev=os.environ.get('ORDER_MANAGER_USERNAME')
        os.environ['ORDER_MANAGER_USERNAME']='intellectual_sabotage'
        try:
            self.assertTrue(self.bot.route_callback('c1',500,500,'pvid:tee-vzglyad'))
            self.assertTrue(self.bot.route_callback('c2',500,500,'pvid:tee-vzglyad'))
            self.assertEqual(len(self.api.videos),2)
            self.assertIsInstance(self.api.videos[0][1],Path)
            self.assertEqual(self.api.videos[1][1],'VID-CACHED-360')
            self.assertTrue(self.bot.route_callback('c3',500,500,'size_guide:product:tee-vzglyad'))
            _,payload=self.api.calls[-1]
            cbs={b.get('callback_data') for row in payload['reply_markup']['inline_keyboard'] for b in row}
            self.assertIn('msize:tee-vzglyad:XL',cbs)
            self.assertTrue(self.bot.route_callback('c4',500,500,'msize:tee-vzglyad:XL'))
            _,mgr_payload=self.api.calls[-1]
            urls=[b.get('url','') for row in mgr_payload['reply_markup']['inline_keyboard'] for b in row]
            self.assertTrue(any('t.me/intellectual_sabotage?text=' in u for u in urls))
        finally:
            if prev is None:os.environ.pop('ORDER_MANAGER_USERNAME',None)
            else:os.environ['ORDER_MANAGER_USERNAME']=prev

if __name__=='__main__':unittest.main()
