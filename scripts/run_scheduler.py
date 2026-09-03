"""
Шаг 5 — фоновый процесс: опрос WB по расписанию и рассылка уведомлений.

Запуск:
    python scripts/run_scheduler.py

Пока процесс не запущен, бот не опрашивает WB и ничего не присылает.
Остановка — Ctrl+C.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wbnotify.scheduler.runner import run

if __name__ == "__main__":
    run()
