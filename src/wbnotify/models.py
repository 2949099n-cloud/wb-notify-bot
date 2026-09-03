"""Датаклассы для строк БД. Сырой (расшифрованный) WB-токен НИКОГДА не попадает
в ShopRow — он достаётся отдельно через shops_repo.get_decrypted_token() только
на момент конкретного вызова WB API.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ShopRow:
    id: int
    owner_user_id: int
    telegram_chat_id: int
    name: str
    brand_code: str | None
    token_status: str
    subscription_status: str
    subscription_expires_at: str | None
    is_active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ShopRow":
        return cls(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            telegram_chat_id=row["telegram_chat_id"],
            name=row["name"],
            brand_code=row["brand_code"],
            token_status=row["token_status"],
            subscription_status=row["subscription_status"],
            subscription_expires_at=row["subscription_expires_at"],
            is_active=bool(row["is_active"]),
        )
