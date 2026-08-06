"""미지원 신고 반박의 세 번째 축 — **애플리케이션이 계산한 의무의 원문 구간**.

앞의 두 축(카탈로그 심볼·선언된 capability)은 모델이 argument/message 에 적은 **이름**에
걸린다. 이름을 다르게 적으면 옳은 반박이 통째로 빠진다 — 실측(2026-08-07)의
``argument='ranked_set_cardinality'`` 가 그 경우였고, 그 결과 재시도 없이 '표현할 수
없습니다'로 종결됐다. 그 조합은 canonical Event IR 이 이미 컴파일한다
(``tests/test_ranked_set_cardinality.py``).

세 번째 축은 이름을 보지 않고 좌표를 본다. 그래서 이 파일이 지키는 것은 둘이다:

  1. 지원되는 의무의 구간을 '표현 불가'로 지목하면 반박된다(재시도).
  2. **감지만 되고 낼 표현이 없는 의무**(주기 반복 등)는 반박하지 않는다 — 그것까지 뒤집으면
     옳은 미지원 신고를 재시도만 반복하게 만든다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import canonical_audience_claims  # noqa: E402
import semantic_requirements  # noqa: E402
from query_structurer import structurer  # noqa: E402

RANKED_QUERY = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
RECURRENCE_QUERY = "매월 3회 이상 구매한 회원"


def _payload(query: str, *, argument: str, message: str, span: str) -> tuple[dict, dict]:
    start = query.index(span)
    issue = {
        "code": "unsupported_semantics",
        "argument": argument,
        "message": message,
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    }
    raw = {"audience_requirement": {"expression": None, "issues": [issue]}}
    enriched = {"original_query": query, "semantic_ir": {}}
    return raw, enriched


def test_supported_obligation_span_refutes_the_unsupported_claim() -> None:
    raw, enriched = _payload(
        RANKED_QUERY,
        argument="ranked_set_cardinality",
        message="랭킹 파생 집합과 회원별 교집합 개수 임계는 지원하지 않습니다.",
        span="가장 많이 팔린 상품 5개 중 2개 이상 구매",
    )

    error = structurer._audience_repair_error(raw, enriched)

    assert error is not None, "지원되는 의무 구간의 미지원 신고가 종결됐다"
    assert "ranked_entity_set" in error
    assert "emit the canonical expression" in error


def test_non_overlapping_span_does_not_fire_this_axis() -> None:
    """구간이 겹치지 않으면 다른 절의 신고다 — 의무가 있다는 이유로 반박하지 않는다."""
    raw, enriched = _payload(
        RANKED_QUERY,
        argument="member_channel_preference",
        message="채널 선호 조건은 표현할 수 없습니다.",
        span="구매한 고객",
    )

    assert structurer._audience_repair_error(raw, enriched) is None


def test_unsupported_obligation_kind_does_not_refute() -> None:
    """감지는 됐지만 낼 표현이 없는 의무는 반박 근거가 아니다."""
    kinds = {
        semantic_requirements.obligation_kind(requirement)
        for requirement in semantic_requirements.capture_source_semantic_obligations(
            RECURRENCE_QUERY
        )
    }
    assert "temporal_recurrence" in kinds, "이 테스트는 의무가 실제로 잡혀야 성립한다"
    assert not kinds & canonical_audience_claims.CANONICAL_COMPILED_OBLIGATION_KINDS

    raw, enriched = _payload(
        RECURRENCE_QUERY,
        argument="calendar_month_recurrence",
        message="주기별 반복 조건은 표현할 수 없습니다.",
        span="매월",
    )

    assert structurer._audience_repair_error(raw, enriched) is None


def test_existing_catalog_symbol_axis_still_refutes() -> None:
    query = "여성 회원을 찾아줘"
    raw, enriched = _payload(
        query,
        argument="subject.gender",
        message="The compiler cannot represent a direct profile field comparison.",
        span="여성",
    )

    error = structurer._audience_repair_error(raw, enriched)

    assert error is not None
    assert "subject.gender" in error


def test_existing_capability_axis_still_refutes() -> None:
    query = "장바구니에 서로 다른 상품을 3개 이상 담아둔 회원"
    raw, enriched = _payload(
        query,
        argument="distinct_products",
        message="중복 제거한 상품 수(count distinct)는 표현할 수 없습니다.",
        span="서로 다른 상품을 3개 이상",
    )

    error = structurer._audience_repair_error(raw, enriched)

    assert error is not None
    assert "aggregate.count_distinct" in error


def test_missing_evidence_span_keeps_previous_behaviour() -> None:
    """좌표가 없으면 겹침을 판정할 수 없다 — 기존 동작(반박 없음)을 유지한다."""
    raw = {
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "unsupported_semantics",
                "argument": "ranked_set_cardinality",
                "message": "지원하지 않습니다.",
                "evidence": None,
            }],
        }
    }
    enriched = {"original_query": RANKED_QUERY, "semantic_ir": {}}

    assert structurer._audience_repair_error(raw, enriched) is None
