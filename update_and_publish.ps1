# update_and_publish.ps1 — Daily data refresh and Vercel publish
# Scheduled via Windows Task Scheduler to run once per day.
#
# What it does:
#   1. Downloads latest market CSVs (Swissgrid + EPEX SFTP)
#   2. Rebuilds data/data.json from all CSVs + live PV data
#   3. Commits and pushes if anything changed

$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\ThijsAntoniedeBoer\OneDrive - HELION\Dokumente\Market Dashboard"
$Python     = "C:\Users\ThijsAntoniedeBoer\OneDrive - HELION\Dokumente\python-projects\standard_env\Scripts\python.exe"
$LogFile    = "$ProjectDir\update_and_publish.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

Set-Location $ProjectDir

Log "── Starting daily update ──────────────────────────────"

# 1. Download new market data
Log "Step 1: update_data.py"
& $Python update_data.py 2>&1 | Tee-Object -Append $LogFile
if ($LASTEXITCODE -ne 0) { Log "ERROR: update_data.py failed (exit $LASTEXITCODE)"; exit 1 }

# 2. Rebuild data.json
Log "Step 2: export_data.py"
& $Python export_data.py 2>&1 | Tee-Object -Append $LogFile
if ($LASTEXITCODE -ne 0) { Log "ERROR: export_data.py failed (exit $LASTEXITCODE)"; exit 1 }

# 3. Commit and push if data.json changed
$changed = & git diff --name-only data/data.json
if ($changed) {
    Log "Step 3: committing and pushing updated data.json"
    & git add data/data.json
    & git commit -m "Update market data $(Get-Date -Format 'yyyy-MM-dd')"
    & git push
    Log "Done — Vercel will redeploy automatically."
} else {
    Log "Step 3: data.json unchanged — nothing to push."
}

Log "── Finished ───────────────────────────────────────────"
