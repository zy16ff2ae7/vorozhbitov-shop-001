"""Synthetic stock fixtures for offline tests/demo ONLY. Never imported by runtime."""


def seed_test_inventory(db, catalog, quantity=1000):
    db.sync_inventory(catalog)
    # Deliberately explicit fixture quantities, not labels from the real catalog.
    # Direct fixture setup avoids creating fake production stock-count audit rows.
    db.connection().execute("UPDATE stock_items SET on_hand=?,reserved=0,version=0", (quantity,))
