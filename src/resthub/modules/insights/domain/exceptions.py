"""Errores de dominio de los indicadores.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class InsightsError(Exception):
    """Raíz de los errores del módulo de indicadores."""

    code: ClassVar[ErrorCode]


class InvalidPeriodError(InsightsError):
    code = ErrorCode.INVALID_PERIOD

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
