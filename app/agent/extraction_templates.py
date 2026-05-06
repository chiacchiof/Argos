"""Template di schema per l'estrazione browser-use.

Ogni template fornisce un blocco di testo che viene iniettato nel system prompt
dell'agente: descrive cosa cercare, come riconoscere la pagina giusta e quale
schema JSON usare per ogni riga di profiles.jsonl.

L'utente può scegliere un template e poi modificarlo liberamente, oppure
partire da "custom" e scrivere uno schema da zero.
"""
from __future__ import annotations

from typing import TypedDict


class Template(TypedDict):
    name: str
    description: str
    schema: str


PROFILE_CONTACTS = """\
OBIETTIVO: identificare PAGINE-PROFILO PUBBLICHE (modella, annuncio personale, escort, freelance,
professionista) e raccogliere i loro contatti pubblici, per poter ricontattare i proprietari delle
pagine e proporre ottimizzazioni dei contenuti.

COME RICONOSCERE LA PAGINA:
- URL univoco tipo /<slug>, /user/<id>, /modella/<x>, /annuncio/<id>, /profilo/<x>, /<nome-cognome>
- Descrizione narrativa di UNA singola persona/attività (non una lista)
- Info di contatto specifiche (telefono, social, email, whatsapp, telegram)
- NON sono profili: home, listing/catalogo, pagina di categoria, FAQ, blog, area riservata, checkout.

CAMPI DA ESTRARRE (schema JSON, UNA riga per profilo in profiles.jsonl):
{
  "url": "URL canonico della pagina-profilo",
  "username": "nickname / handle / nome utente visibile (string|null)",
  "display_name": "nome mostrato in pagina, se diverso (string|null)",
  "email": "email anche se offuscata es. 'nome [at] dominio' (string|null)",
  "whatsapp": "numero o link wa.me (string|null)",
  "telegram": "handle @x o link t.me (string|null)",
  "social": [{"platform": "instagram|facebook|twitter|tiktok|...", "url": "..."}],
  "sitoweb": "URL del sito personale se diverso da source_domain (string|null)",
  "altri_contatti": ["string libero per contatti aggiuntivi"],
  "source_url": "URL della pagina (ridondante per facilità di filtri)",
  "source_domain": "host del source_url (es. example.com)",
  "page_title": "<title> della pagina",
  "meta_description": "valore di <meta name='description'> (string|null)",
  "lang": "codice ISO-639 del contenuto (es. 'it', 'en')",
  "estratto": "primi ~500 char di testo principale, plain text, no HTML",
  "crawled_at": "ISO-8601 UTC al momento dell'estrazione"
}
"""

ECOMMERCE_PRODUCTS = """\
OBIETTIVO: identificare PAGINE-PRODOTTO di un sito e-commerce.

COME RICONOSCERE LA PAGINA:
- URL del tipo /product/<id>, /<slug>, /shop/<x>, /p/<id>, /it/<categoria>/<slug>
- Mostra UN singolo prodotto con prezzo, descrizione, foto, bottone "aggiungi al carrello"
- NON sono prodotto: home, listing/categoria, blog, checkout, account.

CAMPI DA ESTRARRE (schema JSON, UNA riga per prodotto in profiles.jsonl):
{
  "url": "URL canonico",
  "sku": "codice prodotto / SKU se visibile (string|null)",
  "name": "nome prodotto",
  "price_amount": "numero (float) o null",
  "price_currency": "EUR|USD|GBP|... (string|null)",
  "availability": "in_stock|out_of_stock|preorder|unknown",
  "category": "string|null",
  "brand": "string|null",
  "description": "primi ~500 char della descrizione (string|null)",
  "images": ["url1", "url2"],
  "rating_avg": "numero (0-5) o null",
  "reviews_count": "int|null",
  "shipping_info": "string|null",
  "source_url": "...",
  "source_domain": "...",
  "page_title": "<title>",
  "meta_description": "string|null",
  "lang": "codice ISO-639",
  "crawled_at": "ISO-8601 UTC"
}
"""

REAL_ESTATE = """\
OBIETTIVO: identificare ANNUNCI IMMOBILIARI di vendita o affitto.

COME RICONOSCERE LA PAGINA:
- URL del tipo /annuncio/<x>, /immobile/<id>, /property/<id>
- Mostra UN singolo immobile con prezzo, metri quadri, locali, foto, dati agenzia
- NON sono annunci: home, ricerca, mappa, lista risultati.

CAMPI DA ESTRARRE (schema JSON, UNA riga per annuncio in profiles.jsonl):
{
  "url": "URL canonico",
  "tipo": "vendita|affitto|asta|nuova_costruzione",
  "categoria": "appartamento|villa|ufficio|terreno|negozio|...",
  "prezzo_eur": "numero (float) o null",
  "metri_quadri": "numero o null",
  "locali": "int|null",
  "bagni": "int|null",
  "piano": "string|null (es. '2', 'piano terra')",
  "anno_costruzione": "int|null",
  "classe_energetica": "A|B|C|D|E|F|G (string|null)",
  "indirizzo": "string|null",
  "citta": "string|null",
  "cap": "string|null",
  "agenzia": "string|null",
  "telefono_agenzia": "string|null",
  "email_agenzia": "string|null",
  "agente_referente": "string|null",
  "data_pubblicazione": "ISO-8601 o null",
  "descrizione": "primi ~500 char",
  "images": ["url1", "url2"],
  "source_url": "...",
  "source_domain": "...",
  "page_title": "<title>",
  "lang": "codice ISO-639",
  "crawled_at": "ISO-8601 UTC"
}
"""

EVENTS = """\
OBIETTIVO: identificare PAGINE-EVENTO (concerti, conferenze, mostre, sport, teatro).

COME RICONOSCERE LA PAGINA:
- URL del tipo /event/<id>, /evento/<x>, /<data>/<slug>
- Mostra UN singolo evento con data, luogo, descrizione, link biglietti
- NON sono eventi: home, calendario, lista, categoria.

CAMPI DA ESTRARRE (schema JSON, UNA riga per evento in profiles.jsonl):
{
  "url": "URL canonico",
  "title": "nome evento",
  "start_datetime": "ISO-8601 o null",
  "end_datetime": "ISO-8601 o null",
  "timezone": "es. 'Europe/Rome' (string|null)",
  "venue": "nome luogo (string|null)",
  "address": "indirizzo (string|null)",
  "city": "string|null",
  "country": "codice ISO-3166 alpha-2 (string|null)",
  "organizer": "string|null",
  "category": "musica|teatro|conferenza|sport|mostra|...",
  "ticket_url": "string|null",
  "ticket_price_min_eur": "numero o null",
  "ticket_price_max_eur": "numero o null",
  "is_free": "bool|null",
  "is_sold_out": "bool|null",
  "description": "primi ~500 char",
  "images": ["url1"],
  "source_url": "...",
  "source_domain": "...",
  "page_title": "<title>",
  "lang": "codice ISO-639",
  "crawled_at": "ISO-8601 UTC"
}
"""

NEWS_ARTICLES = """\
OBIETTIVO: identificare ARTICOLI giornalistici / post di blog.

COME RICONOSCERE LA PAGINA:
- URL del tipo /articolo/<slug>, /<anno>/<mese>/<slug>, /news/<id>
- Ha autore, data di pubblicazione, corpo testo lungo
- NON sono articoli: home, sezione, archivio, tag, autore.

CAMPI DA ESTRARRE (schema JSON, UNA riga per articolo in profiles.jsonl):
{
  "url": "URL canonico",
  "title": "titolo articolo",
  "author": "string|null",
  "published_at": "ISO-8601 o null",
  "updated_at": "ISO-8601 o null",
  "category": "string|null",
  "tags": ["..."],
  "summary": "primi ~500 char di sommario / corpo (string|null)",
  "word_count": "int|null",
  "reading_time_min": "int|null",
  "images": ["url1"],
  "comments_count": "int|null",
  "source_url": "...",
  "source_domain": "...",
  "page_title": "<title>",
  "meta_description": "string|null",
  "lang": "codice ISO-639",
  "crawled_at": "ISO-8601 UTC"
}
"""

JOB_LISTINGS = """\
OBIETTIVO: identificare ANNUNCI DI LAVORO.

COME RICONOSCERE LA PAGINA:
- URL del tipo /jobs/<id>, /lavoro/<x>, /annuncio-lavoro/<slug>
- Mostra UN singolo annuncio con titolo ruolo, azienda, requisiti, modalità di candidatura
- NON sono annunci: home, ricerca, lista risultati, pagina azienda.

CAMPI DA ESTRARRE (schema JSON, UNA riga per annuncio in profiles.jsonl):
{
  "url": "URL canonico",
  "title": "titolo posizione",
  "company": "string|null",
  "company_url": "string|null",
  "location": "string|null",
  "remote_policy": "remote|hybrid|onsite|unknown",
  "employment_type": "full_time|part_time|contract|internship|unknown",
  "salary_min_eur": "numero o null",
  "salary_max_eur": "numero o null",
  "experience_level": "entry|mid|senior|lead|null",
  "requirements_summary": "primi ~500 char dei requisiti",
  "responsibilities_summary": "primi ~500 char delle mansioni (string|null)",
  "apply_url": "string|null",
  "contact_email": "string|null",
  "posted_at": "ISO-8601 o null",
  "source_url": "...",
  "source_domain": "...",
  "page_title": "<title>",
  "lang": "codice ISO-639",
  "crawled_at": "ISO-8601 UTC"
}
"""

CUSTOM_PLACEHOLDER = """\
OBIETTIVO: descrivi qui cosa vuoi estrarre.

COME RICONOSCERE LA PAGINA:
- elenca i criteri (URL pattern, contenuto distintivo, ecc.)

CAMPI DA ESTRARRE (schema JSON, UNA riga per ogni pagina valida in profiles.jsonl):
{
  "url": "...",
  "field1": "...",
  "field2": "...",
  "source_url": "...",
  "source_domain": "...",
  "page_title": "...",
  "lang": "...",
  "crawled_at": "ISO-8601 UTC"
}
"""


TEMPLATES: dict[str, Template] = {
    "profile_contacts": {
        "name": "Profili con contatti",
        "description": "Pagine personali (modelle, annunci, freelance) con contatti pubblici",
        "schema": PROFILE_CONTACTS,
    },
    "ecommerce_products": {
        "name": "Prodotti e-commerce",
        "description": "Pagine-prodotto con prezzo, SKU, disponibilità, immagini",
        "schema": ECOMMERCE_PRODUCTS,
    },
    "real_estate": {
        "name": "Annunci immobiliari",
        "description": "Annunci di vendita/affitto con prezzo, mq, ubicazione, agenzia",
        "schema": REAL_ESTATE,
    },
    "events": {
        "name": "Eventi",
        "description": "Pagine di concerti, conferenze, mostre con data, luogo, biglietti",
        "schema": EVENTS,
    },
    "news_articles": {
        "name": "Articoli e blog post",
        "description": "Articoli editoriali con autore, data, corpo testo",
        "schema": NEWS_ARTICLES,
    },
    "job_listings": {
        "name": "Annunci di lavoro",
        "description": "Posizioni aperte con azienda, ruolo, requisiti, salary",
        "schema": JOB_LISTINGS,
    },
    "custom": {
        "name": "Personalizzato (vuoto)",
        "description": "Parti da uno schema vuoto e scrivilo da zero",
        "schema": CUSTOM_PLACEHOLDER,
    },
}

DEFAULT_TEMPLATE = "profile_contacts"


def get_schema(key: str | None) -> str:
    """Ritorna il testo dello schema per la chiave; default se sconosciuta."""
    if not key or key not in TEMPLATES:
        key = DEFAULT_TEMPLATE
    return TEMPLATES[key]["schema"]


def list_templates() -> list[dict]:
    """Lista ordinata di template per la UI."""
    return [
        {"key": k, "name": v["name"], "description": v["description"]}
        for k, v in TEMPLATES.items()
    ]
