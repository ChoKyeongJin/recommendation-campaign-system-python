-- ============================================================
-- Full PostgreSQL schema for campaign targeting + CTR analytics
-- Includes original schema, experiment variants, deliveries,
-- append-only event logs, metric views, and deterministic test data.
-- PostgreSQL 14+
-- ============================================================

DROP VIEW IF EXISTS v_campaign_daily_metrics CASCADE;
DROP VIEW IF EXISTS v_campaign_segment_metrics CASCADE;
DROP VIEW IF EXISTS v_campaign_variant_metrics CASCADE;

DROP TABLE IF EXISTS campaign_message_events CASCADE;
DROP TABLE IF EXISTS campaign_message_deliveries CASCADE;
DROP TABLE IF EXISTS campaign_message_variants CASCADE;
DROP TABLE IF EXISTS campaign_experiments CASCADE;
DROP TABLE IF EXISTS campaign_channel_messages CASCADE;
DROP TABLE IF EXISTS campaign_target_audience_members CASCADE;
DROP TABLE IF EXISTS campaign_target_audiences CASCADE;
DROP TABLE IF EXISTS campaign_query_failure_logs CASCADE;
DROP TABLE IF EXISTS campaign_prompt_templates CASCADE;
DROP TABLE IF EXISTS campaign_policies CASCADE;

-- PostgreSQL DDL + sample data
-- Source: campaign_user_rag_sample_50_with_edges(1).json

DROP TABLE IF EXISTS recommendation_edges CASCADE;
DROP TABLE IF EXISTS user_recent_behaviors CASCADE;
DROP TABLE IF EXISTS user_preferred_channels CASCADE;
DROP TABLE IF EXISTS user_interests CASCADE;
DROP TABLE IF EXISTS campaign_message_examples CASCADE;
DROP TABLE IF EXISTS campaign_keywords CASCADE;
DROP TABLE IF EXISTS campaign_target_segments CASCADE;
DROP TABLE IF EXISTS campaign_channels CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS campaigns CASCADE;

CREATE TABLE campaigns (
    campaign_id VARCHAR(20) PRIMARY KEY,
    name TEXT NOT NULL,
    objective VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    offer TEXT,
    budget_krw INTEGER NOT NULL CHECK (budget_krw >= 0),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    expected_ctr NUMERIC(5,2),
    expected_cvr NUMERIC(5,2),
    text_for_embedding TEXT,
    CHECK (end_date >= start_date)
);

CREATE TABLE campaign_channels (
    campaign_id VARCHAR(20) NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    channel VARCHAR(50) NOT NULL,
    PRIMARY KEY (campaign_id, channel)
);

CREATE TABLE campaign_target_segments (
    campaign_id VARCHAR(20) NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    target_segment VARCHAR(100) NOT NULL,
    PRIMARY KEY (campaign_id, target_segment)
);

CREATE TABLE campaign_keywords (
    campaign_id VARCHAR(20) NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    keyword VARCHAR(100) NOT NULL,
    PRIMARY KEY (campaign_id, keyword)
);

CREATE TABLE campaign_message_examples (
  example_id VARCHAR(30) PRIMARY KEY,
  campaign_id VARCHAR(20) NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  channel VARCHAR(50) NOT NULL,
  emphasis_type VARCHAR(50) NOT NULL,
  message_text TEXT NOT NULL,
  brand_tone VARCHAR(100),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
    user_id VARCHAR(20) PRIMARY KEY,
    age INTEGER NOT NULL CHECK (age >= 0),
    gender VARCHAR(20) NOT NULL,
    region VARCHAR(50) NOT NULL,
    lifecycle VARCHAR(50) NOT NULL,
    avg_order_value_krw INTEGER NOT NULL CHECK (avg_order_value_krw >= 0),
    purchase_count_90d INTEGER NOT NULL CHECK (purchase_count_90d >= 0),
    last_active_days INTEGER NOT NULL CHECK (last_active_days >= 0),
    price_sensitivity VARCHAR(20) NOT NULL,
    predicted_ltv_segment VARCHAR(20) NOT NULL,
    text_for_embedding TEXT
);

CREATE TABLE user_interests (
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    interest VARCHAR(100) NOT NULL,
    PRIMARY KEY (user_id, interest)
);

CREATE TABLE user_preferred_channels (
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    preferred_channel VARCHAR(50) NOT NULL,
    PRIMARY KEY (user_id, preferred_channel)
);

CREATE TABLE user_recent_behaviors (
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    behavior VARCHAR(100) NOT NULL,
    PRIMARY KEY (user_id, behavior)
);

CREATE TABLE recommendation_edges (
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    campaign_id VARCHAR(20) NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    reason TEXT,
    label VARCHAR(20) NOT NULL,
    PRIMARY KEY (user_id, campaign_id)
);

-- /target-sql 실행 결과로 생성되는 타겟 오디언스 스냅샷입니다.
-- 대량 사용자 목록은 API 메모리로 가져오지 않고 INSERT ... SELECT로 members에 적재합니다.
CREATE TABLE campaign_target_audiences (
    audience_id BIGSERIAL PRIMARY KEY,
    audience_key VARCHAR(64) NOT NULL UNIQUE,
    prompt TEXT NOT NULL,
    query_parser VARCHAR(20) NOT NULL,
    request_options JSONB NOT NULL DEFAULT '{}'::JSONB,
    generated_sql TEXT NOT NULL,
    sql_hash CHAR(64) NOT NULL,
    query_plan JSONB NOT NULL DEFAULT '{}'::JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'expired')),
    member_count BIGINT NOT NULL DEFAULT 0 CHECK (member_count >= 0),
    target_customer_count BIGINT NOT NULL DEFAULT 0 CHECK (target_customer_count >= 0),
    target_campaign_count BIGINT NOT NULL DEFAULT 0 CHECK (target_campaign_count >= 0),
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,

    CONSTRAINT chk_campaign_target_audience_completed_at
        CHECK (completed_at IS NULL OR completed_at >= created_at),

    CONSTRAINT chk_campaign_target_audience_expires_at
        CHECK (expires_at IS NULL OR expires_at >= created_at)
);

-- 타겟 오디언스의 대량 멤버 목록입니다.
-- 멤버 행은 user_id/campaign_id 식별자만 좁게 저장하고, 발송 시점의 상세 스냅샷은 campaign_message_deliveries에 저장합니다.
CREATE TABLE campaign_target_audience_members (
    audience_id BIGINT NOT NULL
        REFERENCES campaign_target_audiences(audience_id) ON DELETE CASCADE,
    member_id BIGSERIAL NOT NULL,
    user_id VARCHAR(20) NOT NULL,
    campaign_id VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (audience_id, member_id)
);

-- GraphRAG/API 실패 질의를 누적 저장하는 운영 로그입니다.
-- 실패 로그 저장 자체가 사용자 응답을 막지 않도록 API에서는 best-effort로 insert합니다.
CREATE TABLE campaign_query_failure_logs (
    failure_log_id BIGSERIAL PRIMARY KEY,
    endpoint VARCHAR(100) NOT NULL,
    prompt TEXT NOT NULL,
    query_parser VARCHAR(20),
    api_status VARCHAR(40),
    failure_stage VARCHAR(60) NOT NULL,
    failure_reason TEXT NOT NULL,
    error_detail TEXT,
    generated_sql TEXT,
    sql_hash CHAR(64),
    request_options JSONB NOT NULL DEFAULT '{}'::JSONB,
    query_plan JSONB NOT NULL DEFAULT '{}'::JSONB,
    missing_input_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    clarification_questions JSONB NOT NULL DEFAULT '[]'::JSONB,
    selected_candidate JSONB,
    stage_log JSONB NOT NULL DEFAULT '[]'::JSONB,
    context_metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    database_execution JSONB NOT NULL DEFAULT '{}'::JSONB,
    message_generation JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- LLM 프롬프트 템플릿을 파일 대신 DB에서 관리합니다.
-- name은 프롬프트 파일명(예: query_plan_system.txt)을 그대로 사용하며,
-- graph_rag가 이 테이블을 최우선으로 조회합니다(없으면 파일 -> 코드 fallback).
-- 초기 데이터는 seed_prompts.py로 docs/prompts에서 시딩합니다.
CREATE TABLE campaign_prompt_templates (
    name        VARCHAR(120) PRIMARY KEY,
    content     TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 정책(JSON) 파일을 파일 대신 DB에서 관리합니다.
-- name은 정책 파일명에서 확장자를 뺀 이름(예: ctr-model-policy, heuristic-ctr-rules)이며,
-- api._load_ctr_model_policy / _load_heuristic_ctr_rules가 이 테이블을 최우선으로 조회합니다
-- (없으면 파일 -> 코드 fallback). 초기 데이터는 seed_policies.py로 docs/policies에서 시딩합니다.
CREATE TABLE campaign_policies (
    name        VARCHAR(120) PRIMARY KEY,
    content     JSONB NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_campaigns_category ON campaigns(category);
CREATE INDEX idx_campaigns_objective ON campaigns(objective);
CREATE INDEX idx_campaign_message_examples_campaign ON campaign_message_examples(campaign_id);
CREATE INDEX idx_campaign_message_examples_channel ON campaign_message_examples(channel);
CREATE INDEX idx_users_lifecycle ON users(lifecycle);
CREATE INDEX idx_users_region ON users(region);
CREATE INDEX idx_recommendation_edges_campaign ON recommendation_edges(campaign_id);
CREATE INDEX idx_campaign_target_audiences_status_created
    ON campaign_target_audiences(status, created_at);
CREATE INDEX idx_campaign_target_audiences_expires_at
    ON campaign_target_audiences(expires_at)
    WHERE expires_at IS NOT NULL;
CREATE INDEX idx_campaign_target_audience_members_user
    ON campaign_target_audience_members(audience_id, user_id);
CREATE INDEX idx_campaign_target_audience_members_campaign
    ON campaign_target_audience_members(audience_id, campaign_id)
    WHERE campaign_id IS NOT NULL;
CREATE INDEX idx_campaign_query_failure_logs_created
    ON campaign_query_failure_logs(created_at DESC);
CREATE INDEX idx_campaign_query_failure_logs_reason
    ON campaign_query_failure_logs(failure_stage, failure_reason, created_at DESC);
CREATE INDEX idx_campaign_query_failure_logs_status
    ON campaign_query_failure_logs(api_status, created_at DESC);

INSERT INTO campaigns (campaign_id, name, objective, category, offer, budget_krw, start_date, end_date, expected_ctr, expected_cvr, text_for_embedding) VALUES
  ('camp_001', '여름 장바구니 리마인드', 'purchase', 'fashion', '10% 할인 쿠폰', 3000000, '2026-07-15', '2026-07-31', 3.8, 5.2, '여름 장바구니 리마인드 캠페인. 목적은 purchase이고 카테고리는 fashion입니다. 타겟 세그먼트는 20s_female, cart_abandoner, price_sensitive이며 채널은 kakao, app_push입니다. 혜택은 10% 할인 쿠폰입니다. 키워드: 여름, 의류, 쿠폰, 장바구니.'),
  ('camp_002', '첫 구매 웰컴 쿠폰', 'first_purchase', 'all', '첫 구매 5천원 할인', 5000000, '2026-07-10', '2026-08-10', 4.5, 6.8, '첫 구매 웰컴 쿠폰 캠페인. 목적은 first_purchase이고 카테고리는 all입니다. 타겟 세그먼트는 new_user, no_purchase이며 채널은 email, app_push입니다. 혜택은 첫 구매 5천원 할인입니다. 키워드: 신규, 첫구매, 웰컴.'),
  ('camp_003', '프리미엄 뷰티 체험단', 'lead', 'beauty', '샘플 키트 무료', 7000000, '2026-07-20', '2026-08-05', 2.9, 4.1, '프리미엄 뷰티 체험단 캠페인. 목적은 lead이고 카테고리는 beauty입니다. 타겟 세그먼트는 beauty_interest, high_ltv이며 채널은 instagram, sms입니다. 혜택은 샘플 키트 무료입니다. 키워드: 뷰티, 프리미엄, 체험.'),
  ('camp_004', '휴면 고객 복귀 혜택', 'reactivation', 'all', '15% 복귀 쿠폰', 4500000, '2026-07-12', '2026-07-26', 3.2, 3.7, '휴면 고객 복귀 혜택 캠페인. 목적은 reactivation이고 카테고리는 all입니다. 타겟 세그먼트는 inactive_90d, discount_responsive이며 채널은 sms, kakao입니다. 혜택은 15% 복귀 쿠폰입니다. 키워드: 휴면, 복귀, 할인.'),
  ('camp_005', '반려동물 정기배송 추천', 'subscription', 'pet', '정기배송 첫 달 20% 할인', 2500000, '2026-07-18', '2026-08-18', 4.1, 5.9, '반려동물 정기배송 추천 캠페인. 목적은 subscription이고 카테고리는 pet입니다. 타겟 세그먼트는 pet_owner, repeat_buyer이며 채널은 app_push, email입니다. 혜택은 정기배송 첫 달 20% 할인입니다. 키워드: 반려동물, 정기배송, 사료.'),
  ('camp_006', '직장인 점심 간편식', 'purchase', 'food', '간편식 1+1', 3800000, '2026-07-14', '2026-07-28', 3.7, 4.8, '직장인 점심 간편식 캠페인. 목적은 purchase이고 카테고리는 food입니다. 타겟 세그먼트는 office_worker, weekday_active이며 채널은 kakao, app_push입니다. 혜택은 간편식 1+1입니다. 키워드: 점심, 간편식, 직장인.'),
  ('camp_007', '고관여 전자제품 비교 콘텐츠', 'consideration', 'electronics', '구매가이드 제공', 6000000, '2026-07-16', '2026-08-16', 2.4, 2.9, '고관여 전자제품 비교 콘텐츠 캠페인. 목적은 consideration이고 카테고리는 electronics입니다. 타겟 세그먼트는 electronics_interest, researcher이며 채널은 email, web_banner입니다. 혜택은 구매가이드 제공입니다. 키워드: 전자제품, 비교, 가이드.'),
  ('camp_008', '주말 여행 특가', 'purchase', 'travel', '숙박 7% 할인', 8000000, '2026-07-11', '2026-07-25', 4.0, 3.6, '주말 여행 특가 캠페인. 목적은 purchase이고 카테고리는 travel입니다. 타겟 세그먼트는 travel_interest, weekend_buyer이며 채널은 app_push, kakao입니다. 혜택은 숙박 7% 할인입니다. 키워드: 여행, 숙박, 주말.'),
  ('camp_009', '육아용품 재구매 알림', 'repurchase', 'baby', '기저귀 12% 할인', 3200000, '2026-07-13', '2026-07-30', 4.8, 6.1, '육아용품 재구매 알림 캠페인. 목적은 repurchase이고 카테고리는 baby입니다. 타겟 세그먼트는 parent, repeat_cycle_due이며 채널은 sms, app_push입니다. 혜택은 기저귀 12% 할인입니다. 키워드: 육아, 기저귀, 재구매.'),
  ('camp_010', '러닝화 신상품 런칭', 'awareness', 'sports', '런칭 기념 무료배송', 9000000, '2026-07-21', '2026-08-11', 2.7, 3.2, '러닝화 신상품 런칭 캠페인. 목적은 awareness이고 카테고리는 sports입니다. 타겟 세그먼트는 sports_interest, 20s_30s이며 채널은 instagram, web_banner입니다. 혜택은 런칭 기념 무료배송입니다. 키워드: 러닝, 운동화, 신상품.'),
  ('camp_011', 'VIP 생일 혜택', 'loyalty', 'all', '3만원 바우처', 2000000, '2026-07-01', '2026-07-31', 5.5, 7.4, 'VIP 생일 혜택 캠페인. 목적은 loyalty이고 카테고리는 all입니다. 타겟 세그먼트는 vip, birthday_month이며 채널은 email, kakao입니다. 혜택은 3만원 바우처입니다. 키워드: VIP, 생일, 바우처.'),
  ('camp_012', '가성비 생활용품 묶음', 'purchase', 'home_living', '묶음 구매 18% 할인', 4200000, '2026-07-17', '2026-08-02', 3.6, 5.0, '가성비 생활용품 묶음 캠페인. 목적은 purchase이고 카테고리는 home_living입니다. 타겟 세그먼트는 price_sensitive, bulk_buyer이며 채널은 app_push, web_banner입니다. 혜택은 묶음 구매 18% 할인입니다. 키워드: 생활용품, 가성비, 묶음.'),
  ('camp_013', '남성 그루밍 스타터', 'first_purchase', 'beauty', '스타터 세트 20% 할인', 3500000, '2026-07-19', '2026-08-09', 3.1, 4.0, '남성 그루밍 스타터 캠페인. 목적은 first_purchase이고 카테고리는 beauty입니다. 타겟 세그먼트는 male_20s_30s, beauty_beginner이며 채널은 instagram, kakao입니다. 혜택은 스타터 세트 20% 할인입니다. 키워드: 남성, 그루밍, 스타터.'),
  ('camp_014', '친환경 제품 추천', 'purchase', 'eco', '친환경 라인 무료배송', 2800000, '2026-07-22', '2026-08-12', 2.8, 3.9, '친환경 제품 추천 캠페인. 목적은 purchase이고 카테고리는 eco입니다. 타겟 세그먼트는 eco_conscious, premium_buyer이며 채널은 email, web_banner입니다. 혜택은 친환경 라인 무료배송입니다. 키워드: 친환경, 지속가능, 프리미엄.'),
  ('camp_015', '새벽배송 식품 쿠폰', 'purchase', 'food', '새벽배송 3천원 할인', 5500000, '2026-07-09', '2026-07-23', 4.3, 5.7, '새벽배송 식품 쿠폰 캠페인. 목적은 purchase이고 카테고리는 food입니다. 타겟 세그먼트는 grocery_buyer, morning_active이며 채널은 app_push, sms입니다. 혜택은 새벽배송 3천원 할인입니다. 키워드: 새벽배송, 식품, 쿠폰.'),
  ('camp_016', '콘텐츠 구독 업셀', 'upsell', 'digital_content', '프리미엄 1개월 50% 할인', 2600000, '2026-07-15', '2026-08-15', 3.9, 4.4, '콘텐츠 구독 업셀 캠페인. 목적은 upsell이고 카테고리는 digital_content입니다. 타겟 세그먼트는 free_user, content_heavy이며 채널은 email, app_push입니다. 혜택은 프리미엄 1개월 50% 할인입니다. 키워드: 구독, 프리미엄, 콘텐츠.'),
  ('camp_017', '캠핑 시즌 기획전', 'purchase', 'outdoor', '캠핑용품 최대 25%', 6500000, '2026-07-18', '2026-08-08', 3.4, 4.2, '캠핑 시즌 기획전 캠페인. 목적은 purchase이고 카테고리는 outdoor입니다. 타겟 세그먼트는 outdoor_interest, family이며 채널은 kakao, web_banner입니다. 혜택은 캠핑용품 최대 25%입니다. 키워드: 캠핑, 아웃도어, 가족.'),
  ('camp_018', '학생 노트북 추천', 'consideration', 'electronics', '학생 인증 추가 할인', 7200000, '2026-07-25', '2026-08-25', 2.6, 3.1, '학생 노트북 추천 캠페인. 목적은 consideration이고 카테고리는 electronics입니다. 타겟 세그먼트는 student, electronics_interest이며 채널은 email, instagram입니다. 혜택은 학생 인증 추가 할인입니다. 키워드: 학생, 노트북, 할인.'),
  ('camp_019', '고객 리뷰 작성 보상', 'engagement', 'all', '리뷰 작성 1천 포인트', 1800000, '2026-07-10', '2026-07-31', 5.0, 8.0, '고객 리뷰 작성 보상 캠페인. 목적은 engagement이고 카테고리는 all입니다. 타겟 세그먼트는 recent_buyer, review_likely이며 채널은 app_push, email입니다. 혜택은 리뷰 작성 1천 포인트입니다. 키워드: 리뷰, 포인트, 참여.'),
  ('camp_020', '프리미엄 와인잔 홈파티', 'purchase', 'home_living', '홈파티 세트 10% 할인', 3000000, '2026-07-20', '2026-08-03', 2.5, 3.4, '프리미엄 와인잔 홈파티 캠페인. 목적은 purchase이고 카테고리는 home_living입니다. 타겟 세그먼트는 home_party, premium_buyer이며 채널은 instagram, kakao입니다. 혜택은 홈파티 세트 10% 할인입니다. 키워드: 홈파티, 주방, 프리미엄.'),
  ('camp_021', '골프웨어 시즌오프', 'purchase', 'sports', '시즌오프 최대 30%', 4800000, '2026-07-24', '2026-08-14', 3.0, 4.5, '골프웨어 시즌오프 캠페인. 목적은 purchase이고 카테고리는 sports입니다. 타겟 세그먼트는 golf_interest, 40s_50s이며 채널은 sms, kakao입니다. 혜택은 시즌오프 최대 30%입니다. 키워드: 골프, 스포츠, 시즌오프.'),
  ('camp_022', '해외직구 관세 가이드', 'consideration', 'global_shopping', '직구 가이드와 추천템', 2200000, '2026-07-12', '2026-08-01', 2.2, 2.6, '해외직구 관세 가이드 캠페인. 목적은 consideration이고 카테고리는 global_shopping입니다. 타겟 세그먼트는 global_shopper, researcher이며 채널은 email, web_banner입니다. 혜택은 직구 가이드와 추천템입니다. 키워드: 해외직구, 가이드, 관세.'),
  ('camp_023', '모바일 앱 전용 딜', 'app_conversion', 'all', '앱 전용 타임딜', 5000000, '2026-07-16', '2026-07-22', 6.2, 7.0, '모바일 앱 전용 딜 캠페인. 목적은 app_conversion이고 카테고리는 all입니다. 타겟 세그먼트는 app_user, deal_seeker이며 채널은 app_push입니다. 혜택은 앱 전용 타임딜입니다. 키워드: 앱전용, 타임딜, 특가.'),
  ('camp_024', '부모님 건강식품 선물', 'purchase', 'health_food', '선물세트 10% 할인', 4000000, '2026-07-26', '2026-08-16', 3.3, 4.6, '부모님 건강식품 선물 캠페인. 목적은 purchase이고 카테고리는 health_food입니다. 타겟 세그먼트는 gift_buyer, 40s_50s이며 채널은 kakao, email입니다. 혜택은 선물세트 10% 할인입니다. 키워드: 건강식품, 선물, 부모님.'),
  ('camp_025', '로컬 맛집 밀키트', 'purchase', 'food', '밀키트 무료배송', 3400000, '2026-07-23', '2026-08-06', 3.9, 4.9, '로컬 맛집 밀키트 캠페인. 목적은 purchase이고 카테고리는 food입니다. 타겟 세그먼트는 foodie, local_interest이며 채널은 instagram, app_push입니다. 혜택은 밀키트 무료배송입니다. 키워드: 맛집, 밀키트, 로컬.');

INSERT INTO campaign_channels (campaign_id, channel) VALUES
  ('camp_001', 'kakao'),
  ('camp_001', 'app_push'),
  ('camp_001', 'lms'),
  ('camp_001', 'rcs'),
  ('camp_002', 'email'),
  ('camp_002', 'app_push'),
  ('camp_002', 'lms'),
  ('camp_003', 'instagram'),
  ('camp_003', 'sms'),
  ('camp_003', 'rcs'),
  ('camp_004', 'sms'),
  ('camp_004', 'kakao'),
  ('camp_004', 'lms'),
  ('camp_005', 'app_push'),
  ('camp_005', 'email'),
  ('camp_006', 'kakao'),
  ('camp_006', 'app_push'),
  ('camp_007', 'email'),
  ('camp_007', 'web_banner'),
  ('camp_008', 'app_push'),
  ('camp_008', 'kakao'),
  ('camp_009', 'sms'),
  ('camp_009', 'app_push'),
    ('camp_009', 'rcs'),
    ('camp_009', 'lms'),
  ('camp_010', 'instagram'),
  ('camp_010', 'web_banner'),
  ('camp_011', 'email'),
  ('camp_011', 'kakao'),
  ('camp_012', 'app_push'),
  ('camp_012', 'web_banner'),
  ('camp_013', 'instagram'),
  ('camp_013', 'kakao'),
  ('camp_014', 'email'),
  ('camp_014', 'web_banner'),
  ('camp_015', 'app_push'),
  ('camp_015', 'sms'),
  ('camp_016', 'email'),
  ('camp_016', 'app_push'),
  ('camp_017', 'kakao'),
  ('camp_017', 'web_banner'),
  ('camp_018', 'email'),
  ('camp_018', 'instagram'),
  ('camp_019', 'app_push'),
  ('camp_019', 'email'),
  ('camp_020', 'instagram'),
  ('camp_020', 'kakao'),
  ('camp_021', 'sms'),
  ('camp_021', 'kakao'),
  ('camp_022', 'email'),
  ('camp_022', 'web_banner'),
  ('camp_023', 'app_push'),
  ('camp_024', 'kakao'),
  ('camp_024', 'email'),
  ('camp_025', 'instagram'),
  ('camp_025', 'app_push');

INSERT INTO campaign_message_examples (example_id, campaign_id, channel, emphasis_type, message_text, brand_tone, created_at) VALUES
  ('msg_001_benefit', 'camp_001', 'lms', 'benefit_emphasis', '[여름 장바구니 리마인드] 담아둔 상품을 10% 할인 쿠폰으로 다시 만나보세요.', '친근하고 명확한 쇼핑 혜택 톤', '2026-07-11 09:00:00'),
  ('msg_001_urgency', 'camp_001', 'rcs', 'urgency_emphasis', '여름 장바구니 혜택이 곧 종료됩니다. 10% 할인 쿠폰을 기간 내 확인해 보세요.', '친근하고 명확한 쇼핑 혜택 톤', '2026-07-11 09:05:00'),
  ('msg_004_emotion', 'camp_004', 'lms', 'emotion_emphasis', '오랜만에 다시 만나는 고객님께 15% 복귀 쿠폰을 준비했습니다.', '따뜻하고 재방문을 권하는 톤', '2026-07-11 09:10:00'),
    ('msg_009_benefit', 'camp_009', 'lms', 'benefit_emphasis', '[육아용품 재구매 알림] 장바구니에 담아둔 상품을 12% 혜택으로 다시 만나보세요.', '친근하고 명확한 쇼핑 혜택 톤', '2026-07-11 09:11:00'),
    ('msg_009_urgency', 'camp_009', 'rcs', 'urgency_emphasis', '담아둔 육아용품을 12% 혜택으로 다시 만나보세요. 재고 소진 전 확인해 보세요.', '친근하고 명확한 쇼핑 혜택 톤', '2026-07-11 09:12:00'),
  ('msg_015_benefit', 'camp_015', 'rcs', 'benefit_emphasis', '새벽배송 식품 쿠폰으로 오늘 필요한 장보기를 3천원 할인받아 보세요.', '실용적이고 간결한 생활 혜택 톤', '2026-07-11 09:15:00');

INSERT INTO campaign_target_segments (campaign_id, target_segment) VALUES
  ('camp_001', '20s_female'),
  ('camp_001', 'cart_abandoner'),
  ('camp_001', 'price_sensitive'),
  ('camp_002', 'new_user'),
  ('camp_002', 'no_purchase'),
  ('camp_003', 'beauty_interest'),
  ('camp_003', 'high_ltv'),
  ('camp_004', 'inactive_90d'),
  ('camp_004', 'discount_responsive'),
  ('camp_005', 'pet_owner'),
  ('camp_005', 'repeat_buyer'),
  ('camp_006', 'office_worker'),
  ('camp_006', 'weekday_active'),
  ('camp_007', 'electronics_interest'),
  ('camp_007', 'researcher'),
  ('camp_008', 'travel_interest'),
  ('camp_008', 'weekend_buyer'),
    ('camp_009', 'cart_abandoner'),
  ('camp_009', 'parent'),
  ('camp_009', 'repeat_cycle_due'),
  ('camp_010', 'sports_interest'),
  ('camp_010', '20s_30s'),
  ('camp_011', 'vip'),
  ('camp_011', 'birthday_month'),
  ('camp_012', 'price_sensitive'),
  ('camp_012', 'bulk_buyer'),
  ('camp_013', 'male_20s_30s'),
  ('camp_013', 'beauty_beginner'),
  ('camp_014', 'eco_conscious'),
  ('camp_014', 'premium_buyer'),
  ('camp_015', 'grocery_buyer'),
  ('camp_015', 'morning_active'),
  ('camp_016', 'free_user'),
  ('camp_016', 'content_heavy'),
  ('camp_017', 'outdoor_interest'),
  ('camp_017', 'family'),
  ('camp_018', 'student'),
  ('camp_018', 'electronics_interest'),
  ('camp_019', 'recent_buyer'),
  ('camp_019', 'review_likely'),
  ('camp_020', 'home_party'),
  ('camp_020', 'premium_buyer'),
  ('camp_021', 'golf_interest'),
  ('camp_021', '40s_50s'),
  ('camp_022', 'global_shopper'),
  ('camp_022', 'researcher'),
  ('camp_023', 'app_user'),
  ('camp_023', 'deal_seeker'),
  ('camp_024', 'gift_buyer'),
  ('camp_024', '40s_50s'),
  ('camp_025', 'foodie'),
  ('camp_025', 'local_interest');

INSERT INTO campaign_keywords (campaign_id, keyword) VALUES
  ('camp_001', '여름'),
  ('camp_001', '의류'),
  ('camp_001', '쿠폰'),
  ('camp_001', '장바구니'),
  ('camp_002', '신규'),
  ('camp_002', '첫구매'),
  ('camp_002', '웰컴'),
  ('camp_003', '뷰티'),
  ('camp_003', '프리미엄'),
  ('camp_003', '체험'),
  ('camp_004', '휴면'),
  ('camp_004', '복귀'),
  ('camp_004', '할인'),
  ('camp_005', '반려동물'),
  ('camp_005', '정기배송'),
  ('camp_005', '사료'),
  ('camp_006', '점심'),
  ('camp_006', '간편식'),
  ('camp_006', '직장인'),
  ('camp_007', '전자제품'),
  ('camp_007', '비교'),
  ('camp_007', '가이드'),
  ('camp_008', '여행'),
  ('camp_008', '숙박'),
  ('camp_008', '주말'),
  ('camp_009', '육아'),
  ('camp_009', '기저귀'),
  ('camp_009', '재구매'),
  ('camp_010', '러닝'),
  ('camp_010', '운동화'),
  ('camp_010', '신상품'),
  ('camp_011', 'VIP'),
  ('camp_011', '생일'),
  ('camp_011', '바우처'),
  ('camp_012', '생활용품'),
  ('camp_012', '가성비'),
  ('camp_012', '묶음'),
  ('camp_013', '남성'),
  ('camp_013', '그루밍'),
  ('camp_013', '스타터'),
  ('camp_014', '친환경'),
  ('camp_014', '지속가능'),
  ('camp_014', '프리미엄'),
  ('camp_015', '새벽배송'),
  ('camp_015', '식품'),
  ('camp_015', '쿠폰'),
  ('camp_016', '구독'),
  ('camp_016', '프리미엄'),
  ('camp_016', '콘텐츠'),
  ('camp_017', '캠핑'),
  ('camp_017', '아웃도어'),
  ('camp_017', '가족'),
  ('camp_018', '학생'),
  ('camp_018', '노트북'),
  ('camp_018', '할인'),
  ('camp_019', '리뷰'),
  ('camp_019', '포인트'),
  ('camp_019', '참여'),
  ('camp_020', '홈파티'),
  ('camp_020', '주방'),
  ('camp_020', '프리미엄'),
  ('camp_021', '골프'),
  ('camp_021', '스포츠'),
  ('camp_021', '시즌오프'),
  ('camp_022', '해외직구'),
  ('camp_022', '가이드'),
  ('camp_022', '관세'),
  ('camp_023', '앱전용'),
  ('camp_023', '타임딜'),
  ('camp_023', '특가'),
  ('camp_024', '건강식품'),
  ('camp_024', '선물'),
  ('camp_024', '부모님'),
  ('camp_025', '맛집'),
  ('camp_025', '밀키트'),
  ('camp_025', '로컬');

INSERT INTO users (user_id, age, gender, region, lifecycle, avg_order_value_krw, purchase_count_90d, last_active_days, price_sensitivity, predicted_ltv_segment, text_for_embedding) VALUES
  ('user_001', 27, 'female', 'Seoul', 'active', 68000, 4, 1, 'high', 'mid', 'user_001 사용자는 27세 female이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 fashion, beauty, travel입니다. 선호 채널은 app_push, kakao이며 최근 행동은 cart_abandoned:fashion, clicked:coupon입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_002', 34, 'male', 'Gyeonggi', 'new', 120000, 0, 2, 'medium', 'unknown', 'user_002 사용자는 34세 male이며 지역은 Gyeonggi입니다. 라이프사이클은 new이고 관심사는 electronics, sports, digital_content입니다. 선호 채널은 email이며 최근 행동은 viewed:laptop, signed_up입니다. 가격 민감도는 medium, LTV 세그먼트는 unknown입니다.'),
  ('user_003', 41, 'female', 'Busan', 'active', 54000, 7, 0, 'high', 'high', 'user_003 사용자는 41세 female이며 지역은 Busan입니다. 라이프사이클은 active이고 관심사는 baby, home_living, food입니다. 선호 채널은 sms, app_push이며 최근 행동은 purchased:diaper, repeat_cycle_due:baby입니다. 가격 민감도는 high, LTV 세그먼트는 high입니다.'),
  ('user_004', 52, 'male', 'Daegu', 'active', 145000, 3, 5, 'low', 'high', 'user_004 사용자는 52세 male이며 지역은 Daegu입니다. 라이프사이클은 active이고 관심사는 golf, health_food, travel입니다. 선호 채널은 kakao, sms이며 최근 행동은 clicked:golfwear, searched:gift입니다. 가격 민감도는 low, LTV 세그먼트는 high입니다.'),
  ('user_005', 29, 'female', 'Incheon', 'inactive_90d', 39000, 0, 96, 'high', 'low', 'user_005 사용자는 29세 female이며 지역은 Incheon입니다. 라이프사이클은 inactive_90d이고 관심사는 beauty, eco, fashion입니다. 선호 채널은 kakao이며 최근 행동은 inactive, previous_purchase:beauty입니다. 가격 민감도는 high, LTV 세그먼트는 low입니다.'),
  ('user_006', 31, 'male', 'Seoul', 'active', 43000, 5, 1, 'medium', 'mid', 'user_006 사용자는 31세 male이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 food, home_living, digital_content입니다. 선호 채널은 app_push이며 최근 행동은 weekday_lunch_browse, uses_app_daily입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_007', 24, 'female', 'Daejeon', 'new', 32000, 1, 3, 'high', 'mid', 'user_007 사용자는 24세 female이며 지역은 Daejeon입니다. 라이프사이클은 new이고 관심사는 sports, fashion, beauty입니다. 선호 채널은 instagram, app_push이며 최근 행동은 followed:brand_instagram, clicked:running입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_008', 38, 'female', 'Seoul', 'active', 76000, 6, 2, 'medium', 'high', 'user_008 사용자는 38세 female이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 pet, home_living, eco입니다. 선호 채널은 email, app_push이며 최근 행동은 purchased:pet_food, repeat_buyer입니다. 가격 민감도는 medium, LTV 세그먼트는 high입니다.'),
  ('user_009', 46, 'male', 'Gwangju', 'active', 98000, 2, 4, 'medium', 'mid', 'user_009 사용자는 46세 male이며 지역은 Gwangju입니다. 라이프사이클은 active이고 관심사는 outdoor, travel, food입니다. 선호 채널은 kakao이며 최근 행동은 viewed:camping_tent, weekend_browse입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_010', 22, 'female', 'Seoul', 'active', 87000, 2, 1, 'high', 'mid', 'user_010 사용자는 22세 female이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 electronics, digital_content, global_shopping입니다. 선호 채널은 email, instagram이며 최근 행동은 student_verified, viewed:notebook입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_011', 55, 'female', 'Ulsan', 'vip', 160000, 8, 0, 'low', 'vip', 'user_011 사용자는 55세 female이며 지역은 Ulsan입니다. 라이프사이클은 vip이고 관심사는 health_food, home_living, travel입니다. 선호 채널은 kakao, email이며 최근 행동은 birthday_month, gift_buyer입니다. 가격 민감도는 low, LTV 세그먼트는 vip입니다.'),
  ('user_012', 33, 'male', 'Seoul', 'free_user', 25000, 1, 0, 'medium', 'mid', 'user_012 사용자는 33세 male이며 지역은 Seoul입니다. 라이프사이클은 free_user이고 관심사는 digital_content, electronics, sports입니다. 선호 채널은 app_push, email이며 최근 행동은 content_heavy, trial_expired입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_013', 36, 'female', 'Jeju', 'active', 61000, 4, 2, 'medium', 'mid', 'user_013 사용자는 36세 female이며 지역은 Jeju입니다. 라이프사이클은 active이고 관심사는 travel, food, local_interest입니다. 선호 채널은 app_push, instagram이며 최근 행동은 searched:hotel, clicked:milkit입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_014', 28, 'male', 'Busan', 'active', 47000, 3, 1, 'high', 'mid', 'user_014 사용자는 28세 male이며 지역은 Busan입니다. 라이프사이클은 active이고 관심사는 beauty, fashion, sports입니다. 선호 채널은 instagram, kakao이며 최근 행동은 viewed:men_grooming, clicked:starter_set입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_015', 44, 'female', 'Gyeonggi', 'active', 91000, 5, 6, 'low', 'high', 'user_015 사용자는 44세 female이며 지역은 Gyeonggi입니다. 라이프사이클은 active이고 관심사는 eco, home_living, baby입니다. 선호 채널은 email이며 최근 행동은 purchased:eco_cleaner, premium_buyer입니다. 가격 민감도는 low, LTV 세그먼트는 high입니다.'),
  ('user_016', 30, 'female', 'Seoul', 'cart_abandoner', 58000, 2, 0, 'high', 'mid', 'user_016 사용자는 30세 female이며 지역은 Seoul입니다. 라이프사이클은 cart_abandoner이고 관심사는 fashion, home_living, food입니다. 선호 채널은 app_push, kakao이며 최근 행동은 cart_abandoned:fashion, deal_seeker입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_017', 49, 'male', 'Incheon', 'inactive_90d', 130000, 0, 112, 'medium', 'mid', 'user_017 사용자는 49세 male이며 지역은 Incheon입니다. 라이프사이클은 inactive_90d이고 관심사는 electronics, global_shopping, golf입니다. 선호 채널은 sms, email이며 최근 행동은 inactive, viewed:overseas_product입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_018', 26, 'female', 'Daegu', 'active', 36000, 6, 0, 'high', 'mid', 'user_018 사용자는 26세 female이며 지역은 Daegu입니다. 라이프사이클은 active이고 관심사는 food, beauty, fashion입니다. 선호 채널은 app_push이며 최근 행동은 morning_active, purchased:grocery입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.'),
  ('user_019', 39, 'male', 'Seoul', 'recent_buyer', 83000, 4, 3, 'medium', 'mid', 'user_019 사용자는 39세 male이며 지역은 Seoul입니다. 라이프사이클은 recent_buyer이고 관심사는 home_living, food, outdoor입니다. 선호 채널은 email, app_push이며 최근 행동은 recent_purchase:camping_chair, review_likely입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_020', 57, 'female', 'Daejeon', 'vip', 175000, 9, 1, 'low', 'vip', 'user_020 사용자는 57세 female이며 지역은 Daejeon입니다. 라이프사이클은 vip이고 관심사는 home_living, health_food, golf입니다. 선호 채널은 kakao이며 최근 행동은 home_party, premium_buyer입니다. 가격 민감도는 low, LTV 세그먼트는 vip입니다.'),
  ('user_021', 23, 'male', 'Seoul', 'new', 51000, 0, 2, 'high', 'unknown', 'user_021 사용자는 23세 male이며 지역은 Seoul입니다. 라이프사이클은 new이고 관심사는 global_shopping, electronics, sports입니다. 선호 채널은 instagram, email이며 최근 행동은 searched:customs, viewed:running_shoes입니다. 가격 민감도는 high, LTV 세그먼트는 unknown입니다.'),
  ('user_022', 35, 'female', 'Gyeonggi', 'active', 69000, 6, 0, 'medium', 'high', 'user_022 사용자는 35세 female이며 지역은 Gyeonggi입니다. 라이프사이클은 active이고 관심사는 pet, food, travel입니다. 선호 채널은 app_push, kakao이며 최근 행동은 repeat_buyer:pet, weekend_buyer입니다. 가격 민감도는 medium, LTV 세그먼트는 high입니다.'),
  ('user_023', 42, 'male', 'Busan', 'active', 74000, 3, 4, 'medium', 'mid', 'user_023 사용자는 42세 male이며 지역은 Busan입니다. 라이프사이클은 active이고 관심사는 food, health_food, home_living입니다. 선호 채널은 sms, kakao이며 최근 행동은 gift_buyer, clicked:health_set입니다. 가격 민감도는 medium, LTV 세그먼트는 mid입니다.'),
  ('user_024', 32, 'female', 'Seoul', 'active', 88000, 5, 1, 'low', 'high', 'user_024 사용자는 32세 female이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 beauty, eco, digital_content입니다. 선호 채널은 email, instagram이며 최근 행동은 high_ltv, clicked:premium_beauty입니다. 가격 민감도는 low, LTV 세그먼트는 high입니다.'),
  ('user_025', 37, 'male', 'Gwangju', 'app_user', 57000, 4, 0, 'high', 'mid', 'user_025 사용자는 37세 male이며 지역은 Gwangju입니다. 라이프사이클은 app_user이고 관심사는 food, home_living, sports입니다. 선호 채널은 app_push이며 최근 행동은 deal_seeker, uses_app_daily입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다.');

INSERT INTO user_interests (user_id, interest) VALUES
  ('user_001', 'fashion'),
  ('user_001', 'beauty'),
  ('user_001', 'travel'),
  ('user_002', 'electronics'),
  ('user_002', 'sports'),
  ('user_002', 'digital_content'),
  ('user_003', 'baby'),
  ('user_003', 'home_living'),
  ('user_003', 'food'),
  ('user_004', 'golf'),
  ('user_004', 'health_food'),
  ('user_004', 'travel'),
  ('user_005', 'beauty'),
  ('user_005', 'eco'),
  ('user_005', 'fashion'),
  ('user_006', 'food'),
  ('user_006', 'home_living'),
  ('user_006', 'digital_content'),
  ('user_007', 'sports'),
  ('user_007', 'fashion'),
  ('user_007', 'beauty'),
  ('user_008', 'pet'),
  ('user_008', 'home_living'),
  ('user_008', 'eco'),
  ('user_009', 'outdoor'),
  ('user_009', 'travel'),
  ('user_009', 'food'),
  ('user_010', 'electronics'),
  ('user_010', 'digital_content'),
  ('user_010', 'global_shopping'),
  ('user_011', 'health_food'),
  ('user_011', 'home_living'),
  ('user_011', 'travel'),
  ('user_012', 'digital_content'),
  ('user_012', 'electronics'),
  ('user_012', 'sports'),
  ('user_013', 'travel'),
  ('user_013', 'food'),
  ('user_013', 'local_interest'),
  ('user_014', 'beauty'),
  ('user_014', 'fashion'),
  ('user_014', 'sports'),
  ('user_015', 'eco'),
  ('user_015', 'home_living'),
  ('user_015', 'baby'),
  ('user_016', 'fashion'),
  ('user_016', 'home_living'),
  ('user_016', 'food'),
  ('user_017', 'electronics'),
  ('user_017', 'global_shopping'),
  ('user_017', 'golf'),
  ('user_018', 'food'),
  ('user_018', 'beauty'),
  ('user_018', 'fashion'),
  ('user_019', 'home_living'),
  ('user_019', 'food'),
  ('user_019', 'outdoor'),
  ('user_020', 'home_living'),
  ('user_020', 'health_food'),
  ('user_020', 'golf'),
  ('user_021', 'global_shopping'),
  ('user_021', 'electronics'),
  ('user_021', 'sports'),
  ('user_022', 'pet'),
  ('user_022', 'food'),
  ('user_022', 'travel'),
  ('user_023', 'food'),
  ('user_023', 'health_food'),
  ('user_023', 'home_living'),
  ('user_024', 'beauty'),
  ('user_024', 'eco'),
  ('user_024', 'digital_content'),
  ('user_025', 'food'),
  ('user_025', 'home_living'),
  ('user_025', 'sports');

INSERT INTO user_preferred_channels (user_id, preferred_channel) VALUES
  ('user_001', 'app_push'),
  ('user_001', 'kakao'),
  ('user_001', 'lms'),
  ('user_001', 'rcs'),
  ('user_002', 'email'),
  ('user_002', 'lms'),
  ('user_003', 'sms'),
  ('user_003', 'app_push'),
  ('user_003', 'rcs'),
  ('user_004', 'kakao'),
  ('user_004', 'sms'),
  ('user_004', 'lms'),
  ('user_005', 'kakao'),
  ('user_006', 'app_push'),
  ('user_007', 'instagram'),
  ('user_007', 'app_push'),
  ('user_008', 'email'),
  ('user_008', 'app_push'),
  ('user_009', 'kakao'),
  ('user_010', 'email'),
  ('user_010', 'instagram'),
  ('user_011', 'kakao'),
  ('user_011', 'email'),
  ('user_012', 'app_push'),
  ('user_012', 'email'),
  ('user_013', 'app_push'),
  ('user_013', 'instagram'),
  ('user_014', 'instagram'),
  ('user_014', 'kakao'),
  ('user_015', 'email'),
  ('user_016', 'app_push'),
  ('user_016', 'kakao'),
  ('user_016', 'lms'),
  ('user_017', 'sms'),
  ('user_017', 'email'),
  ('user_018', 'app_push'),
  ('user_019', 'email'),
  ('user_019', 'app_push'),
  ('user_020', 'kakao'),
  ('user_021', 'instagram'),
  ('user_021', 'email'),
  ('user_022', 'app_push'),
  ('user_022', 'kakao'),
  ('user_023', 'sms'),
  ('user_023', 'kakao'),
  ('user_024', 'email'),
  ('user_024', 'instagram'),
  ('user_025', 'app_push');

INSERT INTO user_recent_behaviors (user_id, behavior) VALUES
  ('user_001', 'cart_abandoned:fashion'),
  ('user_001', 'clicked:coupon'),
  ('user_002', 'viewed:laptop'),
  ('user_002', 'signed_up'),
  ('user_003', 'purchased:diaper'),
  ('user_003', 'repeat_cycle_due:baby'),
  ('user_004', 'clicked:golfwear'),
  ('user_004', 'searched:gift'),
  ('user_005', 'inactive'),
  ('user_005', 'previous_purchase:beauty'),
  ('user_006', 'weekday_lunch_browse'),
  ('user_006', 'uses_app_daily'),
  ('user_007', 'followed:brand_instagram'),
  ('user_007', 'clicked:running'),
  ('user_008', 'purchased:pet_food'),
  ('user_008', 'repeat_buyer'),
  ('user_009', 'viewed:camping_tent'),
  ('user_009', 'weekend_browse'),
  ('user_010', 'student_verified'),
  ('user_010', 'viewed:notebook'),
  ('user_011', 'birthday_month'),
  ('user_011', 'gift_buyer'),
  ('user_012', 'content_heavy'),
  ('user_012', 'trial_expired'),
  ('user_013', 'searched:hotel'),
  ('user_013', 'clicked:milkit'),
  ('user_014', 'viewed:men_grooming'),
  ('user_014', 'clicked:starter_set'),
  ('user_015', 'purchased:eco_cleaner'),
  ('user_015', 'premium_buyer'),
  ('user_016', 'cart_abandoned:fashion'),
  ('user_016', 'deal_seeker'),
  ('user_017', 'inactive'),
  ('user_017', 'viewed:overseas_product'),
  ('user_018', 'morning_active'),
  ('user_018', 'purchased:grocery'),
  ('user_019', 'recent_purchase:camping_chair'),
  ('user_019', 'review_likely'),
  ('user_020', 'home_party'),
  ('user_020', 'premium_buyer'),
  ('user_021', 'searched:customs'),
  ('user_021', 'viewed:running_shoes'),
  ('user_022', 'repeat_buyer:pet'),
  ('user_022', 'weekend_buyer'),
  ('user_023', 'gift_buyer'),
  ('user_023', 'clicked:health_set'),
  ('user_024', 'high_ltv'),
  ('user_024', 'clicked:premium_beauty'),
  ('user_025', 'deal_seeker'),
  ('user_025', 'uses_app_daily');

INSERT INTO recommendation_edges (user_id, campaign_id, reason, label) VALUES
  ('user_001', 'camp_001', 'fashion cart_abandoner price_sensitive channel match', 'high'),
  ('user_002', 'camp_018', 'student electronics notebook interest', 'high'),
  ('user_003', 'camp_009', 'parent repeat_cycle_due baby category', 'high'),
  ('user_004', 'camp_021', 'golf interest 40s_50s segment', 'high'),
  ('user_005', 'camp_004', 'inactive_90d discount responsive', 'high'),
  ('user_006', 'camp_006', 'office weekday lunch behavior', 'high'),
  ('user_007', 'camp_010', 'sports running instagram', 'medium'),
  ('user_008', 'camp_005', 'pet owner repeat buyer', 'high'),
  ('user_009', 'camp_017', 'outdoor camping family-like interest', 'high'),
  ('user_010', 'camp_018', 'student notebook electronics', 'high'),
  ('user_011', 'camp_011', 'vip birthday_month', 'high'),
  ('user_012', 'camp_016', 'free_user content_heavy trial_expired', 'high'),
  ('user_013', 'camp_008', 'travel weekend buyer hotel search', 'medium'),
  ('user_014', 'camp_013', 'male grooming starter behavior', 'high'),
  ('user_015', 'camp_014', 'eco conscious premium buyer', 'high'),
  ('user_016', 'camp_023', 'deal seeker app user push channel', 'high'),
  ('user_017', 'camp_022', 'global shopping inactive consideration', 'medium'),
  ('user_018', 'camp_015', 'morning active grocery buyer', 'high'),
  ('user_019', 'camp_019', 'recent buyer review likely', 'high'),
  ('user_020', 'camp_020', 'home party premium buyer', 'high'),
  ('user_021', 'camp_022', 'global shopper customs search', 'high'),
  ('user_022', 'camp_005', 'pet repeat buyer', 'high'),
  ('user_023', 'camp_024', 'gift buyer health food', 'high'),
  ('user_024', 'camp_003', 'beauty high_ltv premium interest', 'high'),
  ('user_025', 'camp_023', 'app user deal seeker', 'high');

-- VIP 대상 신제품(awareness) 캠페인 시드.
-- "VIP 등급 고객에게 신제품 출시 소식" 프롬프트가 실제 타겟팅 결과를 반환하도록,
-- VIP 라이프사이클 유저(user_011/user_020)를 awareness 캠페인에 연결한다.
INSERT INTO campaigns (campaign_id, name, objective, category, offer, budget_krw, start_date, end_date, expected_ctr, expected_cvr, text_for_embedding) VALUES
  ('camp_026', 'VIP 신제품 프리뷰', 'awareness', 'all', 'VIP 전용 선공개 프리뷰', 3600000, '2026-07-20', '2026-08-10', 4.2, 5.1, 'VIP 신제품 프리뷰 캠페인. 목적은 awareness이고 카테고리는 all입니다. 타겟 세그먼트는 vip, premium_buyer이며 채널은 rcs, kakao입니다. 혜택은 VIP 전용 선공개 프리뷰입니다. 키워드: 신제품, 출시, VIP, 프리뷰.');

INSERT INTO campaign_channels (campaign_id, channel) VALUES
  ('camp_026', 'rcs'),
  ('camp_026', 'kakao');

INSERT INTO campaign_target_segments (campaign_id, target_segment) VALUES
  ('camp_026', 'vip'),
  ('camp_026', 'premium_buyer');

INSERT INTO campaign_keywords (campaign_id, keyword) VALUES
  ('camp_026', '신제품'),
  ('camp_026', '출시'),
  ('camp_026', 'VIP'),
  ('camp_026', '프리뷰');

INSERT INTO recommendation_edges (user_id, campaign_id, reason, label) VALUES
  ('user_011', 'camp_026', 'vip new product preview', 'high'),
  ('user_020', 'camp_026', 'vip premium buyer new launch', 'high');

-- Basic validation queries
-- SELECT COUNT(*) AS campaign_count FROM campaigns;
-- SELECT COUNT(*) AS user_count FROM users;
-- SELECT COUNT(*) AS recommendation_edge_count FROM recommendation_edges;


-- ============================================================
-- Campaign Message History
-- ============================================================

DROP TABLE IF EXISTS campaign_channel_messages CASCADE;

CREATE TABLE campaign_channel_messages (
    message_id BIGSERIAL PRIMARY KEY,
    campaign_id VARCHAR(20) NOT NULL,
    channel VARCHAR(50) NOT NULL,
    send_type VARCHAR(10) NOT NULL
        CHECK (send_type IN ('LMS', 'RCS')),
    message_body TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    send_status VARCHAR(20) NOT NULL DEFAULT 'sent'
        CHECK (send_status IN ('pending','sent','failed','cancelled')),
    provider_message_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_campaign_channel_messages
        FOREIGN KEY (campaign_id, channel)
        REFERENCES campaign_channels(campaign_id, channel)
        ON DELETE CASCADE
);

CREATE INDEX idx_campaign_channel_messages_campaign
    ON campaign_channel_messages(campaign_id);

CREATE INDEX idx_campaign_channel_messages_sent_at
    ON campaign_channel_messages(sent_at);

INSERT INTO campaign_channel_messages
(campaign_id, channel, send_type, message_body, sent_at, send_status, provider_message_id)
VALUES
('camp_004','sms','LMS',
'[복귀 고객 전용] 오랜만입니다. 다시 방문하시면 15% 복귀 쿠폰을 드립니다.',
'2026-07-12 11:30:00+09','sent','lms-camp004-001'),

('camp_011','kakao','RCS',
'[VIP 생일 혜택] 생일을 축하드립니다. 3만원 바우처가 지급되었습니다.',
'2026-07-05 09:00:00+09','sent','rcs-camp011-001'),

('camp_015','sms','LMS',
'[새벽배송] 오늘 자정까지 3천원 할인쿠폰을 사용하세요.',
'2026-07-10 08:10:00+09','sent','lms-camp015-001');

-- ============================================================
-- CTR / A-B-C Experiment Analytics Extension
-- ============================================================

-- 캠페인 안에서 수행되는 A/B/C 테스트의 실행 단위입니다.
CREATE TABLE campaign_experiments (
    experiment_id BIGSERIAL PRIMARY KEY,
    campaign_id VARCHAR(20) NOT NULL
        REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    experiment_name VARCHAR(200) NOT NULL,
    channel VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'running', 'paused', 'completed', 'cancelled')),
    assignment_method VARCHAR(30) NOT NULL DEFAULT 'random'
        CHECK (assignment_method IN ('random', 'weighted_random', 'manual', 'model')),
    primary_metric VARCHAR(30) NOT NULL DEFAULT 'ctr'
        CHECK (primary_metric IN ('delivery_rate', 'impression_rate', 'open_rate', 'ctr', 'cvr', 'revenue')),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_campaign_experiment_name
        UNIQUE (campaign_id, experiment_name),

    CONSTRAINT fk_campaign_experiment_channel
        FOREIGN KEY (campaign_id, channel)
        REFERENCES campaign_channels(campaign_id, channel)
        ON DELETE CASCADE,

    CONSTRAINT chk_campaign_experiment_period
        CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)
);

-- 실제 사용자에게 노출되는 메시지 A/B/C 버전입니다.
CREATE TABLE campaign_message_variants (
    variant_id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL
        REFERENCES campaign_experiments(experiment_id) ON DELETE CASCADE,
    variant_code VARCHAR(20) NOT NULL,
    message_name VARCHAR(100) NOT NULL,
    message_body TEXT NOT NULL,
    landing_url TEXT,
    allocation_weight NUMERIC(7,4) NOT NULL DEFAULT 1.0000
        CHECK (allocation_weight > 0),
    is_control BOOLEAN NOT NULL DEFAULT FALSE,
    ai_features JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_experiment_variant_code
        UNIQUE (experiment_id, variant_code)
);

-- 사용자 한 명에게 특정 메시지 버전을 배정하고 발송한 사실입니다.
-- 분석 분모를 안정적으로 유지하기 위해 이벤트와 분리합니다.
CREATE TABLE campaign_message_deliveries (
    delivery_id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL
        REFERENCES campaign_experiments(experiment_id) ON DELETE CASCADE,
    variant_id BIGINT NOT NULL
        REFERENCES campaign_message_variants(variant_id) ON DELETE RESTRICT,
    campaign_id VARCHAR(20) NOT NULL
        REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    user_id VARCHAR(20) NOT NULL
        REFERENCES users(user_id) ON DELETE CASCADE,
    channel VARCHAR(50) NOT NULL,
    assignment_source VARCHAR(30) NOT NULL DEFAULT 'random'
        CHECK (assignment_source IN ('random', 'weighted_random', 'manual', 'model')),
    model_version VARCHAR(100),
    predicted_click_probability NUMERIC(8,7)
        CHECK (
            predicted_click_probability IS NULL
            OR predicted_click_probability BETWEEN 0 AND 1
        ),
    provider_message_id VARCHAR(100),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    requested_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    final_status VARCHAR(20) NOT NULL DEFAULT 'assigned'
        CHECK (final_status IN (
            'assigned', 'requested', 'sent', 'delivered',
            'bounced', 'failed', 'cancelled'
        )),
    targeting_snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_experiment_user_assignment
        UNIQUE (experiment_id, user_id),

    CONSTRAINT fk_delivery_campaign_channel
        FOREIGN KEY (campaign_id, channel)
        REFERENCES campaign_channels(campaign_id, channel)
        ON DELETE CASCADE
);

-- 원본 이벤트 로그입니다. 동일 이벤트 재수신 시 event_key로 중복을 차단합니다.
CREATE TABLE campaign_message_events (
    event_id BIGSERIAL PRIMARY KEY,
    delivery_id BIGINT NOT NULL
        REFERENCES campaign_message_deliveries(delivery_id) ON DELETE CASCADE,
    event_type VARCHAR(30) NOT NULL
        CHECK (event_type IN (
            'send_requested',
            'sent',
            'delivered',
            'impression',
            'open',
            'click',
            'conversion',
            'bounce',
            'failed',
            'unsubscribe'
        )),
    event_at TIMESTAMPTZ NOT NULL,
    event_key VARCHAR(200) NOT NULL,
    provider_event_id VARCHAR(150),
    click_url TEXT,
    conversion_type VARCHAR(50),
    conversion_value_krw NUMERIC(14,2)
        CHECK (conversion_value_krw IS NULL OR conversion_value_krw >= 0),
    device_type VARCHAR(30),
    os_name VARCHAR(50),
    browser_name VARCHAR(50),
    ip_hash VARCHAR(128),
    user_agent TEXT,
    event_properties JSONB NOT NULL DEFAULT '{}'::JSONB,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_campaign_message_event_key UNIQUE (event_key)
);

CREATE INDEX idx_campaign_experiments_campaign_status
    ON campaign_experiments(campaign_id, status);

CREATE INDEX idx_campaign_message_variants_experiment
    ON campaign_message_variants(experiment_id);

CREATE INDEX idx_campaign_message_deliveries_campaign
    ON campaign_message_deliveries(campaign_id);

CREATE INDEX idx_campaign_message_deliveries_experiment_variant
    ON campaign_message_deliveries(experiment_id, variant_id);

CREATE INDEX idx_campaign_message_deliveries_user
    ON campaign_message_deliveries(user_id);

CREATE INDEX idx_campaign_message_deliveries_sent_at
    ON campaign_message_deliveries(sent_at);

CREATE INDEX idx_campaign_message_events_delivery_type_time
    ON campaign_message_events(delivery_id, event_type, event_at);

CREATE INDEX idx_campaign_message_events_type_time
    ON campaign_message_events(event_type, event_at);

CREATE INDEX idx_campaign_message_events_event_at_brin
    ON campaign_message_events USING BRIN(event_at);

CREATE INDEX idx_campaign_message_events_properties_gin
    ON campaign_message_events USING GIN(event_properties);


-- ============================================================
-- Analytics Views
-- ============================================================

-- 메시지 버전별 주요 퍼널 지표입니다.
-- CTR 분모는 impression이며, impression이 없는 채널은 별도로
-- delivered_ctr 컬럼을 참고할 수 있습니다.
CREATE VIEW v_campaign_variant_metrics AS
WITH event_flags AS (
    SELECT
        d.delivery_id,
        d.campaign_id,
        d.experiment_id,
        d.variant_id,
        d.channel,
        d.user_id,
        BOOL_OR(e.event_type = 'sent') AS was_sent,
        BOOL_OR(e.event_type = 'delivered') AS was_delivered,
        BOOL_OR(e.event_type = 'impression') AS was_impressed,
        BOOL_OR(e.event_type = 'open') AS was_opened,
        BOOL_OR(e.event_type = 'click') AS was_clicked,
        BOOL_OR(e.event_type = 'conversion') AS was_converted,
        COALESCE(SUM(e.conversion_value_krw)
            FILTER (WHERE e.event_type = 'conversion'), 0) AS revenue_krw
    FROM campaign_message_deliveries d
    LEFT JOIN campaign_message_events e
        ON e.delivery_id = d.delivery_id
    GROUP BY
        d.delivery_id,
        d.campaign_id,
        d.experiment_id,
        d.variant_id,
        d.channel,
        d.user_id
)
SELECT
    c.campaign_id,
    c.name AS campaign_name,
    x.experiment_id,
    x.experiment_name,
    v.variant_id,
    v.variant_code,
    v.message_name,
    f.channel,
    COUNT(*) AS assigned_count,
    COUNT(*) FILTER (WHERE f.was_sent) AS sent_count,
    COUNT(*) FILTER (WHERE f.was_delivered) AS delivered_count,
    COUNT(*) FILTER (WHERE f.was_impressed) AS impression_count,
    COUNT(*) FILTER (WHERE f.was_opened) AS open_count,
    COUNT(*) FILTER (WHERE f.was_clicked) AS click_count,
    COUNT(*) FILTER (WHERE f.was_converted) AS conversion_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_delivered)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_sent), 0),
        4
    ) AS delivery_rate_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_impressed)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_delivered), 0),
        4
    ) AS impression_rate_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_opened)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_delivered), 0),
        4
    ) AS open_rate_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_clicked)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_impressed), 0),
        4
    ) AS ctr_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_clicked)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_delivered), 0),
        4
    ) AS delivered_ctr_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_converted)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_clicked), 0),
        4
    ) AS click_to_conversion_rate_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_converted)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_delivered), 0),
        4
    ) AS cvr_pct,
    SUM(f.revenue_krw) AS revenue_krw
FROM event_flags f
JOIN campaigns c
    ON c.campaign_id = f.campaign_id
JOIN campaign_experiments x
    ON x.experiment_id = f.experiment_id
JOIN campaign_message_variants v
    ON v.variant_id = f.variant_id
GROUP BY
    c.campaign_id,
    c.name,
    x.experiment_id,
    x.experiment_name,
    v.variant_id,
    v.variant_code,
    v.message_name,
    f.channel;


-- 성별·연령대·지역·라이프사이클별 메시지 성과입니다.
CREATE VIEW v_campaign_segment_metrics AS
WITH event_flags AS (
    SELECT
        d.delivery_id,
        d.experiment_id,
        d.variant_id,
        d.user_id,
        BOOL_OR(e.event_type = 'delivered') AS was_delivered,
        BOOL_OR(e.event_type = 'impression') AS was_impressed,
        BOOL_OR(e.event_type = 'click') AS was_clicked,
        BOOL_OR(e.event_type = 'conversion') AS was_converted
    FROM campaign_message_deliveries d
    LEFT JOIN campaign_message_events e
        ON e.delivery_id = d.delivery_id
    GROUP BY d.delivery_id, d.experiment_id, d.variant_id, d.user_id
)
SELECT
    f.experiment_id,
    x.experiment_name,
    f.variant_id,
    v.variant_code,
    u.gender,
    CASE
        WHEN u.age < 20 THEN 'under_20'
        WHEN u.age < 30 THEN '20s'
        WHEN u.age < 40 THEN '30s'
        WHEN u.age < 50 THEN '40s'
        WHEN u.age < 60 THEN '50s'
        ELSE '60_plus'
    END AS age_group,
    u.region,
    u.lifecycle,
    COUNT(*) AS assigned_count,
    COUNT(*) FILTER (WHERE f.was_delivered) AS delivered_count,
    COUNT(*) FILTER (WHERE f.was_impressed) AS impression_count,
    COUNT(*) FILTER (WHERE f.was_clicked) AS click_count,
    COUNT(*) FILTER (WHERE f.was_converted) AS conversion_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_clicked)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_impressed), 0),
        4
    ) AS ctr_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE f.was_converted)
        / NULLIF(COUNT(*) FILTER (WHERE f.was_clicked), 0),
        4
    ) AS click_to_conversion_rate_pct
FROM event_flags f
JOIN users u
    ON u.user_id = f.user_id
JOIN campaign_experiments x
    ON x.experiment_id = f.experiment_id
JOIN campaign_message_variants v
    ON v.variant_id = f.variant_id
GROUP BY
    f.experiment_id,
    x.experiment_name,
    f.variant_id,
    v.variant_code,
    u.gender,
    age_group,
    u.region,
    u.lifecycle;


-- 날짜별 퍼널 추이입니다.
CREATE VIEW v_campaign_daily_metrics AS
SELECT
    d.campaign_id,
    d.experiment_id,
    d.variant_id,
    d.channel,
    (e.event_at AT TIME ZONE 'Asia/Seoul')::DATE AS event_date_kst,
    COUNT(*) FILTER (WHERE e.event_type = 'sent') AS sent_event_count,
    COUNT(*) FILTER (WHERE e.event_type = 'delivered') AS delivered_event_count,
    COUNT(*) FILTER (WHERE e.event_type = 'impression') AS impression_event_count,
    COUNT(*) FILTER (WHERE e.event_type = 'open') AS open_event_count,
    COUNT(*) FILTER (WHERE e.event_type = 'click') AS click_event_count,
    COUNT(*) FILTER (WHERE e.event_type = 'conversion') AS conversion_event_count,
    COALESCE(
        SUM(e.conversion_value_krw)
            FILTER (WHERE e.event_type = 'conversion'),
        0
    ) AS revenue_krw
FROM campaign_message_deliveries d
JOIN campaign_message_events e
    ON e.delivery_id = d.delivery_id
GROUP BY
    d.campaign_id,
    d.experiment_id,
    d.variant_id,
    d.channel,
    event_date_kst;


-- ============================================================
-- Deterministic A/B/C Test Data
-- camp_001 / LMS / 3 variants / 25 users
-- ============================================================

INSERT INTO campaign_experiments (
    campaign_id,
    experiment_name,
    channel,
    status,
    assignment_method,
    primary_metric,
    started_at
)
VALUES (
    'camp_001',
    '여름 장바구니 LMS 문구 A/B/C 테스트',
    'lms',
    'running',
    'random',
    'ctr',
    '2026-07-15 09:00:00+09'
);

INSERT INTO campaign_message_variants (
    experiment_id,
    variant_code,
    message_name,
    message_body,
    landing_url,
    allocation_weight,
    is_control,
    ai_features
)
SELECT
    x.experiment_id,
    data.variant_code,
    data.message_name,
    data.message_body,
    data.landing_url,
    data.allocation_weight,
    data.is_control,
    data.ai_features
FROM campaign_experiments x
CROSS JOIN (
    VALUES
    (
        'A',
        '혜택 강조형',
        '[장바구니 혜택] 담아둔 여름 상품을 지금 10% 할인받아 구매하세요.',
        'https://example.com/campaign/camp_001?variant=A',
        1.0000::NUMERIC,
        TRUE,
        '{"tone":"benefit","urgency":false,"discount_rate":10,"cta":"구매하세요","message_length_group":"medium"}'::JSONB
    ),
    (
        'B',
        '긴급성 강조형',
        '[오늘 마감] 장바구니 속 여름 상품 10% 할인 쿠폰이 곧 사라집니다.',
        'https://example.com/campaign/camp_001?variant=B',
        1.0000::NUMERIC,
        FALSE,
        '{"tone":"urgent","urgency":true,"discount_rate":10,"cta":"확인하세요","message_length_group":"medium"}'::JSONB
    ),
    (
        'C',
        '개인화 친근형',
        '고객님이 담아둔 여름 상품이 기다리고 있어요. 10% 쿠폰으로 다시 확인해 보세요.',
        'https://example.com/campaign/camp_001?variant=C',
        1.0000::NUMERIC,
        FALSE,
        '{"tone":"friendly","urgency":false,"discount_rate":10,"personalized":true,"cta":"확인해 보세요","message_length_group":"long"}'::JSONB
    )
) AS data(
    variant_code,
    message_name,
    message_body,
    landing_url,
    allocation_weight,
    is_control,
    ai_features
)
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트';


-- 사용자 25명을 A/B/C에 9/8/8명으로 결정론적으로 배정합니다.
WITH target_experiment AS (
    SELECT experiment_id
    FROM campaign_experiments
    WHERE campaign_id = 'camp_001'
      AND experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
),
numbered_users AS (
    SELECT
        u.user_id,
        u.age,
        u.gender,
        u.region,
        u.lifecycle,
        ROW_NUMBER() OVER (ORDER BY u.user_id) AS rn
    FROM users u
),
assigned AS (
    SELECT
        tu.*,
        CASE ((tu.rn - 1) % 3)
            WHEN 0 THEN 'A'
            WHEN 1 THEN 'B'
            ELSE 'C'
        END AS variant_code
    FROM numbered_users tu
)
INSERT INTO campaign_message_deliveries (
    experiment_id,
    variant_id,
    campaign_id,
    user_id,
    channel,
    assignment_source,
    predicted_click_probability,
    provider_message_id,
    assigned_at,
    requested_at,
    sent_at,
    final_status,
    targeting_snapshot
)
SELECT
    x.experiment_id,
    v.variant_id,
    'camp_001',
    a.user_id,
    'lms',
    'random',
    CASE v.variant_code
        WHEN 'A' THEN 0.1050000
        WHEN 'B' THEN 0.1450000
        ELSE 0.1200000
    END,
    'test-camp001-' || LOWER(v.variant_code) || '-' || a.user_id,
    '2026-07-15 08:50:00+09'::TIMESTAMPTZ
        + (a.rn * INTERVAL '7 seconds'),
    '2026-07-15 08:55:00+09'::TIMESTAMPTZ
        + (a.rn * INTERVAL '7 seconds'),
    '2026-07-15 09:00:00+09'::TIMESTAMPTZ
        + (a.rn * INTERVAL '7 seconds'),
    CASE WHEN a.rn % 13 = 0 THEN 'failed' ELSE 'delivered' END,
    JSONB_BUILD_OBJECT(
        'age', a.age,
        'gender', a.gender,
        'region', a.region,
        'lifecycle', a.lifecycle,
        'assignment_sequence', a.rn
    )
FROM assigned a
CROSS JOIN target_experiment x
JOIN campaign_message_variants v
  ON v.experiment_id = x.experiment_id
 AND v.variant_code = a.variant_code;


-- 1) 모든 사용자에 발송 요청 이벤트
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id, event_properties
)
SELECT
    d.delivery_id,
    'send_requested',
    d.requested_at,
    'test:delivery:' || d.delivery_id || ':send_requested',
    'provider-request-' || d.delivery_id,
    JSONB_BUILD_OBJECT('source', 'test_seed')
FROM campaign_message_deliveries d
JOIN campaign_experiments x ON x.experiment_id = d.experiment_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트';


-- 2) 실패 대상 외 sent 이벤트
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id, event_properties
)
SELECT
    d.delivery_id,
    'sent',
    d.sent_at,
    'test:delivery:' || d.delivery_id || ':sent',
    'provider-sent-' || d.delivery_id,
    JSONB_BUILD_OBJECT('source', 'test_seed')
FROM campaign_message_deliveries d
JOIN campaign_experiments x ON x.experiment_id = d.experiment_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
  AND d.final_status <> 'failed';


-- 3) 실패 이벤트
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id, event_properties
)
SELECT
    d.delivery_id,
    'failed',
    d.sent_at + INTERVAL '5 seconds',
    'test:delivery:' || d.delivery_id || ':failed',
    'provider-failed-' || d.delivery_id,
    JSONB_BUILD_OBJECT('reason', 'invalid_number', 'source', 'test_seed')
FROM campaign_message_deliveries d
JOIN campaign_experiments x ON x.experiment_id = d.experiment_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
  AND d.final_status = 'failed';


-- 4) 정상 발송 대상 delivered 이벤트
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id,
    device_type, os_name, event_properties
)
SELECT
    d.delivery_id,
    'delivered',
    d.sent_at + INTERVAL '10 seconds',
    'test:delivery:' || d.delivery_id || ':delivered',
    'provider-delivered-' || d.delivery_id,
    CASE WHEN d.delivery_id % 2 = 0 THEN 'mobile' ELSE 'unknown' END,
    CASE WHEN d.delivery_id % 2 = 0 THEN 'Android' ELSE NULL END,
    JSONB_BUILD_OBJECT('source', 'test_seed')
FROM campaign_message_deliveries d
JOIN campaign_experiments x ON x.experiment_id = d.experiment_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
  AND d.final_status = 'delivered';


-- 5) 일부 사용자는 추적 제한으로 impression이 발생하지 않도록 구성
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id,
    device_type, os_name, browser_name, event_properties
)
SELECT
    d.delivery_id,
    'impression',
    d.sent_at + INTERVAL '2 minutes',
    'test:delivery:' || d.delivery_id || ':impression',
    'provider-impression-' || d.delivery_id,
    'mobile',
    CASE WHEN d.delivery_id % 2 = 0 THEN 'Android' ELSE 'iOS' END,
    CASE WHEN d.delivery_id % 2 = 0 THEN 'Chrome' ELSE 'Safari' END,
    JSONB_BUILD_OBJECT('tracking_allowed', TRUE, 'source', 'test_seed')
FROM campaign_message_deliveries d
JOIN campaign_experiments x ON x.experiment_id = d.experiment_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
  AND d.final_status = 'delivered'
  AND d.delivery_id % 7 <> 0;


-- 6) A/B/C별로 다른 클릭 패턴을 생성
-- B가 가장 높고 C, A 순서가 되도록 결정론적 조건을 사용합니다.
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id,
    click_url, device_type, os_name, browser_name, event_properties
)
SELECT
    d.delivery_id,
    'click',
    d.sent_at + INTERVAL '8 minutes',
    'test:delivery:' || d.delivery_id || ':click',
    'provider-click-' || d.delivery_id,
    v.landing_url,
    'mobile',
    CASE WHEN d.delivery_id % 2 = 0 THEN 'Android' ELSE 'iOS' END,
    CASE WHEN d.delivery_id % 2 = 0 THEN 'Chrome' ELSE 'Safari' END,
    JSONB_BUILD_OBJECT(
        'cta_position', 'main',
        'source', 'test_seed',
        'variant_code', v.variant_code
    )
FROM campaign_message_deliveries d
JOIN campaign_experiments x
    ON x.experiment_id = d.experiment_id
JOIN campaign_message_variants v
    ON v.variant_id = d.variant_id
WHERE x.campaign_id = 'camp_001'
  AND x.experiment_name = '여름 장바구니 LMS 문구 A/B/C 테스트'
  AND d.final_status = 'delivered'
  AND d.delivery_id % 7 <> 0
  AND (
      (v.variant_code = 'A' AND d.delivery_id % 8 = 1)
      OR
      (v.variant_code = 'B' AND d.delivery_id % 4 IN (0, 1))
      OR
      (v.variant_code = 'C' AND d.delivery_id % 6 IN (2, 3))
  );


-- 7) 클릭 사용자 중 일부에 구매 전환과 매출 생성
INSERT INTO campaign_message_events (
    delivery_id, event_type, event_at, event_key, provider_event_id,
    conversion_type, conversion_value_krw, event_properties
)
SELECT
    e.delivery_id,
    'conversion',
    e.event_at + INTERVAL '25 minutes',
    'test:delivery:' || e.delivery_id || ':conversion',
    'provider-conversion-' || e.delivery_id,
    'purchase',
    CASE
        WHEN e.delivery_id % 3 = 0 THEN 89000
        WHEN e.delivery_id % 3 = 1 THEN 54000
        ELSE 72000
    END,
    JSONB_BUILD_OBJECT(
        'order_id', 'test-order-' || e.delivery_id,
        'source', 'test_seed'
    )
FROM campaign_message_events e
WHERE e.event_type = 'click'
  AND e.event_key LIKE 'test:delivery:%'
  AND e.delivery_id % 2 = 0;


-- ============================================================
-- Optional larger test-data generator
-- Creates 30 synthetic event days by copying existing deliveries.
-- Disabled by default. Remove the surrounding comment to execute.
-- ============================================================

/*
INSERT INTO campaign_message_events (
    delivery_id,
    event_type,
    event_at,
    event_key,
    provider_event_id,
    click_url,
    conversion_type,
    conversion_value_krw,
    event_properties
)
SELECT
    e.delivery_id,
    e.event_type,
    e.event_at + (g.day_no * INTERVAL '1 day'),
    e.event_key || ':day:' || g.day_no,
    COALESCE(e.provider_event_id, 'generated') || ':day:' || g.day_no,
    e.click_url,
    e.conversion_type,
    e.conversion_value_krw,
    e.event_properties || JSONB_BUILD_OBJECT('generated_day', g.day_no)
FROM campaign_message_events e
CROSS JOIN GENERATE_SERIES(1, 29) AS g(day_no)
WHERE e.event_key LIKE 'test:delivery:%';
*/


-- ============================================================
-- Validation / Example Queries
-- ============================================================

-- 테이블별 건수
SELECT 'campaigns' AS object_name, COUNT(*) AS row_count FROM campaigns
UNION ALL
SELECT 'users', COUNT(*) FROM users
UNION ALL
SELECT 'campaign_experiments', COUNT(*) FROM campaign_experiments
UNION ALL
SELECT 'campaign_message_variants', COUNT(*) FROM campaign_message_variants
UNION ALL
SELECT 'campaign_message_deliveries', COUNT(*) FROM campaign_message_deliveries
UNION ALL
SELECT 'campaign_message_events', COUNT(*) FROM campaign_message_events
ORDER BY object_name;

-- A/B/C 버전별 CTR 및 CVR
SELECT *
FROM v_campaign_variant_metrics
ORDER BY experiment_id, ctr_pct DESC NULLS LAST;

-- 타겟 세그먼트별 CTR
SELECT *
FROM v_campaign_segment_metrics
WHERE impression_count > 0
ORDER BY experiment_id, variant_code, ctr_pct DESC NULLS LAST;

-- 날짜별 이벤트 추이
SELECT *
FROM v_campaign_daily_metrics
ORDER BY event_date_kst, variant_id;

-- 이벤트 중복 방지 확인 예시:
-- 아래 INSERT는 동일 event_key가 이미 있으므로 unique violation이 발생해야 정상입니다.
-- INSERT INTO campaign_message_events
-- (delivery_id, event_type, event_at, event_key)
-- VALUES (1, 'click', CURRENT_TIMESTAMP, 'test:delivery:1:click');


-- ============================================================
-- Shopping Cart / Abandoned Cart Extension
-- 장바구니 미결제 고객 타겟팅 및 재구매 유도 분석용
-- PostgreSQL 14+
-- ============================================================

DROP VIEW IF EXISTS v_cart_repurchase_targets CASCADE;
DROP VIEW IF EXISTS v_abandoned_cart_targets CASCADE;
DROP TABLE IF EXISTS cart_reminder_history CASCADE;
DROP TABLE IF EXISTS shopping_cart_items CASCADE;
DROP TABLE IF EXISTS shopping_carts CASCADE;

-- 사용자별 장바구니 헤더입니다.
CREATE TABLE shopping_carts (
    cart_id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(20) NOT NULL
        REFERENCES users(user_id) ON DELETE CASCADE,
    cart_status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (cart_status IN ('active', 'abandoned', 'purchased', 'expired')),
    source_channel VARCHAR(30) NOT NULL
        CHECK (source_channel IN ('web', 'mobile_web', 'android_app', 'ios_app')),
    total_quantity INTEGER NOT NULL DEFAULT 0
        CHECK (total_quantity >= 0),
    original_amount_krw INTEGER NOT NULL DEFAULT 0
        CHECK (original_amount_krw >= 0),
    discount_amount_krw INTEGER NOT NULL DEFAULT 0
        CHECK (discount_amount_krw >= 0),
    shipping_fee_krw INTEGER NOT NULL DEFAULT 0
        CHECK (shipping_fee_krw >= 0),
    total_amount_krw INTEGER NOT NULL DEFAULT 0
        CHECK (total_amount_krw >= 0),
    purchase_completed BOOLEAN NOT NULL DEFAULT FALSE,
    abandonment_reason VARCHAR(40)
        CHECK (abandonment_reason IS NULL OR abandonment_reason IN (
            'unknown', 'price', 'shipping_fee', 'payment_failure',
            'comparison_shopping', 'coupon_search', 'out_of_stock', 'delayed_decision'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    abandoned_at TIMESTAMPTZ,
    purchased_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_shopping_cart_amount
        CHECK (total_amount_krw = GREATEST(original_amount_krw - discount_amount_krw, 0) + shipping_fee_krw),
    CONSTRAINT chk_shopping_cart_purchase
        CHECK (
            (purchase_completed = FALSE AND purchased_at IS NULL)
            OR
            (purchase_completed = TRUE AND purchased_at IS NOT NULL AND cart_status = 'purchased')
        ),
    CONSTRAINT chk_shopping_cart_abandoned
        CHECK (cart_status <> 'abandoned' OR abandoned_at IS NOT NULL),
    CONSTRAINT chk_shopping_cart_time_order
        CHECK (
            last_activity_at >= created_at
            AND (abandoned_at IS NULL OR abandoned_at >= created_at)
            AND (purchased_at IS NULL OR purchased_at >= created_at)
            AND (expires_at IS NULL OR expires_at >= created_at)
        )
);

-- 장바구니에 담긴 상품 상세입니다.
CREATE TABLE shopping_cart_items (
    cart_item_id BIGSERIAL PRIMARY KEY,
    cart_id BIGINT NOT NULL
        REFERENCES shopping_carts(cart_id) ON DELETE CASCADE,
    product_id VARCHAR(30) NOT NULL,
    product_name VARCHAR(200) NOT NULL,
    category VARCHAR(100) NOT NULL,
    brand VARCHAR(100),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_krw INTEGER NOT NULL CHECK (unit_price_krw >= 0),
    discount_price_krw INTEGER
        CHECK (discount_price_krw IS NULL OR discount_price_krw >= 0),
    final_unit_price_krw INTEGER NOT NULL CHECK (final_unit_price_krw >= 0),
    inventory_quantity INTEGER CHECK (inventory_quantity IS NULL OR inventory_quantity >= 0),
    inventory_status VARCHAR(20) NOT NULL DEFAULT 'in_stock'
        CHECK (inventory_status IN ('in_stock', 'low_stock', 'out_of_stock', 'discontinued')),
    coupon_available BOOLEAN NOT NULL DEFAULT FALSE,
    coupon_code VARCHAR(50),
    coupon_expire_at TIMESTAMPTZ,
    wishlist_flag BOOLEAN NOT NULL DEFAULT FALSE,
    viewed_count INTEGER NOT NULL DEFAULT 0 CHECK (viewed_count >= 0),
    added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_viewed_at TIMESTAMPTZ,
    removed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_shopping_cart_product UNIQUE (cart_id, product_id),
    CONSTRAINT chk_cart_item_discount
        CHECK (final_unit_price_krw <= unit_price_krw),
    CONSTRAINT chk_cart_item_coupon
        CHECK (coupon_available = TRUE OR coupon_code IS NULL),
    CONSTRAINT chk_cart_item_time_order
        CHECK (
            (last_viewed_at IS NULL OR last_viewed_at >= added_at)
            AND (removed_at IS NULL OR removed_at >= added_at)
            AND (coupon_expire_at IS NULL OR coupon_expire_at >= added_at)
        )
);

-- 장바구니 리마인드 발송 및 전환 이력입니다.
CREATE TABLE cart_reminder_history (
    reminder_id BIGSERIAL PRIMARY KEY,
    cart_id BIGINT NOT NULL
        REFERENCES shopping_carts(cart_id) ON DELETE CASCADE,
    campaign_id VARCHAR(20)
        REFERENCES campaigns(campaign_id) ON DELETE SET NULL,
    delivery_id BIGINT
        REFERENCES campaign_message_deliveries(delivery_id) ON DELETE SET NULL,
    channel VARCHAR(30) NOT NULL
        CHECK (channel IN ('app_push', 'kakao', 'sms', 'lms', 'rcs', 'email')),
    reminder_sequence SMALLINT NOT NULL CHECK (reminder_sequence BETWEEN 1 AND 10),
    reminder_type VARCHAR(30) NOT NULL
        CHECK (reminder_type IN ('benefit', 'urgency', 'stock', 'social_proof', 'simple_reminder')),
    coupon_code VARCHAR(50),
    sent_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    clicked_at TIMESTAMPTZ,
    converted_at TIMESTAMPTZ,
    conversion_order_id VARCHAR(50),
    conversion_value_krw INTEGER CHECK (conversion_value_krw IS NULL OR conversion_value_krw >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_cart_reminder_sequence UNIQUE (cart_id, reminder_sequence),
    CONSTRAINT chk_cart_reminder_time_order
        CHECK (
            (delivered_at IS NULL OR delivered_at >= sent_at)
            AND (clicked_at IS NULL OR clicked_at >= sent_at)
            AND (converted_at IS NULL OR converted_at >= sent_at)
        )
);

CREATE INDEX idx_shopping_carts_user_status
    ON shopping_carts(user_id, cart_status);
CREATE INDEX idx_shopping_carts_abandoned_target
    ON shopping_carts(abandoned_at, total_amount_krw)
    WHERE cart_status = 'abandoned' AND purchase_completed = FALSE;
CREATE INDEX idx_shopping_carts_last_activity
    ON shopping_carts(last_activity_at DESC);
CREATE INDEX idx_shopping_cart_items_cart
    ON shopping_cart_items(cart_id);
CREATE INDEX idx_shopping_cart_items_category
    ON shopping_cart_items(category);
CREATE INDEX idx_shopping_cart_items_inventory
    ON shopping_cart_items(inventory_status, coupon_available);
CREATE INDEX idx_cart_reminder_history_cart_sent
    ON cart_reminder_history(cart_id, sent_at DESC);
CREATE INDEX idx_cart_reminder_history_campaign
    ON cart_reminder_history(campaign_id)
    WHERE campaign_id IS NOT NULL;

-- 장바구니 리마인드 타겟 조회용 뷰입니다.
CREATE VIEW v_abandoned_cart_targets AS
SELECT
    c.cart_id,
    c.user_id,
    u.age,
    u.gender,
    u.region,
    u.price_sensitivity,
    u.predicted_ltv_segment,
    c.source_channel,
    c.total_quantity,
    c.original_amount_krw,
    c.discount_amount_krw,
    c.shipping_fee_krw,
    c.total_amount_krw,
    c.abandonment_reason,
    c.abandoned_at,
    FLOOR(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - c.abandoned_at)) / 3600)::BIGINT AS abandoned_hours,
    COUNT(i.cart_item_id) AS item_line_count,
    STRING_AGG(i.product_name, ', ' ORDER BY i.cart_item_id) AS product_names,
    STRING_AGG(DISTINCT i.category, ', ' ORDER BY i.category) AS categories,
    BOOL_OR(i.coupon_available) AS has_available_coupon,
    BOOL_OR(i.inventory_status = 'low_stock') AS has_low_stock_item,
    BOOL_AND(i.inventory_status IN ('in_stock', 'low_stock')) AS all_items_orderable,
    COUNT(r.reminder_id) AS reminder_count,
    MAX(r.sent_at) AS last_reminder_sent_at,
    BOOL_OR(r.converted_at IS NOT NULL) AS reminder_converted
FROM shopping_carts c
JOIN users u
    ON u.user_id = c.user_id
JOIN shopping_cart_items i
    ON i.cart_id = c.cart_id
LEFT JOIN cart_reminder_history r
    ON r.cart_id = c.cart_id
WHERE c.cart_status = 'abandoned'
  AND c.purchase_completed = FALSE
GROUP BY
    c.cart_id,
    c.user_id,
    u.age,
    u.gender,
    u.region,
    u.price_sensitivity,
    u.predicted_ltv_segment,
    c.source_channel,
    c.total_quantity,
    c.original_amount_krw,
    c.discount_amount_krw,
    c.shipping_fee_krw,
    c.total_amount_krw,
    c.abandonment_reason,
    c.abandoned_at;

-- 장바구니 미결제 고객에게 재구매/구매 전환 캠페인 후보를 연결하는 타겟팅 뷰입니다.
CREATE VIEW v_cart_repurchase_targets AS
SELECT
    t.cart_id,
    t.user_id,
    t.age,
    t.gender,
    t.region,
    t.price_sensitivity,
    t.predicted_ltv_segment,
    t.source_channel,
    t.total_quantity,
    t.total_amount_krw,
    t.abandonment_reason,
    t.abandoned_at,
    t.abandoned_hours,
    t.categories,
    t.product_names,
    t.has_available_coupon,
    t.has_low_stock_item,
    t.all_items_orderable,
    t.reminder_count,
    t.last_reminder_sent_at,
    'cart_abandoner' AS target_segment,
    'cart_repurchase_reminder' AS targeting_intent,
    c.campaign_id AS recommended_campaign_id,
    c.name AS recommended_campaign_name,
    c.objective AS campaign_objective,
    c.category AS campaign_category,
    c.offer AS campaign_offer,
    CASE
        WHEN ts_cart.target_segment IS NOT NULL THEN 'cart_abandoner segment match'
        WHEN c.category = ANY(string_to_array(t.categories, ', ')) THEN 'cart category match'
        ELSE 'cart reminder keyword match'
    END AS recommendation_reason
FROM v_abandoned_cart_targets t
JOIN campaigns c
    ON TRUE
LEFT JOIN campaign_target_segments ts_cart
    ON ts_cart.campaign_id = c.campaign_id
   AND ts_cart.target_segment = 'cart_abandoner'
LEFT JOIN campaign_keywords ck_cart
    ON ck_cart.campaign_id = c.campaign_id
   AND ck_cart.keyword = '장바구니'
LEFT JOIN campaign_keywords ck_repurchase
    ON ck_repurchase.campaign_id = c.campaign_id
   AND ck_repurchase.keyword = '재구매'
WHERE t.all_items_orderable = TRUE
  AND t.reminder_converted = FALSE
  AND (
      c.category = ANY(string_to_array(t.categories, ', '))
      OR ck_cart.keyword IS NOT NULL
      OR ck_repurchase.keyword IS NOT NULL
  )
  AND (
      ts_cart.target_segment IS NOT NULL
      OR c.objective IN ('purchase', 'repurchase', 'retention')
      OR ck_cart.keyword IS NOT NULL
      OR ck_repurchase.keyword IS NOT NULL
  );

-- ============================================================
-- Deterministic sample data: abandoned carts 30건
-- 기준 시각: 2026-07-14 KST
-- ============================================================

INSERT INTO shopping_carts (
    cart_id, user_id, cart_status, source_channel,
    total_quantity, original_amount_krw, discount_amount_krw,
    shipping_fee_krw, total_amount_krw, purchase_completed,
    abandonment_reason, created_at, last_activity_at, abandoned_at,
    expires_at, updated_at
) VALUES
(1,  'user_001', 'abandoned', 'ios_app',     1,  39000,  3900,    0,  35100, FALSE, 'coupon_search',       '2026-07-12 23:20+09', '2026-07-13 09:30+09', '2026-07-13 09:30+09', '2026-07-20 09:30+09', '2026-07-13 09:30+09'),
(2,  'user_002', 'abandoned', 'web',         1,  29000,     0, 3000,  32000, FALSE, 'shipping_fee',       '2026-07-12 17:40+09', '2026-07-12 18:20+09', '2026-07-12 18:20+09', '2026-07-19 18:20+09', '2026-07-12 18:20+09'),
(3,  'user_003', 'abandoned', 'android_app',  2,  52000,  6240,    0,  45760, FALSE, 'price',              '2026-07-13 09:40+09', '2026-07-13 10:15+09', '2026-07-13 10:15+09', '2026-07-20 10:15+09', '2026-07-13 10:15+09'),
(4,  'user_004', 'abandoned', 'mobile_web',  1,  25000,     0, 3000,  28000, FALSE, 'delayed_decision',   '2026-07-12 13:20+09', '2026-07-12 14:10+09', '2026-07-12 14:10+09', '2026-07-19 14:10+09', '2026-07-12 14:10+09'),
(5,  'user_005', 'abandoned', 'mobile_web',  1,  43000,  6450,    0,  36550, FALSE, 'price',              '2026-07-13 07:20+09', '2026-07-13 08:00+09', '2026-07-13 08:00+09', '2026-07-20 08:00+09', '2026-07-13 08:00+09'),
(6,  'user_006', 'abandoned', 'android_app',  3,  18000,  1800, 3000,  19200, FALSE, 'comparison_shopping','2026-07-13 10:55+09', '2026-07-13 11:30+09', '2026-07-13 11:30+09', '2026-07-20 11:30+09', '2026-07-13 11:30+09'),
(7,  'user_007', 'abandoned', 'ios_app',     1, 119000,     0,    0, 119000, FALSE, 'comparison_shopping','2026-07-12 08:30+09', '2026-07-12 09:20+09', '2026-07-12 09:20+09', '2026-07-19 09:20+09', '2026-07-12 09:20+09'),
(8,  'user_008', 'abandoned', 'ios_app',     1,  62000, 12400,    0,  49600, FALSE, 'coupon_search',       '2026-07-13 14:35+09', '2026-07-13 15:10+09', '2026-07-13 15:10+09', '2026-07-20 15:10+09', '2026-07-13 15:10+09'),
(9,  'user_009', 'abandoned', 'web',         2,  88000,     0,    0,  88000, FALSE, 'delayed_decision',   '2026-07-12 19:05+09', '2026-07-12 19:50+09', '2026-07-12 19:50+09', '2026-07-19 19:50+09', '2026-07-12 19:50+09'),
(10, 'user_010', 'abandoned', 'mobile_web',  1,  35000,  3500, 3000,  34500, FALSE, 'shipping_fee',       '2026-07-13 06:30+09', '2026-07-13 07:00+09', '2026-07-13 07:00+09', '2026-07-20 07:00+09', '2026-07-13 07:00+09'),
(11, 'user_011', 'abandoned', 'web',         1,  59000,  5900,    0,  53100, FALSE, 'coupon_search',       '2026-07-13 11:20+09', '2026-07-13 12:00+09', '2026-07-13 12:00+09', '2026-07-20 12:00+09', '2026-07-13 12:00+09'),
(12, 'user_012', 'abandoned', 'android_app',  1,   9900,     0,    0,   9900, FALSE, 'delayed_decision',   '2026-07-13 15:25+09', '2026-07-13 16:00+09', '2026-07-13 16:00+09', '2026-07-20 16:00+09', '2026-07-13 16:00+09'),
(13, 'user_013', 'abandoned', 'ios_app',     1, 159000, 11130,    0, 147870, FALSE, 'comparison_shopping','2026-07-12 16:10+09', '2026-07-12 17:00+09', '2026-07-12 17:00+09', '2026-07-19 17:00+09', '2026-07-12 17:00+09'),
(14, 'user_014', 'abandoned', 'mobile_web',  1,  47000,  9400,    0,  37600, FALSE, 'price',              '2026-07-13 10:05+09', '2026-07-13 10:40+09', '2026-07-13 10:40+09', '2026-07-20 10:40+09', '2026-07-13 10:40+09'),
(15, 'user_015', 'abandoned', 'web',         2,  28000,     0, 3000,  31000, FALSE, 'shipping_fee',       '2026-07-12 07:45+09', '2026-07-12 08:30+09', '2026-07-12 08:30+09', '2026-07-19 08:30+09', '2026-07-12 08:30+09'),
(16, 'user_016', 'abandoned', 'android_app',  1,  69000,  6900,    0,  62100, FALSE, 'coupon_search',       '2026-07-13 08:35+09', '2026-07-13 09:10+09', '2026-07-13 09:10+09', '2026-07-20 09:10+09', '2026-07-13 09:10+09'),
(17, 'user_017', 'abandoned', 'web',         1,  78000,     0,    0,  78000, FALSE, 'comparison_shopping','2026-07-12 14:30+09', '2026-07-12 15:20+09', '2026-07-12 15:20+09', '2026-07-19 15:20+09', '2026-07-12 15:20+09'),
(18, 'user_018', 'abandoned', 'android_app',  2,  32000,  3200, 3000,  31800, FALSE, 'shipping_fee',       '2026-07-13 10:25+09', '2026-07-13 11:00+09', '2026-07-13 11:00+09', '2026-07-20 11:00+09', '2026-07-13 11:00+09'),
(19, 'user_019', 'abandoned', 'web',         1,  99000,     0,    0,  99000, FALSE, 'delayed_decision',   '2026-07-12 19:15+09', '2026-07-12 20:00+09', '2026-07-12 20:00+09', '2026-07-19 20:00+09', '2026-07-12 20:00+09'),
(20, 'user_020', 'abandoned', 'mobile_web',  1, 129000, 12900,    0, 116100, FALSE, 'coupon_search',       '2026-07-13 12:45+09', '2026-07-13 13:20+09', '2026-07-13 13:20+09', '2026-07-20 13:20+09', '2026-07-13 13:20+09'),
(21, 'user_021', 'abandoned', 'ios_app',     3,  18000,  1800, 3000,  19200, FALSE, 'price',              '2026-07-13 08:25+09', '2026-07-13 09:00+09', '2026-07-13 09:00+09', '2026-07-20 09:00+09', '2026-07-13 09:00+09'),
(22, 'user_022', 'abandoned', 'android_app',  2,  39000,     0, 3000,  42000, FALSE, 'shipping_fee',       '2026-07-12 17:25+09', '2026-07-12 18:10+09', '2026-07-12 18:10+09', '2026-07-19 18:10+09', '2026-07-12 18:10+09'),
(23, 'user_023', 'abandoned', 'mobile_web',  4,  45000,  4500,    0,  40500, FALSE, 'coupon_search',       '2026-07-13 08:05+09', '2026-07-13 08:40+09', '2026-07-13 08:40+09', '2026-07-20 08:40+09', '2026-07-13 08:40+09'),
(24, 'user_024', 'abandoned', 'web',         1,  68000,  6800,    0,  61200, FALSE, 'comparison_shopping','2026-07-13 13:30+09', '2026-07-13 14:10+09', '2026-07-13 14:10+09', '2026-07-20 14:10+09', '2026-07-13 14:10+09'),
(25, 'user_025', 'abandoned', 'android_app',  1,  89000,     0,    0,  89000, FALSE, 'delayed_decision',   '2026-07-12 20:15+09', '2026-07-12 21:00+09', '2026-07-12 21:00+09', '2026-07-19 21:00+09', '2026-07-12 21:00+09'),
(26, 'user_001', 'abandoned', 'ios_app',     1,  59000,  5900,    0,  53100, FALSE, 'coupon_search',       '2026-07-13 11:35+09', '2026-07-13 12:10+09', '2026-07-13 12:10+09', '2026-07-20 12:10+09', '2026-07-13 12:10+09'),
(27, 'user_006', 'abandoned', 'android_app',  2,  21000,     0, 3000,  24000, FALSE, 'shipping_fee',       '2026-07-13 09:45+09', '2026-07-13 10:20+09', '2026-07-13 10:20+09', '2026-07-20 10:20+09', '2026-07-13 10:20+09'),
(28, 'user_010', 'abandoned', 'web',         1,  74000,  7400,    0,  66600, FALSE, 'price',              '2026-07-12 15:25+09', '2026-07-12 16:10+09', '2026-07-12 16:10+09', '2026-07-19 16:10+09', '2026-07-12 16:10+09'),
(29, 'user_016', 'abandoned', 'android_app',  1,  45000,  4500, 3000,  43500, FALSE, 'shipping_fee',       '2026-07-13 07:55+09', '2026-07-13 08:30+09', '2026-07-13 08:30+09', '2026-07-20 08:30+09', '2026-07-13 08:30+09'),
(30, 'user_022', 'abandoned', 'ios_app',     5,  27000,  2700, 3000,  27300, FALSE, 'coupon_search',       '2026-07-13 11:05+09', '2026-07-13 11:40+09', '2026-07-13 11:40+09', '2026-07-20 11:40+09', '2026-07-13 11:40+09');

INSERT INTO shopping_cart_items (
    cart_item_id, cart_id, product_id, product_name, category, brand,
    quantity, unit_price_krw, discount_price_krw, final_unit_price_krw,
    inventory_quantity, inventory_status, coupon_available, coupon_code,
    coupon_expire_at, wishlist_flag, viewed_count, added_at, last_viewed_at
) VALUES
(1,  1,  'prod_001', '나이키 반팔티',       'fashion',      'Nike',       1, 39000, 35100, 35100, 32, 'in_stock',     TRUE,  'CART10',  '2026-07-16 23:59+09', FALSE, 4, '2026-07-13 09:30+09', '2026-07-13 09:32+09'),
(2,  2,  'prod_002', '맥북 파우치',           'electronics',  'Incase',     1, 29000, NULL,  29000, 18, 'in_stock',     FALSE, NULL,      NULL,                    TRUE,  3, '2026-07-12 18:20+09', '2026-07-12 18:24+09'),
(3,  3,  'prod_003', '기저귀 특대형',         'baby',         'Merries',    2, 26000, 22880, 22880, 11, 'low_stock',    TRUE,  'BABY12',  '2026-07-17 23:59+09', FALSE, 5, '2026-07-13 10:15+09', '2026-07-13 10:18+09'),
(4,  4,  'prod_004', '골프 장갑',             'sports',       'Titleist',   1, 25000, NULL,  25000, 21, 'in_stock',     FALSE, NULL,      NULL,                    FALSE, 2, '2026-07-12 14:10+09', '2026-07-12 14:12+09'),
(5,  5,  'prod_005', '비타민 세트',           'health_food',  'Centrum',    1, 43000, 36550, 36550, 15, 'in_stock',     TRUE,  'RETURN15','2026-07-16 23:59+09', TRUE,  6, '2026-07-13 08:00+09', '2026-07-13 08:04+09'),
(6,  6,  'prod_006', '간편식 도시락',         'food',         'FreshMeal',  3,  6000,  5400,  5400, 44, 'in_stock',     TRUE,  'FOOD10',  '2026-07-15 23:59+09', FALSE, 4, '2026-07-13 11:30+09', '2026-07-13 11:33+09'),
(7,  7,  'prod_007', '러닝화',                'sports',       'Asics',      1,119000, NULL, 119000,  7, 'low_stock',    FALSE, NULL,      NULL,                    TRUE,  8, '2026-07-12 09:20+09', '2026-07-12 09:27+09'),
(8,  8,  'prod_008', '강아지 사료',           'pet',          'RoyalCanin', 1, 62000, 49600, 49600, 26, 'in_stock',     TRUE,  'PET20',   '2026-07-18 23:59+09', FALSE, 5, '2026-07-13 15:10+09', '2026-07-13 15:13+09'),
(9,  9,  'prod_009', '캠핑 의자',             'outdoor',      'SnowPeak',   2, 44000, NULL,  44000,  8, 'low_stock',    FALSE, NULL,      NULL,                    TRUE,  7, '2026-07-12 19:50+09', '2026-07-12 19:55+09'),
(10, 10, 'prod_010', '노트북 거치대',         'electronics',  'RainDesign', 1, 35000, 31500, 31500, 30, 'in_stock',     TRUE,  'STUDENT10','2026-07-19 23:59+09', FALSE, 3, '2026-07-13 07:00+09', '2026-07-13 07:02+09'),
(11, 11, 'prod_011', '와인잔 세트',           'home_living',  'Riedel',     1, 59000, 53100, 53100,  9, 'low_stock',    TRUE,  'HOME10',  '2026-07-20 23:59+09', TRUE,  6, '2026-07-13 12:00+09', '2026-07-13 12:04+09'),
(12, 12, 'prod_012', '프리미엄 구독권',       'digital_content','StreamPlus',1, 9900, NULL,   9900, NULL,'in_stock',   FALSE, NULL,      NULL,                    FALSE, 4, '2026-07-13 16:00+09', '2026-07-13 16:01+09'),
(13, 13, 'prod_013', '호텔 숙박권',           'travel',       'JejuStay',   1,159000,147870,147870, 12, 'in_stock',     TRUE,  'TRAVEL7', '2026-07-16 23:59+09', TRUE,  9, '2026-07-12 17:00+09', '2026-07-12 17:08+09'),
(14, 14, 'prod_014', '남성 스킨세트',         'beauty',       'LabSeries',  1, 47000, 37600, 37600, 20, 'in_stock',     TRUE,  'GROOM20', '2026-07-19 23:59+09', FALSE, 5, '2026-07-13 10:40+09', '2026-07-13 10:44+09'),
(15, 15, 'prod_015', '친환경 세제',           'eco',          'EcoVer',     2, 14000, NULL,  14000, 35, 'in_stock',     FALSE, NULL,      NULL,                    TRUE,  3, '2026-07-12 08:30+09', '2026-07-12 08:32+09'),
(16, 16, 'prod_016', '여름 원피스',           'fashion',      'Zara',       1, 69000, 62100, 62100, 10, 'low_stock',    TRUE,  'CART10',  '2026-07-16 23:59+09', TRUE,  8, '2026-07-13 09:10+09', '2026-07-13 09:16+09'),
(17, 17, 'prod_017', '게이밍 마우스',         'electronics',  'Logitech',   1, 78000, NULL,  78000, 14, 'in_stock',     FALSE, NULL,      NULL,                    FALSE, 7, '2026-07-12 15:20+09', '2026-07-12 15:25+09'),
(18, 18, 'prod_018', '샴푸 세트',             'beauty',       'Aveda',      2, 16000, 14400, 14400, 23, 'in_stock',     TRUE,  'BEAUTY10','2026-07-17 23:59+09', FALSE, 4, '2026-07-13 11:00+09', '2026-07-13 11:03+09'),
(19, 19, 'prod_019', '캠핑 테이블',           'outdoor',      'Coleman',    1, 99000, NULL,  99000,  6, 'low_stock',    FALSE, NULL,      NULL,                    TRUE,  6, '2026-07-12 20:00+09', '2026-07-12 20:05+09'),
(20, 20, 'prod_020', '건강식품 선물세트',     'health_food',  'JungKwanJang',1,129000,116100,116100,16,'in_stock',   TRUE,  'GIFT10',  '2026-07-21 23:59+09', TRUE,  7, '2026-07-13 13:20+09', '2026-07-13 13:25+09'),
(21, 21, 'prod_021', '러닝 양말',             'sports',       'Nike',       3,  6000,  5400,  5400, 40, 'in_stock',     TRUE,  'SPORT10', '2026-07-18 23:59+09', FALSE, 3, '2026-07-13 09:00+09', '2026-07-13 09:02+09'),
(22, 22, 'prod_022', '고양이 모래',           'pet',          'EverClean',  2, 19500, NULL,  19500, 13, 'in_stock',     FALSE, NULL,      NULL,                    FALSE, 5, '2026-07-12 18:10+09', '2026-07-12 18:14+09'),
(23, 23, 'prod_023', '밀키트',                'food',         'LocalKitchen',4,11250,10125,10125, 9, 'low_stock',    TRUE,  'MEALKIT10','2026-07-16 23:59+09', FALSE, 6, '2026-07-13 08:40+09', '2026-07-13 08:45+09'),
(24, 24, 'prod_024', '에센스',                'beauty',       'Sulwhasoo',  1, 68000, 61200, 61200, 17, 'in_stock',     TRUE,  'BEAUTY10','2026-07-18 23:59+09', TRUE,  8, '2026-07-13 14:10+09', '2026-07-13 14:17+09'),
(25, 25, 'prod_025', '운동복 세트',           'sports',       'Adidas',     1, 89000, NULL,  89000, 19, 'in_stock',     FALSE, NULL,      NULL,                    TRUE,  5, '2026-07-12 21:00+09', '2026-07-12 21:04+09'),
(26, 26, 'prod_026', '청바지',                'fashion',      'Levis',      1, 59000, 53100, 53100, 25, 'in_stock',     TRUE,  'CART10',  '2026-07-16 23:59+09', FALSE, 5, '2026-07-13 12:10+09', '2026-07-13 12:14+09'),
(27, 27, 'prod_027', '샌드위치 세트',         'food',         'FreshMeal',  2, 10500, NULL,  10500, 31, 'in_stock',     FALSE, NULL,      NULL,                    FALSE, 3, '2026-07-13 10:20+09', '2026-07-13 10:22+09'),
(28, 28, 'prod_028', '무선 키보드',           'electronics',  'Keychron',   1, 74000, 66600, 66600,  8, 'low_stock',    TRUE,  'DIGITAL10','2026-07-18 23:59+09', TRUE,  9, '2026-07-12 16:10+09', '2026-07-12 16:18+09'),
(29, 29, 'prod_029', '샌들',                  'fashion',      'Birkenstock',1,45000,40500,40500,  5, 'low_stock',    TRUE,  'CART10',  '2026-07-16 23:59+09', FALSE, 7, '2026-07-13 08:30+09', '2026-07-13 08:35+09'),
(30, 30, 'prod_030', '애견 간식',             'pet',          'NaturalCore',5, 5400, 4860, 4860, 42, 'in_stock',     TRUE,  'PET10',   '2026-07-18 23:59+09', FALSE, 4, '2026-07-13 11:40+09', '2026-07-13 11:43+09');

-- 일부 고객에게 이미 발송된 리마인드 예시입니다.
INSERT INTO cart_reminder_history (
    cart_id, campaign_id, channel, reminder_sequence, reminder_type,
    coupon_code, sent_at, delivered_at, clicked_at
) VALUES
(1,  'camp_001', 'app_push', 1, 'benefit', 'CART10',   '2026-07-14 09:00+09', '2026-07-14 09:00:02+09', '2026-07-14 09:12+09'),
(3,  'camp_009', 'sms',      1, 'benefit', 'BABY12',   '2026-07-14 08:30+09', '2026-07-14 08:30:05+09', NULL),
(7,  'camp_010', 'app_push', 1, 'stock',   NULL,       '2026-07-14 10:00+09', '2026-07-14 10:00:03+09', '2026-07-14 10:08+09'),
(8,  'camp_005', 'email',    1, 'benefit', 'PET20',    '2026-07-14 09:15+09', '2026-07-14 09:15:10+09', NULL),
(16, 'camp_001', 'kakao',    1, 'urgency', 'CART10',   '2026-07-14 09:20+09', '2026-07-14 09:20:04+09', '2026-07-14 09:35+09');

-- 명시적으로 입력한 BIGSERIAL 값 이후 시퀀스를 정렬합니다.
SELECT setval(pg_get_serial_sequence('shopping_carts', 'cart_id'),
              COALESCE((SELECT MAX(cart_id) FROM shopping_carts), 1), TRUE);
SELECT setval(pg_get_serial_sequence('shopping_cart_items', 'cart_item_id'),
              COALESCE((SELECT MAX(cart_item_id) FROM shopping_cart_items), 1), TRUE);
SELECT setval(pg_get_serial_sequence('cart_reminder_history', 'reminder_id'),
              COALESCE((SELECT MAX(reminder_id) FROM cart_reminder_history), 1), TRUE);

-- ============================================================
-- Abandoned cart validation / targeting examples
-- ============================================================

-- 생성 데이터 검증
SELECT 'shopping_carts' AS object_name, COUNT(*) AS row_count FROM shopping_carts
UNION ALL
SELECT 'shopping_cart_items', COUNT(*) FROM shopping_cart_items
UNION ALL
SELECT 'cart_reminder_history', COUNT(*) FROM cart_reminder_history
ORDER BY object_name;

-- 24시간 이상 방치되고 주문 가능한 상품이 남아 있는 고객
SELECT *
FROM v_abandoned_cart_targets
WHERE abandoned_hours >= 24
  AND all_items_orderable = TRUE
  AND reminder_converted = FALSE
ORDER BY total_amount_krw DESC, abandoned_hours DESC;

-- 할인 쿠폰이 있고 아직 리마인드를 발송하지 않은 고가치 타겟
SELECT *
FROM v_abandoned_cart_targets
WHERE has_available_coupon = TRUE
  AND reminder_count = 0
  AND total_amount_krw >= 50000
ORDER BY predicted_ltv_segment DESC, total_amount_krw DESC;

-- 장바구니 미결제 고객별 재구매/구매 전환 캠페인 후보
SELECT
        user_id,
        cart_id,
        target_segment,
        recommended_campaign_id,
        recommended_campaign_name,
        campaign_objective,
        campaign_category,
        recommendation_reason
FROM v_cart_repurchase_targets
WHERE abandoned_hours >= 24
ORDER BY total_amount_krw DESC, abandoned_hours DESC, recommended_campaign_id;

COMMENT ON TABLE campaigns IS '캠페인 기본 정보를 저장하는 테이블';
COMMENT ON COLUMN campaigns.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaigns.name IS '이름';
COMMENT ON COLUMN campaigns.objective IS 'objective 컬럼';
COMMENT ON COLUMN campaigns.category IS 'category 컬럼';
COMMENT ON COLUMN campaigns.offer IS 'offer 컬럼';
COMMENT ON COLUMN campaigns.budget_krw IS 'budget_krw 컬럼';
COMMENT ON COLUMN campaigns.start_date IS 'start_date 컬럼';
COMMENT ON COLUMN campaigns.end_date IS 'end_date 컬럼';
COMMENT ON COLUMN campaigns.expected_ctr IS 'expected_ctr 컬럼';
COMMENT ON COLUMN campaigns.expected_cvr IS 'expected_cvr 컬럼';
COMMENT ON COLUMN campaigns.text_for_embedding IS 'text_for_embedding 컬럼';

COMMENT ON TABLE campaign_channels IS 'campaign_channels 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_channels.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_channels.channel IS '채널';

COMMENT ON TABLE campaign_target_segments IS 'campaign_target_segments 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_target_segments.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_target_segments.target_segment IS 'target_segment 컬럼';

COMMENT ON TABLE campaign_keywords IS 'campaign_keywords 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_keywords.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_keywords.keyword IS 'keyword 컬럼';

COMMENT ON TABLE campaign_message_examples IS 'campaign_message_examples 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_message_examples.example_id IS 'example_id 컬럼';
COMMENT ON COLUMN campaign_message_examples.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_message_examples.channel IS '채널';
COMMENT ON COLUMN campaign_message_examples.emphasis_type IS 'emphasis_type 컬럼';
COMMENT ON COLUMN campaign_message_examples.message_text IS 'message_text 컬럼';
COMMENT ON COLUMN campaign_message_examples.brand_tone IS 'brand_tone 컬럼';
COMMENT ON COLUMN campaign_message_examples.created_at IS '생성 일시';

COMMENT ON TABLE users IS '사용자 기본 정보를 저장하는 테이블';
COMMENT ON COLUMN users.user_id IS '사용자 ID';
COMMENT ON COLUMN users.age IS 'age 컬럼';
COMMENT ON COLUMN users.gender IS 'gender 컬럼';
COMMENT ON COLUMN users.region IS 'region 컬럼';
COMMENT ON COLUMN users.lifecycle IS 'lifecycle 컬럼';
COMMENT ON COLUMN users.avg_order_value_krw IS 'avg_order_value_krw 컬럼';
COMMENT ON COLUMN users.purchase_count_90d IS 'purchase_count_90d 컬럼';
COMMENT ON COLUMN users.last_active_days IS 'last_active_days 컬럼';
COMMENT ON COLUMN users.price_sensitivity IS 'price_sensitivity 컬럼';
COMMENT ON COLUMN users.predicted_ltv_segment IS 'predicted_ltv_segment 컬럼';
COMMENT ON COLUMN users.text_for_embedding IS 'text_for_embedding 컬럼';

COMMENT ON TABLE user_interests IS 'user_interests 정보를 저장하는 테이블';
COMMENT ON COLUMN user_interests.user_id IS '사용자 ID';
COMMENT ON COLUMN user_interests.interest IS 'interest 컬럼';

COMMENT ON TABLE user_preferred_channels IS 'user_preferred_channels 정보를 저장하는 테이블';
COMMENT ON COLUMN user_preferred_channels.user_id IS '사용자 ID';
COMMENT ON COLUMN user_preferred_channels.preferred_channel IS 'preferred_channel 컬럼';

COMMENT ON TABLE user_recent_behaviors IS 'user_recent_behaviors 정보를 저장하는 테이블';
COMMENT ON COLUMN user_recent_behaviors.user_id IS '사용자 ID';
COMMENT ON COLUMN user_recent_behaviors.behavior IS 'behavior 컬럼';

COMMENT ON TABLE recommendation_edges IS 'recommendation_edges 정보를 저장하는 테이블';
COMMENT ON COLUMN recommendation_edges.user_id IS '사용자 ID';
COMMENT ON COLUMN recommendation_edges.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN recommendation_edges.reason IS 'reason 컬럼';
COMMENT ON COLUMN recommendation_edges.label IS 'label 컬럼';

COMMENT ON TABLE campaign_target_audiences IS 'campaign_target_audiences 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_target_audiences.audience_id IS 'audience_id 컬럼';
COMMENT ON COLUMN campaign_target_audiences.audience_key IS 'audience_key 컬럼';
COMMENT ON COLUMN campaign_target_audiences.prompt IS 'prompt 컬럼';
COMMENT ON COLUMN campaign_target_audiences.query_parser IS 'query_parser 컬럼';
COMMENT ON COLUMN campaign_target_audiences.request_options IS 'request_options 컬럼';
COMMENT ON COLUMN campaign_target_audiences.generated_sql IS 'generated_sql 컬럼';
COMMENT ON COLUMN campaign_target_audiences.sql_hash IS 'sql_hash 컬럼';
COMMENT ON COLUMN campaign_target_audiences.query_plan IS 'query_plan 컬럼';
COMMENT ON COLUMN campaign_target_audiences.status IS '상태';
COMMENT ON COLUMN campaign_target_audiences.member_count IS 'member_count 컬럼';
COMMENT ON COLUMN campaign_target_audiences.target_customer_count IS 'target_customer_count 컬럼';
COMMENT ON COLUMN campaign_target_audiences.target_campaign_count IS 'target_campaign_count 컬럼';
COMMENT ON COLUMN campaign_target_audiences.failure_reason IS 'failure_reason 컬럼';
COMMENT ON COLUMN campaign_target_audiences.created_at IS '생성 일시';
COMMENT ON COLUMN campaign_target_audiences.completed_at IS 'completed_at 컬럼';
COMMENT ON COLUMN campaign_target_audiences.expires_at IS 'expires_at 컬럼';

COMMENT ON TABLE campaign_target_audience_members IS 'campaign_target_audience_members 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_target_audience_members.audience_id IS 'audience_id 컬럼';
COMMENT ON COLUMN campaign_target_audience_members.member_id IS 'member_id 컬럼';
COMMENT ON COLUMN campaign_target_audience_members.user_id IS '사용자 ID';
COMMENT ON COLUMN campaign_target_audience_members.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_target_audience_members.created_at IS '생성 일시';

COMMENT ON TABLE campaign_query_failure_logs IS 'campaign_query_failure_logs 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_query_failure_logs.failure_log_id IS 'failure_log_id 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.endpoint IS 'endpoint 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.prompt IS 'prompt 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.query_parser IS 'query_parser 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.api_status IS 'api_status 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.failure_stage IS 'failure_stage 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.failure_reason IS 'failure_reason 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.error_detail IS 'error_detail 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.generated_sql IS 'generated_sql 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.sql_hash IS 'sql_hash 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.request_options IS 'request_options 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.query_plan IS 'query_plan 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.missing_input_conditions IS 'missing_input_conditions 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.clarification_questions IS 'clarification_questions 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.selected_candidate IS 'selected_candidate 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.stage_log IS 'stage_log 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.context_metadata IS 'context_metadata 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.database_execution IS 'database_execution 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.message_generation IS 'message_generation 컬럼';
COMMENT ON COLUMN campaign_query_failure_logs.created_at IS '생성 일시';

COMMENT ON TABLE campaign_channel_messages IS 'campaign_channel_messages 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_channel_messages.message_id IS 'message_id 컬럼';
COMMENT ON COLUMN campaign_channel_messages.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_channel_messages.channel IS '채널';
COMMENT ON COLUMN campaign_channel_messages.send_type IS 'send_type 컬럼';
COMMENT ON COLUMN campaign_channel_messages.message_body IS '메시지 내용';
COMMENT ON COLUMN campaign_channel_messages.sent_at IS 'sent_at 컬럼';
COMMENT ON COLUMN campaign_channel_messages.send_status IS 'send_status 컬럼';
COMMENT ON COLUMN campaign_channel_messages.provider_message_id IS 'provider_message_id 컬럼';
COMMENT ON COLUMN campaign_channel_messages.created_at IS '생성 일시';

COMMENT ON TABLE campaign_experiments IS 'campaign_experiments 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_experiments.experiment_id IS 'experiment_id 컬럼';
COMMENT ON COLUMN campaign_experiments.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_experiments.experiment_name IS 'experiment_name 컬럼';
COMMENT ON COLUMN campaign_experiments.channel IS '채널';
COMMENT ON COLUMN campaign_experiments.status IS '상태';
COMMENT ON COLUMN campaign_experiments.assignment_method IS 'assignment_method 컬럼';
COMMENT ON COLUMN campaign_experiments.started_at IS 'started_at 컬럼';
COMMENT ON COLUMN campaign_experiments.ended_at IS 'ended_at 컬럼';
COMMENT ON COLUMN campaign_experiments.created_at IS '생성 일시';
COMMENT ON COLUMN campaign_experiments.updated_at IS '수정 일시';

COMMENT ON TABLE campaign_message_variants IS 'campaign_message_variants 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_message_variants.variant_id IS 'variant_id 컬럼';
COMMENT ON COLUMN campaign_message_variants.experiment_id IS 'experiment_id 컬럼';
COMMENT ON COLUMN campaign_message_variants.variant_code IS 'variant_code 컬럼';
COMMENT ON COLUMN campaign_message_variants.message_name IS 'message_name 컬럼';
COMMENT ON COLUMN campaign_message_variants.message_body IS '메시지 내용';
COMMENT ON COLUMN campaign_message_variants.landing_url IS 'landing_url 컬럼';
COMMENT ON COLUMN campaign_message_variants.allocation_weight IS 'allocation_weight 컬럼';
COMMENT ON COLUMN campaign_message_variants.is_control IS 'is_control 컬럼';
COMMENT ON COLUMN campaign_message_variants.ai_features IS 'ai_features 컬럼';
COMMENT ON COLUMN campaign_message_variants.created_at IS '생성 일시';

COMMENT ON TABLE campaign_message_deliveries IS 'campaign_message_deliveries 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_message_deliveries.delivery_id IS 'delivery_id 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.experiment_id IS 'experiment_id 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.variant_id IS 'variant_id 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN campaign_message_deliveries.user_id IS '사용자 ID';
COMMENT ON COLUMN campaign_message_deliveries.channel IS '채널';
COMMENT ON COLUMN campaign_message_deliveries.assignment_source IS 'assignment_source 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.model_version IS 'model_version 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.predicted_click_probability IS 'predicted_click_probability 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.provider_message_id IS 'provider_message_id 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.assigned_at IS 'assigned_at 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.requested_at IS 'requested_at 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.sent_at IS 'sent_at 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.final_status IS 'final_status 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.targeting_snapshot IS 'targeting_snapshot 컬럼';
COMMENT ON COLUMN campaign_message_deliveries.created_at IS '생성 일시';

COMMENT ON TABLE campaign_message_events IS 'campaign_message_events 정보를 저장하는 테이블';
COMMENT ON COLUMN campaign_message_events.event_id IS 'event_id 컬럼';
COMMENT ON COLUMN campaign_message_events.delivery_id IS 'delivery_id 컬럼';
COMMENT ON COLUMN campaign_message_events.event_type IS '이벤트 유형';
COMMENT ON COLUMN campaign_message_events.event_at IS '이벤트 발생 일시';
COMMENT ON COLUMN campaign_message_events.event_key IS 'event_key 컬럼';
COMMENT ON COLUMN campaign_message_events.provider_event_id IS 'provider_event_id 컬럼';
COMMENT ON COLUMN campaign_message_events.click_url IS 'click_url 컬럼';
COMMENT ON COLUMN campaign_message_events.conversion_type IS 'conversion_type 컬럼';
COMMENT ON COLUMN campaign_message_events.conversion_value_krw IS 'conversion_value_krw 컬럼';
COMMENT ON COLUMN campaign_message_events.device_type IS 'device_type 컬럼';
COMMENT ON COLUMN campaign_message_events.os_name IS 'os_name 컬럼';
COMMENT ON COLUMN campaign_message_events.browser_name IS 'browser_name 컬럼';
COMMENT ON COLUMN campaign_message_events.ip_hash IS 'ip_hash 컬럼';
COMMENT ON COLUMN campaign_message_events.user_agent IS 'user_agent 컬럼';
COMMENT ON COLUMN campaign_message_events.event_properties IS 'event_properties 컬럼';
COMMENT ON COLUMN campaign_message_events.received_at IS 'received_at 컬럼';

COMMENT ON TABLE shopping_carts IS 'shopping_carts 정보를 저장하는 테이블';
COMMENT ON COLUMN shopping_carts.cart_id IS 'cart_id 컬럼';
COMMENT ON COLUMN shopping_carts.user_id IS '사용자 ID';
COMMENT ON COLUMN shopping_carts.cart_status IS 'cart_status 컬럼';
COMMENT ON COLUMN shopping_carts.source_channel IS 'source_channel 컬럼';
COMMENT ON COLUMN shopping_carts.total_quantity IS 'total_quantity 컬럼';
COMMENT ON COLUMN shopping_carts.original_amount_krw IS 'original_amount_krw 컬럼';
COMMENT ON COLUMN shopping_carts.discount_amount_krw IS 'discount_amount_krw 컬럼';
COMMENT ON COLUMN shopping_carts.shipping_fee_krw IS 'shipping_fee_krw 컬럼';
COMMENT ON COLUMN shopping_carts.total_amount_krw IS 'total_amount_krw 컬럼';
COMMENT ON COLUMN shopping_carts.purchase_completed IS 'purchase_completed 컬럼';
COMMENT ON COLUMN shopping_carts.abandonment_reason IS 'abandonment_reason 컬럼';
COMMENT ON COLUMN shopping_carts.created_at IS '생성 일시';
COMMENT ON COLUMN shopping_carts.last_activity_at IS 'last_activity_at 컬럼';
COMMENT ON COLUMN shopping_carts.abandoned_at IS 'abandoned_at 컬럼';
COMMENT ON COLUMN shopping_carts.purchased_at IS 'purchased_at 컬럼';
COMMENT ON COLUMN shopping_carts.expires_at IS 'expires_at 컬럼';
COMMENT ON COLUMN shopping_carts.updated_at IS '수정 일시';

COMMENT ON TABLE shopping_cart_items IS 'shopping_cart_items 정보를 저장하는 테이블';
COMMENT ON COLUMN shopping_cart_items.cart_item_id IS 'cart_item_id 컬럼';
COMMENT ON COLUMN shopping_cart_items.cart_id IS 'cart_id 컬럼';
COMMENT ON COLUMN shopping_cart_items.product_id IS 'product_id 컬럼';
COMMENT ON COLUMN shopping_cart_items.product_name IS 'product_name 컬럼';
COMMENT ON COLUMN shopping_cart_items.category IS 'category 컬럼';
COMMENT ON COLUMN shopping_cart_items.brand IS 'brand 컬럼';
COMMENT ON COLUMN shopping_cart_items.quantity IS 'quantity 컬럼';
COMMENT ON COLUMN shopping_cart_items.unit_price_krw IS 'unit_price_krw 컬럼';
COMMENT ON COLUMN shopping_cart_items.discount_price_krw IS 'discount_price_krw 컬럼';
COMMENT ON COLUMN shopping_cart_items.final_unit_price_krw IS 'final_unit_price_krw 컬럼';
COMMENT ON COLUMN shopping_cart_items.inventory_quantity IS 'inventory_quantity 컬럼';
COMMENT ON COLUMN shopping_cart_items.inventory_status IS 'inventory_status 컬럼';
COMMENT ON COLUMN shopping_cart_items.coupon_available IS 'coupon_available 컬럼';
COMMENT ON COLUMN shopping_cart_items.coupon_code IS 'coupon_code 컬럼';
COMMENT ON COLUMN shopping_cart_items.coupon_expire_at IS 'coupon_expire_at 컬럼';
COMMENT ON COLUMN shopping_cart_items.wishlist_flag IS 'wishlist_flag 컬럼';
COMMENT ON COLUMN shopping_cart_items.viewed_count IS 'viewed_count 컬럼';
COMMENT ON COLUMN shopping_cart_items.added_at IS 'added_at 컬럼';
COMMENT ON COLUMN shopping_cart_items.last_viewed_at IS 'last_viewed_at 컬럼';
COMMENT ON COLUMN shopping_cart_items.removed_at IS 'removed_at 컬럼';
COMMENT ON COLUMN shopping_cart_items.created_at IS '생성 일시';

COMMENT ON TABLE cart_reminder_history IS 'cart_reminder_history 정보를 저장하는 테이블';
COMMENT ON COLUMN cart_reminder_history.reminder_id IS 'reminder_id 컬럼';
COMMENT ON COLUMN cart_reminder_history.cart_id IS 'cart_id 컬럼';
COMMENT ON COLUMN cart_reminder_history.campaign_id IS '캠페인 ID';
COMMENT ON COLUMN cart_reminder_history.delivery_id IS 'delivery_id 컬럼';
COMMENT ON COLUMN cart_reminder_history.channel IS '채널';
COMMENT ON COLUMN cart_reminder_history.reminder_sequence IS 'reminder_sequence 컬럼';
COMMENT ON COLUMN cart_reminder_history.reminder_type IS 'reminder_type 컬럼';
COMMENT ON COLUMN cart_reminder_history.coupon_code IS 'coupon_code 컬럼';
COMMENT ON COLUMN cart_reminder_history.sent_at IS 'sent_at 컬럼';
COMMENT ON COLUMN cart_reminder_history.delivered_at IS 'delivered_at 컬럼';
COMMENT ON COLUMN cart_reminder_history.clicked_at IS 'clicked_at 컬럼';
COMMENT ON COLUMN cart_reminder_history.converted_at IS 'converted_at 컬럼';
COMMENT ON COLUMN cart_reminder_history.conversion_order_id IS 'conversion_order_id 컬럼';
COMMENT ON COLUMN cart_reminder_history.conversion_value_krw IS 'conversion_value_krw 컬럼';
COMMENT ON COLUMN cart_reminder_history.created_at IS '생성 일시';


-- ============================================================
-- Added: 6개월 이상 미접속 휴면 고객 재활성화 데이터/조회 패치
-- ============================================================

-- ============================================================
-- 6개월 이상 미접속 휴면 고객 재활성화용 패치
-- PostgreSQL 14+
-- 기존 전체 스키마 실행 후 이 파일을 실행하세요.
-- ============================================================

BEGIN;

-- 1) Query Plan이 달력 기준 "6개월"을 직접 해석할 수 있도록
--    users에 마지막 접속 일시 컬럼을 추가합니다.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

-- 기존 데이터는 last_active_days를 기준으로 마지막 접속 일시를 역산합니다.
UPDATE users
SET last_login_at = CURRENT_TIMESTAMP - (last_active_days * INTERVAL '1 day')
WHERE last_login_at IS NULL;

ALTER TABLE users
    ALTER COLUMN last_login_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_last_login_at
    ON users(last_login_at);

CREATE INDEX IF NOT EXISTS idx_users_dormant_reactivation
    ON users(last_login_at, lifecycle)
    WHERE lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant');

COMMENT ON COLUMN users.last_login_at IS
    '고객의 마지막 로그인/접속 일시. 6개월 이상 미접속 조건은 CURRENT_TIMESTAMP - INTERVAL ''6 months''와 비교한다.';

-- 2) 재활성화 캠페인이 실행 시점에도 유효하도록 보정합니다.
UPDATE campaigns
SET start_date = LEAST(start_date, CURRENT_DATE),
    end_date = GREATEST(end_date, CURRENT_DATE + 30),
    objective = 'reactivation'
WHERE campaign_id = 'camp_004';

-- Query Plan의 명시적 6개월 세그먼트와 맞춥니다.
INSERT INTO campaign_target_segments (campaign_id, target_segment)
VALUES ('camp_004', 'inactive_180d')
ON CONFLICT DO NOTHING;

-- 3) 6개월 이상 미접속 샘플 고객을 추가합니다.
--    last_active_days와 last_login_at을 모두 제공하여 어떤 방식의 SQL도 결과를 반환하게 합니다.
INSERT INTO users (
    user_id, age, gender, region, lifecycle,
    avg_order_value_krw, purchase_count_90d, last_active_days,
    price_sensitivity, predicted_ltv_segment, text_for_embedding,
    last_login_at
) VALUES
('user_026', 32, 'female', 'Seoul',    'inactive_180d', 72000, 0, 181, 'high',   'mid',
 'user_026 사용자는 181일 이상 미접속한 휴면 고객입니다. 복귀 쿠폰에 반응 가능성이 높고 kakao 채널을 선호합니다.', CURRENT_TIMESTAMP - INTERVAL '181 days'),
('user_027', 45, 'male',   'Busan',   'inactive_180d', 118000, 0, 195, 'medium', 'high',
 'user_027 사용자는 약 6개월 이상 미접속한 고가치 휴면 고객입니다. sms 채널과 할인 혜택이 적합합니다.', CURRENT_TIMESTAMP - INTERVAL '195 days'),
('user_028', 29, 'female', 'Gyeonggi','inactive_180d', 46000, 0, 220, 'high',   'low',
 'user_028 사용자는 220일 미접속 휴면 고객이며 가격 민감도가 높아 복귀 쿠폰 타겟입니다.', CURRENT_TIMESTAMP - INTERVAL '220 days'),
('user_029', 51, 'male',   'Daegu',   'dormant',       155000, 0, 250, 'low',    'high',
 'user_029 사용자는 250일 미접속한 고가치 휴면 고객이며 개인화된 복귀 메시지가 적합합니다.', CURRENT_TIMESTAMP - INTERVAL '250 days'),
('user_030', 38, 'female', 'Incheon', 'inactive_180d', 83000, 0, 275, 'medium', 'mid',
 'user_030 사용자는 275일 미접속한 휴면 고객이며 kakao와 sms 재활성화 캠페인 대상입니다.', CURRENT_TIMESTAMP - INTERVAL '275 days'),
('user_031', 26, 'male',   'Daejeon', 'inactive_180d', 35000, 0, 310, 'high',   'low',
 'user_031 사용자는 310일 미접속한 휴면 고객이며 할인 중심 복귀 메시지가 적합합니다.', CURRENT_TIMESTAMP - INTERVAL '310 days'),
('user_032', 43, 'female', 'Gwangju', 'dormant',       99000, 0, 365, 'medium', 'high',
 'user_032 사용자는 1년가량 미접속한 고가치 휴면 고객이며 복귀 혜택 캠페인 대상입니다.', CURRENT_TIMESTAMP - INTERVAL '365 days'),
('user_033', 57, 'male',   'Ulsan',   'dormant',       132000, 0, 420, 'low',    'high',
 'user_033 사용자는 420일 미접속한 장기 휴면 고객이며 sms 기반 복귀 안내가 적합합니다.', CURRENT_TIMESTAMP - INTERVAL '420 days')
ON CONFLICT (user_id) DO UPDATE SET
    lifecycle = EXCLUDED.lifecycle,
    avg_order_value_krw = EXCLUDED.avg_order_value_krw,
    purchase_count_90d = EXCLUDED.purchase_count_90d,
    last_active_days = EXCLUDED.last_active_days,
    price_sensitivity = EXCLUDED.price_sensitivity,
    predicted_ltv_segment = EXCLUDED.predicted_ltv_segment,
    text_for_embedding = EXCLUDED.text_for_embedding,
    last_login_at = EXCLUDED.last_login_at;

-- 4) 채널, 행동, 추천 관계를 추가합니다.
INSERT INTO user_preferred_channels (user_id, preferred_channel) VALUES
('user_026', 'kakao'), ('user_026', 'sms'),
('user_027', 'sms'),   ('user_027', 'email'),
('user_028', 'kakao'),
('user_029', 'sms'),
('user_030', 'kakao'), ('user_030', 'sms'),
('user_031', 'sms'),
('user_032', 'kakao'), ('user_032', 'email'),
('user_033', 'sms')
ON CONFLICT DO NOTHING;

INSERT INTO user_recent_behaviors (user_id, behavior) VALUES
('user_026', 'inactive_180d'), ('user_026', 'discount_responsive'),
('user_027', 'inactive_180d'), ('user_027', 'high_ltv'),
('user_028', 'inactive_180d'), ('user_028', 'coupon_clicked_before'),
('user_029', 'long_term_dormant'), ('user_029', 'high_ltv'),
('user_030', 'inactive_180d'), ('user_030', 'discount_responsive'),
('user_031', 'long_term_dormant'), ('user_031', 'price_sensitive'),
('user_032', 'long_term_dormant'), ('user_032', 'high_ltv'),
('user_033', 'long_term_dormant'), ('user_033', 'sms_preferred')
ON CONFLICT DO NOTHING;

INSERT INTO recommendation_edges (user_id, campaign_id, reason, label) VALUES
('user_026', 'camp_004', '181일 미접속 및 할인 반응 가능성', 'high'),
('user_027', 'camp_004', '195일 미접속 고가치 고객', 'high'),
('user_028', 'camp_004', '220일 미접속 가격 민감 고객', 'high'),
('user_029', 'camp_004', '250일 미접속 고가치 장기 휴면 고객', 'high'),
('user_030', 'camp_004', '275일 미접속 및 메시지 채널 일치', 'high'),
('user_031', 'camp_004', '310일 미접속 가격 민감 고객', 'medium'),
('user_032', 'camp_004', '365일 미접속 고가치 장기 휴면 고객', 'high'),
('user_033', 'camp_004', '420일 미접속 SMS 선호 고객', 'medium')
ON CONFLICT (user_id, campaign_id) DO UPDATE SET
    reason = EXCLUDED.reason,
    label = EXCLUDED.label;

-- 5) Query Plan이 바로 사용할 수 있는 검증된 타겟 뷰입니다.
CREATE OR REPLACE VIEW v_dormant_6m_reactivation_targets AS
SELECT
    u.user_id,
    u.age,
    u.gender,
    u.region,
    u.lifecycle,
    u.last_login_at,
    (CURRENT_DATE - u.last_login_at::date) AS inactive_days,
    u.avg_order_value_krw,
    u.price_sensitivity,
    u.predicted_ltv_segment,
    c.campaign_id,
    c.name AS campaign_name,
    c.offer,
    ARRAY_AGG(DISTINCT upc.preferred_channel)
        FILTER (WHERE upc.preferred_channel IS NOT NULL) AS preferred_channels,
    re.label AS recommendation_label,
    re.reason AS recommendation_reason
FROM users u
JOIN recommendation_edges re
  ON re.user_id = u.user_id
 AND re.campaign_id = 'camp_004'
JOIN campaigns c
  ON c.campaign_id = re.campaign_id
 AND c.objective = 'reactivation'
LEFT JOIN user_preferred_channels upc
  ON upc.user_id = u.user_id
WHERE u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND u.purchase_count_90d = 0
  AND u.lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant')
GROUP BY
    u.user_id, u.age, u.gender, u.region, u.lifecycle,
    u.last_login_at, u.avg_order_value_krw, u.price_sensitivity,
    u.predicted_ltv_segment, c.campaign_id, c.name, c.offer,
    re.label, re.reason;

COMMENT ON VIEW v_dormant_6m_reactivation_targets IS
    '마지막 접속일이 현재 시점 기준 6개월 이전이고 최근 90일 구매가 없는 재활성화 캠페인 대상 고객';

COMMIT;

-- ============================================================
-- 검증 SQL: 최소 8행이 나와야 정상입니다.
-- ============================================================
SELECT COUNT(*) AS dormant_6m_customer_count
FROM v_dormant_6m_reactivation_targets;

SELECT
    user_id,
    lifecycle,
    last_login_at,
    inactive_days,
    campaign_id,
    campaign_name,
    offer,
    preferred_channels,
    recommendation_label
FROM v_dormant_6m_reactivation_targets
ORDER BY inactive_days DESC, user_id;

-- Query Plan에서 뷰를 쓰지 않고 직접 생성할 경우 사용할 검증된 SQL
SELECT
    u.user_id,
    u.age,
    u.gender,
    u.region,
    u.last_login_at,
    CURRENT_DATE - u.last_login_at::date AS inactive_days,
    u.predicted_ltv_segment,
    c.campaign_id,
    c.name AS campaign_name,
    c.offer
FROM users u
CROSS JOIN campaigns c
WHERE u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND u.purchase_count_90d = 0
  AND u.lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant')
  AND c.objective = 'reactivation'
  AND c.start_date <= CURRENT_DATE
  AND c.end_date >= CURRENT_DATE
ORDER BY inactive_days DESC, u.user_id;

-- ============================================================
-- Seed data for 6+ month dormant LMS reactivation targeting
-- Target query conditions satisfied:
--   last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
--   purchase_count_90d = 0
--   lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant')
--   preferred_channel = 'lms'
--   campaign objective = 'reactivation'
--   campaign channel = 'lms'
-- PostgreSQL 14+
-- ============================================================

BEGIN;

-- Ensure the reactivation campaign supports LMS.
INSERT INTO campaign_channels (campaign_id, channel)
VALUES ('camp_004', 'lms')
ON CONFLICT (campaign_id, channel) DO NOTHING;

-- Existing dormant users: make them satisfy the 6-month condition.
UPDATE users
SET last_login_at = CURRENT_TIMESTAMP - INTERVAL '240 days',
    last_active_days = 240,
    purchase_count_90d = 0,
    lifecycle = 'inactive_180d'
WHERE user_id = 'user_005';

UPDATE users
SET last_login_at = CURRENT_TIMESTAMP - INTERVAL '310 days',
    last_active_days = 310,
    purchase_count_90d = 0,
    lifecycle = 'dormant'
WHERE user_id = 'user_017';

-- Add LMS as a preferred channel.
INSERT INTO user_preferred_channels (user_id, preferred_channel)
VALUES
    ('user_005', 'lms'),
    ('user_017', 'lms')
ON CONFLICT (user_id, preferred_channel) DO NOTHING;

-- Connect existing users to the LMS reactivation campaign.
INSERT INTO recommendation_edges (user_id, campaign_id, reason, label)
VALUES
    ('user_005', 'camp_004', '6개월 이상 미접속, 최근 구매 없음, LMS 선호 휴면 고객', 'high'),
    ('user_017', 'camp_004', '6개월 이상 미접속, 최근 구매 없음, LMS 선호 휴면 고객', 'high')
ON CONFLICT (user_id, campaign_id)
DO UPDATE SET
    reason = EXCLUDED.reason,
    label = EXCLUDED.label;

-- Additional deterministic dormant users.
INSERT INTO users (
    user_id,
    age,
    gender,
    region,
    lifecycle,
    avg_order_value_krw,
    purchase_count_90d,
    last_active_days,
    price_sensitivity,
    predicted_ltv_segment,
    text_for_embedding,
    last_login_at
)
VALUES
    (
        'user_026', 32, 'female', 'Seoul', 'inactive_180d',
        72000, 0, 205, 'high', 'mid',
        'user_026 사용자는 205일 이상 미접속한 휴면 고객이며 LMS 채널을 선호합니다. 최근 90일 구매가 없고 복귀 할인 혜택에 반응할 가능성이 높습니다.',
        CURRENT_TIMESTAMP - INTERVAL '205 days'
    ),
    (
        'user_027', 45, 'male', 'Gyeonggi', 'dormant',
        118000, 0, 275, 'medium', 'high',
        'user_027 사용자는 275일 이상 미접속한 장기 휴면 고객이며 LMS 채널을 선호합니다. 최근 90일 구매가 없고 재활성화 캠페인 대상입니다.',
        CURRENT_TIMESTAMP - INTERVAL '275 days'
    ),
    (
        'user_028', 39, 'female', 'Busan', 'inactive_90d',
        46000, 0, 190, 'high', 'low',
        'user_028 사용자는 190일 이상 미접속한 휴면 고객이며 LMS 채널을 선호합니다. 가격 민감도가 높아 복귀 쿠폰 캠페인에 적합합니다.',
        CURRENT_TIMESTAMP - INTERVAL '190 days'
    )
ON CONFLICT (user_id)
DO UPDATE SET
    age = EXCLUDED.age,
    gender = EXCLUDED.gender,
    region = EXCLUDED.region,
    lifecycle = EXCLUDED.lifecycle,
    avg_order_value_krw = EXCLUDED.avg_order_value_krw,
    purchase_count_90d = EXCLUDED.purchase_count_90d,
    last_active_days = EXCLUDED.last_active_days,
    price_sensitivity = EXCLUDED.price_sensitivity,
    predicted_ltv_segment = EXCLUDED.predicted_ltv_segment,
    text_for_embedding = EXCLUDED.text_for_embedding,
    last_login_at = EXCLUDED.last_login_at;

INSERT INTO user_preferred_channels (user_id, preferred_channel)
VALUES
    ('user_026', 'lms'),
    ('user_027', 'lms'),
    ('user_028', 'lms')
ON CONFLICT (user_id, preferred_channel) DO NOTHING;

INSERT INTO user_recent_behaviors (user_id, behavior)
VALUES
    ('user_026', 'inactive_6m_plus'),
    ('user_026', 'discount_responsive'),
    ('user_027', 'inactive_6m_plus'),
    ('user_027', 'previous_high_value_customer'),
    ('user_028', 'inactive_6m_plus'),
    ('user_028', 'coupon_sensitive')
ON CONFLICT (user_id, behavior) DO NOTHING;

INSERT INTO recommendation_edges (user_id, campaign_id, reason, label)
VALUES
    ('user_026', 'camp_004', '205일 미접속, 최근 구매 없음, LMS 선호, 할인 반응 가능성', 'high'),
    ('user_027', 'camp_004', '275일 미접속 장기 휴면, 최근 구매 없음, LMS 선호', 'high'),
    ('user_028', 'camp_004', '190일 미접속, 가격 민감, LMS 선호 복귀 대상', 'high')
ON CONFLICT (user_id, campaign_id)
DO UPDATE SET
    reason = EXCLUDED.reason,
    label = EXCLUDED.label;

COMMIT;

-- ============================================================
-- Validation: should return at least 5 users.
-- ============================================================
SELECT DISTINCT
    u.user_id,
    u.age,
    u.gender,
    u.price_sensitivity,
    c.campaign_id,
    NULL AS name_masked,
    c.objective,
    c.category,
    c.offer,
    c.start_date,
    c.end_date,
    u.last_login_at,
    CURRENT_DATE - u.last_login_at::date AS inactive_days,
    u.lifecycle
FROM users u
JOIN recommendation_edges re
    ON re.user_id = u.user_id
JOIN campaigns c
    ON c.campaign_id = re.campaign_id
JOIN user_preferred_channels upc
    ON upc.user_id = u.user_id
JOIN campaign_channels cc
    ON cc.campaign_id = c.campaign_id
WHERE u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL '6 months'
  AND u.purchase_count_90d = 0
  AND u.lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant')
  AND upc.preferred_channel = 'lms'
  AND c.objective = 'reactivation'
  AND cc.channel = 'lms'
ORDER BY inactive_days DESC, u.user_id ASC
LIMIT 100;
