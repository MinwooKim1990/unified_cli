# unified-cli

[![PyPI version](https://img.shields.io/pypi/v/unified-cli)](https://pypi.org/project/unified-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/unified-cli)](https://pypi.org/project/unified-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🇺🇸 [English README](README.md) · 📖 [상세 가이드 (한국어)](USAGE.ko.md) · 📖 [Detailed usage (EN)](USAGE.md)

Claude Code / OpenAI Codex / Google Antigravity(`agy`) 세 CLI를 **하나의 Python API + CLI** 로 통합.

> Google 쪽 provider 키는 여전히 `"gemini"` (그리고 `-m gemini-3.5-flash` 등도 그대로 라우팅) 이지만, 내부적으로 **Antigravity `agy` CLI** 를 래핑합니다 — 2026년 구 `gemini` CLI 의 개인 계정 접근이 제한됐기 때문입니다. 아래 마이그레이션 노트 참고.
>
> ⚠️ **`gemini` provider 는 기본 비활성화** 입니다. `agy` 자동화는 Google 서비스 이용 제한으로 이어질 수 있어, 적용되는 정책을 확인한 뒤에만 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정하세요 — [이용약관 및 Provider 사용 정책](#provider-usage-policy-ko) 참고.

## 설치

```bash
pip install unified-cli
```

여기엔 완전한 대화형 REPL(라이브 `/` 슬래시 메뉴, 모델/provider 선택기,
라이브 `/status`)이 포함됩니다 — `prompt_toolkit` 이 코어 의존성이라 별도
옵션 설치가 필요 없습니다.

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

## Core와 Ext

| | Core: `unified-cli` | Ext: [`unified-cli-ext`](https://pypi.org/project/unified-cli-ext/) |
|---|---|---|
| 포함 provider | Claude, Codex, Gemini (`agy`) | 11개 카탈로그 메타데이터: Grok, Kimi, Copilot, Cursor, CodeBuddy, Qoder, Mistral Vibe, Qwen, Cline, OpenCode, Kilo Code |
| 기본 동작 | 기존 기본값은 바뀌지 않음 | Core 기본값과 서버 허용 목록을 절대 변경하지 않음 |
| 현재 상태 | Core provider는 기존 동작을 유지 | 정확히 11개의 비활성 항목이 **Held**: 발견 가능한 메타데이터일 뿐, 실행 가능한 어댑터가 아니며 확장 서버 지원은 비활성화됨 |

Ext는 별도 PyPI 배포판이자 Python 모듈(`unified_cli_ext`)입니다. vendor CLI를
포함하지 않고, 로그인·서비스 호출·과금 발생을 하지 않습니다. provider 바이너리와
계정은 사용자가 직접 설치하고 관리합니다.

<details>
<summary>Ext 설치 및 카탈로그 메타데이터 확인</summary>

```bash
python -m pip install unified-cli-ext
python -c "import importlib.metadata as m; print([e.name for e in m.distribution('unified-cli-ext').entry_points if e.group == 'unified_cli.providers.v1'])"
```

이 확인은 설치된 provider 엔트리포인트 메타데이터만 나열합니다. Stage 5B–5D에서는
`grok`, `kimi`, `copilot`, `cursor`, `codebuddy`, `qoder`, `mistral-vibe`, `qwen`,
`cline`, `opencode`, `kilo`가 표시될 수 있으며 Ext 카탈로그는 11개 항목을 모두
**Held**로 분류합니다. provider 실행, vendor 바이너리 탐색, 인증, 네트워크 요청은 하지
않습니다. 목록에
이름이 있다는 사실을 채팅 명령으로 해석하면 안 됩니다.

`unified-cli providers --include-ext`는 import 없이 탐색하므로 처음에는 수명 주기
`discovered`, 지원 상태 `unknown`으로 표시합니다. 해당 provider를 명시적으로 요청할
때만 그 엔트리포인트 하나를 로드해 지원 상태 `held`를 확인하며, 실행은 계속 비활성입니다.

</details>

provider 카탈로그, 상태 의미, 활성화 전 필요한 근거는
[확장](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.ko.md)을 참고하세요.

<a id="provider-usage-policy-ko"></a>

## 이용약관 및 Provider 사용 정책 — 사용 전 확인

> **각 provider 의 이용약관(ToS) 준수 책임은 사용자 본인에게 있습니다.** 자동화가
> 모든 계정이나 사용 사례에서 허용되는 것은 아니며 서비스 이용이 제한될 수 있습니다.
> 약관은 계속 바뀌고 있으며(2026년 2월 명확화), 이 문서는 법률 자문이 아닙니다.

- **권장되는 안전한 사용 방식 = 본인 구독으로 하는 개인·로컬·단독 사용.**
  Anthropic 은 헤드리스 `claude -p` / 프로그래밍 방식 사용을 **공식적으로
  지원**하므로 그 경로는 위험이 낮습니다. 래퍼를 절대 다른 사람에게 노출하지
  마세요.
- **하지 말 것:** OpenAI 호환 서버를 공개/네트워크 인터페이스로 띄우기, 다른
  사람의 요청을 본인 구독으로 처리하기, 자격증명 공유, 접근 권한 재판매/프록시.
  이는 provider 정책과 충돌할 수 있으며 서비스 이용이 제한될 수 있습니다.
- **Antigravity (`agy` / `gemini` provider)는 추가 정책 확인이 필요합니다.** Google은
  이를 자동화한 개인 계정에서 관련 Gemini CLI / Code Assist 접근을 포함한 이용 제한
  사례를 알린 바 있습니다. 그래서 `gemini` provider는 **기본 비활성화**되어 있으며,
  적용되는 정책을 확인한 뒤에만 환경변수 `UNIFIED_CLI_ENABLE_GEMINI=1`을 설정하세요.
- **`unified-cli serve` 및 `python -m unified_cli.server` 런처는 기본적으로
  `127.0.0.1`(localhost)에 바인딩**되며, `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`을
  설정하지 않는 한 **loopback 이 아닌 호스트를 거부**합니다. raw `uvicorn`은
  Uvicorn 자체 host 설정을 따르지만, 같은 옵트인 전에는 앱의 ASGI 가드가
  non-loopback bind·peer·Host 요청을 HTTP 403으로 거부합니다. 외부 옵트인에는
  공백 없는 32 UTF-8 바이트 이상의 `UNIFIED_CLI_SERVER_AUTH_TOKEN`과 모든 요청의
  `Authorization: Bearer …` 헤더도 필요합니다. 이는 TLS 뒤의 단일 신뢰
  클라이언트용일 뿐 공개·다중 사용자 프록시를 만드는 방법이 아닙니다.
- 이 패키지는 **자격증명을 전혀 포함하지 않습니다** — 각 사용자가 본인 구독을
  가져오며, 어떤 자격증명도 대신 저장·전송하지 않습니다.

- 구독 OAuth (Pro/Max, ChatGPT Plus/Pro, Antigravity) 로 로그인되어 있으면 **구독 크레딧으로** 실행
- Claude/Codex 는 API 키 환경변수로 **자동 폴백** (agy 는 OAuth 전용)
- **이미지 입력 멀티모달** — 3 provider 전부. Claude 는 Read 도구, Codex 는 `-i` 플래그, Gemini(`agy`) 는 `@<path>` 참조를 사용합니다. 권한 우회는 자동으로 켜지지 않습니다.
- 히스토리 · 스트리밍 · 도구 사용 · **웹서치 기본 ON** · OpenAI 호환 HTTP 서버
- 대화형 **REPL** (`unified-cli repl`): `/` 입력 시 라이브 슬래시 메뉴, `/model`·`/provider` 선택기(최신 모델 표시, 기본값 ★), 라이브 `/status` — `prompt_toolkit` 기반
- **다국어(i18n)**: 기본 영어, `--lang ko`(또는 REPL 의 `/lang ko`, 또는 `UNIFIED_CLI_LANG=ko`)로 한국어
- 리디자인된 자동 갱신 **웹 대시보드** `/dashboard` (`/` 접속 시 자동 리다이렉트)
- 명시적 에러 분류 (auth_expired / rate_limit / model_not_allowed / not_found / network / resource_limit / config / internal)

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
create("gemini").chat("describe", images=["local-image.jpg"])
```

provider 별 처리:
- **Claude** — 이미지용 Read 도구를 허용하고 prompt 앞에 경로를 넣음. `bypassPermissions` 는 자동 설정하지 않음
- **Codex** — native `-i, --image` 플래그 (codex CLI 0.129+ 필요)
- **Gemini (`agy`)** — `@<path>` 참조를 prompt 앞에 삽입. 권한 승인은 기본 유지되고, 위험한 `skip_permissions=True` 를 명시할 때만 건너뜀

직접 Python/CLI 이미지 입력에는 신뢰하는 로컬 path/`Path`, bytes 또는
`Attachment(path=...)`/`Attachment(bytes_=...)`를 사용하세요. 원격 URL과 data URI는
래핑한 CLI에서 의도적으로 거부하므로, 신뢰하는 데이터를 직접 다운로드/디코드한 뒤
전달해야 합니다.

### 대화형 REPL (`unified-cli repl`)

한 프로세스에서 multi-turn + provider 교체. REPL 은 `prompt_toolkit` 기반(코어
의존성이라 `pip install unified-cli` 만으로 동작)이며, 실제 터미널에서는 `/` 를
입력하면 모든 슬래시 명령이 **타이핑하는 즉시 드롭다운**으로 떠서 외울 필요가
없습니다.

```bash
unified-cli repl                          # 설정된 기본 provider (설정 전 Claude)로 시작
unified-cli repl --provider codex -m gpt-5.4-mini
```

```text
[claude/haiku] > /                         # 모든 슬래시 명령 라이브 드롭다운
[claude/haiku] > /model                    # 선택기: provider 별 최신 모델 (기본값 ★)
[claude/sonnet] > /provider                # 선택기: provider 선택 (컨텍스트 자동 주입)
[codex/gpt-5.4-mini] > /status             # 라이브 상태 패널 (Ctrl+C → 프롬프트로 복귀)
[codex/gpt-5.4-mini] > /lang ko            # UI 를 한국어로 전환 (저장됨)
```

- **`/model`** (인자 없이) → provider 별 최신 모델 선택기(기본값 ★). `/model <name>` 도 그대로 동작.
- **`/provider`** (인자 없이) → provider 선택기.
- **`/status`** → REPL 안에서 자동 갱신되는 라이브 상태 패널.
- **`/lang en` / `/lang ko`** → UI 언어 즉시 전환 + 저장.

슬래시 명령:

| 명령 | 동작 |
|---|---|
| `/help` | 명령 목록 (현재 언어로) |
| `/model [name]` | 인자 없으면 모델 선택기, 있으면 같은 provider 에서 모델 변경 |
| `/provider [name]` | 인자 없으면 provider 선택기, 있으면 전환 (이전 8턴 컨텍스트 자동 주입) |
| `/status` | REPL 안 라이브 상태 패널 (Ctrl+C 로 복귀) |
| `/lang <en\|ko>` | UI 언어 전환 + 저장 |
| `/new` | 대화 초기화 |
| `/save` | 현재 session_id + 이어쓰기 명령 표시 |
| `/history [N]` | 최근 N 턴 표시 |
| `/tokens` | 누적 사용량 |
| `/doctor` | provider 헬스 한 줄 |
| `/image <path>` | 다음 prompt 에 이미지 첨부 (반복 가능) |
| `/images` | 첨부 목록 |
| `/clear-images` | 첨부 비우기 |
| `/exit` or Ctrl+D | 종료 (마지막 session_id 자동 저장) |

TTY 가 아니면(파이프 등) 같은 명령을 쓰는 평범한 `input()` 루프로 폴백합니다.
REPL 종료 후 `unified-cli chat "..." --continue` 로도 대화가 이어집니다.

### 언어 설정 (기본 영어, 한국어 선택)

CLI/REPL 전체가 현지화되어 있습니다. 기본은 영어이며, 전역 `--lang` 플래그,
`UNIFIED_CLI_LANG` 환경변수, 또는 REPL 의 `/lang ko` 로 한국어로 전환합니다:

```bash
unified-cli --lang ko chat "안녕"          # 단발 호출, 한국어 출력
export UNIFIED_CLI_LANG=ko                  # 셸 세션 전체 한국어
```

해석 우선순위: `--lang {en,ko}` > `~/.unified-cli/settings.json`(`/lang` 으로
설정) > `$UNIFIED_CLI_LANG` > 영어.

`unified-cli setup` 은 3개 CLI(`claude`/`codex`/`gemini`) 중 빠진 것을 감지해서:
1. 패키지 매니저(brew/npm) 로 설치 명령 제안 → Y/n 동의 후 실행
2. 로그인 안 된 provider 는 `login` 명령 spawn → 브라우저 OAuth 로 유도
3. 각 provider 에 "say hi" 테스트 호출로 최종 검증

중간에 거부하면 수동으로 실행할 명령만 출력하고 넘어갑니다.

### 웹 대시보드

서버 기동 후 브라우저에서 **`http://localhost:8000/dashboard`** 접속하면
(루트 `http://localhost:8000/` 도 자동으로 `/dashboard` 로 리다이렉트):
- 퀵 통계 카드 + provider 별 헬스 카드
- inline-SVG 스파크라인 (지연 / 토큰 볼륨)
- 모델별 사용량 막대
- 누적 사용량 (provider/모델별 호출수, 토큰, 평균 지연)
- 최근 30개 호출 로그
- 활성 대화 목록

5초마다 자동 갱신, 반응형 레이아웃. 외부 의존성 없는 단일 HTML + inline JS.

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

> `gemini` provider 는 **기본 비활성화** 입니다(Antigravity `agy` 자동화는 Google 서비스 이용 제한으로 이어질 수 있음). 적용되는 정책을 확인한 뒤 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정해야 위·아래 `gemini` 예제가 동작합니다.

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

> Gemini provider는 이제 Antigravity `agy` CLI를 래핑합니다. agy는 에이전틱이라 웹서치를 스스로 판단해 수행하며 on/off 토글이 없습니다 (`web_search=`는 사실상 no-op). 단, **기본 비활성화**라 `UNIFIED_CLI_ENABLE_GEMINI=1` 을 설정해야 사용할 수 있습니다(`agy` 자동화 시 서비스 이용 제한 가능성).

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

# --continue 는 유효한 저장 provider/model/작업 디렉토리를 복원합니다.
# 명시한 --cwd 가 항상 우선합니다.
unified-cli chat "이 체크아웃에서 계속" --continue --cwd ~/work/project

# -m/--provider·저장 세션이 없을 때 사용할 기본 provider 설정
unified-cli config default-provider codex
unified-cli config default-provider            # 확인
unified-cli config default-provider --reset    # claude 로 초기화

# 설치된 패키지 버전만 출력 (자동화용)
unified-cli --version
```

## OpenAI 호환 HTTP 서버

```bash
unified-cli serve --port 8000 --open          # ← 권장: localhost 가드 + 대시보드 자동 오픈
# raw ASGI 모드는 Uvicorn의 host 설정을 따르며, 기본은 localhost입니다.
# 외부 mode를 명시하지 않으면 앱이 non-loopback HTTP 요청을 거부합니다.
uvicorn unified_cli.server:app --port 8000
# 브라우저:  http://localhost:8000/dashboard  (리디자인된 라이브 사용량/세션)
#            http://localhost:8000/           (/dashboard 로 리다이렉트)
```

> **기본 localhost 전용.** `unified-cli serve` 및
> `python -m unified_cli.server`는 `127.0.0.1`에 바인딩하고,
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` 없이는 loopback 이 아닌 호스트
> (`0.0.0.0` 등)를 **거부**합니다. raw `uvicorn ... --host 0.0.0.0`은 listener를
> 열 수 있지만, 같은 옵트인 전에는 앱의 ASGI 가드가 non-loopback bind·peer·Host를
> HTTP 403으로 거부합니다. 기동 시 개인용 경고 로그도 출력합니다. 본인 구독을
> 다른 사람이나 네트워크에 노출하면 provider 이용 약관에 맞지 않아 서비스 이용이
> 제한될 수 있으니 로컬에서만 사용하세요.

> **외부 모드는 공개 서비스 모드가 아닙니다.** 독립 관리 배포에서 loopback 밖으로
> 바인딩해야 한다면 `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`과 공백 없는 32 UTF-8
> 바이트 이상의 `UNIFIED_CLI_SERVER_AUTH_TOKEN`을 모두 설정해야 합니다. 모든
> route(진단 포함)에 `Authorization: Bearer <token>`이 필요합니다. TLS reverse
> proxy 뒤의 단일 신뢰 클라이언트에만 쓰세요. Bearer 토큰은 HTTPS나 사용자별 격리를
> 제공하지 않으며 브라우저 대시보드는 로컬 사용용입니다.

> **HTTP 신뢰 경계.** 서버는 기본적으로 Claude 모델만 받습니다. 텍스트 요청은
> Claude safe mode + 도구 없음으로, 이미지 요청은 전달된 이미지 바이트만 읽을 수
> 있는 범위 제한 권한으로 실행합니다. Codex와 Antigravity(`agy`)는 임의 HTTP
> 입력에 대한 기밀 데이터 격리를 보장하지 못하는 에이전틱 CLI라 기본 거부됩니다.
> `UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS=1` 은 의도적으로 좁힌 workspace
> mount를 가진 독립 컨테이너/VM 안에서만 설정하세요. 이 값은 인증 기능도 아니고
> 서버 공개를 안전하게 만드는 기능도 아닙니다.

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

# 모델 목록
curl http://localhost:8000/v1/models
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

# 이미지 입력 (OpenAI multi-content 스키마, Claude 서버 프로필)
r = client.chat.completions.create(
    model="haiku",
    messages=[{"role":"user","content":[
        {"type":"text","text":"describe"},
        {"type":"image_url",
         "image_url":{"url":"data:image/png;base64,iVBOR..."}}
    ]}],
)
```

의도적으로 제한된 외부 모드에서는 OpenAI SDK의 API 키에도 같은 Bearer 토큰을
넣고, 반드시 TLS 뒤에서만 사용하세요.

```python
import os
client = OpenAI(base_url="https://trusted.example/v1",
                api_key=os.environ["UNIFIED_CLI_SERVER_AUTH_TOKEN"])
```

HTTP 이미지의 `image_url.url` 은 MIME과 실제 시그니처가 일치하는 정규 base64
`data:image/png;base64,...`, `data:image/jpeg;base64,...`,
`data:image/gif;base64,...`, `data:image/webp;base64,...` 중 하나만 허용합니다.
원격 URL과 파일시스템 경로는 거부합니다. 기본 한도는 메시지당 4장, 이미지 하나당 디코딩 후 4 MiB,
요청 본문 24 MiB이며 `UNIFIED_CLI_SERVER_MAX_IMAGES`,
`UNIFIED_CLI_SERVER_MAX_IMAGE_BYTES`, `UNIFIED_CLI_SERVER_MAX_BODY_BYTES`로
명시적으로 조정할 수 있습니다.

에러는 OpenAI 스키마로 정규화 매핑:
| UnifiedError.kind | HTTP | OpenAI `type` |
|---|---|---|
| auth_expired | 401 | authentication_error |
| rate_limit | 429 | rate_limit_error |
| model_not_allowed / config | 400 | invalid_request_error |
| not_found | 404 | not_found_error |
| network | 502 | upstream_error |
| resource_limit | 413 | invalid_request_error |
| internal | 500 | internal_error |

## launchd / cron / 서버에서 실행 (헤드리스)

래핑하는 CLI들은 **인터랙티브 실행**을 전제로 만들어졌습니다. 백그라운드 런처
(macOS **launchd**, **cron**, **systemd**, 상시 실행 서버)에서는 두 가지가 문제됩니다.

**1. 최소 `PATH` → "바이너리 없음".** launchd/cron은 빈약한 `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`)로 시작하므로 Homebrew·npm-global·`~/.local/bin`에
설치된 `claude`/`codex`를 못 찾습니다. 이제 표준 설치 위치도 자동 탐색하지만,
확실한 방법은 명시하는 것입니다:

```bash
export CLAUDE_CLI_PATH=/opt/homebrew/bin/claude   # 또는 ~/.local/bin/claude
export CODEX_CLI_PATH=/opt/homebrew/bin/codex
# launchd plist: <key>EnvironmentVariables</key> 아래에 설정.
```

**2. macOS 키체인 → 조용한 hang.** macOS에서 `claude`는 OAuth 자격증명을 **로그인
키체인**에 저장합니다. launchd/데몬 컨텍스트에는 **키체인을 열 TTY가 없어서** CLI가
인증 대기로 영원히 멈춥니다 — 호출이 hang 되다 타임아웃. 터미널에선 되고 서버에서만
죽는 이유입니다. **장기 토큰**(공식 헤드리스 방식)으로 해결하세요:

```bash
claude setup-token                         # 실제 터미널에서 한 번만 실행
# → 나온 토큰을 서비스 환경변수로:
export CLAUDE_CODE_OAUTH_TOKEN=<token>     # OAuth 등가, 종량 과금 아님
# (종량 API 과금을 원하면 대신:  export ANTHROPIC_API_KEY=sk-...)
```

> 기본적으로 래퍼는 **구독 OAuth**로 실행되며, 상속된
> `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`를 자식 환경에서 **제거**합니다 — export된 키
> 때문에 몰래 종량 과금으로 바뀌지 않게 하기 위함입니다. 헤드리스 인증은
> `CLAUDE_CODE_OAUTH_TOKEN`을 쓰고, 종량 과금을 *원할 때만* API 키를 export 하세요.

**배포 전에 증명하세요.** 서비스와 **동일한 컨텍스트**(예: launchd 잡 내부)에서
preflight를 실행하면 provider마다 아주 작은 실제 호출을 해서 거기서 auth가 실제로
되는지(hang이 아닌지) 알려줍니다:

```bash
unified-cli doctor --headless
# ✓ claude: auth OK in this context     → 정상
# ✗ claude: network — ... Keychain ...   → CLAUDE_CODE_OAUTH_TOKEN 설정
```

스트리밍 호출에는 짧은 **first-output 워치독**도 있습니다: provider가 ~60초 안에
아무 출력도 안 내면(전형적인 키체인-hang) 프로세스를 죽이고 키체인 해결책을 안내하는
에러를 반환합니다 — 무한 대기 대신. `codex`는 키체인이 필요 없고
(`~/.codex/auth.json`), `agy`는 브라우저 OAuth를 쓰며 어차피 게이트됩니다.

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
