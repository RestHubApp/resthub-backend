"""Lector hacia los nombres del personal, que posee `accounts`.

La bandeja de mermas por aprobar muestra quién registró cada una y quién la
decidió. `inventory` no puede importar `accounts`, así que la pregunta se
declara acá y un adaptador la responde leyendo la tabla ajena. Se lee, nunca se
escribe.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol


class StaffDirectory(Protocol):
    async def names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]:
        """El nombre completo de cada usuario, por su identificador.

        Un usuario que no es del restaurante no aparece.
        """
        ...
