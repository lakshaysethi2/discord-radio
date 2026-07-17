# --- File Provider service image --------------------------------------------

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      tini \
      ca-certificates \
      sqlite3 \
      aria2 \
      bash \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 1000 provider

WORKDIR /app

COPY file_provider/requirements.txt ./
RUN pip install -r requirements.txt

COPY file_provider ./file_provider

RUN mkdir -p /data /cache /media && chown -R provider:provider /app /data /cache /media

USER provider

ENV FILE_PROVIDER_DB_PATH=/data/provider.db \
    FILE_PROVIDER_CACHE_PATH=/cache \
    FILE_PROVIDER_HOST=0.0.0.0 \
    FILE_PROVIDER_PORT=8001 \
    LOCAL_MEDIA_PATH=/media

VOLUME ["/data", "/cache", "/media"]

EXPOSE 8001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "file_provider.api.main:app", "--host", "0.0.0.0", "--port", "8001"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3).status==200 else 1)"
