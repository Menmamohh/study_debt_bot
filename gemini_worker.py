"""Isolated, cancellable Gemini requests. Never starts Telegram polling."""
import json
import logging
import sys

import schedule_review as review


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload = json.load(sys.stdin)
        operation = sys.argv[1]
        if operation == 'prepare':
            review.prepare_reviews()
            result = None
        elif operation == 'check':
            result = review.check_connection()
        elif operation == 'read':
            result = review.read_schedule(payload['paths'], payload.get('group'))
        else:
            raise review.GeminiError('Неизвестное действие распознавания.')
        reply = {'result': result}
    except review.GeminiError as error:
        reply = {'error': str(error)}
    except Exception:
        reply = {'error': 'Не удалось подготовить результат. Повтори запрос или проверь настройки сервера.'}
    print(json.dumps(reply, ensure_ascii=False))


if __name__ == '__main__':
    main()
