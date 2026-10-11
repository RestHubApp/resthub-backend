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
    BaseRoleNameFixedError,
    BaseRoleNotDeletableError,
    CannotDeactivateSelfError,
    CannotGrantPermissionsError,
    CannotManageStrongerAccountError,
    CannotManageStrongerRoleError,
    InvalidRoleNameError,
    RoleInUseError,
    RoleNameTakenError,
    RoleNotEditableError,
    RoleNotFoundError,
    UserNotFoundError,
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

ALL_PERMISSIONS = frozenset(p.value for p in Permission)
RESTAURANT_ID = 1
ACTOR = 99
COOK_PERMISSIONS = frozenset({Permission.ORDERS_MANAGE, Permission.MENU_READ})


class AccountsSpy(InMemoryUserRepository):
    """Registra cada búsqueda para afirmar el filtro que armó el caso de uso."""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[UserQuery] = []

    async def search(self, query: UserQuery) -> Page[User]:
        self.searches.append(query)
        return await super().search(query)


class Restaurant:
    def __init__(self) -> None:
        self.users = AccountsSpy()
        self.roles = InMemoryRoleRepository(self.users)
        self.activity = RecordingActivity()
        self.events = RecordingEvents()

    async def base(self) -> tuple[Role, Role]:
        roles = await ensure_base_roles(self.roles, RESTAURANT_ID)
        await ensure_base_roles(self.roles, 2)
        return roles.owner, roles.waiter

    async def account(self, role: Role, email: str) -> User:
        return await self.users.add(
            User(
                restaurant_id=role.restaurant_id,
                email=email,
                full_name=email.split("@")[0].title(),
                role=role,
                password_hash="hash:x",
            )
        )

    async def cook(self) -> Role:
        return await self.roles.add(
            Role(RESTAURANT_ID, "Cocinero", RoleKind.CUSTOM, stored_permissions=COOK_PERMISSIONS)
        )


# -- Catálogo de permisos y roles --------------------------------------------------------


def test_owner_has_whole_catalog_and_others_only_known_stored() -> None:
    assert effective_permissions(RoleKind.OWNER, []) == frozenset(Permission)
    assert effective_permissions(
        RoleKind.CUSTOM, [Permission.MENU_READ.value, "borrado.viejo"]
    ) == {Permission.MENU_READ}
    assert effective_permissions(RoleKind.WAITER, []) == frozenset()


def test_catalog_is_shown_grouped_in_group_order() -> None:
    ordered = ordered_catalog()

    assert sorted(ordered) == sorted(CATALOG)
    groups = [CATALOG[p].group for p in ordered]
    assert groups == sorted(groups, key=PERMISSION_GROUPS.index)
    # Dentro de «Clientes y reservas» se respeta el orden del catálogo.
    customers = [p for p in ordered if CATALOG[p].group == "Clientes y reservas"]
    assert customers == [
        Permission.CUSTOMERS_READ,
        Permission.CUSTOMERS_MANAGE,
        Permission.CUSTOMERS_ERASE,
        Permission.RESERVATIONS_READ,
        Permission.RESERVATIONS_MANAGE,
    ]
    assert ordered[0] is Permission.MENU_READ
    assert ordered[-1] is Permission.ROLES_MANAGE


def test_role_name_has_max_length_and_compares_case_insensitively() -> None:
    assert Role(RESTAURANT_ID, "R" * MAX_ROLE_NAME_LENGTH, RoleKind.CUSTOM).name == "R" * 40
    with pytest.raises(InvalidRoleNameError) as too_long:
        Role(RESTAURANT_ID, "R" * (MAX_ROLE_NAME_LENGTH + 1), RoleKind.CUSTOM)
    with pytest.raises(InvalidRoleNameError) as empty:
        Role(RESTAURANT_ID, "  ", RoleKind.CUSTOM)
    assert too_long.value.reason == "El nombre del rol no puede pasar de 40 caracteres."
    assert empty.value.reason == "El nombre del rol no puede quedar vacío."
    assert role_name_key("  Jefe   de  Cocina ") == "jefe de cocina"


def test_base_roles_are_born_with_name_and_permissions() -> None:
    owner, waiter = Role.owner(RESTAURANT_ID), Role.waiter(RESTAURANT_ID)

    assert (owner.name, owner.kind, owner.is_editable) == (
        "Encargado",
        RoleKind.OWNER,
        False,
    )
    assert owner.permissions == frozenset(Permission)
    assert (waiter.name, waiter.kind, waiter.is_editable) == ("Mesero", RoleKind.WAITER, True)
    assert waiter.permissions == DEFAULT_WAITER_PERMISSIONS


def test_waiter_changes_permissions_but_not_name() -> None:
    waiter = Role.waiter(RESTAURANT_ID)

    waiter.update(" MESERO ", [Permission.MENU_READ])
    assert (waiter.name, waiter.permissions) == ("Mesero", {Permission.MENU_READ})
    with pytest.raises(BaseRoleNameFixedError) as error:
        waiter.update("Mozo", [])
    assert error.value.name == "Mesero"
    with pytest.raises(RoleNotEditableError):
        Role.owner(RESTAURANT_ID).update("Encargado", [])


def test_custom_role_can_be_renamed() -> None:
    role = Role(RESTAURANT_ID, "Cocina", RoleKind.CUSTOM)

    role.update("  Jefe  de cocina ", COOK_PERMISSIONS)

    assert (role.name, role.permissions) == ("Jefe de cocina", COOK_PERMISSIONS)


def test_only_an_empty_custom_role_is_deleted() -> None:
    Role(RESTAURANT_ID, "Cocina", RoleKind.CUSTOM).ensure_deletable(0)

    with pytest.raises(RoleInUseError) as in_use:
        Role(RESTAURANT_ID, "Cocina", RoleKind.CUSTOM).ensure_deletable(1)
    with pytest.raises(BaseRoleNotDeletableError) as base:
        Role.waiter(RESTAURANT_ID).ensure_deletable(0)
    assert in_use.value.name == "Cocina"
    assert base.value.name == "Mesero"


def test_nobody_grants_what_they_lack() -> None:
    ensure_can_grant(frozenset({Permission.MENU_READ.value}), [Permission.MENU_READ])
    ensure_can_grant(frozenset(), [])

    with pytest.raises(CannotGrantPermissionsError) as error:
        ensure_can_grant(
            frozenset({Permission.MENU_READ.value}), [Permission.MENU_READ, Permission.CASH_MANAGE]
        )
    assert error.value.missing == {Permission.CASH_MANAGE.value}
    assert holds_all(
        frozenset({Permission.MENU_READ.value, Permission.CASH_MANAGE.value}),
        [Permission.CASH_MANAGE],
    )
    assert not holds_all(frozenset({Permission.MENU_READ.value}), [Permission.CASH_MANAGE])
    assert holds_all(frozenset(), [])


# -- Roles ------------------------------------------------------------------------


async def test_roles_listed_owner_waiter_then_by_name() -> None:
    restaurant = Restaurant()
    _, waiter = await restaurant.base()
    await restaurant.roles.add(Role(RESTAURANT_ID, "cocina", RoleKind.CUSTOM))
    bar = await restaurant.roles.add(Role(RESTAURANT_ID, "Barra", RoleKind.CUSTOM))
    await restaurant.account(waiter, "luis@rosa.pe")
    await restaurant.account(waiter, "ana@rosa.pe")

    views = await ListRoles(restaurant.roles)(RESTAURANT_ID)

    assert [(v.role.name, v.member_count) for v in views] == [
        ("Encargado", 0),
        ("Mesero", 2),
        ("Barra", 0),
        ("cocina", 0),
    ]
    assert [v.is_deletable for v in views] == [False, False, True, True]
    assert views[2].role.id == bar.id


def test_custom_role_with_staff_cannot_be_deleted() -> None:
    role = Role(RESTAURANT_ID, "Cocina", RoleKind.CUSTOM)

    assert not RoleView(role, 1).is_deletable
    assert RoleView(role, 0).is_deletable


async def test_creating_role_logs_it_with_permission_count() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    create_role = CreateRole(restaurant.roles, restaurant.activity)

    one = await create_role(
        CreateRoleCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, "  Barra ", frozenset({Permission.MENU_READ})
        )
    )
    two = await create_role(
        CreateRoleCommand(RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, "Cocina", COOK_PERMISSIONS)
    )
    none_granted = await create_role(
        CreateRoleCommand(RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, "Visita", frozenset())
    )

    assert (one.role.name, one.role.kind, one.member_count) == ("Barra", RoleKind.CUSTOM, 0)
    assert two.role.permissions == COOK_PERMISSIONS
    assert none_granted.role.permissions == frozenset()
    assert restaurant.activity.entries == [
        (RESTAURANT_ID, ACTOR, ActivityKind.ROLE_CREATED, "Barra (1 permiso)"),
        (RESTAURANT_ID, ACTOR, ActivityKind.ROLE_CREATED, "Cocina (2 permisos)"),
        (RESTAURANT_ID, ACTOR, ActivityKind.ROLE_CREATED, "Visita (0 permisos)"),
    ]


async def test_creating_role_with_duplicate_name_or_foreign_permissions_does_not_save() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    create_role = CreateRole(restaurant.roles, restaurant.activity)

    with pytest.raises(RoleNameTakenError) as duplicate:
        await create_role(
            CreateRoleCommand(RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, " mesero ", frozenset())
        )
    with pytest.raises(CannotGrantPermissionsError):
        await create_role(
            CreateRoleCommand(
                RESTAURANT_ID,
                ACTOR,
                frozenset({Permission.MENU_READ.value}),
                "Caja",
                COOK_PERMISSIONS,
            )
        )

    assert duplicate.value.name == "mesero"
    assert len(await restaurant.roles.list_for_restaurant(RESTAURANT_ID)) == 2
    assert restaurant.activity.entries == []


async def test_editing_role_with_staff_notifies_their_accounts() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    luis = await restaurant.account(cook, "luis@rosa.pe")
    rose = await restaurant.account(cook, "rosa@rosa.pe")

    view = await UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)(
        UpdateRoleCommand(
            RESTAURANT_ID,
            ACTOR,
            ALL_PERMISSIONS,
            cook.id or 0,
            "Jefe de cocina",
            frozenset({Permission.MENU_READ}),
        )
    )

    assert (view.role.name, view.role.permissions, view.member_count) == (
        "Jefe de cocina",
        {Permission.MENU_READ},
        2,
    )
    stored = await restaurant.roles.get_in_restaurant(RESTAURANT_ID, cook.id or 0)
    assert stored is not None and stored.name == "Jefe de cocina"
    assert restaurant.activity.entries == [
        (RESTAURANT_ID, ACTOR, ActivityKind.ROLE_UPDATED, "Jefe de cocina (1 permiso)")
    ]
    (notice,) = restaurant.events.published
    assert (notice.restaurant_id, notice.topic, notice.user_ids, notice.reference_id) == (
        RESTAURANT_ID,
        PERMISSIONS_TOPIC,
        {luis.id, rose.id},
        cook.id,
    )


async def test_editing_without_changes_or_role_without_staff_does_not_notify() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    edit_role = UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)

    await edit_role(
        UpdateRoleCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, cook.id or 0, "Cocina 2", COOK_PERMISSIONS
        )
    )
    await restaurant.account(cook, "luis@rosa.pe")
    view = await edit_role(
        UpdateRoleCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, cook.id or 0, "Cocina 2", COOK_PERMISSIONS
        )
    )

    assert restaurant.events.published == []
    assert view.member_count == 1
    assert len(restaurant.activity.entries) == 2


async def test_changing_only_name_or_only_permissions_also_notifies() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    await restaurant.account(cook, "luis@rosa.pe")
    edit_role = UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)

    await edit_role(
        UpdateRoleCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, cook.id or 0, "Fogón", COOK_PERMISSIONS
        )
    )
    await edit_role(
        UpdateRoleCommand(
            RESTAURANT_ID,
            ACTOR,
            ALL_PERMISSIONS,
            cook.id or 0,
            "Fogón",
            frozenset({Permission.MENU_READ}),
        )
    )

    assert len(restaurant.events.published) == 2


async def test_editing_role_with_more_power_than_own_is_not_allowed_even_to_trim_it() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    menu_only = frozenset({Permission.MENU_READ.value, Permission.ROLES_MANAGE.value})
    edit_role = UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)

    with pytest.raises(CannotManageStrongerRoleError):
        await edit_role(
            UpdateRoleCommand(
                RESTAURANT_ID,
                ACTOR,
                menu_only,
                cook.id or 0,
                "Cocinero",
                frozenset({Permission.MENU_READ}),
            )
        )
    with pytest.raises(CannotGrantPermissionsError):
        await edit_role(
            UpdateRoleCommand(
                RESTAURANT_ID,
                ACTOR,
                frozenset({Permission.MENU_READ.value, Permission.ORDERS_MANAGE.value}),
                cook.id or 0,
                "Cocinero",
                COOK_PERMISSIONS | {Permission.CASH_MANAGE},
            )
        )
    stored = await restaurant.roles.get_in_restaurant(RESTAURANT_ID, cook.id or 0)
    assert stored is not None and stored.permissions == COOK_PERMISSIONS
    assert restaurant.activity.entries == []


async def test_renaming_role_to_another_roles_name_conflicts() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    edit_role = UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)

    with pytest.raises(RoleNameTakenError) as error:
        await edit_role(
            UpdateRoleCommand(
                RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, cook.id or 0, "MESERO", COOK_PERMISSIONS
            )
        )
    assert error.value.name == "MESERO"
    with pytest.raises(RoleNotFoundError):
        await edit_role(
            UpdateRoleCommand(2, ACTOR, ALL_PERMISSIONS, cook.id or 0, "Cocina", COOK_PERMISSIONS)
        )


async def test_name_from_another_restaurant_does_not_conflict() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    await restaurant.roles.add(Role(2, "Barra", RoleKind.CUSTOM))
    cook = await restaurant.cook()

    view = await UpdateRole(restaurant.roles, restaurant.activity, restaurant.events)(
        UpdateRoleCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, cook.id or 0, "Barra", COOK_PERMISSIONS
        )
    )

    assert view.role.name == "Barra"


async def test_deleting_role_logs_it_and_one_with_staff_is_not_deleted() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    bar = await restaurant.roles.add(Role(RESTAURANT_ID, "Barra", RoleKind.CUSTOM))
    await restaurant.account(cook, "luis@rosa.pe")
    delete_role = DeleteRole(restaurant.roles, restaurant.activity)

    with pytest.raises(RoleInUseError):
        await delete_role(DeleteRoleCommand(RESTAURANT_ID, ACTOR, cook.id or 0))
    with pytest.raises(RoleNotFoundError):
        await delete_role(DeleteRoleCommand(2, ACTOR, bar.id or 0))
    await delete_role(DeleteRoleCommand(RESTAURANT_ID, ACTOR, bar.id or 0))

    assert await restaurant.roles.get_in_restaurant(RESTAURANT_ID, bar.id or 0) is None
    assert await restaurant.roles.get_in_restaurant(RESTAURANT_ID, cook.id or 0) is not None
    assert restaurant.activity.entries == [
        (RESTAURANT_ID, ACTOR, ActivityKind.ROLE_DELETED, "Barra")
    ]


async def test_base_roles_created_only_if_missing() -> None:
    restaurant = Restaurant()
    first = await ensure_base_roles(restaurant.roles, RESTAURANT_ID)
    second = await ensure_base_roles(restaurant.roles, RESTAURANT_ID)

    assert (first.owner.id, first.waiter.id) == (second.owner.id, second.waiter.id)
    assert len(await restaurant.roles.list_for_restaurant(RESTAURANT_ID)) == 2


# -- Personal -----------------------------------------------------------------------


async def test_reactivating_account_logs_it_and_notifies_that_account() -> None:
    restaurant = Restaurant()
    _, waiter = await restaurant.base()
    luis = await restaurant.account(waiter, "luis@rosa.pe")
    change_status = ChangeStaffStatus(restaurant.users, restaurant.activity, restaurant.events)
    command = ChangeStaffStatusCommand(
        RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, luis.id or 0, is_active=False
    )

    deactivated = await change_status(command)
    activated = await change_status(
        ChangeStaffStatusCommand(
            RESTAURANT_ID, ACTOR, ALL_PERMISSIONS, luis.id or 0, is_active=True
        )
    )

    assert (deactivated.is_active, activated.is_active) == (False, True)
    assert (await restaurant.users.get(luis.id or 0)).is_active  # type: ignore[union-attr]
    assert restaurant.activity.entries == [
        (RESTAURANT_ID, ACTOR, ActivityKind.STAFF_STATUS_CHANGED, "Luis: cuenta desactivada"),
        (RESTAURANT_ID, ACTOR, ActivityKind.STAFF_STATUS_CHANGED, "Luis: cuenta activada"),
    ]
    assert [(e.restaurant_id, e.user_ids, e.reference_id) for e in restaurant.events.published] == [
        (RESTAURANT_ID, {luis.id}, luis.id),
        (RESTAURANT_ID, {luis.id}, luis.id),
    ]


async def test_nobody_deactivates_self_or_touches_stronger_account() -> None:
    restaurant = Restaurant()
    owner, waiter = await restaurant.base()
    me = await restaurant.account(waiter, "yo@rosa.pe")
    boss = await restaurant.account(owner, "jefa@rosa.pe")
    change_status = ChangeStaffStatus(restaurant.users, restaurant.activity, restaurant.events)

    with pytest.raises(CannotDeactivateSelfError):
        await change_status(
            ChangeStaffStatusCommand(RESTAURANT_ID, me.id or 0, ALL_PERMISSIONS, me.id or 0, False)
        )
    with pytest.raises(CannotManageStrongerAccountError):
        await change_status(
            ChangeStaffStatusCommand(
                RESTAURANT_ID,
                me.id or 0,
                frozenset({Permission.STAFF_MANAGE.value}),
                boss.id or 0,
                False,
            )
        )
    with pytest.raises(UserNotFoundError):
        await change_status(
            ChangeStaffStatusCommand(2, ACTOR, ALL_PERMISSIONS, boss.id or 0, False)
        )
    # Reactivarse a uno mismo no desactiva nada: se permite.
    await change_status(
        ChangeStaffStatusCommand(RESTAURANT_ID, me.id or 0, ALL_PERMISSIONS, me.id or 0, True)
    )
    assert (await restaurant.users.get(boss.id or 0)).is_active  # type: ignore[union-attr]
    assert len(restaurant.activity.entries) == 1


async def test_staff_list_passes_full_filter_to_repository() -> None:
    restaurant = Restaurant()
    await restaurant.base()

    await ListStaff(restaurant.users)(
        ListStaffQuery(
            RESTAURANT_ID,
            role_ids=frozenset({3}),
            search="ana",
            is_active=True,
            ordering="-full_name",
            limit=5,
            offset=10,
        )
    )
    await ListStaff(restaurant.users)(ListStaffQuery(RESTAURANT_ID, ordering="password_hash"))

    assert restaurant.users.searches == [
        UserQuery(
            restaurant_id=RESTAURANT_ID,
            role_ids=frozenset({3}),
            search="ana",
            is_active=True,
            ordering="-full_name",
            limit=5,
            offset=10,
        ),
        UserQuery(restaurant_id=RESTAURANT_ID, ordering=DEFAULT_ORDERING),
    ]


async def test_viewing_account_of_other_restaurant_says_not_found() -> None:
    restaurant = Restaurant()
    _, waiter = await restaurant.base()
    luis = await restaurant.account(waiter, "luis@rosa.pe")
    read_member = ReadStaffMember(restaurant.users)

    assert (await read_member(RESTAURANT_ID, luis.id or 0)).email == "luis@rosa.pe"
    with pytest.raises(UserNotFoundError) as error:
        await read_member(2, luis.id or 0)
    assert error.value.user_id == luis.id


# -- Bitácora -------------------------------------------------------------------------


class ActivityLog:
    def __init__(self, *records: ActivityRecord) -> None:
        self.records = list(records)
        self.queries: list[ActivityQuery] = []

    async def search(self, query: ActivityQuery) -> Page[ActivityRecord]:
        self.queries.append(query)
        items = [
            r
            for r in self.records
            if r.restaurant_id == query.restaurant_id
            and (query.user_ids is None or r.user_id in query.user_ids)
        ]
        return Page(items=items, total=len(items) + 10)


def _entry(user_id: int, detail: str) -> ActivityRecord:
    return ActivityRecord(
        RESTAURANT_ID, user_id, ActivityKind.ORDER_CHARGED, detail, occurred_at=datetime.now(UTC)
    )


async def test_activity_log_brings_author_of_each_entry() -> None:
    restaurant = Restaurant()
    owner, waiter = await restaurant.base()
    boss = await restaurant.account(owner, "jefa@rosa.pe")
    luis = await restaurant.account(waiter, "luis@rosa.pe")
    activity_log = ActivityLog(
        _entry(boss.id or 0, "a"), _entry(luis.id or 0, "b"), _entry(777, "c")
    )

    page = await ReadActivity(activity_log, restaurant.users)(
        ReadActivityQuery(
            RESTAURANT_ID, kinds=frozenset({ActivityKind.ORDER_CHARGED}), limit=7, offset=2
        )
    )

    # El asiento de una cuenta que ya no está en el local se omite.
    assert [(e.record.detail, e.user.email) for e in page.items] == [
        ("a", "jefa@rosa.pe"),
        ("b", "luis@rosa.pe"),
    ]
    assert page.total == 13
    assert activity_log.queries == [
        ActivityQuery(
            restaurant_id=RESTAURANT_ID,
            user_ids=None,
            kinds=frozenset({ActivityKind.ORDER_CHARGED}),
            limit=7,
            offset=2,
        )
    ]
    assert restaurant.users.searches == [
        UserQuery(
            restaurant_id=RESTAURANT_ID,
            ids=frozenset({boss.id, luis.id, 777}),
            limit=MAX_USERS_PER_PAGE,
        )
    ]


async def test_filtering_by_role_translates_to_accounts_of_that_role() -> None:
    restaurant = Restaurant()
    owner, waiter = await restaurant.base()
    boss = await restaurant.account(owner, "jefa@rosa.pe")
    luis = await restaurant.account(waiter, "luis@rosa.pe")
    activity_log = ActivityLog(_entry(boss.id or 0, "a"), _entry(luis.id or 0, "b"))

    page = await ReadActivity(activity_log, restaurant.users)(
        ReadActivityQuery(RESTAURANT_ID, role_ids=frozenset({waiter.id or 0}))
    )

    assert [e.record.detail for e in page.items] == ["b"]
    assert activity_log.queries[0].user_ids == {luis.id}
    assert restaurant.users.searches[0] == UserQuery(
        restaurant_id=RESTAURANT_ID, role_ids=frozenset({waiter.id or 0}), limit=MAX_USERS_PER_PAGE
    )


async def test_role_without_staff_returns_empty_log_without_querying_it() -> None:
    restaurant = Restaurant()
    await restaurant.base()
    cook = await restaurant.cook()
    activity_log = ActivityLog(_entry(1, "a"))

    page = await ReadActivity(activity_log, restaurant.users)(
        ReadActivityQuery(RESTAURANT_ID, role_ids=frozenset({cook.id or 0}))
    )

    assert (page.items, page.total) == ([], 0)
    assert activity_log.queries == []


async def test_page_without_entries_does_not_search_authors() -> None:
    restaurant = Restaurant()
    await restaurant.base()

    page = await ReadActivity(ActivityLog(), restaurant.users)(ReadActivityQuery(RESTAURANT_ID))

    assert page.items == []
    assert restaurant.users.searches == []
