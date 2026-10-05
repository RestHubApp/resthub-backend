"""Casos de uso de roles, personal y bitácora con dobles en memoria.

Complementa `test_accounts_domain.py` con lo que dejan anotado, lo que avisan
y lo que le piden al repositorio: el filtro exacto de una búsqueda, el asiento
de bitácora y a quién llega el aviso de permisos.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from resthub.core.activity import ActivityKind, ActivityQuery, ActivityRecord
from resthub.core.pagination import Page
from resthub.core.permissions import (
    CATALOG,
    DEFAULT_WAITER_PERMISSIONS,
    PERMISSION_GROUPS,
    Permission,
    RoleKind,
    effective_permissions,
    ordered_catalog,
)
from resthub.core.realtime import PERMISSIONS_TOPIC
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    BaseRoleNameFixed,
    BaseRoleNotDeletable,
    CannotDeactivateSelf,
    CannotGrantPermissions,
    CannotManageStrongerAccount,
    CannotManageStrongerRole,
    InvalidRoleName,
    RoleInUse,
    RoleNameTaken,
    RoleNotEditable,
    RoleNotFound,
    UserNotFound,
)
from resthub.modules.accounts.domain.roles import (
    MAX_ROLE_NAME_LENGTH,
    Role,
    ensure_can_grant,
    holds_all,
    role_name_key,
)
from resthub.modules.accounts.ports.user_repository import UserQuery
from resthub.modules.accounts.use_cases.manage_roles import (
    CreateRole,
    CreateRoleCommand,
    DeleteRole,
    DeleteRoleCommand,
    ListRoles,
    RoleView,
    UpdateRole,
    UpdateRoleCommand,
    ensure_base_roles,
)
from resthub.modules.accounts.use_cases.manage_staff import (
    DEFAULT_ORDERING,
    ChangeStaffStatus,
    ChangeStaffStatusCommand,
    ListStaff,
    ListStaffQuery,
    ReadStaffMember,
)
from resthub.modules.accounts.use_cases.read_activity import (
    MAX_USERS_PER_PAGE,
    ReadActivity,
    ReadActivityQuery,
)
from tests.conftest import RecordingActivity
from tests.fakes import InMemoryRoleRepository, InMemoryUserRepository, RecordingEvents

TODO = frozenset(p.value for p in Permission)
LOCAL = 1
ACTOR = 99
COCINA = frozenset({Permission.ORDERS_MANAGE, Permission.MENU_READ})


class EspiaDeCuentas(InMemoryUserRepository):
    """Registra cada búsqueda para afirmar el filtro que armó el caso de uso."""

    def __init__(self) -> None:
        super().__init__()
        self.busquedas: list[UserQuery] = []

    async def search(self, query: UserQuery) -> Page[User]:
        self.busquedas.append(query)
        return await super().search(query)


class Local:
    def __init__(self) -> None:
        self.users = EspiaDeCuentas()
        self.roles = InMemoryRoleRepository(self.users)
        self.activity = RecordingActivity()
        self.events = RecordingEvents()

    async def base(self) -> tuple[Role, Role]:
        roles = await ensure_base_roles(self.roles, LOCAL)
        await ensure_base_roles(self.roles, 2)
        return roles.owner, roles.waiter

    async def cuenta(self, role: Role, email: str) -> User:
        return await self.users.add(
            User(
                restaurant_id=role.restaurant_id,
                email=email,
                full_name=email.split("@")[0].title(),
                role=role,
                password_hash="hash:x",
            )
        )

    async def cocinero(self) -> Role:
        return await self.roles.add(
            Role(LOCAL, "Cocinero", RoleKind.CUSTOM, stored_permissions=COCINA)
        )


# -- Catálogo de permisos y roles --------------------------------------------------------


def test_el_encargado_tiene_todo_el_catalogo_y_el_resto_lo_guardado_conocido() -> None:
    assert effective_permissions(RoleKind.OWNER, []) == frozenset(Permission)
    assert effective_permissions(RoleKind.CUSTOM, ["menu.read", "borrado.viejo"]) == {
        Permission.MENU_READ
    }
    assert effective_permissions(RoleKind.WAITER, []) == frozenset()


def test_el_catalogo_se_muestra_agrupado_en_el_orden_de_los_grupos() -> None:
    orden = ordered_catalog()

    assert sorted(orden) == sorted(CATALOG)
    grupos = [CATALOG[p].group for p in orden]
    assert grupos == sorted(grupos, key=PERMISSION_GROUPS.index)
    # Dentro de «Clientes y reservas» se respeta el orden del catálogo.
    clientes = [p for p in orden if CATALOG[p].group == "Clientes y reservas"]
    assert clientes == [
        Permission.CUSTOMERS_READ,
        Permission.CUSTOMERS_MANAGE,
        Permission.CUSTOMERS_ERASE,
        Permission.RESERVATIONS_READ,
        Permission.RESERVATIONS_MANAGE,
    ]
    assert orden[0] is Permission.MENU_READ
    assert orden[-1] is Permission.ROLES_MANAGE


def test_el_nombre_del_rol_tiene_tope_y_se_compara_sin_mayusculas() -> None:
    assert Role(LOCAL, "R" * MAX_ROLE_NAME_LENGTH, RoleKind.CUSTOM).name == "R" * 40
    with pytest.raises(InvalidRoleName) as largo:
        Role(LOCAL, "R" * (MAX_ROLE_NAME_LENGTH + 1), RoleKind.CUSTOM)
    with pytest.raises(InvalidRoleName) as vacio:
        Role(LOCAL, "  ", RoleKind.CUSTOM)
    assert largo.value.reason == "El nombre del rol no puede pasar de 40 caracteres."
    assert vacio.value.reason == "El nombre del rol no puede quedar vacío."
    assert role_name_key("  Jefe   de  Cocina ") == "jefe de cocina"


def test_los_roles_base_nacen_con_su_nombre_y_permisos() -> None:
    encargado, mesero = Role.owner(LOCAL), Role.waiter(LOCAL)

    assert (encargado.name, encargado.kind, encargado.is_editable) == (
        "Encargado",
        RoleKind.OWNER,
        False,
    )
    assert encargado.permissions == frozenset(Permission)
    assert (mesero.name, mesero.kind, mesero.is_editable) == ("Mesero", RoleKind.WAITER, True)
    assert mesero.permissions == DEFAULT_WAITER_PERMISSIONS


def test_el_mesero_cambia_permisos_pero_no_de_nombre() -> None:
    mesero = Role.waiter(LOCAL)

    mesero.update(" MESERO ", [Permission.MENU_READ])
    assert (mesero.name, mesero.permissions) == ("Mesero", {Permission.MENU_READ})
    with pytest.raises(BaseRoleNameFixed) as error:
        mesero.update("Mozo", [])
    assert error.value.name == "Mesero"
    with pytest.raises(RoleNotEditable):
        Role.owner(LOCAL).update("Encargado", [])


def test_un_rol_propio_cambia_de_nombre() -> None:
    rol = Role(LOCAL, "Cocina", RoleKind.CUSTOM)

    rol.update("  Jefe  de cocina ", COCINA)

    assert (rol.name, rol.permissions) == ("Jefe de cocina", COCINA)


def test_solo_un_rol_propio_y_vacio_se_borra() -> None:
    Role(LOCAL, "Cocina", RoleKind.CUSTOM).ensure_deletable(0)

    with pytest.raises(RoleInUse) as en_uso:
        Role(LOCAL, "Cocina", RoleKind.CUSTOM).ensure_deletable(1)
    with pytest.raises(BaseRoleNotDeletable) as base:
        Role.waiter(LOCAL).ensure_deletable(0)
    assert en_uso.value.name == "Cocina"
    assert base.value.name == "Mesero"


def test_nadie_reparte_lo_que_no_tiene() -> None:
    ensure_can_grant(frozenset({"menu.read"}), [Permission.MENU_READ])
    ensure_can_grant(frozenset(), [])

    with pytest.raises(CannotGrantPermissions) as error:
        ensure_can_grant(frozenset({"menu.read"}), [Permission.MENU_READ, Permission.CASH_MANAGE])
    assert error.value.missing == {"cash.manage"}
    assert holds_all(frozenset({"menu.read", "cash.manage"}), [Permission.CASH_MANAGE])
    assert not holds_all(frozenset({"menu.read"}), [Permission.CASH_MANAGE])
    assert holds_all(frozenset(), [])


# -- Roles ------------------------------------------------------------------------


async def test_los_roles_se_listan_encargado_mesero_y_luego_por_nombre() -> None:
    local = Local()
    encargado, mesero = await local.base()
    await local.roles.add(Role(LOCAL, "cocina", RoleKind.CUSTOM))
    barra = await local.roles.add(Role(LOCAL, "Barra", RoleKind.CUSTOM))
    await local.cuenta(mesero, "luis@rosa.pe")
    await local.cuenta(mesero, "ana@rosa.pe")

    vistas = await ListRoles(local.roles)(LOCAL)

    assert [(v.role.name, v.member_count) for v in vistas] == [
        ("Encargado", 0),
        ("Mesero", 2),
        ("Barra", 0),
        ("cocina", 0),
    ]
    assert [v.is_deletable for v in vistas] == [False, False, True, True]
    assert vistas[2].role.id == barra.id


def test_un_rol_propio_con_personal_no_se_puede_borrar() -> None:
    rol = Role(LOCAL, "Cocina", RoleKind.CUSTOM)

    assert not RoleView(rol, 1).is_deletable
    assert RoleView(rol, 0).is_deletable


async def test_crear_un_rol_lo_anota_con_su_cantidad_de_permisos() -> None:
    local = Local()
    await local.base()
    crear = CreateRole(local.roles, local.activity)

    uno = await crear(
        CreateRoleCommand(LOCAL, ACTOR, TODO, "  Barra ", frozenset({Permission.MENU_READ}))
    )
    dos = await crear(CreateRoleCommand(LOCAL, ACTOR, TODO, "Cocina", COCINA))
    ninguno = await crear(CreateRoleCommand(LOCAL, ACTOR, TODO, "Visita", frozenset()))

    assert (uno.role.name, uno.role.kind, uno.member_count) == ("Barra", RoleKind.CUSTOM, 0)
    assert dos.role.permissions == COCINA
    assert ninguno.role.permissions == frozenset()
    assert local.activity.entries == [
        (LOCAL, ACTOR, ActivityKind.ROLE_CREATED, "Barra (1 permiso)"),
        (LOCAL, ACTOR, ActivityKind.ROLE_CREATED, "Cocina (2 permisos)"),
        (LOCAL, ACTOR, ActivityKind.ROLE_CREATED, "Visita (0 permisos)"),
    ]


async def test_crear_un_rol_con_nombre_repetido_o_permisos_ajenos_no_guarda() -> None:
    local = Local()
    await local.base()
    crear = CreateRole(local.roles, local.activity)

    with pytest.raises(RoleNameTaken) as repetido:
        await crear(CreateRoleCommand(LOCAL, ACTOR, TODO, " mesero ", frozenset()))
    with pytest.raises(CannotGrantPermissions):
        await crear(CreateRoleCommand(LOCAL, ACTOR, frozenset({"menu.read"}), "Caja", COCINA))

    assert repetido.value.name == "mesero"
    assert len(await local.roles.list_for_restaurant(LOCAL)) == 2
    assert local.activity.entries == []


async def test_editar_un_rol_con_personal_avisa_a_sus_cuentas() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    luis = await local.cuenta(cocinero, "luis@rosa.pe")
    rosa = await local.cuenta(cocinero, "rosa@rosa.pe")

    vista = await UpdateRole(local.roles, local.activity, local.events)(
        UpdateRoleCommand(
            LOCAL,
            ACTOR,
            TODO,
            cocinero.id or 0,
            "Jefe de cocina",
            frozenset({Permission.MENU_READ}),
        )
    )

    assert (vista.role.name, vista.role.permissions, vista.member_count) == (
        "Jefe de cocina",
        {Permission.MENU_READ},
        2,
    )
    guardado = await local.roles.get_in_restaurant(LOCAL, cocinero.id or 0)
    assert guardado is not None and guardado.name == "Jefe de cocina"
    assert local.activity.entries == [
        (LOCAL, ACTOR, ActivityKind.ROLE_UPDATED, "Jefe de cocina (1 permiso)")
    ]
    (aviso,) = local.events.published
    assert (aviso.restaurant_id, aviso.topic, aviso.user_ids, aviso.reference_id) == (
        LOCAL,
        PERMISSIONS_TOPIC,
        {luis.id, rosa.id},
        cocinero.id,
    )


async def test_editar_sin_cambios_o_un_rol_sin_personal_no_avisa() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    editar = UpdateRole(local.roles, local.activity, local.events)

    await editar(UpdateRoleCommand(LOCAL, ACTOR, TODO, cocinero.id or 0, "Cocina 2", COCINA))
    await local.cuenta(cocinero, "luis@rosa.pe")
    vista = await editar(
        UpdateRoleCommand(LOCAL, ACTOR, TODO, cocinero.id or 0, "Cocina 2", COCINA)
    )

    assert local.events.published == []
    assert vista.member_count == 1
    assert len(local.activity.entries) == 2


async def test_cambiar_solo_el_nombre_o_solo_los_permisos_tambien_avisa() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    await local.cuenta(cocinero, "luis@rosa.pe")
    editar = UpdateRole(local.roles, local.activity, local.events)

    await editar(UpdateRoleCommand(LOCAL, ACTOR, TODO, cocinero.id or 0, "Fogón", COCINA))
    await editar(
        UpdateRoleCommand(
            LOCAL, ACTOR, TODO, cocinero.id or 0, "Fogón", frozenset({Permission.MENU_READ})
        )
    )

    assert len(local.events.published) == 2


async def test_editar_un_rol_con_mas_poder_que_uno_no_se_permite_ni_para_recortarlo() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    solo_menu = frozenset({"menu.read", "roles.manage"})
    editar = UpdateRole(local.roles, local.activity, local.events)

    with pytest.raises(CannotManageStrongerRole):
        await editar(
            UpdateRoleCommand(
                LOCAL,
                ACTOR,
                solo_menu,
                cocinero.id or 0,
                "Cocinero",
                frozenset({Permission.MENU_READ}),
            )
        )
    with pytest.raises(CannotGrantPermissions):
        await editar(
            UpdateRoleCommand(
                LOCAL,
                ACTOR,
                frozenset({"menu.read", "orders.manage"}),
                cocinero.id or 0,
                "Cocinero",
                COCINA | {Permission.CASH_MANAGE},
            )
        )
    guardado = await local.roles.get_in_restaurant(LOCAL, cocinero.id or 0)
    assert guardado is not None and guardado.permissions == COCINA
    assert local.activity.entries == []


async def test_renombrar_un_rol_con_el_nombre_de_otro_choca() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    editar = UpdateRole(local.roles, local.activity, local.events)

    with pytest.raises(RoleNameTaken) as error:
        await editar(UpdateRoleCommand(LOCAL, ACTOR, TODO, cocinero.id or 0, "MESERO", COCINA))
    assert error.value.name == "MESERO"
    with pytest.raises(RoleNotFound):
        await editar(UpdateRoleCommand(2, ACTOR, TODO, cocinero.id or 0, "Cocina", COCINA))


async def test_el_nombre_de_otro_local_no_choca() -> None:
    local = Local()
    await local.base()
    await local.roles.add(Role(2, "Barra", RoleKind.CUSTOM))
    cocinero = await local.cocinero()

    vista = await UpdateRole(local.roles, local.activity, local.events)(
        UpdateRoleCommand(LOCAL, ACTOR, TODO, cocinero.id or 0, "Barra", COCINA)
    )

    assert vista.role.name == "Barra"


async def test_borrar_un_rol_lo_anota_y_uno_con_personal_no_se_borra() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    barra = await local.roles.add(Role(LOCAL, "Barra", RoleKind.CUSTOM))
    await local.cuenta(cocinero, "luis@rosa.pe")
    borrar = DeleteRole(local.roles, local.activity)

    with pytest.raises(RoleInUse):
        await borrar(DeleteRoleCommand(LOCAL, ACTOR, cocinero.id or 0))
    with pytest.raises(RoleNotFound):
        await borrar(DeleteRoleCommand(2, ACTOR, barra.id or 0))
    await borrar(DeleteRoleCommand(LOCAL, ACTOR, barra.id or 0))

    assert await local.roles.get_in_restaurant(LOCAL, barra.id or 0) is None
    assert await local.roles.get_in_restaurant(LOCAL, cocinero.id or 0) is not None
    assert local.activity.entries == [(LOCAL, ACTOR, ActivityKind.ROLE_DELETED, "Barra")]


async def test_los_roles_base_solo_se_crean_si_faltan() -> None:
    local = Local()
    primero = await ensure_base_roles(local.roles, LOCAL)
    segundo = await ensure_base_roles(local.roles, LOCAL)

    assert (primero.owner.id, primero.waiter.id) == (segundo.owner.id, segundo.waiter.id)
    assert len(await local.roles.list_for_restaurant(LOCAL)) == 2


# -- Personal -----------------------------------------------------------------------


async def test_reactivar_una_cuenta_la_anota_y_avisa_a_esa_cuenta() -> None:
    local = Local()
    _, mesero = await local.base()
    luis = await local.cuenta(mesero, "luis@rosa.pe")
    cambiar = ChangeStaffStatus(local.users, local.activity, local.events)
    comando = ChangeStaffStatusCommand(LOCAL, ACTOR, TODO, luis.id or 0, is_active=False)

    apagada = await cambiar(comando)
    prendida = await cambiar(
        ChangeStaffStatusCommand(LOCAL, ACTOR, TODO, luis.id or 0, is_active=True)
    )

    assert (apagada.is_active, prendida.is_active) == (False, True)
    assert (await local.users.get(luis.id or 0)).is_active  # type: ignore[union-attr]
    assert local.activity.entries == [
        (LOCAL, ACTOR, ActivityKind.STAFF_STATUS_CHANGED, "Luis: cuenta desactivada"),
        (LOCAL, ACTOR, ActivityKind.STAFF_STATUS_CHANGED, "Luis: cuenta activada"),
    ]
    assert [(e.restaurant_id, e.user_ids, e.reference_id) for e in local.events.published] == [
        (LOCAL, {luis.id}, luis.id),
        (LOCAL, {luis.id}, luis.id),
    ]


async def test_nadie_se_desactiva_a_si_mismo_ni_toca_una_cuenta_mas_fuerte() -> None:
    local = Local()
    encargado, mesero = await local.base()
    yo = await local.cuenta(mesero, "yo@rosa.pe")
    jefa = await local.cuenta(encargado, "jefa@rosa.pe")
    cambiar = ChangeStaffStatus(local.users, local.activity, local.events)

    with pytest.raises(CannotDeactivateSelf):
        await cambiar(ChangeStaffStatusCommand(LOCAL, yo.id or 0, TODO, yo.id or 0, False))
    with pytest.raises(CannotManageStrongerAccount):
        await cambiar(
            ChangeStaffStatusCommand(
                LOCAL, yo.id or 0, frozenset({"staff.manage"}), jefa.id or 0, False
            )
        )
    with pytest.raises(UserNotFound):
        await cambiar(ChangeStaffStatusCommand(2, ACTOR, TODO, jefa.id or 0, False))
    # Reactivarse a uno mismo no desactiva nada: se permite.
    await cambiar(ChangeStaffStatusCommand(LOCAL, yo.id or 0, TODO, yo.id or 0, True))
    assert (await local.users.get(jefa.id or 0)).is_active  # type: ignore[union-attr]
    assert len(local.activity.entries) == 1


async def test_la_lista_de_personal_pasa_el_filtro_completo_al_repositorio() -> None:
    local = Local()
    await local.base()

    await ListStaff(local.users)(
        ListStaffQuery(
            LOCAL,
            role_ids=frozenset({3}),
            search="ana",
            is_active=True,
            ordering="-full_name",
            limit=5,
            offset=10,
        )
    )
    await ListStaff(local.users)(ListStaffQuery(LOCAL, ordering="password_hash"))

    assert local.users.busquedas == [
        UserQuery(
            restaurant_id=LOCAL,
            role_ids=frozenset({3}),
            search="ana",
            is_active=True,
            ordering="-full_name",
            limit=5,
            offset=10,
        ),
        UserQuery(restaurant_id=LOCAL, ordering=DEFAULT_ORDERING),
    ]


async def test_ver_una_cuenta_de_otro_local_responde_que_no_existe() -> None:
    local = Local()
    _, mesero = await local.base()
    luis = await local.cuenta(mesero, "luis@rosa.pe")
    leer = ReadStaffMember(local.users)

    assert (await leer(LOCAL, luis.id or 0)).email == "luis@rosa.pe"
    with pytest.raises(UserNotFound) as error:
        await leer(2, luis.id or 0)
    assert error.value.user_id == luis.id


# -- Bitácora -------------------------------------------------------------------------


class Libro:
    def __init__(self, *records: ActivityRecord) -> None:
        self.records = list(records)
        self.consultas: list[ActivityQuery] = []

    async def search(self, query: ActivityQuery) -> Page[ActivityRecord]:
        self.consultas.append(query)
        items = [
            r
            for r in self.records
            if r.restaurant_id == query.restaurant_id
            and (query.user_ids is None or r.user_id in query.user_ids)
        ]
        return Page(items=items, total=len(items) + 10)


def _asiento(user_id: int, detalle: str) -> ActivityRecord:
    return ActivityRecord(
        LOCAL, user_id, ActivityKind.ORDER_CHARGED, detalle, occurred_at=datetime.now(UTC)
    )


async def test_la_bitacora_trae_el_autor_de_cada_asiento() -> None:
    local = Local()
    encargado, mesero = await local.base()
    jefa = await local.cuenta(encargado, "jefa@rosa.pe")
    luis = await local.cuenta(mesero, "luis@rosa.pe")
    libro = Libro(_asiento(jefa.id or 0, "a"), _asiento(luis.id or 0, "b"), _asiento(777, "c"))

    pagina = await ReadActivity(libro, local.users)(
        ReadActivityQuery(LOCAL, kinds=frozenset({ActivityKind.ORDER_CHARGED}), limit=7, offset=2)
    )

    # El asiento de una cuenta que ya no está en el local se omite.
    assert [(e.record.detail, e.user.email) for e in pagina.items] == [
        ("a", "jefa@rosa.pe"),
        ("b", "luis@rosa.pe"),
    ]
    assert pagina.total == 13
    assert libro.consultas == [
        ActivityQuery(
            restaurant_id=LOCAL,
            user_ids=None,
            kinds=frozenset({ActivityKind.ORDER_CHARGED}),
            limit=7,
            offset=2,
        )
    ]
    assert local.users.busquedas == [
        UserQuery(
            restaurant_id=LOCAL,
            ids=frozenset({jefa.id, luis.id, 777}),
            limit=MAX_USERS_PER_PAGE,
        )
    ]


async def test_filtrar_por_rol_se_traduce_a_las_cuentas_de_ese_rol() -> None:
    local = Local()
    encargado, mesero = await local.base()
    jefa = await local.cuenta(encargado, "jefa@rosa.pe")
    luis = await local.cuenta(mesero, "luis@rosa.pe")
    libro = Libro(_asiento(jefa.id or 0, "a"), _asiento(luis.id or 0, "b"))

    pagina = await ReadActivity(libro, local.users)(
        ReadActivityQuery(LOCAL, role_ids=frozenset({mesero.id or 0}))
    )

    assert [e.record.detail for e in pagina.items] == ["b"]
    assert libro.consultas[0].user_ids == {luis.id}
    assert local.users.busquedas[0] == UserQuery(
        restaurant_id=LOCAL, role_ids=frozenset({mesero.id or 0}), limit=MAX_USERS_PER_PAGE
    )


async def test_un_rol_sin_personal_devuelve_la_bitacora_vacia_sin_consultarla() -> None:
    local = Local()
    await local.base()
    cocinero = await local.cocinero()
    libro = Libro(_asiento(1, "a"))

    pagina = await ReadActivity(libro, local.users)(
        ReadActivityQuery(LOCAL, role_ids=frozenset({cocinero.id or 0}))
    )

    assert (pagina.items, pagina.total) == ([], 0)
    assert libro.consultas == []


async def test_una_pagina_sin_asientos_no_busca_autores() -> None:
    local = Local()
    await local.base()

    pagina = await ReadActivity(Libro(), local.users)(ReadActivityQuery(LOCAL))

    assert pagina.items == []
    assert local.users.busquedas == []
