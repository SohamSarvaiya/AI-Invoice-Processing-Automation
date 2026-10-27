from datetime import date
from decimal import Decimal

import pytest

from app.money import parse_date, parse_money


@pytest.mark.parametrize("raw,expected", [
    ("₹ 1,18,000.00", "118000.00"), ("Rs. 500/-", "500.00"), ("1,234.5", "1234.50"),
    ("$99", "99.00"), (1234.5, "1234.50"), (10, "10.00"), ("(50.00)", "-50.00"), ("0.005", "0.01"),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1.234,56", "12,50", "abc", "1-2", "1,2,3"])
def test_ambiguous_money_is_rejected_not_guessed(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


def test_empty_is_none():
    assert parse_money(None) is None and parse_money("  ") is None


def test_no_float_drift():
    assert parse_money(0.1) + parse_money(0.2) == Decimal("0.30")


def test_dates():
    assert parse_date("14/03/2026") == date(2026, 3, 14)
    assert parse_date("03/04/2026", dayfirst=True) == date(2026, 4, 3)
    assert parse_date("03/04/2026", dayfirst=False) == date(2026, 3, 4)
    assert parse_date("14-Mar-2026") == date(2026, 3, 14)
    assert parse_date("March 14, 2026") == date(2026, 3, 14)
    with pytest.raises(ValueError):
        parse_date("31/02/2026")
