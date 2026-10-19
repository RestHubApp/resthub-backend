"""El importe en letras de un comprobante: «SON CIENTO DIECIOCHO CON 00/100 SOLES».

Es la leyenda 1000 del catálogo 52 de SUNAT, la que va impresa en la boleta y la factura.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_UNITS = [
    "CERO",
    "UNO",
    "DOS",
    "TRES",
    "CUATRO",
    "CINCO",
    "SEIS",
    "SIETE",
    "OCHO",
    "NUEVE",
    "DIEZ",
    "ONCE",
    "DOCE",
    "TRECE",
    "CATORCE",
    "QUINCE",
    "DIECISEIS",
    "DIECISIETE",
    "DIECIOCHO",
    "DIECINUEVE",
    "VEINTE",
    "VEINTIUNO",
    "VEINTIDOS",
    "VEINTITRES",
    "VEINTICUATRO",
    "VEINTICINCO",
    "VEINTISEIS",
    "VEINTISIETE",
    "VEINTIOCHO",
    "VEINTINUEVE",
]
_TENS = [
    "_",
    "_",
    "_",
    "TREINTA",
    "CUARENTA",
    "CINCUENTA",
    "SESENTA",
    "SETENTA",
    "OCHENTA",
    "NOVENTA",
]
_HUNDREDS = [
    "_",
    "CIENTO",
    "DOSCIENTOS",
    "TRESCIENTOS",
    "CUATROCIENTOS",
    "QUINIENTOS",
    "SEISCIENTOS",
    "SETECIENTOS",
    "OCHOCIENTOS",
    "NOVECIENTOS",
]
_THOUSAND = 1000
_MILLION = 1_000_000
_HUNDRED = 100
_THIRTY = 30
_TEN = 10


def _below_thousand(number: int) -> str:
    if number == _HUNDRED:
        return "CIEN"
    hundreds, rest = divmod(number, _HUNDRED)
    words = [_HUNDREDS[hundreds]] if hundreds else []
    if rest >= _THIRTY:
        tens, unit = divmod(rest, _TEN)
        words.append(_TENS[tens] + (f" Y {_UNITS[unit]}" if unit else ""))
    elif rest:
        words.append(_UNITS[rest])
    return " ".join(words)


def integer_in_words(number: int) -> str:
    """Un entero no negativo en letras, en mayúsculas y sin tildes (como en los comprobantes)."""
    if number == 0:
        return "CERO"
    millions, rest = divmod(number, _MILLION)
    thousands, units = divmod(rest, _THOUSAND)
    words = []
    if millions:
        words.append("UN MILLON" if millions == 1 else f"{integer_in_words(millions)} MILLONES")
    if thousands:
        words.append("MIL" if thousands == 1 else f"{_below_thousand(thousands)} MIL")
    if units:
        words.append(_below_thousand(units))
    # «VEINTIUNO MIL» se dice «VEINTIUN MIL», y «UNO MILLONES», «UN MILLONES».
    return " ".join(words).replace("UNO MIL", "UN MIL").replace("UNO MILLONES", "UN MILLONES")


def amount_in_words(amount: Decimal) -> str:
    cents = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = int(cents)
    fraction = int((cents - whole) * 100)
    return f"SON {integer_in_words(whole)} CON {fraction:02d}/100 SOLES"
