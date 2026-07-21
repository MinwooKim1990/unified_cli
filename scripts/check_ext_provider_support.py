#!/usr/bin/env python3
"""Generate and verify the Ext provider support tables in the documentation.

This tool intentionally loads only the explicit Ext entry points declared in
``packages/unified-cli-ext/pyproject.toml``.  It reads plugin metadata and
never constructs a provider or invokes a provider callback.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
EXT_PROJECT = ROOT / "packages" / "unified-cli-ext"
PYPROJECT_PATH = EXT_PROJECT / "pyproject.toml"
ENTRY_POINT_GROUP = '[project.entry-points."unified_cli.providers.v1"]'
BEGIN_MARKER = "<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->"
END_MARKER = "<!-- END GENERATED EXT PROVIDER SUPPORT -->"
_ENTRY_POINT_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_ENTRY_POINT_TARGET = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):PLUGIN$"
)


class SupportTableError(RuntimeError):
    """Raised when metadata or generated documentation is invalid."""


def _add_source_paths() -> None:
    """Make this repository's Core and Ext sources importable for local use."""
    for source_path in (ROOT / "src", EXT_PROJECT / "src"):
        source_text = str(source_path)
        if source_text not in sys.path:
            sys.path.insert(0, source_text)


def read_entry_points(pyproject_path: Path = PYPROJECT_PATH) -> List[Tuple[str, str]]:
    """Read the one supported entry-point section without a TOML dependency."""
    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SupportTableError("cannot read Ext entry-point metadata") from exc

    try:
        section_start = lines.index(ENTRY_POINT_GROUP) + 1
    except ValueError as exc:
        raise SupportTableError("Ext provider entry-point section is missing") from exc

    entries: List[Tuple[str, str]] = []
    seen = set()
    for line in lines[section_start:]:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in stripped:
            stripped = stripped.split("#", 1)[0].rstrip()
        match = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"', stripped)
        if match is None:
            raise SupportTableError("malformed Ext provider entry point")
        name, target = match.groups()
        if _ENTRY_POINT_NAME.fullmatch(name) is None:
            raise SupportTableError("invalid Ext provider entry-point name")
        if _ENTRY_POINT_TARGET.fullmatch(target) is None:
            raise SupportTableError("invalid Ext provider entry-point target")
        if name in seen:
            raise SupportTableError("duplicate Ext provider entry-point name")
        seen.add(name)
        entries.append((name, target))

    if not entries:
        raise SupportTableError("no Ext provider entry points declared")
    return sorted(entries)


def _load_plugin(target: str):
    """Load the explicit metadata object without using provider callbacks."""
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
        plugin = getattr(module, attribute)
    except (AttributeError, ImportError) as exc:
        raise SupportTableError("cannot load Ext provider plugin metadata") from exc

    from unified_cli.plugin import ProviderPluginV1

    if not isinstance(plugin, ProviderPluginV1):
        raise SupportTableError("Ext entry point does not expose ProviderPluginV1")
    return plugin


def provider_rows(entries: Iterable[Tuple[str, str]]) -> List[Tuple[str, str, str, str]]:
    """Collect machine-status fields from each explicit plugin metadata object."""
    _add_source_paths()
    rows = []
    for entry_name, target in entries:
        plugin = _load_plugin(target)
        if plugin.id != entry_name:
            raise SupportTableError("Ext entry-point name does not match plugin id")
        capabilities = ", ".join(sorted(plugin.capabilities)) or "none"
        server = "enabled" if plugin.server_policy.enabled else "disabled"
        rows.append((plugin.id, plugin.support_status, capabilities, server))
    return sorted(rows)


def render_block(rows: Sequence[Tuple[str, str, str, str]], korean: bool) -> str:
    """Render the stable generated block for one documentation language."""
    headings = (
        ("Provider ID", "지원 상태", "Core capability", "서버")
        if korean
        else ("Provider ID", "Support status", "Core capabilities", "Server")
    )
    lines = [
        BEGIN_MARKER,
        "| {} | {} | {} | {} |".format(*headings),
        "|---|---|---|---|",
    ]
    lines.extend("| `{}` | `{}` | `{}` | `{}` |".format(*row) for row in rows)
    lines.append(END_MARKER)
    return "\n".join(lines)


def _replace_generated_block(document: str, block: str) -> str:
    begin_count = document.count(BEGIN_MARKER)
    end_count = document.count(END_MARKER)
    if begin_count != 1 or end_count != 1:
        raise SupportTableError("documentation must contain one generated support block")
    start = document.index(BEGIN_MARKER)
    try:
        end = document.index(END_MARKER, start) + len(END_MARKER)
    except ValueError as exc:
        raise SupportTableError("generated support block markers are out of order") from exc
    if document.find(BEGIN_MARKER, start + 1) != -1:
        raise SupportTableError("documentation has duplicate generated support blocks")
    return document[:start] + block + document[end:]


def _write_atomically(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        os.replace(str(temporary_path), str(path))
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def update_or_check_docs(docs_dir: Path, check: bool) -> None:
    rows = provider_rows(read_entry_points())
    documents = (("extensions.md", False), ("extensions.ko.md", True))
    drifted = []
    replacements: Dict[Path, str] = {}
    for filename, korean in documents:
        path = docs_dir / filename
        try:
            document = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SupportTableError("cannot read documentation file {}".format(filename)) from exc
        replacement = _replace_generated_block(document, render_block(rows, korean))
        if replacement != document:
            drifted.append(filename)
            replacements[path] = replacement

    if check:
        if drifted:
            raise SupportTableError(
                "generated Ext provider support table is out of date: {}".format(
                    ", ".join(drifted)
                )
            )
        return
    for path, replacement in replacements.items():
        _write_atomically(path, replacement)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a generated documentation block differs from metadata",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=ROOT / "docs",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] = ()) -> int:
    args = parse_args(argv)
    try:
        update_or_check_docs(args.docs_dir, check=args.check)
    except SupportTableError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
