"""Documentos de identidad que se consultan (RUC y DNI) y su validación de formato.

Python puro. Validar antes de llamar al proveedor evita gastar cuota del plan en
un número que no puede existir.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from resthub.modules.lookup.domain.exceptions import InvalidDocumentNumberError

RUC_LENGTH = 11
DNI_LENGTH = 8
# Los dos primeros dígitos del RUC dicen el tipo de contribuyente: 10 persona natural,
# 15/16/17 otros sujetos naturales, 20 persona jurídica.
RUC_PREFIXES = frozenset({"10", "15", "16", "17", "20"})
_RUC_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_MODULUS = 11


class DocumentType(StrEnum):
    RUC = "ruc"
    DNI = "dni"


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Lo que el registro oficial dice de un documento; nada de esto se guarda."""

    document_type: DocumentType
    document_number: str
    name: str
    address: str | None = None
    # Solo RUC: estado del contribuyente (ACTIVO, SUSPENSION TEMPORAL, BAJA…) y su condición
    # (HABIDO, NO HABIDO…). Un DNI no tiene ninguno.
    status: str | None = None
    condition: str | None = None


def _ruc_check_digit(first_ten: str) -> int:
    total = sum(int(digit) * weight for digit, weight in zip(first_ten, _RUC_WEIGHTS, strict=True))
    return (_MODULUS - total % _MODULUS) % 10


def is_valid_ruc(value: str) -> bool:
    return (
        len(value) == RUC_LENGTH
        and value.isascii()
        and value.isdigit()
        and value[:2] in RUC_PREFIXES
        and _ruc_check_digit(value[:-1]) == int(value[-1])
    )


def is_valid_dni(value: str) -> bool:
    return len(value) == DNI_LENGTH and value.isascii() and value.isdigit()


def ensure_valid_document(document_type: DocumentType, number: str) -> str:
    """El número sin espacios, o `InvalidDocumentNumberError` si no cumple el formato."""
    clean = number.strip()
    if document_type is DocumentType.RUC:
        if not is_valid_ruc(clean):
            message = "El RUC debe tener 11 dígitos, empezar en 10, 15, 16, 17 o 20 y ser válido."
            raise InvalidDocumentNumberError(message)
    elif not is_valid_dni(clean):
        message = "El DNI debe tener 8 dígitos."
        raise InvalidDocumentNumberError(message)
    return clean
