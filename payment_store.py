"""Durable payment inbox, issued invoices and receipt ledger (SQLite, stdlib only).

Database supplies connection(), get_payment(), payment_is_payable(), event(),
enqueue_message() and _apply_payment_status(). All settlement callbacks enqueue
outbox records only: no network I/O inside a money transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

LOG = logging.getLogger("brand_bot.pay.store")
PROVIDERS = {"stars", "lava", "crypto"}
EXTERNAL_PROVIDERS = {"lava", "crypto"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaymentStore:
    def init_payment_store(self) -> None:
        conn = self.connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS payment_receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                payment_id TEXT NOT NULL,
                claimed_ref TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payer_id INTEGER,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'verified_event',
                received_at TEXT NOT NULL,
                UNIQUE(method, provider_id)
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_payment ON payment_receipts(payment_id);
            CREATE INDEX IF NOT EXISTS idx_receipts_exception ON payment_receipts(receipt_id) WHERE status<>'applied';
            CREATE INDEX IF NOT EXISTS idx_payments_exception ON payments(created_at) WHERE status IN ('review_required','refund_required');
            CREATE TABLE IF NOT EXISTS payment_receipt_conflicts (
                fingerprint TEXT PRIMARY KEY,
                receipt_id INTEGER NOT NULL REFERENCES payment_receipts(receipt_id),
                observed TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_conflicts_receipt ON payment_receipt_conflicts(receipt_id);
            CREATE TABLE IF NOT EXISTS payment_invoices (
                method TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                payment_id TEXT NOT NULL REFERENCES payments(payment_id),
                external_ref TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                currency TEXT NOT NULL,
                url TEXT NOT NULL,
                expires_at REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(method, invoice_id)
            );
            CREATE INDEX IF NOT EXISTS idx_invoices_payment ON payment_invoices(payment_id, method);
            CREATE TABLE IF NOT EXISTS payment_invoice_attempts (
                attempt_id TEXT PRIMARY KEY,
                payment_id TEXT NOT NULL REFERENCES payments(payment_id),
                method TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                legacy_url TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_unresolved
                ON payment_invoice_attempts(payment_id, method)
                WHERE status IN ('creating', 'uncertain', 'legacy_unverified');
            CREATE TABLE IF NOT EXISTS payment_inbox (
                update_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL,
                processed_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_payment_inbox_due ON payment_inbox(processed_at, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_payment_inbox_error ON payment_inbox(received_at) WHERE processed_at IS NULL AND last_error<>'';
        """)
        # An imported row is evidence of the old local state, NOT a verified
        # provider response. Do not replay historical fulfillment/notifications.
        conn.execute("""
            INSERT OR IGNORE INTO payment_receipts
                (method, provider_id, payment_id, claimed_ref, amount_minor,
                 currency, status, reason, source, received_at)
            SELECT method, provider_id, payment_id, payment_id,
                CASE WHEN method='stars' THEN amount_stars ELSE amount_rub*100 END,
                CASE WHEN method='stars' THEN 'XTR' ELSE 'RUB' END,
                'legacy_unreconciled', 'imported_legacy', 'legacy', COALESCE(paid_at, created_at)
            FROM payments WHERE method IN ('stars','lava','crypto') AND provider_id<>''
              AND status IN ('paid','refund_required')
        """)
        # Old versions could reuse/truncate provider IDs. Preserve ambiguous
        # associations as migration conflicts without manufacturing more money.
        for row in conn.execute("""SELECT p.*, r.receipt_id FROM payments p JOIN payment_receipts r
                ON p.method=r.method AND p.provider_id=r.provider_id
                WHERE p.payment_id<>r.payment_id AND p.provider_id<>''"""):
            observed = {"claimed_ref": row["payment_id"], "source": "legacy_collision"}
            fingerprint = hashlib.sha256(_json([row["method"], row["provider_id"], observed]).encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO payment_receipt_conflicts VALUES (?, ?, ?, ?)",
                         (fingerprint, row["receipt_id"], _json(observed), _now()))
        for row in conn.execute("SELECT * FROM payment_methods"):
            try:
                methods = json.loads(row["methods"])
            except (ValueError, TypeError):
                continue
            if not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict) or method.get("id") not in EXTERNAL_PROVIDERS:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM payment_invoices WHERE payment_id=? AND method=? AND url=?",
                    (row["payment_id"], method["id"], method.get("url", "")),
                ).fetchone()
                if not exists:
                    conn.execute("""INSERT OR IGNORE INTO payment_invoice_attempts
                        (attempt_id, payment_id, method, external_ref, status, legacy_url, created_at)
                        VALUES (?, ?, ?, ?, 'legacy_unverified', ?, ?)""",
                        (f"legacy:{row['payment_id']}:{method['id']}", row["payment_id"], method["id"],
                         row["payment_id"], method.get("url", ""), time.time()))

    def get_invoice(self, method: str, invoice_id: str) -> sqlite3.Row | None:
        return self.connection().execute(
            "SELECT * FROM payment_invoices WHERE method=? AND invoice_id=?", (method, invoice_id),
        ).fetchone()

    def invoice_is_usable(self, payment_id: str, method: str, url: str, now: float) -> bool:
        return bool(self.connection().execute("""SELECT 1 FROM payment_invoices
            WHERE payment_id=? AND method=? AND url=? AND (expires_at IS NULL OR expires_at>?)""",
            (payment_id, method, url, now + 60)).fetchone())

    def reserve_invoice(self, payment_id: str, method: str, amount_minor: int) -> dict[str, Any] | None:
        """Reuse a known invoice or durably reserve a creation attempt before I/O.

        Unknown outcomes/legacy URLs are never blindly reissued. Reconciliation
        must close the attempt first (see OPERATIONS.md).
        """
        if method not in EXTERNAL_PROVIDERS:
            raise ValueError("Unsupported invoice provider")
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            payment = self.get_payment(payment_id)
            if not payment or not self.payment_is_payable(payment_id) or payment["amount_rub"] * 100 != amount_minor:
                conn.execute("COMMIT")
                return None
            now = time.time()
            cached = conn.execute("""SELECT * FROM payment_invoices WHERE payment_id=? AND method=?
                AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1""",
                (payment_id, method, now + 60)).fetchone()
            if cached:
                conn.execute("COMMIT")
                return {"cached": dict(cached)}
            unresolved = conn.execute("""SELECT 1 FROM payment_invoice_attempts WHERE payment_id=? AND method=?
                AND status IN ('creating','uncertain','legacy_unverified')""", (payment_id, method)).fetchone()
            if unresolved:
                conn.execute("COMMIT")
                return None
            attempt_id = secrets.token_hex(16)
            # Lava orderId is unique per creation, unlike Crypto's opaque payload.
            external_ref = attempt_id if method == "lava" else payment_id
            conn.execute("""INSERT INTO payment_invoice_attempts
                (attempt_id, payment_id, method, external_ref, status, created_at)
                VALUES (?, ?, ?, ?, 'creating', ?)""", (attempt_id, payment_id, method, external_ref, now))
            conn.execute("COMMIT")
            return {"attempt_id": attempt_id, "external_ref": external_ref, "created_at": now}
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def save_invoice(self, payment_id: str, method: str, invoice_id: str, amount_minor: int,
                     currency: str, url: str, *, external_ref: str | None = None,
                     expires_at: float | None = None, attempt_id: str | None = None) -> None:
        if method not in EXTERNAL_PROVIDERS or not isinstance(invoice_id, str) or not invoice_id.strip() or len(invoice_id) > 512:
            raise ValueError("Invalid invoice identity")
        payment = self.get_payment(payment_id)
        if not payment or amount_minor != payment["amount_rub"] * 100 or currency != "RUB" or not url.startswith("https://"):
            raise ValueError("Invalid issued invoice")
        conn = self.connection()
        owns = not conn.in_transaction
        try:
            if owns:
                conn.execute("BEGIN IMMEDIATE")
            if attempt_id:
                attempt = conn.execute("SELECT * FROM payment_invoice_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
                if not attempt or (attempt["payment_id"], attempt["method"], attempt["external_ref"]) != (payment_id, method, external_ref or payment_id):
                    raise ValueError("Invoice creation attempt does not match")
            existing = self.get_invoice(method, invoice_id)
            values = (payment_id, external_ref or payment_id, amount_minor, currency, url)
            if existing:
                if tuple(existing[key] for key in ("payment_id", "external_ref", "amount_minor", "currency", "url")) != values:
                    raise ValueError("Invoice is already bound to another payment")
            else:
                conn.execute("""INSERT INTO payment_invoices
                    (method, invoice_id, payment_id, external_ref, amount_minor, currency, url, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (method, invoice_id, *values, expires_at, _now()))
            if attempt_id:
                conn.execute("UPDATE payment_invoice_attempts SET status='issued' WHERE attempt_id=?", (attempt_id,))
                self.sync_finance_source("attempt", attempt_id)
            if owns:
                conn.execute("COMMIT")
        except Exception:
            if owns and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def mark_payment_paid(self, payment_id: str, method: str = "manual", provider_id: str = "", *,
                          amount_minor: int | None = None, currency: str = "", payer_id: int | None = None,
                          queue_notifications: Callable[[str], None] | None = None,
                          queue_review: Callable[[dict[str, Any]], None] | None = None) -> bool:
        """Record each distinct charge; True means a new receipt, not fulfillment.

        Incoming adapters must supply actual amount/currency and, for Stars,
        the payer. Only an explicit admin/manual transition bypasses the ledger.
        Unknown invoices and mismatches are durable review cases, not paid orders.
        """
        if method == "manual":
            return self._apply_payment_status(payment_id, method, "", queue_notifications=queue_notifications)
        if method not in PROVIDERS or not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 512:
            raise ValueError("Invalid payment identity")
        if type(amount_minor) is not int or not 0 < amount_minor <= 9_000_000_000_000_000:
            raise ValueError("Invalid payment amount")
        if not isinstance(currency, str) or not currency.isascii() or not currency.isalpha() or not 3 <= len(currency) <= 10:
            raise ValueError("Invalid payment currency")
        if not isinstance(payment_id, str) or len(payment_id) > 256:
            raise ValueError("Invalid payment reference")
        conn = self.connection()
        owns = not conn.in_transaction
        try:
            if owns:
                conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM payment_receipts WHERE method=? AND provider_id=?",
                                    (method, provider_id)).fetchone()
            observed = {"claimed_ref": payment_id, "amount_minor": amount_minor, "currency": currency, "payer_id": payer_id}
            if existing:
                # An id reused with different fields cannot silently pay another order.
                conflict = any(existing[key] != value for key, value in observed.items()
                               if not (key == "payer_id" and existing["source"] == "legacy"))
                if conflict:
                    fingerprint = hashlib.sha256(_json([method, provider_id, observed]).encode()).hexdigest()
                    inserted = conn.execute("INSERT OR IGNORE INTO payment_receipt_conflicts VALUES (?, ?, ?, ?)",
                                            (fingerprint, existing["receipt_id"], _json(observed), _now())).rowcount
                    if inserted:
                        self.sync_finance_source("receipt", existing["receipt_id"])
                    if inserted and queue_review:
                        queue_review({**dict(existing), "reason": "conflicting_duplicate", "status": "review_required",
                                      "alert_key": f"conflict:{fingerprint}", "observed": observed})
                if owns:
                    conn.execute("COMMIT")
                return False

            invoice = self.get_invoice(method, provider_id) if method in EXTERNAL_PROVIDERS else None
            canonical_id = invoice["payment_id"] if invoice else payment_id
            payment = self.get_payment(canonical_id)
            reasons: list[str] = []
            if not payment:
                reasons.append("unknown_payment")
            if method in EXTERNAL_PROVIDERS:
                if not invoice:
                    reasons.append("unknown_invoice")
                elif invoice["external_ref"] != payment_id:
                    reasons.append("invoice_reference_mismatch")
            if payment:
                expected_amount = payment["amount_stars"] if method == "stars" else payment["amount_rub"] * 100
                expected_currency = "XTR" if method == "stars" else "RUB"
                if amount_minor != expected_amount or (invoice and amount_minor != invoice["amount_minor"]):
                    reasons.append("amount_mismatch")
                if currency != expected_currency or (invoice and currency != invoice["currency"]):
                    reasons.append("currency_mismatch")
                if method == "stars" and (type(payer_id) is not int or payer_id != payment["user_id"]):
                    reasons.append("payer_mismatch")
            apply = False
            if reasons:
                status, reason = "review_required", ",".join(reasons)
            elif payment["status"] == "review_required":
                status, reason = "review_required", "awaiting_review"
            elif payment["status"] == "paid":
                if payment["method"] == "manual":
                    status, reason = "review_required", "manual_settlement"
                elif not conn.execute("""SELECT 1 FROM payment_receipts WHERE payment_id=?
                        AND source='verified_event' AND status='applied' LIMIT 1""", (canonical_id,)).fetchone():
                    status, reason = "review_required", "legacy_settlement"
                else:
                    status, reason = "refund_required", "extra_payment"
            elif payment["status"] == "refund_required":
                status, reason = "refund_required", "extra_payment"
            elif not self.payment_is_payable(canonical_id):
                status, reason, apply = "refund_required", "late_payment", True
            else:
                status, reason, apply = "applied", "", True
            cursor = conn.execute("""INSERT INTO payment_receipts
                (method, provider_id, payment_id, claimed_ref, amount_minor, currency, payer_id, status, reason, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (method, provider_id, canonical_id, payment_id, amount_minor, currency, payer_id, status, reason, _now()))
            receipt = dict(conn.execute("SELECT * FROM payment_receipts WHERE receipt_id=?", (cursor.lastrowid,)).fetchone())
            if apply:
                self._apply_payment_status(canonical_id, method, provider_id, queue_notifications=queue_notifications)
            elif payment and payment["status"] == "pending":
                conn.execute("UPDATE payments SET status='review_required' WHERE payment_id=?", (canonical_id,))
            if payment:
                conn.execute("DELETE FROM payment_methods WHERE payment_id=?", (canonical_id,))
            self.event(payment["user_id"] if payment else None, "payment_receipt",
                       {"receipt_id": receipt["receipt_id"], "payment_id": canonical_id, "status": status, "reason": reason})
            self.sync_finance_source("receipt", receipt["receipt_id"])
            if payment:
                self.sync_finance_payment(canonical_id)
            if status != "applied" and not apply and queue_review:
                # Per-receipt keys, unlike the aggregate paid notification.
                queue_review(receipt)
            if owns:
                conn.execute("COMMIT")
            return True
        except Exception:
            if owns and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def payment_attention(self, payment_id: str) -> str:
        payment = self.get_payment(payment_id)
        if payment and payment["status"] == "refund_required":
            return "требуется возврат"
        if payment and payment["status"] == "review_required":
            return "поступление на сверке, не оплачивай повторно"
        issue = self.connection().execute("""SELECT 1 FROM payment_receipts WHERE payment_id=?
            AND status IN ('review_required','refund_required') LIMIT 1""", (payment_id,)).fetchone()
        if issue:
            return "дополнительное поступление на сверке"
        conflict = self.connection().execute("""SELECT 1 FROM payment_receipt_conflicts c
            JOIN payment_receipts r ON r.receipt_id=c.receipt_id WHERE r.payment_id=? LIMIT 1""", (payment_id,)).fetchone()
        return "данные поступления расходятся, требуется сверка" if conflict else ""

    def enqueue_payment_update(self, update: dict[str, Any]) -> None:
        if self.connection().in_transaction:
            raise RuntimeError("Inbox acknowledgement requires its own durable commit")
        message = update["message"]
        info = message["successful_payment"]
        # No complete message, profile, phone, contact, or order_info in this inbox.
        fields = ("invoice_payload", "telegram_payment_charge_id", "provider_payment_charge_id", "currency", "total_amount")
        payload = {key: info[key] for key in fields if key in info}
        self.connection().execute("""INSERT OR IGNORE INTO payment_inbox
            (update_id, user_id, chat_id, payload, received_at) VALUES (?, ?, ?, ?, ?)""",
            (int(update["update_id"]), int(message["from"]["id"]), int(message["chat"]["id"]), _json(payload), _now()))

    def claim_payment_updates(self, now: float) -> list[sqlite3.Row]:
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute("""SELECT * FROM payment_inbox
                WHERE processed_at IS NULL AND next_attempt_at<=? ORDER BY next_attempt_at, update_id LIMIT 1""", (now,)))
            for row in rows:
                conn.execute("UPDATE payment_inbox SET attempts=attempts+1, next_attempt_at=? WHERE update_id=?",
                             (now + 60, row["update_id"]))
            conn.execute("COMMIT")
            return rows
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
