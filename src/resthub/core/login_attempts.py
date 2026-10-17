"""Intentos de acceso compartidos entre réplicas (S3b / RFS-02).

`core/login_throttle.py` cuenta en la memoria de cada proceso: con una réplica
alcanza, con varias cada una cuenta aparte y el tope real se multiplica. Esta
tabla lleva la misma cuenta en la base, así que todas las réplicas ven los
mismos intentos: cinco fallos por correo e IP y veinticinco por correo en
quince minutos, sin importar qué réplica atendió cada uno.

`LoginGuard` usa las dos cuentas. Primero la de memoria, que reserva sin un
`await` de por medio y frena la ráfaga de peticiones simultáneas dentro de un
proceso; después la de la base, que reserva bajo un candado por correo
(`pg_advisory_xact_lock` en PostgreSQL) para que dos réplicas no se pisen entre
leer y anotar. Si la base rechaza el intento, la plaza de memoria se devuelve.

La tabla no guarda el correo ni la IP: guarda su SHA-256. Alcanza para contar y
no deja datos personales en claro. Cada intento vive quince minutos; las filas
vencidas se borran al anotar uno nuevo.

Corre en una sesión propia que confirma enseguida: la de la petición se deshace
cuando el acceso falla, y el intento fallido tiene que quedar contado.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Boolean, DateTime, Index, String, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.clock import Clock, utc_now
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import Base, get_session_factory
from resthub.core.login_throttle import (
    LoginThrottle,
    get_login_throttle,
    get_platform_login_throttle,
)

STAFF_SCOPE = "staff"
PLATFORM_SCOPE = "platform"


class LoginAttemptRow(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # El acceso del personal y el de la plataforma llevan cuentas aparte, como
    # en memoria: fallar en uno no bloquea el otro.
    scope: Mapped[str] = mapped_column(String(10))
    # SHA-256 en hexadecimal del correo en minúsculas y de la IP.
    email_key: Mapped[str] = mapped_column(String(64))
    address_key: Mapped[str] = mapped_column(String(64))
    # Un acceso correcto desde ese par lo limpia para el tope del par, pero el
    # fallo sigue contando para el tope por correo (el atacante seguiría).
    counts_for_pair: Mapped[bool] = mapped_column(Boolean, default=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_login_attempts_scope_email_moment", "scope", "email_key", "attempted_at"),
        Index("ix_login_attempts_moment", "attempted_at"),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class SqlLoginAttemptStore:
    """La cuenta de intentos en la base, con las mismas reglas que `LoginThrottle`."""

    factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scope: str = STAFF_SCOPE
    max_failures: int = 5
    max_failures_per_email: int = 25
    window_seconds: float = 15 * 60

    async def acquire(self, email: str, address: str) -> int:
        """Cero si el intento sigue (y quedó anotado) o los segundos que faltan."""
        email_key, address_key = _digest(email.lower()), _digest(address)
        now = self.clock.now()
        since = now - timedelta(seconds=self.window_seconds)
        async with self.factory() as session, session.begin():
            await self._lock(session, email_key)
            await session.execute(
                delete(LoginAttemptRow).where(LoginAttemptRow.attempted_at < since)
            )
            pair_count, pair_oldest = await self._recent(
                session, since, email_key, address_key=address_key
            )
            email_count, email_oldest = await self._recent(session, since, email_key)
            waits = [
                self._wait(now, oldest)
                for count, oldest, limit in (
                    (pair_count, pair_oldest, self.max_failures),
                    (email_count, email_oldest, self.max_failures_per_email),
                )
                if count >= limit
            ]
            if waits:
                return max(waits)
            session.add(
                LoginAttemptRow(
                    scope=self.scope,
                    email_key=email_key,
                    address_key=address_key,
                    attempted_at=now,
                )
            )
        return 0

    async def succeeded(self, email: str, address: str, session: AsyncSession) -> None:
        """Devuelve la plaza de este intento y limpia el par, como en memoria.

        Corre en la sesión de la petición, que confirma junto con el acceso: el
        acceso ya escribió en ella (la bitácora), y en SQLite una segunda sesión
        que escribe esperaría a esa y se trabaría.
        """
        email_key, address_key = _digest(email.lower()), _digest(address)
        pair = (
            LoginAttemptRow.scope == self.scope,
            LoginAttemptRow.email_key == email_key,
            LoginAttemptRow.address_key == address_key,
        )
        await self._lock(session, email_key)
        latest = await session.scalar(
            select(LoginAttemptRow.id)
            .where(*pair)
            .order_by(LoginAttemptRow.attempted_at.desc(), LoginAttemptRow.id.desc())
            .limit(1)
        )
        if latest is not None:
            await session.execute(delete(LoginAttemptRow).where(LoginAttemptRow.id == latest))
        await session.execute(update(LoginAttemptRow).where(*pair).values(counts_for_pair=False))

    async def _lock(self, session: AsyncSession, email_key: str) -> None:
        # Un candado por correo hasta el fin de la transacción: dos réplicas no
        # leen la misma cuenta a la vez. SQLite no lo necesita (un solo proceso
        # escribe, y la cuenta de memoria ya ordena las peticiones).
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"login:{self.scope}:{email_key}"},
            )

    async def _recent(
        self,
        session: AsyncSession,
        since: datetime,
        email_key: str,
        *,
        address_key: str | None = None,
    ) -> tuple[int, datetime | None]:
        query = select(func.count(), func.min(LoginAttemptRow.attempted_at)).where(
            LoginAttemptRow.scope == self.scope,
            LoginAttemptRow.email_key == email_key,
            LoginAttemptRow.attempted_at >= since,
        )
        if address_key is not None:
            query = query.where(
                LoginAttemptRow.address_key == address_key,
                LoginAttemptRow.counts_for_pair.is_(True),
            )
        count, oldest = (await session.execute(query)).one()
        return int(count), oldest

    def _wait(self, now: datetime, oldest: datetime | None) -> int:
        if oldest is None:
            return 1
        # SQLite devuelve la fecha sin zona: sin zona es UTC.
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=now.tzinfo)
        elapsed = (now - oldest).total_seconds()
        return max(int(self.window_seconds - elapsed) + 1, 1)


@dataclass(slots=True)
class LoginGuard:
    """El límite de intentos de un acceso: primero en memoria, después en la base."""

    memory: LoginThrottle
    store: SqlLoginAttemptStore

    async def acquire(self, email: str, address: str) -> int:
        # Síncrono hasta acá: la ráfaga simultánea se frena antes del primer `await`.
        wait = self.memory.acquire(email, address)
        if wait:
            return wait
        wait = await self.store.acquire(email, address)
        if wait:
            # Lo frenó la cuenta compartida (otra réplica ya anotó los fallos):
            # el intento no se evaluó, así que no ocupa plaza en memoria.
            self.memory.release(email, address)
        return wait

    async def succeeded(self, email: str, address: str, session: AsyncSession) -> None:
        """Libera el intento; `session` es la de la petición (ver `SqlLoginAttemptStore`)."""
        self.memory.succeeded(email, address)
        await self.store.succeeded(email, address, session)


SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def get_login_guard(
    memory: Annotated[LoginThrottle, Depends(get_login_throttle)],
    factory: SessionFactoryDep,
    clock: ClockDep,
) -> LoginGuard:
    return LoginGuard(memory, SqlLoginAttemptStore(factory, clock, STAFF_SCOPE))


def get_platform_login_guard(
    memory: Annotated[LoginThrottle, Depends(get_platform_login_throttle)],
    factory: SessionFactoryDep,
    clock: ClockDep,
) -> LoginGuard:
    return LoginGuard(memory, SqlLoginAttemptStore(factory, clock, PLATFORM_SCOPE))


LoginGuardDep = Annotated[LoginGuard, Depends(get_login_guard)]
PlatformLoginGuardDep = Annotated[LoginGuard, Depends(get_platform_login_guard)]
