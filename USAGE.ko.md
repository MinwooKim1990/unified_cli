# 사용 가이드

README 는 개요, 이 파일은 **자주 쓰는 패턴과 트러블슈팅**.

> ## ⚠️ 이용약관 & 계정 정지 위험
> 각 provider 의 이용약관(ToS) 준수 책임은 사용자 본인에게 있으며, 이 CLI 들을
> 자동화하면 약관을 위반할 수 있으니 **사용에 따른 위험은 본인이 부담**합니다.
> 권장되는 안전한 사용 방식은 **본인 구독으로 하는 개인·로컬·단독 사용**입니다
> (Anthropic 은 헤드리스 `claude -p` 를 공식 지원). OpenAI 호환 서버를 다른
> 사람/네트워크에 노출하거나, 다른 사람의 요청을 본인 구독으로 처리하거나,
> 자격증명을 공유하거나, 접근 권한을 재판매/프록시하지 **마세요** — 모두 ToS
> 위반이며 계정 정지/차단 위험이 있습니다. 이로 인한 세 가지 안전 기본값이
> 아래에 문서화되어 있습니다:
> - **`gemini` provider(Antigravity `agy`) 는 기본 비활성화** — Google 이 이를
>   자동화한 개인 계정을 차단했습니다. `UNIFIED_CLI_ENABLE_GEMINI=1` 로 옵트인.
> - **`unified-cli serve` 및 `python -m unified_cli.server` 런처는 기본적으로
>   `127.0.0.1`에 바인딩**되며, `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` 없이는
>   loopback 이 아닌 호스트를 거부합니다. raw `uvicorn`은 자체 host 설정을
>   따르지만, 같은 옵트인 전에는 앱의 ASGI 가드가 non-loopback bind·peer·Host
>   요청을 HTTP 403으로 거부합니다. 외부 mode에는 공백 없는 32 UTF-8 바이트 이상의
>   `UNIFIED_CLI_SERVER_AUTH_TOKEN`과 모든 요청의 `Authorization: Bearer …`
>   헤더도 필요합니다.
> - 서버는 기본적으로 범위 제한된 **Claude 전용** 프로필만 노출합니다. Codex와
>   `agy`는 독립 컨테이너/VM에서 운영자가 명시적으로 옵트인하지 않으면 HTTP
>   경계에서 거부됩니다.

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
   - Gemini: `agy` 첫 실행 (자동으로 OAuth)
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

**헤드리스(launchd / cron / systemd / 서버)로 실행하나요?** 래핑하는 CLI는
인터랙티브 TTY를 전제로 하므로 백그라운드에선 두 함정이 있습니다: 최소 `PATH`(바이너리
"없음")와, macOS에서 `claude`가 로그인 키체인 대기로 hang(키체인을 열 TTY 없음). 해결:

```bash
export CLAUDE_CLI_PATH=/opt/homebrew/bin/claude   # PATH 최소일 때
claude setup-token                                # 실제 터미널에서 한 번
export CLAUDE_CODE_OAUTH_TOKEN=<token>            # OAuth 등가, 종량 아님
unified-cli doctor --headless   # 서비스 컨텍스트에서 실행해 auth 되는지 증명
```

기본적으로 래퍼는 구독 OAuth로 실행되고 상속된
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`를 자식에서 제거합니다(export된 키로 몰래 종량
과금되지 않게). 전체 레시피는 README의 “launchd / cron / 서버에서 실행” 섹션 참고.

### 웹 대시보드

서버 기동:
```bash
uvicorn unified_cli.server:app --port 8000
```

브라우저에서 `http://localhost:8000/dashboard` → 5초마다 자동 갱신 (루트
`http://localhost:8000/` 도 `/dashboard` 로 리다이렉트). 리디자인된 레이아웃:
- 퀵 통계 카드 + provider 별 헬스 카드 (health, binary, auth, 모델 수)
- inline-SVG 스파크라인 (지연 / 토큰 볼륨) + 모델별 사용량 막대
- Usage totals (provider별 호출/에러/토큰/평균 지연)
- Active conversations (conversation id → 현재 provider → session_id)
- Recent calls (최근 30개, 시간/provider/모델/토큰/지연/프롬프트 일부/에러)
- 반응형 레이아웃

JSON 으로 가져오려면:
- `GET /v1/doctor` — provider 상태
- `GET /v1/usage` — aggregates + recent
- `GET /v1/conversations` — 활성 대화 목록

`doctor` 가 3개 CLI 경로 + auth 상태 + 모델 개수를 출력합니다. 뭐든 ✗ 가 뜨면
해당 CLI 를 먼저 설치/로그인하세요.

## 대화형 REPL

```bash
unified-cli repl                              # 설정된 기본 provider (변경 전 claude)
unified-cli repl --provider codex -m gpt-5.4-mini
unified-cli repl --no-web-search              # 웹서치 끄기
unified-cli repl --lang ko                    # 한국어 UI
```

REPL 은 `prompt_toolkit` 기반(**코어 의존성**이라 `pip install unified-cli`
만으로 동작 — `[repl]` 옵션 없음). 실제 터미널에서는 `/` 를 입력하면 모든 슬래시
명령이 **타이핑하는 즉시 드롭다운**으로 떠서 외울 필요가 없습니다. TTY 가
아니면(파이프 등) 같은 명령을 쓰는 평범한 `input()` 루프로 폴백합니다.

| 명령 | 동작 |
|---|---|
| `/help` | 명령 목록 (현재 언어로) |
| `/model [name]` | 인자 없으면 **선택기** (provider 별 최신 모델, 기본값 ★); `/model <name>` 은 같은 provider 에서 모델 변경 (멀티워드 `agy` display name 도 지원) |
| `/provider [name]` | 인자 없으면 provider **선택기**; 어느 쪽이든 직전 8턴 컨텍스트 자동 주입 |
| `/status` | REPL 안 라이브·자동 갱신 상태 패널 (Ctrl+C 로 프롬프트 복귀) |
| `/lang <en\|ko>` | UI 언어 즉시 전환 + `~/.unified-cli/settings.json` 에 저장 |
| `/new` | 대화 초기화 |
| `/save` | 현재 session_id + 이어쓰기 명령 표시 |
| `/history [N]` | 최근 N 턴 표시 (기본 10) |
| `/tokens` | 이 REPL 세션의 provider 별 누적 사용량 |
| `/doctor` | provider 헬스 한 줄 |
| `/image <path>` | 다음 prompt 에 이미지 첨부 (반복 가능) |
| `/images` | 첨부 목록 |
| `/clear-images` | 첨부 비우기 |
| `/exit`, `/quit`, Ctrl+D | 종료 (마지막 session_id 자동 저장 → `chat --continue`) |

명령 히스토리는 세션 간 `~/.unified-cli/repl_history` 에 저장됩니다 (파일 권한
`0o600` 으로 생성).

## 언어 설정 (i18n)

CLI/REPL 전체가 현지화되어 있습니다. 기본은 영어, 한국어 선택 가능. 해석
우선순위:

1. `--lang {en,ko}` 전역 플래그 (예: `unified-cli --lang ko chat "안녕"`)
2. `~/.unified-cli/settings.json` (REPL 의 `/lang` 으로 기록)
3. `$UNIFIED_CLI_LANG` 환경변수 (`export UNIFIED_CLI_LANG=ko`)
4. 기본값: 영어

```bash
unified-cli --lang ko doctor          # 단발 한국어 출력
export UNIFIED_CLI_LANG=ko            # 셸 세션 전체 한국어
# REPL 안에서:
[claude/haiku] > /lang ko             # 즉시 전환 + 저장
```

## 이미지 입력 (멀티모달, 3 provider 모두)

```python
from unified_cli import create
create("claude").chat("describe", images=["cat.png"])
create("codex").chat("describe", images=["cat.png"])
create("gemini").chat("describe", images=["cat.png"])  # default gemini-3.5-flash
```

직접 Python/CLI 호출에는 신뢰하는 로컬 데이터만 사용하세요(한 호출에 섞어 써도
됩니다):

```python
from pathlib import Path
from unified_cli import Attachment

images=[
    "cat.png",                                 # 로컬 파일 경로 (str)
    Path("/tmp/dog.jpg"),                      # pathlib.Path
    open("photo.webp","rb").read(),            # bytes
    Attachment(path="cat.png", media_type="image/png"),  # 명시
    Attachment(bytes_=raw_bytes, media_type="image/png"), # 명시 bytes
]
```

provider 별 메커니즘 (래퍼가 자동 처리):

| Provider | 방식 | 비고 |
|---|---|---|
| **Codex** | `-i, --image <FILE>` 플래그 | native, 반복 가능. codex CLI ≥ 0.129 필요. image 첨부 시 prompt 가 stdin 으로 전송됨 (CLI 요구사항). |
| **Claude** | Read 도구 | 이미지용 `Read`를 허용하고 경로를 prompt 앞에 넣어 Claude Code가 vision 처리합니다. `bypassPermissions`는 자동 설정하지 않습니다. |
| **Gemini (`agy`)** | `@<path>` 참조 | 경로가 prompt 앞에 삽입됩니다. 권한 승인은 기본 유지되고, 위험한 `skip_permissions=True`를 명시할 때만 건너뜁니다. |

`http(s)` URL과 `data:` URI는 검증을 위해 정규화되지만 subprocess provider에서
의도적으로 `UnifiedError(kind="config")`를 냅니다. 래퍼는 원격 이미지를
가져오거나 신뢰하지 않는 URL을 로컬 파일 읽기로 바꾸지 않으므로, 신뢰하는 데이터를
직접 다운로드/디코드한 뒤 path 또는 bytes로 넘기세요.

provider 별 형식 / 한도:
- **Claude** — PNG / JPEG / GIF / WebP. 한 요청 ~100매, 32MB
- **Codex** — vision 가능 모델이 받는 형식 (보통 PNG/JPEG/WebP)
- **Gemini** — PNG / JPEG / WEBP / HEIC / HEIF. 3,600매/req, inline 20MB

CLI:
```bash
unified-cli chat "describe" --image foo.png --image bar.jpg -m gpt-5.4-mini
```

REPL:
```text
[claude/haiku] > /image photo.png
[claude/haiku] > /image second.jpg
[claude/haiku] > 두 사진의 차이?
```

OpenAI 호환 서버 (multi-content 스키마):
```python
client.chat.completions.create(
    model="haiku",
    messages=[{"role":"user","content":[
        {"type":"text","text":"describe"},
        {"type":"image_url",
         "image_url":{"url":"data:image/png;base64,iVBOR..."}}
    ]}],
)
```

HTTP 경계는 직접 API보다 더 엄격합니다:

- `image_url.url`은 정규 base64 `data:image/png;base64,...`,
  `data:image/jpeg;base64,...`, `data:image/gif;base64,...`,
  `data:image/webp;base64,...` 중 하나여야 하고, 디코딩된 시그니처가 선언한
  MIME type과 일치해야 합니다.
- 원격 URL과 파일시스템 경로는 가져오거나 읽지 않고 거부합니다.
- 기본 한도는 메시지당 4장, 이미지 하나당 디코딩 후 4 MiB, 요청 본문 24 MiB입니다.
  운영자는 `UNIFIED_CLI_SERVER_MAX_IMAGES`,
  `UNIFIED_CLI_SERVER_MAX_IMAGE_BYTES`,
  `UNIFIED_CLI_SERVER_MAX_BODY_BYTES` 환경변수로 명시적으로 조정할 수 있습니다.

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

### 패턴 8 — 이미지 입력 (멀티모달)

```python
from unified_cli import create

# 3 provider 모두 같은 images= 파라미터
for p, m in [("claude","haiku"), ("codex","gpt-5.4-mini"),
             ("gemini","gemini-3.5-flash")]:
    r = create(p, model=m).chat(
        "이 이미지에 무슨 색?",
        images=["/path/to/cat.png"],
    )
    print(p, "→", r.text.strip())

# 한 호출에 여러 형식 섞기
create("codex").chat(
    "두 사진 비교",
    images=["left.png", b"\\x89PNG...raw..."],
)

# 스트리밍 + image
for msg in create("gemini", model="gemini-3.5-flash").stream(
    "각각 묘사해", images=["a.png", "b.png"],
):
    if msg.kind == "text":
        print(msg.text, end="", flush=True)
```

세부 동작 / 한도는 위의 **이미지 입력 (멀티모달, 3 provider 모두)** 섹션 참고.

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

# --continue 는 유효한 저장 provider/model/작업 디렉토리를 복원하며
# 명시한 --cwd 가 항상 우선
unified-cli chat "이 체크아웃에서 계속" --continue --cwd ~/work/project

# -m/--provider·저장 세션이 없을 때 사용할 기본 provider
unified-cli config default-provider codex
unified-cli config default-provider --reset

# provider 탐색 없이 설치된 패키지 버전만 출력
unified-cli --version
```

## OpenAI 호환 서버로 쓰기

기존 OpenAI 클라이언트 코드를 그대로 재활용하고 싶을 때.

```bash
source .venv/bin/activate
# Uvicorn의 기본 host는 127.0.0.1이며, 명시한 외부 host는 external mode 없이는
# 앱의 ASGI 가드가 거부합니다.
uvicorn unified_cli.server:app --port 8000
# 대시보드:  http://localhost:8000/dashboard  (리디자인: 통계 카드, 헬스 카드,
#            지연/토큰 스파크라인, 모델별 사용량 막대)
#            http://localhost:8000/           → /dashboard 로 리다이렉트
```

> **기본 localhost 전용.** `unified-cli serve` 및
> `python -m unified_cli.server`는 `127.0.0.1`에 바인딩하고,
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` 없이는 loopback 이 아닌 호스트
> (`--host 0.0.0.0` 등)를 **거부**합니다. raw `uvicorn ... --host 0.0.0.0`은
> listener를 열 수 있지만, 같은 옵트인 전에는 앱의 ASGI 가드가 non-loopback
> bind·peer·Host를 HTTP 403으로 거부합니다. 기동 시 개인용 경고 로그도 출력합니다.
> 본인 구독을 다른 사람/네트워크에 노출하면 provider ToS 위반이며 **계정 차단 위험**이
> 있으니 로컬에서만 사용하세요.

> **외부 모드는 공개 프록시가 아니라 단일 신뢰 클라이언트용입니다.**
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`과 공백 없는 32 UTF-8 바이트 이상의
> `UNIFIED_CLI_SERVER_AUTH_TOKEN`을 모두 설정해야 하며, 진단을 포함한 모든 route에
> `Authorization: Bearer <token>`이 필요합니다. TLS 뒤에 두고 사용자별 권한으로
> 오해하지 마세요. 대시보드는 loopback 사용을 전제로 합니다.

> **Provider 격리.** 기본 HTTP 프로필은 Claude 모델만 허용합니다. 텍스트 요청은
> Claude safe mode + 도구 없음으로, 이미지 요청은 전달한 이미지 바이트만 읽는
> 범위 제한 권한으로 실행합니다. Codex와 `agy`는 임의 HTTP 요청에 대한 기밀
> 데이터 격리를 보장하는 sandbox가 아니므로 기본 거부됩니다.
> `UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS=1` 은 의도적으로 범위를 좁힌
> workspace mount가 있는 독립 컨테이너/VM 안에서만 설정하세요. 이 옵트인은
> 인증 기능도 아니고 네트워크 공개를 안전하게 만드는 기능도 아닙니다.

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

의도적으로 제한된 외부 배포에서는 SDK API 키에 같은 토큰을 넣고 패키지 밖에서 TLS를
종단하세요.

```python
import os
c = OpenAI(base_url="https://trusted.example/v1",
            api_key=os.environ["UNIFIED_CLI_SERVER_AUTH_TOKEN"])
```

### 모델명 라우팅 규칙
- `claude/<m>`, `claude-*`, `haiku`, `sonnet`, `opus` → Claude (기본 허용)
- `codex/<m>`, `gpt-*`, `o1-*`, `o3-*`, `codex-*` → Codex (기본 HTTP 403)
- `gemini/<m>`, `gemini-*` → Gemini / `agy` (기본 HTTP 403)
- 나머지는 HTTP 400 `invalid_request_error`.

### 대화 연속성
같은 `user` 값은 이후 Claude 요청에 대해 범위가 제한된 로컬 히스토리(기본 최근
8턴)를 유지합니다. Cross-provider HTTP handoff는 위의 명시적·외부 sandbox
옵트인 뒤에만 가능하며, 로컬 cross-provider 작업은 직접 Python
`UnifiedConversation` 사용을 권장합니다.

## Provider 별 팁

### Claude
- 기본 모델 `claude-haiku-4-5`. alias `haiku`/`sonnet`/`opus` 전부 허용
- 무인 도구 사용에는 권한 모드를 의도적으로 선택하세요. 래퍼는 웹서치나 이미지가
  켜졌다는 이유만으로 권한 모드를 바꾸지 않습니다.
  `permission_mode="bypassPermissions"`는 넓은 권한을 주므로 신뢰하는 로컬 입력에만 사용하세요.
- 프로젝트 디렉토리 안에서 작업하려면 `cwd="..."` 전달

### Codex
- 기본 모델 `gpt-5.4-mini`. ChatGPT 구독에서는 `gpt-5`, `gpt-5.5`, `gpt-5-codex` 거부됨
- 파일 편집이 필요하면 `create("codex", full_auto=True, cwd=...)` 로
- 웹서치는 내부적으로 `-c tools.web_search=true` 로 활성화 (wrapper가 자동 처리)

`CodexProvider.config_overrides`에는 점으로 연결한 bare TOML 키와 `str`, `bool`,
`int`, 유한 `float`, 또는 그 값들로 된 중첩 `list`/`tuple`만 전달하세요. 문자열은
`codex exec`에 넘기기 전에 안전하게 TOML 인용 처리되며, 다른 값은 모호하게 전달하지
않고 `ValueError`를 냅니다.

### Gemini (이제 Antigravity `agy` CLI)
- ⚠️ **기본 비활성화.** `agy` 자동화로 Google 개인 계정이 **차단된** 사례가 있어, `gemini` provider 는 환경변수 `UNIFIED_CLI_ENABLE_GEMINI=1` 이 설정됐을 때만 활성화됩니다. 없으면 직접 CLI·Python의 `gemini`/`agy` 호출은 config 에러를 냅니다. HTTP 서버는 별도의 더 엄격한 정책으로 이 gate가 설정돼도 기본 Gemini 요청을 HTTP 403으로 거부하며, 외부 sandbox 안의 별도 agentic-provider 옵트인이 필요합니다. 본인 책임 하에 직접 사용을 켜기:
  ```bash
  export UNIFIED_CLI_ENABLE_GEMINI=1
  ```
- 구 `gemini` CLI 는 개인 계정 차단(IneligibleTier). `gemini` provider 는 `agy`(`~/.local/bin/agy`)를 래핑.
- 기본 모델 `gemini-3.5-flash`. `agy --model` 은 슬러그(`gemini-3.5-flash`, `gemini-3.1-pro`) 와 `agy models` 의 display name(`Gemini 3.5 Flash (Medium)`, `Claude Sonnet 4.6 (Thinking)`, `GPT-OSS 120B (Medium)` 등) 둘 다 허용. 잘못된 이름은 조용히 default 로 폴백.
- 세션은 `--conversation <UUID>`/`--continue`; id 는 `~/.gemini/antigravity-cli/conversations/` 의 최신 .db 에서 복구.
- 에이전틱이라 웹서치를 스스로 판단해 수행 — on/off 토글 없음(`web_search=` 사실상 no-op).
- 헤드리스 출력이 평문이라 토큰 usage 보고 없음(usage=None). 에이전틱 루프라 기본 timeout 300s.
- 권한 승인은 기본으로 켜져 있습니다. `skip_permissions=True`를 명시하면
  `--dangerously-skip-permissions`를 전달하므로, 신뢰하는 로컬 자동화에만 쓰는
  위험한 옵트인입니다.

## 에러 대처

| kind | 무엇 | 어떻게 |
|---|---|---|
| `auth_expired` | 구독 OAuth 만료 | 해당 CLI `login` 재실행. Claude/Codex는 설정된 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`로 1회 재시도할 수 있고, `agy`는 OAuth 전용 |
| `rate_limit` | 주간/일일 한도 초과 | 다른 provider 로 전환하거나 대기 |
| `model_not_allowed` | 모델이 계정에 없거나 오타 | `unified-cli models` 로 확인 |
| `not_found` | session_id 가 현재 cwd 에 없음 (주로 Gemini) | 만들었던 cwd 로 돌아가서 호출 |
| `network` | DNS/ECONNRESET | 래퍼가 이미 2회 재시도함. 네트워크 확인 |
| `resource_limit` | 로컬 출력·스트림·HTTP 안전 한도 도달 | 요청/출력량을 줄이고, 신뢰하는 작업에서만 명시적 한도를 높이기 |
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
→ 먼저 provider가 지원하는 headless 인증을 쓰세요. Claude는 `claude setup-token`으로
만든 `CLAUDE_CODE_OAUTH_TOKEN`을 쓰고, Codex는 자체 CLI 로그인 상태를 사용합니다.
`agy`는 기존 OAuth 세션이 필요하며 API 키 폴백이 없습니다. 종량 과금을 의도할 때만
`ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY`를 폴백으로 설정하세요:
```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token>
# 선택 사항: 의도적으로 종량 과금을 쓸 때만
# export ANTHROPIC_API_KEY=sk-ant-...
# export OPENAI_API_KEY=...
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
