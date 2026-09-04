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

`TELEGRAM_ADMIN_BOT_TOKEN` — отдельный бот для служебной ленты владельца:
статистика `/admin`, оповещения о кабинетах и обращения в поддержку. Создаётся
в @BotFather так же, как основной. Если его не задать, всё это придёт в основной
бот и смешается с лентой заказов.

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

Процессов три: `wbnotify` — планировщик (опрос WB и рассылка уведомлений),
`wbnotify-bot` — сам бот (команды и нажатия кнопок меню), `wbnotify-admin` —
служебный бот владельца. Нажатия к планировщику не приходят, поэтому первые два
нужны оба; третий запускается только при заданном `TELEGRAM_ADMIN_BOT_TOKEN`.

```bash
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wbnotify wbnotify-bot
systemctl enable --now wbnotify-admin    # только если задан TELEGRAM_ADMIN_BOT_TOKEN
systemctl status wbnotify wbnotify-bot
```

`enable` — автозапуск после перезагрузки сервера, `Restart=always` в юните —
автоподъём при падении.

Логи:

```bash
journalctl -u wbnotify -u wbnotify-bot -f          # в реальном времени
journalctl -u wbnotify -u wbnotify-bot --since today
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

Одной командой (от root):

```bash
cd /opt/wb-notify-bot && git pull && bash deploy/update.sh
```

`deploy/update.sh` доставит зависимости, обновит оба systemd-юнита и
перезапустит оба процесса. `.env` и `data/wbnotify.db` он не трогает.

## Если не пускает по SSH

Симптом: `ssh root@IP` висит и отваливается по таймауту, при этом сервер живой
(порты 80/443 отвечают). Значит, пакеты на 22-й порт дропаются — обычно это
файрвол Timeweb или `fail2ban`, забанивший ваш IP после серии неудачных
попыток подключения.

Что делать: зайти в панели Timeweb в **консоль сервера** (VNC), там
авторизоваться (`login:` — `root`, пароль из панели) и уже в ней выполнить
команду обновления. Пароль при вводе не отображается — это нормально.
Многострочные вставки в VNC-консоль часто ломаются, поэтому команда выше —
однострочная.

Разбанить свой IP изнутри: `fail2ban-client set sshd unbanip ВАШ_IP`.

## Резервная копия

Всё состояние — в одном файле. Достаточно копировать его по расписанию:

```bash
sqlite3 /opt/wb-notify-bot/data/wbnotify.db ".backup '/root/wbnotify-$(date +%F).db'"
```

`.backup` корректно работает на живой базе, в отличие от простого `cp`.

## Что проверить, если уведомления не приходят

```bash
systemctl status wbnotify wbnotify-bot   # процессы живы?
journalctl -u wbnotify -u wbnotify-bot --since "1 hour ago" | grep -i error
sqlite3 data/wbnotify.db "SELECT id, name, token_status, notify_from FROM shops;"
```

- `token_status = invalid` — WB-токен отозван или истёк, нужно переподключить магазин
- `notify_from` пустой — рассылка не включена (шаг 9)
