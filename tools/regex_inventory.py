"""규칙 인벤토리 리포트 생성 CLI. 분류 로직은 최상위 :mod:`regex_inventory` 가 소유한다.

    docker compose exec -e PYTHONPATH=/app -w /app api python tools/regex_inventory.py
    docker compose exec -e PYTHONPATH=/app -w /app api python tools/regex_inventory.py --set-baseline [--reason "..."]

출력은 검토용 초안이다. 자동 판정을 그대로 믿지 말고 `decision` 열을 사람이 채운다.
``--set-baseline`` 은 래칫 상한을 현재 값으로 갱신한다. 내리는 것은 자유, **올리는 것은
``--reason`` 없이는 거부된다**(regex_inventory.write_baseline 가 강제).
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-baseline", action="store_true", help="래칫 상한을 현재 값으로 갱신")
    parser.add_argument("--reason", default="", help="상한을 올릴 때의 사유(상향 시 필수)")
    args = parser.parse_args()

    rows = regex_inventory.scan()
    counts = regex_inventory.counts(rows)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "description": "코드에 박힌 표면어 규칙(상수·묶음·인라인 정규식 + 낱말집합)의 이행 분류 초안. decision 열은 사람이 채운다.",
        "counts": counts, "total": len(rows), "rows": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 규칙 인벤토리 (이행 작업 목록)",
        "",
        f"총 {len(rows)}개 — 어휘형 {counts['lexical']} / 낱말집합 {counts['wordlist']} / "
        f"업무의미형 {counts['domain']} / 문법형 {counts['grammar']}",
        "",
        f"래칫 대상: {', '.join(regex_inventory.RATCHETED_CLASSES)} (문법형은 구조라 상한 없음).",
        "",
        "판정 기준: **이 규칙이 열거하는 것이 단어 목록인가, 구조인가?** 단어 목록이면 데이터로 옮긴다",
        "(`lexicon_patterns` + `docs/data/parser_lexicon.json`). `decision` 은 사람이 채운다.",
        "",
        "`source` 열은 규칙이 코드에 담긴 형태다: `constant`(이름 붙은 정규식) / `collection`(튜플·딕트에",
        "담긴 정규식) / `inline`(이름 없는 호출) / `wordlist`(한글 낱말집합 상수).",
        "",
        "| 분류 | 형태 | 파일:줄 | 이름 | 교대수 | 패턴 | 자동 판정 사유 |",
        "|---|---|---|---|---:|---|---|",
    ]
    for row in rows:
        pattern = (row["pattern"] or "(동적 조립)").replace("|", "\\|").replace("`", "'")
        lines.append(
            f"| {row['class']} | {row.get('source', '')} | {row['file']}:{row['line']} | `{row['name']}` | "
            f"{row.get('alternatives') or ''} | `{pattern}` | {row['reason']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.set_baseline:
        try:
            payload = regex_inventory.write_baseline(counts, reason=args.reason)
        except ValueError as exc:
            print(f"기준선 갱신 거부: {exc}")
            return 2
        print(f"기준선 갱신: {payload['previous_counts'] or '(없음)'} → {payload['counts']}")
        if payload["raise_reason"]:
            print(f"  상향 사유: {payload['raise_reason']}")

    print(f"총 {len(rows)}개: {counts}")
    if regex_inventory.BASELINE_PATH.exists():
        recorded = regex_inventory.baseline()["counts"]
        breaches = regex_inventory.regressions(counts, recorded)
        print("래칫: " + ("초과 — " + "; ".join(breaches) if breaches else "이상 없음"))
    print(f"  {OUT_JSON.relative_to(ROOT)}")
    print(f"  {OUT_MD.relative_to(ROOT)}")
    print()
    print("이행 우선순위(교대수 많은 어휘형·낱말집합부터):")
    for row in [r for r in rows if r["class"] in {"lexical", "wordlist"}]:
        print(f"  {row['alternatives']:3d}개  {row['class']:<8} {row['file']}:{row['line']:<6} {row['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
