"""
serve.py — Helion Regelenergiemarkt Dashboard Server
=====================================================
Parses market CSV files, aggregates them, and serves the dashboard
on http://localhost:3000

Usage:
    python serve.py
    # or, using the bundled Python:
    "C:/Users/ThijsAntoniedeBoer/AppData/Local/Python/pythoncore-3.14-64/python.exe" serve.py
"""
import sys
# Ensure stdout can handle UTF-8 on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import csv
import json
import os
import re
import time
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

PORT = 3000
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "market_data"

# ── Solar Manager API ─────────────────────────────────────────────────────────
from config import SM_EMAIL, SM_PASSWORD, SM_ID, SM_BASE_URL

def fetch_pv_profile(days_back: int = 45) -> dict:
    """Fetch 15-min PV production data from Solar Manager and return
    a dict { 'YYYY-MM-DD|HH:MM': kW } keyed in Europe/Zurich local time."""
    import requests as _req
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from zoneinfo import ZoneInfo
    zurich = ZoneInfo("Europe/Zurich")
    try:
        login = _req.post(
            f"{SM_BASE_URL}/v1/oauth/login",
            json={"email": SM_EMAIL, "password": SM_PASSWORD},
            timeout=30,
        )
        login.raise_for_status()
        token = login.json().get("accessToken") or login.json().get("access_token")
        if not token:
            raise RuntimeError("No access token in login response")

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        now   = _dt.now(_tz.utc)
        start = now - _td(days=days_back)
        resp = _req.get(
            f"{SM_BASE_URL}/v3/users/{SM_ID}/data/range",
            params={
                "from":     start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to":       now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "interval": 900,
            },
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        raw    = resp.json()
        points = raw if isinstance(raw, list) else raw.get("data", [])

        pv_profile: dict = {}
        for pt in points:
            t_str = pt.get("t")
            p_wh  = pt.get("pWh")
            if not t_str or p_wh is None:
                continue
            dt_local = _dt.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(zurich)
            key = f"{dt_local.strftime('%Y-%m-%d')}|{dt_local.strftime('%H:%M')}"
            kw  = round(float(p_wh) / 250 * 10) / 10   # Wh/15 min → kW
            if kw > 0:
                pv_profile[key] = kw

        print(f"  [solar] {len(pv_profile)} PV slots fetched (last {days_back} days, Europe/Zurich)")
        return pv_profile

    except Exception as exc:
        print(f"  [solar] WARNING: could not fetch PV data — {exc}")
        return {}

def _iso_week_monday(year: int, week: int) -> str:
    """Return ISO Monday date for a given year and week number."""
    import datetime
    d = datetime.date.fromisocalendar(year, week, 1)
    return d.isoformat()

# 4-hour block labels
BLOCKS = ['00:00-04:00', '04:00-08:00', '08:00-12:00',
          '12:00-16:00', '16:00-20:00', '20:00-24:00']

def parse_num(s: str) -> float:
    if not s or not s.strip():
        return 0.0
    try:
        return float(s.replace(',', '.').strip())
    except ValueError:
        return 0.0


def block_from_desc(desc: str) -> str | None:
    """Extract '00:00-04:00' style block label from description string."""
    m = re.search(r'(\d{2}:\d{2})\s+bis\s+(\d{2}:\d{2})', desc)
    if not m:
        return None
    start, end = m.group(1), m.group(2)
    # Normalise: '24:00' -> '24:00', keep as-is
    return f'{start}-{end}'


# ─── Accumulator helpers ──────────────────────────────────────────────────────

def _dir_entry():
    return {'offered': 0.0, 'awarded': 0.0, 'total_cost': 0.0, 'max_price': 0.0, 'bid_prices': []}


def accum_trl(container: dict, key: str, direction: str,
              offered: float, awarded: float, cap_price: float, costs: float):
    if key not in container:
        container[key] = {}
    if direction not in container[key]:
        container[key][direction] = _dir_entry()
    r = container[key][direction]
    r['offered'] += offered
    if awarded > 0:
        r['awarded']    += awarded
        r['total_cost'] += costs
        if cap_price > r['max_price']:
            r['max_price'] = cap_price
        if cap_price > 0:
            r['bid_prices'].append(cap_price)


def fin_trl_dir(r: dict) -> dict:
    prices = sorted(r.get('bid_prices', []))
    n = len(prices)
    if n == 0:
        median_bid = None
    elif n % 2 == 1:
        median_bid = round(prices[n // 2] * 10) / 10
    else:
        median_bid = round((prices[n // 2 - 1] + prices[n // 2]) / 2 * 10) / 10
    return {
        'offered':   round(r['offered']),
        'awarded':   round(r['awarded']),
        'marginal':  round(r['max_price'] * 10) / 10 if r['max_price'] > 0 else None,
        'medianBid': median_bid,
        'awardRate': round(r['awarded'] / r['offered'] * 1000) / 10 if r['offered'] > 0 else 0,
    }


# ─── Main data build ──────────────────────────────────────────────────────────

def build_dashboard_data() -> dict:
    t0 = time.time()
    print("[Helion] Loading market data (large files - may take 30-120 s)...")

    # ── TRL + SRL ─────────────────────────────────────────────────────────────
    # Columns: Ausschreibung;Beschreibung;AngebVol;Einheit;ZugesVol;Einheit;
    #          Leistungspreis;Einheit;Kosten;Einheit;Preis;Einheit;Land;…
    # Indices: 0               1       2      3       4      5       6
    #          7               8       9      10     11     12

    # TRL maps
    weekly_map    = {}   # { 'KW01': {'up': {...}, 'down': {...}} }
    ant_map       = {}   # TRL anticipated (DOWN only: TRL-_26_KWxx_S1)
    daily_trl_map = {}   # { 'date': { 'block': { 'up':{}, 'down':{} } } }

    # SRL maps
    srl_weekly_map = {}  # { 'KW01': {'up': {...}, 'down': {...}} }
    srl_ant_map    = {}  # SRL anticipated (both UP and DOWN: SRL_26_KWxx_S1)
    srl_daily_map  = {}  # { 'date': { 'block': { 'up':{}, 'down':{} } } }

    trl_srl_files = sorted((DATA_DIR / "SRL&TRL").glob("*-PRL-SRL-TRL-Ergebnis.csv"))
    for trl_file in trl_srl_files:
        print(f"  [parse] {trl_file.name}...")
        with open(trl_file, encoding='latin-1', newline='') as f:
            reader = csv.reader(f, delimiter=';')
            next(reader, None)  # skip header
            for row in reader:
                if not row:
                    continue
                auction   = row[0].strip()
                desc      = row[1].strip() if len(row) > 1 else ''
                offered   = parse_num(row[2]) if len(row) > 2 else 0.0
                awarded   = parse_num(row[4]) if len(row) > 4 else 0.0
                cap_price = parse_num(row[6]) if len(row) > 6 else 0.0
                costs     = parse_num(row[8]) if len(row) > 8 else 0.0
                # Direction: TRL+/TRL- prefix (older TRL files) or SRL+/SRL- in description
                # (older SRL files), or UP/DOWN in description (2025+ both markets)
                if auction.startswith('TRL+'):
                    direction = 'up'
                elif auction.startswith('TRL-'):
                    direction = 'down'
                elif 'SRL+' in desc:
                    direction = 'up'
                elif 'SRL-' in desc:
                    direction = 'down'
                elif 'DOWN' in desc:
                    direction = 'down'
                else:
                    direction = 'up'

                # ── TRL ──────────────────────────────────────────────────────
                # Weekly regular: TRL_YY_KWnn  /  TRL+_YY_KWnn  /  TRL-_YY_KWnn
                m = re.match(r'^TRL[+-]?_(\d{2})_(KW\d+)$', auction)
                if m:
                    yy, kw = int(m.group(1)), m.group(2)
                    key = f"{2000+yy}_{kw}"
                    accum_trl(weekly_map, key, direction, offered, awarded, cap_price, costs)
                    continue

                # Anticipated weekly: TRL-_YY_KWnn_S1  /  TRL+_YY_KWnn_S1
                m = re.match(r'^TRL[+-]?_(\d{2})_(KW\d+)_S1$', auction)
                if m:
                    yy, kw = int(m.group(1)), m.group(2)
                    key = f"{2000+yy}_{kw}"
                    accum_trl(ant_map, key, direction, offered, awarded, cap_price, costs)
                    continue

                # Daily: TRL_YY_MM_DD  /  TRL+_YY_MM_DD  /  TRL-_YY_MM_DD
                m = re.match(r'^TRL[+-]?_(\d{2})_(\d{2})_(\d+)$', auction)
                if m:
                    yy, mm, dd = int(m.group(1)), m.group(2), int(m.group(3))
                    date  = f"{2000+yy}-{mm}-{dd:02d}"
                    block = block_from_desc(desc) or 'unknown'
                    if date not in daily_trl_map:
                        daily_trl_map[date] = {}
                    accum_trl(daily_trl_map[date], block, direction,
                              offered, awarded, cap_price, costs)
                    continue

                # ── SRL ──────────────────────────────────────────────────────
                # Weekly regular: SRL_YY_KWnn
                m = re.match(r'^SRL[+-]?_(\d{2})_(KW\d+)$', auction)
                if m:
                    yy, kw = int(m.group(1)), m.group(2)
                    key = f"{2000+yy}_{kw}"
                    accum_trl(srl_weekly_map, key, direction, offered, awarded, cap_price, costs)
                    continue

                # Anticipated weekly (both UP and DOWN): SRL_YY_KWnn_S1
                m = re.match(r'^SRL[+-]?_(\d{2})_(KW\d+)_S1$', auction)
                if m:
                    yy, kw = int(m.group(1)), m.group(2)
                    key = f"{2000+yy}_{kw}"
                    accum_trl(srl_ant_map, key, direction, offered, awarded, cap_price, costs)
                    continue

                # Daily: SRL_YY_MM_DD
                m = re.match(r'^SRL[+-]?_(\d{2})_(\d{2})_(\d+)$', auction)
                if m:
                    yy, mm, dd = int(m.group(1)), m.group(2), int(m.group(3))
                    date  = f"{2000+yy}-{mm}-{dd:02d}"
                    block = block_from_desc(desc) or 'unknown'
                    if date not in srl_daily_map:
                        srl_daily_map[date] = {}
                    accum_trl(srl_daily_map[date], block, direction,
                              offered, awarded, cap_price, costs)

    # ── TRE ──────────────────────────────────────────────────────────────────
    # Columns: Ausschreibung;Von;Bis;Produkt;AngebMenge;Einheit;AbgerMenge;Einheit;Preis;Einheit;Status
    # Indices: 0              1   2    3       4           5       6          7       8      9       10
    #
    # Per (date, slot): track offered, activated, and marginal price
    #   UP  marginal = max(Preis) among aktiviert rows  (most expensive activated UP bid)
    #   DOWN marginal = min(Preis) among aktiviert rows (cheapest activated DOWN bid)
    #
    # treSlotData stores per-day per-slot data for client-side filtering.
    # Format: { 'date|slot': { po, pa, pm, no, na, nm } }
    # where po=pos_offered, pa=pos_activated, pm=pos_marginal(max),
    #       no=neg_offered, na=neg_activated, nm=neg_marginal(min)

    slot_map = {}   # { 'date|HH:MM': { 'pos': {...}, 'neg': {...} } }

    tre_files = sorted((DATA_DIR / "TRE").rglob("*-TRE-Ergebnis.csv"), key=lambda p: p.name)
    for path in tre_files:
        print(f"  [parse] {path.name}...")
        with open(path, encoding='latin-1', newline='') as f:
            reader = csv.reader(f, delimiter=';')
            next(reader, None)  # skip header
            for row in reader:
                if not row:
                    continue
                auction   = row[0].strip()
                slot_from = row[1].strip() if len(row) > 1 else ''
                product   = row[3].strip() if len(row) > 3 else ''
                offered   = parse_num(row[4]) if len(row) > 4 else 0.0
                activated = parse_num(row[6]) if len(row) > 6 else 0.0
                price     = parse_num(row[8]) if len(row) > 8 else 0.0
                status    = row[10].strip() if len(row) > 10 else ''

                m = re.match(r'^TRE_(\d{2})_(\d{2})_(\d+)$', auction)
                if not m:
                    continue
                yy, mm, dd = int(m.group(1)), m.group(2), int(m.group(3))
                date = f"{2000+yy}-{mm}-{dd:02d}"

                # direction: sa+ / da+ = UP (pos); sa- = DOWN (neg)
                is_pos = 'sa+' in product or 'da+' in product
                direction = 'pos' if is_pos else 'neg'

                key = f"{date}|{slot_from}"
                if key not in slot_map:
                    slot_map[key] = {
                        'pos': {'offered': 0.0, 'activated': 0.0, 'max_price': None},
                        'neg': {'offered': 0.0, 'activated': 0.0, 'min_price': None},
                    }
                r = slot_map[key][direction]
                r['offered'] += offered

                if status == 'aktiviert' and activated > 0:
                    r['activated'] += activated
                    if is_pos:
                        if r['max_price'] is None or price > r['max_price']:
                            r['max_price'] = price
                    else:
                        if r['min_price'] is None or price < r['min_price']:
                            r['min_price'] = price

    # ── Finalize TRL + SRL ───────────────────────────────────────────────────

    def sort_year_kw(k):
        # key format: '2026_KW01'
        m = re.match(r'^(\d{4})_KW(\d+)$', k)
        return (int(m.group(1)), int(m.group(2))) if m else (9999, 0)

    def key_to_date(k):
        """'2026_KW01' -> ISO Monday date string."""
        m = re.match(r'^(\d{4})_KW(\d+)$', k)
        if not m:
            return ''
        return _iso_week_monday(int(m.group(1)), int(m.group(2)))

    def key_to_id(k):
        """'2026_KW01' -> 'KW01' display label."""
        m = re.match(r'^(\d{4})_KW(\d+)$', k)
        if not m:
            return k
        return f"KW{m.group(2)}"

    def fin_weekly(w_map, a_map, ant_both_directions=False):
        """Finalize a weekly map into a list of entries.
        ant_both_directions: if True, anticipated has both 'up' and 'down' keys;
                             if False, anticipated is treated as down-only scalar."""
        result = []
        for kw in sorted(w_map, key=sort_year_kw):
            dirs = w_map[kw]
            entry = {'id': key_to_id(kw), 'date': key_to_date(kw)}
            for d, r in dirs.items():
                entry[d] = fin_trl_dir(r)
            if kw in a_map:
                if ant_both_directions:
                    ant_entry = {}
                    for d, r in a_map[kw].items():
                        ant_entry[d] = fin_trl_dir(r)
                    entry['anticipated'] = ant_entry
                else:
                    # TRL: anticipated is down-only
                    entry['anticipated'] = fin_trl_dir(a_map[kw].get('down', _dir_entry()))
            result.append(entry)
        return result

    def fin_daily(d_map):
        """Finalize a daily-block map into a list of day entries."""
        result = []
        for date in sorted(d_map):
            blocks_data = []
            blocks_present = d_map[date]
            for blk in BLOCKS:
                if blk not in blocks_present:
                    continue
                b = blocks_present[blk]
                blk_entry = {'block': blk}
                for d, r in b.items():
                    blk_entry[d] = fin_trl_dir(r)
                blocks_data.append(blk_entry)
            result.append({'date': date, 'blocks': blocks_data})
        return result

    trl_weekly = fin_weekly(weekly_map, ant_map, ant_both_directions=False)
    trl_daily  = fin_daily(daily_trl_map)
    srl_weekly = fin_weekly(srl_weekly_map, srl_ant_map, ant_both_directions=True)
    srl_daily  = fin_daily(srl_daily_map)

    # ── Finalize TRE ─────────────────────────────────────────────────────────

    # treSlots: compact array sorted by date then slot
    tre_slots = []
    for key in sorted(slot_map):
        date_str, slot = key.split('|', 1)
        s = slot_map[key]
        pos = s['pos']
        neg = s['neg']
        tre_slots.append({
            'd':  date_str,
            's':  slot,
            'po': round(pos['offered']),
            'pa': round(pos['activated']),
            'pm': round(pos['max_price'] * 100) / 100 if pos['max_price'] is not None else None,
            'no': round(neg['offered']),
            'na': round(neg['activated']),
            'nm': round(neg['min_price'] * 100) / 100 if neg['min_price'] is not None else None,
        })

    # treDaily: aggregate daily totals from slot_map
    daily_agg = {}
    for key, s in slot_map.items():
        date_str = key.split('|', 1)[0]
        if date_str not in daily_agg:
            daily_agg[date_str] = {
                'pos': {'offered': 0.0, 'activated': 0.0, 'max_price': None},
                'neg': {'offered': 0.0, 'activated': 0.0, 'min_price': None},
            }
        da = daily_agg[date_str]
        pos, neg = s['pos'], s['neg']
        da['pos']['offered']   += pos['offered']
        da['pos']['activated'] += pos['activated']
        if pos['max_price'] is not None:
            if da['pos']['max_price'] is None or pos['max_price'] > da['pos']['max_price']:
                da['pos']['max_price'] = pos['max_price']
        da['neg']['offered']   += neg['offered']
        da['neg']['activated'] += neg['activated']
        if neg['min_price'] is not None:
            if da['neg']['min_price'] is None or neg['min_price'] < da['neg']['min_price']:
                da['neg']['min_price'] = neg['min_price']

    tre_daily = []
    for date_str in sorted(daily_agg):
        da = daily_agg[date_str]
        p, n = da['pos'], da['neg']
        tre_daily.append({
            'date': date_str,
            'pos': {
                'offered':        round(p['offered']),
                'activated':      round(p['activated']),
                'marginal':       round(p['max_price'] * 100) / 100 if p['max_price'] is not None else None,
                'activationRate': round(p['activated'] / p['offered'] * 1000) / 10 if p['offered'] > 0 else 0,
            },
            'neg': {
                'offered':        round(n['offered']),
                'activated':      round(n['activated']),
                'marginal':       round(n['min_price'] * 100) / 100 if n['min_price'] is not None else None,
                'activationRate': round(n['activated'] / n['offered'] * 1000) / 10 if n['offered'] > 0 else 0,
            },
        })

    # ── Spot Market ──────────────────────────────────────────────────────────
    # 2022 CSV: one row per hour — MTU (CET/CEST), Area, Resolution, Price, Currency
    # 2023+ CSV: one row per day — Delivery day, Hour 1..Hour 3A/3B..Hour 24, aggregates
    # TB spread definitions (sorted hourly rank):
    #   TB1 = max(hours)       – min(hours)       (1st highest – 1st lowest)
    #   TB2 = 2nd_highest      – 2nd_lowest
    #   TB3 = 3rd_highest      – 3rd_lowest
    #   TB4 = 4th_highest      – 4th_lowest

    spot_hour_map = {}   # { 'YYYY-MM-DD': [price_h0..price_h23] }  (None for missing slots)

    spot_files = sorted((DATA_DIR / "Spot").glob("auction_spot_prices_switzerland_*.csv"))
    for path in spot_files:
        is_2022 = '2022' in path.name
        with open(path, encoding='utf-8', errors='replace', newline='') as f:
            reader = csv.reader(f)
            if is_2022:
                next(reader, None)   # skip header row
                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    mtu = row[0].strip()
                    # Format: "  01/01/2022 00:00 - 01/01/2022 01:00"
                    try:
                        start_part = mtu.split(' - ')[0].strip()
                        date_part, time_part = start_part.rsplit(' ', 1)
                        dd, mm, yyyy = date_part.split('/')
                        date_str = f"{yyyy}-{mm}-{dd}"
                        hour = int(time_part.split(':')[0])   # 0–23
                        price = parse_num(row[3].strip() if len(row) > 3 else '')
                    except (ValueError, IndexError):
                        continue
                    if price is None:
                        continue
                    if date_str not in spot_hour_map:
                        spot_hour_map[date_str] = [None] * 24
                    if 0 <= hour < 24:
                        spot_hour_map[date_str][hour] = round(price * 100) / 100
            else:
                next(reader, None)   # skip metadata comment line
                header = next(reader, None)
                if not header:
                    continue
                col = {h.strip(): i for i, h in enumerate(header)}
                # Map 24 hour slots to column indices (Hour 3A covers the 3 o'clock slot)
                h_col_indices = []
                for h_num in range(1, 25):
                    name = 'Hour 3A' if h_num == 3 else f'Hour {h_num}'
                    h_col_indices.append(col.get(name))

                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    raw_date = row[0].strip()
                    try:
                        dd, mm, yyyy = raw_date.split('/')
                        date_str = f"{yyyy}-{mm}-{dd}"
                    except ValueError:
                        continue
                    hours = []
                    for idx in h_col_indices:
                        if idx is None or idx >= len(row):
                            hours.append(None)
                        else:
                            val = parse_num(row[idx].strip())
                            hours.append(round(val * 100) / 100 if val is not None else None)
                    spot_hour_map[date_str] = hours

    # Build spotHourly and spotDaily with sorted-rank TB spreads
    spot_hourly = []
    spot_daily  = []
    for date_str in sorted(spot_hour_map.keys()):
        hours = spot_hour_map[date_str]
        valid = sorted(p for p in hours if p is not None)
        n = len(valid)
        avg_price = round(sum(valid) / n * 100) / 100 if n else None

        def ranked_spread(rank):
            if n > rank:
                return round((valid[-(rank + 1)] - valid[rank]) * 100) / 100
            return None

        spot_hourly.append({'date': date_str, 'h': hours})
        spot_daily.append({
            'date': date_str,
            'avg':  avg_price,
            'maxH': round(valid[-1] * 100) / 100 if valid else None,
            'tb1':  ranked_spread(0),
            'tb2':  ranked_spread(1),
            'tb3':  ranked_spread(2),
            'tb4':  ranked_spread(3),
        })

    # ── PV profile — live data from Solar Manager API ─────────────────────────
    # Fetches last 45 days at 15-min resolution.
    # Keys: "YYYY-MM-DD|HH:MM" in Europe/Zurich local time; values: kW.
    print("  [solar] Fetching live PV data from Solar Manager...")
    pv_profile = fetch_pv_profile(days_back=45)

    ms = round((time.time() - t0) * 1000)
    print(
        f"[Helion] Data ready in {ms/1000:.1f}s -- "
        f"TRL weekly: {len(trl_weekly)} wks, TRL daily: {len(trl_daily)} days, "
        f"SRL weekly: {len(srl_weekly)} wks, SRL daily: {len(srl_daily)} days, "
        f"TRE daily: {len(tre_daily)} days, TRE slots: {len(tre_slots)} rows, "
        f"Spot: {len(spot_daily)} days, {len(spot_hourly)} hourly-day entries, "
        f"PV profile: {len(pv_profile)} slots"
    )

    from datetime import datetime, timezone
    return {
        'trlWeekly':    trl_weekly,
        'trlDaily':     trl_daily,
        'srlWeekly':    srl_weekly,
        'srlDaily':     srl_daily,
        'treDaily':     tre_daily,
        'treSlots':     tre_slots,
        'spotHourly':   spot_hourly,
        'spotDaily':    spot_daily,
        'pvProfile':    pv_profile,
        'processedAt':  datetime.now(timezone.utc).isoformat(),
        'processingMs': ms,
    }


# ─── HTTP Server ──────────────────────────────────────────────────────────────

DASHBOARD_DATA = None   # populated by background thread
RELOADING      = False  # True while a reload is in progress

MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript',
    '.css':  'text/css',
    '.json': 'application/json',
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.ico':  'image/x-icon',
    '.woff2': 'font/woff2',
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default request logging

    def do_GET(self):
        path = self.path.split('?')[0]

        # ── API ──
        if path == '/api/data':
            if DASHBOARD_DATA is None:
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":"Data still loading, please retry"}')
                return
            payload = json.dumps(DASHBOARD_DATA, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
            return

        # ── Reload trigger ──
        if path == '/api/reload':
            global RELOADING
            if RELOADING:
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"already reloading"}')
                return
            RELOADING = True
            Thread(target=load_data_background, daemon=True).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"reloading"}')
            return

        # ── Static files ──
        rel = 'index.html' if path in ('/', '') else path.lstrip('/')
        file_path = (BASE_DIR / rel).resolve()

        # Security check: stay within BASE_DIR
        try:
            file_path.relative_to(BASE_DIR.resolve())
        except ValueError:
            self.send_response(403)
            self.end_headers()
            return

        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')
            return

        ext = file_path.suffix.lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        data = file_path.read_bytes()

        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def load_data_background():
    global DASHBOARD_DATA, RELOADING
    DASHBOARD_DATA = build_dashboard_data()
    RELOADING = False


if __name__ == '__main__':
    import traceback as _tb

    # Start data loading in background so HTTP server starts immediately
    t = Thread(target=load_data_background, daemon=True)
    t.start()

    try:
        server = HTTPServer(('localhost', PORT), Handler)
    except OSError as e:
        print(f"\n[Helion] ERROR: Cannot bind to port {PORT}: {e}")
        print(f"         Another process may already be using port {PORT}.")
        print(f"         Run this to find it:  netstat -ano | findstr :{PORT}")
        sys.exit(1)

    print(f"[Helion] Server running at http://localhost:{PORT}")
    print(f"         (data processing in background - dashboard shows loading screen until ready)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Helion] Server stopped.")
    except Exception as e:
        print(f"\n[Helion] Unexpected error: {e}")
        _tb.print_exc()
