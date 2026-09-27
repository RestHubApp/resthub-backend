"""Casos de uso del menú con un repositorio en memoria: qué cambia, qué se anota y qué se avisa."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.modules.menu.domain.entities import MenuCategory, MenuItem
from resthub.modules.menu.domain.exceptions import (
    CategoryNameTaken,
    CategoryNotEmpty,
    CategoryNotFound,
    InvalidMenuItem,
    MenuItemNameTaken,
    MenuItemNotFound,
)
from resthub.modules.menu.domain.modifiers import ModifierGroup, ModifierOption
from resthub.modules.menu.use_cases.manage_categories import (
    CreateCategory,
    CreateCategoryCommand,
    DeleteCategory,
    DeleteCategoryCommand,
    UpdateCategory,
    UpdateCategoryCommand,
)
from resthub.modules.menu.use_cases.manage_items import (
    CreateMenuItem,
    CreateMenuItemCommand,
    SetAvailability,
    SetAvailabilityCommand,
    UpdateMenuItem,
    UpdateMenuItemCommand,
)
from resthub.modules.menu.use_cases.read_menu import ReadMenu, ReadMenuItem, ReadMenuQuery
from resthub.modules.menu.use_cases.shared import MENU_TOPIC
from tests.fakes import RecordingEvents

LOCAL = 1
OTRO_LOCAL = 2
ENCARGADO = 10


class MemoryMenu:
    """Solo lo que usan estos casos de uso; los nombres se comparan sin mayúsculas."""

    def __init__(self) -> None:
        self.categories: dict[int, MenuCategory] = {}
        self.items: dict[int, MenuItem] = {}

    async def add_category(self, category: MenuCategory) -> MenuCategory:
        stored = replace(category, id=len(self.categories) + 1)
        self.categories[stored.id or 0] = stored
        return replace(stored)

    async def get_category(self, restaurant_id: int, category_id: int) -> MenuCategory | None:
        found = self.categories.get(category_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_category_by_name(self, restaurant_id: int, name: str) -> MenuCategory | None:
        return next(
            (
                replace(c)
                for c in self.categories.values()
                if c.restaurant_id == restaurant_id and c.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_categories(self, restaurant_id: int) -> list[MenuCategory]:
        return sorted(
            (replace(c) for c in self.categories.values() if c.restaurant_id == restaurant_id),
            key=lambda c: c.position,
        )

    async def save_category(self, category: MenuCategory) -> MenuCategory:
        self.categories[category.id or 0] = replace(category)
        return replace(category)

    async def delete_category(self, category: MenuCategory) -> None:
        self.categories.pop(category.id or 0)

    async def count_items_in_category(self, restaurant_id: int, category_id: int) -> int:
        return len(await self.list_items(restaurant_id, category_id))

    async def add_item(self, item: MenuItem) -> MenuItem:
        stored = replace(item, id=100 + len(self.items))
        self.items[stored.id or 0] = stored
        return replace(stored)

    async def get_item(self, restaurant_id: int, item_id: int) -> MenuItem | None:
        found = self.items.get(item_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_item_by_name(self, restaurant_id: int, name: str) -> MenuItem | None:
        return next(
            (
                replace(i)
                for i in self.items.values()
                if i.restaurant_id == restaurant_id and i.name.casefold() == name.casefold()
            ),
            None,
        )

    async def list_items(
        self, restaurant_id: int, category_id: int | None = None
    ) -> list[MenuItem]:
        return sorted(
            (
                replace(i)
                for i in self.items.values()
                if i.restaurant_id == restaurant_id
                and (category_id is None or i.category_id == category_id)
            ),
            key=lambda i: (i.category_id, i.position),
        )

    async def save_item(self, item: MenuItem) -> MenuItem:
        self.items[item.id or 0] = replace(item)
        return replace(item)


class Bitacora:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


class SinStock:
    def __init__(self, *ids: int) -> None:
        self.ids = frozenset(ids)

    async def out_of_stock(self, restaurant_id: int) -> frozenset[int]:
        return self.ids


@pytest.fixture
def menu() -> MemoryMenu:
    return MemoryMenu()


@pytest.fixture
def bitacora() -> Bitacora:
    return Bitacora()


@pytest.fixture
def avisos() -> RecordingEvents:
    return RecordingEvents()


async def _categoria(menu: MemoryMenu, nombre: str, **extra: object) -> MenuCategory:
    return await menu.add_category(MenuCategory(restaurant_id=LOCAL, name=nombre, **extra))  # type: ignore[arg-type]


async def _plato(menu: MemoryMenu, categoria: int, nombre: str, **extra: object) -> MenuItem:
    return await menu.add_item(
        MenuItem(
            restaurant_id=LOCAL,
            category_id=categoria,
            name=nombre,
            price=Decimal("20.00"),
            **extra,  # type: ignore[arg-type]
        )
    )


# -- Categorías ---------------------------------------------------------------------


async def test_una_categoria_nueva_va_al_final_y_queda_anotada(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    await _categoria(menu, "Entradas")
    await _categoria(menu, "Fondos", position=1)

    creada = await CreateCategory(menu, bitacora, avisos)(
        CreateCategoryCommand(LOCAL, ENCARGADO, "  Bebidas  ")
    )

    assert (creada.name, creada.position) == ("Bebidas", 2)
    assert bitacora.entries == [(LOCAL, ENCARGADO, ActivityKind.MENU_CATEGORY_CREATED, "Bebidas")]
    (aviso,) = avisos.published
    assert (aviso.restaurant_id, aviso.topic, aviso.everyone, aviso.reference_id) == (
        LOCAL,
        MENU_TOPIC,
        True,
        creada.id,
    )


async def test_una_categoria_no_repite_el_nombre_de_otra(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    await _categoria(menu, "Fondos")

    with pytest.raises(CategoryNameTaken):
        await CreateCategory(menu, bitacora, avisos)(
            CreateCategoryCommand(LOCAL, ENCARGADO, "fondos")
        )
    assert bitacora.entries == []
    assert avisos.published == []


async def test_renombrar_una_categoria_sin_tocar_si_esta_activa(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos", is_active=False)

    guardada = await UpdateCategory(menu, bitacora, avisos)(
        UpdateCategoryCommand(LOCAL, ENCARGADO, fondos.id or 0, name=" Segundos ")
    )

    assert guardada.name == "Segundos"
    assert guardada.is_active is False
    assert menu.categories[fondos.id or 0].name == "Segundos"
    assert bitacora.entries[-1][2:] == (ActivityKind.MENU_CATEGORY_UPDATED, "Segundos (inactiva)")
    assert avisos.published[-1].reference_id == fondos.id


async def test_una_categoria_puede_conservar_su_propio_nombre(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")

    guardada = await UpdateCategory(menu, bitacora, avisos)(
        UpdateCategoryCommand(LOCAL, ENCARGADO, fondos.id or 0, name="FONDOS", is_active=True)
    )

    assert guardada.name == "FONDOS"
    assert bitacora.entries[-1][3] == "FONDOS (activa)"


async def test_renombrar_una_categoria_con_el_nombre_de_otra_choca(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    await _categoria(menu, "Fondos")
    bebidas = await _categoria(menu, "Bebidas")

    with pytest.raises(CategoryNameTaken):
        await UpdateCategory(menu, bitacora, avisos)(
            UpdateCategoryCommand(LOCAL, ENCARGADO, bebidas.id or 0, name="Fondos")
        )
    assert menu.categories[bebidas.id or 0].name == "Bebidas"
    assert bitacora.entries == []


async def test_una_categoria_de_otro_local_no_existe(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    ajena = await menu.add_category(MenuCategory(restaurant_id=OTRO_LOCAL, name="Postres"))

    with pytest.raises(CategoryNotFound):
        await UpdateCategory(menu, bitacora, avisos)(
            UpdateCategoryCommand(LOCAL, ENCARGADO, ajena.id or 0, is_active=False)
        )
    with pytest.raises(CategoryNotFound):
        await DeleteCategory(menu, bitacora, avisos)(
            DeleteCategoryCommand(LOCAL, ENCARGADO, ajena.id or 0)
        )
    assert menu.categories[ajena.id or 0].is_active is True


async def test_solo_se_borra_la_categoria_sin_platos(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    vacia = await _categoria(menu, "Temporada")
    await _plato(menu, fondos.id or 0, "Lomo saltado")
    borrar = DeleteCategory(menu, bitacora, avisos)

    with pytest.raises(CategoryNotEmpty):
        await borrar(DeleteCategoryCommand(LOCAL, ENCARGADO, fondos.id or 0))
    await borrar(DeleteCategoryCommand(LOCAL, ENCARGADO, vacia.id or 0))

    assert set(menu.categories) == {fondos.id}
    assert bitacora.entries == [(LOCAL, ENCARGADO, ActivityKind.MENU_CATEGORY_DELETED, "Temporada")]


# -- Platos ---------------------------------------------------------------------


async def test_un_plato_nuevo_va_al_final_de_su_categoria(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    await _plato(menu, fondos.id or 0, "Lomo saltado")

    creado = await CreateMenuItem(menu, bitacora, avisos)(
        CreateMenuItemCommand(
            LOCAL, ENCARGADO, fondos.id or 0, " Ají  de gallina ", Decimal("22"), is_available=False
        )
    )

    assert (creado.name, creado.position, creado.is_available) == ("Ají de gallina", 1, False)
    assert bitacora.entries[-1][2:] == (ActivityKind.MENU_ITEM_CREATED, "Ají de gallina (S/ 22.00)")


async def test_un_plato_no_se_crea_en_una_categoria_ajena_ni_con_nombre_repetido(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    ajena = await menu.add_category(MenuCategory(restaurant_id=OTRO_LOCAL, name="Fondos"))
    await _plato(menu, fondos.id or 0, "Lomo saltado")
    crear = CreateMenuItem(menu, bitacora, avisos)

    with pytest.raises(CategoryNotFound):
        await crear(CreateMenuItemCommand(LOCAL, ENCARGADO, ajena.id or 0, "Seco", Decimal("20")))
    with pytest.raises(MenuItemNameTaken):
        await crear(
            CreateMenuItemCommand(LOCAL, ENCARGADO, fondos.id or 0, "LOMO SALTADO", Decimal("20"))
        )
    assert len(menu.items) == 1


async def test_editar_un_plato_cambia_solo_los_campos_enviados(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado", description="Con papas")
    tamano = ModifierGroup("Tamaño", (ModifierOption("Personal"), ModifierOption("Familiar")))

    guardado = await UpdateMenuItem(menu, bitacora, avisos)(
        UpdateMenuItemCommand(
            LOCAL,
            ENCARGADO,
            lomo.id or 0,
            category_id=fondos.id,
            name="Lomo fino",
            description="  Con arroz ",
            is_available=False,
            modifier_groups=(tamano,),
        )
    )

    assert guardado.name == "Lomo fino"
    assert guardado.description == "Con arroz"
    assert guardado.price == Decimal("20.00")
    assert guardado.is_active is True
    assert guardado.is_available is False
    assert [g.name for g in guardado.modifier_groups] == ["Tamaño"]
    assert guardado.position == 0
    assert bitacora.entries[-1][2:] == (ActivityKind.MENU_ITEM_UPDATED, "Lomo fino (S/ 20.00)")


async def test_mover_un_plato_lo_deja_al_final_de_la_otra_categoria(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    bebidas = await _categoria(menu, "Bebidas")
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado")
    await _plato(menu, bebidas.id or 0, "Chicha morada")
    await _plato(menu, bebidas.id or 0, "Inca Kola", position=1)

    movido = await UpdateMenuItem(menu, bitacora, avisos)(
        UpdateMenuItemCommand(LOCAL, ENCARGADO, lomo.id or 0, category_id=bebidas.id)
    )

    assert (movido.category_id, movido.position) == (bebidas.id, 2)


async def test_editar_un_plato_con_nombre_ajeno_o_mal_escrito_no_guarda(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado")
    await _plato(menu, fondos.id or 0, "Seco de res")
    editar = UpdateMenuItem(menu, bitacora, avisos)

    with pytest.raises(MenuItemNameTaken):
        await editar(UpdateMenuItemCommand(LOCAL, ENCARGADO, lomo.id or 0, name="seco de res"))
    with pytest.raises(InvalidMenuItem):
        await editar(UpdateMenuItemCommand(LOCAL, ENCARGADO, lomo.id or 0, name="  "))
    with pytest.raises(CategoryNotFound):
        await editar(UpdateMenuItemCommand(LOCAL, ENCARGADO, lomo.id or 0, category_id=999))
    with pytest.raises(MenuItemNotFound):
        await editar(UpdateMenuItemCommand(OTRO_LOCAL, ENCARGADO, lomo.id or 0, name="Otro"))
    assert menu.items[lomo.id or 0].name == "Lomo saltado"
    assert bitacora.entries == []


async def test_marcar_agotado_dos_veces_no_duplica_la_bitacora(
    menu: MemoryMenu, bitacora: Bitacora, avisos: RecordingEvents
) -> None:
    fondos = await _categoria(menu, "Fondos")
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado")
    cambiar = SetAvailability(menu, bitacora, avisos)

    await cambiar(SetAvailabilityCommand(LOCAL, ENCARGADO, lomo.id or 0, is_available=False))
    repetido = await cambiar(
        SetAvailabilityCommand(LOCAL, ENCARGADO, lomo.id or 0, is_available=False)
    )
    await cambiar(SetAvailabilityCommand(LOCAL, ENCARGADO, lomo.id or 0, is_available=True))

    assert repetido.is_available is False
    assert [detalle for *_, detalle in bitacora.entries] == [
        "Lomo saltado: agotado",
        "Lomo saltado: disponible",
    ]
    assert len(avisos.published) == 2


# -- Lectura --------------------------------------------------------------------------


async def test_la_carta_marca_agotado_lo_que_el_stock_no_alcanza(menu: MemoryMenu) -> None:
    fondos = await _categoria(menu, "Fondos")
    retirada = await _categoria(menu, "Temporada", is_active=False)
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado")
    seco = await _plato(menu, fondos.id or 0, "Seco de res", position=1)
    await _plato(menu, fondos.id or 0, "Ceviche", position=2, is_active=False)
    await _plato(menu, retirada.id or 0, "Causa")

    mesero = await ReadMenu(menu, SinStock(seco.id or 0))(ReadMenuQuery(LOCAL))
    encargado = await ReadMenu(menu)(ReadMenuQuery(LOCAL, include_inactive=True))

    (seccion,) = mesero
    assert seccion.category.id == fondos.id
    assert [(p.id, p.out_of_stock, p.can_be_ordered) for p in seccion.items] == [
        (lomo.id, False, True),
        (seco.id, True, False),
    ]
    assert [len(s.items) for s in encargado] == [3, 1]
    assert not any(p.out_of_stock for s in encargado for p in s.items)


async def test_una_categoria_sin_platos_aparece_vacia(menu: MemoryMenu) -> None:
    await _categoria(menu, "Postres")

    (seccion,) = await ReadMenu(menu)(ReadMenuQuery(LOCAL))

    assert seccion.items == []


async def test_un_plato_retirado_solo_lo_ve_quien_administra(menu: MemoryMenu) -> None:
    fondos = await _categoria(menu, "Fondos")
    lomo = await _plato(menu, fondos.id or 0, "Lomo saltado")
    ceviche = await _plato(menu, fondos.id or 0, "Ceviche", is_active=False)
    leer = ReadMenuItem(menu)

    assert (await leer(LOCAL, lomo.id or 0)).name == "Lomo saltado"
    assert (await leer(LOCAL, ceviche.id or 0, include_inactive=True)).name == "Ceviche"
    with pytest.raises(MenuItemNotFound):
        await leer(LOCAL, ceviche.id or 0)
    with pytest.raises(MenuItemNotFound):
        await leer(OTRO_LOCAL, lomo.id or 0, include_inactive=True)
