"""Consumo automático de insumos al servir: la conexión entre pedidos e inventario."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
from tests.builders import Carta, carta
from tests.conftest import StaffedRestaurant, authorization_for

ORDERS_URL = "/api/v1/orders"
INVENTORY_URL = "/api/v1/inventory"


@dataclass(frozen=True, slots=True)
class Cocina:
    menu: Carta
    carne: int
    papa: int
    chicha: int


@pytest.fixture
async def cocina(client: AsyncClient, session: AsyncSession, local_a: StaffedRestaurant) -> Cocina:
    """Lomo con receta de carne y papa, chicha con receta y sin stock, ají sin receta."""
    menu = await carta(session, local_a.id)
    admin = authorization_for(local_a.admin)

    async def insumo(name: str, unit: str, compra: str | None) -> int:
        created = await client.post(
            f"{INVENTORY_URL}/ingredients",
            json={"name": name, "unit": unit, "min_stock": "100"},
            headers=admin,
        )
        ingredient_id = int(created.json()["id"])
        if compra is not None:
            await client.post(
                f"{INVENTORY_URL}/purchases",
                json={"ingredient_id": ingredient_id, "quantity": compra, "unit_cost": "0.01"},
                headers=admin,
            )
        return ingredient_id

    carne = await insumo("Lomo de res", "g", "1000")
    papa = await insumo("Papa amarilla", "g", "1000")
    chicha = await insumo("Chicha morada preparada", "ml", None)

    async def receta(dish: int, *lines: tuple[int, str]) -> None:
        response = await client.put(
            f"{INVENTORY_URL}/recipes/{dish}",
            json={"lines": [{"ingredient_id": i, "quantity": q} for i, q in lines]},
            headers=admin,
        )
        assert response.status_code == 200, response.text

    await receta(menu.lomo, (carne, "180"), (papa, "150"))
    await receta(menu.chicha, (chicha, "400"))
    return Cocina(menu=menu, carne=carne, papa=papa, chicha=chicha)


async def _stock(client: AsyncClient, local: StaffedRestaurant, ingredient_id: int) -> str:
    response = await client.get(
        f"{INVENTORY_URL}/ingredients/{ingredient_id}", headers=authorization_for(local.admin)
    )
    return str(response.json()["stock"])


async def _consumos(client: AsyncClient, local: StaffedRestaurant, order_id: int) -> list[Any]:
    response = await client.get(
        f"{INVENTORY_URL}/movements",
        params={"order_id": order_id, "kind": "consumption"},
        headers=authorization_for(local.admin),
    )
    return list(response.json()["items"])


async def _hasta_servido(client: AsyncClient, local: StaffedRestaurant, order_id: int) -> None:
    mesero, encargado = authorization_for(local.waiter), authorization_for(local.admin)
    for action, headers in (("ready", encargado), ("served", mesero)):
        response = await client.post(f"{ORDERS_URL}/{order_id}/{action}", headers=headers)
        assert response.status_code == 200, response.text


async def _pedido(
    client: AsyncClient, local: StaffedRestaurant, cocina: Cocina, *items: tuple[int, int]
) -> int:
    mesero = authorization_for(local.waiter)
    created = await client.post(
        ORDERS_URL,
        json={
            "type": "dine_in",
            "table_id": cocina.menu.mesa_1,
            "items": [{"menu_item_id": d, "quantity": q} for d, q in items],
        },
        headers=mesero,
    )
    order_id = int(created.json()["id"])
    await client.post(f"{ORDERS_URL}/{order_id}/send", headers=mesero)
    return order_id


async def test_servir_descuenta_segun_las_recetas(
    client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.lomo, 2), (cocina.menu.aji, 1))
    assert await _stock(client, local_a, cocina.carne) == "1000.000"

    await _hasta_servido(client, local_a, order_id)

    assert await _stock(client, local_a, cocina.carne) == "640.000"
    assert await _stock(client, local_a, cocina.papa) == "700.000"
    consumos = await _consumos(client, local_a, order_id)
    assert sorted(c["quantity"] for c in consumos) == ["-300.000", "-360.000"]
    assert {c["created_by"] for c in consumos} == {local_a.waiter.id}
    assert all(c["unit_cost"] == "0.010000" for c in consumos)
    pedido = await client.get(f"{ORDERS_URL}/{order_id}", headers=authorization_for(local_a.admin))
    assert {c["order_number"] for c in consumos} == {pedido.json()["number"]}


async def test_un_plato_sin_receta_no_descuenta_nada(
    client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.aji, 3))

    await _hasta_servido(client, local_a, order_id)

    assert await _consumos(client, local_a, order_id) == []


async def test_volver_a_servir_solo_descuenta_lo_nuevo(
    client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.lomo, 1))
    await _hasta_servido(client, local_a, order_id)

    otra_vuelta = await client.post(
        f"{ORDERS_URL}/{order_id}/items",
        json={"items": [{"menu_item_id": cocina.menu.lomo, "quantity": 2}]},
        headers=authorization_for(local_a.waiter),
    )
    assert otra_vuelta.json()["status"] == "in_kitchen"
    await _hasta_servido(client, local_a, order_id)

    # 180 por la primera porción y 360 por las dos nuevas; nada dos veces.
    assert await _stock(client, local_a, cocina.carne) == "460.000"
    assert len(await _consumos(client, local_a, order_id)) == 4


async def test_el_consumo_es_idempotente_aunque_llegue_dos_veces(
    session: AsyncSession, client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.lomo, 1))
    await _hasta_servido(client, local_a, order_id)
    detalle = await client.get(f"{ORDERS_URL}/{order_id}", headers=authorization_for(local_a.admin))
    item_id = detalle.json()["items"][0]["id"]
    consumir = ConsumeServedOrder(
        SqlAlchemyIngredientRepository(session),
        SqlAlchemyStockLedger(session),
        SqlAlchemyRecipeRepository(session),
    )

    repetido = await consumir(
        ConsumeServedOrderCommand(
            restaurant_id=local_a.id,
            order_id=order_id,
            actor_id=local_a.admin.id or 0,
            dishes=(ServedDish(order_item_id=item_id, menu_item_id=cocina.menu.lomo, portions=1),),
        )
    )

    assert repetido.movements == []
    stock = await SqlAlchemyStockLedger(session).stock_of(local_a.id, [cocina.carne])
    assert stock[cocina.carne] == Decimal("820.000")


async def test_el_stock_negativo_se_permite_y_se_reporta(
    client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    # Con los platos sin insumos agotándose solos no se podría pedir; un local
    # que todavía no lleva el stock al día lo apaga y el plato sale igual.
    await client.patch(
        "/api/v1/restaurant",
        json={"auto_out_of_stock": False},
        headers=authorization_for(local_a.admin),
    )
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.chicha, 2))

    await _hasta_servido(client, local_a, order_id)

    assert await _stock(client, local_a, cocina.chicha) == "-800.000"
    alertas = await client.get(
        f"{INVENTORY_URL}/alerts/low-stock", headers=authorization_for(local_a.admin)
    )
    primera = alertas.json()[0]
    assert (primera["id"], primera["is_negative"]) == (cocina.chicha, True)


async def test_cancelar_lo_servido_no_devuelve_insumos(
    client: AsyncClient, local_a: StaffedRestaurant, cocina: Cocina
) -> None:
    order_id = await _pedido(client, local_a, cocina, (cocina.menu.lomo, 1))
    await _hasta_servido(client, local_a, order_id)

    await client.post(
        f"{ORDERS_URL}/{order_id}/cancel",
        json={"reason": "El cliente se fue sin pagar"},
        headers=authorization_for(local_a.admin),
    )

    assert await _stock(client, local_a, cocina.carne) == "820.000"
