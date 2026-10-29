FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAILHUB_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 mailhub \
    && useradd --uid 10001 --gid mailhub --no-create-home mailhub \
    && mkdir -p /data \
    && chown mailhub:mailhub /data
COPY --chown=mailhub:mailhub mailhub ./mailhub
USER mailhub
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request, urllib.parse; host=urllib.parse.urlsplit(os.environ['MAILHUB_PUBLIC_URL']).netloc; request=urllib.request.Request('http://127.0.0.1:8080/health', headers={'Host':host}); urllib.request.urlopen(request, timeout=3)" || exit 1
CMD ["uvicorn", "mailhub.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
