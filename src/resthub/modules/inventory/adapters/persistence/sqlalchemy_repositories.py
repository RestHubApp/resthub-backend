from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page
from resthub.modules.inventory.adapters.persistence.mappers import (
    ingredient_to_entity,
    ingredient_to_row,
    line_to_entity,
    movement_to_entity,
    movement_to_row,
)
from resthub.modules.inventory.adapters.persistence.models import (
    IngredientRow,
    RecipeLineRow,
    StockMovementRow,
)
from resthub.modules.inventory.domain.entities import (
    QUANTITY_STEP,
    Ingredient,
    MovementKind,
    RecipeLine,
    StockMovement,
)
from resthub.modules.inventory.domain.exceptions import IngredientNotFound
from resthub.modules.inventory.ports.stock_ledger import MovementQuery


class SqlAlchemyIngredientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, ingredient: Ingredient) -> Ingredient:
        row = ingredient_to_row(ingredient)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return ingredient_to_entity(row)

    async def get(self, restaurant_id: int, ingredient_id: int) -> Ingredient | None:
        row = await self._row(restaurant_id, ingredient_id)
        return ingredient_to_entity(row) if row else None

    async def get_many(
        self, restaurant_id: int, ingredient_ids: Collection[int]
    ) -> dict[int, Ingredient]:
        if not ingredient_ids:
            return {}
        result = await self._session.execute(
            select(IngredientRow).where(
                IngredientRow.restaurant_id == restaurant_id,
                IngredientRow.id.in_(list(ingredient_ids)),
            )
        )
        return {row.id: ingredient_to_entity(row) for row in result.scalars().all()}

    async def find_by_name(self, restaurant_id: int, name: str) -> Ingredient | None:
        result = await self._session.execute(
            select(IngredientRow).where(
                IngredientRow.restaurant_id == restaurant_id,
                func.lower(IngredientRow.name) == name.lower(),
            )
        )
        row = result.scalars().first()
        return ingredient_to_entity(row) if row else None

    async def list_all(self, restaurant_id: int) -> list[Ingredient]:
        result = await self._session.execute(
            select(IngredientRow)
            .where(IngredientRow.restaurant_id == restaurant_id)
            .order_by(IngredientRow.name, IngredientRow.id)
        )
        return [ingredient_to_entity(row) for row in result.scalars().all()]

    async def save(self, ingredient: Ingredient) -> Ingredient:
        row = await self._row(ingredient.restaurant_id, ingredient.id or 0)
        if row is None:
            raise IngredientNotFound(ingredient.id or 0)
        row.name = ingredient.name
        row.min_stock = ingredient.min_stock
        row.unit_cost = ingredient.unit_cost
        row.is_active = ingredient.is_active
        await self._session.flush()
        return ingredient_to_entity(row)

    async def _row(self, restaurant_id: int, ingredient_id: int) -> IngredientRow | None:
        result = await self._session.execute(
            select(IngredientRow).where(
                IngredientRow.id == ingredient_id, IngredientRow.restaurant_id == restaurant_id
            )
        )
        return result.scalar_one_or_none()


class SqlAlchemyStockLedger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, movement: StockMovement) -> StockMovement:
        return (await self.add_many([movement]))[0]

    async def add_many(self, movements: list[StockMovement]) -> list[StockMovement]:
        rows = [movement_to_row(movement) for movement in movements]
        self._session.add_all(rows)
        await self._session.flush()
        return [movement_to_entity(row) for row in rows]

    async def stock_of(
        self, restaurant_id: int, ingredient_ids: Collection[int] | None = None
    ) -> dict[int, Decimal]:
        statement = (
            select(StockMovementRow.ingredient_id, func.sum(StockMovementRow.quantity))
            .where(StockMovementRow.restaurant_id == restaurant_id)
            .group_by(StockMovementRow.ingredient_id)
        )
        if ingredient_ids is not None:
            statement = statement.where(StockMovementRow.ingredient_id.in_(list(ingredient_ids)))
        result = await self._session.execute(statement)
        # SQLite suma en coma flotante; se vuelve a la escala del libro para
        # que 0.1 + 0.2 siga siendo 0.3.
        return {
            int(ingredient_id): Decimal(str(total)).quantize(QUANTITY_STEP)
            for ingredient_id, total in result.all()
        }

    async def search(self, query: MovementQuery) -> Page[StockMovement]:
        base = select(StockMovementRow).where(StockMovementRow.restaurant_id == query.restaurant_id)
        if query.ingredient_id is not None:
            base = base.where(StockMovementRow.ingredient_id == query.ingredient_id)
        if query.kinds:
            base = base.where(StockMovementRow.kind.in_([kind.value for kind in query.kinds]))
        if query.order_id is not None:
            base = base.where(StockMovementRow.order_id == query.order_id)

        total = int(
            (
                await self._session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
        )
        result = await self._session.execute(
            base.order_by(StockMovementRow.created_at.desc(), StockMovementRow.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        return Page(items=[movement_to_entity(row) for row in result.scalars().all()], total=total)

    async def consumed_order_items(
        self, restaurant_id: int, order_item_ids: Collection[int]
    ) -> set[int]:
        if not order_item_ids:
            return set()
        result = await self._session.execute(
            select(StockMovementRow.order_item_id)
            .where(
                StockMovementRow.restaurant_id == restaurant_id,
                StockMovementRow.order_item_id.in_(list(order_item_ids)),
                StockMovementRow.kind == MovementKind.CONSUMPTION.value,
            )
            .distinct()
        )
        return {int(item_id) for item_id in result.scalars().all() if item_id is not None}


class SqlAlchemyRecipeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lines_for(self, restaurant_id: int, menu_item_id: int) -> list[RecipeLine]:
        return (await self.lines_for_many(restaurant_id, [menu_item_id])).get(menu_item_id, [])

    async def lines_for_many(
        self, restaurant_id: int, menu_item_ids: Collection[int] | None = None
    ) -> dict[int, list[RecipeLine]]:
        statement = select(RecipeLineRow).where(RecipeLineRow.restaurant_id == restaurant_id)
        if menu_item_ids is not None:
            statement = statement.where(RecipeLineRow.menu_item_id.in_(list(menu_item_ids)))
        result = await self._session.execute(statement.order_by(RecipeLineRow.id))
        recipes: dict[int, list[RecipeLine]] = {}
        for row in result.scalars().all():
            recipes.setdefault(row.menu_item_id, []).append(line_to_entity(row))
        return recipes

    async def replace(self, restaurant_id: int, menu_item_id: int, lines: list[RecipeLine]) -> None:
        await self._session.execute(
            delete(RecipeLineRow).where(
                RecipeLineRow.restaurant_id == restaurant_id,
                RecipeLineRow.menu_item_id == menu_item_id,
            )
        )
        self._session.add_all(
            RecipeLineRow(
                restaurant_id=restaurant_id,
                menu_item_id=menu_item_id,
                ingredient_id=line.ingredient_id,
                quantity=line.quantity,
            )
            for line in lines
        )
        await self._session.flush()
