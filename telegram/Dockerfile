FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# Проверяем не «жив ли процесс», а «жив ли поллер»: 03-08-2026 контейнер 38 часов
# показывал Up, пока getUpdates молча не работал. /health отдаёт 503, если ответа
# от Telegram давно не было. Рестарт делает внутренний сторож (os._exit + docker
# --restart), healthcheck нужен чтобы состояние было видно в docker ps.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('TGBOT_EVENT_PORT','8877')+'/health', timeout=5)" || exit 1
CMD ["python", "-u", "bot.py"]
