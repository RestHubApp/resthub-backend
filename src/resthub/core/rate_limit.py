"""Límite general de peticiones por IP.

El acceso ya tiene su propio límite de intentos fallidos
(`core/login_throttle.py`). Este es más amplio y más alto: corta a quien
recorre el API a ráfagas (un script que baja la libreta de clientes página por
página, por ejemplo) sin estorbar a un local con varios celulares detrás de la
misma conexión, que comparten IP. Responde 429 con `Retry-After`.

Cuenta por ventana fija de un minuto y en la memoria del proceso: con una sola
réplica alcanza. Es la defensa que vive en el código; la de volumen (bots,
picos, ataques conocidos) va en el WAF por delante de Railway, ver el README.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from resthub.core.auth import client_address
from resthub.core.logs import get_logger

WINDOW_SECONDS = 60
RATE_LIMIT_DETAIL = "Demasiadas peticiones seguidas. Espera un momento y vuelve a intentar."
# Pasado este tamaño se barren las IP cuya ventana ya venció: la memoria no
# crece con cada IP que pasó una vez.
_SWEEP_AT = 10_000

logger = get_logger("resthub.http")


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        per_minute: int,
        exempt: Sequence[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.per_minute = per_minute
        self._exempt = tuple(exempt)
        self._clock = clock
        # IP → (inicio de su ventana, peticiones en ella).
        self._windows: dict[str, tuple[float, int]] = {}

    def _applies(self, scope: Scope) -> bool:
        return (
            self.per_minute > 0
            and scope["type"] == "http"
            and scope["method"] != "OPTIONS"
            and not str(scope["path"]).startswith(self._exempt)
        )

    def _wait(self, address: str) -> int:
        """Segundos que faltan para que vuelva a poder; 0 si puede ahora."""
        now = self._clock()
        start, count = self._windows.get(address, (now, 0))
        if now - start >= WINDOW_SECONDS:
            start, count = now, 0
        if count >= self.per_minute:
            return max(1, math.ceil(WINDOW_SECONDS - (now - start)))
        self._windows[address] = (start, count + 1)
        if len(self._windows) > _SWEEP_AT:
            self._windows = {
                ip: window
                for ip, window in self._windows.items()
                if now - window[0] < WINDOW_SECONDS
            }
        return 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return
        # La misma IP que el límite de acceso: la que agrega el proxy, no la
        # que escribe el cliente.
        address = client_address(Request(scope))
        wait = self._wait(address)
        if not wait:
            await self.app(scope, receive, send)
            return
        logger.warning("http.rate_limited", per_minute=self.per_minute, retry_after=wait)
        response = JSONResponse(
            {"detail": RATE_LIMIT_DETAIL}, status_code=429, headers={"Retry-After": str(wait)}
        )
        await response(scope, receive, send)
