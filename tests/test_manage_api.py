"""Raw security/contract tests for the opt-in browser manage API."""

from __future__ import annotations

import asyncio
import builtins
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
from unified_cli.core import ModelInfo, Response, Usage  # noqa: E402
from unified_cli.errors import UnifiedError  # noqa: E402
from unified_cli.plugin import ProviderServerPolicyV1  # noqa: E402
from unified_cli.registry import ProviderDescriptorV1  # noqa: E402


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


def extension_snapshot(
    provider_id="preview-ext", *, support_status="preview",
    default_model="preview-model", capabilities=frozenset(("chat", "models")),
):
    return ProviderDescriptorV1(
        id=provider_id,
        source="extension",
        status="loaded",
        support_status=support_status,
        default_model=default_model,
        capabilities=capabilities,
        route_prefixes=(provider_id,),
        server_policy=ProviderServerPolicyV1(
            enabled=True, requires_external_isolation=False,
        ),
    )


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


def test_bootstrap_one_time_replay_cookie_scope_and_safe_metadata(tmp_path, monkeypatch):
    import_extension_calls = []
    metadata_calls = []
    original_import = builtins.__import__

    def reject_extension_import(name, *args, **kwargs):
        if name == "unified_cli_ext" or name.startswith("unified_cli_ext."):
            import_extension_calls.append(name)
            raise AssertionError("bootstrap imported the Ext namespace")
        return original_import(name, *args, **kwargs)

    def reject_ext_distribution(name):
        metadata_calls.append(name)
        raise AssertionError("bootstrap queried distribution metadata")

    for module_name in tuple(sys.modules):
        if module_name == "unified_cli_ext" or module_name.startswith("unified_cli_ext."):
            monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.setattr(builtins, "__import__", reject_extension_import)
    monkeypatch.setattr(manage.importlib_metadata, "version", reject_ext_distribution)
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as first:
            exchanged = await bootstrap(first, token)
            assert exchanged.status_code == 200
            body = exchanged.json()
            assert body["mode"] == "manage"
            assert body["authenticated"] is True
            assert body["csrf_token"]
            assert body["versions"] == {
                "unified_cli": "0.5.3",
                "unified_cli_ext": "0.5.3",
            }
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
    assert import_extension_calls == []
    assert metadata_calls == []


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


def test_bootstrap_lists_bundled_entry_point_metadata_without_provider_probes(
    tmp_path, monkeypatch,
):
    calls = {"entry_points": 0, "models": 0, "popen": 0, "load": 0}

    class EntryPoint:
        name = "safeext"
        group = "unified_cli.providers.v1"

        def load(self):
            calls["load"] += 1
            raise AssertionError("plugin imported")

    import unified_cli.registry as registry
    registry._reset_provider_registry_for_tests()
    def forbidden_discovery():
        calls["entry_points"] += 1
        raise AssertionError("entry points enumerated")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden_discovery)
    monkeypatch.setattr(manage, "list_models", lambda *_a, **_kw: calls.__setitem__("models", calls["models"] + 1))
    monkeypatch.setattr(manage.subprocess, "Popen", lambda *_a, **_kw: calls.__setitem__("popen", calls["popen"] + 1))
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            response = await bootstrap(api, token)
            assert response.status_code == 200
            rows = response.json()["providers"]
            assert {row["id"] for row in rows} == {
                "claude", "codex", "gemini",
                "grok", "kimi", "copilot", "cursor", "codebuddy", "qoder",
                "mistral-vibe", "qwen", "cline", "opencode", "kilo",
                "droid", "pi", "oh-my-pi", "hermes", "poolside", "amp",
                "gitlab-duo",
            }
            extensions = [row for row in rows if row["source"] == "extension"]
            assert len(extensions) == 18
            assert all(row["status"] == "discovered" for row in extensions)
            assert all(row["support_status"] == "preview" for row in extensions)
            assert all(row["models_supported"] is True for row in extensions)
            assert {
                row["id"] for row in extensions if row["chat_supported"]
            } == manage._BROWSER_SAFE_EXTENSION_PROVIDER_IDS
    run(scenario())
    assert calls == {"entry_points": 0, "models": 0, "popen": 0, "load": 0}


def test_bundled_ext_explicit_models_and_doctor_reuse_python_contracts(
    tmp_path, monkeypatch,
):
    calls = []

    def models(provider, **kwargs):
        calls.append(("models", provider, kwargs))
        return [
            ModelInfo(
                id="official-preview-model",
                provider=provider,
                default=True,
                source="plugin",
            )
        ]

    def doctor(provider):
        calls.append(("doctor", provider))
        return {
            "id": provider,
            "available": True,
            "status": "Preview",
            "version": "1.2.3",
        }

    def snapshot(provider):
        calls.append(("snapshot", provider))
        return extension_snapshot(
            provider_id=provider,
            default_model="official-preview-model",
            capabilities=frozenset(("chat", "stream", "sessions")),
        )

    monkeypatch.setattr(manage, "list_models", models)
    monkeypatch.setattr(manage, "doctor_provider", doctor)
    monkeypatch.setattr(manage, "snapshot_provider_descriptor", snapshot)
    token = server.prepare_manage([str(tmp_path)])

    async def scenario():
        async with client() as api:
            exchanged = await bootstrap(api, token)
            csrf = exchanged.json()["csrf_token"]
            headers = mutation_headers(csrf)
            first = await api.post(
                "/api/ui/v1/providers/grok/models",
                json={"force_refresh": False},
                headers=headers,
            )
            second = await api.post(
                "/api/ui/v1/providers/grok/models",
                json={"force_refresh": False},
                headers=headers,
            )
            verified = await api.post(
                "/api/ui/v1/providers/grok/verify",
                json={},
                headers=headers,
            )
            assert first.status_code == second.status_code == verified.status_code == 200
            return first.json(), second.json(), verified.json()

    first, second, verified = run(scenario())
    assert first["models"] == [{
        "id": "official-preview-model",
        "display_name": "",
        "default": True,
        "deprecated": False,
        "source": "plugin",
    }]
    assert first["cache"]["cached"] is False
    assert second["cache"]["cached"] is True
    assert second["cache"]["age_seconds"] >= 0
    assert verified == {
        "provider": "grok",
        "installed": True,
        "ready": True,
        "status": "ready",
        "auth": "unknown",
        "version": "1.2.3",
        "support_status": "preview",
        "checks": [{
            "check": "provider_doctor",
            "ok": True,
            "code": "ok",
            "output": "",
        }],
    }
    runtime = manage.get_manage_runtime()
    assert runtime is not None
    chat = runtime.start_chat({
        "provider": "grok",
        "workspace_id": runtime.workspaces[0].id,
        "permission": "read_only",
        "prompt": "hello",
    }, "owner")
    assert chat.provider == "grok"
    assert chat.model == "official-preview-model"
    runtime.finish_chat(chat.id)
    assert calls == [
        ("models", "grok", {}),
        ("doctor", "grok"),
        ("snapshot", "grok"),
    ]


def test_injected_extension_snapshot_is_copied_bounded_metadata_only(
    tmp_path,
):
    hostile_model = "preview-model </script><img src=x onerror=alert(1)>"
    descriptor = extension_snapshot(default_model=hostile_model)
    token = server.prepare_manage(
        [str(tmp_path)], provider_snapshots={"preview-ext": descriptor},
    )
    object.__setattr__(descriptor, "default_model", "mutated-secret-model")
    object.__setattr__(descriptor, "capabilities", frozenset(("factory",)))

    async def scenario():
        async with client() as api:
            response = await bootstrap(api, token)
            assert response.status_code == 200
            rows = response.json()["providers"]
            extension = next(row for row in rows if row["id"] == "preview-ext")
            assert extension == {
                "id": "preview-ext",
                "source": "extension",
                "status": "loaded",
                "support_status": "preview",
                "default_model": hostile_model,
                "capabilities": ["chat", "models"],
                "server_policy": {
                    "enabled": False,
                    "requires_external_isolation": True,
                },
                "chat_supported": False,
                "verify_supported": False,
                "models_supported": False,
                "default_supported": False,
                "metadata_only": True,
            }
            assert "mutated-secret-model" not in response.text
            assert all(key not in extension for key in (
                "commands", "install_command", "login_command", "error",
                "route_prefixes", "receipt", "token", "path", "secret",
            ))

    run(scenario())


def test_held_extension_snapshot_has_no_model_capability_or_action(tmp_path):
    held = extension_snapshot(
        provider_id="held-ext",
        support_status="held",
        default_model="must-not-render",
        capabilities=frozenset(("chat", "models", "server")),
    )
    runtime = manage.ManageRuntime([str(tmp_path)], provider_snapshots=[held])
    extension = next(
        row for row in runtime.provider_metadata()["providers"]
        if row["id"] == "held-ext"
    )
    assert extension["support_status"] == "held"
    assert extension["default_model"] is None
    assert extension["capabilities"] == []
    assert extension["server_policy"] == {
        "enabled": False, "requires_external_isolation": True,
    }
    assert extension["chat_supported"] is False
    assert extension["verify_supported"] is False
    assert extension["models_supported"] is False


def test_extension_snapshot_validation_has_no_hostile_equality_callbacks(tmp_path):
    calls = []

    class Boom:
        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("provider field equality executed")

        def __ne__(self, _other):
            calls.append("ne")
            raise AssertionError("provider field inequality executed")

    forged = object.__new__(ProviderDescriptorV1)
    object.__setattr__(forged, "id", "forged-ext")
    object.__setattr__(forged, "source", Boom())
    object.__setattr__(forged, "status", "loaded")
    object.__setattr__(forged, "default_model", "model")
    object.__setattr__(forged, "capabilities", frozenset())
    object.__setattr__(forged, "route_prefixes", ("forged-ext",))
    object.__setattr__(forged, "server_policy", None)
    object.__setattr__(forged, "error", None)
    object.__setattr__(forged, "support_status", "preview")

    with pytest.raises(ValueError, match="snapshot is invalid"):
        manage.ManageRuntime([str(tmp_path)], provider_snapshots=[forged])
    assert calls == []


def test_extension_snapshot_container_bounds_duplicates_and_callback_boundary(
    tmp_path, monkeypatch,
):
    with pytest.raises(ValueError, match="exceed the limit"):
        manage.ManageRuntime(
            [str(tmp_path)],
            provider_snapshots=[
                extension_snapshot(provider_id="ext-{}".format(index))
                for index in range(manage.MAX_EXTENSION_PROVIDER_SNAPSHOTS + 1)
            ],
        )
    with pytest.raises(ValueError, match="duplicate"):
        manage.ManageRuntime(
            [str(tmp_path)],
            provider_snapshots=[extension_snapshot(), extension_snapshot()],
        )
    broken = extension_snapshot(provider_id="broken-ext")
    object.__setattr__(broken, "status", "broken")
    with pytest.raises(ValueError, match="snapshot is invalid"):
        manage.ManageRuntime([str(tmp_path)], provider_snapshots=[broken])
    with pytest.raises(ValueError, match="mapping is invalid"):
        manage.ManageRuntime(
            [str(tmp_path)],
            provider_snapshots={"other-ext": extension_snapshot()},
        )
    with pytest.raises(ValueError, match="list, tuple, or dict"):
        manage.ManageRuntime(
            [str(tmp_path)], provider_snapshots=(item for item in ()),
        )

    monkeypatch.setattr(
        manage,
        "_copy_extension_provider_snapshot",
        lambda _value: (_ for _ in ()).throw(SystemExit("secret")),
    )
    with pytest.raises(ValueError, match="snapshot is invalid") as error:
        manage.ManageRuntime(
            [str(tmp_path)], provider_snapshots=[extension_snapshot()],
        )
    assert "secret" not in str(error.value)


def test_prepare_and_ensure_forward_snapshots_without_reprocessing_existing_runtime(
    tmp_path,
):
    first_token = server.prepare_manage(
        [str(tmp_path)], provider_snapshots=[extension_snapshot()],
    )
    assert first_token
    first = manage.get_manage_runtime()
    assert first is not None
    assert any(
        row["id"] == "preview-ext"
        for row in first.provider_metadata()["providers"]
    )
    # ensure_manage must not touch a new snapshot argument while a runtime is
    # already installed.
    assert manage.ensure_manage(
        [str(tmp_path)], provider_snapshots=object(),
    ) is None

    second_token = server.prepare_manage(
        [str(tmp_path)],
        provider_snapshots=[extension_snapshot(provider_id="second-ext")],
    )
    assert second_token and second_token != first_token
    second = manage.get_manage_runtime()
    assert second is not None and second is not first
    assert any(
        row["id"] == "second-ext"
        for row in second.provider_metadata()["providers"]
    )
    with pytest.raises(manage.ManageError) as old_error:
        first.verify_provider("preview-ext")
    assert old_error.value.code == "provider_unsupported"


def test_extension_browser_operations_are_stable_403_without_callbacks(
    tmp_path, monkeypatch,
):
    calls = {"models": 0, "verify": 0, "conversation": 0, "popen": 0}

    def forbidden(name):
        def invoke(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError("extension callback executed")
        return invoke

    monkeypatch.setattr(manage, "list_models", forbidden("models"))
    monkeypatch.setattr(manage, "_run_verify_argv", forbidden("verify"))
    monkeypatch.setattr(manage, "UnifiedConversation", forbidden("conversation"))
    monkeypatch.setattr(manage.subprocess, "Popen", forbidden("popen"))
    token = server.prepare_manage(
        [str(tmp_path)], provider_snapshots=[extension_snapshot()],
    )

    async def scenario():
        async with client() as api:
            exchanged = await bootstrap(api, token)
            csrf = exchanged.json()["csrf_token"]
            workspace_id = exchanged.json()["workspaces"][0]["id"]
            headers = mutation_headers(csrf)
            responses = [
                await api.post(
                    "/api/ui/v1/providers/preview-ext/verify",
                    json={}, headers=headers,
                ),
                await api.post(
                    "/api/ui/v1/providers/preview-ext/models",
                    json={}, headers=headers,
                ),
                await api.post(
                    "/api/ui/v1/chat",
                    json={
                        "provider": "preview-ext",
                        "workspace_id": workspace_id,
                        "permission": "read_only",
                        "prompt": "hello",
                    },
                    headers=headers,
                ),
            ]
            assert [response.status_code for response in responses] == [403, 403, 403]
            assert [response.json()["error"]["code"] for response in responses] == [
                "provider_unsupported", "provider_unsupported", "provider_unsupported",
            ]

    run(scenario())
    assert calls == {"models": 0, "verify": 0, "conversation": 0, "popen": 0}


@pytest.mark.parametrize("provider_id", ["bad\x00id", "x" * 10_000])
def test_hostile_provider_operation_ids_fail_with_bounded_errors(
    tmp_path, monkeypatch, provider_id,
):
    runtime = manage.ManageRuntime([str(tmp_path)])
    calls = []
    monkeypatch.setattr(
        manage, "_run_verify_argv",
        lambda *_args, **_kwargs: calls.append("verify"),
    )
    monkeypatch.setattr(
        manage, "list_models",
        lambda *_args, **_kwargs: calls.append("models"),
    )
    operations = (
        (lambda: runtime.verify_provider(provider_id), "verify_unsupported"),
        (lambda: runtime.provider_models(provider_id), "provider_unsupported"),
        (lambda: runtime.start_chat({"provider": provider_id}, "owner"), "provider_forbidden"),
    )
    for operation, code in operations:
        with pytest.raises(manage.ManageError) as error:
            operation()
        assert error.value.status_code == 403
        assert error.value.code == code
        assert len(error.value.message) <= 100
        assert provider_id not in error.value.message
    assert calls == []


def test_disabled_old_runtime_cannot_start_core_or_mutate_local_state(
    tmp_path, monkeypatch,
):
    runtime = manage.ManageRuntime(
        [str(tmp_path)], provider_snapshots=[extension_snapshot()],
    )
    token = runtime.issue_bootstrap()
    _payload, cookie = runtime.bootstrap(
        supplied_token=token, supplied_csrf=None, cookie=None,
        peer_key="127.0.0.1",
    )
    owner = runtime.authenticate(cookie, rate=False)
    runtime.session_manager = session_manager.SessionManager(
        settings.SETTINGS_DIR / "disabled-sessions.json"
    )
    runtime.session_manager.upsert(
        provider="preview-ext", session_id="native-ext-session",
        model="preview-model", cwd=str(tmp_path), name="before",
    )
    handle = runtime.list_sessions()["sessions"][0]["id"]
    chat = runtime.start_chat({
        "provider": "claude",
        "workspace_id": runtime.workspaces[0].id,
        "permission": "read_only",
        "prompt": "hello",
    }, owner.key)
    callbacks = []
    writes = []
    monkeypatch.setattr(
        runtime, "_conversation_for_chat",
        lambda _chat: callbacks.append("conversation") or pytest.fail(
            "disabled runtime created a provider conversation"
        ),
    )
    monkeypatch.setattr(
        manage, "_decode_data_image",
        lambda _value: callbacks.append("image") or pytest.fail(
            "disabled runtime decoded an image"
        ),
    )
    monkeypatch.setattr(
        manage, "save_settings", lambda _settings: writes.append("settings"),
    )

    runtime.disable()
    for operation in (
        runtime.provider_metadata,
        runtime.list_sessions,
        runtime.usage_snapshot,
        runtime.issue_bootstrap,
        lambda: runtime.bootstrap(
            supplied_token=None, supplied_csrf=owner.csrf_token,
            cookie=cookie, peer_key="127.0.0.1",
        ),
        lambda: runtime.patch_settings({"theme": "dark"}),
        lambda: runtime.patch_session(handle, {"name": "after"}),
        lambda: runtime.delete_session(handle),
        lambda: runtime.cancel_chat(chat.id, owner.key),
        lambda: runtime.start_chat({
            "provider": "claude",
            "workspace_id": runtime.workspaces[0].id,
            "permission": "read_only",
            "prompt": "again",
            "images": ["data:image/png;base64,ignored"],
        }, owner.key),
        lambda: next(runtime.stream_chat(chat)),
    ):
        with pytest.raises(manage.ManageError) as error:
            operation()
        assert error.value.status_code == 503
        assert error.value.code == "manage_disabled"
    assert callbacks == []
    assert writes == []
    assert runtime.session_manager.list(include_archived=True)[0].name == "before"

    for operation in (
        lambda: runtime.verify_provider("preview-ext"),
        lambda: runtime.provider_models("preview-ext"),
        lambda: runtime.start_chat({"provider": "preview-ext"}, owner.key),
    ):
        with pytest.raises(manage.ManageError) as error:
            operation()
        assert error.value.status_code == 403
        assert error.value.code == "provider_unsupported"


def test_session_patch_validates_all_fields_before_rename(tmp_path):
    runtime = manage.ManageRuntime([str(tmp_path)])
    runtime.session_manager = session_manager.SessionManager(
        settings.SETTINGS_DIR / "atomic-sessions.json"
    )
    runtime.session_manager.upsert(
        provider="preview-ext", session_id="native-ext-session",
        model="preview-model", cwd=str(tmp_path), name="before",
    )
    handle = runtime.list_sessions()["sessions"][0]["id"]
    with pytest.raises(manage.ManageError) as error:
        runtime.patch_session(handle, {"name": "after", "archived": "yes"})
    assert error.value.code == "invalid_session"
    record = runtime.session_manager.list(include_archived=True)[0]
    assert record.name == "before"
    assert record.archived is False


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
            assert allowed.json() == {
                "provider": "claude",
                "cache": {
                    "cached": False,
                    "age_seconds": 0,
                    "ttl_seconds": 60,
                },
                "fallback": False,
                "models": [],
            }
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
