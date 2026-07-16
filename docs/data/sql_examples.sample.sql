-- NL2SQL/RAG retrieval examples for the campaign recommendation schema.
-- Keep this file between 10 and 30 examples.

-- 1. 활성 사용자 조회
SELECT user_id, age, gender, region, lifecycle
FROM users
WHERE lifecycle = 'active';

-- 2. 휴면 고객 조회
-- 2. 6개월 이상 접속하지 않은 휴면 고객 조회
SELECT
    user_id,
    region,
    lifecycle,
    last_login_at,
    last_active_days,
    price_sensitivity
FROM users
WHERE last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
ORDER BY last_login_at ASC;

-- 3. 패션 카테고리 캠페인 조회
SELECT campaign_id, name, objective, offer
FROM campaigns
WHERE category = 'fashion';

-- 4. 구매 목적 캠페인 중 예산이 큰 순서
SELECT campaign_id, name, category, budget_krw
FROM campaigns
WHERE objective = 'purchase'
ORDER BY budget_krw DESC;

-- 5. app_push 채널 캠페인 조회
SELECT c.campaign_id, c.name, c.category
FROM campaigns c
JOIN campaign_channels cc ON cc.campaign_id = c.campaign_id
WHERE cc.channel = 'app_push';

-- 6. 가격 민감 타겟 캠페인 조회
SELECT c.campaign_id, c.name, c.offer
FROM campaigns c
JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id
WHERE ts.target_segment = 'price_sensitive';

-- 7. 뷰티 관심 사용자 조회
SELECT u.user_id, u.age, u.gender, u.lifecycle
FROM users u
JOIN user_interests ui ON ui.user_id = u.user_id
WHERE ui.interest = 'beauty';

-- 8. 카카오 선호 사용자 조회
SELECT u.user_id, u.region, u.price_sensitivity
FROM users u
JOIN user_preferred_channels upc ON upc.user_id = u.user_id
WHERE upc.preferred_channel = 'kakao';

-- 9. 장바구니 이탈 행동 사용자 조회
SELECT u.user_id, u.gender, u.lifecycle, urb.behavior
FROM users u
JOIN user_recent_behaviors urb ON urb.user_id = u.user_id
WHERE urb.behavior = 'cart_abandoned:fashion';

-- 10. 특정 사용자에게 추천된 캠페인 조회
SELECT re.user_id, c.campaign_id, c.name, re.reason, re.label
FROM recommendation_edges re
JOIN campaigns c ON c.campaign_id = re.campaign_id
WHERE re.user_id = 'user_001';

-- 11. 추천 강도가 높은 추천 edge 조회
SELECT re.user_id, re.campaign_id, c.name, re.reason
FROM recommendation_edges re
JOIN campaigns c ON c.campaign_id = re.campaign_id
WHERE re.label = 'high';

-- 12. 캠페인별 추천 사용자 수 집계
SELECT c.campaign_id, c.name, COUNT(re.user_id) AS recommended_user_count
FROM campaigns c
LEFT JOIN recommendation_edges re ON re.campaign_id = c.campaign_id
GROUP BY c.campaign_id, c.name
ORDER BY recommended_user_count DESC;

-- 13. 지역별 사용자 수 집계
SELECT region, COUNT(*) AS user_count
FROM users
GROUP BY region
ORDER BY user_count DESC;

-- 14. 관심사와 채널이 동시에 맞는 사용자 조회
SELECT DISTINCT u.user_id, u.gender, u.region
FROM users u
JOIN user_interests ui ON ui.user_id = u.user_id
JOIN user_preferred_channels upc ON upc.user_id = u.user_id
WHERE ui.interest = 'food'
  AND upc.preferred_channel = 'app_push';

-- 15. 키워드가 쿠폰인 캠페인과 타겟 세그먼트 조회
SELECT c.campaign_id, c.name, ts.target_segment
FROM campaigns c
JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id
WHERE ck.keyword = '쿠폰'
ORDER BY c.campaign_id, ts.target_segment;

-- 16. 20대 여성 장바구니 이탈 쿠폰 캠페인 추천
SELECT DISTINCT u.user_id, u.age, u.gender, c.campaign_id, c.category, c.offer, urb.behavior
FROM users u
JOIN user_recent_behaviors urb ON urb.user_id = u.user_id
JOIN recommendation_edges re ON re.user_id = u.user_id
JOIN campaigns c ON c.campaign_id = re.campaign_id
JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id
WHERE u.gender = 'female'
  AND u.age BETWEEN 20 AND 29
  AND urb.behavior LIKE 'cart_abandoned:%'
  AND ck.keyword = '쿠폰'
  AND ts.target_segment = 'cart_abandoner';

-- 17. 20~30대 남성이 아닌 장바구니 이탈 쿠폰 캠페인 추천
SELECT DISTINCT u.user_id, u.age, u.gender, c.campaign_id, c.category, c.offer, urb.behavior
FROM users u
JOIN user_recent_behaviors urb ON urb.user_id = u.user_id
JOIN recommendation_edges re ON re.user_id = u.user_id
JOIN campaigns c ON c.campaign_id = re.campaign_id
JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id
WHERE u.gender <> 'male'
  AND u.age BETWEEN 20 AND 39
  AND urb.behavior LIKE 'cart_abandoned:%'
  AND ck.keyword = '쿠폰'
  AND ts.target_segment = 'cart_abandoner';

-- 18. 쿠폰 관심 고객 맞춤 쿠폰 캠페인 추천
SELECT DISTINCT u.user_id, u.price_sensitivity, c.campaign_id, c.name, c.offer, ts.target_segment
FROM users u
JOIN recommendation_edges re ON re.user_id = u.user_id
JOIN campaigns c ON c.campaign_id = re.campaign_id
JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id
WHERE u.price_sensitivity = 'high'
  AND ts.target_segment = 'price_sensitive'
  AND ck.keyword = '쿠폰';

-- 19. A/B/C 메시지 variant별 CTR 성과 조회
SELECT variant_code, message_name, assigned_count, delivered_count,
       impression_count, click_count, conversion_count,
       ctr_pct, delivered_ctr_pct, cvr_pct, revenue_krw
FROM v_campaign_variant_metrics
WHERE experiment_id = 1
ORDER BY delivered_ctr_pct DESC NULLS LAST;

-- 20. 세그먼트별 A/B/C 클릭 성과 조회
SELECT variant_code, gender, age_group, region, lifecycle,
       delivered_count, impression_count, click_count, conversion_count,
       ctr_pct, click_to_conversion_rate_pct
FROM v_campaign_segment_metrics
WHERE experiment_id = 1
ORDER BY click_count DESC, delivered_count DESC;

-- 21. 모델 학습용 delivery 단위 클릭 label 생성
SELECT d.delivery_id, d.user_id, d.campaign_id, d.channel, v.variant_code,
       d.assignment_source, d.predicted_click_probability,
       CASE WHEN COUNT(e.event_id) FILTER (WHERE e.event_type = 'click') > 0 THEN 1 ELSE 0 END AS clicked
FROM campaign_message_deliveries d
JOIN campaign_message_variants v ON v.variant_id = d.variant_id
LEFT JOIN campaign_message_events e ON e.delivery_id = d.delivery_id
WHERE d.experiment_id = 1
GROUP BY d.delivery_id, v.variant_code;

-- 22. 날짜별 메시지 이벤트 추이 조회
SELECT event_date_kst, variant_id, channel,
       sent_event_count, delivered_event_count, impression_event_count,
       click_event_count, conversion_event_count, revenue_krw
FROM v_campaign_daily_metrics
WHERE experiment_id = 1
ORDER BY event_date_kst, variant_id;

-- 23. 장바구니에 상품을 담고 24시간 이상 결제하지 않은 고객 조회
SELECT
    c.cart_id,
    c.user_id,
    c.total_amount_krw
FROM shopping_carts c
WHERE c.cart_status = 'abandoned'
AND c.purchase_completed = FALSE
AND c.abandoned_at <= NOW() - INTERVAL '24 hours';

-- 24. 장바구니 금액이 5만원 이상인 미결제 고객
SELECT
    user_id,
    total_amount_krw
FROM shopping_carts
WHERE cart_status='abandoned'
AND purchase_completed=FALSE
AND total_amount_krw>=50000;

-- 25. 쿠폰 발급 가능한 장바구니 이탈 고객
SELECT DISTINCT
    c.user_id
FROM shopping_carts c
JOIN shopping_cart_items i
ON c.cart_id=i.cart_id
WHERE c.cart_status='abandoned'
AND i.coupon_available=TRUE;

-- 26. 패션 상품을 장바구니에 담고 구매하지 않은 고객
SELECT DISTINCT
    c.user_id
FROM shopping_carts c
JOIN shopping_cart_items i
ON c.cart_id=i.cart_id
WHERE c.cart_status='abandoned'
AND i.category='fashion';

-- 27. 장바구니 리마인드 문자를 아직 보내지 않은 고객
SELECT
    c.user_id
FROM shopping_carts c
LEFT JOIN cart_reminder_history r
ON c.cart_id=r.cart_id
WHERE c.cart_status='abandoned'
AND r.reminder_id IS NULL;

-- 28. 6개월 이상 미접속한 휴면 고객에게 복귀 캠페인 추천
SELECT DISTINCT
    u.user_id,
    u.region,
    u.lifecycle,
    u.last_login_at,
    u.last_active_days,
    c.campaign_id,
    c.name AS campaign_name,
    c.offer
FROM users u
JOIN recommendation_edges re
    ON re.user_id = u.user_id
JOIN campaigns c
    ON c.campaign_id = re.campaign_id
WHERE u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND c.objective = 'reactivation'
ORDER BY u.last_login_at ASC;


-- 29. 6개월 이상 미접속하고 최근 90일 구매가 없는 고객 조회
SELECT
    user_id,
    region,
    lifecycle,
    last_login_at,
    last_active_days,
    purchase_count_90d,
    price_sensitivity
FROM users
WHERE last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND purchase_count_90d = 0
ORDER BY last_active_days DESC;


-- 30. 6개월 이상 휴면 고객 중 문자 또는 카카오 수신 선호 고객 조회
SELECT DISTINCT
    u.user_id,
    u.region,
    u.last_login_at,
    u.last_active_days,
    upc.preferred_channel
FROM users u
JOIN user_preferred_channels upc
    ON upc.user_id = u.user_id
WHERE u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND upc.preferred_channel IN ('sms', 'kakao', 'lms', 'rcs')
ORDER BY u.last_login_at ASC;