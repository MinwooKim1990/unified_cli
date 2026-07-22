"""Stage 7 bounded cache and zero-probe startup regressions."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from unified_cli import cli, manage, models
from unified_cli.core import ModelInfo
from unified_cli.errors import UnifiedError


@pytest.fixture(autouse=True)
def _clear_process_caches():
    models.invalidate_model_cache()
    yield
    models.invalidate_model_cache()


class _ObservedEvent:
    """Delegate an Event while exposing that a production waiter reached it."""

    def __init__(self, event, waiting, *, expected=1):
        self._event = event
        self._waiting = waiting
        self._expected = expected
        self._count = 0
        self._lock = threading.Lock()

    def is_set(self):
        return self._event.is_set()

    def set(self):
        return self._event.set()

    def wait(self, timeout=None):
        with self._lock:
            self._count += 1
            if self._count >= self._expected:
                self._waiting.set()
        return self._event.wait(timeout)


def test_core_model_cache_hit_expiry_force_invalidation_and_copy(monkeypatch):
    now = [100.0]
    calls = []

    def listing():
        calls.append(True)
        return [
            ModelInfo(
                id="model-{}".format(len(calls)),
                provider="claude",
                display_name="pristine",
            )
        ]

    monkeypatch.setattr(models.time, "monotonic", lambda: now[0])
    monkeypatch.setitem(models._LISTERS, "claude", listing)

    first = models.list_models("claude")
    first[0].display_name = "mutated"
    second = models.list_models("claude")
    assert len(calls) == 1
    assert second[0].display_name == "pristine"
    assert second is not first and second[0] is not first[0]

    now[0] += models._TTL
    assert models.list_models("claude")[0].id == "model-2"
    assert models.list_models("claude", force_refresh=True)[0].id == "model-3"
    models.invalidate_model_cache("claude")
    assert models.list_models("claude")[0].id == "model-4"


def test_core_model_force_refresh_is_single_flight_and_failure_wakes_waiters(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(8)
    calls = []

    def failed_listing():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        raise RuntimeError("injected refresh failure")

    monkeypatch.setitem(models._LISTERS, "claude", failed_listing)

    def refresh():
        start.wait(timeout=2)
        return models.list_models("claude", force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(refresh) for _ in range(8)]
        assert entered.wait(1)
        # Give the already-released barrier participants time to join the one
        # blocked flight before allowing its owner to fail.
        assert not release.wait(0.05)
        release.set()
        for future in futures:
            with pytest.raises(RuntimeError, match="injected refresh failure"):
                future.result(timeout=2)
    assert len(calls) == 1

    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        lambda: [ModelInfo(id="recovered", provider="claude")],
    )
    assert models.list_models("claude")[0].id == "recovered"


def test_core_invalidation_fences_old_flight_and_cleanup_preserves_replacement(
    monkeypatch,
):
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    lock = threading.Lock()
    calls = []

    def listing():
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return [ModelInfo(id="generation-{}".format(index), provider="claude")]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(models.list_models, "claude")
        assert entered[0].wait(1)
        models.invalidate_model_cache("claude")
        replacement = pool.submit(models.list_models, "claude")
        assert entered[1].wait(1)
        joined = pool.submit(models.list_models, "claude")
        release[0].set()
        assert old.result(timeout=1)[0].id == "generation-0"
        assert not replacement.done()
        release[1].set()
        assert replacement.result(timeout=1)[0].id == "generation-1"
        assert joined.result(timeout=1)[0].id == "generation-1"
    assert len(calls) == 2
    assert models.list_models("claude")[0].id == "generation-1"


def test_core_force_refresh_fences_ordinary_flight_and_force_callers_share(
    monkeypatch,
):
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    start = threading.Barrier(8)
    lock = threading.Lock()
    calls = []

    def listing():
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return [ModelInfo(id="refresh-{}".format(index), provider="claude")]

    monkeypatch.setitem(models._LISTERS, "claude", listing)

    def forced():
        start.wait(timeout=2)
        return models.list_models("claude", force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        old = pool.submit(models.list_models, "claude")
        assert entered[0].wait(1)
        forced_calls = [pool.submit(forced) for _ in range(8)]
        assert entered[1].wait(1)
        assert not release[1].wait(0.05)
        assert len(calls) == 2
        release[0].set()
        release[1].set()
        assert old.result(timeout=1)[0].id == "refresh-0"
        assert {
            future.result(timeout=1)[0].id for future in forced_calls
        } == {"refresh-1"}
    assert models.list_models("claude")[0].id == "refresh-1"


def test_core_empty_model_results_are_not_cached(monkeypatch):
    calls = []

    def listing():
        calls.append(True)
        if len(calls) == 1:
            return []
        return [ModelInfo(id="available", provider="claude")]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    assert models.list_models("claude") == []
    assert models.list_models("claude")[0].id == "available"
    assert len(calls) == 2


def test_core_model_cache_ignores_wall_clock_rollback(monkeypatch):
    monotonic = [10.0]
    wall = [1_000.0]
    calls = []
    monkeypatch.setattr(models.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(models.time, "time", lambda: wall[0])
    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        lambda: calls.append(True) or [ModelInfo(id="stable", provider="claude")],
    )
    models.list_models("claude")
    wall[0] = -10_000.0
    monotonic[0] += 1.0
    models.list_models("claude")
    assert len(calls) == 1


def test_core_claude_context_is_normalized_hashed_and_proxy_aware(monkeypatch):
    calls = []

    def listing():
        calls.append(True)
        return [
            ModelInfo(
                id="claude-context-{}".format(len(calls)),
                provider="claude",
            )
        ]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  claude-secret-a\n")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-one.invalid")
    assert models.list_models("claude")[0].id == "claude-context-1"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-secret-a")
    assert models.list_models("claude")[0].id == "claude-context-1"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-secret-b")
    assert models.list_models("claude")[0].id == "claude-context-2"
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-two.invalid")
    assert models.list_models("claude")[0].id == "claude-context-3"
    assert len(calls) == 3
    assert models._cached("claude")[0].id == "claude-context-3"

    state = repr(
        (models._CACHE, models._CACHE_FLIGHTS, models._CACHE_GENERATIONS)
    )
    for raw in (
        "claude-secret-a",
        "claude-secret-b",
        "http://proxy-one.invalid",
        "http://proxy-two.invalid",
    ):
        assert raw not in state
    models.invalidate_model_cache("claude")
    assert not any(key[0] == "claude" for key in models._CACHE)


def test_core_codex_context_tracks_home_file_change_and_replacement(
    tmp_path, monkeypatch,
):
    def write_cache(home: Path, slug: str) -> Path:
        path = home / ".codex" / "models_cache.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"models": [{"slug": slug, "display_name": slug}]}),
            encoding="utf-8",
        )
        return path

    home_a = tmp_path / "codex-home-a"
    home_b = tmp_path / "codex-home-b"
    cache_a = write_cache(home_a, "codex-a")
    write_cache(home_b, "codex-b")

    monkeypatch.setenv("HOME", str(home_a))
    assert models.list_models("codex")[0].id == "codex-a"

    replacement = cache_a.with_name("replacement.json")
    replacement.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "codex-a-replaced", "display_name": "replacement"}
                ]
            }
        ),
        encoding="utf-8",
    )
    replacement.replace(cache_a)
    assert models.list_models("codex")[0].id == "codex-a-replaced"

    monkeypatch.setenv("HOME", str(home_b))
    assert models.list_models("codex")[0].id == "codex-b"
    state = repr(
        (models._CACHE, models._CACHE_FLIGHTS, models._CACHE_GENERATIONS)
    )
    assert str(home_a) not in state
    assert str(home_b) not in state
    assert str(cache_a) not in state


def test_core_gemini_context_tracks_opt_in_override_and_binary_change(
    tmp_path, monkeypatch,
):
    binaries = []
    for directory_name in ("agy-one", "agy-two"):
        directory = tmp_path / directory_name
        directory.mkdir()
        binary = directory / "agy"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o700)
        binaries.append(binary)
    calls = []

    def listing():
        calls.append(True)
        return [
            ModelInfo(
                id="gemini-context-{}".format(len(calls)),
                provider="gemini",
            )
        ]

    monkeypatch.setitem(models._LISTERS, "gemini", listing)
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "gemini-home"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("AGY_CLI_PATH", str(binaries[0]))
    assert models.list_models("gemini")[0].id == "gemini-context-1"
    assert models.list_models("gemini")[0].id == "gemini-context-1"

    monkeypatch.setenv("AGY_CLI_PATH", str(binaries[1]))
    assert models.list_models("gemini")[0].id == "gemini-context-2"
    binaries[1].write_text("#!/bin/sh\n# changed binary\nexit 0\n", encoding="utf-8")
    binaries[1].chmod(0o700)
    assert models.list_models("gemini")[0].id == "gemini-context-3"

    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "off")
    assert models.list_models("gemini")[0].id == "gemini-context-4"
    assert len(calls) == 4
    state = repr(
        (models._CACHE, models._CACHE_FLIGHTS, models._CACHE_GENERATIONS)
    )
    assert str(binaries[0]) not in state
    assert str(binaries[1]) not in state
    assert str(tmp_path / "gemini-home") not in state


def test_core_context_fingerprints_are_passive_and_probe_free(
    tmp_path, monkeypatch,
):
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agy.chmod(0o700)
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    monkeypatch.setenv("AGY_CLI_PATH", str(agy))

    def blocked(*_args, **_kwargs):
        raise AssertionError("context fingerprint performed an active probe")

    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(models.urllib.request, "urlopen", blocked)
    contexts = {
        provider: models._model_context(provider)
        for provider in ("claude", "codex", "gemini")
    }
    assert all(len(value) == 64 for value in contexts.values())


def test_core_old_context_flight_cannot_cache_over_current_context(monkeypatch):
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    lock = threading.Lock()
    calls = []

    def listing():
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return [
            ModelInfo(id="context-generation-{}".format(index), provider="claude")
        ]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-context-secret")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(models.list_models, "claude")
        assert entered[0].wait(1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "current-context-secret")
        current = pool.submit(models.list_models, "claude")
        assert entered[1].wait(1)
        release[1].set()
        assert current.result(timeout=2)[0].id == "context-generation-1"
        release[0].set()
        assert old.result(timeout=2)[0].id == "context-generation-0"

    assert models.list_models("claude")[0].id == "context-generation-1"
    assert calls == [0, 1]
    assert len([key for key in models._CACHE if key[0] == "claude"]) == 1
    state = repr((models._CACHE, models._CACHE_FLIGHTS))
    assert "old-context-secret" not in state
    assert "current-context-secret" not in state


def test_core_model_flight_capacity_is_bounded_and_recovers(monkeypatch):
    cap = models.MAX_MODEL_FLIGHTS_PER_PROVIDER
    entered = tuple(threading.Event() for _ in range(cap))
    release = tuple(threading.Event() for _ in range(cap))
    lock = threading.Lock()
    calls = []

    def listing():
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(3)
        return [ModelInfo(id="bounded-{}".format(index), provider="claude")]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=cap) as pool:
        futures = []
        for index in range(cap):
            if index:
                models.invalidate_model_cache("claude")
            futures.append(pool.submit(models.list_models, "claude"))
            assert entered[index].wait(1)

        models.invalidate_model_cache("claude")
        with pytest.raises(UnifiedError) as caught:
            models.list_models("claude")
        assert caught.value.kind == "resource_limit"
        assert caught.value.provider == "claude"
        assert "busy" in caught.value.message.lower()
        assert "retry" in caught.value.hint.lower()
        assert "busy" in str(caught.value).lower()
        assert len(calls) == cap
        assert len(models._CACHE_FLIGHTS) == cap
        assert len(models._CACHE_FLIGHTS) <= models.MAX_MODEL_FLIGHTS

        for event in release:
            event.set()
        assert [future.result(timeout=2)[0].id for future in futures] == [
            "bounded-{}".format(index) for index in range(cap)
        ]

    assert not models._CACHE_FLIGHTS
    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        lambda: [ModelInfo(id="recovered-after-cap", provider="claude")],
    )
    assert models.list_models("claude")[0].id == "recovered-after-cap"


def test_core_global_model_flight_capacity_is_independently_bounded(
    monkeypatch,
):
    entered = tuple(
        threading.Event() for _ in range(models.MAX_MODEL_FLIGHTS)
    )
    release = tuple(
        threading.Event() for _ in range(models.MAX_MODEL_FLIGHTS)
    )
    lock = threading.Lock()
    calls = []

    def listing(provider):
        def blocked_listing():
            with lock:
                index = len(calls)
                calls.append(provider)
            entered[index].set()
            assert release[index].wait(3)
            return [
                ModelInfo(
                    id="global-{}-{}".format(provider, index),
                    provider=provider,
                )
            ]

        return blocked_listing

    for provider in ("claude", "codex", "gemini"):
        monkeypatch.setitem(models._LISTERS, provider, listing(provider))
    # Keep the provider ceiling out of this test so the thirteenth request is
    # rejected specifically by the global ceiling.
    monkeypatch.setattr(
        models,
        "MAX_MODEL_FLIGHTS_PER_PROVIDER",
        models.MAX_MODEL_FLIGHTS,
    )
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "off")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=models.MAX_MODEL_FLIGHTS
    ) as pool:
        futures = []
        providers = ("claude", "codex", "gemini")
        provider_counts = {provider: 0 for provider in providers}
        for index in range(models.MAX_MODEL_FLIGHTS):
            provider = providers[index % len(providers)]
            if provider_counts[provider]:
                models.invalidate_model_cache(provider)
            provider_counts[provider] += 1
            futures.append(pool.submit(models.list_models, provider))
            assert entered[index].wait(1)

        models.invalidate_model_cache("claude")
        with pytest.raises(UnifiedError) as caught:
            models.list_models("claude")
        assert caught.value.kind == "resource_limit"
        assert len(calls) == models.MAX_MODEL_FLIGHTS
        assert len(models._CACHE_FLIGHTS) == models.MAX_MODEL_FLIGHTS

        for event in release:
            event.set()
        assert len([future.result(timeout=2) for future in futures]) == (
            models.MAX_MODEL_FLIGHTS
        )

    assert not models._CACHE_FLIGHTS


def test_cli_model_capacity_error_is_actionable(monkeypatch):
    rendered = []

    class _ErrorConsole:
        @staticmethod
        def print(value):
            rendered.append(value)

    def busy(_provider, *, force_refresh):
        assert force_refresh is False
        raise models._busy_error("claude")

    monkeypatch.setattr(cli, "list_models", busy)
    monkeypatch.setattr(cli, "err_console", _ErrorConsole())
    result = cli._cmd_models(
        argparse.Namespace(provider="claude", refresh=False, json=False)
    )

    assert result == 2
    assert len(rendered) == 1
    assert "[claude:resource_limit]" in rendered[0]
    assert "busy" in rendered[0].lower()
    assert "retry" in rendered[0].lower()


def test_core_context_cache_is_lru_bounded_and_secret_free(monkeypatch):
    calls = []
    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        lambda: calls.append(True)
        or [ModelInfo(id="bounded-context", provider="claude")],
    )
    secrets = [
        "lru-secret-{}".format(index)
        for index in range(models.MAX_MODEL_CACHE_ENTRIES_PER_PROVIDER + 2)
    ]
    for secret in secrets:
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        assert models.list_models("claude")[0].id == "bounded-context"

    provider_entries = [key for key in models._CACHE if key[0] == "claude"]
    assert len(provider_entries) == models.MAX_MODEL_CACHE_ENTRIES_PER_PROVIDER
    assert len(models._CACHE) <= models.MAX_MODEL_CACHE_ENTRIES
    monkeypatch.setenv("ANTHROPIC_API_KEY", secrets[0])
    models.list_models("claude")
    assert len(calls) == len(secrets) + 1
    state = repr((models._CACHE, models._CACHE_FLIGHTS))
    assert all(secret not in state for secret in secrets)


def _fake_verify_binary(tmp_path: Path, monkeypatch) -> Path:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home-a"))
    return binary


def test_manage_verify_ttls_force_home_isolation_and_mutation(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    now = [50.0]
    calls = []

    def verified(argv, _cwd):
        calls.append(argv)
        return {"ok": True, "code": "ok", "output": "version-1"}

    monkeypatch.setattr(manage.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    runtime = manage.ManageRuntime([str(tmp_path)])

    first = runtime.verify_provider("claude")
    first["checks"][0]["output"] = "mutated"
    cached = runtime.verify_provider("claude")
    assert cached["checks"][0]["output"] == "version-1"
    assert len(calls) == 2

    now[0] += manage.VERIFY_AUTH_TTL_SECONDS
    runtime.verify_provider("claude")
    assert len(calls) == 3 and calls[-1][1:] == ("auth", "status", "--text")

    monkeypatch.setenv("HOME", str(tmp_path / "home-b"))
    runtime.verify_provider("claude")
    assert len(calls) == 4 and calls[-1][1:] == ("auth", "status", "--text")

    runtime.verify_provider("claude", force_refresh=True)
    assert len(calls) == 6
    runtime.invalidate_provider_cache("claude")
    runtime.verify_provider("claude")
    assert len(calls) == 8


def test_manage_binary_replacement_invalidates_all_probe_entries(
    tmp_path, monkeypatch,
):
    binary = _fake_verify_binary(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        manage,
        "_run_verify_argv",
        lambda argv, _cwd: calls.append(argv)
        or {"ok": True, "code": "ok", "output": "v"},
    )
    monkeypatch.setattr(
        manage,
        "list_models",
        lambda _provider, **_kwargs: [
            ModelInfo(id="fixture", provider="claude", source="cache")
        ],
    )
    runtime = manage.ManageRuntime([str(tmp_path)])
    runtime.verify_provider("claude")
    runtime.provider_models("claude")
    runtime.verify_provider("claude")
    runtime.provider_models("claude")
    assert len(calls) == 2

    binary.write_text("#!/bin/sh\n# replacement\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    runtime.verify_provider("claude")
    assert len(calls) == 4


def test_manage_model_cache_home_isolation_force_expiry_and_copy(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    now = [5.0]
    calls = []

    def listing(provider, **_kwargs):
        calls.append(provider)
        return [ModelInfo(id="fixture", provider=provider, display_name="clean")]

    monkeypatch.setattr(manage.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manage, "list_models", listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    first = runtime.provider_models("claude")
    first["models"][0]["display_name"] = "changed"
    assert runtime.provider_models("claude")["models"][0]["display_name"] == "clean"
    assert len(calls) == 1

    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    runtime.provider_models("claude")
    assert len(calls) == 2
    runtime.provider_models("claude", force_refresh=True)
    assert len(calls) == 3
    now[0] += manage.PROVIDER_MODELS_TTL_SECONDS
    runtime.provider_models("claude")
    assert len(calls) == 4


def test_manage_probe_errors_are_not_cached(tmp_path, monkeypatch):
    _fake_verify_binary(tmp_path, monkeypatch)
    calls = []

    def failed(argv, _cwd):
        calls.append(argv)
        return {"ok": False, "code": "spawn_failed", "output": ""}

    monkeypatch.setattr(manage, "_run_verify_argv", failed)
    runtime = manage.ManageRuntime([str(tmp_path)])
    runtime.verify_provider("claude")
    runtime.verify_provider("claude")
    assert len(calls) == 4


def test_manage_empty_model_results_are_not_cached(tmp_path, monkeypatch):
    calls = []

    def listing(provider, **_kwargs):
        calls.append(provider)
        if len(calls) == 1:
            return []
        return [ModelInfo(id="available", provider=provider)]

    monkeypatch.setattr(manage, "list_models", listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    assert runtime.provider_models("claude")["models"] == []
    assert runtime.provider_models("claude")["models"][0]["id"] == "available"
    assert calls == ["claude", "claude"]


def test_manage_maps_core_model_capacity_to_bounded_busy_error(
    tmp_path, monkeypatch,
):
    def busy(_provider, **_kwargs):
        raise models._busy_error("claude")

    monkeypatch.setattr(manage, "list_models", busy)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with pytest.raises(manage.ManageError) as caught:
        runtime.provider_models("claude")
    assert caught.value.status_code == 429
    assert caught.value.code == "models_busy"
    assert not runtime._model_flights
    assert not runtime._models_cache


def test_manage_same_provider_verify_is_eight_way_single_flight(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    start = threading.Barrier(8)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []

    def verified(argv, _cwd):
        with lock:
            calls.append(argv)
            first = len(calls) == 1
        if first:
            entered.set()
            assert release.wait(2)
        return {"ok": True, "code": "ok", "output": "version"}

    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    runtime = manage.ManageRuntime([str(tmp_path)])

    def verify():
        start.wait(timeout=2)
        return runtime.verify_provider("claude", force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(verify) for _ in range(8)]
        assert entered.wait(1)
        assert not release.wait(0.05)
        release.set()
        results = [future.result(timeout=2) for future in futures]
    assert calls == [
        ("claude", "--version"),
        ("claude", "auth", "status", "--text"),
    ]
    results[0]["checks"][0]["output"] = "mutated"
    assert all(
        result["checks"][0]["output"] == "version"
        for result in results[1:]
    )


def test_manage_model_invalidation_fences_manage_and_core_flights(
    tmp_path, monkeypatch,
):
    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    lock = threading.Lock()
    calls = []

    def listing():
        with lock:
            index = len(calls)
            calls.append(index)
        entered[index].set()
        assert release[index].wait(2)
        return [
            ModelInfo(
                id="manage-generation-{}".format(index),
                provider="claude",
            )
        ]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(runtime.provider_models, "claude")
        assert entered[0].wait(1)
        runtime.invalidate_provider_cache("claude")
        replacement = pool.submit(runtime.provider_models, "claude")
        assert entered[1].wait(1)
        joined = pool.submit(runtime.provider_models, "claude")
        assert not release[1].wait(0.05)
        release[1].set()
        assert replacement.result(timeout=2)["models"][0]["id"] == (
            "manage-generation-1"
        )
        assert joined.result(timeout=2)["models"][0]["id"] == (
            "manage-generation-1"
        )
        release[0].set()
        assert old.result(timeout=2)["models"][0]["id"] == (
            "manage-generation-0"
        )

    assert calls == [0, 1]
    assert runtime.provider_models("claude")["models"][0]["id"] == (
        "manage-generation-1"
    )
    assert not runtime._model_flights


def test_manage_disable_fences_blocked_model_owner_without_repopulation(
    tmp_path, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    waiter_entered = threading.Event()
    calls = []

    def listing():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return [ModelInfo(id="stale", provider="claude")]

    monkeypatch.setitem(models._LISTERS, "claude", listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(runtime.provider_models, "claude")
        assert entered.wait(1)
        with runtime._lock:
            flight = next(iter(runtime._model_flights.values()))
            flight.done = _ObservedEvent(flight.done, waiter_entered)
        waiter = pool.submit(runtime.provider_models, "claude")
        assert waiter_entered.wait(1)
        runtime.disable()
        assert not runtime._models_cache
        assert not runtime._model_flights
        assert not any(key[0] == "claude" for key in models._CACHE)
        with pytest.raises(manage.ManageError) as caught:
            waiter.result(timeout=1)
        assert caught.value.code == "manage_disabled"
        release.set()
        with pytest.raises(manage.ManageError) as owner_caught:
            owner.result(timeout=2)
        assert owner_caught.value.code == "manage_disabled"
        assert owner_caught.value is caught.value

    assert not runtime._models_cache
    assert len(calls) == 1
    assert not any(key[0] == "claude" for key in models._CACHE)
    assert not runtime._model_flights
    with pytest.raises(manage.ManageError) as caught:
        runtime.provider_models("claude")
    assert caught.value.code == "manage_disabled"


def test_manage_disable_before_model_probe_admission_skips_lister(
    tmp_path, monkeypatch,
):
    calls = []
    original_fence = manage.ManageRuntime._owner_fence_error_locked

    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        lambda: calls.append(True)
        or [ModelInfo(id="unexpected", provider="claude")],
    )
    runtime = manage.ManageRuntime([str(tmp_path)])
    disabled = [False]

    def disable_at_admission(self, flight):
        if self is runtime and not disabled[0]:
            disabled[0] = True
            self.disable()
        return original_fence(self, flight)

    monkeypatch.setattr(
        manage.ManageRuntime,
        "_owner_fence_error_locked",
        disable_at_admission,
    )
    with pytest.raises(manage.ManageError) as caught:
        runtime.provider_models("claude")

    assert caught.value.code == "manage_disabled"
    assert calls == []
    assert not runtime._model_flights
    assert not runtime._models_cache


def test_manage_disable_after_compat_typeerror_skips_legacy_fallback(
    tmp_path, monkeypatch,
):
    force_entered = threading.Event()
    force_release = threading.Event()
    waiter_entered = threading.Event()
    lock = threading.Lock()
    calls = []

    def compatibility_listing(provider, **kwargs):
        with lock:
            calls.append((provider, tuple(sorted(kwargs))))
        if kwargs:
            force_entered.set()
            assert force_release.wait(2)
            raise TypeError("got an unexpected keyword argument 'force_refresh'")
        return [ModelInfo(id="unexpected-fallback", provider=provider)]

    monkeypatch.setattr(manage, "list_models", compatibility_listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(runtime.provider_models, "claude")
        assert force_entered.wait(1)
        with runtime._lock:
            flight = next(iter(runtime._model_flights.values()))
            flight.done = _ObservedEvent(flight.done, waiter_entered)
        waiter = pool.submit(runtime.provider_models, "claude")
        assert waiter_entered.wait(1)

        runtime.disable()
        with pytest.raises(manage.ManageError) as waiter_caught:
            waiter.result(timeout=1)
        assert waiter_caught.value.code == "manage_disabled"
        force_release.set()
        with pytest.raises(manage.ManageError) as owner_caught:
            owner.result(timeout=2)
        assert owner_caught.value is waiter_caught.value

    assert calls == [("claude", ("force_refresh",))]
    assert not runtime._models_cache
    assert not runtime._model_flights
    assert not any(key[0] == "claude" for key in models._CACHE)
    assert not models._CACHE_FLIGHTS
    with pytest.raises(manage.ManageError) as disabled_caught:
        runtime.provider_models("claude")
    assert disabled_caught.value.code == "manage_disabled"
    assert calls == [("claude", ("force_refresh",))]


def test_manage_legacy_model_signature_fallback_still_succeeds(
    tmp_path, monkeypatch,
):
    calls = []

    def legacy_listing(provider):
        calls.append(provider)
        return [ModelInfo(id="legacy-model", provider=provider)]

    monkeypatch.setattr(manage, "list_models", legacy_listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    assert runtime.provider_models("claude")["models"][0]["id"] == (
        "legacy-model"
    )
    assert runtime.provider_models("claude")["models"][0]["id"] == (
        "legacy-model"
    )
    assert calls == ["claude"]
    assert not runtime._model_flights


def test_manage_ordinary_invalidation_preserves_legacy_fallback(
    tmp_path, monkeypatch,
):
    force_entered = threading.Event()
    force_release = threading.Event()
    calls = []
    force_count = [0]
    fallback_count = [0]

    def compatibility_listing(provider, **kwargs):
        if kwargs:
            force_count[0] += 1
            calls.append("force-{}".format(force_count[0]))
            if force_count[0] == 1:
                force_entered.set()
                assert force_release.wait(2)
            raise TypeError("got an unexpected keyword argument 'force_refresh'")
        fallback_count[0] += 1
        calls.append("fallback-{}".format(fallback_count[0]))
        return [
            ModelInfo(
                id="legacy-generation-{}".format(fallback_count[0]),
                provider=provider,
            )
        ]

    monkeypatch.setattr(manage, "list_models", compatibility_listing)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(runtime.provider_models, "claude")
        assert force_entered.wait(1)
        runtime.invalidate_provider_cache("claude")
        force_release.set()
        assert old.result(timeout=2)["models"][0]["id"] == (
            "legacy-generation-1"
        )

    assert not runtime._models_cache
    assert runtime.provider_models("claude")["models"][0]["id"] == (
        "legacy-generation-2"
    )
    assert runtime.provider_models("claude")["models"][0]["id"] == (
        "legacy-generation-2"
    )
    assert calls == ["force-1", "fallback-1", "force-2", "fallback-2"]
    assert not runtime._model_flights


def test_manage_model_commit_before_disable_remains_successful(
    tmp_path, monkeypatch,
):
    listing_entered = threading.Event()
    listing_release = threading.Event()
    response_entered = threading.Event()
    response_release = threading.Event()
    waiter_entered = threading.Event()
    original_response = manage.ManageRuntime._models_response

    def listing():
        listing_entered.set()
        assert listing_release.wait(2)
        return [ModelInfo(id="committed", provider="claude")]

    monkeypatch.setitem(
        models._LISTERS,
        "claude",
        listing,
    )

    def blocked_response(provider_id, rows):
        response_entered.set()
        assert response_release.wait(2)
        return original_response(provider_id, rows)

    monkeypatch.setattr(
        manage.ManageRuntime,
        "_models_response",
        staticmethod(blocked_response),
    )
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(runtime.provider_models, "claude")
        assert listing_entered.wait(1)
        with runtime._lock:
            flight = next(iter(runtime._model_flights.values()))
            flight.done = _ObservedEvent(flight.done, waiter_entered)
        waiter = pool.submit(runtime.provider_models, "claude")
        assert waiter_entered.wait(1)
        listing_release.set()
        assert response_entered.wait(1)
        with runtime._lock:
            assert flight.committed is True
        runtime.disable()
        assert not runtime._models_cache
        assert not runtime._model_flights
        response_release.set()
        assert owner.result(timeout=2)["models"][0]["id"] == "committed"
        assert waiter.result(timeout=2)["models"][0]["id"] == "committed"

    assert not runtime._models_cache
    assert not runtime._model_flights
    assert not any(key[0] == "claude" for key in models._CACHE)


def test_manage_force_verify_waits_for_ordinary_then_is_eight_way_single_flight(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    ordinary_version_entered = threading.Event()
    version_release = threading.Event()
    ordinary_auth_entered = threading.Event()
    ordinary_auth_release = threading.Event()
    forced_entered = threading.Event()
    forced_release = threading.Event()
    start = threading.Barrier(8)
    lock = threading.Lock()
    calls = []

    def verified(argv, _cwd):
        with lock:
            index = len(calls)
            calls.append(argv)
        if index == 0:
            ordinary_version_entered.set()
            assert version_release.wait(2)
        elif index == 1:
            ordinary_auth_entered.set()
            assert ordinary_auth_release.wait(2)
        elif index == 2:
            forced_entered.set()
            assert forced_release.wait(2)
        return {
            "ok": True,
            "code": "ok",
            "output": "verify-generation-{}".format(index),
        }

    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    runtime = manage.ManageRuntime([str(tmp_path)])

    def forced():
        start.wait(timeout=2)
        return runtime.verify_provider("claude", force_refresh=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        ordinary = pool.submit(runtime.verify_provider, "claude")
        assert ordinary_version_entered.wait(1)
        forced_calls = [pool.submit(forced) for _ in range(8)]
        assert not forced_entered.wait(0.05)
        runtime.invalidate_provider_cache("claude")
        version_release.set()
        assert ordinary_auth_entered.wait(1)
        assert not forced_entered.wait(0.05)
        ordinary_auth_release.set()
        assert forced_entered.wait(1)
        assert len(calls) == 3
        forced_release.set()
        ordinary_result = ordinary.result(timeout=2)
        results = [future.result(timeout=2) for future in forced_calls]

    assert ordinary_result["checks"][0]["output"] == "verify-generation-0"
    assert all(
        result["checks"][0]["output"] == "verify-generation-2"
        for result in results
    )
    assert calls == [
        ("claude", "--version"),
        ("claude", "auth", "status", "--text"),
        ("claude", "--version"),
        ("claude", "auth", "status", "--text"),
    ]
    assert not runtime._verify_flights


def test_manage_force_verify_recovers_after_ordinary_exception(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []

    def verified(argv, _cwd):
        with lock:
            index = len(calls)
            calls.append(argv)
        if index == 0:
            entered.set()
            assert release.wait(2)
            raise RuntimeError("ordinary verification failed")
        return {"ok": True, "code": "ok", "output": "recovered"}

    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ordinary = pool.submit(runtime.verify_provider, "claude")
        assert entered.wait(1)
        forced = pool.submit(
            runtime.verify_provider, "claude", force_refresh=True
        )
        assert not release.wait(0.05)
        release.set()
        with pytest.raises(RuntimeError, match="ordinary verification failed"):
            ordinary.result(timeout=2)
        assert forced.result(timeout=2)["checks"][0]["output"] == "recovered"

    assert len(calls) == 3
    assert not runtime._verify_flights
    assert runtime.verify_provider("claude")["ready"] is True


def test_manage_disable_wakes_force_verify_waiter_without_new_generation(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    waiter_entered = threading.Event()
    lock = threading.Lock()
    calls = []

    def verified(argv, _cwd):
        with lock:
            calls.append(argv)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return {"ok": True, "code": "ok", "output": "old"}

    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        ordinary = pool.submit(runtime.verify_provider, "claude")
        assert entered.wait(1)
        with runtime._lock:
            flight = next(iter(runtime._verify_flights.values()))
            flight.done = _ObservedEvent(
                flight.done, waiter_entered, expected=2
            )
        ordinary_waiter = pool.submit(runtime.verify_provider, "claude")
        forced = pool.submit(
            runtime.verify_provider, "claude", force_refresh=True
        )
        assert waiter_entered.wait(1)
        runtime.disable()
        assert not runtime._version_cache
        assert not runtime._auth_cache
        assert not runtime._verify_flights
        shutdown_errors = []
        for future in (ordinary_waiter, forced):
            with pytest.raises(manage.ManageError) as caught:
                future.result(timeout=1)
            assert caught.value.code == "manage_disabled"
            shutdown_errors.append(caught.value)
        release.set()
        with pytest.raises(manage.ManageError) as caught:
            ordinary.result(timeout=2)
        assert caught.value.code == "manage_disabled"
        shutdown_errors.append(caught.value)
        assert all(error is shutdown_errors[0] for error in shutdown_errors)

    assert calls == [("claude", "--version")]
    assert not runtime._verify_flights
    assert not runtime._version_cache
    assert not runtime._auth_cache


def test_manage_verify_commit_before_disable_remains_successful(
    tmp_path, monkeypatch,
):
    _fake_verify_binary(tmp_path, monkeypatch)
    version_entered = threading.Event()
    version_release = threading.Event()
    response_entered = threading.Event()
    response_release = threading.Event()
    waiter_entered = threading.Event()
    calls = []
    original_response = manage.ManageRuntime._verify_response

    def verified(argv, _cwd):
        calls.append(argv)
        if len(calls) == 1:
            version_entered.set()
            assert version_release.wait(2)
        return {"ok": True, "code": "ok", "output": "committed"}

    def blocked_response(provider_id, version_record, auth_record):
        response_entered.set()
        assert response_release.wait(2)
        return original_response(provider_id, version_record, auth_record)

    monkeypatch.setattr(manage, "_run_verify_argv", verified)
    monkeypatch.setattr(
        manage.ManageRuntime,
        "_verify_response",
        staticmethod(blocked_response),
    )
    runtime = manage.ManageRuntime([str(tmp_path)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(runtime.verify_provider, "claude")
        assert version_entered.wait(1)
        with runtime._lock:
            flight = next(iter(runtime._verify_flights.values()))
            flight.done = _ObservedEvent(flight.done, waiter_entered)
        waiter = pool.submit(runtime.verify_provider, "claude")
        assert waiter_entered.wait(1)
        version_release.set()
        assert response_entered.wait(1)
        with runtime._lock:
            assert flight.committed is True
        runtime.disable()
        assert not runtime._version_cache
        assert not runtime._auth_cache
        assert not runtime._verify_flights
        response_release.set()
        assert owner.result(timeout=2)["ready"] is True
        assert waiter.result(timeout=2)["ready"] is True

    assert calls == [
        ("claude", "--version"),
        ("claude", "auth", "status", "--text"),
    ]
    assert not runtime._version_cache
    assert not runtime._auth_cache
    assert not runtime._verify_flights


def test_manage_gemini_models_use_effective_agy_override_identity(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("UNIFIED_CLI_ENABLE_GEMINI", "1")
    binaries = []
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        binary = directory / "agy"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o700)
        binaries.append(binary)
    calls = []
    monkeypatch.setattr(
        manage,
        "list_models",
        lambda provider, **_kwargs: calls.append(provider)
        or [ModelInfo(id="gemini-fixture", provider=provider)],
    )
    runtime = manage.ManageRuntime([str(tmp_path)])

    monkeypatch.setenv("AGY_CLI_PATH", str(binaries[0]))
    runtime.provider_models("gemini")
    runtime.provider_models("gemini")
    assert len(calls) == 1

    monkeypatch.setenv("AGY_CLI_PATH", str(binaries[1]))
    runtime.provider_models("gemini")
    assert len(calls) == 2

    binaries[1].write_text("#!/bin/sh\n# replacement\nexit 0\n", encoding="utf-8")
    binaries[1].chmod(0o700)
    runtime.provider_models("gemini")
    assert len(calls) == 3


def test_core_server_repl_and_manage_startup_are_subprocess_probe_free():
    root = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio
import builtins
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request

loop = asyncio.new_event_loop()
sys.path.insert(0, {source!r})

def blocked(*_args, **_kwargs):
    raise AssertionError("startup performed an external probe")

subprocess.Popen = blocked
subprocess.run = blocked
socket.socket = blocked
socket.create_connection = blocked
urllib.request.urlopen = blocked

import unified_cli
from unified_cli import manage, models, repl, server

async def lifespan():
    async with server._lifespan(server.app):
        pass

loop.run_until_complete(lifespan())
with tempfile.TemporaryDirectory() as workspace:
    runtime = manage.ManageRuntime([workspace])
    token = runtime.issue_bootstrap()
    payload, cookie = runtime.bootstrap(
        supplied_token=token,
        supplied_csrf=None,
        cookie=None,
        peer_key="127.0.0.1",
    )
    assert payload["authenticated"] is True and cookie
    assert not runtime._version_cache
    assert not runtime._auth_cache
    assert not runtime._models_cache

repl._interactive = lambda: False
repl._setup_readline = lambda: None
repl._banner = lambda *_args: None
repl._on_exit = lambda *_args: None
builtins.input = lambda *_args: (_ for _ in ()).throw(EOFError())
assert repl.run_repl(provider="claude", cwd=".") == 0
assert not models._CACHE
loop.close()
'''.format(source=str(root / "src"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
