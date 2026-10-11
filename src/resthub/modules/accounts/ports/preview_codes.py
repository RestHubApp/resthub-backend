"""Puerto de los códigos de vista previa."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from resthub.modules.accounts.domain.preview import PreviewCode, PreviewGrant


class PreviewCodeRepository(Protocol):
    async def add(self, code: PreviewCode) -> None: ...

    async def purge_created_before(self, cutoff: datetime) -> None:
        """Borra los códigos creados antes de `cutoff`: vencidos hace rato, usados o no."""
        ...

    async def consume(self, code_hash: str, now: datetime) -> PreviewGrant | None:
        """Lo marca usado y devuelve lo que habilita, o `None` si no existe, venció o ya se usó.

        Tiene que ser atómico: dos canjes a la vez del mismo código dan uno
        solo que funciona.
        """
        ...
