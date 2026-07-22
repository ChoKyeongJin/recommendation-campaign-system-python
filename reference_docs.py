"""docs/data · docs/prompts 참조 파일을 프론트에 '읽기 전용'으로 노출하기 위한 모듈.

타겟팅 엔진이 실제로 읽는 지식/사전/프롬프트 파일들을 목록·본문으로 제공한다. 편집 기능은 없다
(프론트도 GET 만 프록시). 각 파일이 '무엇을 하는 파일인지' 한국어 설명(DESCRIPTIONS)을 함께 준다.

경로는 항상 두 화이트리스트 디렉터리(docs/data, docs/prompts) 안으로 제한하고, 실제 그 안에 있는
파일만 읽는다(경로 순회·심볼릭 탈출 차단). api.py 가 이 모듈을 import 해 엔드포인트로 감싼다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 노출 대상 디렉터리(카테고리). graph_rag 의 기본 경로/환경변수와 동일하게 맞추되, 무거운 의존성
# (fastembed 등)을 끌어오지 않도록 graph_rag 를 import 하지 않고 여기서 직접 해석한다.
CATEGORY_DIRS: dict[str, Path] = {
    "data": Path(os.getenv("GRAPH_RAG_DATA", "docs/data/rag_knowledge_base.json")).parent,  # docs/data
    "prompts": Path(os.getenv("GRAPH_RAG_PROMPT_DIR", "docs/prompts")),                       # docs/prompts
}

# 파일별 한국어 설명(무엇을 하는 파일인지). 키는 파일명. 목록/상세에서 그대로 보여준다.
DESCRIPTIONS: dict[str, str] = {
    # --- docs/data ---
    "member_target_filters.json": "타겟 조건→실제 컬럼·코드 매핑의 핵심 사전. 성별·회원등급·활동·주문(구매횟수)·가입일·생일 등 조건을 실DB 컬럼/코드값으로 변환하는 원천. 여기 매핑이 없으면 조건이 '실DB 미지원'으로 SQL 생성에서 빠진다.",
    "schema_catalog.json": "허용 테이블·컬럼·DB 방언 카탈로그. sql_guard와 스키마 검증이 '이 테이블/컬럼이 실재하는가'를 판정하는 기준.",
    "normalization_rules.sample.json": "구어체·동의어·오타 표현을 표준(canonical) 값으로 바꾸는 정규화 사전(성별·등급·행동 등). 신뢰도 근거로도 쓰인다. (.sample = 배포 시 실데이터로 교체하는 예시본)",
    "targeting_lexicon.json": "타겟팅 어휘 단일 출처. 대상 방향 표지(에게/한테/대상으로), 채널 신호어, 캠페인 의도어, 판매 동사, 구매이력 신호어 등을 한 곳에서 관리.",
    "dimension_catalog.sample.json": "디멘션(브랜드·카테고리 등) 카탈로그. 이름→코드 변환용 DS_SQL과 동의어를 정의해 '브랜드명'을 실제 코드로 해석한다. (.sample = 예시본)",
    "member_value_index.json": "라벨 없이 값만 언급될 때(예: 라벨 없이 브랜드명만) 값→컬럼을 찾아주는 인덱스. build_member_value_index.py 로 생성.",
    "member_metrics.json": "회원 지표(구매액·방문수 등)의 정의와 랭킹용 메타데이터.",
    "metric_lexicon.sample.json": "계산 지표(공식 기반 metric)를 인식하는 어휘 사전. (.sample = 예시본)",
    "business_policies.sample.json": "캠페인 비즈니스 정책(임계값·규칙) 정의. RAG 지식으로 적재되어 정책 유사도·근거 제시에 쓰인다. (.sample = 예시본)",
    "rag_knowledge_base.json": "RAG 지식 그래프 본체(노드·엣지). 정책·SQL 예시·스키마 등 검색 대상 지식이 모두 들어있다. build_rag_knowledge.py 산출물.",
    "sql_examples.sample.sql": "예시 SQL 모음. 검색 근거·LLM SQL 폴백의 참고 자료. (.sample = 예시본)",
    "table_relationships.md": "테이블 간 관계(조인 경로)를 정리한 문서. build_table_relationships.py 로 스키마에서 생성.",
    "metadata_ddl.sql": "메타데이터 DB(프롬프트·정책·실험 저장소) 스키마 DDL.",
    # --- docs/prompts ---
    "prompt_rewrite_system.txt": "입력 프롬프트를 표준 '타겟팅 프롬프트'로 재작성하는 시스템 프롬프트. 결과(effective_query)가 이후 파싱·SQL의 기준이 된다.",
    "prompt_normalize_system.txt": "보수적 정규화(오타·띄어쓰기만 교정) 시스템 프롬프트. 재작성 대신 최소 교정만 할 때 사용.",
    "prompt_scope_split_system.txt": "프롬프트를 '타겟팅(오디언스 조건)'과 '채널(발송·메시지 의도)' 절로 분리하는 시스템 프롬프트.",
    "prompt_reformulation_system.txt": "RAG 검색 품질을 높이기 위해 쿼리를 검색용으로 재구성하는 시스템 프롬프트.",
    "query_plan_system.txt": "Query Plan 생성 역할·제약 정의 시스템 프롬프트(canonical 값 제한, 부정 조건 처리 등).",
    "query_plan_user.txt": "질문·허용값·fallback plan을 묶어 Query Plan LLM에 넣는 사용자 템플릿.",
    "target_object_extract_system.txt": "상품/구매이력 등 '대상 객체'를 프롬프트에서 추출하는 시스템 프롬프트(정규식이 놓친 경우의 LLM 폴백용).",
    "answer_system.txt": "답변 생성 시스템 프롬프트. 검증된 SQL만 근거로 쓰도록 역할을 제한한다.",
    "answer_user.txt": "질문·Query Plan·검색 context·SQL 결과를 묶어 답변 LLM에 넣는 사용자 템플릿.",
    "message_generation_system.txt": "캠페인 메시지 생성 시스템 프롬프트. 허위 혜택 방지, 채널 제약 등 규칙 포함.",
    "message_generation_user.txt": "캠페인·타겟·SQL context로 메시지 3종을 생성하는 사용자 템플릿.",
    "message_generation_variant_user.txt": "메시지 variant를 1개만 생성하는 사용자 템플릿.",
    "message_generation_retry_user.txt": "메시지 검증 실패 시 실패 사유로 재시도·수정하는 사용자 템플릿.",
    "message_generation_tone_manner.txt": "브랜드 톤·매너·설득 포인트 규칙(고정 텍스트).",
    "extractllmjson.md": "LLM 응답에서 JSON을 안전하게 추출하는 방법을 정리한 참고 문서.",
}

# 확장자 → 표시용 포맷 라벨(프론트 하이라이트/아이콘 힌트).
_FORMAT_BY_SUFFIX = {".json": "json", ".sql": "sql", ".md": "markdown", ".txt": "text"}

# 목록에서 숨길 파일(설명 없는 보조/숨김 파일).
_HIDDEN_PREFIXES = (".", "_")


def _format_for(name: str) -> str:
    return _FORMAT_BY_SUFFIX.get(Path(name).suffix.lower(), "text")


def _iter_category(category: str, directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith(_HIDDEN_PREFIXES):
            continue
        items.append(
            {
                "category": category,
                "name": path.name,
                "size": path.stat().st_size,
                "format": _format_for(path.name),
                "description": DESCRIPTIONS.get(path.name, ""),
            }
        )
    return items


def list_reference_files() -> list[dict[str, Any]]:
    """docs/data · docs/prompts 의 파일 목록(메타데이터+설명). 본문은 포함하지 않는다."""
    result: list[dict[str, Any]] = []
    for category, directory in CATEGORY_DIRS.items():
        result.extend(_iter_category(category, directory))
    return result


def _safe_target(category: str, name: str) -> Path | None:
    """category/name 을 화이트리스트 디렉터리 안의 실제 파일 경로로 안전하게 해석한다.

    경로 순회('..')·심볼릭 탈출을 막기 위해 resolve 후 부모가 정확히 대상 디렉터리인지 확인한다.
    유효하지 않으면 None.
    """
    directory = CATEGORY_DIRS.get(category)
    if directory is None:
        return None
    # name 에 경로 구분자가 섞이면 즉시 거부(단일 파일명만 허용).
    if not name or "/" in name or "\\" in name or name.startswith(_HIDDEN_PREFIXES):
        return None
    base = directory.resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        return None
    return target


def read_reference_file(category: str, name: str) -> dict[str, Any] | None:
    """단일 참조 파일의 본문+메타데이터를 돌려준다. 없거나 경로가 부적합하면 None."""
    target = _safe_target(category, name)
    if target is None:
        return None
    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "category": category,
        "name": name,
        "size": target.stat().st_size,
        "format": _format_for(name),
        "description": DESCRIPTIONS.get(name, ""),
        "content": content,
    }
