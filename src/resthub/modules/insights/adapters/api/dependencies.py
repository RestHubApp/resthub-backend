"""Cableado del adaptador HTTP de los indicadores."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import PrincipalDep
from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.modules.insights.adapters.ai.jev_engine import JevDecisionEngine
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.persistence.directories import (
    SqlAlchemyAiPreferencesReader,
    SqlAlchemyKitchenNotesDirectory,
    SqlAlchemyRestaurantCalendarDirectory,
    SqlAlchemySalesDirectory,
    SqlAlchemyStockDirectory,
    SqlAlchemySubjectDirectory,
)
from resthub.modules.insights.adapters.persistence.sqlalchemy_decision_log import (
    SqlAlchemyDecisionLogRepository,
)
from resthub.modules.insights.domain.decisions import FallbackReason
from resthub.modules.insights.ports.ai_preferences import AiPreferencesReader
from resthub.modules.insights.ports.decision_engine import DecisionEngineGateway
from resthub.modules.insights.ports.decision_log import DecisionLogRepository
from resthub.modules.insights.ports.kitchen_notes import KitchenNotesDirectory
from resthub.modules.insights.ports.restaurant_calendar import RestaurantCalendarDirectory
from resthub.modules.insights.ports.sales_directory import SalesDirectory
from resthub.modules.insights.ports.stock_directory import StockDirectory
from resthub.modules.insights.ports.subject_directory import SubjectDirectory


def get_restaurant_calendar(session: SessionDep) -> RestaurantCalendarDirectory:
    return SqlAlchemyRestaurantCalendarDirectory(session)


def get_sales_directory(session: SessionDep) -> SalesDirectory:
    return SqlAlchemySalesDirectory(session)


def get_stock_directory(session: SessionDep) -> StockDirectory:
    return SqlAlchemyStockDirectory(session)


def get_kitchen_notes_directory(session: SessionDep) -> KitchenNotesDirectory:
    return SqlAlchemyKitchenNotesDirectory(session)


def get_subject_directory(session: SessionDep) -> SubjectDirectory:
    return SqlAlchemySubjectDirectory(session)


def get_decision_log(session: SessionDep) -> DecisionLogRepository:
    return SqlAlchemyDecisionLogRepository(session)


async def get_decision_engine(
    principal: PrincipalDep, session: SessionDep
) -> DecisionEngineGateway:
    """Jev si hay clave y el local lo permite, siempre con las reglas detrás.

    Sin clave no se crea el cliente de Jev: el selector decide todo con las
    reglas y anota que la IA no está configurada. Si el local apagó la IA
    externa, igual, y anota eso: sus datos no salen del sistema.
    """
    settings = get_settings()
    preferences: AiPreferencesReader = SqlAlchemyAiPreferencesReader(session)
    if not await preferences.allows_external_ai(principal.restaurant_id):
        return DecisionEngineSelector(
            rules=RuleBasedDecisionEngine(),
            jev=None,
            min_confidence=settings.ai_min_confidence,
            without_jev=FallbackReason.DISABLED,
        )
    api_key = settings.typesafe_api_key.strip()
    jev = (
        JevDecisionEngine(
            api_key=api_key,
            base_url=settings.typesafe_base_url,
            model=settings.typesafe_model,
            timeout_seconds=settings.typesafe_timeout_seconds,
        )
        if api_key
        else None
    )
    return DecisionEngineSelector(
        rules=RuleBasedDecisionEngine(), jev=jev, min_confidence=settings.ai_min_confidence
    )


RestaurantCalendarDep = Annotated[RestaurantCalendarDirectory, Depends(get_restaurant_calendar)]
SalesDirectoryDep = Annotated[SalesDirectory, Depends(get_sales_directory)]
StockDirectoryDep = Annotated[StockDirectory, Depends(get_stock_directory)]
KitchenNotesDirectoryDep = Annotated[KitchenNotesDirectory, Depends(get_kitchen_notes_directory)]
SubjectDirectoryDep = Annotated[SubjectDirectory, Depends(get_subject_directory)]
DecisionLogDep = Annotated[DecisionLogRepository, Depends(get_decision_log)]
DecisionEngineDep = Annotated[DecisionEngineGateway, Depends(get_decision_engine)]
