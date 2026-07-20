#!/usr/bin/env python3
"""Verify that Core and Ext wheels are separate, compatible distributions."""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence


_MAX_METADATA_BYTES = 1_000_000
_MAX_WHEEL_MEMBERS = 100_000
_DIST_INFO_RE = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.+!-]+\.dist-info$")


class VerificationError(ValueError):
    """Raised when a built distribution violates the Core/Ext boundary."""


@dataclass(frozen=True)
class WheelContents:
    path: Path
    names: frozenset[str]
    distribution: str
    version: str
    requirements: tuple[str, ...]


def _safe_names(infos: Iterable[zipfile.ZipInfo]) -> frozenset[str]:
    names: set[str] = set()
    for index, info in enumerate(infos):
        if index >= _MAX_WHEEL_MEMBERS:
            raise VerificationError("wheel contains too many members")
        name = info.filename
        if not name or info.is_dir():
            continue
        if "\\" in name or "\x00" in name or name.startswith("/"):
            raise VerificationError(f"unsafe wheel member: {name!r}")
        parts = PurePosixPath(name).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise VerificationError(f"unsafe wheel member: {name!r}")
        normalized = "/".join(parts)
        if normalized in names:
            raise VerificationError(f"duplicate wheel member: {normalized!r}")
        names.add(normalized)
    return frozenset(names)


def inspect_wheel(path: Path) -> WheelContents:
    if path.suffix != ".whl" or not path.is_file():
        raise VerificationError(f"not a wheel file: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = _safe_names(archive.infolist())
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise VerificationError(
                    f"wheel must contain exactly one METADATA file: {path}"
                )
            metadata_info = archive.getinfo(metadata_names[0])
            if metadata_info.file_size > _MAX_METADATA_BYTES:
                raise VerificationError(f"wheel METADATA is too large: {path}")
            metadata = BytesParser(policy=compat32).parsebytes(
                archive.read(metadata_info)
            )
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise VerificationError(f"could not inspect wheel: {path}") from exc

    distributions = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(distributions) != 1 or len(versions) != 1:
        raise VerificationError(f"wheel must contain one Name and Version: {path}")
    distribution = distributions[0].strip()
    version = versions[0].strip()
    requirements = tuple(metadata.get_all("Requires-Dist", []))
    if not distribution or not version:
        raise VerificationError(f"wheel has incomplete metadata: {path}")
    return WheelContents(path, names, distribution, version, requirements)


def _top_level_roots(names: Iterable[str]) -> frozenset[str]:
    return frozenset(name.split("/", 1)[0] for name in names)


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _verify_ext_layout(ext: WheelContents) -> None:
    for root in _top_level_roots(ext.names):
        if root == "unified_cli_ext":
            continue
        if root.endswith(".dist-info") and _DIST_INFO_RE.fullmatch(root):
            if root.startswith("unified_cli_ext-"):
                continue
        raise VerificationError(
            "Ext wheel contains a path outside unified_cli_ext: " + root
        )


def _verify_core_requirement(requirements: Iterable[str]) -> None:
    matches = [
        requirement for requirement in requirements
        if _normalized_distribution(
            re.split(r"[ (<>=!~;\[]", requirement, maxsplit=1)[0]
        ) == "unified-cli"
    ]
    if len(matches) != 1:
        raise VerificationError(
            "Ext wheel must declare exactly one unified-cli requirement"
        )
    requirement = matches[0]
    if ";" in requirement or "[" in requirement:
        raise VerificationError(
            "Ext wheel must require the compatible unified-cli 0.5.x line"
        )
    matched = re.fullmatch(
        r"\s*unified[-_.]cli\s*(?:\(([^()]*)\)|([^()]*))\s*",
        requirement,
        flags=re.IGNORECASE,
    )
    if matched is None:
        raise VerificationError(
            "Ext wheel must require the compatible unified-cli 0.5.x line"
        )
    specifiers = (matched.group(1) or matched.group(2) or "").replace(" ", "")
    parts = specifiers.split(",")
    if len(parts) != 2 or set(parts) != {">=0.5", "<0.6"}:
        raise VerificationError(
            "Ext wheel must require exactly the unified-cli 0.5.x line "
            "(>=0.5,<0.6)"
        )


def verify_pair(
    core_path: Path,
    ext_path: Path,
    *,
    core_version: Optional[str] = None,
    ext_version: Optional[str] = None,
) -> tuple[WheelContents, WheelContents]:
    core = inspect_wheel(core_path)
    ext = inspect_wheel(ext_path)

    if _normalized_distribution(core.distribution) != "unified-cli":
        raise VerificationError("Core wheel distribution name is not unified-cli")
    if _normalized_distribution(ext.distribution) != "unified-cli-ext":
        raise VerificationError("Ext wheel distribution name is not unified-cli-ext")
    if core_version is not None and core.version != core_version:
        raise VerificationError(
            f"Core wheel version {core.version!r} != {core_version!r}"
        )
    if ext_version is not None and ext.version != ext_version:
        raise VerificationError(
            f"Ext wheel version {ext.version!r} != {ext_version!r}"
        )

    overlap = core.names & ext.names
    if overlap:
        sample = ", ".join(sorted(overlap)[:5])
        raise VerificationError(f"Core and Ext wheel paths overlap: {sample}")
    if "unified_cli_ext" in _top_level_roots(core.names):
        raise VerificationError("Core wheel unexpectedly contains unified_cli_ext")
    if "unified_cli" in _top_level_roots(ext.names):
        raise VerificationError("Ext wheel unexpectedly contains unified_cli")

    _verify_ext_layout(ext)
    _verify_core_requirement(ext.requirements)
    return core, ext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("core_wheel", type=Path)
    parser.add_argument("ext_wheel", type=Path)
    parser.add_argument("--core-version")
    parser.add_argument("--ext-version")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        core, ext = verify_pair(
            args.core_wheel,
            args.ext_wheel,
            core_version=args.core_version,
            ext_version=args.ext_version,
        )
    except VerificationError as exc:
        print(f"distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"verified {core.distribution} {core.version} + "
        f"{ext.distribution} {ext.version}: no overlapping wheel paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
