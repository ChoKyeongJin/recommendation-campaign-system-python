"""집합식 operand → SQL 컴파일 특성화 — B-2(_compile_set_operand 레지스트리화) 리팩터 전 안전망.

배경: graph_rag._compile_set_operand 는 operand 의 canonical 을 GENDER→LIFECYCLE→price_sensitive→
premium_buyer→INTEREST→BEHAVIOR→CHANNEL→coupon→grade→region 순의 긴 if/elif 로 SQL 술어에 매핑한다.
레지스트리가 없어 새 operand 종류마다 분기를 추가해야 한다. B-2 는 이를 operand recognizer 스펙 리스트로
바꾸는 것이 목표다.

**순서가 의미상 하중을 진다**: cart_abandoner·repeat_buyer 는 LIFECYCLE_TERMS 와 BEHAVIOR_TERMS 에 동시에
있는데 LIFECYCLE 분기가 먼저라 'u.lifecycle = ...' 로 컴파일된다(behavior EXISTS 가 아니다). 순진한
레지스트리가 우선순위를 바꾸면 이 두 canonical 의 SQL 이 뒤집힌다. 아래 테스트가 그 순서를 못박는다.

기존 test_set_expression_dimension.py 는 등급/지역 디멘션 분기만 덮는다. 이 파일은 **모든 값-레벨 term-set
분기를 전수** 고정해 레지스트리 전환의 동작 불변성을 보장한다.

실행: python -m pytest tests/test_set_operand_compilation_characterization.py -q
"""

import pytest

import graph_rag as g


def _sql(canonical: str) -> str:
    result = g._compile_set_operand({"type": "operand", "canonical": canonical})
    assert result["is_valid"], f"{canonical}: 컴파일 실패 {result['issues']}"
    return result["expression_sql"]


# 우선순위 순 family 목록. 각 family 의 '고유 canonical'(앞선 family 에 없는 것)만 그 family 공식으로
# 검증한다 — 중복 canonical(cart_abandoner/repeat_buyer)은 아래 순서 가드가 따로 못박는다.
def _priority_sets():
    return [
        ("gender", g.GENDER_TERMS),
        ("lifecycle", g.LIFECYCLE_TERMS),
        ("interest", g.INTEREST_TERMS),
        ("behavior", g.BEHAVIOR_TERMS),
        ("channel", g.CHANNEL_TERMS),
    ]


def _owned(family_terms, higher_priority):
    return sorted(set(family_terms) - set().union(*higher_priority) if higher_priority else set(family_terms))


def test_gender_terms_compile_to_gender_equality():
    for c in sorted(g.GENDER_TERMS):
        assert _sql(c) == f"u.gender = '{c}'"


def test_lifecycle_terms_compile_to_lifecycle_equality():
    # gender 에 없는 lifecycle canonical 은 전부 u.lifecycle 등식(값 그대로).
    for c in _owned(g.LIFECYCLE_TERMS, [g.GENDER_TERMS]):
        assert _sql(c) == f"u.lifecycle = '{c}'"


def test_interest_terms_compile_to_user_interests_exists():
    for c in _owned(g.INTEREST_TERMS, [g.GENDER_TERMS, g.LIFECYCLE_TERMS]):
        assert _sql(c) == (
            "EXISTS (SELECT 1 FROM user_interests ui_set "
            f"WHERE ui_set.user_id = u.user_id AND ui_set.interest = '{c}')"
        )


def test_behavior_terms_compile_to_recent_behaviors_exists():
    # lifecycle 에 없는(= behavior 가 실제로 소유하는) canonical 만. cart_abandoner/repeat_buyer 는
    # lifecycle 이 가로채므로 여기서 제외되고, 아래 순서 가드가 별도로 검증한다.
    for c in _owned(g.BEHAVIOR_TERMS, [g.GENDER_TERMS, g.LIFECYCLE_TERMS, g.INTEREST_TERMS]):
        assert _sql(c) == (
            "EXISTS (SELECT 1 FROM user_recent_behaviors urb_set "
            f"WHERE urb_set.user_id = u.user_id AND urb_set.behavior = '{c}')"
        )


def test_channel_terms_compile_to_preferred_channels_exists():
    higher = [g.GENDER_TERMS, g.LIFECYCLE_TERMS, g.INTEREST_TERMS, g.BEHAVIOR_TERMS]
    for c in _owned(g.CHANNEL_TERMS, higher):
        assert _sql(c) == (
            "EXISTS (SELECT 1 FROM user_preferred_channels upc_set "
            f"WHERE upc_set.user_id = u.user_id AND upc_set.preferred_channel = '{c}')"
        )


# ── 순서 하중(lifecycle ∩ behavior 우선순위) 가드 — 리팩터가 절대 바꾸면 안 되는 계약 ──────────
@pytest.mark.parametrize("canonical", ["cart_abandoner", "repeat_buyer"])
def test_lifecycle_wins_over_behavior_for_shared_canonicals(canonical):
    # 두 canonical 은 lifecycle·behavior 양쪽에 있으나 lifecycle 분기가 먼저 → lifecycle 등식으로 해석된다.
    assert canonical in g.LIFECYCLE_TERMS and canonical in g.BEHAVIOR_TERMS, "전제: 두 term-set 에 공존"
    assert _sql(canonical) == f"u.lifecycle = '{canonical}'"


# ── 상수 매핑 분기(단일 canonical → 고정 SQL) ─────────────────────────────────────────────
def test_constant_mapping_branches():
    assert _sql("price_sensitive") == "u.price_sensitivity = 'high'"
    assert _sql("premium_buyer") == "u.predicted_ltv_segment = 'high'"
    assert _sql("coupon") == "u.price_sensitivity = 'high'"


# ── 미인식 canonical → 컴파일 불가(하드 실패 이슈) ─────────────────────────────────────────
def test_unknown_canonical_is_invalid():
    result = g._compile_set_operand({"type": "operand", "canonical": "__not_a_real_canonical__"})
    assert not result["is_valid"]
    assert "컴파일할 수 없는 피연산자" in "; ".join(result["issues"])
