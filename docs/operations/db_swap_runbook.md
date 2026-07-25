# DB 스왑 런북 — 타겟팅 대상 실DB를 다른 DB로 바꿀 때

> 타겟팅 대상 business DB(현재 CRMDW `smart_quadmax_mart` 등)를 **완전히 다른 DB**로 교체할 때
> 차례대로 하는 작업. 목표는 이식성 리팩터(A/B/C/D, [db_portability_audit.md](db_portability_audit.md))
> 덕분에 **소스 무수정 — 설정 재매핑 + 카탈로그 재생성**으로 끝내는 것이다.
>
> 원리: 스키마 지식(테이블/컬럼/코드값/방언)은 `member_target_filters.json` 레지스트리 +
> `schema_catalog.json` 카탈로그가 소유하고, 소스(graph_rag/api)와 프롬프트는 그걸 렌더할 뿐이다.
> 그러니 **레지스트리와 카탈로그만 새 DB에 맞추면** 나머지가 따라온다.

---

## 0. 한눈에 보는 순서

```
① 접속정보 교체(.env / .mcp.json)
② 카탈로그 테이블 집합 · database 필드를 새 DB 기준으로 손질
③ 카탈로그 구조 재생성    → schema_extract.py --refresh-external
④ 레지스트리 재매핑        → member_target_filters.json (+ member_metrics.json, sql_examples)
⑤ 프리플라이트 게이트      → db_swap_preflight.py --check-db   (통과할 때까지 ④↔⑤ 반복)
⑥ 파생 카탈로그 재생성     → build_* 체인 + init_rag_collections.py --recreate
⑦ 재기동 · 스모크          → docker compose restart + 대표 질의
⑧ 프론트                   → 원칙적으로 무수정 (§7 참고)
```

각 단계 상세는 아래.

---

## 1. 접속정보 교체

- 백엔드 `.env`: `BUSINESS_DB_*`(로컬 postgres 경로) 또는 원격이면 `MSSQL_*`/`QUADMAX_DB_*`/
  `CRMDW_DB_NAME` 등 새 DB 자격증명으로 교체. ([multi_db_access.md](multi_db_access.md),
  [real_db_connection.md](real_db_connection.md))
- `.mcp.json`: 커넥션의 `SQLSERVER_DATABASE`/`MYSQL_DB` 등을 새 DB로. MCP 재연결(`/mcp reconnect`).
- **새 커넥션 종류가 생기면**(예: 새 엔진/새 커넥션명) 두 곳에 방언을 등록:
  - `sql_dialect.py`의 `CONNECTION_DIALECTS`(커넥션→`tsql`/`mysql`/`postgres`) — 단일 소스.
    `db_connections._DB_DIALECTS`는 여기서 파생되므로 별도 수정 불필요.
  - `db_connections.py`의 커넥션 팩토리(`crman_config` 류)와 `run_read_query`의 분기.

## 2. 카탈로그 테이블 집합 · `database` 필드 손질

`schema_catalog.json`은 **승인 테이블만 큐레이션된** 파일이다(전체 덤프 아님). 새 DB에서 쓸
테이블만 남기고, 각 테이블의 `database` 값을 그 테이블이 사는 **커넥션명**으로 맞춘다.
`③ --refresh-external`이 이 `database` 값을 보고 어느 실DB에 붙을지 정한다.

- 새 테이블을 추가할 거면 최소 골격만 만들어 둔다: `{"database": "<커넥션>", "columns": []}`.
  구조는 ③이 채운다. 사람이 쓴 `description_llm`/`join_hints`/`human_note`는 ③에서 보존된다.
- 최상위 `databases` 설명 문자열도 새 서버/DB에 맞게 갱신(방언 판별 `load_table_dialects`가 이걸 읽음).

## 3. 카탈로그 구조 재생성 (실DB 인트로스펙션)

승인 테이블들의 **컬럼/타입/PK/객체유형**을 실DB `INFORMATION_SCHEMA`에서 읽어 채운다
(MSSQL/MariaDB 공통). 사람이 쓴 지식은 컬럼명 기준으로 보존되고, 신규·삭제 컬럼은 요약 보고된다.

```bash
# 먼저 미리보기(쓰지 않음): 무엇이 갱신되고 무엇이 실DB에 없는지 확인
docker compose exec -w /app python python schema_extract.py --refresh-external --dry-run

# 이상 없으면 반영
docker compose exec -w /app python python schema_extract.py --refresh-external
#   특정 커넥션/테이블만:  --connection CRMDW   --tables CRM_MB_BASEINFO,CRM_SL_ORDERHEADERMALL
```

- `missing_in_db`에 뜬 테이블은 카탈로그엔 있는데 실DB엔 없다는 뜻 → ②로 돌아가 테이블 집합을 고친다.
- `changed_tables`의 added/removed 컬럼을 확인하고, 없어진 컬럼을 레지스트리(④)가 참조하면 고쳐야 한다.
- 로컬 postgres 데모 테이블(users/campaigns 등, `database` 없음)은 이 모드가 건드리지 않는다 —
  그쪽은 `schema_extract.py --from-db`(PostgreSQL 인트로스펙션) 소관.

## 4. 레지스트리 재매핑 (수작업 — 핵심)

이 단계가 실질 작업량의 대부분이다. 새 스키마의 테이블/컬럼/코드값/조인에 맞춰 아래를 고친다.

**[member_target_filters.json](../data/member_target_filters.json)** (규칙 엔진 전체):
- `base_entity`: 회원 기준 테이블/별칭/회원키/로그인ID키/날짜포맷. **`dialect`를 여기 명시**하면
  (`"tsql"`/`"mysql"`/`"postgres"`) 결정론 빌더가 그 방언으로 렌더한다(미지정 시 카탈로그 방언→tsql).
- `eq_filters`/`numeric_filters`/`activity_filters`: 컬럼(`B.<COL>`)과 코드 저장값(예: 성별/등급/상태의
  도메인 접두어 값). **성별/등급의 `synonyms[0]`**은 결과 화면 라벨로도 쓰이니 사람이 읽는 한글로.
- `active_state`/`birthday_target`/`signup_target`/`recent_login_target`/`region_target`: 컬럼명.
- 팩트 조인 섹션: `order_count_targets`/`aggregate_targets`/`purchase_product_target`/`cart_targets`/
  `campaign_response_targets`/`cell_rate_targets` — 테이블·조인키·집계 컬럼. 회원키 타입이 다르면
  `member_join.left`에 캐스트 조인(빌더 기본은 방언 어댑터의 `cast_bigint`).
- `validation.allowed_table_aliases`: 새 별칭을 쓰면 추가.

**[member_metrics.json](../data/member_metrics.json)**: `value_table`/`join_column`/`grain_filter`와
각 지표 `column`을 새 월/스냅샷 테이블에 맞춘다.

**[sql_examples.sample.sql](../data/sql_examples.sample.sql)**: RAG 예시 SQL을 새 테이블/방언으로.

> 코드에 스키마를 다시 박지 말 것. 소스는 위 레지스트리를 렌더하도록 이미 배선돼 있다
> (`_member_table`/`_member_from_clause`/`_cart_from_join_lines` 등). 새 설정 섹션은 JSON에만
> 추가하면 로더가 전부 싣는다(죽은설정 함정 수정됨).

## 5. 프리플라이트 게이트 (④↔⑤ 반복)

레지스트리가 참조하는 테이블/컬럼이 카탈로그(그리고 실DB)에 실재하는지 배포 전에 검증한다.
**이게 DB 스왑 1순위 실패모드(참조 불일치가 조용히 통과 → 런타임 SQL 깨짐/0명)를 막는 게이트다.**

```bash
# 정적: 레지스트리 ↔ schema_catalog (접속 불필요)
docker compose exec -w /app python python db_swap_preflight.py

# + 실DB 대조: 카탈로그 테이블이 각 커넥션 실DB에 실재하는지까지 (접속 필요)
docker compose exec -w /app python python db_swap_preflight.py --check-db
```

- `PASS ✅`가 나올 때까지 ④(레지스트리)와 ②(카탈로그 테이블 집합)를 고친다.
- `❌ 참조 테이블 …`, `❌ 컬럼 …이 카탈로그 테이블에 없음`이 정확히 어디를 고쳐야 하는지 알려준다.
- 종료코드로 CI/스크립트 게이트에 걸 수 있다(0=통과).

## 6. 파생 카탈로그 재생성 + 재색인

레지스트리·카탈로그가 확정되면 파생물(디멘션/값 인덱스/관계/RAG 지식)을 다시 만든다.
빌더 내부의 커넥션/테이블 하드코딩(§ 아래 주의)도 새 DB에 맞는지 확인한다.

```bash
# 디멘션 정의 스냅샷 (quadmax_sdz 접속)
docker compose exec -w /app python python build_dimension_catalog.py -o docs/data/dimension_catalog.sample.json
# 회원 값 인덱스 (CRMDW 접속) — 저카디널리티 컬럼 실값 스냅샷
docker compose exec -w /app python python build_member_value_index.py
# 테이블 관계도 + FK 주입 (DB 미접속)
docker compose exec -w /app python python build_table_relationships.py
# RAG 지식 베이스 병합 + Qdrant 재색인 (build_rag_knowledge 포함)
docker compose exec -w /app python python init_rag_collections.py --recreate
```

> **빌더 내부 하드코딩(재생성해도 자동 반영 안 됨 — 스크립트를 고쳐야 함):**
> - `build_member_value_index.py`: `TABLE = "CRM_MB_BASEINFO"`, `CONNECTION = "CRMDW"`,
>   `AUX_ATTRIBUTE_TABLES` — 회원 테이블/커넥션이 바뀌면 여기를 고친다.
> - `build_dimension_catalog.py`: `DBMS_CONNECTION_MAP`, `TARGETABLE_OVERRIDES`, `t_xlig_*` 원천.
> - `build_table_relationships.py`: `RELATIONSHIPS`(실DB에 FK 선언이 없어 관계는 수동 큐레이션).
> - `schema_extract.py`: `DEFAULT_OBJECT_DESCRIPTIONS`/`IMPORTANT_COLUMN_NAMES`(로컬 데모 스키마용).

## 7. 재기동 · 스모크 테스트

```bash
docker compose restart api        # 코드 변경 없으니 restart 로 충분(볼륨 마운트). 안 되면:
# docker compose up -d --force-recreate --no-deps api

# 회귀 스위트(방언/이식성 포함)
docker compose exec -w /app -e PYTHONPATH=/app python python -m pytest tests/ -q
```

대표 질의 몇 개를 UI/`/target-sql`로 실행해 SQL이 새 테이블/방언으로 나오고 카운트가 채워지는지 확인:
성별+연령, 등급 IN, 최근 로그인 창, 장바구니 이탈, 캠페인 반응, 누적 구매금액 임계값 등
([condition-builder-coverage] 커버리지 표의 조건들). 실패하면 `failure_stage`가 어느 단계에서
막혔는지 알려준다.

## 8. 프론트엔드 (sibling repo)

경로: `C:\git\recommendation-campaign-system-frontend` (Next.js app 라우터, BFF 역할 포함).

**원칙: DB 스왑 시 프론트는 무수정.** 감사(2026-07-25) 결과 프론트에는 실DB 테이블명·컬럼명·
코드값 도메인 접두어(`GENDER_CD.FEMALE` 류)·커넥션명이 **하나도** 하드코딩돼 있지 않다. 프론트는
백엔드의 **DB-추상화된 JSON 응답 계약**과 **소문자 토큰 어휘**(female/seoul/cart_abandoned 등)에만
결합돼 있어, DB만 바뀌고 백엔드 API 계약이 유지되면 손댈 게 없다.

굳이 "백엔드 계약/어휘가 바뀔 때만" 볼 후보(모두 방어적 폴백이라 틀려도 라벨만 원문 노출, 앱은 안 깨짐):
- `app/api/targeting/route.ts` `normalizeValue`(토큰→한글 라벨) / `groupTitles`(segment_composition 그룹키→제목)
- `app/api/targeting/route.ts` SQL 별칭 정규식(백엔드 SQL 표시용 값 추출)
- `lib/targeting-hints.ts` 백엔드 설정 파일명 안내 텍스트(`member_target_filters.json` 등) — DB가 아니라
  설정층을 가리키므로, 오히려 사용자를 올바른 수정 지점(④)으로 안내하는 문구.

이들은 **새 세그먼트 토큰/컬포지션 키가 생기거나 백엔드가 설정 파일을 개명할 때**만 손보면 된다.

---

## 부록: 무엇이 어디에 사는가 (빠른 참조)

| 바뀌는 것 | 사는 곳 | 스왑 시 |
|---|---|---|
| 회원 테이블/키/방언 | `member_target_filters.json` `base_entity` | ④ 손수정 |
| 컬럼·코드값·조인 | `member_target_filters.json` 각 섹션 | ④ 손수정 |
| 지표(매출/횟수) 컬럼 | `member_metrics.json` | ④ 손수정 |
| 테이블 구조(컬럼/타입/PK) | `schema_catalog.json` | ③ 자동(refresh) |
| 커넥션→방언 | `sql_dialect.CONNECTION_DIALECTS` | ① (새 엔진일 때만) |
| SQL 문법(TOP/LIMIT/날짜함수) | `sql_dialect.py` 어댑터 | 수정 불필요(방언 선택만) |
| 값 인덱스/디멘션/관계/RAG | 빌더 산출물 | ⑥ 재생성(+빌더 내부 하드코딩 주의) |
| 프론트 표시 | frontend repo | ⑧ 원칙 무수정 |

관련 문서: [db_portability_audit.md](db_portability_audit.md)(결합 감사·리팩터 상세),
[multi_db_access.md](multi_db_access.md), [real_db_connection.md](real_db_connection.md),
[schema_dictionary_workflow.md](../guides/schema_dictionary_workflow.md).
