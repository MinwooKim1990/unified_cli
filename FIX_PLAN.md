# 버그 + UX 수정 계획

UX 테스트에서 발견된 8개 항목(🐛 4 + ⚠ 4)에 대한 **구체적 코드 수정 계획**.
각 수정은 논리 단위 커밋 1개.

## 우선순위 요약

| # | 제목 | 영향도 | 파일 | 라인 범위 (대략) | LOC |
|---|---|---|---|---|---|
| F1 | Codex 조용한 새 세션 생성 차단 | Major | `base.py` | `chat`/`_stream_run` 후처리 | +12 |
| F2 | 빈 프롬프트 가드 | Medium | `base.py` | `chat`/`stream` 앞 가드 | +6 |
| F3 | Claude "session not found" 분류 추가 | Medium | `errors.py` | MATCHERS["claude"] | +2 |
| F4 | Gemini 매처 순서 재정렬 | Minor | `errors.py` | MATCHERS["gemini"] 2개 줄 스왑 | ±0 |
| U1 | `unified-cli` 무인자 실행 힌트 | Minor | `cli.py` | `main()` 최상단 | +8 |
| U2 | `chat --help` 플래그 설명 | Minor | `cli.py` | argparse add_argument | +4 |
| U3 | Claude 기본 간결 모드 `--terse` | Minor | `cli.py` + `providers/claude.py` | CLI flag + init opt | +10 |
| U4 | 스트리밍 첫 토큰 대기 스피너 | Minor | `cli.py` | `_cmd_chat --stream` 분기 | +8 |

**총 ~60줄**, 파일 4개 수정. 영향도 큰 F1/F2 는 회귀 테스트 반드시.

---

## F1 — Codex 가 조용히 새 세션 생성하는 것 차단

### 증상 재확인
```python
cli = create("codex")
cli.chat("hi", session_id="00000000-0000-0000-0000-000000000000")
# → "Hi. What can I help with?" (새 대화 시작)
```

### 근본 원인
`codex exec resume <nonexistent-uuid>` 은 실패하는 대신 **새 대화를 만들고 다른 thread_id 를 emit**한다. subprocess 종료 코드는 0 이라 `classify` 도 안 걸림.

### 수정: `BaseProvider.chat` / `stream` 공통 후처리
반환된 `session_id` 가 요청값과 다르면 (그리고 요청값이 None 이 아니면) → 명시적 에러.

```python
# src/unified_cli/base.py 의 chat() 끝부분
if session_id and resp.session_id and resp.session_id != session_id:
    raise UnifiedError(
        kind="not_found", provider=self.name,
        message=(f"요청한 세션 {session_id[:12]}… 을 찾을 수 없어 "
                 f"새 세션 {resp.session_id[:12]}… 이 생성되었습니다."),
        hint="세션이 만료되었거나 다른 cwd 에서 생성됐는지 확인하세요.",
        cause=f"requested={session_id} got={resp.session_id}",
    )
```

`stream()` 은 `session` 이벤트를 보는 순간 같은 검사. 다만 스트림은 이미 내용을 일부 yield 한 뒤라 raise 가 늦을 수 있음 → **세션 이벤트 감지 직후, 본격 text 이벤트가 오기 전에 raise**.

Claude/Gemini 는 이 검사에 걸리지 않음 (Claude 는 exit 1, Gemini 는 pre-flight `_find_session_index` 가 먼저 raise). 3 provider 공통 안전장치.

### 테스트
- `test_session_mismatch_raises` — fake session_id 로 chat 하면 `kind="not_found"` raise
- 기존 history 테스트 계속 통과 (정상 session_id 에서는 match)

---

## F2 — 빈 프롬프트 client-side 가드

### 증상 재확인
```python
create("claude").chat("")
# → Claude 가 엉뚱한 "test code 분석" 답변
```

### 수정: `chat`/`stream` 맨 앞
```python
# src/unified_cli/base.py
def chat(self, prompt: str, *, ...):
    if not prompt or not prompt.strip():
        raise UnifiedError(
            kind="config", provider=self.name,
            message="프롬프트가 비어있습니다.",
            hint="공백 아닌 텍스트를 전달하세요. stdin 에서 읽는 경우 파이프 입력을 확인하세요.",
        )
    # 기존 로직
```

`stream()` 도 동일. 동기/비동기 모두.

서버 레벨 `/v1/chat/completions` 는 `_last_user_prompt` 가 이미 마지막 user 메시지를 찾지만 빈 content 는 통과할 수 있음 → `_last_user_prompt` 에서도 `if not content.strip()` 체크.

### 테스트
- `test_empty_prompt_raises_config` — `chat("")` 과 `chat("   \n")` 둘 다 raise
- HTTP: `POST /v1/chat/completions {messages:[{role:"user",content:""}]}` → 400

---

## F3 — Claude 의 "session not found" 분류

### 증상
Claude 로 존재하지 않는 `--resume <uuid>` 하면 `kind="internal"` 로 떨어짐.

### 수정: `errors.py` MATCHERS["claude"] 에 한 줄 추가
현재 `model[^\n]{0,80}(not exist|not accessible|invalid|unknown)` 패턴 앞/뒤에:

```python
(re.compile(r"session[^\n]{0,40}(not found|does not exist|invalid|expired)", re.I),
 "not_found", "check_resource"),
```

순서는 model 매처 **위**. Claude 의 실제 stderr 문자열을 `unified-cli doctor` 로 먼저 샘플링해서 패턴 최종 조정 (첫 구현 후 실제 로그 보고 1회 미세조정).

### 테스트
- `test_claude_session_not_found` — fixture stderr 샘플로 classify 결과 검증

---

## F4 — Gemini "Requested entity was not found" 의미 재해석

### 증상
Gemini 의 `model="gemini-fake"` → 실제 stderr `"Requested entity was not found"` → 현재 `not_found` 로 분류. 하지만 사용자 입장에선 **잘못된 모델** 이 더 정확한 진단.

### 근본 결정
Gemini 의 세션 resume 은 우리 코드 `_find_session_index` 가 pre-check 해서 세션 관련 "not found" 는 이 경로로 안 옴. 즉 CLI 가 뱉는 `"Requested entity was not found"` 는 **사실상 모델 오타가 압도적**.

### 수정: `errors.py` MATCHERS["gemini"]
```python
# 이전
(re.compile(r"Requested entity was not found", re.I), "not_found", "check_resource"),
(re.compile(r"\b404\b|model.{0,40}not found", re.I),
 "model_not_allowed", "check_model_list"),

# 이후
(re.compile(r"Requested entity was not found|\b404\b|model.{0,40}not found", re.I),
 "model_not_allowed", "check_model_list"),
```

세션 UUID→index 조회는 이미 `GeminiProvider._find_session_index` 에서 우리 `UnifiedError(kind="not_found")` 를 raise 하므로 분류기로 안 옴. 따라서 세션 분류 매처를 따로 유지할 필요 없음.

### 테스트
- 기존 `test_gemini_404_model` 이미 있음 — 여전히 pass
- `test_gemini_requested_entity_is_model_not_allowed` 신규 추가

---

## U1 — `unified-cli` 무인자 실행 시 친절한 안내

### 증상
```
$ unified-cli
usage: unified-cli [-h] {doctor,setup,status,models,chat} ...
unified-cli: error: the following arguments are required: cmd
```

### 수정: `cli.py main()` 최상단
```python
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        console.print("[bold cyan]unified-cli[/bold cyan] — 3 provider 통합 CLI 래퍼")
        console.print()
        console.print("처음이면: [bold]unified-cli setup[/bold]")
        console.print("상태확인: [bold]unified-cli doctor[/bold] · [bold]unified-cli status[/bold]")
        console.print("바로 쓰기: [bold]unified-cli chat \"안녕\" -m haiku[/bold]")
        console.print("전체 도움말: [dim]unified-cli --help[/dim]")
        return 0
    # 기존 argparse 로직
```

---

## U2 — argparse `help=""` 누락 채우기

### 수정: `cli.py` 의 chat 서브커맨드
```python
p_chat.add_argument("-m", "--model", help="모델명 또는 provider/model (예: haiku, claude/sonnet)")
p_chat.add_argument("--stream", action="store_true", help="토큰 단위 스트리밍 출력")
p_chat.add_argument("--no-web-search", dest="web_search", action="store_false", default=True,
                    help="웹서치 도구 비활성화 (기본 ON)")
p_chat.add_argument("--cwd", help="하위 CLI 의 작업 디렉토리")
```

다른 서브커맨드들 (`doctor`, `setup`, `status`, `models`) 의 플래그도 동일한 규칙으로 채움.

---

## U3 — Claude 의 짧은 질문 장황 답변 제어: `--terse`

### 증상
`chat("say: ok")` → Claude 가 `"ok\n\nWhat would you like me to..."` 식으로 375 토큰. Codex/Gemini 는 "ok" 한 단어만.

### 수정 (옵션 플래그 방식, 최소 침습)

**`providers/claude.py`** 에 옵션 추가:
```python
def __init__(self, *, terse: bool = False, **kw):
    super().__init__(**kw)
    ...
    if terse:
        terse_rule = "답변은 필요한 만큼만 간결하게. 설명은 요청했을 때만 덧붙이세요."
        self.append_system_prompt = (
            (self.append_system_prompt + "\n\n" + terse_rule)
            if self.append_system_prompt else terse_rule
        )
```

**`cli.py`** 의 chat 서브커맨드:
```python
p_chat.add_argument("--terse", action="store_true", help="짧고 간결한 답변 요청")
# 이후 create(provider, ..., terse=args.terse) 로 전달
```

Codex/Gemini 는 기본적으로 "ok" 에 "ok" 반환하므로 `terse` 무시 (미구현). 옵션 자체는 받되 동작 없음.

### 테스트
- `terse=True` 로 "say: ok" → output tokens < 50 으로 제한되는지 단순 smoke

---

## U4 — 스트리밍 첫 토큰 대기 스피너

### 증상
Claude TTFT 5.76s 동안 아무 출력 없어 사용자가 멈춘 걸로 오해.

### 수정: `cli.py` 의 `_cmd_chat --stream` 분기
```python
from rich.status import Status

if args.stream:
    status = Status("[cyan]응답 대기 중...[/cyan]", console=console)
    status.start()
    started = False
    try:
        for msg in client.stream(prompt):
            if msg.kind == "text" and msg.text:
                if not started:
                    status.stop()
                    started = True
                print(msg.text, end="", flush=True)
            elif msg.kind == "tool_use":
                if not started:
                    status.update(f"[cyan]도구 사용: {(msg.tool or {}).get('name')}[/cyan]")
                else:
                    console.print(f"\n[dim][tool_use: {(msg.tool or {}).get('name')}][/dim]")
    finally:
        status.stop()
    print()
```

tool_use 가 오면 "도구 사용: web_search" 로 스피너 업데이트 → 사용자가 "아 지금 검색 중이구나" 인지.

---

## 공통: 회귀 테스트 전략

### 기존 27개 테스트
모두 통과해야 함. 특히:
- `test_errors.py` 의 Claude/Gemini 매처 관련 테스트 재확인
- `test_usage.py` 는 base.py 변경이라 간접 영향 가능

### 신규 테스트 (`tests/test_fixes.py`)
```python
def test_f1_session_mismatch_raises():
    # fake session_id 주면 not_found UnifiedError
    ...

def test_f2_empty_prompt_raises():
    # "" → config UnifiedError
    ...

def test_f3_claude_session_not_found_classified():
    # fixture stderr 로 classify → not_found
    ...

def test_f4_gemini_requested_entity_is_model_not_allowed():
    # "Requested entity was not found" → model_not_allowed
    ...
```

실제 API 호출 없는 단위 테스트만. 기존 fixture 스타일 따름.

### 스모크 테스트 (UX 매트릭스 재검증)
수정 후 `UX_TEST_REPORT.md` 의 D1-D5, A1-A3 재실행 → 전부 통과하는지 확인.

---

## 커밋 순서

1. `fix(base): reject empty prompts and detect codex silent session fallback` (F1 + F2)
2. `fix(errors): add claude session matcher and reorder gemini matchers` (F3 + F4)
3. `polish(cli): no-arg help banner and chat --help flag descriptions` (U1 + U2)
4. `feat(cli): --terse flag for Claude verbosity control` (U3)
5. `feat(cli): spinner during stream first-token wait` (U4)
6. `test: regression tests for F1-F4 + UX fixes` (tests/test_fixes.py)

각 커밋 후 `tests/` 전체 실행 + 해당 수동 검증 스크립트 1회.

---

## 예상 효과 (UX 매트릭스 점수 변화)

| 카테고리 | 현재 | 수정 후 |
|---|---|---|
| 첫 실행 UX (A) | 4/5 | 5/5 (U1, U2) |
| 기본 사용 (B) | 4/5 | 5/5 (U3 optional, U4) |
| 고급 기능 (C) | 5/5 | 5/5 |
| 에러 처리 (D) | 3/5 | 5/5 (F1, F2, F3, F4) |
| **종합** | **4/5** | **5/5** |

---

## 외부 유사 프로젝트 참고 (리서치 완료)

Go(CLIProxyAPI), TS(cligate), Python(ductor) 3개 프로젝트를 훑어본 결과. 각 항목별로 우리 접근법이 기존 대비 어떤 포지션인지 정리.

### F1 — Codex silent session fallback 감지
**결론: 세 프로젝트 모두 catch 안 함** → 우리 수정이 novel.
- **CLIProxyAPI** (`codex_executor.go:632`): `uuid.NewString()` 으로 매 호출 새 ID 주입 — session 개념 자체 skip
- **cligate** (`codex-provider.js:242`): `thread.started` 이벤트의 thread_id 를 그냥 **덮어쓰기**, 비교 없음
- **ductor**: `SystemInitEvent.session_id` 수용만, 검증 없음

→ 우리 `session_id != requested` 감지 + UnifiedError raise 는 **이 프로젝트군의 기존 구현을 개선**하는 방향. 그대로 진행.

### F2 — Empty prompt
**cligate 만 가드, 그것도 silent skip**:
```js
// codex-provider.js:180
const prompt = String(input || '').trim();
if (prompt) { args.push(prompt); }
```
→ 문제 발생 시 조용히 넘어가서 디버깅 어려움. **우리처럼 명시적 UnifiedError raise 가 더 좋음** (F2 계획대로).

### F3/F4 — Error classification
**CLIProxyAPI (가장 구조화된 패턴)**: JSON-key 우선 + 문자열 fallback 혼합.
```go
candidates := []string{
  gjson.GetBytes(errorBody, "error.message").String(),
  gjson.GetBytes(errorBody, "message").String(),
  string(errorBody),
}
for _, c := range candidates {
  if strings.Contains(strings.ToLower(c), "selected model is at capacity") { ... }
}
```
HTTP status code 가 1차 분류, 본문 파싱이 2차. 우리 정규식 테이블과 철학 다름.

**cligate**: `CODEX_NON_FATAL_STDERR_PATTERNS` 화이트리스트로 warning 성 메시지 **먼저 필터링** 후 나머지를 에러로 취급. 순서 오분류 회피 기법.

→ 우리 수정은 기존 정규식 테이블 유지. 다만 **cligate 스타일의 "non-fatal 먼저 필터" 패턴**은 향후 고려할 가치 있음 (이번 fix 범위 밖). F3/F4 는 매처 한 줄 추가/순서 교체만으로 충분.

### U1 — No-arg invocation
**cligate**: 서브커맨드 생략 시 `start` 를 암묵적 default 로 실행 (`bin/cli.js` 의 `case undefined: case 'start':`).
**CLIProxyAPI**: 버전 출력 후 서비스 기동 — 신규 사용자에게 안내 부족.
**ductor**: 메신저 레벨 `/model` 인터랙티브 선택.

→ 우리 "힌트 출력 + 수동으로 setup 실행 유도" 접근은 중간 포지션. **신규 사용자가 무심결에 뭔가 시작되는 것보다 명시적 힌트가 안전** — 계획대로 진행.

### U4 — Streaming 첫 토큰 대기
**세 프로젝트 모두 spinner/indicator 없음**. cligate 만 `PROGRESS` 이벤트 emit (JSON 레벨, 사용자 보이는 UI 는 아님). ductor 는 Telegram 메시지 edit 으로 UX 보완 (CLI 레벨 아님).

→ 우리 `rich.status.Status` 접근은 novel. 사용자 체감 개선 확실.

### U3 — Concise mode
**세 프로젝트 모두 미구현**. ductor 의 `RULES-all-clis.md` 는 **설정 스키마** 수준, concise 시스템 프롬프트 아님.

→ `--terse` 플래그는 novel differentiator.

### 요약: 우리 수정의 포지션

| 수정 | 기존 OSS 대비 |
|---|---|
| F1 | **새로운 개선** (3 프로젝트 모두 미해결) |
| F2 | **더 좋음** (cligate silent skip → 우리 명시적 error) |
| F3/F4 | **동등** (cligate 의 non-fatal filter 는 향후 탐색) |
| U1 | **다른 철학** (implicit start vs 명시 hint) |
| U2 | **표준** (argparse help 채우기는 일반 권장) |
| U3 | **새로운 기능** (세 프로젝트 모두 미구현) |
| U4 | **사용자 체감 novel** (CLI spinner 는 미구현) |

실행에 특별히 저해되는 기존 패턴 없음. 계획 그대로 진행 가능.
