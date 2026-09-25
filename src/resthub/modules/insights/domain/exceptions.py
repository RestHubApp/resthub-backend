"""Errores de dominio de los indicadores.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class InsightsError(Exception):
    """Raíz de los errores del módulo de indicadores."""


class InvalidPeriod(InsightsError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
