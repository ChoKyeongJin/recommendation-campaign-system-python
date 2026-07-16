from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from graph_rag import (
    DEFAULT_COLLECTION,
    DEFAULT_DATA_PATH,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_MESSAGE_POLICY_PATH,
    DEFAULT_METRIC_LEXICON_PATH,
    DEFAULT_NORMALIZATION_PATH,
    DEFAULT_POLICY_PATH,
    DEFAULT_PROMPT_DIR,
    build_message_context,
    build_message_response,
    build_graph,
    graph_stats,
    load_payload,
    rag_llm_run_scope,
    render_message_prompt,
    retrieve,
)
from sql_guard import DEFAULT_LIMIT, DEFAULT_SCHEMA_PATH


api_logger = logging.getLogger("campaign_api")
if not api_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    api_logger.addHandler(handler)
api_logger.propagate = False
api_logger.setLevel(os.getenv("API_LOG_LEVEL", "INFO").upper())


POLICY_DIR = Path(__file__).resolve().parent / "docs" / "policies"
DEFAULT_HEURISTIC_CTR_RULES_PATH = POLICY_DIR / "heuristic-ctr-rules.json"
DEFAULT_CTR_MODEL_POLICY_PATH = POLICY_DIR / "ctr-model-policy.json"
# campaign_policies 테이블의 조회 키(= 정책 파일명에서 확장자를 뺀 이름).
CTR_MODEL_POLICY_NAME = "ctr-model-policy"
HEURISTIC_CTR_RULES_NAME = "heuristic-ctr-rules"
DEFAULT_HEURISTIC_CTR_RULES: dict[str, Any] = {
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
        "control_variant": 0.001,
    },
    "matchers": {
        "urgency_recent_behavior_keywords": ["cart_abandoned", "deal"],
        "personalized_lifecycles": ["active", "cart_abandoner", "vip"],
    },
}
DEFAULT_CTR_MODEL_POLICY: dict[str, Any] = {
    "default_model_version": "heuristic-ctr-v1",
    "heuristic_model_version_prefixes": ["heuristic"],
    "fallback_to_heuristic_on_ml_error": True,
    "exploration_enabled": False,
    "default_epsilon": 0.0,
    "allow_request_epsilon_override": False,
}


class Utf8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class MessageGenerationOptions(BaseModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=100)
    timeout_seconds: float | None = Field(default=None, ge=1.0)
    max_attempts: int | None = Field(default=None, ge=1)


class PromptTemplateUpsertRequest(BaseModel):
    content: str = Field(..., min_length=1, description="프롬프트 템플릿 본문(파일 내용과 동일한 형식).")
    description: str | None = Field(default=None, description="프롬프트 용도를 설명하는 선택 메모.")


class PolicyUpsertRequest(BaseModel):
    content: dict[str, Any] = Field(..., description="정책 JSON 객체(파일 내용과 동일한 형식).")
    description: str | None = Field(default=None, description="정책 용도를 설명하는 선택 메모.")


class TargetSqlRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Natural language prompt used to generate targeting SQL.")
    query_parser: Literal["rules", "auto", "llm"] = Field(default=os.getenv("QUERY_PARSER", "rules"))
    generate_answer: bool = False
    generate_messages: bool = False
    message_generation_options: MessageGenerationOptions | None = None
    message_channel: Literal["auto", "lms", "rcs", "rcsSms"] = "auto"
    collection: str = Field(default=os.getenv("QDRANT_GRAPH_COLLECTION", DEFAULT_COLLECTION))
    vector_top_k: int = Field(default=_env_int("GRAPH_RAG_VECTOR_TOP_K", 0), ge=0)
    keyword_top_k: int = Field(default=_env_int("GRAPH_RAG_KEYWORD_TOP_K", 5), ge=0)
    graph_top_k: int = Field(default=_env_int("GRAPH_RAG_GRAPH_TOP_K", 15), ge=1)
    hops: int = Field(default=_env_int("GRAPH_RAG_HOPS", 2), ge=0)
    sql_limit: int = Field(default=_env_int("GRAPH_RAG_SQL_LIMIT", DEFAULT_LIMIT), ge=1)
    execute_sql: bool = True
    result_row_limit: int = Field(default=_env_int("TARGET_SQL_RESULT_ROW_LIMIT", 100), ge=1)
    persist_targeting: bool = True
    audience_ttl_days: int = Field(default=_env_int("TARGET_SQL_AUDIENCE_TTL_DAYS", 90), ge=1)
    include_debug: bool = False


class ChannelMessagesRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Natural language prompt used to generate channel message recommendations.")
    message_channel: Literal["auto", "lms", "rcs", "rcsSms"] = "lms"
    message_generation_options: MessageGenerationOptions | None = None
    query_parser: Literal["rules", "auto", "llm"] = Field(default=os.getenv("QUERY_PARSER", "rules"))
    collection: str = Field(default=os.getenv("QDRANT_GRAPH_COLLECTION", DEFAULT_COLLECTION))
    vector_top_k: int = Field(default=_env_int("GRAPH_RAG_VECTOR_TOP_K", 0), ge=0)
    keyword_top_k: int = Field(default=_env_int("GRAPH_RAG_KEYWORD_TOP_K", 5), ge=0)
    graph_top_k: int = Field(default=_env_int("GRAPH_RAG_GRAPH_TOP_K", 15), ge=1)
    hops: int = Field(default=_env_int("GRAPH_RAG_HOPS", 2), ge=0)
    sql_limit: int = Field(default=_env_int("GRAPH_RAG_SQL_LIMIT", DEFAULT_LIMIT), ge=1)
    result_row_limit: int = Field(default=_env_int("TARGET_SQL_RESULT_ROW_LIMIT", 100), ge=1)
    include_debug: bool = False


class ApiBaseModel(BaseModel):
    class Config:
        allow_population_by_field_name = True
        populate_by_name = True


class CampaignMessageVariantRequest(ApiBaseModel):
    code: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=100)
    message_body: str = Field(..., alias="messageBody", min_length=1)
    landing_url: str | None = Field(default=None, alias="landingUrl")
    allocation_weight: float = Field(default=1.0, alias="allocationWeight", gt=0)
    is_control: bool = Field(default=False, alias="isControl")
    ai_features: dict[str, Any] | None = Field(default=None, alias="aiFeatures")


class CampaignExperimentCreateRequest(ApiBaseModel):
    campaign_id: str = Field(..., alias="campaignId", min_length=1, max_length=20)
    experiment_name: str = Field(..., alias="experimentName", min_length=1, max_length=200)
    channel: str = Field(..., min_length=1, max_length=50)
    status: Literal["draft", "running", "paused", "completed", "cancelled"] = "running"
    assignment_method: Literal["random", "weighted_random", "manual", "model"] = Field(default="random", alias="assignmentMethod")
    primary_metric: Literal["delivery_rate", "impression_rate", "open_rate", "ctr", "cvr", "revenue"] = Field(default="ctr", alias="primaryMetric")
    variants: list[CampaignMessageVariantRequest] = Field(..., min_length=3, max_length=3)


class CampaignExperimentRunRequest(ApiBaseModel):
    campaign_id: str = Field(..., alias="campaignId", min_length=1, max_length=20)
    experiment_name: str = Field(..., alias="experimentName", min_length=1, max_length=200)
    channel: str = Field(..., min_length=1, max_length=50)
    primary_metric: Literal["delivery_rate", "impression_rate", "open_rate", "ctr", "cvr", "revenue"] = Field(default="ctr", alias="primaryMetric")
    assignment_method: Literal["random", "weighted_random", "model"] = Field(default="random", alias="assignmentMethod")
    variants: list[CampaignMessageVariantRequest] = Field(..., min_length=3, max_length=3)
    user_ids: list[str] | None = Field(default=None, alias="userIds")
    audience_id: int | None = Field(default=None, alias="audienceId", ge=1)
    model_version: str | None = Field(default=None, alias="modelVersion", min_length=1, max_length=100)
    epsilon: float | None = Field(default=None, ge=0.0, le=1.0)
    provider_message_id_prefix: str | None = Field(default=None, alias="providerMessageIdPrefix", max_length=80)
    limit: int = Field(default=1000, ge=1, le=10000)
    include_analysis: bool = Field(default=False, alias="includeAnalysis")


class CtrScoreRequest(ApiBaseModel):
    experiment_id: int = Field(..., alias="experimentId", ge=1)
    campaign_id: str | None = Field(default=None, alias="campaignId", min_length=1, max_length=20)
    prompt: str | None = Field(default=None, min_length=1)
    channel: str | None = Field(default=None, min_length=1, max_length=50)
    variants: list[CampaignMessageVariantRequest] | None = Field(default=None, min_length=1, max_length=10)
    user_ids: list[str] | None = Field(default=None, alias="userIds", min_length=1)
    audience_id: int | None = Field(default=None, alias="audienceId", ge=1)
    model_version: str | None = Field(default=None, alias="modelVersion", min_length=1, max_length=100)
    limit: int = Field(default=1000, ge=1, le=10000)


class AssignmentCreateRequest(ApiBaseModel):
    user_ids: list[str] | None = Field(default=None, alias="userIds")
    audience_id: int | None = Field(default=None, alias="audienceId", ge=1)
    assignment_method: Literal["random", "weighted_random", "model"] | None = Field(default=None, alias="assignmentMethod")
    model_version: str | None = Field(default=None, alias="modelVersion", min_length=1, max_length=100)
    epsilon: float | None = Field(default=None, ge=0.0, le=1.0)
    provider_message_id_prefix: str | None = Field(default=None, alias="providerMessageIdPrefix", max_length=80)
    limit: int = Field(default=1000, ge=1, le=10000)


class MessageEventWebhookRequest(ApiBaseModel):
    delivery_id: int | None = Field(default=None, alias="deliveryId", ge=1)
    provider_message_id: str | None = Field(default=None, alias="providerMessageId", max_length=100)
    provider_event_id: str | None = Field(default=None, alias="providerEventId", max_length=150)
    event_type: str = Field(..., alias="eventType", min_length=1, max_length=30)
    event_at: datetime = Field(default_factory=lambda: datetime.now().astimezone(), alias="eventAt")
    click_url: str | None = Field(default=None, alias="clickUrl")
    conversion_type: str | None = Field(default=None, alias="conversionType", max_length=50)
    conversion_value_krw: float | None = Field(default=None, alias="conversionValueKrw", ge=0)
    device_type: str | None = Field(default=None, alias="deviceType", max_length=30)
    os_name: str | None = Field(default=None, alias="osName", max_length=50)
    browser_name: str | None = Field(default=None, alias="browserName", max_length=50)
    ip_hash: str | None = Field(default=None, alias="ipHash", max_length=128)
    user_agent: str | None = Field(default=None, alias="userAgent")
    event_properties: dict[str, Any] = Field(default_factory=dict, alias="eventProperties")


class CtrAnalyzeRequest(ApiBaseModel):
    experiment_id: int = Field(..., alias="experimentId", ge=1)
    include_segments: bool = Field(default=True, alias="includeSegments")
    include_daily_trend: bool = Field(default=True, alias="includeDailyTrend")
    generate_next_message: bool = Field(default=True, alias="generateNextMessage")


app = FastAPI(
    title="Campaign Target SQL API",
    description="Generate validated targeting SQL from a natural language campaign prompt.",
    version="1.0.0",
    default_response_class=Utf8JSONResponse,
)


@app.middleware("http")
async def log_api_request_timing(request: Request, call_next: Any) -> Any:
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        api_logger.exception(
            "api_request_failed method=%s path=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            _elapsed_ms(started_at),
        )
        raise
    finally:
        api_logger.info(
            "api_request method=%s path=%s status=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            status_code,
            _elapsed_ms(started_at),
        )


@app.on_event("startup")
def load_graph() -> None:
    data_path = Path(os.getenv("GRAPH_RAG_DATA", DEFAULT_DATA_PATH))
    try:
        payload = load_payload(data_path)
        app.state.graph = build_graph(payload)
        app.state.data_path = data_path
    except Exception as exc:
        app.state.graph = None
        app.state.data_path = data_path
        app.state.startup_error = f"{exc.__class__.__name__}: {exc}"


@app.get("/health")
def health() -> dict[str, Any]:
    graph = getattr(app.state, "graph", None)
    return {
        "status": "ok" if graph is not None else "error",
        "data_path": str(getattr(app.state, "data_path", Path(os.getenv("GRAPH_RAG_DATA", DEFAULT_DATA_PATH)))),
        "startup_error": getattr(app.state, "startup_error", None),
        "graph": graph_stats(graph) if graph is not None else None,
    }


@app.post("/target-sql")
def target_sql(request: TargetSqlRequest) -> dict[str, Any]:
    request_started_at = time.perf_counter()
    graph = getattr(app.state, "graph", None)
    if graph is None:
        startup_error = getattr(app.state, "startup_error", "graph_not_loaded")
        _save_query_failure_log(
            {
                "endpoint": "target_sql",
                "prompt": request.prompt,
                "query_parser": request.query_parser,
                "api_status": "service_unavailable",
                "failure_stage": "startup",
                "failure_reason": "graph_not_loaded",
                "error_detail": startup_error,
                "request_options": _target_sql_request_options(request),
            }
        )
        raise HTTPException(status_code=503, detail=startup_error)

    try:
        retrieve_started_at = time.perf_counter()
        with rag_llm_run_scope():
            result = retrieve(
                query=request.prompt,
                graph=graph,
                collection=request.collection,
                url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                api_key=os.getenv("QDRANT_API_KEY"),
                embedding_model_name=os.getenv("QDRANT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
                vector_top_k=request.vector_top_k,
                keyword_top_k=request.keyword_top_k,
                graph_top_k=request.graph_top_k,
                hops=request.hops,
                normalization_rules=Path(os.getenv("GRAPH_RAG_NORMALIZATION_RULES", DEFAULT_NORMALIZATION_PATH)),
                business_policies=Path(os.getenv("GRAPH_RAG_BUSINESS_POLICIES", DEFAULT_POLICY_PATH)),
                metric_lexicon=Path(os.getenv("GRAPH_RAG_METRIC_LEXICON", DEFAULT_METRIC_LEXICON_PATH)),
                sql_schema=Path(os.getenv("GRAPH_RAG_SQL_SCHEMA", DEFAULT_SCHEMA_PATH)),
                sql_limit=request.sql_limit,
                query_parser=request.query_parser,
                llm_model=os.getenv("OPENAI_MODEL", DEFAULT_LLM_MODEL),
                generate_answer=request.generate_answer,
                generate_messages=request.generate_messages,
                message_channel=request.message_channel,
                message_generation_options=_message_generation_options_payload(request.message_generation_options),
                message_policy=Path(os.getenv("GRAPH_RAG_MESSAGE_POLICY", DEFAULT_MESSAGE_POLICY_PATH)),
                prompt_dir=Path(os.getenv("GRAPH_RAG_PROMPT_DIR", DEFAULT_PROMPT_DIR)),
            )
        retrieve_elapsed_ms = _elapsed_ms(retrieve_started_at)
    except ValueError as exc:
        _save_query_failure_log(
            {
                "endpoint": "target_sql",
                "prompt": request.prompt,
                "query_parser": request.query_parser,
                "api_status": "bad_request",
                "failure_stage": "retrieval",
                "failure_reason": "invalid_request",
                "error_detail": str(exc),
                "request_options": _target_sql_request_options(request),
            }
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _save_query_failure_log(
            {
                "endpoint": "target_sql",
                "prompt": request.prompt,
                "query_parser": request.query_parser,
                "api_status": "internal_error",
                "failure_stage": "retrieval",
                "failure_reason": f"target_sql_failed:{exc.__class__.__name__}",
                "error_detail": str(exc),
                "request_options": _target_sql_request_options(request),
            }
        )
        raise HTTPException(status_code=500, detail=f"target_sql_failed:{exc.__class__.__name__}") from exc

    api_response = result["api_response"]
    database_started_at = time.perf_counter()
    database_execution = execute_target_sql(
        api_response.get("sql"),
        request.execute_sql,
        request.result_row_limit,
        persist_targeting=request.persist_targeting,
        audience_ttl_days=request.audience_ttl_days,
        prompt=request.prompt,
        query_parser=request.query_parser,
        request_options=_target_sql_request_options(request),
        query_plan=result.get("query_plan", {}),
    )
    database_elapsed_ms = _elapsed_ms(database_started_at)
    refresh_started_at = time.perf_counter()
    refresh_result = refresh_message_generation_from_database(request, result, database_execution)
    refresh_elapsed_ms = _elapsed_ms(refresh_started_at)
    timings_ms = {
        **result.get("timings_ms", {}),
        "api_retrieve_call": retrieve_elapsed_ms,
        "database_execution": database_elapsed_ms,
        "database_message_refresh": refresh_elapsed_ms,
        **refresh_result.get("timings_ms", {}),
    }
    timings_ms["total_api"] = _elapsed_ms(request_started_at)
    _log_timing_summary(
        "target_sql",
        timings_ms,
        extra={
            "message_generation_attempt_count": result["message_generation"].get("attempt_count", 0),
            "message_generation_failure_reason": result["message_generation"].get("failure_reason"),
            "database_message_refresh": _database_message_refresh_log_summary(refresh_result),
        },
    )
    api_response.update(
        {
            "message_variants": result["message_generation"].get("messages", []),
            "message_generation_mode": result["message_generation"].get("mode"),
            "message_generation_failure_reason": result["message_generation"].get("failure_reason"),
            "message_generation_validation": result["message_generation"].get("validation"),
            "message_generation_attempt_count": result["message_generation"].get("attempt_count", 0),
            "message_generation_max_attempts": result["message_generation"].get("max_attempts", 0),
            "message_generation_options": result["message_generation"].get("options"),
            "database_message_refresh": refresh_result,
            "timings_ms": timings_ms,
            "database_execution": database_execution,
            "audience": database_execution.get("audience", {}),
            "targeting_result": database_execution.get("targeting_result", {}),
            "segment_composition": database_execution.get("segment_composition", {}),
        }
    )
    failure_log = _save_target_sql_failure_log(request, result, api_response, database_execution)
    if failure_log:
        api_response["failure_log"] = failure_log
    if request.include_debug:
        api_response["debug"] = {
            "stage_log": result["stage_log"],
            "context_assembly": result["context_assembly"],
            "vector_matches": result["vector_matches"],
            "keyword_matches": result["keyword_matches"],
            "message_generation": result["message_generation"],
            "timings_ms": timings_ms,
        }

    return api_response


@app.post("/channel-messages")
def channel_messages(request: ChannelMessagesRequest) -> dict[str, Any]:
    target_response = target_sql(
        TargetSqlRequest(
            prompt=request.prompt,
            query_parser=request.query_parser,
            generate_answer=False,
            generate_messages=True,
            message_channel=request.message_channel,
            message_generation_options=request.message_generation_options,
            collection=request.collection,
            vector_top_k=request.vector_top_k,
            keyword_top_k=request.keyword_top_k,
            graph_top_k=request.graph_top_k,
            hops=request.hops,
            sql_limit=request.sql_limit,
            execute_sql=True,
            result_row_limit=request.result_row_limit,
            include_debug=request.include_debug,
        )
    )
    messages = target_response.get("message_variants", [])
    response = {
        "status": "success" if len(messages) == 3 and not target_response.get("message_generation_failure_reason") else "message_generation_failed",
        "query": target_response.get("query"),
        "channel": _response_message_channel(messages, request.message_channel),
        "messages": messages,
        "message_count": len(messages),
        "message_generation_mode": target_response.get("message_generation_mode"),
        "message_generation_failure_reason": target_response.get("message_generation_failure_reason"),
        "message_generation_validation": target_response.get("message_generation_validation"),
        "message_generation_attempt_count": target_response.get("message_generation_attempt_count"),
        "message_generation_max_attempts": target_response.get("message_generation_max_attempts"),
        "message_generation_options": target_response.get("message_generation_options"),
        "timings_ms": target_response.get("timings_ms"),
        "sql": target_response.get("sql"),
        "targeting_result": target_response.get("targeting_result", {}),
        "segment_composition": target_response.get("segment_composition", {}),
    }
    if request.include_debug:
        response["debug"] = target_response.get("debug")
    _log_timing_summary(
        "channel_messages",
        target_response.get("timings_ms", {}),
        extra={
            "status": response["status"],
            "message_count": response["message_count"],
            "attempt_count": response.get("message_generation_attempt_count"),
            "database_message_refresh": _database_message_refresh_log_summary(target_response.get("database_message_refresh", {})),
        },
    )
    return response


@app.get("/target-audiences/{audience_id}")
def target_audience(audience_id: int) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT audience_id, audience_key, prompt, query_parser, request_options, generated_sql,
                           sql_hash, query_plan, status, member_count, target_customer_count,
                           target_campaign_count, failure_reason, created_at, completed_at, expires_at
                    FROM campaign_target_audiences
                    WHERE audience_id = %s
                    """,
                    (audience_id,),
                )
                row = cursor.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"target_audience_lookup_failed:{exc.__class__.__name__}") from exc

    if row is None:
        raise HTTPException(status_code=404, detail="target_audience_not_found")
    return _jsonable_record(row)


@app.get("/target-audiences/{audience_id}/members")
def target_audience_members(
    audience_id: int,
    limit: Annotated[int, Query(ge=1, le=10000)] = 100,
    after_member_id: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM campaign_target_audiences WHERE audience_id = %s", (audience_id,))
                if cursor.fetchone() is None:
                    raise HTTPException(status_code=404, detail="target_audience_not_found")
                member_params: tuple[Any, ...]
                if after_member_id is None:
                    member_sql = """
                    SELECT member_id, user_id, campaign_id, created_at
                    FROM campaign_target_audience_members
                    WHERE audience_id = %s
                    ORDER BY member_id
                    LIMIT %s
                    """
                    member_params = (audience_id, limit)
                else:
                    member_sql = """
                    SELECT member_id, user_id, campaign_id, created_at
                    FROM campaign_target_audience_members
                    WHERE audience_id = %s
                      AND member_id > %s
                    ORDER BY member_id
                    LIMIT %s
                    """
                    member_params = (audience_id, after_member_id, limit)
                cursor.execute(member_sql, member_params)
                rows = [_jsonable_record(row) for row in cursor.fetchall()]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"target_audience_members_lookup_failed:{exc.__class__.__name__}") from exc

    next_after_member_id = rows[-1]["member_id"] if len(rows) == limit else None
    return {
        "audience_id": audience_id,
        "members": rows,
        "limit": limit,
        "next_after_member_id": next_after_member_id,
    }


@app.delete("/target-audiences/{audience_id}")
def delete_target_audience(audience_id: int) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM campaign_target_audiences
                    WHERE audience_id = %s
                    RETURNING audience_id, audience_key, member_count, target_customer_count,
                              target_campaign_count, created_at, expires_at
                    """,
                    (audience_id,),
                )
                row = cursor.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"target_audience_delete_failed:{exc.__class__.__name__}") from exc

    if row is None:
        raise HTTPException(status_code=404, detail="target_audience_not_found")
    return {"is_success": True, "deleted_audience": _jsonable_record(row)}


@app.post("/target-audiences/cleanup")
def cleanup_target_audiences(limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    WITH expired AS (
                        SELECT audience_id
                        FROM campaign_target_audiences
                        WHERE expires_at IS NOT NULL
                          AND expires_at < CURRENT_TIMESTAMP
                        ORDER BY expires_at, audience_id
                        LIMIT %s
                    )
                    DELETE FROM campaign_target_audiences a
                    USING expired e
                    WHERE a.audience_id = e.audience_id
                    RETURNING a.audience_id, a.audience_key, a.member_count, a.expires_at
                    """,
                    (limit,),
                )
                deleted = [_jsonable_record(row) for row in cursor.fetchall()]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"target_audience_cleanup_failed:{exc.__class__.__name__}") from exc

    return {
        "is_success": True,
        "deleted_audience_count": len(deleted),
        "estimated_deleted_member_count": sum(int(row.get("member_count") or 0) for row in deleted),
        "deleted_audiences": deleted,
        "limit": limit,
    }


@app.get("/prompts")
def list_prompt_templates() -> dict[str, Any]:
    import prompt_store

    try:
        templates = prompt_store.list_templates(_postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_templates_lookup_failed:{exc.__class__.__name__}") from exc
    return {"prompts": [_jsonable_record(item) for item in templates], "count": len(templates)}


@app.get("/prompts/{name}")
def get_prompt_template(name: str) -> dict[str, Any]:
    import prompt_store

    try:
        template = prompt_store.get_one(name, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_template_lookup_failed:{exc.__class__.__name__}") from exc
    if template is None:
        raise HTTPException(status_code=404, detail="prompt_template_not_found")
    return _jsonable_record(template)


@app.put("/prompts/{name}")
def upsert_prompt_template(name: str, request: PromptTemplateUpsertRequest) -> dict[str, Any]:
    import prompt_store

    try:
        template = prompt_store.upsert(name, request.content, request.description, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_template_upsert_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "prompt": _jsonable_record(template)}


@app.delete("/prompts/{name}")
def delete_prompt_template(name: str) -> dict[str, Any]:
    import prompt_store

    try:
        deleted = prompt_store.delete(name, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_template_delete_failed:{exc.__class__.__name__}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="prompt_template_not_found")
    return {"is_success": True, "deleted_prompt": name}


@app.post("/prompts/reload")
def reload_prompt_templates() -> dict[str, Any]:
    import prompt_store

    try:
        loaded = prompt_store.reload(_postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_templates_reload_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "loaded": loaded}


@app.post("/prompts/seed")
def seed_prompt_templates() -> dict[str, Any]:
    import prompt_store

    prompt_dir = Path(os.getenv("GRAPH_RAG_PROMPT_DIR", str(DEFAULT_PROMPT_DIR)))
    try:
        seeded = prompt_store.seed_from_dir(prompt_dir, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"prompt_templates_seed_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "seeded": [_jsonable_record(item) for item in seeded], "count": len(seeded)}


@app.get("/policies")
def list_policies() -> dict[str, Any]:
    import policy_store

    try:
        policies = policy_store.list_policies(_postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policies_lookup_failed:{exc.__class__.__name__}") from exc
    return {"policies": [_jsonable_record(item) for item in policies], "count": len(policies)}


@app.get("/policies/{name}")
def get_policy(name: str) -> dict[str, Any]:
    import policy_store

    try:
        policy = policy_store.get_one(name, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policy_lookup_failed:{exc.__class__.__name__}") from exc
    if policy is None:
        raise HTTPException(status_code=404, detail="policy_not_found")
    return _jsonable_record(policy)


@app.put("/policies/{name}")
def upsert_policy(name: str, request: PolicyUpsertRequest) -> dict[str, Any]:
    import policy_store

    try:
        policy = policy_store.upsert(name, request.content, request.description, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policy_upsert_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "policy": _jsonable_record(policy)}


@app.delete("/policies/{name}")
def delete_policy(name: str) -> dict[str, Any]:
    import policy_store

    try:
        deleted = policy_store.delete(name, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policy_delete_failed:{exc.__class__.__name__}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="policy_not_found")
    return {"is_success": True, "deleted_policy": name}


@app.post("/policies/reload")
def reload_policies() -> dict[str, Any]:
    import policy_store

    try:
        loaded = policy_store.reload(_postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policies_reload_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "loaded": loaded}


@app.post("/policies/seed")
def seed_policies() -> dict[str, Any]:
    import policy_store

    policy_dir = Path(os.getenv("CAMPAIGN_POLICY_DIR", str(POLICY_DIR)))
    try:
        seeded = policy_store.seed_from_dir(policy_dir, _postgres_conninfo())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"policies_seed_failed:{exc.__class__.__name__}") from exc
    return {"is_success": True, "seeded": [_jsonable_record(item) for item in seeded], "count": len(seeded)}


@app.post("/api/campaign-experiments/run")
@app.post("/campaign-experiments/run")
def run_campaign_experiment(request: CampaignExperimentRunRequest) -> dict[str, Any]:
    if not request.user_ids and request.audience_id is None:
        raise HTTPException(status_code=400, detail="user_ids_or_audience_id_required")
    variant_codes = [variant.code for variant in request.variants]
    if len(set(variant_codes)) != len(variant_codes):
        raise HTTPException(status_code=400, detail="variant_code_must_be_unique")
    model_version = _ctr_model_version(request.model_version)
    epsilon = _ctr_assignment_epsilon(request.epsilon)

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT experiment_id, campaign_id, experiment_name, channel, status,
                           assignment_method, primary_metric, started_at, ended_at, created_at, updated_at
                    FROM campaign_experiments
                    WHERE campaign_id = %s
                      AND experiment_name = %s
                    """,
                    (request.campaign_id, request.experiment_name),
                )
                existing_experiment = cursor.fetchone()
                experiment_created = existing_experiment is None
                if existing_experiment is None:
                    cursor.execute(
                        """
                        INSERT INTO campaign_experiments (
                            campaign_id, experiment_name, channel, status, assignment_method, primary_metric, started_at
                        )
                        VALUES (%s, %s, %s, 'running', %s, %s, CURRENT_TIMESTAMP)
                        RETURNING experiment_id, campaign_id, experiment_name, channel, status,
                                  assignment_method, primary_metric, started_at, ended_at, created_at, updated_at
                        """,
                        (
                            request.campaign_id,
                            request.experiment_name,
                            request.channel,
                            request.assignment_method,
                            request.primary_metric,
                        ),
                    )
                    experiment = _jsonable_record(cursor.fetchone())
                    inserted_variants = []
                    for variant in request.variants:
                        ai_features = variant.ai_features or _extract_message_ai_features(variant.message_body)
                        cursor.execute(
                            """
                            INSERT INTO campaign_message_variants (
                                experiment_id, variant_code, message_name, message_body, landing_url,
                                allocation_weight, is_control, ai_features
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                            RETURNING variant_id, experiment_id, variant_code, message_name, message_body,
                                      landing_url, allocation_weight, is_control, ai_features, created_at
                            """,
                            (
                                experiment["experiment_id"],
                                variant.code,
                                variant.name,
                                variant.message_body,
                                variant.landing_url,
                                variant.allocation_weight,
                                variant.is_control,
                                json.dumps(ai_features, ensure_ascii=False),
                            ),
                        )
                        inserted_variants.append(_jsonable_record(cursor.fetchone()))
                else:
                    experiment = _jsonable_record(existing_experiment)
                    if experiment.get("channel") != request.channel:
                        raise HTTPException(status_code=409, detail="campaign_experiment_name_already_used_with_different_channel")
                    inserted_variants = _get_experiment_variants(cursor, experiment["experiment_id"])
                    existing_variant_codes = {str(variant["variant_code"]) for variant in inserted_variants}
                    if len(inserted_variants) != 3 or existing_variant_codes != set(variant_codes):
                        raise HTTPException(status_code=409, detail="campaign_experiment_name_already_used_with_different_variants")
                    inserted_variants = _refresh_experiment_variants_from_request(cursor, experiment["experiment_id"], request.variants)

                assignment_request = AssignmentCreateRequest(
                    user_ids=request.user_ids,
                    audience_id=request.audience_id,
                    assignment_method=request.assignment_method,
                    model_version=model_version,
                    epsilon=epsilon,
                    provider_message_id_prefix=request.provider_message_id_prefix,
                    limit=request.limit,
                )
                full_experiment = _get_experiment(cursor, experiment["experiment_id"])
                if full_experiment is None:
                    raise HTTPException(status_code=500, detail="campaign_experiment_reload_failed")
                user_ids = _assignment_user_ids(cursor, assignment_request, full_experiment)
                if not user_ids:
                    raise HTTPException(status_code=400, detail="assignment_user_ids_empty")
                users = _get_users(cursor, user_ids)
                variants = _get_experiment_variants(cursor, experiment["experiment_id"])
                existing_assignments = _existing_assignments_by_user(cursor, experiment["experiment_id"], user_ids)
                existing_assignment_user_ids = set(existing_assignments)
                assignments = []
                reused_assignments = []
                skipped = []
                for user_id in user_ids:
                    user = users.get(user_id)
                    if user is None:
                        skipped.append({"userId": user_id, "reason": "user_not_found"})
                        continue
                    if user_id in existing_assignment_user_ids:
                        reused_assignments.append({**existing_assignments[user_id], "isReused": True})
                        continue
                    decision = _assignment_decision(
                        user=user,
                        variants=variants,
                        experiment=full_experiment,
                        assignment_method=request.assignment_method,
                        model_version=model_version,
                        epsilon=epsilon,
                    )
                    provider_message_id = _provider_message_id(request.provider_message_id_prefix, experiment["experiment_id"], user_id)
                    targeting_snapshot = _targeting_snapshot(user, full_experiment, decision)
                    cursor.execute(
                        """
                        INSERT INTO campaign_message_deliveries (
                            experiment_id, variant_id, campaign_id, user_id, channel, assignment_source,
                            model_version, predicted_click_probability, provider_message_id,
                            targeting_snapshot, final_status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'assigned')
                        RETURNING delivery_id, experiment_id, variant_id, campaign_id, user_id, channel,
                                  assignment_source, model_version, predicted_click_probability,
                                  provider_message_id, assigned_at, final_status, targeting_snapshot
                        """,
                        (
                            experiment["experiment_id"],
                            decision["variant"]["variant_id"],
                            full_experiment["campaign_id"],
                            user_id,
                            full_experiment["channel"],
                            decision["assignment_source"],
                            decision.get("model_version"),
                            decision.get("predicted_click_probability"),
                            provider_message_id,
                            json.dumps(targeting_snapshot, ensure_ascii=False),
                        ),
                    )
                    delivery = _jsonable_record(cursor.fetchone())
                    assignments.append(
                        {
                            **delivery,
                            "variant_code": decision["variant"]["variant_code"],
                            "decision": decision["public_decision"],
                            "isReused": False,
                        }
                    )
                analysis = None
                if request.include_analysis:
                    variants_metrics = _experiment_variant_metrics(cursor, experiment["experiment_id"])
                    analysis = _ctr_analysis_summary(full_experiment, variants_metrics, [], generate_next_message=True)
    except HTTPException:
        raise
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="campaign_experiment_or_assignment_already_exists") from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=400, detail="campaign_channel_audience_or_user_not_found") from exc
    except psycopg.errors.CheckViolation as exc:
        raise HTTPException(status_code=400, detail="campaign_experiment_run_value_out_of_range") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"campaign_experiment_run_failed:{exc.__class__.__name__}") from exc

    return {
        "is_success": True,
        "status": "ready_to_send",
        "experimentId": experiment["experiment_id"],
        "experimentCreated": experiment_created,
        "experiment": experiment,
        "variants": inserted_variants,
        "createdAssignmentCount": len(assignments),
        "reusedAssignmentCount": len(reused_assignments),
        "skippedAssignmentCount": len(skipped),
        "assignments": [*assignments, *reused_assignments],
        "skipped": skipped,
        "analysis": analysis,
    }


@app.post("/api/campaign-experiments")
@app.post("/campaign-experiments")
def create_campaign_experiment(request: CampaignExperimentCreateRequest) -> dict[str, Any]:
    variant_codes = [variant.code for variant in request.variants]
    if len(set(variant_codes)) != len(variant_codes):
        raise HTTPException(status_code=400, detail="variant_code_must_be_unique")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO campaign_experiments (
                        campaign_id, experiment_name, channel, status, assignment_method, primary_metric, started_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'running' THEN CURRENT_TIMESTAMP ELSE NULL END)
                    RETURNING experiment_id, campaign_id, experiment_name, channel, status,
                              assignment_method, primary_metric, started_at, ended_at, created_at, updated_at
                    """,
                    (
                        request.campaign_id,
                        request.experiment_name,
                        request.channel,
                        request.status,
                        request.assignment_method,
                        request.primary_metric,
                        request.status,
                    ),
                )
                experiment = _jsonable_record(cursor.fetchone())
                inserted_variants = []
                for variant in request.variants:
                    ai_features = variant.ai_features or _extract_message_ai_features(variant.message_body)
                    cursor.execute(
                        """
                        INSERT INTO campaign_message_variants (
                            experiment_id, variant_code, message_name, message_body, landing_url,
                            allocation_weight, is_control, ai_features
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        RETURNING variant_id, experiment_id, variant_code, message_name, message_body,
                                  landing_url, allocation_weight, is_control, ai_features, created_at
                        """,
                        (
                            experiment["experiment_id"],
                            variant.code,
                            variant.name,
                            variant.message_body,
                            variant.landing_url,
                            variant.allocation_weight,
                            variant.is_control,
                            json.dumps(ai_features, ensure_ascii=False),
                        ),
                    )
                    inserted_variants.append(_jsonable_record(cursor.fetchone()))
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="campaign_experiment_or_variant_already_exists") from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=400, detail="campaign_or_channel_not_found") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"campaign_experiment_create_failed:{exc.__class__.__name__}") from exc

    return {
        "is_success": True,
        "experiment": experiment,
        "variants": inserted_variants,
    }


@app.post("/api/ai/ctr/score")
@app.post("/ai/ctr/score")
def score_ctr_variants(request: CtrScoreRequest) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                experiment = _get_experiment(cursor, request.experiment_id)
                if experiment is None and request.campaign_id:
                    experiment = _get_campaign_score_context(cursor, request.campaign_id, request.experiment_id, request.channel)
                if experiment is None:
                    raise HTTPException(status_code=404, detail="campaign_experiment_not_found")
                if request.channel:
                    experiment["channel"] = request.channel
                if request.campaign_id and experiment.get("campaign_id") != request.campaign_id:
                    raise HTTPException(status_code=409, detail="campaign_id_does_not_match_experiment")
                variants = _inline_ctr_variants(request.variants, request.experiment_id) if request.variants else _get_experiment_variants(cursor, request.experiment_id)
                if not variants:
                    raise HTTPException(status_code=400, detail="campaign_experiment_has_no_variants")
                user_ids = _ctr_score_user_ids(cursor, request, experiment)
                if not user_ids:
                    raise HTTPException(status_code=400, detail="ctr_score_user_ids_empty")
                users = _get_users(cursor, user_ids)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ctr_score_failed:{exc.__class__.__name__}") from exc

    results = []
    missing_user_ids = []
    model_version = _ctr_model_version(request.model_version)
    for user_id in user_ids:
        user = users.get(user_id)
        if user is None:
            missing_user_ids.append(user_id)
            continue
        scores = _score_variants(user, variants, experiment, model_version)
        score_breakdowns = _score_variant_breakdowns(user, variants, experiment, model_version)
        selected_code, selected_probability = _best_score(scores)
        variant_scores = _ctr_score_variant_summaries(scores, score_breakdowns, variants, selected_code)
        results.append(
            {
                "userId": user_id,
                "selectedVariantCode": selected_code,
                "predictedClickProbability": selected_probability,
                "predictedClickProbabilityDisplay": _ctr_score_probability_display(selected_probability),
                "scores": scores,
                "scoreBreakdowns": score_breakdowns,
                "variantScores": variant_scores,
                "selectedScoreBreakdown": score_breakdowns.get(str(selected_code)) if selected_code is not None else None,
            }
        )

    selected_result = _ctr_score_selected_result(results, variants)
    selected_variant_score = _ctr_score_selected_variant_score(selected_result)
    return {
        "is_success": True,
        "modelVersion": model_version,
        "experimentId": request.experiment_id,
        "campaignId": experiment.get("campaign_id"),
        "scoreMode": "inline_variants" if request.variants else "stored_experiment_variants",
        "userIds": user_ids,
        "selectedVariantCode": selected_result.get("selectedVariantCode") if selected_result else None,
        "predictedClickProbability": selected_result.get("predictedClickProbability") if selected_result else None,
        "predictedClickProbabilityDisplay": _ctr_score_probability_display(selected_result.get("predictedClickProbability") if selected_result else None),
        "scores": selected_result.get("scores") if selected_result else {},
        "scoreBreakdowns": selected_result.get("scoreBreakdowns") if selected_result else {},
        "variantScores": selected_result.get("variantScores") if selected_result else [],
        "bestVariantCode": selected_result.get("selectedVariantCode") if selected_result else None,
        "selectedVariantScore": selected_variant_score,
        "selectedScoreBreakdown": selected_result.get("selectedScoreBreakdown") if selected_result else None,
        "results": results,
        "missingUserIds": missing_user_ids,
    }


@app.post("/api/campaign-experiments/{experiment_id}/assignments")
@app.post("/campaign-experiments/{experiment_id}/assignments")
def create_campaign_assignments(experiment_id: int, request: AssignmentCreateRequest) -> dict[str, Any]:
    if not request.user_ids and request.audience_id is None:
        raise HTTPException(status_code=400, detail="user_ids_or_audience_id_required")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                experiment = _get_experiment(cursor, experiment_id)
                if experiment is None:
                    raise HTTPException(status_code=404, detail="campaign_experiment_not_found")
                variants = _get_experiment_variants(cursor, experiment_id)
                if not variants:
                    raise HTTPException(status_code=400, detail="campaign_experiment_has_no_variants")
                user_ids = _assignment_user_ids(cursor, request, experiment)
                if not user_ids:
                    raise HTTPException(status_code=400, detail="assignment_user_ids_empty")
                users = _get_users(cursor, user_ids)
                existing_assignments = _existing_assignment_user_ids(cursor, experiment_id, user_ids)
                assignments = []
                skipped = []
                assignment_method = request.assignment_method or str(experiment.get("assignment_method") or "random")
                model_version = _ctr_model_version(request.model_version)
                epsilon = _ctr_assignment_epsilon(request.epsilon)
                for user_id in user_ids:
                    user = users.get(user_id)
                    if user is None:
                        skipped.append({"userId": user_id, "reason": "user_not_found"})
                        continue
                    if user_id in existing_assignments:
                        skipped.append({"userId": user_id, "reason": "already_assigned"})
                        continue
                    decision = _assignment_decision(
                        user=user,
                        variants=variants,
                        experiment=experiment,
                        assignment_method=assignment_method,
                        model_version=model_version,
                        epsilon=epsilon,
                    )
                    provider_message_id = _provider_message_id(request.provider_message_id_prefix, experiment_id, user_id)
                    targeting_snapshot = _targeting_snapshot(user, experiment, decision)
                    cursor.execute(
                        """
                        INSERT INTO campaign_message_deliveries (
                            experiment_id, variant_id, campaign_id, user_id, channel, assignment_source,
                            model_version, predicted_click_probability, provider_message_id,
                            targeting_snapshot, final_status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'assigned')
                        RETURNING delivery_id, experiment_id, variant_id, campaign_id, user_id, channel,
                                  assignment_source, model_version, predicted_click_probability,
                                  provider_message_id, assigned_at, final_status, targeting_snapshot
                        """,
                        (
                            experiment_id,
                            decision["variant"]["variant_id"],
                            experiment["campaign_id"],
                            user_id,
                            experiment["channel"],
                            decision["assignment_source"],
                            decision.get("model_version"),
                            decision.get("predicted_click_probability"),
                            provider_message_id,
                            json.dumps(targeting_snapshot, ensure_ascii=False),
                        ),
                    )
                    delivery = _jsonable_record(cursor.fetchone())
                    assignments.append(
                        {
                            **delivery,
                            "variant_code": decision["variant"]["variant_code"],
                            "decision": decision["public_decision"],
                        }
                    )
    except HTTPException:
        raise
    except psycopg.errors.CheckViolation as exc:
        raise HTTPException(status_code=400, detail="assignment_value_out_of_range") from exc
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="assignment_already_exists") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"campaign_assignment_create_failed:{exc.__class__.__name__}") from exc

    return {
        "is_success": True,
        "experimentId": experiment_id,
        "createdAssignmentCount": len(assignments),
        "skippedAssignmentCount": len(skipped),
        "assignments": assignments,
        "skipped": skipped,
    }


@app.post("/api/webhooks/message-events/{provider}")
@app.post("/webhooks/message-events/{provider}")
def collect_message_event(provider: str, request: MessageEventWebhookRequest) -> dict[str, Any]:
    event_type = _normalize_message_event_type(request.event_type)
    if event_type is None:
        raise HTTPException(status_code=400, detail="unsupported_message_event_type")
    if request.delivery_id is None and not request.provider_message_id:
        raise HTTPException(status_code=400, detail="delivery_id_or_provider_message_id_required")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                delivery = _lookup_delivery_for_event(cursor, request.delivery_id, request.provider_message_id)
                if delivery is None:
                    raise HTTPException(status_code=404, detail="message_delivery_not_found")
                event_key = _message_event_key(provider, delivery, event_type, request)
                cursor.execute(
                    """
                    INSERT INTO campaign_message_events (
                        delivery_id, event_type, event_at, event_key, provider_event_id, click_url,
                        conversion_type, conversion_value_krw, device_type, os_name, browser_name,
                        ip_hash, user_agent, event_properties
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (event_key) DO NOTHING
                    RETURNING event_id, delivery_id, event_type, event_at, event_key, received_at
                    """,
                    (
                        delivery["delivery_id"],
                        event_type,
                        request.event_at,
                        event_key,
                        request.provider_event_id,
                        request.click_url,
                        request.conversion_type,
                        request.conversion_value_krw,
                        request.device_type,
                        request.os_name,
                        request.browser_name,
                        request.ip_hash,
                        request.user_agent,
                        json.dumps(request.event_properties, ensure_ascii=False),
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    _update_delivery_from_event(cursor, delivery["delivery_id"], event_type, request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"message_event_collect_failed:{exc.__class__.__name__}") from exc

    if inserted is None:
        return {
            "is_success": True,
            "isDuplicate": True,
            "eventKey": event_key,
            "deliveryId": delivery["delivery_id"],
        }
    return {
        "is_success": True,
        "isDuplicate": False,
        "event": _jsonable_record(inserted),
    }


@app.post("/api/ai/ctr/analyze")
@app.post("/ai/ctr/analyze")
def analyze_ctr_experiment(request: CtrAnalyzeRequest) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"psycopg_import_failed:{exc.__class__.__name__}") from exc

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                experiment = _get_experiment(cursor, request.experiment_id)
                if experiment is None:
                    raise HTTPException(status_code=404, detail="campaign_experiment_not_found")
                variants = _experiment_variant_metrics(cursor, request.experiment_id)
                segments = _experiment_segment_metrics(cursor, request.experiment_id) if request.include_segments else []
                daily_trend = _experiment_daily_metrics(cursor, request.experiment_id) if request.include_daily_trend else []
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ctr_analyze_failed:{exc.__class__.__name__}") from exc

    analysis = _ctr_analysis_summary(experiment, variants, segments, request.generate_next_message)
    return {
        "is_success": True,
        "analysisMode": "deterministic_sql_summary",
        "experiment": experiment,
        "variants": variants,
        "segments": segments,
        "dailyTrend": daily_trend,
        "analysis": analysis,
    }


def _extract_message_ai_features(message_body: str) -> dict[str, Any]:
    text = message_body.strip()
    marketing_text = _message_marketing_text(text)
    discount_match = re.search(r"(\d{1,2})\s*%", marketing_text)
    has_deadline = any(word in marketing_text for word in ["오늘", "마감", "종료", "기간", "곧", "마지막", "지금", "서둘", "소진", "품절", "임박", "놓치지"])
    has_benefit = any(word in marketing_text for word in ["할인", "쿠폰", "혜택", "특가", "포인트", "무료배송", "무료 배송"])
    personalized = any(word in marketing_text for word in ["고객님", "맞춤", "담아둔", "담아두신", "추천", "준비했습니다", "재구매", "다시 확인"])
    if has_deadline:
        tone = "urgent"
    elif personalized:
        tone = "personalized"
    elif has_benefit:
        tone = "benefit"
    else:
        tone = "neutral"
    return {
        "tone": tone,
        "urgency": has_deadline,
        "discount_rate": int(discount_match.group(1)) if discount_match else None,
        "personalized": personalized,
        "cta": _last_sentence(marketing_text),
        "message_length": len(text),
        "message_length_group": _message_length_group(text),
        "emoji_count": sum(1 for character in text if ord(character) > 0xFFFF),
        "has_price": bool(re.search(r"\d[\d,]*\s*원|%", marketing_text)) or bool(discount_match) or has_benefit,
        "has_deadline": has_deadline,
    }


def _message_marketing_text(text: str) -> str:
    cleaned = re.sub(r"\(?\s*무료\s*수신\s*거부\s*\)?", "", text)
    cleaned = re.sub(r"\(?\s*수신\s*거부\s*\)?", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _last_sentence(text: str) -> str | None:
    parts = [part.strip() for part in re.split(r"[.!?。]\s*", text) if part.strip()]
    return parts[-1] if parts else None


def _message_length_group(text: str) -> str:
    length = len(text)
    if length < 45:
        return "short"
    if length <= 90:
        return "medium"
    return "long"


def _get_experiment(cursor: Any, experiment_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
         SELECT x.experiment_id, x.campaign_id, c.name AS campaign_name, c.objective, c.category,
             c.offer, c.budget_krw, c.expected_ctr, c.expected_cvr,
             x.experiment_name, x.channel, x.status, x.assignment_method,
               x.primary_metric, x.started_at, x.ended_at, x.created_at, x.updated_at,
               COALESCE(ARRAY_AGG(DISTINCT cts.target_segment) FILTER (WHERE cts.target_segment IS NOT NULL), '{}') AS target_segments,
               COALESCE(ARRAY_AGG(DISTINCT ck.keyword) FILTER (WHERE ck.keyword IS NOT NULL), '{}') AS keywords
        FROM campaign_experiments x
        JOIN campaigns c ON c.campaign_id = x.campaign_id
        LEFT JOIN campaign_target_segments cts ON cts.campaign_id = x.campaign_id
        LEFT JOIN campaign_keywords ck ON ck.campaign_id = x.campaign_id
        WHERE x.experiment_id = %s
        GROUP BY x.experiment_id, c.campaign_id
        """,
        (experiment_id,),
    )
    row = cursor.fetchone()
    return _jsonable_record(row) if row is not None else None


def _get_campaign_score_context(cursor: Any, campaign_id: str, experiment_id: int, channel: str | None) -> dict[str, Any] | None:
    cursor.execute(
        """
         SELECT c.campaign_id, c.name AS campaign_name, c.objective, c.category,
             c.offer, c.budget_krw, c.expected_ctr, c.expected_cvr,
               COALESCE(ARRAY_AGG(DISTINCT cts.target_segment) FILTER (WHERE cts.target_segment IS NOT NULL), '{}') AS target_segments,
               COALESCE(ARRAY_AGG(DISTINCT ck.keyword) FILTER (WHERE ck.keyword IS NOT NULL), '{}') AS keywords,
               COALESCE(ARRAY_AGG(DISTINCT cc.channel) FILTER (WHERE cc.channel IS NOT NULL), '{}') AS channels
        FROM campaigns c
        LEFT JOIN campaign_target_segments cts ON cts.campaign_id = c.campaign_id
        LEFT JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
        LEFT JOIN campaign_channels cc ON cc.campaign_id = c.campaign_id
        WHERE c.campaign_id = %s
        GROUP BY c.campaign_id
        """,
        (campaign_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    campaign = _jsonable_record(row)
    channels = campaign.pop("channels", []) or []
    return {
        **campaign,
        "experiment_id": experiment_id,
        "experiment_name": f"inline_score:{campaign_id}",
        "channel": channel or (channels[0] if channels else None),
        "status": "draft",
        "assignment_method": "model",
        "primary_metric": "ctr",
        "started_at": None,
        "ended_at": None,
        "created_at": None,
        "updated_at": None,
    }


def _inline_ctr_variants(variants: list[CampaignMessageVariantRequest] | None, experiment_id: int) -> list[dict[str, Any]]:
    if not variants:
        return []
    return [
        {
            "variant_id": f"inline:{experiment_id}:{variant.code}",
            "experiment_id": experiment_id,
            "variant_code": variant.code,
            "message_name": variant.name,
            "message_body": variant.message_body,
            "landing_url": variant.landing_url,
            "allocation_weight": variant.allocation_weight,
            "is_control": variant.is_control,
            "ai_features": _message_ai_features_from_request(variant.message_body, variant.ai_features),
            "created_at": None,
        }
        for variant in variants
    ]


def _message_ai_features_from_request(message_body: str, request_features: dict[str, Any] | None) -> dict[str, Any]:
    extracted_features = _extract_message_ai_features(message_body)
    if not isinstance(request_features, dict):
        return extracted_features
    return {**request_features, **extracted_features}


def _ctr_score_user_ids(cursor: Any, request: CtrScoreRequest, experiment: dict[str, Any]) -> list[str]:
    if request.user_ids:
        return _dedupe_limited_user_ids(request.user_ids, request.limit)
    if request.audience_id is not None:
        assignment_request = AssignmentCreateRequest(audience_id=request.audience_id, limit=request.limit)
        return _assignment_user_ids(cursor, assignment_request, experiment)

    campaign_id = request.campaign_id or experiment.get("campaign_id")
    if request.variants and campaign_id:
        user_ids = _campaign_recommendation_user_ids(cursor, str(campaign_id), request.limit)
        if user_ids:
            return user_ids

    user_ids = _experiment_delivery_user_ids(cursor, request.experiment_id, request.limit)
    if user_ids:
        return user_ids
    if campaign_id:
        return _campaign_recommendation_user_ids(cursor, str(campaign_id), request.limit)
    return []


def _dedupe_limited_user_ids(user_ids: list[str], limit: int) -> list[str]:
    seen = set()
    deduped = []
    for user_id in user_ids:
        normalized_user_id = str(user_id)
        if normalized_user_id in seen:
            continue
        deduped.append(normalized_user_id)
        seen.add(normalized_user_id)
        if len(deduped) >= limit:
            break
    return deduped


def _experiment_delivery_user_ids(cursor: Any, experiment_id: int, limit: int) -> list[str]:
    cursor.execute(
        """
        SELECT DISTINCT user_id
        FROM campaign_message_deliveries
        WHERE experiment_id = %s
        ORDER BY user_id
        LIMIT %s
        """,
        (experiment_id, limit),
    )
    return [str(row["user_id"]) for row in cursor.fetchall()]


def _campaign_recommendation_user_ids(cursor: Any, campaign_id: str, limit: int) -> list[str]:
    cursor.execute(
        """
        SELECT DISTINCT user_id
        FROM recommendation_edges
        WHERE campaign_id = %s
        ORDER BY user_id
        LIMIT %s
        """,
        (campaign_id, limit),
    )
    return [str(row["user_id"]) for row in cursor.fetchall()]


def _ctr_score_selected_result(results: list[dict[str, Any]], variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None
    if len(results) == 1:
        return results[0]

    score_totals: dict[str, float] = {}
    score_counts: dict[str, int] = {}
    for result in results:
        for code, score in (result.get("scores") or {}).items():
            score_totals[code] = score_totals.get(code, 0.0) + float(score)
            score_counts[code] = score_counts.get(code, 0) + 1
    average_scores = {
        code: round(total / score_counts[code], 7)
        for code, total in score_totals.items()
        if score_counts.get(code)
    }
    selected_code, selected_probability = _best_score(average_scores)
    selected_breakdown = None
    score_breakdowns = {}
    for result in results:
        score_breakdowns = result.get("scoreBreakdowns") or {}
        selected_breakdown = score_breakdowns.get(str(selected_code)) if selected_code is not None else None
        if selected_breakdown is not None:
            break
    score_breakdowns = _ctr_score_aligned_breakdowns(score_breakdowns, average_scores)
    selected_breakdown = score_breakdowns.get(str(selected_code)) if selected_code is not None else None
    return {
        "selectedVariantCode": selected_code,
        "predictedClickProbability": selected_probability,
        "predictedClickProbabilityDisplay": _ctr_score_probability_display(selected_probability),
        "scores": average_scores,
        "scoreBreakdowns": score_breakdowns,
        "variantScores": _ctr_score_variant_summaries(average_scores, score_breakdowns, variants, selected_code),
        "selectedScoreBreakdown": selected_breakdown,
    }


def _ctr_score_variant_summaries(
    scores: dict[str, float],
    score_breakdowns: dict[str, dict[str, Any]],
    variants: list[dict[str, Any]],
    selected_code: str | None,
) -> list[dict[str, Any]]:
    summaries = []
    ranked_codes = [
        code
        for code, _score in sorted(scores.items(), key=lambda item: float(item[1]), reverse=True)
    ]
    selected_probability = scores.get(str(selected_code)) if selected_code is not None else None
    best_probability = scores.get(ranked_codes[0]) if ranked_codes else None
    for variant in variants:
        code = str(variant.get("variant_code"))
        probability = scores.get(code)
        predicted_ctr = _ctr_score_value("예측 CTR", probability) if probability is not None else None
        breakdown = _ctr_score_aligned_breakdown(score_breakdowns.get(code), probability)
        summaries.append(
            {
                "variantCode": code,
                "code": code,
                "rank": ranked_codes.index(code) + 1 if code in ranked_codes else None,
                "name": variant.get("message_name"),
                "messageBody": variant.get("message_body"),
                "isControl": bool(variant.get("is_control")),
                "predictedClickProbability": probability,
                "predictedClickProbabilityDisplay": _ctr_score_probability_display(probability),
                "predictedCtr": predicted_ctr,
                "deltaVsSelected": _ctr_score_delta("선택 시안 대비", probability, selected_probability),
                "deltaVsBest": _ctr_score_delta("최고 시안 대비", probability, best_probability),
                "scoreBreakdown": breakdown,
                "scoreSummary": breakdown.get("summary") if breakdown else None,
                "display": breakdown.get("display") if breakdown else None,
                "isSelected": code == str(selected_code) if selected_code is not None else False,
            }
        )
    return sorted(
        summaries,
        key=lambda summary: (
            summary.get("rank") is None,
            int(summary.get("rank") or 999999),
            str(summary.get("variantCode") or ""),
        ),
    )


def _ctr_score_selected_variant_score(selected_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if selected_result is None:
        return None
    selected_code = selected_result.get("selectedVariantCode")
    if selected_code is None:
        return None
    for variant_score in selected_result.get("variantScores") or []:
        if str(variant_score.get("variantCode")) == str(selected_code):
            return variant_score
    return None


def _ctr_score_aligned_breakdowns(score_breakdowns: dict[str, dict[str, Any]], scores: dict[str, float]) -> dict[str, dict[str, Any]]:
    return {
        str(code): _ctr_score_aligned_breakdown(breakdown, scores.get(str(code)))
        for code, breakdown in score_breakdowns.items()
    }


def _ctr_score_aligned_breakdown(breakdown: dict[str, Any] | None, probability: float | None) -> dict[str, Any] | None:
    if breakdown is None or probability is None:
        return breakdown
    predicted_ctr = _ctr_score_value("예측 CTR", probability)
    aligned = {**breakdown, "predictedCtr": predicted_ctr}
    summary = aligned.get("summary")
    if isinstance(summary, dict):
        aligned["summary"] = {**summary, "predictedCtr": predicted_ctr}
    display = aligned.get("display")
    if isinstance(display, dict):
        aligned_display = {**display, "predictedCtr": predicted_ctr}
        if isinstance(aligned.get("summary"), dict):
            aligned_display["summary"] = aligned["summary"]
        aligned["display"] = aligned_display
    return aligned


def _get_experiment_variants(cursor: Any, experiment_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT variant_id, experiment_id, variant_code, message_name, message_body,
               landing_url, allocation_weight, is_control, ai_features, created_at
        FROM campaign_message_variants
        WHERE experiment_id = %s
        ORDER BY variant_code
        """,
        (experiment_id,),
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _refresh_experiment_variants_from_request(cursor: Any, experiment_id: int, variants: list[CampaignMessageVariantRequest]) -> list[dict[str, Any]]:
    refreshed = []
    for variant in variants:
        ai_features = variant.ai_features or _extract_message_ai_features(variant.message_body)
        cursor.execute(
            """
            UPDATE campaign_message_variants
            SET message_name = %s,
                message_body = %s,
                landing_url = %s,
                allocation_weight = %s,
                is_control = %s,
                ai_features = %s::jsonb
            WHERE experiment_id = %s
              AND variant_code = %s
            RETURNING variant_id, experiment_id, variant_code, message_name, message_body,
                      landing_url, allocation_weight, is_control, ai_features, created_at
            """,
            (
                variant.name,
                variant.message_body,
                variant.landing_url,
                variant.allocation_weight,
                variant.is_control,
                json.dumps(ai_features, ensure_ascii=False),
                experiment_id,
                variant.code,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            refreshed.append(_jsonable_record(row))
    return refreshed


def _get_users(cursor: Any, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    cursor.execute(
        """
        SELECT u.user_id, u.age, u.gender, u.region, u.lifecycle, u.avg_order_value_krw,
               u.purchase_count_90d, u.last_active_days, u.price_sensitivity,
               u.predicted_ltv_segment,
               COALESCE(ARRAY_AGG(DISTINCT ui.interest) FILTER (WHERE ui.interest IS NOT NULL), '{}') AS interests,
               COALESCE(ARRAY_AGG(DISTINCT upc.preferred_channel) FILTER (WHERE upc.preferred_channel IS NOT NULL), '{}') AS preferred_channels,
             COALESCE(ARRAY_AGG(DISTINCT urb.behavior) FILTER (WHERE urb.behavior IS NOT NULL), '{}') AS recent_behaviors,
             COALESCE(ARRAY_AGG(DISTINCT re.campaign_id) FILTER (WHERE re.campaign_id IS NOT NULL), '{}') AS recommendation_campaign_ids
        FROM users u
        LEFT JOIN user_interests ui ON ui.user_id = u.user_id
        LEFT JOIN user_preferred_channels upc ON upc.user_id = u.user_id
        LEFT JOIN user_recent_behaviors urb ON urb.user_id = u.user_id
         LEFT JOIN recommendation_edges re ON re.user_id = u.user_id
        WHERE u.user_id = ANY(%s)
        GROUP BY u.user_id
        """,
        (user_ids,),
    )
    return {row["user_id"]: _jsonable_record(row) for row in cursor.fetchall()}


def _assignment_user_ids(cursor: Any, request: AssignmentCreateRequest, experiment: dict[str, Any]) -> list[str]:
    ordered_user_ids: list[str] = []
    if request.user_ids:
        ordered_user_ids.extend(request.user_ids)
    if request.audience_id is not None:
        cursor.execute(
            """
            SELECT DISTINCT user_id
            FROM campaign_target_audience_members
            WHERE audience_id = %s
              AND (campaign_id IS NULL OR campaign_id = %s)
            ORDER BY user_id
            LIMIT %s
            """,
            (request.audience_id, experiment["campaign_id"], request.limit),
        )
        ordered_user_ids.extend(str(row["user_id"]) for row in cursor.fetchall())
    seen = set()
    deduped = []
    for user_id in ordered_user_ids:
        if user_id not in seen:
            deduped.append(user_id)
            seen.add(user_id)
        if len(deduped) >= request.limit:
            break
    return deduped


def _existing_assignment_user_ids(cursor: Any, experiment_id: int, user_ids: list[str]) -> set[str]:
    if not user_ids:
        return set()
    cursor.execute(
        """
        SELECT user_id
        FROM campaign_message_deliveries
        WHERE experiment_id = %s
          AND user_id = ANY(%s)
        """,
        (experiment_id, user_ids),
    )
    return {str(row["user_id"]) for row in cursor.fetchall()}


def _existing_assignments_by_user(cursor: Any, experiment_id: int, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    cursor.execute(
        """
        SELECT d.delivery_id, d.experiment_id, d.variant_id, d.campaign_id, d.user_id, d.channel,
               d.assignment_source, d.model_version, d.predicted_click_probability,
               d.provider_message_id, d.assigned_at, d.final_status, d.targeting_snapshot,
               v.variant_code
        FROM campaign_message_deliveries d
        JOIN campaign_message_variants v ON v.variant_id = d.variant_id
        WHERE d.experiment_id = %s
          AND d.user_id = ANY(%s)
        ORDER BY d.user_id
        """,
        (experiment_id, user_ids),
    )
    assignments = {}
    for row in cursor.fetchall():
        assignment = _jsonable_record(row)
        snapshot = assignment.get("targeting_snapshot") if isinstance(assignment.get("targeting_snapshot"), dict) else {}
        assignment["decision"] = {
            "decisionPolicy": snapshot.get("decision_policy"),
            "epsilon": snapshot.get("epsilon"),
            "selectedBy": snapshot.get("selected_by"),
            "candidateScores": snapshot.get("candidate_scores") or {},
            "candidateScoreBreakdowns": snapshot.get("candidate_score_breakdowns") or {},
        }
        selected_breakdown = assignment["decision"]["candidateScoreBreakdowns"].get(str(assignment.get("variant_code")))
        if selected_breakdown is not None:
            assignment["decision"]["selectedScoreBreakdown"] = selected_breakdown
        assignments[str(assignment["user_id"])] = assignment
    return assignments


def _score_variants(
    user: dict[str, Any],
    variants: list[dict[str, Any]],
    experiment: dict[str, Any],
    model_version: str,
) -> dict[str, float]:
    model_policy = _load_ctr_model_policy()
    if not _is_heuristic_model_version(model_version):
        try:
            from training.ctr_predictor import predict_variant_scores

            return predict_variant_scores(user, variants, experiment, model_version)
        except Exception as exc:
            if not _ctr_model_policy_bool(model_policy, "fallback_to_heuristic_on_ml_error", True):
                raise HTTPException(status_code=500, detail=f"ctr_model_score_failed:{exc.__class__.__name__}") from exc
            api_logger.warning(
                "ctr_model_score_fallback model_version=%s reason=%s:%s",
                model_version,
                exc.__class__.__name__,
                exc,
            )
    return {
        str(variant["variant_code"]): _score_variant(user, variant, experiment, model_version)
        for variant in variants
    }


def _is_heuristic_model_version(model_version: str) -> bool:
    prefixes = _ctr_model_policy_string_list(_load_ctr_model_policy(), "heuristic_model_version_prefixes", DEFAULT_CTR_MODEL_POLICY["heuristic_model_version_prefixes"])
    return any(model_version.startswith(prefix) for prefix in prefixes)


def _ctr_model_version(request_model_version: str | None) -> str:
    if request_model_version:
        return request_model_version
    return _ctr_model_policy_string(_load_ctr_model_policy(), "default_model_version", DEFAULT_CTR_MODEL_POLICY["default_model_version"])


def _ctr_assignment_epsilon(request_epsilon: float | None) -> float:
    policy = _load_ctr_model_policy()
    if not _ctr_model_policy_bool(policy, "exploration_enabled", False):
        return 0.0
    if request_epsilon is not None and _ctr_model_policy_bool(policy, "allow_request_epsilon_override", False):
        return _clamp_probability(request_epsilon)
    return _clamp_probability(_ctr_model_policy_number(policy, "default_epsilon", DEFAULT_CTR_MODEL_POLICY["default_epsilon"]))


def _load_ctr_model_policy() -> dict[str, Any]:
    """CTR 모델 정책을 DB -> 파일 -> 하드코딩 기본값 순으로 로드한다.

    DB(campaign_policies)에 정책이 있으면 최우선으로 사용하고, DB 미사용/실패/미존재
    시 기존 파일 기반 로직으로 자연스럽게 fallback한다. 어느 경우든 누락된 키는
    ``DEFAULT_CTR_MODEL_POLICY`` 값으로 채운다.
    """
    db_policy = _load_ctr_model_policy_from_db()
    if db_policy is not None:
        return {**DEFAULT_CTR_MODEL_POLICY, **db_policy}
    return _load_ctr_model_policy_from_file()


def _load_ctr_model_policy_from_db() -> dict[str, Any] | None:
    try:
        import policy_store

        policy = policy_store.get_policy(CTR_MODEL_POLICY_NAME, _postgres_conninfo())
    except Exception as exc:  # noqa: BLE001 - 파일 fallback 유지가 목적
        api_logger.warning(
            "ctr_model_policy_db_load_failed reason=%s:%s",
            exc.__class__.__name__,
            exc,
        )
        return None
    if isinstance(policy, dict):
        return policy
    return None


def _load_ctr_model_policy_from_file() -> dict[str, Any]:
    policy_path = _ctr_model_policy_path()
    try:
        with policy_path.open("r", encoding="utf-8") as file:
            configured_policy = json.load(file)
        if not isinstance(configured_policy, dict):
            raise ValueError("ctr_model_policy_must_be_object")
        return {**DEFAULT_CTR_MODEL_POLICY, **configured_policy}
    except Exception as exc:
        api_logger.warning(
            "ctr_model_policy_load_failed path=%s reason=%s:%s",
            policy_path,
            exc.__class__.__name__,
            exc,
        )
        return DEFAULT_CTR_MODEL_POLICY


def _ctr_model_policy_path() -> Path:
    configured_path = os.getenv("CTR_MODEL_POLICY_PATH")
    if not configured_path:
        return DEFAULT_CTR_MODEL_POLICY_PATH
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def _ctr_model_policy_string(values: dict[str, Any], key: str, default: Any) -> str:
    value = values.get(key, default)
    if value is None:
        return str(default)
    normalized = str(value).strip()
    return normalized or str(default)


def _ctr_model_policy_string_list(values: dict[str, Any], key: str, default: Any) -> list[str]:
    configured_values = values.get(key, default)
    if not isinstance(configured_values, list):
        configured_values = default
    return [str(value) for value in configured_values if str(value)]


def _ctr_model_policy_bool(values: dict[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _ctr_model_policy_number(values: dict[str, Any], key: str, default: Any) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _clamp_probability(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _score_variant(user: dict[str, Any], variant: dict[str, Any], experiment: dict[str, Any], model_version: str) -> float:
    return float(_heuristic_variant_score_breakdown(user, variant, experiment, model_version)["predictedCtr"]["probability"])


def _score_variant_breakdowns(
    user: dict[str, Any],
    variants: list[dict[str, Any]],
    experiment: dict[str, Any],
    model_version: str,
) -> dict[str, dict[str, Any]]:
    if not _is_heuristic_model_version(model_version):
        return {}
    return {
        str(variant["variant_code"]): _heuristic_variant_score_breakdown(user, variant, experiment, model_version)
        for variant in variants
    }


def _heuristic_variant_score_breakdown(user: dict[str, Any], variant: dict[str, Any], experiment: dict[str, Any], model_version: str) -> dict[str, Any]:
    rules = _load_heuristic_ctr_rules()
    score_adjustments = rules.get("score_adjustments") if isinstance(rules.get("score_adjustments"), dict) else {}
    matchers = rules.get("matchers") if isinstance(rules.get("matchers"), dict) else {}
    configured_features = variant.get("ai_features") if isinstance(variant.get("ai_features"), dict) else {}
    message_body = str(variant.get("message_body") or "")
    extracted_features = _extract_message_ai_features(message_body) if message_body else {}
    features = {**configured_features, **extracted_features}
    interests = set(user.get("interests") or [])
    preferred_channels = set(user.get("preferred_channels") or [])
    recent_behaviors = set(user.get("recent_behaviors") or [])
    probability = _heuristic_number(rules, "base_probability", DEFAULT_HEURISTIC_CTR_RULES["base_probability"])
    base_probability = probability
    adjustments: list[dict[str, Any]] = []
    calibration_adjustments: list[dict[str, Any]] = []
    rule_evaluations: list[dict[str, Any]] = []

    def evaluate_rule(key: str, label: str, default_value: float, applied: bool, reason: str, condition: str, evidence: dict[str, Any]) -> None:
        nonlocal probability
        amount = _heuristic_number(score_adjustments, key, default_value)
        rule_evaluations.append(_ctr_score_rule_evaluation(key, label, amount, applied, reason, condition, evidence))
        if applied:
            probability += amount
            adjustments.append(_ctr_score_adjustment(key, label, amount, reason))

    channel = experiment.get("channel")
    evaluate_rule(
        "preferred_channel",
        "선호 채널",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["preferred_channel"],
        channel in preferred_channels,
        f"사용자 선호 채널에 {channel}이 포함됩니다." if channel in preferred_channels else f"사용자 선호 채널에 {channel}이 없습니다.",
        "campaign.channel in user.preferred_channels",
        {
            "campaignChannel": channel,
            "preferredChannels": sorted(preferred_channels),
        },
    )
    category = experiment.get("category")
    evaluate_rule(
        "campaign_category_interest_match",
        "관심사 일치",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["campaign_category_interest_match"],
        category in interests,
        f"캠페인 카테고리 {category}가 사용자 관심사와 일치합니다." if category in interests else f"캠페인 카테고리 {category}가 사용자 관심사에 없습니다.",
        "campaign.category in user.interests",
        {
            "campaignCategory": category,
            "interests": sorted(interests),
        },
    )
    has_price_offer = bool(features.get("discount_rate") or features.get("has_price"))
    price_sensitive_offer_match = user.get("price_sensitivity") == "high" and has_price_offer
    evaluate_rule(
        "high_price_sensitivity_with_price_offer",
        "가격 민감도",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["high_price_sensitivity_with_price_offer"],
        price_sensitive_offer_match,
        "가격 민감도가 높고 메시지에 가격/할인 요소가 있습니다." if price_sensitive_offer_match else "가격 민감도 또는 가격/할인 메시지 조건이 충족되지 않았습니다.",
        "user.price_sensitivity == high and message has price or discount",
        {
            "priceSensitivity": user.get("price_sensitivity"),
            "discountRate": features.get("discount_rate"),
            "hasPrice": bool(features.get("has_price")),
        },
    )
    urgency_keywords = _heuristic_string_set(matchers, "urgency_recent_behavior_keywords", DEFAULT_HEURISTIC_CTR_RULES["matchers"]["urgency_recent_behavior_keywords"])
    matched_recent_behaviors = sorted(
        str(behavior)
        for behavior in recent_behaviors
        if any(keyword in str(behavior) for keyword in urgency_keywords)
    )
    urgency_match = bool(features.get("urgency") and matched_recent_behaviors)
    evaluate_rule(
        "urgency_with_recent_behavior",
        "최근 행동",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["urgency_with_recent_behavior"],
        urgency_match,
        "긴급성 메시지가 최근 행동 키워드와 맞습니다." if urgency_match else "긴급성 메시지 또는 최근 행동 키워드 조건이 충족되지 않았습니다.",
        "message.urgency and recent_behaviors match urgency keywords",
        {
            "messageUrgency": bool(features.get("urgency")),
            "urgencyKeywords": sorted(urgency_keywords),
            "matchedRecentBehaviors": matched_recent_behaviors,
            "recentBehaviors": sorted(str(behavior) for behavior in recent_behaviors),
        },
    )
    personalized_lifecycles = _heuristic_string_set(matchers, "personalized_lifecycles", DEFAULT_HEURISTIC_CTR_RULES["matchers"]["personalized_lifecycles"])
    lifecycle = str(user.get("lifecycle"))
    personalized_match = bool(features.get("personalized") and lifecycle in personalized_lifecycles)
    evaluate_rule(
        "personalized_lifecycle_match",
        "라이프사이클",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["personalized_lifecycle_match"],
        personalized_match,
        f"개인화 메시지가 {user.get('lifecycle')} 라이프사이클과 맞습니다." if personalized_match else "개인화 메시지 또는 라이프사이클 조건이 충족되지 않았습니다.",
        "message.personalized and user.lifecycle in personalized_lifecycles",
        {
            "messagePersonalized": bool(features.get("personalized")),
            "userLifecycle": user.get("lifecycle"),
            "personalizedLifecycles": sorted(personalized_lifecycles),
        },
    )
    message_length_group = features.get("message_length_group")
    evaluate_rule(
        "message_length_medium",
        "메시지 길이 medium",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["message_length_medium"],
        message_length_group == "medium",
        "메시지 길이가 medium 그룹입니다." if message_length_group == "medium" else f"메시지 길이 그룹이 {message_length_group}입니다.",
        "message.message_length_group == medium",
        {
            "messageLength": features.get("message_length"),
            "messageLengthGroup": message_length_group,
        },
    )
    evaluate_rule(
        "message_length_long",
        "메시지 길이 long",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["message_length_long"],
        message_length_group == "long",
        "메시지 길이가 long 그룹입니다." if message_length_group == "long" else f"메시지 길이 그룹이 {message_length_group}입니다.",
        "message.message_length_group == long",
        {
            "messageLength": features.get("message_length"),
            "messageLengthGroup": message_length_group,
        },
    )
    evaluate_rule(
        "control_variant",
        "대조군",
        DEFAULT_HEURISTIC_CTR_RULES["score_adjustments"]["control_variant"],
        bool(variant.get("is_control")),
        "컨트롤 variant 안정성 보정입니다." if variant.get("is_control") else "컨트롤 variant가 아닙니다.",
        "variant.is_control == true",
        {
            "isControl": bool(variant.get("is_control")),
        },
    )

    stable_noise = _stable_unit_interval(user.get("user_id"), variant.get("variant_id"), model_version) * _heuristic_number(rules, "stable_noise_max", DEFAULT_HEURISTIC_CTR_RULES["stable_noise_max"])
    probability += stable_noise
    if stable_noise:
        calibration_adjustments.append(_ctr_score_adjustment("stable_noise", "안정화 보정", stable_noise, "동점 방지를 위한 결정적 보정값입니다."))

    raw_probability = probability
    min_probability = _heuristic_number(rules, "min_probability", DEFAULT_HEURISTIC_CTR_RULES["min_probability"])
    max_probability = _heuristic_number(rules, "max_probability", DEFAULT_HEURISTIC_CTR_RULES["max_probability"])
    final_probability = round(min(max(probability, min_probability), max_probability), 7)
    base_score = _ctr_score_value("Base Score", base_probability)
    predicted_ctr = _ctr_score_value("예측 CTR", final_probability)
    applied_adjustment_total = sum(float(adjustment["probabilityDelta"]) for adjustment in adjustments)
    calibration_adjustment_total = sum(float(adjustment["probabilityDelta"]) for adjustment in calibration_adjustments)
    summary = {
        "appliedRuleCount": len(adjustments),
        "notAppliedRuleCount": len([rule for rule in rule_evaluations if not rule.get("applied")]),
        "appliedAdjustmentTotal": _ctr_score_delta_value("규칙 가산/감산 합계", applied_adjustment_total),
        "calibrationAdjustmentTotal": _ctr_score_delta_value("보정 합계", calibration_adjustment_total),
        "totalDeltaFromBase": _ctr_score_delta_value("Base 대비 총 변화", final_probability - base_probability),
        "rawBeforeClamp": _ctr_score_value("클램프 전 CTR", raw_probability),
        "predictedCtr": predicted_ctr,
    }
    formula = {
        "expression": "baseProbability + appliedAdjustments + calibrationAdjustments -> clamp(minProbability, maxProbability)",
        "baseProbability": round(base_probability, 7),
        "appliedAdjustmentProbability": round(applied_adjustment_total, 7),
        "calibrationAdjustmentProbability": round(calibration_adjustment_total, 7),
        "rawProbability": round(raw_probability, 7),
        "minProbability": min_probability,
        "maxProbability": max_probability,
        "finalProbability": final_probability,
    }
    input_signals = _ctr_score_input_signals(user, variant, experiment, features)
    explanation_bullets = _ctr_score_explanation_bullets(base_score, adjustments, calibration_adjustments, predicted_ctr, final_probability != round(raw_probability, 7))
    return {
        "modelVersion": model_version,
        "variantCode": str(variant.get("variant_code")),
        "baseScore": base_score,
        "adjustments": adjustments,
        "calibrationAdjustments": calibration_adjustments,
        "ruleEvaluations": rule_evaluations,
        "inputSignals": input_signals,
        "formula": formula,
        "summary": summary,
        "explanationBullets": explanation_bullets,
        "rawProbability": round(raw_probability, 7),
        "rawPercentage": round(raw_probability * 100, 2),
        "minProbability": min_probability,
        "maxProbability": max_probability,
        "isClamped": final_probability != round(raw_probability, 7),
        "predictedCtr": predicted_ctr,
        "display": {
            "title": "클릭률 분석 근거",
            "baseScore": base_score,
            "adjustments": adjustments,
            "calibrationAdjustments": calibration_adjustments,
            "summary": summary,
            "formula": formula,
            "ruleEvaluations": rule_evaluations,
            "inputSignals": input_signals,
            "explanationBullets": explanation_bullets,
            "predictedCtr": predicted_ctr,
        },
    }


def _ctr_score_value(label: str, probability: float) -> dict[str, Any]:
    return {
        "label": label,
        "probability": round(float(probability), 7),
        "percentage": round(float(probability) * 100, 2),
        "displayValue": _format_probability_percent(probability),
    }


def _ctr_score_probability_display(probability: float | None) -> str | None:
    if probability is None:
        return None
    return _format_probability_percent(probability)


def _ctr_score_delta_value(label: str, amount: float) -> dict[str, Any]:
    return {
        "label": label,
        "probabilityDelta": round(float(amount), 7),
        "percentagePointDelta": round(float(amount) * 100, 2),
        "displayValue": _format_probability_percent(amount, signed=True),
    }


def _ctr_score_delta(label: str, value: float | None, baseline: float | None) -> dict[str, Any] | None:
    if value is None or baseline is None:
        return None
    return _ctr_score_delta_value(label, float(value) - float(baseline))


def _ctr_score_rule_evaluation(
    key: str,
    label: str,
    amount: float,
    applied: bool,
    reason: str,
    condition: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "applied": applied,
        "condition": condition,
        "reason": reason,
        "evidence": evidence,
        "configuredDelta": _ctr_score_delta_value("설정 가중치", amount),
        "appliedDelta": _ctr_score_delta_value("적용 가중치", amount if applied else 0.0),
    }


def _ctr_score_input_signals(user: dict[str, Any], variant: dict[str, Any], experiment: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": {
            "userId": user.get("user_id"),
            "lifecycle": user.get("lifecycle"),
            "priceSensitivity": user.get("price_sensitivity"),
            "interests": sorted(str(value) for value in (user.get("interests") or [])),
            "preferredChannels": sorted(str(value) for value in (user.get("preferred_channels") or [])),
            "recentBehaviors": sorted(str(value) for value in (user.get("recent_behaviors") or [])),
            "purchaseCount90d": user.get("purchase_count_90d"),
            "lastActiveDays": user.get("last_active_days"),
            "predictedLtvSegment": user.get("predicted_ltv_segment"),
        },
        "campaign": {
            "campaignId": experiment.get("campaign_id"),
            "campaignName": experiment.get("campaign_name"),
            "objective": experiment.get("objective"),
            "category": experiment.get("category"),
            "channel": experiment.get("channel"),
            "offer": experiment.get("offer"),
            "targetSegments": sorted(str(value) for value in (experiment.get("target_segments") or [])),
            "keywords": sorted(str(value) for value in (experiment.get("keywords") or [])),
        },
        "variant": {
            "variantCode": str(variant.get("variant_code")),
            "name": variant.get("message_name"),
            "isControl": bool(variant.get("is_control")),
            "features": features,
        },
    }


def _ctr_score_explanation_bullets(
    base_score: dict[str, Any],
    adjustments: list[dict[str, Any]],
    calibration_adjustments: list[dict[str, Any]],
    predicted_ctr: dict[str, Any],
    is_clamped: bool,
) -> list[str]:
    bullets = [f"기본 점수 {base_score['displayValue']}에서 시작합니다."]
    if adjustments:
        adjustment_total = sum(float(adjustment["probabilityDelta"]) for adjustment in adjustments)
        applied_labels = ", ".join(str(adjustment["label"]) for adjustment in adjustments)
        bullets.append(f"적용 규칙 {len(adjustments)}개({applied_labels})로 {_format_probability_percent(adjustment_total, signed=True)}가 반영됐습니다.")
    else:
        bullets.append("적용된 가산/감산 규칙이 없어 기본 점수만 사용했습니다.")
    if calibration_adjustments:
        calibration_total = sum(float(adjustment["probabilityDelta"]) for adjustment in calibration_adjustments)
        bullets.append(f"결정적 보정값 {_format_probability_percent(calibration_total, signed=True)}를 더해 동점 가능성을 낮췄습니다.")
    if is_clamped:
        bullets.append("정책상 최소/최대 CTR 범위로 최종 값을 보정했습니다.")
    bullets.append(f"최종 예측 CTR은 {predicted_ctr['displayValue']}입니다.")
    return bullets


def _ctr_score_adjustment(key: str, label: str, amount: float, reason: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "probabilityDelta": round(float(amount), 7),
        "percentagePointDelta": round(float(amount) * 100, 2),
        "displayValue": _format_probability_percent(amount, signed=True),
        "reason": reason,
    }


def _format_probability_percent(value: float, signed: bool = False) -> str:
    percentage = float(value) * 100
    sign = "+" if signed and percentage >= 0 else ""
    return f"{sign}{percentage:.2f}%"


def _load_heuristic_ctr_rules() -> dict[str, Any]:
    """휴리스틱 CTR 룰을 DB -> 파일 -> 하드코딩 기본값 순으로 로드한다.

    DB(campaign_policies)에 룰이 있으면 최우선으로 사용하고, DB 미사용/실패/미존재
    시 기존 파일 기반 로직으로 자연스럽게 fallback한다. 어느 경우든 누락 키는
    ``DEFAULT_HEURISTIC_CTR_RULES``와 (한 단계 중첩까지) 병합해 보정한다.
    """
    db_rules = _load_heuristic_ctr_rules_from_db()
    if db_rules is not None:
        return _merge_heuristic_ctr_rules(DEFAULT_HEURISTIC_CTR_RULES, db_rules)
    return _load_heuristic_ctr_rules_from_file()


def _load_heuristic_ctr_rules_from_db() -> dict[str, Any] | None:
    try:
        import policy_store

        rules = policy_store.get_policy(HEURISTIC_CTR_RULES_NAME, _postgres_conninfo())
    except Exception as exc:  # noqa: BLE001 - 파일 fallback 유지가 목적
        api_logger.warning(
            "heuristic_ctr_rules_db_load_failed reason=%s:%s",
            exc.__class__.__name__,
            exc,
        )
        return None
    if isinstance(rules, dict):
        return rules
    return None


def _load_heuristic_ctr_rules_from_file() -> dict[str, Any]:
    rules_path = _heuristic_ctr_rules_path()
    try:
        with rules_path.open("r", encoding="utf-8") as file:
            configured_rules = json.load(file)
        if not isinstance(configured_rules, dict):
            raise ValueError("heuristic_ctr_rules_must_be_object")
        return _merge_heuristic_ctr_rules(DEFAULT_HEURISTIC_CTR_RULES, configured_rules)
    except Exception as exc:
        api_logger.warning(
            "heuristic_ctr_rules_load_failed path=%s reason=%s:%s",
            rules_path,
            exc.__class__.__name__,
            exc,
        )
        return DEFAULT_HEURISTIC_CTR_RULES


def _heuristic_ctr_rules_path() -> Path:
    configured_path = os.getenv("CTR_HEURISTIC_RULES_PATH")
    if not configured_path:
        return DEFAULT_HEURISTIC_CTR_RULES_PATH
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def _merge_heuristic_ctr_rules(default_rules: dict[str, Any], configured_rules: dict[str, Any]) -> dict[str, Any]:
    merged = dict(default_rules)
    for key, value in configured_rules.items():
        default_value = merged.get(key)
        if isinstance(default_value, dict) and isinstance(value, dict):
            merged[key] = {**default_value, **value}
        else:
            merged[key] = value
    return merged


def _heuristic_number(values: dict[str, Any], key: str, default: Any) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _heuristic_string_set(values: dict[str, Any], key: str, default: Any) -> set[str]:
    configured_values = values.get(key, default)
    if not isinstance(configured_values, list):
        configured_values = default
    return {str(value) for value in configured_values if str(value)}


def _best_score(scores: dict[str, float]) -> tuple[str | None, float | None]:
    if not scores:
        return None, None
    selected_code = max(scores, key=lambda key: (scores[key], key))
    return selected_code, scores[selected_code]


def _assignment_decision(
    *,
    user: dict[str, Any],
    variants: list[dict[str, Any]],
    experiment: dict[str, Any],
    assignment_method: str,
    model_version: str,
    epsilon: float,
) -> dict[str, Any]:
    candidate_scores: dict[str, float] = {}
    candidate_score_breakdowns: dict[str, dict[str, Any]] = {}
    selected_by = assignment_method
    if assignment_method == "model":
        candidate_scores = _score_variants(user, variants, experiment, model_version)
        candidate_score_breakdowns = _score_variant_breakdowns(user, variants, experiment, model_version)
        explore = _stable_unit_interval(experiment["experiment_id"], user["user_id"], "epsilon") < epsilon
        if explore:
            variant = _weighted_variant(variants, experiment["experiment_id"], user["user_id"], "explore")
            selected_by = "explore"
            assignment_source = "weighted_random"
        else:
            selected_code, _ = _best_score(candidate_scores)
            variant = next(item for item in variants if item["variant_code"] == selected_code)
            selected_by = "exploit"
            assignment_source = "model"
        predicted_probability = candidate_scores.get(str(variant["variant_code"]))
    elif assignment_method == "weighted_random":
        variant = _weighted_variant(variants, experiment["experiment_id"], user["user_id"], "weighted_random")
        assignment_source = "weighted_random"
        predicted_probability = None
    else:
        variant = _equal_variant(variants, experiment["experiment_id"], user["user_id"], "random")
        assignment_source = "random"
        predicted_probability = None
    public_decision = {
        "decisionPolicy": "epsilon_greedy" if assignment_method == "model" and epsilon > 0 else "best_score" if assignment_method == "model" else assignment_method,
        "epsilon": epsilon if assignment_method == "model" else None,
        "selectedBy": selected_by,
        "candidateScores": candidate_scores,
        "candidateScoreBreakdowns": candidate_score_breakdowns,
        "selectedScoreBreakdown": candidate_score_breakdowns.get(str(variant.get("variant_code"))) if candidate_score_breakdowns else None,
    }
    return {
        "variant": variant,
        "assignment_source": assignment_source,
        "model_version": model_version if assignment_method == "model" else None,
        "predicted_click_probability": predicted_probability,
        "candidate_scores": candidate_scores,
        "candidate_score_breakdowns": candidate_score_breakdowns,
        "public_decision": public_decision,
    }


def _weighted_variant(variants: list[dict[str, Any]], *seed_parts: Any) -> dict[str, Any]:
    total_weight = sum(float(variant.get("allocation_weight") or 1) for variant in variants)
    threshold = _stable_unit_interval(*seed_parts) * total_weight
    running_weight = 0.0
    for variant in variants:
        running_weight += float(variant.get("allocation_weight") or 1)
        if threshold <= running_weight:
            return variant
    return variants[-1]


def _equal_variant(variants: list[dict[str, Any]], *seed_parts: Any) -> dict[str, Any]:
    index = int(_stable_unit_interval(*seed_parts) * len(variants))
    return variants[min(index, len(variants) - 1)]


def _stable_unit_interval(*parts: Any) -> float:
    source = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)


def _provider_message_id(prefix: str | None, experiment_id: int, user_id: str) -> str | None:
    if not prefix:
        return None
    return f"{prefix}-{experiment_id}-{user_id}"[:100]


def _targeting_snapshot(user: dict[str, Any], experiment: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    age = int(user.get("age") or 0)
    return {
        "age_group": f"{(age // 10) * 10}s" if age else None,
        "gender": user.get("gender"),
        "region": user.get("region"),
        "lifecycle": user.get("lifecycle"),
        "price_sensitivity": user.get("price_sensitivity"),
        "predicted_ltv_segment": user.get("predicted_ltv_segment"),
        "preferred_channels": user.get("preferred_channels") or [],
        "interests": user.get("interests") or [],
        "recent_behaviors": user.get("recent_behaviors") or [],
        "matched_target_segments": _matched_target_segments(user, experiment),
        "decision_policy": decision["public_decision"].get("decisionPolicy"),
        "epsilon": decision["public_decision"].get("epsilon"),
        "selected_by": decision["public_decision"].get("selectedBy"),
        "candidate_scores": decision.get("candidate_scores", {}),
        "candidate_score_breakdowns": decision.get("candidate_score_breakdowns", {}),
    }


def _matched_target_segments(user: dict[str, Any], experiment: dict[str, Any]) -> list[str]:
    segments = set(experiment.get("target_segments") or [])
    matched = []
    age = int(user.get("age") or 0)
    gender = user.get("gender")
    lifecycle = user.get("lifecycle")
    price_sensitivity = user.get("price_sensitivity")
    interests = set(user.get("interests") or [])
    behaviors = set(user.get("recent_behaviors") or [])
    if gender == "female" and 20 <= age < 30 and "20s_female" in segments:
        matched.append("20s_female")
    if lifecycle in segments:
        matched.append(str(lifecycle))
    if price_sensitivity == "high" and "price_sensitive" in segments:
        matched.append("price_sensitive")
    for segment in segments:
        if segment in interests or any(segment in behavior for behavior in behaviors):
            matched.append(str(segment))
    return sorted(set(matched))


def _normalize_message_event_type(event_type: str) -> str | None:
    normalized = event_type.strip().lower().replace("-", "_")
    aliases = {
        "requested": "send_requested",
        "send_request": "send_requested",
        "delivery": "delivered",
        "delivered": "delivered",
        "sent": "sent",
        "impression": "impression",
        "open": "open",
        "opened": "open",
        "click": "click",
        "clicked": "click",
        "conversion": "conversion",
        "converted": "conversion",
        "bounce": "bounce",
        "bounced": "bounce",
        "failed": "failed",
        "unsubscribe": "unsubscribe",
        "unsubscribed": "unsubscribe",
    }
    allowed = {
        "send_requested", "sent", "delivered", "impression", "open", "click",
        "conversion", "bounce", "failed", "unsubscribe",
    }
    mapped = aliases.get(normalized, normalized)
    return mapped if mapped in allowed else None


def _lookup_delivery_for_event(cursor: Any, delivery_id: int | None, provider_message_id: str | None) -> dict[str, Any] | None:
    if delivery_id is not None:
        cursor.execute(
            """
            SELECT delivery_id, provider_message_id, final_status
            FROM campaign_message_deliveries
            WHERE delivery_id = %s
            """,
            (delivery_id,),
        )
    else:
        cursor.execute(
            """
            SELECT delivery_id, provider_message_id, final_status
            FROM campaign_message_deliveries
            WHERE provider_message_id = %s
            """,
            (provider_message_id,),
        )
    row = cursor.fetchone()
    return _jsonable_record(row) if row is not None else None


def _message_event_key(provider: str, delivery: dict[str, Any], event_type: str, request: MessageEventWebhookRequest) -> str:
    provider_message_id = request.provider_message_id or delivery.get("provider_message_id") or f"delivery:{delivery['delivery_id']}"
    if request.provider_event_id:
        return f"{provider}:{provider_message_id}:{event_type}:{request.provider_event_id}"[:200]
    source = "|".join(
        [
            provider,
            str(provider_message_id),
            event_type,
            request.event_at.isoformat(),
            request.click_url or "",
            request.conversion_type or "",
        ]
    )
    return f"{provider}:{provider_message_id}:{event_type}:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"[:200]


def _update_delivery_from_event(cursor: Any, delivery_id: int, event_type: str, request: MessageEventWebhookRequest) -> None:
    status_map = {
        "send_requested": "requested",
        "sent": "sent",
        "delivered": "delivered",
        "bounce": "bounced",
        "failed": "failed",
    }
    final_status = status_map.get(event_type)
    if final_status is None:
        if request.provider_message_id:
            cursor.execute(
                """
                UPDATE campaign_message_deliveries
                SET provider_message_id = COALESCE(provider_message_id, %s)
                WHERE delivery_id = %s
                """,
                (request.provider_message_id, delivery_id),
            )
        return
    requested_at_expression = "COALESCE(requested_at, %s)" if event_type == "send_requested" else "requested_at"
    sent_at_expression = "COALESCE(sent_at, %s)" if event_type == "sent" else "sent_at"
    params: list[Any] = [request.provider_message_id]
    if event_type == "send_requested":
        params.append(request.event_at)
    if event_type == "sent":
        params.append(request.event_at)
    params.extend([final_status, delivery_id])
    cursor.execute(
        f"""
        UPDATE campaign_message_deliveries
        SET provider_message_id = COALESCE(provider_message_id, %s),
            requested_at = {requested_at_expression},
            sent_at = {sent_at_expression},
            final_status = %s
        WHERE delivery_id = %s
        """,
        params,
    )


def _experiment_variant_metrics(cursor: Any, experiment_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT v.variant_id, v.variant_code, v.message_name, v.message_body,
               COALESCE(m.assigned_count, a.assigned_count, 0) AS assigned_count,
               COALESCE(m.sent_count, 0) AS sent_count,
               COALESCE(m.delivered_count, 0) AS delivered_count,
               COALESCE(m.impression_count, 0) AS impression_count,
               COALESCE(m.open_count, 0) AS open_count,
               COALESCE(m.click_count, 0) AS click_count,
               COALESCE(m.conversion_count, 0) AS conversion_count,
               m.delivery_rate_pct, m.impression_rate_pct, m.open_rate_pct,
               m.ctr_pct, m.delivered_ctr_pct,
               m.click_to_conversion_rate_pct, m.cvr_pct,
               COALESCE(m.revenue_krw, 0) AS revenue_krw,
               a.avg_predicted_click_probability
        FROM campaign_message_variants v
        LEFT JOIN v_campaign_variant_metrics m ON m.variant_id = v.variant_id
        LEFT JOIN (
            SELECT variant_id,
                   COUNT(*) AS assigned_count,
                   AVG(predicted_click_probability) AS avg_predicted_click_probability
            FROM campaign_message_deliveries
            WHERE experiment_id = %s
            GROUP BY variant_id
        ) a ON a.variant_id = v.variant_id
        WHERE v.experiment_id = %s
        ORDER BY m.delivered_ctr_pct DESC NULLS LAST, m.ctr_pct DESC NULLS LAST, v.variant_code
        """,
        (experiment_id, experiment_id),
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _experiment_segment_metrics(cursor: Any, experiment_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT experiment_id, experiment_name, variant_id, variant_code, gender, age_group,
               region, lifecycle, assigned_count, delivered_count, impression_count,
               click_count, conversion_count, ctr_pct, click_to_conversion_rate_pct
        FROM v_campaign_segment_metrics
        WHERE experiment_id = %s
        ORDER BY delivered_count DESC NULLS LAST, click_count DESC NULLS LAST, variant_code
        LIMIT 100
        """,
        (experiment_id,),
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _experiment_daily_metrics(cursor: Any, experiment_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT d.campaign_id, d.experiment_id, d.variant_id, v.variant_code, d.channel,
               d.event_date_kst, d.sent_event_count, d.delivered_event_count,
               d.impression_event_count, d.open_event_count, d.click_event_count,
               d.conversion_event_count, d.revenue_krw
        FROM v_campaign_daily_metrics d
        JOIN campaign_message_variants v ON v.variant_id = d.variant_id
        WHERE d.experiment_id = %s
        ORDER BY d.event_date_kst, v.variant_code
        """,
        (experiment_id,),
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _ctr_analysis_summary(
    experiment: dict[str, Any],
    variants: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    generate_next_message: bool,
) -> dict[str, Any]:
    metric_key = _analysis_metric_key(experiment)
    winner = _winner_variant(variants, metric_key)
    analysis_basis = "observed_events"
    winner_metric_key = metric_key
    if winner is None:
        predicted_winner = _winner_variant(variants, "avg_predicted_click_probability")
        if predicted_winner is not None:
            winner = predicted_winner
            analysis_basis = "predicted_assignment"
            winner_metric_key = "avg_predicted_click_probability"
    total_delivered = sum(int(variant.get("delivered_count") or 0) for variant in variants)
    confidence = _analysis_confidence(variants, metric_key, total_delivered) if analysis_basis == "observed_events" else "low"
    observations = _analysis_observations(winner, variants, winner_metric_key, segments)
    if analysis_basis == "predicted_assignment":
        observations.insert(0, "아직 이벤트가 없어 배정 시점의 예측 클릭 확률로 임시 후보를 산정했습니다.")
    risks = []
    if total_delivered < 1000:
        risks.append("표본 수가 1,000건 미만이므로 승자 판단은 탐색적 참고로만 사용해야 합니다.")
    if not any((variant.get(metric_key) is not None) for variant in variants):
        risks.append("선택 지표의 분모 이벤트가 아직 없어 CTR/CVR 계산이 제한됩니다.")
    next_actions = [
        "분모 이벤트가 안정적으로 수집되는지 webhook event_key 중복 제거와 delivered/impression 적재를 확인합니다.",
        "상위 variant와 대조군을 유지한 2차 실험으로 표본을 추가 확보합니다.",
    ]
    if winner:
        next_actions.append(f"{winner['variant_code']} variant의 메시지 특성을 ai_features에 반영해 후속 문구를 생성합니다.")
    return {
        "winner": winner.get("variant_code") if winner else None,
        "confidence": confidence,
        "analysisBasis": analysis_basis,
        "primaryMetricUsed": winner_metric_key,
        "summary": _analysis_summary_text(winner, winner_metric_key),
        "observations": observations,
        "risks": risks,
        "next_actions": next_actions,
        "suggested_message": _suggested_message(winner) if generate_next_message else None,
    }


def _analysis_metric_key(experiment: dict[str, Any]) -> str:
    primary_metric = str(experiment.get("primary_metric") or "ctr")
    if primary_metric == "ctr" and str(experiment.get("channel") or "").lower() in {"lms", "sms", "kakao"}:
        return "delivered_ctr_pct"
    return {
        "delivery_rate": "delivery_rate_pct",
        "impression_rate": "impression_rate_pct",
        "open_rate": "open_rate_pct",
        "ctr": "ctr_pct",
        "cvr": "cvr_pct",
        "revenue": "revenue_krw",
    }.get(primary_metric, "ctr_pct")


def _winner_variant(variants: list[dict[str, Any]], metric_key: str) -> dict[str, Any] | None:
    candidates = [variant for variant in variants if variant.get(metric_key) is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda variant: (float(variant.get(metric_key) or 0), str(variant.get("variant_code"))))


def _analysis_confidence(variants: list[dict[str, Any]], metric_key: str, total_delivered: int) -> str:
    metric_values = sorted([float(variant.get(metric_key) or 0) for variant in variants], reverse=True)
    gap = metric_values[0] - metric_values[1] if len(metric_values) > 1 else 0
    if total_delivered >= 3000 and gap >= 1.0:
        return "high"
    if total_delivered >= 1000 and gap >= 0.3:
        return "medium"
    return "low"


def _analysis_observations(
    winner: dict[str, Any] | None,
    variants: list[dict[str, Any]],
    metric_key: str,
    segments: list[dict[str, Any]],
) -> list[str]:
    observations = []
    if winner:
        observations.append(
            f"{winner['variant_code']} variant가 {metric_key} 기준으로 가장 높습니다."
        )
        if metric_key != "avg_predicted_click_probability" and winner.get("conversion_count") is not None:
            observations.append(
                f"{winner['variant_code']} variant의 클릭 수는 {winner.get('click_count', 0)}건, 전환 수는 {winner.get('conversion_count', 0)}건입니다."
            )
    delivered_counts = [int(variant.get("delivered_count") or 0) for variant in variants]
    if delivered_counts and max(delivered_counts) - min(delivered_counts) > max(1, sum(delivered_counts) * 0.1):
        observations.append("variant별 delivered_count 차이가 커서 배정 균형을 함께 확인해야 합니다.")
    top_segment = max(segments, key=lambda row: int(row.get("click_count") or 0), default=None)
    if top_segment:
        observations.append(
            f"클릭이 가장 많이 발생한 세그먼트는 {top_segment.get('age_group')}/{top_segment.get('gender')}/{top_segment.get('region')} 조합입니다."
        )
    return observations


def _analysis_summary_text(winner: dict[str, Any] | None, metric_key: str) -> str:
    if winner is None:
        return "아직 분석 가능한 variant 데이터가 없습니다."
    value = winner.get(metric_key)
    if value is None:
        return f"{winner['variant_code']} variant가 현재 기본 후보지만 {metric_key} 계산에 필요한 이벤트가 부족합니다."
    return f"{winner['variant_code']} variant가 {metric_key} {value}로 현재 가장 좋은 성과를 보입니다."


def _suggested_message(winner: dict[str, Any] | None) -> str | None:
    if winner is None:
        return None
    body = str(winner.get("message_body") or "").strip()
    if not body:
        return None
    if any(word in body for word in ["오늘", "지금", "마감", "종료"]):
        return body
    return f"{body} 지금 확인해 보세요."


def _response_message_channel(messages: list[Any], requested_channel: str) -> str:
    for message in messages:
        if isinstance(message, dict) and message.get("channel"):
            return str(message["channel"])
    return "rcs" if requested_channel == "rcsSms" else requested_channel


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _log_timing_summary(endpoint: str, timings_ms: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    payload = {
        "endpoint": endpoint,
        "timings_ms": timings_ms,
        **(extra or {}),
    }
    api_logger.info("api_timing %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _database_message_refresh_log_summary(refresh_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": refresh_result.get("status"),
        "reason": refresh_result.get("reason"),
        "context_node_count": refresh_result.get("context_node_count"),
        "message_generation_attempt_count": refresh_result.get("message_generation_attempt_count"),
        "message_generation_failure_reason": refresh_result.get("message_generation_failure_reason"),
        "message_generation_timing": refresh_result.get("message_generation_timing"),
    }


def _message_generation_timing_summary(message_generation: dict[str, Any]) -> dict[str, Any]:
    attempts = message_generation.get("attempts")
    if not isinstance(attempts, list):
        return {"attempts": []}

    return {
        "attempts": [
            {
                "attempt": attempt.get("attempt"),
                "is_success": attempt.get("is_success"),
                "failure_reason": attempt.get("failure_reason"),
                "duration_ms": attempt.get("duration_ms"),
                "variant_attempts": [
                    {
                        "variant": variant_attempt.get("variant"),
                        "is_success": variant_attempt.get("is_success"),
                        "failure_reason": variant_attempt.get("failure_reason"),
                        "duration_ms": variant_attempt.get("duration_ms"),
                    }
                    for variant_attempt in attempt.get("variant_attempts", [])
                    if isinstance(variant_attempt, dict)
                ],
            }
            for attempt in attempts
            if isinstance(attempt, dict)
        ]
    }


def _target_sql_request_options(request: TargetSqlRequest) -> dict[str, Any]:
    return {
        "collection": request.collection,
        "vector_top_k": request.vector_top_k,
        "keyword_top_k": request.keyword_top_k,
        "graph_top_k": request.graph_top_k,
        "hops": request.hops,
        "sql_limit": request.sql_limit,
        "result_row_limit": request.result_row_limit,
        "message_channel": request.message_channel,
        "generate_answer": request.generate_answer,
        "generate_messages": request.generate_messages,
        "message_generation_options": _message_generation_options_payload(request.message_generation_options),
    }


def _message_generation_options_payload(options: MessageGenerationOptions | None) -> dict[str, Any]:
    if options is None:
        return {}
    return {key: value for key, value in options.dict().items() if value is not None}


def _save_target_sql_failure_log(
    request: TargetSqlRequest,
    result: dict[str, Any],
    api_response: dict[str, Any],
    database_execution: dict[str, Any],
) -> dict[str, Any] | None:
    failure = _target_sql_failure_payload(request, result, api_response, database_execution)
    if failure is None:
        return None
    return _save_query_failure_log(failure)


def _target_sql_failure_payload(
    request: TargetSqlRequest,
    result: dict[str, Any],
    api_response: dict[str, Any],
    database_execution: dict[str, Any],
) -> dict[str, Any] | None:
    sql_result = result.get("sql_result", {})
    message_generation = result.get("message_generation", {})
    api_status = api_response.get("status")
    failure_stage = None
    failure_reason = None
    error_detail = None

    if api_status != "success":
        failure_stage = "sql_generation"
        failure_reason = api_response.get("failure_reason") or sql_result.get("failure_reason") or "target_sql_failed"
    elif request.execute_sql and not database_execution.get("is_success"):
        failure_stage = "database_execution"
        failure_reason = database_execution.get("failure_reason") or "database_execution_failed"
        error_detail = database_execution.get("error")
    elif request.generate_messages and message_generation.get("failure_reason"):
        failure_stage = "message_generation"
        failure_reason = message_generation.get("failure_reason")

    if failure_stage is None or failure_reason is None:
        return None

    sql = api_response.get("sql") or sql_result.get("sql")
    return {
        "endpoint": "target_sql",
        "prompt": request.prompt,
        "query_parser": request.query_parser,
        "api_status": api_status,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "error_detail": error_detail,
        "generated_sql": sql,
        "request_options": _target_sql_request_options(request),
        "query_plan": result.get("query_plan", {}),
        "missing_input_conditions": api_response.get("missing_input_conditions", []),
        "clarification_questions": api_response.get("clarification_questions", []),
        "selected_candidate": sql_result.get("selected"),
        "stage_log": result.get("stage_log", []),
        "context_metadata": result.get("context_assembly", {}).get("metadata", {}),
        "database_execution": _failure_log_database_execution(database_execution),
        "message_generation": _failure_log_message_generation(message_generation),
    }


def _failure_log_database_execution(database_execution: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in database_execution.items()
        if key not in {"rows", "segment_composition", "campaign_context_nodes"}
    }


def _failure_log_message_generation(message_generation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in message_generation.items()
        if key not in {"messages", "raw_content", "prompt"}
    }


def _save_query_failure_log(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        api_logger.warning("query_failure_log_skipped reason=psycopg_import_failed error=%s", exc)
        return None

    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                _ensure_query_failure_log_table(cursor)
                sql = payload.get("generated_sql")
                sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None
                cursor.execute(
                    """
                    INSERT INTO campaign_query_failure_logs (
                        endpoint, prompt, query_parser, api_status, failure_stage, failure_reason,
                        error_detail, generated_sql, sql_hash, request_options, query_plan,
                        missing_input_conditions, clarification_questions, selected_candidate,
                        stage_log, context_metadata, database_execution, message_generation
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb
                    )
                    RETURNING failure_log_id, created_at
                    """,
                    (
                        payload.get("endpoint", "unknown"),
                        payload.get("prompt", ""),
                        payload.get("query_parser"),
                        payload.get("api_status"),
                        payload.get("failure_stage", "unknown"),
                        payload.get("failure_reason", "unknown"),
                        payload.get("error_detail"),
                        sql,
                        sql_hash,
                        _json_dumps(payload.get("request_options", {})),
                        _json_dumps(payload.get("query_plan", {})),
                        _json_dumps(payload.get("missing_input_conditions", [])),
                        _json_dumps(payload.get("clarification_questions", [])),
                        _json_dumps(payload.get("selected_candidate")),
                        _json_dumps(payload.get("stage_log", [])),
                        _json_dumps(payload.get("context_metadata", {})),
                        _json_dumps(payload.get("database_execution", {})),
                        _json_dumps(payload.get("message_generation", {})),
                    ),
                )
                saved = _jsonable_record(cursor.fetchone())
        api_logger.info(
            "query_failure_logged failure_log_id=%s stage=%s reason=%s",
            saved.get("failure_log_id"),
            payload.get("failure_stage"),
            payload.get("failure_reason"),
        )
        return saved
    except Exception as exc:
        api_logger.warning("query_failure_log_failed error=%s", f"{exc.__class__.__name__}: {exc}")
        return None


def _ensure_query_failure_log_table(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_query_failure_logs (
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
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_campaign_query_failure_logs_created
            ON campaign_query_failure_logs(created_at DESC)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_campaign_query_failure_logs_reason
            ON campaign_query_failure_logs(failure_stage, failure_reason, created_at DESC)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_campaign_query_failure_logs_status
            ON campaign_query_failure_logs(api_status, created_at DESC)
        """
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    try:
        converted = _jsonable_value(value)
    except Exception:
        return str(value)
    if converted is value:
        return str(value)
    return converted


def execute_target_sql(
    sql: str | None,
    execute_sql: bool,
    result_row_limit: int,
    *,
    persist_targeting: bool = False,
    audience_ttl_days: int = 90,
    prompt: str | None = None,
    query_parser: str | None = None,
    request_options: dict[str, Any] | None = None,
    query_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not execute_sql:
        return _database_execution_skipped("disabled_by_request")
    if not sql:
        return _database_execution_skipped("sql_result_missing")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        return _database_execution_error("psycopg_import_failed", exc)

    target_sql = _strip_sql_for_subquery(sql)
    try:
        with psycopg.connect(_postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                columns = _target_result_columns(cursor, target_sql)
                audience = _save_target_audience(
                    cursor,
                    target_sql,
                    columns,
                    persist_targeting=persist_targeting,
                    audience_ttl_days=audience_ttl_days,
                    prompt=prompt or "",
                    query_parser=query_parser or "rules",
                    request_options=request_options or {},
                    query_plan=query_plan or {},
                )
                rows = _target_result_rows(cursor, target_sql, result_row_limit)
                targeting_result = _targeting_result(cursor, target_sql, columns, rows)
                segment_composition = _segment_composition(cursor, target_sql, columns)
                campaign_context_nodes = _campaign_context_nodes(cursor, target_sql, columns)
    except Exception as exc:
        return _database_execution_error("postgres_execution_failed", exc)

    return {
        "is_success": True,
        "mode": "postgres_read_only",
        "failure_reason": None,
        "executed_sql": target_sql,
        "result_columns": columns,
        "rows": rows,
        "row_limit": result_row_limit,
        "audience": audience,
        "targeting_result": targeting_result,
        "segment_composition": segment_composition,
        "campaign_context_nodes": campaign_context_nodes,
    }


def _save_target_audience(
    cursor: Any,
    sql: str,
    columns: list[str],
    *,
    persist_targeting: bool,
    audience_ttl_days: int,
    prompt: str,
    query_parser: str,
    request_options: dict[str, Any],
    query_plan: dict[str, Any],
) -> dict[str, Any]:
    if not persist_targeting:
        return _audience_skipped("disabled_by_request")
    if "user_id" not in columns:
        return _audience_skipped("user_id_column_missing")

    audience_key = _audience_key(prompt, sql)
    sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    sql_for_parameterized_query = sql.replace("%", "%%")
    campaign_projection = "campaign_id::VARCHAR(20)" if "campaign_id" in columns else "NULL::VARCHAR(20)"

    try:
        cursor.execute("SAVEPOINT target_audience_save")
        cursor.execute(
            """
            INSERT INTO campaign_target_audiences (
                audience_key, prompt, query_parser, request_options, generated_sql,
                sql_hash, query_plan, status, expires_at
            )
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, 'running', CURRENT_TIMESTAMP + (%s * INTERVAL '1 day'))
            RETURNING audience_id, audience_key, status, created_at, expires_at
            """,
            (
                audience_key,
                prompt,
                query_parser,
                json.dumps(request_options, ensure_ascii=False),
                sql,
                sql_hash,
                json.dumps(query_plan, ensure_ascii=False),
                int(audience_ttl_days),
            ),
        )
        audience = _jsonable_record(cursor.fetchone())
        audience_id = audience["audience_id"]
        cursor.execute(
            f"""
            WITH target_result AS MATERIALIZED ({sql_for_parameterized_query}),
            distinct_members AS (
                SELECT DISTINCT user_id::VARCHAR(20) AS user_id, {campaign_projection} AS campaign_id
                FROM target_result
                WHERE user_id IS NOT NULL
            )
            INSERT INTO campaign_target_audience_members (audience_id, user_id, campaign_id)
            SELECT %s, user_id, campaign_id
            FROM distinct_members
            """,
            (audience_id,),
        )
        cursor.execute(
            """
            SELECT COUNT(*) AS member_count,
                   COUNT(DISTINCT user_id) AS target_customer_count,
                   COUNT(DISTINCT campaign_id) FILTER (WHERE campaign_id IS NOT NULL) AS target_campaign_count
            FROM campaign_target_audience_members
            WHERE audience_id = %s
            """,
            (audience_id,),
        )
        counts = _jsonable_record(cursor.fetchone() or {})
        cursor.execute(
            """
            UPDATE campaign_target_audiences
            SET status = 'completed',
                member_count = %s,
                target_customer_count = %s,
                target_campaign_count = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE audience_id = %s
            RETURNING audience_id, audience_key, status, member_count, target_customer_count,
                      target_campaign_count, created_at, completed_at, expires_at
            """,
            (
                counts.get("member_count", 0),
                counts.get("target_customer_count", 0),
                counts.get("target_campaign_count", 0),
                audience_id,
            ),
        )
        saved = _jsonable_record(cursor.fetchone())
        cursor.execute("RELEASE SAVEPOINT target_audience_save")
        return {"is_success": True, "failure_reason": None, **saved}
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT target_audience_save")
        cursor.execute("RELEASE SAVEPOINT target_audience_save")
        return _audience_error("target_audience_save_failed", exc)


def refresh_message_generation_from_database(request: TargetSqlRequest, result: dict[str, Any], database_execution: dict[str, Any]) -> dict[str, Any]:
    message_generation = result.get("message_generation", {})
    if message_generation.get("failure_reason") != "campaign_context_missing":
        return {"status": "skipped", "reason": "message_generation_not_missing_campaign_context", "timings_ms": {}}
    if not database_execution.get("is_success"):
        return {"status": "skipped", "reason": "database_execution_failed", "timings_ms": {}}

    context_nodes = database_execution.get("campaign_context_nodes", [])
    if not context_nodes:
        return {"status": "skipped", "reason": "database_campaign_context_missing", "timings_ms": {}}

    timings_ms: dict[str, float] = {}
    prompt_dir = Path(os.getenv("GRAPH_RAG_PROMPT_DIR", DEFAULT_PROMPT_DIR))
    started_at = time.perf_counter()
    message_context = build_message_context(
        query_plan=result["query_plan"],
        context_nodes=context_nodes,
        sql_result=result["sql_result"],
        requested_channel=request.message_channel,
        business_policies=Path(os.getenv("GRAPH_RAG_BUSINESS_POLICIES", DEFAULT_POLICY_PATH)),
        message_policy=Path(os.getenv("GRAPH_RAG_MESSAGE_POLICY", DEFAULT_MESSAGE_POLICY_PATH)),
    )
    timings_ms["database_message_refresh.context"] = _elapsed_ms(started_at)
    started_at = time.perf_counter()
    message_prompt = render_message_prompt(request.prompt, result["query_plan"], result["sql_result"], message_context, prompt_dir) if message_context.get("is_success") else None
    result["message_generation_prompt"] = message_prompt
    timings_ms["database_message_refresh.prompt"] = _elapsed_ms(started_at)
    started_at = time.perf_counter()
    result["message_generation"] = build_message_response(
        message_prompt=message_prompt,
        message_context=message_context,
        llm_model=os.getenv("OPENAI_MODEL", DEFAULT_LLM_MODEL),
        generate_messages=request.generate_messages,
        prompt_dir=prompt_dir,
        message_generation_options=_message_generation_options_payload(request.message_generation_options),
    )
    timings_ms["database_message_refresh.message_generation"] = _elapsed_ms(started_at)
    message_generation_timing = _message_generation_timing_summary(result["message_generation"])
    return {
        "status": "refreshed",
        "reason": "campaign_context_missing",
        "context_node_count": len(context_nodes),
        "message_generation_attempt_count": result["message_generation"].get("attempt_count", 0),
        "message_generation_failure_reason": result["message_generation"].get("failure_reason"),
        "message_generation_timing": message_generation_timing,
        "timings_ms": timings_ms,
    }


def _postgres_conninfo() -> str:
    return " ".join(
        [
            f"host={os.getenv('POSTGRES_HOST', 'postgres')}",
            f"port={os.getenv('POSTGRES_PORT', '5432')}",
            f"dbname={os.getenv('POSTGRES_DB', 'campaign_db')}",
            f"user={os.getenv('POSTGRES_USER', 'postgres')}",
            f"password={os.getenv('POSTGRES_PASSWORD', '1234')}",
        ]
    )


def _strip_sql_for_subquery(sql: str) -> str:
    return sql.strip().rstrip(";")


def _audience_key(prompt: str, sql: str) -> str:
    source = f"{time.time_ns()}\0{prompt}\0{sql}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def _target_result_columns(cursor: Any, sql: str) -> list[str]:
    cursor.execute(f"WITH target_result AS ({sql}) SELECT * FROM target_result LIMIT 0")
    return [column.name for column in cursor.description or []]


def _target_result_rows(cursor: Any, sql: str, limit: int) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    cursor.execute(f"WITH target_result AS ({sql}) SELECT * FROM target_result LIMIT {safe_limit}")
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _targeting_result(cursor: Any, sql: str, columns: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    count_projection = ["COUNT(*) AS result_row_count"]
    if "user_id" in columns:
        count_projection.append("COUNT(DISTINCT user_id) AS target_customer_count")
    if "campaign_id" in columns:
        count_projection.append("COUNT(DISTINCT campaign_id) AS target_campaign_count")
    cursor.execute(f"WITH target_result AS ({sql}) SELECT {', '.join(count_projection)} FROM target_result")
    counts = _jsonable_record(cursor.fetchone() or {})
    return {
        "result_row_count": counts.get("result_row_count", 0),
        "target_customer_count": counts.get("target_customer_count", 0),
        "target_campaign_count": counts.get("target_campaign_count", 0),
        "sample_rows": rows,
    }


def _segment_composition(cursor: Any, sql: str, columns: list[str]) -> dict[str, Any]:
    composition: dict[str, Any] = {}
    if "user_id" in columns:
        composition.update(
            {
                "gender": _value_counts(cursor, sql, "users", "u.gender"),
                "age_band": _age_band_counts(cursor, sql),
                "region": _value_counts(cursor, sql, "users", "u.region"),
                "lifecycle": _value_counts(cursor, sql, "users", "u.lifecycle"),
                "price_sensitivity": _value_counts(cursor, sql, "users", "u.price_sensitivity"),
                "predicted_ltv_segment": _value_counts(cursor, sql, "users", "u.predicted_ltv_segment"),
                "preferred_channels": _value_counts(cursor, sql, "user_preferred_channels", "upc.preferred_channel"),
                "behaviors": _value_counts(cursor, sql, "user_recent_behaviors", "urb.behavior"),
                "interests": _value_counts(cursor, sql, "user_interests", "ui.interest"),
            }
        )
    if "campaign_id" in columns:
        composition.update(
            {
                "campaigns": _campaign_counts(cursor, sql),
                "campaign_categories": _value_counts(cursor, sql, "campaigns", "c.category"),
                "campaign_channels": _value_counts(cursor, sql, "campaign_channels", "cc.channel"),
                "campaign_target_segments": _value_counts(cursor, sql, "campaign_target_segments", "cts.target_segment"),
            }
        )
    return composition


def _value_counts(cursor: Any, sql: str, source: str, expression: str) -> list[dict[str, Any]]:
    joins = {
        "users": "JOIN users u ON u.user_id = target_users.user_id",
        "user_preferred_channels": "JOIN user_preferred_channels upc ON upc.user_id = target_users.user_id",
        "user_recent_behaviors": "JOIN user_recent_behaviors urb ON urb.user_id = target_users.user_id",
        "user_interests": "JOIN user_interests ui ON ui.user_id = target_users.user_id",
        "campaigns": "JOIN campaigns c ON c.campaign_id = target_campaigns.campaign_id",
        "campaign_channels": "JOIN campaign_channels cc ON cc.campaign_id = target_campaigns.campaign_id",
        "campaign_target_segments": "JOIN campaign_target_segments cts ON cts.campaign_id = target_campaigns.campaign_id",
    }
    target_cte = "target_users AS (SELECT DISTINCT user_id FROM target_result)" if source.startswith("user") else "target_campaigns AS (SELECT DISTINCT campaign_id FROM target_result)"
    cursor.execute(
        f"""
        WITH target_result AS ({sql}),
        {target_cte}
        SELECT {expression} AS value, COUNT(DISTINCT {('target_users.user_id' if source.startswith('user') else 'target_campaigns.campaign_id')}) AS count
        FROM {('target_users' if source.startswith('user') else 'target_campaigns')}
        {joins[source]}
        GROUP BY value
        ORDER BY count DESC, value
        """
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _age_band_counts(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(
        f"""
        WITH target_result AS ({sql}),
        target_users AS (SELECT DISTINCT user_id FROM target_result)
        SELECT CONCAT((u.age / 10) * 10, 's') AS value, COUNT(DISTINCT target_users.user_id) AS count
        FROM target_users
        JOIN users u ON u.user_id = target_users.user_id
        GROUP BY value
        ORDER BY value
        """
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _campaign_counts(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(
        f"""
        WITH target_result AS ({sql}),
        target_campaigns AS (SELECT DISTINCT campaign_id FROM target_result)
        SELECT c.campaign_id, c.name, c.category, c.offer, COUNT(*) AS count
        FROM target_campaigns
        JOIN campaigns c ON c.campaign_id = target_campaigns.campaign_id
        GROUP BY c.campaign_id, c.name, c.category, c.offer
        ORDER BY c.campaign_id
        """
    )
    return [_jsonable_record(row) for row in cursor.fetchall()]


def _campaign_context_nodes(cursor: Any, sql: str, columns: list[str]) -> list[dict[str, Any]]:
    if "campaign_id" not in columns:
        return []

    cursor.execute(
        f"""
        WITH target_result AS ({sql}),
        target_campaigns AS (SELECT DISTINCT campaign_id FROM target_result)
        SELECT
            c.campaign_id,
            c.name,
            c.objective,
            c.category,
            c.offer,
            c.start_date,
            c.end_date,
            c.text_for_embedding,
            COALESCE(ARRAY_AGG(DISTINCT cc.channel) FILTER (WHERE cc.channel IS NOT NULL), '{{}}') AS channels,
            COALESCE(ARRAY_AGG(DISTINCT cts.target_segment) FILTER (WHERE cts.target_segment IS NOT NULL), '{{}}') AS target_segments,
            COALESCE(ARRAY_AGG(DISTINCT ck.keyword) FILTER (WHERE ck.keyword IS NOT NULL), '{{}}') AS keywords
        FROM target_campaigns
        JOIN campaigns c ON c.campaign_id = target_campaigns.campaign_id
        LEFT JOIN campaign_channels cc ON cc.campaign_id = c.campaign_id
        LEFT JOIN campaign_target_segments cts ON cts.campaign_id = c.campaign_id
        LEFT JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id
        GROUP BY c.campaign_id, c.name, c.objective, c.category, c.offer, c.start_date, c.end_date, c.text_for_embedding
        ORDER BY c.campaign_id
        """
    )
    campaign_nodes = [_campaign_context_node(_jsonable_record(row)) for row in cursor.fetchall()]

    cursor.execute(
        f"""
        WITH target_result AS ({sql}),
        target_campaigns AS (SELECT DISTINCT campaign_id FROM target_result)
        SELECT cme.example_id, cme.campaign_id, cme.channel, cme.emphasis_type, cme.message_text, cme.brand_tone
        FROM target_campaigns
        JOIN campaign_message_examples cme ON cme.campaign_id = target_campaigns.campaign_id
        ORDER BY cme.campaign_id, cme.example_id
        """
    )
    example_nodes = [_message_example_context_node(_jsonable_record(row)) for row in cursor.fetchall()]
    return [*campaign_nodes, *example_nodes]


def _campaign_context_node(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "campaign",
        "id": row["campaign_id"],
        "title": row.get("name"),
        "payload": {
            "id": row["campaign_id"],
            "name": row.get("name"),
            "objective": row.get("objective"),
            "category": row.get("category"),
            "channel": row.get("channels", []),
            "target_segments": row.get("target_segments", []),
            "offer": row.get("offer"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
            "keywords": row.get("keywords", []),
            "text_for_embedding": row.get("text_for_embedding"),
        },
    }


def _message_example_context_node(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "campaign_message_example",
        "id": row["example_id"],
        "payload": {
            "id": row["example_id"],
            "campaign_id": row.get("campaign_id"),
            "channel": row.get("channel"),
            "emphasis_type": row.get("emphasis_type"),
            "message_text": row.get("message_text"),
            "brand_tone": row.get("brand_tone"),
        },
    }


def _jsonable_record(record: Any) -> dict[str, Any]:
    return {key: _jsonable_value(value) for key, value in dict(record).items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _database_execution_skipped(reason: str) -> dict[str, Any]:
    return {
        "is_success": False,
        "mode": "skipped",
        "failure_reason": reason,
        "executed_sql": None,
        "result_columns": [],
        "rows": [],
        "audience": _audience_skipped(reason),
        "targeting_result": {},
        "segment_composition": {},
        "campaign_context_nodes": [],
    }


def _database_execution_error(reason: str, exc: Exception) -> dict[str, Any]:
    return {
        "is_success": False,
        "mode": "postgres_read_only",
        "failure_reason": reason,
        "error": f"{exc.__class__.__name__}: {exc}",
        "executed_sql": None,
        "result_columns": [],
        "rows": [],
        "audience": _audience_error(reason, exc),
        "targeting_result": {},
        "segment_composition": {},
        "campaign_context_nodes": [],
    }


def _audience_skipped(reason: str) -> dict[str, Any]:
    return {
        "is_success": False,
        "status": "skipped",
        "failure_reason": reason,
    }


def _audience_error(reason: str, exc: Exception) -> dict[str, Any]:
    return {
        "is_success": False,
        "status": "failed",
        "failure_reason": reason,
        "error": f"{exc.__class__.__name__}: {exc}",
    }