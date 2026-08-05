"""조건 판정과 최종 결과 집계를 분리하는 닫힌 실행 IR.

자연어 조건을 단순 회원 술어로 평탄화하지 않고 다음 일곱 구성요소를 보존한다.

* decision_target: 판정 대상
* evaluation_scope: 판정 범위
* grouping_unit: 조건 계산의 그룹화 단위
* measure: 측정 대상
* aggregation: 조건 계산 집계 방식
* comparison: 비교 기준
* final_result: 최종 결과 단위와 집계

컴파일러는 capability 별로 검증된 구성 서명만 허용한다. 필드가 비었거나 알 수 없는
조합은 기본값으로 보정하지 않고 ValidationIssue 로 반환한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


PLAN_KEY = "condition_evaluations"
SAME_PRODUCT_CAPABILITY = "same_product_same_order_quantity_v1"


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


_DATE_RE = re.compile(r"^\d{8}$")
# 동시구매 어구를 원문 정규식으로 감지하던 문법(_SAME/_SIMULTANEOUS/_MEMBER_COUNT_RE 등)과
# `detects_same_product_co_purchase`/`requests_member_count`/`same_product_co_purchase_source_span`
# 은 2026-08-02 삭제됐다.


# `build_same_product_co_purchase_evaluation`(동시구매 IR 빌더)는 2026-08-05 삭제됐다.
# 유일한 호출자가 SemanticPlanV2 RelationPredicate(co_purchase) 컴파일러였고 그 노드 축이
# 폐기됐다. 동시구매 요청은 오늘도 ingress 에서 missing_argument(audience_expression) 로 막힌다.
# `apply_same_product_co_purchase_backfill`(원문 감지 백필)은 2026-08-02 이미 삭제됐다.
# 이 모듈에 남는 것은 PLAN_KEY·capability 서명 검증기·출력 계약 파생이다.


def scalar_count_output_contract(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """검증 통과한 조건 판정 IR 전부가 스칼라 카운트 출력을 선언하면 그 출력 계약을 돌려준다.

    배경: 출력 계약 생산자(규칙 계층)가 철거된 뒤 expected_grain 기본값이 'member' 라, '고객수'
    질의의 정당한 COUNT 결과가 query_result_grain_mismatch 로 차단됐다(2026-08-01 실사고 —
    semantic_ir 게이트를 열자 이 게이트가 다음 차단자였다). 출력 형태의 단일 소유자는
    capability IR 의 final_result 다 — 거기서 파생한 계약만 결정론으로 인정하고, IR 이 하나라도
    스칼라 카운트가 아니면 계약을 주장하지 않는다(fail-close)."""
    evaluations = query_plan.get(PLAN_KEY)
    if not isinstance(evaluations, list) or not evaluations or validate_evaluations(evaluations):
        return None
    for evaluation in evaluations:
        final = evaluation.get("final_result") if isinstance(evaluation, dict) else None
        if not (isinstance(final, dict) and final.get("unit") == "scalar"):
            return None
        aggregation = final.get("aggregation")
        if not (
            isinstance(aggregation, dict)
            and str(aggregation.get("function", "")).startswith("count")
        ):
            return None
    return {
        "expected_grain": "analytical",
        "requires_member_id": False,
        "source": "condition_evaluations",
    }


# `drop_capability_owned_missing_fields` 는 2026-08-02 삭제됐다 — capability 가 소유한
# 결핍 보고를 semantic_ir 에서 사후에 걷어내던 sweep 이다. 결핍의 소유자가 LLM 이었기
# 때문에 필요했고, 이제 missing_fields 는 semantic_plan 노드 스키마에서 계산되므로
# 걷어낼 stale 이 구조적으로 생기지 않는다.

def _value(node: Any, *path: str) -> Any:
    current = node
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _require(evaluation: dict[str, Any], path: str, issues: list[ValidationIssue]) -> Any:
    value = _value(evaluation, *path.split("."))
    if value in (None, "", [], {}):
        issues.append(ValidationIssue(path, "required_component_missing", f"필수 의미 구성요소 '{path}'가 없습니다."))
    return value


def validate_evaluation(evaluation: Any) -> list[ValidationIssue]:
    """IR 완전성과 capability 조합을 모두 검증한다(알 수 없는 조합은 fail-close)."""

    if not isinstance(evaluation, dict):
        return [ValidationIssue("condition_evaluation", "invalid_ir", "조건 판정 IR은 객체여야 합니다.")]

    issues: list[ValidationIssue] = []
    required_paths = (
        "id",
        "capability",
        "source_text",
        "decision_target.entity",
        "decision_target.key",
        "evaluation_scope.entity",
        "grouping_unit.entity",
        "grouping_unit.keys",
        "measure.entity",
        "measure.field",
        "aggregation.function",
        "aggregation.measure",
        "comparison.operator",
        "comparison.value",
        "condition_result.entity",
        "condition_result.key",
        "final_result.unit",
        "final_result.aggregation.function",
        "final_result.aggregation.field",
        "final_result.aggregation.alias",
        "bindings.fact_table",
        "bindings.fact_alias",
        "bindings.member_table",
        "bindings.member_alias",
        "bindings.columns",
    )
    for path in required_paths:
        _require(evaluation, path, issues)
    if issues:
        return issues

    capability = evaluation.get("capability")
    if capability != SAME_PRODUCT_CAPABILITY:
        return [ValidationIssue(
            "capability", "unsupported_capability",
            f"검증되지 않은 조건 판정 capability입니다: {capability}",
        )]

    expected = {
        "decision_target.entity": "member",
        "decision_target.key": "member_no",
        "evaluation_scope.entity": "purchase_order_detail",
        "grouping_unit.entity": "order_product",
        "measure.entity": "purchase_order_detail",
        "measure.field": "order_quantity",
        "aggregation.function": "sum",
        "aggregation.measure": "order_quantity",
        "comparison.operator": "gte",
        "comparison.value": 2,
        "condition_result.entity": "member",
        "condition_result.key": "member_no",
        "condition_result.distinct": True,
        "final_result.unit": "scalar",
        "final_result.aggregation.function": "count_distinct",
        "final_result.aggregation.field": "member_no",
        "final_result.aggregation.alias": "CUSTOMER_COUNT",
        "bindings.fact_table": "CRM_SL_ORDERDETAILMALL",
        "bindings.fact_alias": "D",
        "bindings.member_table": "CRM_MB_BASEINFO",
        "bindings.member_alias": "B",
    }
    for path, wanted in expected.items():
        actual = _value(evaluation, *path.split("."))
        if actual != wanted:
            issues.append(ValidationIssue(
                path,
                "unsupported_component_combination",
                f"지원 capability가 보장하는 값은 {wanted!r}이지만 {actual!r}이 지정됐습니다.",
            ))

    group_keys = _value(evaluation, "grouping_unit", "keys")
    if group_keys != ["member_no", "order_id", "product_id"]:
        issues.append(ValidationIssue(
            "grouping_unit.keys", "unsupported_grouping_unit",
            "동일 상품 동시 구매는 member_no/order_id/product_id 그룹만 지원합니다.",
        ))

    expected_columns = {
        "member_no": "MEMBER_NO",
        "order_id": "ORDER_ID",
        "product_id": "PRODUCT_ID",
        "order_quantity": "ORDER_QTY",
        "order_date": "ORDER_DATE",
    }
    if _value(evaluation, "bindings", "columns") != expected_columns:
        issues.append(ValidationIssue(
            "bindings.columns", "unsupported_physical_binding",
            "검증된 주문상세 컬럼 바인딩과 일치하지 않습니다.",
        ))

    time_range = _value(evaluation, "evaluation_scope", "time_range")
    if time_range is not None:
        if not isinstance(time_range, dict):
            issues.append(ValidationIssue("evaluation_scope.time_range", "invalid_time_range", "기간 범위는 객체여야 합니다."))
        else:
            if time_range.get("field") != "order_date":
                issues.append(ValidationIssue(
                    "evaluation_scope.time_range.field", "unsupported_scope_field",
                    "이 capability의 기간은 order_date에만 적용할 수 있습니다.",
                ))
            for name in ("from", "to"):
                date = time_range.get(name)
                if not isinstance(date, str) or _DATE_RE.fullmatch(date) is None:
                    issues.append(ValidationIssue(
                        f"evaluation_scope.time_range.{name}", "invalid_time_range",
                        f"{name}은 YYYYMMDD 형식이어야 합니다.",
                    ))
            if (
                isinstance(time_range.get("from"), str)
                and isinstance(time_range.get("to"), str)
                and time_range["from"] > time_range["to"]
            ):
                issues.append(ValidationIssue(
                    "evaluation_scope.time_range", "invalid_time_range",
                    "기간 시작일이 종료일보다 늦습니다.",
                ))
    return issues


def validate_evaluations(evaluations: Any) -> list[ValidationIssue]:
    if not isinstance(evaluations, list) or not evaluations:
        return [ValidationIssue(PLAN_KEY, "required_component_missing", "조건 판정 IR 목록이 비어 있습니다.")]
    issues: list[ValidationIssue] = []
    for index, evaluation in enumerate(evaluations):
        for issue in validate_evaluation(evaluation):
            issues.append(ValidationIssue(f"{PLAN_KEY}[{index}].{issue.path}", issue.code, issue.message))
    if len(evaluations) != 1:
        issues.append(ValidationIssue(
            PLAN_KEY, "unsupported_condition_combination",
            "여러 조건 판정 IR의 AND/OR 조합은 아직 검증되지 않았습니다.",
        ))
    return issues


def compile_evaluation(
    evaluation: dict[str, Any],
    *,
    member_predicates: Iterable[str] = (),
) -> tuple[str | None, list[ValidationIssue]]:
    """검증된 구성만 2단계 SQL로 컴파일한다."""

    issues = validate_evaluation(evaluation)
    if issues:
        return None, issues

    bindings = evaluation["bindings"]
    columns = bindings["columns"]
    fact_alias = bindings["fact_alias"]
    member_alias = bindings["member_alias"]
    member_column = columns["member_no"]
    order_column = columns["order_id"]
    product_column = columns["product_id"]
    quantity_column = columns["order_quantity"]
    date_column = columns["order_date"]

    where = [f"{fact_alias}.{member_column} IS NOT NULL"]
    time_range = evaluation["evaluation_scope"].get("time_range")
    if isinstance(time_range, dict):
        where.append(
            f"{fact_alias}.{date_column} BETWEEN '{time_range['from']}' AND '{time_range['to']}'"
        )
    final_alias = evaluation["final_result"]["aggregation"]["alias"]
    policy = [str(predicate) for predicate in member_predicates if str(predicate).strip()]

    lines = [
        "WITH CONDITION_GROUPS AS (",
        f"    SELECT {fact_alias}.{member_column}, {fact_alias}.{order_column}, {fact_alias}.{product_column}",
        f"    FROM {bindings['fact_table']} {fact_alias}",
        "    WHERE " + "\n      AND ".join(where),
        f"    GROUP BY {fact_alias}.{member_column}, {fact_alias}.{order_column}, {fact_alias}.{product_column}",
        f"    HAVING SUM({fact_alias}.{quantity_column}) >= 2",
        "),",
        "QUALIFIED_MEMBERS AS (",
        f"    SELECT DISTINCT {member_column}",
        "    FROM CONDITION_GROUPS",
        ")",
        f"SELECT COUNT(DISTINCT M.{member_column}) AS {final_alias}",
        "FROM QUALIFIED_MEMBERS M",
        f"     INNER JOIN {bindings['member_table']} {member_alias}",
        f"       ON M.{member_column} = {member_alias}.{member_column}",
    ]
    if policy:
        lines.append("WHERE " + "\n  AND ".join(policy))
    return "\n".join(lines), []


def validate_compiled_sql(evaluation: dict[str, Any], sql: str) -> list[ValidationIssue]:
    """생성 결과가 IR의 두 grain을 실제 SQL 구조로 보존하는지 확인한다."""

    issues = validate_evaluation(evaluation)
    if issues:
        return issues
    normalized = re.sub(r"\s+", " ", sql or "").strip().upper()
    required = {
        "condition_stage": "WITH CONDITION_GROUPS AS",
        "condition_result_stage": "QUALIFIED_MEMBERS AS",
        "fact_scope": "FROM CRM_SL_ORDERDETAILMALL D",
        "grouping_unit": "GROUP BY D.MEMBER_NO, D.ORDER_ID, D.PRODUCT_ID",
        "condition_aggregation": "HAVING SUM(D.ORDER_QTY) >= 2",
        "condition_result_dedup": "SELECT DISTINCT MEMBER_NO FROM CONDITION_GROUPS",
        "final_result": "COUNT(DISTINCT M.MEMBER_NO)",
    }
    for component, fragment in required.items():
        if fragment not in normalized:
            issues.append(ValidationIssue(
                component, "compiled_semantics_not_guaranteed",
                f"SQL이 IR 구성요소를 보장하지 않습니다: {fragment}",
            ))
    time_range = evaluation["evaluation_scope"].get("time_range")
    if isinstance(time_range, dict):
        fragment = f"D.ORDER_DATE BETWEEN '{time_range['from']}' AND '{time_range['to']}'".upper()
        if fragment not in normalized:
            issues.append(ValidationIssue(
                "evaluation_scope.time_range", "compiled_semantics_not_guaranteed",
                "SQL 조건 판정 범위에 IR의 기간이 반영되지 않았습니다.",
            ))
    return issues
