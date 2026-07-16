from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_BUSINESS_TERMS = [
    {
        "term_id": "business_campaign",
        "term": "캠페인",
        "canonical": "campaign",
        "description": "특정 목적과 기간, 예산, 혜택, 타겟 세그먼트를 가진 마케팅 실행 단위",
        "related_tables": ["campaigns"],
        "related_columns": ["campaigns.campaign_id", "campaigns.objective", "campaigns.category"],
        "synonyms": ["프로모션", "마케팅 캠페인", "campaign"],
    },
    {
        "term_id": "business_user",
        "term": "사용자",
        "canonical": "user",
        "description": "캠페인 추천 대상이 되는 고객 또는 회원",
        "related_tables": ["users"],
        "related_columns": ["users.user_id", "users.lifecycle"],
        "synonyms": ["고객", "회원", "유저", "user"],
    },
    {
        "term_id": "business_recommendation",
        "term": "추천",
        "canonical": "recommendation",
        "description": "사용자와 캠페인 사이의 매칭 결과와 추천 사유",
        "related_tables": ["recommendation_edges"],
        "related_columns": ["recommendation_edges.user_id", "recommendation_edges.campaign_id", "recommendation_edges.reason", "recommendation_edges.label"],
        "synonyms": ["매칭", "추천 결과", "recommendation"],
    },
    {
        "term_id": "business_target_segment",
        "term": "타겟 세그먼트",
        "canonical": "target_segment",
        "description": "캠페인이 겨냥하는 고객 조건 또는 고객군",
        "related_tables": ["campaign_target_segments"],
        "related_columns": ["campaign_target_segments.target_segment"],
        "synonyms": ["대상 세그먼트", "타겟", "고객군", "segment"],
    },
    {
        "term_id": "business_channel",
        "term": "채널",
        "canonical": "channel",
        "description": "캠페인 발송 또는 노출 경로와 사용자의 선호 접점",
        "related_tables": ["campaign_channels", "user_preferred_channels"],
        "related_columns": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
        "synonyms": ["발송 채널", "접점", "매체", "channel"],
    },
    {
        "term_id": "business_interest",
        "term": "관심사",
        "canonical": "interest",
        "description": "사용자가 관심을 보이는 상품 카테고리 또는 주제",
        "related_tables": ["user_interests"],
        "related_columns": ["user_interests.interest"],
        "synonyms": ["취향", "관심 카테고리", "선호 분야", "interest"],
    },
    {
        "term_id": "business_recent_behavior",
        "term": "최근 행동",
        "canonical": "recent_behavior",
        "description": "최근 조회, 클릭, 구매, 이탈 등 추천 조건으로 쓰는 사용자 행동 신호",
        "related_tables": ["user_recent_behaviors"],
        "related_columns": ["user_recent_behaviors.behavior"],
        "synonyms": ["행동", "액션", "사용자 행동", "behavior"],
    },
    {
        "term_id": "business_lifecycle",
        "term": "라이프사이클",
        "canonical": "lifecycle",
        "description": "신규, 활성, 휴면, VIP 등 현재 고객 상태",
        "related_tables": ["users"],
        "related_columns": ["users.lifecycle"],
        "synonyms": ["고객 상태", "고객 단계", "lifecycle"],
    },
    {
        "term_id": "business_price_sensitivity",
        "term": "가격 민감도",
        "canonical": "price_sensitivity",
        "description": "할인, 쿠폰, 특가에 반응하는 정도",
        "related_tables": ["users"],
        "related_columns": ["users.price_sensitivity"],
        "synonyms": ["할인 민감도", "쿠폰 반응", "특가 반응", "price sensitivity"],
    },
    {
        "term_id": "business_ltv_segment",
        "term": "LTV 세그먼트",
        "canonical": "predicted_ltv_segment",
        "description": "예측 생애가치 기준으로 나눈 고객 가치 등급",
        "related_tables": ["users"],
        "related_columns": ["users.predicted_ltv_segment"],
        "synonyms": ["고객 가치", "예측 LTV", "ltv segment"],
    },
    {
        "term_id": "business_offer",
        "term": "혜택",
        "canonical": "offer",
        "description": "캠페인이 사용자에게 제공하는 쿠폰, 할인, 무료배송, 바우처 등의 제안",
        "related_tables": ["campaigns"],
        "related_columns": ["campaigns.offer"],
        "synonyms": ["오퍼", "프로모션 혜택", "benefit", "offer"],
    },
    {
        "term_id": "business_channel_message",
        "term": "채널 메시지",
        "canonical": "channel_message",
        "description": "타겟팅 SQL로 선정한 캠페인과 고객 세그먼트에 대해 LMS 또는 RCS로 생성하거나 실제 발송한 마케팅 문안",
        "related_tables": ["campaigns", "campaign_channels", "campaign_message_examples", "campaign_channel_messages", "campaign_message_variants"],
        "related_columns": [
            "campaigns.campaign_id",
            "campaigns.offer",
            "campaign_channels.channel",
            "campaign_message_examples.channel",
            "campaign_message_examples.message_text",
            "campaign_channel_messages.message_body",
            "campaign_message_variants.message_body",
        ],
        "synonyms": ["발송 문안", "메시지 문안", "LMS 메시지", "RCS 메시지", "message copy"],
    },
    {
        "term_id": "business_message_example",
        "term": "기존 메시지",
        "canonical": "message_example",
        "description": "캠페인별 과거 발송 문안 예시로, 새 메시지를 만들 때 혜택 표현과 브랜드 톤의 근거로 사용한다",
        "related_tables": ["campaign_message_examples"],
        "related_columns": [
            "campaign_message_examples.campaign_id",
            "campaign_message_examples.channel",
            "campaign_message_examples.emphasis_type",
            "campaign_message_examples.message_text",
        ],
        "synonyms": ["기존 문안", "과거 메시지", "참고 메시지", "message example"],
    },
    {
        "term_id": "business_brand_tone",
        "term": "브랜드 톤",
        "canonical": "brand_tone",
        "description": "캠페인 메시지에서 유지해야 하는 말투와 표현 스타일",
        "related_tables": ["campaign_message_examples"],
        "related_columns": ["campaign_message_examples.brand_tone"],
        "synonyms": ["브랜드 말투", "톤앤매너", "tone of voice", "brand tone"],
    },
    {
        "term_id": "business_ctr",
        "term": "CTR",
        "canonical": "expected_ctr",
        "description": "캠페인에 저장된 예상 CTR 또는 실제 메시지 이벤트에서 집계한 노출 대비 클릭률",
        "related_tables": ["campaigns", "v_campaign_variant_metrics", "v_campaign_segment_metrics"],
        "related_columns": ["campaigns.expected_ctr", "v_campaign_variant_metrics.ctr_pct", "v_campaign_segment_metrics.ctr_pct"],
        "synonyms": ["클릭률", "CTR", "expected ctr", "실제 CTR", "variant ctr"],
    },
    {
        "term_id": "business_cvr",
        "term": "예상 CVR",
        "canonical": "expected_cvr",
        "description": "캠페인에 저장된 예상 CVR 또는 실제 메시지 이벤트에서 집계한 전환율",
        "related_tables": ["campaigns", "v_campaign_variant_metrics", "v_campaign_segment_metrics"],
        "related_columns": ["campaigns.expected_cvr", "v_campaign_variant_metrics.cvr_pct", "v_campaign_segment_metrics.click_to_conversion_rate_pct"],
        "synonyms": ["전환율", "CVR", "expected cvr", "실제 CVR"],
    },
    {
        "term_id": "business_message_experiment",
        "term": "메시지 실험",
        "canonical": "message_experiment",
        "description": "캠페인 채널 안에서 메시지 A/B/C 버전을 비교하는 실험 단위",
        "related_tables": ["campaign_experiments", "campaign_message_variants"],
        "related_columns": ["campaign_experiments.experiment_id", "campaign_experiments.primary_metric", "campaign_message_variants.variant_code"],
        "synonyms": ["A/B 테스트", "ABC 테스트", "문구 실험", "message experiment"],
    },
    {
        "term_id": "business_message_delivery",
        "term": "메시지 발송",
        "canonical": "message_delivery",
        "description": "사용자에게 특정 캠페인 메시지 variant를 배정하고 발송한 사실",
        "related_tables": ["campaign_message_deliveries"],
        "related_columns": ["campaign_message_deliveries.delivery_id", "campaign_message_deliveries.user_id", "campaign_message_deliveries.final_status"],
        "synonyms": ["발송 이력", "delivery", "메시지 배정", "발송 상태"],
    },
    {
        "term_id": "business_message_event",
        "term": "메시지 이벤트",
        "canonical": "message_event",
        "description": "발송 요청, 도달, 노출, 클릭, 전환 등 메시지 반응 로그",
        "related_tables": ["campaign_message_events"],
        "related_columns": ["campaign_message_events.event_type", "campaign_message_events.event_at", "campaign_message_events.conversion_value_krw"],
        "synonyms": ["이벤트 로그", "클릭 이벤트", "전환 이벤트", "message event"],
    },
    {
        "term_id": "business_campaign_metrics",
        "term": "캠페인 성과 지표",
        "canonical": "campaign_metrics",
        "description": "메시지 variant, 고객 세그먼트, 날짜 단위로 집계한 발송 퍼널, CTR/CVR, 매출 지표",
        "related_tables": ["v_campaign_variant_metrics", "v_campaign_segment_metrics", "v_campaign_daily_metrics"],
        "related_columns": ["v_campaign_variant_metrics.ctr_pct", "v_campaign_variant_metrics.revenue_krw", "v_campaign_daily_metrics.event_date_kst"],
        "synonyms": ["성과 분석", "퍼널 지표", "CTR 분석", "CVR 분석", "매출 지표"],
    },
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_columns(columns: list[dict[str, Any]]) -> str:
    rendered = []
    for column in columns:
        flags = []
        if column.get("primary_key"):
            flags.append("PK")
        if column.get("references"):
            reference = column["references"]
            flags.append(f"FK {reference['table']}.{reference['column']}")
        if column.get("important"):
            flags.append("important")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        rendered.append(f"{column['name']} {column['type']}{flag_text}")
    return "; ".join(rendered)


def render_foreign_keys(table: dict[str, Any], columns: list[dict[str, Any]]) -> str:
    foreign_keys = [
        f"{column['name']} -> {column['references']['table']}.{column['references']['column']}"
        for column in columns
        if column.get("references")
    ]
    for foreign_key in table.get("foreign_keys", []):
        reference = foreign_key.get("references", {})
        local_columns = ", ".join(foreign_key.get("columns", []))
        reference_columns = ", ".join(reference.get("columns", []))
        reference_table = reference.get("table")
        if local_columns and reference_table and reference_columns:
            foreign_keys.append(f"{local_columns} -> {reference_table}.{reference_columns}")
    return ", ".join(foreign_keys) or "없음"


def schema_nodes(schema_catalog: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for table_name, table in schema_catalog.get("tables", {}).items():
        columns = table.get("columns", [])
        primary_key = ", ".join(table.get("primary_key", [])) or "없음"
        indexes = ", ".join(index["name"] for index in table.get("indexes", [])) or "없음"
        text = (
            f"테이블 {table_name}. {table.get('description_llm', '')} "
            f"주요 컬럼과 타입: {render_columns(columns)}. "
            f"Primary key: {primary_key}. Foreign keys: {render_foreign_keys(table, columns)}. "
            f"Indexes: {indexes}."
        )
        nodes.append(
            {
                "id": f"schema_table:{table_name}",
                "type": "schema_table",
                "table_name": table_name,
                "description": table.get("description_llm", ""),
                "columns": columns,
                "primary_key": table.get("primary_key", []),
                "checks": table.get("checks", []),
                "foreign_keys": table.get("foreign_keys", []),
                "indexes": table.get("indexes", []),
                "text_for_embedding": text,
            }
        )
    return nodes


def normalization_nodes(normalization_payload: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for rule in normalization_payload.get("normalization_rules", []):
        synonyms = rule.get("synonyms", [])
        negative_synonyms = rule.get("negative_synonyms", [])
        text = (
            f"정규화 사전 {rule['canonical']}는 {rule.get('ko_label', rule['canonical'])}을 의미한다. "
            f"동의어: {', '.join(synonyms)}. "
            f"부정 동의어: {', '.join(negative_synonyms) if negative_synonyms else '없음'}."
        )
        nodes.append(
            {
                "id": f"normalization_rule:{rule['rule_id']}",
                "type": "normalization_rule",
                **rule,
                "text_for_embedding": text,
            }
        )
    return nodes


def business_term_nodes(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = []
    for term in terms:
        text = (
            f"비즈니스 용어 {term['term']} canonical {term['canonical']}. {term['description']}. "
            f"관련 테이블: {', '.join(term.get('related_tables', []))}. "
            f"관련 컬럼: {', '.join(term.get('related_columns', []))}. "
            f"동의어: {', '.join(term.get('synonyms', []))}."
        )
        nodes.append(
            {
                "id": f"business_term:{term['canonical']}",
                "type": "business_term",
                **term,
                "text_for_embedding": text,
            }
        )
    return nodes


def sql_example_nodes(sql_text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"--\s*(?P<number>\d+)\.\s*(?P<title>.+?)\n(?P<sql>.*?;)", re.DOTALL)
    nodes = []
    for match in pattern.finditer(sql_text):
        number = int(match.group("number"))
        title = match.group("title").strip()
        sql = match.group("sql").strip()
        tables = sorted(set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql, re.IGNORECASE)))
        text = f"SQL 예시 {number}. {title}. 관련 테이블: {', '.join(tables)}. 쿼리: {sql}"
        nodes.append(
            {
                "id": f"sql_example:{number:02d}",
                "type": "sql_example",
                "title": title,
                "sql": sql,
                "tables": tables,
                "text_for_embedding": text,
            }
        )
    return nodes


def campaign_user_nodes(campaign_user_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not campaign_user_payload:
        return []

    nodes = []
    for raw_node in campaign_user_payload.get("nodes", []):
        if not isinstance(raw_node, dict):
            continue
        node_type = raw_node.get("type")
        if node_type not in {"campaign", "user"}:
            continue
        text = raw_node.get("text_for_embedding")
        if not (isinstance(text, str) and text.strip()):
            continue
        nodes.append(dict(raw_node))
    return nodes


def campaign_user_edges(campaign_user_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not campaign_user_payload:
        return []

    edges = campaign_user_payload.get("recommendation_edges") or []
    return [dict(edge) for edge in edges if isinstance(edge, dict)]


def policy_nodes(policy_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not policy_payload:
        return []

    nodes = []
    for policy in policy_payload.get("policies", []):
        synonyms = policy.get("synonyms", [])
        related_tables = policy.get("related_tables") or [policy.get("table")]
        related_columns = policy.get("related_columns") or [
            f"{policy.get('table')}.{policy.get('column')}" if policy.get("table") and policy.get("column") else ""
        ]
        threshold_text = (
            f"기준값: {policy['threshold_krw']}원."
            if policy.get("threshold_krw") is not None
            else "기준값: 정책 파일에서 아직 확정하지 않음."
        )
        behavior = policy.get("sql_behavior", "context")
        text = (
            f"업무 정책 {policy.get('ko_label', policy['canonical'])} canonical {policy['canonical']}. "
            f"{policy.get('description', '')} "
            f"SQL 동작: {behavior}. metric: {policy.get('metric')}. "
            f"표현식: {policy.get('expression') or policy.get('column')}. {threshold_text} "
            f"관련 테이블: {', '.join(table for table in related_tables if table)}. "
            f"관련 컬럼: {', '.join(column for column in related_columns if column)}. "
            f"동의어: {', '.join(synonyms)}."
        )
        nodes.append(
            {
                "id": f"business_policy:{policy['policy_id']}",
                "type": "business_policy",
                **policy,
                "related_tables": [table for table in related_tables if table],
                "related_columns": [column for column in related_columns if column],
                "text_for_embedding": text,
            }
        )
    return nodes


def metric_alias_nodes(metric_lexicon_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not metric_lexicon_payload:
        return []

    nodes = []
    for metric in metric_lexicon_payload.get("metrics", []):
        table = metric.get("table")
        column = metric.get("column")
        if not metric.get("metric_id") or not metric.get("canonical") or not table or not column:
            continue
        related_columns = [f"{table}.{column}"]
        synonyms = metric.get("synonyms", [])
        text = (
            f"계산 지표 별칭 {metric.get('ko_label', metric['canonical'])} canonical {metric['canonical']}. "
            f"자연어 계산식에서 {table}.{column} 숫자형 컬럼으로 해석한다. "
            f"관련 테이블: {table}. 관련 컬럼: {', '.join(related_columns)}. "
            f"동의어: {', '.join(synonyms)}."
        )
        nodes.append(
            {
                "id": f"metric_alias:{metric['metric_id']}",
                "type": "metric_alias",
                **metric,
                "related_tables": [table],
                "related_columns": related_columns,
                "text_for_embedding": text,
            }
        )
    return nodes


def build_payload(
    schema_catalog: dict[str, Any],
    normalization_payload: dict[str, Any],
    sql_text: str,
    policy_payload: dict[str, Any] | None = None,
    metric_lexicon_payload: dict[str, Any] | None = None,
    campaign_user_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = [
        *schema_nodes(schema_catalog),
        *normalization_nodes(normalization_payload),
        *business_term_nodes(DEFAULT_BUSINESS_TERMS),
        *policy_nodes(policy_payload),
        *metric_alias_nodes(metric_lexicon_payload),
        *sql_example_nodes(sql_text),
        *campaign_user_nodes(campaign_user_payload),
    ]
    recommendation_edges = campaign_user_edges(campaign_user_payload)
    return {
        "version": "1.0",
        "description": "NL2SQL/RAG 검색용 지식 노드. 테이블 스키마, 정규화 사전, 비즈니스 용어, 업무 정책, SQL 예시, 캠페인/사용자 샘플을 포함한다.",
        "source_files": {
            "table_schema": "docs/data/schema_catalog.json",
            "normalization_dictionary": "docs/data/normalization_rules.sample.json",
            "business_policies": "docs/data/business_policies.sample.json",
            "metric_lexicon": "docs/data/metric_lexicon.sample.json",
            "sql_examples": "docs/data/sql_examples.sample.sql",
            "business_terms": "build_rag_knowledge.py",
            "campaign_user_nodes": "docs/data/campaign_user_rag_sample_50_with_edges.json",
        },
        "node_counts": {
            "schema_table": len([node for node in nodes if node["type"] == "schema_table"]),
            "normalization_rule": len([node for node in nodes if node["type"] == "normalization_rule"]),
            "business_term": len([node for node in nodes if node["type"] == "business_term"]),
            "business_policy": len([node for node in nodes if node["type"] == "business_policy"]),
            "metric_alias": len([node for node in nodes if node["type"] == "metric_alias"]),
            "sql_example": len([node for node in nodes if node["type"] == "sql_example"]),
            "campaign": len([node for node in nodes if node["type"] == "campaign"]),
            "user": len([node for node in nodes if node["type"] == "user"]),
            "total": len(nodes),
        },
        "nodes": nodes,
        "recommendation_edges": recommendation_edges,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build RAG-ready JSON knowledge nodes.")
    parser.add_argument("--schema", type=Path, default=Path("docs/data/schema_catalog.json"))
    parser.add_argument("--normalization", type=Path, default=Path("docs/data/normalization_rules.sample.json"))
    parser.add_argument("--business-policies", type=Path, default=Path("docs/data/business_policies.sample.json"))
    parser.add_argument("--metric-lexicon", type=Path, default=Path("docs/data/metric_lexicon.sample.json"))
    parser.add_argument("--sql-examples", type=Path, default=Path("docs/data/sql_examples.sample.sql"))
    parser.add_argument("--campaign-user", type=Path, default=Path("docs/data/campaign_user_rag_sample_50_with_edges.json"))
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data/rag_knowledge_base.json"))
    args = parser.parse_args()

    payload = build_payload(
        schema_catalog=load_json(args.schema),
        normalization_payload=load_json(args.normalization),
        sql_text=args.sql_examples.read_text(encoding="utf-8"),
        policy_payload=load_json(args.business_policies) if args.business_policies.exists() else None,
        metric_lexicon_payload=load_json(args.metric_lexicon) if args.metric_lexicon.exists() else None,
        campaign_user_payload=load_json(args.campaign_user) if args.campaign_user.exists() else None,
    )
    save_json(args.output, payload)
    print(json.dumps(payload["node_counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()