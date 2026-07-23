"""Lazy PATH discovery for explicitly selected Preview providers."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from ..errors import ConfigurationError
from .installation import InstallationReceiptV1


def _manifest_for_target(
    target: str, executable: str, package_names: Tuple[str, ...]
) -> tuple[str, str, str]:
    target_path = Path(target)
    for depth, candidate in enumerate((target_path.parent, *target_path.parents)):
        if depth >= 16:
            break
        manifest_path = candidate / "package.json"
        try:
            metadata = manifest_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_size > 1024 * 1024
            ):
                continue
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        version = payload.get("version")
        bins = payload.get("bin")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or (package_names and name not in package_names)
        ):
            continue
        if isinstance(bins, str):
            mapped = bins
        elif isinstance(bins, dict):
            mapped = bins.get(executable)
        else:
            mapped = None
        if not isinstance(mapped, str):
            continue
        expected = os.path.realpath(os.path.join(str(candidate), mapped))
        if expected == target:
            return str(candidate), name, version
    raise ConfigurationError("Preview npm launcher package metadata was not found")


def _interpreter_for_target(target: str) -> Optional[str]:
    try:
        with open(target, "rb") as stream:
            first = stream.readline(4096)
    except OSError:
        raise ConfigurationError("Preview launcher could not be inspected") from None
    if not first.startswith(b"#!"):
        return None
    try:
        parts = shlex.split(first[2:].decode("utf-8", "strict").strip())
    except (UnicodeError, ValueError):
        raise ConfigurationError("Preview launcher shebang is unsupported") from None
    if not parts:
        raise ConfigurationError("Preview launcher shebang is unsupported")
    command = parts[0]
    if os.path.basename(command) == "env":
        if len(parts) != 2:
            raise ConfigurationError("Preview launcher env shebang is unsupported")
        command = shutil.which(parts[1]) or ""
    if not command or not os.path.isabs(command):
        raise ConfigurationError("Preview launcher interpreter is unavailable")
    return os.path.realpath(command)


def resolve_path_installation(
    *,
    provider_id: str,
    executable: str,
    package_names: Iterable[str] = (),
) -> InstallationReceiptV1:
    """Capture a direct or npm receipt from PATH only after explicit selection."""

    launcher = shutil.which(executable)
    if launcher is None:
        raise ConfigurationError(
            "{} is not installed or is not available on PATH".format(executable)
        )
    launcher = os.path.abspath(launcher)
    target = os.path.realpath(launcher)
    # npm global launchers are commonly symlinks whose package target keeps
    # the public executable basename (for example ``bin/grok``).  Direct
    # vendor installers also commonly publish a symlink to a same-named
    # executable.  Treat only targets actually owned by a ``node_modules``
    # tree as npm candidates; same-named targets elsewhere remain explicit
    # direct installations.
    target_parts = os.path.normpath(target).split(os.sep)
    if os.path.basename(target) == executable and "node_modules" not in target_parts:
        return InstallationReceiptV1.capture_explicit_direct(
            provider_id=provider_id,
            executable_path=target,
            executable_basename=executable,
        )

    package_root, package_name, package_version = _manifest_for_target(
        target, executable, tuple(package_names)
    )
    ownership_root = os.path.commonpath((launcher, package_root))
    if ownership_root == os.path.sep:
        raise ConfigurationError("Preview npm installation ownership is ambiguous")
    return InstallationReceiptV1.capture_npm(
        provider_id=provider_id,
        launcher_path=launcher,
        executable_basename=executable,
        package_root=package_root,
        ownership_root=ownership_root,
        distribution_name=package_name,
        distribution_version=package_version,
        acquisition_source="path-discovery",
        interpreter_path=_interpreter_for_target(target),
    )


def path_launch_resolver(
    *,
    provider_id: str,
    executable: str,
    package_names: Iterable[str] = (),
) -> Callable[[], InstallationReceiptV1]:
    packages = tuple(package_names)

    def resolve() -> InstallationReceiptV1:
        return resolve_path_installation(
            provider_id=provider_id,
            executable=executable,
            package_names=packages,
        )

    return resolve


__all__ = ["path_launch_resolver", "resolve_path_installation"]
