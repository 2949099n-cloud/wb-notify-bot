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
cp deploy/wbnotify.service deploy/wbnotify-bot.service deploy/wbnotify-admin.service /etc/systemd/system/
systemctl daemon-reload

chown -R wbnotify:wbnotify "$APP_DIR"

echo "== перезапуск =="
systemctl enable --now wbnotify wbnotify-bot
systemctl restart wbnotify wbnotify-bot

# Служебный бот владельца — только если для него задан токен. Без токена процесс
# сразу упал бы, и systemd крутил бы его в бесконечном рестарте.
SERVICES="wbnotify wbnotify-bot"
if grep -qE '^TELEGRAM_ADMIN_BOT_TOKEN=.+' .env; then
  systemctl enable --now wbnotify-admin
  systemctl restart wbnotify-admin
  SERVICES="$SERVICES wbnotify-admin"
else
  systemctl disable --now wbnotify-admin 2>/dev/null || true
  echo "TELEGRAM_ADMIN_BOT_TOKEN не задан — служебный бот не запускается"
fi

sleep 3
# Статус ПО КАЖДОМУ сервису с именем: голый список из «active active» не
# показывал, какой процесс какой, и пропущенный третий было не заметить.
for unit in $SERVICES; do
  printf '%-16s %s
' "$unit" "$(systemctl is-active "$unit")"
done
echo "Готово. Логи: journalctl -u wbnotify -u wbnotify-bot -u wbnotify-admin -f"
