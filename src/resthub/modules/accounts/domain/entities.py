"""Entidades de dominio de cuentas.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic. Este archivo debe
poder ejecutarse sin que exista una base de datos ni un servidor web, y los
contratos de Import Linter lo verifican.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# El alias `as` es la reexportación explícita que exige mypy estricto: el resto del
# módulo importa estas constantes desde aquí.
from resthub.core.clock import utc_now
from resthub.core.credentials import (
    MAX_PASSWORD_LENGTH as MAX_PASSWORD_LENGTH,  # noqa: PLC0414 - reexportación explícita
)
from resthub.core.credentials import (
    MIN_PASSWORD_LENGTH as MIN_PASSWORD_LENGTH,  # noqa: PLC0414 - reexportación explícita
)
from resthub.core.credentials import SERVICE_NAME, normalized_email, password_problem
from resthub.modules.accounts.domain.exceptions import (
    CannotChangeOwnRoleError,
    CannotDeactivateSelfError,
    CannotManageStrongerAccountError,
    CannotResetOwnPasswordError,
    InvalidDeactivationReasonError,
    InvalidEmailError,
    InvalidFullNameError,
    InvalidPinError,
    OutdatedTermsError,
    WeakPasswordError,
)
from resthub.modules.accounts.domain.roles import Role, holds_all

MAX_FULL_NAME_LENGTH = 120
MIN_DEACTIVATION_REASON_LENGTH = 3
MAX_DEACTIVATION_REASON_LENGTH = 200


# Las versiones vigentes de los dos textos que cada cuenta acepta por separado
# (Ley N.º 29733): los términos y condiciones del servicio y el tratamiento de
# datos personales (política de privacidad). La interfaz muestra cada texto en
# su página (`/terminos` y `/privacidad`); si uno cambia, cambia su versión y
# cada cuenta lo vuelve a aceptar al entrar.
TERMS_VERSION = "2026-10-15"
PRIVACY_VERSION = "2026-10"


@dataclass(slots=True)
class User:
    # Obligatorio: no existe una cuenta sin restaurante, ni siquiera la del
    # primer encargado, que nace junto con el suyo.
    restaurant_id: int
    email: str
    full_name: str
    # El rol entero y no solo su identificador: su nombre se muestra y sus
    # permisos deciden quién puede gestionar esta cuenta.
    role: Role
    password_hash: str
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    # La última versión de los términos y la política de privacidad que aceptó;
    # vacía si todavía no aceptó ninguna. Cada aceptación queda en la bitácora.
    terms_version: str = ""
    terms_accepted_at: datetime | None = None
    # Lo mismo para el consentimiento de tratamiento de datos personales, que se
    # acepta aparte de los términos.
    privacy_version: str = ""
    privacy_accepted_at: datetime | None = None
    sessions_valid_since: datetime | None = None
    # La contraseña la puso otra persona (alta o restablecimiento): hasta que la
    # cuenta elija la suya, solo puede cambiarla (RFS-20).
    must_change_password: bool = False
    # Quién la desactivó, cuándo y por qué (RFS-18). Vacíos mientras esté activa:
    # reactivarla los limpia (el historial queda en la bitácora).
    deactivated_at: datetime | None = None
    deactivated_by: int | None = None
    deactivation_reason: str = ""
    # El hash del PIN de desbloqueo (RFS-29); `None` si la cuenta no eligió uno.
    pin_hash: str | None = None

    def __post_init__(self) -> None:
        self.email = normalize_email(self.email)
        self.full_name = validate_full_name(self.full_name)

    @property
    def has_pin(self) -> bool:
        return self.pin_hash is not None

    def unused_since(self, last_activity: datetime | None) -> datetime:
        """Desde cuándo nadie usa la cuenta: su último uso o, si nunca entró, su alta."""
        return last_activity if last_activity is not None else self.created_at

    def rename(self, full_name: str) -> None:
        self.full_name = validate_full_name(full_name)

    def deactivate(self, now: datetime, by: int, reason: str) -> None:
        # Sella el corte: reactivar la cuenta no devuelve la vida a los tokens
        # que tenía abiertos al desactivarla.
        self.deactivation_reason = validate_deactivation_reason(reason)
        self.is_active = False
        self.sessions_valid_since = now
        self.deactivated_at = now
        self.deactivated_by = by

    def activate(self) -> None:
        self.is_active = True
        self.deactivated_at = None
        self.deactivated_by = None
        self.deactivation_reason = ""

    def set_temporary_password(self, password_hash: str, now: datetime) -> None:
        """Una contraseña que dictó otra persona: corta las sesiones y obliga a cambiarla."""
        self.password_hash = password_hash
        self.sessions_valid_since = now
        self.must_change_password = True

    def set_own_password(self, password_hash: str, now: datetime) -> None:
        """La contraseña que eligió la propia cuenta: corta las demás sesiones."""
        self.password_hash = password_hash
        self.sessions_valid_since = now
        self.must_change_password = False

    @property
    def has_current_terms(self) -> bool:
        return self.terms_version == TERMS_VERSION

    def accept_terms(self, version: str, now: datetime) -> None:
        if version != TERMS_VERSION:
            raise OutdatedTermsError(version)
        self.terms_version = version
        self.terms_accepted_at = now

    @property
    def has_current_privacy(self) -> bool:
        return self.privacy_version == PRIVACY_VERSION

    def accept_privacy(self, version: str, now: datetime) -> None:
        if version != PRIVACY_VERSION:
            raise OutdatedTermsError(version)
        self.privacy_version = version
        self.privacy_accepted_at = now


def normalize_email(raw: str) -> str:
    email = normalized_email(raw)
    if email is None:
        raise InvalidEmailError(raw)
    return email


def validate_full_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        message = "El nombre no puede quedar vacío."
        raise InvalidFullNameError(message)
    if len(name) > MAX_FULL_NAME_LENGTH:
        message = f"El nombre no puede pasar de {MAX_FULL_NAME_LENGTH} caracteres."
        raise InvalidFullNameError(message)
    return name


def validate_deactivation_reason(raw: str) -> str:
    reason = " ".join(raw.split())
    if not MIN_DEACTIVATION_REASON_LENGTH <= len(reason) <= MAX_DEACTIVATION_REASON_LENGTH:
        message = (
            "Escribe por qué se desactiva la cuenta (de "
            f"{MIN_DEACTIVATION_REASON_LENGTH} a {MAX_DEACTIVATION_REASON_LENGTH} caracteres)."
        )
        raise InvalidDeactivationReasonError(message)
    return reason


def validate_new_password(plain_password: str, context: frozenset[str] = frozenset()) -> str:
    """Largo, lista de las más usadas y palabras del contexto de una contraseña nueva.

    Vive en el dominio y no solo en el esquema HTTP porque también la usa el
    script de alta de restaurantes, que no pasa por la API. `context` son las
    palabras de la cuenta y del local (`core.credentials.context_words`).
    """
    problem = password_problem(plain_password, context | {SERVICE_NAME})
    if problem is not None:
        raise WeakPasswordError(problem)
    return plain_password


# Las tres reglas que siguen protegen lo mismo: que el restaurante no se quede
# sin quien lo administre. Como el encargado no puede degradarse ni desactivarse
# a sí mismo, siempre queda al menos uno activo, el que está operando. La
# cuarta impide que alguien con menos permisos degrade o tome su cuenta.


def ensure_can_deactivate(actor_id: int, target: User) -> None:
    if target.id == actor_id:
        raise CannotDeactivateSelfError


def ensure_can_change_role(actor_id: int, target: User, new_role: Role) -> None:
    if target.id == actor_id and new_role.id != target.role.id:
        raise CannotChangeOwnRoleError


def ensure_can_reset_password(actor_id: int, target: User) -> None:
    # Sobre la propia cuenta se exige la contraseña actual, que este camino
    # no pide: si no, una sesión olvidada abierta alcanzaría para cambiarla.
    if target.id == actor_id:
        raise CannotResetOwnPasswordError


PIN_LENGTHS = range(4, 7)


def validate_pin(pin: str) -> str:
    """De 4 a 6 dígitos (RFS-29): corto para el celular, y lo frena el tercer error."""
    if not (pin.isascii() and pin.isdigit() and len(pin) in PIN_LENGTHS):
        raise InvalidPinError
    return pin


def ensure_can_manage(actor_permissions: frozenset[str], target: User) -> None:
    if not holds_all(actor_permissions, target.role.permissions):
        raise CannotManageStrongerAccountError
