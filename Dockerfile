# syntax=docker/dockerfile:1
FROM python:3.14-slim-bookworm

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA1_amd64=e63c11a9726b775a6a11801e81af4f3fb926aa68
ARG SUPERCRONIC_SHA1_arm64=0b6c5bb743e0b0dafed1132198c81807927ac413
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    TZ=Europe/Warsaw \
    CRON_SCHEDULE="0 9 3 * *" \
    LOGIN_PORT=8080

# supercronic - cron dla kontenerów (bez roota, logi JSON, poprawna obsługa sygnałów)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tzdata; \
    case "${TARGETARCH:-amd64}" in \
      amd64) sha1="$SUPERCRONIC_SHA1_amd64" ;; \
      arm64) sha1="$SUPERCRONIC_SHA1_arm64" ;; \
      *) echo "nieobsługiwana architektura: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH:-amd64}"; \
    echo "${sha1}  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN set -eux; \
    pip install -r /app/requirements.txt; \
    playwright install --with-deps --no-shell chromium; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -g "$GID" app \
 && useradd -u "$UID" -g app -m -d /home/app -s /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown app:app /data \
 && chmod 0700 /data

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --chmod=0644 orange_rabat.py orange_login.py /app/

USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["cron"]
