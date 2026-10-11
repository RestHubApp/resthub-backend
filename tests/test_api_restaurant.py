"""El restaurante propio por HTTP."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import StaffedRestaurant, authorization_for, url

URL = url("restaurant")


async def test_each_account_reads_its_own_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    del restaurant_a
    response = await client.get(URL, headers=authorization_for(restaurant_b.waiter))

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["slug"], body["timezone"]) == (
        restaurant_b.id,
        "local-b",
        "America/Lima",
    )


async def test_owner_edits_name_and_timezone(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL,
        json={"name": "  Cevichería   Doña Rosa ", "timezone": "America/Bogota"},
        headers=authorization_for(restaurant_a.admin),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Cevichería Doña Rosa"
    assert response.json()["timezone"] == "America/Bogota"
    # El slug no cambia, y el otro restaurante tampoco.
    assert response.json()["slug"] == "local-a"
    other = await client.get(URL, headers=authorization_for(restaurant_b.admin))
    assert other.json()["name"] == "Restaurante local-b"


async def test_new_name_shows_in_the_session(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    await client.patch(
        URL, json={"name": "Doña Rosa"}, headers=authorization_for(restaurant_a.admin)
    )

    me = await client.get(url("auth/me"), headers=authorization_for(restaurant_a.waiter))

    assert me.json()["restaurant"]["name"] == "Doña Rosa"


async def test_unknown_timezone_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL, json={"timezone": "Hora de Lima"}, headers=authorization_for(restaurant_a.admin)
    )

    assert response.status_code == 422


async def test_waiter_cannot_edit_the_restaurant(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.patch(
        URL, json={"name": "Intruso"}, headers=authorization_for(restaurant_a.waiter)
    )

    assert response.status_code == 403


async def test_no_credential_no_restaurant(client: AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
