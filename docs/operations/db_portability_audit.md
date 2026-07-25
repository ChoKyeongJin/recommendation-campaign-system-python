# DB 이식성 감사 (2026-07-25)

> 전제: 타겟팅 대상 business DB 는 앞으로 **계속 완전히 다른 DB로 바뀔 수 있다**
> (현재 CRMDW `smart_quadmax_mart` → CRMAN `Customer_Analytics` 이행 중이며 그게 끝이 아님).
> 목표 상태: **DB 스왑 = `.env` 교체 + 설정/카탈로그 재생성만으로 끝, 소스 무수정.**
>
> 이 문서는 그 목표 대비 현재 결합도를 전수 감사한 결과와 리팩터 로드맵이다.

## 0. 한 줄 결론

이 코드베이스는 이미 "스키마를 소스 밖으로 빼는" 이전을 절반쯤 해둔 상태다.
`sql_guard.py` / `targeting_ir.py` / `sql_ast.py` 는 목표형(스키마 리터럴 0개, 전부
카탈로그에서 로드)이고, 결합의 진앙은 `graph_rag.py`(300+ 사이트)다.

## 1. 계층 구분: DB 스왑 시 무엇을 손대나

| 계층 | 자산 | DB 스왑 시 |
|------|------|-----------|
| ① 자동생성 카탈로그 | `schema_catalog.json`, `dimension_catalog.sample.json`, `member_value_index.json`, `table_relationships.md`, `rag_knowledge_base.json` | `build_*.py` 재실행 (단 §2 캐비앗) |
| ② 수작업 설정(단일 진실 소스) | `member_target_filters.json`(★최대, ~1040줄), `member_metrics.json`, `sql_examples.sample.sql`, `business_policies`/`metric_lexicon`(아직 데모 스키마 기준) | 사람이 재매핑 |
| ③ 소스에 샌 결합 | `graph_rag.py`, `api.py`, `confidence.py`, `formula_engine.py` 일부 | **리팩터로 소거해야 할 대상 (§4)** |

`metadata_ddl.sql` 은 앱 소유 로컬 메타 DB라 스왑과 무관.
`targeting_lexicon.json` / `normalization_rules.sample.json` 은 대부분 언어 사전이라 유지 가능(코드값 항목만 손질).

## 2. 계층① 재생성 시퀀스와 캐비앗

의존성: `schema_extract` → `build_table_relationships`(FK 주입) → `build_rag_knowledge`(전체 병합).
`build_dimension_catalog`(quadmax_sdz 접속) / `build_member_value_index`(CRMDW 접속)는 병합 전이면 순서 무관.

```bash
python schema_extract.py --from-db            # → schema_catalog.json  (⚠ 아래 캐비앗)
python build_dimension_catalog.py -o docs/data/dimension_catalog.sample.json
python build_member_value_index.py            # → member_value_index.json
python build_table_relationships.py           # FK 주입 + table_relationships.md
python build_rag_knowledge.py                 # → rag_knowledge_base.json
python init_rag_collections.py --recreate     # Qdrant 재색인
```

**캐비앗 (재생성이 "그냥" 안 되는 이유):**

- `schema_extract.py --from-db` 는 **PostgreSQL 인트로스펙션 전용**(psycopg + pg_catalog).
  실DB는 SQL Server/MariaDB라 직접 못 읽는다. 현재 `schema_catalog.json` 의 실DB 테이블·
  `database`/`join_hints` 필드는 스크립트 순수 출력이 아니라 별도 추출+수작업 큐레이션 결과다.
- 빌더 내부에도 수작업 하드코딩이 있다(재실행해도 자동 갱신 안 됨):
  - `build_table_relationships.py` `RELATIONSHIPS`(실DB에 FK 선언이 없어 전부 수동 큐레이션)
  - `build_dimension_catalog.py` `TARGETABLE_OVERRIDES` / `DBMS_CONNECTION_MAP`
  - `build_member_value_index.py` `AUX_ATTRIBUTE_TABLES`, `TABLE`, `CONNECTION="CRMDW"`
  - `schema_extract.py` `DEFAULT_OBJECT_DESCRIPTIONS` / `IMPORTANT_COLUMN_NAMES`(데모 스키마)
  - `build_rag_knowledge.py` `DEFAULT_BUSINESS_TERMS`(데모 스키마 용어)

## 3. 파일별 결합도 실측 (요약)

| 파일 | 테이블 리터럴 | 컬럼 리터럴 | 방언 함수 | 판정 |
|---|---|---|---|---|
| `graph_rag.py` | ~110 | ~172 | ~39 (GETDATE/CONVERT/TRY_CAST/CONCAT) | **진앙. §4 전 항목 해당** |
| `api.py` | ~7 | ~10 | WITH(NOLOCK) 등 | 외부 세그먼트 집계 경로에 국소 집중 |
| `confidence.py` | ~12 (폴백) | ~15 (폴백) | 0 | 전부 `cfg.get(key, "리터럴")` 폴백 미러 |
| `db_connections.py` | 0 | 0 | `_DB_DIALECTS` 맵 1 | 인프라 로직(드라이버 분기)은 정당 |
| `sql_guard.py` | 0 | 0 | 방언 추상화 자체 | ✅ 모범 — 목표 형태 |
| `targeting_ir.py` | 0 | 0 | 0 | ✅ 모범 — 순수 IR |
| `sql_ast.py` | 0 | 0 | 0 | ✅ 모범 |
| `formula_engine.py` | 2 (데모) | 0 | 0 | `TABLE_ALIASES` 데모 고정 |
| `schema_extract.py` | ~19 (데모) | ~40 (데모) | 0 | 빌더 성격 — §2 캐비앗 |
| `reference_docs.py` | 0 | 0 | 0 | UI 메타데이터만 |

프롬프트: `docs/prompts/*` 파일 프롬프트는 **이미 스키마-중립**(canonical/IR 추상화 + 런타임 주입).
결합은 `graph_rag.py` 안 인라인 프롬프트 **딱 2곳**에 집중 — §4-C.

## 4. 소스 결합 4대 경로와 리팩터 로드맵

### A. 방언 인라인 (~39곳) — 최우선

`CONVERT(CHAR(8), DATEADD(DAY,-N,GETDATE()),112)`, `TRY_CAST(x AS BIGINT)`, `CONCAT(...)`,
`ISNULL(x,0)` 같은 T-SQL 이 날짜창·조인키 캐스트 **로직에 직접** 박혀 있다
(예: `graph_rag.py` 술어 빌더들, `api.py` WITH(NOLOCK)). MariaDB/PostgreSQL 로 가면 전부 깨진다.

→ **방언 어댑터** 도입: `dialect.date_cutoff(days)`, `dialect.date_today()`,
`dialect.cast_bigint(col)`, `dialect.concat(...)`, `dialect.coalesce0(col)` 등.
방언 결정은 sql_guard 가 이미 아는 per-table/per-connection dialect 에서.

### B. 단일 진실 소스화 (폴백 미러 + 하드코딩 FROM/JOIN)

- `graph_rag.py:129-282` `_DEFAULT_MEMBER_TARGET_FILTERS` 가 `member_target_filters.json` 을
  통째로 복제(폴백). `confidence.py` 전역의 `cfg.get(k, "리터럴")` 이중 보관,
  `api.py` `_EXTERNAL_MEMBER_SCHEMA` + gender/grade 코드→라벨 맵도 동일 패턴.
  → 스키마가 JSON+소스 두 곳에 존재해 **drift 위험**. 설정을 진짜 단일 소스로 삼고
  폴백 리터럴 제거(설정 없으면 명시적 실패).
- 일부 빌더는 설정을 아예 경유하지 않는다:
  `FROM ODS_MALL_OMS_CART A INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID=B.MEMBER_ID ...`
  (`graph_rag.py:8847-8880` 등), `B.MEMBER_NO AS CUST_ID` 관례, `if table_name != "CRM_MB_BASEINFO"` 분기.
  → 조인키/회원키를 join_key_registry(schema_catalog)·레지스트리에서 파생.

### C. 인라인 LLM 프롬프트 2곳

- `graph_rag.py:7569-7591` LLM SQL 폴백 생성기 system prompt — 테이블/회원키/상태코드/
  도메인 접두어 규약/MSSQL 방언이 텍스트로 박힘 (테이블 요약·허용목록은 이미 동적 주입 중).
- `graph_rag.py:7651-7687` SQL 의미검증 게이트 system prompt — "원문 개념→SQL 표현" 사전을
  통째로 하드코딩(카트/캠페인 테이블, KEEP_YN, LAST_LOGIN_DATE, EMART_GRADE_CD …). 주입 전무.

→ 두 프롬프트 모두 `member_target_filters.json` 레지스트리에서 **렌더링**하도록 전환.
(스키마-중립 검증 규칙 문구는 유지.)

### D. 계층① 빌더의 실DB 인트로스펙션

`schema_extract.py` 에 MSSQL/MariaDB 인트로스펙션 경로 추가(또는 별도 빌더) —
이것이 되어야 "재생성만으로 끝"이 성립.

## 5. 우선순위와 진행 상태 (2026-07-25 갱신)

1. **A. 방언 어댑터** — ✅ 완료. `sql_dialect.py` 신설(TSql/MySql/Postgres/ANSI).
   - graph_rag 의 날짜창·캐스트·CONCAT 인라인 전부 어댑터 경유(`_member_dialect()` —
     base_entity.dialect → 카탈로그 테이블 방언 → tsql 순 판별, tsql 기본이라 기존 출력 불변).
   - api.py WITH(NOLOCK) → `dialect_for_connection().nolock_hint()`.
   - 회귀: `tests/test_sql_dialect.py`(tsql 렌더 = 리팩터 전 문자열, 방언 스왑 시나리오 포함).
2. **B. 단일 진실 소스화** — ✅ 핵심 완료(빌더의 설정 미경유 하드코딩 소거).
   - base_entity 접근자(`_member_table/_member_alias/_member_key_column/_member_from_clause/
     _member_key_select/_member_grade_column`) 도입, `FROM CRM_MB_BASEINFO B`·`B.MEMBER_NO AS
     CUST_ID`·`B.EMART_GRADE_CD` 등 ~35곳 파생 전환.
   - 카트/주문상세 조인: `_cart_from_join_lines`/`_cart_member_join_on`/`_order_detail_member_join_lines`
     — 조인키·테이블은 cart_targets/purchase_product_target 레지스트리 소유.
   - api.py `_EXTERNAL_MEMBER_SCHEMA` 를 레지스트리+schema_catalog(테이블→커넥션)에서 빌드,
     성별/등급 코드→라벨·서열 맵을 eq_filters(synonyms/rank)에서 파생.
   - 잔여(후속): `_DEFAULT_MEMBER_TARGET_FILTERS` 폴백 미러 축소, confidence.py 폴백 정리,
     `db_connections._DB_DIALECTS` ↔ `sql_dialect.CONNECTION_DIALECTS` 통합.
3. **C. 프롬프트 렌더링** — ✅ 완료. 인라인 프롬프트 2곳(SQL 폴백 생성기·의미검증 게이트)의
   스키마 사실(회원키/상태 술어/코드값 예시/카트·캠페인 테이블/날짜 포맷/방언 함수)을
   레지스트리·어댑터에서 렌더. 검증 원칙 문구(스키마 무관)는 리터럴 유지.
4. **D. 빌더 실DB 지원** — ⬜ 미착수. `schema_extract.py` 에 MSSQL/MariaDB 인트로스펙션 경로
   추가해야 "재생성만으로 끝"이 성립(§2 캐비앗).

모범 참조: `sql_guard.py`(카탈로그 로드 방식), `targeting_ir.py`(논리 개념만), `sql_ast.py`.

### DB 스왑 절차(현재 기준)

1. `.env` 접속정보 교체 + `.mcp.json`/schema_catalog `databases`·`database` 필드 갱신
2. `member_target_filters.json` 재매핑(테이블/컬럼/코드값/조인 — base_entity.dialect 로 방언 명시 가능)
   + `member_metrics.json`, `sql_examples.sample.sql`
3. §2 재생성 시퀀스 실행(카탈로그 5종) — 단 §2 캐비앗의 빌더 내부 하드코딩 손질 포함
4. 소스는 원칙적으로 무수정 — A/B/C 이후 남은 결합은 §5-2 '잔여' 항목뿐
