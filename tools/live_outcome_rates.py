"""한 프롬프트를 N회 돌려 **귀결의 흔들림**을 센다.

라이브 LLM 호출은 결정론이 아니다 — 같은 코드로 같은 원문을 돌려도 회차마다 다른 귀결이 나온다
(실측 2026-08-07: 12회에 네 가지). 그래서 한 번 성공했다는 사실도, 한 번 실패했다는 사실도
회귀 판정의 근거가 될 수 없다. 판정의 단위는 **비율**이어야 한다.

세는 축은 넷이고, 뒤의 둘은 목표가 0 이다::

    executable        SQL 이 나왔다
    false_unsupported 낮출 수 있는데 '표현할 수 없다'로 끝났다
    false_registry_gap 낮출 수 있는데 '선언된 자산은 있으나…'로 끝났다
    emission_failure  지원되는 의미인데 표현이 확정되지 않았다(재시도 소진)
    ambiguous         원문 결핍을 사용자에게 되물었다

``emission_failure`` 와 ``ambiguous`` 를 가르는 이유는 고칠 곳이 다르기 때문이다 — 앞은 방출
품질(우리 쪽), 뒤는 원문 결핍(사용자 쪽)이다. 한 칸으로 뭉치면 개선의 방향이 보이지 않는다.

'false' 판정의 근거는 사람의 눈이 아니라 :mod:`lowering_planner` 다 — 그 원문에 실제로 낮출 수
있는 계획이 있는데도 미지원/registry gap 으로 끝났다면 거짓이다.

    python tools/live_outcome_rates.py --id 12 --runs 10
    python tools/live_outcome_rates.py --prompt "2026년 2월과 3월의 구매금액이 증가한 회원" --runs 6
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "docs" / "data" / "test_baselines" / "live_prompts.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGISTRY_GAP = "semantic_registry_gap"
EMISSION_FAILURE = "semantic_emission_failure"


def call(base_url: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = json.dumps({
        "prompt": prompt,
        "query_parser": "auto",
        "execute_sql": False,
        "persist_targeting": False,
        "generate_messages": False,
        "include_debug": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/target-sql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def classify(response: dict[str, Any], *, lowerable: bool) -> str:
    """이 회차의 귀결 한 가지. ``lowerable`` 은 애플리케이션이 계산한 사실이지 판단이 아니다."""
    if response.get("sql"):
        return "executable"
    failure_reason = response.get("failure_reason")
    status = response.get("status")
    if failure_reason == REGISTRY_GAP:
        return "false_registry_gap" if lowerable else "registry_gap"
    if failure_reason == EMISSION_FAILURE:
        return "emission_failure"
    if status == "unsupported":
        return "false_unsupported" if lowerable else "unsupported"
    if status == "needs_clarification":
        return "ambiguous"
    return f"other:{status}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--id", type=int, default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()

    prompt = args.prompt
    if prompt is None:
        if args.id is None:
            raise SystemExit("--id 또는 --prompt 가 필요하다")
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))["prompts"]
        entry = next((item for item in corpus if item.get("id") == args.id), None)
        if entry is None:
            raise SystemExit(f"코퍼스에 id={args.id} 가 없다")
        prompt = str(entry["text"])

    import lowering_planner

    plans = lowering_planner.plans_for_query(prompt)
    lowerable = bool(plans)
    print(f"프롬프트: {prompt}")
    print(f"낮출 수 있는 계획: {len(plans)}개 " + (f"({plans[0].obligation.kind})" if plans else ""))
    print()

    tally: Counter[str] = Counter()
    for index in range(args.runs):
        try:
            response = call(args.base_url, prompt, args.timeout)
        except Exception as exc:  # noqa: BLE001 — 네트워크 실패도 한 회차의 관측이다.
            tally["call_failed"] += 1
            print(f"  {index + 1:>3}. call_failed ({exc.__class__.__name__})")
            continue
        outcome = classify(response, lowerable=lowerable)
        tally[outcome] += 1
        print(f"  {index + 1:>3}. {outcome}")

    print()
    total = max(sum(tally.values()), 1)
    for outcome, count in tally.most_common():
        print(f"  {outcome:<22} {count:>3}/{total}  {count / total:6.1%}")
    print()
    for axis in ("false_unsupported", "false_registry_gap"):
        print(f"  목표 0 → {axis}: {tally[axis]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
