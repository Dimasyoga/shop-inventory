FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All mutable state lives here and nothing is written to /app, so the source
# tree can stay read-only and a `git clean` can never touch the shop's data.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

ENV SHOP_DB_PATH=/data/shop.db \
    SHOP_SECRET_KEY_PATH=/data/.secret_key \
    SHOP_ENCRYPTION_KEY_PATH=/data/.encryption_key
VOLUME ["/data"]

EXPOSE 5000

# Exactly one worker, and this is not a tuning knob. The Telegram poller is an
# in-process thread holding a single getUpdates offset, and the store is one
# SQLite file -- a second worker duplicates every bot message and doubles the
# stale-order alerts. Scale with threads, never with workers.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:5000", "--access-logfile", "-", \
     "--timeout", "60", "wsgi:app"]
