"""Account pool: rotation, rate limiting, health tracking.

Stato in-memory durante una sessione, persistito su DB tabella `social_accounts`
(la cui migration e' predisposta in `migrations/008_social_accounts.sql`,
applicata quando il framework e' libero da job in corso).

Logica di rotation:
- Ogni account ha `daily_dm_cap` (default 10) e contatore `dms_today`
- Round-robin tra account `active` con `dms_today < cap`
- Account in stato `quarantine` o `banned` esclusi
- Quarantine: 7 giorni post-challenge; auto-recovery dopo
- Health: ogni N DM (5 default), check_health → se != OK setta status
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .platform_base import HealthStatus, SocialAccount

log = logging.getLogger(__name__)


@dataclass
class AccountRuntime:
    """Stato runtime di un account durante la sessione (non persistito)."""
    account: SocialAccount
    dms_today: int = 0
    consecutive_challenges: int = 0
    last_dm_at: datetime | None = None
    last_health: HealthStatus = HealthStatus.OK
    in_use: bool = False


class AccountPool:
    """Pool di account social con round-robin e health tracking."""

    def __init__(self, accounts: list[SocialAccount]):
        self._runtimes: dict[str, AccountRuntime] = {
            a.uuid: AccountRuntime(account=a) for a in accounts
        }
        self._rr_index = 0
        self._order = list(self._runtimes.keys())

    @property
    def total(self) -> int:
        return len(self._runtimes)

    def by_uuid(self, uuid: str) -> AccountRuntime | None:
        return self._runtimes.get(uuid)

    def acquire_next(self, platform: str | None = None) -> AccountRuntime | None:
        """Round-robin: ritorna il prossimo account utilizzabile.

        Filtra per piattaforma + cap residuo + status active. None se nessuno
        disponibile (es. tutti gli account hanno raggiunto il cap giornaliero).
        """
        n = len(self._order)
        for _ in range(n):
            uuid = self._order[self._rr_index % n]
            self._rr_index += 1
            rt = self._runtimes[uuid]
            if rt.in_use:
                continue
            if rt.account.status != "active":
                continue
            if platform and rt.account.platform != platform:
                continue
            if rt.dms_today >= rt.account.daily_dm_cap:
                continue
            rt.in_use = True
            return rt
        return None

    def release(self, uuid: str, dm_sent: bool, health: HealthStatus = HealthStatus.OK) -> None:
        rt = self._runtimes.get(uuid)
        if rt is None:
            return
        rt.in_use = False
        if dm_sent:
            rt.dms_today += 1
            rt.last_dm_at = datetime.now(timezone.utc)
        rt.last_health = health
        # Quarantine logic
        if health == HealthStatus.CHALLENGED:
            rt.consecutive_challenges += 1
            if rt.consecutive_challenges >= 3:
                rt.account.status = "quarantine"
                log.warning(
                    "account %s [%s] -> QUARANTINE (3 challenges consecutivi)",
                    rt.account.username, rt.account.platform,
                )
        elif health == HealthStatus.BANNED:
            rt.account.status = "banned"
            log.error("account %s [%s] -> BANNED", rt.account.username, rt.account.platform)
        elif health == HealthStatus.OK:
            rt.consecutive_challenges = 0

    def stats(self) -> dict:
        """Snapshot statistiche pool, per logging/UI."""
        return {
            "total": self.total,
            "active": sum(1 for r in self._runtimes.values() if r.account.status == "active"),
            "quarantine": sum(1 for r in self._runtimes.values() if r.account.status == "quarantine"),
            "banned": sum(1 for r in self._runtimes.values() if r.account.status == "banned"),
            "dms_today_total": sum(r.dms_today for r in self._runtimes.values()),
            "cap_remaining_total": sum(
                max(0, r.account.daily_dm_cap - r.dms_today) for r in self._runtimes.values()
            ),
        }
