"""Límite de intentos de acceso fallidos.

Cinco contraseñas equivocadas seguidas para el mismo correo desde la misma
dirección bloquean ese par quince minutos: frena a quien prueba contraseñas
sin castigar al resto del local, que suele salir por la misma IP del wifi.
Un acceso correcto limpia el contador.

Vive en memoria del proceso. Con varios procesos cada uno lleva su cuenta; el
límite real queda en N × 5, que igual vuelve impráctico adivinar. Como `/login`
es público, la memoria también tiene tope: un par sin intentos vigentes se
borra, y pasadas `MAX_KEYS` claves se barren las vencidas y, si no alcanza, las
más viejas.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
MAX_KEYS = 10_000


@dataclass(slots=True)
class LoginThrottle:
    max_failures: int = MAX_FAILURES
    window_seconds: float = WINDOW_SECONDS
    max_keys: int = MAX_KEYS
    _failures: dict[tuple[str, str], deque[float]] = field(default_factory=dict)

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures.get(key)
        if attempts is None:
            return deque()
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
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
        key = (email.lower(), address)
        attempts = self._recent(key, now)
        attempts.append(now)
        self._failures[key] = attempts
        if len(self._failures) > self.max_keys:
            self._sweep(now)

    def succeeded(self, email: str, address: str) -> None:
        self._failures.pop((email.lower(), address), None)

    def _sweep(self, now: float) -> None:
        for key in [k for k, v in self._failures.items() if now - v[-1] > self.window_seconds]:
            del self._failures[key]
        excess = len(self._failures) - self.max_keys
        if excess > 0:
            # Los que hace más que no fallan son los que menos riesgo corren al olvidarse.
            oldest = sorted(self._failures, key=lambda k: self._failures[k][-1])[:excess]
            for key in oldest:
                del self._failures[key]


@lru_cache(maxsize=1)
def get_login_throttle() -> LoginThrottle:
    return LoginThrottle()


@lru_cache(maxsize=1)
def get_platform_login_throttle() -> LoginThrottle:
    """Cuenta aparte para el acceso de la administración del sistema.

    Mismas reglas, otro contador: fallar en un acceso no bloquea el otro, y
    acertar en uno no limpia los intentos contra el otro.
    """
    return LoginThrottle()
