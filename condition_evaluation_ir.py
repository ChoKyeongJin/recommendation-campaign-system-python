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


def _same_product_co_purchase_matches(query: str) -> list[re.Match[str]]:
    source = query or ""
    return [
        match
        for pattern in _SAME_PRODUCT_PATTERNS
        for match in pattern.finditer(source)
    ]


def detects_same_product_co_purchase(query: str) -> bool:
    return bool(_same_product_co_purchase_matches(query))


def requests_member_count(query: str) -> bool:
    return _MEMBER_COUNT_RE.search(query or "") is not None


def same_product_co_purchase_source_span(query: str) -> tuple[int, int] | None:
    """Return the exact clause owned by the registered co-purchase capability.

    Detection and provenance deliberately share the same grammar.  The span
    starts at the identity/product phrase and ends at the member-count result;
    calendar qualifiers and unrelated neighboring clauses remain available to
    their own condition owners.
    """

    candidates: list[tuple[int, int]] = []
    for condition_match in _same_product_co_purchase_matches(query):
        count_match = _MEMBER_COUNT_RE.search(query, condition_match.end())
        if count_match is not None:
            candidates.append((condition_match.start(), count_match.end()))
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else None


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
    evaluation = {
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
    source_span = same_product_co_purchase_source_span(query)
    if source_span is not None:
        evaluation["source_span"] = {
            "start": source_span[0],
            "end": source_span[1],
        }
    return evaluation


def apply_same_product_co_purchase_backfill(query_plan: dict[str, Any], source_query: str) -> None:
    """'같은 상품 동시 구매 고객수'의 조건 판정 IR 결정론 생산자(규칙 계층 삭제로 소실된 배선 복원).

    condition_evaluations 는 application-owned plan 필드라 LLM 이 만들 수 없고, 생산자가 없으면
    원문 감지가 무조건 fail-close(확인 요청)로 끝난다. 카운트 출력이 확정되고 조건 절 소유 스팬이
    유일할 때만 검증된 capability IR 을 채운다 — 그 외(리스트 출력 요청, 다중 스팬)는 fail-close 를
    유지한다. 판정 기간은 purchase_date 슬롯의 절대창을 물려받고, 생성 IR 은 기존 검증 체인
    (validate_evaluations → 전용 2단계 컴파일러 잠금)을 그대로 탄다."""
    if query_plan.get(PLAN_KEY):
        return
    if not (
        detects_same_product_co_purchase(source_query)
        and requests_member_count(source_query)
        and same_product_co_purchase_source_span(source_query) is not None
    ):
        return
    target_user = query_plan.get("target_user") if isinstance(query_plan.get("target_user"), dict) else {}
    purchase_date = target_user.get("purchase_date")
    query_plan[PLAN_KEY] = [
        build_same_product_co_purchase_evaluation(
            source_query, purchase_date if isinstance(purchase_date, dict) else None
        )
    ]


# semantic_ir 게이트가 결정론 소유를 인정할 missing_fields 어휘(축별). LLM 의 missing_fields 는
# 자유 문자열이라(닫힌 스키마에 enum 없음) 정규화 비교로 잡는다. 상품 축은 '같은 상품'이 특정
# 상품명이 아니라 관계 조건임을 capability 가 보증한다. 기간 축은 **IR 이 판정창을 실제로
# 물려받았을 때만** 소유한다 — time_range 없는 IR 에서 기간 결핍을 걷으면 '기간이 언급됐으나
# 파싱 실패'(진짜 결핍)와 '기간 미언급'(제한 없음)을 구분하지 못해 조용한 lifetime 오답이 된다.
# 브랜드/카테고리 등 다른 축은 이 capability 소유가 아니다 — 넓히면 진짜 결핍을 삼킨다(fail-close).
_OWNED_PRODUCT_FIELD_TOKENS = frozenset({
    "condition_evaluations",
    "purchase_object", "purchase_objects", "purchase_product", "product", "product_name", "상품",
})
_OWNED_PERIOD_FIELD_TOKENS = frozenset({
    "purchase_date", "purchase_dates", "purchase_period", "purchase_window",
    "order_date", "order_period", "구매기간", "구매일",
})

_MISSING_FIELD_NORMALIZE_RE = re.compile(r"[^0-9a-z가-힣]+")
# 동시구매 어구 밖의 다른 구매 절/엔터티 표지 — 있으면 이 플랜의 결핍이 다른 절 소유일 수 있어
# sweep 전체를 포기한다(fail-close). 표지 낱말은 부분 문자열 검색이라 '상품명/브랜드명'도 잡힌다.
_OUTSIDE_PURCHASE_RE = re.compile(_PURCHASE)
_OUTSIDE_ENTITY_MARKERS = ("브랜드", "상품", "제품", "품목", "카테고리")


def _normalize_missing_field(field: str) -> str:
    lowered = _MISSING_FIELD_NORMALIZE_RE.sub("_", field.casefold()).strip("_")
    for prefix in ("target_user_", "user_", "customer_"):
        if lowered.startswith(prefix):
            return lowered[len(prefix):]
    return lowered


def _owned_token(field: str, tokens: frozenset[str]) -> bool:
    normalized = _normalize_missing_field(field)
    # '구매 기간'→'구매_기간' 같은 공백 표기 변형도 흡수한다(밑줄 제거 후 재비교).
    return normalized in tokens or normalized.replace("_", "") in {t.replace("_", "") for t in tokens}


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    masked = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(masked), end)):
            masked[index] = " "
    return "".join(masked)


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


def drop_capability_owned_missing_fields(query_plan: dict[str, Any], source_query: str) -> list[str]:
    """검증 통과한 조건 판정 IR 이 **자기 절에 귀속되는** 결핍 보고를 걷어낸다.

    LLM 은 '같은 상품'을 확정하지 못한 purchase_object 결핍으로, 판정 창을 purchase_date 요구로
    보고하곤 한다. 동시구매 capability 는 상품 축(관계 조건 — 특정 상품명 불요)과, 판정창을
    실제로 물려받았을 때의 기간 축을 스스로 소유하므로 그 요구는 사용자에게 물을 결핍이 아니다.
    이 sweep 이 없으면 전용 capability 가 있어도 semantic_ir 게이트가 먼저 차단해 영원히 도달
    불가다(2026-08-01 '같은 상품 동시 구매 고객수' 실사고).

    귀속 가드(전부 통과해야 걷는다 — 하나라도 걸리면 sweep 전체 포기, fail-close):
      1. 원문에서 동시구매 어구가 실제로 감지돼야 한다(감지 문법과 소유 문법의 공유).
      2. 어구 **밖**에 다른 구매 동사·엔터티 표지가 있으면 걷지 않는다 — 그 결핍은 다른 절
         ('행사 상품을 구매했고 …', '… 지난 시즌에 구매한 …')의 것일 수 있다.
      3. 기간 축은 모든 IR 이 time_range 를 실제 보유할 때만 소유한다(위 어휘 주석 참고).
    한계: 구매 동사·표지 없이 인접한 맨 고유명사('신라면과 같은 상품…')는 결정론으로 귀속을
    가릴 수 없어 남는 잔여 위험이다 — 그 판정은 9단계 LLM 의미검증이 담당한다.

    같은 LLM 결핍 신호의 두 번째 채널(unresolved_source_conditions 의 source=llm_semantic_ir
    행)도 같은 귀속 규칙으로 걷는다 — missing_fields 만 걷으면 그 채널이 그대로 차단해 수리가
    무효가 된다. 걷어낸 항목은 plan_decisions 에 CLAIM 으로 기록한다(소유권 이동 감사 —
    behavior_demotion 의 claim_slot 관례와 같은 이유: 사라진 확인 질문을 사후 추적 가능하게).

    걷어낸 뒤 missing_fields 가 비고 unsupported_operations 도 없으면 status 를 resolved 로
    되돌린다 — needs_clarification 에 빈 missing_fields 를 남기면 validate_semantic_ir 의 닫힌
    스키마가 internal_invalid 로 판정한다. 소유 주장은 IR 이 검증을 통과할 때만 한다.
    반환: 걷어낸 missing_fields 필드명(진단용)."""
    import plan_decisions

    semantic_ir = query_plan.get("semantic_ir")
    if not isinstance(semantic_ir, dict) or semantic_ir.get("status") != "needs_clarification":
        return []
    evaluations = query_plan.get(PLAN_KEY)
    if not isinstance(evaluations, list) or not evaluations or validate_evaluations(evaluations):
        return []
    matches = _same_product_co_purchase_matches(source_query or "")
    if not matches:
        return []
    outside = _mask_spans(source_query, [(m.start(), m.end()) for m in matches])
    if _OUTSIDE_PURCHASE_RE.search(outside) or any(marker in outside for marker in _OUTSIDE_ENTITY_MARKERS):
        return []
    period_owned = all(
        isinstance(evaluation, dict)
        and isinstance(_value(evaluation, "evaluation_scope", "time_range"), dict)
        for evaluation in evaluations
    )

    def owned(field: str) -> bool:
        if _owned_token(field, _OWNED_PRODUCT_FIELD_TOKENS):
            return True
        return period_owned and _owned_token(field, _OWNED_PERIOD_FIELD_TOKENS)

    matched_texts = [source_query[m.start():m.end()] for m in matches]

    dropped: list[str] = []
    missing = semantic_ir.get("missing_fields")
    if isinstance(missing, list) and missing:
        dropped = [field for field in missing if isinstance(field, str) and owned(field)]
        if dropped:
            semantic_ir["missing_fields"] = [field for field in missing if field not in dropped]
            if not semantic_ir["missing_fields"] and not semantic_ir.get("unsupported_operations"):
                semantic_ir["status"] = "resolved"

    unresolved = query_plan.get("unresolved_source_conditions")
    dropped_rows: list[str] = []
    if isinstance(unresolved, list) and unresolved:
        kept_rows = []
        for row in unresolved:
            if isinstance(row, dict) and row.get("source") == "llm_semantic_ir":
                path_leaf = str(row.get("path") or "").rpartition(".")[2]
                condition_text = str(row.get("condition") or "")
                row_owned = (bool(path_leaf) and owned(path_leaf)) or any(
                    text and (text in condition_text or condition_text in text)
                    for text in matched_texts
                )
                if row_owned:
                    dropped_rows.append(condition_text or path_leaf)
                    continue
            kept_rows.append(row)
        if dropped_rows:
            query_plan["unresolved_source_conditions"] = kept_rows

    for label in [*dropped, *dropped_rows]:
        plan_decisions.record(
            query_plan,
            filter_name=f"condition_evaluations:{SAME_PRODUCT_CAPABILITY}",
            action=plan_decisions.CLAIM,
            slot=f"semantic_ir.missing_fields:{label}",
            reason="capability 소유 결핍 회수(상품=관계 조건, 기간=IR 판정창 보유)",
            value=label,
            evidence=matched_texts[0] if matched_texts else None,
        )
    return dropped


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
