"""Точка входа служебного бота владельца сервиса (статистика, алерты, поддержка).

Запускается отдельным процессом (deploy/wbnotify-admin.service) и только если
в .env задан TELEGRAM_ADMIN_BOT_TOKEN.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wbnotify.telegram.admin_bot import run

if __name__ == "__main__":
    run()
