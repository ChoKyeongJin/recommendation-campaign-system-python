from __future__ import annotations

import pytest

from capability_discovery.candidates import (
    CapabilityCandidateGenerator,
    PromotionDisabledError,
    promote_candidate,
    validate_candidate_payload,
)
from capability_discovery.domain import Evidence, GapRecord, RepositoryRevision
from capability_discovery.gap_analysis import (
    CapabilityGapAnalyzer,
    validate_gap_report_payload,
)


def _revision() -> RepositoryRevision:
    return RepositoryRevision(
        git_revision="a" * 40,
        dirty=True,
        content_sha256="b" * 64,
        source_hashes={"tools/canonical_coverage_inventory.py": "c" * 64},
    )


def _evidence() -> Evidence:
    return Evidence(
        source_type="config",
        source_path="docs/data/runtime/sql/member_target_filters.json",
        source_pointer="/eq_filters/0/column",
        extraction_method="deterministic",
        confidence=1.0,
        content_hash="d" * 64,
        revision="a" * 40,
        trust_state="observed",
    )


def test_p4_inventory_becomes_deterministic_legacy_only_gaps() -> None:
    inventory = {
        "canonical_columns": 1,
        "legacy_columns": 3,
        "missing_columns": 2,
        "missing_axes": 1,
        "exempt_columns": 0,
        "by_axis": {"eq_filters": ["SMS_YN", "EMAIL_YN"]},
        "columns": {"EMAIL_YN": ["eq_filters"], "SMS_YN": ["eq_filters"]},
    }
    analyzer = CapabilityGapAnalyzer()
    first = analyzer.analyze_inventory(inventory, revision=_revision())
    second = analyzer.analyze_inventory(inventory, revision=_revision())

    assert first.to_dict() == second.to_dict()
    assert len(first.gaps) == 1
    assert first.gaps[0].classification == "LEGACY_ONLY"
    assert first.gaps[0].details["missing_columns"] == ("EMAIL_YN", "SMS_YN") or list(
        first.gaps[0].details["missing_columns"]
    ) == ["EMAIL_YN", "SMS_YN"]
    assert first.gaps[0].evidence
    assert first.to_dict()["policy"]["executable"] is False
    assert validate_gap_report_payload(first.to_dict()) == ()


def test_candidate_is_review_only_and_has_field_level_provenance() -> None:
    gap = GapRecord(
        gap_id="gap:test",
        concept_id="eq_filters",
        classification="LEGACY_ONLY",
        summary="legacy-only test gap",
        legacy_assets=("legacy:eq_filters",),
        evidence=(_evidence(),),
        details={"axis": "eq_filters", "missing_columns": ["EMAIL_YN"]},
        blocking_questions=("NULL semantics?",),
    )
    candidate = CapabilityCandidateGenerator().generate(gap, revision=_revision())
    payload = candidate.to_dict()

    assert payload["status"] == "pending_review"
    assert payload["executable"] is False
    assert payload["mutation_allowed"] is False
    assert payload["validation"]["ready_for_promotion"] is False
    assert payload["evidence"]
    assert all(change["evidence"] for change in payload["field_changes"])
    assert validate_candidate_payload(payload) == ()

    malformed = {**payload, "unexpected_runtime_field": "SELECT 1"}
    assert any(
        "Additional properties" in issue
        for issue in validate_candidate_payload(malformed)
    )

    with pytest.raises(PromotionDisabledError):
        promote_candidate(payload)
