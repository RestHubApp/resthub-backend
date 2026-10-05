"""Puerto: si el local deja que sus datos vayan a la IA externa.

La preferencia la posee `restaurants`; aquí solo se lee.
"""

from __future__ import annotations

from typing import Protocol


class AiPreferences(Protocol):
    async def allows_external_ai(self, restaurant_id: int) -> bool: ...
