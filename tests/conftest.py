from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 조건 슬롯 LLM 보완은 테스트에서 기본 끔. 켜두면 OPENAI_API_KEY 가 있는 환경에서 스냅샷 테스트가
# 네트워크·모델 출력에 의존하게 되어 같은 입력에 다른 결과가 나온다(골든 코퍼스가 흔들린다).
# 이 경로의 동작은 tests/test_condition_slot_llm.py 가 추출기를 스텁으로 갈아끼워 검증한다.
os.environ["CONDITION_SLOT_LLM_FALLBACK"] = "off"

# 표면 신호 LLM 해석(의도/목적/문맥 신호어)도 같은 이유로 기본 끔 — 켜두면 골든 코퍼스가 모델 출력에
# 의존한다. 끈 상태에서는 동결 백스톱 낱말만으로 동작하므로 이관 전 결정론 동작이 그대로 재현된다.
# 이 경로의 동작은 tests/test_surface_lexicon_llm.py 가 추출기를 스텁으로 갈아끼워 검증한다.
os.environ["SURFACE_LEXICON_LLM"] = "off"

# 범용 상식 타겟 해석도 운영 기본은 on이지만, 회귀 테스트는 네트워크/모델 출력과 분리한다.
# 전용 테스트는 fake structured completion 또는 주입 service를 사용해 명시적으로 검증한다.
os.environ["CONCEPTUAL_TARGETING_LLM"] = "off"

# 상품 추출 LLM 폴백도 기본 끔. 지금까지는 tests/golden/cases.json 의 env 를 골든 헬퍼가 프로세스
# 전역에 덮어써서 "우연히" 꺼져 있었다(복원도 없음) — 그러면 이 값이 골든 모듈의 수집 여부에 좌우된다.
# 결정론 바닥은 수집 순서와 무관하게 여기 한 곳이 소유한다. 기본값은 graph_rag 에서 "true" 다.
os.environ["TARGET_OBJECT_LLM_FALLBACK"] = "false"


@pytest.fixture(autouse=True)
def _restore_environment():
    """테스트가 남긴 환경변수를 되돌린다 — 결과가 실행 순서에 좌우되지 않게.

    tests/golden_support.py:apply_corpus_env 는 코퍼스가 선언한 env 를 프로세스 전역에 쓰고
    복원하지 않는다(재생성 스크립트에서는 그게 맞다 — pytest 밖 단발 실행이니까). 그러나 테스트
    안에서는 골든 케이스를 한 번 돌리는 것만으로 이후 모든 테스트의 환경이 바뀐다. 그러면
    "단독 실행은 통과하는데 전체 실행에서 실패"(혹은 그 반대)가 생기고, 무엇보다 이 저장소가
    앞으로 측정하려는 멱등성·경로 패리티가 측정 도구 자체의 오염 위에서 계산된다.

    전체 스냅샷을 복원하므로 apply_corpus_env 를 손대지 않아도 누수가 닫힌다.
    """
    import os

    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


# ── 알려진 실패 대장(strict xfail) ────────────────────────────────────────────────────
# 대장에 적힌 nodeid 는 "지금 빨간 것"이다. 실패해도 스위트를 막지 않되(xfail), 고쳐지면 즉시 실패한다
# (strict=True → XPASS(strict)). 그래서 대장은 늘 현재의 부채와 정확히 일치하도록 강제된다.
KNOWN_FAILURES_PATH = Path(__file__).resolve().parent / "known_failures.json"

_REQUIRED_FIELDS = ("nodeid", "reason", "found_at", "owner")


def _as_pytest_nodeid(nodeid: str) -> str:
    """대장의 nodeid 를 pytest 표기로 맞춘다.

    pytest 는 파라미터 id 의 비ASCII 문자를 unicode escape 로 바꾼다. 대장에는 한글을 그대로
    적을 수 있게 하고 여기서 변환한다(JSON 에 백슬래시를 이중으로 쓰는 실수를 막는다).
    """
    return nodeid if nodeid.isascii() else nodeid.encode("unicode_escape").decode("ascii")


def _load_known_failures() -> dict[str, dict]:
    if not KNOWN_FAILURES_PATH.exists():
        return {}
    raw = json.loads(KNOWN_FAILURES_PATH.read_text(encoding="utf-8"))
    entries = raw.get("failures", []) if isinstance(raw, dict) else raw
    ledger: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise pytest.UsageError(f"{KNOWN_FAILURES_PATH.name}[{index}] 은 객체여야 한다.")
        missing = [key for key in _REQUIRED_FIELDS if not str(entry.get(key) or "").strip()]
        if missing:
            # 사유·발견일·담당 없는 등록을 막는다 — 그게 없으면 대장은 그냥 무덤이 된다.
            raise pytest.UsageError(
                f"{KNOWN_FAILURES_PATH.name}[{index}] 필수 필드 누락: {', '.join(missing)}."
            )
        nodeid = _as_pytest_nodeid(str(entry["nodeid"]))
        if nodeid in ledger:
            raise pytest.UsageError(f"{KNOWN_FAILURES_PATH.name} 에 중복 nodeid: {nodeid}")
        ledger[nodeid] = entry
    return ledger


def _selection_is_filtered(config: pytest.Config) -> bool:
    """-k/-m/--lf 처럼 선택을 좁힌 실행에서는 '대장에 있는데 수집 안 됨'이 정상이다."""
    option = config.option
    return bool(
        getattr(option, "keyword", "")
        or getattr(option, "markexpr", "")
        or getattr(option, "lf", False)
        or getattr(option, "failedfirst", False)
        or getattr(option, "stepwise", False)
        or getattr(option, "deselect", None)
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ledger = _load_known_failures()
    if not ledger:
        return

    matched: set[str] = set()
    for item in items:
        entry = ledger.get(item.nodeid)
        if entry is None:
            continue
        matched.add(item.nodeid)
        item.add_marker(
            pytest.mark.xfail(
                reason=(
                    f"[known_failures] {entry['reason']} "
                    f"(발견 {entry['found_at']}, 담당 {entry['owner']}) "
                    f"— 고쳐졌으면 {KNOWN_FAILURES_PATH.name} 에서 이 줄을 지워라."
                ),
                strict=True,
                run=True,
            )
        )

    if _selection_is_filtered(config):
        return

    # 대장이 가리키는 파일은 수집됐는데 그 nodeid 가 없다 → 이름 변경/삭제 또는 이스케이프 오타.
    # 조용히 넘어가면 대장이 아무것도 지키지 않게 되므로 세션을 세운다.
    collected_files = {item.nodeid.split("::", 1)[0] for item in items}
    stale = sorted(
        nodeid
        for nodeid in ledger
        if nodeid not in matched and nodeid.split("::", 1)[0] in collected_files
    )
    if stale:
        raise pytest.UsageError(
            "known_failures 대장에 수집되지 않는 nodeid 가 있다(테스트 이름 변경/삭제 또는 이스케이프 오타):\n  "
            + "\n  ".join(stale)
        )
