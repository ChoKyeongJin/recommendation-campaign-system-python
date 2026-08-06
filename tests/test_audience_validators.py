"""주입된 검증기 계약 — 요구 계층은 **모양만** 알고, 판정은 도메인이 한다.

`_derive_audience_execution` 이 217줄이었던 이유의 절반은 이 넷이 인라인이었기 때문이다.
그것을 이름 있는 단위로 떼어낸 뒤 지켜야 할 것이 셋 있다.

1. 넷이 각자 **자기 판정만** 낸다(이름과 산출이 맞는다).
2. **순서가 계약이다** — `semantic_ir.message` 는 첫 issue 의 문장이므로 순서가 바뀌면
   사용자가 보는 첫 문장이 바뀐다.
3. 요구 계층이 실제로 그것들을 **돌린다** — 주입만 받고 부르지 않으면 검증이 통째로
   사라지고, 그 상태는 "전부 통과"로 보인다(가장 나쁜 실패 모양).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from query_pipeline_fixtures import id_factory, resolution_context, run  # noqa: E402

import audience_validators  # noqa: E402
import event_ir  # noqa: E402
from query_pipeline.event_query.event_ir_bridge import from_wire  # noqa: E402
from query_pipeline.event_query.expressions import (  # noqa: E402
    EventExpression,
    SourceEvidence,
)
from query_pipeline.requirement.issues import IssueKind, RequirementIssue  # noqa: E402
from query_pipeline.requirement.models import (  # noqa: E402
    AudienceRequirement,
    IntentKind,
    ProposedExpression,
    RequirementIntent,
    RequirementSource,
)
from query_pipeline.requirement.resolver import DefaultRequirementResolver  # noqa: E402
from query_pipeline.requirement.validation import (  # noqa: E402
    ISSUE_CODE_KINDS,
    issue_from_report,
    report_from_issue,
)
from query_structurer.audience_execution import run_audience_resolver  # noqa: E402

QUERY = "최근 30일 동안 구매한 회원을 찾아줘"
SPAN = "최근 30일 동안 구매한"


def _wire(source: str, *, window: dict | None = None) -> dict:
    where: dict = {
        "type": "time_filter",
        "field": {"type": "field", "name": f"{source}.occurred_at"},
        "window": window or {"type": "rolling", "value": 30, "unit": "day"},
    }
    return {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": source},
            "where": where,
        },
        "evidence": {"text": SPAN, "start": 0, "end": len(SPAN)},
    }


def _expression(source: str = "purchase") -> EventExpression:
    return from_wire(_wire(source))


def _requirement(expression: EventExpression, text: str = QUERY) -> AudienceRequirement:
    from query_pipeline.event_query.event_ir_bridge import to_wire

    return AudienceRequirement(
        id="req-1",
        version="1",
        intent=RequirementIntent(kind=IntentKind.FIND),
        expression=ProposedExpression(payload=to_wire(expression)),
        source=RequirementSource(text=text),
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


# ── 1. 각자 자기 판정만 낸다 ──────────────────────────────────────────────────────


def test_catalog_symbol_validator_names_the_unregistered_symbol() -> None:
    issues = audience_validators.CatalogSymbolValidator().validate(
        _expression("not_a_declared_event"), query=QUERY, literals=[]
    )
    assert issues, "등록되지 않은 사건이 통과했다."
    assert all(issue.kind is IssueKind.UNSUPPORTED for issue in issues)
    assert any("not_a_declared_event" in issue.message for issue in issues)


def test_catalog_symbol_validator_is_silent_on_declared_symbols() -> None:
    assert (
        audience_validators.CatalogSymbolValidator().validate(
            _expression(), query=QUERY, literals=[]
        )
        == ()
    )


def test_temporal_validator_reports_a_bare_recency_marker() -> None:
    """값 없는 '최근'은 결핍이다 — 그대로 두면 전체 이력 폴백이 된다."""
    query = "최근 구매한 회원을 찾아줘"
    span = "최근 구매한"
    wire = {
        "type": "exists",
        "relation": {"type": "source", "name": "purchase"},
        "evidence": {"text": span, "start": 0, "end": len(span)},
    }
    issues = audience_validators.TemporalSpanValidator().validate(
        from_wire(wire), query=query, literals=[]
    )
    codes = {report_from_issue(issue)[0] for issue in issues}
    assert "missing_argument" in codes
    bare = next(issue for issue in issues if issue.evidence is not None
                and issue.evidence.text == "최근")
    assert (bare.evidence.start, bare.evidence.end) == (0, 2)  # type: ignore[union-attr]


def test_temporal_validator_is_silent_when_the_window_is_present() -> None:
    assert (
        audience_validators.TemporalSpanValidator().validate(
            _expression(), query=QUERY, literals=[]
        )
        == ()
    )


# ── 값 없는 '최근' × 배포 기본 기간 ───────────────────────────────────────────────
#
# 기본 기간이 설정된 배포에서는 `default_period_policy` 가 구조화기에게 "그 창을 넣어라"를
# 지시하고 모델은 실제로 넣는다. 그런데 모델이 다는 근거 구간은 임계값 쪽('3회 이상')이라
# 표지 '최근'을 덮지 않는다 — 그 이유만으로 정확히 옳은 표현이 거부되면 정책은 영원히
# `candidate_has_no_expression` 으로 끝난다(2026-08-06 실측).

_BARE_RECENT_QUERY = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"


def _campaign_count_expression(window_days: int | None) -> EventExpression:
    """모델이 실제로 낸 모양: 근거 구간이 '3회 이상'이고 표지를 덮지 않는다."""
    relation: dict[str, Any] = {"type": "source", "name": "campaign_contact_success"}
    if window_days is not None:
        relation = {
            "type": "filter",
            "relation": relation,
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": "campaign_contact_success.occurred_at"},
                "window": {"type": "rolling", "value": window_days, "unit": "day"},
            },
        }
    span = "3회 이상"
    start = _BARE_RECENT_QUERY.index(span)
    return from_wire({
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "count",
            "relation": relation,
            "expression": {"type": "field", "name": "campaign_contact_success.execution_id"},
            "distinct": True,
        },
        "right": {"type": "literal", "value": 3},
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    })


def test_bare_recency_stays_a_deficiency_when_no_default_period_is_configured() -> None:
    """정책이 꺼진 배포의 판정은 종전 그대로다 — 창이 있어도 원문이 말하지 않은 값이다."""
    issues = audience_validators.TemporalSpanValidator().validate(
        _campaign_count_expression(3), query=_BARE_RECENT_QUERY, literals=[]
    )
    assert "missing_argument" in {report_from_issue(issue)[0] for issue in issues}


def test_the_configured_default_period_satisfies_a_bare_recency_marker() -> None:
    """설정된 기본 기간을 실제로 들고 있으면 결핍이 아니다(근거 구간 위치와 무관)."""
    assert (
        audience_validators.TemporalSpanValidator(default_period=(3, "day")).validate(
            _campaign_count_expression(3), query=_BARE_RECENT_QUERY, literals=[]
        )
        == ()
    )


def test_a_window_other_than_the_configured_default_is_still_a_deficiency() -> None:
    """모델이 지어낸 창은 기본 기간이 아니다 — 설정을 켰다고 아무 창이나 통과하지 않는다."""
    issues = audience_validators.TemporalSpanValidator(default_period=(3, "day")).validate(
        _campaign_count_expression(30), query=_BARE_RECENT_QUERY, literals=[]
    )
    assert "missing_argument" in {report_from_issue(issue)[0] for issue in issues}


def test_a_dropped_window_is_still_a_deficiency_under_the_default_policy() -> None:
    """창이 아예 사라진 표현은 정책이 켜져 있어도 결핍이다(전체 이력 폴백 방지)."""
    issues = audience_validators.TemporalSpanValidator(default_period=(3, "day")).validate(
        _campaign_count_expression(None), query=_BARE_RECENT_QUERY, literals=[]
    )
    assert "missing_argument" in {report_from_issue(issue)[0] for issue in issues}


def test_two_bare_markers_consume_two_windows() -> None:
    """표지 하나가 창 하나를 소비한다 — 창 하나로 표지 둘을 덮지 않는다."""
    query = "최근 구매하고 최근 로그인한 회원"
    wire = {
        "type": "and",
        "operands": [
            {
                "type": "exists",
                "relation": {
                    "type": "filter",
                    "relation": {"type": "source", "name": "purchase"},
                    "where": {
                        "type": "time_filter",
                        "field": {"type": "field", "name": "purchase.occurred_at"},
                        "window": {"type": "rolling", "value": 3, "unit": "day"},
                    },
                },
                "evidence": {"text": "최근 구매", "start": 0, "end": 5},
            },
            {
                "type": "exists",
                "relation": {"type": "source", "name": "app_login"},
                "evidence": {"text": "로그인한", "start": 9, "end": 14},
            },
        ],
    }
    issues = audience_validators.TemporalSpanValidator(default_period=(3, "day")).validate(
        from_wire(wire), query=query, literals=[]
    )
    assert "missing_argument" in {report_from_issue(issue)[0] for issue in issues}


def test_compiler_capability_validator_asks_the_real_compiler() -> None:
    """컴파일러가 낼 수 있다고 답하면 이 검증기는 조용하다(모델 산문이 아니라 자산이 판정한다)."""
    assert (
        audience_validators.CompilerCapabilityValidator().validate(
            _expression(), query=QUERY, literals=[]
        )
        == ()
    )


# ── 2. 순서가 계약이다 ────────────────────────────────────────────────────────────


def test_validator_order_is_declared_and_stable() -> None:
    kinds = [type(item).__name__ for item in audience_validators.audience_validators()]
    assert kinds == [
        "CatalogSymbolValidator",
        "CompilerCapabilityValidator",
        "TemporalSpanValidator",
        "CanonicalClaimValidator",
    ]


# ── 3. 요구 계층이 실제로 돌린다 ──────────────────────────────────────────────────


class _Spy:
    """주입된 검증기가 정말 호출되는지 보는 최소 구현."""

    def __init__(self, issue: RequirementIssue | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._issue = issue

    def validate(
        self, expression: EventExpression, *, query: str, literals: Sequence[object]
    ) -> tuple[RequirementIssue, ...]:
        self.calls.append((query, tuple(literals)))
        return (self._issue,) if self._issue is not None else ()


def test_resolver_runs_injected_validators_with_query_and_literals() -> None:
    spy = _Spy()
    context = resolution_context()
    context = context.model_copy(update={"literals": ({"kind": "duration"},)})
    result = run(
        DefaultRequirementResolver(id_factory=id_factory(), validators=(spy,)).resolve(
            _requirement(_expression()), context
        )
    )
    assert spy.calls == [(QUERY, ({"kind": "duration"},))], "검증기가 호출되지 않았다."
    assert result.status == "ready"


def _login_channel_resolution(
    *, negated: bool, operator: str = "=", semantic_plan: dict | None = None
):
    query = "앱으로 로그인하지 않은 회원을 찾아줘"
    span = "앱으로 로그인하지 않은"
    start = query.index(span)
    comparison = event_ir.Comparison(
        operator,
        event_ir.FieldRef("subject.last_login_channel"),
        event_ir.Literal("app_user"),
        evidence=event_ir.Evidence(
            text=span,
            start=start,
            end=start + len(span),
        ),
    )
    expression = event_ir.Not(comparison) if negated else comparison
    payload = {
        "audience_requirement": {
            "expression": expression.to_dict(),
            "issues": [],
        },
        "literal_bindings": [],
    }
    if semantic_plan is not None:
        payload["semantic_plan"] = semantic_plan
    return run_audience_resolver(
        payload,
        query,
        current_date="2026-08-04",
    )


def test_run_audience_resolver_rejects_dropped_app_login_negation() -> None:
    """주입된 claim validator가 원문의 제외 극성을 잃은 양수 표현을 막는다."""
    resolution = _login_channel_resolution(negated=False)

    assert resolution is not None
    assert [
        (issue["code"], issue["argument"])
        for issue in resolution.issues
    ] == [
        ("validation_mismatch", "catalog_value.subject.last_login_channel")
    ]


def test_run_audience_resolver_accepts_preserved_app_login_negation() -> None:
    """같은 원문을 올바르게 Not으로 보존하면 실제 resolver 경로가 통과한다."""
    resolution = _login_channel_resolution(negated=True)

    assert resolution is not None
    assert resolution.issues == []


def test_run_audience_resolver_accepts_direct_app_login_not_equal() -> None:
    resolution = _login_channel_resolution(negated=False, operator="!=")

    assert resolution is not None
    assert resolution.issues == []


def test_real_login_channel_ambiguity_remains_unresolved() -> None:
    query = "로그인 채널을 알 수 없는 회원"
    evidence = "로그인 채널"
    resolution = run_audience_resolver(
        {
            "audience_requirement": {
                "expression": None,
                "issues": [{
                    "code": "ambiguous_requirement",
                    "argument": "last_login_channel",
                    "message": "어느 로그인 채널인지 확정할 수 없습니다.",
                    "evidence": {
                        "text": evidence,
                        "start": 0,
                        "end": len(evidence),
                    },
                }],
            },
            "literal_bindings": [],
        },
        query,
        current_date="2026-08-04",
    )

    assert resolution is not None
    assert resolution.expression is None
    assert [issue["code"] for issue in resolution.issues] == ["ambiguous_requirement"]


def test_unrelated_relation_span_cannot_hide_wrong_app_login_polarity() -> None:
    query = "앱으로 로그인하지 않은 회원을 찾아줘"
    resolution = _login_channel_resolution(
        negated=False,
        semantic_plan={
            "nodes": [{
                "id": "r1",
                "type": "relation_predicate",
                "subject": "member",
                "attribute": "member_grade",
                "relation": "transition",
                "from_value": "골드",
                "to_value": "VIP",
                "source_span": query,
                "source_start": 0,
                "source_end": len(query),
            }]
        },
    )

    assert resolution is not None
    assert any(
        issue["argument"] == "catalog_value.subject.last_login_channel"
        for issue in resolution.issues
    )


def test_validator_issue_blocks_the_spec() -> None:
    """검증기가 결핍을 내면 사양은 만들어지지 않는다 — 부분 사양이 SQL 로 가는 길을 막는다."""
    issue = issue_from_report(
        code="unsupported_semantics",
        argument="purchase.nope",
        message="등록되지 않은 심볼입니다",
        evidence=SourceEvidence(text=SPAN, start=0, end=len(SPAN)),
    )
    result = run(
        DefaultRequirementResolver(
            id_factory=id_factory(), validators=(_Spy(issue),)
        ).resolve(_requirement(_expression()), resolution_context())
    )
    assert result.status == "unresolved"
    assert not hasattr(result, "spec")
    assert [report_from_issue(item) for item in result.issues] == [
        ("unsupported_semantics", "purchase.nope")
    ]


# ── 표기 왕복 ─────────────────────────────────────────────────────────────────────


def test_issue_report_roundtrip_is_lossless() -> None:
    """code/argument 는 구조화된 결핍을 지나 **그대로** 돌아온다.

    투영이 legacy 표기를 다시 만들려면 이 왕복이 성립해야 한다. 깨지면 사용자에게 나가는
    issue 의 이름이 조용히 바뀐다.
    """
    for code in sorted(ISSUE_CODE_KINDS):
        for argument in ("period", "purchase.amount", "audience_expression"):
            issue = issue_from_report(code=code, argument=argument, message="m")
            assert report_from_issue(issue) == (code, argument)


def test_event_ir_is_reachable_from_the_validated_expression() -> None:
    """검증기는 표현을 사건 IR 로 되돌려 본다 — 그 왕복이 무손실이어야 판정이 같다."""
    from query_pipeline.event_query.event_ir_bridge import to_event_ir

    wire = _wire("purchase")
    assert to_event_ir(from_wire(wire)).to_dict() == (
        event_ir.condition_from_dict(wire).to_dict()
    )
