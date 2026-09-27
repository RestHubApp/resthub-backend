"""Reglas de las reservas sin HTTP: validación, ciclo, cruce de horarios y casos de uso."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from resthub.core.activity import ActivityKind
from resthub.modules.reservations.domain.exceptions import (
    CustomerNotFound,
    InvalidReservation,
    ReservationConflict,
    ReservationNotFound,
    TableNotFound,
)
from resthub.modules.reservations.domain.reservations import (
    GRACE,
    MAX_DURATION,
    MAX_PARTY_SIZE,
    MIN_DURATION,
    Reservation,
    ReservationStatus,
    ensure_table_free,
)
from resthub.modules.reservations.use_cases.manage_reservations import (
    ChangeReservationStatus,
    ListReservationsOfDay,
    ReservationData,
    SaveReservation,
)

LOCAL = 1
MESERO = 5
ENCARGADO = 6
NOCHE = datetime(2030, 3, 10, 20, 0, tzinfo=UTC)


def _reserva(**datos: object) -> Reservation:
    campos: dict[str, object] = {
        "restaurant_id": LOCAL,
        "customer_name": "Ana Torres",
        "party_size": 4,
        "reserved_for": NOCHE,
        "created_by": MESERO,
    }
    campos.update(datos)
    return Reservation(**campos)  # type: ignore[arg-type]


# -- Dominio ------------------------------------------------------------------------


def test_los_estados_tienen_rotulo_y_solo_dos_ocupan_la_mesa() -> None:
    assert [s.label for s in ReservationStatus] == [
        "Reservada",
        "Llegaron",
        "Cancelada",
        "No vinieron",
    ]
    assert [s for s in ReservationStatus if s.holds_table] == [
        ReservationStatus.BOOKED,
        ReservationStatus.SEATED,
    ]


def test_la_reserva_limpia_sus_textos_y_dura_dos_horas_por_omision() -> None:
    reserva = _reserva(customer_name="  Ana   Torres ", phone=" 987 654 ", notes=" cumple  ")

    assert (reserva.customer_name, reserva.phone, reserva.notes) == (
        "Ana Torres",
        "987 654",
        "cumple",
    )
    assert reserva.ends_at == NOCHE + timedelta(minutes=120)


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        ({"customer_name": "  "}, "La reserva necesita el nombre de quien reserva."),
        ({"customer_name": "N" * 81}, "El nombre admite 80 caracteres como máximo."),
        ({"phone": "9" * 21}, "El teléfono admite 20 caracteres como máximo."),
        ({"notes": "N" * 301}, "La nota admite 300 caracteres como máximo."),
        ({"party_size": 0}, "Una reserva es de 1 a 50 personas."),
        ({"party_size": MAX_PARTY_SIZE + 1}, "Una reserva es de 1 a 50 personas."),
        ({"duration_minutes": MIN_DURATION - 1}, "La mesa se reserva de 30 a 360 minutos."),
        ({"duration_minutes": MAX_DURATION + 1}, "La mesa se reserva de 30 a 360 minutos."),
        (
            {"reserved_for": datetime(2030, 3, 10, 20, 0)},
            "La hora de la reserva necesita su zona horaria.",
        ),
    ],
)
def test_una_reserva_invalida_explica_el_motivo(datos: dict[str, object], mensaje: str) -> None:
    with pytest.raises(InvalidReservation) as error:
        _reserva(**datos)
    assert error.value.reason == mensaje


def test_los_limites_exactos_se_aceptan() -> None:
    assert _reserva(party_size=1, duration_minutes=MIN_DURATION).ends_at == NOCHE + timedelta(
        minutes=30
    )
    assert _reserva(party_size=MAX_PARTY_SIZE, duration_minutes=MAX_DURATION).party_size == 50
    assert _reserva(customer_name="N" * 80, phone="9" * 20, notes="N" * 300).phone == "9" * 20


def test_se_acepta_con_quince_minutos_de_atraso_y_no_mas() -> None:
    reserva = _reserva()

    reserva.ensure_future(NOCHE + GRACE)
    with pytest.raises(InvalidReservation) as error:
        reserva.ensure_future(NOCHE + GRACE + timedelta(seconds=1))
    assert error.value.reason == "La hora de la reserva ya pasó."


def test_dos_horarios_se_cruzan_solo_si_se_pisan() -> None:
    reserva = _reserva()
    justo_despues = _reserva(reserved_for=reserva.ends_at)
    justo_antes = _reserva(reserved_for=NOCHE - timedelta(minutes=120))
    pisando = _reserva(reserved_for=reserva.ends_at - timedelta(minutes=1))

    assert not reserva.overlaps(justo_despues)
    assert not justo_despues.overlaps(reserva)
    assert not reserva.overlaps(justo_antes)
    assert reserva.overlaps(pisando)
    assert pisando.overlaps(reserva)


@pytest.mark.parametrize(
    "final", [ReservationStatus.SEATED, ReservationStatus.CANCELLED, ReservationStatus.NO_SHOW]
)
def test_una_reserva_cerrada_ya_no_cambia_ni_se_edita(final: ReservationStatus) -> None:
    reserva = _reserva()
    reserva.change_status(final)

    with pytest.raises(InvalidReservation) as cambio:
        reserva.change_status(ReservationStatus.CANCELLED)
    with pytest.raises(InvalidReservation) as edicion:
        reserva.ensure_editable()
    assert cambio.value.reason == f"Una reserva «{final.label}» ya no cambia."
    assert edicion.value.reason == f"Una reserva «{final.label}» ya no se edita."
    assert reserva.status is final


def test_una_reserva_no_vuelve_a_reservada() -> None:
    reserva = _reserva()

    with pytest.raises(InvalidReservation) as error:
        reserva.change_status(ReservationStatus.BOOKED)
    assert error.value.reason == "La reserva ya está reservada."
    reserva.ensure_editable()


def test_la_mesa_libre_ignora_su_propia_reserva_otras_mesas_y_las_cerradas() -> None:
    candidata = _reserva(id=1, table_id=3)
    misma = _reserva(id=1, table_id=3)
    otra_mesa = _reserva(id=2, table_id=4)
    cancelada = _reserva(id=3, table_id=3, status=ReservationStatus.CANCELLED)
    no_vino = _reserva(id=4, table_id=3, status=ReservationStatus.NO_SHOW)
    mas_tarde = _reserva(id=5, table_id=3, reserved_for=candidata.ends_at)

    ensure_table_free(candidata, [misma, otra_mesa, cancelada, no_vino, mas_tarde])


@pytest.mark.parametrize("estado", [ReservationStatus.BOOKED, ReservationStatus.SEATED])
def test_una_reserva_vigente_de_la_misma_mesa_choca(estado: ReservationStatus) -> None:
    candidata = _reserva(table_id=3)
    vigente = _reserva(id=9, table_id=3, customer_name="Luis", status=estado)

    with pytest.raises(ReservationConflict) as error:
        ensure_table_free(candidata, [vigente])
    assert str(error.value) == "Esa mesa ya está reservada para Luis en ese horario."


def test_sin_mesa_asignada_no_hay_cruce_posible() -> None:
    ensure_table_free(_reserva(), [_reserva(id=9, table_id=3), _reserva(id=10)])


# -- Casos de uso con dobles en memoria --------------------------------------------------


class Agenda:
    def __init__(self) -> None:
        self.rows: dict[int, Reservation] = {}
        self.bloqueos: list[int] = []
        self.ventanas: list[tuple[datetime, datetime]] = []

    async def add(self, reservation: Reservation) -> Reservation:
        stored = replace(reservation, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(
        self, restaurant_id: int, reservation_id: int, *, for_update: bool = False
    ) -> Reservation | None:
        if for_update:
            self.bloqueos.append(reservation_id)
        found = self.rows.get(reservation_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def save(self, reservation: Reservation) -> Reservation:
        self.rows[reservation.id or 0] = replace(reservation)
        return replace(reservation)

    async def between(
        self, restaurant_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        self.ventanas.append((start, end))
        return [
            replace(r)
            for r in self.rows.values()
            if r.restaurant_id == restaurant_id and start <= r.reserved_for < end
        ]

    async def for_table(
        self, restaurant_id: int, table_id: int, start: datetime, end: datetime
    ) -> list[Reservation]:
        self.ventanas.append((start, end))
        return [
            replace(r)
            for r in self.rows.values()
            if r.restaurant_id == restaurant_id and r.table_id == table_id
        ]


class Calendario:
    def __init__(self, zona: str = "America/Lima", mesas: dict[int, str] | None = None) -> None:
        self.zona = zona
        self.mesas = mesas or {3: "Terraza 3"}

    async def timezone(self, restaurant_id: int) -> str:
        return self.zona

    async def table_label(self, restaurant_id: int, table_id: int) -> str | None:
        return self.mesas.get(table_id)


class Clientes:
    def __init__(self, *ids: int) -> None:
        self.ids = set(ids)

    async def exists(self, restaurant_id: int, customer_id: int) -> bool:
        return customer_id in self.ids


class Bitacora:
    def __init__(self) -> None:
        self.entries: list[tuple[int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((user_id, kind, detail))


def _guardar(agenda: Agenda, bitacora: Bitacora, *clientes: int) -> SaveReservation:
    return SaveReservation(agenda, Calendario(), Clientes(*clientes), bitacora)


async def test_tomar_una_reserva_con_mesa_la_anota_con_el_nombre_de_la_mesa() -> None:
    agenda, bitacora = Agenda(), Bitacora()

    guardada = await _guardar(agenda, bitacora)(
        LOCAL, MESERO, ReservationData("Ana", 4, NOCHE, table_id=3)
    )

    assert guardada.id == 1
    assert guardada.created_by == MESERO
    assert bitacora.entries == [
        (MESERO, ActivityKind.RESERVATION_CREATED, "Ana, 4 personas en Terraza 3")
    ]
    assert agenda.ventanas == [(NOCHE - timedelta(hours=6), NOCHE + timedelta(hours=2))]


async def test_una_reserva_sin_mesa_no_nombra_mesa_en_la_bitacora() -> None:
    agenda, bitacora = Agenda(), Bitacora()

    await _guardar(agenda, bitacora)(LOCAL, MESERO, ReservationData("Ana", 2, NOCHE))

    assert bitacora.entries[0][2] == "Ana, 2 personas"
    assert agenda.ventanas == []


async def test_editar_conserva_quien_la_tomo_y_cuando() -> None:
    agenda, bitacora = Agenda(), Bitacora()
    guardar = _guardar(agenda, bitacora, 7)
    alta = await guardar(LOCAL, MESERO, ReservationData("Ana", 4, NOCHE, table_id=3))

    editada = await guardar(
        LOCAL, ENCARGADO, ReservationData("Ana", 6, NOCHE, table_id=3, customer_id=7), alta.id
    )

    assert editada.id == alta.id
    assert editada.party_size == 6
    assert editada.created_by == MESERO
    assert editada.created_at == alta.created_at
    assert agenda.bloqueos == [alta.id]
    assert bitacora.entries[-1] == (
        ENCARGADO,
        ActivityKind.RESERVATION_UPDATED,
        "Ana, 6 personas en Terraza 3",
    )


async def test_la_hora_se_guarda_en_utc() -> None:
    agenda, bitacora = Agenda(), Bitacora()
    lima = datetime.fromisoformat("2030-03-10T15:00:00-05:00")

    guardada = await _guardar(agenda, bitacora)(LOCAL, MESERO, ReservationData("Ana", 2, lima))

    assert guardada.reserved_for == NOCHE
    assert guardada.reserved_for.tzinfo is UTC


async def test_no_se_edita_una_reserva_cerrada_ni_una_ajena() -> None:
    agenda, bitacora = Agenda(), Bitacora()
    guardar = _guardar(agenda, bitacora)
    alta = await guardar(LOCAL, MESERO, ReservationData("Ana", 4, NOCHE))
    await ChangeReservationStatus(agenda, bitacora)(
        LOCAL, MESERO, alta.id or 0, ReservationStatus.SEATED
    )

    with pytest.raises(InvalidReservation):
        await guardar(LOCAL, MESERO, ReservationData("Ana", 8, NOCHE), alta.id)
    with pytest.raises(ReservationNotFound):
        await guardar(2, MESERO, ReservationData("Ana", 8, NOCHE), alta.id)
    assert agenda.rows[alta.id or 0].party_size == 4


async def test_un_cliente_o_una_mesa_que_no_existen_en_el_local_se_rechazan() -> None:
    agenda, bitacora = Agenda(), Bitacora()
    guardar = _guardar(agenda, bitacora, 7)

    with pytest.raises(CustomerNotFound):
        await guardar(LOCAL, MESERO, ReservationData("Ana", 4, NOCHE, customer_id=8))
    with pytest.raises(TableNotFound):
        await guardar(LOCAL, MESERO, ReservationData("Ana", 4, NOCHE, table_id=99))
    assert agenda.rows == {}
    assert bitacora.entries == []


async def test_una_reserva_en_el_pasado_no_se_toma() -> None:
    agenda, bitacora = Agenda(), Bitacora()

    with pytest.raises(InvalidReservation):
        await _guardar(agenda, bitacora)(
            LOCAL, MESERO, ReservationData("Ana", 4, datetime(2020, 1, 1, tzinfo=UTC))
        )


async def test_cerrar_una_reserva_la_toma_con_bloqueo_y_la_anota() -> None:
    agenda, bitacora = Agenda(), Bitacora()
    alta = await _guardar(agenda, bitacora)(LOCAL, MESERO, ReservationData("Ana", 4, NOCHE))

    cerrada = await ChangeReservationStatus(agenda, bitacora)(
        LOCAL, ENCARGADO, alta.id or 0, ReservationStatus.NO_SHOW
    )

    assert cerrada.status is ReservationStatus.NO_SHOW
    assert agenda.rows[alta.id or 0].status is ReservationStatus.NO_SHOW
    assert agenda.bloqueos == [alta.id]
    assert bitacora.entries[-1] == (ENCARGADO, ActivityKind.RESERVATION_UPDATED, "Ana: no vinieron")


async def test_el_dia_se_cuenta_en_la_zona_del_local() -> None:
    agenda = Agenda()

    await ListReservationsOfDay(agenda, Calendario("America/Lima"))(LOCAL, date(2030, 3, 10))

    assert agenda.ventanas == [
        (datetime(2030, 3, 10, 5, 0, tzinfo=UTC), datetime(2030, 3, 11, 5, 0, tzinfo=UTC))
    ]
