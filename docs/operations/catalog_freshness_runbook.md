# 카탈로그 신선도 런북 — 무엇을 바꾸면 무엇을 다시 만들어야 하는가 (2026-08-02)

"코드를 고쳤으면 컨테이너 재시작으로 충분하다"는 규칙은 **코드에만** 적용된다. 카탈로그를
재생성하면 그 사본을 들고 있는 소비자들이 스테일해지고, 그 스테일은 **오류가 아니라 오답**으로
나타난다 — RAG 가 없어진 컬럼을 추천하고, 검증기가 새 테이블을 모르며, SQL 은 성공하는데 0명이다.

이 문서는 그 전파 경로를 적는다.

## 자산 계층

| 계층 | 파일 | 누가 만드나 | 누가 읽나 |
|---|---|---|---|
| **원천 스키마** | `docs/data/generated/schema_catalog.json` | `schema_extract.py` (실DB) | sql_guard 허용목록·타입군, preflight, 물리바인딩 스캐너, confidence, RAG 적재 |
| 관계(FK) | 같은 파일의 `tables[*].foreign_keys` | `build_table_relationships.py` | RAG 스키마 그래프, join_paths 후보 |
| 디멘션 | `docs/data/generated/dimension_catalog.sample.json` | `build_dimension_catalog.py` | compiler_strategies(값→코드 변환), rag/trace, RAG 적재 |
| 회원 값 인덱스 | `docs/data/generated/member_value_index.json` | `build_member_value_index.py` | 값 정규화, RAG 적재 |
| **RAG 지식베이스** | `docs/data/generated/rag_knowledge_base.json` | `build_rag_knowledge.py` | Qdrant 적재(`init_rag_collections.py`) |
| 실행 바인딩(legacy) | `docs/data/runtime/sql/member_target_filters.json`, `member_metrics.json` | 사람 | 20개 SQL 빌더, confidence, member_policy |
| 실행 바인딩(canonical) | `docs/data/runtime/semantics/audience_catalog.json` | 사람 | Event IR lowering·event_compiler |
| 속성 시점/이력 | `docs/data/runtime/semantics/attribute_catalog.json` | 사람 | compositional_targeting |

## 전파 규칙

### 1. 코드만 고쳤다 → 재시작으로 충분

```bash
docker restart recommendation-campaign-system-python-api-1
```

볼륨 마운트라 코드는 즉시 반영된다. **RAG 재적재는 불필요하다.**

### 2. 실행 바인딩 JSON(`docs/data/runtime/**`)을 고쳤다 → 재시작 + 프리플라이트

```bash
python db_swap_preflight.py      # 네 레지스트리 ↔ schema_catalog 대조
python -m pytest -q              # 드리프트 가드(미러·소유권·순서·BFF 계약)
docker restart recommendation-campaign-system-python-api-1
```

RAG 재적재는 **불필요하다** — 이 파일들은 RAG 에 적재되지 않는다.

### 3. `schema_catalog.json` 을 재생성했다(= DB 를 갈아끼웠다) → 전체 사슬

순서가 중요하다. 아래 순서를 지키지 않으면 하위 산출물이 옛 스키마를 물고 간다.

```bash
# (1) 원천 재추출
python schema_extract.py                    # 실DB → schema_catalog.json
python build_table_relationships.py         # FK 큐레이션 주입

# (2) 파생 카탈로그
python build_dimension_catalog.py
python build_member_value_index.py

# (3) RAG 지식베이스 재생성 + 재적재  ← 이 단계를 빠뜨리는 것이 가장 흔한 사고다
python build_rag_knowledge.py
python init_rag_collections.py

# (4) 게이트
python db_swap_preflight.py --check-db      # 카탈로그 ↔ 실DB 까지 대조
python -m pytest -q
python tools/physical_binding_inventory.py  # 소스 하드코딩이 늘지 않았는지

# (5) 라이브 회귀
python tools/live_prompt_baseline.py --repeat 2
```

**왜 (3) 이 필수인가**: `build_rag_knowledge.py` 는 schema_catalog·dimension_catalog·
member_value_index 를 **복사해** 지식 노드를 만든다. 원천만 재생성하고 여기서 멈추면 Qdrant 에는
옛 테이블·옛 컬럼이 남아, 검색이 존재하지 않는 스키마를 근거로 제시한다. 이때 나오는 SQL 은
문법이 맞고 검증도 통과하지만 결과가 0건이거나 틀린다.

### 4. 실행 바인딩에 **새 섹션/새 사건**을 추가했다 → 게이트 범위도 함께 넓혀라

`db_swap_preflight.REGISTRY_PATHS` 에 새 카탈로그를 **만드는 커밋에서 같이** 추가한다.
그러지 않으면 그 파일의 스키마 드리프트는 배포 전에 아무도 보지 못한다
(`tests/test_db_swap_preflight_gate.py::test_preflight_covers_the_canonical_execution_catalogs`
가 이 규칙을 강제한다).

## 스테일을 의심할 때 — 무엇부터 보나

| 증상 | 먼저 볼 것 |
|---|---|
| SQL 은 성공하는데 0명 | `db_swap_preflight.py` → 물리 바인딩이 실DB 와 어긋났는지 |
| 검색이 없는 컬럼을 추천 | RAG 재적재를 빠뜨렸는지((3) 단계) |
| "레거시는 되는데 canonical 만 실패" | `audience_catalog.json` 이 preflight 범위 안인지 |
| 특정 프롬프트만 갑자기 미지원 | `python tools/live_prompt_baseline.py --only <id> --repeat 3` (방출 편차 확인) |
| 원인 불명 | 로컬 postgres `campaign_query_failure_logs`(stage_log·query_plan·decisions) |

## 검사 범위의 한계 (정직하게)

- `db_swap_preflight.py` 는 기본적으로 **정적 검사**다. `--check-db` 없이는 카탈로그와 설정만
  대조하며, 실DB 에 그 테이블이 실제로 있는지는 보지 않는다.
- 물리 바인딩 스캐너는 **카탈로그에 실재하는 이름과 정확히 일치하는 대문자 리터럴**만 센다.
  별칭 접두(`C.KEEP_YN`)나 표현식 내장(`SUM(QTY)`)은 세지 않으므로, 실제 부채는 보고된 수치보다
  많다. 수치를 "0 에 도달했다"로 읽지 마라.
- 라이브 회귀는 LLM 방출 편차가 있다. 한 번의 실행으로 회귀를 단정하지 말고 `--repeat` 를 써라.
