"""축 행 랭킹('회원 수가 많은 시군구 상위 5개') 문법의 발화·미발화 계약.

이 트랙은 결과 행이 **회원이 아니라 그룹 축**이다. 그래서 두 방향의 사고가 모두 조용하다:
발화해야 하는데 안 하면 질의가 어떤 트랙에도 안 잡혀 미지원으로 강등되고, 발화하면 안 되는데
하면 회원 목록을 요청한 사람이 지역 목록을 받는다(둘 다 SQL 은 그럴듯하게 나온다).

이 파일이 없던 동안 실제로 통과했던 결함들을 케이스로 고정한다 — 기간 표현('3개월')을 행 수로
읽어 요청하지 않은 TOP 3 을 붙이던 것, 무관한 절의 '하위'가 정렬을 뒤집던 것, 축 낱말이 동사
('시도한')·다른 낱말('구별') 안에서 잡히던 것.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import analytical_intent  # noqa: E402


@pytest.fixture(scope="module")
def registry() -> dict:
    return analytical_intent.load_analytics_registry()


def _ranking(query: str, registry: dict) -> dict | None:
    return analytical_intent._dimension_ranking(query, analytical_intent._compact(query), registry)


# (질의, 축, 방향, 행 수 제한) — 제한 None 은 '정렬만, 제한 없음'이다.
FIRES = [
    ("회원 수가 많은 시군구 상위 5개", "sigungu", "DESC", 5),
    ("여성 회원 수가 많은 시군구 상위 5개", "sigungu", "DESC", 5),
    ("회원 수가 적은 시도 3곳", "sido", "ASC", 3),
    ("시도별 회원 수 상위 3개", "sido", "DESC", 3),
    ("20대 여성 회원이 가장 많은 시군구 10곳", "sigungu", "DESC", 10),
    ("회원 수가 많은 동네 5곳", "dong", "DESC", 5),
    # '하위'의 '하' 는 '하다' 활용이 아니다 — 한 글자만 보고 자르면 랭킹의 '아래쪽 절반'이 통째로 죽는다.
    ("회원 수 적은 시도 하위 3개", "sido", "ASC", 3),
    ("회원 수가 많은 시군구 하위 5개", "sigungu", "DESC", 5),
    # 앞 절의 같은 축 낱말이 진짜 축을 가리면 안 된다(첫 등장만 보던 문제).
    ("지역별 매출을 보고 회원 수가 많은 시군구 상위 5개", "sigungu", "DESC", 5),
    # 제한을 요청하지 않았으면 지어내지 않는다.
    ("회원 수가 많은 시군구", "sigungu", "DESC", None),
    # 기간 표현은 행 수가 아니다 — '3개월'의 '3개'를 제한으로 읽으면 조용한 오답이 된다.
    ("최근 3개월간 회원 수가 많은 시군구 상위 10곳", "sigungu", "DESC", 10),
    ("최근 6개월 회원 수가 많은 시군구", "sigungu", "DESC", None),
    # 방향은 순위에 실제로 참여한 표지가 정한다 — 앞 절의 '하위 등급'이 정렬을 뒤집으면 안 된다.
    ("하위 등급 회원 수가 많은 시군구 상위 5개", "sigungu", "DESC", 5),
    # 문장 아무 데나 있는 개수도 행 수가 아니다.
    ("장바구니에 3개 이상 담은 회원 수가 많은 시군구 상위 10곳", "sigungu", "DESC", 10),
]

# 결과 행이 축이 아니어서 다른 트랙이 소유해야 하는 질의들.
YIELDS = [
    "시군구별 회원 수",                              # 순위 요청이 아니다(단순 그룹 집계)
    "구매금액이 높은 동네 상위 5곳에 사는 회원 추출",   # 밀집 지역 거주자 추출(회원 행)
    "카테고리별 구매금액 상위 3개 상품을 산 고객",      # 파생 엔터티 집합(회원 행)
    "회원이 가장 많은 지역의 회원을 추출해줘",          # 축 뒤 회원 명사 = 회원 행
    "가장 많이 거주하는 동네의 회원",                  # 밀집 지역 트랙
    "지역별 매출 높은 회원 100명씩",                  # 그룹별 회원 Top-N(N명)
    "등급을 구별하지 않고 구매금액 상위 3개 알려줘",    # '구별'은 축 낱말이 아니다
    "실적은 시군구별 회원 수",                        # '실적은'의 '적은'은 순위 표지가 아니다
    "구매금액 10만원 이상인 회원",                    # 회원 선택
    "밀집 지역 회원 추출",
    "VIP 고객이 가장 많이 사는 시군구 상위 3곳",       # 거주 표지 = 밀집 지역 트랙(회원 행)
    "20대가 많이 거주하는 시도 5곳",
    "활동별 회원 수",                                 # '동별'이 부분문자열로 걸리면 안 된다
    "행동별 구매금액 합계",
]


@pytest.mark.parametrize("query, dimension, direction, limit", FIRES)
def test_axis_ranking_fires(query: str, dimension: str, direction: str, limit: int | None, registry: dict) -> None:
    result = _ranking(query, registry)
    assert result is not None, f"축 랭킹으로 안 잡힌다: {query!r}"
    assert result["dimension"] == dimension
    assert result["direction"] == direction, "정렬 방향이 뒤집혔다(조용한 의미 반전)"
    assert result.get("limit") == limit, "요청하지 않은 행 수 제한이 붙었거나 요청한 제한이 사라졌다"


@pytest.mark.parametrize("query", YIELDS)
def test_axis_ranking_yields_to_member_row_tracks(query: str, registry: dict) -> None:
    assert _ranking(query, registry) is None, (
        f"이웃 트랙의 질의를 축 랭킹이 가로챘다: {query!r} — 회원 목록을 요청한 사람이 지역 목록을 받는다"
    )


def test_axis_terms_come_from_the_registry(registry: dict) -> None:
    """축 낱말은 소스가 아니라 레지스트리가 소유한다 — 새 축은 JSON 한 줄로 열려야 한다."""
    dimensions = registry.get("dimensions") or {}
    ranked = {key for key, spec in dimensions.items() if isinstance(spec, dict) and spec.get("rankingTerms")}
    assert {"sigungu", "sido", "dong"} <= ranked
    for key in ranked:
        assert dimensions[key].get("field", {}).get("column"), f"{key} 축에 물리 컬럼 선언이 없다"


def test_surface_terms_are_not_hardcoded_in_source() -> None:
    """순위 표지 낱말은 사전(parser_lexicon)이 소유한다(정규식 래칫이 지키는 규약과 같은 축)."""
    for name in ("_DIMENSION_RANK_HIGH_RE", "_DIMENSION_RANK_LOW_RE"):
        pattern = getattr(analytical_intent, name)
        assert hasattr(pattern, "terms"), f"{name} 이 사전 기반 패턴이 아니다(소스 하드코딩)"


def test_blank_axis_rows_are_excluded_from_ranking(registry: dict) -> None:
    """값이 없는 그룹은 '어느 지역인가'에 대한 답이 아니다 — 순위 모집단에서 빠져야 한다."""
    query = "회원 수가 많은 시군구 상위 5개"
    intent = analytical_intent.analyze_analytical_intent(query)
    request = analytical_intent.build_aggregation_request(intent)
    ast = analytical_intent.compile_aggregation_ast(intent, request)
    assert any("IS NOT NULL" in predicate for predicate in ast.where)
    assert any("<> ''" in predicate for predicate in ast.where)
