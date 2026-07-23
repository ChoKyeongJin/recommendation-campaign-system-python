"""명시적 결과 개수 제한(result_limit) → DBMS 방언별 TOP/LIMIT 회귀.

방침: 타겟 오디언스는 기본 '전체 반환'(행수 제한 없음)이지만, 프롬프트가 'N명만/상위 N명/최대 N명/
N명으로 제한'처럼 개수를 명시하면 그 수만큼만 뽑는다. 실제 TOP(MSSQL)/LIMIT(MariaDB) 부착은
sql_guard 가 대상 DBMS 방언에 맞춰 처리하고, 지표 랭킹의 기존 TOP 은 중복 없이 보존한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_result_limit_targeting.py -q
"""

import graph_rag as g
from sql_guard import load_allowed_tables, load_table_dialects, validate_sql


def _safe_sql(query: str) -> tuple[int | None, str]:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None, query
    rl = plan.get("result_limit")
    rl = rl if isinstance(rl, int) and rl > 0 else None
    validation = validate_sql(
        cand["sql"],
        allowed_tables=load_allowed_tables(),
        default_limit=rl,
        table_dialects=load_table_dialects(),
    )
    return plan.get("result_limit"), validation["safe_sql"]


def test_parses_explicit_counts():
    assert g._parse_result_limit("구매 고객 100명만 뽑아줘") == 100
    assert g._parse_result_limit("상위 50명 추출") == 50
    assert g._parse_result_limit("최대 200명") == 200
    assert g._parse_result_limit("300명으로 제한") == 300
    assert g._parse_result_limit("고객 1,500건만") == 1500


def test_ignores_non_limit_numbers():
    # 전체 요청·금액 임계값·기간 창은 개수 제한이 아니다.
    assert g._parse_result_limit("여성 고객 전체 뽑아줘") is None
    assert g._parse_result_limit("누적 구매 금액 5000만원 이상 고객") is None
    assert g._parse_result_limit("3개월 이내 가입한 고객") is None
    assert g._parse_result_limit("최근 30일 미구매 고객") is None


def test_explicit_count_applies_top_mssql():
    rl, sql = _safe_sql("기저귀 구매한 고객 100명만")
    assert rl == 100
    first_line = sql.splitlines()[0]
    assert "TOP 100" in first_line


def test_no_count_stays_unlimited():
    rl, sql = _safe_sql("30대 여성 고객")
    assert rl is None
    assert "TOP" not in sql.upper()


def test_ranking_top_not_doubled():
    rl, sql = _safe_sql("매출 높은 고객 상위 10명")
    assert rl == 10
    # 랭킹 빌더의 TOP 10 하나만 있어야 한다(sql_guard 가 중복 TOP 를 붙이지 않는다).
    assert sql.upper().count("TOP ") == 1
    assert "TOP 10" in sql


def test_response_surfaces_result_limit():
    plan = g.build_query_plan("여성 고객 200명만", parser="rules")
    assert plan.get("result_limit") == 200


def test_recovers_limit_from_original_when_rewrite_drops_particle():
    # 재작성기가 'N명만' → 'N명'으로 조사 '만'을 떼면 재작성본 파싱은 개수를 놓친다.
    rewritten_plan = g.build_query_plan("2024년 하반기 기저귀 구매 고객 100명", parser="rules")
    assert rewritten_plan.get("result_limit") is None
    # retrieve 처럼 원문으로 재적용하면 개수 제한이 복구된다.
    g._apply_result_limit_filter("2024년 하반기에 기저귀 구매한 고객 100명만", rewritten_plan)
    assert rewritten_plan.get("result_limit") == 100
