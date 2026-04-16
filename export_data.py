"""
export_data.py — Build static data snapshot for Vercel deployment
==================================================================
Processes all local market CSV files and fetches live PV data from
Solar Manager, then writes the result to data/data.json.

Run this after update_data.py whenever you want to publish fresh data:

    python update_data.py
    python export_data.py
    git add data/data.json
    git commit -m "Update market data"
    git push

Vercel will auto-deploy within ~30 seconds of the push.
"""

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from serve import build_dashboard_data

OUT_DIR  = Path(__file__).parent / "data"
OUT_FILE = OUT_DIR / "data.json"

print("Building dashboard data snapshot...")
data = build_dashboard_data()

OUT_DIR.mkdir(exist_ok=True)
payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
OUT_FILE.write_text(payload, encoding="utf-8")

size_kb = OUT_FILE.stat().st_size // 1024
print(f"\nWrote {OUT_FILE} ({size_kb} KB)")
print("Next steps:")
print("  git add data/data.json")
print('  git commit -m "Update market data"')
print("  git push")
