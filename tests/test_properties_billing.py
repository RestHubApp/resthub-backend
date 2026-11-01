"""Propiedades del dinero en los comprobantes que se mandan a SUNAT.

Las líneas separan su IGV una por una y la cabecera lo separa del total: Hypothesis
busca los redondeos de céntimos en que no cuadran o en que aparece un descuento que
el cliente nunca tuvo.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from resthub.modules.billing.adapters.sunat.lycet import build_payload
from resthub.modules.billing.domain.invoices import (
    CENT,
    ZERO,
    BillingSettings,
    Customer,
    Invoice,
    InvoiceKind,
    build_lines,
)
from tests.fakes import FIXED_NOW

SETTINGS = BillingSettings(restaurant_id=1, ruc="20123456789", legal_name="Local S.A.C.")

lines = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=5),
        st.integers(min_value=10, max_value=20_000).map(lambda cents: Decimal(cents) * CENT),
        st.booleans(),
    ),
    min_size=1,
    max_size=12,
).filter(lambda items: any(not courtesy for _, _, courtesy in items))
rates = st.sampled_from([Decimal("18.00"), Decimal("10.50")])


def _sum(details: list[dict[str, object]], key: str) -> Decimal:
    return sum((Decimal(str(d[key])) for d in details), ZERO)


@given(items=lines, rate=rates)
def test_undiscounted_invoice_lines_add_up_to_the_header(
    items: list[tuple[int, Decimal, bool]], rate: Decimal
) -> None:
    total = sum((price * quantity for quantity, price, courtesy in items if not courtesy), ZERO)
    invoice = Invoice(
        restaurant_id=1,
        order_id=1,
        kind=InvoiceKind.SALES_RECEIPT,
        series="B001",
        number=1,
        customer=Customer(),
        lines=build_lines(
            [(f"Plato {n}", qty, price, courtesy) for n, (qty, price, courtesy) in enumerate(items)]
        ),
        total=total,
        discount=ZERO,
        igv_rate=rate,
        issued_by=1,
        issued_at=FIXED_NOW,
    )

    payload = build_payload(SETTINGS, invoice)

    paid = [d for d in payload["details"] if d["tipAfeIgv"] == "10"]
    assert "descuentos" not in payload
    assert _sum(paid, "mtoValorVenta") == Decimal(str(payload["mtoOperGravadas"]))
    assert _sum(paid, "igv") == Decimal(str(payload["mtoIGV"]))
