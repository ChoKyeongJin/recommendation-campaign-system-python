"""폐기된 축이 SQL 을 내지 않는다는 고정 계약.

2026-08-05 이행에서 오디언스 IR 은 canonical Event IR 하나가 됐고, SemanticPlanV2 노드로만
실행되던 세 축이 함께 폐기됐다. 그중 **둘은 되살아났고 하나는 폐기 상태 그대로다** —
되살아난 근거는 "옛 코드를 복원했다"가 아니라 "canonical Event IR 이 그 뜻을 표현하게 됐다"다.

* 축1 등급 이력·전이(월 스냅샷 as_of / previous / transition / 전칭) — **복귀**(2026-08-05).
  마지막까지 남았던 선행 조건은 "의무에 컴파일 영수증을 발급하는 경로"였고, 원문 →
  canonical Temporal IR 생산자(:mod:`temporal_claims`)가 그 경로를 만들었다. 값·축 어휘는
  카탈로그가, 연산자→조합 사상은 선언표가 소유하므로 문형별 분기는 늘지 않는다.
  **상태(정상/휴면) 축은 그대로 닫혀 있다** — 전이 지표도 이력 소스도 선언이 없다.
* 축2 프로필 스칼라 지표(구매주기 등) — **복귀**. 카탈로그에 회원별 스칼라 metric kind
  (``member_scalar``)를 선언하고 :mod:`member_scalar_metrics` 가 기존 IR 노드 조합으로 낮춘다.
* 축3 캠페인당 평균 구매금액 — **복귀**. Event IR 에 행 값(tuple)과 0 값 가드(null_if)가
  생겨 ``SUM / COUNT(DISTINCT (CAMP_ID, CAMP_EXEC_NO))`` 를 구조로 표현할 수 있게 됐다.

복귀한 두 축에서 고정하는 것은 **뜻이다**: 축3 은 여전히 ``AVG(BUY_AMT)`` 로 나가면 안 되고
(그것은 반응 행당 평균이라 값이 다르다), 축2 는 여전히 최신 월 스냅샷 한 행만 읽어야 한다.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import campaign_metric_claims  # noqa: E402
import execution_assets  # noqa: E402
import graph_rag  # noqa: E402
import targeting_domain  # noqa: E402
import temporal_semantics  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-04"

CAMPAIGN_AVERAGE_QUERY = "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"
BUY_CYCLE_QUERY = "구매주기가 30일 이하인 회원"
SNAPSHOT_QUERY = "이번 달 기준 골드 등급 회원"
TRANSITION_QUERY = "골드에서 VIP로 바뀐 회원"
BARE_RECENT_QUERY = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"


def _raw(
    query: str,
    *,
    expression: dict | None = None,
    issues: list[dict] | None = None,
) -> dict:
    """LLM 도구 계약 그대로의 원본 payload(4필드)."""

    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": expression,
            "issues": list(issues or []),
        },
    }


def _evidence(query: str, span: str) -> dict:
    start = query.index(span)
    return {"text": span, "start": start, "end": start + len(span)}


def _unsupported_issue(query: str, span: str, *, argument: str, message: str) -> dict:
    return {
        "code": "unsupported_semantics",
        "argument": argument,
        "message": message,
        "evidence": _evidence(query, span),
    }


def _campaign_average_expression(query: str, *, empty_filter: bool) -> dict:
    """모델이 실제로 내는 모양: 금액 필드를 **행 단위**로 평균 내는 비교."""

    source: dict = {"type": "source", "name": "campaign_purchase_response"}
    return {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "avg",
            "relation": (
                {"type": "filter", "relation": source, "where": None}
                if empty_filter
                else source
            ),
            "expression": {
                "type": "field",
                "name": "campaign_purchase_response.amount",
            },
            "distinct": False,
        },
        "right": {"type": "literal", "value": 100000},
        "evidence": _evidence(query, "10만 원 이상"),
    }


def _structure(query: str, raw: dict) -> dict:
    return attach_campaign_query_plan_v4_identity(
        copy.deepcopy(raw), query, current_date=CURRENT_DATE
    )


def _sql_result(query: str, structured: dict) -> tuple[dict, dict]:
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=query,
    )
    return plan, result


def test_monthly_snapshot_grade_comparison_never_reaches_sql() -> None:
    """축1-a: 월 스냅샷 필드 비교는 승격 없이 미지원으로 닫힌다."""

    structured = _structure(
        SNAPSHOT_QUERY,
        _raw(
            SNAPSHOT_QUERY,
            expression={
                "type": "comparison",
                "operator": "=",
                "left": {"type": "field", "name": "member_month_snapshot.grade"},
                "right": {"type": "literal", "value": "gold_grade"},
                "evidence": _evidence(SNAPSHOT_QUERY, "골드 등급"),
            },
        ),
    )

    assert structured["semantic_ir"]["status"] == "unsupported"
    assert structured.get("event_expression") is None
    _plan, result = _sql_result(SNAPSHOT_QUERY, structured)
    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "semantic_ir_unsupported"


def test_grade_transition_request_compiles_both_halves_into_one_row_comparison() -> None:
    """축1-b **복귀**: 등급 전이는 한 행의 현재값·직전값 비교로 낮아진다.

    2026-08-05 이행 시점에는 이 축이 폐기 상태였고 이 테스트는 '어떤 SQL 도 나가지 않는다'를
    고정했다. 복귀의 근거는 옛 코드의 복원이 아니라 **원문 → canonical Temporal IR 생산자**
    (:mod:`temporal_claims`)가 생겨 의무에 컴파일 영수증을 발급할 수 있게 된 것이다.

    고정하는 것은 여전히 '부분 SQL 부재'다. 뒤집힌 것은 결말이 아니라 전제다 — 예전에는
    두 절 중 하나(전이)를 아무도 컴파일하지 못해 문장 전체가 막혔고, 이제는 그 절이
    **두 비교를 한 EXISTS 안에** 담아 나간다. 한쪽만 나가는 SQL 은 여전히 실패다.
    """

    structured = _structure(
        TRANSITION_QUERY,
        _raw(
            TRANSITION_QUERY,
            issues=[
                _unsupported_issue(
                    TRANSITION_QUERY,
                    "골드에서 VIP로 바뀐",
                    argument="grade_transition",
                    message="등급 전이 조건을 canonical Event IR로 표현할 수 없습니다.",
                )
            ],
        ),
    )

    # 모델의 미지원 신고를 애플리케이션이 반박했다 — 표현이 생기고 신고는 해소된다.
    assert structured["semantic_ir"]["status"] == "resolved"
    assert structured["audience_requirement"]["issues"] == []
    assert structured["audience_requirement"]["expression"] is not None

    _plan, result = _sql_result(TRANSITION_QUERY, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    # 두 비교가 **같은 EXISTS 안에** 있어야 한다. 나뉘면 '지금 VIP' AND '언젠가 직전이 골드'가
    # 되어 전이가 아닌 다른 집합이 나간다.
    assert "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql
    assert "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in sql
    assert sql.count("EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO") == 1


def test_a_direction_contradicting_the_declared_ranking_still_reaches_no_sql() -> None:
    """복귀가 모순 문장까지 열지는 않는다 — '승급'이 서열과 어긋나면 SQL 은 없다.

    이 계약이 없으면 축을 여는 순간 'VIP에서 골드로 승급'이 그대로 컴파일돼, 문장의 모순이
    조용히 사라진 채 사용자가 말하지 않은 집합이 나간다(실측된 회귀 후보).
    """

    query = "VIP에서 골드로 승급한 회원"
    structured = _structure(
        query,
        _raw(
            query,
            issues=[
                _unsupported_issue(
                    query,
                    "VIP에서 골드로 승급",
                    argument="grade_transition",
                    message="등급 전이 조건을 표현할 수 없습니다.",
                )
            ],
        ),
    )

    assert structured["semantic_ir"]["status"] == "unsupported"
    _plan, result = _sql_result(query, structured)
    assert result["is_success"] is False
    assert not result["sql"]


def test_profile_scalar_metric_request_compiles_to_the_snapshot_row() -> None:
    """축2 복귀: 구매주기 임계가 최신 월 스냅샷 **한 행**을 읽는 SQL 로 나간다.

    고정하는 것은 SQL 문자열이 아니라 뜻이다 — 회원 상관, 최신 월 고정, 그 회원의 값과 임계값의
    비교. 스냅샷 고정이 빠지면 같은 회원의 과거 월 행이 조건을 만족시켜 대상이 부풀고, 그것은
    오류가 아니라 조용히 다른 오디언스다.
    """

    structured = _structure(
        BUY_CYCLE_QUERY,
        _raw(
            BUY_CYCLE_QUERY,
            issues=[
                _unsupported_issue(
                    BUY_CYCLE_QUERY,
                    "구매주기",
                    argument="purchase_cycle",
                    message=(
                        "The requested purchase cycle condition cannot be represented "
                        "in the canonical Event IR."
                    ),
                )
            ],
        ),
    )

    expression = structured["audience_requirement"]["expression"]
    assert expression is not None
    assert structured["audience_requirement"]["issues"] == []
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(BUY_CYCLE_QUERY, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    assert "MS.BUY_CYCLE <= 30" in sql
    assert "MS.MEMBER_NO = B.MEMBER_NO" in sql
    assert "MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in sql


def _assert_campaign_denominator_average(sql: str) -> None:
    """캠페인 분모 평균의 **뜻** 가드 — 물리 모양과 무관하게 잰다.

    같은 뜻이 두 물리 모양으로 나온다. 회원 상관 스칼라(``ISNULL(SUM(R.BUY_AMT), 0)`` +
    ``SELECT DISTINCT`` 서브쿼리)와, 그것을 낮춘 2단 집계 semi-join(안쪽 ``SUM(R.BUY_AMT)``
    부분합 + 안쪽 ``GROUP BY`` 키 + 바깥 ``ISNULL(SUM(EPn), 0)`` / ``COUNT(*)``)이다. 어느 쪽이든
    **분자는 행당 평균이 아닌 금액 합계**이고 **분모는 서로 다른 (CAMP_ID, CAMP_EXEC_NO) 조합의
    수**여야 한다. 정확한 문자열 golden 은 tests/test_composite_aggregate_lowering.py 가 두 모양
    모두에 대해 따로 고정한다.
    """

    # 분자: 반응 행당 평균이 아니라 금액 합계다(F5 회귀 가드).
    assert "AVG(R.BUY_AMT)" not in sql
    assert "SUM(R.BUY_AMT)" in sql
    # 행이 없을 때 SUM 이 NULL 이 되어 조건이 조용히 거짓이 되지 않도록 0 으로 접는다.
    assert re.search(r"ISNULL\(SUM\((?:R\.BUY_AMT|EP\d+)\), 0\)", sql), sql
    # 분모: (CAMP_ID, CAMP_EXEC_NO) 두 컬럼의 조합이 하나의 키다. 두 컬럼이 `,` 로 나란히
    # 오는 자리는 DISTINCT 투영과 GROUP BY 키 목록뿐이다(조인 ON 절은 `AND` 로 잇는다).
    assert re.search(
        r"(?:SELECT DISTINCT|GROUP BY)[^\n]*?R\.CAMP_ID, R\.CAMP_EXEC_NO", sql
    ), sql
    # 0 분모 가드(반응이 없는 회원은 NULL 비교로 빠진다).
    assert "NULLIF(" in sql
    # 정수 나눗셈 방지.
    assert "* 1.0" in sql
    # 구분자 결합 키는 값 안의 ':' 나 NULL 로 서로 다른 키가 충돌할 수 있어 쓰지 않는다.
    assert "CONCAT(R.CAMP_ID" not in sql
    # 네이티브 다중 컬럼 distinct 는 NULL 규칙이 방언마다 달라 쓰지 않는다.
    assert "COUNT(DISTINCT R.CAMP_ID, R.CAMP_EXEC_NO)" not in sql


@pytest.mark.parametrize("empty_filter", [True, False])
def test_campaign_average_amount_never_degrades_into_a_row_average(
    empty_filter: bool,
) -> None:
    """축3 복귀: 캠페인 분모 평균 요청이 행당 평균 SQL 로 **바뀌지 않은 채** 실행된다.

    모델이 내는 것은 여전히 ``Aggregate(avg)``(행당 평균)이다. 카탈로그 선언이 그 자리를
    분자 SUM / 분모 서로 다른 캠페인 실행 수로 바꾼다 — 원래의 F5 회귀 가드(행당 평균 금지)는
    그대로이고, 결말만 fail-close 에서 재작성으로 바뀌었다.
    """

    structured = _structure(
        CAMPAIGN_AVERAGE_QUERY,
        _raw(
            CAMPAIGN_AVERAGE_QUERY,
            expression=_campaign_average_expression(
                CAMPAIGN_AVERAGE_QUERY, empty_filter=empty_filter
            ),
        ),
    )

    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert requirement["expression"] is not None
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(CAMPAIGN_AVERAGE_QUERY, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    # F5 회귀 가드: 캠페인 수로 나눈 평균 대신 반응 행당 평균이 나가면 뜻이 다르다.
    _assert_campaign_denominator_average(sql)


# 실측(2026-08-05)으로 가드를 통째로 우회했던 표면 변형들. 예전 판정은 grain → 지표어 →
# 평균이 **그 순서로 인접**해야 성립해서 7종 중 1종만 닫혔다. 뜻이 같은데 자리·조사·띄어쓰기만
# 다른 문장이 뚫리면 그 가드는 없는 것과 같으므로, 변형별로 고정한다.
CAMPAIGN_AVERAGE_SURFACE_VARIANTS = (
    ("aggregation_before_metric", "캠페인별 평균 구매반응 금액이 10만 원 이상인 회원"),
    ("grain_with_particle", "캠페인별로 구매반응 금액이 평균 10만 원 이상인 회원"),
    ("grain_with_space", "캠페인 별 구매반응 금액이 평균 10만 원 이상인 회원"),
    ("per_campaign_grain", "캠페인당 평균 구매반응 금액이 10만 원 이상인 회원"),
    ("modifier_between", "캠페인별 구매반응 금액이 대략 평균 10만 원 이상인 회원"),
    # 아래 둘은 위 재현에서 어떤 기존 선언으로도 걸리지 않아 카탈로그 grain_terms 에
    # **선언으로** 추가한 표면어다(코드에 낱말을 심지 않는다).
    ("campaign_unit_grain", "캠페인 단위 평균 구매반응 금액이 10만 원 이상인 회원"),
    ("each_campaign_grain", "각 캠페인 평균 구매반응 금액이 10만 원 이상인 회원"),
)


@pytest.mark.parametrize(
    "query",
    [query for _name, query in CAMPAIGN_AVERAGE_SURFACE_VARIANTS],
    ids=[name for name, _query in CAMPAIGN_AVERAGE_SURFACE_VARIANTS],
)
def test_campaign_average_guard_holds_across_surface_variants(query: str) -> None:
    """어순·조사·띄어쓰기·수식어가 달라져도 같은 뜻이면 같이 재작성된다."""

    structured = _structure(
        query,
        _raw(query, expression=_campaign_average_expression(query, empty_filter=False)),
    )

    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert requirement["expression"] is not None
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(query, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    _assert_campaign_denominator_average(sql)


def _grouping_ambiguity(query: str, span: str = "캠페인별") -> dict:
    """모델이 실제로 내는 신고(실측 2026-08-06): 표현 없이 '캠페인별'을 모호로 신고한다."""

    return {
        "code": "ambiguous_requirement",
        "argument": "grouping",
        "message": (
            "'캠페인별'의 의미(어떤 집계 수준으로 평균을 계산할지)가 불명확합니다: "
            "캠페인별로 각각 평균이 10만 원 이상을 요구하는지, 전체 캠페인에 걸친 평균을 "
            "요구하는지 명확하지 않습니다."
        ),
        "evidence": _evidence(query, span),
    }


def test_campaign_average_ambiguity_is_answered_by_the_declaration() -> None:
    """축3 두 번째 갈래: 모델이 표현을 비우고 모호를 신고해도 선언이 그 뜻을 확정한다.

    실측(2026-08-06)에서 이 문장은 ``expression=null`` + ``ambiguous_requirement(grouping)``
    으로 돌아와 되묻기로 닫혔다. 그런데 그 모호는 카탈로그가 이미 하나로 풀어 놓은 것이고
    (분자 SUM / 분모 서로 다른 캠페인 실행 수), 사용자 확인도 같은 뜻이었다 — 회원이 반응한
    캠페인들에 대한 평균. 그래서 애플리케이션이 같은 선언으로 술어를 세우고 신고를 해소한다.

    고정하는 것은 여전히 **뜻**이다. 세운 식이 행당 평균으로 나가면 값이 다르므로, 재작성
    경로와 똑같은 분자·분모·0 분모 가드를 여기서도 잰다.
    """

    structured = _structure(
        CAMPAIGN_AVERAGE_QUERY,
        _raw(CAMPAIGN_AVERAGE_QUERY, issues=[_grouping_ambiguity(CAMPAIGN_AVERAGE_QUERY)]),
    )

    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert requirement["expression"] is not None
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(CAMPAIGN_AVERAGE_QUERY, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    _assert_campaign_denominator_average(sql)


def test_campaign_average_ambiguity_stays_closed_when_a_clause_is_left_over() -> None:
    """선언 합성은 **문장 전체**를 세운다 — 절이 하나 더 있으면 세우지 않고 신고가 남는다.

    이 계약이 없으면 '여성'이 조용히 사라진 SQL 이 성공으로 나간다(모델은 표현을 내지 않았고
    합성기는 그 절을 모른다).
    """

    query = "캠페인별 구매반응 금액이 평균 10만 원 이상인 여성 회원"
    structured = _structure(query, _raw(query, issues=[_grouping_ambiguity(query)]))

    assert structured["audience_requirement"]["expression"] is None
    assert [item["code"] for item in structured["audience_requirement"]["issues"]] == [
        "ambiguous_requirement"
    ]
    assert structured["semantic_ir"]["status"] == "needs_clarification"
    _plan, result = _sql_result(query, structured)
    assert result["is_success"] is False
    assert not result["sql"]


@pytest.mark.parametrize(
    "query",
    [
        "구매반응 금액이 평균 10만 원 이상인 회원",
        "평균 구매반응 금액이 10만 원 이상인 회원",
    ],
)
def test_campaign_average_guard_does_not_block_a_row_average_request(query: str) -> None:
    """가드는 카탈로그가 선언한 grain 표면어가 있는 문장에서만 닫는다(전면 차단이 아니다).

    grain 어가 없는 '구매반응 금액이 평균 …'은 폐기 축이 아니라 **행당 평균** 요청이므로 그대로
    ``AVG(R.BUY_AMT)`` 로 컴파일돼야 한다 — 여기까지 막으면 가드가 아니라 기능 축소다.
    """

    structured = _structure(
        query,
        _raw(query, expression=_campaign_average_expression(query, empty_filter=False)),
    )

    assert structured["audience_requirement"]["issues"] == []
    _plan, result = _sql_result(query, structured)
    assert result["is_success"] is True, result.get("failure_reason")
    assert "AVG(R.BUY_AMT)" in result["sql"]


def test_campaign_average_guard_fails_closed_when_the_catalog_cannot_be_read() -> None:
    """판정 근거(카탈로그)를 못 읽는 것은 '폐기 축이 아니다'가 아니다.

    예전 호출부는 :class:`audience_runtime.AudienceCatalogLoadError` 를 삼키고 '해당 없음'으로
    돌려줘서, 근거 부재가 곧 통과가 됐다(실측: 주입 시 행당 평균 SQL 출고). 이제는 전파한다 —
    SQL 이 나가지 않는다는 것이 고정 대상이다.
    """

    import audience_runtime  # noqa: PLC0415 — 주입 대상 모듈

    original = audience_runtime.catalog_snapshot

    def _broken(*_args: object, **_kwargs: object) -> dict:
        raise audience_runtime.AudienceCatalogLoadError("injected catalog load failure")

    audience_runtime.catalog_snapshot = _broken  # type: ignore[assignment]
    try:
        with pytest.raises(audience_runtime.AudienceCatalogLoadError):
            _structure(
                CAMPAIGN_AVERAGE_QUERY,
                _raw(
                    CAMPAIGN_AVERAGE_QUERY,
                    expression=_campaign_average_expression(
                        CAMPAIGN_AVERAGE_QUERY, empty_filter=False
                    ),
                ),
            )
    finally:
        audience_runtime.catalog_snapshot = original  # type: ignore[assignment]


def test_still_closed_axis_issues_preserve_exact_original_evidence() -> None:
    """닫혀 있는 축의 issue 는 원문 구간을 손실 없이 보존한다(원문 삭제·근사 금지).

    예시를 등급 전이에서 **상태 전이**로 옮겼다. 등급 축은 2026-08-05 이후 컴파일되므로
    더 이상 issue 를 남기지 않고(그 근거 보존은 합성된 표현의 evidence 가 담당한다),
    상태 축은 전이 지표도 이력 소스도 선언이 없어 여전히 닫혀 있다 — 고정하려는 계약
    ('닫힌 축은 원문을 손실 없이 남긴다')이 살아 있는 자리가 그쪽이다.
    """

    span = "정상에서 휴면으로 바뀐"
    structured = _structure(
        STATE_TRANSITION_QUERY,
        _raw(
            STATE_TRANSITION_QUERY,
            issues=[
                _unsupported_issue(
                    STATE_TRANSITION_QUERY,
                    span,
                    argument="state_transition",
                    message="상태 전이 조건을 표현할 수 없습니다.",
                )
            ],
        ),
    )
    issues = structured["audience_requirement"]["issues"]

    assert issues
    for issue in issues:
        evidence = issue["evidence"]
        assert (
            STATE_TRANSITION_QUERY[evidence["start"] : evidence["end"]]
            == evidence["text"]
        )
        assert evidence["text"] in STATE_TRANSITION_QUERY
        assert issue["message"].strip()


@pytest.mark.parametrize(
    ("query", "raw_factory"),
    [
        (
            CAMPAIGN_AVERAGE_QUERY,
            lambda query: _raw(
                query,
                expression=_campaign_average_expression(query, empty_filter=False),
            ),
        ),
        (
            BUY_CYCLE_QUERY,
            lambda query: _raw(
                query,
                issues=[
                    _unsupported_issue(
                        query,
                        "구매주기",
                        argument="purchase_cycle",
                        message="구매주기 조건을 표현할 수 없습니다.",
                    )
                ],
            ),
        ),
    ],
)
def test_revived_axis_expressions_preserve_exact_original_evidence(
    query: str, raw_factory
) -> None:
    """복귀한 축의 표현은 원문 구간을 정확히 가리킨다(합성이 근거를 지어내지 않는다)."""

    import event_ir  # noqa: PLC0415 — 계약 확인용 지역 import

    structured = _structure(query, raw_factory(query))
    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []

    expression = event_ir.condition_from_dict(requirement["expression"])
    evidences = [
        atom.evidence
        for atom, _negated in event_ir.iter_signed_atoms(expression)
        if atom.evidence is not None
    ]
    assert evidences
    for evidence in evidences:
        assert query[evidence.start : evidence.end] == evidence.text


STATE_TRANSITION_QUERY = "여성이면서 정상에서 휴면으로 바뀐 회원"


def test_current_state_asset_never_rebuts_an_unsupported_state_history_claim() -> None:
    """축1 이관 계약: 현재값 자산('휴면' 필터)이 이력 미지원 신고를 반박하지 않는다.

    `tests/test_live_transition_semantics.py`(2026-08-05 삭제) 에서 옮겨 왔다.
    반박을 허용하면 '휴면이 된
    회원'이 '지금 휴면인 회원'으로 조용히 바뀐다(실측된 의미 반전). 축이 폐기된 뒤에도
    같은 문장이 현재값 자산으로 되살아나면 안 되므로 계약은 그대로 남는다.
    """

    span = "정상에서 휴면으로 바뀐"
    issue = _unsupported_issue(
        STATE_TRANSITION_QUERY,
        span,
        argument="grade_transition_values",
        message="model-authored issue",
    )

    # 현재값 자산 자체는 여전히 선언돼 있다(그 낱말이 원문에 있으므로 걸린다).
    assert any(
        asset.symbol in {"dormant", "inactivity_period"}
        for asset in execution_assets.non_canonical_assets_for_text(span)
    )
    # 그러나 **이력 신고**에는 그 자산이 답이 될 수 없다.
    assert execution_assets.assets_compatible_with_issue(
        issue, query=STATE_TRANSITION_QUERY
    ) == ()

    structured = _structure(
        STATE_TRANSITION_QUERY, _raw(STATE_TRANSITION_QUERY, issues=[issue])
    )
    assert structured["semantic_ir"]["status"] == "unsupported"
    assert structured.get("audience_execution_assets") is None
    _plan, result = _sql_result(STATE_TRANSITION_QUERY, structured)
    assert result["is_success"] is False
    assert not result.get("sql")


def test_snapshot_as_of_bridge_never_leaks_into_the_shared_temporal_lexicon() -> None:
    """축1 이관 계약: 폐기 축의 as-of 다리가 공용 시간 어휘를 오염시키지 않는다.

    `tests/test_live_history_coverage.py`(2026-08-05 삭제) 에서 옮겨 왔다.
    '이번 달 기준'을 공용 어휘의
    as-of 마커로 올리면 스냅샷과 무관한 문장(구매금액·상품 조건)까지 시점 한정으로 읽힌다.
    """

    query = "이번 달 기준 구매금액이 10만원 이상인 VIP 상품 구매 회원"

    assert not any(
        marker.operator == temporal_semantics.AS_OF
        for marker in targeting_domain.temporal_lexicon().detect(query)
    )


def test_retired_axis_slots_and_plan_keys_have_no_definitions_left() -> None:
    """축1 의 슬롯·플랜 키·실패 사유가 선언에서 사라졌다(생산자 없는 광고 금지)."""

    import plan_schema  # noqa: PLC0415 — 계약 확인용 지역 import
    import targeting_ir  # noqa: PLC0415
    from rag import failure_stage  # noqa: PLC0415

    assert "relational_operation" not in targeting_ir.SLOT_SHAPES
    assert all(spec.kind != "relational_operation" for spec in targeting_ir.CONDITION_SPECS)
    for key in ("relational_operations", "relational_ir"):
        assert plan_schema.kind_of(key) is None, f"폐기된 플랜 키가 남아 있다: {key}"
    for reason in ("relational_ir_unsupported", "relational_ir_needs_clarification"):
        assert reason not in failure_stage._FAILURE_REASON_TO_STAGE


def test_bare_recent_campaign_count_without_a_default_window_remains_blocked() -> None:
    """맨 '최근'에 기본 창을 지어내지 않는다(폐기 축과 무관한 정책 래칫).

    `docs/data/test_baselines/live_prompts.json` 78번 항목이 이 이름을 인용한다.
    """

    raw = _raw(
        BARE_RECENT_QUERY,
        issues=[
            {
                "code": "missing_argument",
                "argument": "period",
                "message": (
                    "The term '최근' requires a specific period to define the time frame "
                    "for the audience segment and no duration was provided."
                ),
                "evidence": {"text": "최근", "start": 0, "end": 2},
            }
        ],
    )
    structured = _structure(BARE_RECENT_QUERY, raw)

    assert [binding["kind"] for binding in structured["literal_bindings"]] == [
        "number_with_unit",
        "comparison_operator",
    ]
    assert structured.get("event_expression") is None
    assert structured["semantic_ir"]["status"] == "needs_clarification"

    _plan, result = _sql_result(BARE_RECENT_QUERY, structured)
    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "semantic_ir_needs_clarification"


# 축2 의 **라이브 갈래**. 위 `test_profile_scalar_metric_request_compiles_to_the_snapshot_row`
# 는 모델이 표현을 비우고 미지원을 신고한 합성 갈래만 고정한다. 실측(2026-08-06)에서 모델이
# 같은 조건을 스스로 표현하면 결말이 달랐다 — '구매주기가 30일 이하'의 `30일` 이 **소실된
# 기간 창**으로 세어져 `validation_mismatch[period]` 로 반려됐다. `30일` 자체에는 고정된 뜻이
# 없고 무엇인지는 최종 표현의 구조가 정하므로, 두 갈래를 따로 고정한다.
MIXED_SCALAR_AND_WINDOW_QUERY = "구매주기가 30일 이하이고 최근 30일 이내 구매한 회원"


def _member_scalar_threshold_expression(query: str) -> dict:
    """원문 + 지표 레지스트리 선언 + 리터럴 원장에서 회원별 스칼라 임계 표현을 역산한다.

    지표 id·표면어·임계값·비교 기호를 이 파일에 손으로 심지 않는다. 그것들은 카탈로그와
    회원 지표 레지스트리가 소유하는 지식이고, 선언이 바뀌면 픽스처도 함께 따라가야 한다 —
    손으로 적어 두면 선언과 어긋난 모양을 라이브 계약으로 고정하게 된다.
    """

    import audience_runtime  # noqa: PLC0415 — 선언 소유자
    import event_ir  # noqa: PLC0415
    import member_scalar_metric_claims  # noqa: PLC0415
    import member_scalar_metrics  # noqa: PLC0415
    from query_structurer.semantic_ir import (  # noqa: PLC0415
        extract_literal_bindings,
    )

    registry = audience_runtime.member_metric_registry_snapshot()
    assert registry is not None, "회원 지표 레지스트리 선언이 없다"
    declarations = member_scalar_metric_claims._declared_metrics(registry)
    candidates = member_scalar_metric_claims._alias_candidates(query, declarations)
    assert len(candidates) == 1, candidates
    declaration, _alias, alias_start, _alias_end = candidates[0]

    bindings = extract_literal_bindings(query, current_date=CURRENT_DATE)
    operators = [row for row in bindings if row.get("kind") == "comparison_operator"]
    assert len(operators) == 1, bindings
    thresholds = []
    for row in bindings:
        unit_and_value = member_scalar_metric_claims._threshold_unit_and_value(row)
        if unit_and_value is not None and unit_and_value[0] == declaration["threshold_unit"]:
            thresholds.append(unit_and_value[1])
    assert thresholds, bindings

    operator_end = int(operators[0]["end"])
    predicate = member_scalar_metrics.lower_member_scalar_metric(
        audience_runtime.resolve_audience_catalog(),
        declaration["catalog_metric_id"],
        operator=str(operators[0]["normalized"]),
        value=thresholds[0],
        evidence=event_ir.Evidence(
            text=query[alias_start:operator_end],
            start=alias_start,
            end=operator_end,
        ),
    )
    return predicate.expression.to_dict()


def _rolling_purchase_expression(query: str, span: str) -> dict:
    """사용자가 **실제로 말한** 최근 30일 구매 창 — 스칼라 임계와 구별되는 진짜 시간 조건."""

    return {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": "purchase"},
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": "purchase.occurred_at"},
                "window": {"type": "rolling", "value": 30, "unit": "day"},
            },
        },
        "evidence": _evidence(query, span),
    }


def test_a_model_authored_scalar_threshold_is_not_read_as_a_lost_time_window() -> None:
    """축2 라이브 갈래: 모델이 스스로 낸 스칼라 임계 표현이 그대로 스냅샷 SQL 로 나간다.

    합성 갈래에서만 초록이면 래칫이 라이브를 지키지 못한다 — 합성은 모델이 표현을 비웠을
    때만 돌고, 그 부산물(소비한 리터럴 구간)이 없으면 같은 문장이 기간 검증에서 반려됐다.
    고정하는 것은 SQL 문자열이 아니라 뜻이다: 최신 월 스냅샷 한 행, 그 회원의 값과 임계값의
    비교, 그리고 **사용자가 말하지 않은 시간 창이 붙지 않았다**는 것.
    """

    query = BUY_CYCLE_QUERY
    structured = _structure(
        query, _raw(query, expression=_member_scalar_threshold_expression(query))
    )

    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert requirement["expression"] is not None
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(query, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    assert "MS.BUY_CYCLE <= 30" in sql
    assert "MS.MEMBER_NO = B.MEMBER_NO" in sql
    assert "MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in sql
    # '30일' 을 기간으로 되읽어 구매 이력 창을 덧붙이면 조용히 다른 오디언스가 된다.
    assert "CRM_SL_ORDERHEADERMALL" not in sql
    assert "DATEADD(" not in sql


def test_a_scalar_threshold_beside_a_real_window_keeps_both_clauses_in_one_sql() -> None:
    """혼합문: 임계값 ``30일`` 을 소비하고도 뒤의 **진짜 창이 살아서** 한 SQL 에 함께 나간다.

    같은 문장에 값·단위가 완전히 같은 ``30일`` 이 둘 있다. 앞의 것은 지표 임계값이고 뒤의
    것은 시간 창이며, 리터럴 원장에서 둘은 구별되지 않는다. 소비 구간을 하나도 청구하지
    않으면 이 옳은 요청이 기간 불일치로 반려되므로(실측), 그 반려가 돌아오지 않는 것을
    여기서 고정한다.

    반대 방향(청구가 진짜 창까지 삼키는 과도 소비)은 **이 층에서는 보이지 않는다** — 표현은
    이미 주어져 있고 마스킹은 검증기만 건드리므로 SQL 이 달라지지 않는다. 그 계약은 소비
    구간 자체를 재는 청구 함수의 단위 테스트가 소유한다(기간 검증기는 창의 개수만 비교해
    과도한 소비를 스스로 검출하지 못한다).
    """

    query = MIXED_SCALAR_AND_WINDOW_QUERY
    structured = _structure(
        query,
        _raw(
            query,
            expression={
                "type": "and",
                "operands": [
                    _member_scalar_threshold_expression(query),
                    _rolling_purchase_expression(query, "최근 30일 이내 구매한"),
                ],
            },
        ),
    )

    requirement = structured["audience_requirement"]
    assert requirement["issues"] == []
    assert structured["semantic_ir"]["status"] == "resolved"

    _plan, result = _sql_result(query, structured)
    sql = result["sql"] or ""
    assert result["is_success"] is True, result.get("failure_reason")
    # 스칼라 절: 최신 월 스냅샷 한 행 비교.
    assert "MS.BUY_CYCLE <= 30" in sql
    assert "MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in sql
    assert sql.count("EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO") == 1
    # 시간 절: 사용자가 말한 최근 30일 구매 창이 그대로 남아 있다.
    assert "CRM_SL_ORDERHEADERMALL" in sql
    assert "DATEADD(DAY, -30, GETDATE())" in sql
