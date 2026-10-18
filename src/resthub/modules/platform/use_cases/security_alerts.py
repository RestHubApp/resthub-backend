"""Detectar picos de accesos fallidos, bloqueos o 403 y alertar (RFS-31).

Corre cada minuto en segundo plano (`adapters/security_alert_loop.py`), que
deja cada alerta también en los logs como `security.spike`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from resthub.modules.platform.domain.security_alerts import (
    SecurityAlert,
    SpikeKind,
    SpikeThresholds,
)
from resthub.modules.platform.ports.security_alerts import (
    SecurityAlertRepository,
    SecuritySignalReader,
)


class DetectSecuritySpikes:
    def __init__(
        self,
        signals: SecuritySignalReader,
        alerts: SecurityAlertRepository,
        thresholds: SpikeThresholds,
    ) -> None:
        self._signals = signals
        self._alerts = alerts
        self._thresholds = thresholds

    async def __call__(self, now: datetime) -> list[SecurityAlert]:
        window = self._thresholds.window_minutes
        counts = await self._signals.count_since(now - timedelta(minutes=window))
        raised: list[SecurityAlert] = []
        for kind in SpikeKind:
            count, threshold = counts.get(kind, 0), self._thresholds.of(kind)
            if count < threshold or await self._cooling_down(kind, now):
                continue
            raised.append(
                await self._alerts.add(SecurityAlert(kind, count, threshold, window, now))
            )
        return raised

    async def _cooling_down(self, kind: SpikeKind, now: datetime) -> bool:
        last = await self._alerts.last_raised(kind)
        pause = timedelta(minutes=self._thresholds.cooldown_minutes)
        return last is not None and now - last < pause


class ReadSecurityAlerts:
    def __init__(self, alerts: SecurityAlertRepository) -> None:
        self._alerts = alerts

    async def __call__(self, limit: int = 50) -> list[SecurityAlert]:
        return await self._alerts.recent(limit)
