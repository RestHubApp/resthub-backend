"""La revisión periódica de picos (RFS-31), en segundo plano mientras corre el API.

Cada `SECURITY_ALERT_INTERVAL_SECONDS` abre su propia sesión, cuenta las señales
y guarda las alertas. Con varias réplicas puede correr en más de una: la pausa
entre alertas de la misma señal se lee de la base, así que a lo sumo se cuela
una de más si dos revisan en el mismo instante.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.clock import Clock
from resthub.core.config import Settings
from resthub.core.logs import get_logger
from resthub.modules.platform.adapters.persistence.sqlalchemy_security_alerts import (
    SqlAlchemySecurityAlertRepository,
    SqlAlchemySecuritySignals,
)
from resthub.modules.platform.domain.security_alerts import SpikeThresholds
from resthub.modules.platform.use_cases.security_alerts import DetectSecuritySpikes

logger = get_logger("resthub.platform.security")


def thresholds_from(settings: Settings) -> SpikeThresholds:
    return SpikeThresholds(
        window_minutes=settings.security_alert_window_minutes,
        failed_logins=settings.security_alert_failed_logins,
        lockouts=settings.security_alert_lockouts,
        forbidden=settings.security_alert_forbidden,
        cooldown_minutes=settings.security_alert_cooldown_minutes,
    )


async def check_once(
    factory: async_sessionmaker[AsyncSession], clock: Clock, thresholds: SpikeThresholds
) -> None:
    async with factory() as session, session.begin():
        detect = DetectSecuritySpikes(
            SqlAlchemySecuritySignals(session),
            SqlAlchemySecurityAlertRepository(session),
            thresholds,
        )
        for alert in await detect(clock.now()):
            # Nivel `warning`: el panel de observabilidad también lo muestra.
            logger.warning(
                "security.spike",
                kind=alert.kind.value,
                count=alert.count,
                threshold=alert.threshold,
                window=alert.window_minutes,
            )


async def _loop(
    factory: async_sessionmaker[AsyncSession], clock: Clock, settings: Settings
) -> None:
    thresholds = thresholds_from(settings)
    while True:
        await asyncio.sleep(settings.security_alert_interval_seconds)
        try:
            await check_once(factory, clock, thresholds)
        except Exception:
            logger.exception("security.spike_check_failed")


class SecurityAlertLoop:
    """La tarea de fondo: `start` al arrancar el API y `stop` al apagarlo."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], clock: Clock, settings: Settings
    ) -> None:
        self._factory, self._clock, self._settings = factory, clock, settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._settings.security_alert_interval_seconds > 0:
            self._task = asyncio.create_task(
                _loop(self._factory, self._clock, self._settings), name="security-alerts"
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
