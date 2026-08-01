"""회원속성 토큰 승격 문법 — 표면어를 lifecycle/exclude.lifecycle 로 올리는 선언형 스펙.

graph_rag.py 에서 분리했다. "활동회원", "자녀 등록", "앱 가입" 같은 단순 속성형 조건은
파싱 구조가 전부 같다(표면어 + 긍정/부정 접미어 + 발동 게이트). 그 공통 문법을 데이터로
선언해 두면 새 속성 필터가 전용 _apply_* 함수 없이 스펙 한 줄로 열린다.

설정(docs/data/attribute_token_groups.json)이 우선이고, 파일이 없거나 깨졌을 때를 위한
코드 폴백(_default_attribute_token_groups_raw)이 같은 스키마로 여기 함께 산다 — 둘이
갈라지면 "설정만 읽는 도구"와 "런타임"이 다른 답을 내므로, 한 모듈에 두어 나란히 읽히게 했다.

이 문법으로 표현 못 하는 복합 파싱(서열 랭크 확장·이중부정·채널어 강등)은 커스텀 필터로
남는다. 그 분류를 강제하는 가드는 없다(TODO — 원본 주석의 지적을 그대로 옮긴다).

순수 모듈 불변식: graph_rag 를 import 하지 않는다. stdlib 전용.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# 속성 토큰 그룹 선언(회원속성 표면어→lifecycle/exclude 승격 문법)의 단일 소스. 파일 부재/파손 시
# _default_attribute_token_groups_raw() 코드 폴백을 쓴다(동작 불변).
DEFAULT_ATTRIBUTE_TOKEN_GROUPS_PATH = Path(
    os.getenv("GRAPH_RAG_ATTRIBUTE_TOKEN_GROUPS", "docs/data/attribute_token_groups.json")
)


@dataclass(frozen=True)
class _AttributeTokenGroup:
    """회원속성 토큰 승격 문법(선언형 스펙). 표면어(canonical)를 lifecycle(포함)/exclude.lifecycle(제외)로
    올리는 '단순 속성형' 필터의 전체 동작을 데이터로 선언한다 — 새 필터는 이 스펙 한 줄 + 레지스트리
    attribute_token 엔트리 등록만으로 열린다(전용 _apply_* 함수 불필요). 복합 파싱(서열 랭크 확장·이중부정·
    채널어 강등 등)은 이 문법으로 표현 못 하므로 커스텀 필터(impl="custom", family="attribute_token",
    exception_reason=...)로 남긴다 — 그 분류를 강제하는 가드는 없다(TODO)."""

    canonicals: tuple[tuple[str, tuple[str, ...]], ...]  # (canonical, 기본 표면어); surface_terms JSON 있으면 덮음
    neg: str | None = None   # 부정 접미어 정규식 → exclude.lifecycle (None=부정 없음)
    pos: str = ""            # 긍정 접미어 정규식 ('' = 표면어 단독)
    gate: str | None = None  # 이 문자열이 있어야 발동 (None = 무조건)
    first_only: bool = False  # 첫 매치 하나만(구체성 우선)


def _default_attribute_token_groups_raw() -> dict[str, dict[str, Any]]:
    """속성 토큰 그룹의 코드 폴백(JSON 파일 부재/파손 시). JSON 스키마와 동일 형태로 반환한다.
    런타임 호출이라 아래 상수(_MEMBER_FLAG_TARGETS 등)가 나중에 정의돼도 된다."""
    return {
        # 활동회원/블랙리스트 등 Y/N 플래그: 부정→제외, 표면어 단독→포함. 여러 플래그 동시 매치 가능.
        "member_flag": {"neg": _MEMBER_FLAG_NEG, "pos": "", "gate": None, "first_only": False,
                        "canonicals": [[c, list(t)] for c, t in _MEMBER_FLAG_TARGETS]},
        # 자녀정보 등록: '등록/보유/있음' 문맥이 붙어야 긍정, '없/미등록' 부정→제외('자녀 선물' 등 비속성 분리).
        "children": {"neg": _CHILDREN_NEG, "pos": _CHILDREN_POS, "gate": None, "first_only": False,
                     "canonicals": [["children_registered", list(_CHILDREN_TERMS)]]},
        # 가입 디바이스(앱/PC/모바일웹): '가입' 문맥 필수, 구체성 순서로 첫 매치 하나만, 부정 없음.
        "signup_device": {"neg": None, "pos": _SIGNUP_DEVICE_SUFFIX, "gate": "가입", "first_only": True,
                          "canonicals": [[c, list(t)] for c, t in _SIGNUP_DEVICE_TARGETS]},
    }


def _load_attribute_token_groups_raw(path: Path = DEFAULT_ATTRIBUTE_TOKEN_GROUPS_PATH) -> dict[str, dict[str, Any]]:
    """attribute_token_groups.json 의 "groups" 를 읽는다. 파일 부재/파손/빈 groups 면 코드 폴백."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_attribute_token_groups_raw()
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if isinstance(groups, dict) and groups:
        return groups
    return _default_attribute_token_groups_raw()


def _coerce_attribute_token_group(raw: Any, backstop: dict[str, tuple[str, ...]] | None = None) -> _AttributeTokenGroup | None:
    """JSON 그룹 dict → _AttributeTokenGroup 스펙.

    canonicals 는 두 형태를 받는다.

      * ``"active_member"``            — canonical 만 선언(권장). 표면어는 코드의 동결 백스톱에서
        가져오고, 백스톱이 모르는 말투는 LLM 조건 슬롯 추출기가 이 닫힌 집합에서 골라 메운다.
      * ``["active_member", [표면어…]]`` — 표면어까지 파일이 들고 있던 구형. 계속 읽어 준다.

    표면어를 파일에서 뺀 이유: 그 목록은 끝이 없어 사람이 다 적을 수 없고, 다 적지 못한 만큼 조용히
    침묵한다. 파일이 소유하는 것은 **무엇이 존재하는가**(canonical)이고, 그 경계가 곧 LLM 이 지어낼
    수 없는 이유다. 백스톱에 없는 canonical 을 표면어 없이 선언하면 규칙은 침묵하고 LLM 만 채운다.
    """
    if not isinstance(raw, dict):
        return None
    backstop = backstop or {}
    canonicals: list[tuple[str, tuple[str, ...]]] = []
    for item in raw.get("canonicals", []):
        if isinstance(item, str) and item:
            canonicals.append((item, backstop.get(item, ())))
            continue
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], (list, tuple)):
            terms = tuple(t for t in item[1] if isinstance(t, str) and t)
            if item[0] and terms:
                canonicals.append((item[0], terms))
    if not canonicals:
        return None
    neg = raw.get("neg")
    gate = raw.get("gate")
    return _AttributeTokenGroup(
        canonicals=tuple(canonicals),
        neg=neg if isinstance(neg, str) and neg else None,
        pos=raw.get("pos") if isinstance(raw.get("pos"), str) else "",
        gate=gate if isinstance(gate, str) and gate else None,
        first_only=bool(raw.get("first_only", False)),
    )


def _attribute_token_groups() -> dict[str, _AttributeTokenGroup]:
    """회원속성 토큰 승격 그룹의 선언형 문법 스펙(단일 소스 = attribute_token_groups.json, 코드 폴백).

    새 단순 속성형 필터는 이 JSON 에 그룹/카노니컬 한 줄 추가 + graph_rag 레지스트리에 attribute_token
    엔트리 등록만으로 열린다(전용 _apply_* 함수 불필요). 표면어는 파일이 아니라 그룹별 동결 백스톱
    (+ eq_filters surface_terms, + LLM 조건 슬롯 추출기)이 소유한다."""
    defaults = _default_attribute_token_groups_raw()
    backstops = {
        name: {
            canonical: tuple(terms)
            for canonical, terms in raw.get("canonicals", [])
            if isinstance(canonical, str)
        }
        for name, raw in defaults.items()
    }
    out: dict[str, _AttributeTokenGroup] = {}
    for name, raw in _load_attribute_token_groups_raw().items():
        group = _coerce_attribute_token_group(raw, backstops.get(name))
        if group is not None:
            out[name] = group
    return out or {
        name: _coerce_attribute_token_group(raw, backstops.get(name))
        for name, raw in defaults.items()
    }


# 가입 디바이스 채널(REG_CHANNEL_CD): '앱/PC/모바일웹 (으)로 가입한'. 모바일웹을 PC('웹')보다 먼저
# 판정해 '모바일웹'이 '웹'으로 새지 않게 한다. '가입' 문맥이 있을 때만 발동해 로그인 채널(app_user)·
# 앱푸시 동의 등 다른 '앱' 언급과 분리한다. 실컬럼은 eq_filters signup_channel 카테고리가 소유한다.
_SIGNUP_DEVICE_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("app_signup", ("앱", "어플", "app", "어플리케이션")),
    ("mobile_web_signup", ("모바일웹", "엠웹", "모바일브라우저")),
    ("pc_signup", ("pc", "컴퓨터", "웹", "데스크탑", "데스크톱")),
)


_SIGNUP_DEVICE_SUFFIX = r"(?:으로|로|에서|을통해|를통해|앱)?가입"


# 자녀정보 등록 여부(CHILDREN_YN) 타겟: '자녀(정보) 등록/보유/있음'은 육아 관심(parent 페르소나)이
# 아니라 회원 속성(CHILDREN_YN Y/N)이다. 실컬럼은 children_registered eq_filter 가 소유한다. 정규화가
# '자녀'→parent(관심/페르소나, 회원 컬럼 미표현)로 삼켜 조건이 새므로, '등록/보유/있음' 문맥이 붙은
# 경우만 결정론으로 승격한다. 부정('없/미등록')은 exclude 로 `<> 'Y'`.
_CHILDREN_TERMS = ("자녀", "아이", "키즈")


# 부정을 먼저 판정한다 — '등록 안'/'없'.
_CHILDREN_NEG = r"(?:정보)?(?:가|이|를|을|은|는)?(?:없|미등록|미보유|등록안|등록하지\s*않|보유하지\s*않)"


_CHILDREN_POS = r"(?:정보)?(?:가|이|를|을|은|는)?(?:등록|보유|있|존재)"


# 회원 Y/N 플래그(활동회원·블랙리스트) 문맥 판정. 표면어와 canonical 매핑을 결정론 필터와
# 재작성 게이트(_member_flag_signals)가 함께 쓴다. 이 canonical 들은 eq_filters 에 있어야
# compile_member_target_conditions 가 `= 'Y'`(포함)/`<> 'Y'`(exclude.lifecycle) 로 만든다
# ([[boolean-filters-dead-registry]] 참고).
_MEMBER_FLAG_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("active_member", ("활동회원", "활성회원", "액티브회원", "활동중인회원")),
    ("blacklisted", ("블랙리스트", "발송제외고객", "차단회원")),
    ("employee", ("임직원", "직원회원")),
    ("premium_member", ("프리미엄회원", "프리미엄고객", "프리미엄")),
    ("membership_member", ("멤버십회원", "멤버십가입고객", "멤버십가입")),
    ("sns_registered", ("sns가입", "소셜가입", "간편가입", "sns회원")),
)


# 부정(제외) 접미어 — '블랙리스트가 아니', '블랙리스트 제외', '활동회원이 아닌' 등. 없으면 포함(긍정)으로 본다.
# 소비어(회원/고객)와 조사를 건너뛰고 부정 동사에 닿는다.
_MEMBER_FLAG_NEG = r"(?:인|한|중인|상태)?(?:회원|고객|사람)?(?:가|이|은|는|를|을|도|이면)?(?:아니|아닌|제외|빼|말고|배제)"
