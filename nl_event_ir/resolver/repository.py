"""테스트·개발용 인메모리 repository.

운영 구현(카탈로그 DB, 검색 엔진)은 같은 :class:`~nl_event_ir.resolver.base.EntityRepository`
계약을 만족하면 그대로 교체된다. 이 구현이 존재하는 이유는 **파서 테스트가 데이터베이스를
필요로 하지 않게** 하기 위해서다.

점수 규칙은 단순하고 결정론적이다. 정확히 같으면 1.0, 한쪽이 다른 쪽을 포함하면 길이 비율,
그 외에는 후보가 아니다. 유사도 추정을 넣지 않은 것은 의도다 — 느슨한 매칭은 실제 카탈로그
검색기가 할 일이고, 여기서 흉내 내면 테스트가 운영과 다른 것을 검증하게 된다.
"""

from __future__ import annotations

from nl_event_ir.enums import EntityType
from nl_event_ir.resolver.base import EntityCandidate

__all__ = ["InMemoryEntityRepository"]


class InMemoryEntityRepository:
    """선언된 엔티티 목록에서만 찾는 repository."""

    def __init__(self, entities: list[EntityCandidate]) -> None:
        self._entities = tuple(entities)

    def search(
        self,
        text: str,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[EntityCandidate]:
        needle = text.strip().casefold()
        if not needle:
            return []

        matches: list[EntityCandidate] = []
        for entity in self._entities:
            if entity_types is not None and entity.entity_type not in entity_types:
                continue
            score = _match_score(needle, entity.canonical_name.strip().casefold())
            if score is None:
                continue
            matches.append(
                EntityCandidate(
                    entity_type=entity.entity_type,
                    entity_id=entity.entity_id,
                    canonical_name=entity.canonical_name,
                    score=score,
                )
            )

        # 점수 내림차순, 동점이면 entity_id 오름차순. 보조 키가 있어야 결과가 재현된다.
        matches.sort(key=lambda candidate: (-candidate.score, candidate.entity_id))
        return matches


def _match_score(needle: str, canonical: str) -> float | None:
    """일치 점수. 후보가 아니면 ``None``."""

    if not canonical:
        return None
    if needle == canonical:
        return 1.0
    if needle in canonical:
        # 부분 일치는 길이 비율로 감점한다. '나이' 가 '나이키' 를 확정하지 못하게 한다.
        return round(len(needle) / len(canonical), 4)
    return None
