"""실행 가능한 데모 — ``python -m nl_event_ir.demo``.

이 파일은 **데모 진입점**이므로 표준 출력에 결과를 쓴다. 운영 코드의 로깅은 여전히
:mod:`logging` 이 담당한다(CLAUDE.md 49) — 여기의 ``print`` 는 로깅이 아니라 이 명령의
산출물 그 자체다.

카탈로그는 인메모리 예시다. 실제 카탈로그를 붙이려면
:class:`~nl_event_ir.resolver.base.EntityRepository` 프로토콜을 만족하는 구현을 넘기면 된다.
"""

from __future__ import annotations

import json
from datetime import date

from nl_event_ir.enums import EntityType
from nl_event_ir.parser import build_default_parser
from nl_event_ir.resolver import EntityCandidate, InMemoryEntityRepository

# 데모용 고정 기준일. 현재 시각을 읽지 않는 이유는 출력이 실행할 때마다 달라지지 않게
# 하기 위해서다(CLAUDE.md 15·44).
DEMO_REFERENCE_DATE = date(2026, 8, 5)

DEMO_CATALOG = [
    EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:adidas", "아디다스", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:newbalance", "뉴발란스", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:apple", "애플", 1.0),
    EntityCandidate(EntityType.PRODUCT, "product:apple", "애플", 1.0),
]

DEMO_SENTENCES = (
    "최근 3개월 동안 나이키를 두 번 이상 산 고객",
    "지난 7일 동안 로그인하지 않은 회원",
    "이번 달 주문 금액이 100000원 이상인 고객",
    "아디다스 또는 나이키 상품을 구매한 사용자",
    "정확히 1회 구매한 회원",
    "한 번도 로그인하지 않은 고객",
    "상품을 샀다",
    "애플을 구매한 고객",
    "최근에 뭔가 많이 한 사람",
)


def main() -> None:
    parser = build_default_parser(
        InMemoryEntityRepository(DEMO_CATALOG),
        reference_date=DEMO_REFERENCE_DATE,
    )

    for sentence in DEMO_SENTENCES:
        result = parser.parse(sentence)
        print("=" * 78)
        print(f"원문      : {result.original_text}")
        print(f"정규화    : {result.normalized_text}")
        print(
            "토큰      : "
            + ", ".join(f"{token.kind.value}({token.raw_text})" for token in result.tokens)
        )
        if result.ir is not None:
            print("IR        : " + json.dumps(result.ir.to_dict(), ensure_ascii=False))
        else:
            print(f"IR        : 없음 — fallback 필요({result.fallback_reason})")
        for issue in result.issues:
            print(f"  [{issue.severity.value}] {issue.code} @ {issue.path}: {issue.message}")


if __name__ == "__main__":
    main()
