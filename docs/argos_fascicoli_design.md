# Argos Fascicoli — Design v1

> **Stato:** bozza per revisione. Non implementare prima dell'OK esplicito sui punti
> contrassegnati con `[DA CONFERMARE]`. Le sezioni "Roadmap" e "Aperte" sono di
> partenza, non vincolanti.

---

## Indice

1. [Premessa e obiettivi](#1-premessa-e-obiettivi)
2. [Principio architetturale](#2-principio-architetturale)
3. [Concetti e terminologia](#3-concetti-e-terminologia)
4. [Modello dati (Neon)](#4-modello-dati-neon)
5. [Flussi operativi](#5-flussi-operativi)
6. [Permessi e CRUD](#6-permessi-e-crud)
7. [Vista architect: consumo spazio](#7-vista-architect-consumo-spazio)
8. [Roadmap MVP per stadi](#8-roadmap-mvp-per-stadi)
9. [Perimetro v1 / fuori scope](#9-perimetro-v1--fuori-scope)
10. [Domande aperte residue](#10-domande-aperte-residue)

---

## 1. Premessa e obiettivi

**Argos Fascicoli** è il modulo di gestione documentale operativa per le PMI servite
da Argos. Permette a un utente o a un team di:

- **Organizzare** documenti aziendali per progetto/pratica (commesse, clienti,
  fornitori, fascicoli interni).
- **Interrogarli** in linguaggio naturale (Q&A su contenuti, riassunti, estrazione
  strutturata, confronti, generazione di output operativi).
- **Tenere il controllo della propria base documentale**: i file fisici restano
  sul PC del cliente, dove erano. Argos li legge, non li sposta.

L'obiettivo strategico è offrire un'alternativa **locale, multi-utente e
tenant-aware** a strumenti tipo NotebookLM / chat-su-PDF cloud, sfruttando la
posizione differenziante di Argos (privacy-first, gestita da fornitori
tecnici per conto dei clienti finali).

---

## 2. Principio architetturale

**Il fascicolo è una cartella fisica sul disco del cliente, non un record opaco
nel nostro DB.** Da questo derivano cinque scelte cementate:

| # | Scelta | Conseguenza pratica |
|---|---|---|
| 1 | Filesystem = fonte di verità | Se l'utente cancella un file da Esplora Risorse, il fascicolo perde quel file. Niente "fantasmi" nel DB. |
| 2 | `.argos/` sotto ogni progetto contiene **solo materiale rigenerabile** | Cancellare `.argos/` non fa perdere nulla: indici, embeddings, riassunti vengono ricostruiti dai file sorgente. |
| 3 | Backup/sync gestiti dall'OS | OneDrive, NAS, Dropbox: l'utente sceglie. Argos non implementa backup proprio. |
| 4 | Portabilità totale come argomento di vendita | "Se Argos chiude domani, i tuoi documenti restano sul tuo PC, organizzati come sono." |
| 5 | LLM intercambiabile | Ollama locale per fascicoli sensibili, provider remoto per ragionamenti complessi su materiale non sensibile. Decisione del fornitore/utente, non vincolo architetturale. |

**Vantaggio narrativo distillato (per i fornitori che vendono Argos):**

> *"I tuoi documenti restano nella tua cartella, sul tuo PC. Argos li legge per te.
> Nel cloud passano solo i metadati per coordinare gli utenti del tuo team: nomi,
> dimensioni, struttura — mai un byte di contenuto."*

---

## 3. Concetti e terminologia

### 3.1 RootProject

Cartella di primo livello configurata da ciascun **utente** al primo utilizzo
("dove vuoi che Argos tenga i tuoi fascicoli?"). È un setting per-utente, salvato
nel profilo lato applicazione. Tutte le sottocartelle dirette della RootProject
sono fascicoli candidati.

**Default suggerito:** `Documents/Argos/` (Windows), `~/Documenti/Argos/` (macOS/Linux).

> **Decisione di scope:** v1 → una sola RootProject per installazione/utente.
> Multiple root (es. "Studio" + "Personale") rimandate a una v2 se emergerà
> il bisogno.

### 3.2 Fascicolo / Progetto

Una **sottocartella diretta della RootProject** che contiene:

- I file di lavoro dell'utente (PDF, DOCX, TXT, MD, EML/MSG, ...) in qualsiasi
  organizzazione (sotto-sottocartelle libere).
- Una directory **`.argos/`** con materiale derivato:
  - `manifest.json` con l'UUID del progetto (per binding al record DB).
  - Indice testuale (chunk + posizioni).
  - Embeddings (vector store su file, vedi [§10](#10-domande-aperte-residue)).
  - Riassunti precalcolati per documento e per progetto.
  - Cache strutturata di estrazioni (scadenze, importi, ecc.).
  - Log conversazioni della chat sul progetto.

**Nome cartella = nome progetto.** Nessuna mappatura nascosta. L'utente può
rinominare la cartella da Esplora Risorse: Argos riconcilia tramite l'UUID nel
`manifest.json`.

### 3.3 Stato di un fascicolo

| Stato | Significato | UX |
|---|---|---|
| **completo** | Il PC corrente vede la cartella fisica → chat e azioni attive | Card normale, badge "Su questo PC" |
| **monco** | Il record DB esiste, ma la cartella fisica non è su questo PC → vedi metadati, niente chat | Card grigia, badge "File su [hostname noto, se disponibile]" |
| **orfano** | La cartella fisica c'è ma nessun record DB corrispondente (es. spostata da un altro PC senza connessione) | Card "Da riconciliare" con CTA "Collega a un progetto esistente" o "Promuovi a nuovo progetto" |

### 3.4 Visibilità: Tenant-Use vs User-Use

| Visibilità | Chi vede il progetto | Default suggerito |
|---|---|---|
| **Tenant-Use** | Tutti gli utenti del tenant | Per fascicoli condivisi (cliente, commessa aziendale) |
| **User-Use** | Solo il creatore + utenti del tenant esplicitamente condivisi | Per fascicoli personali, bozze, sperimentazioni |

La visibilità si imposta alla creazione ed è modificabile (CRUD). User-Use
ammette condivisione granulare verso altri utenti del tenant tramite
`project_users` (vedi §4).

> Nota: la visibilità riguarda chi può **vedere l'esistenza e i metadati** del
> progetto. La capacità di **interagire con la chat sui contenuti** dipende
> sempre dall'avere i file fisicamente sul proprio PC.

---

## 4. Modello dati (Neon)

Tutte le nuove tabelle sono tenant-scoped come il resto dello schema. Riusano
`tenants(id)` e `users(id)` già definite in [app/db_cloud.py](app/db_cloud.py).

### 4.1 `projects`

```sql
CREATE TABLE projects (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  owner_user_id   BIGINT NOT NULL REFERENCES users(id)   ON DELETE RESTRICT,
  folder_uuid     UUID NOT NULL UNIQUE,         -- match con .argos/manifest.json
  title           TEXT NOT NULL,                 -- nome visualizzato (= nome cartella alla creazione)
  description     TEXT,
  visibility      TEXT NOT NULL CHECK (visibility IN ('tenant', 'user')),
  is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_projects_tenant_visibility ON projects(tenant_id, visibility);
CREATE INDEX idx_projects_owner            ON projects(owner_user_id);
```

### 4.2 `project_users` (ACL per User-Use)

```sql
CREATE TABLE project_users (
  project_id    BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id       BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
  role          TEXT NOT NULL CHECK (role IN ('viewer', 'editor')),
  added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (project_id, user_id)
);
```

- `role = 'viewer'`: vede metadati e può interrogare la chat se ha i file localmente.
- `role = 'editor'`: viewer + può aggiungere/rimuovere file dal proprio PC e modificarne i metadati (titolo, descrizione).
- L'owner ha sempre pieno controllo (non serve riga in `project_users`).
- Per progetti **Tenant-Use** la tabella resta vuota: i permessi sono impliciti dal tenant.

### 4.3 `project_files`

Registro **solo metadati** dei file presenti in un progetto. Mai contenuto.

```sql
CREATE TABLE project_files (
  id              BIGSERIAL PRIMARY KEY,
  project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  relative_path   TEXT NOT NULL,             -- es. "contratti/2026/Acme-NDA.pdf"
  name            TEXT NOT NULL,             -- nome file
  size_bytes      BIGINT NOT NULL,
  content_hash    TEXT,                       -- sha256 — per rilevare modifiche
  mime_type       TEXT,
  added_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  mtime           TIMESTAMPTZ,
  last_indexed_at TIMESTAMPTZ,                -- quando il PC d'origine ha aggiornato l'indice
  UNIQUE (project_id, relative_path)
);

CREATE INDEX idx_project_files_project ON project_files(project_id);
```

### 4.4 Marker locale `.argos/manifest.json`

```json
{
  "argos_project_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_slug": "studio-rossi",
  "created_at": "2026-05-28T14:00:00Z",
  "schema_version": 1
}
```

Sufficiente per il binding cartella ↔ record DB. Lo `schema_version` permette
evoluzioni future. Il `tenant_slug` è ridondante (l'autorità è il DB) ma utile
per detection di "cartella di un tenant diverso da quello loggato".

---

## 5. Flussi operativi

### 5.1 Setup iniziale (prima volta)

1. L'utente apre Argos e va su `/fascicoli` (o equivalente).
2. Se la `RootProject` non è ancora configurata per l'utente corrente: prompt
   "Dove vuoi che Argos tenga i tuoi fascicoli?" con default suggerito.
3. Argos crea la cartella se non esiste e salva il path nel profilo utente.
4. Da qui in avanti la lista fascicoli è la scansione di `RootProject/*/` filtrata
   per quelle che hanno `.argos/manifest.json` con UUID corrispondente a un
   `projects.folder_uuid` visibile all'utente.

### 5.2 Creazione di un nuovo progetto

1. L'utente clicca "Nuovo fascicolo", inserisce:
   - Titolo (= nome cartella sul disco; default sanitizzato)
   - Descrizione opzionale
   - Visibilità (Tenant-Use / User-Use)
2. Argos:
   - Genera un `UUID`.
   - Crea la cartella `<RootProject>/<Titolo>/` + `.argos/manifest.json`.
   - `INSERT INTO projects (...)` con `folder_uuid = <UUID>`.
3. Il progetto compare come **completo** (la cartella è su questo PC).

### 5.3 Aggiunta/rimozione/modifica file

L'utente lavora normalmente con Esplora Risorse oppure usa la UI di Argos
(drag-and-drop in browser). In entrambi i casi:

1. Un **watcher locale** del processo Argos rileva l'evento `created/modified/deleted`
   nella cartella del progetto.
2. Per ogni evento:
   - Calcola `size_bytes` + `content_hash`.
   - Aggiorna `project_files` su Neon (insert/update/delete).
   - Triggera reindicizzazione asincrona del `.argos/` (chunking, embedding, riassunto).
3. La sincronizzazione è **push event-driven**. Al startup di Argos viene fatta
   una scansione di riconciliazione completa per recuperare eventi persi mentre
   l'app era spenta.

### 5.4 Login + scoperta progetti

1. Login utente (cookie session via `SessionMiddleware`, già esistente).
2. Query: progetti visibili al tenant + progetti User-Use posseduti o condivisi.
3. Per ciascun progetto, Argos guarda la `RootProject` dell'utente sul PC
   corrente:
   - Cerca una sottocartella con `manifest.json.argos_project_uuid` == `projects.folder_uuid`.
   - Match → **completo**.
   - Nessun match → **monco**.
4. La UI mostra i progetti con badge di stato; sui **monchi** la chat è
   disabilitata e c'è un messaggio chiaro: *"I file di questo progetto non
   sono su questo PC. Apri Argos dal PC dove sono stati caricati."*

### 5.5 Spostamento progetto / cambio PC (v1)

In v1 lo spostamento è **manuale**: l'utente copia la cartella su un altro PC
(o un altro path) e al boot Argos la trova tramite UUID. Nessun supporto
automatico di "trasferimento attivo" tra installazioni — quello entra nella
discussione cloud-sync v2.

---

## 6. Permessi e CRUD

| Azione | Owner | Editor (User-Use) | Viewer (User-Use) | Altro utente del tenant (Tenant-Use) | Architect del tenant | Super-admin |
|---|---|---|---|---|---|---|
| Vedi progetto | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Apri chat (se cartella su PC) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Aggiungi/rimuovi file | ✓ | ✓ | ✗ | ✓ (Tenant-Use) | ✓ | ✓ |
| Modifica titolo/descrizione | ✓ | ✓ | ✗ | ✗ | ✓ | ✓ |
| Cambia visibilità Tenant↔User | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ |
| Aggiungi/rimuovi membri (User-Use) | ✓ | ✗ | ✗ | n/a | ✓ | ✓ |
| Trasferisci ownership | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ |
| Archivia / elimina progetto | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ |
| Vedi consumo spazio (vista architect) | n/a | n/a | n/a | n/a | ✓ | ✓ |

Eliminazione = soft delete (`is_archived = TRUE`) + `.argos/` mantenuto. La
cartella fisica con i file dell'utente non viene mai toccata dall'app.

---

## 7. Vista architect: consumo spazio

L'architect del tenant accede a una vista riepilogativa (probabilmente sotto
`/admin/fascicoli` o nella dashboard architect) con:

- Lista progetti del tenant (Tenant-Use + User-Use altrui — l'architect ha
  "supervisione architetturale" sull'intero tenant).
- Per ciascun progetto: titolo, owner, n. file, dimensione totale, ultimo
  aggiornamento, stato (completo su quanti PC noti / orfano / monco).
- Drill-down per progetto: lista file con dimensioni.

Le dimensioni vivono in `project_files.size_bytes` aggiornate dal watcher: la
vista architect è una **query aggregata** sul DB Neon, funziona anche se i PC
con i file sono offline.

> Nota privacy: l'architect vede **nomi file**, dimensioni e struttura. Non vede
> contenuti né conversazioni. Da formalizzare nel "Manifesto Privacy Argos"
> menzionato nella narrativa di vendita.

---

## 8. Roadmap MVP per stadi

Ogni stadio è uno **stato spendibile**: dimostrabile a un fornitore, vendibile a
un cliente. Tempi indicativi.

### Stadio 1 — Indicizzazione + Q&A (settimane 1-3)

- Creazione progetto, RootProject, marker `.argos/`.
- Schema DB Neon nuovo + migration.
- Watcher filesystem locale + sync metadati verso DB.
- Ingestion: PDF, DOCX, TXT, MD (libreria es. `unstructured` o `langchain` loader; **[DA CONFERMARE]**).
- Chunking + embedding locali (Ollama embedding model; **[DA CONFERMARE]** quale).
- Vector store su file dentro `.argos/` (es. `sqlite` con `sqlite-vss`, o LanceDB, o ChromaDB embedded; **[DA CONFERMARE]**).
- Chat per progetto: Q&A retrieval-augmented, basata sull'orchestrator
  esistente con un nuovo `agent_mode = "fascicolo_qa"` o equivalente.

**Stato fine stadio**: l'utente carica documenti → fa domande → ottiene risposte
con citazioni dei file sorgente.

### Stadio 2 — Riassunti (settimane 3-4)

- Riassunto on-demand per documento singolo.
- Riassunto progetto ("Dimmi a che punto siamo su questo fascicolo").
- Cache dei riassunti in `.argos/` con invalidazione su modifica file.

### Stadio 3 — Estrazione strutturata (settimane 4-6)

- Template di estrazione riusabili (es. "scadenze contrattuali", "importi e
  parti", "obblighi del fornitore"). Si appoggia al pattern `extraction_template`
  già presente in `app/agent/extraction_templates.py`.
- Output: tabella esportabile, eventualmente sincronizzabile con calendario o
  task interni (decidere se in stadio 3 o 4).

### Stadio 4 — Confronto + generazione output (da settimana 6)

- Confronto multi-documento ("differenze fra questo contratto e quello standard").
- Generazione di output operativi: checklist da capitolato, bozza di mail da
  riassunto di scambio con cliente, riepilogo settimanale.
- Integrazione con altri moduli Argos (es. "manda questa bozza via outreach_whatsapp").

---

## 9. Perimetro v1 / fuori scope

| Dentro v1 | Fuori v1 (rimandato a v2 o oltre) |
|---|---|
| Una RootProject per utente | Multiple root per utente |
| Detection PC giusto via UUID | Sync attiva contenuti tra PC del tenant |
| Visibilità Tenant-Use / User-Use con CRUD | Permessi a livello di singolo file (ACL fine-grained) |
| Watcher event-driven + scansione di riconciliazione | Conflict resolution su modifica concorrente da PC diversi |
| Vista architect con dimensioni | Vista architect con telemetria sull'utilizzo (chi ha chiesto cosa) |
| Ingestion PDF/DOCX/TXT/MD/EML | XLSX, PPTX, immagini con OCR, DWG, P7M (PEC) |
| Q&A + riassunto + estrazione + confronto/generazione (4 stadi sopra) | Generazione documenti complessi (es. nuova versione di contratto) |
| Cloud sync = solo metadati | Cloud sync contenuti (per smart working multi-sede) |
| Embeddings locali via Ollama | Embeddings remoti come opzione esplicita del fornitore |

---

## 10. Domande aperte residue

Decisioni che si possono prendere durante lo Stadio 1, ma vanno chiuse prima
del codice. Per ciascuna metto un'opzione preferita come punto di partenza,
non come decisione presa.

| # | Domanda | Opzione di partenza | Note |
|---|---|---|---|
| Q1 | Formati v1 oltre a PDF/DOCX/TXT/MD? | Aggiungere **EML/MSG** subito (mail = fonte abbondante per le PMI italiane) | XLSX/PPTX/OCR → backlog. P7M (PEC) → valutare in v1.5 se serve a fornitori specifici. |
| Q2 | Libreria di parsing PDF | `pypdf` per inizio (puro Python, no deps native pesanti); fallback `unstructured` se qualità insufficiente | Evitare deps che richiedono Visual Studio Build Tools su Windows. |
| Q3 | Modello embedding | `nomic-embed-text` via Ollama (locale, no costi, qualità sufficiente) | Configurabile dal fornitore. Memorizzare il modello usato nel chunk metadata per rebuild deterministici. |
| Q4 | Vector store | `sqlite` con `sqlite-vss` dentro `.argos/` (zero deps esterne, file unico) | Alternative: LanceDB, Chroma embedded. Decidere su prototipo. |
| Q5 | Chunking strategy | `RecursiveCharacterTextSplitter` 1000/200 come baseline | Da affinare per tipo documento (mail vs contratto). |
| Q7 | RootProject default path su Windows | `%USERPROFILE%\Documents\Argos\` | Configurabile, ma il default deve essere senza spazi se possibile (no `Programmi`). |
| Q8 | Hash file: sha256 di tutto o solo testa+coda+dim? | Sha256 completo (al chunking si legge comunque); cache risultato | Su file molto grossi (>100MB) considerare hash incrementale. |
| Q9 | Comportamento su file enorme (>500MB) | Soft cap configurabile per-tenant; oltre il cap l'ingestion è skip + warning | Evita timeout/OOM con LLM locali. |
| Q10 | Path stoccaggio `.argos/` su OneDrive | Funziona "by accident": Argos non sa che è OneDrive. Documentare che la `.argos/` può essere esclusa dalla sync OneDrive per non sprecare banda | Aggiungere una nota in USER_GUIDE. |

---

## Appendice A — Stile schema

Le migration seguiranno il pattern stabilito da
[`alembic/versions/dd9f4fc12f91_chat_conversations_*.py`](alembic/versions/dd9f4fc12f91_chat_conversations_for_operator_with_.py):

- Raw SQL con `CREATE TABLE IF NOT EXISTS` (idempotente, coesiste con `init_db()`).
- Indici espliciti su FK e su colonne usate in filtri tenant.
- `downgrade()` non distruttiva quando possibile.
- Branch git dedicato + `python scripts/db.py new "..."` + `migrate` + revisione manuale prima di `promote` su Neon (vedi memoria *DB schema workflow*).

---

## Appendice B — Confine cloud / locale

| Tipo di dato | Dove vive |
|---|---|
| Identità utenti, tenant, ruoli | Neon |
| Registro progetti (`projects`) | Neon |
| ACL progetti (`project_users`) | Neon |
| Metadati file (`project_files`: nome, dimensione, hash, mtime) | Neon |
| Conversazioni della chat sui fascicoli | Neon (per consistenza con `chat_conversations` esistente) |
| Contenuto dei file (PDF, DOCX, ...) | **Solo filesystem locale** |
| Chunk testuali estratti | **Solo `.argos/` locale** |
| Embeddings | **Solo `.argos/` locale** |
| Riassunti / estrazioni generate dall'LLM | **Solo `.argos/` locale** |
| Log conversazione raw (con citazioni del contenuto) | **Solo `.argos/` locale** |

Questo confine è il **Manifesto Privacy Argos**. Ogni nuovo elemento che si
aggiunge a Neon va valutato contro questa tabella e giustificato.
