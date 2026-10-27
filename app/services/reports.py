import csv
import io
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Invoice, Status


def _query(db: Session, date_from: datetime | None, date_to: datetime | None):
    stmt = select(Invoice).order_by(Invoice.id)
    if date_from:
        stmt = stmt.where(Invoice.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Invoice.created_at <= date_to)
    return db.scalars(stmt).all()


def summary(db: Session, date_from: datetime | None = None, date_to: datetime | None = None) -> dict:
    s = get_settings()
    rows = _query(db, date_from, date_to)
    n = len(rows)
    by_status = Counter(r.status for r in rows)
    approved_amounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    pending_amounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    issue_counter: Counter = Counter()
    times = []
    for r in rows:
        cur = r.currency or "INR"
        if r.status == Status.APPROVED.value and r.total_amount is not None:
            approved_amounts[cur] += Decimal(r.total_amount)
        if r.status == Status.PENDING_APPROVAL.value and r.total_amount is not None:
            pending_amounts[cur] += Decimal(r.total_amount)
        for i in r.validation_issues or []:
            issue_counter[i["code"]] += 1
        if r.processing_seconds is not None:
            times.append(float(r.processing_seconds))

    clean = by_status[Status.APPROVED.value] + by_status[Status.PENDING_APPROVAL.value]
    touched = n - clean
    manual = n * s.manual_minutes_per_invoice
    automated = clean * s.review_minutes_per_invoice + touched * s.correction_minutes_per_invoice
    saved = max(0.0, manual - automated)
    return {
        "total_invoices": n,
        "by_status": dict(by_status),
        "straight_through_rate": round(clean / n, 3) if n else 0.0,
        "flagged_for_review": by_status[Status.NEEDS_REVIEW.value],
        "duplicates_detected": issue_counter["DUPLICATE_INVOICE"],
        "possible_duplicates": issue_counter["POSSIBLE_DUPLICATE"],
        "approved_amount": {k: str(v) for k, v in approved_amounts.items()},
        "awaiting_approval_amount": {k: str(v) for k, v in pending_amounts.items()},
        "top_issues": [{"code": c, "count": k} for c, k in issue_counter.most_common(10)],
        "avg_processing_seconds": round(sum(times) / len(times), 2) if times else None,
        "time_saved": {
            "note": "Estimate from configurable assumptions, not a measurement.",
            "assumptions": {
                "manual_minutes_per_invoice": s.manual_minutes_per_invoice,
                "human_minutes_per_clean_invoice (approve click)": s.review_minutes_per_invoice,
                "human_minutes_per_flagged_invoice (fix/reject)": s.correction_minutes_per_invoice,
                "hourly_cost": str(s.hourly_cost),
            },
            "manual_minutes_baseline": round(manual, 1),
            "minutes_with_automation": round(automated, 1),
            "minutes_saved": round(saved, 1),
            "hours_saved": round(saved / 60, 2),
            "percent_saved": round(saved / manual * 100, 1) if manual else 0.0,
            "estimated_cost_saved": str((Decimal(str(saved)) / 60 * s.hourly_cost).quantize(Decimal("0.01"))),
        },
    }


def csv_export(db: Session, date_from=None, date_to=None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "file", "status", "invoice_number", "invoice_date", "vendor", "gstin", "currency",
                "subtotal", "tax", "total", "issues", "reviewer", "review_note", "created_at"])
    for r in _query(db, date_from, date_to):
        w.writerow([r.id, r.file_name, r.status, r.invoice_number, r.invoice_date, r.vendor_name, r.vendor_gstin,
                    r.currency, r.subtotal, r.tax_amount, r.total_amount,
                    "; ".join(i["code"] for i in (r.validation_issues or [])), r.reviewer, r.review_note, r.created_at])
    return buf.getvalue()
