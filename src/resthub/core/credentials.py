"""Reglas del correo y la contraseña de cualquier cuenta.

Las cumplen igual el personal de cada restaurante (`accounts`) y la
administración del sistema (`platform`). Viven en el núcleo para que haya una
sola regla sin que un módulo importe al otro; cada uno traduce el fallo a su
propio error de dominio.

Python puro: lo importa la capa de dominio.
"""

from __future__ import annotations

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128


def normalized_email(raw: str) -> str | None:
    """El correo sin espacios y en minúsculas, o `None` si no tiene forma de correo."""
    email = raw.strip().lower()
    local, separator, domain = email.partition("@")
    if not separator or not local or "." not in domain:
        return None
    return email


def password_problem(plain_password: str) -> str | None:
    """Qué le falta a una contraseña nueva, o `None` si sirve."""
    if len(plain_password) < MIN_PASSWORD_LENGTH:
        return f"La contraseña necesita al menos {MIN_PASSWORD_LENGTH} caracteres."
    if len(plain_password) > MAX_PASSWORD_LENGTH:
        return f"La contraseña no puede pasar de {MAX_PASSWORD_LENGTH} caracteres."
    return None
