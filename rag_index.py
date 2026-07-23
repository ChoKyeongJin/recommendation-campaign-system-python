from __future__ import annotations

import argparse
import html
import json
import os
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models


DEFAULT_COLLECTION = "campaign_user_rag_nodes"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
HTML_SCRIPT_STYLE_PATTERN = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"\s+")
SENTENCE_PATTERN = re.compile(r"[^.!?。！？]+[.!?。！？]*")
SPECIAL_CHARACTER_PATTERN = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s.!?%+\-_,:/]")


@dataclass(frozen=True)
class RagNode:
    node_id: str
    node_type: str
    text: str
    metadata: dict[str, Any]


def clean_rag_text(value: Any) -> str:
    if value is None:
        return ""

    text = html.unescape(str(value))
    text = HTML_SCRIPT_STYLE_PATTERN.sub(" ", text)
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = _normalize_whitespace(text)
    text = _deduplicate_sentences(text)
    text = SPECIAL_CHARACTER_PATTERN.sub(" ", text)
    return _normalize_whitespace(text)


def clean_text_dataframe(
    dataframe: pd.DataFrame,
    text_column: str = "text",
    output_column: str | None = None,
    inplace: bool = False,
) -> pd.DataFrame:
    if text_column not in dataframe.columns:
        raise ValueError(f"DataFrame must contain a '{text_column}' column.")

    target_dataframe = dataframe if inplace else dataframe.copy()
    cleaned_column = output_column or text_column
    target_dataframe[cleaned_column] = target_dataframe[text_column].map(clean_rag_text)
    return target_dataframe


def load_nodes(
    data_path: str | Path,
    node_types: Sequence[str] | None = None,
    limit: int | None = None,
    clean_text: bool = True,
) -> list[RagNode]:
    with Path(data_path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)
    return nodes_from_dict(payload, node_types=node_types, limit=limit, clean_text=clean_text)


def nodes_from_dict(
    payload: dict[str, Any],
    node_types: Sequence[str] | None = None,
    limit: int | None = None,
    clean_text: bool = True,
) -> list[RagNode]:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("Input JSON must contain a nodes list.")

    wanted_node_types = set(node_types) if node_types else None
    recommendation_edges = _recommendation_edges_by_node(payload)

    nodes: list[RagNode] = []
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            raise ValueError(f"Node at index {index} must be an object.")

        node_id = _required_string(raw_node, "id", index)
        node_type = _required_string(raw_node, "type", index)
        if wanted_node_types is not None and node_type not in wanted_node_types:
            continue

        metadata = {key: value for key, value in raw_node.items() if key != "text_for_embedding"}
        metadata["recommendation_edges"] = recommendation_edges.get(node_id, [])

        nodes.append(
            RagNode(
                node_id=node_id,
                node_type=node_type,
                text=_embedding_text(raw_node, index, clean_text=clean_text),
                metadata=metadata,
            )
        )
        if limit is not None and len(nodes) >= limit:
            break

    return nodes


def build_points(
    data_path: str | Path,
    embedding_model_name: str,
    node_types: Sequence[str] | None = None,
    limit: int | None = None,
    clean_text: bool = True,
) -> tuple[list[models.PointStruct], int]:
    nodes = load_nodes(data_path, node_types=node_types, limit=limit, clean_text=clean_text)
    texts = [node.text for node in nodes]
    if not texts:
        return [], 0

    embedding_model = TextEmbedding(model_name=embedding_model_name)
    # 기본 batch(256)는 다국어 mpnet(768차원)에서 512토큰 패딩과 겹쳐 메모리 스파이크로 컨테이너가
    # OOM(137) 죽을 수 있어, 색인은 소배치로 나눠 임베딩한다(속도보다 안정성 우선 — 색인은 배치 작업).
    vectors = [_as_float_list(vector) for vector in embedding_model.embed(texts, batch_size=32)]
    if not vectors:
        return [], 0

    points = []
    for node, vector in zip(nodes, vectors, strict=True):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"campaign-user-rag:{node.node_type}:{node.node_id}"))
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload=_point_payload(node),
            )
        )
    return points, len(vectors[0])


def upsert_points(
    client: QdrantClient,
    collection_name: str,
    points: list[models.PointStruct],
    vector_size: int,
    recreate: bool,
) -> None:
    if recreate and client.collection_exists(collection_name):
        client.delete_collection(collection_name)

    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )

    if points:
        client.upsert(collection_name=collection_name, points=points)


def _recommendation_edges_by_node(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_edges = payload.get("recommendation_edges", [])
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("recommendation_edges must be a list when provided.")

    edges_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, dict):
            raise ValueError(f"Recommendation edge at index {index} must be an object.")

        for node_key in ("user_id", "campaign_id"):
            node_id = raw_edge.get(node_key)
            if isinstance(node_id, str) and node_id.strip():
                edges_by_node[node_id].append(raw_edge)

    return dict(edges_by_node)


def _embedding_text(raw_node: dict[str, Any], index: int, clean_text: bool) -> str:
    text = raw_node.get("text_for_embedding")
    if isinstance(text, str) and text.strip():
        return clean_rag_text(text) if clean_text else text.strip()

    fallback_text = _metadata_text(raw_node)
    if fallback_text:
        return clean_rag_text(fallback_text) if clean_text else fallback_text

    raise ValueError(f"Node at index {index} must contain text_for_embedding or metadata text.")


def _normalize_whitespace(text: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _deduplicate_sentences(text: str) -> str:
    seen_sentences: set[str] = set()
    deduplicated_sentences: list[str] = []

    for match in SENTENCE_PATTERN.finditer(text):
        sentence = _normalize_whitespace(match.group(0))
        if not sentence:
            continue

        sentence_key = sentence.casefold().rstrip(".!?。！？")
        if sentence_key in seen_sentences:
            continue

        seen_sentences.add(sentence_key)
        deduplicated_sentences.append(sentence)

    return " ".join(deduplicated_sentences) if deduplicated_sentences else text


def _metadata_text(raw_node: dict[str, Any]) -> str:
    parts = []
    for key, value in raw_node.items():
        if key == "text_for_embedding":
            continue
        rendered_value = _render_value(value)
        if rendered_value:
            parts.append(f"{key}: {rendered_value}")
    return ". ".join(parts)


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list):
        rendered_items = [_render_value(item) for item in value]
        return ", ".join(item for item in rendered_items if item)
    return ""


def _point_payload(node: RagNode) -> dict[str, Any]:
    payload = {
        "text": node.text,
        "node_id": node.node_id,
        "node_type": node.node_type,
        "source": node.metadata,
    }
    for key, value in node.metadata.items():
        if key not in {"id", "type"}:
            payload[key] = value
    return payload


def _required_string(raw_node: dict[str, Any], field_name: str, index: int) -> str:
    value = raw_node.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Node at index {index} must contain a non-empty {field_name} string.")
    return value.strip()


def _as_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return list(vector)


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be greater than 0.")


def _node_summary(nodes: Iterable[RagNode]) -> dict[str, Any]:
    node_list = list(nodes)
    node_type_counts = Counter(node.node_type for node in node_list)
    return {
        "node_count": len(node_list),
        "node_types": dict(sorted(node_type_counts.items())),
        "sample_ids": [node.node_id for node in node_list[:5]],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index campaign and user RAG nodes into Qdrant.")
    parser.add_argument("data", help="Path to the campaign/user nodes JSON file.")
    parser.add_argument(
        "--url",
        default=os.getenv("QDRANT_URL", "http://localhost:6333"),
        help="Qdrant URL. Defaults to QDRANT_URL or http://localhost:6333.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("QDRANT_API_KEY"),
        help="Qdrant API key. Defaults to QDRANT_API_KEY.",
    )
    parser.add_argument(
        "--collection",
        default=os.getenv("QDRANT_RAG_COLLECTION", DEFAULT_COLLECTION),
        help=f"Qdrant collection name. Defaults to {DEFAULT_COLLECTION}.",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("QDRANT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        help=f"FastEmbed model name. Defaults to {DEFAULT_EMBEDDING_MODEL}.",
    )
    parser.add_argument(
        "--node-type",
        action="append",
        help="Only index nodes with this type. Can be specified multiple times.",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N matching nodes.")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate the collection.")
    parser.add_argument("--dry-run", action="store_true", help="Build vectors but do not write to Qdrant.")
    parser.add_argument("--validate-only", action="store_true", help="Validate and summarize input JSON only.")
    parser.add_argument("--no-clean-text", action="store_true", help="Skip RAG text preprocessing before embedding.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _validate_limit(args.limit)

    if args.validate_only:
        nodes = load_nodes(
            args.data,
            node_types=args.node_type,
            limit=args.limit,
            clean_text=not args.no_clean_text,
        )
        print(json.dumps(_node_summary(nodes), ensure_ascii=False, indent=2))
        return

    points, vector_size = build_points(
        args.data,
        args.embedding_model,
        node_types=args.node_type,
        limit=args.limit,
        clean_text=not args.no_clean_text,
    )
    if vector_size == 0:
        raise ValueError("No nodes were found to index.")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "point_count": len(points),
                    "vector_size": vector_size,
                    "collection": args.collection,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    client = QdrantClient(url=args.url, api_key=args.api_key)
    upsert_points(client, args.collection, points, vector_size, args.recreate)
    print(
        f"Indexed {len(points)} campaign/user nodes into Qdrant collection "
        f"'{args.collection}' at {args.url}."
    )


if __name__ == "__main__":
    main()