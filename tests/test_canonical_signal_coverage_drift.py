"""canonical 신호 커버리지 선언의 드리프트 가드.

`canonical_signal_coverage` 는 "Event IR 이 이 원문 신호를 이미 소유했는가"를 카탈로그 선언으로
판정한다. 이 판정이 틀리는 두 방향은 대가가 정반대다:

  · 과잉 커버 → 조건이 사라진 SQL 이 경고 없이 나간다(조용한 오답).
  · 과소 커버 → 정상 요청이 드롭 경고로 막힌다(시끄러운 오탐).

그래서 **선언 누락이 조용히 '커버 아님'으로 흘러가면 안 된다.** 감지기가 아는 모든 신호 family 는
카탈로그에 표현 가능/불가능 중 하나로 **명시 선언**되어야 하고, 표현 가능하다고 선언한 심볼은
카탈로그에 실재해야 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import canonical_signal_coverage  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402


def _catalog() -> dict:
    return audience_runtime.load_audience_catalog_config()


def _detector_families() -> set[str]:
    """결정론 드롭 감지기가 실제로 이름을 대는 신호 family 전부."""
    return (
        {"gender", "cart", "login", "purchase_absence"}
        | set(graph_rag._CAMPAIGN_RESPONSE_SIGNAL_LABELS)
        | {"consent"}
    )


def test_every_detector_family_is_declared_one_way_or_the_other() -> None:
    declared = canonical_signal_coverage.declared_families(_catalog())
    missing = _detector_families() - declared
    assert not missing, (
        f"감지기가 아는 신호 family 가 카탈로그에 선언되지 않았다: {sorted(missing)}. "
        "Event IR 로 표현 가능하면 sources/fields 를, 불가능하면 expressible:false 를 선언하라 — "
        "선언하지 않으면 '아직 안 적었다'와 '못 한다'를 구분할 수 없다."
    )


def test_expressible_declarations_point_at_real_catalog_symbols() -> None:
    catalog = _catalog()
    known = set(catalog.get("sources") or {}) | set(catalog.get("fields") or {})
    inexpressible = canonical_signal_coverage.inexpressible_families(catalog)
    for family, symbols in canonical_signal_coverage.declaration_symbols(catalog).items():
        if family in inexpressible:
            continue
        unknown = symbols - known
        assert not unknown, (
            f"family {family!r} 이 카탈로그에 없는 심볼을 가리킨다: {sorted(unknown)}. "
            "오타 하나가 조용한 과소 커버(=정상 요청 차단)가 된다."
        )


def test_inexpressible_families_are_never_covered() -> None:
    """표현 불가 선언은 우연한 심볼 일치보다 세다 — 그래야 그 축의 드롭이 계속 시끄럽다."""
    catalog = _catalog()
    inexpressible = canonical_signal_coverage.inexpressible_families(catalog)
    assert inexpressible, "표현 불가 family 가 하나도 없다면 선언이 사라진 것이다(수신동의·쿠폰 사용)."

    expression = event_ir.And(
        operands=(
            event_ir.Comparison(
                operator="=",
                left=event_ir.FieldRef(name="subject.gender"),
                right=event_ir.Literal(value="female"),
            ),
            event_ir.Exists(relation=event_ir.Source(name="cart")),
        )
    )
    plan = {"event_expression": {"expression": expression.to_dict(), "source": "audience_requirement"}}
    covered = canonical_signal_coverage.covered_families(plan, catalog)

    assert "gender" in covered and "cart" in covered
    assert not (covered & inexpressible)


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param({}, id="no-event-expression"),
        pytest.param({"event_expression": {"source": "audience_requirement"}}, id="no-expression-body"),
        pytest.param(
            {"event_expression": {"expression": {"type": "nonsense"}, "source": "audience_requirement"}},
            id="broken-ir",
        ),
    ],
)
def test_coverage_fails_closed(plan: dict) -> None:
    """IR 이 없거나 파손이면 아무것도 소유하지 못한다 — 커버 판정에서도 fail-close."""
    assert canonical_signal_coverage.covered_families(plan, _catalog()) == frozenset()


def test_undeclared_family_is_not_covered() -> None:
    """선언에 없는 family 는 IR 이 무엇을 담고 있든 커버가 아니다(기본값이 fail-close)."""
    expression = event_ir.Exists(relation=event_ir.Source(name="cart"))
    plan = {"event_expression": {"expression": expression.to_dict(), "source": "audience_requirement"}}
    covered = canonical_signal_coverage.covered_families(plan, {"signal_coverage": {}})
    assert covered == frozenset()


def test_login_channel_comparison_covers_login_without_a_login_source() -> None:
    """LAST_LOGIN_CHANNEL은 subject 직접 필드라 Source("login") 노드가 생기지 않는다."""
    expression = event_ir.Not(event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef(name="subject.last_login_channel"),
        right=event_ir.Literal(value="app_user"),
    ))
    plan = {
        "event_expression": {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
        }
    }

    assert "login" in canonical_signal_coverage.covered_families(plan, _catalog())


def test_coverage_is_family_grained_not_span_grained() -> None:
    """알려진 한계를 이름으로 고정한다.

    한 family 안에 절이 여럿인 원문에서 IR 이 그중 하나만 표현해도 family 는 커버로 읽힌다.
    정확한 최종형은 근거 스팬 단위이며, 그때 이 테스트는 **바뀌어야 한다** — 지금 통과한다는 것은
    아직 그 정밀도가 없다는 뜻이지 이 동작이 옳다는 뜻이 아니다.
    """
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="count",
            relation=event_ir.Source(name="cart"),
            expression=event_ir.FieldRef(name="cart.quantity"),
        ),
        right=event_ir.Literal(value=3),
    )
    plan = {"event_expression": {"expression": expression.to_dict(), "source": "audience_requirement"}}
    assert "cart" in canonical_signal_coverage.covered_families(plan, _catalog())
