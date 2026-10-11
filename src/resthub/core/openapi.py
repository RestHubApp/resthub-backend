"""Los errores del API en su esquema OpenAPI.

Todo error de la API tiene la misma forma, `{"detail": "texto"}`: lo producen las
excepciones de dominio traducidas por cada router, la autenticación y los límites. Solo el
error de validación de FastAPI (422) trae un arreglo en `detail`. FastAPI documenta únicamente
ese 422, así que el esquema declaraba un contrato que el servidor incumplía: respuestas 401,
403, 404 y 409 que no figuraban y 422 con `detail` de texto que no cabían en el esquema.

Aquí se completa el esquema a partir de lo que el servidor devuelve de verdad. No cambia
ninguna respuesta: solo hace que `openapi.json`, y con él los tipos que genera el frontend y
las pruebas de Schemathesis, digan la verdad.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, status

ERROR_SCHEMA = "ErrorResponse"
# Estados de error que cualquier operación puede devolver, con su significado.
ERROR_STATUSES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "La petición no se puede atender tal como llegó.",
    status.HTTP_401_UNAUTHORIZED: "Falta la credencial de acceso o ya no vale.",
    status.HTTP_403_FORBIDDEN: "La cuenta no tiene el permiso que la operación exige.",
    status.HTTP_404_NOT_FOUND: "El recurso no existe (o es de otro restaurante).",
    status.HTTP_409_CONFLICT: "El estado actual no permite la operación.",
    status.HTTP_429_TOO_MANY_REQUESTS: "Demasiadas peticiones desde esta dirección.",
}
_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
_VALIDATION = str(status.HTTP_422_UNPROCESSABLE_CONTENT)


def _error_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "title": ERROR_SCHEMA,
        "properties": {"detail": {"type": "string", "title": "Detail"}},
        "required": ["detail"],
    }


def _json_content(schema: dict[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": schema}}


def _complete(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components[ERROR_SCHEMA] = _error_schema()
    reference = {"$ref": f"#/components/schemas/{ERROR_SCHEMA}"}
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in _METHODS:
                continue
            responses = operation.setdefault("responses", {})
            for code, description in ERROR_STATUSES.items():
                responses.setdefault(
                    str(code), {"description": description, "content": _json_content(reference)}
                )
            # El 422 es de dos formas: la lista de FastAPI o el texto de una regla de negocio.
            validation = responses.get(_VALIDATION)
            if validation is not None:
                content = validation.setdefault("content", {}).setdefault("application/json", {})
                original = content.get("schema")
                if original is not None and "anyOf" not in original:
                    content["schema"] = {"anyOf": [original, reference]}


def document_error_responses(app: FastAPI) -> None:
    """Hace que `app.openapi()` incluya los errores reales del API."""
    build = app.openapi
    done = False

    def openapi() -> dict[str, Any]:
        nonlocal done
        schema = build()
        if not done:
            _complete(schema)
            done = True
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI documenta este reemplazo
