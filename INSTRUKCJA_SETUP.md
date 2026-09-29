# auto-morange: instrukcja krok po kroku

Opis działania, konfiguracji i bezpieczeństwa jest w [README.md](README.md).
Wybierz jeden wariant:

- **A. Docker (zalecany):** gotowy obraz z GHCR, bez klonowania repozytorium.
- **B. Bez Dockera (lekki):** Python i systemowy cron, nic nie działa między przebiegami.

| | A. Docker | B. Bez Dockera |
|---|---|---|
| Odbiór rabatu | kontener `auto-morange` z wbudowanym cronem ([supercronic](https://github.com/aptible/supercronic)) | `orange_rabat.py` z systemowego `crontab` |
| Logowanie | kontener `auto-morange-login` (na żądanie) | `python3 orange_login.py` (na żądanie) |
| Dane | wolumen Dockera `auto-morange-data` | `./data` obok skryptów |
| Narzut | obraz ok. 1,5 GB; czekający kontener zajmuje kilka MB RAM | brak |

W obu wariantach przebieg trwa ok. 30 s i zużywa ok. 900 MB RAM
(ok. 500 MB z `LOW_RESOURCES=1`).

---

## A. Docker

Wymagania: Docker z Compose v2.24+ (`docker compose version`).

```bash
mkdir auto-morange && cd auto-morange
curl -fsSLO https://raw.githubusercontent.com/pprazzi99/auto-morange/main/docker-compose.yaml
docker compose pull
```

Opcjonalnie pobierz wzór konfiguracji. Nie jest wymagany, bo wszystko ustawisz w panelu:

```bash
curl -fsSL -o .env https://raw.githubusercontent.com/pprazzi99/auto-morange/main/.env.example
chmod 600 .env
```

### A1. Logowanie (panel)

```bash
docker compose up auto-morange-login
```

W logu pojawi się linia `"panel logowania gotowy"` z adresem `http://127.0.0.1:8080/?token=…`.
Jeśli serwer nie ma ekranu, zestaw najpierw tunel ze swojego komputera:
`ssh -L 8080:127.0.0.1:8080 użytkownik@serwer`. Potem otwórz ten adres u siebie.

W panelu:

1. Zaloguj się do Mój Orange (login, hasło, kod SMS) i zaznacz **„Zapamiętaj to urządzenie”**.
   Sesja zapisze się sama. Krok 2 w panelu zmieni się na ✓.
2. Kliknij **Sprawdź sesję**.
3. Kliknij **Ustaw login i hasło** i wpisz dane konta. Dzięki nim skrypt sam odnowi sesję,
   gdy ta wygaśnie.
4. Opcjonalnie w **Ustawieniach** wpisz dane SMTP, kliknij **Zapisz**, a potem **Test maila**.
5. Kliknij **Zakończ**. Kontener panelu sam się wyłączy.

### A2. Harmonogram

```bash
docker compose up -d
docker compose logs -f auto-morange
```

Domyślnie przebieg startuje 3. dnia miesiąca o 9:00. Inny termin ustawisz w `.env`,
np. `CRON_SCHEDULE=0 8 5 * *`, a potem uruchom `docker compose up -d`.

Test od razu, bez czekania na harmonogram:

```bash
docker compose run --rm auto-morange run
```

### A3. Aktualizacja

```bash
docker compose pull && docker compose up -d
```

Żeby przypiąć konkretną wersję, dodaj do `.env` na przykład `AUTO_MORANGE_TAG=1.0.0`.

---

## B. Bez Dockera (lekki)

Wymagania: Python 3.10+, ok. 500 MB na Chromium, 0,5–1 GB RAM przez ok. 30 s raz w miesiącu.

```bash
git clone https://github.com/pprazzi99/auto-morange.git && cd auto-morange

sudo apt install python3-venv                     # Debian/Ubuntu, jeśli brakuje
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install-deps chromium   # biblioteki systemowe dla Chromium
.venv/bin/playwright install --no-shell chromium
```

### B1. Logowanie (panel)

```bash
.venv/bin/python orange_login.py
```

Dalej postępuj jak w **A1**. Adres z tokenem pojawi się w terminalu, a dane zapiszą się w `./data`.

### B2. Harmonogram (systemowy cron)

```bash
./auto-morange-cron.sh install               # 3. dnia miesiąca o 9:00
./auto-morange-cron.sh install "0 8 5 * *"   # własny termin
./auto-morange-cron.sh status | remove
```

Skrypt dodaje do crontabu wpis (reszta crontabu zostaje bez zmian). Ręcznie, przez `crontab -e`:

```
0 9 3 * * cd /ścieżka/do/auto-morange && LOW_RESOURCES=1 .venv/bin/python orange_rabat.py >> data/orange.log 2>&1
```

Test od razu: `./auto-morange-cron.sh run`. Systemowy cron działa w strefie czasowej serwera.

---

## Typowe problemy

| Objaw | Co zrobić |
|---|---|
| `result: login_required` / mail „wymaga logowania” | sesja wygasła albo Orange zażądał kodu SMS; powtórz logowanie (A1 / B1) |
| `result: not_confirmed` | na stronie usług pakietowych nie ma napisu „Rabat za e-fakturę”. Obejrzyj zrzut (`docker compose cp auto-morange:/data/orange_last.png .`) i w razie potrzeby popraw selektor `REWARD_DONE` w panelu („Zaawansowane”) |
| „Sprawdź sesję” mówi, że nie jesteś zalogowany, choć jesteś | selektor `LOGGED_IN` musi wskazywać coś, co widać **tylko po zalogowaniu** i co pojawia się szybko; ustaw go w panelu (Ustawienia → Selektory) |
| Wszystko działa bardzo wolno (serwer z 1 vCPU) | ustaw `LOW_RESOURCES=1`, zamykaj kartę panelu, gdy go nie używasz, i nie uruchamiaj panelu razem z przebiegiem |
| `nie udało się wysłać maila` | szczegóły są w logu; dla Gmaila użyj [hasła aplikacji](https://myaccount.google.com/apppasswords) |
| `Permission denied` na `/data` | przy katalogu na hoście (`AUTO_MORANGE_DATA=./data`): `sudo chown -R 1000:1000 data` |
| Chromium pada w kontenerze albo kontener jest zabijany (`OOMKilled`) | sprawdź `shm_size: 256m`; zwiększ `AUTO_MORANGE_MEM_LIMIT` albo ustaw `LOW_RESOURCES=1` |
| Port 8080 jest zajęty | ustaw w `.env` `LOGIN_HOST_PORT=8081` |
