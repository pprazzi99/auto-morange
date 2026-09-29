#!/bin/sh
# Tryby:
#   cron (domyślny) - harmonogram z CRON_SCHEDULE przez supercronic, logi JSON na stdout
#   run             - jednorazowe uruchomienie
#   test-smtp       - mail testowy (sprawdza konfigurację SMTP, bez przeglądarki)
#   login           - panel WWW do zalogowania w Mój Orange (orange_login.py)
#   inne            - wykonanie podanej komendy
set -eu

case "${1:-cron}" in
  cron)
    if [ -z "${CRON_SCHEDULE:-}" ]; then
      echo '{"level":"error","msg":"brak zmiennej CRON_SCHEDULE"}'
      exit 64
    fi
    case "$CRON_SCHEDULE" in
      *"
"*) echo '{"level":"error","msg":"CRON_SCHEDULE nie może zawierać nowej linii"}'; exit 64 ;;
    esac

    crontab="$(mktemp)"
    printf '%s python /app/orange_rabat.py\n' "$CRON_SCHEDULE" > "$crontab"
    supercronic -json -test "$crontab" >/dev/null 2>&1 || {
      echo '{"level":"error","msg":"niepoprawny format CRON_SCHEDULE"}'
      exit 64
    }

    if [ "${RUN_ON_START:-0}" = "1" ]; then
      python /app/orange_rabat.py || true
    fi
    # -passthrough-logs: linie JSON skryptu idą na stdout bez opakowywania,
    # -json: własne logi supercronic (start/koniec/kod wyjścia joba) też jako JSON.
    exec supercronic -json -passthrough-logs "$crontab"
    ;;
  run)
    exec python /app/orange_rabat.py
    ;;
  test-smtp)
    exec python /app/orange_rabat.py --test-smtp
    ;;
  login)
    shift
    exec python /app/orange_login.py --host 0.0.0.0 "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
