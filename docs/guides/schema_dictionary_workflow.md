# DDL 기반 스키마 사전 운영 방식

## 원칙

이 프로젝트의 스키마 사전은 DDL에서 자동 추출한 구조 정보를 기준으로 관리한다.

1. 스키마 구조는 `docs/data/local_bootstrap.sql`에서 자동 추출한다.
2. LLM은 테이블별 설명만 생성한다.
3. 사람이 직접 수정하는 영역은 중요한 컬럼의 `human_note`로 제한한다.
4. SQL 예시는 10~30개만 유지한다.
5. 운영 로그에서 실패/오해 사례를 모아 부족한 사전 용어와 SQL 예시를 보강한다.

## 자동 추출

```bash
docker compose run --rm python python schema_extract.py docs/data/local_bootstrap.sql --output docs/data/schema_catalog.json
```

생성 파일:

- `docs/data/schema_catalog.json`

이미 생성된 카탈로그가 있으면 재추출 시 `description_llm`과 `human_note`는 보존한다.

자동 추출 대상:

- 테이블명
- 컬럼명
- 컬럼 타입
- nullable 여부
- primary key
- foreign key
- check 제약
- index

LLM이 생성하지 않는 값:

- 테이블명
- 컬럼명
- 타입
- 키/제약/인덱스
- 조인 관계

## LLM 생성 범위

LLM은 `description_llm`만 채운다. 컬럼 설명을 전부 생성하지 않는다.

권장 프롬프트:

```text
너는 PostgreSQL 스키마 사전 작성자다.
입력 JSON의 테이블 구조를 보고 각 테이블의 description_llm만 한국어 한 문장으로 작성해라.
컬럼명, 타입, 키, 인덱스, important, human_note는 절대 수정하지 마라.
출력은 입력과 같은 JSON 구조로만 반환해라.
```

## 사람 수정 범위

사람은 `important: true`인 컬럼의 `human_note`만 수정한다.

수정 예시:

```json
{
  "name": "lifecycle",
  "important": true,
  "human_note": "고객 상태를 나타낸다. active, new, inactive_90d, vip, cart_abandoner 등이 주요 값이다."
}
```

중요하지 않은 컬럼은 기본적으로 자동 추출값을 그대로 사용한다. 필요하면 `schema_extract.py`의 `IMPORTANT_COLUMN_NAMES`에 컬럼명을 추가한 뒤 카탈로그를 다시 생성한다.

## SQL 예시 관리

SQL 예시는 `docs/data/sql_examples.sample.sql`에 10~30개만 둔다. 예시는 다음 범주가 고르게 포함되게 관리한다.

- 단일 테이블 필터
- 사용자 세그먼트 조회
- 캠페인 조건 조회
- 캠페인-채널/키워드/타겟 세그먼트 조인
- 추천 edge 조회
- 집계와 정렬

예시를 너무 많이 넣으면 검색 품질이 희석되므로 운영에서 실제로 반복되는 질문만 남긴다.

## 운영 로그 반영

운영 로그는 다음 형식으로 주기적으로 검토한다.

| 로그 유형                              | 조치                                                              |
| -------------------------------------- | ----------------------------------------------------------------- |
| 자연어 용어를 canonical 값으로 못 바꿈 | `docs/data/normalization_rules.sample.json`에 동의어 추가         |
| 올바른 테이블을 못 고름                | 해당 테이블의 `description_llm` 개선                              |
| 중요한 조건 컬럼을 빠뜨림              | 해당 컬럼을 `IMPORTANT_COLUMN_NAMES`에 추가하고 `human_note` 작성 |
| 조인 경로를 틀림                       | `docs/data/sql_examples.sample.sql`에 대표 예시 1개 추가          |
| 비슷한 예시가 이미 많음                | 기존 예시를 교체하고 전체 개수는 30개 이하로 유지                 |

반영 순서:

1. 실패 로그를 유형별로 묶는다.
2. 사전 문제인지, 스키마 설명 문제인지, SQL 예시 문제인지 나눈다.
3. 사전 또는 예시를 최소 단위로 추가한다.
4. 같은 질문을 재실행해 개선 여부를 확인한다.
