"""Lo que se envía a la IA externa va sin datos personales (Ley N.º 29733)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.insights.domain.decisions import KitchenNote, SubjectType, note_state
from resthub.modules.insights.domain.privacy import MASK, scrub_personal_data
from resthub.modules.insights.domain.stock import WasteFact, waste_state


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("llamar al 987 654 321", f"llamar al {MASK}"),
        ("cel +51 987-654-321 antes", f"cel {MASK} antes"),
        ("factura al DNI 45678912", f"factura al DNI {MASK}"),
        ("RUC 20123456789", f"RUC {MASK}"),
        ("avisar a ana.torres@correo.pe", f"avisar a {MASK}"),
        # Las cantidades de una nota no son datos personales.
        ("2 porciones, sin ají, mesa 12", "2 porciones, sin ají, mesa 12"),
    ],
)
def test_masks_what_identifies_someone(text: str, expected: str) -> None:
    assert scrub_personal_data(text) == expected


def test_masks_customer_name_whole_or_in_parts_ignoring_case() -> None:
    text = "Ana Torres es alérgica al maní; a ANA le gusta poco picante, dice torres"

    clean = scrub_personal_data(text, ["Ana Torres"])

    assert clean == (f"{MASK} es alérgica al maní; a {MASK} le gusta poco picante, dice {MASK}")


def test_does_not_mask_words_that_merely_contain_the_name() -> None:
    # «Ana» está dentro de «banana», pero no es el nombre.
    assert scrub_personal_data("batido de banana", ["Ana"]) == "batido de banana"


def test_note_seen_by_ai_has_no_name_or_phone() -> None:
    note = KitchenNote(
        subject_type=SubjectType.ORDER,
        subject_id=1,
        order_id=1,
        text="Rosa es celíaca, llamar al 912345678",
        customer_name="Rosa Quispe",
    )

    assert note_state(note)["note"] == f"{MASK} es celíaca, llamar al {MASK}"


def test_waste_reason_seen_by_ai_has_no_phones() -> None:
    waste = WasteFact(
        movement_id=1,
        ingredient_id=1,
        ingredient_name="Pescado",
        unit="g",
        quantity=Decimal(500),
        cost=Decimal("12.50"),
        reason="lo devolvió el cliente del 987654321",
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    assert waste_state(waste)["reason"] == f"lo devolvió el cliente del {MASK}"
