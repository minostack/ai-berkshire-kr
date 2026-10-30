@echo off
REM ---------------------------------------------------------------
REM CVD/OBV - provisional daily load (bars-1.0)
REM
REM NOTE: keep this file ASCII-only.
REM   cmd.exe parses .bat in the OEM codepage (949 here), so UTF-8
REM   Korean comments shift the byte stream and cmd tries to run
REM   garbage as commands (measured 2026-10-19: 1 bogus attempt).
REM   A UTF-8 BOM does NOT fix it - it breaks "@echo off".
REM   Korean rationale lives in docs/BAR_STANDARD.md section 4.
REM
REM Schedule : Mon-Fri 16:10, Task Scheduler entry whose action is
REM            this file (name is in Korean; find it with
REM            Get-ScheduledTask | ? { $_.Actions.Execute -like "*provisional-load*" })
REM
REM Steps    : 1) pre-check: per-day minute / provisional / confirmed status
REM            2) backlog first: minute bars -> missing provisional bars
REM               -> regular confirmation (covers a skipped 08:30 run)
REM            3) today's provisional regular daily bar from 1-min bars
REM               (daily-cache provisional)
REM            NXT: after the KRX minute catchup it also fetches NXT 1-min
REM               bars (minute-cache catchup --venue NXT, optional step).
REM               Daily volume = KRX + NXT main market 09:00:30-15:20.
REM            4) KIS buy/sell trade sides (trade-sides daily): UN (KRX+NXT)
REM               today as PROVISIONAL rev 0 - KRX after-market and NXT run
REM               until 20:00, so it is final only next day. Optional step:
REM               a failure does not block the bar load or the ledger.
REM
REM            5) index constituent history (universe refresh), optional:
REM               calendar -> KIS snapshot -> KRX check data -> notices
REM               -> rebuild (dry-run first, write only if 0 unresolved)
REM               -> verify last 60 days -> data/index_stocks.json.
REM               History ends at the last day with KRX data (yesterday).
REM
REM Provisional bar: price is the regular-session value, but volume is
REM   the 1-min sum (regular + after-hours single price), so it is
REM   approximate. Stored as revision 0 and overwritten by the next
REM   confirmed load (08:30 next day).
REM
REM Why 16:10: after the post-close single-price session (15:40-16:00).
REM   Safe to run on weekends/holidays - with no 1-min bars for today it
REM   simply does not create a provisional bar.
REM
REM Exit codes (cvd_obv.domain.status.ExitCode):
REM   0 ok / 9 partial (halted or no-trade gaps) / 4 data quality / 5 API
REM
REM Log      : logs\provisional-load\YYYYMMDD.log
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0.."

set "PYTHONIOENCODING=utf-8"
set "LOGDIR=%CD%\logs\provisional-load"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "tokens=1-3 delims=/- " %%a in ("%DATE%") do set "TODAY=%%a%%b%%c"
set "LOGFILE=%LOGDIR%\%TODAY%.log"

echo ==== %DATE% %TIME% ==== >> "%LOGFILE%"
python cvdobv.py data-load provisional >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo exit=%RC% >> "%LOGFILE%"

REM Treat partial success (9) as success for the task scheduler.
if "%RC%"=="9" exit /b 0
exit /b %RC%
