# SPDX-License-Identifier: Apache-2.0
"""Decimal money helpers for VTEX values.

Checkout returns integer cents (discounts are negative); Intelligent Search returns decimal
prices. Everything is held as `Decimal` rounded half up to two places.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def cents_to_decimal(cents: int | None) -> Decimal | None:
    """Convert integer cents to a Decimal. None stays None; bool and float raise TypeError."""
    if cents is None:
        return None
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"cents must be an int, got {type(cents).__name__}")
    return _quantize(Decimal(cents) / _HUNDRED)


def decimal_to_cents(value: int | float | str | Decimal) -> int:
    """Convert a currency amount to integer cents, rounding half up."""
    dec = to_decimal(value)
    if dec is None:
        raise TypeError("value must not be None")
    return int(dec * _HUNDRED)


def to_decimal(value: int | float | str | Decimal | None) -> Decimal | None:
    """Convert a price to Decimal at 0.01, half up. Floats go through `str` first."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("bool is not a price")
    if isinstance(value, float):
        value = str(value)
    return _quantize(Decimal(value))


def to_float(value: Decimal | None) -> float | None:
    """Convert to the float prices the shared agent types use."""
    return None if value is None else float(value)


def format_brl(value: Decimal | int | float | None, *, from_cents: bool = False) -> str:
    """Format as "R$ 1.234,56" (negatives "-R$ 10,00"). None gives an empty string."""
    if value is None:
        return ""
    if from_cents:
        if isinstance(value, Decimal | float):
            raise TypeError("from_cents requires an int")
        dec = cents_to_decimal(value)
    else:
        dec = to_decimal(value)
    assert dec is not None
    sign = "-" if dec < 0 else ""
    whole, frac = f"{abs(dec):,.2f}".split(".")
    return f"{sign}R$ {whole.replace(',', '.')},{frac}"
