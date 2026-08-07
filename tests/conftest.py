from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# 표면 신호 LLM 해석(의도/목적/문맥 신호어)도 같은 이유로 기본 끔 — 켜두면 골든 코퍼스가 모델 출력에
# 의존한다. 끈 상태에서는 동결 백스톱 낱말만으로 동작하므로 이관 전 결정론 동작이 그대로 재현된다.
# 이 경로를 스텁으로 검증하던 테스트는 규칙 계층 철거와 함께 삭제됐다.
os.environ["SURFACE_LEXICON_LLM"] = "off"




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


@pytest.fixture
def member_slot_gate_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """폐쇄된 회원 슬롯 빌더를 **이 테스트 안에서만** 연다.

    2026-08-07 legacy 오디언스 실행 레인이 닫혔다. 채워진 ``target_user``/``exclude`` 를 가진
    플랜은 `audience_admission` 이 "오디언스를 말했다"로 판정하고, 그 순간 실행자는
    ``build_event_expression_sql_candidate`` 하나로 좁혀지며 `plan_validation` 이
    ``canonical_legacy_audience_conflict`` 를 낸다. 회원 슬롯 빌더 **코드는 남아 있지만
    프로덕션에서 도달하지 않는다**.

    그래서 그 빌더들의 컴파일 계약(어떤 슬롯이 어떤 SQL 바이트가 되는가)을 재던 단위 테스트는
    폐쇄 후 전부 ``None`` 을 받는다. 이 픽스처는 그 테스트들이 **무엇을 재는지**를 바꾸지 않고
    유지하기 위한 것이다 — 단언을 지우면 축을 다시 열 때 되돌아갈 근거가 사라진다.

    **재는 것이 달라진다는 점은 숨기지 않는다.** 이 픽스처를 쓰는 테스트는 "제품이 이 SQL 을
    낸다"가 아니라 "게이트를 걷으면 빌더가 이 SQL 을 낸다"를 잰다. 게이트가 실제로 걸린다는 것은
    ``tests/test_event_ir_builder_fail_close.py`` 와
    ``tests/test_canonical_legacy_audience_conflict.py`` 가 따로 고정한다 — 그 둘이 없으면 이
    픽스처는 폐쇄를 통째로 무효화하는 뒷문이 된다.

    두 술어를 함께 끄는 이유: `declares_audience` 만 끄면 `plan_validation` 이 여전히
    `execution_conflicts` 로 막아 빌더가 검증 게이트에서 죽는다(그러면 이 픽스처가 아무것도
    열지 못한 채 통과한 것처럼 보인다).
    """
    import audience_admission

    monkeypatch.setattr(audience_admission, "declares_audience", lambda _plan: False)
    monkeypatch.setattr(audience_admission, "execution_conflicts", lambda _plan: ())
