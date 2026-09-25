"""Reglas de dominio de cuentas y del personal, sin base ni servidor."""

from __future__ import annotations

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Role
from resthub.core.realtime import PERMISSIONS_TOPIC
from resthub.modules.accounts.domain.entities import (
    MIN_PASSWORD_LENGTH,
    User,
    validate_new_password,
)
from resthub.modules.accounts.domain.exceptions import (
    CannotChangeOwnRole,
    CannotDeactivateSelf,
    EmailAlreadyRegistered,
    InvalidEmail,
    InvalidFullName,
    RestaurantAlreadyHasStaff,
    UserNotFound,
    WeakPassword,
)
from resthub.modules.accounts.use_cases.manage_staff import (
    ChangeStaffStatus,
    ChangeStaffStatusCommand,
    RegisterFirstAdmin,
    RegisterFirstAdminCommand,
    RegisterStaff,
    RegisterStaffCommand,
    UpdateStaff,
    UpdateStaffCommand,
    resolve_ordering,
)
from tests.conftest import RecordingActivity
from tests.fakes import FakeHasher, InMemoryUserRepository, RecordingEvents

PASSWORD = "contrasena-larga"


def _user(restaurant_id: int = 1, email: str = "ana@example.com", role: Role = Role.ADMIN) -> User:
    return User(
        restaurant_id=restaurant_id,
        email=email,
        full_name="Ana Quispe",
        role=role,
        password_hash="hash:x",
    )


def test_el_correo_se_normaliza() -> None:
    assert _user(email="  Ana@Example.COM ").email == "ana@example.com"


@pytest.mark.parametrize("email", ["", "ana", "ana@", "@example.com", "ana@local"])
def test_un_correo_invalido_se_rechaza(email: str) -> None:
    with pytest.raises(InvalidEmail):
        _user(email=email)


def test_el_nombre_colapsa_espacios_y_no_puede_quedar_vacio() -> None:
    user = _user()
    user.rename("  Ana   María  Quispe ")
    assert user.full_name == "Ana María Quispe"

    with pytest.raises(InvalidFullName):
        user.rename("   ")


def test_la_contrasena_nueva_tiene_un_minimo() -> None:
    assert validate_new_password("x" * MIN_PASSWORD_LENGTH)
    with pytest.raises(WeakPassword):
        validate_new_password("x" * (MIN_PASSWORD_LENGTH - 1))


async def test_el_alta_de_personal_queda_en_el_restaurante_del_actor() -> None:
    users = InMemoryUserRepository()
    activity = RecordingActivity()

    creado = await RegisterStaff(users, FakeHasher(), activity)(
        RegisterStaffCommand(
            restaurant_id=4,
            actor_id=9,
            email="luis@example.com",
            full_name="Luis Torres",
            role=Role.WAITER,
            password=PASSWORD,
        )
    )

    assert creado.restaurant_id == 4
    assert activity.entries[0][:3] == (4, 9, ActivityKind.STAFF_REGISTERED)


async def test_el_correo_es_unico_entre_restaurantes() -> None:
    users = InMemoryUserRepository()
    await users.add(_user(restaurant_id=1, email="luis@example.com"))

    with pytest.raises(EmailAlreadyRegistered):
        await RegisterStaff(users, FakeHasher(), RecordingActivity())(
            RegisterStaffCommand(
                restaurant_id=2,
                actor_id=9,
                email="luis@example.com",
                full_name="Luis Torres",
                role=Role.WAITER,
                password=PASSWORD,
            )
        )


async def test_no_se_edita_a_alguien_de_otro_restaurante() -> None:
    users = InMemoryUserRepository()
    ajeno = await users.add(_user(restaurant_id=2, role=Role.WAITER))

    with pytest.raises(UserNotFound):
        await UpdateStaff(users, RecordingActivity(), RecordingEvents())(
            UpdateStaffCommand(restaurant_id=1, actor_id=9, user_id=ajeno.id or 0, role=Role.ADMIN)
        )


async def test_el_encargado_no_se_degrada_ni_se_desactiva_a_si_mismo() -> None:
    users = InMemoryUserRepository()
    yo = await users.add(_user())

    with pytest.raises(CannotChangeOwnRole):
        await UpdateStaff(users, RecordingActivity(), RecordingEvents())(
            UpdateStaffCommand(
                restaurant_id=1, actor_id=yo.id or 0, user_id=yo.id or 0, role=Role.WAITER
            )
        )
    with pytest.raises(CannotDeactivateSelf):
        await ChangeStaffStatus(users, RecordingActivity(), RecordingEvents())(
            ChangeStaffStatusCommand(
                restaurant_id=1, actor_id=yo.id or 0, user_id=yo.id or 0, is_active=False
            )
        )


async def test_desactivar_avisa_a_la_cuenta_afectada() -> None:
    users = InMemoryUserRepository()
    mesero = await users.add(_user(email="luis@example.com", role=Role.WAITER))
    events = RecordingEvents()

    await ChangeStaffStatus(users, RecordingActivity(), events)(
        ChangeStaffStatusCommand(
            restaurant_id=1, actor_id=99, user_id=mesero.id or 0, is_active=False
        )
    )

    assert [(event.topic, event.user_ids) for event in events.published] == [
        (PERMISSIONS_TOPIC, {mesero.id})
    ]


async def test_el_primer_encargado_solo_entra_a_un_restaurante_vacio() -> None:
    users = InMemoryUserRepository()
    alta = RegisterFirstAdmin(users, FakeHasher())
    comando = RegisterFirstAdminCommand(
        restaurant_id=1, email="rosa@example.com", full_name="Rosa Pérez", password=PASSWORD
    )

    primero = await alta(comando)
    assert primero.role is Role.ADMIN

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
