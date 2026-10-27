"""Orchestrates: store -> OCR -> extract -> validate -> duplicate check -> route."""
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import ExtractedInvoice
from ..errors import DuplicateFileError, TransientError, UnsupportedFileType
from ..models import Invoice, ProcessingLog, Status
from ..retry import retry_call
from .duplicates import find_duplicates, normalize_number, normalize_vendor
from .extraction import extract_invoice
from .ocr import SUPPORTED_EXT, extract_text
from .validation import ERROR, Issue, has_errors, validate

logger = logging.getLogger("invoice.pipeline")


def log_event(db: Session, invoice_id: int, step: str, level: str, message: str, details: dict | None = None):
    """Persist a processing-log row immediately so the trail survives later failures."""
    db.add(ProcessingLog(invoice_id=invoice_id, step=step, level=level, message=message, details=details))
    db.commit()
    logger.log(logging.WARNING if level in ("WARN", "ERROR") else logging.INFO, "[invoice %s] %s: %s", invoice_id, step, message)


def ingest(db: Session, content: bytes, filename: str, source: str = "api", llm_post=None) -> Invoice:
    s = get_settings()
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise UnsupportedFileType(f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(SUPPORTED_EXT))}")

    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(Invoice).where(Invoice.file_hash == digest))
    if existing:
        raise DuplicateFileError(existing.id)

    storage = Path(s.storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:120]
    path = storage / f"{digest[:16]}_{safe}"
    path.write_bytes(content)

    inv = Invoice(file_name=Path(filename).name, file_path=str(path), file_hash=digest, source=source,
                  status=Status.PROCESSING.value)
    db.add(inv)
    try:
        db.commit()
    except IntegrityError:  # two identical uploads racing
        db.rollback()
        existing = db.scalar(select(Invoice).where(Invoice.file_hash == digest))
        raise DuplicateFileError(existing.id if existing else -1)
    log_event(db, inv.id, "received", "INFO", f"File received via {source}: {inv.file_name} ({len(content)} bytes)")
    return run_pipeline(db, inv, llm_post=llm_post)


def run_pipeline(db: Session, inv: Invoice, llm_post=None) -> Invoice:
    """(Re)process a stored file. Technical failures end in FAILED and can be retried."""
    s = get_settings()
    started = time.perf_counter()
    inv.attempts = (inv.attempts or 0) + 1
    inv.status = Status.PROCESSING.value
    inv.error_message = None
    db.commit()
    log = lambda step, level, msg: log_event(db, inv.id, step, level, msg)  # noqa: E731
    try:
        ocr = retry_call(
            lambda: extract_text(Path(inv.file_path)),
            attempts=s.max_attempts, base_delay=s.retry_base_delay,
            on_retry=lambda n, e: log("ocr", "WARN", f"OCR attempt {n} failed, retrying: {e}"),
        )
        inv.raw_text, inv.ocr_method = ocr.text, ocr.method
        log("ocr", "INFO", f"Text extracted via {ocr.method} ({len(ocr.text)} chars)")

        if not ocr.text.strip():
            inv.validation_issues = [Issue("OCR_EMPTY", ERROR, None, "No readable text found in the document.").to_dict()]
            inv.status = Status.NEEDS_REVIEW.value
            log("validate", "WARN", "No text found - routed to human review")
        else:
            raw, method, notes = extract_invoice(ocr.text, s, log, llm_post)
            inv.extraction_method = method
            ex = ExtractedInvoice.from_dict(raw, s.date_dayfirst)
            inv.original_extraction = ex.to_dict()
            log("extract", "INFO", f"Fields extracted using '{method}'")
            evaluate(db, inv, ex, ground=True, extra=[Issue(c, "warning", None, m) for c, m in notes])
    except Exception as exc:  # noqa: BLE001 - last line of defence, state must be recorded
        db.rollback()
        inv.status = Status.FAILED.value
        inv.error_message = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        log("pipeline", "ERROR", f"Processing failed (attempt {inv.attempts}): {inv.error_message}")
    inv.processing_seconds = Decimal(str(round(time.perf_counter() - started, 3)))
    db.commit()
    return inv


def evaluate(db: Session, inv: Invoice, ex: ExtractedInvoice, *, ground: bool, extra: list[Issue] | None = None) -> None:
    """Deterministic validation + duplicate check, then route the invoice."""
    s = get_settings()
    issues = validate(ex, inv.raw_text if ground else None, s)
    dup_issues, dup_id = find_duplicates(db, inv, ex)
    issues = issues + dup_issues + (extra or [])

    inv.extracted_data = ex.to_dict()
    inv.invoice_number, inv.invoice_date, inv.vendor_name = ex.invoice_number, ex.invoice_date, ex.vendor_name
    inv.vendor_gstin, inv.currency = ex.vendor_gstin, ex.currency
    inv.subtotal, inv.tax_amount, inv.total_amount = ex.subtotal, ex.effective_tax(), ex.total_amount
    inv.invoice_number_norm, inv.vendor_norm = normalize_number(ex.invoice_number), normalize_vendor(ex.vendor_name)
    inv.duplicate_of_id = dup_id
    inv.validation_issues = [i.to_dict() for i in issues]

    if has_errors(issues):
        inv.status = Status.NEEDS_REVIEW.value
        errs = [i.code for i in issues if i.severity == ERROR]
        log_event(db, inv.id, "validate", "WARN", f"{len(errs)} validation error(s) -> NEEDS_REVIEW: {', '.join(errs)}")
        return

    inv.status = Status.PENDING_APPROVAL.value
    log_event(db, inv.id, "validate", "INFO",
              f"All deterministic checks passed ({len(issues)} warning(s)) -> PENDING_APPROVAL")

    # Optional rule-based auto approval (off by default). Never depends on AI confidence.
    limit = s.auto_approve_limit
    if limit > 0 and not issues and ex.total_amount is not None and ex.total_amount <= limit:
        inv.status = Status.APPROVED.value
        inv.reviewer, inv.review_note = "system:auto-rule", f"Auto-approved: zero issues and total <= {limit}"
        inv.reviewed_at = datetime.now(timezone.utc)
        log_event(db, inv.id, "approval", "INFO", inv.review_note)
