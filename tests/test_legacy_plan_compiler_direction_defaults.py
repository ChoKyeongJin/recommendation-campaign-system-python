"""랭킹 방향·비교 연산자의 무언 기본값 제거 고정.

두 자리 모두 **인식 실패를 방향으로 덮는** 같은 형태의 결함이었다.

- ``"top" if direction == "descending" else "bottom"`` — 'descending' 이 아닌 **모든** 값이
  하위가 된다. 오타·대소문자·누락이 전부 조용히 정반대 모집단을 만든다.
- ``normalize_or_none(...) or ">="`` — 연산자를 못 읽으면 ``>=`` 로 떨어진다. 사용자가
  '넘지 않는'(≤)이라고 썼는데 ``>=`` 가 되면 모집단이 뒤집힌다.

0건이 나오는 것은 이 프로젝트에서 문제가 아니지만 **정반대 집합이 나오는 것은 문제**다. 그래서
값이 없을 때의 기본값(정보 손실 없음)과 값이 있는데 못 읽는 경우(정보 손실 있음)를 가른다.
"""

from __future__ import annotations

import pytest

import legacy_plan_compiler as lpc


# ── 랭킹 방향 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("descending", "top"),
        ("ascending", "bottom"),
    ],
)
def test_known_directions_map_explicitly(raw: str, expected: str) -> None:
    assert lpc.ranking_direction(raw, high="top", low="bottom") == expected


@pytest.mark.parametrize("raw", ["desc", "DESCENDING", "내림차순", "", None, 0, {}])
def test_unreadable_direction_is_not_silently_flipped_to_the_opposite(raw: object) -> None:
    """읽을 수 없으면 반대 방향으로 떨어지지 않고 ``None`` 을 돌려준다.

    호출자가 그 ``None`` 을 보고 명시적으로 처리한다 — 예전에는 이 자리가 전부 '하위'였다.
    """
    assert lpc.ranking_direction(raw, high="top", low="bottom") is None


def test_direction_mapping_is_reused_by_both_ranking_slots() -> None:
    """회원 랭킹(high/low)과 파생 집합 랭킹(top/bottom)이 같은 판정을 쓴다."""
    assert lpc.ranking_direction("descending", high="high", low="low") == "high"
    assert lpc.ranking_direction("ascending", high="high", low="low") == "low"


# ── 비교 연산자 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, "", {}, []])
def test_absent_operator_falls_back_to_the_declared_default(raw: object) -> None:
    """값이 아예 없으면 기본값을 쓴다 — 이건 정보 손실이 아니다."""
    assert lpc.comparison_operator_or_none(raw, default=">=") == ">="


@pytest.mark.parametrize("raw", ["넘지 않는", "알수없는연산자", "≤≥"])
def test_unreadable_operator_does_not_fall_back_to_a_direction(raw: str) -> None:
    """값이 있는데 못 읽으면 기본값으로 덮지 않는다 — 그 덮기가 극성 반전의 원인이었다."""
    assert lpc.comparison_operator_or_none(raw, default=">=") is None


def test_readable_operator_is_returned_as_is() -> None:
    assert lpc.comparison_operator_or_none(">=", default=">=") == ">="
    assert lpc.comparison_operator_or_none("이하", default=">=") == "<="
