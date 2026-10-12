"""Límite de intentos de acceso fallidos.

Cinco contraseñas equivocadas seguidas para el mismo correo desde la misma
dirección bloquean ese par quince minutos: frena a quien prueba contraseñas
sin castigar al resto del local, que suele salir por la misma IP del wifi.
Un acceso correcto limpia el contador.

Además hay un tope por correo, sin importar la IP: quien rota de dirección
para esquivar el límite del par llega igual a `MAX_FAILURES_PER_EMAIL` fallos
en la ventana y bloquea ese correo. Es más alto que el del par para que un
atacante no pueda dejar fuera a la cuenta con unos pocos intentos, y un acceso
correcto no lo limpia (el atacante seguiría probando).

Cada intento se cuenta *antes* de verificar la contraseña, con `acquire`, que
comprueba y reserva en el mismo tramo síncrono: entre las dos cosas no hay un
`await` por donde se cuele otra petición. Si se contara al fallar, una ráfaga
de peticiones simultáneas pasaría entera el control mientras la primera espera
a bcrypt, y el tope de cinco no frenaría nada.

Vive en memoria del proceso. Con varios procesos cada uno lleva su cuenta; el
límite real queda en N x 5, que igual vuelve impráctico adivinar. Como `/login`
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
MAX_FAILURES_PER_EMAIL = 25


@dataclass(slots=True)
class LoginThrottle:
    max_failures: int = MAX_FAILURES
    window_seconds: float = WINDOW_SECONDS
    max_keys: int = MAX_KEYS
    max_failures_per_email: int = MAX_FAILURES_PER_EMAIL
    _failures: dict[tuple[str, str], deque[float]] = field(default_factory=dict)
    _by_email: dict[str, deque[float]] = field(default_factory=dict)

    def _recent_email(self, email: str, now: float) -> deque[float]:
        attempts = self._by_email.get(email)
        if attempts is None:
            return deque()
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._by_email[email]
        return attempts

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures.get(key)
        if attempts is None:
            return deque()
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
        return attempts

    def acquire(self, email: str, address: str) -> int:
        """Comprueba y reserva un intento de una vez.

        Devuelve cero si el intento puede seguir (y ya quedó contado, en el par
        y en el tope por correo) o los segundos que faltan para volver a
        intentar; un intento rechazado no se anota. Síncrono a propósito: la
        reserva no puede ceder el bucle de eventos entre leer y anotar.
        """
        now = time.monotonic()
        key = (email.lower(), address)
        attempts = self._recent(key, now)
        by_email = self._recent_email(key[0], now)
        waits = [
            max(int(self.window_seconds - (now - recent[0])) + 1, 1)
            for recent, limit in (
                (attempts, self.max_failures),
                (by_email, self.max_failures_per_email),
            )
            if len(recent) >= limit
        ]
        if waits:
            return max(waits)
        attempts.append(now)
        self._failures[key] = attempts
        by_email.append(now)
        self._by_email[key[0]] = by_email
        if len(self._failures) > self.max_keys or len(self._by_email) > self.max_keys:
            self._sweep(now)
        return 0

    def retry_after(self, email: str, address: str) -> int:
        """Segundos que faltan para volver a intentar, sin reservar nada; cero si puede ya."""
        now = time.monotonic()
        waits = [0]
        for attempts, limit in (
            (self._recent((email.lower(), address), now), self.max_failures),
            (self._recent_email(email.lower(), now), self.max_failures_per_email),
        ):
            if len(attempts) >= limit:
                waits.append(max(int(self.window_seconds - (now - attempts[0])) + 1, 1))
        return max(waits)

    def succeeded(self, email: str, address: str) -> None:
        """Limpia el par y devuelve la plaza que reservó este intento.

        Los fallos ajenos del tope por correo se quedan (el atacante seguiría
        probando), pero un acierto no ocupa plaza: quien entra seguido desde
        varios equipos no se bloquea a sí mismo.
        """
        email = email.lower()
        self._failures.pop((email, address), None)
        by_email = self._by_email.get(email)
        if by_email:
            by_email.pop()
            if not by_email:
                del self._by_email[email]

    def _sweep(self, now: float) -> None:
        for key in [k for k, v in self._failures.items() if now - v[-1] > self.window_seconds]:
            del self._failures[key]
        for email in [e for e, v in self._by_email.items() if now - v[-1] > self.window_seconds]:
            del self._by_email[email]
        excess_emails = len(self._by_email) - self.max_keys
        if excess_emails > 0:
            oldest_emails = sorted(self._by_email, key=lambda e: self._by_email[e][-1])
            for email in oldest_emails[:excess_emails]:
                del self._by_email[email]
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
