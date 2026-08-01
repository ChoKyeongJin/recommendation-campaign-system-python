"""미지원 사유 식별자의 닫힌 집합.

배경: `{"reason": "..."}` 로 쓰이는 사유 문자열이 graph_rag 곳곳에 리터럴로 흩어져 있고, 그중 일부는
다른 모듈이 **문자열로 되받아** 동작을 바꾼다 — targeting_ir 의 SlotShape 가 특정 사유를 "해소됨"으로
선언하고, 쿠폰 오버라이드 목록이 별도 상수로 같은 문자열을 재나열한다. 즉 같은 사실이 3곳에
손배선돼 있어서, 사유 이름을 하나 바꾸면 게이트가 조용히 동작을 멈춘다(예외도 경고도 없이).

이 모듈은 그 이름들의 단일 소유자다. 소비자는 리터럴 대신 여기 상수를 참조하고, 계약 테스트가
"소스가 실제로 내는 사유 ⊆ 여기 선언" 과 "소비자 집합 ⊆ 여기 선언" 을 함께 강제한다.

문장형 사유(사람이 읽는 메시지)는 대상이 아니다 — 식별자로 분기하는 것만 닫는다.
"""

from __future__ import annotations

# 소스가 실제로 생산하는 사유 식별자(2026-08-01 기준). 규칙 기반 해석 계층을 철거하면서 그 계층만
# 내던 사유 24종은 함께 지웠다 — 아무도 내지 않는 선언은 게이트가 있다는 착시만 만든다.
ALL: frozenset[str] = frozenset({
    "analytical_sql_compilation_failed",
    "coupon_semantic_preservation_failed",
    "external_common_sense_contract_invalid",
    "external_condition_filter_missing",
    "external_condition_id_not_unique",
    "external_condition_invalid",
    "external_condition_result_mismatch",
    "metric_aggregation_mismatch",
    "metric_trend_metric_unsupported",
    "metric_trend_multi_product_scope_unsupported",
    "metric_trend_relative_change_invalid",
    "missing_openai_api_key",
    "unresolved_aggregate_column",
})


def is_known(reason: str) -> bool:
    """선언된 사유인가. 미선언 사유는 게이트가 못 알아보고 조용히 지나친다."""
    return reason in ALL


__all__ = ["ALL", "is_known"]
