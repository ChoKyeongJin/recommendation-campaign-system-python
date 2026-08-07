"""'최근 30일' 이 '최근' 하나 때문에 되묻기로 닫히던 실행의 **오프라인 재생**.

실측(2026-08-07, `/target-sql` 전수 22회 중 4회 되묻기). 프롬프트는
``최근 30일 구매한 회원 수를 알려줘`` 하나이고, 원문은 기간을 분명히 말했다. 그런데도 결말은
"기간을 알려 주세요"였다. 로그가 남긴 체인은 다음과 같다.

  1차 구조화   expression=null + ``missing_argument(period)``, 근거는 ``최근`` 뿐이다.
               (모델이 준 좌표는 [3,5]로 어긋나 있고 애플리케이션이 [0,2]로 교정한다.)
               → **그 신고는 원문과 모순**인데 그때는 반박 재료가 결과에 실리지 않아
                 첫 호출이 그대로 '성공'으로 끝났다(재시도 0회).
  교정 라운드   3시도 전부 실패. (1) JSON ``Extra data``, (2) TimeFilter 를
               ``purchase.order_time``(선언 data_type=``string``)에 걸어 컴파일 fail-close,
               (3) 그걸 ``unsupported`` 로 선언했다가 심볼 축 반박에 걸림.
               2번의 원인은 모델 잘못만이 아니다 — 기간 창을 걸 수 있는 유일한 필드
               (``purchase.occurred_at``)가 프롬프트에 **한 번도 등장하지 않았다**.
  기본기간 라운드 여기서 모델이 정답(``purchase.occurred_at`` 에 rolling 30 day)을 냈다.
               그런데 그 후보가 ``candidate_ignored_the_default_window`` 로 폐기됐다 —
               기본 창(5일)을 지시한 라운드였으므로 기본 창이 없는 후보를 거부한 것이다.
               **정확히 옳게 해결한 답이 정책 때문에 버려지고 되묻기로 되돌아갔다.**

이 파일은 그 체인을 LLM·검색·DB 없이 재생한다. 관용구는
``tests/test_cart_abandonment_replay.py`` 와 같다 — 실측 모델 응답을 가짜 complete 로 주입하고,
구조화기 → 정책 라운드 → :func:`graph_rag.compile_executable_plan` → :mod:`sql_guard` 까지
종단으로 고정한다. 시각은 고정 기준일 하나만 쓴다(CLAUDE.md §44).

고정하는 계약은 넷이다.

  [C] 거짓 결핍 신고는 **첫 구조화 호출 안에서** 재시도를 부른다(모델 누락으로 판정되므로).
  [B] 교정 라운드가 실패해도 기본기간 라운드가 낸 정답 후보는 **채택**되고, 그 창은 사용자의
      값이므로 기본값 영수증도 ``policy_applied`` 도 붙지 않는다.
  [종단] 채택된 플랜이 실제로 SQL 이 되고 sql_guard 를 통과하며, 그 술어가 카탈로그가 선언한
      시간 컬럼 위에 선다.
  [A] 그 소스의 파생 시각 필드가 구조화기 프롬프트에 광고된다(시도2의 실패 원인 제거).

[A] 의 **카탈로그 전역** 불변식(모든 소스 순회·grain 표기·문자열 시각 컬럼 배제·미등록 포맷
고지)은 ``tests/test_audience_catalog_time_field_guidance.py`` 가 이미 소유한다. 여기서는 이
재생이 실제로 쓰는 소스에 한정해 얇게만 확인한다 — 같은 사실을 두 파일이 주장하면 카탈로그가
바뀔 때 두 곳을 고쳐야 한다.

실행: docker exec recommendation-campaign-system-python-api-1 \
        python -m pytest -q -p no:cacheprovider tests/test_stated_period_correction_replay.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

import audience_runtime
import default_period_policy
import event_compiler
import event_ir
import graph_rag
import semantic_outcome
import sql_guard
import targeting_policy
from query_structurer.campaign_plan_v4 import (
    AUDIENCE_REQUIREMENT_KEY,
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext

# 고정 기준일 하나(CLAUDE.md §44). 롤링 창의 경계는 실행 시점 함수로 렌더되므로 이 값이 SQL
# 문자열에 굳지 않는다 — 그래서 같은 SQL 을 내일 실행해도 같은 뜻이다.
CURRENT_DATE = "2026-08-04"


@dataclass(frozen=True)
class ReplayCase:
    """재생할 요청 하나. 소스·기간은 **카탈로그에 선언된 것**만 쓴다."""

    query: str
    source: str
    value: int
    unit: str
    marker: str = "최근"
    # 모델이 결핍 근거로 지목한 구간. ``None`` 이면 원문에서 표지 위치를 찾아 쓴다.
    reported_evidence: tuple[int, int] | None = None

    @property
    def evidence(self) -> tuple[int, int]:
        if self.reported_evidence is not None:
            return self.reported_evidence
        start = self.query.index(self.marker)
        return (start, start + len(self.marker))

    @property
    def window(self) -> event_ir.RollingWindow:
        return event_ir.RollingWindow(value=self.value, unit=self.unit)


# 실측 요청. ``reported_evidence`` 는 로그에 남은 **그대로**다 — '최근' 은 [0,2]에 있는데
# 모델은 [3,5]를 적었고, 애플리케이션이 그 좌표를 [0,2]로 교정한다. 재생이 교정 전 좌표에서
# 출발해야 교정 단계까지 함께 고정된다.
LIVE = ReplayCase(
    query="최근 30일 구매한 회원 수를 알려줘",
    source="purchase",
    value=30,
    unit="day",
    reported_evidence=(3, 5),
)

# 일반성. 다른 기간 단위(개월·주)와 다른 사건 소스를 카탈로그 선언에서 골랐다 — 존재하지 않는
# 조합을 지어내지 않기 위해, 아래 ``test_every_replay_case_uses_a_declared_event_source`` 가
# 이 목록 자체를 카탈로그와 대조한다.
GENERALITY_CASES = (
    ReplayCase(query="최근 3개월 로그인한 회원", source="login", value=3, unit="month"),
    ReplayCase(query="최근 2주 장바구니에 담은 회원", source="cart", value=2, unit="week"),
    ReplayCase(
        query="최근 6개월 캠페인에 반응한 회원",
        source="campaign_response",
        value=6,
        unit="month",
    ),
)
ALL_CASES = (LIVE, *GENERALITY_CASES)

# 실측 시도2가 TimeFilter 를 걸었던 심볼. 선언된 필드이지만 시각 타입이 아니라 컴파일이
# fail-close 했다(event_compiler.py:397 "시간 컬럼이 아닌 타입에 기간 조건을 걸 수 없습니다").
LIVE_ATTEMPT_TIME_SYMBOL = "purchase.order_time"


def _base_payload(requirement: dict[str, Any]) -> dict[str, Any]:
    """구조화기 tool 스키마가 받는 네 필드. 실측 응답과 같은 모양이다."""
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": requirement,
    }


def _false_period_gap_payload(case: ReplayCase) -> dict[str, Any]:
    """1차 응답 — 원문이 이미 말한 기간을 '없다'고 신고한다(거짓 결핍)."""
    start, end = case.evidence
    return _base_payload({
        "expression": None,
        "issues": [{
            "code": "missing_argument",
            "argument": "period",
            "message": (
                f"The query specifies '{case.marker}' without a duration, which is "
                "required to define the time window."
            ),
            "evidence": {"text": case.marker, "start": start, "end": end},
        }],
    })


def _stated_period_answer_payload(case: ReplayCase) -> dict[str, Any]:
    """기본기간 라운드가 실제로 낸 **정답 후보**(실측 로그의 마지막 성공 응답)."""
    amount_start = case.query.index(f"{case.value}")
    amount_text = case.query[amount_start:].split(" ", 1)[0]
    return _base_payload({
        "expression": {
            "type": "exists",
            "relation": {
                "type": "filter",
                "relation": {"type": "source", "name": case.source},
                "where": {
                    "type": "time_filter",
                    "field": {
                        "type": "field",
                        "name": f"{case.source}.{event_ir.TIME_FIELD_SUFFIX}",
                    },
                    "window": case.window.to_dict(),
                },
            },
            "evidence": {
                "text": amount_text,
                "start": amount_start,
                "end": amount_start + len(amount_text),
            },
        },
        "issues": [],
    })


def _enriched(payload: dict[str, Any], case: ReplayCase) -> dict[str, Any]:
    """모델 응답 → 애플리케이션이 정체성·리터럴·semantic_ir 을 붙인 플랜."""
    return attach_campaign_query_plan_v4_identity(
        json.loads(json.dumps(payload, ensure_ascii=False)),
        case.query,
        current_date=CURRENT_DATE,
    )


def _fixture_default_period() -> default_period_policy.DefaultPeriod:
    """이 배포가 켜 둔 기본 창(실측은 최근 5일).

    env 를 읽지 않는다 — 정책 값이 테스트 밖 설정에 흔들리면 이 회귀가 배포마다 다른 것을
    증명하게 된다. 값 자체는 계약의 일부가 아니고, '사용자 값과 다른 창'이면 충분하다.
    """
    return default_period_policy.DefaultPeriod(
        value=5, unit="day", origin="replay-fixture", expression="최근"
    )


@dataclass
class PeriodRounds:
    """정책 두 라운드를 실행한 결과와 그 라운드에 넘어간 지시문들."""

    plan: dict[str, Any]
    instructions: list[str]
    logs: list[tuple[str, dict[str, Any]]]


def _run_period_rounds(case: ReplayCase) -> PeriodRounds:
    """graph_rag 와 **같은 순서**로 기간 정책 두 단계를 조립한다.

    순서의 출처는 :func:`graph_rag.structure_campaign_query_plan_once` 다 — 교정
    (:func:`targeting_policy.resolve_stated_period`) 이 먼저, 기본값
    (:func:`default_period_policy.apply_default_period`) 이 그다음이다. graph_rag 전체를
    돌리면 LLM·검색·임베딩이 따라오므로 여기서는 그 두 호출만 같은 인자 모양으로 재현한다.

    ``restructure`` 는 실측을 그대로 흉내 낸다: 교정 라운드는 표현을 못 내고(로그의
    ``candidate_has_no_expression``), 기본기간 라운드가 정답 후보를 낸다.
    """
    instructions: list[str] = []
    logs: list[tuple[str, dict[str, Any]]] = []

    def restructure(instruction: str) -> dict[str, Any]:
        instructions.append(instruction)
        payload = (
            _false_period_gap_payload(case)
            if len(instructions) == 1
            else _stated_period_answer_payload(case)
        )
        return _enriched(payload, case)

    plan = _enriched(_false_period_gap_payload(case), case)
    plan = targeting_policy.resolve_stated_period(
        plan,
        query=case.query,
        current_date=CURRENT_DATE,
        restructure=restructure,
        requirement_key=AUDIENCE_REQUIREMENT_KEY,
        missing_period_issues=default_period_policy.missing_period_issues,
        write_log=lambda name, payload: logs.append((name, payload)),
    )
    plan = default_period_policy.apply_default_period(
        plan,
        query=case.query,
        current_date=CURRENT_DATE,
        period=_fixture_default_period(),
        restructure=restructure,
        write_log=lambda name, payload: logs.append((name, payload)),
    )
    return PeriodRounds(plan=plan, instructions=instructions, logs=logs)


def _compile(plan: dict[str, Any]) -> dict[str, Any]:
    """플랜 → 실행 SQL 후보. sql_guard 까지 통과해야 반환한다."""
    candidate = graph_rag.compile_executable_plan(dict(plan))
    assert candidate is not None
    assert candidate["id"] == "sql_template:event_expression"
    assert candidate["validation"]["issues"] == []
    guarded = sql_guard.validate_sql(
        candidate["sql"],
        allowed_tables=set(candidate["tables"]),
        default_limit=None,
        dialect="tsql",
        schema_columns=sql_guard.load_schema_columns(),
    )
    assert guarded["is_valid"], guarded["issues"]
    return candidate


def _declared_time_predicate(case: ReplayCase) -> str:
    """이 사건의 기간 술어를 **카탈로그 선언에서 파생**한다.

    컬럼 이름을 손으로 박으면 다른 배포에서 컬럼이 바뀔 때 테스트가 거짓으로 red 가 된다.
    별칭도 적지 않는다(별칭은 컴파일러가 정한다) — 컬럼 앞의 점만 남긴다.
    """
    event = audience_runtime.resolve_audience_catalog().compiler_events[case.source]
    grain = event_compiler.TIME_GRAINS[event.time_format]
    assert grain.rolling_cutoff is not None, (
        f"{case.source} 는 {grain.unit} 칸 컬럼이라 롤링 창을 표현할 수 없다 — "
        "이 재생 사례로 쓸 수 없는 소스다."
    )
    cutoff = grain.rolling_cutoff(graph_rag._member_dialect(), case.window.days)
    return f".{event.time_column} >= {cutoff}"


# ── 재생 재료가 실제 선언과 맞는가 ────────────────────────────────────────────────


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.source)
def test_every_replay_case_uses_a_declared_event_source_and_a_rolling_capable_grain(
    case: ReplayCase,
) -> None:
    """지어낸 조합으로 회귀를 세우지 않는다 — 소스도 grain 도 카탈로그가 선언한 것이다."""
    catalog = audience_runtime.resolve_audience_catalog()
    assert case.source in catalog.compiler_events, (
        f"{case.source} 는 카탈로그에 없는 소스다."
    )
    event = catalog.compiler_events[case.source]
    assert event.time_column, f"{case.source} 는 시각 컬럼을 선언하지 않았다."
    grain = event_compiler.TIME_GRAINS[event.time_format]
    assert grain.rolling_cutoff is not None
    assert case.unit in event_ir.WINDOW_UNITS


# ── [C] 거짓 결핍은 첫 호출 안에서 재시도를 부른다 ──────────────────────────────


def test_a_falsely_reported_period_gap_is_retried_inside_the_first_structuring_call() -> None:
    """실측 1차 응답이 그대로 '성공'으로 끝나지 않는다.

    판정의 출처는 :func:`canonical_audience_claims.missing_field_cause_records` 다. 신고 구간
    ('최근')과 값 구간('30일')은 서로 다른 문자 구간이라 포함 관계로는 만나지 않는다 — 그래서
    시간 절 판정자에게 한 번 더 물어 ``model_omission`` 으로 승격한다. 그 원인 값이
    :func:`query_structurer.structurer._audience_repair_error` 의 재시도 트리거다.
    """
    calls: list[list[dict[str, str]]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        payload = (
            _false_period_gap_payload(LIVE)
            if len(calls) == 1
            else _stated_period_answer_payload(LIVE)
        )
        return json.dumps(payload, ensure_ascii=False)

    plan = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=LIVE.query,
        context=StructuringContext(current_date=CURRENT_DATE, timezone="Asia/Seoul"),
    ))

    assert len(calls) == 2, "거짓 결핍 신고가 첫 호출에서 그대로 받아들여졌다."
    names = [name for name, _ in events]
    assert names == [
        "campaign_query_plan_v4_attempt_failed",
        "campaign_query_plan_v4_success",
    ]
    failure = next(payload for name, payload in events if name.endswith("attempt_failed"))
    assert "audience.period" in failure["error"]

    # 재시도를 부른 **결정론 판정**도 함께 고정한다. 이 값이 user_omission 이면 같은 응답이
    # 되묻기로 종결된다(실측).
    causes = _enriched(_false_period_gap_payload(LIVE), LIVE)["semantic_ir"][
        "missing_field_causes"
    ]
    assert [record["field"] for record in causes] == ["audience.period"]
    assert [record["cause"] for record in causes] == [
        semantic_outcome.CAUSE_MODEL_OMISSION
    ]

    assert plan["semantic_ir"]["status"] == "resolved"


def test_a_repeated_false_period_gap_spends_the_whole_retry_budget() -> None:
    """같은 거짓 결핍만 계속 오면 재시도가 **소진**된다 — 한 번만 걸리는 특례가 아니다."""
    calls: list[int] = []
    events: list[str] = []

    def complete(_messages: list[dict[str, str]]) -> str:
        calls.append(len(calls))
        return json.dumps(_false_period_gap_payload(LIVE), ensure_ascii=False)

    LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, _payload: events.append(name),
    ).structure(QueryStructuringInput(
        query=LIVE.query,
        context=StructuringContext(current_date=CURRENT_DATE, timezone="Asia/Seoul"),
    ))

    assert len(calls) == 3, "재시도가 걸리지 않아 첫 응답이 곧 결말이 됐다."
    assert events.count("campaign_query_plan_v4_attempt_failed") == 3
    assert events[-1] == "campaign_query_plan_v4_fallback"


# ── [B] 기본기간 라운드의 정답 후보가 채택된다 ────────────────────────────────────


def test_the_answer_from_the_default_period_round_is_adopted_instead_of_a_clarification() -> None:
    """교정 라운드가 실패해도 다음 라운드의 정답이 살아남는다.

    예전에는 이 후보가 ``candidate_ignored_the_default_window`` 로 폐기됐다 — 기본 창(5일)이
    후보에 없다는 이유였고, 그 창은 애초에 요구되면 안 되는 것이었다(원문이 30일을 말했다).
    """
    rounds = _run_period_rounds(LIVE)
    requirement = rounds.plan[AUDIENCE_REQUIREMENT_KEY]

    assert requirement["expression"] is not None, "정답 후보가 폐기됐다(되묻기로 회귀)."
    assert requirement["issues"] == []
    assert default_period_policy.missing_period_issues(rounds.plan) == []
    assert rounds.plan["semantic_ir"]["status"] == "resolved"
    assert rounds.plan["semantic_ir"]["failure_kind"] is None

    # 교정 라운드는 실제로 실패했다(실측과 같은 사유). 그 실패가 결말이 아니라는 것이 계약이다.
    assert [
        (name, payload.get("reason")) for name, payload in rounds.logs
    ] == [
        ("stated_period_correction", "candidate_has_no_expression"),
        ("audience_default_period_policy", None),
    ]
    # 채택된 플랜에는 되묻기 판정이 남아 있지 않다.
    assert [
        decision["decision"] for decision in targeting_policy.decisions(rounds.plan)
    ].count(targeting_policy.DECISION_CLARIFY) == 0

    views = event_ir.existence_views(
        event_ir.condition_from_dict(rounds.plan["event_expression"]["expression"])
    )
    assert [(view.source, view.negated) for view in views] == [(LIVE.source, False)]
    assert isinstance(views[0].window, event_ir.RollingWindow)
    assert (views[0].window.value, views[0].window.unit) == (LIVE.value, LIVE.unit)


def test_the_adopted_window_is_reported_as_the_users_value_not_a_deployment_default() -> None:
    """값의 출처가 사용자이므로 기본값 영수증도 ``policy_applied`` 도 붙지 않는다.

    붙이면 응답을 읽는 쪽이 사용자가 말한 30일을 제품 설정이 고른 값으로 보고받는다. 그 라운드가
    기본 창을 **지시하지도 않았다**는 사실까지 함께 고정한다 — 영수증이 없는 이유가 우연이 아니라
    지시문이 애초에 교정문이었기 때문이다.
    """
    rounds = _run_period_rounds(LIVE)

    assert default_period_policy.applied_period_receipt(rounds.plan) is None
    assert default_period_policy.DEFAULT_PERIOD_KEY not in rounds.plan
    assert rounds.plan["semantic_ir"]["status"] != "policy_applied"
    assert rounds.plan["semantic_ir"]["policy_applications"] == []

    # 두 번째 라운드의 지시문은 교정문 하나다(기본 창 지시문이 아니다). 문구는 소유 모듈에서
    # 파생해 대조한다 — 여기에 같은 문장을 다시 적으면 문구가 두 벌이 된다.
    literal_bindings = rounds.plan["literal_bindings"]
    stated, unstated = targeting_policy.split_period_issues(
        LIVE.query,
        default_period_policy.missing_period_issues(
            _enriched(_false_period_gap_payload(LIVE), LIVE)
        ),
        literal_bindings,
    )
    assert unstated == []
    assert len(rounds.instructions) == 2
    assert rounds.instructions[1] == targeting_policy.stated_period_instruction(stated)
    assert (
        default_period_policy.render_default_period_instruction(
            _fixture_default_period(), []
        )
        not in rounds.instructions[1]
    )

    # 기본값 로그도 '채택했지만 적용하지 않았다'로 남는다.
    _, applied = next(
        (name, payload)
        for name, payload in rounds.logs
        if name == "audience_default_period_policy"
    )
    assert applied["adopted"] is True
    assert applied["applied"] is False
    assert applied["stated_period_issues"] == 1


# ── [종단] 채택된 플랜이 선언된 시간 컬럼 위의 SQL 이 된다 ────────────────────────


def test_the_adopted_plan_compiles_to_sql_over_the_declared_event_time_column() -> None:
    """되묻기 대신 실제 SQL 이 나오고, 그 창이 카탈로그가 선언한 시간 컬럼 위에 선다."""
    rounds = _run_period_rounds(LIVE)
    candidate = _compile(rounds.plan)
    sql = candidate["sql"]

    event = audience_runtime.resolve_audience_catalog().compiler_events[LIVE.source]
    assert event.table in sql
    assert _declared_time_predicate(LIVE) in sql, sql

    # 시도2가 골랐던 문자열 시각 컬럼은 술어에 등장하지 않는다(그 컬럼으로는 컴파일되지 않는다).
    string_column = audience_runtime.resolve_audience_catalog().fields[
        LIVE_ATTEMPT_TIME_SYMBOL
    ].compiler_field.column
    assert f"{string_column} >=" not in sql

    # 조건이 조용히 사라지지도, 의미가 뒤집히지도 않았다.
    dropped = graph_rag._deterministic_dropped_conditions(LIVE.query, rounds.plan)
    assert dropped == []
    assert graph_rag._verify_sql_semantic_invariants(
        LIVE.query, rounds.plan, sql, dropped
    ) == {"ran": True, "ok": True, "issues": []}


# ── [A] 프롬프트가 그 소스의 파생 시각 필드를 광고한다 ────────────────────────────


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.source)
def test_the_structurer_prompt_advertises_the_time_field_of_the_sources_this_replay_uses(
    case: ReplayCase,
) -> None:
    """시도2가 문자열 컬럼을 고를 수밖에 없던 상태가 사라졌다.

    카탈로그 전역 불변식은 ``tests/test_audience_catalog_time_field_guidance.py`` 가 소유한다.
    여기서는 이 재생이 쓰는 소스만 본다 — 광고된 심볼이 있어야 정답 후보가 나올 수 있다.
    """
    guidance = audience_runtime.audience_catalog_guidance()
    field_id = f"{case.source}.{event_ir.TIME_FIELD_SUFFIX}"
    assert f"- {field_id}:" in guidance, (
        f"{field_id} 가 안내에 없다 — 모델은 광고되지 않은 심볼을 쓸 수 없어 "
        "문자열 시각 컬럼으로 우회한다(실측 시도2)."
    )

    catalog = audience_runtime.resolve_audience_catalog()
    compiled = event_compiler.resolve_fields(dict(catalog.compiler_events))
    assert compiled[field_id].data_type in event_compiler.DATE_DATA_TYPES

    # 대조군: 실측이 고른 심볼은 선언돼 있지만 시각 타입이 아니다. 이것이 시도2가 죽은 이유이고,
    # 그래서 파생 필드를 광고하지 않으면 모델에게 남는 선택지가 없다.
    declared = catalog.fields[LIVE_ATTEMPT_TIME_SYMBOL]
    assert declared.compiler_field.data_type not in event_compiler.DATE_DATA_TYPES


# ── [일반성] 한 문장 전용 회귀가 아니다 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    GENERALITY_CASES,
    ids=lambda case: f"{case.source}-{case.value}{case.unit}",
)
def test_the_same_correction_holds_for_other_units_and_other_event_sources(
    case: ReplayCase,
) -> None:
    """단위(개월·주)와 사건 소스가 달라도 같은 체인이 같은 결말을 낸다.

    판정 재료는 recency 표지 어휘와 애플리케이션이 뽑은 duration 리터럴뿐이므로, 문장이 늘어도
    분기가 늘지 않아야 한다. 그 사실을 소스별로 확인한다.
    """
    causes = _enriched(_false_period_gap_payload(case), case)["semantic_ir"][
        "missing_field_causes"
    ]
    assert [record["cause"] for record in causes] == [
        semantic_outcome.CAUSE_MODEL_OMISSION
    ]

    rounds = _run_period_rounds(case)
    assert rounds.plan[AUDIENCE_REQUIREMENT_KEY]["expression"] is not None
    assert rounds.plan["semantic_ir"]["status"] == "resolved"
    assert default_period_policy.applied_period_receipt(rounds.plan) is None

    sql = _compile(rounds.plan)["sql"]
    assert _declared_time_predicate(case) in sql, sql
