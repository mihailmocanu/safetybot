# Verificarea API Samsara

Documentație oficială consultată la 15 octombrie 2026. Nu au fost folosite tokenuri de client
pentru această verificare. Disponibilitatea reală în organizație trebuie validată cu `setup check`
și un eveniment real; documentația nu confirmă licențele, setările sau accesul flotei.

## Endpointuri folosite

| Endpoint | Rol |
| --- | --- |
| `GET /safety-events/stream` | Evenimente/actualizări, cursor persistent, `queryByTimeField=updatedAtTime` |
| `GET /safety-events?safetyEventIds=...` | Reîmprospătarea evenimentului și a URL-urilor media |

Ambele cer **Read Safety Events & Scores**. Fluxul documentează **Read Camera Media** pentru video.
Se solicită `includeAsset=true`, `includeDriver=true`, `includeVgOnlyEvents=true`.
Nu se filtrează punctajul sau starea, inclusiv dismissed. Rata clientului este maximum patru
cereri pe secundă, sub limita documentată de cinci pentru aceste endpointuri.

Schema v2 documentată folosește ID UUID; `startMs`/`endMs` sunt stringuri RFC3339 în această
versiune, în pofida numelor. Truck este `asset.name`, driver este `driver.name`, locația este
`location.address` sau coordonatele. Linkurile sunt `inboxEventUrl` și `incidentReportUrl`.

Surse: [Safety Events Stream](https://developers.samsara.com/reference/getsafetyeventsv2stream),
[Get Safety Events](https://developers.samsara.com/reference/getsafetyeventsv2).

## Cele șapte categorii

Numele cerute sunt **risk factors din dashboard**, nu valorile parametrului `behaviorLabels`.
Samsara confirmă cele șapte grupe în
[Safety Dashboard](https://kb.samsara.com/hc/en-us/articles/20287494189709-View-your-Safety-Dashboard-from-the-Driver-Portal).
API-ul returnează etichete individuale. Tabelul este maparea de integrare folosită de bot,
nu o afirmație că API-ul publică această taxonomie per eveniment.

| Categorie | Etichete API urmărite |
| --- | --- |
| Harsh Driving | `Acceleration`, `Braking`, `HarshTurn`, `AggressiveDriving` |
| Speeding | `Speeding`, `LightSpeeding`, `ModerateSpeeding`, `HeavySpeeding`, `SevereSpeeding`, `MaxSpeed` |
| Traffic Signs & Signals | `RollingStop`, `RanRedLight`, `DidNotYield`, `EdgeRailroadCrossingViolation` |
| Collision Risk | `FollowingDistance`, `FollowingDistanceModerate`, `FollowingDistanceSevere`, `GenericTailgating`, `ForwardCollisionWarning`, `NearCollison`, `NearPedestrianCollision`, `LaneDeparture`, `LateResponse`, `UnsafeManeuver`, `VulnerableRoadUserCollisionWarning`, `ProximityWarning`, `RearCollisionWarning`, `VehicleInBlindSpotWarning`, `RolloverProtection`, `YawControl` |
| Policy Violations | `NoSeatbelt`, `ObstructedCamera`, `Passenger`, `Smoking`, `PolicyViolationMask`, `ProtectiveEquipment`, `BluetoothHeadset`, `HosViolation`, `OtherViolation`, `UnsafeParking`, `Reversing`, `UTurn`, `LeftTurn`, `ImproperEgress` |
| Crash | `Crash`, `HarshImpact`, `HighSpeedSuddenDisconnect` |
| Distracted Driving | `EdgeDistractedDriving`, `GenericDistraction`, `MobileUsage`, `Drowsy`, `Eating`, `Drinking`, `EatingDrinking` |

Ortografia `NearCollison` este cea documentată în API. `HarshImpact` și
`HighSpeedSuddenDisconnect` sunt incluse pentru acoperire, fără a le redenumi automat „Crash”.
Unele etichete adiționale apar în schema de răspuns, chiar dacă lista parametrului de filtrare
este mai scurtă. Botul citește fluxul fără filtru server-side și filtrează local.
Maparea completă și ajustabilă este în `safety_bot/events.py`. Evenimentele cu mai multe etichete
se trimit o dată, cu toate etichetele. Etichetele necunoscute sunt semnalate în loguri pentru actualizare.

## Evenimente cu 0.0 puncte și limite de acoperire

Schema Safety Events nu definește „Distracted Driving (0.0 pts)” ca un tip separat.
Botul nu citește scorul pentru selecție: orice eveniment expus, cu o etichetă urmărită, se trimite,
chiar dacă în dashboard contribuția sa este zero. Zero puncte nu dovedește existența unui eveniment.

Nu toate detecțiile ajung în Safety Inbox. Endpointul beta
[`GET /detections/stream`](https://developers.samsara.com/reference/getdetections) permite
identificarea motivelor de filtrare și a detecțiilor fără eveniment Inbox; cere acces Detection Log.
Acest proiect urmărește Safety Events, **nu toate detecțiile brute**, și nu activează implicit API-ul beta.
Dacă exemplul real de „0.0 pts” există doar în Detection Log, este necesară această extensie și
validarea structurii/datei cu organizația înainte de a promite acoperirea.

De asemenea, Safety Events nu este identic cu fiecare interval din istoricul de viteză.
[`GET /speeding-intervals`](https://developers.samsara.com/reference/getspeedingintervals)
este o sursă separată, cu intervale ale călătoriilor finalizate, fără garanție de videoclip.
Nu este importată de acest bot, pentru a nu crea două alerte pentru același incident din surse
care nu au garantat un ID comun. Sunt urmărite toate **Safety Events expuse** cu etichetele Speeding.

Suplimentar, modul `LIVE_SPEEDING_ENABLED` folosește acum
[`GET /readings/latest`](https://developers.samsara.com/reference/getreadingssnapshot):
`samsaraSpeed` și `samsaraSpeedLimit` (ambele m/s), cu Read Readings, iar
[`GET /assets`](https://developers.samsara.com/reference/listassets) oferă numele camioanelor
cu Read Assets. Acest mod compară strict viteză > limită la fiecare 60 secunde, independent
de etichetele de severitate. Folosește numai perechi proaspete de date, maximum 120 secunde;
depasirile între două eșantioane pot fi ratate. Nu presupune existența unui clip pentru o alertă
generată direct din citiri; media se atașează când există un eveniment Safety corespunzător.

## Video oficial

Schema Safety Events v2 oferă `media[]`, fiecare cu `input` și `url` pentru descărcare.
Botul reîmprospătează evenimentul, descarcă URL-ul media fără token Bearer, apoi încarcă fișierul
în Telegram. Nu folosește scraping, cookie-uri de dashboard sau ocolirea autentificării.
Permisiunile, disponibilitatea în cloud, camera și politica de retenție pot limita media.
Un eveniment detectat numai de Vehicle Gateway nu implică existența unui clip.
Imaginile sau alte formate nu sunt trimise ca videoclip fals.

Alternativa oficială pentru media care nu se află deja pe eveniment este
[`POST /cameras/media/retrieval`](https://developers.samsara.com/reference/postmediaretrieval),
urmat de [`GET /cameras/media/retrieval`](https://developers.samsara.com/reference/getmediaretrieval).
Este asincronă, necesită Write Media Retrieval pentru inițiere și are cote lunare;
dispozitivele offline pot livra ulterior. Media cea mai apropiată temporal nu este neapărat
clipul exact al evenimentului. Botul nu inițiază automat aceste cereri; preferă clipul oficial asociat.

Telegram are limită de upload video de 50 MB și suportă MPEG4. Botul încearcă FFmpeg pentru
clipuri mari; dacă nu încap, păstrează notificarea și reîncearcă, înregistrând eroarea.
Surse: [Telegram sendVideo](https://core.telegram.org/bots/api#sendvideo),
[aiogram sendVideo](https://docs.aiogram.dev/en/latest/api/methods/send_video.html).

## Webhooks

Samsara permite [Alert Webhooks](https://developers.samsara.com/docs/webhook-reference)
configurate pe condiții individuale și
[Event Subscriptions](https://developers.samsara.com/docs/event-subscriptions-webhooks).
Documentația publică verificată nu garantează o singură subscripție care să livreze toate
Safety Events din cele șapte categorii, actualizările și media disponibilă ulterior.
De aceea webhook-urile Safety configurate în organizație sunt un semnal de verificare imediată,
iar fluxul cu polling rămâne sursa de recuperare.

Semnătura verificată este HMAC SHA256 peste corpul original prefixat cu `v1:<timestamp>:`;
cheia Samsara se decodează din Base64. Vezi
[Webhook Signatures](https://developers.samsara.com/docs/webhooks).
Fără domeniu/HTTPS, aplicația funcționează exclusiv prin polling.
