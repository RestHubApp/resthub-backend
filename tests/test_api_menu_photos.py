"""La foto de un plato por HTTP: quién la sube, qué se acepta y que no cruce de local."""

from __future__ import annotations

import base64
import io

from httpx import AsyncClient
from PIL import Image

from resthub.modules.menu.adapters.images.pillow_photo_encoder import (
    MAX_PHOTO_SIDE,
    reencode_photo,
)
from resthub.modules.menu.domain.photos import MAX_PHOTO_BYTES, detect_image_type
from tests.conftest import StaffedRestaurant, authorization_for, url

MENU_URL = url("menu")
# La etiqueta EXIF de la ubicación (GPSInfo) y la del autor.
EXIF_GPS_TAG = 0x8825
EXIF_ARTIST_TAG = 0x013B


def _image(image_format: str, size: tuple[int, int] = (40, 30), *, exif: bool = False) -> bytes:
    image = Image.new("RGB", size, (200, 80, 40))
    output = io.BytesIO()
    if exif:
        tags = Image.Exif()
        tags[EXIF_ARTIST_TAG] = "Juana Perez"
        tags[EXIF_GPS_TAG] = {1: "S", 2: (12.0, 3.0, 0.0)}
        image.save(output, format=image_format, exif=tags.tobytes())
    else:
        image.save(output, format=image_format)
    return output.getvalue()


def _decoded(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


JPEG = _image("JPEG")
PNG = _image("PNG")
WEBP = _image("WEBP")


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
    # Se guarda la imagen vuelta a codificar, no los bytes que llegaron.
    assert image.headers["content-type"] == "image/webp"
    assert detect_image_type(image.content) == "image/webp"
    assert _decoded(image.content).size == (40, 30)
    assert "private" in image.headers["cache-control"]

    replaced = await client.put(f"{MENU_URL}/items/{dish}/photo", json=_encode(JPEG), headers=admin)
    assert replaced.status_code == 200
    assert replaced.json()["photo_version"] >= version
    image = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=waiter)
    assert image.headers["content-type"] == "image/webp"

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
    assert own.status_code == 200
    assert own.headers["content-type"] == "image/webp"


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


async def test_upload_is_resized_and_loses_its_metadata(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)
    original = _image("JPEG", (1600, 900), exif=True)
    assert _decoded(original).getexif()[EXIF_ARTIST_TAG] == "Juana Perez"

    uploaded = await client.put(
        f"{MENU_URL}/items/{dish}/photo", json=_encode(original), headers=admin
    )
    stored = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=admin)

    assert uploaded.status_code == 200, uploaded.text
    image = _decoded(stored.content)
    assert image.format == "WEBP"
    assert image.size == (MAX_PHOTO_SIDE, 450)
    assert not image.getexif()
    assert "exif" not in image.info
    assert "icc_profile" not in image.info
    assert b"Juana" not in stored.content


async def test_undecodable_and_polyglot_photos_are_rejected(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)
    photo_url = f"{MENU_URL}/items/{dish}/photo"
    # La firma de una PNG con basura detrás: pasa la prueba de los primeros bytes.
    garbage = b"\x89PNG\r\n\x1a\n" + b"<script>alert(1)</script>" * 4
    # Una GIF disfrazada de WebP en el contenedor RIFF.
    disguised = b"RIFF\x24\x00\x00\x00WEBP" + _image("GIF")

    for data in (garbage, disguised):
        response = await client.put(photo_url, json=_encode(data), headers=admin)
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_menu_item"
    dish_after = await client.get(MENU_URL, headers=admin)
    assert dish_after.json()["categories"][0]["items"][0]["photo_version"] is None


async def test_a_polyglot_photo_is_stored_without_its_payload(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    admin = authorization_for(restaurant_a.admin)
    dish = await _dish(client, restaurant_a)
    polyglot = JPEG + b"<html><script>alert(document.cookie)</script></html>"

    uploaded = await client.put(
        f"{MENU_URL}/items/{dish}/photo", json=_encode(polyglot), headers=admin
    )
    stored = await client.get(f"{MENU_URL}/items/{dish}/photo", headers=admin)

    assert uploaded.status_code == 200
    assert b"<script>" not in stored.content
    assert _decoded(stored.content).format == "WEBP"


def test_reencoding_keeps_transparency_and_refuses_huge_images() -> None:
    transparent = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(transparent, format="PNG")
    bomb = io.BytesIO()
    # 64 megapíxeles de un bit: pocos KB en disco, gigas al decodificar en color.
    Image.new("1", (8000, 8000)).save(bomb, format="PNG")

    encoded = reencode_photo(transparent.getvalue())

    assert encoded is not None
    assert _decoded(encoded).mode == "RGBA"
    assert len(bomb.getvalue()) < MAX_PHOTO_BYTES
    assert reencode_photo(bomb.getvalue()) is None
