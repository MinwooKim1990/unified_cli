#!/usr/local/bin/python3
"""Fixed, accountless-only provider actions inside the Stage-6C container.

No action accepts a prompt, arbitrary argv, path, URL, environment value, or
shell string. Current candidate profiles have no validated canonical supply
manifest, so real install refuses before invoking a network-capable tool.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys


AUTH_HOME = "/home/lab"
TOOL_ROOT = "/opt/unified-ext-lab/tool"
WORKSPACE = "/workspace"
MAX_PROBE_OUTPUT_BYTES = 64 * 1024
PROBE_TIMEOUT_SECONDS = 10

# profile_sha256 values are filled from the source-controlled host profile by
# the context-lock update.  They are identities, not package artifact hashes.
PROFILES = {
    "grok": {
        "binary": "grok",
        "expected_version": "0.2.106",
        "help": ("grok", "--help"),
        "profile_sha256": "68d7bb3fd823c3f10b9041943f64cf3dfa9675951110c81ab2fad5db9c63ca9e",
        "status": None,
        "version": ("grok", "--version"),
    },
    "kimi": {
        "binary": "kimi",
        "expected_version": "0.29.0",
        "help": ("kimi", "--help"),
        "profile_sha256": "38c678c1303e7843b9cec4a196315cf3c52c7ecc1a39b431343c863c64d7c330",
        "status": None,
        "version": ("kimi", "--version"),
    },
    "copilot": {
        "binary": "copilot",
        "expected_version": "1.0.73",
        "help": ("copilot", "help"),
        "profile_sha256": "029a29799cd7c507de7faf443b865b4fcfa5179c26290664051e62fe44961631",
        "status": None,
        "version": ("copilot", "--binary-version"),
    },
    "cursor": {
        "binary": "agent",
        "expected_version": "2026.07.20-8cc9c0b",
        "help": ("agent", "--help"),
        "profile_sha256": "8e983f66bbdeac02d82004a9e7895bf8257e6e0a1462a509abd2ee6ba1008316",
        "status": ("agent", "status"),
        "version": ("agent", "--version"),
    },
}


def _profile() -> tuple:
    if len(sys.argv) != 4:
        raise ValueError("one fixed provider action is required")
    action = sys.argv[1]
    provider_id = sys.argv[2]
    claimed_hash = sys.argv[3]
    profile = PROFILES.get(provider_id)
    if action not in ("install", "test", "logout") or profile is None:
        raise ValueError("unsupported provider action")
    if profile["profile_sha256"] != claimed_hash:
        raise ValueError("provider profile identity mismatch")
    return action, provider_id, profile


def _safe_environment(provider_id: str, binary_directory: str) -> dict:
    environment = {
        "HOME": AUTH_HOME,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": binary_directory + ":/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": AUTH_HOME + "/.cache",
        "XDG_CONFIG_HOME": AUTH_HOME + "/.config",
        "XDG_DATA_HOME": AUTH_HOME + "/.local/share",
    }
    if provider_id == "kimi":
        environment["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
        environment["KIMI_DISABLE_CRON"] = "1"
        environment["KIMI_DISABLE_TELEMETRY"] = "1"
    elif provider_id == "copilot":
        environment["COPILOT_AUTO_UPDATE"] = "false"
    return environment


def _installed_binary(provider_id: str, profile: dict) -> str:
    binary = os.path.join(TOOL_ROOT, provider_id, "bin", profile["binary"])
    try:
        info = os.stat(binary, follow_symlinks=False)
    except OSError as error:
        raise ValueError("provider binary is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_gid != os.getgid()
        or not (info.st_mode & stat.S_IXUSR)
    ):
        raise ValueError("provider binary identity is unsafe")
    return binary


def _contains_exact_version(output: bytes, expected_token: str) -> bool:
    token = re.escape(expected_token.encode("ascii"))
    pattern = rb"(?<![0-9A-Za-z._+-])" + token + rb"(?![0-9A-Za-z._+-])"
    return re.search(pattern, output) is not None


def _run_probe(
    form: tuple,
    binary: str,
    environment: dict,
    expected_token: str = "",
) -> bool:
    argv = (binary,) + tuple(form[1:])
    completed = subprocess.run(
        argv,
        cwd=WORKSPACE,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    output = completed.stdout + completed.stderr
    output_size = len(output)
    if output_size == 0 or output_size > MAX_PROBE_OUTPUT_BYTES:
        return False
    return completed.returncode == 0 and (
        not expected_token or _contains_exact_version(output, expected_token)
    )


def _install(provider_id: str, profile: dict) -> int:
    # No current profile names a validated canonical supply manifest. Cursor
    # additionally lacks an evidenced artifact checksum. Refuse here as a
    # second boundary after the host gate.
    del provider_id, profile
    print("provider acquisition is held", file=sys.stderr)
    return 3


def _test(provider_id: str, profile: dict) -> int:
    binary = _installed_binary(provider_id, profile)
    environment = _safe_environment(provider_id, os.path.dirname(binary))
    version_ok = _run_probe(
        profile["version"], binary, environment, profile["expected_version"]
    )
    help_ok = _run_probe(profile["help"], binary, environment)
    status_form = profile["status"]
    status_ok = status_form is None or _run_probe(
        status_form, binary, environment
    )
    if not (version_ok and help_ok and status_ok):
        print("provider accountless probe failed", file=sys.stderr)
        return 6
    print(
        json.dumps(
            {
                "action": "test",
                "help_probe": "passed",
                "profile_sha256": profile["profile_sha256"],
                "status_probe": "passed" if status_form else "not_supported",
                "version_probe": "passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _logout(provider_id: str, profile: dict) -> int:
    # The host lifecycle deliberately does not call this action. Keep a fixed,
    # accountless response for defense in depth if the command grammar is ever
    # inspected or exercised by a future locked profile.
    del provider_id
    print(
        json.dumps(
            {
                "action": "logout",
                "profile_sha256": profile["profile_sha256"],
                "status": "accountless",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def main() -> int:
    try:
        action, provider_id, profile = _profile()
        if action == "install":
            return _install(provider_id, profile)
        if action == "test":
            return _test(provider_id, profile)
        return _logout(provider_id, profile)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("provider action refused", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
