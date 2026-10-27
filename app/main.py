import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from pathlib import Path

from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, init_db
from .domain import ExtractedInvoice
from .errors import DuplicateFileError, PermanentError, TransientError, UnsupportedFileType
from .models import Invoice, ProcessingLog, Status
from .schemas import ApproveIn, CorrectionIn, InvoiceDetail, InvoiceOut, LLMModelsIn, LLMSettingsIn, LogOut, RejectIn
from .services import llm, pipeline, reports
from .services.extraction import LLMExtractor
from .services.validation import NON_OVERRIDABLE


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="AI Invoice Processing Automation",
    version="1.0.0",
    description="Upload invoices -> OCR -> structured extraction -> deterministic validation -> human approval.",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str | None = Header(default=None)):
    key = get_settings().api_key
    if key and not secrets.compare_digest(x_api_key or "", key):
        raise HTTPException(401, "Invalid or missing X-API-Key")


router = APIRouter(dependencies=[Depends(require_api_key)])


def _get(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, f"Invoice {invoice_id} not found")
    return inv


def _blocking(inv: Invoice) -> list[str]:
    return sorted({i["code"] for i in (inv.validation_issues or []) if i["severity"] == "error" and i["code"] in NON_OVERRIDABLE})


@app.get("/health", tags=["system"])
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    """Send anyone who opens the bare URL to the review console."""
    return RedirectResponse("/review")


@app.get("/review", include_in_schema=False)
def review_page():
    """Static review console. The page itself holds no data; every API call it makes needs the API key."""
    return FileResponse(Path(__file__).parent / "static" / "review.html")


# ------------------------------------------------------------------ intake
@router.post("/invoices/upload", response_model=InvoiceOut, status_code=201, tags=["intake"])
async def upload(file: UploadFile = File(...), source: str = Form("api"), db: Session = Depends(get_db)):
    """Process one invoice (PDF/PNG/JPG/TIFF). Returns the result immediately.
    Check `status`: PENDING_APPROVAL, NEEDS_REVIEW or FAILED."""
    content = await file.read()
    if len(content) > get_settings().max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {get_settings().max_upload_mb} MB")
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        return pipeline.ingest(db, content, file.filename or "upload", source)
    except DuplicateFileError as e:
        raise HTTPException(409, {"message": str(e), "existing_invoice_id": e.existing_id})
    except UnsupportedFileType as e:
        raise HTTPException(415, str(e))


# ------------------------------------------------------------------ read
@router.get("/invoices", response_model=list[InvoiceOut], tags=["invoices"])
def list_invoices(status: Status | None = None, limit: int = Query(50, le=500), offset: int = 0, db: Session = Depends(get_db)):
    stmt = select(Invoice).order_by(Invoice.id.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(Invoice.status == status.value)
    return db.scalars(stmt).all()


@router.get("/invoices/review-queue", response_model=list[InvoiceOut], tags=["invoices"])
def review_queue(db: Session = Depends(get_db)):
    """Everything waiting for a human: errors first, then clean invoices awaiting approval."""
    rows = db.scalars(select(Invoice).where(Invoice.status.in_([Status.NEEDS_REVIEW.value, Status.PENDING_APPROVAL.value, Status.FAILED.value])).order_by(Invoice.id)).all()
    order = {Status.FAILED.value: 0, Status.NEEDS_REVIEW.value: 1, Status.PENDING_APPROVAL.value: 2}
    return sorted(rows, key=lambda r: (order[r.status], r.id))


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetail, tags=["invoices"])
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    return _get(db, invoice_id)


@router.get("/invoices/{invoice_id}/logs", response_model=list[LogOut], tags=["invoices"])
def get_logs(invoice_id: int, db: Session = Depends(get_db)):
    _get(db, invoice_id)
    return db.scalars(select(ProcessingLog).where(ProcessingLog.invoice_id == invoice_id).order_by(ProcessingLog.id)).all()


# ------------------------------------------------------------------ human actions
@router.post("/invoices/{invoice_id}/correct", response_model=InvoiceOut, tags=["review"])
def correct(invoice_id: int, body: CorrectionIn, db: Session = Depends(get_db)):
    """Human fixes extracted fields; validation + duplicate check are re-run by deterministic code."""
    inv = _get(db, invoice_id)
    if inv.status not in (Status.NEEDS_REVIEW.value, Status.PENDING_APPROVAL.value):
        raise HTTPException(409, f"Cannot correct an invoice in status {inv.status}")
    changes = body.model_dump(exclude_unset=True, exclude={"reviewer", "note"})
    if not changes:
        raise HTTPException(400, "No fields to change")
    data = dict(inv.extracted_data or {})
    diff = {k: [data.get(k), v] for k, v in changes.items() if data.get(k) != v}
    data.update({k: (None if v is None else (v if k == "line_items" else str(v))) for k, v in changes.items()})
    ex = ExtractedInvoice.from_dict(data, get_settings().date_dayfirst)
    inv.corrections = (inv.corrections or []) + [{"at": datetime.now(timezone.utc).isoformat(), "reviewer": body.reviewer, "note": body.note, "changes": diff}]
    pipeline.log_event(db, inv.id, "correction", "INFO", f"{body.reviewer} corrected: {', '.join(diff) or 'no effective change'}")
    # grounding is skipped on purpose: a human has now verified the numbers against the document
    pipeline.evaluate(db, inv, ex, ground=False)
    db.commit()
    return inv


@router.post("/invoices/{invoice_id}/approve", response_model=InvoiceOut, tags=["review"])
def approve(invoice_id: int, body: ApproveIn, db: Session = Depends(get_db)):
    inv = _get(db, invoice_id)
    if inv.status == Status.PENDING_APPROVAL.value:
        pass
    elif inv.status == Status.NEEDS_REVIEW.value:
        blocked = _blocking(inv)
        if blocked:
            raise HTTPException(409, {"message": "Money/integrity errors cannot be force-approved. Fix them with /correct or reject.", "blocking_codes": blocked})
        if not body.force or len((body.note or "").strip()) < 5:
            raise HTTPException(409, "Invoice has validation errors. Fix via /correct, or approve with force=true and a note (min 5 chars).")
    else:
        raise HTTPException(409, f"Cannot approve an invoice in status {inv.status}")
    inv.status = Status.APPROVED.value
    inv.reviewer, inv.review_note, inv.reviewed_at = body.reviewer, body.note, datetime.now(timezone.utc)
    pipeline.log_event(db, inv.id, "approval", "INFO", f"Approved by {body.reviewer}" + (" (forced)" if body.force else ""))
    return inv


@router.post("/invoices/{invoice_id}/reject", response_model=InvoiceOut, tags=["review"])
def reject(invoice_id: int, body: RejectIn, db: Session = Depends(get_db)):
    inv = _get(db, invoice_id)
    if inv.status in (Status.APPROVED.value, Status.REJECTED.value, Status.PROCESSING.value):
        raise HTTPException(409, f"Cannot reject an invoice in status {inv.status}")
    inv.status = Status.REJECTED.value
    inv.reviewer, inv.review_note, inv.reviewed_at = body.reviewer, body.reason, datetime.now(timezone.utc)
    pipeline.log_event(db, inv.id, "rejection", "INFO", f"Rejected by {body.reviewer}: {body.reason}")
    return inv


@router.post("/invoices/{invoice_id}/retry", response_model=InvoiceOut, tags=["review"])
def retry_one(invoice_id: int, db: Session = Depends(get_db)):
    """Re-run the pipeline on the stored file (e.g. after an outage)."""
    inv = _get(db, invoice_id)
    if inv.status not in (Status.FAILED.value, Status.NEEDS_REVIEW.value):
        raise HTTPException(409, f"Only FAILED or NEEDS_REVIEW invoices can be retried (status is {inv.status})")
    pipeline.log_event(db, inv.id, "retry", "INFO", "Manual retry requested")
    return pipeline.run_pipeline(db, inv)


@router.post("/invoices/retry-failed", tags=["review"])
def retry_failed(db: Session = Depends(get_db)):
    """Retry every FAILED invoice. Wire this to an n8n schedule."""
    ids = db.scalars(select(Invoice.id).where(Invoice.status == Status.FAILED.value)).all()
    results = {}
    for i in ids:
        results[i] = pipeline.run_pipeline(db, db.get(Invoice, i)).status
    return {"retried": len(ids), "results": results}


# ------------------------------------------------------------------ AI engine (live demo settings)
def _provider(name: str) -> str:
    p = llm.normalize(name)
    if p not in llm.PROVIDERS:
        raise HTTPException(400, "Provider must be one of: anthropic (Claude), openai (ChatGPT), gemini (Google)")
    return p


def _llm_state() -> dict:
    s = get_settings()
    cfg = llm.resolve(s)
    return {
        "extraction_mode": s.extraction_mode,
        "active": None if not cfg else {"provider": cfg.provider, "label": cfg.label, "model": cfg.model,
                                        "key_hint": cfg.key_hint, "source": cfg.source},
        "providers": [{"id": p, "label": llm.LABELS[p], "default_model": llm.default_model(s, p),
                       "env_key": bool(llm.env_key(s, p))} for p in llm.PROVIDERS],
    }


@router.get("/settings/llm", tags=["settings"])
def llm_get():
    """Which AI engine reads invoices right now. Keys are never returned (only the last 4 characters)."""
    return _llm_state()


@router.put("/settings/llm", tags=["settings"])
def llm_put(body: LLMSettingsIn):
    """Pick provider/model and optionally paste a key. Held in server memory only; cleared on restart."""
    s = get_settings()
    p = _provider(body.provider)
    key = body.api_key.strip() or llm.env_key(s, p)
    if len(key) < 8:
        raise HTTPException(400, f"Paste your {llm.LABELS[p]} API key (none found in .env)")
    llm.set_override(llm.LLMConfig(p, key, body.model.strip() or llm.default_model(s, p), source="ui"))
    return _llm_state()


@router.delete("/settings/llm", tags=["settings"])
def llm_clear():
    llm.set_override(None)
    return _llm_state()


@router.post("/settings/llm/models", tags=["settings"])
def llm_models(body: LLMModelsIn):
    """List the models this key can use (the key is not stored by this call)."""
    s = get_settings()
    p = _provider(body.provider)
    key = body.api_key.strip() or llm.env_key(s, p)
    if not key:
        raise HTTPException(400, f"Paste your {llm.LABELS[p]} API key first")
    try:
        return {"provider": p, "models": llm.list_models(p, key)}
    except PermanentError as e:
        raise HTTPException(400, str(e))
    except TransientError as e:
        raise HTTPException(502, str(e))


@router.post("/settings/llm/test", tags=["settings"])
def llm_test():
    """One real call to the active provider with a tiny sample invoice. Always HTTP 200; check `ok`."""
    s = get_settings()
    cfg = llm.resolve(s)
    if not cfg:
        raise HTTPException(400, "No AI key configured")
    t0 = time.time()
    try:
        data = LLMExtractor(s, config=cfg).extract(llm.SAMPLE_INVOICE)
    except (PermanentError, TransientError) as e:
        return {"ok": False, "provider": cfg.provider, "model": cfg.model, "error": str(e)}
    fields = {k: data.get(k) for k in ("invoice_number", "invoice_date", "vendor_name", "total_amount")}
    return {"ok": True, "provider": cfg.provider, "model": cfg.model, "seconds": round(time.time() - t0, 1), "fields": fields}


# ------------------------------------------------------------------ reports
def _range(date_from: datetime | None, date_to: datetime | None):
    return date_from, date_to


@router.get("/reports/summary", tags=["reports"])
def report_summary(date_from: datetime | None = None, date_to: datetime | None = None, db: Session = Depends(get_db)):
    return reports.summary(db, *_range(date_from, date_to))


@router.get("/reports/invoices.csv", response_class=PlainTextResponse, tags=["reports"])
def report_csv(date_from: datetime | None = None, date_to: datetime | None = None, db: Session = Depends(get_db)):
    return PlainTextResponse(reports.csv_export(db, *_range(date_from, date_to)), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=processed_invoices.csv"})


app.include_router(router)
