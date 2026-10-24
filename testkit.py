"""Общие заготовки для тестов: настройки собираются в одном месте.

Новые поля Settings больше не ломают десятки тестов: конструктор один,
дефолты здесь, индивидуальные значения приезжают overrides-ами.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bot import Settings

REPO_CATALOG = Path(__file__).with_name("catalog.json")

DEFAULTS: dict[str, Any] = {
    "token": "test-token",
    "admin_ids": frozenset({1}),
    "channel_url": "https://t.me/test",
    "webapp_url": "https://example.com",
    "manager_chat_id": None,
    "brand_name": "Test",
    "support_username": "",
    "giveaway_min_invites": 3,
    "privacy_url": "",
    "health_port": 0,
}


def make_settings(database_path: Path, catalog_path: Path | None = None,
                  **overrides: Any) -> Settings:
    """Settings с тестовыми дефолтами; всё особенное — через overrides."""
    kw = {**DEFAULTS, **overrides}
    kw["database_path"] = Path(database_path)
    kw["catalog_path"] = Path(catalog_path) if catalog_path else REPO_CATALOG
    return Settings(**kw)
