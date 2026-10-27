"""Generate realistic sample invoices (PDF + PNG) for demos and tests."""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.services.validation import gstin_check_char


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    body = f"{state}{pan}{entity}Z"
    return body + gstin_check_char(body)


VENDOR_GSTIN = make_gstin("24", "AABCU9603R")
BUYER_GSTIN = make_gstin("27", "AAPFU0939F")

BASE = {
    "vendor": "Urban Cloud Solutions Pvt Ltd",
    "gstin": VENDOR_GSTIN,
    "number": "INV-2026-0042",
    "date": "14/03/2026",
    "due": "13/04/2026",
    "buyer": "Acme Traders",
    "buyer_gstin": BUYER_GSTIN,
    "items": [("Cloud hosting (annual)", 2, "5,000.00", "10,000.00"), ("Priority support", 4, "2,000.00", "8,000.00")],
    "subtotal": "18,000.00",
    "cgst": "1,620.00",
    "sgst": "1,620.00",
    "total": "21,240.00",
}


def invoice_lines(**over) -> list[str]:
    d = {**BASE, **over}
    lines = [d["vendor"]]
    if d["gstin"]:
        lines.append(f"GSTIN: {d['gstin']}")
    lines.append("TAX INVOICE")
    head = []
    if d["number"]:
        head.append(f"Invoice No: {d['number']}")
    if d["date"]:
        head.append(f"Invoice Date: {d['date']}")
    head.append(f"Due Date: {d['due']}")
    lines.append("   ".join(head))
    lines += [f"Bill To: {d['buyer']}", f"Buyer GSTIN: {d['buyer_gstin']}", "# Description Qty Rate Amount"]
    lines += [f"{i} {desc} {qty} {rate} {amt}" for i, (desc, qty, rate, amt) in enumerate(d["items"], 1)]
    lines += [f"Subtotal {d['subtotal']}", f"CGST @9% {d['cgst']}", f"SGST @9% {d['sgst']}", f"Grand Total {d['total']}"]
    return lines


def make_pdf(path: Path, **over) -> Path:
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for n, line in enumerate(invoice_lines(**over)):
        c.setFont("Helvetica-Bold" if n == 0 else "Helvetica", 16 if n == 0 else 11)
        c.drawString(50, y, line)
        y -= 28 if n == 0 else 20
    c.save()
    return path


def make_png(path: Path, **over) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    y = 40
    for line in invoice_lines(**over):
        draw.text((50, y), line, fill="black", font=font)
        y += 55
    img.save(path)
    return path


SCENARIOS = {
    "01_clean_invoice.pdf": {},
    "02_duplicate_number.pdf": {"date": "15/03/2026", "items": [("Cloud hosting (annual)", 2, "5,000.00", "10,000.00"), ("Priority support", 4, "2,000.00", "8,000.00")]},
    "03_tampered_total.pdf": {"number": "INV-2026-0043", "date": "16/03/2026", "total": "24,240.00"},
    "04_missing_fields.pdf": {"number": None, "date": None},
    "05_bad_gstin.pdf": {"number": "INV-2026-0044", "date": "17/03/2026", "gstin": "24AABCU9603R1ZX"},
    "06_line_math_error.pdf": {"number": "INV-2026-0045", "date": "18/03/2026", "items": [("Cloud hosting (annual)", 2, "5,000.00", "11,000.00"), ("Priority support", 4, "2,000.00", "8,000.00")]},
}


def generate_all(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = [make_pdf(out / name, **over) for name, over in SCENARIOS.items()]
    files.append(make_png(out / "07_scanned_image.png", number="INV-2026-0050", date="20/03/2026"))
    return files


if __name__ == "__main__":
    for f in generate_all(Path(__file__).resolve().parent.parent / "samples"):
        print("created", f)
