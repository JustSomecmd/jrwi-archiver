FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jrwi ./jrwi

ENV JRWI_DATA=/data \
    JRWI_ARCHIVE=/archive \
    JRWI_MEDIA=/media/jrwi \
    JRWI_PORT=8090 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/.cache

VOLUME ["/data", "/archive", "/media/jrwi"]
EXPOSE 8090
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/ping', timeout=5)"

CMD ["python", "-m", "jrwi", "serve"]
