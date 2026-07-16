from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from build_rag_knowledge import build_payload, load_json, save_json
from rag_index import DEFAULT_EMBEDDING_MODEL, build_points, load_nodes, upsert_points


DEFAULT_USER_DATA = Path("docs/data/campaign_user_rag_sample_50_with_edges.json")
DEFAULT_SCHEMA = Path("docs/data/schema_catalog.json")
DEFAULT_NORMALIZATION = Path("docs/data/normalization_rules.sample.json")
DEFAULT_BUSINESS_POLICIES = Path("docs/data/business_policies.sample.json")
DEFAULT_METRIC_LEXICON = Path("docs/data/metric_lexicon.sample.json")
DEFAULT_SQL_EXAMPLES = Path("docs/data/sql_examples.sample.sql")
DEFAULT_KNOWLEDGE_DATA = Path("docs/data/rag_knowledge_base.json")
DEFAULT_USER_COLLECTION = "campaign_user_rag_nodes"
DEFAULT_KNOWLEDGE_COLLECTION = "campaign_knowledge_rag"


def rebuild_knowledge_base(
    schema_path: Path,
    normalization_path: Path,
    business_policies_path: Path,
    metric_lexicon_path: Path,
    sql_examples_path: Path,
    output_path: Path,
    campaign_user_path: Path | None = None,
) -> dict[str, Any]:
    payload = build_payload(
        schema_catalog=load_json(schema_path),
        normalization_payload=load_json(normalization_path),
        policy_payload=load_json(business_policies_path) if business_policies_path.exists() else None,
        metric_lexicon_payload=load_json(metric_lexicon_path) if metric_lexicon_path.exists() else None,
        sql_text=sql_examples_path.read_text(encoding="utf-8"),
        campaign_user_payload=(
            load_json(campaign_user_path) if campaign_user_path and campaign_user_path.exists() else None
        ),
    )
    save_json(output_path, payload)
    return payload["node_counts"]


def index_collection(
    client: QdrantClient | None,
    data_path: Path,
    collection_name: str,
    embedding_model: str,
    recreate: bool,
    dry_run: bool,
    validate_only: bool,
    clean_text: bool,
) -> dict[str, Any]:
    if validate_only:
        nodes = load_nodes(data_path, clean_text=clean_text)
        return {
            "collection": collection_name,
            "data": str(data_path),
            "mode": "validate_only",
            "node_count": len(nodes),
            "node_types": dict(sorted(Counter(node.node_type for node in nodes).items())),
            "sample_ids": [node.node_id for node in nodes[:5]],
        }

    points, vector_size = build_points(data_path, embedding_model, clean_text=clean_text)
    if vector_size == 0:
        raise ValueError(f"No nodes were found to index from {data_path}.")

    if dry_run:
        return {
            "collection": collection_name,
            "data": str(data_path),
            "mode": "dry_run",
            "point_count": len(points),
            "vector_size": vector_size,
            "recreate": recreate,
        }

    if client is None:
        raise ValueError("Qdrant client is required unless --dry-run or --validate-only is used.")

    upsert_points(client, collection_name, points, vector_size, recreate)
    return {
        "collection": collection_name,
        "data": str(data_path),
        "mode": "indexed",
        "point_count": len(points),
        "vector_size": vector_size,
        "recreate": recreate,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize both campaign RAG Qdrant collections.")
    parser.add_argument("--url", default=os.getenv("QDRANT_URL", "http://localhost:6333"), help="Qdrant URL.")
    parser.add_argument("--api-key", default=os.getenv("QDRANT_API_KEY"), help="Qdrant API key.")
    parser.add_argument("--embedding-model", default=os.getenv("QDRANT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL), help="FastEmbed model name.")
    parser.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA, help="Campaign/user RAG JSON path.")
    parser.add_argument("--knowledge-data", type=Path, default=DEFAULT_KNOWLEDGE_DATA, help="Generated knowledge RAG JSON path.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="Schema catalog JSON path.")
    parser.add_argument("--normalization", type=Path, default=DEFAULT_NORMALIZATION, help="Normalization dictionary JSON path.")
    parser.add_argument("--business-policies", type=Path, default=DEFAULT_BUSINESS_POLICIES, help="Business policy JSON path.")
    parser.add_argument("--metric-lexicon", type=Path, default=DEFAULT_METRIC_LEXICON, help="Metric alias JSON path for computed formula parsing.")
    parser.add_argument("--sql-examples", type=Path, default=DEFAULT_SQL_EXAMPLES, help="SQL examples file path.")
    parser.add_argument("--user-collection", default=os.getenv("QDRANT_RAG_COLLECTION", DEFAULT_USER_COLLECTION), help="Campaign/user collection name.")
    parser.add_argument("--knowledge-collection", default=os.getenv("QDRANT_GRAPH_COLLECTION", DEFAULT_KNOWLEDGE_COLLECTION), help="Knowledge graph collection name.")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate both collections before indexing.")
    parser.add_argument("--skip-knowledge-build", action="store_true", help="Use the existing knowledge JSON without regenerating it.")
    parser.add_argument("--dry-run", action="store_true", help="Build vectors but do not write to Qdrant.")
    parser.add_argument("--validate-only", action="store_true", help="Validate and summarize both input JSON files without embedding or writing.")
    parser.add_argument("--no-clean-text", action="store_true", help="Skip text cleanup before embedding.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    knowledge_build = None
    if not args.skip_knowledge_build and not args.validate_only:
        knowledge_build = rebuild_knowledge_base(
            schema_path=args.schema,
            normalization_path=args.normalization,
            business_policies_path=args.business_policies,
            metric_lexicon_path=args.metric_lexicon,
            sql_examples_path=args.sql_examples,
            output_path=args.knowledge_data,
            campaign_user_path=args.user_data,
        )

    client = None if args.dry_run or args.validate_only else QdrantClient(url=args.url, api_key=args.api_key)
    clean_text = not args.no_clean_text
    collections = [
        index_collection(
            client=client,
            data_path=args.user_data,
            collection_name=args.user_collection,
            embedding_model=args.embedding_model,
            recreate=args.recreate,
            dry_run=args.dry_run,
            validate_only=args.validate_only,
            clean_text=clean_text,
        ),
        index_collection(
            client=client,
            data_path=args.knowledge_data,
            collection_name=args.knowledge_collection,
            embedding_model=args.embedding_model,
            recreate=args.recreate,
            dry_run=args.dry_run,
            validate_only=args.validate_only,
            clean_text=clean_text,
        ),
    ]
    print(
        json.dumps(
            {
                "qdrant_url": args.url,
                "knowledge_build": knowledge_build or "skipped",
                "collections": collections,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()