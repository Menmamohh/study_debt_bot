# 🚀 Инструкция по развертыванию «Помощника Пятнашки» на сервере

Бот полностью подготовлен для непрерывной круглосуточной работы (24/7).

---

## Шаг 1. Перенос файлов на сервер

### Вариант А: Быстро скопировать папку через Терминал на Mac (SCP)
На вашем Mac в Терминале выполните (замените `root@IP_СЕРВЕРА` на ваш IP):
```bash
scp -r "/Users/menmamac/Documents/mind menma/study_debt_bot" root@IP_СЕРВЕРА:/opt/study_debt_bot
```

### Вариант Б: Через SFTP / FileZilla
Просто перетащите папку `study_debt_bot` на сервер в каталог `/opt/study_debt_bot`.

---

## Шаг 2. Запуск на сервере (выберите один удобный способ)

### Способ 1: Через Docker (самый простой и надежный, рекомендуется ⭐)
На сервере перейдите в папку бота и выполните:
```bash
cd /opt/study_debt_bot
cp .env.example .env  # если .env ещё не создан
chmod 600 .env
# Укажите BOT_TOKEN и числовой ADMIN_ID, затем сохраните файл.
mkdir -p data
if [ ! -f data/debts.db ] && [ -f debts.db ]; then cp debts.db data/debts.db; fi
docker compose up -d --build
```
* **Готово!** Бот работает в изолированном контейнере, автоматически перезапустится при падениях или перезагрузке сервера.
* **Посмотреть логи:** `docker compose logs -f`
* **Остановить бота:** `docker compose down`

---

### Способ 2: Классический запуск через Linux systemd (без Docker)
1. Подключитесь к серверу по SSH:
   ```bash
   cd /opt/study_debt_bot
   adduser --system --group --no-create-home study-debt-bot
   chown -R root:study-debt-bot /opt/study_debt_bot
   chmod 750 /opt/study_debt_bot
   cp .env.example .env  # если .env ещё не создан
   chown root:study-debt-bot .env
   chmod 640 .env
   # Укажите BOT_TOKEN и числовой ADMIN_ID в .env.
   apt update && apt install -y python3 python3-venv python3-pip
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   install -d -o study-debt-bot -g study-debt-bot -m 750 /var/lib/study-debt-bot
   # При первом переносе установите существующую базу; повторная команда её не перезапишет.
   if [ -f debts.db ] && [ ! -f /var/lib/study-debt-bot/debts.db ]; then install -o study-debt-bot -g study-debt-bot -m 640 debts.db /var/lib/study-debt-bot/debts.db; fi
   ```
2. Подключите системную службу:
   ```bash
   cp study_debt_bot.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now study_debt_bot
   ```
* **Проверить статус:** `systemctl status study_debt_bot`
* **Смотреть логи:** `journalctl -u study_debt_bot -f`
* **Перезапустить:** `systemctl restart study_debt_bot`
* **Остановить:** `systemctl stop study_debt_bot`

---

## 🔒 Переменные окружения (.env)
Убедитесь, что в файле `/opt/study_debt_bot/.env` указан ваш токен бота:
```env
BOT_TOKEN=токен_от_BotFather
```
База данных `debts.db` сохраняется автоматически на сервере и не сбрасывается.
