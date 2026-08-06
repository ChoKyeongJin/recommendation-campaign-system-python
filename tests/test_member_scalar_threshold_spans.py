"""``30일`` 이 임계값인가 시간 창인가 — **청구된 구간 자체**를 고정한다.

같은 원장 원자(``duration '30일'``)가 '구매주기가 30일 이하'에서는 스칼라 임계값이고 '최근
30일 구매'에서는 롤링 창이다. 무엇인지는 최종 표현의 구조와 원문이 함께 정하고, 그 판정을
:func:`member_scalar_metric_claims.consumed_scalar_threshold_spans` 하나가 소유한다.

**이 파일이 ``issues == []`` 를 재지 않는 이유**: 시간 검증기는 `expected > 실제 창 수` 라는
**단방향** 비교라 과도한 청구를 스스로 검출하지 못한다(진짜 창까지 마스킹해도 통과다).
그러므로 회귀 고정은 청구 함수가 돌려준 **구간 좌표**여야 한다. 창 개수만 세도 안 된다 —
혼합문에서 ``((6,9),)`` 도 ``((0,12),)`` 도 남는 창이 똑같이 하나다.

표면어·지표명을 여기 손으로 심지 않는다. 문장은 회원 지표 레지스트리의 ``synonyms`` 와
``threshold_unit``, 정규화 어휘의 공개 투영(:mod:`condition_normalizers`,
:mod:`semantic_domain_binding`)에서 파생하고, 파생이 비면 초록이 되지 않도록 선언 전수를
먼저 잰다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import condition_normalizers  # noqa: E402
import event_ir  # noqa: E402
import event_parser  # noqa: E402
import member_scalar_metric_claims  # noqa: E402
import member_scalar_metrics  # noqa: E402
import semantic_domain_binding  # noqa: E402
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

CURRENT_DATE = "2026-08-04"
# 임계값 크기. 지표마다 뜻이 달라도 상관없다 — 재는 것은 값의 타당성이 아니라 **어느 구간을
# 임계값으로 소비했는가**다.
THRESHOLD_MAGNITUDE = 30

_CATALOG = audience_runtime.resolve_audience_catalog()
_REGISTRY: dict[str, Any] = audience_runtime.member_metric_registry_snapshot() or {}
_SNAPSHOT_METRICS: dict[str, Any] = audience_runtime.catalog_snapshot().get("metrics") or {}
_DECLARATIONS: tuple[dict[str, Any], ...] = member_scalar_metric_claims._declared_metrics(
    _REGISTRY
)
_COMPARISON_SURFACES: dict[str, str] = condition_normalizers.comparison_literal_operators()
# 임계값 접미어 **후보** 풀이다. 어느 접미어가 어느 단위인지는 여기서 정하지 않는다 —
# 문장을 만들어 실제로 추출해 보고 지표 선언과 맞는 것만 고른다(아래 :func:`_sentence`).
# 통화 표면어만 공개 투영이 없어 후보로 적는다: 맞는지는 추출기가 판정한다.
_THRESHOLD_SURFACE_CANDIDATES: tuple[str, ...] = (
    *sorted(condition_normalizers.numeric_duration_unit_semantics()),
    *sorted(semantic_domain_binding.counter_units()),
    "원",
)


def _metric_ids(declarations: tuple[dict[str, Any], ...]) -> list[str]:
    return [str(declaration["metric_id"]) for declaration in declarations]


def _subject_particle(word: str) -> str:
    """주격 조사 — 종성 유무로 정한다(어휘가 아니라 한글 코드포인트 산술)."""

    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28:
        return "이"
    return "가"


def _metric_alias(declaration: dict[str, Any]) -> str:
    """레지스트리가 선언한 표면어 중 **가장 긴 것**.

    짧은 표면어는 다른 지표의 긴 표면어 안에 들어 있을 수 있고, 그때 후보 판정이 포함 관계로
    갈린다. 가장 긴 것을 쓰면 문장이 어느 지표를 말하는지가 구간으로 확정된다.
    """

    return max(sorted(declaration["synonyms"]), key=len)


def _operator_surface(symbol: str) -> str:
    """비교 기호를 말하는 한국어 표면어(정규화 어휘의 공개 투영에서 파생)."""

    surfaces = sorted(
        surface
        for surface, canonical in _COMPARISON_SURFACES.items()
        if canonical == symbol and not surface.isascii()
    )
    assert surfaces, f"비교 기호 {symbol} 를 말하는 표면어가 어휘에 없다."
    return surfaces[0]


def _compose(
    declaration: dict[str, Any],
    *,
    surface: str,
    operator_surface: str,
    magnitude: int = THRESHOLD_MAGNITUDE,
) -> str:
    alias = _metric_alias(declaration)
    return f"{alias}{_subject_particle(alias)} {magnitude}{surface} {operator_surface}인 회원"


def _bindings(query: str) -> list[dict[str, Any]]:
    return extract_literal_bindings(query, current_date=CURRENT_DATE)


@dataclass(frozen=True)
class _ThresholdSentence:
    """지표 하나를 임계와 비교하는 문장 + 그 문장에서 실제로 추출된 원장 좌표."""

    declaration: dict[str, Any]
    query: str
    bindings: tuple[dict[str, Any], ...]
    threshold_span: tuple[int, int]
    operator_span: tuple[int, int]
    value: Any
    symbol: str

    @property
    def literal_spans(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted({self.threshold_span, self.operator_span}))


def _sentence(
    declaration: dict[str, Any],
    *,
    symbol: str,
    magnitude: int = THRESHOLD_MAGNITUDE,
) -> _ThresholdSentence:
    """선언한 단위로 읽히는 임계 문장을 **실측으로** 만든다.

    접미어 후보를 하나씩 넣어 보고, 추출기가 선언 단위(``threshold_unit``)로 읽어 낸 임계값
    하나와 비교어 하나만 나오는 문장을 고른다. 어느 접미어가 어느 단위인지를 이 파일이
    주장하지 않는 것이 요점이다.
    """

    for surface in _THRESHOLD_SURFACE_CANDIDATES:
        query = _compose(
            declaration,
            surface=surface,
            operator_surface=_operator_surface(symbol),
            magnitude=magnitude,
        )
        bindings = _bindings(query)
        if len(bindings) != 2:
            continue
        thresholds = [
            (row, member_scalar_metric_claims._threshold_unit_and_value(row))
            for row in bindings
        ]
        matched = [
            (row, unit_value)
            for row, unit_value in thresholds
            if unit_value is not None and unit_value[0] == declaration["threshold_unit"]
        ]
        operators = [row for row in bindings if row.get("kind") == "comparison_operator"]
        if len(matched) != 1 or len(operators) != 1:
            continue
        threshold, unit_value = matched[0]
        return _ThresholdSentence(
            declaration=declaration,
            query=query,
            bindings=tuple(bindings),
            threshold_span=(threshold["start"], threshold["end"]),
            operator_span=(operators[0]["start"], operators[0]["end"]),
            value=unit_value[1],
            symbol=symbol,
        )
    raise AssertionError(
        f"{declaration['metric_id']} 의 선언 단위({declaration['threshold_unit']})로 읽히는 "
        "임계 문장을 만들지 못했다."
    )


def _expression(
    declaration: dict[str, Any],
    *,
    query: str,
    symbol: str,
    value: Any,
) -> event_ir.Condition:
    """카탈로그가 소유한 낮추기로 만든 정답 모양(테스트가 wire 를 손으로 적지 않는다)."""

    predicate = member_scalar_metrics.lower_member_scalar_metric(
        _CATALOG,
        str(declaration["catalog_metric_id"]),
        operator=symbol,
        value=value,
        evidence=event_ir.Evidence(text=query, start=0, end=len(query)),
    )
    return predicate.expression


def _claim(
    query: str,
    expression: event_ir.Condition,
    bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    registry: dict[str, Any] | None = None,
) -> tuple[tuple[int, int], ...]:
    return member_scalar_metric_claims.consumed_scalar_threshold_spans(
        query,
        expression,
        list(bindings),
        _REGISTRY if registry is None else registry,
        _CATALOG,
    )


def _rolling_window_declarations() -> tuple[dict[str, Any], ...]:
    """임계 접미어가 **진짜 롤링 창으로도 읽히는** 지표만.

    창 보존을 재려면 마스킹 전후로 셀 창이 실제로 있어야 한다. 어느 단위가 그런지는 이 파일이
    정하지 않고 :func:`event_parser.source_time_span_count` 에게 묻는다.
    """

    resolved: list[dict[str, Any]] = []
    for declaration in _DECLARATIONS:
        sentence = _sentence(declaration, symbol="<=")
        surface = sentence.query[sentence.threshold_span[0] : sentence.threshold_span[1]]
        window_only = f"최근 {surface} 구매한 회원"
        if event_parser.source_time_span_count(window_only, today=None) == 1:
            resolved.append(declaration)
    return tuple(resolved)


_ROLLING_WINDOW_DECLARATIONS = _rolling_window_declarations()
_KOREAN_COMPARISON_SYMBOLS: tuple[str, ...] = tuple(
    sorted(
        {
            canonical
            for surface, canonical in _COMPARISON_SURFACES.items()
            if not surface.isascii()
        }
    )
)


def _probe_declaration() -> dict[str, Any]:
    """단일 지표로 재는 항목의 대표. 창과 충돌하는 지표를 쓰는 것이 이 파일의 요점이다."""

    return _ROLLING_WINDOW_DECLARATIONS[0]


# ── 0. 파생이 비면 초록이 되지 않는다 ──────────────────────────────────────────────


def test_the_registry_declares_every_member_scalar_metric_the_catalog_registers() -> None:
    """파라미터를 파생으로 세는 파일의 전제 — 선언이 사라지면 여기서 먼저 빨개진다."""

    assert _DECLARATIONS, "회원 지표 레지스트리에 임계 선언이 하나도 없다."
    assert {str(item["catalog_metric_id"]) for item in _DECLARATIONS} == set(
        member_scalar_metrics.member_scalar_metric_ids(_CATALOG)
    )
    assert _ROLLING_WINDOW_DECLARATIONS, (
        "임계 접미어가 시간 창으로도 읽히는 지표가 없다 — 이 파일이 재려는 충돌이 사라졌다."
    )
    assert _KOREAN_COMPARISON_SYMBOLS, "한국어 비교 표면어가 어휘에 없다."


# ── 1. 기본 성공: 선언된 지표 전수 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "declaration", _DECLARATIONS, ids=_metric_ids(_DECLARATIONS)
)
def test_a_declared_threshold_sentence_claims_exactly_its_two_literals(
    declaration: dict[str, Any],
) -> None:
    """임계값 구간과 비교어 구간을 청구한다 — 지표 하나에만 통하는 규칙이 아니다."""

    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value
    )
    assert _claim(sentence.query, expression, sentence.bindings) == sentence.literal_spans


@pytest.mark.parametrize(
    "declaration", _ROLLING_WINDOW_DECLARATIONS, ids=_metric_ids(_ROLLING_WINDOW_DECLARATIONS)
)
def test_the_claim_removes_the_threshold_from_the_time_window_count(
    declaration: dict[str, Any],
) -> None:
    """청구가 실제로 창 계수를 지운다 — 이것이 없으면 임계 문장이 '창이 사라졌다'로 막힌다."""

    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value
    )
    claimed = _claim(sentence.query, expression, sentence.bindings)

    assert event_parser.source_time_span_count(sentence.query, today=None) == 1
    assert (
        event_parser.source_time_span_count(
            sentence.query, today=None, masked_spans=claimed
        )
        == 0
    )


@pytest.mark.parametrize("symbol", _KOREAN_COMPARISON_SYMBOLS)
def test_every_declared_comparison_surface_claims_its_literals(symbol: str) -> None:
    """비교 표면어 전수 — '이하' 하나에만 맞춘 규칙이 아니다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol=symbol)
    expression = _expression(
        declaration, query=sentence.query, symbol=symbol, value=sentence.value
    )
    assert _claim(sentence.query, expression, sentence.bindings) == sentence.literal_spans


# ── 2. 반대 방향 안전: 창 문장에 환각 지표가 붙어도 청구하지 않는다 ────────────────


@pytest.mark.parametrize(
    "declaration", _ROLLING_WINDOW_DECLARATIONS, ids=_metric_ids(_ROLLING_WINDOW_DECLARATIONS)
)
def test_a_window_only_sentence_claims_nothing_when_the_expression_hallucinates_a_metric(
    declaration: dict[str, Any],
) -> None:
    """지표 표면어가 원문에 없으면 청구하지 않는다 — **이것이 유일한 방어선이다**.

    검증기는 창의 개수만 비교하므로 과도한 마스킹을 스스로 검출하지 못한다. 환각 표현의 근거
    구간이 기간 구간을 덮으면 리터럴 백스톱도 뜨지 않는다(실측). 즉 이 문장에서 뜻이
    ``임계 비교`` 로 조용히 바뀌는 것을 막는 것은 표면어 등장·국소 인접 조건뿐이다.
    """

    reference = _sentence(declaration, symbol="<=")
    surface = reference.query[reference.threshold_span[0] : reference.threshold_span[1]]
    query = f"최근 {surface} 구매한 회원"
    bindings = _bindings(query)

    assert (
        member_scalar_metric_claims._alias_candidates(query, _DECLARATIONS) == []
    ), "이 문장에는 지표 표면어가 없어야 한다(청구가 없는 근거)."

    expression = _expression(
        declaration, query=query, symbol="<=", value=reference.value
    )
    claimed = _claim(query, expression, bindings)

    assert claimed == ()
    assert (
        event_parser.source_time_span_count(query, today=None, masked_spans=claimed) == 1
    ), "청구가 없으므로 진짜 창은 그대로 남아야 한다."


# ── 3. 혼합문: 임계값만 청구하고 진짜 창은 남긴다 ─────────────────────────────────


@pytest.mark.parametrize(
    "declaration", _ROLLING_WINDOW_DECLARATIONS, ids=_metric_ids(_ROLLING_WINDOW_DECLARATIONS)
)
def test_a_mixed_sentence_claims_only_the_threshold_and_keeps_the_real_window(
    declaration: dict[str, Any],
) -> None:
    """같은 표면어가 둘이면 비교어 앞의 것만 임계값이다 — 뒤의 창은 지우지 않는다."""

    reference = _sentence(declaration, symbol="<=")
    surface = reference.query[reference.threshold_span[0] : reference.threshold_span[1]]
    alias = _metric_alias(declaration)
    operator_surface = _operator_surface("<=")
    query = (
        f"{alias}{_subject_particle(alias)} {surface} {operator_surface}이고 "
        f"최근 {surface} 이내 구매한 회원"
    )
    bindings = _bindings(query)

    operator_row = next(
        row for row in bindings if row.get("kind") == "comparison_operator"
    )
    thresholds = [
        row
        for row in bindings
        if member_scalar_metric_claims._threshold_unit_and_value(row) is not None
    ]
    assert len(thresholds) == 2, "혼합문에는 같은 모양의 임계값 후보가 둘 있어야 한다."
    before, after = sorted(thresholds, key=lambda row: row["start"])
    assert before["end"] <= operator_row["start"] <= after["start"]

    expression = _expression(
        declaration, query=query, symbol="<=", value=reference.value
    )
    claimed = _claim(query, expression, bindings)

    assert claimed == tuple(
        sorted(
            {
                (before["start"], before["end"]),
                (operator_row["start"], operator_row["end"]),
            }
        )
    )
    assert (after["start"], after["end"]) not in claimed
    assert event_parser.source_time_span_count(query, today=None) == 2
    assert (
        event_parser.source_time_span_count(query, today=None, masked_spans=claimed) == 1
    ), "진짜 창 하나가 남아야 한다 — 창 수만 세면 잘못된 구간을 지워도 같은 값이 나온다."


# ── 4. 조건별 fail-close 분해 ─────────────────────────────────────────────────────


def test_a_bare_comparison_is_not_a_scalar_threshold_claim() -> None:
    """회원 행 하나를 읽는 모양(Exists)이 아니면 청구하지 않는다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value
    )
    assert isinstance(expression, event_ir.Exists)
    assert isinstance(expression.relation, event_ir.Filter)

    assert _claim(sentence.query, expression.relation.where, sentence.bindings) == ()


def test_a_source_outside_the_member_scalar_contract_claims_nothing() -> None:
    """같은 표면어를 가진 **전역 순위** 쌍둥이 지표로는 임계 구간을 가져가지 못한다.

    같은 물리 컬럼에 계약이 둘 걸려 있다. 순위 계약은 모집단·NULL 정책이 다른 별개의 SQL이고,
    그쪽으로 간 표현이 회원별 임계 청구를 얻으면 뜻이 바뀐 채 창만 지워진다.
    """

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    catalog_id = str(declaration["catalog_metric_id"])
    label = _SNAPSHOT_METRICS[catalog_id].get("label")
    twins = sorted(
        metric_id
        for metric_id, metric in _SNAPSHOT_METRICS.items()
        if metric.get("label") == label
        and metric.get("kind") != member_scalar_metrics.MEMBER_SCALAR_KIND
        and metric.get("expression_field")
    )
    assert twins, f"{catalog_id} 와 같은 표면어를 가진 다른 계약이 카탈로그에 없다."

    evidence = event_ir.Evidence(text=sentence.query, start=0, end=len(sentence.query))
    twin = twins[0]
    expression = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source(twin),
            event_ir.Comparison(
                "<=",
                event_ir.FieldRef(str(_SNAPSHOT_METRICS[twin]["expression_field"])),
                event_ir.Literal(sentence.value),
                evidence=evidence,
            ),
        ),
        evidence=evidence,
    )
    assert _claim(sentence.query, expression, sentence.bindings) == ()


def test_an_unregistered_source_symbol_claims_nothing() -> None:
    """카탈로그에 없는 심볼은 '해당 없음'으로 접힌다 — 예외로 터지지도, 청구하지도 않는다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    evidence = event_ir.Evidence(text=sentence.query, start=0, end=len(sentence.query))
    ghost = "member_scalar_not_a_declared_metric"
    expression = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source(ghost),
            event_ir.Comparison(
                "<=",
                event_ir.FieldRef(f"{ghost}.value"),
                event_ir.Literal(sentence.value),
                evidence=evidence,
            ),
        ),
        evidence=evidence,
    )
    assert _claim(sentence.query, expression, sentence.bindings) == ()


def test_a_metric_whose_surface_is_absent_from_the_sentence_claims_nothing() -> None:
    """원문이 말한 지표와 표현이 든 지표가 다르면 청구하지 않는다."""

    spoken = _probe_declaration()
    sentence = _sentence(spoken, symbol="<=")
    other = next(
        item
        for item in sorted(_DECLARATIONS, key=lambda row: str(row["metric_id"]))
        if item["catalog_metric_id"] != spoken["catalog_metric_id"]
    )
    expression = _expression(
        other, query=sentence.query, symbol="<=", value=sentence.value
    )
    assert _claim(sentence.query, expression, sentence.bindings) == ()


@pytest.mark.parametrize("symbol", _KOREAN_COMPARISON_SYMBOLS)
def test_an_operator_that_disagrees_with_the_sentence_claims_nothing(symbol: str) -> None:
    """표현의 비교 기호가 원문 비교어와 다르면 그 구간을 소비했다고 말할 수 없다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol=symbol)
    disagreeing = next(
        item for item in _KOREAN_COMPARISON_SYMBOLS if item != symbol
    )
    expression = _expression(
        declaration, query=sentence.query, symbol=disagreeing, value=sentence.value
    )
    assert _claim(sentence.query, expression, sentence.bindings) == ()


def test_a_threshold_value_that_disagrees_with_the_sentence_claims_nothing() -> None:
    """표현의 임계값이 원문 숫자와 다르면 그 리터럴을 소비한 것이 아니다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value + 1
    )
    assert _claim(sentence.query, expression, sentence.bindings) == ()


def _mismatched_unit_query(declaration: dict[str, Any], surface: str) -> str | None:
    """선언 단위와 **다른** 단위로 읽히는 임계 문장(만들 수 없으면 ``None``)."""

    query = _compose(
        declaration, surface=surface, operator_surface=_operator_surface("<=")
    )
    bindings = _bindings(query)
    if len(bindings) != 2:
        return None
    units = {
        unit_value[0]
        for unit_value in (
            member_scalar_metric_claims._threshold_unit_and_value(row) for row in bindings
        )
        if unit_value is not None
    }
    if len(units) != 1 or next(iter(units)) == declaration["threshold_unit"]:
        return None
    return query


def test_a_unit_that_disagrees_with_the_declaration_claims_nothing() -> None:
    """선언 단위와 다른 단위로 쓰인 임계값은 청구하지 않는다(단위 추측 금지)."""

    declaration = _probe_declaration()
    reference = _sentence(declaration, symbol="<=")
    matched_surface = reference.query[
        reference.threshold_span[0] : reference.threshold_span[1]
    ]
    alien = next(
        surface
        for surface in _THRESHOLD_SURFACE_CANDIDATES
        if not matched_surface.endswith(surface)
        and _mismatched_unit_query(declaration, surface) is not None
    )
    query = _mismatched_unit_query(declaration, alien)
    assert query is not None
    bindings = _bindings(query)
    expression = _expression(
        declaration, query=query, symbol="<=", value=reference.value
    )
    assert _claim(query, expression, bindings) == ()


@pytest.mark.parametrize("registry", [{}, {"metrics": []}, {"metrics": [{}]}])
def test_a_registry_without_declarations_claims_nothing(registry: dict[str, Any]) -> None:
    """선언이 없으면 대조할 단위도 표면어도 없다 — 청구하지 않는다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value
    )
    assert _claim(sentence.query, expression, sentence.bindings, registry=registry) == ()


def test_an_expression_without_literal_bindings_claims_nothing() -> None:
    """원장이 비면 어느 구간을 소비했는지 증명할 수 없다."""

    declaration = _probe_declaration()
    sentence = _sentence(declaration, symbol="<=")
    expression = _expression(
        declaration, query=sentence.query, symbol="<=", value=sentence.value
    )
    assert _claim(sentence.query, expression, []) == ()
