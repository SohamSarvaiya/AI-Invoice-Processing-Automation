from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class IssueOut(BaseModel):
    code: str
    severity: str
    field: str | None = None
    message: str


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    file_name: str
    source: str
    status: str
    invoice_number: str | None = None
    invoice_date: date | None = None
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    extraction_method: str | None = None
    validation_issues: list[IssueOut] = []
    duplicate_of_id: int | None = None
    attempts: int = 0
    error_message: str | None = None
    reviewer: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None
    processing_seconds: float | None = None
    created_at: datetime


class InvoiceDetail(InvoiceOut):
    extracted_data: dict | None = None
    original_extraction: dict | None = None
    corrections: list | None = None
    raw_text: str | None = None


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    step: str
    level: str
    message: str
    created_at: datetime


class CorrectionIn(BaseModel):
    """Only the fields you send are changed. Validation is re-run by deterministic code."""
    reviewer: str = Field(min_length=1)
    note: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    currency: str | None = None
    subtotal: str | float | None = None
    tax_amount: str | float | None = None
    tax_rate: str | float | None = None
    cgst: str | float | None = None
    sgst: str | float | None = None
    igst: str | float | None = None
    round_off: str | float | None = None
    total_amount: str | float | None = None
    line_items: list[dict] | None = None


class ApproveIn(BaseModel):
    reviewer: str = Field(min_length=1)
    note: str | None = None
    force: bool = False  # approve despite non-critical errors (note required)


class RejectIn(BaseModel):
    reviewer: str = Field(min_length=1)
    reason: str = Field(min_length=3)


class LLMSettingsIn(BaseModel):
    provider: str
    api_key: str = ""  # blank = use the key already in .env for that provider
    model: str = ""  # blank = provider default


class LLMModelsIn(BaseModel):
    provider: str
    api_key: str = ""
