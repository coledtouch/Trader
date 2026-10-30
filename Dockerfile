# AI Trader: the web app and the trading engine in one container (docs/DEPLOY.md).
# IB Gateway and Cloudflare Tunnel run as separate containers (docker-compose.yml).
FROM python:3.13-slim-trixie

# The app runs as this non-root user. Match it to the owner of ./data on the host
# (`id -u` / `id -g`; the first user on a fresh Ubuntu droplet is 1000) so the bind-mounted
# data directory and .env are writable without opening their permissions.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRADER_DATA_DIR=/data \
    TRADER_HOST=0.0.0.0 \
    TRADER_PORT=8000

RUN groupadd --gid "${APP_GID}" trader \
    && useradd --uid "${APP_UID}" --gid trader --no-create-home --home-dir /app \
        --shell /usr/sbin/nologin trader \
    && mkdir -p /app /data \
    && chown trader:trader /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY trader/ ./trader/

USER trader
VOLUME ["/data"]
EXPOSE 8000

# /healthz needs no login and reveals nothing. The Host header (127.0.0.1) must stay in
# TRADER_ALLOWED_HOSTS or this check fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

CMD ["python", "-m", "trader", "serve"]
