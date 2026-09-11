"""Native opt-in product signals. No implicit subscriptions or payment actions."""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import re
import time

from alerts_store import LIVE,STATE_NAMES,TERMS_VERSION,policy_fingerprint,product_identity
from commerce_bot import button,esc
from payments import format_rub


def when(value):
    return datetime.fromtimestamp(value,timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


class AlertsBot:
    def __init__(self, bot):
        self.bot,self.db,self.ui,self.catalog=bot,bot.db,bot.commerce,bot.catalog

    def ready(self):
        if not self.bot.settings.product_alerts_enabled:
            raise ValueError("Оператор ещё не включил доставку уведомлений. Подписки сейчас не создаём; существующие можно отключить через /alerts.")
        return policy_fingerprint(self.bot.settings.alerts_policy_url())

    def banner(self):
        try: self.ready();return "Доставка включена."
        except ValueError: return "Доставка выключена или не настроена. Можно просмотреть и отключить прежние подписки."

    def action(self, uid, label, kind, **data):
        token=self.db.create_chat_action(uid,"watch."+kind,data).split(":")[-1]
        return button(label,"a:do:"+token)

    def screen(self, chat, uid, title, text, rows, *, pause=True):
        if pause: self.ui.pause(uid)
        self.ui.screen(chat,uid,title,text,rows)

    def home(self, chat, uid, history=False, page=0, notice=""):
        data=self.db.alert_list(uid,self.catalog,history=history,page=page)
        text=(esc(notice)+"\n\n") if notice else ""
        text+="<b>"+("История сигналов" if history else "Мои уведомления")+f" · {data['total']}</b>\n"+self.banner()
        text+="\n\nТолько выбранные условия, однократно за 30 дней. Избранное, старый лист ожидания, контакты и реклама не включают эти подписки автоматически."
        rows=[]
        for item in data["items"]:
            text+=f"\n\n<b>#{item['watch_id']:04d} · {esc(item['name'][:65])}</b>\n"
            if item["visible"]:
                text+=("Появление размера "+esc(item["size"])) if item["kind"]=="stock" else "Цена до "+esc(format_rub(item["limit_rub"]))
                text+=" · "
            text+=STATE_NAMES[item["state"]]
            rows.append([button(f"Открыть подписку #{item['watch_id']:04d}",f"a:watch:{item['watch_id']}")])
        if not data["items"]: text+="\n\nПодписок здесь пока нет. Выбери вещь и отдельное условие — ничего не включится без подтверждения."
        if data["page"]: rows.append([button("← Новее",f"a:list:{int(history)}:{data['page']-1}")])
        if data["more"]: rows.append([button("Раньше →",f"a:list:{int(history)}:{data['page']+1}")])
        rows+=[[button("Настроить уведомление…","a:catalog:0")],
               [button("Активные подписки","a:home"),button("История","a:list:1:0")],
               [button("Отключить всё и удалить подписки",f"a:offall:{data['account']['generation']}")],
               [button("Избранное / сравнение","d:lists"),button("Главная","c:home")]]
        text+="\n\nДо 20 активных условий. Не более двух попыток отправки за 24 часа, с интервалом от пяти минут. Это не резерв и не очередь на покупку. Начатая до отключения отправка может дойти. Архивный запрос ожидания не является подпиской и здесь не удаляется."
        self.screen(chat,uid,"АККАУНТ / УВЕДОМЛЕНИЯ",text,rows)

    def catalog_view(self, chat, uid, page=0):
        self.ready();products=self.catalog.public_products()
        page=max(0,min(page,max(0,(len(products)-1)//6)))
        rows=[[button(p["name"][:55],"a:item:"+p["id"])] for p in products[page*6:(page+1)*6]]
        if page: rows.append([button("← Вещи",f"a:catalog:{page-1}")])
        if (page+1)*6<len(products): rows.append([button("Ещё вещи →",f"a:catalog:{page+1}")])
        rows.append([button("← Мои уведомления","a:home")])
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / ВЕЩЬ","<b>За какой вещью наблюдать?</b>\nТолько действующий каталог. Подписка на каждое условие подтверждается отдельно.",rows)

    def item(self, chat, uid, pid):
        self.ready();p=self.db._alert_product(pid,self.catalog)
        self.screen(chat,uid,"ВЕЩЬ / УВЕДОМЛЕНИЯ","<b>"+esc(p["name"][:100])+"</b>\nВыбери условие: появление конкретного размера или снижение каталожной цены. Это разные подписки; покупку, промокод и резерв они не создают.",
            [[button("Появление размера…","a:stock:"+pid)],[button("Снижение цены…","a:price:"+pid)],
             [button("Актуальная карточка","a:live:"+pid),button("Мои уведомления","a:home")]])

    def sizes(self, chat, uid, pid, page=0):
        self.ready();p=self.db._alert_product(pid,self.catalog);stock=self.db.inventory_view().get(pid,{})
        page=max(0,min(page,(len(p["sizes"])-1)//6));rows=[]
        for size in p["sizes"][page*6:(page+1)*6]:
            free=stock.get(size,{}).get("available");label=size+" · "+("уточняем" if free is None else str(free)+" свободно")
            rows.append([self.action(uid,label,"stock",pid=pid,size=size)])
        if page: rows.append([button("← Размеры",f"a:stock:{pid}:{page-1}")])
        if (page+1)*6<len(p["sizes"]): rows.append([button("Ещё размеры →",f"a:stock:{pid}:{page+1}")])
        rows.append([button("← Условия","a:item:"+pid),button("Мои уведомления","a:home")])
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / РАЗМЕР","<b>"+esc(p["name"][:100])+"</b>\nЕсли размер уже доступен, подписка не нужна. Для отсутствующего или неподтверждённого остатка можно запросить один будущий сигнал. Другой размер не подставляем.",rows)

    def stock_consent(self, chat, uid, pid, size):
        self.ready();c=self.db.alert_candidate(uid,"stock",pid,size,None,self.catalog)
        self.consent(chat,uid,c)

    def price_choices(self, chat, uid, pid):
        policy=self.ready();p=self.db._alert_product(pid,self.catalog);price=p["price_rub"]
        if type(price) is not int or price<=1: raise ValueError("Нужна известная каталожная цена больше 1 ₽. Ноль не считаем скидкой.")
        data={"pid":pid,"anchor":price,"identity":product_identity(p),"generation":self.db.alert_account(uid)["generation"],"policy":policy}
        rows=[[self.action(uid,"Любое снижение · до "+format_rub(price-1),"price",limit=price-1,**data)]]
        if price*9//10>=1: rows.append([self.action(uid,"Не выше "+format_rub(price*9//10),"price",limit=price*9//10,**data)])
        rows+=[[self.action(uid,"Указать свой порог…","price_input",**data)],[button("← Условия","a:item:"+pid),button("Мои уведомления","a:home")]]
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / ЦЕНА","<b>"+esc(p["name"][:100])+"</b>\nСейчас: "+esc(format_rub(price))+"\n\nСообщим один раз, если каталожная цена станет не выше выбранного порога и ниже этой базы. Повышение цены не поднимает базу подписки. Промокоды, доставка и наличие размера в условие не входят.",rows)

    def price_consent(self, chat, uid, data, limit):
        if self.ready()!=data["policy"]: raise ValueError("Политика изменилась. Открой настройку подписки заново.")
        c=self.db.alert_candidate(uid,"price",data["pid"],"",limit,self.catalog)
        if c["anchor_rub"]!=data["anchor"] or c["identity_digest"]!=data["identity"] or c["generation"]!=data["generation"]:
            raise ValueError("Цена, карточка или разрешение изменились. Проверь новый порог.")
        self.consent(chat,uid,c)

    def consent(self, chat, uid, candidate):
        policy=self.ready();c=candidate
        condition=("Появление размера <b>"+esc(c["size"])+"</b> при подтверждённом свободном остатке больше нуля.") if c["kind"]=="stock" else ("Каталожная цена не выше <b>"+esc(format_rub(c["limit_rub"]))+"</b>, ниже базы "+esc(format_rub(c["anchor_rub"]))+". Наличие и промокоды не учитываются.")
        text="<b>Разрешить одно уведомление?</b>\n"+esc(c["name"][:100])+"\n\n"+condition
        text+="\n\nСрок — 30 дней от подтверждения. Сохраняем Telegram ID, ссылку на вещь, размер или ценовой порог, базу цены и технический результат отправки. Контакты не нужны; избранное и реклама не включаются."
        if c["kind"]=="stock": text+="\n\nСигналы ограничены числом новых свободных единиц. Приоритет у более ранних допустимых подписок с учётом лимита отправок. Это не гарантированная очередь на покупку и не резерв."
        text+="\n\nДо двух попыток за 24 часа, не чаще раза в пять минут. Если Telegram не подтвердит результат, автоматически не повторяем. Доставку и наличие к моменту прочтения не гарантируем."
        text+="\nОтключение — /alerts или /alertsoff в один шаг. Уже начатая отправка может дойти.\n\n<a href=\""+esc(self.bot.settings.alerts_policy_url())+"\">Политика обработки данных</a>"
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / ОТДЕЛЬНОЕ СОГЛАСИЕ",text,
            [[self.action(uid,"Да, уведомить один раз","subscribe",candidate=c,policy=policy,terms=TERMS_VERSION)],
             [button("Не подписываться","a:home"),button("Актуальная карточка","a:live:"+c["product_id"])]])

    def watch(self, chat, uid, wid):
        w=self.db.alert_watch(uid,wid,self.catalog)
        text=f"<b>Подписка #{wid:04d}</b>\n"+esc(w["name"][:100])+"\n"+STATE_NAMES[w["state"]]
        if w["visible"]:
            text+=("\nРазмер: "+esc(w["size"])) if w["kind"]=="stock" else "\nБаза: "+esc(format_rub(w["anchor_rub"]))+"\nПорог: "+esc(format_rub(w["limit_rub"]))
        text+="\nДо: "+when(w["expires_at"])+"\n\n"+self.banner()
        if w["state"]=="uncertain": text+="\n\nРезультат отправки не подтверждён. Сообщение могло прийти — проверь диалог. Автоматического повтора не будет. Это не подтверждение чтения."
        text+="\n\nОдин сигнал не даёт резерв и не фиксирует цену покупки. История закрытых подписок хранится до 30 дней после закрытия. Все записи можно удалить из /alerts."
        rows=[]
        if w["state"] in LIVE: rows.append([button("Отключить эту подписку",f"a:off:{wid}")])
        if w["visible"]: rows.append([button("Актуальная карточка","a:live:"+w["product_id"]),button("Настроить заново","a:item:"+w["product_id"])])
        rows.append([button("Мои уведомления","a:home"),button("Главная","c:home")])
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / ПОДПИСКА",text,rows)

    def health(self, chat, uid):
        stats=self.db.alert_health(uid,owner_ids=self.bot.settings.admin_ids)
        text="<b>Только состояние механизма, без чужих предпочтений.</b>\n"+self.banner()+"\n\nПодписки:\n"+"\n".join(STATE_NAMES[s]+": "+str(n) for s,n in stats["watches"].items())
        text+="\n\nОтправки:\n"+"\n".join(STATE_NAMES.get(s,s)+": "+str(n) for s,n in stats["deliveries"].items())
        text+="\n\nПроверка примерно раз в 30 секунд, до 200 подписок за цикл и до пяти отправок. Неизвестный исход не переотправляется. Старый /restock не обходит согласие; остаток меняется через подтверждённый пересчёт /stock. Ручной кнопки «считать доставленным» нет."
        self.screen(chat,uid,"ВЛАДЕЛЕЦ / СИГНАЛЫ",text,[[button("Обновить","a:health"),button("← Рабочее место","c:team")]])

    def dispatch(self, chat, uid, token):
        kind,d=self.db.chat_action(uid,token)
        if not kind.startswith("watch."): raise ValueError("Это другая кнопка.")
        if kind=="watch.stock": self.stock_consent(chat,uid,d["pid"],d["size"])
        elif kind=="watch.price": self.price_consent(chat,uid,d,d["limit"])
        elif kind=="watch.subscribe":
            if self.ready()!=d["policy"]: raise ValueError("Политика изменилась. Нужно новое подтверждение.")
            w=self.db.subscribe_alert(uid,d["candidate"],token,self.catalog,policy_url=self.bot.settings.alerts_policy_url(),terms_version=d["terms"])
            self.watch(chat,uid,w["watch_id"])
        elif kind=="watch.price_input":
            if self.ready()!=d["policy"]: raise ValueError("Политика изменилась. Открой настройку заново.")
            self.db.set_state(uid,"watch_price",{**d,"expires_at":time.time()+900})
            self.input_prompt(chat,uid,d)
        else: raise ValueError("Открой актуальные уведомления.")

    def input_prompt(self, chat, uid, data):
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / СВОЙ ПОРОГ",f"<b>Целая цена от 1 до {data['anchor']-1} ₽.</b>\nНапиши только цифры одним сообщением. Например: 4000. Телефон или другие личные данные не нужны.\n\nСначала покажем условие и отдельное согласие. Ввод действует 15 минут. /cancel отменяет ввод.",[[button("Пауза · главная","c:home"),button("Мои уведомления","a:home")]],pause=False)

    def error(self, chat, uid, exc, *, pause_other=True):
        state=self.db.get_state(uid);keep=state and state[0]=="watch_price" and not state[1].get("paused")
        self.screen(chat,uid,"УВЕДОМЛЕНИЯ / НУЖНО ВНИМАНИЕ",esc(str(exc)),[[button("Мои уведомления","a:home"),button("Выбрать вещь","a:catalog:0")],[button("Главная","c:home")]],pause=pause_other and not keep)

    def handle_callback(self, chat, user, data):
        if not data.startswith(("a:","wait:","wsize:")): return False
        uid=int(user["id"])
        try:
            p=data.split(":")
            if p[0]=="wait": self.sizes(chat,uid,p[1]);return True
            if p[0]=="wsize": self.bot.confirm_waitlist(chat,uid,p[1],p[2]);return True
            if p[1]=="do": self.dispatch(chat,uid,p[2])
            elif p[1]=="home": self.home(chat,uid)
            elif p[1]=="list": self.home(chat,uid,bool(int(p[2])),int(p[3]))
            elif p[1]=="catalog": self.catalog_view(chat,uid,int(p[2]))
            elif p[1]=="item": self.item(chat,uid,p[2])
            elif p[1]=="live": self.bot.discovery.product(chat,uid,p[2])
            elif p[1]=="stock": self.sizes(chat,uid,p[2],int(p[3]) if len(p)>3 else 0)
            elif p[1]=="price": self.price_choices(chat,uid,p[2])
            elif p[1]=="watch": self.watch(chat,uid,int(p[2]))
            elif p[1]=="off":
                self.db.stop_alert(uid,int(p[2]),"watch-off:"+p[2])
                self.home(chat,uid,notice="Эта подписка отключена или уже завершилась. Другие не изменены; начатая отправка может дойти.")
            elif p[1]=="offall":
                self.db.stop_all_alerts(uid,int(p[2]),"watch-all:"+p[2])
                self.home(chat,uid,notice="Показываю актуальное состояние. Удаление подписок не стирает сообщения Telegram, покупки, профиль или избранное.")
            elif p[1]=="health": self.health(chat,uid)
            else: raise ValueError("Открой /alerts заново.")
        except (ValueError,PermissionError,TypeError,KeyError,IndexError,OverflowError) as exc: self.error(chat,uid,exc)
        return True

    def resume(self, chat, uid):
        state=self.db.get_state(uid)
        if not state or state[0]!="watch_price": return False
        try:
            current_policy=self.ready();d=state[1]
            if d["expires_at"]<=time.time(): raise ValueError("Время ввода истекло. Настрой порог заново.")
            p=self.db._alert_product(d["pid"],self.catalog)
            if d["policy"]!=current_policy or d["generation"]!=self.db.alert_account(uid)["generation"] or d["anchor"]!=p["price_rub"] or d["identity"]!=product_identity(p):
                raise ValueError("Карточка, цена или условия изменились. Настрой новый порог.")
            self.db.set_state(uid,state[0],{k:v for k,v in d.items() if k!="paused"})
            self.input_prompt(chat,uid,d)
        except (ValueError,PermissionError,KeyError) as exc: self.error(chat,uid,exc)
        return True

    def handle_message(self, chat, user, message):
        uid=int(user["id"]);text=str(message.get("text", "")).strip();words=text.split(maxsplit=1)
        command=(words[0] if words else "").split("@",1)[0].lower();mid=message.get("message_id")
        op=f"watch-input:{chat}:{mid}";digest=hashlib.sha256(text.encode()).hexdigest()
        previous=self.db.service_result(uid,op) if type(mid) is int and mid>0 else None
        if previous and previous["kind"]=="watch_input":
            try: self.db._service_request(uid,op,"watch_input",[digest])
            except ValueError as exc: self.error(chat,uid,exc,pause_other=False)
            return True
        if command in {"/alerts","/alertdesk"}:
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?",(uid,))
            return self.handle_callback(chat,user,"a:health" if command=="/alertdesk" else "a:home")
        state=self.db.get_state(uid)
        if state and state[0]=="watch_price" and (command=="/cancel" or text.lower() in {"отмена","cancel"}):
            self.db.clear_state(uid);self.ui.home(chat,uid);return True
        if command!="/alertsoff" and (not state or state[0]!="watch_price" or command.startswith("/")): return False
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?",(uid,))
        try:
            if command!="/alertsoff" and state[1].get("paused"): self.ui.home(chat,uid);return True
            if type(mid) is not int or mid<=0: raise ValueError("Пришли новое сообщение Telegram.")
            with self.catalog.lock,self.db.commerce_transaction():
                _,fp=self.db._service_request(uid,op,"watch_input",[digest])
                if command=="/alertsoff": self.db.stop_all_alerts(uid,self.db.alert_account(uid)["generation"],op+"-off")
                else:
                    self.ready()
                    if state[1]["expires_at"]<=time.time(): raise ValueError("Время ввода истекло. Настрой новый порог.")
                    if not re.fullmatch(r"[0-9]{1,8}",text): raise ValueError("Нужна целая цена только цифрами, без минуса, валюты и личных данных.")
                    # Validate without a network/UI call inside the transaction.
                    c=self.db.alert_candidate(uid,"price",state[1]["pid"],"",int(text),self.catalog)
                    if c["anchor_rub"]!=state[1]["anchor"] or c["identity_digest"]!=state[1]["identity"] or c["generation"]!=state[1]["generation"] or self.ready()!=state[1]["policy"]:
                        raise ValueError("Карточка, цена или условия изменились. Начни настройку заново.")
                    self.db.clear_state(uid)
                self.db._service_done(uid,op,"watch_input",fp,0)
            if command=="/alertsoff": self.home(chat,uid,notice="Уведомления отключены, записи подписок удалены. Уже начатое сообщение может дойти; покупки и избранное не изменены.")
            else: self.consent(chat,uid,c)
        except (ValueError,PermissionError,TypeError,KeyError) as exc: self.error(chat,uid,exc)
        return True

    def tick(self, *, stop_requested=lambda: False):
        try: self.ready()
        except ValueError: return {"queued":0,"sent":0,"uncertain":0}
        result={"queued":self.db.scan_product_alerts(self.catalog,policy_url=self.bot.settings.alerts_policy_url()),"sent":0,"uncertain":0}
        for _ in range(5):
            if stop_requested(): break
            try: self.ready()
            except ValueError: break
            claim=self.db.claim_product_alert(self.catalog,policy_url=self.bot.settings.alerts_policy_url())
            if not claim: break
            w=claim["watch"];p=claim["product"]
            text="<b>"+esc(self.bot.settings.brand_name[:100])+"</b>\n<code>ОДНОКРАТНОЕ УВЕДОМЛЕНИЕ</code>\n\n<b>"+esc(p["name"][:100])+"</b>\n"
            if w["kind"]=="stock":
                text+="Размер "+esc(w["size"])+f": при проверке свободно {claim['available']} шт.\nЭто подтверждённый остаток базы, не обещание наличия к моменту прочтения."
            else:
                text+="Каталожная цена: <b>"+esc(format_rub(p["price_rub"]))+"</b>\nБаза подписки: "+esc(format_rub(w["anchor_rub"]))+"\nТвой порог: "+esc(format_rub(w["limit_rub"]))+"\nНаличие размера этим сигналом не подтверждается. Промокоды и доставка не учтены."
            text+="\n\nПроверено: "+when(claim["observed_at"])+f".\nПодписка #{w['watch_id']:04d} завершится этим сигналом. Это не резерв, не счёт и не гарантия цены новой покупки. Проверь актуальную карточку."
            rows=[[button("Актуальная карточка","a:live:"+p["id"])],[button("Мои уведомления","a:home"),button("Отключить все",f"a:offall:{w['generation']}")]]
            message_id=None;blocked=False
            try:
                # This dedicated transport intentionally has NO automatic retry.
                response=self.bot.api.send_message_once(w["user_id"],text,{"inline_keyboard":rows})
                if isinstance(response,dict) and type(response.get("message_id")) is int and response["message_id"]>0:
                    if not response.get("chat") or response["chat"].get("id")==w["user_id"]: message_id=response["message_id"]
            except Exception as exc:
                blocked=getattr(exc,"telegram_status",None)==403
            sent=self.db.finish_product_alert(claim["delivery_id"],claim["claim_token"],message_id=message_id,blocked=blocked)
            result["sent" if sent else "uncertain"]+=1
        return result
