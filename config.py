# config.py — loads credentials from Keeper Secrets Manager at startup.
# Locally: requires the keeper package and a valid keeper_config.json.
# GitHub Actions: falls back to SM_EMAIL / SM_PASSWORD / SFTP_USER / SFTP_PASS env vars.

import os

try:
    from keeper import Keeper
    _k = Keeper()

    # Mein Tresor → Energysolutions → HelionONE SE
    SM_EMAIL    = _k.get_field("HelionONE SE", "login")
    SM_PASSWORD = _k.get_password("HelionONE SE")

    # Mein Tresor → Helion EPEX Marketdata
    SFTP_USER = _k.get_field("Helion EPEX Marketdata", "login")
    SFTP_PASS = _k.get_password("Helion EPEX Marketdata")

    if not SM_PASSWORD:
        raise RuntimeError("Keeper returned no password for 'HelionONE SE' — check secret title")
    if not SFTP_PASS:
        raise RuntimeError("Keeper returned no password for 'Helion EPEX Marketdata' — check secret title")

except Exception as _exc:
    # Fall back to environment variables (used in GitHub Actions)
    SM_EMAIL    = os.environ.get("SM_EMAIL",    "")
    SM_PASSWORD = os.environ.get("SM_PASSWORD", "")
    SFTP_USER   = os.environ.get("SFTP_USER",   "")
    SFTP_PASS   = os.environ.get("SFTP_PASS",   "")

    if not SM_EMAIL and not SM_PASSWORD and not SFTP_USER and not SFTP_PASS:
        raise RuntimeError(
            f"[config] Keeper failed and no environment variables set: {_exc}\n"
            "Run locally with keeper_config.json, or set SM_EMAIL / SM_PASSWORD / "
            "SFTP_USER / SFTP_PASS as environment variables."
        ) from _exc

# Non-secret constants (can be overridden via env vars)
SM_ID       = os.environ.get("SM_ID",       "000000009462EB13")
SM_BASE_URL = os.environ.get("SM_BASE_URL", "https://cloud.solar-manager.ch")
