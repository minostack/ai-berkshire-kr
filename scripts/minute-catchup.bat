@echo off
REM ---------------------------------------------------------------
REM CVD/OBV - 1-min bar daily catch-up (Task Scheduler wrapper)
REM
REM NOTE: keep this file ASCII-only.
REM   cmd.exe parses .bat in the OEM codepage (949 here), so UTF-8
REM   Korean comments shift the byte stream and cmd tries to run
REM   garbage as commands (measured 2026-10-19: 4 bogus attempts).
REM   A UTF-8 BOM does NOT fix it - it breaks "@echo off".
REM
REM SUPERSEDED since 2026-10-06: scripts\provisional-load.bat (16:10,
REM   provisional daily bar) and scripts\daily-load.bat (08:30 next day,
REM   confirmed regular session) both run the minute catch-up as their
REM   first step (bars-1.0). Use this file only to fetch minute bars on
REM   their own.
REM
REM Steps    : fetch only the missing 1-min bars, log to logs\.
REM            Safe to run late - it finds the gaps itself by comparing
REM            against silver.bar_coverage.
REM
REM Register (once, from an elevated command prompt):
REM   schtasks /create /tn "CVD minute catchup" /tr "C:\a-ai\ai-berkshire-kr\scripts\minute-catchup.bat" /sc daily /st 16:10
REM   (the existing entry's name is in Korean; this file is not
REM    scheduled by default any more - see SUPERSEDED above)
REM
REM Why 16:10: after the regular session closes (15:30) and before the
REM   after-market session (20:00), so that day's regular bars are all
REM   published. Any time after 20:10 works too. Safe on weekends and
REM   holidays - with nothing new to fetch it just exits.
REM
REM Exit codes (cvd_obv.domain.status.ExitCode):
REM   0 ok / 9 partial (unresolved gaps) / 4 data quality / 5 API
REM   Partial success is not treated as failure: halted and no-trade
REM   securities mix in normally.
REM
REM Log      : logs\minute-catchup\YYYYMMDD.log
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0.."

set "PYTHONIOENCODING=utf-8"
set "LOGDIR=%CD%\logs\minute-catchup"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "tokens=1-3 delims=/- " %%a in ("%DATE%") do set "TODAY=%%a%%b%%c"
set "LOGFILE=%LOGDIR%\%TODAY%.log"

echo ==== %DATE% %TIME% ==== >> "%LOGFILE%"
python cvdobv.py minute-cache catchup --period 1 --days 10 >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo exit=%RC% >> "%LOGFILE%"

if "%RC%"=="9" exit /b 0
exit /b %RC%
