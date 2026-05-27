"""B-008: cifra at-rest le LLM API key dei task ancora in chiaro.

One-time, idempotente. La cifratura at-rest è già attiva sui NUOVI salvataggi di
task (create/update) e la lettura ha fallback sui valori legacy in chiaro: questo
script serve solo a "ripulire" i task ESISTENTI senza aspettare un loro re-save.

Uso:
    python scripts/encrypt_task_keys.py            # cifra sul DB attivo (.env / override)

Opera sul DB risolto dall'app (DATABASE_URL / override /dbconfig). Per cifrare su
Neon, attiva l'override Neon PRIMA di lanciarlo (e ricorda: è una scrittura su
prod — fallo consapevolmente). Richiede ARGOS_SECRET in .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from app import db
    from app.secrets_util import is_secret_configured

    if not is_secret_configured():
        print("[ERROR] ARGOS_SECRET non configurata: impossibile cifrare. Settala in .env.")
        return 1

    print("Cifratura at-rest LLM API key sui task (DB attivo da .env / override)…")
    n = db.encrypt_legacy_task_keys()
    print(f"[OK] {n} chiavi-task cifrate (0 = già tutte cifrate / nessuna chiave plaintext).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
