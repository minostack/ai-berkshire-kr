#!/bin/bash
# 사용자 명령어를 로그 파일에 기록
# user_prompt_submit 훅에 의해 호출됨 (stdin으로 사용자 입력 수신)

LOG_DIR="$HOME/ai-berkshire/logs"
LOG_FILE="$LOG_DIR/command-log.jsonl"
COUNTER_FILE="$LOG_DIR/.counter"

mkdir -p "$LOG_DIR"

# 사용자 입력 읽기
PROMPT=$(cat)

# 빈 입력 건너뜀
[ -z "$PROMPT" ] && exit 0

# 타임스탬프 (초 단위)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# 앞 200자만 기록 (초과 입력 방지)
PROMPT_SHORT=$(echo "$PROMPT" | head -c 200 | tr '\n' ' ' | tr '"' "'")

# 로그 추가 (JSONL 형식)
echo "{\"time\":\"$TIMESTAMP\",\"prompt\":\"$PROMPT_SHORT\"}" >> "$LOG_FILE"

# 카운터 업데이트
if [ -f "$COUNTER_FILE" ]; then
    COUNT=$(cat "$COUNTER_FILE")
else
    COUNT=0
fi
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

# 10개마다 알림 출력 (hook stdout → Claude에게 표시됨)
if [ $((COUNT % 10)) -eq 0 ]; then
    TOTAL=$(wc -l < "$LOG_FILE" | tr -d ' ')
    echo "[명령어 로그] 총 ${TOTAL}개 명령어가 기록되었습니다."
fi
