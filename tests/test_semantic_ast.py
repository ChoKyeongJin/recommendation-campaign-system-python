"""의미 AST 단위 테스트 — 부정 정규화·정준화·부호 추출·집합/범위 충돌.

여기서 고정하는 것은 **의미 규칙**이지 특정 문장이 아니다. 파서가 어떤 표현을 어떻게 잡든, 그 결과가
이 AST 로 들어오면 극성·결합자·소유자가 보존되는지를 본다.
"""

from __future__ import annotations

import pytest

from semantic_ast import (
    And,
    Not,
    Or,
    Predicate,
    Range,
    SourceSpan,
    Unknown,
    canonicalize_predicate,
    describe,
    detect_conflicts,
    detect_range_conflict,
    detect_set_conflict,
    predicate_key,
    signed_predicates,
    to_nnf,
    unknown_nodes,
    verify_expression,
)


def _seoul(operator: str = "in") -> Predicate:
    return Predicate("member", "sido", operator, ("서울",), SourceSpan(0, 2, "서울"))


# ── 부정 정규화 ───────────────────────────────────────────────────────────────


def test_not_over_predicate_folds_into_negative_operator() -> None:
    assert to_nnf(Not(_seoul())) == Predicate("member", "sido", "not_in", ("서울",), SourceSpan(0, 2, "서울"))


def test_double_negation_is_eliminated() -> None:
    assert to_nnf(Not(Not(_seoul()))) == _seoul()


def test_de_morgan_pushes_negation_to_leaves() -> None:
    expr = Not(Or((_seoul(), Predicate("member", "gender", "eq", ("male",)))))
    normalized = to_nnf(expr)
    assert isinstance(normalized, And)
    assert normalized.children == (
        Predicate("member", "sido", "not_in", ("서울",), SourceSpan(0, 2, "서울")),
        Predicate("member", "gender", "neq", ("male",)),
    )


def test_negated_and_becomes_or() -> None:
    expr = Not(And((_seoul(), Predicate("member", "gender", "eq", ("male",)))))
    assert isinstance(to_nnf(expr), Or)


def test_negation_of_unknown_is_preserved_not_dropped() -> None:
    expr = to_nnf(Not(Unknown("OWNER_AMBIGUOUS", SourceSpan(0, 4, "담당자"))))
    assert isinstance(expr, Not)
    assert unknown_nodes(expr)


def test_nested_conjunctions_are_flattened_without_losing_children() -> None:
    expr = And((And((_seoul(), Predicate("member", "gender", "eq", ("male",)))), Unknown("VALUE_UNRESOLVED")))
    assert len(to_nnf(expr).children) == 3


# ── 정준화 ────────────────────────────────────────────────────────────────────


def test_canonicalization_splits_polarity_from_operator() -> None:
    canonical, polarity = canonicalize_predicate(_seoul("not_in"))
    assert polarity == "negative"
    assert canonical.operator == "in"
    assert canonical.operator_family == "membership"


def test_value_order_does_not_change_predicate_key() -> None:
    left = Predicate("member", "sido", "in", ("서울", "부산"))
    right = Predicate("member", "sido", "in", ("부산", "서울"))
    assert predicate_key(canonicalize_predicate(left)[0]) == predicate_key(canonicalize_predicate(right)[0])


def test_eq_and_in_share_one_comparison_family() -> None:
    membership, _ = canonicalize_predicate(Predicate("member", "gender", "eq", ("male",)))
    listed, _ = canonicalize_predicate(Predicate("member", "gender", "in", ("male",)))
    assert membership.operator_family == listed.operator_family == "membership"


# ── 부호 있는 predicate / 논리 경로 ───────────────────────────────────────────


def test_signed_predicate_reports_negative_polarity_and_empty_path() -> None:
    signed = signed_predicates(Not(_seoul()))
    assert len(signed) == 1
    assert signed[0].polarity == "negative"
    assert signed[0].path == ()


def test_logical_path_records_branch_of_each_leaf() -> None:
    expr = Or((_seoul(), And((Predicate("member", "gender", "eq", ("male",)), Unknown("VALUE_UNRESOLVED")))))
    signed = signed_predicates(expr)
    assert [node.path[0].type for node in signed] == ["or", "or"]
    assert signed[1].path[-1].type == "and"


# ── 집합 충돌 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("included", "excluded", "expected"),
    [
        (["서울"], ["부산"], "none"),
        (["서울"], ["서울"], "full"),
        (["서울", "부산"], ["서울"], "partial"),
        (["서울", "부산"], ["서울", "부산", "대구"], "full"),
    ],
)
def test_set_conflict_classification(included: list[str], excluded: list[str], expected: str) -> None:
    assert detect_set_conflict(included, excluded)["type"] == expected


def test_include_and_exclude_in_same_and_scope_is_full_conflict() -> None:
    expr = And((Predicate("member", "gender", "eq", ("male",)), Not(Predicate("member", "gender", "eq", ("male",)))))
    issues = detect_conflicts(expr)
    assert [issue.code for issue in issues] == ["FULL_CONFLICT"]
    assert issues[0].metadata["overlap"] == ["male"]


def test_partial_overlap_is_reported_with_overlapping_values() -> None:
    expr = And((Predicate("member", "sido", "in", ("서울", "부산")), Not(_seoul())))
    issues = detect_conflicts(expr)
    assert [issue.code for issue in issues] == ["PARTIAL_CONFLICT"]
    assert issues[0].metadata == {
        "owner": "member",
        "dimension": "sido",
        "included": ["부산", "서울"],
        "excluded": ["서울"],
        "overlap": ["서울"],
    }


def test_same_value_opposite_polarity_under_one_or_is_tautology() -> None:
    expr = Or((Predicate("member", "gender", "eq", ("male",)), Not(Predicate("member", "gender", "eq", ("male",)))))
    assert [issue.code for issue in detect_conflicts(expr)] == ["TAUTOLOGY"]


def test_opposite_polarity_in_different_or_branches_is_not_a_conflict() -> None:
    left = And((Predicate("member", "sido", "in", ("서울",)), Predicate("member", "gender", "eq", ("male",))))
    right = And((Predicate("member", "sido", "in", ("부산",)), Not(Predicate("member", "gender", "eq", ("male",)))))
    assert detect_conflicts(Or((left, right))) == []


def test_different_owners_never_conflict() -> None:
    expr = And((
        Predicate("member", "gender", "eq", ("male",)),
        Not(Predicate("account_manager", "gender", "eq", ("male",))),
    ))
    assert detect_conflicts(expr) == []


def test_different_dimensions_never_conflict() -> None:
    expr = And((Predicate("member", "gender", "eq", ("male",)), Not(Predicate("member", "lifecycle", "in", ("male",)))))
    assert detect_conflicts(expr) == []


# ── 범위 충돌 ─────────────────────────────────────────────────────────────────


def test_disjoint_numeric_ranges_are_full_conflict() -> None:
    assert detect_range_conflict([("gte", (30,)), ("lt", (20,))])["type"] == "full"


def test_overlapping_numeric_ranges_are_fine() -> None:
    assert detect_range_conflict([("gte", (20,)), ("lt", (30,))])["type"] == "none"


def test_conflicting_age_bounds_are_detected_in_expression() -> None:
    expr = And((Predicate("member", "age", "gte", (30,)), Predicate("member", "age", "lt", (20,))))
    assert [issue.code for issue in detect_conflicts(expr)] == ["FULL_CONFLICT"]


def test_incomparable_range_values_do_not_claim_a_conflict() -> None:
    assert detect_range_conflict([("gte", ("2026-01-01",)), ("lt", (20,))])["type"] == "none"


def test_between_range_is_normalized_from_range_value() -> None:
    assert detect_range_conflict([("between", (Range(20, 29),)), ("gte", (40,))])["type"] == "full"


# ── 검증 결과 상태 ────────────────────────────────────────────────────────────


def test_valid_expression_reports_valid_status() -> None:
    result = verify_expression(And((_seoul(), Predicate("member", "gender", "eq", ("female",)))))
    assert result.status == "valid"
    assert result.is_blocking is False


def test_unknown_inside_or_is_never_dropped_and_blocks() -> None:
    expr = Or((
        Predicate("member", "lifecycle", "in", ("vip",)),
        Unknown("OWNER_AMBIGUOUS", SourceSpan(6, 14, "서울 거주 고객"), candidates=("member", "account_manager")),
    ))
    result = verify_expression(expr)
    assert result.status == "needs_clarification"
    assert [issue.code for issue in result.issues] == ["OWNER_AMBIGUOUS"]
    assert unknown_nodes(result.expr)  # OR 축소 금지 — 남은 항이 그대로 있다
    assert len(result.expr.children) == 2


def test_full_conflict_reports_conflict_status() -> None:
    expr = And((Predicate("member", "gender", "eq", ("male",)), Not(Predicate("member", "gender", "eq", ("male",)))))
    assert verify_expression(expr).status == "conflict"


def test_describe_renders_polarity_and_operator() -> None:
    assert describe(to_nnf(Not(_seoul()))) == "member.sido not_in(서울)"
