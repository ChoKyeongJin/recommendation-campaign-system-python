"""단일 프롬프트 진단 프로브 — `/target-sql` 응답에서 '왜 이 귀결인가'만 뽑아 본다.

러너(:mod:`tools.live_prompt_baseline`)는 86건의 **귀결 종류**를 세는 도구라 한 건을 파고들 때
쓰기엔 출력이 너무 얕다. 이 프로브는 반대다 — 한 건의 실패 좌표(단계·코드·요구사항·정책 결정)를
전부 펼친다.

    python tools/probe_target_sql.py --id 71
    python tools/probe_target_sql.py --prompt "최근 30일 구매한 회원 수를 알려줘"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "data" / "test_baselines" / "live_prompts.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def call(base_url: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = json.dumps(
        {
            "prompt": prompt,
            "query_parser": "auto",
            "execute_sql": False,
            "persist_targeting": False,
            "generate_messages": False,
            "include_debug": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/target-sql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


_INTERESTING = (
    "status",
    "failure_reason",
    "unsupported_reason",
    "interpretation_status",
    "failure_stage",
    "audience_diagnosis",
    "clarification_questions",
    "missing_input_conditions",
    "unsupported_conditions",
    "semantic_ir",
    "requirement_coverage",
    "policy_decisions",
    "result_shape",
    "semantic_fingerprint",
    "execution_fingerprint",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--id", type=int, default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--full", action="store_true", help="응답 전체를 그대로 출력")
    args = parser.parse_args()

    prompt = args.prompt
    if prompt is None:
        if args.id is None:
            raise SystemExit("--id 또는 --prompt 가 필요하다")
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))["prompts"]
        entry = next((item for item in corpus if item.get("id") == args.id), None)
        if entry is None:
            raise SystemExit(f"코퍼스에 #{args.id} 가 없다")
        prompt = entry["text"]

    response = call(args.base_url, prompt, args.timeout)
    if args.full:
        print(json.dumps(response, ensure_ascii=False, indent=1, default=str))
        return 0

    print(f"prompt: {prompt}")
    print(f"sql:\n{response.get('sql')}")
    for key in _INTERESTING:
        if key in response:
            print(f"--- {key}: {json.dumps(response[key], ensure_ascii=False, default=str)[:2200]}")
    debug = response.get("debug") or {}
    for key in ("sql_provenance", "unresolved_source_conditions", "plan_resolution"):
        if key in debug:
            print(f"--- debug.{key}: {json.dumps(debug[key], ensure_ascii=False, default=str)[:2200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
