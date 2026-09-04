"""Разбор WB-токена (JWT) без проверки подписи — для экрана «Токены WB».

Токен наш собственный, подпись проверять нечем и незачем: это диагностика
(тип, срок, категории доступа), а не авторизация. Настоящий статус токена
(«действителен / отозван») знает только сервер WB, поэтому экран показывает
ещё и `token_status` из БД, который выставляется по реальным 401.

Таблица бит сверена с официальной документацией WB (раздел «Авторизация» →
«Поле s»), не с памятью: https://dev.wildberries.ru/ru/docs/openapi/api-information
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Позиция бита в маске `s` -> название категории доступа. Биты 8, 14, 15 и
# остальные в документации не определены — намеренно отсутствуют здесь.
SCOPE_BITS = {
    1: "Контент",
    2: "Аналитика",
    3: "Цены и скидки",
    4: "Маркетплейс",
    5: "Статистика",
    6: "Продвижение",
    7: "Вопросы и отзывы",
    9: "Чат с покупателями",
    10: "Поставки",
    11: "Возвраты покупателями",
    12: "Документы",
    13: "Финансы",
    16: "Пользователи",
}
READONLY_BIT = 30

TOKEN_TYPES = {1: "Базовый", 2: "Тестовый", 3: "Персональный", 4: "Сервисный"}


@dataclass(frozen=True)
class TokenInfo:
    tail: str                      # последние 4 символа — чтобы отличать токены, не показывая сам токен
    token_type: str
    is_readonly: bool
    is_test: bool
    expires_at: datetime | None
    scopes: list[str]

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < datetime.now(timezone.utc)


def _decode_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # base64url в JWT идёт без выравнивающих '='
    return json.loads(base64.urlsafe_b64decode(payload))


def parse_token(token: str) -> TokenInfo | None:
    """None, если токен не разбирается как JWT — экран тогда покажет только то,
    что знает БД. Ронять меню из-за нестандартного токена нельзя."""
    try:
        data = _decode_payload(token)
    except Exception as exc:  # noqa: BLE001 — любой мусор в токене не должен ломать меню
        logger.warning("Не удалось разобрать WB-токен как JWT: %s", exc)
        return None

    mask = data.get("s") or 0
    exp = data.get("exp")
    return TokenInfo(
        tail=token[-4:],
        token_type=TOKEN_TYPES.get(data.get("acc"), "Неизвестный"),
        is_readonly=bool(mask >> READONLY_BIT & 1),
        is_test=bool(data.get("t")),
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None,
        scopes=[name for bit, name in SCOPE_BITS.items() if mask >> bit & 1],
    )
