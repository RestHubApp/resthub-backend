"""Lector hacia los nombres del personal, que posee `accounts`.

El tablero muestra quién tomó cada pedido. Se lee por SQL desde un adaptador;
nunca se escribe.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol


class StaffDirectory(Protocol):
    async def names(self, restaurant_id: int, user_ids: Collection[int]) -> dict[int, str]: ...
