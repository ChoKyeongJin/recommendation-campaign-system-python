"""월 단위 컬럼은 날짜가 아니다 — grain 을 타입으로 세운 계약.

이 파일이 있는 이유: 예전에는 미등록 `time_format` 이 조용히 `date` 로 폴백했다. 월 스냅샷
컬럼(nvarchar(6) 'YYYYMM')을 소스로 선언하면 '지난달' 이 ``>= '2026-07-01' AND < '2026-08-01'``
로 렌더되고, nvarchar 와 사전식으로 비교되어(``'6'(0x36) > '-'(0x2D)``) 하한은 통과·상한은 탈락 →
**어떤 프롬프트로도 항상 0건**이 나온다. 예외도 경고도 없이.

0건은 이 저장소에서 문제가 아니라고 합의돼 있으므로(적재가 2017년이다) 그 사고는 **영원히
드러나지 않는다**. 그래서 폴백을 지우고 grain 을 타입으로 만든 것이고, 여기서 그 계약을 고정한다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import resolved_semantic_catalog  # noqa: E402

MONTH_COLUMN = "MS.SNAPSHOT_MONTH"


def _context(**kwargs) -> event_compiler.CompileContext:
    return event_compiler.CompileContext(literals=True, today=date(2026, 8, 15), **kwargs)


def test_unregistered_time_format_fails_closed() -> None:
    """조용한 폴백 금지 — 모르는 포맷은 컴파일 오류다."""
    with pytest.raises(event_compiler.SqlCompileError) as excinfo:
        event_compiler.time_format_data_type("char6_but_typoed")
    assert "등록되지 않은 시간 저장 포맷" in str(excinfo.value)


def test_registered_formats_keep_their_data_types() -> None:
    """기존 두 포맷의 파생은 바이트 동일해야 한다 — 골든과 저장된 플랜이 여기에 걸려 있다."""
    assert event_compiler.time_format_data_type("char8") == "date_char8"
    assert event_compiler.time_format_data_type("date") == "date"
    assert event_compiler.time_format_data_type("char6") == "date_char6"


def test_date_type_vocabulary_has_no_second_copy() -> None:
    """같은 어휘가 여러 곳에 손으로 적혀 있으면 grain 을 늘릴 때 반드시 어긋난다.

    이 저장소에서 반복된 함정이라 파생 관계를 테스트로 고정한다.
    세 번째 사본이던 `semantic_plan_event_lowering` 은 2026-08-05 SemanticPlanV2 폐기로
    사라지므로 여기서 검사하지 않는다(그 파일과 함께 사본도 없어진다).
    """
    grains = event_compiler.DATE_DATA_TYPES
    assert grains == {grain.data_type for grain in event_compiler.TIME_GRAINS.values()}

    catalog_source = (REPO_ROOT / "resolved_semantic_catalog.py").read_text(encoding="utf-8")
    assert "event_compiler.DATE_DATA_TYPES" in catalog_source
    # 손 목록이 되살아나면 즉시 잡는다.
    assert '{"date", "date_char8", "date_string"}' not in catalog_source


def test_month_window_renders_as_month_literals() -> None:
    """'2026년 7월' → >= '202607' AND < '202608'. 반개구간 계약은 그대로다."""
    condition = event_compiler.compile_time_window(
        MONTH_COLUMN,
        event_ir.AbsoluteInterval(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)),
        "w0",
        data_type="date_char6",
        context=_context(),
    )
    assert condition.sql == f"{MONTH_COLUMN} >= '202607' AND {MONTH_COLUMN} < '202608'"


def test_relative_month_window_resolves_to_month_boundaries() -> None:
    """'지난달' 은 IR 단계에서 이미 달 경계로 확정된다 — grain 은 렌더만 맡는다."""
    condition = event_compiler.compile_time_window(
        MONTH_COLUMN,
        event_ir.RelativeWindow(value=1, unit="month"),
        "w0",
        data_type="date_char6",
        context=_context(),
    )
    assert condition.sql == f"{MONTH_COLUMN} >= '202607' AND {MONTH_COLUMN} < '202608'"


def test_day_grained_window_on_a_month_column_fails_closed() -> None:
    """'지난달 15일부터' 는 월 스냅샷으로 답할 수 없다 — 접어서 근사하지 않는다."""
    with pytest.raises(event_compiler.SqlCompileError) as excinfo:
        event_compiler.compile_time_window(
            MONTH_COLUMN,
            event_ir.AbsoluteInterval(start=date(2026, 7, 15), end_exclusive=date(2026, 8, 1)),
            "w0",
            data_type="date_char6",
            context=_context(),
        )
    assert "month 단위" in str(excinfo.value)


def test_rolling_window_on_a_month_column_fails_closed() -> None:
    """'최근 90일'을 3개월로 근사하면 요청하지 않은 대상이 나온다."""
    with pytest.raises(event_compiler.SqlCompileError) as excinfo:
        event_compiler.compile_time_window(
            MONTH_COLUMN,
            event_ir.RollingWindow(value=90, unit="day"),
            "w0",
            data_type="date_char6",
            context=_context(),
        )
    assert "롤링 창" in str(excinfo.value)


def test_day_grains_still_support_rolling_windows() -> None:
    """월 grain 의 fail-close 가 기존 일 단위 경로를 건드리면 안 된다."""
    condition = event_compiler.compile_time_window(
        "EO.ORDER_DATE",
        event_ir.RollingWindow(value=90, unit="day"),
        "w0",
        data_type="date_char8",
        context=_context(),
    )
    assert condition.sql.startswith("EO.ORDER_DATE >= ")


def test_month_source_time_field_is_derived_not_declared_twice() -> None:
    """소스의 time_format 이 시각 필드 grain 의 단일 소유자다."""
    registry = {
        "month_snapshot": event_compiler.EventSpec(
            table="SNAPSHOT_TABLE", alias="MS",
            subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
            time_column="SNAPSHOT_MONTH", time_format="char6", binding="fact_table",
        )
    }
    fields = event_compiler.resolve_fields(registry)
    assert fields["month_snapshot.occurred_at"].data_type == "date_char6"

    conflicting = {
        "month_snapshot.occurred_at": event_compiler.FieldSpec(
            source="month_snapshot", column="SNAPSHOT_MONTH", data_type="date_char8"
        )
    }
    with pytest.raises(event_compiler.SqlCompileError) as excinfo:
        event_compiler.resolve_fields(registry, conflicting)
    assert "어긋납니다" in str(excinfo.value)


def test_bad_time_format_in_a_catalog_surfaces_as_a_catalog_error() -> None:
    """선언 오류는 호출자 계약(CatalogError)으로 나가야 한다 — raw 예외로 터지면 기동이 죽는다."""
    catalog = {
        "version": 1,
        "subject": {"table": "CRM_MB_BASEINFO", "alias": "B", "key": "MEMBER_NO", "name": "회원"},
        "sources": {
            "typoed": {
                "table": "SNAPSHOT_TABLE", "alias": "TS",
                "subject_key": "MEMBER_NO", "event_subject_key": "MEMBER_NO",
                "time_column": "SNAPSHOT_MONTH", "time_format": "yyyymm", "binding": "fact_table",
            }
        },
    }
    with pytest.raises(resolved_semantic_catalog.CatalogError):
        resolved_semantic_catalog.ResolvedSemanticCatalog.from_compiler(runtime_config=catalog)


def test_value_comparison_path_accepts_month_typed_literals() -> None:
    """목록 패리티의 실질 확인 — 새 grain 이 값 비교 경로에서 막히지 않는다.

    확인 지점은 `semantic_plan_event_lowering`(2026-08-05 폐기) 이 아니라 파생의 원본이다.
    """
    assert "date_char6" in event_compiler.DATE_DATA_TYPES
    assert "date_char6" in {
        grain.data_type for grain in event_compiler.TIME_GRAINS.values()
    }
