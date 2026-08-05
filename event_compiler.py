"""조건 IR → SQL. 자연어를 **모르는** 컴파일러 + 업무 심볼 레지스트리.

이 모듈은 :mod:`event_ir` 의 범용 노드만 읽는다. 한국어 표면어도, 구매 전용 분기도, 슬롯 이름도 여기
없다 — 있으면 업무 개념이 하나 늘 때마다 컴파일러가 같이 자란다. 업무 개념과 물리 스키마의 대응은
**레지스트리 둘**이 소유하고, 그 확장이 새 조건을 여는 유일한 방법이다:

    EVENT_REGISTRY  — 사건 심볼('purchase') → 테이블/조인키/시각컬럼
    FIELD_REGISTRY  — 필드 심볼('purchase.amount') → 컬럼/타입

새 업무 이벤트/속성이 등장하면 IR 타입을 추가하지 않고 이 표에 한 줄을 넣는다. 사건의 발생 시각
필드(``<event>.occurred_at``)는 EventSpec 에서 **자동 파생**되므로 따로 적지 않는다.

바인딩 종류가 둘인 이유는 실제 스키마가 그렇기 때문이다. 주문은 별도 팩트 테이블(EXISTS/NOT EXISTS)
이지만, 로그인·가입은 실CRM 에서 회원 테이블의 컬럼 하나다(``LAST_LOGIN_DATE``/``REG_DT``). 둘을 한
컴파일러가 다루지 못하면 '구매는 없고 로그인은 있는' 같은 한 문장이 다시 두 트랙으로 쪼개진다.

경계 규칙: 절대 구간은 **반개구간**(``>= start AND < end_exclusive``)이다. ``BETWEEN`` 은 날짜 컬럼이
timestamp 일 때 마지막 날을 잘라먹는다 — IR 이 정한 경계를 SQL 이 다시 흔들지 않는다.

파라미터: 기본은 이름 있는 바인드 파라미터(``:event_0_start``)다. 실행 파이프라인이 SQL **문자열**을
검증·가드하는 이 저장소의 기존 계약을 위해 리터럴 렌더도 같은 코드 경로로 지원한다
(:class:`CompileContext` 의 ``literals=True``) — 두 렌더러를 따로 두면 반드시 갈라진다.
"""

from __future__ import annotations

import sql_dialect

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import event_ir
import semantic_fields
from event_ir import (
    AbsoluteInterval,
    Aggregate,
    And,
    Arithmetic,
    Comparison,
    EventReference,
    Exists,
    FieldRef,
    Filter,
    Group,
    Join,
    Limit,
    Literal,
    Not,
    NullIf,
    Order,
    Or,
    Project,
    RelativeWindow,
    RollingWindow,
    Source,
    Summarize,
    TemporalRelation,
    TimeFilter,
    Tuple,
)
from sql_dialect import (
    RowLimit,
    SqlDialect,
    UnsupportedDialectFeatureError,
    get_dialect,
)


class SqlCompileError(Exception):
    """IR 은 유효하지만 이 스키마/방언으로는 표현할 수 없다. 의미를 줄이지 말고 여기서 멈춘다."""


# 컴파일 규칙의 버전. **의미가 같아도 SQL 이 달라지는 변경**(경계 렌더·조인 형태·NULL 처리)이 있으면
# 올린다. 이행 계층의 binding fingerprint 가 이 값을 포함하므로, 올리면 검증된 자산이 자동으로
# '바인딩 변경'으로 표시되고 cut-over 전에 재검증을 요구한다.
# 1.2.0: 회원 상관 스칼라 집계를 집합형 semi-join 으로 낮춘다(의미 동일, SQL 모양 변경).
COMPILER_VERSION = "1.2.0"

CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_PARTIALLY_SUPPORTED = "partially_supported"


@dataclass(frozen=True)
class CompilerCapabilityIssue:
    code: str
    node_id: str
    symbol: str | None = None


@dataclass(frozen=True)
class CompilerCapabilityResult:
    status: str
    issues: tuple[CompilerCapabilityIssue, ...]
    supported_node_ids: tuple[str, ...]
    unsupported_node_ids: tuple[str, ...]


@dataclass
class CompiledCondition:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)


# ── 레지스트리 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubjectSpec:
    """조건이 걸리는 주체(오디언스). 실CRM 은 회원 기준 테이블이다."""

    table: str = "CRM_MB_BASEINFO"
    alias: str = "B"
    key: str = "MEMBER_NO"
    name: str = "subject"


@dataclass(frozen=True)
class EventSpec:
    """사건 심볼 하나의 물리 바인딩.

    ``binding="fact_table"``    — 사건이 별도 테이블의 행이다(EXISTS/NOT EXISTS 상관 서브쿼리).
    ``binding="subject_column"`` — 사건의 발생 시각이 주체 테이블의 컬럼 하나다(직접 비교).

    ``time_format`` 은 시각 컬럼의 저장 관례다. 실CRM 은 ``nvarchar(8) 'YYYYMMDD'``(``char8``)이고,
    일반 날짜/시각 컬럼은 ``date``. 경계 렌더가 여기서 갈린다.
    """

    table: str
    alias: str
    subject_key: str
    event_subject_key: str
    time_column: str
    time_format: str = "char8"
    binding: str = "fact_table"
    # 사건 정의에 항상 붙는 추가 술어(예: 보관 중인 카트만). 별칭은 ``{alias}`` 로 적는다.
    extra_predicates: tuple[str, ...] = ()
    label: str = ""
    # 단순 ``table alias`` 로 끝나지 않는 물리 소스(검증된 조인을 포함한 relation)도
    # 같은 Source 노드로 컴파일한다. ``{alias}`` 는 이 사건 인스턴스의 별칭이다.
    # 비어 있으면 기존 ``table alias`` 바인딩을 그대로 사용한다.
    from_sql: str = ""
    # 회원키 타입 변환처럼 ``alias.column = subject.column`` 으로 표현할 수 없는 상관식.
    # {alias}, {subject_alias}, {subject_key}, {event_subject_key} 를 사용할 수 있다.
    correlation_sql: str = ""
    # 상관식의 **사건 쪽 절반**(집합형 집계의 group key). 컴파일러 전용 물리 바인딩이며 Core IR 이
    # 아니다. ``correlation_sql`` 을 정규식으로 되짚어 집계키를 추측하지 않기 위해 따로 선언한다.
    # 비어 있고 correlation_sql 도 없으면 기본 상관식의 왼쪽인 ``{alias}.{event_subject_key}`` 로
    # 파생한다(선언과 파생이 어긋날 여지가 없는 유일한 경우).
    group_subject_expression: str = ""
    # 발생 시각이 검증된 조인 대상에 있거나 계산식인 경우의 논리 시각 필드 바인딩.
    time_expression: str = ""


@dataclass(frozen=True)
class FieldSpec:
    """필드 심볼 하나의 물리 바인딩. 새 업무 속성은 이 표에 한 줄이면 쓸 수 있다."""

    source: str
    column: str
    data_type: str = "number"  # number | string | date | date_char8 | date_char6
    # 계산 필드/조인 필드. ``{alias}`` 는 필드가 속한 Source 의 현재 별칭이다.
    # 비어 있으면 기존 ``alias.column`` 바인딩을 사용한다.
    expression: str = ""
    # 논리값(canonical) → 물리 저장값. 값 도메인이 선언된 필드는 알 수 없는
    # 문자열을 그대로 SQL에 흘리지 않고 fail-close 한다.
    value_map: tuple[tuple[str, Any], ...] = ()
    # 값 도메인에 **순서가 있으면** canonical 을 낮은 등급부터 나열한다. 비면 무순서 도메인이고,
    # 그때 서열 비교(>=, <, …)는 표현할 수 없다 — 저장값 문자열을 부등호로 비교하면 조용히 틀린다
    # (실측: EMART_GRADE_CD 는 'MEM_GRADE_CD.*' 라 사전식 순서가 F<G<S<V<W 이고,
    #  '골드 이상'에 SILVER·WELCOME 이 섞이고 '실버 이상'에서 GOLD 가 빠진다).
    value_order: tuple[str, ...] = ()
    # 값 표면어 → canonical. 표면어 목록의 소유자는 값 사전(eq_filters)이고 여기는 파생 사본이다.
    value_aliases: tuple[tuple[str, str], ...] = ()
    # 자유 텍스트 이름/분류를 물리 식별자 컬럼에 직접 비교하지 않도록 하는 선언형 검색 바인딩.
    # ``contains`` 필드는 ``=``/``!=`` 비교를 아래 SQL 표현식들의 안전한 LIKE 검색으로
    # 컴파일한다. 표현식은 catalog 소유이고 사용자 리터럴은 절대 들어가지 않는다.
    match_mode: str = "exact"  # exact | contains
    search_expressions: tuple[str, ...] = ()
    # 식별자 필드에 자연어 이름이 들어오는 것을 막는 catalog 소유 정규식. 비어 있으면 제한 없음.
    literal_pattern: str = ""
    # ``Not(field = value)`` 에서 물리 NULL 행을 포함할지 여부. 오디언스 보수의 기본은
    # include_unknown 이지만, SITE_MEMBER_YN처럼 승인된 업무 SQL이 명시적 N만 허용하는
    # 필드는 exclude_unknown을 선언한다. 모델이나 GraphRAG가 이 정책을 고르지 않는다.
    negative_null_policy: str = "include_unknown"  # include_unknown | exclude_unknown
    # Distinguish a catalog-owned policy from the compatibility default.  A
    # direct ``!=`` keeps historical SQL semantics unless the field explicitly
    # opted into one of the two audience-complement policies.
    negative_null_policy_declared: bool = False

    def __post_init__(self) -> None:
        if self.match_mode not in {"exact", "contains"}:
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'의 match_mode가 올바르지 않습니다: "
                f"{self.match_mode!r}"
            )
        if self.negative_null_policy not in {"include_unknown", "exclude_unknown"}:
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'의 negative_null_policy가 올바르지 않습니다: "
                f"{self.negative_null_policy!r}"
            )
        if not isinstance(self.negative_null_policy_declared, bool):
            raise SqlCompileError("negative_null_policy_declared must be boolean")
        if self.match_mode == "contains" and self.data_type != "string":
            raise SqlCompileError(
                f"부분 문자열 검색 필드 '{self.source}.{self.column}'는 string 타입이어야 합니다"
            )
        if self.literal_pattern:
            try:
                re.compile(self.literal_pattern)
            except re.error as exc:
                raise SqlCompileError(
                    f"필드 '{self.source}.{self.column}'의 literal_pattern이 올바르지 않습니다"
                ) from exc

    def canonicalize(self, value: Any) -> Any:
        """표면어를 canonical 로 바꾼다. 모르는 값은 **그대로 둔다** — 판정은 physical_value 가 한다."""
        if not isinstance(value, str) or not self.value_aliases:
            return value
        if any(canonical == value for canonical, _ in self.value_map):
            return value
        return dict(self.value_aliases).get(value.strip().casefold(), value)

    def complementary_physical_value(self, value: Any) -> Any | None:
        """Return the one catalog-declared opposite value, when it is unique."""

        canonical = self.canonicalize(value)
        candidates = {
            physical
            for candidate, physical in self.value_map
            if candidate != canonical
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    def ordered_values(self, operator: str, canonical: Any) -> tuple[Any, ...]:
        """서열 비교를 만족하는 **물리값 집합**. 부등호를 값 집합으로 바꿔 collation 의존을 없앤다."""
        if not isinstance(canonical, str) or canonical not in self.value_order:
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'의 순서 있는 값이 아닙니다: {canonical!r}"
            )
        index = self.value_order.index(canonical)
        keep: Callable[[int], bool] = {
            ">=": lambda position: position >= index,
            ">": lambda position: position > index,
            "<=": lambda position: position <= index,
            "<": lambda position: position < index,
        }[operator]
        values = dict(self.value_map)
        return tuple(values[name] for position, name in enumerate(self.value_order) if keep(position))

    def physical_value(self, value: Any) -> Any:
        if (
            self.literal_pattern
            and (
                not isinstance(value, str)
                or re.fullmatch(self.literal_pattern, value) is None
            )
        ):
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'에는 식별자 형식의 값만 비교할 수 있습니다: "
                f"{value!r}"
            )
        if not self.value_map:
            return value
        if not isinstance(value, str):
            raise SqlCompileError(
                f"값 도메인이 있는 필드 '{self.source}.{self.column}'에는 canonical 문자열이 필요합니다"
            )
        values = dict(self.value_map)
        if value not in values:
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'에 등록되지 않은 canonical 값입니다: {value}"
            )
        return values[value]


# 기본 사건 레지스트리 — 실CRM(CRMDW) 바인딩. graph_rag 가 member_target_filters.json 의 값으로
# 오버라이드를 주입한다(:func:`resolve_registry`). 코드 상수는 설정 부재 시 폴백이다.
EVENT_REGISTRY: dict[str, EventSpec] = {
    "purchase": EventSpec(
        table="CRM_SL_ORDERHEADERMALL", alias="EO",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="ORDER_DATE", time_format="char8", binding="fact_table", label="구매",
    ),
    "login": EventSpec(
        # 실CRM 에는 로그인 이력 테이블이 없고 회원 기준 테이블의 마지막 접속일만 있다.
        table="CRM_MB_BASEINFO", alias="B",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="LAST_LOGIN_DATE", time_format="char8", binding="subject_column", label="로그인",
    ),
    "signup": EventSpec(
        table="CRM_MB_BASEINFO", alias="B",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="REG_DT", time_format="char8", binding="subject_column", label="가입",
    ),
    "cart": EventSpec(
        # 장바구니만 회원키가 다르다 — 팩트 쪽은 CART_ID(로그인ID 문자열)이고 짝은 회원 테이블의
        # MEMBER_ID 다(MEMBER_NO 는 bigint 라 도메인이 다르다). UPD_DT 는 datetime2 라 char8 이 아니다.
        table="ODS_MALL_OMS_CART", alias="EC",
        subject_key="MEMBER_ID", event_subject_key="CART_ID",
        time_column="UPD_DT", time_format="date", binding="fact_table", label="장바구니 담기",
    ),
}

# 기본 필드 레지스트리. 발생 시각(``<event>.occurred_at``)은 EventSpec 에서 파생하므로 여기 없다.
FIELD_REGISTRY: dict[str, FieldSpec] = {
    "purchase.amount": FieldSpec(source="purchase", column="PAYMENT_AMT", data_type="number"),
    "purchase.order_id": FieldSpec(source="purchase", column="ORDER_ID", data_type="string"),
    "cart.quantity": FieldSpec(source="cart", column="QTY", data_type="number"),
    "cart.product_id": FieldSpec(source="cart", column="PRODUCT_ID", data_type="string"),
    "subject.grade": FieldSpec(source="subject", column="EMART_GRADE_CD", data_type="string"),
    "subject.age": FieldSpec(source="subject", column="AGE", data_type="number"),
}

@dataclass(frozen=True)
class TimeGrainSpec:
    """시간 컬럼 하나의 **저장 표기와 칸 크기**.

    grain 을 타입으로 세우는 이유는 하나다 — 예전에는 미등록 포맷이 조용히 ``date`` 로 폴백했고,
    그러면 월 스냅샷 컬럼(nvarchar(6) 'YYYYMM')에 ``>= '2026-07-01'`` 이 렌더돼 사전식 비교로
    **항상 0건**이 나왔다. 예외도 경고도 없이. 등록되지 않은 포맷은 이제 컴파일 오류다.

    ``unit`` 은 이 컬럼이 가리킬 수 있는 가장 작은 칸이다. 칸보다 잘게 쪼갠 창(월 컬럼에 '지난달
    15일부터')이나 칸 산술이 성립하지 않는 연산(월 컬럼에 'N일 이내')은 fail-close 한다 —
    근사해서 답하면 사용자가 요청하지 않은 대상이 나온다.
    """

    time_format: str
    data_type: str
    unit: str  # event_ir.WINDOW_UNITS 의 값. 'day' | 'month'
    # 이 grain 이 롤링 창(실행 시점 컷오프)을 표현할 수 있는가. None 이면 표현 불가.
    rolling_cutoff: Callable[[Any, int], str] | None = None
    # 저장 표기로의 변환. 파라미터 바인딩과 리터럴 렌더가 같은 함수를 쓴다(두 벌이면 곧 어긋난다).
    render: Callable[[date], Any] = lambda value: value

    def aligned(self, value: date) -> bool:
        """이 날짜가 grain 칸의 **시작**인가. 월 grain 은 달의 1일만 칸 경계다."""
        return True if self.unit == "day" else value.day == 1


TIME_GRAINS: dict[str, TimeGrainSpec] = {
    "char8": TimeGrainSpec(
        time_format="char8", data_type="date_char8", unit="day",
        rolling_cutoff=lambda dialect, days: dialect.char8_cutoff(days),
        render=lambda value: f"{value.year:04d}{value.month:02d}{value.day:02d}",
    ),
    "date": TimeGrainSpec(
        time_format="date", data_type="date", unit="day",
        rolling_cutoff=lambda dialect, days: dialect.datetime_cutoff(days),
        render=lambda value: value,
    ),
    # 월 스냅샷. 0-패딩 고정폭이라 사전식 순서 = 시간 순서이므로 반개구간 계약이 그대로 보존된다.
    "char6": TimeGrainSpec(
        time_format="char6", data_type="date_char6", unit="month",
        rolling_cutoff=None,  # '최근 N일'은 월 칸으로 답할 수 없다 — 근사 금지.
        render=lambda value: f"{value.year:04d}{value.month:02d}",
    ),
}

# 아래 둘은 **파생**이다. 손 목록으로 두면 grain 을 늘릴 때마다 어긋난다.
DATA_TYPE_GRAINS: dict[str, TimeGrainSpec] = {grain.data_type: grain for grain in TIME_GRAINS.values()}
DATE_DATA_TYPES: frozenset[str] = frozenset(DATA_TYPE_GRAINS)


def time_format_data_type(time_format: str) -> str:
    """선언된 저장 포맷 → 필드 data_type. 미등록 포맷은 **조용히 넘어가지 않는다**."""
    grain = TIME_GRAINS.get(time_format)
    if grain is None:
        raise SqlCompileError(
            f"등록되지 않은 시간 저장 포맷입니다: {time_format!r} "
            f"(등록된 포맷: {', '.join(sorted(TIME_GRAINS))})"
        )
    return grain.data_type


def _time_grain(data_type: str) -> TimeGrainSpec:
    grain = DATA_TYPE_GRAINS.get(data_type)
    if grain is None:
        raise SqlCompileError(f"시간 컬럼이 아닌 타입에 기간 조건을 걸 수 없습니다: {data_type!r}")
    return grain


def resolve_registry(overrides: dict[str, EventSpec] | None = None) -> dict[str, EventSpec]:
    """기본 사건 레지스트리에 배포별 오버라이드를 얹는다(설정이 스키마의 단일 소스이도록)."""
    resolved = dict(EVENT_REGISTRY)
    if overrides:
        resolved.update(overrides)
    return resolved


def resolve_fields(
    registry: dict[str, EventSpec], overrides: dict[str, FieldSpec] | None = None
) -> dict[str, FieldSpec]:
    """필드 레지스트리 + **사건에서 파생된 발생 시각 필드**.

    ``<event>.occurred_at`` 을 손으로 적지 않는 이유: 이벤트를 등록해 놓고 시각 필드를 빠뜨리면
    그 사건에는 기간 조건을 걸 수 없는데, 그 사실이 조용히 드러나지 않는다."""
    fields = dict(FIELD_REGISTRY)
    for name, spec in registry.items():
        fields[f"{name}.{event_ir.TIME_FIELD_SUFFIX}"] = FieldSpec(
            source=name, column=spec.time_column,
            data_type=time_format_data_type(spec.time_format),
            expression=spec.time_expression,
        )
    if overrides:
        # 시각 필드를 override 로 다시 선언하면 grain 을 두 곳이 말하게 된다 — time_format 에서
        # 파생된 것과 어긋나면 조용히 override 가 이긴다. 어긋남은 선언 오류로 막는다.
        for name, override in overrides.items():
            derived = fields.get(name)
            if (
                name.endswith(f".{event_ir.TIME_FIELD_SUFFIX}")
                and derived is not None
                and (override.column, override.data_type) != (derived.column, derived.data_type)
            ):
                raise SqlCompileError(
                    f"시각 필드 '{name}' 의 선언이 소스의 time_column/time_format 과 어긋납니다: "
                    f"{(override.column, override.data_type)} != {(derived.column, derived.data_type)}"
                )
        fields.update(overrides)
    return fields


@dataclass
class CompileContext:
    """컴파일 한 번의 환경. 주체·레지스트리·방언·파라미터 스타일·기준일을 한 곳에 모은다."""

    subject: SubjectSpec = field(default_factory=SubjectSpec)
    registry: dict[str, EventSpec] = field(default_factory=lambda: dict(EVENT_REGISTRY))
    fields: dict[str, FieldSpec] | None = None
    dialect: SqlDialect = field(default_factory=lambda: get_dialect("tsql"))
    # True 면 값을 SQL 리터럴로 인라인한다(문자열 SQL 을 검증하는 기존 파이프라인용).
    literals: bool = False
    # 상대 창('3개월 전')을 절대 구간으로 확정하는 기준일. 계획 시점에 날짜가 드러나야 감사 가능하다.
    today: date | None = None
    # 회원 상관 스칼라 집계 → 집합형 semi-join 물리 lowering 스위치. 전역 mutable state 가 아니라
    # 컴파일 한 번의 환경으로 **주입**한다(끄면 기존 SQL 이 바이트 동일하게 나온다).
    optimize_aggregate_membership: bool = True
    # 최적화 적용/스킵 receipt 를 받는 선택적 채널. IR capabilities 도 query identity 도 아니다 —
    # 컴파일러가 무엇을 했는지 운영에서 되짚기 위한 진단 전용이다.
    optimization_receipts: list[dict[str, Any]] | None = None
    _counter: list[int] = field(default_factory=lambda: [0])
    # 컴파일 중인 관계 스코프: 소스 심볼 → 별칭. FieldRef 해석이 이걸 본다.
    _scope: dict[str, str] = field(default_factory=dict)
    # 파생 관계가 materialize 된 뒤 canonical field가 가리키는 SQL 출력.
    _field_bindings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = resolve_fields(self.registry)

    def next_index(self) -> int:
        index = self._counter[0]
        self._counter[0] += 1
        return index

    def event_spec(self, name: str) -> EventSpec:
        spec = self.registry.get(name)
        if spec is None:
            raise SqlCompileError(f"등록되지 않은 사건입니다: {name}")
        return spec

    def field_spec(self, name: str) -> FieldSpec:
        spec = (self.fields or {}).get(name)
        if spec is None:
            raise SqlCompileError(f"등록되지 않은 필드입니다: {name}")
        return spec

    def with_scope(self, scope: dict[str, str]) -> "CompileContext":
        return CompileContext(
            subject=self.subject, registry=self.registry, fields=self.fields, dialect=self.dialect,
            literals=self.literals, today=self.today,
            optimize_aggregate_membership=self.optimize_aggregate_membership,
            optimization_receipts=self.optimization_receipts,
            _counter=self._counter,
            _scope={**self._scope, **scope},
            _field_bindings=self._field_bindings,
        )

    def with_field_bindings(self, bindings: dict[str, str]) -> "CompileContext":
        return CompileContext(
            subject=self.subject, registry=self.registry, fields=self.fields, dialect=self.dialect,
            literals=self.literals, today=self.today,
            optimize_aggregate_membership=self.optimize_aggregate_membership,
            optimization_receipts=self.optimization_receipts,
            _counter=self._counter,
            _scope=self._scope,
            _field_bindings={**self._field_bindings, **bindings},
        )


def _sql_quote(value: Any) -> str:
    """SQL 문자열 리터럴. 구현은 sql_dialect 가 단일 소유한다(미러 복제 금지)."""
    return sql_dialect.quote_literal(value)


def _nlike_contains_literal(column: str, term: str) -> str:
    """사용자 자유 텍스트 하나를 리터럴 부분일치 술어로 렌더한다.

    홑따옴표뿐 아니라 LIKE 메타문자(``%``, ``_``, SQL Server의 ``[``)도 이스케이프한다.
    따라서 상품명 ``100% 사료``나 ``A_B``는 와일드카드가 아니라 입력 그대로 검색되고,
    자유 텍스트가 SQL 구조로 빠져나갈 수 없다.
    """
    pattern = "N" + _sql_quote(_like_contains_pattern(term))
    return f"{column} LIKE {pattern} ESCAPE N'~'"


def _like_contains_pattern(term: str) -> str:
    """Return one escaped value shared by literal and bound LIKE renderers."""
    escaped = (
        term.replace("~", "~~")
        .replace("%", "~%")
        .replace("_", "~_")
        .replace("[", "~[")
    )
    return f"%{escaped}%"


# ── 시간 ──────────────────────────────────────────────────────────────────────────


def _render_date(value: date, data_type: str) -> str:
    rendered = _time_grain(data_type).render(value)
    return _sql_quote(rendered if isinstance(rendered, str) else rendered.isoformat())


def _param_value(value: date, data_type: str) -> Any:
    return _time_grain(data_type).render(value)


def compile_time_window(
    column: str, window: event_ir.TimeWindow, param_prefix: str, *,
    data_type: str, context: CompileContext,
) -> CompiledCondition:
    """시간 창 하나 → 컬럼 비교 술어. 절대 구간은 반개구간, 롤링은 실행 시점 컷오프.

    창은 grain 을 모른다(생산자는 대상 컬럼의 물리 저장 방식을 알 필요가 없다). 창과 컬럼의
    grain 이 안 맞으면 근사하지 않고 fail-close 한다 — 월 컬럼에 일 단위 창을 접으면 조용히
    다른 대상이 나온다.
    """
    grain = _time_grain(data_type)
    if isinstance(window, RelativeWindow):
        window = event_ir.resolve_relative_window(window, context.today)

    if isinstance(window, AbsoluteInterval):
        if window.has_time_bounds:
            # 시각 컬럼은 이 계층의 시간 바인딩(날짜 컬럼 하나)에 선언되어 있지 않다. 날짜만 걸면
            # '23시 59분 59초까지'가 '그날 하루 전체'가 되므로 근사하지 않고 미지원으로 닫는다 —
            # 시각을 표현할 수 있는 경로(주문 헤더 ORDER_TIME 술어)가 따로 있고, 그쪽으로 가야 한다.
            raise SqlCompileError(
                "시각 경계가 걸린 기간은 이 시간 바인딩(날짜 단위 컬럼)으로 표현할 수 없습니다"
                f"(요청 구간: {window.start.isoformat()} {window.start_time or ''}"
                f" ~ {window.inclusive_end.isoformat()} {window.end_time or ''})"
            )
        if not (grain.aligned(window.start) and grain.aligned(window.end_exclusive)):
            raise SqlCompileError(
                f"{grain.unit} 단위로 적재된 컬럼에는 그 경계에 맞는 기간만 걸 수 있습니다"
                f"(요청 구간: {window.start.isoformat()} ~ {window.end_exclusive.isoformat()})"
            )
        start_key, end_key = f"{param_prefix}_start", f"{param_prefix}_end"
        if context.literals:
            start_sql = _render_date(window.start, data_type)
            end_sql = _render_date(window.end_exclusive, data_type)
            params: dict[str, Any] = {}
        else:
            start_sql, end_sql = f":{start_key}", f":{end_key}"
            params = {
                start_key: _param_value(window.start, data_type),
                end_key: _param_value(window.end_exclusive, data_type),
            }
        return CompiledCondition(sql=f"{column} >= {start_sql} AND {column} < {end_sql}", params=params)

    if isinstance(window, RollingWindow):
        # 롤링 경계는 실행 시점 함수로 렌더한다 — 계획 시점 날짜로 굳히면 '최근 30일'이 고정된다.
        if grain.rolling_cutoff is None:
            raise SqlCompileError(
                f"{grain.unit} 단위로 적재된 컬럼에는 '최근 {window.days}일' 같은 롤링 창을 걸 수 없습니다"
                " — 일수를 칸 수로 근사하면 요청과 다른 구간이 됩니다"
            )
        return CompiledCondition(sql=f"{column} >= {grain.rolling_cutoff(context.dialect, window.days)}")

    raise SqlCompileError(f"지원하지 않는 시간 창입니다: {window!r}")


# ── 관계 ──────────────────────────────────────────────────────────────────────────


@dataclass
class RelationPlan:
    """관계 하나를 서브쿼리 조각으로 편 것(FROM/WHERE/GROUP BY)."""

    from_sql: str
    where: list[str]
    group_by: list[str]
    scope: dict[str, str]  # 소스 심볼 → 별칭
    root_source: str
    binding: str
    params: dict[str, Any]
    projection: list[str] = field(default_factory=list)
    # Both an output's short name and its canonical input FieldRef can resolve
    # to the materialized column.  Measures only have the short name.
    output_aliases: dict[str, str] = field(default_factory=dict)
    output_expressions: dict[str, str] = field(default_factory=dict)
    order_by: list[str] = field(default_factory=list)
    limit: RowLimit | None = None
    field_bindings: dict[str, str] = field(default_factory=dict)


def _binding_tokens(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> dict[str, str]:
    """Catalog SQL 조각에 허용하는 닫힌 치환 변수.

    업무 사건별 Python 분기를 만들지 않고 검증된 물리 선언을 재사용하기 위한 경계다.
    임의 query 값은 들어오지 않으며, 리터럴은 catalog 적재 시점에 확정된 문자열만 사용한다.
    """
    return {
        "alias": alias or spec.alias,
        "subject_alias": context.subject.alias,
        "subject_key": spec.subject_key,
        "event_subject_key": spec.event_subject_key,
    }


def _render_binding(
    template: str, spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    try:
        return template.format(**_binding_tokens(spec, context, alias=alias))
    except (KeyError, ValueError) as exc:
        raise SqlCompileError(f"사건 '{spec.label or spec.table}' 물리 바인딩 형식이 잘못되었습니다") from exc


def _source_sql(spec: EventSpec, context: CompileContext, *, alias: str | None = None) -> str:
    active_alias = alias or spec.alias
    if spec.from_sql:
        return _render_binding(spec.from_sql, spec, context, alias=active_alias)
    return f"{spec.table} {active_alias}"


def render_source_binding(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    """Render a trusted catalog source through the compiler's canonical binder.

    SQL validation uses this public boundary to derive aliases owned by the
    same catalog that owns compilation.  Keeping template expansion here avoids
    a second, potentially divergent formatter in the orchestration layer.
    """
    return _source_sql(spec, context, alias=alias)


def _correlation(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    active_alias = alias or spec.alias
    if spec.correlation_sql:
        return _render_binding(spec.correlation_sql, spec, context, alias=active_alias)
    return f"{active_alias}.{spec.event_subject_key} = {context.subject.alias}.{spec.subject_key}"


def _group_subject_sql(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str | None:
    """상관식의 사건 쪽 절반 — 회원별 집합형 집계의 group key.

    선언이 없고 상관식도 기본형이면 기본 상관식의 왼쪽을 그대로 파생한다. 상관식이 선언돼 있는데
    group key 가 없으면 ``None`` 을 돌려준다 — 문자열을 되짚어 집계키를 **추측하지 않는다**.
    """
    active_alias = alias or spec.alias
    if spec.group_subject_expression:
        return _render_binding(spec.group_subject_expression, spec, context, alias=active_alias)
    if spec.correlation_sql:
        return None
    return f"{active_alias}.{spec.event_subject_key}"


def _extra_predicates(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> list[str]:
    return [
        _render_binding(item, spec, context, alias=alias or spec.alias)
        for item in spec.extra_predicates
    ]


def _event_time_sql(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    active_alias = alias or spec.alias
    if spec.time_expression:
        return _render_binding(spec.time_expression, spec, context, alias=active_alias)
    return f"{active_alias}.{spec.time_column}"


def _validate_contains_filter_conjunction(
    condition: event_ir.Condition, context: CompileContext
) -> None:
    """Fail closed on the impossible same-row shape for ``all products``.

    ``Filter(Source(purchase_line), And(product_text = A, product_text = B))``
    asks one physical product row to match both values.  Natural-language
    "A와 B를 모두 구매" instead needs one correlated ``Exists`` per value and
    an ``And`` above those independent existence predicates.

    Keep this deliberately narrow: only positive equality comparisons that are
    direct conjuncts (including nested ``And`` nodes) of one Filter participate.
    ``Or`` branches, negation, different fields, and repeated identical values
    retain their existing semantics.
    """
    if not isinstance(condition, And):
        return

    def conjuncts(item: event_ir.Condition) -> list[event_ir.Condition]:
        if isinstance(item, And):
            return [child for operand in item.operands for child in conjuncts(operand)]
        return [item]

    values_by_field: dict[str, set[str]] = {}
    for item in conjuncts(condition):
        if not isinstance(item, Comparison) or item.operator != "=":
            continue
        field_ref: FieldRef | None = None
        literal: Literal | None = None
        if isinstance(item.left, FieldRef) and isinstance(item.right, Literal):
            field_ref, literal = item.left, item.right
        elif isinstance(item.right, FieldRef) and isinstance(item.left, Literal):
            field_ref, literal = item.right, item.left
        if field_ref is None or literal is None or not isinstance(literal.value, str):
            continue
        spec = context.field_spec(field_ref.name)
        if spec.match_mode != "contains":
            continue
        values_by_field.setdefault(field_ref.name, set()).add(
            literal.value.strip().casefold()
        )

    conflicted = sorted(name for name, values in values_by_field.items() if len(values) > 1)
    if conflicted:
        raise SqlCompileError(
            "부분검색 필드의 서로 다른 값들을 한 Filter AND로 비교하면 같은 상품 행에 "
            "모두 일치하라는 뜻이 됩니다: "
            + ", ".join(conflicted)
            + ". '모두 구매'는 값마다 독립된 Exists(Filter(...))를 만들고 그 Exists들을 "
            "And로 묶어야 합니다"
        )


def compile_relation(relation: event_ir.Relation, context: CompileContext) -> RelationPlan:
    if isinstance(relation, Source):
        spec = context.event_spec(relation.name)
        if spec.binding == "subject_column":
            if relation.correlation == "none":
                raise SqlCompileError("주체 컬럼 사건은 전역 비상관 관계로 사용할 수 없습니다")
            # 주체 테이블 컬럼으로 표현되는 사건은 독립 관계가 아니다 — 별도 서브쿼리를 만들지 않는다.
            return RelationPlan(
                from_sql="", where=[], group_by=[], scope={relation.name: context.subject.alias},
                root_source=relation.name, binding="subject_column", params={},
            )
        return RelationPlan(
            from_sql=_source_sql(spec, context),
            where=[
                *([_correlation(spec, context)] if relation.correlation == "subject" else []),
                *_extra_predicates(spec, context),
            ],
            group_by=[], scope={relation.name: spec.alias},
            root_source=relation.name, binding="fact_table", params={},
        )

    if isinstance(relation, Filter):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        _validate_contains_filter_conjunction(relation.where, inner)
        compiled = compile_condition(relation.where, inner)
        plan.where.append(compiled.sql)
        plan.params.update(compiled.params)
        return plan

    if isinstance(relation, Join):
        left = compile_relation(relation.left, context)
        if left.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 조인할 수 없습니다")
        if relation.kind == "inner" and isinstance(relation.right, Source):
            # Exact legacy shape: the joined source is governed by ON, not by
            # an additional subject correlation predicate.
            right_spec = context.event_spec(relation.right.name)
            if right_spec.from_sql:
                raise SqlCompileError("복합 물리 소스는 Join 오른쪽이 아니라 독립 Source 로 사용해야 합니다")
            left.scope[relation.right.name] = right_spec.alias
            on_sql = compile_condition(relation.on, _relation_context(left, context))
            left.from_sql += f" INNER JOIN {_source_sql(right_spec, context)} ON {on_sql.sql}"
            left.where.extend(_extra_predicates(right_spec, context))
            left.params.update(on_sql.params)
            return left

        right = compile_relation(relation.right, context)
        if right.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 파생 관계 조인 오른쪽에 사용할 수 없습니다")
        if relation.kind in {"semi", "anti"}:
            return _compile_membership_join(relation, left, right, context)
        return _compile_derived_inner_join(relation, left, right, context)

    if isinstance(relation, Group):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        plan.group_by.extend(compile_scalar(key, inner) for key in relation.keys)
        return plan

    if isinstance(relation, Project):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        projection: list[str] = []
        aliases: dict[str, str] = {}
        expressions: dict[str, str] = {}
        for item in relation.items:
            expression = compile_scalar(item.expression, inner)
            projection.append(f"{expression} AS {item.name}")
            aliases[item.name] = item.name
            expressions[item.name] = expression
            if isinstance(item.expression, FieldRef):
                aliases[item.expression.name] = item.name
                expressions[item.expression.name] = expression
        plan.projection = projection
        plan.output_aliases = aliases
        plan.output_expressions = expressions
        return plan

    if isinstance(relation, Summarize):
        plan = compile_relation(relation.relation, context)
        if plan.group_by or plan.projection or plan.order_by or plan.limit is not None:
            raise SqlCompileError("summarize 입력은 아직 materialize 된 관계를 지원하지 않습니다")
        inner = _relation_context(plan, context)
        projection = []
        aliases = {}
        expressions = {}
        group_by: list[str] = []
        for key in relation.keys:
            expression = compile_scalar(key.expression, inner)
            projection.append(f"{expression} AS {key.name}")
            group_by.append(expression)
            aliases[key.name] = key.name
            expressions[key.name] = expression
            if isinstance(key.expression, FieldRef):
                aliases[key.expression.name] = key.name
                expressions[key.expression.name] = expression
        for measure in relation.measures:
            expression = _named_measure_expression(measure, inner)
            projection.append(f"{expression} AS {measure.name}")
            aliases[measure.name] = measure.name
            expressions[measure.name] = expression
        plan.projection = projection
        plan.output_aliases = aliases
        plan.output_expressions = expressions
        plan.group_by = group_by
        return plan

    if isinstance(relation, Order):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        order_by: list[str] = []
        for sort_key in relation.keys:
            sort_expression = plan.output_expressions.get(sort_key.name)
            if sort_expression is None and "." in sort_key.name:
                sort_expression = compile_scalar(FieldRef(sort_key.name), inner)
            if sort_expression is None:
                raise SqlCompileError(f"정렬 출력이 관계에 없습니다: {sort_key.name}")
            order_by.append(f"{sort_expression} {sort_key.direction.upper()}")
        plan.order_by = order_by
        return plan

    if isinstance(relation, Limit):
        plan = compile_relation(relation.relation, context)
        requested = (
            RowLimit("count", relation.count)
            if relation.count is not None
            else RowLimit("percent", relation.percent)
        )
        if plan.limit is None:
            plan.limit = requested
        elif plan.limit.unit == requested.unit == "count":
            plan.limit = RowLimit("count", min(plan.limit.value, requested.value))
        else:
            raise SqlCompileError(
                "중첩 percent 제한은 모집단이 달라질 수 있어 정확히 컴파일할 수 없습니다"
            )
        return plan

    raise SqlCompileError(f"지원하지 않는 관계입니다: {relation!r}")


def _relation_context(plan: RelationPlan, context: CompileContext) -> CompileContext:
    return context.with_scope(plan.scope).with_field_bindings(plan.field_bindings)


def _named_measure_expression(
    measure: event_ir.NamedMeasure, context: CompileContext
) -> str:
    if isinstance(measure.expression, Tuple):
        # Summarize 측정치는 GROUP BY 결과 행의 스칼라다 — 행 값 distinct 는 이 자리에서
        # 방언 독립으로 낮출 수 없다(서브쿼리 재작성이 그룹 경계를 넘는다).
        raise SqlCompileError(
            "요약 측정치에는 행 값 distinct 집계를 사용할 수 없습니다"
        )
    argument = "*" if measure.expression is None else compile_scalar(measure.expression, context)
    distinct = "DISTINCT " if measure.distinct and argument != "*" else ""
    return f"{measure.function.upper()}({distinct}{argument})"


def _merge_params(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    duplicated = target.keys() & incoming.keys()
    if duplicated:
        raise SqlCompileError(f"SQL 파라미터 이름이 중복되었습니다: {sorted(duplicated)}")
    target.update(incoming)


def _derived_field_bindings(plan: RelationPlan, alias: str) -> dict[str, str]:
    return {
        symbol: f"{alias}.{column}"
        for symbol, column in plan.output_aliases.items()
        if "." in symbol
    }


def _compile_membership_join(
    relation: Join,
    left: RelationPlan,
    right: RelationPlan,
    context: CompileContext,
) -> RelationPlan:
    materialized = bool(
        right.projection or right.group_by or right.order_by or right.limit is not None
    )
    if materialized:
        if not right.projection:
            raise SqlCompileError("파생 조인 관계에는 명시적 출력이 필요합니다")
        alias = f"ER{context.next_index()}"
        bindings = _derived_field_bindings(right, alias)
        # Join.on has a directional contract: its left scalar is evaluated in
        # the left relation and its right scalar in the right relation.  A
        # self-semi-join legitimately uses the same canonical field on both
        # sides; merging bindings first would turn ``left.id = right.id`` into
        # the tautology ``right.id = right.id``.
        left_context = _relation_context(left, context)
        right_context = context.with_field_bindings(bindings)
        on_sql = CompiledCondition(
            sql=(
                f"{compile_scalar(relation.on.left, left_context)} "
                f"{relation.on.operator} "
                f"{compile_scalar(relation.on.right, right_context)}"
            )
        )
        body = _subquery(right, None, context)
        predicate = f"EXISTS (SELECT 1 FROM ({body}) AS {alias} WHERE {on_sql.sql})"
    else:
        scope = {**left.scope, **right.scope}
        bindings = {**left.field_bindings, **right.field_bindings}
        on_context = context.with_scope(scope).with_field_bindings(bindings)
        on_sql = compile_condition(relation.on, on_context)
        right.where.append(on_sql.sql)
        predicate = f"EXISTS ({_subquery(right, '1', context)})"
    if relation.kind == "anti":
        predicate = "NOT " + predicate
    left.where.append(predicate)
    _merge_params(left.params, right.params)
    _merge_params(left.params, on_sql.params)
    return left


def _compile_derived_inner_join(
    relation: Join,
    left: RelationPlan,
    right: RelationPlan,
    context: CompileContext,
) -> RelationPlan:
    if not right.projection:
        raise SqlCompileError("파생 inner join 오른쪽에는 명시적 출력이 필요합니다")
    alias = f"ER{context.next_index()}"
    bindings = _derived_field_bindings(right, alias)
    on_context = _relation_context(left, context).with_field_bindings(bindings)
    on_sql = compile_condition(relation.on, on_context)
    left.from_sql += f" INNER JOIN ({_subquery(right, None, context)}) AS {alias} ON {on_sql.sql}"
    left.field_bindings.update(bindings)
    _merge_params(left.params, right.params)
    _merge_params(left.params, on_sql.params)
    return left


def _subquery(
    plan: RelationPlan, projection: str | None, context: CompileContext
) -> str:
    selected = projection if projection is not None else ", ".join(plan.projection or ["*"])
    try:
        rendered_limit = (
            context.dialect.render_row_limit(plan.limit)
            if plan.limit is not None
            else sql_dialect.RenderedRowLimit()
        )
    except UnsupportedDialectFeatureError as exc:
        raise SqlCompileError(str(exc)) from exc
    parts = [f"SELECT {rendered_limit.prefix}{selected} FROM {plan.from_sql}"]
    if plan.where:
        parts.append("WHERE " + " AND ".join(plan.where))
    if plan.group_by:
        parts.append("GROUP BY " + ", ".join(plan.group_by))
    if plan.order_by:
        parts.append("ORDER BY " + ", ".join(plan.order_by))
    if rendered_limit.suffix:
        parts.append(rendered_limit.suffix)
    return " ".join(parts)


# ── 스칼라 ────────────────────────────────────────────────────────────────────────


def compile_scalar(scalar: event_ir.Scalar, context: CompileContext) -> str:
    if isinstance(scalar, Literal):
        return _sql_quote(scalar.value) if isinstance(scalar.value, str) else str(scalar.value)

    if isinstance(scalar, FieldRef):
        bound = context._field_bindings.get(scalar.name)
        if bound is not None:
            return bound
        spec = context.field_spec(scalar.name)
        alias = context._scope.get(spec.source) or (
            context.subject.alias if spec.source == context.subject.name else None
        )
        if alias is None:
            raise SqlCompileError(f"'{scalar.name}' 을 참조할 관계가 현재 스코프에 없습니다")
        if spec.expression:
            try:
                return spec.expression.format(
                    alias=alias,
                    subject_alias=context.subject.alias,
                    subject_key=context.subject.key,
                )
            except (KeyError, ValueError) as exc:
                raise SqlCompileError(f"필드 '{scalar.name}' 물리 바인딩 형식이 잘못되었습니다") from exc
        return f"{alias}.{spec.column}"

    if isinstance(scalar, Arithmetic):
        return f"({compile_scalar(scalar.left, context)} {scalar.operator} {compile_scalar(scalar.right, context)})"

    if isinstance(scalar, NullIf):
        return context.dialect.null_if(
            compile_scalar(scalar.expression, context),
            compile_scalar(scalar.value, context),
        )

    if isinstance(scalar, Tuple):
        # 행 값은 자기 자리가 정해져 있다(집계 distinct 인자). 여기까지 왔다는 것은 그 밖의
        # 자리에 놓였다는 뜻이므로 조용히 이어 붙이지 않고 멈춘다.
        raise SqlCompileError(
            "행 값(tuple)은 count(distinct ...) 인자 자리에서만 컴파일됩니다"
        )

    if isinstance(scalar, Aggregate):
        plan = compile_relation(scalar.relation, context)
        if plan.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 집계할 수 없습니다")
        if plan.group_by:
            raise SqlCompileError("그룹 집계는 비교 조건에서만 컴파일됩니다(EXISTS + HAVING)")
        return _aggregate_subquery(scalar, plan, context)

    raise SqlCompileError(f"지원하지 않는 스칼라입니다: {scalar!r}")


def _tuple_parts(expression: Tuple, plan: RelationPlan, context: CompileContext) -> tuple[str, ...]:
    inner = context.with_scope(plan.scope)
    return tuple(compile_scalar(item, inner) for item in expression.items)


def _distinct_tuple_count_subquery(
    aggregate: Aggregate, plan: RelationPlan, context: CompileContext
) -> str:
    """``COUNT(DISTINCT (a, b, …))`` → DISTINCT 서브쿼리 위의 ``COUNT(*)``.

    네이티브 다중 컬럼 문법을 쓰지 않는 이유는 :mod:`sql_dialect` 의 주석에 있다 — MySQL 은
    NULL 인자를 가진 행을 세지 않고 PostgreSQL 은 세므로, 같은 IR 이 방언에 따라 다른 수를
    센다. 구분자 CONCAT 도 쓰지 않는다: 값 안의 구분자나 NULL 이 서로 다른 키를 같은 문자열로
    만든다. 이 모양은 네 방언 모두에서 '서로 다른 값 조합의 개수' 하나만 뜻한다.
    """

    assert isinstance(aggregate.expression, Tuple)
    parts = _tuple_parts(aggregate.expression, plan, context)
    alias = f"ED{context.next_index()}"
    inner = _subquery(plan, "DISTINCT " + ", ".join(parts), context)
    return f"(SELECT COUNT(*) FROM ({inner}) AS {alias})"


def _aggregate_expression(aggregate: Aggregate, plan: RelationPlan, context: CompileContext) -> str:
    inner = context.with_scope(plan.scope)
    if isinstance(aggregate.expression, Tuple):
        raise SqlCompileError(
            "행 값 distinct 집계는 스칼라 식이 아니라 DISTINCT 서브쿼리로 낮춥니다"
        )
    argument = "*" if aggregate.expression is None else compile_scalar(aggregate.expression, inner)
    distinct = "DISTINCT " if aggregate.distinct and argument != "*" else ""
    return f"{aggregate.function.upper()}({distinct}{argument})"


def _aggregate_subquery(aggregate: Aggregate, plan: RelationPlan, context: CompileContext) -> str:
    if isinstance(aggregate.expression, Tuple):
        return _distinct_tuple_count_subquery(aggregate, plan, context)
    expression = _aggregate_expression(aggregate, plan, context)
    # 행이 없을 때 SUM 은 NULL 을 돌려줘 '0원 이상' 같은 조건이 조용히 거짓이 된다 — 0 으로 접는다.
    # COUNT 는 0 을 돌려주므로 감싸지 않는다(불필요한 함수 중첩은 인덱스 판단만 흐린다).
    projection = context.dialect.coalesce(expression, "0") if aggregate.function == "sum" else expression
    return f"({_subquery(plan, projection, context)})"


# ── 조건 ──────────────────────────────────────────────────────────────────────────


def compile_condition(condition: event_ir.Condition, context: CompileContext) -> CompiledCondition:
    if isinstance(condition, And):
        return _combine(condition.operands, " AND ", context)
    if isinstance(condition, Or):
        return _combine(condition.operands, " OR ", context)
    if isinstance(condition, Not):
        if isinstance(condition.operand, Comparison):
            # contains 검색의 부정은 일반 2값 보수와 다르다. 상품마스터에 매핑되지 않아 모든
            # 이름/카테고리가 NULL인 행을 '다른 상품'의 증거로 삼을 수 없으므로, 전용 !=
            # 컴파일이 검색값 존재 가드를 함께 붙인다.
            left, right = condition.operand.left, condition.operand.right
            if isinstance(left, FieldRef) and isinstance(right, Literal):
                inverted = {"=": "!=", "!=": "="}.get(condition.operand.operator)
                if inverted is not None:
                    text_search = _compile_text_search_comparison(left, right, inverted, context)
                    if text_search is not None:
                        return text_search
                    if context.field_spec(left.name).negative_null_policy_declared:
                        return _compile_comparison(
                            Comparison(
                                inverted,
                                condition.operand.left,
                                condition.operand.right,
                                condition.operand.evidence,
                            ),
                            context,
                        )
            elif isinstance(right, FieldRef) and isinstance(left, Literal):
                inverted = {"=": "!=", "!=": "="}.get(condition.operand.operator)
                if inverted is not None:
                    text_search = _compile_text_search_comparison(right, left, inverted, context)
                    if text_search is not None:
                        return text_search
                    if context.field_spec(right.name).negative_null_policy_declared:
                        return _compile_comparison(
                            Comparison(
                                inverted,
                                condition.operand.left,
                                condition.operand.right,
                                condition.operand.evidence,
                            ),
                            context,
                        )
        inner = compile_condition(condition.operand, context)
        # NOT EXISTS 는 SQL 이 직접 지원하는 형태라 NOT (EXISTS ...) 로 감싸지 않는다(플랜 동일, 가독성 우위).
        if inner.sql.startswith("EXISTS ("):
            return CompiledCondition(sql="NOT " + inner.sql, params=inner.params)
        # 일부 nullable Y/N 필드는 승인된 업무 정책상 'Y가 아닌 모든 값'이 아니라 **명시적 N**만
        # 대상이다. 그런 필드의 직접 부정은 SQL 3값 논리를 보존해 NULL을 제외한다. 정책은 catalog
        # FieldSpec이 소유하며, 복합식이나 미등록 필드에 추론으로 확장하지 않는다.
        if isinstance(condition.operand, Comparison):
            comparison_fields = [
                scalar.name
                for scalar in (condition.operand.left, condition.operand.right)
                if isinstance(scalar, FieldRef)
            ]
            if any(
                context.field_spec(field_name).negative_null_policy == "exclude_unknown"
                for field_name in comparison_fields
            ):
                return CompiledCondition(sql=f"NOT ({inner.sql})", params=inner.params)
        # SQL's NOT UNKNOWN is still UNKNOWN, while an audience complement is
        # two-valued: members for whom the predicate is not true (including
        # NULL/unknown) must remain.  Fold the predicate to 1/0 before NOT.
        complement = f"NOT (CASE WHEN ({inner.sql}) THEN 1 ELSE 0 END = 1)"
        # A compound product-text negation (for example ``NOT (A OR B)``) means
        # "another known product", not "a purchase row whose dimension join is
        # missing".  Direct Comparison negation is handled above; apply the same
        # fail-closed searchability guard to compound Boolean shapes.  We stop at
        # Exists boundaries so ``NOT EXISTS(product=A)`` keeps audience-absence
        # semantics and does not require any matching product row to exist.
        searchable = _contains_comparison_search_expressions(condition.operand, context)
        if searchable:
            present = " OR ".join(f"{column} IS NOT NULL" for column in searchable)
            complement = f"({present}) AND {complement}"
        return CompiledCondition(sql=complement, params=inner.params)
    if isinstance(condition, TimeFilter):
        spec = context.field_spec(condition.field.name)
        return compile_time_window(
            compile_scalar(condition.field, context), condition.window,
            f"event_{context.next_index()}", data_type=spec.data_type, context=context,
        )
    if isinstance(condition, Exists):
        return _compile_exists(condition, context)
    if isinstance(condition, Comparison):
        return _compile_comparison(condition, context)
    if isinstance(condition, TemporalRelation):
        return _compile_temporal_relation(condition, context)
    raise SqlCompileError(f"지원하지 않는 조건입니다: {condition!r}")


def _combine(
    operands: tuple[event_ir.Condition, ...], joiner: str, context: CompileContext
) -> CompiledCondition:
    compiled = [compile_condition(operand, context) for operand in operands]
    if len(compiled) == 1:
        return compiled[0]
    params: dict[str, Any] = {}
    for item in compiled:
        duplicated = params.keys() & item.params.keys()
        if duplicated:
            raise SqlCompileError(f"SQL 파라미터 이름이 중복되었습니다: {sorted(duplicated)}")
        params.update(item.params)
    return CompiledCondition(sql=joiner.join(f"({item.sql})" for item in compiled), params=params)


def _compile_exists(condition: Exists, context: CompileContext) -> CompiledCondition:
    plan = compile_relation(condition.relation, context)
    if plan.binding == "subject_column":
        # 주체 컬럼 사건의 '존재'는 값이 있느냐다. 창이 붙었으면 Filter 가 이미 술어를 만들어 뒀다.
        spec = context.event_spec(plan.root_source)
        predicates = [
            f"{_event_time_sql(spec, context, alias=context.subject.alias)} IS NOT NULL",
            *plan.where,
        ]
        return CompiledCondition(sql="(" + " AND ".join(predicates) + ")", params=plan.params)
    return CompiledCondition(sql=f"EXISTS ({_subquery(plan, '1', context)})", params=plan.params)


# 필드가 오른쪽에 오는 비교('VIP <= 등급')를 왼쪽 기준으로 뒤집는다.
_MIRRORED_OPERATORS = {">=": "<=", "<=": ">=", ">": "<", "<": ">"}
_ORDINAL_OPERATORS = frozenset(_MIRRORED_OPERATORS)


def _compile_ordinal_comparison(
    field: FieldRef, literal: Literal, operator: str, context: CompileContext
) -> CompiledCondition | None:
    """등급처럼 **순서 있는 코드 도메인**의 부등호를 물리값 IN 목록으로 컴파일한다.

    None 을 돌려주면 서열 비교가 아니라는 뜻이다(=/≠ 이거나 순서 없는 도메인이 아닌 일반 필드).
    순서가 선언되지 않은 코드 도메인(회원 상태: 정상/휴면/탈퇴)에 부등호가 오면 **fail-close** 한다 —
    저장값 문자열을 부등호로 비교하면 아무 의미 없는 집합이 조용히 나온다.
    """
    if operator not in _ORDINAL_OPERATORS:
        return None
    spec = context.field_spec(field.name)
    if not spec.value_map:
        return None  # 값 도메인이 없는 일반 필드(숫자·날짜)는 원래 부등호가 옳다.
    if not spec.value_order:
        raise SqlCompileError(
            f"'{field.name}' 은 순서가 선언되지 않은 값 도메인이라 크기 비교를 표현할 수 없습니다"
            " — '<값> 이상' 대신 값을 직접 나열해 주세요"
        )
    values = spec.ordered_values(operator, literal.value)
    if not values:
        raise SqlCompileError(f"'{field.name}' 의 이 조건을 만족하는 값이 도메인에 없습니다: {literal.value!r}")
    column = compile_scalar(field, context)
    rendered = ", ".join(_sql_quote(value) for value in values)
    return CompiledCondition(sql=f"{column} IN ({rendered})")


def _text_search_expressions(field: FieldRef, context: CompileContext) -> tuple[str, ...]:
    """catalog의 contains 바인딩을 현재 relation alias에 맞춰 렌더한다."""
    spec = context.field_spec(field.name)
    if spec.match_mode != "contains":
        return ()
    if not spec.search_expressions:
        return (compile_scalar(field, context),)
    alias = context._scope.get(spec.source) or (
        context.subject.alias if spec.source == context.subject.name else None
    )
    if alias is None:
        raise SqlCompileError(f"'{field.name}' 을 참조할 관계가 현재 스코프에 없습니다")
    rendered: list[str] = []
    for template in spec.search_expressions:
        try:
            expression = template.format(
                alias=alias,
                subject_alias=context.subject.alias,
                subject_key=context.subject.key,
            )
        except (KeyError, ValueError) as exc:
            raise SqlCompileError(f"필드 '{field.name}' 검색 바인딩 형식이 잘못되었습니다") from exc
        if expression not in rendered:
            rendered.append(expression)
    if not rendered:
        raise SqlCompileError(f"필드 '{field.name}' 검색 바인딩이 비어 있습니다")
    return tuple(rendered)


def _contains_comparison_search_expressions(
    condition: event_ir.Condition, context: CompileContext
) -> tuple[str, ...]:
    """Search columns used by contains comparisons in one Boolean row scope.

    Relation-valued atoms deliberately form a boundary: their aliases live in
    a subquery, and especially ``Not(Exists(...))`` represents member-level
    absence rather than a negated property of an existing product row.
    """
    if isinstance(condition, (And, Or)):
        expressions = [
            expression
            for operand in condition.operands
            for expression in _contains_comparison_search_expressions(operand, context)
        ]
        return tuple(dict.fromkeys(expressions))
    if isinstance(condition, Not):
        return _contains_comparison_search_expressions(condition.operand, context)
    if not isinstance(condition, Comparison) or condition.operator not in {"=", "!="}:
        return ()
    field_ref: FieldRef | None = None
    if isinstance(condition.left, FieldRef) and isinstance(condition.right, Literal):
        field_ref = condition.left
    elif isinstance(condition.right, FieldRef) and isinstance(condition.left, Literal):
        field_ref = condition.right
    if field_ref is None or context.field_spec(field_ref.name).match_mode != "contains":
        return ()
    return _text_search_expressions(field_ref, context)


def _compile_text_search_comparison(
    field: FieldRef,
    literal: Literal,
    operator: str,
    context: CompileContext,
) -> CompiledCondition | None:
    """contains 필드의 ``=``/``!=``를 상품마스터 LIKE 술어로 컴파일한다.

    IR의 보편 비교 연산자를 늘리지 않고, 해당 필드의 물리 매칭 정책만 catalog가 바꾼다.
    ``Not(Comparison('='))``도 기존 불리언 대수 그대로 동작하므로 미구매/제외/다른 상품을
    위한 전용 노드를 만들 필요가 없다.
    """
    expressions = _text_search_expressions(field, context)
    if not expressions:
        return None
    if operator not in {"=", "!="}:
        raise SqlCompileError(
            f"부분 문자열 검색 필드 '{field.name}'는 = 또는 != 비교만 지원합니다"
        )
    if not isinstance(literal.value, str) or not literal.value.strip():
        raise SqlCompileError(f"부분 문자열 검색 필드 '{field.name}'에는 비어 있지 않은 문자열이 필요합니다")
    term = literal.value.strip()
    params: dict[str, Any] = {}
    if context.literals:
        matches = " OR ".join(
            _nlike_contains_literal(column, term) for column in expressions
        )
    else:
        parameter_name = f"text_search_{context.next_index()}"
        placeholder = f":{parameter_name}"
        matches = " OR ".join(
            f"{column} LIKE {placeholder} ESCAPE N'~'" for column in expressions
        )
        params[parameter_name] = _like_contains_pattern(term)
    predicate = f"({matches})"
    if operator == "=":
        return CompiledCondition(sql=predicate, params=params)
    # SQL NULL의 3값 논리 때문에 단순 NOT LIKE/NOT(...)를 쓰지 않는다. 어떤 이름/카테고리도
    # 일치하지 않는 행이라는 audience 보수를 CASE로 2값화한다. 동시에 상품마스터 미매핑으로
    # 검색 표현이 전부 NULL인 행은 '다른 상품'이라고 증명할 수 없으므로 fail-close 한다.
    searchable = " OR ".join(f"{column} IS NOT NULL" for column in expressions)
    return CompiledCondition(
        sql=f"({searchable}) AND NOT (CASE WHEN ({predicate}) THEN 1 ELSE 0 END = 1)",
        params=params,
    )


# ── 집합형 집계 lowering(물리 최적화) ────────────────────────────────────────────
#
# 회원 상관 스칼라 집계는 바깥 회원 **수만큼** 팩트 집계를 반복한다. 실측(2026-08-04, CRMDW):
# 회원 69,287행 × Z_CAMP_MBR 상관 집계 = 예상 비용 19118.6, rebind/rewind 약 69,286회로 앱의
# 15초 timeout 을 넘겼다. 같은 의미를 한 번의 GROUP BY 로 낮추면 팩트 스캔이 한 번이 된다
# (같은 DB·같은 통계에서 예상 비용 3.70648).
#
# 이 변환은 Event IR 의미 변경이 **아니다** — IR·지문·query identity 는 그대로이고 물리 SQL 만
# 바뀐다. 의미 동치의 유일한 조건은 "이벤트가 하나도 없는 회원이 거짓"이다: 상관 스칼라는 그런
# 회원에게 COUNT=0 을 주지만 GROUP BY 형태에는 그 회원의 그룹 자체가 없다. 그래서 연산자
# 허용목록이 아니라 **임계값 0 에서 비교를 계산**해 거짓일 때만 낮춘다. 그러면 '>= 3' 뿐 아니라
# '> 0' · '= 2' · '!= 0' 이 자동으로 열리고 '= 0' · '<= 1' · '< 5' 는 자동으로 닫힌다.
AGGREGATE_MEMBERSHIP_OPTIMIZATION = "aggregate_membership_semi_join"

# 스킵 사유 코드(안정 문자열 — 진단이 코드로 비교된다).
SKIP_NO_CORRELATED_AGGREGATE = "NO_CORRELATED_AGGREGATE"
SKIP_SUBJECT_COLUMN_FAST_PATH = "SUBJECT_COLUMN_FAST_PATH"
SKIP_ALREADY_SET_BASED = "ALREADY_SET_BASED"
SKIP_UNSUPPORTED_SCOPE = "UNSUPPORTED_OPTIMIZATION_SCOPE"
SKIP_ZERO_SENSITIVE_COMPARISON = "ZERO_SENSITIVE_COMPARISON"
SKIP_NO_GROUP_SUBJECT_BINDING = "NO_GROUP_SUBJECT_BINDING"
SKIP_SUBJECT_REFERENCE_INSIDE_AGGREGATE = "SUBJECT_REFERENCE_INSIDE_AGGREGATE"
SKIP_OPTIMIZATION_DISABLED = "OPTIMIZATION_DISABLED"
SKIP_RESIDUAL_SUBJECT_CORRELATION = "RESIDUAL_SUBJECT_CORRELATION"

# 임계값 0(=이벤트가 없는 회원)에서의 비교 진리값. 거짓이어야만 낮출 수 있다.
_ZERO_COMPARISONS: dict[str, Callable[[Any], bool]] = {
    "=": lambda threshold: 0 == threshold,
    "!=": lambda threshold: 0 != threshold,
    ">": lambda threshold: 0 > threshold,
    ">=": lambda threshold: 0 >= threshold,
    "<": lambda threshold: 0 < threshold,
    "<=": lambda threshold: 0 <= threshold,
}
# 집계가 오른쪽에 온 비교('3 <= count')를 집계 기준으로 뒤집는다. =/!= 는 자기 자신이다.
_MIRRORED_COMPARISONS = {**_MIRRORED_OPERATORS, "=": "=", "!=": "!="}
# 빈 집합에서 0 을 돌려주는 집계만 대상이다. SUM/AVG 는 빈 집합에서 NULL 이고(SUM 은 COALESCE
# 로 접히지만 음수 합이 가능하다) 0 판정이 임계값 의미와 일치한다는 증명이 따로 필요하다.
_EMPTY_SET_ZERO_FUNCTIONS = frozenset({"count"})


def _record_optimization(
    context: CompileContext,
    *,
    status: str,
    source: str | None,
    reason: str | None = None,
    node: event_ir.Condition | None = None,
) -> None:
    if context.optimization_receipts is None:
        return
    receipt: dict[str, Any] = {
        "optimization": AGGREGATE_MEMBERSHIP_OPTIMIZATION,
        "status": status,
    }
    if source is not None:
        receipt["source"] = source
    if reason is not None:
        receipt["reason"] = reason
    if node is not None:
        # 표현 지문 — 최적화 전후 IR 이 같다는 증거이며 query identity 계산에는 들어가지 않는다.
        receipt["preserved_expression_fingerprint"] = _capability_node_id(node)
    if receipt not in context.optimization_receipts:
        context.optimization_receipts.append(receipt)


def _membership_operands(
    condition: Comparison,
) -> tuple[Aggregate | None, Literal | None, str]:
    """집계-리터럴 비교를 **집계 기준**으로 정규화한다. 아니면 첫 값이 None."""
    if isinstance(condition.left, Aggregate) and isinstance(condition.right, Literal):
        return condition.left, condition.right, condition.operator
    if isinstance(condition.right, Aggregate) and isinstance(condition.left, Literal):
        mirrored = _MIRRORED_COMPARISONS.get(condition.operator)
        if mirrored is None:
            return None, None, condition.operator
        return condition.right, condition.left, mirrored
    return None, None, condition.operator


def _uncorrelated_relation(relation: event_ir.Relation) -> event_ir.Relation | None:
    """``Filter*(Source(correlation='subject'))`` 를 같은 필터의 **비상관** 관계로 다시 만든다.

    원본 IR 객체는 건드리지 않는다(frozen dataclass 를 새로 만든다). 조인·그룹·정렬·제한이
    끼어 있으면 None — 초기 적용 범위를 구조적으로 명백한 모양으로 좁힌다.
    """
    if isinstance(relation, Source):
        if relation.correlation != "subject":
            return None
        return Source(name=relation.name, correlation="none")
    if isinstance(relation, Filter):
        inner = _uncorrelated_relation(relation.relation)
        if inner is None:
            return None
        return Filter(relation=inner, where=relation.where)
    return None


def _relation_root_source(relation: event_ir.Relation) -> Source | None:
    current = relation
    while isinstance(current, Filter):
        current = current.relation
    return current if isinstance(current, Source) else None


def _membership_skip_reason(
    aggregate: Aggregate, literal: Literal, operator: str, context: CompileContext
) -> str | None:
    """DB 조회 없이 IR 한 번만 훑어 적용 가능성을 판정한다(없으면 None)."""
    if not context.optimize_aggregate_membership:
        return SKIP_OPTIMIZATION_DISABLED
    if aggregate.function not in _EMPTY_SET_ZERO_FUNCTIONS:
        return SKIP_UNSUPPORTED_SCOPE
    if isinstance(aggregate.expression, Tuple):
        # 행 값 distinct 는 HAVING 절의 스칼라 집계식으로 표현할 수 없다(그 자리가 정확히
        # 네이티브 다중 컬럼 문법이 필요한 곳이다). 상관 서브쿼리 경로가 정확한 번역이다.
        return SKIP_UNSUPPORTED_SCOPE
    zero_test = _ZERO_COMPARISONS.get(operator)
    if zero_test is None or not isinstance(literal.value, (int, Decimal)) or isinstance(literal.value, bool):
        return SKIP_UNSUPPORTED_SCOPE
    if zero_test(literal.value):
        # 이벤트가 없는 회원도 참이 되는 조건(COUNT = 0, <= 1, < 5 …). 집합형 semi-join 은 그런
        # 회원을 표현하지 못하므로 낮추지 않는다 — LEFT JOIN/anti-join 은 별도 동치 증명이 필요하다.
        return SKIP_ZERO_SENSITIVE_COMPARISON
    if any(
        isinstance(node, (Group, Join, Project, Summarize, Order, Limit))
        for node in event_ir.walk(aggregate)
    ):
        # 이미 grain 이 회원이 아니거나 관계가 materialize 돼 있다 — 다시 낮추지 않는다.
        return SKIP_ALREADY_SET_BASED
    source = _relation_root_source(aggregate.relation)
    if source is None or source.correlation != "subject":
        return SKIP_NO_CORRELATED_AGGREGATE
    spec = context.registry.get(source.name)
    if spec is None:
        return SKIP_UNSUPPORTED_SCOPE
    if spec.binding != "fact_table":
        return SKIP_SUBJECT_COLUMN_FAST_PATH
    if _group_subject_sql(spec, context) is None:
        return SKIP_NO_GROUP_SUBJECT_BINDING
    for node in event_ir.walk(aggregate):
        if isinstance(node, Aggregate) and node is not aggregate:
            return SKIP_UNSUPPORTED_SCOPE
        if isinstance(node, FieldRef):
            referenced = (context.fields or {}).get(node.name)
            if referenced is None or referenced.source != source.name:
                # 바깥 주체(또는 다른 소스)를 참조하면 group key 로 묶는 순간 의미가 달라진다.
                return SKIP_SUBJECT_REFERENCE_INSIDE_AGGREGATE
    return None


def _aggregate_membership_predicate(
    aggregate: Aggregate, literal: Literal, operator: str, context: CompileContext
) -> CompiledCondition | None:
    """상관 스칼라 집계 비교 → 비상관 GROUP BY 서브쿼리에 대한 회원 semi-join."""
    source = _relation_root_source(aggregate.relation)
    relation = _uncorrelated_relation(aggregate.relation)
    if source is None or relation is None:
        return None
    spec = context.event_spec(source.name)
    plan = compile_relation(relation, context)
    if plan.binding != "fact_table" or plan.group_by or plan.projection or plan.order_by or plan.limit is not None:
        return None
    group_key = _group_subject_sql(spec, context)
    if group_key is None:
        return None
    expression = _aggregate_expression(aggregate, plan, context)
    # 변환 실패(TRY_CAST → NULL) 회원키는 NULL 그룹으로 모인다. non-null 회원키와는 어차피
    # 매칭되지 않지만, 명시적으로 제외해 IN 술어를 2값으로 유지한다.
    plan.where.append(f"{group_key} IS NOT NULL")
    plan.group_by = [group_key]
    having = f"{expression} {operator} {compile_scalar(literal, context)}"
    body = f"{_subquery(plan, group_key, context)} HAVING {having}"
    if re.search(rf"\b{re.escape(context.subject.alias)}\.", body):
        # 성능 구조 가드: 낮췄는데 바깥 주체 참조가 남았다면 상관이 사라지지 않은 것이다.
        # 이런 계획은 최적화가 아니라 잠재적 오답이므로 기존 경로로 fail-safe 한다.
        _record_optimization(
            context,
            status="skipped",
            source=source.name,
            reason=SKIP_RESIDUAL_SUBJECT_CORRELATION,
        )
        return None
    subject_key = f"{context.subject.alias}.{spec.subject_key}"
    return CompiledCondition(sql=f"{subject_key} IN ({body})", params=plan.params)


def _lower_aggregate_membership(
    condition: Comparison, context: CompileContext
) -> CompiledCondition | None:
    """저비용 fast-path: 집계 비교가 아니면 IR 한 노드만 보고 **즉시** 기존 경로로 돌려보낸다."""
    aggregate, literal, operator = _membership_operands(condition)
    if aggregate is None or literal is None:
        return None
    source = _relation_root_source(aggregate.relation)
    source_name = source.name if source is not None else None
    reason = _membership_skip_reason(aggregate, literal, operator, context)
    if reason is not None:
        _record_optimization(context, status="skipped", source=source_name, reason=reason)
        return None
    compiled = _aggregate_membership_predicate(aggregate, literal, operator, context)
    if compiled is None:
        _record_optimization(
            context, status="skipped", source=source_name, reason=SKIP_UNSUPPORTED_SCOPE
        )
        return None
    _record_optimization(
        context, status="applied", source=source_name, node=condition
    )
    return compiled


def _compile_comparison(condition: Comparison, context: CompileContext) -> CompiledCondition:
    # 회원 상관 스칼라 집계는 회원마다 팩트를 다시 집계한다 — 의미가 보존되는 모양에서만 집합형
    # semi-join 으로 낮춘다. 적용 불가면 아무 것도 컴파일하지 않고 즉시 기존 경로로 돌아간다
    # (파라미터 카운터도 건드리지 않으므로 기존 SQL 은 바이트 동일하다).
    lowered = _lower_aggregate_membership(condition, context)
    if lowered is not None:
        return lowered
    # 그룹 집계 비교('한 주문에 3개 이상')는 스칼라 서브쿼리로 표현할 수 없다 — grain 이 회원이 아니라
    # 그룹이므로 EXISTS + HAVING 이 정확한 번역이다.
    if isinstance(condition.left, Aggregate):
        plan = compile_relation(condition.left.relation, context)
        if plan.group_by:
            having = (
                f"{_aggregate_expression(condition.left, plan, context)} "
                f"{condition.operator} {compile_scalar(condition.right, context)}"
            )
            return CompiledCondition(
                sql=f"EXISTS ({_subquery(plan, '1', context)} HAVING {having})",
                params=plan.params,
            )
    left_scalar, right_scalar = condition.left, condition.right
    if isinstance(left_scalar, FieldRef) and isinstance(right_scalar, Literal):
        text_search = _compile_text_search_comparison(
            left_scalar, right_scalar, condition.operator, context
        )
        if text_search is not None:
            return text_search
        ordinal = _compile_ordinal_comparison(left_scalar, right_scalar, condition.operator, context)
        if ordinal is not None:
            return ordinal
        spec = context.field_spec(left_scalar.name)
        if condition.operator == "!=" and spec.negative_null_policy_declared:
            field_sql = compile_scalar(left_scalar, context)
            target_sql = compile_scalar(
                Literal(spec.physical_value(right_scalar.value)), context
            )
            if spec.negative_null_policy == "exclude_unknown":
                complement = spec.complementary_physical_value(right_scalar.value)
                if complement is None:
                    raise SqlCompileError(
                        f"'{left_scalar.name}'의 명시적 반대값이 catalog에 없습니다"
                    )
                return CompiledCondition(
                    sql=f"{field_sql} = {compile_scalar(Literal(complement), context)}"
                )
            return CompiledCondition(
                sql=f"NOT (CASE WHEN ({field_sql} = {target_sql}) THEN 1 ELSE 0 END = 1)"
            )
        right_scalar = Literal(spec.physical_value(right_scalar.value))
    elif isinstance(right_scalar, FieldRef) and isinstance(left_scalar, Literal):
        text_search = _compile_text_search_comparison(
            right_scalar, left_scalar, condition.operator, context
        )
        if text_search is not None:
            return text_search
        ordinal = _compile_ordinal_comparison(
            right_scalar, left_scalar, _MIRRORED_OPERATORS.get(condition.operator, condition.operator), context
        )
        if ordinal is not None:
            return ordinal
        spec = context.field_spec(right_scalar.name)
        if condition.operator == "!=" and spec.negative_null_policy_declared:
            field_sql = compile_scalar(right_scalar, context)
            target_sql = compile_scalar(
                Literal(spec.physical_value(left_scalar.value)), context
            )
            if spec.negative_null_policy == "exclude_unknown":
                complement = spec.complementary_physical_value(left_scalar.value)
                if complement is None:
                    raise SqlCompileError(
                        f"'{right_scalar.name}'의 명시적 반대값이 catalog에 없습니다"
                    )
                return CompiledCondition(
                    sql=f"{field_sql} = {compile_scalar(Literal(complement), context)}"
                )
            return CompiledCondition(
                sql=f"NOT (CASE WHEN ({field_sql} = {target_sql}) THEN 1 ELSE 0 END = 1)"
            )
        left_scalar = Literal(spec.physical_value(left_scalar.value))
    left = compile_scalar(left_scalar, context)
    right = compile_scalar(right_scalar, context)
    return CompiledCondition(sql=f"{left} {condition.operator} {right}")


def _event_time_anchor(reference: EventReference, context: CompileContext) -> str:
    """시간 관계의 기준 시점 — 팩트 테이블이면 MIN/MAX 상관 서브쿼리, 주체 컬럼이면 그 컬럼."""
    spec = context.event_spec(reference.source)
    if spec.binding == "subject_column":
        return _event_time_sql(spec, context, alias=context.subject.alias)
    aggregate = {"first": "MIN", "last": "MAX"}.get(reference.selector)
    if aggregate is None:
        raise SqlCompileError(f"시간 관계의 기준 시점은 first/last 여야 합니다(받은 값: {reference.selector})")
    alias = spec.alias + "1"
    predicates = [
        _correlation(spec, context, alias=alias),
        *_extra_predicates(spec, context, alias=alias),
    ]
    return (
        f"(SELECT {aggregate}({_event_time_sql(spec, context, alias=alias)}) "
        f"FROM {_source_sql(spec, context, alias=alias)} "
        f"WHERE {' AND '.join(predicates)})"
    )


def _compile_temporal_relation(condition: TemporalRelation, context: CompileContext) -> CompiledCondition:
    """'A 후 D 이내에 B' — 기준 시점을 스칼라로 잡고 대상 사건을 그 창 안에서 찾는다.

    소스 이름만 바꾸면 '가입 후 7일 이내 구매'·'배송 후 30일 이내 반품'에 그대로 쓰인다."""
    if condition.operator != "within_after":
        raise SqlCompileError(f"아직 지원하지 않는 시간 관계입니다: {condition.operator}")
    target = context.event_spec(condition.right.source)
    if target.binding != "fact_table":
        raise SqlCompileError("시간 관계의 대상 사건은 팩트 테이블이어야 합니다")
    days = condition.duration.days
    if days is None:
        raise SqlCompileError(
            f"'{target.time_column}' 은 날짜 단위 컬럼이라 {condition.duration.unit} 단위 관계를 표현할 수 없습니다"
        )

    # grain 스위치는 TIME_GRAINS 한 곳이 소유한다 — 여기 time_format 을 다시 읽으면 grain 지식이 두 벌이 된다.
    if TIME_GRAINS[target.time_format].unit != "day":
        raise SqlCompileError(
            f"'{target.time_column}' 은 {TIME_GRAINS[target.time_format].unit} 단위로 적재된 컬럼이라 "
            f"'{days}일 이내' 같은 시점 관계를 표현할 수 없습니다"
        )
    anchor = _event_time_anchor(condition.left, context)
    alias = target.alias + "2"
    upper = (
        context.dialect.char8_shift(anchor, days)
        if target.time_format == "char8"
        else context.dialect.date_add_days(anchor, days)
    )
    predicates = [
        _correlation(target, context, alias=alias),
        *_extra_predicates(target, context, alias=alias),
        # 기준 시점 **이후**부터(같은 사건이면 기준이 된 발생 자체를 다시 세지 않도록 초과 비교) 창 끝까지.
        f"{_event_time_sql(target, context, alias=alias)} > {anchor}",
        f"{_event_time_sql(target, context, alias=alias)} <= {upper}",
    ]
    return CompiledCondition(
        sql=f"EXISTS (SELECT 1 FROM {_source_sql(target, context, alias=alias)} "
            f"WHERE {' AND '.join(predicates)})"
    )


# ── 진입점 ────────────────────────────────────────────────────────────────────────


def compile_expression(
    expression: event_ir.Condition, *, context: CompileContext | None = None
) -> CompiledCondition:
    """조건 IR → SQL 조건. AND/OR 우선순위는 트리 모양 그대로 괄호로 보존한다."""
    return compile_condition(expression, context or CompileContext())


def compile_expression_sql(
    expression: event_ir.Condition,
    *,
    subject: SubjectSpec | None = None,
    registry: dict[str, EventSpec] | None = None,
    fields: dict[str, FieldSpec] | None = None,
    dialect: SqlDialect | None = None,
    today: date | None = None,
) -> str:
    """리터럴 인라인 SQL 조건 문자열(문자열 SQL 을 검증·가드하는 기존 빌더용 진입점)."""
    resolved = registry or dict(EVENT_REGISTRY)
    context = CompileContext(
        subject=subject or SubjectSpec(), registry=resolved,
        fields=fields or resolve_fields(resolved),
        dialect=dialect or get_dialect("tsql"), literals=True, today=today,
    )
    return compile_expression(expression, context=context).sql


# 이 컴파일러가 SQL 로 낮출 수 있는 표현 capability. 어휘의 소유자는 :mod:`event_ir` 이고
# 여기서는 **제공 가능 집합**만 선언한다. 방언 인자를 받는 이유는 이 집합이 방언에 따라 달라질
# 수 있기 때문이다 — 지금은 네 방언이 같지만(다중 컬럼 distinct 를 전부 DISTINCT 서브쿼리로
# 낮춘다), 방언별 차이가 생기면 이 함수 하나가 갈라진다.
#
# `metric.member_scalar` 는 여기 없다 — 그것은 표현 모양이 아니라 카탈로그 지표 계약이고,
# 제공자는 :mod:`member_scalar_metrics` 다.
_COMPILER_CAPABILITIES: frozenset[str] = frozenset({
    "scalar.arithmetic",
    "scalar.tuple",
    "scalar.null_if",
    "scalar.safe_divide",
    "aggregate.scalar",
    "aggregate.count_distinct",
    "aggregate.multi_column_count_distinct",
    "aggregate.derived_expression",
    "relation.membership_join",
    "relation.ranked_limit",
})


def compiler_capabilities(dialect: SqlDialect | None = None) -> frozenset[str]:
    """이 방언으로 낮출 수 있는 표현 capability 집합."""

    del dialect  # 현재는 방언과 무관하다(위 주석의 이유). 시그니처가 그 사실을 드러낸다.
    return _COMPILER_CAPABILITIES


def unsupported_capabilities(
    expression: event_ir.Condition, dialect: SqlDialect | None = None
) -> tuple[str, ...]:
    """표현이 요구하는데 컴파일러가 제공하지 못하는 capability(없으면 빈 튜플)."""

    return tuple(
        sorted(event_ir.expression_capabilities(expression) - compiler_capabilities(dialect))
    )


def supported_events(registry: dict[str, EventSpec] | None = None) -> frozenset[str]:
    return frozenset(registry or EVENT_REGISTRY)


def unsupported_events(
    expression: event_ir.Condition, registry: dict[str, EventSpec] | None = None
) -> list[str]:
    """레지스트리에 없는 사건 심볼 목록(컴파일 전 fail-close 판정용)."""
    known = supported_events(registry)
    subject_name = SubjectSpec().name
    return sorted(name for name in event_ir.sources(expression) if name not in known and name != subject_name)


def unsupported_fields(
    expression: event_ir.Condition,
    registry: dict[str, EventSpec] | None = None,
    fields: dict[str, FieldSpec] | None = None,
) -> list[str]:
    """레지스트리에 없는 필드 심볼 목록."""
    resolved = fields or resolve_fields(registry or EVENT_REGISTRY)
    return sorted(name for name in event_ir.field_names(expression) if name not in resolved)


def _semantic_payload(value: Any) -> Any:
    """노드 지문용 의미 정규형. 무엇이 출처인지는 :mod:`semantic_fields` 가 단일 소스로 소유한다."""
    return semantic_fields.strip_provenance(value)


def _capability_node_id(expression: event_ir.Condition) -> str:
    payload = json.dumps(
        _semantic_payload(expression.to_dict()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "event_node_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capability_leaves(expression: event_ir.Condition) -> list[event_ir.Condition]:
    if isinstance(expression, (And, Or)):
        return [leaf for child in expression.operands for leaf in _capability_leaves(child)]
    return [expression]


def _fresh_context(context: CompileContext) -> CompileContext:
    return CompileContext(
        subject=context.subject,
        registry=context.registry,
        fields=context.fields,
        dialect=context.dialect,
        literals=context.literals,
        today=context.today,
        optimize_aggregate_membership=context.optimize_aggregate_membership,
        # capability 판정은 컴파일 **가능성**만 본다 — 진단 채널을 공유하면 같은 노드의 receipt 가
        # 실제 컴파일 것과 섞인다.
        optimization_receipts=None,
        _field_bindings=context._field_bindings,
    )


def validate_compiler_capability(
    expression: event_ir.Condition,
    *,
    context: CompileContext | None = None,
) -> CompilerCapabilityResult:
    """Report physical compiler capability without changing semantic status.

    Capability is evaluated per Boolean leaf.  A mixture of supported and
    unsupported leaves is ``partially_supported`` and must never be projected
    to SQL by dropping the unsupported leaves.
    """

    active = context or CompileContext()
    issues: list[CompilerCapabilityIssue] = []
    supported: list[str] = []
    unsupported: list[str] = []
    for leaf in _capability_leaves(expression):
        node_id = _capability_node_id(leaf)
        leaf_issues: list[CompilerCapabilityIssue] = []
        for symbol in unsupported_events(leaf, active.registry):
            leaf_issues.append(CompilerCapabilityIssue("compiler_event_unregistered", node_id, symbol))
        for symbol in unsupported_fields(leaf, active.registry, active.fields):
            leaf_issues.append(CompilerCapabilityIssue("compiler_field_unregistered", node_id, symbol))
        # capability 는 컴파일을 시도하기 **전에** 답한다. 시도해서 터진 예외로만 판정하면
        # 사용자에게 나가는 사유가 '알 수 없는 연산'으로 뭉개진다.
        for symbol in unsupported_capabilities(leaf, active.dialect):
            leaf_issues.append(
                CompilerCapabilityIssue("compiler_capability_unsupported", node_id, symbol)
            )
        if not leaf_issues:
            try:
                compile_expression(leaf, context=_fresh_context(active))
            except (SqlCompileError, event_ir.IrSchemaError, ValueError):
                leaf_issues.append(CompilerCapabilityIssue("compiler_operation_unsupported", node_id))
        if leaf_issues:
            unsupported.append(node_id)
            issues.extend(leaf_issues)
        else:
            supported.append(node_id)

    if unsupported and supported:
        status = CAPABILITY_PARTIALLY_SUPPORTED
    elif unsupported or not supported:
        status = CAPABILITY_UNSUPPORTED
    else:
        status = CAPABILITY_SUPPORTED
    return CompilerCapabilityResult(
        status=status,
        issues=tuple(issues),
        supported_node_ids=tuple(supported),
        unsupported_node_ids=tuple(unsupported),
    )
