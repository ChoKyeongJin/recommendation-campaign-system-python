"""실패 응답의 사용자 표시용 한국어 렌더링.

plan_validation 은 의도적으로 메시지를 만들지 않고(렌더링은 나중 경계 소관),
semantic_ir 의 missing_fields 는 모델 내부 필드명(영문)이다. 응답 조립이 사용자에게
"어떤 조건이·어디서·왜" 막혔는지 말할 수 있도록, 코드/필드명 → 한국어 문구 낮춤을
이 모듈이 단독 소유한다. 문구가 없으면 internal_invalid 가 사유 없는 '미지원'으로,
미확정 필드가 범용 문구('필수 비교 조건을 확인해 주세요')로 뭉개진다 —
26종 프롬프트 감사(2026-08-02)에서 확인된 실패 양식이다.
"""

from __future__ import annotations

from typing import Any

import plan_validation
import requirement_ledger

# semantic_ir missing_fields 의 첫 경로 세그먼트 → 한국어 라벨.
SEMANTIC_IR_FIELD_KO_LABELS: dict[str, str] = {
    "threshold": "임계값(얼마 이상/이하인지)",
    "comparison": "비교 조건(무엇을 어떤 기준과 비교하는지)",
    "purchase_date": "구매 기간",
    "inactivity_period": "휴면·미활동 기간",
    "lifecycle": "회원 등급·상태 조건",
    "metric": "기준 지표",
    "window": "적용 기간",
    "window_days": "적용 기간(일수)",
    "campaign": "대상 캠페인",
    "operator": "비교 연산(이상/이하/초과/미만)",
    "value": "기준 값",
}


# SemanticPlan 파생 결핍의 접두어별 렌더링. 파생 결핍은 `<node_id>.<field>` 또는
# `uncovered:<원문 구절>` / `conflict:<원문 구절>` 형태다 — 앞의 둘은 필드 라벨로, 뒤의 둘은
# 원문 구절 자체로 말해야 사용자가 무엇을 다시 적어야 할지 안다.
_SPAN_PREFIXES = ("uncovered:", "conflict:", "invalid:")


def semantic_failure_reason(status: str, failure_kind: Any) -> str:
    """failure_reason 도 원인을 말한다 — 운영자가 로그만 보고 어느 계층인지 알 수 있어야 한다.

    `semantic_ir_needs_clarification` 하나로 뭉치면 "사용자가 안 알려준 것", "구조화기가 못
    만든 것", "실행 설정이 비어 있는 것"이 같은 코드로 보인다 — 셋의 고칠 곳이 다 다르다.
    """
    import semantic_plan  # 지연 import — 렌더링 계층은 코어 스키마에 의존하지 않는다

    if failure_kind == semantic_plan.FAILURE_KIND_STRUCTURER:
        return "semantic_structurer_failure"
    if failure_kind == semantic_plan.FAILURE_KIND_SYSTEM:
        return "semantic_registry_gap"
    return f"semantic_ir_{status}"


def cause_missing_conditions(
    causes: Any, fallback_message: str, *, build: Any, label_of: Any
) -> list[dict[str, Any]]:
    """결핍 원인 → 사용자용 미충족 조건. **내부 필드명을 노출하지 않는다.**

    예전에는 `req-1.member_entity, ranked_set, relation` 이 그대로 나갔다 — 사용자가 정할 수
    없는 이름이다. 이제 보이는 것은 원문 구절과 조건 라벨뿐이다.
    """
    conditions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in causes or ():
        span = str(record.get("source_span") or "").strip()
        node_type = record.get("node_type")
        label = (label_of(node_type) if isinstance(node_type, str) and node_type else "") or span or "조건"
        key = f"{label}\0{span}"
        if key in seen:
            continue
        seen.add(key)
        question = str(record.get("question") or "").strip()
        reason = question or (
            f"'{span}' 조건을 실행 가능한 형태로 해석하지 못했습니다."
            if span else fallback_message
        )
        conditions.append(build(f"requirements.{len(conditions) + 1}", label, reason))
    return conditions


def semantic_ir_field_label(field: str) -> str:
    for prefix in _SPAN_PREFIXES:
        if field.startswith(prefix):
            return field[len(prefix):].strip() or field
    head = field.split(".", 1)[0].strip()
    if head in SEMANTIC_IR_FIELD_KO_LABELS:
        return SEMANTIC_IR_FIELD_KO_LABELS[head]
    # SemanticPlan 파생 결핍('req-1.operator')은 노드 id 가 아니라 **필드**가 사용자에게 의미 있다.
    leaf = field.rpartition(".")[2].strip()
    return SEMANTIC_IR_FIELD_KO_LABELS.get(leaf, field)


def semantic_ir_clarification_message(
    status: str, missing_field_labels: list[str]
) -> str:
    """missing_fields 라벨 목록으로 사용자에게 무엇을 보완할지 말하는 문구를 만든다."""
    if missing_field_labels:
        joined = ", ".join(f"'{label}'" for label in missing_field_labels)
        if status == "needs_clarification":
            return (
                f"요청에서 {joined} 항목을 확정하지 못했습니다. "
                "값·기간·비교 기준을 구체적으로 적어 주세요."
            )
        return f"요청의 {joined} 항목은 현재 지원하지 않는 연산입니다."
    if status == "needs_clarification":
        return "필수 비교 조건을 확인해 주세요."
    return "요청한 연산은 현재 지원하지 않습니다."


def requirement_failure_report(
    ledger_payload: Any,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """요구사항 원장(직렬화본) → (미충족 조건 목록, 실패 분류 보고).

    분류를 그대로 실어 보내는 것이 요점이다: 내부 사고(failed)와 능력 부재(unsupported)를
    한 덩어리로 뭉개면 사용자가 '이 서비스는 이걸 못 한다'로 오해한다. 소비자는
    `has_internal_failure` / `has_unsupported` 로 분기하고, 조건별 근거는 by_outcome 에 남는다.
    """
    requirements = ledger_payload.get("requirements") if isinstance(ledger_payload, dict) else None
    if not isinstance(requirements, list) or not requirements:
        return [], {}

    missing: list[dict[str, str]] = []
    questions: list[str] = []
    by_outcome: dict[str, list[dict[str, Any]]] = {}
    for item in requirements:
        if not isinstance(item, dict):
            continue
        validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
        outcome = str(validation.get("outcome") or "")
        if outcome == requirement_ledger.COMPILED:
            continue
        label = str(item.get("label") or "조건")
        span = str(item.get("source_span") or "").strip()
        reason = str(validation.get("reason") or "").strip()
        by_outcome.setdefault(outcome, []).append({
            "requirement_id": item.get("requirement_id"),
            "label": label,
            "source_span": span,
            "failure_code": validation.get("failure_code"),
            "reason": reason,
            "capability": item.get("capability"),
            "candidate_slots": item.get("candidate_slots"),
        })
        question = (
            f"'{span}' — {reason}" if span and reason
            else (reason or f"'{label}' 을 확정하지 못했습니다.")
        )
        if question not in questions:
            questions.append(question)
        missing.append({
            "path": f"requirements.{item.get('requirement_id')}",
            "label": label,
            "question": question,
        })
    return missing, {
        "by_outcome": by_outcome,
        "summary": ledger_payload.get("summary") or {},
        "clarification_questions": questions,
        "has_internal_failure": bool(by_outcome.get(requirement_ledger.FAILED)),
        "has_unsupported": bool(by_outcome.get(requirement_ledger.UNSUPPORTED)),
    }


def plan_validation_issue_ko(issue: plan_validation.PlanValidationIssue) -> str:
    """검증 이슈를 사용자 표시용 한국어 사유로 낮춘다(코드 패밀리별)."""
    code = str(issue.code or "")
    path = str(issue.path or "query_plan")
    if code.endswith("_schema_invalid"):
        return (
            f"'{path}' 해석 결과가 내부 형식 검증을 통과하지 못했습니다(코드: {code}). "
            "조건을 조금 다르게 표현해 다시 요청해 주세요."
        )
    if "projection_loss" in code:
        return f"'{path}' 조건이 실행 계획으로 옮겨지는 과정에서 유실됐습니다(코드: {code})."
    if "partial_coercion" in code:
        return f"'{path}' 조건이 실행 가능한 형식으로 변환되지 못했습니다(코드: {code})."
    if code.endswith("required_field_missing"):
        return f"'{path}' 값이 확정되지 않았습니다. 해당 값을 명시해 주세요."
    if issue.status == plan_validation.SEMANTIC_CONFLICT:
        return f"서로 모순되는 조건이 함께 요청됐습니다({path}, 코드: {code})."
    if issue.status == plan_validation.UNSUPPORTED or code.endswith("_unsupported"):
        return f"'{path}' 조건의 연산은 아직 지원되지 않습니다(코드: {code})."
    return f"'{path}' 조건을 실행 계획으로 확정하지 못했습니다(검증 코드: {code})."
