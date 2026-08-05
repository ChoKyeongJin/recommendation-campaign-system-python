"""이벤트 표현 추출 — ``무슨 사건인가``만 읽는다.

이 extractor 가 규칙을 거의 갖지 않는 것이 정상이다. 한자어 명사+하다 동사는 활용이
어간을 보존하므로(``구매한``/``구매했던``/``구매하지`` 안에 ``구매`` 가 그대로 있다) alias
부분 일치만으로 충분하고, 어간이 변형되는 고유어 동사는 이미 정규화기가 ``사다`` 로
되돌려 놓았다. 즉 활용형 처리는 **여기 오기 전에 끝나 있다**.

여기서 하지 않는 일: 극성 판정(``구매하지 않은``), 시간 귀속, 횟수 결합. 그 셋은 각자의
축이 읽고 composer 가 붙인다. 한 extractor 가 문장 전체를 이해하려 들면 곧 문장 유형별
정규식으로 되돌아간다.
"""

from __future__ import annotations

from nl_event_ir.aliases import AliasRegistry, AliasSection, find_alias_matches
from nl_event_ir.enums import EventType
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["EventExtractor"]


class EventExtractor:
    """alias 로 선언된 사건 표면어를 :class:`~nl_event_ir.enums.EventType` 토큰으로 만든다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        for start, end, surface, canonical in find_alias_matches(
            text, self._registry, AliasSection.EVENT
        ):
            try:
                event_type = EventType(canonical)
            except ValueError as exc:
                # alias 파일이 enum 에 없는 canonical 값을 선언했다. 조용히 건너뛰면 사전과
                # 코드가 어긋난 채로 계속 도므로 여기서 멈춘다.
                raise ValueError(
                    f"alias file declares unknown event type '{canonical}' "
                    f"(surface '{surface}'). Known: "
                    f"{', '.join(member.value for member in EventType)}"
                ) from exc
            tokens.append(
                SemanticToken(
                    kind=TokenKind.EVENT,
                    value=event_type,
                    start=start,
                    end=end,
                    raw_text=surface,
                )
            )
        return tokens
