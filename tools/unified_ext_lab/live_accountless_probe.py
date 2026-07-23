#!/usr/bin/env python3
"""Bounded guest probe for real, accountless Ext CLI installations.

This helper is intentionally guest-only.  It does not install software, read
host state, persist receipts, or accept arbitrary commands.  Run it inside a
credential-free disposable container with networking disconnected.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


PROVIDER_MODULES = {
    "amp": "unified_cli_ext.providers.amp",
    "cline": "unified_cli_ext.providers.cline",
    "codebuddy": "unified_cli_ext.providers.codebuddy",
    "copilot": "unified_cli_ext.providers.copilot",
    "cursor": "unified_cli_ext.providers.cursor",
    "droid": "unified_cli_ext.providers.droid",
    "gitlab-duo": "unified_cli_ext.providers.gitlab_duo",
    "grok": "unified_cli_ext.providers.grok",
    "hermes": "unified_cli_ext.providers.hermes",
    "kilo": "unified_cli_ext.providers.kilo",
    "kimi": "unified_cli_ext.providers.kimi",
    "mistral-vibe": "unified_cli_ext.providers.mistral_vibe",
    "oh-my-pi": "unified_cli_ext.providers.oh_my_pi",
    "opencode": "unified_cli_ext.providers.opencode",
    "pi": "unified_cli_ext.providers.pi",
    "poolside": "unified_cli_ext.providers.poolside",
    "qoder": "unified_cli_ext.providers.qoder",
    "qwen": "unified_cli_ext.providers.qwen",
}

_CREDENTIAL_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SYNTHETIC_TURN = "accountless validation"


def _result(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseException):
        from unified_cli.errors import UnifiedError

        if isinstance(value, UnifiedError):
            return {
                "ok": False,
                "error_type": type(value).__name__,
                "kind": value.kind,
                "message": value.message,
                "hint": value.hint,
            }
        return {
            "ok": False,
            "error_type": type(value).__name__,
            "message": str(value)[:512],
        }
    return {"ok": True, "value": value}


def _call(callback) -> dict[str, Any]:
    try:
        return _result(callback())
    except Exception as error:
        return _result(error)


def _command(binary: str, suffix: tuple[str, ...]) -> dict[str, Any]:
    path = shutil.which(binary)
    if path is None:
        return {"ok": False, "error_type": "NotInstalled"}
    try:
        completed = subprocess.run(
            [path, *suffix],
            cwd="/workspace/project",
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "TimeoutExpired"}
    stdout = completed.stdout
    stderr = completed.stderr
    first_line = (stdout or stderr).decode("utf-8", "replace").splitlines()
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "binary": os.path.basename(path),
        "first_line": first_line[0][:256] if first_line else "",
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("provider", choices=tuple(PROVIDER_MODULES))
    parser.add_argument("--exercise-turn", action="store_true")
    args = parser.parse_args()

    import unified_cli
    import unified_cli_ext
    from unified_cli import create
    from unified_cli.extension_config import default_provider_home
    from unified_cli.registry import (
        doctor_provider,
        extension_provider_exists,
        list_providers,
        load_provider_plugin,
        snapshot_provider_descriptor,
    )

    workspace = Path("/workspace/project")
    if not workspace.is_absolute() or not (workspace / ".git").is_dir():
        raise SystemExit("synthetic absolute repository is unavailable")
    credential_env = sorted(
        key
        for key in os.environ
        if any(marker in key.upper() for marker in _CREDENTIAL_MARKERS)
    )
    if credential_env:
        raise SystemExit("credential-like environment variables are forbidden")

    passive = next(
        (
            descriptor
            for descriptor in list_providers(include_ext=True)
            if descriptor.id == args.provider
        ),
        None,
    )
    module = importlib.import_module(PROVIDER_MODULES[args.provider])
    spec = module.ADAPTER_SPEC

    home = Path(default_provider_home(args.provider))
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)

    payload: dict[str, Any] = {
        "provider": args.provider,
        "imports": [unified_cli.__name__, unified_cli_ext.__name__],
        "credential_env_present": False,
        "passive": (
            None
            if passive is None
            else {
                "source": passive.source,
                "lifecycle_status": passive.lifecycle_status,
            }
        ),
        "version": _command(spec.binary.executable, spec.binary.version_probe.command.argv),
        "help": _command(spec.binary.executable, spec.binary.feature_probe.command.argv),
        "exists": _call(lambda: extension_provider_exists(args.provider)),
        "load": _call(lambda: load_provider_plugin(args.provider).id),
        "snapshot": _call(
            lambda: snapshot_provider_descriptor(args.provider).support_status
        ),
        "doctor": _call(lambda: doctor_provider(args.provider)),
        "create": _call(
            lambda: create(
                args.provider,
                cwd=str(workspace),
                timeout=10,
            ).name
        ),
    }

    if args.exercise_turn:
        def exercise() -> str:
            provider = create(args.provider, cwd=str(workspace), timeout=10)
            provider.chat(_SYNTHETIC_TURN)
            return "unexpected_success"

        payload["turn"] = _call(exercise)

    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
