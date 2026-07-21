# unified-cli-ext

`unified-cli-ext`는 [`unified-cli`](https://github.com/MinwooKim1990/unified_cli)를
위한 확장 기반 패키지입니다. 0.1.0은 전송·런타임 계약(contract)과 비활성 Stage 5B–5E
카탈로그를 제공합니다. 동작하거나 활성화된 provider 어댑터는 포함하지 않습니다.

## 이 릴리스의 범위

이 패키지는 확장 작성자를 위한 기반입니다. provider 식별자는 Core에서 명시적으로
요청되고 필요할 때만 지연 로드됩니다. 설치해도 Core의 기본 동작, 내장 provider 세 개
(Claude, Codex, Gemini/Antigravity), 로컬 서버 허용 목록은 바뀌지 않습니다. 확장
provider의 서버 노출도 계속 꺼져 있습니다.

설치되는 카탈로그에는 Grok, Kimi, Copilot, Cursor, CodeBuddy, Qoder, Mistral Vibe,
Qwen, Cline, OpenCode, Kilo Code, Factory Droid, Pi, Oh My Pi, Hermes Agent,
Poolside Agent CLI용 엔트리포인트 메타데이터가 있습니다. 정확히 16개의
비활성 항목은 모두 **Held**입니다. 어댑터
메타데이터의 `chat`은 잠정 목표일 뿐이며
Core 플러그인은 실행 capability를 하나도 표시하지 않고 provider를 만들거나 명령을
실행할 수 없습니다. 이는 동작하는 어댑터가 아니며 Preview나 Stable로 표현해서는 안
됩니다.

0.1.0에는 자격 증명, 인증 흐름, 실제 provider 호출, 유료 서비스 호출이 없습니다.
설치만으로 vendor CLI가 설치되거나 로그인·서비스 호출·과금이 발생하지 않습니다.
provider 바이너리와 계정은 사용자가 직접 관리합니다. 검증은 오프라인 fixture만
사용합니다. 활성화된 provider가 없으므로 이 릴리스는 vendor 로그인이나 요청 처리에
관여하지 않으며 계정 데이터를 읽거나 가져오지 않습니다.

확장은 로드될 때 호스트 Python 프로세스 안에서 신뢰된 설치 코드로 실행됩니다.
따라서 신뢰할 수 있는 배포판만 설치하세요.

로컬 설치 기록 API는 명시적으로 선택한 실행 파일이나 npm launcher를 관찰된 파일 식별
정보와 메타데이터에 연결합니다. 게시 주체를 증명하는 기능은 아니므로 vendor 공식 배포
경로를 계속 사용하고 실행 직전에 기록을 재확인해야 합니다.

## 요구 사항 및 설치

이 배포판은 `unified-cli` 0.5.x를 대상으로 합니다. 호환되는 Core와 함께 설치하세요.

```bash
python -m pip install "unified-cli~=0.5.0" unified-cli-ext
```

가져오기(import) 패키지 이름은 `unified_cli_ext`입니다. 활성화된 실제 provider가
없으므로 provider 채팅 명령, 어댑터 설정, 인증 절차는 문서화하지 않습니다. Held
카탈로그와 공식 vendor 문서는 루트의 [확장 가이드](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.ko.md)를
참고하세요.

## 선택적 프로토콜 의존성

프로토콜 SDK는 계속 선택 사항입니다. 기반 패키지 설치에는 필요하지 않으며 0.1.0에서
provider 호출을 활성화하지 않습니다. 제공되는 extra는 `acp`, `mcp`, 두 프로토콜 SDK를
함께 설치하는 `all`, 테스트 의존성용 `dev`입니다.

```bash
python -m pip install "unified-cli-ext[acp]"
python -m pip install "unified-cli-ext[mcp]"
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

## 상태

이 릴리스는 비활성 Held 카탈로그가 있는 기반 패키지이지 지원되는 외부 provider
목록이 아닙니다. 전송/런타임 기반은 포함하지만, 활성 provider 어댑터와 provider별
실검증 세션, 인증, 네트워크 기반 검증은 0.1.0의 의도적인 범위 밖입니다.
