# 실제 DB 연결 (SELECT 전용) 운영 가이드

샘플 데이터(docker 로컬 postgres) 대신 **실제 business DB**를 연결하되, 앱이 실제 데이터를
**절대 변경하지 못하도록 SELECT 전용**으로만 접속하는 구성이다.

## 두 개의 DB 역할

| 역할 | 연결 함수 | 접근 | 담는 것 |
|------|-----------|------|---------|
| **business (실제 소스)** | `_business_conninfo()` | **읽기 전용** | campaigns/users 등 실제 소스 데이터. NL2SQL 타겟팅 조회 대상 |
| **로컬 쓰기(메타데이터/운영)** | `_metadata_conninfo()` / `_postgres_conninfo()` | 읽기+쓰기 | 타겟 오디언스·멤버·실패로그·프롬프트·정책(메타데이터)과 FK로 묶인 A/B 실험·발송·이벤트(운영) |

- NL2SQL 타겟팅 조회(`execute_target_sql`)만 실제 business DB에서 읽는다. `graph_rag` 는 SQL을
  **생성만** 하고 DB에 직접 접속하지 않는다.
- 타겟 오디언스는 business DB에서 읽은 멤버 목록을 **로컬 메타데이터 DB에 저장**한다
  (실제 DB에 쓰지 않음).

## 읽기 전용 강제 방식

`_business_conninfo()` 는 conninfo 에 `options='-c default_transaction_read_only=on'` 을 넣어
**서버 레벨**에서 모든 트랜잭션을 읽기 전용으로 강제한다. 추가로 `execute_target_sql` 은
`conn.read_only = True` 로 한 번 더 강제한다(이중 안전장치). INSERT/UPDATE/DELETE/DDL 은
`ReadOnlySqlTransaction` 오류로 거부된다.

> 애플리케이션 레벨 SELECT-only 검증(`sql_guard.py`)은 그대로 유지되며, 위 DB 레벨 강제와 함께
> 이중으로 방어한다.

## 설정 방법

`.env` 에서 `BUSINESS_DB_*` 를 지정한다(미지정 시 `POSTGRES_*` 로 폴백).

```dotenv
BUSINESS_DB_HOST=host.docker.internal   # 도커 밖 호스트의 DB일 때
BUSINESS_DB_PORT=5432
BUSINESS_DB_NAME=your_real_db
BUSINESS_DB_USER=readonly_user
BUSINESS_DB_PASSWORD=change_me
```

`.env` 는 `docker-compose` 의 `env_file` 로 컨테이너에 자동 주입된다.

### (권장) DB 계정 자체도 SELECT 권한만 부여

애플리케이션의 읽기 전용 강제와 **별개로**, DBA가 SELECT 권한만 가진 계정을 만들어
`BUSINESS_DB_USER` 로 쓰면 방어가 한 겹 더 늘어난다.

```sql
CREATE ROLE readonly_user LOGIN PASSWORD 'change_me';
GRANT CONNECT ON DATABASE your_real_db TO readonly_user;
GRANT USAGE ON SCHEMA public TO readonly_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly_user;
```

## ⚠️ 실제 DB에 `docs/data/local_bootstrap.sql` 을 실행하지 말 것

`docs/data/local_bootstrap.sql` 은 로컬 샘플 DB용이며 맨 앞에서 `DROP TABLE ... CASCADE` 로 기존 데이터를
전부 삭제한 뒤 재생성한다. **실제 business DB에 실행하면 데이터가 소실**된다. 실제 DB는
SELECT 전용으로만 접속하므로 앱이 이를 실행할 수 없고(읽기 전용 트랜잭션에서 거부), 수동으로도
실행하지 않는다.

## (선택) 메타데이터를 전용 로컬 DB로 완전 분리

기본값은 메타데이터/운영 데이터가 로컬 `POSTGRES_*` DB에 함께 있다. 메타데이터만 별도 DB로
떼어내려면 `.env` 에 `METADATA_DB_*` 를 지정하고 `docs/data/metadata_ddl.sql` 로 스키마를
만든다(5개 자기완결 테이블: 오디언스/멤버/실패로그/프롬프트/정책).

> 주의: A/B 실험·발송·이벤트 테이블은 campaigns/users 에 FK로 묶여 있어 이 분리 대상이 아니며,
> 로컬 full-schema DB(`POSTGRES_*`)에 남는다. 따라서 `METADATA_DB_*` 를 별도 DB로 분리하면
> 오디언스 기반 A/B 배정(`audience_id` 로 멤버 조회)은 두 DB에 걸쳐 동작하지 않는다. 이 기능이
> 필요하면 메타데이터를 분리하지 말고 기본값(단일 로컬 DB)을 사용한다.
