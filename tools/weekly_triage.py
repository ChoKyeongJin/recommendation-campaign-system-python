"""주간 이행 리포트 — 미해석 표현 A/B/C 분류 + 슬롯 승격 후보 + 이행 지표.

    docker compose exec -e PYTHONPATH=/app -w /app api python tools/weekly_triage.py \
        --unresolved logs/unresolved.jsonl --shadow logs/parser_shadow.jsonl

사람이 하는 일은 리포트의 `decision` 열을 채우는 것뿐이다.

    A → docs/data/parser_lexicon.json 의 어휘에 한 줄 (배포 불필요)
    B → targeting_ir / _FilterSpec 레지스트리에 한 줄
    C → 새 ConditionSpec + 빌더 (드물게, 제대로)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import parser_shadow  # noqa: E402
import regex_inventory  # noqa: E402
import slot_policy  # noqa: E402
import unresolved_triage  # noqa: E402

OUT_MD = ROOT / "docs" / "weekly_triage.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unresolved", type=Path, default=None, help="미해석 로그(JSONL)")
    parser.add_argument("--shadow", type=Path, default=None, help="shadow 관찰 로그(JSONL)")
    parser.add_argument("--top", type=int, default=30, help="리포트에 실을 표현 수")
    args = parser.parse_args()

    cases = unresolved_triage.load(args.unresolved)
    rows = unresolved_triage.triage(cases)
    summary = unresolved_triage.summary(rows)

    observations = parser_shadow.load_observations(args.shadow)
    agreement = parser_shadow.agreement_by_slot(observations)
    promotions = slot_policy.promotion_report(agreement)
    eligible = [row for row in promotions if row["eligible"]]

    counts = regex_inventory.counts()
    losses = slot_policy.silent_loss_slots()

    lines = [
        "# 주간 이행 리포트",
        "",
        "## 1. 이행 지표",
        "",
        "| 지표 | 값 | 뜻 |",
        "|---|---:|---|",
        f"| 어휘형 정규식 | {counts['lexical']} | 코드에 남은 표면어 목록. 0 이 목표 |",
        f"| 업무의미형 정규식 | {counts['domain']} | 어휘+구조 혼합. 어휘 부분만 분리 대상 |",
        f"| 문법형 정규식 | {counts['grammar']} | 구조만. 코드에 남는 것이 정상 |",
        f"| LLM 소유 슬롯 | {len(slot_policy.llm_owned_slots())} | 정책상 LLM 이 확정하는 슬롯 |",
        f"| 조용한 소실 슬롯 | {len(losses)} | 백스톱도 fail-close 도 없는 자리. **0 이어야 한다** |",
        f"| shadow 관찰 | {len(observations)} | 누적 비교 건수 |",
        f"| 미해석 표현 | {len(rows)} | 이번 큐에 쌓인 서로 다른 표현 |",
        "",
    ]

    if losses:
        lines += ["### ⚠ 조용한 소실이 남아 있다", "",
                  "이 슬롯들은 값을 못 만들면 아무 표시 없이 사라진다 — 미해석 큐에 잡히지 않으므로 이 루프가 돌지 않는다.", ""]
        for slot, gap in sorted(losses.items()):
            lines.append(f"- `{slot}` — {gap or '(사유 미기재)'}")
        lines.append("")

    lines += [
        "## 2. 미해석 표현 (A/B/C 분류)",
        "",
        f"초안: A 어휘 {summary['A']} / B 파라미터 {summary['B']} / C 능력 {summary['C']}",
        "",
        "A 가 많으면 사전에 낱말이 모자란 것이고, C 가 많으면 정말로 새 능력이 필요한 것이다.",
        "`decision` 은 사람이 채운다 — 초안은 정렬용이지 판정이 아니다.",
        "",
        "| 빈도 | 초안 | 표현 | 근거 | 판정 사유 | decision |",
        "|---:|---|---|---|---|---|",
    ]
    if rows:
        for row in rows[: args.top]:
            evidence = ", ".join(row["evidence"]) or "-"
            lines.append(
                f"| {row['count']} | {row['class_draft']} | {row['query']} | {evidence} | {row['reason']} |  |"
            )
        if len(rows) > args.top:
            lines.append(f"| … | | 외 {len(rows) - args.top}건 | | | |")
    else:
        lines.append("| - | - | (큐가 비어 있다 — UNRESOLVED_LOG 가 설정돼 있는지 확인) | | | |")

    lines += [
        "",
        "## 3. 슬롯 승격 후보 (rule → llm)",
        "",
        "승격은 누적 shadow 관찰이 위험 등급별 문턱을 넘고, 위험 판정이 0 건일 때만 가능하다.",
        "",
        "| 슬롯 | 소유 | 위험 | 관찰 | 일치율 | 위험판정 | 막는 것 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    shown = [row for row in promotions if row["observed"] > 0] or promotions[:10]
    for row in shown[:30]:
        blocked = ", ".join(row["blocked_by"]) or "**승격 가능**"
        lines.append(
            f"| `{row['slot']}` | {row['owner']} | {row['risk']} | {row['observed']} | "
            f"{row['agreement']:.3f} | {row['risky']} | {blocked} |"
        )
    lines += ["", f"승격 가능: {len(eligible)}건", ""]

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"미해석 {len(rows)}건 (A{summary['A']}/B{summary['B']}/C{summary['C']}), "
          f"shadow 관찰 {len(observations)}건, 승격 가능 {len(eligible)}건, 조용한 소실 {len(losses)}건")
    print(f"  {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
