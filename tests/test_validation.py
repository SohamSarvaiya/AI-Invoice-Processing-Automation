from datetime import date
from decimal import Decimal as D

from app.domain import ExtractedInvoice
from app.services.validation import gstin_valid, has_errors, validate
from scripts.generate_samples import VENDOR_GSTIN

TODAY = date(2026, 6, 1)


def good(**over):
    d = {
        "invoice_number": "INV-1", "invoice_date": "14/03/2026", "vendor_name": "Acme Pvt Ltd",
        "vendor_gstin": VENDOR_GSTIN, "currency": "INR",
        "line_items": [{"description": "A", "quantity": "2", "unit_price": "5,000.00", "amount": "10,000.00"},
                       {"description": "B", "quantity": "4", "unit_price": "2,000.00", "amount": "8,000.00"}],
        "subtotal": "18,000.00", "cgst": "1,620.00", "sgst": "1,620.00", "tax_rate": "18", "total_amount": "21,240.00",
    }
    d.update(over)
    return ExtractedInvoice.from_dict(d)


TEXT = "INV-1 18,000.00 1,620.00 21,240.00 3,240.00"


def codes(ex, text=TEXT, s=None, **kw):
    from app.config import get_settings
    return {i.code for i in validate(ex, text, s or get_settings(), today=TODAY, **kw)}


def test_clean_invoice_has_no_issues(settings):
    assert validate(good(), TEXT, settings, today=TODAY) == []


def test_total_mismatch(settings):
    assert "TOTAL_MISMATCH" in codes(good(total_amount="24,240.00"), TEXT + " 24,240.00")


def test_small_rounding_is_tolerated():
    assert codes(good(total_amount="21,240.04"), TEXT + " 21,240.04") == set()


def test_round_off_line_is_respected():
    assert codes(good(total_amount="21,239.60", round_off="-0.40"), TEXT + " 21,239.60") == set()


def test_line_items_vs_subtotal():
    ex = good(subtotal="19,000.00")
    assert "LINE_ITEMS_MISMATCH" in codes(ex, TEXT + " 19,000.00")


def test_line_qty_times_rate():
    items = [{"description": "A", "quantity": "2", "unit_price": "5,000.00", "amount": "11,000.00"}]
    assert "LINE_AMOUNT_MISMATCH" in codes(good(line_items=items), TEXT + " 11,000.00")


def test_tax_rate_mismatch():
    assert "TAX_MISMATCH" in codes(good(tax_rate="12"))


def test_cgst_sgst_must_match():
    assert "TAX_MISMATCH" in codes(good(sgst="1,000.00"), TEXT + " 1,000.00")


def test_igst_with_cgst_rejected():
    assert "TAX_MISMATCH" in codes(good(igst="10.00"), TEXT + " 10.00")


def test_irregular_slab_warning():
    ex = good(cgst=None, sgst=None, tax_amount="1,800.00", tax_rate=None, total_amount="19,800.00")
    issues = validate(ex, TEXT + " 1,800.00 19,800.00", __import__("app.config").config.get_settings(), today=TODAY)
    assert any(i.code == "IRREGULAR_TAX_RATE" and i.severity == "warning" for i in issues)


def test_missing_required_fields():
    ex = good(invoice_number=None, invoice_date=None, vendor_name=None, total_amount=None)
    issues = validate(ex, TEXT, __import__("app.config").config.get_settings(), today=TODAY)
    assert {i.field for i in issues if i.code == "MISSING_FIELD"} == {"invoice_number", "invoice_date", "vendor_name", "total_amount"}


def test_unreadable_date_is_flagged():
    assert "UNREADABLE_VALUE" in codes(good(invoice_date="31/02/2026"))


def test_future_and_stale_dates():
    assert "FUTURE_DATE" in codes(good(invoice_date="01/01/2027"))
    assert "STALE_DATE" in codes(good(invoice_date="01/01/2024"))


def test_non_positive_and_high_value():
    assert "INVALID_AMOUNT" in codes(good(total_amount="0"), TEXT)
    assert "HIGH_VALUE" in codes(good(subtotal="500,000.00", cgst=None, sgst=None, tax_amount="0", line_items=[], total_amount="500,000.00"), "INV-1 500,000.00 0.00")


def test_gstin_checksum():
    assert gstin_valid(VENDOR_GSTIN)
    assert not gstin_valid(VENDOR_GSTIN[:-1] + ("X" if VENDOR_GSTIN[-1] != "X" else "Y"))
    assert "INVALID_GSTIN" in codes(good(vendor_gstin="BADGSTIN"))


def test_no_breakdown_warning_when_total_cannot_be_verified():
    ex = good(subtotal=None, line_items=[], cgst=None, sgst=None, tax_rate=None)
    assert "NO_BREAKDOWN" in codes(ex)


def test_grounding_catches_hallucinated_amount():
    # total is arithmetically consistent but never printed on the document
    ex = good(subtotal="20,000.00", line_items=[], cgst="1,800.00", sgst="1,800.00", total_amount="23,600.00")
    issues = validate(ex, "INV-1 something else entirely 18,000.00", __import__("app.config").config.get_settings(), today=TODAY)
    assert "AMOUNT_NOT_IN_SOURCE" in {i.code for i in issues} and has_errors(issues)


def test_grounding_catches_invented_invoice_number():
    assert "VALUE_NOT_IN_SOURCE" in codes(good(invoice_number="INV-999"))


def test_grounding_skipped_after_human_correction(settings):
    ex = good(total_amount="24,240.00")  # not in text
    assert "AMOUNT_NOT_IN_SOURCE" not in {i.code for i in validate(ex, None, settings, today=TODAY)}


def test_decimal_exactness():
    # 0.1 + 0.2 style drift must not create false errors
    ex = good(subtotal="0.30", line_items=[{"description": "x", "quantity": "1", "unit_price": "0.10", "amount": "0.10"},
                                          {"description": "y", "quantity": "1", "unit_price": "0.20", "amount": "0.20"}],
              cgst=None, sgst=None, tax_rate=None, total_amount="0.30")
    assert "LINE_ITEMS_MISMATCH" not in codes(ex, "INV-1 0.30 0.10 0.20")
