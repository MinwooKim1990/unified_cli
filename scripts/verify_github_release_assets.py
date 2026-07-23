#!/usr/bin/env python3
"""Verify an exact final GitHub Release manifest against downloaded assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple


_MAX_RELEASE_JSON_BYTES = 1 * 1024 * 1024
_MAX_ASSET_BYTES = 256 * 1024 * 1024
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ReleaseAssetVerificationError(ValueError):
    """The release record or downloaded asset set is incomplete or inconsistent."""


def _expected_names(wheel_name: str, sdist_name: str) -> Tuple[str, str]:
    names = (wheel_name, sdist_name)
    for name in names:
        if (
            not name
            or Path(name).name != name
            or name in {".", ".."}
            or "\\" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise ReleaseAssetVerificationError("expected asset name is unsafe")
    if wheel_name == sdist_name:
        raise ReleaseAssetVerificationError("expected asset names are ambiguous")
    return names


def _release_payload(path: Path) -> object:
    try:
        mode = path.lstat().st_mode
        size = path.stat().st_size
    except OSError as exc:
        raise ReleaseAssetVerificationError("release JSON is unreadable") from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise ReleaseAssetVerificationError("release JSON is not a regular file")
    if size <= 0 or size > _MAX_RELEASE_JSON_BYTES:
        raise ReleaseAssetVerificationError("release JSON size is invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetVerificationError("release JSON is malformed") from exc


def _release_assets(
    payload: object, *, expected_tag: str, expected_names: Sequence[str],
) -> Dict[str, Tuple[int, str]]:
    if not isinstance(payload, dict) or set(payload) != {
        "assets", "isDraft", "isPrerelease", "tagName",
    }:
        raise ReleaseAssetVerificationError("release JSON shape is invalid")
    if payload["tagName"] != expected_tag:
        raise ReleaseAssetVerificationError("release tag does not match")
    if payload["isDraft"] is not False or payload["isPrerelease"] is not False:
        raise ReleaseAssetVerificationError("release must be final")
    raw_assets = payload["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) != len(expected_names):
        raise ReleaseAssetVerificationError("release asset set does not match")
    assets: Dict[str, Tuple[int, str]] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            raise ReleaseAssetVerificationError("release asset metadata is malformed")
        name = item.get("name")
        size = item.get("size")
        digest = item.get("digest")
        if not isinstance(name, str) or name in assets:
            raise ReleaseAssetVerificationError("release asset names are ambiguous")
        if type(size) is not int or size <= 0 or size > _MAX_ASSET_BYTES:
            raise ReleaseAssetVerificationError("release asset size is invalid")
        if not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
            raise ReleaseAssetVerificationError("release asset sha256 digest is missing or invalid")
        assets[name] = (size, digest)
    if set(assets) != set(expected_names):
        raise ReleaseAssetVerificationError("release asset set does not match")
    return assets


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseAssetVerificationError("downloaded release asset is unreadable") from exc
    return "sha256:" + digest.hexdigest()


def verify_release_assets(
    release_json: Path,
    assets_directory: Path,
    *,
    expected_tag: str,
    wheel_name: str,
    sdist_name: str,
) -> Tuple[Path, Path]:
    names = _expected_names(wheel_name, sdist_name)
    assets = verify_release_manifest(
        release_json,
        expected_tag=expected_tag,
        wheel_name=wheel_name,
        sdist_name=sdist_name,
    )
    if assets_directory.is_symlink() or not assets_directory.is_dir():
        raise ReleaseAssetVerificationError("release asset directory does not exist")
    try:
        entries = sorted(assets_directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ReleaseAssetVerificationError("release asset directory is unreadable") from exc
    if [entry.name for entry in entries] != sorted(names):
        raise ReleaseAssetVerificationError("downloaded release asset set does not match")
    by_name = {entry.name: entry for entry in entries}
    for name in names:
        entry = by_name[name]
        try:
            mode = entry.lstat().st_mode
            size = entry.stat().st_size
        except OSError as exc:
            raise ReleaseAssetVerificationError("downloaded release asset is unreadable") from exc
        if entry.is_symlink() or not stat.S_ISREG(mode):
            raise ReleaseAssetVerificationError("downloaded release asset is not a regular file")
        expected_size, expected_digest = assets[name]
        if size <= 0 or size != expected_size:
            raise ReleaseAssetVerificationError("downloaded release asset size does not match")
        if _sha256(entry) != expected_digest:
            raise ReleaseAssetVerificationError("downloaded release asset digest does not match")
    return by_name[wheel_name], by_name[sdist_name]


def verify_release_manifest(
    release_json: Path,
    *,
    expected_tag: str,
    wheel_name: str,
    sdist_name: str,
) -> Dict[str, Tuple[int, str]]:
    names = _expected_names(wheel_name, sdist_name)
    return _release_assets(
        _release_payload(release_json),
        expected_tag=expected_tag,
        expected_names=names,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_json", type=Path)
    parser.add_argument("assets_directory", type=Path, nargs="?")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--wheel-name", required=True)
    parser.add_argument("--sdist-name", required=True)
    parser.add_argument("--manifest-only", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.manifest_only:
            if args.assets_directory is not None:
                raise ReleaseAssetVerificationError(
                    "manifest-only verification does not accept an asset directory"
                )
            verify_release_manifest(
                args.release_json,
                expected_tag=args.tag,
                wheel_name=args.wheel_name,
                sdist_name=args.sdist_name,
            )
            print("verified final GitHub Release manifest: " + args.tag)
            return 0
        if args.assets_directory is None:
            raise ReleaseAssetVerificationError("release asset directory is required")
        wheel, sdist = verify_release_assets(
            args.release_json,
            args.assets_directory,
            expected_tag=args.tag,
            wheel_name=args.wheel_name,
            sdist_name=args.sdist_name,
        )
    except (OSError, ReleaseAssetVerificationError) as exc:
        print("GitHub Release asset verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified GitHub Release assets: " + wheel.name + " + " + sdist.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
