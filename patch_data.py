"""
patch_data.py — Incremental daily update for GitHub Actions
===========================================================
Downloads only the current-period CSV files:
  - Current year's SRL/TRL file  (from Swissgrid, public)
  - Current month's TRE file     (from Swissgrid, public)
  - Current year's Spot file     (from EPEX SFTP, credentials required)

Parses those files, merges the results into the existing data/data.json
(which holds the full history), fetches live PV data, and writes the
updated JSON. No historical CSVs needed.

Used by .github/workflows/update.yml — run manually or via Task Scheduler.
"""

import io, json, re, sys, tempfile, zipfile, urllib.request, urllib.error
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import SFTP_USER, SFTP_PASS

BASE_DIR  = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "data.json"

SWISSGRID_TENDERS = "https://www.swissgrid.ch/en/home/customers/topics/ancillary-services/tenders.html"
SWISSGRID_BASE    = "https://www.swissgrid.ch"
SFTP_HOST         = "sftp.marketdata.epexspot.com"
SFTP_PORT         = 22
SFTP_REMOTE       = "/switzerland/Day-Ahead Auction/Hourly/Current/Prices_Volumes"


# ── Download helpers ──────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _extract_if_zip(data: bytes) -> bytes:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("ZIP contains no CSV files")
            return zf.read(csv_names[0])
    return data


def _find_csv_links(html: str) -> dict:
    """Return {filename: full_url} for every .csv link on the page."""
    pattern = r'(/dam/jcr:[a-f0-9\-]+/([^"\'>\s]+\.csv))'
    links = {}
    for path, filename in re.findall(pattern, html):
        if filename not in links:
            links[filename] = SWISSGRID_BASE + path
    return links


# ── Download current-period files to a temp directory ────────────────────────

def download_current_files(dest_dir: Path):
    today = date.today()
    year  = today.year
    month = today.strftime("%m")

    (dest_dir / "SRL&TRL").mkdir(parents=True, exist_ok=True)
    (dest_dir / "TRE").mkdir(parents=True, exist_ok=True)
    (dest_dir / "Spot").mkdir(parents=True, exist_ok=True)

    # ── Swissgrid (public) ────────────────────────────────────────────────────
    print("  Fetching Swissgrid tenders page...")
    html  = _http_get(SWISSGRID_TENDERS).decode("utf-8", errors="replace")
    links = _find_csv_links(html)

    fn = f"{year}-PRL-SRL-TRL-Ergebnis.csv"
    if fn in links:
        print(f"  Downloading {fn}...", end=" ", flush=True)
        raw = _extract_if_zip(_http_get(links[fn]))
        (dest_dir / "SRL&TRL" / fn).write_bytes(raw)
        print(f"{len(raw)//1024} KB")
    else:
        print(f"  WARNING: {fn} not found on Swissgrid page")

    fn = f"{year}-{month}-TRE-Ergebnis.csv"
    if fn in links:
        print(f"  Downloading {fn}...", end=" ", flush=True)
        raw = _extract_if_zip(_http_get(links[fn]))
        (dest_dir / "TRE" / fn).write_bytes(raw)
        print(f"{len(raw)//1024} KB")
    else:
        print(f"  WARNING: {fn} not found on Swissgrid page")

    # ── EPEX Spot SFTP ────────────────────────────────────────────────────────
    print("  Connecting to EPEX SFTP...")
    try:
        import paramiko
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        fn = f"auction_spot_prices_switzerland_{year}.csv"
        local = dest_dir / "Spot" / fn
        print(f"  Downloading {fn}...", end=" ", flush=True)
        sftp.get(SFTP_REMOTE + "/" + fn, str(local))
        print(f"{local.stat().st_size//1024} KB")
        sftp.close()
        transport.close()
    except Exception as e:
        print(f"  WARNING: EPEX SFTP failed: {e}")


# ── Merge partial data into existing JSON ─────────────────────────────────────

def merge_data(existing: dict, partial: dict) -> dict:
    """
    Replace current-period entries in existing with freshly parsed ones.
    - TRL / SRL / Spot: replace entries from the current calendar year
    - TRE:              replace entries from the current calendar month
    - PV profile:       replace entirely (always last 45 days from API)
    """
    today      = date.today()
    year_str   = str(today.year)          # e.g. "2026"
    month_str  = today.strftime("%Y-%m")  # e.g. "2026-04"

    def patch_by_date(key: str, date_field: str, keep_pred):
        kept = [e for e in existing.get(key, []) if keep_pred(e[date_field])]
        kept.extend(partial.get(key, []))
        kept.sort(key=lambda e: e[date_field])
        existing[key] = kept

    # TRL / SRL: keyed by week start date — replace current year
    patch_by_date("trlWeekly", "date", lambda d: not d.startswith(year_str))
    patch_by_date("trlDaily",  "date", lambda d: not d.startswith(year_str))
    patch_by_date("srlWeekly", "date", lambda d: not d.startswith(year_str))
    patch_by_date("srlDaily",  "date", lambda d: not d.startswith(year_str))

    # TRE daily: replace current month
    patch_by_date("treDaily", "date", lambda d: not d.startswith(month_str))

    # TRE slots: 'd' field — replace current month
    kept_slots = [s for s in existing.get("treSlots", []) if not s["d"].startswith(month_str)]
    kept_slots.extend(partial.get("treSlots", []))
    kept_slots.sort(key=lambda s: (s["d"], s["s"]))
    existing["treSlots"] = kept_slots

    # Spot: replace current year
    patch_by_date("spotHourly", "date", lambda d: not d.startswith(year_str))
    patch_by_date("spotDaily",  "date", lambda d: not d.startswith(year_str))

    # PV profile: always replace entirely (fresh 45-day window from API)
    existing["pvProfile"] = partial.get("pvProfile", {})

    existing["processedAt"]  = partial["processedAt"]
    existing["processingMs"] = partial["processingMs"]

    return existing


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not DATA_FILE.exists():
        print("ERROR: data/data.json not found. Run export_data.py first to create it.")
        sys.exit(1)

    print("Loading existing data/data.json...")
    existing = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        print("\n── Downloading current-period files ──────────────────")
        download_current_files(tmp_dir)

        print("\n── Parsing downloaded files ──────────────────────────")
        from serve import build_dashboard_data
        partial = build_dashboard_data(data_dir=tmp_dir)

    print("\n── Merging into existing data ────────────────────────")
    merged = merge_data(existing, partial)

    payload = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    DATA_FILE.write_text(payload, encoding="utf-8")

    size_kb = DATA_FILE.stat().st_size // 1024
    print(f"\nWrote data/data.json ({size_kb} KB)")


if __name__ == "__main__":
    main()
