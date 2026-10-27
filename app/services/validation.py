"""Deterministic validation. No AI is involved in any decision made here.

Every money check is plain Decimal arithmetic on the extracted numbers, and a
"grounding" check proves each key number/identifier literally appears in the
OCR text - so an LLM that hallucinates an amount cannot get it past this layer.
"""
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from ..config import Settings
from ..domain import ExtractedInvoice
from ..money import parse_money, q

ERROR, WARNING = "error", "warning"

# Money-math / integrity problems a human may NOT force-approve. They must fix
# the data (correct endpoint) or reject the invoice.
NON_OVERRIDABLE = {
    "DUPLICATE_INVOICE", "MISSING_FIELD", "INVALID_AMOUNT", "OCR_EMPTY", "UNREADABLE_VALUE",
    "TOTAL_MISMATCH", "LINE_ITEMS_MISMATCH", "LINE_AMOUNT_MISMATCH", "TAX_MISMATCH",
}

GST_SLABS = [Decimal(x) for x in ("0", "0.25", "3", "5", "12", "18", "28")]
_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class Issue:
    code: str
    severity: str
    field: str | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, c in enumerate(first14):
        p = _CH.index(c) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return _CH[(36 - total % 36) % 36]


def gstin_valid(g: str) -> bool:
    return bool(_GSTIN_RE.match(g)) and gstin_check_char(g[:14]) == g[14]


def _amounts_in_text(text: str) -> set[Decimal]:
    found = set()
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            v = parse_money(tok.rstrip(",."))
        except ValueError:
            continue
        if v is not None:
            found.add(v)
    return found


def _compact(s: str) -> str:
    return re.sub(r"[\s]+", "", s).upper()


def validate(
    ex: ExtractedInvoice,
    raw_text: str | None,
    settings: Settings,
    today: date | None = None,
) -> list[Issue]:
    """raw_text=None skips the grounding checks (used after a human corrected the data)."""
    today = today or date.today()
    tol = settings.amount_tolerance
    out: list[Issue] = []
    add = lambda code, sev, fld, msg: out.append(Issue(code, sev, fld, msg))  # noqa: E731

    # 1. unreadable raw values
    for f in ex.parse_errors:
        add("UNREADABLE_VALUE", ERROR, f, f"Could not read a valid value for '{f}'.")

    # 2. required fields
    for f, v in (
        ("invoice_number", ex.invoice_number),
        ("invoice_date", ex.invoice_date),
        ("vendor_name", ex.vendor_name),
        ("total_amount", ex.total_amount),
    ):
        if v is None and f not in ex.parse_errors:
            add("MISSING_FIELD", ERROR, f, f"Required field '{f}' is missing.")

    # 3. date sanity
    if ex.invoice_date:
        if ex.invoice_date > today:
            add("FUTURE_DATE", ERROR, "invoice_date", f"Invoice date {ex.invoice_date} is in the future.")
        elif (today - ex.invoice_date).days > settings.max_invoice_age_days:
            add("STALE_DATE", WARNING, "invoice_date",
                f"Invoice is older than {settings.max_invoice_age_days} days.")

    # 4. currency
    if ex.currency not in settings.currencies:
        add("UNSUPPORTED_CURRENCY", WARNING, "currency", f"Currency {ex.currency} is not in the allowed list.")

    # 5. GSTIN
    if ex.vendor_gstin:
        if not _GSTIN_RE.match(ex.vendor_gstin):
            add("INVALID_GSTIN", ERROR, "vendor_gstin", f"GSTIN '{ex.vendor_gstin}' has an invalid format.")
        elif not gstin_valid(ex.vendor_gstin):
            add("INVALID_GSTIN", ERROR, "vendor_gstin", f"GSTIN '{ex.vendor_gstin}' fails the checksum (likely a misread).")

    # 6. amount sanity
    if ex.total_amount is not None:
        if ex.total_amount <= 0:
            add("INVALID_AMOUNT", ERROR, "total_amount", "Total amount must be greater than zero.")
        elif ex.total_amount >= settings.high_value_threshold:
            add("HIGH_VALUE", WARNING, "total_amount",
                f"Total {ex.total_amount} is at or above the high-value threshold {settings.high_value_threshold}.")
    for f in ("subtotal", "tax_amount", "cgst", "sgst", "igst"):
        v = getattr(ex, f)
        if v is not None and v < 0:
            add("INVALID_AMOUNT", ERROR, f, f"'{f}' cannot be negative.")

    # 7. line items: qty x rate = amount, and sum = subtotal
    items_sum = None
    for n, it in enumerate(ex.line_items, 1):
        if None not in (it.quantity, it.unit_price, it.amount):
            if abs(q(it.quantity * it.unit_price) - it.amount) > tol:
                add("LINE_AMOUNT_MISMATCH", ERROR, f"line_items[{n}]",
                    f"Line {n}: {it.quantity} x {it.unit_price} = {q(it.quantity * it.unit_price)}, invoice says {it.amount}.")
    if ex.line_items and all(i.amount is not None for i in ex.line_items):
        items_sum = sum((i.amount for i in ex.line_items), Decimal("0"))
        if ex.subtotal is not None and abs(items_sum - ex.subtotal) > tol:
            add("LINE_ITEMS_MISMATCH", ERROR, "subtotal",
                f"Line items add up to {items_sum} but subtotal says {ex.subtotal}.")

    base = ex.subtotal if ex.subtotal is not None else items_sum

    # 8. tax
    tax = ex.effective_tax()
    comp = ex.components_tax()
    if ex.tax_amount is not None and comp is not None and abs(ex.tax_amount - comp) > tol:
        add("TAX_MISMATCH", ERROR, "tax_amount",
            f"Tax amount {ex.tax_amount} differs from CGST+SGST+IGST = {comp}.")
    if ex.cgst is not None and ex.sgst is not None and abs(ex.cgst - ex.sgst) > tol:
        add("TAX_MISMATCH", ERROR, "cgst", f"CGST ({ex.cgst}) and SGST ({ex.sgst}) must be equal.")
    if ex.igst is not None and (ex.cgst is not None or ex.sgst is not None):
        add("TAX_MISMATCH", ERROR, "igst", "IGST cannot be combined with CGST/SGST on one invoice.")
    if base is not None and tax is not None:
        if ex.tax_rate is not None:
            expected = q(base * ex.tax_rate / 100)
            if abs(expected - tax) > max(tol, Decimal("1.00")):  # tolerate per-line rounding
                add("TAX_MISMATCH", ERROR, "tax_amount",
                    f"{ex.tax_rate}% of {base} = {expected}, but invoice shows tax {tax}.")
            if ex.tax_rate not in GST_SLABS:
                add("IRREGULAR_TAX_RATE", WARNING, "tax_rate", f"{ex.tax_rate}% is not a standard GST slab.")
        elif base > 0:
            implied = tax / base * 100
            if not any(abs(implied - s) <= Decimal("0.15") for s in GST_SLABS):
                add("IRREGULAR_TAX_RATE", WARNING, "tax_amount",
                    f"Implied tax rate {implied:.2f}% matches no standard GST slab.")

    # 9. the key check: subtotal + tax + round-off == total
    if ex.total_amount is not None:
        if base is None:
            add("NO_BREAKDOWN", WARNING, "total_amount",
                "No subtotal or line items found, so the total could not be cross-verified.")
        else:
            expected_total = base + (tax or Decimal("0")) + (ex.round_off or Decimal("0"))
            if abs(expected_total - ex.total_amount) > tol:
                add("TOTAL_MISMATCH", ERROR, "total_amount",
                    f"Subtotal {base} + tax {tax or 0} + round-off {ex.round_off or 0} = {expected_total}, "
                    f"but invoice total is {ex.total_amount}.")

    # 10. grounding: values must literally exist in the OCR text
    if raw_text is not None and raw_text.strip():
        if ex.invoice_number and _compact(ex.invoice_number) not in _compact(raw_text):
            add("VALUE_NOT_IN_SOURCE", ERROR, "invoice_number",
                f"Invoice number '{ex.invoice_number}' does not appear in the document text.")
        present = _amounts_in_text(raw_text)
        for f in ("total_amount", "subtotal", "tax_amount"):
            v = getattr(ex, f)
            if v is not None and v not in present:
                add("AMOUNT_NOT_IN_SOURCE", ERROR, f,
                    f"{f} {v} does not appear in the document text (possible AI misread/hallucination).")
    return out


def has_errors(issues: list[Issue] | list[dict]) -> bool:
    return any((i.severity if isinstance(i, Issue) else i["severity"]) == ERROR for i in issues)
