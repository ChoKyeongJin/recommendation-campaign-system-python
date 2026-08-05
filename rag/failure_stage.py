"""타겟 SQL 이 "어디서 막혔는가"의 분류 — 실패 사유 → 파이프라인 단계.

graph_rag.py 에서 분리했다. 사용자에게 "SQL 을 못 만들었다"만 말하면 다음 행동을 정할 수
없다. 이 모듈은 실패 사유를 6단계 파이프라인 중 한 지점으로 사상해 api_response.failure_stage
로 내보내고, 프론트가 그것을 스텝퍼로 렌더한다 — 즉 이 매핑은 **UI 계약**이다.

세 축이 여기 모여 있다:
  - 실패 단계 순서(_FAILURE_STAGE_SEQUENCE)와 사유→단계 매핑(_FAILURE_REASON_TO_STAGE)
  - 누락 조건 종류(_MISSING_CONDITION_KINDS) — "무엇이 빠져서" 막혔는지의 어휘
  - 인식된 도메인(_RECOGNIZED_DOMAINS) — 질의가 어느 도메인을 건드렸는지의 진단 표지

순수 모듈 불변식: graph_rag 를 import 하지 않는다. plain dict 입력만 받는다.

이 모듈은 판단하지 않고 **분류만** 한다. 어떤 사유를 낼지는 상위(빌더·검증기)가 정하고,
여기서는 그 사유가 어느 단계에 속하는지만 답한다 — 그래서 새 실패 사유가 생기면
매핑 한 줄만 추가하면 되고, 매핑이 없으면 조용히 넘어가는 대신 드러난다.
"""

from __future__ import annotations

from typing import Any


# 프롬프트에서 어휘로 인식되는 타겟 도메인. SQL 후보가 하나도 안 나왔을 때(no_sql_candidates) 실패를
# 두 가지로 가르는 데 쓴다:
#   (a) 신호 자체가 없음        → "타겟 조건을 넣어 주세요"(기존 안내가 맞다)
#   (b) 도메인은 인식했는데 그 형태를 컴파일할 수단이 없음 → 지원 형태를 구체적으로 안내해야 한다
# (b)를 (a)로 안내하면 사용자는 이미 쓴 조건을 다시 쓰게 된다("장바구니에 10만원" → "장바구니 조건을
# 넣어 주세요"). 파서가 슬롯을 못 채우면 흔적이 안 남아 하위에서 둘을 구별할 수 없었기에, 어휘 인식
# 사실만 플랜에 남긴다(조건을 지어내지 않으므로 SQL 에는 영향이 없다).
_RECOGNIZED_DOMAINS: tuple[dict[str, str], ...] = (
    {
        "id": "cart",
        "lexicon": "cart_terms",
        "label": "장바구니",
        "supported": "담은 상품 개수·수량·금액 임계값, 보관 기간, 브랜드, 미결제 이탈",
    },
)


def _recognized_domains(query_plan: dict[str, Any]) -> list[dict[str, str]]:
    recognized = query_plan.get("recognized_domains")
    if not isinstance(recognized, list):
        return []
    return [domain for domain in _RECOGNIZED_DOMAINS if domain["id"] in recognized]


# 명시적 미지원(unsupported-intent) 사유 — 조건은 인식했으나 실DB SQL 로 컴파일할 수 없는 경우. 이들은
# SQL 안전 검증/의미 검증이 아니라 '실DB 조건 매핑' 단계에서 막힌 것이고, plan['unsupported'] 에 구체적
# 안내(message/clarification)를 이미 담고 있다. 사용자가 재입력(예: 쿠폰 '사용 여부')으로 풀 수 있어
# needs_clarification 으로 다루고, 무관한 일반 조건 라벨('혜택 유형 조건' 등)이 아니라 그 안내를 노출한다.
_UNSUPPORTED_INTENT_REASONS = frozenset({
    "coupon_usage_count_filter_unsupported",
    "coupon_usage_count_ranking_unsupported",
    "coupon_usage_count_metric_comparison_unsupported",
    "derived_metric_filter_unsupported",
    "coupon_semantic_preservation_failed",
    "entity_ranking_not_structured",
})


# 타겟 SQL 실패가 발생할 수 있는 파이프라인 단계(실행 순서대로). failure_reason 은 "왜"(=message)를
# 설명하지만, 사용자는 "어디서" 막혔는지도 한 눈에 알아야 한다(집합식/조건 인식에서 막혔나, SQL 안전
# 검증에서 막혔나). failure_reason 을 아래 단계로 승격해 프론트가 단계 배지·스텝퍼로 그대로 노출한다.
_FAILURE_STAGE_SEQUENCE: tuple[dict[str, str], ...] = (
    {"code": "condition_recognition", "label": "타겟 조건 인식"},
    {"code": "real_db_mapping", "label": "실DB 조건 매핑"},
    {"code": "sql_safety_validation", "label": "SQL 안전 검증"},
    {"code": "aggregation_validation", "label": "집계 요구 검증"},
    {"code": "condition_coverage", "label": "조건 반영 검증"},
    {"code": "intent_scope", "label": "요청 의도 검증"},
    {"code": "semantic_verification", "label": "의미 검증"},
)


# failure_reason → 단계 code. 새 failure_reason 을 추가하면 여기에도 매핑을 넣어야 프론트에 단계가 뜬다.
_FAILURE_REASON_TO_STAGE: dict[str, str] = {
    # 집합식/계산식/의미해석이 확정 안 되면 required_conditions_missing 으로, 그 외 조건 미인식은
    # no_sql_candidates 로 떨어진다 — 둘 다 '조건 인식' 단계다(세부 라벨은 _refine_stage_label 로 가른다).
    "no_sql_candidates": "condition_recognition",
    "recognized_domain_unsupported": "condition_recognition",
    "query_plan_required_conditions_missing": "condition_recognition",
    # 구조화기가 의미 노드를 못 만든 것과, 실행 어휘가 비어 있는 것은 서로 다른 실패다.
    # 둘 다 '조건 인식' 단계지만 사유 코드로 갈려야 운영자가 어디를 고칠지 안다.
    "semantic_structurer_failure": "condition_recognition",
    "semantic_registry_gap": "condition_recognition",
    "semantic_conditions_not_extracted": "condition_recognition",
    "entity_ranking_not_structured": "condition_recognition",
    "real_db_unsupported_conditions": "real_db_mapping",
    "external_condition_resolution_failed": "real_db_mapping",
    # 배포 물리 바인딩 파일이 없거나 불완전한 경우. 조건 의미는 이미 알지만 실DB 좌표를
    # 안전하게 정할 수 없으므로 자유 SQL/구 코드 기본값으로 우회하지 않고 매핑 단계에서 막는다.
    "member_target_filters_unavailable": "real_db_mapping",
    # 명시적 미지원(쿠폰 건수/순위/비교/파생·의미보존 실패): 조건은 인식했으나 실DB 로 매핑 불가 —
    # SQL 안전 검증/의미 검증이 아니라 '실DB 조건 매핑' 단계에서 막힌 것으로 스텝퍼에 정직하게 표시한다.
    "coupon_usage_count_filter_unsupported": "real_db_mapping",
    "coupon_usage_count_ranking_unsupported": "real_db_mapping",
    "coupon_usage_count_metric_comparison_unsupported": "real_db_mapping",
    "derived_metric_filter_unsupported": "real_db_mapping",
    "coupon_semantic_preservation_failed": "real_db_mapping",
    # ``relational_ir_unsupported`` / ``relational_ir_needs_clarification`` 는 2026-08-05
    # 삭제됐다 — 속성 시점·이력 축이 폐기돼 그 사유를 내는 생산자가 없다.
    "sql_guard_failed": "sql_safety_validation",
    "aggregation_validation_failed": "aggregation_validation",
    "intent_sql_contract_failed": "intent_scope",
    "query_plan_conditions_missing": "condition_coverage",
    "semantic_conditions_not_covered": "condition_coverage",
    "semantic_condition_polarity_mismatch": "semantic_verification",
    "critical_conditions_dropped": "semantic_verification",
    "critical_semantic_issue": "semantic_verification",
    "semantic_verification_unavailable": "semantic_verification",
    # ── canonical Event IR 레인 ────────────────────────────────────────────────────
    # 이 레인의 사유는 접두어+상태로 **조립**된다("plan_validation_" + status 등). 조립형은
    # 닫힌 집합 밖이라 매핑에서 통째로 빠져 있었고, 그 결과 프론트는 이 레인의 실패에
    # 단계 배지를 아예 그리지 않았다. 상태가 하나 늘면 사유도 하나 느는 구조라
    # tests/test_failure_stage_totality.py 가 곱집합으로 강제한다.
    #
    # 플랜 검증: 조건을 확정 못 한 것과 능력이 없는 것은 고칠 곳이 다르다.
    "plan_validation_clarification_required": "condition_recognition",
    # 내부 불량(해석 산출물의 결함)도 사용자에게는 '확인 필요'로 안내한다 — 표적 재방출·재시도의
    # 대상이지 능력 부재 선언이 아니다(graph_rag 의 interpretation_status 판정과 같은 결).
    "plan_validation_internal_invalid": "condition_recognition",
    "plan_validation_unsupported": "real_db_mapping",
    "plan_validation_semantic_conflict": "semantic_verification",
    # 의미 IR: 값을 못 확정한 것 ↔ 실행 자산으로 표현 못 하는 것.
    "semantic_ir_needs_clarification": "condition_recognition",
    "semantic_ir_unsupported": "real_db_mapping",
    # Event IR 컴파일러 능력: 조건은 인식했고 의미도 일관되지만 실DB 술어로 못 낸다.
    "event_compiler_unsupported": "real_db_mapping",
    "event_compiler_partially_supported": "real_db_mapping",
    # 오디언스 실행 권위 값이 닫힌 어휘 밖(저장·생산 산출물의 불량). 조건을 읽기 **전에**
    # 막히므로 파이프라인의 첫 단계로 사상한다.
    "audience_authority_invalid": "condition_recognition",
    # 디멘션 IR 자체가 잘못됐거나 서로 충돌한다 — 값 해석이 아니라 조건 인식의 결함이다.
    "invalid_dimension_filters": "condition_recognition",
    "semantic_condition_conflict": "semantic_verification",
    "query_result_grain_mismatch": "intent_scope",
    "targeting_result_member_id_missing": "intent_scope",
    "targeting_result_member_projection_missing": "intent_scope",
    "query_plan_unmentioned_conditions_added": "condition_coverage",
    "intent_scope_mismatch": "intent_scope",
    "semantic_verification_failed": "semantic_verification",
}


# 미확정 required 조건(query_plan_required_conditions_missing)의 종류별 세부 라벨.
# (missing_input_conditions[].path prefix, 단계 세부 라벨, 안내문에 쓸 종류 라벨).
# 같은 '조건 인식' 단계라도 집합식 파싱에서 막혔는지, 계산식/의미 해석에서 막혔는지 구분해 보여준다.
_MISSING_CONDITION_KINDS: tuple[tuple[str, str, str], ...] = (
    ("source_coverage.", "원문 조건 해석", "원문 조건"),
    ("condition_ownership.", "조건 소유권 확정", "조건 소유권"),
    ("aggregation_request.", "집계 요구사항 확정", "집계 요구사항"),
    ("set_expressions.", "집합식 파싱", "집합식"),
    ("computed_metrics.", "계산식 해석", "계산식"),
    ("semantic_resolutions.", "의미 해석 확정", "의미 해석"),
)


def _missing_condition_kind(sql_result: dict[str, Any]) -> tuple[str, str] | None:
    """미확정 required 조건의 종류를 (단계 세부 라벨, 안내 종류 라벨)로 돌려준다(해당 없으면 None).

    집합식/계산식/의미해석이 확정되지 못해 SQL 이 막힌 경우, 어느 것이 원인인지 path prefix 로 판별한다.
    """
    paths = [str(condition.get("path", "")) for condition in sql_result.get("missing_input_conditions", [])]
    for prefix, stage_label, kind_label in _MISSING_CONDITION_KINDS:
        if any(path.startswith(prefix) for path in paths):
            return stage_label, kind_label
    return None


def _refine_stage_label(failure_reason: str, sql_result: dict[str, Any]) -> str | None:
    """같은 단계 안에서 실패 원인을 더 구체적으로 짚는 세부 라벨(없으면 None=기본 라벨).

    현재는 조건 확정 실패(required_conditions_missing)를 집합식/계산식/의미해석으로 세분한다.
    """
    if failure_reason != "query_plan_required_conditions_missing":
        return None
    kind = _missing_condition_kind(sql_result)
    return kind[0] if kind else None


def _classify_failure_stage(
    failure_reason: str | None,
    sql_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """실패 사유(failure_reason)를 파이프라인 단계로 승격한다(어디서 막혔는지 UI 노출용).

    반환: {"code", "label", "order"(1-base), "total", "reason"(원 코드),
           "pipeline"[전체 단계 목록]} — 프론트가 단계 배지·스텝퍼를 데이터 기반으로 그린다.
    sql_result 로 같은 단계 안의 세부 원인(집합식 파싱 등)을 라벨에 반영한다. 매핑이 없으면 None.
    """
    if not failure_reason:
        return None
    stage_code = _FAILURE_REASON_TO_STAGE.get(failure_reason)
    if stage_code is None:
        return None
    # 세부 라벨: 같은 순번(단계) 안에서 무엇이 막혔는지 더 구체적으로 짚어준다(예: '타겟 조건 인식' → '집합식 파싱').
    label_override = _refine_stage_label(failure_reason, sql_result or {})
    pipeline: list[dict[str, Any]] = []
    matched: dict[str, Any] | None = None
    for index, stage in enumerate(_FAILURE_STAGE_SEQUENCE):
        label = label_override if (stage["code"] == stage_code and label_override) else stage["label"]
        entry = {"order": index + 1, "code": stage["code"], "label": label}
        pipeline.append(entry)
        if stage["code"] == stage_code:
            matched = entry
    assert matched is not None  # stage_code 는 항상 시퀀스에 존재
    return {
        "code": matched["code"],
        "label": matched["label"],
        "order": matched["order"],
        "total": len(pipeline),
        "reason": failure_reason,
        "pipeline": pipeline,
    }
