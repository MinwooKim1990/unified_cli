# 확장

`unified-cli-ext`는 Core(`unified-cli`)와 별도입니다. Core는 기존 기본값으로
Claude, Codex, Gemini(`agy`)를 계속 지원합니다. Ext 설치는 이 기본값을 바꾸지
않고, Core의 로컬 서버 허용 목록에 확장을 추가하지 않으며, vendor 소프트웨어를
설치하거나 설정하지 않습니다.

Stage 5B–5E는 정확히 16개의 비활성 Held provider 카탈로그 메타데이터를 설치합니다.
각 항목은 명시적 엔트리포인트로 발견할 수 있지만 상태는 **Held**이며, provider나 외부 명령을
시작할 수 없습니다. 어댑터 카탈로그의 `chat`은 잠정 설계 목표이고 Core 플러그인은
실행 capability를 하나도 표시하지 않습니다. 이 항목들을 위한 지원되는 provider 채팅
명령은 아직 없습니다.

vendor 바이너리, 계정, 구독, 업데이트는 모두 사용자가 소유하고 관리합니다. Ext 설치만으로
vendor CLI 설치, 로그인, 서비스 호출, 과금 발생이 일어나지 않습니다. Ext는 아래 vendor와
제휴 관계가 아닙니다.

## 설치 및 확인

```bash
python -m pip install unified-cli-ext
python -c "import importlib.metadata as m; print([e.name for e in m.distribution('unified-cli-ext').entry_points if e.group == 'unified_cli.providers.v1'])"
```

두 번째 명령은 설치된 엔트리포인트 이름만 표시합니다. vendor 설치, 인증 상태, 서비스
가용성을 확인하지 않습니다.

Core는 import 없이 엔트리포인트를 탐색합니다. 따라서 `unified-cli providers
--include-ext`에서 새 항목은 수명 주기 `discovered`, 지원 상태 `unknown`으로 표시됩니다.
명시적인 provider 요청이 있을 때만 해당 엔트리포인트 하나를 로드하며, Core는 지원 상태
`held`를 확인한 뒤 provider callback 전에 중단합니다.

## 로컬 설치 기록

Ext는 명시적으로 선택한 로컬 실행 파일이나 npm launcher의 파일 식별 정보와 메타데이터를
기록하고 나중에 다시 확인할 수 있습니다. 이 기록은 로컬 파일을 설명하며 게시 주체를
증명하거나 vendor 공식 배포 경로 확인을 대신하지 않습니다. 같은 파일시스템 접근 권한을
가진 다른 프로세스가 확인 사이에 경로를 바꿀 수 있으므로 기록 생성과 재확인은 실행 시점에
가깝게 수행해야 합니다.

## 상태 용어

| 상태 | 의미 |
|---|---|
| Stable | 문서화된 호환성 근거가 있는 출시·지원 통합입니다. |
| Preview | 제한 사항을 문서화하며 평가 중인 활성화된 통합입니다. |
| Experimental | 동작이 바뀔 수 있는 제한 범위의 활성화된 통합입니다. |
| Held | 발견 가능한 메타데이터일 뿐입니다. provider 생성, 바이너리 탐색, 명령 실행 전에 차단됩니다. |

아래 카탈로그의 모든 항목에는 **Held**만 적용됩니다.

## 생성된 provider 지원 상태

아래 기계 상태 표는 명시적인 Ext 엔트리포인트 플러그인에서 생성됩니다. 뒤따르는
상세 후보 전송 방식 카탈로그는 수동 설계 기록으로 유지됩니다.

<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->
| Provider ID | 지원 상태 | Core capability | 서버 |
|---|---|---|---|
| `cline` | `held` | `none` | `disabled` |
| `codebuddy` | `held` | `none` | `disabled` |
| `copilot` | `held` | `none` | `disabled` |
| `cursor` | `held` | `none` | `disabled` |
| `droid` | `held` | `none` | `disabled` |
| `grok` | `held` | `none` | `disabled` |
| `hermes` | `held` | `none` | `disabled` |
| `kilo` | `held` | `none` | `disabled` |
| `kimi` | `held` | `none` | `disabled` |
| `mistral-vibe` | `held` | `none` | `disabled` |
| `oh-my-pi` | `held` | `none` | `disabled` |
| `opencode` | `held` | `none` | `disabled` |
| `pi` | `held` | `none` | `disabled` |
| `poolside` | `held` | `none` | `disabled` |
| `qoder` | `held` | `none` | `disabled` |
| `qwen` | `held` | `none` | `disabled` |
<!-- END GENERATED EXT PROVIDER SUPPORT -->

## Stage 5B–5E 카탈로그

“후보 전송 방식”은 잠정적인 설계 방향이며 명령 계약이 아닙니다. “자동 업데이트 차단”은
나중에 어댑터가 활성화될 경우의 의도된 경계를 설명합니다. Held 메타데이터는 현재 어느
쪽도 실행하지 않습니다.

| Provider ID | 공식 바이너리/패키지 | 후보 전송 방식 | 잠정 어댑터 목표 | 상태 | 자동 업데이트 차단 | 공식 문서 |
|---|---|---|---|---|---|---|
| `grok` | Grok CLI (`grok`) | JSONL | `chat` 후보; Core capability 없음 | Held | 후보 `--no-auto-update`; 사용 전 검증 필요 | [개요](https://docs.x.ai/build/overview) · [CLI reference](https://docs.x.ai/build/cli/reference) · [Headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| `kimi` | Kimi Code CLI (`kimi`), 레거시 Python `kimi-cli`가 아닌 현재 후속 제품 | JSONL | `chat` 후보; Core capability 없음 | Held | 후보 opt-in `KIMI_CODE_NO_AUTO_UPDATE`; 사용 전 검증 필요 | [시작하기](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html) · [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html) |
| `copilot` | GitHub Copilot CLI (`copilot`, `@github/copilot`) | 일반 텍스트 | `chat` 후보; Core capability 없음 | Held | 후보 `--no-auto-update`; 사용 전 검증 필요 | [설치](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) · [CLI 명령 reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [ACP 서버](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) |
| `cursor` | Cursor Agent CLI (`cursor-agent`) | JSON | `chat` 후보; Core capability 없음 | Held | 아직 차단 방식 주장 없음; 사용 전 업데이트 동작 검증 필요 | [설치](https://cursor.com/docs/cli/installation) · [파라미터](https://cursor.com/docs/cli/reference/parameters) · [출력 형식](https://cursor.com/docs/cli/reference/output-format) · [ACP](https://cursor.com/docs/cli/acp) |
| `codebuddy` | CodeBuddy Code (`codebuddy`, `@tencent-ai/codebuddy-code`) | JSONL 프로토콜 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `DISABLE_AUTOUPDATER=1`; 정확한 프레임과 설정 격리 검증 필요 | [CLI reference](https://www.codebuddy.ai/docs/cli/cli-reference) · [Headless mode](https://www.codebuddy.ai/docs/cli/headless) · [ACP](https://www.codebuddy.ai/docs/cli/acp) |
| `qoder` | Qoder CLI (`qodercli`, `@qoder-ai/qodercli`) | ACP stdio 후보 | `chat` 후보; Core capability 없음 | Held | 후보 전용 설정 `general.enableAutoUpdate=false`; ACP 수명 주기 검증 필요 | [빠른 시작](https://docs.qoder.com/en/cli/quick-start) · [ACP](https://docs.qoder.com/en/cli/acp) · [권한](https://docs.qoder.com/en/cli/permissions) |
| `mistral-vibe` | Mistral Vibe (`vibe`, `mistral-vibe`) | JSONL 메시지 스트림 후보 | `chat` 후보; Core capability 없음 | Held | 업데이트 확인을 끈 전용 설정 후보; direct와 `vibe-acp` 경로를 따로 검증해야 함 | [설치](https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli) · [CLI 사용](https://docs.mistral.ai/vibe/code/cli/work-with-cli) · [ACP surface](https://docs.mistral.ai/vibe/code/choose-cli-vscode-web-sessions) |
| `qwen` | Qwen Code (`qwen`, `@qwen-code/qwen-code`) | JSONL 후보 | `chat` 후보; Core capability 없음 | Held | backend 선택, 자격 정보, 업데이트 동작, event schema 검증 필요 | [저장소](https://github.com/QwenLM/qwen-code) · [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/) · [인증](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) |
| `cline` | Cline CLI (`cline`) | JSONL 후보; ACP는 별도 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `CLINE_NO_AUTO_UPDATE=1`; stdin EOF, event schema, 로컬 설정 격리 검증 필요 | [CLI 개요](https://docs.cline.bot/usage/cli-overview) · [CLI reference](https://docs.cline.bot/cli/cli-reference) · [릴리스 소스](https://github.com/cline/cline/tree/cli-v3.0.46/apps/cli) |
| `opencode` | OpenCode (`opencode`, 패키지 `opencode-ai`) | `JSONL one-shot` 후보 | `chat` 후보; Core capability 없음 | Held | 후보 비활성화 제어는 검증이 필요하며 stdin EOF, config/MCP 격리, 프로세스/세션 수명 주기는 Stage 6 관문으로 남음 | [문서](https://opencode.ai/docs/) · [CLI](https://opencode.ai/docs/cli/) · [서버](https://opencode.ai/docs/server/) |
| `kilo` | Kilo Code (`kilo`, 패키지 `@kilocode/cli`) | `내부 loopback 서버가 있는 ACP stdio` 후보 | `chat` 후보; Core capability 없음 | Held | 검증된 자동 업데이트 차단 방식 주장은 아직 없음; ACP loopback/프로세스/config/권한 수명 주기는 Stage 6 검증 필요 | [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli) · [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference) · [릴리스](https://github.com/Kilo-Org/kilocode/releases/tag/v7.4.11) |
| `droid` | Factory Droid (`droid`, npm 패키지 `droid`) | vendor stream JSON-RPC 후보 | `chat` 후보; Core capability 없음 | Held | 후보 업데이트 제어, 프로토콜 envelope, 권한 흐름, 프로세스 수명 주기는 Stage 6 검증 필요 | [CLI reference](https://docs.factory.ai/reference/cli-reference) · [Droid Exec](https://docs.factory.ai/cli/droid-exec/overview) · [패키지 메타데이터](https://registry.npmjs.org/droid/latest) |
| `pi` | Pi Coding Agent (`pi`, 패키지 `@earendil-works/pi-coding-agent`) | 전용 NDJSON RPC 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `--offline`과 resource 비활성 플래그는 Stage 6 검증 필요; JSON-RPC는 아님 | [패키지](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json) · [README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) · [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) |
| `oh-my-pi` | Oh My Pi (`omp`, 패키지 `@oh-my-pi/pi-coding-agent`) | 전용 NDJSON RPC 후보 | `chat` 후보; Core capability 없음 | Held | 검증된 업데이트 차단 방식 주장은 아직 없음; 설정, resource, 권한, 프로세스 수명 주기는 Stage 6 검증 필요 | [저장소](https://github.com/can1357/oh-my-pi) · [RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md) · [Approval mode](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) |
| `hermes` | Hermes Agent (`hermes`, PyPI `hermes-agent[acp]`) | ACP stdio 후보 | `chat` 후보; Core capability 없음 | Held | Hermes는 ACP 0.9.0을 고정하지만 Ext는 0.11.x를 대상으로 함; 호환성, 설정, 수명 주기는 Stage 6 검증 필요 | [PyPI](https://pypi.org/project/hermes-agent/) · [저장소](https://github.com/NousResearch/hermes-agent) · [ACP 가이드](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md) |
| `poolside` | Poolside Agent CLI (`pool`, 공식 native 릴리스) | ACP stdio 후보 | `chat` 후보; Core capability 없음 | Held | 검증된 시작/업데이트 차단 방식 주장은 아직 없음; proprietary 바이너리 식별, ACP schema, 설정, 제거는 Stage 6 검증 필요 | [설치](https://docs.poolside.ai/cli/install) · [CLI reference](https://docs.poolside.ai/cli/cli-reference) · [릴리스](https://github.com/poolsideai/pool/releases/tag/v1.0.13) |

선택적 `acp`, `mcp` extra는 프로토콜 SDK 의존성만 설치합니다. Held provider를
활성화하거나 provider 호출을 만들지 않습니다.

## 활성화된 통합으로 승격하기 위한 조건

향후 Stage 6 승격은 provider와 버전별로 격리된 환경에서 평가합니다. 상태를 바꾸기
전에는 프로젝트가 다음 항목에 대한 반복 가능한 기록 근거를 갖춰야 합니다.

- 정확한 vendor CLI 설치 출처와 버전
- 관찰된 인증 상태와 사용자에게 보이는 동작
- 지원하는 입출력 형식을 정하는 프롬프트·출력 fixture
- 중단된 작업 뒤에 남는 항목을 포함한 취소·정리 동작
- 문서화된 호출에서의 권한 동작
- 세션의 시작·이어서 사용·종료 방식을 포함한 세션 의미

이 근거는 호환성 관문일 뿐 특정 provider의 승격을 약속하지 않습니다. 완료되고
검토될 때까지 항목은 Held로 남으며 실행할 수 없습니다.

## 신뢰 및 소유 경계

확장은 로드될 때 호스트 Python 프로세스에서 신뢰된 코드로 실행됩니다. 신뢰할 수 있는
배포판만 설치하세요. provider 탐색과 정책은 Core가 소유합니다. 확장 provider는
명시적으로 요청해야 하며 접두사 없는 모델 이름 추론으로 선택되지 않습니다. 이 단계에서
Core HTTP 서버는 확장 provider를 계속 거부합니다.

Core 확장 ABI와 신뢰 경계는 [provider plugin ABI](development/provider-plugin-abi-v1.md)를
참고하세요.
