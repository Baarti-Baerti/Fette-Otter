FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY garmin-backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY garmin-backend/ .
COPY start.sh /start.sh
RUN chmod +x /start.sh

ENV GARTH_SQUAD_HOME=/data/garth_squad
ENV PORT=8080

RUN useradd -m appuser && chown -R appuser:appuser /app

EXPOSE 8080

# Run as root so start.sh can fix volume permissions, then drop to appuser
CMD ["/start.sh"]
