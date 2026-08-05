"""원문 → 전이 요청 선택의 고정 계약.

여기서 재는 것은 **문장에서 무엇을 골랐는가**다. 데이터베이스에 연결하지 않고 실제 회원 행·
전이 이력·조회 결과 건수를 만들지 않는다. 입력은 문장과 선언(카탈로그 파일)뿐이다.

축 이름을 코드에 적지 않는다는 계약은 두 곳에서 잰다 — 여기서는 **행동으로**(같은 문형이
선언만 바뀌어 다른 축의 지표를 고른다), :mod:`tests.test_transition_metrics` 에서는
**구조로**(실행되는 이름에 축 이름이 없다).
"""

from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import resolved_semantic_catalog  # noqa: E402
import transition_claims  # noqa: E402
import transition_metrics  # noqa: E402

GRADE_TRANSITION = "member_grade_transition"
WORTH_TRANSITION = "member_worth_grade_transition"


@pytest.fixture(scope="module")
def snapshot():
    return audience_runtime.catalog_snapshot()


@pytest.fixture(scope="module")
def catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def ambiguous_catalog():
    """같은 값 도메인을 주장하는 전이 지표가 둘인 카탈로그(모호성 고정용)."""

    path = audience_runtime.DEFAULT_AUDIENCE_CATALOG_PATH
    raw = audience_runtime.materialize_member_metric_rankings(
        audience_runtime.load_audience_catalog_config(path), catalog_path=path
    )
    materialized = audience_runtime.materialize_value_domains(raw)
    if materialized:
        raw["value_domains"] = materialized
    raw = copy.deepcopy(raw)
    duplicate = copy.deepcopy(raw["metrics"][GRADE_TRANSITION])
    duplicate.pop("aliases", None)
    duplicate.pop("_comment", None)
    raw["metrics"][f"{GRADE_TRANSITION}_copy"] = duplicate
    subject_raw = raw["subject"]
    return resolved_semantic_catalog.resolve_semantic_catalog(
        runtime_config=raw,
        subject=event_compiler.SubjectSpec(
            table=subject_raw["table"],
            alias=subject_raw.get("alias", "B"),
            key=subject_raw["key"],
            name=subject_raw.get("name", "subject"),
        ),
    )


def _request(query, snapshot, catalog, **kwargs):
    return transition_claims.detect_transition_request(
        query, snapshot=snapshot, catalog=catalog, **kwargs
    )


def _synthesis(query, snapshot, catalog, **kwargs):
    return transition_claims.synthesize_transition_predicate(
        query, snapshot=snapshot, catalog=catalog, **kwargs
    )


def _rejection(query, snapshot, catalog, **kwargs):
    result = _synthesis(query, snapshot, catalog, **kwargs)
    assert isinstance(result, transition_claims.TransitionRejection), result
    return result


# ── 1~2. 값 쌍과 어순 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query",
    [
        "골드에서 VIP로 바뀐 회원",
        "골드에서 VIP로 변경된 회원",
        "골드에서 VIP로 전환된 회원",
        "골드에서 VIP로 승급한 회원",
        "골드에서 VIP가 된 회원",
        "골드부터 VIP로 바뀐 회원",
    ],
)
def test_every_declared_marker_form_yields_the_same_pair(query, snapshot, catalog) -> None:
    request = _request(query, snapshot, catalog)
    assert isinstance(request, transition_claims.TransitionRequest), request
    assert (request.from_value, request.to_value) == ("gold_grade", "vip")
    assert request.metric_id == GRADE_TRANSITION


def test_source_order_in_the_sentence_decides_from_and_to(snapshot, catalog) -> None:
    """어순이 바뀌면 전이의 방향도 바뀐다 — 값의 정체만 보고 접지 않는다."""

    forward = _request("골드에서 VIP로 바뀐 회원", snapshot, catalog)
    backward = _request("VIP에서 골드로 바뀐 회원", snapshot, catalog)
    assert (forward.from_value, forward.to_value) == ("gold_grade", "vip")
    assert (backward.from_value, backward.to_value) == ("vip", "gold_grade")


def test_the_evidence_span_covers_the_whole_transition_phrase(snapshot, catalog) -> None:
    query = "골드에서 VIP로 승급한 회원"
    request = _request(query, snapshot, catalog)
    assert query[request.evidence.start:request.evidence.end] == request.evidence.text
    assert "골드" in request.evidence.text and "VIP" in request.evidence.text


# ── 3. 값이 정확히 둘일 때만 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query",
    [
        "최근에 등급이 승급한 회원",
        "VIP로 승급한 회원",
        "정상에서 휴면으로 바뀐 회원",
    ],
)
def test_a_transition_without_both_values_is_not_synthesized(
    query, snapshot, catalog
) -> None:
    result = _synthesis(query, snapshot, catalog)
    if result is None:
        return
    assert result.code == transition_claims.VALUES_NOT_PAIRED


def test_values_outside_the_marker_clause_do_not_join_the_pair(snapshot, catalog) -> None:
    """절이 다르면 다른 조건이다 — 옆 절의 값을 끌어와 쌍을 채우지 않는다."""

    request = _request("실버에서 골드로 승급하고 VIP인 회원", snapshot, catalog)
    assert isinstance(request, transition_claims.TransitionRequest), request
    assert (request.from_value, request.to_value) == ("silver_grade", "gold_grade")


# ── 4. 도메인을 섞지 않는다 ─────────────────────────────────────────────────────

def test_two_values_from_different_domains_fail_closed(snapshot, catalog) -> None:
    rejection = _rejection("휴면에서 VIP로 바뀐 회원", snapshot, catalog)
    assert rejection.code == transition_claims.DOMAIN_MIXED


# ── 5~7. 축 선택 ────────────────────────────────────────────────────────────────

def test_an_axis_cue_moves_the_same_words_to_the_other_declared_axis(
    snapshot, catalog
) -> None:
    """같은 낱말('골드에서 VIP로')이 축 표지 하나로 다른 지표를 고른다 — 코드가 아니라 선언이다."""

    plain = _request("골드에서 VIP로 바뀐 회원", snapshot, catalog)
    qualified = _request("가치등급이 골드에서 VIP로 바뀐 회원", snapshot, catalog)
    assert (plain.value_domain, plain.metric_id) == ("grade", GRADE_TRANSITION)
    assert (qualified.value_domain, qualified.metric_id) == (
        "worth_grade",
        WORTH_TRANSITION,
    )
    assert (qualified.from_value, qualified.to_value) == (
        "gold_worth_grade",
        "vip_worth_grade",
    )


def test_the_longer_axis_qualified_alias_wins_over_the_bare_value(
    snapshot, catalog
) -> None:
    """'VIP' 는 두 도메인에 있다. 축 한정이 있으면 짧은 일반 값이 이기지 못한다."""

    request = _request("가치등급이 실버에서 VIP로 승급한 회원", snapshot, catalog)
    assert request.value_domain == "worth_grade"
    assert (request.from_value, request.to_value) == (
        "silver_worth_grade",
        "vip_worth_grade",
    )


def test_both_axes_reach_sql_through_the_same_claim_path(snapshot, catalog) -> None:
    context = catalog.compile_context(literals=True)
    grade = _synthesis("골드에서 VIP로 바뀐 회원", snapshot, catalog)
    worth = _synthesis("가치등급이 골드에서 VIP로 바뀐 회원", snapshot, catalog)
    grade_sql = event_compiler.compile_expression(grade.expression, context=context).sql
    worth_sql = event_compiler.compile_expression(
        worth.expression, context=catalog.compile_context(literals=True)
    ).sql
    assert "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in grade_sql
    assert "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in grade_sql
    assert "MS.WORTH_GRADE = 'VIP'" in worth_sql
    assert "MS.PREV_WORTH_GRADE = 'GOLD'" in worth_sql
    assert grade.receipt["owner"] == worth.receipt["owner"] == transition_claims.OWNER


# ── 8. 방향어 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("query", "direction"),
    [
        ("실버에서 골드로 승급한 회원", transition_metrics.ASCENDING),
        ("골드에서 실버로 강등된 회원", transition_metrics.DESCENDING),
        ("실버에서 골드로 바뀐 회원", None),
    ],
)
def test_direction_cues_come_from_the_shared_vocabulary(
    query, direction, snapshot, catalog
) -> None:
    request = _request(query, snapshot, catalog)
    assert isinstance(request, transition_claims.TransitionRequest), request
    assert request.direction == direction


@pytest.mark.parametrize(
    "query", ["골드에서 실버로 승급한 회원", "실버에서 골드로 강등된 회원"]
)
def test_a_direction_cue_that_contradicts_the_values_fails_closed(
    query, snapshot, catalog
) -> None:
    rejection = _rejection(query, snapshot, catalog)
    assert rejection.code == transition_metrics.DIRECTION_CONTRADICTED


# ── 9. 기간이 붙은 전이 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query",
    [
        "최근 3개월 동안 골드에서 VIP로 변경된 회원",
        "지난달 실버에서 골드로 승급한 회원",
        "1년 이내에 실버에서 골드가 된 회원",
        "2026년 3월에 실버에서 골드로 승급한 회원",
    ],
)
def test_a_period_qualified_transition_is_not_synthesized(
    query, snapshot, catalog
) -> None:
    rejection = _rejection(query, snapshot, catalog)
    assert rejection.code == transition_claims.PERIOD_UNSUPPORTED


@pytest.mark.parametrize("today", [date(2019, 1, 31), date(2026, 8, 5)])
def test_period_detection_does_not_depend_on_the_reference_date(
    today, snapshot, catalog
) -> None:
    """상대 달력어의 **구간**은 기준일과 무관하다 — 탐지는 그 구간만 쓴다."""

    rejection = _rejection(
        "지난달 실버에서 골드로 승급한 회원", snapshot, catalog, today=today
    )
    assert rejection.code == transition_claims.PERIOD_UNSUPPORTED


def test_a_period_owned_by_another_condition_does_not_close_the_transition(
    snapshot, catalog
) -> None:
    """다른 조건이 이미 그 기간을 소유했다면 전이의 한정어가 아니다."""

    query = "최근 3개월 동안 골드에서 VIP로 변경된 회원"
    start = query.index("3개월")
    result = _synthesis(
        query, snapshot, catalog, consumed_spans=[(start, start + len("3개월"))]
    )
    assert isinstance(result, transition_claims.TransitionSynthesis), result
    assert result.receipt["metric_id"] == GRADE_TRANSITION


def test_a_period_in_another_clause_does_not_close_the_transition(
    snapshot, catalog
) -> None:
    """절이 다르면 그 기간은 다른 조건의 것이다(어휘가 절 경계를 정한다)."""

    result = _synthesis(
        "최근 90일 동안 3회 이상 구매하고 골드에서 VIP로 승급한 회원", snapshot, catalog
    )
    assert isinstance(result, transition_claims.TransitionSynthesis), result
    assert result.receipt["from_value"] == "gold_grade"


# ── 10~11. 지표 후보 ────────────────────────────────────────────────────────────

def test_a_domain_with_no_declared_transition_metric_fails_closed(
    snapshot, catalog
) -> None:
    rejection = _rejection("휴면에서 탈퇴로 바뀐 회원", snapshot, catalog)
    assert rejection.code == transition_metrics.METRIC_NOT_DECLARED


def test_two_metrics_claiming_one_domain_reach_the_caller_as_a_reason(
    snapshot, ambiguous_catalog
) -> None:
    rejection = _rejection("골드에서 VIP로 바뀐 회원", snapshot, ambiguous_catalog)
    assert rejection.code == transition_metrics.METRIC_AMBIGUOUS


# ── 전이를 말하지 않은 문장 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query",
    [
        "구매주기가 30일 이하인 회원",
        "현재 등급이 VIP인 회원",
        "골드 이상 등급인 회원",
        "",
    ],
)
def test_a_sentence_without_a_transition_is_not_claimed(query, snapshot, catalog) -> None:
    """``None`` 은 '전이를 말하지 않았다'이고 거절은 '말했지만 못 만든다'이다."""

    assert _synthesis(query, snapshot, catalog) is None


def test_two_transition_markers_in_one_sentence_fail_closed(snapshot, catalog) -> None:
    query = "골드에서 VIP로 바뀐 회원, 실버에서 골드로 바뀐 회원"
    rejection = _rejection(query, snapshot, catalog)
    assert rejection.code == transition_claims.MARKER_AMBIGUOUS


# ── 영수증 ──────────────────────────────────────────────────────────────────────

def test_the_receipt_explains_the_declaration_and_the_source_span(
    snapshot, catalog
) -> None:
    result = _synthesis("가치등급이 골드에서 VIP로 승급한 회원", snapshot, catalog)
    receipt = result.receipt
    assert receipt["metric_id"] == WORTH_TRANSITION
    assert receipt["strategy"] == "current_previous_columns"
    assert receipt["direction"] == transition_metrics.ASCENDING
    assert receipt["conditions"] == {
        "current": "member_month_snapshot.worth_grade = vip_worth_grade",
        "previous": "member_month_snapshot.prev_worth_grade = gold_worth_grade",
    }
    evidence = receipt["evidence"]
    assert evidence["text"] == "골드에서 VIP로 승급"
