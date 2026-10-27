"""Claude / ChatGPT / Gemini adapters and the live-demo settings endpoints (all HTTP mocked)."""
import json

import httpx
import pytest

from app.errors import PermanentError
from app.services import llm
from app.services.extraction import LLMExtractor, extract_invoice
from scripts.generate_samples import invoice_lines

TEXT = "\n".join(invoice_lines())
DATA = {"invoice_number": "INV-9", "total_amount": "10.00"}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._b, self.text = status, body, text

    def json(self):
        return self._b


def reply(provider):
    js = json.dumps(DATA)
    return {
        "anthropic": {"content": [{"type": "thinking"}, {"type": "text", "text": js}]},
        "openai": {"choices": [{"message": {"content": js}}]},
        "gemini": {"candidates": [{"content": {"parts": [{"thought": True, "text": "hmm"}, {"text": js}]}}]},
    }[provider]


@pytest.mark.parametrize("provider,host,auth", [
    ("anthropic", "api.anthropic.com", "x-api-key"),
    ("openai", "api.openai.com", "Authorization"),
    ("gemini", "generativelanguage.googleapis.com", "x-goog-api-key"),
])
def test_each_provider_request_and_parse(settings, provider, host, auth):
    seen = {}

    def post(url, **k):
        seen.update(url=url, headers=k["headers"], body=k["json"])
        return Resp(200, reply(provider))

    cfg = llm.LLMConfig(provider, "SECRET-KEY-1234", llm.default_model(settings, provider))
    assert LLMExtractor(settings, post=post, config=cfg).extract("t") == DATA
    assert host in seen["url"] and "SECRET-KEY-1234" not in seen["url"]  # key only in headers
    assert "SECRET-KEY-1234" in seen["headers"][auth]
    assert not {"temperature", "top_p", "top_k"} & seen["body"].keys()


def test_openai_uses_json_mode_and_gemini_json_mime(settings):
    cap = {}
    for p in ("openai", "gemini"):
        LLMExtractor(settings, post=lambda u, **k: (cap.__setitem__(p, k["json"]), Resp(200, reply(p)))[1],
                     config=llm.LLMConfig(p, "k" * 10, "m")).extract("t")
    assert cap["openai"]["response_format"] == {"type": "json_object"}
    assert cap["gemini"]["generationConfig"]["responseMimeType"] == "application/json"


def test_auto_picks_first_configured_provider_and_respects_llm_provider(settings):
    assert llm.resolve(settings) is None
    s = settings.model_copy(update={"gemini_api_key": "g" * 10, "openai_api_key": "o" * 10})
    assert llm.resolve(s).provider == "openai"
    assert llm.resolve(s.model_copy(update={"llm_provider": "gemini"})).provider == "gemini"
    assert llm.resolve(s.model_copy(update={"llm_provider": "chatgpt"})).provider == "openai"


def test_method_records_provider_and_missing_key_in_llm_mode_fails(settings):
    s = settings.model_copy(update={"openai_api_key": "o" * 10, "extraction_mode": "auto"})
    _, method, _ = extract_invoice(TEXT, s, lambda *a: None, llm_post=lambda u, **k: Resp(200, reply("openai")))
    assert method == "llm:openai"
    with pytest.raises(PermanentError):
        extract_invoice(TEXT, settings.model_copy(update={"extraction_mode": "llm"}), lambda *a: None)


def test_claude_effort_rejected_by_older_model_is_retried_without_it(settings):
    calls = []

    def post(u, **k):
        calls.append("output_config" in k["json"])
        return Resp(400, text='{"error":"effort not supported"}') if len(calls) == 1 else Resp(200, reply("anthropic"))

    LLMExtractor(settings, post=post, config=llm.LLMConfig("anthropic", "k" * 10, "old-model")).extract("t")
    assert calls == [True, False]


def test_list_models_filters_and_sorts():
    g = lambda url, **k: Resp(200, {"models": [{"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
                                               {"name": "models/embed-1", "supportedGenerationMethods": ["embedContent"]}]})
    assert llm.list_models("gemini", "k" * 10, get=g) == ["gemini-a"]
    o = lambda url, **k: Resp(200, {"data": [{"id": "gpt-x"}, {"id": "text-embedding-3"}, {"id": "whisper-1"}, {"id": "gpt-y"}]})
    assert llm.list_models("openai", "k" * 10, get=o) == ["gpt-y", "gpt-x"]
    with pytest.raises(PermanentError, match="rejected"):
        llm.list_models("openai", "bad", get=lambda *a, **k: Resp(401, text="no"))


# ----------------------------------------------------------------------------- API endpoints
def test_settings_endpoints_end_to_end(client, monkeypatch, pdf, settings):
    monkeypatch.setattr(settings, "extraction_mode", "auto")  # test env forces offline rules
    st = client.get("/settings/llm").json()
    assert st["active"] is None and {p["id"] for p in st["providers"]} == {"anthropic", "openai", "gemini"}

    assert client.put("/settings/llm", json={"provider": "nope"}).status_code == 400
    assert client.put("/settings/llm", json={"provider": "openai"}).status_code == 400  # no key anywhere

    monkeypatch.setattr(httpx, "get", lambda url, **k: Resp(200, {"data": [{"id": "gpt-demo"}]}))
    assert client.post("/settings/llm/models", json={"provider": "openai", "api_key": "sk-test-1234"}).json()["models"] == ["gpt-demo"]

    put = client.put("/settings/llm", json={"provider": "chatgpt", "api_key": "sk-test-1234", "model": "gpt-demo"}).json()
    assert put["active"] == {"provider": "openai", "label": "ChatGPT", "model": "gpt-demo", "key_hint": "…1234", "source": "ui"}
    assert "sk-test-1234" not in json.dumps(client.get("/settings/llm").json())  # never echoed back

    monkeypatch.setattr(httpx, "post", lambda url, **k: Resp(200, reply("openai")))
    t = client.post("/settings/llm/test").json()
    assert t["ok"] and t["fields"]["invoice_number"] == "INV-9"

    monkeypatch.setattr(httpx, "post", lambda url, **k: Resp(401, text="bad key"))
    t = client.post("/settings/llm/test").json()
    assert not t["ok"] and "401" in t["error"]

    # a real upload now goes through the pasted-in provider
    good = {"invoice_number": "INV-2026-0042", "invoice_date": "14/03/2026", "vendor_name": "Urban Cloud Solutions Pvt Ltd",
            "currency": "INR", "total_amount": "21,240.00", "subtotal": "18,000.00", "cgst": "1,620.00", "sgst": "1,620.00", "tax_rate": "18"}
    monkeypatch.setattr(httpx, "post", lambda url, **k: Resp(200, {"choices": [{"message": {"content": json.dumps(good)}}]}))
    from tests.conftest import upload

    r = upload(client, pdf())
    assert r.status_code == 201 and r.json()["extraction_method"] == "llm:openai"

    assert client.delete("/settings/llm").json()["active"] is None
