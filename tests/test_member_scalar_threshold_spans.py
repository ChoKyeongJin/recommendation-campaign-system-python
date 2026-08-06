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

import audience_frame  # noqa: E402
import audience_runtime  # noqa: E402
import condition_normalizers  # noqa: E402
import event_ir  # noqa: E402
import event_parser  # noqa: E402
import member_scalar_metric_claims  # noqa: E402
import member_scalar_metrics  # noqa: E402
import metric_recipe_selection  # noqa: E402
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


# ── 5. 고정 좌표 회귀: 어느 구간을 청구했는지 자체를 못 박는다 ─────────────────────
#
# 위 절들은 문장을 레지스트리에서 파생하므로 "무엇이 청구됐는가"를 파생 기대값과 대조한다.
# 그 방식은 어휘를 코드에 심지 않는 대신 **좌표 자체를 눈으로 볼 수 없다** — 청구 함수와 기대값
# 계산이 같은 방향으로 함께 틀어지면 초록이다. 그래서 여기서는 반대로 문장과 좌표를 손으로
# 적어 고정한다. 손으로 적은 것이 레지스트리와 어긋나면 조용히 통과하지 않도록
# :func:`test_the_anchored_sentences_still_match_the_registry_declarations` 가 먼저 잰다.
#
# ``issues == []`` 를 재지 않는 이유는 파일 첫 docstring 그대로다: 시간 검증기는 단방향이라
# 과도한 청구를 검출하지 못하므로, 회귀는 **좌표**여야 한다.


@dataclass(frozen=True)
class _AnchoredClaim:
    """문장 하나 + 그 문장에서 청구돼야 하는 정확한 좌표."""

    metric_id: str
    alias: str
    query: str
    operator: str
    value: Any
    char_spans: tuple[tuple[int, int], ...]
    texts: tuple[str, ...]
    word_spans: tuple[tuple[int, int], ...]
    remaining_windows: int


ANCHORED_CLAIMS: tuple[_AnchoredClaim, ...] = (
    # 조사 문서 Q1. 청구 ((6,9),(10,12)) — 창 1 → 0.
    _AnchoredClaim(
        metric_id="buy_cycle",
        alias="구매주기",
        query="구매주기가 30일 이하인 회원",
        operator="<=",
        value=30,
        char_spans=((6, 9), (10, 12)),
        texts=("30일", "이하"),
        word_spans=((1, 2), (2, 3)),
        remaining_windows=0,
    ),
    # 조사 문서 Q3(혼합문). 앞의 '30일'만 임계값이고 뒤의 '30일'(18,21)은 진짜 창이다.
    _AnchoredClaim(
        metric_id="buy_cycle",
        alias="구매주기",
        query="구매주기가 30일 이하이고 최근 30일 이내 구매한 회원",
        operator="<=",
        value=30,
        char_spans=((6, 9), (10, 12)),
        texts=("30일", "이하"),
        word_spans=((1, 2), (2, 3)),
        remaining_windows=1,
    ),
    # 금액 단위. 임계 리터럴이 숫자+통화 표면어 전체다('100000' 이 아니라 '100000원').
    _AnchoredClaim(
        metric_id="total_buy_amt",
        alias="누적 구매금액",
        query="누적 구매금액이 100000원 이상인 회원",
        operator=">=",
        value=100000,
        char_spans=((9, 16), (17, 19)),
        texts=("100000원", "이상"),
        word_spans=((2, 3), (3, 4)),
        remaining_windows=0,
    ),
    # 표면어가 다른 지표의 표면어('구매금액')를 품은 문장. 긴 쪽이 이겨 mean_buy_amt 로 간다.
    _AnchoredClaim(
        metric_id="mean_buy_amt",
        alias="평균 구매금액",
        query="평균 구매금액이 50000원 이상인 회원",
        operator=">=",
        value=50000,
        char_spans=((9, 15), (16, 18)),
        texts=("50000원", "이상"),
        word_spans=((2, 3), (3, 4)),
        remaining_windows=0,
    ),
)

_ANCHOR_IDS: tuple[str, ...] = tuple(
    f"{anchor.metric_id}:{index}" for index, anchor in enumerate(ANCHORED_CLAIMS)
)


def _anchor_declaration(anchor: _AnchoredClaim) -> dict[str, Any]:
    matched = [item for item in _DECLARATIONS if item["metric_id"] == anchor.metric_id]
    assert matched, f"레지스트리에 {anchor.metric_id} 선언이 없다."
    return matched[0]


def _word_span(query: str, span: tuple[int, int]) -> tuple[int, int]:
    """문자 구간이 걸치는 **어절 색인** 구간(반열림).

    저장소의 좌표계는 전부 문자 오프셋이라 토큰 좌표라는 생산 개념이 없다. 그래서 여기서는
    공백으로 끊은 어절을 토큰으로 보고 그 색인을 잰다 — 청구가 '30일'을 넘어 '이하인'까지
    삼키면 문자 좌표보다 이쪽이 먼저 눈에 띈다.
    """

    cursor = 0
    bounds: list[tuple[int, int]] = []
    for word in query.split(" "):
        bounds.append((cursor, cursor + len(word)))
        cursor += len(word) + 1
    covered = [
        index
        for index, (start, end) in enumerate(bounds)
        if start < span[1] and span[0] < end
    ]
    assert covered, f"구간 {span} 이 어느 어절에도 걸치지 않는다."
    return covered[0], covered[-1] + 1


def test_the_anchored_sentences_still_match_the_registry_declarations() -> None:
    """손으로 적은 문장이 레지스트리와 어긋나면 좌표 회귀가 엉뚱한 것을 잰다."""

    assert ANCHORED_CLAIMS, "고정 좌표 회귀가 하나도 없다."
    for anchor in ANCHORED_CLAIMS:
        declaration = _anchor_declaration(anchor)
        assert anchor.alias in declaration["synonyms"], (
            f"{anchor.alias!r} 가 {anchor.metric_id} 의 선언 표면어가 아니다 — "
            "레지스트리가 바뀌었으면 문장과 좌표를 함께 고쳐라."
        )
        assert anchor.query.startswith(anchor.alias)
        for span, text in zip(anchor.char_spans, anchor.texts):
            assert anchor.query[span[0] : span[1]] == text, (
                f"{anchor.query!r} 의 {span} 은 {anchor.query[span[0]:span[1]]!r} 다."
            )


@pytest.mark.parametrize("anchor", ANCHORED_CLAIMS, ids=_ANCHOR_IDS)
def test_the_claim_pins_exact_character_and_word_spans(anchor: _AnchoredClaim) -> None:
    """청구 좌표 · 그 좌표로 잘라 낸 문자열 · 어절 색인을 모두 고정한다."""

    declaration = _anchor_declaration(anchor)
    bindings = _bindings(anchor.query)
    expression = _expression(
        declaration, query=anchor.query, symbol=anchor.operator, value=anchor.value
    )
    claimed = _claim(anchor.query, expression, bindings)

    assert claimed == anchor.char_spans
    assert tuple(anchor.query[start:end] for start, end in claimed) == anchor.texts
    assert tuple(_word_span(anchor.query, span) for span in claimed) == anchor.word_spans


@pytest.mark.parametrize("anchor", ANCHORED_CLAIMS, ids=_ANCHOR_IDS)
def test_the_claim_selects_the_recipe_the_sentence_names(anchor: _AnchoredClaim) -> None:
    """겹치는 표면어가 있어도 문장이 말한 recipe 하나만 후보로 남는다.

    '평균 구매금액'에는 다른 지표의 표면어 '구매금액'이 통째로 들어 있다. 둘 다 후보로 만들어진
    뒤 겹침 해석이 하나를 고르므로, 여기서 재는 것은 **고른 결과**다.
    """

    resolved = member_scalar_metric_claims._alias_candidates(anchor.query, _DECLARATIONS)
    assert [item[0]["metric_id"] for item in resolved] == [anchor.metric_id]
    assert [(item[1], item[2], item[3]) for item in resolved] == [
        (anchor.alias, 0, len(anchor.alias))
    ]


def _raw_surface_candidates(query: str) -> tuple[metric_recipe_selection.RecipeCandidate, ...]:
    """겹침 해석 **전** 후보 전량. 선언된 표면어가 원문에 등장한 자리를 모두 만든다."""

    return tuple(
        metric_recipe_selection.RecipeCandidate(
            recipe_id=f"{declaration['metric_id']}|{alias}|{start}",
            kind=member_scalar_metrics.MEMBER_SCALAR_KIND,
            span=(start, start + len(alias)),
            surface=alias,
        )
        for declaration in _DECLARATIONS
        for alias in sorted(set(declaration["synonyms"]))
        for start in [query.find(alias)]
        if start >= 0
    )


def test_two_recipes_can_both_claim_one_surface_and_the_resolver_keeps_one() -> None:
    """경쟁 후보를 지우지 않는다 — 둘 다 만들어지고 겹침 해석이 하나를 고른다.

    '평균 구매금액'에는 다른 지표의 표면어 '구매금액'이 통째로 들어 있다. 어느 쪽도 삭제하거나
    비활성화하지 않으므로, 남는 문제는 "겹칠 때 무엇을 고르는가"뿐이다.
    """

    anchor = next(item for item in ANCHORED_CLAIMS if item.metric_id == "mean_buy_amt")
    raw = _raw_surface_candidates(anchor.query)
    competing = [
        candidate
        for candidate in raw
        if candidate.span is not None and candidate.span[0] < len(anchor.alias)
    ]
    assert len({candidate.recipe_id.split("|")[0] for candidate in competing}) >= 2, (
        "이 문장에서 표면어가 겹치는 지표가 둘이어야 이 테스트가 의미를 갖는다."
    )

    resolved = metric_recipe_selection.resolve_overlapping_candidates(list(raw))
    assert [candidate.surface for candidate in resolved] == [anchor.alias]
    assert (
        metric_recipe_selection.resolve_overlapping_candidates(list(reversed(raw))) == resolved
    )


@pytest.mark.parametrize("anchor", ANCHORED_CLAIMS, ids=_ANCHOR_IDS)
def test_the_claim_covers_only_the_two_literals_and_nothing_between(
    anchor: _AnchoredClaim,
) -> None:
    """필요한 최소 범위만 청구한다 — 절이나 사이 글자를 삼키지 않는다."""

    declaration = _anchor_declaration(anchor)
    claimed = _claim(
        anchor.query,
        _expression(
            declaration, query=anchor.query, symbol=anchor.operator, value=anchor.value
        ),
        _bindings(anchor.query),
    )

    assert len(claimed) == 2
    assert sum(end - start for start, end in claimed) == sum(map(len, anchor.texts))
    (_first_start, first_end), (second_start, _second_end) = claimed
    assert first_end < second_start, "두 청구가 붙어 있으면 사이 글자도 삼킨 것이다."
    assert anchor.query[first_end:second_start] not in anchor.texts


@pytest.mark.parametrize("anchor", ANCHORED_CLAIMS, ids=_ANCHOR_IDS)
def test_the_claim_leaves_every_window_it_did_not_prove(anchor: _AnchoredClaim) -> None:
    """마스킹 후 남는 창 수를 문장별로 고정한다(혼합문에서 진짜 창이 지워지지 않는다)."""

    declaration = _anchor_declaration(anchor)
    claimed = _claim(
        anchor.query,
        _expression(
            declaration, query=anchor.query, symbol=anchor.operator, value=anchor.value
        ),
        _bindings(anchor.query),
    )
    assert (
        event_parser.source_time_span_count(
            anchor.query, today=None, masked_spans=claimed
        )
        == anchor.remaining_windows
    )


@dataclass(frozen=True)
class _NonAdjacentClaim:
    """지표 표면어 · 그 단위의 임계값 · 비교 기호가 **모두** 있지만 셋이 이어지지 않는 문장.

    값·단위·기호 대조만으로는 전부 통과하므로, 청구를 막는 것은 국소 인접 하나뿐이다.
    ``would_be_spans`` 는 인접 규칙이 느슨해졌을 때 나올 좌표다 — 그 좌표가 나오면 같은 문장의
    진짜 시간 창이 지워져 뜻이 조용히 바뀐다(검증기는 창의 개수만 세므로 잡아 주지 않는다).
    """

    metric_id: str
    query: str
    operator: str
    value: Any
    would_be_spans: tuple[tuple[int, int], ...]
    gap: str


NON_ADJACENT_CLAIMS: tuple[_NonAdjacentClaim, ...] = (
    # 임계값 ↔ 비교어 사이에 절이 통째로 끼어 있다('30일'(6,9) … '이상'(25,27)).
    _NonAdjacentClaim(
        metric_id="buy_cycle",
        query="구매주기가 30일 이내 구매한 회원 중 5회 이상 구매한 회원",
        operator=">=",
        value=30,
        would_be_spans=((6, 9), (25, 27)),
        gap="value_to_operator",
    ),
    # 지표 표면어 ↔ 임계값 사이에 절이 통째로 끼어 있다('구매주기'(0,4) … '30일'(16,19)).
    _NonAdjacentClaim(
        metric_id="buy_cycle",
        query="구매주기가 긴 회원 중 최근 30일 이상 로그인한 회원",
        operator=">=",
        value=30,
        would_be_spans=((16, 19), (20, 22)),
        gap="metric_to_value",
    ),
)


@pytest.mark.parametrize(
    "case", NON_ADJACENT_CLAIMS, ids=[item.gap for item in NON_ADJACENT_CLAIMS]
)
def test_expressions_that_are_not_adjacent_are_not_joined_into_one_claim(
    case: _NonAdjacentClaim,
) -> None:
    """이어지지 않은 표현을 한 청구로 합치지 않는다 — 값·단위·기호가 다 맞아도 그렇다."""

    declaration = next(
        item for item in _DECLARATIONS if item["metric_id"] == case.metric_id
    )
    bindings = _bindings(case.query)
    spans = {(row["start"], row["end"]) for row in bindings}
    assert set(case.would_be_spans) <= spans, (
        "원장이 바뀌었다 — 이 회귀가 재려는 두 리터럴이 더 이상 추출되지 않는다."
    )
    assert case.query[case.would_be_spans[1][0] : case.would_be_spans[1][1]] == (
        _operator_surface(case.operator)
    )
    assert member_scalar_metric_claims._alias_candidates(case.query, _DECLARATIONS), (
        "표면어가 없으면 인접이 아니라 조건 4(표면어 등장)가 막는 것이라 이 회귀가 무의미하다."
    )

    expression = _expression(
        declaration, query=case.query, symbol=case.operator, value=case.value
    )
    claimed = _claim(case.query, expression, bindings)

    assert claimed == ()
    assert claimed != case.would_be_spans
    assert (
        event_parser.source_time_span_count(case.query, today=None, masked_spans=claimed) == 1
    ), "청구가 없으므로 진짜 창은 그대로 남아야 한다."


def test_a_second_metric_mention_far_from_the_operator_is_not_merged_into_one_claim() -> None:
    """인접하지 않은 표현을 한 청구로 합치지 않는다.

    같은 임계 표면어가 문장에 둘 있고 그중 비교어 **앞에 붙은** 것만 임계값이다. 뒤의 것을 함께
    청구하면 같은 문장의 진짜 창이 사라진다 — 검증기는 창의 개수만 세므로 그것을 잡지 못한다.
    """

    anchor = ANCHORED_CLAIMS[1]
    declaration = _anchor_declaration(anchor)
    bindings = _bindings(anchor.query)
    claimed = _claim(
        anchor.query,
        _expression(
            declaration, query=anchor.query, symbol=anchor.operator, value=anchor.value
        ),
        bindings,
    )

    later = [
        (row["start"], row["end"])
        for row in bindings
        if member_scalar_metric_claims._threshold_unit_and_value(row) is not None
        and row["start"] > anchor.char_spans[-1][1]
    ]
    assert later, "혼합문 뒤쪽에 같은 모양의 임계값 후보가 있어야 한다."
    assert all(span not in claimed for span in later)
    assert max(end for _start, end in claimed) < min(start for start, _end in later)


@pytest.mark.parametrize("anchor", ANCHORED_CLAIMS, ids=_ANCHOR_IDS)
def test_reversing_the_declaration_order_does_not_change_the_claim(
    anchor: _AnchoredClaim,
) -> None:
    """recipe 등록 순서를 뒤집어도 같은 좌표를 청구한다."""

    declaration = _anchor_declaration(anchor)
    expression = _expression(
        declaration, query=anchor.query, symbol=anchor.operator, value=anchor.value
    )
    bindings = _bindings(anchor.query)
    reversed_registry = {
        **_REGISTRY,
        "metrics": list(reversed(list(_REGISTRY["metrics"]))),
    }

    assert _claim(anchor.query, expression, bindings) == anchor.char_spans
    assert (
        _claim(anchor.query, expression, bindings, registry=reversed_registry)
        == anchor.char_spans
    )
    assert [
        item[0]["metric_id"]
        for item in member_scalar_metric_claims._alias_candidates(
            anchor.query, tuple(reversed(_DECLARATIONS))
        )
    ] == [anchor.metric_id]


# ── 6. 공용 인접 헬퍼: 두 소비자가 같은 답을 낸다 ──────────────────────────────────


def test_both_adjacency_consumers_read_the_same_shared_helper() -> None:
    """합성 문형 판정과 청구 판정이 같은 국소 인접 헬퍼를 쓴다.

    두 판정이 각자 인접 규칙을 들면 "합성은 되는데 청구는 안 된다"가 조용히 생긴다. 여기서는
    :func:`audience_frame.spans_are_locally_adjacent` 를 직접 호출한 결과가
    :func:`member_scalar_metric_claims._threshold_phrase_is_adjacent` 와 일치하는지 잰다.
    """

    anchor = ANCHORED_CLAIMS[0]
    alias_end = len(anchor.alias)
    threshold, operator = anchor.char_spans

    for value_bounds, operator_bounds in (
        (threshold, operator),
        (operator, threshold),
        (threshold, threshold),
        ((0, len(anchor.alias)), operator),
    ):
        local = member_scalar_metric_claims._threshold_phrase_is_adjacent(
            anchor.query,
            alias_end=alias_end,
            value_bounds=value_bounds,
            operator_bounds=operator_bounds,
        )
        shared = audience_frame.spans_are_locally_adjacent(
            anchor.query,
            ((alias_end, alias_end), value_bounds, operator_bounds),
            gaps=(
                member_scalar_metric_claims._METRIC_TO_VALUE_RE,
                member_scalar_metric_claims._VALUE_TO_OPERATOR_RE,
            ),
        )
        assert local == shared, (value_bounds, operator_bounds)

    assert member_scalar_metric_claims._threshold_phrase_is_adjacent(
        anchor.query, alias_end=alias_end, value_bounds=threshold, operator_bounds=operator
    )


def test_the_shared_helper_requires_the_declared_order_not_just_empty_gaps() -> None:
    """지표 → 임계값 → 비교는 **그 순서**여야 한다.

    순서 검사가 빠지면 뒤집힌 좌표의 '사이'가 빈 문자열이 되어 모든 gap 패턴을 통과한다. 그러면
    비교어 앞의 임계값과 뒤의 임계값이 구별되지 않아 같은 문장의 진짜 창까지 청구된다.
    """

    anchor = ANCHORED_CLAIMS[0]
    threshold, operator = anchor.char_spans

    # 뒤집힌 좌표: 사이 문자열은 전부 빈 문자열이지만 순서가 어긋난다.
    assert not member_scalar_metric_claims._threshold_phrase_is_adjacent(
        anchor.query,
        alias_end=threshold[1],
        value_bounds=operator,
        operator_bounds=threshold,
    )
    assert not audience_frame.spans_are_locally_adjacent(
        anchor.query,
        ((threshold[1], threshold[1]), operator, threshold),
        gaps=(
            member_scalar_metric_claims._METRIC_TO_VALUE_RE,
            member_scalar_metric_claims._VALUE_TO_OPERATOR_RE,
        ),
    )
    # gap 개수가 스팬 사이 개수와 다르면 남는 사이를 **검사하지 않은 채** 통과시키지 않는다.
    # 여기 첫 gap 은 통과하므로, 개수 검사가 빠지면 두 번째 사이가 조용히 무검사로 넘어간다.
    assert (
        member_scalar_metric_claims._METRIC_TO_VALUE_RE.fullmatch(
            anchor.query[len(anchor.alias) : threshold[0]]
        )
        is not None
    )
    assert not audience_frame.spans_are_locally_adjacent(
        anchor.query,
        ((0, len(anchor.alias)), threshold, operator),
        gaps=(member_scalar_metric_claims._METRIC_TO_VALUE_RE,),
    )


def test_the_whole_phrase_gate_keeps_its_sentence_wide_judgement() -> None:
    """``_whole_phrase_matches`` 는 국소 인접 **더하기** 문장 전역 판정이다(완화하지 않았다).

    혼합문은 국소 인접을 통과하지만 문장 전역 판정에서 닫힌다 — 이 차이가 사라지면
    ``구매주기가 30일 이하이고 여성인 회원`` 이 조용히 합성된다.
    """

    closed, mixed = ANCHORED_CLAIMS[0], ANCHORED_CLAIMS[1]
    for anchor, expected in ((closed, True), (mixed, False)):
        alias_end = len(anchor.alias)
        threshold, operator = anchor.char_spans
        assert member_scalar_metric_claims._threshold_phrase_is_adjacent(
            anchor.query,
            alias_end=alias_end,
            value_bounds=threshold,
            operator_bounds=operator,
        )
        assert (
            member_scalar_metric_claims._whole_phrase_matches(
                anchor.query,
                alias_start=0,
                alias_end=alias_end,
                value_bounds=threshold,
                operator_bounds=operator,
            )
            is expected
        )
