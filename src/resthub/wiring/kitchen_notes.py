"""Platos que llegan a cocina → clasificación de sus notas.

`orders` declara el puerto `SentToKitchenHook` sin saber quién lo escucha, e
`insights` expone el caso de uso `ClassifyKitchenNotes` sin saber qué es un
pedido. Este adaptador traduce uno al otro y `main.py` lo instala en lugar de la
dependencia por omisión de `orders`, que no hace nada.

No bloquea al mesero. Clasificar puede tardar lo que tarde Jev, y el pedido ya
tiene que verse en cocina, así que el trabajo corre aparte:

1. Al recibir el aviso, solo toma nota de qué notas hay que mirar.
2. Cuando la transacción del envío se confirma, lanza una tarea en segundo
   plano (`core/background.py`). Si el envío se deshace, no lanza nada: no
   tiene sentido clasificar las notas de un pedido que no llegó a cocina.
3. La tarea abre su propia sesión, clasifica, guarda y confirma. Al confirmar
   sale el aviso SSE `insights` con el id del pedido, y el tablero vuelve a
   pedir sus notas.

Es idempotente: una nota cuyo texto ya se clasificó no se vuelve a mandar.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from resthub.core.auth import SessionDep
from resthub.core.background import BackgroundJobs, get_background_jobs
from resthub.core.database import get_session_factory
from resthub.core.logs import get_logger
from resthub.core.realtime_broker import BrokerDep, LocalBroker, SessionEventPublisher
from resthub.modules.insights.adapters.api.dependencies import DecisionEngineDep
from resthub.modules.insights.adapters.persistence.sqlalchemy_decision_log import (
    SqlAlchemyDecisionLog,
)
from resthub.modules.insights.domain.decisions import KitchenNote, SubjectType
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.insights.use_cases.order_notes import ClassifyKitchenNotes
from resthub.modules.orders.ports.sent_to_kitchen_hook import SentOrder, SentToKitchenHook

logger = get_logger("resthub.insights")

SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
BackgroundJobsDep = Annotated[BackgroundJobs, Depends(get_background_jobs)]


def kitchen_notes(sent: SentOrder) -> list[KitchenNote]:
    """Las notas escritas del pedido: la general y la de cada plato."""
    notes: list[KitchenNote] = []
    if sent.notes.strip():
        notes.append(
            KitchenNote(
                subject_type=SubjectType.ORDER,
                subject_id=sent.order_id,
                order_id=sent.order_id,
                text=sent.notes.strip(),
            )
        )
    notes.extend(
        KitchenNote(
            subject_type=SubjectType.ORDER_ITEM,
            subject_id=item.order_item_id,
            order_id=sent.order_id,
            text=item.notes.strip(),
            dish_name=item.name,
        )
        for item in sent.items
        if item.notes.strip()
    )
    return notes


async def classify_in_background(
    factory: async_sessionmaker[AsyncSession],
    engine: DecisionEngine,
    broker: LocalBroker,
    restaurant_id: int,
    notes: list[KitchenNote],
) -> None:
    async with factory() as session:
        run = await ClassifyKitchenNotes(
            SqlAlchemyDecisionLog(session), engine, SessionEventPublisher(session, broker)
        )(restaurant_id, notes)
        await session.commit()
    allergies = sum(1 for view in run.classified if view.outcome and view.outcome.mentions_allergy)
    logger.info(
        "insights.kitchen_notes_classified",
        restaurant_id=restaurant_id,
        order_ids=sorted({note.order_id for note in notes}),
        classified=len(run.classified),
        already_classified=run.already_classified,
        allergies=allergies,
    )


class KitchenNoteClassification:
    """Implementa el puerto de `orders` con el caso de uso de `insights`."""

    def __init__(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        jobs: BackgroundJobs,
        engine: DecisionEngine,
        broker: LocalBroker,
    ) -> None:
        self._session = session
        self._factory = factory
        self._jobs = jobs
        self._engine = engine
        self._broker = broker

    def order_sent(self, sent: SentOrder) -> None:
        notes = kitchen_notes(sent)
        if not notes:
            return

        factory, engine, broker, jobs = self._factory, self._engine, self._broker, self._jobs

        def launch(_: Session) -> None:
            jobs.spawn(
                f"notas-pedido-{sent.order_id}",
                lambda: classify_in_background(factory, engine, broker, sent.restaurant_id, notes),
            )

        # `once`: la sesión es de esta petición y se confirma una sola vez.
        # Si se deshace, el oyente nunca se dispara y se va con la sesión.
        sqlalchemy_event.listen(self._session.sync_session, "after_commit", launch, once=True)


def get_kitchen_note_classification(
    session: SessionDep,
    factory: SessionFactoryDep,
    jobs: BackgroundJobsDep,
    engine: DecisionEngineDep,
    broker: BrokerDep,
) -> SentToKitchenHook:
    return KitchenNoteClassification(session, factory, jobs, engine, broker)
