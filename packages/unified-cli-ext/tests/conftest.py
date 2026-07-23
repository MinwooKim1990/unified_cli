import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def force_darwin_libc_waitid_for_portability_checks(monkeypatch):
    """Let CI run existing cleanup tests through the Darwin compatibility path."""

    if os.environ.get("UNIFIED_CLI_EXT_TEST_DARWIN_LIBC_WAITID") != "1":
        return
    if sys.platform != "darwin":
        pytest.fail("Darwin libc waitid checks require macOS")
    process_module = __import__(
        "unified_cli_ext.transports.process", fromlist=["unused"]
    )
    monkeypatch.setattr(process_module, "_NATIVE_WAITID", None)
    monkeypatch.setattr(process_module, "_DARWIN_LIBC_WAITID", None)
    process_module._require_nonreaping_process_observation()


@pytest.fixture
def fake_cli(tmp_path):
    source = Path(__file__).parent / "fixtures" / "providers" / "fake_cli.py"
    interpreter = tmp_path / "fixture-python"
    shutil.copyfile(os.path.realpath(sys.executable), interpreter)
    interpreter.chmod(0o700)
    source_text = source.read_text(encoding="utf-8")
    _, separator, body = source_text.partition("\n")
    assert separator
    target = tmp_path / "fake-cli"
    target.write_text(
        "#!{}\n{}".format(interpreter, body),
        encoding="utf-8",
    )
    target.chmod(0o700)
    return str(target)
