"""Cuentas de la administración del sistema y su bitácora.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from resthub.core.clock import utc_now
from resthub.core.credentials import (
    SERVICE_NAME,
    normalized_email,
    password_problem,
    temporary_password_expired,
)
from resthub.modules.platform.domain.exceptions import InvalidAccountDataError

MAX_FULL_NAME_LENGTH = 120
MAX_DETAIL_LENGTH = 200


@dataclass(slots=True)
class PlatformAdmin:
    # Sin restaurante a propósito: esta cuenta no es personal de ningún local.
    email: str
    full_name: str
    password_hash: str
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    # Desde cuándo valen las sesiones: los tokens emitidos antes ya no sirven.
    # Nulo si nunca se cortaron.
    sessions_valid_since: datetime | None = None
    # La contraseña la generó otra persona (`scripts/create_platform_admin.py`): hasta
    # cambiarla, la cuenta no usa la plataforma; vence como la del personal (ASVS 6.4.1).
    must_change_password: bool = False
    # `None` si la temporal se fijó en el alta: cuenta desde `created_at`.
    temporary_password_set_at: datetime | None = None

    def __post_init__(self) -> None:
        self.email = normalize_email(self.email)
        self.full_name = validate_full_name(self.full_name)

    def deactivate(self, now: datetime) -> None:
        # Sella el corte: reactivar la cuenta no devuelve la vida a los tokens
        # que tenía abiertos al desactivarla.
        self.is_active = False
        self.sessions_valid_since = now

    def activate(self) -> None:
        self.is_active = True

    def set_own_password(self, password_hash: str, now: datetime) -> None:
        """La contraseña que eligió la propia cuenta: corta las demás sesiones."""
        self.password_hash = password_hash
        self.sessions_valid_since = now
        self.must_change_password = False
        self.temporary_password_set_at = None

    def temporary_password_expired(self, now: datetime, lifetime: timedelta) -> bool:
        return temporary_password_expired(
            must_change=self.must_change_password,
            set_at=self.temporary_password_set_at or self.created_at,
            now=now,
            lifetime=lifetime,
        )


def normalize_email(raw: str) -> str:
    email = normalized_email(raw)
    if email is None:
        message = f"El correo electrónico no es válido: {raw!r}"
        raise InvalidAccountDataError(message)
    return email


def validate_full_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        message = "El nombre no puede quedar vacío."
        raise InvalidAccountDataError(message)
    if len(name) > MAX_FULL_NAME_LENGTH:
        message = f"El nombre no puede pasar de {MAX_FULL_NAME_LENGTH} caracteres."
        raise InvalidAccountDataError(message)
    return name


def validate_new_password(plain_password: str, context: frozenset[str] = frozenset()) -> str:
    """Las mismas reglas que el personal; `context` sale de `context_words`."""
    problem = password_problem(plain_password, context | {SERVICE_NAME})
    if problem is not None:
        raise InvalidAccountDataError(problem)
    return plain_password


class PlatformActivityKind(StrEnum):
    """Qué hizo la administración del sistema; la frase se arma al mostrarla."""

    SIGNED_IN = "signed_in"
    RESTAURANT_CREATED = "restaurant_created"
    # También activar y desactivar: el detalle dice qué cambió.
    RESTAURANT_UPDATED = "restaurant_updated"
    OWNER_ADDED = "owner_added"
    OWNER_PASSWORD_RESET = "owner_password_reset"  # noqa: S105  # nosec B105 # nombre del evento
    SANDBOX_RESET = "sandbox_reset"
    PREVIEW_STARTED = "preview_started"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105  # nosec B105 # nombre del evento

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[PlatformActivityKind, str] = {
    PlatformActivityKind.SIGNED_IN: "Inició sesión",
    PlatformActivityKind.RESTAURANT_CREATED: "Dio de alta un restaurante",
    PlatformActivityKind.RESTAURANT_UPDATED: "Editó un restaurante",
    PlatformActivityKind.OWNER_ADDED: "Agregó un encargado",
    PlatformActivityKind.OWNER_PASSWORD_RESET: "Restableció la contraseña de un encargado",
    PlatformActivityKind.SANDBOX_RESET: "Reinició el local de muestra",
    PlatformActivityKind.PREVIEW_STARTED: "Abrió la vista previa",
    PlatformActivityKind.PASSWORD_CHANGED: "Cambió su contraseña",
}


@dataclass(frozen=True, slots=True)
class PlatformActivityRecord:
    admin_id: int
    kind: PlatformActivityKind
    detail: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", self.detail.strip()[:MAX_DETAIL_LENGTH])
