# 확장

확장은 계획된 `unified-cli` 0.5.1 배포판에 Core와 함께 들어 있습니다. 하나의 wheel에
`unified_cli`와 `unified_cli_ext` 공개 namespace가 모두 포함됩니다. Core는 기존 기본값으로
Claude, Codex, Gemini(`agy`)만 계속 지원합니다. 확장은 패키지 경계가 아니라 기능 경계이므로,
사용해도 이 기본값을 바꾸지 않고 Core의 로컬 서버 허용 목록에 확장을 추가하지 않으며 vendor
소프트웨어를 설치하거나 설정하지 않습니다.

함께 제공되는 확장은 명시적인 provider 엔트리포인트 18개를 제공합니다. Grok Build는 오프라인 fixture로
검증한 읽기 도구 제한 **Preview**입니다. Qoder, Kilo, Poolside는 실행 가능한
**Experimental** 통합이며, 나머지 14개는 **Held**로 provider 생성, 바이너리 탐색,
명령 실행 전에 중단됩니다. Core 서버 모드에서 활성화된 Ext provider는 없습니다.

vendor 바이너리, 계정, 구독, 업데이트는 모두 사용자가 소유하고 관리합니다. Ext 설치만으로
vendor CLI 설치, 로그인, 서비스 호출, 과금 발생이 일어나지 않습니다. Ext는 아래 vendor와
제휴 관계가 아닙니다.

## 설치 및 확인

```bash
python -m pip install "unified-cli==0.5.1"
python -c "import unified_cli_ext; print(unified_cli_ext.__name__)"
unified-cli providers --include-ext
```

Python 명령은 함께 설치된 확장 namespace의 import만 확인하고, `providers` 명령은 설치된
엔트리포인트 메타데이터를 표시합니다. 어느 명령도 vendor 설치, 인증 상태, 서비스
가용성을 확인하지 않습니다.

개발자나 테스터가 레거시 로컬 wheel 또는 실패한 분리 wheel을 설치했다면, 계획된 통합
릴리스를 설치하기 전에 다음과 같이 복구하세요.

```bash
python -m pip uninstall -y unified-cli-ext
python -m pip install --force-reinstall "unified-cli==0.5.1"
```

Core는 import 없이 엔트리포인트를 탐색합니다. 따라서 `unified-cli providers
--include-ext`에서 새 항목은 수명 주기 `discovered`, 지원 상태 `unknown`으로 표시됩니다.
명시적인 provider 요청이 있을 때만 해당 엔트리포인트 하나를 로드합니다. Held provider는
callback 전에 중단됩니다. Grok은 명시적으로 선택한 로컬 바이너리가 정확한 `0.2.111`
버전과 제한된 기능 probe를 통과해야만 계속 실행됩니다.
Qoder, Kilo, Poolside는 실험적 명시 요청 통합이며 서버 모드는 계속 비활성입니다.

## Grok Preview 설정 및 경계

Grok Build Preview는 실행 가능한 Ext provider 중 하나입니다. 기본 공식 native 설치 경로는
`https://x.ai/cli/install.sh`입니다. 공식 npm 패키지 `@xai-official/grok`도 vendor 대안이지만
이 0.1 Preview 설정 절차는 native 설치 구조만 사용합니다.
알려진 관련 없는 `@vibe-kit/grok-cli` CLI 형태는 거부합니다. 검토한 버전은 정확히
`0.2.111`뿐이며 다른 버전은 fail closed합니다. 검증된 플랫폼은 macOS arm64뿐입니다.
vendor 설치 프로그램을 일반 사용자 home에서 실행한 뒤 아래 fail-closed 절차를
사용합니다. 이 절차는 native 설치 프로그램의 소유자 일치·비쓰기 가능 `~/.grok`
구조만 허용하고, `bin/grok` symlink가 `downloads` 안의 single-link regular file을
직접 가리키는지 확인하며, 검토한 정확한 SHA-256을 검증합니다. 그런 다음 새
version/platform-qualified private snapshot에 복사하고 로그인 전에 정확한 safe config도
생성합니다. 지원하지 않는 OS/architecture, 경로 형태, 소유자, link type/count, mode,
digest, version 출력은 모두 거부합니다.

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
python -I - <<'PY'
import hashlib
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path
from unified_cli_ext.providers.grok import GROK_FIXED_ENVIRONMENT, GROK_SAFE_CONFIG

DIGEST = "e1fafdfffe14f339460befaf194360e8f90bfd02efe8a4f24cfa1c7aea657ffe"
VERSION = b"0.2.111"
SNAPSHOT = "native-0.2.111-darwin-arm64"
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)

if sys.platform != "darwin" or platform.machine().lower() != "arm64":
    raise SystemExit("verified only on macOS arm64")
if not NOFOLLOW or not DIRECTORY:
    raise SystemExit("required no-follow operations are unavailable")

def directory(parent, name, *, create=False, private=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    meta = os.stat(name, dir_fd=parent, follow_symlinks=False)
    mode = stat.S_IMODE(meta.st_mode)
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or mode & 0o022
        or (private and mode != 0o700)
    ):
        raise SystemExit(f"refusing unsafe directory {name!r}")
    return os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=parent)

def sha256(fd):
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        value.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return value.hexdigest()

def environment(home, tmp):
    # GROK_FIXED_ENVIRONMENT includes GROK_MANAGED_MCPS_ENABLED=false,
    # GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED=false, and GROK_RESPECT_GITIGNORE=1.
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TMPDIR": str(tmp),
        **dict(GROK_FIXED_ENVIRONMENT),
    }

def require_version(binary, cwd, env):
    result = subprocess.run(
        [str(binary), "--version"],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    match = re.fullmatch(
        rb"grok ([0-9]+\.[0-9]+\.[0-9]+)(?: [^\r\n]*)?\r?\n?",
        result.stdout,
    )
    if (
        result.returncode != 0
        or len(result.stdout) > 4096
        or match is None
        or match.group(1) != VERSION
    ):
        raise SystemExit("refusing unexpected Grok version output")

home_path = Path.home()
home_fd = os.open(home_path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
home_meta = os.fstat(home_fd)
if (
    not stat.S_ISDIR(home_meta.st_mode)
    or home_meta.st_uid != os.getuid()
    or stat.S_IMODE(home_meta.st_mode) & 0o022
):
    raise SystemExit("refusing unsafe home directory")

vendor_fd = directory(home_fd, ".grok", private=False)
vendor_bin_fd = directory(vendor_fd, "bin", private=False)
downloads_fd = directory(vendor_fd, "downloads", private=False)
alias = os.stat("grok", dir_fd=vendor_bin_fd, follow_symlinks=False)
if not stat.S_ISLNK(alias.st_mode) or alias.st_uid != os.getuid():
    raise SystemExit("refusing unexpected ~/.grok/bin/grok")
link = os.readlink("grok", dir_fd=vendor_bin_fd)
source = Path(os.path.abspath(os.path.join(home_path, ".grok", "bin", link)))
downloads = home_path / ".grok" / "downloads"
if (
    source.parent != downloads
    or re.fullmatch(r"grok-[A-Za-z0-9._-]+", source.name) is None
):
    raise SystemExit("refusing unexpected Grok download path")
source_fd = os.open(source.name, os.O_RDONLY | NOFOLLOW, dir_fd=downloads_fd)
source_meta = os.fstat(source_fd)
if (
    not stat.S_ISREG(source_meta.st_mode)
    or source_meta.st_uid != os.getuid()
    or source_meta.st_nlink != 1
    or not (source_meta.st_mode & stat.S_IXUSR)
    or stat.S_IMODE(source_meta.st_mode) & 0o022
    or sha256(source_fd) != DIGEST
):
    raise SystemExit("refusing unsafe or unreviewed Grok download")

os.umask(0o077)
unified_fd = directory(home_fd, ".unified-cli", create=True)
providers_fd = directory(unified_fd, "providers", create=True)
grok_fd = directory(providers_fd, "grok", create=True)
try:
    os.mkdir(SNAPSHOT, 0o700, dir_fd=grok_fd)
    os.fsync(grok_fd)
except FileExistsError as exc:
    raise SystemExit("snapshot already exists; refusing reuse") from exc
root_fd = directory(grok_fd, SNAPSHOT)
bin_fd = directory(root_fd, "bin", create=True)
provider_home_fd = directory(root_fd, "home", create=True)
state_fd = directory(provider_home_fd, ".grok", create=True)
login_fd = directory(root_fd, "login-cwd", create=True)
tmp_fd = directory(root_fd, "tmp", create=True)

target_fd = os.open(
    "grok",
    os.O_RDWR | os.O_CREAT | os.O_EXCL | NOFOLLOW,
    0o500,
    dir_fd=bin_fd,
)
target_meta = os.fstat(target_fd)
if (
    not stat.S_ISREG(target_meta.st_mode)
    or target_meta.st_uid != os.getuid()
    or target_meta.st_nlink != 1
    or stat.S_IMODE(target_meta.st_mode) != 0o500
):
    raise SystemExit("refusing unsafe snapshot binary")
while block := os.read(source_fd, 1024 * 1024):
    while block:
        written = os.write(target_fd, block)
        block = block[written:]
os.fsync(target_fd)
os.fsync(bin_fd)
if sha256(target_fd) != DIGEST:
    raise SystemExit("snapshot digest verification failed")

config = GROK_SAFE_CONFIG.encode("utf-8")
config_fd = os.open(
    "config.toml",
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
    0o600,
    dir_fd=state_fd,
)
config_meta = os.fstat(config_fd)
if (
    not stat.S_ISREG(config_meta.st_mode)
    or config_meta.st_uid != os.getuid()
    or config_meta.st_nlink != 1
    or stat.S_IMODE(config_meta.st_mode) != 0o600
):
    raise SystemExit("refusing unsafe config target")
while config:
    written = os.write(config_fd, config)
    config = config[written:]
os.fsync(config_fd)
os.fsync(state_fd)

root = home_path / ".unified-cli" / "providers" / "grok" / SNAPSHOT
binary = root / "bin" / "grok"
provider_home = root / "home"
login_cwd = root / "login-cwd"
tmp = root / "tmp"
if os.listdir(login_fd):
    raise SystemExit("refusing non-empty login cwd")
fixed_environment = environment(provider_home, tmp)
require_version(binary, login_cwd, fixed_environment)
os.fchdir(login_fd)
os.execve(
    str(binary),
    [str(binary), "login", "--device-auth"],
    fixed_environment,
)
PY
```

일반 host 로그인을 재사용하지 않습니다. 위의 단일 setup 호출은 no-follow exclusive
open으로 config와 복사 바이너리를 만들고, regular single-link metadata를 확인한 뒤 둘
다 fsync합니다. 이어서 복사본의 digest와 정확히 파싱한 `0.2.111` version을 다시
확인하고 login cwd가 비어 있는지 검사한 다음, 최소 고정 environment와 고정 login
argv로 `execve`합니다. 기존 snapshot/config를 truncate하거나 교체하지 않고, 기존
경로를 따라가지 않으며, shell interpolation을 사용하거나 auth 내용을 읽거나 credential을
출력하지 않습니다. qualified snapshot이 이미 있으면 조용히 재사용하지 않고 setup을
거부합니다. 이미 인증한 snapshot을 사용하거나, 의도적인 새 setup 전에 직접
검사·제거하세요. 이 snapshot을 인증한 뒤 같은 canonical 바이너리와 home을 Core의 공개
확장 설정 API에 등록합니다.

```bash
python - <<'PY'
from pathlib import Path
from unified_cli import ExtensionLaunchOverridesV1, configure_extension_provider

root = (
    Path.home()
    / ".unified-cli"
    / "providers"
    / "grok"
    / "native-0.2.111-darwin-arm64"
)
configure_extension_provider(
    "grok",
    ExtensionLaunchOverridesV1(
        bin_path=str(root / "bin" / "grok"),
        provider_home=str(root / "home"),
    ),
)
PY
unified-cli chat "이 프로젝트를 설명해줘" --provider grok --model grok-4.5
```

이는 사용자가 복사해 실행하는 안내일 뿐 Ext가 설치나 로그인을 실행하지 않습니다. vendor
CLI를 의도적으로 업데이트한 뒤에는 snapshot 복사와 등록을 다시 수행합니다.

각 prompt에서 어댑터는 `--no-auto-update`, strict sandbox, `dontAsk`를 고정하고
`read_file`, `grep`, `list_dir`만 허용합니다. 고정 process environment는 updater, write,
tool-search, LSP, memory, subagent, web과 Claude/Cursor/Codex skills, rules, agents, MCP,
hooks, sessions를 끄고 gitignore-aware 탐색을 요구합니다. managed MCP, 공식 marketplace
자동 등록도 꺼지고 marketplace package에는 SHA가 필요하며 호출자는 이를 덮어쓸 수
없습니다. 매 요청 직전에 cwd부터 Git root까지(Git 저장소
밖이면 cwd만) `.grok`, `.envrc`, `.mcp.json`, `.cursor/mcp.json`,
`.cursor/hooks.json`, `.claude`를 거부하고,
provider-home shell 시작 파일·runtime config(위의 정확한 `0600` safe template 제외)·plugin·hook
디렉터리·hook 경로 파일 및 관리되는 `/etc/grok` 설정도 거부합니다. provider home은 소유자가
현재 사용자이며 symlink가 아닌 `0700` 디렉터리여야 합니다. `.grok`은 소유자가 현재
사용자이며 symlink가 아니고 group/other write가 없어야 하고, config/auth state는 private
regular single-link 파일이어야 합니다. 이 검증은 auth 내용을 읽거나 해석하지 않습니다.

이 통제는 read-only Preview를 위한 defense in depth이며 완전한 secret boundary가 아닙니다.
gitignore-aware 탐색은 우발적 노출을 줄이지만 ignore된 파일이나 workspace에서 읽을 수 있는
파일을 vendor 프로세스로부터 비밀로 만들지는 않습니다.

오프라인 fixture는 어댑터를 검증했습니다. 공식 native Grok `0.2.111`(commit marker
`94172f2aa4e5`)의 대표 격리 device-code smoke는 macOS arm64에서 2026-07-23에 통과했으며,
정제한 결과는 [smoke 근거](development/grok-0.2.111-smoke.md)에 기록했습니다. 이는 하나의
version/platform/auth 표본일 뿐 전체 릴리스 호환성을 뜻하지 않습니다. 따라서 Grok은 Stable이
아닌 Preview이고 서버 모드는 계속 비활성입니다.

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

Grok은 **Preview**이고 Qoder, Kilo, Poolside는 **Experimental**이며 나머지 카탈로그
항목은 모두 **Held**입니다. 모든 Ext 서버 정책은 비활성입니다.

## 생성된 provider 지원 상태

아래 기계 상태 표는 명시적인 Ext 엔트리포인트 플러그인에서 생성됩니다. 뒤따르는
상세 후보 전송 방식 카탈로그는 수동 설계 기록으로 유지됩니다.

<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->
| Provider ID | 지원 상태 | Core capability | 서버 |
|---|---|---|---|
| `amp` | `held` | `none` | `disabled` |
| `cline` | `held` | `none` | `disabled` |
| `codebuddy` | `held` | `none` | `disabled` |
| `copilot` | `held` | `none` | `disabled` |
| `cursor` | `held` | `none` | `disabled` |
| `droid` | `held` | `none` | `disabled` |
| `gitlab-duo` | `held` | `none` | `disabled` |
| `grok` | `preview` | `chat, sessions, stream` | `disabled` |
| `hermes` | `held` | `none` | `disabled` |
| `kilo` | `experimental` | `chat` | `disabled` |
| `kimi` | `held` | `none` | `disabled` |
| `mistral-vibe` | `held` | `none` | `disabled` |
| `oh-my-pi` | `held` | `none` | `disabled` |
| `opencode` | `held` | `none` | `disabled` |
| `pi` | `held` | `none` | `disabled` |
| `poolside` | `experimental` | `chat` | `disabled` |
| `qoder` | `experimental` | `chat` | `disabled` |
| `qwen` | `held` | `none` | `disabled` |
<!-- END GENERATED EXT PROVIDER SUPPORT -->

## Stage 5B–5F 카탈로그

“후보 전송 방식”은 잠정적인 설계 방향이며 명령 계약이 아닙니다. “자동 업데이트 차단”은
의도된 경계를 설명합니다. Held 메타데이터는 실행하지 않으며, 아래 세 Experimental
어댑터는 명시적으로 실행할 수 있지만 동작이 바뀔 수 있습니다.

Grok, Kimi, Copilot, Cursor 행은 현재 공식 문서 조사와 고정된 호환 목표를 기록합니다.
프롬프트는 argv 값이 되므로 로그에 남겨서는 안 됩니다. Grok은 오프라인 fixture로 검증한
one-shot bridge와 대표 인증 native smoke가 있습니다. Kimi, Copilot, Cursor는
Held이며 factory가 바이너리 탐색, 환경 읽기, 실행 전에 거부합니다. ACP 후보는 활성화된
bridge가 아닙니다.

| Provider ID | 공식 바이너리/패키지 | 후보 전송 방식 | 잠정 어댑터 목표 | 상태 | 자동 업데이트 차단 | 공식 문서 |
|---|---|---|---|---|---|---|
| `grok` | xAI Grok Build (`grok`): 기본 native 설치 경로 `https://x.ai/cli/install.sh`; 공식 npm `@xai-official/grok`은 대안; 관련 없는 `@vibe-kit/grok-cli` CLI 형태 거부 | 스트리밍 JSONL | 명시적 `-p` one-shot, `chat`, `stream`, `sessions`; 정확히 `0.2.111`; 기본 `grok-4.5` | Preview | no-auto-update, strict sandbox, `dontAsk`, write/tool-search/LSP/memory/subagent/web, compatibility scanner, managed MCP, marketplace 자동 등록 비활성; marketplace SHA 및 gitignore-aware 탐색 필요; 정확한 private safe config와 workspace/home/system fail-closed preflight; 완전한 secret boundary가 아닌 defense in depth; offline fixture 및 대표 인증 native `0.2.111` macOS arm64 smoke | [저장소](https://github.com/xai-org/grok-build) · [개요](https://docs.x.ai/build/overview) · [CLI reference](https://docs.x.ai/build/cli/reference) · [Headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| `kimi` | Kimi Code CLI (`kimi`, `@moonshot-ai/kimi-code`), 레거시 Python `kimi-cli`가 아님 | stream JSON 후보 | `-p` one-shot은 일반 도구를 자동 승인; Core capability 없음 | Held | 후보 `KIMI_CODE_NO_AUTO_UPDATE=1`과 `KIMI_DISABLE_TELEMETRY=1`; 실행별 read-only/no-tools/web-off/MCP-off 계약 없음 | [시작하기](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html) · [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html) |
| `copilot` | GitHub Copilot CLI (`copilot`, `@github/copilot`) | 일반 텍스트 one-shot 후보 | 명시적 읽기 전용 도구 후보; Core capability 없음 | Held | 후보 `--no-auto-update`와 도구/MCP 제어; JSONL schema, 전체 사용자/workspace MCP, 전용 home 격리는 미검증 | [설치](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) · [CLI 명령 reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [ACP 서버](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) |
| `cursor` | Cursor Agent CLI (`agent`가 기본, `cursor-agent`는 2026-01-08부터 레거시 별칭) | 최종/스트림 JSON schema 후보 | 위치 인자 프롬프트 ABI를 안전하게 표현할 수 없음; Core capability 없음 | Held | 검증된 read-only, MCP, 업데이트 차단 없음; `CURSOR_API_KEY`는 환경 변수 전용이며 argv에 넣지 않음 | [설치](https://cursor.com/docs/cli/installation) · [파라미터](https://cursor.com/docs/cli/reference/parameters) · [출력 형식](https://cursor.com/docs/cli/reference/output-format) · [ACP](https://cursor.com/docs/cli/acp) |
| `codebuddy` | CodeBuddy Code (`codebuddy`, `@tencent-ai/codebuddy-code`) | JSONL 프로토콜 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `DISABLE_AUTOUPDATER=1`; 정확한 프레임과 설정 격리 검증 필요 | [CLI reference](https://www.codebuddy.ai/docs/cli/cli-reference) · [Headless mode](https://www.codebuddy.ai/docs/cli/headless) · [ACP](https://www.codebuddy.ai/docs/cli/acp) |
| `qoder` | Qoder CLI (`qodercli`, `@qoder-ai/qodercli`) | ACP stdio | 명시적 `chat`; Core 서버 비활성 | Experimental | 전용 설정 `general.enableAutoUpdate=false`; 제한된 ACP 수명 주기 제어로 실행 가능하나 Experimental 변경 가능 | [빠른 시작](https://docs.qoder.com/en/cli/quick-start) · [ACP](https://docs.qoder.com/en/cli/acp) · [권한](https://docs.qoder.com/en/cli/permissions) |
| `mistral-vibe` | Mistral Vibe (`vibe`, `mistral-vibe`) | JSONL 메시지 스트림 후보 | `chat` 후보; Core capability 없음 | Held | 업데이트 확인을 끈 전용 설정 후보; direct와 `vibe-acp` 경로를 따로 검증해야 함 | [설치](https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli) · [CLI 사용](https://docs.mistral.ai/vibe/code/cli/work-with-cli) · [ACP surface](https://docs.mistral.ai/vibe/code/choose-cli-vscode-web-sessions) |
| `qwen` | Qwen Code (`qwen`, `@qwen-code/qwen-code`) | JSONL 후보 | `chat` 후보; Core capability 없음 | Held | backend 선택, 자격 정보, 업데이트 동작, event schema 검증 필요 | [저장소](https://github.com/QwenLM/qwen-code) · [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/) · [인증](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) |
| `cline` | Cline CLI (`cline`) | JSONL 후보; ACP는 별도 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `CLINE_NO_AUTO_UPDATE=1`; stdin EOF, event schema, 로컬 설정 격리 검증 필요 | [CLI 개요](https://docs.cline.bot/usage/cli-overview) · [CLI reference](https://docs.cline.bot/cli/cli-reference) · [릴리스 소스](https://github.com/cline/cline/tree/cli-v3.0.46/apps/cli) |
| `opencode` | OpenCode (`opencode`, 패키지 `opencode-ai`) | `JSONL one-shot` 후보 | `chat` 후보; Core capability 없음 | Held | 후보 비활성화 제어는 검증이 필요하며 stdin EOF, config/MCP 격리, 프로세스/세션 수명 주기는 Stage 6 관문으로 남음 | [문서](https://opencode.ai/docs/) · [CLI](https://opencode.ai/docs/cli/) · [서버](https://opencode.ai/docs/server/) |
| `kilo` | Kilo Code (`kilo`, 패키지 `@kilocode/cli`) | `내부 loopback 서버가 있는 ACP stdio` | 명시적 `chat`; Core 서버 비활성 | Experimental | 제한된 ACP loopback/프로세스/config/권한 제어로 실행 가능하나 동작이 바뀔 수 있음 | [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli) · [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference) · [릴리스](https://github.com/Kilo-Org/kilocode/releases/tag/v7.4.11) |
| `droid` | Factory Droid (`droid`, npm 패키지 `droid`) | vendor stream JSON-RPC 후보 | `chat` 후보; Core capability 없음 | Held | 후보 업데이트 제어, 프로토콜 envelope, 권한 흐름, 프로세스 수명 주기는 Stage 6 검증 필요 | [CLI reference](https://docs.factory.ai/reference/cli-reference) · [Droid Exec](https://docs.factory.ai/cli/droid-exec/overview) · [패키지 메타데이터](https://registry.npmjs.org/droid/latest) |
| `pi` | Pi Coding Agent (`pi`, 패키지 `@earendil-works/pi-coding-agent`) | 전용 NDJSON RPC 후보 | `chat` 후보; Core capability 없음 | Held | 후보 `--offline`과 resource 비활성 플래그는 Stage 6 검증 필요; JSON-RPC는 아님 | [패키지](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json) · [README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) · [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) |
| `oh-my-pi` | Oh My Pi (`omp`, 패키지 `@oh-my-pi/pi-coding-agent`) | 전용 NDJSON RPC 후보 | `chat` 후보; Core capability 없음 | Held | 검증된 업데이트 차단 방식 주장은 아직 없음; 설정, resource, 권한, 프로세스 수명 주기는 Stage 6 검증 필요 | [저장소](https://github.com/can1357/oh-my-pi) · [RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md) · [Approval mode](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) |
| `hermes` | Hermes Agent (`hermes`, PyPI `hermes-agent[acp]`) | ACP stdio 후보 | `chat` 후보; Core capability 없음 | Held | Hermes는 ACP 0.9.0을 고정하지만 Ext는 0.11.x를 대상으로 함; 호환성, 설정, 수명 주기는 Stage 6 검증 필요 | [PyPI](https://pypi.org/project/hermes-agent/) · [저장소](https://github.com/NousResearch/hermes-agent) · [ACP 가이드](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md) |
| `poolside` | Poolside Agent CLI (`pool`, 공식 native 릴리스) | ACP stdio | 명시적 `chat`; Core 서버 비활성 | Experimental | 제한된 ACP 시작/설정 제어로 실행 가능하며 proprietary 바이너리 동작은 바뀔 수 있음 | [설치](https://docs.poolside.ai/cli/install) · [CLI reference](https://docs.poolside.ai/cli/cli-reference) · [릴리스](https://github.com/poolsideai/pool/releases/tag/v1.0.13) |
| `amp` | Amp CLI (`amp`, 정식 패키지 `@ampcode/cli`) | Claude 호환 스트리밍 JSONL 입출력 후보 | `chat` 후보; Core capability 없음 | Held | 기본적으로 도구 실행 전 승인을 묻지 않으며 workspace 설정, plugin, MCP, EOF/프로세스 수명 주기와 유료 opt-in 실행은 격리된 Stage 6 근거가 필요 | [매뉴얼](https://ampcode.com/manual) · [스트림 schema](https://ampcode.com/manual/appendix) · [패키지](https://www.npmjs.com/package/@ampcode/cli) |
| `gitlab-duo` | GitLab Duo CLI (`duo`, compiled generic package 또는 공식 npm 패키지 `@gitlab/duo-cli`) | one-shot JSON 후보 | `chat` 후보; Core capability 없음 | Held | headless 실행은 도구를 자동 승인하므로 JSON schema 1.0, 인증/사용 권한, context/MCP/hook, 정리와 격리에 대한 Stage 6 근거가 필요 | [개요](https://docs.gitlab.com/user/gitlab_duo_cli/) · [사용법](https://docs.gitlab.com/user/gitlab_duo_cli/use/) · [설정](https://docs.gitlab.com/user/gitlab_duo_cli/set_up/) |

선택적 `acp`, `mcp` extra는 각각 `unified-cli[acp]`, `unified-cli[mcp]`로 프로토콜 SDK
의존성만 설치합니다. 다른 provider를 활성화하거나 provider 호출을 만들지 않습니다.

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
