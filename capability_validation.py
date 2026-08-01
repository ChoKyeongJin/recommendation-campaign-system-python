"""Capability 정적 검증(플랜 A-3) — 선언과 실행 자산의 교차 검증.

ac924ff 에서 삭제된 capability_registry.validate_capabilities 의 후계다. 원복이 아니라 신규
작성: 권위는 별도 선언 파일이 아니라 기존 실행 자산(targeting_ir 의 CONDITION_SPECS/SLOT_SHAPES/
SLOT_KO_LABELS, V4 노출면, graph_rag 빌더 레지스트리, requirement_capabilities.json)이고,
이 모듈은 그 사이의 드리프트만 기계 검증한다. 순수 in/out — graph_rag 를 import 하지 않으며,
빌더 레지스트리는 호출자가 주입한다.

검사 축:
  A. 슬롯 라벨 완전성 — 모든 SLOT_SHAPES 슬롯에 비어 있지 않은 ko_label(안내 침묵 삭제 방지).
  B. plan 노출 완전성 — plan 컨테이너 슬롯은 LLM 노출 ∨ 사유 있는 제외(조용한 미노출 금지).
  C. 빌더 소유권 — 1:1 기본 + 선언된 예외. dispatch 가 우선순위 튜플·첫 non-None 승자라
     다중 소유를 허용하면 튜플 순서가 의미가 된다(순서 변경 = 조용한 SQL 변경). 그래서:
       C1. fact_join kind 는 정확히 1개 빌더가 소유한다.
       C2. 레지스트리 kind ⊆ CONDITION_SPECS kind(미선언 kind 금지).
       C3. fact_join 아닌데 소유된 kind 는 허용목록만(현재 campaign_responses).
       C4. kind 미소유 빌더는 선언된 복합 컴파일러만(피연산자를 재귀 컴파일하는 부류).
  D. base×qualifier 레지스트리 — supported 는 bool, 미지원 조합엔 message 필수
     (semantic_requirements.RequirementRegistry.load 가 스키마 권위 — 여기서는 로드 가능성만 확인).

배선: tests/test_capability_contract.py(전체 축), db_swap_preflight(경량 축 A/B/D —
graph_rag import 없이), graph_rag 응답 조립의 capability_check(요약 파생).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import targeting_ir

# fact_join=False 인데 빌더가 소유하는 kind 허용목록(역사적 예외 — EXISTS 술어라 팩트 집계
# 조인이 필요 없지만 전용 빌더가 있다). 늘리려면 사유를 여기 적어라.
OWNED_NON_FACT_JOIN_KINDS: frozenset[str] = frozenset({"campaign_responses"})

# kind 를 소유하지 않는 복합 컴파일러 허용목록(피연산자를 재귀 컴파일 — 단일 조건 kind 가 없다).
COMPOSITE_BUILDER_NAMES: frozenset[str] = frozenset({
    "build_analytical_aggregation_sql_candidate",
    "build_union_targets_sql_candidate",
})


def slot_label_issues() -> list[str]:
    """축 A: 라벨 없는 슬롯·스테일 라벨."""
    issues: list[str] = []
    shapes = set(targeting_ir.SLOT_SHAPES)
    labels = targeting_ir.SLOT_KO_LABELS
    for name in sorted(shapes - set(labels)):
        issues.append(f"slot_label_missing: {name} — SLOT_KO_LABELS 에 라벨을 추가하라.")
    for name in sorted(set(labels) - shapes):
        issues.append(f"slot_label_stale: {name} — 슬롯이 없는데 라벨만 남았다.")
    for name, label in labels.items():
        if name in shapes and not str(label).strip():
            issues.append(f"slot_label_empty: {name}")
    return issues


def plan_exposure_issues() -> list[str]:
    """축 B: plan 컨테이너 슬롯의 '노출 ∨ 선언된 제외' 완전성."""
    from query_structurer.campaign_plan_v4 import (  # noqa: PLC0415 — 순환 없음(v4 는 targeting_ir 만 본다)
        CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
        _PLAN_SLOT_EXPOSURE_EXCLUSIONS,
    )

    exposed = set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"])
    issues: list[str] = []
    for name, shape in targeting_ir.SLOT_SHAPES.items():
        if shape.container != "plan":
            continue
        if name in exposed:
            continue
        reason = _PLAN_SLOT_EXPOSURE_EXCLUSIONS.get(name)
        if not (isinstance(reason, str) and reason.strip()):
            issues.append(
                f"plan_slot_unexposed_undeclared: {name} — V4 에 노출하거나 제외 사유를 선언하라."
            )
    for name in _PLAN_SLOT_EXPOSURE_EXCLUSIONS:
        if name in exposed or name not in targeting_ir.SLOT_SHAPES:
            issues.append(f"plan_slot_exclusion_stale: {name}")
    return issues


def builder_ownership_issues(
    builder_registry: Iterable[tuple[Any, frozenset[str]]],
) -> list[str]:
    """축 C: 빌더 소유권 1:1 + 선언된 예외(모듈 상수)."""
    issues: list[str] = []
    owned: Counter[str] = Counter()
    composites: list[str] = []
    for builder, kinds in builder_registry:
        name = getattr(builder, "__name__", str(builder))
        if kinds:
            for kind in kinds:
                owned[kind] += 1
        else:
            composites.append(name)

    fact_kinds = targeting_ir.fact_join_kinds()
    spec_kinds = frozenset(spec.kind for spec in targeting_ir.CONDITION_SPECS)

    for kind, count in sorted(owned.items()):
        if count > 1:
            issues.append(
                f"kind_multi_owned: {kind} 를 빌더 {count}개가 소유 — 튜플 순서가 의미가 된다. 하나만 남겨라."
            )
    for kind in sorted(fact_kinds - set(owned)):
        issues.append(f"fact_kind_unowned: {kind} — fact_join 조건인데 소유 빌더가 없다.")
    for kind in sorted(set(owned) - spec_kinds):
        issues.append(f"kind_undeclared: {kind} — CONDITION_SPECS 에 없는 kind 를 빌더가 소유한다.")
    for kind in sorted(set(owned) - fact_kinds - OWNED_NON_FACT_JOIN_KINDS):
        issues.append(
            f"non_fact_kind_owned: {kind} — fact_join=False 인데 소유됐다. "
            "의도라면 OWNED_NON_FACT_JOIN_KINDS 에 사유와 함께 등재하라."
        )
    for name in composites:
        if name not in COMPOSITE_BUILDER_NAMES:
            issues.append(
                f"composite_builder_undeclared: {name} — kind 미소유 빌더는 COMPOSITE_BUILDER_NAMES 선언 필수."
            )
    for name in sorted(COMPOSITE_BUILDER_NAMES - set(composites)):
        issues.append(f"composite_builder_stale: {name} — 선언은 있는데 레지스트리에 없다.")
    return issues


def requirement_capability_issues(path: str | Path | None = None) -> list[str]:
    """축 D: base×qualifier 레지스트리 로드 가능성(스키마 권위는 semantic_requirements 로더)."""
    import semantic_requirements  # noqa: PLC0415 — 순수 모듈

    try:
        if path is None:
            registry = semantic_requirements.RequirementRegistry.load()
        else:
            registry = semantic_requirements.RequirementRegistry.load(Path(path))
    except Exception as exc:  # noqa: BLE001 — 로드 실패 유형 전부가 '검증 실패' 보고 대상
        return [f"requirement_capabilities_invalid: {exc}"]
    if not registry.capabilities:
        return ["requirement_capabilities_empty: base×qualifier 항목이 하나도 없다."]
    return []


def validate_capabilities(
    builder_registry: Iterable[tuple[Any, frozenset[str]]] | None = None,
) -> list[str]:
    """전 축 검증. builder_registry 미주입 시 축 C 는 건너뛴다(경량 배선용 — preflight)."""
    issues = slot_label_issues() + plan_exposure_issues() + requirement_capability_issues()
    if builder_registry is not None:
        issues += builder_ownership_issues(builder_registry)
    return issues


def capability_check_summary(
    builder_registry: Iterable[tuple[Any, frozenset[str]]] | None = None,
) -> dict[str, Any]:
    """응답 capability_check 필드용 요약(파생 — plan 을 변형하지 않는다)."""
    issues = validate_capabilities(builder_registry)
    checked = ["slot_labels", "plan_exposure", "requirement_capabilities"]
    if builder_registry is not None:
        checked.append("builder_ownership")
    return {
        "status": "ok" if not issues else "issues",
        "checked": checked,
        "issues": issues,
    }
