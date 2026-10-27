"""Verify an AI key works for invoice extraction (Claude, ChatGPT or Gemini, whichever is in .env).
Usage: python -m scripts.check_llm [anthropic|openai|gemini]"""
import json
import sys

from app.config import get_settings
from app.errors import PermanentError, TransientError
from app.services import llm
from app.services.extraction import LLMExtractor

s = get_settings()
if len(sys.argv) > 1:
    s = s.model_copy(update={"llm_provider": sys.argv[1]})
cfg = llm.resolve(s)
if not cfg:
    sys.exit("No key found. Put ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY in .env (no quotes, no spaces).")
print(f"Provider: {cfg.label} | model: {cfg.model} | key: {cfg.key_hint}")
try:
    data = LLMExtractor(s, config=cfg).extract(llm.SAMPLE_INVOICE)
except PermanentError as e:
    sys.exit(f"FAILED (check key / model name / billing): {e}")
except TransientError as e:
    sys.exit(f"FAILED (temporary, try again): {e}")
print("OK - the AI extracted:")
print(json.dumps({k: data.get(k) for k in ("invoice_number", "invoice_date", "vendor_name", "total_amount")}, indent=2))
