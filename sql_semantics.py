"""AST 기반 SQL 의미 추출기(§2) — sqlglot 로 파싱해 SqlSemantics 구조체를 만든다.

문자열 비교가 아니라 AST 를 정규화해 의미를 뽑는다: 참조 테이블/컬럼, SELECT 항목, JOIN(종류·조건),
WHERE/HAVING 필터, GROUP BY, DISTINCT, ORDER BY, LIMIT, 서브쿼리, CTE, EXISTS/IN, 날짜 범위, NULL 처리,
중복 제거 기준, 집계 함수. alias→실테이블 해소를 먼저 수행해 alias 차이가 의미 차이로 보이지 않게 한다.

파싱 실패는 **의미상 fail 이 아니라 별도의 기술 오류**로 분류한다(§2 마지막): SqlParseError 를 던지고,
호출자(semantic_mapping)가 이를 parser_errors + review 로 처리한다 — 정상 SQL 을 파서 한계로 fail 시키지 않는다.

이 모듈은 graph_rag 를 import 하지 않는다(순수 in/out).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp


class SqlParseError(Exception):
    """SQL 파싱/분석 실패 — 의미 불일치가 아니라 기술 오류(파서 한계·미지원 방언)."""


@dataclass
class Filter:
    """단일 비교/술어. normalized_* 는 alias 해소·연산자 정규화를 거친 형태."""

    expression: str
    normalized_field: str | None
    normalized_operator: str | None
    normalized_value: str | None
    location: str = "where"       # where | having | join | subquery
    negated: bool = False         # 상위 NOT 로 부정됐는지
    is_date_window: bool = False   # 날짜/기간 비교인지

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JoinInfo:
    table: str
    kind: str                     # inner | left | right | full | cross
    on: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExistsCondition:
    """EXISTS / NOT EXISTS 서브쿼리(anti-join 포함)."""

    negated: bool
    tables: list[str] = field(default_factory=list)
    correlated_on: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    expression: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "filters": [f.to_dict() for f in self.filters]}


@dataclass
class InCondition:
    """IN / NOT IN. subquery=True 면 서브쿼리 IN(NULL semantics 주의 대상)."""

    field: str | None
    negated: bool
    subquery: bool
    tables: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    expression: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AggregateInfo:
    func: str                     # count | sum | avg | min | max | count_distinct
    argument: str | None
    operator: str | None = None   # HAVING 비교 연산자
    value: str | None = None      # HAVING 임계값
    expression: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SqlSemantics:
    """AST 에서 추출한 구조화 SQL 의미(§2)."""

    dialect: str
    tables: list[str] = field(default_factory=list)
    selected_columns: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    joins: list[JoinInfo] = field(default_factory=list)
    exists_conditions: list[ExistsCondition] = field(default_factory=list)
    in_conditions: list[InCondition] = field(default_factory=list)
    aggregates: list[AggregateInfo] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    has_distinct: bool = False
    deduplication_keys: list[str] = field(default_factory=list)
    cte_names: list[str] = field(default_factory=list)
    has_subquery: bool = False
    limit: int | None = None
    # {alias 혹은 실명(lower)} -> 실테이블명(lower)
    alias_map: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "tables": self.tables,
            "selected_columns": self.selected_columns,
            "filters": [f.to_dict() for f in self.filters],
            "joins": [j.to_dict() for j in self.joins],
            "exists_conditions": [e.to_dict() for e in self.exists_conditions],
            "in_conditions": [i.to_dict() for i in self.in_conditions],
            "aggregates": [a.to_dict() for a in self.aggregates],
            "group_by": self.group_by,
            "order_by": self.order_by,
            "has_distinct": self.has_distinct,
            "deduplication_keys": self.deduplication_keys,
            "cte_names": self.cte_names,
            "has_subquery": self.has_subquery,
            "limit": self.limit,
            "alias_map": self.alias_map,
        }


# 날짜/기간 비교로 볼 함수/키워드(날짜 창 방향 판정 대상).
_DATE_TOKENS = ("current_date", "getdate", "current_timestamp", "now", "interval",
                "dateadd", "date_sub", "date_add", "sysdate")

_OP_MAP = {
    exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
}


def _dialect_arg(dialect: str | None) -> str | None:
    """sqlglot 방언명 정규화. 알 수 없으면 None(제네릭 파싱)."""
    if not dialect:
        return None
    d = dialect.strip().lower()
    aliases = {
        "mssql": "tsql", "sqlserver": "tsql", "sql_server": "tsql", "tsql": "tsql",
        "mariadb": "mysql", "mysql": "mysql", "postgres": "postgres", "postgresql": "postgres",
        "pg": "postgres", "oracle": "oracle", "sqlite": "sqlite",
    }
    return aliases.get(d, d if d in sqlglot.Dialects.__members__.values() else None)


def _build_alias_map(root: exp.Expression) -> dict[str, str]:
    """alias(및 실명) → 실테이블명(lower). alias 차이가 의미 차이로 보이지 않게 해소용."""
    alias_map: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        alias = (table.alias or "").lower()
        alias_map.setdefault(name, name)
        if alias:
            alias_map[alias] = name
    return alias_map


def _resolve_column(col: exp.Column, alias_map: dict[str, str]) -> str:
    """컬럼을 '<실테이블>.<컬럼>'(lower) 로 정규화. 테이블 alias 는 실명으로 치환한다."""
    name = (col.name or "").lower()
    tbl = (col.table or "").lower()
    if tbl:
        tbl = alias_map.get(tbl, tbl)
        return f"{tbl}.{name}"
    return name


def _normalize_value(node: exp.Expression) -> str:
    """비교 우변을 정규화 문자열로. 리터럴은 값만, 식은 공백 없는 SQL 로."""
    if isinstance(node, exp.Literal):
        return node.name
    if isinstance(node, exp.Boolean):
        return "true" if node.this else "false"
    if isinstance(node, exp.Null):
        return "null"
    return node.sql().replace(" ", "").lower()


def _is_date_expr(node: exp.Expression) -> bool:
    text = node.sql().lower()
    return any(tok in text for tok in _DATE_TOKENS)


def _extract_comparison(node: exp.Expression, alias_map: dict[str, str], location: str,
                        negated: bool) -> Filter | None:
    """이항 비교(=, !=, >, >=, <, <=) → Filter. 컬럼이 좌변이 아니면 좌우를 바꿔 정규화한다."""
    op = _OP_MAP.get(type(node))
    if op is None:
        return None
    left, right = node.this, node.expression
    field_node, value_node, flip = left, right, False
    if not isinstance(left, exp.Column) and isinstance(right, exp.Column):
        field_node, value_node, flip = right, left, True
    normalized_field = (
        _resolve_column(field_node, alias_map) if isinstance(field_node, exp.Column)
        else field_node.sql().replace(" ", "").lower()
    )
    operator = _flip_operator(op) if flip else op
    return Filter(
        expression=node.sql(),
        normalized_field=normalized_field,
        normalized_operator=operator,
        normalized_value=_normalize_value(value_node),
        location=location,
        negated=negated,
        is_date_window=_is_date_expr(value_node) or _is_date_expr(field_node),
    )


def _flip_operator(op: str) -> str:
    return {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "!=": "!="}[op]


def _extract_between(node: exp.Between, alias_map: dict[str, str], location: str,
                     negated: bool) -> list[Filter]:
    """BETWEEN a AND b → 두 개의 범위 Filter(>=, <=)로 전개(범위↔부등호 동치의 기반)."""
    field_node = node.this
    normalized_field = (
        _resolve_column(field_node, alias_map) if isinstance(field_node, exp.Column)
        else field_node.sql().replace(" ", "").lower()
    )
    low, high = node.args.get("low"), node.args.get("high")
    is_date = _is_date_expr(node)
    out: list[Filter] = []
    if low is not None:
        out.append(Filter(node.sql(), normalized_field, "<" if negated else ">=",
                          _normalize_value(low), location, negated, is_date))
    if high is not None:
        out.append(Filter(node.sql(), normalized_field, ">" if negated else "<=",
                          _normalize_value(high), location, negated, is_date))
    return out


def _extract_null(node: exp.Expression, alias_map: dict[str, str], location: str,
                  negated: bool) -> Filter | None:
    """IS NULL / IS NOT NULL → Filter(operator=is_null|is_not_null)."""
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        field_node = node.this
        normalized_field = (
            _resolve_column(field_node, alias_map) if isinstance(field_node, exp.Column)
            else field_node.sql().replace(" ", "").lower()
        )
        op = "is_not_null" if negated else "is_null"
        return Filter(node.sql(), normalized_field, op, "null", location, negated, False)
    return None


def _extract_like(node: exp.Expression, alias_map: dict[str, str], location: str,
                  negated: bool) -> Filter | None:
    """LIKE/ILIKE → Filter(operator='like'). 값/코드 부분일치라 규칙으로 등호 동치를 확정하지 않고
    (필드는 present 로 인식) 매핑기가 ambiguous 로 두게 한다 — 필드 누락(missing)으로 오판하지 않기 위함."""
    field_node = node.this
    normalized_field = (
        _resolve_column(field_node, alias_map) if isinstance(field_node, exp.Column)
        else field_node.sql().replace(" ", "").lower()
    )
    return Filter(node.sql(), normalized_field, "like", _normalize_value(node.expression),
                  location, negated, False)


def _split_conjuncts(node: exp.Expression, negated: bool = False):
    """AND/NOT 를 펼쳐 (leaf, negated) 를 순회. OR 는 leaf 로 두되 하위 컬럼은 별도로 수집한다.

    AND 순서 차이·괄호 차이는 이 평탄화로 사라진다(집합으로 다룸). NOT 는 극성 플래그로 전파한다."""
    if isinstance(node, exp.Paren):
        yield from _split_conjuncts(node.this, negated)
    elif isinstance(node, exp.And):
        yield from _split_conjuncts(node.left, negated)
        yield from _split_conjuncts(node.right, negated)
    elif isinstance(node, exp.Not):
        yield from _split_conjuncts(node.this, not negated)
    else:
        yield node, negated


def _collect_filters_from_predicate(predicate: exp.Expression, alias_map: dict[str, str],
                                    location: str, semantics: "SqlSemantics") -> None:
    """WHERE/HAVING/ON 술어를 leaf 로 분해해 Filter/EXISTS/IN/집계 조건으로 수집한다."""
    for leaf, negated in _split_conjuncts(predicate):
        if isinstance(leaf, exp.Exists):
            semantics.exists_conditions.append(_extract_exists(leaf, alias_map, negated))
            continue
        if isinstance(leaf, exp.In):
            semantics.in_conditions.append(_extract_in(leaf, alias_map, negated))
            continue
        if isinstance(leaf, exp.Between):
            semantics.filters.extend(_extract_between(leaf, alias_map, location, negated))
            continue
        if isinstance(leaf, exp.Is):
            null_filter = _extract_null(leaf, alias_map, location, negated)
            if null_filter is not None:
                semantics.filters.append(null_filter)
            continue
        if isinstance(leaf, (exp.Like, exp.ILike)):
            like_filter = _extract_like(leaf, alias_map, location, negated)
            if like_filter is not None:
                semantics.filters.append(like_filter)
            continue
        # HAVING 안의 집계 비교(COUNT(...) >= n)는 aggregates 로도 기록한다.
        if location == "having" and type(leaf) in _OP_MAP:
            agg = _extract_having_aggregate(leaf, alias_map)
            if agg is not None:
                semantics.aggregates.append(agg)
                continue
        comparison = _extract_comparison(leaf, alias_map, location, negated)
        if comparison is not None:
            semantics.filters.append(comparison)


def _extract_exists(node: exp.Exists, alias_map: dict[str, str], negated: bool) -> ExistsCondition:
    sub = node.this
    tables, correlated, sub_filters = [], [], []
    if isinstance(sub, exp.Select) or (hasattr(sub, "find_all")):
        sub_alias = _build_alias_map(sub)
        merged = {**alias_map, **sub_alias}
        tables = sorted({t for t in sub_alias.values()})
        where = sub.args.get("where") if hasattr(sub, "args") else None
        if where is not None:
            tmp = SqlSemantics(dialect="")
            _collect_filters_from_predicate(where.this, merged, "subquery", tmp)
            sub_filters = tmp.filters
            # 상관 조인 컬럼(양쪽 컬럼 비교)을 correlated_on 으로 기록.
            for f in tmp.filters:
                if f.normalized_value and "." in str(f.normalized_value) and f.normalized_operator == "=":
                    correlated.append(f"{f.normalized_field}={f.normalized_value}")
    return ExistsCondition(negated=negated, tables=tables, correlated_on=correlated,
                           filters=sub_filters, expression=node.sql())


def _extract_in(node: exp.In, alias_map: dict[str, str], negated: bool) -> InCondition:
    field_node = node.this
    normalized_field = (
        _resolve_column(field_node, alias_map) if isinstance(field_node, exp.Column)
        else field_node.sql().replace(" ", "").lower()
    )
    query = node.args.get("query")
    exprs = node.args.get("expressions") or []
    if query is not None:
        sub_alias = _build_alias_map(query)
        return InCondition(field=normalized_field, negated=negated, subquery=True,
                           tables=sorted(set(sub_alias.values())), expression=node.sql())
    values = [_normalize_value(e) for e in exprs]
    return InCondition(field=normalized_field, negated=negated, subquery=False,
                       values=values, expression=node.sql())


def _extract_having_aggregate(node: exp.Expression, alias_map: dict[str, str]) -> AggregateInfo | None:
    op = _OP_MAP.get(type(node))
    if op is None:
        return None
    left, right = node.this, node.expression
    agg_node, value_node = (left, right)
    if not _is_aggregate_func(left) and _is_aggregate_func(right):
        agg_node, value_node = right, left
        op = _flip_operator(op)
    if not _is_aggregate_func(agg_node):
        return None
    func, arg = _aggregate_func_and_arg(agg_node, alias_map)
    return AggregateInfo(func=func, argument=arg, operator=op,
                         value=_normalize_value(value_node), expression=node.sql())


_AGG_FUNCS = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)


def _is_aggregate_func(node: exp.Expression) -> bool:
    return isinstance(node, _AGG_FUNCS)


def _aggregate_func_and_arg(node: exp.Expression, alias_map: dict[str, str]) -> tuple[str, str | None]:
    name = type(node).__name__.lower()
    arg_node = node.this
    distinct = False
    if isinstance(arg_node, exp.Distinct):
        distinct = True
        exprs = arg_node.expressions
        arg_node = exprs[0] if exprs else None
    arg = None
    if isinstance(arg_node, exp.Column):
        arg = _resolve_column(arg_node, alias_map)
    elif arg_node is not None and not isinstance(arg_node, exp.Star):
        arg = arg_node.sql().replace(" ", "").lower()
    if name == "count" and distinct:
        name = "count_distinct"
    return name, arg


def extract_sql_semantics(sql: str, dialect: str | None = None) -> SqlSemantics:
    """SQL → SqlSemantics. 파싱 실패 시 SqlParseError(기술 오류, 의미 fail 아님)."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlParseError("empty SQL")
    d = _dialect_arg(dialect)
    try:
        parsed = sqlglot.parse_one(sql, dialect=d)
    except Exception as exc:  # noqa: BLE001 - 파서 한계는 기술 오류로 승격
        raise SqlParseError(f"parse failed ({type(exc).__name__}): {exc}") from exc
    if parsed is None:
        raise SqlParseError("no statement parsed")

    semantics = SqlSemantics(dialect=(d or "generic"))
    alias_map = _build_alias_map(parsed)
    semantics.alias_map = alias_map
    semantics.tables = sorted({t for t in alias_map.values()})

    # CTE(sqlglot 버전별 arg 키 차이가 있어 노드 탐색으로 통일).
    semantics.cte_names = [
        cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE) if cte.alias_or_name
    ]
    # CTE 이름은 실테이블이 아니므로 tables 에서 제외한다(FROM recent 는 실제 참조 테이블이 아님).
    if semantics.cte_names:
        cte_set = set(semantics.cte_names)
        semantics.tables = [t for t in semantics.tables if t not in cte_set]

    # 최상위 SELECT(파싱 결과가 WITH 면 내부 SELECT).
    select = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    if select is None:
        raise SqlParseError("no SELECT block")

    # DISTINCT.
    semantics.has_distinct = bool(select.args.get("distinct"))

    # SELECT 항목.
    for proj in select.expressions:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(target, exp.Column):
            semantics.selected_columns.append(_resolve_column(target, alias_map))
        elif isinstance(target, exp.Star):
            semantics.selected_columns.append("*")
        else:
            semantics.selected_columns.append(target.sql().replace(" ", "").lower())

    # JOIN.
    for join in select.args.get("joins", []) or []:
        j_table = join.this
        table_name = ""
        if isinstance(j_table, exp.Table):
            table_name = alias_map.get((j_table.alias or j_table.name or "").lower(),
                                       (j_table.name or "").lower())
        kind = (join.args.get("kind") or join.args.get("side") or "inner")
        kind = str(kind).lower() or "inner"
        on_node = join.args.get("on")
        semantics.joins.append(JoinInfo(
            table=table_name, kind=kind, on=on_node.sql() if on_node is not None else None))
        # JOIN ON 술어도 필터로 수집(조건이 WHERE 아닌 JOIN 에 있어도 누락으로 보지 않게 §4).
        if on_node is not None:
            _collect_filters_from_predicate(on_node, alias_map, "join", semantics)

    # WHERE.
    where = select.args.get("where")
    if where is not None:
        _collect_filters_from_predicate(where.this, alias_map, "where", semantics)

    # CTE / FROM 절 파생 서브쿼리의 WHERE 필터도 수집한다 — 조건이 서브쿼리에 있어도 누락으로 보지
    # 않게 한다(§4: 조건이 WHERE 가 아니라 서브쿼리/CTE 에 위치한 경우). EXISTS/IN 서브쿼리는 술어
    # 수집기가 이미 별도 처리하므로 여기선 CTE 본문과 FROM/JOIN 파생 테이블만 스캔한다.
    for cte in parsed.find_all(exp.CTE):
        inner = cte.this if isinstance(cte.this, exp.Select) else (
            cte.this.find(exp.Select) if cte.this is not None else None)
        inner_where = inner.args.get("where") if inner is not None else None
        if inner_where is not None:
            _collect_filters_from_predicate(inner_where.this, alias_map, "cte", semantics)
    for sub in select.find_all(exp.Subquery):
        parent = sub.parent
        if not isinstance(parent, (exp.From, exp.Join)):
            continue  # EXISTS/IN/스칼라 서브쿼리는 제외(FROM/JOIN 파생 테이블만)
        inner = sub.this if isinstance(sub.this, exp.Select) else sub.find(exp.Select)
        inner_where = inner.args.get("where") if inner is not None else None
        if inner_where is not None:
            _collect_filters_from_predicate(inner_where.this, alias_map, "subquery", semantics)

    # GROUP BY.
    group = select.args.get("group")
    if group is not None:
        for e in group.expressions:
            if isinstance(e, exp.Column):
                semantics.group_by.append(_resolve_column(e, alias_map))
            else:
                semantics.group_by.append(e.sql().replace(" ", "").lower())

    # HAVING.
    having = select.args.get("having")
    if having is not None:
        _collect_filters_from_predicate(having.this, alias_map, "having", semantics)

    # 중첩 서브쿼리(FROM/JOIN 파생·IN 서브쿼리)의 HAVING 집계도 수집한다 — 회원별 집계 임계를
    # 'MEMBER_NO IN (SELECT ... GROUP BY ... HAVING COUNT>=5)' 로 컴파일하는 흔한 패턴을 놓치지 않기 위함.
    for sub_sel in parsed.find_all(exp.Select):
        if sub_sel is select:
            continue
        sub_having = sub_sel.args.get("having")
        if sub_having is None:
            continue
        tmp = SqlSemantics(dialect="")
        _collect_filters_from_predicate(sub_having.this, alias_map, "having", tmp)
        for agg in tmp.aggregates:
            if not any(a.func == agg.func and a.argument == agg.argument
                       and a.operator == agg.operator and a.value == agg.value
                       for a in semantics.aggregates):
                semantics.aggregates.append(agg)

    # SELECT 절 집계 함수도 수집(HAVING 없이 SELECT COUNT(DISTINCT ...) 등).
    for proj in select.expressions:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if _is_aggregate_func(target):
            func, arg = _aggregate_func_and_arg(target, alias_map)
            if not any(a.func == func and a.argument == arg for a in semantics.aggregates):
                semantics.aggregates.append(AggregateInfo(func=func, argument=arg, expression=target.sql()))

    # ORDER BY.
    order = select.args.get("order")
    if order is not None:
        for e in order.expressions:
            semantics.order_by.append(e.sql().replace(" ", "").lower())

    # LIMIT / TOP.
    limit = select.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        try:
            semantics.limit = int(limit.expression.name)
        except (TypeError, ValueError):
            semantics.limit = None
    else:
        # TSQL TOP.
        top = None
        distinct_node = select.args.get("distinct")
        if isinstance(distinct_node, exp.Distinct):
            top = None
        limit_arg = select.find(exp.Limit)
        if limit_arg is not None and isinstance(limit_arg.expression, exp.Literal):
            try:
                semantics.limit = int(limit_arg.expression.name)
            except (TypeError, ValueError):
                pass

    # 서브쿼리 존재.
    semantics.has_subquery = any(True for _ in select.find_all(exp.Subquery)) or bool(
        semantics.exists_conditions) or any(i.subquery for i in semantics.in_conditions) or bool(
        semantics.cte_names)

    # 중복 제거 기준: DISTINCT + 단일 SELECT 컬럼, 또는 GROUP BY 키.
    semantics.deduplication_keys = _infer_dedup_keys(semantics)

    return semantics


def _infer_dedup_keys(semantics: SqlSemantics) -> list[str]:
    """결과 행의 유일성 기준(§중복 제거). DISTINCT 면 SELECT 컬럼, 아니면 GROUP BY 키."""
    cols = [c for c in semantics.selected_columns if c != "*"]
    if semantics.has_distinct and cols:
        return list(dict.fromkeys(cols))
    if semantics.group_by:
        return list(dict.fromkeys(semantics.group_by))
    # DISTINCT 도 GROUP BY 도 없으면 유일성 보장 없음(빈 목록).
    return []
