"""
update_data.py — Auto-download latest market data
==================================================
Downloads auction results from two sources:

  Swissgrid (HTTP scrape):
    • market_data/SRL&TRL/YYYY-PRL-SRL-TRL-Ergebnis.csv
    • market_data/TRE/YYYY-MM-TRE-Ergebnis.csv

  EPEX Spot (SFTP):
    • market_data/Spot/auction_spot_prices_switzerland_YYYY.csv

Usage:
    python update_data.py

Requires paramiko for SFTP:
    pip install paramiko
"""

import io
import re
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Swissgrid ────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.swissgrid.ch"
TENDERS_URL = BASE_URL + "/en/home/customers/topics/ancillary-services/tenders.html"

# ── EPEX Spot SFTP ───────────────────────────────────────────────────────────
from config import SFTP_USER, SFTP_PASS
SFTP_HOST      = "sftp.marketdata.epexspot.com"
SFTP_PORT      = 22
SFTP_REMOTE    = "/switzerland/Day-Ahead Auction/Hourly/Current/Prices_Volumes"

# ── Local paths ──────────────────────────────────────────────────────────────
DATA_DIR    = Path(__file__).parent / "market_data"
SRL_TRL_DIR = DATA_DIR / "SRL&TRL"
TRE_DIR     = DATA_DIR / "TRE"
SPOT_DIR    = DATA_DIR / "Spot"


# ═══════════════════════════════════════════════════════
#  Swissgrid helpers
# ═══════════════════════════════════════════════════════

def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _find_csv_links(html: str) -> list[tuple[str, str]]:
    """Return [(filename, full_url)] for every .csv link found on the tenders page."""
    pattern = r'(/dam/jcr:[a-f0-9\-]+/([^"\'>\s]+\.csv))'
    seen: dict[str, str] = {}
    for path, filename in re.findall(pattern, html):
        if filename not in seen:
            seen[filename] = BASE_URL + path
    return list(seen.items())



def update_swissgrid(results: dict) -> None:
    print("── Swissgrid ───────────────────────────────────────")
    try:
        html = _http_get(TENDERS_URL).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [!] Could not fetch tenders page: {e}")
        return

    links = _find_csv_links(html)
    print(f"  Found {len(links)} CSV link(s) on page")

    # Collect SRL&TRL links and TRE monthly links separately
    srl_trl_links = {}   # filename -> url
    tre_links = {}       # (year, month) -> (filename, url)

    for filename, url in links:
        if re.match(r"^\d{4}-PRL-SRL-TRL-Ergebnis\.csv$", filename):
            srl_trl_links[filename] = url
        elif m := re.match(r"^(\d{4})-(\d{2})-TRE-Ergebnis\.csv$", filename):
            tre_links[(int(m.group(1)), int(m.group(2)))] = (filename, url)

    # SRL&TRL: download all annual files found
    to_download = [(fn, url, SRL_TRL_DIR / fn) for fn, url in srl_trl_links.items()]

    # TRE: only download the latest month (older months are finalized)
    if tre_links:
        latest_key = max(tre_links)
        fn, url = tre_links[latest_key]
        to_download.append((fn, url, TRE_DIR / fn))
        skipped_tre = len(tre_links) - 1
        if skipped_tre:
            print(f"  TRE: skipping {skipped_tre} older month(s), downloading only {fn}")

    for filename, url, dest in to_download:
        existed = dest.exists()
        print(f"  Downloading {filename} …", end=" ", flush=True)
        try:
            data = _http_get(url, timeout=120)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}")
            results["error"].append(filename)
            continue

        # If Swissgrid delivers a ZIP, extract the CSV inside it
        if data[:2] == b'PK':
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    csv_names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
                    if not csv_names:
                        raise ValueError("ZIP contains no CSV files")
                    data = zf.read(csv_names[0])
                    print(f"(extracted {csv_names[0]} from ZIP) ", end="")
            except Exception as e:
                print(f"ZIP extract failed: {e}")
                results["error"].append(filename)
                continue

        if existed and dest.read_bytes() == data:
            print("unchanged")
            results["unchanged"].append(filename)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            status = "new" if not existed else "updated"
            print(f"{len(data)//1024} KB — {status}")
            results[status].append(filename)


# ═══════════════════════════════════════════════════════
#  EPEX Spot SFTP helpers
# ═══════════════════════════════════════════════════════

def update_epex_spot(results: dict) -> None:
    print("── EPEX Spot (SFTP) ────────────────────────────────")
    try:
        import paramiko
    except ImportError:
        print("  [!] paramiko not installed — run: pip install paramiko")
        return

    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
    except Exception as e:
        print(f"  [!] SFTP connection failed: {e}")
        return

    try:
        entries = sftp.listdir_attr(SFTP_REMOTE)
    except Exception as e:
        print(f"  [!] Could not list remote directory: {e}")
        sftp.close()
        transport.close()
        return

    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    spot_pattern = re.compile(r"^auction_spot_prices_switzerland_\d{4}\.csv$")

    for entry in entries:
        if not spot_pattern.match(entry.filename):
            continue
        dest = SPOT_DIR / entry.filename
        remote_path = SFTP_REMOTE + "/" + entry.filename

        if dest.exists() and dest.stat().st_size == entry.st_size:
            print(f"  {entry.filename} — unchanged")
            results["unchanged"].append(entry.filename)
            continue

        existed = dest.exists()
        print(f"  Downloading {entry.filename} …", end=" ", flush=True)
        try:
            sftp.get(remote_path, str(dest))
            status = "new" if not existed else "updated"
            print(f"{entry.st_size//1024} KB — {status}")
            results[status].append(entry.filename)
        except Exception as e:
            print(f"failed: {e}")
            results["error"].append(entry.filename)

    sftp.close()
    transport.close()


# ═══════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════

def main():
    results: dict[str, list[str]] = {"new": [], "updated": [], "unchanged": [], "error": []}

    update_swissgrid(results)
    print()
    update_epex_spot(results)
    print()

    if results["new"]:
        print(f"New ({len(results['new'])}):")
        for f in results["new"]:      print(f"  + {f}")
    if results["updated"]:
        print(f"Updated ({len(results['updated'])}):")
        for f in results["updated"]:  print(f"  ↑ {f}")
    if results["unchanged"]:
        print(f"Unchanged ({len(results['unchanged'])}):")
        for f in results["unchanged"]: print(f"  = {f}")
    if results["error"]:
        print(f"Errors ({len(results['error'])}):")
        for f in results["error"]:    print(f"  ! {f}")

    # If any files changed, trigger a live reload of the running dashboard
    changed = results["new"] + results["updated"]
    if changed:
        print()
        try:
            resp = _http_get("http://localhost:3000/api/reload", timeout=5)
            print("Dashboard reload triggered — refresh your browser in ~4 minutes.")
        except Exception:
            print("Note: Could not reach dashboard server. Restart serve.py to load new data.")
    else:
        print("\nNo changes — dashboard is already up to date.")


if __name__ == "__main__":
    main()
