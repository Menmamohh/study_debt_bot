FROM python:3.11-slim

WORKDIR /app

# Установка часового пояса (Минск / Москва)
ENV TZ=Europe/Minsk
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Только файлы, необходимые в рантайме.
COPY bot.py database.py keyboards.py ./
COPY helper_cat.png meme_shark.jpg ./
COPY assets ./assets

RUN useradd --system --uid 10001 --create-home bot \
    && mkdir -p /data \
    && chown bot:bot /data

ENV DB_PATH=/data/debts.db \
    PYTHONDONTWRITEBYTECODE=1
USER bot

# Запуск бота
CMD ["python", "bot.py"]
