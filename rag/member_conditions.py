"""Pure compilation of member targeting conditions.

Physical schema bindings and graph-owned strategies are supplied explicitly by
:class:`MemberConditionContext`. This module intentionally never imports
`graph_rag`, configuration loaders, or runtime registries.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


DIMENSION_OPERATOR_SQL_MAP: dict[str, str] = {
    "IN": "IN",
    "NOT_IN": "NOT IN",
}


class SqlDialect(Protocol):
    def parse_char8(self, expression: str) -> str: ...
    def str_len(self, expression: str) -> str: ...
    def char8_cutoff(self, days: int, anchor: str | None = None) -> str: ...
    def char8_valid(self, column: str) -> str: ...


class MemberEqualsPredicate(Protocol):
    def __call__(self, canonical: str, negate: bool = False) -> str | None: ...


class CampaignResponsePredicate(Protocol):
    def __call__(
        self,
        predicate: str,
        negated: bool = False,
        source: str | None = None,
    ) -> str: ...


class WindowPredicate(Protocol):
    def __call__(
        self,
        days: int | None,
        *,
        window: Any = None,
    ) -> str: ...


ConditionValidator = Callable[[Mapping[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class MemberConditionContext:
    """Graph-owned dependencies required by the pure member compiler."""

    configuration_error: str | None
    member_eq_filters: Mapping[str, tuple[str, str, str]]
    member_activity_filters: Mapping[str, int]
    gender_terms: Collection[str]
    dimension_operator_sql_map: Mapping[str, str]
    member_table: str
    member_alias: str
    member_age_column: str
    member_region_columns: Collection[str]
    reference_date_sql: str | None
    resolve_plan_lifecycle_aliases: Callable[[dict[str, Any]], dict[str, Any]]
    validate_dimension_filters: ConditionValidator
    validate_compound_dimension_filters: ConditionValidator
    compile_compound_dimension_filter: Callable[[Mapping[str, Any]], str]
    dimension_filter_operator: Callable[[Mapping[str, Any]], str | None]
    member_eq_predicate: MemberEqualsPredicate
    member_activity_predicate: Callable[[int], str]
    member_recent_login_predicate: Callable[[int], str]
    member_birthday_predicate: Callable[[str], str]
    format_threshold: Callable[[Any], str]
    campaign_response_exists_predicate: CampaignResponsePredicate
    coupon_usage_threshold_predicate: Callable[[dict[str, Any]], str | None]
    cart_absence_predicate: Callable[[], str]
    cart_quantity_missing_predicate: Callable[[], str]
    purchase_membership_needs_own_predicate: Callable[[Any], bool]
    purchase_membership_predicate: WindowPredicate
    purchase_inactivity_predicate: WindowPredicate
    member_signup_predicate: Callable[[int | None], str]
    member_region_predicates: Callable[[dict[str, list[str]]], list[str]]
    sql_quote: Callable[[Any], str]
    unique_strings: Callable[[list[str]], list[str]]


def dimension_filter_operator(
    dimension_filter: Mapping[str, Any],
) -> str | None:
    """Validate a dimension-filter operator against the closed SQL enum."""

    raw = dimension_filter.get("operator")
    operator = "IN" if raw is None else str(raw).strip().upper()
    return operator if operator in DIMENSION_OPERATOR_SQL_MAP else None


def resolve_plan_lifecycle_aliases(
    slots: dict[str, Any],
    *,
    raw_aliases: Any,
    member_eq_filters: Mapping[str, tuple[str, str, str]],
    member_activity_filters: Mapping[str, int],
    resolve_name_choice: Callable[[str, str], str | None],
) -> dict[str, Any]:
    """Return an immutable-style copy with lifecycle aliases resolved."""

    values = slots.get("lifecycle") if isinstance(slots, dict) else None
    if not isinstance(values, list) or not values:
        return slots if isinstance(slots, dict) else {}

    compilable = frozenset(member_eq_filters) | frozenset(member_activity_filters)
    aliases = {
        alias: canonical
        for alias, canonical in (
            raw_aliases.items() if isinstance(raw_aliases, Mapping) else ()
        )
        if isinstance(alias, str)
        and isinstance(canonical, str)
        and canonical in compilable
    }

    def resolve(value: str) -> str:
        if value in aliases:
            return aliases[value]
        if value in compilable:
            return value
        picked = resolve_name_choice("lifecycle_canonical", value)
        return picked if picked and picked in compilable else value

    mapped = [resolve(value) if isinstance(value, str) else value for value in values]
    if mapped == list(values):
        return slots

    unique: list[Any] = []
    for value in mapped:
        if value and value not in unique:
            unique.append(value)
    return {**slots, "lifecycle": unique}


def member_birthday_predicate(
    granularity: str,
    *,
    alias: str,
    binding: Mapping[str, Any],
    dialect: SqlDialect,
    reference_date_sql: str,
) -> str:
    """Compile a configured birthday month/day comparison."""

    column = str(binding["column"])
    length = 2 if granularity == "month" else 4
    qualified = f"{alias}.{column}"
    return (
        f"({dialect.char8_valid(qualified)} "
        f"AND SUBSTRING({qualified}, 5, {length}) = "
        f"SUBSTRING({reference_date_sql}, 5, {length}))"
    )


def member_signup_predicate(
    days: int | None,
    *,
    alias: str,
    binding: Mapping[str, Any],
    dialect: SqlDialect,
    cutoff_sql: str,
) -> str:
    """Compile a configured signup recency comparison."""

    column = str(binding["column"])
    table = str(binding["table"])
    if not isinstance(days, int) or days <= 0:
        days = binding["default_days"]
    qualified = f"{alias}.{column}"
    if binding.get("anchor") == "data_max":
        anchor = dialect.parse_char8(
            f"(SELECT MAX({column}) FROM {table} "
            f"WHERE {dialect.str_len(column)} = 8)"
        )
        boundary = dialect.char8_cutoff(days, anchor)
    elif binding.get("anchor") == "reference_date":
        boundary = cutoff_sql
    else:
        raise ValueError("signup anchor must be data_max or reference_date")
    return f"({dialect.char8_valid(qualified)} AND {qualified} >= {boundary})"


def member_recent_login_predicate(
    days: int,
    *,
    alias: str,
    binding: Mapping[str, Any],
    dialect: SqlDialect,
    cutoff_sql: str,
) -> str:
    """Compile a configured positive recent-login window."""

    column = str(binding["column"])
    table = str(binding["table"])
    qualified = f"{alias}.{column}"
    if binding.get("anchor") == "data_max":
        anchor = dialect.parse_char8(
            f"(SELECT MAX({column}) FROM {table} "
            f"WHERE {dialect.str_len(column)} = 8)"
        )
        boundary = dialect.char8_cutoff(days, anchor)
    elif binding.get("anchor") == "reference_date":
        boundary = cutoff_sql
    else:
        raise ValueError("recent-login anchor must be data_max or reference_date")
    return f"({dialect.char8_valid(qualified)} AND {qualified} >= {boundary})"


def validate_compound_dimension_filters(
    query_plan: Mapping[str, Any],
    *,
    member_table: str,
    allowed_columns: Collection[str],
) -> list[dict[str, Any]]:
    """Validate the closed OR-of-AND grammar for member region filters."""

    errors: list[dict[str, Any]] = []
    allowed = frozenset(column.upper() for column in allowed_columns if column)
    for outer_index, compound in enumerate(
        query_plan.get("compound_dimension_filters") or []
    ):
        path = f"compound_dimension_filters.{outer_index}"
        if not isinstance(compound, Mapping) or compound.get("logic") != "OR":
            errors.append({
                "code": "COMPOUND_FILTER_INVALID",
                "path": path,
                "message": "compound filter must be an OR object",
            })
            continue
        groups = compound.get("groups")
        if not isinstance(groups, list) or not groups:
            errors.append({
                "code": "COMPOUND_FILTER_EMPTY",
                "path": path,
                "message": "compound filter must contain groups",
            })
            continue
        for group_index, group in enumerate(groups):
            group_path = f"{path}.groups.{group_index}"
            if not isinstance(group, Mapping) or group.get("logic") != "AND":
                errors.append({
                    "code": "COMPOUND_GROUP_INVALID",
                    "path": group_path,
                    "message": "compound group must be an AND object",
                })
                continue
            filters = group.get("filters")
            if not isinstance(filters, list) or not filters:
                errors.append({
                    "code": "COMPOUND_GROUP_EMPTY",
                    "path": group_path,
                    "message": "compound group must contain filters",
                })
                continue
            seen_columns: set[str] = set()
            for filter_index, item in enumerate(filters):
                item_path = f"{group_path}.filters.{filter_index}"
                if not isinstance(item, Mapping):
                    errors.append({
                        "code": "COMPOUND_ITEM_INVALID",
                        "path": item_path,
                        "message": "compound filter item must be an object",
                    })
                    continue
                table = str(item.get("table") or "")
                column = str(item.get("column") or "").split(".")[-1].upper()
                value = item.get("value")
                if table != member_table or column not in allowed:
                    errors.append({
                        "code": "COMPOUND_COLUMN_UNSUPPORTED",
                        "path": item_path,
                        "message": (
                            "compound region filter must use member region columns"
                        ),
                    })
                if item.get("operator") != "=":
                    errors.append({
                        "code": "COMPOUND_OPERATOR_UNSUPPORTED",
                        "path": item_path,
                        "message": "compound region filter only supports equality",
                    })
                if not isinstance(value, str) or not value:
                    errors.append({
                        "code": "COMPOUND_VALUE_INVALID",
                        "path": item_path,
                        "message": "compound filter value must be non-empty",
                    })
                if column in seen_columns:
                    errors.append({
                        "code": "COMPOUND_COLUMN_DUPLICATE",
                        "path": item_path,
                        "message": "a compound group cannot repeat a region column",
                    })
                seen_columns.add(column)
    return errors


def compile_compound_dimension_filter(
    compound: Mapping[str, Any],
    *,
    base_alias: str,
    quote_literal: Callable[[Any], str],
) -> str:
    groups: list[str] = []
    for group in compound.get("groups") or []:
        predicates = []
        for item in group.get("filters") or []:
            column = str(item["column"]).split(".")[-1].upper()
            predicates.append(
                f"{base_alias}.{column} = {quote_literal(str(item['value']))}"
            )
        groups.append("(" + " AND ".join(predicates) + ")")
    return "(" + " OR ".join(groups) + ")"


def compile_member_region_filter(
    region_codes: dict[str, list[str]],
    *,
    base_alias: str,
    sido_column: str,
    sigungu_column: str,
    sigungu_to_sido: Mapping[str, Collection[str]],
    quote_literal: Callable[[Any], str],
) -> list[str]:
    """Choose AND for hierarchical regions and OR for unrelated region lists."""

    if not region_codes:
        return []

    def in_predicate(column: str) -> str:
        values = ", ".join(quote_literal(code) for code in region_codes[column])
        return f"{base_alias}.{column} IN ({values})"

    sido_codes = region_codes.get(sido_column)
    sigungu_codes = region_codes.get(sigungu_column)
    if not sido_codes or not sigungu_codes:
        return [in_predicate(column) for column in region_codes]

    hierarchical = bool(sigungu_to_sido) and all(
        any(sido in sido_codes for sido in sigungu_to_sido.get(sigungu, ()))
        for sigungu in sigungu_codes
    )
    if hierarchical or not sigungu_to_sido:
        return [in_predicate(sido_column), in_predicate(sigungu_column)]
    return [
        "("
        + in_predicate(sido_column)
        + " OR "
        + in_predicate(sigungu_column)
        + ")"
    ]


def compile_member_target_conditions(
    query_plan: dict[str, Any],
    context: MemberConditionContext,
) -> dict[str, Any]:
    """query_plan 의 타겟 조건을 실회원 테이블(CRM_MB_BASEINFO) 술어로 컴파일한다.

    조건 -> 실컬럼 매핑을 한곳(context.member_eq_filters/context.member_activity_filters)에서 조회하므로 지원 속성의
    어떤 조합(포함/제외/연령 …)도 자동으로 술어 목록이 된다. CRM_MB_BASEINFO 단독으로 표현할 수 없는
    조건은 그 경로(path)를 unsupported 에 모은다. 호출부는 unsupported 가 비어있을 때만 실DB SQL 을 쓴다.

    반환 dict: predicates(WHERE 술어), labels(세그먼트 라벨 canonical 값), forces_state(기본 상태필터
    (NORMAL 한정) 해제 여부 — 회원상태 직접 지정 또는 미접속 재활성화 신호), has_signal(회원 대상 신호
    존재), unsupported(미지원 조건 path 목록).
    """
    if context.configuration_error is not None:
        return {
            "predicates": [],
            "labels": [],
            "forces_state": False,
            "has_signal": False,
            "unsupported": ["system.member_target_filters"],
            "configuration_error": "member_target_filters_unavailable",
        }

    target_user = query_plan.get("target_user", {})
    exclude = query_plan.get("exclude", {})
    campaign = query_plan.get("campaign_constraints", {})
    # LLM 어휘 별칭(withdrawn_user 등)을 컴파일 가능한 canonical(withdrawn)로 먼저 해석한다 — 플래너에
    # 허용된 어휘가 컴파일러 매핑보다 넓어, 별칭이 나오면 '미지원 조건'으로 SQL 이 통째로 막혔다.
    # 별칭 표는 설정(lifecycle_aliases)이 소유하므로 새 별칭은 한 줄 추가로 열린다.
    target_user = context.resolve_plan_lifecycle_aliases(target_user)
    exclude = context.resolve_plan_lifecycle_aliases(exclude)
    eq_includes: dict[str, list[str]] = {}  # 실컬럼 -> 포함 저장값들(같은 컬럼은 IN 으로 OR)
    include_categories: set[str] = set()
    other_predicates: list[str] = []  # 제외(<>)/연령/활동 등은 그대로 AND
    # 외부 회원 프로필 조건은 같은 table/alias/grain끼리 한 EXISTS로 묶는다. 서로 다른 월 행에
    # 조건이 나뉘어 참이 되는 오류를 막고, 최신 스냅샷 필터도 한 번만 적용한다.
    profile_predicates: dict[tuple[str, str, str, str, str | None], list[str]] = {}
    labels: list[str] = []
    unsupported: list[str] = []
    unsupported.extend(
        str(error.get("path") or "dimension_filters")
        for error in context.validate_dimension_filters(query_plan)
    )
    unsupported.extend(
        str(error.get("path") or "compound_dimension_filters")
        for error in context.validate_compound_dimension_filters(query_plan)
    )
    has_signal = False
    # 장기 미접속(휴면 재활성화) 신호가 있으면 기본 상태필터(NORMAL 한정)를 해제한다 — "6개월 이상
    # 접속하지 않은 휴면 고객"처럼 미접속=휴면으로 읽는 요청에서 NORMAL 이 붙으면 SLEEP/WITHDRAW 를
    # 배제해 원문("휴면 고객")과 모순되고, 의미검증기가 이를 반전으로 오탐한다. forces_state 로 흡수한다.
    suppresses_default_state = False

    def _add_include(canonical: str) -> None:
        category, column, value = context.member_eq_filters[canonical]
        eq_includes.setdefault(column, [])
        if value not in eq_includes[column]:
            eq_includes[column].append(value)
        include_categories.add(category)

    def _add_profile_predicate(source: Any, predicate: str) -> bool:
        if not isinstance(source, dict):
            return False
        table = source.get("table")
        alias = source.get("alias")
        member_column = source.get("member_column")
        base_member_column = source.get("base_member_column")
        grain_filter = source.get("grain_filter")
        identifiers = (table, alias, member_column, base_member_column)
        if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\.]*", value) for value in identifiers):
            return False
        if grain_filter is not None and not isinstance(grain_filter, str):
            return False
        key = (table, alias, member_column, base_member_column, grain_filter)
        profile_predicates.setdefault(key, []).append(predicate)
        return True

    # 성별(포함/제외)
    gender = target_user.get("gender")
    if gender in context.gender_terms:
        _add_include(gender); labels.append(gender); has_signal = True
    elif gender:
        unsupported.append("target_user.gender")
    for value in exclude.get("gender", []):
        if value in context.gender_terms:
            other_predicates.append(context.member_eq_predicate(value, negate=True)); labels.append("non_" + value); has_signal = True
        else:
            unsupported.append("exclude.gender")

    # 연령. 하·상한이 같으면(정확 연령 "30세인") >=N AND <=N 대신 = N 으로 방출한다 — 깔끔하고,
    # 의미검증 게이트가 '>=N AND <=N'을 '=N'과 다르다고 오탐하는 것도 원천 차단한다.
    age_min = target_user.get("age_min")
    age_max = target_user.get("age_max")
    age_column = context.member_age_column
    if isinstance(age_min, int) and isinstance(age_max, int) and age_min == age_max:
        other_predicates.append(f"{age_column} = {age_min}"); has_signal = True
    else:
        if isinstance(age_min, int):
            other_predicates.append(f"{age_column} >= {age_min}"); has_signal = True
        if isinstance(age_max, int):
            other_predicates.append(f"{age_column} <= {age_max}"); has_signal = True
    # 닫힌 연령 구간 제외("20대가 아닌"). 여집합이 분리 2구간이라 NOT BETWEEN 으로 뺀다(널은 BETWEEN 이 이미 거름).
    for age_range in target_user.get("age_exclude_ranges", []):
        if isinstance(age_range, (list, tuple)) and len(age_range) == 2 and all(isinstance(v, int) for v in age_range):
            lo, hi = age_range
            other_predicates.append(f"NOT ({age_column} BETWEEN {lo} AND {hi})"); has_signal = True

    # lifecycle 포함(등가/활동). 같은 canonical 이 제외에도 있으면 제외가 이긴다 — 포함·제외를 둘 다
    # 컴파일하면 `= 'Y' AND <> 'Y'` 같은 항상-거짓 술어가 되어 결과가 무조건 0명이 된다. 재작성·스코프
    # 분리로 한 표현이 양쪽 슬롯에 들어가는 경우가 있어(예: '블랙리스트 … 아닌 고객') 결정론으로 정리한다.
    excluded_lifecycles = {value for value in exclude.get("lifecycle", []) if isinstance(value, str)}
    for lifecycle in target_user.get("lifecycle", []):
        if lifecycle in excluded_lifecycles:
            continue
        if lifecycle == "new_user":
            continue  # 신규 가입은 아래 signup_target 분기가 REG_DT 창 술어로 처리(미지원 아님)
        if lifecycle in context.member_eq_filters:
            _add_include(lifecycle); labels.append(lifecycle); has_signal = True
        elif lifecycle in context.member_activity_filters:
            other_predicates.append(context.member_activity_predicate(context.member_activity_filters[lifecycle])); labels.append(lifecycle); has_signal = True; suppresses_default_state = True
        else:
            unsupported.append("target_user.lifecycle:" + lifecycle)

    # lifecycle 제외: 등가 필터는 부정(<>), 미접속 활동 필터는 여집합(최근 N일 내 접속)으로 컴파일한다.
    # '휴면(=N일 이상 미접속) 회원 제외'의 여집합은 '최근 N일 내 접속'으로 정확히 정의되므로
    # (context.member_recent_login_predicate 는 미접속 술어의 대칭), 등록된 모든 활동 필터에 같은 규칙이 적용된다.
    for lifecycle in exclude.get("lifecycle", []):
        if lifecycle in context.member_eq_filters:
            _category, column, value = context.member_eq_filters[lifecycle]
            # 같은 컬럼에 등가 포함이 이미 있고 그 값이 제외 값과 다르면 `<>` 는 그 등가 조건에 이미
            # 함축된다(상태='NORMAL' 이면 <>'WITHDRAW' 는 자명). 중복 술어는 결과를 바꾸지 않으면서
            # 의미검증기가 '요청하지 않은 추가 조건'으로 오탐하는 잡음이 되므로 라벨만 남기고 뺀다.
            included_values = eq_includes.get(column) or []
            if not (included_values and value not in included_values):
                other_predicates.append(context.member_eq_predicate(lifecycle, negate=True))
            labels.append("non_" + lifecycle); has_signal = True
        elif lifecycle in context.member_activity_filters:
            other_predicates.append(context.member_recent_login_predicate(context.member_activity_filters[lifecycle]))
            labels.append("non_" + lifecycle); has_signal = True
        else:
            unsupported.append("exclude.lifecycle:" + lifecycle)

    # 미접속 기간(휴면/장기 미접속): LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교 술어로 컴파일한다.
    # 미접속=휴면 재활성화 신호이므로 기본 상태필터(NORMAL 한정)를 해제한다(suppresses_default_state) —
    # "6개월 이상 접속하지 않은 휴면 고객"에 NORMAL 을 붙이면 SLEEP/WITHDRAW 를 배제해 원문과 모순되고
    # 의미검증기가 반전으로 오탐하기 때문. 오디언스는 LAST_LOGIN_DATE 창만으로 정의한다.
    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("min_days"), int):
        other_predicates.append(context.member_activity_predicate(inactivity_period["min_days"])); has_signal = True; suppresses_default_state = True

    # 최근 로그인 창(긍정형 접속): LAST_LOGIN_DATE >= (기준일-N일) 술어. 적재 데이터가 과거라 0명이
    # 나올 수 있어도, 조건 표현이 가능하면 요청 기간을 왜곡하지 않고 무조건 그대로 건다.
    recent_login = target_user.get("recent_login")
    if isinstance(recent_login, dict) and isinstance(recent_login.get("min_days"), int):
        other_predicates.append(context.member_recent_login_predicate(recent_login["min_days"])); labels.append("recent_login"); has_signal = True

    # 생일 타겟(BIRTHDAY 월일 비교; '이달 생일'은 월 비교). 년도는 비교하지 않는다.
    birthday_target = target_user.get("birthday_target")
    if isinstance(birthday_target, dict):
        granularity = "month" if birthday_target.get("granularity") == "month" else "day"
        other_predicates.append(context.member_birthday_predicate(granularity)); labels.append("birthday_" + granularity); has_signal = True

    # 잔액 임계값(적립금/예치금 N원 이상): 회원 테이블 잔액 컬럼 직접 비교. balance_conditions 는
    # _apply_balance_condition_filter 가 numeric_filters(balance) 설정 기준으로 뽑는다.
    for condition in target_user.get("balance_conditions", []):
        if not isinstance(condition, dict):
            continue
        column = condition.get("column")
        # NULL/0 구분 술어: '정보가 없는'(IS NULL, 0 과 구분)·'없거나 0원'(IS NULL OR = 0). 값이 0 인
        # 회원과 값 자체가 없는(미기입) 회원을 다른 대상으로 취급한다([[deterministic-filter-registry]]).
        null_mode = condition.get("null_mode")
        if isinstance(column, str) and column and null_mode in {"is_null", "null_or_zero"}:
            source = condition.get("profile_source")
            if isinstance(source, dict):
                alias = source.get("alias")
                source_column = source.get("column") or column
                if not (isinstance(alias, str) and isinstance(source_column, str)):
                    continue
                predicate = (
                    f"{alias}.{source_column} IS NULL" if null_mode == "is_null"
                    else f"({alias}.{source_column} IS NULL OR {alias}.{source_column} = 0)"
                )
                if _add_profile_predicate(source, predicate):
                    labels.append(str(condition.get("label") or column)); has_signal = True
                continue
            if null_mode == "is_null":
                other_predicates.append(f"{context.member_alias}.{column} IS NULL")
            else:
                other_predicates.append(f"({context.member_alias}.{column} IS NULL OR {context.member_alias}.{column} = 0)")
            labels.append(str(condition.get("label") or column)); has_signal = True
            continue
        operator = condition.get("operator")
        if not (isinstance(column, str) and column and operator in {"=", ">", ">=", "<", "<="}):
            continue
        threshold_expr = condition.get("threshold_expr")
        threshold = condition.get("threshold")
        if isinstance(threshold_expr, str) and threshold_expr:
            right = threshold_expr  # 컬럼 대 컬럼 비교('적립금 > 예치금')
        else:
            try:
                # Exact fractional values cross the JSON boundary as strings;
                # the injected formatter owns finite-decimal validation.
                right = context.format_threshold(threshold)
            except (TypeError, ValueError):
                continue
        # 파생 비율 지표(하루 평균 = CNT/DAYS)는 좌변을 이미 조립된 식(column_expr)으로 쓴다.
        column_expr = condition.get("column_expr")
        if isinstance(column_expr, str) and column_expr:
            left = column_expr
        else:
            source = condition.get("profile_source")
            if isinstance(source, dict) and isinstance(source.get("alias"), str):
                qualified = f"{source['alias']}.{source.get('column') or column}"
            else:
                qualified = f"{context.member_alias}.{column}"
            # zero_semantics(missing_as_zero): NULL 을 0 으로 봐야 '한 번도 …' 조건이 NULL 회원까지 포함한다.
            left = f"COALESCE({qualified}, 0)" if condition.get("coalesce_zero") else qualified
        predicate = f"{left} {operator} {right}"
        source = condition.get("profile_source")
        if isinstance(source, dict):
            if not _add_profile_predicate(source, predicate):
                unsupported.append("target_user.balance_conditions")
                continue
        else:
            other_predicates.append(predicate)
        labels.append(str(condition.get("label") or column)); has_signal = True

    # 날짜 프로필의 현재일 상대 상태(지난/도래 전 등). 숫자 프로필과 같은 source key를 사용하므로
    # BUY_CYCLE과 BUY_DUE_DATE처럼 한 스냅샷 행에서 동시에 만족해야 하는 조건은 하나의 EXISTS가 된다.
    for condition in target_user.get("profile_date_conditions", []):
        if not isinstance(condition, dict):
            continue
        source = condition.get("profile_source")
        operator = condition.get("operator")
        right = (
            context.reference_date_sql
            if condition.get("anchor") == "reference_date"
            else condition.get("right_expression")
        )
        if not isinstance(source, dict) or operator not in {"=", ">", ">=", "<", "<="}:
            continue
        alias = source.get("alias")
        column = source.get("column") or condition.get("column")
        if not (isinstance(alias, str) and isinstance(column, str) and isinstance(right, str) and right):
            unsupported.append("target_user.profile_date_conditions")
            continue
        if _add_profile_predicate(source, f"{alias}.{column} {operator} {right}"):
            labels.append(str(condition.get("label") or condition.get("metric_id") or column)); has_signal = True

    for (table, alias, member_column, base_member_column, grain_filter), predicates in profile_predicates.items():
        clauses = [f"{alias}.{member_column} = {context.member_alias}.{base_member_column}"]
        if grain_filter:
            clauses.append(grain_filter)
        clauses.extend(context.unique_strings(predicates))
        other_predicates.append(
            "EXISTS (SELECT 1 FROM " + table + " " + alias
            + " WHERE " + " AND ".join(clauses) + ")"
        )

    # 캠페인 반응(접촉 성공/오퍼·구매 반응/쿠폰 사용): 회원키 EXISTS 서브쿼리라 회원 컬럼 술어와 똑같이
    # AND 결합된다. 여기서 컴파일해야 어느 빌더를 타든 조건이 남는다 — 예전엔 전용 빌더만 이 조건을
    # 알아서, '발송 성공했지만 구매하지 않은'처럼 다른 트랙(무구매 anti-join)이 이기는 프롬프트에서
    # 캠페인 조건이 조용히 사라졌다.
    for response in target_user.get("campaign_responses", []):
        predicate = response.get("predicate") if isinstance(response, dict) else None
        if not predicate:
            continue
        other_predicates.append(
            context.campaign_response_exists_predicate(
                str(predicate),
                negated=bool(response.get("negated")),
                source=response.get("source"),
            )
        )
        labels.append(str(response.get("canonical") or "campaign_response")); has_signal = True

    # 쿠폰 사용 '건수' 임계(≥2·>5·범위 등): 회원별 SUM(USE_CPN_CNT) HAVING 집계를 회원키 IN 서브쿼리로
    # 컴파일한다(사용 '여부'는 위 campaign_responses EXISTS 가 담당). 다른 회원 조건과 AND 결합된다.
    for threshold in target_user.get("coupon_usage_thresholds", []) or []:
        predicate = context.coupon_usage_threshold_predicate(threshold) if isinstance(threshold, dict) else None
        if predicate:
            other_predicates.append(predicate)
            labels.append("coupon_usage_count"); has_signal = True

    # 장바구니 부재('장바구니 없는/생성 안 한'): 보관(KEEP_YN='Y') 카트 라인이 없는 회원. 회원키
    # NOT EXISTS 라 캠페인 반응과 같이 어느 빌더에나 AND 결합된다. 구매 부재(no_purchase)와 함께 오면
    # ("장바구니나 구매 이력 없는") 각각 NOT EXISTS/anti-join 으로 둘 다 남는다.
    if target_user.get("cart_absence"):
        other_predicates.append(context.cart_absence_predicate())
        labels.append("cart_absence"); has_signal = True

    # 장바구니 수량 미입력('수량이 입력되지 않은'): 담은 수량(QTY)이 NULL 인 카트 라인이 있는 회원.
    # '수량 0'(=0)이 아니라 값 자체가 미기입(NULL) — 회원키 EXISTS 라 어느 빌더에나 AND 결합된다.
    if target_user.get("cart_quantity_missing"):
        other_predicates.append(context.cart_quantity_missing_predicate())
        labels.append("cart_quantity_missing"); has_signal = True

    # 구매 이력 존재(선택적으로 최근 N일 창). 단순 "구매한 회원"도 주문 근거 없이 회원 테이블 전체로
    # 축약되지 않도록 반드시 주문 헤더 EXISTS로 컴파일한다.
    purchase_membership = target_user.get("purchase_membership")
    if context.purchase_membership_needs_own_predicate(purchase_membership):
        other_predicates.append(context.purchase_membership_predicate(
            purchase_membership.get("window_days"),
            window=purchase_membership.get("window"),
        ))
        labels.append("purchase_exists"); has_signal = True

    # 구매 미발생 기간('최근 N일 미구매'): 회원키 NOT EXISTS anti-join 이라 cart_absence/캠페인 반응처럼
    # 어느 빌더에나 AND 결합된다. 여기서 방출해야 '장바구니 보유 + 최근 90일 미구매'처럼 다른 팩트 빌더
    # (카트)가 이기는 조합에서도 미구매 조건이 살아남는다. order_count 빌더와 동일 문자열이라 dedup 됨.
    purchase_inactivity = target_user.get("purchase_inactivity")
    if isinstance(purchase_inactivity, dict) and (
        isinstance(purchase_inactivity.get("min_days"), int)
        or isinstance(purchase_inactivity.get("window"), Mapping)
    ):
        other_predicates.append(context.purchase_inactivity_predicate(
            purchase_inactivity.get("min_days"),
            window=purchase_inactivity.get("window"),
        ))
        min_days = purchase_inactivity.get("min_days")
        labels.append(
            f"purchase_inactive_{min_days}d"
            if isinstance(min_days, int)
            else "purchase_inactive_calendar_window"
        )
        has_signal = True

    # 신규 가입 타겟(REG_DT 최근 N일 창). signup_target(창 파싱) 또는 lifecycle 'new_user'(LLM 라벨)
    # 어느 쪽이든 트리거하고 하나의 술어로 합친다. 창은 signup_target.days > default_days 순으로 결정.
    signup_target = target_user.get("signup_target")
    if isinstance(signup_target, dict) or "new_user" in (target_user.get("lifecycle") or []):
        days = signup_target.get("days") if isinstance(signup_target, dict) else None
        other_predicates.append(context.member_signup_predicate(days if isinstance(days, int) else None))
        labels.append("new_user"); has_signal = True

    # 회원 테이블 디멘션 필터(예: 시도 → CRM_MB_BASEINFO.SIDO IN ('서울')). dimension_catalog 로 값이
    # 이미 코드로 해석돼 넘어오고, 회원 기본정보 단독 컬럼이라 조인 없이 술어로 AND 결합한다.
    # 지역 컬럼(SIDO/SIGUNGU)은 같은 '거주 지역' 도메인이라 별도 수집 후 나열(OR)/수식(AND)을 판별한다.
    # 보조 속성 테이블 필터(join_column 지정, 예: ODS_MALL_MMS_MEMBER_ZTS.JOB_CD)는 회원키 서브쿼리
    # (B.<join> IN (SELECT <join> FROM <표> WHERE <컬럼> IN ...))로 결합한다 — 값 인덱스가 채워지면
    # 코드 수정 없이 자동으로 이 경로를 탄다. (dimension_id 별 필터는 각각 술어가 되어 자동 조합.)
    member_region_codes: dict[str, list[str]] = {}
    member_region_excludes: dict[str, list[str]] = {}
    for dimension_index, dimension_filter in enumerate(query_plan.get("dimension_filters", [])):
        if not isinstance(dimension_filter, Mapping):
            unsupported.append(f"dimension_filters.{dimension_index}")
            continue
        operator = context.dimension_filter_operator(dimension_filter)
        if operator is None:
            unsupported.append(f"dimension_filters.{dimension_index}.operator")
            continue
        sql_operator = context.dimension_operator_sql_map[operator]
        table_name = dimension_filter.get("table")
        join_column = dimension_filter.get("join_column")
        if table_name != context.member_table and not join_column:
            continue
        column_short = (dimension_filter.get("column") or "").split(".")[-1].upper()
        codes = [code for code in dimension_filter.get("codes", []) if isinstance(code, str) and code]
        if not column_short or not codes:
            continue
        if table_name == context.member_table and column_short in context.member_region_columns:
            region_target = member_region_excludes if operator == "NOT_IN" else member_region_codes
            region_target.setdefault(column_short, [])
            region_target[column_short].extend(code for code in codes if code not in region_target[column_short])
        else:
            in_list = ", ".join(context.sql_quote(code) for code in codes)
            if table_name == context.member_table:
                other_predicates.append(context.member_alias + "." + column_short + f" {sql_operator} (" + in_list + ")")
            elif operator == "NOT_IN":
                # 보조 테이블은 한 회원에 여러 행이 있을 수 있다. ``IN (SELECT ... WHERE value NOT IN)``은
                # 제외값 행과 다른 행이 함께 있을 때 회원을 다시 포함하므로, 제외값 존재 자체를 anti-join 한다.
                other_predicates.append(
                    f"NOT EXISTS (SELECT 1 FROM {table_name} S WHERE S.{join_column} = {context.member_alias}.{join_column} "
                    f"AND S.{column_short} IN ({in_list}))"
                )
            else:
                other_predicates.append(
                    f"{context.member_alias}.{join_column} IN (SELECT S.{join_column} FROM {table_name} S WHERE S.{column_short} IN ({in_list}))"
                )
        label_values = dimension_filter.get("names") or codes
        labels.extend(["non_" + str(value) for value in label_values] if operator == "NOT_IN" else label_values)
        has_signal = True
    other_predicates.extend(context.member_region_predicates(member_region_codes))
    for column, codes in member_region_excludes.items():
        in_list = ", ".join(context.sql_quote(code) for code in codes)
        other_predicates.append(f"{context.member_alias}.{column} NOT IN ({in_list})")
    if not context.validate_compound_dimension_filters(query_plan):
        for compound in query_plan.get("compound_dimension_filters") or []:
            other_predicates.append(context.compile_compound_dimension_filter(compound))
            for group in compound.get("groups") or []:
                labels.extend(
                    str(item.get("value"))
                    for item in group.get("filters") or []
                    if isinstance(item, Mapping) and item.get("value")
                )
            has_signal = True

    # CRM_MB_BASEINFO 단독으로 표현할 수 없는 조건(→ unsupported 로 모아 fallback 유도)
    for field in ("interests", "preferred_channels", "behaviors", "purchase_object", "price_sensitivity"):
        if target_user.get(field):
            unsupported.append("target_user." + field)
    if exclude.get("interests"):
        unsupported.append("exclude.interests")
    # 집계 조건은 build_aggregate_targets_sql_candidate 가 커버한다. 그 빌더가 dropped 에서 빼주므로,
    # 여기선 일단 unsupported 로 표시해 (집계 빌더에 닿지 못하고) 회원 빌더로 빠질 때 조용한 누락을 막는다.
    if target_user.get("aggregate_conditions"):
        unsupported.append("target_user.aggregate_conditions")
    for field in ("set_expressions", "computed_metrics", "policy_constraints", "semantic_resolutions"):
        if query_plan.get(field):
            unsupported.append(field)
    for field in ("category", "offer_type", "channels"):
        if campaign.get(field):
            unsupported.append("campaign_constraints." + field)

    # 같은 컬럼 포함값은 1개면 `=`, 2개 이상이면 `IN (...)` 으로 묶는다(예: 실버 OR 골드 등급).
    include_predicates: list[str] = []
    for column, values in eq_includes.items():
        if len(values) == 1:
            include_predicates.append(column + " = " + context.sql_quote(values[0]))
        else:
            include_predicates.append(column + " IN (" + ", ".join(context.sql_quote(value) for value in values) + ")")

    return {
        "predicates": context.unique_strings([*include_predicates, *other_predicates]),
        "labels": context.unique_strings(labels),
        "forces_state": ("state" in include_categories) or suppresses_default_state,
        "has_signal": has_signal,
        "unsupported": unsupported,
    }
