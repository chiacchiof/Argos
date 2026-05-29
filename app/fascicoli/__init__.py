"""Argos Fascicoli — modulo di gestione documentale operativa per le PMI.

Vedi `docs/argos_fascicoli_design.md` per il design completo.

Layout:
- fs.py     -> helpers filesystem (manifest, hash file, scan, sanitize nome).
- db.py     -> CRUD tenant-scoped su projects/project_users/project_files.
- sync.py   -> reconciliation filesystem -> registro DB.

Le route web vivono in `app.routes.fascicoli`. Templates in `app/templates/`.
"""
