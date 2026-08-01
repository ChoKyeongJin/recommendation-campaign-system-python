"""범용 direct-column 필터 계약 — 필드별 predicate builder 없이 레지스트리 선언만으로 컴파일된다.

세 층을 지킨다:
  1. 단위: compile_registered_direct_column_filters 의 연산자/정규화/오류 계약(순수 모듈만 import).
  2. 회귀: cart_type 이 카트 빌더 3경로에서 이전과 동일한 술어로 컴파일되고 dropped 로 오표시되지 않는다.
  3. 구조/드리프트 가드: 삭제된 전용 빌더(_cart_type_predicates 등)가 되살아나지 않고, 레지스트리에
     등록된 물리 컬럼을 범용 컴파일러 밖에서 직접 SQL 로 조립하지 않는다(AST 검사). `*_predicates`
     함수는 명시 allowlist 로 고정한다 — 새 필드는 전용 빌더가 아니라 DIRECT_COLUMN_FILTER_SPECS
     선언 + 설정 키로 열어야 한다.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import compiler_strategies as cs  # noqa: E402

SHIPPED_CONFIG_PATH = REPO_ROOT / "docs" / "data" / "member_target_filters.json"


def _shipped_config() -> dict:
    return json.loads(SHIPPED_CONFIG_PATH.read_text(encoding="utf-8"))


def _compile(plan: dict, alias: str = "A", config: dict | None = None):
    return cs.compile_registered_direct_column_filters(
        query_plan=plan,
        field_names={"cart.type"},
        alias=alias,
        config=config if config is not None else _shipped_config(),
    )


def _cart_plan(node) -> dict:
    return {"target_user": {"cart_type": node}}


# ── 1. 단위: 연산자/alias/정규화 ─────────────────────────────────────────────────────────


def test_eq_compiles_with_alias() -> None:
    result = _compile(_cart_plan({"value": "CART_TYPE_CD.REGULARDELIVERY"}))
    assert result.predicates == ("A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'",)
    assert result.consumed_paths == frozenset({"target_user.cart_type"})
    assert result.unsupported_paths == frozenset()


def test_explicit_eq_operator_is_equivalent() -> None:
    explicit = _compile(_cart_plan({"operator": "eq", "value": "CART_TYPE_CD.PICKUP"}))
    implicit = _compile(_cart_plan({"value": "CART_TYPE_CD.PICKUP"}))
    assert explicit == implicit


def test_in_compiles_and_is_input_order_independent() -> None:
    forward = _compile(_cart_plan({"operator": "in", "values": ["CART_TYPE_CD.PICKUP", "CART_TYPE_CD.B2E"]}))
    backward = _compile(_cart_plan({"operator": "in", "values": ["CART_TYPE_CD.B2E", "CART_TYPE_CD.PICKUP"]}))
    assert forward.predicates == ("A.CART_TYPE_CD IN ('CART_TYPE_CD.B2E', 'CART_TYPE_CD.PICKUP')",)
    assert forward == backward  # 동일 입력 집합 → 항상 동일 SQL(정렬로 결정론)


def test_not_in_compiles() -> None:
    result = _compile(_cart_plan({"operator": "not_in", "values": ["CART_TYPE_CD.B2E"]}))
    assert result.predicates == ("A.CART_TYPE_CD NOT IN ('CART_TYPE_CD.B2E')",)


def test_empty_alias_has_no_dangling_dot() -> None:
    result = _compile(_cart_plan({"value": "CART_TYPE_CD.GENERAL"}), alias="")
    assert result.predicates == ("CART_TYPE_CD = 'CART_TYPE_CD.GENERAL'",)


def test_absent_slot_returns_empty_result() -> None:
    result = _compile({"target_user": {}})
    assert result == cs.CompiledDirectColumnFilters()
    assert _compile({}) == cs.CompiledDirectColumnFilters()


def test_duplicate_values_are_deduplicated() -> None:
    result = _compile(_cart_plan({"operator": "in", "values": ["CART_TYPE_CD.B2E", "CART_TYPE_CD.B2E"]}))
    assert result.predicates == ("A.CART_TYPE_CD IN ('CART_TYPE_CD.B2E')",)


def test_quoting_goes_through_shared_dialect() -> None:
    predicate = cs.render_direct_column_predicate(
        column="COL", operator="eq", values=("O'Brien",), alias="A"
    )
    assert predicate == "A.COL = 'O''Brien'"  # 값은 _quote 단일 경로 — 따옴표 이스케이프 보장


# ── 1b. 단위: 컴파일 불가 입력은 unsupported path(조용한 드롭 금지) ─────────────────────


@pytest.mark.parametrize(
    "node",
    [
        {"value": ""},                                        # 빈 문자열
        {"value": 3},                                          # 값 타입 위반
        {"operator": "in", "values": []},                     # 빈 IN 목록
        {"operator": "in"},                                    # 값 부재
        {"operator": "like", "value": "CART_TYPE_CD.B2E"},   # 허용되지 않은 연산자
        {"value": "CART_TYPE_CD.NOPE"},                       # 값 레지스트리에 없는 값
        "CART_TYPE_CD.B2E",                                    # dict 아님
        {"operator": "in", "values": ["CART_TYPE_CD.B2E", 7]},  # 다중값 중 타입 위반
    ],
)
def test_uncompilable_nodes_are_reported_as_unsupported(node) -> None:
    result = _compile(_cart_plan(node))
    assert result.predicates == ()
    assert result.consumed_paths == frozenset()
    assert result.unsupported_paths == frozenset({"target_user.cart_type"})


def test_single_value_field_rejects_multiple_values() -> None:
    spec = cs.DirectColumnFilterSpec(
        logical_field="cart.type",
        source_path=("target_user", "cart_type"),
        column_setting=("cart_targets", "cart_type_column"),
        allowed_operators=frozenset({"eq", "in"}),
        value_registry=("cart_targets", "cart_types"),
        allow_multiple=False,
    )
    result = cs.compile_registered_direct_column_filters(
        query_plan=_cart_plan({"operator": "in", "values": ["CART_TYPE_CD.B2E", "CART_TYPE_CD.PICKUP"]}),
        field_names={"cart.type"},
        alias="A",
        config=_shipped_config(),
        specs={"cart.type": spec},
    )
    assert result.predicates == ()
    assert result.unsupported_paths == frozenset({"target_user.cart_type"})


def test_spec_allowed_operators_gate_the_input() -> None:
    spec = cs.DirectColumnFilterSpec(
        logical_field="cart.type",
        source_path=("target_user", "cart_type"),
        column_setting=("cart_targets", "cart_type_column"),
        allowed_operators=frozenset({"eq"}),
        value_registry=("cart_targets", "cart_types"),
    )
    result = cs.compile_registered_direct_column_filters(
        query_plan=_cart_plan({"operator": "not_in", "values": ["CART_TYPE_CD.B2E"]}),
        field_names={"cart.type"},
        alias="A",
        config=_shipped_config(),
        specs={"cart.type": spec},
    )
    assert result.unsupported_paths == frozenset({"target_user.cart_type"})


# ── 1c. 단위: 배선/설정 계약 위반은 즉시 실패(임의 폴백 금지) ───────────────────────────


def test_unregistered_field_name_fails_fast() -> None:
    with pytest.raises(cs.DirectColumnFilterError):
        cs.compile_registered_direct_column_filters(
            query_plan={}, field_names={"cart.nope"}, alias="A", config=_shipped_config()
        )


def test_missing_column_binding_fails_fast_instead_of_fallback() -> None:
    broken = {"cart_targets": {"cart_types": _shipped_config()["cart_targets"]["cart_types"]}}
    with pytest.raises(cs.DirectColumnFilterError):
        _compile(_cart_plan({"value": "CART_TYPE_CD.GENERAL"}), config=broken)
    with pytest.raises(cs.DirectColumnFilterError):
        cs.resolve_direct_filter_column({}, "cart.type")


def test_missing_value_registry_fails_fast_on_compile_path() -> None:
    broken = {"cart_targets": {"cart_type_column": "C.CART_TYPE_CD"}}
    with pytest.raises(cs.DirectColumnFilterError):
        _compile(_cart_plan({"value": "CART_TYPE_CD.GENERAL"}), config=broken)
    # 어휘 노출 경로는 missing_ok 로 빈 목록을 허용한다(필드가 어휘에 안 올라갈 뿐).
    assert cs.direct_filter_value_entries(broken, "cart.type", missing_ok=True) == ()


def test_render_rejects_bad_column_and_operator() -> None:
    with pytest.raises(cs.DirectColumnFilterError):
        cs.render_direct_column_predicate(column="1; DROP", operator="eq", values=("x",))
    with pytest.raises(cs.DirectColumnFilterError):
        cs.render_direct_column_predicate(column="COL", operator="like", values=("x",))
    with pytest.raises(cs.DirectColumnFilterError):
        cs.render_direct_column_predicate(column="COL", operator="eq", values=())


# ── 2. 회귀: 카트 빌더 3경로 + dropped 연계 ──────────────────────────────────────────────


def _graph_rag():
    import graph_rag

    return graph_rag


CART_TYPE_NODE = {
    "canonical": "regular_delivery_cart",
    "value": "CART_TYPE_CD.REGULARDELIVERY",
    "label": "정기배송 장바구니",
    "unpaid_only": False,
}


def test_cart_type_only_template_matches_previous_sql() -> None:
    graph_rag = _graph_rag()
    candidate = graph_rag._build_cart_targets_candidate(
        {"target_user": {"cart_type": dict(CART_TYPE_NODE), "behaviors": []}, "campaign_constraints": {}}
    )
    sql = candidate.get("content") or candidate.get("sql") or ""
    assert "A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "KEEP_YN" not in sql  # 유형만 물은 오디언스는 미결제 한정을 걸지 않는다(기존 동작 유지)
    assert "target_user.cart_type" not in candidate["dropped_conditions"]


def test_cart_abandoner_with_type_keeps_both_predicates() -> None:
    graph_rag = _graph_rag()
    node = dict(CART_TYPE_NODE, unpaid_only=True)
    candidate = graph_rag._build_cart_targets_candidate(
        {
            "target_user": {"cart_type": node, "behaviors": ["cart_abandoner"]},
            "campaign_constraints": {"objective": "repurchase"},
        }
    )
    sql = candidate.get("content") or candidate.get("sql") or ""
    assert "A.KEEP_YN = 'Y'" in sql
    assert "A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert candidate["dropped_conditions"] == []


def test_cart_aggregate_builder_narrows_lines_by_type() -> None:
    graph_rag = _graph_rag()
    candidate = graph_rag.build_cart_aggregate_targets_sql_candidate(
        {
            "target_user": {
                "cart_aggregate": {"metric": "cart_line_count", "operator": ">=", "threshold": 3},
                "cart_type": dict(CART_TYPE_NODE),
                "behaviors": [],
            },
            "campaign_constraints": {},
        }
    )
    sql = candidate.get("content") or candidate.get("sql") or ""
    assert "AND CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql  # 서브쿼리라 alias 없음
    assert "target_user.cart_type" not in candidate["dropped_conditions"]


def test_dropped_conditions_use_consumed_paths_not_field_names() -> None:
    graph_rag = _graph_rag()
    plan = {"target_user": {"behaviors": []}}
    compiled = {"unsupported": ["target_user.cart_type", "target_user.interests"]}

    # 실제로 컴파일러가 소비한 경로는 dropped 에서 빠진다.
    consumed = cs.CompiledDirectColumnFilters(
        predicates=("A.CART_TYPE_CD = 'CART_TYPE_CD.GENERAL'",),
        consumed_paths=frozenset({"target_user.cart_type"}),
    )
    candidate: dict = {}
    graph_rag._attach_cart_dropped_conditions(candidate, plan, compiled, direct_filters=consumed)
    assert candidate["dropped_conditions"] == ["target_user.interests"]

    # 값이 있었지만 컴파일하지 못한 경로는 dropped 에 남거나 추가된다.
    failed = cs.CompiledDirectColumnFilters(unsupported_paths=frozenset({"target_user.cart_type"}))
    candidate = {}
    graph_rag._attach_cart_dropped_conditions(candidate, plan, {"unsupported": []}, direct_filters=failed)
    assert candidate["dropped_conditions"] == ["target_user.cart_type"]


def test_uncompilable_cart_type_fails_closed_instead_of_silent_omission() -> None:
    """미등록 값은 unsupported path 로 기록되고, 공개 직접 호출은 fail-closed(None)로 거부된다.

    예전 전용 빌더는 이 입력에서 조건을 **조용히 뺀** SQL 을 돌려줬다(dropped 기록도 없음) —
    범용 경로는 컴파일 실패를 unsupported 로 남기므로 _admitted_sql_builder 가 부분 SQL 을 막는다."""
    graph_rag = _graph_rag()
    node = dict(CART_TYPE_NODE, value="CART_TYPE_CD.NOT_A_REGISTERED_VALUE")
    plan = {"target_user": {"cart_type": node, "behaviors": []}, "campaign_constraints": {}}
    assert graph_rag._build_cart_targets_candidate(plan) is None
    # unsupported 기록 자체는 범용 컴파일 결과가 남긴다(dropped 고지의 근거).
    direct = graph_rag._compile_cart_direct_column_filters(plan, alias="A")
    assert direct.predicates == ()
    assert direct.unsupported_paths == frozenset({"target_user.cart_type"})


def test_llm_lexicon_still_exposes_cart_types_from_registry() -> None:
    graph_rag = _graph_rag()
    allowed = graph_rag._llm_slot_allowed()["cart_types"]
    assert "regular_delivery_cart" in allowed
    shape = allowed["regular_delivery_cart"]
    assert shape["value"] == "CART_TYPE_CD.REGULARDELIVERY"
    assert shape["unpaid_only"] is False
    assert allowed["CART_TYPE_CD.REGULARDELIVERY"] == shape  # canonical/value 양키 노출 유지


# ── 3. 구조/드리프트 가드 ───────────────────────────────────────────────────────────────

# direct-column 필드를 소비할 수 있는 모듈(범용 컴파일러 + 설정/도구). 이 밖에서 레지스트리 물리
# 컬럼을 문자열로 직접 조립하면 실패한다.
_SCANNED_SOURCES = ("graph_rag.py", "confidence.py", "conceptual_targeting.py", "targeting_ir.py")

# graph_rag 에 남아 있어도 되는 `*_predicates` 빌더(복합 전략 — 날짜 연산/계층 판별/정책/사건 결합).
# 여기에 없는 이름이 새로 생기면 실패한다: 새 direct-column 필드는 전용 빌더가 아니라
# compiler_strategies.DIRECT_COLUMN_FILTER_SPECS 선언으로 열어야 한다.
_ALLOWED_PREDICATES_BUILDERS = {
    "_member_policy_predicates",     # 정책 계약(appliedPolicyFilters) 렌더 — 정책 강제 로직
    "_cart_retention_predicates",    # 날짜 경계 연산(datetime_cutoff) — 단순 컬럼 필터 아님
    "_cart_keep_predicates",         # 정책 상수(KEEP_YN='Y') + 도메인 게이트 — plan 경로 필터 아님
    "_member_region_predicates",     # 행정 계층 AND/OR 판별 — 복합 전략
    "_scope_predicates",             # 상품 마스터 다중 컬럼 OR — 집계 scope 전용
    "_purchase_absence_predicates",  # anti-join 사건 논리
    "_purchase_event_predicates",    # 사건 논리식 결합
}


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_deleted_field_specific_builders_do_not_come_back() -> None:
    graph_rag = _graph_rag()
    for name in ("_cart_type_predicates", "_cart_type_column", "_cart_type_entries"):
        assert not hasattr(graph_rag, name), f"삭제된 전용 빌더가 되살아났다: {name}"


def test_predicates_builders_are_allowlisted() -> None:
    found: set[str] = set()
    for source in ("graph_rag.py", *[p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "rag").glob("*.py")]):
        for node in ast.walk(_parsed(REPO_ROOT / source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_predicates"):
                found.add(node.name)
    new_builders = found - _ALLOWED_PREDICATES_BUILDERS
    assert not new_builders, (
        f"새 `*_predicates` 빌더가 추가됐다: {sorted(new_builders)}.\n"
        "direct-column 필드라면 전용 빌더 대신 compiler_strategies.DIRECT_COLUMN_FILTER_SPECS 에 "
        "선언하고 compile_registered_direct_column_filters 를 쓰라. 복합 전략이면 이 allowlist 에 "
        "사유와 함께 추가하라."
    )
    assert found, "스캔이 아무 빌더도 찾지 못했다 — 검사가 공허하다."


def test_registered_specs_are_wired_to_real_config_and_strategies() -> None:
    config = _shipped_config()
    assert cs.DIRECT_COLUMN_FILTER_SPECS, "레지스트리가 비면 이 계약 전체가 공허해진다."
    assert "cart.type" in cs.DIRECT_COLUMN_FILTER_SPECS
    for field_name, spec in cs.DIRECT_COLUMN_FILTER_SPECS.items():
        assert spec.logical_field == field_name
        assert spec.source_path and all(part.isidentifier() for part in spec.source_path)
        # 컴파일 전략은 dispatch 레지스트리에 실재해야 한다(죽은 라벨 금지).
        assert spec.compile_strategy in cs.COMPILER_STRATEGIES, (
            f"{field_name}: 등록되지 않은 compile_strategy {spec.compile_strategy!r}"
        )
        assert spec.allowed_operators <= cs.SUPPORTED_DIRECT_OPERATORS, (
            f"{field_name}: 범용 컴파일러가 지원하지 않는 연산자 선언 {sorted(spec.allowed_operators)}"
        )
        # 물리 컬럼 바인딩·값 레지스트리는 배포 설정에서 실제로 해석돼야 한다(fail fast 계약 검증).
        column = cs.resolve_direct_filter_column(config, field_name)
        assert column.isidentifier()
        if spec.value_registry is not None:
            assert cs.direct_filter_value_entries(config, field_name), (
                f"{field_name}: 값 레지스트리가 비었다"
            )


def test_registered_columns_are_not_assembled_outside_the_compiler() -> None:
    """레지스트리에 등록된 물리 컬럼을 다른 모듈이 문자열 상수로 다시 조립하면 드리프트다.

    docstring 은 제외하고 실제 코드 상수만 본다(설정 JSON·테스트는 스캔 대상이 아니다)."""
    config = _shipped_config()
    columns = {cs.resolve_direct_filter_column(config, name) for name in cs.DIRECT_COLUMN_FILTER_SPECS}
    offenders: list[str] = []
    for source in _SCANNED_SOURCES:
        tree = _parsed(REPO_ROOT / source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and any(column in node.value for column in columns)
            ):
                offenders.append(f"{source}:{node.lineno}: {node.value[:80]!r}")
    assert not offenders, (
        "direct-column 레지스트리 소유 컬럼이 범용 컴파일러 밖에서 문자열로 조립됐다:\n  "
        + "\n  ".join(offenders)
        + "\n\ncompile_registered_direct_column_filters / resolve_direct_filter_column 을 쓰라."
    )
