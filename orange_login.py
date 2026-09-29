#!/usr/bin/env python3
"""
Panel WWW do zalogowania się w Mój Orange i zapisania ustawień - bez okna przeglądarki na serwerze.

Przeglądarka (headless Chromium, ta sama konfiguracja co w orange_rabat.py) działa na serwerze.
Jej obraz jest przesyłany jako strumień MJPEG (Chromium wysyła klatkę tylko, gdy coś się zmieni),
a panel przekazuje do niej kliknięcia i klawiaturę. Po zalogowaniu sesja trafia do
DATA_DIR/state.json, a ustawienia (hasła, SMTP, selektory) do DATA_DIR/config.env.
Tylko biblioteka standardowa Pythona + Playwright.

Użycie:
  python3 orange_login.py [--host 127.0.0.1] [--port 8080]

Adres z jednorazowym tokenem pojawia się w logu przy starcie. Panel domyślnie słucha tylko na
127.0.0.1 - z innego komputera użyj tunelu SSH:  ssh -L 8080:127.0.0.1:8080 serwer
Zmienne: LOGIN_HOST, LOGIN_PORT, LOGIN_TOKEN (stały token zamiast losowego),
         LOGIN_PUBLIC_URL (adres pokazywany w logu), LOGIN_IDLE_MINUTES (auto-wyłączenie, 0 = nigdy),
         LOGIN_FPS (maks. klatek/s podglądu, domyślnie 3).
"""
import os
import hmac
import json
import time
import queue
import base64
import logging
import secrets
import argparse
import signal
import threading
from concurrent.futures import Future
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright

import orange_rabat as core
from orange_rabat import event

COOKIE = "am_token"
MAX_BODY = 64 * 1024
FPS = max(1.0, min(30.0, float(os.environ.get("LOGIN_FPS", "3"))))
ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "Space",
                "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"}

# (klucz, etykieta, typ pola, grupa)
CONFIG_FIELDS = [
    ("ORANGE_LOGIN", "Login (e-mail lub telefon)", "text", "orange"),
    ("ORANGE_PASSWORD", "Hasło", "password", "orange"),
    ("SMTP_HOST", "Serwer SMTP", "text", "mail"),
    ("SMTP_PORT", "Port (465 = SSL, 587 = STARTTLS)", "number", "mail"),
    ("SMTP_USER", "Użytkownik SMTP", "text", "mail"),
    ("SMTP_PASS", "Hasło SMTP (Gmail: hasło aplikacji)", "password", "mail"),
    ("MAIL_TO", "Wysyłaj do", "email", "mail"),
    ("MAIL_FROM", "Nadawca (domyślnie użytkownik SMTP)", "email", "mail"),
] + [(f"ORANGE_SEL_{name}", name, "text", "selectors") for name in core.SELECTORS]
FIELD_TYPES = {key: ftype for key, _, ftype, _ in CONFIG_FIELDS}


def read_config() -> dict[str, str]:
    values = {}
    if core.CONFIG_FILE.is_file():
        for line in core.CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def write_config(values: dict[str, str]) -> None:
    body = "# Zapisane przez orange_login.py - POUFNE, nie commitować.\n"
    body += "".join(f"{k}={v}\n" for k, v in values.items() if v)
    core.write_private(core.CONFIG_FILE, lambda tmp: tmp.write_text(body, encoding="utf-8"))
    # Od razu stosujemy w tym procesie (np. dla testu maila); klucze z .env / środowiska wygrywają.
    for key in FIELD_TYPES:
        if key in core.ENV_OVERRIDES:
            continue
        if values.get(key):
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)


class RemoteBrowser:
    """Headless Chromium obsługiwany z jednego wątku (API sync Playwrighta nie jest wielowątkowe).

    Wątek przeglądarki wykonuje polecenia z kolejki, a w wolnych chwilach "pompuje" zdarzenia
    Playwrighta, dzięki czemu docierają klatki screencastu. Klatki są potwierdzane (ack) tylko,
    gdy ktoś ogląda podgląd - bez widzów Chromium przestaje je generować.
    """

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._cond = threading.Condition()
        self._frame, self._frame_id = b"", 0
        self._viewers = 0
        self._paused = False                    # podgląd wstrzymany na czas testu sesji (oszczędza CPU)
        self._pending_ack = None
        self._cdp = self._cdp_page = None
        self.running = True
        started: Future = Future()
        self._thread = threading.Thread(target=self._loop, args=(started,), name="browser", daemon=True)
        self._thread.start()
        started.result(timeout=120)

    # --- wątek przeglądarki -------------------------------------------------------
    def _loop(self, started: Future):
        try:
            self._start()
        except BaseException as e:
            started.set_exception(e)
            return
        started.set_result(None)
        while self.running:
            try:
                fn, args, fut = self._q.get_nowait()
            except queue.Empty:
                self._flush_ack()
                try:
                    self._active().wait_for_timeout(100)  # pompuje zdarzenia (klatki, popupy)
                except Exception:
                    time.sleep(0.1)
                continue
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except BaseException as e:
                    fut.set_exception(e)
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass

    def _start(self):
        self._pw = sync_playwright().start()
        self._browser = core.launch_browser(self._pw)
        self._ctx = core.new_context(self._browser, with_state=True)
        self._ctx.set_default_timeout(30000)
        self._ctx.on("page", self._on_page)
        self._page = self._ctx.new_page()
        self._attach(self._page)
        self._page.goto(core.START_URL, wait_until="domcontentloaded")

    def _on_page(self, page):
        page.on("dialog", lambda d: d.accept())
        self._page = page                       # nowe okno (np. popup logowania) staje się aktywne
        self._attach(page)

    def _attach(self, page):
        if self._cdp_page is page:
            return
        if self._cdp:
            try:
                self._cdp.send("Page.stopScreencast")
                self._cdp.detach()
            except Exception:
                pass
        cdp = self._ctx.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda params: self._on_frame(cdp, params))
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 60,
            "maxWidth": core.VIEWPORT["width"], "maxHeight": core.VIEWPORT["height"],
            "everyNthFrame": max(1, round(60 / FPS)),   # limit klatek przy animacjach na stronie
        })
        self._cdp, self._cdp_page, self._pending_ack = cdp, page, None

    def _on_frame(self, cdp, params):
        with self._cond:
            self._frame = base64.b64decode(params["data"])
            self._frame_id += 1
            self._cond.notify_all()
            viewers = self._viewers
        if viewers and not self._paused:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        else:
            self._pending_ack = (cdp, params["sessionId"])

    def _flush_ack(self):
        if self._pending_ack and self._viewers and not self._paused:
            cdp, sid = self._pending_ack
            self._pending_ack = None
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass

    def _active(self):
        if self._page.is_closed():
            pages = self._ctx.pages
            self._page = pages[-1] if pages else self._ctx.new_page()
            self._attach(self._page)
        return self._page

    # --- API dla wątków HTTP -------------------------------------------------------
    def call(self, fn, *args, timeout: float = 90):
        if not self.running:
            raise RuntimeError("przeglądarka jest zamknięta")
        fut: Future = Future()
        self._q.put((fn, args, fut))
        return fut.result(timeout=timeout)

    def viewer(self, delta: int):
        with self._cond:
            self._viewers += delta

    def wait_frame(self, last_id: int, timeout: float) -> tuple[bytes, int]:
        with self._cond:
            self._cond.wait_for(lambda: self._frame_id != last_id or not self.running, timeout)
            return self._frame, self._frame_id

    def stop(self):
        self.running = False
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=20)

    # --- polecenia (wykonywane w wątku przeglądarki) ------------------------------------
    def click(self, x: float, y: float):
        self._active().mouse.click(x, y)

    def type(self, text: str):
        self._active().keyboard.type(text)

    def key(self, key: str):
        self._active().keyboard.press(" " if key == "Space" else key)

    def scroll(self, dy: float):
        self._active().mouse.wheel(0, dy)

    def nav(self, action: str):
        page = self._active()
        if action == "home":
            page.goto(core.START_URL, wait_until="domcontentloaded")
        elif action == "back":
            page.go_back(wait_until="domcontentloaded")
        elif action == "reload":
            page.reload(wait_until="domcontentloaded")

    def status(self) -> dict:
        page = self._active()
        try:
            logged_in = page.locator(core.sel("LOGGED_IN")).first.is_visible()
        except Exception:
            logged_in = False
        return {"url": page.url.split("?")[0], "logged_in": logged_in}

    def save(self):
        core.save_state(self._ctx)

    def check(self) -> bool:
        self._paused = True
        try:
            return core.check_session(self._browser)
        finally:
            self._paused = False


class App:
    def __init__(self, token: str, idle_minutes: float):
        self.token = token
        self.idle_seconds = idle_minutes * 60
        self.last_activity = time.monotonic()
        self.browser = RemoteBrowser()
        self.httpd: ThreadingHTTPServer | None = None
        self.was_logged_in = False
        self.last_check: dict | None = None
        self._stopping = threading.Lock()

    def touch(self):
        self.last_activity = time.monotonic()

    def shutdown(self, reason: str):
        if not self._stopping.acquire(blocking=False):
            return
        event(logging.INFO, "zamykanie panelu logowania", reason=reason)
        threading.Thread(target=self._stop, daemon=True).start()

    def _stop(self):
        self.browser.stop()
        self.httpd.shutdown()

    def watchdog(self):
        while self.idle_seconds > 0:
            time.sleep(30)
            if time.monotonic() - self.last_activity > self.idle_seconds:
                self.shutdown("brak aktywności")
                return

    def status(self) -> dict:
        b = self.browser
        status = b.call(b.status)
        # Automatyczny zapis sesji w chwili zalogowania - nie trzeba pamiętać o przycisku.
        if status["logged_in"] and not self.was_logged_in:
            b.call(b.save)
            event(logging.INFO, "zalogowano - sesja zapisana automatycznie", state_file=str(core.STATE_FILE))
        self.was_logged_in = status["logged_in"]
        state = core.STATE_FILE
        status["state_saved_at"] = (time.strftime("%d.%m %H:%M", time.localtime(state.stat().st_mtime))
                                    if state.exists() else None)
        status["last_check"] = self.last_check
        cfg = {k: bool(os.environ.get(k)) for k in ("ORANGE_LOGIN", "ORANGE_PASSWORD", "SMTP_HOST")}
        status["relogin_ready"] = cfg["ORANGE_LOGIN"] and cfg["ORANGE_PASSWORD"]
        status["mail_ready"] = cfg["SMTP_HOST"]
        return status

    def config_view(self) -> list[dict]:
        saved = read_config()
        fields = []
        for key, label, ftype, group in CONFIG_FIELDS:
            value = os.environ.get(key, "") if key in core.ENV_OVERRIDES else saved.get(key, "")
            fields.append({
                "key": key, "label": label, "type": ftype, "group": group,
                "value": "" if ftype == "password" else value,   # hasła nigdy nie wracają do przeglądarki
                "is_set": bool(value),
                "overridden": key in core.ENV_OVERRIDES,
                "placeholder": core.SELECTORS.get(key.removeprefix("ORANGE_SEL_"), ""),
            })
        return fields

    def save_config(self, incoming: dict) -> None:
        values = read_config()
        for key, value in incoming.items():
            if key not in FIELD_TYPES or key in core.ENV_OVERRIDES:
                continue
            if not isinstance(value, str) or len(value) > 500 or "\n" in value or "\r" in value:
                raise ValueError(f"niepoprawna wartość pola {key}")
            value = value.strip()
            if FIELD_TYPES[key] == "password" and value == "":
                continue                        # puste pole hasła = bez zmian
            if FIELD_TYPES[key] == "number" and value and not value.isdigit():
                raise ValueError(f"{key} musi być liczbą")
            values[key] = value
        for key in incoming.get("_clear", []):
            if key in FIELD_TYPES and key not in core.ENV_OVERRIDES:
                values.pop(key, None)
        write_config(values)
        event(logging.INFO, "ustawienia zapisane", config_file=str(core.CONFIG_FILE))


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self'; frame-ancestors 'none'",
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "auto-morange"
        sys_version = ""

        def log_message(self, *args):          # domyślny log zawiera URL z tokenem - wyłączony
            pass

        # --- odpowiedzi --------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _authed(self) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie[COOKIE].value if COOKIE in cookie else ""
            return hmac.compare_digest(value.encode(), app.token.encode())

        def _stream(self):
            """Podgląd ekranu jako MJPEG - przeglądarka wyświetla go natywnie w <img>."""
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            b = app.browser
            b.viewer(+1)
            last = -1
            try:
                while b.running:
                    frame, last = b.wait_frame(last, timeout=15)   # co 15 s powtórka = keepalive
                    if frame:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                        self.wfile.flush()
            except OSError:
                pass                            # widz zamknął kartę
            finally:
                b.viewer(-1)

        # --- GET ---------------------------------------------------------------
        def do_GET(self):
            url = urlsplit(self.path)
            token = parse_qs(url.query).get("token", [""])[0]
            if url.path == "/" and token:
                if hmac.compare_digest(token.encode(), app.token.encode()):
                    # token przenosimy do ciasteczka i usuwamy z paska adresu / historii
                    self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={app.token}; HttpOnly; SameSite=Strict; Path=/",
                    })
                    event(logging.INFO, "otwarto panel", client=self.client_address[0])
                    return
                event(logging.WARNING, "niepoprawny token", client=self.client_address[0])
            if not self._authed():
                self._send(HTTPStatus.UNAUTHORIZED, UNAUTHORIZED_HTML.encode(), "text/html; charset=utf-8")
                return
            if url.path == "/stream.mjpg":
                self._stream()
                return
            app.touch()
            if url.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif url.path == "/app.js":
                self._send(200, APP_JS.encode(), "text/javascript; charset=utf-8")
            elif url.path == "/app.css":
                self._send(200, APP_CSS.encode(), "text/css; charset=utf-8")
            elif url.path == "/api/status":
                self._json(app.status())
            elif url.path == "/api/config":
                self._json({"fields": app.config_view(), "config_file": str(core.CONFIG_FILE)})
            else:
                self._json({"error": "nie znaleziono"}, 404)

        # --- POST --------------------------------------------------------------
        def do_POST(self):
            # Własny nagłówek wymusza preflight CORS, więc obca strona nie wyśle takiego żądania (CSRF).
            if not self._authed() or self.headers.get("X-Requested-With") != "auto-morange":
                self._json({"error": "brak autoryzacji"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._json({"error": "za duże żądanie"}, 413)
                return
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json({"error": "niepoprawny JSON"}, 400)
                return
            app.touch()
            b = app.browser
            path = urlsplit(self.path).path
            try:
                if path == "/api/click":
                    x, y = float(data["x"]), float(data["y"])
                    if not (0 <= x <= core.VIEWPORT["width"] and 0 <= y <= core.VIEWPORT["height"]):
                        raise ValueError("kliknięcie poza ekranem")
                    b.call(b.click, x, y)
                elif path == "/api/type":
                    b.call(b.type, str(data.get("text", ""))[:1000])
                elif path == "/api/key":
                    if data.get("key") not in ALLOWED_KEYS:
                        raise ValueError("niedozwolony klawisz")
                    b.call(b.key, data["key"])
                elif path == "/api/scroll":
                    b.call(b.scroll, max(-3000.0, min(3000.0, float(data.get("dy", 0)))))
                elif path == "/api/nav":
                    if data.get("action") not in ("home", "back", "reload"):
                        raise ValueError("nieznana akcja")
                    b.call(b.nav, data["action"])
                elif path == "/api/save-session":
                    b.call(b.save)
                    event(logging.INFO, "sesja zapisana", state_file=str(core.STATE_FILE))
                elif path == "/api/check-session":
                    ok = b.call(b.check, timeout=300)
                    app.last_check = {"ok": ok, "at": time.strftime("%H:%M")}
                    event(logging.INFO, "test sesji", logged_in=ok)
                    self._json({"ok": True, "logged_in": ok})
                    return
                elif path == "/api/config":
                    app.save_config(data)
                elif path == "/api/test-mail":
                    missing = [k for k in ("SMTP_HOST", "MAIL_TO") if not os.environ.get(k)]
                    if not (os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER")):
                        missing.append("SMTP_USER lub MAIL_FROM")
                    if missing:
                        raise ValueError("najpierw ustaw i zapisz: " + ", ".join(missing))
                    if not core.send_mail("Orange rabat: test maila ✅",
                                          "Konfiguracja SMTP działa - to jest wiadomość testowa."):
                        raise ValueError("wysyłka nie powiodła się - szczegóły w logu")
                elif path == "/api/shutdown":
                    self._json({"ok": True})
                    app.shutdown("na żądanie z panelu")
                    return
                else:
                    self._json({"error": "nie znaleziono"}, 404)
                    return
                self._json({"ok": True})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"error": str(e)}, 400)
            except PermissionError:
                event(logging.ERROR, "brak uprawnień do zapisu", path=path, data_dir=str(core.DATA_DIR))
                self._json({"error": f"brak uprawnień do zapisu w {core.DATA_DIR} "
                                     "(Docker: sudo chown -R 1000:1000 data)"}, 500)
            except Exception as e:
                event(logging.ERROR, "błąd akcji w panelu", path=path, error=f"{type(e).__name__}: {e}")
                self._json({"error": f"{type(e).__name__} - szczegóły w logu"}, 500)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("LOGIN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOGIN_PORT", "8080")))
    args = parser.parse_args()

    token = os.environ.get("LOGIN_TOKEN") or secrets.token_urlsafe(24)
    idle = float(os.environ.get("LOGIN_IDLE_MINUTES", "30"))
    core.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    event(logging.INFO, "uruchamiam przeglądarkę")
    app = App(token, idle)
    app.httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    app.httpd.daemon_threads = True
    threading.Thread(target=app.watchdog, daemon=True).start()

    base = os.environ.get("LOGIN_PUBLIC_URL") or f"http://{args.host}:{args.port}"
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        event(logging.WARNING, "panel słucha na wszystkich interfejsach - wystawiaj go tylko przez "
                               "localhost / tunel SSH / reverse proxy z HTTPS", host=args.host)
    event(logging.INFO, "panel logowania gotowy - otwórz adres w przeglądarce",
          url=f"{base.rstrip('/')}/?token={token}", idle_shutdown_minutes=idle)
    # W kontenerze proces ma PID 1 i domyślnie ignoruje SIGTERM (docker stop) - obsługujemy go jawnie.
    signal.signal(signal.SIGTERM, lambda *_: app.shutdown("SIGTERM"))
    try:
        app.httpd.serve_forever()
    except KeyboardInterrupt:
        app.shutdown("Ctrl+C")
        app.browser.stop()
    finally:
        app.httpd.server_close()
        event(logging.INFO, "panel zamknięty")


UNAUTHORIZED_HTML = """<!doctype html><html lang="pl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>auto-morange</title>
<body style="font-family:system-ui,sans-serif;max-width:560px;margin:10vh auto;padding:0 16px">
<h1>Brak dostępu</h1><p>Otwórz pełny adres z tokenem, który panel wypisał w logu przy starcie
(<code>…/?token=…</code>).</p></body></html>"""

INDEX_HTML = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logowanie · auto-morange</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header>
  <div class="brand"><span class="logo"></span>auto-morange</div>
  <span id="pill" class="pill">łączenie…</span>
  <button id="shutdown" class="ghost">Zakończ</button>
</header>

<main>
  <section class="browser">
    <div class="bar">
      <button data-nav="back" title="Wstecz">←</button>
      <button data-nav="reload" title="Odśwież">↻</button>
      <button data-nav="home" title="Mój Orange">⌂</button>
      <div id="url" class="url">…</div>
    </div>
    <div id="screenWrap" tabindex="0">
      <img id="screen" src="/stream.mjpg" alt="Podgląd przeglądarki" draggable="false">
      <div id="kbd" class="kbd">⌨ klawiatura aktywna</div>
    </div>
    <div class="typebar">
      <input id="typeBox" placeholder="Albo wpisz tutaj i naciśnij Enter…" autocomplete="off">
      <button data-key="Tab">Tab</button>
      <button data-key="Backspace">⌫</button>
      <button data-key="Enter">Enter ↵</button>
    </div>
  </section>

  <aside>
    <ol class="steps">
      <li id="s1">
        <b>Zaloguj się</b>
        <p>Klikaj w obraz i pisz z klawiatury. Przy kodzie SMS zaznacz <i>„Zapamiętaj to urządzenie”</i>.</p>
      </li>
      <li id="s2">
        <b>Sesja zapisana</b>
        <p id="s2txt">Zapisze się sama po zalogowaniu.</p>
      </li>
      <li id="s3">
        <b>Sprawdź</b>
        <p id="s3txt">Otwiera Mój Orange w czystym oknie z zapisaną sesją.</p>
        <button id="check">Sprawdź sesję</button>
      </li>
      <li id="s4">
        <b>Automatyczne ponowne logowanie</b>
        <p id="s4txt">Sesja Orange wygasa po kilku godzinach – co miesiąc skrypt loguje się hasłem
          (bez SMS dzięki zapamiętanemu urządzeniu).</p>
        <button data-open="orange">Ustaw login i hasło</button>
      </li>
    </ol>

    <details id="settings">
      <summary>Ustawienia</summary>
      <form id="cfg" autocomplete="off"></form>
      <div class="row">
        <button id="saveCfg" class="primary">Zapisz</button>
        <button id="testMail">Test maila</button>
      </div>
      <p class="muted small">Hasła nie są wyświetlane – puste pole = bez zmian. Plik: <code id="cfgFile"></code></p>
    </details>
  </aside>
</main>
<div id="toast"></div>
<script src="/app.js"></script>
</body>
</html>"""

APP_CSS = """
:root{--bg:#f4f4f2;--card:#fff;--fg:#1b1b19;--muted:#6d6d67;--line:#e2e2dc;--accent:#ff7900;--ok:#16874a;--err:#c4382b;--r:10px}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1d1d1b;--fg:#ecece8;--muted:#9d9d96;--line:#33332f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.5;cursor:progress}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.ghost{background:transparent}
input{font:inherit;width:100%;padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
code{font-size:12px}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--line)}
.brand{font-weight:700;display:flex;align-items:center;gap:8px;margin-right:auto}
.logo{width:18px;height:18px;background:var(--accent);border-radius:3px}
.pill{font-size:13px;padding:3px 10px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.pill.ok{color:var(--ok);border-color:var(--ok)}
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px;max-width:1700px;margin:auto}
@media (max-width:1000px){main{grid-template-columns:1fr}}
.browser{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.bar{display:flex;gap:6px;align-items:center;padding:8px;border-bottom:1px solid var(--line)}
.bar button{padding:4px 10px}
.url{flex:1;min-width:0;padding:5px 10px;border-radius:99px;background:var(--bg);color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#screenWrap{position:relative;line-height:0;outline:none;background:#000;aspect-ratio:1366/900}
#screen{width:100%;height:100%;object-fit:contain;cursor:pointer;user-select:none}
#screenWrap:focus{box-shadow:inset 0 0 0 3px var(--accent)}
.kbd{position:absolute;right:10px;bottom:10px;line-height:1.4;font-size:12px;padding:3px 8px;border-radius:6px;background:var(--accent);color:#fff;display:none}
#screenWrap:focus .kbd{display:block}
.typebar{display:flex;gap:6px;padding:8px;border-top:1px solid var(--line)}
aside{display:flex;flex-direction:column;gap:16px}
.steps{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:var(--r);counter-reset:s}
.steps li{position:relative;padding:14px 14px 14px 52px;border-bottom:1px solid var(--line);counter-increment:s}
.steps li:last-child{border-bottom:0}
.steps li::before{content:counter(s);position:absolute;left:14px;top:13px;width:26px;height:26px;border-radius:50%;border:2px solid var(--line);display:grid;place-items:center;font-size:13px;font-weight:700;color:var(--muted)}
.steps li.done::before{content:"✓";background:var(--ok);border-color:var(--ok);color:#fff}
.steps li.fail::before{content:"!";background:var(--err);border-color:var(--err);color:#fff}
.steps p{margin:3px 0 8px;color:var(--muted);font-size:14px}
.steps button{padding:5px 10px;font-size:14px}
details{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px}
summary{cursor:pointer;font-weight:600}
fieldset{border:0;padding:0;margin:12px 0 0}
legend{font-weight:600;font-size:14px;padding:0}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 3px}
.row{display:flex;gap:6px;margin-top:12px}
.muted{color:var(--muted)}.small{font-size:12px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);padding:10px 16px;border-radius:8px;background:var(--fg);color:var(--bg);display:none;max-width:90vw;z-index:9}
#toast.err{background:var(--err);color:#fff}
"""

APP_JS = r"""
const W = 1366, H = 900;
const HDR = {'Content-Type': 'application/json', 'X-Requested-With': 'auto-morange'};
const $ = (s) => document.querySelector(s);
const screen = $('#screen'), wrap = $('#screenWrap');
let stopped = false;

function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = err ? 'err' : ''; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', err ? 6000 : 3000);
}
async function api(path, body) {
  const r = await fetch(path, {method: 'POST', headers: HDR, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
// Akcje idą po kolei, żeby tekst i klawisze nie zamieniły się miejscami.
let queue = Promise.resolve();
const send = (path, body) => (queue = queue.then(() => api(path, body)).catch(e => toast(e.message, true)));
async function busy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

// --- podgląd (MJPEG); gdy karta jest ukryta, zamykamy strumień, żeby serwer odpoczął ---
const STREAM = '/stream.mjpg';
screen.onerror = () => { if (!stopped) setTimeout(() => screen.src = STREAM + '?' + Date.now(), 2000); };
document.addEventListener('visibilitychange', () => {
  if (stopped) return;
  if (document.hidden) screen.removeAttribute('src'); else screen.src = STREAM + '?' + Date.now();
});

// --- mysz ---
screen.addEventListener('click', (e) => {
  const r = screen.getBoundingClientRect();
  send('/api/click', {x: (e.clientX - r.left) * W / r.width, y: (e.clientY - r.top) * H / r.height});
  wrap.focus();
});
let dy = 0;
screen.addEventListener('wheel', (e) => {
  e.preventDefault();
  if (!dy) setTimeout(() => { send('/api/scroll', {dy}); dy = 0; }, 100);
  dy += e.deltaY;
}, {passive: false});

// --- klawiatura: gdy podgląd ma fokus, wszystko idzie do strony ---
const SPECIAL = new Set(['Enter','Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft',
                         'ArrowRight','Home','End','PageUp','PageDown']);
let buf = '', bufT;
function flush() { clearTimeout(bufT); if (buf) { send('/api/type', {text: buf}); buf = ''; } }
wrap.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') return;   // obsłuży "paste"
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key.length === 1) { e.preventDefault(); buf += e.key; clearTimeout(bufT); bufT = setTimeout(flush, 120); }
  else if (SPECIAL.has(e.key)) { e.preventDefault(); flush(); send('/api/key', {key: e.key}); }
});
wrap.addEventListener('paste', (e) => {
  e.preventDefault(); flush();
  const text = e.clipboardData.getData('text'); if (text) send('/api/type', {text});
});
$('#typeBox').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const v = e.target.value; e.target.value = '';
  if (v) send('/api/type', {text: v});
  send('/api/key', {key: 'Enter'});
});
document.querySelectorAll('[data-key]').forEach(b => b.onclick = () => send('/api/key', {key: b.dataset.key}));
document.querySelectorAll('[data-nav]').forEach(b => b.onclick = () => send('/api/nav', {action: b.dataset.nav}));

// --- status i kroki ---
const mark = (id, state) => { $(id).classList.toggle('done', state === true); $(id).classList.toggle('fail', state === false); };
async function refreshStatus() {
  if (stopped || document.hidden) return;
  try {
    const s = await (await fetch('/api/status')).json();
    $('#url').textContent = s.url;
    $('#pill').textContent = s.logged_in ? 'zalogowano' : 'niezalogowano';
    $('#pill').className = 'pill' + (s.logged_in ? ' ok' : '');
    mark('#s1', s.logged_in || null);
    mark('#s2', s.state_saved_at ? true : null);
    $('#s2txt').textContent = s.state_saved_at ? 'Zapisano ' + s.state_saved_at + '.' : 'Zapisze się sama po zalogowaniu.';
    if (s.last_check) {
      mark('#s3', s.last_check.ok);
      $('#s3txt').textContent = s.last_check.ok ? 'Zapisana sesja działa (' + s.last_check.at + ').'
        : 'Zapisana sesja nie jest zalogowana (' + s.last_check.at + ') – zaloguj się ponownie albo popraw selektor LOGGED_IN.';
    }
    mark('#s4', s.relogin_ready || null);
    if (s.relogin_ready) $('#s4txt').textContent = 'Login i hasło zapisane – skrypt sam odnowi sesję.';
  } catch (e) {}
}
setInterval(refreshStatus, 2000);
refreshStatus();

$('#check').onclick = (e) => busy(e.target, async () => {
  toast('Sprawdzam… na słabym serwerze może to potrwać 1–2 min', false);
  await api('/api/check-session'); refreshStatus();
});

// --- ustawienia ---
const LEGEND = {orange: 'Konto Orange', mail: 'Mail z wynikiem (opcjonalnie)', selectors: 'Selektory strony (zaawansowane)'};
const el = (tag, props, ...kids) => { const n = Object.assign(document.createElement(tag), props || {}); n.append(...kids); return n; };
async function loadConfig() {
  const j = await (await fetch('/api/config')).json();
  $('#cfgFile').textContent = j.config_file;
  const sets = {};
  for (const f of j.fields) {
    sets[f.group] ||= el('fieldset', {id: 'g-' + f.group}, el('legend', {textContent: LEGEND[f.group]}));
    let note = f.overridden ? ' · z .env (ma pierwszeństwo)' : (f.type === 'password' && f.is_set ? ' · ustawione' : '');
    sets[f.group].append(el('label', {textContent: f.label + note}), el('input', {
      name: f.key, type: f.type, value: f.value, disabled: f.overridden,
      placeholder: f.placeholder || (f.type === 'password' && f.is_set ? '•••••••• (bez zmian)' : ''),
      autocomplete: f.type === 'password' ? 'new-password' : 'off'}));
  }
  $('#cfg').replaceChildren(...Object.values(sets));
}
document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => {
  $('#settings').open = true;
  const g = $('#g-' + b.dataset.open); g.scrollIntoView({behavior: 'smooth'}); g.querySelector('input')?.focus();
});
$('#saveCfg').onclick = (e) => busy(e.target, async () => {
  const values = {_clear: []};
  for (const i of $('#cfg').querySelectorAll('input:not([disabled])')) {
    if (i.type === 'password') { if (i.value) values[i.name] = i.value; }
    else if (i.value.trim()) values[i.name] = i.value; else values._clear.push(i.name);
  }
  await api('/api/config', values); toast('Zapisano'); loadConfig(); refreshStatus();
});
$('#testMail').onclick = (e) => busy(e.target, async () => { await api('/api/test-mail'); toast('Mail testowy wysłany'); });
$('#shutdown').onclick = (e) => busy(e.target, async () => {
  if (!confirm('Wyłączyć panel?')) return;
  stopped = true; screen.removeAttribute('src');
  await api('/api/shutdown');
  document.body.replaceChildren(el('p', {textContent: 'Panel wyłączony – możesz zamknąć kartę.', style: 'padding:24px'}));
});
loadConfig();
"""

if __name__ == "__main__":
    main()
