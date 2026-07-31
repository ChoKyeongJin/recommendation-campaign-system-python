"""주문 횟수 행동 집합이 소비자마다 갈리지 않는지 지킨다.

``order_count_targets.behaviors`` 는 설정(JSON)이 소유하는데, 소비자 셋이 서로 다른 방법으로
그 값을 얻고 있었다: graph_rag 는 설정을 주입하고 confidence·canonical_targeting 은
targeting_ir 의 코드 기본값 폴백을 썼다. 그래서 설정에 행동을 추가하면 같은 조건이
graph_rag 에서는 order_count_behavior 로, 나머지 둘에서는 unclassified_behavior 로 분류돼
신뢰도 리포트와 레거시 조건 트리에서 조용히 빠졌다.

지금은 member_filters_config 가 단일 소스다. 이 파일은 (1) 세 소비자가 같은 값을 보는지,
(2) 선언된 행동이 실제로 컴파일 가능한지(죽은 설정이 지원을 광고하지 않는지)를 본다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import member_filters_config  # noqa: E402
import targeting_ir  # noqa: E402

REGISTRY_PATH = REPO_ROOT / "docs" / "data" / "member_target_filters.json"


def _declared_behaviors() -> set[str]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return set((payload.get("order_count_targets") or {}).get("behaviors") or {})


def test_accessor_matches_the_configuration_file() -> None:
    assert set(member_filters_config.order_count_behaviors()) == _declared_behaviors()


def test_accessor_is_not_empty() -> None:
    """빈 집합은 '설정을 못 읽었다'는 뜻이다 — 조용한 강등을 green 으로 넘기지 않는다."""

    assert member_filters_config.order_count_behaviors(), (
        "주문 횟수 행동 집합이 비었다 — 설정 파일 경로/파싱을 확인하라."
    )


def test_code_default_never_silently_replaces_configuration() -> None:
    """코드 기본값이 설정보다 넓거나 좁으면, 주입을 빠뜨린 소비자가 다른 답을 내게 된다.

    기본값 자체를 없앨 수는 없지만(순수 모듈 규약), 설정과 어긋나면 드러나야 한다.
    """

    declared = _declared_behaviors()
    default = set(targeting_ir.DEFAULT_ORDER_COUNT_BEHAVIORS)
    assert default <= declared, (
        f"코드 기본값에만 있는 행동: {sorted(default - declared)}. "
        "설정에 없는 행동을 코드가 안다고 주장하면 주입 여부에 따라 분류가 갈린다."
    )


def _behaviors_config() -> dict:
    import graph_rag

    return (graph_rag._order_count_targets_config() or {}).get("behaviors") or {}


def test_unsupported_declarations_are_marked_as_such() -> None:
    """구현이 없는 선언은 ``_supported: false`` 로 표기돼 지원을 광고하지 않아야 한다.

    죽은 설정은 두 가지로 해롭다: 사용자가 표현해도 아무 일도 안 일어나고, 그 표현이 다른 행동으로
    흘러가면 의미가 반전된 오답이 나온다. 후자가 실제로 lapsed_buyer 에서 일어난다.
    """

    for name, spec in _behaviors_config().items():
        if not isinstance(spec, dict) or spec.get("_supported", True):
            continue
        assert str(spec.get("_unsupported_reason") or "").strip(), (
            f"{name} 이 미지원으로 표기됐는데 사유가 없다 — 다음 사람이 판단할 수 없다."
        )


def test_supported_behaviors_are_reachable_from_the_parser() -> None:
    """지원한다고 선언한 행동은 자연어로 도달 가능해야 한다.

    도달 판정은 behaviors 와 lifecycle 을 함께 본다 — 파서가 같은 개념을 어느 슬롯에 담는지는
    구현 선택이고(재구매는 lifecycle 로 간다), 여기서 보려는 것은 '표현이 그 개념에 닿는가'다.
    """

    import graph_rag

    unreachable: list[str] = []
    for name, spec in _behaviors_config().items():
        if not isinstance(spec, dict) or not spec.get("_supported", True):
            continue
        synonyms = spec.get("synonyms") or []
        if not synonyms:
            continue
        produced: set[str] = set()
        for phrase in synonyms:
            plan = graph_rag.build_query_plan(f"{phrase} 고객", parser="rules")
            target_user = plan.get("target_user") or {}
            produced |= set(target_user.get("behaviors") or [])
            produced |= set(target_user.get("lifecycle") or [])
        if name not in produced:
            unreachable.append(f"{name}(동의어 {synonyms} → {sorted(produced) or '아무것도 안 나옴'})")
    assert not unreachable, (
        "지원 선언된 행동인데 자연어로 도달할 수 없다:\n  " + "\n  ".join(unreachable)
    )


def test_unsupported_behaviour_phrasing_does_not_silently_become_another_behaviour() -> None:
    """미지원 표현이 **다른 행동으로 조용히 바뀌는지**를 기록한다(의미 반전 감시).

    lapsed_buyer('예전에는 구매했지만 최근 구매하지 않은')는 현재 no_purchase(무구매)로 흘러간다 —
    '과거에 샀다'가 '산 적 없다'로 뒤집히는 오답이다. 아직 고치지 못했으므로 여기서는 그 사실을
    **고정**해 둔다: 상태가 달라지면(고쳐지거나 더 나빠지거나) 이 테스트가 알려준다.
    """

    import graph_rag

    plan = graph_rag.build_query_plan("예전에는 구매했지만 최근 구매하지 않은 고객", parser="rules")
    behaviours = set((plan.get("target_user") or {}).get("behaviors") or [])
    assert behaviours == {"no_purchase"}, (
        f"lapsed 표현의 귀결이 바뀌었다: {sorted(behaviours)}. "
        "lapsed_buyer 를 구현했다면 이 테스트와 설정의 _supported 표기를 함께 갱신하라."
    )
