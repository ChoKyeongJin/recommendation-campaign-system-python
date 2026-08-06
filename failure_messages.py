"""실패 응답의 사용자 표시용 한국어 렌더링.

plan_validation 은 의도적으로 메시지를 만들지 않고(렌더링은 나중 경계 소관),
semantic_ir 의 missing_fields 는 모델 내부 필드명(영문)이다. 응답 조립이 사용자에게
"어떤 조건이·어디서·왜" 막혔는지 말할 수 있도록, 코드/필드명 → 한국어 문구 낮춤을
이 모듈이 단독 소유한다. 문구가 없으면 internal_invalid 가 사유 없는 '미지원'으로,
미확정 필드가 범용 문구('필수 비교 조건을 확인해 주세요')로 뭉개진다 —
26종 프롬프트 감사(2026-08-02)에서 확인된 실패 양식이다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import audience_admission
import plan_validation

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


# 파생 결핍의 접두어별 렌더링. 파생 결핍은 `<node_id>.<field>` 또는
# `uncovered:<원문 구절>` / `conflict:<원문 구절>` 형태다 — 앞의 둘은 필드 라벨로, 뒤의 둘은
# 원문 구절 자체로 말해야 사용자가 무엇을 다시 적어야 할지 안다.
_SPAN_PREFIXES = ("uncovered:", "conflict:", "invalid:")


def semantic_failure_reason(
    status: str, failure_kind: Any, declared_reason: Any = None
) -> str:
    """failure_reason 도 원인을 말한다 — 운영자가 로그만 보고 어느 계층인지 알 수 있어야 한다.

    `semantic_ir_needs_clarification` 하나로 뭉치면 "사용자가 안 알려준 것", "구조화기가 못
    만든 것", "실행 설정이 비어 있는 것"이 같은 코드로 보인다 — 셋의 고칠 곳이 다 다르다.

    kind 로 갈리지 않는 실패는 판정한 계층이 ``semantic_ir.failure_reason`` 에 **명시**한다.
    system_failure 안에는 성격이 다른 둘이 들어 있다 — 실행 자산이 없는 것(레지스트리 구멍)과,
    자산도 컴파일러도 있는데 표현이 방출되지 않은 것. 파생만 쓰면 후자가 전자로 보고된다.
    """
    import semantic_outcome  # 지연 import — 렌더링 계층은 코어 스키마에 의존하지 않는다

    if isinstance(declared_reason, str) and declared_reason.strip():
        return declared_reason
    if failure_kind == semantic_outcome.FAILURE_KIND_STRUCTURER:
        return "semantic_structurer_failure"
    if failure_kind == semantic_outcome.FAILURE_KIND_SYSTEM:
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


def partial_sql_message(missing_kinds: Any, questions: Any) -> str:
    """일부 의미가 빠진 SQL 을 막았을 때의 안내.

    "조건을 다시 써 보라"만으로는 사용자가 무엇을 고쳐야 할지 모른다 — **어떤 종류의 의미**가
    빠졌는지까지 말한다. 내부 코드는 진단 필드(coverage_gate)에 그대로 남는다.
    """
    labels = {
        "result_shape": "결과 형태",
        "temporal_window": "기간",
        "source_condition": "조건",
    }
    named = ", ".join(
        labels.get(str(kind), str(kind)) for kind in (missing_kinds or ()) if kind
    )
    lead = (
        f"생성된 SQL 에 요청하신 {named} 의미가 반영되지 않아 출고를 막았습니다."
        if named
        else "생성된 SQL 에 요청 의미 일부가 반영되지 않아 출고를 막았습니다."
    )
    listed = " / ".join(str(item) for item in (questions or ()) if item)
    return lead + (f" {listed}" if listed else "")


def compile_outcome_message(blocking: Any, unresolved: Any) -> str:
    """조립형 사유(``<단계>:<코드>``)의 사용자 안내.

    사용자에게는 코드가 아니라 **어느 의미를 못 다뤘는지**를 말한다.
    """
    blocking = blocking if isinstance(blocking, dict) else {}
    honest = (
        "요청한 조건 중 일부를 현재 실행 자산으로 표현하지 못했습니다"
        if blocking.get("status") == "explicit_unsupported"
        else "요청한 조건을 실DB 술어로 컴파일하지 못했습니다"
    )
    detail = next(
        (
            str(item.get("reason"))
            for item in unresolved or ()
            if isinstance(item, dict) and item.get("code") == blocking.get("code")
        ),
        "",
    )
    return honest + (f": {detail}" if detail else ".")


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


# `requirement_failure_report` 는 2026-08-05 삭제됐다 — 요구사항 원장(`requirement_ledger`)은
# SemanticPlanV2 파이프라인의 산출물이었고 생산자가 폐기되면서 렌더링할 입력이 사라졌다.
# 응답 계약(`requirement_report`)은 graph_rag 가 빈 값으로 유지한다(호출자 분기는 그대로다).


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
    if code == audience_admission.LEGACY_AUDIENCE_CONFLICT_CODE:
        # 이 코드의 path 는 **내부 실행 슬롯**(`target_user.<슬롯>`)이다. 아래 기본 분기는 path 를
        # 문장에 그대로 넣으므로, 그대로 두면 이 저장소가 계약으로 갖는 "내부 필드명 미노출"이
        # 조용히 깨진다(docs/plans_event_ir_only.md §6-6). 좌표는 운영자용 채널
        # (audience_diagnosis / unresolved_source_conditions)에 이미 남으므로 여기서는 뺀다.
        return (
            "요청하신 조건이 서로 다른 두 방식으로 해석돼 실행 계획을 확정하지 못했습니다"
            f"(코드: {code}). 조건을 나눠서 다시 요청해 주세요."
        )
    if issue.status == plan_validation.SEMANTIC_CONFLICT:
        return f"서로 모순되는 조건이 함께 요청됐습니다({path}, 코드: {code})."
    if issue.status == plan_validation.UNSUPPORTED or code.endswith("_unsupported"):
        return f"'{path}' 조건의 연산은 아직 지원되지 않습니다(코드: {code})."
    return f"'{path}' 조건을 실행 계획으로 확정하지 못했습니다(검증 코드: {code})."


# ── 관문 거부 근거 보존 ────────────────────────────────────────────────────────
# 실측(2026-08-03, campaign_query_failure_logs): 후보가 있는데 거부된 실패 행은
# `generated_sql`·`error_detail` 두 **이름 있는 컬럼**이 전부 NULL 이었다. 근거 자체는
# 사라지지 않았다 — `selected_candidate` JSONB 안에만 있어서 운영 조회로는 보이지 않았다.
# 그래서 여기서 하는 일은 새 판정이 아니라 **투영**이다.
#
# 어느 하위 보고를 읽을지 이름으로 들지 않는다. 관문이 늘 때마다 목록을 고쳐야 하면
# 새 관문의 거부는 다시 조용해진다. 하위 보고의 모양은 이미 닫혀 있다 —
# 만족 여부 불리언 + 이름 있는 이슈 목록 — 그 **구조**로 수집한다.
_REJECTION_VERDICT_KEYS = ("is_satisfied", "is_valid", "valid", "ok", "faithful")
_REJECTION_ISSUE_KEYS = ("issues", "errors", "missing_conditions")
_REJECTION_DETAIL_MAX = 4000
# 후보 **선택 뒤에** 도는 관문들. 이들의 거부 사유는 후보 안이 아니라 결과 최상위에 있다
# (실측: semantic_verification_failed 는 SQL 은 남는데 사유 컬럼이 비었다).
_POST_SELECTION_GATES = (
    "semantic_verification", "semantic_invariants", "delivery_validation",
    "aggregation_validation", "condition_evaluation_validation", "intent_sql_contract",
    "metric_profile_validation", "semantic_validation_v2",
)


def _rejection_issue_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, Mapping):
        return ""
    code = str(item.get("code") or item.get("reason_code") or item.get("path") or "").strip()
    message = str(item.get("message") or item.get("detail") or item.get("value") or "").strip()
    return " ".join(part for part in (code, message) if part)


def rejected_candidate_reasons(candidate: Any) -> list[str]:
    """거부된 후보의 사유를 후보 자신의 하위 검증 보고에서 구조로 수집한다."""
    if not isinstance(candidate, Mapping):
        return []
    reasons: list[str] = []
    for name, report in candidate.items():
        if not isinstance(report, Mapping):
            continue
        verdicts = [report.get(key) for key in _REJECTION_VERDICT_KEYS if key in report]
        if not verdicts or all(verdict is not False for verdict in verdicts):
            continue
        rendered = [
            f"{name}.{issue_key}: {text}"
            for issue_key in _REJECTION_ISSUE_KEYS
            for item in (report.get(issue_key) or ())
            if (text := _rejection_issue_text(item))
        ]
        # 보고가 불만족을 선언했는데 이슈 항목이 없으면 그 사실 자체가 사유다 —
        # 침묵보다 "어느 관문이 막았는지"가 언제나 낫다.
        reasons.extend(rendered or [f"{name}: 불만족(사유 항목 없음)"])
    return list(dict.fromkeys(reasons))


def rejected_candidate_evidence(sql_result: Any) -> dict[str, Any]:
    """실패 로그의 이름 있는 컬럼에 실을 (거부된 SQL, 거부 사유, 후보 수).

    출고에 성공한 결과에는 아무것도 돌려주지 않는다 — 이것은 실패 경로의 투영이다.
    """
    empty: dict[str, Any] = {"sql": None, "detail": None, "candidate_count": 0, "reasons": []}
    if not isinstance(sql_result, Mapping) or sql_result.get("is_success") is True:
        return empty
    candidates = sql_result.get("candidates")
    count = sql_result.get("candidate_count")
    if not isinstance(count, int):
        count = len(candidates) if isinstance(candidates, (list, tuple)) else 0
    candidate = sql_result.get("selected")
    sql = sql_result.get("blocked_sql")
    if not sql and isinstance(candidate, Mapping):
        sql = candidate.get("sql")
    reasons = rejected_candidate_reasons(candidate)
    if candidate is not None or sql:
        # 후보가 실제로 만들어졌다가 거부된 경우에만 최상위 관문을 읽는다. 결핍으로 후보가
        # 아예 없었던 요청까지 훑으면 '거부 근거' 채널에 결핍 사유가 섞인다.
        reasons.extend(
            reason for reason in rejected_candidate_reasons(
                {name: sql_result.get(name) for name in _POST_SELECTION_GATES}
            ) if reason not in reasons
        )
    detail = "; ".join(reasons)[:_REJECTION_DETAIL_MAX] or None
    return {
        "sql": sql if isinstance(sql, str) and sql.strip() else None,
        "detail": detail,
        "candidate_count": count,
        "reasons": reasons,
    }
