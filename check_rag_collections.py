from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from init_rag_collections import (
    DEFAULT_KNOWLEDGE_COLLECTION,
    DEFAULT_KNOWLEDGE_DATA,
    DEFAULT_USER_COLLECTION,
    DEFAULT_USER_DATA,
)
from rag_index import load_nodes


REQUIRED_PAYLOAD_FIELDS = {"node_id", "node_type", "text", "source"}


def expected_collection_summary(data_path: Path, clean_text: bool) -> dict[str, Any]:
    nodes = load_nodes(data_path, clean_text=clean_text)
    return {
        "data": str(data_path),
        "expected_points": len(nodes),
        "expected_node_types": dict(sorted(Counter(node.node_type for node in nodes).items())),
    }


def collection_status(
    client: QdrantClient,
    collection_name: str,
    data_path: Path,
    clean_text: bool,
    sample_limit: int,
) -> dict[str, Any]:
    expected = expected_collection_summary(data_path, clean_text=clean_text)
    issues: list[dict[str, str]] = []

    if not client.collection_exists(collection_name):
        issues.append({"code": "collection_missing", "severity": "error", "message": "Qdrant collection does not exist."})
        return {
            "collection": collection_name,
            **expected,
            "exists": False,
            "actual_points": None,
            "vector_size": None,
            "distance": None,
            "sample_payloads": [],
            "is_healthy": False,
            "issues": issues,
        }

    info = client.get_collection(collection_name)
    actual_points = _count_points(client, collection_name, info)
    vector_size, distance = _vector_config(info)
    sample_payloads, sample_issues = _sample_payloads(client, collection_name, sample_limit)
    issues.extend(sample_issues)

    if actual_points != expected["expected_points"]:
        issues.append(
            {
                "code": "point_count_mismatch",
                "severity": "error",
                "message": f"Expected {expected['expected_points']} points but found {actual_points}.",
            }
        )

    if vector_size is None:
        issues.append({"code": "vector_size_missing", "severity": "error", "message": "Vector size could not be read."})

    return {
        "collection": collection_name,
        **expected,
        "exists": True,
        "actual_points": actual_points,
        "vector_size": vector_size,
        "distance": distance,
        "sample_payloads": sample_payloads,
        "is_healthy": not any(issue["severity"] == "error" for issue in issues),
        "issues": issues,
    }


def check_collections(args: argparse.Namespace) -> dict[str, Any]:
    client = QdrantClient(url=args.url, api_key=args.api_key)
    clean_text = not args.no_clean_text
    collections = [
        collection_status(
            client=client,
            collection_name=args.user_collection,
            data_path=args.user_data,
            clean_text=clean_text,
            sample_limit=args.sample_limit,
        ),
        collection_status(
            client=client,
            collection_name=args.knowledge_collection,
            data_path=args.knowledge_data,
            clean_text=clean_text,
            sample_limit=args.sample_limit,
        ),
    ]
    return {
        "qdrant_url": args.url,
        "is_healthy": all(collection["is_healthy"] for collection in collections),
        "collections": collections,
    }


def _count_points(client: QdrantClient, collection_name: str, info: Any) -> int | None:
    count_result = client.count(collection_name=collection_name, exact=True)
    count = getattr(count_result, "count", None)
    if isinstance(count, int):
        return count

    points_count = getattr(info, "points_count", None)
    return points_count if isinstance(points_count, int) else None


def _vector_config(info: Any) -> tuple[int | dict[str, int] | None, str | dict[str, str] | None]:
    config = getattr(info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None:
        return None, None

    if isinstance(vectors, dict):
        sizes: dict[str, int] = {}
        distances: dict[str, str] = {}
        for name, vector_params in vectors.items():
            size = getattr(vector_params, "size", None)
            distance = getattr(vector_params, "distance", None)
            if isinstance(size, int):
                sizes[name] = size
            if distance is not None:
                distances[name] = str(distance)
        return sizes or None, distances or None

    size = getattr(vectors, "size", None)
    distance = getattr(vectors, "distance", None)
    return size if isinstance(size, int) else None, str(distance) if distance is not None else None


def _sample_payloads(
    client: QdrantClient,
    collection_name: str,
    sample_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if sample_limit < 1:
        return [], []

    points, _ = client.scroll(
        collection_name=collection_name,
        limit=sample_limit,
        with_payload=True,
        with_vectors=False,
    )
    samples = []
    issues: list[dict[str, str]] = []
    for point in points:
        payload = getattr(point, "payload", None) or {}
        missing_fields = sorted(REQUIRED_PAYLOAD_FIELDS - set(payload))
        if missing_fields:
            issues.append(
                {
                    "code": "payload_fields_missing",
                    "severity": "error",
                    "message": f"Sample point is missing payload fields: {', '.join(missing_fields)}.",
                }
            )
        samples.append(
            {
                "id": str(getattr(point, "id", "")),
                "node_id": payload.get("node_id"),
                "node_type": payload.get("node_type"),
                "payload_keys": sorted(payload),
            }
        )
    return samples, issues


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Qdrant RAG collection state after indexing.")
    parser.add_argument("--url", default=os.getenv("QDRANT_URL", "http://localhost:6333"), help="Qdrant URL.")
    parser.add_argument("--api-key", default=os.getenv("QDRANT_API_KEY"), help="Qdrant API key.")
    parser.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA, help="Campaign/user RAG JSON path.")
    parser.add_argument("--knowledge-data", type=Path, default=DEFAULT_KNOWLEDGE_DATA, help="Generated knowledge RAG JSON path.")
    parser.add_argument("--user-collection", default=os.getenv("QDRANT_RAG_COLLECTION", DEFAULT_USER_COLLECTION), help="Campaign/user collection name.")
    parser.add_argument("--knowledge-collection", default=os.getenv("QDRANT_GRAPH_COLLECTION", DEFAULT_KNOWLEDGE_COLLECTION), help="Knowledge graph collection name.")
    parser.add_argument("--sample-limit", type=int, default=3, help="Number of sample payloads to inspect per collection.")
    parser.add_argument("--no-clean-text", action="store_true", help="Use the same no-clean-text mode as indexing when computing expected input counts.")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 when any collection check fails.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = check_collections(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.strict and not result["is_healthy"]:
        sys.exit(1)


if __name__ == "__main__":
    main()