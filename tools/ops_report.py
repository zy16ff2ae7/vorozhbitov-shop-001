#!/usr/bin/env python3
"""Read-only operational reports for payments and the purchase funnel."""
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',type=Path,required=True); ns=ap.parse_args()
    conn=sqlite3.connect(ns.db); conn.row_factory=sqlite3.Row
    payments={
      'pending': conn.execute("SELECT COUNT(*) c, COALESCE(SUM(amount_rub),0) amount FROM payments WHERE status='pending'").fetchone(),
      'paid': conn.execute("SELECT COUNT(*) c, COALESCE(SUM(amount_rub),0) amount FROM payments WHERE status='paid'").fetchone(),
      'refund_required': conn.execute("SELECT COUNT(*) c, COALESCE(SUM(amount_rub),0) amount FROM payments WHERE status='refund_required'").fetchone(),
      'orphan_orders': conn.execute("SELECT COUNT(*) c FROM orders o LEFT JOIN payments p ON p.payment_id=o.payment_id WHERE o.payment_id<>'' AND p.payment_id IS NULL").fetchone(),
      'amount_mismatches': conn.execute("SELECT COUNT(*) c FROM payments p LEFT JOIN orders o ON o.payment_id=p.payment_id GROUP BY p.payment_id HAVING p.amount_rub<>COALESCE(SUM(o.amount_rub),0)").fetchall(),
    }
    events=conn.execute("SELECT event, COUNT(*) c FROM events GROUP BY event ORDER BY c DESC").fetchall()
    funnel={row['event']:row['c'] for row in events}
    result={'payments':{k:dict(v) if isinstance(v,sqlite3.Row) else len(v) for k,v in payments.items()},'events':funnel}
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
