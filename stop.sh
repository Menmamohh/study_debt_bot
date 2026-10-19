#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"

PID_FILE="$DIR/bot.pid"
STOPPED=0

if [ -f "$PID_FILE" ]; then
    read -r BOT_PID < "$PID_FILE"
    if kill -0 "$BOT_PID" 2>/dev/null; then
        kill "$BOT_PID" 2>/dev/null || true
        for _ in $(seq 1 5); do
            if ! kill -0 "$BOT_PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        kill -9 "$BOT_PID" 2>/dev/null || true
        STOPPED=1
    fi
    rm -f "$PID_FILE"
fi

# Также проверяем любые процессы bot.py из этой директории
EXTRA_PIDS=$(pgrep -f "$DIR/bot\.py" 2>/dev/null || true)
if [ -n "$EXTRA_PIDS" ]; then
    for p in $EXTRA_PIDS; do
        kill "$p" 2>/dev/null || true
    done
    sleep 1
    for p in $EXTRA_PIDS; do
        kill -9 "$p" 2>/dev/null || true
    done
    STOPPED=1
fi

if [ $STOPPED -eq 1 ]; then
    echo "🛑 Бот остановлен."
else
    echo "Бот уже остановлен."
fi
