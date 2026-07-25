"""member_target_filters.json 단일 진실 소스 회귀 — 죽은 설정·폴백 drift 가드.

배경(docs/operations/db_portability_audit.md §4-B): 레지스트리 JSON 이 스키마 지식의 단일
소스여야 DB 스왑이 "JSON 재매핑"으로 끝난다. 과거 로더가 코드 기본값에 선언된 키만 머지해
base_entity/region_target/purchase_product_target 같은 섹션이 파일에 있어도 조용히 버려지는
'죽은 설정' 함정이 있었다(boolean_filters 사례와 동일 원인 — 수정됨).

고정 내용:
  - 로더가 JSON 의 모든 최상위 키를 싣는다(선언 키 선별 머지로 회귀 금지).
  - 코드 기본값(폴백)의 모든 키는 JSON 에도 존재한다 — 파일이 있을 때 폴백이 조용히
    한 섹션을 대신 공급하는 일이 없다(drift 방향 가드).
  - base_entity 접근자가 JSON 값을 실제로 읽는다(폴백 문자열이 아니라).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_registry_single_source.py -q
"""

import json

import graph_rag as g


def _registry_file() -> dict:
    return json.loads(g.DEFAULT_MEMBER_TARGET_FILTERS_PATH.read_text(encoding="utf-8"))


def test_loader_merges_all_json_top_level_keys():
    payload = _registry_file()
    missing = [key for key in payload if key not in g._MEMBER_TARGET_FILTERS]
    assert not missing, f"로더가 JSON 최상위 키를 버렸다(죽은 설정 함정 회귀): {missing}"
    # 대표적으로 과거에 버려지던 섹션들이 실제로 실려 있어야 한다.
    for key in ("base_entity", "region_target", "purchase_product_target", "date_expression_registry"):
        assert key in g._MEMBER_TARGET_FILTERS, key


def test_default_fallback_keys_all_exist_in_json():
    payload = _registry_file()
    orphaned = [key for key in g._DEFAULT_MEMBER_TARGET_FILTERS if key not in payload]
    assert not orphaned, (
        "코드 폴백에만 있는 섹션 발견 — 파일이 정상일 때도 폴백이 그 섹션을 공급하게 되어 "
        f"JSON 단일 소스가 깨진다. JSON 에도 추가할 것: {orphaned}"
    )


def test_base_entity_accessors_read_json_values():
    base = _registry_file()["base_entity"]
    # 접근자가 폴백 리터럴이 아니라 JSON 값을 돌려준다(값 자체가 같더라도 dict 원본으로 확인).
    assert g._member_base_entity() == base
    assert g._member_table() == base["table"]
    assert g._member_alias() == base["alias"]
    assert g._member_key_column() == base["member_key"]
    assert g._member_login_id_column() == base["login_id_key"]
    assert g._member_from_clause() == f"FROM {base['table']} {base['alias']}"


def test_region_and_purchase_registries_read_json():
    payload = _registry_file()
    sido = payload["region_target"]["columns"]["sido"].split(".")[-1]
    sigungu = payload["region_target"]["columns"]["sigungu"].split(".")[-1]
    assert g._member_region_short_columns() == (sido, sigungu)
    assert g._purchase_product_registry() == payload["purchase_product_target"]
