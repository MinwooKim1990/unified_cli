# 사용 가이드

README 는 개요, 이 파일은 **자주 쓰는 패턴과 트러블슈팅**.

## 처음 시작

```bash
cd path/to/cli-wrapper-unified     # 저장소 clone 후 이동

# (최초 1회) 가상환경 + 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

# 온보딩 마법사 (권장 첫 실행)
unified-cli setup

# 환경 점검
unified-cli doctor

# 사용량 대시보드 (터미널)
unified-cli status             # 스냅샷 한 번
unified-cli status --watch     # 5초 주기 자동 갱신
```

## 온보딩 마법사 (`unified-cli setup`)

5단계로 구성된 대화형 온보딩:

1. **환경 검사** — 각 provider 의 binary/OAuth/API key 유무 표로 표시
2. **설치** — binary 없는 provider 에 대해 `brew install codex` / `npm install -g @openai/codex` 같은 명령 제안 → Confirm → 실행
3. **로그인** — OAuth 도 API key 도 없는 provider 에 대해 해당 CLI 의 login 명령 spawn (터미널 TTY 인계)
   - Claude: `claude` 실행 → TUI 에서 `/login` 슬래시 명령 → `/exit`
   - Codex: `codex login` (자동으로 브라우저 열림)
   - Gemini: `gemini` 첫 실행 (자동으로 OAuth)
4. **검증** — 각 provider 에 `say hi` 1회 호출 → 성공/실패 + 토큰 표시
5. **요약** — 최종 상태 표 + 다음 단계 안내

거부(`n`) 하면 해당 단계는 명령만 출력하고 스킵. 모두 건너뛰어도 안전.

### 선택적 플래그

```bash
unified-cli setup --provider claude     # 특정 provider 만
unified-cli setup --skip-install        # 설치 건너뛰고 로그인/검증만
unified-cli setup --skip-verify         # 테스트 호출 건너뛰어 토큰 아끼기
```

## 상태 확인 방법

### 터미널

`unified-cli doctor` — rich 컬러 표로 3 provider 의 health + 바이너리 경로 + auth 상태 + 모델 수 + 기본 모델. `--json` 으로 machine-readable.

`unified-cli status` — doctor 정보 + 누적 사용량 + 최근 10개 호출. `--watch` 는 `rich.live.Live` 로 5초마다 갱신되는 대시보드.

### 웹 대시보드

서버 기동:
```bash
uvicorn unified_cli.server:app --port 8000
```

브라우저에서 `http://localhost:8000/dashboard` → 5초마다 자동 갱신:
- Providers (health, binary, auth, 모델 수)
- Usage totals (provider별 호출/에러/토큰/평균 지연)
- Active conversations (conversation id → 현재 provider → session_id)
- Recent calls (최근 30개, 시간/provider/모델/토큰/지연/프롬프트 일부/에러)

JSON 으로 가져오려면:
- `GET /v1/doctor` — provider 상태
- `GET /v1/usage` — aggregates + recent
- `GET /v1/conversations` — 활성 대화 목록

`doctor` 가 3개 CLI 경로 + auth 상태 + 모델 개수를 출력합니다. 뭐든 ✗ 가 뜨면
해당 CLI 를 먼저 설치/로그인하세요.

## Python API 쿡북

### 임포트 한 줄

```python
from unified_cli import (
    create, UnifiedConversation,             # 핵심 진입점
    Message, Response, Usage,                # 데이터 타입
    UnifiedError, ErrorKind,                 # 에러 처리
    list_models, route,                      # 유틸
    tracker,                                 # 누적 사용량
)
```

### 패턴 1 — 단발 호출

```python
resp = create("claude").chat("hi")
print(resp.text, resp.session_id, resp.usage.output_tokens)
```

### 패턴 2 — 외부 코드가 히스토리 관리 (session_id 수동 전달)

```python
cli = create("codex")                         # 한 번만 만들고 재사용
sessions: dict[str, str] = {}                 # 본인 앱의 user_id → session_id

def reply(user_id: str, prompt: str) -> str:
    resp = cli.chat(prompt, session_id=sessions.get(user_id))
    sessions[user_id] = resp.session_id       # 다음 턴을 위해 저장
    return resp.text
```

### 패턴 3 — 래퍼가 히스토리 관리 (+ provider 전환)

```python
conv = UnifiedConversation()                  # sticky=False 기본
conv.send("내 이름은 민우", provider="claude")
conv.send("내 이름?", provider="gemini")      # 직전 8턴 컨텍스트 자동 주입
for turn in conv.history():
    print(turn.provider, turn.prompt, "→", turn.text[:40])
```

### 패턴 4 — 스트리밍 + 도구 이벤트

```python
for msg in create("claude").stream("오늘 최신 Python 버전?"):
    if msg.kind == "text":
        print(msg.text, end="", flush=True)
    elif msg.kind == "tool_use":
        print(f"\n[{msg.tool['name']}]", flush=True)
    elif msg.kind == "usage":
        print(f"\n(tokens: {msg.usage.input_tokens}/{msg.usage.output_tokens})")
```

### 패턴 5 — async 병렬

```python
import asyncio
from unified_cli import create

async def main():
    r = await asyncio.gather(
        create("claude").achat("A"),
        create("codex").achat("B"),
        create("gemini").achat("C"),
    )
    for resp in r:
        print(resp.provider, resp.text.strip()[:30])

asyncio.run(main())
```

### 패턴 6 — 에러 분류 기반 폴백

```python
from unified_cli import create, UnifiedError

def try_chat(prompt: str):
    for provider in ("claude", "codex", "gemini"):
        try:
            return create(provider).chat(prompt)
        except UnifiedError as e:
            if e.kind in ("auth_expired", "rate_limit"):
                continue                      # 다음 provider
            raise                             # 그 외는 즉시 전파
    raise RuntimeError("all providers unavailable")
```

### 패턴 7 — CLI 가 저장한 세션을 Python 에서 이어쓰기

```python
from unified_cli import create, load_last_session

state = load_last_session()                   # ~/.unified-cli/state.json
if state:
    cli = create(state.provider, model=state.model)
    resp = cli.chat("추가 질문", session_id=state.session_id)
    print(resp.text)
```

반대 방향 (Python 에서 저장 → CLI 에서 `--continue`): `save_last_session(provider, model, session_id)`.

---

## Python 으로 쓰기 (기본 예시)

```python
from unified_cli import create, UnifiedConversation, UnifiedError
```

4가지 대표 시나리오:

| 하고싶은 일 | 쓸 거 |
|---|---|
| 한 번만 호출 (가장 단순) | `create(provider).chat(prompt)` |
| 같은 provider 로 대화 이어쓰기 | `UnifiedConversation(default_provider=..., sticky=True)` |
| 여러 provider 왔다갔다 | `UnifiedConversation()` (기본 sticky=False) |
| 응답을 토큰 단위로 받기 | `create(p).stream(prompt)` → Message iter |

## 즉시 실행 가능한 예제

`examples/` 에 8개 파일. 복사 붙여넣기 아니라 그대로 실행.

```bash
source .venv/bin/activate

python examples/01_hello.py             # 3 provider 인사
python examples/02_history.py           # 같은 provider 이어쓰기
python examples/03_multi_provider.py    # provider 전환 + 컨텍스트 주입
python examples/04_streaming.py         # 스트리밍 이벤트 종류별
python examples/05_web_search.py        # 3 provider 웹서치
python examples/06_error_handling.py    # UnifiedError 분류
python examples/07_openai_sdk.py        # OpenAI SDK 로 서버 호출 (서버 기동 필요)
python examples/08_async.py             # async / 병렬
```

## 터미널에서 빠르게 쓰기

```bash
source .venv/bin/activate

# 한 번 호출
unified-cli chat "안녕" -m haiku

# stdin 으로 긴 프롬프트
pbpaste | unified-cli chat -m gpt-5.4-mini

# 스트리밍 (에디터에 붙여넣기 용)
unified-cli chat "파이썬 퀵정렬 코드" -m haiku --stream

# 웹서치 끄기
unified-cli chat "안녕" -m haiku --no-web-search

# 모델 목록
unified-cli models
unified-cli models codex --json
```

## OpenAI 호환 서버로 쓰기

기존 OpenAI 클라이언트 코드를 그대로 재활용하고 싶을 때.

```bash
source .venv/bin/activate
uvicorn unified_cli.server:app --port 8000
```

다른 터미널에서:
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"haiku","messages":[{"role":"user","content":"hi"}]}' \
  | python3 -m json.tool
```

### Python (OpenAI SDK)
```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
r = c.chat.completions.create(
    model="haiku",                    # 자동으로 claude 라우팅
    messages=[{"role":"user","content":"hi"}],
    user="my-chat-1",                 # 같은 값이면 히스토리 유지됨
)
print(r.choices[0].message.content)
```

### 모델명 라우팅 규칙
- `claude/<m>`, `codex/<m>`, `gemini/<m>` — 명시 prefix (최우선)
- `claude-*`, `haiku`, `sonnet`, `opus` → Claude
- `gpt-*`, `o1-*`, `o3-*`, `codex-*` → Codex
- `gemini-*` → Gemini
- 나머지는 HTTP 400 `invalid_request_error`

### Cross-provider 대화
같은 `user` 값으로 다른 `model` 보내면 직전 8턴 컨텍스트가 새 provider 에
자동 주입됨. 코드 변경 없이 "claude 로 시작 → codex 로 이어받기" 가 가능.

## Provider 별 팁

### Claude
- 기본 모델 `claude-haiku-4-5`. alias `haiku`/`sonnet`/`opus` 전부 허용
- 도구 사용 자동 허용하려면 `permission_mode="bypassPermissions"` (래퍼 기본값: web_search=True 일 때 자동 설정)
- 프로젝트 디렉토리 안에서 작업하려면 `cwd="..."` 전달

### Codex
- 기본 모델 `gpt-5.4-mini`. ChatGPT 구독에서는 `gpt-5`, `gpt-5.5`, `gpt-5-codex` 거부됨
- 파일 편집이 필요하면 `create("codex", full_auto=True, cwd=...)` 로
- 웹서치는 내부적으로 `-c tools.web_search=true` 로 활성화 (wrapper가 자동 처리)

### Gemini
- 기본 모델 `gemini-3.1-flash-lite-preview` (★ `-preview` 접미사 꼭 포함)
- 세션 resume 은 **인덱스 기반**이라 래퍼가 `--list-sessions` 로 UUID→index 매핑 (turn당 ~0.5s)
- `google_web_search` 는 구조적으로 항상 ON. `web_search=False` 는 `--approval-mode plan` 으로 근사
- 아무 디렉토리에서나 실행되도록 `skip_trust=True` 가 기본

## 에러 대처

| kind | 무엇 | 어떻게 |
|---|---|---|
| `auth_expired` | 구독 OAuth 만료 | 해당 CLI `login` 재실행, 또는 환경변수 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`GEMINI_API_KEY` 세팅 (래퍼가 자동 재시도) |
| `rate_limit` | 주간/일일 한도 초과 | 다른 provider 로 전환하거나 대기 |
| `model_not_allowed` | 모델이 계정에 없거나 오타 | `unified-cli models` 로 확인 |
| `not_found` | session_id 가 현재 cwd 에 없음 (주로 Gemini) | 만들었던 cwd 로 돌아가서 호출 |
| `network` | DNS/ECONNRESET | 래퍼가 이미 2회 재시도함. 네트워크 확인 |
| `config` | provider 이름 오타, 라우팅 실패 | 메시지 + hint 확인 |
| `internal` | 알 수 없음 | `cause` 필드에 원본 stderr 첫 줄 |

예시:
```python
from unified_cli import UnifiedError, create

try:
    create("claude").chat("...")
except UnifiedError as e:
    if e.kind == "auth_expired":
        print("Claude 로그인 필요:", e.hint)
    elif e.kind == "rate_limit":
        # fallback 을 다른 provider 로
        create("codex").chat("...")
    else:
        raise
```

## 자주 묻는 것

**Q. 여러 호출을 병렬로 돌리고 싶다**
→ `achat` / `astream` + `asyncio.gather` 사용. `examples/08_async.py` 참고.

**Q. web_search 가 있으면 토큰이 너무 많이 쓰인다**
→ 짧은 질의는 `create(provider, web_search=False)` 로. 불필요한 시스템 프롬프트 팽창을 막음.

**Q. conversation 이 너무 길어지면 컨텍스트 주입 문제 있나?**
→ 기본 `context_window=8` 로 최근 8턴만 prefix 에 들어감. 늘리거나 줄이려면 `UnifiedConversation(context_window=16)`.

**Q. 서버의 `x_session_id` / `x_provider` 가 뭔가?**
→ OpenAI 스키마 외 우리 확장 필드. conversation 내부 어느 provider 에서 어떤 세션으로 처리됐는지 추적용.

**Q. 모델 목록이 안 업데이트된다**
→ 메모리 캐시 1시간. `list_models(provider, force_refresh=True)` 또는 `unified-cli models --refresh`.

**Q. CI/서버에 올릴 때 OAuth 는 어떻게?**
→ Headless 환경에서 OAuth 는 불가. 각 provider 의 API 키 환경변수로만 운영:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
```

## 구조 이해 짧게

```
factory.create(provider, ...)         ← 가장 간단한 진입점
    └→ ClaudeProvider / CodexProvider / GeminiProvider
         └→ BaseProvider._run / _stream_run  ← subprocess + 재시도/폴백
              └→ errors.classify            ← 모든 실패를 UnifiedError 로

UnifiedConversation                    ← 여러 provider 믹스
    └→ _resolve + _context_prefix_if_switch
    └→ create() 를 내부에서 provider 별로 재사용

server.app (FastAPI)                   ← OpenAI 호환
    └→ route(model) → (provider, model)
    └→ 에러 → OpenAI 호환 {error:{type,...}}
```
