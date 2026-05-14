"""
make_slim_data.py — produce a small subset of data/data.json for design iteration.

Run from your local Market Dashboard repo root:
    python make_slim_data.py

Output: data/data_slim.json   (target: under ~3 MB)

The dashboard only renders bid-stack charts for the most recent windows, so
this strips bid stacks from older auctions while keeping summary metrics
(marginal, vwap, awarded, etc.) intact — every chart still has data, just
without the historical heavy detail.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "data" / "data.json"
DST = ROOT / "data" / "data_slim.json"

if not SRC.exists():
    raise SystemExit(f"Not found: {SRC}")

print(f"Loading {SRC} …")
with SRC.open("r", encoding="utf-8") as f:
    d = json.load(f)

# Windows
TAIL_DAILY  = 60   # last ~2 months of daily summaries
TAIL_WEEKLY = 20   # last ~5 months of weekly summaries
TAIL_SLOTS_DAYS = 7    # last 7 days of 15-min TRE slots (672 entries)
# Bid stacks only kept for these many most-recent entries per series:
BIDSTACK_KEEP_WEEKLY = 6      # last 6 weeks
BIDSTACK_KEEP_DAILY  = 10     # last 10 days
BIDSTACK_KEEP_SLOTS  = 2 * 96 # only last 2 days of slots keep bid stacks

def strip_bidstacks(entry):
    """Recursively remove 'bidStack' keys from a dict (in place copy)."""
    if isinstance(entry, dict):
        return {k: strip_bidstacks(v) for k, v in entry.items() if k != "bidStack"}
    if isinstance(entry, list):
        return [strip_bidstacks(x) for x in entry]
    return entry

def tail_with_bidstack_window(seq, total_keep, bidstack_keep):
    """Take last `total_keep`; strip bidStack from all but the last `bidstack_keep`."""
    if not isinstance(seq, list):
        return seq
    tail = seq[-total_keep:]
    cutoff = max(0, len(tail) - bidstack_keep)
    return [strip_bidstacks(e) if i < cutoff else e for i, e in enumerate(tail)]

slim = {
    "trlWeekly":  tail_with_bidstack_window(d.get("trlWeekly", []),  TAIL_WEEKLY, BIDSTACK_KEEP_WEEKLY),
    "trlDaily":   tail_with_bidstack_window(d.get("trlDaily", []),   TAIL_DAILY,  BIDSTACK_KEEP_DAILY),
    "srlWeekly":  tail_with_bidstack_window(d.get("srlWeekly", []),  TAIL_WEEKLY, BIDSTACK_KEEP_WEEKLY),
    "srlDaily":   tail_with_bidstack_window(d.get("srlDaily", []),   TAIL_DAILY,  BIDSTACK_KEEP_DAILY),
    "treDaily":   tail_with_bidstack_window(d.get("treDaily", []),   TAIL_DAILY,  BIDSTACK_KEEP_DAILY),
    "treSlots":   tail_with_bidstack_window(d.get("treSlots", []),   TAIL_SLOTS_DAYS * 96, BIDSTACK_KEEP_SLOTS),
    "spotDaily":  (d.get("spotDaily") or [])[-TAIL_DAILY:],
    "spotHourly": (d.get("spotHourly") or [])[-TAIL_DAILY:],
    "pvProfile":  d.get("pvProfile"),
    "processingMs": d.get("processingMs", 0),
}

# Carry over any other top-level keys, stripped of bid stacks just in case.
for k, v in d.items():
    if k not in slim:
        slim[k] = strip_bidstacks(v)

print(f"Writing {DST} …")
with DST.open("w", encoding="utf-8") as f:
    json.dump(slim, f, ensure_ascii=False, separators=(",", ":"))

size_kb = DST.stat().st_size / 1024
print(f"Done. {DST.name} = {size_kb:.0f} KB")
print(f"  weekly={TAIL_WEEKLY} (last {BIDSTACK_KEEP_WEEKLY} keep bidStack)")
print(f"  daily={TAIL_DAILY} (last {BIDSTACK_KEEP_DAILY} keep bidStack)")
print(f"  TRE slots={TAIL_SLOTS_DAYS} days (last 30 keep bidStack)")
