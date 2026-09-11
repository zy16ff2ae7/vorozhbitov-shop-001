"""Financial workbench over the immutable payment evidence.

This module NEVER settles a payment, edits a receipt/invoice, executes a refund
or moves stock. A task is not a financial decision. Only authoritative source
changes can make its exception disappear; staff cannot close an active one.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict

from commerce_store import CartConflict, encode, now_iso
from operations_store import clean_text

FINANCE_STATES = {"open": "нужен разбор", "working": "в работе", "waiting": "ожидаем внешнюю сверку", "escalated": "нужно внимание владельца", "closed": "задача разбора закрыта"}
FINANCE_STEPS = {"working": "Продолжить разбор", "waiting": "Ожидаем внешнюю сверку", "escalated": "Передать на контроль владельцу", "checked": "Сведения в карточке просмотрены", "closed": "Закрыть задачу без изменения денег"}
METHOD_NAMES = {"stars": "Telegram Stars", "lava": "Lava", "crypto": "Crypto Pay", "manual": "Ручная отметка"}
STATUS_NAMES = {"applied": "зачтено в покупку", "review_required": "требуется сверка", "refund_required": "требуется разбор возврата", "legacy_unreconciled": "историческая запись, не сверена", "creating": "создание счёта не завершено", "uncertain": "результат создания неизвестен", "legacy_unverified": "старый непроверенный счёт", "issued": "счёт сохранён (это не оплата)", "failed": "создание отклонено", "pending": "ждёт оплаты", "paid": "оплата учтена", "cancelled": "отменено", "expired": "резерв истёк", "processed": "inbox обработан", "deferred": "обработка inbox отложена"}
REASON_NAMES = {"unknown_payment": "не найдена покупка/оплата", "unknown_invoice": "нет сохранённой привязки счёта", "invoice_reference_mismatch": "ссылка на покупку не совпала", "amount_mismatch": "не совпала сумма", "currency_mismatch": "не совпала валюта", "payer_mismatch": "не совпал плательщик Stars", "awaiting_review": "покупка уже находится на сверке", "manual_settlement": "ранее была ручная отметка оплаты", "legacy_settlement": "прежняя оплата не подтверждена новым журналом", "extra_payment": "дополнительное поступление", "late_payment": "поступление после допустимого срока/отмены", "order_cancelled": "отмена после оплаты", "imported_legacy": "импорт старого локального состояния", "conflicting_duplicate": "у одного ID поступления разные сведения", "missing_source": "источник отсутствует — нужна проверка", "stalled_creation": "создание длится больше 2 минут", "inbox_error": "обработчик не завершил финансовый update"}
CASE_KINDS = {"receipt": "Поступление", "attempt": "Создание счёта", "payment": "Состояние оплаты", "inbox": "Финансовый inbox"}


def finance_money(amount, currency):
    if type(amount) is not int:
        return "сумма не подтверждена"
    if currency == "RUB":
        sign, value = ("-", -amount) if amount < 0 else ("", amount)
        return sign + f"{value // 100:,}".replace(",", " ") + (f",{value % 100:02d}" if value % 100 else "") + " ₽"
    if currency == "XTR": return f"{amount:,} XTR".replace(",", " ")
    # Do not guess the exponent of an unrecognised currency, nor convert to RUB.
    return f"{amount:,}".replace(",", " ") + f" мин. ед. {currency}"


def _object(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _local_id(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("Некорректный номер записи.")
    return value


class FinanceStore:
    def init_finance_store(self):
        self.connection().executescript("""
            CREATE TABLE IF NOT EXISTS finance_cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('receipt','attempt','payment','inbox')),
                source_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','working','waiting','escalated','closed')),
                assigned_to INTEGER, version INTEGER NOT NULL DEFAULT 0,
                due_at REAL, due_generation INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(source_kind,source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_finance_cases_due ON finance_cases(due_at) WHERE state<>'closed';
            CREATE INDEX IF NOT EXISTS idx_finance_cases_assigned ON finance_cases(assigned_to,state);
            CREATE TABLE IF NOT EXISTS finance_journal (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES finance_cases(case_id),
                actor_id INTEGER, kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_finance_journal_case ON finance_journal(case_id,event_id);
            CREATE TABLE IF NOT EXISTS finance_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES finance_cases(case_id),
                actor_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_finance_notes_case ON finance_notes(case_id,note_id);
            CREATE TABLE IF NOT EXISTS finance_alerts (
                alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES finance_cases(case_id),
                kind TEXT NOT NULL, generation INTEGER NOT NULL,
                queued_at TEXT, UNIQUE(case_id,kind,generation)
            );
            CREATE INDEX IF NOT EXISTS idx_finance_alert_pending ON finance_alerts(alert_id) WHERE queued_at IS NULL;
            CREATE TABLE IF NOT EXISTS finance_scan (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO finance_scan VALUES (1,0);
        """)

    def _finance_source(self, kind, source_id, *, stamp=None):
        """Live allowlisted evidence. No customer snapshot, URLs or raw inbox."""
        now = time.time() if stamp is None else stamp
        sid = str(source_id)
        if kind not in CASE_KINDS or not 1 <= len(sid) <= 600:
            raise ValueError("Неизвестный источник финансовой записи.")
        if kind in {"receipt", "inbox"}:
            if not re.fullmatch(r"[0-9]{1,19}", sid) or int(sid) > 2**63 - 1:
                raise ValueError("Некорректный номер источника.")
        conn = self.connection()
        table, key = {"receipt": ("payment_receipts", "receipt_id"), "attempt": ("payment_invoice_attempts", "attempt_id"), "payment": ("payments", "payment_id"), "inbox": ("payment_inbox", "update_id")}[kind]
        raw = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (sid,)).fetchone()
        result = {"kind": kind, "source_id": sid, "title": CASE_KINDS[kind], "active": True,
                  "method": "", "currency": "", "amount_minor": None, "status": "missing_source",
                  "reasons": ["missing_source"], "purchase_id": None, "owner_id": None,
                  "payment_id": None, "payment_status": None, "payment_attention": "",
                  "expected_minor": None, "expected_currency": None, "fields": {},
                  "source_label": "источник отсутствует", "received_at": None, "conflicts": []}
        if not raw:
            result["evidence_hash"] = hashlib.sha256(encode([kind, sid, "missing"]).encode()).hexdigest()
            return result
        r = dict(raw)
        payment_id = r.get("payment_id")
        if kind == "inbox":
            payload = _object(r["payload"])
            candidate = payload.get("invoice_payload")
            payment_id = candidate if isinstance(candidate, str) and len(candidate) <= 256 else None
        payment = self.get_payment(payment_id) if payment_id else None
        purchase = conn.execute("SELECT purchase_id,user_id FROM purchases WHERE payment_id=?", (payment_id,)).fetchone() if payment else None
        if payment:
            result.update(payment_id=payment["payment_id"], payment_status=payment["status"],
                          payment_attention=self.payment_attention(payment["payment_id"]), owner_id=payment["user_id"],
                          purchase_id=purchase["purchase_id"] if purchase and purchase["user_id"] == payment["user_id"] else None)
        material = {"record": {}, "payment_attention": result["payment_attention"], "purchase_id": result["purchase_id"], "payment": {k: payment[k] for k in ("payment_id", "user_id", "amount_rub", "amount_stars", "status", "method", "provider_id")} if payment else None}
        if kind == "receipt":
            # Hash every fingerprint, but never load all conflict bodies on a
            # hot settlement/read path. Full observations have their own pages.
            signature = hashlib.sha256()
            conflict_count = 0
            for x in conn.execute("SELECT fingerprint FROM payment_receipt_conflicts WHERE receipt_id=? ORDER BY fingerprint", (sid,)):
                signature.update(x[0].encode() + b"\0")
                conflict_count += 1
            conflicts = conflict_count > 0
            result["conflict_count"] = conflict_count
            result["conflicts"] = [dict(x) for x in conn.execute("SELECT fingerprint,received_at FROM payment_receipt_conflicts WHERE receipt_id=? ORDER BY received_at DESC,fingerprint LIMIT 3", (sid,))]
            reasons = [x for x in r["reason"].split(",") if x]
            if conflicts: reasons.append("conflicting_duplicate")
            result.update(method=r["method"], currency=r["currency"], amount_minor=r["amount_minor"], status=r["status"],
                active=r["status"] != "applied" or r["source"] != "verified_event" or bool(conflicts), reasons=reasons,
                source_label="проверенное входящее событие — не выписка провайдера" if r["source"] == "verified_event" else "импорт локального состояния — не новая оплата",
                received_at=r["received_at"],
                fields={"provider_id": r["provider_id"], "recorded_payment": r["payment_id"], "claimed_ref": r["claimed_ref"], "payer_id": r["payer_id"]})
            invoice = self.get_invoice(r["method"], r["provider_id"]) if r["method"] in {"lava", "crypto"} else None
            if invoice:
                result["fields"]["invoice_payment"] = invoice["payment_id"]
                result["fields"]["invoice_reference"] = invoice["external_ref"]
            if payment:
                result.update(expected_minor=payment["amount_stars"] if r["method"] == "stars" else payment["amount_rub"] * 100,
                              expected_currency="XTR" if r["method"] == "stars" else "RUB")
            trusted_owner = bool(payment and ((r["method"] == "stars" and r["payer_id"] == payment["user_id"]) or
                (invoice and invoice["payment_id"] == payment["payment_id"])))
            if not trusted_owner: result["owner_id"] = None
            material["record"] = r
            material["conflicts"] = signature.hexdigest()
            material["invoice"] = {k: invoice[k] for k in ("payment_id", "external_ref", "amount_minor", "currency")} if invoice else None
        elif kind == "attempt":
            aged = r["created_at"] + 120 <= now
            issued = conn.execute("SELECT invoice_id FROM payment_invoices WHERE payment_id=? AND method=? AND external_ref=? AND currency='RUB' AND amount_minor=? ORDER BY created_at DESC LIMIT 1",
                (r["payment_id"], r["method"], r["external_ref"], payment["amount_rub"] * 100 if payment else -1)).fetchone()
            known_terminal = r["status"] == "issued" and bool(issued)
            # Unknown future states are not permission to close an exception.
            active = not known_terminal
            result["discoverable"] = active and (r["status"] != "creating" or aged)
            result.update(method=r["method"], status=r["status"], active=active,
                source_label="журнал попыток создания — не поступление денег", received_at=r["created_at"],
                reasons=["stalled_creation"] if r["status"] == "creating" and aged else [r["status"]] if active else [],
                expected_minor=payment["amount_rub"] * 100 if payment else None, expected_currency="RUB" if payment else None,
                fields={"attempt_id": r["attempt_id"], "external_ref": r["external_ref"], "terminal": known_terminal, "issued_invoice": issued[0] if issued else None})
            material["issued_invoice"] = issued[0] if issued else None
            material["record"] = {k: r[k] for k in ("attempt_id", "payment_id", "method", "external_ref", "status", "created_at")}
        elif kind == "payment":
            covered = bool(conn.execute("""SELECT 1 FROM payment_receipts r WHERE r.payment_id=? AND
                (r.status<>'applied' OR EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id)) LIMIT 1""", (sid,)).fetchone())
            active = bool(result["payment_attention"]) or r["status"] not in {"paid", "pending", "cancelled", "expired"}
            result.update(method=r["method"] or "manual", status=r["status"], active=active,
                reasons=[r["status"]] if active else [], source_label="агрегированное состояние оплаты, не новое поступление",
                received_at=r["paid_at"] or r["created_at"], expected_minor=r["amount_rub"] * 100, expected_currency="RUB",
                fields={"payment_id": sid, "covered_by_receipt": covered})
            material["record"] = material["payment"]
        else:
            payload = _object(r["payload"])
            result.update(method="stars", status="processed" if r["processed_at"] else "deferred",
                active=not bool(r["processed_at"]),
                reasons=["inbox_error"] if not r["processed_at"] and r["last_error"] else [],
                source_label="Telegram inbox — обработка ещё не равна оплате", received_at=r["received_at"],
                fields={"update_id": r["update_id"], "attempts": r["attempts"], "next_attempt_at": r["next_attempt_at"], "last_error": r["last_error"], "processed_at": r["processed_at"]})
            result["discoverable"] = not r["processed_at"] and bool(r["last_error"])
            if not payment or r["user_id"] != payment["user_id"]: result["owner_id"] = None
            material["record"] = {k: r[k] for k in ("update_id", "user_id", "payload", "processed_at", "last_error")}
            # Repeated retries of the same payload do not invalidate a staff draft.
        material["active"] = result["active"]
        result["evidence_hash"] = hashlib.sha256(encode(material).encode()).hexdigest()
        return result

    def _finance_event(self, case_id, actor, kind, data=None):
        data = data or {}
        self.connection().execute("INSERT INTO finance_journal(case_id,actor_id,kind,data,created_at) VALUES (?,?,?,?,?)", (case_id, actor, kind, encode(data), now_iso()))
        if actor:
            self.audit_staff(actor, "finance." + kind, str(case_id), None, data)

    def _finance_alert(self, case_id, kind, generation):
        self.connection().execute("INSERT OR IGNORE INTO finance_alerts(case_id,kind,generation) VALUES (?,?,?)", (case_id, kind, generation))

    def sync_finance_source(self, kind, source_id, *, stamp=None):
        """Idempotent materialisation, usable inside the money SAVEPOINT chain."""
        with self.commerce_transaction() as conn:
            source = self._finance_source(kind, source_id, stamp=stamp)
            row = conn.execute("SELECT * FROM finance_cases WHERE source_kind=? AND source_id=?", (kind, str(source_id))).fetchone()
            if not row:
                if not source["active"] or not source.get("discoverable", True): return None
                cid = conn.execute("INSERT INTO finance_cases(source_kind,source_id,evidence_hash,active,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (kind, str(source_id), source["evidence_hash"], 1, now_iso(), now_iso())).lastrowid
                self._finance_event(cid, None, "observed")
                self._finance_alert(cid, "observed", 0)
                return cid
            if row["evidence_hash"] != source["evidence_hash"]:
                reopened = bool(source["active"] and row["state"] == "closed")
                state = "open" if reopened else row["state"]
                conn.execute("UPDATE finance_cases SET evidence_hash=?,active=?,state=?,assigned_to=?,due_at=?,due_generation=due_generation+?,version=version+1,updated_at=? WHERE case_id=?",
                    (source["evidence_hash"], int(source["active"]), state, None if reopened else row["assigned_to"], None if reopened else row["due_at"], int(reopened), now_iso(), row["case_id"]))
                self._finance_event(row["case_id"], None, "evidence_changed", {"active": bool(source["active"]), "state": state})
                self._finance_alert(row["case_id"], "evidence", row["version"] + 1)
            return row["case_id"]

    def sync_finance_payment(self, payment_id):
        with self.commerce_transaction() as conn:
            for r in list(conn.execute("SELECT receipt_id FROM payment_receipts WHERE payment_id=?", (payment_id,))):
                self.sync_finance_source("receipt", r[0])
            payment = self.get_payment(payment_id)
            existing = conn.execute("SELECT 1 FROM finance_cases WHERE source_kind='payment' AND source_id=?", (payment_id,)).fetchone()
            if existing or (payment and payment["status"] in {"review_required", "refund_required"} and not conn.execute("""SELECT 1 FROM payment_receipts r WHERE payment_id=? AND
                    (status<>'applied' OR EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id)) LIMIT 1""", (payment_id,)).fetchone()):
                self.sync_finance_source("payment", payment_id)

    def sync_finance(self, *, stamp=None, limit=50):
        """Bounded discovery for pre-upgrade sources + round-robin revalidation.

        New receipts are also hooked transactionally; this scanner is recovery,
        not permission to resolve money by editing these task tables.
        """
        now = time.time() if stamp is None else stamp
        limit = max(1, min(int(limit), 200))
        with self.commerce_transaction() as conn:
            queries = (
                ("receipt", """SELECT r.receipt_id FROM payment_receipts r LEFT JOIN finance_cases f ON f.source_kind='receipt' AND f.source_id=CAST(r.receipt_id AS TEXT)
                    WHERE f.case_id IS NULL AND r.status<>'applied' ORDER BY r.receipt_id LIMIT ?""", (limit,)),
                ("receipt", """SELECT DISTINCT c.receipt_id FROM payment_receipt_conflicts c LEFT JOIN finance_cases f ON f.source_kind='receipt' AND f.source_id=CAST(c.receipt_id AS TEXT)
                    WHERE f.case_id IS NULL ORDER BY c.receipt_id LIMIT ?""", (limit,)),
                ("attempt", """SELECT a.attempt_id FROM payment_invoice_attempts a LEFT JOIN finance_cases f ON f.source_kind='attempt' AND f.source_id=a.attempt_id
                    WHERE f.case_id IS NULL AND a.status IN ('creating','uncertain','legacy_unverified') AND (a.status<>'creating' OR a.created_at<=?) ORDER BY a.created_at LIMIT ?""", (now - 120, limit)),
                ("payment", """SELECT p.payment_id FROM payments p LEFT JOIN finance_cases f ON f.source_kind='payment' AND f.source_id=p.payment_id
                    WHERE f.case_id IS NULL AND p.status IN ('review_required','refund_required') AND NOT EXISTS(SELECT 1 FROM payment_receipts r WHERE r.payment_id=p.payment_id
                        AND (r.status<>'applied' OR EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id))) ORDER BY p.created_at LIMIT ?""", (limit,)),
                ("inbox", """SELECT i.update_id FROM payment_inbox i LEFT JOIN finance_cases f ON f.source_kind='inbox' AND f.source_id=CAST(i.update_id AS TEXT)
                    WHERE f.case_id IS NULL AND i.processed_at IS NULL AND i.last_error<>'' ORDER BY i.received_at LIMIT ?""", (limit,)),
            )
            for kind, query, params in queries:
                for row in list(conn.execute(query, params)): self.sync_finance_source(kind, row[0], stamp=now)
            cursor = conn.execute("SELECT cursor FROM finance_scan WHERE singleton=1").fetchone()[0]
            batch = list(conn.execute("SELECT * FROM finance_cases WHERE case_id>? ORDER BY case_id LIMIT ?", (cursor, limit)))
            for row in batch: self.sync_finance_source(row["source_kind"], row["source_id"], stamp=now)
            conn.execute("UPDATE finance_scan SET cursor=? WHERE singleton=1", (batch[-1]["case_id"] if len(batch) == limit else 0,))

    def _finance_case(self, actor, case_id, owner_ids, permission="finance.read"):
        self.require_staff(actor, permission, owner_ids)
        row = self.connection().execute("SELECT * FROM finance_cases WHERE case_id=?", (_local_id(case_id),)).fetchone()
        if not row: raise ValueError("Задача финансового разбора не найдена.")
        self.sync_finance_source(row["source_kind"], row["source_id"])
        return self.connection().execute("SELECT * FROM finance_cases WHERE case_id=?", (case_id,)).fetchone()

    @staticmethod
    def _finance_version(row, version):
        if type(version) is not int or version != row["version"]:
            raise CartConflict("Сведения или задача изменились. Обнови карточку перед подтверждением.")

    def _finance_assigned(self, actor, row, owner_ids):
        if row["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner":
            raise PermissionError("Сначала возьми финансовый разбор в работу.")
        if row["state"] == "closed": raise ValueError("Задача уже закрыта. Новое исключение откроет её автоматически.")

    def claim_finance(self, actor, case_id, version, operation_id, *, release=False, owner_ids=frozenset()):
        if type(release) is not bool: raise ValueError("Некорректное действие с назначением.")
        with self.commerce_transaction() as conn:
            row = self._finance_case(actor, case_id, owner_ids, "finance.work")
            prior, fp = self._service_request(actor, operation_id, "finance_claim", [case_id, version, release])
            if prior is not None: return prior
            self._finance_version(row, version)
            if row["state"] == "closed": raise ValueError("Задача уже закрыта.")
            if release:
                if row["assigned_to"] != actor and self.staff_role(actor, owner_ids) != "owner":
                    raise PermissionError("Чужое назначение снимает владелец.")
                target = None
            else:
                if row["assigned_to"] not in {None, actor}: raise ValueError("Задачу уже взял другой сотрудник.")
                target = actor
            conn.execute("UPDATE finance_cases SET assigned_to=?,state=?,due_generation=due_generation+1,version=version+1,updated_at=? WHERE case_id=?",
                         (target, "open" if release else "working", now_iso(), case_id))
            self._finance_event(case_id, actor, "released" if release else "claimed", {"assigned_to": target})
            if release: self._finance_alert(case_id, "assignment", row["version"] + 1)
            self._service_done(actor, operation_id, "finance_claim", fp, case_id)
            return case_id

    def finance_step(self, actor, case_id, step, version, operation_id, *, owner_ids=frozenset()):
        if step not in FINANCE_STEPS: raise ValueError("Неизвестный этап разбора.")
        with self.commerce_transaction() as conn:
            row = self._finance_case(actor, case_id, owner_ids, "finance.work")
            prior, fp = self._service_request(actor, operation_id, "finance_step", [case_id, step, version])
            if prior is not None: return prior
            self._finance_version(row, version)
            self._finance_assigned(actor, row, owner_ids)
            if step == "closed" and row["active"]:
                raise ValueError("Исключение в финансовом источнике не устранено. Заметка или ручная кнопка не подтверждают деньги/возврат.")
            state = row["state"] if step == "checked" else step
            conn.execute("UPDATE finance_cases SET state=?,version=version+1,updated_at=? WHERE case_id=?", (state, now_iso(), case_id))
            self._finance_event(case_id, actor, "step", {"step": step, "state": state})
            if step == "escalated": self._finance_alert(case_id, "escalation", row["version"] + 1)
            self._service_done(actor, operation_id, "finance_step", fp, case_id)
            return case_id

    def schedule_finance(self, actor, case_id, hours, version, operation_id, *, owner_ids=frozenset()):
        if type(hours) is not int or hours not in {0, 1, 4, 24}:
            raise ValueError("Контроль можно назначить через 1, 4 или 24 часа либо явно снять срок.")
        with self.commerce_transaction() as conn:
            row = self._finance_case(actor, case_id, owner_ids, "finance.work")
            prior, fp = self._service_request(actor, operation_id, "finance_schedule", [case_id, hours, version])
            if prior is not None: return prior
            self._finance_version(row, version)
            self._finance_assigned(actor, row, owner_ids)
            due = time.time() + hours * 3600 if hours else None
            conn.execute("UPDATE finance_cases SET due_at=?,due_generation=due_generation+1,version=version+1,updated_at=? WHERE case_id=?", (due, now_iso(), case_id))
            self._finance_event(case_id, actor, "scheduled" if hours else "deadline_cleared", {"due_at": due, "escalate_at": due + 3600 if due else None})
            self._service_done(actor, operation_id, "finance_schedule", fp, case_id)
            return case_id

    def require_finance_note(self, actor, case_id, owner_ids=frozenset()):
        row = self._finance_case(actor, case_id, owner_ids, "finance.work")
        self._finance_assigned(actor, row, owner_ids)
        source = self._finance_source(row["source_kind"], row["source_id"])
        if not source["owner_id"] or not self.has_consent(source["owner_id"]):
            raise ValueError("Нет согласованной привязки к покупателю. Здесь доступны только структурированные этапы, без свободного текста и контактов.")
        return row

    def add_finance_note(self, actor, case_id, body, version, operation_id, *, owner_ids=frozenset()):
        body = clean_text(body, 800, minimum=3)
        with self.commerce_transaction() as conn:
            row = self._finance_case(actor, case_id, owner_ids, "finance.work")
            prior, fp = self._service_request(actor, operation_id, "finance_note", [case_id, body, version])
            if prior is not None: return prior
            self._finance_version(row, version)
            self.require_finance_note(actor, case_id, owner_ids)
            if conn.execute("SELECT COUNT(*) FROM finance_notes WHERE case_id=?", (case_id,)).fetchone()[0] >= 500:
                raise ValueError("В задаче уже 500 заметок. Продолжай структурированными этапами или согласуй архивирование с владельцем.")
            nid = conn.execute("INSERT INTO finance_notes(case_id,actor_id,body,created_at) VALUES (?,?,?,?)", (case_id, actor, body, now_iso())).lastrowid
            conn.execute("UPDATE finance_cases SET version=version+1,updated_at=? WHERE case_id=?", (now_iso(), case_id))
            self._finance_event(case_id, actor, "note", {"note_id": nid})
            self._service_done(actor, operation_id, "finance_note", fp, case_id)
            return case_id

    def release_finance_actor(self, actor_id, revoked_id):
        with self.commerce_transaction() as conn:
            rows = list(conn.execute("SELECT * FROM finance_cases WHERE assigned_to=? AND state<>'closed'", (revoked_id,)))
            for row in rows:
                conn.execute("UPDATE finance_cases SET assigned_to=NULL,due_generation=due_generation+1,version=version+1,updated_at=? WHERE case_id=?", (now_iso(), row["case_id"]))
                self._finance_event(row["case_id"], actor_id, "role_revoked", {"previous_assignee": revoked_id})
                self._finance_alert(row["case_id"], "assignment", row["version"] + 1)

    def finance_case_view(self, actor, case_id, *, owner_ids=frozenset(), page=0):
        with self.commerce_transaction() as conn:
            row = self._finance_case(actor, case_id, owner_ids)
            source = self._finance_source(row["source_kind"], row["source_id"])
            events = list(conn.execute("SELECT * FROM finance_journal WHERE case_id=? ORDER BY event_id DESC LIMIT 5 OFFSET ?", (case_id, max(0, min(10000, page)) * 4)))
            can_note = bool(source["owner_id"] and self.has_consent(source["owner_id"]))
            return {**dict(row), "state_label": FINANCE_STATES[row["state"]], "source": source,
                    "can_note": can_note, "can_close": not bool(row["active"]) and row["state"] != "closed",
                    "overdue": bool(row["due_at"] and row["due_at"] <= time.time() and row["state"] != "closed"),
                    "has_more": len(events) > 4,
                    "events": [{"event_id": x["event_id"], "actor_id": x["actor_id"], "kind": x["kind"], "data": _object(x["data"]), "created_at": x["created_at"]} for x in events[:4]]}

    def finance_note_view(self, actor, note_id, *, owner_ids=frozenset()):
        self.require_staff(actor, "finance.read", owner_ids)
        row = self.connection().execute("SELECT * FROM finance_notes WHERE note_id=?", (_local_id(note_id),)).fetchone()
        if not row: raise ValueError("Заметка не найдена.")
        return dict(row)

    def finance_cases(self, actor, bucket="attention", offset=0, *, owner_ids=frozenset()):
        self.require_staff(actor, "finance.read", owner_ids)
        self.sync_finance()
        filters = {"attention": "(active=1 OR state<>'closed')", "mine": "assigned_to=? AND state<>'closed'", "unassigned": "assigned_to IS NULL AND state<>'closed'",
                   "overdue": "due_at<=? AND state<>'closed'", "waiting": "state='waiting'", "closed": "state='closed'", "all": "1=1",
                   "receipt": "source_kind='receipt' AND state<>'closed'", "attempt": "source_kind='attempt' AND state<>'closed'", "inbox": "source_kind='inbox' AND state<>'closed'"}
        if bucket not in filters: raise ValueError("Неизвестная очередь финансов.")
        args = [actor] if bucket == "mine" else [time.time()] if bucket == "overdue" else []
        rows = list(self.connection().execute("SELECT case_id FROM finance_cases WHERE " + filters[bucket] + " ORDER BY (due_at IS NULL),due_at,case_id LIMIT 6 OFFSET ?", (*args, max(0, min(10000, offset)))))
        return [self.finance_case_view(actor, r[0], owner_ids=owner_ids) for r in rows]

    def finance_receipt(self, actor, receipt_id, *, owner_ids=frozenset()):
        self.require_staff(actor, "finance.read", owner_ids)
        _local_id(receipt_id)
        if not self.connection().execute("SELECT 1 FROM payment_receipts WHERE receipt_id=?", (receipt_id,)).fetchone(): raise ValueError("Поступление не найдено.")
        case_id = self.sync_finance_source("receipt", receipt_id)
        return {**self._finance_source("receipt", receipt_id), "case_id": case_id}

    def finance_receipts(self, actor, offset=0, *, owner_ids=frozenset(), purchase_id=None):
        self.require_staff(actor, "finance.read", owner_ids)
        params = []
        where = "1=1"
        if purchase_id is not None:
            p = self._service_purchase(purchase_id, actor, "finance.read", owner_ids)
            where = "payment_id=?"; params.append(p["payment_id"])
        rows = list(self.connection().execute("SELECT receipt_id FROM payment_receipts WHERE " + where + " ORDER BY receipt_id DESC LIMIT 6 OFFSET ?", (*params, max(0, min(10000, offset)))))
        return [self.finance_receipt(actor, r[0], owner_ids=owner_ids) for r in rows]

    def finance_summary(self, actor, *, owner_ids=frozenset()):
        self.require_staff(actor, "finance.read", owner_ids)
        self.sync_finance()
        conn = self.connection()
        groups = defaultdict(lambda: {"count": 0, "amount_minor": 0})
        imported = 0
        # Python integers avoid SQLite SUM overflow on large audit histories.
        for r in conn.execute("""SELECT r.method,r.currency,r.amount_minor,r.status,r.source,
            EXISTS(SELECT 1 FROM payment_receipt_conflicts c WHERE c.receipt_id=r.receipt_id) AS disputed FROM payment_receipts r"""):
            if r["source"] != "verified_event": imported += 1; continue
            bucket = "disputed" if r["disputed"] else r["status"]
            item = groups[(r["method"], r["currency"], bucket)]
            item["count"] += 1; item["amount_minor"] += r["amount_minor"]
        counts = conn.execute("""SELECT COUNT(*) AS total, COALESCE(SUM(active),0) AS active,
            COALESCE(SUM(state<>'closed' AND assigned_to IS NULL),0) AS unassigned,
            COALESCE(SUM(state<>'closed' AND due_at<=?),0) AS overdue FROM finance_cases""", (time.time(),)).fetchone()
        return {"cases": dict(counts), "paid_purchases": conn.execute("SELECT COUNT(*) FROM purchases u JOIN payments p ON p.payment_id=u.payment_id WHERE p.status='paid'").fetchone()[0],
                "legacy_records": imported, "executed_refunds": None,
                "totals": [{"method": k[0], "currency": k[1], "status": k[2], **v, "amount_label": finance_money(v["amount_minor"], k[1])} for k, v in sorted(groups.items())]}

    def queue_finance_alerts(self, owner_ids=frozenset(), manager_chat_id=None, *, stamp=None):
        now = time.time() if stamp is None else stamp
        with self.commerce_transaction() as conn:
            for r in list(conn.execute("""SELECT f.* FROM finance_cases f WHERE state<>'closed' AND due_at<=? AND
                (NOT EXISTS(SELECT 1 FROM finance_alerts a WHERE a.case_id=f.case_id AND a.kind='due' AND a.generation=f.due_generation) OR
                 (due_at+3600<=? AND NOT EXISTS(SELECT 1 FROM finance_alerts a WHERE a.case_id=f.case_id AND a.kind='overdue_owner' AND a.generation=f.due_generation)))
                ORDER BY due_at,case_id LIMIT 50""", (now, now))):
                self._finance_alert(r["case_id"], "due", r["due_generation"])
                if r["due_at"] + 3600 <= now: self._finance_alert(r["case_id"], "overdue_owner", r["due_generation"])
            for alert in list(conn.execute("SELECT * FROM finance_alerts WHERE queued_at IS NULL ORDER BY alert_id LIMIT 50")):
                case = conn.execute("SELECT * FROM finance_cases WHERE case_id=?", (alert["case_id"],)).fetchone()
                scheduled = alert["kind"] in {"due", "overdue_owner"}
                if (alert["kind"] == "escalation" and case["state"] != "escalated") or (scheduled and (case["state"] == "closed" or case["due_generation"] != alert["generation"] or not case["due_at"] or case["due_at"] > now)):
                    conn.execute("UPDATE finance_alerts SET queued_at=? WHERE alert_id=?", (now_iso(), alert["alert_id"]))
                    continue
                targets = self._finance_targets(case, alert["kind"], owner_ids, manager_chat_id)
                if not targets: continue
                for target in set(targets):
                    body = ("Наступил срок контроля" if alert["kind"] == "due" else "Нужен контроль владельца" if alert["kind"] in {"overdue_owner", "escalation"} else "Есть обновление финансового разбора")
                    self.enqueue_message(f"finance:{alert['alert_id']}:{target}", target,
                        f"{body}: задача F{case['case_id']:04d}. Подробности — /finance в личном чате. Это не подтверждение оплаты или выполненного возврата.",
                        {"inline_keyboard": [[{"text": "Открыть финансовую задачу", "callback_data": f"f:case:{case['case_id']}"}]]})
                conn.execute("UPDATE finance_alerts SET queued_at=? WHERE alert_id=?", (now_iso(), alert["alert_id"]))

    def _finance_targets(self, case, kind, owner_ids, manager_chat_id):
        targets = []
        if kind in {"overdue_owner", "escalation"}: targets = sorted(owner_ids)
        elif case["assigned_to"] and self.staff_role(case["assigned_to"], owner_ids) in {"owner", "finance"}: targets = [case["assigned_to"]]
        if not targets:
            targets = sorted(owner_ids) or [r[0] for r in self.connection().execute("SELECT user_id FROM staff_roles WHERE role='finance' ORDER BY user_id LIMIT 25")]
        return targets or ([manager_chat_id] if manager_chat_id else [])

    def finance_notification_current(self, notification_id, target, owner_ids=frozenset(), manager_chat_id=None, *, stamp=None):
        if not notification_id.startswith("finance:"): return True
        pieces = notification_id.split(":")
        if len(pieces) != 3 or not pieces[1].isdigit(): return False
        alert = self.connection().execute("SELECT * FROM finance_alerts WHERE alert_id=?", (int(pieces[1]),)).fetchone()
        if not alert: return False
        case = self.connection().execute("SELECT * FROM finance_cases WHERE case_id=?", (alert["case_id"],)).fetchone()
        if not case or target not in self._finance_targets(case, alert["kind"], owner_ids, manager_chat_id): return False
        if alert["kind"] == "escalation" and case["state"] != "escalated": return False
        if alert["kind"] in {"due", "overdue_owner"}:
            now = time.time() if stamp is None else stamp
            if case["state"] == "closed" or not case["due_at"] or case["due_generation"] != alert["generation"]: return False
            if now < case["due_at"] + (3600 if alert["kind"] == "overdue_owner" else 0): return False
            if alert["kind"] == "due" and case["assigned_to"] and target > 0 and target != case["assigned_to"]: return False
            if alert["kind"] == "overdue_owner" and owner_ids and target not in owner_ids: return False
        return True

    def customer_finance(self, user_id, purchase_id, page=0):
        purchase = self._service_purchase(purchase_id, user_id)
        payment = self.get_payment(purchase["payment_id"])
        # Stars must match the payer; external events require a saved invoice
        # binding. Never disclose orphan charges, other payers or conflict bodies.
        records = list(self.connection().execute("""SELECT r.* FROM payment_receipts r
            WHERE r.payment_id=? AND r.source='verified_event' AND
                ((r.method='stars' AND r.payer_id=?) OR (r.method IN ('lava','crypto') AND EXISTS(
                    SELECT 1 FROM payment_invoices i WHERE i.method=r.method AND i.invoice_id=r.provider_id AND i.payment_id=r.payment_id)))
            ORDER BY r.receipt_id DESC LIMIT 5 OFFSET ?""", (purchase["payment_id"], user_id, max(0, min(10000, page)) * 4)))
        receipts = []
        for r in records[:4]:
            conflict = bool(self.connection().execute("SELECT 1 FROM payment_receipt_conflicts WHERE receipt_id=? LIMIT 1", (r["receipt_id"],)).fetchone())
            receipts.append({"number": f"R{r['receipt_id']:04d}", "method": r["method"], "method_label": METHOD_NAMES.get(r["method"], r["method"]),
                "amount_label": finance_money(r["amount_minor"], r["currency"]), "currency": r["currency"],
                "status": "review_required" if conflict else r["status"], "status_label": "сведения проверяются" if conflict else STATUS_NAMES.get(r["status"], "нужна проверка"), "received_at": r["received_at"]})
        return {"purchase_id": purchase_id, "number": f"{purchase_id:04d}", "goods_amount_label": finance_money(payment["amount_rub"] * 100, "RUB"),
                "payment_status": payment["status"], "payment_status_label": STATUS_NAMES.get(payment["status"], payment["status"]),
                "attention": self.payment_attention(purchase["payment_id"]), "manual": payment["method"] == "manual",
                "has_legacy_records": bool(self.connection().execute("SELECT 1 FROM payment_receipts WHERE payment_id=? AND source='legacy' LIMIT 1", (purchase["payment_id"],)).fetchone()),
                "invoice_uncertain": bool(self.connection().execute("SELECT 1 FROM payment_invoice_attempts WHERE payment_id=? AND (status IN ('uncertain','legacy_unverified') OR (status='creating' AND created_at<=?)) LIMIT 1", (purchase["payment_id"], time.time() - 120)).fetchone()),
                "receipts": receipts, "has_more": len(records) > 4, "executed_refunds": None, "fiscal_document": None}
