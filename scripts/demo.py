"""Runs every sample invoice through a running API and prints what happened.
Usage:  python -m scripts.demo [--url http://localhost:8000] [--key API_KEY]"""
import argparse
from pathlib import Path

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000")
ap.add_argument("--key", default="")
a = ap.parse_args()
H = {"X-API-Key": a.key} if a.key else {}
samples = sorted((Path(__file__).resolve().parent.parent / "samples").glob("0*"))

with httpx.Client(base_url=a.url, headers=H, timeout=120) as c:
    print(f"{'file':28} {'status':17} issues")
    print("-" * 90)
    for f in samples:
        r = c.post("/invoices/upload", files={"file": (f.name, f.read_bytes())}, data={"source": "demo"})
        if r.status_code == 409:
            print(f"{f.name:28} {'(already uploaded)':17}")
            continue
        j = r.json()
        print(f"{f.name:28} {j['status']:17} {', '.join(sorted({i['code'] for i in j['validation_issues']})) or '-'}")
    s = c.get("/reports/summary").json()
    t = s["time_saved"]
    print(f"\n{s['total_invoices']} invoices | straight-through {s['straight_through_rate']:.0%} | "
          f"duplicates caught {s['duplicates_detected']} | est. time saved {t['minutes_saved']} of {t['manual_minutes_baseline']} min")
