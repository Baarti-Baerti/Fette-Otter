FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy source
COPY . .

# Token storage lives in a named volume so it survives redeploys
# Default: /data/garth_squad  (override with GARTH_SQUAD_HOME)
ENV GARTH_SQUAD_HOME=/data/garth_squad
ENV PORT=8080

# Create data dir (will be overridden by volume mount at runtime)
RUN mkdir -p /data/garth_squad && chmod 700 /data/garth_squad

# Non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080

# Gunicorn: 4 workers, 120s timeout (Garmin API can be slow)
CMD gunicorn \
    --bind 0.0.0.0:${PORT} \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "api.server:app"
