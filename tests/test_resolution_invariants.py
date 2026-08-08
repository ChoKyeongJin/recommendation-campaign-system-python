"""Resolution 계층의 **구조적 불변식** — 말이 아니라 코드가 지키게 한다.

기능 테스트(``tests/test_resolution_policy.py``)가 "이 문장은 이렇게 귀결된다"를 고정한다면,
여기서는 "그 귀결이 다른 방식으로는 나올 수 없다"를 고정한다. 두 축이 다르다: 기능은 예시로
증명되고 불변식은 반례가 없음으로 증명된다.

특히 F(원문 재해석 금지)는 예시로는 증명되지 않는다. 적용기가 언젠가 정규화기를 부르기
시작하면 그날 이후의 모든 답변이 원문 전체를 다시 흔들지만, 어떤 기능 테스트도 그것을 보지
못한다 — 결과가 대체로 같아 보이기 때문이다. 그래서 import 그래프를 직접 잰다.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
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
    ResolutionRisk,
    ResolvedRequest,
    UnsupportedResolution,
    detect_semantic_issues,
    plan_clarifications,
    resolve_request,
    resolved_config,
)
from resolution.issues import (
    ISSUE_KIND_SPECS,
    UNSUPPORTED_KINDS,
    IssueCandidate,
    IssueFamily,
    SemanticIssue,
)
from resolution.policy import AUTO_RESOLVERS, ResolutionDecisions
from resolution.request import NON_EXPLICIT_PROVENANCES

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "resolution"


def _module_imports(path: Path) -> set[str]:
    """모듈 수준 import 만 센다(지연 import 는 함수 안에 있고 여기 잡히지 않는다)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ── A. 운영 기본값은 정책 계층 밖에서 만들어지지 않는다 ──────────────────────────


def test_auto_resolution_values_are_produced_only_by_the_policy_module() -> None:
    """``AutoResolution`` 을 만드는 자리가 정책 모듈 하나뿐인가.

    파서·컴파일러·플래너가 자기 기본값을 만들기 시작하면 응답의 provenance 는 거짓이 된다 —
    '사용자가 말하지 않은 값'이 영수증 없이 SQL 에 들어간다.
    """

    producers = []
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AutoResolution"
            ):
                producers.append(path.name)

    assert set(producers) == {"policy.py"}


def test_every_auto_resolver_is_registered_for_a_declared_kind() -> None:
    for kind in AUTO_RESOLVERS:
        assert kind in ISSUE_KIND_SPECS
        assert ISSUE_KIND_SPECS[kind].family is not IssueFamily.UNSUPPORTED


# ── B/C/D. 질문 생성기 입력의 계약 ───────────────────────────────────────────────


def _grain_request() -> CanonicalRequest:
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.event_relation(
                "purchase", event_ir.RollingWindow(value=30, unit="day")
            ),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=200000),
        evidence=event_ir.Evidence(text="20만원 이상 구매", start=3, end=13),
    )
    return CanonicalRequest(query="최근 20만원 이상 구매한 회원", expression=expression.to_dict())


def test_an_unjudged_issue_cannot_become_a_question() -> None:
    request = _grain_request()
    issues = detect_semantic_issues(request, resolved_config())

    with pytest.raises(ClarificationInvariantViolation):
        plan_clarifications(issues, ResolutionDecisions())


def test_an_auto_resolved_issue_never_appears_in_the_question_list(monkeypatch: Any) -> None:
    """§34-C — 정책이 채운 것을 다시 묻지 않는다."""

    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근 구매한 회원"
    request = CanonicalRequest(
        query=query,
        expression=event_ir.Exists(
            relation=event_ir.event_relation("purchase"),
            evidence=event_ir.Evidence(text="최근 구매", start=0, end=5),
        ).to_dict(),
        reported_issues=(
            {
                "code": "missing_argument",
                "argument": "period",
                "message": "기간이 없습니다.",
                "evidence": {"text": "최근", "start": 0, "end": 2},
            },
        ),
    )

    outcome = resolve_request(request)

    assert isinstance(outcome, ResolvedRequest)
    assert [item.code for item in outcome.assumptions] == ["DEFAULT_RECENT_PERIOD"]


def test_an_unsupported_issue_never_becomes_a_question() -> None:
    """§34-D — 답해도 열리지 않는 것을 묻지 않는다."""

    query = "정상에서 휴면으로 바뀐 회원"
    request = CanonicalRequest(
        query=query,
        reported_issues=(
            {
                "code": "unsupported_semantics",
                "argument": "state_history",
                "message": "속성 이력 소스가 없습니다.",
                "evidence": {"text": query, "start": 0, "end": len(query)},
            },
        ),
    )

    outcome = resolve_request(request)

    assert isinstance(outcome, UnsupportedResolution)
    assert not hasattr(outcome, "plan")


def test_unsupported_kinds_declare_no_question_text() -> None:
    """질문 문구가 있으면 언젠가 누군가 그것을 사용자에게 보낸다."""

    for kind in UNSUPPORTED_KINDS:
        assert ISSUE_KIND_SPECS[kind].question_text == ""


# ── E. 답변은 그 슬롯 밖 의미를 바꾸지 않는다 ────────────────────────────────────


def test_a_grain_answer_leaves_a_sibling_clause_byte_identical() -> None:
    query = "최근 30일 로그인하고 20만원 이상 구매한 회원"
    login = event_ir.Exists(
        relation=event_ir.event_relation(
            "login", event_ir.RollingWindow(value=30, unit="day")
        ),
        evidence=event_ir.Evidence(text="최근 30일 로그인", start=0, end=10),
    )
    purchase = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.event_relation(
                "purchase", event_ir.RollingWindow(value=30, unit="day")
            ),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=200000),
        evidence=event_ir.Evidence(text="20만원 이상 구매", start=13, end=23),
    )
    request = CanonicalRequest(
        query=query,
        expression=event_ir.And(operands=(login, purchase)).to_dict(),
    )
    asked = resolve_request(request)
    assert isinstance(asked, NeedsClarification)

    settled = resolve_request(
        request,
        answers=[ClarificationAnswer(issue_id=asked.issues[0].issue_id, option_id="row")],
    )

    assert isinstance(settled, ResolvedRequest)
    operands = settled.request.expression["operands"]  # type: ignore[index]
    assert operands[0] == login.to_dict()  # 형제 절은 바이트 동일


# ── F. 답변 처리는 원문을 다시 해석하지 않는다 ───────────────────────────────────

#: 원문을 다시 읽는 계층. 확정 계층이 여기 의존하면 답변 하나가 문장 전체를 흔든다.
NORMALIZER_MODULES = frozenset(
    {
        "query_structurer",
        "graph_rag",
        "normalization_orchestrator",
        "semantic_normalizers",
        "condition_normalizers",
        "lexicon_llm",
    }
)

#: 답변 처리 경로. 이 모듈들이 정규화기를 모듈 수준에서 부르면 안 된다.
ANSWER_PATH_MODULES = ("applier.py", "slots.py", "clarification.py", "loop.py", "issues.py")


@pytest.mark.parametrize("module_name", ANSWER_PATH_MODULES)
def test_the_answer_path_does_not_depend_on_the_normalizer(module_name: str) -> None:
    imports = _module_imports(PACKAGE_ROOT / module_name)
    offending = {
        name
        for name in imports
        if name.split(".")[0] in NORMALIZER_MODULES
    }

    assert not offending, (
        f"{module_name} 이 원문 재해석 계층을 import 한다: {sorted(offending)}"
    )


def test_applying_an_answer_does_not_touch_the_source_text() -> None:
    """답변 적용 전후로 원문 문자열이 동일한가 — 답이 문장이 되지 않는다."""

    request = _grain_request()
    asked = resolve_request(request)
    assert isinstance(asked, NeedsClarification)

    settled = resolve_request(
        request,
        answers=[ClarificationAnswer(issue_id=asked.issues[0].issue_id, option_id="row")],
    )

    assert settled.request.query == request.query


# ── G. 사용자 명시가 아닌 의미에는 영수증이 있다 ─────────────────────────────────


def test_a_changed_expression_always_carries_a_provenance_receipt(monkeypatch: Any) -> None:
    monkeypatch.setenv("AUDIENCE_DEFAULT_PERIOD", "30 day")
    query = "최근 구매한 회원"
    request = CanonicalRequest(
        query=query,
        expression=event_ir.Exists(
            relation=event_ir.event_relation("purchase"),
            evidence=event_ir.Evidence(text="최근 구매", start=0, end=5),
        ).to_dict(),
        reported_issues=(
            {
                "code": "missing_argument",
                "argument": "period",
                "message": "기간이 없습니다.",
                "evidence": {"text": "최근", "start": 0, "end": 2},
            },
        ),
    )

    outcome = resolve_request(request)

    assert isinstance(outcome, ResolvedRequest)
    assert outcome.request.expression != request.expression
    assert outcome.assumptions, "IR 이 바뀌었는데 영수증이 없다"
    assert all(
        assumption.provenance in NON_EXPLICIT_PROVENANCES
        for assumption in outcome.assumptions
    )
    assert outcome.resolution == "assumed"


# ── H. HIGH 위험 모호성은 자동으로 사라지지 않는다 ───────────────────────────────


HIGH_RISK_KINDS = tuple(
    kind
    for kind, spec in sorted(ISSUE_KIND_SPECS.items())
    if spec.risk is ResolutionRisk.HIGH and spec.family is not IssueFamily.UNSUPPORTED
)


@pytest.mark.parametrize(
    ("kind", "mode"), list(itertools.product(HIGH_RISK_KINDS, list(ResolutionMode)))
)
def test_high_risk_deficits_are_never_auto_resolved(kind: str, mode: ResolutionMode) -> None:
    spec = ISSUE_KIND_SPECS[kind]
    issue = SemanticIssue(
        kind=kind,
        slot=spec.slot,
        message="테스트 결핍",
        candidates=(
            ()
            if spec.allow_free_text
            else tuple(
                IssueCandidate(value=name, label=name) for name in ("row", "subject")
            )
        ),
        entity_type="product",
    )
    policy = ResolutionPolicy(resolved_config().with_mode(mode))

    decisions = policy.resolve(CanonicalRequest(query="테스트"), [issue])

    assert decisions.decisions[0].action is not ResolutionAction.AUTO_RESOLVE


def test_every_declared_kind_is_judged_by_the_policy() -> None:
    """선언만 있고 판정되지 않는 종류가 있으면 그 결핍은 조용히 사라진다."""

    policy = ResolutionPolicy()
    request = CanonicalRequest(query="테스트")
    for kind, spec in ISSUE_KIND_SPECS.items():
        issue = SemanticIssue(
            kind=kind,
            slot=spec.slot,
            message="테스트 결핍",
            candidates=(
                ()
                if spec.allow_free_text or spec.family is not IssueFamily.AMBIGUOUS
                else tuple(
                    IssueCandidate(value=name, label=name)
                    for name in ("row", "subject")
                )
            ),
        )
        decision = policy.resolve(request, [issue]).decisions[0]
        assert decision.action in set(ResolutionAction)
        if spec.family is IssueFamily.UNSUPPORTED:
            assert decision.action is ResolutionAction.UNSUPPORTED
