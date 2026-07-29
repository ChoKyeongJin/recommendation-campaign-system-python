"""query_plan(파서 공통 Raw Plan) → 의미 AST 투영과 SQL 역검증(순수 모듈).

``rules`` 파서와 ``llm`` 파서는 이미 **같은 query_plan 스키마**를 만든다(LLM 후보는
``_coerce_llm_query_plan_candidate`` 로 같은 슬롯에 정규화된 뒤 같은 결정론 필터·소유권 재조정을 탄다).
따라서 이 모듈이 그 plan 하나만 읽으면 두 경로가 자동으로 같은 검증을 통과한다 — 파서별 분기가 없다.

  query_plan ──▶ plan_to_semantic_expr ──▶ to_nnf ──▶ verify_expression ──▶ (SQL) ──▶ verify_compiled_sql

투영 규칙(§의미 보존 불변식)
  * ``dimension_filters`` 의 ``NOT_IN`` 은 ``Not(Predicate(in))`` 으로 — 극성을 필드가 아니라 노드로 옮긴다.
  * ``exclude.*`` 슬롯도 마찬가지로 ``Not`` 노드가 된다.
  * ``set_expressions`` 의 합집합/교집합/차집합은 ``Or``/``And``/``And(.., Not(..))`` 로 구조를 보존한다.
  * ``unknown_operand`` 는 ``Unknown`` 으로 남긴다 — 어떤 단계도 조용히 지우지 않는다.

graph_rag 를 import 하지 않는다(순수 in/out). 어휘(값→dimension)는 호출자가 주입한다.
실행: python -m pytest tests/test_plan_semantic_ast.py -q
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from semantic_ast import (
    And,
    Not,
    Or,
    Predicate,
    Range,
    SourceSpan,
    Unknown,
    iter_nodes,
    VerificationIssue,
    VerificationResult,
    canonicalize_value,
    describe,
    detect_set_conflict,
    has_or,
    scope_key,
    serialize_canonical_value,
    signed_predicates,
    simplify,
    to_dict,
    to_nnf,
    verify_expression,
)

DEFAULT_OWNER = "member"

# 회원 속성 슬롯 → 의미 AST dimension. 슬롯이 늘면 이 표에 한 줄 추가한다.
_LIST_ATTRIBUTE_SLOTS: tuple[tuple[str, str], ...] = (
    ("lifecycle", "lifecycle"),
    ("interests", "interests"),
    ("preferred_channels", "preferred_channels"),
    ("behaviors", "behaviors"),
)


def _span(payload: Any, text: str = "") -> SourceSpan | None:
    if isinstance(payload, Mapping):
        start, end = payload.get("start"), payload.get("end")
        if isinstance(start, int) and isinstance(end, int):
            return SourceSpan(start, end, str(payload.get("text") or text))
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)) and len(payload) == 2:
        start, end = payload
        if isinstance(start, int) and isinstance(end, int):
            return SourceSpan(start, end, text)
    return None


def _short_column(column: Any) -> str:
    return str(column or "").split(".")[-1].strip().upper()


def _slot_owner(payload: Any, default: str = DEFAULT_OWNER) -> str:
    """슬롯이 owner(대상 엔터티)를 선언했으면 그것을, 아니면 기본 회원 owner 를 쓴다.

    owner 를 임의로 회원으로 '자동 지정' 하지 않는다 — 파생 엔터티 집합처럼 자기 owner 를 선언한
    슬롯은 그 값을 그대로 들고 간다(서로 다른 owner 의 조건은 충돌로 보지 않기 위해서다).
    """
    if isinstance(payload, Mapping):
        for key in ("owner", "entity", "entity_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


# ── plan → 의미 AST ───────────────────────────────────────────────────────────


def plan_to_semantic_expr(
    plan: Mapping[str, Any],
    *,
    value_dimensions: Mapping[str, str] | None = None,
    owner: str = DEFAULT_OWNER,
) -> Any:
    """query_plan 의 확정 슬롯을 하나의 의미 AST 로 투영한다(NL 재파싱 없음)."""

    children: list[Any] = []
    children.extend(_dimension_filter_nodes(plan, owner))
    children.extend(_attribute_nodes(plan, owner))
    children.extend(_set_expression_nodes(plan, owner, value_dimensions or {}))
    return simplify(to_nnf(And(tuple(children))))


def _dimension_filter_nodes(plan: Mapping[str, Any], owner: str) -> list[Any]:
    nodes: list[Any] = []
    for dimension_filter in plan.get("dimension_filters") or []:
        if not isinstance(dimension_filter, Mapping):
            continue
        column = _short_column(dimension_filter.get("column"))
        if not column:
            continue
        values = [
            str(value)
            for value in (dimension_filter.get("codes") or dimension_filter.get("names") or [])
            if isinstance(value, str) and value
        ]
        if not values:
            continue
        spans = dimension_filter.get("value_spans") or []
        span = _span(spans[0] if spans else None, str(dimension_filter.get("evidence") or ""))
        predicate = Predicate(
            _slot_owner(dimension_filter, owner), column, "in", tuple(values), span
        )
        operator = str(dimension_filter.get("operator") or "IN").strip().upper()
        nodes.append(Not(predicate, source_span=span) if operator == "NOT_IN" else predicate)
    return nodes


def _attribute_nodes(plan: Mapping[str, Any], owner: str) -> list[Any]:
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), Mapping) else {}
    exclude = plan.get("exclude") if isinstance(plan.get("exclude"), Mapping) else {}
    nodes: list[Any] = []
    slot_owner = _slot_owner(target_user, owner)

    gender = target_user.get("gender")
    if isinstance(gender, str) and gender:
        nodes.append(Predicate(slot_owner, "gender", "eq", (gender,)))
    for slot, dimension in _LIST_ATTRIBUTE_SLOTS:
        values = [value for value in (target_user.get(slot) or []) if isinstance(value, str) and value]
        if values:
            nodes.append(Predicate(slot_owner, dimension, "in", tuple(values)))

    age_min, age_max = target_user.get("age_min"), target_user.get("age_max")
    if isinstance(age_min, int):
        nodes.append(Predicate(slot_owner, "age", "gte", (age_min,)))
    if isinstance(age_max, int):
        nodes.append(Predicate(slot_owner, "age", "lte", (age_max,)))
    for excluded_range in target_user.get("age_exclude_ranges") or []:
        if isinstance(excluded_range, Mapping):
            lower, upper = excluded_range.get("age_min"), excluded_range.get("age_max")
            if isinstance(lower, int) and isinstance(upper, int):
                nodes.append(Not(Predicate(slot_owner, "age", "between", (Range(lower, upper),))))

    exclude_owner = _slot_owner(exclude, owner)
    for slot, dimension in (("gender", "gender"), *_LIST_ATTRIBUTE_SLOTS):
        values = [value for value in (exclude.get(slot) or []) if isinstance(value, str) and value]
        if values:
            nodes.append(Not(Predicate(exclude_owner, dimension, "in", tuple(values))))
    return nodes


def _set_expression_nodes(
    plan: Mapping[str, Any], owner: str, value_dimensions: Mapping[str, str]
) -> list[Any]:
    nodes: list[Any] = []
    for expression in plan.get("set_expressions") or []:
        if not isinstance(expression, Mapping):
            continue
        ast = expression.get("set_ast")
        if ast is None:
            nodes.append(
                Unknown(
                    "UNSUPPORTED_EXPRESSION",
                    SourceSpan(0, 0, str(expression.get("expression_text") or "")),
                    metadata={"source": "set_expressions"},
                )
            )
            continue
        node = set_ast_to_expr(ast, _slot_owner(expression, owner), value_dimensions)
        if node is not None:
            nodes.append(node)
    return nodes


def set_ast_to_expr(
    ast: Any, owner: str = DEFAULT_OWNER, value_dimensions: Mapping[str, str] | None = None
) -> Any:
    """집합식 AST(set_ast)를 의미 AST 로 옮긴다 — 합집합/차집합 구조를 그대로 보존한다."""
    value_dimensions = value_dimensions or {}
    if not isinstance(ast, Mapping):
        return None
    node_type = ast.get("type")
    if node_type == "universe":
        return And(())  # 전칭(모집합) — 조건 없음
    if node_type in ("operand", "age_range"):
        return _operand_expr(ast, owner, value_dimensions)
    if node_type == "unknown_operand":
        text = str(ast.get("text") or "")
        return Unknown(
            "VALUE_UNRESOLVED",
            _span(ast.get("source_span"), text) or SourceSpan(0, 0, text),
            metadata={"condition_id": ast.get("condition_id")},
        )
    if node_type != "set_op":
        return None
    left = set_ast_to_expr(ast.get("left"), owner, value_dimensions)
    right = set_ast_to_expr(ast.get("right"), owner, value_dimensions)
    operands = [operand for operand in (left, right) if operand is not None]
    if not operands:
        return None
    op = ast.get("op")
    if op == "+":
        return Or(tuple(operands))
    if op == "-":
        if left is None or right is None:
            return operands[0]
        return And((left, Not(right)))
    return And(tuple(operands))


def _operand_expr(ast: Mapping[str, Any], owner: str, value_dimensions: Mapping[str, str]) -> Any:
    canonical = ast.get("canonical")
    span = _span(ast.get("source_span"), str(ast.get("matched_text") or ast.get("label") or ""))
    if ast.get("type") == "age_range":
        lower, upper = ast.get("age_min"), ast.get("age_max")
        if isinstance(lower, int) and isinstance(upper, int):
            return Predicate(owner, "age", "between", (Range(lower, upper),), span)
    if not isinstance(canonical, str) or not canonical:
        return Unknown("VALUE_UNRESOLVED", span, metadata={"node": dict(ast)})
    dimension = value_dimensions.get(canonical, "segment")
    return Predicate(owner, dimension, "eq", (canonical,), span)


# ── plan 의미 검증 ────────────────────────────────────────────────────────────


def verify_plan_semantics(
    plan: Mapping[str, Any],
    *,
    value_dimensions: Mapping[str, str] | None = None,
    owner: str = DEFAULT_OWNER,
) -> VerificationResult:
    """plan 이 담은 조건들의 충돌·미해결을 판정한다(SQL 생성 전 게이트)."""
    expr = plan_to_semantic_expr(plan, value_dimensions=value_dimensions, owner=owner)
    return verify_expression(expr)


# ── SQL 역검증 ────────────────────────────────────────────────────────────────


def _sql_column_occurrences(sql: str, dialect: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """생성 SQL 을 AST 로 되읽어 컬럼별 (극성, 값) 목록을 만든다.

    문자열 포함 검사가 아니라 sqlglot AST 를 쓴다 — ``SIDO NOT IN (...)`` 과
    ``NOT (SIDO IN (...))`` 은 같은 의미로, ``SIDO IN (...)`` 과는 다른 의미로 읽힌다.
    """
    from sql_semantics import extract_sql_semantics

    semantics = extract_sql_semantics(sql, dialect)
    occurrences: dict[str, list[dict[str, Any]]] = {}

    def record(field: Any, negated: bool, values: list[str], location: str) -> None:
        column = _short_column(field)
        if not column:
            return
        occurrences.setdefault(column, []).append(
            {
                "polarity": "negative" if negated else "positive",
                "values": [_strip_literal(value) for value in values],
                "location": location,
            }
        )

    for condition in semantics.in_conditions:
        record(condition.field, condition.negated, list(condition.values), "in")
    for sql_filter in semantics.filters:
        operator = (sql_filter.normalized_operator or "").strip()
        if operator not in ("=", "!=", "<>"):
            continue
        negated = bool(sql_filter.negated) != (operator in ("!=", "<>"))
        value = sql_filter.normalized_value
        record(sql_filter.normalized_field, negated, [value] if value is not None else [], sql_filter.location)
    for exists in semantics.exists_conditions:
        for sql_filter in exists.filters:
            operator = (sql_filter.normalized_operator or "").strip()
            if operator not in ("=", "!=", "<>"):
                continue
            negated = bool(exists.negated) != (bool(sql_filter.negated) != (operator in ("!=", "<>")))
            value = sql_filter.normalized_value
            record(sql_filter.normalized_field, negated, [value] if value is not None else [], "exists")
    return occurrences


def _strip_literal(value: Any) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def _sql_has_disjunction(sql: str, dialect: str | None = None) -> bool:
    """SQL 이 서로 다른 조건 사이의 OR 를 담고 있는가(값 나열 IN 은 OR 로 세지 않는다)."""
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(sql, read=dialect) if dialect else sqlglot.parse_one(sql)
    except Exception:
        return " or " in f" {sql.casefold()} "
    return bool(parsed.find(exp.Or))


def verify_compiled_sql(
    expr: Any,
    sql: str,
    *,
    dialect: str | None = None,
    columns: set[str] | frozenset[str] | None = None,
) -> VerificationResult:
    """의미 AST 와 생성 SQL 의 의미를 대조한다(§극성/구조/조건 유실).

    ``columns`` 는 물리 컬럼으로 확정된 dimension 만 검사 대상으로 삼는다 — 논리 슬롯(성별/등급 등)은
    코드값 확장·EXISTS 인코딩이 있어 여기서 판정하지 않는다(그쪽은 의미 검증 v2 소관).
    """
    normalized = to_nnf(expr)
    issues: list[VerificationIssue] = []
    try:
        occurrences = _sql_column_occurrences(sql, dialect)
    except Exception as error:  # 파싱 실패는 근거 없음 → 판정 보류(리뷰)
        return VerificationResult(
            "needs_clarification",
            normalized,
            (
                VerificationIssue(
                    "UNSUPPORTED_EXPRESSION",
                    "생성된 SQL 을 의미 검증용으로 파싱하지 못했습니다.",
                    metadata={"error": str(error)},
                ),
            ),
        )

    checked = {str(column).upper() for column in (columns or set())}
    for signed in signed_predicates(normalized):
        dimension = signed.predicate.dimension.upper()
        if checked and dimension not in checked:
            continue
        found = occurrences.get(dimension)
        if not found:
            if dimension.casefold() not in sql.casefold():
                issues.append(
                    VerificationIssue(
                        "MISSING_CONDITION",
                        f"'{dimension}' 조건이 생성된 SQL 에 반영되지 않았습니다.",
                        signed.source_span,
                        {"dimension": dimension, "expected_polarity": signed.polarity},
                    )
                )
            continue
        same_polarity = [item for item in found if item["polarity"] == signed.polarity]
        if not same_polarity:
            issues.append(
                VerificationIssue(
                    "POLARITY_MISMATCH",
                    (
                        f"'{dimension}' 의 제외 조건이 SQL 에서 포함 조건으로 변환되었습니다."
                        if signed.polarity == "negative"
                        else f"'{dimension}' 의 포함 조건이 SQL 에서 제외 조건으로 변환되었습니다."
                    ),
                    signed.source_span,
                    {
                        "dimension": dimension,
                        "source": f"{signed.polarity}:{[str(v) for v in signed.predicate.values]}",
                        "compiled": [item["polarity"] for item in found],
                    },
                )
            )
            continue
        expected = [canonicalize_value(value) for value in signed.predicate.values]
        actual_keys = {
            serialize_canonical_value(value) for item in same_polarity for value in item["values"]
        }
        if expected and actual_keys:
            overlap = detect_set_conflict(expected, [_reverse(key) for key in actual_keys])
            if overlap["type"] == "none":
                issues.append(
                    VerificationIssue(
                        "VALUE_MISMATCH",
                        f"'{dimension}' 조건의 값이 SQL 과 일치하지 않습니다.",
                        signed.source_span,
                        {
                            "dimension": dimension,
                            "expected": [str(value) for value in expected],
                            "compiled": sorted(actual_keys),
                        },
                    )
                )

    if _source_has_cross_dimension_or(normalized) and not _sql_has_disjunction(sql, dialect):
        issues.append(
            VerificationIssue(
                "LOGICAL_OPERATOR_MISMATCH",
                "원문의 OR 조건이 SQL 에서 AND 로 축소되었습니다.",
                metadata={"source": describe(normalized)},
            )
        )

    if any(issue.code in ("POLARITY_MISMATCH", "LOGICAL_OPERATOR_MISMATCH") for issue in issues):
        return VerificationResult("unsafe", normalized, tuple(issues))
    if issues:
        return VerificationResult("needs_clarification", normalized, tuple(issues))
    return VerificationResult("valid", normalized)


def _reverse(serialized: str) -> Any:
    try:
        return json.loads(serialized)
    except ValueError:
        return serialized


def _source_has_cross_dimension_or(expr: Any) -> bool:
    """서로 다른 dimension 을 잇는 OR 가 있는가(같은 컬럼 값 나열 OR 은 IN 으로 컴파일되므로 제외)."""
    if not has_or(expr):
        return False
    for node in _or_nodes(expr):
        scopes = set()
        for child in node.children:
            for signed in signed_predicates(child):
                scopes.add(scope_key(signed.predicate))
        if len(scopes) > 1:
            return True
    return False


def _or_nodes(expr: Any) -> list[Or]:
    return [node for node in iter_nodes(expr) if isinstance(node, Or) and len(node.children) > 1]


# ── 디버깅 payload ────────────────────────────────────────────────────────────


def semantic_debug_info(
    *,
    input_text: str,
    parser: str,
    plan: Mapping[str, Any],
    expr: Any,
    result: VerificationResult,
    compiled_sql: str | None = None,
) -> dict[str, Any]:
    """검증 실패 시 남기는 구조화 진단(§로그). 값은 plan 이 이미 담고 있는 수준만 싣는다."""
    return {
        "input_text": input_text,
        "parser": parser,
        "raw_plan": {
            "dimension_filters": plan.get("dimension_filters") or [],
            "target_user": plan.get("target_user") or {},
            "exclude": plan.get("exclude") or {},
            "set_expressions": plan.get("set_expressions") or [],
        },
        "normalized_expr": to_dict(expr),
        "normalized_summary": describe(expr),
        "compiled_sql": compiled_sql,
        "status": result.status,
        "issues": [issue.to_dict() for issue in result.issues],
    }
