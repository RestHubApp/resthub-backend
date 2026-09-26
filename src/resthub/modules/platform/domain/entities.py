"""Cuentas de la administración del sistema y su bitácora.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from resthub.core.credentials import normalized_email, password_problem
from resthub.modules.platform.domain.exceptions import InvalidAccountData

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
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.email = normalize_email(self.email)
        self.full_name = validate_full_name(self.full_name)


def normalize_email(raw: str) -> str:
    email = normalized_email(raw)
    if email is None:
        raise InvalidAccountData(f"El correo electrónico no es válido: {raw!r}")
    return email


def validate_full_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidAccountData("El nombre no puede quedar vacío.")
    if len(name) > MAX_FULL_NAME_LENGTH:
        raise InvalidAccountData(f"El nombre no puede pasar de {MAX_FULL_NAME_LENGTH} caracteres.")
    return name


def validate_new_password(plain_password: str) -> str:
    problem = password_problem(plain_password)
    if problem is not None:
        raise InvalidAccountData(problem)
    return plain_password


class PlatformActivityKind(StrEnum):
    """Qué hizo la administración del sistema; la frase se arma al mostrarla."""

    SIGNED_IN = "signed_in"
    RESTAURANT_CREATED = "restaurant_created"
    # También activar y desactivar: el detalle dice qué cambió.
    RESTAURANT_UPDATED = "restaurant_updated"
    OWNER_ADDED = "owner_added"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[PlatformActivityKind, str] = {
    PlatformActivityKind.SIGNED_IN: "Inició sesión",
    PlatformActivityKind.RESTAURANT_CREATED: "Dio de alta un restaurante",
    PlatformActivityKind.RESTAURANT_UPDATED: "Editó un restaurante",
    PlatformActivityKind.OWNER_ADDED: "Agregó un encargado",
}


@dataclass(frozen=True, slots=True)
class PlatformActivityRecord:
    admin_id: int
    kind: PlatformActivityKind
    detail: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", self.detail.strip()[:MAX_DETAIL_LENGTH])
