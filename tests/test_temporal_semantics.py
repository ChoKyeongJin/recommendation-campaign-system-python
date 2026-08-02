"""시간 한정어의 범용 연산자 계약.

강제하는 것:
  - 표면 표현('기준'·'내내'·'직전' …)이 **낱말별 분기**가 아니라 범용 연산자로 사상된다.
  - 연산자의 인자 요구가 한 곳(OperatorSpec)에서만 선언된다.
  - 인자 결핍은 MISSING_ARGUMENT 다 — 미지원이 아니다.
  - 어휘는 코어가 아니라 도메인이 주입한다(코어에 낱말이 없다).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import targeting_domain  # noqa: E402
import temporal_semantics as ts  # noqa: E402


# ── 연산자 집합 ──────────────────────────────────────────────────────────────────
def test_operator_set_is_closed_and_documented() -> None:
    documented = ts.operator_documentation()
    assert set(documented) == set(ts.OPERATORS)
    for spec in ts.OPERATOR_SPECS.values():
        assert spec.requires <= ts.ARGUMENTS and spec.accepts <= ts.ARGUMENTS
        assert spec.description, f"{spec.id} 에 설명이 없다"


def test_requested_operators_exist() -> None:
    """요구사항이 이름으로 지목한 세 연산자는 반드시 있어야 한다."""
    for operator in (ts.AS_OF, ts.THROUGHOUT_INTERVAL, ts.IMMEDIATELY_PRECEDING):
        assert operator in ts.OPERATOR_SPECS


def test_unknown_operator_is_a_contract_error() -> None:
    with pytest.raises(ts.TemporalSemanticsError):
        ts.TemporalQualifier(operator="WHENEVER")


# ── 인자 요구 ────────────────────────────────────────────────────────────────────
def test_missing_argument_is_a_deficit_not_unsupported() -> None:
    qualifier = ts.TemporalQualifier(operator=ts.THROUGHOUT_INTERVAL)
    assert qualifier.missing_arguments() == (ts.ARG_INTERVAL,)
    filled = ts.TemporalQualifier(
        operator=ts.THROUGHOUT_INTERVAL, interval={"value": 3, "unit": "months"}
    )
    assert filled.missing_arguments() == ()


def test_anchor_only_operators_do_not_require_an_interval() -> None:
    assert ts.TemporalQualifier(operator=ts.AS_OF).missing_arguments() == ()
    assert ts.TemporalQualifier(operator=ts.IMMEDIATELY_PRECEDING).missing_arguments() == ()
    assert ts.TemporalQualifier(operator=ts.CHANGE_BETWEEN).missing_arguments() == ()


def test_surplus_argument_is_detected() -> None:
    """연산자가 받지 않는 인자가 채워지면 오배선으로 보고한다."""
    qualifier = ts.TemporalQualifier(operator=ts.AS_OF, count=3)
    assert ts.ARG_COUNT in qualifier.surplus_arguments()


def test_change_count_requires_interval_and_count() -> None:
    assert set(ts.TemporalQualifier(operator=ts.CHANGE_COUNT).missing_arguments()) == {
        ts.ARG_INTERVAL, ts.ARG_COUNT
    }


def test_multi_point_operators_are_flagged() -> None:
    """다중 관측 시점 요구는 데이터 그레인 깊이 판정의 근거다."""
    assert ts.THROUGHOUT_INTERVAL in ts.MULTI_POINT_OPERATORS
    assert ts.AS_OF not in ts.MULTI_POINT_OPERATORS


# ── 정규화(별칭 주입) ────────────────────────────────────────────────────────────
def test_domain_relation_names_resolve_through_injected_aliases() -> None:
    aliases = {"held_throughout": ts.THROUGHOUT_INTERVAL}
    assert ts.normalize("held_throughout", aliases=aliases).operator == ts.THROUGHOUT_INTERVAL
    # 별칭이 없으면 모른다 — 코어가 도메인 낱말을 알고 있으면 안 된다.
    assert ts.normalize("held_throughout") is None


def test_generic_operator_id_normalizes_without_aliases() -> None:
    assert ts.normalize(ts.AS_OF).operator == ts.AS_OF
    assert ts.normalize({"operator": ts.NEVER_IN_INTERVAL, "interval": {"value": 6, "unit": "months"}})


def test_unknown_argument_is_rejected() -> None:
    with pytest.raises(ts.TemporalSemanticsError):
        ts.normalize({"operator": ts.AS_OF, "made_up": 1})


def test_alias_to_unknown_operator_fails_loudly() -> None:
    with pytest.raises(ts.TemporalSemanticsError):
        ts.normalize("x", aliases={"x": "NOT_AN_OPERATOR"})


# ── 표면 감지(어휘는 도메인 주입) ────────────────────────────────────────────────
def test_lexicon_rejects_unknown_operators() -> None:
    with pytest.raises(ts.TemporalSemanticsError):
        ts.TemporalLexicon.from_pairs([("NOT_AN_OPERATOR", r"x")])


def test_overlapping_markers_are_deduped() -> None:
    lexicon = ts.TemporalLexicon.from_pairs(
        [(ts.AS_OF, r"기준"), (ts.AS_OF, r"말 기준")]
    )
    markers = lexicon.detect("지난달 말 기준 우수 고객")
    assert len(markers) == 1 and markers[0].text == "말 기준"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("지난달 말 기준 VIP였던 회원을 찾아줘.", ts.AS_OF),
        ("최근 3개월 내내 VIP 등급을 유지한 회원을 찾아줘.", ts.THROUGHOUT_INTERVAL),
        ("최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘.", ts.IMMEDIATELY_PRECEDING),
        ("지난 6개월 동안 골드에서 VIP로 승급한 회원을 찾아줘.", ts.CHANGE_BETWEEN),
        ("최근 3개월 동안 등급이 두 번 이상 변경된 회원을 찾아줘.", ts.CHANGE_COUNT),
        ("한 번이라도 휴면 상태였지만 현재는 정상인 회원을 추출해줘.", ts.AT_LEAST_ONCE_IN_INTERVAL),
        ("2026년 상반기 동안 한 번도 휴면 상태가 아니었던 회원을 추출해줘.", ts.NEVER_IN_INTERVAL),
        ("최근 12개월 동안 등급이 한 번도 바뀌지 않은 회원을 추출해줘.", ts.UNCHANGED_THROUGHOUT),
    ],
)
def test_domain_surface_forms_map_to_generic_operators(query: str, expected: str) -> None:
    """서로 다른 낱말이 같은 축의 **연산자 값**으로 정규화된다(케이스별 분기 없음)."""
    markers = targeting_domain.temporal_lexicon().detect(query)
    assert expected in {marker.operator for marker in markers}, (
        f"{query!r} 에서 {expected} 를 얻지 못했다: {[m.to_dict() for m in markers]}"
    )


def test_non_temporal_clause_produces_no_marker() -> None:
    """시간 축이 아닌 절에서는 아무 마커도 나오지 않는다(과발화 방지)."""
    markers = targeting_domain.temporal_lexicon().detect(
        "최근 30일 장바구니 상품 종류가 2개 이상인 회원"
    )
    assert markers == []


def test_purchase_clause_markers_do_not_fire_on_state_words_alone() -> None:
    """'한 번이라도 구매한' 같은 구매 절이 속성 이력 조건으로 과발화하지 않는다."""
    markers = targeting_domain.temporal_lexicon().detect("한 번이라도 구매한 실버 회원")
    assert not any(marker.operator == ts.AT_LEAST_ONCE_IN_INTERVAL for marker in markers)


def test_core_module_defines_no_surface_patterns() -> None:
    """코어는 표면형 정규식을 **정의하지 않는다** — 패턴은 전부 도메인이 컴파일해 넘긴다.

    `re.compile(<리터럴>)` 이 코어에 생기는 순간 어휘 소유가 둘로 갈린다. 코어가 하는 일은
    받은 패턴을 돌리는 것뿐이므로, 리터럴 컴파일이 하나도 없어야 정상이다.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "temporal_semantics.py").read_text(encoding="utf-8"))
    literal_compiles = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert not literal_compiles, (
        f"코어가 표면형 패턴을 직접 컴파일한다: {literal_compiles}"
    )
