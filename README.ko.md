# unified-cli

[![PyPI version](https://img.shields.io/pypi/v/unified-cli)](https://pypi.org/project/unified-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/unified-cli)](https://pypi.org/project/unified-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🇺🇸 [English README](README.md) · 📖 [상세 가이드 (한국어)](USAGE.ko.md) · 📖 [Detailed usage (EN)](USAGE.md)

Claude Code / OpenAI Codex / Google Antigravity(`agy`) 세 CLI를 **하나의 Python API + CLI** 로 통합.

> Google 쪽 provider 키는 여전히 `"gemini"` (그리고 `-m gemini-3.5-flash` 등도 그대로 라우팅) 이지만, 내부적으로 **Antigravity `agy` CLI** 를 래핑합니다 — 2026년 구 `gemini` CLI 가 개인 계정에서 차단됐기 때문. 아래 마이그레이션 노트 참고.
>
> ⚠️ **`gemini` provider 는 기본 비활성화** 입니다. `agy` 자동화로 Google 개인 계정이 차단된 사례가 있어, `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정해야만 본인 책임 하에 켜집니다 — [이용약관 & 계정 정지 위험](#️-이용약관--계정-정지-위험--사용-전-반드시-읽기) 참고.

## 설치

```bash
pip install unified-cli
```

OpenAI 호환 HTTP 서버까지 쓰려면 `server` 옵션 의존성을 함께 설치:

```bash
pip install "unified-cli[server]"
```

> **사전 준비 — 이 패키지는 아무것도 설치하거나 로그인시키지 않습니다.**
> `unified-cli` 는 이미 설치된 공식 에이전틱 CLI 들에 그저 명령을 위임하는 얇은
> 래퍼입니다. **API 키도 자격증명도 포함하지 않으며**, 자체적으로 **어떤
> 자격증명도 저장하거나 전송하지 않습니다** — 모든 호출은 사용자 머신에 이미
> 되어 있는 로그인을 그대로 재사용합니다.
>
> 각 provider 를 쓰려면 해당 CLI 가 설치되어 있고 **본인 구독으로 로그인**되어
> 있어야 합니다:
>
> - **Claude** → `claude` CLI (Claude Code), Claude Pro/Max 로그인
> - **Codex** → `codex` CLI, ChatGPT Plus/Pro 로그인
> - **Gemini** → `agy` CLI (Google Antigravity), Google Antigravity 계정 로그인
>
> 셋 다 필요하지 않습니다 — **일부만 있어도 동작**합니다. 래퍼는 `$PATH` 에서
> 발견되는 `claude` / `codex` / `agy` 만 사용합니다.

## ⚠️ 이용약관 & 계정 정지 위험 — 사용 전 반드시 읽기

> **각 provider 의 이용약관(ToS) 준수 책임은 사용자 본인에게 있습니다.** 이
> CLI 들을 자동화하면 약관을 위반할 수 있으니 **사용에 따른 위험은 본인이
> 부담**합니다. 약관은 계속 바뀌고 있으며(2026년 2월 명확화), 이 문서는 법률
> 자문이 아닙니다.

- **권장되는 안전한 사용 방식 = 본인 구독으로 하는 개인·로컬·단독 사용.**
  Anthropic 은 헤드리스 `claude -p` / 프로그래밍 방식 사용을 **공식적으로
  지원**하므로 그 경로는 위험이 낮습니다. 래퍼를 절대 다른 사람에게 노출하지
  마세요.
- **하지 말 것:** OpenAI 호환 서버를 공개/네트워크 인터페이스로 띄우기, 다른
  사람의 요청을 본인 구독으로 처리하기, 자격증명 공유, 접근 권한 재판매/프록시.
  이것들은 provider 의 ToS 위반이며 **계정 정지 또는 영구 차단 위험**이 있습니다.
- **Antigravity (`agy` / `gemini` provider) 가 가장 위험합니다.** Google 은
  이를 자동화한 **개인 계정을 실제로 차단**했습니다(차단이 Gemini CLI / Code
  Assist 까지 연쇄 적용). 그래서 `gemini` provider 는 이제 **기본 비활성화**
  되어 있으며, 환경변수 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정해야만 본인 책임
  하에 켜집니다.
- **OpenAI 호환 서버는 기본적으로 `127.0.0.1`(localhost) 에 바인딩**되며,
  `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` 을 설정하지 않는 한 **loopback 이 아닌
  바인딩을 거부**합니다. 기동 시 개인용 경고 로그도 출력합니다.
- 이 패키지는 **자격증명을 전혀 포함하지 않습니다** — 각 사용자가 본인 구독을
  가져오며, 어떤 자격증명도 대신 저장·전송하지 않습니다.

- 구독 OAuth (Pro/Max, ChatGPT Plus/Pro, Antigravity) 로 로그인되어 있으면 **구독 크레딧으로** 실행
- Claude/Codex 는 API 키 환경변수로 **자동 폴백** (agy 는 OAuth 전용)
- **이미지 입력 멀티모달** — 3 provider 전부. Claude 는 Read 도구, Codex 는 `-i` 플래그, Gemini(`agy`) 는 `@<path>` 참조
- 히스토리 · 스트리밍 · 도구 사용 · **웹서치 기본 ON** · OpenAI 호환 HTTP 서버
- 대화형 **REPL** (`unified-cli repl`) + 슬래시 명령
- 명시적 에러 분류 (auth_expired / rate_limit / model_not_allowed / not_found / network / config / internal)

## 소스에서 설치 (개발용)

```bash
git clone https://github.com/MinwooKim1990/unified_cli.git cli-wrapper-unified
cd cli-wrapper-unified
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

unified-cli setup      # 최초 1회: 대화형 온보딩 위저드 (아래 설명 참고)
unified-cli doctor     # 언제든지 환경 상태 점검
unified-cli status     # 사용량/최근 호출 스냅샷
```

Python 3.9+ 와 `claude` / `codex` / `agy` 중 **최소 하나가 이미 설치 + 로그인**되어
있어야 합니다 — 위 **사전 준비** 참고. `setup` 위저드는 빠진 CLI 의 공식 설치
명령을 *제안*하고 각 provider 의 브라우저 로그인을 열어줄 뿐이며, 자격증명을
저장하지 않고 어느 단계든 거부할 수 있습니다.

### CLI 세션 관리

`unified-cli chat` 은 매 호출마다 session_id 를 `~/.unified-cli/state.json` 에 저장.
다음 호출에서 `--continue` 로 이어쓰기 가능:

```bash
unified-cli chat "내 이름은 민우"              # 새 대화 → state 저장
unified-cli chat "내 이름?" --continue         # 직전 세션 이어쓰기 → "민우" 답변
unified-cli chat "..." --resume <session_id>   # 특정 세션 이어쓰기
unified-cli chat "..." --new                    # state 리셋 + 새 대화
```

### 이미지 입력 (3 provider 모두)

```bash
# CLI
unified-cli chat "이 이미지에 무슨 색?" --image cat.png -m haiku
unified-cli chat "두 그림 비교해" --image a.jpg --image b.jpg -m gpt-5.4-mini
unified-cli chat "describe" --image photo.png -m gemini-3.5-flash
```

```python
# Python — 모든 provider 동일 인터페이스
from unified_cli import create
create("claude").chat("describe", images=["photo.png"])
create("codex").chat("describe", images=[image_bytes])
create("gemini").chat("describe", images=["https://example.com/x.jpg"])
```

provider 별 처리:
- **Claude** — Read 도구 자동 활성화 + bypassPermissions, prompt 앞에 경로 prepend
- **Codex** — native `-i, --image` 플래그 (codex CLI 0.129+ 필요)
- **Gemini (`agy`)** — `@<path>` 참조를 prompt 앞에 삽입 + `--dangerously-skip-permissions` 로 에이전트가 파일을 읽게 함

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
| `/image <path>` | 다음 prompt 에 이미지 첨부 (반복 가능) |
| `/images` | 첨부 목록 |
| `/clear-images` | 첨부 비우기 |
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
| gemini (`agy`) | `gemini-3.5-flash` |

모델명만 알면 provider 자동 라우팅:

```python
from unified_cli import route
route("haiku")                    # ('claude', 'haiku')
route("gpt-5.4-mini")             # ('codex', 'gpt-5.4-mini')
route("gemini-3.5-flash")         # ('gemini', 'gemini-3.5-flash')
route("claude/sonnet")            # 명시 prefix도 지원
```

## 통합 대화 (provider 자유 전환)

```python
from unified_cli import UnifiedConversation

conv = UnifiedConversation()   # sticky=False 가 기본
conv.send("내 이름은 민우야", provider="claude")
conv.send("내 이름 뭐였지?", provider="codex")     # ← 자동으로 Claude 대화의 직전 8턴을
                                                      #   Codex 프롬프트 앞에 컨텍스트로 주입
conv.send("내 이름 한 번 더 말해", provider="gemini")  # UNIFIED_CLI_ENABLE_GEMINI=1 필요
```

> `gemini` provider 는 **기본 비활성화** 입니다(Antigravity `agy` 자동화로 Google 계정이 차단된 사례 있음). 위·아래 `gemini` 예제는 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 먼저 설정해야 동작합니다.

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

> Gemini provider는 이제 Antigravity `agy` CLI를 래핑합니다. agy는 에이전틱이라 웹서치를 스스로 판단해 수행하며 on/off 토글이 없습니다 (`web_search=`는 사실상 no-op). 단, **기본 비활성화**라 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정해야 사용할 수 있습니다(`agy` 자동화 계정 차단 위험).

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
- **auth_expired**: Claude/Codex 는 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 환경변수가 있으면 **자동으로 1회 재시도**. 없으면 hint 포함한 에러 raise. Gemini provider(Antigravity `agy`)는 **OAuth 전용**이라 API 키 폴백이 없으니 `agy` 재로그인이 필요
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
uvicorn unified_cli.server:app --port 8000   # 기본 127.0.0.1(localhost) 바인딩
```

> **기본 localhost 전용.** 서버는 `127.0.0.1` 에만 바인딩하며,
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` 을 설정하지 않는 한 loopback 이 아닌
> 호스트(`0.0.0.0` 등) 바인딩을 **거부**합니다. 기동 시 개인용 경고 로그도
> 출력합니다. 본인 구독을 다른 사람/네트워크에 노출하면 provider ToS 위반이며
> **계정 차단 위험**이 있으니 로컬에서만 사용하세요.

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

# 평범한 텍스트
r = client.chat.completions.create(
    model="haiku",
    messages=[{"role":"user","content":"hi"}],
    user="my-conv",
)

# 이미지 입력 (OpenAI multi-content 스키마, 3 provider 모두)
r = client.chat.completions.create(
    model="gpt-5.4-mini",
    messages=[{"role":"user","content":[
        {"type":"text","text":"describe"},
        {"type":"image_url",
         "image_url":{"url":"data:image/png;base64,iVBOR..."}}
    ]}],
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
| Gemini (`agy`) | `agy models` 출력 (Antigravity CLI 가 직접 표시) | 1시간 |

`agy` 를 찾지 못하거나 호출에 실패하면 하드코딩된 주요 모델 리스트로 폴백.
**임의 모델 ID 는 리스트에 없어도 그대로 CLI 에 전달** — allowlist 는 정보용.

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
- Gemini(`agy`)는 헤드리스 출력이 평문이라 토큰 사용량 보고가 없음(usage=None). 세션은 `--conversation <UUID>`/`--continue`, id는 `~/.gemini/antigravity-cli/conversations/`의 최신 .db에서 복구. 에이전틱 루프라 기본 timeout 300s

## 라이선스

MIT License · Copyright (c) 2026 Minwoo Kim — 전문: [LICENSE](LICENSE).

누구나 자유롭게 사용·수정·재배포 가능. 단, 재배포 시 저작권 표시와 라이선스
문구를 그대로 유지해야 합니다. 각 provider 구독 사용은 해당 provider 의
이용약관(ToS)에 따른 본인 책임.
