"""[Metric recipes] 안내가 지시하는 모양이 **실제로 컴파일되는가** — recipe capability 래칫.

`audience_runtime.audience_catalog_guidance()` 의 `[Metric recipes]` 절은 구조화 모델에게
"이 지표는 이 wire 로 쓰라"고 지시한다. 그 지시가 컴파일 불가능한 모양이면 모델이 안내를
정확히 따를수록 `compiler_operation_unsupported` 로 죽는다. 실제로 그랬다 — 회원당 0..1행을
읽는 지표 14종(member_scalar 9 + 기준월 스냅샷 field 3 + transition 2)이 bare FieldRef 를
지시받아 전부 컴파일에 실패했다(`docs/operations/buy_cycle_scalar_threshold_investigation.md` §3).

그래서 이 파일이 고정하는 것은 **안내와 컴파일러의 일치**다:

    안내에 실린 recipe  →  자리표시자를 선언값으로 채움  →  event_compiler capability = supported

측정 방법 자체도 함께 고정한다. 두 가지를 틀리면 래칫이 엉뚱한 것을 재기 때문이다:

  - **compile context 를 반드시 넘긴다.** 기본 컨텍스트로 재면 카탈로그 심볼이 통째로
    미등록으로 나와 라이브와 다른 사유 코드가 잡힌다(`test_capability_measured_without_...`).
  - **kind 마다 Condition 완성법이 다르다.** aggregate recipe 는 Condition 이 아니라 **Scalar**
    라서 그대로 `condition_from_dict` 에 넣으면 `IrSchemaError` 다 — 임계 비교로 감싸야 한다.
    Exists recipe 만 그 자체로 Condition 이다.

지표 목록은 손으로 적지 않고 `catalog_snapshot()["metrics"]` 에서 파생한다. 대신 선언이
줄어들어 초록이 되는 것을 막으려고 회원 행 지표 **하한 14**만 별도로 못 박는다.

**주의 — capability supported 는 의미 정합이 아니다.** 기준월 스냅샷 소스
(`member_month_snapshot`)에는 최신월 고정 술어가 없어 field/transition recipe 는 지금
"어느 달이든 한 번이라도"로 컴파일된다. 이 파일은 그 의미 결함을 재지 않는다(같은 조사
문서의 "벽 A 의 의미 함정"). 여기서 재는 것은 **모델이 안내대로 냈을 때 죽지 않는가** 하나다.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import member_scalar_metrics  # noqa: E402

# 회원당 0..1 행을 읽는 kind. 조사 문서 §3 의 14개가 정확히 이 셋이다.
MEMBER_ROW_KINDS = frozenset({member_scalar_metrics.MEMBER_SCALAR_KIND, "field", "transition"})
# 선언이 조용히 줄어들어도 파생 파라미터는 초록이 된다. 문서가 완료 조건으로 못 박은 수를 하한으로 둔다.
MEMBER_ROW_METRIC_FLOOR = 14

# 자리표시자를 채우는 결정론 값. capability 는 값의 의미를 보지 않으므로 무엇이든 되지만
# 매 실행 같은 값이어야 한다.
SAMPLE_THRESHOLD = 30
SAMPLE_EVIDENCE_TEXT = "지표 조건"
# aggregate recipe(Scalar)를 판정 가능한 Condition 으로 감쌀 때 쓰는 비교. 지표 선언과 무관하다.
AGGREGATE_PROBE_OPERATOR = ">="

_PLACEHOLDERS: tuple[str, ...] = (
    audience_runtime.METRIC_RECIPE_EVIDENCE_TEXT,
    audience_runtime.METRIC_RECIPE_OPERATOR_PLACEHOLDER,
    audience_runtime.METRIC_RECIPE_THRESHOLD_PLACEHOLDER,
    audience_runtime.METRIC_RECIPE_VALUE_PLACEHOLDER,
    audience_runtime.METRIC_RECIPE_PREVIOUS_VALUE_PLACEHOLDER,
)

# 카탈로그 로딩은 비싸다. 파라미터도 여기서 파생되므로 모듈에서 한 번만 읽는다
# (`load_audience_catalog_config` / `resolve_audience_catalog` 는 이미 lru_cache 다).
_SNAPSHOT: Mapping[str, Any] = audience_runtime.catalog_snapshot()
_METRICS: Mapping[str, Any] = _SNAPSHOT.get("metrics") or {}
_FIELDS: Mapping[str, Any] = _SNAPSHOT.get("fields") or {}
_VALUE_DOMAINS: Mapping[str, Any] = _SNAPSHOT.get("value_domains") or {}
_CATALOG = audience_runtime.resolve_audience_catalog()
_GUIDANCE = audience_runtime.audience_catalog_guidance()
_GUIDANCE_LINES: tuple[str, ...] = tuple(_GUIDANCE.splitlines())


def _declaration(metric_id: str) -> Mapping[str, Any]:
    declaration = _METRICS.get(metric_id)
    if not isinstance(declaration, Mapping):
        raise AssertionError(f"지표 선언이 Mapping 이 아니다: {metric_id}")
    return declaration


def _kind(declaration: Mapping[str, Any]) -> str:
    return str(declaration.get("kind") or declaration.get("semantic_type") or "")


METRIC_IDS: tuple[str, ...] = tuple(
    metric_id
    for metric_id in sorted(_METRICS)
    if isinstance(_METRICS.get(metric_id), Mapping)
)
MEMBER_ROW_METRIC_IDS: tuple[str, ...] = tuple(
    metric_id
    for metric_id in METRIC_IDS
    if _kind(_declaration(metric_id)) in MEMBER_ROW_KINDS
)
SNAPSHOT_VALUE_METRIC_IDS: tuple[str, ...] = tuple(
    metric_id
    for metric_id in METRIC_IDS
    if _kind(_declaration(metric_id)) in {"field", "transition"}
)
TRANSITION_METRIC_IDS: tuple[str, ...] = tuple(
    metric_id
    for metric_id in METRIC_IDS
    if _kind(_declaration(metric_id)) == "transition"
)


def _compile_context() -> event_compiler.CompileContext:
    """라이브가 쓰는 것과 같은 컨텍스트. 매 호출 새로 만들어 테스트 간 상태를 공유하지 않는다."""
    return _CATALOG.compile_context(literals=True, today=None)


def _canonical_values(field_id: str) -> tuple[str, ...]:
    """필드가 선언한 값 도메인의 canonical 값(정렬). 물리코드는 여기서 나오지 않는다."""
    declaration = _FIELDS.get(field_id)
    domain_id = declaration.get("value_domain") if isinstance(declaration, Mapping) else None
    if not domain_id:
        return ()
    domain = _VALUE_DOMAINS.get(str(domain_id))
    values = domain.get("values") if isinstance(domain, Mapping) else None
    if not isinstance(values, Mapping):
        return ()
    return tuple(sorted(str(canonical) for canonical in values))


def _physical_value(field_id: str, canonical: str) -> str | None:
    declaration = _FIELDS.get(field_id)
    domain_id = declaration.get("value_domain") if isinstance(declaration, Mapping) else None
    domain = _VALUE_DOMAINS.get(str(domain_id or ""))
    values = domain.get("values") if isinstance(domain, Mapping) else None
    entry = values.get(canonical) if isinstance(values, Mapping) else None
    physical = entry.get("physical") if isinstance(entry, Mapping) else None
    return str(physical) if physical else None


def _fill_operator(operator: Any, declaration: Mapping[str, Any]) -> Any:
    if operator != audience_runtime.METRIC_RECIPE_OPERATOR_PLACEHOLDER:
        return operator
    allowed = declaration.get("allowed_operators")
    if isinstance(allowed, list) and allowed:
        return str(allowed[0])
    # 채우지 못한 자리표시자는 조용히 다른 값으로 바꾸지 않는다 — 그대로 남겨 테스트가 잡게 한다.
    return operator


def _fill_literal(right: Any, left: Any) -> Any:
    if not isinstance(right, Mapping) or right.get("type") != "literal":
        return right
    value = right.get("value")
    if value == audience_runtime.METRIC_RECIPE_THRESHOLD_PLACEHOLDER:
        return {**right, "value": SAMPLE_THRESHOLD}
    if value not in (
        audience_runtime.METRIC_RECIPE_VALUE_PLACEHOLDER,
        audience_runtime.METRIC_RECIPE_PREVIOUS_VALUE_PLACEHOLDER,
    ):
        return right
    field_id = str(left.get("name") or "") if isinstance(left, Mapping) else ""
    canonicals = _canonical_values(field_id)
    if not canonicals:
        return right
    # 도착값과 출발값이 같으면 전이 recipe 가 자기 자신으로의 전이가 된다. 값이 둘 이상이면 갈라 쓴다.
    if value == audience_runtime.METRIC_RECIPE_VALUE_PLACEHOLDER:
        return {**right, "value": canonicals[0]}
    return {**right, "value": canonicals[-1]}


def _fill_placeholders(node: Any, declaration: Mapping[str, Any]) -> Any:
    """recipe 의 자리표시자를 **선언 데이터**로 채운다(모델이 하는 일과 같은 일)."""
    if isinstance(node, Mapping):
        filled: dict[str, Any] = {
            key: _fill_placeholders(value, declaration) for key, value in node.items()
        }
        if filled.get("type") == "comparison":
            filled["operator"] = _fill_operator(filled.get("operator"), declaration)
            filled["right"] = _fill_literal(filled.get("right"), filled.get("left"))
        evidence = filled.get("evidence")
        if (
            isinstance(evidence, Mapping)
            and evidence.get("text") == audience_runtime.METRIC_RECIPE_EVIDENCE_TEXT
        ):
            filled["evidence"] = {**evidence, "text": SAMPLE_EVIDENCE_TEXT}
        return filled
    if isinstance(node, list):
        return [_fill_placeholders(item, declaration) for item in node]
    return node


def _remaining_placeholders(wire: Any) -> tuple[str, ...]:
    """채워지지 않은 자리표시자. 남은 채로 재면 픽스처 결함이 recipe 결함으로 잘못 잡힌다."""
    rendered = json.dumps(wire, ensure_ascii=False)
    return tuple(token for token in _PLACEHOLDERS if token in rendered)


def _filled_recipe(metric_id: str) -> dict[str, Any]:
    declaration = _declaration(metric_id)
    wire = audience_runtime.metric_recipe_wire(declaration)
    assert wire is not None, (
        f"{metric_id} 의 recipe 가 없다 — 안내가 이 지표를 '쓰지 말라'로 내보내고 있다. "
        "선언에 그 kind 가 요구하는 필드가 빠졌는지 확인하라."
    )
    filled = _fill_placeholders(wire, declaration)
    assert isinstance(filled, dict)
    return filled


def _as_condition(filled: Mapping[str, Any]) -> event_ir.Condition:
    """recipe wire 를 판정 가능한 Condition 으로 만든다.

    kind 마다 완성법이 다르다. aggregate recipe(그리고 옛 bare field recipe)는 Condition 이 아니라
    **Scalar** 라서 그대로 `condition_from_dict` 에 넣으면 `IrSchemaError` 다 — 임계 비교로 감싸야
    capability 를 잴 수 있다. Exists recipe 만 그 자체로 Condition 이다.

    Scalar 를 감싸 재는 이유는 관대함이 아니라 **진단 가능성**이다: 감싸지 않으면 recipe 회귀가
    'unsupported' 가 아니라 스키마 예외로 터져 무엇이 잘못됐는지 읽히지 않는다.
    """
    try:
        return event_ir.condition_from_dict(dict(filled))
    except event_ir.IrSchemaError:
        return event_ir.condition_from_dict(
            {
                "type": "comparison",
                "operator": AGGREGATE_PROBE_OPERATOR,
                "left": dict(filled),
                "right": {"type": "literal", "value": SAMPLE_THRESHOLD},
                "evidence": {"text": SAMPLE_EVIDENCE_TEXT, "start": 0, "end": 1},
            }
        )


def _capability(condition: event_ir.Condition) -> event_compiler.CompilerCapabilityResult:
    return event_compiler.validate_compiler_capability(condition, context=_compile_context())


def _issue_codes(result: event_compiler.CompilerCapabilityResult) -> tuple[str, ...]:
    return tuple(sorted({issue.code for issue in result.issues}))


def _guidance_recipe_index(metric_id: str) -> int:
    prefix = f"- {metric_id} ("
    for index, line in enumerate(_GUIDANCE_LINES):
        if line.startswith(prefix):
            return index
    raise AssertionError(
        f"안내의 [Metric recipes] 에 {metric_id} 줄이 없다 — 지표가 모델에게 보이지 않는다."
    )


def _mentions_symbol(line: str, symbol: str) -> bool:
    """식별자를 **토큰 단위**로 찾는다.

    부분문자열로 세면 `member_scalar_buy_cycle` recipe 줄이 `buy_cycle` 안내로도 계산돼
    선택 규칙이 통째로 사라져도 초록이 된다(실제로 그렇게 한 번 속았다).
    """
    return re.search(rf"(?<![0-9A-Za-z_]){re.escape(symbol)}(?![0-9A-Za-z_])", line) is not None


def _labels_owned_by_multiple_kinds() -> dict[str, dict[str, str]]:
    """같은 표면어(label)를 서로 다른 kind 가 나눠 가진 쌍. 카탈로그에서 파생한다."""
    by_label: dict[str, dict[str, str]] = {}
    for metric_id in METRIC_IDS:
        declaration = _declaration(metric_id)
        kind = _kind(declaration)
        if not kind:
            continue
        label = str(declaration.get("label") or metric_id)
        by_label.setdefault(label, {})[kind] = metric_id
    return {label: owners for label, owners in by_label.items() if len(owners) > 1}


def test_the_ratchet_measures_every_metric_the_catalog_declares() -> None:
    """파라미터가 파생이라 조용히 줄어들 수 있다. 재는 대상 자체를 먼저 고정한다."""
    assert METRIC_IDS, "카탈로그에 지표 선언이 하나도 없다."
    assert set(METRIC_IDS) == set(_METRICS), "일부 지표 선언이 Mapping 이 아니라 래칫에서 빠졌다."
    assert len(MEMBER_ROW_METRIC_IDS) >= MEMBER_ROW_METRIC_FLOOR, (
        f"회원 행 지표가 {len(MEMBER_ROW_METRIC_IDS)}개뿐이다 — 조사 문서가 완료 조건으로 못 박은 "
        f"{MEMBER_ROW_METRIC_FLOOR}개보다 적다. 선언이 사라지면 이 래칫도 함께 사라진다."
    )
    declared_scalars = set(member_scalar_metrics.member_scalar_metric_ids(_CATALOG))
    assert declared_scalars <= set(MEMBER_ROW_METRIC_IDS), (
        "해석된 카탈로그가 member_scalar 로 아는 지표 중 일부가 스냅샷 파라미터에 없다: "
        f"{sorted(declared_scalars - set(MEMBER_ROW_METRIC_IDS))}"
    )


@pytest.mark.parametrize("metric_id", METRIC_IDS)
def test_every_metric_recipe_compiles_with_the_catalog_context(metric_id: str) -> None:
    """안내가 지시하는 모든 recipe 는 컴파일러가 지원해야 한다.

    지원하지 않으면 모델이 안내를 **정확히 따를수록** 실패한다 — 라이브에서 실제로 그랬다.

    선언이 불완전해 컴파일 가능한 모양이 아예 없는 지표는 capability 를 주장하지 않는다. 대신
    안내가 그 사실을 말하는지만 본다 — 줄을 조용히 지우면 모델은 그 지표를 계속 지어낸다(규칙 11).
    """
    if audience_runtime.metric_recipe_wire(_declaration(metric_id)) is None:
        assert (
            audience_runtime.METRIC_RECIPE_UNAVAILABLE
            in _GUIDANCE_LINES[_guidance_recipe_index(metric_id)]
        ), f"{metric_id} 는 recipe 가 없는데 안내가 그 사실을 말하지 않는다."
        return
    filled = _filled_recipe(metric_id)
    unfilled = _remaining_placeholders(filled)
    assert not unfilled, (
        f"{metric_id} recipe 의 자리표시자를 선언값으로 채우지 못했다: {list(unfilled)} — "
        "모델도 같은 이유로 채울 수 없다(선언에 allowed_operators/value_domain 이 없다)."
    )
    result = _capability(_as_condition(filled))
    assert result.status == event_compiler.CAPABILITY_SUPPORTED, (
        f"{metric_id} 의 안내 recipe 가 {result.status} 다: {list(_issue_codes(result))}. "
        "안내 문자열만 고치고 컴파일 검증을 생략하면 이 결함이 그대로 남는다."
    )


@pytest.mark.parametrize("metric_id", METRIC_IDS)
def test_the_guidance_line_is_exactly_the_measured_recipe(metric_id: str) -> None:
    """재는 것과 모델이 보는 것이 같은 문자열인가.

    이 두 개가 갈리면 '안내는 고쳤는데 컴파일은 여전히 불가' 또는 그 반대가 조용히 남는다.
    """
    declaration = _declaration(metric_id)
    wire = audience_runtime.metric_recipe_wire(declaration)
    label = str(declaration.get("label") or metric_id)
    if wire is None:
        expected = f"- {metric_id} ({label}): {audience_runtime.METRIC_RECIPE_UNAVAILABLE}"
    else:
        expected = f"- {metric_id} ({label}): " + json.dumps(
            wire, ensure_ascii=False, separators=(",", ":")
        )
    assert _GUIDANCE_LINES[_guidance_recipe_index(metric_id)] == expected


@pytest.mark.parametrize("metric_id", MEMBER_ROW_METRIC_IDS)
def test_member_row_metric_recipes_are_exists_relations_not_bare_fields(metric_id: str) -> None:
    """회원 행 지표는 스칼라 하나가 아니라 관계다 — 안내 골격이 Exists(Filter(Source, ...)) 인가."""
    filled = _filled_recipe(metric_id)
    assert filled.get("type") == "exists", (
        f"{metric_id} recipe 가 {filled.get('type')!r} 다. 회원 행 지표를 bare FieldRef 나 "
        "Comparison 단독으로 안내하면 참조할 관계가 스코프에 없어 컴파일되지 않는다."
    )
    relation = filled.get("relation")
    assert isinstance(relation, Mapping) and relation.get("type") == "filter"
    inner = relation.get("relation")
    assert isinstance(inner, Mapping) and inner.get("type") == "source"
    assert inner.get("name") == _declaration(metric_id).get("source")


@pytest.mark.parametrize("metric_id", MEMBER_ROW_METRIC_IDS)
def test_a_bare_field_recipe_stays_unsupported_for_member_row_metrics(metric_id: str) -> None:
    """왜 Exists 가 필요한가를 테스트에 남긴다 — 옛 recipe 모양은 지금도 컴파일 불가다.

    이 래칫이 없으면 누군가 "bare field 도 되던데"로 안내를 되돌려도 아무것도 빨개지지 않는다.
    """
    declaration = _declaration(metric_id)
    field_id = str(declaration.get("expression_field") or "")
    assert field_id, f"{metric_id} 에 expression_field 선언이 없다."
    canonicals = _canonical_values(field_id)
    literal: Any = SAMPLE_THRESHOLD if not canonicals else canonicals[0]
    bare = event_ir.condition_from_dict(
        {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": field_id},
            "right": {"type": "literal", "value": literal},
            "evidence": {"text": SAMPLE_EVIDENCE_TEXT, "start": 0, "end": 1},
        }
    )
    result = _capability(bare)
    assert result.status == event_compiler.CAPABILITY_UNSUPPORTED, (
        f"{metric_id} 의 bare FieldRef 비교가 {result.status} 가 됐다. 컴파일러가 이 모양을 "
        "지원하게 됐다면 안내도 함께 정하고 이 테스트의 의도를 다시 쓰라(규칙 47)."
    )
    assert "compiler_operation_unsupported" in _issue_codes(result)


@pytest.mark.parametrize("metric_id", SNAPSHOT_VALUE_METRIC_IDS)
def test_snapshot_value_recipes_reject_physical_codes(metric_id: str) -> None:
    """스냅샷 값 지표의 Literal 은 canonical 값이다 — 물리코드는 컴파일되지 않는다.

    안내가 그렇게 말하고 있으므로(`[Fixed wire shapes]`) 컴파일러도 같은 말을 해야 한다.
    같은 이유로 이 파일의 다른 테스트들은 반드시 canonical 값으로 재야 한다 — 물리코드를 넣으면
    똑같은 `compiler_operation_unsupported` 로 접혀 recipe 결함과 픽스처 결함이 구분되지 않는다.
    """
    declaration = _declaration(metric_id)
    field_id = str(declaration.get("expression_field") or "")
    canonicals = _canonical_values(field_id)
    assert canonicals, f"{metric_id} 의 {field_id} 에 canonical 값 도메인 선언이 없다."
    physical = _physical_value(field_id, canonicals[0])
    assert physical is not None and physical not in canonicals, (
        f"{field_id} 의 값 {canonicals[0]!r} 이 물리코드와 구분되지 않는다 — canonical 사전이 "
        "물리 스키마와 분리돼 있지 않으면 DB 가 바뀔 때 IR 이 함께 깨진다."
    )
    filled = _filled_recipe(metric_id)
    swapped = json.loads(
        json.dumps(filled, ensure_ascii=False).replace(
            json.dumps(canonicals[0], ensure_ascii=False),
            json.dumps(physical, ensure_ascii=False),
        )
    )
    result = _capability(_as_condition(swapped))
    assert result.status == event_compiler.CAPABILITY_UNSUPPORTED, (
        f"{metric_id} 가 물리코드 {physical!r} 를 그대로 받아들였다 — 값 사전을 우회하는 "
        "리터럴이 컴파일되면 DB 스왑 때 조용히 0명이 된다."
    )


@pytest.mark.parametrize("metric_id", TRANSITION_METRIC_IDS)
def test_transition_recipes_compare_both_endpoints(metric_id: str) -> None:
    """전이는 도착값과 출발값 둘 다 있어야 한다 — 한 시점 비교로 낮아지면 다른 뜻이다."""
    declaration = _declaration(metric_id)
    arrival = str(declaration.get("expression_field") or "")
    departure = str(declaration.get("prev_expression_field") or "")
    assert arrival and departure, f"{metric_id} 에 전이 양끝 필드 선언이 없다."
    compared = {
        node.left.name
        for node in event_ir.walk(_as_condition(_filled_recipe(metric_id)))
        if isinstance(node, event_ir.Comparison) and isinstance(node.left, event_ir.FieldRef)
    }
    assert {arrival, departure} <= compared, (
        f"{metric_id} recipe 가 비교하는 필드는 {sorted(compared)} 뿐이다. 출발값이 빠지면 "
        "전이 요청이 조용히 현재 시점 비교 하나로 낮아진다."
    )


def test_capability_measured_without_the_catalog_context_reports_other_reasons() -> None:
    """측정 방법을 고정한다 — context 없이 재면 라이브와 다른 사유가 나온다.

    기본 컨텍스트에는 카탈로그 심볼이 없어 모든 recipe 가 '미등록'으로 나오고, 그러면 이 래칫이
    recipe 모양이 아니라 레지스트리 유무를 재게 된다.
    """
    metric_id = MEMBER_ROW_METRIC_IDS[0]
    condition = _as_condition(_filled_recipe(metric_id))
    with_context = _capability(condition)
    without_context = event_compiler.validate_compiler_capability(condition)
    assert with_context.status == event_compiler.CAPABILITY_SUPPORTED
    assert without_context.status == event_compiler.CAPABILITY_UNSUPPORTED
    assert "compiler_event_unregistered" in _issue_codes(without_context)


@pytest.mark.parametrize("metric_id", MEMBER_ROW_METRIC_IDS)
def test_placeholder_recipes_tell_the_model_how_to_fill_them(metric_id: str) -> None:
    """자리표시자를 남겼으면 채우는 데 필요한 선언값도 같은 자리에 있어야 한다.

    허용 연산자·단위·값 도메인이 안내에 없으면 모델은 `<operator>` 를 채울 근거가 없다.
    """
    declaration = _declaration(metric_id)
    index = _guidance_recipe_index(metric_id)
    assert index + 1 < len(_GUIDANCE_LINES), f"{metric_id} recipe 뒤에 세부 줄이 없다."
    detail = _GUIDANCE_LINES[index + 1]
    assert detail.startswith("  "), (
        f"{metric_id} recipe 다음 줄이 세부 줄이 아니다: {detail!r}"
    )
    for operator in declaration.get("allowed_operators") or []:
        assert str(operator) in detail, (
            f"{metric_id} 의 허용 연산자 {operator!r} 가 안내 세부 줄에 없다."
        )
    if _kind(declaration) == member_scalar_metrics.MEMBER_SCALAR_KIND:
        unit = str(declaration.get("unit") or "")
        assert unit, f"{metric_id} 에 unit 선언이 없다 — 임계값의 단위를 알릴 방법이 없다."
        assert unit in detail
    else:
        field_declaration = _FIELDS.get(str(declaration.get("expression_field") or ""))
        domain = (
            field_declaration.get("value_domain")
            if isinstance(field_declaration, Mapping)
            else None
        )
        assert domain, f"{metric_id} 의 값 필드에 value_domain 선언이 없다."
        assert str(domain) in detail


def test_an_incomplete_declaration_produces_no_recipe_instead_of_a_wrong_one() -> None:
    """증명하지 못하면 아무 모양이나 채우지 않는다(규칙 11).

    전이 지표에 출발 필드 선언이 없으면 현재 시점 비교 하나로 낮추는 것이 아니라 recipe 자체가
    없어야 한다 — 낮추면 전이 요청이 조용히 다른 뜻으로 나간다.
    """
    assert audience_runtime.metric_recipe_wire({}) is None
    assert (
        audience_runtime.metric_recipe_wire(
            {"kind": "transition", "source": "member_month_snapshot", "expression_field": "x.y"}
        )
        is None
    )
    assert (
        audience_runtime.metric_recipe_wire(
            {"kind": "field", "source": "", "expression_field": "x.y"}
        )
        is None
    )
    assert (
        audience_runtime.metric_recipe_wire(
            {"kind": "존재하지 않는 kind", "source": "s", "expression_field": "s.f"}
        )
        is None
    )


def test_labels_owned_by_two_kinds_carry_a_selection_rule() -> None:
    """같은 표면어에 kind 가 다른 지표가 둘 걸려 있으면 어느 쪽을 언제 쓰는지 안내가 있어야 한다.

    둘 다 컴파일되지만 모집단과 NULL 정책이 다른 별개의 SQL 이라 어느 쪽도 지울 수 없다.
    지우는 대신 고르는 법을 준다. 쌍 목록은 카탈로그에서 파생하므로 새 쌍이 생기면 여기서 잡힌다.
    """
    competing = _labels_owned_by_multiple_kinds()
    assert competing, (
        "같은 label 을 나눠 가진 지표 쌍이 하나도 없다 — 카탈로그가 바뀌었다면 이 테스트의 "
        "전제를 다시 확인하라."
    )
    for label, owners in sorted(competing.items()):
        matching = [
            line
            for line in _GUIDANCE_LINES
            if label in line
            and all(_mentions_symbol(line, metric_id) for metric_id in owners.values())
        ]
        assert matching, (
            f"표면어 {label!r} 를 {sorted(owners.items())} 가 나눠 갖는데 안내에 선택 규칙이 없다 — "
            "모델이 두 recipe 사이를 계속 오간다."
        )


def test_the_guidance_is_byte_identical_across_calls() -> None:
    """같은 카탈로그에서는 같은 프롬프트가 나와야 한다(캐시·정렬 회귀 차단)."""
    assert audience_runtime.audience_catalog_guidance() == _GUIDANCE
