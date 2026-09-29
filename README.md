<div align="center">

# auto-morange

**Automatyczny comiesięczny odbiór rabatu w Mój Orange**

[![Docker image](https://github.com/pprazzi99/auto-morange/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/pprazzi99/auto-morange/actions/workflows/docker-publish.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-pprazzi99%2Fauto--morange-2496ED?logo=docker&logoColor=white)](https://github.com/pprazzi99/auto-morange/pkgs/container/auto-morange)
![Platformy](https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-555)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/playwright-1.63-2EAD33?logo=playwright&logoColor=white)
[![Licencja](https://img.shields.io/badge/licencja-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)

</div>

auto-morange raz w miesiącu loguje się do Mój Orange przeglądarką headless, sprawdza, czy
rabat jest naliczony, i wysyła ci maila z wynikiem i zrzutem ekranu. Pierwsze logowanie
(z kodem SMS) robisz raz, w panelu WWW, który pokazuje ekran przeglądarki działającej na serwerze.
Serwer nie potrzebuje do tego ekranu.

- **Działa bez nadzoru.** Wbudowany harmonogram (domyślnie 3. dnia miesiąca o 9:00), a gdy sesja wygaśnie, skrypt sam loguje się ponownie.
- **Panel logowania w przeglądarce.** Działa na serwerze bez GUI, także przez tunel SSH.
- **Gotowy obraz Dockera.** `linux/amd64` i `linux/arm64` (np. Raspberry Pi), bez roota, z systemem plików tylko do odczytu.
- **Logi JSON i powiadomienia e-mail.** Czytelne kody wyjścia i zrzut ekranu przy każdym przebiegu.

> [!NOTE]
> Projekt nie jest powiązany z Orange Polska S.A. Działa na twoim koncie i twoich danych
> logowania; korzystasz z niego na własną odpowiedzialność.

## Spis treści

- [Szybki start (Docker)](#szybki-start-docker)
- [Jak to działa](#jak-to-działa)
- [Konfiguracja](#konfiguracja)
- [Wyniki i logi](#wyniki-i-logi)
- [Obraz Dockera i aktualizacje](#obraz-dockera-i-aktualizacje)
- [Bezpieczeństwo](#bezpieczeństwo)
- [Rozwój](#rozwój)
- [Licencja](#licencja)

## Szybki start (Docker)

Wymagania: Docker z Compose v2.24+, ok. 1,5 GB miejsca na obraz, ok. 1 GB RAM na czas przebiegu (ok. 30 s).
Nie musisz klonować repozytorium, wystarczy jeden plik:

```bash
mkdir auto-morange && cd auto-morange
curl -fsSLO https://raw.githubusercontent.com/pprazzi99/auto-morange/main/docker-compose.yaml
```

**1. Zaloguj się** (jednorazowo i za każdym razem, gdy Orange zażąda kodu SMS):

```bash
docker compose up auto-morange-login
```

W logu pojawi się adres `http://127.0.0.1:8080/?token=…`. Na serwerze bez ekranu najpierw
zestaw tunel ze swojego komputera: `ssh -L 8080:127.0.0.1:8080 użytkownik@serwer`.
W panelu zaloguj się do Mój Orange, zaznacz **„Zapamiętaj to urządzenie”**, a w **Ustawieniach**
wpisz login i hasło (do automatycznego ponownego logowania) i opcjonalnie SMTP. Na koniec kliknij **Zakończ**.

**2. Uruchom harmonogram:**

```bash
docker compose up -d
docker compose logs -f auto-morange
```

**Test od razu**, bez czekania na harmonogram:

```bash
docker compose run --rm auto-morange run
```

Samą konfigurację SMTP sprawdzisz bez logowania do Orange (bez Dockera: `python orange_rabat.py --test-smtp`):

```bash
docker compose run --rm auto-morange test-smtp
```

Instrukcja krok po kroku jest w [INSTRUKCJA_SETUP.md](INSTRUKCJA_SETUP.md).

<details>
<summary>Bez Compose, samym <code>docker run</code></summary>

```bash
docker volume create auto-morange-data

# logowanie (panel)
docker run --rm -it -p 127.0.0.1:8080:8080 --shm-size=256m \
  -e LOGIN_PUBLIC_URL=http://127.0.0.1:8080 -v auto-morange-data:/data ghcr.io/pprazzi99/auto-morange:latest login

# harmonogram
docker run -d --name auto-morange --restart unless-stopped --shm-size=256m --init \
  --read-only --tmpfs /tmp --tmpfs /home/app:uid=1000,gid=1000 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -v auto-morange-data:/data ghcr.io/pprazzi99/auto-morange:latest
```

</details>

<details>
<summary>Bez Dockera (niezalecane), z systemowym cronem</summary>

Skrypty działają też bez kontenera, ale **nie jest to zalecane**: musisz sam utrzymywać Pythona,
Playwrighta i systemowe biblioteki Chromium, a przebieg nie jest odizolowany od reszty systemu
(w Dockerze działa bez roota, z systemem plików tylko do odczytu i limitami zasobów).
Wymagania: Python 3.10+, ok. 500 MB na Chromium.

```bash
git clone https://github.com/pprazzi99/auto-morange.git && cd auto-morange
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install-deps chromium   # biblioteki systemowe
.venv/bin/playwright install --no-shell chromium

.venv/bin/python orange_login.py                  # logowanie (panel), dane trafiają do ./data
```

**Harmonogram (cronjob).** Skrypt [auto-morange-cron.sh](auto-morange-cron.sh) dodaje wpis do
twojego crontabu (nie rusza pozostałych wpisów):

```bash
./auto-morange-cron.sh install               # 3. dnia miesiąca o 9:00
./auto-morange-cron.sh install "0 8 5 * *"   # własny termin (5 pól cron)
./auto-morange-cron.sh status                # pokaż wpis
./auto-morange-cron.sh run                   # test: jeden przebieg teraz
./auto-morange-cron.sh remove                # usuń wpis
```

Wynik `install` to zwykły wpis, który możesz też dodać ręcznie przez `crontab -e`:

```
0 9 3 * * cd /ścieżka/do/auto-morange && LOW_RESOURCES=1 .venv/bin/python orange_rabat.py >> data/orange.log 2>&1
```

Systemowy cron działa w strefie czasowej serwera, a logi (JSON) trafiają do `data/orange.log`.

</details>

## Jak to działa

```
            ┌──────────────────────────┐   jednorazowo / gdy sesja wygaśnie
  Ty ──────▶│ orange_login.py          │   panel WWW: widzisz ekran przeglądarki
 (przeglą-  │ panel logowania          │   działającej na serwerze, klikasz, wpisujesz
  darka)    └────────────┬─────────────┘   login + 2FA, zapisujesz sesję i ustawienia
                         │ zapisuje
                         ▼
               /data/state.json   ← ciasteczka sesji („zapamiętane urządzenie”)
               /data/config.env   ← hasła, SMTP, selektory
                         │ czyta
                         ▼
            ┌──────────────────────────┐   wg harmonogramu (cron)
            │ orange_rabat.py          │   headless Chromium: loguje się do Mój Orange,
            │ odbiór rabatu            │   otwiera usługi pakietowe, szuka rabatu,
            └──────────────────────────┘   loguje JSON, wysyła mail, odświeża sesję
```

### Odbiór rabatu (`orange_rabat.py`)

1. Otwiera Mój Orange z zapisaną sesją (`state.json`).
2. Jeśli sesja wygasła, loguje się przez `ORANGE_LOGIN` / `ORANGE_PASSWORD`. Działa to, dopóki
   Orange rozpoznaje urządzenie i nie żąda kodu SMS.
3. Gdy Orange zażąda 2FA, kończy się kodem `2` i wysyła mail „wymaga logowania”.
4. Zapisuje odświeżone ciasteczka i otwiera
   [usługi pakietowe](https://www.orange.pl/moj-orange/uslugi-pakietowe) (`ORANGE_REWARD_URL`).
5. Szuka napisu **„Rabat za e-fakturę”** (`ORANGE_SEL_REWARD_DONE`). Jeśli ustawisz
   `ORANGE_SEL_REWARD_BUTTON`, najpierw kliknie ten przycisk.

### Panel logowania (`orange_login.py`)

- Chromium w panelu ma **ten sam** user-agent, język i rozdzielczość co przebieg automatyczny,
  a logujesz się z tego samego serwera i IP. Dzięki temu Orange rozpoznaje „zapamiętane urządzenie”.
- Obraz przychodzi jako strumień MJPEG (natywnie w `<img>`). Chromium wysyła klatkę tylko po zmianie,
  maks. `LOGIN_FPS` na sekundę, a gdy karta jest ukryta, podgląd się zatrzymuje.
  Kliknięcia, klawiatura (także wklejanie) i kółko myszy trafiają do przeglądarki.
- Sesja zapisuje się sama w chwili zalogowania. **Sprawdź sesję** otwiera Mój Orange w czystym
  oknie z samymi zapisanymi ciasteczkami. W **Ustawieniach** jest też przycisk testu maila.
- Tylko biblioteka standardowa Pythona i Playwright. Interfejs to jeden plik HTML bez frameworków
  i bez zasobów z zewnątrz.

## Konfiguracja

Większość ustawień wpisujesz w panelu. Zapisuje je do `/data/config.env`.
Możesz też utworzyć plik `.env` obok `docker-compose.yaml`
([wzór: .env.example](.env.example)). Kolejność ważności (wyższa wygrywa):

1. zmienne środowiskowe,
2. `.env` (w Dockerze wczytywany przez `env_file`),
3. `/data/config.env` z panelu.

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `CRON_SCHEDULE` | `0 9 3 * *` | harmonogram w formacie cron (Docker) |
| `TZ` | `Europe/Warsaw` | strefa czasowa (Docker) |
| `RUN_ON_START` | `0` | `1` = dodatkowy przebieg zaraz po starcie kontenera |
| `LOW_RESOURCES` | `0` | `1` = bez obrazków, fontów i mediów (ok. 500 MB RAM zamiast ok. 900 MB) i dłuższe czasy oczekiwania na stronę rabatu (60 s zamiast 20 s) |
| `ORANGE_LOGIN`, `ORANGE_PASSWORD` | — | automatyczne ponowne logowanie (zalecane: przez panel) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`, `MAIL_FROM` | — | mail z wynikiem (port 465 = SSL, inny = STARTTLS) |
| `ORANGE_REWARD_URL` | usługi pakietowe | strona sprawdzana po zalogowaniu |
| `ORANGE_SEL_*` | patrz kod | selektory strony (także w panelu, sekcja „Zaawansowane”) |
| `LOGIN_HOST_PORT` | `8080` | port panelu na hoście |
| `LOGIN_IDLE_MINUTES` | `30` | panel wyłącza się po tylu minutach bezczynności (`0` = nigdy) |
| `LOGIN_FPS` | `3` | maks. klatek/s podglądu w panelu |
| `LOG_LEVEL` | `INFO` | poziom logów |
| `DEBUG` | `0` | `1` = szczegółowe logi (nawigacja, błędy strony, czasy oczekiwania), jak `LOG_LEVEL=DEBUG` |
| `AUTO_MORANGE_TAG` | `latest` | wersja obrazu (Compose) |
| `AUTO_MORANGE_DATA` | `auto-morange-data` | wolumen Dockera albo katalog na hoście, np. `./data` (Compose) |
| `AUTO_MORANGE_MEM_LIMIT` | `1536m` | limit pamięci kontenera (Compose) |

## Wyniki i logi

| Kod wyjścia | `result` w logu | Znaczenie |
|---|---|---|
| `0` | `ok` | napis „Rabat za e-fakturę” znaleziony |
| `1` | `not_confirmed` / `error` | brak napisu albo błąd, zrzut w `/data/orange_last.png` |
| `2` | `login_required` | trzeba zalogować się ponownie przez panel |

Logi to JSON na stdout, jedna linia na zdarzenie:

```json
{"time": "2026-10-03T09:00:31+02:00", "level": "info", "msg": "rabat widoczny", "result": "ok", "url": "https://www.orange.pl/moj-orange/uslugi-pakietowe"}
{"level":"error","msg":"error running command: exit status 2","job.schedule":"0 9 3 * *", ...}
```

Pierwsza linia pochodzi ze skryptu, druga z [supercronic](https://github.com/aptible/supercronic)
(harmonogram w kontenerze). Ostatni zrzut ekranu skopiujesz z kontenera poleceniem
`docker compose cp auto-morange:/data/orange_last.png .`.

## Obraz Dockera i aktualizacje

Obraz `ghcr.io/pprazzi99/auto-morange` budują [GitHub Actions](.github/workflows/docker-publish.yml)
na `linux/amd64` i `linux/arm64`, z SBOM i atestacją pochodzenia (SLSA).
Obraz jest publikowany **tylko przy wydaniu wersji**, czyli po wypchnięciu tagu git `vX.Y.Z`.
Commity na `main` i pull requesty jedynie sprawdzają, czy obraz się buduje.

| Tag obrazu | Znaczenie |
|---|---|
| `latest` | najnowsze stabilne wydanie |
| `1.2.3`, `1.2`, `1` | konkretna wersja albo najnowsza w danej linii |
| `1.3.0-rc.1` | wersja przedpremierowa (nie zmienia `latest`) |
| `sha-abc1234` | commit, z którego zbudowano wydanie |

Aktualizacja:

```bash
docker compose pull && docker compose up -d
```

Pochodzenie obrazu możesz zweryfikować:
`gh attestation verify oci://ghcr.io/pprazzi99/auto-morange:latest --owner pprazzi99`.

## Bezpieczeństwo

| Plik | Zawartość | Ochrona |
|---|---|---|
| `/data/state.json` | ciasteczka sesji = **pełny dostęp do konta** | `chmod 600`, poza obrazem i repo |
| `/data/config.env`, `.env` | hasła Orange i SMTP | `chmod 600`, poza obrazem i repo |
| `/data/orange_last.png` | zrzut ekranu z danymi konta | `chmod 600`, poza obrazem i repo |

- Obraz **nie zawiera żadnych danych ani sekretów**. `.dockerignore` działa jak allowlista:
  do obrazu trafiają tylko skrypty `.py`, `requirements.txt` i entrypoint.
- Wartości `ORANGE_LOGIN`, `ORANGE_PASSWORD`, `SMTP_USER` i `SMTP_PASS` są zamieniane na `***` w logach i mailach.
- Kontener działa jako użytkownik bez uprawnień (UID 1000), z systemem plików tylko do odczytu,
  `cap_drop: ALL`, `no-new-privileges` oraz limitami pamięci i procesów.
- Panel: losowy token w adresie, po pierwszym wejściu przeniesiony do ciasteczka
  `HttpOnly; SameSite=Strict`. Do tego ochrona CSRF, nagłówki CSP, hasła nigdy nie wracają
  do przeglądarki, a panel sam się wyłącza po 30 min bezczynności.

> [!WARNING]
> Panel działa po zwykłym HTTP i przesyła hasło oraz sesję. Domyślnie słucha tylko na
> `127.0.0.1`, więc otwieraj go przez tunel SSH. Jeśli musisz go wystawić, zrób to wyłącznie
> za reverse proxy z HTTPS i tylko na czas logowania.

Luki bezpieczeństwa zgłaszaj prywatnie przez
[GitHub Security Advisories](https://github.com/pprazzi99/auto-morange/security/advisories/new).

## Rozwój

```bash
git clone https://github.com/pprazzi99/auto-morange.git && cd auto-morange
export COMPOSE_FILE=docker-compose.yaml:docker-compose.build.yaml
docker compose build          # obraz auto-morange:local z lokalnego kodu, dane w ./data
docker compose up auto-morange-login
```

Nakładka [docker-compose.build.yaml](docker-compose.build.yaml) buduje obraz lokalnie
i montuje `./data` zamiast wolumenu. Jeśli twój UID jest inny niż 1000, zbuduj obraz z
`APP_UID=$(id -u) APP_GID=$(id -g) docker compose build`.

| Plik | Rola |
|---|---|
| [orange_rabat.py](orange_rabat.py) | przebieg automatyczny (odbiór rabatu, mail, logi) |
| [orange_login.py](orange_login.py) | panel WWW do logowania i ustawień |
| [auto-morange-cron.sh](auto-morange-cron.sh) | zarządzanie wpisem w crontabie (wariant bez Dockera) |
| [docker-entrypoint.sh](docker-entrypoint.sh) | tryby kontenera: `cron` (domyślny), `run`, `login` |
| [Dockerfile](Dockerfile) | obraz: Python 3.12, Chromium (Playwright), supercronic |
| [docker-compose.yaml](docker-compose.yaml) | produkcyjny Compose z gotowym obrazem z GHCR |

Wydanie nowej wersji (publikuje obraz na GHCR):

```bash
git tag -a v1.0.0 -m "v1.0.0" && git push origin v1.0.0
```

## Licencja

[PolyForm Noncommercial 1.0.0](LICENSE). Możesz używać, modyfikować, rozwijać i udostępniać
ten kod, w tym zmienione wersje, **wyłącznie w celach niekomercyjnych**, np. do własnego
użytku, nauki czy jako projekt hobbystyczny. Przy dalszym udostępnianiu dołącz treść licencji
i linię `Required Notice` z pliku [LICENSE](LICENSE). Na użycie komercyjne potrzebna jest
osobna zgoda autora.
