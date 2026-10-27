import shutil
from decimal import Decimal

import pytest

from app.errors import PermanentError, TransientError
from app.services import pipeline
from scripts.generate_samples import make_png
from tests.conftest import upload

REVIEWER = {"reviewer": "meera"}


def codes(resp):
    return {i["code"] for i in resp["validation_issues"]}


def test_clean_invoice_goes_to_pending_approval_not_auto_approved(client, pdf):
    r = upload(client, pdf())
    assert r.status_code == 201
    j = r.json()
    assert j["status"] == "PENDING_APPROVAL"
    assert (j["invoice_number"], j["invoice_date"], j["vendor_name"]) == ("INV-2026-0042", "2026-03-14", "Urban Cloud Solutions Pvt Ltd")
    assert Decimal(str(j["total_amount"])) == Decimal("21240.00") and j["validation_issues"] == []
    assert j["reviewer"] is None  # nobody approved it yet


def test_exact_same_file_rejected_with_409(client, pdf):
    p = pdf()
    first = upload(client, p).json()
    r = upload(client, p)
    assert r.status_code == 409 and r.json()["detail"]["existing_invoice_id"] == first["id"]


def test_same_invoice_number_different_file_flagged_as_duplicate(client, pdf):
    upload(client, pdf("a.pdf"))
    j = upload(client, pdf("b.pdf", date="15/03/2026")).json()
    assert j["status"] == "NEEDS_REVIEW" and "DUPLICATE_INVOICE" in codes(j) and j["duplicate_of_id"]


def test_vendor_name_variation_still_detected(client, pdf):
    upload(client, pdf("a.pdf"))
    j = upload(client, pdf("b.pdf", vendor="URBAN CLOUD SOLUTIONS PRIVATE LIMITED", date="16/03/2026")).json()
    assert "DUPLICATE_INVOICE" in codes(j)


def test_same_vendor_date_amount_new_number_is_warning_only(client, pdf):
    upload(client, pdf("a.pdf"))
    j = upload(client, pdf("b.pdf", number="INV-2026-9999")).json()
    assert "POSSIBLE_DUPLICATE" in codes(j) and j["status"] == "PENDING_APPROVAL"


def test_tampered_total_needs_review_and_cannot_be_force_approved(client, pdf):
    j = upload(client, pdf(total="24,240.00")).json()
    assert j["status"] == "NEEDS_REVIEW" and "TOTAL_MISMATCH" in codes(j)
    r = client.post(f"/invoices/{j['id']}/approve", json={**REVIEWER, "force": True, "note": "looks fine to me"})
    assert r.status_code == 409 and "TOTAL_MISMATCH" in r.json()["detail"]["blocking_codes"]


def test_missing_fields_routed_to_review(client, pdf):
    j = upload(client, pdf(number=None, date=None)).json()
    assert j["status"] == "NEEDS_REVIEW"
    assert {"MISSING_FIELD"} <= codes(j)


def test_human_corrects_then_approves(client, pdf):
    j = upload(client, pdf(number=None)).json()
    assert j["status"] == "NEEDS_REVIEW"
    r = client.post(f"/invoices/{j['id']}/correct", json={**REVIEWER, "invoice_number": "INV-MANUAL-1", "note": "typed from paper copy"}).json()
    assert r["status"] == "PENDING_APPROVAL" and r["invoice_number"] == "INV-MANUAL-1"
    r = client.post(f"/invoices/{j['id']}/approve", json={**REVIEWER}).json()
    assert r["status"] == "APPROVED" and r["reviewer"] == "meera" and r["reviewed_at"]
    detail = client.get(f"/invoices/{j['id']}").json()
    assert detail["corrections"][0]["changes"]["invoice_number"] == [None, "INV-MANUAL-1"]
    assert detail["original_extraction"]["invoice_number"] is None  # AI output preserved for audit


def test_correction_is_revalidated_by_code_not_trusted(client, pdf):
    j = upload(client, pdf(total="24,240.00")).json()
    r = client.post(f"/invoices/{j['id']}/correct", json={**REVIEWER, "total_amount": "25,000.00"}).json()
    assert r["status"] == "NEEDS_REVIEW" and "TOTAL_MISMATCH" in codes(r)
    r = client.post(f"/invoices/{j['id']}/correct", json={**REVIEWER, "total_amount": "21,240.00"}).json()
    assert r["status"] == "PENDING_APPROVAL"


def test_force_approve_overridable_error_needs_note(client, pdf):
    j = upload(client, pdf(number="INV-2026-0044", gstin="24AABCU9603R1ZX")).json()
    assert "INVALID_GSTIN" in codes(j)
    assert client.post(f"/invoices/{j['id']}/approve", json=REVIEWER).status_code == 409
    assert client.post(f"/invoices/{j['id']}/approve", json={**REVIEWER, "force": True}).status_code == 409
    r = client.post(f"/invoices/{j['id']}/approve", json={**REVIEWER, "force": True, "note": "GSTIN verified on portal"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"


def test_reject_and_resubmit_corrected_invoice_allowed(client, pdf):
    a = upload(client, pdf("a.pdf")).json()
    assert client.post(f"/invoices/{a['id']}/reject", json={**REVIEWER, "reason": "wrong PO"}).json()["status"] == "REJECTED"
    j = upload(client, pdf("b.pdf", date="15/03/2026")).json()  # same number, but the original was rejected
    assert "DUPLICATE_INVOICE" not in codes(j)


def test_cannot_approve_twice_or_reject_approved(client, pdf):
    j = upload(client, pdf()).json()
    assert client.post(f"/invoices/{j['id']}/approve", json=REVIEWER).status_code == 200
    assert client.post(f"/invoices/{j['id']}/approve", json=REVIEWER).status_code == 409
    assert client.post(f"/invoices/{j['id']}/reject", json={**REVIEWER, "reason": "oops"}).status_code == 409


def test_processing_logs_written(client, pdf):
    j = upload(client, pdf()).json()
    logs = client.get(f"/invoices/{j['id']}/logs").json()
    steps = [l["step"] for l in logs]
    assert steps[:3] == ["received", "ocr", "extract"] and "validate" in steps
    client.post(f"/invoices/{j['id']}/approve", json=REVIEWER)
    assert client.get(f"/invoices/{j['id']}/logs").json()[-1]["step"] == "approval"


def test_unsupported_and_empty_files(client, tmp_path):
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"MZ")
    assert upload(client, bad).status_code == 415
    empty = tmp_path / "e.pdf"
    empty.write_bytes(b"")
    assert upload(client, empty).status_code == 400


def test_corrupt_pdf_becomes_failed_not_500(client, tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4 this is not really a pdf")
    r = upload(client, p)
    assert r.status_code == 201 and r.json()["status"] == "FAILED" and "PDF" in r.json()["error_message"]


def test_transient_ocr_error_is_retried_automatically(client, pdf, monkeypatch):
    real, calls = pipeline.extract_text, []

    def flaky(path):
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("disk hiccup")
        return real(path)

    monkeypatch.setattr(pipeline, "extract_text", flaky)
    j = upload(client, pdf()).json()
    assert j["status"] == "PENDING_APPROVAL" and len(calls) == 3
    assert any("retrying" in l["message"] for l in client.get(f"/invoices/{j['id']}/logs").json())


def test_failed_invoice_can_be_retried_later(client, pdf, monkeypatch):
    real = pipeline.extract_text
    monkeypatch.setattr(pipeline, "extract_text", lambda p: (_ for _ in ()).throw(PermanentError("OCR engine down")))
    j = upload(client, pdf()).json()
    assert j["status"] == "FAILED" and j["attempts"] == 1
    monkeypatch.setattr(pipeline, "extract_text", real)
    r = client.post(f"/invoices/{j['id']}/retry").json()
    assert r["status"] == "PENDING_APPROVAL" and r["attempts"] == 2 and r["error_message"] is None


def test_retry_failed_batch_endpoint(client, pdf, monkeypatch):
    real = pipeline.extract_text
    monkeypatch.setattr(pipeline, "extract_text", lambda p: (_ for _ in ()).throw(PermanentError("down")))
    upload(client, pdf("a.pdf")); upload(client, pdf("b.pdf", number="INV-2"))
    monkeypatch.setattr(pipeline, "extract_text", real)
    r = client.post("/invoices/retry-failed").json()
    assert r["retried"] == 2 and set(r["results"].values()) <= {"PENDING_APPROVAL", "NEEDS_REVIEW"}


def test_retry_rejected_for_wrong_status(client, pdf):
    j = upload(client, pdf()).json()
    assert client.post(f"/invoices/{j['id']}/retry").status_code == 409


def test_auto_approve_is_off_by_default_and_rule_based_when_on(client, pdf, settings, monkeypatch):
    monkeypatch.setattr(settings, "auto_approve_limit", Decimal("50000"))
    ok = upload(client, pdf("a.pdf")).json()
    assert ok["status"] == "APPROVED" and ok["reviewer"] == "system:auto-rule"
    over = upload(client, pdf("b.pdf", number="INV-BIG", subtotal="800,000.00", items=[("Big", 1, "800,000.00", "800,000.00")],
                              cgst="72,000.00", sgst="72,000.00", total="944,000.00")).json()
    assert over["status"] == "PENDING_APPROVAL"  # above limit -> human
    warn = upload(client, pdf("c.pdf", number="INV-2026-7777")).json()  # POSSIBLE_DUPLICATE warning -> human
    assert warn["status"] == "PENDING_APPROVAL"


def test_review_queue_order_and_filters(client, pdf):
    good = upload(client, pdf("a.pdf")).json()
    bad = upload(client, pdf("b.pdf", number="INV-2", total="99.00")).json()
    q = client.get("/invoices/review-queue").json()
    assert [i["id"] for i in q] == [bad["id"], good["id"]]
    assert len(client.get("/invoices", params={"status": "NEEDS_REVIEW"}).json()) == 1


def test_report_summary_and_csv(client, pdf):
    a = upload(client, pdf("a.pdf")).json()
    upload(client, pdf("b.pdf", number="INV-2", total="99.00"))
    upload(client, pdf("c.pdf", date="15/03/2026"))
    client.post(f"/invoices/{a['id']}/approve", json=REVIEWER)
    s = client.get("/reports/summary").json()
    assert s["total_invoices"] == 3 and s["by_status"]["APPROVED"] == 1 and s["flagged_for_review"] == 2
    assert s["duplicates_detected"] == 1 and s["approved_amount"] == {"INR": "21240.00"}
    ts = s["time_saved"]
    assert ts["manual_minutes_baseline"] == 24.0 and ts["minutes_with_automation"] == 2 + 12 and ts["minutes_saved"] == 10.0
    assert {"code": "TOTAL_MISMATCH", "count": 1} in s["top_issues"]
    csv_text = client.get("/reports/invoices.csv").text.splitlines()
    assert csv_text[0].startswith("id,file,status") and len(csv_text) == 4


def test_api_key_enforced_when_configured(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert client.get("/invoices").status_code == 401
    assert client.get("/invoices", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/invoices", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_image_goes_through_real_ocr(client, tmp_path):
    p = make_png(tmp_path / "scan.png", number="INV-2026-0050", date="20/03/2026")
    j = upload(client, p).json()
    detail = client.get(f"/invoices/{j['id']}").json()
    assert detail["status"] in ("PENDING_APPROVAL", "NEEDS_REVIEW")  # OCR noise must never silently pass
    assert j["status"] != "FAILED"
    if j["status"] == "PENDING_APPROVAL":
        assert Decimal(str(j["total_amount"])) == Decimal("21240.00")


def test_review_page_served_without_leaking_data(client, pdf, settings, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    r = client.get("/review")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "Invoice Review" in r.text
    assert client.get("/invoices").status_code == 401  # the data endpoints stay protected


def test_root_redirects_to_review_console(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/review"
    assert client.get("/review").status_code == 200
