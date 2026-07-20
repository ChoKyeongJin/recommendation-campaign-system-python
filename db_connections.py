"""다중 DB 연결 계층.

프로젝트에서 사용하는 4개 DB를 한 곳에서 관리한다.

| 이름          | 종류              | 접근      |
|---------------|-------------------|-----------|
| postgres      | PostgreSQL(로컬)  | 읽기/쓰기 |
| quadmax_sdz   | MariaDB/MySQL     | 읽기 전용 |
| CRMAN         | SQL Server        | 읽기 전용 |
| CRMDW         | SQL Server        | 읽기 전용 |

읽기 전용 강제 방식(DB마다 다름):
- postgres : 트랜잭션을 READ ONLY 로 시작(`default_transaction_read_only`). 서버 레벨.
- quadmax_sdz(MariaDB) : `START TRANSACTION READ ONLY`. 서버 레벨.
- CRMAN/CRMDW(SQL Server) : 서버 레벨 read-only 트랜잭션이 없어, `sql_guard` 로 검증한
  **SELECT 계열 문장만** 실행하고 절대 커밋/DML을 하지 않는다(애플리케이션 레벨).

접속 정보는 모두 환경변수에서 읽는다(자격증명을 소스에 하드코딩하지 않음). 값은 `.env`
(gitignore됨)에 둔다. 미설정 시 접속은 실패한다(read-only 원격 DB에 대한 안전한 기본값 없음).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from sql_guard import DEFAULT_LIMIT, load_allowed_tables, validate_sql

READ_ONLY_DBS = ("quadmax_sdz", "CRMAN", "CRMDW")
ALL_DBS = ("postgres",) + READ_ONLY_DBS


def _env(*names_then_default: str | None) -> str | None:
    # 여러 환경변수 후보를 순서대로 확인해 첫 번째 비어있지 않은 값을 반환한다.
    # 마지막 인자는 기본값(None 가능). 빈 문자열("")은 미설정으로 간주한다.
    *names, default = names_then_default
    for name in names:
        value = os.getenv(name or "")
        if value:
            return value
    return default


# ---------------------------------------------------------------------------
# 접속 설정 (환경변수)
# ---------------------------------------------------------------------------
def postgres_config() -> dict[str, Any]:
    return {
        "host": _env("POSTGRES_HOST", "postgres"),
        "port": int(_env("POSTGRES_PORT", "5432")),
        "dbname": _env("POSTGRES_DB", "campaign_db"),
        "user": _env("POSTGRES_USER", "postgres"),
        "password": _env("POSTGRES_PASSWORD", "1234"),
    }


def quadmax_config() -> dict[str, Any]:
    return {
        "host": _env("QUADMAX_DB_HOST", None),
        "port": int(_env("QUADMAX_DB_PORT", "3306")),
        "dbname": _env("QUADMAX_DB_NAME", "quadmax_sdz"),
        "user": _env("QUADMAX_DB_USER", None),
        "password": _env("QUADMAX_DB_PASSWORD", None),
    }


def _mssql_config(prefix: str, default_db: str) -> dict[str, Any]:
    # CRMAN/CRMDW 는 host/port/user/password 를 공유할 수 있으므로 공용 MSSQL_* 로 폴백한다.
    return {
        "host": _env(f"{prefix}_DB_HOST", "MSSQL_HOST", None),
        "port": int(_env(f"{prefix}_DB_PORT", "MSSQL_PORT", "1433")),
        "dbname": _env(f"{prefix}_DB_NAME", default_db),
        "user": _env(f"{prefix}_DB_USER", "MSSQL_USER", None),
        "password": _env(f"{prefix}_DB_PASSWORD", "MSSQL_PASSWORD", None),
    }


def crman_config() -> dict[str, Any]:
    return _mssql_config("CRMAN", "Customer_Analytics")


def crmdw_config() -> dict[str, Any]:
    return _mssql_config("CRMDW", "smart_quadmax_mart")


def _require(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    missing = [key for key in ("host", "user", "password") if not cfg.get(key)]
    if missing:
        raise RuntimeError(
            f"db_connections: '{name}' 접속정보가 없습니다(환경변수 미설정): {', '.join(missing)}"
        )
    return cfg


# ---------------------------------------------------------------------------
# 연결 (컨텍스트 매니저) — read-only DB 는 서버/앱 레벨로 읽기 전용 강제
# ---------------------------------------------------------------------------
@contextmanager
def postgres_connection(read_only: bool = False) -> Iterator[Any]:
    import psycopg
    from psycopg.rows import dict_row

    cfg = postgres_config()
    conninfo = (
        f"host={cfg['host']} port={cfg['port']} dbname={cfg['dbname']} "
        f"user={cfg['user']} password={cfg['password']}"
    )
    if read_only:
        conninfo += " options='-c default_transaction_read_only=on'"
    with psycopg.connect(conninfo, row_factory=dict_row, connect_timeout=5) as conn:
        conn.read_only = read_only
        yield conn


@contextmanager
def quadmax_connection() -> Iterator[Any]:
    import pymysql

    cfg = _require(quadmax_config(), "quadmax_sdz")
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["dbname"],
        connect_timeout=5,
        read_timeout=15,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        # MariaDB/MySQL: 세션 트랜잭션을 읽기 전용으로 강제(쓰기 시도 시 서버가 거부).
        with conn.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


@contextmanager
def _mssql_connection(cfg: dict[str, Any], name: str) -> Iterator[Any]:
    import pymssql

    cfg = _require(cfg, name)
    # encrypt=false 서버: pymssql(FreeTDS)은 기본적으로 암호화를 강제하지 않는다.
    conn = pymssql.connect(
        server=cfg["host"],
        port=str(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["dbname"],
        login_timeout=5,
        timeout=15,
        autocommit=True,  # DML 을 하지 않으므로 트랜잭션 상태를 남기지 않는다.
        as_dict=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def crman_connection():
    return _mssql_connection(crman_config(), "CRMAN")


def crmdw_connection():
    return _mssql_connection(crmdw_config(), "CRMDW")


# ---------------------------------------------------------------------------
# 통합 읽기 쿼리 — read-only DB 공통 진입점
# ---------------------------------------------------------------------------
def _assert_select_only(sql: str) -> str:
    # 모든 read-only DB 에 대한 애플리케이션 레벨 방어선(특히 서버 레벨 강제가 없는 MSSQL).
    result = validate_sql(sql, allowed_tables=None, default_limit=DEFAULT_LIMIT)
    blocking = [issue for issue in result["issues"] if issue["severity"] == "error"]
    if blocking:
        reasons = "; ".join(issue["message"] for issue in blocking)
        raise ValueError(f"read-only 위반: SELECT 문만 허용됩니다 ({reasons})")
    return result["safe_sql"]


def run_read_query(db: str, sql: str, params: Any = None, *, enforce_select: bool = True) -> list[dict[str, Any]]:
    """읽기 전용 DB에서 SELECT 결과를 dict 리스트로 반환한다.

    db: 'quadmax_sdz' | 'CRMAN' | 'CRMDW' | 'postgres'
    """
    if enforce_select and db in READ_ONLY_DBS:
        sql = _assert_select_only(sql)

    if db == "quadmax_sdz":
        with quadmax_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params or ())
                return list(cursor.fetchall())
    if db == "CRMAN":
        with crman_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            return list(cursor.fetchall())
    if db == "CRMDW":
        with crmdw_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            return list(cursor.fetchall())
    if db == "postgres":
        with postgres_connection(read_only=enforce_select) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

    raise ValueError(f"알 수 없는 db: {db!r} (사용 가능: {', '.join(ALL_DBS)})")


# ---------------------------------------------------------------------------
# 다중 DB 타겟팅 — DB 간 조인이 불가능할 때, DB별 SQL 을 각각 돌려
# 결과 ID 집합을 합집합/교집합/차집합으로 결합한다.
#
# (같은 DB/스키마 접근이 가능하면 한 쿼리로 조인하면 되므로 이 함수가 필요 없다.)
# ---------------------------------------------------------------------------
_UNION = {"union", "합집합", "or", "u", "|"}
_INTERSECT = {"intersect", "intersection", "교집합", "and", "&", "n"}
_DIFFERENCE = {"difference", "diff", "except", "minus", "차집합", "-"}


def _normalize_op(op: Any) -> str:
    token = str(op).strip().lower()
    if token in _UNION:
        return "union"
    if token in _INTERSECT:
        return "intersect"
    if token in _DIFFERENCE:
        return "difference"
    raise ValueError(f"알 수 없는 집합 연산: {op!r} (union|intersect|difference)")


def _step_id_set(step: dict[str, Any], default_key: str, index: int) -> tuple[set[Any], str, int]:
    db_name = step.get("db")
    sql = step.get("sql")
    if not db_name or not sql:
        raise ValueError(f"step[{index}] 에 'db' 와 'sql' 이 필요합니다.")
    key = step.get("key", default_key)
    rows = run_read_query(db_name, sql, step.get("params"))
    ids: set[Any] = set()
    for row in rows:
        if key not in row:
            raise ValueError(
                f"step[{index}] ({db_name}) 결과에 키 컬럼 '{key}' 이(가) 없습니다. "
                f"SQL 의 SELECT 에 '{key}' 를 포함하거나 step['key'] 를 지정하세요."
            )
        value = row[key]
        if value is not None:
            ids.add(value)
    return ids, key, len(rows)


def run_set_targeting(
    steps: list[dict[str, Any]],
    *,
    default_key: str = "user_id",
) -> dict[str, Any]:
    """여러 DB에서 각각 SELECT 를 실행해 ID 집합을 집합 연산으로 결합한다.

    steps: 각 항목은 dict
        - db     : 'quadmax_sdz' | 'CRMAN' | 'CRMDW' | 'postgres'
        - sql    : SELECT 문 (원격 DB 는 SELECT 만 허용)
        - params : (선택) 바인딩 파라미터
        - key    : (선택) ID 로 사용할 컬럼명. 기본값 default_key
        - op     : 2번째 step 부터 필수 — union | intersect | difference
                   (한글/기호 별칭 허용). 누산기에 왼쪽부터 접힌다: acc = acc OP thisSet
      첫 step 은 op 를 두지 않는다(기저 집합).

    반환:
        {
          "target_ids": [...정렬된 최종 ID...],
          "target_count": int,
          "key": default_key,
          "steps": [{"db", "key", "op", "row_count", "id_count"}...],
          "accumulated_count": [각 step 적용 후 누산기 크기...],
        }
    """
    if not steps:
        raise ValueError("steps 가 비었습니다.")

    accumulator: set[Any] = set()
    step_reports: list[dict[str, Any]] = []
    accumulated_count: list[int] = []

    for index, step in enumerate(steps):
        ids, key, row_count = _step_id_set(step, default_key, index)
        if index == 0:
            if step.get("op"):
                raise ValueError("첫 step 에는 op 를 지정하지 않습니다(기저 집합).")
            op = None
            accumulator = set(ids)
        else:
            if not step.get("op"):
                raise ValueError(f"step[{index}] 에 op(union|intersect|difference)가 필요합니다.")
            op = _normalize_op(step["op"])
            if op == "union":
                accumulator |= ids
            elif op == "intersect":
                accumulator &= ids
            else:  # difference
                accumulator -= ids

        step_reports.append(
            {"db": step["db"], "key": key, "op": op, "row_count": row_count, "id_count": len(ids)}
        )
        accumulated_count.append(len(accumulator))

    return {
        "target_ids": sorted(accumulator),
        "target_count": len(accumulator),
        "key": default_key,
        "steps": step_reports,
        "accumulated_count": accumulated_count,
    }
