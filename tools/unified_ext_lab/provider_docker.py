"""Finite Docker grammar for Stage-6C accountless provider probes."""

from __future__ import annotations

from typing import Tuple

from .docker import GUEST_EXECUTABLE, GuestAction, DockerLabSpec
from .docker_runtime import RealDockerCommandBuilder, _inspect_record
from .errors import InvariantRefusalError, UsageStateError
from .model import ResourceRole
from .profile import RealDockerProfile
from .provider_profiles import AccountlessProviderProfile


PROVIDER_GUEST_EXECUTABLE = "/opt/unified-ext-lab/provider_guest.py"
INSTALL_NETWORK = "bridge"


def _container_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise UsageStateError("invalid provider container id")
    return value


def validate_provider_offline_networks(payload: object) -> None:
    """Require no live attachment other than Docker's inert ``none`` endpoint."""

    record = _inspect_record(payload, "provider container inspect response")
    settings = record.get("NetworkSettings")
    if type(settings) is not dict or type(settings.get("Networks")) is not dict:
        raise InvariantRefusalError("provider network attachments are unavailable")
    networks = settings["Networks"]
    if set(networks) not in (set(), {"none"}):
        raise InvariantRefusalError("provider container has an active network")
    if not networks:
        return
    endpoint = networks["none"]
    if type(endpoint) is not dict:
        raise InvariantRefusalError("provider none-network endpoint is invalid")
    required_inert_fields = {
        "Gateway",
        "GlobalIPv6Address",
        "GlobalIPv6PrefixLen",
        "IPAddress",
        "IPPrefixLen",
        "IPv6Gateway",
        "MacAddress",
    }
    if not required_inert_fields.issubset(endpoint):
        raise InvariantRefusalError("provider none-network proof is incomplete")
    for field in (
        "Gateway",
        "IPAddress",
        "GlobalIPv6Address",
        "IPv6Gateway",
        "MacAddress",
    ):
        if endpoint.get(field) not in (None, ""):
            raise InvariantRefusalError("provider none-network endpoint is active")
    for field in ("IPPrefixLen", "GlobalIPv6PrefixLen"):
        value = endpoint.get(field)
        if value is not None and (type(value) is not int or value != 0):
            raise InvariantRefusalError("provider none-network endpoint is active")
    for field in ("Aliases", "DNSNames", "Links"):
        if endpoint.get(field) not in (None, []):
            raise InvariantRefusalError("provider none-network endpoint is active")
    for field in ("DriverOpts", "IPAMConfig"):
        if endpoint.get(field) not in (None, {}):
            raise InvariantRefusalError("provider none-network endpoint is active")


class ProviderDockerCommandBuilder(RealDockerCommandBuilder):
    """Extend the hardened container-only grammar with fixed provider actions.

    The container is created with ``--network none``.  A future install whose
    profile has complete source-controlled locks may attach that exact
    container to Docker's fixed bridge only for the bounded install action;
    the lifecycle always disconnects it before any accountless probe.
    """

    def __init__(
        self,
        spec: DockerLabSpec,
        docker_profile: RealDockerProfile,
        provider_profile: AccountlessProviderProfile,
    ) -> None:
        if type(provider_profile) is not AccountlessProviderProfile:
            raise UsageStateError("invalid provider Docker profile")
        if spec.identity.provider_id != provider_profile.provider_id:
            raise InvariantRefusalError("provider Docker identity mismatch")
        super().__init__(spec, docker_profile)
        self._provider_profile = provider_profile

    @property
    def provider_profile(self) -> AccountlessProviderProfile:
        return self._provider_profile

    def exec_guest(
        self,
        action: GuestAction,
        resource_id: str,
        extra: Tuple[str, ...] = (),
    ) -> Tuple[str, ...]:
        if not isinstance(action, GuestAction) or type(extra) is not tuple or extra:
            raise InvariantRefusalError("provider guest command arguments are fixed")
        if action is GuestAction.READY:
            return super().exec_guest(action, resource_id, extra)
        self._base.validate_context()
        argv = list(super().exec_guest(action, resource_id, extra))
        if tuple(argv[-2:]) != (GUEST_EXECUTABLE, action.value):
            raise InvariantRefusalError("provider guest command grammar drift")
        argv[-2:] = (
            PROVIDER_GUEST_EXECUTABLE,
            action.value,
            self._provider_profile.provider_id,
            self._provider_profile.profile_sha256,
        )
        return tuple(argv)

    def connect_install_network(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "network",
            "connect",
            INSTALL_NETWORK,
            _container_id(resource_id),
        )

    def disconnect_install_network(self, resource_id: str) -> Tuple[str, ...]:
        return self.prefix + (
            "network",
            "disconnect",
            "--force",
            INSTALL_NETWORK,
            _container_id(resource_id),
        )

    def validate_inspect(self, role: ResourceRole, payload: object) -> str:
        resource_id = super().validate_inspect(role, payload)
        if role is ResourceRole.CONTAINER:
            validate_provider_offline_networks(payload)
        return resource_id

    def accountless_command_grammar(self) -> Tuple[Tuple[str, ...], ...]:
        """Return fixed pre-ID probe forms for static audits.

        Provider commands are executed only inside the fixed guest and are not
        spliced into Docker argv.  This method intentionally returns the
        source-controlled forms without executing or persisting them.
        """

        commands = [
            self._provider_profile.version_argv,
            self._provider_profile.help_argv,
        ]
        if self._provider_profile.status_argv is not None:
            commands.append(self._provider_profile.status_argv)
        return tuple(commands)

    def validate_accountless_boundary(self) -> None:
        """Re-assert the inherited hardened container policy."""

        argv = self.create_container()
        required = (
            ("--user", "65532:65532"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges=true"),
            ("--network", "none"),
            ("--pids-limit", "128"),
            ("--memory", "1g"),
            ("--memory-swap", "1g"),
            ("--cpus", "1.0"),
        )
        if "--read-only" not in argv:
            raise InvariantRefusalError("provider root filesystem is not read-only")
        for pair in required:
            if not any(argv[index : index + 2] == pair for index in range(len(argv) - 1)):
                raise InvariantRefusalError("provider container boundary drift")
        forbidden = (
            "/var/run/docker.sock",
            ".ssh",
            ".gitconfig",
            "host.docker.internal",
            "--privileged",
            "--network=host",
        )
        # The Docker client itself uses the fixed local-engine endpoint.  Scan
        # only the container-create body so this check means "not mounted or
        # exposed inside the container", not "the client cannot reach Docker".
        flattened = "\0".join(argv[3:])
        if any(value in flattened for value in forbidden):
            raise InvariantRefusalError("provider container exposes a host capability")
        if self.planned_roles != (ResourceRole.CONTAINER,):
            raise InvariantRefusalError("provider resource plan drift")


__all__ = [
    "INSTALL_NETWORK",
    "PROVIDER_GUEST_EXECUTABLE",
    "ProviderDockerCommandBuilder",
    "validate_provider_offline_networks",
]
