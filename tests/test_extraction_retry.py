import json

import httpx
import pytest

from app.errors import PermanentError, TransientError
from app.retry import retry_call
from app.services.extraction import LLMExtractor, RulesExtractor, extract_invoice
from scripts.generate_samples import invoice_lines

TEXT = "\n".join(invoice_lines())


def test_rules_extractor_reads_all_fields():
    r = RulesExtractor().extract(TEXT)
    assert r["invoice_number"] == "INV-2026-0042"
    assert r["invoice_date"] == "14/03/2026"  # not the due date
    assert r["vendor_name"] == "Urban Cloud Solutions Pvt Ltd"
    assert r["subtotal"] == "18,000.00" and r["total_amount"] == "21,240.00"
    assert r["cgst"] == r["sgst"] == "1,620.00" and r["tax_rate"] == "18.0"
    assert len(r["line_items"]) == 2


def test_rules_extractor_igst_and_round_off():
    r = RulesExtractor().extract("ACME\nInvoice #A-1\nDate: 5 Mar 2026\nTaxable Value 1000.00\nIGST (18%) 180.00\nRound off -0.40\nTotal Due Rs. 1,179.60")
    assert (r["igst"], r["round_off"], r["total_amount"], r["tax_rate"]) == ("180.00", "-0.40", "1,179.60", "18")


class FakeResp:
    def __init__(self, status=200, text=None, body=None):
        self.status_code, self.text = status, text or ""
        self._body = body

    def json(self):
        return self._body


def llm_ok(payload):
    return FakeResp(200, body={"content": [{"type": "text", "text": payload}]})


def test_llm_parses_fenced_json(settings):
    data = {"invoice_number": "X1", "total_amount": "10.00"}
    ex = LLMExtractor(settings.model_copy(update={"anthropic_api_key": "k"}), post=lambda *a, **k: llm_ok("```json\n" + json.dumps(data) + "\n```"))
    assert ex.extract("t") == data


def test_llm_retries_transient_then_succeeds(settings):
    s = settings.model_copy(update={"anthropic_api_key": "k", "extraction_mode": "llm", "retry_base_delay": 0})
    calls = []

    def post(*a, **k):
        calls.append(1)
        return FakeResp(429) if len(calls) < 3 else llm_ok('{"invoice_number": "X"}')

    data, method, notes = extract_invoice("t", s, lambda *a: None, llm_post=post)
    assert method == "llm:anthropic" and len(calls) == 3 and not notes


def test_llm_failure_falls_back_to_rules_with_warning(settings):
    s = settings.model_copy(update={"anthropic_api_key": "k", "extraction_mode": "auto", "retry_base_delay": 0})

    def post(*a, **k):
        raise httpx.ConnectError("down")

    data, method, notes = extract_invoice(TEXT, s, lambda *a: None, llm_post=post)
    assert method == "rules" and data["total_amount"] == "21,240.00" and notes[0][0] == "LLM_FALLBACK"


def test_strict_llm_mode_raises(settings):
    s = settings.model_copy(update={"anthropic_api_key": "k", "extraction_mode": "llm", "retry_base_delay": 0, "max_attempts": 2})
    with pytest.raises(TransientError):
        extract_invoice("t", s, lambda *a: None, llm_post=lambda *a, **k: FakeResp(503))


def test_llm_bad_credentials_not_retried(settings):
    s = settings.model_copy(update={"anthropic_api_key": "k", "extraction_mode": "llm"})
    calls = []
    with pytest.raises(PermanentError):
        extract_invoice("t", s, lambda *a: None, llm_post=lambda *a, **k: (calls.append(1), FakeResp(401, text="bad key"))[1])
    assert len(calls) == 1


def test_retry_call_backoff_and_exhaustion():
    sleeps, n = [], []

    def flaky():
        n.append(1)
        raise TransientError("boom")

    with pytest.raises(TransientError):
        retry_call(flaky, attempts=4, base_delay=1, sleep=sleeps.append)
    assert len(n) == 4 and sleeps == [1, 2, 4]


def test_retry_call_does_not_retry_other_errors():
    n = []
    with pytest.raises(ValueError):
        retry_call(lambda: (n.append(1), (_ for _ in ()).throw(ValueError("x")))[1], attempts=3, base_delay=0)
    assert len(n) == 1


def test_ocr_noise_stripped_from_invoice_number():
    assert RulesExtractor().extract("ACME\nInvoice No: INV-2026-0050_ Invoice Date: 20/03/2026")["invoice_number"] == "INV-2026-0050"


def test_llm_skips_thinking_block_and_sends_no_sampling_params(settings):
    sent = {}

    def post(url, **k):
        sent.update(k["json"])
        return FakeResp(200, body={"content": [{"type": "thinking", "thinking": ""},
                                               {"type": "text", "text": 'Here you go: {"invoice_number": "Z9"}'}]})

    ex = LLMExtractor(settings.model_copy(update={"anthropic_api_key": "k"}), post=post)
    assert ex.extract("t") == {"invoice_number": "Z9"}
    assert not {"temperature", "top_p", "top_k"} & sent.keys()
    assert sent["max_tokens"] >= 4096
