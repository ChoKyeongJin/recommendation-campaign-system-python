"""그래프 RAG 검색 코어 — 지식 그래프 구축 · 벡터/키워드 검색 · 컨텍스트 조립.

graph_rag.py 에서 분리했다. 이 모듈이 담당하는 것은 "질문과 관련된 지식 노드를 찾아
프롬프트 컨텍스트로 만드는 것"까지이고, 타겟팅 SQL 컴파일과는 무관하다 — 분리 전에는
그 둘이 한 파일에 있어서 파일 이름(graph_rag)이 내용의 2% 만 설명했다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(순환 방지). 공유 텍스트 유틸은
common_utils 에서 alias 로 가져온다. 소비 방향은 graph_rag → rag.search 단방향이다.

무거운 의존(fastembed·qdrant_client)이 여기 모여 있다. :func:`vector_search` 만
그것을 쓰므로, 임베딩 없이 도는 경로(규칙 파서·키워드 검색)를 위해 지연 import 로
바꿀 여지가 이 경계 덕분에 생긴다.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx
from fastembed import TextEmbedding
from qdrant_client import QdrantClient

from common_utils import HANGUL_SYLLABLE as _HANGUL_SYLLABLE
from common_utils import unique_strings as _unique_strings


@dataclass(frozen=True)
class SearchHit:
    node_id: str
    score: float
    payload: dict[str, Any]


def load_payload(data_path: Path) -> dict[str, Any]:
    return json.loads(data_path.read_text(encoding="utf-8"))


def build_graph(payload: dict[str, Any]) -> nx.Graph:
    graph = nx.Graph()
    nodes = payload.get("nodes", [])
    nodes_by_id = {node["id"]: node for node in nodes}

    for node in nodes:
        graph.add_node(
            node["id"],
            node_type=node["type"],
            title=_node_title(node),
            text=node.get("text_for_embedding", ""),
            payload=node,
        )

    for node in nodes:
        if node["type"] == "schema_table":
            _add_schema_edges(graph, node, nodes_by_id)
        elif node["type"] == "business_term":
            _add_business_term_edges(graph, node)
        elif node["type"] == "business_policy":
            _add_business_policy_edges(graph, node)
        elif node["type"] == "normalization_rule":
            _add_normalization_edges(graph, node)
        elif node["type"] == "dimension":
            _add_dimension_edges(graph, node)
        elif node["type"] == "dimension_value":
            _add_dimension_value_edges(graph, node)
        elif node["type"] == "sql_example":
            _add_sql_example_edges(graph, node)

    return graph


def vector_search(
    query: str,
    collection: str,
    url: str,
    api_key: str | None,
    embedding_model_name: str,
    limit: int,
) -> list[SearchHit]:
    if limit < 1:
        return []

    embedding_model = TextEmbedding(model_name=embedding_model_name)
    query_vector = list(next(embedding_model.embed([query])))
    client = QdrantClient(url=url, api_key=api_key)

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
    else:
        points = client.search(
            collection_name=collection,
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
        )

    hits = []
    for point in points:
        payload = point.payload or {}
        node_id = payload.get("node_id") or payload.get("source", {}).get("id")
        if not node_id:
            continue
        hits.append(SearchHit(node_id=node_id, score=float(point.score), payload=payload))
    return hits


def keyword_search(graph: nx.Graph, query: str, limit: int) -> list[SearchHit]:
    query_terms = _unique_strings([*_keyword_tokens(query), *_query_tokens(query)])
    if not query_terms or limit < 1:
        return []

    documents: list[tuple[str, dict[str, Any], list[str], str]] = []
    document_frequency: Counter[str] = Counter()
    for node_id, node_data in graph.nodes(data=True):
        haystack = _node_haystack(node_id, node_data)
        document_tokens = _keyword_tokens(haystack)
        if not document_tokens:
            continue
        documents.append((node_id, node_data, document_tokens, haystack))
        document_frequency.update(set(document_tokens))

    if not documents:
        return []

    average_doc_length = sum(len(document_tokens) for _, _, document_tokens, _ in documents) / len(documents)
    hits = []
    for node_id, node_data, document_tokens, haystack in documents:
        token_counts = Counter(document_tokens)
        matched_terms = [term for term in query_terms if token_counts.get(term, 0) > 0]
        if not matched_terms:
            continue
        score = _bm25_score(
            query_terms=matched_terms,
            token_counts=token_counts,
            doc_length=len(document_tokens),
            average_doc_length=average_doc_length,
            document_count=len(documents),
            document_frequency=document_frequency,
        )
        hits.append(
            SearchHit(
                node_id=node_id,
                score=score,
                payload={
                    "node_id": node_id,
                    "node_type": node_data.get("node_type"),
                    "text": node_data.get("text", ""),
                    "matched_terms": matched_terms,
                },
            )
        )

    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]


def _node_haystack(node_id: str, node_data: dict[str, Any]) -> str:
    payload = node_data.get("payload", {})
    return " ".join(
        [
            node_id,
            node_data.get("title", ""),
            node_data.get("text", ""),
            json.dumps(payload, ensure_ascii=False),
        ]
    ).casefold()


def _keyword_tokens(text: str) -> list[str]:
    """BM25 색인/질의용 토큰. 단어 토큰(정확 일치)에 더해, 한글은 교착어라 조사·어미가 붙어 정확
    토큰 일치가 깨지므로('결제수단으로'≠'결제수단') 인접 한글 문자 bigram 을 함께 색인해 변형을
    흡수한다. 질의·문서를 같은 방식으로 토큰화하므로, 정확 단어는 단어+bigram 양쪽으로 걸려 최상위
    점수를 유지하고, 조사/활용 변형은 공유 bigram 으로 부분 점수를 받는다(재현율↑, 정밀도는 idf 로 보정)."""
    tokens: list[str] = []
    for raw_token in re.findall(r"[0-9A-Za-z가-힣_]+", text.casefold()):
        parts = [raw_token, *raw_token.split("_")] if "_" in raw_token else [raw_token]
        for part in parts:
            if len(part) < 2:
                continue
            tokens.append(part)
            # 한글 인접쌍 bigram(3자 이상; 2자 토큰은 그 자체가 bigram 이라 중복 색인하지 않는다).
            # 혼합 토큰('sms수신동의여부')도 한글 구간만 bigram 처리한다.
            if len(part) >= 3:
                tokens.extend(
                    part[i:i + 2]
                    for i in range(len(part) - 1)
                    if _HANGUL_SYLLABLE.match(part[i]) and _HANGUL_SYLLABLE.match(part[i + 1])
                )
    return tokens


def _bm25_score(
    query_terms: list[str],
    token_counts: Counter[str],
    doc_length: int,
    average_doc_length: float,
    document_count: int,
    document_frequency: Counter[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    score = 0.0
    for term in query_terms:
        term_frequency = token_counts.get(term, 0)
        if term_frequency == 0:
            continue
        idf = math.log(1 + (document_count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
        denominator = term_frequency + k1 * (1 - b + b * doc_length / average_doc_length)
        score += idf * (term_frequency * (k1 + 1)) / denominator
    return score


def merge_hits(hits: list[SearchHit]) -> list[SearchHit]:
    merged: dict[str, SearchHit] = {}
    for hit in hits:
        existing = merged.get(hit.node_id)
        if existing is None or hit.score > existing.score:
            merged[hit.node_id] = hit
    return sorted(merged.values(), key=lambda hit: hit.score, reverse=True)


def expand_context(graph: nx.Graph, hits: list[SearchHit], hops: int, limit: int) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    # 각 노드까지의 '대표 경로'(점수 최고 seed에서 최단 경로)를 함께 보관해,
    # UI가 어떤 출발점에서 어떤 관계를 타고 확장됐는지 그대로 보여줄 수 있게 한다.
    best_paths: dict[str, list[str]] = {}
    seed_scores = {hit.node_id: hit.score for hit in hits}

    for hit in hits:
        if hit.node_id not in graph:
            continue
        # _length 대신 실제 경로를 받아, distance(=len(path)-1)와 확장 경로를 동시에 얻는다.
        paths = nx.single_source_shortest_path(graph, hit.node_id, cutoff=hops)
        for node_id, path in paths.items():
            distance = len(path) - 1
            graph_score = hit.score / (1 + distance * 0.35)
            if graph_score > scores.get(node_id, 0.0):
                scores[node_id] = graph_score
                best_paths[node_id] = path
            reasons.setdefault(node_id, []).append(f"seed={hit.node_id}, distance={distance}")

    ordered_node_ids = sorted(scores, key=lambda node_id: scores[node_id], reverse=True)[:limit]
    context = []
    for node_id in ordered_node_ids:
        node_data = graph.nodes[node_id]
        context.append(
            {
                "id": node_id,
                "type": node_data["node_type"],
                "title": node_data["title"],
                "score": round(scores[node_id], 6),
                "seed_score": round(seed_scores.get(node_id, 0.0), 6) if node_id in seed_scores else None,
                "reasons": reasons[node_id][:3],
                "path": _describe_path(graph, best_paths.get(node_id, [node_id])),
                "neighbors": _neighbor_summary(graph, node_id),
                "payload": _compact_payload(node_data["payload"]),
            }
        )
    return context


def _describe_path(graph: nx.Graph, path_ids: list[str]) -> list[dict[str, Any]]:
    """출발점(seed)→목표 노드까지의 경로를 관계명과 함께 사람이 읽을 수 있는 형태로 만든다.

    각 원소는 {id, title, type, relation}이며 relation 은 '직전 노드에서 이 노드로 온 엣지'의
    관계명(첫 노드=seed 는 None)이다. UI 브레드크럼(A ─relation→ B ─relation→ C)에 그대로 쓴다.
    """
    described: list[dict[str, Any]] = []
    previous_id: str | None = None
    for node_id in path_ids:
        node_data = graph.nodes[node_id]
        relation = None
        if previous_id is not None:
            edge_data = graph.get_edge_data(previous_id, node_id) or {}
            relation = edge_data.get("relation", "related")
        described.append(
            {
                "id": node_id,
                "title": node_data.get("title", node_id),
                "type": node_data.get("node_type", "unknown"),
                "relation": relation,
            }
        )
        previous_id = node_id
    return described


def render_prompt_context(context_nodes: list[dict[str, Any]]) -> str:
    sections = []
    for index, node in enumerate(context_nodes, start=1):
        payload = node["payload"]
        text = payload.get("text_for_embedding") or payload.get("description") or payload.get("sql") or ""
        sections.append(f"[{index}] {node['type']} {node['title']}\n{text}")
    return "\n\n".join(sections)


def assemble_context(context_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    top_k_chunks = []
    graph_context = []
    node_type_counts: Counter[str] = Counter()

    for index, node in enumerate(context_nodes, start=1):
        payload = node["payload"]
        text = payload.get("text_for_embedding") or payload.get("description") or payload.get("sql") or ""
        node_type_counts[node["type"]] += 1
        top_k_chunks.append(
            {
                "rank": index,
                "id": node["id"],
                "type": node["type"],
                "title": node["title"],
                "score": node["score"],
                "text": text,
            }
        )
        graph_context.append(
            {
                "id": node["id"],
                "type": node["type"],
                "score": node["score"],
                "neighbors": node["neighbors"],
                "reasons": node["reasons"],
            }
        )

    return {
        "top_k_chunks": top_k_chunks,
        "graph_context": graph_context,
        "metadata": {
            "node_count": len(context_nodes),
            "node_types": dict(sorted(node_type_counts.items())),
        },
        "prompt": render_prompt_context(context_nodes),
    }


def graph_stats(graph: nx.Graph) -> dict[str, Any]:
    node_types = Counter(nx.get_node_attributes(graph, "node_type").values())
    edge_types = Counter(edge_data.get("relation", "related") for _, _, edge_data in graph.edges(data=True))
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
    }


def _query_tokens(query: str) -> list[str]:
    raw_tokens = [token.strip().lower() for token in query.replace("_", " ").split()]
    compact_query = query.replace(" ", "").lower()
    tokens = {token for token in raw_tokens if len(token) >= 2}
    for raw_token in raw_tokens:
        if raw_token:
            tokens.add(raw_token.replace(" ", ""))
    if "앱푸시" in compact_query:
        tokens.update({"앱푸시", "앱 푸시", "app_push"})
    if "카카오" in query or "카톡" in query:
        tokens.update({"kakao", "카카오", "카톡"})
    if "coupon" in query.lower() or "쿠폰" in query or "할인" in query:
        tokens.update({"coupon", "쿠폰", "할인"})
    if "sql" in query.lower() or "쿼리" in query:
        tokens.update({"sql", "select", "쿼리"})
    return sorted(tokens, key=len, reverse=True)


def _add_schema_edges(graph: nx.Graph, node: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]) -> None:
    table_node_id = node["id"]
    table_name = node["table_name"]
    for column in node.get("columns", []):
        column_node_id = _column_node_id(table_name, column["name"])
        graph.add_node(
            column_node_id,
            node_type="schema_column",
            title=f"{table_name}.{column['name']}",
            text=f"컬럼 {table_name}.{column['name']} {column['type']}",
            payload={"id": column_node_id, "type": "schema_column", "table_name": table_name, **column},
        )
        graph.add_edge(table_node_id, column_node_id, relation="has_column")

        reference = column.get("references")
        if reference:
            target_table_node_id = f"schema_table:{reference['table']}"
            target_column_node_id = _column_node_id(reference["table"], reference["column"])
            if target_table_node_id in nodes_by_id:
                graph.add_edge(table_node_id, target_table_node_id, relation="foreign_key_to")
                graph.add_edge(column_node_id, target_table_node_id, relation="references_table")
            if target_column_node_id in graph:
                graph.add_edge(column_node_id, target_column_node_id, relation="references_column")

    for foreign_key in node.get("foreign_keys", []):
        reference = foreign_key.get("references", {})
        target_table = reference.get("table")
        if not target_table:
            continue
        target_table_node_id = f"schema_table:{target_table}"
        if target_table_node_id in nodes_by_id:
            graph.add_edge(table_node_id, target_table_node_id, relation="foreign_key_to")
        for column_name, target_column_name in zip(foreign_key.get("columns", []), reference.get("columns", [])):
            column_node_id = _column_node_id(table_name, column_name)
            target_column_node_id = _column_node_id(target_table, target_column_name)
            if column_node_id in graph and target_table_node_id in nodes_by_id:
                graph.add_edge(column_node_id, target_table_node_id, relation="references_table")
            if column_node_id in graph and target_column_node_id in graph:
                graph.add_edge(column_node_id, target_column_node_id, relation="references_column")


def _add_business_term_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("related_tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="business_term_table")

    for column_name in node.get("related_columns", []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="business_term_column")


def _add_business_policy_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("related_tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="business_policy_table")

    for column_name in node.get("related_columns", []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="business_policy_column")


def _add_normalization_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    business_term_node_id = f"business_term:{node.get('canonical')}"
    if business_term_node_id in graph:
        graph.add_edge(node["id"], business_term_node_id, relation="normalizes_business_term")


def _add_dimension_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    # 디멘션(예: 상품브랜드)을 실제 필터 대상 스키마 테이블/컬럼에 연결해
    # 브랜드명 -> 코드 -> BRAND_ID IN (...) 경로가 스키마 허브로 이어지게 한다.
    table_name = node.get("target_table")
    if table_name:
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="dimension_filters_table")

    column_name = node.get("target_column")
    if column_name:
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="dimension_filters_column")


def _add_dimension_value_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    dimension_node_id = f"dimension:{node.get('dimension_id')}"
    if dimension_node_id in graph:
        graph.add_edge(node["id"], dimension_node_id, relation="value_of_dimension")
    # 회원 값 인덱스 노드는 저장 컬럼/테이블로 연결해 값→컬럼→테이블 그래프 확장이 이어지게 한다.
    column_name = node.get("target_column")
    if column_name and f"schema_column:{column_name}" in graph:
        graph.add_edge(node["id"], f"schema_column:{column_name}", relation="value_of_column")
    table_name = node.get("target_table")
    if table_name and f"schema_table:{table_name}" in graph:
        graph.add_edge(node["id"], f"schema_table:{table_name}", relation="value_in_table")


def _add_sql_example_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="sql_uses_table")


def _node_title(node: dict[str, Any]) -> str:
    if node["type"] == "schema_table":
        return node.get("table_name", node["id"])
    if node["type"] == "normalization_rule":
        return node.get("canonical", node["id"])
    if node["type"] == "business_term":
        return node.get("term", node["id"])
    if node["type"] == "business_policy":
        return node.get("ko_label", node.get("canonical", node["id"]))
    if node["type"] == "sql_example":
        return node.get("title", node["id"])
    if node["type"] == "dimension":
        return node.get("prompt_label", node["id"])
    if node["type"] == "dimension_value":
        return node.get("name", node["id"])
    if node["type"] == "campaign":
        return node.get("name", node["id"])
    if node["type"] == "user":
        return node.get("id", node["id"])
    return node["id"]


def _column_node_id(table_name: str, column_name: str) -> str:
    return f"schema_column:{table_name}.{column_name}"


def _neighbor_summary(graph: nx.Graph, node_id: str) -> list[dict[str, str]]:
    neighbors = []
    for neighbor_id in list(graph.neighbors(node_id))[:12]:
        edge_data = graph.get_edge_data(node_id, neighbor_id) or {}
        neighbors.append(
            {
                "id": neighbor_id,
                "type": graph.nodes[neighbor_id].get("node_type", "unknown"),
                "relation": edge_data.get("relation", "related"),
            }
        )
    return neighbors


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep_keys = [
        "id",
        "type",
        "table_name",
        "description",
        "columns",
        "canonical",
        "ko_label",
        "synonyms",
        "negative_synonyms",
        "term",
        "policy_id",
        "metric",
        "scope",
        "expression",
        "operator",
        "threshold_krw",
        "requires_threshold",
        "sql_behavior",
        "order_by",
        "related_tables",
        "related_columns",
        "title",
        "name",
        "objective",
        "category",
        "channel",
        "channels",
        "target_segments",
        "offer",
        "start_date",
        "end_date",
        "keywords",
        "expected_ctr",
        "expected_cvr",
        "campaign_id",
        "emphasis_type",
        "message_text",
        "brand_tone",
        "sql",
        "tables",
        "text_for_embedding",
    ]
    return {key: payload[key] for key in keep_keys if key in payload}
