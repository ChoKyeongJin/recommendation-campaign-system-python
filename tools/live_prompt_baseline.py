"""라이브 프롬프트 회귀 러너 — 코퍼스를 `/target-sql` 로 전수 실행하고 귀결을 분류한다.

    python tools/live_prompt_baseline.py                    # 1회 실행, 스코어보드 출력
    python tools/live_prompt_baseline.py --repeat 3         # 방출 편차 관측(프롬프트당 3회)
    python tools/live_prompt_baseline.py --only 3,8,12      # 일부만
    python tools/live_prompt_baseline.py --json out.json    # 기계 판독 결과 저장
    python tools/live_prompt_baseline.py --base-url http://localhost:8000

배경: 이 러너는 원래 세션 스크래치패드에만 있었다. 그래서 같은 숫자를 두 번 잴 수 없었고,
결국 하나의 코퍼스에 대해 14/26·0/26·12/26 세 개의 '기준선'이 서로를 반박하며 공존했다
(NOTES.md · docs/architecture_generic_core.md §9 · 세션 기억). **재현할 수 없는 수치는
기준선이 아니다** — 그것이 이 파일이 저장소에 있는 이유다.

분류(닫힌 5종). 러너는 성공/실패를 판정하지 않고 **귀결의 종류**를 센다:

    sql            타겟 SQL 이 출고됐다
    unsupported    정직한 미지원(사유가 이름을 대며 막혔다)
    clarification  사용자에게 되물었다
    failure        위 셋 중 어느 것도 아닌 실패(사유는 있으나 귀결이 아니다)
    error          호출 자체가 깨졌다(네트워크·5xx)

`unsupported` 를 실패로 세지 않는 것이 이 도구의 핵심 편향이다. 적재되지 않은 데이터를
요구하는 프롬프트에서 '정직한 미지원'은 **정답**이고, 그럴듯한 SQL 이야말로 최악의 출력이다.

`required_clauses`(코퍼스 항목의 선택 필드) — "SQL 을 낸다면 반드시 포함해야 하는 조각".
선언된 조각이 빠졌으면 귀결을 `failure` 로 **강등**한다. 닫힌 5종 어휘는 "SQL 은 나왔는데
절이 하나 사라졌다"를 표현하지 못하고, 그 상태는 `_verdict` 에서 `outcome == "sql"` 규칙에
걸려 **improvement 로 오집계**된다(혼합축 프롬프트에서 실측: 이력 절이 사라진 성별 전용 SQL 이
경고 0으로 '개선'으로 보고됐다). 강등이 없으면 그런 항목은 안전망이 아니라 오도 장치다.

expectation 대비 판정(`--json` 및 종료코드):

    regression     기대가 sql 인데 sql 이 아니다 / 기대가 unsupported·clarification 인데 failure 다
    improvement    기대가 sql 이 아닌데 sql 이 나왔다(기대치 갱신 후보)
    match          기대와 같다
    unknown        합의된 기대가 없다(관측만)

종료코드: 0 = regression 없음, 1 = regression 있음, 2 = 호출 오류로 판정 불가.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "docs" / "data" / "test_baselines" / "live_prompts.json"
DEFAULT_BASE_URL = "http://localhost:8000"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTCOMES = ("sql", "unsupported", "clarification", "failure", "error")


def _load_corpus(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    prompts = data.get("prompts") or []
    if not prompts:
        raise SystemExit(f"코퍼스가 비었다: {path}")
    return prompts


def _call(base_url: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = json.dumps(
        {
            "prompt": prompt,
            "query_parser": "auto",
            # SQL 출고 여부만 재므로 실행·적재는 하지 않는다(실DB 상태에 좌우되지 않게).
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
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    body["_elapsed_s"] = round(time.time() - started, 1)
    return body


def classify(response: dict[str, Any]) -> str:
    """API 응답을 닫힌 5종 귀결로 분류한다.

    **권위는 `status` 필드다**(graph_rag._api_status 의 닫힌 어휘:
    success / needs_clarification / unsupported / no_verified_sql, + api.py 의 sql_validation_failed).
    보조 필드로 세면 안 된다 — 미지원 응답도 같은 문구를 `clarification_questions` 에 실어
    사용자에게 보여주기 때문에, 그 필드를 먼저 보면 **정직한 미지원이 전부 '되묻기'로 오집계**된다
    (이 러너의 첫 실행이 정확히 그렇게 틀렸다: unsupported 0건, clarification 20건).

    순서: SQL 이 있으면 무엇이 더 있든 출고다. 그다음은 status 가 정한다.
    """

    if response.get("sql"):
        return "sql"
    status = response.get("status")
    if status == "unsupported":
        return "unsupported"
    if status == "needs_clarification":
        return "clarification"
    # status 가 없는 응답(구버전·부분 응답)만 보조 필드로 판정한다.
    if status is None:
        if response.get("clarification_questions") or response.get("missing_input_conditions"):
            return "clarification"
        if response.get("unsupported_conditions") or response.get("unsupported_reason"):
            return "unsupported"
    return "failure"


def _verdict(expectation: str, outcome: str) -> str:
    if expectation == "unknown":
        return "unknown"
    if outcome == "error":
        return "regression"
    if expectation == outcome:
        return "match"
    if outcome == "sql":
        return "improvement"
    return "regression"


def provenance(response: dict[str, Any]) -> dict[str, Any]:
    """이 응답의 SQL 을 **어느 컴파일러가** 냈는가(debug.sql_provenance).

    귀결 5종은 "무엇이 나왔는가"만 세고 "누가 냈는가"는 세지 않는다. legacy 실행 레인을
    지울 수 있는지는 후자로만 답할 수 있다 — 후보 id 가 `sql_template:event_expression`
    이면 canonical 레인이고, 그 밖이면 legacy 빌더가 아직 살아 있다는 뜻이다.
    debug 블록이 없는 응답(구버전 API)에서는 빈 dict 이며, 그 경우 레인 집계는 `unknown` 이다.
    """
    debug = response.get("debug")
    if not isinstance(debug, dict):
        return {}
    value = debug.get("sql_provenance")
    return value if isinstance(value, dict) else {}


def _reason(response: dict[str, Any]) -> str:
    for key in ("failure_reason", "unsupported_reason"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and value.get("reason"):
            return str(value["reason"])
    stage = response.get("failure_stage")
    if isinstance(stage, dict) and stage.get("code"):
        return f"stage:{stage['code']}"
    return ""


def missing_clauses(entry: dict[str, Any], response: dict[str, Any]) -> list[str]:
    """SQL 이 나왔는데 항목이 요구한 조각이 빠졌다면 그 조각들을 돌려준다.

    `classify` 를 건드리지 않는 이유: 저 함수는 응답 하나만 보는 순수 분류기이고
    그 계약을 테스트가 고정한다. '기대되는 조각'은 응답이 아니라 **코퍼스 항목**의
    지식이므로 분류 다음 단계에서 합성한다.
    """
    required = entry.get("required_clauses") or []
    sql = response.get("sql")
    if not required or not isinstance(sql, str) or not sql:
        return []
    return [clause for clause in required if clause not in sql]


def run(
    corpus: list[dict[str, Any]],
    base_url: str,
    repeat: int,
    timeout: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in corpus:
        prompt = entry["text"]
        expectation = entry.get("expectation", "unknown")
        observations: list[dict[str, Any]] = []
        for attempt in range(repeat):
            try:
                response = _call(base_url, prompt, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                observations.append({"outcome": "error", "reason": str(exc)[:120], "elapsed_s": None})
                continue
            outcome = classify(response)
            reason = _reason(response)
            missing = missing_clauses(entry, response)
            if missing:
                # 절이 사라진 SQL 은 출고가 아니다. 강등하지 않으면 improvement 로 오집계된다.
                outcome = "failure"
                reason = "partial_sql:" + ",".join(missing)
            observations.append(
                {
                    "outcome": outcome,
                    "reason": reason,
                    "status": response.get("status"),
                    "elapsed_s": response.get("_elapsed_s"),
                    "provenance": provenance(response),
                }
            )
        outcomes = [observation["outcome"] for observation in observations]
        # 편차가 있으면 **가장 나쁜** 귀결로 센다 — 3번 중 1번만 나오는 SQL 은 출고가 아니다.
        worst = max(outcomes, key=OUTCOMES.index)
        row = {
            "id": entry.get("id"),
            "prompt": prompt,
            "expectation": expectation,
            "outcome": worst,
            "verdict": _verdict(expectation, worst),
            "unstable": len(set(outcomes)) > 1,
            "observations": observations,
            "note": entry.get("note", ""),
        }
        rows.append(row)
        flag = "~" if row["unstable"] else " "
        print(
            f"[{row['id']:>2}]{flag}{row['outcome']:<14} {row['verdict']:<12} "
            f"{(observations[0].get('reason') or '')[:46]:<46} {prompt[:38]}",
            flush=True,
        )
    return rows


DEFAULT_BASELINE = ROOT / "docs" / "data" / "test_baselines" / "live_prompt_outcomes_baseline.json"


def compare_to_baseline(
    rows: list[dict[str, Any]], baseline: dict[str, Any]
) -> list[dict[str, str]]:
    """커밋된 **실측** 기준선과의 차이. expectation 대비 판정과는 다른 축이다.

    expectation 은 '합의된 귀결'이라 오래 고정돼 있고, 그래서 "지금 무엇이 달라졌는가"를
    말해 주지 못한다. 이 대조는 마지막 실측과의 차이만 본다 — 재현할 수 없는 수치가
    기준선 노릇을 하던 상태로 돌아가지 않게 하는 것이 목적이다.
    """
    recorded = baseline.get("rows") or {}
    changes: list[dict[str, str]] = []
    for row in rows:
        before = (recorded.get(str(row["id"])) or {}).get("outcome")
        if before is None or before == row["outcome"]:
            continue
        direction = (
            "better" if OUTCOMES.index(row["outcome"]) < OUTCOMES.index(before) else "worse"
        )
        changes.append({
            "id": str(row["id"]), "from": before, "to": row["outcome"], "direction": direction,
        })
    return changes


def _row_candidate(row: dict[str, Any]) -> str | None:
    """이 항목에서 SQL 을 낸 후보 id(없으면 None). 반복 실행 시 첫 관측을 쓴다."""
    for observation in row.get("observations", ()):
        candidate = (observation.get("provenance") or {}).get("candidate_id")
        if candidate:
            return str(candidate)
    return None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {outcome: 0 for outcome in OUTCOMES}
    verdicts = {"match": 0, "improvement": 0, "regression": 0, "unknown": 0}
    # 레인 집계: 후보 id 별 항목 수. SQL 을 낸 항목만 센다(낸 적 없으면 레인이 없다).
    lanes: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] += 1
        verdicts[row["verdict"]] += 1
        candidate = _row_candidate(row)
        if candidate:
            lanes[candidate] = lanes.get(candidate, 0) + 1
    return {
        "total": len(rows),
        "outcomes": counts,
        "verdicts": verdicts,
        "lanes": dict(sorted(lanes.items(), key=lambda item: (-item[1], item[0]))),
        "lane_ids": {
            candidate: [row["id"] for row in rows if _row_candidate(row) == candidate]
            for candidate in sorted(lanes)
        },
        "unstable": [row["id"] for row in rows if row["unstable"]],
        "regressions": [row["id"] for row in rows if row["verdict"] == "regression"],
        "improvements": [row["id"] for row in rows if row["verdict"] == "improvement"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--repeat", type=int, default=1, help="프롬프트당 반복 횟수(방출 편차 관측)")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--only", default="", help="쉼표로 구분한 id 목록")
    parser.add_argument("--json", type=Path, default=None, help="결과 저장 경로")
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE,
        help="커밋된 실측 기준선과 대조(항목별 귀결 변화)",
    )
    args = parser.parse_args()

    corpus = _load_corpus(args.corpus)
    if args.only:
        wanted = {int(token) for token in args.only.split(",") if token.strip()}
        corpus = [entry for entry in corpus if entry.get("id") in wanted]

    rows = run(corpus, args.base_url, max(1, args.repeat), args.timeout)
    summary = summarize(rows)

    print("\n" + "=" * 72)
    print(f"귀결: " + " / ".join(f"{name} {summary['outcomes'][name]}" for name in OUTCOMES))
    print(
        f"판정: match {summary['verdicts']['match']} / improvement {summary['verdicts']['improvement']}"
        f" / regression {summary['verdicts']['regression']} / unknown {summary['verdicts']['unknown']}"
    )
    if summary["lanes"]:
        print("\n레인(어느 컴파일러가 SQL 을 냈나):")
        for candidate, count in summary["lanes"].items():
            print(f"  {count:>3}건  {candidate}  {summary['lane_ids'][candidate]}")
    if summary["unstable"]:
        print(f"방출 편차(반복 간 귀결 불일치): {summary['unstable']}")
    if summary["regressions"]:
        print(f"회귀: {summary['regressions']}")
    if summary["improvements"]:
        print(f"개선(기대치 갱신 후보): {summary['improvements']}")

    if args.baseline and args.baseline.exists():
        changes = compare_to_baseline(rows, json.loads(args.baseline.read_text(encoding="utf-8")))
        if changes:
            print(f"\n실측 기준선 대비 변화 {len(changes)}건:")
            for change in changes:
                mark = "+" if change["direction"] == "better" else "-"
                print(f"  {mark} #{change['id']:>2} {change['from']} → {change['to']}")
        else:
            print("\n실측 기준선 대비 변화 없음")

    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"결과 저장: {args.json}")

    if summary["outcomes"]["error"] == summary["total"]:
        print("\n전부 호출 오류다 — API 가 떠 있는지 확인하라(docker compose up api).")
        return 2
    return 1 if summary["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
