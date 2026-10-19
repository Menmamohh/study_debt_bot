#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"

PIDS=$(pgrep -f "bot\.py")
if [ -z "$PIDS" ]; then
    echo "⚪ Статус: БОТ ОСТАНОВЛЕН"
else
    echo "🟢 Статус: БОТ РАБОТАЕТ (PID: $PIDS)"
    echo ""
    echo "📜 Последние 15 строк из лога (bot.log):"
    echo "----------------------------------------"
    if [ -f "$DIR/bot.log" ]; then
        tail -n 15 "$DIR/bot.log"
    else
        echo "(файл логов ещё не создан)"
    fi
    echo "----------------------------------------"
fi
