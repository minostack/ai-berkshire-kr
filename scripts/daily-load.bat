@echo off
REM ---------------------------------------------------------------
REM CVD/OBV - confirmed regular-session daily load (bars-1.0)
REM
REM NOTE: keep this file ASCII-only.
REM   cmd.exe parses .bat in the OEM codepage (949 here), so UTF-8
REM   Korean comments shift the byte stream and cmd tries to run
REM   garbage as commands. Measured 2026-10-19: 4 bogus command
REM   attempts, exit 9009. A UTF-8 BOM does NOT fix it (it breaks
REM   "@echo off" instead). ASCII-only = 0 errors, exit 0.
REM   Korean rationale lives in docs/BAR_STANDARD.md section 4.
REM
REM Schedule : Mon-Sat 08:30, Task Scheduler entry whose action is
REM            this file (name is in Korean; find it with
REM            Get-ScheduledTask | ? { $_.Actions.Execute -like "*daily-load*" })
REM            (StartWhenAvailable=True, so a missed run fires late)
REM
REM Steps    : 1) data-load daily
REM               minute catchup -> missing provisional bars ->
REM               daily-cache build -> regular-from-minute,
REM               for every day after data/manifests/data_load_state.json
REM               confirmed_through. Also backfills a skipped 16:10 run.
REM               Provisional bars (approx. volume) become confirmed here.
REM               NXT 1-min bars are fetched right after the KRX catchup
REM               (--venue NXT, optional). Confirmed volume adds the NXT
REM               main market (09:00:30-15:20); prices stay KRX.
REM               Ends with trade-sides daily (KIS FHKST03010800): UN
REM               (KRX+NXT) yesterday as confirmed rev 1.
REM               Optional step - a failure does not stop the load.
REM            2) benchmark catchup (tools/benchmark_catchup.py)
REM               KOSPI200 / KOSPI / KOSDAQ index daily bars, source KIS
REM               FHKUP03500100 since 2026-10-23 (was Kiwoom ka20006;
REM               --source kiwoom to go back). The backtest
REM               calendar in ab_backtest_b.load_calendar_and_index is built
REM               FROM these rows, so a stale index silently caps every
REM               backtest window (measured 2026-10-20: index 09-22 vs bars
REM               10-19, so 19 trading days were invisible).
REM               NOTE: do NOT call "cvdobv.py benchmark build" with no
REM               window here. Without --start it refetches the whole
REM               history and rewrites the same primary keys at
REM               data_revision=1, piling up ~13,000 duplicate rows per run.
REM               The catchup script finds the last stored date per index
REM               and fetches only after it, the same way data-load does.
REM            3) calendar build
REM               silver.market_calendar is derived from daily bars but
REM               data-load never touched it, so it kept drifting
REM               (measured 2026-10-19: calendar 10-12 vs bars 10-19;
REM               earlier 09-28 vs 10-08). A stale calendar hides new
REM               trading days from universe krx-cache / rebuild
REM               (skills/update-index.md step 0). Must run AFTER the
REM               bars are loaded.
REM
REM Why 08:30 next day: regular volume is restored as
REM   "EXTENDED minus after-market minute bars", so it needs the
REM   after-market close (20:00 + 5min). Any run after 20:05 gives the
REM   same result. Saturday's run confirms Friday; Monday's run covers
REM   a missed Saturday.
REM
REM Exit codes (cvd_obv.domain.status.ExitCode):
REM   0 ok / 9 partial (halted or no-trade gaps) / 4 data quality / 5 API
REM
REM Log      : logs\daily-load\YYYYMMDD.log
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0.."

set "PYTHONIOENCODING=utf-8"
set "LOGDIR=%CD%\logs\daily-load"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "tokens=1-3 delims=/- " %%a in ("%DATE%") do set "TODAY=%%a%%b%%c"
set "LOGFILE=%LOGDIR%\%TODAY%.log"

echo ==== %DATE% %TIME% ==== >> "%LOGFILE%"
python cvdobv.py data-load daily >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo data-load exit=%RC% >> "%LOGFILE%"

REM Treat partial success (9) as success for the task scheduler.
if "%RC%"=="9" set "RC=0"

REM Index bars next. Independent of the load above (separate API), but
REM kept after it so the log reads in data-freshness order. Exits 0 with
REM nothing written when the index is already current (weekends, holidays).
echo ---- benchmark catchup ---- >> "%LOGFILE%"
python tools/benchmark_catchup.py >> "%LOGFILE%" 2>&1
set "BRC=%ERRORLEVEL%"
echo benchmark exit=%BRC% >> "%LOGFILE%"
if "%BRC%"=="9" set "BRC=0"

REM Build the calendar regardless of the results above: it only reads
REM bars already stored, so it is harmless on failure and still needed
REM on partial success.
echo ---- calendar build ---- >> "%LOGFILE%"
python cvdobv.py calendar build >> "%LOGFILE%" 2>&1
set "CRC=%ERRORLEVEL%"
echo calendar exit=%CRC% >> "%LOGFILE%"

REM Report the first failure; do not let a later success hide it.
if not "%RC%"=="0" exit /b %RC%
if not "%BRC%"=="0" exit /b %BRC%
exit /b %CRC%
