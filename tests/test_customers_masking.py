"""Enmascarado de los datos de contacto de un cliente."""

from __future__ import annotations

import pytest

from resthub.modules.customers.domain.masking import mask_email, mask_phone


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("987654222", "987•••222"),
        ("987 654 222", "987•••222"),
        ("+51987654222", "+51•••222"),
        ("12345", "1•••"),
        ("", ""),
    ],
)
def test_mask_phone_keeps_only_the_edges(phone: str, expected: str) -> None:
    assert mask_phone(phone) == expected


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("ana@correo.pe", "a•••@correo.pe"),
        ("sin-arroba", "•••"),
        ("", ""),
    ],
)
def test_mask_email_keeps_first_letter_and_domain(email: str, expected: str) -> None:
    assert mask_email(email) == expected
