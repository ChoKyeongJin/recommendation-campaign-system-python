"""시간 표현의 소유권 — 하나의 원문 표현이 시간 조건 하나만 만드는지 고정한다.

이 파일이 잡는 회귀는 구체적이다. '7년 전 기저귀를 구매한 여자 고객'에서 ``7년 전`` 하나가
**두 개**의 시간 조건이 됐다 — 절대 창(2019-01-01~2019-12-31)과 롤링 창(최근 2555일). 둘이 AND 로
겹치면 원문에 없는 교집합(2019년 8~12월)이 조회된다. 조건이 사라지는 결함과 달리 SQL 이 그럴듯해
눈에 띄지 않고, 기준일이 흐르면 결과 집합까지 조용히 변한다.

의미 판정(시점/롤링/경계)의 단일 소유자는 :mod:`calendar_window` 다. 그래서 이 스위트는 두 층을
같이 본다: 후보 생성 시점에 종류가 확정되는지(단위), 그리고 그 종류가 플랜·SQL 까지 그대로
전달되는지(파이프라인). 한 층만 보면 소비자가 텍스트를 다시 읽는 회귀를 놓친다.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("CONDITION_SLOT_LLM_FALLBACK", "off")
os.environ.setdefault("SURFACE_LEXICON_LLM", "off")
os.environ.setdefault("TARGET_OBJECT_LLM_FALLBACK", "false")

import calendar_window
import event_ir
import graph_rag
import slot_ownership

# 단위 테스트용 고정 기준일(달력 연산이 실행 날짜에 흔들리지 않게).
FIXED_TODAY = date(2026, 7, 30)
# 파이프라인 경로는 기준일을 인자로 받지 않으므로 기대값을 같은 **규칙**으로 계산한다
# (이웃 스위트 test_event_expression_pipeline 과 같은 관례).
TODAY = date.today()


@pytest.fixture(autouse=True)
def _offline_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 보완 계층을 끈다 — 결정론 경로의 의미 보존만 고정한다(기존 테스트와 같은 관례)."""
    monkeypatch.setattr(graph_rag, "_apply_llm_condition_slot_fallback", lambda *_a, **_kw: None)
    monkeypatch.setenv("TARGET_OBJECT_LLM_FALLBACK", "false")
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "off")
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")


def plan_for(query: str) -> dict:
    return graph_rag.build_query_plan(query, parser="rules")


def sql_for(query: str) -> str:
    plan = plan_for(query)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 1000, original_query=query
    )
    return result.get("sql") or ""


def kinds(text: str) -> list[tuple[str, str, str | None]]:
    """(표면형, 의미 종류, 억제 사유) 목록 — 후보 생성 시점의 판정을 그대로 본다."""
    compact = text.replace(" ", "").casefold()
    return [
        (compact[candidate.start: candidate.end], candidate.kind, candidate.suppressed_by)
        for candidate in calendar_window.duration_window_candidates(compact)
    ]


def event_windows(query: str) -> list[tuple[bool, object]]:
    """사건 IR 이 세운 (부정 여부, 시간 창) 목록. 나열형 복합 문장은 이 층이 소유한다."""
    expression = event_ir.condition_from_dict(plan_for(query)["event_expression"]["expression"])
    return [(view.negated, view.window) for view in event_ir.existence_views(expression)]


def decisions_by(plan: dict, filter_name: str) -> list[dict]:
    return [entry for entry in plan.get("decisions", []) if entry.get("filter") == filter_name]


# ── 의미 종류 판정(단위) ───────────────────────────────────────────────────────────


def test_past_point_expression_is_not_a_rolling_duration() -> None:
    """'7년 전'의 '7년'은 시점의 일부다 — 롤링 기간 후보로 쓰이지 않게 억제 표시가 붙는다."""
    assert kinds("7년 전 기저귀를 구매한 여자 고객") == [("7년", calendar_window.KIND_PAST_POINT, "past_point")]
    assert calendar_window.parse_duration_window(
        "7년 전 기저귀를 구매한 여자 고객", past_point=calendar_window.PAST_POINT_SUPPRESS
    ) is None
    assert calendar_window.parse_relative_past_window("7년 전 기저귀를 구매한 여자 고객", today=FIXED_TODAY) == {
        "from": "20190101", "to": "20191231", "label": "2019년",
        calendar_window.SOURCE_TEMPORAL_KIND_KEY: calendar_window.KIND_PAST_POINT,
    }


def test_boundary_expression_stays_a_rolling_duration() -> None:
    """'3개월 전부터/전까지'는 시점이 아니라 경계다 — 기존 롤링 해석(90일)을 그대로 유지한다."""
    assert kinds("3개월 전부터 구매 없는 고객") == [("3개월", calendar_window.KIND_BOUNDARY_FROM, None)]
    assert kinds("3개월 전까지 구매한 고객") == [("3개월", calendar_window.KIND_BOUNDARY_UNTIL, None)]
    assert kinds("3개월 전 이후 구매한 고객") == [("3개월", calendar_window.KIND_BOUNDARY_FROM, None)]
    for query in ("3개월 전부터 구매 없는 고객", "3개월 전까지 구매한 고객", "3개월 전 이후 구매한 고객"):
        assert calendar_window.parse_duration_window(query)["min_days"] == 90
        assert calendar_window.past_point_spans(query.replace(" ", "")) == []


def test_rolling_expression_is_untouched() -> None:
    assert kinds("최근 7년 구매한 고객") == [("7년", calendar_window.KIND_ROLLING, None)]
    assert calendar_window.parse_duration_window("최근 7년 구매한 고객")["min_days"] == 2555


def test_exclude_past_is_decided_by_kind_not_by_the_character_전() -> None:
    """호출자 정책(exclude_past)도 문자 검사가 아니라 종류로 판정한다 — 결과는 기존과 동일하다."""
    assert calendar_window.parse_duration_window("3개월 전부터 로그인 안 한 고객", exclude_past=True) is None
    assert calendar_window.parse_duration_window("7년 전 구매한 고객", exclude_past=True) is None
    assert calendar_window.parse_duration_window(
        "최근 3개월 로그인 안 한 고객", exclude_past=True
    )["min_days"] == 90


def test_same_number_in_two_expressions_keeps_both() -> None:
    """가장 위험한 형태: 숫자가 같고 구간만 다르다. 텍스트·값 매칭으로 새면 여기서 터진다."""
    assert kinds("7년 전 구매하고 최근 7년 재구매한 고객") == [
        ("7년", calendar_window.KIND_PAST_POINT, "past_point"),
        ("7년", calendar_window.KIND_ROLLING, None),
    ]
    # 억제 정책 아래에서는 롤링 쪽 후보만 남으므로 창은 뒤쪽 '최근 7년'의 것이다(값이 아니라 구간으로 구분된다).
    window = calendar_window.parse_duration_window(
        "7년 전 구매하고 최근 7년 재구매한 고객", past_point=calendar_window.PAST_POINT_SUPPRESS
    )
    assert window["min_days"] == 2555
    assert window[calendar_window.SOURCE_SPAN_KEY] == (9, 11)


def test_word_form_past_point_keeps_current_behavior() -> None:
    """단어형('일주일 전')은 아직 시점으로 읽지 않는다 — 이 변경 단위의 범위 밖(현행 동작 고정).

    숫자형과 달리 :data:`calendar_window.RELATIVE_PAST_PATTERN` 이 ``\\d+`` 를 요구하므로 시점 구간이
    잡히지 않고 롤링 7일로 남는다. 같은 버그 클래스가 단어형에 남아 있다는 사실을 여기서 못 박는다."""
    assert kinds("일주일 전 구매한 고객") == [("일주일", calendar_window.KIND_ROLLING, None)]
    assert calendar_window.parse_duration_window("일주일 전 구매한 고객")["min_days"] == 7
    assert calendar_window.past_point_spans("일주일전구매한고객") == []


def test_past_point_policy_is_owned_by_the_consuming_slot() -> None:
    """억제는 그 시점을 표현할 **다른 소유자가 있는 도메인**에서만 옳다.

    구매는 절대 창 슬롯(purchase_date)이 있어 시점이 그쪽으로 간다. 가입·집계·휴면은 아직 그 소유자가
    없어 억제하면 조건이 통째로 사라지므로(더 큰 결함) 현행 롤링 해석을 유지한다 — 그래서 기본 정책은
    :data:`calendar_window.PAST_POINT_AS_DURATION` 이고, 억제는 호출자가 명시한다."""
    query = "1년 전에 가입한 고객"
    assert calendar_window.parse_duration_window(query, anchor_terms=("가입", "등록"))["min_days"] == 365
    assert calendar_window.parse_duration_window(
        query, anchor_terms=("가입", "등록"), past_point=calendar_window.PAST_POINT_SUPPRESS
    ) is None
    # 그 정책이 플랜까지 유지되는지(가입 조건이 조용히 사라지지 않는지) 함께 못 박는다.
    assert plan_for(query)["target_user"]["signup_target"] == {"days": 365}


def test_adjacent_expressions_do_not_inherit_each_others_kind() -> None:
    """포함(containment) 판정이라 인접한 별개 표현은 서로의 종류를 물려받지 않는다."""
    assert kinds("3년 전 가입한 고객 중 최근 1년 구매한 고객") == [
        ("3년", calendar_window.KIND_PAST_POINT, "past_point"),
        ("1년", calendar_window.KIND_ROLLING, None),
    ]
    assert slot_ownership.span_contains((0, 3), (0, 2)) is True
    assert slot_ownership.span_contains((0, 2), (0, 3)) is False
    assert slot_ownership.span_contains((0, 3), (2, 5)) is False  # 겹치기만 하면 포함이 아니다


def test_suppression_is_idempotent() -> None:
    """억제 로직을 두 번 적용해도 결과가 같다(멱등)."""
    compact = "7년전구매하고최근7년재구매한고객"
    once = calendar_window.duration_window_candidates(compact)
    assert once == calendar_window.duration_window_candidates(compact)
    past = [(match.span(), kind) for match, kind in calendar_window._past_expressions(compact)]
    assert [calendar_window._classified_duration_candidate(c, past) for c in once] == once


# ── 플랜·SQL 로의 전달(파이프라인) ─────────────────────────────────────────────────


def test_past_point_purchase_makes_exactly_one_time_condition() -> None:
    """케이스 1 — 절대 창 하나만 남고 롤링 창은 생기지 않는다(상품·성별 조건은 그대로)."""
    query = "7년 전 기저귀를 구매한 여자 고객"
    plan = plan_for(query)
    target_user = plan["target_user"]
    year = TODAY.year - 7

    assert target_user["purchase_date"]["from"] == f"{year}0101"
    assert target_user["purchase_date"]["to"] == f"{year}1231"
    assert target_user["purchase_membership"].get("window_days") is None
    assert target_user.get("purchase_inactivity") is None
    assert target_user["purchase_object"] == "기저귀"
    assert target_user["gender"] == "female"

    sql = sql_for(query)
    assert f"BETWEEN '{year}0101' AND '{year}1231'" in sql
    assert "DATEADD(DAY, -2555" not in sql
    assert "기저귀" in sql
    assert "GENDER_CD.FEMALE" in sql


def test_past_point_suppression_leaves_a_trace() -> None:
    """억제는 결정이다 — 감사 로그에 사유와 근거가 남는다."""
    plan = plan_for("7년 전 기저귀를 구매한 여자 고객")
    suppressed = decisions_by(plan, "duration_window")
    assert suppressed, "억제된 기간 후보가 감사 로그에 남아야 한다"
    assert suppressed[0]["action"] == graph_rag.plan_decisions.DROP
    assert "과거 시점" in suppressed[0]["reason"]
    assert suppressed[0]["value"]["kind"] == calendar_window.KIND_PAST_POINT


def test_windowless_purchase_membership_is_absorbed_by_the_date_window() -> None:
    """창 없는 구매 존재는 구매일 창에 흡수돼 중복 EXISTS 를 내지 않는다(의미 조건은 보존)."""
    query = "7년 전 기저귀를 구매한 여자 고객"
    plan = plan_for(query)
    assert plan["target_user"]["purchase_membership"]["satisfied_by"] == "purchase_date"
    assert decisions_by(plan, "purchase_membership_absorption")
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)" not in sql_for(query)


def test_windowed_purchase_membership_is_not_absorbed() -> None:
    """창 있는 구매 존재는 절대 창이 대체할 수 없다 — 흡수하지 않고 자기 술어를 낸다."""
    plan = plan_for("최근 7년 구매한 고객")
    membership = plan["target_user"]["purchase_membership"]
    assert membership["window_days"] == 2555
    assert membership.get("satisfied_by") is None
    assert "DATEADD(DAY, -2555" in sql_for("최근 7년 구매한 고객")


def test_boundary_purchase_absence_keeps_the_90_day_anti_join() -> None:
    """케이스 2 — '3개월 전부터 구매 없는'의 90일 anti-join 이 유지된다."""
    query = "3개월 전부터 구매 없는 고객"
    plan = plan_for(query)
    inactivity = plan["target_user"]["purchase_inactivity"]
    assert inactivity["min_days"] == 90
    assert inactivity[calendar_window.SOURCE_TEMPORAL_KIND_KEY] == calendar_window.KIND_BOUNDARY_FROM
    assert plan["target_user"].get("purchase_date") is None
    sql = sql_for(query)
    assert "NOT EXISTS" in sql and "DATEADD(DAY, -90" in sql


def test_rolling_purchase_window_survives() -> None:
    """케이스 3 — '최근 7년'은 롤링 2555일로 남고 절대 연도로 바뀌지 않는다."""
    plan = plan_for("최근 7년 구매한 고객")
    assert plan["target_user"]["purchase_membership"]["window_days"] == 2555
    assert plan["target_user"].get("purchase_date") is None
    sql = sql_for("최근 7년 구매한 고객")
    assert "DATEADD(DAY, -2555" in sql
    assert "BETWEEN" not in sql


def test_weeks_ago_becomes_that_calendar_week() -> None:
    """케이스 4 — '2주 전'은 그 주(월~일) 절대 구간이고 롤링 14일이 아니다."""
    query = "2주 전 주문한 고객"
    target = TODAY - timedelta(days=14)
    monday = target - timedelta(days=target.weekday())
    sunday = monday + timedelta(days=6)
    plan = plan_for(query)

    assert plan["target_user"]["purchase_date"]["from"] == monday.strftime("%Y%m%d")
    assert plan["target_user"]["purchase_date"]["to"] == sunday.strftime("%Y%m%d")
    assert plan["target_user"]["purchase_membership"].get("window_days") is None
    sql = sql_for(query)
    assert f"BETWEEN '{monday:%Y%m%d}' AND '{sunday:%Y%m%d}'" in sql
    assert "DATEADD(DAY, -14" not in sql


def test_past_point_without_space_reads_the_same() -> None:
    """케이스 9 — 띄어쓰기 없는 '7년전'도 같은 절대 창이고, 상품명이 '전 기저귀'로 새지 않는다."""
    plan = plan_for("7년전 기저귀를 구매한 여자 고객")
    year = TODAY.year - 7
    assert plan["target_user"]["purchase_date"]["from"] == f"{year}0101"
    assert plan["target_user"]["purchase_membership"].get("window_days") is None
    assert plan["target_user"]["purchase_object"] == "기저귀"


def test_extra_spacing_does_not_shift_the_suppression() -> None:
    """케이스 10 — 공백이 많은 문장(compact 좌표계가 원문과 크게 어긋나는 경우).

    억제 판정은 공백 제거 좌표계에서, 감사 로그의 근거는 원문 좌표계에서 나온다. 두 좌표계가 어긋나면
    예외가 아니라 **조용한 오작동**(억제 미적용 또는 멀쩡한 창 소실)이 되므로 근거 텍스트까지 본다."""
    query = "7년  전    기저귀를   구매한   여자   고객"
    plan = plan_for(query)
    year = TODAY.year - 7

    assert plan["target_user"]["purchase_date"]["from"] == f"{year}0101"
    assert plan["target_user"]["purchase_membership"].get("window_days") is None
    assert plan["target_user"]["gender"] == "female"
    assert decisions_by(plan, "duration_window")[0]["evidence"] == "7년"


def test_boundary_until_purchase_keeps_current_rolling_reading() -> None:
    """케이스 8 — '7년 전까지'는 경계 표현이라 기존 롤링 해석을 유지한다(현행 동작 고정)."""
    plan = plan_for("7년 전까지 구매한 고객")
    assert plan["target_user"]["purchase_membership"]["window_days"] == 2555
    assert plan["target_user"].get("purchase_date") is None


def test_plan_is_deterministic_across_repeated_builds() -> None:
    """케이스 11 — 같은 입력을 두 번 계획해도 같다(억제·흡수·감사 모두 멱등)."""
    import ir_snapshot

    query = "7년 전 기저귀를 구매한 여자 고객"
    first, second = plan_for(query), plan_for(query)
    assert ir_snapshot.snapshot(first) == ir_snapshot.snapshot(second)

    absorbed = plan_for(query)
    graph_rag._absorb_windowless_purchase_membership(query, absorbed)
    graph_rag._audit_time_span_ownership(query, absorbed)
    assert ir_snapshot.snapshot(absorbed) == ir_snapshot.snapshot(first)
    assert len(decisions_by(absorbed, "purchase_membership_absorption")) == 1


# ── 복합 문장: 사건 IR 이 소유하는 층 ──────────────────────────────────────────────
# 나열형 복합 문장은 절 단위 사건 IR 이 실행 모델이 된다(레거시 슬롯은 회수된다). 시점/롤링 구분은
# 그 층에서도 같은 문법(calendar_window)에서 나오므로, 같은 불변식을 그 표기로 확인한다.


def test_two_time_expressions_make_two_independent_conditions() -> None:
    """케이스 5 — '1년 전 구매' + '최근 3개월 미구매': 서로 다른 구간, 서로 다른 극성."""
    windows = event_windows("1년 전 구매하고 최근 3개월 구매 없는 고객")
    assert len(windows) == 2
    assert windows[0] == (False, event_ir.RelativeWindow(value=1, unit="year"))
    assert windows[1] == (True, event_ir.RollingWindow(value=90, unit="day"))


def test_rolling_and_past_point_across_domains_both_survive() -> None:
    """케이스 6 — 구매는 롤링 1년, 가입은 '3년 전' 시점. 두 조건 모두 생존한다."""
    windows = event_windows("최근 1년 구매한 고객 중 3년 전 가입한 고객")
    assert (False, event_ir.RollingWindow(value=365, unit="day")) in windows
    assert len(windows) == 2


def test_same_number_two_spans_both_survive_in_the_pipeline() -> None:
    """케이스 7 — '7년 전 구매' + '최근 7년 재구매': 숫자가 같아도 두 조건이 모두 남는다."""
    windows = event_windows("7년 전 구매하고 최근 7년 재구매한 고객")
    assert windows == [
        (False, event_ir.RelativeWindow(value=7, unit="year")),
        (False, event_ir.RollingWindow(value=2555, unit="day")),
    ]


# ── 중복 소유권 감사(경고 모드) ────────────────────────────────────────────────────


def _plan_with(source_query: str, target_user: dict) -> dict:
    return {"intent": "find_user_segment", "target_user": target_user, "exclude": {}, "campaign_constraints": {}}


def test_audit_warns_when_one_expression_owns_two_time_conditions() -> None:
    """감사가 실제로 문제 형태를 잡는지 — 수정 전 모양(같은 어구, 시간 조건 2개)을 직접 세워 본다."""
    query = "7년 전 기저귀를 구매한 여자 고객"
    plan = _plan_with(query, {
        "purchase_date": {"from": "20190101", "to": "20191231", "label": "2019년 구매"},
        "purchase_membership": {
            "domain": "purchase", "operator": "exists", "window_days": 2555,
            calendar_window.SOURCE_SPAN_KEY: (0, 2),
            calendar_window.SOURCE_TEMPORAL_KIND_KEY: calendar_window.KIND_ROLLING,
        },
    })
    slot_ownership.record_slot_span(plan, "purchase_date", (0, 5), source_text=query, container="target_user")

    graph_rag._audit_time_span_ownership(query, plan)

    warnings = decisions_by(plan, "time_span_ownership")
    assert len(warnings) == 1
    assert warnings[0]["action"] == graph_rag.plan_decisions.KEEP  # 경고만 — 드롭하지 않는다
    assert sorted(warnings[0]["value"]) == ["target_user.purchase_date", "target_user.purchase_membership"]
    # 조건은 그대로 살아 있다(1차 도입은 관찰만 한다).
    assert plan["target_user"]["purchase_membership"]["window_days"] == 2555
    assert plan["target_user"]["purchase_date"]["from"] == "20190101"


def test_audit_is_silent_for_two_distinct_expressions() -> None:
    """구간이 다르면 시간 조건이 둘이어도 정상이다 — 개수가 아니라 구간으로 비교한다."""
    query = "1년 전 구매하고 최근 3개월 구매 없는 고객"
    plan = _plan_with(query, {
        "purchase_date": {"from": "20250101", "to": "20251231", "label": "2025년 구매"},
        "purchase_inactivity": {
            "value": 3, "unit": "months", "min_days": 90,
            calendar_window.SOURCE_SPAN_KEY: (8, 11),
            calendar_window.SOURCE_TEMPORAL_KIND_KEY: calendar_window.KIND_ROLLING,
        },
    })
    slot_ownership.record_slot_span(plan, "purchase_date", (0, 4), source_text=query, container="target_user")

    graph_rag._audit_time_span_ownership(query, plan)

    assert decisions_by(plan, "time_span_ownership") == []


def test_audit_ignores_conditions_whose_ownership_moved() -> None:
    """소유권이 이전된 조건(satisfied_by)은 독립 조건이 아니다 — 흡수 결과가 경고로 되돌아오지 않는다."""
    query = "7년 전 기저귀를 구매한 여자 고객"
    plan = _plan_with(query, {
        "purchase_date": {"from": "20190101", "to": "20191231", "label": "2019년 구매"},
        "purchase_membership": {"domain": "purchase", "operator": "exists", "satisfied_by": "purchase_date"},
    })
    slot_ownership.record_slot_span(plan, "purchase_date", (0, 5), source_text=query, container="target_user")

    graph_rag._audit_time_span_ownership(query, plan)

    assert decisions_by(plan, "time_span_ownership") == []
