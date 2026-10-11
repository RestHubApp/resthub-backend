"""El catálogo de códigos de error está completo y sin sobras.

Se leen los archivos con `ast`, como `test_architecture.py`: toda excepción de dominio declara
su `code`, un mismo código no sirve a dos errores distintos y todo valor de `ErrorCode` se usa.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from resthub.core.error_codes import ErrorCode

SRC = Path(__file__).resolve().parents[1] / "src" / "resthub"
CATALOG = SRC / "core" / "error_codes.py"
# Los errores de identidad viven en el núcleo y no en un `domain/`, pero también salen por la API.
ERROR_FILES = [*sorted(SRC.glob("modules/*/domain/**/*.py")), SRC / "core" / "identity.py"]


def _declared_code(node: ast.ClassDef) -> str | None:
    """El nombre del miembro de `ErrorCode` que la clase asigna a `code`, si lo asigna."""
    for statement in node.body:
        if (
            isinstance(statement, ast.Assign)
            and [ast.unparse(target) for target in statement.targets] == ["code"]
            and isinstance(statement.value, ast.Attribute)
            and ast.unparse(statement.value.value) == "ErrorCode"
        ):
            return statement.value.attr
    return None


def _is_root(node: ast.ClassDef) -> bool:
    return [ast.unparse(base) for base in node.bases] == ["Exception"]


def _errors() -> list[tuple[str, ast.ClassDef]]:
    return [
        (path.relative_to(SRC).as_posix(), node)
        for path in ERROR_FILES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and node.name.endswith("Error")
    ]


def test_every_domain_error_declares_a_code() -> None:
    errors = _errors()
    missing = [
        f"{path}:{node.name}"
        for path, node in errors
        if _declared_code(node) is None and not _is_root(node)
    ]
    roots_without_annotation = [
        f"{path}:{node.name}"
        for path, node in errors
        if _is_root(node)
        and not any(
            isinstance(statement, ast.AnnAssign) and ast.unparse(statement.target) == "code"
            for statement in node.body
        )
    ]

    assert errors
    assert not missing, f"Errores sin `code = ErrorCode.X`: {missing}"
    assert not roots_without_annotation, (
        f"Raíces sin `code: ClassVar[ErrorCode]`: {roots_without_annotation}"
    )


def test_a_code_is_shared_only_by_errors_with_the_same_name() -> None:
    names_by_code: dict[str, set[str]] = defaultdict(set)
    for _, node in _errors():
        if (code := _declared_code(node)) is not None:
            names_by_code[code].add(node.name)

    shared = {code: sorted(names) for code, names in names_by_code.items() if len(names) > 1}

    assert not shared, f"Un código para dos errores distintos: {shared}"


def test_a_domain_error_code_is_named_after_its_class() -> None:
    wrong = []
    for path, node in _errors():
        code = _declared_code(node)
        expected = "".join(
            f"_{char}" if char.isupper() and index else char
            for index, char in enumerate(node.name.removesuffix("Error"))
        ).upper()
        if code is not None and code != expected:
            wrong.append(f"{path}:{node.name} usa {code}")

    assert not wrong


def test_every_error_code_is_used_outside_the_catalog() -> None:
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path == CATALOG:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "ErrorCode":
                used.add(node.attr)

    unused = sorted(member.name for member in ErrorCode if member.name not in used)

    assert not unused, f"Códigos sin uso: {unused}"
