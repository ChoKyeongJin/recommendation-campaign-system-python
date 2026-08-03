"""Review-only candidate artifacts derived from deterministic capability gaps."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Evidence, GapRecord, RepositoryRevision, stable_id
from .policy import (
    DEFAULT_DISCOVERY_POLICY,
    DiscoveryBoundaryError,
    validate_offline_payload,
)

CANDIDATE_TYPES = frozenset(
    {
        "new_capability",
        "alias_addition",
        "operator_alias_addition",
        "physical_binding_addition",
        "value_mapping_addition",
        "lowering_required",
        "compiler_required",
        "expressible_false_declaration",
        "conflict_resolution",
        "stale_asset_removal",
    }
)

_TYPE_BY_GAP = {
    "LEGACY_ONLY": "new_capability",
    "ALIAS_MISSING": "alias_addition",
    "PHYSICAL_BINDING_MISSING": "physical_binding_addition",
    "VALUE_MAPPING_MISSING": "value_mapping_addition",
    "LOWERING_MISSING": "lowering_required",
    "COMPILER_MISSING": "compiler_required",
    "EXPRESSIBILITY_UNDECLARED": "expressible_false_declaration",
    "CONFLICTING_DEFINITION": "conflict_resolution",
    "STALE_APPROVED_ASSET": "stale_asset_removal",
}


class CandidateValidationError(ValueError):
    """Raised when a candidate cannot satisfy the review-artifact contract."""


class PromotionDisabledError(DiscoveryBoundaryError):
    """Raised for every attempt to promote from the offline G0 subsystem."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CandidateValidationError(
        f"candidate contains a non-JSON value: {type(value).__name__}"
    )


def _candidate_symbol(concept_id: str) -> str:
    symbol = re.sub(r"[^a-z0-9]+", "_", concept_id.strip().lower()).strip("_")
    if not symbol:
        raise CandidateValidationError(
            "gap concept_id cannot produce a canonical identifier"
        )
    if symbol[0].isdigit():
        symbol = f"capability_{symbol}"
    return symbol


def _evidence_record(item: Evidence) -> dict[str, Any]:
    payload = item.to_dict()
    evidence_id = stable_id(
        "evidence",
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )
    return {"evidence_id": evidence_id, **payload}


@dataclass(frozen=True)
class CapabilityCandidate:
    candidate_id: str
    gap_id: str
    canonical_id_candidate: str
    candidate_type: str
    proposal: Mapping[str, Any]
    field_changes: tuple[Mapping[str, Any], ...]
    evidence: tuple[Evidence, ...]
    blocking_questions: tuple[str, ...]
    conflicts: tuple[str, ...]
    generated_at_revision: str
    status: str = "pending_review"
    schema_version: str = "capability-candidate/v1"

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.gap_id.strip():
            raise CandidateValidationError("candidate_id and gap_id are required")
        if self.candidate_type not in CANDIDATE_TYPES:
            raise CandidateValidationError(
                f"unknown candidate_type: {self.candidate_type}"
            )
        if self.status != "pending_review":
            raise CandidateValidationError(
                "offline candidates must remain pending_review"
            )
        if not self.evidence:
            raise CandidateValidationError("candidate evidence cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        evidence = [_evidence_record(item) for item in self.evidence]
        evidence_ids = {item["evidence_id"] for item in evidence}
        field_changes = [_plain(item) for item in self.field_changes]
        provenance_complete = bool(field_changes) and all(
            isinstance(change.get("evidence"), list)
            and bool(change["evidence"])
            and set(change["evidence"]) <= evidence_ids
            for change in field_changes
        )
        issues = [] if provenance_complete else ["field-level evidence is incomplete"]
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "gap_id": self.gap_id,
            "canonical_id_candidate": self.canonical_id_candidate,
            "candidate_type": self.candidate_type,
            "status": self.status,
            "policy": DEFAULT_DISCOVERY_POLICY.to_dict(),
            "executable": False,
            "mutation_allowed": False,
            "proposal": _plain(self.proposal),
            "field_changes": field_changes,
            "evidence": evidence,
            "blocking_questions": list(self.blocking_questions),
            "conflicts": list(self.conflicts),
            "validation": {
                "schema_valid": True,
                "provenance_complete": provenance_complete,
                "ready_for_promotion": False,
                "issues": issues,
            },
            "generated_at_revision": self.generated_at_revision,
        }


class CapabilityCandidateGenerator:
    """Convert one gap to a content-addressed, non-executable review draft."""

    def generate(
        self,
        gap: GapRecord,
        *,
        revision: RepositoryRevision,
    ) -> CapabilityCandidate:
        if not gap.evidence:
            raise CandidateValidationError(
                "a candidate cannot be generated without gap evidence"
            )
        candidate_type = _TYPE_BY_GAP.get(gap.classification, "new_capability")
        symbol = _candidate_symbol(gap.concept_id)
        candidate_id = stable_id(
            "candidate",
            gap.gap_id,
            revision.content_sha256,
            candidate_type,
        )
        evidence_records = [_evidence_record(item) for item in gap.evidence]
        evidence_ids = [item["evidence_id"] for item in evidence_records]
        details = _plain(gap.details)
        field_changes: tuple[Mapping[str, Any], ...] = (
            {
                "field": "canonical_id",
                "proposed_value": symbol,
                "evidence": evidence_ids,
            },
            {
                "field": "physical_binding.columns",
                "proposed_value": details.get("missing_columns", []),
                "evidence": evidence_ids,
            },
        )
        revision_id = revision.git_revision or revision.content_sha256
        return CapabilityCandidate(
            candidate_id=candidate_id,
            gap_id=gap.gap_id,
            canonical_id_candidate=symbol,
            candidate_type=candidate_type,
            proposal={
                "kind": "legacy_capability_candidate",
                "classification": gap.classification,
                "legacy_assets": list(gap.legacy_assets),
                "details": details,
            },
            field_changes=field_changes,
            evidence=gap.evidence,
            blocking_questions=tuple(gap.blocking_questions),
            conflicts=tuple(gap.conflicts),
            generated_at_revision=revision_id,
        )


def _schema_issues(
    payload: Mapping[str, Any], schema_path: str | Path | None
) -> list[str]:
    path = (
        Path(schema_path).resolve()
        if schema_path is not None
        else Path(__file__).resolve().parents[1]
        / "docs"
        / "data"
        / "schemas"
        / "capability_candidate.schema.json"
    )
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return ["jsonschema dependency is required for candidate validation"]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        return [f"candidate schema is unavailable or invalid: {exc}"]

    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    issues: list[str] = []
    for error in errors:
        pointer = "/" + "/".join(str(part) for part in error.absolute_path)
        issues.append(f"schema{pointer}: {error.message}")
    return issues


def validate_candidate_payload(
    payload: Mapping[str, Any],
    *,
    schema_path: str | Path | None = None,
) -> tuple[str, ...]:
    """Validate the JSON Schema and the non-executable semantic boundary."""

    issues = _schema_issues(payload, schema_path)
    try:
        validate_offline_payload(payload)
    except DiscoveryBoundaryError as exc:
        issues.append(str(exc))

    required = {
        "schema_version",
        "candidate_id",
        "gap_id",
        "canonical_id_candidate",
        "candidate_type",
        "status",
        "proposal",
        "field_changes",
        "evidence",
        "blocking_questions",
        "conflicts",
        "validation",
        "generated_at_revision",
    }
    missing = sorted(required - set(payload))
    if missing:
        issues.append(f"missing required fields: {missing}")
    if payload.get("status") != "pending_review":
        issues.append("status must be pending_review")
    if payload.get("candidate_type") not in CANDIDATE_TYPES:
        issues.append("candidate_type is not recognized")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        issues.append("evidence must be a non-empty list")
        evidence_ids: set[str] = set()
    else:
        evidence_ids = {
            str(item.get("evidence_id"))
            for item in evidence
            if isinstance(item, Mapping) and item.get("evidence_id")
        }
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                issues.append(f"evidence[{index}] must be an object")
                continue
            for key in (
                "evidence_id",
                "source_type",
                "source_path",
                "source_pointer",
                "content_hash",
                "trust_state",
            ):
                if not item.get(key):
                    issues.append(f"evidence[{index}].{key} is required")

    changes = payload.get("field_changes")
    if not isinstance(changes, list) or not changes:
        issues.append("field_changes must be a non-empty list")
    else:
        for index, change in enumerate(changes):
            if not isinstance(change, Mapping):
                issues.append(f"field_changes[{index}] must be an object")
                continue
            refs = change.get("evidence")
            if not isinstance(refs, list) or not refs:
                issues.append(f"field_changes[{index}].evidence is required")
            elif not set(map(str, refs)) <= evidence_ids:
                issues.append(f"field_changes[{index}] references unknown evidence")

    validation = payload.get("validation")
    if (
        isinstance(validation, Mapping)
        and validation.get("ready_for_promotion") is not False
    ):
        issues.append("offline candidate must declare ready_for_promotion=false")
    return tuple(dict.fromkeys(issues))


def load_candidate(path: str | Path) -> dict[str, Any]:
    candidate_path = Path(path).resolve()
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CandidateValidationError("candidate document must be a JSON object")
    return payload


def write_candidate(
    candidate: CapabilityCandidate,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write only to a caller-selected path or directory."""

    target = Path(output).resolve()
    if target.exists() and target.is_dir():
        target = target / f"{candidate.candidate_id}.json"
    elif not target.suffix:
        target.mkdir(parents=True, exist_ok=True)
        target = target / f"{candidate.candidate_id}.json"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"candidate already exists: {target}")

    encoded = (
        json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists() and not overwrite:
            raise FileExistsError(f"candidate already exists: {target}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def promote_candidate(_payload: Mapping[str, Any]) -> None:
    """Make the forbidden operation explicit for callers and architecture tests."""

    raise PromotionDisabledError(
        "offline G0 cannot promote or mutate approved capability sources; "
        "create a reviewed source patch/PR and run the existing validation suite"
    )
