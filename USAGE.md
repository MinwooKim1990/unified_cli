# 사용 가이드

README 는 개요, 이 파일은 **자주 쓰는 패턴과 트러블슈팅**.

## 처음 시작

```bash
cd path/to/cli-wrapper-unified     # 저장소 clone 후 이동

# (최초 1회) 가상환경 + 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

# 환경 점검
unified-cli doctor
```

`doctor` 가 3개 CLI 경로 + auth 상태 + 모델 개수를 출력합니다. 뭐든 ✗ 가 뜨면
해당 CLI 를 먼저 설치/로그인하세요.

## Python 으로 쓰기

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
