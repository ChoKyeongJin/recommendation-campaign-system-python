"""docs/policies 정책 파일을 DB(campaign_policies)로 시딩하는 일회성 스크립트.

사용 예:
    python seed_policies.py                 # DB로 관리되는 정책만 시딩(기본)
    python seed_policies.py --all           # docs/policies/*.json 전체 시딩

DB 접속은 policy_store가 참조하는 POSTGRES_* 환경변수를 그대로 사용한다.
정책 디렉터리는 CAMPAIGN_POLICY_DIR(기본 docs/policies)로 지정한다.

기본 동작은 현재 DB 로더가 연결된 정책(``DB_MANAGED_POLICY_NAMES``)만 시딩한다.
아직 로더가 연결되지 않은 다른 정책까지 함께 넣으려면 ``--all``을 사용한다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import policy_store

DEFAULT_POLICY_DIR = Path(__file__).resolve().parent / "docs" / "policies"
# api._load_* 로더가 DB에서 조회하는 정책 이름과 동일해야 한다.
DB_MANAGED_POLICY_NAMES = [
    "ctr-model-policy",
    "heuristic-ctr-rules",
]


def _seed_named(policy_dir: Path, names: list[str]) -> list[dict[str, object]]:
    seeded: list[dict[str, object]] = []
    for name in names:
        path = policy_dir / f"{name}.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        seeded.append(policy_store.upsert(name, content))
    return seeded


def main() -> None:
    policy_dir = Path(os.getenv("CAMPAIGN_POLICY_DIR", str(DEFAULT_POLICY_DIR)))
    seed_all = "--all" in sys.argv[1:]

    if seed_all:
        seeded = policy_store.seed_from_dir(policy_dir)
    else:
        seeded = _seed_named(policy_dir, DB_MANAGED_POLICY_NAMES)

    print(f"Seeded {len(seeded)} policies from {policy_dir}:")
    for item in seeded:
        print(f"  - {item['name']}")


if __name__ == "__main__":
    main()
