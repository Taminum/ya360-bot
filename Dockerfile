FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY y360/ ./y360/
COPY bot.py ./
COPY tests/ ./tests/
COPY examples/ ./examples/

# бот пишет журнал и архив счетов только в /app/data
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/out \
    && chown -R bot:bot /app
USER bot

VOLUME ["/app/data"]

# контейнер считается живым, пока процесс бота отвечает Telegram
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
t=os.environ.get('TELEGRAM_BOT_TOKEN'); \
sys.exit(0 if t and urllib.request.urlopen(f'https://api.telegram.org/bot{t}/getMe', timeout=20).status == 200 else 1)"

CMD ["python", "bot.py"]
