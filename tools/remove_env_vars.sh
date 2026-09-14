#!/bin/bash
# ============================================================
# remove_env_vars.sh
# ai-berkshire-kr 프로젝트에서 사용하던 macOS/Linux 환경변수 삭제
#
# ⚠️ 실행 전 확인사항:
#   1. .env 파일에 아래 키 값들이 전부 정상적으로 옮겨져 있는지 확인
#   2. 이 스크립트는 ~/.zshrc, ~/.zprofile, ~/.bash_profile, ~/.bashrc 에서
#      해당 export 줄을 찾아 제거합니다 (원본은 .bak으로 백업됨)
#
# 사용법:
#   chmod +x remove_env_vars.sh
#   ./remove_env_vars.sh
# ============================================================

TARGETS=(
    "KIWOOM_APP_KEY"
    "KIWOOM_APP_SECRET"
    "KIS_APP_KEY"
    "KIS_APP_SECRET"
    "KIS_ACCOUNT_NO"
    "DART_API_KEY"
    "EASTMONEY_SEARCH_TOKEN"
    "GITHUB_TOKEN"
    "FINMIND_TOKEN"
)

PROFILE_FILES=(
    "$HOME/.zshrc"
    "$HOME/.zprofile"
    "$HOME/.bash_profile"
    "$HOME/.bashrc"
)

echo "=== 삭제 전 현재 값 확인 (백업용 출력) ==="
for name in "${TARGETS[@]}"; do
    val="${!name}"
    if [ -n "$val" ]; then
        echo "  $name = $val"
    else
        echo "  $name = (현재 세션에 설정 안 됨)"
    fi
done

echo ""
read -p "위 값들을 .env로 옮기셨나요? 삭제를 진행하려면 'yes' 입력: " confirm
if [ "$confirm" != "yes" ]; then
    echo "취소되었습니다. 아무것도 삭제하지 않았습니다."
    exit 0
fi

echo ""
echo "=== 셸 설정 파일에서 export 줄 제거 ==="
for profile in "${PROFILE_FILES[@]}"; do
    if [ -f "$profile" ]; then
        found=false
        for name in "${TARGETS[@]}"; do
            if grep -qE "^[[:space:]]*export[[:space:]]+${name}=" "$profile"; then
                found=true
            fi
        done

        if [ "$found" = true ]; then
            cp "$profile" "${profile}.bak"
            for name in "${TARGETS[@]}"; do
                sed -i.tmp -E "/^[[:space:]]*export[[:space:]]+${name}=/d" "$profile"
                rm -f "${profile}.tmp"
            done
            echo "  ✅ 수정됨: $profile (백업: ${profile}.bak)"
        else
            echo "  ⏭️  해당 없음: $profile"
        fi
    fi
done

echo ""
echo "=== 현재 셸 세션에서 unset ==="
for name in "${TARGETS[@]}"; do
    unset "$name"
    echo "  unset $name"
done

echo ""
echo "완료. 새 터미널 창을 열거나 'source ~/.zshrc' (또는 해당 프로필 파일)를 실행해야"
echo "완전히 반영됩니다. 문제가 생기면 .bak 파일로 복원하세요."
