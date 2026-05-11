"""rebuild_data.py — regenerate data/data.json without needing keeper/Solar Manager.

Patches out the Solar Manager fetch so the script can run offline.
Usage:
    python rebuild_data.py
"""
import sys
import types

# Stub out 'keeper' so config.py doesn't crash
sys.modules['keeper'] = types.ModuleType('keeper')

# Stub SM credentials so config import succeeds
import importlib, os
os.environ.setdefault('SM_EMAIL',    'stub')
os.environ.setdefault('SM_PASSWORD', 'stub')
os.environ.setdefault('SM_ID',       'stub')

# Now import serve — the config import will succeed via env-var fallback
import serve

# Patch out the live Solar Manager fetch (returns empty dict)
serve.fetch_pv_profile = lambda days_back=45: {}

import json
from pathlib import Path

print("[rebuild] Building dashboard data (no Solar Manager)...")
data = serve.build_dashboard_data()

out = Path(__file__).parent / 'data' / 'data.json'
out.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
size_mb = round(out.stat().st_size / 1_048_576, 1)
print(f"[rebuild] Written {out} ({size_mb} MB)")

# Quick sanity check
slots_with_stack = sum(1 for s in data.get('treSlots', []) if 'pbs' in s)
print(f"[rebuild] TRE slots with bid stack: {slots_with_stack}")
