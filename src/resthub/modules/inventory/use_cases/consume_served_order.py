"""Descuento automático de insumos al servir un pedido.

No lo dispara nadie desde el inventario: lo llama la raíz de composición cuando
`orders` avisa que un pedido se sirvió. Este módulo no sabe qué es un pedido;
recibe platos servidos con su identificador de ítem y nada más.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.modules.inventory.domain.entities import MovementKind, StockMovement
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.recipe_repository import RecipeRepository
from resthub.modules.inventory.ports.stock_ledger import StockLedger


@dataclass(frozen=True, slots=True)
class ServedDish:
    order_item_id: int
    menu_item_id: int
    portions: int


@dataclass(frozen=True, slots=True)
class ConsumeServedOrderCommand:
    restaurant_id: int
    order_id: int
    actor_id: int
    dishes: tuple[ServedDish, ...]


@dataclass(frozen=True, slots=True)
class ConsumptionResult:
    movements: list[StockMovement]
    # Insumos que quedaron en negativo después de descontar. No es un error:
    # se informa para que alguien revise compras o recetas.
    negative_ingredient_ids: frozenset[int]


class ConsumeServedOrder:
    """Registra un consumo por insumo de receta y por plato servido.

    Es idempotente por ítem: un plato que ya descontó no vuelve a descontar.
    Así, si a un pedido servido se le agregan platos y se vuelve a servir, solo
    se descuentan los nuevos. Un plato sin receta no descuenta nada, y tampoco
    es un error: no todo lo que se vende se controla en el almacén.
    """

    def __init__(
        self,
        ingredients: IngredientRepository,
        ledger: StockLedger,
        recipes: RecipeRepository,
    ) -> None:
        self._ingredients = ingredients
        self._ledger = ledger
        self._recipes = recipes

    async def __call__(self, command: ConsumeServedOrderCommand) -> ConsumptionResult:
        already = await self._ledger.consumed_order_items(
            command.restaurant_id, {dish.order_item_id for dish in command.dishes}
        )
        pending = [dish for dish in command.dishes if dish.order_item_id not in already]
        if not pending:
            return ConsumptionResult(movements=[], negative_ingredient_ids=frozenset())

        recipes = await self._recipes.lines_for_many(
            command.restaurant_id, {dish.menu_item_id for dish in pending}
        )
        ingredients = await self._ingredients.get_many(
            command.restaurant_id,
            {line.ingredient_id for lines in recipes.values() for line in lines},
        )

        movements = [
            StockMovement(
                restaurant_id=command.restaurant_id,
                ingredient_id=line.ingredient_id,
                kind=MovementKind.CONSUMPTION,
                quantity=-(line.quantity * dish.portions),
                unit_cost=ingredients[line.ingredient_id].unit_cost,
                order_id=command.order_id,
                order_item_id=dish.order_item_id,
                created_by=command.actor_id,
            )
            for dish in pending
            for line in recipes.get(dish.menu_item_id, [])
            if line.ingredient_id in ingredients
        ]
        if not movements:
            return ConsumptionResult(movements=[], negative_ingredient_ids=frozenset())

        saved = await self._ledger.add_many(movements)
        touched = {movement.ingredient_id for movement in saved}
        stock = await self._ledger.stock_of(command.restaurant_id, touched)
        return ConsumptionResult(
            movements=saved,
            negative_ingredient_ids=frozenset(
                ingredient_id for ingredient_id, level in stock.items() if level < 0
            ),
        )
