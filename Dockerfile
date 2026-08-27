FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# yt-dlp breaks whenever YouTube changes, so it is installed in its own layer.
# Bump YTDLP_REFRESH (e.g. --build-arg YTDLP_REFRESH=$(date +%s)) to force a
# fresh copy instead of reusing a cached, stale layer.
ARG YTDLP_REFRESH=1
RUN echo "yt-dlp refresh: ${YTDLP_REFRESH}" \
    && pip install --no-cache-dir -U "yt-dlp[default,curl-cffi]"

# Copy every module: an explicit list silently ships a broken image the first
# time a new file is added.
COPY *.py ./
COPY web/static ./web/static

ENV BIND_HOST=0.0.0.0 \
    PORT=8787 \
    OUTPUT_DIR=/data \
    ALLOW_LOCALHOST=1 \
    REQUIRE_TAILSCALE_CLIENT=0 \
    DOCKER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /data

USER appuser
EXPOSE 8787

CMD ["python", "web_server.py"]
