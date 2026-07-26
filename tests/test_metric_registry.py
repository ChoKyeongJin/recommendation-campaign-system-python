"""통합 지표 스펙 레지스트리(P0) — 스키마/로더 검증. graph_rag 미연결이라 이 파일이 유일한 소비자다.

개선안 P0 완료 기준: (1) 3개 지표(total_login_count/total_login_days/last_login_at)가 스펙만으로 로드·검증되고,
(2) semantic_type(scalar/ratio/date) 별 필수 계약이 강제되며, (3) 잘못된 스펙은 어느 지표 어느 필드가 왜
틀렸는지로 실패한다. 기존 파이프라인은 건드리지 않으므로 이 테스트는 기존 749 스위트와 독립이다.

실행: python -m pytest tests/test_metric_registry.py -q
"""

import json

import pytest

import metric_registry as mr


@pytest.fixture(scope="module")
def registry() -> mr.MetricRegistry:
    return mr.MetricRegistry.load()


# ── 로드/등록 ────────────────────────────────────────────────────────────────────────────
def test_three_seed_metrics_load(registry):
    ids = {s.metric_id for s in registry.all()}
    assert {"total_login_count", "total_login_days", "last_login_at"} <= ids


def test_scalar_login_count_shape(registry):
    spec = registry.by_id("total_login_count")
    assert spec is not None and spec.semantic_type == "scalar" and spec.data_type == "integer"
    assert spec.source is not None and spec.source.qualified == "B.TOTAL_LOGIN_CNT"
    assert spec.units is not None and "회" in spec.units.expressions
    assert spec.zero_semantics.get("missing_as_zero") is True
    assert spec.time_semantics.supports_recent_period is False


def test_scalar_login_days_declares_day_unit(registry):
    # 개선안 핵심: '일' 단위가 코드가 아니라 스펙에 있다('30일 이상' 파싱의 근거).
    spec = registry.by_id("total_login_days")
    assert spec is not None and spec.units is not None
    assert spec.units.canonical == "day" and spec.units.expressions == ("일",)
    assert "last_login_at" in spec.priority_hints.get("conflicts_with", [])


def test_date_metric_declares_recency_and_windows(registry):
    spec = registry.by_id("last_login_at")
    assert spec is not None and spec.semantic_type == "date"
    assert spec.source is not None and spec.source.date_format == "yyyyMMdd"
    ts = spec.time_semantics
    assert ts.supports_recent_period is True and ts.anchor == "getdate" and ts.default_days == 30
    # recency(>=)·inactivity(<) 를 한 지표로 표현 — 흩어진 recent_login_target/activity_filters 를 통합.
    assert ts.windows["recent"]["operator"] == ">="
    assert ts.windows["inactive"]["operator"] == "<" and ts.windows["inactive"]["include_null"] is True


def test_alias_resolution_prefers_longest(registry):
    hits = dict((s.metric_id, alias) for s, alias in registry.resolve_alias("누적 로그인 일수가 30일 이상"))
    assert hits.get("total_login_days") == "누적 로그인 일수"  # 짧은 '로그인 일수'가 아닌 최장 매칭


def test_ratio_default_formula_renders():
    # 아직 등록 지표는 아니지만(다음 단계), 스키마가 비율을 담을 수 있어야 한다 — formula 치환 확인.
    spec = mr._parse_spec({
        "metric_id": "daily_avg_login_count", "name": "하루 평균 로그인 횟수",
        "semantic_type": "ratio", "data_type": "decimal",
        "aliases": {"nouns": ["하루 평균 로그인 횟수"]},
        "units": {"canonical": "count", "expressions": ["회"]},
        "derivation": {
            "numerator": {"table": "CRM_MB_BASEINFO", "alias": "B", "column": "TOTAL_LOGIN_CNT"},
            "denominator": {"table": "CRM_MB_BASEINFO", "alias": "B", "column": "TOTAL_LOGIN_DAYS"},
        },
        "operators": {"allowed": ["GTE", "LTE", "BETWEEN"]},
    })
    assert spec.derivation.render() == "CAST(B.TOTAL_LOGIN_CNT AS FLOAT) / NULLIF(B.TOTAL_LOGIN_DAYS, 0)"


# ── 검증 실패 경로(스키마가 실제로 강제되는지) ───────────────────────────────────────────────
def _base_scalar() -> dict:
    return {
        "metric_id": "x", "name": "X", "semantic_type": "scalar", "data_type": "integer",
        "aliases": {"nouns": ["엑스"]}, "units": {"canonical": "count", "expressions": ["회"]},
        "source": {"table": "T", "alias": "B", "column": "C"},
        "operators": {"allowed": ["GTE"]},
    }


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("metric_id"), "metric_id"),
    (lambda d: d.update(semantic_type="weird"), "semantic_type"),
    (lambda d: d.update(data_type="blob"), "data_type"),
    (lambda d: d.__setitem__("aliases", {"nouns": []}), "aliases.nouns"),
    (lambda d: d.__setitem__("units", {"canonical": "count", "expressions": []}), "units.expressions"),
    (lambda d: d.pop("source"), "source"),
    (lambda d: d.update(operators={"allowed": ["FOO"]}), "미지원 토큰"),
    (lambda d: d.update(time_semantics={"supports_recent_period": True}), "supports_recent_period=false"),
])
def test_scalar_validation_rejects_bad_spec(mutate, needle):
    data = _base_scalar()
    mutate(data)
    with pytest.raises(mr.MetricSpecError) as exc:
        mr._parse_spec(data)
    assert needle in str(exc.value)


def test_date_requires_windows():
    data = {
        "metric_id": "d", "name": "D", "semantic_type": "date", "data_type": "date_string",
        "aliases": {"nouns": ["최근"]}, "source": {"table": "T", "alias": "B", "column": "C"},
        "time_semantics": {"supports_recent_period": True},  # windows 누락
        "operators": {"allowed": ["WITHIN"]},
    }
    with pytest.raises(mr.MetricSpecError) as exc:
        mr._parse_spec(data)
    assert "windows" in str(exc.value)


def test_ratio_requires_derivation():
    data = {
        "metric_id": "r", "name": "R", "semantic_type": "ratio", "data_type": "decimal",
        "aliases": {"nouns": ["비율"]}, "units": {"canonical": "count", "expressions": ["회"]},
        "operators": {"allowed": ["GTE"]},
    }
    with pytest.raises(mr.MetricSpecError) as exc:
        mr._parse_spec(data)
    assert "derivation" in str(exc.value)


def test_duplicate_metric_id_rejected(tmp_path):
    for i in (1, 2):
        (tmp_path / f"dup{i}.json").write_text(json.dumps(_base_scalar()), encoding="utf-8")
    with pytest.raises(mr.MetricSpecError) as exc:
        mr.MetricRegistry.load(tmp_path)
    assert "중복" in str(exc.value)


# ── 카탈로그 lint(DB 이식성 연결) ─────────────────────────────────────────────────────────
def test_lint_flags_missing_column(registry):
    catalog = {"CRM_MB_BASEINFO": {"TOTAL_LOGIN_CNT", "LAST_LOGIN_DATE"}}  # TOTAL_LOGIN_DAYS 누락
    warnings = registry.lint_against_catalog(catalog)
    assert any("TOTAL_LOGIN_DAYS" in w for w in warnings)


def test_lint_clean_when_all_columns_present(registry):
    catalog = {"CRM_MB_BASEINFO": {"TOTAL_LOGIN_CNT", "TOTAL_LOGIN_DAYS", "LAST_LOGIN_DATE"}}
    assert registry.lint_against_catalog(catalog) == []
