from pathlib import Path

import pytest


@pytest.fixture
def fake_cli():
    return str(Path(__file__).parent / "fixtures" / "providers" / "fake_cli.py")
