"""Qué datos no se guardan nunca en claro.

Los logs de la consola ya ocultan un puñado de claves conocidas
(`core/logs.py`). Lo que se guarda en la base para el panel de observabilidad
dura días y lo lee otra persona, así que la regla es más amplia: se oculta todo
campo cuyo nombre *contenga* una palabra sensible, sin importar mayúsculas ni
lo hondo que esté en un diccionario anidado.

El nombre se parte en palabras (`preview_code`, `apiKey`, `X-Auth-Token`) y se
compara palabra por palabra. Buscar la palabra como subcadena ocultaría
`monkey` o `keyboard`; compararla entera no deja pasar `access_token`. Las
palabras largas y sin falsos positivos (`token`, `password`, `secret`…) sí se
buscan como subcadena del nombre sin separadores, para que `ACCESSTOKEN` o
`PASSWORDHASH` tampoco pasen. Unos pocos nombres con una palabra sensible pero
sin secreto (`invoice_code`, `status_code`) se dejan ver: en las llamadas a
log se usan esos nombres y no el genérico `code`.

Además, todo texto se limpia por su valor (`scrub_text`), venga en el campo
que venga o en el mensaje y el traceback de una excepción: JWT, `Bearer …` y
`Basic …`, `token="…"`, usuario y contraseña en una URL
(`postgresql://usuario:clave@…`), los parámetros que SQLAlchemy copia en el
mensaje de sus excepciones (`[parameters: ('juan@x.com', '$2b$12$…')]`) y los
valores de la fila que PostgreSQL pone en su detalle. El motor ya se crea con
`hide_parameters=True` (`core/database.py`); esto es la segunda barrera.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[oculto]"

# Palabras que vuelven sensible un campo, comparadas enteras. `code` cubre los
# códigos de vista previa, que valen como una contraseña mientras no se canjean.
SENSITIVE_WORDS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "code",
        "cookie",
        "credential",
        "dsn",
        "hash",
        "jwt",
        "key",
        "otp",
        "passcode",
        "password",
        "pin",
        "secret",
        "signature",
        "token",
    }
)
# Palabras que se buscan también como subcadena del nombre sin separadores.
SENSITIVE_FRAGMENTS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "passcode",
        "passwd",
        "password",
        "secret",
        "signature",
        "token",
    }
)
# Nombres enteros, sin separadores y en minúsculas, que no se dejan partir en
# palabras sensibles.
SENSITIVE_NAMES = frozenset({"apikey", "databaseurl", "pwd", "sessionid", "setcookie"})
# Nombres con una palabra sensible que no guardan ningún secreto.
SAFE_NAMES = frozenset({"invoice_code", "status_code"})

# Hasta dónde se baja por estructuras anidadas. Más abajo el valor se oculta
# entero: no hay motivo para loguear algo tan hondo y así no hay recursión
# sin fin con una estructura que se contiene a sí misma.
_MAX_DEPTH = 6
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def _words(name: str) -> list[str]:
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    return [word.lower() for word in _SEPARATORS.split(spaced) if word]


def is_sensitive_key(name: object) -> bool:
    if not isinstance(name, str):
        return False
    if name.lower() in SAFE_NAMES:
        return False
    words = _words(name)
    compact = "".join(words)
    if compact in SENSITIVE_NAMES:
        return True
    if any(fragment in compact for fragment in SENSITIVE_FRAGMENTS):
        return True
    # `passwords` o `tokens` también: se compara sin la `s` final.
    return any({word, word.removesuffix("s")} & SENSITIVE_WORDS for word in words)


# Lo que SQLAlchemy agrega al mensaje de sus excepciones. Los parámetros van en
# una sola línea (son un `repr`) y terminan en `]` al final de la línea; si el
# texto viene recortado y falta, se tapa hasta el final.
_SQL_PARAMETERS = re.compile(r"\[parameters: .*?(?:\](?=[ \t]*(?:\r?\n|$))|\Z)", re.DOTALL)

# Estas expresiones corren sobre texto que puede traer datos del cliente
# (mensajes y tracebacks, sin recortar), así que ninguna puede retroceder en
# tiempo cuadrático. `_SQL_STATEMENT` y `_PG_KEY_DETAIL` dejan de buscar donde
# empezaría otra coincidencia (el `(?!…)` delante de cada carácter); `_JWT` y
# `_URL_CREDENTIALS` solo arrancan al principio de la palabra. Con `.*?` a
# secas, 112 KB de `[SQL: ` repetido tardaban unos 9 segundos.
_SQL_STATEMENT = re.compile(
    r"\[SQL: (?P<sql>(?:(?!\[SQL: ).)*?)\]"
    r"(?=\s*(?:\[parameters|\[SQL parameters|\(Background on this error|\Z))",
    re.DOTALL,
)
# Un valor escrito dentro de la sentencia (`WHERE email = 'juan@x.com'`).
_SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# El detalle de PostgreSQL de una clave repetida o de una fila rechazada.
_PG_KEY_DETAIL = re.compile(
    r"(Key \((?:(?!Key \()[^)\n])*\)=\()(?:(?!Key \()[^\n])*?(\) (?:already exists|is not present))"
)
_PG_FAILING_ROW = re.compile(r"(Failing row contains \().*$", re.MULTILINE)
_JWT = re.compile(r"(?<![\w-])eyJ[\w-]+\.[\w-]+\.[\w-]*")
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)\s+[^\s\"',;]+", re.IGNORECASE)
_QUOTED_TOKEN = re.compile(r"(\btoken\s*=\s*\")[^\"]*(\")", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(
    r"(?<![a-zA-Z0-9+.-])([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s:/@]*:(?:(?!://)[^\s@])+@"
)


def _hide_sql_literals(match: re.Match[str]) -> str:
    return "[SQL: " + _SQL_STRING_LITERAL.sub(f"'{REDACTED}'", match["sql"]) + "]"


def scrub_text(text: str) -> str:
    """Tapa en un texto lo que parece una credencial o un valor de una fila."""
    if "[parameters: " in text:
        text = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", text)
    if "[SQL: " in text:
        text = _SQL_STATEMENT.sub(_hide_sql_literals, text)
    if "Key (" in text:
        text = _PG_KEY_DETAIL.sub(rf"\g<1>{REDACTED}\g<2>", text)
    if "Failing row contains" in text:
        text = _PG_FAILING_ROW.sub(rf"\g<1>{REDACTED})", text)
    if "eyJ" in text:
        text = _JWT.sub(REDACTED, text)
    text = _AUTH_SCHEME.sub(rf"\g<1> {REDACTED}", text)
    text = _QUOTED_TOKEN.sub(rf"\g<1>{REDACTED}\g<2>", text)
    if "://" in text:
        text = _URL_CREDENTIALS.sub(rf"\g<1>{REDACTED}@", text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Copia `value` con las claves sensibles reemplazadas y los textos limpios."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key) else redact(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item, _depth + 1) for item in value]
    return value
