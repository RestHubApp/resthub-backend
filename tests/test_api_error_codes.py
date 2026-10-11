"""Toda respuesta de error lleva `code` y conserva su `detail`."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from resthub.core.error_codes import ErrorCode
from tests.conftest import VALID_PASSWORD, StaffedRestaurant, authorization_for, url

LOGIN_BODY = {"email": "nadie@example.com", "password": "no-es-esta-1"}


async def test_missing_credentials_are_unauthenticated(client: AsyncClient) -> None:
    response = await client.get(url("auth/me"))

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Falta la credencial de acceso.",
        "code": ErrorCode.UNAUTHENTICATED.value,
    }
    assert response.headers["www-authenticate"] == "Bearer"


async def test_wrong_login_keeps_its_detail_and_domain_code(client: AsyncClient) -> None:
    response = await client.post(url("auth/login"), json=LOGIN_BODY)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "El correo o la contraseña no son correctos.",
        "code": ErrorCode.INVALID_CREDENTIALS.value,
    }


async def test_repeated_failed_logins_are_throttled_with_retry_after(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    body = {"email": restaurant_a.admin.email, "password": "no-es-esta"}
    for _ in range(5):
        await client.post(url("auth/login"), json=body)
    blocked = await client.post(url("auth/login"), json={**body, "password": VALID_PASSWORD})

    assert blocked.status_code == 429
    assert blocked.json()["code"] == ErrorCode.LOGIN_THROTTLED.value
    assert "Retry-After" in blocked.headers


async def test_unknown_route_and_wrong_method_have_generic_codes(client: AsyncClient) -> None:
    missing = await client.get(url("no-existe"))
    wrong_method = await client.delete(url("health"))

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found", "code": ErrorCode.NOT_FOUND.value}
    assert wrong_method.status_code == 405
    assert wrong_method.json()["code"] == ErrorCode.METHOD_NOT_ALLOWED.value
    assert "allow" in wrong_method.headers


async def test_validation_error_keeps_the_field_list_in_detail_and_errors(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(
        url("orders"), params={"offset": -1}, headers=authorization_for(restaurant_a.admin)
    )

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["query", "offset"]
    assert body["errors"] == body["detail"]


async def test_missing_permission_is_forbidden_with_its_domain_code(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    response = await client.get(url("staff"), headers=authorization_for(restaurant_a.waiter))

    assert response.status_code == 403
    assert response.json()["code"] == ErrorCode.MISSING_PERMISSION.value


async def test_domain_not_found_and_business_rule_codes(
    client: AsyncClient, restaurant_a: StaffedRestaurant
) -> None:
    headers = authorization_for(restaurant_a.admin)

    not_found = await client.get(url("orders/999999"), headers=headers)
    invalid_period = await client.get(
        url("insights/summary"), params={"date_to": "0001-01-01"}, headers=headers
    )

    assert not_found.status_code == 404
    assert not_found.json()["code"] == ErrorCode.ORDER_NOT_FOUND.value
    assert invalid_period.status_code == 422
    assert invalid_period.json()["code"] == ErrorCode.INVALID_PERIOD.value


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_code_values_are_stable_snake_case(code: ErrorCode) -> None:
    assert code.value == code.name.lower()
