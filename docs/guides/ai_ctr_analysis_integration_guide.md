# 타겟팅 결과·채널 메시지 3종 기반 AI 클릭률 분석 연동 가이드

## 1. 결론

현재 DDL은 A/B/C 메시지 실험과 CTR 분석을 시작하기에 충분한 구조를 이미 갖고 있다.

추천 구조는 다음과 같다.

1. PostgreSQL은 발송 배정, 이벤트 원본, CTR·CVR 집계를 담당한다.
2. AI 예측 모델은 사용자·메시지·채널 조합별 클릭 확률을 계산한다.
3. LLM은 집계 결과를 읽고 성과 원인, 세그먼트 특징, 다음 메시지 개선안을 설명한다.
4. 실시간 발송에서는 초기에는 랜덤 A/B/C를 유지하고, 데이터가 쌓이면 모델 기반 배정으로 전환한다.
5. AI의 판단 결과와 모델 버전을 반드시 DB에 기록해 나중에 재현 가능하게 만든다.

즉, **정확한 수치는 SQL**, **클릭 가능성 예측은 ML 모델**, **사람이 읽는 분석과 문구 제안은 LLM**으로 역할을 분리하는 것이 좋다.

---

## 1.1 화면 기준 연동 흐름

현재 캠페인 자동 추천 화면은 다음 4단계로 동작한다.

```text
1. 프롬프트 입력 -> 2. 타겟팅 결과 -> 3. 메시지 추천 -> 4. 클릭 분석
```

이 문서는 이 중 4단계 `클릭 분석`을 중심으로 설명한다. 앞 단계와의 연결 계약은 다음과 같다.

| 선행 화면     | 클릭 분석으로 넘기는 값                                    | 사용 위치                         |
| ------------- | ---------------------------------------------------------- | --------------------------------- |
| 프롬프트 입력 | 선택 채널 `lms` 또는 `rcs`                                 | 실험 `channel`, 분석 주 지표 선택 |
| 타겟팅 결과   | `audience_id`, `campaign_id`, 실행 SQL 요약, 세그먼트 구성 | 배정 대상과 `targeting_snapshot`  |
| 메시지 추천   | 메시지 3개, 강조 유형, 캠페인 offer 근거                   | A/B/C `campaign_message_variants` |

4단계 화면은 처음부터 승자 확정 화면이 아니다. 실험을 만들고 배정까지 끝났지만 발송/클릭 이벤트가 충분하지 않으면 `발송 준비` 또는 `클릭 데이터 수집 대기` 상태를 보여준다. 이때 분석 신뢰도는 낮게 표시하고, 현재 선택 지표만 안내한다.

화면 카드와 API 응답의 기본 매핑은 다음과 같다.

| 화면 요소                   | 데이터 기준                                                                                              |
| --------------------------- | -------------------------------------------------------------------------------------------------------- |
| 실행 ID                     | `/campaign-experiments/run`의 `experimentId`                                                             |
| 배정 후보                   | `assignments`. 신규 배정과 재사용 배정을 모두 포함한다.                                                  |
| 신규 / 재사용 / 제외        | `createdAssignmentCount` / `reusedAssignmentCount` / `skippedAssignmentCount`                            |
| 분석 신뢰도                 | `/ai/ctr/analyze` 또는 `run` 응답의 `analysis.confidence`                                                |
| 분석 근거                   | `analysis.analysisBasis`. `predicted_assignment`과 `observed_events`를 구분한다.                         |
| 선택 지표                   | `analysis.primaryMetricUsed`. 이벤트 전에는 `avg_predicted_click_probability` fallback을 사용할 수 있다. |
| 메시지 시안 목록            | `variants`                                                                                               |
| 톤/긴급/가격혜택/길이 badge | `campaign_message_variants.ai_features`                                                                  |

---

## 2. 현재 DDL에서 바로 활용할 수 있는 테이블

### `campaign_experiments`

메시지 실험의 실행 단위다.

주요 컬럼:

- `experiment_id`: 실험 식별자
- `campaign_id`: 캠페인
- `channel`: 발송 채널
- `assignment_method`: `random`, `weighted_random`, `manual`, `model`
- `primary_metric`: 기본 분석 지표. 현재 CTR 사용 가능
- `status`: 실험 상태

AI 기반 배정을 시작할 때 `assignment_method = 'model'`로 구분할 수 있다.

### `campaign_message_variants`

채널 메시지 3개를 A/B/C 변형으로 저장한다.

주요 컬럼:

- `variant_code`: A, B, C
- `message_body`: 실제 메시지 문구
- `allocation_weight`: 초기 분배 비율
- `is_control`: 기준 메시지 여부
- `ai_features`: 메시지 특징 JSON

`ai_features`에는 다음과 같은 분석용 속성을 넣는 것이 좋다.

```json
{
  "tone": "urgent",
  "urgency": true,
  "discount_rate": 10,
  "personalized": false,
  "cta": "확인하세요",
  "message_length": 42,
  "message_length_group": "medium",
  "emoji_count": 0,
  "has_price": true,
  "has_deadline": true
}
```

처음에는 운영자가 직접 넣고, 이후에는 LLM이 메시지 등록 시 자동 추출하도록 구성할 수 있다.

### `campaign_message_deliveries`

사용자별로 어떤 메시지가 배정됐는지 저장하는 핵심 테이블이다.

주요 컬럼:

- `user_id`
- `variant_id`
- `assignment_source`
- `model_version`
- `predicted_click_probability`
- `targeting_snapshot`

AI 연동에서 가장 중요한 테이블이다. 모델이 사용자에게 A/B/C 중 어떤 문구를 선택했는지, 당시 예상 클릭 확률이 얼마였는지 여기 기록한다.

예시:

```json
{
  "age_group": "20s",
  "gender": "female",
  "region": "Seoul",
  "lifecycle": "cart_abandoner",
  "price_sensitivity": "high",
  "preferred_channels": ["app_push", "kakao", "lms"],
  "interests": ["fashion", "beauty", "travel"],
  "recent_behaviors": ["cart_abandoned:fashion", "clicked:coupon"],
  "matched_target_segments": ["20s_female", "cart_abandoner", "price_sensitive"]
}
```

중요한 점은 예측할 때 사용한 사용자 정보를 `targeting_snapshot`에 복사해 두는 것이다. 사용자 프로필이 나중에 변경돼도 당시 판단 근거를 재현할 수 있다.

### `campaign_message_events`

발송 이후 실제 행동을 append-only 방식으로 저장한다.

CTR 분석에 필요한 이벤트:

- `sent`
- `delivered`
- `impression`
- `open`
- `click`
- `conversion`

`event_key`가 unique이므로 공급사 webhook 재전송에 따른 중복 클릭 집계를 막을 수 있다.

### 분석 뷰

현재 DDL의 다음 뷰를 그대로 AI 입력 데이터로 사용할 수 있다.

- `v_campaign_variant_metrics`: 메시지 A/B/C별 CTR, CVR, 매출
- `v_campaign_segment_metrics`: 성별, 연령대, 지역, 라이프사이클별 성과
- `v_campaign_daily_metrics`: 날짜별 이벤트 추이

---

## 3. 권장 전체 아키텍처

```text
[타겟팅 결과 생성]
        |
        v
[실험 및 메시지 A/B/C 등록]
        |
        v
[AI Scoring API]
 사용자 특징 + 메시지 특징 + 채널 특징
        |
        +--> 각 variant별 predicted CTR 계산
        |
        v
[메시지 배정]
 campaign_message_deliveries 저장
        |
        v
[문자/LMS/RCS 발송사]
        |
        v
[Webhook Event Collector]
 campaign_message_events 저장
        |
        v
[PostgreSQL Analytics Views]
        |
        +--> 대시보드
        +--> 모델 학습 데이터
        +--> LLM 분석 리포트
```

서비스를 다음 세 부분으로 나누는 것이 관리하기 편하다.

### 1) Campaign API

실험 생성, 메시지 3개 등록, 타겟 사용자 조회, 발송 배정을 담당한다.

### 2) Event Collector

LMS/RCS 공급사의 delivery, impression, click webhook을 받아 `campaign_message_events`에 적재한다.

### 3) AI Analysis Service

두 가지 기능을 분리한다.

- Predictive ML: 클릭 확률 예측 및 variant 선택
- Generative AI: 결과 해석, 요약, 다음 문구 제안

---

## 4. 권장 처리 순서

화면 오케스트레이션에서는 아래 단계를 하나씩 사용자에게 노출한다. 백엔드 구현은 운영 복잡도에 따라 한 번에 처리하는 API와 세부 API 호출 방식 중 하나를 선택한다.

### 화면 단계와 API 매핑

| 화면 단계         | API                                                                    | 결과                                                   |
| ----------------- | ---------------------------------------------------------------------- | ------------------------------------------------------ |
| 1. 프롬프트 입력  | `POST /target-sql`                                                     | Query Plan, 검증 SQL, DB 실행 결과, 타겟 오디언스 저장 |
| 2. 타겟팅 결과    | `GET /target-audiences/{audience_id}`                                  | 저장된 오디언스 메타데이터와 세그먼트 구성 확인        |
| 3. 메시지 추천    | `POST /channel-messages` 또는 `/target-sql`의 `generate_messages=true` | LMS/RCS 메시지 3종 생성                                |
| 4. 클릭 분석      | `POST /campaign-experiments/run`                                       | 실험 생성, variant 저장, 대상 사용자 배정, 초기 분석   |
| 4. 클릭 분석 갱신 | `POST /webhooks/message-events/{provider}`, `POST /ai/ctr/analyze`     | 이벤트 적재 후 CTR/CVR 재집계와 분석 리포트            |

## 단계 1. 메시지 3개를 A/B/C로 등록

하나의 `campaign_experiments` 행을 만들고 `campaign_message_variants`에 메시지 3개를 넣는다.

초기에는 다음 분배를 권장한다.

- A: 33.3%
- B: 33.3%
- C: 33.3%

데이터가 적은 상태에서 바로 AI가 특정 메시지를 몰아주면 편향이 생긴다. 최초 실험은 랜덤 배정으로 기준 데이터를 확보하는 것이 안전하다.

## 단계 2. 타겟 사용자별 feature 구성

DDL에 있는 데이터를 다음과 같이 합친다.

사용자 feature:

- `age`, `gender`, `region`
- `lifecycle`
- `avg_order_value_krw`
- `purchase_count_90d`
- `last_active_days`
- `price_sensitivity`
- `predicted_ltv_segment`
- interests
- preferred channels
- recent behaviors

캠페인 feature:

- objective
- category
- offer
- target segments
- keywords
- channel

메시지 feature:

- tone
- urgency
- discount rate
- CTA
- personalization 여부
- 길이
- 가격/마감/혜택 표현 여부

## 단계 3. 초기 랜덤 배정

사용자별 배정 결과를 `campaign_message_deliveries`에 저장한다.

초기 예시:

```sql
INSERT INTO campaign_message_deliveries (
    experiment_id,
    variant_id,
    campaign_id,
    user_id,
    channel,
    assignment_source,
    model_version,
    predicted_click_probability,
    targeting_snapshot,
    final_status
)
VALUES (
    :experiment_id,
    :variant_id,
    :campaign_id,
    :user_id,
    :channel,
    'random',
    NULL,
    NULL,
    :targeting_snapshot::jsonb,
    'assigned'
);
```

## 단계 4. 발송 및 이벤트 수집

발송사 응답과 webhook을 `campaign_message_events`에 저장한다.

이벤트 중복 방지를 위해 `event_key`는 공급사 이벤트 ID 또는 다음 형식으로 만든다.

```text
{provider}:{provider_message_id}:{event_type}:{provider_event_id}
```

공급사 고유 이벤트 ID가 없다면 click timestamp, URL, provider message ID를 조합해 해시를 생성한다.

## 단계 5. SQL로 CTR 계산

CTR의 기준은 채널 특성에 따라 구분해야 한다.

```text
일반 CTR = unique clicked delivery / unique impressed delivery
Delivered CTR = unique clicked delivery / unique delivered delivery
```

현재 `v_campaign_variant_metrics`는 두 값을 모두 제공하므로 적절하다.

LMS처럼 impression 이벤트가 안정적으로 오지 않는 채널은 `delivered_ctr_pct`를 주 지표로 사용한다. RCS나 앱 푸시처럼 impression 추적이 가능한 채널은 `ctr_pct`를 사용한다.

## 단계 6. AI 리포트 생성

LLM에는 원본 사용자 개인정보나 이벤트 전체를 넘기지 말고, SQL로 집계한 결과를 전달한다.

입력 예시:

```json
{
  "experiment": {
    "name": "여름 장바구니 LMS 문구 A/B/C 테스트",
    "channel": "lms",
    "primary_metric": "delivered_ctr"
  },
  "variants": [
    {
      "variant_code": "A",
      "message_name": "혜택 강조형",
      "delivered_count": 3250,
      "click_count": 143,
      "delivered_ctr_pct": 4.4,
      "conversion_count": 31
    },
    {
      "variant_code": "B",
      "message_name": "긴급성 강조형",
      "delivered_count": 3210,
      "click_count": 177,
      "delivered_ctr_pct": 5.51,
      "conversion_count": 27
    },
    {
      "variant_code": "C",
      "message_name": "개인화 친근형",
      "delivered_count": 3275,
      "click_count": 169,
      "delivered_ctr_pct": 5.16,
      "conversion_count": 39
    }
  ],
  "segments": [],
  "daily_trend": []
}
```

LLM 출력은 자유 문장이 아니라 JSON schema로 고정하는 것이 좋다.

```json
{
  "winner": "B",
  "confidence": "medium",
  "summary": "긴급성 강조형의 클릭률이 가장 높았습니다.",
  "observations": [
    "B는 전체 클릭률이 높지만 전환 수는 C보다 낮습니다.",
    "가격 민감도가 높은 사용자에서 B의 상승폭이 큽니다."
  ],
  "risks": ["표본 수와 실험 기간이 충분한지 통계 검정이 필요합니다."],
  "next_actions": [
    "B와 C를 대상으로 2차 실험을 진행합니다.",
    "B의 긴급성과 C의 개인화 표현을 결합한 D 문구를 생성합니다."
  ],
  "suggested_message": "고객님이 담아둔 여름 상품, 10% 쿠폰이 오늘 종료됩니다. 지금 확인해 보세요."
}
```

---

## 5. AI 모델은 두 단계로 도입

## 1단계: 분석 보조형

가장 먼저 적용하기 좋은 방식이다.

AI가 하는 일:

- A/B/C 결과 요약
- 어떤 세그먼트에서 어떤 문구가 잘 작동했는지 설명
- CTR과 CVR이 충돌할 때 해석
- 다음 실험 문구 제안
- 이상치 탐지

AI가 하지 않는 일:

- CTR 수치 직접 계산
- 임의로 승자 확정
- 데이터가 적은데 특정 variant에 트래픽 몰아주기

이 단계는 구현 난도가 낮고 리스크가 적다.

## 2단계: 사용자별 클릭 확률 예측

충분한 이벤트가 쌓이면 학습 데이터를 만든다.

학습 단위는 `delivery_id` 한 건이다.

label:

```text
clicked = 1 if 해당 delivery에 click 이벤트 존재
clicked = 0 otherwise
```

주요 feature:

```text
user features
campaign features
variant ai_features
channel
send hour
send weekday
historical user channel CTR
historical segment CTR
user-campaign similarity
```

초기 모델은 복잡한 딥러닝보다 다음 순서가 좋다.

1. Logistic Regression
2. LightGBM 또는 XGBoost
3. 충분한 데이터가 생기면 contextual bandit

CTR처럼 불균형한 데이터는 accuracy보다 ROC-AUC, PR-AUC, log loss, calibration을 봐야 한다.

예측 결과는 다음과 같이 저장한다.

```sql
UPDATE campaign_message_deliveries
SET
    assignment_source = 'model',
    model_version = 'ctr-lgbm-2026-07-01',
    predicted_click_probability = 0.0712453
WHERE delivery_id = :delivery_id;
```

실제로는 배정 전에 variant별 예측값을 계산하고 가장 높은 variant를 선택한다.

```json
{
  "user_id": "user_001",
  "experiment_id": 1,
  "scores": [
    { "variant_code": "A", "click_probability": 0.0412 },
    { "variant_code": "B", "click_probability": 0.0673 },
    { "variant_code": "C", "click_probability": 0.0591 }
  ],
  "selected_variant": "B",
  "model_version": "ctr-lgbm-2026-07-01"
}
```

---

## 6. 모델 배정 시 반드시 exploration 유지

예측 확률이 가장 높은 문구만 계속 보내면 새로운 메시지의 성능을 학습할 수 없고 모델 편향이 강화된다.

권장 정책:

```text
80%: 모델이 가장 높은 점수를 준 variant
20%: 랜덤 exploration
```

또는 softmax/epsilon-greedy 방식을 사용한다.

배정 출처는 명확히 기록한다.

- 모델 선택: `assignment_source = 'model'`
- 탐색 랜덤: `assignment_source = 'weighted_random'`

가능하면 `targeting_snapshot`에 다음 필드를 추가한다.

```json
{
  "decision_policy": "epsilon_greedy",
  "epsilon": 0.2,
  "selected_by": "exploit",
  "candidate_scores": {
    "A": 0.0412,
    "B": 0.0673,
    "C": 0.0591
  }
}
```

---

## 7. 권장 API 설계

## 실험 생성

```http
POST /api/campaign-experiments
```

```json
{
  "campaignId": "camp_001",
  "experimentName": "여름 장바구니 메시지 3종 테스트",
  "channel": "lms",
  "primaryMetric": "ctr",
  "variants": [
    {
      "code": "A",
      "name": "혜택 강조형",
      "messageBody": "...",
      "isControl": true
    },
    {
      "code": "B",
      "name": "긴급성 강조형",
      "messageBody": "..."
    },
    {
      "code": "C",
      "name": "개인화 친근형",
      "messageBody": "..."
    }
  ]
}
```

메시지 등록 시 LLM feature extractor를 호출해 `ai_features`를 채운다.

## 사용자별 variant 점수 계산

```http
POST /api/ai/ctr/score
```

```json
{
  "experimentId": 1,
  "userIds": ["user_001", "user_002", "user_003"]
}
```

응답:

```json
{
  "modelVersion": "ctr-lgbm-2026-07-01",
  "results": [
    {
      "userId": "user_001",
      "selectedVariantCode": "B",
      "predictedClickProbability": 0.0673,
      "scores": {
        "A": 0.0412,
        "B": 0.0673,
        "C": 0.0591
      }
    }
  ]
}
```

## 배정 및 발송 생성

```http
POST /api/campaign-experiments/{experimentId}/assignments
```

이 API는 한 transaction에서 다음을 처리한다.

1. 동일 실험·사용자 중복 여부 확인
2. 모델 또는 랜덤 variant 선택
3. `campaign_message_deliveries` 저장
4. 발송 큐에 publish

## 이벤트 webhook

```http
POST /api/webhooks/message-events/{provider}
```

공급사 이벤트를 내부 표준 이벤트로 변환한다.

```json
{
  "providerMessageId": "lms-camp001-0001",
  "providerEventId": "evt-123",
  "eventType": "click",
  "eventAt": "2026-07-15T10:31:00+09:00",
  "clickUrl": "https://example.com/campaign/camp_001?variant=B"
}
```

## AI 분석 리포트

```http
POST /api/ai/ctr/analyze
```

```json
{
  "experimentId": 1,
  "includeSegments": true,
  "includeDailyTrend": true,
  "generateNextMessage": true
}
```

### 현재 프로토타입 구현 상태

`api.py`에는 위 설계를 기준으로 다음 엔드포인트가 구현되어 있다. 기존 API 스타일과 맞추기 위해 `/api/...` 경로와 prefix 없는 경로를 모두 제공한다.

| 기능               | 구현 경로                                                                                                            | 구현 내용                                                                                                                                                                                                                                                                                                                             |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 실험 시작          | `POST /api/campaign-experiments/run`, `POST /campaign-experiments/run`                                               | 웹 버튼 하나에서 호출하는 오케스트레이션 API다. 실험 생성, A/B/C variant 저장, 대상 사용자 배정, 선택적 초기 분석을 한 transaction으로 처리하고 `ready_to_send` 상태를 반환한다. 같은 `campaignId`와 `experimentName`으로 재호출하면 기존 실험과 기존 배정을 재사용하고 `reusedAssignmentCount`, `assignments[].isReused`로 표시한다. |
| 실험 생성          | `POST /api/campaign-experiments`, `POST /campaign-experiments`                                                       | 실험 1개와 A/B/C variant 3개를 한 transaction으로 생성한다. `aiFeatures`가 없으면 메시지 본문에서 긴급성, 혜택, 개인화, 길이 같은 feature를 로컬 규칙으로 추출한다.                                                                                                                                                                   |
| 사용자별 점수 계산 | `POST /api/ai/ctr/score`, `POST /ai/ctr/score`                                                                       | `heuristic-ctr-v1` 점수기로 사용자 feature, 메시지 feature, 채널 선호도 기반 predicted CTR을 계산한다. 실제 ML 모델이 붙기 전 재현 가능한 baseline이다.                                                                                                                                                                               |
| 배정 생성          | `POST /api/campaign-experiments/{experimentId}/assignments`, `POST /campaign-experiments/{experimentId}/assignments` | `userIds` 또는 `audienceId`를 받아 중복 배정을 건너뛰고 `campaign_message_deliveries`에 저장한다. `assignmentMethod=model`이면 epsilon-greedy 정책과 candidate score를 `targeting_snapshot`에 남긴다.                                                                                                                                 |
| 이벤트 수집        | `POST /api/webhooks/message-events/{provider}`, `POST /webhooks/message-events/{provider}`                           | 공급사 이벤트를 표준 event type으로 변환하고 `event_key` 기준으로 중복 insert를 차단한다. `sent`, `delivered`, `bounce`, `failed` 이벤트는 delivery 상태도 갱신한다.                                                                                                                                                                  |
| CTR 분석           | `POST /api/ai/ctr/analyze`, `POST /ai/ctr/analyze`                                                                   | 분석 view를 읽어 variant, segment, daily trend를 반환하고 고정 JSON schema의 요약 리포트를 만든다. LMS/SMS/Kakao처럼 impression이 불안정한 채널은 `delivered_ctr_pct`를 우선 사용한다. 이벤트 지표가 없으면 `avg_predicted_click_probability` 기반 `analysisBasis=predicted_assignment`로 임시 후보를 반환한다.                       |

현재 구현은 운영용 ML/LLM 서비스를 호출하지 않는다. 모델 배정은 deterministic heuristic baseline이며, 분석 리포트도 SQL 집계 결과 기반의 규칙형 JSON이다. 따라서 다음 단계에서 실제 모델 서버나 LLM JSON schema 호출을 붙이더라도 DB 기록 형식과 API 계약은 유지할 수 있다.

클릭 분석 화면의 세부 프로세스, 산식, 재호출 처리, 예측 fallback 근거는 `docs/guides/ctr_analysis_process.md`를 기준으로 한다.

---

## 8. 분석용 SQL

## A/B/C 전체 성과

```sql
SELECT
    variant_code,
    message_name,
    assigned_count,
    delivered_count,
    impression_count,
    click_count,
    conversion_count,
    ctr_pct,
    delivered_ctr_pct,
    cvr_pct,
    revenue_krw
FROM v_campaign_variant_metrics
WHERE experiment_id = :experiment_id
ORDER BY delivered_ctr_pct DESC NULLS LAST;
```

## 최소 표본 기준 포함

```sql
SELECT *
FROM v_campaign_variant_metrics
WHERE experiment_id = :experiment_id
  AND delivered_count >= 1000
ORDER BY delivered_ctr_pct DESC NULLS LAST;
```

작은 표본에서 단순히 CTR 1위만 winner로 표시하지 않도록 최소 표본 기준을 둔다.

## 세그먼트별 차이

```sql
SELECT
    variant_code,
    gender,
    age_group,
    region,
    lifecycle,
    impression_count,
    click_count,
    ctr_pct
FROM v_campaign_segment_metrics
WHERE experiment_id = :experiment_id
  AND impression_count >= 100
ORDER BY ctr_pct DESC NULLS LAST;
```

## 모델 예측값과 실제 클릭 비교

```sql
WITH actual AS (
    SELECT
        d.delivery_id,
        d.model_version,
        d.predicted_click_probability,
        CASE
            WHEN BOOL_OR(e.event_type = 'click') THEN 1
            ELSE 0
        END AS clicked
    FROM campaign_message_deliveries d
    LEFT JOIN campaign_message_events e
        ON e.delivery_id = d.delivery_id
    WHERE d.experiment_id = :experiment_id
      AND d.predicted_click_probability IS NOT NULL
    GROUP BY
        d.delivery_id,
        d.model_version,
        d.predicted_click_probability
)
SELECT
    model_version,
    COUNT(*) AS prediction_count,
    ROUND(AVG(predicted_click_probability), 6) AS avg_predicted_ctr,
    ROUND(AVG(clicked), 6) AS actual_ctr,
    ROUND(
        AVG(POWER(predicted_click_probability - clicked, 2)),
        6
    ) AS brier_score
FROM actual
GROUP BY model_version;
```

Brier score가 낮을수록 예측 확률 calibration이 좋다.

## 시간대별 CTR

```sql
WITH delivery_flags AS (
    SELECT
        d.delivery_id,
        d.variant_id,
        EXTRACT(HOUR FROM d.sent_at AT TIME ZONE 'Asia/Seoul') AS send_hour,
        BOOL_OR(e.event_type = 'delivered') AS delivered,
        BOOL_OR(e.event_type = 'click') AS clicked
    FROM campaign_message_deliveries d
    LEFT JOIN campaign_message_events e
        ON e.delivery_id = d.delivery_id
    WHERE d.experiment_id = :experiment_id
    GROUP BY d.delivery_id, d.variant_id, send_hour
)
SELECT
    send_hour,
    COUNT(*) FILTER (WHERE delivered) AS delivered_count,
    COUNT(*) FILTER (WHERE clicked) AS click_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE clicked)
        / NULLIF(COUNT(*) FILTER (WHERE delivered), 0),
        4
    ) AS delivered_ctr_pct
FROM delivery_flags
GROUP BY send_hour
ORDER BY send_hour;
```

---

## 9. DDL에 추가하면 좋은 테이블

현재 구조만으로도 시작 가능하지만, 운영 단계에서는 AI 실행 이력을 별도 저장하는 편이 좋다.

```sql
CREATE TABLE campaign_ai_analysis_runs (
    analysis_run_id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL
        REFERENCES campaign_experiments(experiment_id) ON DELETE CASCADE,
    analysis_type VARCHAR(30) NOT NULL
        CHECK (analysis_type IN (
            'performance_summary',
            'segment_analysis',
            'message_suggestion',
            'anomaly_detection'
        )),
    model_provider VARCHAR(50) NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    prompt_version VARCHAR(50) NOT NULL,
    input_snapshot JSONB NOT NULL,
    output_result JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_by VARCHAR(100),
    approved_at TIMESTAMPTZ
);

CREATE INDEX idx_campaign_ai_analysis_runs_experiment
    ON campaign_ai_analysis_runs(experiment_id, generated_at DESC);
```

이 테이블이 필요한 이유:

- 같은 데이터에서 어떤 프롬프트와 모델이 어떤 결론을 냈는지 확인 가능
- AI 결과 변경 이력 관리 가능
- 잘못된 추천이 실제 발송에 사용됐는지 감사 가능
- 프롬프트 버전별 품질 비교 가능

모델별 candidate score를 정규화해서 보관하려면 다음 테이블도 고려할 수 있다.

```sql
CREATE TABLE campaign_delivery_model_scores (
    delivery_id BIGINT NOT NULL
        REFERENCES campaign_message_deliveries(delivery_id) ON DELETE CASCADE,
    variant_id BIGINT NOT NULL
        REFERENCES campaign_message_variants(variant_id) ON DELETE CASCADE,
    model_version VARCHAR(100) NOT NULL,
    predicted_click_probability NUMERIC(8,7) NOT NULL
        CHECK (predicted_click_probability BETWEEN 0 AND 1),
    score_rank INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (delivery_id, variant_id, model_version)
);
```

현재 `campaign_message_deliveries.predicted_click_probability`에는 선택된 variant의 점수만 저장된다. A/B/C 후보 전체 점수를 비교하거나 모델을 재평가하려면 별도 score 테이블이 유용하다.

---

## 10. 통계적으로 주의할 부분

### CTR 1위가 바로 승자는 아니다

표본 수가 적으면 우연히 높게 나올 수 있다. 최소한 다음을 같이 확인한다.

- variant별 delivered 또는 impression 수
- 절대 클릭 수
- 신뢰구간
- control 대비 uplift
- p-value 또는 Bayesian probability
- 실험 기간

### 다중 세그먼트 탐색 문제

성별, 나이, 지역, 라이프사이클을 많이 나누면 우연히 높은 세그먼트가 생긴다. 세그먼트 분석은 설명용으로 사용하고, 다음 실험에서 재검증해야 한다.

### CTR만 최적화하면 전환이 떨어질 수 있다

자극적인 문구는 클릭은 높지만 구매가 낮을 수 있다.

운영 판단은 다음 우선순위를 권장한다.

```text
1차: delivered CTR 또는 impression CTR
2차: click-to-conversion rate
3차: 사용자당 매출
4차: unsubscribe / complaint
```

장기적으로는 클릭 확률보다 기대 매출을 최적화할 수도 있다.

```text
expected_value = P(click) × P(conversion | click) × expected_order_value
```

---

## 11. LLM 프롬프트 권장안

```text
역할:
너는 CRM 캠페인 실험 분석가다.

규칙:
1. 제공된 집계값만 사용한다.
2. CTR을 직접 재계산하지 않는다.
3. 표본이 부족하면 승자를 확정하지 않는다.
4. CTR과 CVR이 충돌하면 둘을 분리해 설명한다.
5. 개인정보를 추론하지 않는다.
6. 결과는 지정된 JSON schema로 출력한다.

분석 요청:
- 전체 winner 후보
- control 대비 uplift
- 세그먼트별 차이
- 일별 추세와 이상치
- 통계적 위험
- 다음 실험 제안
- 신규 메시지 1개 제안
```

LLM 입력에는 집계 데이터와 메시지 특징만 넣는다. `user_id`, 전화번호, IP, user-agent 원문은 전달하지 않는다.

---

## 12. 구현 우선순위

### 1차 구현

- 메시지 3개를 `campaign_message_variants`에 저장
- 타겟 사용자를 랜덤 A/B/C 배정
- delivery/event webhook 적재
- 기존 view로 CTR 대시보드 구현
- LLM으로 실험 결과 요약

### 2차 구현

- 메시지 `ai_features` 자동 추출
- 세그먼트별 LLM 분석
- 통계 검정 및 confidence 표시
- AI 분석 결과 이력 테이블 추가

### 3차 구현

- 클릭 예측 모델 학습
- `model_version`, `predicted_click_probability` 저장
- 80% 모델 선택 + 20% exploration
- 모델 calibration 및 drift 모니터링

---

## 13. 추천 기술 조합

웹 애플리케이션 기준 예시:

```text
Backend: Spring Boot / Node.js / FastAPI
DB: PostgreSQL
Queue: Kafka, RabbitMQ 또는 Redis Streams
Scheduler: Airflow, Dagster 또는 Spring Batch
ML Serving: FastAPI + LightGBM/XGBoost
LLM: JSON schema 출력이 가능한 API
Dashboard: 기존 관리자 페이지 + SQL 집계 API
```

트래픽이 크지 않다면 처음부터 Kafka까지 도입할 필요는 없다.

초기에는 다음 정도로 충분하다.

```text
Application -> PostgreSQL
Application -> 발송사 API
발송사 webhook -> Application -> PostgreSQL
Batch job -> 분석 view 조회 -> LLM API -> 분석 결과 저장
```

---

## 14. 최종 권장안

현재 상황에서는 다음 방식이 가장 현실적이다.

1. 채널 메시지 3개를 하나의 experiment의 A/B/C variant로 저장한다.
2. 타겟팅된 다수 사용자에게 우선 균등 랜덤 배정한다.
3. 모든 배정은 `campaign_message_deliveries`에 먼저 저장한다.
4. 공급사 webhook은 `campaign_message_events`에 중복 없이 적재한다.
5. 실제 CTR은 기존 분석 view에서 계산한다.
6. LLM은 view 결과를 JSON으로 받아 원인 분석과 다음 메시지 제안만 수행한다.
7. 최소 수천 건 이상의 안정된 데이터가 쌓인 뒤 클릭 예측 모델을 붙인다.
8. 모델 배정 시에도 10~20% 랜덤 탐색 트래픽을 남긴다.
9. `model_version`, 예측 확률, 타겟 snapshot, AI 분석 이력을 모두 저장한다.
10. 최종 목표는 단순 최고 CTR보다 CTR·CVR·매출·수신 거부를 함께 최적화하는 것이다.

이 방식이면 현재 DDL을 크게 갈아엎지 않고도 분석 보조형 AI부터 개인화 배정 모델까지 단계적으로 확장할 수 있다.
