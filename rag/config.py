"""기본 경로·모델·컬렉션 등 실행 설정의 단일 소스 — 최하위 리프 계층.

graph_rag.py 에서 분리했다. 여기 있는 것은 "코드가 파일·모델·컬렉션을 어디서 찾는가"의
기본값뿐이다. env 로 덮어쓸 수 있는 것과 그렇지 않은 것이 한눈에 구분되도록 모아 뒀다.

이 모듈이 분리돼야 하는 이유: 상위 모듈(rag.message 등)이 기본 경로 상수 하나 때문에
graph_rag 를 되돌아 import 하면 분할이 순환으로 퇴화한다. 경로 상수는 어떤 도메인 판단도
담지 않으므로 바닥에 두는 것이 맞다.

경로가 상대경로인 것은 의도다 — 호출자가 cwd 를 레포 루트로 두는 실행 관례를 유지한다.
(레지스트리 로더의 cwd 독립 절대경로화는 별개 축이고 각 로더가 스스로 처리한다.)

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 프로젝트 의존이 없다(stdlib 전용).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# ── 지식/정책 데이터 (env 덮어쓰기 없음 — 레포에 동봉된 자산) ──────────────────────
DEFAULT_DATA_PATH = Path("docs/data/generated/rag_knowledge_base.json")
DEFAULT_NORMALIZATION_PATH = Path("docs/data/runtime/language/normalization_rules.sample.json")
DEFAULT_POLICY_PATH = Path("docs/data/runtime/policies/business_policies.sample.json")
DEFAULT_DIMENSION_CATALOG_PATH = Path("docs/data/generated/dimension_catalog.sample.json")

# ── 검색 백엔드 ──────────────────────────────────────────────────────────────────
DEFAULT_COLLECTION = "campaign_knowledge_rag"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# ── env 로 덮어쓰는 것들 (배포 환경마다 달라진다) ─────────────────────────────────
DEFAULT_LLM_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_PROMPT_DIR = Path(os.getenv("GRAPH_RAG_PROMPT_DIR", "docs/prompts"))
DEFAULT_MESSAGE_POLICY_PATH = Path(os.getenv("GRAPH_RAG_MESSAGE_POLICY", "docs/policies/message-policy.json"))


def _load_business_policies(business_policies: Path | None) -> list[dict[str, Any]]:
    if business_policies is None or not business_policies.exists():
        return []
    payload = json.loads(business_policies.read_text(encoding="utf-8"))
    policies = payload.get("policies", [])
    return [policy for policy in policies if isinstance(policy, dict) and policy.get("policy_id") and policy.get("canonical")]
