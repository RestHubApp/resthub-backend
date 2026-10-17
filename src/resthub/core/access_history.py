"""Historial de inicios de sesión (RFS-15 / RNFS-15).

Cada intento de acceso deja una fila: cuándo, cómo terminó, desde qué
dispositivo, desde qué red (la IP sin su último bloque) y, si hay una base
GeoLite local, desde qué ciudad y país. Sirve para que cada cuenta revise sus
propios accesos, para que el encargado revise los de su personal y para avisar
de un dispositivo o país nuevo (RFS-16).

Lo que no se guarda: la IP completa, el correo escrito ni la contraseña. Un
intento con un correo que no es de nadie queda sin cuenta. Las filas viven
`ACCESS_HISTORY_RETENTION_DAYS` días (90 por omisión): las vencidas se borran al
anotar un intento nuevo (RNFP-12).

Corre en una sesión propia que confirma enseguida: la de la petición se deshace
cuando el acceso falla, y el intento fallido también tiene que quedar.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Boolean, DateTime, Index, Integer, String, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.clock import Clock, utc_now
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.database import Base, get_session_factory
from resthub.core.geo import GeoLocator, get_geo_locator
from resthub.core.pagination import Page, PageRequest
from resthub.core.timestamps import as_utc

STAFF_ACCOUNT = "staff"
PLATFORM_ACCOUNT = "platform"
MAX_DEVICE_LENGTH = 120
# Cuántas filas vencidas se borran por intento: la purga no frena un acceso.
PURGE_BATCH = 500


class AccessOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    # Contraseña equivocada, cuenta desactivada o correo que no es de nadie.
    FAILED = "failed"
    # Lo frenó el límite de intentos (429).
    THROTTLED = "throttled"
    # La contraseña era la correcta pero el local está desactivado.
    BLOCKED = "blocked"


class AccessHistoryRow(Base):
    __tablename__ = "access_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # `staff` o `platform`; vacío si el correo no es de ninguna cuenta.
    account_kind: Mapped[str] = mapped_column(String(10), default="")
    # Enteros sueltos, sin clave foránea, como la telemetría: la fila es de una
    # cuenta de `users` o de `platform_admins` según `account_kind`.
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restaurant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(12))
    device: Mapped[str] = mapped_column(String(MAX_DEVICE_LENGTH), default="")
    # La IP sin su último bloque: `200.48.1.x`, `2800:200:e840::/48`.
    network: Mapped[str] = mapped_column(String(48), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    country: Mapped[str] = mapped_column(String(2), default="")
    # Primera vez que la cuenta entra desde ese dispositivo o ese país (RFS-16).
    new_device: Mapped[bool] = mapped_column(Boolean, default=False)
    new_country: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_access_history_account_moment", "account_kind", "account_id", "occurred_at"),
        Index("ix_access_history_restaurant_moment", "restaurant_id", "occurred_at"),
        Index("ix_access_history_moment", "occurred_at"),
    )


@dataclass(frozen=True, slots=True)
class AccessEntry:
    id: int
    occurred_at: datetime
    account_kind: str
    account_id: int | None
    restaurant_id: int | None
    outcome: AccessOutcome
    device: str
    network: str
    city: str
    country: str
    new_device: bool
    new_country: bool


def _to_entry(row: AccessHistoryRow) -> AccessEntry:
    return AccessEntry(
        id=row.id,
        occurred_at=as_utc(row.occurred_at),
        account_kind=row.account_kind,
        account_id=row.account_id,
        restaurant_id=row.restaurant_id,
        outcome=AccessOutcome(row.outcome),
        device=row.device,
        network=row.network,
        city=row.city,
        country=row.country,
        new_device=row.new_device,
        new_country=row.new_country,
    )


def mask_address(raw: str) -> str:
    """La IP sin su último bloque: alcanza para reconocer la red y no identifica el equipo."""
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return ""
    if isinstance(address, ipaddress.IPv4Address):
        first, second, third, _ = str(address).split(".")
        return f"{first}.{second}.{third}.x"
    network = ipaddress.ip_network(f"{address}/48", strict=False)
    return str(network)


_BROWSERS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/")),
    ("Samsung Internet", re.compile(r"SamsungBrowser/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Safari", re.compile(r"Safari/")),
)
_SYSTEMS = (
    ("Android", re.compile(r"Android")),
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Mac OS X|Macintosh")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("Linux", re.compile(r"Linux")),
)


def describe_device(user_agent: str) -> str:
    """El navegador y el sistema, sin versiones: «Chrome en Android»."""
    agent = user_agent[:512]
    browser = next((name for name, pattern in _BROWSERS if pattern.search(agent)), "")
    system = next((name for name, pattern in _SYSTEMS if pattern.search(agent)), "")
    if browser and system:
        return f"{browser} en {system}"
    return (browser or system or "Desconocido")[:MAX_DEVICE_LENGTH]


_STAFF_BY_EMAIL = text("SELECT id, restaurant_id FROM users WHERE email = :email")
_ADMIN_BY_EMAIL = text("SELECT id FROM platform_admins WHERE email = :email")


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    email: str
    outcome: AccessOutcome
    address: str
    user_agent: str


@dataclass(slots=True)
class SqlAccessHistory:
    factory: async_sessionmaker[AsyncSession]
    clock: Clock
    geo: GeoLocator
    retention_days: int = 90

    async def record(
        self, attempt: LoginAttempt, session: AsyncSession | None = None
    ) -> AccessEntry:
        """Anota el intento.

        Un acceso correcto pasa la sesión de la petición: la fila se confirma con
        él, y en SQLite una segunda sesión que escribe se trabaría con esa. Un
        fallo no la pasa, porque la de la petición se deshace con el error.
        """
        if session is not None:
            row = await self._write(session, attempt)
            await session.flush()
            return _to_entry(row)
        async with self.factory() as own, own.begin():
            row = await self._write(own, attempt)
        return _to_entry(row)

    async def _write(self, session: AsyncSession, attempt: LoginAttempt) -> AccessHistoryRow:
        now = self.clock.now()
        location = self.geo.locate(attempt.address)
        row = AccessHistoryRow(
            occurred_at=now,
            outcome=attempt.outcome.value,
            device=describe_device(attempt.user_agent),
            network=mask_address(attempt.address),
            city=location.city,
            country=location.country,
        )
        await self._attribute(session, row, attempt.email.strip().lower())
        if attempt.outcome is AccessOutcome.SUCCEEDED and row.account_id is not None:
            await self._mark_novelties(session, row)
        session.add(row)
        await self._purge(session, now)
        return row

    async def _attribute(self, session: AsyncSession, row: AccessHistoryRow, email: str) -> None:
        staff = (await session.execute(_STAFF_BY_EMAIL, {"email": email})).one_or_none()
        if staff is not None:
            row.account_kind, row.account_id = STAFF_ACCOUNT, int(staff.id)
            row.restaurant_id = int(staff.restaurant_id)
            return
        admin = (await session.execute(_ADMIN_BY_EMAIL, {"email": email})).one_or_none()
        if admin is not None:
            row.account_kind, row.account_id = PLATFORM_ACCOUNT, int(admin.id)

    async def _mark_novelties(self, session: AsyncSession, row: AccessHistoryRow) -> None:
        # Contra los accesos correctos anteriores de la misma cuenta. El primero
        # de todos no es «nuevo»: no hay con qué comparar y avisaría siempre.
        previous = (
            await session.execute(
                select(AccessHistoryRow.device, AccessHistoryRow.country).where(
                    AccessHistoryRow.account_kind == row.account_kind,
                    AccessHistoryRow.account_id == row.account_id,
                    AccessHistoryRow.outcome == AccessOutcome.SUCCEEDED.value,
                )
            )
        ).all()
        if not previous:
            return
        row.new_device = row.device not in {entry.device for entry in previous}
        known_countries = {entry.country for entry in previous if entry.country}
        row.new_country = bool(row.country) and row.country not in known_countries

    async def _purge(self, session: AsyncSession, now: datetime) -> None:
        cutoff = now - timedelta(days=self.retention_days)
        expired = (
            select(AccessHistoryRow.id)
            .where(AccessHistoryRow.occurred_at < cutoff)
            .limit(PURGE_BATCH)
        )
        await session.execute(
            delete(AccessHistoryRow)
            .where(AccessHistoryRow.id.in_(expired))
            .execution_options(synchronize_session=False)
        )

    async def list_for_account(
        self, account_kind: str, account_id: int, page: PageRequest
    ) -> Page[AccessEntry]:
        """Lo más nuevo primero."""
        where = (
            AccessHistoryRow.account_kind == account_kind,
            AccessHistoryRow.account_id == account_id,
        )
        async with self.factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(AccessHistoryRow).where(*where)
            )
            rows = (
                await session.execute(
                    select(AccessHistoryRow)
                    .where(*where)
                    .order_by(AccessHistoryRow.occurred_at.desc(), AccessHistoryRow.id.desc())
                    .limit(page.limit)
                    .offset(page.offset)
                )
            ).scalars()
            return Page(items=[_to_entry(row) for row in rows], total=int(total or 0))


SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def get_access_history(
    factory: SessionFactoryDep,
    clock: ClockDep,
    geo: Annotated[GeoLocator, Depends(get_geo_locator)],
) -> SqlAccessHistory:
    return SqlAccessHistory(
        factory, clock, geo, retention_days=get_settings().access_history_retention_days
    )


AccessHistoryDep = Annotated[SqlAccessHistory, Depends(get_access_history)]
