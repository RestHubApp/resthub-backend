"""La foto de un plato por HTTP: quién la sube, qué se acepta y que no cruce de local."""

from __future__ import annotations

import base64

from httpx import AsyncClient

from resthub.modules.menu.domain.photos import MAX_PHOTO_BYTES, detect_image_type
from tests.conftest import StaffedRestaurant, authorization_for, url

MENU_URL = url("menu")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 32


def _encode(data: bytes) -> dict[str, str]:
    return {"data": base64.b64encode(data).decode("ascii")}


async def _dish(client: AsyncClient, restaurant: StaffedRestaurant) -> int:
    admin = authorization_for(restaurant.admin)
    category = await client.post(f"{MENU_URL}/categories", json={"name": "Fondos"}, headers=admin)
    dish = await client.post(
        f"{MENU_URL}/items",
        json={
            "category_id": category.json()["id"],
            "name": "Lomo saltado",
            "price": "28.00",
        },
        headers=admin,
    )
    assert dish.status_code == 201, dish.text
    assert dish.json()["photo_version"] is None
    return int(dish.json()["id"])


def test_image_type_comes_from_the_bytes() -> None:
    assert detect_image_type(JPEG) == "image/jpeg"
    assert detect_image_type(PNG) == "image/png"
    assert detect_image_type(WEBP) == "image/webp"
    assert detect_image_type(b"GIF89a" + b"\x00" * 32) is None
    assert detect_image_type(b"RIFF\x00\x00") is None


async def test_manager_uploads_replaces_and_removes_a_photo(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)

    uploaded = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(PNG), headers=admin)

    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()["photo_version"]
    assert isinstance(version, int)

    waiter = authorization_for(restaurant_a.waiter)
    menu = await client.get(MENU_URL, headers=waiter)
    assert menu.json()["categories"][0]["items"][0]["photo_version"] == version

    image = await client.get(f"{MENU_URL}/items/{dish}/photo?v={version}", headers=waiter)
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert "private" in image.headers["cache-control"]

    replaced = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(JPEG), headers=admin)
    assert replaced.status_code == 200
    image = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=waiter)
    assert image.content == JPEG
    assert image.headers["content-type"] == "image/jpeg"

    removed = await client.delete(f"{MENU_URL}/items/{dish}/photo", headers=admin)
    assert removed.status_code == 200
    assert removed.json()["photo_version"] is None
    gone = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=waiter)
    assert gone.status_code == 404

    again = await client.delete(f"{MENU_URL}/items/{dish}/photo", headers=admin)
    assert again.status_code == 200


async def test_waiter_cannot_change_photos(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    dish = await _dish(client, restaurant_a)
    waiter = authorization_for(restaurant_a.waiter)

    put = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(JPEG), headers=waiter)
    delete = await client.delete(f"{MENU_URL}/items/{dish}/photo", headers=waiter)

    assert put.status_code == 403
    assert delete.status_code == 403


async def test_invalid_photos_are_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)
    photo_url = f"{MENU_URL}/items/{dish}/photo"

    not_an_image = await client.put(photo_url, json=_encode(b"%PDF-1.7 hola"), headers=admin)
    too_big = await client.put(
        photo_url, json=_encode(JPEG + b"\x00" * MAX_PHOTO_BYTES), headers=admin
    )
    not_base64 = await client.put(photo_url, json={"data": "###"}, headers=admin)
    empty = await client.put(photo_url, json={"data": ""}, headers=admin)
    huge = await client.put(photo_url, json={"data": "A" * (MAX_PHOTO_BYTES * 2)}, headers=admin)

    assert not_an_image.status_code == 422
    assert too_big.status_code == 422
    assert not_base64.status_code == 422
    assert empty.status_code == 422
    assert huge.status_code == 422
    assert not_an_image.json()["code"] == "invalid_menu_item"


async def test_zero_length_decoded_photo_is_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)

    response = await client.put(
        f"{MENU_URL}/items/{dish}/photo", json={"data": "===="}, headers=admin
    )

    assert response.status_code == 422


async def test_photo_of_another_restaurant_is_not_found(
    client: AsyncClient, restaurant_a: StaffedRestaurant, restaurant_b: StaffedRestaurant
) -> None:
    dish = await _dish(client, restaurant_b)
    admin_b = authorization_for(restaurant_b.admin)
    await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(JPEG), headers=admin_b)
    admin_a = authorization_for(restaurant_a.admin)

    read = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=admin_a)
    put = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(PNG), headers=admin_a)
    delete = await client.delete(f"{MENU_URL}/items/{dish}/photo", headers=admin_a)

    assert read.status_code == 404
    assert put.status_code == 404
    assert delete.status_code == 404
    own = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=admin_b)
    assert own.content == JPEG


async def test_waiter_does_not_see_the_photo_of_a_withdrawn_dish(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    waiter = authorization_for(restaurant_a.waiter)
    dish = await _dish(client, restaurant_a)
    await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(JPEG), headers=admin)
    await client.patch(f"{MENU_URL}/items/{dish}", json={"is_active": False}, headers=admin)

    as_waiter = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=waiter)
    as_admin = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=admin)

    assert as_waiter.status_code == 404
    assert as_admin.status_code == 200


async def test_editing_a_dish_keeps_its_photo(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)
    uploaded = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(WEBP), headers=admin)

    edited = await client.patch(f"{MENU_URL}/items/{dish}", json={"price": "30.00"}, headers=admin)

    assert edited.json()["photo_version"] == uploaded.json()["photo_version"]
