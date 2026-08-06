"""실측 결과(JSON) → 커밋되는 귀결 기준선 갱신.

기준선은 **손으로 적는 숫자가 아니다**. 재현할 수 없는 수치가 기준선 노릇을 하던 상태로
돌아가지 않게 하려면, 갱신도 실측 산출물에서 기계적으로 나와야 한다
(:mod:`tools.live_prompt_baseline` 의 ``--json`` 결과가 유일한 입력이다).

    python tools/update_live_prompt_baseline.py artifacts/measure/run_after.json --note "..."

행의 ``reason`` 은 첫 관측의 사유를 그대로 싣는다 — 요약하거나 다듬지 않는다. 사유를 사람이
고쳐 쓰기 시작하면 기준선이 다시 '기억'이 된다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "docs" / "data" / "test_baselines" / "live_prompt_outcomes_baseline.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_rows(measured: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in measured.get("rows") or []:
        observations = row.get("observations") or []
        first = observations[0] if observations else {}
        entry: dict[str, Any] = {"outcome": row["outcome"]}
        reason = first.get("reason")
        if reason:
            entry["reason"] = reason
        if row.get("unstable"):
            entry["unstable"] = True
        if row.get("semantic_unstable"):
            entry["semantic_unstable"] = True
        rows[str(row["id"])] = entry
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measured", type=Path, help="live_prompt_baseline.py --json 산출물")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--note", default="", help="기준선 헤더에 남길 측정 맥락")
    args = parser.parse_args()

    measured = json.loads(args.measured.read_text(encoding="utf-8"))
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.baseline.exists()
        else {}
    )
    rows = build_rows(measured)
    if not rows:
        raise SystemExit("측정 산출물에 행이 없다 — 갱신하지 않는다.")

    previous = baseline.get("rows") or {}
    changed = sorted(
        key for key in rows if (previous.get(key) or {}).get("outcome") != rows[key]["outcome"]
    )
    baseline["rows"] = rows
    baseline["summary"] = measured.get("summary", {}).get("outcomes", {})
    if args.note:
        baseline["note"] = args.note
    args.baseline.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"갱신: {args.baseline} ({len(rows)}행, 귀결 변화 {len(changed)}건)")
    if changed:
        print("변화한 id: " + ", ".join(f"#{key}" for key in changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
