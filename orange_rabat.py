#!/usr/bin/env python3
"""
Comiesięczne odebranie rabatu w Mój Orange + mail z wynikiem.

Użycie:
  python3 orange_login.py           # pierwszy raz: panel WWW do zalogowania (także na serwerze bez ekranu)
  python3 orange_rabat.py           # tryb automatyczny (dla crona / Dockera), bez okna
  python3 orange_rabat.py --setup   # alternatywa dla panelu: zwykłe okno przeglądarki (komputer z ekranem)

Konfiguracja (od najważniejszej):
  1. zmienne środowiskowe,
  2. plik .env obok skryptu (albo ENV_FILE),
  3. DATA_DIR/config.env - zapisywany z panelu WWW (orange_login.py).

  DATA_DIR                         - katalog na sesję, konfigurację i zrzut (domyślnie ./data obok skryptu)
  ORANGE_LOGIN, ORANGE_PASSWORD    - opcjonalnie, gdy Orange poprosi tylko o hasło
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO, MAIL_FROM - opcjonalnie, mail z wynikiem
  ORANGE_SEL_*                     - opcjonalne nadpisanie selektorów

Logi są wypisywane na stdout jako JSON (jedna linia = jedno zdarzenie).
Test poczty bez przeglądarki: python3 orange_rabat.py --test-smtp
Kody wyjścia: 0 - rabat widoczny, 1 - błąd / brak rabatu, 2 - wymagane ponowne logowanie.
"""
import os
import sys
import time
import json
import logging
import smtplib
import datetime
from pathlib import Path
from email.message import EmailMessage

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def load_env_file(path: Path) -> None:
    """Wczytuje KEY=wartość z pliku; nie nadpisuje zmiennych, które mają już niepustą wartość."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not os.environ.get(key):
            os.environ[key] = value


# Lokalnie (cron na hoście) konfiguracja leży w .env obok skryptu; w Dockerze podaje ją --env-file.
load_env_file(Path(os.environ.get("ENV_FILE") or Path(__file__).with_name(".env")))

# --- KONFIGURACJA --------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().with_name("data"))
STATE_FILE = DATA_DIR / "state.json"               # ciasteczka / zapamiętane urządzenie (POUFNE!)
CONFIG_FILE = DATA_DIR / "config.env"              # ustawienia zapisane z panelu WWW (POUFNE!)
SCREENSHOT = DATA_DIR / "orange_last.png"
START_URL = "https://www.orange.pl/moj-orange"
# Strona, na której po zalogowaniu sprawdzamy, czy rabat jest naliczony
REWARD_URL = os.environ.get("ORANGE_REWARD_URL") or "https://www.orange.pl/moj-orange/uslugi-pakietowe"

# Klucze ustawione w środowisku / .env wygrywają z config.env - panel WWW ich nie nadpisze.
ENV_OVERRIDES = {k for k, v in os.environ.items() if v}
load_env_file(CONFIG_FILE)

# Stały user-agent, żeby tryb headless wyglądał dla Orange tak samo jak sesja logowania
# (inaczej "zapamiętane urządzenie" może nie zostać rozpoznane).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1366, "height": 900}

# TODO: uzupełnij selektory po podejrzeniu strony (F12 -> prawy klik na elemencie -> Copy selector).
# Każdy można nadpisać zmienną ORANGE_SEL_<NAZWA> (w .env albo w panelu WWW).
SELECTORS = {
    "LOGGED_IN": "text=Faktury i płatności",       # zakładka menu widoczna tylko po zalogowaniu
    "COOKIES": "text=Kontynuuj bez wyrażania zgody",   # baner cookies (zamykany, jeśli się pojawi)
    "LOGIN_INPUT": "input[data-test-id='input-login']",  # pole "Adres e-mail lub numer telefonu"
    "PASSWORD_INPUT": "input[type='password']",
    "SUBMIT": "button[type='submit']",             # "Dalej" / "Zaloguj"
    "REWARD_BUTTON": "",                           # opcjonalny przycisk do kliknięcia na REWARD_URL
    "REWARD_DONE": "text=Rabat za e-fakturę",      # napis potwierdzający rabat na REWARD_URL
}
# --------------------------------------------------------------------------------


def sel(name: str) -> str:
    return os.environ.get(f"ORANGE_SEL_{name}") or SELECTORS[name]


# Wartości tych zmiennych nigdy nie mogą trafić do logów ani maili.
SECRET_ENV = ("ORANGE_LOGIN", "ORANGE_PASSWORD", "SMTP_USER", "SMTP_PASS")


def redact(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 3:
            text = text.replace(value, "***")
            text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, ensure_ascii=False))


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("orange_rabat")
    logger.handlers[:] = [handler]
    level = "DEBUG" if os.environ.get("DEBUG") == "1" else os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(level)
    logger.propagate = False
    return logger


log = setup_logging()


def event(level: int, msg: str, **fields) -> None:
    log.log(level, msg, extra={"fields": fields})


def send_mail(subject: str, body: str, attachment: Path | None = None) -> bool:
    """Wysyła mail z wynikiem. Zwraca True, gdy wysłano; błędy tylko loguje."""
    if not os.environ.get("SMTP_HOST"):
        event(logging.INFO, "mail pominięty (brak SMTP_HOST)", subject=subject)
        return False
    missing = [k for k in ("MAIL_TO",) if not os.environ.get(k)]
    if not (os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER")):
        missing.append("SMTP_USER/MAIL_FROM")
    if missing:
        event(logging.ERROR, "nie udało się wysłać maila - niepełna konfiguracja",
              subject=subject, missing=missing)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
        msg["To"] = os.environ["MAIL_TO"]
        msg.set_content(redact(body))
        if attachment and attachment.exists():
            msg.add_attachment(attachment.read_bytes(), maintype="image",
                               subtype="png", filename=attachment.name)
        host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 465)
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
            smtp.starttls()
        with smtp as s:
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
        event(logging.INFO, "mail wysłany", subject=subject)
        return True
    except Exception as e:
        # Błąd maila nie może zmienić wyniku odbioru rabatu - tylko logujemy.
        event(logging.ERROR, "nie udało się wysłać maila",
              subject=subject, error=f"{type(e).__name__}: {e}")
        return False


def launch_browser(p, headless: bool = True):
    if not headless:
        return p.chromium.launch(headless=False)
    # channel="chromium" = pełny Chromium w trybie "new headless" (mniej wykrywalny niż headless shell)
    return p.chromium.launch(headless=True, channel="chromium", args=["--disable-dev-shm-usage"])


def new_context(browser, with_state: bool):
    ctx = browser.new_context(
        storage_state=str(STATE_FILE) if with_state and STATE_FILE.exists() else None,
        user_agent=USER_AGENT,
        locale="pl-PL",
        viewport=VIEWPORT,
    )
    # Mój Orange jest ciężki - na słabym serwerze (1 vCPU) ładowanie trwa nawet 1-2 minuty.
    ctx.set_default_timeout(60000)
    ctx.set_default_navigation_timeout(120000)
    return ctx


def block_heavy(ctx) -> None:
    """Bez obrazków, fontów i mediów - kilka razy mniej pracy dla przeglądarki."""
    ctx.route("**/*", lambda route: route.abort()
              if route.request.resource_type in ("image", "media", "font")
              else route.continue_())


def write_private(path: Path, write) -> None:
    """Atomowy zapis pliku z uprawnieniami 600 (write dostaje ścieżkę tymczasową)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    tmp.chmod(0o600)
    tmp.replace(path)


def save_state(ctx) -> None:
    write_private(STATE_FILE, lambda tmp: ctx.storage_state(path=str(tmp)))


LOW_RESOURCES = os.environ.get("LOW_RESOURCES") == "1"
# Na słabszym sprzęcie strona rabatu ładuje się kilka razy dłużej.
REWARD_TIMEOUT = 60000 if LOW_RESOURCES else 20000
SETTLE_TIMEOUT = 30000 if LOW_RESOURCES else 10000


def debug_page(page) -> None:
    """DEBUG=1: loguje nawigację, błędy konsoli i nieudane żądania."""
    page.on("framenavigated", lambda f: f == page.main_frame and event(
        logging.DEBUG, "nawigacja", url=f.url.split("?")[0]))
    page.on("console", lambda m: m.type == "error" and event(
        logging.DEBUG, "błąd konsoli strony", text=m.text[:300]))
    page.on("pageerror", lambda e: event(logging.DEBUG, "wyjątek na stronie", error=str(e)[:300]))
    page.on("requestfailed", lambda r: event(
        logging.DEBUG, "żądanie nieudane", url=r.url.split("?")[0][:200], error=r.failure))


def settle(page, timeout: int = SETTLE_TIMEOUT) -> None:
    """Daje stronie chwilę na doładowanie. Mój Orange nigdy nie przestaje całkiem odpytywać sieci,
    więc nie czekamy na pełne "networkidle" - o gotowości decydują konkretne selektory."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        event(logging.DEBUG, "settle: networkidle nie osiągnięty", timeout_ms=timeout)


def is_visible(page, selector: str, timeout: int = 5000) -> bool:
    try:
        page.wait_for_selector(selector, timeout=timeout)
        return True
    except PWTimeout:
        event(logging.DEBUG, "selektor niewidoczny", selector=selector, timeout_ms=timeout)
        return False


def is_logged_in(page, timeout: int = 120000) -> bool:
    """Czeka na znacznik zalogowania ALBO formularz logowania - co pojawi się pierwsze.
    Dzięki temu można dać długi limit czasu, a wynik i tak przychodzi od razu."""
    try:
        page.locator(sel("LOGGED_IN")).or_(page.locator(sel("LOGIN_INPUT"))).first.wait_for(timeout=timeout)
    except PWTimeout:
        return False
    return page.locator(sel("LOGGED_IN")).first.is_visible()


def check_session(browser) -> bool:
    """Otwiera Mój Orange z zapisaną sesją (bez odbierania rabatu) i sprawdza, czy jesteśmy zalogowani."""
    ctx = new_context(browser, with_state=True)
    block_heavy(ctx)
    try:
        page = ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        return is_logged_in(page)
    finally:
        ctx.close()


def setup() -> None:
    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        ctx = new_context(browser, with_state=False)
        page = ctx.new_page()
        page.goto(START_URL)
        input("Zaloguj się w oknie (z 2FA, zaznacz 'zapamiętaj urządzenie'), "
              "a potem naciśnij Enter tutaj...")
        save_state(ctx)
        browser.close()
    event(logging.INFO, "sesja zapisana", state_file=str(STATE_FILE))


def screenshot(page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        SCREENSHOT.chmod(0o600)
    except Exception as e:
        event(logging.WARNING, "nie udało się zrobić zrzutu", error=type(e).__name__)


def run() -> int:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not STATE_FILE.exists():
        event(logging.WARNING, "brak zapisanej sesji - zaloguj się przez orange_login.py",
              state_file=str(STATE_FILE))
    event(logging.INFO, "start", url=START_URL)

    with sync_playwright() as p:
        browser = launch_browser(p)
        ctx = new_context(browser, with_state=True)
        if LOW_RESOURCES:
            # Słabszy serwer: bez obrazków, fontów i mediów (zrzut w mailu będzie uboższy).
            block_heavy(ctx)
        page = ctx.new_page()
        if log.isEnabledFor(logging.DEBUG):
            debug_page(page)
            event(logging.DEBUG, "konfiguracja", low_resources=LOW_RESOURCES, reward_timeout_ms=REWARD_TIMEOUT,
                  settle_timeout_ms=SETTLE_TIMEOUT, state_file_exists=STATE_FILE.exists())
        # Baner cookies wyskakuje z losowym opóźnieniem - Playwright zamknie go sam,
        # gdy tylko zasłoni element, w który klikamy / wpisujemy.
        page.add_locator_handler(page.locator(sel("COOKIES")).first, lambda loc: loc.click())
        try:
            page.goto(START_URL, wait_until="domcontentloaded")

            # 1. Jeśli sesja wygasła, a urządzenie jest zapamiętane, wystarczy login + hasło
            if not is_logged_in(page):
                if not os.environ.get("ORANGE_LOGIN"):
                    event(logging.WARNING, "sesja wygasła, a ORANGE_LOGIN/ORANGE_PASSWORD nie są ustawione")
                elif page.locator(sel("LOGIN_INPUT")).first.is_visible():
                    event(logging.INFO, "sesja wygasła, logowanie loginem i hasłem")
                    page.fill(sel("LOGIN_INPUT"), os.environ["ORANGE_LOGIN"])
                    page.click(sel("SUBMIT"))
                    page.fill(sel("PASSWORD_INPUT"), os.environ["ORANGE_PASSWORD"])
                    page.click(sel("SUBMIT"))
                    settle(page)

            # 2. Dalej niezalogowany (np. Orange zażądał kodu SMS / 2FA) -> potrzebne ręczne logowanie
            if not is_visible(page, sel("LOGGED_IN"), 120000):
                screenshot(page)
                event(logging.ERROR, "wymagane ponowne logowanie (orange_login.py)",
                      result="login_required")
                send_mail(f"Orange rabat: wymaga logowania ({stamp})",
                          "Zapamiętanie urządzenia wygasło albo pojawiło się 2FA.\n"
                          "Zaloguj się ponownie: python3 orange_login.py "
                          "(Docker: docker compose up auto-morange-login)",
                          SCREENSHOT)
                return 2

            # Odświeżone ciasteczka zapisujemy od razu, żeby sesja żyła jak najdłużej.
            save_state(ctx)
            event(logging.INFO, "zalogowano")

            # 3. Strona usług pakietowych - tam powinien być widoczny rabat
            event(logging.INFO, "przechodzę na stronę rabatu", url=REWARD_URL)
            page.goto(REWARD_URL, wait_until="domcontentloaded")
            settle(page)
            if page.url.split("?")[0].rstrip("/") != REWARD_URL.rstrip("/"):
                event(logging.WARNING, "Orange przekierował na inną stronę", expected=REWARD_URL,
                      actual=page.url.split("?")[0])
            if sel("REWARD_BUTTON") and is_visible(page, sel("REWARD_BUTTON")):
                event(logging.INFO, "klikam przycisk rabatu")
                page.click(sel("REWARD_BUTTON"))
                settle(page)

            t0 = time.monotonic()
            ok = is_visible(page, sel("REWARD_DONE"), REWARD_TIMEOUT)
            event(logging.DEBUG, "oczekiwanie na rabat zakończone", found=ok,
                  waited_s=round(time.monotonic() - t0, 1), timeout_ms=REWARD_TIMEOUT)
            if ok:
                page.locator(sel("REWARD_DONE")).first.scroll_into_view_if_needed()
            screenshot(page)
            if ok:
                event(logging.INFO, "rabat widoczny", result="ok", url=page.url.split("?")[0])
                send_mail(f"Orange: Rabat za e-fakturę jest ✅ ({stamp})",
                          f"Zalogowano i znaleziono rabat na {REWARD_URL}.", SCREENSHOT)
                return 0
            event(logging.ERROR, "nie znaleziono rabatu na stronie", result="not_confirmed",
                  url=REWARD_URL, selector=sel("REWARD_DONE"))
            send_mail(f"Orange: brak rabatu ❌ ({stamp})",
                      f"Zalogowano, ale na {REWARD_URL} nie znaleziono rabatu "
                      f"({sel('REWARD_DONE')}). Sprawdź zrzut.", SCREENSHOT)
            return 1
        except Exception as e:
            screenshot(page)
            log.exception("błąd podczas odbioru rabatu", extra={"fields": {"result": "error"}})
            send_mail(f"Orange rabat: błąd ❌ ({stamp})", f"{type(e).__name__}: {e}", SCREENSHOT)
            return 1
        finally:
            browser.close()


def test_smtp() -> int:
    """Wysyła mail testowy (bez przeglądarki i logowania do Orange). 0 = wysłano, 1 = nie."""
    event(logging.INFO, "test SMTP", smtp_host=os.environ.get("SMTP_HOST") or None,
          smtp_port=os.environ.get("SMTP_PORT") or 465, mail_to=os.environ.get("MAIL_TO") or None)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if send_mail(f"Orange rabat: test maila ✅ ({stamp})",
                 "Konfiguracja SMTP działa - to jest wiadomość testowa."):
        return 0
    event(logging.ERROR, "test SMTP nieudany - szczegóły w logu powyżej", result="smtp_failed")
    return 1


if __name__ == "__main__":
    if "--test-smtp" in sys.argv:
        sys.exit(test_smtp())
    if "--setup" in sys.argv:
        setup()
    else:
        sys.exit(run())
