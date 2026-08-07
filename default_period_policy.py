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

신고된 결핍이 **거짓**일 수 있다(실측 2026-08-07)
------------------------------------------------
``최근 30일 구매한 회원 수를 알려줘`` 에서 모델이 ``최근`` 만 근거로 "기간이 없다"를 신고했다.
원문은 기간을 말했다 — 그 신고는 결핍이 아니라 오신고다. 그때 이 모듈이 기본 창(5일)을
지시하면 두 번 틀린다: 지시문이 **사용자가 말한 값을 덮으라**는 뜻이 되고, 정확히 옳게
해결한 재구조화 결과마저 "기본 창이 없다"는 이유로 폐기돼 되묻기로 되돌아간다(실측).

그래서 이 모듈은 결핍을 먼저 두 갈래로 가른다. 가르는 자리는 교정 단계와 공유한다
(:func:`targeting_policy.split_period_issues`) — 그 안의 판정자는 하나뿐이다
(``temporal_clause.stated_period_for_issue``). 절이 나오면 거짓 결핍이고, 그 라운드는
기본 창을 지시하지도 요구하지도 않는다(값의 출처가 사용자이므로 영수증도 남기지 않는다).
절이 나오지 않는 **진짜 결핍**의 처리는 예전 그대로다 — 기본 창을 지시하고, 그 창이 결과에
있을 때만 채택한다.
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
import targeting_policy
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

# 최종 SQL↔원문 의미 검증 게이트에 이 정책을 알리는 블록 제목(지시문과 user content 가 함께 쓴다).
VERIFIER_CONTEXT_HEADING = "[적용된 기본 기간 정책]"

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


def _quote_spans(spans: Sequence[Mapping[str, Any]]) -> str:
    """근거 구간들을 ``'최근' [0, 2)`` 모양으로 인용한다(좌표는 원문 좌표계다).

    같은 표면어가 여러 번 나오는 문장에서 표면어만으로는 구간이 특정되지 않는다. 좌표는
    모델이 이미 evidence 로 쓰는 값이라 새 어휘를 만들지 않는다.
    """

    quoted = [
        f"'{span['text']}' [{span['start']}, {span['end']})"
        for span in spans
        if isinstance(span.get("text"), str)
    ]
    return ", ".join(dict.fromkeys(quoted))


def render_default_period_instruction(
    period: DefaultPeriod, issues: Sequence[Mapping[str, Any]]
) -> str:
    """구조화기에 넘길 **애플리케이션 소유** 지시문 하나.

    프롬프트 본문이 아니라 이 지시문이 기본값을 나른다 — 정책이 꺼진 배포의 구조화기는 이
    문장을 아예 보지 않으므로, 기본 창을 아는 경로가 설정과 정확히 일치한다.

    구간은 표면어와 **좌표**로 함께 지목한다. 표면어만 인용하면 한 문장에 같은 표지가 둘일 때
    (가장 흔한 '최근') 이 지시문과 명시 기간 교정문이 같은 문자열을 놓고 서로 다른 창을
    요구하게 되고, 모델에게는 어느 쪽이 어느 쪽인지 가릴 재료가 없다.
    """

    quoted = _quote_spans(_issue_evidence(issues)) or "the bare recency marker"
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


def applied_period_receipt(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """이 플랜에서 **정책이 채운** 기간의 영수증(정책이 돌지 않았으면 ``None``).

    영수증은 :func:`apply_default_period` 가 채택한 뒤에만 남는다 — 즉 재구조화 결과가 그 창을
    실제로 들고 있고 원문이 말한 기간을 하나도 잃지 않았음이 이미 확인된 상태다.
    """

    receipt = plan.get(DEFAULT_PERIOD_KEY) if isinstance(plan, Mapping) else None
    if isinstance(receipt, Mapping) and receipt.get("source") == POLICY_SOURCE:
        return dict(receipt)
    return None


def verifier_instruction() -> str:
    """정책이 채운 창을 의미 검증기가 결함으로 부르지 않게 하는 지시문.

    검증기는 원문만 보면 정확히 관측한다 — "원문에 기간이 없는데 SQL 은 최근 5일이다". 관측이
    맞기 때문에 더 나쁘다: 알려 주지 않으면 정책이 켜진 배포는 정책이 적용될 때마다 자기 SQL 을
    '원문에 없는 조건'으로 막는다.
    """

    return (
        f"같은 이유로 {VERIFIER_CONTEXT_HEADING} 블록이 함께 제공되면, 원문이 기간을 말하지 않은 "
        "'최근'류 표현을 이 배포가 그 블록의 창(예: 최근 5일)으로 해석한 것이다 — SQL 에 그 창이 "
        "있는 것은 정상이며 원문에 기간이 없다는 이유로 spurious/wrong_value 로 판정하지 말라. "
        "다만 원문이 **직접 말한** 기간을 SQL 이 다른 값으로 바꿨다면 그건 여전히 불일치다. "
    )


def render_verifier_context(plan: Mapping[str, Any] | None) -> str:
    """검증기 user content 에 붙일 정책 블록(정책이 돌지 않았으면 빈 문자열)."""

    receipt = applied_period_receipt(plan)
    if receipt is None:
        return ""
    return f"\n\n{VERIFIER_CONTEXT_HEADING}\n" + json.dumps(
        receipt, ensure_ascii=False, indent=2, default=str
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


def _stated_rolling_durations(
    literal_bindings: Sequence[Mapping[str, Any]],
) -> set[tuple[int, str]]:
    """원문이 **직접 말한** 롤링 기간들(리터럴 추출기 소유).

    재구조화가 이 중 하나라도 잃으면 기본값이 사용자 명시값을 덮은 것이므로 채택하지 않는다.

    바인딩을 인자로 받는 이유는 같은 원문에서 리터럴 목록이 두 벌 생기지 않게 하기 위해서다 —
    '무엇을 말했나'(결핍 갈래 판정)와 '무엇을 잃었나'(채택 판정)가 다른 사실을 보면 안 된다.
    """

    stated: set[tuple[int, str]] = set()
    for binding in literal_bindings:
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
    required_window: DefaultPeriod | None,
    stated: set[tuple[int, str]],
    instructed_windows: frozenset[tuple[Any, ...]] = frozenset(),
) -> str | None:
    """채택 사유 없음이면 ``None``, 거부면 그 사유 문자열.

    ``required_window`` 가 ``None`` 이면 이 라운드는 기본 창을 **지시하지 않았다**(신고된 결핍이
    전부 거짓이었다). 그때까지 기본 창 포함을 요구하면 사용자가 말한 값으로 정확히 해결한
    후보가 폐기된다 — 실측에서 '최근 30일'을 rolling 30 day 로 옳게 세운 재구조화 결과가
    ``candidate_ignored_the_default_window`` 로 버려지고 되묻기로 되돌아갔다. 나머지 검사
    (표현 존재·결핍 없음·다른 issue 없음·명시 기간 보존)는 두 갈래가 똑같이 받는다.

    명시 기간 보존 검사는 재료가 둘이고 범위가 다르다.

    * ``stated`` — 원문의 **롤링 기간 전부**(리터럴 원장). 종류를 여기서 넓히지 않는다:
      달력 창은 랭킹 모집단처럼 회원 조건이 아닌 절에도 붙으므로, 원문에 있다는 이유만으로
      결과에 있으라고 요구하면 지시문이 요구한 적 없는 창 때문에 정상 후보가 거부된다.
    * ``instructed_windows`` — **이 라운드의 교정 지시문이 실제로 요구한 창들**(거짓 결핍으로
      판정된 절에서만 나온다). 달력 구간은 여기로 들어온다. 지시문이 ``event_ir_window`` 를
      그대로 복사하라고 요구했으므로 그 창을 잃은 결과는 요구를 지키지 않은 것이다.
    """

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
    if required_window is not None and required_window.key not in windows:
        return "candidate_ignored_the_default_window"
    lost = sorted(stated - windows)
    if lost:
        # 사용자가 말한 기간이 사라졌다 — 기본값이 명시값을 덮은 것이므로 원래 결핍을 지킨다.
        return f"candidate_dropped_stated_period:{lost}"
    present = targeting_policy.window_keys(candidate)
    lost_instructed = sorted(
        targeting_policy.window_label(key)
        for key in instructed_windows
        if key not in present
    )
    if lost_instructed:
        return f"candidate_dropped_stated_period:{lost_instructed}"
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

    신고된 결핍이 전부 거짓이면(원문이 이미 다 말했으면) 이 라운드는 기본 창을 지시하지 않고
    :func:`targeting_policy.stated_period_instruction` 의 교정만 지시한다. 그 갈래에서도 정책이
    꺼진 배포는 아무 것도 하지 않는다 — 거짓 결핍 교정의 1차 소유자는 앞 단계
    (:func:`targeting_policy.resolve_stated_period`)이고, 여기서는 **이미 쓰기로 한 라운드**를
    사용자가 말한 값을 덮는 데 쓰지 않는 것이 목적이다.
    """

    def log(event: str, payload: dict[str, Any]) -> None:
        if write_log is not None:
            write_log(event, {"query": query, **payload})

    issues = missing_period_issues(plan)
    if not issues:
        return plan
    literal_bindings = extract_literal_bindings(query, current_date=current_date)
    # 갈래를 가르는 자리도 하나다(:func:`targeting_policy.split_period_issues`). 교정 단계와
    # 이 단계가 각자 같은 루프를 들고 있으면 같은 문장을 다르게 갈라 지시문이 어긋난다.
    stated_issues, unstated_issues = targeting_policy.split_period_issues(
        query, issues, literal_bindings
    )
    if marker_resolver is not None:
        # 기본 창을 고를 근거는 **진짜 결핍**의 표면어뿐이다. 원문이 이미 수량화한 스팬을
        # 해석기에 넘기면 그 스팬에 기본값을 붙이라는 지시가 만들어진다.
        period = marker_resolver(unstated_issues) or period
    if period is None:
        return plan

    stated = _stated_rolling_durations(literal_bindings)
    # 교정 지시문이 실제로 요구할 창들. 창을 wire 로 적을 수 없는 절은 지시문이 언급하지
    # 않으므로 요구 목록에도 없다 — 지시하지 않은 것을 채택 조건으로 삼지 않는다.
    instructed_windows = frozenset(
        key
        for _issue, clause in stated_issues
        if (key := targeting_policy.window_key(clause.wire_window)) is not None
    )
    if not unstated_issues and not instructed_windows:
        # 이 라운드가 요구할 것이 없다. 내용 없는 지시문으로 재구조화 예산을 태우지 않는다.
        return plan
    if unstated_issues:
        required_window: DefaultPeriod | None = period
        instruction = render_default_period_instruction(period, unstated_issues)
        if stated_issues:
            # 한 문장에 두 갈래가 함께 있다. 기본 창은 말하지 않은 스팬에만, 원문이 말한 값은
            # 그 값 그대로 — 두 지시를 함께 넘겨야 모델이 어느 쪽이 어느 쪽인지 구분한다.
            instruction = "\n\n".join(
                [instruction, targeting_policy.stated_period_instruction(stated_issues)]
            )
    else:
        required_window = None
        instruction = targeting_policy.stated_period_instruction(stated_issues)
    candidate = restructure(instruction)
    rejected = _admit(
        candidate,
        required_window=required_window,
        stated=stated,
        instructed_windows=instructed_windows,
    )
    log(
        "audience_default_period_policy",
        {
            # ``applied`` 의 뜻은 예나 지금이나 '기본값이 적용됐는가'다. 거짓 결핍만 있던
            # 라운드는 후보를 채택해도 기본값을 쓰지 않으므로 ``adopted`` 로만 참이 된다.
            "applied": rejected is None and required_window is not None,
            "adopted": rejected is None,
            "reason": rejected,
            "period": period.window,
            "origin": period.origin,
            "stated_periods": sorted(stated),
            "stated_period_issues": len(stated_issues),
        },
    )
    if rejected is not None:
        return plan
    if required_window is None:
        # 이 창의 출처는 제품 설정이 아니라 사용자다. 기본값 영수증도 policy_applied 도 붙이지
        # 않는다 — 붙이면 사용자가 말한 30일이 배포가 고른 값처럼 보고된다.
        #
        # 다만 **결정 자체는 기록한다**. 이 라운드는 플랜을 바꿔서 채택하므로, 앞 단계가 버려질
        # 플랜에 적어 둔 영수증(교정 실패 → CLARIFY)이 함께 사라진다. 아무것도 남기지 않으면
        # 배송된 플랜만 보는 감사에는 기간을 무엇으로 확정했는지가 존재하지 않게 된다.
        # 사유 코드는 같은 교정이 앞 단계에서 성공했을 때와 같은 것을 쓴다 — 성공한 단계가
        # 어디냐에 따라 원장이 달라지면 원장으로는 같은 결말을 알아볼 수 없다.
        targeting_policy.record_decision(
            candidate if isinstance(candidate, dict) else None,
            targeting_policy.PolicyDecision(
                policy_id=targeting_policy.PERIOD_POLICY_ID,
                stage="ambiguity",
                decision=targeting_policy.DECISION_ALLOW,
                reason_code=targeting_policy.REASON_PERIOD_STATED,
                input_facts=("period_stated=true", "repair=accepted_in_default_round"),
                # 이 라운드가 확정한 창을 적는다. 롤링 원장(``stated``)만 적으면 달력 구간으로
                # 해결된 요청의 영수증이 빈 목록이 되어, 무엇으로 확정했는지가 사라진다.
                detail={
                    "windows": [
                        {"value": key[1], "unit": key[2]}
                        for key in sorted(
                            (key for key in instructed_windows if key[0] == "rolling"),
                            key=lambda item: (item[2], item[1]),
                        )
                    ],
                    "intervals": [
                        {"start": key[1], "end_exclusive": key[2]}
                        for key in sorted(
                            key for key in instructed_windows if key[0] == "interval"
                        )
                    ],
                },
            ),
        )
        return candidate

    candidate[DEFAULT_PERIOD_KEY] = {
        "source": POLICY_SOURCE,
        "value": period.value,
        "unit": period.unit,
        "window": period.window,
        "origin": period.origin,
        "expression": period.expression,
        # 기본값이 실제로 채운 구간만 남긴다 — 원문이 이미 말한 스팬까지 적으면 영수증이
        # 사용자의 말을 제품 설정의 값으로 보고하게 된다.
        "evidence": _issue_evidence(unstated_issues),
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
    "VERIFIER_CONTEXT_HEADING",
    "DefaultPeriod",
    "applied_period_receipt",
    "apply_default_period",
    "missing_period_issues",
    "render_default_period_instruction",
    "render_verifier_context",
    "resolve_catalog_period",
    "resolve_default_period",
    "verifier_instruction",
]
