# 현재 프로젝트 기준 LightGBM CTR 예측 확장 가이드

## 0. 문서 목적

이 문서는 현재 저장소의 캠페인 RAG/GraphRAG 데이터 구조와 CTR 실험 로그를 기준으로, LightGBM 기반 클릭 확률 예측 기능을 붙이는 방법을 정리한다.

현재 프로젝트는 별도의 CTR 전용 서브프로젝트가 아니라 다음 자산을 중심으로 동작한다.

```text
api.py
graph_rag.py
rag_index.py
build_rag_knowledge.py
docs/data/local_bootstrap.sql
docs/data/campaign_user_rag_sample_50_with_edges.json
docs/data/schema_catalog.json
docs/data/rag_knowledge_base.json
docs/prompts/*
docs/policies/message-policy.json
docker-compose.yml
requirements.txt
```

현재 API에는 이미 A/B/C 실험, 사용자 배정, 메시지 이벤트 수집, CTR 분석 흐름이 있다. 다만 CTR 점수화는 아직 LightGBM 모델이 아니라 `heuristic-ctr-v1` 규칙 기반 baseline이다.

LightGBM 확장의 목표는 다음 조합의 클릭 확률을 예측하는 것이다.

```text
사용자 x 캠페인 x 메시지 variant x 채널 x 발송 시점
-> click probability
```

예상 응답 형태는 기존 `/ai/ctr/score` 응답을 유지한다.

```json
{
  "userId": "user_001",
  "selectedVariantCode": "B",
  "predictedClickProbability": 0.0712384,
  "scores": {
    "A": 0.0531021,
    "B": 0.0712384,
    "C": 0.0479942
  }
}
```

---

## 1. 현재 데이터 구조 요약

### 1.1 샘플 원천

`docs/data/campaign_user_rag_sample_50_with_edges.json`는 RAG 색인과 DDL 샘플 데이터의 원천이다.

주요 노드 타입은 다음과 같다.

| JSON 타입  | DB 매핑                | 설명                                                           |
| ---------- | ---------------------- | -------------------------------------------------------------- |
| `campaign` | `campaigns`            | 캠페인 목적, 카테고리, 예산, 기간, 예상 CTR/CVR, 임베딩 텍스트 |
| `user`     | `users`                | 사용자 인구통계, 라이프사이클, 구매/활동 지표, LTV 세그먼트    |
| edge       | `recommendation_edges` | 사용자와 캠페인 추천 관계, 추천 이유, label                    |

캠페인의 다중 값은 별도 테이블로 분리되어 있다.

| 값            | 테이블                     |
| ------------- | -------------------------- |
| 채널          | `campaign_channels`        |
| 타겟 세그먼트 | `campaign_target_segments` |
| 키워드        | `campaign_keywords`        |

사용자 다중 값도 별도 테이블로 분리된다.

| 값        | 테이블                    |
| --------- | ------------------------- |
| 관심사    | `user_interests`          |
| 선호 채널 | `user_preferred_channels` |
| 최근 행동 | `user_recent_behaviors`   |

### 1.2 CTR 실험/발송/이벤트 테이블

`docs/data/local_bootstrap.sql`에는 CTR 분석 확장 테이블이 이미 포함되어 있다.

| 테이블                        | 역할                                                                     |
| ----------------------------- | ------------------------------------------------------------------------ |
| `campaign_experiments`        | 캠페인별 A/B/C 실험 실행 단위                                            |
| `campaign_message_variants`   | 실험 안의 메시지 A/B/C variant                                           |
| `campaign_message_deliveries` | 사용자별 variant 배정과 발송 사실                                        |
| `campaign_message_events`     | sent, delivered, impression, open, click, conversion 등 원본 이벤트 로그 |
| `v_campaign_variant_metrics`  | variant별 퍼널 성과 view                                                 |
| `v_campaign_segment_metrics`  | 성별, 연령대, 지역, lifecycle별 성과 view                                |
| `v_campaign_daily_metrics`    | 날짜별 이벤트 추이 view                                                  |

학습 데이터의 기준 행은 `campaign_message_deliveries.delivery_id`다. 클릭 여부는 `campaign_message_events`를 집계해 발송 1건당 0 또는 1로 만든다.

---

## 2. 현재 API 흐름

`api.py`는 CTR 실험과 분석에 필요한 엔드포인트를 제공한다. `/api/...` prefix가 있는 경로도 함께 열려 있다.

| 엔드포인트                                    | 역할                                                    |
| --------------------------------------------- | ------------------------------------------------------- |
| `POST /campaign-experiments/run`              | 메시지 3종을 실험으로 만들고 사용자 배정을 한 번에 수행 |
| `POST /campaign-experiments`                  | 실험과 variant 등록                                     |
| `POST /ai/ctr/score`                          | 사용자별 variant 점수 산출                              |
| `POST /campaign-experiments/{id}/assignments` | 사용자 또는 타겟 오디언스에 variant 배정                |
| `POST /webhooks/message-events/{provider}`    | 발송/클릭/전환 이벤트 수집                              |
| `POST /ai/ctr/analyze`                        | SQL view 기반 실험 분석 요약 생성                       |

현재 `/ai/ctr/score`는 `_score_variant()`의 heuristic으로 점수를 만든다. LightGBM을 붙일 때는 API 계약을 바꾸기보다 `_score_variants()` 내부 구현을 모델 예측기로 교체하는 방식이 가장 작다.

---

## 3. LightGBM 적용 기준

### 3.1 모델이 학습할 문제

모델은 과거 배정/발송/이벤트 로그를 보고 다음을 예측한다.

```text
delivery_id 단위로 클릭 이벤트가 관찰 기간 안에 발생했는가?
```

기본 라벨은 다음처럼 둔다.

```text
clicked = 1: sent_at 이후 24시간 안에 click 이벤트가 있음
clicked = 0: sent_at 이후 24시간 안에 click 이벤트가 없음
```

24시간은 MVP 기본값이다. 실제 운영에서는 채널과 캠페인 목적에 따라 6시간, 24시간, 48시간 중 하나로 정책화한다.

### 3.2 현재 프로젝트에서 특히 중요한 전제

클릭한 발송만 저장하면 학습할 수 없다. `campaign_message_deliveries`에는 클릭하지 않은 발송도 반드시 남아야 한다.

```text
발송됨 + 클릭됨     -> clicked = 1
발송됨 + 클릭 안 됨 -> clicked = 0
```

또한 `sent_at` 이후에 알 수 있는 값은 feature로 쓰지 않는다.

feature에서 제외해야 하는 값은 다음과 같다.

| 제외 값                                                | 이유                                 |
| ------------------------------------------------------ | ------------------------------------ |
| `campaign_message_events`의 click/open/conversion 집계 | 발송 이후 결과 값                    |
| `v_campaign_*_metrics`의 CTR/CVR                       | 결과 집계 view                       |
| `final_status = delivered` 여부                        | 일부 채널에서는 결과 이벤트에 가까움 |
| `predicted_click_probability`                          | 기존 모델/heuristic의 출력값         |
| `model_version`                                        | 기존 scoring 정책 식별자             |

`assignment_source`는 랜덤/모델 배정 여부를 나타내므로 진단 컬럼으로는 유용하지만, 첫 모델 feature에는 넣지 않는 편이 안전하다. 모델이 과거 배정 정책을 외울 수 있기 때문이다.

---

## 4. 실제 테이블 기준 Feature 매핑

### 4.1 키와 라벨

| 용도           | 실제 컬럼                                                                          |
| -------------- | ---------------------------------------------------------------------------------- |
| 학습 행 식별자 | `campaign_message_deliveries.delivery_id`                                          |
| 사용자         | `campaign_message_deliveries.user_id`                                              |
| 캠페인         | `campaign_message_deliveries.campaign_id`                                          |
| 실험           | `campaign_message_deliveries.experiment_id`                                        |
| 메시지 variant | `campaign_message_deliveries.variant_id`, `campaign_message_variants.variant_code` |
| 채널           | `campaign_message_deliveries.channel`                                              |
| 발송 시각      | `campaign_message_deliveries.sent_at`                                              |
| 클릭 라벨      | `campaign_message_events.event_type = 'click'`                                     |

### 4.2 후보 Feature

| 그룹        | 후보 컬럼                                                                                  | 비고                                                      |
| ----------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| 시간        | `send_hour`, `send_day_of_week`, `is_weekend`                                              | `sent_at`에서 파생                                        |
| 메시지      | `variant_code`, `is_control`, `allocation_weight`, `ai_features` 일부                      | `message_body` 원문은 MVP에서는 제외                      |
| 캠페인      | `objective`, `category`, `budget_krw`, `expected_ctr`, `expected_cvr`                      | `expected_ctr/cvr`는 계획값이므로 사용 가능하나 과신 금지 |
| 사용자      | `age_group`, `gender`, `region`, `lifecycle`, `price_sensitivity`, `predicted_ltv_segment` | 우선 `targeting_snapshot` 값을 사용                       |
| 사용자 수치 | `avg_order_value_krw`, `purchase_count_90d`, `last_active_days`                            | 현재 `users` 테이블 값이라 시점 누수 가능성 표시 필요     |
| 다중 값     | interests, preferred_channels, recent_behaviors, target_segments                           | 배열을 count, match flag, 문자열 feature로 변환           |
| 추천 관계   | `recommendation_edges.label` 존재 여부                                                     | 사용자-캠페인 추천 후보였는지 표시                        |

현재 DDL에는 별도의 사용자 feature snapshot 테이블이 없다. 대신 `campaign_message_deliveries.targeting_snapshot`에 배정 시점의 사용자 속성이 JSONB로 저장된다. 학습에서는 가능하면 이 snapshot을 우선 사용하고, 비어 있는 경우에만 `users` 현재값으로 fallback한다.

---

## 5. 현재 DDL 기준 학습 데이터 SQL

아래 SQL은 `delivery_id` 1건당 정확히 1행을 만들고, 발송 후 24시간 안의 클릭만 `clicked=1`로 본다.

```sql
WITH delivery_base AS (
    SELECT
        d.*,
        d.sent_at AS exposure_at
    FROM campaign_message_deliveries d
    WHERE d.sent_at IS NOT NULL
      AND d.sent_at < NOW() - INTERVAL '24 hours'
      AND d.final_status IN ('sent', 'delivered')
),
event_summary AS (
    SELECT
        b.delivery_id,
        BOOL_OR(
            e.event_type = 'click'
            AND e.event_at >= b.exposure_at
            AND e.event_at < b.exposure_at + INTERVAL '24 hours'
        ) AS clicked_24h
    FROM delivery_base b
    LEFT JOIN campaign_message_events e
      ON e.delivery_id = b.delivery_id
    GROUP BY b.delivery_id
)
SELECT
    b.delivery_id,
    b.experiment_id,
    b.campaign_id,
    b.user_id,
    b.variant_id,
    v.variant_code,
    b.channel,
    b.sent_at,

    EXTRACT(HOUR FROM b.sent_at AT TIME ZONE 'Asia/Seoul')::INTEGER AS send_hour,
    EXTRACT(DOW FROM b.sent_at AT TIME ZONE 'Asia/Seoul')::INTEGER AS send_day_of_week,
    CASE
        WHEN EXTRACT(DOW FROM b.sent_at AT TIME ZONE 'Asia/Seoul') IN (0, 6) THEN 1
        ELSE 0
    END AS is_weekend,

    x.assignment_method,
    x.primary_metric,

    v.is_control,
    v.allocation_weight,
    v.ai_features ->> 'urgency' AS variant_urgency,
    v.ai_features ->> 'personalized' AS variant_personalized,
    v.ai_features ->> 'message_length_group' AS message_length_group,

    c.objective,
    c.category,
    c.budget_krw,
    c.expected_ctr,
    c.expected_cvr,

    COALESCE(b.targeting_snapshot ->> 'age_group',
        CASE
            WHEN u.age < 20 THEN 'under_20'
            WHEN u.age < 30 THEN '20s'
            WHEN u.age < 40 THEN '30s'
            WHEN u.age < 50 THEN '40s'
            WHEN u.age < 60 THEN '50s'
            ELSE '60_plus'
        END
    ) AS age_group,
    COALESCE(b.targeting_snapshot ->> 'gender', u.gender) AS gender,
    COALESCE(b.targeting_snapshot ->> 'region', u.region) AS region,
    COALESCE(b.targeting_snapshot ->> 'lifecycle', u.lifecycle) AS lifecycle,
    COALESCE(b.targeting_snapshot ->> 'price_sensitivity', u.price_sensitivity) AS price_sensitivity,
    COALESCE(b.targeting_snapshot ->> 'predicted_ltv_segment', u.predicted_ltv_segment) AS predicted_ltv_segment,

    u.avg_order_value_krw,
    u.purchase_count_90d,
    u.last_active_days,

    (EXISTS (
        SELECT 1
        FROM user_preferred_channels upc
        WHERE upc.user_id = b.user_id
          AND upc.preferred_channel = b.channel
    ))::INTEGER AS is_preferred_channel,
    (EXISTS (
        SELECT 1
        FROM user_interests ui
        WHERE ui.user_id = b.user_id
          AND ui.interest = c.category
    ))::INTEGER AS is_category_interest,
    (EXISTS (
        SELECT 1
        FROM recommendation_edges re
        WHERE re.user_id = b.user_id
          AND re.campaign_id = b.campaign_id
    ))::INTEGER AS has_recommendation_edge,

    COALESCE(es.clicked_24h, FALSE)::INTEGER AS clicked
FROM delivery_base b
JOIN campaign_experiments x
  ON x.experiment_id = b.experiment_id
JOIN campaign_message_variants v
  ON v.variant_id = b.variant_id
JOIN campaigns c
  ON c.campaign_id = b.campaign_id
JOIN users u
  ON u.user_id = b.user_id
LEFT JOIN event_summary es
  ON es.delivery_id = b.delivery_id;
```

이 SQL은 현재 `training/ctr_training_dataset.sql`에 구현되어 있다.

---

## 6. 최소 Feature 목록

처음부터 많은 feature를 넣지 말고 현재 DDL에서 안정적으로 얻을 수 있는 값으로 시작한다.

### 범주형 Feature

```python
CATEGORICAL_FEATURES = [
    "variant_code",
    "channel",
    "objective",
    "category",
    "age_group",
    "gender",
    "region",
    "lifecycle",
    "price_sensitivity",
    "predicted_ltv_segment",
    "message_length_group",
]
```

### 수치형 Feature

```python
NUMERIC_FEATURES = [
    "send_hour",
    "send_day_of_week",
    "is_weekend",
    "is_control",
    "allocation_weight",
    "budget_krw",
    "expected_ctr",
    "expected_cvr",
    "avg_order_value_krw",
    "purchase_count_90d",
    "last_active_days",
    "is_preferred_channel",
    "is_category_interest",
    "has_recommendation_edge",
]
```

초기 모델에서는 다음 값은 제외한다.

```text
delivery_id
experiment_id
campaign_id
user_id
variant_id
assignment_method
primary_metric
model_version
predicted_click_probability
clicked/open/conversion 집계값
```

ID 자체는 모델이 특정 사용자나 캠페인을 외우게 만들 수 있다. 데이터가 충분히 쌓인 뒤 별도 실험으로만 추가한다.

---

## 7. 패키지와 구현 위치

현재 `requirements.txt`에는 `pandas`, `numpy`, `fastapi`, `psycopg[binary]`는 있지만 LightGBM 학습에 필요한 패키지가 빠져 있다.

LightGBM 구현 시 추가가 필요한 패키지는 다음이다.

```txt
lightgbm
scikit-learn
joblib
```

현재 프로젝트 구조에 맞춘 권장 추가 파일은 다음 정도로 제한한다.

```text
training/
  ctr_training_dataset.sql
  ctr_features.py
  train_lightgbm_ctr.py
  ctr_predictor.py
artifacts/
  ctr_model.joblib
  ctr_metadata.json
```

현재 구현된 학습 실행 명령은 다음과 같다.

```bash
python -m training.train_lightgbm_ctr
```

현재 샘플 DB처럼 발송 시간이 아직 24시간 관찰 기간을 지나지 않은 로컬 데모 데이터로 파이프라인만 검증하려면 아래 옵션을 붙인다.

```bash
python -m training.train_lightgbm_ctr \
  --include-unobserved \
  --model-version ctr-lgbm-local-demo
```

`--include-unobserved`는 로컬 검증용 옵션이다. 운영 학습에서는 클릭 관찰 기간이 끝난 발송만 사용해야 한다.

API는 새 FastAPI 앱을 만들지 않고 기존 `api.py`의 `_score_variants()` 경로를 교체한다.

```text
기존: _score_variant() heuristic
변경: CtrPredictor.predict(user, variants, experiment)
```

이렇게 하면 `/ai/ctr/score`, `/campaign-experiments/{id}/assignments`, `/campaign-experiments/run` 응답 구조와 DB 저장 구조를 유지할 수 있다.

---

## 8. 학습/검증 분할

랜덤 분할보다 시간 기준 분할을 우선한다.

```text
과거 기간: train
최근 일부: validation
가장 최근 일부: test
```

예시:

```text
2026-07-01 ~ 2026-07-20: train
2026-07-21 ~ 2026-07-25: validation
2026-07-26 ~ 2026-07-31: test
```

현재 샘플 DDL의 deterministic test data는 모델 성능을 판단하기에 충분하지 않다. 샘플 데이터는 파이프라인과 SQL shape 검증용으로만 사용하고, 실제 모델 평가는 운영성 발송/이벤트 로그가 쌓인 뒤 진행한다.

---

## 9. 평가 지표

Accuracy는 CTR 모델 평가에 적합하지 않다. 클릭하지 않은 데이터가 대부분이면 전부 0으로 예측해도 정확도가 높게 보인다.

필수 지표는 다음이다.

| 지표               | 의미                                          |
| ------------------ | --------------------------------------------- |
| ROC-AUC            | 클릭 건이 비클릭 건보다 높은 점수를 받는 정도 |
| PR-AUC             | 클릭 희소 데이터에서 유용한 순위 성능         |
| Log Loss           | 예측 확률 품질                                |
| Brier Score        | 확률 calibration 품질                         |
| Actual CTR         | 검증 데이터의 실제 클릭률                     |
| Predicted CTR Mean | 모델 평균 예측 클릭률                         |
| Lift@10%           | 상위 10% 점수군의 실제 CTR lift               |

채널별로는 `ctr_pct`와 `delivered_ctr_pct`를 구분해서 본다. LMS, SMS, Kakao처럼 impression 이벤트가 안정적으로 없을 수 있는 채널은 `delivered_ctr_pct`를 주요 보조 지표로 본다. 현재 `api.py`의 분석 함수도 CTR 채널이 `lms`, `sms`, `kakao`이면 `delivered_ctr_pct`를 우선 사용한다.

---

## 10. 현재 view와 학습 데이터의 역할 차이

`v_campaign_variant_metrics`, `v_campaign_segment_metrics`, `v_campaign_daily_metrics`는 운영 분석용 view다.

이 view들은 다음에 사용한다.

- 실험 결과 화면
- `/ai/ctr/analyze` 응답
- variant별/세그먼트별 성과 점검
- 모델 배포 후 shadow 성능 비교

하지만 학습 feature에는 직접 넣지 않는다. view 안의 `click_count`, `ctr_pct`, `cvr_pct`는 모델이 예측해야 할 결과를 이미 포함하기 때문이다.

---

## 11. 예측 결과 저장 위치

현재 DDL에는 예측 결과 저장 컬럼이 이미 있다.

| 저장 값                 | 컬럼                                                                 |
| ----------------------- | -------------------------------------------------------------------- |
| 선택된 variant          | `campaign_message_deliveries.variant_id`                             |
| 배정 방식               | `campaign_message_deliveries.assignment_source`                      |
| 모델 버전               | `campaign_message_deliveries.model_version`                          |
| 선택 variant의 점수     | `campaign_message_deliveries.predicted_click_probability`            |
| 후보별 점수와 배정 근거 | `campaign_message_deliveries.targeting_snapshot->'candidate_scores'` |

별도 prediction log 테이블을 당장 추가하지 않아도 MVP 운영은 가능하다. 다만 모델 감사와 재현성이 중요해지면 다음 형태의 별도 로그를 추가한다.

```sql
CREATE TABLE campaign_ctr_prediction_logs (
    prediction_id BIGSERIAL PRIMARY KEY,
    delivery_id BIGINT REFERENCES campaign_message_deliveries(delivery_id) ON DELETE SET NULL,
    experiment_id BIGINT NOT NULL REFERENCES campaign_experiments(experiment_id) ON DELETE CASCADE,
    user_id VARCHAR(20) NOT NULL,
    campaign_id VARCHAR(20) NOT NULL,
    model_version VARCHAR(100) NOT NULL,
    selected_variant_code VARCHAR(20),
    candidate_scores JSONB NOT NULL,
    feature_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    predicted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`feature_payload`에는 개인정보 원문을 넣지 않고 모델 입력에 필요한 파생값만 저장한다.

---

## 12. API 교체 전략

### 12.1 유지할 계약

다음 API 계약은 유지한다.

```text
POST /ai/ctr/score
입력: experimentId, userIds, modelVersion
출력: userId, selectedVariantCode, predictedClickProbability, scores
```

assignment API도 유지한다.

```text
POST /campaign-experiments/{id}/assignments
assignmentMethod=model이면 점수 기반 epsilon-greedy 배정
```

### 12.2 교체할 내부 구현

현재 구현:

```text
_score_variants()
  -> _score_variant()
     -> preferred channel, category interest, urgency, personalized 등 규칙 점수
```

LightGBM 구현:

```text
_score_variants()
  -> 실험, 사용자, variant를 feature row로 변환
  -> ctr_predictor.predict_proba(rows)
  -> variant_code별 확률 dict 반환
```

예측 실패 시에는 두 가지 정책 중 하나를 명시적으로 선택한다.

| 정책        | 설명                                                                |
| ----------- | ------------------------------------------------------------------- |
| fail closed | 500을 반환하고 배정을 막음                                          |
| fallback    | 기존 `heuristic-ctr-v1`로 점수화하고 `modelVersion`에 fallback 표시 |

MVP에서는 운영 흐름을 끊지 않는 `fallback`이 유용하지만, 모델 성능 측정 중에는 fallback 비율을 별도 로그로 확인해야 한다.

---

## 13. 데이터 검증 SQL

### 13.1 발송과 클릭 기본 규모

```sql
SELECT
    COUNT(*) AS delivery_count,
    COUNT(*) FILTER (WHERE sent_at IS NOT NULL) AS sent_delivery_count,
    COUNT(*) FILTER (WHERE final_status = 'delivered') AS delivered_count,
    MIN(sent_at) AS first_sent_at,
    MAX(sent_at) AS last_sent_at
FROM campaign_message_deliveries;
```

### 13.2 클릭 라벨 분포

```sql
WITH labels AS (
    SELECT
        d.delivery_id,
        BOOL_OR(e.event_type = 'click') AS clicked
    FROM campaign_message_deliveries d
    LEFT JOIN campaign_message_events e
      ON e.delivery_id = d.delivery_id
    GROUP BY d.delivery_id
)
SELECT
    COUNT(*) AS deliveries,
    COUNT(*) FILTER (WHERE clicked) AS clicked_deliveries,
    ROUND(100.0 * COUNT(*) FILTER (WHERE clicked) / NULLIF(COUNT(*), 0), 4) AS click_rate_pct
FROM labels;
```

### 13.3 variant/channel별 데이터 균형

```sql
SELECT
    d.channel,
    v.variant_code,
    COUNT(*) AS deliveries,
    COUNT(*) FILTER (WHERE e.event_type = 'click') AS click_events
FROM campaign_message_deliveries d
JOIN campaign_message_variants v
  ON v.variant_id = d.variant_id
LEFT JOIN campaign_message_events e
  ON e.delivery_id = d.delivery_id
GROUP BY d.channel, v.variant_code
ORDER BY d.channel, v.variant_code;
```

### 13.4 학습 SQL의 1 delivery 1 row 보장

학습 SQL을 `training_rows`라는 CTE로 감싼 뒤 확인한다.

```sql
SELECT
    COUNT(*) AS row_count,
    COUNT(DISTINCT delivery_id) AS distinct_delivery_count
FROM training_rows;
```

두 값이 다르면 이벤트 join 때문에 중복이 생긴 것이다.

---

## 14. 운영 적용 순서

현재 프로젝트에서 가장 안전한 적용 순서는 다음이다.

```text
1. 현재 DDL로 학습 데이터 SQL 작성
2. delivery_id 중복과 clicked 라벨 검증
3. Logistic Regression 또는 heuristic baseline 지표 저장
4. LightGBM 오프라인 학습
5. 시간 기준 validation/test 성능 확인
6. /ai/ctr/score에서 shadow mode로 예측만 기록
7. assignmentMethod=model 트래픽을 소량 적용
8. epsilon-greedy 탐색 트래픽 유지
9. /ai/ctr/analyze와 view로 control/treatment 성과 비교
```

처음부터 모델이 고른 메시지만 계속 보내면 새 variant의 학습 데이터가 줄어든다. `assignment_method = 'model'`을 쓰더라도 `epsilon`을 0으로 두지 말고 최소 10%에서 20%의 탐색을 유지한다.

---

## 15. 흔한 실패 원인

| 실패                             | 현재 프로젝트에서의 증상          | 대응                                             |
| -------------------------------- | --------------------------------- | ------------------------------------------------ |
| 클릭 로그만 학습                 | `clicked=0` 행이 거의 없음        | `campaign_message_deliveries` 전체를 분모로 사용 |
| 이벤트 join 중복                 | delivery 1건이 여러 행으로 늘어남 | 이벤트는 먼저 `delivery_id`로 집계               |
| 결과 view를 feature로 사용       | 오프라인 성능이 비현실적으로 높음 | `v_campaign_*_metrics`는 분석 전용으로 유지      |
| 현재 users 값으로 과거 발송 학습 | 과거 시점 재현 불가               | `targeting_snapshot` 우선 사용                   |
| deterministic sample로 성능 판단 | 지표가 불안정하거나 완벽하게 보임 | 샘플은 파이프라인 검증용으로만 사용              |
| 모델 선택만 계속 적용            | exploration 데이터가 사라짐       | epsilon-greedy와 control 유지                    |
| predicted CTR을 feature로 재사용 | 기존 heuristic을 그대로 복제      | 첫 모델에서는 제외                               |

---

## 16. 바이브 코딩용 현재 프로젝트 프롬프트

아래 프롬프트는 현재 저장소 구조와 DDL을 기준으로 LightGBM CTR 확장을 구현할 때 사용한다.

```text
너는 시니어 Python ML 엔지니어이자 FastAPI 백엔드 엔지니어다.

목표:
현재 저장소의 api.py, docs/data/local_bootstrap.sql, campaign_message_* 테이블 구조를 유지하면서
LightGBM 기반 CTR 예측 MVP를 추가한다.

현재 구조:
- FastAPI 앱은 루트 api.py에 있다.
- DB 스키마는 docs/data/local_bootstrap.sql에 있다.
- CTR 실험 테이블은 campaign_experiments, campaign_message_variants,
  campaign_message_deliveries, campaign_message_events다.
- /ai/ctr/score는 현재 heuristic-ctr-v1으로 점수화한다.
- /campaign-experiments/{id}/assignments는 점수와 targeting_snapshot을 저장한다.

구현 요구사항:
1. 새 FastAPI 앱을 만들지 말고 기존 api.py의 API 계약을 유지한다.
2. 학습 데이터 기준 행은 campaign_message_deliveries.delivery_id로 한다.
3. campaign_message_events는 delivery_id로 먼저 집계해 중복 행을 만들지 않는다.
4. sent_at 이후 24시간 안의 click 이벤트만 clicked=1로 본다.
5. sent_at이 없거나 관찰 기간 24시간이 끝나지 않은 행은 학습에서 제외한다.
6. targeting_snapshot JSONB를 사용자 feature의 1차 원천으로 사용한다.
7. users 현재값은 snapshot이 비어 있을 때만 fallback으로 사용하고 누수 위험을 문서화한다.
8. v_campaign_*_metrics의 결과 지표는 feature로 사용하지 않는다.
9. model_version, predicted_click_probability, assignment_source는 첫 모델 feature에서 제외한다.
10. 범주형 feature는 pandas category dtype으로 처리한다.
11. 시간 기준 train/validation/test split을 사용한다.
12. ROC-AUC, PR-AUC, Log Loss, Brier Score, Actual CTR, Predicted CTR Mean, Lift@10%를 출력한다.
13. 모델은 artifacts/ctr_model.joblib에 저장한다.
14. feature 목록, category 목록, 모델 버전, 데이터 범위, 성능 지표는 artifacts/ctr_metadata.json에 저장한다.
15. _score_variants() 내부를 LightGBM predictor 호출로 교체하되, 모델 로딩 실패 시 fallback 정책을 명시한다.
16. /ai/ctr/score 응답 구조는 userId, selectedVariantCode, predictedClickProbability, scores를 유지한다.
17. assignmentMethod=model의 epsilon-greedy 흐름은 유지한다.
18. requirements.txt에 필요한 패키지만 추가한다: lightgbm, scikit-learn, joblib.
19. docker compose 환경에서 문법 검증과 최소 smoke test를 수행한다.
20. 불확실한 컬럼이나 정책은 사실처럼 확정하지 말고 TODO로 표시한다.

먼저 실제 DDL 매핑표와 학습 SQL을 제시한 뒤, 필요한 파일만 최소 변경으로 구현하라.
```

---

## 17. 최종 체크리스트

### 데이터

- [ ] `campaign_message_deliveries`에 클릭하지 않은 발송도 있다.
- [ ] `delivery_id` 1건당 학습 데이터가 1행이다.
- [ ] 클릭 인정 기간이 명확하다.
- [ ] 관찰 기간이 끝나지 않은 발송은 제외했다.
- [ ] `targeting_snapshot`을 우선 사용한다.
- [ ] 결과 이벤트와 결과 view를 feature로 쓰지 않는다.

### 모델

- [ ] 시간 기준 split을 사용한다.
- [ ] Accuracy 외의 지표를 본다.
- [ ] 실제 CTR과 평균 예측 CTR을 비교한다.
- [ ] channel/variant별 성능을 따로 본다.
- [ ] 신규 category/variant/channel 처리 정책이 있다.
- [ ] 모델 파일과 metadata 파일이 같은 버전이다.

### 운영

- [ ] 기존 `/ai/ctr/score` API 계약을 유지한다.
- [ ] `campaign_message_deliveries.model_version`에 모델 버전을 저장한다.
- [ ] `targeting_snapshot.candidate_scores`에 후보별 점수를 남긴다.
- [ ] 처음에는 shadow mode로 예측만 저장한다.
- [ ] model 배정 시에도 epsilon 탐색을 유지한다.
- [ ] `/ai/ctr/analyze`와 SQL view로 실험 결과를 검증한다.

---

## 18. 참고 문서

- 현재 DDL: `docs/data/local_bootstrap.sql`
- 현재 스키마 카탈로그: `docs/data/schema_catalog.json`
- 캠페인/사용자 샘플: `docs/data/campaign_user_rag_sample_50_with_edges.json`
- 프로젝트 구조 보고서: `docs/overview/structure.md`
- API 구현: `api.py`
- LightGBM Python API: https://lightgbm.readthedocs.io/en/latest/Python-API.html
- LGBMClassifier: https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.LGBMClassifier.html
