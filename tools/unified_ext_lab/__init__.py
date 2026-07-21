"""Validated, import-safe identity models for the Stage 6 extension lab."""

from .errors import (
    CleanupIncompleteError,
    InvariantRefusalError,
    LabError,
    RunnerFailureError,
    TestFailureError,
    UnsupportedError,
    UsageStateError,
)
from .model import (
    LABEL_PREFIX,
    RESOURCE_NAME_MAX_LENGTH,
    LabIdentity,
    LabLifecycle,
    LabResource,
    LabResourceSet,
    ResourceRole,
    make_resource_name,
    ownership_labels,
    validate_lab_id,
    validate_ownership_token,
    validate_provider_id,
)

__all__ = [
    "CleanupIncompleteError",
    "InvariantRefusalError",
    "LABEL_PREFIX",
    "LabError",
    "LabIdentity",
    "LabLifecycle",
    "LabResource",
    "LabResourceSet",
    "RESOURCE_NAME_MAX_LENGTH",
    "ResourceRole",
    "RunnerFailureError",
    "TestFailureError",
    "UnsupportedError",
    "UsageStateError",
    "make_resource_name",
    "ownership_labels",
    "validate_lab_id",
    "validate_ownership_token",
    "validate_provider_id",
]
