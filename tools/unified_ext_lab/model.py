"""Strict immutable identity and ownership models for the extension lab.

This module is pure Python: it does not access Docker, subprocesses, providers,
credentials, or the network.  It is intentionally small so later lab layers
can use its exact identity and ownership rules as a trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from re import compile as re_compile
from types import MappingProxyType
from typing import Mapping, Tuple, Union

from .errors import InvariantRefusalError, UsageStateError


LAB_ID_RE = re_compile(r"^[a-z][a-z0-9-]{0,31}$")
PROVIDER_ID_RE = re_compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
OWNERSHIP_TOKEN_RE = re_compile(r"^[0-9a-f]{32}$")

LABEL_PREFIX = "io.unified-cli.ext-lab"
RESOURCE_NAME_PREFIX = "unified-ext-lab"
RESOURCE_NAME_MAX_LENGTH = 128
TOKEN_SUFFIX_LENGTH = 32


class ResourceRole(str, Enum):
    """The complete, finite set of resources owned by a lab."""

    IMAGE = "image"
    CONTAINER = "container"
    WORKSPACE = "workspace"
    AUTH = "auth"
    TOOL = "tool"
    NETWORK = "network"


class LabLifecycle(str, Enum):
    """Lifecycle states which a future runner may transition between."""

    CREATING = "creating"
    ACTIVE = "active"
    CLEANUP_PENDING = "cleanup_pending"
    DESTROYED = "destroyed"


def validate_lab_id(value: object) -> str:
    """Return a valid lab identifier or raise a stable usage/state error."""

    if type(value) is not str or LAB_ID_RE.fullmatch(value) is None:
        raise UsageStateError("invalid lab id")
    return value


def validate_provider_id(value: object) -> str:
    """Return a valid Ext provider identifier or raise a stable error."""

    if (
        type(value) is not str
        or len(value) > 64
        or PROVIDER_ID_RE.fullmatch(value) is None
    ):
        raise UsageStateError("invalid provider id")
    return value


def validate_ownership_token(value: object) -> str:
    """Return a 32-character lowercase hexadecimal ownership token."""

    if type(value) is not str or OWNERSHIP_TOKEN_RE.fullmatch(value) is None:
        raise UsageStateError("invalid ownership token")
    return value


def _role(value: Union[ResourceRole, str]) -> ResourceRole:
    if isinstance(value, ResourceRole):
        return value
    if type(value) is str:
        try:
            return ResourceRole(value)
        except ValueError:
            pass
    raise UsageStateError("invalid resource role")


def _lifecycle(value: Union[LabLifecycle, str]) -> LabLifecycle:
    if isinstance(value, LabLifecycle):
        return value
    if type(value) is str:
        try:
            return LabLifecycle(value)
        except ValueError:
            pass
    raise UsageStateError("invalid lab lifecycle")


def make_resource_name(
    lab_id: object,
    provider_id: object,
    role: Union[ResourceRole, str],
    ownership_token: object,
) -> str:
    """Build the exact deterministic resource name without truncation.

    The complete 128-bit ownership token is part of the visible name as well as
    the ownership labels.  A name that would cross the hard length ceiling is
    refused rather than shortened, because shortening could make ownership
    ambiguous.
    """

    lab = validate_lab_id(lab_id)
    provider = validate_provider_id(provider_id)
    resource_role = _role(role)
    token = validate_ownership_token(ownership_token)
    name = "{}-{}-{}-{}-{}".format(
        RESOURCE_NAME_PREFIX, lab, provider, resource_role.value, token[-TOKEN_SUFFIX_LENGTH:]
    )
    if len(name) > RESOURCE_NAME_MAX_LENGTH:
        raise InvariantRefusalError("resource name exceeds maximum length")
    return name


def ownership_labels(
    lab_id: object,
    provider_id: object,
    role: Union[ResourceRole, str],
    ownership_token: object,
) -> Mapping[str, str]:
    """Return the exact immutable ownership-label set for one resource."""

    lab = validate_lab_id(lab_id)
    provider = validate_provider_id(provider_id)
    resource_role = _role(role)
    token = validate_ownership_token(ownership_token)
    return MappingProxyType(
        {
            LABEL_PREFIX + "/managed": "true",
            LABEL_PREFIX + "/schema": "1",
            LABEL_PREFIX + "/lab-id": lab,
            LABEL_PREFIX + "/provider": provider,
            LABEL_PREFIX + "/ownership-token": token,
            LABEL_PREFIX + "/role": resource_role.value,
        }
    )


@dataclass(frozen=True)
class LabIdentity:
    """The immutable identity shared by every resource in one lab."""

    lab_id: str
    provider_id: str
    ownership_token: str
    lifecycle: LabLifecycle = LabLifecycle.CREATING

    def __post_init__(self) -> None:
        object.__setattr__(self, "lab_id", validate_lab_id(self.lab_id))
        object.__setattr__(self, "provider_id", validate_provider_id(self.provider_id))
        object.__setattr__(
            self, "ownership_token", validate_ownership_token(self.ownership_token)
        )
        object.__setattr__(self, "lifecycle", _lifecycle(self.lifecycle))

    def resource(self, role: Union[ResourceRole, str]) -> "LabResource":
        """Create one validated resource identity under this lab."""

        return LabResource(identity=self, role=role)


@dataclass(frozen=True)
class LabResource:
    """An immutable owned resource with a deterministic name and labels."""

    identity: LabIdentity
    role: ResourceRole

    def __post_init__(self) -> None:
        if type(self.identity) is not LabIdentity:
            raise UsageStateError("invalid lab identity")
        object.__setattr__(self, "role", _role(self.role))

    @property
    def name(self) -> str:
        return make_resource_name(
            self.identity.lab_id,
            self.identity.provider_id,
            self.role,
            self.identity.ownership_token,
        )

    @property
    def labels(self) -> Mapping[str, str]:
        return ownership_labels(
            self.identity.lab_id,
            self.identity.provider_id,
            self.role,
            self.identity.ownership_token,
        )


@dataclass(frozen=True)
class LabResourceSet:
    """A validated immutable collection that refuses deterministic-name clashes."""

    resources: Tuple[LabResource, ...]

    def __post_init__(self) -> None:
        if type(self.resources) is not tuple:
            raise UsageStateError("resources must be an immutable tuple")
        names = set()
        for resource in self.resources:
            if type(resource) is not LabResource:
                raise UsageStateError("invalid lab resource")
            if resource.name in names:
                raise InvariantRefusalError("resource name collision")
            names.add(resource.name)
