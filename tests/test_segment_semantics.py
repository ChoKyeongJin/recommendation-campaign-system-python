"""세그먼트 스펙의 소유권 분리 계약 + 해석 동작.

이 스펙은 두 파일로 갈라져 있고, 가른 이유가 계약이다.

  * `segment_metrics.json`(접지) — 어느 테이블/컬럼에서 오는가, 컴파일러가 그 연산을 실제로 구현했는가.
    틀리면 잘못된 SQL 이거나 못 지킬 약속이므로 추론으로 채우면 안 되는 계층.
  * `segment_lexicon.json`(어휘) — 사람이 쓰는 표면 표현. 끝없이 늘어나는 목록이라 나중에 LLM 슬롯
    추출로 대체·보강할 수 있는 계층.

여기 테스트가 막는 것은 '두 계층이 다시 섞이는 것'이다. 표면어가 접지 파일로 새거나, 한쪽에만 있는
지표가 생기면(어휘 오타 → 영영 인식 안 되는 지표) 로드 시점에 죽어야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import segment_semantics as ss

GROUNDING_PATH = Path("docs/data/segment_metrics.json")
LEXICON_PATH = Path("docs/data/segment_lexicon.json")

# 접지 파일이 소유하지 않는 키(어휘 파일 소유).
SURFACE_KEYS = ("aliases", "units", "usage_verbs", "negative_expressions")


@pytest.fixture(scope="module")
def registry() -> ss.SegmentSemanticsRegistry:
    return ss.SegmentSemanticsRegistry.load()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 소유권 분리 계약 ──────────────────────────────────────────────────────────────────────
def test_grounding_file_holds_no_surface_words() -> None:
    """표면어는 접지 파일에 있으면 안 된다 — 있으면 '어휘 한 줄 추가'가 접지 파일을 건드리게 된다."""
    for metric_id, data in _load(GROUNDING_PATH)["metrics"].items():
        stray = [key for key in SURFACE_KEYS if key in data]
        assert not stray, f"{metric_id}: 표면어 키가 접지 파일에 있음 {stray}"


def test_lexicon_file_holds_no_grounding() -> None:
    """반대 방향도 막는다 — 물리 소스/지원 여부가 어휘 파일에서 선언되면 안 된다."""
    for metric_id, data in _load(LEXICON_PATH)["metrics"].items():
        stray = [key for key in ("source", "aggregation", "capabilities", "formula") if key in data]
        assert not stray, f"{metric_id}: 접지 키가 어휘 파일에 있음 {stray}"


def test_two_files_cover_the_same_metrics(registry: ss.SegmentSemanticsRegistry) -> None:
    assert set(_load(GROUNDING_PATH)["metrics"]) == set(_load(LEXICON_PATH)["metrics"])
    for metric in registry.metrics.values():
        assert metric.aliases, f"{metric.metric_id}: 별칭이 없으면 문장에서 영영 인식되지 않는다"


def test_vocabulary_for_unknown_metric_fails_loading(tmp_path: Path) -> None:
    """어휘에만 있는 지표(오타)는 조용히 무시되지 않고 로드에서 죽어야 한다."""
    lexicon = _load(LEXICON_PATH)
    lexicon["metrics"]["coupon_usage_kount"] = {"aliases": ["오타 지표"]}
    broken = tmp_path / "segment_lexicon.json"
    broken.write_text(json.dumps(lexicon, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ss.SegmentSemanticsError, match="coupon_usage_kount"):
        ss.SegmentSemanticsRegistry.load(GROUNDING_PATH, broken)


def test_metric_without_vocabulary_fails_loading(tmp_path: Path) -> None:
    """접지에만 있는 지표는 별칭이 없어 절대 매칭되지 않는다 — 조용한 사각지대 대신 로드 실패."""
    lexicon = _load(LEXICON_PATH)
    lexicon["metrics"].pop("purchase_count")
    broken = tmp_path / "segment_lexicon.json"
    broken.write_text(json.dumps(lexicon, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ss.SegmentSemanticsError, match="purchase_count"):
        ss.SegmentSemanticsRegistry.load(GROUNDING_PATH, broken)


def test_alias_collision_between_metrics_fails_loading(tmp_path: Path) -> None:
    lexicon = _load(LEXICON_PATH)
    lexicon["metrics"]["purchase_count"]["aliases"].append("쿠폰 사용 횟수")
    broken = tmp_path / "segment_lexicon.json"
    broken.write_text(json.dumps(lexicon, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ss.SegmentSemanticsError, match="별칭 충돌"):
        ss.SegmentSemanticsRegistry.load(GROUNDING_PATH, broken)


# ── 해석 동작(분리 전후 불변) ─────────────────────────────────────────────────────────────
def test_threshold_keeps_operator_and_value(registry: ss.SegmentSemanticsRegistry) -> None:
    interp = ss.interpret("쿠폰 3개 이상 사용한 고객", registry)
    assert interp is not None
    assert interp.condition.type == "metric_filter"
    assert (interp.condition.operator, interp.condition.value) == ("gte", 3.0)
    assert interp.capability.supported


def test_negative_expression_becomes_absence(registry: ss.SegmentSemanticsRegistry) -> None:
    interp = ss.interpret("쿠폰 한 번도 사용하지 않은 고객", registry)
    assert interp is not None
    assert interp.condition.type == "existence_filter"
    assert interp.condition.exists is False


def test_existence_predicate_comes_from_grounding(registry: ss.SegmentSemanticsRegistry) -> None:
    """EXISTS 술어의 컬럼은 어휘가 아니라 접지(source)에서만 온다."""
    interp = ss.interpret("쿠폰 사용한 고객", registry)
    assert interp is not None
    source = registry.metrics["coupon_usage_count"].source
    assert interp.existence_predicate == f"{source['alias']}.{source['column']} > 0"


def test_unsupported_ranking_reports_declared_code(registry: ss.SegmentSemanticsRegistry) -> None:
    interp = ss.interpret("쿠폰 사용 횟수 상위 100명", registry)
    assert interp is not None
    assert interp.condition.type == "ranking"
    assert interp.condition.limit == 100
    assert not interp.capability.supported
    assert interp.capability.code == "coupon_usage_count_ranking_unsupported"


def test_derived_metric_keeps_formula_even_when_unsupported(registry: ss.SegmentSemanticsRegistry) -> None:
    """미지원이라고 분모를 버리지 않는다 — 의미 노드는 온전히 남고 게이트만 막는다."""
    interp = ss.interpret("쿠폰 한 개당 구매금액 5만원 이상", registry)
    assert interp is not None
    assert interp.condition.value == 50000
    assert interp.condition.formula == {
        "type": "ratio",
        "numerator": "purchase_amount",
        "denominator": "coupon_usage_count",
    }
    assert not interp.capability.supported


def test_non_coupon_query_is_left_to_other_paths(registry: ss.SegmentSemanticsRegistry) -> None:
    assert ss.interpret("30대 여성 VIP 고객", registry) is None
