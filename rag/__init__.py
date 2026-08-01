"""graph_rag.py 를 역할별로 나눈 패키지.

graph_rag 는 이행 기간 동안 재수출 façade 로 남는다(tests/test_graph_rag_facade.py 가
외부 소비 심볼 보존을 강제한다). 하위 모듈은 graph_rag 를 import 하지 않는다 —
의존 방향은 항상 graph_rag → rag.* 단방향이고, tests/test_module_layering.py 가 강제한다.
"""
