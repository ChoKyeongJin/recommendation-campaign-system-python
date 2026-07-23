"""RAG 스키마 검색어 정제(_schema_retrieval_query) 회귀 테스트.

배경: RAG 검색은 스키마(테이블·컬럼 의미)를 찾는 역할인데, 사용자 문장을 그대로 임베딩/키워드 검색해
날짜/숫자/기간/개수 리터럴이 검색 품질을 떨어뜨렸다. 날짜·숫자 등은 이미 결정론 추출기가 구조화 필터
(purchase_date/result_limit/aggregate_conditions 등)로 뽑아 SQL 조건이 되므로, 검색어에서는 값 토큰을
제거하고 '스키마 의미'만 남긴다(제안 #2). purchase_date 자체는 별개로 SQL 조건에 그대로 반영된다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_schema_retrieval_query.py -q
"""

import graph_rag as g


# --- 값 토큰 판정 ---

def test_value_tokens_are_dropped():
    for token in ["2019년", "2월", "15일", "2024-03", "20240301",
                  "10명", "100만원", "3천만원", "3개월", "90일간", "5건",
                  "100만원이상", "100", "2024"]:
        assert g._is_schema_query_value_token(token), f"{token!r} 는 값 토큰이어야 한다"


def test_semantic_tokens_are_kept():
    for token in ["구매한", "고객", "조회", "서울", "VIP", "여성",
                  "20대", "2030세대", "일요일", "이상"]:
        assert not g._is_schema_query_value_token(token), f"{token!r} 는 의미 토큰이어야 한다(유지)"


# --- 문장 정제 ---

def test_strips_date_from_purchase_lookup():
    assert g._schema_retrieval_query("2019년 2월 구매한 고객 조회") == "구매한 고객 조회"


def test_strips_amount_and_window():
    # 값(90일·100만원)은 빠지고 스키마 의미어만 남는다('최근/누적/구매/금액/고객' 유지).
    out = g._schema_retrieval_query("최근 90일 누적 구매 금액 100만원 이상 고객")
    assert "90일" not in out and "100만원" not in out
    assert "구매" in out and "금액" in out and "고객" in out


def test_strips_top_n_count():
    assert g._schema_retrieval_query("구매 많은 고객 10명") == "구매 많은 고객"


def test_value_only_query_is_preserved():
    # 값만 있는 질의는 빈 검색어가 되지 않게 원문을 유지한다.
    assert g._schema_retrieval_query("2019년 2월") == "2019년 2월"


def test_non_string_input_is_safe():
    assert g._schema_retrieval_query("") == ""


# --- plan 연동: 날짜는 검색어에서 빠져도 SQL 조건으로는 살아있다 ---

def test_date_removed_from_search_but_kept_as_sql_filter():
    plan = g.build_query_plan("2019년 2월 구매한 고객 조회")
    # 검색어에선 날짜 제거
    assert "2019년" not in g._schema_retrieval_query(plan["retrieval"]["query"])
    # 그러나 구조화 필터엔 그대로
    assert plan["target_user"]["purchase_date"]["from"] == "20190201"
    assert plan["target_user"]["purchase_date"]["to"] == "20190228"
