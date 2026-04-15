@echo off
echo Starte Helion Marktdaten-Dashboard...
echo Daten werden verarbeitet (1-2 Minuten)...
echo Dashboard: http://localhost:3000
echo Beenden: Strg+C
echo.

REM Try to find Python — check common locations
set PYTHON=
if exist "C:\Users\ThijsAntoniedeBoer\AppData\Local\Python\pythoncore-3.14-64\python.exe" (
    set PYTHON=C:\Users\ThijsAntoniedeBoer\AppData\Local\Python\pythoncore-3.14-64\python.exe
) else if exist "C:\Python313\python.exe" (
    set PYTHON=C:\Python313\python.exe
) else if exist "C:\Python312\python.exe" (
    set PYTHON=C:\Python312\python.exe
) else (
    REM Try system python
    python --version >nul 2>&1 && set PYTHON=python
)

if "%PYTHON%"=="" (
    echo FEHLER: Python nicht gefunden. Bitte Python installieren.
    pause
    exit /b 1
)

echo Benutze Python: %PYTHON%
"%PYTHON%" "%~dp0serve.py"
pause
