"""Compresión gzip de las respuestas del API.

Los JSON del API (la carta, las mesas, los pedidos activos, los reportes del BI) son texto
muy repetido: comprimidos ocupan una fracción y llegan antes al celular del mesero con
menos datos y menos energía en la red (RNF de software sostenible). Lo hace la aplicación
y no el proxy: el proxy de Dokploy o Coolify no comprime si nadie se lo pide.

Comprimir una respuesta que lleva un secreto junto con texto que un atacante controla deja
adivinar el secreto por el tamaño (BREACH). Por eso solo se comprimen las respuestas a un
`GET` (los secretos que el API entrega, como la contraseña temporal, los códigos de
recuperación, el secreto de la verificación en dos pasos o el enlace de una vista previa,
salen de un `POST`) y nunca las del acceso (`/auth`, `/platform/auth`): `GET /auth/me`
devuelve el `csrf_token`.

No se tocan los avisos en tiempo real (`text/event-stream`: comprimirlos los retendría en el
búfer) ni lo que ya viene comprimido (fotos, PDF, ZIP): sería gastar CPU para nada.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

# Por debajo de esto la cabecera de gzip y el trabajo no compensan lo que se ahorra.
MINIMUM_SIZE_BYTES = 1024
# 6 es el punto de zlib entre CPU y tamaño; 9 (el de Starlette) gasta bastante más CPU
# para unos pocos bytes menos.
COMPRESS_LEVEL = 6
# Los de Starlette (avisos, imágenes, audio, video, fuentes, ZIP) más el PDF, que ya viene
# comprimido por dentro.
UNCOMPRESSED_CONTENT_TYPES = (*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/pdf")
_GZIP_CODINGS = ("gzip", "x-gzip", "*")


def accepts_gzip(accept_encoding: str) -> bool:
    """Si `Accept-Encoding` acepta gzip (RFC 9110 §12.5.3).

    `gzip;q=0` lo rechaza aunque diga «gzip», que es lo único que mira Starlette. Manda la
    mención explícita (`gzip`, después `x-gzip`) sobre el comodín `*`.
    """
    weights: dict[str, float] = {}
    for item in accept_encoding.split(","):
        coding, *params = item.split(";")
        name = coding.strip().lower()
        if not name:
            continue
        weights[name] = _weight(params)
    for coding in _GZIP_CODINGS:
        if coding in weights:
            return weights[coding] > 0
    return False


def _weight(params: list[str]) -> float:
    for param in params:
        key, _, value = param.partition("=")
        if key.strip().lower() == "q":
            try:
                return float(value.strip())
            except ValueError:
                return 0.0
    return 1.0


def _route_path(scope: Scope) -> str:
    """La ruta sin el `root_path` de la aplicación, la misma que usa el enrutador."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    if root and (path == root or path.startswith(f"{root}/")):
        return path[len(root) :] or "/"
    return path


class CompressionMiddleware:
    """Comprime con gzip las respuestas a un `GET`, salvo las de las rutas excluidas."""

    def __init__(self, app: ASGIApp, *, excluded_prefixes: tuple[str, ...]) -> None:
        self._app = app
        self._excluded_prefixes = excluded_prefixes
        self._gzip = GZipMiddleware(
            app,
            minimum_size=MINIMUM_SIZE_BYTES,
            compresslevel=COMPRESS_LEVEL,
            exclude_content_types=UNCOMPRESSED_CONTENT_TYPES,
        )

    def _compressible(self, scope: Scope) -> bool:
        if scope["type"] != "http" or scope["method"] != "GET":
            return False
        if not accepts_gzip(Headers(scope=scope).get("accept-encoding", "")):
            return False
        path = _route_path(scope)
        return not any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in self._excluded_prefixes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pasa la petición por gzip solo si se puede comprimir."""
        if self._compressible(scope):
            await self._gzip(scope, receive, send)
        else:
            await self._app(scope, receive, send)
