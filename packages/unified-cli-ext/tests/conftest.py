import os
import shutil
import sys
from pathlib import Path

import pytest


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
