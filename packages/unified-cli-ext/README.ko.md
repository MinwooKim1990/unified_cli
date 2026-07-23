# 확장 소스 트리

이 디렉터리는 [`unified-cli`](https://github.com/MinwooKim1990/unified_cli)에 포함되는
확장 소스를 정리한 곳입니다. 0.5.2 릴리스에서는 독립적으로 build하거나 install할 수
없으며, 하나의 `unified-cli` wheel이 `unified_cli`와 `unified_cli_ext`를 모두 제공합니다.
확장 기능은 전송·런타임 계약(contract)과 실행 가능한 Preview 어댑터 18개를 제공합니다.

## 이 릴리스의 범위

이 패키지는 확장 작성자를 위한 기반입니다. provider 식별자는 Core에서 명시적으로
요청되고 필요할 때만 지연 로드됩니다. 설치해도 Core의 기본 동작, 내장 provider 세 개
(Claude, Codex, Gemini/Antigravity), 로컬 서버 허용 목록은 바뀌지 않습니다. 확장
provider의 서버 노출도 계속 꺼져 있습니다.

설치되는 카탈로그에는 Grok, Kimi, Copilot, Cursor, CodeBuddy, Qoder, Mistral Vibe,
Qwen, Cline, OpenCode, Kilo Code, Factory Droid, Pi, Oh My Pi, Hermes Agent,
Poolside Agent CLI, Amp, GitLab Duo CLI용 엔트리포인트 메타데이터가 있습니다. Grok은
`chat`, `stream`, `sessions` capability를 제공하는 읽기 도구 제한 **Preview**입니다.
나머지 모든 카탈로그 항목도 명시적으로 선택하면 실행 가능한 **Preview** adapter입니다.
공통 transport는 fixture로 검증했지만 vendor CLI·계정 호환성을 보장하지 않습니다. 모든
확장 서버 정책은 비활성입니다.

Grok Build, Kimi Code CLI, GitHub Copilot CLI, Cursor Agent CLI에 대해 카탈로그는
공식 출처 링크, 고정된 향후 lab 목표, 문서 기반 명령 후보, 남은 Stage 6 근거 관문을
기록합니다. Grok 어댑터는 문서화된
공식 CLI 형태만 허용하고 관련 없는 `@vibe-kit/grok-cli` 형태를 거부하며, 자동 업데이트,
web, plan, subagent, memory를 끄고 `read_file`, `grep`, `list_dir`만 노출합니다. 오프라인
fixture로 정확한 argv, stream/session 정규화, 잘못된 출력, 취소, 출력 제한을 검증했습니다.
또한 공식 native `0.2.111`의 대표 격리 인증 smoke가 macOS arm64에서 통과했습니다. 이는
명시적인 exact setup을 사용한 단일 version/platform/auth 표본일 뿐이므로 Grok은 Stable이
아닌 Preview이며 서버 모드는 비활성입니다.
Kimi `-p`는 일반 도구를 자동 승인하고, Copilot은 로컬 출처 캡처가 더 필요하며, Cursor는
위치 인자 프롬프트와 설정 경계를 검증해야 합니다. Grok을 제외한 모든 Preview는 vendor
CLI·계정 조합을 보장하는 대신 공통 transport fixture로 검증합니다.

통합 0.5.2 릴리스에는 자격 증명, 로그인 흐름, 유료 서비스 호출이 포함되지 않습니다.
설치만으로 vendor CLI가 설치되거나 로그인·서비스 호출·과금이 발생하지 않습니다.
provider 바이너리와 계정은 사용자가 직접 관리합니다. 자동화된 테스트는 오프라인 fixture를
사용하고, Grok에는 위의 대표 인증 native smoke 근거도 있습니다. Grok 호출은 사용자가 exact
setup을 완료하고 `grok`을 명시적으로 선택한 경우에만 발생하며 수동적인 provider 탐색은
probe나 provider 호출을 하지 않습니다.

확장은 로드될 때 호스트 Python 프로세스 안에서 신뢰된 설치 코드로 실행됩니다.
따라서 신뢰할 수 있는 배포판만 설치하세요.

로컬 설치 기록 API는 명시적으로 선택한 실행 파일이나 npm launcher를 관찰된 파일 식별
정보와 메타데이터에 연결합니다. 게시 주체를 증명하는 기능은 아니므로 vendor 공식 배포
경로를 계속 사용하고 실행 직전에 기록을 재확인해야 합니다.

## 요구 사항 및 설치

계획된 통합 릴리스를 설치하세요. Core와 확장은 두 배포판이 아니라 기능 경계입니다.

```bash
python -m pip install "unified-cli==0.5.2"
```

개발자나 테스터가 레거시 로컬 wheel 또는 실패한 분리 wheel을 설치했다면 먼저
`python -m pip uninstall -y unified-cli-ext`를 실행한 후
`python -m pip install --force-reinstall "unified-cli==0.5.2"`를 실행하세요. 공개 PyPI에는
별도 프로젝트가 게시된 적이 없습니다.

가져오기(import) 패키지 이름은 `unified_cli_ext`입니다. Grok을 선택하기 전에 루트
[확장 가이드](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.ko.md)의
공식 native 바이너리 snapshot, 격리 로그인, `configure_extension_provider(...)` 등록을
완료해야 합니다. 이후 Preview를 명시적으로 선택할 수 있습니다.

```bash
unified-cli chat "이 프로젝트를 설명해줘" --provider grok --model grok-4.5
```

0.1 Preview 설정은 `https://x.ai/cli/install.sh`의 native 설치 구조를 사용합니다.
`@xai-official/grok`은 공식 vendor 대안이지만 이 설정 절차로는 등록하지 않으며,
`@vibe-kit/grok-cli`는 거부합니다. Grok은 정확히 `0.2.111`만 허용하며 검토하지 않은 patch나
minor version은 fail closed합니다. 인증은 일반 host 로그인을 재사용한다고 가정하지 않는
격리된 provider `HOME`을 사용하며 로그인 전에 정확한 private (`0600`) safe config
template이 필요합니다. config가 없거나 다르면 거부합니다. 고정 실행 경계는 auto-update,
write, tool search, LSP, plan, subagent, memory, web, managed MCP, 공식 marketplace 자동
등록과 Claude/Cursor/Codex skills, rules, agents, MCP, hooks, sessions를 끄고 marketplace
package에는 SHA를 요구하며 탐색은 gitignore를 존중해야 합니다. strict sandbox와
`dontAsk`를 사용하며 `read_file`, `grep`, `list_dir`만 허용합니다.

어댑터는 선택한 작업 디렉터리의 파일을 읽을 수 있고 vendor CLI가 자체 계정·설정 파일을
관리할 수 있습니다. 신뢰하는 workspace에서만 사용하세요. 이 통제는 defense in depth이며
완전한 secret boundary가 아닙니다. gitignore는 읽을 수 있는 파일을 vendor 프로세스로부터
비밀로 만들지 않습니다. 전체 Preview 카탈로그와 공식 vendor 문서는 루트의
[확장 가이드](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.ko.md)를
참고하세요.

## 선택적 프로토콜 의존성

프로토콜 SDK는 계속 선택 사항이며 `unified-cli` 0.5.2의 Core 또는 Grok 사용에는
필요하지 않습니다. ACP 기반 Preview 연동을 명시적으로 사용할 때는 `acp` extra가
필요하지만, 이를 설치하는 것만으로 provider가 선택되거나 실행되지는 않습니다. 제공되는
extra는 `acp`, `mcp`, 두 프로토콜 SDK를 함께 설치하는 `all`,
테스트 의존성용 `dev`입니다.

```bash
python -m pip install "unified-cli[acp]"
python -m pip install "unified-cli[mcp]"
```

- ACP는 공식 Python 패키지
  [`agent-client-protocol`](https://github.com/agentclientprotocol/python-sdk)을
  사용하며 `>=0.11,<0.12`로 제한합니다. 현재 0.11.0은 Python 3.10 이상이
  필요하며, 이 extra는 Python 3.10–3.14용으로 선언되어 있습니다.
- MCP는 공식 안정 v1 Python SDK
  [`mcp`](https://github.com/modelcontextprotocol/python-sdk)를 대상으로 합니다.
  v2 호환성을 검토하는 동안 `mcp>=1.27,<2`로 제한합니다. 이 extra는 Python
  3.10 이상이 필요합니다.

## 실행 범위

공급자 검색과 정책은 Core가 소유합니다. 향후 공급자는 명시적으로 요청해야 하며,
접두사 없는 모델 이름 추론으로 선택되지 않습니다. 이 ABI 단계에서 Core HTTP 서버는
확장이 서버 관련 메타데이터를 선언해도 확장 공급자를 계속 거부합니다.

Core 확장 ABI와 신뢰 경계는
[provider plugin ABI](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/development/provider-plugin-abi-v1.md)를
참고하세요.

### 명시적 probe 캐시

`ProviderAdapterV1`의 캐시는 처음에는 비어 있으며 호출자가 `inspect`,
`authenticated`, `list_models`를 명시적으로 호출할 때만 채워집니다. 성공한 불변
레코드는 monotonic 시계와 서로 다른 제한 TTL을 사용합니다. inspection은 5분,
인증 상태는 15초, 비어 있지 않은 모델 목록은 1분입니다. 캐시 hit에서도 먼저 저렴한
`BinaryProvenance` 메타데이터 검증을 다시 수행하며, 정확한 실행 파일이 교체되면 해당
probe 레코드를 무효화합니다. 계정에 민감한 키에는 검증된 provider home 디렉터리의
식별 정보와 어댑터가 선택한 환경 값의 digest도 포함됩니다. 원문 secret은 캐시 키나
값으로 보관하지 않습니다.

현재 API에는 provider가 제공하는 account identifier가 없으므로 이 캐시는
home/environment 경계를 넘어 계정을 식별한다고 주장하지 않습니다. 외부 프로세스가
계정을 바꾸면 짧은 auth TTL 동안 이전 상태가 보일 수 있습니다. 어댑터의 login/logout
준비 경로는 해당 컨텍스트의 auth/model 레코드를 무효화합니다. 세 probe 메서드에서
`force_refresh=True`를 지정하거나 `invalidate_cache()`를 호출해 명시적으로 갱신할 수
있습니다. 예외, 취소, permission 실패는 성공 캐시 레코드로 남지 않으며 이 기능 때문에
시작 시 plugin import나 provider probe가 실행되지 않습니다.

## 상태

이 릴리스에는 실행 가능한 Preview 통합 18개가 있습니다. Grok은 대표 인증 native smoke를
통과했고 나머지 공통 transport는 fixture로 검증했지만 vendor·계정 호환성을 보장하지
않습니다. Ext provider는 Core 로컬 서버에 노출되지 않습니다. Preview 실행 실패 시
`~/.unified-cli/preview-diagnostics/`에 prompt 없는 보고서가 생성되며 GitHub Issue에
첨부해 호환성 개선을 도울 수 있습니다.
