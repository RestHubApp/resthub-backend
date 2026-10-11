"""Reglas de arquitectura que import-linter y ruff no expresan.

Se leen los archivos con `ast`, sin importar la aplicación: cada comprobación
tarda milisegundos y no depende de que el código arranque.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "resthub"
MODULES = SRC / "modules"

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
    assert not offenders, (
        f"Modelos de la API que deben terminar en Request o Response: {sorted(offenders)}"
    )


PORT_SUFFIXES = (
    "Repository",
    "Directory",
    "Gateway",
    "Hook",
    "Clock",
    "Hasher",
    "Reader",
    "Recorder",
    "Policy",
)


def _check(offenders: set[str], message: str) -> None:
    assert not offenders, f"{message}: {sorted(offenders)}"


def test_ports_end_in_an_allowed_suffix() -> None:
    offenders = {
        f"{relative}:{node.name}"
        for relative, node in _classes("*/ports/*.py")
        if "Protocol" in _base_names(node) and not node.name.endswith(PORT_SUFFIXES)
    }
    _check(offenders, f"Protocol de ports/ sin sufijo en {PORT_SUFFIXES}")


def _is_orm_row(node: ast.ClassDef) -> bool:
    return any(name.endswith("Base") for name in _base_names(node))


def test_orm_rows_end_in_row() -> None:
    offenders = {
        f"{relative}:{node.name}"
        for relative, node in _classes("*/adapters/persistence/**/*.py")
        if _is_orm_row(node) and not node.name.endswith("Row")
    }
    _check(offenders, "Clases del ORM (hijas de Base) que deben terminar en Row")


def test_persistence_implementations_use_sqlalchemy_prefix() -> None:
    offenders = {
        f"{relative}:{node.name}"
        for relative, node in _classes("*/adapters/persistence/**/*.py")
        if not _is_orm_row(node) and not node.name.startswith(("SqlAlchemy", "_"))
    }
    _check(
        offenders,
        "Clases de adapters/persistence que deben llamarse SqlAlchemy* (o ser privadas)",
    )


def test_exceptions_end_in_error() -> None:
    offenders = {
        f"{path.relative_to(SRC).as_posix()}:{node.name}"
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        and not node.name.endswith("Error")
        and (
            path.name == "exceptions.py"
            or any(b.endswith(("Exception", "Error")) for b in _base_names(node))
        )
    }
    _check(offenders, "Excepciones que deben terminar en Error")


def test_use_case_inputs_are_commands_or_queries() -> None:
    offenders = {
        f"{relative}:{node.name}"
        for relative, node in _classes("*/use_cases/*.py")
        if node.name.endswith(("Input", "Request", "Dto", "Params"))
    }
    _check(offenders, "Entradas de casos de uso: usa Command o Query")


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def _string_constants(path: Path) -> Iterator[ast.Constant]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def test_tests_do_not_hardcode_the_api_prefix() -> None:
    """Las rutas se arman con `url()`/`API_PREFIX` de `tests/conftest.py`."""
    # Una URL absoluta de otro servicio (`https://host/api/v1/...`) no es nuestro prefijo.
    own_prefix = re.compile(r"(?<![\w./])/api/v1")
    offenders = {
        f"{path.relative_to(TESTS).as_posix()}:{node.lineno}"
        for path in sorted(TESTS.rglob("*.py"))
        if path.name != "conftest.py" and path != Path(__file__).resolve()
        for node in _string_constants(path)
        if own_prefix.search(node.value)
    }
    assert not offenders, f"Usa url(...) o API_PREFIX en vez de '/api/v1': {sorted(offenders)}"


def _enum_member_values(tree: ast.AST) -> set[int]:
    """Los `id()` de las constantes que definen miembros de un `StrEnum`."""
    return {
        id(item.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _base_names(node) & {"StrEnum", "Enum"}
        for item in node.body
        if isinstance(item, ast.Assign)
    }


def _enum_values(path: Path, enum_name: str) -> set[str]:
    values = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef) and node.name == enum_name:
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
                    values.add(str(item.value.value))
    return values


def test_permission_and_activity_values_are_not_repeated_as_literals() -> None:
    """Un permiso o un tipo de actividad se nombra por su enum (`Permission.X.value` en tests)."""
    core = SRC / "core"
    sources = {
        core / "permissions.py": _enum_values(core / "permissions.py", "Permission"),
        core / "activity.py": _enum_values(core / "activity.py", "ActivityKind"),
    }
    assert all(sources.values()), "No se pudieron leer los valores de los enums"
    forbidden = set().union(*sources.values())
    # Un `StrEnum` propio (p. ej. `PlatformActivityKind`) puede coincidir en valores: es otra
    # bitácora, y su definición es la única fuente de sus miembros.
    offenders = {
        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
        for root in (SRC, TESTS)
        for path in sorted(root.rglob("*.py"))
        if path not in sources
        for tree in [ast.parse(path.read_text(encoding="utf-8"))]
        for members in [_enum_member_values(tree)]
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in forbidden
        and id(node) not in members
    }
    assert not offenders, (
        f"Literales que son valores de Permission o ActivityKind: {sorted(offenders)}"
    )
