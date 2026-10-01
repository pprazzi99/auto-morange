# Dodatkowe informacje

Materiały dla osób, które chcą zrozumieć wnętrze projektu, budować obraz lub go rozwijać. Podstawowa instrukcja jest w [README](../README.md).

## Architektura

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

## Obraz Dockera i aktualizacje

Obraz `ghcr.io/pprazzi99/auto-morange` budują [GitHub Actions](../.github/workflows/docker-publish.yml)
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

## Rozwój

```bash
git clone https://github.com/pprazzi99/auto-morange.git && cd auto-morange
export COMPOSE_FILE=docker-compose.yaml:docker-compose.build.yaml
docker compose build          # obraz auto-morange:local z lokalnego kodu, dane w ./data
docker compose up auto-morange-login
```

Nakładka [docker-compose.build.yaml](../docker-compose.build.yaml) buduje obraz lokalnie
i montuje `./data` zamiast wolumenu. Jeśli twój UID jest inny niż 1000, zbuduj obraz z
`APP_UID=$(id -u) APP_GID=$(id -g) docker compose build`.

| Plik | Rola |
|---|---|
| [orange_rabat.py](../orange_rabat.py) | przebieg automatyczny (odbiór rabatu, mail, logi) |
| [orange_login.py](../orange_login.py) | panel WWW do logowania i ustawień |
| [auto-morange-cron.sh](../auto-morange-cron.sh) | zarządzanie wpisem w crontabie (wariant bez Dockera) |
| [docker-entrypoint.sh](../docker-entrypoint.sh) | tryby kontenera: `cron` (domyślny), `run`, `login` |
| [Dockerfile](../Dockerfile) | obraz: Python 3.12, Chromium (Playwright), supercronic |
| [docker-compose.yaml](../docker-compose.yaml) | produkcyjny Compose z gotowym obrazem z GHCR |

Wydanie nowej wersji (publikuje obraz na GHCR):

```bash
git tag -a v1.0.0 -m "v1.0.0" && git push origin v1.0.0
```

## Szczegóły zabezpieczeń

- Obraz **nie zawiera żadnych danych ani sekretów**. `.dockerignore` działa jak allowlista:
  do obrazu trafiają tylko skrypty `.py`, `requirements.txt` i entrypoint.
- Wartości `ORANGE_LOGIN`, `ORANGE_PASSWORD`, `SMTP_USER` i `SMTP_PASS` są zamieniane na `***` w logach i mailach.
- Kontener działa jako użytkownik bez uprawnień (UID 1000), z systemem plików tylko do odczytu,
  `cap_drop: ALL`, `no-new-privileges` oraz limitami pamięci i procesów.
- Panel: losowy token w adresie, po pierwszym wejściu przeniesiony do ciasteczka
  `HttpOnly; SameSite=Strict`. Do tego ochrona CSRF, nagłówki CSP, hasła nigdy nie wracają
  do przeglądarki, a panel sam się wyłącza po 30 min bezczynności.
