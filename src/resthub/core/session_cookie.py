"""La sesión en una cookie `HttpOnly`, con defensa contra CSRF (S2 / RNFS-31).

El token de acceso viaja en una cookie que el JavaScript de la página no puede
leer: un script inyectado no se lo lleva, como sí podría desde `localStorage`.
El frontend (Vercel) y el API (el VPS) están en sitios distintos, así que la
cookie va con `SameSite=None; Secure` y el navegador la manda en cada petición
con credenciales... también en las que arme otra página. Por eso toda petición
que cambia algo (POST, PUT, PATCH, DELETE) autenticada por cookie exige además
la cabecera `X-CSRF-Token` con el mismo valor que la cookie `resthub_csrf`
(doble envío): otra página no puede leer ese valor ni poner la cabecera sin
pasar por CORS. El valor llega al frontend en el cuerpo del login
(`csrf_token`), porque desde otro sitio tampoco puede leer la cookie.

Cada alcance tiene su cookie (`resthub_session` para el personal,
`resthub_platform_session` para la plataforma) y entrar con uno borra la del
otro: nunca hay dos sesiones mezcladas en el mismo navegador. La vista previa no
usa cookie: vive solo en su pestaña, con `Authorization: Bearer`.

`Authorization: Bearer` sigue valiendo y tiene prioridad: el navegador no lo
manda solo, así que no necesita la defensa contra CSRF.

Con `Secure` (siempre fuera de desarrollo), las tres cookies llevan el prefijo
`__Host-` (ASVS V3.3.1 y V3.3.3): el navegador solo las acepta del propio host,
con `Secure`, `Path=/` y sin `Domain`, así que un subdominio no las puede pisar.
Las constantes de abajo son el nombre sin prefijo; `cookie_name` da el que se
escribe. Mientras queden sesiones abiertas antes del prefijo, también se lee el
nombre viejo (`read_cookie`), y escribir o borrar una cookie borra el viejo.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request, Response, status

from resthub.core.config import get_settings
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError
from resthub.core.logs import get_logger

STAFF_COOKIE = "resthub_session"
PLATFORM_COOKIE = "resthub_platform_session"
CSRF_COOKIE = "resthub_csrf"
HOST_PREFIX = "__Host-"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SCOPES = (STAFF_COOKIE, PLATFORM_COOKIE)
# Al salir, el navegador borra lo que guardó en caché del API (la foto de un plato, que se
# guarda a propósito). Solo la caché: el almacenamiento del origen del frontend, donde vive
# la cola de pedidos sin conexión, no se toca.
CLEAR_SITE_DATA = '"cache"'

logger = get_logger("resthub.auth.csrf")


def cookie_name(name: str) -> str:
    """El nombre con que se escribe la cookie: con `__Host-` si va con `Secure`.

    Sin `Secure` (en local por http) el navegador rechazaría una cookie con el
    prefijo, así que ahí se usa el nombre sin él.
    """
    return HOST_PREFIX + name if get_settings().session_cookie_secure else name


def read_cookie(request: Request, name: str) -> str | None:
    """El valor de la cookie, con el nombre de ahora o, si no está, con el de antes del prefijo."""
    return request.cookies.get(cookie_name(name)) or request.cookies.get(name)


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    settings = get_settings()
    written = cookie_name(name)
    response.set_cookie(
        written,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )
    if written != name:
        _delete_named(response, name)


def _delete_named(response: Response, written: str) -> None:
    settings = get_settings()
    response.delete_cookie(
        written,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def _delete(response: Response, name: str) -> None:
    """Borra la cookie con su nombre de ahora y con el de antes del prefijo."""
    for written in {cookie_name(name), name}:
        _delete_named(response, written)


def attach_session(response: Response, cookie: str, token: str, max_age: int) -> str:
    """Deja la sesión en su cookie, borra la del otro alcance y devuelve el valor anti-CSRF."""
    csrf = secrets.token_urlsafe(32)
    _set(response, cookie, token, max_age)
    _set(response, CSRF_COOKIE, csrf, max_age)
    for other in _SCOPES:
        if other != cookie:
            _delete(response, other)
    return csrf


def clear_session(response: Response) -> None:
    for name in (*_SCOPES, CSRF_COOKIE):
        _delete(response, name)
    response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA


def token_from_cookie(request: Request, cookie: str) -> str | None:
    """El token de la cookie de ese alcance, con el CSRF ya comprobado; `None` si no hay."""
    token = read_cookie(request, cookie)
    if not token:
        return None
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        expected = read_cookie(request, CSRF_COOKIE) or ""
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            # Un evento de seguridad: o la interfaz perdió el valor o alguien
            # intenta usar la cookie desde otro sitio. Nunca se loguea el valor.
            logger.warning(
                "auth.csrf_rejected",
                reason="missing" if not sent or not expected else "mismatch",
                session_scope="platform" if cookie == PLATFORM_COOKIE else "staff",
            )
            message = "Falta la cabecera X-CSRF-Token o no coincide con la sesión."
            raise ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.CSRF_TOKEN_INVALID)
    return token
