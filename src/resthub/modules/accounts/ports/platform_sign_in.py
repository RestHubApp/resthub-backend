"""Puerto hacia el acceso de la administración del sistema.

El acceso es único: `POST /auth/login` prueba primero al personal y, si no hay
una credencial válida del personal, a la plataforma. `accounts` no importa
`platform`, así que lo que necesita de ella —verificar una credencial y releer
una cuenta— es este puerto, y `wiring/platform_sign_in.py` lo responde con los
casos de uso de `platform`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from resthub.core.identity import AccessToken


@dataclass(frozen=True, slots=True)
class PlatformAccount:
    id: int
    email: str
    full_name: str


@dataclass(frozen=True, slots=True)
class PlatformSignIn:
    account: PlatformAccount
    token: AccessToken


class PlatformSignInGateway(Protocol):
    async def authenticate(self, email: str, password: str) -> PlatformSignIn | None:
        """La cuenta y su token si la credencial es de una cuenta de plataforma activa.

        Deja el rastro del ingreso en la bitácora de la plataforma. `None` si
        la credencial no sirve, sin decir por qué.
        """
        ...

    async def read(self, admin_id: int, issued_at: float) -> PlatformAccount | None:
        """La cuenta releída de la base, o `None` si ya no está disponible.

        Desactivada, borrada o con un token anterior a su último corte de sesiones.
        """
        ...
