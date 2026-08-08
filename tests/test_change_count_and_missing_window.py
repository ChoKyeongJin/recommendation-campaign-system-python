"""변경 횟수와 '기간 미명시' 정책의 고정 계약 — **적재량은 컴파일 조건이 아니다**.

이 파일이 지키는 한 문장: SQL 컴파일러는 "현재 데이터로 답이 나오는가"가 아니라 "이 요청을
지금 선언된 스키마로 SQL 로 표현할 수 있는가"만 판단한다. 그래서 여기 있는 단정은 전부
**선언 → 표현 → SQL 모양**이고, 행 수·적재 월 수·실제 변경 건수는 어디에도 없다.

불변식(요청서 §12)과 이 파일의 대응:

    A 실DB 데이터 개수가 compile 성공/실패를 바꾸지 않는다     → coverage 계열 3종
    B 같은 선언이면 DB 가 비어도 SQL 이 나온다                 → test_empty_database…
    C 기간 미명시 + all_available_data 정책 → TimeFilter 없음  → test_..._without_period…
    D 기간을 명시하면 TimeFilter 만 늘고 의미 연산은 같다      → test_..._keeps_time_filter
    E change_count 는 호환 binding 이면 어느 도메인에서나 동작 → test_..._domain_independent
    F prev 값이 없어도 정렬 관측 + LAG 로 컴파일된다           → test_..._uses_lag…
    G row count·적재 구간·실제 전이 수로 unsupported 가 되지 않는다 → coverage 계열 3종
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import temporal_claims  # noqa: E402
import temporal_ir  # noqa: E402
import temporal_semantics  # noqa: E402
from temporal_ir import catalog as tcat  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=SEOUL)
TODAY = date(2026, 8, 5)
SNAPSHOT = "member.grade.monthly_snapshot"
EVIDENCE = sir.Evidence(text="등급이 2회 이상 변경", start=0, end=12)


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def snapshot_values():
    return audience_runtime.catalog_snapshot()


@pytest.fixture(scope="module")
def runtime(semantic_catalog) -> temporal_ir.TemporalRuntime:
    return temporal_ir.create_temporal_runtime(semantic_catalog)


@pytest.fixture()
def context() -> sir.TemporalRequestContext:
    return sir.TemporalRequestContext(now=NOW)


def _sql(semantic_catalog, expression: event_ir.Condition) -> str:
    return event_compiler.compile_condition(
        expression, semantic_catalog.compile_context(today=TODAY, literals=True)
    ).sql


def _synthesize(query: str, snapshot_values, semantic_catalog, runtime):
    return temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot_values,
        catalog=semantic_catalog,
        runtime=runtime,
        context=sir.TemporalRequestContext(now=NOW),
        today=TODAY,
    )


def _change_count(
    *,
    metric: str = "member.grade",
    binding: str | None = SNAPSHOT,
    window: sir.TemporalWindow | None = None,
    threshold: int = 2,
) -> sir.TemporalCondition:
    return sir.TemporalCondition(
        metric=metric,
        binding=binding,
        selector=sir.WindowSelector(
            window=window
            or sir.AllAvailableDataWindow(source=sir.WindowSource.POLICY_DEFAULT)
        ),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.ChangeCountPredicate(
            transition=sir.AnyValueChange(),
            comparison=sir.NumericComparison(operator=">=", value=Decimal(threshold)),
        ),
        evidence=EVIDENCE,
    )


def _catalog_payload() -> dict:
    return json.loads(tcat.DEFAULT_TEMPORAL_CATALOG_PATH.read_text(encoding="utf-8"))


# ── 1. 기간 미명시 → 전체 가용 데이터 범위 ────────────────────────────────────────


def test_change_count_without_period_uses_all_available_data(
    snapshot_values, semantic_catalog, runtime
) -> None:
    outcome = _synthesize("등급이 2회 이상 변경된 회원", snapshot_values, semantic_catalog, runtime)

    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis), outcome
    request = outcome.requests[0]
    assert request.operator == temporal_semantics.CHANGE_COUNT
    window = request.condition.selector.window
    assert isinstance(window, sir.AllAvailableDataWindow)
    # 출처가 남아 있어야 한다 — 사용자가 말한 구간과 정책이 채운 구간은 다른 사건이다.
    assert window.source is sir.WindowSource.POLICY_DEFAULT
    assert request.window_source is sir.WindowSource.POLICY_DEFAULT

    # 시간 필터가 없다(불변식 C). 전체 범위는 '경계를 지어내는 것'이 아니라 '거는 것이 없는 것'이다.
    assert event_ir.time_windows(outcome.expression) == []
    sql = _sql(semantic_catalog, outcome.expression)
    assert "MS.ZTS_GRADE != MS.PREV_ZTS_GRADE" in sql
    assert "HAVING COUNT(*) >= 2" in sql
    assert "YYYYMM" not in sql


def test_a_bare_period_marker_is_not_a_clarification_anymore(
    snapshot_values, semantic_catalog, runtime
) -> None:
    """기간 미명시는 이 연산에서 **결핍이 아니다** — 되묻기 코드가 나오지 않는다."""

    outcome = _synthesize("등급이 2회 이상 변경된 회원", snapshot_values, semantic_catalog, runtime)
    assert not isinstance(outcome, temporal_claims.TemporalClaimRejection), outcome


# ── 2. 기간 명시 → TimeFilter 만 늘어난다 ─────────────────────────────────────────


def test_change_count_with_explicit_period_keeps_time_filter(
    snapshot_values, semantic_catalog, runtime
) -> None:
    bare = _synthesize("등급이 2회 이상 변경된 회원", snapshot_values, semantic_catalog, runtime)
    windowed = _synthesize(
        "최근 6개월 동안 등급이 2회 이상 변경된 회원",
        snapshot_values, semantic_catalog, runtime,
    )
    assert isinstance(bare, temporal_claims.TemporalClaimSynthesis), bare
    assert isinstance(windowed, temporal_claims.TemporalClaimSynthesis), windowed

    assert windowed.requests[0].window_source is sir.WindowSource.USER
    sql = _sql(semantic_catalog, windowed.expression)
    assert "MS.YYYYMM >= '202603'" in sql and "MS.YYYYMM < '202609'" in sql
    assert "MS.ZTS_GRADE != MS.PREV_ZTS_GRADE" in sql
    assert "HAVING COUNT(*) >= 2" in sql

    # 불변식 D: 늘어난 것은 시간 조건뿐이고 의미 연산(집계·비교·술어)은 그대로다.
    added = event_ir.node_type_names(windowed.expression) - event_ir.node_type_names(
        bare.expression
    )
    assert added <= {"time_filter", "and", "interval"}
    for receipt_key in ("operator", "predicate_type", "measurement", "comparison_operator"):
        assert windowed.receipts[0][receipt_key] == bare.receipts[0][receipt_key]


# ── 3. 한글 수사 정규화(공통 문법) ────────────────────────────────────────────────


def test_korean_number_words_are_normalized(
    snapshot_values, semantic_catalog, runtime
) -> None:
    """'두 번 이상 변경'과 '2회 이상 변경'은 같은 요청이다(threshold=2)."""

    words = _synthesize(
        "최근 3개월 동안 등급이 두 번 이상 변경된 회원",
        snapshot_values, semantic_catalog, runtime,
    )
    digits = _synthesize(
        "최근 3개월 동안 등급이 2회 이상 변경된 회원",
        snapshot_values, semantic_catalog, runtime,
    )
    assert isinstance(words, temporal_claims.TemporalClaimSynthesis), words
    assert isinstance(digits, temporal_claims.TemporalClaimSynthesis), digits

    def semantics(outcome) -> dict:
        payload = outcome.requests[0].condition.to_dict()
        payload.pop("evidence", None)  # 근거 구간은 뜻이 아니다(출처다)
        return payload

    assert semantics(words) == semantics(digits)
    assert (
        semantics(words)["predicate"]["comparison"]["value"] == "2"
    ), semantics(words)["predicate"]


# ── 4. 적재량은 컴파일 조건이 아니다(불변식 A·B·G) ────────────────────────────────


def _lower_with_coverage(semantic_catalog, coverage: dict | None, threshold: int = 2):
    payload = _catalog_payload()
    binding = payload["bindings"][SNAPSHOT]
    if coverage is None:
        binding.pop("coverage", None)
    else:
        binding["coverage"] = coverage
    runtime = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)
    return runtime.lower(
        _change_count(threshold=threshold),
        sir.TemporalRequestContext(now=NOW),
    )


def test_empty_database_does_not_affect_compilation(semantic_catalog) -> None:
    """적재 선언이 아예 없어도(= 데이터를 하나도 주장하지 않아도) 같은 SQL 이 나온다."""

    declared = _lower_with_coverage(semantic_catalog, None)
    assert declared.status == "compiled", declared
    assert "MS.ZTS_GRADE != MS.PREV_ZTS_GRADE" in _sql(semantic_catalog, declared.expression)


def test_single_snapshot_does_not_block_sql_generation(semantic_catalog) -> None:
    """적재가 한 달뿐이라 답이 0건이 될 임계값도 컴파일된다(0건은 답이지 실패가 아니다)."""

    outcome = _lower_with_coverage(
        semantic_catalog,
        {
            "available_from": "20170101",
            "available_through": "20170131",
            "expected_cadence": "month",
            "completeness": "complete",
        },
        threshold=5,
    )
    assert outcome.status == "compiled", outcome
    assert "HAVING COUNT(*) >= 5" in _sql(semantic_catalog, outcome.expression)


def test_runtime_coverage_is_not_a_compile_gate(semantic_catalog) -> None:
    """적재 구간을 1개월 ↔ 100개월로 바꿔도 귀결과 SQL 모양이 같다(불변식 A·G)."""

    one_month = _lower_with_coverage(
        semantic_catalog,
        {
            "available_from": "20170101",
            "available_through": "20170131",
            "expected_cadence": "month",
            "completeness": "complete",
        },
    )
    hundred_months = _lower_with_coverage(
        semantic_catalog,
        {
            "available_from": "20170101",
            "available_through": "20260131",
            "expected_cadence": "month",
            "completeness": "complete",
        },
    )
    assert one_month.status == hundred_months.status == "compiled"
    assert _sql(semantic_catalog, one_month.expression) == _sql(
        semantic_catalog, hundred_months.expression
    )
    assert one_month.receipt.lowered_ir_hash == hundred_months.receipt.lowered_ir_hash


def test_the_compile_path_never_reads_the_database() -> None:
    """컴파일 계층 소스에 실행/조회 경로가 없다 — 적재량을 **볼 방법 자체가** 없어야 한다."""

    forbidden = ("psycopg", "pyodbc", "execute_query", "sql_executor", "run_query")
    for name in ("temporal_claims.py", "temporal_ir/lowering.py", "temporal_ir/operators.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{name} 이 {token!r} 를 참조한다"


# ── 5. 전략 A/B: 직전 값을 어떻게 얻는가(불변식 E·F) ──────────────────────────────


def test_change_count_uses_previous_value_when_available(semantic_catalog, runtime, context) -> None:
    """한 행에 직전 값이 있으면 같은 행의 두 컬럼 비교로 낮춘다(윈도 함수 없음)."""

    outcome = runtime.lower(_change_count(), context)
    assert outcome.status == "compiled", outcome
    sql = _sql(semantic_catalog, outcome.expression)
    assert "MS.ZTS_GRADE != MS.PREV_ZTS_GRADE" in sql
    assert "LAG(" not in sql
    assert "window" not in event_ir.node_type_names(outcome.expression)


def test_change_count_uses_lag_when_previous_value_is_missing(semantic_catalog, context) -> None:
    """직전 값 컬럼이 없어도 주체 키 + 정렬 필드가 있으면 LAG 로 만든다(불변식 F)."""

    outcome = temporal_ir.create_temporal_runtime(semantic_catalog).lower(
        _change_count(
            metric="member.newproduct_favor",
            binding="member.newproduct_favor.monthly_snapshot",
        ),
        context,
    )
    assert outcome.status == "compiled", outcome
    sql = _sql(semantic_catalog, outcome.expression)
    assert "LAG(MS.NEWPRODUCT_FAVOR_YN, 1) OVER (PARTITION BY MS.MEMBER_NO ORDER BY MS.YYYYMM ASC)" in sql
    assert "COUNT(*)" in sql and ">= 2" in sql
    assert {"window", "materialize", "output"} <= event_ir.node_type_names(outcome.expression)


def test_change_count_is_domain_independent(semantic_catalog, context) -> None:
    """전용 축이 아니다 — 호환 binding 을 선언한 **가짜 도메인**에서도 같은 연산자가 컴파일된다."""

    payload = _catalog_payload()
    binding = copy.deepcopy(payload["bindings"][SNAPSHOT])
    binding.update({
        "metric_id": "test.axis",
        "value_field": "member_month_snapshot.worth_grade",
        "prev_value_field": "member_month_snapshot.prev_worth_grade",
        "label": "테스트 전용 축",
    })
    payload["metrics"]["test.axis"] = {
        "label": "테스트 축",
        "value_type": "string",
        "value_domain": "worth_grade",
        "supported_comparisons": ["=", "!="],
        "temporal_bindings": ["test.axis.monthly_snapshot"],
    }
    payload["bindings"]["test.axis.monthly_snapshot"] = binding

    runtime = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)
    outcome = runtime.lower(
        _change_count(metric="test.axis", binding="test.axis.monthly_snapshot"), context
    )
    assert outcome.status == "compiled", outcome
    assert outcome.receipt.operator == "temporal.change_count"
    assert "MS.WORTH_GRADE != MS.PREV_WORTH_GRADE" in _sql(semantic_catalog, outcome.expression)


def test_change_count_is_unsupported_only_when_the_schema_cannot_express_it(
    semantic_catalog, context
) -> None:
    """유지해야 할 fail-close(§14): 직전 값도, 주체 키·정렬 필드도 없으면 그때가 미지원이다."""

    payload = _catalog_payload()
    binding = copy.deepcopy(payload["bindings"][SNAPSHOT])
    binding.pop("prev_value_field")
    binding.pop("entity_key_field")
    payload["bindings"][SNAPSHOT] = binding
    runtime = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)

    outcome = runtime.lower(_change_count(), context)
    assert outcome.status == "unsupported", outcome
    assert outcome.code == "temporal_change_count_shape_unavailable"
    assert "entity_key_field" in outcome.message


# ── 6. 정책은 선언이다(연산자 이름 분기 금지) ─────────────────────────────────────


def test_every_operator_declares_its_missing_window_policy() -> None:
    """모든 연산자 계획이 닫힌 정책 어휘 중 하나를 **선언**한다."""

    plans = temporal_claims._OPERATOR_PLANS
    assert set(plans) == set(temporal_semantics.OPERATORS)
    for operator, plan in plans.items():
        assert plan.missing_window in temporal_claims.MISSING_WINDOW_POLICIES, operator


def test_the_missing_window_decision_is_not_a_name_branch() -> None:
    """구간 기본값 판단에 연산자 **이름**이 등장하지 않는다(선언 표 밖에서 이름을 보지 않는다).

    이름으로 분기하면 새 연산자가 어느 목록에 들어가야 하는지 알 수 없게 되고, 같은 판단이
    선언과 코드 두 곳에 생긴다.
    """

    source = (REPO_ROOT / "temporal_claims.py").read_text(encoding="utf-8")
    body = source[source.index("def _plan_request(") :]
    for name in ("CHANGE_COUNT", "EVERY_SUBINTERVAL", "CONSECUTIVE_SUBINTERVALS"):
        assert f"temporal_semantics.{name}" not in body, name


def test_bucket_operators_still_ask_for_a_period(
    snapshot_values, semantic_catalog, runtime
) -> None:
    """칸 수가 판정의 재료인 연산은 전체 범위로 읽을 수 없다 — 되묻기가 유지된다."""

    outcome = _synthesize("매월 골드 등급이었던 회원", snapshot_values, semantic_catalog, runtime)
    assert isinstance(outcome, temporal_claims.TemporalClaimRejection), outcome
    assert outcome.code == temporal_claims.INTERVAL_MISSING
    assert outcome.disposition == temporal_claims.CLARIFICATION


# ── 7. 반려 사유는 판정 계층이 선언한 문장 그대로 나간다 ─────────────────────────


def test_the_declared_rejection_reason_reaches_the_response() -> None:
    """범용 문구가 아니라 **판정이 만든 사유**가 사용자 문장과 진단에 함께 도달한다.

    실측 2026-08-08: 판정 계층은 '기간이 없어 성립하지 않는다'를 이미 알고 있었는데 응답에는
    "요청한 조건을 현재 실행 자산으로 표현할 수 없습니다"만 나갔다. 사유를 아는 계층과 문장을
    만드는 계층이 이어져 있지 않으면, 사용자는 고칠 수 있는 문제를 고칠 수 없는 문제로 읽는다.
    """

    import copy  # noqa: PLC0415

    import networkx as nx  # noqa: PLC0415

    import graph_rag  # noqa: PLC0415
    import semantic_requirements  # noqa: PLC0415
    from query_structurer.audience_execution import TEMPORAL_REJECTION_KEY  # noqa: PLC0415
    from query_structurer.campaign_plan_v4 import (  # noqa: PLC0415
        attach_campaign_query_plan_v4_identity,
    )

    query, span = "매월 골드 등급이었던 회원", "매월 골드 등급"
    start = query.index(span)
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None, "offer_type": None, "channels": None, "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "temporal_condition",
                    "message": "cannot represent",
                    "evidence": {"text": span, "start": start, "end": start + len(span)},
                }
            ],
        },
    }
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(payload), query, current_date="2026-08-05"
    )
    # 구조화 단계가 선언된 사유를 문장으로 나른다.
    assert "기간" in structured["semantic_ir"]["message"], structured["semantic_ir"]
    declared = structured[TEMPORAL_REJECTION_KEY]
    assert declared["code"] == temporal_claims.INTERVAL_MISSING
    assert declared["disposition"] == temporal_claims.CLARIFICATION

    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    assert result["is_success"] is False and not result["sql"]
    assert graph_rag._describe_sql_failure(plan, result).startswith(
        "이 시간 연산은 구간이 있어야"
    )
    # 운영이 읽는 구조화 진단(의무 영수증)에도 같은 사유가 남는다.
    receipts = plan.get(semantic_requirements.SOURCE_REQUIREMENT_RECEIPTS_KEY) or []
    assert any(
        receipt.get("compiler") == temporal_claims.OWNER
        and receipt.get("status") == temporal_claims.CLARIFICATION
        and temporal_claims.INTERVAL_MISSING in str(receipt.get("reason"))
        for receipt in receipts
    ), receipts
