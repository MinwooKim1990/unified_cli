"""CLI wiring for the opt-in local management dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_serve_help_has_manage_flags_without_loading_providers(capsys):
    from unified_cli import cli

    before = set(sys.modules)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--manage" in output
    assert "--workspace" in output
    assert not {
        "unified_cli.registry", "unified_cli.plugin",
        "unified_cli.providers.claude", "unified_cli.providers.codex",
        "unified_cli.providers.gemini",
    }.intersection(set(sys.modules) - before)


def test_manage_prepares_before_browser_and_runs_with_runtime(monkeypatch):
    from unified_cli import cli, server

    events = []
    monkeypatch.setattr(server, "prepare_manage",
                        lambda workspaces: events.append(("prepare", workspaces)) or "a/b token",
                        raising=False)
    monkeypatch.setattr(server, "run", lambda **kwargs: events.append(("run", kwargs)))
    monkeypatch.setitem(sys.modules, "webbrowser", type("Browser", (), {
        "open": staticmethod(lambda url: events.append(("open", url))),
    }))

    assert cli.main([
        "serve", "--manage", "--workspace", "/one", "--workspace", "/two", "--open",
    ]) == 0
    assert events == [
        ("prepare", ("/one", "/two")),
        ("open", "http://127.0.0.1:8000/dashboard#bootstrap=a%2Fb%20token"),
        ("run", {
            "host": "127.0.0.1", "port": 8000, "manage": True,
            "workspaces": ("/one", "/two"),
        }),
    ]


def test_plain_serve_uses_original_run_arguments(monkeypatch):
    from unified_cli import cli, server

    called = {}
    monkeypatch.setattr(server, "run", lambda **kwargs: called.update(kwargs))
    assert cli.main(["serve", "--port", "8123"]) == 0
    assert called == {"host": "127.0.0.1", "port": 8123}


def test_workspace_requires_manage_before_server_import(monkeypatch):
    from unified_cli import cli

    monkeypatch.delitem(sys.modules, "unified_cli.server", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--workspace", "/project"])
    assert exc.value.code == 2


def test_browser_open_failure_does_not_prevent_manage_run(monkeypatch):
    from unified_cli import cli, server

    called = {}
    monkeypatch.setattr(server, "prepare_manage", lambda workspaces: "token", raising=False)
    monkeypatch.setattr(server, "run", lambda **kwargs: called.update(kwargs))
    monkeypatch.setitem(sys.modules, "webbrowser", type("Browser", (), {
        "open": staticmethod(lambda url: (_ for _ in ()).throw(RuntimeError("no browser"))),
    }))

    assert cli.main(["serve", "--manage", "--open"]) == 0
    assert called["manage"] is True


def test_invalid_workspace_backend_error_is_a_safe_cli_failure(monkeypatch, capsys):
    from unified_cli import cli, server

    monkeypatch.setattr(
        server, "prepare_manage", lambda workspaces: (_ for _ in ()).throw(ValueError("bad path")),
        raising=False,
    )
    assert cli.main(["serve", "--manage", "--workspace", "/missing"]) == 2
    captured = capsys.readouterr()
    assert "Management mode could not use the requested workspace." in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("argv", [
    ["serve"],
    ["serve", "--manage", "--workspace", "/project"],
])
def test_server_start_oserror_is_not_misreported_as_workspace_error(monkeypatch, capsys, argv):
    from unified_cli import cli, server

    if "--manage" in argv:
        monkeypatch.setattr(server, "prepare_manage", lambda workspaces: "token", raising=False)
    monkeypatch.setattr(
        server, "run", lambda **kwargs: (_ for _ in ()).throw(OSError("port already in use")),
    )

    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "local server could not start or bind" in captured.err
    assert "workspace" not in captured.err.lower()
