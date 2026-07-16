# 문서 구조

`docs`는 실행 자산과 설명 문서를 역할별로 나눈다. 코드가 직접 읽는 경로는 실행 중 깨지지 않도록 `data`, `prompts`, `policies`에 둔다.

| 폴더          | 역할                                               | 주요 파일                                                                                                                                 |
| ------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `overview/`   | 프로젝트 전체 구조와 개발 보고서                   | `structure.md`                                                                                                                            |
| `operations/` | 구축, 실행, 인제션 runbook                         | `project_process.md`, `ingest_normalization.md`, `ingest_campaign_user_rag.md`                                                            |
| `guides/`     | 기능별 설계/운영 가이드                            | `channel.md`, `ctr_analysis_process.md`, `ai_ctr_analysis_integration_guide.md`, `schema_dictionary_workflow.md`, `prompt_engineering.md` |
| `data/`       | DDL, 샘플 데이터, 스키마 카탈로그, RAG 지식 베이스 | `ddl.sql`, `schema_catalog.json`, `rag_knowledge_base.json`                                                                               |
| `prompts/`    | GraphRAG/메시지 생성 프롬프트 템플릿               | `query_plan_*.txt`, `answer_*.txt`, `message_generation_*.txt`, `message_generation_tone_manner.txt`                                      |
| `policies/`   | 런타임 정책 JSON                                   | `message-policy.json`                                                                                                                     |

경로 변경 시 주의할 기본값:

- `graph_rag.py`는 기본 RAG 데이터로 `docs/data/rag_knowledge_base.json`을 읽는다.
- `graph_rag.py`는 기본 프롬프트 디렉터리로 `docs/prompts`를 읽는다.
- 메시지 채널 정책 기본값은 `docs/policies/message-policy.json`이다. 다른 파일을 쓰려면 `GRAPH_RAG_MESSAGE_POLICY` 환경 변수나 `--message-policy` 옵션을 사용한다.

## 지식베이스 반영법

스키마의 **진짜 소스는 라이브 PostgreSQL DB**다. 아래 체인을 따라 catalog → 지식 베이스 → Qdrant 순으로 재생성한다.

```
PostgreSQL(campaign_db)
  └─(schema_extract.py --from-db)→ docs/data/schema_catalog.json
       └─(build_rag_knowledge.py)→ docs/data/rag_knowledge_base.json  ┐
                                                                       ├─(init_rag_collections.py)→ Qdrant
       docs/data/campaign_user_rag_sample_50_with_edges.json ─────────┘   · campaign_knowledge_rag (지식 노드 + 캠페인/사용자)
                                                                          · campaign_user_rag_nodes (캠페인/사용자 벡터)
```

### 스키마가 바뀐 뒤 (기본 흐름)

```bash
# 1) 라이브 DB에서 schema_catalog.json 갱신 (손으로 쓴 description_llm/human_note는 자동 보존)
docker compose exec python python schema_extract.py --from-db

# 2) rag_knowledge_base.json 재생성 + 두 Qdrant 컬렉션 재색인
docker compose exec python python init_rag_collections.py --recreate
```

- `schema_extract.py`는 `--from-db`(라이브 DB, 권장)와 DDL 파일 파싱(`schema_extract.py docs/data/ddl.sql`) 두 모드를 지원한다. 접속은 `POSTGRES_*` 환경 변수를 쓰며 `--conninfo`로 재정의한다.
- `init_rag_collections.py`는 catalog·정규화 사전·업무 정책·계산 지표·SQL 예시와 `campaign_user_rag_sample_50_with_edges.json`을 합쳐 지식 베이스를 다시 만든 뒤 `campaign_knowledge_rag`, `campaign_user_rag_nodes`를 색인한다.

### 부분 작업

```bash
# 지식 베이스 JSON만 재생성 (색인 없이)
docker compose exec python python build_rag_knowledge.py

# 캠페인/사용자 샘플만 campaign_user_rag_nodes에 색인
docker compose exec python python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --recreate

# 반영 없이 입력만 검증
docker compose exec python python schema_extract.py --from-db --output /tmp/catalog.check.json
docker compose exec python python init_rag_collections.py --validate-only
```

> 새 테이블/뷰가 잡히면 `schema_extract.py`의 `DEFAULT_OBJECT_DESCRIPTIONS`에 한 줄 설명을 추가한다. 한 번 넣으면 이후 `--from-db` 재생성에도 보존된다.
