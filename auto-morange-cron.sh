#!/usr/bin/env bash
# Zarządza wpisem w crontabie dla auto-morange (wariant bez Dockera).
#
#   ./auto-morange-cron.sh install ["0 9 3 * *"]   dodaj/zaktualizuj wpis (domyślnie 3. dnia miesiąca, 9:00)
#   ./auto-morange-cron.sh remove                  usuń wpis
#   ./auto-morange-cron.sh status                  pokaż aktualny wpis
#   ./auto-morange-cron.sh run                     uruchom jeden przebieg teraz (jak z crona)
#
# Wpis jest oznaczony komentarzem, więc ponowne "install" go podmienia, a reszta crontabu zostaje nietknięta.
# Wymaga wcześniej przygotowanego .venv (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# && .venv/bin/playwright install --no-shell chromium).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
LOG="$DIR/data/orange.log"
MARK="# auto-morange"
DEFAULT_SCHEDULE="0 9 3 * *"

# Poza samym przebiegiem: LOW_RESOURCES=1 (bez obrazków/fontów) i log do pliku.
job() { printf 'cd %q && LOW_RESOURCES=1 %q orange_rabat.py >> %q 2>&1' "$DIR" "$PYTHON" "$LOG"; }

current() { crontab -l 2>/dev/null | grep -vF "$MARK" || true; }

need_venv() {
  [ -x "$PYTHON" ] || { echo "Brak $PYTHON - najpierw przygotuj .venv (patrz INSTRUKCJA_SETUP.md, wariant B)." >&2; exit 1; }
}

case "${1:-status}" in
  install)
    need_venv
    schedule="${2:-$DEFAULT_SCHEDULE}"
    if [ "$(awk '{print NF}' <<<"$schedule")" -ne 5 ]; then
      echo "Harmonogram musi mieć 5 pól, np. \"$DEFAULT_SCHEDULE\" (min godz dzień-mies mies dzień-tyg)." >&2
      exit 64
    fi
    mkdir -p "$DIR/data"; chmod 700 "$DIR/data"
    { current; printf '%s %s %s\n' "$schedule" "$(job)" "$MARK"; } | crontab -
    echo "Dodano wpis do crontaba:"
    crontab -l | grep -F "$MARK"
    ;;
  remove)
    current | crontab -
    echo "Usunięto wpis auto-morange z crontabu."
    ;;
  status)
    crontab -l 2>/dev/null | grep -F "$MARK" || echo "Brak wpisu auto-morange w crontabie."
    ;;
  run)
    need_venv
    cd "$DIR" && LOW_RESOURCES=1 exec "$PYTHON" orange_rabat.py
    ;;
  *)
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 64
    ;;
esac
