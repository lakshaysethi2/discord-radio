# --- Discord TV Bot + Dashboard image ---------------------------------------
# Same image serves both the bot (default CMD) and the dashboard (compose
# overrides the CMD). Keeps the build simple and dependency-sharing tight.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg + libopus for voice; ca-certs for HTTPS; tini for clean signal handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      tini \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for defense in depth.
RUN useradd --create-home --shell /bin/bash --uid 1000 tvbot

WORKDIR /app

# Install Python deps first (cache-friendly).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code.
COPY bot ./bot
COPY dashboard ./dashboard
COPY db ./db
COPY provider ./provider

# Data & cache mount points.
RUN mkdir -p /data /cache && chown -R tvbot:tvbot /app /data /cache

USER tvbot

ENV DATABASE_PATH=/data/tv.db \
    CACHE_PATH=/cache \
    DASHBOARD_PORT=8000

VOLUME ["/data", "/cache"]

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot.main"]
