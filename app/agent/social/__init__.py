"""Social outreach module — invio DM a profili pubblici via browser automation.

Modulo orientato a Instagram, TikTok e (lettura) OnlyFans. Non importato
da nessun runner esistente: serve come libreria opzionale per il futuro
runner `outreach_social` (ancora da integrare). Lo sviluppo procede in
isolamento per non rompere il framework di scraping esistente.

Modulo organizzato per separazione di concerns:
- `humanize.py`         — funzioni di umanizzazione (mouse curves, typing delay)
- `session_manager.py`  — persistenza session_state Playwright per account
- `account_pool.py`     — rotation account con rate limit + health check
- `proxy_pool.py`       — assegnazione proxy residenziale per account (sticky)
- `platform_base.py`    — interfaccia astratta delle piattaforme
- `instagram.py`        — implementazione Instagram (login + DM)
- `tiktok.py`           — implementazione TikTok (login + DM)
- `crypto_creds.py`     — cifratura simmetrica credenziali (Fernet)

Sicurezza:
- Le credenziali account sono salvate in DB CIFRATE con chiave master in env
  (`ARGOS_SECRET`). Se la chiave non e' settata, il modulo rifiuta di
  funzionare.
- Le session_state Playwright sono salvate in `data/sessions/<account_id>.json`
  con permessi user-only (Windows ACL su Linux 600).
- I log NON includono mai password o token, solo username e contatori metrica.
"""
