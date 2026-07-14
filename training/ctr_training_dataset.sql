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
