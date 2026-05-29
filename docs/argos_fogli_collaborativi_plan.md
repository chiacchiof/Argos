# Argos Fogli Collaborativi - Piano per Opus

## Obiettivo

Creare nei Fascicoli/Progetti di Argos una funzionalita' "Fogli collaborativi"
tipo Google Sheets, con editing multiutente realtime via WebSocket e Redis.

La funzionalita' deve permettere a piu' utenti dello stesso tenant, autorizzati
sul progetto, di aprire lo stesso foglio e vedere in tempo reale:

- modifiche alle celle;
- cursori/selezioni degli altri utenti;
- utenti online nel foglio;
- recupero coerente dopo riconnessione.

## Contesto Argos

Argos oggi ha un modulo Fascicoli privacy-first:

- i file fisici del fascicolo restano sul PC del cliente;
- Postgres/Neon salva solo metadati, ACL, chat e riferimenti;
- la visibilita' e' tenant-scoped;
- i permessi sono gestiti tramite `projects` e `project_users`.

I fogli collaborativi sono una nuova eccezione consapevole: il contenuto del
foglio deve vivere online, perche' piu' utenti devono modificarlo insieme da
macchine diverse.

Quindi il foglio collaborativo non va trattato come un normale file locale del
fascicolo. Va trattato come un asset online del progetto.

File da studiare prima di implementare:

- `docs/argos_fascicoli_design.md`
- `app/routes/fascicoli.py`
- `app/fascicoli/db.py`
- `app/db.py`
- `app/auth.py`
- `app/main.py`
- `app/templates/fascicoli_detail.html`

## Architettura Target

Usare questa separazione:

- Postgres/Neon: sorgente di verita' del foglio.
- Redis: bus realtime tra processi/server Argos.
- WebSocket: connessione live tra browser e server.
- Revisioni DB: recupero dopo reconnect e audit.

Flusso generale:

```text
Browser utente A
  -> WebSocket
Argos worker 1
  -> scrive modifica in Postgres
  -> pubblica evento su Redis
Redis
  -> distribuisce evento agli altri worker
Argos worker 2
  -> inoltra evento via WebSocket
Browser utente B
```

Redis non deve contenere lo stato definitivo del foglio. Redis serve solo per
notificare live gli altri worker. Se Redis perde un evento, il client deve poter
recuperare da Postgres tramite revisioni.

## Schema DB

Aggiungere le tabelle nello schema inizializzato da `app/db.py`.

### `project_sheets`

```sql
CREATE TABLE IF NOT EXISTS project_sheets (
  id                 BIGSERIAL PRIMARY KEY,
  tenant_id          BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  project_id         BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title              TEXT NOT NULL,
  created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  is_archived        BOOLEAN NOT NULL DEFAULT FALSE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_project_sheets_project
  ON project_sheets(project_id, is_archived);

CREATE INDEX IF NOT EXISTS idx_project_sheets_tenant
  ON project_sheets(tenant_id);
```

### `project_sheet_cells`

```sql
CREATE TABLE IF NOT EXISTS project_sheet_cells (
  sheet_id           BIGINT NOT NULL REFERENCES project_sheets(id) ON DELETE CASCADE,
  row_idx            INT NOT NULL,
  col_idx            INT NOT NULL,
  value              TEXT,
  formula            TEXT,
  style_json         JSONB,
  updated_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revision           BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (sheet_id, row_idx, col_idx)
);

CREATE INDEX IF NOT EXISTS idx_project_sheet_cells_sheet_revision
  ON project_sheet_cells(sheet_id, revision);
```

### `project_sheet_revisions`

```sql
CREATE TABLE IF NOT EXISTS project_sheet_revisions (
  id            BIGSERIAL PRIMARY KEY,
  sheet_id      BIGINT NOT NULL REFERENCES project_sheets(id) ON DELETE CASCADE,
  actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  revision      BIGINT NOT NULL,
  patch_json    JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (sheet_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_project_sheet_revisions_sheet_revision
  ON project_sheet_revisions(sheet_id, revision);
```

## Backend

Creare un modulo dedicato:

- `app/fascicoli/sheets_db.py`
- `app/routes/fascicoli_sheets.py`

Registrare il router in `app/main.py`.

Aggiornare dipendenze:

```toml
redis>=5.0
```

Aggiornare configurazione:

- `REDIS_URL` in `app/config.py`
- `REDIS_URL` in `.env.example`

Usare `redis.asyncio` perche' WebSocket e listener Redis sono async.

## Route HTTP

Implementare:

- `GET /fascicoli/{project_id}/sheets`
- `POST /fascicoli/{project_id}/sheets`
- `GET /fascicoli/{project_id}/sheets/{sheet_id}`
- `POST /fascicoli/{project_id}/sheets/{sheet_id}/rename`
- `POST /fascicoli/{project_id}/sheets/{sheet_id}/archive`
- eventuale `GET /fascicoli/{project_id}/sheets/{sheet_id}/export.csv`

Il dettaglio fascicolo deve avere una sezione o tab "Fogli" con:

- lista fogli del progetto;
- pulsante "Nuovo foglio";
- link per aprire il foglio;
- stato archivio se serve.

## WebSocket

Endpoint:

```text
/ws/fascicoli/{project_id}/sheets/{sheet_id}
```

All'apertura:

1. autenticare l'utente tramite cookie sessione Argos;
2. verificare tenant e accesso al progetto;
3. verificare che il foglio appartenga al progetto;
4. inviare snapshot iniziale;
5. iscrivere la connessione al gruppo in memoria del worker;
6. iscrivere il worker al canale Redis del foglio se non gia' iscritto.

Controllare i permessi sia all'apertura sia su ogni messaggio di modifica.

## Permessi

Riusare il modello dei Fascicoli:

- viewer: puo' leggere il foglio e vedere realtime;
- editor: puo' modificare celle;
- owner: puo' modificare, rinominare e archiviare;
- tenant_architect: puo' gestire;
- super_admin: puo' gestire.

Le funzioni esistenti utili sono in `app/routes/fascicoli.py`:

- `_can_edit_project`
- `_can_manage_project`

Valutare se spostarle in un helper condiviso, per evitare duplicazione tra route
Fascicoli e route Fogli.

## Protocollo WebSocket

### Messaggi client -> server

`hello`

```json
{
  "type": "hello",
  "last_revision": 123
}
```

`cell_patch`

```json
{
  "type": "cell_patch",
  "patch_id": "client-generated-id",
  "cells": [
    {"row": 0, "col": 0, "value": "Acme SRL", "formula": null}
  ]
}
```

`cursor`

```json
{
  "type": "cursor",
  "row": 4,
  "col": 2,
  "selection": {"row1": 4, "col1": 2, "row2": 6, "col2": 4}
}
```

`ping`

```json
{
  "type": "ping"
}
```

### Messaggi server -> client

`snapshot`

```json
{
  "type": "snapshot",
  "sheet_id": 12,
  "revision": 123,
  "cells": [
    {"row": 0, "col": 0, "value": "Acme SRL", "formula": null}
  ],
  "users": []
}
```

`revision_patch`

```json
{
  "type": "revision_patch",
  "sheet_id": 12,
  "revision": 124,
  "actor_user_id": 7,
  "patch": {
    "cells": [
      {"row": 0, "col": 0, "value": "Acme SRL", "formula": null}
    ]
  }
}
```

`cursor`

```json
{
  "type": "cursor",
  "user_id": 7,
  "row": 4,
  "col": 2,
  "selection": {"row1": 4, "col1": 2, "row2": 6, "col2": 4}
}
```

`presence`

```json
{
  "type": "presence",
  "users": [
    {"user_id": 7, "email": "user@example.com", "color": "#38bdf8"}
  ]
}
```

`error`

```json
{
  "type": "error",
  "code": "forbidden",
  "message": "Non puoi modificare questo foglio."
}
```

## Flusso Modifica Cella

1. Il browser manda `cell_patch`.
2. Il server valida payload, permessi, sheet e project.
3. Il server apre transazione Postgres.
4. Calcola la prossima revisione per il foglio.
5. Upsert delle celle in `project_sheet_cells`.
6. Insert in `project_sheet_revisions`.
7. Commit.
8. Dopo il commit pubblica su Redis.
9. Tutti i worker ricevono l'evento Redis.
10. Ogni worker inoltra via WebSocket ai browser collegati allo stesso foglio.

## Redis

Canali:

```text
sheet:{sheet_id}:events
sheet:{sheet_id}:presence
```

Chiavi presenza/cursori:

```text
sheet:{sheet_id}:presence:user:{user_id}
sheet:{sheet_id}:cursor:user:{user_id}
```

Usare TTL:

- presenza TTL 30 secondi;
- refresh ogni 10 secondi;
- alla disconnessione tentare cleanup esplicito;
- se il browser o worker cade, il TTL pulisce da solo.

Per MVP:

- Redis Pub/Sub per eventi live;
- Postgres revisioni per recupero affidabile.

Non usare Redis come storage permanente.

## Reconnect e Recupero Revisioni

Il client mantiene `last_revision`.

Al reconnect:

1. apre WebSocket;
2. manda `hello` con `last_revision`;
3. server legge da `project_sheet_revisions` tutte le revisioni successive;
4. se il gap e' piccolo, manda patch mancanti;
5. se il gap e' troppo grande, manda snapshot completo.

Questo risolve anche il limite di Redis Pub/Sub: se un messaggio live viene
perso, il DB resta autorevole.

## Frontend

Nel dettaglio fascicolo aggiungere sezione "Fogli".

Pagina foglio:

- griglia editabile;
- righe e colonne fisse per MVP, esempio 100 x 26;
- modifica celle testo;
- copia/incolla base;
- evidenziazione cella attiva;
- utenti online;
- cursori o selezioni colorate degli altri utenti;
- stato connessione: online, riconnessione, offline;
- salvataggio realtime senza pulsante "Salva".

Per MVP e' accettabile una griglia custom HTML/JS.

Valutare librerie solo se servono davvero:

- AG Grid Community: forte come data grid, meno come spreadsheet puro.
- Handsontable: ottima UX spreadsheet, ma verificare licenza.
- Univer/Luckysheet: piu' simili a suite spreadsheet, ma piu' pesanti.

## Validazione Payload

Limiti consigliati MVP:

- massimo 1000 righe x 100 colonne per foglio;
- massimo 500 celle per patch;
- massimo 20 KB per valore cella;
- formula disabilitata o trattata come testo in MVP;
- rifiutare indici negativi;
- rifiutare payload non JSON o troppo grandi.

## Sicurezza

Requisiti:

- ogni query deve essere tenant-scoped;
- ogni sheet deve appartenere al project indicato;
- ogni project deve essere visibile all'utente;
- ogni patch deve verificare permesso editor;
- viewer non deve poter inviare patch;
- non fidarsi di `user_id` mandato dal client;
- usare sempre l'utente autenticato lato server;
- proteggere export CSV da CSV formula injection.

Per CSV formula injection: se una cella esportata inizia con `=`, `+`, `-`, `@`,
prefissare con apostrofo o applicare una policy esplicita.

## Deploy Redis

Produzione consigliata:

- Redis Cloud oppure Upstash Redis;
- stessa region dell'app Argos;
- TLS abilitato se il provider lo supporta;
- `REDIS_URL` in env.

Redis su container/VPS e' accettabile per staging, ma non come scelta produzione
definitiva se si vuole alta affidabilita'.

## Criteri di Accettazione

- Due browser con due utenti sullo stesso foglio vedono modifiche in realtime.
- Un utente vede almeno la presenza degli altri utenti collegati.
- Viewer puo' leggere ma non modificare.
- Editor puo' modificare celle.
- Owner/architect puo' rinominare o archiviare il foglio.
- Se Redis non consegna un evento live, il reconnect recupera da Postgres.
- Se un browser si chiude male, la presenza sparisce dopo TTL.
- Le modifiche sono salvate in `project_sheet_revisions`.
- Tutte le query rispettano tenant e ACL progetto.
- Nessun contenuto del foglio viene scritto nella cartella locale del fascicolo,
  salvo export esplicito.

## Fasi di Implementazione

### Fase 1 - Modello dati e CRUD base

- Aggiungere tabelle in `app/db.py`.
- Creare `app/fascicoli/sheets_db.py`.
- Implementare create/list/get/rename/archive sheet.
- Implementare snapshot celle.
- Aggiungere sezione "Fogli" nel dettaglio fascicolo.

### Fase 2 - Griglia senza realtime

- Creare pagina foglio.
- Renderizzare griglia.
- Permettere modifica cella con HTTP/fetch.
- Salvare celle e revisioni.
- Testare permessi viewer/editor.

### Fase 3 - WebSocket singolo processo

- Implementare endpoint WebSocket.
- Implementare connection manager in memoria.
- Broadcast modifiche ai client collegati allo stesso worker.
- Gestire presence locale.

### Fase 4 - Redis realtime multi-worker

- Aggiungere Redis client async.
- Pubblicare eventi dopo commit DB.
- Listener Redis per worker.
- Inoltro eventi ai client locali.
- Presence con TTL Redis.

### Fase 5 - Reconnect robusto

- Client mantiene `last_revision`.
- Server invia patch mancanti da `project_sheet_revisions`.
- Snapshot completo se gap troppo grande.
- UI stato connessione/riconnessione.

### Fase 6 - Rifiniture

- Export CSV.
- Import CSV opzionale.
- Migliorare copy/paste.
- Cursori colorati.
- Test Playwright multi-browser.
- Test DB tenant isolation.

## Note Finali

La decisione architetturale centrale e' questa:

> i file del fascicolo restano locali; i fogli collaborativi sono asset online
> del progetto.

Questa distinzione evita ambiguita' con il principio privacy-first dei Fascicoli
e rende tecnicamente possibile la collaborazione realtime tra utenti.
