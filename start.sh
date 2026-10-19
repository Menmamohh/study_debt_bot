#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PID_FILE="$DIR/bot.pid"
if [ -f "$PID_FILE" ]; then
    read -r BOT_PID < "$PID_FILE"
    if kill -0 "$BOT_PID" 2>/dev/null && ps -p "$BOT_PID" -o command= | grep -Fq "$DIR/bot.py"; then
        echo "Бот уже запущен (PID $BOT_PID)."
        exit 0
    fi
    rm -f "$PID_FILE"
fi

export BOT_LOG_FILE="$DIR/bot.log"
nohup "$DIR/venv/bin/python3" "$DIR/bot.py" </dev/null >/dev/null 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"
sleep 1
if ! kill -0 "$BOT_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Бот завершился при запуске. Последние строки журнала:"
    tail -n 20 "$DIR/bot.log"
    exit 1
fi
echo "🚀 Бот успешно запущен в фоновом режиме!"
echo "🆔 PID процесса: $BOT_PID"
echo "📄 Просмотр логов: tail -f bot.log"
echo "🛑 Остановка: ./stop.sh"
