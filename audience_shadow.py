"""shadow 검증 — legacy 실행과 Event IR 실행이 **같은 대상을 뽑는가**를 실행 권위를 바꾸지 않고 대조한다.

이행의 위험은 "변환이 됐다"가 아니라 "변환된 것이 같은 뜻인가"에 있고, 그 답은 지문 비교만으로는
나오지 않는다(지문은 IR 이 자기 자신과 같다는 것만 말한다). 그래서 여기서는 **legacy 쪽 산출물**을
독립적으로 렌더해 여섯 단계로 대조한다:

    ① 의미 경로 소비 완전성   어댑터가 모든 조건 경로를 소비했는가
    ② canonical semantic 지문 같은 입력이 같은 의미를 내는가(멱등)
    ③ SQL 술어 구조           술어·조인·NULL·기간 경계·부정이 구조로 같은가(문자열 일치가 아니다)
    ④ adversarial fixture     경계에 놓인 회원으로 **실행해** 결과 집합이 같은가
    ⑤ 스냅샷 회원 집합        실데이터 스냅샷에서 두 집합이 같은가
    ⑥ 성능·실행 계획          같은 뜻이어도 비용이 달라지면 자동 cut-over 하지 않는다

**SQL 문자열 완전 일치를 요구하지 않는다.** 같은 뜻을 다른 구조로 적을 수 있기 때문이다. 대신
③은 구조를, ④⑤는 결과 집합을 본다.

차이가 나면 성공/실패로 끝내지 않고 :class:`DivergenceKind` 로 분류한다. 기본값은
``UNEXPLAINED_DIVERGENCE`` 이고 그것은 cut-over 를 막는다 — 설명되지 않은 차이를 통과시키면
"검증했다"는 말이 아무것도 보증하지 않게 된다. legacy 버그 수정으로 차이를 허용하려면
:class:`Approval` 의 여섯 항목을 **전부** 채워야 한다(승인 정보 없는 허용은 없다).

미실행 단계는 통과가 아니다: 필수 단계가 ``NOT_RUN`` 이면 cut-over 는 그대로 막힌다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

import sql_dialect  # 방언 지식(시계 어휘·앵커 표기)의 단일 소유자. 순수 모듈이라 여기서 바로 쓴다.


class ShadowVerificationError(RuntimeError):
    """검증 자체를 수행할 수 없다(재료 부재·SQL 파싱 실패). 결과 '같음'과 구분한다."""


# ── divergence 분류 ───────────────────────────────────────────────────────────────

UNEXPLAINED_DIVERGENCE = "UNEXPLAINED_DIVERGENCE"
APPROVED_LEGACY_BUG_FIX = "APPROVED_LEGACY_BUG_FIX"
DATA_VOLATILITY = "DATA_VOLATILITY"
NONDETERMINISTIC_SOURCE = "NONDETERMINISTIC_SOURCE"
EXPECTED_BINDING_CHANGE = "EXPECTED_BINDING_CHANGE"

DIVERGENCE_KINDS: tuple[str, ...] = (
    UNEXPLAINED_DIVERGENCE,
    APPROVED_LEGACY_BUG_FIX,
    DATA_VOLATILITY,
    NONDETERMINISTIC_SOURCE,
    EXPECTED_BINDING_CHANGE,
)

# cut-over 를 막는 분류. 기본값이 여기 들어 있는 것이 이 표의 요점이다 — 분류되지 않은 차이는
# '아직 설명되지 않았다'이지 '무해하다'가 아니다.
BLOCKING_KINDS: frozenset[str] = frozenset({UNEXPLAINED_DIVERGENCE})


@dataclass(frozen=True)
class Approval:
    """legacy 버그 수정으로 결과 차이를 허용할 때 **반드시** 남겨야 하는 기록.

    여섯 항목 중 하나라도 비면 승인이 아니다. 승인 없이 차이를 허용하는 경로를 두지 않는 이유는
    간단하다 — 그 경로가 생기면 이행 중 발견된 모든 차이가 '아마 버그였을 것'으로 정리된다.
    """

    before_meaning: str
    after_meaning: str
    affected_member_count: int
    approver: str
    approved_at: str
    reference: str          # 이슈 번호 또는 문서 경로
    rollback_criterion: str  # 어떤 신호가 보이면 되돌리는가

    def missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        for name in ("before_meaning", "after_meaning", "approver", "approved_at",
                     "reference", "rollback_criterion"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
        if not isinstance(self.affected_member_count, int) or isinstance(self.affected_member_count, bool):
            missing.append("affected_member_count")
        elif self.affected_member_count < 0:
            missing.append("affected_member_count")
        return tuple(missing)

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_meaning": self.before_meaning,
            "after_meaning": self.after_meaning,
            "affected_member_count": self.affected_member_count,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "reference": self.reference,
            "rollback_criterion": self.rollback_criterion,
        }


@dataclass(frozen=True)
class Divergence:
    """대조에서 발견된 차이 하나."""

    stage: str
    summary: str
    kind: str = UNEXPLAINED_DIVERGENCE
    detail: Mapping[str, Any] = field(default_factory=dict)
    approval: Approval | None = None

    def __post_init__(self) -> None:
        if self.kind not in DIVERGENCE_KINDS:
            raise ShadowVerificationError(f"알 수 없는 divergence 분류: {self.kind!r}")

    @property
    def blocks_cutover(self) -> bool:
        """이 차이가 cut-over 를 막는가.

        승인 분류라도 **승인 기록이 완전하지 않으면 막는다** — 분류 이름만 바꿔 통과시키는 길을
        열지 않기 위해서다.
        """
        if self.kind == APPROVED_LEGACY_BUG_FIX:
            return self.approval is None or not self.approval.is_complete
        return self.kind in BLOCKING_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "summary": self.summary,
            "detail": dict(self.detail),
            "approval": self.approval.to_dict() if self.approval else None,
            "blocks_cutover": self.blocks_cutover,
        }


# ── 단계 모델 ─────────────────────────────────────────────────────────────────────

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

STAGE_PATH_ACCOUNTING = "path_accounting"
STAGE_SEMANTIC_FINGERPRINT = "semantic_fingerprint"
STAGE_SQL_STRUCTURE = "sql_structure"
STAGE_ADVERSARIAL_FIXTURE = "adversarial_fixture"
STAGE_SNAPSHOT_MEMBERS = "snapshot_members"
STAGE_PERFORMANCE = "performance"

# 여섯 단계 전부가 cut-over 의 전제다. 미실행 단계를 통과로 세면 "검증했다"가 거짓말이 된다.
REQUIRED_STAGES: tuple[str, ...] = (
    STAGE_PATH_ACCOUNTING,
    STAGE_SEMANTIC_FINGERPRINT,
    STAGE_SQL_STRUCTURE,
    STAGE_ADVERSARIAL_FIXTURE,
    STAGE_SNAPSHOT_MEMBERS,
    STAGE_PERFORMANCE,
)


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    detail: str = ""
    divergences: tuple[Divergence, ...] = ()

    def __post_init__(self) -> None:
        if self.stage not in REQUIRED_STAGES:
            raise ShadowVerificationError(f"선언되지 않은 검증 단계: {self.stage!r}")
        if self.status not in (PASS, FAIL, NOT_RUN):
            raise ShadowVerificationError(f"알 수 없는 단계 상태: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
            "divergences": [item.to_dict() for item in self.divergences],
        }


@dataclass(frozen=True)
class ShadowReport:
    asset_id: str
    asset_revision: int
    stages: tuple[StageResult, ...]
    semantic_fingerprint: str | None = None
    source_fingerprint: str = ""

    @property
    def divergences(self) -> tuple[Divergence, ...]:
        return tuple(item for stage in self.stages for item in stage.divergences)

    @property
    def blocking_divergences(self) -> tuple[Divergence, ...]:
        return tuple(item for item in self.divergences if item.blocks_cutover)

    @property
    def missing_stages(self) -> tuple[str, ...]:
        ran = {stage.stage for stage in self.stages if stage.status != NOT_RUN}
        return tuple(name for name in REQUIRED_STAGES if name not in ran)

    @property
    def failed_stages(self) -> tuple[str, ...]:
        """차이를 발견한 단계. **관측 사실**이고, 그 자체가 곧 차단은 아니다(분류가 판정한다)."""
        return tuple(stage.stage for stage in self.stages if stage.status == FAIL)

    @property
    def unaccounted_stages(self) -> tuple[str, ...]:
        """실패했는데 그 사유(divergence)를 남기지 않은 단계 — 무조건 차단한다.

        상태와 분류를 분리한 대가로 생기는 구멍이 정확히 이것이다: 사유 없는 FAIL 은 분류할 것이
        없어 '설명되지 않음'조차 세지 못하고 조용히 통과한다.
        """
        return tuple(stage.stage for stage in self.stages if stage.status == FAIL and not stage.divergences)

    @property
    def cutover_allowed(self) -> bool:
        """SHADOW_VERIFIED 로 올릴 수 있는가.

        단계가 차이를 발견했다는 사실만으로 막지 않는다 — 그러면 승인 계약이 장식이 된다
        (승인된 legacy 버그 수정은 정의상 결과가 다르다). 막는 것은 **설명되지 않은 차이**와
        **미실행 필수 단계**, 그리고 사유 없는 실패다.
        """
        return not (self.blocking_divergences or self.missing_stages or self.unaccounted_stages)

    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        for name in self.missing_stages:
            reasons.append(f"필수 검증 단계 미실행: {name}")
        for name in self.unaccounted_stages:
            reasons.append(f"검증 단계가 사유 없이 실패했다: {name}")
        for item in self.blocking_divergences:
            reasons.append(f"[{item.kind}] {item.stage}: {item.summary}")
        return tuple(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_revision": self.asset_revision,
            "semantic_fingerprint": self.semantic_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "cutover_allowed": self.cutover_allowed,
            "blocking_reasons": list(self.blocking_reasons()),
            "failed_stages": list(self.failed_stages),
            "stages": [stage.to_dict() for stage in self.stages],
        }


def not_run(stage: str, reason: str) -> StageResult:
    """미실행 단계를 **사유와 함께** 기록한다. 사유 없는 미실행은 만들지 않는다."""
    if not reason.strip():
        raise ShadowVerificationError(f"{stage}: 미실행 사유가 필요하다")
    return StageResult(stage=stage, status=NOT_RUN, detail=reason)


VACUOUS_MATCH_REASON = (
    "두 산출물이 모두 회원 0명을 냈다 — 공집합끼리의 일치는 두 술어가 같은 뜻임을 **증명하지 않는다**"
    "(뜻이 완전히 다른 두 조건도 대상이 없으면 똑같이 0명이다). 대상이 존재하는 기간·데이터에서 다시 대조하라"
)


def _vacuous(legacy_members: int, event_ir_members: int) -> bool:
    """이 대조가 아무것도 구별하지 못했는가.

    결과 집합 대조(④⑤)의 힘은 **어떤 회원이 갈리는가**에서 나온다. 양쪽이 0명이면 갈릴 회원이
    없었다는 뜻이고, 그때 '같다'는 관측은 술어에 대해 아무 말도 하지 않는다. 그것을 통과로 세면
    데이터가 비어 있는 자산일수록 검증을 쉽게 통과하게 된다 — 정확히 거꾸로다.
    """
    return legacy_members == 0 and event_ir_members == 0


# ── ③ SQL 술어 구조 대조 ──────────────────────────────────────────────────────────


def sql_structure_signature(sql: str, *, dialect: str = "tsql") -> dict[str, Any]:
    """SQL 술어의 **구조 서명**. 별칭·공백·구문 표기가 아니라 의미 요소만 남긴다.

    비교 대상: 술어(컬럼·연산자·값·극성) · 조인 · EXISTS 극성 · NULL 처리 · 기간 경계.
    문자열 비교를 쓰지 않는 이유는 같은 집합을 내는 표기가 여럿이기 때문이고
    (``NOT EXISTS`` ↔ ``NOT (EXISTS ...)``), 별칭 하나가 달라도 다른 SQL 로 보이기 때문이다.
    """
    import sql_semantics  # 지연 import — 이 모듈을 taxonomy 용도로만 쓰는 소비자를 위해.

    table, alias, _key = subject_binding()
    try:
        semantics = sql_semantics.extract_sql_semantics(
            _as_statement(sql, subject_table=table, subject_alias=alias), dialect=dialect
        )
    except Exception as exc:  # noqa: BLE001 — 파싱 실패는 '같다/다르다'가 아니라 검증 불가다.
        raise ShadowVerificationError(f"SQL 구조를 해석하지 못했다: {exc}") from exc

    # 필드 이름을 직접 읽는다(getattr 기본값 금지). 기본값을 두면 추출기 필드가 바뀌었을 때
    # 서명이 조용히 **빈 값끼리 같아지고**, 그 순간 이 단계는 항상 통과하는 장식이 된다.
    def filter_key(item: Any) -> tuple[Any, ...]:
        return (
            str(item.normalized_field or "").upper(),
            str(item.normalized_operator or ""),
            str(item.normalized_value or ""),
            bool(item.negated),
            bool(item.is_date_window),
        )

    signature = {
        "filters": sorted(filter_key(item) for item in semantics.filters),
        "exists": sorted(
            (
                bool(item.negated),
                tuple(sorted(str(table).upper() for table in item.tables)),
                tuple(sorted(str(column).upper() for column in item.correlated_on)),
                tuple(sorted(filter_key(child) for child in item.filters)),
            )
            for item in semantics.exists_conditions
        ),
        "in_conditions": sorted(
            (str(item.field or "").upper(), bool(item.negated), bool(item.subquery),
             tuple(sorted(str(table).upper() for table in item.tables)),
             tuple(sorted(str(value) for value in item.values)))
            for item in semantics.in_conditions
        ),
        "joins": sorted(
            (str(item.table).upper(), str(item.kind).lower()) for item in semantics.joins
        ),
        "aggregates": sorted(
            (str(item.func).lower(), str(item.argument or "").upper(),
             str(item.operator or ""), str(item.value or ""))
            for item in semantics.aggregates
        ),
        "group_by": sorted(str(item).upper() for item in semantics.group_by),
        "distinct": semantics.has_distinct,
    }
    if not any(signature[key] for key in ("filters", "exists", "in_conditions", "aggregates")):
        # 아무 술어도 못 읽었으면 '같다'가 아니라 '읽지 못했다'다.
        raise ShadowVerificationError(f"SQL 에서 술어를 하나도 추출하지 못했다: {sql[:200]}")
    return signature


def subject_binding() -> tuple[str, str, str]:
    """주체(오디언스) 테이블·별칭·키 — **물리 사실이므로 카탈로그가 소유한다.**

    이 검증 계층이 테이블 이름을 자기 소스에 적으면 물리 바인딩 소유자가 하나 더 생기고,
    실제로 이름이 바뀌는 날 검증기만 옛 이름을 들고 조용히 다른 것을 대조한다.
    """
    import audience_runtime  # 지연 import — taxonomy 소비자에게 카탈로그 의존을 강요하지 않는다.

    subject = audience_runtime.resolve_audience_catalog().subject
    return subject.table, subject.alias, subject.key


def _as_statement(sql: str, *, subject_table: str, subject_alias: str) -> str:
    """술어 조각을 파싱 가능한 문장으로 감싼다(구조 추출기는 문장을 기대한다)."""
    text = sql.strip()
    if text.upper().startswith("SELECT"):
        return text
    return f"SELECT 1 FROM {subject_table} {subject_alias} WHERE {text}"


# ── ④ adversarial fixture 실행 ────────────────────────────────────────────────────
# 운영 데이터 비교만으로는 경계가 검증되지 않는다 — 경계에 놓인 회원이 실제로 없을 수 있기 때문이다.
# 그래서 경계를 **만들어** 넣고 두 술어를 실행한다. 실행 엔진은 sqlite 인메모리라 외부 의존이 없고,
# 두 SQL 이 **같은 방식으로** 트랜스파일되므로 방언 차이가 한쪽으로만 유리하게 작용하지 않는다.


@dataclass(frozen=True)
class FixtureTable:
    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class FixtureResult:
    legacy_members: tuple[Any, ...]
    event_ir_members: tuple[Any, ...]
    legacy_sql: str
    event_ir_sql: str

    @property
    def only_in_legacy(self) -> tuple[Any, ...]:
        return tuple(sorted(set(self.legacy_members) - set(self.event_ir_members), key=str))

    @property
    def only_in_event_ir(self) -> tuple[Any, ...]:
        return tuple(sorted(set(self.event_ir_members) - set(self.legacy_members), key=str))

    @property
    def matches(self) -> bool:
        return not (self.only_in_legacy or self.only_in_event_ir)

    @property
    def has_duplicates(self) -> bool:
        """중복 회원이 결과에 섞였는가(조인 증폭 탐지 — 두 쪽 어느 쪽이든)."""
        return (
            len(self.legacy_members) != len(set(self.legacy_members))
            or len(self.event_ir_members) != len(set(self.event_ir_members))
        )


def engine_anchor_date() -> date:
    """fixture 행을 만들 기준일 — **검증 엔진이 보는 오늘**이다.

    롤링 창은 두 SQL 모두에서 실행 시점 함수(``GETDATE()`` → ``CURRENT_TIMESTAMP``)로 렌더된다.
    그런데 fixture 행의 날짜는 파이썬이 만든다. 두 시계가 다르면(선언 timezone 은 서울인데 엔진은
    UTC) 경계 정확히 그날에 놓은 행이 하루 어긋나고, 경계 검증이 **조용히 경계를 안 보게** 된다.
    그래서 행을 만들 때는 엔진에게 직접 오늘을 묻는다. 선언된 ``as_of`` 는 스냅샷 대조(⑤)의
    기준일이지 이 인메모리 엔진의 시계가 아니다.
    """
    connection = sqlite3.connect(":memory:")
    try:
        value = connection.execute("SELECT DATE(CURRENT_TIMESTAMP)").fetchone()[0]
    finally:
        connection.close()
    return date.fromisoformat(str(value))


def offset_date(anchor: date, days: int) -> str:
    """기준일에서 ``days`` 만큼 떨어진 날짜의 char8 표기. 경계 fixture 는 **절대 날짜를 적지 않는다** —
    적으면 오늘이 지날 때마다 경계가 이동해 테스트가 조용히 무의미해진다."""
    return (anchor + timedelta(days=days)).strftime("%Y%m%d")


def build_fixture_tables(spec: Mapping[str, Any], *, anchor: date) -> tuple[FixtureTable, ...]:
    """선언된 fixture 명세를 기준일 기준 실제 행으로 실체화한다.

    행의 값은 리터럴이거나 ``{"offset_days": -90}`` 형태의 **기준일 상대 표기**다.
    """
    tables: list[FixtureTable] = []
    declared = spec.get("tables")
    if not isinstance(declared, Mapping) or not declared:
        raise ShadowVerificationError("fixture 명세에 tables 가 없다")
    for name, table in declared.items():
        if not isinstance(table, Mapping):
            raise ShadowVerificationError(f"fixture 테이블 {name!r} 선언이 객체가 아니다")
        columns = tuple(str(item) for item in table.get("columns") or ())
        if not columns:
            raise ShadowVerificationError(f"fixture 테이블 {name!r} 에 컬럼 선언이 없다")
        rows: list[tuple[Any, ...]] = []
        for row in table.get("rows") or ():
            if not isinstance(row, Mapping):
                raise ShadowVerificationError(f"fixture 테이블 {name!r} 의 행이 객체가 아니다")
            rows.append(tuple(_materialize(row.get(column), anchor) for column in columns))
        tables.append(FixtureTable(name=str(name), columns=columns, rows=tuple(rows)))
    return tuple(tables)


def _materialize(value: Any, anchor: date) -> Any:
    if isinstance(value, Mapping) and "offset_days" in value:
        return offset_date(anchor, int(value["offset_days"]))
    return value


def _sqlite_sql(sql: str, *, dialect: str) -> str:
    import sqlglot

    try:
        return sqlglot.transpile(sql, read=dialect, write="sqlite")[0]
    except Exception as exc:  # noqa: BLE001
        raise ShadowVerificationError(f"검증 엔진 방언으로 옮기지 못했다: {exc}") from exc


class SqliteVerificationDialect(sql_dialect.TSqlDialect):
    """인메모리 검증 엔진(sqlite)이 볼 방언 뷰.

    문법은 대조 대상과 **같은 T-SQL** 이다 — 두 산출물이 그 방언으로 렌더돼 있고, 실행 직전에
    같은 방식으로 sqlite 로 옮기기 때문이다(그래서 방언 차이가 한쪽에만 유리하게 작용하지 않는다).
    다른 것은 **앵커 리터럴 표기 하나**뿐이다: T-SQL 의 ``CAST('…' AS DATETIME)`` 은 sqlite 로
    옮겨지면 숫자 캐스트가 되어 조용히 다른 날짜가 되고(오류가 아니라 **틀린 값**이다), sqlite 의
    날짜 함수는 ISO-8601 문자열을 그대로 읽는다.

    앵커를 어떻게 적는가는 **그 SQL 을 실행할 엔진**의 사실이라, 여기 한 줄이 그 사실의 자리다.
    """

    name = "sqlite_verification"

    def datetime_anchor(self, iso_timestamp: str) -> str:
        return sql_dialect.quote_literal(iso_timestamp)


def _member_query(sql: str, *, subject_table: str, subject_alias: str, subject_key: str) -> str:
    """술어 조각이면 회원 조회로 감싸고, 이미 문장이면 그대로 쓴다(집계 legacy 는 조인 문장이다)."""
    text = sql.strip()
    if text.upper().startswith("SELECT"):
        return text
    return (
        f"SELECT {subject_alias}.{subject_key} FROM {subject_table} {subject_alias} WHERE {text}"
    )


def run_fixture_comparison(
    *,
    legacy_sql: str,
    event_ir_sql: str,
    tables: Sequence[FixtureTable],
    dialect: str = "tsql",
    subject: tuple[str, str, str] | None = None,
) -> FixtureResult:
    """경계 fixture 위에서 두 산출물을 **실행해** 회원 집합을 비교한다.

    ``subject`` 는 (테이블, 별칭, 키). 생략하면 카탈로그 선언에서 가져온다 — 이 모듈은 그 이름을
    스스로 알지 않는다.
    """
    subject_table, subject_alias, subject_key = subject or subject_binding()
    legacy_query = _member_query(
        legacy_sql, subject_table=subject_table, subject_alias=subject_alias, subject_key=subject_key)
    event_query = _member_query(
        event_ir_sql, subject_table=subject_table, subject_alias=subject_alias, subject_key=subject_key)

    connection = sqlite3.connect(":memory:")
    try:
        for table in tables:
            columns = ", ".join(f'"{column}"' for column in table.columns)
            placeholders = ", ".join("?" for _ in table.columns)
            connection.execute(f'CREATE TABLE "{table.name}" ({columns})')
            connection.executemany(
                f'INSERT INTO "{table.name}" ({columns}) VALUES ({placeholders})', table.rows
            )
        legacy_members = _select(connection, _sqlite_sql(legacy_query, dialect=dialect))
        event_members = _select(connection, _sqlite_sql(event_query, dialect=dialect))
    finally:
        connection.close()
    return FixtureResult(
        legacy_members=legacy_members,
        event_ir_members=event_members,
        legacy_sql=legacy_query,
        event_ir_sql=event_query,
    )


def _select(connection: sqlite3.Connection, sql: str) -> tuple[Any, ...]:
    try:
        return tuple(row[0] for row in connection.execute(sql).fetchall())
    except sqlite3.Error as exc:
        raise ShadowVerificationError(f"검증 질의를 실행하지 못했다: {exc}\n{sql}") from exc


# ── ⑤ 스냅샷 회원 집합 대조 ───────────────────────────────────────────────────────
# ④ 는 **만든 경계**를 보고, ⑤ 는 **있는 데이터**를 본다. 둘 다 필요하다: 운영 데이터에 경계 회원이
# 없을 수 있고(그러면 ④ 만이 경계를 검증한다), 반대로 fixture 에는 없는 값 분포·NULL·중복이 실데이터에만
# 있다(그러면 ⑤ 만이 그것을 잡는다).
#
# 이 계층은 **연결을 만들지 않는다.** 질의 실행자는 주입받는다(:data:`SnapshotExecutor`) — 검증기가
# 접속 정보를 알기 시작하면 "확인만 해보려던 실행"이 어떤 DB 에 붙을지 검증기 안에서 정해진다.
# 실행자는 읽기 전용 경로(db_connections.run_read_query)여야 하고, 그 강제는 실행자 소유다.

SnapshotRow = Any  # Mapping[str, Any] | Sequence[Any] — 드라이버마다 다르다(dict/tuple 모두 받는다).
SnapshotExecutor = Callable[[str], Sequence[SnapshotRow]]

# 대조 대상 SQL 에 있으면 **합성 자체를 거부**하는 토큰. 실행자(sql_guard)가 1차 방어선이고 이건 2차다 —
# 여기서 막는 이유는 우리가 두 조각을 하나의 문장으로 **합성**하기 때문이다: 조각에 문장 구분자가 있으면
# 합성물이 우리가 읽은 것과 다른 문장이 된다.
_STATEMENT_BREAKERS: tuple[str, ...] = (";",)
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

# 카운트 버킷. 이름과 순서를 여기서 고정한다 — 결과 해석이 열 순서에 의존하지 않게.
COUNT_BUCKETS: tuple[str, ...] = (
    "legacy_rows", "legacy_members", "event_ir_rows", "event_ir_members",
    "only_in_legacy", "only_in_event_ir",
)


@dataclass(frozen=True)
class SnapshotCounts:
    """한 번의 대조 실행이 낸 수치. 회원 id 전체를 가져오지 않는 이유는 실데이터에서 그것이
    수백만 행이기 때문이다 — 차집합·중복 판정은 엔진이 하고, 여기로 오는 것은 **수치와 표본**이다."""

    legacy_rows: int
    legacy_members: int
    event_ir_rows: int
    event_ir_members: int
    only_in_legacy: int
    only_in_event_ir: int

    @property
    def matches(self) -> bool:
        return not (self.only_in_legacy or self.only_in_event_ir)

    @property
    def has_duplicates(self) -> bool:
        """행 수와 회원 수가 다르면 조인 증폭이다(어느 쪽이든)."""
        return self.legacy_rows != self.legacy_members or self.event_ir_rows != self.event_ir_members

    def as_tuple(self) -> tuple[int, ...]:
        return tuple(getattr(self, name) for name in COUNT_BUCKETS)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in COUNT_BUCKETS}


@dataclass(frozen=True)
class SnapshotComparison:
    counts: SnapshotCounts
    # 안정성 probe. 같은 문장을 다시 실행해 **원천이 움직이는지** 본다. 차이가 없을 때는 두 번 돌리지
    # 않는다 — 그때 재실행이 답하는 질문이 없고(같다는 관측은 그 순간의 사실이다), 실데이터 전수 스캔은
    # 비싸다. 차이가 나왔을 때만 "이 차이가 뜻의 차이인가, 원천이 움직인 것인가"를 물어야 한다.
    probes: tuple[SnapshotCounts, ...]
    clock_pinned: bool
    anchor: str
    clock_functions: tuple[str, ...]
    sample_only_in_legacy: tuple[Any, ...] = ()
    sample_only_in_event_ir: tuple[Any, ...] = ()
    sample_limit: int = 0
    legacy_sql: str = ""
    event_ir_sql: str = ""

    @property
    def stable(self) -> bool:
        return all(probe.as_tuple() == self.counts.as_tuple() for probe in self.probes)

    @property
    def sample_truncated(self) -> bool:
        return (
            self.counts.only_in_legacy > len(self.sample_only_in_legacy)
            or self.counts.only_in_event_ir > len(self.sample_only_in_event_ir)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts.to_dict(),
            "probes": [probe.to_dict() for probe in self.probes],
            "stable": self.stable,
            "clock_pinned": self.clock_pinned,
            "clock_functions": list(self.clock_functions),
            "anchor": self.anchor,
            "sample_limit": self.sample_limit,
            "sample_truncated": self.sample_truncated,
            "sample_only_in_legacy": list(self.sample_only_in_legacy),
            "sample_only_in_event_ir": list(self.sample_only_in_event_ir),
        }


def clock_functions_in(sql: str, *, dialect: Any) -> tuple[str, ...]:
    """SQL 에 남아 있는 실행 시점 시계 함수들(방언이 선언한 어휘로 센다)."""
    return _scan_clocks(sql, dialect.clock_functions())


def residual_clock_functions(sql: str) -> tuple[str, ...]:
    """**아는 모든 방언**의 어휘로 훑는다 — 다른 방언이 렌더한 시계를 '없다'고 세지 않기 위해서다.

    legacy 산출물의 방언(회원 빌더)과 대조를 실행할 연결의 방언은 서로 다를 수 있다. 한쪽 어휘로만
    세면 그 SQL 은 실행 시 문법 오류로 죽지만, 그 전에 "고정할 시계가 없다"는 잘못된 판정을 통과한다.
    """
    return _scan_clocks(sql, sql_dialect.all_clock_functions())


def _scan_clocks(sql: str, vocabulary: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for token in vocabulary:
        pattern = re.escape(token).replace(r"\(\)", r"\s*\(\s*\)")
        if re.search(rf"(?<![\w.]){pattern}", sql, re.IGNORECASE):
            found.append(token)
    return tuple(sorted(set(found)))


def pin_execution_clock(sql: str, *, clock_token: str, anchor_sql: str) -> tuple[str, int]:
    """실행 시점 시계 호출을 **고정된 앵커 식**으로 바꾼다. 반환: (치환된 SQL, 치환 횟수).

    왜 필요한가: 롤링 컷오프는 두 산출물 모두 실행 시점 함수로 렌더된다. 두 문장을 각각 실행하면
    두 시계를 보게 되고, 자정을 넘기거나 적재가 도는 순간에 걸리면 **뜻이 같은데도 다른 집합**이 나온다.
    그 차이를 의미 차이로 보고하면 보고서가 거짓을 말하고, 무시하면 진짜 차이를 놓친다.

    치환은 **양쪽에 똑같이** 적용한다 — 한쪽만 고정하면 그 순간 대조가 편향된다. 앵커 식이 문자열이
    아니라 타입 있는 식인 이유는 :meth:`SqlDialect.datetime_anchor` 에 적어 두었다.
    """
    pattern = re.compile(
        rf"(?<![\w.]){re.escape(clock_token).replace(r'\(\)', r'\s*\(\s*\)')}", re.IGNORECASE
    )
    return pattern.subn(anchor_sql, sql)


def _normalize_anchor(value: Any) -> str:
    """엔진이 돌려준 기준 시각을 ISO-8601(초 단위)로. 읽지 못하면 **고정하지 않는다**(예외)."""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep="T", timespec="seconds")
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        raise ShadowVerificationError("엔진에서 기준 시각을 읽지 못했다(빈 값)")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ShadowVerificationError(f"엔진 기준 시각을 ISO-8601 로 읽지 못했다: {value!r}") from exc
    return parsed.replace(microsecond=0).isoformat(sep="T", timespec="seconds")


def engine_clock_anchor(execute: SnapshotExecutor, *, dialect: Any) -> str:
    """대조의 기준 시각 — **질의를 실행할 엔진의 시계**에서 읽는다.

    파이썬의 시계를 쓰지 않는 이유는 ④의 :func:`engine_anchor_date` 와 같다: 컷오프를 계산하는 것은
    엔진이고, 검증 프로세스가 다른 timezone 에 있으면 경계가 통째로 하루 어긋난다.
    """
    rows = execute(f"SELECT {dialect.now()} AS anchor_ts")
    values = [_row_values(row) for row in rows]
    if not values or not values[0]:
        raise ShadowVerificationError("엔진 기준 시각 질의가 값을 돌려주지 않았다")
    return _normalize_anchor(values[0][0])


def _row_values(row: SnapshotRow) -> tuple[Any, ...]:
    """드라이버 차이(dict/tuple)를 흡수한다. dict 는 **선언한 컬럼 이름**으로 읽는다."""
    if isinstance(row, Mapping):
        return tuple(row.values())
    if isinstance(row, (list, tuple)):
        return tuple(row)
    return (row,)


def _assert_comparable_fragment(sql: str, *, label: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ShadowVerificationError(f"{label} 산출물이 비어 있어 대조할 수 없다")
    for breaker in _STATEMENT_BREAKERS:
        if breaker in text:
            raise ShadowVerificationError(
                f"{label} 산출물에 문장 구분자({breaker!r})가 있다 — 합성하면 우리가 읽은 것과 다른 문장이 된다"
            )
    match = _WRITE_KEYWORDS.search(text)
    if match:
        raise ShadowVerificationError(
            f"{label} 산출물에 쓰기 계열 키워드가 있다: {match.group(0)!r} — 스냅샷 대조는 읽기만 한다"
        )
    return text


def _member_keys(sql: str, *, alias: str, subject: tuple[str, str, str]) -> str:
    """회원 조회를 '주체 키 한 컬럼'으로 정규화한다.

    안쪽 프로젝션이 주체 키가 아니면 엔진이 오류를 낸다 — 그 실패가 옳다. 컬럼을 위치로 집으면
    (``SELECT 1 FROM``) 엉뚱한 값이 회원 id 자리에 조용히 들어온다.
    """
    subject_table, subject_alias, subject_key = subject
    inner = _member_query(
        sql, subject_table=subject_table, subject_alias=subject_alias, subject_key=subject_key)
    return f"SELECT {alias}.{subject_key} AS {subject_key} FROM ({inner}) {alias}"


def build_snapshot_count_sql(
    legacy_sql: str, event_ir_sql: str, *, subject: tuple[str, str, str]
) -> str:
    """두 산출물의 행수·회원수·양방향 차집합 크기를 **한 문장으로** 센다.

    한 문장인 것이 계약이다: 여섯 수치가 같은 실행·같은 스냅샷에서 나와야 서로 비교할 수 있고,
    나눠 실행하면 그 사이에 원천이 움직인 만큼이 '의미 차이'로 둔갑한다.
    """
    _, _, key = subject
    legacy_keys = _member_keys(legacy_sql, alias="SL", subject=subject)
    event_keys = _member_keys(event_ir_sql, alias="SE", subject=subject)
    parts = [
        f"SELECT 'legacy_rows' AS bucket, COUNT(*) AS member_count FROM ({legacy_keys}) CLR",
        f"SELECT 'legacy_members', COUNT(*) FROM (SELECT DISTINCT CLD.{key} FROM ({legacy_keys}) CLD) CLM",
        f"SELECT 'event_ir_rows', COUNT(*) FROM ({event_keys}) CER",
        f"SELECT 'event_ir_members', COUNT(*) FROM (SELECT DISTINCT CED.{key} FROM ({event_keys}) CED) CEM",
        f"SELECT 'only_in_legacy', COUNT(*) FROM ({legacy_keys} EXCEPT {event_keys}) COL",
        f"SELECT 'only_in_event_ir', COUNT(*) FROM ({event_keys} EXCEPT {legacy_keys}) COE",
    ]
    return " UNION ALL ".join(parts)


def build_snapshot_sample_sql(
    minuend_sql: str, subtrahend_sql: str, *, subject: tuple[str, str, str],
    limit: int, dialect: Any,
) -> str:
    """차집합의 상위 N 회원 표본(진단용). 정렬을 넣는 이유는 표본이 실행마다 달라지면
    두 보고서를 나란히 놓고 읽을 수 없기 때문이다."""
    _, _, key = subject
    minuend = _member_keys(minuend_sql, alias="SL", subject=subject)
    subtrahend = _member_keys(subtrahend_sql, alias="SE", subject=subject)
    return (
        f"SELECT {dialect.row_limit_prefix(limit)}SD.{key} AS {key} "
        f"FROM ({minuend} EXCEPT {subtrahend}) SD ORDER BY SD.{key} {dialect.row_limit_suffix(limit)}"
    ).strip()


def _counts_from_rows(rows: Sequence[SnapshotRow]) -> SnapshotCounts:
    buckets: dict[str, int] = {}
    for row in rows:
        values = _row_values(row)
        if len(values) < 2:
            raise ShadowVerificationError(f"카운트 결과 행의 모양이 (버킷, 개수)가 아니다: {values!r}")
        buckets[str(values[0])] = int(values[1])
    missing = [name for name in COUNT_BUCKETS if name not in buckets]
    if missing:
        raise ShadowVerificationError(f"카운트 결과에 빠진 버킷이 있다: {', '.join(missing)}")
    return SnapshotCounts(**{name: buckets[name] for name in COUNT_BUCKETS})


@dataclass(frozen=True)
class PinnedPair:
    """같은 앵커로 고정된 두 산출물. ⑤와 ⑥이 같은 고정 규칙을 쓰게 하는 자리다 —
    한쪽만 고정 규칙이 달라지면 두 단계가 서로 다른 시점의 SQL 을 재는 일이 조용히 생긴다."""

    legacy_sql: str
    event_ir_sql: str
    anchor: str
    pinned: bool
    clock_functions: tuple[str, ...]


def pin_both_sides(
    legacy_sql: str, event_ir_sql: str, *,
    execute: SnapshotExecutor, dialect: Any, pin_clock: bool = True,
) -> PinnedPair:
    """두 산출물의 실행 시점 시계를 **하나의 앵커**로 고정한다(고정할 수 없으면 예외).

    ``pin_clock=False`` 는 **측정용 진단 모드**다. 시계 함수가 남아 있는 채로는 시작하지 않는다 —
    고정할 수 있는데 고정하지 않고 얻은 '같다'는 우연일 수 있고, 그 우연을 통과로 기록하면
    검증이 검증이 아니게 된다.
    """
    legacy_sql = _assert_comparable_fragment(legacy_sql, label="legacy")
    event_ir_sql = _assert_comparable_fragment(event_ir_sql, label="event_ir")
    found = tuple(sorted(set(
        residual_clock_functions(legacy_sql) + residual_clock_functions(event_ir_sql)
    )))
    if not found:
        return PinnedPair(legacy_sql, event_ir_sql, anchor="", pinned=False, clock_functions=())
    if not pin_clock:
        raise ShadowVerificationError(
            f"실행 시점 시계({', '.join(found)})가 고정되지 않은 채로는 두 산출물이 같은 기준 시각에"
            " 평가되지 않는다 — 대조 결과를 의미 차이로 읽을 수 없다"
        )
    anchor = engine_clock_anchor(execute, dialect=dialect)
    anchor_sql = dialect.datetime_anchor(anchor)
    legacy_sql, legacy_hits = pin_execution_clock(
        legacy_sql, clock_token=dialect.now(), anchor_sql=anchor_sql)
    event_ir_sql, event_hits = pin_execution_clock(
        event_ir_sql, clock_token=dialect.now(), anchor_sql=anchor_sql)
    residual = tuple(sorted(set(
        residual_clock_functions(legacy_sql) + residual_clock_functions(event_ir_sql)
    )))
    if residual:
        # 치환하는 것은 **대조를 실행할 연결의** 기본 시계(now) 하나뿐이다. 다른 시계가 남았다는 것은
        # 우리가 모르는 자리에서 앵커가 계속 움직인다는 뜻이고(또는 산출물이 다른 방언으로 렌더됐다는
        # 뜻이고), 그 위에서 얻은 결과는 '같다'도 '다르다'도 말할 수 없다.
        raise ShadowVerificationError(
            f"고정하지 못한 실행 시점 시계가 남아 있다: {', '.join(residual)}"
        )
    return PinnedPair(
        legacy_sql, event_ir_sql, anchor=anchor,
        pinned=bool(legacy_hits or event_hits), clock_functions=found,
    )


def run_snapshot_comparison(
    *,
    legacy_sql: str,
    event_ir_sql: str,
    execute: SnapshotExecutor,
    dialect: Any,
    subject: tuple[str, str, str] | None = None,
    sample_limit: int = 20,
    pin_clock: bool = True,
) -> SnapshotComparison:
    """실데이터 스냅샷에서 두 산출물의 회원 집합을 대조한다(읽기만 한다).

    순서: ① 시계 고정 → ② 수치 한 문장 → ③ 차이가 있으면 안정성 probe + 표본.
    """
    subject = subject or subject_binding()
    pair = pin_both_sides(
        legacy_sql, event_ir_sql, execute=execute, dialect=dialect, pin_clock=pin_clock)
    legacy_sql, event_ir_sql = pair.legacy_sql, pair.event_ir_sql
    found, anchor, pinned = pair.clock_functions, pair.anchor, pair.pinned

    count_sql = build_snapshot_count_sql(legacy_sql, event_ir_sql, subject=subject)
    counts = _counts_from_rows(execute(count_sql))

    probes: list[SnapshotCounts] = []
    samples: dict[str, tuple[Any, ...]] = {"only_in_legacy": (), "only_in_event_ir": ()}
    if not counts.matches or counts.has_duplicates:
        probes.append(_counts_from_rows(execute(count_sql)))
    if not counts.matches and sample_limit > 0:
        samples["only_in_legacy"] = _sample(
            execute, build_snapshot_sample_sql(
                legacy_sql, event_ir_sql, subject=subject, limit=sample_limit, dialect=dialect))
        samples["only_in_event_ir"] = _sample(
            execute, build_snapshot_sample_sql(
                event_ir_sql, legacy_sql, subject=subject, limit=sample_limit, dialect=dialect))

    return SnapshotComparison(
        counts=counts, probes=tuple(probes), clock_pinned=pinned, anchor=anchor,
        clock_functions=found,
        sample_only_in_legacy=samples["only_in_legacy"],
        sample_only_in_event_ir=samples["only_in_event_ir"],
        sample_limit=sample_limit if not counts.matches else 0,
        legacy_sql=legacy_sql, event_ir_sql=event_ir_sql,
    )


def _sample(execute: SnapshotExecutor, sql: str) -> tuple[Any, ...]:
    return tuple(_row_values(row)[0] for row in execute(sql))


def snapshot_stage(comparison: SnapshotComparison) -> StageResult:
    """⑤ 실데이터 스냅샷에서 두 산출물의 회원 집합이 같은가.

    **불안정한 원천 위에서는 판정하지 않는다.** 같은 문장이 두 번 다른 답을 냈다면 그 차이는 뜻의
    차이일 수도, 그 사이에 움직인 데이터일 수도 있다 — 어느 쪽인지 말할 수 없는 관측을 'FAIL' 로
    적으면 승인 계약이 잘못된 것을 승인하게 되고, 'PASS' 로 적으면 검증하지 않은 것을 검증했다고
    말하게 된다. 그래서 ``NOT_RUN`` 이고, 필수 단계 미실행이므로 cut-over 는 그대로 막힌다.
    """
    if not comparison.stable:
        moved = [
            {"probe": index + 1, "counts": probe.to_dict()}
            for index, probe in enumerate(comparison.probes)
            if probe.as_tuple() != comparison.counts.as_tuple()
        ]
        # 분류는 **가릴 수 있는 만큼만** 말한다. 시계를 고정했는데도 움직였다면 원인이 시계는 아니다
        # (데이터 변동인지 질의 비결정성인지는 이 계층이 가르지 못한다 — 원천 워터마크가 필요하다).
        # 고정할 시계가 애초에 없었다면 남는 원인은 원천 데이터뿐이다.
        kind = NONDETERMINISTIC_SOURCE if comparison.clock_pinned else DATA_VOLATILITY
        reason = (
            "같은 기준 시각으로 두 번 실행했는데 수치가 달랐다 — 원천이 대조 중에 움직였거나 질의가 "
            "결정론적이지 않다(둘을 가르려면 원천 워터마크가 필요하다)"
            if comparison.clock_pinned else
            "두 번 실행의 수치가 달랐다 — 고정할 실행 시점 시계가 없으므로 원천 데이터가 대조 중에 움직였다"
        )
        return StageResult(
            stage=STAGE_SNAPSHOT_MEMBERS, status=NOT_RUN, detail=reason,
            divergences=(Divergence(
                stage=STAGE_SNAPSHOT_MEMBERS, summary="스냅샷이 대조 중에 움직였다", kind=kind,
                detail={"first": comparison.counts.to_dict(), "moved_probes": moved},
            ),),
        )

    divergences: list[Divergence] = []
    if not comparison.counts.matches:
        divergences.append(Divergence(
            stage=STAGE_SNAPSHOT_MEMBERS,
            summary="실데이터 스냅샷에서 결과 회원 집합이 다르다",
            detail={
                "counts": comparison.counts.to_dict(),
                "sample_only_in_legacy": list(comparison.sample_only_in_legacy),
                "sample_only_in_event_ir": list(comparison.sample_only_in_event_ir),
                # 표본이 잘렸다는 사실을 반드시 싣는다 — 조용한 절단은 '이게 전부'로 읽힌다.
                "sample_truncated": comparison.sample_truncated,
                "sample_limit": comparison.sample_limit,
            },
        ))
    if comparison.counts.has_duplicates:
        divergences.append(Divergence(
            stage=STAGE_SNAPSHOT_MEMBERS,
            summary="결과에 중복 회원이 있다(조인 증폭)",
            detail=comparison.counts.to_dict(),
        ))
    if divergences:
        return StageResult(
            stage=STAGE_SNAPSHOT_MEMBERS, status=FAIL,
            detail=(
                f"only_in_legacy={comparison.counts.only_in_legacy} · "
                f"only_in_event_ir={comparison.counts.only_in_event_ir}"
            ),
            divergences=tuple(divergences),
        )
    if _vacuous(comparison.counts.legacy_members, comparison.counts.event_ir_members):
        return not_run(STAGE_SNAPSHOT_MEMBERS, VACUOUS_MATCH_REASON)
    return StageResult(
        stage=STAGE_SNAPSHOT_MEMBERS, status=PASS,
        detail=(
            f"회원 {comparison.counts.legacy_members}명 집합 동일"
            + (f" (기준 시각 고정: {comparison.anchor})" if comparison.clock_pinned else "")
        ),
    )


# ── ⑥ 실행 비용 대조 ──────────────────────────────────────────────────────────────
# 같은 뜻이어도 비용이 달라지면 자동으로 넘기지 않는다. legacy 술어와 Event IR 술어는 구조가 다르고
# (조인 문장 ↔ 스칼라 서브쿼리), 같은 집합을 내면서도 실행 계획이 완전히 달라질 수 있다.
#
# **재지 못하는 축을 재는 척하지 않는다.** 읽기 전용 SELECT 경로로는 ``SET STATISTICS IO`` ·
# ``SET SHOWPLAN_XML`` 을 낼 수 없다(그 가드가 이 이행의 안전장치이므로 뚫지 않는다). 그래서 이
# 단계가 재는 것은 **술어 평가의 벽시계 시간**이고, 재지 못한 축은 결과에 이름으로 남는다 —
# 보고서를 읽는 사람이 "성능이 모든 축에서 같다"로 읽지 않도록.
#
# 비용 퇴행에는 **승인 경로를 만들지 않았다.** 만들면 그 경로가 기본이 된다. 의미 차이 승인
# (:class:`Approval`)을 재사용하면 감사 기록이 'legacy 버그 수정'이라는 거짓 라벨을 갖게 되므로,
# 필요해지는 날 전용 분류와 승인 스키마를 함께 만드는 것이 옳다.

# 이 단계가 재지 않는 비용 축. 이름을 적어 두는 것이 계약이다 — 목록이 비면 '다 쟀다'로 읽힌다.
UNMEASURED_COST_AXES: tuple[str, ...] = (
    "논리·물리 읽기(SET STATISTICS IO — 읽기 전용 SELECT 경로에서 낼 수 없다)",
    "실행 계획 모양·스캔/조인 행수(SET SHOWPLAN_XML 동일 사유)",
    "sort/temp 사용량",
    "동시 부하 하의 거동(단일 세션 순차 측정이다)",
)


@dataclass(frozen=True)
class Timing:
    """한쪽 산출물의 실행 시간 표본(ms).

    첫 실행을 표본에서 빼는 이유: 계획 컴파일과 캐시 적재가 거기 다 들어간다. 빼지 않으면 먼저 돈
    쪽이 항상 느려 보이고, 그러면 이 단계는 실행 순서를 재는 장치가 된다.
    """

    first_ms: float
    samples: tuple[float, ...]
    failures: int = 0
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.samples)

    def percentile(self, ratio: float) -> float:
        """nearest-rank 백분위. 표본 수가 적으면 상위 백분위는 사실상 최대값이다 —
        그래서 :meth:`to_dict` 가 ``samples`` 개수를 항상 함께 싣는다."""
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = max(1, min(len(ordered), _ceil_int(ratio * len(ordered))))
        return ordered[index - 1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": len(self.samples),
            "first_ms": round(self.first_ms, 3),
            "min_ms": round(min(self.samples), 3) if self.samples else None,
            "p50_ms": round(self.percentile(0.50), 3) if self.samples else None,
            "p95_ms": round(self.percentile(0.95), 3) if self.samples else None,
            "p99_ms": round(self.percentile(0.99), 3) if self.samples else None,
            "max_ms": round(max(self.samples), 3) if self.samples else None,
            "failures": self.failures,
            "error": self.error,
        }


def _ceil_int(value: float) -> int:
    return int(value) + (1 if value > int(value) else 0)


@dataclass(frozen=True)
class PerformanceComparison:
    legacy: Timing
    event_ir: Timing
    repetitions: int
    ratio_threshold: float
    noise_floor_ms: float
    anchor: str = ""
    clock_pinned: bool = False
    measured_sql: tuple[str, str] = ("", "")

    @property
    def ratio(self) -> float | None:
        """Event IR p50 ÷ legacy p50. 한쪽이라도 못 쟀으면 비율은 없다(0으로 두지 않는다)."""
        if not (self.legacy.usable and self.event_ir.usable):
            return None
        base = self.legacy.percentile(0.50)
        if base <= 0:
            return None
        return self.event_ir.percentile(0.50) / base

    @property
    def regressed(self) -> bool:
        """비용 퇴행인가 — 비율과 **절대 차이**를 함께 본다.

        비율만 보면 2ms → 4ms 가 '2배 퇴행'이 되어 보고서가 잡음으로 찬다. 절대 차이만 보면
        느린 질의의 배수 퇴행을 놓친다. 둘 다 넘을 때만 퇴행이다.
        """
        ratio = self.ratio
        if ratio is None:
            return False
        delta = self.event_ir.percentile(0.50) - self.legacy.percentile(0.50)
        return ratio > self.ratio_threshold and delta > self.noise_floor_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy": self.legacy.to_dict(),
            "event_ir": self.event_ir.to_dict(),
            "repetitions": self.repetitions,
            "ratio_p50": round(self.ratio, 3) if self.ratio is not None else None,
            "ratio_threshold": self.ratio_threshold,
            "noise_floor_ms": self.noise_floor_ms,
            "clock_pinned": self.clock_pinned,
            "anchor": self.anchor,
            "unmeasured_cost_axes": list(UNMEASURED_COST_AXES),
        }


def build_cost_probe_sql(sql: str, *, subject: tuple[str, str, str], alias: str) -> str:
    """비용 측정용 질의 — 회원 집합의 **크기**를 센다.

    회원 id 를 전부 가져오지 않는 이유: 그러면 술어 비용이 아니라 네트워크 전송을 재게 되고,
    대상이 큰 자산일수록 그 비중이 커져 두 술어의 차이가 묻힌다. 대신 이 질의가 재는 것은
    '술어 평가 + 집계'이지 '결과 스트리밍'이 아니라는 사실을 보고서가 함께 싣는다.
    """
    return f"SELECT COUNT(*) AS member_count FROM ({_member_keys(sql, alias=alias, subject=subject)}) {alias}P"


def run_performance_comparison(
    *,
    legacy_sql: str,
    event_ir_sql: str,
    execute: SnapshotExecutor,
    dialect: Any,
    subject: tuple[str, str, str] | None = None,
    repetitions: int = 5,
    ratio_threshold: float = 1.5,
    noise_floor_ms: float = 50.0,
    pin_clock: bool = True,
    timer: Callable[[], float] | None = None,
) -> PerformanceComparison:
    """두 산출물의 실행 비용을 **번갈아** 재서 비교한다(읽기만 한다).

    번갈아 재는 이유: 한쪽을 몰아서 돌리면 캐시 적재·서버 부하 변동·다른 세션의 간섭이 한쪽에만
    몰린다. 교대 실행은 그 표류를 양쪽에 고르게 나눈다 — ④에서 두 SQL 을 같은 방식으로
    트랜스파일하는 것과 같은 이유다(측정 장치가 한쪽 편을 들지 않게).
    """
    import time

    subject = subject or subject_binding()
    clock = timer or time.perf_counter
    pair = pin_both_sides(
        legacy_sql, event_ir_sql, execute=execute, dialect=dialect, pin_clock=pin_clock)
    probes = {
        "legacy": build_cost_probe_sql(pair.legacy_sql, subject=subject, alias="SL"),
        "event_ir": build_cost_probe_sql(pair.event_ir_sql, subject=subject, alias="SE"),
    }

    first: dict[str, float] = {}
    samples: dict[str, list[float]] = {"legacy": [], "event_ir": []}
    failures: dict[str, int] = {"legacy": 0, "event_ir": 0}
    errors: dict[str, str] = {"legacy": "", "event_ir": ""}

    for index in range(max(1, repetitions) + 1):  # +1 = 표본에서 제외되는 첫 실행(계획 컴파일·캐시)
        for side, sql in probes.items():
            started = clock()
            try:
                execute(sql)
            except Exception as exc:  # noqa: BLE001 — 한쪽의 실패도 측정 결과다(그 자체가 비용 신호).
                failures[side] += 1
                errors[side] = errors[side] or f"{exc.__class__.__name__}: {exc}"
                first.setdefault(side, 0.0)
                continue
            elapsed = (clock() - started) * 1000.0
            if index == 0:
                first[side] = elapsed
            else:
                samples[side].append(elapsed)

    return PerformanceComparison(
        legacy=Timing(first.get("legacy", 0.0), tuple(samples["legacy"]),
                      failures["legacy"], errors["legacy"]),
        event_ir=Timing(first.get("event_ir", 0.0), tuple(samples["event_ir"]),
                        failures["event_ir"], errors["event_ir"]),
        repetitions=max(1, repetitions),
        ratio_threshold=ratio_threshold,
        noise_floor_ms=noise_floor_ms,
        anchor=pair.anchor,
        clock_pinned=pair.pinned,
        measured_sql=(probes["legacy"], probes["event_ir"]),
    )


def performance_stage(comparison: PerformanceComparison) -> StageResult:
    """⑥ 같은 뜻을 내는 두 산출물의 실행 비용이 견줄 만한가.

    한쪽만 실패했다면 그것이 곧 퇴행이므로 **차이**로 보고한다. 양쪽 다 재지 못했다면 비교가
    없었던 것이므로 미실행이다 — 실패를 통과로도, 차이로도 세지 않는다.
    """
    measured = f"측정 {len(comparison.legacy.samples)}회(첫 실행 제외) · 재지 않은 축 {len(UNMEASURED_COST_AXES)}종"
    if not comparison.legacy.usable and not comparison.event_ir.usable:
        return not_run(
            STAGE_PERFORMANCE,
            "양쪽 모두 비용 질의를 실행하지 못해 비교가 성립하지 않는다"
            + (f" (legacy: {comparison.legacy.error})" if comparison.legacy.error else "")
            + (f" (event_ir: {comparison.event_ir.error})" if comparison.event_ir.error else ""),
        )
    if not comparison.event_ir.usable or not comparison.legacy.usable:
        failed = "event_ir" if not comparison.event_ir.usable else "legacy"
        timing = comparison.event_ir if failed == "event_ir" else comparison.legacy
        return StageResult(
            stage=STAGE_PERFORMANCE, status=FAIL,
            detail=f"{failed} 쪽 비용 질의가 실행되지 않았다",
            divergences=(Divergence(
                stage=STAGE_PERFORMANCE,
                summary=f"{failed} 산출물만 실행에 실패했다",
                detail={"side": failed, "error": timing.error, **comparison.to_dict()},
            ),),
        )
    if comparison.regressed:
        return StageResult(
            stage=STAGE_PERFORMANCE, status=FAIL,
            detail=(
                f"p50 {comparison.legacy.percentile(0.50):.1f}ms → "
                f"{comparison.event_ir.percentile(0.50):.1f}ms"
            ),
            divergences=(Divergence(
                stage=STAGE_PERFORMANCE,
                summary="Event IR 산출물의 실행 비용이 legacy 보다 크다",
                detail=comparison.to_dict(),
            ),),
        )
    return StageResult(
        stage=STAGE_PERFORMANCE, status=PASS,
        detail=(
            f"p50 {comparison.legacy.percentile(0.50):.1f}ms → "
            f"{comparison.event_ir.percentile(0.50):.1f}ms ({measured})"
        ),
    )


# ── 단계 조립 ─────────────────────────────────────────────────────────────────────


def path_accounting_stage(result: Any) -> StageResult:
    """① 어댑터가 의미 경로를 남김없이 소비했는가."""
    unmapped = tuple(result.unmapped_paths)
    invalid = tuple(result.invalid_paths)
    if unmapped or invalid:
        return StageResult(
            stage=STAGE_PATH_ACCOUNTING, status=FAIL,
            detail=f"미매핑 {len(unmapped)}건 · 무효 {len(invalid)}건",
            divergences=(Divergence(
                stage=STAGE_PATH_ACCOUNTING,
                summary="변환되지 않은 의미 경로가 남아 있다",
                detail={"unmapped_paths": list(unmapped), "invalid_paths": list(invalid)},
            ),),
        )
    return StageResult(
        stage=STAGE_PATH_ACCOUNTING, status=PASS,
        detail=f"소비 {len(result.consumed_paths)}건 · 무시 {len(result.ignored_paths)}건(사유 선언됨)",
    )


def fingerprint_stage(first: Any, second: Any) -> StageResult:
    """② 같은 입력을 두 번 변환했을 때 의미 지문이 같은가(멱등)."""
    if first.fingerprint != second.fingerprint:
        return StageResult(
            stage=STAGE_SEMANTIC_FINGERPRINT, status=FAIL, detail="반복 변환의 의미 지문이 다르다",
            divergences=(Divergence(
                stage=STAGE_SEMANTIC_FINGERPRINT,
                summary="변환이 멱등하지 않다",
                detail={"first": first.fingerprint, "second": second.fingerprint},
            ),),
        )
    return StageResult(
        stage=STAGE_SEMANTIC_FINGERPRINT, status=PASS,
        detail=f"semantic={first.fingerprint} (반복 변환 동일)",
    )


def shape_class(signature: Mapping[str, Any]) -> str:
    """이 산출물이 **어떤 모양**인가 — 회원 술어인가, 조인 문장인가.

    모양이 다르면 구조 서명 대조는 성립하지 않는다. legacy 집계는 ``INNER JOIN (GROUP BY … HAVING)``
    이고 Event IR 은 스칼라 서브쿼리 술어인데, 이 둘은 **같은 뜻을 다른 구조로** 적은 것이라
    서명이 다른 것이 정상이다. 그런 쌍을 '차이'로 보고하면 보고서가 잡음으로 차고, 진짜 차이가 묻힌다.
    그렇다고 통과시키면 검증하지 않은 것을 검증했다고 말하게 되므로 — 대조하지 않았다고 말한다.
    """
    return "join_statement" if (signature.get("joins") or signature.get("group_by")) else "predicate"


def sql_structure_stage(
    legacy_sql: str, event_ir_sql: str, *, dialect: str = "tsql",
    classify: Callable[[dict[str, Any], dict[str, Any]], Divergence | None] | None = None,
) -> StageResult:
    """③ 두 SQL 의 술어·조인·극성·NULL·기간 경계 구조가 같은가(문자열 일치가 아니다)."""
    legacy_signature = sql_structure_signature(legacy_sql, dialect=dialect)
    event_signature = sql_structure_signature(event_ir_sql, dialect=dialect)
    legacy_shape, event_shape = shape_class(legacy_signature), shape_class(event_signature)
    if legacy_shape != event_shape:
        return not_run(
            STAGE_SQL_STRUCTURE,
            f"산출물 모양이 달라 구조 서명 대조가 성립하지 않는다"
            f"(legacy={legacy_shape}, event_ir={event_shape}) — 같은 뜻의 다른 구조인지는"
            " 결과 집합 대조(④⑤)가 판정해야 한다",
        )
    if legacy_signature == event_signature:
        return StageResult(stage=STAGE_SQL_STRUCTURE, status=PASS, detail="구조 서명 동일")
    divergence = (
        classify(legacy_signature, event_signature) if classify is not None else None
    ) or Divergence(
        stage=STAGE_SQL_STRUCTURE,
        summary="SQL 구조 서명이 다르다",
        detail={"legacy": legacy_signature, "event_ir": event_signature},
    )
    return StageResult(
        stage=STAGE_SQL_STRUCTURE, status=FAIL, detail="구조 서명 불일치", divergences=(divergence,)
    )


def fixture_stage(result: FixtureResult) -> StageResult:
    """④ 경계 fixture 실행 결과 집합이 같은가(+ 중복 회원 발생 여부)."""
    divergences: list[Divergence] = []
    if not result.matches:
        divergences.append(Divergence(
            stage=STAGE_ADVERSARIAL_FIXTURE,
            summary="경계 fixture 에서 결과 회원 집합이 다르다",
            detail={
                "only_in_legacy": list(result.only_in_legacy),
                "only_in_event_ir": list(result.only_in_event_ir),
                "legacy_sql": result.legacy_sql,
                "event_ir_sql": result.event_ir_sql,
            },
        ))
    if result.has_duplicates:
        divergences.append(Divergence(
            stage=STAGE_ADVERSARIAL_FIXTURE,
            summary="결과에 중복 회원이 있다(조인 증폭)",
            detail={
                "legacy_rows": len(result.legacy_members),
                "legacy_distinct": len(set(result.legacy_members)),
                "event_ir_rows": len(result.event_ir_members),
                "event_ir_distinct": len(set(result.event_ir_members)),
            },
        ))
    if divergences:
        return StageResult(
            stage=STAGE_ADVERSARIAL_FIXTURE, status=FAIL,
            detail=f"차이 {len(divergences)}건", divergences=tuple(divergences),
        )
    if _vacuous(len(set(result.legacy_members)), len(set(result.event_ir_members))):
        # fixture 는 경계 회원을 **넣어서** 만들지만, 그 경계가 이 조건에 걸리지 않으면 결과는
        # 0명끼리 같아진다 — 코퍼스에 경계를 더해야 한다는 신호이지 통과가 아니다.
        return not_run(STAGE_ADVERSARIAL_FIXTURE, VACUOUS_MATCH_REASON)
    return StageResult(
        stage=STAGE_ADVERSARIAL_FIXTURE, status=PASS,
        detail=f"회원 {len(set(result.legacy_members))}명 집합 동일",
    )


def apply_approvals(
    report: ShadowReport, approvals: Mapping[str, Approval]
) -> ShadowReport:
    """분류되지 않은 차이에 승인 기록을 붙인다(키: ``<stage>:<summary>``).

    승인은 **차이를 지우지 않는다** — 분류를 바꾸고 기록을 남길 뿐이며, 기록이 불완전하면
    :attr:`Divergence.blocks_cutover` 가 여전히 True 다.
    """
    stages: list[StageResult] = []
    for stage in report.stages:
        updated: list[Divergence] = []
        for divergence in stage.divergences:
            approval = approvals.get(f"{divergence.stage}:{divergence.summary}")
            if approval is None or divergence.kind != UNEXPLAINED_DIVERGENCE:
                updated.append(divergence)
                continue
            updated.append(Divergence(
                stage=divergence.stage, summary=divergence.summary,
                kind=APPROVED_LEGACY_BUG_FIX, detail=divergence.detail, approval=approval,
            ))
        stages.append(StageResult(
            stage=stage.stage, status=stage.status, detail=stage.detail,
            divergences=tuple(updated),
        ))
    return ShadowReport(
        asset_id=report.asset_id, asset_revision=report.asset_revision, stages=tuple(stages),
        semantic_fingerprint=report.semantic_fingerprint, source_fingerprint=report.source_fingerprint,
    )


def approvals_from_dicts(raw: Iterable[Mapping[str, Any]]) -> dict[str, Approval]:
    """승인 파일(JSON) → 승인 색인. 항목이 불완전하면 **읽는 단계에서** 실패한다."""
    approvals: dict[str, Approval] = {}
    for item in raw:
        key = str(item.get("key") or "")
        if not key:
            raise ShadowVerificationError("승인 항목에 key(<stage>:<summary>)가 없다")
        approval = Approval(
            before_meaning=str(item.get("before_meaning") or ""),
            after_meaning=str(item.get("after_meaning") or ""),
            affected_member_count=int(item.get("affected_member_count") or -1),
            approver=str(item.get("approver") or ""),
            approved_at=str(item.get("approved_at") or ""),
            reference=str(item.get("reference") or ""),
            rollback_criterion=str(item.get("rollback_criterion") or ""),
        )
        if not approval.is_complete:
            raise ShadowVerificationError(
                f"승인 기록이 불완전하다({key}): 누락 {', '.join(approval.missing_fields())}"
            )
        approvals[key] = approval
    return approvals


__all__ = [
    "APPROVED_LEGACY_BUG_FIX",
    "Approval",
    "BLOCKING_KINDS",
    "COUNT_BUCKETS",
    "DATA_VOLATILITY",
    "DIVERGENCE_KINDS",
    "Divergence",
    "EXPECTED_BINDING_CHANGE",
    "FAIL",
    "FixtureResult",
    "FixtureTable",
    "NONDETERMINISTIC_SOURCE",
    "NOT_RUN",
    "PASS",
    "PerformanceComparison",
    "PinnedPair",
    "REQUIRED_STAGES",
    "STAGE_ADVERSARIAL_FIXTURE",
    "STAGE_PATH_ACCOUNTING",
    "STAGE_PERFORMANCE",
    "STAGE_SEMANTIC_FINGERPRINT",
    "STAGE_SNAPSHOT_MEMBERS",
    "STAGE_SQL_STRUCTURE",
    "ShadowReport",
    "ShadowVerificationError",
    "SqliteVerificationDialect",
    "SnapshotComparison",
    "SnapshotCounts",
    "SnapshotExecutor",
    "StageResult",
    "Timing",
    "UNEXPLAINED_DIVERGENCE",
    "UNMEASURED_COST_AXES",
    "VACUOUS_MATCH_REASON",
    "apply_approvals",
    "approvals_from_dicts",
    "build_cost_probe_sql",
    "build_fixture_tables",
    "build_snapshot_count_sql",
    "build_snapshot_sample_sql",
    "clock_functions_in",
    "engine_anchor_date",
    "engine_clock_anchor",
    "fingerprint_stage",
    "fixture_stage",
    "not_run",
    "offset_date",
    "path_accounting_stage",
    "performance_stage",
    "pin_both_sides",
    "pin_execution_clock",
    "residual_clock_functions",
    "run_fixture_comparison",
    "run_performance_comparison",
    "run_snapshot_comparison",
    "shape_class",
    "snapshot_stage",
    "sql_structure_signature",
    "sql_structure_stage",
    "subject_binding",
]
