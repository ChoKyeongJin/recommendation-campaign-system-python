-- ============================================================
-- 앱 메타데이터 전용 로컬 DB 스키마
-- 실제(business) DB는 SELECT 전용으로만 연결하므로, 앱이 직접 쓰는
-- 자기완결(self-contained) 메타데이터 테이블만 이 로컬 DB에 둔다.
--
-- 여기에는 campaigns/users 등 business 테이블에 대한 FK가 전혀 없다.
-- user_id / campaign_id 는 read-only business DB의 식별자를 그대로
-- 평문(VARCHAR)으로 보관한다(교차 DB FK는 사용하지 않음).
--
-- 주의: docs/data/local_bootstrap.sql 은 business DB(전체 스키마 + 시드)용이며
--       실제 DB에 절대 실행하지 않는다. 이 파일은 그 하위 집합이다.
-- PostgreSQL 14+
-- ============================================================

DROP TABLE IF EXISTS campaign_audience_migration_log CASCADE;
DROP TABLE IF EXISTS campaign_audience_migration CASCADE;
DROP TABLE IF EXISTS campaign_target_audience_members CASCADE;
DROP TABLE IF EXISTS campaign_target_audiences CASCADE;
DROP TABLE IF EXISTS campaign_query_failure_logs CASCADE;
DROP TABLE IF EXISTS campaign_prompt_templates CASCADE;
DROP TABLE IF EXISTS campaign_policies CASCADE;

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
-- 멤버 행은 user_id/campaign_id 식별자만 좁게 저장합니다(business DB의 값).
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
CREATE TABLE campaign_prompt_templates (
    name        VARCHAR(120) PRIMARY KEY,
    content     TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- legacy 오디언스 슬롯 → Event IR 이행 상태입니다(자산 하나 + revision 하나 = 한 행).
-- campaign_target_audiences 에 revision 컬럼이 없어 이행 상태는 여기서 소유하고, cut-over 는
-- 아래 값 전부를 WHERE 로 거는 compare-and-swap 으로만 이뤄집니다(갱신 행 0 = stale/동시 수정 → 중단).
-- 실행 권위 자체는 campaign_target_audiences.query_plan 의 audience_authority 필드에 있습니다.
CREATE TABLE campaign_audience_migration (
    asset_id                VARCHAR(200) NOT NULL,
    revision                INTEGER      NOT NULL,
    migration_status        VARCHAR(40)  NOT NULL,
    source_fingerprint      VARCHAR(64)  NOT NULL,
    source_schema_checksum  VARCHAR(64)  NOT NULL,
    semantic_fingerprint    VARCHAR(64)  NOT NULL DEFAULT '',
    binding_fingerprint     VARCHAR(64)  NOT NULL DEFAULT '',
    legacy_payload          JSONB        NOT NULL,          -- rollback 의 유일한 재료(역변환 없음)
    legacy_payload_checksum VARCHAR(64)  NOT NULL,          -- 저장 무결성(의미 지문과 다른 질문)
    event_expression        JSONB,                          -- 검증을 통과한 산출물 그대로
    verification_digest     VARCHAR(64)  NOT NULL DEFAULT '',
    verified_at             TIMESTAMPTZ,
    row_version             INTEGER      NOT NULL DEFAULT 1,
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by              VARCHAR(120) NOT NULL DEFAULT '',

    PRIMARY KEY (asset_id, revision)
);

-- 권위가 움직인 사건의 append-only 기록입니다. 상태 표는 '지금'만 말하고 여기가 '언제 누가 왜'를 답합니다.
CREATE TABLE campaign_audience_migration_log (
    log_id             BIGSERIAL PRIMARY KEY,
    asset_id           VARCHAR(200) NOT NULL,
    revision           INTEGER      NOT NULL,
    action             VARCHAR(20)  NOT NULL,
    from_status        VARCHAR(40),
    to_status          VARCHAR(40),
    audience_authority VARCHAR(20)  NOT NULL,
    actor              VARCHAR(120) NOT NULL,
    evidence_digest    VARCHAR(64)  NOT NULL DEFAULT '',
    reasons            JSONB        NOT NULL DEFAULT '{}'::JSONB,
    note               TEXT         NOT NULL DEFAULT '',
    occurred_at        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 정책(JSON) 파일을 파일 대신 DB에서 관리합니다.
CREATE TABLE campaign_policies (
    name        VARCHAR(120) PRIMARY KEY,
    content     JSONB NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
CREATE INDEX idx_campaign_audience_migration_log_asset
    ON campaign_audience_migration_log(asset_id, revision, occurred_at);
