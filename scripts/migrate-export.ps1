<#
.SYNOPSIS
  운영 PC 이관 1단계(기존 PC) — 프로젝트·작업 스케줄러·Claude 메모리를 한 폴더로 내보낸다.

.DESCRIPTION
  docs/MIGRATION.md 3절 ①②, 4절, 6절을 자동으로 한다.

  내보내는 폴더 구조(<Dest>):
    project\                 프로젝트 폴더 전체(수집 데이터·.env·연구 결과 포함, 토큰·캐시·잠금 제외)
    tasks\*.xml              작업 스케줄러 정의("CVD*" 작업)
    claude-memory\           Claude 작업 메모리
    claude-commands-extra\   skills\에 원본이 없는 설치 스킬(설치 스크립트로 복원 안 되는 것)
    pip-freeze.txt           기존 파이썬 패키지 버전
    export-info.txt          내보낸 시각·경로·장부·용량 요약

  기본으로 "CVD*" 작업을 끈다 — 이관 뒤 두 PC가 동시에 적재하지 않게 하기 위함이다.
  .env(API 키)가 들어 있으므로 <Dest>는 USB·사내 공유에만 두고 클라우드에 올리지 않는다.

  이 파일은 UTF-8 BOM으로 저장한다(Windows PowerShell 5.1이 BOM 없는 UTF-8 한글을 깨뜨린다).

.PARAMETER Dest
  내보낼 폴더(예: E:\berkshire-migrate). 없으면 만든다.

.PARAMETER SkipGold
  data\gold(계산 결과, 약 2.5GB)를 뺀다. 새 PC에서 다시 계산해야 한다.

.PARAMETER KeepTasksEnabled
  작업 스케줄러 작업을 끄지 않는다(시험 복사용).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\migrate-export.ps1 -Dest E:\berkshire-migrate
#>
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [switch]$SkipGold,
    [switch]$KeepTasksEnabled
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Dest = [System.IO.Path]::GetFullPath($Dest)

function Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }

if ($Dest.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "내보낼 폴더가 프로젝트 안에 있다: $Dest — 다른 드라이브나 폴더를 지정할 것"
}

Write-Host "이관 내보내기 — 원본 $Root → $Dest"

# 1) 적재가 돌고 있으면 멈춘다(복사 중 Parquet이 바뀌면 안 된다)
Step "적재 실행 여부 확인"
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match "cvdobv\.py|data-load|minute-cache|daily-cache|trade-sides" }
if ($running) {
    $running | ForEach-Object { Write-Host "  실행 중: PID $($_.ProcessId) $($_.CommandLine)" }
    throw "적재가 실행 중이다 — 끝난 뒤 다시 실행할 것(10:00~15:00 또는 21:00 이후 권장)"
}
Write-Host "  실행 중인 적재 없음"

New-Item -ItemType Directory -Force -Path $Dest | Out-Null

# 2) 작업 스케줄러 — XML로 내보내고 끈다
Step "작업 스케줄러 내보내기"
$taskDir = Join-Path $Dest "tasks"
New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -like "CVD*" })
if (-not $tasks) { Warn "'CVD*' 작업이 없다 — 작업 스케줄러는 새 PC에서 직접 등록해야 한다" }
foreach ($t in $tasks) {
    $bat = [System.IO.Path]::GetFileNameWithoutExtension(($t.Actions | Select-Object -First 1).Execute)
    $file = Join-Path $taskDir "task-$bat.xml"
    Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath | Out-File -Encoding unicode $file
    Write-Host "  $($t.TaskName) → tasks\task-$bat.xml (상태 $($t.State))"
    if (-not $KeepTasksEnabled -and $t.State -ne "Disabled") {
        Disable-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath | Out-Null
        Write-Host "    작업을 껐다 — 기존 PC로 되돌릴 때는 Enable-ScheduledTask"
    }
}

# 3) 프로젝트 폴더 복사
Step "프로젝트 폴더 복사(수 분~수십 분)"
$proj = Join-Path $Dest "project"
# 빼는 것 — 새 PC에서 저절로 다시 생기는 것만(토큰·잠금·파이썬 캐시·KRX 조회 캐시).
# .gitignore에 걸린 나머지(.env, 수집 데이터, 연구 결과, scratchx.py 등)는 git으로 못 옮기므로 반드시 복사한다
$xd = @("__pycache__", ".pytest_cache", (Join-Path $Root "C83F901C09BC4A43BF0D43811169DB9A66EDA04F"))
if ($SkipGold) { $xd += (Join-Path $Root "data\gold") }
$xf = @(".kis_token.json", ".kiwoom_token.json", "data_load.lock", "data_load.lock.json")
$log = Join-Path $Dest "robocopy-export.log"
robocopy $Root $proj /E /COPY:DAT /DCOPY:T /R:1 /W:1 /MT:16 /NP /NFL /NDL `
    /XD @xd /XF @xf /LOG:$log | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) { throw "robocopy 실패(종료 코드 $rc) — $log 확인" }
Write-Host "  완료(robocopy 종료 코드 $rc, 로그 robocopy-export.log)"
if ($SkipGold) { Warn "data\gold를 뺐다 — 새 PC에서 백테스트 전에 다시 계산해야 한다" }

# 4) Claude 메모리·설치 스킬 중 원본 없는 것
Step "Claude 메모리·스킬"
$key = ($Root -replace "[:\\/]", "-")
$mem = Join-Path $env:USERPROFILE ".claude\projects\$key\memory"
if (Test-Path $mem) {
    robocopy $mem (Join-Path $Dest "claude-memory") /E /R:1 /W:1 /NP /NFL /NDL | Out-Null
    Write-Host "  메모리 $((Get-ChildItem $mem -File).Count)개 → claude-memory\"
} else { Write-Host "  메모리 없음($mem)" }
$cmdDir = Join-Path $env:USERPROFILE ".claude\commands"
if (Test-Path $cmdDir) {
    $extra = Get-ChildItem $cmdDir -File -Filter *.md |
        Where-Object { -not (Test-Path (Join-Path $Root "skills\$($_.Name)")) }
    if ($extra) {
        $ed = Join-Path $Dest "claude-commands-extra"
        New-Item -ItemType Directory -Force -Path $ed | Out-Null
        $extra | ForEach-Object { Copy-Item $_.FullName $ed -Force; Write-Host "  skills\에 원본 없는 스킬: $($_.Name)" }
    }
}

# 5) 파이썬 패키지 버전
Step "파이썬 패키지 버전 기록"
$py = (& python --version) 2>&1
& python -m pip freeze 2>$null | Out-File -Encoding utf8 (Join-Path $Dest "pip-freeze.txt")
Write-Host "  $py → pip-freeze.txt"

# 6) git에 올라가지 않는 자료가 복사본에 다 들어갔나(docs/MIGRATION.md 1·6절)
Step "git에 없는 자료 확인"
$required = [ordered]@{
    "data\silver"                           = "수집 데이터(1분봉·일봉·체결량)"
    "data\bronze"                           = "원천 응답(KRX·DART)"
    "data\manifests\data_load_state.json"   = "적재 장부(마지막 확정일, git 제외)"
    ".env"                                  = "API 키"
    "cvd_selection\research"                = "CVD 선정 연구 코드"
    "cvd_selection\research\output"         = "CVD 선정 연구 결과"
}
$optional = [ordered]@{
    "data\manifests\minute_catchup_state.json" = "분봉 재시도 억제 장부(git 제외)"
    "data\gold"                             = "계산 결과"
    ".claude\settings.local.json"           = "Claude Code 로컬 설정"
    "local"                                 = "민감 정보 폴더"
    "reports\portfolio-latest.md"           = "실제 포지션 리포트"
    "scratchx.py"                           = "일회성 점검 스크립트"
}
$missing = @()
$checks = @()
foreach ($kv in $required.GetEnumerator()) {
    $inSrc = Test-Path (Join-Path $Root $kv.Key)
    $inDst = Test-Path (Join-Path $proj $kv.Key)
    $mark = if ($inDst) { "포함" } elseif ($inSrc) { "빠짐" } else { "원본에 없음" }
    if (-not $inDst) { $missing += $kv.Key }
    $checks += "  [필수] $($kv.Key) — $($kv.Value): $mark"
}
foreach ($kv in $optional.GetEnumerator()) {
    $inSrc = Test-Path (Join-Path $Root $kv.Key)
    if (-not $inSrc) { continue }
    $inDst = Test-Path (Join-Path $proj $kv.Key)
    $mark = if ($inDst) { "포함" } elseif ($kv.Key -eq "data\gold" -and $SkipGold) { "제외(-SkipGold)" } else { "빠짐" }
    $checks += "  [선택] $($kv.Key) — $($kv.Value): $mark"
}
$checks | ForEach-Object { Write-Host $_ }
foreach ($x in $xf) {
    if (Get-ChildItem $proj -Recurse -Force -File -Filter $x -ErrorAction SilentlyContinue) {
        Warn "제외해야 할 $x 가 복사본에 있다"
    }
}
if ($missing) { Warn "필수 자료가 없다: $($missing -join ', ') — 원본을 확인할 것" }

# 7) 요약
Step "요약"
$state = Join-Path $Root "data\manifests\data_load_state.json"
$confirmed = if (Test-Path $state) { (Get-Content $state -Raw | ConvertFrom-Json).confirmed_through } else { "없음" }
$files = Get-ChildItem $proj -Recurse -File -Force
$gb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1GB, 2)
$info = @(
    "내보낸 시각: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "원본 경로: $Root"
    "PC 이름: $env:COMPUTERNAME"
    "파이썬: $py"
    "장부 confirmed_through: $confirmed"
    "프로젝트 복사본: $($files.Count)개 파일, $gb GB"
    "data\gold 제외: $([bool]$SkipGold)"
    "작업 스케줄러: $($tasks.Count)개 내보냄, 끔: $(-not $KeepTasksEnabled)"
    "필수 자료 빠짐: $(if ($missing) { $missing -join ', ' } else { '없음' })"
    "git에 없는 자료:"
) + $checks
$info | Out-File -Encoding utf8 (Join-Path $Dest "export-info.txt")
$info | ForEach-Object { Write-Host "  $_" }

Write-Host "`n✅ 내보내기 완료 — $Dest"
Write-Host "   .env(API 키)가 들어 있다. USB·사내 공유로만 옮길 것"
Write-Host "   새 PC에서: powershell -NoProfile -ExecutionPolicy Bypass -File <Dest>\project\scripts\migrate-import.ps1 -Source <Dest>"
