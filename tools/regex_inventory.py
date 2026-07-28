"""정규식 인벤토리 리포트 생성 CLI. 분류 로직은 최상위 :mod:`regex_inventory` 가 소유한다.

    docker compose exec -e PYTHONPATH=/app -w /app api python tools/regex_inventory.py [--set-baseline]

출력은 검토용 초안이다. 자동 판정을 그대로 믿지 말고 `decision` 열을 사람이 채운다.
``--set-baseline`` 은 어휘형 래칫의 상한을 현재 값으로 내린다(줄일 때만 쓴다 — 늘리려면 사유가 필요).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import regex_inventory  # noqa: E402

OUT_JSON = ROOT / "docs" / "data" / "regex_inventory.json"
OUT_MD = ROOT / "docs" / "regex_inventory.md"
BASELINE = ROOT / "docs" / "data" / "regex_inventory_baseline.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-baseline", action="store_true", help="어휘형 래칫 상한을 현재 값으로 갱신")
    args = parser.parse_args()

    rows = regex_inventory.scan()
    counts = regex_inventory.counts(rows)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "description": "코드에 박힌 정규식 상수의 이행 분류 초안. decision 열은 사람이 채운다.",
        "counts": counts, "total": len(rows), "rows": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 정규식 인벤토리 (이행 작업 목록)",
        "",
        f"총 {len(rows)}개 — 어휘형 {counts['lexical']} / 업무의미형 {counts['domain']} / 문법형 {counts['grammar']}",
        "",
        "판정 기준: **이 패턴이 열거하는 것이 단어 목록인가, 구조인가?** 단어 목록이면 데이터로 옮긴다",
        "(`lexicon_patterns` + `docs/data/parser_lexicon.json`). `decision` 은 사람이 채운다.",
        "",
        "| 분류 | 파일:줄 | 이름 | 교대수 | 패턴 | 자동 판정 사유 |",
        "|---|---|---|---:|---|---|",
    ]
    for row in rows:
        pattern = (row["pattern"] or "(동적 조립)").replace("|", "\\|").replace("`", "'")
        lines.append(
            f"| {row['class']} | {row['file']}:{row['line']} | `{row['name']}` | "
            f"{row.get('alternatives') or ''} | `{pattern}` | {row['reason']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.set_baseline:
        previous = json.loads(BASELINE.read_text(encoding="utf-8"))["counts"] if BASELINE.exists() else {}
        BASELINE.write_text(json.dumps({
            "description": "어휘형 정규식 래칫 상한. 이행이 진행되면 내려간다. 올리려면 사유가 필요하다.",
            "counts": counts,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"기준선 갱신: {previous.get('lexical', '-')} → {counts['lexical']}")

    print(f"총 {len(rows)}개: {counts}")
    print(f"  {OUT_JSON.relative_to(ROOT)}")
    print(f"  {OUT_MD.relative_to(ROOT)}")
    print()
    print("이행 우선순위(교대수 많은 어휘형부터):")
    for row in [r for r in rows if r["class"] == "lexical"]:
        print(f"  {row['alternatives']:3d}개  {row['file']}:{row['line']:<6} {row['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
