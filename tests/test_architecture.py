"""Reglas de arquitectura que import-linter y ruff no expresan.

Se leen los archivos con `ast`, sin importar la aplicación: cada comprobación
tarda milisegundos y no depende de que el código arranque.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "resthub"
MODULES = SRC / "modules"

# Los modelos de `adapters/api` que hoy no terminan en Request/Response. Renombrarlos
# cambia los nombres de componente del OpenAPI publicado, así que se dejan para la
# fase de renombrado (TODO fase 2). La lista solo puede encogerse: un modelo nuevo
# con otro nombre falla.
KNOWN_API_MODELS_WITHOUT_SUFFIX = {
    "insights/adapters/api/schemas.py:SalesTotals",
    "insights/adapters/api/schemas.py:DailySalesPoint",
    "insights/adapters/api/schemas.py:CauseCount",
    "orders/adapters/api/schemas.py:ActiveOrderSummary",
    "menu/adapters/api/schemas.py:ModifierOptionSchema",
    "menu/adapters/api/schemas.py:ModifierGroupSchema",
}

# Bases que hacen de una clase de `ports/` un tipo de datos legítimo del puerto.
_DATA_BASES = {"Enum", "StrEnum", "IntEnum", "Exception", "NamedTuple", "TypedDict"}


def _classes(pattern: str) -> Iterator[tuple[str, ast.ClassDef]]:
    """Las clases de los archivos que cumplen `pattern` (relativo a `modules/`)."""
    for path in sorted(MODULES.glob(pattern)):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(MODULES).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield relative, node


def _base_names(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        names.add(ast.unparse(target).rsplit(".", 1)[-1])
    return names


def _is_frozen_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if ast.unparse(decorator.func).rsplit(".", 1)[-1] != "dataclass":
            continue
        if any(
            keyword.arg == "frozen"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        ):
            return True
    return False


def test_init_files_are_empty_or_docstring_only() -> None:
    """Sin reexportaciones: cada símbolo se importa desde el archivo que lo define."""
    offenders = []
    for path in sorted(SRC.rglob("__init__.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        only_docstring = all(
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for node in body
        )
        if not only_docstring or len(body) > 1:
            offenders.append(path.relative_to(SRC).as_posix())
    assert not offenders, f"__init__.py con contenido (deben estar vacíos): {offenders}"


def test_port_classes_are_protocols_or_plain_data() -> None:
    """Un puerto es un `Protocol`; junto a él solo viven sus tipos de datos."""
    offenders = []
    for relative, node in _classes("*/ports/*.py"):
        bases = _base_names(node)
        if "Protocol" in bases or bases & _DATA_BASES or _is_frozen_dataclass(node):
            continue
        offenders.append(f"{relative}:{node.name}")
    assert not offenders, (
        "Clases de ports/ que no son Protocol, dataclass(frozen=True), Enum ni excepción: "
        f"{offenders}"
    )


def test_api_models_end_in_request_or_response() -> None:
    offenders = {
        f"{relative}:{node.name}"
        for relative, node in _classes("*/adapters/api/**/*.py")
        if "BaseModel" in _base_names(node) and not node.name.endswith(("Request", "Response"))
    }
    new = sorted(offenders - KNOWN_API_MODELS_WITHOUT_SUFFIX)
    assert not new, f"Modelos de la API que deben terminar en Request o Response: {new}"
    fixed = sorted(KNOWN_API_MODELS_WITHOUT_SUFFIX - offenders)
    assert not fixed, f"Ya cumplen la regla: quítalos de KNOWN_API_MODELS_WITHOUT_SUFFIX: {fixed}"
