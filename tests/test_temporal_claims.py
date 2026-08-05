"""원문 → canonical Temporal IR 생산자의 고정 계약.

이 파일이 재는 것은 세 가지다.

1. **파싱** — 같은 뜻의 문장이 같은 조합(selector × quantifier × predicate)으로 간다.
   문형별 노드를 만들지 않았으므로, 고정할 것은 '어떤 클래스가 나왔는가'가 아니라
   '어떤 조합이 나왔고 그 조합이 어떤 연산자로 파생되는가'이다.
2. **컴파일** — 그 조합이 실제로 SQL 로 낮아진다(모양을 고정하되 행 수는 재지 않는다).
   실데이터가 성공을 증명해 주지 못하기 때문이다: 등급 전이는 적재된 달에서 0건이고
   월 스냅샷은 한 달뿐이다. 그래서 계약은 **SQL 조각과 귀결 코드**로 건다.
3. **닫힘** — 데이터 표현이 답할 수 없는 요청은 사유를 대며 닫힌다. 근사하지 않는다.

문장 번호나 코퍼스 id 에 의존하지 않는다 — 여기 있는 문장은 전부 이 파일 안에서 자족한다.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import targeting_domain  # noqa: E402
import temporal_claims  # noqa: E402
import temporal_ir  # noqa: E402
import temporal_semantics  # noqa: E402
from temporal_ir import registry as treg  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=SEOUL)
TODAY = date(2026, 8, 5)


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def snapshot():
    return audience_runtime.catalog_snapshot()


@pytest.fixture(scope="module")
def runtime(semantic_catalog) -> temporal_ir.TemporalRuntime:
    return temporal_ir.create_temporal_runtime(semantic_catalog)


@pytest.fixture()
def context() -> sir.TemporalRequestContext:
    return sir.TemporalRequestContext(now=NOW)


def _detect(query: str, snapshot, semantic_catalog, runtime):
    return temporal_claims.detect_temporal_claims(
        query,
        snapshot=snapshot,
        catalog=semantic_catalog,
        runtime=runtime,
        today=TODAY,
    )


def _synthesize(query: str, snapshot, semantic_catalog, runtime, context):
    return temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot,
        catalog=semantic_catalog,
        runtime=runtime,
        context=context,
        today=TODAY,
    )


def _sql(semantic_catalog, expression) -> str:
    return event_compiler.compile_condition(
        expression, semantic_catalog.compile_context(today=TODAY, literals=True)
    ).sql


# ── 1. 파싱: 문장 → 조합 ────────────────────────────────────────────────────────

# (원문, 기대 연산자, 기대 selector 타입, 기대 quantifier 타입, 기대 predicate 타입)
PARSE_CASES: tuple[tuple[str, str, type, type, type], ...] = (
    (
        "지난달 말 기준 VIP였던 회원",
        treg.AS_OF,
        sir.AsOfSelector,
        sir.ExistsQuantifier,
        sir.StatePredicate,
    ),
    (
        "직전 등급이 골드였던 회원",
        treg.PREVIOUS_BUCKET,
        sir.PreviousSelector,
        sir.ExistsQuantifier,
        sir.StatePredicate,
    ),
    (
        "골드에서 VIP로 승급한 회원",
        treg.DIRECT_TRANSITION,
        sir.AsOfSelector,
        sir.ExistsQuantifier,
        sir.TransitionPredicate,
    ),
    (
        "최근 6개월 동안 골드에서 VIP로 승급한 회원",
        treg.DIRECT_TRANSITION,
        sir.WindowSelector,
        sir.ExistsQuantifier,
        sir.TransitionPredicate,
    ),
    (
        "최근 6개월 내내 골드 등급을 유지한 회원",
        treg.ALL_OBSERVATIONS,
        sir.WindowSelector,
        sir.AllObservationsQuantifier,
        sir.StatePredicate,
    ),
    (
        "지난 6개월 매월 골드 등급이었던 회원",
        treg.EVERY_BUCKET,
        sir.WindowSelector,
        sir.EveryBucketQuantifier,
        sir.StatePredicate,
    ),
    (
        "최근 1년 동안 등급이 3회 이상 변경된 회원",
        treg.CHANGE_COUNT,
        sir.WindowSelector,
        sir.ExistsQuantifier,
        sir.ChangeCountPredicate,
    ),
    (
        "3개월 연속 골드 등급이었던 회원",
        treg.CONSECUTIVE_BUCKETS,
        sir.WindowSelector,
        sir.ConsecutiveBucketsQuantifier,
        sir.StatePredicate,
    ),
)


@pytest.mark.parametrize(
    ("query", "operator", "selector_type", "quantifier_type", "predicate_type"),
    PARSE_CASES,
    ids=[case[0] for case in PARSE_CASES],
)
def test_a_sentence_becomes_the_declared_combination(
    query, operator, selector_type, quantifier_type, predicate_type,
    snapshot, semantic_catalog, runtime,
) -> None:
    """문장은 조합이 되고, 연산자 이름은 그 조합에서 파생된다(생산자가 짓지 않는다)."""

    detected = _detect(query, snapshot, semantic_catalog, runtime)
    assert isinstance(detected, tuple), detected
    assert len(detected) == 1, [request.operator for request in detected]
    condition = detected[0].condition
    assert isinstance(condition.selector, selector_type)
    assert isinstance(condition.quantifier, quantifier_type)
    assert isinstance(condition.predicate, predicate_type)
    assert treg.resolve_operator_name(condition) == operator


def test_the_evidence_points_at_the_exact_source_text(
    snapshot, semantic_catalog, runtime
) -> None:
    """근거 구간은 원문을 정확히 가리킨다 — 합성이 근거를 지어내지 않는다."""

    query = "최근 6개월 동안 골드에서 VIP로 승급한 회원"
    detected = _detect(query, snapshot, semantic_catalog, runtime)
    assert isinstance(detected, tuple)
    for request in detected:
        evidence = request.condition.evidence
        assert evidence is not None
        assert query[evidence.start : evidence.end] == evidence.text
        for start, end in request.spans:
            assert 0 <= start < end <= len(query)


# ── 2. 카탈로그 범용성 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected_metric"),
    [
        ("골드에서 VIP로 바뀐 회원", "member.grade"),
        ("가치등급이 골드에서 VIP로 바뀐 회원", "member.worth_grade"),
    ],
)
def test_both_value_axes_use_the_same_code_path(
    query, expected_metric, snapshot, semantic_catalog, runtime
) -> None:
    """축이 다른 것은 **선언**뿐이다 — 같은 판정, 같은 조합, 같은 lowering."""

    detected = _detect(query, snapshot, semantic_catalog, runtime)
    assert isinstance(detected, tuple)
    assert detected[0].metric_id == expected_metric
    assert isinstance(detected[0].condition.predicate, sir.TransitionPredicate)


def test_the_producer_never_hardcodes_a_value_or_axis_word() -> None:
    """값·축 낱말이 생산자 실행 상수에 박혀 있지 않다(어휘의 소유자는 카탈로그다)."""

    import ast

    source = (REPO_ROOT / "temporal_claims.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    constants = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    forbidden = set(targeting_domain.attribute_value_terms()) | set(
        targeting_domain.attribute_axis_terms()
    )
    for term in forbidden:
        for constant in constants:
            assert term not in constant, (
                f"값/축 표면어 {term!r} 가 생산자 실행 상수에 박혀 있다 — "
                "어휘의 소유자는 카탈로그다."
            )


def test_every_closed_operator_has_a_declared_plan() -> None:
    """범용 연산자 집합과 조합 선언표가 갈라지지 않는다(광고만 하고 못 만드는 연산 금지)."""

    assert set(temporal_claims._OPERATOR_PLANS) == set(temporal_semantics.OPERATORS)


# ── 3. 컴파일: 조합 → SQL ───────────────────────────────────────────────────────

# 행 수가 아니라 **뜻**을 고정한다. 등급 축 전이는 적재된 달에서 0건이므로 결과 행으로는
# 정확성을 증명할 수 없고, 증명할 수 있는 것은 어느 컬럼을 어떤 창에서 비교하는가이다.
COMPILE_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "지난달 말 기준 VIP였던 회원",
        ("CRM_MB_MONTHCRMINFO", "MS.YYYYMM >= '202607'", "MS.YYYYMM < '202608'",
         "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'"),
    ),
    (
        "직전 등급이 골드였던 회원",
        ("MS.YYYYMM >= '202607'", "MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "골드에서 VIP로 승급한 회원",
        ("MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'", "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "가치등급이 골드에서 VIP로 바뀐 회원",
        ("MS.WORTH_GRADE = 'VIP'", "MS.PREV_WORTH_GRADE = 'GOLD'"),
    ),
    (
        "최근 6개월 동안 골드에서 VIP로 승급한 회원",
        ("MS.YYYYMM >= '202603'", "MS.YYYYMM < '202609'",
         "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "지난 6개월 매월 골드 등급이었던 회원",
        ("COUNT(DISTINCT MS.YYYYMM) = 6", "MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "최근 6개월 내내 골드 등급을 유지한 회원",
        ("NOT EXISTS", "MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
)


@pytest.mark.parametrize(
    ("query", "fragments"), COMPILE_CASES, ids=[case[0] for case in COMPILE_CASES]
)
def test_the_combination_lowers_to_the_declared_columns(
    query, fragments, snapshot, semantic_catalog, runtime, context
) -> None:
    outcome = _synthesize(query, snapshot, semantic_catalog, runtime, context)
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis), outcome
    sql = _sql(semantic_catalog, outcome.expression)
    for fragment in fragments:
        assert fragment in sql, f"{fragment!r} 가 없다:\n{sql}"


def test_the_receipt_names_the_operator_and_the_observation(
    snapshot, semantic_catalog, runtime, context
) -> None:
    """영수증은 실행 없이 '무엇으로 답했는가'를 설명할 수 있어야 한다."""

    outcome = _synthesize(
        "지난달 말 기준 VIP였던 회원", snapshot, semantic_catalog, runtime, context
    )
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis)
    receipt = outcome.receipts[0]
    assert receipt["operator"] == treg.AS_OF
    assert receipt["binding"] == "member.grade.monthly_snapshot"
    assert receipt["owner"] == temporal_claims.OWNER
    assert receipt["value_domain"] == "grade"


# ── 4. 닫힘: 답할 수 없는 요청 ──────────────────────────────────────────────────

# (원문, 기대 사유 코드). 전부 **이름 있는** 사유다 — 무언 실패는 합격이 아니다.
REJECTION_CASES: tuple[tuple[str, str], ...] = (
    # 방향어가 값 순서와 모순된다. 한쪽을 골라 SQL 을 내면 사용자가 말하지 않은 집합이 나간다.
    ("VIP에서 골드로 승급한 회원", "transition_direction_contradicted"),
    ("골드에서 VIP로 강등된 회원", "transition_direction_contradicted"),
    # 같은 값 둘은 변화가 아니다.
    ("골드에서 골드로 바뀐 회원", "transition_values_identical"),
    # 상태 축에는 전이 지표도 이력 소스도 선언이 없다.
    ("정상에서 휴면으로 바뀐 회원", temporal_claims.VALUE_COUNT_MISMATCH),
    # 월 스냅샷은 칸 안의 변경을 관측하지 못한다 — '관측된 변화 수'는 업무 변경 횟수가 아니다.
    ("최근 1년 동안 등급이 3회 이상 변경된 회원", "temporal_operator_unsupported_by_metric"),
    # 끊기지 않은 N칸은 주체별 정렬·간격 판정이 필요하고 실행 IR 에 그 primitive 가 없다.
    ("3개월 연속 골드 등급이었던 회원", "temporal_operator_unsupported_by_metric"),
    # 구간이 없으면 '매월'이 어느 범위의 매월인지 정해지지 않는다(기본값을 지어내지 않는다).
    ("매월 골드 등급이었던 회원", temporal_claims.INTERVAL_MISSING),
)


@pytest.mark.parametrize(
    ("query", "code"), REJECTION_CASES, ids=[case[0] for case in REJECTION_CASES]
)
def test_an_unanswerable_request_closes_with_a_named_reason(
    query, code, snapshot, semantic_catalog, runtime, context
) -> None:
    outcome = _synthesize(query, snapshot, semantic_catalog, runtime, context)
    assert isinstance(outcome, temporal_claims.TemporalClaimRejection), outcome
    assert outcome.code == code
    assert outcome.message.strip()
    assert outcome.evidence["text"] in query


def test_a_sentence_without_a_temporal_marker_is_not_claimed(
    snapshot, semantic_catalog, runtime, context
) -> None:
    """시간 조건이 없는 문장은 ``None`` 이다 — '만들 수 없다'와 구분한다."""

    assert (
        _synthesize(
            "최근 90일 동안 3회 이상 구매한 회원",
            snapshot, semantic_catalog, runtime, context,
        )
        is None
    )


def test_the_consecutive_operator_is_declared_unsupported_not_approximated(
    runtime,
) -> None:
    """'연속'은 이름은 있고 낮춤은 없다 — 총 칸 수 비교로 근사하지 않는다."""

    definition = runtime.registry.get(treg.CONSECUTIVE_BUCKETS)
    assert definition.lower is None
    assert definition.unsupported_reason
    assert treg.EVERY_BUCKET in definition.unsupported_reason


def test_one_bucket_is_existence_not_consecutiveness() -> None:
    """1칸 '연속'은 존재 조건이고 그 뜻은 다른 quantifier 가 이미 갖고 있다."""

    with pytest.raises(sir.TemporalIrError):
        sir.ConsecutiveBucketsQuantifier(bucket_count=1)


def test_the_consecutive_quantifier_round_trips() -> None:
    """인자를 가진 quantifier 도 직렬화가 대칭이다."""

    quantifier = sir.ConsecutiveBucketsQuantifier(bucket_count=3)
    assert sir.quantifier_from_dict(quantifier.to_dict()) == quantifier


# ── 5. 코퍼스 커버리지: 문장이 아니라 연산으로 센다 ─────────────────────────────


def test_the_corpus_is_classified_by_operation_not_by_sentence() -> None:
    """코퍼스의 모든 요청이 **의미 연산**으로 분류되고, 컴파일 커버리지가 0 이 아니다.

    문장 목록을 기대값으로 박지 않는 이유는 그것이 곧 사양이 되기 때문이다. 여기서 고정하는
    것은 (a) 모든 요청이 분류된다, (b) 분류된 연산은 전부 정본 이름이다, (c) 시간 축을 말한
    요청 중 컴파일되는 것이 존재한다 — 셋뿐이다. 문장이 늘어도 이 테스트는 그대로다.
    """

    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "tools"))
    import temporal_operation_census as census_tool  # noqa: PLC0415

    result = census_tool.census(census_tool.DEFAULT_CORPUS, today=TODAY)
    rows = result["rows"]
    assert rows, "코퍼스가 비었다."

    known = set(treg.OPERATOR_NAMES) | {census_tool.UNRELATED, "UNRESOLVED"}
    for row in rows:
        assert row["operation"] in known, row
        assert row["outcome"] in {"compiled", "unsupported", "unrelated"}
        if row["outcome"] == "compiled":
            assert row["reason"] is None
        if row["outcome"] == "unsupported":
            assert row["reason"], row

    temporal_rows = [
        row for row in rows if row["operation"] != census_tool.UNRELATED
    ]
    compiled = [row for row in temporal_rows if row["outcome"] == "compiled"]
    assert temporal_rows, "코퍼스에 시간 조건을 말한 요청이 하나도 없다."
    assert compiled, "시간 축에서 컴파일되는 요청이 하나도 없다(배선이 끊겼다)."
