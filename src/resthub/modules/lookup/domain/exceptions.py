"""Errores de dominio de la consulta de documentos.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class DocumentLookupError(Exception):
    """Raíz de los errores de la consulta de documentos."""

    code: ClassVar[ErrorCode]


class InvalidDocumentNumberError(DocumentLookupError):
    code = ErrorCode.INVALID_DOCUMENT_NUMBER


class DocumentNotFoundError(DocumentLookupError):
    code = ErrorCode.DOCUMENT_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("No se encontró el documento en SUNAT/RENIEC. Revisa el número.")


class LookupDisabledError(DocumentLookupError):
    code = ErrorCode.LOOKUP_DISABLED

    def __init__(self) -> None:
        super().__init__("La búsqueda por RUC o DNI no está activada en este servidor.")


class LookupUnavailableError(DocumentLookupError):
    code = ErrorCode.LOOKUP_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("Servicio no disponible por ahora. Escribe los datos a mano.")


class LookupRateLimitedError(DocumentLookupError):
    code = ErrorCode.LOOKUP_RATE_LIMITED

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Hiciste demasiadas búsquedas seguidas. Espera un momento.")
        self.retry_after_seconds = retry_after_seconds
