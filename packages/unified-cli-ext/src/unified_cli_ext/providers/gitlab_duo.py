"""Opt-in Preview adapter for the official GitLab Duo CLI."""

from __future__ import annotations

from .bridge import adapter_plugin
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    OperationLimits,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .path_resolver import path_launch_resolver


GITLAB_DUO_OFFICIAL_SOURCES = (
    "https://docs.gitlab.com/user/gitlab_duo_cli/use/",
    "https://docs.gitlab.com/user/gitlab_duo_cli/reference/",
)
GITLAB_DUO_DEFAULT_MODEL = "default"
GITLAB_DUO_OFFICIAL_PACKAGE = "@gitlab/duo-cli"
GITLAB_DUO_HEADLESS_FIXED_ARGV = ("run",)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="gitlab-duo",
    display_name="GitLab Duo CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="duo",
        expected_identity="duo",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="duo ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("run", "--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "--goal"},
            identity_marker="--goal",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=GITLAB_DUO_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="--goal",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(
            (
                "GITLAB_TOKEN",
                "GITLAB_OAUTH_TOKEN",
                "GITLAB_BASE_URL",
                "GITLAB_URL",
                "GITLAB_DUO_MODEL",
            )
        )
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=GITLAB_DUO_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="gitlab-duo",
        executable="duo",
        package_names=(GITLAB_DUO_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "GITLAB_DUO_DEFAULT_MODEL",
    "GITLAB_DUO_HEADLESS_FIXED_ARGV",
    "GITLAB_DUO_OFFICIAL_PACKAGE",
    "GITLAB_DUO_OFFICIAL_SOURCES",
    "PLUGIN",
]
