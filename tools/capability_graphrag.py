"""Read-only canonical capability discovery and review-candidate CLI.

G0 builds the deterministic typed graph and gap inventory.  The optional G1
``search --llm`` path may only rerank the closed set of graph-retrieved IDs;
all outputs remain diagnostic-only and non-executable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capability_discovery.candidates import write_candidate
from capability_discovery.llm_search import (
    CapabilityGraphRAGSearch,
    build_openai_capability_search,
)
from capability_discovery.service import CapabilityDiscoveryService


def _add_common(command: argparse.ArgumentParser) -> None:
    command.add_argument("--repo-root", type=Path, default=Path.cwd())
    command.add_argument("--format", choices=("text", "json"), default="text")


def _add_output(command: argparse.ArgumentParser) -> None:
    command.add_argument("--output", type=Path, help="explicit JSON output path")
    command.add_argument("--overwrite", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser(
        "index", help="build a full deterministic graph snapshot"
    )
    _add_common(index)
    _add_output(index)
    index.add_argument(
        "--changed-since",
        help="accepted as an audit hint; G0 still performs a parity-safe full rebuild",
    )

    gaps = commands.add_parser("gaps", help="report P4 legacy/canonical coverage gaps")
    _add_common(gaps)
    _add_output(gaps)

    explain = commands.add_parser(
        "explain", help="show graph evidence around one concept"
    )
    _add_common(explain)
    explain.add_argument("concept_id")

    aliases = commands.add_parser(
        "aliases", help="search surface/operator/value alias facts"
    )
    _add_common(aliases)
    aliases.add_argument("query")
    aliases.add_argument("--approved-only", action="store_true")
    aliases.add_argument("--limit", type=int, default=20)

    search = commands.add_parser(
        "search", help="graph-first capability search with optional closed-set LLM rerank"
    )
    _add_common(search)
    search.add_argument("query")
    search.add_argument("--approved-only", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument(
        "--llm",
        action="store_true",
        help="rerank only the retrieved graph IDs through the configured OpenAI model",
    )
    search.add_argument(
        "--model",
        help="LLM reranker model; defaults to CAPABILITY_DISCOVERY_LLM_MODEL, OPENAI_FAST_MODEL, then gpt-4o-mini",
    )
    search.add_argument("--timeout", type=float, default=12.0)

    candidate = commands.add_parser(
        "candidate", help="generate a review-only candidate"
    )
    _add_common(candidate)
    candidate.add_argument("gap", help="gap ID or unique concept ID")
    candidate.add_argument("--output", type=Path, help="explicit file or directory")
    candidate.add_argument("--overwrite", action="store_true")

    drift = commands.add_parser(
        "drift-check", help="verify deterministic projection contracts"
    )
    _add_common(drift)
    drift.add_argument("--fail-on-conflict", action="store_true")

    verify = commands.add_parser("verify", help="CI-safe deterministic verification")
    _add_common(verify)
    verify.add_argument("--fail-on-conflict", action="store_true")
    return parser


def _write_json(payload: Any, target: Path, *, overwrite: bool) -> Path:
    resolved = target.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {resolved}")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if resolved.exists() and not overwrite:
            raise FileExistsError(f"output already exists: {resolved}")
        os.replace(temporary, resolved)
    finally:
        if temporary.exists():
            temporary.unlink()
    return resolved


def _emit(payload: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            print(f"{key}: {value}")
        return
    print(payload)


def _resolve_gap(service: CapabilityDiscoveryService, identifier: str) -> str:
    report = service.find_gaps()
    if any(gap.gap_id == identifier for gap in report.gaps):
        return identifier
    matches = [gap.gap_id for gap in report.gaps if gap.concept_id == identifier]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"unknown gap or concept: {identifier}")
    raise KeyError(f"concept {identifier!r} is ambiguous; use one of {matches}")


def _run(args: argparse.Namespace) -> tuple[Any, int]:
    service = CapabilityDiscoveryService(args.repo_root)

    if args.command == "index":
        envelope = {
            "mode": "full_rebuild",
            "changed_since": args.changed_since,
            "incremental_requested": args.changed_since is not None,
            "snapshot": service.snapshot().to_dict(),
        }
        if args.output:
            path = _write_json(envelope, args.output, overwrite=args.overwrite)
            return {
                "ok": service.projection.ok,
                "mode": "full_rebuild",
                "output": str(path),
                "nodes": len(service.projection.nodes),
                "edges": len(service.projection.edges),
                "projection_issues": len(service.projection.issues),
            }, 0 if service.projection.ok else 1
        if args.format == "json":
            return envelope, 0 if service.projection.ok else 1
        return {
            "ok": service.projection.ok,
            "mode": "full_rebuild",
            "changed_since": args.changed_since,
            "nodes": len(service.projection.nodes),
            "edges": len(service.projection.edges),
            "projection_issues": len(service.projection.issues),
            "runtime_changed": False,
        }, 0 if service.projection.ok else 1

    if args.command == "gaps":
        report = service.find_gaps().to_dict()
        if args.output:
            path = _write_json(report, args.output, overwrite=args.overwrite)
            return {
                "ok": True,
                "output": str(path),
                "gaps": len(report["gaps"]),
                "missing_columns": report["inventory"]["missing_columns"],
            }, 0
        if args.format == "json":
            return report, 0
        return {
            "report_id": report["report_id"],
            "gaps": len(report["gaps"]),
            "missing_columns": report["inventory"]["missing_columns"],
            "missing_axes": report["inventory"]["missing_axes"],
            "runtime_changed": False,
        }, 0

    if args.command == "explain":
        return service.find_capability_evidence(args.concept_id).to_dict(), 0

    if args.command == "aliases":
        alias_kinds = {
            "Alias",
            "SurfaceTerm",
            "SymbolAlias",
            "OperatorAlias",
            "ValueAlias",
        }
        results = [
            result.to_dict()
            for result in service.search_concepts(
                args.query,
                approved_only=args.approved_only,
                limit=max(args.limit * 4, args.limit),
            )
            if result.kind in alias_kinds
        ][: args.limit]
        return {"query": args.query, "results": results, "executable": False}, 0

    if args.command == "search":
        if args.limit < 1:
            raise ValueError("limit must be positive")
        snapshot = service.snapshot()
        if args.llm:
            search_service = build_openai_capability_search(
                snapshot,
                args.repo_root,
                model=args.model,
                timeout=args.timeout,
            )
        else:
            if args.model:
                raise ValueError("--model requires --llm")
            search_service = CapabilityGraphRAGSearch(
                snapshot,
                repository_root=args.repo_root,
            )
        result = search_service.search(
            args.query,
            approved_only=args.approved_only,
            limit=args.limit,
        )
        return result.to_dict(), 0

    if args.command == "candidate":
        gap_id = _resolve_gap(service, args.gap)
        candidate = service.generate_candidate(gap_id)
        if args.output:
            path = write_candidate(candidate, args.output, overwrite=args.overwrite)
            return {
                "ok": True,
                "candidate_id": candidate.candidate_id,
                "output": str(path),
                "mutation_performed": False,
            }, 0
        return candidate.to_dict(), 0

    verification = service.verify(fail_on_conflict=args.fail_on_conflict)
    payload = verification.to_dict()
    deterministic = service.full_rebuild_is_deterministic()
    payload["full_rebuild_deterministic"] = deterministic
    if not deterministic:
        payload["errors"].append(
            {
                "code": "FULL_REBUILD_NONDETERMINISTIC",
                "message": "two isolated full projections produced different snapshots",
            }
        )
        payload["ok"] = False
    return payload, 0 if payload["ok"] else 1


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        payload, status = _run(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        payload, status = {"ok": False, "error": str(exc), "runtime_changed": False}, 2
    _emit(payload, args.format)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
