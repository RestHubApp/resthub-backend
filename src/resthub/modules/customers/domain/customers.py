"""Clientes frecuentes: la libreta de clientes del local.

Python puro. Un cliente tiene nombre y, para ubicarlo rápido al tomar un
delivery, su teléfono (único en el local). Guarda también su dirección, una
referencia y lo que conviene recordar (alergias, preferencias). Cuántas veces
vino y cuánto consumió se calcula con sus pedidos, no se escribe a mano.

Guardar a alguien en la libreta es tratar sus datos personales (Ley N.º 29733),
y la nota puede traer datos de salud, como una alergia. Por eso el alta exige
su consentimiento informado, y queda anotado cuándo lo dio, sobre qué versión
del texto y quién lo registró.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from resthub.modules.customers.domain.exceptions import InvalidCustomer

MAX_NAME_LENGTH = 80
MAX_PHONE_LENGTH = 20
MAX_EMAIL_LENGTH = 120
MAX_ADDRESS_LENGTH = 200
MAX_REFERENCE_LENGTH = 150
MAX_NOTES_LENGTH = 300
# La versión del texto de consentimiento que se le lee o muestra al cliente.
# Si el texto cambia, cambia la versión: lo aceptado antes queda con la suya.
CONSENT_VERSION = "2026-10"
_PHONE = re.compile(r"\+?[\d ]{6,20}")


def _text(raw: str, limit: int, what: str) -> str:
    text = " ".join(raw.split())
    if len(text) > limit:
        raise InvalidCustomer(f"{what} admite {limit} caracteres como máximo.")
    return text


@dataclass(frozen=True, slots=True)
class Consent:
    """El cliente aceptó que el local guarde sus datos."""

    given_at: datetime
    version: str
    # La cuenta que lo registró; `None` si esa cuenta ya no existe.
    recorded_by: int | None


@dataclass(slots=True)
class Customer:
    restaurant_id: int
    name: str
    phone: str = ""
    email: str = ""
    address: str = ""
    reference: str = ""
    # Alergias, preferencias, «pide la mesa del fondo».
    notes: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # `None` en los clientes guardados antes de pedir el consentimiento.
    consent: Consent | None = None

    def __post_init__(self) -> None:
        self.name = _text(self.name, MAX_NAME_LENGTH, "El nombre")
        if not self.name:
            raise InvalidCustomer("El cliente necesita un nombre.")
        self.phone = _text(self.phone, MAX_PHONE_LENGTH, "El teléfono")
        if self.phone and not _PHONE.fullmatch(self.phone):
            raise InvalidCustomer("El teléfono solo lleva dígitos, espacios y un «+» inicial.")
        self.email = _text(self.email, MAX_EMAIL_LENGTH, "El correo").lower()
        if self.email and "@" not in self.email:
            raise InvalidCustomer("El correo no es válido.")
        self.address = _text(self.address, MAX_ADDRESS_LENGTH, "La dirección")
        self.reference = _text(self.reference, MAX_REFERENCE_LENGTH, "La referencia")
        self.notes = _text(self.notes, MAX_NOTES_LENGTH, "La nota")

    @property
    def phone_key(self) -> str:
        """El teléfono sin espacios: «987 654 321» y «987654321» son el mismo."""
        return self.phone.replace(" ", "")


@dataclass(frozen=True, slots=True)
class CustomerStats:
    """Lo que dicen sus pedidos pagados: cuántas veces, cuánto y cuándo."""

    visits: int = 0
    spent: Decimal = Decimal("0.00")
    last_visit: datetime | None = None

    @property
    def average_ticket(self) -> Decimal:
        return (
            (self.spent / self.visits).quantize(Decimal("0.01"), ROUND_HALF_UP)
            if self.visits
            else Decimal("0.00")
        )

    @property
    def is_frequent(self) -> bool:
        """Frecuente desde la tercera visita pagada."""
        return self.visits >= FREQUENT_VISITS


FREQUENT_VISITS = 3
