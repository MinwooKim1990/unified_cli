# unified-cli

🇺🇸 [English README](README.md) · 📖 [상세 가이드 (한국어)](USAGE.ko.md) · 📖 [Detailed usage (EN)](USAGE.md)

Claude Code / OpenAI Codex / Google Gemini 세 CLI를 **하나의 Python API** 로 통합.

- 구독 OAuth (Pro/Max, ChatGPT Plus/Pro, Google) 로 로그인되어 있으면 **구독 크레딧으로** 실행
- API 키 환경변수만 있으면 **자동 폴백**
- 히스토리 · 스트리밍 · 도구 사용 · **웹서치 기본 ON** · OpenAI 호환 HTTP 서버
- 명시적 에러 분류 (auth_expired / rate_limit / model_not_allowed / not_found / network / config / internal)

## 설치

```bash
git clone <repo-url> cli-wrapper-unified   # 또는 로컬 경로로 이동
cd cli-wrapper-unified
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

unified-cli setup      # 최초 1회: 대화형 온보딩 (CLI 설치 + 로그인 + 검증)
unified-cli doctor     # 언제든지 환경 상태 점검
unified-cli status     # 사용량/최근 호출 스냅샷
```

### CLI 세션 관리

`unified-cli chat` 은 매 호출마다 session_id 를 `~/.unified-cli/state.json` 에 저장.
다음 호출에서 `--continue` 로 이어쓰기 가능:

```bash
unified-cli chat "내 이름은 민우"              # 새 대화 → state 저장
unified-cli chat "내 이름?" --continue         # 직전 세션 이어쓰기 → "민우" 답변
unified-cli chat "..." --resume <session_id>   # 특정 세션 이어쓰기
unified-cli chat "..." --new                    # state 리셋 + 새 대화
```

### 대화형 REPL (`unified-cli repl`)

한 프로세스에서 multi-turn + provider 교체:

```bash
unified-cli repl                          # 기본 Claude 로 시작
unified-cli repl --provider codex -m gpt-5.4-mini
```

슬래시 명령:

| 명령 | 동작 |
|---|---|
| `/help` | 명령 목록 |
| `/model <name>` | 같은 provider 에서 모델 변경 |
| `/provider <name>` | provider 전환 (이전 8턴 컨텍스트 자동 주입) |
| `/new` | 대화 초기화 |
| `/save` | 현재 session_id + 이어쓰기 명령 표시 |
| `/history [N]` | 최근 N 턴 표시 |
| `/tokens` | 누적 사용량 |
| `/doctor` | provider 헬스 한 줄 |
| `/exit` or Ctrl+D | 종료 (마지막 session_id 자동 저장) |

REPL 종료 후 `unified-cli chat "..." --continue` 로도 대화가 이어집니다.

`unified-cli setup` 은 3개 CLI(`claude`/`codex`/`gemini`) 중 빠진 것을 감지해서:
1. 패키지 매니저(brew/npm) 로 설치 명령 제안 → Y/n 동의 후 실행
2. 로그인 안 된 provider 는 `login` 명령 spawn → 브라우저 OAuth 로 유도
3. 각 provider 에 "say hi" 테스트 호출로 최종 검증

중간에 거부하면 수동으로 실행할 명령만 출력하고 넘어갑니다.

### 웹 대시보드

서버 기동 후 브라우저에서 **`http://localhost:8000/dashboard`** 접속하면:
- 3 provider 헬스 상태
- 누적 사용량 (provider/모델별 호출수, 토큰, 평균 지연)
- 최근 30개 호출 로그
- 활성 대화 목록

5초마다 자동 갱신. 외부 의존성 없는 단일 HTML + inline JS.

의존성: Python 3.9+, 각 provider의 CLI (자동 탐색).

## 실행 가능한 예제

`examples/` 디렉토리에 8개의 실행 가능한 스크립트가 있습니다. 바로 `python examples/XX.py` 로 실행.

| 파일 | 내용 |
|---|---|
| `examples/01_hello.py` | 3 provider 인사 — 가장 단순한 단일 호출 |
| `examples/02_history.py` | 한 provider 안에서 대화 이어쓰기 |
| `examples/03_multi_provider.py` | provider 자유 전환 + 컨텍스트 자동 주입 |
| `examples/04_streaming.py` | 스트리밍 이벤트 종류별 처리 |
| `examples/05_web_search.py` | 3 provider 전부 웹서치 호출 |
| `examples/06_error_handling.py` | `UnifiedError` 분류 시연 |
| `examples/07_openai_sdk.py` | OpenAI Python SDK 로 로컬 서버 호출 |
| `examples/08_async.py` | `achat` / `astream` / `asyncio.gather` |

더 상세한 사용 가이드 / 트러블슈팅: [USAGE.ko.md](USAGE.ko.md) (한국어) · [USAGE.md](USAGE.md) (English)

## 빠른 시작

```python
from unified_cli import create

# 기본 provider = Claude, 기본 모델 = claude-haiku-4-5
cli = create("claude")
resp = cli.chat("안녕")
print(resp.text, resp.session_id, resp.usage.input_tokens)
```

Provider별 기본 모델:
| Provider | 기본 모델 |
|---|---|
| claude | `claude-haiku-4-5` |
| codex | `gpt-5.4-mini` |
| gemini | `gemini-3.1-flash-lite-preview` |

모델명만 알면 provider 자동 라우팅:

```python
from unified_cli import route
route("haiku")                    # ('claude', 'haiku')
route("gpt-5.4-mini")             # ('codex', 'gpt-5.4-mini')
route("gemini-3.1-flash-lite-preview")  # ('gemini', '...')
route("claude/sonnet")            # 명시 prefix도 지원
```

## 통합 대화 (provider 자유 전환)

```python
from unified_cli import UnifiedConversation

conv = UnifiedConversation()   # sticky=False 가 기본
conv.send("내 이름은 민우야", provider="claude")
conv.send("내 이름 뭐였지?", provider="codex")     # ← 자동으로 Claude 대화의 직전 8턴을
                                                      #   Codex 프롬프트 앞에 컨텍스트로 주입
conv.send("내 이름 한 번 더 말해", provider="gemini")
```

같은 provider 로 연속 호출하면 native session (`--resume`) 으로 처리되어 효율적.
`sticky=True` 로 생성하면 첫 provider 에 고정되고 전환 시 에러.

## 스트리밍 + 도구 + 웹서치

```python
cli = create("claude")  # web_search=True 기본
for msg in cli.stream("오늘 최신 Python 버전은?"):
    if msg.kind == "text":
        print(msg.text, end="", flush=True)
    elif msg.kind == "tool_use":
        print(f"\n[tool: {msg.tool['name']}]", flush=True)
```

웹서치 비활성화:
```python
cli = create("claude", web_search=False)
```

> Gemini CLI는 `google_web_search`가 구조적으로 항상 ON이라 `web_search=False` 설정 시 `--approval-mode plan` 으로 근사 차단.

## 에러 분류 + 자동 복구

```python
from unified_cli import UnifiedError

try:
    cli.chat("...")
except UnifiedError as e:
    print(e.kind)      # auth_expired / rate_limit / model_not_allowed / ...
    print(e.provider)  # "claude"
    print(e.message)   # 사용자용 한국어 메시지
    print(e.hint)      # 복구 힌트 ("claude /login 재실행...")
```

동작:
- **auth_expired**: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` 환경변수가 있으면 **자동으로 1회 재시도**. 없으면 hint 포함한 에러 raise
- **network**: exponential backoff (0.5s, 1.5s) 로 최대 2회 재시도
- **rate_limit / model_not_allowed / not_found**: 즉시 raise

## CLI

```bash
# 환경 점검
unified-cli doctor

# 모델 리스트 (전부 / provider별)
unified-cli models
unified-cli models claude --refresh

# 단일 호출
unified-cli chat "hi" -m haiku
unified-cli chat "오늘 최신 Python?" -m claude/haiku --stream

# stdin 으로 프롬프트
cat prompt.txt | unified-cli chat -m gpt-5.4-mini
```

## OpenAI 호환 HTTP 서버

```bash
uvicorn unified_cli.server:app --port 8000
```

```bash
# non-streaming, 자동 라우팅
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"haiku","messages":[{"role":"user","content":"hi"}]}'

# streaming + 대화 지속 (user 필드로 conv id 지정)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"claude/haiku",
    "messages":[{"role":"user","content":"내 이름 민우"}],
    "stream":true,
    "user":"chat-42"
  }'

# 같은 user 로 다른 provider 에 이어 보내기 (컨텍스트 자동 주입)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"codex/gpt-5.4-mini",
    "messages":[{"role":"user","content":"내 이름?"}],
    "user":"chat-42"
  }'

# 모델 목록
curl http://localhost:8000/v1/models
curl http://localhost:8000/v1/models?provider=gemini
```

OpenAI Python SDK 그대로 사용:
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy")
r = client.chat.completions.create(
    model="haiku",
    messages=[{"role":"user","content":"hi"}],
    user="my-conv",
)
```

에러는 OpenAI 스키마로 정규화 매핑:
| UnifiedError.kind | HTTP | OpenAI `type` |
|---|---|---|
| auth_expired | 401 | authentication_error |
| rate_limit | 429 | rate_limit_error |
| model_not_allowed / config | 400 | invalid_request_error |
| not_found | 404 | not_found_error |
| network | 502 | upstream_error |
| internal | 500 | internal_error |

## 신규 모델 자동 반영

`list_models()` 는 각 provider에서 다음 소스로 가져옴:

| Provider | 소스 | TTL |
|---|---|---|
| Claude | `GET https://api.anthropic.com/v1/models` (`$ANTHROPIC_API_KEY` 있을 때) | 1시간 메모리 캐시 |
| Codex | `~/.codex/models_cache.json` (Codex CLI가 5분마다 업데이트) | 파일 기준 |
| Gemini | `GET https://generativelanguage.googleapis.com/v1/models` (`$GEMINI_API_KEY`) | 1시간 |

API 키 없을 때는 하드코딩된 주요 모델 리스트로 폴백. **임의 모델 ID 는 리스트에 없어도 그대로 CLI 에 전달** — allowlist 는 정보용.

## 패키지 구조

```
cli-wrapper-unified/
├── pyproject.toml
├── README.md
└── src/unified_cli/
    ├── __init__.py      # 공개 심볼 re-export
    ├── core.py          # Message, Response, Usage, ModelInfo
    ├── errors.py        # UnifiedError + classify (정규식 매칭 테이블)
    ├── discovery.py     # find_{claude,codex,gemini}_bin()
    ├── base.py          # BaseProvider (retry + api-key fallback 포함)
    ├── models.py        # list_models() dispatcher
    ├── factory.py       # create() + route()
    ├── conversation.py  # UnifiedConversation
    ├── cli.py           # unified-cli 명령어
    ├── server.py        # FastAPI OpenAI-호환 (선택 의존성)
    └── providers/
        ├── claude.py    # ClaudeProvider
        ├── codex.py     # CodexProvider (web_search: `-c tools.web_search=true`)
        └── gemini.py    # GeminiProvider (UUID ↔ index 자동 매핑)
```

## 주의

- 구독 기반 호출은 **3자 서비스로 재판매 금지** (각 provider ToS). 개인 로컬 자동화 전용
- `auth_expired` 자동 복구는 API 키 환경변수 fallback 뿐. 브라우저 로그인은 수동으로
- 호출당 Node/Rust 프로세스 spawn 오버헤드 ~수백 ms — 초저지연 시스템엔 부적합
- Gemini resume 은 UUID→index 조회로 turn당 `--list-sessions` 1회 추가 호출 (~0.5s)

## 라이선스

MIT License · Copyright (c) 2026 Minwoo Kim — 전문: [LICENSE](LICENSE).

누구나 자유롭게 사용·수정·재배포 가능. 단, 재배포 시 저작권 표시와 라이선스
문구를 그대로 유지해야 합니다. 각 provider 구독 사용은 해당 provider 의
이용약관(ToS)에 따른 본인 책임.
