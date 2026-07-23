from __future__ import annotations

import os
from pathlib import Path

from unified_cli_ext.errors import ProcessFailed, ProtocolError
from unified_cli_ext.providers import diagnostics
from unified_cli_ext.providers.bridge import _core_error


def test_preview_failure_writes_private_prompt_free_report(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    error = ProcessFailed(7, "redacted provider stderr")

    translated = _core_error("kimi", error)

    directory = tmp_path / ".unified-cli" / "preview-diagnostics"
    reports = list(directory.iterdir())
    assert len(reports) == 1
    report = reports[0]
    text = report.read_text()
    assert "provider=kimi" in text
    assert "returncode=7" in text
    assert "redacted provider stderr" not in text
    assert "prompt=" not in text.lower()
    assert os.stat(directory).st_mode & 0o077 == 0
    assert os.stat(report).st_mode & 0o077 == 0
    assert str(report) in translated.message
    assert diagnostics.REPORT_URL in translated.message


def test_protocol_failure_report_does_not_copy_exception_text(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    translated = _core_error("qwen", ProtocolError("secret-like raw payload"))

    report = next(
        (tmp_path / ".unified-cli" / "preview-diagnostics").iterdir()
    ).read_text()
    assert "secret-like raw payload" not in report
    assert "invalid response" in translated.message
