# 통합 지표 스펙 (`docs/data/metrics/*.json`)

신규 지표 추가 구조 개선안의 **단일 진실 소스**. 지표별 정보(컬럼·별칭·단위·NULL/0건·최근성·우선순위·
집계·파생)를 코드가 아니라 이 스펙으로 관리한다. 한 파일 = 한 지표(`metric_id.json`).

> **현재 상태(P0):** 스키마 + 로더([../../../metric_registry.py](../../../metric_registry.py))만 존재하며
> **graph_rag 파이프라인에는 아직 연결되지 않았다.** 기존 동작은 여전히 `member_target_filters.json`
> (numeric_filters / ratio_filters / activity_filters / recent_login_target)이 구동한다. 이후 단계에서
> 단위 → zero_semantics → 최근성 → 비율 순으로 이 레지스트리로 점진 교체한다.

## semantic_type 별 최소 계약

| semantic_type | 필수 | 용도 |
|---|---|---|
| `scalar` | `source`, `units.expressions`, `time_semantics.supports_recent_period=false` | 정수/소수/금액/횟수/일수 임계·범위·랭킹·평균대비 |
| `ratio`  | `derivation.{numerator,denominator}`, `units` | 파생 비율(하루 평균 = CNT/DAYS) |
| `date`   | `source.date_format`, `time_semantics.{supports_recent_period=true, windows}` | 최근성/미접속(YYYYMMDD 창) |

스키마 규범은 [metric_registry.py](../../../metric_registry.py) 상단 docstring + 데이터클래스다. 위반 시
로더가 `MetricSpecError`(어느 지표 어느 필드가 왜 틀렸는지)를 던진다.

## 새 지표 추가 절차(목표 상태)

1. 이 디렉터리에 `<metric_id>.json` 추가(기존 파일 복사 후 값 교체).
2. 회귀 코퍼스에 `질의 → 기대 술어` 테스트 데이터 추가.
3. 끝 — **같은 semantic_type 이면 파서/컴파일러 코드 수정 불필요.**
   (새 semantic_type 을 처음 도입할 때만 전략 1개를 코드에 추가.)

## 포맷 참고

개선안 예시는 YAML 이지만, 레포 전 설정이 JSON 이라 의존성/포맷을 통일해 JSON 으로 둔다(키 구조는 동일 —
나중에 YAML 로 바꿔도 스키마는 그대로).
