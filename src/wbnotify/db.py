"""SQLite-подключение и схема БД. Мультитенантность: shop_id — FK почти везде."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- AUTOINCREMENT: sqlite никогда не переиспользует id, даже после DELETE
  owner_user_id INTEGER NOT NULL,        -- Telegram user_id того, кто подключил магазин через /addshop
  telegram_chat_id INTEGER NOT NULL,     -- куда слать уведомления по этому магазину
  name TEXT NOT NULL,
  brand_code TEXT,                       -- nullable, может быть заполнено позже
  wb_api_token_encrypted BLOB NOT NULL,  -- Fernet ciphertext, см. security/tokens.py
  -- Уведомления шлются только по событиям, зарегистрированным ПОСЛЕ этой метки.
  -- Нужна, чтобы первичный бэкфилл (десятки тысяч исторических заказов) не улетел
  -- пользователю в чат при первом же запуске планировщика. NULL = не уведомлять
  -- ни о чём, пока метка не выставлена (безопасное значение по умолчанию).
  notify_from TEXT,
  token_status TEXT NOT NULL DEFAULT 'active' CHECK(token_status IN ('active','invalid')),
  token_checked_at TEXT,
  -- Оплата/подписка — отдельная ось от token_status (токен может быть валиден,
  -- но доступ приостановлен из-за неоплаты) и от is_active (ручное отключение
  -- владельцем). 'expires_at IS NULL' = бессрочно (пока нет модели оплаты).
  subscription_status TEXT NOT NULL DEFAULT 'active' CHECK(subscription_status IN ('active','expired')),
  subscription_expires_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shops_owner ON shops(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_shops_active ON shops(is_active, token_status, subscription_status);

CREATE TABLE IF NOT EXISTS shop_alerts (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  kind TEXT NOT NULL CHECK(kind IN ('reconnect_required')),
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_users (
  telegram_user_id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,            -- по умолчанию имя из Telegram, можно переименовать в «Профиле»
  chat_id INTEGER,                       -- личный чат пользователя с ботом
  created_at TEXT NOT NULL
);

-- Доступ к кабинету: владелец (тот, кто подключил) и приглашённые менеджеры.
-- Строка владельца заводится автоматически при регистрации магазина, см. _backfill_owners.
CREATE TABLE IF NOT EXISTS shop_members (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  telegram_user_id INTEGER NOT NULL,
  telegram_chat_id INTEGER NOT NULL,     -- куда слать уведомления ИМЕННО этому участнику
  role TEXT NOT NULL CHECK(role IN ('owner','manager')),
  added_at TEXT NOT NULL,
  UNIQUE(shop_id, telegram_user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON shop_members(telegram_user_id);

-- Одноразовые приглашения в кабинет. Ссылка вида t.me/<bot>?start=inv_<token>.
CREATE TABLE IF NOT EXISTS shop_invites (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  token TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('manager')),
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  used_by INTEGER
);

-- Служебные уведомления владельцу бота: подключения, сбои, отток. Копятся в
-- БД и рассылаются планировщиком (см. admin_alerts.py).
CREATE TABLE IF NOT EXISTS admin_alerts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  shop_id INTEGER,                       -- может быть NULL: удаление аккаунта не привязано к кабинету
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_alerts_unsent ON admin_alerts(sent_at);

-- Обращения в поддержку: связь «сообщение в чате админа» -> «кто его написал»,
-- чтобы ответ реплаем ушёл обратно нужному человеку.
CREATE TABLE IF NOT EXISTS support_threads (
  admin_message_id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_cursors (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  endpoint TEXT NOT NULL CHECK(endpoint IN ('orders','sales','stocks','cards','tariffs')),
  last_value TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, endpoint)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  srid TEXT NOT NULL,
  g_number TEXT,
  date TEXT NOT NULL,
  last_change_date TEXT NOT NULL,
  nm_id INTEGER NOT NULL,
  chrt_id INTEGER,
  tech_size TEXT,
  barcode TEXT,
  supplier_article TEXT,               -- артикул продавца (WB: supplierArticle) — то, что реально
                                        -- показывают в поле "🆔" пользователю, НЕ chrt_id
  subject TEXT,                        -- категория товара (WB: subject) — ключ для комиссии по тарифам
  warehouse_type TEXT,                 -- WB: warehouseType ("Склад WB" = FBW/kgvpSupplier,
                                        -- иначе FBS/DBS = kgvpMarketplace) — для выбора модели комиссии
  warehouse_name TEXT,
  region_name TEXT,
  oblast_okrug_name TEXT,
  total_price REAL,
  discount_percent REAL,
  spp REAL,
  price_with_disc REAL,
  finished_price REAL,
  is_cancel INTEGER NOT NULL DEFAULT 0,
  cancel_date TEXT,
  notified_order_at TEXT,
  notified_cancel_at TEXT,
  raw_json TEXT NOT NULL,
  UNIQUE(shop_id, srid)
);
CREATE INDEX IF NOT EXISTS idx_orders_shop_change ON orders(shop_id, last_change_date);
CREATE INDEX IF NOT EXISTS idx_orders_shop_nm ON orders(shop_id, nm_id, tech_size);

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  sale_id TEXT NOT NULL,               -- WB saleID: "S..." продажа, "R..." возврат
  srid TEXT,
  is_return INTEGER NOT NULL,
  date TEXT NOT NULL,
  last_change_date TEXT NOT NULL,
  nm_id INTEGER NOT NULL,
  chrt_id INTEGER,
  tech_size TEXT,
  barcode TEXT,
  supplier_article TEXT,               -- см. orders.supplier_article
  subject TEXT,                        -- см. orders.subject
  warehouse_type TEXT,                 -- см. orders.warehouse_type
  warehouse_name TEXT,
  region_name TEXT,
  oblast_okrug_name TEXT,
  price_with_disc REAL,
  finished_price REAL,
  for_pay REAL,
  spp REAL,
  notified_at TEXT,
  raw_json TEXT NOT NULL,
  UNIQUE(shop_id, sale_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_shop_change ON sales(shop_id, last_change_date);
CREATE INDEX IF NOT EXISTS idx_sales_shop_nm ON sales(shop_id, nm_id, tech_size);
CREATE INDEX IF NOT EXISTS idx_sales_srid ON sales(shop_id, srid);

-- Реальные поля POST /api/analytics/v1/stocks-report/wb-warehouses (проверено вживую):
-- nmId, chrtId, warehouseId, warehouseName, regionName, quantity, inWayToClient, inWayFromClient.
-- techSize/barcode этот эндпоинт НЕ возвращает — они достаются через join с cards_cache.sizes_json
-- по chrt_id (см. shops_repo/wb_api/content.py: sizes[].chrtID/techSize/skus).
CREATE TABLE IF NOT EXISTS stocks_current (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  nm_id INTEGER NOT NULL,
  chrt_id INTEGER NOT NULL,
  warehouse_id INTEGER NOT NULL,
  warehouse_name TEXT,
  region_name TEXT,
  quantity INTEGER NOT NULL,
  in_way_to_client INTEGER NOT NULL DEFAULT 0,
  in_way_from_client INTEGER NOT NULL DEFAULT 0,
  -- 'wb' — склады WB (stocks-report, Analytics API), 'seller' — свои склады
  -- продавца (FBS, Marketplace API). Два независимых источника в одной таблице,
  -- поэтому каждый синк заменяет снимок ТОЛЬКО своего вида складов.
  warehouse_kind TEXT NOT NULL DEFAULT 'wb' CHECK(warehouse_kind IN ('wb','seller')),
  snapshot_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, nm_id, chrt_id, warehouse_id)
);

-- sizes_json: JSON-массив [{"chrtID":.., "techSize":.., "wbSize":.., "skus":[barcode,...]}, ...]
-- из content/v2/get/cards/list — единственное место, где связаны chrtId <-> techSize <-> barcode.
CREATE TABLE IF NOT EXISTS cards_cache (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  nm_id INTEGER NOT NULL,
  brand TEXT,
  name TEXT,
  vendor_code TEXT,
  photo_url TEXT,                      -- первое фото (для обратной совместимости/фоллбека)
  photos_json TEXT,                    -- JSON-массив URL первых N фото — для карусели (media group)
  dims_l REAL,
  dims_w REAL,
  dims_h REAL,
  volume_l REAL,
  sizes_json TEXT,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, nm_id)
);

CREATE TABLE IF NOT EXISTS card_analytics_cache (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  nm_id INTEGER NOT NULL,
  rating REAL,
  reviews_count INTEGER,
  wb_buyout_percent REAL,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, nm_id)
);

CREATE TABLE IF NOT EXISTS tariffs_cache (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('commission','box')),
  subject_or_category TEXT,
  warehouse_name TEXT,
  value_json TEXT NOT NULL,
  refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  event_type TEXT NOT NULL CHECK(event_type IN ('order','cancel','buyout','return')),
  date_msk TEXT NOT NULL,
  last_seq INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (shop_id, event_type, date_msk)
);

CREATE TABLE IF NOT EXISTS notification_queue (
  id INTEGER PRIMARY KEY,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  event_type TEXT NOT NULL CHECK(event_type IN ('order','cancel','buyout','return')),
  ref_table TEXT NOT NULL,
  ref_id INTEGER NOT NULL,
  daily_seq INTEGER NOT NULL,
  -- Дата/время САМОГО события (order.date / cancel_date / sale.date), а не момента
  -- постановки в очередь. Нужна, чтобы «не присылать старое» работало надёжно:
  -- created_at говорит лишь когда мы это заметили, и старый заказ, впервые
  -- классифицированный сегодня, по created_at выглядел бы свежим.
  event_date TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
  created_at TEXT NOT NULL,
  sent_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON notification_queue(status);

CREATE TABLE IF NOT EXISTS chat_settings (
  chat_id INTEGER PRIMARY KEY,
  appearance_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_mutes (
  chat_id INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('order','cancel','buyout','return')),
  shop_id INTEGER,                     -- NULL = замьючено для всех магазинов
  PRIMARY KEY (chat_id, event_type, shop_id)
);

CREATE TABLE IF NOT EXISTS daily_summary (
  chat_id INTEGER NOT NULL,
  date_msk TEXT NOT NULL,
  message_id INTEGER,
  stats_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, date_msk)
);

-- Воронка продаж (Analytics API) — источник цифр «сводки на главной» в ЛК.
-- Statistics API (/orders) считает заказы ИНАЧЕ: на реальных днях у него 30 и 30
-- заказов, а у воронки 37 и 38 — и именно вторые совпадают со сводкой пользователя
-- до рубля. Поэтому «Вчера/Сегодня всего» по ЗАКАЗАМ берётся отсюда, а не из orders.
-- Гранулярность — nmId (размеров у воронки нет), день — по МСК.
CREATE TABLE IF NOT EXISTS sales_funnel_daily (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  date_msk TEXT NOT NULL,
  nm_id INTEGER NOT NULL,
  order_count INTEGER NOT NULL DEFAULT 0,
  order_sum REAL NOT NULL DEFAULT 0,
  buyout_count INTEGER NOT NULL DEFAULT 0,
  buyout_sum REAL NOT NULL DEFAULT 0,
  cancel_count INTEGER NOT NULL DEFAULT 0,
  cancel_sum REAL NOT NULL DEFAULT 0,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY (shop_id, date_msk, nm_id)
);

-- FBS-модуль (Marketplace API, marketplace-api.wildberries.ru).
-- Нужен для расчёта скидки/штрафа за скорость отгрузки: Statistics API
-- (/orders, /sales) момент передачи заказа в доставку НЕ отдаёт вообще.
CREATE TABLE IF NOT EXISTS fbs_assembly_tasks (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  task_id INTEGER NOT NULL,            -- Marketplace: orders[].id (ID сборочного задания)
  rid TEXT NOT NULL,                   -- Marketplace: rid == orders.srid из Statistics API
                                        -- (проверено вживую: совпало 85 из 100)
  supply_id TEXT,                      -- Marketplace: supplyId, ключ к fbs_supplies
  nm_id INTEGER,
  created_at TEXT NOT NULL,            -- Marketplace: createdAt, UTC (наш orders.date = МСК = +3ч)
  supplier_status TEXT,                -- new / confirm / complete("В доставке") / cancel / cancel_carrier
  wb_status TEXT,
  status_checked_at TEXT,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (shop_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_fbs_tasks_rid ON fbs_assembly_tasks(shop_id, rid);
CREATE INDEX IF NOT EXISTS idx_fbs_tasks_supply ON fbs_assembly_tasks(shop_id, supply_id);

-- Поставки кэшируются отдельно: у одной поставки десятки заданий (в живом тесте
-- 30 заданий = 5 уникальных поставок), дёргать /supplies/{id} на каждое задание
-- бессмысленно. closedAt — момент передачи поставки в доставку, то самое Тф.
CREATE TABLE IF NOT EXISTS fbs_supplies (
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  supply_id TEXT NOT NULL,
  created_at TEXT,
  closed_at TEXT,                      -- Marketplace: closedAt (UTC) = Тф, момент отгрузки
  done INTEGER NOT NULL DEFAULT 0,
  refreshed_at TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (shop_id, supply_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(os.path.dirname(db_path) or ".").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Лёгкие миграции для БД, созданных до появления этих колонок (CREATE TABLE IF NOT
# EXISTS в SCHEMA их не добавит в уже существующую таблицу). Для новых БД колонки
# и так есть в SCHEMA — это no-op. Формат: (таблица, колонка, DDL-тип).
_COLUMN_MIGRATIONS = [
    ("orders", "supplier_article", "TEXT"),
    ("sales", "supplier_article", "TEXT"),
    ("cards_cache", "photos_json", "TEXT"),
    ("orders", "subject", "TEXT"),
    ("orders", "warehouse_type", "TEXT"),
    ("sales", "subject", "TEXT"),
    ("sales", "warehouse_type", "TEXT"),
    ("stocks_current", "warehouse_kind", "TEXT NOT NULL DEFAULT 'wb'"),
    ("shops", "notify_from", "TEXT"),
    ("notification_queue", "event_date", "TEXT"),
    ("bot_users", "username", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _COLUMN_MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def _backfill_owners(conn: sqlite3.Connection) -> None:
    """Заводит строку владельца в shop_members для магазинов, подключённых до
    появления таблицы. Без неё уведомления по таким магазинам никому не уйдут:
    рассылка ходит по участникам, а не по shops.telegram_chat_id."""
    conn.execute(
        """
        INSERT OR IGNORE INTO shop_members (shop_id, telegram_user_id, telegram_chat_id, role, added_at)
        SELECT id, owner_user_id, telegram_chat_id, 'owner', ? FROM shops
        """,
        (utcnow(),),
    )
    conn.commit()


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        _backfill_owners(conn)
    finally:
        conn.close()


def get_cursor(conn: sqlite3.Connection, shop_id: int, endpoint: str) -> str | None:
    row = conn.execute(
        "SELECT last_value FROM sync_cursors WHERE shop_id = ? AND endpoint = ?", (shop_id, endpoint)
    ).fetchone()
    return row["last_value"] if row else None


def set_cursor(conn: sqlite3.Connection, shop_id: int, endpoint: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_cursors (shop_id, endpoint, last_value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(shop_id, endpoint) DO UPDATE SET last_value = excluded.last_value, updated_at = excluded.updated_at
        """,
        (shop_id, endpoint, value, utcnow()),
    )


def utcnow() -> str:
    """Текущее время в UTC, ISO 8601 — общий формат временных меток во всей БД."""
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db_session(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
