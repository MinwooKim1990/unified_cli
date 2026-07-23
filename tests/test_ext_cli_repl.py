"""Explicit CLI/REPL extension selection stays lazy and capability-gated."""

from __future__ import annotations

import builtins
import io
import inspect
import time
from collections import OrderedDict
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from rich.console import Console

from unified_cli import registry
from unified_cli import repl
from unified_cli import repl_completion
from unified_cli import cli
from unified_cli import models as core_models
from unified_cli.base import BaseProvider
from unified_cli.cli import main
from unified_cli.core import Message, ModelInfo, Response, Usage
from unified_cli.errors import UnifiedError
from unified_cli.plugin import ProviderPluginV1, ProviderServerPolicyV1
from unified_cli.repl_state import ReplState
from unified_cli.settings import Settings
from unified_cli.state import SessionState


class _Provider(BaseProvider):
    name = "synthetic"
    default_model = "synthetic-default"
    api_key_env = ""

    def __init__(self, *, model=None, **opts):
        self.model = model or self.default_model
        self.opts = opts

    @classmethod
    def _discover_bin(cls):
        return None

    @classmethod
    def _install_hint(cls):
        return ""

    def _build_args(self, *args, **kwargs):
        return []

    def _normalize(self, obj):
        return iter(())

    def _parse_json_response(self, text, model):
        raise NotImplementedError

    def chat(self, prompt, **kwargs):
        return Response(
            text="synthetic reply",
            session_id="",
            provider=self.name,
            model=self.model,
            usage=Usage(),
            messages=[],
            raw=[],
        )

    def stream(self, prompt, **kwargs):
        yield Message(kind="text", provider=self.name, text="synthetic reply")


class _EntryPoint:
    group = registry.ENTRY_POINT_GROUP

    def __init__(self, name, plugin):
        self.name = name
        self.plugin = plugin
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self.plugin


def _plugin(
    provider_id="synthetic",
    *,
    capabilities=frozenset(("chat", "stream")),
    support_status="experimental",
    factory=None,
    model_lister=None,
    doctor=None,
    server_policy=None,
):
    if factory is None:
        def factory(*, model=None, **opts):
            provider = _Provider(model=model, **opts)
            provider.name = provider_id
            return provider
    plugin_kwargs = {}
    if server_policy is not None:
        plugin_kwargs["server_policy"] = server_policy
    return ProviderPluginV1(
        id=provider_id,
        factory=factory,
        default_model="vendor/default",
        model_lister=model_lister or (lambda: []),
        doctor=doctor or (lambda: None),
        capabilities=capabilities,
        support_status=support_status,
        **plugin_kwargs,
    )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.delenv(registry.DISABLE_PLUGINS_ENV, raising=False)
    registry._reset_provider_registry_for_tests()
    yield
    registry._reset_provider_registry_for_tests()


def _install(monkeypatch, *entries):
    discovery = {"calls": 0}

    def entry_points():
        discovery["calls"] += 1
        return list(entries)

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", entry_points)
    return discovery


def test_exact_descriptor_snapshot_loads_one_entrypoint_and_no_callbacks(monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    def models():
        calls["models"] += 1
        return []

    def doctor():
        calls["doctor"] += 1
        return {"secret": "must not render"}

    chosen = _EntryPoint(
        "synthetic",
        _plugin(factory=factory, model_lister=models, doctor=doctor),
    )
    other = _EntryPoint("other", _plugin("other"))
    discovery = _install(monkeypatch, other, chosen)

    descriptor = registry.snapshot_provider_descriptor("synthetic")

    assert descriptor.id == "synthetic"
    assert descriptor.default_model == "vendor/default"
    assert descriptor.capabilities == frozenset(("chat", "stream"))
    assert descriptor.status == "loaded"
    assert chosen.load_calls == 1
    assert other.load_calls == 0
    assert discovery["calls"] == 1
    assert calls == {"factory": 0, "models": 0, "doctor": 0}
    with pytest.raises(FrozenInstanceError):
        descriptor.id = "changed"  # type: ignore[misc]

    listed = next(
        item for item in registry.list_providers(include_ext=True)
        if item.id == "synthetic"
    )
    assert listed == descriptor
    assert calls == {"factory": 0, "models": 0, "doctor": 0}


def test_descriptor_reconstructs_server_policy_without_aliasing(monkeypatch):
    retained = ProviderServerPolicyV1(
        enabled=True, requires_external_isolation=False
    )
    entry = _EntryPoint(
        "synthetic", _plugin(server_policy=retained)
    )
    _install(monkeypatch, entry)

    descriptor = registry.snapshot_provider_descriptor("synthetic")
    assert descriptor.server_policy is not retained
    assert descriptor.server_policy == retained

    object.__setattr__(retained, "enabled", False)
    object.__setattr__(retained, "requires_external_isolation", True)
    assert descriptor.server_policy == ProviderServerPolicyV1(
        enabled=True, requires_external_isolation=False
    )


def test_exact_descriptor_snapshot_rejects_held_before_callbacks(monkeypatch):
    entry = _EntryPoint(
        "synthetic",
        _plugin(
            capabilities=frozenset(),
            support_status="held",
            factory=lambda **kwargs: pytest.fail("factory must not run"),
            model_lister=lambda: pytest.fail("model lister must not run"),
            doctor=lambda: pytest.fail("doctor must not run"),
        ),
    )
    _install(monkeypatch, entry)

    with pytest.raises(UnifiedError, match="held"):
        registry.snapshot_provider_descriptor("synthetic")
    assert entry.load_calls == 1


@pytest.mark.parametrize("web_flag", [(), ("--no-web-search",)])
def test_chat_explicit_provider_keeps_slash_model_literal_and_omits_core_web(
    monkeypatch, capsys, web_flag,
):
    created = []

    def factory(*, model=None, **opts):
        created.append((model, opts))
        provider = _Provider(model=model, **opts)
        provider.name = "synthetic"
        return provider

    entry = _EntryPoint("synthetic", _plugin(factory=factory))
    _install(monkeypatch, entry)

    assert main([
        "chat", "hello", "--provider", "synthetic",
        "--model", "vendor/family/model", *web_flag,
    ]) == 0

    assert capsys.readouterr().out == "synthetic reply\n"
    assert created and created[0][0] == "vendor/family/model"
    assert "web_search" not in created[0][1]
    assert entry.load_calls == 1


@pytest.mark.parametrize(
    "extra",
    [
        ("--resume", "session-1"),
        ("--image", "image.png"),
    ],
)
def test_chat_extension_capabilities_fail_before_factory(monkeypatch, extra):
    entry = _EntryPoint(
        "synthetic",
        _plugin(
            capabilities=frozenset(("chat",)),
            factory=lambda **kwargs: pytest.fail("factory must not run"),
        ),
    )
    _install(monkeypatch, entry)

    assert main(["chat", "hello", "--provider", "synthetic", *extra]) == 2
    assert entry.load_calls == 1


@pytest.mark.parametrize(
    "selection",
    [
        ("--provider", "synthetic", "--model", "vendor/family/model"),
        ("--model", "synthetic/vendor/family/model"),
    ],
)
def test_legacy_extension_stream_remains_available_without_capability_labels(
    monkeypatch, capsys, selection,
):
    created = []

    def factory(*, model=None, **opts):
        created.append((model, opts))
        provider = _Provider(model=model, **opts)
        provider.name = "synthetic"
        return provider

    entry = _EntryPoint(
        "synthetic",
        _plugin(capabilities=frozenset(), factory=factory),
    )
    _install(monkeypatch, entry)

    assert main(["chat", "hello", "--stream", *selection]) == 0
    assert capsys.readouterr().out == "synthetic reply\n"
    assert created[0][0] == "vendor/family/model"
    assert "web_search" not in created[0][1]


def _exit_repl_immediately(monkeypatch, captured):
    monkeypatch.setattr(repl, "_interactive", lambda: False)
    monkeypatch.setattr(repl, "_setup_readline", lambda: None)
    monkeypatch.setattr(repl, "_banner", lambda current, use_ptk: None)
    monkeypatch.setattr(
        repl,
        "_on_exit",
        lambda conv, current, options=None: captured.update({
            "conv": conv,
            "current": dict(current),
            "options": dict(options or {}),
        }),
    )
    monkeypatch.setattr(
        builtins, "input", lambda prompt: (_ for _ in ()).throw(EOFError)
    )
    monkeypatch.setattr(repl.settings, "load_settings", lambda: Settings())


def test_repl_state_snapshots_round_trip_as_copies(monkeypatch):
    retained_policy = ProviderServerPolicyV1(
        enabled=True, requires_external_isolation=False
    )
    entry = _EntryPoint(
        "synthetic", _plugin(server_policy=retained_policy)
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    supplied = ModelInfo(
        id="vendor/one", provider="synthetic", display_name="One",
        source="plugin",
    )
    state = ReplState(provider="synthetic", model="vendor/one")
    state.remember_extension_descriptor(descriptor)
    state.replace_extension_models("synthetic", [supplied])
    supplied.id = "mutated"

    current = {}
    state.sync_legacy(current)
    mirrored = current["loaded_extension_models"]["synthetic"][0]
    mirrored.id = "also-mutated"
    restored = ReplState.from_legacy(current)
    object.__setattr__(retained_policy, "enabled", False)
    object.__setattr__(retained_policy, "requires_external_isolation", True)

    assert state.extension_models("synthetic")[0].id == "vendor/one"
    assert restored.extension_models("synthetic")[0].id == "also-mutated"
    assert set(restored.loaded_extension_descriptors) == {"synthetic"}
    assert restored.loaded_extension_descriptors[
        "synthetic"
    ].server_policy == ProviderServerPolicyV1(
        enabled=True, requires_external_isolation=False
    )


def test_run_repl_resolves_exact_extension_before_core_default_lookup(
    monkeypatch, tmp_path,
):
    entry = _EntryPoint(
        "synthetic",
        _plugin(
            capabilities=frozenset(),
            factory=lambda **kwargs: pytest.fail("startup must not construct"),
        ),
    )
    other = _EntryPoint("other", _plugin("other"))
    discovery = _install(monkeypatch, other, entry)
    captured = {}
    _exit_repl_immediately(monkeypatch, captured)
    monkeypatch.setattr(
        registry, "list_providers",
        lambda *args, **kwargs: pytest.fail("startup must not enumerate listing"),
    )

    assert repl.run_repl(provider="synthetic", cwd=str(tmp_path)) == 0

    assert captured["current"]["provider"] == "synthetic"
    assert captured["current"]["model"] == "vendor/default"
    assert set(captured["current"]["loaded_extension_descriptors"]) == {
        "synthetic"
    }
    assert captured["conv"].provider_opts == {"cwd": str(tmp_path.resolve())}
    assert entry.load_calls == 1 and other.load_calls == 0
    assert discovery["calls"] == 1


def test_core_repl_startup_never_snapshots_extensions(monkeypatch, tmp_path):
    captured = {}
    _exit_repl_immediately(monkeypatch, captured)
    monkeypatch.setattr(
        registry,
        "snapshot_provider_descriptor",
        lambda provider: pytest.fail("Core startup must not inspect Ext"),
    )

    assert repl.run_repl(provider="claude", cwd=str(tmp_path)) == 0
    assert captured["current"]["provider"] == "claude"


def test_ext_no_web_state_survives_later_core_switch(monkeypatch, tmp_path):
    entry = _EntryPoint("synthetic", _plugin(capabilities=frozenset()))
    _install(monkeypatch, entry)
    captured = {}
    _exit_repl_immediately(monkeypatch, captured)

    assert repl.run_repl(
        provider="synthetic", cwd=str(tmp_path), web_search=False
    ) == 0
    assert captured["conv"].provider_opts == {"cwd": str(tmp_path.resolve())}
    assert captured["current"]["web_search"] is False

    state = ReplState.from_legacy(
        captured["current"], captured["conv"].provider_opts
    )
    assert repl._switch_provider(captured["current"], "codex", state) is True
    state.sync_legacy(captured["current"], captured["conv"].provider_opts)
    assert captured["conv"].provider_opts["web_search"] is False


def test_ext_repl_terse_refuses_before_factory(monkeypatch, tmp_path):
    entry = _EntryPoint(
        "synthetic",
        _plugin(factory=lambda **kwargs: pytest.fail("factory must not run")),
    )
    _install(monkeypatch, entry)

    assert repl.run_repl(
        provider="synthetic", cwd=str(tmp_path), terse=True
    ) == 2
    assert entry.load_calls == 1


def test_extension_resume_requires_sessions_before_state_mutation(monkeypatch):
    entry = _EntryPoint(
        "synthetic", _plugin(capabilities=frozenset())
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    state = ReplState(provider="claude", model="haiku")
    state.remember_extension_descriptor(descriptor)
    conv = type("Conversation", (), {
        "sessions": {}, "turns": [], "_clients": {},
    })()
    current = {"provider": "claude", "model": "haiku"}
    monkeypatch.setattr(
        "unified_cli.state.load_last_session",
        lambda: SessionState(
            provider="synthetic", model="vendor/default",
            session_id="native-session", cwd="", updated_at=1,
        ),
    )

    assert repl._apply_resume(conv, current, {}, repl_state=state) is False
    assert current == {"provider": "claude", "model": "haiku"}
    assert conv.sessions == {} and conv.turns == []


def test_extension_resume_validates_model_before_state_mutation(
    monkeypatch, capsys,
):
    calls = {"factory": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    entry = _EntryPoint(
        "synthetic",
        _plugin(
            capabilities=frozenset(("sessions",)),
            factory=factory,
        ),
    )
    _install(monkeypatch, entry)
    state = ReplState(provider="claude", model="haiku")
    conv = SimpleNamespace(sessions={}, turns=[], _clients={})
    current = {"provider": "claude", "model": "haiku"}
    monkeypatch.setattr(
        "unified_cli.state.load_last_session",
        lambda: SessionState(
            provider="synthetic", model=" padded ",
            session_id="native-session", cwd="", updated_at=1,
        ),
    )

    assert repl._apply_resume(conv, current, {}, repl_state=state) is False
    assert current == {"provider": "claude", "model": "haiku"}
    assert conv.sessions == {} and conv.turns == []
    assert state.provider == "claude" and state.model == "haiku"
    assert state.loaded_extension_descriptors == {}
    assert state.loaded_extension_models == {}
    assert calls["factory"] == 0
    output = capsys.readouterr()
    assert " padded " not in output.out + output.err
    assert len(output.out + output.err) < 512


def test_run_repl_validates_extension_model_before_factory(
    monkeypatch, tmp_path, capsys,
):
    calls = {"factory": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    entry = _EntryPoint(
        "synthetic",
        _plugin(factory=factory),
    )
    _install(monkeypatch, entry)
    monkeypatch.setattr(
        repl.ReplState,
        "from_legacy",
        lambda *args, **kwargs: pytest.fail(
            "invalid initial model must not construct REPL state"
        ),
    )
    invalid_model = " " + ("x" * 10_000)

    assert repl.run_repl(
        provider="synthetic", model=invalid_model, cwd=str(tmp_path)
    ) == 2
    assert calls["factory"] == 0
    output = capsys.readouterr()
    assert invalid_model not in output.out + output.err
    assert len(output.out + output.err) < 512


def _slash_conversation():
    return SimpleNamespace(
        context_window=8,
        provider_opts={},
        provider_opts_by_provider={},
        sessions={},
        turns=[],
        _clients={},
        _locked_provider=None,
    )


def test_repl_provider_and_literal_model_load_metadata_only(monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    def model_lister():
        calls["models"] += 1
        return []

    def doctor():
        calls["doctor"] += 1
        return None

    entry = _EntryPoint(
        "synthetic",
        _plugin(factory=factory, model_lister=model_lister, doctor=doctor),
    )
    other = _EntryPoint("other", _plugin("other"))
    _install(monkeypatch, other, entry)
    conv = _slash_conversation()
    current = {"provider": "claude", "model": "haiku"}
    state = ReplState(provider="claude", model="haiku")

    repl._handle_slash(
        "/provider synthetic", conv, current, {}, [], False,
        repl_state=state,
    )
    repl._handle_slash(
        "/model vendor/family/model", conv, current, {}, [], False,
        repl_state=state,
    )

    assert current["provider"] == "synthetic"
    assert current["model"] == "vendor/family/model"
    assert set(state.loaded_extension_descriptors) == {"synthetic"}
    assert entry.load_calls == 1 and other.load_calls == 0
    assert calls == {"factory": 0, "models": 0, "doctor": 0}


def test_repl_extension_model_refresh_replaces_only_last_good_snapshot(monkeypatch):
    calls = {"models": 0}
    failing = {"value": False}

    def model_lister():
        calls["models"] += 1
        if failing["value"]:
            raise RuntimeError("provider secret")
        return [ModelInfo(
            id="vendor/one", provider="synthetic", display_name="One",
            default=True, source="plugin",
        )]

    entry = _EntryPoint(
        "synthetic", _plugin(model_lister=model_lister)
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    conv = _slash_conversation()
    current = {"provider": "synthetic", "model": "vendor/default"}
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)

    repl._handle_slash(
        "/model --refresh", conv, current, {}, [], False,
        repl_state=state,
    )
    assert calls["models"] == 1
    assert [model.id for model in state.extension_models("synthetic")] == [
        "vendor/one"
    ]

    failing["value"] = True
    repl._handle_slash(
        "/model --refresh", conv, current, {}, [], False,
        repl_state=state,
    )
    assert calls["models"] == 2
    assert [model.id for model in state.extension_models("synthetic")] == [
        "vendor/one"
    ]


def test_repl_extension_model_without_refresh_never_probes(monkeypatch):
    entry = _EntryPoint("synthetic", _plugin())
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    conv = _slash_conversation()
    current = {"provider": "synthetic", "model": "vendor/default"}
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)
    monkeypatch.setattr(
        registry, "list_extension_models",
        lambda provider: pytest.fail("plain /model must not probe"),
    )

    repl._handle_slash(
        "/model", conv, current, {}, [], False, repl_state=state
    )
    assert [model.id for model in state.extension_models("synthetic")] == [
        "vendor/default"
    ]


def test_empty_extension_model_refresh_preserves_descriptor_default(monkeypatch):
    calls = {"models": 0}

    def empty_lister():
        calls["models"] += 1
        return []

    entry = _EntryPoint(
        "synthetic", _plugin(model_lister=empty_lister)
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)

    repl._handle_slash(
        "/model --refresh",
        _slash_conversation(),
        {"provider": "synthetic", "model": "vendor/default"},
        {}, [], False, repl_state=state,
    )
    assert calls["models"] == 1
    models = state.extension_models("synthetic")
    assert [(model.id, model.default, model.source) for model in models] == [
        ("vendor/default", True, "plugin")
    ]


@pytest.mark.parametrize("capabilities", [frozenset(), frozenset(("auth",))])
def test_extension_auth_never_starts_a_process(monkeypatch, capabilities):
    entry = _EntryPoint(
        "synthetic", _plugin(capabilities=capabilities)
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)
    monkeypatch.setattr(
        repl.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Ext auth must not start a process"),
    )

    repl._handle_slash(
        "/auth status synthetic",
        _slash_conversation(),
        {"provider": "synthetic", "model": "vendor/default"},
        {},
        [],
        False,
        repl_state=state,
    )


def test_doctor_is_only_extension_diagnostic_and_never_renders_return(monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    def model_lister():
        calls["models"] += 1
        return []

    def doctor():
        calls["doctor"] += 1
        return {"secret": "ARBITRARY-RETURN-MUST-NOT-RENDER"}

    entry = _EntryPoint(
        "synthetic",
        _plugin(factory=factory, model_lister=model_lister, doctor=doctor),
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)
    conv = _slash_conversation()
    current = {"provider": "synthetic", "model": "vendor/default"}
    target = io.StringIO()
    monkeypatch.setattr(
        repl, "console", Console(file=target, color_system=None, width=120)
    )
    monkeypatch.setattr(
        "unified_cli.ui.collect_states",
        lambda: pytest.fail("Ext /doctor must not collect Core states"),
    )
    monkeypatch.setattr(
        repl, "_live_status", lambda: pytest.fail("REPL status must not probe")
    )
    monkeypatch.setattr(
        "unified_cli.models.list_models",
        lambda *args, **kwargs: pytest.fail("status/settings must not list models"),
    )
    for finder in ("find_claude_bin", "find_codex_bin", "find_agy_bin"):
        monkeypatch.setattr(
            "unified_cli.discovery." + finder,
            lambda: pytest.fail("status/settings must not discover binaries"),
        )
    monkeypatch.setattr(
        repl.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("status/settings must not run processes"),
    )

    for command in ("/status", "/settings"):
        repl._handle_slash(
            command, conv, current, {}, [], False, repl_state=state
        )
    assert calls == {"factory": 0, "models": 0, "doctor": 0}

    repl._handle_slash(
        "/doctor", conv, current, {}, [], False, repl_state=state
    )
    assert calls == {"factory": 0, "models": 0, "doctor": 1}
    rendered = target.getvalue()
    assert "ARBITRARY-RETURN-MUST-NOT-RENDER" not in rendered
    assert "diagnostic completed" in rendered


@pytest.mark.parametrize(
    ("images", "resume"),
    [(True, False), (False, True)],
)
def test_repl_optional_capability_refusal_precedes_stream(
    monkeypatch, images, resume,
):
    entry = _EntryPoint(
        "synthetic", _plugin(capabilities=frozenset())
    )
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    state = ReplState(provider="synthetic", model="vendor/default")
    state.remember_extension_descriptor(descriptor)
    called = []
    turns = [SimpleNamespace(provider="synthetic")] if resume else []
    sessions = {"synthetic": "session"} if resume else {}
    conv = SimpleNamespace(
        stream=lambda *args, **kwargs: called.append((args, kwargs)),
        sessions=sessions,
        turns=turns,
        provider_opts={},
        provider_opts_by_provider={},
        context_window=8,
    )

    repl._run_turn(
        conv,
        {"provider": "synthetic", "model": "vendor/default"},
        "hello",
        images=["image.png"] if images else None,
        repl_state=state,
    )
    assert called == []


def test_completion_uses_only_in_memory_extension_snapshots(monkeypatch):
    entry = _EntryPoint("synthetic", _plugin())
    _install(monkeypatch, entry)
    descriptor = registry.snapshot_provider_descriptor("synthetic")
    current = {
        "provider": "synthetic",
        "model": "vendor/default",
        "loaded_extension_descriptors": {"synthetic": descriptor},
        "loaded_extension_models": {
            "synthetic": (
                ModelInfo(
                    id="vendor/one", provider="synthetic",
                    display_name="One", source="plugin",
                ),
            ),
        },
        "_completion_core_models": {
            provider: tuple(repl_completion.cached_or_hardcoded(provider))
            for provider in ("claude", "codex", "gemini")
        },
    }
    completer = repl_completion.UnifiedCompleter(current)
    monkeypatch.setattr(
        repl_completion,
        "cached_or_hardcoded",
        lambda provider: pytest.fail("completion callback must not read models"),
    )
    monkeypatch.setattr(
        registry,
        "snapshot_provider_descriptor",
        lambda provider: pytest.fail("completion callback must not discover"),
    )
    from prompt_toolkit.document import Document

    models = list(completer.get_completions(Document("/model "), None))
    providers = list(completer.get_completions(Document("/provider "), None))
    assert [item.text for item in models] == ["vendor/one"]
    assert [item.text for item in providers] == [
        "claude", "codex", "gemini", "synthetic",
    ]


def test_build_session_uses_passive_last_good_core_model_snapshots(monkeypatch):
    now = time.monotonic()
    warm = (
        ("gemini-warm", "gemini", "Warm", True, False, "cache"),
    )
    stale = (
        ("gemini-stale", "gemini", "Stale", False, False, "cache"),
    )
    monkeypatch.setattr(
        core_models,
        "_CACHE",
        OrderedDict([
            (("gemini", "last-good"), (now + 60, warm)),
            (("gemini", "newer-but-expired"), (now - 1, stale)),
        ]),
    )

    def unexpected_probe(*args, **kwargs):
        pytest.fail("build_session must use passive in-memory snapshots")

    monkeypatch.setattr(core_models, "_model_context", unexpected_probe)
    monkeypatch.setattr(core_models, "_passive_stat_fields", unexpected_probe)
    monkeypatch.setattr(core_models, "list_models", unexpected_probe)
    monkeypatch.setattr("unified_cli.discovery.find_agy_bin", unexpected_probe)
    monkeypatch.setattr(
        registry.importlib_metadata, "entry_points", unexpected_probe
    )
    monkeypatch.setattr(repl.subprocess, "Popen", unexpected_probe)

    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    current = {"provider": "gemini", "model": "gemini-warm"}
    dummy_input = DummyInput()
    try:
        session = repl_completion.build_session(
            None, current, input=dummy_input, output=DummyOutput()
        )
        assert session is not None
    finally:
        dummy_input.close()

    assert [
        model.id for model in current["_completion_core_models"]["gemini"]
    ] == ["gemini-warm"]


def test_core_model_refresh_updates_existing_completer_snapshot(monkeypatch):
    old = ModelInfo(
        id="old-model", provider="claude", display_name="Old", source="cache"
    )
    new = ModelInfo(
        id="new-model", provider="claude", display_name="New", source="api"
    )
    snapshots = {"claude": (old,)}
    current = {
        "provider": "claude",
        "model": "old-model",
        "_completion_core_models": snapshots,
    }
    state = ReplState(provider="claude", model="old-model")
    completer = repl_completion.UnifiedCompleter(current)
    monkeypatch.setattr(
        core_models,
        "list_models",
        lambda provider, force_refresh=False: [new],
    )

    repl._handle_slash(
        "/model --refresh",
        _slash_conversation(),
        current,
        {},
        [],
        False,
        repl_state=state,
    )

    assert current["_completion_core_models"] is snapshots
    assert snapshots["claude"] == (new,)
    from prompt_toolkit.document import Document

    assert [
        item.text
        for item in completer.get_completions(Document("/model "), None)
    ] == ["new-model"]


def test_plain_core_model_list_creates_only_passive_snapshot(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail("plain Core /model must use passive snapshots")

    monkeypatch.setattr(core_models, "_CACHE", OrderedDict())
    monkeypatch.setattr(core_models, "_model_context", unexpected_probe)
    monkeypatch.setattr(core_models, "_passive_stat_fields", unexpected_probe)
    monkeypatch.setattr(core_models, "list_models", unexpected_probe)
    monkeypatch.setattr("unified_cli.discovery.find_agy_bin", unexpected_probe)
    current = {"provider": "gemini", "model": "gemini-3.5-flash"}
    state = ReplState(provider="gemini", model="gemini-3.5-flash")

    repl._handle_slash(
        "/model",
        _slash_conversation(),
        current,
        {},
        [],
        False,
        repl_state=state,
    )

    snapshot = current["_completion_core_models"]["gemini"]
    assert isinstance(snapshot, tuple)
    assert [model.id for model in snapshot] == [
        "gemini-3.5-flash", "gemini-3.1-pro",
    ]


def test_core_literal_model_uses_passive_known_snapshot(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail("Core literal /model must not probe")

    monkeypatch.setattr(core_models, "_CACHE", OrderedDict())
    monkeypatch.setattr(core_models, "_model_context", unexpected_probe)
    monkeypatch.setattr(core_models, "_passive_stat_fields", unexpected_probe)
    monkeypatch.setattr(core_models, "list_models", unexpected_probe)
    for finder in ("find_claude_bin", "find_codex_bin", "find_agy_bin"):
        monkeypatch.setattr(
            "unified_cli.discovery." + finder, unexpected_probe
        )
    current = {"provider": "claude", "model": "haiku"}
    state = ReplState(provider="claude", model="haiku")

    repl._handle_slash(
        "/model custom-literal",
        _slash_conversation(),
        current,
        {},
        [],
        False,
        repl_state=state,
    )

    assert current["model"] == "custom-literal"
    assert state.model == "custom-literal"
    assert current["_completion_core_models"]["claude"]


def test_oversized_implicit_route_error_is_bounded_and_control_safe(
    capsys,
):
    ordinary_model = "unknown-normal"
    try:
        cli.route(ordinary_model)
    except UnifiedError as error:
        assert cli._bounded_route_error_text(error, ordinary_model) == str(error)
    else:  # pragma: no cover - routing contract guard
        pytest.fail("ordinary unknown model unexpectedly routed")

    oversized = "unknown/" + ("x" * 10_000) + "\x1b[31m"
    assert main(["chat", "hello", "--model", oversized]) == 2
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert len(rendered) < 2_048
    assert "\x1b" not in rendered
    assert oversized not in rendered


def test_loaded_extension_provider_picker_uses_snapshot_without_signature_break(
    monkeypatch,
):
    captured = {}

    class Dialog:
        def run(self):
            return "synthetic"

    def dialog(**kwargs):
        captured.update(kwargs)
        return Dialog()

    monkeypatch.setattr("prompt_toolkit.shortcuts.radiolist_dialog", dialog)
    assert list(inspect.signature(repl_completion.pick_provider).parameters) == []
    assert repl_completion.pick_provider_from_snapshots(["synthetic"]) == "synthetic"
    assert [value for value, _label in captured["values"]] == [
        "claude", "codex", "gemini", "synthetic",
    ]


@pytest.mark.parametrize("surface", ["snapshot", "chat", "repl"])
def test_mutated_nested_policy_is_bounded_before_callbacks(
    monkeypatch, capsys, tmp_path, surface,
):
    calls = {"factory": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    policy = ProviderServerPolicyV1()
    plugin = _plugin(factory=factory, server_policy=policy)
    object.__setattr__(policy, "enabled", "invalid")
    entry = _EntryPoint("synthetic", plugin)
    _install(monkeypatch, entry)

    if surface == "snapshot":
        with pytest.raises(UnifiedError) as error:
            registry.snapshot_provider_descriptor("synthetic")
        assert error.value.kind == "config"
        assert "invalid metadata" in str(error.value)
    elif surface == "chat":
        assert main([
            "chat", "hello", "--provider", "synthetic"
        ]) == 3
    else:
        assert repl.run_repl(
            provider="synthetic", cwd=str(tmp_path)
        ) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert calls["factory"] == 0


@pytest.mark.parametrize("provider", ["UPPER", "bad\ncontrol", "x" * 10_000])
def test_invalid_explicit_provider_ids_are_bounded_before_registry(
    monkeypatch, capsys, tmp_path, provider,
):
    monkeypatch.setattr(
        registry.importlib_metadata,
        "entry_points",
        lambda: pytest.fail("invalid ID must not reach registry discovery"),
    )

    assert main(["chat", "hello", "--provider", provider]) == 2
    cli_output = capsys.readouterr()
    assert cli_output.out == ""
    assert len(cli_output.err) < 512
    assert provider not in cli_output.err

    assert repl.run_repl(provider=provider, cwd=str(tmp_path)) == 2
    repl_output = capsys.readouterr()
    assert len(repl_output.out + repl_output.err) < 512
    assert provider not in repl_output.out + repl_output.err


@pytest.mark.parametrize(
    "model",
    ["x" * 10_000, "bad\ncontrol", " leading", "trailing "],
)
def test_invalid_extension_models_are_bounded_before_factory(
    monkeypatch, capsys, model,
):
    calls = {"factory": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    entry = _EntryPoint("synthetic", _plugin(factory=factory))
    _install(monkeypatch, entry)

    assert main([
        "chat", "hello", "--provider", "synthetic", "--model", model,
    ]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err) < 512
    assert model not in output.err
    assert calls["factory"] == 0


def test_session_resolved_extension_model_is_validated_before_factory(
    monkeypatch,
):
    calls = {"factory": 0}

    def factory(**kwargs):
        calls["factory"] += 1
        return _Provider(**kwargs)

    entry = _EntryPoint("synthetic", _plugin(factory=factory))
    _install(monkeypatch, entry)
    monkeypatch.setattr(
        cli,
        "load_last_session",
        lambda: SessionState(
            provider="synthetic", model=" trailing ",
            session_id="native", cwd="", updated_at=1,
        ),
    )

    assert main(["chat", "hello", "--continue"]) == 2
    assert calls["factory"] == 0


@pytest.mark.parametrize(
    "selection",
    [
        ("--provider", "synthetic"),
        ("--model", "synthetic/vendor/default"),
    ],
)
def test_ext_chat_terse_refuses_before_factory(monkeypatch, selection):
    entry = _EntryPoint(
        "synthetic",
        _plugin(factory=lambda **kwargs: pytest.fail("factory must not run")),
    )
    _install(monkeypatch, entry)

    assert main(["chat", "hello", "--terse", *selection]) == 2
