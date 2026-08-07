"""'장바구니에 서로 다른 상품 N개 이상'을 Canonical Event IR 로 표현하는 계약.

2026-08-07 실측한 실패는 새 operator 가 없어서가 아니었다. ``aggregate.count_distinct`` 는 이미
선언된 capability 이고 컴파일러도 낮춘다. 실제 벽은 둘이었다.

* ``active_cart`` 소스에 필드 선언이 **하나도 없어서**, 보관 중 카트를 세려면 형제 소스의
  ``cart.product_id`` 를 참조할 수밖에 없었고 그것이 관계 스코프 위반으로 거절됐다.
* 그 거절 사유가 ``compiler_operation_unsupported`` 로 뭉개져 모델에게 '무엇을 고쳐야 하는지'가
  전달되지 않았고, 재시도가 같은 식 → '표현 불가' 신고로 왕복하다 3회를 소진했다.

그래서 여기서 재는 것은 "이 문장이 통과하는가"가 아니라 **다시 닫히지 않는가**다.

1. 상태 소스(``active_cart``)가 기반 소스의 직접 컬럼 필드를 전부 다시 선언한다(카탈로그 전역 규칙).
2. 회원별 distinct 집계 임계값이 두 소스 각각에서 컴파일되고, 두 SQL 의 **뜻이 다르다**.
3. distinct 여부가 SQL 을 실제로 바꾼다.
4. 소스/필드 스코프 불일치는 fail-close 하며 **사유가 보존**된다.
5. 레거시 ``cart_line_count`` 는 '서로 다른 상품 수'가 아니다(모수가 다르다).
6. 현재 보관 표면어가 ``active_cart`` 에 선언되어 모델 안내에 실린다.

실DB(CRMDW) 대조로 확인한 사실(이 테스트는 DB 를 부르지 않는다, 기록용). **모수를 반드시 함께
읽는다** — 아래 세 숫자는 서로 다른 세 집합이고, 이 셋을 섞으면 어느 소스를 골라야 하는지가
거꾸로 읽힌다:

* ``CART_PRODUCT_NO`` 는 라인 고유키다. KEEP_YN='Y' 114라인에 ``CART_PRODUCT_NO`` 114개,
  ``PRODUCT_ID`` 33개. 따라서 ``COUNT(DISTINCT CART_PRODUCT_NO)`` 는 상품 종류 수가 아니라 라인 수다.
* ≥3 회원 수(KEEP_YN 필터 **없는** cart 전체): 라인 4,929명 / 상품 종류 2,793명.
* ≥3 회원 수(KEEP_YN='Y' 인 active_cart): 라인 8명 / 상품 종류 2명.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pydantic
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import audience_validators  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import event_state_selection  # noqa: E402
import graph_rag  # noqa: E402
from query_pipeline.event_query.expressions import EventExpression  # noqa: E402

CATALOG_PATH = REPO_ROOT / "docs/data/runtime/semantics/audience_catalog.json"

EVIDENCE = {"text": "서로 다른 상품을 3개 이상", "start": 7, "end": 21}


@pytest.fixture(scope="module")
def context() -> event_compiler.CompileContext:
    """카탈로그가 실제로 배포하는 컴파일 문맥. 기본 문맥으로 재면 다른 사유가 나온다."""
    return audience_runtime.resolve_audience_catalog().compile_context(literals=True)


@pytest.fixture(scope="module")
def catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _distinct_count_threshold(
    *,
    source: str,
    field: str | None,
    distinct: bool = True,
    threshold: int = 3,
    operator: str = ">=",
    relation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """canonical 골격: Comparison(Aggregate(count, ...), '>=', Literal). Exists 로 감싸지 않는다."""
    return {
        "type": "comparison",
        "operator": operator,
        "left": {
            "type": "aggregate",
            "function": "count",
            "relation": relation or {"type": "source", "name": source},
            "expression": {"type": "field", "name": field} if field else None,
            "distinct": distinct,
        },
        "right": {"type": "literal", "value": threshold},
        "evidence": EVIDENCE,
    }


def _capability(
    expression: dict[str, Any], context: event_compiler.CompileContext
) -> event_compiler.CompilerCapabilityResult:
    return event_compiler.validate_compiler_capability(
        event_ir.condition_from_dict(expression), context=context
    )


def _sql(expression: dict[str, Any], context: event_compiler.CompileContext) -> str:
    return event_compiler.compile_expression(
        event_ir.condition_from_dict(expression), context=context
    ).sql


# ── 1. 카탈로그 전역 규칙: 상태 소스의 필드 표면 ────────────────────────────────────


def _direct_column_fields(catalog: dict[str, Any], source: str) -> dict[str, str]:
    """조인 파생이 아닌 필드의 **접미 이름 → 물리 컬럼**(product_id → PRODUCT_ID).

    ``expression``/``search_expressions`` 를 쓰는 필드는 기반 소스의 ``from_sql`` 조인 별칭을
    참조하므로 그 조인이 없는 상태 소스로 그대로 옮길 수 없다. 그 구분을 선언에서 파생한다.

    이름만이 아니라 컬럼까지 돌려주는 이유는, 이름만 맞고 **다른 컬럼**에 묶인 미러가
    통과하면 불변식이 아무것도 막지 못하기 때문이다(active_cart.product_id 가 QTY 를
    가리켜도 이름 비교는 통과한다).
    """
    found: dict[str, str] = {}
    for field_id, declaration in (catalog.get("fields") or {}).items():
        if not isinstance(declaration, dict) or declaration.get("source") != source:
            continue
        if declaration.get("expression") or declaration.get("search_expressions"):
            continue
        found[str(field_id).split(".", 1)[-1]] = str(declaration.get("column") or "")
    return found


def test_state_source_mirrors_base_direct_column_fields(catalog: dict[str, Any]) -> None:
    """``selected_by`` 로 선언된 모든 상태 소스는 기반 소스의 직접 컬럼 필드를 전부 갖는다.

    이 규칙이 깨진 상태가 정확히 이번 결함이었다 — 상태 소스를 고른 순간 셀 필드가 없어
    형제 소스를 참조하게 되고, 그 식은 컴파일되지 않는다. 새 상태 소스(미사용 쿠폰 등)가
    추가돼도 같은 함정에 빠지지 않도록 카탈로그 전역으로 잰다.
    """
    declarations = event_state_selection.declarations(catalog)
    assert declarations, "selected_by 선언이 사라졌다면 이 불변식의 대상도 사라진 것이다"
    for selection in declarations:
        base_fields = _direct_column_fields(catalog, selection.base)
        state_fields = _direct_column_fields(catalog, selection.selected)
        missing = sorted(set(base_fields) - set(state_fields))
        assert not missing, (
            f"상태 소스 {selection.selected!r} 에 기반 소스 {selection.base!r} 의 직접 컬럼 필드"
            f" {missing} 가 없다 — 이 소스로는 그 필드를 집계할 수 없다"
        )
        # 같은 이름이 **같은 물리 컬럼**을 가리켜야 미러다. 이름만 맞추면 상태 소스의
        # product_id 가 QTY 를 가리켜도 통과하고, 그 SQL 은 조용히 다른 뜻이 된다.
        for name, column in base_fields.items():
            assert state_fields[name] == column, (
                f"{selection.selected}.{name} 는 {state_fields[name]!r} 를 가리키는데 "
                f"기반 {selection.base}.{name} 는 {column!r} 다 — 미러가 아니다"
            )


def test_active_cart_declares_the_three_aggregatable_fields(catalog: dict[str, Any]) -> None:
    fields = catalog["fields"]
    for field_id, column in (
        ("active_cart.product_id", "PRODUCT_ID"),
        ("active_cart.quantity", "QTY"),
        ("active_cart.amount", "TOTAL_SALE_PRICE"),
    ):
        assert fields[field_id]["source"] == "active_cart"
        assert fields[field_id]["column"] == column
        # 조인 파생 필드를 여기에 넣으면 컴파일 시점에 깨진다(active_cart 에는 from_sql 이 없다).
        assert "expression" not in fields[field_id]
        assert "search_expressions" not in fields[field_id]


def test_active_cart_does_not_declare_join_derived_product_text(catalog: dict[str, Any]) -> None:
    """상품명·카테고리는 상품 마스터 조인을 쓰므로 active_cart 로 옮기지 않는다."""
    for field_id in ("active_cart.product_name", "active_cart.product_category", "active_cart.product_text"):
        assert field_id not in catalog["fields"]
    assert "from_sql" not in catalog["sources"]["active_cart"]


# ── 2. canonical 골격이 두 소스에서 컴파일된다 ──────────────────────────────────────


def test_active_cart_distinct_product_count_compiles(context: event_compiler.CompileContext) -> None:
    """Case 1 — 현재 장바구니에 서로 다른 상품 3개 이상."""
    expression = _distinct_count_threshold(source="active_cart", field="active_cart.product_id")
    capability = _capability(expression, context)
    assert capability.status == event_compiler.CAPABILITY_SUPPORTED
    assert capability.issues == ()

    sql = _sql(expression, context)
    # 문자열 전체가 아니라 뜻을 이루는 조각을 잰다(별칭·공백 변경으로 깨지지 않게).
    assert "ODS_MALL_OMS_CART" in sql
    assert "KEEP_YN = 'Y'" in sql
    assert "GROUP BY EAC.CART_ID" in sql
    assert "HAVING COUNT(DISTINCT EAC.PRODUCT_ID) >= 3" in sql
    assert "B.MEMBER_ID IN (" in sql


def test_historical_cart_distinct_product_count_compiles(context: event_compiler.CompileContext) -> None:
    """Case 2 — 과거 담은 이력 기준. 컴파일되지만 뜻이 다르다."""
    expression = _distinct_count_threshold(source="cart", field="cart.product_id")
    capability = _capability(expression, context)
    assert capability.status == event_compiler.CAPABILITY_SUPPORTED

    sql = _sql(expression, context)
    assert "HAVING COUNT(DISTINCT EC.PRODUCT_ID) >= 3" in sql
    # 보관 상태 술어가 **없다** — 구매·삭제된 라인까지 세는 모수다.
    assert "KEEP_YN" not in sql


def test_state_and_history_sources_are_not_interchangeable(
    context: event_compiler.CompileContext,
) -> None:
    """두 소스의 SQL 이 같아지면 '현재 담긴'과 '담은 적 있는'의 구분이 사라진 것이다."""
    active = _sql(_distinct_count_threshold(source="active_cart", field="active_cart.product_id"), context)
    history = _sql(_distinct_count_threshold(source="cart", field="cart.product_id"), context)
    assert active != history
    assert "KEEP_YN = 'Y'" in active and "KEEP_YN" not in history


def test_time_filtered_relation_compiles(context: event_compiler.CompileContext) -> None:
    """Case 5 — Aggregate.relation 이 Source 가 아니라 Filter 여도 낮춰진다."""
    expression = _distinct_count_threshold(
        source="active_cart",
        field="active_cart.product_id",
        relation={
            "type": "filter",
            "relation": {"type": "source", "name": "active_cart"},
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": "active_cart.occurred_at"},
                "window": {"type": "rolling", "value": 7, "unit": "day"},
            },
        },
    )
    assert _capability(expression, context).status == event_compiler.CAPABILITY_SUPPORTED
    sql = _sql(expression, context)
    assert "EAC.INS_DT >=" in sql
    assert "HAVING COUNT(DISTINCT EAC.PRODUCT_ID) >= 3" in sql
    assert "KEEP_YN = 'Y'" in sql


# ── 3. distinct 여부가 SQL 을 바꾼다 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("distinct", "field", "expected_fragment"),
    [
        (True, "active_cart.product_id", "COUNT(DISTINCT EAC.PRODUCT_ID) >= 3"),
        (False, "active_cart.product_id", "COUNT(EAC.PRODUCT_ID) >= 3"),
        (False, None, "COUNT(*) >= 3"),
    ],
)
def test_distinct_flag_changes_the_aggregate(
    context: event_compiler.CompileContext,
    distinct: bool,
    field: str | None,
    expected_fragment: str,
) -> None:
    """Case 3 — '서로 다른 N개'와 '라인 N건'은 같은 카트에서 값이 다르다."""
    sql = _sql(
        _distinct_count_threshold(source="active_cart", field=field, distinct=distinct), context
    )
    assert expected_fragment in sql


def test_distinct_and_line_count_sql_differ(context: event_compiler.CompileContext) -> None:
    kinds = {
        _sql(_distinct_count_threshold(source="active_cart", field="active_cart.product_id"), context),
        _sql(
            _distinct_count_threshold(
                source="active_cart", field="active_cart.product_id", distinct=False
            ),
            context,
        ),
        _sql(_distinct_count_threshold(source="active_cart", field=None, distinct=False), context),
    }
    assert len(kinds) == 3, "세 집계가 같은 SQL 로 무너지면 '종류/건수' 구분이 사라진다"


# ── 4. 스코프 불일치는 fail-close 하고 사유를 남긴다 ───────────────────────────────


def test_cross_source_field_fails_closed_with_reason(context: event_compiler.CompileContext) -> None:
    """Case 4 — active_cart 관계에서 cart.product_id 를 참조하면 컴파일되지 않는다."""
    capability = _capability(
        _distinct_count_threshold(source="active_cart", field="cart.product_id"), context
    )
    assert capability.status == event_compiler.CAPABILITY_UNSUPPORTED
    assert [issue.code for issue in capability.issues] == ["compiler_operation_unsupported"]

    issue = capability.issues[0]
    assert issue.reason == "field_out_of_relation_scope"
    # 무엇을 고쳐야 하는지가 남아 있어야 한다: 잘못된 필드, 그 필드의 소속, 실제 스코프.
    assert "cart.product_id" in (issue.detail or "")
    assert "active_cart" in (issue.detail or "")


def test_scope_reason_reaches_the_model_message(context: event_compiler.CompileContext) -> None:
    """진단이 검증 메시지까지 살아 있어야 재시도가 같은 실수를 반복하지 않는다."""
    capability = _capability(
        _distinct_count_threshold(source="active_cart", field="cart.product_id"), context
    )
    message = audience_validators._capability_message(capability.issues[0])
    assert "Canonical Event IR을 현재 SQL compiler가 표현하지 못합니다." in message
    assert "field_out_of_relation_scope" in message
    assert "cart.product_id" in message


def test_validator_carries_the_reason_and_keeps_the_issue_identity() -> None:
    """검증기를 **실제로 호출해** 소비자가 보는 값을 잰다.

    앞의 두 테스트는 컴파일러 산출물과 문장 조립 함수를 따로 본다. 그 둘이 다 통과해도
    ``CompilerCapabilityValidator`` 가 그 문장을 쓰지 않으면 사유는 사용자에게 닿지 않는다 —
    실제로 이 배선이 한 번 원래 문자열로 되돌아간 적이 있고(2026-08-07), 그때 위의 두
    테스트는 통과했다. 그래서 여기서는 종단 산출물인 ``RequirementIssue`` 를 직접 본다.

    ``id``/``path`` 는 이 issue 의 **정체성**이라 사유를 실었다고 바뀌면 안 된다.
    """
    expression = pydantic.TypeAdapter(EventExpression).validate_python(
        {
            "kind": "comparison",
            "operator": "gte",
            "left": {
                "kind": "aggregate",
                "function": "count",
                "distinct": True,
                "relation": {"kind": "entity", "entity": "active_cart"},
                "expression": {"kind": "attribute", "entity": "cart", "attribute": "product_id"},
            },
            "right": {"kind": "literal", "value": 3},
            "evidence": EVIDENCE,
        }
    )
    query = "장바구니에 서로 다른 상품을 3개 이상 담아둔 회원"
    issues = audience_validators.CompilerCapabilityValidator().validate(
        expression, query=query, literals=[]
    )

    assert len(issues) == 1
    issue = issues[0]
    # 정체성은 불변 — 이 두 값으로 issue 를 집는 소비자가 있다.
    assert issue.id == "unsupported_semantics:compiler_operation_unsupported"
    assert issue.path == "$.compiler_operation_unsupported"
    # 사유는 사용자·모델이 읽는 문장에 실제로 실려 있다.
    assert "reason=field_out_of_relation_scope" in issue.message
    assert "cart.product_id" in issue.message
    assert "active_cart" in issue.message


# ── 5. 잘못 감싼 골격이 다른 뜻으로 컴파일되지 않는다 ─────────────────────────────


def _exists_wrapped(inner: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": "active_cart"},
            "where": inner,
        },
        "evidence": EVIDENCE,
    }


def test_exists_wrapped_aggregate_keeps_the_having_judgement(
    context: event_compiler.CompileContext,
) -> None:
    """Case 6 — 모델이 실제로 냈던 Exists 감싼 모양은 거절되지 않는다.

    컴파일러는 이 모양을 회원 상관 존재 술어가 하나 덧붙은 형태로 낮춘다. 여기서 고정하는
    것은 "HAVING 판정이 사라지거나 임계값이 달라지지는 않는다"뿐이다 — 이 모양이 **옳다**는
    뜻이 아니다. 아래 테스트가 이 모양이 실제로 뜻을 바꾸는 연산자를 보인다.
    """
    sql = _sql(
        _exists_wrapped(_distinct_count_threshold(source="active_cart", field="active_cart.product_id")),
        context,
    )
    assert "HAVING COUNT(DISTINCT EAC.PRODUCT_ID) >= 3" in sql
    assert "KEEP_YN = 'Y'" in sql
    # 덧붙는 것은 회원 상관 존재 술어뿐이다.
    assert "EAC.CART_ID = B.MEMBER_ID" in sql


def test_exists_wrapping_changes_meaning_for_upper_bound_thresholds(
    context: event_compiler.CompileContext,
) -> None:
    """Exists 로 감싸는 것이 **안전한 습관이 아니라는** 근거.

    ``>=`` 에서는 덧붙은 존재 술어가 임계값에 함의되어 결과가 같다. 그러나 ``<=`` 에서는
    카트가 **하나도 없는** 회원이 갈린다 — 맨 비교는 상관 부분질의가 0 을 돌려주어 그를
    포함하고(0 <= 3), Exists 로 감싼 쪽은 카트 행이 하나도 없으니 EXISTS 가 거짓이 되어
    제외한다. 그래서 안내는 '감싸지 마라'를 연산자와 무관하게 말한다.
    """
    upper_bound = _distinct_count_threshold(
        source="active_cart", field="active_cart.product_id", operator="<=", threshold=3
    )
    bare = _sql(upper_bound, context)
    wrapped = _sql(_exists_wrapped(upper_bound), context)

    # 맨 비교는 회원당 스칼라 부분질의다 — 카트 행이 없으면 0 이 되어 조건을 만족한다.
    assert bare.startswith("(SELECT COUNT(DISTINCT EAC.PRODUCT_ID)")
    assert not bare.startswith("EXISTS")
    # 감싼 쪽은 그 앞에 '카트 행이 최소 하나 있다'를 더 요구한다 — 그만큼 모수가 좁다.
    assert wrapped.startswith("EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART")
    assert bare in wrapped and bare != wrapped


def test_canonical_shape_is_the_bare_comparison(context: event_compiler.CompileContext) -> None:
    """canonical 골격은 Exists 로 감싸지 않은 쪽이고, 그쪽이 더 좁은 SQL 을 낸다."""
    bare = _sql(
        _distinct_count_threshold(source="active_cart", field="active_cart.product_id"), context
    )
    assert not bare.startswith("EXISTS")
    assert bare.startswith("B.MEMBER_ID IN (")


# ── 6. 레거시 지표 의미 오용 차단 ─────────────────────────────────────────────────


def test_legacy_cart_line_count_is_not_distinct_product_count() -> None:
    """Case 7 — ``cart_line_count`` 는 라인 수다. '서로 다른 상품 수'로 쓰면 모수가 달라진다."""
    expressions = graph_rag._CART_AGGREGATE_METRIC_EXPRESSIONS
    assert expressions["cart_line_count"] == "COUNT(DISTINCT CART_PRODUCT_NO)"
    # 상품 종류 수를 뜻하는 지표는 이 레지스트리에 없다 — Event IR 경로가 소유한다.
    assert "COUNT(DISTINCT PRODUCT_ID)" not in set(expressions.values())


# '서로 다른 상품 수'를 뜻하는 표면어. 라인 수 지표가 이 말을 소유하면 조용히 다른 집합이 나간다.
DISTINCT_PRODUCT_SURFACES = ("상품 종류", "상품종류", "가짓수", "서로 다른 상품")


def test_line_count_metric_does_not_own_distinct_product_surfaces() -> None:
    """레거시 레인이 '종류' 표면어를 라인 수 지표로 가져가지 않는다.

    ``cart_line_count`` 의 synonyms 에는 '장바구니 상품 종류'가 달려 있었다. 그 표면어가
    라인 수로 컴파일되면 ≥3 기준으로 4,929명(라인)과 2,793명(상품 종류)이라는 **다른 집합**이
    같은 이름으로 나간다 — 틀린 매핑은 매핑 없음보다 나쁘다(CLAUDE.md §11).
    """
    metrics = graph_rag._cart_targets_registry().get("aggregate_metrics") or {}
    for metric_id, declaration in metrics.items():
        if not isinstance(declaration, dict):
            continue
        column = str(declaration.get("column") or "")
        if "PRODUCT_ID" in column:
            continue  # 실제로 상품 종류를 세는 지표라면 그 표면어를 가져도 된다
        for synonym in declaration.get("synonyms") or ():
            assert not any(surface in str(synonym) for surface in DISTINCT_PRODUCT_SURFACES), (
                f"{metric_id}({column}) 는 상품 종류를 세지 않는데 '{synonym}' 표면어를 소유한다"
            )


def test_event_ir_distinct_count_uses_product_id_not_line_key(
    context: event_compiler.CompileContext,
) -> None:
    sql = _sql(
        _distinct_count_threshold(source="active_cart", field="active_cart.product_id"), context
    )
    assert "COUNT(DISTINCT EAC.PRODUCT_ID)" in sql
    assert "CART_PRODUCT_NO" not in sql


# ── 7. 소스 귀속 선언이 모델 안내에 실린다 ────────────────────────────────────────


CURRENT_STATE_SURFACES = (
    "현재 장바구니",
    "장바구니에 담아둔",
    "장바구니에 담겨 있는",
    "아직 장바구니에 남아 있는",
    "현재 보관 중인",
)


def test_current_state_surfaces_belong_to_active_cart(catalog: dict[str, Any]) -> None:
    """Case 8(결정론 부분) — 현재 보관 표면어는 active_cart 에만 선언된다.

    모델이 실제로 어느 소스를 고르는지는 라이브 실행이 답한다. 여기서 잴 수 있는 것은
    **선언이 존재하고 두 소스가 갈라져 있는가**이며, 그것이 안내의 입력이다.
    """
    active = catalog["sources"]["active_cart"]
    cart = catalog["sources"]["cart"]
    declared = set(active.get("selection_surfaces") or ())
    for surface in CURRENT_STATE_SURFACES:
        assert surface in declared
        assert surface not in set(cart.get("selection_surfaces") or ())
        assert surface not in set(cart["aliases"])


def test_selection_surfaces_never_leak_into_negation_grounding_aliases(
    catalog: dict[str, Any],
) -> None:
    """소스 선택용 표면어를 ``aliases`` 에 넣지 않는다 — 그 목록은 겸용 어휘다.

    ``rolling_absence_claims._source_terms`` 는 label + aliases 로 '이 사건의 국소 부정'을
    접지한다. 긍정 상태 표면어를 aliases 에 넣으면 '6개월 이상 장바구니에 담겨 있는 상품이
    없는 회원'이 '최근 6개월 안에 담은 적 없음'으로 합성된다 — 원문은 **체류 시간**을 말하고
    합성된 것은 **부재 창**이라 부등호가 반대다. 실측(2026-08-07) A/B: 별칭에 넣기 전에는
    후보 0개(fail-close), 넣은 뒤에는 창 방향이 뒤집힌 Not(Exists(...)) 가 조용히 생겼다.
    """
    import rolling_absence_claims

    for source_id, declaration in (catalog.get("sources") or {}).items():
        surfaces = set(declaration.get("selection_surfaces") or ())
        overlap = surfaces & set(declaration.get("aliases") or ())
        assert not overlap, f"{source_id}: selection_surfaces 가 aliases 로 새어 나갔다: {sorted(overlap)}"

    # 겸용이 끊겼는지 그 소비자로 직접 확인한다(선언 검사만으로는 배선이 보장되지 않는다).
    for query in (
        "6개월 이상 장바구니에 담겨 있는 상품이 없는 회원",
        "3개월 이상 장바구니에 담아둔 상품이 없는 회원",
        "6개월 이상 장바구니에 담겨 있는 우유가 없는 회원",
    ):
        candidates = rolling_absence_claims._source_evidence_candidates(query, catalog)
        assert candidates == [], f"{query!r} 가 롤링 부재 후보를 만들었다: {candidates}"


def test_guidance_renders_the_source_selection_note() -> None:
    guidance = audience_runtime.audience_catalog_guidance()
    assert "- active_cart: 미결제 장바구니 (" in guidance
    for surface in CURRENT_STATE_SURFACES:
        assert surface in guidance
    # 두 소스가 무엇으로 갈리는지가 안내에 있어야 한다.
    assert "selection_note" not in guidance  # 키 이름이 아니라 문장이 실려야 한다
    assert "지금 유지 중인 장바구니" in guidance
    assert "장바구니 담기 **이력**이다" in guidance


def test_guidance_states_the_aggregate_threshold_shape() -> None:
    guidance = audience_runtime.audience_catalog_guidance()
    assert "회원별 건수·종류 임계" in guidance
    assert "Exists로 감싸지 않는다" in guidance
    assert "distinct:true" in guidance
    assert "관계 스코프 밖이라 컴파일되지 않는다" in guidance
    # 스코프 위반의 해법은 '포기'가 아니라 '필드가 선언된 소스를 고르기'다. 미지원 신고를
    # 지시하면 self-refutation 반박기(structurer._audience_repair_error)와 왕복한다 —
    # 그 반박기는 'count distinct'·'종류 수' 같은 표면어를 담은 미지원 신고를 즉시 되돌린다.
    assert "세려는 field가 [Fields]에 선언된 소스를 relation으로 고른다" in guidance
    assert "unsupported_semantics issue를 낸다" not in guidance


def test_guidance_resolves_the_product_text_conflict() -> None:
    """'담아둔 <상품명>'에서 두 지시가 충돌하지 않아야 한다.

    selection_note 는 '담아둔'을 active_cart 로 보내는데, 상품명·카테고리 검색 필드는 상품
    마스터 조인이 있는 cart 에만 있다. 어느 쪽을 쓰라는 말이 없으면 모델은 active_cart 관계에
    cart.product_text 를 걸고 그 식은 컴파일되지 않는다 — 이 결함의 원형과 같은 모양이다.
    """
    guidance = audience_runtime.audience_catalog_guidance()
    assert "상품 텍스트 검색 필드는 상품 마스터 조인이 있는 소스에만 있다" in guidance
    assert "active_cart에는 없으므로" in guidance


def test_bare_put_in_cart_surface_defaults_to_the_state_source(catalog: dict[str, Any]) -> None:
    """시제 표지가 없는 장바구니 요청의 기본 소스가 선언으로 정해져 있어야 한다.

    선언이 없으면 모델이 회차마다 소스를 뒤집는다(실측: 같은 프롬프트가 cart 와 active_cart 를
    번갈아 골랐다). '정확히 네 종류 담은' 기준 모수는 cart 653명 vs active_cart 1명이라
    그 흔들림이 곧 다른 답이다. 기본은 상태 소스이고, 이력 소스는 과거임을 **명시**했을 때만
    고른다 — 이력이 기본이 되면 이미 사고 지운 카트까지 세게 된다.
    """
    active_note = catalog["sources"]["active_cart"]["selection_note"]
    history_note = catalog["sources"]["cart"]["selection_note"]
    assert "기본으로 이 소스" in active_note
    assert "명시" in history_note
    # 두 선언이 서로 반대 기본값을 말하면 안 된다.
    assert "기본으로 이 소스" not in history_note

    guidance = audience_runtime.audience_catalog_guidance()
    assert guidance.count(active_note) == 1
    assert guidance.count(history_note) == 1


def test_guidance_is_deterministic() -> None:
    assert audience_runtime.audience_catalog_guidance() == audience_runtime.audience_catalog_guidance()


def test_active_cart_fields_are_offered_to_the_model() -> None:
    guidance = audience_runtime.audience_catalog_guidance()
    for field_id in ("active_cart.product_id", "active_cart.quantity", "active_cart.amount"):
        assert f"- {field_id}:" in guidance
