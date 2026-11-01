"""Reglas puras del nombre de un rol."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from resthub.modules.accounts.adapters.persistence.models import RoleRow
from resthub.modules.accounts.domain.exceptions import InvalidRoleNameError
from resthub.modules.accounts.domain.roles import role_name_key, validate_role_name

NAME_KEY_LENGTH = RoleRow.__table__.c.name_key.type.length


@given(st.text(max_size=40))
def test_validate_role_name_accepts_only_names_whose_key_fits(raw: str) -> None:
    try:
        name = validate_role_name(raw)
    except InvalidRoleNameError:
        return

    assert len(name) <= NAME_KEY_LENGTH
    assert len(role_name_key(name)) <= NAME_KEY_LENGTH


def test_validate_role_name_rejects_a_name_whose_casefold_expands() -> None:
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("ß" * 40)


def test_validate_role_name_keeps_a_short_name_with_expanding_letters() -> None:
    assert validate_role_name("Jefe de barra Straße") == "Jefe de barra Straße"
