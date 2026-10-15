# Samsara Safety → Telegram

Bot Python/aiogram pentru IDS Transportation: notificări automate într-un singur grup,
apoi upload direct al clipurilor oficiale Samsara, ca răspuns la notificare.
Fus orar implicit **America/New_York**, EST/EDT automat. Fără rapoarte, clasamente sau comenzi de lucru.

**Deployment Railway:** folosește PostgreSQL separat prin `DATABASE_URL`.
Vezi [instalarea Railway și activarea ulterioară a API-urilor](docs/RAILWAY.md).
Instrucțiunile SQLite/volume Docker de mai jos se referă la rularea locală sau pe VPS.

## Ce acoperă aplicația

- Safety Events v2: `GET /safety-events/stream`, fără filtru de punctaj sau stare.
- Polling la 60 secunde; paginare completă, cursor și coadă salvate atomic în PostgreSQL pe Railway sau SQLite local.
- Reîncercări persistente pentru API, Telegram și media întârziată; fiecare cameră are evidență proprie.
- Webhook opțional cu semnătură HMAC: declanșează citirea imediată a fluxului;
  polling-ul rămâne activ pentru recuperarea alertelor ratate.
- Mesajul apare imediat după descoperire. Videoclipurile sunt trimise când devin disponibile,
  separat pentru fiecare cameră returnată de API. Nu se inventează șoferul, adresa sau linkul.
- Fișierele se descarcă temporar, se încarcă prin `sendVideo` și se șterg inclusiv la eroare.
- Pentru clipuri peste 49 MB se încearcă micșorarea cu FFmpeg, inclus în Docker.

Vezi [verificarea API și limitele reale](docs/SAMSARA_API.md) înainte de activare.
Implementarea folosește etichetele expuse de API; categoriile dashboard nu sunt parametri API.

## De ce este nevoie pentru activare

1. Repository GitHub privat cu drept de scriere pentru contul conectat.
2. Token Samsara pentru toate camioanele: **Read Safety Events & Scores** și **Read Camera Media**.
3. Bot creat prin [@BotFather](https://t.me/BotFather), tokenul lui și ID-ul grupului Fleet Department.
4. Un server pornit permanent, cu Docker Engine și Docker Compose v2, internet către Samsara,
   hosturile media Samsara/AWS și `api.telegram.org`. GitHub păstrează codul; nu rulează botul 24/7.
5. Opțional, domeniu HTTPS și reverse proxy pentru webhook. Pentru polling nu trebuie port public.

Tokenurile se introduc în `.env` pe server. Nu se trimit în issue-uri, commituri sau mesaje.

## Configurarea Samsara

În dashboard: **Settings → API Tokens → Add an API Token**.
Selectează organizația/flota corectă și permisiunile de citire de mai sus.
Nu limita tokenul la un tag care exclude o parte din cele aproximativ 100 de camioane.
Asigură-te că organizația are licențele și setările de detecție necesare.

Botul nu cere Write Media Retrieval: folosește `media[].url` din evenimentul oficial.
Nu inițiază descărcări suplimentare din memoria camerei și nu consumă intenționat cota de retrieval.
API-ul nu poate livra evenimente care nu au fost create sau nu sunt accesibile tokenului.

## Configurarea Telegram

1. Deschide BotFather, folosește `/newbot` și păstrează tokenul.
2. Adaugă botul în grupul Fleet Department. Permite trimiterea mesajelor și videoclipurilor.
   Rolul de administrator nu este obligatoriu dacă permisiunile grupului permit aceste acțiuni.
3. Copiază `.env.example` în `.env` și completează `TELEGRAM_BOT_TOKEN`.
4. În grup trimite `/groupid@username_bot` pentru ca un update să fie disponibil.
5. Rulează helperul de mai jos pentru a vedea ID-ul negativ al grupului, apoi completează `.env`.

```sh
docker compose build
docker compose run --rm bot python -m safety_bot.setup group-id
```

Helperul doar citește updates și afișează ID-uri; nu implementează o comandă permanentă în grup.
Dacă tokenul este deja folosit de alt serviciu cu webhook sau `getUpdates`, rezolvă acel conflict
înainte de configurare. Botul acesta nu are nevoie de polling Telegram pentru notificările trimise.
Privacy Mode poate rămâne activ.

## Instalare și pornire pe un VPS Linux

După instalarea Docker Engine și Compose v2 din sursele oficiale, clonează repository-ul privat
folosind autentificarea GitHub proprie:

```sh
git clone <URL_REPOSITORY_PRIVAT>
cd samsara-safety-telegram
cp .env.example .env
chmod 600 .env
nano .env
docker compose build
docker compose run --rm bot python -m safety_bot.setup check
docker compose up -d
docker compose logs -f --tail=100 bot
```

Comanda `check` este read-only: verifică accesul Telegram și o pagină de evenimente Samsara.
Nu trimite mesaje și nu modifică SQLite. Un răspuns fără media nu confirmă permisiunea media.
Prima pornire de producție trimite evenimentele selectate **actualizate în ultimele 24 de ore**,
inclusiv evenimente vechi editate recent. Setează `INITIAL_LOOKBACK_HOURS=0` înainte de prima
pornire dacă vrei doar actualizările de acum înainte. După prima pornire se păstrează cursorul original.

Fișierul `.env` minim:

```dotenv
SAMSARA_API_TOKEN=completeaza_local
TELEGRAM_BOT_TOKEN=completeaza_local
TELEGRAM_GROUP_ID=-1001234567890
POLL_INTERVAL_SECONDS=60
TIMEZONE=America/New_York
```

Nu porni două instanțe pentru aceeași bază/grup. Configurația livrează un singur container;
nu folosi `--scale`. Volumul `safety_data` păstrează cursorul și trimiterile după restart.
Nu folosi `docker compose down -v`: eliminarea volumului șterge deduplicarea și coada.
Pentru un grup diferit folosește o bază separată. Migrarea Telegram la supergroup este salvată automat.

## Webhooks Samsara (opțional)

În **Settings → Webhooks**, creează un webhook către
`https://DOMENIUL_TAU/samsara/webhook`. Copiază cheia Base64 în `SAMSARA_WEBHOOK_SECRET`.
Configurează alertele Safety disponibile în organizație să trimită către acest webhook,
cu toate tipurile și tagurile dorite; poți folosi payload 2.0. Folosește funcția Test/Ping.

Reverse proxy-ul de pe server trebuie să păstreze corpul și headerele originale și să trimită
la `http://127.0.0.1:8080`. Docker publică portul doar pe loopback. Pentru alt port trebuie
actualizată și maparea din Compose. Endpointul acceptă numai POST, corp de maximum 1 MiB,
semnătură validă și timestamp cu abatere de maximum cinci minute. Serverul trebuie să aibă ora sincronizată.
Fără secret, serverul webhook nu pornește; polling-ul este suficient.

Webhook-ul confirmă rapid cu 202 și trezește fluxul. Nu interpretează ID-ul alertei drept
ID Safety Event. Evenimentul poate apărea în API mai târziu, deci polling-ul continuă.
Nu există în documentația verificată o singură subscripție SafetyEvent care să garanteze
toate cele șapte categorii și media; alertele se configurează separat în organizație.

## Exploatare și backup

```sh
docker compose restart bot
docker compose logs --tail=200 bot
docker compose stop bot
docker compose cp bot:/app/data ./backup-data
docker compose start bot
```

Backupul se face cu botul oprit, pentru a copia coerent fișierele SQLite și WAL.
Păstrează backupurile privat: baza conține șoferi, locații și URL-uri media.
Nu sunt păstrate videoclipuri în baza de date. Datele evenimentelor nu sunt șterse automat,
pentru a păstra deduplicarea; dimensionează discul și stabilește politica internă de retenție.

Pentru actualizare: `git pull`, `docker compose up -d --build`. Baza rămâne în volum.
Erorile sunt în loguri fără tokenuri sau URL-uri semnate. HTTP 401/403 indică token/permisiuni;
429 impune așteptare; problemele persistente necesită intervenție în configurare.
Serviciul reîncearcă și erorile de configurare pentru a putea continua după corectare.

## Garanția de livrare

Deduplicarea normală se face după ID Samsara, iar video după ID eveniment + intrare cameră.
Mesajul text și fiecare video au confirmări separate în SQLite.
Cursorul și evenimentele sunt salvate în aceeași tranzacție înainte de livrare.

**Telegram Bot API nu oferă cheie de idempotency pentru sendMessage/sendVideo.** Dacă Telegram
acceptă uploadul, dar conexiunea se întrerupe înainte de confirmare sau procesul cade înainte
de salvarea confirmării în SQLite, o reîncercare poate dubla acel mesaj/video. Acesta este un
sistem de livrare „cel puțin o dată”, cu deduplicare persistentă; nu poate promite matematic
„exact o dată” între două servicii independente. Preferă recuperarea trimiterii față de pierderea ei.

Media absentă se reverifică la 15 minute; media deja transmisă la șase ore pentru eventuale
camere suplimentare. Modificările `media` observate în flux reactivează imediat evenimentul.
Nu există termen după care botul declară definitiv că un eveniment nu va avea video.
Erorile de trimitere au backoff exponențial până la șase ore, cu respectarea Retry-After.

## Dezvoltare și verificare

Python 3.12+ și FFmpeg (pentru clipuri mari):

```sh
python -m venv .venv
# Linux: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m safety_bot
```

Testele folosesc servicii simulate: restart SQLite, rollback cursor/coadă, deduplicare,
video întârziat, eroare upload și ștergere temporare, semnături webhook, lipsa câmpurilor,
punctaj zero și EST/EDT. Workflow-ul GitHub verifică și construirea imaginii Docker.
Acceptanța finală necesită un eveniment real și vizionarea clipului din grupul desemnat.
