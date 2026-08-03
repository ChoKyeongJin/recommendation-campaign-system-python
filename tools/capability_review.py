"""Inspect and validate offline capability candidates.

Promotion is intentionally unavailable in G0.  An approved change must be a
reviewed patch to the existing source registries, never a graph-side mutation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capability_discovery.candidates import (
    PromotionDisabledError,
    load_candidate,
    promote_candidate,
    validate_candidate_payload,
)


def _emit(payload: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            rendered = (
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else value
            )
            print(f"{key}: {rendered}")
        return
    print(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "validate", "promote"):
        command = commands.add_parser(name)
        command.add_argument(
            "candidate", type=Path, help="explicit candidate JSON path"
        )
        command.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        payload = load_candidate(args.candidate)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": str(exc)}, args.format)
        return 2

    if args.command == "show":
        _emit(payload, args.format)
        return 0

    issues = validate_candidate_payload(payload)
    if args.command == "validate":
        _emit({"ok": not issues, "issues": list(issues)}, args.format)
        return 0 if not issues else 1

    try:
        promote_candidate(payload)
    except PromotionDisabledError as exc:
        _emit(
            {"ok": False, "error": str(exc), "mutation_performed": False}, args.format
        )
        return 2
    raise RuntimeError("unreachable: offline promotion returned without an error")


if __name__ == "__main__":
    raise SystemExit(main())
