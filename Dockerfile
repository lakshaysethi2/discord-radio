# --- Discord TV Bot + Dashboard image ---------------------------------------
# Same image serves the bot (default CMD), the dashboard (compose overrides),
# and dev/test runs (`make test` mounts the tree and runs pytest).

ARG BASE_TARGET=runtime

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg + libopus for voice; ca-certs for HTTPS; tini for clean signal
# handling; sqlite3 CLI for `make db-shell`; make + bash for in-container Make.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      tini \
      ca-certificates \
      sqlite3 \
      bash \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for defense in depth.
RUN useradd --create-home --shell /bin/bash --uid 1000 tvbot

WORKDIR /app

# Install runtime Python deps first (cache-friendly).
# The bot + dashboard requirements file also contains pytest / ruff / respx
# so the same image can run tests without a second dep-install step.
COPY requirements.txt ./
COPY file_provider/requirements.txt ./file_provider/requirements.txt
RUN pip install -r requirements.txt && \
    pip install -r file_provider/requirements.txt

# App code.
COPY bot ./bot
COPY dashboard ./dashboard
COPY db ./db
COPY provider ./provider
COPY file_provider ./file_provider

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
