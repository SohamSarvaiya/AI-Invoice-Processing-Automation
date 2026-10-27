"""Money and date parsing. Decimal only - floats are never used for amounts."""
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
_SYMBOLS = re.compile(r"(?i)(rs\.?|inr|usd|eur|gbp|₹|\$|€|£|/-)")
# 1,234.50 | 1,23,456.00 (Indian grouping) | 123456.5 | .5
_VALID = re.compile(r"^(?:\d{1,3}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$|^\.\d+$")


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(value) -> Decimal | None:
    """Parse '₹ 1,18,000.00', 'Rs. 500/-', 1234.5, '(50.00)' -> Decimal(2dp).
    Returns None for empty input; raises ValueError for ambiguous/garbled input."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    if isinstance(value, Decimal):
        return q(value)
    if isinstance(value, int):
        return q(Decimal(value))
    if isinstance(value, float):
        return q(Decimal(str(value)))
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = _SYMBOLS.sub("", s).replace(" ", "").strip("()-+")
    if not _VALID.match(s):
        raise ValueError(f"Unrecognised or ambiguous amount: {value!r}")
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"Unrecognised amount: {value!r}") from exc
    return q(-d if negative else d)


def parse_decimal(value) -> Decimal | None:
    """For quantities and percentages (no rounding to 2dp)."""
    if value is None:
        return None
    s = str(value).replace("%", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


_DAY_FIRST = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"]
_MONTH_FIRST = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"]
_COMMON = [
    "%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y", "%d/%b/%Y",
    "%d %b, %Y", "%d %B, %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
]


def parse_date(value, dayfirst: bool = True) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = re.sub(r"\s+", " ", str(value)).strip().rstrip(".,")
    if not s:
        return None
    formats = _COMMON[:2] + (_DAY_FIRST + _MONTH_FIRST if dayfirst else _MONTH_FIRST + _DAY_FIRST) + _COMMON[2:]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")
