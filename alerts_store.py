"""Consented one-shot product watches and an at-most-one-attempt delivery queue.

Not the transactional payment outbox or a marketing list. No network, checkout,
stock mutation, legacy waitlist import, or automatic retry after an uncertain send.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
from urllib.parse import urlsplit

from commerce_store import CartConflict, encode, now_iso

TERMS_VERSION = "product-alerts-2026-09-11-v1"
WATCH_TTL = 30 * 86400
QUEUE_TTL = 86400
SEND_LEASE = 120
HISTORY_TTL = 30 * 86400
MAX_WATCHES = 20
DAILY_ATTEMPTS = 2
MIN_INTERVAL = 300
LIVE = ("watching", "queued", "sending")
STATE_NAMES = {"watching":"наблюдаем", "queued":"сигнал ожидает проверки", "sending":"отправка начата",
               "sent":"Telegram подтвердил отправку", "uncertain":"исход отправки неизвестен",
               "cancelled":"отключена", "expired":"срок истёк", "changed":"нужно новое подтверждение", "blocked":"доставка недоступна"}


def policy_fingerprint(url):
    if not isinstance(url,str) or len(url)>512 or any(ord(c)<33 for c in url):
        raise ValueError("Оператору нужно указать публичную HTTPS-политику обработки данных.")
    try:
        parts=urlsplit(url)
        if parts.scheme!="https" or not parts.hostname or parts.username or parts.password:
            raise ValueError()
    except ValueError:
        raise ValueError("Оператору нужно указать публичную HTTPS-политику обработки данных.")
    return hashlib.sha256(url.encode()).hexdigest()


def product_identity(p):
    # Changes to the actual description/variants require new consent. Price and
    # stock intentionally do not: those are exactly the values being watched.
    fields=("id","name","category","sizes","description","material","fit","details","personalization")
    return hashlib.sha256(encode({k:p.get(k) for k in fields}).encode()).hexdigest()


def stamp_value(now=None):
    value=time.time() if now is None else now
    if type(value) not in {float,int} or not math.isfinite(value) or value<0:
        raise ValueError("Некорректное время проверки.")
    return value


class AlertsStore:
    def init_alerts_store(self):
        self.connection().executescript("""
            CREATE TABLE IF NOT EXISTS product_alert_accounts (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                generation INTEGER NOT NULL DEFAULT 0,
                attempt_times TEXT NOT NULL DEFAULT '[]', next_attempt_at REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_watches (
                watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES product_alert_accounts(user_id),
                generation INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('stock','price')),
                product_id TEXT NOT NULL, size TEXT NOT NULL DEFAULT '',
                identity_digest TEXT NOT NULL,
                anchor_rub INTEGER, limit_rub INTEGER,
                terms_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
                consent_at TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('watching','queued','sending','sent','uncertain','cancelled','expired','changed','blocked')),
                checked_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_live_variant
                ON product_watches(user_id,kind,product_id,size) WHERE state IN ('watching','queued','sending');
            CREATE INDEX IF NOT EXISTS idx_watch_scan ON product_watches(state,checked_at,watch_id);
            CREATE INDEX IF NOT EXISTS idx_watch_owner ON product_watches(user_id,watch_id);
            CREATE TABLE IF NOT EXISTS product_alert_deliveries (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL REFERENCES product_watches(watch_id) ON DELETE CASCADE,
                state TEXT NOT NULL CHECK(state IN ('queued','sending','sent','uncertain','cancelled')),
                reason TEXT NOT NULL DEFAULT '', observed_price INTEGER, available INTEGER,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                claim_token TEXT, started_at REAL, finished_at REAL, message_id INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_one_delivery
                ON product_alert_deliveries(watch_id) WHERE state<>'cancelled';
            CREATE INDEX IF NOT EXISTS idx_watch_delivery_queue ON product_alert_deliveries(state,delivery_id);
            CREATE TABLE IF NOT EXISTS product_alert_stock (
                product_id TEXT NOT NULL, size TEXT NOT NULL,
                last_available INTEGER, credits INTEGER NOT NULL CHECK(credits>=0),
                updated_at REAL NOT NULL, PRIMARY KEY(product_id,size)
            );
        """)
        conn=self.connection()
        if "next_attempt_at" not in {r[1] for r in conn.execute("PRAGMA table_info(product_alert_accounts)")}:
            conn.execute("ALTER TABLE product_alert_accounts ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
            for row in conn.execute("SELECT user_id FROM product_alert_accounts").fetchall():
                conn.execute("UPDATE product_alert_accounts SET next_attempt_at=? WHERE user_id=?",(self._next_alert_attempt(row[0],time.time()),row[0]))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_account_due ON product_alert_accounts(next_attempt_at,user_id)")

    def alert_account(self, uid):
        if type(uid) is not int or uid<=0 or not self.get_user(uid):
            raise PermissionError("Открой бота в своём личном чате.")
        r=self.connection().execute("SELECT * FROM product_alert_accounts WHERE user_id=?",(uid,)).fetchone()
        return {**dict(r),"attempt_times":json.loads(r["attempt_times"])} if r else {"user_id":uid,"enabled":0,"generation":0,"attempt_times":[],"next_attempt_at":0,"updated_at":None}

    def _next_alert_attempt(self, uid, now):
        times=[t for t in self.alert_account(uid)["attempt_times"] if t>now-86400]
        # Preserve pacing across opt-out/re-enable. Clock rollback is conservative.
        if len(times)>=DAILY_ATTEMPTS: return max(times[-1]+MIN_INTERVAL,times[-DAILY_ATTEMPTS]+86400)
        return times[-1]+MIN_INTERVAL if times else 0

    def _alert_product(self, pid, catalog):
        p=next((p for p in catalog.public_products() if p["id"]==pid),None)
        if not p: raise ValueError("Вещь недоступна в действующей витрине.")
        return p

    def alert_candidate(self, uid, kind, pid, size, limit_rub, catalog):
        with catalog.lock, self.commerce_transaction():
            account=self.alert_account(uid)
            if kind not in {"stock","price"}: raise ValueError("Выбери наличие размера или снижение цены.")
            p=self._alert_product(pid,catalog);price=p["price_rub"]
            available=None
            if kind=="stock":
                if not isinstance(size,str) or size not in p["sizes"] or limit_rub is not None:
                    raise ValueError("Выбери один действующий размер.")
                available=self.inventory_view().get(pid,{}).get(size,{}).get("available")
                if available is not None and available>0:
                    raise ValueError("Размер уже доступен. Открой карточку и проверь покупку; подписка на появление не создана.")
                anchor=None
            else:
                if size!="" or type(price) is not int or price<=1:
                    raise ValueError("Для снижения нужна известная каталожная цена больше 1 ₽.")
                if type(limit_rub) is not int or not 1<=limit_rub<price:
                    raise ValueError("Порог — целые рубли от 1 до текущей цены минус 1. Уже достигнутый порог не превращается в новую подписку.")
                anchor=price
            return {"generation":account["generation"],"kind":kind,"product_id":pid,"size":size,
                    "identity_digest":product_identity(p),"anchor_rub":anchor,"limit_rub":limit_rub,
                    "name":p["name"],"available":available}

    def subscribe_alert(self, uid, candidate, operation_id, catalog, *, policy_url, terms_version=TERMS_VERSION):
        if not isinstance(candidate,dict): raise ValueError("Нужно актуальное подтверждение подписки.")
        policy_hash=policy_fingerprint(policy_url)
        if terms_version!=TERMS_VERSION: raise ValueError("Условия изменились. Подтверди подписку заново.")
        with catalog.lock, self.commerce_transaction() as conn:
            a=self.alert_account(uid);now=stamp_value()
            if type(candidate.get("generation")) is not int or candidate["generation"]!=a["generation"]:
                raise CartConflict("Разрешение на уведомления уже изменилось. Старая кнопка его не восстановит.")
            keys=("generation","kind","product_id","size","identity_digest","anchor_rub","limit_rub")
            intent={k:candidate.get(k) for k in keys}
            previous,fp=self._service_request(uid,operation_id,"watch_subscribe",[intent,policy_hash,terms_version])
            if previous is not None:
                r=conn.execute("SELECT * FROM product_watches WHERE watch_id=? AND user_id=?",(previous,uid)).fetchone()
                if not r: raise ValueError("Эта подписка уже удалена. Новую нужно подтвердить отдельно.")
                return dict(r)
            if self.get_user(uid)["is_blocked"]: raise ValueError("Доставка в этот личный чат недоступна.")
            current=self.alert_candidate(uid,candidate["kind"],candidate["product_id"],candidate["size"],candidate["limit_rub"],catalog)
            if intent!={k:current[k] for k in keys}:
                raise CartConflict("Карточка или базовая цена изменились. Проверь подписку заново.")
            self._expire_alerts(now,uid)
            existing=conn.execute("SELECT 1 FROM product_watches WHERE user_id=? AND kind=? AND product_id=? AND size=? AND state IN ('watching','queued','sending')",(uid,candidate["kind"],candidate["product_id"],candidate["size"])).fetchone()
            if existing: raise ValueError("Такая активная подписка уже есть. Срок не продлеваем повторным нажатием; для нового порога сначала отключи прежнюю.")
            if conn.execute("SELECT COUNT(*) FROM product_watches WHERE user_id=? AND state IN ('watching','queued','sending')",(uid,)).fetchone()[0]>=MAX_WATCHES:
                raise ValueError("Одновременно можно наблюдать до 20 условий. Сначала отключи ненужную подписку.")
            conn.execute("INSERT OR IGNORE INTO product_alert_accounts(user_id,updated_at) VALUES (?,?)",(uid,now_iso()))
            conn.execute("UPDATE product_alert_accounts SET enabled=1,updated_at=? WHERE user_id=?",(now_iso(),uid))
            # Requesting a service signal is NOT contact or marketing consent.
            conn.execute("UPDATE users SET service_only=1 WHERE user_id=? AND consent_at IS NULL",(uid,))
            wid=conn.execute("""INSERT INTO product_watches(user_id,generation,kind,product_id,size,identity_digest,
                anchor_rub,limit_rub,terms_version,policy_hash,consent_at,created_at,expires_at,state,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'watching',?)""",
                (uid,a["generation"],candidate["kind"],candidate["product_id"],candidate["size"],candidate["identity_digest"],
                 candidate["anchor_rub"],candidate["limit_rub"],terms_version,policy_hash,now_iso(),now,now+WATCH_TTL,now)).lastrowid
            if candidate["kind"]=="stock": self._observe_alert_stock(candidate["product_id"],candidate["size"],current["available"],now)
            self._service_done(uid,operation_id,"watch_subscribe",fp,wid)
            self.event(uid,"product_alert_subscribed",{"watch_id":wid,"kind":candidate["kind"]})
            return dict(conn.execute("SELECT * FROM product_watches WHERE watch_id=?",(wid,)).fetchone())

    def _retire_alert(self, wid, state, now):
        conn=self.connection()
        conn.execute("UPDATE product_watches SET state=?,updated_at=? WHERE watch_id=? AND state IN ('watching','queued','sending')",(state,now,wid))
        conn.execute("UPDATE product_alert_deliveries SET state='cancelled',reason=?,finished_at=? WHERE watch_id=? AND state IN ('queued','sending')",(state,now,wid))

    def stop_alert(self, uid, wid, operation_id):
        with self.commerce_transaction() as conn:
            self.alert_account(uid)
            previous,fp=self._service_request(uid,operation_id,"watch_stop",[wid])
            if previous is not None: return
            row=conn.execute("SELECT * FROM product_watches WHERE watch_id=? AND user_id=?",(wid,uid)).fetchone()
            if not row: raise ValueError("Подписка не найдена в твоём аккаунте.")
            self._retire_alert(wid,"cancelled",stamp_value())
            self._service_done(uid,operation_id,"watch_stop",fp,0)
            self.event(uid,"product_alert_stopped",{"watch_id":wid})

    def stop_all_alerts(self, uid, generation, operation_id):
        with self.commerce_transaction() as conn:
            a=self.alert_account(uid)
            previous,fp=self._service_request(uid,operation_id,"watch_stop_all",[generation])
            if previous is not None: return False
            if type(generation) is not int or generation!=a["generation"]:
                raise CartConflict("Настройки уже изменились. Открой /alerts или отправь новую /alertsoff.")
            conn.execute("INSERT OR IGNORE INTO product_alert_accounts(user_id,updated_at) VALUES (?,?)",(uid,now_iso()))
            conn.execute("UPDATE product_alert_accounts SET enabled=0,generation=generation+1,updated_at=? WHERE user_id=?",(now_iso(),uid))
            conn.execute("UPDATE users SET service_only=1 WHERE user_id=? AND consent_at IS NULL",(uid,))
            conn.execute("DELETE FROM product_watches WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM chat_actions WHERE user_id=? AND kind LIKE 'watch.%'",(uid,))
            conn.execute("DELETE FROM states WHERE user_id=? AND state LIKE 'watch_%'",(uid,))
            self._service_done(uid,operation_id,"watch_stop_all",fp,0)
            self.event(uid,"product_alerts_disabled")
            return True

    def _expire_alerts(self, now, uid=None):
        conn=self.connection()
        rows=list(conn.execute("SELECT watch_id FROM product_watches WHERE state IN ('watching','queued','sending') AND expires_at<=? AND (? IS NULL OR user_id=?)",(now,uid,uid)))
        for r in rows: self._retire_alert(r[0],"expired",now)
        # A process that died after claiming may already have sent the message.
        rows=list(conn.execute("SELECT delivery_id,watch_id FROM product_alert_deliveries WHERE state='sending' AND started_at<=?",(now-SEND_LEASE,)))
        for r in rows:
            conn.execute("UPDATE product_alert_deliveries SET state='uncertain',reason='interrupted',finished_at=? WHERE delivery_id=?",(now,r[0]))
            conn.execute("UPDATE product_watches SET state='uncertain',updated_at=? WHERE watch_id=? AND state='sending'",(now,r[1]))

    def _observe_alert_stock(self, pid, size, available, now):
        conn=self.connection()
        if available is not None and (type(available) is not int or available<0): raise ValueError("Некорректный подтверждённый остаток.")
        old=conn.execute("SELECT * FROM product_alert_stock WHERE product_id=? AND size=?",(pid,size)).fetchone()
        previous=old["last_available"] if old else None;credits=old["credits"] if old else 0
        if available is None or available==0: credits=0
        elif previous is None or previous<=0: credits=available
        elif available>previous: credits=min(available,credits+available-previous)
        else: credits=min(available,credits)
        conn.execute("INSERT INTO product_alert_stock VALUES (?,?,?,?,?) ON CONFLICT(product_id,size) DO UPDATE SET last_available=excluded.last_available,credits=excluded.credits,updated_at=excluded.updated_at",(pid,size,available,credits,now))
        return credits

    def _watch_current(self, row, products, inventory, policy_hash, now):
        a=self.alert_account(row["user_id"])
        if not a["enabled"] or a["generation"]!=row["generation"]: return "cancelled",None,None
        if self.get_user(row["user_id"])["is_blocked"]: return "blocked",None,None
        if row["expires_at"]<=now: return "expired",None,None
        p=products.get(row["product_id"])
        if not p or row["identity_digest"]!=product_identity(p) or row["terms_version"]!=TERMS_VERSION or row["policy_hash"]!=policy_hash:
            return "changed",None,None
        if row["kind"]=="stock":
            if row["size"] not in p["sizes"]: return "changed",None,None
            available=inventory.get(p["id"],{}).get(row["size"],{}).get("available")
            return None,p,available
        return None,p,None

    def _cancel_queued_alert(self, delivery, now, reason="condition_changed"):
        conn=self.connection()
        conn.execute("UPDATE product_alert_deliveries SET state='cancelled',reason=?,finished_at=? WHERE delivery_id=? AND state='queued'",(reason,now,delivery["delivery_id"]))
        conn.execute("UPDATE product_watches SET state='watching',updated_at=? WHERE watch_id=? AND state='queued'",(now,delivery["watch_id"]))

    def scan_product_alerts(self, catalog, *, policy_url, now=None, limit=200):
        now=stamp_value(now);policy_hash=policy_fingerprint(policy_url);limit=max(1,min(int(limit),1000))
        with catalog.lock,self.commerce_transaction() as conn:
            self._expire_alerts(now)
            products={p["id"]:p for p in catalog.public_products()};inventory=self.inventory_view()
            rows=list(conn.execute("SELECT * FROM product_watches WHERE state IN ('watching','queued','sending') ORDER BY checked_at,watch_id LIMIT ?",(limit,)))
            stocks=set();queued=0
            for row in rows:
                reason,p,available=self._watch_current(row,products,inventory,policy_hash,now)
                conn.execute("UPDATE product_watches SET checked_at=? WHERE watch_id=?",(now,row["watch_id"]))
                if reason:
                    self._retire_alert(row["watch_id"],reason,now);continue
                if row["kind"]=="stock":
                    stocks.add((row["product_id"],row["size"]));self._observe_alert_stock(row["product_id"],row["size"],available,now)
                eligible=(available is not None and available>0) if row["kind"]=="stock" else (type(p["price_rub"]) is int and 0<p["price_rub"]<=row["limit_rub"] and p["price_rub"]<row["anchor_rub"])
                delivery=conn.execute("SELECT * FROM product_alert_deliveries WHERE watch_id=? AND state='queued'",(row["watch_id"],)).fetchone()
                if delivery and (not eligible or delivery["expires_at"]<=now or self._next_alert_attempt(row["user_id"],now)>now):
                    self._cancel_queued_alert(delivery,now);delivery=None
                if eligible and row["kind"]=="price" and row["state"] in {"watching","queued"} and not delivery and self._next_alert_attempt(row["user_id"],now)<=now:
                    queued+=self._queue_product_alert(row,p["price_rub"],None,now)
            # Queue oldest eligible subscriptions, bounded by new free stock.
            # A signal consumes a credit only when an attempt actually starts.
            for pid,size in stocks:
                credits=conn.execute("SELECT credits FROM product_alert_stock WHERE product_id=? AND size=?",(pid,size)).fetchone()[0]
                pending=conn.execute("SELECT COUNT(*) FROM product_alert_deliveries d JOIN product_watches w USING(watch_id) WHERE w.product_id=? AND w.size=? AND w.kind='stock' AND d.state='queued'",(pid,size)).fetchone()[0]
                candidates=list(conn.execute("SELECT * FROM product_watches WHERE kind='stock' AND product_id=? AND size=? AND state='watching' ORDER BY created_at,watch_id LIMIT ?",(pid,size,limit)))
                for row in candidates:
                    if credits<=pending or queued>=limit: break
                    reason,p,available=self._watch_current(row,products,inventory,policy_hash,now)
                    if reason: self._retire_alert(row["watch_id"],reason,now);continue
                    if available is None or available<=0 or self._next_alert_attempt(row["user_id"],now)>now: continue
                    queued+=self._queue_product_alert(row,p["price_rub"],available,now);pending+=1
            return queued

    def _queue_product_alert(self, row, price, available, now):
        conn=self.connection()
        cursor=conn.execute("INSERT OR IGNORE INTO product_alert_deliveries(watch_id,state,observed_price,available,created_at,expires_at) VALUES (?,'queued',?,?,?,?)",(row["watch_id"],price if type(price) is int and price>0 else None,available,now,min(row["expires_at"],now+QUEUE_TTL)))
        if cursor.rowcount:
            conn.execute("UPDATE product_watches SET state='queued',updated_at=? WHERE watch_id=? AND state IN ('watching','queued')",(now,row["watch_id"]))
        return int(bool(cursor.rowcount))

    def claim_product_alert(self, catalog, *, policy_url, now=None):
        now=stamp_value(now);policy_hash=policy_fingerprint(policy_url)
        with catalog.lock,self.commerce_transaction() as conn:
            self._expire_alerts(now)
            products={p["id"]:p for p in catalog.public_products()};inventory=self.inventory_view()
            for d in conn.execute("""SELECT d.* FROM product_alert_deliveries d
                    JOIN product_watches w USING(watch_id)
                    JOIN product_alert_accounts a ON a.user_id=w.user_id
                    WHERE d.state='queued' AND a.next_attempt_at<=?
                    ORDER BY d.delivery_id LIMIT 200""",(now,)).fetchall():
                w=conn.execute("SELECT * FROM product_watches WHERE watch_id=?",(d["watch_id"],)).fetchone()
                reason,p,available=self._watch_current(w,products,inventory,policy_hash,now)
                if reason: self._retire_alert(w["watch_id"],reason,now);continue
                if d["expires_at"]<=now:
                    self._cancel_queued_alert(d,now,"queue_expired");continue
                if w["state"]!="queued":
                    self._cancel_queued_alert(d,now,"retired");continue
                if self._next_alert_attempt(w["user_id"],now)>now: continue
                if w["kind"]=="stock":
                    credits=self._observe_alert_stock(w["product_id"],w["size"],available,now)
                    if available is None or available<=0 or credits<=0:
                        self._cancel_queued_alert(d,now);continue
                    # Do not skip an older ready stock signal for this SKU.
                    earlier=conn.execute("""SELECT 1 FROM product_alert_deliveries d2 JOIN product_watches w2 USING(watch_id)
                        JOIN product_alert_accounts a2 ON a2.user_id=w2.user_id
                        WHERE d2.state='queued' AND d2.delivery_id<? AND w2.product_id=? AND w2.size=? AND w2.kind='stock'
                        AND a2.next_attempt_at<=?""",(d["delivery_id"],w["product_id"],w["size"],now)).fetchone()
                    if earlier: continue
                    conn.execute("UPDATE product_alert_stock SET credits=credits-1 WHERE product_id=? AND size=?",(w["product_id"],w["size"]))
                elif not (type(p["price_rub"]) is int and 0<p["price_rub"]<=w["limit_rub"] and p["price_rub"]<w["anchor_rub"]):
                    self._cancel_queued_alert(d,now);continue
                a=self.alert_account(w["user_id"]);times=[t for t in a["attempt_times"] if t>now-86400]+[now]
                claim=secrets.token_hex(16)
                due=max(now+MIN_INTERVAL,times[-DAILY_ATTEMPTS]+86400) if len(times)>=DAILY_ATTEMPTS else now+MIN_INTERVAL
                conn.execute("UPDATE product_alert_accounts SET attempt_times=?,next_attempt_at=?,updated_at=? WHERE user_id=?",(encode(times[-DAILY_ATTEMPTS:]),due,now_iso(),w["user_id"]))
                conn.execute("UPDATE product_alert_deliveries SET state='sending',claim_token=?,started_at=?,observed_price=?,available=? WHERE delivery_id=?",(claim,now,p["price_rub"] if p["price_rub"]>0 else None,available,d["delivery_id"]))
                conn.execute("UPDATE product_watches SET state='sending',updated_at=? WHERE watch_id=?",(now,w["watch_id"]))
                return {"delivery_id":d["delivery_id"],"claim_token":claim,"watch":dict(w),"product":p,"available":available,"observed_at":now}
            return None

    def finish_product_alert(self, delivery_id, claim, *, message_id=None, blocked=False, now=None):
        now=stamp_value(now);sent=type(message_id) is int and message_id>0
        with self.commerce_transaction() as conn:
            d=conn.execute("SELECT d.*,w.user_id FROM product_alert_deliveries d JOIN product_watches w USING(watch_id) WHERE delivery_id=? AND claim_token=?",(delivery_id,claim)).fetchone()
            if not d or d["state"] not in {"sending","uncertain"}: return False
            target="sent" if sent else "uncertain"
            conn.execute("UPDATE product_alert_deliveries SET state=?,reason=?,finished_at=?,message_id=? WHERE delivery_id=?",(target,"" if sent else "telegram_unconfirmed",now,message_id if sent else None,delivery_id))
            conn.execute("UPDATE product_watches SET state=?,updated_at=? WHERE watch_id=? AND state IN ('sending','uncertain')",(target,now,d["watch_id"]))
            conn.execute("DELETE FROM chat_panels WHERE user_id=?",(d["user_id"],))
            if blocked:
                self.mark_blocked(d["user_id"])
                for row in conn.execute("SELECT watch_id FROM product_watches WHERE user_id=? AND state IN ('watching','queued','sending')",(d["user_id"],)).fetchall(): self._retire_alert(row[0],"blocked",now)
            self.event(d["user_id"],"product_alert_sent" if sent else "product_alert_uncertain",{"watch_id":d["watch_id"]})
            return sent

    def alert_watch(self, uid, wid, catalog):
        with catalog.lock,self.commerce_transaction() as conn:
            self.alert_account(uid);self._expire_alerts(stamp_value(),uid)
            row=conn.execute("SELECT * FROM product_watches WHERE watch_id=? AND user_id=?",(wid,uid)).fetchone()
            if not row: raise ValueError("Подписка не найдена в твоём аккаунте.")
            p=catalog.get(row["product_id"])
            visible=bool(p and product_identity(p)==row["identity_digest"])
            return {**dict(row),"name":p["name"] if visible else "Карточка недоступна или изменилась","visible":visible}

    def alert_list(self, uid, catalog, *, history=False, page=0):
        with catalog.lock,self.commerce_transaction() as conn:
            a=self.alert_account(uid);self._expire_alerts(stamp_value(),uid)
            rows=list(conn.execute("SELECT watch_id FROM product_watches WHERE user_id=? AND (state NOT IN ('watching','queued','sending'))=? ORDER BY watch_id DESC",(uid,int(history))))
            page=max(0,min(int(page),max(0,(len(rows)-1)//5)))
            return {"account":a,"total":len(rows),"page":page,"more":(page+1)*5<len(rows),
                    "items":[self.alert_watch(uid,r[0],catalog) for r in rows[page*5:(page+1)*5]]}

    def alert_health(self, actor, *, owner_ids=frozenset()):
        self.require_staff(actor,"alerts.monitor",owner_ids)
        conn=self.connection()
        return {"watches":dict(conn.execute("SELECT state,COUNT(*) FROM product_watches GROUP BY state")),
                "deliveries":dict(conn.execute("SELECT state,COUNT(*) FROM product_alert_deliveries GROUP BY state"))}

    def prune_alerts(self, now=None):
        now=stamp_value(now)
        with self.commerce_transaction() as conn:
            self._expire_alerts(now)
            conn.execute("DELETE FROM product_watches WHERE state NOT IN ('watching','queued','sending') AND updated_at<?",(now-HISTORY_TTL,))
            conn.execute("DELETE FROM product_alert_stock WHERE NOT EXISTS(SELECT 1 FROM product_watches w WHERE w.product_id=product_alert_stock.product_id AND w.size=product_alert_stock.size AND w.kind='stock' AND w.state IN ('watching','queued','sending'))")
            conn.execute("DELETE FROM states WHERE state LIKE 'watch_%' AND json_extract(data,'$.expires_at')<?",(now,))
            conn.execute("DELETE FROM service_requests WHERE kind='watch_input' AND julianday(created_at)<julianday('now','-7 days')")
