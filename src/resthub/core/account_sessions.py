"""Las sesiones abiertas de cada cuenta, una por dispositivo (RFS-17, RFS-21, RFS-29).

El token sigue siendo un JWT, pero lleva un `sid` firmado que apunta a una fila
de `account_sessions`. La fila dice desde qué dispositivo y red se abrió, cuándo
se usó por última vez y si se cerró. Así cada cuenta ve sus sesiones y cierra
una sola (la de un celular perdido) sin cortar las demás, cosa que
`sessions_valid_since` no puede: corta todas a la vez.

La misma fila guarda dos estados de la sesión, no de la cuenta:

- cuándo se volvió a confirmar la contraseña (RFS-21): las acciones críticas
  lo exigen reciente, y vale solo en ese dispositivo;
- si está bloqueada por inactividad y cuántos PIN equivocados lleva (RFS-29).

Una vista previa no abre fila: vive media hora en su pestaña y no se renueva.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy import DateTime, Index, Integer, String, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.access_history import MAX_DEVICE_LENGTH, describe_device, mask_address
from resthub.core.clock import Clock, utc_now
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.database import Base
from resthub.core.geo import GeoLocator, get_geo_locator
from resthub.core.timestamps import as_utc

SESSION_ID_LENGTH = 43
# Cada cuánto se anota el último uso: escribir en cada petición sería una
# escritura por cada lectura de la pantalla, y para «último uso» sobra el minuto.
TOUCH_EVERY = timedelta(seconds=60)
# Las filas que llevan este tiempo sin usarse se borran al abrir una sesión nueva.
# Más que los 60 días de la revisión de accesos (RFS-24), que mira el último uso.
PURGE_AFTER = timedelta(days=120)
PURGE_BATCH = 500
# El valor por omisión de `SESSION_ABSOLUTE_LIFETIME_HOURS` (`core/config.py`).
DEFAULT_ABSOLUTE_LIFETIME_HOURS = 12


def new_session_id() -> str:
    """Aleatorio y largo: viaja firmado en el token, pero no tiene que adivinarse."""
    return secrets.token_urlsafe(32)


class AccountSessionRow(Base):
    __tablename__ = "account_sessions"

    id: Mapped[str] = mapped_column(String(SESSION_ID_LENGTH), primary_key=True)
    # `staff` o `platform`; la cuenta es de `users` o de `platform_admins`, sin
    # clave foránea, como en `access_history`.
    account_kind: Mapped[str] = mapped_column(String(10))
    account_id: Mapped[int] = mapped_column(Integer)
    restaurant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    device: Mapped[str] = mapped_column(String(MAX_DEVICE_LENGTH), default="")
    network: Mapped[str] = mapped_column(String(48), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    country: Mapped[str] = mapped_column(String(2), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reauthenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlock_failures: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_account_sessions_account", "account_kind", "account_id", "last_seen_at"),
        Index("ix_account_sessions_last_seen", "last_seen_at"),
    )


@dataclass(frozen=True, slots=True)
class AccountSession:
    id: str
    account_kind: str
    account_id: int
    restaurant_id: int | None
    created_at: datetime
    last_seen_at: datetime
    device: str
    network: str
    city: str
    country: str
    revoked_at: datetime | None
    reauthenticated_at: datetime | None
    locked_at: datetime | None
    unlock_failures: int

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None

    @property
    def ref(self) -> str:
        """El nombre público de la sesión, para listarla y cerrarla.

        No es el `sid`: el identificador viaja firmado en el token y no se
        muestra, ni siquiera a quien gestiona al personal.
        """
        return public_session_ref(self.id)


def public_session_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:24]


def _optional_utc(moment: datetime | None) -> datetime | None:
    return as_utc(moment) if moment is not None else None


def _to_session(row: AccountSessionRow) -> AccountSession:
    return AccountSession(
        id=row.id,
        account_kind=row.account_kind,
        account_id=row.account_id,
        restaurant_id=row.restaurant_id,
        created_at=as_utc(row.created_at),
        last_seen_at=as_utc(row.last_seen_at),
        device=row.device,
        network=row.network,
        city=row.city,
        country=row.country,
        revoked_at=_optional_utc(row.revoked_at),
        reauthenticated_at=_optional_utc(row.reauthenticated_at),
        locked_at=_optional_utc(row.locked_at),
        unlock_failures=row.unlock_failures,
    )


@dataclass(frozen=True, slots=True)
class SessionOrigin:
    """Desde dónde se abre una sesión: la IP real y el `User-Agent`."""

    address: str
    user_agent: str


@dataclass(slots=True)
class SqlSessionRegistry:
    """Las sesiones viven en la sesión de base de la petición: se confirman con ella."""

    clock: Clock
    geo: GeoLocator
    # Lo que vive un token: una sesión que no se usó en ese plazo ya no tiene
    # ninguno vigente, así que deja de contar como abierta.
    token_ttl: timedelta
    # Lo que vive una sesión desde que se abrió, aunque se renueve sin parar
    # (ASVS V7.3.2): pasado ese plazo se vuelve a entrar.
    absolute_lifetime: timedelta = timedelta(hours=DEFAULT_ABSOLUTE_LIFETIME_HOURS)

    async def open(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        account_kind: str,
        account_id: int,
        restaurant_id: int | None,
        origin: SessionOrigin,
    ) -> AccountSession:
        now = self.clock.now()
        location = self.geo.locate(origin.address)
        row = AccountSessionRow(
            id=session_id,
            account_kind=account_kind,
            account_id=account_id,
            restaurant_id=restaurant_id,
            created_at=now,
            last_seen_at=now,
            device=describe_device(origin.user_agent),
            network=mask_address(origin.address),
            city=location.city,
            country=location.country,
            unlock_failures=0,
        )
        session.add(row)
        await self._purge(session, now)
        await session.flush()
        return _to_session(row)

    async def find(self, session: AsyncSession, session_id: str) -> AccountSession | None:
        # `populate_existing`: las escrituras de este registro van sin sincronizar
        # la sesión del ORM, así que una fila ya cargada podría estar vieja.
        row = await session.get(AccountSessionRow, session_id, populate_existing=True)
        return _to_session(row) if row is not None else None

    def is_alive(self, state: AccountSession) -> bool:
        """Sin cerrar, usada dentro de la vida de un token y sin pasar su vida máxima."""
        now = self.clock.now()
        return (
            state.revoked_at is None
            and state.last_seen_at >= now - self.token_ttl
            and not self.has_expired(state)
        )

    def has_expired(self, state: AccountSession) -> bool:
        """Si pasó la vida máxima de la sesión desde que se abrió (ASVS V7.3.2)."""
        return state.created_at < self.clock.now() - self.absolute_lifetime

    async def touch(
        self, session: AsyncSession, state: AccountSession, *, force: bool = False
    ) -> None:
        now = self.clock.now()
        if force or now - state.last_seen_at >= TOUCH_EVERY:
            await self._set(session, state.id, last_seen_at=now)

    async def revoke(self, session: AsyncSession, session_id: str) -> None:
        await self._set(session, session_id, revoked_at=self.clock.now())

    async def revoke_all(
        self, session: AsyncSession, account_kind: str, account_id: int, *, keep: str | None = None
    ) -> None:
        """Cierra todas las sesiones de una cuenta, menos `keep` si se indica."""
        statement = (
            update(AccountSessionRow)
            .where(
                AccountSessionRow.account_kind == account_kind,
                AccountSessionRow.account_id == account_id,
                AccountSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=self.clock.now())
            .execution_options(synchronize_session=False)
        )
        if keep is not None:
            statement = statement.where(AccountSessionRow.id != keep)
        await session.execute(statement)

    async def list_open(
        self, session: AsyncSession, account_kind: str, account_id: int
    ) -> list[AccountSession]:
        """Las sesiones abiertas de la cuenta, la usada más recientemente primero."""
        now = self.clock.now()
        rows = (
            await session.execute(
                select(AccountSessionRow)
                .where(
                    AccountSessionRow.account_kind == account_kind,
                    AccountSessionRow.account_id == account_id,
                    AccountSessionRow.revoked_at.is_(None),
                    AccountSessionRow.last_seen_at >= now - self.token_ttl,
                    AccountSessionRow.created_at >= now - self.absolute_lifetime,
                )
                .order_by(AccountSessionRow.last_seen_at.desc(), AccountSessionRow.id)
            )
        ).scalars()
        return [_to_session(row) for row in rows]

    async def find_open_by_ref(
        self, session: AsyncSession, account_kind: str, account_id: int, ref: str
    ) -> AccountSession | None:
        """Una sesión abierta de la cuenta por su nombre público; `None` si no hay."""
        opened = await self.list_open(session, account_kind, account_id)
        return next((state for state in opened if state.ref == ref), None)

    async def mark_reauthenticated(self, session: AsyncSession, session_id: str) -> datetime:
        now = self.clock.now()
        await self._set(session, session_id, reauthenticated_at=now, last_seen_at=now)
        return now

    async def rotate(self, session: AsyncSession, state: AccountSession) -> str:
        """Cambia el `sid` de la sesión por uno nuevo y cierra el anterior (ASVS V7.2.4).

        La sesión sigue siendo la misma para quien la usa: el dispositivo, la
        apertura (su vida máxima no se alarga), la contraseña confirmada y el
        bloqueo pasan a la fila nueva. Un token con el `sid` anterior ya no vale.
        """
        new_id = new_session_id()
        session.add(
            AccountSessionRow(
                id=new_id,
                account_kind=state.account_kind,
                account_id=state.account_id,
                restaurant_id=state.restaurant_id,
                created_at=state.created_at,
                last_seen_at=self.clock.now(),
                device=state.device,
                network=state.network,
                city=state.city,
                country=state.country,
                reauthenticated_at=state.reauthenticated_at,
                locked_at=state.locked_at,
                unlock_failures=state.unlock_failures,
            )
        )
        await session.flush()
        await self.revoke(session, state.id)
        return new_id

    async def lock(self, session: AsyncSession, session_id: str) -> None:
        await self._set(session, session_id, locked_at=self.clock.now(), unlock_failures=0)

    async def unlock(self, session: AsyncSession, session_id: str) -> None:
        now = self.clock.now()
        await self._set(session, session_id, locked_at=None, unlock_failures=0, last_seen_at=now)

    async def count_unlock_failure(self, session: AsyncSession, state: AccountSession) -> int:
        failures = state.unlock_failures + 1
        await self._set(session, state.id, unlock_failures=failures)
        return failures

    async def _set(self, session: AsyncSession, session_id: str, **values: object) -> None:
        await session.execute(
            update(AccountSessionRow)
            .where(AccountSessionRow.id == session_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    async def _purge(self, session: AsyncSession, now: datetime) -> None:
        stale = (
            select(AccountSessionRow.id)
            .where(AccountSessionRow.last_seen_at < now - PURGE_AFTER)
            .limit(PURGE_BATCH)
        )
        await session.execute(
            delete(AccountSessionRow)
            .where(AccountSessionRow.id.in_(stale))
            .execution_options(synchronize_session=False)
        )


def get_session_registry(
    clock: ClockDep, geo: Annotated[GeoLocator, Depends(get_geo_locator)]
) -> SqlSessionRegistry:
    settings = get_settings()
    return SqlSessionRegistry(
        clock=clock,
        geo=geo,
        token_ttl=timedelta(seconds=settings.access_token_ttl_seconds),
        absolute_lifetime=timedelta(hours=settings.session_absolute_lifetime_hours),
    )


SessionRegistryDep = Annotated[SqlSessionRegistry, Depends(get_session_registry)]
