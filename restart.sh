#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "🔄 Перезапуск бота «Помощник Пятнашки»..."
"$DIR/stop.sh"
sleep 1
"$DIR/start.sh"
