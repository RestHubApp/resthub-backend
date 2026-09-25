"""Casos de uso de las recetas y su costo."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.modules.inventory.domain.entities import (
    Ingredient,
    RecipeLine,
    recipe_cost,
    validate_recipe,
)
from resthub.modules.inventory.domain.exceptions import (
    DishNotFound,
    IngredientInactive,
    IngredientNotFound,
)
from resthub.modules.inventory.ports.dish_directory import Dish, DishDirectory
from resthub.modules.inventory.ports.ingredient_repository import IngredientRepository
from resthub.modules.inventory.ports.recipe_repository import RecipeRepository

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class RecipeLineView:
    line: RecipeLine
    ingredient: Ingredient

    @property
    def cost(self) -> Decimal:
        return self.line.quantity * self.ingredient.unit_cost


@dataclass(frozen=True, slots=True)
class DishCost:
    """El costo de una porción frente a su precio: la base del margen por plato."""

    dish: Dish
    # `None` cuando el plato no tiene receta: no es que cueste cero, es que no
    # se sabe. Mostrarlo como cero inflaría el margen.
    cost: Decimal | None

    @property
    def has_recipe(self) -> bool:
        return self.cost is not None

    @property
    def rounded_cost(self) -> Decimal | None:
        return self.cost.quantize(CENT, ROUND_HALF_UP) if self.cost is not None else None

    @property
    def margin(self) -> Decimal | None:
        if self.cost is None:
            return None
        return (self.dish.price - self.cost).quantize(CENT, ROUND_HALF_UP)

    @property
    def margin_percent(self) -> Decimal | None:
        if self.cost is None or self.dish.price <= 0:
            return None
        return ((self.dish.price - self.cost) * 100 / self.dish.price).quantize(
            Decimal("0.1"), ROUND_HALF_UP
        )


@dataclass(frozen=True, slots=True)
class RecipeView:
    dish: Dish
    lines: list[RecipeLineView]

    @property
    def cost(self) -> DishCost:
        if not self.lines:
            return DishCost(dish=self.dish, cost=None)
        return DishCost(dish=self.dish, cost=sum((line.cost for line in self.lines), Decimal(0)))


async def _find_dish(dishes: DishDirectory, restaurant_id: int, menu_item_id: int) -> Dish:
    dish = await dishes.get(restaurant_id, menu_item_id)
    if dish is None:
        raise DishNotFound(menu_item_id)
    return dish


async def _describe(
    ingredients: IngredientRepository, dish: Dish, restaurant_id: int, lines: list[RecipeLine]
) -> RecipeView:
    by_id = await ingredients.get_many(restaurant_id, {line.ingredient_id for line in lines})
    return RecipeView(
        dish=dish,
        lines=[
            RecipeLineView(line=line, ingredient=by_id[line.ingredient_id])
            for line in lines
            if line.ingredient_id in by_id
        ],
    )


class ReadRecipe:
    def __init__(
        self,
        recipes: RecipeRepository,
        ingredients: IngredientRepository,
        dishes: DishDirectory,
    ) -> None:
        self._recipes = recipes
        self._ingredients = ingredients
        self._dishes = dishes

    async def __call__(self, restaurant_id: int, menu_item_id: int) -> RecipeView:
        dish = await _find_dish(self._dishes, restaurant_id, menu_item_id)
        lines = await self._recipes.lines_for(restaurant_id, menu_item_id)
        return await _describe(self._ingredients, dish, restaurant_id, lines)


@dataclass(frozen=True, slots=True)
class ReplaceRecipeCommand:
    restaurant_id: int
    actor_id: int
    menu_item_id: int
    lines: list[RecipeLine]


class ReplaceRecipe:
    """Guarda la receta completa de un plato. Una lista vacía la borra.

    Cambiar la receta no toca lo ya consumido: los movimientos pasados
    guardaron su cantidad, y el libro no se reescribe.
    """

    def __init__(
        self,
        recipes: RecipeRepository,
        ingredients: IngredientRepository,
        dishes: DishDirectory,
        activity: ActivityRecorder,
    ) -> None:
        self._recipes = recipes
        self._ingredients = ingredients
        self._dishes = dishes
        self._activity = activity

    async def __call__(self, command: ReplaceRecipeCommand) -> RecipeView:
        dish = await _find_dish(self._dishes, command.restaurant_id, command.menu_item_id)
        lines = validate_recipe(command.lines)
        found = await self._ingredients.get_many(
            command.restaurant_id, {line.ingredient_id for line in lines}
        )
        for line in lines:
            ingredient = found.get(line.ingredient_id)
            if ingredient is None:
                raise IngredientNotFound(line.ingredient_id)
            if not ingredient.is_active:
                raise IngredientInactive(ingredient.name)

        await self._recipes.replace(command.restaurant_id, command.menu_item_id, lines)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.RECIPE_UPDATED,
            f"{dish.name}: {len(lines)} insumos",
        )
        return await _describe(self._ingredients, dish, command.restaurant_id, lines)


class ListDishCosts:
    """Costo y margen de cada plato de la carta, con receta o sin ella."""

    def __init__(
        self,
        recipes: RecipeRepository,
        ingredients: IngredientRepository,
        dishes: DishDirectory,
    ) -> None:
        self._recipes = recipes
        self._ingredients = ingredients
        self._dishes = dishes

    async def __call__(self, restaurant_id: int) -> list[DishCost]:
        recipes = await self._recipes.lines_for_many(restaurant_id)
        ingredients = {
            ingredient.id: ingredient
            for ingredient in await self._ingredients.list_all(restaurant_id)
            if ingredient.id is not None
        }
        return [
            DishCost(
                dish=dish,
                cost=recipe_cost(recipes[dish.id], ingredients) if dish.id in recipes else None,
            )
            for dish in await self._dishes.list_all(restaurant_id)
        ]
