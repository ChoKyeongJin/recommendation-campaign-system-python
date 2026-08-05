"""복귀한 두 축이 **Event IR 경로 하나만** 쓴다는 회귀 방지 계약.

축2(프로필 스칼라 지표)·축3(캠페인당 평균 구매금액)이 되살아난 근거는 "옛 코드를 복원했다"가
아니라 "canonical Event IR 이 그 뜻을 표현하게 됐다"다. 그 구분이 흐려지는 방식은 늘 같다 —
어느 날 누군가 옛 슬롯 하나를 채우거나, IR 이 실패했을 때를 대비한 '임시' 폴백을 붙인다.
그 순간 같은 오디언스를 두 언어가 말하게 되고, 되돌리기는 다시 이행 프로젝트가 된다.

여기서 다섯 가지를 잰다.

1. 삭제된 SemanticPlan 스택을 import 하는 새 모듈이 없다.
2. 복귀한 두 축의 산출물이 canonical Event IR 표현 **하나**이고 legacy 오디언스 표면은 비어 있다.
3. 폐기된 등급/전이 축이 다시 노출되지 않았다.
4. 회원별 스칼라와 전역 순위가 **서로 다른 심볼**로 남아 있다(하나가 다른 하나를 흡수하지 않았다).
5. IR 이 실패하면 SQL 이 나가지 않는다(폴백 없음).
"""

from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import canonical_event_ir_grounding  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import member_scalar_metric_claims  # noqa: E402
import member_scalar_metrics  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)
from test_no_semantic_plan_residue import RETIRED_MODULES  # noqa: E402

CURRENT_DATE = "2026-08-04"

# 이 작업이 새로 만든 모듈. 삭제된 스택을 하나도 부르지 않아야 한다.
NEW_MODULES: tuple[str, ...] = (
    "member_scalar_metrics.py",
    "member_scalar_metric_claims.py",
)

# 9종 각각의 원문 · 지표 표면어 · 기대 물리 컬럼.
PROFILE_SCALAR_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("구매주기가 30일 이하인 회원", "구매주기", "MS.BUY_CYCLE <= 30"),
    ("누적 구매금액이 100000원 이상인 회원", "누적 구매금액", "MS.TOTAL_BUY_AMT >= 100000"),
    ("누적 구매건수가 5회 이상인 회원", "누적 구매건수", "MS.TOTAL_BUY_CNT >= 5"),
    ("평균 구매금액이 50000원 이상인 회원", "평균 구매금액", "MS.MEAN_BUY_AMT >= 50000"),
    ("최대 구매금액이 300000원 이상인 회원", "최대 구매금액", "MS.MAX_BUY_AMT >= 300000"),
    ("최소 구매금액이 1000원 이상인 회원", "최소 구매금액", "MS.MIN_BUY_AMT >= 1000"),
    ("누적 구매수량이 10회 이상인 회원", "누적 구매수량", "MS.TOTAL_BUY_QTY >= 10"),
    ("활동 개월 수가 6개월 이상인 회원", "활동 개월 수", "MS.ACTIVITY_MONTH_CNT >= 6"),
    ("구매 상품 수가 3회 이상인 회원", "구매 상품 수", "MS.BUY_PRODUCT_CNT >= 3"),
)


def _raw(query: str, span: str) -> dict:
    start = query.index(span)
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "metric",
                    "message": "The requested condition cannot be represented.",
                    "evidence": {
                        "text": span,
                        "start": start,
                        "end": start + len(span),
                    },
                }
            ],
        },
    }


def _sql_result(query: str, structured: dict) -> tuple[dict, dict]:
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=query,
    )
    return plan, result


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module_name", NEW_MODULES)
def test_new_modules_do_not_import_the_retired_stack(module_name: str) -> None:
    tree = ast.parse((REPO_ROOT / module_name).read_text(encoding="utf-8"))
    revived = sorted(_imported_roots(tree) & RETIRED_MODULES)
    assert not revived, (
        f"{module_name} 이 삭제된 중간표현 스택을 부른다: {revived}. "
        "복귀 근거는 옛 계층의 복원이 아니라 Event IR 의 표현력이다."
    )


@pytest.mark.parametrize(
    ("query", "span", "expected_predicate"),
    PROFILE_SCALAR_QUERIES,
    ids=[case[0] for case in PROFILE_SCALAR_QUERIES],
)
def test_profile_scalar_requests_execute_through_event_ir_only(
    query: str, span: str, expected_predicate: str
) -> None:
    """산출물은 canonical 표현 하나이고 legacy 오디언스 표면은 비어 있다."""

    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(_raw(query, span)), query, current_date=CURRENT_DATE
    )
    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert requirement["expression"] is not None
    assert canonical_event_ir_grounding.has_empty_legacy_audience_surface(structured)

    plan, result = _sql_result(query, structured)
    assert result["is_success"] is True, result.get("failure_reason")
    assert expected_predicate in result["sql"]
    assert canonical_event_ir_grounding.has_empty_legacy_audience_surface(plan)
    # 폐기된 순위 슬롯이 이 경로에서 되살아나지 않았다.
    assert plan.get("member_metric_ranking") is None
    assert plan.get("member_metric_selection") is None


def test_member_scalar_and_global_ranking_stay_separate_symbols() -> None:
    """같은 컬럼의 두 계약이 서로를 흡수하지 않는다."""

    catalog = audience_runtime.resolve_audience_catalog()
    scalar_ids = set(member_scalar_metrics.member_scalar_metric_ids(catalog))
    ranking_ids = {
        metric_id
        for metric_id, metric in catalog.metrics.items()
        if metric.ranking_entity is not None
    }
    assert scalar_ids and ranking_ids
    assert not scalar_ids & ranking_ids
    # 순위 지표는 여전히 순위 계약을 온전히 들고 있다.
    for metric_id in ranking_ids:
        metric = catalog.metrics[metric_id]
        assert metric.kind == "aggregate"
        assert metric.cardinality == "set"
        assert metric.ranking_limit_units
        assert metric.ranking_tie_policy == "exact_count"


def test_retired_grade_transition_axis_is_still_absent() -> None:
    """복귀 작업이 폐기된 등급/전이 축까지 함께 열지 않았다."""

    import plan_schema  # noqa: PLC0415
    import targeting_ir  # noqa: PLC0415

    assert "relational_operation" not in targeting_ir.SLOT_SHAPES
    assert all(spec.kind != "relational_operation" for spec in targeting_ir.CONDITION_SPECS)
    for key in ("relational_operations", "relational_ir"):
        assert plan_schema.kind_of(key) is None
    # 등급 전이 지표는 카탈로그 선언으로만 남아 있고 회원별 스칼라로 승격되지 않았다.
    catalog = audience_runtime.resolve_audience_catalog()
    assert catalog.metric("member_grade_transition").kind == "transition"
    assert catalog.metric("member_grade_transition").cardinality == "set"


def test_member_scalar_synthesis_has_no_fallback_when_the_contract_fails() -> None:
    """계약이 어긋나면 합성하지 않는다 — 비슷한 지표로 갈아타는 폴백이 없다."""

    query = "구매주기가 100000원 이하인 회원"  # 단위가 지표 선언(day)과 어긋난다
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(_raw(query, "구매주기")), query, current_date=CURRENT_DATE
    )
    assert structured["audience_requirement"]["expression"] is None
    assert structured["audience_requirement"]["issues"]
    _plan, result = _sql_result(query, structured)
    assert result["is_success"] is False
    assert not result["sql"]
    assert "BUY_CYCLE" not in (result["sql"] or "")


def test_member_scalar_synthesis_refuses_an_ambiguous_sentence() -> None:
    """조건이 하나 더 있으면 닫힌 문형이 아니다 — 나머지를 조용히 버리지 않는다."""

    query = "구매주기가 30일 이하이고 여성인 회원"
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(_raw(query, "구매주기")), query, current_date=CURRENT_DATE
    )
    assert structured["audience_requirement"]["expression"] is None
    assert structured["audience_requirement"]["issues"]


def _runtime_string_constants(path: Path) -> list[str]:
    """실행에 쓰이는 문자열 상수만(주석·docstring 제외).

    설명문에 예시로 적힌 표면어까지 금지하면 그 모듈은 왜 그렇게 하는지 적을 수 없게 된다.
    재려는 것은 **어휘가 코드에 박혔는가**이므로 실행 상수만 본다.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_member_scalar_claim_reads_only_declarations() -> None:
    """표면어·단위는 레지스트리 선언에서만 온다(모듈에 어휘를 심지 않는다)."""

    constants = _runtime_string_constants(REPO_ROOT / "member_scalar_metric_claims.py")
    registry = audience_runtime.member_metric_registry_snapshot()
    assert registry is not None
    for entry in registry["metrics"]:
        for synonym in entry["synonyms"]:
            for constant in constants:
                assert synonym not in constant, (
                    f"지표 표면어 {synonym!r} 가 실행 상수에 박혀 있다 — "
                    "어휘의 소유자는 레지스트리다."
                )


def test_capability_shortfall_is_reported_before_sql_generation() -> None:
    """capability 부족은 SQL 생성 도중의 예외가 아니라 판정 결과로 나온다."""

    import event_compiler  # noqa: PLC0415

    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="count",
            relation=event_ir.Source(name="purchase"),
            expression=event_ir.Tuple(
                items=(
                    event_ir.FieldRef("purchase.order_id"),
                    event_ir.FieldRef("purchase.amount"),
                )
            ),
            distinct=True,
        ),
        right=event_ir.Literal(value=1),
    )
    assert "aggregate.multi_column_count_distinct" in event_ir.expression_capabilities(
        expression
    )

    original = event_compiler.compiler_capabilities
    try:
        event_compiler.compiler_capabilities = (  # type: ignore[assignment]
            lambda dialect=None: original(dialect) - {"aggregate.multi_column_count_distinct"}
        )
        capability = event_compiler.validate_compiler_capability(expression)
    finally:
        event_compiler.compiler_capabilities = original  # type: ignore[assignment]

    assert capability.status == event_compiler.CAPABILITY_UNSUPPORTED
    assert any(
        issue.code == "compiler_capability_unsupported"
        and issue.symbol == "aggregate.multi_column_count_distinct"
        for issue in capability.issues
    )


def test_superlative_metric_name_is_not_an_arg_extreme_ranking() -> None:
    """'최소 구매금액이 1000원 이상' 은 임계 선택이지 argmin 이 아니다.

    이 문장의 '최소'는 스냅샷 지표 이름(``최소 구매금액`` = MIN_BUY_AMT)의 첫 낱말이다.
    순위로 읽으면 임계값이 통째로 무시되고 **회원 한 명**만 나온다(실측: argmin/limit 1).
    그러면서도 SQL 은 성공으로 보이므로 값이 조용히 틀린다.

    반대 방향도 함께 고정한다 — 임계 비교가 없는 진짜 최상급 요청은 여전히 순위다.
    """

    import analytical_intent  # noqa: PLC0415

    threshold = analytical_intent.analyze_analytical_intent(
        "최소 구매금액이 1000원 이상인 회원"
    )
    assert threshold is None

    ranking = analytical_intent.analyze_analytical_intent("구매금액이 가장 적은 회원")
    assert ranking is not None
    assert ranking["query_type"] == "ranking"
    assert ranking["comparison"]["operator"] == "argmin"


def test_owner_constant_matches_the_recorded_decision() -> None:
    """합성 소유자 이름이 감사 로그와 모듈 상수 두 곳에서 갈라지지 않는다."""

    assert member_scalar_metric_claims.OWNER.startswith("member_scalar_metrics.")
