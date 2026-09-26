"""Reglas del cliente sin HTTP."""

from __future__ import annotations

from decimal import Decimal

from resthub.modules.customers.domain.customers import CustomerStats


def test_el_ticket_promedio_redondea_el_medio_centavo_hacia_arriba() -> None:
    assert CustomerStats(visits=2, spent=Decimal("20.01")).average_ticket == Decimal("10.01")
