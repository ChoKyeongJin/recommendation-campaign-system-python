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

import re
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
        # 값 없이 방향만 말한 전이. **같은 연산자**이고 술어만 다르다 — 방향은 '무엇이
        # 바뀌었나'를 좁힐 뿐 '바뀜'의 뜻을 바꾸지 않는다.
        "최근에 등급이 승급한 회원",
        treg.DIRECT_TRANSITION,
        sir.AsOfSelector,
        sir.ExistsQuantifier,
        sir.DirectionalTransitionPredicate,
    ),
    (
        "최근 6개월 동안 등급이 강등된 회원",
        treg.DIRECT_TRANSITION,
        sir.WindowSelector,
        sir.ExistsQuantifier,
        sir.DirectionalTransitionPredicate,
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
        # 승급 = rank(현재) > rank(직전). 도착값마다 "현재 = v ∧ 직전 < v" 한 항이고,
        # 부등호는 순서 도메인 전개로 물리값 IN 목록이 된다. 서열 최하위는 도착값이 될 수 없다.
        "최근에 등급이 승급한 회원",
        ("MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'",
         "MS.PREV_ZTS_GRADE IN ('MEM_GRADE_CD.WELCOME', 'MEM_GRADE_CD.FAMILY', "
         "'MEM_GRADE_CD.SILVER', 'MEM_GRADE_CD.GOLD')",
         "MS.ZTS_GRADE = 'MEM_GRADE_CD.FAMILY'"),
    ),
    (
        "등급이 강등된 회원",
        ("MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'",
         "MS.PREV_ZTS_GRADE IN ('MEM_GRADE_CD.VIP')"),
    ),
    (
        # 축이 다르면 컬럼도 값 사전도 다르다. 이 항목이 '가치등급' 이 '등급' 에 흡수되는
        # 오축 해석의 회귀 가드다(2026-08-08).
        "가치등급이 승급한 회원",
        ("MS.WORTH_GRADE = 'VVIP'", "MS.PREV_WORTH_GRADE IN ("),
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


# ── 4-1. 방향 전이: 값 없이 방향만 말한 요청 ────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected_metric", "expected_direction"),
    [
        ("최근에 등급이 승급한 회원", "member.grade", sir.TransitionDirection.ASCENDING),
        ("등급이 강등된 회원", "member.grade", sir.TransitionDirection.DESCENDING),
        ("가치등급이 승급한 회원", "member.worth_grade", sir.TransitionDirection.ASCENDING),
        ("가치등급이 강등된 회원", "member.worth_grade", sir.TransitionDirection.DESCENDING),
    ],
)
def test_a_direction_word_alone_picks_the_axis_and_the_direction(
    query, expected_metric, expected_direction, snapshot, semantic_catalog, runtime
) -> None:
    """방향어 하나가 축(어느 값 사전인가)과 방향(어느 쪽인가)을 모두 확정한다.

    축을 표면어 **최장 일치**로 가리는 것이 이 테스트의 절반이다 — '가치등급'은 '등급'을
    포함하므로, 짧은 쪽이 이기면 가치등급 요청이 조용히 ZTS 축으로 컴파일된다.
    """

    detected = _detect(query, snapshot, semantic_catalog, runtime)
    assert isinstance(detected, tuple) and len(detected) == 1, detected
    predicate = detected[0].condition.predicate
    assert isinstance(predicate, sir.DirectionalTransitionPredicate)
    assert detected[0].metric_id == expected_metric
    assert predicate.direction is expected_direction


def test_the_direction_only_transition_never_leaves_the_declared_values(
    snapshot, semantic_catalog, runtime, context
) -> None:
    """전개된 값은 전부 **선언된 값**이다 — '직전 없음' 센티널은 어느 쪽에도 오지 않는다.

    PREV_WORTH_GRADE 에는 직전 값이 없다는 뜻의 ``'~'`` 가 실제로 1,273 행 있다(2026-08-03
    실측). 그 행은 승급도 강등도 아니므로 IN 목록에 들어오면 안 되고, 목록을 선언된 순서에서
    만드는 한 자연히 빠진다 — 그 성질을 여기서 고정한다.
    """

    outcome = _synthesize("가치등급이 승급한 회원", snapshot, semantic_catalog, runtime, context)
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis), outcome
    sql = _sql(semantic_catalog, outcome.expression)
    assert "~" not in sql
    declared = {
        str(payload.get("physical"))
        for payload in snapshot["value_domains"]["worth_grade"]["values"].values()
    }
    compared = {
        value
        for fragment in re.findall(r"(?:PREV_)?WORTH_GRADE\s*(?:=|IN)\s*(\([^)]*\)|'[^']*')", sql)
        for value in re.findall(r"'([^']*)'", fragment)
    }
    assert compared, f"가치등급 비교가 하나도 없다:\n{sql}"
    assert compared <= declared, sorted(compared - declared)


def test_the_direction_only_transition_keeps_its_own_observation_window(
    snapshot, semantic_catalog, runtime, context
) -> None:
    """기간이 없으면 최신 관측(as_of), 있으면 그 구간 — 값 쌍 전이와 **같은 규칙**이다.

    '최근'만으로는 기간이 되지 않는다는 것이 요점이다. 그 낱말이 창을 넓히면 조용히 다른
    집합이 나가고, 결핍으로 읽히면 답할 수 있는 요청이 되묻기로 닫힌다.
    """

    bare = _synthesize("최근에 등급이 승급한 회원", snapshot, semantic_catalog, runtime, context)
    windowed = _synthesize(
        "최근 6개월 동안 등급이 승급한 회원", snapshot, semantic_catalog, runtime, context
    )
    assert isinstance(bare, temporal_claims.TemporalClaimSynthesis), bare
    assert isinstance(windowed, temporal_claims.TemporalClaimSynthesis), windowed
    assert "MS.YYYYMM >= '202608'" in _sql(semantic_catalog, bare.expression)
    assert "MS.YYYYMM >= '202603'" in _sql(semantic_catalog, windowed.expression)


def test_an_unordered_axis_cannot_be_given_a_direction(semantic_catalog, runtime) -> None:
    """서열이 선언되지 않은 축에는 방향이 없다 — 낮추기 전에 이름을 대며 막힌다.

    이 판정의 근거는 낱말이 아니라 선언이다(지표의 supported_comparisons). 서열 없는 코드
    도메인에 부등호를 허용하면 저장 문자열 순서로 조용히 틀린 집합이 나온다.
    """

    condition = sir.TemporalCondition(
        metric="member.newproduct_favor",
        binding=None,
        selector=sir.AsOfSelector(anchor=sir.ReferenceAnchor(), strategy=sir.ExactBucket()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.DirectionalTransitionPredicate(
            direction=sir.TransitionDirection.ASCENDING,
            transition_mode=sir.TransitionMode.DIRECT_OBSERVED_TRANSITION,
        ),
        evidence=sir.Evidence(text="승급", start=0, end=2),
    )
    outcome = runtime.lower(condition, sir.TemporalRequestContext(now=NOW))
    assert not isinstance(outcome, temporal_ir.TemporalLoweringReceipt), outcome
    assert outcome.code in {
        "temporal_comparison_unsupported",
        "temporal_value_order_undeclared",
        "temporal_operator_unsupported_by_metric",
    }, outcome


def test_every_transition_axis_declares_an_order(snapshot, runtime) -> None:
    """전이를 받는 축은 **서열이 선언돼 있다** — 방향 전이가 성립할 수 있는 조건이다.

    위 테스트가 재는 것은 '지금 카탈로그에서 막힌다'이고, 이 테스트가 재는 것은 '앞으로도
    어긋날 수 없다'이다. 서열 없는 도메인에 전이 관측을 배선하면 여기서 먼저 걸린다 —
    걸리지 않으면 '승급'이 저장 문자열 순서로 조용히 틀린 집합을 만든다.
    """

    domains = snapshot["value_domains"]
    axes = [
        (metric_id, metric.value_domain)
        for metric_id, metric in runtime.temporal_catalog.metrics.items()
        if metric.value_domain
        and any(
            treg.DIRECT_TRANSITION in runtime.temporal_catalog.bindings[binding_id].supported_operators
            for binding_id in metric.temporal_bindings
        )
    ]
    assert axes, "전이를 받는 축이 하나도 없다(배선이 끊겼다)."
    for metric_id, domain in axes:
        declaration = domains.get(domain) or {}
        assert declaration.get("ordered") and declaration.get("order"), (
            f"{metric_id!r} 은 전이를 받지만 값 도메인 {domain!r} 에 서열 선언이 없다."
        )


def test_the_directional_predicate_round_trips() -> None:
    """방향 전이도 직렬화가 대칭이다(방향과 전이 모드가 모두 남는다)."""

    predicate = sir.DirectionalTransitionPredicate(
        direction=sir.TransitionDirection.DESCENDING,
        transition_mode=sir.TransitionMode.DIRECT_OBSERVED_TRANSITION,
    )
    assert sir.predicate_from_dict(predicate.to_dict()) == predicate


@pytest.mark.parametrize(
    "query",
    [
        # 도착값만 말한 요청. 출발 쪽을 방향으로 덮으면 사용자가 말한 값이 조용히 사라진다.
        "VIP로 승급한 회원",
        # 상태 축에는 전이 지표도 이력 소스도 선언이 없다(방향어도 없다).
        "정상에서 휴면으로 바뀐 회원",
    ],
)
def test_a_partial_transition_request_stays_closed(
    query, snapshot, semantic_catalog, runtime, context
) -> None:
    """방향 전이가 열려도 **값이 반만 있는** 요청은 그대로 닫힌다."""

    outcome = _synthesize(query, snapshot, semantic_catalog, runtime, context)
    assert isinstance(outcome, temporal_claims.TemporalClaimRejection), outcome
    assert outcome.code == temporal_claims.VALUE_COUNT_MISMATCH


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
