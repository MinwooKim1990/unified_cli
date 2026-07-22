"""Checked-in non-provider supply fixture used only by local unit tests."""

from tools.unified_ext_lab.provider_profiles import (
    AcquisitionKind,
    AccountlessProviderProfile,
    SupplyManifestSource,
)


SYNTHETIC_SUPPLY_SOURCE = SupplyManifestSource(
    filename="synthetic-readiness.supply.v1.json",
    sha256="c248340e02a38e6b3ed279254bfe69927786f10596e56c3f8209ffd2ff8d8fbb",
    entry_count=3,
    fixture_only=True,
)


def ready_synthetic_profile() -> AccountlessProviderProfile:
    """Return the non-routable fixture profile after actual manifest parsing."""

    return AccountlessProviderProfile(
        provider_id="synthetic-fixture",
        vendor="Synthetic local fixture",
        package_name="@example/unified-ext-provider-fixture",
        version="0.0.1",
        binary="fixture-provider",
        version_argv=("fixture-provider", "--version"),
        help_argv=("fixture-provider", "--help"),
        status_argv=None,
        acquisition_kind=AcquisitionKind.NPM_REGISTRY_INTEGRITY,
        acquisition_locator="npm/example/unified-ext-provider-fixture/0.0.1",
        supply_manifest_source=SYNTHETIC_SUPPLY_SOURCE,
        hold_reason="synthetic parser fixture only",
        fixture_only=True,
    )


__all__ = ["SYNTHETIC_SUPPLY_SOURCE", "ready_synthetic_profile"]
