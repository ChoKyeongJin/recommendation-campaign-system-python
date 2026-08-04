from __future__ import annotations

from datetime import UTC, datetime

import pytest

import api
import graph_rag
from query_structurer import StructuringContext


_FIXED_CONTEXT = StructuringContext(
    current_date="2026-08-04",
    timezone="Asia/Seoul",
    current_datetime="2026-08-04T09:00:00+09:00",
)
_FIXED_30_DAY_CUTOFF = "20260705"


def _build_member_sql_result(
    target_user: dict,
    *,
    context: StructuringContext | None,
) -> dict:
    plan = {
        "intent": "find_user_segment",
        "target_user": target_user,
        "exclude": {},
        "campaign_constraints": {},
    }
    return graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        "회원 날짜 조건 테스트",
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query="회원 날짜 조건 테스트",
        structuring_context=context,
    )


def test_api_captures_the_reference_date_in_the_declared_timezone(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_RAG_TIMEZONE", "Asia/Seoul")

    context = api._request_structuring_context(
        now=datetime(2026, 8, 3, 16, 30, tzinfo=UTC)
    )

    assert context.current_date == "2026-08-04"
    assert context.timezone == "Asia/Seoul"
    assert context.current_datetime == "2026-08-04T01:30:00+09:00"


def test_api_rejects_a_naive_injected_instant(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_RAG_TIMEZONE", "Asia/Seoul")

    with pytest.raises(ValueError, match="timezone-aware"):
        api._request_structuring_context(now=datetime(2026, 8, 4, 1, 30))


def test_date_only_context_is_rejected_at_the_execution_boundary() -> None:
    context = StructuringContext(
        current_date="2026-08-04",
        timezone="Asia/Seoul",
    )

    with pytest.raises(ValueError, match="current_datetime is required"):
        graph_rag._structuring_reference_now(context)


def test_timezone_is_required_at_the_execution_boundary() -> None:
    context = StructuringContext(
        current_date="2026-08-04",
        current_datetime="2026-08-04T00:00:00+09:00",
    )

    with pytest.raises(ValueError, match="timezone is required"):
        graph_rag._structuring_reference_now(context)


def test_reference_date_must_match_the_instant_in_the_declared_timezone() -> None:
    context = StructuringContext(
        current_date="2026-08-03",
        timezone="Asia/Seoul",
        current_datetime="2026-08-03T16:30:00+00:00",
    )

    with pytest.raises(ValueError, match="must match current_datetime"):
        graph_rag._structuring_reference_now(context)


def test_retrieve_requires_the_request_context() -> None:
    with pytest.raises(ValueError, match="explicit structuring_context"):
        graph_rag.retrieve(
            query="test",
            graph=graph_rag.nx.Graph(),
            collection="test",
            url="http://localhost",
            api_key=None,
            embedding_model_name="test",
            vector_top_k=1,
            keyword_top_k=1,
            graph_top_k=1,
            hops=1,
        )


def test_birthday_month_ownership_uses_only_the_injected_date() -> None:
    plan = {
        "target_user": {
            "birthday_target": {"granularity": "month", "month": 2}
        }
    }

    assert graph_rag._plan_calendar_ranges(plan) == []
    assert graph_rag._plan_calendar_ranges(
        plan, today=datetime(2024, 2, 29, tzinfo=UTC).date()
    ) == [("20240201", "20240229")]


@pytest.mark.parametrize(
    ("target_user", "expected_sql"),
    [
        pytest.param(
            {"inactivity_period": {"min_days": 30}},
            f"B.LAST_LOGIN_DATE <= '{_FIXED_30_DAY_CUTOFF}'",
            id="inactivity-period",
        ),
        pytest.param(
            {"lifecycle": ["inactive_30d"]},
            f"B.LAST_LOGIN_DATE <= '{_FIXED_30_DAY_CUTOFF}'",
            id="inactivity-lifecycle",
        ),
        pytest.param(
            {"recent_login": {"min_days": 30}},
            f"B.LAST_LOGIN_DATE >= '{_FIXED_30_DAY_CUTOFF}'",
            id="recent-login",
        ),
        pytest.param(
            {"birthday_target": {"granularity": "month"}},
            "SUBSTRING('20260804', 5, 2)",
            id="birthday-month",
        ),
        pytest.param(
            {"signup_target": {"days": 30}},
            f"B.REG_DT >= '{_FIXED_30_DAY_CUTOFF}'",
            id="signup",
        ),
    ],
)
def test_member_calendar_sql_uses_the_request_reference_date(
    target_user: dict,
    expected_sql: str,
) -> None:
    result = _build_member_sql_result(target_user, context=_FIXED_CONTEXT)

    assert result["is_success"] is True
    assert isinstance(result["sql"], str)
    assert expected_sql in result["sql"]
    assert "GETDATE(" not in result["sql"].upper()


@pytest.mark.parametrize(
    "target_user",
    [
        pytest.param(
            {"inactivity_period": {"min_days": 30}},
            id="inactivity-period",
        ),
        pytest.param(
            {"lifecycle": ["inactive_30d"]},
            id="inactivity-lifecycle",
        ),
        pytest.param(
            {"recent_login": {"min_days": 30}},
            id="recent-login",
        ),
        pytest.param(
            {"birthday_target": {"granularity": "month"}},
            id="birthday-month",
        ),
        pytest.param(
            {"signup_target": {"days": 30}},
            id="signup",
        ),
    ],
)
def test_member_calendar_sql_fails_closed_without_a_reference_date(
    target_user: dict,
) -> None:
    result = _build_member_sql_result(target_user, context=None)

    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "reference_date_required"
