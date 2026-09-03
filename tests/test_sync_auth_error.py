"""Проверка требования из плана шага 2: 401 у одного магазина не должен ронять
опрос остальных — магазин помечается invalid, пишется алерт, другие магазины
синкаются нормально. Сетевые вызовы WB замоканы — тестируем именно изоляцию
ошибок в sync_shop_all/sync_all_active_shops, а не сами HTTP-эндпоинты.
"""
from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet

from wbnotify import shops_repo
from wbnotify.db import utcnow
from wbnotify.security.tokens import encrypt_token
from wbnotify.sync import sync_all_active_shops
from wbnotify.wb_api.client import WBAuthError

ENC_KEY = Fernet.generate_key().decode()


def _insert_shop(conn, shop_id_hint: int, name: str) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO shops (owner_user_id, telegram_chat_id, name, wb_api_token_encrypted,
                            token_status, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', 1, ?, ?)
        """,
        (shop_id_hint, shop_id_hint, name, encrypt_token("dummy-token", ENC_KEY), now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_one_shop_401_does_not_break_others(conn, monkeypatch):
    bad_shop_id = _insert_shop(conn, 111, "Магазин с невалидным токеном")
    good_shop_id = _insert_shop(conn, 222, "Рабочий магазин")

    async def fake_orders(conn, shop_id, token):
        if shop_id == bad_shop_id:
            raise WBAuthError("401 Unauthorized (test)")
        return 3

    async def fake_zero(conn, shop_id, token):
        return 0

    monkeypatch.setattr("wbnotify.sync.sync_shop_orders", fake_orders)
    monkeypatch.setattr("wbnotify.sync.sync_shop_sales", fake_zero)
    monkeypatch.setattr("wbnotify.sync.sync_shop_stocks", fake_zero)
    monkeypatch.setattr("wbnotify.sync.sync_shop_cards", fake_zero)
    # sync_shop_analytics в пустой тестовой БД сам уходит в ноль (нет карточек),
    # а fbs_sync ходит в сеть всегда — мокаем, иначе тест ловит реальный 401.
    monkeypatch.setattr("wbnotify.sync.sync_shop_fbs", fake_zero)
    monkeypatch.setattr("wbnotify.sync.sync_shop_seller_stocks", fake_zero)
    monkeypatch.setattr("wbnotify.sync.sync_shop_funnel", fake_zero)

    results = asyncio.run(sync_all_active_shops(conn, ENC_KEY))
    by_shop = {r["shop_id"]: r for r in results}

    assert by_shop[bad_shop_id]["error"] == "invalid_token"
    assert by_shop[good_shop_id]["error"] is None
    assert by_shop[good_shop_id]["orders"] == 3

    bad_shop = shops_repo.get_shop(conn, bad_shop_id)
    good_shop = shops_repo.get_shop(conn, good_shop_id)
    assert bad_shop.token_status == "invalid"
    assert good_shop.token_status == "active"

    alerts = conn.execute(
        "SELECT * FROM shop_alerts WHERE shop_id = ? AND kind = 'reconnect_required'", (bad_shop_id,)
    ).fetchall()
    assert len(alerts) == 1

    # invalid магазин больше не попадает в list_active_shops -> не будет опрашиваться дальше
    active_ids = {s.id for s in shops_repo.list_active_shops(conn)}
    assert bad_shop_id not in active_ids
    assert good_shop_id in active_ids
