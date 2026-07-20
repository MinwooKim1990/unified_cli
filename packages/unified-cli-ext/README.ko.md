# unified-cli-ext

`unified-cli-ext`는 [`unified-cli`](https://github.com/MinwooKim1990/unified_cli)를
위한 Stage 2 확장 기반 패키지입니다. 0.1.0은 전송 계층과 계약(contract) 구성
요소만 제공합니다. 실제로 동작하는 공급자 어댑터는 포함하지 않습니다.

## 이 릴리스의 범위

이 패키지는 향후 확장 작성자를 위한 기반입니다. 확장 식별자는 Core에서 명시적으로
요청되고 필요할 때만 지연 로드됩니다. 설치해도 Core의 기본 동작, 내장 공급자 세 개
(Claude, Codex, Gemini/Antigravity), 로컬 서버 허용 목록은 바뀌지 않습니다. 확장
공급자의 서버 노출도 계속 꺼져 있습니다.

0.1.0에는 자격 증명, 인증 흐름, 실제 공급자 호출, 유료 서비스 호출이 없습니다.
검증도 가짜/오프라인 방식만 사용합니다. 인증이나 속도 제한을 우회하지 않으며,
자격 증명을 스크래핑·수집·복구하지 않습니다.

확장은 로드될 때 호스트 Python 프로세스 안에서 신뢰된 설치 코드로 실행됩니다.
따라서 신뢰할 수 있는 배포판만 설치하세요.

## 요구 사항 및 설치

이 배포판은 `unified-cli` 0.5.x를 대상으로 합니다. 호환되는 Core와 함께 설치하세요.

```bash
python -m pip install "unified-cli~=0.5.0" unified-cli-ext
```

가져오기(import) 패키지 이름은 `unified_cli_ext`입니다. 이 초기 릴리스에는 실제
어댑터가 없으므로 공급자 명령, 어댑터 설정, 인증 절차는 문서화하지 않습니다.

## 선택적 프로토콜 의존성

프로토콜 SDK는 선택 사항입니다. 기반 패키지 설치에는 필요하지 않으며 0.1.0에서
공급자 호출에 사용되지 않습니다. 제공되는 extra는 `acp`, `mcp`, 두 프로토콜 SDK를
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

## 범위와 보안 경계

공급자 검색과 정책은 Core가 소유합니다. 향후 공급자는 명시적으로 요청해야 하며,
접두사 없는 모델 이름 추론으로 선택되지 않습니다. 이 ABI 단계에서 Core HTTP 서버는
확장이 서버 관련 메타데이터를 선언해도 확장 공급자를 계속 거부합니다.

Core 확장 ABI와 신뢰 경계는
[provider plugin ABI](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/development/provider-plugin-abi-v1.md)를
참고하세요.

## 상태

이 릴리스는 기반 패키지이며 지원되는 외부 공급자 목록이 아닙니다. 공급자 어댑터,
실제 프로토콜 세션, 인증, 네트워크 기반 검증은 0.1.0의 의도적인 범위 밖입니다.
