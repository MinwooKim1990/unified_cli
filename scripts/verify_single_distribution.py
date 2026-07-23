#!/usr/bin/env python3
"""Verify an installed unified-cli wheel owns both namespaces and stays inert."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as importlib_metadata
import socket
import subprocess
import sys
from typing import Dict, Optional, Sequence


EXPECTED_PROVIDER_ENTRY_POINTS: Dict[str, str] = {
    "amp": "unified_cli_ext.providers.amp:PLUGIN",
    "cline": "unified_cli_ext.providers.cline:PLUGIN",
    "codebuddy": "unified_cli_ext.providers.codebuddy:PLUGIN",
    "copilot": "unified_cli_ext.providers.copilot:PLUGIN",
    "cursor": "unified_cli_ext.providers.cursor:PLUGIN",
    "droid": "unified_cli_ext.providers.droid:PLUGIN",
    "gitlab-duo": "unified_cli_ext.providers.gitlab_duo:PLUGIN",
    "grok": "unified_cli_ext.providers.grok:PLUGIN",
    "hermes": "unified_cli_ext.providers.hermes:PLUGIN",
    "kilo": "unified_cli_ext.providers.kilo:PLUGIN",
    "kimi": "unified_cli_ext.providers.kimi:PLUGIN",
    "mistral-vibe": "unified_cli_ext.providers.mistral_vibe:PLUGIN",
    "oh-my-pi": "unified_cli_ext.providers.oh_my_pi:PLUGIN",
    "opencode": "unified_cli_ext.providers.opencode:PLUGIN",
    "pi": "unified_cli_ext.providers.pi:PLUGIN",
    "poolside": "unified_cli_ext.providers.poolside:PLUGIN",
    "qoder": "unified_cli_ext.providers.qoder:PLUGIN",
    "qwen": "unified_cli_ext.providers.qwen:PLUGIN",
}


class SingleDistributionError(RuntimeError):
    """The installed wheel does not match the unified release contract."""


def _fail(message: str) -> None:
    raise SingleDistributionError(message)


def _extension_modules() -> tuple:
    return tuple(
        sorted(name for name in sys.modules if name.startswith("unified_cli_ext"))
    )


def verify_installed(expected_version: str) -> None:
    original_entry_points = importlib_metadata.entry_points

    def discovery_forbidden(*args, **kwargs):
        _fail("default Core path enumerated extension entry points")

    importlib_metadata.entry_points = discovery_forbidden
    try:
        unified_cli = importlib.import_module("unified_cli")
        if unified_cli.__version__ != expected_version:
            _fail("Core namespace version does not match")
        if tuple(unified_cli.PROVIDERS) != ("claude", "codex", "gemini"):
            _fail("Core provider defaults changed")
        descriptors = unified_cli.list_providers()
        if tuple(item.id for item in descriptors) != (
            "claude",
            "codex",
            "gemini",
        ):
            _fail("default provider listing is not Core-only")
        if _extension_modules():
            _fail("default Core import loaded the extension namespace")
    finally:
        importlib_metadata.entry_points = original_entry_points

    if importlib_metadata.version("unified-cli") != expected_version:
        _fail("unified-cli distribution version does not match")
    try:
        importlib_metadata.distribution("unified-cli-ext")
    except importlib_metadata.PackageNotFoundError:
        pass
    else:
        _fail("legacy unified-cli-ext distribution metadata is installed")

    distribution = importlib_metadata.distribution("unified-cli")
    provider_entry_points = tuple(
        entry
        for entry in distribution.entry_points
        if entry.group == "unified_cli.providers.v1"
    )
    actual = {entry.name: entry.value for entry in provider_entry_points}
    if len(provider_entry_points) != len(actual):
        _fail("provider entry-point names are duplicated")
    if actual != EXPECTED_PROVIDER_ENTRY_POINTS:
        _fail("provider entry-point inventory does not match")

    descriptors = unified_cli.list_providers(include_ext=True)
    if {item.id for item in descriptors if item.source == "extension"} != set(
        EXPECTED_PROVIDER_ENTRY_POINTS
    ):
        _fail("metadata-only extension discovery does not match")
    if any(
        item.status != "discovered"
        for item in descriptors
        if item.source == "extension"
    ):
        _fail("metadata-only extension discovery loaded provider code")
    if _extension_modules():
        _fail("metadata-only discovery imported the extension namespace")

    original_popen = subprocess.Popen
    original_run = subprocess.run
    original_create_connection = socket.create_connection

    def process_forbidden(*args, **kwargs):
        _fail("provider metadata import spawned a subprocess")

    def network_forbidden(*args, **kwargs):
        _fail("provider metadata import attempted a network connection")

    subprocess.Popen = process_forbidden
    subprocess.run = process_forbidden
    socket.create_connection = network_forbidden
    try:
        plugins = [entry.load() for entry in provider_entry_points]
    finally:
        subprocess.Popen = original_popen
        subprocess.run = original_run
        socket.create_connection = original_create_connection

    support = {plugin.id: plugin.support_status for plugin in plugins}
    if support.get("grok") != "preview":
        _fail("Grok is not Preview")
    experimental = {"qoder", "kilo", "poolside"}
    if {key for key, value in support.items() if value == "experimental"} != experimental:
        _fail("experimental provider inventory does not match")
    if {key for key, value in support.items() if value == "held"} != (
        set(EXPECTED_PROVIDER_ENTRY_POINTS) - {"grok", *experimental}
    ):
        _fail("held provider inventory does not match")
    if any(plugin.server_policy.enabled for plugin in plugins):
        _fail("an extension provider is enabled for server use")
    if "acp" in sys.modules or "mcp" in sys.modules:
        _fail("provider metadata import loaded an optional SDK")

    unified_cli_ext = importlib.import_module("unified_cli_ext")
    if unified_cli_ext.__version__ != expected_version:
        _fail("extension namespace version does not match")
    if "acp" in sys.modules or "mcp" in sys.modules:
        _fail("extension API import loaded an optional SDK")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_installed(args.version)
    except (SingleDistributionError, OSError, ImportError) as exc:
        print("single-distribution verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified installed unified-cli " + args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
