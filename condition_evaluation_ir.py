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

import lexicon_patterns


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
def _alt(vocabulary: str) -> str:
    return "(?:" + "|".join(
        re.escape(term) for term in sorted(lexicon_patterns.vocabulary(vocabulary), key=len, reverse=True)
    ) + ")"


_SAME = _alt("identity_same")
_PRODUCT = _alt("product_noun")
_SIMULTANEOUS = _alt("simultaneity")
_PURCHASE = _alt("purchase_verb")
_MEMBER = _alt("member_noun") + "|" + _alt("member_noun_informal")
_COUNT_RESULT = _alt("count_result_noun")

_SAME_PRODUCT_PATTERNS = (
    re.compile(
        rf"{_SAME}\s*{_PRODUCT}.{{0,16}}?{_SIMULTANEOUS}\s*{_PURCHASE}"
    ),
    re.compile(
        rf"{_SIMULTANEOUS}\s*{_PURCHASE}.{{0,16}}?{_SAME}\s*{_PRODUCT}"
    ),
)
_MEMBER_COUNT_RE = re.compile(rf"(?:{_MEMBER})\s*(?:의\s*)?{_COUNT_RESULT}")


def detects_same_product_co_purchase(query: str) -> bool:
    compact = re.sub(r"\s+", " ", query or "").strip()
    return any(pattern.search(compact) for pattern in _SAME_PRODUCT_PATTERNS)


def requests_member_count(query: str) -> bool:
    return _MEMBER_COUNT_RE.search(query or "") is not None


def build_same_product_co_purchase_evaluation(
    query: str,
    purchase_date: dict[str, Any] | None,
) -> dict[str, Any]:
    """같은 주문에서 같은 상품 수량 합계가 2 이상인 조건의 완전한 IR을 만든다.

    ``동시 구매``의 실행 정의는 검증된 capability에 고정한다: 동일 MEMBER_NO/ORDER_ID/
    PRODUCT_ID 그룹에서 SUM(ORDER_QTY) >= 2. 이 정의를 다른 grain이나 COUNT(라인)로
    축소하지 않는다.
    """

    time_range = None
    if isinstance(purchase_date, dict):
        time_range = {
            "field": "order_date",
            "from": purchase_date.get("from"),
            "to": purchase_date.get("to"),
        }
    return {
        "id": "same_product_co_purchase",
        "capability": SAME_PRODUCT_CAPABILITY,
        "source_text": query,
        "decision_target": {"entity": "member", "key": "member_no"},
        "evaluation_scope": {
            "entity": "purchase_order_detail",
            "time_range": time_range,
        },
        "grouping_unit": {
            "entity": "order_product",
            "keys": ["member_no", "order_id", "product_id"],
        },
        "measure": {
            "entity": "purchase_order_detail",
            "field": "order_quantity",
        },
        "aggregation": {
            "function": "sum",
            "measure": "order_quantity",
        },
        "comparison": {"operator": "gte", "value": 2},
        "condition_result": {
            "entity": "member",
            "key": "member_no",
            "distinct": True,
        },
        "final_result": {
            "unit": "scalar",
            "aggregation": {
                "function": "count_distinct",
                "field": "member_no",
                "alias": "CUSTOMER_COUNT",
            },
        },
        "bindings": {
            "fact_table": "CRM_SL_ORDERDETAILMALL",
            "fact_alias": "D",
            "member_table": "CRM_MB_BASEINFO",
            "member_alias": "B",
            "columns": {
                "member_no": "MEMBER_NO",
                "order_id": "ORDER_ID",
                "product_id": "PRODUCT_ID",
                "order_quantity": "ORDER_QTY",
                "order_date": "ORDER_DATE",
            },
        },
    }


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
