# 다중 DB 접근 (db_connections.py)

프로젝트에서 사용하는 4개 DB를 `db_connections.py` 한 곳에서 관리한다. 원격 3개는 읽기 전용이다.

| 이름 | 종류 | 접근 | 읽기전용 강제 |
|------|------|------|--------------|
| `postgres` | PostgreSQL(로컬) | 읽기/쓰기 | (RW) |
| `quadmax_sdz` | MariaDB | 읽기 전용 | 서버 `SET SESSION TRANSACTION READ ONLY` |
| `CRMDW` | SQL Server (Smart_QuadMax_Mart) | 읽기 전용 | 앱 레벨 SELECT 전용 가드(`sql_guard`) |

> SQL Server 는 PostgreSQL/MySQL 과 달리 서버 레벨 read-only 트랜잭션이 없다. 따라서
> `sql_guard.validate_sql` 로 **SELECT 계열(SELECT/WITH)만** 통과시키는 애플리케이션 가드로
> 강제한다. INSERT/UPDATE/DELETE/DROP 등은 실행 전에 거부된다.

## 서버별 상세 (실측: 2026-07-21)

각 커넥션이 실제로 어떤 서버·DB를 가리키는지, SQL 방언(dialect)과 함께 정리한다.
**쿼리 생성 시 대상 커넥션의 방언에 맞춰야 한다** — 특히 행수 제한.

| 커넥션 | 엔진 | 호스트:포트 | 서버명 | DB(카탈로그) | 기본 스키마 | 행수 제한 방언 |
|--------|------|------------|--------|--------------|------------|----------------|
| `quadmax_sdz` | MariaDB/MySQL | 58.226.35.74:3306 | — | `quadmax_sdz` (+`quadmax_sdz_mart` 등 다수 스키마) | (DB=스키마) | `... LIMIT n` |
| `CRMDW` | SQL Server 2012 (SP4) | 58.226.35.75:1433 | `EASYCORE-75` | `Smart_QuadMax_Mart` | `dbo` | `SELECT TOP n ...` |

- `quadmax_sdz` (MariaDB): 분석 마트/원천이 스키마별로 나뉘어 있다. SDZ 계열은 `quadmax_sdz`,
  마트는 `quadmax_sdz_mart`. 회원 기준정보 복제본 `crm_mb_baseinfo`, 코드 `crm_cm_code` 등을 보유.
  MySQL 방언이므로 **`LIMIT n`** 사용.
- `CRMDW` (SQL Server `Smart_QuadMax_Mart`): **본사 원천/마트(DWH)**. 몰/장바구니 ODS와 CRM 원천이
  여기에 모여 있다 — 예: `dbo.ODS_MALL_OMS_CART`, `dbo.CRM_MB_BASEINFO`, `dbo.CRM_CM_PRODUCT`.
  본사 데이터 기반 타겟팅 SQL(장바구니·상품·회원 조인)은 **이 커넥션**에서 실행한다.
  MSSQL 방언이므로 **`SELECT TOP n`**(❌ `LIMIT` 미지원).

> ⚠️ 방언 주의: 행수 제한을 **붙일 경우** MySQL은 `... LIMIT n`, MSSQL은 `SELECT [DISTINCT] TOP n ...`
> 이어야 한다. `sql_guard.validate_sql(dialect=|table_dialects=)` 가 대상 방언에 맞춰 자동 처리한다.
>
> 단, **타겟 오디언스는 전체 결과가 나와야 하므로 행수 제한을 붙이지 않는다**(`validate_sql(default_limit=None)`).
> 적용: `build_sql_result` 후보 검증과 `_assert_select_only` 모두 `default_limit=None`(SELECT 강제만 수행).
> 따라서 생성/실행 SQL 에는 LIMIT/TOP 이 없고 `result_row_count` 는 전체 인원을 반영한다.

## 사용법

가장 간단한 진입점은 `run_read_query` 다. 원격 DB 는 SELECT 만 통과한다.

```python
import db_connections as db

# MariaDB
rows = db.run_read_query(
    "quadmax_sdz",
    "SELECT customer_id, grade FROM member WHERE grade = %s LIMIT 100",
    ("VIP",),
)

# SQL Server (CRMDW)
rows = db.run_read_query(
    "CRMDW",
    "SELECT TOP 100 customer_id, ltv FROM dbo.customer_summary WHERE region = %s",
    ("Seoul",),
)
```

- 반환값은 `list[dict]`.
- 파라미터 바인딩: MariaDB/SQL Server(pymysql/pymssql) 모두 `%s` 플레이스홀더 사용.
- 원격 DB 에 SELECT 가 아닌 문장을 넣으면 `ValueError("read-only 위반 ...")` 로 즉시 거부.

세밀한 제어가 필요하면 연결 컨텍스트 매니저를 직접 쓴다.

```python
with db.quadmax_connection() as conn:      # 세션이 READ ONLY 로 시작됨
    with conn.cursor() as cur:
        cur.execute("SELECT ...")
        rows = cur.fetchall()

with db.crmdw_connection() as conn:        # SQL Server
    cur = conn.cursor()
    cur.execute("SELECT ...")
    rows = cur.fetchall()

with db.postgres_connection(read_only=False) as conn:   # 로컬, 쓰기 가능
    ...
```

## 다중 DB 타겟팅 (집합 연산)

DB 가 서로 달라 **하나의 쿼리로 조인할 수 없을 때**는, DB별로 SQL 을 각각 돌려서 나온
ID 집합을 **합집합/교집합/차집합**으로 결합한다. `run_set_targeting` 이 이 과정을 결정적으로
수행한다.

> 같은 DB(스키마 접근 가능)면 한 쿼리로 조인해서 타겟팅하면 되므로 이 함수가 필요 없다.

```python
import db_connections as db

result = db.run_set_targeting([
    # 1) 기저 집합: CRMDW 에서 VIP 고객
    {"db": "CRMDW", "sql": "SELECT customer_id FROM dbo.vip WHERE grade='A'", "key": "customer_id"},
    # 2) 교집합: quadmax_sdz 에서 최근 구매자
    {"db": "quadmax_sdz", "op": "intersect",
     "sql": "SELECT user_id FROM orders WHERE ordered_at >= %s", "params": ("2026-01-01",), "key": "user_id"},
    # 3) 차집합: CRMDW 에서 수신거부 고객 제외
    {"db": "CRMDW", "op": "difference",
     "sql": "SELECT customer_id FROM dbo.optout", "key": "customer_id"},
], default_key="user_id")

result["target_ids"]     # 최종 타겟 ID 목록(정렬됨)
result["target_count"]   # 최종 인원 수
result["steps"]          # 각 step: {db, key, op, row_count, id_count}
result["accumulated_count"]  # step 적용마다 누산기 크기 변화
```

동작 규칙:
- 각 step 의 `sql` 은 SELECT 여야 하고(원격 DB 는 자동 강제), ID 로 쓸 컬럼을 SELECT 에 포함한다.
- 컬럼명이 DB마다 다르면(`customer_id` vs `user_id`) step 별 `key` 로 지정한다. 결합은 **값** 기준.
- 첫 step 은 기저 집합(op 없음). 2번째부터 `op` 필수. 누산기에 **왼쪽부터** 접힌다
  (`acc = acc OP thisSet`), 즉 `((base ∩ s2) − s3) ∪ s4` 처럼 평가된다.
- `op` 별칭: `union`/`합집합`, `intersect`/`교집합`, `difference`/`차집합`.
- 각 SQL 은 자기 DB 에서만 실행되므로 교차 DB 조인이 없다(스키마 접근 불가 상황에 적합).

## 설정 (.env)

자격증명은 소스에 넣지 않고 `.env`(gitignore됨)에서 읽는다. `docker-compose` 가 `env_file` 로
컨테이너에 주입한다.

```dotenv
# MariaDB (읽기 전용)
QUADMAX_DB_HOST=58.226.35.74
QUADMAX_DB_PORT=3306
QUADMAX_DB_NAME=quadmax_sdz
QUADMAX_DB_USER=quadmax
QUADMAX_DB_PASSWORD=...

# SQL Server (CRMDW, 읽기 전용)
MSSQL_HOST=58.226.35.75
MSSQL_PORT=1433
MSSQL_USER=sa
MSSQL_PASSWORD=...
CRMDW_DB_NAME=smart_quadmax_mart
```

미설정 시 원격 DB 접속은 `RuntimeError`(접속정보 없음)로 실패한다(안전한 기본값을 두지 않음).

## 드라이버 / 이미지

`requirements.txt` 에 `pymysql`, `pymssql` 이 포함된다. 추가 후에는 이미지를 재빌드해야 한다.

```bash
docker compose build python api
docker compose up -d
```

## 현재 상태 (2026-07-21)

- `quadmax_sdz`: 조회 + 읽기전용 강제(앱/서버) 검증 완료.
- `CRMDW`: `MSSQL_PASSWORD` 교체 후 **로그인/조회 정상**(이전 `18456 Login failed` 해소).
  MCP 설정(`.mcp.json`) 변경 후에는 `/mcp reconnect` 로 서버를 재연결해야 반영된다.
- 본사 타겟팅 SQL의 3개 테이블(`ODS_MALL_OMS_CART`/`CRM_MB_BASEINFO`/`CRM_CM_PRODUCT`)은 모두
  `CRMDW`(`Smart_QuadMax_Mart`)의 `dbo` 스키마에 존재함을 실측 확인.
- **앱 실행 라우팅(카운트/샘플)**: `/target-sql` 이 생성한 타겟 SQL 은 `build_sql_result` 가
  `schema_catalog.json` 의 `database` 로 `target_connection` 을 판별해 api_response 로 넘기고,
  `execute_target_sql(target_connection=)` 가 외부 RO DB(CRMDW 등)면 `run_read_query` 로 실행해
  `result_row_count`/`target_customer_count`/샘플을 채운다. 로컬 테이블이면 기존 postgres 경로.
  세그먼트 구성·오디언스 저장은 로컬 postgres 스키마 전용이라 외부 DB 대상일 때는 생략된다.
- 배포: 코드 변경 후 `docker compose restart api` 로 반영 안 될 때가 있어
  `docker compose up -d --force-recreate --no-deps api` 로 재생성해야 확실히 로드된다.
