"""입력 패턴별 few-shot 가이드(query_plan_examples.txt) 주입 골격 회귀.

계약:
  - 예시 파일이 전부 주석/공백이면 시스템 프롬프트에 아무 것도 덧붙지 않는다(기본 무동작).
  - '#' 로 시작하는 줄은 편집자용 주석이라 LLM 프롬프트에 새어 나가지 않는다.
  - 실제 예시 줄(‘#’ 아님)이 있으면 그 본문이 시스템 프롬프트 뒤에 그대로 덧붙는다.

DB(prompt_store) 를 타지 않도록 GRAPH_RAG_PROMPT_SOURCE=file 로 강제하고, 임시 prompt_dir 로
파일 기반 경로만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_query_plan_fewshot.py -q
"""

import graph_rag as g


def _write(prompt_dir, name, content):
    (prompt_dir / name).write_text(content, encoding="utf-8")


def test_all_comment_examples_are_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_PROMPT_SOURCE", "file")
    _write(tmp_path, "query_plan_system.txt", "SYSTEM_BASE")
    _write(tmp_path, "query_plan_examples.txt", "# 편집용 주석\n#   들여쓴 주석\n\n")

    assert g._query_plan_fewshot_examples(tmp_path) == ""
    assert g._query_plan_system_prompt(tmp_path) == "SYSTEM_BASE"


def test_missing_examples_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_PROMPT_SOURCE", "file")
    _write(tmp_path, "query_plan_system.txt", "SYSTEM_BASE")

    assert g._query_plan_fewshot_examples(tmp_path) == ""
    assert g._query_plan_system_prompt(tmp_path) == "SYSTEM_BASE"


def test_real_examples_are_appended_without_comment_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_PROMPT_SOURCE", "file")
    _write(tmp_path, "query_plan_system.txt", "SYSTEM_BASE")
    _write(
        tmp_path,
        "query_plan_examples.txt",
        "# 헤더 주석(전달 금지)\n"
        "[입력 패턴별 Query Plan 가이드]\n"
        '입력 패턴: "<상품명> 구매한 고객"\n'
        "# 중간 편집 메모(전달 금지)\n"
        '기대: {"target_user": {"purchase_object": "<상품명>"}}\n',
    )

    examples = g._query_plan_fewshot_examples(tmp_path)
    assert "전달 금지" not in examples
    assert "[입력 패턴별 Query Plan 가이드]" in examples
    assert "purchase_object" in examples

    system_prompt = g._query_plan_system_prompt(tmp_path)
    assert system_prompt.startswith("SYSTEM_BASE\n\n")
    assert examples in system_prompt
