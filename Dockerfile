FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts

RUN pip install --no-cache-dir . && mkdir -p /app/data

# Очередь должна переживать рестарт контейнера
VOLUME ["/app/data"]

CMD ["python", "-m", "app.main"]