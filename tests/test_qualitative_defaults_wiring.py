"""[Qualitative defaults] 카탈로그가 **실제로 SQL 경로에서 읽히는가** — 배선 계약.

`tests/test_qualitative_defaults_catalog.py` 는 파일 표기가 규약을 지키는지만 잰다. 그것만으로는
"참조하라고 준 표"가 아무 것도 바꾸지 못한 채 저장소에 남는다. 여기서 고정하는 것은 그 표가
자연어→SQL 경로의 어느 지점에서 어떤 값으로 읽히는가다.

읽히는 지점은 하나다 — 구조화기가 ``missing_argument(period)`` 로 닫은 결핍을, 그 결핍이
지목한 **표면어 자체**로 카탈로그에 물어 채우는 자리
(:func:`default_period_policy.resolve_catalog_period`). 그래서 env 값 하나를 모든 표현에
똑같이 적용하던 경로와 달리 '요즘'은 14일, '최근 한 달'은 30일이 된다.

함께 고정하는 경계 넷은 전부 "적혀 있다고 쓸 수 있는 것은 아니다"에 관한 것이다:

  - **꺼져 있으면 아무 것도 하지 않는다.** 스위치 없는 배포의 결말은 코어 fail-close 다.
  - **최장 일치가 이긴다.** '최근 한 달'은 '최근'을 포함한다. 짧은 쪽이 먼저 걸리면 30일이어야
    할 문장이 조용히 5일이 된다.
  - **hour 는 창이 될 수 없다.** '방금 = 최근 1시간'은 카탈로그에 있지만 ``RollingWindow`` 의
    단위가 아니므로(``event_ir.WINDOW_UNITS``) 나오지 않는다 — 하루로 올림하지 않는다.
  - **사용자가 말한 기간이 언제나 이긴다.** 이 불변식은 기존 ``_admit`` 이 지키며, 카탈로그
    경로로 들어와도 그대로여야 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import default_period_policy  # noqa: E402
import event_ir  # noqa: E402
import qualitative_defaults  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 기본 상태는 '켜짐'이다. 꺼진 상태는 해당 테스트가 직접 되돌린다."""
    monkeypatch.setenv(qualitative_defaults.ENABLED_ENV, "1")
    qualitative_defaults.reset_cache()


def _issues(text: str, *, start: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "code": "missing_argument",
            "argument": "period",
            "evidence": {"text": text, "start": start, "end": start + len(text)},
        }
    ]


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("최근", (5, "day")),
        ("요즘", (14, "day")),
        ("지난 며칠", (5, "day")),
        ("최근 일주일", (7, "day")),
        ("최근 한 달", (30, "day")),
        ("최근 분기", (90, "day")),
    ],
)
def test_each_recency_marker_resolves_to_its_own_window(
    marker: str, expected: tuple[int, str]
) -> None:
    """표현마다 다른 창을 준다 — env 값 하나로는 낼 수 없던 구분이다."""
    found = qualitative_defaults.resolve_recency_marker(marker)
    assert found is not None, f"{marker!r} 를 카탈로그가 답하지 못했다"
    assert found.key == expected


def test_longest_expression_wins() -> None:
    """'최근 한 달'이 '최근'보다 먼저 걸린다(짧은 쪽이 이기면 30일이 5일이 된다)."""
    sentence = "최근 한 달 동안 구매한 회원"
    found = qualitative_defaults.resolve_recency_marker(sentence)
    assert found is not None
    assert (found.expression, found.key) == ("최근 한 달", (30, "day"))


def test_marker_is_found_inside_a_longer_evidence_span() -> None:
    """구조화기의 evidence 는 조건 문장 전체다 — 그 안에 든 표면어를 찾아야 한다."""
    span = "최근 캠페인 발송 성공 횟수가 3회 이상"
    found = qualitative_defaults.resolve_recency_marker(span)
    assert found is not None
    assert found.canonical == "recent"


def test_hour_scale_marker_is_not_offered_as_a_window() -> None:
    """'방금 = 최근 1시간'은 창이 될 수 없다. 하루로 올림하지 않고 아예 내보내지 않는다."""
    assert "hour" not in event_ir.WINDOW_UNITS
    assert all(item.expression != "방금" for item in qualitative_defaults.recency_defaults())
    assert qualitative_defaults.resolve_recency_marker("방금 가입한 회원") is None


@pytest.mark.parametrize("marker", ["오늘", "어제", "지난주", "이번 주", "단기간", "당분간", "장기간"])
def test_non_rolling_markers_are_not_offered(marker: str) -> None:
    """달력 경계·기간 길이·미래 창은 롤링 창이 아니므로 이 통로로 나오지 않는다."""
    assert qualitative_defaults.resolve_recency_marker(marker) is None


def test_disabled_deployment_resolves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """스위치가 없으면 카탈로그는 침묵한다 — 코어 fail-close 가 그대로 결말이다."""
    monkeypatch.delenv(qualitative_defaults.ENABLED_ENV, raising=False)
    assert qualitative_defaults.resolve_recency_marker("요즘 구매한 회원") is None
    assert default_period_policy.resolve_catalog_period(_issues("요즘 구매한 회원")) is None


def test_catalog_period_carries_its_provenance() -> None:
    """어느 항목이 이 창을 정했는지 남는다. 표식이 없으면 사용자의 말과 구분되지 않는다."""
    period = default_period_policy.resolve_catalog_period(_issues("요즘 구매한 회원"))
    assert period is not None
    assert (period.value, period.unit) == (14, "day")
    assert period.expression == "요즘"
    assert qualitative_defaults.ENABLED_ENV in period.origin
    assert "these_days" in period.origin


def test_catalog_period_is_none_for_unknown_markers() -> None:
    """모르는 말에는 비슷한 항목을 대신 주지 않는다(§12)."""
    assert default_period_policy.resolve_catalog_period(_issues("한참 전에 구매한 회원")) is None


def test_marker_resolver_beats_the_deployment_wide_default() -> None:
    """표현별 값이 배포 전역 값보다 구체적이므로 이긴다.

    ``apply_default_period`` 가 재구조화 결과를 채택할 때 어느 창을 요구했는지로 확인한다 —
    카탈로그가 이겼다면 요구된 창은 env 값(5일)이 아니라 '요즘'의 14일이다.
    """
    plan: dict[str, Any] = {
        "audience_requirement": {"expression": None, "issues": _issues("요즘 구매한 회원")}
    }
    seen: list[str] = []

    def restructure(instruction: str) -> dict[str, Any]:
        seen.append(instruction)
        return {"audience_requirement": {"expression": None, "issues": []}}

    default_period_policy.apply_default_period(
        plan,  # type: ignore[arg-type]  # 정책은 매핑만 읽는다(플랜 타입 전체가 필요하지 않다).
        query="요즘 구매한 회원을 대상으로 캠페인을 만들어줘",
        current_date="2026-08-06",
        period=default_period_policy.DefaultPeriod(value=5, unit="day", origin="env"),
        marker_resolver=default_period_policy.resolve_catalog_period,
        restructure=restructure,
    )

    assert seen, "재구조화가 호출되지 않았다"
    assert '"value": 14' in seen[0] and '"unit": "day"' in seen[0]
    assert '"value": 5' not in seen[0]


def test_env_default_still_applies_when_the_catalog_cannot_answer() -> None:
    """카탈로그가 답하지 못하면 배포 전역 값으로 물러선다(기존 경로가 살아 있다)."""
    plan: dict[str, Any] = {
        "audience_requirement": {"expression": None, "issues": _issues("한참 전")}
    }
    seen: list[str] = []

    def restructure(instruction: str) -> dict[str, Any]:
        seen.append(instruction)
        return {"audience_requirement": {"expression": None, "issues": []}}

    default_period_policy.apply_default_period(
        plan,  # type: ignore[arg-type]
        query="한참 전 구매한 회원",
        current_date="2026-08-06",
        period=default_period_policy.DefaultPeriod(value=5, unit="day", origin="env"),
        marker_resolver=default_period_policy.resolve_catalog_period,
        restructure=restructure,
    )

    assert seen and '"value": 5' in seen[0]


def test_stated_period_still_wins_over_the_catalog() -> None:
    """원문이 말한 기간을 잃은 후보는 카탈로그 경로에서도 채택하지 않는다.

    사용자가 '최근 90일'이라고 말했는데 재구조화가 그 창을 잃고 카탈로그의 5일만 들고 오면,
    기본값이 명시값을 덮은 것이므로 원래 결핍을 보존해야 한다.
    """
    query = "최근 90일 동안 구매한 회원"
    plan: dict[str, Any] = {
        "audience_requirement": {"expression": None, "issues": _issues("최근")}
    }

    def restructure(_instruction: str) -> dict[str, Any]:
        return {
            "audience_requirement": {
                "expression": {"window": {"type": "rolling", "value": 5, "unit": "day"}},
                "issues": [],
            }
        }

    result = default_period_policy.apply_default_period(
        plan,  # type: ignore[arg-type]
        query=query,
        current_date="2026-08-06",
        period=None,
        marker_resolver=default_period_policy.resolve_catalog_period,
        restructure=restructure,
    )

    assert result is plan, "명시값을 잃은 후보가 채택됐다"
    assert default_period_policy.DEFAULT_PERIOD_KEY not in result


def test_wiring_is_reachable_from_the_sql_path() -> None:
    """SQL 경로가 이 해석기를 실제로 넘긴다 — 모듈만 있고 호출이 없으면 배선이 아니다."""
    source = (REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8")
    assert "marker_resolver=default_period_policy.resolve_catalog_period" in source
