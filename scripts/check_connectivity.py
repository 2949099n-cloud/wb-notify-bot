"""
Шаг 1 — smoke-test подключения (мультитенантная модель).

WB-токены магазинов больше не проверяются здесь — их пока просто нет в БД
до первого /addshop (или до scripts/sync_cli.py register для теста).

Проверяет:
  - TOKEN_ENCRYPTION_KEY — валидный Fernet-ключ
  - БД инициализируется
  - Telegram: getMe + (если задан TELEGRAM_ADMIN_CHAT_ID) тестовое sendMessage

Запуск:
    python scripts/check_connectivity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from cryptography.fernet import Fernet

from wbnotify.config import ConfigError, load_config
from wbnotify.db import init_db

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def check_encryption_key(key: str) -> bool:
    print("  [Config] TOKEN_ENCRYPTION_KEY ... ", end="", flush=True)
    try:
        Fernet(key.encode())
        print("OK — валидный Fernet-ключ")
        return True
    except Exception as exc:  # noqa: BLE001 — любой сбой парсинга ключа
        print(f"FAIL — {exc}")
        print("  Сгенерируйте новый: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        return False


def check_telegram(bot_token: str, admin_chat_id: int | None) -> None:
    print("  [Telegram] getMe ... ", end="", flush=True)
    try:
        resp = httpx.get(TELEGRAM_API.format(token=bot_token, method="getMe"), timeout=15)
        data = resp.json()
        if data.get("ok"):
            username = data["result"].get("username")
            print(f"OK (@{username})")
        else:
            print(f"FAIL — {data}")
            return
    except httpx.HTTPError as exc:
        print(f"FAIL — {exc}")
        return

    if admin_chat_id is None:
        print("  [Telegram] sendMessage ... пропущено (TELEGRAM_ADMIN_CHAT_ID не задан)")
        return

    print(f"  [Telegram] sendMessage -> chat_id={admin_chat_id} ... ", end="", flush=True)
    try:
        resp = httpx.post(
            TELEGRAM_API.format(token=bot_token, method="sendMessage"),
            json={"chat_id": admin_chat_id, "text": "wb-notify-bot: проверка подключения OK"},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            print("OK — сообщение отправлено")
        else:
            print(f"FAIL — {data}")
    except httpx.HTTPError as exc:
        print(f"FAIL — {exc}")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        print("Скопируйте .env.example в .env и заполните значения.")
        return 1

    print("[DB] init_db ... ", end="", flush=True)
    init_db(config.db_path)
    print(f"OK ({config.db_path})")

    ok = check_encryption_key(config.token_encryption_key)

    print("\nПроверка Telegram:")
    check_telegram(config.telegram_bot_token, config.telegram_admin_chat_id)

    print(
        "\nМагазины подключаются пользователями через /addshop (или "
        "scripts/sync_cli.py register для ручного теста) — здесь не проверяются."
    )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
