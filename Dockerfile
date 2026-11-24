# LAN Call behind a TLS-terminating reverse proxy (Traefik, Caddy, nginx).
# The proxy provides HTTPS; the app listens on plain HTTP port 8000.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    LANCALL_NO_TLS=1 LANCALL_TRUST_PROXY=1 \
    LANCALL_HOST=0.0.0.0 LANCALL_PORT=8000 LANCALL_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lancall ./lancall
COPY web ./web

RUN useradd --system --home /app lancall && mkdir /data && chown lancall /data
USER lancall
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
CMD ["python", "-m", "lancall"]
