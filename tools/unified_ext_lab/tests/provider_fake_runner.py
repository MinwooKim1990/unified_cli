"""Deterministic in-memory runner for Stage-6C provider tests."""

from __future__ import annotations

import json

from tools.unified_ext_lab.docker import (
    DockerCommandBuilder,
    DockerLabSpec,
    DockerOperation,
    GuestAction,
    classify_docker_argv,
    validate_inspect,
)
from tools.unified_ext_lab.errors import InvariantRefusalError, UsageStateError
from tools.unified_ext_lab.fake_docker import FakeRunner
from tools.unified_ext_lab.model import LABEL_PREFIX, ResourceRole
from tools.unified_ext_lab.provider_profiles import AccountlessProviderProfile
from tools.unified_ext_lab.provider_docker import validate_provider_offline_networks
from tools.unified_ext_lab.runner import CommandResult


FAKE_IMAGE_ID = "sha256:" + "a" * 64
FAKE_CONTAINER_ID = "b" * 64


class ProviderFakeRunner(FakeRunner):
    """Recognize only the fake builder's two network and two guest actions."""

    def __init__(self, profile: AccountlessProviderProfile) -> None:
        super().__init__()
        self.profile = profile
        self.network_connected = False
        self.fail_network_connect_after_mutation = False
        self.ineffective_disconnect = False
        self.fail_provider_action = None

    def _provider_container(self):
        candidates = []
        for record in self.containers.values():
            config = record.get("Config", {})
            labels = config.get("Labels", {}) if type(config) is dict else {}
            if labels.get(LABEL_PREFIX + "/provider") == self.profile.provider_id:
                candidates.append(record)
        if len(candidates) != 1:
            raise AssertionError("provider fake container identity drift")
        return candidates[0]

    def run(self, argv, *, timeout):
        if len(argv) >= 2 and argv[1] == "provider-network-connect":
            self.commands.append(tuple(argv))
            self.network_connected = True
            networks = self._provider_container()["NetworkSettings"]["Networks"]
            networks["bridge"] = {
                "Gateway": "172.17.0.1",
                "IPAddress": "172.17.0.2",
                "IPPrefixLen": 16,
                "MacAddress": "02:42:ac:11:00:02",
            }
            if self.fail_network_connect_after_mutation:
                return CommandResult(
                    tuple(argv), 5, "", "ambiguous fake network failure"
                )
            return CommandResult(tuple(argv), 0, "", "")
        if len(argv) >= 2 and argv[1] == "provider-network-disconnect":
            self.commands.append(tuple(argv))
            if not self.ineffective_disconnect:
                self._provider_container()["NetworkSettings"]["Networks"].pop(
                    "bridge", None
                )
                self.network_connected = False
            return CommandResult(tuple(argv), 0, "", "")
        if len(argv) >= 3 and argv[1] == "provider-guest":
            self.commands.append(tuple(argv))
            action = argv[2]
            if action == self.fail_provider_action:
                return CommandResult(tuple(argv), 5, "", "fake provider failure")
            if action == GuestAction.INSTALL.value:
                payload = {
                    "action": "install",
                    "profile_sha256": self.profile.profile_sha256,
                    "status": "ok",
                }
            elif action == GuestAction.TEST.value:
                payload = {
                    "action": "test",
                    "help_probe": "passed",
                    "profile_sha256": self.profile.profile_sha256,
                    "status_probe": (
                        "passed"
                        if self.profile.status_argv is not None
                        else "not_supported"
                    ),
                    "version_probe": "passed",
                }
            else:
                raise AssertionError("unexpected provider fake guest action")
            return CommandResult(
                tuple(argv),
                0,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "",
            )
        result = super().run(argv, timeout=timeout)
        operation = classify_docker_argv(argv, self._operations)
        if operation is DockerOperation.LIST_IMAGE and result.stdout:
            return CommandResult(
                result.argv,
                result.returncode,
                FAKE_IMAGE_ID + "\n",
                result.stderr,
            )
        if operation is DockerOperation.LIST_CONTAINER and result.stdout:
            return CommandResult(
                result.argv,
                result.returncode,
                FAKE_CONTAINER_ID + "\n",
                result.stderr,
            )
        return result


class ProviderFakeCommands:
    """ID-bound facade over the existing fully in-memory Docker model."""

    uses_resource_ids = True
    builds_image = False
    planned_roles = (ResourceRole.CONTAINER,)
    cleanup_roles = (ResourceRole.CONTAINER,)
    create_volume_roles = ()

    def __init__(
        self, spec: DockerLabSpec, profile: AccountlessProviderProfile
    ) -> None:
        self._spec = spec
        self._profile = profile
        self._base = DockerCommandBuilder(spec)

    def __getattr__(self, name):
        return getattr(self._base, name)

    @staticmethod
    def _resource_id(role: ResourceRole) -> str:
        if role is ResourceRole.IMAGE:
            return FAKE_IMAGE_ID
        if role is ResourceRole.CONTAINER:
            return FAKE_CONTAINER_ID
        raise UsageStateError("provider fake role has no immutable id")

    def inspect(self, role: ResourceRole, resource_id=None):
        if resource_id is not None and resource_id != self._resource_id(role):
            raise InvariantRefusalError("provider fake resource id drift")
        return self._base.inspect(role)

    def list_identity(self, role: ResourceRole):
        self._resource_id(role)
        return self._base.list_owned(role)

    def exec_guest(self, action, resource_id, extra=()):
        if resource_id != FAKE_CONTAINER_ID or extra:
            raise InvariantRefusalError("provider fake guest target drift")
        if action is GuestAction.READY:
            return self._base.exec_guest(action)
        if action not in (GuestAction.INSTALL, GuestAction.TEST):
            raise InvariantRefusalError("provider fake guest action drift")
        return (
            self._spec.docker_executable,
            "provider-guest",
            action.value,
            self._profile.provider_id,
        )

    def connect_install_network(self, resource_id):
        if resource_id != FAKE_CONTAINER_ID:
            raise InvariantRefusalError("provider fake network target drift")
        return (
            self._spec.docker_executable,
            "provider-network-connect",
            self._profile.provider_id,
        )

    def disconnect_install_network(self, resource_id):
        if resource_id != FAKE_CONTAINER_ID:
            raise InvariantRefusalError("provider fake network target drift")
        return (
            self._spec.docker_executable,
            "provider-network-disconnect",
            self._profile.provider_id,
        )

    def start_container(self, resource_id):
        if resource_id != FAKE_CONTAINER_ID:
            raise InvariantRefusalError("provider fake resource id drift")
        return self._base.start_container()

    def stop_container(self, resource_id):
        if resource_id != FAKE_CONTAINER_ID:
            raise InvariantRefusalError("provider fake resource id drift")
        return self._base.stop_container()

    def remove_container(self, resource_id):
        if resource_id != FAKE_CONTAINER_ID:
            raise InvariantRefusalError("provider fake resource id drift")
        return self._base.remove_container()

    def remove_image(self, resource_id):
        if resource_id != FAKE_IMAGE_ID:
            raise InvariantRefusalError("provider fake resource id drift")
        return self._base.remove_image()

    def validate_inspect(self, role, payload):
        validate_inspect(self._spec, role, payload)
        if role is ResourceRole.CONTAINER:
            validate_provider_offline_networks(payload)
        return self._resource_id(role)

    def validate_cleanup_inspect(self, role, payload):
        return self.validate_inspect(role, payload)

    def validate_cleanup_identity_inspect(self, role, payload, expected_id):
        observed = self.validate_inspect(role, payload)
        if observed != expected_id:
            raise InvariantRefusalError("provider fake resource id drift")
        return observed


__all__ = [
    "FAKE_CONTAINER_ID",
    "FAKE_IMAGE_ID",
    "ProviderFakeCommands",
    "ProviderFakeRunner",
]
