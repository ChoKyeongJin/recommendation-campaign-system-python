"""의미 필드와 출처(provenance) 필드의 경계 — "같은 뜻인가"를 판정할 때 무엇을 빼는가.

조건 IR 노드는 뜻(무엇을 조건으로 읽었나)과 출처(그 뜻을 원문 어디에서·무엇이 읽었나)를 한 dict 에
같이 담는다. 출처는 진단·감사에 반드시 필요하지만, **두 해석이 같은 뜻인지** 판정할 때 섞이면
곧바로 오탐이 된다.

실제로 그렇게 샜다. 파서 게이트가 노드를 통째로 ``==`` 비교하는 바람에, 의미 서브트리가 글자 단위로
동일한데도 ``source_text`` 하나가 달라(한쪽은 절 분리된 문장, 다른 쪽은 원문) '의미 불일치'로
판정돼 SQL 생성이 막혔다. 출처가 다른 것은 정상이다 — 두 해석은 애초에 서로 다른 입력 문자열을 봤다.

그래서 경계를 한곳에 못 박는다.

    :data:`PROVENANCE_FIELDS`  뜻이 아니라 '어떻게 얻었는가'에 속하는 키 이름.
    :func:`strip_provenance`   그 키를 재귀적으로 걷어낸 의미 비교 전용 정규형.

소비자들이 이미 각자 같은 목록을 들고 있었다(:mod:`event_compiler` 의 노드 지문, 파서 게이트,
그리고 이후 삭제된 canonical_targeting 의 의미 해시). 목록이 갈라지면 "지문은 같은데 게이트는
다르다"는 모순이 생기므로 이 모듈이 단일 소스가 된다.

순수 모듈 불변식: 프로젝트 모듈을 import 하지 않는다(plain 값 입력).
"""

from __future__ import annotations

from typing import Any

# 의미가 아니라 **추출 과정**에 속하는 키. 이름 기준으로만 판정한다(값의 모양을 보지 않는다).
#
# 판정 기준은 하나다: "이 값이 달라졌을 때, 사용자가 요청한 조건이 달라졌다고 말할 수 있는가?"
# 아니라면 출처다. 반대로 조건 자체를 결정하는 값(연산자·필드·리터럴·극성·상태)은 절대 넣지 않는다.
PROVENANCE_FIELDS: frozenset[str] = frozenset({
    # 원문 근거와 그 좌표 — 같은 뜻을 서로 다른 입력 문자열에서 읽으면 반드시 달라진다.
    "evidence",
    "evidence_span",
    "source_span",
    "source_spans",
    "sourceSpan",
    "source_text",
    # 표면 문자열과 그 좌표. 실행 AST와 함께 보존되지만 절 분리본과 원문은 같은 뜻이어도
    # 접두사·출력 명사 때문에 이 값이 달라질 수 있다.
    "surface",
    "span",
    "spans",
    # 그 값을 만든 주체·경로(어느 파서·후보·룩업이 채웠는가).
    "source",
    "sources",
    "origin",
    "provenance",
    "resolution_source",
    "extracted_by",
    "generated_by",
    "rewrite_source",
    "parser",
    "stage",
    # 진단 부산물.
    "trace",
    "debug",
    "timestamp",
    # 확신도는 '얼마나 확실한가'이지 '무엇을 조건으로 읽었나'가 아니다.
    "confidence",
})


def is_provenance_field(key: Any) -> bool:
    """키가 출처/메타데이터인가. ``_`` 로 시작하는 내부 키도 조건이 아니다(기존 관례와 같다)."""
    name = str(key)
    return name.startswith("_") or name in PROVENANCE_FIELDS


def strip_provenance(value: Any) -> Any:
    """의미 비교 전용 정규형 — 출처 키를 재귀적으로 걷어낸다.

    입력을 변경하지 않는다(비교는 관찰이지 변환이 아니다). dict/list 이외의 값은 그대로 둔다.
    """
    if isinstance(value, dict):
        return {
            key: strip_provenance(child)
            for key, child in value.items()
            if not is_provenance_field(key)
        }
    if isinstance(value, (list, tuple)):
        return [strip_provenance(item) for item in value]
    return value
