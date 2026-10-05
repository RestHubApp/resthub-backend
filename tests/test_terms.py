"""Términos de uso y política de privacidad sin HTTP (Ley N.º 29733)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from resthub.core.activity import ActivityRecorder
from resthub.modules.accounts.domain.entities import TERMS_VERSION, User
from resthub.modules.accounts.domain.exceptions import OutdatedTerms, PreviewSessionRestricted
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.ports.user_repository import UserRepository
from resthub.modules.accounts.use_cases.accept_terms import AcceptTerms, AcceptTermsCommand

AHORA = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _cuenta() -> User:
    return User(
        restaurant_id=1,
        email="mesero@local.pe",
        full_name="Luis Paz",
        role=cast(Role, object()),
        password_hash="x",
        id=7,
    )


def test_una_cuenta_nueva_no_tiene_los_terminos_vigentes() -> None:
    assert _cuenta().has_current_terms is False


def test_aceptar_la_version_vigente_anota_cuando() -> None:
    cuenta = _cuenta()
    cuenta.accept_terms(TERMS_VERSION, AHORA)

    assert cuenta.has_current_terms is True
    assert cuenta.terms_accepted_at == AHORA


def test_una_version_que_ya_no_es_la_vigente_no_vale() -> None:
    cuenta = _cuenta()
    with pytest.raises(OutdatedTerms):
        cuenta.accept_terms("2020-01", AHORA)
    assert cuenta.terms_version == ""


async def test_la_vista_previa_no_acepta_por_el_dueno_de_la_cuenta() -> None:
    # Ni siquiera se lee la cuenta: quien mira no es quien acepta.
    sin_uso: Any = None
    caso = AcceptTerms(cast(UserRepository, sin_uso), cast(ActivityRecorder, sin_uso))

    with pytest.raises(PreviewSessionRestricted):
        await caso(AcceptTermsCommand(user_id=7, version=TERMS_VERSION, preview=True))
