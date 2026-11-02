#!/usr/bin/env python3
"""Export a published calibration as one Desktop-installable offline package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.parallel_modeling import ParallelModelingManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export hardware profile, plan, measured fit and published model as a portable package.")
    parser.add_argument("--state-root", type=Path, required=True,
                        help="State root passed to ParallelModelingManager.")
    parser.add_argument("--version-id", required=True, help="Published cm_<24 hex> version ID.")
    parser.add_argument("--output", type=Path, required=True, help="Destination JSON file.")
    parser.add_argument(
        "--supersedes-json", type=Path,
        help=("Optional JSON file containing a supersedes array. Each target names the retired "
              "package_id, hardware_profile_id, calibration_version_id and reason."),
    )
    args = parser.parse_args()

    manager = ParallelModelingManager(args.state_root)
    supersedes = None
    if args.supersedes_json is not None:
        try:
            supersedes = json.loads(args.supersedes_json.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read --supersedes-json: {exc}")
    package = manager.calibration.export_portable_package(
        args.version_id, supersedes=supersedes)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(package, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    print(json.dumps({
        "output": str(output),
        "package_id": package["package_id"],
        "version_id": package["calibration_version"]["id"],
        "hardware_profile_id": package["hardware_profile"]["id"],
        "sample_count": len(package["fit"]["measurements"].get("samples", [])),
        "supersedes": package.get("supersedes", []),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
