# 채널 메시지 생성

이 문서는 타겟팅 SQL 생성 이후 LMS/RCS 발송 문안을 만드는 후속 단계를 정의한다.

## 화면에서의 위치

캠페인 자동 추천 화면에서는 3단계 `메시지 추천`이 이 문서의 담당 범위다.

```text
1. 프롬프트 입력 -> 2. 타겟팅 결과 -> 3. 메시지 추천 -> 4. 클릭 분석
```

운영자는 1단계에서 LMS 또는 RCS를 선택한다. 선택 채널은 메시지 생성 채널로 고정되어 3단계 제목 badge와 각 메시지의 `channel` 값에 반영된다. 2단계에서 타겟 SQL이 성공하지 않았거나 캠페인 컨텍스트가 없으면 3단계로 넘어가지 않는다.

3단계 화면은 메시지 3개를 카드 형태로 보여준다.

| 카드 요소                    | 데이터 기준                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| 제목 옆 badge                | `channel` 값. `LMS` 또는 `RCS`로 표시한다.                                                        |
| `시안 1`, `시안 2`, `시안 3` | 생성 순서. 내부 variant는 각각 `benefit_emphasis`, `urgency_emphasis`, `emotion_emphasis`다.      |
| 우측 badge                   | 메시지 강조 유형. 혜택 강조형, 긴급성 강조형, 감성 강조형으로 치환해 표시한다.                    |
| 굵은 제목                    | 근거 캠페인명 또는 메시지 추천명이 있으면 사용한다. 없으면 캠페인명과 `(광고)` suffix를 조합한다. |
| 본문 영역                    | 실제 발송 문안인 `text`를 그대로 표시한다.                                                        |

3단계의 `클릭률 예측 보기` 버튼은 메시지를 새로 생성하지 않는다. 현재 표시된 3개 문안을 A/B/C variant로 넘겨 클릭 분석 흐름을 시작한다.

## 전체 위치

기존 흐름은 다음과 같다.

```text
User Query -> Query Plan -> RAG Context -> SQL Result -> Answer/API Response
```

채널 메시지 생성은 `SQL Result`가 성공한 뒤에만 실행한다.

```text
SQL Result success -> Message Context -> Message Prompt -> OpenAI Variant Generation -> Validation -> API message_variants
```

SQL 생성이 실패하면 메시지를 만들지 않는다. 캠페인 offer 근거가 없으면 확인되지 않은 혜택 표현과 `used_offer`를 만들지 않는다. API의 `/channel-messages` 경로에서는 초기 RAG context에 캠페인 payload가 부족하면 PostgreSQL 실행 결과로 캠페인 컨텍스트를 보강한 뒤 메시지를 다시 생성한다.

## 채널 선택 규칙

- 생성 가능한 메시지 채널은 `lms`, `rcs`다.
- 사용자 프롬프트나 Query Plan에 `lms` 또는 `rcs`가 있으면 해당 채널을 사용한다.
- 프롬프트에 메시지 채널이 없으면 기본값은 `lms`다.
- `sms`, `email`, `app_push`, `kakao`, `instagram`은 기존 타겟팅/추천 채널로 유지한다. 메시지 생성 채널과는 구분한다.
- CLI에서는 `--message-channel auto|lms|rcs`로 선택한다.

## 근거 데이터

메시지 생성은 아래 근거만 사용한다.

- `campaigns.offer`: 혜택 문구의 1차 근거
- `campaigns.name`, `objective`, `category`, `start_date`, `end_date`: 캠페인 문맥
- `campaign_message_examples.message_text`: 기존 메시지 참고 문안
- `campaign_message_examples.brand_tone`: 브랜드 톤 참고
- `campaign_channel_messages.message_body`: 실제 발송 이력 참고
- `campaign_message_variants.message_body`: 실험 variant 문안 참고
- `campaign_message_deliveries`, `campaign_message_events`: 발송/반응 성과 분석 근거
- Query Plan의 `target_user`, `campaign_constraints`: 대상 고객 문맥

없는 혜택, 할인율, 무료 제공, 기간, 조건은 생성하지 않는다.

## DB 테이블

기존 메시지 참고와 실제 발송/성과 분석을 위해 아래 메시지 관련 테이블과 view를 둔다.

| 객체                          | 목적                                                         |
| ----------------------------- | ------------------------------------------------------------ |
| `campaign_message_examples`   | 새 문안 생성 시 참고할 과거 메시지 예시와 브랜드 톤          |
| `campaign_channel_messages`   | 캠페인 채널별 실제 발송 메시지 이력                          |
| `campaign_experiments`        | 캠페인별 LMS/RCS A/B/C 실험 실행 단위                        |
| `campaign_message_variants`   | 실험에 포함되는 메시지 variant와 랜딩 URL, 배정 가중치       |
| `campaign_message_deliveries` | 사용자별 variant 배정과 발송 상태                            |
| `campaign_message_events`     | 발송 요청, 도달, 노출, 클릭, 전환 등 append-only 이벤트 로그 |
| `v_campaign_variant_metrics`  | variant별 발송 퍼널, CTR/CVR, 매출 집계                      |
| `v_campaign_segment_metrics`  | 성별, 연령대, 지역, 라이프사이클별 성과 집계                 |
| `v_campaign_daily_metrics`    | 날짜별 이벤트 퍼널과 매출 추이                               |

`campaign_message_examples` 컬럼은 다음과 같다.

| 컬럼            | 목적                                                       |
| --------------- | ---------------------------------------------------------- |
| `example_id`    | 메시지 예시 식별자                                         |
| `campaign_id`   | 캠페인 FK                                                  |
| `channel`       | `lms` 또는 `rcs`                                           |
| `emphasis_type` | `benefit_emphasis`, `urgency_emphasis`, `emotion_emphasis` |
| `message_text`  | 기존 발송 문안                                             |
| `brand_tone`    | 참고할 브랜드 톤                                           |
| `created_at`    | 예시 생성/수집 시각                                        |

## 메시지 생성 규칙

- 기존 메시지를 참고하되 그대로 복사하지 않는다.
- 없는 혜택을 생성하지 않는다.
- 채널 글자 수를 준수한다.
- 브랜드 톤을 유지한다.
- 동일한 문장 구조를 반복하지 않는다.
- 추천 메시지는 3개 생성한다.

| variant            | 목적        |
| ------------------ | ----------- |
| `benefit_emphasis` | 혜택 강조   |
| `urgency_emphasis` | 긴급성 강조 |
| `emotion_emphasis` | 감성 강조   |

## 출력 구조

`/target-sql` 응답에서는 `message`를 상태 안내 문구로 유지하고 실제 발송 문안은 최상위 `message_variants`에 분리한다. CLI `graph_rag.py --format json` 결과에서는 같은 값이 `api_response.message_variants`에 들어간다.

`/channel-messages` 응답에서는 화면 바인딩을 쉽게 하기 위해 `channel`, `messages`, `message_count`, `targeting_result`를 최상위에 둔다. `messages` 배열의 원소는 아래 `message_variants`와 같은 의미를 가진다.

```json
{
  "message_variants": [
    {
      "channel": "lms",
      "variant": "benefit_emphasis",
      "text": "...",
      "source_campaign_id": "camp_001",
      "used_offer": "10% 할인 쿠폰",
      "char_count": 42,
      "within_limits": true
    }
  ],
  "message_generation_mode": "openai_chat_completion_parallel_variants",
  "message_generation_failure_reason": null
}
```

화면에서 A/B/C 실험으로 넘길 때는 다음 매핑을 사용한다.

| 메시지 variant     | 실험 code | 기본 이름     | 역할                                          |
| ------------------ | --------- | ------------- | --------------------------------------------- |
| `benefit_emphasis` | `A`       | 혜택 강조형   | 대조군으로 쓰기 좋다.                         |
| `urgency_emphasis` | `B`       | 긴급성 강조형 | 기간, 마감, 즉시 행동 표현의 성과를 비교한다. |
| `emotion_emphasis` | `C`       | 감성 강조형   | 개인화나 재방문 유도 표현의 성과를 비교한다.  |

대조군 기준이 별도로 있으면 `isControl`은 해당 문안에 둔다. 별도 기준이 없으면 `A`를 대조군으로 둔다.

## 검증 규칙

- `messages`는 배열이어야 한다.
- `benefit_emphasis`, `urgency_emphasis`, `emotion_emphasis`가 각각 1개씩 있어야 한다.
- 각 메시지의 `channel`은 요청 채널과 같아야 한다.
- `source_campaign_id`는 Message Context의 캠페인 ID 중 하나여야 한다.
- Message Context의 캠페인이 정확히 1개이고 `source_campaign_id`가 비어 있으면 검증 단계에서 해당 캠페인 ID로 보정한다.
- `used_offer`는 선택값이다. 값이 있으면 Message Context의 `offer` 중 하나와 정확히 같아야 하지만, 메시지 본문에 반드시 포함될 필요는 없다.
- 같은 본문을 중복 생성하면 실패한다.

## 지연 분석 로그

메시지 생성은 `benefit_emphasis`, `urgency_emphasis`, `emotion_emphasis`를 병렬 OpenAI 호출로 만든다. 전체 attempt 시간은 세 variant 중 가장 늦게 끝난 호출에 맞춰진다. 검증 실패가 발생하면 `MESSAGE_GENERATION_MAX_ATTEMPTS`만큼 재시도할 수 있으므로 응답시간은 attempt 수에 비례해 늘어난다.

API 로그의 `api_timing` 이벤트에는 다음 요약이 포함된다.

| 로그 필드                                                                                      | 설명                                      |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `database_message_refresh.status`                                                              | DB 컨텍스트 보강 후 메시지를 재생성했는지 |
| `database_message_refresh.message_generation_attempt_count`                                    | 메시지 생성 시도 횟수                     |
| `database_message_refresh.message_generation_timing.attempts[].duration_ms`                    | attempt 1회 전체 시간                     |
| `database_message_refresh.message_generation_timing.attempts[].variant_attempts[].duration_ms` | variant별 OpenAI 호출 시간                |
| `database_message_refresh.message_generation_failure_reason`                                   | 최종 실패 사유                            |

OpenAI variant 호출 timeout은 `.env`의 `MESSAGE_GENERATION_OPENAI_TIMEOUT_SECONDS`로 조절한다. 기본값은 15초다.

## 실행 예시

아래 명령은 `docker-compose.yml`이 있는 프로젝트 루트에서 실행해야 한다.

```powershell
Set-Location C:\PROJECT\sample
```

다른 폴더에서 실행하면 Docker Compose가 설정 파일을 찾지 못해 `no configuration file provided: not found`가 발생한다.

프롬프트만 확인한다.

```powershell
docker compose run --rm python python graph_rag.py "장바구니 이탈 여성 고객에게 쿠폰 캠페인 추천하고 RCS 메시지까지 만들어줘" --format json --data docs/data/campaign_user_rag_sample_50_with_edges.json --collection campaign_user_rag_nodes --vector-top-k 0 --keyword-top-k 5 --graph-top-k 5 --message-channel rcs
```

OpenAI로 메시지를 생성한다.

```powershell
docker compose run --rm python python graph_rag.py "장바구니 이탈 여성 고객에게 쿠폰 캠페인 추천하고 RCS 메시지까지 만들어줘" --format json --data docs/data/campaign_user_rag_sample_50_with_edges.json --collection campaign_user_rag_nodes --vector-top-k 0 --keyword-top-k 5 --graph-top-k 5 --query-parser auto --generate-messages --message-channel rcs
```

캠페인 payload가 필요한 경우 사용자/캠페인 컬렉션을 대상으로 실행한다.

```powershell
docker compose run --rm python python graph_rag.py "장바구니 이탈 고객에게 쿠폰 캠페인 추천하고 LMS 메시지 생성" --format json --data docs/data/campaign_user_rag_sample_50_with_edges.json --collection campaign_user_rag_nodes --vector-top-k 0 --keyword-top-k 5 --graph-top-k 5 --generate-messages --message-channel lms
```

프로젝트 루트로 이동하지 않고 실행해야 한다면 Compose 파일과 project directory를 직접 지정한다.

```powershell
docker compose -f C:\PROJECT\sample\docker-compose.yml --project-directory C:\PROJECT\sample run --rm python python graph_rag.py "장바구니 이탈 여성 고객에게 쿠폰 캠페인 추천하고 RCS 메시지까지 만들어줘" --format json --data docs/data/campaign_user_rag_sample_50_with_edges.json --collection campaign_user_rag_nodes --vector-top-k 0 --keyword-top-k 5 --graph-top-k 5 --message-channel rcs
```

기본 `campaign_knowledge_rag` 컬렉션은 스키마/정책/용어 중심이라 실제 캠페인 `offer` payload가 없을 수 있다. 이 경우 메시지 생성은 계속 진행하되 확인되지 않은 혜택 표현과 `used_offer`는 만들지 않는다.
