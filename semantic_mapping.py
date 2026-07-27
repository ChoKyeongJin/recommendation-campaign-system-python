"""규칙 기반 요구사항↔SQL 매핑 + 의미 동치 판정 + pass/review/fail 판정(§3·§4·§6).

핵심 원칙(§4): SQL 의 **문자열 형태**가 아니라 **결과 집합의 의미**로 판정한다. 표현 차이(BETWEEN↔범위,
IN↔EXISTS, JOIN↔상관 서브쿼리, CTE↔인라인, AND 순서/괄호/alias 차이, 날짜 함수 표현, DISTINCT↔GROUP BY,
NOT EXISTS↔anti-join)는 자동 실패로 보지 않는다. 단 NULL semantics 로 결과가 달라질 수 있는
NOT IN↔NOT EXISTS 는 무조건 동치로 보지 않고 review 로 분리한다.

판정 순서(§4 하단): (1) AST 정규화(sql_semantics 가 이미 수행) → (2) alias 해소(추출기 수행) →
(3) 연산자 정규화 → (4) 조건식 canonicalization → (5) 규칙 기반 동치 판정 → (6) 규칙 미결 시에만 LLM.

이 모듈은 규칙 판정까지만 담당한다(LLM 미포함, 순수 함수). LLM 근거 보강은 semantic_validation.py 가 주입한다.
"""

from __future__ import annotations

import re
from typing import Any

from sql_semantics import SqlSemantics, Filter, ExistsCondition, InCondition, AggregateInfo
from target_spec import (
    Exclusion,
    ExecutionAssertion,
    PolicyViolation,
    Requirement,
    RequirementCheck,
    TargetSpecification,
    ValidationResult,
)


# ------------------------------------------------------------------ 값/필드 정규화

def _short_field(field: str | None) -> str:
    """'customers.age' → 'age'. 테이블 한정자를 떼 필드명만 비교(스키마 alias/테이블 차이 흡수)."""
    if not field:
        return ""
    return str(field).split(".")[-1].strip().lower()


def _num(value: Any) -> float | None:
    """비교 가능한 숫자로. 콤마 제거. 불가하면 None."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        m = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
        if m:
            return float(text)
    return None


def _interval_days(text: str) -> int | None:
    """SQL 날짜 식에서 롤링 창 일수를 뽑는다(INTERVAL '90' DAY, DATEADD(day,-90,...), -90 등).

    개월/주 단위는 일수로 환산(개월=30, 주=7)한다. 방향(부호)은 여기서 판단하지 않는다."""
    t = str(text).lower()
    m = re.search(r"interval\s*'?(\d+)'?\s*(day|days|month|months|week|weeks)?", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2) or "day"
        return n * (30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
    m = re.search(r"dateadd\s*\(\s*(day|month|week|dd|mm|wk)\s*,\s*-?(\d+)", t)
    if m:
        n = int(m.group(2))
        unit = m.group(1)
        return n * (30 if unit in ("month", "mm") else 7 if unit in ("week", "wk") else 1)
    m = re.search(r"-\s*(\d+)\b", t)
    if m:
        return int(m.group(1))
    return None


# ------------------------------------------------------------------ 개별 매칭

def _matching_filters(req: Requirement, semantics: SqlSemantics) -> list[Filter]:
    """요구 필드와 같은 필드를 가진 SQL 필터(위치 무관: WHERE/JOIN/HAVING/서브쿼리)."""
    target = _short_field(req.field)
    return [f for f in semantics.filters if _short_field(f.normalized_field) == target]


def _operator_direction(op: str | None) -> str | None:
    """연산자를 방향 부류로 정규화. 값 동치 비교에 쓴다."""
    return op


def _compare_filter(req: Requirement, f: Filter) -> tuple[str, str] | None:
    """요구(연산자+값)와 SQL 필터를 비교해 (status, reason_code) 를 돌린다. 무관하면 None.

    matched: 연산자·값 동일. equivalent: 방향 동일+값 동일(부호 흡수). contradicted: 방향 반대 또는 값 상이."""
    req_op = req.operator
    sql_op = f.normalized_operator
    if req_op is None or sql_op is None:
        return None
    # NULL 처리 요구.
    if req_op in ("is_null", "is_not_null"):
        if sql_op == req_op:
            return ("matched", "null_check_matched")
        if sql_op in ("is_null", "is_not_null"):
            return ("contradicted", "null_check_inverted")
        return None
    rv, sv = _num(req.value), _num(f.normalized_value)
    # 극성(부정) 반영: SQL 필터가 상위 NOT 로 감싸졌으면 방향을 반전해 본다.
    effective_sql_op = _negate_operator(sql_op) if f.negated else sql_op
    if rv is not None and sv is not None:
        if req_op == effective_sql_op and rv == sv:
            return ("matched", "filter_exact")
        if _same_direction(req_op, effective_sql_op) and rv == sv:
            return ("equivalent", "filter_equivalent_direction")
        if _opposite_direction(req_op, effective_sql_op) and rv == sv:
            return ("contradicted", "filter_direction_inverted")
        if req_op == effective_sql_op and rv != sv:
            # 같은 방향인데 값이 다름: 날짜 창이면 별도 판정, 아니면 값 오류.
            return ("contradicted", "filter_value_mismatch")
        return None
    # 비수치(문자/코드/불리언) 등호 비교.
    if req_op in ("=", "!=") and effective_sql_op in ("=", "!="):
        same_val = _values_equal(req.value, f.normalized_value)
        if req_op == effective_sql_op:
            return ("matched", "filter_exact") if same_val else None
        return ("contradicted", "filter_polarity_inverted") if same_val else None
    return None


def _values_equal(a: Any, b: Any) -> bool:
    def norm(x: Any) -> str:
        s = str(x).strip().strip("'\"").lower()
        return s
    if isinstance(a, bool) or isinstance(b, bool):
        truthy = {"true", "1", "y", "yes"}
        return (str(a).lower() in truthy) == (str(b).lower() in truthy)
    return norm(a) == norm(b)


_DIRECTION = {">": "up", ">=": "up", "<": "down", "<=": "down", "=": "eq", "!=": "ne"}


def _same_direction(a: str, b: str) -> bool:
    return _DIRECTION.get(a) == _DIRECTION.get(b) and _DIRECTION.get(a) in ("up", "down", "eq")


def _opposite_direction(a: str, b: str) -> bool:
    pairs = {("up", "down"), ("down", "up"), ("eq", "ne"), ("ne", "eq")}
    return (_DIRECTION.get(a), _DIRECTION.get(b)) in pairs


def _negate_operator(op: str) -> str:
    return {">": "<=", ">=": "<", "<": ">=", "<=": ">", "=": "!=", "!=": "=",
            "is_null": "is_not_null", "is_not_null": "is_null"}.get(op, op)


# ------------------------------------------------------------------ 요구 유형별 판정

def _check_categorical_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    """범주형(코드/등급/권역) 요구: 값 확장의 완전성은 판정하지 않고(§4) 해당 컬럼에 등호/IN 필터가
    존재하는지(+극성)만 본다. '여성→GENDER_CD IN(...)' 처럼 코드로 확장돼도 컬럼만 맞으면 반영된 것."""
    target = _short_field(req.field)
    eq_filters = [f for f in semantics.filters
                  if _short_field(f.normalized_field) == target
                  and f.normalized_operator in ("=", "!=", "in", "like")]
    in_conds = [i for i in semantics.in_conditions
                if _short_field(i.field) == target and not i.subquery]
    positive = [f for f in eq_filters if (f.normalized_operator in ("=", "in", "like")) != f.negated]
    negative = [f for f in eq_filters if (f.normalized_operator in ("=", "in", "like")) == f.negated
                and f.normalized_operator != "like"]
    positive += [i for i in in_conds if not i.negated]
    negative += [i for i in in_conds if i.negated]
    if req.negated:
        positive, negative = negative, positive
    if positive:
        ev = [getattr(p, "expression", str(p)) for p in positive]
        return RequirementCheck(req.id, "equivalent", ev, "categorical_column_present",
                                f"'{req.field}' 범주 조건이 코드/집합 필터로 반영됨(값 확장 완전성은 미판정)")
    if negative:
        ev = [getattr(n, "expression", str(n)) for n in negative]
        return RequirementCheck(req.id, "contradicted", ev, "categorical_polarity_inverted",
                                f"'{req.field}' 범주 조건의 극성이 반대로 반영됨")
    return RequirementCheck(req.id, "missing", [], "categorical_column_absent",
                            f"'{req.field}' 범주 컬럼 필터가 SQL 에 없음")


def _check_filter_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    if req.categorical:
        return _check_categorical_requirement(req, semantics)
    candidates = _matching_filters(req, semantics)
    if not candidates:
        # 필드가 SQL 에 아예 없음 → 누락.
        return RequirementCheck(req.id, "missing", [], "filter_field_absent",
                                f"'{req.field}' 조건이 SQL 어디에도(WHERE/JOIN/HAVING/서브쿼리) 없음")
    best: tuple[str, str, Filter] | None = None
    rank = {"matched": 3, "equivalent": 2, "contradicted": 1}
    for f in candidates:
        verdict = _compare_filter(req, f)
        if verdict is None:
            continue
        status, code = verdict
        if best is None or rank.get(status, 0) > rank.get(best[0], 0):
            best = (status, code, f)
    if best is None:
        # 같은 필드가 있으나 연산자/값 대응을 규칙으로 못 지음 → 모호(리뷰).
        ev = [c.expression for c in candidates]
        return RequirementCheck(req.id, "ambiguous", ev, "filter_operator_unresolved",
                                f"'{req.field}' 필드는 있으나 연산자/값 대응을 규칙으로 확정 못 함")
    status, code, f = best
    reason = {
        "matched": "연산자·값이 동일하게 반영됨",
        "equivalent": "표현은 다르나 방향·값이 동등(범위/부호 흡수)",
        "contradicted": "원문과 방향/값이 반대로 반영됨",
    }.get(status, "")
    return RequirementCheck(req.id, status, [f.expression], code, reason)


def _check_date_window_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    """날짜 창 요구(최근 N일 등). 필드의 날짜 필터에서 창 일수·방향을 뽑아 비교한다."""
    candidates = [f for f in _matching_filters(req, semantics) if f.is_date_window] or \
                 [f for f in semantics.filters if f.is_date_window and _short_field(f.normalized_field) == _short_field(req.field)]
    # EXISTS/서브쿼리 안의 날짜 필터도 포함.
    for ec in semantics.exists_conditions:
        for f in ec.filters:
            if f.is_date_window and _short_field(f.normalized_field) == _short_field(req.field):
                candidates.append(f)
    if not candidates:
        return RequirementCheck(req.id, "missing", [], "date_window_absent",
                                f"'{req.field}' 날짜 창 조건이 SQL 에 없음")
    want = req.window_days
    # 같은 컬럼에 여러 날짜 창(기본 세그먼트 90일 + 명시 30일 등)이 함께 올 수 있다. 하나라도 요구 창과
    # 맞으면 반영된 것(equivalent)으로 본다 — 첫 후보 불일치로 성급히 contradicted 하지 않는다.
    parsed_days = [(_interval_days(f.expression), f) for f in candidates]
    if want is None or all(d is None for d, _ in parsed_days):
        return RequirementCheck(req.id, "ambiguous", [f.expression for f in candidates],
                                "date_window_unparsed", "날짜 창 일수를 규칙으로 확정 못 함(표현 확인 필요)")
    for got, f in parsed_days:
        if got == want:
            return RequirementCheck(req.id, "equivalent", [f.expression], "date_window_matched",
                                    f"최근 {want}일 창이 동등하게 반영됨")
    diffs = [(got, f) for got, f in parsed_days if got is not None]
    got, f = diffs[0]
    return RequirementCheck(req.id, "contradicted", [f.expression], "date_window_mismatch",
                            f"요구 {want}일과 다른 {got}일 창만 반영됨")


def _check_membership_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    """behavior/존재 요구(구매 이력 있음/없음 등). EXISTS/JOIN/IN(서브쿼리) 중 무엇이든 대응하면 인정(§4).

    req.type == 'membership'(존재) 또는 'not_membership'(부재). negated 로 극성 표현."""
    want_negated = req.type == "not_membership" or req.negated
    target_tables = _membership_target_tables(req)

    # 1) EXISTS/NOT EXISTS.
    for ec in semantics.exists_conditions:
        if target_tables and not (set(ec.tables) & target_tables):
            continue
        if ec.negated == want_negated:
            kind = "부재(anti-join)" if want_negated else "존재"
            return RequirementCheck(req.id, "equivalent", [ec.expression], "membership_exists",
                                    f"{kind} 조건이 EXISTS/anti-join 으로 반영됨")
        return RequirementCheck(req.id, "contradicted", [ec.expression], "membership_polarity_inverted",
                                "존재/부재 극성이 반대로 반영됨")

    # 2) IN / NOT IN(서브쿼리). NULL semantics 주의: NOT IN 서브쿼리는 동치 확정하지 않고 review.
    for ic in semantics.in_conditions:
        if not ic.subquery:
            continue
        if target_tables and not (set(ic.tables) & target_tables):
            continue
        if ic.negated == want_negated:
            if ic.negated:
                return RequirementCheck(req.id, "ambiguous", [ic.expression], "not_in_null_semantics",
                                        "NOT IN 서브쿼리는 NULL 값에 따라 NOT EXISTS 와 결과가 달라질 수 있어 확인 필요")
            return RequirementCheck(req.id, "equivalent", [ic.expression], "membership_in_subquery",
                                    "존재 조건이 IN 서브쿼리로 반영됨")
        return RequirementCheck(req.id, "contradicted", [ic.expression], "membership_polarity_inverted",
                                "존재/부재 극성이 반대로 반영됨")

    # 3) JOIN(존재만; anti-join 은 보통 LEFT JOIN ... IS NULL 이라 여기선 미확정).
    if not want_negated:
        for j in semantics.joins:
            if target_tables and j.table not in target_tables:
                continue
            if j.kind in ("inner", "join", ""):
                return RequirementCheck(req.id, "equivalent", [f"JOIN {j.table} ON {j.on}"],
                                        "membership_join", "존재 조건이 INNER JOIN 으로 반영됨")
        # 4) 팩트 테이블이 FROM 기준 테이블로 조인돼 들어온 경우(예: FROM cart JOIN member): 조인 대상이
        #    아니라 기준 테이블이라 위 루프에 안 걸린다. 회원 쿼리에 팩트가 참조되고 조인이 있으면 존재로 본다.
        if target_tables and (target_tables & set(semantics.tables)) and semantics.joins:
            return RequirementCheck(req.id, "equivalent", sorted(target_tables & set(semantics.tables)),
                                    "membership_referenced_table",
                                    "존재 조건이 팩트 테이블 조인(FROM 기준)으로 반영됨")

    return RequirementCheck(req.id, "missing", [], "membership_absent",
                            f"'{req.field}' {'부재' if want_negated else '존재'} 조건이 SQL 에 없음")


def _membership_target_tables(req: Requirement) -> set[str]:
    """요구가 지목하는 팩트 테이블 후보(있으면 EXISTS/JOIN 매칭을 좁힌다). value 에 테이블 힌트가 있으면 사용."""
    hint = req.metric or req.value
    if isinstance(hint, str) and hint:
        return {hint.split(".")[0].lower()}
    return set()


def _check_aggregate_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    """집계 요구(구매 N회 이상 등). SQL 의 HAVING/집계 함수와 func·연산자·임계를 비교한다."""
    want_func = (req.aggregate_func or "").lower()
    matches = [a for a in semantics.aggregates
               if not want_func or a.func == want_func or _agg_func_equiv(a.func, want_func)]
    if not matches:
        if semantics.aggregates:
            return RequirementCheck(req.id, "ambiguous",
                                    [a.expression for a in semantics.aggregates],
                                    "aggregate_func_mismatch",
                                    f"요구 집계({want_func})와 다른 집계만 존재")
        return RequirementCheck(req.id, "missing", [], "aggregate_absent",
                                f"집계 조건({req.metric or want_func})이 SQL 에 없음")
    want_val = _num(req.value)
    for a in matches:
        if a.operator is None or want_val is None:
            return RequirementCheck(req.id, "ambiguous", [a.expression], "aggregate_threshold_unparsed",
                                    "집계 임계 비교를 규칙으로 확정 못 함")
        got = _num(a.value)
        if got is None:
            return RequirementCheck(req.id, "ambiguous", [a.expression], "aggregate_threshold_unparsed",
                                    "집계 임계값 파싱 불가")
        if req.operator == a.operator and got == want_val:
            return RequirementCheck(req.id, "matched", [a.expression], "aggregate_matched",
                                    "집계 함수·연산자·임계가 동일하게 반영됨")
        if _same_direction(req.operator or "", a.operator) and got == want_val:
            return RequirementCheck(req.id, "equivalent", [a.expression], "aggregate_equivalent",
                                    "집계 방향·임계가 동등하게 반영됨")
        if _opposite_direction(req.operator or "", a.operator) or got != want_val:
            return RequirementCheck(req.id, "contradicted", [a.expression], "aggregate_mismatch",
                                    f"집계 임계/방향이 요구({req.operator}{want_val})와 다름")
    return RequirementCheck(req.id, "ambiguous", [m.expression for m in matches],
                            "aggregate_unresolved", "집계 대응 확정 불가")


def _agg_func_equiv(a: str, b: str) -> bool:
    groups = [{"count", "count_distinct"}]
    return any(a in g and b in g for g in groups)


def _check_dedup_requirement(req: Requirement, semantics: SqlSemantics) -> RequirementCheck:
    """중복 제거 요구(deduplication_key). DISTINCT 또는 GROUP BY 로 그 키의 유일성이 보장되면 동등(§4)."""
    key = _short_field(req.field)
    dedup = {_short_field(k) for k in semantics.deduplication_keys}
    if key in dedup:
        return RequirementCheck(req.id, "equivalent", sorted(semantics.deduplication_keys),
                                "dedup_guaranteed", f"'{req.field}' 유일성이 DISTINCT/GROUP BY 로 보장됨")
    if not semantics.deduplication_keys:
        return RequirementCheck(req.id, "missing", [], "dedup_absent",
                                f"'{req.field}' 중복 제거(DISTINCT/GROUP BY)가 SQL 에 없음")
    return RequirementCheck(req.id, "contradicted", sorted(semantics.deduplication_keys),
                            "dedup_key_mismatch",
                            f"중복 제거 기준이 요구('{req.field}')와 다름: {semantics.deduplication_keys}")


def _check_exclusion(exc: Exclusion, semantics: SqlSemantics) -> RequirementCheck:
    """필수 제외 조건이 SQL 에 반영됐는지. 제외는 '해당 값을 배제'하는 필터/anti-join 으로 나타난다."""
    target = _short_field(exc.field)
    # 제외 = 그 필드에 대한 반대 극성 필터. 예: opt_out=true 제외 → SQL 에 opt_out != true / = false / IS NULL.
    for f in semantics.filters:
        if _short_field(f.normalized_field) != target:
            continue
        op = _negate_operator(f.normalized_operator) if f.negated else f.normalized_operator
        # 제외 대상 값을 걸러내는 방향이면 반영된 것.
        if exc.operator in ("=", "is_not_null") and op in ("!=", "is_null", "="):
            if op == "=" and _values_equal(exc.value, f.normalized_value):
                # 오히려 제외 대상을 '포함'하고 있음 → 위반.
                return RequirementCheck(exc.id, "contradicted", [f.expression],
                                        "exclusion_includes_forbidden",
                                        f"제외 대상('{exc.field}={exc.value}')을 오히려 포함함")
            return RequirementCheck(exc.id, "equivalent", [f.expression], "exclusion_applied",
                                    f"제외 조건('{exc.field}')이 반영됨")
    # anti-join / NOT EXISTS 로 제외했을 수 있음.
    for ec in semantics.exists_conditions:
        if ec.negated and (target in " ".join(ec.tables) or any(target in c for c in ec.correlated_on)):
            return RequirementCheck(exc.id, "equivalent", [ec.expression], "exclusion_anti_join",
                                    f"제외 조건('{exc.field}')이 anti-join 으로 반영됨")
    return RequirementCheck(exc.id, "missing", [], "exclusion_absent",
                            f"필수 제외 조건('{exc.field}')이 SQL 에 없음")


# ------------------------------------------------------------------ 오케스트레이션(규칙 단계)

def map_requirements(spec: TargetSpecification, semantics: SqlSemantics) -> list[RequirementCheck]:
    """모든 요구사항/제외를 SQL 의미에 매핑해 RequirementCheck 목록을 만든다(규칙 단계, LLM 미포함)."""
    checks: list[RequirementCheck] = []
    for req in spec.requirements:
        if req.type == "date_window":
            checks.append(_check_date_window_requirement(req, semantics))
        elif req.type in ("membership", "not_membership"):
            checks.append(_check_membership_requirement(req, semantics))
        elif req.type == "aggregate":
            checks.append(_check_aggregate_requirement(req, semantics))
        elif req.type == "dedup":
            checks.append(_check_dedup_requirement(req, semantics))
        else:  # filter
            checks.append(_check_filter_requirement(req, semantics))
    for exc in spec.exclusions:
        checks.append(_check_exclusion(exc, semantics))
    return checks


def detect_extra_restrictions(spec: TargetSpecification, semantics: SqlSemantics) -> list[str]:
    """요구에 없는데 결과 집합을 축소하는 추가 제한(불필요한 필터)을 찾는다(§3 extra_restrictions).

    보수적으로: 요구사항이 참조하지 않은 필드에 대한 '값 한정' WHERE 필터만 후보로 든다(날짜/조인/라벨/
    팩트테이블 조건/계산식 가드 제외). 팩트 테이블(cart/orders/campaign) 컬럼과 계산식(LENGTH/CAST 가드)은
    오디언스 축소가 아니라 멤버십/포맷 가드이므로 제외해 오탐(불필요한 review)을 줄인다."""
    req_fields = {_short_field(r.field) for r in spec.requirements} | {_short_field(e.field) for e in spec.exclusions}
    # 멤버십/부재 요구가 지목하는 팩트 테이블(값에 실테이블명). 그 테이블 컬럼 조건은 조인/팩트 조건이다.
    fact_tables = {str(r.value).split(".")[0].lower() for r in spec.requirements
                   if r.type in ("membership", "not_membership") and isinstance(r.value, str)}
    extras: list[str] = []
    for f in semantics.filters:
        if f.location != "where" or f.is_date_window:
            continue
        if f.normalized_operator in ("is_null", "is_not_null"):
            continue  # 널 가드는 결과 축소 자문이 아님(보수적 제외)
        norm = str(f.normalized_field or "")
        if "(" in norm or ")" in norm:
            continue  # 계산식 가드(LENGTH/CAST 등)는 오디언스 축소가 아님
        table = norm.split(".")[0].lower() if "." in norm else ""
        if table and table in fact_tables:
            continue  # 팩트 테이블 컬럼(KEEP_YN 등)은 멤버십/조인 조건
        field = _short_field(f.normalized_field)
        if field and field not in req_fields:
            # 조인 상관 컬럼 비교(col=col)는 제한이 아니라 조인 조건.
            if f.normalized_value and "." in str(f.normalized_value):
                continue
            extras.append(f.expression)
    return extras


# ------------------------------------------------------------------ 판정(§6)

def decide(
    spec: TargetSpecification,
    checks: list[RequirementCheck],
    *,
    parser_errors: list[str] | None = None,
    policy_violations: list[PolicyViolation] | None = None,
    extra_restrictions: list[str] | None = None,
    execution_assertions: list[ExecutionAssertion] | None = None,
    evidence_version_mismatch: bool = False,
) -> ValidationResult:
    """pass/review/fail 판정(§6). fail 은 구체적 근거(§6 fail 조건)가 있을 때만.

    - fail: 필수 요구 missing/contradicted, 필수 제외 누락, 정책 위반, 실행 assertion fail 등 구체 근거.
    - review: 모호/파서 한계/근거 부족/동치 미확정/버전 불일치/데이터 없음.
    - pass: 필수 요구 전부 matched|equivalent, 정책 위반 없음, 결과 축소 추가 제한 없음.
    """
    parser_errors = parser_errors or []
    policy_violations = policy_violations or []
    extra_restrictions = extra_restrictions or []
    execution_assertions = execution_assertions or []

    by_id = {c.requirement_id: c for c in checks}
    required_ids = {r.id for r in spec.requirements if r.required} | {e.id for e in spec.exclusions}

    missing = [cid for cid in required_ids if by_id.get(cid) and by_id[cid].status == "missing"]
    contradicted = [cid for cid in required_ids if by_id.get(cid) and by_id[cid].status == "contradicted"]
    ambiguous = [c.requirement_id for c in checks if c.status == "ambiguous"]
    partial = [cid for cid in required_ids if by_id.get(cid) and by_id[cid].status == "partially_matched"]

    reason_codes: list[str] = []
    exec_fail = [a for a in execution_assertions if a.status == "fail"]
    exec_skipped = [a for a in execution_assertions if a.status == "skipped"]

    # --- fail: 구체적 근거가 있는 경우에만 ---
    status: str
    if contradicted:
        status = "fail"
        reason_codes += [f"contradicted:{cid}" for cid in contradicted]
    elif missing:
        status = "fail"
        reason_codes += [f"missing_required:{cid}" for cid in missing]
    elif policy_violations:
        status = "fail"
        reason_codes += [f"policy:{p.code}" for p in policy_violations]
    elif exec_fail:
        status = "fail"
        reason_codes += [f"execution:{a.name}" for a in exec_fail]
    # --- review: 근거 부족/모호/미확정 ---
    elif parser_errors:
        status = "review"
        reason_codes += [f"parser_error:{e}" for e in parser_errors]
    elif evidence_version_mismatch:
        status = "review"
        reason_codes.append("rag_evidence_version_mismatch")
    elif ambiguous or partial:
        status = "review"
        reason_codes += [f"ambiguous:{cid}" for cid in ambiguous]
        reason_codes += [f"partial:{cid}" for cid in partial]
    elif spec.ambiguous_requirements:
        status = "review"
        reason_codes += [f"ambiguous_spec:{cid}" for cid in spec.ambiguous_requirements]
    elif exec_skipped and not any(a.status == "pass" for a in execution_assertions):
        # 데이터가 없어 결과 검증을 못 함 → review(§9).
        status = "review"
        reason_codes.append("execution_skipped_no_data")
    elif extra_restrictions:
        # 결과를 축소하는 불필요한 추가 제한 → 확인 필요(리뷰). 단독 fail 아님(§6 pass 조건 위반).
        status = "review"
        reason_codes += [f"extra_restriction:{r}" for r in extra_restrictions]
    else:
        status = "pass"

    confidence = _confidence(checks, status, parser_errors, policy_violations)
    return ValidationResult(
        status=status,  # type: ignore[arg-type]
        checks=checks,
        missing_requirements=missing,
        extra_restrictions=extra_restrictions,
        ambiguous_requirements=sorted(set(ambiguous) | set(spec.ambiguous_requirements)),
        policy_violations=policy_violations,
        confidence=confidence,
        parser_errors=parser_errors,
        execution_assertions=execution_assertions,
        reason_codes=reason_codes,
    )


def _confidence(checks: list[RequirementCheck], status: str,
                parser_errors: list[str], policy_violations: list[PolicyViolation]) -> float:
    if not checks:
        return 0.5
    weight = {"matched": 1.0, "equivalent": 0.9, "not_applicable": 1.0,
              "partially_matched": 0.5, "ambiguous": 0.4, "missing": 0.0, "contradicted": 0.0}
    score = sum(weight.get(c.status, 0.3) for c in checks) / len(checks)
    if parser_errors:
        score *= 0.8
    if policy_violations:
        score *= 0.5
    return max(0.0, min(1.0, score))


def evaluate(
    spec: TargetSpecification,
    semantics: SqlSemantics | None,
    *,
    parser_errors: list[str] | None = None,
    policy_violations: list[PolicyViolation] | None = None,
    execution_assertions: list[ExecutionAssertion] | None = None,
    evidence_version_mismatch: bool = False,
) -> ValidationResult:
    """규칙 기반 전체 평가: 매핑 → 추가제한 탐지 → 판정. 파서 실패면 semantics=None 으로 호출."""
    if semantics is None:
        return decide(spec, [], parser_errors=parser_errors or ["ast_unavailable"],
                      policy_violations=policy_violations,
                      execution_assertions=execution_assertions,
                      evidence_version_mismatch=evidence_version_mismatch)
    checks = map_requirements(spec, semantics)
    extras = detect_extra_restrictions(spec, semantics)
    return decide(spec, checks, parser_errors=parser_errors,
                  policy_violations=policy_violations, extra_restrictions=extras,
                  execution_assertions=execution_assertions,
                  evidence_version_mismatch=evidence_version_mismatch)
