"""역할별 docs/data 구조를 기존 basename 참조 API와 함께 지킨다."""

from __future__ import annotations

import reference_docs


def test_nested_data_files_keep_the_basename_api() -> None:
    files = reference_docs.list_reference_files()
    data_names = [item["name"] for item in files if item["category"] == "data"]
    assert len(data_names) == len(set(data_names)), "참조 API basename이 중복됐다"
    member_filters = next(
        item
        for item in files
        if item["category"] == "data" and item["name"] == "member_target_filters.json"
    )

    assert member_filters["relative_path"] == "runtime/sql/member_target_filters.json"
    assert member_filters["role"] == "runtime.sql"

    payload = reference_docs.read_reference_file("data", "member_target_filters.json")
    assert payload is not None
    assert payload["relative_path"] == member_filters["relative_path"]
    assert '"eq_filters"' in payload["content"]


def test_reference_file_lookup_rejects_paths() -> None:
    assert reference_docs.read_reference_file("data", "../member_target_filters.json") is None
    assert reference_docs.read_reference_file(
        "data", "runtime/sql/member_target_filters.json"
    ) is None


def test_duplicate_basenames_fail_closed(tmp_path, monkeypatch) -> None:
    for role in ("one", "two"):
        directory = tmp_path / role
        directory.mkdir()
        (directory / "duplicate.json").write_text("{}", encoding="utf-8")

    monkeypatch.setitem(reference_docs.CATEGORY_DIRS, "data", tmp_path)
    assert reference_docs.read_reference_file("data", "duplicate.json") is None
