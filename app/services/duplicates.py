import re
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import ExtractedInvoice
from ..models import Invoice, Status
from .validation import ERROR, WARNING, Issue

_NOISE = {"pvt", "private", "ltd", "limited", "llp", "inc", "llc", "co", "company", "corp", "corporation"}
_IGNORED = (Status.REJECTED.value, Status.FAILED.value, Status.PROCESSING.value)
SIMILARITY = 0.88


def normalize_vendor(name: str | None) -> str | None:
    if not name:
        return None
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if w not in _NOISE]
    return " ".join(words) or None


def normalize_number(num: str | None) -> str | None:
    if not num:
        return None
    return re.sub(r"[^A-Z0-9]", "", num.upper()) or None


def _similar(a: str | None, b: str | None) -> bool:
    return bool(a and b and SequenceMatcher(None, a, b).ratio() >= SIMILARITY)


def find_duplicates(db: Session, inv: Invoice, ex: ExtractedInvoice) -> tuple[list[Issue], int | None]:
    issues: list[Issue] = []
    dup_id = None
    vnorm, nnorm = normalize_vendor(ex.vendor_name), normalize_number(ex.invoice_number)

    if nnorm:  # same invoice number + (near-)same vendor -> the classic double payment
        rows = db.scalars(
            select(Invoice).where(Invoice.invoice_number_norm == nnorm, Invoice.id != inv.id,
                                  Invoice.status.not_in(_IGNORED))
        ).all()
        for r in rows:
            if _similar(vnorm, r.vendor_norm) or vnorm == r.vendor_norm:
                dup_id = r.id
                issues.append(Issue("DUPLICATE_INVOICE", ERROR, "invoice_number",
                                    f"Invoice {ex.invoice_number} from this vendor already exists as #{r.id} ({r.status})."))
                break

    if dup_id is None and ex.invoice_date and ex.total_amount is not None and vnorm:
        rows = db.scalars(
            select(Invoice).where(Invoice.invoice_date == ex.invoice_date, Invoice.id != inv.id,
                                  Invoice.status.not_in(_IGNORED))
        ).all()
        for r in rows:
            if r.total_amount == ex.total_amount and _similar(vnorm, r.vendor_norm):
                dup_id = r.id
                issues.append(Issue("POSSIBLE_DUPLICATE", WARNING, "total_amount",
                                    f"Same vendor, date and amount as invoice #{r.id} (different invoice number)."))
                break
    return issues, dup_id
