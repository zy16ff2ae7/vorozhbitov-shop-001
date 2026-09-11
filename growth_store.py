"""Explicit repeat-to-cart and finite, non-stacking native promotion campaigns.

No provider calls, historical repricing, refunds, marketing or automatic checkout.
A promotion use is spent at committed purchase creation, not at payment. Cancelling
or expiring that purchase does not restore the use (late invoices remain real).
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import time

from commerce_store import CartConflict, encode, now_iso

RULE_FIELDS = {"kind", "value", "max_discount", "min_subtotal", "categories", "starts_at", "ends_at", "total_limit", "user_limit"}


class PromotionError(ValueError):
    def __init__(self, message, code="promotion_changed"):
        super().__init__(message)
        self.checkout_code = code


def promo_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,23}", value.strip()):
        raise ValueError("Код: 3–24 латинских буквы, цифры, дефис или подчёркивание. Не присылай личные данные.")
    return value.strip().upper()


def validate_rules(data, catalog=None):
    if not isinstance(data, dict) or set(data) != RULE_FIELDS:
        raise ValueError("Нужен полный набор правил промокода, без дополнительных полей.")
    if data["kind"] not in {"percent", "fixed"}: raise ValueError("Скидка — процент или сумма в рублях.")
    for key, low, high in (("value",1,90 if data["kind"]=="percent" else 10_000_000),
                          ("max_discount",1,10_000_000),("min_subtotal",0,10_000_000),
                          ("total_limit",1,100_000),("user_limit",1,100),
                          ("starts_at",1,4_102_444_800),("ends_at",1,4_102_444_800)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f"Некорректное правило {key}: целое число от {low} до {high}.")
    if not 0 < data["ends_at"]-data["starts_at"] <= 366*86400:
        raise ValueError("Срок должен быть положительным и не длиннее 366 дней.")
    if data["user_limit"] > data["total_limit"]: raise ValueError("Личный лимит не может быть выше общего.")
    if data["kind"]=="fixed" and data["max_discount"]!=data["value"]:
        raise ValueError("Потолок фиксированной скидки должен равняться её сумме.")
    cats=data["categories"]
    if not isinstance(cats,list) or len(cats)>1 or any(not isinstance(x,str) or not 1<=len(x)<=64 for x in cats) or len(set(cats))!=len(cats):
        raise ValueError("Некорректный список разделов.")
    if catalog is not None and set(cats)-{p["category"] for p in catalog.public_products()}:
        raise ValueError("Раздел больше не представлен в активной витрине.")
    return {**data,"categories":sorted(cats)}


def allocation(amounts, discount):
    """Integer proportional allocation; at least one RUB remains in each line."""
    if not amounts or any(type(x) is not int or x<=0 for x in amounts) or type(discount) is not int or not 1<=discount<=sum(x-1 for x in amounts):
        raise PromotionError("Эту скидку нельзя применить без нулевой цены позиции. Нужна другая корзина или другой код.")
    total=sum(amounts)
    parts=[x*discount//total for x in amounts]
    remaining=discount-sum(parts)
    order=sorted(range(len(amounts)),key=lambda i: (-(amounts[i]*discount%total),i))
    while remaining:
        for i in order:
            if parts[i]<amounts[i]-1 and remaining:
                parts[i]+=1;remaining-=1
    return parts


def quote_fingerprint(quote):
    return hashlib.sha256(encode(quote).encode()).hexdigest()


class GrowthStore:
    def init_growth_store(self):
        self.connection().executescript("""
            CREATE TABLE IF NOT EXISTS promotions (
                promotion_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE, rules TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
                version INTEGER NOT NULL DEFAULT 0, created_by INTEGER NOT NULL REFERENCES users(user_id),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cart_promotions (
                user_id INTEGER PRIMARY KEY REFERENCES shopping_carts(user_id) ON DELETE CASCADE,
                promotion_id INTEGER NOT NULL REFERENCES promotions(promotion_id),
                promotion_version INTEGER NOT NULL, selected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promotion_redemptions (
                payment_id TEXT PRIMARY KEY REFERENCES payments(payment_id),
                promotion_id INTEGER NOT NULL REFERENCES promotions(promotion_id),
                user_id INTEGER NOT NULL REFERENCES users(user_id), code TEXT NOT NULL,
                promotion_version INTEGER NOT NULL, rules TEXT NOT NULL,
                subtotal_rub INTEGER NOT NULL CHECK(subtotal_rub>0),
                discount_rub INTEGER NOT NULL CHECK(discount_rub>0),
                total_rub INTEGER NOT NULL CHECK(total_rub>0 AND total_rub=subtotal_rub-discount_rub),
                allocations TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_promotion_uses ON promotion_redemptions(promotion_id,user_id);
        """)

    def promotion(self, promotion_id):
        row=self.connection().execute("SELECT * FROM promotions WHERE promotion_id=?",(promotion_id,)).fetchone()
        if not row: raise ValueError("Промокод не найден.")
        return {**dict(row),"rules":json.loads(row["rules"])}

    def promotions_for_owner(self, actor, *, owner_ids=frozenset(), page=0):
        self.require_staff(actor,"promotions.write",owner_ids)
        return [self.promotion(r[0]) for r in self.connection().execute("SELECT promotion_id FROM promotions ORDER BY promotion_id DESC LIMIT 6 OFFSET ?",(max(0,page)*6,))]

    def create_promotion(self, actor, code, rules, operation_id, catalog, *, owner_ids=frozenset()):
        with catalog.lock, self.commerce_transaction() as conn:
            self.require_staff(actor,"promotions.write",owner_ids)
            code=promo_code(code);rules=validate_rules(rules,catalog)
            previous,fp=self._service_request(actor,operation_id,"growth_campaign",[code,rules])
            if previous is not None: return self.promotion(previous)
            if rules["ends_at"]<=time.time(): raise ValueError("Промокод не создаётся с уже истёкшим сроком.")
            if conn.execute("SELECT 1 FROM promotions WHERE code=?",(code,)).fetchone():
                raise ValueError("Этот код уже существует. Его историю и лимиты нельзя обнулить новым созданием.")
            pid=conn.execute("INSERT INTO promotions(code,rules,created_by,created_at,updated_at) VALUES (?,?,?,?,?)",(code,encode(rules),actor,now_iso(),now_iso())).lastrowid
            self.audit_staff(actor,"promotion.created",str(pid),None,{"code":code,"rules":rules,"active":False})
            self._service_done(actor,operation_id,"growth_campaign",fp,pid)
            return self.promotion(pid)

    def set_promotion_active(self, actor, pid, active, version, operation_id, *, owner_ids=frozenset()):
        if type(active) is not bool or type(version) is not int: raise ValueError("Нужно явное версионное решение о промокоде.")
        with self.commerce_transaction() as conn:
            self.require_staff(actor,"promotions.write",owner_ids)
            p=self.promotion(pid)
            previous,fp=self._service_request(actor,operation_id,"growth_campaign_state",[pid,active,version])
            if previous is not None: return p  # An old repeat never re-enables a stopped campaign.
            if p["version"]!=version: raise CartConflict("Промокод уже изменён. Открой его актуальную карточку.")
            if active and p["rules"]["ends_at"]<=time.time(): raise ValueError("Нельзя включить истёкший промокод.")
            conn.execute("UPDATE promotions SET active=?,version=version+1,updated_at=? WHERE promotion_id=?",(int(active),now_iso(),pid))
            self.audit_staff(actor,"promotion.enabled" if active else "promotion.disabled",str(pid),{"active":bool(p["active"]),"version":version},{"active":active,"version":version+1})
            self._service_done(actor,operation_id,"growth_campaign_state",fp,pid)
            return self.promotion(pid)

    def promotion_usage(self, pid, user_id=None):
        conn=self.connection()
        return {"total":conn.execute("SELECT COUNT(*) FROM promotion_redemptions WHERE promotion_id=?",(pid,)).fetchone()[0],
                "user":conn.execute("SELECT COUNT(*) FROM promotion_redemptions WHERE promotion_id=? AND user_id=?",(pid,user_id)).fetchone()[0] if user_id is not None else None}

    def cart_promotion(self, uid):
        row=self.connection().execute("SELECT c.promotion_id,c.promotion_version,p.code FROM cart_promotions c JOIN promotions p USING(promotion_id) WHERE c.user_id=?",(uid,)).fetchone()
        return {**dict(row),"native_checkout":True} if row else None

    def _promotion_available(self, p, uid):
        r=p["rules"];now=time.time()
        if not p["active"] or not r["starts_at"]<=now<r["ends_at"]:
            raise PromotionError("Промокод сейчас не действует. Сними его или выбери другой; полную цену автоматически не подставляем.")
        use=self.promotion_usage(p["promotion_id"],uid)
        if use["total"]>=r["total_limit"] or use["user"]>=r["user_limit"]:
            raise PromotionError("Достигнут лимит применений промокода. Отмена покупки не возвращает применение.")

    def growth_quote(self, uid, catalog, *, candidate=None):
        """Actual cart and current prices. Candidate is an explicit code preview."""
        with catalog.lock, self.commerce_transaction():
            cart=self.cart(uid)
            clean=self.canonical_cart(cart["items"],catalog)
            if not clean: raise ValueError("Сначала добавь вещи в корзину.")
            lookup={p["id"]:p for p in catalog.public_products()}
            lines=[]
            for x in clean:
                p=lookup[x["product_id"]];price=p["price_rub"]
                if type(price) is not int or price<=0: raise ValueError("Есть вещь с неизвестной ценой. Обнови корзину.")
                lines.append({**x,"category":p["category"],"subtotal_rub":price*x["quantity"],"discount_rub":0})
            selected=cart["promotion"]
            p=candidate
            if p is None and selected:
                p=self.promotion(selected["promotion_id"])
                if p["version"]!=selected["promotion_version"]:
                    raise PromotionError("Состояние промокода изменилось. Примени его заново или явно сними.")
            subtotal=sum(x["subtotal_rub"] for x in lines)
            discount=0
            if p:
                self._promotion_available(p,uid);r=p["rules"]
                eligible=[i for i,x in enumerate(lines) if not r["categories"] or x["category"] in r["categories"]]
                amount=sum(lines[i]["subtotal_rub"] for i in eligible)
                if not eligible or amount<r["min_subtotal"]:
                    raise PromotionError("Не выполнены условия по разделу или минимальной сумме подходящих товаров.")
                discount=min(amount*r["value"]//100,r["max_discount"]) if r["kind"]=="percent" else r["value"]
                if discount<=0: raise PromotionError("После округления скидка равна 0 ₽. Не будем расходовать применение без выгоды.")
                parts=allocation([lines[i]["subtotal_rub"] for i in eligible],discount)
                for i,part in zip(eligible,parts): lines[i]["discount_rub"]=part
            for x in lines: x["amount_rub"]=x["subtotal_rub"]-x["discount_rub"]
            return {"cart_revision":cart["revision"],"promotion_id":p["promotion_id"] if p else None,
                    "promotion_version":p["version"] if p else None,"code":p["code"] if p else None,
                    "rules":p["rules"] if p else None,"subtotal_rub":subtotal,"discount_rub":discount,
                    "total_rub":subtotal-discount,"lines":lines}

    def promotion_candidate(self, uid, code, catalog):
        code=promo_code(code)
        with catalog.lock, self.commerce_transaction() as conn:
            row=conn.execute("SELECT promotion_id FROM promotions WHERE code=?",(code,)).fetchone()
            if not row: raise PromotionError("Промокод недоступен. Проверь написание или выбери другой.")
            return self.growth_quote(uid,catalog,candidate=self.promotion(row[0]))

    def select_promotion(self, uid, pid, version, revision, expected_quote, operation_id, catalog):
        with catalog.lock, self.commerce_transaction() as conn:
            previous,fp=self._service_request(uid,operation_id,"growth_select",[pid,version,revision,expected_quote])
            if previous is not None: return self.cart(uid)
            current=self.cart(uid)
            if type(revision) is not int or revision!=current["revision"]: raise CartConflict("Корзина изменилась. Проверь скидку заново.")
            conn.execute("INSERT OR IGNORE INTO shopping_carts VALUES (?,0,?)",(uid,now_iso()))
            if pid is not None:
                p=self.promotion(pid)
                if type(version) is not int or version!=p["version"]: raise CartConflict("Условия промокода изменились.")
                quote=self.growth_quote(uid,catalog,candidate=p)
                if quote_fingerprint(quote)!=expected_quote: raise CartConflict("Сумма или состав изменились. Проверь скидку заново.")
                conn.execute("INSERT INTO cart_promotions VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET promotion_id=excluded.promotion_id,promotion_version=excluded.promotion_version,selected_at=excluded.selected_at",(uid,pid,version,now_iso()))
            else:
                conn.execute("DELETE FROM cart_promotions WHERE user_id=?",(uid,))
            conn.execute("UPDATE shopping_carts SET revision=revision+1,updated_at=? WHERE user_id=?",(now_iso(),uid))
            self._service_done(uid,operation_id,"growth_select",fp,0)
            self.event(uid,"promotion_selected" if pid else "promotion_removed",{"promotion_id":pid})
            return self.cart(uid)

    def growth_checkout_preview(self, uid, catalog):
        with catalog.lock, self.commerce_transaction():
            quote=self.growth_quote(uid,catalog)
            token=None
            if quote["promotion_id"] is not None:
                token=self.create_chat_action(uid,"growth.checkout",{"fingerprint":quote_fingerprint(quote)}).split(":")[-1]
            return quote,token

    def prepare_promotion(self, uid, payload, lines, catalog):
        """Called under catalog lock; DB commit repeats the quota/state checks."""
        selected=self.cart_promotion(uid)
        token=payload.get("promotion_token")
        if not selected:
            if token is not None: raise PromotionError("Выбор промокода уже изменён. Проверь корзину.")
            return None
        if not isinstance(token,str) or not re.fullmatch(r"[a-f0-9]{16}",token):
            raise PromotionError("В корзине выбран промокод. Оформи её через /cart в боте или явно сними код там. В Mini App скидку не теряем молча.","promotion_requires_chat")
        kind,data=self.chat_action(uid,token)
        if kind!="growth.checkout": raise PromotionError("Нужна актуальная проверка скидки в боте.")
        quote=self.growth_quote(uid,catalog)
        if quote_fingerprint(quote)!=data["fingerprint"] or payload.get("cart_revision")!=quote["cart_revision"]:
            raise PromotionError("Состав, цена или промокод изменились. Проверь новое оформление в боте.")
        canonical=self.canonical_cart(lines)
        if len(canonical)!=len(lines) or canonical!=self.canonical_cart(quote["lines"]):
            raise PromotionError("Промокод относится к другой версии корзины.")
        expected={(x["product_id"],x["size"],x["person"]):x for x in quote["lines"]}
        if any(x["amount_rub"]!=expected[(x["product_id"],x["size"],x.get("person", ""))]["subtotal_rub"] for x in lines):
            raise PromotionError("Цена изменилась. Проверь оформление заново.")
        return {**quote,"_action_token":token}

    def validate_promotion_commit(self, uid, quote, lines, cart_revision):
        selected=self.cart_promotion(uid)
        if quote is None:
            if selected: raise PromotionError("Промокод требует проверки в /cart бота.","promotion_requires_chat")
            return
        if not selected or selected["promotion_id"]!=quote["promotion_id"] or selected["promotion_version"]!=quote["promotion_version"]:
            raise PromotionError("Выбранный промокод уже изменён.")
        kind,data=self.chat_action(uid,quote.get("_action_token"))
        if kind!="growth.checkout" or data["fingerprint"]!=quote_fingerprint({k:v for k,v in quote.items() if k!="_action_token"}):
            raise PromotionError("Проверка скидки устарела или изменилась.")
        p=self.promotion(quote["promotion_id"])
        if p["version"]!=quote["promotion_version"] or p["rules"]!=quote["rules"]: raise PromotionError("Состояние промокода изменилось.")
        if type(cart_revision) is not int or self.cart(uid)["revision"]!=cart_revision or quote["cart_revision"]!=cart_revision:
            error=CartConflict("Корзина изменилась. Проверь новое оформление.");error.checkout_revision_stale=True;raise error
        self._promotion_available(p,uid)
        lookup={(x["product_id"],x["size"],x["person"]):x for x in quote["lines"]}
        if (len(lines)!=len(lookup) or self.canonical_cart(lines)!=self.canonical_cart(quote["lines"])
                or sum(x["amount_rub"] for x in lines)!=quote["total_rub"]
                or any(type(x["amount_rub"]) is not int or x["amount_rub"]<=0
                       or x["amount_rub"]!=lookup[(x["product_id"],x["size"],x.get("person", ""))]["amount_rub"] for x in lines)):
            raise ValueError("Некорректное распределение скидки.")

    def record_promotion(self, uid, payment_id, quote, lines, order_ids):
        if quote is None: return
        lookup={(x["product_id"],x["size"],x["person"]):x for x in quote["lines"]}
        parts=[]
        for order_id,line in zip(order_ids,lines):
            q=lookup[(line["product_id"],line["size"],line.get("person", ""))]
            parts.append({"order_id":order_id,"subtotal_rub":q["subtotal_rub"],"discount_rub":q["discount_rub"],"amount_rub":q["amount_rub"]})
        self.connection().execute("INSERT INTO promotion_redemptions VALUES (?,?,?,?,?,?,?,?,?,?,?)",(payment_id,quote["promotion_id"],uid,quote["code"],quote["promotion_version"],encode(quote["rules"]),quote["subtotal_rub"],quote["discount_rub"],quote["total_rub"],encode(parts),now_iso()))
        self.event(uid,"promotion_redeemed",{"promotion_id":quote["promotion_id"],"payment_id":payment_id})

    def purchase_promotion(self, payment_id):
        row=self.connection().execute("SELECT code,subtotal_rub,discount_rub,total_rub,allocations FROM promotion_redemptions WHERE payment_id=?",(payment_id,)).fetchone()
        return {**dict(row),"allocations":json.loads(row["allocations"])} if row else None

    def repeat_preview(self, uid, purchase_id, catalog):
        with catalog.lock, self.commerce_transaction():
            p=self.purchase_view(purchase_id,user_id=uid)
            if p["payment_status"]!="paid" or p["payment_attention"] or p["fulfillment"] in {"cancelled","expired"}:
                raise ValueError("Повтор доступен для своей оплаченной покупки без финансовых исключений. Для незавершённой — открой её оплату, не создавай дубликат.")
            original=self.orders_for_payment(p["payment_id"])
            if any(r["user_id"]!=uid or r["amount_rub"]<=0 for r in original) or sum(r["amount_rub"] for r in original)!=p["amount_rub"]:
                raise ValueError("Состав прежней покупки требует сверки.")
            cart=self.cart(uid)
            added=[{"product_id":r["product_id"],"size":r["size"],"quantity":r["quantity"],"person":""} for r in original]
            combined={}
            for x in cart["items"]+added:
                key=(x["product_id"],x["size"],x["person"])
                if key in combined: combined[key]["quantity"]+=x["quantity"]
                else: combined[key]={k:x[k] for k in ("product_id","size","person","quantity")}
            merged=self.canonical_cart(list(combined.values()),catalog)
            inventory=self.inventory_view();needed=defaultdict(int);rows=[];total=0
            prices={x["id"]:x["price_rub"] for x in catalog.public_products()}
            for x in merged:
                product=catalog.get(x["product_id"])
                amount=prices[x["product_id"]]*x["quantity"]
                if amount<=0: raise ValueError("Нельзя повторить вещь с неизвестной ценой.")
                key=(x["product_id"],x["size"]);needed[key]+=x["quantity"]
                stock=inventory.get(key[0],{}).get(key[1],{}).get("available")
                if stock is None: raise ValueError("Наличие одного из размеров не подтверждено. Уточни остаток; ничего в корзину не перенесено.")
                if needed[key]>stock: raise ValueError("На весь объединённый состав не хватает выбранного размера. Другой размер или меньшее количество не подставляем; корзина не изменена.")
                total+=amount
                rows.append({**x,"name":product["name"],"amount_rub":amount,"available":stock,"personalization_available":bool(product.get("personalization"))})
            return {"purchase_id":purchase_id,"source_version":p["version"],"cart_revision":cart["revision"],"current_promotion":cart["promotion"],"items":rows,"total_rub":total,"added_quantity":sum(x["quantity"] for x in added)}

    def repeat_purchase(self, uid, purchase_id, expected, operation_id, catalog):
        with catalog.lock, self.commerce_transaction() as conn:
            previous,fp=self._service_request(uid,operation_id,"growth_repeat",[purchase_id,expected])
            if previous is not None: return self.cart(uid)
            preview=self.repeat_preview(uid,purchase_id,catalog)
            if quote_fingerprint(preview)!=expected: raise CartConflict("Цена, наличие или корзина изменились. Открой повтор покупки заново.")
            self.replace_cart(uid,self.canonical_cart(preview["items"]),preview["cart_revision"],"repeat-"+operation_id,catalog)
            # Explicitly disclosed on the confirmation screen; neither an old
            # purchase's code nor the current cart's code is silently carried.
            conn.execute("DELETE FROM cart_promotions WHERE user_id=?",(uid,))
            self._service_done(uid,operation_id,"growth_repeat",fp,0)
            self.event(uid,"purchase_repeated_to_cart",{"purchase_id":purchase_id})
            return self.cart(uid)

    def prune_growth(self):
        with self.commerce_transaction() as conn:
            conn.execute("DELETE FROM service_requests WHERE kind='growth_input' AND julianday(created_at)<julianday('now','-7 days')")
            # Financial discount records/campaigns/repeat markers are retained.
            conn.execute("DELETE FROM states WHERE state LIKE 'growth_%' AND json_extract(data,'$.expires_at')<?",(time.time(),))
