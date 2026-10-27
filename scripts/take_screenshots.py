"""Seeds demo invoices into a running API and captures every review-screen state.
Usage: python -m scripts.take_screenshots --url http://localhost:8000 --key API_KEY --out docs/screenshots
Needs: pip install playwright && python -m playwright install chromium"""
import argparse
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000")
ap.add_argument("--key", required=True)
ap.add_argument("--out", default="docs/screenshots")
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
samples = sorted((Path(__file__).resolve().parent.parent / "samples").glob("0*"))

ids = {}
with httpx.Client(base_url=a.url, headers={"X-API-Key": a.key}, timeout=120) as c:
    for f in samples:
        r = c.post("/invoices/upload", files={"file": (f.name, f.read_bytes())}, data={"source": "email"})
        ids[f.name.split("_")[0]] = r.json()["id"] if r.status_code == 201 else None

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5, color_scheme="light")
    pg = ctx.new_page()

    def shot(name, full=False):
        pg.wait_for_timeout(350)
        pg.screenshot(path=str(out / f"{name}.png"), full_page=full)
        print("saved", name)

    def pick(num):
        pg.click(f'.row[data-id="{ids[num]}"]')
        pg.wait_for_selector(".bar")

    pg.goto(a.url + "/review")
    shot("01_sign_in")
    pg.fill("#key", a.key)
    pg.fill("#who", "Meera (Accounts)")
    pg.click("#go")
    pg.wait_for_selector(".row")
    shot("02_queue_overview")

    pick("01"); shot("03_clean_invoice_ready_to_approve", True)
    pick("03"); shot("04_tampered_total_blocked", True)
    pick("02"); shot("05_duplicate_detected", True)
    pick("05"); shot("06_invalid_gstin", True)
    pick("07"); shot("07_scanned_image_ocr", True)

    pick("04"); shot("08_missing_fields_before_fix", True)
    pg.fill('input[data-k="invoice_number"]', "INV-2026-0046")
    pg.fill('input[data-k="invoice_date"]', "19/03/2026")
    shot("09_human_fixing_fields", True)
    pg.click("#save")
    pg.wait_for_selector(".pill.PENDING_APPROVAL >> nth=0")
    pg.wait_for_timeout(600)
    shot("10_after_recheck_passed", True)

    pick("07")
    pg.fill("#note", "Checked against paper copy")
    pg.click("#ok")
    pg.wait_for_timeout(900)
    shot("11_approved", True)

    pg.click('.tab[data-t="NEEDS_REVIEW"]')
    shot("12_filter_needs_review")

    dark = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5, color_scheme="dark").new_page()
    dark.goto(a.url + "/review")
    dark.fill("#key", a.key); dark.fill("#who", "Meera (Accounts)"); dark.click("#go")
    dark.wait_for_selector(".row")
    dark.click(f'.row[data-id="{ids["03"]}"]'); dark.wait_for_selector(".bar"); dark.wait_for_timeout(400)
    dark.screenshot(path=str(out / "13_dark_mode.png")); print("saved 13_dark_mode")

    try:  # Swagger UI loads its JS from a CDN, so this needs internet access
        pg.goto(a.url + "/docs")
        pg.wait_for_selector(".opblock-tag", timeout=15000)
        shot("14_api_docs_swagger")
    except Exception:
        print("skipped Swagger screenshot (no internet for its CDN assets)")
    b.close()
