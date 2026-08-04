"""Cart aggregate metric SQL bindings are owned by the runtime catalog."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402


CATALOG_PATH = REPO_ROOT / "docs/data/runtime/sql/member_target_filters.json"
EXPECTED_EXPRESSIONS = {
    "cart_line_count": "COUNT(DISTINCT CART_PRODUCT_NO)",
    "cart_quantity": "SUM(QTY)",
    "cart_amount": "SUM(TOTAL_SALE_PRICE)",
    "cart_same_product_quantity": "MAX(QTY)",
}


def _cart_catalog() -> dict[str, object]:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    cart_targets = payload["cart_targets"]
    assert isinstance(cart_targets, dict)
    return cart_targets


def test_shipped_catalog_derives_every_cart_aggregate_expression() -> None:
    assert graph_rag._cart_aggregate_metric_expressions(_cart_catalog()) == EXPECTED_EXPRESSIONS
    assert graph_rag._CART_AGGREGATE_METRIC_EXPRESSIONS == EXPECTED_EXPRESSIONS


@pytest.mark.parametrize(
    ("metric", "expected_sql"),
    [
        (
            "cart_line_count",
            "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'cart_abandoner' AS target_segment, "
            "'cart_line_count>=2' AS segment_label\n"
            "FROM CRM_MB_BASEINFO B\n"
            "WHERE B.MEMBER_ID IN (SELECT CART_ID FROM ODS_MALL_OMS_CART WHERE CART_ID IS NOT NULL "
            "AND KEEP_YN = 'Y' GROUP BY CART_ID HAVING COUNT(DISTINCT CART_PRODUCT_NO) >= 2)\n"
            "  AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'",
        ),
        (
            "cart_quantity",
            "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'cart_abandoner' AS target_segment, "
            "'cart_quantity>=2' AS segment_label\n"
            "FROM CRM_MB_BASEINFO B\n"
            "WHERE B.MEMBER_ID IN (SELECT CART_ID FROM ODS_MALL_OMS_CART WHERE CART_ID IS NOT NULL "
            "AND KEEP_YN = 'Y' GROUP BY CART_ID HAVING SUM(QTY) >= 2)\n"
            "  AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'",
        ),
        (
            "cart_amount",
            "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'cart_abandoner' AS target_segment, "
            "'cart_amount>=2' AS segment_label\n"
            "FROM CRM_MB_BASEINFO B\n"
            "WHERE B.MEMBER_ID IN (SELECT CART_ID FROM ODS_MALL_OMS_CART WHERE CART_ID IS NOT NULL "
            "AND KEEP_YN = 'Y' GROUP BY CART_ID HAVING SUM(TOTAL_SALE_PRICE) >= 2)\n"
            "  AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'",
        ),
        (
            "cart_same_product_quantity",
            "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'cart_abandoner' AS target_segment, "
            "'cart_same_product_quantity>=2' AS segment_label\n"
            "FROM CRM_MB_BASEINFO B\n"
            "WHERE B.MEMBER_ID IN (SELECT CART_ID FROM ODS_MALL_OMS_CART WHERE CART_ID IS NOT NULL "
            "AND KEEP_YN = 'Y' GROUP BY CART_ID HAVING MAX(QTY) >= 2)\n"
            "  AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'",
        ),
    ],
)
def test_catalog_derived_metric_preserves_sql_bytes(metric: str, expected_sql: str) -> None:
    candidate = graph_rag.build_cart_aggregate_targets_sql_candidate(
        {
            "target_user": {
                "cart_aggregate": {"metric": metric, "operator": ">=", "threshold": 2}
            }
        }
    )
    assert candidate is not None
    assert candidate["sql"] == expected_sql


def test_missing_cart_aggregate_catalog_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert graph_rag._cart_aggregate_metric_expressions({}) == {}
    monkeypatch.setattr(graph_rag, "_CART_AGGREGATE_METRIC_EXPRESSIONS", {})

    candidate = graph_rag.build_cart_aggregate_targets_sql_candidate(
        {
            "target_user": {
                "cart_aggregate": {
                    "metric": "cart_line_count",
                    "operator": ">=",
                    "threshold": 2,
                }
            }
        }
    )

    assert candidate is None


def test_explicit_unknown_cart_aggregate_metric_fails_closed() -> None:
    candidate = graph_rag.build_cart_aggregate_targets_sql_candidate(
        {
            "target_user": {
                "cart_aggregate": {
                    "metric": "cart_unknown_metric",
                    "operator": ">=",
                    "threshold": 2,
                }
            }
        }
    )

    assert candidate is None

