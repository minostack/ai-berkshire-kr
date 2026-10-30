<#
.SYNOPSIS
  변경 사항을 묶음별로 커밋하고 GitHub에 올린다(docs/MIGRATION.md 7절).

.DESCRIPTION
  순서
    1) git fetch, 현재 상태 출력
    2) 안전 점검 — 올라가면 안 되는 파일(.env·토큰·수집 데이터·적재 장부·잠금)이나 50MB 넘는 파일이
       올라갈 목록에 있으면 아무것도 하지 않고 멈춘다
    3) 묶음별 커밋(바뀐 것이 없는 묶음은 건너뜀)
         ① .gitignore(+ 이미 스테이징된 것)
         ② 코드·테스트: src tests tools configs scripts cvdobv.py pyproject.toml cvd_selection
         ③ 문서·스킬: docs skills CLAUDE.md
         ④ 나머지: 리포트·스캔 결과·로그·구성종목 명단 등
    4) git pull --rebase origin main(프로젝트 규칙 — 원격에 새 커밋이 있을 수 있다)
    5) 푸시 — 확인을 받는다(-Yes면 묻지 않음, -NoPush면 하지 않음)

  푸시 여부는 사용자가 정한다. 이 스크립트도 푸시 전에 묻는다.
  이 파일은 UTF-8 BOM으로 저장한다(Windows PowerShell 5.1이 BOM 없는 UTF-8 한글을 깨뜨린다).

.PARAMETER DryRun
  점검과 묶음별 파일 수만 보여 주고 아무것도 커밋하지 않는다.

.PARAMETER NoPush
  커밋과 pull --rebase까지만 한다.

.PARAMETER Yes
  푸시 전에 묻지 않는다.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\git-upload.ps1 -DryRun
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\git-upload.ps1
#>
param(
    [switch]$DryRun,
    [switch]$NoPush,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Invoke-Git {
    # 한글 경로를 \355… 로 바꾸지 않게 한다. git은 진행 메시지를 stderr로 쓰므로
    # 그것을 오류로 보지 않게 잠시 Continue로 두고 종료 코드로만 판정한다
    $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & git -c core.quotepath=false @args } finally { $ErrorActionPreference = $eap }
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') 실패(종료 코드 $LASTEXITCODE)" }
}
function Pending([string[]]$paths) {
    # 커밋되지 않은 변경(수정·삭제·새 파일) — 무시 규칙에 걸린 것은 나오지 않는다
    $out = & git -c core.quotepath=false status --porcelain -uall -- @paths
    @($out | Where-Object { $_ } | ForEach-Object { $_.Substring(3).Trim('"') })
}

$Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($Branch -ne "main") { throw "지금 브랜치가 $Branch 다 — main에서 실행할 것" }
if ((& git rev-parse --git-path rebase-merge) -and (Test-Path (& git rev-parse --git-path rebase-merge))) {
    throw "rebase가 진행 중이다 — 먼저 끝내거나(git rebase --continue) 취소할 것(git rebase --abort)"
}

# 1) 원격 상태
Step "원격 상태 확인"
Invoke-Git fetch origin
Invoke-Git status -sb | Select-Object -First 1
$behind = [int](& git rev-list --count "HEAD..origin/main")
if ($behind) { Write-Host "  원격에 새 커밋 $behind 개 — 4단계 pull --rebase가 받는다" }

# 2) 안전 점검
Step "안전 점검(올라가면 안 되는 파일)"
$all = Pending @(".")
if (-not $all) { Write-Host "  커밋할 변경이 없다" }
$forbidden = @(
    '^\.env$', '^\.env\.', 'token\.json$', '^local/',
    '^data/(bronze|silver|gold)/', '^data/manifests/(bronze|silver|gold)_',
    '^data/manifests/data_load\.lock', '^data/manifests/(data_load_state|minute_catchup_state)\.json$',
    '^cvd_selection/research/output/', '^reports/portfolio-latest\.md$', '^logs/command-log\.jsonl$'
)
$bad = @($all | Where-Object { $p = $_; $forbidden | Where-Object { $p -match $_ } })
# 이미 지우는 중인 토큰(추적 해제)은 괜찮다
$staged = @(& git -c core.quotepath=false diff --cached --name-status | Where-Object { $_ -match '^D\s' } |
    ForEach-Object { ($_ -split '\s+', 2)[1] })
$bad = @($bad | Where-Object { $staged -notcontains $_ })
$big = @($all | Where-Object { (Test-Path -LiteralPath $_ -PathType Leaf) -and
    (Get-Item -LiteralPath $_).Length -gt 50MB })
if ($bad) { $bad | ForEach-Object { Write-Host "  금지: $_" -ForegroundColor Red } }
if ($big) { $big | ForEach-Object { Write-Host "  50MB 초과: $_" -ForegroundColor Red } }
if ($bad -or $big) { throw "올라가면 안 되는 파일이 있다 — .gitignore를 고치거나 파일을 옮긴 뒤 다시 실행할 것" }
$mb = [math]::Round((($all | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    ForEach-Object { (Get-Item -LiteralPath $_).Length }) | Measure-Object -Sum).Sum / 1MB, 1)
Write-Host "  통과 — 변경 $($all.Count)개, 약 $mb MB"

# 3) 묶음별 커밋
$groups = @(
    @{ Name = "무시 규칙"; Paths = @(".gitignore");
       Msg = "gitignore 정리: 토큰·적재 잠금·적재 장부·KRX 캐시·점검 스크립트 제외, CVD 선정 연구 코드 추적" },
    @{ Name = "코드·테스트"; Paths = @("src", "tests", "tools", "configs", "scripts", "cvdobv.py", "pyproject.toml", "cvd_selection");
       Msg = "CVD/OBV V2 데이터 레이어: KIS 적재 원천, NXT 메인마켓 거래량(8/3~), 체결량 UN, 선정 스캐너, 이관·업로드 스크립트" },
    @{ Name = "문서·스킬"; Paths = @("docs", "skills", "CLAUDE.md");
       Msg = "문서·스킬 갱신: 봉 기준(NXT 경계 8/3), 사전등록, 이관 절차(MIGRATION)" },
    @{ Name = "나머지"; Paths = @(".");
       Msg = "리포트·스캔 결과·적재 로그·구성종목 명단 갱신" }
)
Step "묶음별 커밋$(if ($DryRun) { ' — 시험 실행(커밋하지 않음)' })"
$seen = @{}
# 이미 스테이징된 삭제는 첫 묶음 커밋에 함께 들어간다
$staged | ForEach-Object { $seen[$_] = $true }
foreach ($g in $groups) {
    $existing = @($g.Paths | Where-Object { Test-Path -LiteralPath $_ })
    if (-not $existing) { continue }
    # 시험 실행에서는 앞 묶음이 커밋되지 않으므로 앞 묶음 파일을 빼고 센다
    $files = @(Pending $existing | Where-Object { -not $seen.ContainsKey($_) })
    $files | ForEach-Object { $seen[$_] = $true }
    $hasStaged = $g.Name -eq "무시 규칙" -and $staged
    if (-not $files -and -not $hasStaged) { Write-Host "  [$($g.Name)] 변경 없음 — 건너뜀"; continue }
    Write-Host "  [$($g.Name)] $($files.Count)개$(if ($hasStaged) { " + 스테이징된 삭제 $($staged.Count)개" })"
    if ($DryRun) {
        $files | Select-Object -First 8 | ForEach-Object { Write-Host "      $_" }
        if ($files.Count -gt 8) { Write-Host "      … 외 $($files.Count - 8)개" }
        continue
    }
    if ($files) { Invoke-Git add -A -- @existing }
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { Write-Host "    스테이징된 것 없음 — 건너뜀"; continue }
    Invoke-Git commit -q -m $g.Msg
    Write-Host "    커밋: $(& git log -1 --format='%h %s')"
}
if ($DryRun) { Write-Host "`n시험 실행 끝 — 실제로 하려면 -DryRun 없이 실행"; exit 0 }

$left = Pending @(".")
if ($left) { Warn "커밋되지 않은 변경이 남았다: $($left.Count)개"; $left | Select-Object -First 10 | ForEach-Object { Write-Host "      $_" } }

# 4) 원격 변경 먼저 받기
Step "git pull --rebase origin main"
$eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
try { & git -c core.quotepath=false pull --rebase origin main } finally { $ErrorActionPreference = $eap }
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ rebase 충돌 — 충돌 파일을 고친 뒤 'git add <파일>' → 'git rebase --continue', 그다음 이 스크립트를 다시 실행" -ForegroundColor Red
    exit 1
}

# 5) 푸시
$ahead = [int](& git rev-list --count "origin/main..HEAD")
Step "푸시 — 올릴 커밋 $ahead 개"
if (-not $ahead) { Write-Host "  올릴 커밋이 없다"; exit 0 }
& git log --oneline "origin/main..HEAD" | ForEach-Object { Write-Host "    $_" }
if ($NoPush) { Write-Host "  -NoPush — 푸시하지 않았다. 나중에: git push origin main"; exit 0 }
if (-not $Yes) {
    $ans = Read-Host "  origin/main에 푸시할까요? (y/N)"
    if ($ans -notmatch '^(y|yes|ㅇ|예)$') { Write-Host "  푸시하지 않았다. 나중에: git push origin main"; exit 0 }
}
Invoke-Git push origin main
Write-Host "`n✅ 푸시 완료 — $(& git rev-parse --short HEAD)"
