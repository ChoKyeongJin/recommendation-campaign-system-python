# CTR 모델 정책 관리 화면 — 프론트엔드 구현 가이드

`ctr-model-policy` 정책을 DB(`campaign_policies`)에서 관리하는 화면의 전용 가이드다. 어사인먼트/스코어링이 **어떤 모델 버전으로 CTR을 계산할지**, **탐험(ε-greedy)을 할지**를 운영자가 코드 배포 없이 조정할 수 있게 한다.

- 이 문서는 `ctr-model-policy` **한 정책**에 집중한다. 공통 REST 규약·에러 UX·"변경사항 적용" 버튼 카피·목록 화면 등 정책 관리 화면 전반은 [policy_admin_frontend.md](./policy_admin_frontend.md)를 함께 참고한다.
- 기본 API 호스트: `http://localhost:8000` (배포 환경에 맞게 교체)
- 정책 식별자(`name`): `ctr-model-policy`
- 본문(`content`)은 문자열이 아니라 **JSON 객체**다 → 편집기는 키별 폼(또는 raw JSON 에디터).

---

## 1. 이 정책이 하는 일

어사인먼트/스코어링 요청이 들어오면 이 정책이 다음을 결정한다.

1. 요청에 `modelVersion`이 없으면 `default_model_version`을 사용
2. 그 모델 버전이 `heuristic_model_version_prefixes` 중 하나로 시작하면 **휴리스틱 스코어러**로, 아니면 **ML 예측 경로**로 처리
3. ML 예측 중 오류가 나면 `fallback_to_heuristic_on_ml_error`에 따라 휴리스틱으로 폴백할지 결정
4. `exploration_enabled`가 켜져 있으면 `default_epsilon` 확률로 탐험(무작위 배정), `allow_request_epsilon_override`가 켜져 있으면 개별 요청의 `epsilon`으로 그 값을 덮어씀

운영자용 설명으로 이 흐름을 화면 상단에 요약해 두면 이해를 돕는다.

---

## 2. 데이터 스키마 (`content`)

전체 예시(기본값):

```json
{
  "default_model_version": "heuristic-ctr-v1",
  "heuristic_model_version_prefixes": ["heuristic"],
  "fallback_to_heuristic_on_ml_error": true,
  "exploration_enabled": false,
  "default_epsilon": 0.0,
  "allow_request_epsilon_override": false
}
```

프론트에서 이 구조를 스키마 상수로 들고 있으면 폼 렌더링·검증·툴팁에 그대로 활용할 수 있다.

### 2.1 필드 명세

| 키 | 타입 | 기본값 | 의미 | UI 위젯 |
| --- | --- | --- | --- | --- |
| `default_model_version` | string | `"heuristic-ctr-v1"` | 요청에 `modelVersion`이 없을 때 사용할 기본 모델 버전 | 텍스트 입력 |
| `heuristic_model_version_prefixes` | string[] | `["heuristic"]` | 이 접두사로 시작하는 모델 버전은 휴리스틱 스코어러로 처리(그 외는 ML 예측 경로) | 태그(칩) 입력 |
| `fallback_to_heuristic_on_ml_error` | boolean | `true` | ML 예측 실패 시 휴리스틱 점수로 폴백할지 여부. `false`면 예측 실패가 오류로 전파됨 | 토글 |
| `exploration_enabled` | boolean | `false` | ε-greedy 탐험 활성화 여부. `false`면 탐험 확률은 항상 0 | 토글 |
| `default_epsilon` | number (0.0~1.0) | `0.0` | 탐험 확률(ε). `exploration_enabled=true`일 때만 유효 | 슬라이더/숫자 입력 |
| `allow_request_epsilon_override` | boolean | `false` | 개별 요청이 전달한 `epsilon` 값으로 `default_epsilon`을 덮어쓰도록 허용할지 | 토글 |

### 2.2 필드 간 의존 관계 (폼 로직)

- `exploration_enabled = false`이면 `default_epsilon`, `allow_request_epsilon_override`는 실효성이 없다 → 폼에서 비활성(disabled) 처리하고 안내 문구 노출.
- `default_epsilon`은 0~1 범위로 클램핑되어 저장/사용된다. UI에서도 0~1로 제한.
- `heuristic_model_version_prefixes`가 비어 있으면 모든 모델 버전이 ML 예측 경로로 간주되므로, 빈 배열 저장 시 경고(치명적 아님).

### 2.3 검증 규칙 (클라이언트)

- `default_epsilon`: `0.0 ~ 1.0`. 범위 밖이면 저장 차단(백엔드도 클램핑하지만 UX상 사전 차단).
- boolean 필드는 실제 `true/false`로 직렬화(문자열 `"true"` 금지).
- JSON 탭 저장 시 파싱 가능 + 최상위가 객체(`{}`)인지 확인.

---

## 3. API 사용 (이 정책 기준)

엔드포인트 스펙 전문은 [policy_admin_frontend.md §1](./policy_admin_frontend.md#1-api-명세). 여기서는 `ctr-model-policy`에 대입한 호출만 정리한다.

### 3.1 조회

```
GET /policies/ctr-model-policy
```

응답 `200` — §2 예시와 동일한 형태의 `{ name, content, description, updated_at }`.
없으면 `404 { "detail": "policy_not_found" }`.

### 3.2 저장 (upsert)

```
PUT /policies/ctr-model-policy
Content-Type: application/json

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

- `content`는 **JSON 객체**여야 함(배열/문자열이면 `422`).
- **전체 교체 저장이다.** 폼에서 일부 값만 바꿔도 전체 객체를 실어 보내는 것을 권장.
- 저장 즉시 실행 중인 서버 캐시가 갱신되어 다음 스코어링/어사인먼트부터 반영.

### 3.3 삭제 / 적용 / 시딩

- `DELETE /policies/ctr-model-policy` — 삭제해도 서비스는 파일(`docs/policies/ctr-model-policy.json`) → 코드 기본값으로 계속 동작.
- `POST /policies/reload` — "변경사항 적용" 버튼. DB 최신값을 실행 중 서버 캐시로 다시 로드.
- `POST /policies/seed` — `docs/policies/*.json`을 DB로 초기 시딩(위험 동작, 확인 모달 권장).

> 로딩 우선순위: **DB → 파일 → 코드 기본값**. DB에 일부 키만 있어도 나머지는 코드 기본값으로 자동 보정된다. 예측 가능성을 위해 저장은 전체 객체 단위를 권장.

---

## 4. 편집 화면 구성

`[폼] | [JSON]` 탭으로 raw JSON 편집도 지원한다.

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

- 데이터: `GET /policies/ctr-model-policy` (신규 모드는 §2 기본값으로 프리필)
- 필드 라벨은 §2 표의 "의미"를 그대로 툴팁/보조설명으로 노출하면 좋다.
- 저장 페이로드: 폼 값을 §2 구조의 `content` 객체로 직렬화. boolean/number/string[] 타입을 정확히 맞출 것.
- 액션:
  - `[저장]` → `PUT /policies/ctr-model-policy` (DB 기록)
  - `[저장 후 적용]` → `PUT` 성공 후 `POST /policies/reload`
  - `[삭제]` → 확인 모달 → `DELETE`. "삭제해도 파일/기본값으로 동작" 안내
- `exploration_enabled` 토글에 따라 `default_epsilon`/`allow_request_epsilon_override`를 실시간 활성/비활성 처리.
- 목록 화면·"변경사항 적용" 버튼 카피·상태/에러 UX는 [policy_admin_frontend.md §3~4](./policy_admin_frontend.md#3-화면-구성)와 동일.

---

## 5. 참고 사항

- `name`은 안전하게 URL 인코딩(`encodeURIComponent("ctr-model-policy")`).
- `CAMPAIGN_POLICY_SOURCE=file`로 뜬 서버는 DB 편집이 스코어링에 반영되지 않는다(파일 우선 모드). 운영 서버는 기본값(`db`).
- 초기 시딩: `python seed_policies.py`(ctr-model-policy·heuristic-ctr-rules) 또는 `POST /policies/seed`.
- 백엔드 로딩 로직은 `api._load_ctr_model_policy`, 저장소는 [policy_store.py](../../policy_store.py) 참고.
- 관련 정책: [heuristic_ctr_rules_admin_frontend.md](./heuristic_ctr_rules_admin_frontend.md) — 휴리스틱 경로일 때 점수를 만드는 룰.
```
