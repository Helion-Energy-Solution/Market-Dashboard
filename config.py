# config.py — loads credentials from Keeper Secrets Manager at startup.
# Requires the keeper package and a valid keeper_config.json.
# See: https://docs.keeper.io/secrets-manager/

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

except Exception as exc:
    raise RuntimeError(
        f"[config] Failed to load credentials from Keeper: {exc}\n"
        "Ensure the keeper package is installed and keeper_config.json is present."
    ) from exc

# Non-secret constants
SM_ID       = "000000009462EB13"   # Solar Manager installation ID
SM_BASE_URL = "https://cloud.solar-manager.ch"
