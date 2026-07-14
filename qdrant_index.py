from __future__ import annotations

import argparse
import os
import uuid
from typing import Any

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from ingest import NormalizationIngester


DEFAULT_COLLECTION = "campaign_normalization_terms"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def build_points(rules_path: str, embedding_model_name: str) -> tuple[list[models.PointStruct], int]:
    ingester = NormalizationIngester.from_file(rules_path)
    index = ingester.to_index()
    terms: list[dict[str, Any]] = index["terms"]
    texts = [_embedding_text(term) for term in terms]

    embedding_model = TextEmbedding(model_name=embedding_model_name)
    vectors = [_as_float_list(vector) for vector in embedding_model.embed(texts)]
    if not vectors:
        return [], 0

    points = []
    for term, text, vector in zip(terms, texts, vectors, strict=True):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{term['rule_id']}:{term['lookup_key']}"))
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": text,
                    "term": term["term"],
                    "lookup_key": term["lookup_key"],
                    "canonical": term["canonical"],
                    "rule_id": term["rule_id"],
                    "ko_label": term["ko_label"],
                    "match_type": term["match_type"],
                },
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


def _embedding_text(term: dict[str, Any]) -> str:
    return " | ".join(
        part
        for part in (
            term["term"],
            term["canonical"],
            term.get("ko_label"),
            term["match_type"],
        )
        if part
    )


def _as_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return list(vector)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index normalization dictionary terms into Qdrant.")
    parser.add_argument("rules", help="Path to the normalization rules JSON file.")
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
        default=os.getenv("QDRANT_COLLECTION", DEFAULT_COLLECTION),
        help=f"Qdrant collection name. Defaults to {DEFAULT_COLLECTION}.",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("QDRANT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        help=f"FastEmbed model name. Defaults to {DEFAULT_EMBEDDING_MODEL}.",
    )
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate the collection.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    points, vector_size = build_points(args.rules, args.embedding_model)
    if vector_size == 0:
        raise ValueError("No terms were found to index.")

    client = QdrantClient(url=args.url, api_key=args.api_key)
    upsert_points(client, args.collection, points, vector_size, args.recreate)
    print(
        f"Indexed {len(points)} terms into Qdrant collection '{args.collection}' "
        f"at {args.url}."
    )


if __name__ == "__main__":
    main()