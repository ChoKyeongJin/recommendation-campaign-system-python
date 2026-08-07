"""``nl_event_ir`` 패키지가 기존 저장소 모듈을 가리지 않는지 본다.

이 파일의 전신은 ``tests/test_legacy_compatibility.py`` 였고, 그 대부분은
``nl_event_ir.legacy_adapter``(기존 어휘 사전으로 잔여 구간을 메우던 보완 어댑터)의 계약이었다.
그 어댑터는 legacy 오디언스 레인 폐쇄와 함께 **삭제**됐다 — 켜는 호출자가 테스트뿐이었고,
어휘를 두 벌로 읽는 두 번째 경로였다. 남는 계약은 패키지 이름이 만드는 사고 둘뿐이다.
"""

from __future__ import annotations

from pathlib import Path

import lexicon_patterns


def test_existing_lexicon_module_still_loads() -> None:
    """보완 어댑터를 지운 것이 기존 사전 로더를 건드리지 않았는지 확인한다."""

    assert lexicon_patterns.vocabulary("purchase_verb")


def test_existing_event_ir_module_is_not_shadowed() -> None:
    """``nl_event_ir`` 패키지 이름이 기존 ``event_ir`` 모듈을 가리면 저장소 절반이 깨진다."""

    import event_ir

    assert Path(event_ir.__file__).name == "event_ir.py"
