FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy from the garmin-backend subfolder
COPY garmin-backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY garmin-backend/ .

ENV GARTH_SQUAD_HOME=/data/garth_squad
ENV PORT=8080

# Create data dir, create user, fix all permissions before switching user
RUN useradd -m appuser \
    && mkdir -p /data/garth_squad \
    && chown -R appuser:appuser /app /data \
    && chmod -R 755 /data \
    && chmod 700 /data/garth_squad

USER appuser

EXPOSE 8080

CMD python api/server.py
