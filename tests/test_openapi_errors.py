"""El esquema OpenAPI dice la verdad sobre los errores del API.

Schemathesis encontraba cientos de respuestas «no documentadas»: 401, 403, 404 y 409 que el
servidor devuelve pero el esquema no declaraba, y 422 con `detail` de texto (regla de negocio)
donde el esquema prometía solo la lista de validación de FastAPI.
"""

from __future__ import annotations

from fastapi import FastAPI

from resthub.core.openapi import ERROR_SCHEMA, ERROR_STATUSES
from tests.conftest import url

METHODS = {"get", "put", "post", "delete", "patch"}


def operations(app: FastAPI) -> list[tuple[str, dict[str, object]]]:
    paths = app.openapi()["paths"]
    return [
        (f"{method.upper()} {path}", operation)
        for path, item in paths.items()
        for method, operation in item.items()
        if method in METHODS
    ]


def test_every_operation_documents_the_errors_the_server_returns(app: FastAPI) -> None:
    for name, operation in operations(app):
        responses = operation["responses"]
        assert isinstance(responses, dict)
        for code in ERROR_STATUSES:
            assert str(code) in responses, f"{name} no documenta el {code}"


def test_unprocessable_responses_accept_both_error_shapes(app: FastAPI) -> None:
    schema = app.openapi()
    assert schema["components"]["schemas"][ERROR_SCHEMA]["required"] == ["detail"]
    with_422 = [op for _, op in operations(app) if "422" in op["responses"]]  # type: ignore[operator]
    assert with_422
    for operation in with_422:
        body = operation["responses"]["422"]["content"]["application/json"]["schema"]  # type: ignore[index]
        refs = {option["$ref"].rsplit("/", 1)[1] for option in body["anyOf"]}
        assert refs == {"HTTPValidationError", ERROR_SCHEMA}


def test_documented_errors_do_not_replace_the_ones_a_route_declares(app: FastAPI) -> None:
    health = app.openapi()["paths"][url("health")]["get"]["responses"]
    assert "HealthResponse" in str(health["503"])
