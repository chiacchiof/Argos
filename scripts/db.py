"""Comandi DB: status / new / migrate / promote.

Wrapper user-friendly su Alembic + safety checks. Workflow tipico:

    # 1. crea revisione vuota per il nuovo schema change
    python scripts/db.py new "add priority column to tasks"

    # 2. edita alembic/versions/XXXX_*.py (upgrade/downgrade a mano)
    # 3. applica su locale + pytest
    python scripts/db.py migrate

    # 4. quando soddisfatto, applica su Neon (prod) — USA IL WRAPPER:
    pwsh scripts/deploy_to_neon.ps1
    #    (oppure `python scripts/db.py promote` SOLO se sei sicuro che la DSN
    #     risolta sia davvero Neon — vedi caveat in scripts/README.md sezione
    #     "DSN Neon": l'override `data/db_config.enc` ha priorità massima e
    #     puo' silenziosamente puntare al locale.)

Vedi scripts/README.md per dettagli + esempi.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Carica .env (per AGENTSCRAPER_SECRET, necessario per decifrare data/db_config.enc).
# NOTA: NON importiamo app.config qui, perché triggerebbe apply_override() che
# sovrascrive os.environ["DATABASE_URL"] e confonderebbe il calcolo del DSN locale.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------

def _resolve_local_dsn() -> str:
    """DSN del DB locale dev (default da .env, override file cifrato escluso).

    Il `.env` ha `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentscraper_dev`
    come default. Lo leggiamo direttamente senza apply_override (altrimenti
    finirebbe puntando a Neon se l'utente ha attivo l'override /dbconfig).
    """
    from dotenv import dotenv_values

    env = dotenv_values(PROJECT_ROOT / ".env")
    dsn = (env.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise SystemExit(
            "[ERROR] DATABASE_URL non trovata in .env. Lo script `db.py migrate` "
            "richiede una DSN locale esplicita in .env (non legge l'override "
            "/dbconfig per evitare di applicare migrations su prod per errore)."
        )
    return dsn


def _resolve_neon_dsn() -> str:
    """DSN di Neon (prod). Priorita':

    1. NEON_DATABASE_URL (env var esplicita, settata da deploy_to_neon.ps1)
    2. DBCONFIG_PRESET_NEON_DSN (env var del preset /dbconfig, sempre Neon)
    3. c:/tmp/neon_url.txt (file legacy)
    4. data/db_config.enc (override /dbconfig — ULTIMA opzione perche' puo'
       contenere local DSN se l'utente ha switchato per testing)

    Safety check: rifiuta DSN che contengono 'localhost' / '127.0.0.1' / '@db'
    senza porta cloud — prevent applicazione di promote/copy verso local per errore.
    """
    candidates: list[tuple[str, str]] = []

    # 1) NEON_DATABASE_URL esplicito
    env_url = (os.environ.get("NEON_DATABASE_URL") or "").strip()
    if env_url:
        candidates.append(("NEON_DATABASE_URL env", env_url))

    # 2) DBCONFIG_PRESET_NEON_DSN (lo stesso preset usato dalla UI /dbconfig)
    preset_url = (os.environ.get("DBCONFIG_PRESET_NEON_DSN") or "").strip()
    if preset_url:
        candidates.append(("DBCONFIG_PRESET_NEON_DSN env", preset_url))

    # 3) File tmp
    tmp_file = Path("c:/tmp/neon_url.txt")
    if tmp_file.exists():
        content = tmp_file.read_text(encoding="utf-8").strip()
        if content:
            candidates.append(("c:/tmp/neon_url.txt", content))

    # 4) /dbconfig override (RISCHIOSO: puo' essere local se l'utente ha switchato)
    try:
        from app import _runtime_db_override

        data = _runtime_db_override.read_override()
        if data and data.get("database_url"):
            candidates.append(("/dbconfig override (data/db_config.enc)", data["database_url"]))
    except Exception:
        pass

    # Filtra candidati che puntano a localhost (sicurezza)
    safe_candidates = []
    rejected_local = []
    for source, dsn in candidates:
        if "localhost" in dsn or "127.0.0.1" in dsn:
            rejected_local.append((source, dsn))
            continue
        safe_candidates.append((source, dsn))

    if rejected_local:
        for source, _ in rejected_local:
            print(f"[WARN] DSN da '{source}' rifiutata: punta a localhost (non Neon).", file=sys.stderr)

    if not safe_candidates:
        raise SystemExit(
            "[ERROR] Nessuna DSN Neon valida trovata (non-localhost). Setta una delle:\n"
            "   - $env:NEON_DATABASE_URL=postgresql://...neon.tech/...\n"
            "   - DBCONFIG_PRESET_NEON_DSN in .env (usato dal dropdown /dbconfig)\n"
            "   - c:/tmp/neon_url.txt"
        )

    source, dsn = safe_candidates[0]
    print(f"[INFO] Neon DSN risolta da: {source}", file=sys.stderr)
    return dsn


def _mask(dsn: str) -> str:
    import re

    return re.sub(r"(postgres(?:ql)?://[^:]+:)([^@]+)(@)", r"\1****\3", dsn)


# ---------------------------------------------------------------------------
# Alembic helpers (in-process, no subprocess CLI)
# ---------------------------------------------------------------------------

def _alembic_cfg():
    from alembic.config import Config

    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _alembic_current_version(dsn: str) -> str | None:
    """Legge la version_num corrente dalla tabella alembic_version."""
    import psycopg

    try:
        with psycopg.connect(dsn) as c:
            row = c.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None
    except psycopg.errors.UndefinedTable:
        return None
    except Exception as exc:
        return f"<ERR: {exc}>"


def _alembic_heads() -> list[str]:
    """Lista delle revision heads (di solito 1, ma 2+ se ci sono branches)."""
    from alembic.script import ScriptDirectory

    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    return [r.revision for r in script.get_revisions("head")]


def _alembic_upgrade(dsn: str, target: str = "head") -> None:
    """Esegue alembic upgrade in-process puntando alla DSN data."""
    from alembic import command

    cfg = _alembic_cfg()
    # env.py rispetta os.environ["DATABASE_URL"] se settata
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        command.upgrade(cfg, target)
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def _alembic_stamp(dsn: str, revision: str) -> None:
    """Marca la DB a una revision SENZA eseguire le migration. Utile quando
    lo schema esiste gia' (creato da `init_db()` raw) ma manca `alembic_version`
    — situazione tipica di Neon dopo un reset/restore non-alembic.
    """
    from alembic import command

    cfg = _alembic_cfg()
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        command.stamp(cfg, revision)
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def _schema_present(dsn: str) -> bool:
    """True se almeno una tabella business esiste nello schema public (es. tasks).
    Heuristic per capire se Neon e' davvero "vuoto" o ha lo schema senza alembic_version."""
    import psycopg

    try:
        with psycopg.connect(dsn) as c:
            row = c.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='tasks' LIMIT 1"
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _alembic_history() -> list[tuple[str, str]]:
    """Lista (revision, description) ordinata da più vecchia a più nuova."""
    from alembic.script import ScriptDirectory

    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    revs = list(script.walk_revisions())
    revs.reverse()  # oldest first
    return [(r.revision, (r.doc or "").splitlines()[0] if r.doc else "") for r in revs]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    """Mostra alembic version di locale + Neon, e revision head del repo."""
    local_dsn = _resolve_local_dsn()
    try:
        neon_dsn = _resolve_neon_dsn()
    except SystemExit as e:
        neon_dsn = None
        neon_err = str(e)

    head = _alembic_heads()
    head_str = ", ".join(head) if head else "(nessuna revisione nel repo)"

    print("=" * 70)
    print("DB STATUS")
    print("=" * 70)
    print(f"Repo HEAD revision: {head_str}")
    print()
    print(f"LOCALE ({_mask(local_dsn)})")
    print(f"  alembic_version: {_alembic_current_version(local_dsn) or '(nessuna)'}")
    print()
    if neon_dsn:
        print(f"NEON   ({_mask(neon_dsn)})")
        print(f"  alembic_version: {_alembic_current_version(neon_dsn) or '(nessuna)'}")
    else:
        print(f"NEON: {neon_err}")
    print()

    # Verifica drift
    if neon_dsn:
        v_loc = _alembic_current_version(local_dsn)
        v_neon = _alembic_current_version(neon_dsn)
        if v_loc == v_neon:
            print(f"[OK] Locale e Neon allineati a {v_loc or 'nessuna revisione'}.")
        else:
            print(f"[WARN]  DRIFT: locale={v_loc}  neon={v_neon}")
        if head and v_loc not in head:
            print(f"[WARN]  Locale NON è a head ({head_str}). Esegui `db.py migrate`.")
    return 0


def cmd_new(args) -> int:
    """Crea una nuova revision Alembic con il messaggio dato."""
    from alembic import command

    message = args.message.strip()
    if not message:
        print("[ERROR] Messaggio obbligatorio.")
        return 1

    # Branch check (warning non bloccante)
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if branch in ("main", "master"):
            print(f"[WARN]  Sei su `{branch}`. Convenzione: crea un branch dedicato")
            print("    per il cambio schema prima di generare la revision.")
            print(f"    Esempio: git checkout -b feature/{message.lower().replace(' ', '-')}")
            if not args.yes:
                resp = input("Procedere comunque? [y/N] ").strip().lower()
                if resp != "y":
                    print("Aborted.")
                    return 1
    except Exception:
        pass

    cfg = _alembic_cfg()
    command.revision(cfg, message=message)
    print()
    print("[OK] Revision creata. Prossimi passi:")
    print(f"  1. Edita il file appena generato in alembic/versions/")
    print(f"     (scrivi upgrade() e downgrade() a mano: NO autogenerate)")
    print(f"  2. python scripts/db.py migrate")
    return 0


def cmd_migrate(args) -> int:
    """Applica alembic upgrade head su LOCALE + esegue pytest."""
    local_dsn = _resolve_local_dsn()

    print(f"[1/3] alembic upgrade head su LOCALE ({_mask(local_dsn)})...")
    try:
        _alembic_upgrade(local_dsn)
    except Exception as exc:
        print(f"[ERROR] Upgrade locale FALLITO: {exc}")
        return 1
    print(f"      -> ora a {_alembic_current_version(local_dsn)}")
    print()

    if args.skip_tests:
        print("[2/3] pytest SKIPPATO (--skip-tests)")
    else:
        print("[2/3] pytest...")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=PROJECT_ROOT,
        )
        if r.returncode != 0:
            print(f"[ERROR] Pytest FALLITO. Risolvi i test prima di procedere a promote.")
            return r.returncode
        print("      -> tutti i test passano")
    print()

    print("[3/3] OK locale aggiornato e testato.")
    print()
    print("Prossimo step (quando sei pronto a deployare in prod):")
    print("  pwsh scripts/deploy_to_neon.ps1")
    print()
    print("  (Wrapper safe per Neon: nasconde temporaneamente l'override")
    print("   `data/db_config.enc` e forza la DSN Neon vera. `db.py promote`")
    print("   diretto va usato SOLO se sei sicuro che la DSN risolta sia Neon —")
    print("   vedi scripts/README.md sezione 'DSN Neon' per i caveat.)")
    return 0


def cmd_promote(args) -> int:
    """Applica alembic upgrade head su NEON (prod) dopo safety checks."""
    local_dsn = _resolve_local_dsn()
    neon_dsn = _resolve_neon_dsn()

    # 1) Locale deve essere a head
    head = _alembic_heads()
    if not head:
        print("[ERROR] Nessuna revisione Alembic nel repo. Nulla da promuovere.")
        return 1
    v_local = _alembic_current_version(local_dsn)
    if v_local not in head:
        print(f"[ERROR] LOCALE non è a head ({head}). Locale attuale: {v_local}.")
        print(f"   Esegui prima: python scripts/db.py migrate")
        return 1

    # 2) Pytest gating (strict by default)
    if args.skip_tests:
        print("[WARN]  pytest SKIPPATO (--skip-tests)")
    else:
        print("[1/4] pytest gating...")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=PROJECT_ROOT,
        )
        if r.returncode != 0:
            print(f"[ERROR] Pytest FALLITO. Promote bloccato.")
            print(f"   Usa --skip-tests SOLO se sai cosa stai facendo.")
            return r.returncode
        print("      -> tutti i test passano")
    print()

    # 3) Mostra diff revisioni pending
    v_neon = _alembic_current_version(neon_dsn)
    schema_present_on_neon = _schema_present(neon_dsn)
    print(f"[2/4] Stato attuale:")
    print(f"  LOCALE: {v_local}")
    print(f"  NEON:   {v_neon} (schema presente: {schema_present_on_neon})")
    if v_local == v_neon:
        print(f"  -> già allineati. Niente da promuovere.")
        return 0

    # CASO SPECIALE: Neon ha lo schema ma manca alembic_version (es. reset+init_db
    # raw senza alembic). Non possiamo fare upgrade (CREATE TABLE fallirebbe).
    # Usiamo `alembic stamp` per marcare la DB alla revision di local senza
    # ri-applicare le migration: lo schema gia' c'e' (creato da SCHEMA_SQL raw),
    # alembic_version viene popolato a v_local. Le revision pending saranno 0.
    if v_neon is None and schema_present_on_neon:
        print()
        print(f"[!] Neon ha lo schema ma manca alembic_version.")
        print(f"    Probabilmente lo schema e' stato creato da `init_db()` raw (non via alembic).")
        print(f"    Soluzione: STAMP della revision corrente di local ({v_local}) su Neon")
        print(f"    SENZA ri-applicare le migration (lo schema e' gia' presente).")
        print()
        if not args.yes:
            resp = input(f"  Eseguire `alembic stamp {v_local}` su Neon? [y/N] ").strip().lower()
            if resp != "y":
                print("Aborted.")
                return 1
        try:
            _alembic_stamp(neon_dsn, v_local)
        except Exception as exc:
            print(f"[ERROR] Stamp fallito: {exc}")
            return 1
        v_neon_final = _alembic_current_version(neon_dsn)
        print(f"[OK] Neon ora a alembic_version = {v_neon_final}.")
        print(f"     Schema gia' presente, niente da migrare.")
        return 0

    # Lista revisioni pending (between v_neon and v_local)
    history = _alembic_history()
    pending = []
    seen_neon = False
    for rev, desc in history:
        if rev == v_neon:
            seen_neon = True
            continue
        if seen_neon or v_neon is None:
            pending.append((rev, desc))
        if rev in head:
            break

    print(f"\n  Revisioni pending (da applicare su Neon): {len(pending)}")
    for rev, desc in pending:
        print(f"    - {rev}: {desc}")
    print()

    # 4) Conferma + apply
    if not args.yes:
        print(f"[3/4] CONFERMA")
        print(f"  Sto per applicare {len(pending)} revision(i) su NEON ({_mask(neon_dsn)}).")
        resp = input("  Procedere? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 1

    print(f"\n[4/4] alembic upgrade head su NEON...")
    try:
        _alembic_upgrade(neon_dsn)
    except Exception as exc:
        print(f"[ERROR] Upgrade Neon FALLITO: {exc}")
        print(f"   ATTENZIONE: locale è a {v_local} ma Neon potrebbe essere in stato intermedio.")
        print(f"   Verifica con: python scripts/db.py status")
        return 1

    v_neon_final = _alembic_current_version(neon_dsn)
    print(f"      -> ora a {v_neon_final}")
    print()
    print(f"[OK] PROMOTE COMPLETATO. Locale e Neon entrambi a {v_neon_final}.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMANDO")

    p_status = sub.add_parser("status", help="Mostra alembic version locale + Neon + head repo")

    p_new = sub.add_parser("new", help="Crea una nuova revision Alembic")
    p_new.add_argument("message", help="Descrizione breve (es. 'add priority column to tasks')")
    p_new.add_argument("--yes", "-y", action="store_true", help="No prompt branch warning")

    p_migrate = sub.add_parser("migrate", help="alembic upgrade head su LOCALE + pytest")
    p_migrate.add_argument("--skip-tests", action="store_true", help="Salta pytest (sconsigliato)")

    p_promote = sub.add_parser("promote", help="alembic upgrade head su NEON (con safety checks)")
    p_promote.add_argument("--skip-tests", action="store_true", help="Salta pytest gating (uso emergenza)")
    p_promote.add_argument("--yes", "-y", action="store_true", help="Salta prompt conferma")

    args = p.parse_args()
    funcs = {"status": cmd_status, "new": cmd_new, "migrate": cmd_migrate, "promote": cmd_promote}
    return funcs[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
