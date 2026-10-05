"""Lo que se envía a la IA externa va sin datos personales (Ley N.º 29733)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.insights.domain.decisions import KitchenNote, SubjectType, note_state
from resthub.modules.insights.domain.privacy import MASK, scrub_personal_data
from resthub.modules.insights.domain.stock import WasteFact, waste_state


@pytest.mark.parametrize(
    ("texto", "esperado"),
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
def test_tapa_lo_que_identifica_a_alguien(texto: str, esperado: str) -> None:
    assert scrub_personal_data(texto) == esperado


def test_tapa_el_nombre_del_cliente_entero_o_por_partes_sin_importar_mayusculas() -> None:
    texto = "Ana Torres es alérgica al maní; a ANA le gusta poco picante, dice torres"

    limpio = scrub_personal_data(texto, ["Ana Torres"])

    assert limpio == (f"{MASK} es alérgica al maní; a {MASK} le gusta poco picante, dice {MASK}")


def test_no_tapa_palabras_que_solo_contienen_el_nombre() -> None:
    # «Ana» está dentro de «banana», pero no es el nombre.
    assert scrub_personal_data("batido de banana", ["Ana"]) == "batido de banana"


def test_la_nota_que_ve_la_ia_no_lleva_el_nombre_ni_el_telefono() -> None:
    nota = KitchenNote(
        subject_type=SubjectType.ORDER,
        subject_id=1,
        order_id=1,
        text="Rosa es celíaca, llamar al 912345678",
        customer_name="Rosa Quispe",
    )

    assert note_state(nota)["note"] == f"{MASK} es celíaca, llamar al {MASK}"


def test_el_motivo_de_merma_que_ve_la_ia_no_lleva_telefonos() -> None:
    merma = WasteFact(
        movement_id=1,
        ingredient_id=1,
        ingredient_name="Pescado",
        unit="g",
        quantity=Decimal("500"),
        cost=Decimal("12.50"),
        reason="lo devolvió el cliente del 987654321",
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    assert waste_state(merma)["reason"] == f"lo devolvió el cliente del {MASK}"
