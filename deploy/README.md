# Развёртывание на сервере (Timeweb Cloud)

Нужен **облачный сервер (VPS)**, а не обычный хостинг сайтов: бот — постоянно
работающий процесс, а не сайт, отвечающий на запросы. Самого дешёвого тарифа
достаточно: один Python-процесс и файл SQLite.

Пока процесс не запущен, бот не опрашивает WB и ничего не присылает.

---

## 1. Создать сервер

В панели Timeweb Cloud: **Облачные серверы → Создать**.

- ОС: **Ubuntu 24.04**
- Конфигурация: минимальная (1 vCPU / 1 ГБ RAM / 10 ГБ диска) — с запасом
- Регион: любой; часовой пояс настроим отдельно (шаг 2)

Записать IP-адрес и root-пароль (или добавить свой SSH-ключ при создании).

## 2. Подключиться и подготовить систему

```bash
ssh root@IP_СЕРВЕРА
```

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git

# Часовой пояс — МСК. WB отдаёт все даты в московском времени, и суточные
# расписания бота должны совпадать с ним.
timedatectl set-timezone Europe/Moscow
timedatectl   # проверить, что стоит MSK
```

Отдельный пользователь для сервиса (не запускаем бота от root):

```bash
adduser --system --group --home /opt/wb-notify-bot wbnotify
```

## 3. Залить код

Вариант А — через Git (рекомендуется: обновление одной командой, есть откат):

```bash
cd /opt
git clone АДРЕС_РЕПОЗИТОРИЯ wb-notify-bot
```

Вариант Б — без Git, скопировать с рабочего компьютера:

```bash
# выполняется НА ЛОКАЛЬНОЙ МАШИНЕ, не на сервере
scp -r C:/Users/Наталья/wb-notify-bot root@IP_СЕРВЕРА:/opt/wb-notify-bot
```

## 4. Окружение и зависимости

```bash
cd /opt/wb-notify-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 5. Секреты (`.env`)

`.env` НЕ хранится в репозитории — переносится вручную.

```bash
cp .env.example .env
nano .env
```

Заполнить `TELEGRAM_BOT_TOKEN`, `TOKEN_ENCRYPTION_KEY`, `TELEGRAM_ADMIN_CHAT_ID`.

> **`TOKEN_ENCRYPTION_KEY` — тот же самый, что на рабочей машине.** Этим ключом
> зашифрованы WB-токены магазинов в базе. Другой ключ = токены не расшифруются
> и все магазины придётся подключать заново.

Закрыть доступ к файлу:

```bash
chmod 600 .env
```

## 6. Перенести базу

В `data/wbnotify.db` лежит история заказов, курсоры синка и отметка
`notify_from`. Без неё бот начнёт с нуля и заново вычитает всю историю.

```bash
# НА ЛОКАЛЬНОЙ МАШИНЕ
scp C:/Users/Наталья/wb-notify-bot/data/wbnotify.db root@IP_СЕРВЕРА:/opt/wb-notify-bot/data/
```

## 7. Проверить, что всё живо

```bash
cd /opt/wb-notify-bot
chown -R wbnotify:wbnotify /opt/wb-notify-bot
sudo -u wbnotify .venv/bin/python scripts/check_connectivity.py
```

Должно быть OK по Telegram; в чат придёт тестовое сообщение.

## 8. Запустить как сервис

```bash
cp deploy/wbnotify.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wbnotify
systemctl status wbnotify
```

`enable` — автозапуск после перезагрузки сервера, `Restart=always` в юните —
автоподъём при падении.

Логи:

```bash
journalctl -u wbnotify -f          # в реальном времени
journalctl -u wbnotify --since today
```

## 9. Включить рассылку

Бот по умолчанию **не рассылает ничего**, пока не выставлена отметка
`notify_from` — это защита от того, чтобы накопленная история (десятки тысяч
событий) не улетела в чат разом.

```bash
sudo -u wbnotify .venv/bin/python scripts/notify_cli.py start-notifying --shop-id 1
```

Если база переносилась с рабочей машины, отметка уже выставлена — команда просто
сдвинет её на текущий момент.

---

## Обновление кода

```bash
cd /opt/wb-notify-bot
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart wbnotify
```

## Резервная копия

Всё состояние — в одном файле. Достаточно копировать его по расписанию:

```bash
sqlite3 /opt/wb-notify-bot/data/wbnotify.db ".backup '/root/wbnotify-$(date +%F).db'"
```

`.backup` корректно работает на живой базе, в отличие от простого `cp`.

## Что проверить, если уведомления не приходят

```bash
systemctl status wbnotify              # процесс жив?
journalctl -u wbnotify --since "1 hour ago" | grep -i error
sqlite3 data/wbnotify.db "SELECT id, name, token_status, notify_from FROM shops;"
```

- `token_status = invalid` — WB-токен отозван или истёк, нужно переподключить магазин
- `notify_from` пустой — рассылка не включена (шаг 9)
