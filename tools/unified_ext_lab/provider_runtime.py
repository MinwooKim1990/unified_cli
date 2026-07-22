"""Real-Docker routing for the held Stage-6C provider profiles."""

from __future__ import annotations

from .docker import DockerLabSpec, load_snapshot_image_context
from .docker_runtime import (
    DerivedSnapshotResource,
    RealDockerRuntime,
    discover_docker_executable,
)
from .errors import InvariantRefusalError, UsageStateError
from .profile import RealDockerProfile, load_real_docker_profile
from .provider_docker import ProviderDockerCommandBuilder
from .provider_profiles import AccountlessProviderProfile
from .runner import Runner, SubprocessRunner


class ProviderDockerRuntime(RealDockerRuntime):
    """Bind one immutable provider profile to the existing hardened runtime."""

    def __init__(
        self,
        executable: str,
        runner: Runner,
        docker_profile: RealDockerProfile,
        provider_profile: AccountlessProviderProfile,
        *,
        timeout: float = 30.0,
    ) -> None:
        if type(provider_profile) is not AccountlessProviderProfile:
            raise UsageStateError("invalid accountless provider runtime")
        super().__init__(
            executable,
            runner,
            docker_profile,
            timeout=timeout,
            cleanup_only=False,
        )
        self.provider_profile = provider_profile

    def preflight(self) -> None:
        super().preflight()
        runtime_lock = self.provider_profile.runtime_lock
        if runtime_lock is None:
            return
        if (
            self.profile.base_reference != runtime_lock.base_reference
            or self.profile.operating_system != runtime_lock.operating_system
            or self.profile.architecture != runtime_lock.architecture
            or self._local_base_id != runtime_lock.base_image_id
        ):
            raise InvariantRefusalError(
                "provider runtime/platform lock does not match Docker preflight"
            )

    def capture_snapshot(self, root: str) -> None:
        super().capture_snapshot(root)
        assert self._snapshot_resource is not None
        self._snapshot_resource.seal_identity()

    @classmethod
    def discover(
        cls, provider_profile: AccountlessProviderProfile
    ) -> "ProviderDockerRuntime":
        profile = load_real_docker_profile()
        profile.require_routable()
        executable = discover_docker_executable()
        runner = SubprocessRunner(executable)
        try:
            return cls(executable, runner, profile, provider_profile)
        except BaseException:
            runner.close()
            raise

    def bind_existing_snapshot(self, root: str) -> None:
        """Bind only the deterministic state-derived snapshot for forward work."""

        if self._local_base_id is None:
            raise UsageStateError("provider Docker preflight is required")
        if self._snapshot_resource is not None or self._snapshot is not None:
            raise UsageStateError("provider runtime snapshot is already bound")
        resource = DerivedSnapshotResource(root)
        if not resource.present():
            raise InvariantRefusalError("provider runtime snapshot is unavailable")
        resource.validate_identity()
        snapshot = load_snapshot_image_context(root)
        # Publish the three related runtime fields only after the existing
        # snapshot has passed the complete read-only validation above.
        self._snapshot_resource = resource
        self._snapshot_root = root
        self._snapshot = snapshot

    def commands(self, spec: DockerLabSpec) -> ProviderDockerCommandBuilder:
        if type(spec) is not DockerLabSpec:
            raise UsageStateError("provider runtime requires a forward Docker spec")
        if spec.docker_executable != self.executable:
            raise InvariantRefusalError("provider Docker executable drift")
        if type(self.profile) is not RealDockerProfile:
            raise UsageStateError("provider Docker base profile is unavailable")
        commands = ProviderDockerCommandBuilder(
            spec, self.profile, self.provider_profile
        )
        commands.validate_accountless_boundary()
        return commands


__all__ = ["ProviderDockerRuntime"]
