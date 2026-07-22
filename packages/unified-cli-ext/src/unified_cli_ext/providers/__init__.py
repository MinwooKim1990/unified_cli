"""Lazy, provider-neutral adapter ABI.

Importing this package validates no installation metadata, resolves no binary,
and runs no provider probe.  Concrete distributions may expose a
``ProviderAdapterSpecV1`` and build Core plugin metadata only when their entry
point is explicitly loaded.
"""

from .contract import (
    PROVIDER_ADAPTER_ABI_V1,
    AdapterDescriptorV1,
    AdapterServerPolicy,
    AdapterStatus,
    AuthSpec,
    BinarySpec,
    BuiltPromptInvocation,
    DeclarativeProbeSpec,
    DoctorProbeSpec,
    DynamicArgument,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    JsonProbeSpec,
    ModelProbeSpec,
    OperationLimits,
    PromptCommandSpec,
    PromptMode,
    PromptSentinelPolicy,
    PlainTextFieldSpec,
    PlainTextProbeSpec,
    ProbeFormat,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    TransportConfig,
    VersionProbeSpec,
    describe_adapter,
    valid_provider_id,
)
from .registry import ProviderAdapterRegistryV1
from .installation import (
    INSTALLATION_RECEIPT_ABI_V1,
    ArtifactIdentityV1,
    DirectoryIdentityV1,
    DistributionTypeV1,
    InstallationReceiptKindV1,
    InstallationReceiptV1,
    SymlinkIdentityV1,
    VerifiedLaunchV1,
    installation_receipt_from_record,
    installation_receipt_to_record,
)
from .runtime import (
    AdapterInspectionV1,
    BinaryProvenance,
    InteractiveAuthSessionV1,
    OpenedProcessTransportV1,
    ProtocolLaunchBoundaryV1,
    ProviderAdapterV1,
    drain_pending_cleanups,
)


_BRIDGE_EXPORTS = frozenset(
    (
        "AdapterLaunchResolverV1",
        "AdapterFinalizerV1",
        "AdapterProviderBridge",
        "AdapterRecordMapperV1",
        "AdapterResponseMapperV1",
        "AdapterStateFactoryV1",
        "INSTALLATION_RECEIPT_MEDIA_TYPE_V1",
        "adapter_plugin",
        "installation_receipt_envelope",
        "installation_receipt_from_envelope",
        "provider_plugin",
    )
)


def __getattr__(name):
    """Load the Core-facing bridge only when explicitly requested."""

    if name not in _BRIDGE_EXPORTS:
        raise AttributeError(name)
    import importlib

    module = importlib.import_module(".bridge", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "INSTALLATION_RECEIPT_ABI_V1",
    "PROVIDER_ADAPTER_ABI_V1",
    "AdapterDescriptorV1",
    "AdapterFinalizerV1",
    "AdapterInspectionV1",
    "AdapterLaunchResolverV1",
    "AdapterProviderBridge",
    "AdapterRecordMapperV1",
    "AdapterResponseMapperV1",
    "AdapterServerPolicy",
    "AdapterStatus",
    "AdapterStateFactoryV1",
    "AuthSpec",
    "ArtifactIdentityV1",
    "BinaryProvenance",
    "BinarySpec",
    "BuiltPromptInvocation",
    "DeclarativeProbeSpec",
    "DoctorProbeSpec",
    "DynamicArgument",
    "DirectoryIdentityV1",
    "DistributionTypeV1",
    "EnvironmentPolicy",
    "ExitStatusProbeSpec",
    "FeatureProbeSpec",
    "FixedCommandSpec",
    "InteractiveAuthSessionV1",
    "InstallationReceiptKindV1",
    "InstallationReceiptV1",
    "INSTALLATION_RECEIPT_MEDIA_TYPE_V1",
    "JsonProbeSpec",
    "ModelProbeSpec",
    "OperationLimits",
    "OpenedProcessTransportV1",
    "PlainTextFieldSpec",
    "PlainTextProbeSpec",
    "PromptCommandSpec",
    "PromptMode",
    "PromptSentinelPolicy",
    "ProbeFormat",
    "ProtocolLaunchBoundaryV1",
    "ProviderAdapterRegistryV1",
    "ProviderAdapterSpecV1",
    "ProviderAdapterV1",
    "ProviderCapability",
    "SymlinkIdentityV1",
    "TransportKind",
    "TransportConfig",
    "VersionProbeSpec",
    "VerifiedLaunchV1",
    "adapter_plugin",
    "describe_adapter",
    "drain_pending_cleanups",
    "installation_receipt_envelope",
    "installation_receipt_from_envelope",
    "installation_receipt_from_record",
    "installation_receipt_to_record",
    "provider_plugin",
    "valid_provider_id",
]
