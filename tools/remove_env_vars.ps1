# ============================================================
# remove_env_vars.ps1
# ai-berkshire-kr 프로젝트에서 사용하던 Windows User 환경변수 삭제
#
# ⚠️ 실행 전 확인사항:
#   1. .env 파일에 아래 키 값들이 전부 정상적으로 옮겨져 있는지 확인
#   2. 이 스크립트는 되돌릴 수 없습니다 (값 자체가 지워짐)
#
# 사용법 (PowerShell 관리자 권한 불필요, 일반 사용자 권한으로 실행):
#   powershell -ExecutionPolicy Bypass -File remove_env_vars.ps1
# 또는 PowerShell 콘솔에 그대로 붙여넣기
# ============================================================

$targets = @(
    "KIWOOM_APP_KEY",
    "KIWOOM_APP_SECRET",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "DART_API_KEY",
    "EASTMONEY_SEARCH_TOKEN",
    "GITHUB_TOKEN",
    "FINMIND_TOKEN"
)

Write-Host "=== 삭제 전 현재 값 확인 (백업용 출력) ===" -ForegroundColor Yellow
foreach ($name in $targets) {
    $val = [System.Environment]::GetEnvironmentVariable($name, "User")
    if ($val) {
        Write-Host "  $name = $val"
    } else {
        Write-Host "  $name = (설정 안 됨)"
    }
}

Write-Host ""
$confirm = Read-Host "위 값들을 .env로 옮기셨나요? 삭제를 진행하려면 'yes' 입력"
if ($confirm -ne "yes") {
    Write-Host "취소되었습니다. 아무것도 삭제하지 않았습니다." -ForegroundColor Red
    exit
}

Write-Host ""
Write-Host "=== 삭제 진행 ===" -ForegroundColor Yellow
foreach ($name in $targets) {
    $existing = [System.Environment]::GetEnvironmentVariable($name, "User")
    if ($existing) {
        [System.Environment]::SetEnvironmentVariable($name, $null, "User")
        Write-Host "  ✅ 삭제됨: $name"
    } else {
        Write-Host "  ⏭️  건너뜀 (이미 없음): $name"
    }
}

Write-Host ""
Write-Host "완료. 변경사항을 반영하려면 현재 열린 터미널/PowerShell 창을 모두 닫고 새로 여세요." -ForegroundColor Green
Write-Host "(User 환경변수는 새 프로세스부터 반영됩니다. 이미 실행 중인 창에는 남아있을 수 있습니다.)"
