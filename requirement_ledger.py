"""요구사항 원장 — 사용자 조건 하나를 **끝까지 추적 가능한 레코드 하나**로 만든다.

왜: 지금까지 한 조건의 정보는 다섯 군데에 흩어져 있었다. 의미 노드(semantic_plan), 결핍
계산(missing_fields), capability 판정(capability_verdicts), 컴파일 산출(node_slots),
검증 실패(validation_errors). 그래서 "이 조건이 왜 안 나왔나"에 답하려면 다섯 곳을 조인해야
했고, 어느 한 곳에서 조용히 빠진 조건은 **아무 데도 나타나지 않았다**(가짜 성공).

원장은 그 조인을 결정론으로 한 번만 하고, 조건마다 다음을 **전부** 보존한다:

    requirement_id      조건 식별자(노드 id 또는 미커버 구간 id)
    label               사용자에게 보이는 조건 라벨
    source_span         사용자 원문 구절(+ 좌표)
    predicate           정규화된 술어(값 정규화 이후의 노드 값)
    temporal            시간 한정어(범용 연산자)
    required_grain      필요한 데이터 그레인
    capability          5축 판정 + 사유
    candidate_slots     후보 출력 슬롯
    validation          최종 검증 결과(status + failure_code + reason)

**원장에 없는 조건은 없는 것이고, 원장에 pending 이 있으면 resolved 가 될 수 없다.**
그 두 문장이 가짜 성공을 구조적으로 막는다.

범용 코어 규약: graph_rag 를 import 하지 않는다. 라벨·슬롯 카탈로그는 주입받는다 —
이 파일에 도메인 이름이 없다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import semantic_domain_binding
import semantic_plan
import temporal_semantics
from semantic_plan import SemanticNode, SemanticPlanV2

# ── 요구사항 귀결(닫힌 집합) ────────────────────────────────────────────────────
# '어디까지 갔는가'이지 '성공/실패'가 아니다 — 실패의 **종류**는 failure_code 가 말한다.
COMPILED = "compiled"                    # 실행 계획으로 옮겨졌다
PENDING = "pending"                      # 인자/근거가 모자라 아직 귀결되지 않았다
UNSUPPORTED = "unsupported"              # 능력의 부재(사용자에게 그렇게 말해도 된다)
FAILED = "failed"                        # 우리 쪽 사고(미지원으로 표시하면 안 된다)
UNCOVERED = "uncovered"                  # 원문에 근거가 있는데 의미 노드가 없다

OUTCOMES: frozenset[str] = frozenset({COMPILED, PENDING, UNSUPPORTED, FAILED, UNCOVERED})

# 아직 해결되지 않은(= 재방출 후보인) 귀결.
UNRESOLVED_OUTCOMES: frozenset[str] = frozenset({PENDING, UNCOVERED})

# failure_code → 사용자에게 능력 부재로 말해도 되는가.
_USER_FACING = semantic_plan.USER_FACING_UNSUPPORTED
_INTERNAL = semantic_plan.INTERNAL_FAILURE_CODES


def outcome_for(failure_code: str | None) -> str:
    """실패 코드 → 귀결. 내부 사고를 '미지원'으로 접지 않는 유일한 판정 지점."""
    if failure_code is None:
        return COMPILED
    if failure_code in _USER_FACING:
        return UNSUPPORTED
    if failure_code in _INTERNAL:
        return FAILED
    # missing_argument / ambiguous_requirement — 사용자에게 물어야 하는 것들.
    return PENDING


@dataclass
class Requirement:
    """조건 하나의 전 생애 레코드."""

    requirement_id: str
    label: str
    node_type: str = ""
    source_span: str = ""
    source_start: int | None = None
    source_end: int | None = None
    predicate: dict[str, Any] = field(default_factory=dict)
    temporal: dict[str, Any] | None = None
    required_grain: str | None = None
    capability: dict[str, Any] = field(default_factory=dict)
    candidate_slots: tuple[str, ...] = ()
    compiled_slot: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return str(self.validation.get("outcome") or PENDING)

    @property
    def failure_code(self) -> str | None:
        code = self.validation.get("failure_code")
        return str(code) if code else None

    @property
    def is_resolved(self) -> bool:
        return self.outcome == COMPILED

    @property
    def is_unresolved(self) -> bool:
        return self.outcome in UNRESOLVED_OUTCOMES

    @property
    def blocks_execution(self) -> bool:
        """이 조건 때문에 응답을 출고하면 안 되는가(= 조용히 빠뜨리면 가짜 성공)."""
        return self.outcome != COMPILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "label": self.label,
            "node_type": self.node_type,
            "source_span": self.source_span,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "predicate": copy.deepcopy(self.predicate),
            "temporal": copy.deepcopy(self.temporal),
            "required_grain": self.required_grain,
            "capability": copy.deepcopy(self.capability),
            "candidate_slots": list(self.candidate_slots),
            "compiled_slot": self.compiled_slot,
            "validation": copy.deepcopy(self.validation),
        }


@dataclass
class RequirementLedger:
    """한 요청의 요구사항 전체 + 파생 판정."""

    requirements: list[Requirement] = field(default_factory=list)

    def __iter__(self):
        return iter(self.requirements)

    def __len__(self) -> int:
        return len(self.requirements)

    def by_id(self, requirement_id: str) -> Requirement | None:
        for requirement in self.requirements:
            if requirement.requirement_id == requirement_id:
                return requirement
        return None

    def with_outcome(self, *outcomes: str) -> list[Requirement]:
        wanted = frozenset(outcomes)
        return [item for item in self.requirements if item.outcome in wanted]

    def resolved(self) -> list[Requirement]:
        return self.with_outcome(COMPILED)

    def unresolved(self) -> list[Requirement]:
        """재방출로 메울 수 있는 것만(미지원·내부 사고는 재방출 대상이 아니다)."""
        return [item for item in self.requirements if item.is_unresolved]

    def unsupported(self) -> list[Requirement]:
        return self.with_outcome(UNSUPPORTED)

    def failed(self) -> list[Requirement]:
        return self.with_outcome(FAILED)

    def is_complete(self) -> bool:
        """모든 조건이 실행 계획으로 옮겨졌는가 — resolved 를 선언할 수 있는 유일한 조건."""
        return bool(self.requirements) and all(item.is_resolved for item in self.requirements)

    def protected_ids(self) -> frozenset[str]:
        """재방출이 건드리면 안 되는 요구사항(이미 통과한 것)."""
        return frozenset(item.requirement_id for item in self.requirements if item.is_resolved)

    def summary(self) -> dict[str, int]:
        counts = {outcome: 0 for outcome in sorted(OUTCOMES)}
        for item in self.requirements:
            counts[item.outcome] = counts.get(item.outcome, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": [item.to_dict() for item in self.requirements],
            "summary": self.summary(),
            "is_complete": self.is_complete(),
        }


# ── 원장 조립 ────────────────────────────────────────────────────────────────────
def _label_for(node: SemanticNode, *, labeller: Any = None) -> str:
    if callable(labeller):
        label = labeller(node)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return semantic_domain_binding.condition_label(node.type)


def _predicate_of(node: SemanticNode) -> dict[str, Any]:
    """정규화된 술어 — 시간 한정어와 하위 노드는 별도 필드로 빠진다."""
    predicate: dict[str, Any] = {}
    for spec in type(node).FIELDS:
        if spec.kind in {"temporal", "nodes"}:
            continue
        value = node.values.get(spec.name)
        if value in (None, "", [], {}):
            continue
        predicate[spec.name] = copy.deepcopy(value)
    return predicate


def _temporal_of(node: SemanticNode) -> dict[str, Any] | None:
    raw = node.values.get("temporal")
    if isinstance(raw, Mapping) and raw.get("operator"):
        return dict(copy.deepcopy(raw))
    return None


def _capability_of(
    node: SemanticNode, verdicts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    verdict = verdicts.get(node.id)
    if not isinstance(verdict, Mapping):
        # 판정 목록에 없다 = 실행 가능 판정(레지스트리는 불가만 싣는다).
        return {"executable_in_request": True, "reason": None}
    axes = verdict.get("axes")
    payload: dict[str, Any] = dict(axes) if isinstance(axes, Mapping) else {}
    payload.setdefault("executable_in_request", bool(verdict.get("executable")))
    payload["failure_code"] = verdict.get("failure_code")
    payload["reason"] = verdict.get("message")
    return payload


def _validation_of(
    node: SemanticNode,
    *,
    capability: Mapping[str, Any],
    compiled_slot: str | None,
    compile_failure: Mapping[str, Any] | None,
    node_errors: Sequence[Mapping[str, Any]],
    contested: bool,
) -> dict[str, Any]:
    """조건 하나의 최종 검증 결과. 우선순위: 내부 사고 > 미지원 > 결핍/모호 > 컴파일."""
    missing = node.missing_fields()
    invalid = node.invalid_values()

    if node_errors:
        first = node_errors[0]
        return {
            "outcome": outcome_for(str(first.get("failure_code") or semantic_plan.INTERNAL_FAULT)),
            "failure_code": first.get("failure_code") or semantic_plan.INTERNAL_FAULT,
            "reason": _field_error_reason(node, first),
        }
    if invalid:
        name, received, allowed = invalid[0]
        return {
            "outcome": FAILED,
            "failure_code": semantic_plan.VALIDATION_MISMATCH,
            "reason": (
                f"'{name}' 값 {received!r} 이 선언된 어휘에 없습니다(허용: {', '.join(allowed)})."
            ),
        }
    capability_code = capability.get("failure_code")
    if capability_code:
        return {
            "outcome": outcome_for(str(capability_code)),
            "failure_code": capability_code,
            "reason": capability.get("reason"),
        }
    if contested:
        return {
            "outcome": PENDING,
            "failure_code": semantic_plan.AMBIGUOUS_REQUIREMENT,
            "reason": "같은 원문 근거가 둘 이상의 의미로 해석됩니다.",
        }
    if missing:
        return {
            "outcome": PENDING,
            "failure_code": semantic_plan.MISSING_ARGUMENT,
            "reason": f"확정되지 않은 항목: {', '.join(missing)}",
            "missing_fields": list(missing),
        }
    temporal_missing = _temporal_missing_arguments(node)
    if temporal_missing:
        return {
            "outcome": PENDING,
            "failure_code": semantic_plan.MISSING_ARGUMENT,
            "reason": f"시간 한정어의 인자가 없습니다: {', '.join(temporal_missing)}",
            "missing_fields": [f"temporal.{name}" for name in temporal_missing],
        }
    if compile_failure is not None:
        code = str(compile_failure.get("failure_code") or semantic_plan.INTERNAL_FAULT)
        return {
            "outcome": outcome_for(code),
            "failure_code": code,
            "reason": compile_failure.get("reason"),
        }
    if compiled_slot:
        return {"outcome": COMPILED, "failure_code": None, "reason": None, "slot": compiled_slot}
    return {
        "outcome": PENDING,
        "failure_code": semantic_plan.MISSING_ARGUMENT,
        "reason": "실행 계획으로 옮겨지지 않았습니다(컴파일 산출 없음).",
    }


def _field_error_reason(node: SemanticNode, error: Mapping[str, Any]) -> str:
    """정규화 오류를 **어느 필드가 어떤 값 종류를 기대했는가**로 말한다.

    파서 내부 문구('수량을 읽지 못했다: …')를 그대로 노출하면 사용자는 무엇을 고쳐야
    할지 알 수 없다. 필드와 기대 종류를 붙이면 오배선(범주 값을 수치 필드에 넣음)이
    사용자에게도, 로그를 읽는 사람에게도 즉시 보인다.
    """
    reason = str(error.get("reason") or "").strip()
    field_name = str(error.get("field") or "").strip()
    if not field_name:
        return reason
    spec = type(node).field_spec(field_name)
    kind = spec.kind if spec is not None else "값"
    received = error.get("received")
    detail = f" (받은 값: {received!r})" if received not in (None, "") else ""
    return f"'{field_name}' 필드가 {kind} 값을 기대했지만 해석하지 못했습니다{detail}. {reason}".strip()


def _temporal_missing_arguments(node: SemanticNode) -> tuple[str, ...]:
    raw = node.values.get("temporal")
    if not isinstance(raw, Mapping):
        return ()
    try:
        qualifier = temporal_semantics.normalize(
            dict(raw), aliases=semantic_domain_binding.temporal_aliases()
        )
    except temporal_semantics.TemporalSemanticsError:
        return ()
    return qualifier.missing_arguments() if qualifier else ()


def build_ledger(
    plan: SemanticPlanV2,
    *,
    compiled: Any = None,
    coverage: Any = None,
    slot_catalog: Mapping[str, Sequence[str]] | None = None,
    labeller: Any = None,
) -> RequirementLedger:
    """의미 플랜 + 컴파일 산출 + coverage 를 조인해 요구사항 원장을 만든다(순수 함수)."""
    verdicts: dict[str, Mapping[str, Any]] = {
        str(verdict.get("node_id")): verdict
        for verdict in plan.capability_verdicts
        if isinstance(verdict, Mapping) and verdict.get("node_id")
    }
    node_slots: Mapping[str, str] = getattr(compiled, "node_slots", {}) or {}
    compile_failures: dict[str, Mapping[str, Any]] = {
        str(failure.get("node_id")): failure
        for failure in (getattr(compiled, "failures", []) or [])
        if isinstance(failure, Mapping) and failure.get("node_id")
    }
    errors_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for error in plan.validation_errors:
        if isinstance(error, Mapping) and error.get("node_id"):
            errors_by_node.setdefault(str(error["node_id"]), []).append(error)
    contested_ids = {
        str(node_id)
        for item in (getattr(coverage, "contested_spans", []) or [])
        if isinstance(item, Mapping)
        for node_id in (item.get("node_ids") or [])
    }
    catalog = dict(slot_catalog or {})

    requirements: list[Requirement] = []
    owned_by_parent = {
        child.id
        for node in plan.walk()
        if node.type != semantic_plan.LogicalExpression.TYPE
        for child in node.children()
    }
    for node in plan.walk():
        if node.type == semantic_plan.LogicalExpression.TYPE or node.id in owned_by_parent:
            continue
        capability = _capability_of(node, verdicts)
        compiled_slot = node_slots.get(node.id)
        validation = _validation_of(
            node,
            capability=capability,
            compiled_slot=compiled_slot,
            compile_failure=compile_failures.get(node.id),
            node_errors=errors_by_node.get(node.id, []),
            contested=node.id in contested_ids,
        )
        requirements.append(
            Requirement(
                requirement_id=node.id,
                label=_label_for(node, labeller=labeller),
                node_type=node.type,
                source_span=node.source_span,
                source_start=node.source_start,
                source_end=node.source_end,
                predicate=_predicate_of(node),
                temporal=_temporal_of(node),
                required_grain=(
                    str(capability.get("required_grain"))
                    if capability.get("required_grain")
                    else None
                ),
                capability=capability,
                candidate_slots=tuple(catalog.get(node.type, ())),
                compiled_slot=compiled_slot,
                validation=validation,
            )
        )

    requirements.extend(_uncovered_requirements(plan, coverage))
    return RequirementLedger(requirements=requirements)


def _uncovered_requirements(plan: SemanticPlanV2, coverage: Any) -> list[Requirement]:
    """원문에 근거가 있는데 의미 노드가 없는 구간 — 조용히 사라지지 않도록 원장에 올린다."""
    items = list(getattr(coverage, "uncovered_requirements", None) or plan.uncovered_requirements)
    out: list[Requirement] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        span = str(item.get("source_span") or "").strip()
        out.append(
            Requirement(
                requirement_id=f"uncovered-{index}",
                label="해석되지 않은 조건",
                node_type="",
                source_span=span,
                source_start=item.get("source_start") if isinstance(item.get("source_start"), int) else None,
                source_end=item.get("source_end") if isinstance(item.get("source_end"), int) else None,
                capability={"executable_in_request": False, "reason": item.get("reason")},
                validation={
                    "outcome": UNCOVERED,
                    "failure_code": semantic_plan.MISSING_ARGUMENT,
                    "reason": str(item.get("reason") or "대응하는 의미 노드가 없습니다."),
                    "anchors": list(item.get("anchors") or []),
                },
            )
        )
    return out


def clarification_targets(ledger: RequirementLedger) -> list[dict[str, Any]]:
    """사용자에게 되물어야 하는 것 — 원문 구절과 무엇이 모자란지를 함께 준다."""
    return [
        {
            "requirement_id": item.requirement_id,
            "label": item.label,
            "source_span": item.source_span,
            "reason": item.validation.get("reason"),
            "missing_fields": item.validation.get("missing_fields") or [],
        }
        for item in ledger.requirements
        if item.outcome in UNRESOLVED_OUTCOMES
    ]


def merge_outcomes(previous: RequirementLedger, current: RequirementLedger) -> dict[str, Any]:
    """재방출 전후 비교 — **이미 통과한 요구사항이 훼손되지 않았는지**의 결정론 판정."""
    before = {item.requirement_id: item.outcome for item in previous.requirements}
    after = {item.requirement_id: item.outcome for item in current.requirements}
    regressed = sorted(
        requirement_id
        for requirement_id, outcome in before.items()
        if outcome == COMPILED and after.get(requirement_id) != COMPILED
    )
    gained = sorted(
        requirement_id
        for requirement_id, outcome in after.items()
        if outcome == COMPILED and before.get(requirement_id) != COMPILED
    )
    return {
        "regressed": regressed,
        "gained": gained,
        "before": previous.summary(),
        "after": current.summary(),
    }


__all__ = [
    "COMPILED",
    "FAILED",
    "OUTCOMES",
    "PENDING",
    "Requirement",
    "RequirementLedger",
    "UNCOVERED",
    "UNRESOLVED_OUTCOMES",
    "UNSUPPORTED",
    "build_ledger",
    "clarification_targets",
    "merge_outcomes",
    "outcome_for",
]
