"""일반 집계 IR → SQL AST 강제 검증 회귀 테스트."""

import json

import pytest

from aggregation_requirements import parse_aggregation_request, validate_aggregation_sql


@pytest.fixture()
def schema_path(tmp_path):
    def col(name, typ="varchar", *, pk=False):
        return {"name": name, "type": typ, "nullable": False, "primary_key": pk}

    tables = {
        "customers": {"columns": [col("customer_id", pk=True), col("region"), col("deleted", "int")], "primary_key": ["customer_id"], "indexes": []},
        "orders": {"columns": [col("order_id", pk=True), col("customer_id"), col("store_id"), col("order_date", "date"), col("amount", "numeric"), col("status")], "primary_key": ["order_id"], "indexes": []},
        "order_items": {"columns": [col("item_id", pk=True), col("order_id"), col("product_id"), col("quantity", "int"), col("sales_amount", "numeric"), col("status")], "primary_key": ["item_id"], "indexes": []},
        "products": {"columns": [col("product_id", pk=True), col("category_id"), col("product_name")], "primary_key": ["product_id"], "indexes": []},
        "categories": {"columns": [col("category_id", pk=True), col("category_name")], "primary_key": ["category_id"], "indexes": []},
        "stores": {"columns": [col("store_id", pk=True), col("store_name")], "primary_key": ["store_id"], "indexes": []},
        "order_tags": {"columns": [col("order_id"), col("tag")], "primary_key": [], "indexes": []},
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"tables": tables}), encoding="utf-8")
    return path


def field(entity, name, table, column, alias=None):
    return {"entity": entity, "field": name, "table": table, "column": column, "alias": alias}


def request(*, target="customer", output=None, filters=None, groups=None, aggs=None, derived=None,
            sorting=None, ranking=None, post=None, relations=None, grain=None, unresolved=None):
    return {
        "targetEntity": target,
        "outputColumns": output or [],
        "filters": filters or [],
        "groupings": groups or [],
        "aggregations": aggs or [],
        "derivedMetrics": derived or [],
        "sorting": sorting or [],
        "ranking": ranking or {"enabled": False, "partitionBy": []},
        "postAggregationFilters": post or [],
        "relationConditions": relations or [],
        "dateGrain": grain,
        "comparison": None,
        "businessRules": {"excludeCancelled": False, "excludeReturned": False, "excludeDeleted": False},
        "assumptions": [],
        "unresolvedFields": unresolved or [],
    }


def agg(metric_id, function, entity, name, table, column, *, distinct=False, condition=None):
    return {"id": metric_id, "function": function, **field(entity, name, table, column),
            "distinct": distinct, "condition": condition, "alias": metric_id}


def parsed(payload, schema_path):
    spec, errors = parse_aggregation_request(payload, schema_path, dialect="postgres")
    assert spec is not None and not errors, [e.to_dict() for e in errors]
    return spec


def result(payload, sql, schema_path):
    return validate_aggregation_sql(parsed(payload, schema_path), sql, schema_path, dialect="postgres")


def codes(validation):
    return {error["code"] for error in validation["errors"]}


def test_01_total_sales_sum(schema_path):
    payload = request(target="metric", aggs=[agg("total_sales", "sum", "order", "salesAmount", "orders", "amount")])
    validation = result(payload, "SELECT SUM(o.amount) AS total_sales FROM orders o WHERE o.order_date >= '2019-01-01' AND o.order_date < '2020-01-01'", schema_path)
    assert validation["valid"] and validation["usedTables"] == [{"table": "orders"}]


def test_02_customer_grouped_sum(schema_path):
    customer = field("customer", "customerId", "orders", "customer_id")
    payload = request(output=[customer], groups=[customer], aggs=[agg("total_sales", "sum", "order", "amount", "orders", "amount")])
    validation = result(payload, "SELECT o.customer_id, SUM(o.amount) total_sales FROM orders o GROUP BY o.customer_id", schema_path)
    assert validation["valid"]


def test_03_monthly_order_count_with_period_filter(schema_path):
    month = field("order", "orderDate", "orders", "order_date")
    payload = request(target="period", groups=[month], filters=[{**month, "id": "filter-1", "operator": "gte", "value": "2019-01-01"}],
                      aggs=[agg("order_count", "count", "order", "orderId", "orders", "order_id")], grain="month")
    sql = "SELECT DATE_TRUNC('month', o.order_date), COUNT(o.order_id) FROM orders o WHERE o.order_date >= '2019-01-01' GROUP BY DATE_TRUNC('month', o.order_date)"
    assert result(payload, sql, schema_path)["valid"]


def test_04_distinct_customer_count_by_product(schema_path):
    product = field("product", "productId", "order_items", "product_id")
    payload = request(target="product", output=[product], groups=[product],
                      aggs=[agg("customer_count", "count_distinct", "customer", "customerId", "orders", "customer_id", distinct=True)])
    sql = "SELECT i.product_id, COUNT(DISTINCT o.customer_id) FROM order_items i JOIN orders o ON o.order_id=i.order_id GROUP BY i.product_id"
    assert result(payload, sql, schema_path)["valid"]


def test_05_average_order_amount_by_customer(schema_path):
    customer = field("customer", "customerId", "orders", "customer_id")
    payload = request(output=[customer], groups=[customer], aggs=[agg("average_amount", "avg", "order", "amount", "orders", "amount")])
    assert result(payload, "SELECT o.customer_id, AVG(o.amount) FROM orders o GROUP BY o.customer_id", schema_path)["valid"]


def test_06_post_aggregation_filter_uses_having(schema_path):
    customer = field("customer", "customerId", "orders", "customer_id")
    payload = request(output=[customer], groups=[customer], aggs=[agg("order_count", "count_distinct", "order", "orderId", "orders", "order_id", distinct=True)],
                      post=[{"id": "post-filter-1", "metricId": "order_count", "operator": "gte", "value": 3}])
    sql = "SELECT o.customer_id, COUNT(DISTINCT o.order_id) order_count FROM orders o GROUP BY o.customer_id HAVING COUNT(DISTINCT o.order_id) >= 3"
    assert result(payload, sql, schema_path)["valid"]


def test_07_top_ten_products_requires_aggregation_order_and_limit(schema_path):
    product = field("product", "productId", "order_items", "product_id")
    payload = request(target="product", output=[product], groups=[product], aggs=[agg("sales_qty", "sum", "orderItem", "quantity", "order_items", "quantity")],
                      sorting=[{"metricId": "sales_qty", "direction": "desc"}],
                      ranking={"enabled": True, "type": "top", "limit": 10, "partitionBy": [], "orderByMetricId": "sales_qty", "tiePolicy": "include_exact_limit"})
    sql = "SELECT i.product_id, SUM(i.quantity) sales_qty FROM order_items i GROUP BY i.product_id ORDER BY sales_qty DESC LIMIT 10"
    assert result(payload, sql, schema_path)["valid"]


def test_08_ranked_products_are_rejoined_to_customers(schema_path):
    product = field("product", "productId", "order_items", "product_id")
    payload = request(output=[field("customer", "customerId", "orders", "customer_id")], groups=[product],
                      aggs=[agg("sales_qty", "sum", "orderItem", "quantity", "order_items", "quantity")],
                      ranking={"enabled": True, "type": "top", "limit": 10, "partitionBy": [], "orderByMetricId": "sales_qty", "tiePolicy": "include_exact_limit"},
                      relations=[{"id": "relation-1", "sourceEntity": "customer", "relation": "purchased", "targetEntity": "selected_product", "mode": "any", "minimumCount": 1}])
    sql = "WITH top_products AS (SELECT i.product_id, SUM(i.quantity) sales_qty FROM order_items i GROUP BY i.product_id ORDER BY sales_qty DESC LIMIT 10) SELECT DISTINCT o.customer_id FROM orders o JOIN order_items i ON i.order_id=o.order_id JOIN top_products t ON t.product_id=i.product_id"
    assert result(payload, sql, schema_path)["valid"]


def test_09_top_products_per_category_requires_partition(schema_path):
    category = field("category", "categoryId", "products", "category_id")
    product = field("product", "productId", "order_items", "product_id")
    payload = request(target="product", groups=[category, product], aggs=[agg("sales", "sum", "orderItem", "sales", "order_items", "sales_amount")],
                      ranking={"enabled": True, "type": "top", "limit": 3, "partitionBy": [category], "orderByMetricId": "sales", "tiePolicy": "include_exact_limit"})
    sql = "WITH ranked AS (SELECT p.category_id, i.product_id, SUM(i.sales_amount) sales, ROW_NUMBER() OVER(PARTITION BY p.category_id ORDER BY SUM(i.sales_amount) DESC) rn FROM order_items i JOIN products p ON p.product_id=i.product_id GROUP BY p.category_id,i.product_id) SELECT * FROM ranked WHERE rn <= 3"
    assert result(payload, sql, schema_path)["valid"]


def test_10_bottom_five_stores(schema_path):
    store = field("store", "storeId", "orders", "store_id")
    payload = request(target="store", groups=[store], aggs=[agg("sales", "sum", "order", "amount", "orders", "amount")],
                      ranking={"enabled": True, "type": "bottom", "limit": 5, "partitionBy": [], "orderByMetricId": "sales", "tiePolicy": "include_exact_limit"})
    assert result(payload, "SELECT o.store_id,SUM(o.amount) sales FROM orders o GROUP BY o.store_id ORDER BY sales ASC LIMIT 5", schema_path)["valid"]


def test_11_purchase_customer_ratio_has_zero_guard(schema_path):
    payload = request(target="metric", aggs=[agg("buyers", "count_distinct", "customer", "customerId", "orders", "customer_id", distinct=True),
                                                     agg("customers", "count_distinct", "customer", "customerId", "customers", "customer_id", distinct=True)],
                      derived=[{"id": "purchase_rate", "type": "division", "numeratorMetricId": "buyers", "denominatorMetricId": "customers", "multiplyBy": 100, "alias": "purchase_rate", "zeroDivisionPolicy": "null"}])
    sql = "SELECT COUNT(DISTINCT o.customer_id) * 100.0 / NULLIF(COUNT(DISTINCT c.customer_id),0) purchase_rate FROM customers c LEFT JOIN orders o ON o.customer_id=c.customer_id"
    assert result(payload, sql, schema_path)["valid"]


def test_12_month_over_month_growth_rate(schema_path):
    payload = request(target="metric", aggs=[agg("current_sales", "sum", "order", "amount", "orders", "amount"), agg("previous_sales", "sum", "order", "amount", "orders", "amount")],
                      derived=[{"id": "growth", "type": "growth_rate", "currentMetricId": "current_sales", "previousMetricId": "previous_sales", "multiplyBy": 100, "alias": "growth", "zeroDivisionPolicy": "null"}])
    sql = "SELECT (SUM(CASE WHEN o.order_date >= '2020-02-01' THEN o.amount ELSE 0 END)-SUM(CASE WHEN o.order_date >= '2020-01-01' AND o.order_date < '2020-02-01' THEN o.amount ELSE 0 END))*100.0/NULLIF(SUM(CASE WHEN o.order_date >= '2020-01-01' AND o.order_date < '2020-02-01' THEN o.amount ELSE 0 END),0) growth FROM orders o"
    assert result(payload, sql, schema_path)["valid"]


def test_13_conditional_aggregations(schema_path):
    customer = field("customer", "customerId", "orders", "customer_id")
    normal = {**field("order", "status", "orders", "status"), "id": "condition-1", "operator": "eq", "value": "COMPLETED"}
    payload = request(groups=[customer], aggs=[agg("completed", "count", "order", "orderId", "orders", "order_id", condition=normal)])
    sql = "SELECT o.customer_id, COUNT(CASE WHEN o.status = 'COMPLETED' THEN o.order_id END) completed FROM orders o GROUP BY o.customer_id"
    assert result(payload, sql, schema_path)["valid"]


def test_14_cumulative_monthly_sales(schema_path):
    month = field("order", "orderDate", "orders", "order_date")
    payload = request(target="period", groups=[month], aggs=[agg("monthly_sales", "sum", "order", "amount", "orders", "amount")],
                      derived=[{"id": "cumulative_sales", "type": "cumulative", "numeratorMetricId": "monthly_sales", "alias": "cumulative_sales", "zeroDivisionPolicy": None}])
    sql = "WITH monthly AS (SELECT DATE_TRUNC('month',o.order_date) month, SUM(o.amount) monthly_sales FROM orders o GROUP BY DATE_TRUNC('month',o.order_date)) SELECT month, monthly_sales, SUM(monthly_sales) OVER(ORDER BY month) cumulative_sales FROM monthly"
    assert result(payload, sql, schema_path)["valid"]


def test_15_unknown_column_blocks_generation(schema_path):
    payload = request(aggs=[agg("sales", "sum", "order", "unknown", "orders", "does_not_exist")])
    _spec, errors = parse_aggregation_request(payload, schema_path, dialect="postgres")
    assert "UNKNOWN_COLUMN" in {error.code for error in errors}


def test_16_ambiguous_best_selling_metric_is_unresolved(schema_path):
    payload = request(target="product", unresolved=["best_selling_metric: 판매량 또는 매출 기준 확인 필요"])
    spec, _errors = parse_aggregation_request(payload, schema_path, dialect="postgres")
    validation = validate_aggregation_sql(spec, "SELECT p.product_id FROM products p", schema_path, dialect="postgres")
    assert "UNRESOLVED_BUSINESS_RULE" in codes(validation)


def test_17_non_unique_join_blocks_amplified_sum(schema_path):
    payload = request(target="metric", aggs=[agg("sales", "sum", "order", "amount", "orders", "amount")])
    validation = result(payload, "SELECT SUM(o.amount) FROM orders o JOIN order_tags t ON t.order_id=o.order_id", schema_path)
    assert "AGGREGATION_DUPLICATION_RISK" in codes(validation)


def test_18_where_and_having_are_not_interchangeable(schema_path):
    customer = field("customer", "customerId", "orders", "customer_id")
    payload = request(groups=[customer], filters=[{**field("order", "orderDate", "orders", "order_date"), "id": "filter-1", "operator": "gte", "value": "2019-01-01"}],
                      aggs=[agg("order_count", "count", "order", "orderId", "orders", "order_id")],
                      post=[{"id": "post-filter-1", "metricId": "order_count", "operator": "gte", "value": 3}])
    validation = result(payload, "SELECT o.customer_id,COUNT(o.order_id) FROM orders o WHERE o.order_date >= '2019-01-01' GROUP BY o.customer_id", schema_path)
    assert "MISSING_HAVING" in codes(validation)


def test_19_row_count_and_distinct_order_count_are_different(schema_path):
    payload = request(target="metric", aggs=[agg("orders", "count_distinct", "order", "orderId", "orders", "order_id", distinct=True)])
    validation = result(payload, "SELECT COUNT(o.order_id) FROM orders o", schema_path)
    assert "MISSING_DISTINCT" in codes(validation)


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "UPDATE orders SET amount=0", "DROP TABLE orders",
    "SELECT * FROM orders; DELETE FROM orders",
])
def test_20_unsafe_or_multiple_statements_are_blocked(schema_path, sql):
    payload = request(target="metric", aggs=[agg("orders", "count", "order", "orderId", "orders", "order_id")])
    validation = validate_aggregation_sql(parsed(payload, schema_path), sql, schema_path, dialect="postgres")
    assert not validation["valid"]
    assert codes(validation) & {"UNSAFE_SQL", "MULTIPLE_SQL_STATEMENTS"}
