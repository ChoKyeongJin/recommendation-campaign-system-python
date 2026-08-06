"""같은 프롬프트 두 실행의 **의미 지문 입력**을 직접 비교한다.

지문이 갈렸는데 SQL 이 바이트 동일하면 둘 중 하나다 — 정규화가 놓친 휘발성 값이 있거나,
정말 뜻이 달랐는데 우연히 같은 SQL 이 됐거나. 응답만 봐서는 구별할 수 없으므로 여기서
구조화기를 직접 두 번 불러 canonical IR 을 diff 한다.

    python tools/diff_semantic_fingerprint.py "최근 30일 구매한 회원 수를 알려줘"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import graph_rag  # noqa: E402
import semantic_fingerprint  # noqa: E402


def _canonical(payload: object) -> object:
    return semantic_fingerprint._canonical(payload)


def _walk_diff(left: object, right: object, path: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        out: list[str] = []
        for key in sorted(set(left) | set(right)):
            out.extend(_walk_diff(left.get(key), right.get(key), f"{path}.{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: 길이 {len(left)} != {len(right)}"]
        out = []
        for index, (a, b) in enumerate(zip(left, right)):
            out.extend(_walk_diff(a, b, f"{path}[{index}]"))
        return out
    return [] if left == right else [f"{path}: {left!r} != {right!r}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--current-date", default=None)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    args = parser.parse_args()

    # 구조화기 진입점과 요청 문맥은 실행 경로가 소유한다 — 여기서 두 번째 조립 경로를 만들면
    # 재현 대상이 실제 실행 경로와 달라진다.
    import api

    context = api._request_structuring_context()
    plans = [
        graph_rag._structure_campaign_query_plan_v4(args.prompt, context, args.model)
        for _ in range(2)
    ]
    fingerprints = [
        semantic_fingerprint.semantic_fingerprint(
            canonical_ir=plan.get("event_expression") or plan.get("audience_requirement")
        )
        for plan in plans
    ]
    print("fingerprints:", fingerprints)
    if fingerprints[0] == fingerprints[1]:
        print("동일 — 정규화가 두 방출을 같은 뜻으로 접었다.")
        return 0
    left, right = (
        _canonical(plan.get("event_expression") or plan.get("audience_requirement"))
        for plan in plans
    )
    print("정규화 후에도 남은 차이:")
    for line in _walk_diff(left, right) or ["(구조 차이 없음 — 상위 키 구성이 다르다)"]:
        print("  " + line)
    print("\n좌:", json.dumps(left, ensure_ascii=False)[:1200])
    print("\n우:", json.dumps(right, ensure_ascii=False)[:1200])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
