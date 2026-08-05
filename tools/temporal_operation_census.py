"""라이브 코퍼스를 **의미 연산으로** 분류한다(문장 번호가 아니라).

왜 필요한가
-----------
"어떤 문장이 되고 어떤 문장이 안 되는가"를 문장 목록으로 관리하면 목록이 곧 사양이 되고,
새 문장이 올 때마다 목록이 한 줄 는다. 재야 하는 것은 문장이 아니라 **의미 연산의 커버리지**다:
어떤 시간 연산이 몇 개의 요청에 등장하고, 그중 몇 개가 컴파일되며, 못 되는 것은 어떤 사유로
닫히는가. 이 표가 있으면 다음에 열 축을 문장이 아니라 연산으로 고를 수 있다.

분류는 손으로 하지 않는다 — :mod:`temporal_claims` 의 판정기를 그대로 돌려 나온 결과를 센다.
그래서 코퍼스가 늘거나 카탈로그가 늘면 이 표는 다시 돌리는 것만으로 최신이 된다.

사용::

    python tools/temporal_operation_census.py                 # 표를 stdout 으로
    python tools/temporal_operation_census.py --json          # 원자료를 JSON 으로
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import temporal_claims  # noqa: E402
import temporal_ir  # noqa: E402
from temporal_ir import registry as treg  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

DEFAULT_CORPUS = REPO_ROOT / "docs/data/test_baselines/live_prompts.json"
SEOUL = ZoneInfo("Asia/Seoul")

# 시간 조건을 말하지 않은 요청의 분류. 이 축의 커버리지와 무관하다는 뜻이지
# '지원하지 않는다'가 아니다.
UNRELATED = "UNRELATED"


def classify(
    query: str,
    *,
    snapshot: dict[str, Any],
    catalog: Any,
    runtime: temporal_ir.TemporalRuntime,
    context: sir.TemporalRequestContext,
    today: date,
) -> dict[str, Any]:
    """요청 하나 → (의미 연산, 귀결, 사유)."""

    outcome = temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot,
        catalog=catalog,
        runtime=runtime,
        context=context,
        today=today,
    )
    if outcome is None:
        return {"operation": UNRELATED, "outcome": "unrelated", "reason": None}
    if isinstance(outcome, temporal_claims.TemporalClaimRejection):
        # 조합까지 만들어졌는지에 따라 '무엇이 막혔는가'가 갈린다. 조합 이전에 닫힌 요청은
        # 연산을 확정할 수 없으므로 그 사실을 그대로 적는다(연산을 추측해 채우지 않는다).
        detected = temporal_claims.detect_temporal_claims(
            query,
            snapshot=snapshot,
            catalog=catalog,
            runtime=runtime,
            today=today,
        )
        operation = (
            treg.resolve_operator_name(detected[0].condition)
            if isinstance(detected, tuple) and detected
            else "UNRESOLVED"
        )
        return {
            "operation": operation,
            "outcome": "unsupported",
            "reason": outcome.code,
        }
    return {
        "operation": outcome.receipts[0]["operator"],
        "outcome": "compiled",
        "reason": None,
    }


def census(corpus_path: Path, *, today: date) -> dict[str, Any]:
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    prompts = payload.get("prompts") or []
    catalog = audience_runtime.resolve_audience_catalog()
    snapshot = audience_runtime.catalog_snapshot()
    runtime = temporal_ir.create_temporal_runtime(catalog)
    context = sir.TemporalRequestContext(
        now=datetime.combine(today, datetime.min.time(), tzinfo=SEOUL)
    )

    rows: list[dict[str, Any]] = []
    for item in prompts:
        query = str(item.get("text") or "")
        if not query:
            continue
        verdict = classify(
            query,
            snapshot=snapshot,
            catalog=catalog,
            runtime=runtime,
            context=context,
            today=today,
        )
        rows.append({"text": query, **verdict})
    return {"corpus": str(corpus_path), "today": today.isoformat(), "rows": rows}


def render(result: dict[str, Any]) -> str:
    rows = result["rows"]
    by_operation: dict[str, Counter] = {}
    for row in rows:
        by_operation.setdefault(row["operation"], Counter())[row["outcome"]] += 1

    lines = [
        f"# 코퍼스 시간·이력 연산 census ({result['corpus']}, 기준일 {result['today']})",
        "",
        f"총 {len(rows)}종 · 시간 조건을 말한 요청 "
        f"{sum(1 for row in rows if row['operation'] != UNRELATED)}종",
        "",
        "| 의미 연산 | 컴파일 | 미지원 | 합계 |",
        "|---|---:|---:|---:|",
    ]
    for operation in sorted(by_operation, key=lambda name: (name == UNRELATED, name)):
        counts = by_operation[operation]
        total = sum(counts.values())
        lines.append(
            f"| `{operation}` | {counts['compiled']} | {counts['unsupported']} | {total} |"
        )

    blocked = [row for row in rows if row["outcome"] == "unsupported"]
    if blocked:
        lines += ["", "## 닫힌 요청의 사유", "", "| 사유 | 건수 |", "|---|---:|"]
        for reason, count in Counter(row["reason"] for row in blocked).most_common():
            lines.append(f"| `{reason}` | {count} |")

    compiled = [row for row in rows if row["outcome"] == "compiled"]
    if compiled:
        lines += ["", "## 컴파일되는 요청", ""]
        lines += [f"- `{row['operation']}` — {row['text']}" for row in compiled]
    if blocked:
        lines += ["", "## 닫히는 요청", ""]
        lines += [f"- `{row['reason']}` — {row['text']}" for row in blocked]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="기준일(YYYY-MM-DD). 상대 표현을 확정하는 기준이며 생략하면 오늘.",
    )
    parser.add_argument("--json", action="store_true", help="원자료를 JSON 으로 출력")
    args = parser.parse_args()

    result = census(args.corpus, today=args.today or datetime.now(SEOUL).date())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result), end="")


if __name__ == "__main__":
    main()
