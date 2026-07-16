# 휴리스틱 CTR 룰 관리 화면 — 프론트엔드 구현 가이드

`heuristic-ctr-rules` 정책을 DB(`campaign_policies`)에서 관리하는 화면의 전용 가이드다. 휴리스틱 CTR 스코어러가 변형별 예측 CTR을 계산할 때 사용하는 값들을 운영자가 코드 배포 없이 조정할 수 있게 한다.

- 이 문서는 `heuristic-ctr-rules` **한 정책**에 집중한다. 공통 REST 규약·에러 UX·"변경사항 적용" 버튼 카피 등 정책 관리 화면 전반은 [policy_admin_frontend.md](./policy_admin_frontend.md)를 함께 참고한다.
- 기본 API 호스트: `http://localhost:8000` (배포 환경에 맞게 교체)
- 정책 식별자(`name`): `heuristic-ctr-rules`
- 본문(`content`)은 문자열이 아니라 **중첩 JSON 객체**다 → 폼은 섹션으로 나누고 raw JSON 탭을 함께 제공한다.

---

## 1. 이 정책이 하는 일

휴리스틱 스코어러는 다음 순서로 변형(variant)의 예측 CTR을 만든다.

1. `base_probability`에서 시작
2. `score_adjustments`의 각 조건이 충족되면 해당 가감치를 더함(음수면 감점)
3. user·variant 조합으로 결정되는 안정적 노이즈(`stable_noise_max` 이하)를 더함
4. 최종값을 `min_probability` ~ `max_probability`로 클램핑

즉 이 화면에서 바꾸는 값이 **어떤 유저에게 어떤 변형이 우선 노출되는지**에 직접 영향을 준다. 운영자용 설명 문구로 이 4단계를 화면 상단에 요약해 두면 이해를 돕는다.

---

## 2. 데이터 스키마 (`content`)

전체 예시(기본값):

```json
{
  "base_probability": 0.025,
  "min_probability": 0.001,
  "max_probability": 0.25,
  "stable_noise_max": 0.01,
  "score_adjustments": {
    "preferred_channel": 0.012,
    "campaign_category_interest_match": 0.01,
    "high_price_sensitivity_with_price_offer": 0.012,
    "urgency_with_recent_behavior": 0.009,
    "personalized_lifecycle_match": 0.006,
    "message_length_medium": 0.004,
    "message_length_long": -0.004,
    "control_variant": 0.001
  },
  "matchers": {
    "urgency_recent_behavior_keywords": ["cart_abandoned", "deal"],
    "personalized_lifecycles": ["active", "cart_abandoner", "vip"]
  }
}
```

프론트에서 이 구조를 스키마 상수로 들고 있으면 폼 렌더링·검증·툴팁에 그대로 활용할 수 있다.

### 2.1 최상위 확률 파라미터

| 키 | 타입 | 기본값 | 의미 | UI 위젯 |
| --- | --- | --- | --- | --- |
| `base_probability` | number (0~1) | `0.025` | 모든 변형의 기준 예측 CTR. 여기서 조정값이 가감된다 | 숫자 입력 |
| `min_probability` | number (0~1) | `0.001` | 최종 확률 하한(클램핑) | 숫자 입력 |
| `max_probability` | number (0~1) | `0.25` | 최종 확률 상한(클램핑) | 숫자 입력 |
| `stable_noise_max` | number (0~1) | `0.01` | user·variant 조합으로 결정되는 안정적 노이즈의 최대치 | 숫자 입력 |

### 2.2 `score_adjustments` (조건 충족 시 확률 가감치, 단위: 절대 확률)

| 키 | 기본값 | 조건 |
| --- | --- | --- |
| `preferred_channel` | `0.012` | 사용자의 선호 채널과 변형 채널이 일치 |
| `campaign_category_interest_match` | `0.01` | 캠페인 카테고리가 사용자 관심사와 일치 |
| `high_price_sensitivity_with_price_offer` | `0.012` | 가격 민감 사용자 + 가격 혜택 메시지 |
| `urgency_with_recent_behavior` | `0.009` | 긴급성 문구 + `matchers.urgency_recent_behavior_keywords`에 해당하는 최근 행동 |
| `personalized_lifecycle_match` | `0.006` | `matchers.personalized_lifecycles`에 속하는 라이프사이클 개인화 |
| `message_length_medium` | `0.004` | 메시지 길이가 중간 |
| `message_length_long` | `-0.004` | 메시지 길이가 김(감점) |
| `control_variant` | `0.001` | 대조군(control) 변형 |

- 값은 **음수 허용**(감점). UI는 부호 포함 숫자 입력.
- 8개 키는 고정 집합이다. 스코어러가 참조하지 않는 임의 키를 추가해도 무시되므로, 폼에서는 위 8개만 노출하고 raw JSON 탭에서만 자유 편집을 허용(미사용 키는 경고).

### 2.3 `matchers` (조건 판정에 쓰이는 키워드/집합)

| 키 | 타입 | 기본값 | 의미 | UI 위젯 |
| --- | --- | --- | --- | --- |
| `urgency_recent_behavior_keywords` | string[] | `["cart_abandoned", "deal"]` | `urgency_with_recent_behavior` 가점을 트리거하는 최근 행동 키워드 | 태그(칩) 입력 |
| `personalized_lifecycles` | string[] | `["active", "cart_abandoner", "vip"]` | `personalized_lifecycle_match` 가점 대상 라이프사이클 | 태그(칩) 입력 |

### 2.4 검증 규칙 (클라이언트)

- 모든 확률 값은 `0.0 ~ 1.0` 범위. `score_adjustments`는 음수 허용이지만 절댓값이 과도하면(예: `|v| > max_probability`) 경고.
- 논리적 권장: `min_probability ≤ base_probability ≤ max_probability`. 위반 시 경고(치명적 아님, 백엔드가 최종 클램핑함).
- JSON 탭 저장 시 파싱 가능 + 최상위가 객체(`{}`)인지 확인.

---

## 3. API 사용 (이 정책 기준)

엔드포인트 스펙 전문은 [policy_admin_frontend.md §1](./policy_admin_frontend.md#1-api-명세). 여기서는 `heuristic-ctr-rules`에 대입한 호출만 정리한다.

### 3.1 조회

```
GET /policies/heuristic-ctr-rules
```

응답 `200` — §2 예시와 동일한 형태의 `{ name, content, description, updated_at }`.
없으면 `404 { "detail": "policy_not_found" }`.

### 3.2 저장 (upsert)

```
PUT /policies/heuristic-ctr-rules
Content-Type: application/json

{
  "content": { /* §2 전체 객체 */ },
  "description": "휴리스틱 CTR 룰 (선택)"
}
```

- `content`는 **JSON 객체**여야 함(배열/문자열이면 `422`).
- **전체 교체 저장이다.** 폼에서 일부 값만 바꿔도 전체 객체를 실어 보내는 것을 권장.
- 저장 즉시 실행 중인 서버 캐시가 갱신되어 다음 스코어링부터 반영.

### 3.3 삭제 / 적용 / 시딩

- `DELETE /policies/heuristic-ctr-rules` — 삭제해도 서비스는 파일(`docs/policies/heuristic-ctr-rules.json`) → 코드 기본값으로 계속 동작.
- `POST /policies/reload` — "변경사항 적용" 버튼. DB 최신값을 실행 중 서버 캐시로 다시 로드.
- `POST /policies/seed` — `docs/policies/*.json`을 DB로 초기 시딩(위험 동작, 확인 모달 권장).

> 로딩 우선순위: **DB → 파일 → 코드 기본값**. DB에 일부 키만 있어도 나머지는 코드 기본값과 **한 단계 중첩까지 병합**되어 보정된다(예: `score_adjustments`에서 한 항목만 저장해도 나머지 항목은 기본값 유지). 예측 가능성을 위해 저장은 전체 객체 단위를 권장.

---

## 4. 편집 화면 구성

중첩 구조라 폼을 3개 섹션으로 나눈다. `[폼] | [JSON]` 탭으로 raw JSON 편집도 지원한다.

```
┌───────────────────────────────────────────────────────────────┐
│ ← 목록   heuristic-ctr-rules       [저장] [저장 후 적용] [삭제] │
│ 설명: [ 휴리스틱 CTR 룰                                  ]      │
│ 수정 시각: 2026-07-16 23:10 (KST)         [폼] | [JSON]        │
│ ┌── 확률 파라미터 ──────────────────────────────────────────┐ │
│ │ 기준 확률  [ 0.025 ]   최소 [ 0.001 ]   최대 [ 0.25 ]     │ │
│ │ 노이즈 최대 [ 0.01 ]                                       │ │
│ └───────────────────────────────────────────────────────────┘ │
│ ┌── 점수 조정 (score_adjustments) ──────────────────────────┐ │
│ │ 선호 채널 일치            [ +0.012 ]                       │ │
│ │ 카테고리 관심 일치        [ +0.010 ]                       │ │
│ │ 가격민감 + 가격혜택       [ +0.012 ]                       │ │
│ │ 긴급성 + 최근행동         [ +0.009 ]                       │ │
│ │ 라이프사이클 개인화       [ +0.006 ]                       │ │
│ │ 메시지 길이(중)           [ +0.004 ]                       │ │
│ │ 메시지 길이(장)           [ -0.004 ]                       │ │
│ │ 대조군                    [ +0.001 ]                       │ │
│ └───────────────────────────────────────────────────────────┘ │
│ ┌── 매처 (matchers) ────────────────────────────────────────┐ │
│ │ 긴급 최근행동 키워드  [ cart_abandoned ✕ ] [ deal ✕ ] [+]  │ │
│ │ 개인화 라이프사이클   [ active ✕ ][ cart_abandoner ✕ ]…[+] │ │
│ └───────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

- 데이터: `GET /policies/heuristic-ctr-rules` (신규 모드는 §2 기본값으로 프리필)
- 필드 라벨은 §2 표의 "의미"를 그대로 툴팁/보조설명으로 노출하면 좋다.
- 저장 페이로드: 폼 값을 §2 구조의 `content` 객체로 직렬화. number/string[] 타입을 정확히 맞출 것(문자열 `"0.025"` 금지 → 숫자 `0.025`).
- 액션:
  - `[저장]` → `PUT /policies/heuristic-ctr-rules` (DB 기록)
  - `[저장 후 적용]` → `PUT` 성공 후 `POST /policies/reload`
  - `[삭제]` → 확인 모달 → `DELETE`. "삭제해도 파일/기본값으로 동작" 안내
- 목록 화면·"변경사항 적용" 버튼 카피·상태/에러 UX는 [policy_admin_frontend.md §3~4](./policy_admin_frontend.md#3-화면-구성)와 동일.

---

## 5. 참고 사항

- `name`은 안전하게 URL 인코딩(`encodeURIComponent("heuristic-ctr-rules")`).
- `CAMPAIGN_POLICY_SOURCE=file`로 뜬 서버는 DB 편집이 스코어링에 반영되지 않는다(파일 우선 모드). 운영 서버는 기본값(`db`).
- 초기 시딩: `python seed_policies.py`(ctr-model-policy·heuristic-ctr-rules) 또는 `POST /policies/seed`.
- 백엔드 로딩 로직은 `api._load_heuristic_ctr_rules`, 저장소는 [policy_store.py](../../policy_store.py) 참고.
```
