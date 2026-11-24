"""`harness-validate`: check `harness.yaml` files against schema v1.

Exit codes: 0 when every file is valid, 1 when any file has issues, 2 for a usage error or a
file that cannot be read.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from harness.manifest.validate import manifest_json_schema_text, validate_manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness-validate",
        description="Validate harness.yaml manifests against schema v1 (docs/harness-yaml.md).",
    )
    parser.add_argument("files", nargs="*", type=Path, help="manifest files to check")
    parser.add_argument(
        "--schema", action="store_true", help="print the JSON Schema of the manifest and exit"
    )
    args = parser.parse_args(argv)

    if args.schema:
        sys.stdout.write(manifest_json_schema_text())
        return 0
    if not args.files:
        parser.error("give at least one manifest file, or --schema")

    status = 0
    for path in args.files:
        try:
            content = path.read_bytes()
        except OSError as exc:
            print(f"{path}: cannot read the file: {exc.strerror or exc}", file=sys.stderr)
            status = 2
            continue
        result = validate_manifest(content)
        if result.manifest is not None:
            agents = ", ".join(f"{agent.id} ({agent.type})" for agent in result.manifest.agents)
            print(f"{path}: OK: hotel {result.manifest.hotel}; agents: {agents}")
            continue
        for issue in result.issues:
            print(f"{path}: {issue}")
        status = max(status, 1)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
