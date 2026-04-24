# UX 테스트 리포트

**테스트 대상**: unified-cli (베이스 + 온보딩/상태 UI 통합 후)
**테스트 실행**: claude-haiku-4-5 · gpt-5.4-mini · gemini-3.1-flash-lite-preview (기본 모델, 토큰 절약)
**테스트 관점**: 신규 사용자 초보 ~ 고수가 모두 겪을 수 있는 실제 사용 흐름

## 평가 축

- **사용자 층**: 🟢 초보 · 🟡 중급 · 🔴 고수
- **결과**: ✅ 통과 · ⚠️ 경미한 문제 · ❌ 실제 UX 저해 · 🐛 명확한 버그
- **영향도**: Minor / Medium / Major

## 매트릭스

### A. 첫 실행 UX (초보 대상)

| # | 시나리오 | 기대 동작 | 실제 | 결과 | 비고 |
|---|---|---|---|---|---|
| A1 | `unified-cli` (서브커맨드 없이) | "어떤 명령을 쓸지" 힌트 | argparse 에러만 (짧음) | ⚠️ Minor | "먼저 `unified-cli setup` 하세요" 같은 안내 없음 |
| A2 | `unified-cli --help` | 각 명령 역할 한 줄 요약 | 간결하게 잘 나옴 | ✅ | 초보 기준 OK |
| A3 | `unified-cli chat --help` | 모든 플래그 설명 | `--stream`/`--no-web-search` 설명 누락 | ⚠️ Minor | argparse `help="..."` 추가 필요 |
| A4 | `unified-cli chat "hi"` (모델 지정 없음) | 기본 provider 로 작동 | Claude 기본 모델로 작동 | ✅ | |
| A5 | `-m haikuu` (오타) | 오타 안내 + 사용 가능 목록 힌트 | 명확한 에러 + hint | ✅ | |
| A6 | `doctor` 출력 가독성 | 한눈에 상태 파악 | rich 테이블, 이모지 컬러 구분 | ✅ | |
| A7 | `setup --skip-install --skip-verify` (모두 로그인됨) | "다 됐다" 메시지 | ✅ 요약 표 + 다음 단계 | ✅ | |

### B. 기본 사용 (초중급)

| # | 시나리오 | Claude | Codex | Gemini | 비고 |
|---|---|---|---|---|---|
| B1 | `chat("say: ok")` token | ✅ 10/375 | ✅ 9370/65 | ✅ 8146/1 | **⚠️ Claude 가 "ok" 에 375 토큰 장황 답변** (Codex/Gemini 는 "ok" 한 단어) |
| B2 | session resume ("이름 민우" → "이름?") | ✅ 기억 | ⚠️ 자기 이름으로 오해 | ⚠️ 자기 이름으로 오해 | 프롬프트 모호성 — 명확하게 쓰면 모두 통과. Claude 는 모호해도 잘 맞춤 |
| B3 | 스트리밍 TTFT (Time To First Text) | 5.76s | **2.48s** | 3.47s | Claude 가 제일 느림. UX 체감 좋지 않을 수 있음 |
| B4 | 기본값만으로 한국어 호출 | ✅ 자연스러움 | ✅ | ✅ | |
| B5 | `--stream` CLI 사용 | ✅ 실시간 출력 | ✅ | ✅ | |

### C. 고급 기능 (중급~고수)

| # | 시나리오 | 결과 | 비고 |
|---|---|---|---|
| C1 | 웹서치 기본 ON (3 provider) | ✅ 3개 모두 `tool_use` 이벤트 + 정답 "2025" | Claude 11s / Codex 16s / Gemini 9s |
| C2 | Conversation cross-provider (숫자 42 기억) | ✅ 명확한 프롬프트면 claude→codex→gemini 모두 "42" 기억 | |
| C3 | `UnifiedConversation(sticky=True)` 전환 차단 | ✅ `UnifiedError(kind="config")` | 메시지 명확 |
| C4 | `unified-cli status` 스냅샷 + `--watch` | ✅ rich Live 5초 갱신 | |
| C5 | OpenAI SDK 로 서버 호출 | ✅ `base_url=http://localhost/v1` 자동 라우팅 | |
| C6 | 대시보드 `/dashboard` 렌더 | ✅ 7084 bytes HTML, 5초 자동 갱신 | |
| C7 | `/v1/doctor` + `/v1/usage` + `/v1/conversations` JSON | ✅ 3개 provider × 정확한 aggregates + 세션 추적 | |
| C8 | `--json` 플래그 (doctor/status/models) | ✅ 자동화 친화적 스키마 | |

### D. 에러/뻘짓 (전 사용자 층)

| # | 시나리오 | Claude | Codex | Gemini | 문제 |
|---|---|---|---|---|---|
| D1 | 없는 모델명 | ✅ `model_not_allowed` | ✅ `model_not_allowed` | ⚠️ `not_found` | **Gemini 는 `not_found` 로 분류 — 의미상 `model_not_allowed`가 맞음.** 매처 순서 버그 |
| D2 | 없는 session_id | ⚠️ `internal` | 🐛 **에러 없이 새 대화 시작** | ✅ `not_found` + 힌트 | Claude: 전용 매처 없어서 internal. Codex: **치명적 UX — 히스토리 사라진 걸 사용자가 모름** |
| D3 | 빈 문자열 프롬프트 `""` | 🐛 엉뚱한 답변 | (미테스트) | (미테스트) | Client 레벨에서 reject 해야 |
| D4 | 잘못된 provider 이름 (`create("gpt4")`) | N/A | N/A | N/A | ✅ `config` 에러 + 한국어 메시지 |
| D5 | sticky 대화에서 provider 전환 | ✅ | ✅ | ✅ | `config` 에러 + hint |
| D6 | Ctrl+C 중간 (stream) | — | — | — | **미테스트 — 후속 필요** |
| D7 | `CODEX_CLI_PATH=/bogus` 환경변수 | — | — | — | **미테스트 — 디스커버리 우선순위 확인 필요** |

### E. 배포/자동화 (고수)

| # | 시나리오 | 결과 | 비고 |
|---|---|---|---|
| E1 | `pytest tests/` 전체 | ✅ 27 passed | 새로 추가한 usage 7개 포함 |
| E2 | `.gitignore` 실제로 작동 (.venv/__pycache__ 제외) | ✅ baseline 커밋 28 파일, 누수 0 | |
| E3 | 가상환경 재구성 후 `pip install -e .[server]` 로 바로 동작 | ✅ | |
| E4 | `uvicorn unified_cli.server:app` → `/dashboard` 접속 | ✅ 170+ 라인 HTML 응답 | |
| E5 | `/v1/models?provider=codex` 필터링 | ✅ 6 모델 | 서버 캐시 TTL 과 직접 호출 결과가 약간 다를 수 있음 (cache 갱신 시점 차이) |

## 발견된 문제 요약 + 개선 방향

### 🐛 High Priority (명백한 버그 or UX 저해)

#### Bug-1. Codex 가 없는 session_id 에 대해 조용히 새 대화 시작
- **증상**: `chat("hi", session_id="00000000-...")` → UnexpectedSuccess: "Hi. What can I help with?"
- **원인**: `codex exec resume <존재하지않는 id>` 이 에러 없이 새 대화 fallback 으로 동작
- **영향**: Major — 사용자가 컨텍스트 유지되는 줄 알고 쓰는데 실제로는 매번 새 대화. 디버깅 어려움
- **개선**: [providers/codex.py](Desktop/cli-wrapper-unified/src/unified_cli/providers/codex.py) 에서 resume 시 `thread.started` 이벤트의 `thread_id` 가 요청한 `session_id` 와 일치하는지 검증 → 불일치 시 `UnifiedError(kind="not_found")` raise. 또는 pre-flight 로 `~/.codex/sessions/` 에 해당 thread UUID 파일이 있는지 `Path.exists()` 체크

#### Bug-2. 빈 프롬프트 허용
- **증상**: `chat("")` → Claude 가 "보이는 코드" 같은 엉뚱한 답변
- **원인**: 클라이언트에서 guard 없음
- **영향**: Medium — 실제로 사용자가 빈 프롬프트를 의도적으로 보낼 일은 드물지만 pipe 실수 등 가능
- **개선**: [base.py](Desktop/cli-wrapper-unified/src/unified_cli/base.py) `chat`/`stream` 첫 줄에 `if not prompt.strip(): raise UnifiedError(kind="config", ..., message="빈 프롬프트")`

#### Bug-3. Claude 에 session not-found 에러 분류 없음
- **증상**: 없는 session_id → `kind="internal"` 로 떨어짐 (alias 용어로 분류 안 됨)
- **개선**: [errors.py](Desktop/cli-wrapper-unified/src/unified_cli/errors.py) MATCHERS["claude"] 에 `(r"session.{0,20}(not found|does not exist)", "not_found", "check_resource")` 추가

#### Bug-4. Gemini 모델 오타 → `not_found` (기대: `model_not_allowed`)
- **증상**: `model="gemini-fake"` → "Requested entity was not found" 가 `not_found` 매처에 먼저 매칭됨
- **개선**: MATCHERS["gemini"] 순서 재정렬 — "model" 단어 포함 검사를 `not_found` 보다 앞에 두기. 또는 `not_found` 매처에 session 문맥만 남기고 404 는 model_not_allowed 로

### ⚠️ Medium Priority (UX 개선)

#### UX-1. `unified-cli` (no args) → 서브커맨드 힌트 추가
- 현재: argparse 기본 에러
- 개선: `cli.py` `main()` 에서 `args` 파싱 전에 `sys.argv` 길이 체크 → 한 줄 요약 + `unified-cli setup` 권장

#### UX-2. `chat --help` 의 플래그 설명 누락
- `--stream` / `--no-web-search` / `--cwd` 가 `help=""` 없이 나옴
- 개선: `cli.py` 의 argparse add_argument 에 `help="..."` 모두 채우기

#### UX-3. Claude 가 짧은 질문에 장황하게 답함
- `chat("ok")` → output 375 tokens (Codex/Gemini는 1-65)
- 원인: Claude Code 기본 시스템 프롬프트가 "친절하고 자세한" 편향
- 개선 옵션:
  - A) wrapper 기본값에 `append_system_prompt="답변은 필요한 만큼만 간결하게 하세요."` 추가 (`ClaudeProvider.__init__` 에서)
  - B) `unified-cli chat` CLI 에 `--terse` 플래그 추가 (default off, on 시 위 시스템 프롬프트 주입)
  - C) 현 상태 유지 (사용자 책임으로)
- 권장: B (invasive 하지 않음)

#### UX-4. Claude 스트리밍 TTFT 5.76s — 체감 느림
- 구조적 한계 (Node spawn + Anthropic API latency). 마커/진행 표시로 체감 개선 가능
- 개선: `stream()` 첫 event 수신 전까지 rich spinner 표시 옵션 (CLI 레벨). `_cmd_chat` 의 `--stream` 분기에 `rich.status.Status` 로 "응답 대기 중..." 스피너

### ✅ 추가 테스트 필요 (이번 회차 미수행)

- **D6**: Ctrl+C 중간에 stream 중단 — cleanup, subprocess termination, tracker 상태 무결성
- **D7**: `CODEX_CLI_PATH=/bogus` 또는 `PATH` 에서 바이너리 제거한 뒤 setup wizard 가 제대로 감지하는지
- **Stream 중 tool_use 후 다시 text 오는 multi-round**: 현재 단순 웹서치만 1회 테스트
- **동시 요청**: 서버에서 10개 동시 /v1/chat 호출 시 subprocess 동시성 한계
- **대용량 프롬프트**: argv limit 2MB 근접, stdin pipe 로 전달해야 하는지
- **한글 깨짐**: JSON 파싱 중 UTF-8 처리 (지금까지 샘플은 모두 정상)

## 사용자 시나리오 재연 ("초보 실수" 카탈로그)

| 시나리오 | 현재 동작 | 권장 개선 |
|---|---|---|
| "어 뭐 쓰지?" → `unified-cli` | argparse 에러 | 친절한 "먼저 `setup` 하세요" |
| "걍 chat 해보자" → `unified-cli chat "hi"` (no -m) | ✅ Claude 기본값 | OK |
| "모델 이름 까먹음" → `unified-cli chat "hi" -m sonnet4.5` (오타) | ✅ 라우팅 실패 + hint | OK (but hint 에 실제 사용가능한 모델 n개 예시 나오면 더 좋음) |
| "세션 ID 복붙해서 이어쓰기" → 만료된 ID | ❌ Codex 는 조용히 새 대화, Claude는 internal 에러 | Bug-1, Bug-3 fix |
| "비어있는 파일 pipe" → `cat empty.txt \| unified-cli chat` | ❌ 엉뚱한 답변 | Bug-2 fix |
| "pbpaste 긴 문서" → 2MB 가까운 프롬프트 | 미테스트 | argv 한계 체크 + stdin pipe 권장 |
| "서버 띄웠는데 OpenAI 코드 그대로 쓰기" → `user="chat-1"` | ✅ 자동 대화 이어짐 | OK |
| "이 provider 크레딧 다 씀" → rate_limit 에러 | ✅ `kind=rate_limit` + "다른 provider 전환" hint | OK (아직 실제 rate limit 못 발동시켜 검증 미완) |
| "대시보드 켜두고 얼마 썼나 보고 싶음" | ✅ `/dashboard` 5초 폴링 | OK |
| "stream 중에 Ctrl+C" | 미테스트 | D6 follow-up |

## 다음 단계 (권장 커밋 순서)

1. **fix**: Bug-1 (Codex resume session validation)
2. **fix**: Bug-2 (empty prompt guard)
3. **fix**: Bug-3 + Bug-4 (error classifier 매처 보강)
4. **polish**: UX-1 (no-args 힌트)
5. **polish**: UX-2 (chat flag help 텍스트)
6. **feat**: UX-4 (stream spinner via rich.status)
7. (optional) UX-3 `--terse` 플래그

각 수정 후 기존 27개 테스트 계속 통과 확인 + 해당 시나리오 재실행.

## 종합 점수

| 카테고리 | 점수 | 비고 |
|---|---|---|
| 첫 실행 UX | 4/5 | 미세한 힌트 부족 (A1, A3) |
| 기본 사용 | 4/5 | Claude 장황함 제외 대부분 OK |
| 고급 기능 | 5/5 | 웹서치/대시보드/conversation 전부 문제없음 |
| 에러 처리 | 3/5 | Codex resume 버그, Claude 매처 공백, 빈 프롬프트 가드 없음 |
| 종합 | **4/5** | **고수는 바로 쓰기 좋음. 초보도 setup 만 하면 OK. 중급은 위 4개 버그만 잡으면 완성도 급상승.** |
