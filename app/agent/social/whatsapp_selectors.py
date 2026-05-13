"""Selettori CSS per WhatsApp Web.

WA Web cambia spesso il DOM (test-id mantenuti meglio degli attributi css).
Tenere tutti i selettori QUI permette di aggiornarli senza toccare la logica
del runner. Se il login o l'invio si rompono, prima cosa: aprire web.whatsapp.com
da un browser, ispezionare i nuovi data-testid e aggiornare qui.

Fonte: web.whatsapp.com snapshot 2026-Q2. I selettori sono ridondanti per
robustezza: ogni costante è una *lista* in ordine di preferenza; il primo che
matcha vince.
"""
from __future__ import annotations


# === Login / QR ===

# Canvas del QR code (mostrato quando NON loggato). Aspetta solo dopo navigate.
QR_CANVAS = [
    'canvas[aria-label*="Scan"]',
    'canvas[aria-label*="QR"]',
    'div[data-ref] canvas',  # fallback: il QR è dentro un div con data-ref hash
]

# Bottone "Click to reload QR code" che appare dopo timeout del QR.
QR_REFRESH_BUTTON = [
    '[data-testid="qr-refresh-button"]',
    'button[aria-label*="ricarica"]',
    'button:has-text("Click to reload")',
]

# Indicatore "telefono richiesto a riconnettersi" (sessione scaduta lato mobile).
PHONE_REQUIRED = [
    'text="Phone not connected"',
    'text="Telefono non connesso"',
    '[data-testid="phone-not-connected"]',
]


# === App principale loggata ===

# Lista chat a sinistra — appare solo se loggato e sincronizzato.
CHAT_LIST = [
    '[data-testid="chat-list"]',
    '[aria-label="Chat list"]',
    '[aria-label="Elenco chat"]',
    '#pane-side',  # storico, fallback
]

# Header dell'app (search bar in alto a sinistra).
APP_HEADER = [
    '[data-testid="chatlist-header"]',
    'header[data-testid="chat-list-header"]',
]


# === Apertura chat via wa.me/send ===

# Quando navighi a /send?phone=N, dopo loading appare uno di:
# - il pannello chat aperto (numero valido + ha WhatsApp)
# - un alert "phone share number" che dice "phone number shared via url is not on WhatsApp"
# - dialog "use WhatsApp" con bottone "Continue to chat"
SEND_PHONE_INVALID = [
    'text="Phone number shared via url is invalid"',
    'text="Il numero di telefono condiviso nell\'URL non è valido"',
    'div[data-animate-modal-popup]:has-text("not on WhatsApp")',
    'div[data-animate-modal-popup]:has-text("non è su WhatsApp")',
]

# Dialog "Use WhatsApp in your browser" — appare a volte prima della chat.
USE_WEB_BUTTON = [
    'a:has-text("Use WhatsApp Web")',
    'a:has-text("Usa WhatsApp Web")',
    'button:has-text("Continue to chat")',
    'button:has-text("Continua nella chat")',
]


# === Composer / input messaggio ===

# Editor del messaggio (textbox contenteditable).
# WA usa data-tab="10" per il footer composer.
MESSAGE_INPUT = [
    '[data-testid="conversation-compose-box-input"]',
    'div[contenteditable="true"][data-tab="10"]',
    'div[contenteditable="true"][role="textbox"][data-tab]',
    'footer div[contenteditable="true"]',
]

# Bottone invio (a destra del composer). Spesso compare solo dopo aver typato.
SEND_BUTTON = [
    '[data-testid="send"]',
    'button[aria-label="Send"]',
    'button[aria-label="Invia"]',
    'span[data-icon="send"]',  # icona dentro il button
]

# Header della chat aperta (con il numero/nome del contatto). Conferma che la
# chat è aperta e pronta a ricevere typing.
CHAT_HEADER = [
    '[data-testid="conversation-header"]',
    'header[data-testid="conversation-header"]',
    'header div[role="button"]:has(img)',  # avatar nell'header
]


# === Conferma invio ===

# Spunte di stato sui messaggi outbound (nel chat history).
# 1 spunta grigia = sent / 2 spunte grigie = delivered / 2 blu = read
CHECKMARK_SENT = [
    'span[data-testid="msg-time"] [data-testid="msg-check"]',
    'span[data-icon="msg-check"]',
]
CHECKMARK_DELIVERED = [
    'span[data-testid="msg-time"] [data-testid="msg-dblcheck"]',
    'span[data-icon="msg-dblcheck"]',
]

# Indicatore di errore "messaggio non inviato".
MSG_FAILED = [
    'span[data-icon="error-out"]',
    'span[aria-label="Message failed"]',
    'span[aria-label="Messaggio non inviato"]',
]


# === Health / errori ===

# Splash "Connecting" (mostrato quando WA Web sta riconnettendosi dopo offline).
CONNECTING_SPLASH = [
    'div[data-testid="loading-splash"]',
    'div:has-text("Connecting...")',
    'div:has-text("In connessione...")',
]

# Ban / disconnect notice.
LOGOUT_OR_BAN = [
    'text="You\'ve been logged out"',
    'text="Sei stato disconnesso"',
    'text="Use WhatsApp on your phone"',
    'text="Usa WhatsApp dal telefono"',
]

# Updated/expired session.
SESSION_EXPIRED = [
    'text="Reload to continue"',
    'text="Ricarica per continuare"',
]


# === URLs ===

WA_WEB_URL = "https://web.whatsapp.com/"
WA_SEND_URL = "https://web.whatsapp.com/send?phone={digits}&text="
