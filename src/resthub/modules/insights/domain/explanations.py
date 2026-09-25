"""Explicaciones en castellano, escritas por el código a partir de los números.

Python puro. Jev no escribe texto: elige una opción y dice con cuánta
confianza. La frase que lee el encargado la arma este módulo con los mismos
números que vio el motor, así nunca dice algo que los datos no respalden.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from resthub.modules.insights.domain.decisions import (
    Engine,
    FallbackReason,
    RestockAction,
    RestockOutcome,
    Verdict,
)
from resthub.modules.insights.domain.stock import RestockFacts, UsageTrend

_THOUSAND = Decimal("1000")
_BIG_UNITS = {"g": "kg", "ml": "L"}


def _decimal(value: Decimal, places: str = "0.1") -> str:
    rounded = value.quantize(Decimal(places), ROUND_HALF_UP).normalize()
    # `normalize` puede dejar notación científica ("1E+3"); `:f` la evita.
    return f"{rounded:f}".replace(".", ",")


def amount(value: Decimal, unit: str) -> str:
    """Una cantidad legible: 5600 g se lee "5,6 kg"; 3 unidades, "3 unidades"."""
    if unit in _BIG_UNITS and abs(value) >= _THOUSAND:
        # Dos decimales: con uno, 1960 g se leería "2 kg" justo al lado de un
        # mínimo de 2 kg, y no se entendería por qué está por debajo.
        return f"{_decimal(value / _THOUSAND, '0.01')} {_BIG_UNITS[unit]}"
    if unit == "unit":
        text = _decimal(value)
        return f"{text} unidad" if value == 1 else f"{text} unidades"
    return f"{_decimal(value, '1')} {unit}"


def days(value: Decimal) -> str:
    return f"{_decimal(value)} día" if value == 1 else f"{_decimal(value)} días"


def _stock_sentence(facts: RestockFacts) -> str:
    if facts.stock <= 0:
        return f"No queda {facts.name} en el almacén."
    sentence = f"Quedan {amount(facts.stock, facts.unit)} de {facts.name}"
    if facts.below_minimum:
        sentence += f", por debajo del mínimo de {amount(facts.min_stock, facts.unit)}"
    return sentence + "."


def _use_sentence(facts: RestockFacts) -> str:
    use = facts.reference_daily_use
    if use <= 0:
        return "No se ha usado en las últimas cuatro semanas."
    coverage = facts.coverage_days
    sentence = f"Se usan unos {amount(use, facts.unit)} por día"
    if coverage is not None and facts.stock > 0:
        sentence += f", así que alcanza para {days(coverage)}"
    return sentence + "."


def _trend_sentence(facts: RestockFacts) -> str:
    change = facts.usage_change_percent
    if facts.trend is UsageTrend.RISING and change is not None:
        return f"El uso de esta semana va {_decimal(abs(change), '1')} % por encima del mes."
    if facts.trend is UsageTrend.FALLING and change is not None:
        return f"El uso de esta semana va {_decimal(abs(change), '1')} % por debajo del mes."
    return ""


def _waste_sentence(facts: RestockFacts) -> str:
    if facts.wasted_long <= 0:
        return ""
    share = _decimal(facts.waste_share * 100, "1")
    return (
        f"En cuatro semanas se perdieron {amount(facts.wasted_long, facts.unit)} por merma, "
        f"el {share} % de lo que salió del almacén."
    )


_ACTION_SENTENCES: dict[RestockAction, str] = {
    RestockAction.BUY_TODAY: "Conviene comprarlo hoy.",
    RestockAction.BUY_THIS_WEEK: "Conviene comprarlo en los próximos días.",
    RestockAction.WAIT: "Por ahora no hace falta comprar.",
    RestockAction.REVIEW_WASTE: "Antes de comprar más, revisa cómo se guarda y se manipula.",
}


def _engine_sentence(verdict: Verdict[RestockOutcome]) -> str:
    if verdict.engine is Engine.JEV and verdict.confidence is not None:
        confidence = _decimal(Decimal(str(verdict.confidence)) * 100, "1")
        return f"Lo sugiere Jev con una confianza de {confidence} %."
    if verdict.fallback is FallbackReason.LOW_CONFIDENCE:
        return "Jev no estaba seguro, así que decidieron las reglas fijas: revísalo a mano."
    if verdict.fallback is FallbackReason.UNAVAILABLE:
        return "Jev no respondió a tiempo; decidieron las reglas fijas."
    return "Lo deciden las reglas fijas."


def explain_restock(facts: RestockFacts, verdict: Verdict[RestockOutcome]) -> str:
    parts = [
        _stock_sentence(facts),
        _use_sentence(facts),
        _trend_sentence(facts),
        _waste_sentence(facts),
        _ACTION_SENTENCES[verdict.outcome.action],
        _engine_sentence(verdict),
    ]
    return " ".join(part for part in parts if part)
