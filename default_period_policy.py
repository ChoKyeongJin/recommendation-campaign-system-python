"""기간 없는 '최근'에 적용할 **기본 기간 정책** — 소유자는 호출 계층이다.

코어 구조화 경로의 정책은 fail-close 다. 원문이 기간을 말하지 않은 '최근'은
:mod:`query_structurer.structurer` 가 ``expression=null`` + ``missing_argument(period)`` 로
닫고, 구조화기 프롬프트(:mod:`query_structurer.prompt`)는 어떤 기본 창도 지어내지 않는다.
그것이 기본 동작이고 이 모듈은 그 판정을 뒤집지 않는다.

예전에는 그 기본값('맨 최근 = 최근 5일')이 구조화기 프롬프트 안에 적혀 있었다. 그러면 두 가지가
동시에 깨진다 — 되묻기와 기본값 중 무엇이 옳은지는 원문이 아니라 **제품 설정**이 정하는 문제인데
결정을 구조화기가 내렸고, 결과물에서는 사용자가 말한 기간과 애플리케이션이 고른 기간이 똑같은
``RollingWindow`` 하나로 섞여 구분되지 않았다.

그래서 정책은 여기(호출 계층)에 있다. 제품이 "되묻지 말고 최근 N일로 읽어라"를 **명시적으로**
설정한 배포에서만, 설정된 기간 하나를 같은 구조화기에 한 번 더 지시해 그 결핍을 채운다.
두 가지를 지킨다.

1. **사용자 명시값이 언제나 우선한다.** 정책은 ``missing_argument(period)`` 가 있을 때만 돌고,
   재구조화 결과가 원문이 말한 기간을 하나라도 잃으면 채택하지 않는다(원래 결핍을 보존한다).
2. **기본값은 사용자가 말한 값과 구분된다.** 채택하면 결과에
   ``audience_default_period.source = "default_policy"`` 를 남긴다 — 이 표식이 없으면 응답을
   읽는 쪽은 5일이 사용자의 말인지 제품 설정인지 알 수 없다.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import event_ir
import qualitative_defaults
from query_structurer import CampaignQueryPlanV4
from query_structurer.campaign_plan_v4 import AUDIENCE_REQUIREMENT_KEY
from query_structurer.semantic_ir import (
    empty_semantic_ir,
    extract_literal_bindings,
    write_semantic_ir,
)

# 정책 값의 출처. 설정하지 않으면 정책은 꺼져 있고 코어 fail-close 가 그대로 결말이다.
DEFAULT_PERIOD_ENV = "AUDIENCE_DEFAULT_PERIOD"
# 기본값이 적용된 사실을 남기는 플랜 키(응답 debug 로 그대로 나간다).
DEFAULT_PERIOD_KEY = "audience_default_period"
# 이 기간의 출처가 사용자가 아니라 제품 설정임을 알리는 표식.
POLICY_SOURCE = "default_policy"
# 결정 감사 로그(:mod:`plan_decisions`)에 남는 소유자 이름이자 semantic_ir 의 policy_id.
POLICY_OWNER = "default_period_policy"
# 기본값이 채운 의미 슬롯(semantic_ir.policy_applications[].fields).
_POLICY_FIELD = "audience.period"

_PERIOD_ARGUMENT = "period"
_MISSING_ARGUMENT = "missing_argument"
# 설정 형식은 "<양수> <단위>" 하나다(예: "5 day", "5days", "2 weeks"). 단위 어휘의 소유자는
# :mod:`event_ir` 이고 표기 흔들림(day/days)도 그쪽이 접는다 — 여기서 두 번째 단위 사전을 만들면
# 설정이 IR 과 다른 말을 하게 된다.
_PERIOD_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]+)\s*$")


@dataclass(frozen=True, slots=True)
class DefaultPeriod:
    """제품이 선택한 기본 기간 하나(값·단위·출처).

    ``expression`` 은 이 기간을 고르게 만든 표면어다(카탈로그 해석일 때만 채워진다). 설정
    한 값을 모든 표현에 똑같이 적용하는 env 경로에서는 ``None`` 이고, 그 차이가 응답 문구와
    감사 로그에 그대로 드러난다 — '요즘'이 14일이 된 것과 아무 '최근'이나 5일이 된 것은
    같은 사건이 아니다.
    """

    value: int
    unit: str
    origin: str
    expression: str | None = None

    @property
    def window(self) -> dict[str, Any]:
        return event_ir.RollingWindow(value=self.value, unit=self.unit).to_dict()

    @property
    def key(self) -> tuple[int, str]:
        return (self.value, self.unit)


def resolve_default_period(
    configured: str | None = None, *, origin: str | None = None
) -> DefaultPeriod | None:
    """설정된 기본 기간을 읽는다. 설정이 없거나 해석 불가면 ``None``(= 정책 꺼짐).

    해석 불가를 임의의 기본값으로 대체하지 않는다 — 오타 하나가 조용히 '최근 5일'이 되면
    이 모듈이 막으려던 바로 그 상태가 된다.
    """

    source = origin or DEFAULT_PERIOD_ENV
    raw = configured if configured is not None else os.getenv(DEFAULT_PERIOD_ENV)
    if raw is None or not str(raw).strip():
        return None
    match = _PERIOD_RE.match(str(raw))
    if match is None:
        return None
    unit = event_ir.canonical_unit(match.group(2))
    if unit not in event_ir.WINDOW_UNITS:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    return DefaultPeriod(value=value, unit=str(unit), origin=source)


def resolve_catalog_period(
    issues: Sequence[Mapping[str, Any]], *, catalog_path: Any = None
) -> DefaultPeriod | None:
    """결핍이 **지목한 표면어**를 카탈로그에서 찾아 그 표현의 기본 창을 준다.

    env 경로(:func:`resolve_default_period`)는 배포마다 값 하나만 받으므로 '요즘'과 '최근
    한 달'이 같은 창이 된다. 구조화기는 결핍을 신고할 때 어떤 말이 기간을 요구하는지 evidence
    스팬으로 알려 주므로, 그 말 자체를 카탈로그에 물어보면 표현마다 다른 창을 줄 수 있다.

    카탈로그가 답하지 못하면 ``None`` 이고, 그때 결말을 정하는 것은 호출 계층이다(env 기본값
    또는 코어 fail-close). 여기서 비슷한 항목으로 대신하지 않는다.
    """

    for span in _issue_evidence(issues):
        found = qualitative_defaults.resolve_recency_marker(span["text"], catalog_path)
        if found is None:
            continue
        return DefaultPeriod(
            value=found.value,
            unit=found.unit,
            origin=f"{qualitative_defaults.ENABLED_ENV}:{found.canonical}",
            expression=found.expression,
        )
    return None


def missing_period_issues(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """이 플랜이 신고한 '기간이 없다' 결핍 목록(정책이 도는 유일한 조건)."""

    requirement = plan.get(AUDIENCE_REQUIREMENT_KEY)
    if not isinstance(requirement, Mapping):
        return []
    issues = requirement.get("issues")
    if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes)):
        return []
    return [
        dict(item)
        for item in issues
        if isinstance(item, Mapping)
        and item.get("code") == _MISSING_ARGUMENT
        and item.get("argument") == _PERIOD_ARGUMENT
    ]


def _issue_evidence(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for item in issues:
        evidence = item.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        text = evidence.get("text")
        start, end = evidence.get("start"), evidence.get("end")
        if isinstance(text, str) and isinstance(start, int) and isinstance(end, int):
            spans.append({"text": text, "start": start, "end": end})
    return spans


def render_default_period_instruction(
    period: DefaultPeriod, issues: Sequence[Mapping[str, Any]]
) -> str:
    """구조화기에 넘길 **애플리케이션 소유** 지시문 하나.

    프롬프트 본문이 아니라 이 지시문이 기본값을 나른다 — 정책이 꺼진 배포의 구조화기는 이
    문장을 아예 보지 않으므로, 기본 창을 아는 경로가 설정과 정확히 일치한다.
    """

    spans = sorted({span["text"] for span in _issue_evidence(issues)})
    quoted = ", ".join(f"'{span}'" for span in spans) if spans else "the bare recency marker"
    return "\n".join(
        [
            "[Application-owned Default Period Policy]",
            (
                f"This deployment resolves an unquantified recency marker to the most recent "
                f"{period.value} {period.unit}. Rebuild the same audience meaning and put "
                f"{json.dumps(period.window)} in the TimeFilter that owns {quoted}. "
                f"Do not report missing_argument(period) for that span."
            ),
            (
                "This default applies ONLY where the query states no duration. Every period the "
                "query does state keeps its own stated value unchanged, and no other missing "
                "value may be inferred."
            ),
        ]
    )


def _rolling_windows(plan: Mapping[str, Any]) -> set[tuple[int, str]]:
    """이 플랜의 오디언스 표현이 실제로 들고 있는 롤링 창들."""

    requirement = plan.get(AUDIENCE_REQUIREMENT_KEY)
    expression = requirement.get("expression") if isinstance(requirement, Mapping) else None
    found: set[tuple[int, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "rolling":
                unit = event_ir.canonical_unit(value.get("unit"))
                raw = value.get("value")
                if unit is not None and isinstance(raw, int) and not isinstance(raw, bool):
                    found.add((raw, unit))
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child)

    visit(expression)
    return found


def _stated_rolling_durations(query: str, current_date: str | None) -> set[tuple[int, str]]:
    """원문이 **직접 말한** 롤링 기간들(리터럴 추출기 소유).

    재구조화가 이 중 하나라도 잃으면 기본값이 사용자 명시값을 덮은 것이므로 채택하지 않는다.
    """

    stated: set[tuple[int, str]] = set()
    for binding in extract_literal_bindings(query, current_date=current_date):
        if not isinstance(binding, Mapping) or binding.get("kind") != "duration":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        if normalized.get("temporal_kind") != "rolling_duration":
            continue
        unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
        value = normalized.get("value")
        if unit is not None and isinstance(value, int) and not isinstance(value, bool):
            stated.add((value, unit))
    return stated


def _admit(
    candidate: Any,
    *,
    period: DefaultPeriod,
    stated: set[tuple[int, str]],
) -> str | None:
    """채택 사유 없음이면 ``None``, 거부면 그 사유 문자열."""

    if not isinstance(candidate, Mapping):
        return "candidate_not_a_plan"
    requirement = candidate.get(AUDIENCE_REQUIREMENT_KEY)
    if not isinstance(requirement, Mapping) or requirement.get("expression") is None:
        return "candidate_has_no_expression"
    if missing_period_issues(candidate):
        return "candidate_still_missing_period"
    issues = requirement.get("issues")
    if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes)) and list(issues):
        return "candidate_reports_other_issues"
    windows = _rolling_windows(candidate)
    if period.key not in windows:
        return "candidate_ignored_the_default_window"
    lost = sorted(stated - windows)
    if lost:
        # 사용자가 말한 기간이 사라졌다 — 기본값이 명시값을 덮은 것이므로 원래 결핍을 지킨다.
        return f"candidate_dropped_stated_period:{lost}"
    return None


def apply_default_period(
    plan: CampaignQueryPlanV4,
    *,
    query: str,
    current_date: str | None,
    period: DefaultPeriod | None,
    restructure: Callable[[str], CampaignQueryPlanV4],
    write_log: Callable[[str, dict[str, Any]], None] | None = None,
    marker_resolver: Callable[[Sequence[Mapping[str, Any]]], DefaultPeriod | None] | None = None,
) -> CampaignQueryPlanV4:
    """기간 결핍 하나를 설정된 기본 기간으로 채운다(설정이 없으면 원본 그대로).

    ``restructure`` 는 애플리케이션 소유 지시문 하나를 받아 **같은 구조화기**를 다시 부르는
    호출자의 클로저다. 모델이 표현을 ``null`` 로 닫아 놓았으므로 여기서 트리를 기워 넣을 수는
    없다 — 창을 어느 조건이 소유하는지는 표현을 만든 쪽만 안다.

    ``marker_resolver`` 는 결핍이 지목한 표면어를 보고 **그 표현에 맞는** 기간을 고르는 선택적
    해석기다(:func:`resolve_catalog_period`). 표현별 값이 배포 전역 값보다 구체적이므로 답을
    내면 그쪽이 이긴다. 답하지 못하면 ``period`` 로 물러서고, 둘 다 없으면 정책은 돌지 않는다.
    """

    def log(event: str, payload: dict[str, Any]) -> None:
        if write_log is not None:
            write_log(event, {"query": query, **payload})

    issues = missing_period_issues(plan)
    if not issues:
        return plan
    if marker_resolver is not None:
        period = marker_resolver(issues) or period
    if period is None:
        return plan

    stated = _stated_rolling_durations(query, current_date)
    instruction = render_default_period_instruction(period, issues)
    candidate = restructure(instruction)
    rejected = _admit(candidate, period=period, stated=stated)
    log(
        "audience_default_period_policy",
        {
            "applied": rejected is None,
            "reason": rejected,
            "period": period.window,
            "origin": period.origin,
            "stated_periods": sorted(stated),
        },
    )
    if rejected is not None:
        return plan

    candidate[DEFAULT_PERIOD_KEY] = {
        "source": POLICY_SOURCE,
        "value": period.value,
        "unit": period.unit,
        "window": period.window,
        "origin": period.origin,
        "expression": period.expression,
        "evidence": _issue_evidence(issues),
    }
    # 의미 판정도 'resolved'(사용자 문장만으로 확정)가 아니라 **정책이 채운 성공**이다. 이 상태는
    # 이미 선언돼 있었고(SemanticStatus.policy_applied) 하류 게이트도 중립 성공으로 받는다 —
    # 생산자만 없었다. 여기서 쓰지 않으면 응답의 status 는 사용자가 기간을 말한 경우와 같아진다.
    write_semantic_ir(
        candidate,
        empty_semantic_ir(
            status="policy_applied",
            policy_applications=[
                {"policy_id": POLICY_OWNER, "fields": [_POLICY_FIELD]}
            ],
            message=(
                f"기간을 말하지 않은 '{period.expression}'을(를) 기본 기간"
                f"(최근 {period.value} {period.unit})으로 해석했습니다."
                if period.expression
                else (
                    f"기간을 말하지 않은 '최근'을 배포 설정의 기본 기간"
                    f"(최근 {period.value} {period.unit})으로 해석했습니다."
                )
            ),
        ),
    )
    return candidate


__all__ = [
    "DEFAULT_PERIOD_ENV",
    "DEFAULT_PERIOD_KEY",
    "POLICY_OWNER",
    "POLICY_SOURCE",
    "DefaultPeriod",
    "apply_default_period",
    "missing_period_issues",
    "render_default_period_instruction",
    "resolve_catalog_period",
    "resolve_default_period",
]
