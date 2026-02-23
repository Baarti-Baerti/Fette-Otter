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

RUN mkdir -p /data/garth_squad && chmod 700 /data/garth_squad

RUN useradd -m appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080

CMD gunicorn \
    --bind 0.0.0.0:${PORT} \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "wsgi:app"
