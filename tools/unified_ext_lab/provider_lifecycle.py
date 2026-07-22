"""Durable Stage-6C lifecycle for accountless provider identity probes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Optional

from .docker import DockerCleanupSpec, DockerLabSpec, GuestAction
from .errors import LabError, UsageStateError
from .evidence import (
    ACCOUNTLESS_EXECUTOR_KIND,
    AccountlessProviderPlatform,
    ArtifactEvidence,
    SchemaHashes,
    capture_draft,
)
from .lifecycle import FixtureLifecycle
from .model import ResourceRole
from .provider_profiles import AccountlessProviderProfile
from .state import (
    PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE,
    StatePhase,
)


_MANIFEST_SCHEMA_DESCRIPTOR = (
    b"unified-ext-lab/provider-accountless/evidence/v1:artifact,captured_at_ns,"
    b"cleanup,evidence_kind,executor_kind,lab_id,manifest_schema_sha256,"
    b"operations,observed_protocol_schema_sha256,promotion_eligible,provider_id,"
    b"result,schema"
)
_PROTOCOL_SCHEMA_DESCRIPTOR = (
    b"unified-ext-lab/provider-accountless/probe/v1:action,help_probe,"
    b"profile_sha256,status_probe,version_probe"
)
_MANIFEST_SCHEMA_SHA256 = hashlib.sha256(_MANIFEST_SCHEMA_DESCRIPTOR).hexdigest()
_PROTOCOL_SCHEMA_SHA256 = hashlib.sha256(_PROTOCOL_SCHEMA_DESCRIPTOR).hexdigest()


def profile_artifact(profile: AccountlessProviderProfile) -> ArtifactEvidence:
    if type(profile) is not AccountlessProviderProfile:
        raise UsageStateError("invalid provider evidence profile")
    digest = profile.profile_sha256
    return ArtifactEvidence(
        package="{}-accountless-profile".format(profile.provider_id),
        version=profile.version,
        source_kind="repository",
        source_locator="provider-profiles/{}/{}-{}".format(
            profile.provider_id, profile.version, digest[:12]
        ),
        sha256=digest,
    )


class ProviderLifecycle(FixtureLifecycle):
    """Reuse exact-ID cleanup while replacing the synthetic probe contract."""

    def __init__(
        self,
        store: object,
        spec: object,
        runner: object,
        provider_profile: Optional[AccountlessProviderProfile],
        *,
        command_builder: object,
        runtime_snapshot: object,
        timeout: float = 30.0,
        monotonic_ns: object = None,
        evidence_clock_ns: object = None,
        readiness_monotonic: object = None,
        readiness_sleep: object = None,
    ) -> None:
        if type(spec) is DockerLabSpec and type(provider_profile) is not AccountlessProviderProfile:
            raise UsageStateError("invalid provider lifecycle profile")
        if type(spec) is DockerCleanupSpec and provider_profile is not None:
            raise UsageStateError("cleanup lifecycle cannot depend on a current profile")
        if (
            provider_profile is not None
            and spec.identity.provider_id != provider_profile.provider_id
        ):
            raise UsageStateError("provider lifecycle identity mismatch")
        keyword = {
            "timeout": timeout,
            "execution_profile": PROVIDER_ACCOUNTLESS_EXECUTION_PROFILE,
            "executor_kind": ACCOUNTLESS_EXECUTOR_KIND,
            "command_builder": command_builder,
            "runtime_snapshot": runtime_snapshot,
        }
        if monotonic_ns is not None:
            keyword["monotonic_ns"] = monotonic_ns
        if evidence_clock_ns is not None:
            keyword["evidence_clock_ns"] = evidence_clock_ns
        if readiness_monotonic is not None:
            keyword["readiness_monotonic"] = readiness_monotonic
        if readiness_sleep is not None:
            keyword["readiness_sleep"] = readiness_sleep
        super().__init__(store, spec, runner, **keyword)
        required = (
            "connect_install_network",
            "disconnect_install_network",
        )
        if type(spec) is DockerLabSpec and any(
            not callable(getattr(command_builder, name, None)) for name in required
        ):
            raise UsageStateError("provider install network grammar is unavailable")
        self._provider_profile = provider_profile

    @property
    def provider_profile(self) -> AccountlessProviderProfile:
        if self._provider_profile is None:
            raise UsageStateError("current provider profile is cleanup-inaccessible")
        return self._provider_profile

    def _artifact_from_spec(self) -> ArtifactEvidence:
        self._require_forward_spec()
        return profile_artifact(self.provider_profile)

    @staticmethod
    def _schema_hashes() -> SchemaHashes:
        return SchemaHashes(
            manifest_schema_sha256=_MANIFEST_SCHEMA_SHA256,
            observed_protocol_schema_sha256=_PROTOCOL_SCHEMA_SHA256,
        )

    def _evidence_platform(self) -> AccountlessProviderPlatform:
        return AccountlessProviderPlatform()

    def install(
        self, *, allow_network: bool = False, allow_install: bool = False
    ):
        """Install only with two exact acknowledgements and complete locks."""

        if allow_network is not True or allow_install is not True:
            raise UsageStateError(
                "provider install requires --allow-network and --allow-install"
            )
        self._provider_profile.require_install_ready()
        self._require_forward_spec()
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.CREATED, StatePhase.INSTALL_PENDING
            )
            started = self._now()
            failure: Optional[LabError] = None
            network_attempted = False
            result = None
            try:
                container_id = self._inspect(ResourceRole.CONTAINER)
                target = self._resource_target(
                    pending, ResourceRole.CONTAINER, container_id
                )
                assert isinstance(target, str)
                # Once the connect mutation is submitted its outcome may be
                # uncertain (for example, a client timeout after the daemon
                # attached the network).  Always issue the exact-ID forced
                # disconnect from this point onward, even when connect itself
                # reports failure.
                network_attempted = True
                self._execute(self._commands.connect_install_network(target))
                result = self._execute(
                    self._guest_command(
                        pending, GuestAction.INSTALL, container_id
                    )
                )
            except LabError as error:
                failure = error
            finally:
                if network_attempted:
                    try:
                        self._execute(
                            self._commands.disconnect_install_network(target)
                        )
                    except LabError as error:
                        if failure is None:
                            failure = error
                try:
                    final_container_id = self._inspect(ResourceRole.CONTAINER)
                    self._resource_target(
                        pending,
                        ResourceRole.CONTAINER,
                        final_container_id,
                    )
                except LabError as error:
                    if failure is None:
                        failure = error
            if failure is not None:
                locked.fail_pending(
                    StatePhase.INSTALL_PENDING,
                    self._observation("install", started, error=failure),
                )
                raise failure
            assert result is not None
            try:
                self._require_mapping(
                    result,
                    {
                        "action": "install",
                        "profile_sha256": self._provider_profile.profile_sha256,
                        "status": "ok",
                    },
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.INSTALL_PENDING,
                    self._observation("install", started, error=error),
                )
                raise
            observation = self._observation("install", started)
            return locked.transition(
                StatePhase.INSTALL_PENDING,
                StatePhase.INSTALLED,
                generation=pending.generation + 1,
                operations=self._append(pending, observation),
            )

    def test(self):
        """Run only version/help and the profile's documented status probe."""

        self._require_forward_spec()
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.INSTALLED, StatePhase.TEST_PENDING
            )
            started = self._now()
            try:
                container_id = self._inspect(ResourceRole.CONTAINER)
                self._resource_target(
                    pending, ResourceRole.CONTAINER, container_id
                )
                result = self._execute(
                    self._guest_command(
                        pending, GuestAction.TEST, container_id
                    )
                )
                self._require_mapping(
                    result,
                    {
                        "action": "test",
                        "help_probe": "passed",
                        "profile_sha256": self._provider_profile.profile_sha256,
                        "status_probe": (
                            "passed"
                            if self._provider_profile.status_argv is not None
                            else "not_supported"
                        ),
                        "version_probe": "passed",
                    },
                    test_result=True,
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.TEST_PENDING,
                    self._observation("test", started, error=error),
                )
                raise
            observation = self._observation("test", started)
            return locked.transition(
                StatePhase.TEST_PENDING,
                StatePhase.TESTED,
                operations=self._append(pending, observation),
            )

    def evidence(self):
        """Capture only bounded accountless identity outcomes."""

        self._require_forward_spec()
        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            pending = locked.transition(
                StatePhase.TESTED, StatePhase.EVIDENCE_PENDING
            )
            started = self._now()
            try:
                observation = self._observation("evidence", started)
                operations = self._append(pending, observation)
                captured_state = replace(pending, operations=operations)
                draft = capture_draft(
                    captured_state,
                    self._artifact(captured_state),
                    self._evidence_platform(),
                    self._schema_hashes(),
                    clock_ns=self._evidence_clock_ns,
                )
            except LabError as error:
                locked.fail_pending(
                    StatePhase.EVIDENCE_PENDING,
                    self._observation("evidence", started, error=error),
                )
                raise
            return locked.transition(
                StatePhase.EVIDENCE_PENDING,
                StatePhase.EVIDENCE_CAPTURED,
                operations=operations,
                draft_evidence=draft,
            )

    def logout(self):
        """Record accountless logout without invoking a vendor auth command.

        The container HOME is a provider-scoped tmpfs and credentials are never
        mounted or created.  Vendor logout could mutate an unrelated signed-in
        account if this invariant ever regressed, so Stage 6C deliberately
        performs no vendor command; container destruction discards the tmpfs.
        """

        with self._store.locked(self._spec.identity.lab_id) as locked:
            current = locked.load()
            self._require_identity(current)
            current = self._persist_mutation_hold(locked, current)
            if current.phase not in (
                StatePhase.EVIDENCE_CAPTURED,
                StatePhase.RECOVERY_REQUIRED,
            ):
                raise UsageStateError("logout is unavailable in this lifecycle phase")
            pending = locked.transition(current.phase, StatePhase.LOGOUT_PENDING)
            started = self._now()
            observation = self._observation("logout", started)
            return locked.transition(
                StatePhase.LOGOUT_PENDING,
                StatePhase.LOGOUT_DONE,
                auth_generation=pending.auth_generation + 1,
                operations=self._append(pending, observation),
            )


__all__ = ["ProviderLifecycle", "profile_artifact"]
