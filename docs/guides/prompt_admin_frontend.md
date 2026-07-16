# 프롬프트 관리 화면 — 프론트엔드 구현 가이드

LLM 프롬프트를 DB(`campaign_prompt_templates`)에서 관리하는 관리자 화면의 API 명세와 화면 구성을 정리한다. 백엔드는 이미 구현/검증되어 있으며, 이 문서는 프론트 구현 기준이다.

- 기본 API 호스트: `http://localhost:8000` (배포 환경에 맞게 교체)
- 인증: 현재 별도 인증 없음(내부 운영 화면 전제). 필요 시 게이트웨이/프록시 단에서 보호.
- 공통 성공 규약: 변경성 요청은 `is_success: true`를 포함.
- 공통 실패 규약: FastAPI 표준 `{"detail": "<error_code>"}` + HTTP 상태 코드.

---

## 1. API 명세

### 1.1 프롬프트 목록 조회

```
GET /prompts
```

응답 `200`

```json
{
  "prompts": [
    {
      "name": "query_plan_system.txt",
      "content": "너는 캠페인 추천/NL2SQL Query Planner다. ...",
      "description": null,
      "updated_at": "2026-07-16T14:05:38.236285+00:00"
    }
  ],
  "count": 9
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `prompts[].name` | string | 프롬프트 식별자(=파일명). 예: `query_plan_system.txt` |
| `prompts[].content` | string | 프롬프트 본문 전체 |
| `prompts[].description` | string \| null | 용도 메모(선택) |
| `prompts[].updated_at` | string(ISO8601) | 마지막 수정 시각(UTC) |
| `count` | number | 총 개수 |

> 목록에도 `content` 전체가 포함된다. 리스트 화면에서는 미리보기(앞 1~2줄)만 노출하고 본문은 상세에서 보여주는 것을 권장.

### 1.2 단일 프롬프트 조회

```
GET /prompts/{name}
```

- `name`은 URL 인코딩 필요(파일명에 `.`이 포함됨. 예: `query_plan_system.txt`).

응답 `200`

```json
{
  "name": "query_plan_system.txt",
  "content": "너는 캠페인 추천/NL2SQL Query Planner다. ...",
  "description": "쿼리 플래너 시스템 프롬프트",
  "updated_at": "2026-07-16T14:05:38.236285+00:00"
}
```

응답 `404`

```json
{ "detail": "prompt_template_not_found" }
```

### 1.3 프롬프트 추가/수정 (upsert)

```
PUT /prompts/{name}
Content-Type: application/json
```

요청 바디

```json
{
  "content": "프롬프트 본문 (필수, 1자 이상)",
  "description": "용도 메모 (선택)"
}
```

| 필드 | 타입 | 필수 | 비고 |
| --- | --- | --- | --- |
| `content` | string | ✅ | 최소 1자. 빈 문자열이면 `422` |
| `description` | string \| null | ❌ | 생략 시 기존 `description` 유지(덮어쓰지 않음) |

응답 `200`

```json
{
  "is_success": true,
  "prompt": {
    "name": "query_plan_system.txt",
    "content": "...",
    "description": "쿼리 플래너 시스템 프롬프트",
    "updated_at": "2026-07-16T14:10:02.101234+00:00"
  }
}
```

- 존재하지 않는 `name`이면 새로 생성(upsert). 즉 "새 프롬프트 추가"도 이 엔드포인트를 사용.
- 유효성 실패(예: `content` 누락/빈값)는 FastAPI 표준 `422` (`detail`은 필드 에러 배열).
- 저장 즉시 실행 중인 API 서버의 캐시가 갱신되어 다음 LLM 호출부터 반영된다.

### 1.4 프롬프트 삭제

```
DELETE /prompts/{name}
```

응답 `200`

```json
{ "is_success": true, "deleted_prompt": "query_plan_system.txt" }
```

응답 `404`

```json
{ "detail": "prompt_template_not_found" }
```

- 삭제해도 서비스는 파일(`docs/prompts`) → 코드 기본값 순으로 계속 동작한다(끊기지 않음). 화면에서는 "삭제 시 파일/기본값으로 되돌아감"을 안내.

### 1.5 캐시 리로드 = "변경사항 적용" 버튼

```
POST /prompts/reload
```

응답 `200`

```json
{ "is_success": true, "loaded": 9 }
```

- 화면의 **[변경사항 적용]** 버튼이 이 엔드포인트에 매핑된다.
- DB의 최신 프롬프트를 실행 중인 API 서버 캐시로 다시 불러온다. 이후 LLM 호출부터 반영된다.
- 사용 시점:
  - DB를 직접(psql/관리 콘솔 등) 수정한 뒤 반영할 때
  - 여러 API 인스턴스의 캐시를 강제로 동기화할 때
  - 운영자가 저장과 별개로 "지금 반영"을 명시적으로 확정하고 싶을 때

> 참고: `PUT /prompts/{name}`(저장), `DELETE`, `POST /prompts/seed`는 **성공 시 해당 API 프로세스의 캐시를 이미 자동 갱신**한다. 따라서 단일 인스턴스에서는 저장만으로도 반영되며, [변경사항 적용] 버튼은 위 상황을 위한 명시적/보조 수단이다. UX상 "저장 = DB 기록, 적용 = 실행 반영"으로 역할을 나눠 안내하면 운영자가 이해하기 쉽다.

### 1.6 파일에서 시딩

```
POST /prompts/seed
```

응답 `200`

```json
{
  "is_success": true,
  "count": 9,
  "seeded": [
    { "name": "answer_system.txt", "content": "...", "description": null, "updated_at": "..." }
  ]
}
```

- `docs/prompts/*.txt` 파일을 DB로 upsert. 최초 1회 초기화 또는 "파일 원본으로 되돌리기" 용도.
- 화면에서는 위험 동작으로 취급(기존 DB 편집분을 파일 내용으로 덮어씀) → 확인 모달 권장.

### 1.7 에러 코드 정리

| HTTP | detail | 발생 지점 |
| --- | --- | --- |
| 404 | `prompt_template_not_found` | 조회/삭제 시 대상 없음 |
| 422 | (FastAPI 필드 에러 배열) | `content` 누락/빈값 등 유효성 실패 |
| 500 | `prompt_templates_lookup_failed:<Exc>` | 목록 조회 중 DB 오류 |
| 500 | `prompt_template_lookup_failed:<Exc>` | 단일 조회 중 DB 오류 |
| 500 | `prompt_template_upsert_failed:<Exc>` | 저장 중 DB 오류 |
| 500 | `prompt_template_delete_failed:<Exc>` | 삭제 중 DB 오류 |
| 500 | `prompt_templates_reload_failed:<Exc>` | 리로드 중 DB 오류 |
| 500 | `prompt_templates_seed_failed:<Exc>` | 시딩 중 파일/DB 오류 |
| 500 | `psycopg_import_failed:<Exc>` | DB 드라이버 미설치 |

---

## 2. 프롬프트 카탈로그 (편집 대상 9종)

리스트/상세 화면에서 프롬프트별 "역할"과 "사용 가능한 템플릿 변수"를 함께 보여주면 편집 실수를 줄일 수 있다. 변수는 본문에서 `${변수명}` 형식으로 사용한다.

| name | 역할 | 사용 가능한 변수 |
| --- | --- | --- |
| `query_plan_system.txt` | Query Plan 생성 역할, canonical 값 제한, 부정 조건 처리 | (고정 텍스트, 변수 없음) |
| `query_plan_user.txt` | 질문·허용값·fallback plan을 묶는 템플릿 | `${query}`, `${allowed_values}`, `${fallback_plan}` |
| `answer_system.txt` | 검증된 SQL만 사용하도록 답변 역할 제한 | (고정 텍스트, 변수 없음) |
| `answer_user.txt` | Query Plan·Context·SQL 결과로 답변 입력 구성 | `${query}`, `${query_plan}`, `${context}`, `${sql_result}`, `${sql_policy}` |
| `message_generation_system.txt` | 메시지 생성 역할, 허위 혜택 방지, 채널 제약 | (고정 텍스트, 변수 없음) |
| `message_generation_user.txt` | 캠페인/타겟/SQL context로 메시지 3종 생성 | `${query}`, `${requested_channel}`, `${channel_policy}`, `${selected_channel_policy}`, `${query_plan}`, `${campaign_context}`, `${target_context}`, `${message_examples}`, `${tone_manner_rules}`, `${sql_result}` |
| `message_generation_variant_user.txt` | variant 1개만 생성 | `${variant}`, `${requested_channel}`, `${selected_channel_policy}`, `${campaign_context}`, `${target_context}`, `${message_examples}`, `${tone_manner_rules}`, `${repair_context}` |
| `message_generation_retry_user.txt` | 검증 실패 사유로 재시도 수정 | `${original_prompt}`, `${previous_content}`, `${failure_reason}`, `${validation_issues}`, `${attempt_number}`, `${max_attempts}` |
| `message_generation_tone_manner.txt` | 브랜드 톤·스타일·설득 포인트 | (고정 텍스트, 변수 없음) |

> 프론트에서 이 표를 상수로 들고 있으면(name → {role, variables}) 상세 화면의 변수 도우미 패널과 저장 전 `${...}` 검증에 활용할 수 있다.

---

## 3. 화면 구성

### 3.1 화면 목록

| 화면 | 경로(예시) | 목적 |
| --- | --- | --- |
| 프롬프트 목록 | `/admin/prompts` | 전체 프롬프트 조회, 검색, 상세 진입 |
| 프롬프트 상세/편집 | `/admin/prompts/:name` | 본문 편집, 저장, 삭제, 변수 도우미 |
| (모달) 시딩/리로드 | 목록 화면 내 | 파일 초기화, 캐시 동기화 |

### 3.2 프롬프트 목록 화면

```
┌───────────────────────────────────────────────────────────────┐
│ 프롬프트 관리            [파일에서 시딩] [변경사항 적용] ●변경중 │
│ ┌───────────────────────────────────────────────────────────┐ │
│ │ 🔍 검색 (name / 내용)                                       │ │
│ └───────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────┬───────────────┬────────────────┐ │
│ │ name                     │ 설명          │ 수정 시각       │ │
│ ├──────────────────────────┼───────────────┼────────────────┤ │
│ │ query_plan_system.txt    │ 쿼리 플래너…  │ 07-16 14:05    │ │
│ │  너는 캠페인 추천/NL2SQL… │               │            [편집]│ │
│ ├──────────────────────────┼───────────────┼────────────────┤ │
│ │ answer_user.txt          │ –             │ 07-16 14:05    │ │
│ └──────────────────────────┴───────────────┴────────────────┘ │
│                              총 9개                    [+ 새 프롬프트] │
└───────────────────────────────────────────────────────────────┘
```

- 데이터: `GET /prompts`
- 컬럼: `name`, `description`(없으면 `–`), `updated_at`(로컬 타임존 포맷), 본문 미리보기(1줄, 말줄임)
- 액션:
  - 행 클릭/`[편집]` → 상세 화면
  - `[+ 새 프롬프트]` → 상세 화면(신규 모드, `name` 입력 활성)
  - `[파일에서 시딩]` → 확인 모달 후 `POST /prompts/seed`
  - `[변경사항 적용]` → `POST /prompts/reload`, 토스트로 `loaded` 개수 표시 (§3.4 참고)
- `●변경중` 표식: 이 화면에서 저장/삭제 후 아직 [변경사항 적용]을 누르지 않았을 때 노출하는 선택적 인디케이터. 단일 인스턴스 환경에선 생략 가능.
- 검색은 클라이언트 필터로 충분(9건 규모). `name`/`content` 부분일치.

### 3.3 프롬프트 상세/편집 화면

```
┌───────────────────────────────────────────────────────────────┐
│ ← 목록   query_plan_user.txt        [저장] [저장 후 적용] [삭제] │
│ 설명: [ 쿼리 플래너 유저 프롬프트                        ]      │
│ 수정 시각: 2026-07-16 23:10 (KST)                              │
│ ┌─────────────────────────────────┬───────────────────────────┐ │
│ │ 본문 (content)                  │ 사용 가능한 변수           │ │
│ │ ┌─────────────────────────────┐ │  ${query}        [삽입]    │ │
│ │ │ 사용자 질문: ${query}        │ │  ${allowed_values} [삽입] │ │
│ │ │ 허용 값: ${allowed_values}   │ │  ${fallback_plan}  [삽입] │ │
│ │ │ ...                          │ │                           │ │
│ │ │                              │ │ ⚠ 미정의 변수: 없음        │ │
│ │ └─────────────────────────────┘ │                           │ │
│ └─────────────────────────────────┴───────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

- 데이터: `GET /prompts/{name}` (신규 모드는 빈 폼)
- 필드:
  - `name` — 신규일 때만 입력, 편집일 때는 읽기 전용(파일명 규칙 안내: 소문자+`_`+`.txt`)
  - `description` — 텍스트 입력(선택)
  - `content` — 멀티라인 에디터(모노스페이스, 자동 높이/스크롤). 코드 에디터 컴포넌트 권장
- 변수 도우미 패널:
  - 카탈로그 상수에서 해당 `name`의 변수 목록 표시, `[삽입]`으로 커서 위치에 `${var}` 삽입
  - 저장 전 본문의 `${...}` 토큰을 스캔해 카탈로그에 없는 변수는 경고(치명적 아님, 확인 후 저장 허용)
- 액션:
  - `[저장]` → `PUT /prompts/{name}` (신규/편집 공통). 성공 시 토스트 + 목록 갱신. DB에만 기록.
  - `[저장 후 적용]` → `PUT /prompts/{name}` 성공 후 이어서 `POST /prompts/reload` 호출. 저장과 실행 반영을 한 번에 확정. (§3.4 참고)
  - `[삭제]` → 확인 모달 → `DELETE /prompts/{name}`. "삭제해도 파일/기본값으로 계속 동작함" 안내 문구 포함
- 저장 버튼은 `content`가 비어 있으면 비활성(백엔드도 `422`로 막지만 UX상 사전 차단).
- 단일 인스턴스 환경에서 UI를 단순화하려면 `[저장]` 하나만 두고(저장이 곧 반영), 다중 인스턴스/직접 DB 편집을 고려한다면 `[저장 후 적용]`을 기본, `[저장]`을 보조로 둔다.

### 3.4 "변경사항 적용" 버튼 문구 (카피)

`POST /prompts/reload`에 매핑되는 버튼의 라벨/설명/토스트 문구 표준안이다. `[저장 후 적용]`은 `PUT` → `reload`를 이어서 호출하므로 저장 실패 시 적용 단계로 넘어가지 않는다.

| 위치 | 상황 | 문구 |
| --- | --- | --- |
| 버튼 라벨 | 목록 화면 | `변경사항 적용` |
| 버튼 라벨 | 상세 화면 | `저장 후 적용` |
| 버튼 툴팁/보조설명 | 공통 | `저장한 프롬프트를 실행 중인 서버에 즉시 반영합니다.` |
| 버튼 로딩 라벨 | 요청 중 | `적용 중…` |
| 토스트(성공) | reload 완료 | `프롬프트 {loaded}개를 적용했습니다. 다음 생성 요청부터 반영됩니다.` |
| 토스트(성공) | 저장 후 적용 완료 | `저장하고 적용했습니다. 다음 생성 요청부터 반영됩니다.` |
| 토스트(실패) | 500 등 | `적용에 실패했습니다. 저장 내용은 유지됩니다. 잠시 후 다시 시도해 주세요.` |
| 인디케이터 | 저장했으나 미적용 | `적용되지 않은 변경사항이 있습니다.` |
| 빈 상태 힌트 | 최초 진입 | `프롬프트를 수정한 뒤 [적용]을 눌러 서버에 반영하세요.` |

- 실패 시 안내의 핵심: **저장(DB)은 이미 끝났고 적용(실행 반영)만 실패**했다는 점을 명확히 해 데이터 유실 오해를 막는다. 재시도는 [변경사항 적용]을 다시 누르면 된다.
- 문구에서 "즉시"는 캐시 갱신 기준이며, 이미 진행 중인 생성 요청에는 적용되지 않고 "다음 생성 요청부터" 반영됨을 함께 안내한다.

### 3.5 확인 모달

| 액션 | 모달 문구 |
| --- | --- |
| 파일에서 시딩 | "docs/prompts 파일 내용으로 DB를 덮어씁니다. DB에서 편집한 내용이 있으면 사라집니다. 계속할까요?" |
| 삭제 | "이 프롬프트를 삭제하면 서비스는 파일 또는 코드 기본값을 사용합니다. 삭제할까요?" |

---

## 4. 상태/에러 처리 UX

- 로딩: 목록/상세 진입 시 스켈레톤 또는 스피너.
- 저장/삭제/시딩/리로드: 버튼 인라인 로딩 + 성공 토스트.
- 404(`prompt_template_not_found`): 상세 화면에서 "삭제되었거나 없는 프롬프트" 안내 후 목록으로.
- 422: 필드별 에러 메시지 표시(주로 `content` 필수).
- 500(`*_failed:*`): "서버/DB 오류" 토스트 + 재시도 버튼. `detail` 원문은 개발 모드에서만 노출.
- 낙관적 업데이트보다는 응답의 `updated_at`/`prompt`를 신뢰해 목록을 재조회하는 편이 안전.

---

## 5. 참고 사항

- `name`은 반드시 URL 인코딩해서 요청(`.` 포함). 예: `encodeURIComponent("query_plan_system.txt")`.
- 저장은 즉시 서버 캐시에 반영되지만, 여러 API 인스턴스를 운용하면 각 인스턴스 캐시가 분리되므로 필요 시 각 인스턴스에 `POST /prompts/reload`가 필요할 수 있다(현재 단일 인스턴스 전제).
- `GRAPH_RAG_PROMPT_SOURCE=file`로 뜬 서버는 DB 편집이 LLM 호출에 반영되지 않는다(파일 우선 모드). 운영 서버는 기본값(`db`)로 둘 것.
- 백엔드 상세 동작은 [prompt_engineering.md](./prompt_engineering.md) 참고.
