"""
make_slim_data.py — produce a small subset of data/data.json for design iteration.

Run from your local Market Dashboard repo root:
    python make_slim_data.py

Output: data/data_slim.json   (under ~1 MB, last 3 months detailed + 6 months weekly)

Upload data_slim.json to the chat once it's generated.
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

# Tail counts — tweak if you want more/less history in the slim file.
TAIL_DAILY = 90    # last ~3 months for daily series
TAIL_WEEKLY = 26   # last ~6 months for weekly series
TAIL_SLOTS = 30    # bid stacks only exist for the last 30 days anyway

def tail(key, n):
    v = d.get(key)
    if isinstance(v, list):
        return v[-n:]
    return v

slim = {
    "trlWeekly":   tail("trlWeekly", TAIL_WEEKLY),
    "trlDaily":    tail("trlDaily",  TAIL_DAILY),
    "srlWeekly":   tail("srlWeekly", TAIL_WEEKLY),
    "srlDaily":    tail("srlDaily",  TAIL_DAILY),
    "treDaily":    tail("treDaily",  TAIL_DAILY),
    "treSlots":    tail("treSlots",  TAIL_SLOTS * 96),  # 96 fifteen-min slots/day
    "spotDaily":   tail("spotDaily", TAIL_DAILY),
    "spotHourly":  tail("spotHourly", TAIL_DAILY),
    "pvProfile":   d.get("pvProfile"),
    "processingMs": d.get("processingMs", 0),
}

# Carry over any other top-level keys we didn't slim, just in case.
for k, v in d.items():
    if k not in slim:
        slim[k] = v

print(f"Writing {DST} …")
with DST.open("w", encoding="utf-8") as f:
    json.dump(slim, f, ensure_ascii=False, separators=(",", ":"))

size_kb = DST.stat().st_size / 1024
print(f"Done. {DST.name} = {size_kb:.0f} KB")
print(f"Trim sizes: weekly={TAIL_WEEKLY}, daily={TAIL_DAILY}, slots={TAIL_SLOTS} days")
