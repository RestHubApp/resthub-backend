"""Qué datos no se guardan nunca en claro.

Los logs de la consola ya ocultan un puñado de claves conocidas
(`core/logs.py`). Lo que se guarda en la base para el panel de observabilidad
dura días y lo lee otra persona, así que la regla es más amplia: se oculta todo
campo cuyo nombre *contenga* una palabra sensible, sin importar mayúsculas ni
lo hondo que esté en un diccionario anidado.

El nombre se parte en palabras (`preview_code`, `apiKey`, `X-Auth-Token`) y se
compara palabra por palabra. Buscar la palabra como subcadena ocultaría
`monkey` o `keyboard`; compararla entera no deja pasar `access_token`.

Los mensajes de error también se limpian (`scrub_text`): SQLAlchemy copia en
el mensaje de sus excepciones los parámetros de la consulta
(`[parameters: ('juan@x.com', '$2b$12$…')]`) y PostgreSQL, los valores de la
fila rechazada. El motor ya se crea con `hide_parameters=True`
(`core/database.py`); esto es la segunda barrera.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[oculto]"

# Palabras que vuelven sensible un campo. `code` cubre los códigos de vista
# previa, que valen como una contraseña mientras no se canjean.
SENSITIVE_WORDS = frozenset({"authorization", "code", "key", "password", "secret", "token"})
# Nombres enteros que no se dejan partir en palabras sensibles.
SENSITIVE_NAMES = frozenset({"apikey", "passwd", "pwd", "cookie", "set-cookie"})

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
    if name.lower() in SENSITIVE_NAMES:
        return True
    # `passwords` o `tokens` también: se compara sin la `s` final.
    return any({word, word.removesuffix("s")} & SENSITIVE_WORDS for word in _words(name))


# Lo que SQLAlchemy agrega al mensaje de sus excepciones. Los parámetros van en
# una sola línea (son un `repr`) y terminan en `]` al final de la línea; si el
# texto viene recortado y falta, se tapa hasta el final.
_SQL_PARAMETERS = re.compile(r"\[parameters: .*?(?:\](?=[ \t]*(?:\r?\n|$))|\Z)", re.DOTALL)
_SQL_STATEMENT = re.compile(
    r"\[SQL: (?P<sql>.*?)\](?=\s*(?:\[parameters|\[SQL parameters|\(Background on this error|\Z))",
    re.DOTALL,
)
# Un valor escrito dentro de la sentencia (`WHERE email = 'juan@x.com'`).
_SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# El detalle de PostgreSQL de una clave repetida o de una fila rechazada.
_PG_KEY_DETAIL = re.compile(r"(Key \([^)\n]*\)=\().*?(\) (?:already exists|is not present))")
_PG_FAILING_ROW = re.compile(r"(Failing row contains \().*$", re.MULTILINE)


def _hide_sql_literals(match: re.Match[str]) -> str:
    return "[SQL: " + _SQL_STRING_LITERAL.sub(f"'{REDACTED}'", match["sql"]) + "]"


def scrub_text(text: str) -> str:
    """Tapa en un texto los valores de consultas y filas que copian los errores."""
    if "[parameters: " in text:
        text = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", text)
    if "[SQL: " in text:
        text = _SQL_STATEMENT.sub(_hide_sql_literals, text)
    if "Key (" in text:
        text = _PG_KEY_DETAIL.sub(rf"\g<1>{REDACTED}\g<2>", text)
    if "Failing row contains" in text:
        text = _PG_FAILING_ROW.sub(rf"\g<1>{REDACTED})", text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Copia `value` con las claves sensibles reemplazadas, en cualquier nivel."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key) else redact(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item, _depth + 1) for item in value]
    return value
