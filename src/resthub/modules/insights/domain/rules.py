"""Reglas fijas: las mismas decisiones que Jev, sin IA.

Python puro. Se usan cuando no hay clave de TypeSafe, cuando Jev no responde a
tiempo y cuando responde con poca confianza. Son deliberadamente simples y
conservadoras: ante la duda compran antes y marcan la alergia, porque quedarse
sin lomo un sábado o servirle maní a un alérgico cuesta más que un falso aviso.

Las notas y las mermas se escriben en castellano peruano, así que las reglas
buscan palabras clave en castellano, sin distinguir tildes ni mayúsculas.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

from resthub.modules.insights.domain.decisions import (
    NoteOutcome,
    NoteType,
    RestockAction,
    RestockOutcome,
    Urgency,
    WasteCause,
    WasteOutcome,
)
from resthub.modules.insights.domain.stock import RestockFacts

# -- Reposición --------------------------------------------------------------

# Si una de cada siete unidades que salen del almacén va a la basura, comprar
# más no es la respuesta: primero hay que ver qué pasa con ese insumo.
WASTE_SHARE_LIMIT = Decimal("0.15")
# Días de cobertura que separan una acción de otra.
CRITICAL_DAYS = Decimal("1")
TODAY_DAYS = Decimal("2")
WEEK_DAYS = Decimal("7")


def urgency_for(facts: RestockFacts) -> Urgency:
    coverage = facts.coverage_days
    if facts.stock <= 0 or (coverage is not None and coverage < CRITICAL_DAYS):
        return Urgency.CRITICAL
    if coverage is not None and coverage < TODAY_DAYS:
        return Urgency.HIGH
    if facts.below_minimum or (coverage is not None and coverage < WEEK_DAYS):
        return Urgency.MEDIUM
    return Urgency.LOW


def restock_by_rules(facts: RestockFacts) -> tuple[RestockOutcome, dict[str, Any]]:
    """La acción para un insumo y el nombre de la regla que la decidió."""
    urgency = urgency_for(facts)
    coverage = facts.coverage_days

    if facts.stock <= 0:
        action, rule = RestockAction.BUY_TODAY, "sin_stock"
    elif facts.wasted_long > 0 and facts.waste_share >= WASTE_SHARE_LIMIT:
        action, rule = RestockAction.REVIEW_WASTE, "merma_alta"
    elif coverage is not None and coverage < TODAY_DAYS:
        action, rule = RestockAction.BUY_TODAY, "cobertura_menor_a_2_dias"
    elif coverage is not None and coverage < WEEK_DAYS:
        action, rule = RestockAction.BUY_THIS_WEEK, "cobertura_menor_a_7_dias"
    elif facts.below_minimum:
        action, rule = RestockAction.BUY_THIS_WEEK, "bajo_el_minimo"
    else:
        action, rule = RestockAction.WAIT, "cobertura_suficiente"

    return (
        RestockOutcome(action=action, urgency=urgency, urgency_score=float(urgency.value)),
        {"rule": rule},
    )


# -- Texto en castellano -----------------------------------------------------


def normalize(text: str) -> str:
    """Minúsculas y sin tildes: "Alérgico" y "alergico" tienen que coincidir."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _pattern(*words: str) -> re.Pattern[str]:
    # Cada palabra entera, para que "sin sal" no encienda "salsa" ni "mani"
    # encienda "manilla". Las variantes se escriben ya normalizadas.
    return re.compile(r"\b(" + "|".join(words) + r")\b")


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    found = pattern.search(text)
    return found.group(0) if found else None


# -- Notas de pedido ---------------------------------------------------------

_ALLERGY = _pattern(
    r"alergi\w*",
    r"alergic\w*",
    r"intoleran\w*",
    r"celiac\w*",
    r"sin gluten",
    r"gluten",
    r"lactosa",
    r"mani",
    r"cacahuate\w*",
    r"nuez",
    r"nueces",
    r"mariscos?",
    r"camarones?",
    r"crustaceos?",
    r"vegetarian\w*",
    r"vegan\w*",
    r"sin carne",
    r"diabetic\w*",
    r"no puede comer",
    r"le hace dano",
    r"le cae mal",
)
_PRIORITY = _pattern(
    r"urgente",
    r"rapido",
    r"apurad\w*",
    r"primero",
    r"prioridad",
    r"lo antes posible",
    r"de inmediato",
    r"ya mismo",
    r"tiene prisa",
    r"esta esperando",
)
_PREFERENCE = _pattern(
    r"sin",
    r"extra",
    r"poco",
    r"poca",
    r"mas",
    r"menos",
    r"bien cocid\w*",
    r"termino",
    r"aparte",
    r"picante",
    r"cremas?",
    r"salsas?",
    r"arroz",
    r"cambiar",
    r"en vez de",
)


def note_by_rules(text: str) -> tuple[NoteOutcome, dict[str, Any]]:
    """Clasifica una nota por palabras clave.

    Una alergia gana siempre, aunque la nota también pida algo más ("sin
    cebolla y es alérgico al maní"): es lo único que la cocina no puede pasar
    por alto.
    """
    normalized = normalize(text)
    allergy = _first_match(_ALLERGY, normalized)
    if allergy is not None:
        return (
            NoteOutcome(mentions_allergy=True, note_type=NoteType.ALLERGY),
            {"rule": "palabra_clave", "keyword": allergy},
        )
    for note_type, pattern in ((NoteType.PRIORITY, _PRIORITY), (NoteType.PREFERENCE, _PREFERENCE)):
        keyword = _first_match(pattern, normalized)
        if keyword is not None:
            return (
                NoteOutcome(mentions_allergy=False, note_type=note_type),
                {"rule": "palabra_clave", "keyword": keyword},
            )
    return NoteOutcome(mentions_allergy=False, note_type=NoteType.OTHER), {
        "rule": "sin_coincidencia"
    }


# -- Mermas ------------------------------------------------------------------

# El orden importa: "el cliente lo devolvió porque estaba salado" es una
# devolución, aunque también hable de un error de cocina.
_WASTE_RULES: tuple[tuple[WasteCause, re.Pattern[str]], ...] = (
    (
        WasteCause.CUSTOMER_RETURN,
        _pattern(r"devolvi\w*", r"devolucion", r"devuelto", r"cliente\w*", r"reclam\w*"),
    ),
    (
        WasteCause.EXPIRATION,
        _pattern(
            r"venci\w*",
            r"vencid\w*",
            r"caduc\w*",
            r"malogr\w*",
            r"podrid\w*",
            r"pudri\w*",
            r"olia mal",
            r"huele mal",
            r"mal olor",
            r"hongos?",
            r"moho",
            r"pasad[oa]s?",
            r"marchit\w*",
            r"agri\w*",
            r"cortad[oa]",
        ),
    ),
    (
        WasteCause.PREPARATION_ERROR,
        _pattern(
            r"quem\w*",
            r"salad[oa]",
            r"crud[oa]",
            r"sobrecocid\w*",
            r"mal preparad\w*",
            r"error\w*",
            r"equivoc\w*",
            r"receta",
            r"se paso de coccion",
        ),
    ),
    (
        WasteCause.MISHANDLING,
        _pattern(
            r"cay\w*",
            r"derram\w*",
            r"rompi\w*",
            r"roto",
            r"moj\w*",
            r"contamin\w*",
            r"refrigera\w*",
            r"congelad\w*",
            r"se corto la luz",
            r"sin luz",
            r"mal almacenad\w*",
            r"fuera del frio",
            r"golpead\w*",
        ),
    ),
)


def waste_by_rules(reason: str) -> tuple[WasteOutcome, dict[str, Any]]:
    normalized = normalize(reason)
    for cause, pattern in _WASTE_RULES:
        keyword = _first_match(pattern, normalized)
        if keyword is not None:
            return WasteOutcome(cause=cause), {"rule": "palabra_clave", "keyword": keyword}
    return WasteOutcome(cause=WasteCause.OTHER), {"rule": "sin_coincidencia"}
