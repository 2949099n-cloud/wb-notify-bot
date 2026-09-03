#!/usr/bin/env bash
# Обновление бота на сервере до текущего main.
#
# Запускать от root:
#     cd /opt/wb-notify-bot && git pull && bash deploy/update.sh
#
# Скрипт идемпотентен: можно гонять повторно. Ни .env, ни data/wbnotify.db
# не трогает — они не в репозитории и переживают любое обновление.
set -euo pipefail

APP_DIR=/opt/wb-notify-bot
cd "$APP_DIR"

echo "== зависимости =="
.venv/bin/pip install -q -r requirements.txt

echo "== systemd-юниты =="
# Два процесса: wbnotify — планировщик (опрос WB + рассылка),
# wbnotify-bot — сам бот (команды и нажатия инлайн-кнопок меню).
cp deploy/wbnotify.service deploy/wbnotify-bot.service /etc/systemd/system/
systemctl daemon-reload

chown -R wbnotify:wbnotify "$APP_DIR"

echo "== перезапуск =="
systemctl enable --now wbnotify wbnotify-bot
systemctl restart wbnotify wbnotify-bot

sleep 3
systemctl is-active wbnotify wbnotify-bot
echo "Готово. Логи: journalctl -u wbnotify -u wbnotify-bot -f"
