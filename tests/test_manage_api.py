"""Raw security/contract tests for the opt-in browser manage API."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from unified_cli import manage, server, session_manager, settings  # noqa: E402
from unified_cli.core import Response, Usage  # noqa: E402
from unified_cli.errors import UnifiedError  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    server.disable_manage()
    monkeypatch.delenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", raising=False)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(settings, "SETTINGS_DIR", state_dir)
    monkeypatch.setattr(settings, "SETTINGS_FILE", state_dir / "settings.json")
    monkeypatch.setattr(session_manager, "SESSIONS_DIR", state_dir)
    monkeypatch.setattr(session_manager, "SESSIONS_FILE", state_dir / "sessions.json")
    yield
    server.disable_manage()


def run(coro):
    return asyncio.run(coro)


def client(*, base_url="http://127.0.0.1", peer="127.0.0.1"):
    transport = httpx.ASGITransport(app=server.app, client=(peer, 43123))
    return httpx.AsyncClient(transport=transport, base_url=base_url)


async def bootstrap(api, token, *, origin=None):
    headers = {"X-Unified-Bootstrap": token, "Sec-Fetch-Site": "same-origin"}
    if origin is not None:
        headers["Origin"] = origin
    response = await api.get("/api/ui/v1/bootstrap", headers=headers)
    if response.status_code == 200:
        csrf = response.json().get("csrf_token")
        if isinstance(csrf, str):
            api.headers["X-CSRF-Token"] = csrf
    return response


def mutation_headers(csrf, origin="http://127.0.0.1"):
    return {"Origin": origin, "X-CSRF-Token": csrf}


def test_manage_disabled_is_404_but_plain_dashboard_remains_available():
    async def scenario():
        async with client() as api:
            disabled = await api.get("/api/ui/v1/providers")
            dashboard = await api.get("/dashboard")
            assert disabled.status_code == 404
            assert disabled.json()["error"]["code"] == "manage_disabled"
            assert dashboard.status_code == 200
            assert "default-src 'self'" in dashboard.headers["content-security-policy"]
    run(scenario())


def test_bootstrap_one_time_replay_cookie_scope_and_safe_metadata(tmp_path):
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as first:
            exchanged = await bootstrap(first, token)
            assert exchanged.status_code == 200
            body = exchanged.json()
            assert body["mode"] == "manage"
            assert body["authenticated"] is True
            assert body["csrf_token"]
            assert body["versions"]["unified_cli"] == "0.5.0"
            assert body["limits"]["image_total_bytes"] == 12 * 1024 * 1024
            assert body["providers"]
            assert body["workspaces"][0]["id"].startswith("ws_")
            assert str(tmp_path) not in json.dumps(body)
            cookie = exchanged.headers["set-cookie"]
            assert "HttpOnly" in cookie
            assert "SameSite=strict" in cookie
            assert "Path=/api/ui/v1" in cookie
            assert "Domain=" not in cookie

            refetch = await first.get(
                "/api/ui/v1/bootstrap", headers={"Sec-Fetch-Site": "same-origin"})
            assert refetch.status_code == 200
            assert refetch.json()["csrf_token"] == body["csrf_token"]
            assert "set-cookie" not in refetch.headers

        async with client() as replay:
            rejected = await bootstrap(replay, token)
            assert rejected.status_code == 401
            assert token not in rejected.text
    run(scenario())


def test_manage_cookie_alone_is_not_an_authenticated_browser_proof(tmp_path):
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            exchanged = await bootstrap(api, token)
            csrf = exchanged.json()["csrf_token"]
            del api.headers["X-CSRF-Token"]
            providers = await api.get("/api/ui/v1/providers")
            refetch = await api.get(
                "/api/ui/v1/bootstrap",
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            assert providers.status_code == 403
            assert providers.json()["error"]["code"] == "csrf_required"
            assert refetch.status_code == 403
            api.headers["X-CSRF-Token"] = csrf
            assert (await api.get("/api/ui/v1/providers")).status_code == 200

    run(scenario())


def test_bootstrap_expiry_and_cross_site_fetch_metadata(tmp_path):
    token = server.prepare_manage([str(tmp_path)])
    runtime = manage.get_manage_runtime()
    assert runtime is not None
    runtime._bootstrap_expires = time.monotonic() - 1

    async def scenario():
        async with client() as api:
            expired = await bootstrap(api, token)
            assert expired.status_code == 401
        fresh = server.prepare_manage([str(tmp_path)])
        async with client() as api:
            cross_site = await api.get("/api/ui/v1/bootstrap", headers={
                "X-Unified-Bootstrap": fresh,
                "Sec-Fetch-Site": "cross-site",
            })
            assert cross_site.status_code == 403
    run(scenario())


def test_mutations_require_exact_origin_and_csrf(tmp_path):
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            response = await bootstrap(api, token)
            csrf = response.json()["csrf_token"]
            missing = await api.patch("/api/ui/v1/settings", json={"theme": "dark"})
            bad_origin = await api.patch(
                "/api/ui/v1/settings", json={"theme": "dark"},
                headers=mutation_headers(csrf, "http://localhost"),
            )
            bad_csrf = await api.patch(
                "/api/ui/v1/settings", json={"theme": "dark"},
                headers=mutation_headers("wrong"),
            )
            valid = await api.patch(
                "/api/ui/v1/settings",
                json={"theme": "dark", "lang": "ko", "browser_permission": "read_only"},
                headers=mutation_headers(csrf),
            )
            assert [missing.status_code, bad_origin.status_code, bad_csrf.status_code] == [403, 403, 403]
            assert valid.status_code == 200
            assert valid.json()["theme"] == "dark"
            assert valid.json()["lang"] == "ko"
            assert stat.S_IMODE(settings.SETTINGS_FILE.stat().st_mode) == 0o600
    run(scenario())


@pytest.mark.parametrize(
    "base_url,peer,host",
    [
        ("http://127.0.0.1", "198.51.100.9", "127.0.0.1"),
        ("http://127.0.0.1", "127.0.0.1", "evil.example"),
        ("http://198.51.100.9", "127.0.0.1", "127.0.0.1"),
    ],
)
def test_manage_rejects_nonloopback_peer_host_or_bound_even_external_opt_in(
    tmp_path, monkeypatch, base_url, peer, host,
):
    server.prepare_manage([str(tmp_path)])
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv(
        "UNIFIED_CLI_SERVER_AUTH_TOKEN",
        "strong-external-token-0123456789-abcdefghijklmnopqrstuvwxyz",
    )

    async def scenario():
        async with client(base_url=base_url, peer=peer) as api:
            response = await api.get("/api/ui/v1/bootstrap", headers={"Host": host})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "manage_loopback_required"
    run(scenario())


def test_plain_dashboard_external_bearer_contract_is_preserved(monkeypatch):
    token = "strong-external-token-0123456789-abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("UNIFIED_CLI_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv("UNIFIED_CLI_SERVER_AUTH_TOKEN", token)

    async def scenario():
        async with client(base_url="http://198.51.100.9", peer="198.51.100.8") as api:
            denied = await api.get("/dashboard")
            allowed = await api.get(
                "/dashboard", headers={"Authorization": "Bearer " + token})
            assert denied.status_code == 401
            assert allowed.status_code == 200
    run(scenario())


def test_security_headers_csp_no_cors_and_manage_body_limit(tmp_path, monkeypatch):
    token = server.prepare_manage([str(tmp_path)])
    monkeypatch.setattr(server, "_MAX_UI_BODY_BYTES", 64)

    async def scenario():
        async with client() as api:
            response = await bootstrap(api, token)
            for key, value in {
                "x-content-type-options": "nosniff",
                "referrer-policy": "no-referrer",
                "cache-control": "no-store",
                "x-frame-options": "DENY",
            }.items():
                assert response.headers[key] == value
            csp = response.headers["content-security-policy"]
            assert "script-src 'self'" in csp
            assert "style-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "access-control-allow-origin" not in response.headers
            oversized = await api.post(
                "/api/ui/v1/chat", content=b"{" + b"x" * 100,
                headers={"Content-Type": "application/json"},
            )
            assert oversized.status_code == 413
    run(scenario())


def test_bootstrap_and_provider_listing_do_not_probe_models_or_import_plugins(
    tmp_path, monkeypatch,
):
    calls = {"models": 0, "popen": 0, "load": 0}

    class EntryPoint:
        name = "safeext"
        group = "unified_cli.providers.v1"

        def load(self):
            calls["load"] += 1
            raise AssertionError("plugin imported")

    import unified_cli.registry as registry
    registry._reset_provider_registry_for_tests()
    monkeypatch.setattr(registry.importlib_metadata, "entry_points", lambda: [EntryPoint()])
    monkeypatch.setattr(manage, "list_models", lambda *_a, **_kw: calls.__setitem__("models", calls["models"] + 1))
    monkeypatch.setattr(manage.subprocess, "Popen", lambda *_a, **_kw: calls.__setitem__("popen", calls["popen"] + 1))
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            response = await bootstrap(api, token)
            assert response.status_code == 200
            assert any(row["id"] == "safeext" for row in response.json()["providers"])
    run(scenario())
    assert calls == {"models": 0, "popen": 0, "load": 0}


def test_model_discovery_is_an_explicit_same_origin_mutation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "list_models", lambda provider: calls.append(provider) or [])
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            exchanged = await bootstrap(api, token)
            csrf = exchanged.json()["csrf_token"]
            assert (await api.get(
                "/api/ui/v1/providers/claude/models")).status_code == 405
            missing_origin = await api.post(
                "/api/ui/v1/providers/claude/models", json={})
            assert missing_origin.status_code == 403
            allowed = await api.post(
                "/api/ui/v1/providers/claude/models", json={},
                headers=mutation_headers(csrf),
            )
            assert allowed.status_code == 200
            assert allowed.json() == {"provider": "claude", "models": []}
            assert calls == ["claude"]

    run(scenario())


def test_verify_fixed_argv_temp_cwd_minimal_env_and_redaction(tmp_path, monkeypatch):
    log = tmp_path / "verify.log"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    script = binary_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s|%s|%s|%s\\n' \"$PWD\" \"$1 $2 $3\" \"$DANGEROUS_SECRET\" \"$HOME\" >> {log}\n"
        "printf 'version user@example.com token=supersecret %s\\n' \"$HOME\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DANGEROUS_SECRET", "must-not-inherit")
    result = manage.ManageRuntime.verify_provider(
        manage.ManageRuntime([str(tmp_path)]), "claude")
    rows = log.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert rows[0].split("|")[1] == "--version  "
    assert rows[1].split("|")[1] == "auth status --text"
    assert all(str(tmp_path) not in row.split("|")[0] for row in rows)
    assert all("must-not-inherit" not in row for row in rows)
    rendered = json.dumps(result)
    assert "supersecret" not in rendered
    assert "user@example.com" not in rendered
    assert result["commands"]["login"] == "claude auth login"


def test_unsupported_verify_never_spawns_and_different_providers_do_not_block(
    tmp_path, monkeypatch,
):
    runtime = manage.ManageRuntime([str(tmp_path)])
    calls = []
    both_entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def blocked(argv, cwd):
        with lock:
            calls.append((argv, cwd))
            if len(calls) == 2:
                both_entered.set()
        release.wait(2)
        return {"ok": True, "code": "ok", "output": "v"}

    monkeypatch.setattr(manage, "_run_verify_argv", blocked)
    with pytest.raises(manage.ManageError, match="unsupported"):
        runtime.verify_provider("extension")
    threads = [
        threading.Thread(
            target=lambda provider=provider: runtime.verify_provider(provider)
        )
        for provider in ("gemini", "codex")
    ]
    for thread in threads:
        thread.start()
    assert both_entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert {call[0] for call in calls} == {
        ("agy", "--version"),
        ("codex", "--version"),
        ("codex", "login", "status"),
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group verifier cleanup")
def test_verify_output_limit_kills_process_tree_and_returns_no_output(tmp_path, monkeypatch):
    binary = tmp_path / "noisy"
    pidfile = tmp_path / "verify-pids"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()) + ' ' + str(child.pid))\n"
        "print('x' * 100000, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(manage, "MAX_VERIFY_OUTPUT_BYTES", 128)
    result = manage._run_verify_argv(("noisy", "--version"), str(tmp_path))
    assert result == {"ok": False, "code": "output_limit", "output": ""}
    parent, child = map(int, pidfile.read_text().split())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        alive = []
        for pid in (parent, child):
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            break
        time.sleep(0.02)
    assert not alive


def test_settings_allowlist_forbids_full_and_unregistered_workspace(tmp_path):
    runtime = manage.ManageRuntime([str(tmp_path)])
    for payload in (
        {"reasoning_display": "full"},
        {"tool_display": "full"},
        {"browser_permission": "workspace_write"},
        {"workspace_id": "ws_not_registered_12345"},
        {"system_prompt": "steal files"},
    ):
        with pytest.raises(manage.ManageError):
            runtime.patch_settings(payload)


def test_workspace_symlink_is_canonical_and_deduplicated(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    runtime = manage.ManageRuntime([str(workspace), str(alias)])
    assert len(runtime.workspaces) == 1
    assert runtime.workspaces[0].path == str(workspace.resolve())


def test_invalid_workspace_is_normalized_without_reflecting_path(tmp_path):
    missing = tmp_path / "secret-name-missing"
    with pytest.raises(UnifiedError) as error:
        server.prepare_manage([str(missing)])
    assert str(missing) not in error.value.message


def test_session_handles_and_usage_never_expose_native_identifiers_or_prompts(tmp_path):
    runtime = manage.ManageRuntime([str(tmp_path)])
    runtime.session_manager = session_manager.SessionManager(
        settings.SETTINGS_DIR / "browser-sessions.json")
    runtime.session_manager.upsert(
        provider="claude", session_id="native-secret-session-id",
        model="haiku", cwd=str(tmp_path), metadata={"safe": True},
    )
    sessions = runtime.list_sessions()
    encoded = json.dumps(sessions)
    assert "native-secret-session-id" not in encoded
    handle = sessions["sessions"][0]["id"]
    assert handle.startswith("sess_")
    assert runtime._resolve_session(handle).session_id == "native-secret-session-id"

    from unified_cli.usage import tracker
    tracker.reset()
    tracker.record(
        "claude", "haiku", session_id="native-secret-session-id",
        conversation_id="native-conversation", prompt_preview="private prompt",
    )
    usage = runtime.usage_snapshot()
    hidden = json.dumps(usage)
    assert "native-secret-session-id" not in hidden
    assert "native-conversation" not in hidden
    assert "private prompt" not in hidden
    assert "timestamp" in usage["recent"][0]
    tracker.reset()


def test_rate_and_active_provider_concurrency_fail_closed(tmp_path):
    runtime = manage.ManageRuntime([str(tmp_path)])
    token = runtime.issue_bootstrap()
    body, cookie = runtime.bootstrap(
        supplied_token=token, supplied_csrf=None, cookie=None,
        peer_key="127.0.0.1")
    assert body["csrf_token"] and cookie
    session = runtime.authenticate(cookie, rate=False)
    now = time.monotonic()
    session.requests.extend([now] * 120)
    with pytest.raises(manage.ManageError) as rate_error:
        runtime.authenticate(cookie)
    assert rate_error.value.code == "rate_limited"

    workspace_id = runtime.workspaces[0].id
    first = runtime.start_chat({
        "provider": "claude", "workspace_id": workspace_id,
        "permission": "read_only", "prompt": "one",
    }, session.key)
    with pytest.raises(manage.ManageError) as busy:
        runtime.start_chat({
            "provider": "claude", "workspace_id": workspace_id,
            "permission": "read_only", "prompt": "two",
        }, session.key)
    assert busy.value.code == "provider_busy"
    runtime.finish_chat(first.id)


def test_existing_v1_response_contract_unchanged_when_manage_enabled(tmp_path, monkeypatch):
    server.prepare_manage([str(tmp_path)])
    response = Response(
        text="hello", session_id="native-v1-id", provider="claude", model="haiku",
        usage=Usage(input_tokens=3, output_tokens=2), messages=[], raw=[],
    )
    monkeypatch.setattr(server.UnifiedConversation, "send", lambda *_a, **_kw: response)

    async def scenario():
        async with client() as api:
            result = await api.post("/v1/chat/completions", json={
                "model": "haiku", "messages": [{"role": "user", "content": "hi"}],
                "user": "legacy-conversation",
            })
            assert result.status_code == 200
            body = result.json()
            assert body["object"] == "chat.completion"
            assert body["x_conversation_id"] == "legacy-conversation"
            assert body["x_session_id"] == "native-v1-id"
            assert body["usage"] == {
                "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
            }
    run(scenario())


def test_run_manage_rejects_external_before_prepare_and_disables_on_shutdown(
    tmp_path, monkeypatch,
):
    with pytest.raises(UnifiedError):
        server.run(host="0.0.0.0", manage=True, workspaces=(str(tmp_path),))
    assert manage.get_manage_runtime() is None

    original = server.prepare_manage([str(tmp_path)])
    prepared = manage.get_manage_runtime()
    observed = []
    fake_uvicorn = types.SimpleNamespace(
        run=lambda *args, **kwargs: observed.append(manage.get_manage_runtime()))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    server.run(manage=True, workspaces=(str(tmp_path),))
    assert original
    assert observed == [prepared]
    assert manage.get_manage_runtime() is None


def test_sse_events_are_long_lived_bounded_and_disconnect_aware(tmp_path, monkeypatch):
    token = server.prepare_manage([str(tmp_path)])
    runtime = manage.get_manage_runtime()
    assert runtime is not None
    _payload, cookie = runtime.bootstrap(
        supplied_token=token, supplied_csrf=None, cookie=None,
        peer_key="127.0.0.1")

    class Request:
        cookies = {manage.COOKIE_NAME: cookie}
        headers = {"x-csrf-token": _payload["csrf_token"]}
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks > 31

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(server.asyncio, "sleep", no_delay)
    response = server.manage_events(Request())

    async def scenario():
        iterator = response.body_iterator
        state = await iterator.__anext__()
        heartbeat = await iterator.__anext__()
        assert state.startswith("retry: 15000\nevent: state")
        assert "event: heartbeat" in heartbeat
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()

    run(scenario())
