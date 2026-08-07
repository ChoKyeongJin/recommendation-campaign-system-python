"""구조화 모델에게 **소스별 시각 필드**가 보이는가 — 광고되지 않은 심볼은 쓰이지 않는다.

``event_compiler.resolve_fields`` 는 사건마다 ``<source>.occurred_at`` 을 자동으로 파생한다
(원본은 소스 선언의 ``time_column``/``time_format``). 그런데 카탈로그 안내의 ``[Fields]`` 는
JSON 의 ``fields`` 선언만 렌더했기 때문에, **기간 창을 걸 수 있는 유일한 필드가 프롬프트에 한
번도 등장하지 않았다**. 그 상태에서 '최근 30일 구매' 요청을 받은 모델은 목록에 실제로 보이는
시각처럼 생긴 필드(``purchase.order_time``, 선언 data_type 은 ``string``)를 TimeFilter 에
걸었고, 컴파일러가 "시간 컬럼이 아닌 타입에 기간 조건을 걸 수 없습니다" 로 죽였다. 재구조화
예산이 그 왕복에 소모되면서 정답을 뽑을 수 있는 요청이 되묻기로 닫혔다(실측 2026-08-07).

그래서 이 파일이 고정하는 것은 다섯 가지다.

1. ``time_column`` 을 선언하고 그 컬럼이 **고정돼 있지 않은** 모든 소스의 시각 필드가 안내에
   있다(특정 소스 하드코딩 금지).
2. grain 표기가 저장 포맷에서 파생돼 일 단위와 월 단위가 서로 다르게 보인다 —
   월 칸 컬럼에 '최근 N일' 롤링 창을 거는 실수를 안내가 미리 막는다.
3. 광고된 심볼이 **실제로 컴파일되는 심볼**이다. 존재하지 않는 이름을 안내하면 결함의 방향만
   바뀔 뿐(모델이 다른 이유로 죽는다) 요청은 똑같이 닫힌다.
4. 선언이 자기 시각 컬럼을 한 값으로 고정한 소스(월 스냅샷)는 심볼을 팔지 않고 이유를
   말한다. 그 컬럼에 기간 창을 더 걸면 컴파일은 성공하고 술어만 자기모순이 되어 예외도
   경고도 없이 0명이 된다 — fail-close 가 아니라 조용한 왜곡이다.
5. 이 절이 실제로 구조화 프롬프트에 실린다. 빌더만 검사하면 배선이 끊겨도 전부 녹색이고,
   그때 되살아나는 것은 이 파일이 막으려던 바로 그 실패다.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

import audience_runtime
import event_compiler
import event_ir


def _catalog_sources() -> dict[str, Mapping[str, Any]]:
    return {
        source_id: declaration
        for source_id, declaration in (audience_runtime.catalog_snapshot().get("sources") or {}).items()
        if isinstance(declaration, Mapping)
    }


def _time_declaring_sources() -> dict[str, Mapping[str, Any]]:
    return {
        source_id: declaration
        for source_id, declaration in _catalog_sources().items()
        if str(declaration.get("time_column") or "")
    }


def _window_capable_sources() -> dict[str, Mapping[str, Any]]:
    """시각 컬럼을 선언했고 그 컬럼이 **고정돼 있지 않은** 소스.

    선언이 자기 시각 컬럼을 한 값으로 고정한 소스(월 스냅샷)는 기간 창을 받을 수 없다 —
    분리 기준은 이 파일이 아니라 선언이다(:func:`audience_runtime._pins_its_own_time_column`).
    """
    return {
        source_id: declaration
        for source_id, declaration in _time_declaring_sources().items()
        if not audience_runtime._pins_its_own_time_column(
            declaration, str(declaration.get("time_column") or "")
        )
    }


def _time_field_block() -> tuple[str, ...]:
    """안내에서 ``[Event time fields]`` 절만 잘라낸다(다음 대괄호 소제목 직전까지)."""
    lines = audience_runtime.audience_catalog_guidance().splitlines()
    start = lines.index(audience_runtime.EVENT_TIME_FIELD_HEADING)
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("["):
            break
        block.append(line)
    return tuple(block)


def _advertised_time_fields() -> tuple[str, ...]:
    suffix = f".{event_ir.TIME_FIELD_SUFFIX}"
    return tuple(
        line.removeprefix("- ").split(":", 1)[0]
        for line in _time_field_block()
        if line.startswith("- ") and line.split(":", 1)[0].endswith(suffix)
    )


@pytest.mark.parametrize("source_id", sorted(_window_capable_sources()))
def test_every_window_capable_source_advertises_its_occurred_at_field(source_id: str) -> None:
    """카탈로그를 순회해 확인한다 — 소스를 하나 더 선언하면 안내도 함께 자라야 한다."""
    guidance = audience_runtime.audience_catalog_guidance()
    field_id = f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}"
    assert f"- {field_id}:" in guidance, (
        f"{source_id} 는 time_column 을 선언했는데 {field_id} 가 안내에 없다 — "
        "모델은 광고되지 않은 심볼을 쓸 수 없어 문자열 시각 컬럼으로 우회한다."
    )


def test_every_time_declaring_source_appears_in_the_section() -> None:
    """창을 걸 수 있든 없든 **빠지지는 않는다** — 조용히 사라지면 모델이 우회한다(규칙 11)."""
    block = "\n".join(_time_field_block())
    for source_id in sorted(_time_declaring_sources()):
        assert f"{source_id}" in block, f"{source_id} 가 안내에서 통째로 빠졌다."


def test_advertised_time_fields_carry_the_source_label() -> None:
    """라벨은 손으로 적지 않고 소스 라벨에서 파생한다(라벨을 고치면 안내도 따라 바뀐다)."""
    block = "\n".join(_time_field_block())
    for source_id, declaration in sorted(_window_capable_sources().items()):
        label = str(declaration.get("label") or source_id)
        assert f"- {source_id}.{event_ir.TIME_FIELD_SUFFIX}: {label} " in block


def test_day_and_month_grain_sources_render_different_grain_marks() -> None:
    """grain 표기는 ``TIME_GRAINS`` 에서 파생되고 단위가 다르면 표기도 달라야 한다.

    월 칸 컬럼(char6)에 '최근 N일' 을 걸면 컴파일러가 fail-close 하는데, 그 사실을 안내가
    말해주지 않으면 모델은 재시도 예산을 그 벽에 쓴다.
    """
    by_unit: dict[str, list[str]] = {}
    for source_id, declaration in sorted(_window_capable_sources().items()):
        grain = event_compiler.TIME_GRAINS.get(str(declaration.get("time_format") or ""))
        if grain is None:
            continue
        by_unit.setdefault(grain.unit, []).append(source_id)

    assert {"day", "month"} <= set(by_unit), (
        "카탈로그에 일 단위와 월 단위 소스가 모두 있어야 이 대조가 성립한다: " f"{sorted(by_unit)}"
    )

    block = {
        line.removeprefix("- ").split(":", 1)[0]: line
        for line in _time_field_block()
        if line.startswith("- ")
    }
    for unit, source_ids in sorted(by_unit.items()):
        for source_id in source_ids:
            line = block[f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}"]
            assert f"grain={unit}" in line
            for other in sorted(set(by_unit) - {unit}):
                assert f"grain={other}" not in line

    # 롤링 창 가능 여부도 같은 선언(rolling_cutoff)에서 파생돼야 한다 — 두 번째 표 금지.
    for source_id, declaration in sorted(_window_capable_sources().items()):
        grain = event_compiler.TIME_GRAINS.get(str(declaration.get("time_format") or ""))
        if grain is None:
            continue
        line = block[f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}"]
        assert ("롤링 창 불가" in line) is (grain.rolling_cutoff is None), (
            f"{source_id} 의 롤링 창 안내가 TIME_GRAINS 선언과 어긋난다: {line}"
        )


def test_advertised_time_fields_are_compilable_symbols() -> None:
    """안내가 파는 심볼이 컴파일러가 아는 심볼인가 — 존재하지 않는 이름을 광고하지 않는다."""
    resolved = audience_runtime.resolve_audience_catalog()
    fields = event_compiler.resolve_fields(dict(resolved.compiler_events))

    advertised = _advertised_time_fields()
    assert advertised, "시각 필드가 하나도 광고되지 않았다."
    for field_id in advertised:
        spec = fields.get(field_id)
        assert spec is not None, f"안내에만 있고 컴파일러에는 없는 심볼: {field_id}"
        # 시각 필드로 광고했으면 실제로 기간 창을 걸 수 있는 타입이어야 한다.
        assert spec.data_type in event_compiler.DATE_DATA_TYPES, (
            f"{field_id} 의 data_type 이 시간 타입이 아니다: {spec.data_type!r}"
        )


def test_declared_non_time_fields_are_not_offered_as_time_fields() -> None:
    """``purchase.order_time`` 처럼 문자열로 적재된 시각 컬럼이 이 절에 섞이면 안 된다.

    필드 id 를 코드에 적지 않고 선언을 순회한다 — 새로 추가되는 문자열 시각 컬럼에도 같은
    규칙이 성립해야 한다.
    """
    block = "\n".join(_time_field_block())
    declared_fields = audience_runtime.catalog_snapshot().get("fields") or {}
    for field_id, declaration in sorted(declared_fields.items()):
        if not isinstance(declaration, Mapping):
            continue
        assert f"- {field_id}:" not in block, (
            f"선언 필드 {field_id} 가 시각 필드 절에 실렸다 — "
            "TimeFilter 를 걸 수 있는 필드는 파생된 occurred_at 뿐이다."
        )


def test_wire_shapes_bind_time_filter_to_the_derived_time_field() -> None:
    """[Fixed wire shapes] 가 TimeFilter.field 의 유일한 정답을 말한다."""
    guidance = audience_runtime.audience_catalog_guidance()
    rules = [line for line in guidance.splitlines() if "TimeFilter.field" in line]
    assert len(rules) == 1, f"TimeFilter.field 규칙이 {len(rules)}줄이다(단일 소유 위반)."
    rule = rules[0]
    assert f"<source>.{event_ir.TIME_FIELD_SUFFIX}" in rule
    assert audience_runtime.EVENT_TIME_FIELD_HEADING in rule
    # 기존 '기간 집계' 줄과 같은 어휘를 쓰는지 — 두 줄이 다른 말을 하면 모델이 고르게 된다.
    assert any(
        "기간 집계" in line and f"TimeFilter(<source>.{event_ir.TIME_FIELD_SUFFIX}" in line
        for line in guidance.splitlines()
    )


def test_unregistered_time_format_is_reported_not_silently_dropped() -> None:
    """미등록 저장 포맷의 소스는 조용히 사라지지 않고 '기간 창 불가'로 드러난다(규칙 11).

    조용히 빼면 모델은 그 소스에 기간 창을 걸 수 있다고 믿고 시도하며, 실패 이유는 프롬프트
    어디에도 없다. 심볼을 광고하지도 않는다 — 컴파일되지 않는 이름을 파는 셈이기 때문이다.
    """
    probe_format = "__unregistered_time_format__"
    assert probe_format not in event_compiler.TIME_GRAINS

    lines = audience_runtime._source_time_field_lines(
        {
            "__probe_event__": {
                "label": "probe",
                "time_column": "OCCURRED_ON",
                "time_format": probe_format,
            }
        }
    )
    rendered = "\n".join(lines)
    assert "__probe_event__" in rendered
    assert probe_format in rendered
    assert f"__probe_event__.{event_ir.TIME_FIELD_SUFFIX}" not in rendered


def test_sources_without_a_time_column_advertise_nothing() -> None:
    """시각 컬럼 선언이 없으면 파생될 필드도 없다 — 없는 심볼을 만들어 팔지 않는다."""
    lines = audience_runtime._source_time_field_lines(
        {"__probe_event__": {"label": "probe"}}
    )
    assert lines == []


# ── 선언이 자기 시각 컬럼을 고정한 소스 ───────────────────────────────────────────
# ``member_scalar_*`` / ``member_metric_*`` 는 월 스냅샷 테이블을 최신 달 한 칸에 고정한다
# (``extra_predicates`` 에 ``{alias}.YYYYMM = (SELECT MAX(YYYYMM) …)``). 그 컬럼에 기간 창을
# 더 걸면 컴파일은 성공하고 술어만 자기모순이 되어 **예외도 경고도 없이 0명**이 된다. 그래서
# 이 소스들의 시각 필드는 광고 대상이 아니다 — 광고하면 조용한 왜곡을 프롬프트가 유도한다.


def _pinned_time_sources() -> dict[str, Mapping[str, Any]]:
    return {
        source_id: declaration
        for source_id, declaration in _time_declaring_sources().items()
        if audience_runtime._pins_its_own_time_column(
            declaration, str(declaration.get("time_column") or "")
        )
    }


def test_a_source_that_pins_its_own_time_column_is_not_sold_as_a_window_field() -> None:
    """선언이 시각 컬럼을 한 값으로 고정하면 심볼을 팔지 않고 이유를 말한다(규칙 11)."""
    lines = audience_runtime._source_time_field_lines(
        {
            "__pinned_snapshot__": {
                "label": "probe snapshot",
                "time_column": "YYYYMM",
                "time_format": "char6",
                "extra_predicates": [
                    "{alias}.YYYYMM = (SELECT MAX(YYYYMM) FROM PROBE_TABLE)"
                ],
            }
        }
    )
    rendered = "\n".join(lines)
    assert "__pinned_snapshot__" in rendered
    assert f"__pinned_snapshot__.{event_ir.TIME_FIELD_SUFFIX}" not in rendered
    assert "YYYYMM" in rendered, "고정된 컬럼 이름을 말하지 않으면 이유가 전달되지 않는다."


def test_a_pinned_time_window_compiles_to_a_self_contradicting_predicate() -> None:
    """왜 팔면 안 되는지 — 컴파일러는 막지 않는다(fail-close 가 아니라 조용한 공집합).

    카탈로그가 이미 구분 정보를 들고 있으므로(핀 술어) 안내가 그 사실을 따라야 한다.
    """
    pinned = _pinned_time_sources()
    assert pinned, "이 카탈로그에는 스냅샷 핀 소스가 없다 — 대조가 성립하지 않는다."

    source_id, declaration = sorted(pinned.items())[0]
    resolved = audience_runtime.resolve_audience_catalog()
    registry = dict(resolved.compiler_events)
    fields = event_compiler.resolve_fields(registry)
    expression = event_ir.condition_from_dict({
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": source_id},
            "where": {
                "type": "time_filter",
                "field": {
                    "type": "field",
                    "name": f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}",
                },
                "window": {
                    "type": "interval",
                    "start": "2026-02-01",
                    "end_exclusive": "2026-03-01",
                },
            },
        },
    })
    sql = event_compiler.compile_expression_sql(
        expression, registry=registry, fields=fields
    )
    column = str(declaration.get("time_column"))
    # 핀 술어와 기간 술어가 같은 컬럼에 AND 로 붙는다 — 최신 칸이 아니면 언제나 공집합이다.
    assert f"{column} = (SELECT MAX(" in sql, sql
    assert f"{column} >=" in sql, sql


@pytest.mark.parametrize("source_id", sorted(_pinned_time_sources()))
def test_pinned_sources_are_reported_as_window_incapable(source_id: str) -> None:
    """카탈로그를 순회해 확인한다 — 핀 소스가 하나 늘어도 안내가 따라간다."""
    block = "\n".join(_time_field_block())
    assert f"- {source_id}.{event_ir.TIME_FIELD_SUFFIX}:" not in block
    assert f"{source_id}:" in block, (
        f"{source_id} 가 안내에서 조용히 사라졌다 — 이유를 말하지 않으면 모델은 "
        "다른 컬럼으로 우회한다(규칙 11)."
    )


# ── 이 절이 실제로 프롬프트에 실리는가 ────────────────────────────────────────────
# 위의 검사들은 모두 :func:`audience_runtime.audience_catalog_guidance` 가 만드는 **문자열**만
# 본다. 그 문자열이 구조화 프롬프트에 실리지 않으면 이 변경이 막으려던 실패(광고되지 않은
# 심볼)가 그대로 되살아나는데, 빌더 검사는 전부 녹색으로 남는다. 배송 경로를 함께 고정한다.


def test_the_event_time_section_reaches_the_structuring_prompt() -> None:
    """graph_rag 가 만드는 안내가 구조화기 user 프롬프트에 그대로 실린다."""
    from query_structurer.prompt import build_campaign_query_plan_v4_user_prompt
    from query_structurer.types import QueryStructuringInput, StructuringContext

    import graph_rag

    guidance = graph_rag._v4_slot_guidance({})
    assert audience_runtime.EVENT_TIME_FIELD_HEADING in guidance, (
        "graph_rag 가 넘기는 안내에 시각 필드 절이 없다."
    )

    prompt = build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query="최근 30일 구매한 회원 수를 알려줘",
            context=StructuringContext(
                current_date="2026-08-07",
                timezone="Asia/Seoul",
                slot_guidance=guidance,
            ),
        )
    )
    assert audience_runtime.EVENT_TIME_FIELD_HEADING in prompt, (
        "안내는 만들어졌지만 프롬프트에 실리지 않는다 — 배선이 끊기면 모델은 심볼을 볼 수 없다."
    )
    for line in _time_field_block():
        if line.startswith("- "):
            assert line in prompt
