# Rapoarte Speeding și contor pe 30 de zile

Activează `SPEEDING_REPORTS_ENABLED=true` pentru contorul din alerte și rapoartele automate.
Setarea implicită din `.env.example` este `false`. Pe Railway modifică variabila serviciului
`safety-bot`, apoi aplică schimbarea printr-un deployment nou. Botul folosește aceeași bază
de date ca monitorizarea Safety; nu este necesar un serviciu sau un cron separat.

## Program și perioade

Cu `TIMEZONE=America/New_York`, programul este:

| Raport | Trimitere | Perioadă principală | Comparație |
| --- | --- | --- | --- |
| Săptămânal | Duminică, 20:00 | Duminica precedentă 20:00 → duminica curentă 20:00 | Săptămâna imediat precedentă |
| Lunar | Pe data de 1, 08:00 | Ultimele 30 de zile până la ora raportului | Cele 30 de zile imediat precedente |

Raportul săptămânal include și totalul din ultimele 30 de zile. Raportul lunar folosește
o fereastră mobilă de exact 30 × 24 de ore, nu luna calendaristică precedentă.
Raportul săptămânal păstrează ora locală 20:00 inclusiv la trecerea EST/EDT; o astfel
de săptămână poate avea 167 sau 169 de ore. Datele sunt stocate și comparate în UTC,
iar mesajele arată ora locală și abrevierea EST/EDT.

Perioadele includ începutul și exclud sfârșitul. Un incident produs exact la încheierea
unei săptămâni intră în săptămâna următoare, fără dublare între rapoartele săptămânale.
Ferestrele lunare de 30 de zile pot avea suprapuneri sau goluri între ele, în funcție de
lungimea lunii; acesta este efectul programului lunar combinat cu fereastra mobilă cerută.

## Conținutul raportului

Mesajele sunt în engleză, ca alertele, și includ:

- Numărul total de incidente Speeding înregistrate în flotă în perioada raportului.
- Numărul camioanelor cu cel puțin un incident în acea perioadă.
- Totalul incidentelor în ultimele 30 de zile.
- Totalul perioadei precedente și diferența numerică.
- Top 10 camioane după numărul incidentelor; raportul săptămânal arată și totalul pe 30 de zile
  pentru fiecare camion, iar cel lunar arată numărul din cele 30 de zile precedente.

Nu există puncte, penalizări sau scoruri pe șoferi. „Total flotă” înseamnă totalul incidentelor
din flotă, nu numărul presupus de camioane active. Nu se inventează date despre activele flotei.
Un raport fără incidente arată explicit că nu au fost înregistrate incidente în perioada respectivă.

## Contorul din fiecare alertă

După încărcarea istoricului, o alertă Speeding poate include, de exemplu:

```text
⚠️ Speeding Alert
Truck: 1037
Speeding incidents in past 30 days: 3
Time: Oct 16, 2026, 10:25 AM EDT
```

Numărul este calculat pentru același asset/camion în ultimele 30 × 24 de ore la prima
pregătire a notificării, include incidentul curent și este păstrat pentru reîncercările acelei
alerte. Nu înseamnă „a treia verificare la 60 de secunde”. Este numărul incidentelor cunoscute
în acel moment; un eveniment disponibil mai târziu în API poate modifica următoarele calcule.
În timpul importului inițial contorul este omis pentru a nu prezenta un total parțial ca total complet.
Un eveniment vechi de peste 30 de zile nu primește un contor curent în alertă.

În modul Safety Events, un ID de eveniment este un incident chiar dacă are mai multe
etichete Speeding. Actualizarea evenimentului, reîncercarea mesajului și atașarea clipului
nu cresc contorul. În modul live, un episod continuu observat de depășire este un incident;
evenimentul oficial corelat pentru video este legat de același incident. Dacă există deja
o alertă oficială corespunzătoare, episodul live o adoptă.

## Importul istoricului și limitele datelor

La prima activare se citesc 60 de zile de evenimente disponibile prin
[`GET /safety-events/stream`](https://developers.samsara.com/reference/getsafetyeventsv2stream),
cu `queryByTimeField=createdAtTime` și limite fixe de timp. Cele 60 de zile permit calculul
ferestrei curente de 30 de zile și comparația cu cele 30 de zile precedente. Conform
documentației endpointului, acest mod folosește momentul detectării evenimentului (`startMs`).
Se păstrează momentul incidentului, nu momentul trimiterii în Telegram.

Importul salvează numai evidența pentru calcule: nu pune evenimente istorice în coada de
notificări și nu modifică cursorul fluxului curent. Paginarea și progresul importului sunt
persistente; erorile temporare se reîncearcă. Rapoartele așteaptă terminarea importului.

Sunt numărate toate evenimentele disponibile cu etichetele Speeding recunoscute, fără
filtru după punctaj. Acest istoric reprezintă **Safety Events expuse tokenului**, nu fiecare
secundă în care un camion a depășit limita. Configurația detecției Samsara, accesul tokenului,
datele disponibile și întârzierile API influențează ce incidente pot fi numărate. Un import
terminat confirmă că au fost citite paginile disponibile, fără a garanta evenimente inexistente
sau inaccesibile prin API.

Regula directă `samsaraSpeed > samsaraSpeedLimit` necesită separat
[`GET /readings/latest`](https://developers.samsara.com/reference/getreadingssnapshot),
permisiunea **Read Readings** și `LIVE_SPEEDING_ENABLED=true`.
Acest mod rămâne dezactivat până la verificarea accesului. Citirile curente nu reconstruiesc
retroactiv toate episoadele brute din ultimele 30 de zile. La verificare din 60 în 60 de secunde,
o depășire mai scurtă dintre verificări poate să nu fie observată.

## Restarturi și livrare

La prima activare nu se trimit rapoarte pentru scadențe anterioare activării. Primul raport
este cel de la următoarea scadență. După aceea, scadențele ratate în timpul unei opriri sunt
recuperate, folosind ferestrele lor originale. Istoricul incidentelor și evidența rapoartelor
persistă în PostgreSQL pe Railway sau SQLite local.

Fiecare raport are o cheie persistentă după tip și scadență. Conținutul este fixat înaintea
trimiterii, iar fiecare mesaj al unui raport lung are confirmare separată. Reîncercările folosesc
același conținut. Datele devenite disponibile după pregătirea raportului nu rescriu un raport
deja trimis.

Telegram nu oferă chei de idempotency pentru `sendMessage`: dacă acceptă un mesaj, dar
confirmarea se pierde înainte de salvare, o reîncercare îl poate dubla. Evidența persistentă
previne repetarea normală după restart, dar nu poate garanta matematic livrarea exact o dată.
