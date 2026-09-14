# .env 마이그레이션 패치 가이드

## 0. 사전 준비

```bash
pip install python-dotenv
```

`requirements.txt`가 있다면 `python-dotenv` 한 줄 추가.

`.gitignore`에 아래 줄이 없으면 추가 (이미 있는지 먼저 확인):

```bash
grep -q "^\.env$" .gitignore || echo ".env" >> .gitignore
```

---

## 1. 공통 로더 방식 (권장 — 코드 변경 최소화)

각 `tools/*.py` 파일에서 `os.environ.get(...)`을 호출하기 전에 딱 2줄만 추가하면,
기존 `os.environ.get("KIWOOM_APP_KEY")` 같은 코드는 **전혀 수정할 필요 없이** `.env` 값을 그대로 읽어옵니다.
(`load_dotenv()`가 `.env` 파일 내용을 프로세스의 `os.environ`에 주입해주기 때문)

파일 상단, `import os` 근처에 추가:

```python
from dotenv import load_dotenv
load_dotenv()  # 프로젝트 루트의 .env 파일을 os.environ에 로드
```

`load_dotenv()`는 기본적으로 현재 작업 디렉터리부터 상위로 올라가며 `.env`를 찾습니다.
프로젝트 루트가 아닌 곳에서 스크립트를 실행할 가능성이 있다면 경로를 명시하는 게 안전합니다:

```python
from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
```

---

## 2. 파일별 패치 위치

### tools/daily_scanner.py
- 상단 import 구역(현재 line 19-20 근처, 환경변수 안내 docstring 있는 곳) 바로 아래에 로더 2줄 추가
- `os.environ.get("KIS_APP_KEY")` 등 line 472, 473, 581-584, 605-606, 807-808, 845-846, 938-939 — **수정 불필요** (load_dotenv만 추가하면 그대로 동작)
- docstring/print 안내문(line 19-20, 586-588, 618-620, 826, 870, 941-942)의 `export KIS_APP_KEY=...` 문구는 아래로 교체 권장:
  ```
  .env 파일에 KIS_APP_KEY=... 형식으로 설정하세요 (.env.example 참고)
  ```

### tools/kiwoom_cvd_obv_scanner.py
- 상단 docstring(line 17-19) 바로 아래 import 구역에 로더 2줄 추가
- line 113-114, 186 — 수정 불필요
- line 724-725, 861-862 안내 문구를 `.env` 방식으로 교체:
  ```
  .env 파일에 KIWOOM_APP_KEY=... / KIWOOM_APP_SECRET=... 설정 (.env.example 참고)
  ```

### tools/update_index.py
- import 구역에 로더 2줄 추가
- line 149-150, 183 — 수정 불필요
- line 34-35, 471 안내 문구 `.env` 방식으로 교체

### tools/kr_scanner.py
- import 구역에 로더 2줄 추가
- line 318, 341 — 수정 불필요
- line 325, 344 안내 문구 교체

### tools/kstock_data.py
- import 구역에 로더 2줄 추가
- line 231 — 수정 불필요
- line 241, 428 안내 문구 교체

### tools/ashare_data.py — ⚠️ 별도 처리 필요 (아래 3번 항목 참고)

### tools/star_history_chart.py, tools/twstock_data.py
- 필요하다면 동일하게 로더 2줄 추가 (line 59, 45 각각 수정 불필요)
- 다만 두 파일 모두 한국 포크에 실제로 필요한지부터 검토 권장 (star_history_chart는 레포 관리용, twstock_data는 대만 주식용으로 보임)

---

## 3. ashare_data.py — EASTMONEY_SEARCH_TOKEN 확인 결과

**현재 코드 (line 304):**
```python
token = os.environ.get("EASTMONEY_SEARCH_TOKEN") or "D43BF722C8E33BDC906FB84D85E326E8"
```

**이 파일이 하는 일**: 원본 저장소(xbtlin/ai-berkshire)에서 가져온 중국 A주(본토 상장주) 실시간 데이터 수집 도구입니다. 텐센트 시세 API와 동방재부(East Money) 재무 API를 `curl`로 직접 호출합니다.

**이 토큰의 용도**: 변수명(`EASTMONEY_SEARCH_TOKEN`)과 문맥상 동방재부의 **종목 검색/조회 API 호출 시 인증 파라미터**로 쓰이는 값으로 보입니다. 이미 `or "D43BF722C8E33BDC906FB84D85E326E8"` 형태로 **소스코드에 하드코딩된 기본값**이 들어 있다는 점이 특징적입니다.

**중요 — 확인이 필요한 부분**: 이 토큰이
1. 동방재부가 공개적으로 배포하는 **범용 데모/공개 토큰**(원저자가 원본 오픈소스 저장소에 그대로 남겨둔 것)인지,
2. 아니면 원저자 개인 계정에 연결된 **실제 시크릿 값**인지

grep 결과와 함수 하나만으로는 100% 단정할 수 없습니다. 다만 오픈소스 저장소 코드에 아무 보호 장치 없이 하드코딩되어 있었다는 점, 그리고 이 파일 자체가 "한국 포크에서는 사용 빈도가 낮다"고 이전에 평가됐던 점을 고려하면 **공개용 데모 토큰일 가능성이 높습니다**. 그래도 안전하게 처리하려면:

- `.env`로 옮기되 원본 값을 기본값(fallback)으로 유지 (아래 패치)
- 이 파일을 실제로 계속 쓸 계획이 없다면 삭제 후보로 유지 (이전 대화에서 "한국주용으로 교체 또는 삭제" 권장됨)

**권장 패치** (line 304, 하드코딩 기본값은 유지하되 `.env`로 오버라이드 가능하게):
```python
token = os.environ.get("EASTMONEY_SEARCH_TOKEN", "D43BF722C8E33BDC906FB84D85E326E8")
```
(`or` 대신 `os.environ.get`의 두 번째 인자로 기본값을 넘기는 방식 — 동작은 동일하지만 관례적으로 더 명확함)

파일 상단에도 로더 2줄 추가:
```python
from dotenv import load_dotenv
load_dotenv()
```

**최종 판단은 사용자 몫**: 이 도구를 계속 쓸 계획이 없다면 `.env` 작업 자체를 생략하고 파일을 삭제하는 게 더 깔끔합니다. 계속 쓸 계획이면 위 패치대로 `.env`에 개인 토큰(있다면)을 넣고, 없으면 기본값이 계속 동작합니다.

---

## 4. 적용 순서 요약

1. `.env.example`을 프로젝트 루트에 저장
2. `cp .env.example .env` 후 실제 키 값 입력
3. `.gitignore`에 `.env` 포함 확인
4. `pip install python-dotenv`
5. 위 파일별 목록대로 각 `tools/*.py` 상단에 로더 2줄 추가
6. 안내 메시지(`print("export ...")`) 문구를 `.env` 안내로 교체 (선택사항이지만 권장)
7. 기존 Windows User 환경변수는 정상 동작 확인 후 제거 검토 (`.env`가 우선 적용되도록 `load_dotenv(override=True)`를 쓰면 `.env` 값이 항상 우선함— 필요 시 이 옵션도 고려)
8. 이번 기회에 노출 이력이 있는 키(KIWOOM/KIS/DART)는 재발급 권장
