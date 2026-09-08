"""
Шаг 2 — CLI поверх shops_repo/sync, без Telegram.

Подкоманды:
  register  — регистрирует магазин (та же логика, что будет за /addshop).
              Токен передавайте через --token-file <путь>, а не --token в аргументах
              (аргументы командной строки видны в истории/логах процесса).
  sync      — запускает синхронизацию активных магазинов.

Примеры:
    python scripts/sync_cli.py register --owner-user-id 266632854 --chat-id 266632854 --token-file token.txt
    python scripts/sync_cli.py sync --all --once
    python scripts/sync_cli.py sync --shop-id 1 --once
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
from wbnotify.db import db_session, init_db
from wbnotify.logging_conf import setup_logging
from wbnotify.sync import sync_all_active_shops, sync_shop_all
from wbnotify.sync.tariffs_sync import sync_tariffs


def cmd_register(args: argparse.Namespace) -> int:
    config = load_config()
    setup_logging(config.log_level)
    init_db(config.db_path)

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("Файл с токеном пуст")
        return 1

    async def run() -> int:
        with db_session(config.db_path) as conn:
            try:
                shop = await shops_repo.register_shop(
                    conn, args.owner_user_id, args.chat_id, token, config.token_encryption_key
                )
            except shops_repo.InvalidTokenError as exc:
                print(f"Ошибка регистрации: {exc}")
                return 1
        print(f"Магазин зарегистрирован: id={shop.id}, name={shop.name!r}, brand_code={shop.brand_code!r}")
        return 0

    return asyncio.run(run())


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    setup_logging(config.log_level)
    init_db(config.db_path)

    async def run() -> int:
        with db_session(config.db_path) as conn:
            if args.all:
                print("Синхронизация всех активных магазинов (+ тарифы)...")
                results = await sync_all_active_shops(conn, config.token_encryption_key)
            else:
                shop = shops_repo.get_shop(conn, args.shop_id)
                token = shops_repo.get_decrypted_token(conn, shop.id, config.token_encryption_key)
                await sync_tariffs(conn, token)
                results = [await sync_shop_all(conn, shop, config.token_encryption_key)]

            if not results:
                print("Нет активных магазинов для синхронизации (зарегистрируйте через `register`)")
                return 0

        for r in results:
            if r["error"]:
                print(f"  - shop_id={r['shop_id']}: ОШИБКА — {r['error']}")
            else:
                print(
                    f"  - shop_id={r['shop_id']}: orders={r['orders']} sales={r['sales']} "
                    f"stocks={r['stocks']} cards={r['cards']} analytics={r['analytics']}"
                )
        return 0

    return asyncio.run(run())


def cmd_resync(args) -> int:
    """Глубокий перезабор заказов и продаж за последние N дней, минуя курсор.

    Нужен после того, как WB отдал события задним числом: обычный синк ходит от
    курсора с суточным перекрытием, а тут можно вычерпать хоть неделю. Дубликатов
    не создаёт — дедуп по UNIQUE(shop_id, srid); уже отправленные уведомления
    повторно не уйдут (у них проставлен notified_*_at).
    """
    from datetime import timedelta

    from wbnotify.counters import now_msk
    from wbnotify.sync.orders_sync import sync_shop_orders
    from wbnotify.sync.sales_sync import sync_shop_sales

    config = load_config()
    date_from = (now_msk() - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")

    async def run(conn, shop):
        token = shops_repo.get_decrypted_token(conn, shop.id, config.token_encryption_key)
        orders = await sync_shop_orders(conn, shop.id, token, date_from_override=date_from)
        sales = await sync_shop_sales(conn, shop.id, token, date_from_override=date_from)
        return orders, sales

    with db_session(config.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM orders WHERE shop_id = ?", (args.shop_id,)).fetchone()[0]
        shop = shops_repo.get_shop(conn, args.shop_id)
        orders, sales = asyncio.run(run(conn, shop))
        after = conn.execute("SELECT COUNT(*) FROM orders WHERE shop_id = ?", (args.shop_id,)).fetchone()[0]

    print(f"Кабинет «{shop.name}»: перезабор с {date_from}")
    print(f"  обработано строк: заказы {orders}, продажи {sales}")
    print(f"  НОВЫХ заказов добавлено: {after - before}")
    print("Уведомления по ним уйдут ближайшим циклом, если событию меньше 7 дней.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_register = sub.add_parser("register", help="Зарегистрировать магазин")
    p_register.add_argument("--owner-user-id", type=int, required=True)
    p_register.add_argument("--chat-id", type=int, required=True)
    p_register.add_argument("--token-file", required=True, help="Путь к файлу с сырым WB-токеном")
    p_register.set_defaults(func=cmd_register)

    p_sync = sub.add_parser("sync", help="Синхронизировать магазин(ы)")
    group = p_sync.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--shop-id", type=int)
    p_sync.add_argument("--once", action="store_true", default=True)
    p_sync.set_defaults(func=cmd_sync)

    p_resync = sub.add_parser(
        "resync", help="Перезабрать заказы и продажи за N дней, минуя курсор (WB отдал задним числом)"
    )
    p_resync.add_argument("--shop-id", type=int, required=True)
    p_resync.add_argument("--days", type=int, default=3)
    p_resync.set_defaults(func=cmd_resync)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
