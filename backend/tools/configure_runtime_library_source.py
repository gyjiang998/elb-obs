"""Register one administrator-owned runtime-library directory for all profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..runtime_library_source import configure_runtime_library_source


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register one shared OpenHarmony AArch64 runtime-library root without copying libraries",
    )
    parser.add_argument(
        "--library-root",
        required=True,
        help="administrator-owned directory containing target AArch64 OpenHarmony .so files",
    )
    parser.add_argument("--state-root", required=True, help="platform runtime state directory")
    args = parser.parse_args()
    try:
        result = configure_runtime_library_source(
            library_root=Path(args.library_root),
            state_root=Path(args.state_root),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
