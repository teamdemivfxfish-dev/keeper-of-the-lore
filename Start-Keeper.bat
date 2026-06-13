@echo off
REM Launch the Keeper of the Lore Discord bot (single-instance).
cd /d "%~dp0"

REM --- Fail-safe: stop any previous copy of THIS bot before starting a new one.
REM Matches only python running keeper_of_the_lore.py, so other Python apps are safe.
echo Stopping any previous instance...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*keeper_of_the_lore.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if not exist ".venv\" (
    echo First run: creating virtual environment and installing dependencies...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

echo Starting Keeper of the Lore...
python keeper_of_the_lore.py
pause
