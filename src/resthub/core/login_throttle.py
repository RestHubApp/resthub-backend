"""Límite de intentos de acceso fallidos.

Cinco contraseñas equivocadas seguidas para el mismo correo desde la misma
dirección bloquean ese par quince minutos: frena a quien prueba contraseñas
sin castigar al resto del local, que suele salir por la misma IP del wifi.
Un acceso correcto limpia el contador.

Vive en memoria del proceso. Con varios procesos cada uno lleva su cuenta; el
límite real queda en N × 5, que igual vuelve impráctico adivinar.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


@dataclass(slots=True)
class LoginThrottle:
    max_failures: int = MAX_FAILURES
    window_seconds: float = WINDOW_SECONDS
    _failures: dict[tuple[str, str], deque[float]] = field(default_factory=dict)

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures.setdefault(key, deque())
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        return attempts

    def retry_after(self, email: str, address: str) -> int:
        """Segundos que faltan para volver a intentar; cero si puede intentar ya."""
        now = time.monotonic()
        attempts = self._recent((email.lower(), address), now)
        if len(attempts) < self.max_failures:
            return 0
        return max(int(self.window_seconds - (now - attempts[0])) + 1, 1)

    def failed(self, email: str, address: str) -> None:
        now = time.monotonic()
        self._recent((email.lower(), address), now).append(now)

    def succeeded(self, email: str, address: str) -> None:
        self._failures.pop((email.lower(), address), None)


@lru_cache(maxsize=1)
def get_login_throttle() -> LoginThrottle:
    return LoginThrottle()
