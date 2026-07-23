#!/usr/bin/env python3
"""Verify the exact one-wheel unified-cli release artifact contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from verify_release_artifacts import (
    ArtifactVerificationError,
    verify_release_directory,
)
from verify_single_distribution import EXPECTED_PROVIDER_ENTRY_POINTS


EXPECTED_RUNTIME_DEPENDENCIES = (
    "rich>=13",
    "prompt-toolkit>=3.0.43",
)

EXPECTED_OPTIONAL_DEPENDENCIES = (
    'fastapi>=0.100; extra == "server"',
    'uvicorn>=0.23; extra == "server"',
    'pydantic>=2; extra == "server"',
    (
        'agent-client-protocol<0.12,>=0.11; '
        '(python_version >= "3.10" and python_version < "3.15") '
        'and extra == "acp"'
    ),
    'mcp<2,>=1.27; python_version >= "3.10" and extra == "mcp"',
    'pytest>=7; extra == "dev"',
    'fastapi>=0.100; extra == "dev"',
    'pydantic>=2; extra == "dev"',
    'httpx>=0.24; extra == "dev"',
    'fastapi>=0.100; extra == "all"',
    'uvicorn>=0.23; extra == "all"',
    'pydantic>=2; extra == "all"',
    'pytest>=7; extra == "all"',
    'httpx>=0.24; extra == "all"',
    (
        'agent-client-protocol<0.12,>=0.11; '
        '(python_version >= "3.10" and python_version < "3.15") '
        'and extra == "all"'
    ),
    'mcp<2,>=1.27; python_version >= "3.10" and extra == "all"',
)


def verify_unified_release(directory: Path, version: str) -> None:
    verify_release_directory(
        directory,
        expected_name="unified-cli",
        expected_version=version,
        package_sources=(
            ("unified_cli", "src/unified_cli"),
            (
                "unified_cli_ext",
                "packages/unified-cli-ext/src/unified_cli_ext",
            ),
        ),
        required_package_files=(
            ("unified_cli", "py.typed"),
            ("unified_cli_ext", "py.typed"),
        ),
        expected_dependency=EXPECTED_RUNTIME_DEPENDENCIES,
        expected_optional_dependency=EXPECTED_OPTIONAL_DEPENDENCIES,
        forbidden_dependencies=("unified-cli", "unified-cli-ext"),
        expected_entry_points={
            "console_scripts": {
                "unified-cli": "unified_cli.cli:main",
            },
            "unified_cli.providers.v1": EXPECTED_PROVIDER_ENTRY_POINTS,
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_unified_release(args.directory, args.version)
    except (ArtifactVerificationError, OSError) as exc:
        print("unified release verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("verified unified-cli release artifacts for " + args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
