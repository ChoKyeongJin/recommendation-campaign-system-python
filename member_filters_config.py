"""회원 타겟 레지스트리에서 **여러 모듈이 공유해야 하는 값**만 꺼내는 순수 접근자.

배경: ``order_count_targets.behaviors`` 의 키 집합(주문 횟수 행동)을 세 소비자가 각자 다른 방법으로
얻고 있었다 — graph_rag 는 설정 JSON 을 주입하고, confidence 와 canonical_targeting 은
targeting_ir 의 코드 기본값 폴백을 썼다. 그래서 설정에 행동을 하나 추가하면 같은 조건이
graph_rag 에서는 ``order_count_behavior`` 로, 나머지 둘에서는 ``unclassified_behavior`` 로 분류돼
신뢰도 리포트와 레거시 조건 트리에서 조용히 빠졌다. 값이 세 곳에 이중 소유돼 있던 것이다.

이 모듈은 그 값을 한 곳에서 읽는다. graph_rag 를 import 하지 않으므로(순수 모듈) confidence·
canonical_targeting 같은 하위 계층도 순환 없이 쓸 수 있고, targeting_ir 은 여전히 설정을 모른다
(호출자가 주입하는 규약 유지).

경로 규약은 graph_rag 와 동일하다: 환경변수 GRAPH_RAG_MEMBER_TARGET_FILTERS 로 재지정 가능,
기본값은 저장소의 docs/data/member_target_filters.json. 기본 경로는 **모듈 기준 절대경로**다 —
상대경로면 cwd 가 바뀌는 순간 조용히 빈 설정으로 강등되기 때문이다.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = Path(
    os.getenv("GRAPH_RAG_MEMBER_TARGET_FILTERS")
    or (_REPO_ROOT / "docs" / "data" / "member_target_filters.json")
)


def _load(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=4)
def _behaviors_cached(path_text: str) -> frozenset[str]:
    section = _load(Path(path_text)).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    return frozenset(behaviors) if isinstance(behaviors, dict) else frozenset()


def order_count_behaviors(path: Path | None = None) -> frozenset[str]:
    """주문 횟수 행동의 캐노니컬 키 집합(설정이 단일 소스).

    설정을 못 읽으면 **빈 집합**을 돌려준다 — 코드 기본값으로 조용히 대체하지 않는다. 폴백이
    있으면 설정 파손이 '조금 다른 분류'로 나타나 눈에 띄지 않기 때문이다. 호출자는 빈 집합을
    보고 자기 정책(폴백/차단)을 정한다.
    """
    return _behaviors_cached(str(Path(path) if path is not None else DEFAULT_PATH))


def behavior_spec(behavior: str, path: Path | None = None) -> dict[str, Any] | None:
    """행동 하나의 설정 선언(없으면 None)."""
    section = _load(path).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    if not isinstance(behaviors, dict):
        return None
    spec = behaviors.get(behavior)
    return spec if isinstance(spec, dict) else None


def clear_cache() -> None:
    """설정 파일을 바꿔 끼우는 테스트용."""
    _behaviors_cached.cache_clear()


__all__ = ["DEFAULT_PATH", "behavior_spec", "clear_cache", "order_count_behaviors"]
