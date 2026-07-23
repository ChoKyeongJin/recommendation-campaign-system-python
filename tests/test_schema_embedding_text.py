"""스키마 노드 임베딩 텍스트(render_columns/schema_nodes) 회귀 테스트.

배경: RAG 벡터 검색과 LLM 컨텍스트는 schema_table 노드의 text_for_embedding 을 쓰는데,
기존에는 컬럼 이름+타입만 담겨 '구매일/주문일' 같은 업무 용어(human_note)로는 스키마가
검색되지 않았다(제안 #4 — 스키마 설명 강화). human_note 를 컬럼과 함께 임베딩하고,
임베딩 모델 절단(512토큰)에 대비해 핵심 컬럼(PK/FK/important)을 앞에 배치한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_schema_embedding_text.py -q
"""

import build_rag_knowledge as b


COLUMNS = [
    {"name": "MEMO", "type": "nvarchar(100)", "human_note": "비고"},
    {"name": "ORDER_DATE", "type": "nvarchar(8)", "primary_key": True, "important": True,
     "human_note": "주문일자(YYYYMMDD, nvarchar8).  구매일  기준 컬럼"},
    {"name": "MEMBER_NO", "type": "bigint", "important": True,
     "references": {"table": "CRM_MB_BASEINFO", "column": "MEMBER_NO"},
     "human_note": "회원번호(bigint)"},
    {"name": "NO_NOTE", "type": "int"},
]


def test_human_note_is_embedded_with_column():
    text = b.render_columns(COLUMNS)
    assert "ORDER_DATE nvarchar(8) (PK, important) — 주문일자(YYYYMMDD, nvarchar8). 구매일 기준 컬럼" in text
    assert "MEMBER_NO bigint (FK CRM_MB_BASEINFO.MEMBER_NO, important) — 회원번호(bigint)" in text


def test_note_absent_renders_without_dash():
    text = b.render_columns(COLUMNS)
    assert "NO_NOTE int;" in text or text.endswith("NO_NOTE int")
    assert "NO_NOTE int —" not in text


def test_key_columns_come_first_for_truncation():
    # PK/FK/important 컬럼이 일반 컬럼(MEMO/NO_NOTE)보다 앞에 온다(그 안에서는 원래 순서 유지).
    text = b.render_columns(COLUMNS)
    assert text.index("ORDER_DATE") < text.index("MEMBER_NO") < text.index("MEMO") < text.index("NO_NOTE")


def test_schema_nodes_text_contains_business_terms():
    catalog = {
        "tables": {
            "TB_ORDER": {
                "description_llm": "고객의 상품 구매 및 주문 이력을 저장한다.",
                "columns": COLUMNS,
                "primary_key": ["ORDER_DATE"],
            }
        }
    }
    (node,) = b.schema_nodes(catalog)
    assert "구매일" in node["text_for_embedding"]
    assert "회원번호" in node["text_for_embedding"]
