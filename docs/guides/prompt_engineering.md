# 프롬프트 엔지니어링 반영 지점

이 프로젝트에서 프롬프트 엔지니어링이 필요한 부분은 LLM이 직접 SQL을 자유 생성하는 단계가 아니라, 자연어 질문을 검증 가능한 Query Plan으로 바꾸고 검증된 SQL 결과만 답변에 쓰도록 제한하는 단계다.

## 프롬프트 저장소 (DB 우선)

프롬프트는 이제 DB(`campaign_prompt_templates` 테이블)에서 관리한다. `graph_rag.py`의 `_read_prompt_template`은 다음 우선순위로 프롬프트를 조회한다.

1. **DB** — `prompt_store` 모듈이 `campaign_prompt_templates`를 조회한다(프로세스 인메모리 캐시로 서빙, `name`은 프롬프트 파일명과 동일).
2. **파일** — DB에 없거나 DB 연결이 실패하면 `docs/prompts` 아래 파일을 읽는다.
3. **하드코딩 fallback** — 위 둘 다 없으면 `graph_rag.py` 내부의 fallback 문자열을 쓴다.

이 fallback 체인 덕분에 DB가 비어 있거나 일시적으로 접속이 안 돼도 서비스는 파일/코드 기본값으로 계속 동작한다.

### 초기 시딩과 관리

- 기존 `docs/prompts/*.txt`를 DB로 넣으려면 시딩 스크립트를 한 번 실행한다.

  ```bash
  docker compose exec api python seed_prompts.py
  # 또는 API로: curl -X POST http://localhost:8000/prompts/seed
  ```

- 이후 프롬프트는 관리 REST API로 조회/수정/삭제한다. 수정·삭제·시딩·리로드는 실행 중인 API 프로세스의 캐시를 즉시 갱신한다.

  | 메서드/경로               | 설명                                          |
  | ------------------------- | --------------------------------------------- |
  | `GET /prompts`            | 전체 프롬프트 목록                            |
  | `GET /prompts/{name}`     | 단일 프롬프트 조회 (name = 파일명)            |
  | `PUT /prompts/{name}`     | 프롬프트 추가/수정 (`content`, `description`) |
  | `DELETE /prompts/{name}`  | 프롬프트 삭제                                 |
  | `POST /prompts/reload`    | 캐시를 DB에서 다시 로딩                        |
  | `POST /prompts/seed`      | `docs/prompts` 파일을 DB로 upsert             |

- 테스트/오프라인 등에서 DB를 건너뛰고 파일 기반으로만 동작시키려면 `GRAPH_RAG_PROMPT_SOURCE=file`로 설정한다.

## 수정 위치

아래 표의 `docs/prompts` 파일은 DB `name`(=파일명)과 1:1로 대응한다. 파일을 편집한 뒤 `POST /prompts/seed`로 DB에 반영하거나, `PUT /prompts/{name}`로 DB를 직접 수정한다. `GRAPH_RAG_PROMPT_SOURCE=file`이면 파일을 바로 읽으므로 같은 명령을 다시 실행하면 변경 사항이 반영된다.

| 파일                                               | 반영 위치                        | 목적                                                           |
| -------------------------------------------------- | -------------------------------- | -------------------------------------------------------------- |
| `docs/prompts/query_plan_system.txt`               | LLM Query Parser system message  | Query Plan 생성 역할, canonical 값 제한, 부정 조건 처리 원칙   |
| `docs/prompts/query_plan_user.txt`                 | LLM Query Parser user message    | 사용자 질문, 허용 canonical 값, fallback plan을 묶는 템플릿    |
| `docs/prompts/answer_system.txt`                   | 최종 답변 system message         | 검증된 SQL만 사용하도록 답변 생성 역할 제한                    |
| `docs/prompts/answer_user.txt`                     | 최종 답변 user message           | Query Plan, 검색 Context, SQL Result를 답변 생성 입력으로 구성 |
| `docs/prompts/message_generation_system.txt`       | 메시지 생성 system message       | LMS/RCS 메시지 생성 역할, 허위 혜택 방지, 채널 제약을 제한     |
| `docs/prompts/message_generation_user.txt`         | 메시지 생성 user message         | 캠페인/타겟/SQL context를 메시지 3종 생성 입력으로 구성        |
| `docs/prompts/message_generation_variant_user.txt` | 메시지 variant 생성 user message | 병렬 생성 시 지정 variant 1개만 만들도록 제한                  |
| `docs/prompts/message_generation_retry_user.txt`   | 메시지 생성 재시도 user message  | 검증 실패 사유를 바탕으로 다음 attempt를 수정                  |
| `docs/prompts/message_generation_tone_manner.txt`  | 메시지 톤앤매너 규칙             | 브랜드 톤, 기존 메시지 스타일, variant별 설득 포인트를 조정    |

기본 프롬프트 디렉터리는 `GRAPH_RAG_PROMPT_DIR` 환경 변수나 CLI의 `--prompt-dir` 옵션으로 바꿀 수 있다.

```bash
docker compose run --rm python python graph_rag.py "20대 여성 장바구니 이탈 고객에게 쿠폰 캠페인 추천" --query-parser auto --prompt-dir docs/prompts --format json
```

## 템플릿 변수

프롬프트 파일에는 다음 변수를 사용할 수 있다. 변수는 `${name}` 형식으로 작성한다.

| 파일                                  | 사용 가능한 변수                                                                                                                                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `query_plan_user.txt`                 | `${query}`, `${allowed_values}`, `${fallback_plan}`                                                                                                                                                                |
| `answer_user.txt`                     | `${query}`, `${query_plan}`, `${context}`, `${sql_result}`, `${sql_policy}`                                                                                                                                        |
| `message_generation_user.txt`         | `${query}`, `${requested_channel}`, `${channel_policy}`, `${selected_channel_policy}`, `${query_plan}`, `${campaign_context}`, `${target_context}`, `${message_examples}`, `${tone_manner_rules}`, `${sql_result}` |
| `message_generation_variant_user.txt` | `${variant}`, `${requested_channel}`, `${selected_channel_policy}`, `${campaign_context}`, `${target_context}`, `${message_examples}`, `${tone_manner_rules}`, `${repair_context}`                                 |
| `message_generation_retry_user.txt`   | `${original_prompt}`, `${previous_content}`, `${failure_reason}`, `${validation_issues}`, `${attempt_number}`, `${max_attempts}`                                                                                   |

`query_plan_system.txt`, `answer_system.txt`, `message_generation_system.txt`, `message_generation_tone_manner.txt`는 고정 텍스트로 읽히므로 템플릿 변수를 사용하지 않는다.

## 운영 시 우선순위

1. 자연어 조건 추출이 틀리면 `query_plan_system.txt` 또는 `query_plan_user.txt`를 조정한다.
2. 검증된 SQL이 있는데 답변이 과장되거나 새 SQL을 만들려고 하면 `answer_system.txt` 또는 `answer_user.txt`를 조정한다.
3. 메시지의 말투, 브랜드 표현, variant별 설득 포인트가 어긋나면 `message_generation_tone_manner.txt`를 먼저 조정한다.
4. 메시지 JSON 구조나 채널별 필드가 틀리면 `message_generation_user.txt`, `message_generation_variant_user.txt`, `message_generation_retry_user.txt`를 조정한다.
5. 올바른 테이블이나 컬럼을 못 찾으면 프롬프트보다 `docs/data/schema_catalog.json`의 `description_llm`, `human_note`와 `docs/data/sql_examples.sample.sql`을 먼저 보강한다.
6. 특정 표현을 canonical 값으로 못 바꾸면 `docs/data/normalization_rules.sample.json`을 먼저 보강한다.

## 주의 사항

- SQL을 직접 생성하라는 지시를 프롬프트에 추가하지 않는다. 최종 SQL은 `graph_rag.py`의 조건 토큰, SQL 템플릿, `sql_guard.py` 검증 결과만 사용한다.
- 질문에 없는 성별, 연령, 행동, 혜택 조건을 임의로 추가하지 않는다.
- 부정 조건은 긍정 조건으로 치환하지 않고 `exclude`에 남긴다.
- 프롬프트 변경 후에는 대표 질문을 `--format json`으로 재실행해 `query_plan`, `sql_result`, `answer_prompt`, `message_generation_prompt`를 확인한다.
