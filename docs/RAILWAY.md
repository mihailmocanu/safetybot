# Railway: bot + PostgreSQL

Arhitectura Railway folosește un serviciu `safety-bot` și unul `Postgres` cu volum persistent.
Botul folosește `DATABASE_URL`; SQLite rămâne disponibil pentru rulare locală, dacă URL-ul este gol.
Pe Railway, pornirea fără `DATABASE_URL` este refuzată pentru a evita pierderea deduplicării
în filesystemul efemer. Nu este necesar un volum pe serviciul botului când folosește PostgreSQL.

## Configurare

1. Creează proiectul și adaugă template-ul PostgreSQL. Păstrează volumul bazei.
2. Creează serviciul `safety-bot`, din repository-ul GitHub și Dockerfile-ul proiectului.
3. În variabilele **botului**, adaugă referința privată:

```dotenv
DATABASE_URL=${{Postgres.DATABASE_URL}}
TIMEZONE=America/New_York
COMPANY_NAME=IDS Transportation
POLL_INTERVAL_SECONDS=60
INITIAL_LOOKBACK_HOURS=0
BOT_ENABLED=false
```

Numele `Postgres` trebuie să corespundă serviciului de bază de date.
Referința evită copierea parolei în surse. Nu publica PostgreSQL prin TCP proxy pentru bot;
comunicarea se face în rețeaua privată Railway.

`BOT_ENABLED=false` creează tabelele și păstrează procesul în așteptare, fără cereri Samsara
sau Telegram. Nu inițializează cursorul; prima pornire activă stabilește începutul monitorizării.
Acest mod permite verificarea deploymentului înainte de introducerea tokenurilor.

Pentru configurare asistată, cu `BOT_ENABLED=false`, setează temporar `SETUP_ACTION=group-id`
și aplică variabilele. La pornire botul citește updates Telegram și afișează în loguri numai
username-ul botului și grupurile/ID-urile disponibile. Nu trimite mesaje și nu afișează tokenuri.
După completarea ID-ului, `SETUP_ACTION=check` verifică Telegram și accesul Safety Events.
După verificare, golește `SETUP_ACTION` înainte de activare.

4. Setează **Serverless/Sleep off**, o singură replică, fără cron, restart **Always**,
   deployment overlap **0 secunde**, draining **45 secunde**. Folosește Dockerfile-ul din rădăcină;
   comanda implicită este `python -m safety_bot`. Nu seta healthcheck HTTP pentru workerul fără webhook.
5. După ce infrastructura este gata, introdu direct în Variables:

```dotenv
SAMSARA_API_TOKEN=<tokenul real>
TELEGRAM_BOT_TOKEN=<tokenul real>
TELEGRAM_GROUP_ID=<ID-ul negativ al grupului>
BOT_ENABLED=true
```

Aplică variabilele și redeploy. Verifică logurile și un eveniment real.
Tokenurile, parola bazei și valorile reale ale variabilelor nu se salvează în GitHub.
`INITIAL_LOOKBACK_HOURS=0` începe cu actualizările de la activare. Alege 24 înainte de
prima activare dacă dorești și ultimele 24 ore.

## Persistență și operare

La pornire sunt create automat tabelele `meta`, `events` și `videos`, plus indexul cozii.
PostgreSQL păstrează cursorul și confirmările de trimitere între restarturile botului.
O blocare PostgreSQL pe sesiune împiedică rularea simultană a două instanțe pe aceeași bază.
La pierderea conexiunii, procesul se poate opri și Railway îl repornește conform politicii.

Nu există migrare automată a unei baze SQLite existente către PostgreSQL. Pentru un proiect nou,
nu există istoric de transferat; dacă ai trimis deja alerte din SQLite, oprește botul și migrează
ID-urile înainte să activezi PostgreSQL, pentru a evita reimportul.

Configurează backupurile din Railway pentru volumul PostgreSQL și verifică restaurarea.
Backupurile nu sunt garantate/configurate de simpla existență a bazei. Costul serviciului și
stocării este facturat de Railway conform planului contului.

Pentru webhook-ul opțional, setează cheia `SAMSARA_WEBHOOK_SECRET` și generează un domeniu
Railway pe portul `PORT` al serviciului. Botul citește automat `PORT`; URL-ul este
`https://<domeniu>/samsara/webhook`. Fără cheie nu pornește serverul HTTP și nu trebuie domeniu.

## Validare

Testele unitare acoperă fluxul de notificări cu servicii simulate. CI pornește un PostgreSQL
temporar separat și verifică tranzacțiile, cursorul, deduplicarea după reconectare și blocarea
celei de-a doua instanțe. Pentru integrare locală folosește `TEST_DATABASE_URL` doar către
o bază **goală, de test**, niciodată baza de producție.

Documentație oficială: [Railway PostgreSQL](https://docs.railway.com/databases/postgresql),
[servicii](https://docs.railway.com/services), [volume](https://docs.railway.com/volumes/reference).
