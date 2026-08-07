"""집계 타겟 SQL 의 기간 앵커 계약 — 절대 달력 창은 DB 시계로 재앵커되지 않는다.

원본은 `tests/test_lapsed_buyer_regression.py` 의
`test_calendar_window_reaches_sql_without_database_clock_reanchoring` 였고, 2026-08-05
'노드 → 실행 슬롯 컴파일 계층 폐기' 정리에서 함께 삭제됐다. 그 삭제는 과했다 — 컴파일러는
`target_user` dict 를 만들어 주던 **픽스처**였을 뿐이고, 단언이 재던 대상은
`graph_rag.build_aggregate_targets_sql_candidate` 와 그 안의 `window=condition.get("window")`
분기, 즉 **SQL 빌더의 계약**이다. 그 대상은 지금도 살아 있고
`targeting_ir.SLOT_SHAPES["aggregate_conditions"]` 가 window 를 정식 슬롯 필드로 선언한다.
그래서 픽스처만 리터럴 dict 로 바꿔 같은 단언을 복원한다.

왜 이 계약이 중요한가: `aggregate_conditions` 의 기간에는 두 표기가 있고 **렌더가 서로 다르다**.
  - `window`(절대 달력 구간)   → `ORDER_DATE BETWEEN '20260301' AND '20260331'`
  - `window_days`(후행 N일)    → `ORDER_DATE >= ...DATEADD(DAY, -N, GETDATE())...`
window 분기가 무너지면 절대 구간이 조용히 후자로 흘러 **'2026년 3월'이 '실행 시점 기준 최근
N일'로 뜻이 바뀐다**. SQL 은 여전히 유효해 보이고 실패도 나지 않으므로, 이 차이는 단언으로만
드러난다. 아래 두 테스트는 그 두 렌더를 각각 고정한다(뒤쪽이 앞쪽 부정 단언의 비어있지 않음을
증명한다 — GETDATE 는 실제로 나올 수 있는 문자열이다).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import targeting_ir  # noqa: E402

# 원본 테스트의 입력('최근 1개월 5건 이상 구매한 회원', 기준일 2026-03-31)을 컴파일러 대신
# 리터럴로 적는다. 컴파일러가 만들던 값과 같은 모양이다.
_CALENDAR_WINDOW: dict[str, Any] = {
    "type": "relative",
    "value": 1,
    "unit": "months",
    "from": "20260301",
    "to": "20260331",
}


def _order_count_query_plan(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent": "find_user_segment",
        "target_user": {"aggregate_conditions": [condition]},
    }


def test_aggregate_condition_window_is_a_declared_slot_field() -> None:
    """전제 고정: `window` 는 임시 키가 아니라 슬롯 스키마가 선언한 필드다.

    이 전제가 깨지면 아래 단언은 '아무도 안 보내는 키'를 재는 셈이 되므로 먼저 확인한다.
    """
    shape = targeting_ir.SLOT_SHAPES["aggregate_conditions"]
    item_properties = shape.schema["items"]["properties"]
    assert "window" in item_properties, item_properties.keys()
    assert "window_days" in item_properties, item_properties.keys()


def test_calendar_window_reaches_sql_without_database_clock_reanchoring(
    member_slot_gate_lifted: None,
) -> None:
    """절대 달력 창은 그대로 렌더되고 DB 시계(GETDATE)로 재앵커되지 않는다."""
    query_plan = _order_count_query_plan(
        {
            "metric_id": "order_count",
            "operator": ">=",
            "threshold": 5,
            "window": _CALENDAR_WINDOW,
        }
    )

    candidate = graph_rag.build_aggregate_targets_sql_candidate(query_plan)

    assert candidate is not None, query_plan.get("unsupported")
    assert "ORDER_DATE BETWEEN '20260301' AND '20260331'" in candidate["sql"]
    assert "GETDATE" not in candidate["sql"].upper()


def test_trailing_day_window_still_anchors_on_the_database_clock() -> None:
    """반대 방향(위 부정 단언이 비어있지 않음을 증명한다).

    같은 빌더의 서브쿼리 조립부에 `window` 대신 `window_days` 를 주면 GETDATE 앵커가 실제로
    나온다. 즉 `GETDATE not in sql` 은 '어차피 안 나오는 문자열'을 재는 공허한 단언이 아니라
    두 렌더 중 어느 쪽이 선택됐는지를 가르는 판별식이다.
    """
    config = graph_rag._aggregate_targets_config()
    metric = config["metrics"]["order_count"]

    trailing = graph_rag._aggregate_member_subquery(
        config, metric, ">=", 5, 30, "AGG0", window=None
    )
    calendar = graph_rag._aggregate_member_subquery(
        config, metric, ">=", 5, None, "AGG0", window=_CALENDAR_WINDOW
    )

    assert trailing is not None and calendar is not None
    assert "GETDATE" in trailing.upper(), trailing
    assert "ORDER_DATE BETWEEN '20260301' AND '20260331'" in calendar, calendar
    assert "GETDATE" not in calendar.upper(), calendar
