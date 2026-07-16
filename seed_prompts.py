"""docs/prompts 파일을 DB(campaign_prompt_templates)로 시딩하는 일회성 스크립트.

사용 예:
    python seed_prompts.py

DB 접속은 prompt_store가 참조하는 POSTGRES_* 환경변수를 그대로 사용한다.
프롬프트 디렉터리는 GRAPH_RAG_PROMPT_DIR(기본 docs/prompts)로 지정한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import prompt_store
from graph_rag import DEFAULT_PROMPT_DIR


def main() -> None:
    prompt_dir = Path(os.getenv("GRAPH_RAG_PROMPT_DIR", str(DEFAULT_PROMPT_DIR)))
    seeded = prompt_store.seed_from_dir(prompt_dir)
    print(f"Seeded {len(seeded)} prompt templates from {prompt_dir}:")
    for item in seeded:
        print(f"  - {item['name']}")


if __name__ == "__main__":
    main()
