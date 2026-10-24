"""Network-isolated smoke checks for the built runtime image.

The CI job bind-mounts this file into the container, so production images do
not need to carry test tooling.  Only loopback HTTP and a temporary SQLite
database are used; Telegram and payment providers are never called.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

APP_ROOT = Path(os.getenv("APP_ROOT", str(Path.cwd()))).resolve()
sys.path.insert(0, str(APP_ROOT))

from bot import Catalog, Database, Settings, start_health_server


def read(path: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:18080{path}", timeout=5) as response:
        return response.status, response.read(), response.headers.get_content_type()


def read_json(path: str) -> dict:
    status, body, content_type = read(path)
    assert status == 200, (path, status)
    assert content_type == "application/json", (path, content_type)
    payload = json.loads(body)
    assert isinstance(payload, dict), path
    return payload


def main() -> None:
    catalog_path = Path(os.getenv("CATALOG_PATH", str(APP_ROOT / "catalog.json"))).resolve()
    with tempfile.TemporaryDirectory(prefix="shop-container-smoke-") as directory:
        settings = Settings(
            token="",
            admin_ids=frozenset(),
            channel_url="https://t.me/test",
            webapp_url="",
            manager_chat_id=None,
            brand_name="CI",
            support_username="",
            database_path=Path(directory) / "shop.sqlite3",
            catalog_path=catalog_path,
            health_port=18080,
            giveaway_min_invites=3,
            privacy_url="",
        )
        catalog = Catalog(catalog_path)
        db = Database(settings.database_path)
        server = start_health_server(18080, catalog, settings, db)
        try:
            health = read_json("/health")
            assert health["status"] == "ok", health
            assert health["checks"] == {"database": "ok", "catalog": "ok"}, health

            payload = read_json("/api/catalog")
            assert len(payload["products"]) >= 2, payload
            assert payload["gift"]["remaining"] == 100, payload["gift"]

            status, html, content_type = read("/")
            assert status == 200 and content_type == "text/html", (status, content_type)
            assert b'content="#0c0c0e"' in html
            status, css, _ = read("/styles.css")
            assert status == 200 and b"color-scheme: dark" in css

            emoji = read_json("/api/emoji-pack")
            assert len(emoji["icons"]) == 12, emoji
            status, icon, content_type = read("/assets/emoji/01_sword.png")
            assert status == 200 and content_type == "image/png", (status, content_type)
            assert icon.startswith(b"\x89PNG\r\n\x1a\n")

            print(json.dumps({
                "ok": True,
                "health": health["checks"],
                "products": len(payload["products"]),
                "gift_remaining": payload["gift"]["remaining"],
                "emoji_icons": len(emoji["icons"]),
                "dark_theme": True,
            }, ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
            db.close_current()


if __name__ == "__main__":
    main()
