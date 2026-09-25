"""Pedido servido → consumo de insumos.

`orders` declara el puerto `ServedOrderHook` sin saber quién lo escucha, e
`inventory` expone el caso de uso `ConsumeServedOrder` sin saber qué es un
pedido. Este adaptador traduce uno al otro y `main.py` lo instala en lugar de la
dependencia por omisión de `orders`, que no hace nada.

Corre en la misma sesión, y por lo tanto en la misma transacción, que la
petición que sirvió el pedido: si el descuento falla, el pedido no queda
servido, y no puede haber un pedido servido sin su consumo registrado.
"""

from __future__ import annotations

from resthub.core.auth import SessionDep
from resthub.core.logs import get_logger
from resthub.modules.inventory.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyIngredientRepository,
    SqlAlchemyRecipeRepository,
    SqlAlchemyStockLedger,
)
from resthub.modules.inventory.use_cases.consume_served_order import (
    ConsumeServedOrder,
    ConsumeServedOrderCommand,
    ServedDish,
)
from resthub.modules.orders.ports.served_order_hook import ServedOrder, ServedOrderHook

logger = get_logger("resthub.inventory")


class InventoryConsumption:
    """Implementa el puerto de `orders` con el caso de uso de `inventory`."""

    def __init__(self, consume: ConsumeServedOrder) -> None:
        self._consume = consume

    async def order_served(self, served: ServedOrder) -> None:
        result = await self._consume(
            ConsumeServedOrderCommand(
                restaurant_id=served.restaurant_id,
                order_id=served.order_id,
                actor_id=served.actor_id,
                dishes=tuple(
                    ServedDish(
                        order_item_id=portion.order_item_id,
                        menu_item_id=portion.menu_item_id,
                        portions=portion.quantity,
                    )
                    for portion in served.portions
                ),
            )
        )
        if result.negative_ingredient_ids:
            # Se permite y no corta el servicio, pero queda a la vista: casi
            # siempre es una compra sin registrar o una receta mal cargada.
            logger.warning(
                "inventory.negative_stock",
                restaurant_id=served.restaurant_id,
                order_id=served.order_id,
                ingredient_ids=sorted(result.negative_ingredient_ids),
            )


def get_inventory_consumption(session: SessionDep) -> ServedOrderHook:
    return InventoryConsumption(
        ConsumeServedOrder(
            SqlAlchemyIngredientRepository(session),
            SqlAlchemyStockLedger(session),
            SqlAlchemyRecipeRepository(session),
        )
    )
