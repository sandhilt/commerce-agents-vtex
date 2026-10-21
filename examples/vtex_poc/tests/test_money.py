# SPDX-License-Identifier: Apache-2.0
"""Tests for the money helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from vtex_poc.money import cents_to_decimal, decimal_to_cents, format_brl, to_decimal, to_float


def test_cents_to_decimal_basics() -> None:
    assert cents_to_decimal(0) == Decimal("0.00")
    assert cents_to_decimal(None) is None
    assert cents_to_decimal(1999) == Decimal("19.99")
    assert cents_to_decimal(-1000) == Decimal("-10.00")
    assert cents_to_decimal(123456789012) == Decimal("1234567890.12")


@pytest.mark.parametrize("bad", [True, False, 1.5, "100"])
def test_cents_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        cents_to_decimal(bad)  # type: ignore[arg-type]


def test_decimal_to_cents() -> None:
    assert decimal_to_cents(Decimal("19.99")) == 1999
    assert decimal_to_cents(Decimal("-10")) == -1000
    assert decimal_to_cents(0) == 0
    assert decimal_to_cents(Decimal("1234567890.12")) == 123456789012
    assert decimal_to_cents(Decimal("0.005")) == 1
    with pytest.raises(TypeError):
        decimal_to_cents(None)  # type: ignore[arg-type]


def test_half_up_rounding() -> None:
    assert to_decimal(Decimal("0.005")) == Decimal("0.01")
    assert to_decimal("2.675") == Decimal("2.68")
    assert to_decimal("2.674") == Decimal("2.67")
    assert to_decimal("-0.005") == Decimal("-0.01")


def test_float_artefacts() -> None:
    assert to_decimal(0.1 + 0.2) == Decimal("0.30")
    assert to_decimal(2.675) == Decimal("2.68")
    assert to_decimal(None) is None
    assert to_decimal(5) == Decimal("5.00")
    with pytest.raises(TypeError):
        to_decimal(True)


def test_to_float() -> None:
    assert to_float(Decimal("19.99")) == 19.99
    assert to_float(None) is None


def test_format_brl() -> None:
    assert format_brl(Decimal("1234.56")) == "R$ 1.234,56"
    assert format_brl(0) == "R$ 0,00"
    assert format_brl(Decimal("-10")) == "-R$ 10,00"
    assert format_brl(1234567.891) == "R$ 1.234.567,89"
    assert format_brl(123456789012, from_cents=True) == "R$ 1.234.567.890,12"
    assert format_brl(-1000, from_cents=True) == "-R$ 10,00"
    assert format_brl(None) == ""
    assert format_brl(None, from_cents=True) == ""
    with pytest.raises(TypeError):
        format_brl(10.5, from_cents=True)
