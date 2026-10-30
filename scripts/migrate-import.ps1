<#
.SYNOPSIS
  운영 PC 이관 2단계(새 PC) — migrate-export.ps1로 내보낸 폴더를 새 PC에 들인다.

.DESCRIPTION
  docs/MIGRATION.md 3절 ②~⑥, 4절을 자동으로 한다.
    1) <Source>\project → <Target> 복사(대상에 이미 파일이 있으면 -Force 없이는 멈춘다)
    2) 파이썬 패키지 설치(-InstallPackages)
    3) 작업 스케줄러 등록 — 실행 계정을 지금 사용자로, 경로를 <Target>으로 바꾼다.
       확인 전 실행되지 않도록 **꺼진 상태로** 등록한다(-EnableTasks면 켠다)
    4) Claude 메모리·원본 없는 스킬 복사, 스킬 설치
    5) 확인 — 테스트(-SkipTests로 건너뜀), data-load daily --dry-run

  키움 IP 등록은 사람이 키움 Open API 사이트에서 해야 한다(스크립트 밖).
  이 파일은 UTF-8 BOM으로 저장한다.

.PARAMETER Source
  migrate-export.ps1의 -Dest 폴더.

.PARAMETER Target
  설치할 프로젝트 경로. 기본 C:\a-ai\ai-berkshire-kr(기존과 같은 경로 권장).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File E:\berkshire-migrate\project\scripts\migrate-import.ps1 -Source E:\berkshire-migrate -InstallPackages
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$Target = "C:\a-ai\ai-berkshire-kr",
    [switch]$InstallPackages,
    [switch]$EnableTasks,
    [switch]$SkipTests,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Source = [System.IO.Path]::GetFullPath($Source)
$Target = [System.IO.Path]::GetFullPath($Target)
$proj = Join-Path $Source "project"

function Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }

if (-not (Test-Path (Join-Path $proj "cvdobv.py"))) {
    throw "$proj 에 프로젝트가 없다 — migrate-export.ps1의 -Dest 폴더를 지정할 것"
}
$infoFile = Join-Path $Source "export-info.txt"
$oldRoot = $null
if (Test-Path $infoFile) {
    Write-Host "내보낸 정보:"
    Get-Content $infoFile -Encoding UTF8 | ForEach-Object {
        Write-Host "  $_"
        if ($_ -match "^원본 경로: (.+)$") { $oldRoot = $Matches[1].Trim() }
    }
}

# 1) 프로젝트 복사
Step "프로젝트 복사 → $Target"
if ((Test-Path $Target) -and (Get-ChildItem $Target -Force | Select-Object -First 1) -and -not $Force) {
    throw "$Target 에 이미 파일이 있다 — 덮어쓰려면 -Force(지우지는 않고 같은 이름만 덮는다)"
}
$log = Join-Path $Source "robocopy-import.log"
robocopy $proj $Target /E /COPY:DAT /DCOPY:T /R:1 /W:1 /MT:16 /NP /NFL /NDL /LOG:$log | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) { throw "robocopy 실패(종료 코드 $rc) — $log 확인" }
Write-Host "  완료(robocopy 종료 코드 $rc)"
# git에 올라가지 않는 필수 자료 — 없으면 적재·연구를 이어갈 수 없다(docs/MIGRATION.md 6절)
$need = @("data\silver", "data\bronze", "data\manifests\data_load_state.json", ".env",
          "cvd_selection\research", "cvd_selection\research\output")
$lack = @($need | Where-Object { -not (Test-Path (Join-Path $Target $_)) })
if ($lack) { Warn "git에 없는 필수 자료가 빠졌다: $($lack -join ', ') — 기존 PC에서 따로 복사할 것" }
else { Write-Host "  git에 없는 필수 자료 $($need.Count)개 모두 있음" }

# 2) 파이썬
Step "파이썬"
try { $py = (& python --version) 2>&1; Write-Host "  $py" }
catch { throw "python이 없다 — Python 3.11 이상(기존 PC는 3.14)을 설치하고 PATH에 넣을 것" }
$pkgs = @("pyarrow", "pandas", "numpy", "requests", "python-dotenv", "openpyxl", "xlrd",
          "matplotlib", "pytest", "pytest-mock")
if ($InstallPackages) {
    & python -m pip install @pkgs
    if ($LASTEXITCODE -ne 0) { throw "pip install 실패" }
} else {
    Write-Host "  패키지 설치는 건너뜀(-InstallPackages로 설치). 기존 버전: $Source\pip-freeze.txt"
}

# 3) 작업 스케줄러
Step "작업 스케줄러 등록"
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$xmls = @(Get-ChildItem (Join-Path $Source "tasks") -Filter *.xml -ErrorAction SilentlyContinue)
if (-not $xmls) { Warn "tasks\*.xml 없음 — 작업을 직접 등록해야 한다(docs/MIGRATION.md 3절 ⑤)" }
foreach ($f in $xmls) {
    $xml = Get-Content $f.FullName -Raw -Encoding Unicode
    if ($xml -notmatch "<URI>\\?([^<]+)</URI>") { Warn "$($f.Name): 작업 이름(URI)을 못 찾음 — 건너뜀"; continue }
    $name = $Matches[1]
    $xml = $xml -replace "<UserId>[^<]*</UserId>", "<UserId>$sid</UserId>"
    if ($oldRoot -and $oldRoot -ne $Target) {
        $xml = $xml.Replace($oldRoot, $Target)
        Write-Host "  경로 바꿈: $oldRoot → $Target"
    }
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Warn "'$name' 작업이 이미 있다 — 그대로 둔다(다시 등록하려면 먼저 지울 것)"
        continue
    }
    Register-ScheduledTask -TaskName $name -Xml $xml | Out-Null
    if ($EnableTasks) {
        Enable-ScheduledTask -TaskName $name | Out-Null
        Write-Host "  등록·켬: $name"
    } else {
        Disable-ScheduledTask -TaskName $name | Out-Null
        Write-Host "  등록(꺼진 상태): $name"
    }
}

# 4) Claude 메모리·스킬
Step "Claude 메모리·스킬"
$key = ($Target -replace "[:\\/]", "-")
$memSrc = Join-Path $Source "claude-memory"
if (Test-Path $memSrc) {
    $memDst = Join-Path $env:USERPROFILE ".claude\projects\$key\memory"
    robocopy $memSrc $memDst /E /R:1 /W:1 /NP /NFL /NDL /XC /XN /XO | Out-Null
    Write-Host "  메모리 → $memDst (이미 있는 파일은 덮지 않음)"
}
$cmdDir = Join-Path $env:USERPROFILE ".claude\commands"
New-Item -ItemType Directory -Force -Path $cmdDir | Out-Null
Copy-Item (Join-Path $Target "skills\*.md") $cmdDir -Force
$extra = Join-Path $Source "claude-commands-extra"
if (Test-Path $extra) { Copy-Item (Join-Path $extra "*.md") $cmdDir -Force }
Write-Host "  스킬 설치 → $cmdDir"

# 5) 확인
Step "확인"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $Target
try {
    if (-not (Test-Path ".env")) { Warn ".env가 없다 — API 키를 넣어야 적재가 돈다" }
    if (-not $SkipTests) {
        & python -m pytest -q tests
        if ($LASTEXITCODE -ne 0) { Warn "테스트 실패 — 패키지 설치(-InstallPackages)를 확인할 것" }
    }
    & python cvdobv.py data-load daily --dry-run
} finally { Pop-Location }

Write-Host "`n✅ 가져오기 완료 — $Target"
Write-Host "   남은 일:"
Write-Host "   1) 키움 Open API 사이트에 이 PC 공인 IP 등록(V1.5 스캐너·update_index·NXT 30분봉 소급용)"
if (-not $EnableTasks) {
    Write-Host "   2) 확인이 끝나면 작업을 켠다:"
    Write-Host "      Get-ScheduledTask | Where-Object { `$_.TaskName -like 'CVD*' } | Enable-ScheduledTask"
}
Write-Host "   3) 기존 PC 작업은 꺼진 채로 둔다 — 두 PC가 동시에 적재하면 KIS 토큰이 충돌한다"
Write-Host "   4) 첫 정기 실행 뒤 logs\provisional-load\, logs\daily-load\ 로그의 종료 코드 확인"
