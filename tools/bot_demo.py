"""Interactive OFFLINE client for the real native bot controller.

python3 tools/bot_demo.py --port 4174
Temporary synthetic data, no .env, no Telegram/provider network requests.
Never publish this unauthed role-switching test harness as a production service.
"""
from __future__ import annotations

import argparse
import copy
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot import BrandBot, Catalog, Database, Settings, TelegramAPI
from commerce_test_support import seed_test_inventory

ACTORS = {420: 'Покупатель', 421: 'Покупатель Б', 9003: 'Менеджер', 9002: 'Склад', 9004: 'Поддержка', 9005: 'Финансы', 9001: 'Владелец'}
FAKE_INPUT = {'name': 'Алексей · демо', 'phone': '+79990000000', 'city': 'Москва', 'address': 'Демо-ПВЗ · Тестовая улица, 1', 'note': 'Тестовая покупка'}


class DemoTelegram(TelegramAPI):
    def __init__(self):
        self.messages = {uid: [] for uid in ACTORS}
        self.sequence = 0
        self.precheckout = {}
        self.next_alert_uncertain = False

    def append(self, chat_id, text, markup=None, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'text': text, 'markup': markup or {}, **extra}
        self.messages.setdefault(chat_id, []).append(message)
        return message

    def call(self, method, payload=None, timeout=70):
        data = payload or {}
        if method == 'sendMessage':
            return self.append(data['chat_id'], data['text'], data.get('reply_markup'))
        if method == 'editMessageText':
            for message in self.messages.get(data['chat_id'], []):
                if message['message_id'] == data['message_id']:
                    message.update(text=data['text'], markup=data.get('reply_markup') or {}, edited=True)
                    return message
            raise RuntimeError('message to edit not found')
        if method == 'sendInvoice':
            return self.append(data['chat_id'], '<b>ТЕСТОВЫЙ СЧЁТ</b>\nНикаких списаний. Кнопка имитирует подтверждённое поступление через финансовый inbox бота.', invoice=data['payload'])
        if method == 'createInvoiceLink':
            return 'https://example.invalid/demo-invoice/' + data['payload']
        if method == 'sendPhoto':
            return self.append(data['chat_id'], data.get('caption', ''), data.get('reply_markup'))
        if method == 'sendMediaGroup':
            return [self.append(data['chat_id'], '<b>Образы выпуска</b>\nМедиа доступны в Mini App.')]
        if method == 'answerCallbackQuery': return True
        if method == 'answerPreCheckoutQuery':
            self.precheckout[data['pre_checkout_query_id']] = bool(data['ok'])
            return True
        raise RuntimeError('Offline demo does not implement Telegram method: ' + method)

    def send_message_once(self, chat_id, text, reply_markup=None):
        result = self.append(chat_id, text, reply_markup)
        if self.next_alert_uncertain:
            self.next_alert_uncertain = False
            raise RuntimeError('DEMO: response lost after a synthetic alert; no external send')
        return result

    def send_photo_file(self, chat_id, path, caption='', reply_markup=None):
        try:
            image = '/shop-assets/' + Path(path).resolve().relative_to(ROOT / 'miniapp').as_posix()
        except ValueError:
            image = ''
        return self.append(chat_id, caption, reply_markup, image=image)

    def send_video(self, chat_id, video, caption='', reply_markup=None, **kwargs):
        return self.append(chat_id, caption or 'Тизер выпуска · медиа в Mini App', reply_markup)

    def send_document(self, chat_id, filename, content, caption=''):
        return self.append(chat_id, html.escape(caption or filename) + '\nЭкспорт сформирован в памяти демо. Наружу ничего не отправлено.')


class Demo:
    def __init__(self, directory):
        root = Path(directory)
        shutil.copy(ROOT / 'catalog.json', root / 'catalog.json')
        self.settings = Settings(token='offline-demo-no-network', admin_ids=frozenset({9001}),
            channel_url='https://t.me/example', webapp_url='', manager_chat_id=9003,
            brand_name='ВОРОЖБИТОВ', support_username='', database_path=root / 'demo.sqlite',
            catalog_path=root / 'catalog.json', health_port=0, giveaway_min_invites=3, privacy_url='',
            product_alerts_enabled=True, product_alerts_policy_url='https://example.invalid/offline-alert-policy')
        self.db = Database(self.settings.database_path)
        self.catalog = Catalog(self.settings.catalog_path)
        self.api = DemoTelegram()
        self.bot = BrandBot(self.settings, self.api, self.db, self.catalog)
        self.lock = threading.RLock()
        self.finance_fixture = None
        self.next_finance_scan = 0.0
        self.next_alert_scan = 0.0
        seed_test_inventory(self.db, self.catalog, quantity=3)
        self.db.connection().execute("UPDATE stock_items SET on_hand=1 WHERE product_id='tee-sila-i-chest' AND size='M'")
        self.db.connection().execute("UPDATE stock_items SET on_hand=NULL WHERE product_id='tee-sila-i-chest' AND size='XXL'")
        for uid, name in ACTORS.items():
            self.db.upsert_user({'id': uid, 'first_name': name})
        for uid, role in ((9003, 'manager'), (9002, 'warehouse'), (9004, 'support'), (9005, 'finance')):
            self.db.set_staff_role(9001, uid, role, owner_ids=self.settings.admin_ids)
        for uid in ACTORS:
            if uid > 9000: self.bot.commerce.team_view(uid, uid)
            else: self.bot.commerce.home(uid, uid)

    def act(self, uid, data):
        user = {'id': uid, 'first_name': ACTORS[uid]}
        if data.get('signal'):
            self.signal_scenario(uid, data['signal'])
        elif data.get('finance'):
            self.finance_scenario(uid, data['finance'])
        elif data.get('pay'):
            pid = str(data['pay'])
            payment = self.db.get_payment(pid)
            if not payment or payment['user_id'] != uid:
                raise ValueError('Это не покупка выбранного демо-пользователя.')
            query_id = 'demo-preflight-' + pid
            self.bot.handle_pre_checkout({'id': query_id, 'from': user, 'invoice_payload': pid, 'currency': 'XTR', 'total_amount': payment['amount_stars']})
            if not self.api.precheckout.get(query_id) and not data.get('late'):
                raise ValueError('Резерв уже не действует. Старый счёт не оплачиваем.')
            self.api.append(uid, 'Имитация поступления · без настоящего списания', human=True)
            self.db.enqueue_payment_update({'update_id': self.api.sequence, 'message': {'from': user,
                'chat': {'id': uid}, 'successful_payment': {'invoice_payload': pid,
                'telegram_payment_charge_id': 'demo-charge-' + pid, 'currency': 'XTR', 'total_amount': payment['amount_stars']}}})
            self.bot.flush_payment_updates()
        elif data.get('expire'):
            self.db.expire_reservations(time.time() + 3700)
            self.api.append(uid, 'Демо: смоделировано истечение неоплаченных резервов. Новые резервы по-прежнему создаются на 60 минут.', human=True)
        elif data.get('callback'):
            self.bot.handle_callback({'id': 'demo-' + str(self.api.sequence), 'from': user,
                'message': {'chat': {'id': uid, 'type': 'private'}, 'message_id': data.get('message_id')}, 'data': str(data['callback'])[:64]})
        else:
            text = str(data.get('text') or '')[:2000]
            if data.get('example'):
                state = self.db.get_state(uid)
                text = self.example(uid) or '/menu'
            self.api.append(uid, text, human=True)
            self.bot.handle_message({'message_id': self.api.sequence, 'from': user, 'chat': {'id': uid, 'type': 'private'}, 'text': text})
        self.db.queue_ticket_alerts(self.settings.admin_ids, self.settings.manager_chat_id)
        self.db.sync_finance()
        self.db.queue_finance_alerts(self.settings.admin_ids, self.settings.manager_chat_id)
        self.bot.flush_notifications()

    def finance_scenario(self, uid, kind):
        """Explicit offline fixtures. This method is NEVER part of production."""
        if self.db.staff_role(uid, self.settings.admin_ids) not in {'owner', 'finance'}: raise ValueError('Тестовые финансовые сценарии доступны в ролях «Финансы» и «Владелец».')
        conn = self.db.connection()
        if kind == 'orphan':
            self.db.mark_payment_paid('demo-unknown-reference', 'lava', 'demo-orphan-invoice',
                amount_minor=149050, currency='UNK', queue_review=self.bot.notify_payment_review)
            text = 'ДЕМО: записано вымышленное неизвестное поступление. Валюта неизвестна, рубли не подставляются. Повтор кнопки не добавляет денег.'
        elif kind == 'conflict':
            r = conn.execute("SELECT * FROM payment_receipts WHERE method='stars' AND source='verified_event' ORDER BY receipt_id LIMIT 1").fetchone()
            if not r: raise ValueError('Сначала оформи демо-покупку и подтверди тестовую оплату Stars.')
            self.db.mark_payment_paid(r['claimed_ref'], r['method'], r['provider_id'], amount_minor=r['amount_minor'] + 1,
                currency=r['currency'], payer_id=r['payer_id'], queue_review=self.bot.notify_payment_review)
            text = 'ДЕМО: у одного поступления разные суммы в двух событиях. Оригинал не переписан, ещё одна оплата не создана.'
        elif kind == 'attempt':
            payload = {'type': 'order', 'request_id': 'finance-demo-attempt', 'consent': True,
                'customer': {**FAKE_INPUT, 'deliver': 'СДЭК'}, 'items': [{'product_id': 'tee-sila-i-chest', 'size': 'L', 'quantity': 1}]}
            r = self.bot.checkout_web_payload({'id': 420, 'first_name': ACTORS[420]}, payload)
            if not r.get('ok'): raise ValueError(r.get('error', 'Нет тестового остатка. Перезапусти чистое демо.'))
            if self.finance_fixture is None:
                a = self.db.reserve_invoice(r['payment_id'], 'lava', r['amount_rub'] * 100)
                if not a or 'attempt_id' not in a: raise ValueError('Тестовая попытка уже существует.')
                with self.db.commerce_transaction():
                    conn.execute("UPDATE payment_invoice_attempts SET status='uncertain' WHERE attempt_id=?", (a['attempt_id'],))
                    self.db.sync_finance_source('attempt', a['attempt_id'])
                self.finance_fixture = (r, a)
            text = 'ДЕМО: создана отдельная неоплаченная покупка покупателя А, футболка L. Выдача вымышленного счёта имеет неизвестный исход. Списания нет.'
        elif kind == 'issued':
            if self.finance_fixture is None: raise ValueError('Сначала создай сценарий зависшего счёта.')
            r, a = self.finance_fixture
            self.db.save_invoice(r['payment_id'], 'lava', 'demo-fixture-issued-invoice', r['amount_rub'] * 100,
                'RUB', 'https://pay.example.test/offline-demo', external_ref=a['external_ref'], attempt_id=a['attempt_id'])
            text = 'ДЕМО: сохранён результат выдачи вымышленного счёта. Только задача попытки может быть закрыта; это не оплата и не выполненный возврат.'
        else: raise ValueError('Неизвестный тестовый сценарий.')
        self.api.append(uid, text, human=True)
        self.bot.finance.home(uid, uid)

    def signal_scenario(self, uid, kind):
        """Explicit fixtures confined to this temporary demo, never production."""
        if self.db.staff_role(uid, self.settings.admin_ids) != 'owner':
            raise ValueError('Эти тестовые сценарии доступны только демо-владельцу.')
        if kind == 'price_drop':
            price = next(p['price_rub'] for p in self.catalog.public_products() if p['id'] == 'tee-sila-i-chest')
            if price <= 500: raise ValueError('Тест не создаёт нулевую или отрицательную цену.')
            candidate = copy.deepcopy(self.catalog.data)
            next(p for p in candidate['products'] if p['id'] == 'tee-sila-i-chest')['price'] = str(price - 500) + ' ₽'
            self.catalog.save(candidate)
            text = 'ДЕМО: цена футболки уменьшена на 500 ₽ только в временной копии каталога. Старые покупки не изменены.'
        elif kind == 'uncertain':
            self.api.next_alert_uncertain = True
            text = 'ДЕМО: следующая попытка сигнала потеряет ответ после тестовой отправки. Автоматического повтора не будет.'
        elif kind == 'check':
            result = self.bot.alerts.tick()
            text = 'ДЕМО: проверены реальные условия временной базы. Отправок с подтверждением: ' + str(result['sent']) + '. Без повторов ранее начатых отправок.'
        else: raise ValueError('Неизвестный тестовый сценарий сигнала.')
        self.api.append(uid, text, human=True)
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?", (uid,))
        self.bot.alerts.health(uid, uid)
        self.next_alert_scan = 0.0

    def example(self, uid):
        state = self.db.get_state(uid)
        if not state or state[1].get('paused'): return ''
        if state[0] == 'growth_code': return 'DEMO10'
        if state[0] == 'growth_create':
            return {'code':'DEMO10','value':'10','max_discount':'500','min_subtotal':'1900','total_limit':'2','user_limit':'1'}.get(state[1].get('field'),'')
        if state[0] == 'watch_price': return str(max(1, state[1]['anchor'] - 500))
        if state[0] == 'disc_input': return 'чёрная футболка S до 5000 в наличии'
        if state[0] == 'fin_input': return 'Демо: сверяем сведения. Результат оплаты или возврата ещё не подтверждён.'
        if state[0] == 'ops_input':
            return {'destination': 'Демо-ПВЗ, Тестовая улица, 1', 'amount_minor': '350,50',
                'eta': '2–4 дня после передачи', 'basis': 'Вымышленный тариф только для демо',
                'tracking': 'DEMO123456', 'reason': 'Исправлена опечатка в тестовом номере',
                'body': 'Тестовый вопрос по покупке. Реальные данные не используются.'}.get(state[1].get('field'), '')
        return FAKE_INPUT.get(state[1].get('field'), '2') if state[0] in {'commerce_field', 'commerce_count', 'commerce_person'} else ''

    def snapshot(self, uid):
        self.db.expire_reservations()
        if time.monotonic() >= self.next_finance_scan:
            self.db.sync_finance()
            self.db.queue_finance_alerts(self.settings.admin_ids, self.settings.manager_chat_id)
            self.next_finance_scan = time.monotonic() + 5
        self.bot.flush_notifications()
        if time.monotonic() >= self.next_alert_scan:
            self.bot.alerts.tick()
            self.next_alert_scan = time.monotonic() + 2
        conn = self.db.connection()
        sku = self.db.stock_item('tee-sila-i-chest', 'M')
        state = self.db.get_state(uid)
        return {'ok': True, 'actor': uid, 'role': ACTORS[uid], 'finance_access': self.db.staff_role(uid, self.settings.admin_ids) in {'owner', 'finance'}, 'messages': self.api.messages[uid][-60:],
            'purchases': conn.execute('SELECT COUNT(*) FROM purchases').fetchone()[0],
            'paid': conn.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0],
            'reserved': conn.execute('SELECT COALESCE(SUM(reserved),0) FROM stock_items').fetchone()[0],
            'stock': {'available': sku['on_hand'] - sku['reserved'], 'reserved': sku['reserved'], 'on_hand': sku['on_hand']},
            'example': self.example(uid),
            'alerts_access': self.db.staff_role(uid, self.settings.admin_ids) == 'owner',
            'alerts_active': conn.execute("SELECT COUNT(*) FROM product_watches WHERE user_id=? AND state IN ('watching','queued','sending')", (uid,)).fetchone()[0],
            'alerts_sent': conn.execute("SELECT COUNT(*) FROM product_watches WHERE user_id=? AND state='sent'", (uid,)).fetchone()[0],
            'alerts_uncertain': conn.execute("SELECT COUNT(*) FROM product_watches WHERE user_id=? AND state='uncertain'", (uid,)).fetchone()[0],
            'promotion_count': conn.execute('SELECT COUNT(*) FROM promotions').fetchone()[0],
            'promotions_active': conn.execute('SELECT COUNT(*) FROM promotions WHERE active=1').fetchone()[0],
            'promotion_uses': conn.execute('SELECT COUNT(*) FROM promotion_redemptions').fetchone()[0],
            'discount_rub': conn.execute('SELECT COALESCE(SUM(discount_rub),0) FROM promotion_redemptions').fetchone()[0],
            'cart_promotion': self.db.cart_promotion(uid),
            'cart_quantity': sum(x['quantity'] for x in self.db.cart(uid)['items']),
            'list_storage': bool(self.db.discovery_account(uid)['enabled']),
            'saved_count': conn.execute("SELECT COUNT(*) FROM discovery_items WHERE user_id=? AND kind='saved'", (uid,)).fetchone()[0],
            'compare_count': conn.execute("SELECT COUNT(*) FROM discovery_items WHERE user_id=? AND kind='compare'", (uid,)).fetchone()[0],
            'finance_cases': conn.execute("SELECT COUNT(*) FROM finance_cases WHERE state<>'closed'").fetchone()[0],
            'finance_overdue': conn.execute("SELECT COUNT(*) FROM finance_cases WHERE state<>'closed' AND due_at<=?", (time.time(),)).fetchone()[0],
            'finance_closed': conn.execute("SELECT COUNT(*) FROM finance_cases WHERE state='closed'").fetchone()[0],
            'tickets': conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status<>'resolved'").fetchone()[0],
            'shipments': conn.execute("SELECT COUNT(*) FROM delivery_plans WHERE state IN ('in_transit','pickup_ready')").fetchone()[0],
            'delivered': conn.execute("SELECT COUNT(*) FROM delivery_plans WHERE state='delivered'").fetchone()[0],
            'receipts': [dict(r) for r in conn.execute("SELECT r.method,r.status,r.reason,r.source,EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id) AS disputed FROM payment_receipts r ORDER BY r.receipt_id DESC LIMIT 4")],
            'audit': [dict(r) for r in conn.execute('SELECT actor_id,action,entity FROM staff_audit ORDER BY audit_id DESC LIMIT 4')]}


class DemoHandler(BaseHTTPRequestHandler):
    demo: Demo
    def respond(self, content, mime='application/json; charset=utf-8', status=200):
        body = json.dumps(content, ensure_ascii=False).encode() if isinstance(content, dict) else content
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/demo/api/state':
            try:
                uid = int(urllib.parse.parse_qs(parsed.query).get('actor', ['420'])[0])
                if uid not in ACTORS: raise ValueError('Неизвестный демо-пользователь.')
                with self.demo.lock: result = self.demo.snapshot(uid)
                self.respond(result)
            except ValueError as exc: self.respond({'error': str(exc)}, status=400)
            return
        relative = 'index.html' if parsed.path == '/' else parsed.path.lstrip('/')
        base = ROOT / 'tools' / 'bot-demo'
        if relative.startswith('shop-assets/'):
            base = ROOT / 'miniapp'
            relative = relative.removeprefix('shop-assets/')
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base.resolve()) or not candidate.is_file() or candidate.suffix not in {'.html', '.css', '.js', '.woff2', '.jpg', '.png', '.webp'}:
            self.respond({'error': 'Not found'}, status=404)
            return
        self.respond(candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream')

    def do_POST(self):
        if self.path != '/demo/api/action':
            self.respond({'error': 'Not found'}, status=404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192: raise ValueError('Некорректный запрос.')
            data = json.loads(self.rfile.read(length))
            uid = data.get('actor')
            if type(uid) is not int or uid not in ACTORS: raise ValueError('Неизвестный демо-пользователь.')
            with self.demo.lock:
                self.demo.act(uid, data)
                result = self.demo.snapshot(uid)
            self.respond(result)
        except (ValueError, TypeError, AttributeError) as exc:
            self.respond({'error': str(exc)}, status=400)
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond({'error': 'Ошибка демо. Смотри журнал процесса.'}, status=500)

    do_HEAD = do_GET
    def log_message(self, *args): pass
    def finish(self):
        try: super().finish()
        finally: self.demo.db.close_current()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4174)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='native-bot-demo-') as directory:
        demo = Demo(directory)
        handler = type('OfflineBotDemoHandler', (DemoHandler,), {'demo': demo})
        server = ThreadingHTTPServer(('0.0.0.0', args.port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'Bot demo: http://0.0.0.0:{args.port} · synthetic data, NO external payments', flush=True)
        try: stop.wait()
        finally:
            server.shutdown()
            server.server_close()
            demo.db.close_current()


if __name__ == '__main__':
    main()
