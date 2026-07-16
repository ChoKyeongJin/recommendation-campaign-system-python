# 정책 관리 화면 — 공통 프론트엔드 가이드

운영 정책(JSON)을 파일 대신 DB(`campaign_policies`)에서 관리하는 관리자 화면의 **공통 규약**(API 명세·화면 셸·버튼 카피·에러 UX)을 정리한다. 정책별 필드 스키마는 [전용 문서](#전용-문서-인덱스)로 분리한다. 백엔드는 구현되어 있으며, 이 문서는 프론트 구현 기준이다. 프롬프트 관리 화면([prompt_admin_frontend.md](./prompt_admin_frontend.md))과 동일한 패턴이므로 컴포넌트를 재사용할 수 있다.

- 기본 API 호스트: `http://localhost:8000` (배포 환경에 맞게 교체)
- 인증: 현재 별도 인증 없음(내부 운영 화면 전제). 필요 시 게이트웨이/프록시 단에서 보호.
- 공통 성공 규약: 변경성 요청은 `is_success: true`를 포함.
- 공통 실패 규약: FastAPI 표준 `{"detail": "<error_code>"}` + HTTP 상태 코드.
- **프롬프트와의 가장 큰 차이**: 정책 본문(`content`)은 문자열이 아니라 **JSON 객체**다. 편집기는 자유 텍스트가 아니라 키별 폼(또는 raw JSON 에디터)으로 구성한다.

> 이 문서는 정책 관리 화면의 **공통 규약**(REST API·목록/편집 화면 셸·버튼 카피·에러 UX)을 다룬다. 각 정책의 **필드 스키마와 편집 폼**은 아래 전용 문서를 참고한다.

### 전용 문서 인덱스

| 정책(`name`) | 역할 | 전용 문서 |
| --- | --- | --- |
| `ctr-model-policy` | 모델 버전 선택 / 휴리스틱 vs ML 경로 / 탐험(ε-greedy) | [ctr_model_policy_admin_frontend.md](./ctr_model_policy_admin_frontend.md) |
| `heuristic-ctr-rules` | 휴리스틱 CTR 스코어러의 기준 확률·가감치·매처 | [heuristic_ctr_rules_admin_frontend.md](./heuristic_ctr_rules_admin_frontend.md) |

저장소/화면은 범용으로 설계되어 있어 이후 다른 정책(`message-policy` 등)을 추가하면 같은 화면에 전용 문서 한 개를 더 붙이는 방식으로 확장한다.

---

## 1. API 명세

### 1.1 정책 목록 조회

```
GET /policies
```

응답 `200`

```json
{
  "policies": [
    {
      "name": "ctr-model-policy",
      "content": {
        "default_model_version": "heuristic-ctr-v1",
        "heuristic_model_version_prefixes": ["heuristic"],
        "fallback_to_heuristic_on_ml_error": true,
        "exploration_enabled": false,
        "default_epsilon": 0.0,
        "allow_request_epsilon_override": false
      },
      "description": null,
      "updated_at": "2026-07-16T14:05:38.236285+00:00"
    },
    {
      "name": "heuristic-ctr-rules",
      "content": {
        "base_probability": 0.025,
        "min_probability": 0.001,
        "max_probability": 0.25,
        "stable_noise_max": 0.01,
        "score_adjustments": { "preferred_channel": 0.012, "control_variant": 0.001 },
        "matchers": {
          "urgency_recent_behavior_keywords": ["cart_abandoned", "deal"],
          "personalized_lifecycles": ["active", "cart_abandoner", "vip"]
        }
      },
      "description": null,
      "updated_at": "2026-07-16T14:05:38.236285+00:00"
    }
  ],
  "count": 2
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `policies[].name` | string | 정책 식별자(=정책 파일명에서 확장자 제외). 예: `ctr-model-policy` |
| `policies[].content` | object | 정책 JSON 객체 전체 |
| `policies[].description` | string \| null | 용도 메모(선택) |
| `policies[].updated_at` | string(ISO8601) | 마지막 수정 시각(UTC) |
| `count` | number | 총 개수 |

### 1.2 단일 정책 조회

```
GET /policies/{name}
```

- `name` 예: `ctr-model-policy` (하이픈 포함, 특수문자 없으면 인코딩 불필요하나 안전하게 `encodeURIComponent` 권장)

응답 `200`

```json
{
  "name": "ctr-model-policy",
  "content": {
    "default_model_version": "heuristic-ctr-v1",
    "heuristic_model_version_prefixes": ["heuristic"],
    "fallback_to_heuristic_on_ml_error": true,
    "exploration_enabled": false,
    "default_epsilon": 0.0,
    "allow_request_epsilon_override": false
  },
  "description": "CTR 모델 선택/탐험 정책",
  "updated_at": "2026-07-16T14:05:38.236285+00:00"
}
```

응답 `404`

```json
{ "detail": "policy_not_found" }
```

### 1.3 정책 추가/수정 (upsert)

```
PUT /policies/{name}
Content-Type: application/json
```

요청 바디

```json
{
  "content": {
    "default_model_version": "heuristic-ctr-v1",
    "heuristic_model_version_prefixes": ["heuristic"],
    "fallback_to_heuristic_on_ml_error": true,
    "exploration_enabled": true,
    "default_epsilon": 0.1,
    "allow_request_epsilon_override": false
  },
  "description": "탐험 10% 활성화 (선택)"
}
```

| 필드 | 타입 | 필수 | 비고 |
| --- | --- | --- | --- |
| `content` | object | ✅ | JSON 객체여야 함. 배열/문자열/숫자면 `422` |
| `description` | string \| null | ❌ | 생략 시 기존 `description` 유지(덮어쓰지 않음) |

응답 `200`

```json
{
  "is_success": true,
  "policy": {
    "name": "ctr-model-policy",
    "content": { "...": "..." },
    "description": "탐험 10% 활성화",
    "updated_at": "2026-07-16T14:10:02.101234+00:00"
  }
}
```

- 존재하지 않는 `name`이면 새로 생성(upsert). "새 정책 추가"도 이 엔드포인트를 사용.
- 저장 즉시 실행 중인 API 서버의 캐시가 갱신되어 다음 스코어링/어사인먼트부터 반영된다.
- **부분 수정이 아니라 전체 교체다.** `content`에 담긴 객체가 통째로 저장된다. 폼에서 일부 필드만 바꿔도 나머지 필드를 함께 실어 보내야 한다(누락 키는 로딩 시 코드 기본값으로 보정되지만, 명시적으로 전체를 보내는 것을 권장).

### 1.4 정책 삭제

```
DELETE /policies/{name}
```

응답 `200`

```json
{ "is_success": true, "deleted_policy": "ctr-model-policy" }
```

응답 `404`

```json
{ "detail": "policy_not_found" }
```

- 삭제해도 서비스는 파일(`docs/policies/ctr-model-policy.json`) → 코드 기본값 순으로 계속 동작한다(끊기지 않음). 화면에서는 "삭제 시 파일/기본값으로 되돌아감"을 안내.

### 1.5 캐시 리로드 = "변경사항 적용" 버튼

```
POST /policies/reload
```

응답 `200`

```json
{ "is_success": true, "loaded": 1 }
```

- 화면의 **[변경사항 적용]** 버튼이 이 엔드포인트에 매핑된다.
- DB의 최신 정책을 실행 중인 API 서버 캐시로 다시 불러온다. 이후 스코어링/어사인먼트부터 반영된다.
- 사용 시점: DB를 직접 수정한 뒤 반영할 때 / 여러 API 인스턴스 캐시를 강제 동기화할 때 / 저장과 별개로 "지금 반영"을 명시적으로 확정하고 싶을 때.

> 참고: `PUT /policies/{name}`(저장), `DELETE`, `POST /policies/seed`는 **성공 시 해당 API 프로세스의 캐시를 이미 자동 갱신**한다. 단일 인스턴스에서는 저장만으로도 반영되며, [변경사항 적용]은 위 상황을 위한 보조 수단이다.

### 1.6 파일에서 시딩

```
POST /policies/seed
```

응답 `200`

```json
{
  "is_success": true,
  "count": 3,
  "seeded": [
    { "name": "ctr-model-policy", "content": { "...": "..." }, "description": null, "updated_at": "..." }
  ]
}
```

- `docs/policies/*.json` 파일을 DB로 upsert. 최초 1회 초기화 또는 "파일 원본으로 되돌리기" 용도.
- 화면에서는 위험 동작으로 취급(기존 DB 편집분을 파일 내용으로 덮어씀) → 확인 모달 권장.
- 참고: 이 엔드포인트는 `docs/policies` 폴더의 **모든** `*.json`을 시딩한다. 현재 DB 로더가 연결된 정책은 `ctr-model-policy` 뿐이지만, 다른 파일도 함께 DB 행으로 생성된다(미사용 행이므로 무해).

### 1.7 에러 코드 정리

| HTTP | detail | 발생 지점 |
| --- | --- | --- |
| 404 | `policy_not_found` | 조회/삭제 시 대상 없음 |
| 422 | (FastAPI 필드 에러 배열) | `content`가 객체가 아니거나 누락 |
| 500 | `policies_lookup_failed:<Exc>` | 목록 조회 중 DB 오류 |
| 500 | `policy_lookup_failed:<Exc>` | 단일 조회 중 DB 오류 |
| 500 | `policy_upsert_failed:<Exc>` | 저장 중 DB 오류 |
| 500 | `policy_delete_failed:<Exc>` | 삭제 중 DB 오류 |
| 500 | `policies_reload_failed:<Exc>` | 리로드 중 DB 오류 |
| 500 | `policies_seed_failed:<Exc>` | 시딩 중 파일/DB 오류 |

---

## 2. 정책별 필드 스키마 (전용 문서)

각 정책의 필드 명세·의존 관계·편집 폼 구성은 정책별 전용 문서로 분리한다. 프론트에서는 정책마다 이 스키마를 상수로 들고 폼 렌더링·검증·툴팁에 활용한다.

| 정책(`name`) | 전용 문서 |
| --- | --- |
| `ctr-model-policy` | [ctr_model_policy_admin_frontend.md](./ctr_model_policy_admin_frontend.md) |
| `heuristic-ctr-rules` | [heuristic_ctr_rules_admin_frontend.md](./heuristic_ctr_rules_admin_frontend.md) |

### 2.1 공통 로딩 규칙 (모든 정책 공통)

- 로딩 우선순위는 **DB → 파일(`docs/policies/<name>.json`) → 코드 기본값**이다.
- DB에 일부 키만 있어도 나머지 키는 코드 기본값으로 자동 보정된다. 중첩 구조 정책(예: `heuristic-ctr-rules`)은 **한 단계 중첩까지 병합**되므로 하위 객체의 한 항목만 저장해도 나머지 항목은 기본값이 유지된다.
- **저장은 전체 객체 단위 교체**다. 예측 가능성을 위해 폼에서 일부만 바꿔도 전체 `content`를 실어 보내는 것을 권장한다.
- `content`는 반드시 JSON **객체**여야 한다(배열/문자열/숫자면 `422`).

---

## 3. 화면 구성

### 3.1 화면 목록

| 화면 | 경로(예시) | 목적 |
| --- | --- | --- |
| 정책 목록 | `/admin/policies` | 전체 정책 조회, 검색, 상세 진입 |
| 정책 상세/편집 | `/admin/policies/:name` | 필드 편집, 저장, 삭제 |
| (모달) 시딩/리로드 | 목록 화면 내 | 파일 초기화, 캐시 동기화 |

### 3.2 정책 목록 화면

```
┌───────────────────────────────────────────────────────────────┐
│ 정책 관리                [파일에서 시딩] [변경사항 적용] ●변경중 │
│ ┌───────────────────────────────────────────────────────────┐ │
│ │ 🔍 검색 (name / 설명)                                       │ │
│ └───────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────┬───────────────┬────────────────┐ │
│ │ name                     │ 설명          │ 수정 시각       │ │
│ ├──────────────────────────┼───────────────┼────────────────┤ │
│ │ ctr-model-policy         │ CTR 모델 정책 │ 07-16 14:05    │ │
│ │  기본 heuristic-ctr-v1…  │               │            [편집]│ │
│ ├──────────────────────────┼───────────────┼────────────────┤ │
│ │ heuristic-ctr-rules      │ 휴리스틱 룰   │ 07-16 14:05    │ │
│ │  base 0.025 / max 0.25…  │               │            [편집]│ │
│ └──────────────────────────┴───────────────┴────────────────┘ │
│                              총 2개                     [+ 새 정책] │
└───────────────────────────────────────────────────────────────┘
```

- 데이터: `GET /policies`
- 컬럼: `name`, `description`(없으면 `–`), `updated_at`(로컬 타임존 포맷), 요약 미리보기(주요 키 1줄, 예: `default_model_version` 값)
- 액션:
  - 행 클릭/`[편집]` → 상세 화면
  - `[+ 새 정책]` → 상세 화면(신규 모드, `name` 입력 활성). 알려진 스키마가 없는 새 정책은 raw JSON 에디터로.
  - `[파일에서 시딩]` → 확인 모달 후 `POST /policies/seed`
  - `[변경사항 적용]` → `POST /policies/reload`, 토스트로 `loaded` 개수 표시 (§3.4 참고)

### 3.3 정책 상세/편집 화면

스키마를 아는 정책(전용 문서가 있는 정책)은 **필드 폼**, 스키마를 모르는 정책은 **raw JSON 에디터**로 렌더링한다. 두 모드를 탭으로 전환할 수 있게 하면 운영 편의성이 높다(폼 ↔ JSON). 아래는 `ctr-model-policy` 폼 예시이며, **정책별 상세 폼 구성은 [전용 문서](#2-정책별-필드-스키마-전용-문서)를 참고**한다.

```
┌───────────────────────────────────────────────────────────────┐
│ ← 목록   ctr-model-policy        [저장] [저장 후 적용] [삭제]   │
│ 설명: [ CTR 모델 선택/탐험 정책                          ]      │
│ 수정 시각: 2026-07-16 23:10 (KST)         [폼] | [JSON]        │
│ ┌───────────────────────────────────────────────────────────┐ │
│ │ 기본 모델 버전   [ heuristic-ctr-v1                      ]  │ │
│ │ 휴리스틱 접두사  [ heuristic ✕ ] [+ 추가]                  │ │
│ │ ML 오류 시 폴백  (●) 켜짐                                   │ │
│ │ 탐험 활성화      ( ) 꺼짐                                   │ │
│ │ 기본 ε          [ 0.00 ]  ─────●──  (탐험 꺼짐: 비활성)     │ │
│ │ 요청 ε 재정의    ( ) 꺼짐                (탐험 꺼짐: 비활성) │ │
│ └───────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

- 데이터: `GET /policies/{name}` (신규 모드는 스키마 기본값으로 프리필)
- 필드:
  - `name` — 신규일 때만 입력, 편집일 때는 읽기 전용(파일명 규칙 안내: 소문자+하이픈, 확장자 없음)
  - `description` — 텍스트 입력(선택)
  - `content` — 정책별 전용 문서의 스키마 기반 폼. `[JSON]` 탭에서는 모노스페이스 JSON 에디터(문법 검증 포함)
- 저장 페이로드 구성: 폼 값을 `content` 객체로 직렬화. boolean/number 타입을 정확히 맞출 것(문자열 `"true"` 금지).
- 액션:
  - `[저장]` → `PUT /policies/{name}` (신규/편집 공통). 성공 시 토스트 + 목록 갱신. DB에만 기록.
  - `[저장 후 적용]` → `PUT /policies/{name}` 성공 후 이어서 `POST /policies/reload` 호출.
  - `[삭제]` → 확인 모달 → `DELETE /policies/{name}`. "삭제해도 파일/기본값으로 계속 동작함" 안내 포함
- 저장 전 검증(클라이언트):
  - JSON 탭이면 파싱 가능 여부 확인, 객체(`{}`)인지 확인
  - `default_epsilon` 0~1 범위, `exploration_enabled=false`일 때 ε 필드 비활성
  - 알 수 없는 키가 있어도 저장은 허용하되 경고 표시(스키마 확장 대비)

### 3.4 "변경사항 적용" 버튼 문구 (카피)

`POST /policies/reload`에 매핑되는 버튼의 라벨/설명/토스트 표준안이다. `[저장 후 적용]`은 `PUT` → `reload`를 이어서 호출하므로 저장 실패 시 적용 단계로 넘어가지 않는다.

| 위치 | 상황 | 문구 |
| --- | --- | --- |
| 버튼 라벨 | 목록 화면 | `변경사항 적용` |
| 버튼 라벨 | 상세 화면 | `저장 후 적용` |
| 버튼 툴팁/보조설명 | 공통 | `저장한 정책을 실행 중인 서버에 즉시 반영합니다.` |
| 버튼 로딩 라벨 | 요청 중 | `적용 중…` |
| 토스트(성공) | reload 완료 | `정책 {loaded}개를 적용했습니다. 다음 스코어링부터 반영됩니다.` |
| 토스트(성공) | 저장 후 적용 완료 | `저장하고 적용했습니다. 다음 스코어링부터 반영됩니다.` |
| 토스트(실패) | 500 등 | `적용에 실패했습니다. 저장 내용은 유지됩니다. 잠시 후 다시 시도해 주세요.` |
| 인디케이터 | 저장했으나 미적용 | `적용되지 않은 변경사항이 있습니다.` |

- 실패 시 안내의 핵심: **저장(DB)은 이미 끝났고 적용(실행 반영)만 실패**했다는 점을 명확히 해 데이터 유실 오해를 막는다.
- "즉시"는 캐시 갱신 기준이며, 이미 진행 중인 요청에는 적용되지 않고 "다음 스코어링/어사인먼트 요청부터" 반영됨을 함께 안내한다.

### 3.5 확인 모달

| 액션 | 모달 문구 |
| --- | --- |
| 파일에서 시딩 | "docs/policies 파일 내용으로 DB를 덮어씁니다. DB에서 편집한 내용이 있으면 사라집니다. 계속할까요?" |
| 삭제 | "이 정책을 삭제하면 서비스는 파일 또는 코드 기본값을 사용합니다. 삭제할까요?" |

---

## 4. 상태/에러 처리 UX

- 로딩: 목록/상세 진입 시 스켈레톤 또는 스피너.
- 저장/삭제/시딩/리로드: 버튼 인라인 로딩 + 성공 토스트.
- 404(`policy_not_found`): 상세 화면에서 "삭제되었거나 없는 정책" 안내 후 목록으로.
- 422: `content`가 객체가 아니거나 누락된 경우. 필드/JSON 에러 메시지 표시.
- 500(`*_failed:*`): "서버/DB 오류" 토스트 + 재시도 버튼. `detail` 원문은 개발 모드에서만 노출.
- 낙관적 업데이트보다는 응답의 `updated_at`/`policy`를 신뢰해 목록을 재조회하는 편이 안전.

---

## 5. 참고 사항

- `name`은 안전하게 URL 인코딩해서 요청(`encodeURIComponent("ctr-model-policy")`).
- 저장은 즉시 서버 캐시에 반영되지만, 여러 API 인스턴스를 운용하면 각 인스턴스 캐시가 분리되므로 필요 시 각 인스턴스에 `POST /policies/reload`가 필요할 수 있다(현재 단일 인스턴스 전제).
- `CAMPAIGN_POLICY_SOURCE=file`로 뜬 서버는 DB 편집이 스코어링에 반영되지 않는다(파일 우선 모드). 운영 서버는 기본값(`db`)로 둘 것.
- 초기 시딩은 `python seed_policies.py`(DB 관리 대상: ctr-model-policy, heuristic-ctr-rules) 또는 `python seed_policies.py --all`(폴더 내 전체 `*.json`), 혹은 `POST /policies/seed`로 수행한다.
- 백엔드 저장소 구현은 [policy_store.py](../../policy_store.py), 로딩 로직은 `api._load_ctr_model_policy` / `api._load_heuristic_ctr_rules` 참고.
```
