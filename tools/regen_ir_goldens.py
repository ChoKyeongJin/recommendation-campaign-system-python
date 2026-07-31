"""골든 스냅샷·이행 기준선 재생성.

    docker compose exec -e PYTHONPATH=/app api python tools/regen_ir_goldens.py

생성물은 **검토 후 커밋**한다. 스냅샷을 눈으로 안 보고 갱신하면 골든이 회귀를 축복하는 도장이 된다.
diff 를 읽고 "이 변화가 의도한 개선인가"를 판단하는 것이 이 스크립트의 사용법이다.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# 진행 로그에 한글·기호(⚠)가 섞이므로 콘솔 기본 인코딩(예: Windows cp949)에서는 print 가
# UnicodeEncodeError 로 죽는다 — 그러면 스냅샷 일부만 쓰인 채 중단돼 골든이 반쪽 상태가 된다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ir_snapshot  # noqa: E402
import provenance  # noqa: E402
from golden_support import BASELINE_PATH, build_plan, load_cases, load_corpus, load_snapshot, write_snapshot  # noqa: E402


def main() -> int:
    cases = load_cases()
    parser = load_corpus().get("parser", "rules")
    total: Counter = Counter()
    changed, created = [], []

    for case in cases:
        plan = build_plan(case["prompt"], parser=parser)
        snapshot = ir_snapshot.snapshot(plan)
        previous = load_snapshot(case["id"])
        if previous is None:
            created.append(case["id"])
        elif previous != snapshot:
            changed.append(case["id"])
        write_snapshot(case["id"], snapshot)
        total.update(provenance.method_mix(plan))

        produced = ir_snapshot.condition_slots(plan)
        missing = sorted(set(case.get("expect_slots", [])) - produced)
        flag = f"  ⚠ 미충족: {missing}" if missing else ""
        print(f"  {case['id']:<34} {sorted(produced)}{flag}")

    BASELINE_PATH.write_text(
        json.dumps(
            {
                "description": "코퍼스 전체의 방법별 조건 수. rule 은 래칫 상한으로 쓰인다(늘리려면 사유와 함께 갱신).",
                "schema_version": ir_snapshot.SCHEMA_VERSION,
                "cases": len(cases),
                "methods": dict(sorted(total.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(f"신규 스냅샷 {len(created)}건 {created}")
    print(f"변경 스냅샷 {len(changed)}건 {changed}")
    print(f"방법 구성(원문 해석분): {dict(sorted(total.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
