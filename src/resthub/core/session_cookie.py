"""La sesión en una cookie `HttpOnly`, con defensa contra CSRF (S2 / RNFS-31).

El token de acceso viaja en una cookie que el JavaScript de la página no puede
leer: un script inyectado no se lo lleva, como sí podría desde `localStorage`.
El frontend (Vercel) y el API (Railway) están en sitios distintos, así que la
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
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SCOPES = (STAFF_COOKIE, PLATFORM_COOKIE)
# Al salir, el navegador borra lo que guardó en caché del API (la foto de un plato, que se
# guarda a propósito). Solo la caché: el almacenamiento del origen del frontend, donde vive
# la cola de pedidos sin conexión, no se toca.
CLEAR_SITE_DATA = '"cache"'

logger = get_logger("resthub.auth.csrf")


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def _delete(response: Response, name: str) -> None:
    settings = get_settings()
    response.delete_cookie(
        name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


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
    token = request.cookies.get(cookie)
    if not token:
        return None
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        expected = request.cookies.get(CSRF_COOKIE, "")
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
