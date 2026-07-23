"""Small, prompt-free diagnostics for opt-in Preview provider failures."""

from __future__ import annotations

import os
import stat
import time
import uuid
from pathlib import Path
from typing import Optional

from ..errors import ExtensionError, ProcessFailed


REPORT_URL = "https://github.com/MinwooKim1990/unified_cli/issues/new"


def _private_directory(path: Path) -> bool:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return False
    return True


def write_preview_diagnostic(
    provider: str, error: ExtensionError
) -> Optional[str]:
    """Write a bounded report containing no prompt, token, environment, or auth data."""

    base = Path.home() / ".unified-cli"
    if not _private_directory(base):
        return None
    directory = base / "preview-diagnostics"
    if not _private_directory(directory):
        return None

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / "{}-{}-{}.log".format(
        provider, stamp, uuid.uuid4().hex[:12]
    )
    lines = [
        "unified-cli Preview provider diagnostic",
        "provider={}".format(provider),
        "error_type={}".format(type(error).__name__),
    ]
    if isinstance(error, ProcessFailed):
        lines.append("returncode={}".format(error.returncode))
    lines.append("report_url={}".format(REPORT_URL))
    payload = ("\n".join(lines) + "\n").encode("utf-8", "replace")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return str(path)


__all__ = ["REPORT_URL", "write_preview_diagnostic"]
