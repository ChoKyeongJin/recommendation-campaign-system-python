"""capability 정적 검증 계약(플랜 A-3) — 선언↔실행자산 대조가 CI 에서 상시 돈다.

ac924ff 가 capability_registry.py(validate_capabilities)를 삭제한 뒤 이 대조는 전무했다.
후계 capability_validation 은 순수 모듈이고, 여기서 실제 graph_rag 빌더 레지스트리를 주입해
전 축(A 라벨/B 노출/C 빌더 소유권/D base×qualifier)을 강제한다. db_swap_preflight 는 경량
축(A/B/D)만 돌리므로 **축 C 는 이 파일이 유일한 게이트다**.

역검증: 가드가 실제로 문다는 것을 주입 변형으로 증명한다(탐지력 없는 green 은 공허하다).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import capability_validation  # noqa: E402
import graph_rag  # noqa: E402
import targeting_ir  # noqa: E402


def _real_registry():
    return list(graph_rag._sql_target_builder_registry())


def test_static_validation_is_clean() -> None:
    issues = capability_validation.validate_capabilities(_real_registry())
    assert issues == [], "capability 선언과 실행 자산이 어긋났다:\n  " + "\n  ".join(issues)


def test_summary_is_ok_and_covers_all_axes() -> None:
    summary = capability_validation.capability_check_summary(_real_registry())
    assert summary["status"] == "ok"
    assert summary["issues"] == []
    assert set(summary["checked"]) == {
        "slot_labels", "plan_exposure", "requirement_capabilities", "builder_ownership",
    }


def test_response_summary_wiring_is_ok() -> None:
    """응답 조립이 싣는 파생 요약 — 생산자 0 시절의 '항상 None 계약'이 실계약으로 바뀌었다."""
    summary = graph_rag._capability_check_summary()
    assert summary["status"] == "ok"


# ── 역검증: 각 축이 실제로 위반을 무는지 주입 변형으로 증명 ──────────────────────────


def _fake_builder(query_plan):  # noqa: ANN001, ANN201 — 시그니처만 흉내
    return None


def test_detects_multi_owned_kind() -> None:
    registry = _real_registry()
    some_fact_kind = next(iter(sorted(targeting_ir.fact_join_kinds())))
    registry.append((_fake_builder, frozenset({some_fact_kind})))
    issues = capability_validation.builder_ownership_issues(registry)
    assert any("kind_multi_owned" in issue for issue in issues), issues


def test_detects_undeclared_kind() -> None:
    registry = _real_registry()
    registry.append((_fake_builder, frozenset({"definitely_not_a_kind"})))
    issues = capability_validation.builder_ownership_issues(registry)
    assert any("kind_undeclared" in issue for issue in issues), issues


def test_detects_unowned_fact_kind() -> None:
    target = next(iter(sorted(targeting_ir.fact_join_kinds())))
    registry = [
        (builder, frozenset(kinds - {target}))
        for builder, kinds in _real_registry()
    ]
    issues = capability_validation.builder_ownership_issues(registry)
    assert any(f"fact_kind_unowned: {target}" in issue for issue in issues), issues


def test_detects_undeclared_composite_builder() -> None:
    registry = _real_registry()
    registry.append((_fake_builder, frozenset()))
    issues = capability_validation.builder_ownership_issues(registry)
    assert any("composite_builder_undeclared" in issue for issue in issues), issues


def test_detects_missing_slot_label(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delitem(targeting_ir.SLOT_KO_LABELS, "metric_trend")
    issues = capability_validation.slot_label_issues()
    assert any("slot_label_missing: metric_trend" in issue for issue in issues), issues
