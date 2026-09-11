"""Native repeat purchase and owner-controlled promotions, never a money tool."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import secrets
import time

from commerce_bot import button, esc
from growth_store import promo_code, quote_fingerprint, validate_rules
from payments import format_rub


def utc_label(value):
    return datetime.fromtimestamp(value,timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


class GrowthBot:
    def __init__(self, bot):
        self.bot,self.db,self.ui,self.catalog=bot,bot.db,bot.commerce,bot.catalog
        self.owners=bot.settings.admin_ids

    def action(self, uid, label, kind, **data):
        token=self.db.create_chat_action(uid,"growth."+kind,data).split(":")[-1]
        return button(label,"g:a:"+token)

    def screen(self, chat, uid, title, text, rows, *, pause=True):
        if pause: self.ui.pause(uid)
        self.ui.screen(chat,uid,title,text,rows)

    def rules_text(self, rules):
        cats={c["id"]:c["name"] for c in self.catalog.categories}
        amount=str(rules["value"])+"% · максимум "+format_rub(rules["max_discount"]) if rules["kind"]=="percent" else format_rub(rules["value"])
        return esc(amount)+"\nРазделы: "+esc(", ".join(cats.get(x,"раздел снят с витрины") for x in rules["categories"]) or "все")+"\nМинимум подходящих товаров: "+esc(format_rub(rules["min_subtotal"]))+"\nС "+utc_label(rules["starts_at"])+"\nДо "+utc_label(rules["ends_at"])+" (не включая)\nЛимит оформлений: "+str(rules["total_limit"])+" всего / "+str(rules["user_limit"])+" на аккаунт."

    @staticmethod
    def totals(quote):
        return "Товары до скидки: "+esc(format_rub(quote["subtotal_rub"]))+"\nСкидка: −"+esc(format_rub(quote["discount_rub"]))+"\n<b>К оплате за товары: "+esc(format_rub(quote["total_rub"]))+"</b>"

    def promo(self, chat, uid):
        cart=self.db.cart(uid);selected=cart["promotion"]
        text="<b>Промокод для текущей корзины.</b>\nОдин код, без сложения скидок. Скидка не меняет уже созданные покупки и не является возвратом денег."
        rows=[[button("Ввести промокод…","g:input")]]
        if selected:
            text+="\n\nВыбран: <b>"+esc(selected["code"])+"</b>\n"
            try: text+=self.totals(self.db.growth_quote(uid,self.catalog))
            except ValueError as exc: text+="⚠ "+esc(str(exc))
            rows.append([self.action(uid,"Снять промокод…","remove_check",revision=cart["revision"])])
        text+="\n\nОформление с кодом — через /cart в этом чате. Mini App покажет выбранный код, но не оформит корзину по полной цене молча. Применение расходуется при создании покупки, в том числе неоплаченной; отмена его не возвращает. Доставка не участвует."
        rows.append([button("← Корзина","c:cart"),button("Главная","c:home")])
        self.screen(chat,uid,"КОРЗИНА / ПРОМОКОД",text,rows)

    def prompt(self, chat, uid):
        self.db.set_state(uid,"growth_code",{"expires_at":time.time()+1800})
        self.screen(chat,uid,"ПРОМОКОД / ВВОД","<b>Пришли код одним сообщением.</b>\n3–24 латинских буквы, цифры, дефис или подчёркивание. Не присылай контакты или банковские данные.\n\nСначала покажем правила и точную сумму. Сохранение кода — отдельной кнопкой. /cancel отменяет ввод.",[[button("Без кода · корзина","c:cart"),button("Пауза · главная","c:home")]],pause=False)

    def candidate(self, chat, uid, code):
        q=self.db.promotion_candidate(uid,code,self.catalog)
        text="<b>"+esc(q["code"])+" / проверь скидку</b>\n"+self.rules_text(q["rules"])+"\n\n"+self.totals(q)
        text+="\n\nКод заменит другой код этой корзины. Пока это только расчёт: лимит и остатки не заняты. Применение расходуется при создании покупки, не при оплате; отмена и истечение резерва его не вернут. Доставка оплачивается отдельно."
        self.screen(chat,uid,"ПРОМОКОД / ПОДТВЕРЖДЕНИЕ",text,[[self.action(uid,"Применить к корзине","select",pid=q["promotion_id"],version=q["promotion_version"],revision=q["cart_revision"],expected=quote_fingerprint(q))],[button("Не применять","g:promo"),button("Корзина","c:cart")]])

    def repeat(self, chat, uid, purchase_id, page=0):
        p=self.db.repeat_preview(uid,purchase_id,self.catalog)
        page=max(0,min(page,(len(p["items"])-1)//5))
        text=f"<b>Повтор состава покупки №{purchase_id:04d}</b>\nДобавим {p['added_quantity']} шт. к текущей корзине, не заменяя её.\n\n<b>Объединённый состав · страница {page+1}/{(len(p['items'])+4)//5}</b>"
        for x in p["items"][page*5:(page+1)*5]:
            text+="\n\n"+esc(x["name"][:55])+" · "+esc(x["size"])+f" × {x['quantity']} · "+esc(format_rub(x["amount_rub"]))
            if not x["person"] and x["personalization_available"]: text+="\nПерсонализацию можно задать заново."
        text+="\n\n<b>Товары сейчас: "+esc(format_rub(p["total_rub"]))+"</b>\nНаличие проверено для всего объединённого состава. Это не резерв и не счёт."
        text+="\n\nСтарые цены, персонализация, адрес, условия доставки и скидка не копируются. Текущий промокод корзины, если выбран, будет снят — примени его заново после проверки состава. Оплата старой покупки не используется."
        rows=[];nav=[]
        if page: nav.append(button("← Состав",f"g:repeat:{purchase_id}:{page-1}"))
        if (page+1)*5<len(p["items"]): nav.append(button("Ещё состав →",f"g:repeat:{purchase_id}:{page+1}"))
        if nav: rows.append(nav)
        rows+=[[self.action(uid,"Добавить состав в корзину","repeat",purchase_id=purchase_id,expected=quote_fingerprint(p))],[button("Не повторять",f"c:purchase:{purchase_id}"),button("Корзина","c:cart")]]
        self.screen(chat,uid,"ПОКУПКА / ПОВТОР",text,rows)

    def campaigns(self, chat, uid, page=0):
        values=self.db.promotions_for_owner(uid,owner_ids=self.owners,page=page)
        rows=[[button("Новый промокод…","g:new")]]
        for p in values: rows.append([button(p["code"]+" · "+("включён" if p["active"] else "выключен"),f"g:campaign:{p['promotion_id']}")])
        if page: rows.append([button("← Новее",f"g:campaigns:{page-1}")])
        if len(values)==6: rows.append([button("Раньше →",f"g:campaigns:{page+1}")])
        rows.append([button("← Рабочее место","c:team")])
        self.screen(chat,uid,"ВЛАДЕЛЕЦ / ПРОМОКОДЫ","<b>Скидки с явными правилами.</b>\nНовый код создаётся выключенным. После создания условия неизменны: для других условий нужен новый уникальный код. Можно включить или остановить новые применения; уже созданные счета сохраняют сумму.\n\nКвота считается по созданным покупкам, включая неоплаченные и отменённые. Автоматических рассылок нет.",rows)

    def campaign(self, chat, uid, pid):
        self.db.require_staff(uid,"promotions.write",self.owners)
        p=self.db.promotion(pid);use=self.db.promotion_usage(pid)
        text="<b>"+esc(p["code"])+"</b> · "+("включён" if p["active"] else "выключен")+"\n"+self.rules_text(p["rules"])
        text+=f"\n\nСоздано покупок с кодом: <b>{use['total']}</b>. Это не количество оплат и не выручка.\nПосле остановки прежние счета остаются действующими на сохранённую сумму. Лимиты при повторном включении не обнуляются."
        rows=[[self.action(uid,"Остановить новые применения…" if p["active"] else "Включить промокод…","campaign_check",pid=pid,active=not bool(p["active"]),version=p["version"])],[button("← Все промокоды","g:campaigns:0"),button("Рабочее место","c:team")]]
        self.screen(chat,uid,"ВЛАДЕЛЕЦ / ПРАВИЛА КОДА",text,rows)

    def new_campaign(self, chat, uid):
        self.db.require_staff(uid,"promotions.write",self.owners)
        self.db.set_state(uid,"growth_create",{"generation":secrets.token_hex(8),"revision":0,"field":"code","rules":{},"expires_at":time.time()+1800})
        self.wizard(chat,uid)

    def wizard_state(self, uid):
        self.db.require_staff(uid,"promotions.write",self.owners)
        state=self.db.get_state(uid)
        if not state or state[0]!="growth_create" or state[1]["expires_at"]<=time.time(): raise ValueError("Мастер промокода истёк. Открой новый; ничего не опубликовано.")
        return state[1]

    def wizard(self, chat, uid, page=0):
        d=self.wizard_state(uid);field=d["field"];r=d["rules"]
        prompts={"code":"<b>Новый уникальный код.</b>\n3–24 латинских буквы, цифры, дефис, подчёркивание. Не вводи личные данные.",
                 "kind":"<b>Как рассчитывается скидка?</b>","value":"<b>Размер скидки.</b>\n"+("Целый процент от 1 до 90." if r.get("kind")=="percent" else "Целая сумма от 1 до 10000000 ₽."),
                 "max_discount":"<b>Максимальная скидка на одну покупку.</b>\nЦелые рубли от 1 до 10000000. Для процента потолок обязателен.",
                 "min_subtotal":"<b>Минимум подходящих товаров.</b>\nЦелые рубли от 0 до 10000000, до скидки. Неподходящие разделы и доставка не помогают достигнуть минимума.",
                 "categories":"<b>Для каких вещей?</b>\nОдин активный раздел или весь каталог. Доставка не участвует.",
                 "starts_at":"<b>Когда начинается действие?</b>\nНапиши дату и время в UTC: 2026-09-15 12:00 UTC. Либо выбери «Сейчас».",
                 "ends_at":"<b>Когда заканчивается действие?</b>\nДата и время в UTC, например 2026-09-30 21:00 UTC. Конец не включается; срок не длиннее 366 дней. Кнопки отсчитывают дни от начала.",
                 "total_limit":"<b>Общий лимит оформлений с кодом.</b>\nЦелое число от 1 до 100000. Применение расходуется при создании покупки, включая неоплаченную; отмена и истечение не возвращают его.",
                 "user_limit":"<b>Лимит на Telegram-аккаунт.</b>\nОт 1 до 100, не выше общего. Несколько кодов одновременно не складываются."}
        meta={"generation":d["generation"],"revision":d["revision"],"field":field}
        rows=[]
        def option(label,value): return self.action(uid,label,"wizard",value=value,**meta)
        if field=="kind": rows=[[option("Процент","percent"),option("Сумма ₽","fixed")]]
        elif field=="categories":
            active={p["category"] for p in self.catalog.public_products()}
            values=[("Весь каталог",[])]+[(c["name"][:60],[c["id"]]) for c in self.catalog.categories if c["id"] in active]
            page=max(0,min(page,(len(values)-1)//6))
            rows=[[option(label,value)] for label,value in values[page*6:(page+1)*6]]
            if page: rows.append([button("← Разделы",f"g:wpage:{page-1}")])
            if (page+1)*6<len(values): rows.append([button("Ещё разделы →",f"g:wpage:{page+1}")])
        elif field=="starts_at": rows=[[option("Сейчас",int(time.time()))]]
        elif field=="ends_at": rows=[[option(f"{days} дней от начала",r["starts_at"]+days*86400)] for days in (1,7,30)]
        if field=="review":
            text="<b>Создать "+esc(d["code"])+" выключенным?</b>\n"+self.rules_text(r)+"\n\nПосле создания правила не редактируются. Для изменения условий создаётся другой код. Квоты учитывают все созданные покупки, не только оплаченные. Ничего не рассылаем."
            rows=[[self.action(uid,"Создать выключенным","create",generation=d["generation"],revision=d["revision"],code=d["code"],rules=r)]]
        else: text=prompts[field]+"\n\nМастер действует 30 минут. /cancel отменяет его, /menu ставит на паузу."
        rows.append([button("Пауза · главная","c:home"),button("Список промокодов","g:campaigns:0")])
        self.screen(chat,uid,"ВЛАДЕЛЕЦ / НОВЫЙ КОД",text,rows,pause=False)

    def wizard_value(self, uid, field, value, generation, revision):
        d=self.wizard_state(uid)
        if d["generation"]!=generation or d["revision"]!=revision or d["field"]!=field or d.get("paused"):
            raise ValueError("Мастер изменился или на паузе. Продолжи актуальный шаг.")
        r=dict(d["rules"]);next_field=None
        if field=="code": d["code"]=promo_code(value);next_field="kind"
        elif field=="kind":
            if value not in {"percent","fixed"}: raise ValueError("Выбери тип скидки кнопкой.")
            r["kind"]=value;next_field="value"
        elif field=="categories":
            active={p["category"] for p in self.catalog.public_products()}
            if not isinstance(value,list) or len(value)>1 or any(not isinstance(x,str) or x not in active for x in value): raise ValueError("Выбери действующий раздел.")
            r[field]=value;next_field="starts_at"
        elif field in {"starts_at","ends_at"}:
            if type(value) is not int:
                try: value=int(datetime.strptime(value,"%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).timestamp())
                except (ValueError,TypeError,OverflowError): raise ValueError("Формат: ГГГГ-ММ-ДД ЧЧ:ММ UTC. Время указывается явно в UTC.")
            if not 1<=value<=4_102_444_800: raise ValueError("Дата вне допустимых границ.")
            if field=="ends_at" and (not 0<value-r["starts_at"]<=366*86400 or value<=time.time()): raise ValueError("Конец должен быть в будущем, позже начала и в пределах 366 дней.")
            r[field]=value;next_field="ends_at" if field=="starts_at" else "total_limit"
        elif field in {"value","max_discount","min_subtotal","total_limit","user_limit"}:
            if isinstance(value,str) and re.fullmatch(r"[0-9]{1,10}",value): value=int(value)
            high=90 if field=="value" and r["kind"]=="percent" else 100_000 if field=="total_limit" else 100 if field=="user_limit" else 10_000_000
            low=0 if field=="min_subtotal" else 1
            if type(value) is not int or not low<=value<=high: raise ValueError(f"Нужно целое число от {low} до {high}.")
            if field=="user_limit" and value>r["total_limit"]: raise ValueError("Личный лимит выше общего.")
            r[field]=value
            next_field={"value":"max_discount" if r["kind"]=="percent" else "min_subtotal","max_discount":"min_subtotal","min_subtotal":"categories","total_limit":"user_limit","user_limit":"review"}[field]
            if field=="value" and r["kind"]=="fixed": r["max_discount"]=value
        else: raise ValueError("На этом шаге нужна кнопка подтверждения.")
        if next_field=="review": r=validate_rules(r,self.catalog)
        self.db.set_state(uid,"growth_create",{**d,"rules":r,"field":next_field,"revision":revision+1})

    def dispatch(self, chat, uid, token):
        kind,d=self.db.chat_action(uid,token)
        if not kind.startswith("growth."): raise ValueError("Это другая кнопка.")
        kind=kind.removeprefix("growth.")
        if kind=="select":
            self.db.select_promotion(uid,d["pid"],d["version"],d["revision"],d["expected"],token,self.catalog)
            self.ui.cart_view(chat,uid,notice="Промокод выбран. Итог проверим в оформлении; применение пока не израсходовано.")
        elif kind=="remove_check":
            self.screen(chat,uid,"ПРОМОКОД / СНЯТЬ","<b>Снять код с текущей корзины?</b>\nУже созданные покупки не изменятся. Цена новой покупки будет показана заново.",[[self.action(uid,"Да, снять промокод","remove",revision=d["revision"])],[button("Оставить код","g:promo")]])
        elif kind=="remove":
            self.db.select_promotion(uid,None,None,d["revision"],None,token,self.catalog)
            self.ui.cart_view(chat,uid,notice="Промокод снят. Сумма уже созданных покупок прежняя.")
        elif kind=="repeat":
            self.db.repeat_purchase(uid,d["purchase_id"],d["expected"],token,self.catalog)
            self.ui.cart_view(chat,uid,notice="Состав перенесён в корзину. Проверь размеры и при необходимости задай персонализацию заново. Новая покупка и оплата ещё не созданы.")
        elif kind=="campaign_check":
            self.db.require_staff(uid,"promotions.write",self.owners)
            text="<b>"+("Включить новые применения?" if d["active"] else "Остановить новые применения?")+"</b>\nУсловия и квоты не сбрасываются. Ранее созданные покупки и счета сохраняют свою сумму."
            self.screen(chat,uid,"ПРОМОКОД / РЕШЕНИЕ",text,[[self.action(uid,"Да, включить" if d["active"] else "Да, остановить","campaign_set",**d)],[button("Не менять",f"g:campaign:{d['pid']}")]])
        elif kind=="campaign_set":
            self.db.set_promotion_active(uid,d["pid"],d["active"],d["version"],token,owner_ids=self.owners)
            self.campaign(chat,uid,d["pid"])
        elif kind=="wizard":
            with self.catalog.lock,self.db.commerce_transaction():
                previous,fp=self.db._service_request(uid,token,"growth_wizard",d)
                if previous is None:
                    self.wizard_value(uid,d["field"],d["value"],d["generation"],d["revision"])
                    self.db._service_done(uid,token,"growth_wizard",fp,0)
            self.wizard(chat,uid)
        elif kind=="create":
            with self.catalog.lock,self.db.commerce_transaction():
                self.db.require_staff(uid,"promotions.write",self.owners)
                previous=self.db.service_result(uid,token)
                if previous is None:
                    state=self.wizard_state(uid)
                    if state["generation"]!=d["generation"] or state["revision"]!=d["revision"] or state["field"]!="review" or state.get("paused"): raise ValueError("Предпросмотр уже изменился или на паузе.")
                p=self.db.create_promotion(uid,d["code"],d["rules"],token,self.catalog,owner_ids=self.owners)
                if previous is None: self.db.clear_state(uid)
            self.campaign(chat,uid,p["promotion_id"])
        else: raise ValueError("Открой актуальную карточку.")

    def error(self, chat, uid, exc, *, pause_other=True):
        state=self.db.get_state(uid)
        own_input=state and state[0] in {"growth_code","growth_create"} and not state[1].get("paused")
        self.screen(chat,uid,"КОРЗИНА / НУЖНО ВНИМАНИЕ",esc(str(exc)),[[button("Промокод","g:promo"),button("Корзина","c:cart")],[button("Мои покупки","c:orders"),button("Главная","c:home")]],pause=pause_other and not own_input)

    def handle_callback(self, chat, user, data):
        if not data.startswith("g:"): return False
        uid=int(user["id"])
        try:
            p=data.split(":")
            if p[1]=="a": self.dispatch(chat,uid,p[2])
            elif p[1]=="promo": self.promo(chat,uid)
            elif p[1]=="input": self.prompt(chat,uid)
            elif p[1]=="repeat": self.repeat(chat,uid,int(p[2]),int(p[3]) if len(p)>3 else 0)
            elif p[1]=="campaigns": self.campaigns(chat,uid,int(p[2]) if len(p)>2 else 0)
            elif p[1]=="campaign": self.campaign(chat,uid,int(p[2]))
            elif p[1]=="new": self.new_campaign(chat,uid)
            elif p[1]=="wpage": self.wizard(chat,uid,int(p[2]))
            else: raise ValueError("Открой актуальный раздел.")
        except (ValueError,PermissionError,TypeError,KeyError,IndexError) as exc: self.error(chat,uid,exc)
        return True

    def resume(self, chat, uid):
        state=self.db.get_state(uid)
        if not state or not state[0].startswith("growth_"): return False
        try:
            if state[1]["expires_at"]<=time.time(): raise ValueError("Время ввода истекло. Открой промокод заново.")
            self.db.set_state(uid,state[0],{k:v for k,v in state[1].items() if k!="paused"})
            if state[0]=="growth_create": self.wizard(chat,uid)
            else: self.prompt(chat,uid)
        except (ValueError,PermissionError,KeyError) as exc: self.error(chat,uid,exc)
        return True

    def handle_message(self, chat, user, message):
        uid=int(user["id"]);text=str(message.get("text", "")).strip()
        parts=text.split(maxsplit=1);command=(parts[0] if parts else "").split("@",1)[0].lower()
        mid=message.get("message_id");operation=f"growth-input:{chat}:{mid}"
        hashed=hashlib.sha256(text.encode()).hexdigest()
        prior=self.db.service_result(uid,operation) if type(mid) is int and mid>0 else None
        if prior and prior["kind"]=="growth_input":
            try: self.db._service_request(uid,operation,"growth_input",[hashed])
            except ValueError as exc: self.error(chat,uid,exc,pause_other=False)
            return True
        if command in {"/promos","/newpromo"} or (command=="/promo" and len(parts)==1):
            self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?",(uid,))
            return self.handle_callback(chat,user,{"/promos":"g:campaigns:0","/newpromo":"g:new","/promo":"g:promo"}[command])
        state=self.db.get_state(uid);direct=command=="/promo"
        if state and state[0].startswith("growth_") and (command=="/cancel" or text.lower() in {"отмена","cancel"}):
            self.db.clear_state(uid);self.ui.home(chat,uid);return True
        if not direct and (not state or not state[0].startswith("growth_") or command.startswith("/")): return False
        self.db.connection().execute("DELETE FROM chat_panels WHERE user_id=?",(uid,))
        try:
            if not direct and state[1].get("paused"): self.ui.home(chat,uid);return True
            if not direct and state[1]["expires_at"]<=time.time(): raise ValueError("Время ввода истекло. Открой раздел заново.")
            if type(mid) is not int or mid<1: raise ValueError("Пришли новое сообщение Telegram.")
            with self.catalog.lock,self.db.commerce_transaction():
                _,fp=self.db._service_request(uid,operation,"growth_input",[hashed])
                code=None
                if direct or state[0]=="growth_code":
                    code=parts[1] if direct else text
                    self.db.promotion_candidate(uid,code,self.catalog)  # Validate before retaining a marker or leaving the input.
                    if state and state[0]=="growth_code": self.db.clear_state(uid)
                else:
                    self.wizard_value(uid,state[1]["field"],text,state[1]["generation"],state[1]["revision"])
                self.db._service_done(uid,operation,"growth_input",fp,0)
            if code is not None: self.candidate(chat,uid,code)
            else: self.wizard(chat,uid)
        except (ValueError,PermissionError,TypeError,KeyError) as exc: self.error(chat,uid,exc)
        return True
