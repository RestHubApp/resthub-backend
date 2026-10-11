"""Verifica que los identificadores del repositorio estén en inglés.

Decisión de diseño: variables, funciones, clases, argumentos, atributos,
constantes, nombres de test y nombres de archivo y carpeta van en inglés. Los
textos para el usuario, los mensajes de error, los comentarios, las docstrings
y los datos de prueba dentro de strings siguen en español, y este script no los
mira: recorre el AST y solo toma nombres.

Una palabra es española si el diccionario español de pyspellchecker la conoce
y el inglés no (o la frecuencia española es mucho mayor), o si lleva tilde o
eñe. Las excepciones (siglas legales, nombres de platos, falsos positivos) van
en `scripts/english_allowlist.txt`.

Uso:
    uv run python scripts/check_english_identifiers.py          # sale 1 si hay
    uv run python scripts/check_english_identifiers.py --words  # detalle
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

from spellchecker import SpellChecker

ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("src", "tests", "scripts", "chaos")
FILE_SUFFIXES = {".py", ".js", ".sh", ".json"}
ALLOWLIST = Path(__file__).resolve().with_name("english_allowlist.txt")
MIN_LENGTH = 3
FREQUENCY_RATIO = 20
SKIPPED_PARTS = {"__pycache__", "node_modules", ".venv", "htmlcov"}


def load_allowlist(path: Path = ALLOWLIST) -> set[str]:
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        word = line.split("#", 1)[0].strip().lower()
        if word:
            words.add(word)
    return words


def split_words(identifier: str) -> list[str]:
    """Parte snake_case, camelCase y PascalCase en palabras en minúscula."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", identifier)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.split(r"[^A-Za-zÀ-ÿ]+", spaced) if w]


class SpanishDetector:
    def __init__(self, allowed: set[str]) -> None:
        self._allowed = allowed
        self._spanish = SpellChecker(language="es")
        self._english = SpellChecker(language="en")
        self._cache: dict[str, bool] = {}

    def is_spanish(self, word: str) -> bool:
        if len(word) < MIN_LENGTH or word in self._allowed:
            return False
        if word not in self._cache:
            self._cache[word] = self._classify(word)
        return self._cache[word]

    def _classify(self, word: str) -> bool:
        if re.search("[áéíóúñü]", word):
            return True
        if self._is_spanish_form(word):
            return True
        # Plural regular: «fondos», «comprobantes», «ciudades».
        singulars = [word[:-1]] if word.endswith("s") else []
        if word.endswith("es"):
            singulars.append(word[:-2])
        return self._english.word_usage_frequency(word) == 0 and any(
            s not in self._allowed and self._is_spanish_form(s) for s in singulars
        )

    def _is_spanish_form(self, word: str) -> bool:
        if len(word) < MIN_LENGTH:
            return False
        spanish = self._spanish.word_usage_frequency(word)
        english = self._english.word_usage_frequency(word)
        return spanish > 0 and (english == 0 or spanish > FREQUENCY_RATIO * english)


def _node_names(node: ast.AST) -> list[str]:
    """Nombres que el código define o pasa por nombre en este nodo del AST."""
    names: list[str | None] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        names = [node.name]
    elif isinstance(node, ast.arg | ast.keyword):
        names = [node.arg]
    elif isinstance(node, ast.Name):
        names = [node.id] if isinstance(node.ctx, ast.Store | ast.Del) else []
    elif isinstance(node, ast.Attribute):
        names = [node.attr] if isinstance(node.ctx, ast.Store) else []
    elif isinstance(node, ast.alias):
        names = [node.asname]
    elif isinstance(node, ast.Global | ast.Nonlocal):
        names = list(node.names)
    elif isinstance(
        node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar | ast.TypeVar | ast.ParamSpec
    ):
        names = [node.name]
    return [name for name in names if name]


def _target_names(tree: ast.AST) -> list[str]:
    return [name for node in ast.walk(tree) for name in _node_names(node)]


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        base = root / directory
        if base.is_dir():
            files.extend(
                p
                for p in sorted(base.rglob("*"))
                if p.is_file()
                and p.suffix in FILE_SUFFIXES
                and not SKIPPED_PARTS.intersection(p.parts)
            )
    return files


def collect_identifiers(root: Path = ROOT) -> dict[str, set[str]]:
    """Mapa identificador -> lugares donde aparece (archivo o archivo:nombre)."""
    found: dict[str, set[str]] = defaultdict(set)
    for path in _iter_files(root):
        relative = path.relative_to(root)
        place = str(relative)
        found[path.stem].add(f"{place} (file name)")
        for segment in relative.parent.parts:
            found[segment].add(f"{place} (folder name)")
        if path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=place)
            for name in _target_names(tree):
                found[name].add(place)
    return found


def find_spanish(
    root: Path = ROOT, allowed: set[str] | None = None
) -> dict[str, dict[str, set[str]]]:
    """Mapa palabra española -> identificador -> lugares."""
    detector = SpanishDetector(load_allowlist() if allowed is None else allowed)
    result: dict[str, dict[str, set[str]]] = defaultdict(dict)
    for identifier, places in collect_identifiers(root).items():
        for word in set(split_words(identifier)):
            if detector.is_spanish(word):
                result[word][identifier] = places
    return result


def main(argv: list[str]) -> int:
    spanish = find_spanish()
    if not spanish:
        print("Todos los identificadores están en inglés.")
        return 0
    detail = "--words" in argv
    for word in sorted(spanish):
        identifiers = spanish[word]
        sample = ", ".join(sorted(identifiers)[:3])
        print(f"{word}: {len(identifiers)} identificadores, p. ej. {sample}")
        if detail:
            for identifier in sorted(identifiers):
                for place in sorted(identifiers[identifier]):
                    print(f"    {identifier}  {place}")
    print(
        f"\n{len(spanish)} palabras españolas en identificadores. Traduce el "
        "identificador o, si es una sigla o un falso positivo, añade la palabra a "
        "scripts/english_allowlist.txt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
