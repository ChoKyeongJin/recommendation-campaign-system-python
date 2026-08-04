"""퍼센트포인트를 퍼센트로 삼키지 않는다.

``비중 차이가 10%포인트 이내`` 에서 ``포인트`` 를 버리면 ``10%`` 라는 **다른 뜻**의 리터럴이
남는다. 이건 결과가 0건이 되는 문제가 아니라 조건의 의미가 바뀌는 문제다 — 비중의 차이(%p)와
비중 자체(%)는 서로 다른 값이다.

현재 이 시스템에는 퍼센트포인트를 받을 지표(채널 비중·할인율 차이)가 없다. 그래서 여기서 하는
일은 지원을 늘리는 것이 아니라 **틀린 값으로 조용히 바꾸지 않는 것**이다. 지표가 생기면 그때
percentage_point 리터럴을 별도 종류로 추가하면 되고, 이 테스트가 그 자리를 지켜 준다.
"""

from __future__ import annotations

import pytest

from query_structurer import semantic_ir


def _percent_spans(text: str) -> list[str]:
    return [match.group(0) for match in semantic_ir._PERCENT_RE.finditer(text)]


def _percentage_literals(text: str) -> list[dict]:
    bindings = semantic_ir.extract_literal_bindings(text)
    return [binding for binding in bindings if binding.get("kind") == "percentage"]


@pytest.mark.parametrize(
    "text",
    [
        "비중 차이가 10%포인트 이내인 회원",
        "할인율 차이가 20%포인트 이상인 회원",
        "20퍼센트포인트 이상 벌어진 회원",
        "격차가 5%p 이상인 회원",
    ],
)
def test_percentage_point_is_not_captured_as_a_percentage(text: str) -> None:
    assert _percent_spans(text) == [], text
    assert _percentage_literals(text) == [], text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("할인율이 20% 이상인 회원", "20%"),
        ("할인율이 20퍼센트 이상인 회원", "20퍼센트"),
        ("할인율이 20프로 이상인 회원", "20프로"),
        ("상위 10% 회원", "10%"),
    ],
)
def test_plain_percentages_still_bind(text: str, expected: str) -> None:
    """반대 방향 가드 — 퍼센트포인트를 막느라 멀쩡한 퍼센트까지 잃으면 안 된다."""
    assert _percent_spans(text) == [expected], text
    assert len(_percentage_literals(text)) == 1, text
