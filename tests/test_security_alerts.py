"""Alertas a la plataforma por picos de accesos fallidos, bloqueos o 403 (RFS-31)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resthub.core.access_history import AccessHistoryRow, AccessOutcome
from resthub.modules.platform.adapters.persistence.models import ObsRequestRow
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_security_alerts import (
    SqlAlchemySecurityAlertRepository,
)
from resthub.modules.platform.adapters.security_alert_loop import check_once
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.security_alerts import SpikeThresholds
from tests.conftest import TEST_HASHER, TEST_TOKEN_SERVICE, VALID_PASSWORD, url
from tests.fakes import FixedClock

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
LOW = SpikeThresholds(window_minutes=15, failed_logins=3, lockouts=2, forbidden=2)


@pytest.fixture
async def headers(session: AsyncSession) -> dict[str, str]:
    admin = await SqlAlchemyPlatformAdminRepository(session).add(
        PlatformAdmin(
            email="equipo@resthub.dev",
            full_name="Equipo RestHub",
            password_hash=TEST_HASHER.hash(VALID_PASSWORD),
        )
    )
    await session.commit()
    return {"Authorization": f"Bearer {TEST_TOKEN_SERVICE.issue_platform(admin.id or 0).value}"}


def _factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)


def _access(outcome: AccessOutcome, minutes_ago: int) -> AccessHistoryRow:
    return AccessHistoryRow(
        occurred_at=NOW - timedelta(minutes=minutes_ago), outcome=outcome.value, device=""
    )


def _forbidden(minutes_ago: int) -> ObsRequestRow:
    return ObsRequestRow(
        at=NOW - timedelta(minutes=minutes_ago),
        method="GET",
        route=url("staff"),
        status=403,
        duration_ms=1.0,
        db_ms=0.0,
        db_queries=0,
        request_id="req",
        account_kind="staff",
    )


async def test_a_spike_raises_an_alert_once_per_pause(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    session.add_all(
        [
            *(_access(AccessOutcome.FAILED, minutes) for minutes in (1, 5, 10)),
            # Fuera de la ventana: no cuenta.
            _access(AccessOutcome.FAILED, 40),
            _access(AccessOutcome.THROTTLED, 2),
            _forbidden(1),
            _forbidden(3),
        ]
    )
    await session.commit()
    clock = FixedClock(NOW)

    await check_once(_factory(session), clock, LOW)
    clock.set(NOW + timedelta(minutes=5))
    await check_once(_factory(session), clock, LOW)
    alerts = await client.get(url("platform/observability/security-alerts"), headers=headers)

    body = alerts.json()
    assert alerts.status_code == 200, alerts.text
    # Uno de bloqueos no llega a su umbral; los otros dos alertan una sola vez.
    assert sorted((alert["kind"], alert["count"]) for alert in body) == [
        ("failed_logins", 3),
        ("forbidden", 2),
    ]
    assert {alert["kind_label"] for alert in body} == {
        "Accesos fallidos",
        "Respuestas 403 (sin permiso)",
    }


async def test_after_the_pause_it_alerts_again(session: AsyncSession) -> None:
    session.add_all([_access(AccessOutcome.THROTTLED, 1), _access(AccessOutcome.THROTTLED, 2)])
    await session.commit()
    clock = FixedClock(NOW)
    factory = _factory(session)

    await check_once(factory, clock, LOW)
    clock.set(NOW + timedelta(minutes=61))
    async with factory() as own, own.begin():
        own.add_all(
            [
                AccessHistoryRow(
                    occurred_at=clock.now(), outcome=AccessOutcome.THROTTLED.value, device=""
                )
                for _ in range(2)
            ]
        )
    await check_once(factory, clock, LOW)

    async with factory() as own:
        recent = await SqlAlchemySecurityAlertRepository(own).recent(10)
    assert [alert.kind.value for alert in recent] == ["lockouts", "lockouts"]
