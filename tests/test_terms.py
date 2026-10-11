"""Términos de uso y política de privacidad sin HTTP (Ley N.º 29733)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from resthub.core.activity import ActivityRecorder
from resthub.modules.accounts.domain.entities import TERMS_VERSION, User
from resthub.modules.accounts.domain.exceptions import (
    OutdatedTermsError,
    PreviewSessionRestrictedError,
)
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.ports.user_repository import UserRepository
from resthub.modules.accounts.use_cases.accept_terms import AcceptTerms, AcceptTermsCommand
from tests.fakes import FixedClock

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _account() -> User:
    return User(
        restaurant_id=1,
        email="mesero@local.pe",
        full_name="Luis Paz",
        role=cast("Role", object()),
        password_hash="x",
        id=7,
    )


def test_new_account_lacks_current_terms() -> None:
    assert _account().has_current_terms is False


def test_accepting_current_version_records_when() -> None:
    account = _account()
    account.accept_terms(TERMS_VERSION, NOW)

    assert account.has_current_terms is True
    assert account.terms_accepted_at == NOW


def test_outdated_version_is_not_valid() -> None:
    account = _account()
    with pytest.raises(OutdatedTermsError):
        account.accept_terms("2020-01", NOW)
    assert account.terms_version == ""


async def test_preview_does_not_accept_on_behalf_of_account_owner() -> None:
    # Ni siquiera se lee la cuenta: quien mira no es quien acepta.
    unused: Any = None
    use_case = AcceptTerms(
        cast("UserRepository", unused), cast("ActivityRecorder", unused), clock=FixedClock()
    )

    with pytest.raises(PreviewSessionRestrictedError):
        await use_case(AcceptTermsCommand(user_id=7, version=TERMS_VERSION, preview=True))
