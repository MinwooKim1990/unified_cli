"""Embed one Core or Preview coding CLI through the public Python API.

Examples:
    python examples/09_extensions.py grok "Explain this repository"
    python examples/09_extensions.py kimi "Review the current diff"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from unified_cli import UnifiedError, configure_extension_provider, create


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider")
    parser.add_argument("prompt")
    parser.add_argument("--model")
    parser.add_argument("--cwd", default=".")
    parser.add_argument(
        "--configure",
        action="store_true",
        help="verify the vendor CLI and persist its launch receipt first",
    )
    arguments = parser.parse_args()
    workspace = str(Path(arguments.cwd).expanduser().resolve())

    try:
        if arguments.configure:
            configure_extension_provider(arguments.provider)
        client = create(
            arguments.provider,
            model=arguments.model,
            cwd=workspace,
        )
        for event in client.stream(arguments.prompt):
            if event.kind == "text":
                print(event.text, end="", flush=True)
        print()
        return 0
    except UnifiedError as error:
        print("{}: {}".format(error.kind, error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
