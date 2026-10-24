FROM python:3.11-slim

WORKDIR /app

# Установка часового пояса (Минск / Москва)
ENV TZ=Europe/Minsk
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Только файлы, необходимые в рантайме.
COPY bot.py database.py keyboards.py gdrive_sync.py schedule_review.py gemini_schedule.py gemini_worker.py ./
COPY helper_cat.png ./
COPY assets ./assets

RUN mkdir -p /app/data /app/assets/schedule/downloads

ENV DB_PATH=/app/data/debts.db \
    PYTHONDONTWRITEBYTECODE=1
# Запуск бота
CMD ["python", "bot.py"]
