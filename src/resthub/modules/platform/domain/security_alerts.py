"""Alertas a la administración del sistema ante picos sospechosos (RFS-31).

Tres señales, contadas en una ventana corta sobre todo el sistema: accesos
fallidos (alguien prueba contraseñas), accesos frenados por el límite de
intentos (una cuenta bajo ataque) y respuestas 403 (alguien recorre lo que no
puede). Pasar el umbral deja una alerta; la misma señal no vuelve a alertar
hasta que pase la pausa, para no llenar el panel durante un mismo ataque.

Python puro, como el resto del dominio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SpikeKind(StrEnum):
    FAILED_LOGINS = "failed_logins"
    LOCKOUTS = "lockouts"
    FORBIDDEN = "forbidden"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS: dict[SpikeKind, str] = {
    SpikeKind.FAILED_LOGINS: "Accesos fallidos",
    SpikeKind.LOCKOUTS: "Accesos frenados por el límite de intentos",
    SpikeKind.FORBIDDEN: "Respuestas 403 (sin permiso)",
}


@dataclass(frozen=True, slots=True)
class SpikeThresholds:
    window_minutes: int = 15
    failed_logins: int = 30
    lockouts: int = 5
    forbidden: int = 50
    # Tras una alerta, la misma señal espera esto antes de volver a alertar.
    cooldown_minutes: int = 60

    def of(self, kind: SpikeKind) -> int:
        return {
            SpikeKind.FAILED_LOGINS: self.failed_logins,
            SpikeKind.LOCKOUTS: self.lockouts,
            SpikeKind.FORBIDDEN: self.forbidden,
        }[kind]


@dataclass(frozen=True, slots=True)
class SecurityAlert:
    kind: SpikeKind
    count: int
    threshold: int
    window_minutes: int
    raised_at: datetime
    id: int | None = None
