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
  E. 빌더 **순서** — 축 C 가 "누가 소유하는가"라면 축 E 는 "누가 먼저 시도되는가"다.
     dispatch 가 첫 non-None 승자라 순서 자체가 의미이고(순서 변경 = 조용한 SQL 변경),
     그동안 그 지식은 레지스트리 주석 8줄에만 있었다 — 그중 하나('analytical 가장 먼저')는
     이미 사실과 달랐다(실제 3번째). 주석은 검증되지 않으므로 선행 제약을 데이터로 옮긴다.
       E1. 선언된 모든 선행쌍이 실제 튜플 순서에서 성립한다.
       E2. 선행쌍이 가리키는 빌더가 레지스트리에 실재한다(스테일 제약 금지).
       E3. 종단 폴백 빌더는 마지막이다(앞에 오면 뒤 빌더가 영영 시도되지 않는다).

배선: tests/test_capability_contract.py(전체 축), tests/test_builder_order_contract.py(축 E 상세),
db_swap_preflight(경량 축 A/B/D — graph_rag import 없이), graph_rag 응답 조립의 capability_check
(요약 파생), graph_rag._install_sql_builder_admission_guards(import 시점 축 C·E 하드 실패).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
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


class BuilderContractError(RuntimeError):
    """빌더 레지스트리 계약(축 C·E) 위반. 기동 자체를 막는다."""


# ── 축 E: 빌더 우선순위 제약 ──────────────────────────────────────────────────────────────
#
# 각 항목은 (먼저 시도돼야 하는 빌더, 나중 빌더, 어기면 무엇이 조용히 틀리는가)다.
# **사유가 필수**인 이유: 순서 제약은 "왜"를 잃으면 다음 사람이 안전하게 못 고친다. 실제로
# 이 저장소의 순서 지식은 주석에만 있었고, 그중 하나는 이미 거짓이 된 채로 남아 있었다.
#
# 새 빌더를 넣을 때: 그 빌더가 다른 빌더의 신호를 가로챌 수 있으면 여기 한 줄을 추가하라.
# 제약이 없다는 것은 "순서가 무관하다"는 **주장**이므로, 무관하지 않으면 반드시 적어야 한다.
BUILDER_PRECEDENCE: tuple[tuple[str, str, str], ...] = (
    (
        "build_condition_evaluation_sql_candidate",
        "build_analytical_aggregation_sql_candidate",
        "판정 grain 과 결과 grain 을 분리하는 닫힌 IR 이 먼저다 — 뒤로 밀면 주문·상품 단위 "
        "HAVING 이 회원 COUNT 로 평탄화된다.",
    ),
    (
        "build_entity_set_targets_sql_candidate",
        "build_purchase_history_targets_sql_candidate",
        "파생 엔터티 집합(순위 서브쿼리)이 먼저다 — 같은 문장의 '상품'·기간 표현이 리터럴 상품 "
        "조건으로 새면 순위 집합이 통째로 사라진 채 그럴듯한 SQL 이 나간다.",
    ),
    (
        "build_campaign_response_frequency_targets_sql_candidate",
        "build_campaign_response_targets_sql_candidate",
        "반응 '횟수'(HAVING COUNT) 가 먼저다 — EXISTS(≥1회) 빌더가 먼저 잡으면 임계값이 사라져 "
        "'3회 이상'이 '1회 이상'으로 조용히 넓어진다.",
    ),
    (
        "build_purchase_count_ranking_sql_candidate",
        "build_purchase_history_targets_sql_candidate",
        "기간 내 상위 N 랭킹이 먼저다 — 날짜만 있는 랭킹이 구매 이력 쪽으로 새면 순위가 사라진다.",
    ),
    (
        "build_metric_trend_targets_sql_candidate",
        "build_purchase_history_targets_sql_candidate",
        "기간 대 기간 증감이 먼저다 — 같은 문장의 기간 표현이 단일 기간 필터로 새면 '증감' 자체가 사라진다.",
    ),
    (
        "build_metric_trend_targets_sql_candidate",
        "build_aggregate_targets_sql_candidate",
        "두 기간 비교는 단일 창 집계로 환원되지 않는다 — 집계 빌더가 먼저 잡으면 한쪽 기간이 사라진다.",
    ),
    (
        "build_event_expression_sql_candidate",
        "build_purchase_history_targets_sql_candidate",
        "범용 사건 논리식이 먼저다 — 구매 이력 빌더는 극성별 창을 하나씩만 담을 수 있어 "
        "뒤로 밀면 한쪽 창이 조용히 사라진다.",
    ),
    (
        "build_event_expression_sql_candidate",
        "build_order_count_targets_sql_candidate",
        "범용 사건 논리식이 먼저다 — 주문수 빌더도 극성별 창을 하나만 담는다(이탈 문형에서 "
        "존재 창 또는 부재 창 한쪽이 사라진다).",
    ),
    (
        "build_group_ranking_sql_candidate",
        "build_member_metric_ranking_sql_candidate",
        "그룹별 Top-N(PARTITION BY)이 먼저다 — 전역 랭킹이 '매출 높은 회원'을 가로채면 "
        "'지역별 N명씩'의 그룹 축이 버려진다.",
    ),
)

# 어느 플랜에도 후보를 낼 수 있는 종단 폴백. 앞에 오면 뒤 빌더가 영영 시도되지 않는다.
TERMINAL_BUILDER_NAME = "build_member_targets_sql_candidate"

# ── 배타 라우팅: "이 슬롯이 있으면 이 빌더만 시도한다" ────────────────────────────────
#
# 우선순위 스캔의 예외다. 어떤 컴파일러는 **경쟁시키면 안 된다** — 다른 빌더가 같은 조건을
# 더 단순한(그래서 의미가 축소된) SQL 로 표현해 버리고, 그게 '첫 유효 후보'로 채택되기 때문이다.
# 이 규칙이 코드 안 if 문으로만 있으면 "왜 여기만 예외인가"가 사라지므로 사유와 함께 선언한다.
EXCLUSIVE_ROUTES: tuple[tuple[str, str, str], ...] = (
    (
        "event_expression",
        "build_event_expression_sql_candidate",
        "범용 사건 논리식은 기간·극성을 트리로 들고 있다. 다른 빌더는 극성별 창을 하나씩만 담을 수 "
        "있어 경쟁시키면 한쪽 창이 사라진 채 '그럴듯한' SQL 이 먼저 채택된다.",
    ),
)


def exclusive_route_for(plan_keys: Iterable[str]) -> str | None:
    """플랜에 존재하는 슬롯 중 배타 라우팅이 선언된 것이 있으면 그 빌더 이름을 돌려준다."""
    present = set(plan_keys)
    for slot, builder, _reason in EXCLUSIVE_ROUTES:
        if slot in present:
            return builder
    return None


def derive_builder_order(names: Sequence[str]) -> list[str]:
    """선언된 선행 제약으로 빌더 순서를 **파생**한다(안정 위상정렬).

    같은 위계에서는 입력 순서를 유지하므로, 제약이 현재 순서와 모순되지 않는 한 결과는
    입력과 같다. 이 함수의 쓸모는 순서를 바꾸는 것이 아니라 **제약이 일관적이고 현재 순서를
    설명하는지**를 기계로 확인하는 것이다(순환이면 예외, 모순이면 다른 순서가 나온다).
    """
    order = {name: index for index, name in enumerate(names)}
    successors: dict[str, set[str]] = {name: set() for name in names}
    indegree: dict[str, int] = {name: 0 for name in names}
    for earlier, later, _reason in BUILDER_PRECEDENCE:
        if earlier not in order or later not in order or later in successors[earlier]:
            continue
        successors[earlier].add(later)
        indegree[later] += 1
    # 종단 폴백은 모든 빌더 뒤에 온다(명시 제약이 없어도).
    if TERMINAL_BUILDER_NAME in order:
        for name in names:
            if name != TERMINAL_BUILDER_NAME and TERMINAL_BUILDER_NAME not in successors[name]:
                successors[name].add(TERMINAL_BUILDER_NAME)
                indegree[TERMINAL_BUILDER_NAME] += 1

    ready = sorted((name for name in names if indegree[name] == 0), key=order.__getitem__)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for successor in sorted(successors[current], key=order.__getitem__):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
        ready.sort(key=order.__getitem__)
    if len(result) != len(names):
        raise BuilderContractError(
            "선행 제약에 순환이 있다 — 위상정렬 불가: "
            f"{sorted(set(names) - set(result))}"
        )
    return result


def builder_order_issues(
    builder_registry: Iterable[tuple[Any, frozenset[str]]],
) -> list[str]:
    """축 E: 선언된 선행 제약이 실제 튜플 순서에서 성립하는가."""
    names = [getattr(builder, "__name__", str(builder)) for builder, _kinds in builder_registry]
    position = {name: index for index, name in enumerate(names)}
    issues: list[str] = []

    for earlier, later, reason in BUILDER_PRECEDENCE:
        missing = [name for name in (earlier, later) if name not in position]
        if missing:
            issues.append(
                f"precedence_builder_missing: {', '.join(missing)} — 레지스트리에 없는 빌더를 "
                f"선행 제약이 가리킨다(스테일 제약). 제약을 지우거나 빌더를 되살려라."
            )
            continue
        if position[earlier] >= position[later]:
            issues.append(
                f"precedence_violated: {earlier}(#{position[earlier]}) 가 "
                f"{later}(#{position[later]}) 보다 뒤에 있다 — {reason}"
            )

    if TERMINAL_BUILDER_NAME not in position:
        issues.append(
            f"terminal_builder_missing: {TERMINAL_BUILDER_NAME} 가 레지스트리에 없다."
        )
    elif position[TERMINAL_BUILDER_NAME] != len(names) - 1:
        issues.append(
            f"terminal_builder_not_last: {TERMINAL_BUILDER_NAME} 가 #{position[TERMINAL_BUILDER_NAME]} "
            f"(전체 {len(names)}개) — 종단 폴백이 앞에 오면 뒤 빌더가 영영 시도되지 않는다."
        )

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        issues.append(f"builder_registered_twice: {duplicates} — 같은 빌더가 두 번 시도된다.")
    return issues


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
    """전 축 검증. builder_registry 미주입 시 축 C·E 는 건너뛴다(경량 배선용 — preflight)."""
    issues = slot_label_issues() + plan_exposure_issues() + requirement_capability_issues()
    if builder_registry is not None:
        registry = tuple(builder_registry)
        issues += builder_ownership_issues(registry)
        issues += builder_order_issues(registry)
    return issues


def enforce_builder_contracts(
    builder_registry: Iterable[tuple[Any, frozenset[str]]],
) -> None:
    """축 C·E 를 **import 시점에** 강제한다 — 위반이면 모듈 로딩이 실패한다.

    테스트가 아니라 여기서 막는 이유: 이 저장소에서 빌더 계약 테스트는 두 번(ce39f68, 8ba50b6)
    일괄 정리 커밋에 휩쓸려 삭제됐다. 테스트 파일은 지워질 수 있지만 기동 경로의 호출은
    지우면 즉시 드러난다. 테스트(tests/test_capability_contract.py, test_builder_order_contract.py)는
    그대로 두되, 마지막 방어선은 코드 안에 둔다.
    """
    registry = tuple(builder_registry)
    issues = builder_ownership_issues(registry) + builder_order_issues(registry)
    if issues:
        raise BuilderContractError(
            "SQL 빌더 레지스트리 계약 위반 — 순서/소유권이 곧 어떤 SQL 이 나가는지를 결정한다:\n  "
            + "\n  ".join(issues)
        )


def capability_check_summary(
    builder_registry: Iterable[tuple[Any, frozenset[str]]] | None = None,
) -> dict[str, Any]:
    """응답 capability_check 필드용 요약(파생 — plan 을 변형하지 않는다)."""
    issues = validate_capabilities(builder_registry)
    checked = ["slot_labels", "plan_exposure", "requirement_capabilities"]
    if builder_registry is not None:
        checked.extend(("builder_ownership", "builder_order"))
    return {
        "status": "ok" if not issues else "issues",
        "checked": checked,
        "issues": issues,
    }
