"""Resolution Policy · Clarification 파이프라인의 동작 계약.

여기서 고정하는 것은 하나다: **정책으로 안전하게 정할 수 있는 것은 묻지 않고, 결과가 크게
달라지는 것은 반드시 묻는다.** 그리고 답은 그 자리 하나만 고친다.

시각·기간처럼 배포 설정에 달린 값은 env 를 테스트가 직접 세운다(고정 입력, §44).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import event_ir
from resolution import (
    CanonicalRequest,
    ClarificationAnswer,
    ClarificationInvariantViolation,
    NeedsClarification,
    ResolutionAction,
    ResolutionMode,
    ResolutionPolicy,
    ResolutionProvenance,
    ResolvedRequest,
    UnsupportedResolution,
    detect_semantic_issues,
    legacy_questions,
    parse_answers,
    plan_clarifications,
    resolution_payload,
    resolve_request,
    resolved_config,
)
from resolution.config import ResolutionConfigError, parse_config
from resolution.issues import (
    AMBIGUOUS_COMPARISON_GRAIN,
    GRAIN_ROW,
    GRAIN_SUBJECT,
    MISSING_ENTITY_VALUE,
    MISSING_RECENT_PERIOD,
    UNSUPPORTED_SEMANTICS,
)
from resolution.policy import ResolutionDecisions

QUERY_AMOUNT = "최근 20만원 이상 구매한 회원"
AMOUNT_EVIDENCE = event_ir.Evidence(text="20만원 이상 구매", start=3, end=13)


# ── 재료 ─────────────────────────────────────────────────────────────────────────


def _subject_threshold_expression(window: event_ir.TimeWindow | None = None) -> event_ir.Condition:
    """회원별 합계 임계 — '20만원 이상 구매'가 subject grain 으로 굳은 모양."""

    return event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.event_relation("purchase", window),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=200000),
        evidence=AMOUNT_EVIDENCE,
    )


def _windowless_existence(query: str) -> event_ir.Condition:
    return event_ir.Exists(
        relation=event_ir.event_relation("purchase"),
        evidence=event_ir.Evidence(text=query[:4], start=0, end=4),
    )


def _request(expression: event_ir.Condition | None, *, query: str = QUERY_AMOUNT, issues: Any = ()) -> CanonicalRequest:
    return CanonicalRequest(
        query=query,
        expression=None if expression is None else expression.to_dict(),
        reported_issues=tuple(issues),
    )


def _report(code: str, argument: str, *, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "code": code,
        "argument": argument,
        "message": f"{argument} 를 확정하지 못했습니다.",
        "evidence": {"text": text, "start": start, "end": end},
    }


# ── 설정 ─────────────────────────────────────────────────────────────────────────


def test_the_shipped_config_declares_only_kinds_that_exist() -> None:
    config = resolved_config()

    assert config.mode is ResolutionMode.SAFE_DEFAULTS
    assert config.auto_resolution_allowed(MISSING_RECENT_PERIOD)
    assert config.clarification_required(AMBIGUOUS_COMPARISON_GRAIN)


def test_an_undeclared_issue_kind_in_config_is_an_error_not_a_silent_skip() -> None:
    with pytest.raises(ResolutionConfigError):
        parse_config({"allowed_auto_resolution": {"missing_moon_phase": True}}, origin="test")


def test_an_unsupported_kind_cannot_be_opened_for_auto_resolution() -> None:
    """사용자가 답해도 안 열리는 것을 정책이 대신 채울 수는 더더욱 없다(§25)."""

    with pytest.raises(ResolutionConfigError):
        parse_config({"allowed_auto_resolution": {UNSUPPORTED_SEMANTICS: True}}, origin="test")


# ── 감지 ─────────────────────────────────────────────────────────────────────────


def test_an_unstated_threshold_grain_is_detected_as_ambiguous() -> None:
    issues = detect_semantic_issues(_request(_subject_threshold_expression()), resolved_config())

    assert [issue.kind for issue in issues] == [AMBIGUOUS_COMPARISON_GRAIN]
    assert {candidate.value for candidate in issues[0].candidates} == {GRAIN_ROW, GRAIN_SUBJECT}


def test_a_stated_grain_is_not_an_ambiguity() -> None:
    """원문이 grain 을 말했으면 확정된 의미다 — 말한 것을 되묻지 않는다."""

    stated = "최근 총 구매금액이 20만원 이상인 회원"
    issues = detect_semantic_issues(
        _request(_subject_threshold_expression(), query=stated), resolved_config()
    )

    assert [issue.kind for issue in issues] == []


def test_a_period_report_becomes_a_typed_recent_period_issue() -> None:
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    issues = detect_semantic_issues(
        _request(None, query=query, issues=[report]), resolved_config()
    )

    assert [issue.kind for issue in issues] == [MISSING_RECENT_PERIOD]
    assert issues[0].slot == "audience.period"
    assert issues[0].evidence is not None and issues[0].evidence.text == "최근"


def test_a_declared_entity_argument_becomes_a_missing_entity_issue() -> None:
    query = "특정 상품을 구매한 회원"
    report = _report("missing_argument", "product", text="특정 상품", start=0, end=5)
    issues = detect_semantic_issues(
        _request(None, query=query, issues=[report]), resolved_config()
    )

    assert [issue.kind for issue in issues] == [MISSING_ENTITY_VALUE]
    assert issues[0].entity_type == "product"


def test_the_same_issue_keeps_its_identity_across_rounds() -> None:
    """id 가 순번이면 질문 하나가 늘고 줄 때 답이 엉뚱한 자리에 붙는다."""

    config = resolved_config()
    first = detect_semantic_issues(_request(_subject_threshold_expression()), config)
    second = detect_semantic_issues(_request(_subject_threshold_expression()), config)

    assert [issue.issue_id for issue in first] == [issue.issue_id for issue in second]


# ── 판정 ─────────────────────────────────────────────────────────────────────────


def test_a_configured_default_period_is_resolved_without_asking(monkeypatch: Any) -> None:
    """사례 1 — '최근 구매한 회원'은 설정된 기본 기간으로 SQL 까지 간다."""

    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    request = _request(_windowless_existence(query), query=query, issues=[report])

    outcome = resolve_request(request, policy=ResolutionPolicy())

    assert isinstance(outcome, ResolvedRequest)
    assert outcome.resolution == "assumed"
    assumption = outcome.assumptions[0]
    assert assumption.code == "DEFAULT_RECENT_PERIOD"
    assert assumption.provenance is ResolutionProvenance.POLICY_DEFAULT
    assert assumption.value == {"type": "rolling", "value": 30, "unit": "day"}
    # 실제로 창이 들어갔다 — 영수증만 남고 IR 은 그대로인 상태를 허용하지 않는다.
    assert "time_filter" in str(outcome.request.expression)
    assert outcome.request.reported_issues == ()


def test_without_a_configured_period_the_deficit_is_asked_not_invented(monkeypatch: Any) -> None:
    monkeypatch.delenv("AUDIENCE_DEFAULT_PERIOD", raising=False)
    monkeypatch.delenv("AUDIENCE_QUALITATIVE_DEFAULTS", raising=False)
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    request = _request(_windowless_existence(query), query=query, issues=[report])

    outcome = resolve_request(request, policy=ResolutionPolicy())

    assert isinstance(outcome, NeedsClarification)
    assert [question.code for question in outcome.plan.questions] == ["MISSING_RECENT_PERIOD"]


def test_a_missing_entity_value_is_never_guessed() -> None:
    """사례 2 — 상품을 임의로 고르지 않는다."""

    query = "특정 상품을 구매한 회원"
    report = _report("missing_argument", "product", text="특정 상품", start=0, end=5)
    outcome = resolve_request(_request(None, query=query, issues=[report]))

    assert isinstance(outcome, NeedsClarification)
    question = outcome.plan.questions[0]
    assert question.code == "MISSING_ENTITY_VALUE"
    assert question.allow_free_text is True
    assert question.options == ()
    assert outcome.assumptions == ()


def test_an_ambiguous_grain_is_asked_not_defaulted() -> None:
    """사례 3 — 임의 subject grain 기본값 금지."""

    outcome = resolve_request(_request(_subject_threshold_expression()))

    assert isinstance(outcome, NeedsClarification)
    question = outcome.plan.questions[0]
    assert question.code == "AMBIGUOUS_AMOUNT_GRAIN"
    assert [option.option_id for option in question.options] == [GRAIN_ROW, GRAIN_SUBJECT]


def test_a_capability_gap_is_unsupported_and_never_a_question() -> None:
    """사례 5 — 사용자가 아무리 설명해도 자산이 생기지 않는다."""

    query = "정상에서 휴면으로 바뀐 회원"
    report = _report("unsupported_semantics", "state_history", text=query, start=0, end=len(query))
    outcome = resolve_request(_request(None, query=query, issues=[report]))

    assert isinstance(outcome, UnsupportedResolution)
    assert [issue.kind for issue in outcome.issues] == [UNSUPPORTED_SEMANTICS]
    payload = resolution_payload(outcome, mode="safe_defaults")
    assert payload["questions"] == []


# ── 질문 최소화 ──────────────────────────────────────────────────────────────────


def test_a_policy_resolved_deficit_is_not_asked_again(monkeypatch: Any) -> None:
    """사례 3 확장 — 기간은 정책이 채우고 grain 만 묻는다(질문 1개)."""

    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근에 20만원 이상 구매한 회원"
    evidence = event_ir.Evidence(text="20만원 이상 구매", start=5, end=15)
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.event_relation("purchase"),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=200000),
        evidence=evidence,
    )
    report = _report("missing_argument", "period", text="최근", start=0, end=2)

    outcome = resolve_request(
        CanonicalRequest(
            query=query, expression=expression.to_dict(), reported_issues=(report,)
        )
    )

    assert isinstance(outcome, NeedsClarification)
    assert [question.code for question in outcome.plan.questions] == ["AMBIGUOUS_AMOUNT_GRAIN"]
    assert [assumption.code for assumption in outcome.assumptions] == ["DEFAULT_RECENT_PERIOD"]


# ── 답변 적용 ────────────────────────────────────────────────────────────────────


def test_a_grain_answer_changes_only_that_slot() -> None:
    """사례 4 — 기간·금액·사건은 그대로, grain 만 바뀐다(§29)."""

    window = event_ir.RollingWindow(value=30, unit="day")
    request = _request(_subject_threshold_expression(window))
    first = resolve_request(request)
    assert isinstance(first, NeedsClarification)

    answer = ClarificationAnswer(issue_id=first.issues[0].issue_id, option_id=GRAIN_ROW)
    second = resolve_request(request, answers=[answer])

    assert isinstance(second, ResolvedRequest)
    rendered = json.dumps(second.request.expression, ensure_ascii=False)
    assert '"value": 30' in rendered and '"unit": "day"' in rendered  # 기간 그대로
    assert '"value": 200000' in rendered  # 금액 그대로
    assert '"name": "purchase"' in rendered  # 사건 그대로
    assert '"type": "aggregate"' not in rendered  # grain 만 row 로 내려갔다
    assumption = second.assumptions[0]
    assert assumption.slot == "comparison.grain"
    assert assumption.provenance is ResolutionProvenance.USER_CLARIFICATION


def test_confirming_the_realized_grain_settles_without_touching_the_tree() -> None:
    request = _request(_subject_threshold_expression())
    first = resolve_request(request)
    assert isinstance(first, NeedsClarification)

    answer = ClarificationAnswer(issue_id=first.issues[0].issue_id, option_id=GRAIN_SUBJECT)
    second = resolve_request(request, answers=[answer])

    assert isinstance(second, ResolvedRequest)
    assert second.request.expression == request.expression
    assert second.assumptions[0].value == GRAIN_SUBJECT


def test_a_free_text_period_answer_is_read_by_a_slot_scoped_resolver(monkeypatch: Any) -> None:
    monkeypatch.delenv("AUDIENCE_DEFAULT_PERIOD", raising=False)
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    request = _request(_windowless_existence(query), query=query, issues=[report])
    first = resolve_request(request)
    assert isinstance(first, NeedsClarification)

    answer = ClarificationAnswer(issue_id=first.issues[0].issue_id, text="3개월")
    second = resolve_request(request, answers=[answer])

    assert isinstance(second, ResolvedRequest)
    assert second.assumptions[0].value == {"type": "rolling", "value": 3, "unit": "month"}


def test_a_free_text_answer_to_a_choice_question_is_refused_not_guessed() -> None:
    request = _request(_subject_threshold_expression())
    first = resolve_request(request)
    assert isinstance(first, NeedsClarification)

    answer = ClarificationAnswer(issue_id=first.issues[0].issue_id, text="아무거나")
    second = resolve_request(request, answers=[answer])

    assert isinstance(second, NeedsClarification)
    assert second.unapplied_answers == ((first.issues[0].issue_id, "free_text_not_allowed"),)


def test_an_answer_for_an_issue_that_no_longer_exists_is_reported_not_applied() -> None:
    """문장이 바뀌어 결핍이 사라졌으면 옛 답은 조용히 어딘가에 적용되지 않는다."""

    request = _request(_subject_threshold_expression())
    first = resolve_request(request)
    assert isinstance(first, NeedsClarification)

    stale = ClarificationAnswer(issue_id="0" * 16, option_id=GRAIN_ROW)
    second = resolve_request(request, answers=[stale])

    assert isinstance(second, NeedsClarification)
    assert second.unapplied_answers == (("0" * 16, "question_not_in_plan"),)


# ── 모드 ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ResolutionMode.STRICT, NeedsClarification),
        (ResolutionMode.SAFE_DEFAULTS, ResolvedRequest),
        (ResolutionMode.BEST_EFFORT, ResolvedRequest),
    ],
)
def test_mode_decides_whether_a_low_risk_default_is_applied(
    monkeypatch: Any, mode: ResolutionMode, expected: type
) -> None:
    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    request = _request(_windowless_existence(query), query=query, issues=[report])

    outcome = resolve_request(
        request, policy=ResolutionPolicy(resolved_config().with_mode(mode))
    )

    assert isinstance(outcome, expected)


@pytest.mark.parametrize("mode", list(ResolutionMode))
def test_no_mode_silently_settles_a_high_risk_grain_ambiguity(mode: ResolutionMode) -> None:
    """§34-H — BEST_EFFORT 도 grain 은 묻는다."""

    outcome = resolve_request(
        _request(_subject_threshold_expression()),
        policy=ResolutionPolicy(resolved_config().with_mode(mode)),
    )

    assert isinstance(outcome, NeedsClarification)


# ── 고정점 ───────────────────────────────────────────────────────────────────────


def test_resolution_reaches_a_fixed_point_and_reports_its_rounds(monkeypatch: Any) -> None:
    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근 구매한 회원"
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    outcome = resolve_request(
        _request(_windowless_existence(query), query=query, issues=[report])
    )

    assert isinstance(outcome, ResolvedRequest)
    # 1 라운드: 결핍 감지 → 자동 확정, 2 라운드: 결핍 없음.
    assert outcome.rounds == 2


def test_an_exact_request_needs_no_assumption() -> None:
    query = "최근 30일 구매한 회원"
    expression = event_ir.Exists(
        relation=event_ir.event_relation(
            "purchase", event_ir.RollingWindow(value=30, unit="day")
        ),
        evidence=event_ir.Evidence(text=query[:7], start=0, end=7),
    )
    outcome = resolve_request(_request(expression, query=query))

    assert isinstance(outcome, ResolvedRequest)
    assert outcome.resolution == "exact"
    assert outcome.assumptions == ()


# ── 응답 계약 ────────────────────────────────────────────────────────────────────


def test_the_response_block_separates_questions_from_assumptions(monkeypatch: Any) -> None:
    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근에 20만원 이상 구매한 회원"
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.event_relation("purchase"),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=200000),
        evidence=event_ir.Evidence(text="20만원 이상 구매", start=5, end=15),
    )
    report = _report("missing_argument", "period", text="최근", start=0, end=2)
    outcome = resolve_request(
        CanonicalRequest(query=query, expression=expression.to_dict(), reported_issues=(report,))
    )
    payload = resolution_payload(outcome, mode="safe_defaults", answer_count=0)

    assert payload["status"] == "needs_clarification"
    assert [item["code"] for item in payload["assumptions"]] == ["DEFAULT_RECENT_PERIOD"]
    assert [item["code"] for item in payload["questions"]] == ["AMBIGUOUS_AMOUNT_GRAIN"]
    assert payload["questions"][0]["options"][0]["id"] == GRAIN_ROW


def test_legacy_string_questions_are_derived_from_typed_ones() -> None:
    outcome = resolve_request(_request(_subject_threshold_expression()))
    assert isinstance(outcome, NeedsClarification)

    rendered = legacy_questions(outcome.plan)

    assert len(rendered) == 1
    assert "한 번의 주문 금액" in rendered[0] and "기간 내 총 구매금액" in rendered[0]


def test_answers_are_parsed_from_the_api_boundary_shape() -> None:
    parsed = parse_answers(
        [
            {"issue_id": "abc", "option_id": "row"},
            {"issueId": "def", "text": " 로얄캐닌 "},
            {"option_id": "row"},
            "not-an-answer",
        ]
    )

    assert [item.issue_id for item in parsed] == ["abc", "def"]
    assert parsed[1].text == "로얄캐닌"


# ── 질문 상한 ────────────────────────────────────────────────────────────────────


def test_a_question_cap_reports_what_it_deferred() -> None:
    """조용한 절단을 만들지 않는다 — 남긴 수가 응답에 함께 나간다."""

    query = "특정 브랜드의 특정 상품을 구매한 회원"
    reports = [
        _report("missing_argument", "brand", text="특정 브랜드", start=0, end=6),
        _report("missing_argument", "product", text="특정 상품", start=8, end=13),
    ]
    config = resolved_config()
    request = _request(None, query=query, issues=reports)
    issues = detect_semantic_issues(request, config)
    decisions = ResolutionDecisions(
        decisions=tuple(
            ResolutionPolicy(config)._decide(request, issue) for issue in issues
        )
    )

    plan = plan_clarifications(issues, decisions, max_questions=1)

    assert len(plan.questions) == 1
    assert plan.deferred_count == 1


def test_the_planner_refuses_a_decision_it_did_not_ask_for() -> None:
    """§34-B — ASK_USER 아닌 결정이 질문 입력에 들어오면 구조적으로 막힌다."""

    request = _request(_subject_threshold_expression())
    issues = detect_semantic_issues(request, resolved_config())
    policy = ResolutionPolicy()
    decisions = policy.resolve(request, issues)
    forged = ResolutionDecisions(
        decisions=tuple(
            type(item)(
                issue_id=item.issue_id,
                action=ResolutionAction.AUTO_RESOLVE,
                risk=item.risk,
                kind=item.kind,
            )
            for item in decisions
        )
    )

    with pytest.raises(ClarificationInvariantViolation):
        plan_clarifications(issues, forged)
