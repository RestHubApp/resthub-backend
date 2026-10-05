"""Cabeceras de seguridad en cada respuesta del API.

El API solo devuelve JSON (y el canal de avisos): ninguna respuesta tiene que
ejecutarse, enmarcarse ni filtrar de dónde vino. Estas cabeceras se lo dicen al
navegador, por si alguien engaña a uno para abrir una respuesta como página.
La documentación interactiva (`/docs`, `/redoc`) es la excepción a la CSP:
carga Swagger UI y ReDoc de un CDN.

Es la parte del WAF que vive en el código. El resto (filtrar ataques conocidos,
bots y picos por delante de Railway) va en el proxy; ver el README.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Dos años y los subdominios: el API solo se sirve por HTTPS. En local, por
# HTTP, el navegador la ignora.
HSTS = "max-age=63072000; includeSubDomains"
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

_HEADERS = {
    "strict-transport-security": HSTS,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, csp_exempt: Sequence[str] = ()) -> None:
        self.app = app
        self._csp_exempt = tuple(csp_exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with_csp = not str(scope["path"]).startswith(self._csp_exempt)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _HEADERS.items():
                    headers.setdefault(name, value)
                if with_csp:
                    headers.setdefault("content-security-policy", API_CSP)
            await send(message)

        await self.app(scope, receive, send_with_headers)
