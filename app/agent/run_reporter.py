"""Run reporter — produzione di `report.md` arricchito con diagnostica.

Single source of truth per:
- Struttura del report.md (header / metriche / fasi / diagnostica / suggerimenti / file output)
- Heuristiche di diagnostica per pattern di fallimento noti
- Suggerimenti concreti di tuning con field + valore esatti

Designato per essere usato da TUTTI i runner Argos (refactoring trasversale).
- v1: integrato in `runner_audience_discovery.py`.
- v2-v11: gli altri 10 runner uno alla volta nei commit successivi
  (recon_social, react, bulk_extract, auto_extract, site_explorer,
   qualifier, outreach, outreach_social, outreach_whatsapp, responder).

Pattern d'uso lato runner:

    reporter = RunReporter(task, job_id, run_dir)
    p1 = reporter.start_phase("keywords", description="Deduzione keyword dal brief")
    keywords = await llm_deduce_keywords(...)
    reporter.end_phase(p1, status="ok" if keywords else "empty",
                       items_in=1, items_out=len(keywords),
                       keywords=keywords)
    # ... altre fasi ...
    reporter.add_metric("saved_count", saved_count)
    reporter.write()  # produce run_dir/report.md
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


log = logging.getLogger(__name__)


PhaseStatus = Literal["ok", "empty", "error", "skipped", "running"]
Severity = Literal["critical", "warning", "info", "success"]


@dataclass
class PhaseResult:
    name: str
    description: str = ""
    status: PhaseStatus = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    items_in: int = 0
    items_out: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class Insight:
    """Una osservazione/raccomandazione prodotta dalla diagnostica.

    severity:
      - critical: blocca il successo del job (es. selettori obsoleti, LLM rotto)
      - warning: degrada il risultato (es. threshold troppo alto, dedup eccessivo)
      - info: contesto utile (es. cap raggiunto, fase saltata per scelta)
      - success: la fase è andata bene (UX positivo)

    suggestion: testo concreto con i campi+valori da modificare. Es:
      "Abbassa `recon_score_threshold` da 6 a 4 nel form del task."
    evidence: dati/numeri che hanno guidato la diagnosi (riportati nel report).
    """
    severity: Severity
    title: str
    description: str
    suggestion: str | None = None
    evidence: str | None = None


class RunReporter:
    """Accumula metriche, fasi, warning/error durante un run e produce
    `report.md` arricchito alla fine."""

    def __init__(
        self,
        task: dict[str, Any],
        job_id: int,
        run_dir: Path,
    ):
        self.task = task
        self.job_id = job_id
        self.run_dir = Path(run_dir)
        self.agent_mode = (task.get("agent_mode") or "unknown").strip()
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: datetime | None = None
        self.phases: list[PhaseResult] = []
        self.metrics: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.outputs: dict[str, str] = {}  # filename → description
        self.final_status: PhaseStatus = "running"

    # ------------------------------------------------------------------
    # API per il runner
    # ------------------------------------------------------------------

    def start_phase(self, name: str, *, description: str = "") -> PhaseResult:
        p = PhaseResult(name=name, description=description)
        self.phases.append(p)
        return p

    def end_phase(
        self,
        phase: PhaseResult,
        *,
        status: PhaseStatus,
        items_in: int = 0,
        items_out: int = 0,
        **details,
    ) -> None:
        phase.status = status
        phase.finished_at = datetime.now(timezone.utc)
        phase.items_in = items_in
        phase.items_out = items_out
        phase.details.update(details)

    def skip_phase(self, name: str, *, reason: str = "") -> None:
        """Helper per registrare una fase saltata (es. anchor vuoti)."""
        p = PhaseResult(name=name, description=reason, status="skipped")
        p.finished_at = p.started_at
        self.phases.append(p)

    def add_metric(self, key: str, value: Any) -> None:
        self.metrics[key] = value

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_output(self, filename: str, description: str) -> None:
        self.outputs[filename] = description

    def set_final_status(self, status: PhaseStatus) -> None:
        self.final_status = status

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def write(self, path: Path | None = None) -> Path:
        """Scrive il report.md sotto `run_dir/report.md` (override con `path`).
        Ritorna il path scritto."""
        if path is None:
            path = self.run_dir / "report.md"
        if self.finished_at is None:
            self.finished_at = datetime.now(timezone.utc)
        # Invoca dispatch heuristiche per agent_mode
        insights = diagnose(self.agent_mode, self)
        content = _render_markdown(self, insights)
        path.write_text(content, encoding="utf-8")
        return path


# ============================================================
# Rendering markdown
# ============================================================

_STATUS_EMOJI: dict[PhaseStatus, str] = {
    "ok": "✅",
    "empty": "⚠️",
    "error": "❌",
    "skipped": "⏭️",
    "running": "🔄",
}

_SEVERITY_EMOJI: dict[Severity, str] = {
    "critical": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "success": "✅",
}


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}min {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}min"


def _render_markdown(r: RunReporter, insights: list[Insight]) -> str:
    task = r.task
    lines: list[str] = []

    # Header
    lines.append(f"# Run report — task #{task.get('id')} · job #{r.job_id}")
    lines.append("")
    lines.append(f"**Task name**: {task.get('name', '(no name)')}")
    lines.append(f"**Agent mode**: `{r.agent_mode}`")
    lines.append(f"**Final status**: {_STATUS_EMOJI.get(r.final_status, '?')} `{r.final_status}`")
    lines.append("")
    lines.append(f"- **Iniziato**: {r.started_at.isoformat()}")
    lines.append(f"- **Finito**: {r.finished_at.isoformat() if r.finished_at else 'in corso'}")
    if r.finished_at:
        duration = (r.finished_at - r.started_at).total_seconds()
        lines.append(f"- **Durata**: {_fmt_duration(duration)}")
    lines.append("")

    # Brief / obiettivo (sempre presente)
    obj = (task.get("objective") or "").strip()
    if obj:
        lines.append("## Brief / obiettivo")
        lines.append("")
        # Tronca a 2000 char per evitare report enormi
        lines.append(obj[:2000] + ("..." if len(obj) > 2000 else ""))
        lines.append("")

    # Configurazione chiave (selezione di campi rilevanti — il dispatch
    # diagnose può arricchire ulteriormente in base al mode)
    lines.append("## Configurazione")
    lines.append("")
    config_pairs = _config_pairs_for_mode(task, r.agent_mode)
    if config_pairs:
        for k, v in config_pairs:
            lines.append(f"- **{k}**: `{v}`")
    else:
        lines.append("_(configurazione minima — vedi task per dettagli)_")
    lines.append("")

    # Risultato (metriche aggregate)
    if r.metrics:
        lines.append("## Risultati chiave")
        lines.append("")
        for k, v in r.metrics.items():
            lines.append(f"- **{k}**: `{v}`")
        lines.append("")

    # Fasi (tabella)
    if r.phases:
        lines.append("## Fasi")
        lines.append("")
        lines.append("| # | Fase | Status | Input | Output | Durata |")
        lines.append("|---|---|---|---|---|---|")
        for i, p in enumerate(r.phases, 1):
            emoji = _STATUS_EMOJI.get(p.status, "?")
            lines.append(
                f"| {i} | {p.name}"
                + (f" — {p.description}" if p.description else "")
                + f" | {emoji} {p.status}"
                + f" | {p.items_in} | {p.items_out} | {_fmt_duration(p.duration_s)} |"
            )
        lines.append("")

    # Diagnostica & Suggerimenti
    lines.append("## Diagnostica & Suggerimenti")
    lines.append("")
    if not insights:
        lines.append("_Nessuna diagnostica specifica — il runner non ha riconosciuto pattern noti per questa esecuzione._")
        lines.append("")
    else:
        # Ordina: critical → warning → info → success
        order = {"critical": 0, "warning": 1, "info": 2, "success": 3}
        sorted_ins = sorted(insights, key=lambda ix: order.get(ix.severity, 99))
        for ins in sorted_ins:
            emoji = _SEVERITY_EMOJI.get(ins.severity, "")
            lines.append(f"### {emoji} {ins.title}")
            lines.append("")
            lines.append(ins.description)
            if ins.evidence:
                lines.append("")
                lines.append(f"**Evidenza**: {ins.evidence}")
            if ins.suggestion:
                lines.append("")
                lines.append(f"**Azione**: {ins.suggestion}")
            lines.append("")

    # Warning/error grezzi (se ce ne sono, oltre agli insight strutturati)
    if r.warnings:
        lines.append("## Warning (log)")
        lines.append("")
        for w in r.warnings[:20]:
            lines.append(f"- {w}")
        if len(r.warnings) > 20:
            lines.append(f"- _(... e altri {len(r.warnings) - 20} warning, vedi job log)_")
        lines.append("")

    if r.errors:
        lines.append("## Errori (log)")
        lines.append("")
        for e in r.errors[:20]:
            lines.append(f"- {e}")
        if len(r.errors) > 20:
            lines.append(f"- _(... e altri {len(r.errors) - 20} errori, vedi job log)_")
        lines.append("")

    # File output
    if r.outputs:
        lines.append("## File output")
        lines.append("")
        for filename, desc in r.outputs.items():
            lines.append(f"- `{filename}` — {desc}")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(
        f"_Report generato da `RunReporter` v1 il {datetime.now(timezone.utc).isoformat()}_"
    )

    return "\n".join(lines)


def _config_pairs_for_mode(task: dict, agent_mode: str) -> list[tuple[str, Any]]:
    """Selezione di campi config rilevanti da mostrare nel report, dipendente
    dal mode. Più sono pochi e mirati, più leggibile è il report."""
    common = [
        ("model", task.get("model") or "(default)"),
        ("llm_provider", task.get("llm_provider") or "ollama"),
    ]
    if agent_mode == "audience_discovery":
        return common + [
            ("social_platform", task.get("social_platform") or "facebook"),
            ("recon_social_account_id", task.get("recon_social_account_id")),
            ("recon_max_targets_per_day", task.get("recon_max_targets_per_day") or 50),
            ("recon_score_threshold", task.get("recon_score_threshold") or 6),
            ("speed_profile", task.get("speed_profile") or "safe"),
            ("refresh_policy_days", task.get("refresh_policy_days") or 7),
            ("anchor_profiles_count", len(task.get("seed_queries") or [])),
        ]
    if agent_mode == "recon_social":
        return common + [
            ("recon_mode", task.get("recon_mode") or "url_driven"),
            ("social_platform", task.get("social_platform")),
            ("recon_social_account_id", task.get("recon_social_account_id")),
            ("seed_count", len(task.get("seed_queries") or [])),
            ("speed_profile", task.get("speed_profile") or "safe"),
        ]
    if agent_mode in ("bulk_extract", "auto_extract", "browser_use", "site_explorer", "react"):
        return common + [
            ("seed_count", len(task.get("seed_queries") or [])),
            ("extraction_template", task.get("extraction_template")),
            ("max_iterations", task.get("max_iterations")),
        ]
    if agent_mode in ("outreach", "outreach_social", "outreach_whatsapp"):
        return common + [
            ("audience_asset_count", len(task.get("target_asset_ids") or [])),
            ("social_platform", task.get("social_platform")),
            ("max_dms_per_run", task.get("max_dms_per_run")),
        ]
    return common


# ============================================================
# Diagnostica — dispatch per agent_mode
# ============================================================

def diagnose(agent_mode: str, reporter: RunReporter) -> list[Insight]:
    """Dispatch alle heuristiche specifiche per agent_mode.

    Nuovi runner: aggiungi qui il mapping mode → funzione `diagnose_<mode>`.
    """
    dispatch = {
        "audience_discovery": _diagnose_audience_discovery,
    }
    fn = dispatch.get(agent_mode, _diagnose_default)
    try:
        return fn(reporter)
    except Exception as exc:
        log.exception("diagnose() failed for mode %s", agent_mode)
        return [Insight(
            severity="warning",
            title="Errore interno della diagnostica",
            description=f"Lo script di diagnosi ha sollevato un'eccezione: {exc}",
            suggestion="Bug in app/agent/run_reporter.py — segnala lo stack trace.",
        )]


def _diagnose_default(r: RunReporter) -> list[Insight]:
    """Diagnostica generica quando l'agent_mode non ha heuristiche dedicate.
    Produce solo summary basico delle fasi."""
    insights: list[Insight] = []
    n_critical_phases = sum(1 for p in r.phases if p.status == "error")
    if n_critical_phases:
        insights.append(Insight(
            severity="critical",
            title=f"{n_critical_phases} fase/i in errore",
            description="Il runner ha riportato fasi con status='error'. Vedi log per dettagli.",
        ))
    if not r.phases:
        insights.append(Insight(
            severity="info",
            title="Nessuna fase registrata",
            description=(
                "Questo runner non ha ancora l'integrazione con RunReporter. "
                "Il report mostra solo le metriche raw. Vedi commit di refactoring "
                "trasversale in roadmap."
            ),
        ))
    return insights


# ------------------------------------------------------------------
# Heuristiche per audience_discovery (v1)
# ------------------------------------------------------------------

# Modelli noti per essere thinking-mode (output vuoto via Ollama OpenAI compat)
_THINKING_MODELS_HINTS = (
    "qwen3.6", "qwen3-thinking", "qwen-thinking",
    "deepseek-r1", "deepseek-thinking",
    "-thinking",
)


def _is_thinking_model(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _THINKING_MODELS_HINTS)


def _diagnose_audience_discovery(r: RunReporter) -> list[Insight]:
    insights: list[Insight] = []
    phases_by_name = {p.name: p for p in r.phases}
    task = r.task
    model = (task.get("model") or "").strip()

    # ── FASE 1: keywords ──
    kw = phases_by_name.get("keywords")
    if kw:
        if kw.status == "empty" or (kw.status == "ok" and kw.items_out == 0):
            # Diagnostica thinking-mode
            if _is_thinking_model(model):
                insights.append(Insight(
                    severity="critical",
                    title="LLM in thinking-mode → content vuoto in Fase 1",
                    description=(
                        f"Il modello `{model}` sembra essere thinking-mode. Tutto il "
                        "ragionamento finisce in `message.thinking` invisibile via "
                        "Ollama OpenAI compat (`/v1/chat/completions`); il campo "
                        "`message.content` resta vuoto, e la deduzione keyword "
                        "ritorna 0 risultati."
                    ),
                    suggestion=(
                        f"Cambia il campo **`model`** del task #{task.get('id')} da "
                        f"`{model}` a **`qwen3-coder:30b`** (locale, preferito) oppure "
                        "`gpt-oss:20b` (locale) oppure `gpt-4o-mini` (cloud). Vedi "
                        "`_PLANNER_MANUAL` sezione \"Scelta del campo model\"."
                    ),
                    evidence=(
                        f"Fase 1 ha prodotto {kw.items_out} keyword nonostante "
                        "brief non vuoto. Modello matcha pattern thinking-mode noti."
                    ),
                ))
            else:
                insights.append(Insight(
                    severity="critical",
                    title="LLM ha ritornato 0 keyword in Fase 1",
                    description=(
                        f"Il modello `{model}` non ha prodotto keyword utilizzabili. "
                        "Possibili cause: server LLM giù, timeout, prompt rifiutato "
                        "per content policy, JSON malformato non parsabile."
                    ),
                    suggestion=(
                        "Verifica Ollama running con `curl http://localhost:11434/api/tags`. "
                        "Prova chiamata diretta con il prompt del runner. Se OK, "
                        "considera un modello alternativo: `qwen3-coder:30b` o `gpt-4o-mini`."
                    ),
                ))
        elif kw.status == "ok" and kw.items_out > 0:
            keywords = kw.details.get("keywords") or []
            insights.append(Insight(
                severity="success",
                title=f"Fase 1 OK: {kw.items_out} keyword dedotte",
                description=f"Brief tradotto in: `{keywords}`.",
            ))

    # ── FASE 2: anchor_friends ──
    af = phases_by_name.get("anchor_friends")
    if af and af.status == "skipped":
        # è normale se l'utente non ha messo anchor — non flagga
        pass
    elif af and af.items_in > 0 and af.items_out == 0:
        insights.append(Insight(
            severity="warning",
            title=f"Fase 2: 0 amici visibili dai {af.items_in} anchor",
            description=(
                "Hai impostato anchor profili ma la friend list non è visibile "
                "(probabilmente impostata su \"Solo amici\" per ciascuno)."
            ),
            suggestion=(
                "Verifica gli anchor a mano: apri ogni URL del seed e controlla se "
                "la sezione \"Amici\" è pubblica. Se no, rimuovili dal seed_queries "
                "e affidati solo alla ricerca per gruppi (Fase 3)."
            ),
        ))

    # ── FASE 3: search_groups ──
    sg = phases_by_name.get("search_groups")
    if sg:
        if sg.items_in > 0 and sg.items_out == 0:
            insights.append(Insight(
                severity="critical",
                title="Fase 3: 0 gruppi trovati nonostante keyword presenti",
                description=(
                    f"Su {sg.items_in} keyword cercate, nessun gruppo è stato estratto. "
                    "Probabilmente i selettori `GROUP_RESULT_SELECTORS` in "
                    "`app/agent/social/facebook_audience.py` sono obsoleti, "
                    "oppure l'URL `/search/groups/?q=...` non rende risultati per "
                    "l'account loggato."
                ),
                suggestion=(
                    "Apri manualmente https://www.facebook.com/search/groups/?q=<keyword> "
                    "nel browser dell'account loggato. Se vedi gruppi, ispeziona la "
                    "struttura DOM e aggiorna `GROUP_RESULT_SELECTORS` (file: "
                    "`app/agent/social/facebook_audience.py`, ~riga 58)."
                ),
            ))
        elif sg.items_out > 0:
            insights.append(Insight(
                severity="success",
                title=f"Fase 3: {sg.items_out} gruppi unici trovati",
                description=f"Da {sg.items_in} keyword di ricerca.",
            ))

    # ── FASE 4: open_groups ──
    og = phases_by_name.get("open_groups")
    if og:
        if og.items_in > 0 and og.items_out == 0:
            insights.append(Insight(
                severity="critical",
                title="Fase 4: 0 autori estratti dai gruppi aperti",
                description=(
                    f"Aperti {og.items_in} gruppi ma nessun autore raccolto. "
                    "Selettore del link autore nei post probabilmente obsoleto. "
                    "Possibili cause: FB ha cambiato struttura DOM dei post; "
                    "i gruppi aperti sono tutti closed/private."
                ),
                suggestion=(
                    "Apri uno dei gruppi (vedi `recon_audit_log.jsonl` per gli URL) "
                    "e ispeziona la struttura DOM di un post. Aggiorna "
                    "`AUTHOR_LINK_SELECTORS_IN_POST` (file: "
                    "`app/agent/social/facebook_audience.py`, ~riga 305)."
                ),
            ))
        elif og.items_in > 0 and og.items_out > 0:
            n_groups = og.items_in
            n_authors = og.items_out
            zero_groups = og.details.get("zero_author_groups", 0)
            if zero_groups > n_groups / 2:
                insights.append(Insight(
                    severity="warning",
                    title=f"Fase 4: {zero_groups}/{n_groups} gruppi non hanno reso autori",
                    description=(
                        "Più di metà dei gruppi aperti ha prodotto 0 autori. "
                        "Probabilmente sono gruppi privati (membership richiesta) "
                        "o gruppi con post tutti media (no testo) per l'account loggato."
                    ),
                    suggestion=(
                        "Per il prossimo run: filtra in `facebook_audience.search_groups` "
                        "i gruppi che non hai joined (richiede check separato sul DOM "
                        "del card risultato). Oppure aumenta il pool di keyword nel brief "
                        "per pescare più gruppi candidate."
                    ),
                ))
            else:
                insights.append(Insight(
                    severity="success",
                    title=f"Fase 4: {n_authors} candidati raccolti da {n_groups} gruppi",
                    description=f"Media {n_authors / n_groups:.1f} candidati per gruppo aperto.",
                ))

    # ── FASE 5: scoring ──
    sc = phases_by_name.get("scoring")
    if sc:
        scores = sc.details.get("score_distribution") or []
        saved = sc.details.get("saved", 0)
        skipped_low = sc.details.get("skipped_low_score", 0)
        skipped_dedup = sc.details.get("skipped_dedup", 0)
        extract_fail = sc.details.get("extract_fail", 0)
        threshold = int(task.get("recon_score_threshold") or 6)

        if sc.items_in == 0:
            insights.append(Insight(
                severity="warning",
                title="Fase 5: 0 candidati da scorare",
                description=(
                    "Nessun candidato è arrivato alla Fase 5 di scoring. La "
                    "pipeline si è bloccata in Fase 2/3/4 (vedi insight sopra)."
                ),
            ))
        elif scores:
            avg = sum(scores) / len(scores)
            mn, mx = min(scores), max(scores)
            median = sorted(scores)[len(scores) // 2]
            if saved == 0 and skipped_low > 0 and avg < threshold:
                suggested = max(1, int(median))
                insights.append(Insight(
                    severity="warning",
                    title=f"Fase 5: score medio {avg:.1f} sotto threshold {threshold} — 0 saved",
                    description=(
                        f"Su {len(scores)} candidati scorati: min={mn}, max={mx}, "
                        f"median={median}, avg={avg:.1f}. Nessuno raggiunge il "
                        f"threshold attuale ({threshold})."
                    ),
                    suggestion=(
                        f"Abbassa il campo **`recon_score_threshold`** da "
                        f"`{threshold}` a **`{suggested}`** per recuperare i "
                        f"candidati borderline. In alternativa: arricchisci "
                        f"l'`objective` con segnali più specifici (parole-chiave "
                        f"che l'LLM userà per scoring più indulgente)."
                    ),
                    evidence=f"score distribution: {sorted(scores)}",
                ))
            elif saved > 0:
                insights.append(Insight(
                    severity="success",
                    title=f"Fase 5: {saved} profili salvati",
                    description=(
                        f"Su {sc.items_in} candidati: {saved} matched (score≥{threshold}), "
                        f"{skipped_low} sotto threshold, {skipped_dedup} dedup, "
                        f"{extract_fail} extract failed. Score medio: {avg:.1f}."
                    ),
                ))

        if sc.items_in > 0 and skipped_dedup / max(sc.items_in, 1) > 0.5:
            refresh = int(task.get("refresh_policy_days") or 7)
            new_refresh = refresh * 4 if refresh > 0 else 30
            insights.append(Insight(
                severity="warning",
                title=f"Fase 5: {skipped_dedup}/{sc.items_in} candidati skippati per dedup",
                description=(
                    f"Più di metà dei candidati erano già in DB (refresh_policy_days={refresh}). "
                    "Il task sta scoprendo solo profili nuovi rispetto agli ultimi run."
                ),
                suggestion=(
                    f"Per riprocessare anche profili recenti: alza **`refresh_policy_days`** "
                    f"da `{refresh}` a **`{new_refresh}`** oppure imposta **`-1`** "
                    "(sempre re-scrape, ignora dedup)."
                ),
            ))

        if sc.items_in > 0 and extract_fail / max(sc.items_in, 1) > 0.3:
            insights.append(Insight(
                severity="warning",
                title=f"Fase 5: {extract_fail}/{sc.items_in} profili con extract_fail",
                description=(
                    "Più del 30% dei profili candidate non si è aperto correttamente. "
                    "Possibili cause: profili privati (richiedono follow/friend); "
                    "FB ha cambiato DOM del profilo (`facebook_recon.extract_profile_data` "
                    "selettori obsoleti)."
                ),
                suggestion=(
                    "Apri 2-3 profili falliti (vedi `recon_audit_log.jsonl` EXTRACT_FAIL) "
                    "e verifica se sono privati. Se sono pubblici, aggiorna i selettori "
                    "in `app/agent/social/facebook_recon.py`."
                ),
            ))

    # ── Cap audience ──
    saved_total = r.metrics.get("saved_count") or 0
    cap = int(task.get("recon_max_targets_per_day") or 50)
    if saved_total >= cap:
        insights.append(Insight(
            severity="info",
            title=f"Cap audience raggiunto ({cap})",
            description=(
                f"Il runner ha salvato {saved_total} profili e si è fermato per il cap."
            ),
            suggestion=(
                f"Per raccogliere più profili: alza **`recon_max_targets_per_day`** "
                f"da `{cap}` a **`{cap * 2}`**. ⚠️ Attento al rischio ban — "
                "usa account warmup. Considera anche `speed_profile=safe` "
                "(default pause 30-180s) per ridurre l'esposizione."
            ),
        ))

    return insights
