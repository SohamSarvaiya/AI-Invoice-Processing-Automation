"""Plain, typed representation of an extracted invoice (Decimal amounts, real dates)."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .money import parse_date, parse_decimal, parse_money


@dataclass
class LineItem:
    description: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


@dataclass
class ExtractedInvoice:
    invoice_number: str | None = None
    invoice_date: date | None = None
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    currency: str = "INR"
    line_items: list[LineItem] = field(default_factory=list)
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    tax_rate: Decimal | None = None  # total GST rate in %, e.g. 18
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    round_off: Decimal | None = None
    total_amount: Decimal | None = None
    parse_errors: list[str] = field(default_factory=list)  # fields whose raw value was unreadable

    def components_tax(self) -> Decimal | None:
        parts = [x for x in (self.cgst, self.sgst, self.igst) if x is not None]
        return sum(parts, Decimal("0")) if parts else None

    def effective_tax(self) -> Decimal | None:
        return self.tax_amount if self.tax_amount is not None else self.components_tax()

    # ------------------------------------------------------------------ io
    @classmethod
    def from_dict(cls, d: dict, dayfirst: bool = True) -> "ExtractedInvoice":
        errs: list[str] = []

        def safe(fn, key, *a):
            try:
                return fn(d.get(key), *a)
            except ValueError:
                errs.append(key)
                return None

        items = []
        for raw in d.get("line_items") or []:
            try:
                items.append(
                    LineItem(
                        description=str(raw.get("description") or "").strip(),
                        quantity=parse_decimal(raw.get("quantity")),
                        unit_price=parse_money(raw.get("unit_price")),
                        amount=parse_money(raw.get("amount")),
                    )
                )
            except ValueError:
                errs.append("line_items")

        def text(key):
            v = d.get(key)
            v = str(v).strip() if v is not None else ""
            return v or None

        gstin = text("vendor_gstin")
        return cls(
            invoice_number=text("invoice_number"),
            invoice_date=safe(parse_date, "invoice_date", dayfirst),
            vendor_name=text("vendor_name"),
            vendor_gstin=gstin.upper().replace(" ", "") if gstin else None,
            currency=(text("currency") or "INR").upper(),
            line_items=items,
            subtotal=safe(parse_money, "subtotal"),
            tax_amount=safe(parse_money, "tax_amount"),
            tax_rate=safe(parse_decimal, "tax_rate"),
            cgst=safe(parse_money, "cgst"),
            sgst=safe(parse_money, "sgst"),
            igst=safe(parse_money, "igst"),
            round_off=safe(parse_money, "round_off"),
            total_amount=safe(parse_money, "total_amount"),
            parse_errors=sorted(set(errs)),
        )

    def to_dict(self) -> dict:
        s = lambda v: None if v is None else str(v)  # noqa: E731
        return {
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "vendor_name": self.vendor_name,
            "vendor_gstin": self.vendor_gstin,
            "currency": self.currency,
            "line_items": [
                {"description": i.description, "quantity": s(i.quantity), "unit_price": s(i.unit_price), "amount": s(i.amount)}
                for i in self.line_items
            ],
            "subtotal": s(self.subtotal),
            "tax_amount": s(self.tax_amount),
            "tax_rate": s(self.tax_rate),
            "cgst": s(self.cgst),
            "sgst": s(self.sgst),
            "igst": s(self.igst),
            "round_off": s(self.round_off),
            "total_amount": s(self.total_amount),
        }
