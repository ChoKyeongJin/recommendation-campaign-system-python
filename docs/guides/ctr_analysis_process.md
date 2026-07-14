# 클릭률 분석 프로세스와 근거

이 문서는 캠페인 자동 추천 화면의 4단계 `클릭률 예측 보기`가 어떤 데이터와 근거로 동작하는지 설명한다. 현재 구현은 운영 ML/LLM 호출 없이 `api.py`의 deterministic heuristic, PostgreSQL 저장 데이터, SQL 집계 view를 사용한다. 따라서 화면에 표시되는 값은 재현 가능해야 하며, 실제 이벤트가 부족할 때는 관측 CTR과 예측 CTR을 명확히 구분해서 보여줘야 한다.

## 1. 분석의 원칙

클릭률 분석은 세 종류의 판단을 분리한다.

| 구분      | 기준                                   | 사용 시점                                            | 화면 표현                            |
| --------- | -------------------------------------- | ---------------------------------------------------- | ------------------------------------ |
| 배정 후보 | `campaign_message_deliveries`          | 실험 생성 직후 또는 재호출 시                        | 발송 준비, 배정된 사용자 수          |
| 예측 분석 | `predicted_click_probability`          | delivered/impression/click 이벤트가 없거나 부족할 때 | `analysisBasis=predicted_assignment` |
| 관측 분석 | `v_campaign_variant_metrics`의 CTR/CVR | 이벤트가 쌓인 뒤                                     | `analysisBasis=observed_events`      |

핵심 원칙은 다음과 같다.

- `createdAssignmentCount=0`이어도 기존 배정이 있으면 발송 후보가 없는 것이 아니다. `reusedAssignmentCount`와 `assignments`를 함께 봐야 한다.
- 실제 CTR은 이벤트에서 계산한다. LLM이나 heuristic이 CTR 수치를 직접 만들지 않는다.
- 이벤트가 없을 때는 winner를 확정하지 않고, 예측 클릭 확률 기준의 임시 후보만 보여준다.
- `analysis.confidence`는 이벤트 표본이 충분하지 않으면 `low`로 둔다.
- 메시지 feature는 분석 보조 근거이며, `무료 수신거부` 같은 법정 고지는 CTA나 가격 혜택으로 보지 않는다.

## 2. 전체 흐름

```text
1. /target-sql
   자연어 프롬프트 -> 타겟 SQL -> audience/campaign context

2. /channel-messages
   캠페인 offer와 채널 정책 근거 -> LMS/RCS 메시지 3종

3. /campaign-experiments/run
   메시지 3종 -> A/B/C variant 저장 -> 사용자별 variant 배정 -> 초기 분석

4. /webhooks/message-events/{provider}
   발송사 이벤트 -> sent/delivered/impression/click/conversion 적재

5. /ai/ctr/analyze
   SQL view 집계 -> 관측 CTR/CVR 분석 또는 예측 fallback 분석
```

운영 화면에서는 3단계 메시지 추천 결과의 `source_campaign_id`를 4단계의 `campaignId`로 넘긴다. 현재 샘플 DB에서 RCS 가능한 여름 장바구니 캠페인은 `camp_001`이다. `web_rcs_campaign`처럼 DB에 없는 ID를 보내면 `campaign_channel_audience_or_user_not_found`가 발생한다.

## 3. 데이터 저장 근거

### 3.1 실험 단위: `campaign_experiments`

`/campaign-experiments/run`은 `campaignId + experimentName`으로 기존 실험을 찾는다.

- 없으면 새 `campaign_experiments` 행을 만든다.
- 있으면 기존 `experiment_id`를 재사용한다.
- 같은 이름이지만 channel이 다르면 충돌로 본다.
- variant code 3개가 기존 실험과 다르면 다른 실험으로 보아 충돌로 본다.

이 정책 때문에 같은 버튼을 다시 눌러도 새 실험이 무한히 생기지 않는다. 대신 응답에서 `experimentCreated=false`가 반환된다.

### 3.2 메시지 단위: `campaign_message_variants`

각 실험은 A/B/C 3개의 variant를 가진다.

저장되는 주요 필드:

- `variant_code`: A, B, C
- `message_name`: 화면에 보여줄 시안 이름
- `message_body`: 실제 발송 문안
- `is_control`: 대조군 여부
- `allocation_weight`: 랜덤 또는 weighted random 배정 가중치
- `ai_features`: 메시지 분석 feature

기존 실험을 재호출하면 같은 variant code의 `message_name`, `message_body`, `allocation_weight`, `is_control`, `ai_features`를 요청값 기준으로 갱신한다. 이 동작은 이전 feature 추출 오류가 있어도 같은 실험 재호출만으로 문서/화면의 feature 근거를 최신화하기 위한 것이다.

### 3.3 배정 단위: `campaign_message_deliveries`

사용자 1명에게 어떤 variant를 배정했는지 저장하는 기준 테이블이다.

중요 컬럼:

- `delivery_id`: 분석과 이벤트 연결 기준
- `experiment_id`, `variant_id`, `campaign_id`, `user_id`, `channel`
- `assignment_source`: `random`, `weighted_random`, `model`
- `model_version`: 예측 모델 버전
- `predicted_click_probability`: 배정 시점의 예측 클릭 확률
- `provider_message_id`: 발송사와 연결할 메시지 ID
- `targeting_snapshot`: 배정 당시 사용자/결정 근거
- `final_status`: `assigned`, `requested`, `sent`, `delivered`, `bounced`, `failed`, `cancelled`

`campaign_message_deliveries`에는 `UNIQUE (experiment_id, user_id)` 제약이 있다. 같은 실험에서 같은 사용자는 한 번만 배정된다. 재호출 시 이미 배정된 사용자는 새 insert를 하지 않고 기존 delivery를 `isReused=true`로 응답한다.

## 4. `/campaign-experiments/run` 응답 해석

대표 요청:

```json
{
  "campaignId": "camp_001",
  "experimentName": "장바구니 이탈 고객에게 캠페인 추천 RCS 메시지 클릭률 예측",
  "channel": "rcs",
  "primaryMetric": "ctr",
  "assignmentMethod": "model",
  "epsilon": 0.2,
  "providerMessageIdPrefix": "web-rcs-campaign",
  "userIds": ["user_001", "user_002"],
  "includeAnalysis": true,
  "variants": [
    {
      "code": "A",
      "name": "여름 장바구니(광고)",
      "messageBody": "담아두신 상품을 10% 할인 쿠폰으로 만나보세요! 무료 수신거부",
      "isControl": true
    },
    {
      "code": "B",
      "name": "(광고) 여름 혜택",
      "messageBody": "여름 장바구니 혜택이 곧 종료됩니다. 10% 할인 쿠폰을 기간 내 확인해 보세요. 무료 수신거부"
    },
    {
      "code": "C",
      "name": "여름 장바구니(광고)",
      "messageBody": "담아두신 상품을 다시 확인해보세요! 무료 수신거부"
    }
  ]
}
```

응답에서 화면이 우선 봐야 하는 필드:

| 필드                                     | 의미                                    | 화면 사용 기준                              |
| ---------------------------------------- | --------------------------------------- | ------------------------------------------- |
| `experimentId`                           | 실험 식별자                             | 이후 `/ai/ctr/analyze`, webhook 테스트 기준 |
| `experimentCreated`                      | 이번 호출에서 새 실험을 만들었는지 여부 | `false`면 기존 실험 재사용 안내             |
| `createdAssignmentCount`                 | 새로 insert한 배정 수                   | 신규 발송 후보 수                           |
| `reusedAssignmentCount`                  | 기존 delivery를 재사용한 배정 수        | 재호출 시 발송 후보 수에 포함               |
| `skippedAssignmentCount`                 | 실제로 배정 후보에서 빠진 수            | `user_not_found` 등만 포함                  |
| `assignments`                            | 신규/재사용 배정 전체                   | 발송 후보 목록                              |
| `assignments[].isReused`                 | 기존 배정 재사용 여부                   | 재호출 badge 또는 로그 표시                 |
| `assignments[].decision.candidateScores` | 사용자별 A/B/C 예측 점수                | 모델 판단 근거 tooltip                      |
| `analysis.analysisBasis`                 | 분석 근거                               | 예측/관측 구분                              |
| `analysis.primaryMetricUsed`             | winner 산정 지표                        | 카드의 기준 지표                            |
| `analysis.winner`                        | 현재 후보 variant                       | 관측 이벤트 전에는 임시 후보                |

재호출 예시 결과:

```json
{
  "experimentId": 14,
  "experimentCreated": false,
  "createdAssignmentCount": 0,
  "reusedAssignmentCount": 2,
  "skippedAssignmentCount": 0,
  "assignments": [
    { "user_id": "user_001", "variant_code": "B", "isReused": true },
    { "user_id": "user_002", "variant_code": "C", "isReused": true }
  ],
  "analysis": {
    "winner": "B",
    "analysisBasis": "predicted_assignment",
    "primaryMetricUsed": "avg_predicted_click_probability",
    "confidence": "low"
  }
}
```

해석:

- 신규 insert는 0건이지만 발송 후보는 2건이다.
- 두 사용자는 이미 실험 14에 배정되어 있으므로 기존 delivery를 재사용한다.
- 아직 이벤트가 없으므로 관측 CTR이 아니라 예측 클릭 확률 평균으로 임시 후보를 계산한다.
- `confidence=low`이므로 화면은 승자 확정이 아니라 `데이터 수집 전 예측 후보`로 표시해야 한다.

## 5. 분석 지표 선택 로직

분석 함수는 실험의 `primary_metric`과 `channel`을 기준으로 우선 지표를 정한다.

| 조건                                                  | 우선 지표             | 근거                                                                             |
| ----------------------------------------------------- | --------------------- | -------------------------------------------------------------------------------- |
| `primary_metric=ctr`, channel이 `lms`, `sms`, `kakao` | `delivered_ctr_pct`   | 문자/메신저 채널은 impression 이벤트가 불안정할 수 있어 delivered를 분모로 본다. |
| `primary_metric=ctr`, channel이 `rcs`                 | `ctr_pct`             | RCS는 impression 이벤트를 수집할 수 있으므로 impression 대비 click을 우선 본다.  |
| `primary_metric=impression_rate`                      | `impression_rate_pct` | 노출 도달 효율을 본다.                                                           |
| `primary_metric=open_rate`                            | `open_rate_pct`       | 오픈 이벤트가 있는 채널에서 사용한다.                                            |
| `primary_metric=cvr`                                  | `cvr_pct`             | 전환 성과 중심 분석에 사용한다.                                                  |
| `primary_metric=revenue`                              | `revenue_krw`         | 매출 성과 중심 분석에 사용한다.                                                  |

관측 CTR 산식:

```text
ctr_pct = unique click delivery count / unique impression delivery count * 100
delivered_ctr_pct = unique click delivery count / unique delivered delivery count * 100
cvr_pct = unique conversion delivery count / unique click delivery count * 100
```

집계는 `campaign_message_events` 원본 이벤트를 기준으로 한 SQL view가 담당한다. 이벤트 중복은 `event_key` unique 제약으로 막는다.

## 6. 예측 fallback 로직

이벤트가 없으면 `v_campaign_variant_metrics`의 `ctr_pct`, `delivered_ctr_pct`가 `null`일 수 있다. 이때 분석 함수는 다음 순서로 판단한다.

1. `primaryMetricUsed`에 해당하는 관측 지표가 있는 variant를 찾는다.
2. 관측 지표 winner가 있으면 `analysisBasis=observed_events`를 반환한다.
3. 관측 지표 winner가 없고 `avg_predicted_click_probability`가 있으면 `analysisBasis=predicted_assignment`를 반환한다.
4. 예측값도 없으면 `winner=null`을 반환한다.

`avg_predicted_click_probability`는 같은 experiment/variant에 배정된 delivery의 `predicted_click_probability` 평균이다. 이 값은 실제 CTR이 아니라 모델이 배정 시점에 예측한 클릭 가능성이다.

화면 문구 기준:

| `analysisBasis`        | 화면 상태                          | 설명                                                  |
| ---------------------- | ---------------------------------- | ----------------------------------------------------- |
| `predicted_assignment` | 발송 준비 또는 데이터 수집 전 예측 | 이벤트 전 임시 후보다. 승자 확정으로 표시하지 않는다. |
| `observed_events`      | 클릭 데이터 분석                   | 실제 이벤트 기반 성과다. 표본 수를 함께 보여준다.     |

## 7. 모델 배정 근거

현재 `assignmentMethod=model`은 `heuristic-ctr-v1` 기준으로 동작한다.

점수에 영향을 주는 근거:

- 사용자의 `preferred_channels`에 실험 channel이 있으면 가산
- 사용자 관심사와 campaign category가 맞으면 가산
- 가격 민감도가 높고 메시지에 할인율/가격 혜택이 있으면 가산
- urgency 메시지와 장바구니/딜 행동이 맞으면 가산
- personalized 메시지와 active/cart_abandoner/vip lifecycle이 맞으면 가산
- medium 길이는 소폭 가산, long 길이는 감산
- 대조군은 아주 작게 가산
- 같은 입력에서 재현 가능한 stable hash noise를 더해 동률을 줄임

`epsilon=0.2`이면 다음 정책을 쓴다.

```text
80%: 예측 점수가 가장 높은 variant 선택(exploit)
20%: allocation_weight 기준 탐색 배정(explore)
```

결정 근거는 `targeting_snapshot`과 응답의 `assignments[].decision`에 남긴다.

```json
{
  "decisionPolicy": "epsilon_greedy",
  "epsilon": 0.2,
  "selectedBy": "exploit",
  "candidateScores": {
    "A": 0.06708,
    "B": 0.0804992,
    "C": 0.0605698
  }
}
```

## 8. 메시지 feature 추출 근거

`aiFeatures`를 요청에서 직접 주지 않으면 API가 `messageBody`에서 feature를 추출한다.

| feature                | 추출 기준                                     | 주의점                                     |
| ---------------------- | --------------------------------------------- | ------------------------------------------ |
| `discount_rate`        | `10%` 같은 퍼센트 표현                        | 없으면 `null`                              |
| `urgency`              | 오늘, 마감, 종료, 기간, 곧, 마지막, 지금      | 마감성 표현 판단                           |
| `personalized`         | 고객님, 맞춤, 담아둔, 담아두신, 추천          | 개인화 표현 판단                           |
| `cta`                  | 법정 고지를 제거한 마지막 문장                | `무료 수신거부`는 CTA가 아니다.            |
| `message_length_group` | 본문 길이 기준 short/medium/long              | 길이 효과 분석에 사용                      |
| `has_price`            | 가격/할인/쿠폰/혜택/특가/포인트/무료배송 표현 | 단순 `무료 수신거부`는 가격 혜택이 아니다. |
| `has_deadline`         | urgency와 같은 마감성 표현                    | 긴급성 badge에 사용                        |

예시:

| 문구                                                          | `cta`                                          | `has_price` | `discount_rate` |
| ------------------------------------------------------------- | ---------------------------------------------- | ----------- | --------------- |
| `담아두신 상품을 10% 할인 쿠폰으로 만나보세요! 무료 수신거부` | `담아두신 상품을 10% 할인 쿠폰으로 만나보세요` | `true`      | `10`            |
| `담아두신 상품을 다시 확인해보세요! 무료 수신거부`            | `담아두신 상품을 다시 확인해보세요`            | `false`     | `null`          |

## 9. 이벤트 수집 후 관측 분석

발송 시스템이 붙으면 공급사 이벤트를 `/webhooks/message-events/{provider}`로 보낸다.

대표 click 이벤트:

```json
{
  "providerMessageId": "web-rcs-campaign-14-user_001",
  "providerEventId": "evt-click-001",
  "eventType": "click",
  "eventAt": "2026-07-14T10:30:00+09:00",
  "clickUrl": "https://example.com/campaign/camp_001?variant=B"
}
```

이벤트 적재 뒤 `/ai/ctr/analyze`를 다시 호출하면 view 집계 결과를 읽는다.

```json
{
  "experimentId": 14,
  "includeSegments": true,
  "includeDailyTrend": true,
  "generateNextMessage": true
}
```

분석 응답에서 확인할 필드:

- `variants[].assigned_count`
- `variants[].delivered_count`
- `variants[].impression_count`
- `variants[].click_count`
- `variants[].ctr_pct`
- `variants[].delivered_ctr_pct`
- `segments[]`
- `dailyTrend[]`
- `analysis.analysisBasis`
- `analysis.winner`
- `analysis.confidence`

## 10. 화면 상태 결정표

| 조건                                                                        | 화면 상태                  | 이유                                               |
| --------------------------------------------------------------------------- | -------------------------- | -------------------------------------------------- |
| `assignments.length > 0`, 이벤트 없음, `analysisBasis=predicted_assignment` | 발송 준비 / 예측 후보 표시 | 배정은 됐지만 실제 클릭 데이터는 없다.             |
| `createdAssignmentCount=0`, `reusedAssignmentCount>0`                       | 기존 배정 재사용           | 재호출이므로 발송 후보는 기존 delivery다.          |
| `skippedAssignmentCount>0`                                                  | 일부 대상 제외             | 사용자 없음, FK 오류 등 실제 제외 사유를 확인한다. |
| `analysisBasis=observed_events`, delivered/impression/click 있음            | 클릭 데이터 분석           | 실제 이벤트 기반 성과다.                           |
| `confidence=low`                                                            | 참고 지표                  | 표본이 부족하거나 이벤트가 없다.                   |
| `winner=null`                                                               | 분석 불가                  | 관측 지표와 예측값 모두 없다.                      |

## 11. 검증 명령

기존 실험 재호출 시 발송 후보와 예측 분석이 나오는지 확인한다.

```powershell
$body = '{"campaignId":"camp_001","experimentName":"장바구니 이탈 고객에게 캠페인 추천 RCS 메시지 클릭률 예측","channel":"rcs","primaryMetric":"ctr","assignmentMethod":"model","epsilon":0.2,"providerMessageIdPrefix":"web-rcs-campaign","userIds":["user_001","user_002"],"includeAnalysis":true,"variants":[{"code":"A","name":"여름 장바구니(광고)","messageBody":"담아두신 상품을 10% 할인 쿠폰으로 만나보세요! 무료 수신거부","isControl":true},{"code":"B","name":"(광고) 여름 혜택","messageBody":"여름 장바구니 혜택이 곧 종료됩니다. 10% 할인 쿠폰을 기간 내 확인해 보세요. 무료 수신거부","isControl":false},{"code":"C","name":"여름 장바구니(광고)","messageBody":"담아두신 상품을 다시 확인해보세요! 무료 수신거부","isControl":false}]}'
$content = Invoke-WebRequest -UseBasicParsing -Method Post -Uri http://localhost:8000/campaign-experiments/run `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) |
  Select-Object -ExpandProperty Content
$json = $content | ConvertFrom-Json
[pscustomobject]@{
  experimentId = $json.experimentId
  experimentCreated = $json.experimentCreated
  createdAssignmentCount = $json.createdAssignmentCount
  reusedAssignmentCount = $json.reusedAssignmentCount
  skippedAssignmentCount = $json.skippedAssignmentCount
  assignmentCount = $json.assignments.Count
  winner = $json.analysis.winner
  analysisBasis = $json.analysis.analysisBasis
  primaryMetricUsed = $json.analysis.primaryMetricUsed
} | ConvertTo-Json -Depth 5
```

기대 결과:

```json
{
  "experimentId": 14,
  "experimentCreated": false,
  "createdAssignmentCount": 0,
  "reusedAssignmentCount": 2,
  "skippedAssignmentCount": 0,
  "assignmentCount": 2,
  "winner": "B",
  "analysisBasis": "predicted_assignment",
  "primaryMetricUsed": "avg_predicted_click_probability"
}
```

분석 전용 엔드포인트도 같은 기준으로 확인한다.

```powershell
$body = '{"experimentId":14,"includeSegments":true,"includeDailyTrend":true,"generateNextMessage":true}'
$content = Invoke-WebRequest -UseBasicParsing -Method Post -Uri http://localhost:8000/ai/ctr/analyze `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) |
  Select-Object -ExpandProperty Content
$json = $content | ConvertFrom-Json
[pscustomobject]@{
  winner = $json.analysis.winner
  analysisBasis = $json.analysis.analysisBasis
  primaryMetricUsed = $json.analysis.primaryMetricUsed
  confidence = $json.analysis.confidence
  variantCount = $json.variants.Count
} | ConvertTo-Json -Depth 5
```

## 12. 장애 판단 기준

| 증상                                          | 원인                                            | 조치                                                                  |
| --------------------------------------------- | ----------------------------------------------- | --------------------------------------------------------------------- |
| `campaign_channel_audience_or_user_not_found` | `campaignId/channel` 조합 또는 user가 DB에 없음 | `campaigns`, `campaign_channels`, `users`를 확인한다.                 |
| `createdAssignmentCount=0`                    | 이미 배정된 사용자만 재호출함                   | `reusedAssignmentCount`와 `assignments[].isReused`를 확인한다.        |
| `winner=null`                                 | 관측 지표와 예측값이 모두 없음                  | model 배정 여부와 `predicted_click_probability` 저장 여부를 확인한다. |
| `analysisBasis=predicted_assignment`          | 이벤트가 아직 없음                              | 발송/webhook 이후 `/ai/ctr/analyze`를 재호출한다.                     |
| `confidence=low`                              | 표본 부족 또는 이벤트 부족                      | 승자 확정이 아니라 참고 후보로 표시한다.                              |
| CTA가 `무료 수신거부`로 보임                  | feature 추출 구버전 결과                        | 같은 실험을 재호출해 variant feature를 갱신한다.                      |
