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
    CannotChangeOwnRole,
    CannotDeactivateSelf,
    CannotGrantPermissions,
    CannotManageStrongerAccount,
    EmailAlreadyRegistered,
    InvalidEmail,
    InvalidFullName,
    RestaurantAlreadyHasStaff,
    RoleNotFound,
    UserNotFound,
    WeakPassword,
)
from resthub.modules.accounts.domain.roles import Role
from resthub.modules.accounts.use_cases.manage_roles import ensure_base_roles
from resthub.modules.accounts.use_cases.manage_staff import (
    ChangeStaffStatus,
    ChangeStaffStatusCommand,
    RegisterFirstAdmin,
    RegisterFirstAdminCommand,
    RegisterStaff,
    RegisterStaffCommand,
    ResetStaffPassword,
    ResetStaffPasswordCommand,
    UpdateStaff,
    UpdateStaffCommand,
    resolve_ordering,
)
from tests.conftest import RecordingActivity
from tests.fakes import FakeHasher, InMemoryRoleRepository, InMemoryUserRepository, RecordingEvents

PASSWORD = "contrasena-larga"
EVERYTHING = frozenset(permission.value for permission in Permission)
# Gestiona al personal pero no cobra ni ve indicadores.
SUPERVISOR = frozenset({Permission.STAFF_MANAGE.value, Permission.MENU_READ.value})


@dataclass
class Local:
    users: InMemoryUserRepository
    roles: InMemoryRoleRepository
    owner: Role
    waiter: Role


async def _local() -> Local:
    """El restaurante 1 con sus roles base, y el 2 con los suyos para probar el aislamiento."""
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository(users)
    base = await ensure_base_roles(roles, 1)
    await ensure_base_roles(roles, 2)
    return Local(users=users, roles=roles, owner=base.owner, waiter=base.waiter)


async def _waiter_of(local: Local, restaurant_id: int) -> Role:
    role = await local.roles.find_by_kind(restaurant_id, RoleKind.WAITER)
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


def _update(local: Local) -> UpdateStaff:
    return UpdateStaff(local.users, local.roles, RecordingActivity(), RecordingEvents())


def test_el_correo_se_normaliza() -> None:
    assert _user(Role.owner(1), email="  Ana@Example.COM ").email == "ana@example.com"


@pytest.mark.parametrize("email", ["", "ana", "ana@", "@example.com", "ana@local"])
def test_un_correo_invalido_se_rechaza(email: str) -> None:
    with pytest.raises(InvalidEmail):
        _user(Role.owner(1), email=email)


def test_el_nombre_colapsa_espacios_y_no_puede_quedar_vacio() -> None:
    user = _user(Role.owner(1))
    user.rename("  Ana   María  Quispe ")
    assert user.full_name == "Ana María Quispe"

    with pytest.raises(InvalidFullName):
        user.rename("   ")


def test_la_contrasena_nueva_tiene_un_minimo() -> None:
    assert validate_new_password("x" * MIN_PASSWORD_LENGTH)
    with pytest.raises(WeakPassword):
        validate_new_password("x" * (MIN_PASSWORD_LENGTH - 1))


async def test_el_alta_de_personal_queda_en_el_restaurante_del_actor() -> None:
    local = await _local()
    activity = RecordingActivity()

    creado = await RegisterStaff(local.users, local.roles, FakeHasher(), activity)(
        _register_command(local.waiter.id or 0)
    )

    assert creado.restaurant_id == 1
    assert creado.role.kind is RoleKind.WAITER
    assert activity.entries == [(1, 9, ActivityKind.STAFF_REGISTERED, "Luis Torres (Mesero)")]


async def test_el_alta_no_acepta_un_rol_de_otro_restaurante() -> None:
    local = await _local()
    ajeno = await _waiter_of(local, 2)

    with pytest.raises(RoleNotFound):
        await RegisterStaff(local.users, local.roles, FakeHasher(), RecordingActivity())(
            _register_command(ajeno.id or 0)
        )


async def test_nadie_da_de_alta_con_un_rol_que_tiene_mas_permisos_que_el() -> None:
    local = await _local()

    with pytest.raises(CannotGrantPermissions):
        await RegisterStaff(local.users, local.roles, FakeHasher(), RecordingActivity())(
            _register_command(local.owner.id or 0, actor_permissions=SUPERVISOR)
        )


async def test_el_correo_es_unico_entre_restaurantes() -> None:
    local = await _local()
    await local.users.add(_user(local.owner, email="luis@example.com"))
    mesero_2 = await _waiter_of(local, 2)

    with pytest.raises(EmailAlreadyRegistered):
        await RegisterStaff(local.users, local.roles, FakeHasher(), RecordingActivity())(
            _register_command(mesero_2.id or 0, restaurant_id=2)
        )


async def test_no_se_edita_a_alguien_de_otro_restaurante() -> None:
    local = await _local()
    ajeno = await local.users.add(_user(await _waiter_of(local, 2)))

    with pytest.raises(UserNotFound):
        await _update(local)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=9,
                actor_permissions=EVERYTHING,
                user_id=ajeno.id or 0,
                role_id=local.owner.id,
            )
        )


async def test_el_encargado_no_se_degrada_ni_se_desactiva_a_si_mismo() -> None:
    local = await _local()
    yo = await local.users.add(_user(local.owner))

    with pytest.raises(CannotChangeOwnRole):
        await _update(local)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=yo.id or 0,
                actor_permissions=EVERYTHING,
                user_id=yo.id or 0,
                role_id=local.waiter.id,
            )
        )
    with pytest.raises(CannotDeactivateSelf):
        await ChangeStaffStatus(local.users, RecordingActivity(), RecordingEvents())(
            ChangeStaffStatusCommand(
                restaurant_id=1,
                actor_id=yo.id or 0,
                actor_permissions=EVERYTHING,
                user_id=yo.id or 0,
                is_active=False,
            )
        )


async def test_quien_tiene_menos_permisos_no_toca_la_cuenta_del_encargado() -> None:
    """Con solo `staff.manage` no se degrada, desactiva ni toma la cuenta del encargado."""
    local = await _local()
    encargado = await local.users.add(_user(local.owner))
    supervisor = 99

    with pytest.raises(CannotManageStrongerAccount):
        await _update(local)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=encargado.id or 0,
                role_id=local.waiter.id,
            )
        )
    with pytest.raises(CannotManageStrongerAccount):
        await ChangeStaffStatus(local.users, RecordingActivity(), RecordingEvents())(
            ChangeStaffStatusCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=encargado.id or 0,
                is_active=False,
            )
        )
    with pytest.raises(CannotManageStrongerAccount):
        await ResetStaffPassword(local.users, FakeHasher(), RecordingActivity())(
            ResetStaffPasswordCommand(
                restaurant_id=1,
                actor_id=supervisor,
                actor_permissions=SUPERVISOR,
                user_id=encargado.id or 0,
                new_password="otra-contrasena-larga",
            )
        )


async def test_nadie_asciende_a_otro_por_encima_de_si_mismo() -> None:
    local = await _local()
    ayudante_de_cocina = await local.roles.add(
        Role(restaurant_id=1, name="Ayudante", kind=RoleKind.CUSTOM)
    )
    ayudante = await local.users.add(_user(ayudante_de_cocina))

    with pytest.raises(CannotGrantPermissions):
        await _update(local)(
            UpdateStaffCommand(
                restaurant_id=1,
                actor_id=99,
                actor_permissions=SUPERVISOR,
                user_id=ayudante.id or 0,
                role_id=local.owner.id,
            )
        )


async def test_desactivar_avisa_a_la_cuenta_afectada() -> None:
    local = await _local()
    mesero = await local.users.add(_user(local.waiter, email="luis@example.com"))
    events = RecordingEvents()

    await ChangeStaffStatus(local.users, RecordingActivity(), events)(
        ChangeStaffStatusCommand(
            restaurant_id=1,
            actor_id=99,
            actor_permissions=EVERYTHING,
            user_id=mesero.id or 0,
            is_active=False,
        )
    )

    assert [(event.topic, event.user_ids) for event in events.published] == [
        (PERMISSIONS_TOPIC, {mesero.id})
    ]


async def test_el_primer_encargado_solo_entra_a_un_restaurante_vacio() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository(users)
    alta = RegisterFirstAdmin(users, roles, FakeHasher())
    comando = RegisterFirstAdminCommand(
        restaurant_id=1, email="rosa@example.com", full_name="Rosa Pérez", password=PASSWORD
    )

    primero = await alta(comando)
    assert primero.role.kind is RoleKind.OWNER
    assert {(role.kind, role.name) for role in await roles.list_for_restaurant(1)} == {
        (RoleKind.OWNER, "Encargado"),
        (RoleKind.WAITER, "Mesero"),
    }

    with pytest.raises(RestaurantAlreadyHasStaff):
        await alta(
            RegisterFirstAdminCommand(
                restaurant_id=1, email="otra@example.com", full_name="Otra", password=PASSWORD
            )
        )


@pytest.mark.parametrize(
    ("pedido", "resultado"),
    [(None, "full_name"), ("-created_at", "-created_at"), ("password_hash", "full_name")],
)
def test_solo_se_ordena_por_columnas_permitidas(pedido: str | None, resultado: str) -> None:
    assert resolve_ordering(pedido) == resultado
