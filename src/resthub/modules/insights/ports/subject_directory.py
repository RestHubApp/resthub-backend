"""Puerto de lectura de los asuntos sobre los que se decidió.

Una decisión apunta a una fila de otro módulo por su tipo y su identificador:
un insumo, un pedido, un plato de un pedido o un movimiento de stock. Para
auditarla hace falta nombrarla como se nombra en el local: el insumo por su
nombre, el pedido por su número del día. Las tablas son de `orders` e
`inventory`; este módulo solo las lee.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from resthub.modules.insights.domain.decisions import SubjectType

type SubjectKey = tuple[SubjectType, int]


@dataclass(frozen=True, slots=True)
class SubjectDescription:
    # El número del día del pedido, si el asunto es un pedido o uno de sus platos.
    order_number: int | None = None
    # El nombre del insumo o del plato; `None` si el asunto es el pedido entero.
    label: str | None = None


class SubjectDirectory(Protocol):
    async def describe(
        self, restaurant_id: int, subjects: Collection[SubjectKey]
    ) -> dict[SubjectKey, SubjectDescription]:
        """Cómo se nombra cada asunto. Uno borrado o de otro restaurante no aparece."""
        ...
