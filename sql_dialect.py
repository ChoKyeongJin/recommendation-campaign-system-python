"""SQL 방언 어댑터 — 결정론 타겟 빌더가 특정 DBMS 문법을 소스에 직접 박지 않게 하는 계층.

배경(docs/operations/db_portability_audit.md §4-A): 타겟팅 대상 실DB는 계속 다른 DB로
바뀔 수 있다(MSSQL→MariaDB→…). 날짜창 컷오프·문자열 길이·캐스트·잠금 힌트 같은 **엔진
문법**은 전부 여기 어댑터가 소유하고, 빌더는 논리 연산(예: "기준일-N일을 YYYYMMDD 로")만
말한다. 역할 경계:

- 테이블/컬럼/코드값(스키마 지식)   → member_target_filters.json / schema_catalog.json 소유
- 행수 제한(TOP/LIMIT) 부착·검증    → sql_guard 소유(여기서 다루지 않음)
- 테이블→방언 판별                  → sql_guard.load_table_dialects 가 카탈로그에서 도출
- 방언 이름 → 실제 문법 렌더        → **이 모듈**

'char8' 계열 메서드는 실CRM 의 날짜 저장 관례(nvarchar(8) 'YYYYMMDD' 문자열, 포맷 고정이라
문자열 대소 = 날짜 대소)를 전제로 한 렌더다 — 포맷 자체는 member_target_filters.json
base_entity.date_format 이 선언하는 스키마 사실이고, 여기는 그 포맷의 방언별 표현만 안다.
"""

from __future__ import annotations


class SqlDialect:
    """ANSI/PostgreSQL 지향 기본 방언. 서브클래스가 엔진별 문법만 오버라이드한다."""

    name = "ansi"

    # ── 시각/날짜 연산 ──────────────────────────────────────────────
    def now(self) -> str:
        """현재 시각 앵커."""
        return "NOW()"

    def clock_functions(self) -> tuple[str, ...]:
        """이 방언에서 **값이 실행 시점에 정해지는** 시계 함수 어휘.

        롤링 창('최근 90일')은 계획 시점 날짜로 굳히지 않고 이 함수들로 렌더된다. 그래서 두 산출물을
        대조할 때는 "어디가 시계에 걸려 있는가"를 알아야 한다 — 두 번 실행하면 두 답이 나오는 자리이고,
        그 차이를 의미 차이로 읽으면 검증이 거짓말을 한다. :meth:`now` 만 알면 자기 렌더러가 만든 것만
        보이므로, **남이 만든 SQL 에 남아 있는 시계**까지 세도록 어휘 전체를 선언한다.
        """
        return ("NOW()", "CURRENT_TIMESTAMP")

    def datetime_anchor(self, iso_timestamp: str) -> str:
        """기준 시각 하나를 **타입 있는** datetime 식으로 — :meth:`now` 자리에 그대로 끼울 수 있어야 한다.

        따옴표만 씌운 문자열을 쓰면 안 된다: ``DATEADD`` 자리에서는 암묵 변환으로 통하지만
        ``to_char8(now())`` 자리에서는 문자열을 문자열로 자르는 다른 뜻이 되고, 그 차이는 오류가
        아니라 **조용히 틀린 컷오프**로 나온다.
        """
        return f"CAST({quote_literal(iso_timestamp)} AS TIMESTAMP)"

    def date_sub_days(self, anchor: str, days: int) -> str:
        """anchor(날짜/시각 식)에서 days 일 전."""
        return f"({anchor} - INTERVAL '{int(days)} day')"

    def date_add_days(self, anchor: str, days: int) -> str:
        """anchor 에서 days 일 후. ``date_sub_days(anchor, -days)`` 로 대신하지 않는다 —
        엔진에 따라 부호가 겹쳐(``DATEADD(DAY, --30, …)``) 주석 토큰이 되기 때문이다."""
        return f"({anchor} + INTERVAL '{int(days)} day')"

    # ── YYYYMMDD(char8) 문자열 컬럼 관례 ───────────────────────────
    def to_char8(self, expr: str) -> str:
        """날짜 식을 'YYYYMMDD' 문자열로 렌더(char8 컬럼과 사전식 비교용)."""
        return f"TO_CHAR({expr}, 'YYYYMMDD')"

    def parse_char8(self, expr: str) -> str:
        """'YYYYMMDD' 문자열 식을 날짜로 파싱(날짜 연산 전 단계)."""
        return f"TO_DATE({expr}, 'YYYYMMDD')"

    def char8_cutoff(self, days: int, anchor: str | None = None) -> str:
        """(anchor 또는 오늘) - days 일을 'YYYYMMDD' 로 — char8 컬럼 기간창 컷오프."""
        return self.to_char8(self.date_sub_days(anchor or self.now(), days))

    def char8_today(self) -> str:
        """오늘을 'YYYYMMDD' 로."""
        return self.to_char8(self.now())

    def char8_shift(self, char8_expr: str, days: int) -> str:
        """'YYYYMMDD' 문자열 식을 days 일만큼 옮겨 다시 'YYYYMMDD' 로.

        시점 기준 상대 조건('첫 구매 후 30일 이내')처럼 **컬럼에서 읽은 날짜**를 앵커로 삼는 경우가
        있다. 리터럴 컷오프(char8_cutoff)와 달리 앵커가 식이므로 파싱→연산→포맷을 왕복한다."""
        shifted = self.date_add_days(self.parse_char8(char8_expr), days) if days >= 0 else \
            self.date_sub_days(self.parse_char8(char8_expr), -days)
        return self.to_char8(shifted)

    def datetime_cutoff(self, days: int) -> str:
        """오늘 - days 일(진짜 날짜/시각 컬럼과 직접 비교용 — char8 변환 없음)."""
        return self.date_sub_days(self.now(), days)

    def char8_valid(self, col: str) -> str:
        """char8 컬럼 정상값 가드(널/이상치 제외) — 비교 술어 앞에 AND 로 붙인다."""
        return f"{col} IS NOT NULL AND {self.str_len(col)} = 8"

    # ── 일반 함수 문법 ─────────────────────────────────────────────
    def str_len(self, expr: str) -> str:
        return f"LENGTH({expr})"

    def cast_bigint(self, expr: str) -> str:
        """조인키 타입 정합용 정수 캐스트(문자 회원번호 ↔ 숫자 회원번호)."""
        return f"CAST({expr} AS BIGINT)"

    def coalesce(self, expr: str, default_sql: str) -> str:
        return f"COALESCE({expr}, {default_sql})"

    def concat(self, *parts: str) -> str:
        return f"CONCAT({', '.join(parts)})"

    # ── 행 수 제한(상위 N) ─────────────────────────────────────────
    # 엔진이 제한을 앞(SELECT TOP n)에 두느냐 뒤(LIMIT n)에 두느냐만 다르다. 두 자리를 모두
    # 물어보게 해서 렌더러가 위치를 알 필요 없게 한다 — 쓰지 않는 쪽은 빈 문자열이다.
    def row_limit_prefix(self, limit: int) -> str:
        """SELECT 바로 뒤에 붙는 제한 구문(없으면 빈 문자열)."""
        return ""

    def row_limit_suffix(self, limit: int) -> str:
        """문장 끝에 붙는 제한 구문(없으면 빈 문자열)."""
        return f"LIMIT {int(limit)}"

    # ── 힌트 ───────────────────────────────────────────────────────
    def nolock_hint(self) -> str:
        """더티리드 허용 테이블 힌트(진단용 COUNT 등). 지원 안 하는 엔진은 빈 문자열."""
        return ""


class TSqlDialect(SqlDialect):
    """SQL Server(T-SQL). 실CRM(CRMDW/CRMAN)의 현행 방언 — 기존 빌더 출력과 문자열 동일."""

    name = "tsql"

    def now(self) -> str:
        return "GETDATE()"

    def clock_functions(self) -> tuple[str, ...]:
        return ("GETDATE()", "GETUTCDATE()", "SYSDATETIME()", "SYSUTCDATETIME()", "CURRENT_TIMESTAMP")

    def datetime_anchor(self, iso_timestamp: str) -> str:
        return f"CAST({quote_literal(iso_timestamp)} AS DATETIME)"

    def date_sub_days(self, anchor: str, days: int) -> str:
        return f"DATEADD(DAY, -{int(days)}, {anchor})"

    def date_add_days(self, anchor: str, days: int) -> str:
        return f"DATEADD(DAY, {int(days)}, {anchor})"

    def to_char8(self, expr: str) -> str:
        return f"CONVERT(CHAR(8), {expr}, 112)"

    def parse_char8(self, expr: str) -> str:
        return f"CONVERT(DATE, {expr}, 112)"

    def str_len(self, expr: str) -> str:
        return f"LEN({expr})"

    def cast_bigint(self, expr: str) -> str:
        # TRY_CAST: 이상치(비숫자 문자열)를 오류 대신 NULL 로 — 조인에서 자연 탈락.
        return f"TRY_CAST({expr} AS BIGINT)"

    def coalesce(self, expr: str, default_sql: str) -> str:
        return f"ISNULL({expr}, {default_sql})"

    def row_limit_prefix(self, limit: int) -> str:
        return f"TOP {int(limit)} "

    def row_limit_suffix(self, limit: int) -> str:
        return ""

    def nolock_hint(self) -> str:
        return " WITH(NOLOCK)"


class MySqlDialect(SqlDialect):
    """MySQL/MariaDB(quadmax_sdz 계열)."""

    name = "mysql"

    def clock_functions(self) -> tuple[str, ...]:
        return ("NOW()", "SYSDATE()", "CURRENT_TIMESTAMP", "UTC_TIMESTAMP()")

    def datetime_anchor(self, iso_timestamp: str) -> str:
        return f"CAST({quote_literal(iso_timestamp)} AS DATETIME)"

    def date_sub_days(self, anchor: str, days: int) -> str:
        return f"DATE_SUB({anchor}, INTERVAL {int(days)} DAY)"

    def date_add_days(self, anchor: str, days: int) -> str:
        return f"DATE_ADD({anchor}, INTERVAL {int(days)} DAY)"

    def to_char8(self, expr: str) -> str:
        return f"DATE_FORMAT({expr}, '%Y%m%d')"

    def parse_char8(self, expr: str) -> str:
        return f"STR_TO_DATE({expr}, '%Y%m%d')"

    def str_len(self, expr: str) -> str:
        return f"CHAR_LENGTH({expr})"

    def cast_bigint(self, expr: str) -> str:
        return f"CAST({expr} AS SIGNED)"


class PostgresDialect(SqlDialect):
    """PostgreSQL(로컬 데모 DB). ANSI 기본과 동일 문법이라 이름만 구분한다."""

    name = "postgres"

    def clock_functions(self) -> tuple[str, ...]:
        # clock_timestamp() 는 같은 문장 안에서도 호출마다 다른 값을 낸다 — 문장 하나로 묶는 것만으로는
        # 앵커가 고정되지 않는 유일한 항목이라 어휘에 반드시 있어야 한다.
        return ("NOW()", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP", "STATEMENT_TIMESTAMP()", "CLOCK_TIMESTAMP()")

    def datetime_anchor(self, iso_timestamp: str) -> str:
        return f"TIMESTAMP {quote_literal(iso_timestamp)}"


_DIALECTS: dict[str, SqlDialect] = {
    "tsql": TSqlDialect(),
    "mssql": TSqlDialect(),
    "sqlserver": TSqlDialect(),
    "mysql": MySqlDialect(),
    "mariadb": MySqlDialect(),
    "postgres": PostgresDialect(),
    "postgresql": PostgresDialect(),
    "ansi": SqlDialect(),
}


def get_dialect(name: str | None) -> SqlDialect:
    """방언 이름('tsql'|'mysql'|'postgres'|…)으로 어댑터를 얻는다. 미지의 이름은 ANSI 기본."""
    return _DIALECTS.get((name or "").strip().casefold(), _DIALECTS["ansi"])


# 커넥션명 → 방언. db_connections._DB_DIALECTS 와 겹치는 지식이다 — 단일 진실 소스화(감사 §4-B)
# 때 db_connections 가 이 맵을 쓰도록 통합할 것. 커넥션명 자체가 배포별 사실이므로, 장기적으로는
# schema_catalog.json databases 설명(load_table_dialects 참고)에서 도출하는 것이 목표다.
CONNECTION_DIALECTS: dict[str, str] = {
    "CRMAN": "tsql",
    "CRMDW": "tsql",
    "quadmax_sdz": "mysql",
    "postgres": "postgres",
}


def dialect_for_connection(connection: str | None, default: str = "ansi") -> SqlDialect:
    """커넥션명으로 방언 어댑터를 얻는다(미등록 커넥션은 default)."""
    return get_dialect(CONNECTION_DIALECTS.get(connection or "", default))


def all_clock_functions() -> tuple[str, ...]:
    """등록된 **모든** 방언의 실행 시점 시계 어휘.

    한 방언의 어휘만으로 SQL 을 훑으면 다른 방언이 렌더한 시계를 '없다'고 세게 된다 — 그 SQL 은
    실행하면 문법 오류로 죽지만, 그 전에 "고정할 시계가 없다 → 고정 없이 대조해도 된다"는 잘못된
    판정을 통과한다. 잔여 검사는 아는 어휘 전부로 한다.
    """
    return tuple(sorted({token for dialect in _DIALECTS.values() for token in dialect.clock_functions()}))


# ── SQL 리터럴 렌더(방언 무관) ────────────────────────────────────────────────────────
# 홑따옴표 이스케이프와 유니코드 LIKE 술어는 지금까지 세 모듈이 각자 복제하고 있었고,
# "바이트 동일 출력"이라는 계약은 주석으로만 존재했다. 실제로 갈라져 있었다 — graph_rag 쪽만
# str() 캐스팅이 없어 비문자열 입력에서 AttributeError 를 던졌다. 같은 WHERE 리스트에 섞이는
# 리터럴이라 한 글자만 달라도 다른 SQL 이 되므로, 여기 한 곳이 소유한다.


def quote_literal(value: object) -> str:
    """문자열 리터럴로 감싼다('O'Brien' → 'O''Brien'). 비문자열은 str() 로 강제한다."""
    return "'" + str(value).replace("'", "''") + "'"


def nlike_contains(column: str, term: object) -> str:
    """유니코드 부분일치 LIKE 술어(N'%term%').

    N 접두어는 tsql/mysql 모두 유효해 한글 리터럴을 안전하게 비교한다.
    """
    return f"{column} LIKE N'%{str(term).replace(chr(39), chr(39) * 2)}%'"
