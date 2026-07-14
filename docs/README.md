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
