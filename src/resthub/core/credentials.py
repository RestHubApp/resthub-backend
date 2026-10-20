"""Reglas del correo y la contraseña de cualquier cuenta.

Las cumplen igual el personal de cada restaurante (`accounts`) y la
administración del sistema (`platform`). Viven en el núcleo para que haya una
sola regla sin que un módulo importe al otro; cada uno traduce el fallo a su
propio error de dominio.

Python puro: lo importa la capa de dominio.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache
from importlib.resources import files

MIN_PASSWORD_LENGTH = 10
# En caracteres, no en bytes: Argon2id acepta cualquier largo, así que tildes y emojis
# no restan (ASVS 6.2.9). El tope solo evita que alguien mande un texto enorme a hashear.
MAX_PASSWORD_LENGTH = 128
# Palabras del contexto que no pueden ir dentro de una contraseña (ASVS 6.2.11): el
# nombre del servicio siempre, y las de la cuenta y del local de al menos este largo.
SERVICE_NAME = "resthub"
MIN_CONTEXT_WORD_LENGTH = 4
_WORD_SEPARATORS = re.compile(r"[^0-9a-z]+")


def normalized_email(raw: str) -> str | None:
    """El correo sin espacios y en minúsculas, o `None` si no tiene forma de correo."""
    email = raw.strip().lower()
    local, separator, domain = email.partition("@")
    if not separator or not local or "." not in domain:
        return None
    return email


@cache
def common_passwords() -> frozenset[str]:
    """Las contraseñas más usadas que pasan el largo mínimo, en minúsculas.

    Lista local (`common_passwords.txt`): la contraseña nunca sale a un servicio
    de terceros para saber si es conocida. Se lee una vez por proceso.
    """
    content = (
        files(__package__ or "resthub.core")
        .joinpath("common_passwords.txt")
        .read_text(encoding="utf-8")
    )
    return frozenset(
        line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")
    )


def _fold(text: str) -> str:
    """En minúsculas y sin tildes: `José Núñez` es `jose nunez`."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def context_words(*values: str) -> frozenset[str]:
    """Las palabras de la cuenta y del local que una contraseña no puede llevar.

    Recibe el correo (se usa la parte antes de la `@`), el nombre de la persona y
    el nombre y el identificador del local. De cada uno salen sus palabras de al
    menos cuatro caracteres y el valor entero sin separadores («anapaz» de «Ana
    Paz»). `resthub` va siempre.
    """
    words = {SERVICE_NAME}
    for value in values:
        local = _fold(value).partition("@")[0]
        parts = [part for part in _WORD_SEPARATORS.split(local) if part]
        candidates = [*parts, "".join(parts)]
        words.update(word for word in candidates if len(word) >= MIN_CONTEXT_WORD_LENGTH)
    return frozenset(words)


def password_problem(
    plain_password: str, context: frozenset[str] = frozenset({SERVICE_NAME})
) -> str | None:
    """Qué le falta a una contraseña nueva, o `None` si sirve.

    `context` son las palabras de `context_words`: el nombre, el correo, el local.
    """
    if len(plain_password) < MIN_PASSWORD_LENGTH:
        return f"La contraseña necesita al menos {MIN_PASSWORD_LENGTH} caracteres."
    if len(plain_password) > MAX_PASSWORD_LENGTH:
        return f"La contraseña no puede pasar de {MAX_PASSWORD_LENGTH} caracteres."
    # Sin distinguir mayúsculas: «Password123» se adivina igual que «password123».
    if plain_password.strip().lower() in common_passwords():
        return "Esa contraseña es de las más usadas y se adivina fácil: elige otra."
    folded = "".join(_fold(plain_password).split())
    if any(word in folded for word in context | {SERVICE_NAME}):
        return (
            "La contraseña no puede llevar tu nombre, tu correo, el nombre del local "
            "ni «RestHub»: se adivina fácil. Elige otra."
        )
    return None
