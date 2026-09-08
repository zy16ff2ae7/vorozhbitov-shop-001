"""Локальный просмотр витрины без токена Telegram.

Запуск из корня репозитория:
    python3 preview_server.py
"""
from __future__ import annotations

import time
from pathlib import Path

from bot import Catalog, Settings, start_health_server


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    catalog = Catalog(root / "catalog.json")
    settings = Settings(
        token="",
        admin_ids=frozenset(),
        channel_url="https://t.me/+XufFz8GGR0o3Njky",
        webapp_url="",
        manager_chat_id=None,
        brand_name="ВОРОЖБИТОВ",
        support_username="",
        database_path=root / "data/preview.sqlite3",
        catalog_path=root / "catalog.json",
        health_port=4173,
        giveaway_min_invites=3,
        privacy_url="",
    )
    server = start_health_server(4173, catalog, settings)
    print("Mini App preview: http://0.0.0.0:4173")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
