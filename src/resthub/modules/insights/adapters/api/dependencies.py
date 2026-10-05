"""Cableado del adaptador HTTP de los indicadores."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import PrincipalDep, SessionDep
from resthub.core.config import get_settings
from resthub.modules.insights.adapters.ai.jev_engine import JevDecisionEngine
from resthub.modules.insights.adapters.ai.rule_based_engine import RuleBasedDecisionEngine
from resthub.modules.insights.adapters.ai.selector import DecisionEngineSelector
from resthub.modules.insights.adapters.persistence.directories import (
    SqlAiPreferences,
    SqlKitchenNotesDirectory,
    SqlRestaurantCalendar,
    SqlSalesDirectory,
    SqlStockDirectory,
    SqlSubjectDirectory,
)
from resthub.modules.insights.adapters.persistence.sqlalchemy_decision_log import (
    SqlAlchemyDecisionLog,
)
from resthub.modules.insights.domain.decisions import FallbackReason
from resthub.modules.insights.ports.decision_engine import DecisionEngine
from resthub.modules.insights.ports.decision_log import DecisionLog
from resthub.modules.insights.ports.kitchen_notes import KitchenNotesDirectory
from resthub.modules.insights.ports.restaurant_calendar import RestaurantCalendar
from resthub.modules.insights.ports.sales_directory import SalesDirectory
from resthub.modules.insights.ports.stock_directory import StockDirectory
from resthub.modules.insights.ports.subject_directory import SubjectDirectory


def get_restaurant_calendar(session: SessionDep) -> RestaurantCalendar:
    return SqlRestaurantCalendar(session)


def get_sales_directory(session: SessionDep) -> SalesDirectory:
    return SqlSalesDirectory(session)


def get_stock_directory(session: SessionDep) -> StockDirectory:
    return SqlStockDirectory(session)


def get_kitchen_notes_directory(session: SessionDep) -> KitchenNotesDirectory:
    return SqlKitchenNotesDirectory(session)


def get_subject_directory(session: SessionDep) -> SubjectDirectory:
    return SqlSubjectDirectory(session)


def get_decision_log(session: SessionDep) -> DecisionLog:
    return SqlAlchemyDecisionLog(session)


async def get_decision_engine(principal: PrincipalDep, session: SessionDep) -> DecisionEngine:
    """Jev si hay clave y el local lo permite, siempre con las reglas detrás.

    Sin clave no se crea el cliente de Jev: el selector decide todo con las
    reglas y anota que la IA no está configurada. Si el local apagó la IA
    externa, igual, y anota eso: sus datos no salen del sistema.
    """
    settings = get_settings()
    if not await SqlAiPreferences(session).allows_external_ai(principal.restaurant_id):
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


RestaurantCalendarDep = Annotated[RestaurantCalendar, Depends(get_restaurant_calendar)]
SalesDirectoryDep = Annotated[SalesDirectory, Depends(get_sales_directory)]
StockDirectoryDep = Annotated[StockDirectory, Depends(get_stock_directory)]
KitchenNotesDirectoryDep = Annotated[KitchenNotesDirectory, Depends(get_kitchen_notes_directory)]
SubjectDirectoryDep = Annotated[SubjectDirectory, Depends(get_subject_directory)]
DecisionLogDep = Annotated[DecisionLog, Depends(get_decision_log)]
DecisionEngineDep = Annotated[DecisionEngine, Depends(get_decision_engine)]
