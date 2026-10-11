"""Reglas de dominio de cuentas y del personal, sin base ni servidor."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.permissions import Permission, RoleKind
from resthub.core.realtime import PERMISSIONS_TOPIC
from resthub.modules.accounts.domain.entities import (
    MIN_PASSWORD_LENGTH,
    User,
    validate_new_password,
)
from resthub.modules.accounts.domain.exceptions import (
    CannotChangeOwnRoleError,
    CannotDeactivateSelfError,
    CannotGrantPermissionsError,
    CannotManageStrongerAccountError,
    EmailAlreadyRegisteredError,
    InvalidEmailError,
    InvalidFullNameError,
    RestaurantAlreadyHasStaffError,
    RoleNotFoundError,
    UserNotFoundError,
    WeakPasswordError,
)
from resthub.modules.accounts.domain.preview import sandbox_email
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.use_cases.manage_roles import ensure_base_roles
from resthub.modules.accounts.use_cases.manage_staff import (
    ChangeStaffStatus,
    ChangeStaffStatusCommand,
    RegisterFirstAdmin,
    RegisterFirstAdminCommand,
    RegisterOwner,
    RegisterOwnerCommand,
    RegisterStaff,
    RegisterStaffCommand,
    ResetStaffPassword,
    ResetStaffPasswordCommand,
    UpdateStaff,
    UpdateStaffCommand,
    resolve_ordering,
)
from tests.conftest import RecordingActivity
from tests.fakes import (
    FakeHasher,
    InMemoryRestaurantDirectory,
    InMemoryRoleRepository,
    InMemoryUserRepository,
    RecordingEvents,
)

PASSWORD = "contrasena-larga"
EVERYTHING = frozenset(permission.value for permission in Permission)
# Gestiona al personal pero no cobra ni ve indicadores.
SUPERVISOR = frozenset({Permission.STAFF_MANAGE.value, Permission.MENU_READ.value})
# Sin el restaurante en el directorio: un local real como cualquiera.
NO_SANDBOX = InMemoryRestaurantDirectory()


@dataclass
class RestaurantSetup:
    users: InMemoryUserRepository
    roles: InMemoryRoleRepository
    owner: Role
    waiter: Role


async def _restaurant_setup() -> RestaurantSetup:
    """El restaurante 1 con sus roles base, y el 2 con los suyos para probar el aislamiento."""
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository(users)
    base = await ensure_base_roles(roles, 1)
    await ensure_base_roles(roles, 2)
    return RestaurantSetup(users=users, roles=roles, owner=base.owner, waiter=base.waiter)


async def _waiter_of(setup: RestaurantSetup, restaurant_id: int) -> Role:
    role = await setup.roles.find_by_kind(restaurant_id, RoleKind.WAITER)
    assert role is not None
    return role


def _user(role: Role, email: str = "ana@example.com") -> User:
    return User(
        restaurant_id=role.restaurant_id,
        email=email,
        full_name="Ana Quispe",
        role=role,
        password_hash="hash:x",
    )


def _register_command(role_id: int, **overrides: Any) -> RegisterStaffCommand:
    command = RegisterStaffCommand(
        restaurant_id=1,
        actor_id=9,
        actor_permissions=EVERYTHING,
        email="luis@example.com",
        full_name="Luis Torres",
        role_id=role_id,
        password=PASSWORD,
    )
    return replace(command, **overrides)


def _register(
    setup: RestaurantSetup,
    activity: RecordingActivity | None = None,
    restaurants: InMemoryRestaurantDirectory = NO_SANDBOX,
) -> RegisterStaff:
    return RegisterStaff(
        setup.users, setup.roles, restaurants, FakeHasher(), activity or RecordingActivity()
    )


def _update(setup: RestaurantSetup) -> UpdateStaff:
    return UpdateStaff(setup.users, setup.roles, RecordingActivity(), RecordingEvents())


def test_email_is_normalized() -> None:
    assert _user(Role.owner(1), email="  Ana@Example.COM ").email == "ana@example.com"


@pytest.mark.parametrize("email", ["", "ana", "ana@", "@example.com", "ana@local"])
def test_invalid_email_is_rejected(email: str) -> None:
    with pytest.raises(InvalidEmailError):
        _user(Role.owner(1), email=email)


def test_name_collapses_spaces_and_cannot_be_empty() -> None:
    user = _user(Role.owner(1))
    user.rename("  Ana   María  Quispe ")
    assert user.full_name == "Ana María Quispe"

    with pytest.raises(InvalidFullNameError):
        user.rename("   ")


def test_new_password_has_a_minimum() -> None:
    assert validate_new_password("x" * MIN_PASSWORD_LENGTH)
    with pytest.raises(WeakPasswordError):
        validate_new_password("x" * (MIN_PASSWORD_LENGTH - 1))


async def test_staff_registration_stays_in_the_actors_restaurant() -> None:
    setup = await _restaurant_setup()
    activity = RecordingActivity()

    created = await _register(setup, activity)(_register_command(setup.waiter.id or 0))

    assert created.restaurant_id == 1
    assert created.role.kind is RoleKind.WAITER
    assert activity.entries == [(1, 9, ActivityKind.STAFF_REGISTERED, "Luis Torres (Mesero)")]


async def test_registration_rejects_a_role_from_another_restaurant() -> None:
    setup = await _restaurant_setup()
    foreign = await _waiter_of(setup, 2)

    with pytest.raises(RoleNotFoundError):
        await _register(setup)(_register_command(foreign.id or 0))


async def test_nobody_registers_staff_with_a_role_stronger_than_their_own() -> None:
    setup = await _restaurant_setup()

    with pytest.raises(CannotGrantPermissionsError):
        await _register(setup)(_register_command(setup.owner.id or 0, actor_permissions=SUPERVISOR))


async def test_email_is_unique_across_restaurants() -> None:
    setup = await _restaurant_setup()
    await setup.users.add(_user(setup.owner, email="luis@example.com"))
    waiter_2 = await _waiter_of(setup, 2)

    with pytest.raises(EmailAlreadyRegisteredError):
        await _register(setup)(_register_command(waiter_2.id or 0, restaurant_id=2))


def _sandbox(restaurant_id: int = 1) -> InMemoryRestaurantDirectory:
    return InMemoryRestaurantDirectory(
        RestaurantSummary(
            id=restaurant_id,
            name="Restaurante de muestra",
            slug="muestra-0a1b2c3d",
            timezone="America/Lima",
            is_active=True,
            is_sandbox=True,
        )
    )


def test_sandbox_email_keeps_the_name_and_carries_the_restaurant() -> None:
    assert sandbox_email("  Ana.Rios@Gmail.com ", 7) == "ana.rios-7@muestra.resthub.invalid"
    # Una parte local larguísima no pasa del tope de la columna.
    assert len(sandbox_email("a" * 240 + "@example.com", 123456)) <= 254


async def test_in_sandbox_restaurant_registration_does_not_take_a_real_email() -> None:
    """Una cuenta creada en la vista previa no ocupa para siempre un correo de verdad."""
    setup = await _restaurant_setup()

    created = await _register(setup, restaurants=_sandbox())(
        _register_command(setup.waiter.id or 0)
    )

    assert created.email == "luis-1@muestra.resthub.invalid"
    assert not await setup.users.exists_with_email("luis@example.com")


async def test_in_sandbox_restaurant_a_repeated_email_still_clashes() -> None:
    setup = await _restaurant_setup()
    register = _register(setup, restaurants=_sandbox())
    await register(_register_command(setup.waiter.id or 0))

    with pytest.raises(EmailAlreadyRegisteredError):
        await register(_register_command(setup.waiter.id or 0, full_name="Otro Luis"))


async def test_new_owner_of_sandbox_restaurant_does_not_take_a_real_email_either() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository(users)

    created = await RegisterOwner(users, roles, _sandbox(), FakeHasher())(
        RegisterOwnerCommand(
            restaurant_id=1, email="rosa@example.com", full_name="Rosa Pérez", password=PASSWORD
        )
    )

    assert created.email == "rosa-1@muestra.resthub.invalid"


async def test_in_a_real_restaurant_the_email_stays_as_given() -> None:
    setup = await _restaurant_setup()
    real = InMemoryRestaurantDirectory(
        RestaurantSummary(id=1, name="Local", slug="local", timezone="America/Lima", is_active=True)
    )

    created = await _register(setup, restaurants=real)(_register_command(setup.waiter.id or 0))

    assert created.email == "luis@example.com"


async def test_nobody_from_another_restaurant_is_edited() -> None:
    setup = await _restaurant_setup()
    foreign = await setup.users.add(_user(await _waiter_of(setup, 2)))

    with pytest.raises(UserNotFoundError):
        await _update(setup)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=9,
                actor_permissions=EVERYTHING,
                user_id=foreign.id or 0,
                role_id=setup.owner.id,
            )
        )


async def test_owner_cannot_demote_or_deactivate_themselves() -> None:
    setup = await _restaurant_setup()
    self_user = await setup.users.add(_user(setup.owner))

    with pytest.raises(CannotChangeOwnRoleError):
        await _update(setup)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=self_user.id or 0,
                actor_permissions=EVERYTHING,
                user_id=self_user.id or 0,
                role_id=setup.waiter.id,
            )
        )
    with pytest.raises(CannotDeactivateSelfError):
        await ChangeStaffStatus(setup.users, RecordingActivity(), RecordingEvents())(
            ChangeStaffStatusCommand(
                restaurant_id=1,
                actor_id=self_user.id or 0,
                actor_permissions=EVERYTHING,
                user_id=self_user.id or 0,
                is_active=False,
            )
        )


async def test_whoever_has_fewer_permissions_cannot_touch_the_owner_account() -> None:
    """Con solo `staff.manage` no se degrada, desactiva ni toma la cuenta del encargado."""
    setup = await _restaurant_setup()
    owner_user = await setup.users.add(_user(setup.owner))
    supervisor = 99

    with pytest.raises(CannotManageStrongerAccountError):
        await _update(setup)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=owner_user.id or 0,
                role_id=setup.waiter.id,
            )
        )
    with pytest.raises(CannotManageStrongerAccountError):
        await ChangeStaffStatus(setup.users, RecordingActivity(), RecordingEvents())(
            ChangeStaffStatusCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=owner_user.id or 0,
                is_active=False,
            )
        )
    with pytest.raises(CannotManageStrongerAccountError):
        await ResetStaffPassword(setup.users, FakeHasher(), RecordingActivity())(
            ResetStaffPasswordCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=owner_user.id or 0,
                new_password="otra-contrasena-larga",
            )
        )


async def test_nobody_promotes_another_above_themselves() -> None:
    setup = await _restaurant_setup()
    kitchen_helper_role = await setup.roles.add(
        Role(restaurant_id=1, name="Ayudante", kind=RoleKind.CUSTOM)
    )
    helper = await setup.users.add(_user(kitchen_helper_role))

    with pytest.raises(CannotGrantPermissionsError):
        await _update(setup)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=99,
                actor_permissions=SUPERVISOR,
                user_id=helper.id or 0,
                role_id=setup.owner.id,
            )
        )


async def test_deactivating_notifies_the_affected_account() -> None:
    setup = await _restaurant_setup()
    waiter_user = await setup.users.add(_user(setup.waiter, email="luis@example.com"))
    events = RecordingEvents()

    await ChangeStaffStatus(setup.users, RecordingActivity(), events)(
        ChangeStaffStatusCommand(
            restaurant_id=1,
            actor_id=99,
            actor_permissions=EVERYTHING,
            user_id=waiter_user.id or 0,
            is_active=False,
        )
    )

    assert [(event.topic, event.user_ids) for event in events.published] == [
        (PERMISSIONS_TOPIC, {waiter_user.id})
    ]


async def test_first_owner_only_enters_an_empty_restaurant() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository(users)
    register = RegisterFirstAdmin(users, roles, NO_SANDBOX, FakeHasher())
    command = RegisterFirstAdminCommand(
        restaurant_id=1, email="rosa@example.com", full_name="Rosa Pérez", password=PASSWORD
    )

    first = await register(command)
    assert first.role.kind is RoleKind.OWNER
    assert {(role.kind, role.name) for role in await roles.list_for_restaurant(1)} == {
        (RoleKind.OWNER, "Encargado"),
        (RoleKind.WAITER, "Mesero"),
    }

    with pytest.raises(RestaurantAlreadyHasStaffError):
        await register(
            RegisterFirstAdminCommand(
                restaurant_id=1, email="otra@example.com", full_name="Otra", password=PASSWORD
            )
        )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, "full_name"), ("-created_at", "-created_at"), ("password_hash", "full_name")],
)
def test_ordering_only_by_allowed_columns(requested: str | None, expected: str) -> None:
    assert resolve_ordering(requested) == expected
