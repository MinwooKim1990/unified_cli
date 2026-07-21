"""Stage 1 provider plugin ABI tests using inert fake entry points only."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import unified_cli
from unified_cli import (
    PROVIDERS,
    ProviderPluginV1,
    ProviderServerPolicyV1,
    UnifiedError,
    create,
    doctor_provider,
    list_models,
    list_providers,
    load_provider_plugin,
    route,
)
from unified_cli.base import BaseProvider
from unified_cli.core import ModelInfo
from unified_cli.errors import classify
from unified_cli.registry import _reset_provider_registry_for_tests
from unified_cli import registry


class DummyProvider(BaseProvider):
    name = "dummy"
    default_model = "dummy-default"
    api_key_env = "DUMMY_API_KEY"

    def __init__(self, *, model=None, **opts):
        self.model = model or self.default_model
        self.opts = opts

    @classmethod
    def _discover_bin(cls):
        return "/unused"

    @classmethod
    def _install_hint(cls):
        return "unused"

    def _build_args(self, *args, **kwargs):
        return []

    def _normalize(self, obj):
        return iter(())

    def _parse_json_response(self, raw):
        raise NotImplementedError


class FakeEntryPoint:
    group = registry.ENTRY_POINT_GROUP

    def __init__(self, name, loaded=None, error=None):
        self.name = name
        self._loaded = loaded
        self._error = error
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if self._error is not None:
            raise self._error
        return self._loaded


def _plugin(provider_id="acme", *, factory=None, **kw):
    if factory is None:
        def factory(*, model=None, **opts):
            provider = DummyProvider(model=model, **opts)
            provider.name = provider_id
            return provider
    return ProviderPluginV1(
        id=provider_id,
        factory=factory,
        default_model="acme-default",
        model_lister=lambda: [],
        doctor=lambda: {"ok": True},
        capabilities=frozenset({"chat", "stream"}),
        route_prefixes=(provider_id,),
        server_policy=ProviderServerPolicyV1(enabled=True),
        **kw,
    )


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    monkeypatch.delenv(registry.DISABLE_PLUGINS_ENV, raising=False)
    _reset_provider_registry_for_tests()
    yield
    _reset_provider_registry_for_tests()


def _set_entry_points(monkeypatch, entries):
    calls = {"n": 0}

    def entry_points():
        calls["n"] += 1
        return list(entries)

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", entry_points)
    return calls


def test_public_builtins_and_backwards_imports_remain_exact(monkeypatch):
    calls = _set_entry_points(monkeypatch, [])

    assert tuple(PROVIDERS) == ("claude", "codex", "gemini")
    assert set(PROVIDERS) == {"claude", "codex", "gemini"}
    assert unified_cli.ProviderName is not None
    assert unified_cli.ProviderId is str
    assert unified_cli.ProviderSupportStatusV1 is not None
    assert unified_cli.ClaudeProvider is PROVIDERS["claude"]
    descriptors = list_providers()
    assert [item.id for item in descriptors] == ["claude", "codex", "gemini"]
    assert all(item.status == "builtin" for item in descriptors)
    assert all(item.lifecycle_status == "builtin" for item in descriptors)
    assert all(item.support_status == "stable" for item in descriptors)
    assert calls["n"] == 0


def test_core_create_route_help_version_and_listing_do_not_discover(monkeypatch, capsys):
    def forbidden():
        raise AssertionError("entry points touched on core fast path")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden)
    provider = create("claude", bin_path="/bin/echo")
    assert provider.name == "claude"
    assert route("haiku") == ("claude", "haiku")
    assert route("codex/gpt-5.4-mini") == ("codex", "gpt-5.4-mini")
    assert len(list_providers()) == 3

    from unified_cli.cli import main
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == unified_cli.__version__
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_extension_listing_is_metadata_only_and_does_not_load_slow_plugin(monkeypatch):
    class NeverLoad(FakeEntryPoint):
        def load(self):
            raise AssertionError("metadata listing imported plugin code")

    ep = NeverLoad("slow")
    calls = _set_entry_points(monkeypatch, [ep])

    descriptors = list_providers(include_ext=True)

    assert [(d.id, d.status) for d in descriptors][-1] == (
        "slow", "discovered",
    )
    assert descriptors[-1].lifecycle_status == "discovered"
    assert descriptors[-1].support_status == "unknown"
    assert calls["n"] == 1
    assert ep.load_calls == 0


def test_explicit_route_is_metadata_only_and_create_loads_only_requested(monkeypatch):
    acme_ep = FakeEntryPoint("acme", _plugin("acme"))
    other_ep = FakeEntryPoint("other", _plugin("other"))
    calls = _set_entry_points(monkeypatch, [other_ep, acme_ep])

    assert route("acme/model-x") == ("acme", "model-x")
    assert acme_ep.load_calls == other_ep.load_calls == 0

    provider = create("acme", web_search=False)
    assert provider.name == "acme"
    assert provider.model == "acme-default"
    assert provider.opts == {"web_search": False}
    assert acme_ep.load_calls == 1
    assert other_ep.load_calls == 0

    assert create("acme", model="chosen").model == "chosen"
    assert acme_ep.load_calls == 1
    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-custom/path", "claude"),
        ("gpt-custom/path", "codex"),
        ("gemini-custom/path", "gemini"),
    ],
)
def test_slash_core_inference_stays_zero_discovery_when_plugins_disabled(
    monkeypatch, model, expected,
):
    monkeypatch.setenv(registry.DISABLE_PLUGINS_ENV, "1")

    def forbidden():
        raise AssertionError("entry points touched for a Core model")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden)
    assert route(model) == (expected, model)


def test_unprefixed_unknown_model_never_discovers_or_auto_infers_extension(monkeypatch):
    def forbidden():
        raise AssertionError("extension inference attempted")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden)
    with pytest.raises(UnifiedError) as exc:
        route("acme-model")
    assert exc.value.kind == "config"


@pytest.mark.parametrize(
    ("entries", "provider_id", "safe_text"),
    [
        ([FakeEntryPoint("dupe", _plugin("dupe")),
          FakeEntryPoint("dupe", _plugin("dupe"))], "dupe", "ambiguous"),
        ([FakeEntryPoint("agy", _plugin("agy"))], "agy", "reserved"),
        ([FakeEntryPoint("bad", SimpleNamespace(abi_version=99))], "bad", "unsupported ABI"),
        ([FakeEntryPoint("mismatch", _plugin("different"))], "mismatch", "invalid metadata"),
    ],
)
def test_duplicate_reserved_bad_abi_and_id_are_safe_and_isolated(
    monkeypatch, entries, provider_id, safe_text,
):
    _set_entry_points(monkeypatch, entries)

    with pytest.raises(UnifiedError) as exc:
        create(provider_id)
    assert safe_text in str(exc.value)
    assert set(PROVIDERS) == {"claude", "codex", "gemini"}
    assert create("codex", bin_path="/bin/echo").name == "codex"


def test_broken_import_and_factory_never_leak_plugin_exception_text(monkeypatch):
    secret = "token=super-secret-value"
    broken = FakeEntryPoint("broken", error=RuntimeError(secret))
    bad_factory = FakeEntryPoint(
        "badfactory", _plugin("badfactory", factory=lambda **kw: object()),
    )
    _set_entry_points(monkeypatch, [broken, bad_factory])

    for provider_id in ("broken", "badfactory"):
        with pytest.raises(UnifiedError) as exc:
            create(provider_id)
        rendered = str(exc.value)
        assert provider_id in rendered
        assert secret not in rendered
        assert "super-secret" not in exc.value.message
        formatted = "".join(traceback.format_exception(
            type(exc.value), exc.value, exc.value.__traceback__,
        ))
        assert secret not in formatted
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None


def test_disable_env_prevents_discovery_loading_but_not_core(monkeypatch):
    monkeypatch.setenv(registry.DISABLE_PLUGINS_ENV, "1")

    def forbidden():
        raise AssertionError("disabled registry performed discovery")

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", forbidden)
    assert create("claude", bin_path="/bin/echo").name == "claude"
    assert route("gpt-5.4-mini") == ("codex", "gpt-5.4-mini")
    assert len(list_providers(include_ext=True)) == 3
    with pytest.raises(UnifiedError, match="disabled"):
        create("acme")
    with pytest.raises(UnifiedError, match="disabled"):
        route("acme/model")


def test_plugin_metadata_is_immutable_and_normalized():
    plugin = ProviderPluginV1(
        id="acme",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=lambda: [],
        doctor=lambda: None,
        capabilities=["chat", "stream"],  # type: ignore[arg-type]
        route_prefixes=["acme"],  # type: ignore[arg-type]
    )
    assert plugin.capabilities == frozenset({"chat", "stream"})
    assert plugin.route_prefixes == ("acme",)
    assert plugin.support_status == "experimental"
    with pytest.raises(FrozenInstanceError):
        plugin.id = "changed"  # type: ignore[misc]

    implicit_prefix = ProviderPluginV1(
        id="implicit",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=lambda: [],
        doctor=lambda: None,
    )
    assert implicit_prefix.route_prefixes == ("implicit",)
    with pytest.raises(ValueError, match="only the plugin id"):
        ProviderPluginV1(
            id="acme",
            factory=lambda **kw: DummyProvider(**kw),
            default_model="m",
            model_lister=lambda: [],
            doctor=lambda: None,
            route_prefixes=("alias",),
        )
    with pytest.raises(ValueError, match="invalid provider plugin id"):
        _plugin("gpt-addon")

    positional = ProviderPluginV1(
        "positional",
        lambda **kw: DummyProvider(**kw),
        "m",
        lambda: [],
        lambda: None,
        frozenset(),
        ("positional",),
        ProviderServerPolicyV1(),
        1,
    )
    assert positional.support_status == "experimental"


@pytest.mark.parametrize(
    "support_status",
    ["Held", "unknown", "held\n", 1, None],
)
def test_plugin_support_status_rejects_malformed_values_locally(support_status):
    with pytest.raises(ValueError, match="invalid provider support status"):
        ProviderPluginV1(
            id="bad-support",
            factory=lambda **kw: DummyProvider(**kw),
            default_model="m",
            model_lister=lambda: [],
            doctor=lambda: None,
            support_status=support_status,  # type: ignore[arg-type]
        )

    assert create("claude", bin_path="/bin/echo").name == "claude"


def test_held_plugin_cannot_advertise_executable_capabilities():
    with pytest.raises(ValueError, match="cannot advertise capabilities"):
        _plugin("held-capabilities", support_status="held")


def test_loading_is_thread_safe_and_cached(monkeypatch):
    started = threading.Event()

    class SlowEntryPoint(FakeEntryPoint):
        def load(self):
            self.load_calls += 1
            started.set()
            time.sleep(0.05)
            return self._loaded

    ep = SlowEntryPoint("threaded", _plugin("threaded"))
    calls = _set_entry_points(monkeypatch, [ep])

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(load_provider_plugin, "threaded") for _ in range(8)]
        assert started.wait(1)
        plugins = [future.result(timeout=2) for future in futures]

    assert all(plugin is plugins[0] for plugin in plugins)
    assert ep.load_calls == 1
    assert calls["n"] == 1


def test_same_thread_reentrant_entry_point_load_fails_without_deadlock(monkeypatch):
    class ReentrantEntryPoint(FakeEntryPoint):
        def load(self):
            self.load_calls += 1
            return create("recursive")

    ep = ReentrantEntryPoint("recursive")
    _set_entry_points(monkeypatch, [ep])
    errors = []

    def invoke():
        try:
            create("recursive")
        except BaseException as exc:  # captured only for the daemon test thread
            errors.append(exc)

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive(), "re-entrant plugin load deadlocked"
    assert len(errors) == 1
    assert isinstance(errors[0], UnifiedError)
    assert ep.load_calls == 1


def test_cross_thread_nested_provider_load_cycle_fails_without_deadlock(monkeypatch):
    barrier = threading.Barrier(2)

    class CyclicEntryPoint(FakeEntryPoint):
        def __init__(self, name, other):
            super().__init__(name)
            self.other = other

        def load(self):
            self.load_calls += 1
            barrier.wait(timeout=1)
            return create(self.other)

    first = CyclicEntryPoint("cycle-a", "cycle-b")
    second = CyclicEntryPoint("cycle-b", "cycle-a")
    _set_entry_points(monkeypatch, [first, second])
    errors = []

    def invoke(provider_id):
        try:
            create(provider_id)
        except BaseException as exc:  # captured only for daemon test threads
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=("cycle-a",), daemon=True),
        threading.Thread(target=invoke, args=("cycle-b",), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 2
    assert all(isinstance(error, UnifiedError) for error in errors)


def test_registry_cache_can_be_reset_and_supports_legacy_metadata_shape(monkeypatch):
    ep = FakeEntryPoint("legacy", _plugin("legacy"))
    calls = {"n": 0}

    def entry_points():
        calls["n"] += 1
        return {registry.ENTRY_POINT_GROUP: [ep], "other": []}

    monkeypatch.setattr(registry.importlib_metadata, "entry_points", entry_points)
    assert route("legacy/m") == ("legacy", "m")
    assert route("legacy/m2") == ("legacy", "m2")
    assert calls["n"] == 1
    _reset_provider_registry_for_tests()
    assert route("legacy/m3") == ("legacy", "m3")
    assert calls["n"] == 2


def test_modern_select_metadata_shape_filters_the_entry_point_group(monkeypatch):
    wanted = FakeEntryPoint("modern", _plugin("modern"))
    unrelated = FakeEntryPoint("ignored", _plugin("ignored"))
    unrelated.group = "some.other.group"

    class SelectableEntryPoints(tuple):
        def select(self, **params):
            return tuple(
                item for item in self
                if item.group == params.get("group")
            )

    monkeypatch.setattr(
        registry.importlib_metadata,
        "entry_points",
        lambda: SelectableEntryPoints((wanted, unrelated)),
    )

    assert route("modern/m") == ("modern", "m")
    assert wanted.load_calls == unrelated.load_calls == 0


def test_hostile_loaded_metadata_is_wrapped_without_secret_leak(monkeypatch):
    secret = "credential=do-not-render"

    class HostileMetadata:
        @property
        def abi_version(self):
            raise RuntimeError(secret)

    ep = FakeEntryPoint("hostile", HostileMetadata())
    _set_entry_points(monkeypatch, [ep])

    with pytest.raises(UnifiedError) as exc:
        load_provider_plugin("hostile")
    assert secret not in str(exc.value)
    assert secret not in "".join(traceback.format_exception(
        type(exc.value), exc.value, exc.value.__traceback__,
    ))
    assert exc.value.kind == "config"


def test_plugin_unified_error_from_metadata_access_is_sanitized(monkeypatch):
    secret = "plugin-owned-unified-metadata-detail"

    class UnifiedErrorMetadata:
        @property
        def abi_version(self):
            raise UnifiedError(
                kind="internal",
                provider="external-source",
                message=secret,
                hint=secret,
                cause=secret,
            )

    ep = FakeEntryPoint("unified-metadata", UnifiedErrorMetadata())
    _set_entry_points(monkeypatch, [ep])

    with pytest.raises(UnifiedError) as exc:
        load_provider_plugin("unified-metadata")

    formatted = "".join(traceback.format_exception(
        type(exc.value), exc.value, exc.value.__traceback__,
    ))
    assert exc.value.kind == "config"
    assert exc.value.message == (
        "Provider extension 'unified-metadata' has invalid metadata."
    )
    assert exc.value.cause == "provider plugin metadata rejected"
    assert secret not in str(exc.value)
    assert secret not in formatted
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    descriptor = next(
        item for item in list_providers(include_ext=True)
        if item.id == "unified-metadata"
    )
    assert descriptor.lifecycle_status == "broken"
    assert create("codex", bin_path="/bin/echo").name == "codex"


def test_metadata_value_evaluation_failures_are_sanitized(monkeypatch):
    class MetadataAbort(BaseException):
        pass

    def external_unified_error(secret):
        return UnifiedError(
            kind="internal",
            provider="external-source",
            message=secret,
            hint=secret,
            cause=secret,
        )

    class EqualityBomb:
        def __ne__(self, other):
            del other
            raise external_unified_error("plugin-owned-equality-detail")

    class IterationBomb:
        def __iter__(self):
            raise MetadataAbort("plugin-owned-iteration-detail")

    class CoercionBomb:
        def __iter__(self):
            return iter(())

        def __bool__(self):
            raise MetadataAbort("plugin-owned-coercion-detail")

    equality_plugin = _plugin("metadata-equality")
    object.__setattr__(equality_plugin, "abi_version", EqualityBomb())
    iteration_plugin = _plugin("metadata-iteration")
    object.__setattr__(iteration_plugin, "capabilities", IterationBomb())
    coercion_plugin = _plugin("metadata-coercion")
    object.__setattr__(coercion_plugin, "route_prefixes", CoercionBomb())
    entries = [
        FakeEntryPoint("metadata-equality", equality_plugin),
        FakeEntryPoint("metadata-iteration", iteration_plugin),
        FakeEntryPoint("metadata-coercion", coercion_plugin),
    ]
    _set_entry_points(monkeypatch, entries)

    secrets = {
        "metadata-equality": "plugin-owned-equality-detail",
        "metadata-iteration": "plugin-owned-iteration-detail",
        "metadata-coercion": "plugin-owned-coercion-detail",
    }
    for provider_id, secret in secrets.items():
        with pytest.raises(UnifiedError, match="invalid metadata") as exc:
            load_provider_plugin(provider_id)
        formatted = "".join(traceback.format_exception(
            type(exc.value), exc.value, exc.value.__traceback__,
        ))
        assert exc.value.kind == "config"
        assert secret not in str(exc.value)
        assert secret not in formatted
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None
        descriptor = next(
            item for item in list_providers(include_ext=True)
            if item.id == provider_id
        )
        assert descriptor.lifecycle_status == "broken"

    assert create("claude", bin_path="/bin/echo").name == "claude"


def test_hostile_entry_point_name_subclass_is_rejected_without_execution(monkeypatch):
    secret = "credential-from-entry-point-name"

    class HostileName(str):
        def __len__(self):
            raise RuntimeError(secret)

    ep = FakeEntryPoint(HostileName("hostile-name"), _plugin("hostile-name"))
    _set_entry_points(monkeypatch, [ep])

    descriptors = list_providers(include_ext=True)
    assert descriptors[-1].status == "invalid"
    assert descriptors[-1].id == "<invalid>"
    assert secret not in repr(descriptors)
    assert ep.load_calls == 0


def test_broken_entry_point_name_does_not_block_a_healthy_extension(monkeypatch):
    secret = "metadata-property-detail"

    class BrokenNameEntryPoint:
        group = registry.ENTRY_POINT_GROUP

        @property
        def name(self):
            raise RuntimeError(secret)

    class BrokenGroupEntryPoint:
        name = "broken-group"

        @property
        def group(self):
            raise RuntimeError(secret)

    healthy = FakeEntryPoint("healthy", _plugin("healthy"))
    _set_entry_points(monkeypatch, [
        BrokenNameEntryPoint(), BrokenGroupEntryPoint(), healthy,
    ])

    assert route("healthy/model") == ("healthy", "model")
    descriptors = list_providers(include_ext=True)
    assert any(item.id == "healthy" for item in descriptors)
    assert any(item.id == "<invalid>" for item in descriptors)
    assert secret not in repr(descriptors)


def test_explicit_extension_model_lister_and_doctor_are_safe(monkeypatch):
    plugin = ProviderPluginV1(
        id="inspectable",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m1",
        model_lister=lambda: [
            ModelInfo(
                id="m1", provider="inspectable", default=True, source="plugin",
            ),
        ],
        doctor=lambda: {"status": "ok"},
    )
    ep = FakeEntryPoint("inspectable", plugin)
    _set_entry_points(monkeypatch, [ep])

    assert [model.id for model in list_models("inspectable")] == ["m1"]
    assert doctor_provider("inspectable") == {"status": "ok"}
    assert ep.load_calls == 1


def test_held_support_blocks_all_plugin_callbacks(monkeypatch):
    calls = {"factory": 0, "models": 0, "doctor": 0}
    secret = "\x1b[31mplugin-owned-held-detail"

    def forbidden(name):
        def callback(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise RuntimeError(secret)

        return callback

    plugin = ProviderPluginV1(
        id="held-provider",
        factory=forbidden("factory"),
        default_model="unavailable",
        model_lister=forbidden("models"),
        doctor=forbidden("doctor"),
        capabilities=frozenset(),
        support_status="held",
    )
    ep = FakeEntryPoint("held-provider", plugin)
    _set_entry_points(monkeypatch, [ep])

    before = list_providers(include_ext=True)[-1]
    assert before.lifecycle_status == "discovered"
    assert before.support_status == "unknown"
    assert ep.load_calls == 0

    for call in (
        lambda: create("held-provider"),
        lambda: list_models("held-provider"),
        lambda: doctor_provider("held-provider"),
    ):
        with pytest.raises(
            UnifiedError, match="is held",
        ) as exc:
            call()
        assert exc.value.kind == "config"
        assert secret not in str(exc.value)

    assert calls == {"factory": 0, "models": 0, "doctor": 0}
    assert ep.load_calls == 1
    after = list_providers(include_ext=True)[-1]
    assert after.lifecycle_status == "loaded"
    assert after.support_status == "held"
    assert after.default_model is None
    assert after.capabilities == frozenset()


def test_mutated_support_metadata_is_safely_isolated(monkeypatch):
    secret = "\x1b[31mprivate-support-metadata"
    plugin = _plugin("mutated-support")
    object.__setattr__(plugin, "support_status", secret)
    _set_entry_points(
        monkeypatch, [FakeEntryPoint("mutated-support", plugin)],
    )

    with pytest.raises(UnifiedError, match="invalid metadata") as exc:
        load_provider_plugin("mutated-support")
    assert secret not in str(exc.value)
    assert secret not in "".join(traceback.format_exception(
        type(exc.value), exc.value, exc.value.__traceback__,
    ))
    descriptor = list_providers(include_ext=True)[-1]
    assert descriptor.lifecycle_status == "broken"
    assert descriptor.support_status == "unknown"
    assert create("codex", bin_path="/bin/echo").name == "codex"


@pytest.mark.parametrize(
    "bad_model",
    [
        ModelInfo(id="", provider="malformed", source="plugin"),
        ModelInfo(id="bad\nmodel", provider="malformed", source="plugin"),
        ModelInfo(id="\ud800", provider="malformed", source="plugin"),
        ModelInfo(id="bad\u0085model", provider="malformed", source="plugin"),
        ModelInfo(id="bad\u2028model", provider="malformed", source="plugin"),
        ModelInfo(id="ok", provider="other", source="plugin"),
        ModelInfo(id="ok", provider="malformed", display_name="bad\x00name",
                  source="plugin"),
        ModelInfo(id="ok", provider="malformed", default=1, source="plugin"),
        ModelInfo(id="ok", provider="malformed", source="hardcoded"),
    ],
)
def test_extension_model_metadata_is_validated_before_render(monkeypatch, bad_model):
    plugin = ProviderPluginV1(
        id="malformed",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=lambda: [bad_model],
        doctor=lambda: None,
    )
    _set_entry_points(monkeypatch, [FakeEntryPoint("malformed", plugin)])

    with pytest.raises(UnifiedError, match="could not list models"):
        list_models("malformed")


def test_extension_model_listing_has_a_hard_count_limit(monkeypatch):
    plugin = ProviderPluginV1(
        id="overflow",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=lambda: (
            ModelInfo(id=f"m-{index}", provider="overflow", source="plugin")
            for index in range(1_001)
        ),
        doctor=lambda: None,
    )
    _set_entry_points(monkeypatch, [FakeEntryPoint("overflow", plugin)])

    with pytest.raises(UnifiedError, match="could not list models"):
        list_models("overflow")


@pytest.mark.parametrize("callback", ["models", "doctor"])
def test_extension_inspection_errors_are_sanitized(monkeypatch, callback):
    secret = "oauth-token-from-plugin"

    def fail():
        raise RuntimeError(secret)

    plugin = ProviderPluginV1(
        id="faulty",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=fail if callback == "models" else (lambda: []),
        doctor=fail if callback == "doctor" else (lambda: None),
    )
    _set_entry_points(monkeypatch, [FakeEntryPoint("faulty", plugin)])

    with pytest.raises(UnifiedError) as exc:
        if callback == "models":
            list_models("faulty")
        else:
            doctor_provider("faulty")
    assert secret not in "".join(traceback.format_exception(
        type(exc.value), exc.value, exc.value.__traceback__,
    ))


def test_plugin_keyboard_interrupt_is_not_swallowed(monkeypatch):
    class InterruptedEntryPoint(FakeEntryPoint):
        def load(self):
            self.load_calls += 1
            raise KeyboardInterrupt

    ep = InterruptedEntryPoint("interrupted")
    _set_entry_points(monkeypatch, [ep])

    with pytest.raises(KeyboardInterrupt):
        load_provider_plugin("interrupted")
    # The cancelled owner publishes a sanitized terminal error for subsequent
    # callers rather than leaving a cache record that waits forever.
    with pytest.raises(UnifiedError, match="could not be loaded"):
        load_provider_plugin("interrupted")


def test_extension_process_classification_does_not_echo_output():
    secret = "Authorization: Bearer private-plugin-token"
    error = classify("acme", stderr=secret, stdout=secret, exitcode=7)

    assert error.kind == "internal"
    assert secret not in str(error)
    assert error.cause == "extension provider process failed"


def test_extension_runtime_boundary_sanitizes_sync_and_async_failures(monkeypatch):
    secret = "provider-runtime-private-detail"

    class ExplodingProvider(DummyProvider):
        def chat(self, prompt, **kwargs):
            raise RuntimeError(secret)

        def stream(self, prompt, **kwargs):
            raise RuntimeError(secret)
            yield  # pragma: no cover

        async def achat(self, prompt, **kwargs):
            raise RuntimeError(secret)

        async def astream(self, prompt, **kwargs):
            raise RuntimeError(secret)
            yield  # pragma: no cover

    def factory(**kwargs):
        provider = ExplodingProvider(**kwargs)
        provider.name = "runtime-fail"
        return provider

    plugin = _plugin("runtime-fail", factory=factory)
    _set_entry_points(monkeypatch, [FakeEntryPoint("runtime-fail", plugin)])
    provider = create("runtime-fail")

    def consume_async_stream():
        async def consume():
            return [message async for message in provider.astream("hello")]

        return asyncio.run(consume())

    calls = [
        lambda: provider.chat("hello"),
        lambda: list(provider.stream("hello")),
        lambda: asyncio.run(provider.achat("hello")),
        consume_async_stream,
    ]
    for call in calls:
        with pytest.raises(UnifiedError) as exc:
            call()
        formatted = "".join(traceback.format_exception(
            type(exc.value), exc.value, exc.value.__traceback__,
        ))
        assert exc.value.kind == "internal"
        assert secret not in formatted
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None


def test_extension_runtime_boundary_sanitizes_plugin_unified_errors(monkeypatch):
    secret = "plugin-owned-unified-error-secret"

    class ExplodingProvider(DummyProvider):
        @staticmethod
        def _error():
            return UnifiedError(
                kind="internal",
                provider="runtime-unified-error",
                message=secret,
                hint=secret,
                cause=secret,
            )

        def chat(self, prompt, **kwargs):
            raise self._error()

        def stream(self, prompt, **kwargs):
            raise self._error()
            yield  # pragma: no cover

        async def achat(self, prompt, **kwargs):
            raise self._error()

        async def astream(self, prompt, **kwargs):
            raise self._error()
            yield  # pragma: no cover

    def factory(**kwargs):
        provider = ExplodingProvider(**kwargs)
        provider.name = "runtime-unified-error"
        return provider

    _set_entry_points(monkeypatch, [FakeEntryPoint(
        "runtime-unified-error",
        _plugin("runtime-unified-error", factory=factory),
    )])
    provider = create("runtime-unified-error")

    async def consume_async_stream():
        return [message async for message in provider.astream("hello")]

    calls = [
        lambda: provider.chat("hello"),
        lambda: list(provider.stream("hello")),
        lambda: asyncio.run(provider.achat("hello")),
        lambda: asyncio.run(consume_async_stream()),
    ]
    for call in calls:
        with pytest.raises(UnifiedError) as exc:
            call()
        formatted = "".join(traceback.format_exception(
            type(exc.value), exc.value, exc.value.__traceback__,
        ))
        assert secret not in formatted
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None


def test_extension_factory_proxy_construction_is_inside_error_boundary(monkeypatch):
    secret = "provider-attribute-secret"

    class AttributeBombProvider(DummyProvider):
        @property
        def default_model(self):
            raise RuntimeError(secret)

    def factory(**kwargs):
        provider = AttributeBombProvider(**kwargs)
        provider.name = "attribute-bomb"
        return provider

    _set_entry_points(monkeypatch, [FakeEntryPoint(
        "attribute-bomb",
        _plugin("attribute-bomb", factory=factory),
    )])

    with pytest.raises(UnifiedError) as exc:
        create("attribute-bomb")
    formatted = "".join(traceback.format_exception(
        type(exc.value), exc.value, exc.value.__traceback__,
    ))
    assert "could not be created" in str(exc.value)
    assert secret not in formatted
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_extension_async_stream_close_closes_inner_iterator(monkeypatch):
    closed = {"value": False}

    class ClosingProvider(DummyProvider):
        async def astream(self, prompt, **kwargs):
            try:
                yield object()
                await asyncio.sleep(60)
            finally:
                closed["value"] = True

    def factory(**kwargs):
        provider = ClosingProvider(**kwargs)
        provider.name = "closing-stream"
        return provider

    _set_entry_points(monkeypatch, [FakeEntryPoint(
        "closing-stream",
        _plugin("closing-stream", factory=factory),
    )])

    async def close_after_first_item():
        stream = create("closing-stream").astream("hello")
        await stream.__anext__()
        await stream.aclose()

    asyncio.run(close_after_first_item())
    assert closed["value"] is True


def test_extension_runtime_boundary_preserves_keyboard_interrupt(monkeypatch):
    class InterruptedProvider(DummyProvider):
        def chat(self, prompt, **kwargs):
            raise KeyboardInterrupt

    def factory(**kwargs):
        provider = InterruptedProvider(**kwargs)
        provider.name = "runtime-interrupt"
        return provider

    _set_entry_points(monkeypatch, [
        FakeEntryPoint(
            "runtime-interrupt",
            _plugin("runtime-interrupt", factory=factory),
        ),
    ])

    with pytest.raises(KeyboardInterrupt):
        create("runtime-interrupt").chat("hello")


def test_providers_cli_plain_and_json_remain_metadata_only(monkeypatch, capsys):
    ep = FakeEntryPoint("acme", _plugin("acme"))
    _set_entry_points(monkeypatch, [ep])
    from unified_cli.cli import main

    assert main(["providers", "--include-ext", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    extension = next(item for item in payload if item["id"] == "acme")
    assert extension["status"] == "discovered"
    assert extension["lifecycle_status"] == "discovered"
    assert extension["support_status"] == "unknown"
    assert ep.load_calls == 0

    assert main(["providers", "--include-ext"]) == 0
    output = capsys.readouterr().out
    assert "lifecycle" in output
    assert "support" in output
    assert "discovered" in output
    assert "unknown" in output
    assert ep.load_calls == 0


def test_models_cli_can_explicitly_list_one_extension(monkeypatch, capsys):
    plugin = ProviderPluginV1(
        id="acme",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="m",
        model_lister=lambda: [ModelInfo(id="m", provider="acme", source="plugin")],
        doctor=lambda: None,
    )
    ep = FakeEntryPoint("acme", plugin)
    _set_entry_points(monkeypatch, [ep])
    from unified_cli.cli import main

    assert main(["models", "acme", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"provider": "acme"' in output
    assert '"source": "plugin"' in output
    assert ep.load_calls == 1


def test_models_cli_rejects_held_provider_and_reports_loaded_support(
    monkeypatch, capsys,
):
    calls = {"models": 0}

    def forbidden_models():
        calls["models"] += 1
        raise AssertionError("held model callback ran")

    plugin = ProviderPluginV1(
        id="held-cli",
        factory=lambda **kw: DummyProvider(**kw),
        default_model="unavailable",
        model_lister=forbidden_models,
        doctor=lambda: None,
        support_status="held",
    )
    ep = FakeEntryPoint("held-cli", plugin)
    _set_entry_points(monkeypatch, [ep])
    from unified_cli.cli import main

    assert main(["models", "held-cli", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is held" in captured.err
    assert calls["models"] == 0
    assert ep.load_calls == 1

    assert main(["providers", "--include-ext", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    extension = next(item for item in payload if item["id"] == "held-cli")
    assert extension["lifecycle_status"] == "loaded"
    assert extension["support_status"] == "held"
    assert extension["default_model"] is None


def test_server_rejects_extension_prefix_with_legacy_error_before_route_or_state(
    monkeypatch,
):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from unified_cli import server

    state_calls = {"n": 0}
    discovery_calls = _set_entry_points(
        monkeypatch, [FakeEntryPoint("acme", _plugin("acme"))],
    )
    monkeypatch.setattr(
        server, "_acquire_conversation",
        lambda user: state_calls.__setitem__("n", state_calls["n"] + 1),
    )
    req = server.ChatRequest(
        model="acme/model",
        messages=[{"role": "user", "content": "hi"}],
    )

    with pytest.raises(HTTPException) as exc:
        server.chat_completions(req)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "config"
    assert exc.value.detail["provider"] == "claude"
    assert discovery_calls["n"] == 0
    assert state_calls["n"] == 0


def test_server_preserves_core_inferred_slash_models(monkeypatch):
    pytest.importorskip("fastapi")
    from unified_cli import server

    def state_reached(user):
        raise RuntimeError("core route reached conversation state")

    monkeypatch.setattr(server, "_acquire_conversation", state_reached)
    req = server.ChatRequest(
        model="claude-custom/path",
        messages=[{"role": "user", "content": "hi"}],
    )

    with pytest.raises(RuntimeError, match="core route reached"):
        server.chat_completions(req)
