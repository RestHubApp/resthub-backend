"""Lo que no sale del local: los datos personales dentro de un texto libre.

Las notas de los pedidos y los motivos de merma los escribe el personal y se
envían a un proveedor de IA externo, fuera del Perú: es un flujo
transfronterizo de datos (Ley N.º 29733), y una nota puede traer además datos
de salud, como una alergia. La IA solo necesita saber qué dice la nota, no de
quién es, así que antes de enviarla se tapan los correos, los números largos
(teléfonos, DNI, RUC) y el nombre del cliente del pedido.

Python puro.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "[dato personal]"
# Una palabra más corta se confunde con texto común («al», «con»).
MIN_NAME_PART_LENGTH = 3

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Seis dígitos o más, con espacios o guiones entre grupos: «987 654 321»,
# «45678912», «+51 987-654-321». Las cantidades de una nota son más cortas.
_LONG_NUMBER = re.compile(r"(?<!\w)\+?\d(?:[ -]?\d){5,}(?!\w)")


def _name_parts(names: Iterable[str]) -> list[str]:
    """El nombre entero y cada palabra suya, del más largo al más corto."""
    parts: set[str] = set()
    for name in names:
        clean = " ".join(name.split())
        if len(clean) >= MIN_NAME_PART_LENGTH:
            parts.add(clean)
        parts.update(word for word in clean.split() if len(word) >= MIN_NAME_PART_LENGTH)
    return sorted(parts, key=len, reverse=True)


def scrub_personal_data(text: str, names: Iterable[str] = ()) -> str:
    """El texto con los datos que identifican a alguien tapados por `MASK`."""
    text = _EMAIL.sub(MASK, text)
    text = _LONG_NUMBER.sub(MASK, text)
    for part in _name_parts(names):
        text = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", MASK, text, flags=re.IGNORECASE)
    return text
