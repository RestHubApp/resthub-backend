"""Lector hacia las cuentas de la administración del sistema (`platform_admins`).

Un código de vista previa lo pidió una cuenta de plataforma. Si esa cuenta se
desactiva, sus vistas previas dejan de servir: el canje lo pregunta acá.
`accounts` no importa `platform`, así que la pregunta es un puerto propio y un
adaptador la responde leyendo la tabla ajena. Se lee, nunca se escribe.
"""

from __future__ import annotations

from typing import Protocol


class PlatformAdminDirectory(Protocol):
    async def is_active(self, admin_id: int) -> bool:
        """Si la cuenta de plataforma existe y está activa."""
        ...
