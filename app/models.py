import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Status(str, enum.Enum):
    PROCESSING = "PROCESSING"
    PENDING_APPROVAL = "PENDING_APPROVAL"  # passed every deterministic check, waits for a human
    NEEDS_REVIEW = "NEEDS_REVIEW"  # at least one validation error
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"  # technical failure (OCR/LLM/etc), can be retried


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoice_dup", "invoice_number_norm", "vendor_norm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(50), default="api")
    status: Mapped[str] = mapped_column(String(30), index=True, default=Status.PROCESSING.value)

    raw_text: Mapped[str | None] = mapped_column(Text)
    ocr_method: Mapped[str | None] = mapped_column(String(30))
    extraction_method: Mapped[str | None] = mapped_column(String(30))
    original_extraction: Mapped[dict | None] = mapped_column(JSON)  # untouched AI/rules output
    extracted_data: Mapped[dict | None] = mapped_column(JSON)  # working copy (after human fixes)
    corrections: Mapped[list | None] = mapped_column(JSON)  # audit trail of human edits

    invoice_number: Mapped[str | None] = mapped_column(String(100))
    invoice_number_norm: Mapped[str | None] = mapped_column(String(100))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    vendor_norm: Mapped[str | None] = mapped_column(String(255))
    vendor_gstin: Mapped[str | None] = mapped_column(String(15))
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    validation_issues: Mapped[list | None] = mapped_column(JSON, default=list)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    processing_seconds: Mapped[float | None] = mapped_column(Numeric(8, 3))

    reviewer: Mapped[str | None] = mapped_column(String(100))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProcessingLog(Base):
    __tablename__ = "processing_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    step: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
