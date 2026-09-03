"""
Шаг 4 — CLI для отправки уведомлений из notification_queue в Telegram.

Подкоманды:
  drain  — отправляет pending-уведомления магазина. --limit ОБЯЗАТЕЛЕН и не может
           быть больше 50 за один запуск — защита от случайного залпа тысяч
           сообщений в реальный чат пользователя (в очереди могут скопиться
           десятки тысяч событий после большого бэкфилла на шаге 2/3).

Пример:
    python scripts/notify_cli.py drain --shop-id 1 --limit 3
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from wbnotify import shops_repo
from wbnotify.config import ConfigError, load_config
from wbnotify.db import db_session, utcnow
from wbnotify.logging_conf import setup_logging
from wbnotify.telegram.sender import drain_queue_for_shop, make_bot

MAX_LIMIT = 50


def cmd_drain(args: argparse.Namespace) -> int:
    if args.limit > MAX_LIMIT:
        print(f"--limit не может быть больше {MAX_LIMIT} за один запуск (защита от залпа в реальный чат)")
        return 1

    config = load_config()
    setup_logging(config.log_level)
    bot = make_bot(config.telegram_bot_token)

    async def run() -> int:
        with db_session(config.db_path) as conn:
            shop = shops_repo.get_shop(conn, args.shop_id)
            sent = await drain_queue_for_shop(conn, bot, shop, limit=args.limit)
        print(f"Отправлено: {sent} (лимит запроса: {args.limit})")
        return 0

    return asyncio.run(run())


def cmd_start_notifying(args: argparse.Namespace) -> int:
    config = load_config()
    with db_session(config.db_path) as conn:
        shop = shops_repo.get_shop(conn, args.shop_id)
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM notification_queue WHERE shop_id=? AND status='pending'", (shop.id,)
        ).fetchone()[0]
        now = utcnow()
        conn.execute("UPDATE shops SET notify_from=?, updated_at=? WHERE id=?", (now, now, shop.id))
        conn.commit()
    print(f"Магазин «{shop.name}»: рассылка включена с {now}")
    print(f"В очереди осталось {pending_before} исторических событий — они НЕ будут разосланы")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_drain = sub.add_parser("drain", help="Отправить pending-уведомления магазина")
    p_drain.add_argument("--shop-id", type=int, required=True)
    p_drain.add_argument("--limit", type=int, required=True, help=f"Макс. {MAX_LIMIT} за раз")
    p_drain.set_defaults(func=cmd_drain)

    p_start = sub.add_parser(
        "start-notifying",
        help="Включить рассылку: уведомлять только о событиях ПОСЛЕ этого момента "
             "(накопленный бэкфилл останется в очереди, но разослан не будет)",
    )
    p_start.add_argument("--shop-id", type=int, required=True)
    p_start.set_defaults(func=cmd_start_notifying)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
