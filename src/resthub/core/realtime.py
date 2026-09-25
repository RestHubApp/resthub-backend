"""Avisos en tiempo real, sin tecnología.

Un aviso dice qué cambió y a quién le importa, nunca los datos: el navegador que
lo recibe vuelve a pedirlos por la API, y así la regla de quién puede ver qué
sigue viviendo en un solo lugar. Por eso alcanza con el restaurante, el tema,
las cuentas destinatarias y los roles que ven todo.

El envío concreto (en memoria, o repartido por Postgres entre procesos) es un
adaptador: vive en `realtime_broker` y este módulo no lo conoce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from resthub.core.identity import Principal, Role

# Cambiaron el rol o el estado de la cuenta: la interfaz vuelve a pedir
# `/auth/me` para rearmar la navegación con los permisos nuevos.
PERMISSIONS_TOPIC = "permissions"


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    # Un aviso nunca cruza de un restaurante a otro, aunque el rol coincida.
    restaurant_id: int
    topic: str
    user_ids: frozenset[int] = field(default_factory=frozenset)
    # Roles que reciben el aviso aunque no participen, como el encargado, que
    # ve todos los pedidos del local.
    roles: frozenset[Role] = field(default_factory=frozenset)
    reference_id: int | None = None

    def is_for(self, principal: Principal) -> bool:
        if principal.restaurant_id != self.restaurant_id:
            return False
        return principal.user_id in self.user_ids or principal.role in self.roles


class EventPublisher(Protocol):
    def publish(self, event: RealtimeEvent) -> None:
        """Encola el aviso; sale recién si la transacción en curso se confirma."""
