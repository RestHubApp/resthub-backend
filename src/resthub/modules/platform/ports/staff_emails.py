"""Lector hacia los correos del personal de los restaurantes (`users`).

Un correo no puede existir a la vez como cuenta de plataforma y como cuenta del
personal: el acceso es único (`POST /auth/login`) y entra por el correo. Este
puerto deja preguntarlo sin que `platform` conozca la tabla de `accounts`.
"""

from __future__ import annotations

from typing import Protocol


class StaffEmailDirectory(Protocol):
    async def exists(self, email: str) -> bool:
        """Si alguna cuenta del personal de cualquier restaurante usa ese correo."""
        ...
