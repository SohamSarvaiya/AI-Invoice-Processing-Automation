"""Structured extraction. The extractor only *reads* printed values; it never
decides anything. Everything it returns is re-checked by validation.py."""
import json
import re
from typing import Callable

import httpx

from ..config import Settings
from ..errors import PermanentError, TransientError
from ..retry import retry_call
from . import llm

# --------------------------------------------------------------------------- rules
_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_DATE_TOKEN = re.compile(
    r"(?<![\d,.])(\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[-/ .][A-Za-z]{3,9}[-/ .,]+\d{2,4}"
    r"|[A-Za-z]{3,9}\.? \d{1,2},? \d{4})(?![\d,])"
)
_DUE = re.compile(r"(?i)due\s*date\s*[:\-]?\s*" + _DATE_TOKEN.pattern)
_GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_ITEM = re.compile(r"^\s*\d+\s+(.+?)\s+(\d+(?:\.\d+)?)\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s*$")
_INV_NO = re.compile(r"(?i)(?:invoice|bill|inv)\s*(?:no\.?|number|num|#)\s*[:\-#]?\s*([A-Z0-9][A-Z0-9\-/_.]*)")


def _last_number(line: str) -> str | None:
    toks = re.findall(_NUM, line)
    return toks[-1] if toks else None


def _find_line_amount(lines: list[str], label: str, exclude: str | None = None) -> str | None:
    hit = None
    for ln in lines:
        m = re.search(label, ln, re.I)
        if m and not (exclude and re.search(exclude, ln, re.I)):
            n = _last_number(ln[m.end():])
            if n:
                hit = n  # keep the LAST matching line (totals sit at the bottom)
    return hit


class RulesExtractor:
    """Offline, deterministic baseline. Great for typed PDFs; the LLM handles messy layouts."""

    name = "rules"

    def extract(self, text: str) -> dict:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        out: dict = {}

        m = _INV_NO.search(text)
        out["invoice_number"] = m.group(1).rstrip("._-/") if m else None

        out["invoice_date"] = None
        label = re.compile(r"(?i)invoice\s*date|date\s*of\s*issue|\bdated\b|^date\b")
        for ln in lines:
            m = label.search(ln)
            d = _DATE_TOKEN.search(ln[m.end():]) if m else None
            if d:
                out["invoice_date"] = d.group(1)
                break
        if not out["invoice_date"]:  # fallback: first date that is not a due date
            for ln in lines:
                d = _DATE_TOKEN.search(_DUE.sub("", ln))
                if d:
                    out["invoice_date"] = d.group(1)
                    break

        vendor = None
        for i, ln in enumerate(lines):
            m = re.match(r"(?i)^(?:from|vendor|supplier|billed\s*by|seller)\s*[:\-]\s*(.*)$", ln)
            if m:
                vendor = m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else None)
                break
        if not vendor:
            vendor = next((ln for ln in lines if not re.search(r"(?i)invoice|^\d+$|gstin", ln)), None)
        out["vendor_name"] = vendor

        g = _GSTIN.search(text)
        out["vendor_gstin"] = g.group(1) if g else None

        out["currency"] = (
            "USD" if re.search(r"\$|\bUSD\b", text) else "EUR" if re.search(r"€|\bEUR\b", text)
            else "GBP" if re.search(r"£|\bGBP\b", text) else "INR"
        )

        items = []
        for ln in lines:
            im = _ITEM.match(ln)
            if im:
                items.append({"description": im.group(1), "quantity": im.group(2),
                              "unit_price": im.group(3), "amount": im.group(4)})
        out["line_items"] = items

        gstin_free = [re.sub(_GSTIN, "", ln) for ln in lines]
        out["total_amount"] = (
            _find_line_amount(gstin_free, r"grand\s*total|total\s*amount|amount\s*(?:due|payable)|net\s*payable|invoice\s*total|total\s*due|balance\s*due")
            or _find_line_amount(gstin_free, r"^total\b", r"sub|tax|gst")
        )
        out["subtotal"] = _find_line_amount(gstin_free, r"sub\s*-?\s*total|taxable\s*(?:value|amount)")
        out["cgst"] = _find_line_amount(gstin_free, r"\bcgst\b", None)
        out["sgst"] = _find_line_amount(gstin_free, r"\bsgst\b", None)
        out["igst"] = _find_line_amount(gstin_free, r"\bigst\b", None)
        out["round_off"] = _find_line_amount(gstin_free, r"round\s*-?\s*off")
        generic = _find_line_amount(
            gstin_free, r"\b(?:total\s*)?(?:gst|tax|vat)\b", r"cgst|sgst|igst|taxable|invoice|gstin|%\s*of"
        )
        out["tax_amount"] = None if (out["cgst"] or out["sgst"] or out["igst"]) else generic

        # Rates are printed like "CGST @9%" or "GST (18%)"; amount parsing above takes the last number
        # on a line, so for "@9% 1,620.00" it returns 1,620.00 (correct) - rate is read separately.
        rates = {k: re.search(rf"(?i)\b{k}\b[^\n\d]*?(\d{{1,2}}(?:\.\d+)?)\s*%", text) for k in ("cgst", "sgst", "igst", "(?:gst|tax|vat)")}
        rate = None
        if rates["igst"]:
            rate = rates["igst"].group(1)
        elif rates["(?:gst|tax|vat)"]:
            rate = rates["(?:gst|tax|vat)"].group(1)
        elif rates["cgst"] and rates["sgst"]:
            rate = str(float(rates["cgst"].group(1)) + float(rates["sgst"].group(1)))
        out["tax_rate"] = rate
        return out


# --------------------------------------------------------------------------- LLM
_SYSTEM = """You extract fields from invoice text for an accounting system.
Rules:
- Copy values EXACTLY as printed. Never calculate, correct, round or guess.
- If a field is not printed on the invoice, use null.
- Amounts are strings exactly as printed (e.g. "1,18,000.00"), without currency symbols.
- invoice_date as printed. currency as a 3-letter ISO code.
- tax_rate is the TOTAL GST/VAT percentage as a number string (CGST 9% + SGST 9% -> "18").
- Respond with ONE JSON object and nothing else, with exactly these keys:
invoice_number, invoice_date, vendor_name, vendor_gstin, currency,
line_items (list of {description, quantity, unit_price, amount}),
subtotal, tax_amount, tax_rate, cgst, sgst, igst, round_off, total_amount"""


class LLMExtractor:
    name = "llm"

    def __init__(self, settings: Settings, post: Callable | None = None, config: llm.LLMConfig | None = None):
        self.s = settings
        self._post = post or httpx.post
        self.cfg = config or llm.resolve(settings)

    def _send(self, req: tuple[str, dict, dict]):
        url, headers, body = req
        try:
            return self._post(url, headers=headers, json=body, timeout=self.s.llm_timeout_seconds)
        except httpx.HTTPError as exc:
            raise TransientError(f"LLM network error: {exc}") from exc

    def extract(self, text: str) -> dict:
        if not self.cfg or not self.cfg.api_key:
            raise PermanentError("No AI key configured (set ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY, or paste one in AI settings)")
        label = self.cfg.label
        req = llm.build_request(self.cfg, self.s, _SYSTEM, f"<invoice_text>\n{text[:15000]}\n</invoice_text>")
        r = self._send(req)
        if r.status_code == 400 and "output_config" in req[2] and "effort" in (r.text or "").lower():
            body = {k: v for k, v in req[2].items() if k != "output_config"}  # older Claude model: retry without effort
            r = self._send((req[0], req[1], body))
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"{label} temporary error HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PermanentError(f"{label} request rejected HTTP {r.status_code}: {(r.text or '')[:300]}")
        try:
            body = llm.read_text(self.cfg.provider, r.json()).strip()
            body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
            if not body.startswith("{") and "{" in body:  # tolerate a stray sentence around the JSON
                body = body[body.index("{"): body.rindex("}") + 1]
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TransientError(f"{label} returned unparseable output: {exc}") from exc


# --------------------------------------------------------------------------- orchestration
def extract_invoice(
    text: str, settings: Settings, log: Callable[[str, str, str], None], llm_post: Callable | None = None
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Returns (raw_dict, method_used, notes[(code, message)]). Notes become validation warnings."""
    mode = settings.extraction_mode
    cfg = llm.resolve(settings)
    use_llm = mode == "llm" or (mode == "auto" and cfg is not None)
    notes: list[tuple[str, str]] = []
    if use_llm:
        extractor = LLMExtractor(settings, llm_post, cfg)
        try:
            data = retry_call(
                lambda: extractor.extract(text),
                attempts=settings.max_attempts,
                base_delay=settings.retry_base_delay,
                on_retry=lambda n, e: log("extract", "WARN", f"LLM attempt {n} failed, retrying: {e}"),
            )
            return data, f"llm:{cfg.provider}" if cfg else "llm", notes
        except Exception as exc:
            if mode == "llm":
                raise
            log("extract", "WARN", f"LLM failed ({exc}); falling back to rule-based extraction")
            notes.append(("LLM_FALLBACK", f"LLM extraction failed ({exc}); rule-based extraction used."))
    return RulesExtractor().extract(text), "rules", notes
