"""Los identificadores del repositorio van en inglés; el script lo vigila en el CI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_english_identifiers as checker


def test_repository_identifiers_are_in_english() -> None:
    spanish = checker.find_spanish()

    assert not spanish, f"Identificadores en español: {sorted(spanish)[:30]}"


def test_script_exits_with_error_when_it_finds_spanish_words(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pedidos.py").write_text("def cobrar_pedido(mesero):\n    return mesero\n")

    spanish = checker.find_spanish(tmp_path)

    assert {"pedidos", "cobrar", "pedido", "mesero"} <= set(spanish)


def test_script_ignores_strings_comments_and_allowed_words(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "billing.py").write_text(
        '# El cobro del pedido\nruc = "Lomo saltado"\n\n\ndef issue(igv: int) -> int:\n'
        '    """Cobra el pedido."""\n    return igv\n'
    )

    assert checker.find_spanish(tmp_path) == {}


@pytest.mark.parametrize(
    ("identifier", "words"),
    [
        ("test_cobrar_el_pedido", ["test", "cobrar", "el", "pedido"]),
        ("MenuItemResponse", ["menu", "item", "response"]),
        ("SALES_RECEIPT", ["sales", "receipt"]),
        ("parseHTTPResponse", ["parse", "http", "response"]),
    ],
)
def test_split_words_handles_snake_and_camel_case(identifier: str, words: list[str]) -> None:
    assert checker.split_words(identifier) == words


# Las migraciones ya publicadas conservan su nombre en español: el archivo no
# cambia el `revision`, pero renombrarlo rompe el historial. Desde la siguiente,
# el nombre va en inglés y con el formato `NNNN_snake_case.py`.
LAST_SPANISH_MIGRATION = 22
MIGRATIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def test_new_migrations_have_english_snake_case_names() -> None:
    detector = checker.SpanishDetector(checker.load_allowlist())
    bad = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        match = re.fullmatch(r"(\d{4})_([a-z0-9]+(?:_[a-z0-9]+)*)", path.stem)
        if match is None:
            bad.append(f"{path.name}: formato distinto de NNNN_snake_case")
        elif int(match[1]) > LAST_SPANISH_MIGRATION and any(
            detector.is_spanish(word) for word in checker.split_words(match[2])
        ):
            bad.append(f"{path.name}: nombre en español")

    assert not bad, "\n".join(bad)
