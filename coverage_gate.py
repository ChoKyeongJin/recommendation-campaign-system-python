"""출고 전 **커버리지 게이트** — 원장을 세우고, provenance 로 잇고, 부분 SQL 을 막는다.

이 모듈은 세 계층을 조립하는 자리다:

    :mod:`semantic_ledger`   요구와 그 귀결(terminal disposition)
    :mod:`provenance`        IR 노드 → 영수증 → SQL artifact
    :mod:`result_shape` · :mod:`temporal_clause`   요구를 만드는 typed 재료

무엇을 **차단**하는가
---------------------
차단은 "증명할 수 있는 누락"에만 건다. 증명 없이 막으면 정상 SQL 이 partial_sql 로 강등되고,
그런 가드는 안전망이 아니라 가짜 회귀 장치가 된다(코퍼스 #78 의 required_clauses 가 정확히
그렇게 틀렸다). 오늘 증명 가능한 것은 둘이다:

1. **결과 형태**  — 회원 목록이 아닌 형태를 요청했는데 SQL 에 투영·집계 artifact 가 없다.
   회원 목록을 '회원 수' 질문의 답인 척 내보내는 자리다(#71).
2. **시간 창**    — 원문이 말한 기간이 IR 의 어떤 창으로도 남지 않았다. 창이 조용히 사라진
   SQL 은 다른 모수를 낸다(#78 의 위험).

나머지 요구는 원장에 기록하고 기존 회계가 정한 귀결을 그대로 싣는다 — 이 게이트가 두 번째
판정자가 되면 같은 사실에 두 개의 답이 생긴다.

역방향(출처 없는 artifact)은 **진단**으로만 낸다. 회원 속성 술어처럼 Event IR 밖의 컴파일러가
소유한 조각이 정상적으로 존재하므로, 오늘 그것을 차단으로 쓰면 거짓 양성이 된다. 그 사실을
숨기지 않고 ``orphan_artifacts`` 로 노출한다.

실행: python -m pytest tests/test_coverage_gate.py -q
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import provenance
import result_shape
import semantic_ledger
import temporal_clause

GATE_ENV = "REQUIREMENT_COVERAGE_GATE"
MODE_ENFORCE = "enforce"
MODE_SHADOW = "shadow"
MODE_OFF = "off"

KIND_RESULT_SHAPE = "result_shape"
KIND_TEMPORAL_WINDOW = "temporal_window"
KIND_SOURCE_CONDITION = "source_condition"

# 단위별 실제 일수 범위. '6개월'을 180일 하나로 못 박으면 같은 뜻의 달력 구간(181~184일)이
# 불일치가 된다 — 그 오차는 임의 관용이 아니라 **달력 자체의 폭**이므로 여기 선언한다.
# 반개구간/포함 구간 표기 차이 1일은 아래 슬랙이 흡수한다.
_UNIT_DAY_RANGE: dict[str, tuple[int, int]] = {
    "day": (1, 1),
    "week": (7, 7),
    "month": (28, 31),
    "year": (365, 366),
}
_BOUNDARY_SLACK_DAYS = 1


def gate_mode() -> str:
    mode = os.getenv(GATE_ENV, MODE_ENFORCE).strip().casefold()
    return mode if mode in {MODE_ENFORCE, MODE_SHADOW, MODE_OFF} else MODE_ENFORCE


@dataclass(frozen=True, slots=True)
class GateResult:
    """게이트 판정 + 그 근거 전부(응답 진단에 그대로 실린다)."""

    verdict: semantic_ledger.CoverageVerdict
    ledger: semantic_ledger.RequirementLedger
    graph: provenance.ProvenanceGraph
    mode: str

    @property
    def blocks(self) -> bool:
        return self.mode == MODE_ENFORCE and not self.verdict.is_shippable

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "blocks": self.blocks,
            "verdict": self.verdict.to_dict(),
            "ledger": self.ledger.to_dict(),
            "provenance": self.graph.to_dict(),
            "orphan_artifacts": [
                artifact.to_dict() for artifact in self.graph.orphan_artifacts()
            ],
        }


def _window_days(window: Any) -> int | None:
    """IR 창의 일수(환산 불가면 None)."""
    import event_ir

    if isinstance(window, event_ir.RollingWindow):
        return window.days
    if isinstance(window, event_ir.RelativeWindow):
        return window.value * event_ir.UNIT_DAYS.get(window.unit, 0) or None
    if isinstance(window, event_ir.AbsoluteInterval):
        calendar = window.to_calendar_window()
        try:
            from datetime import date

            start = date.fromisoformat(str(calendar["from"]))
            end = date.fromisoformat(str(calendar["to"]))
        except (KeyError, TypeError, ValueError):
            return None
        return (end - start).days + 1
    return None


def _clause_day_range(clause: temporal_clause.TemporalClause) -> tuple[int, int] | None:
    """이 절이 뜻하는 기간의 **일수 범위**(달력 폭 + 경계 표기 슬랙)."""
    import event_ir

    if clause.amount is None or clause.unit is None:
        return None
    unit = event_ir.canonical_unit(clause.unit)
    low, high = _UNIT_DAY_RANGE.get(str(unit), (0, 0))
    if not low:
        return None
    return (
        clause.amount * low - _BOUNDARY_SLACK_DAYS,
        clause.amount * high + _BOUNDARY_SLACK_DAYS,
    )


def _temporal_requirements(
    query: str,
    literal_bindings: Sequence[Mapping[str, Any]] | None,
    expression: Any,
) -> list[tuple[semantic_ledger.SemanticRequirement, bool]]:
    """원문의 수량화된 시간 절 → 요구. 두 번째 값은 'IR 이 그 창을 들고 있는가'."""
    import event_ir

    ir_windows = event_ir.time_windows(expression) if expression is not None else []
    ir_days = [days for days in (_window_days(window) for window in ir_windows) if days]
    requirements: list[tuple[semantic_ledger.SemanticRequirement, bool]] = []
    for clause in temporal_clause.combine_temporal_clauses(query, literal_bindings):
        # 표지에 묶인 창만 요구로 승격한다. 표지 없는 duration 은 임계값일 수 있고
        # ('구매주기가 30일 이하'), 그것을 창으로 단정하면 정상 SQL 이 부분 SQL 로 강등된다.
        if not (clause.is_quantified and clause.marker_bound):
            continue
        spans = tuple(
            semantic_ledger.EvidenceSpan(span.start, span.end, span.text)
            for span in clause.source_spans
        )
        typed = {"relation": clause.relation, "amount": clause.amount, "unit": clause.unit}
        wanted = _clause_day_range(clause)
        # 표현 방식(rolling/relative/absolute)은 lowering 의 선택이다. 요구가 묻는 것은
        # '그 길이의 창이 남아 있는가' 하나다 — 물리 표현을 지정하면 §5 를 어긴다.
        satisfied = wanted is not None and any(
            wanted[0] <= days <= wanted[1] for days in ir_days
        )
        requirements.append(
            (
                semantic_ledger.SemanticRequirement(
                    requirement_id=semantic_ledger.requirement_id(
                        canonical_kind=KIND_TEMPORAL_WINDOW, spans=spans, typed_value=typed
                    ),
                    canonical_kind=KIND_TEMPORAL_WINDOW,
                    typed_value=typed,
                    source_spans=spans,
                    ownership=semantic_ledger.OWNERSHIP_AUDIENCE,
                ),
                satisfied,
            )
        )
    return requirements


def _result_shape_requirement(
    shape: result_shape.ResultShape,
) -> semantic_ledger.SemanticRequirement:
    typed = shape.to_dict()
    return semantic_ledger.SemanticRequirement(
        requirement_id=semantic_ledger.requirement_id(
            canonical_kind=KIND_RESULT_SHAPE, spans=(), typed_value=typed
        ),
        canonical_kind=KIND_RESULT_SHAPE,
        typed_value=typed,
        ownership=semantic_ledger.OWNERSHIP_PROJECTION,
    )


def _accounted_requirements(
    accounted: Sequence[Mapping[str, Any]],
) -> list[tuple[semantic_ledger.SemanticRequirement, str]]:
    """기존 회계(:mod:`semantic_requirements`) 결과를 원장 항목으로 옮긴다.

    귀결은 **그대로** 가져온다 — 이 게이트가 같은 요구를 다시 판정하면 한 사실에 답이 둘이 된다.
    """
    mapping = {
        "compiled": semantic_ledger.DISPOSITION_COMPILED,
        "parsed": semantic_ledger.DISPOSITION_COMPILED,
        "unsupported": semantic_ledger.DISPOSITION_UNSUPPORTED,
        "clarification": semantic_ledger.DISPOSITION_CLARIFICATION,
    }
    items: list[tuple[semantic_ledger.SemanticRequirement, str]] = []
    for raw in accounted:
        if not isinstance(raw, Mapping):
            continue
        disposition = mapping.get(str(raw.get("status") or ""))
        if disposition is None:
            continue
        span = raw.get("source_span")
        spans: tuple[semantic_ledger.EvidenceSpan, ...] = ()
        if isinstance(span, Mapping):
            spans = (
                semantic_ledger.EvidenceSpan(
                    int(span.get("start", 0)),
                    int(span.get("end", 0)),
                    str(raw.get("source_text") or ""),
                ),
            )
        typed = {"path": raw.get("path"), "base": raw.get("base")}
        items.append(
            (
                semantic_ledger.SemanticRequirement(
                    requirement_id=semantic_ledger.requirement_id(
                        canonical_kind=KIND_SOURCE_CONDITION, spans=spans, typed_value=typed
                    ),
                    canonical_kind=KIND_SOURCE_CONDITION,
                    typed_value=typed,
                    source_spans=spans,
                    polarity=raw.get("polarity"),
                    ownership=semantic_ledger.OWNERSHIP_AUDIENCE,
                ),
                disposition,
            )
        )
    return items


def evaluate(
    *,
    query: str,
    sql: str,
    expression: Any,
    shape: result_shape.ResultShape | None,
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
    accounted_requirements: Sequence[Mapping[str, Any]] = (),
    dialect: str = "tsql",
    extra_artifacts: Sequence[Mapping[str, Any]] = (),
) -> GateResult:
    """출고 직전 판정. 차단 여부와 그 근거를 한 객체로 돌려준다."""

    graph = provenance.build_graph(
        event_expression=expression,
        sql=sql,
        dialect=dialect,
        extra_artifacts=extra_artifacts,
    )
    ledger = semantic_ledger.RequirementLedger()

    # ① 기존 회계가 이미 정한 귀결.
    for requirement, disposition in _accounted_requirements(accounted_requirements):
        ledger.add(requirement)
        ledger.settle(requirement.requirement_id, disposition, reason_code="source_accounting")

    # ② 결과 형태.
    if shape is not None:
        requirement = _result_shape_requirement(shape)
        ledger.add(requirement)
        artifacts = [
            artifact.to_dict()
            for artifact in graph.artifacts.values()
            if artifact.kind
            in {
                provenance.ARTIFACT_PROJECTION,
                provenance.ARTIFACT_AGGREGATION,
                provenance.ARTIFACT_GROUPING,
                provenance.ARTIFACT_ORDERING,
                provenance.ARTIFACT_LIMIT,
            }
        ]
        if result_shape.shape_requirement_satisfied(shape, artifacts):
            receipt = graph.add_receipt(
                provenance.CompileReceipt(
                    receipt_id=f"cr_shape_{requirement.requirement_id}",
                    stage="projection_lowering",
                    requirement_ids=(requirement.requirement_id,),
                    artifact_ids=tuple(str(item["artifact_id"]) for item in artifacts),
                )
            )
            ledger.settle(
                requirement.requirement_id,
                semantic_ledger.DISPOSITION_COMPILED,
                reason_code="result_shape_projected",
                ir_node_ids=("shape",),
                compile_receipt_ids=(receipt.receipt_id,),
                sql_artifact_ids=receipt.artifact_ids,
            )

    # ③ 시간 창.
    for requirement, satisfied in _temporal_requirements(query, literal_bindings, expression):
        ledger.add(requirement)
        if not satisfied:
            continue
        span = requirement.span or (0, 0)
        nodes = graph.nodes_covering_span(*span)
        receipts = [
            receipt
            for node in nodes
            for receipt in graph.receipts_for_node(node.node_id)
        ]
        receipt_ids = tuple(receipt.receipt_id for receipt in receipts)
        artifact_ids = tuple(
            artifact_id for receipt in receipts for artifact_id in receipt.artifact_ids
        )
        if not receipt_ids:
            # 근거 구간으로 노드를 못 짚어도 창 자체는 IR 에 남아 있다. 그 사실만으로
            # 영수증 하나를 만든다 — 추적 고리가 없다고 요구를 미해결로 두면 표현 방식
            # 차이가 곧 거짓 차단이 된다.
            receipt = graph.add_receipt(
                provenance.CompileReceipt(
                    receipt_id=f"cr_window_{requirement.requirement_id}",
                    stage="audience_lowering",
                    requirement_ids=(requirement.requirement_id,),
                    detail={"matched_by": "ir_window_duration"},
                )
            )
            receipt_ids, artifact_ids = (receipt.receipt_id,), ()
        ledger.settle(
            requirement.requirement_id,
            semantic_ledger.DISPOSITION_COMPILED,
            reason_code="temporal_window_preserved",
            ir_node_ids=tuple(node.node_id for node in nodes),
            compile_receipt_ids=receipt_ids,
            sql_artifact_ids=artifact_ids,
        )

    return GateResult(
        verdict=semantic_ledger.verdict(ledger),
        ledger=ledger,
        graph=graph,
        mode=gate_mode(),
    )


def evaluate_for_plan(
    plan: dict[str, Any],
    *,
    query: str,
    sql: str,
    expression: Any,
    shape: result_shape.ResultShape | None,
    accounted_requirements: Sequence[Mapping[str, Any]] = (),
    dialect: str = "tsql",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """플랜을 재료로 게이트를 돌리고, 원장·provenance 를 플랜에 남긴다.

    돌려주는 둘째 값은 **차단 지시**다(차단하지 않으면 ``None``). 호출자가 판정 로직을 다시
    쓰지 않도록 사유와 사용자 문구까지 여기서 만든다 — 판정이 두 곳에 있으면 갈라진다.
    """

    gate = evaluate(
        query=query,
        sql=sql,
        expression=expression,
        shape=shape,
        literal_bindings=plan.get("literal_bindings"),
        accounted_requirements=accounted_requirements,
        dialect=dialect,
    )
    semantic_ledger.write_ledger(plan, gate.ledger)
    plan[provenance.PROVENANCE_KEY] = gate.graph.to_dict()
    payload = {"ran": True, **gate.to_dict()}
    if not gate.blocks:
        return payload, None
    return payload, {
        "failure_reason": gate.verdict.reason_code or semantic_ledger.REASON_OPEN_REQUIREMENTS,
        "clarification_questions": clarification_questions(gate),
    }


def response_fields(plan: Mapping[str, Any], sql_result: Mapping[str, Any]) -> dict[str, Any]:
    """의미 완전성 계층이 응답에 싣는 필드 묶음(§1·§2·§4·§6·§9).

    한 곳에 모으는 이유는 하나다 — 이 필드들은 함께 읽어야 뜻이 통한다. 흩어 두면 응답
    계약이 바뀔 때 일부만 갱신되고, 그 상태의 진단은 서로를 반박한다.
    """
    import compile_outcome
    import result_shape as result_shape_module
    import semantic_fingerprint
    import targeting_policy

    return {
        # 요청이 요구한 결과 형태. 회원 목록인지 스칼라 집계인지가 응답 자체에 드러난다.
        "result_shape": dict(plan.get(result_shape_module.RESULT_SHAPE_KEY) or {}) or None,
        # 정책 결정 영수증. "왜 되묻는가/왜 통과했는가"의 근거.
        "policy_decisions": targeting_policy.decisions(plan),
        "policy_version": targeting_policy.POLICY_VERSION,
        # 요구 커버리지 게이트(원장 + provenance + 판정). SQL 이 없으면 ran=False.
        "coverage_gate": sql_result.get("coverage_gate", {"ran": False}),
        # 컴파일 귀결 원장: 후보를 못 낸 이유가 단계·코드·요구 id 로 남는다.
        "compile_outcomes": compile_outcome.outcomes(plan),
        # 지문 두 축. 같은 뜻이면 semantic 이 같고, 환경이 다르면 execution 이 다르다.
        "semantic_fingerprint": plan.get(semantic_fingerprint.SEMANTIC_FINGERPRINT_KEY),
        "execution_fingerprint": plan.get(semantic_fingerprint.EXECUTION_FINGERPRINT_KEY),
    }


def clarification_questions(result: GateResult) -> list[str]:
    """차단 사유를 사용자 언어로. 내부 코드·경로를 노출하지 않는다."""
    questions: list[str] = []
    for requirement in result.ledger.open_requirements():
        if requirement.canonical_kind == KIND_RESULT_SHAPE:
            questions.append(
                "요청하신 결과 형태(집계 결과)를 만들지 못했습니다. "
                "무엇을 세거나 합산할지 구체적으로 적어 주세요."
            )
        elif requirement.canonical_kind == KIND_TEMPORAL_WINDOW:
            spans = " ".join(span.text for span in requirement.source_spans if span.text)
            questions.append(
                f"'{spans}' 기간이 조건에 반영되지 않았습니다. 기간을 다시 확인해 주세요."
                if spans
                else "요청하신 기간이 조건에 반영되지 않았습니다. 기간을 다시 확인해 주세요."
            )
        else:
            spans = " ".join(span.text for span in requirement.source_spans if span.text)
            questions.append(
                f"'{spans}' 조건이 결과에 반영되지 않았습니다. 조건을 다시 확인해 주세요."
                if spans
                else "요청하신 조건 일부가 결과에 반영되지 않았습니다."
            )
    # 같은 문장이 여러 요구에서 나올 수 있다 — 사용자에게 같은 말을 두 번 하지 않는다.
    return list(dict.fromkeys(questions))


__all__ = [
    "GATE_ENV",
    "KIND_RESULT_SHAPE",
    "KIND_SOURCE_CONDITION",
    "KIND_TEMPORAL_WINDOW",
    "MODE_ENFORCE",
    "MODE_OFF",
    "MODE_SHADOW",
    "GateResult",
    "clarification_questions",
    "evaluate",
    "evaluate_for_plan",
    "gate_mode",
]
