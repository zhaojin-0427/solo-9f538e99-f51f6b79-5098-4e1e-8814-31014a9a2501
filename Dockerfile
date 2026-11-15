FROM python:3.12-slim

# tzdata 提供完整 IANA 时区库，zoneinfo 依赖它处理夏令时
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCHED_DB_PATH=/data/schedule.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .
COPY samples ./samples

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/ -o /dev/null || exit 1

# 多 worker 并发上传；SQLite WAL + BEGIN IMMEDIATE 保证裁决串行一致
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "60", "wsgi:app"]
