"""Avisos en tiempo real, sin tecnología.

Un aviso dice qué cambió y a quién le importa, nunca los datos: el navegador que
lo recibe vuelve a pedirlos por la API, y así la regla de quién puede ver qué
sigue viviendo en un solo lugar. Por eso alcanza con el restaurante, el tema,
las cuentas destinatarias y los permisos de quienes ven todo.

El envío concreto (en memoria, o repartido por Postgres entre procesos) es un
adaptador: vive en `realtime_broker` y este módulo no lo conoce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from resthub.core.identity import Principal

# Cambiaron el rol de la cuenta, los permisos de ese rol o su estado: la
# interfaz vuelve a pedir `/auth/me` para rearmar la navegación.
PERMISSIONS_TOPIC = "permissions"


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    # Un aviso nunca cruza de un restaurante a otro, aunque el permiso coincida.
    restaurant_id: int
    topic: str
    user_ids: frozenset[int] = field(default_factory=frozenset)
    # A cualquiera del local, como con los pedidos: el aviso no trae datos
    # y cada uno vuelve a pedir solo lo que su rol le deja ver.
    everyone: bool = False
    # Con tener uno de estos permisos se recibe el aviso aunque no se participe.
    permissions: frozenset[str] = field(default_factory=frozenset)
    reference_id: int | None = None

    def is_for(self, principal: Principal) -> bool:
        if principal.restaurant_id != self.restaurant_id:
            return False
        return (
            self.everyone
            or principal.user_id in self.user_ids
            or not self.permissions.isdisjoint(principal.permissions)
        )


class EventPublisher(Protocol):
    def publish(self, event: RealtimeEvent) -> None:
        """Encola el aviso; sale recién si la transacción en curso se confirma."""
