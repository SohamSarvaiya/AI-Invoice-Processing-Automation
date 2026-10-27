"""LLM provider adapters (Claude / ChatGPT / Gemini) + an in-memory key override for live demos.

Only *reading* goes through here: the model returns raw invoice fields as JSON and validation.py
re-checks every number. A key pasted in the UI lives in process memory only (never written to disk)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import httpx

from ..config import Settings
from ..errors import PermanentError, TransientError

PROVIDERS = ("anthropic", "openai", "gemini")
LABELS = {"anthropic": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}
ALIASES = {"claude": "anthropic", "chatgpt": "openai", "gpt": "openai", "google": "gemini"}

SAMPLE_INVOICE = """Urban Cloud Solutions Pvt Ltd
GSTIN: 27AABCU9603R1ZM
Tax Invoice
Invoice No: INV-2026-0042
Invoice Date: 14/03/2026
1 Cloud hosting 1 10,000.00 10,000.00
2 Support retainer 1 8,000.00 8,000.00
Subtotal 18,000.00
CGST @9% 1,620.00
SGST @9% 1,620.00
Grand Total 21,240.00"""


def normalize(provider: str | None) -> str:
    p = (provider or "auto").strip().lower()
    return ALIASES.get(p, p)


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    model: str
    source: str = "env"  # env | ui

    @property
    def label(self) -> str:
        return LABELS[self.provider]

    @property
    def key_hint(self) -> str:
        return "…" + self.api_key[-4:] if len(self.api_key) >= 8 else "…"


_override: LLMConfig | None = None


def set_override(cfg: LLMConfig | None) -> None:
    global _override
    _override = cfg


def get_override() -> LLMConfig | None:
    return _override


def env_key(s: Settings, provider: str) -> str:
    return {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key, "gemini": s.gemini_api_key}[provider].strip()


def default_model(s: Settings, provider: str) -> str:
    return {"anthropic": s.llm_model, "openai": s.openai_model, "gemini": s.gemini_model}[provider]


def resolve(s: Settings) -> LLMConfig | None:
    """Which provider/key/model is active right now? UI override > .env (LLM_PROVIDER or first key found)."""
    if _override:
        return _override
    wanted = normalize(s.llm_provider)
    for p in (PROVIDERS if wanted == "auto" else (wanted,)):
        if p in PROVIDERS and env_key(s, p):
            return LLMConfig(p, env_key(s, p), default_model(s, p))
    return None


# ----------------------------------------------------------------------------- requests
def build_request(cfg: LLMConfig, s: Settings, system: str, user: str) -> tuple[str, dict, dict]:
    """Returns (url, headers, json_body). Keys travel in headers, never in URLs."""
    if cfg.provider == "anthropic":
        body: dict = {"model": cfg.model, "max_tokens": s.llm_max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]}
        if s.llm_effort:  # Claude 5.x thinks adaptively; keep it cheap. No temperature: 5.x rejects it.
            body["output_config"] = {"effort": s.llm_effort}
        return ("https://api.anthropic.com/v1/messages",
                {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, body)
    if cfg.provider == "openai":
        return ("https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                {"model": cfg.model, "max_completion_tokens": s.llm_max_tokens,
                 "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    model = cfg.model.removeprefix("models/")
    return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": cfg.api_key, "Content-Type": "application/json"},
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": s.llm_max_tokens}})


def read_text(provider: str, data: dict) -> str:
    """Pull the answer text out of a response, skipping thinking blocks."""
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    if provider == "openai":
        return data["choices"][0]["message"]["content"] or ""
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


# ----------------------------------------------------------------------------- model list
_SKIP = ("embed", "whisper", "tts", "dall", "image", "moderation", "transcribe", "realtime", "audio", "search", "imagen", "veo")


def list_models(provider: str, api_key: str, get: Callable | None = None) -> list[str]:
    """Ask the provider which models this key can use, so a wrong model name never blocks a demo."""
    get = get or httpx.get
    if provider == "anthropic":
        url, h = "https://api.anthropic.com/v1/models?limit=100", {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "openai":
        url, h = "https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"}
    else:
        url, h = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": api_key}
    try:
        r = get(url, headers=h, timeout=20)
    except httpx.HTTPError as exc:
        raise TransientError(f"Could not reach {LABELS[provider]}: {exc}") from exc
    if r.status_code in (401, 403):
        raise PermanentError(f"{LABELS[provider]} rejected this key (HTTP {r.status_code}). Check the key and that billing/quota is enabled.")
    if r.status_code >= 400:
        raise PermanentError(f"{LABELS[provider]} model list failed (HTTP {r.status_code}): {r.text[:200]}")
    j = r.json()
    if provider == "gemini":
        ids = [m["name"].removeprefix("models/") for m in j.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    else:
        ids = [m["id"] for m in j.get("data", [])]
    return sorted((i for i in ids if not any(x in i.lower() for x in _SKIP)), reverse=True)
