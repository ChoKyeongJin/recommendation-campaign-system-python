# 범용 Temporal Semantic Framework (`temporal_ir/`)

## 무엇을 푸는가

시간 표현을 문구별로 처리하면 새 표현마다 분기가 하나씩 늘고, 그 분기가 어떤 데이터 형태를
요구하는지는 코드 곳곳에 흩어진다. 이 계층은 시간 조건을 **세 요소의 분리**로 다룬다.

```
시간 의미(무엇을 묻는가) + 데이터가 보유한 이력 형태(무엇을 관측할 수 있는가) + 시간 연산(어떻게 계산하는가)
```

목표는 하나다: **새 시간 속성을 추가할 때 문구·컬럼명·도메인 값에 대한 Python 분기를 추가하지
않는다.** 새 월별 속성은 `docs/data/runtime/semantics/temporal_bindings.json` 항목 하나이며
`temporal_ir/` 의 코드는 바뀌지 않는다(회귀 테스트:
`tests/test_temporal_lowering.py::test_a_new_monthly_attribute_needs_only_catalog_declarations`).

## 계층

```
자연어(도메인 계층 소유 — 이 패키지 밖)
  → Canonical Temporal Semantic IR      temporal_ir/semantic_ir.py
  → metric · temporal binding 해석       temporal_ir/catalog.py
  → 시간 의미와 데이터 역량 검증          temporal_ir/registry.py (계약)
  → Temporal Operator Registry           temporal_ir/operators.py (계약 + lowerer)
  → Execution IR 로 lowering              temporal_ir/lowering.py → event_ir
  → SQL                                   event_compiler
  → Lowering Receipt · Coverage 판정      temporal_ir/lowering.py
```

`event_compiler` 는 `TemporalCondition` 을 **보지 않는다**. 실행 IR(`event_ir`)만 받는다 —
그 경계가 있어야 "시간 의미가 어디서 사라졌는가"를 물을 수 있다.

이 리팩터에서 `event_ir` 과 `event_compiler` 는 **한 줄도 바뀌지 않았다**. 필요한 실행 모양이
이미 전부 있었기 때문이다(아래 lowering 표 참조).

## 상위 IR: 조합이지 연산자 문자열이 아니다

```python
TemporalCondition(
    metric="member.grade",                    # 무엇을
    binding="member.grade.monthly_snapshot",  # 어떤 관측 방식으로 (생략하면 resolver 가 고른다)
    selector=AsOfSelector(...),               # 어느 시점/구간의 관측을
    quantifier=ExistsQuantifier(),            # 몇 개가 성립해야
    predicate=StatePredicate(...),            # 각 관측에서 무엇이 성립해야
    evidence=Evidence(...),                   # 원문 근거(없으면 영수증을 발급하지 않는다)
)
```

연산자 이름은 **구조에서 파생한다**(`registry.resolve_operator_name`). 생산자가 이름을 지어
넣지 않으므로 `every_bucket_as_of` 같은 합성 이름이 늘어나지 않는다.

`options: dict` 는 없다. 선택 전략·버킷·missing/null/coverage 정책·전이 모드·빈 구간 정책은
전부 타입이거나 enum 이며, 잘못된 조합은 객체 생성이나 계약 검증에서 막힌다.

## 지원 연산자 (파생 표 — 손 목록이 아니다)

`create_default_temporal_operator_registry().documentation()` 에서 그대로 나온다.
`X` 는 오타가 아니라 **선언된 미지원**이다: 의미는 정의되어 있고 이름도 있지만 정확한 SQL 을
만들 수 없어 사유와 함께 fail-close 한다.

| operator | lowering | 받는 표현 | 요구 관측 능력 |
| --- | :---: | --- | --- |
| `temporal.as_of` | O | current_only, periodic_snapshot, validity_interval | supports_point_state |
| `temporal.previous_bucket` | O | periodic_snapshot, validity_interval | supports_point_state |
| `temporal.in_window` | O | event_log, periodic_snapshot, singleton, validity_interval | – |
| `temporal.none` | O | event_log, periodic_snapshot, singleton, validity_interval | – |
| `temporal.all` | O | event_log, periodic_snapshot, validity_interval | supports_ordered_observations |
| `temporal.every_bucket` | O | periodic_snapshot, validity_interval | supports_complete_bucket_enumeration |
| `temporal.consecutive_buckets` | X | periodic_snapshot, event_log, validity_interval | supports_ordered_observations |
| `temporal.unchanged_observations` | O | event_log, periodic_snapshot, validity_interval | supports_ordered_observations |
| `temporal.direct_transition` | O | periodic_snapshot | supports_ordered_observations |
| `temporal.latest_in_window` | O | latest_only, singleton | supports_point_state |
| `temporal.before` / `temporal.after` | O | event_log, periodic_snapshot, singleton, validity_interval | – |
| `temporal.within_after` | O | event_log, singleton | supports_all_occurrences |
| `temporal.within_before` | X | event_log, singleton | supports_all_occurrences |
| `temporal.previous_observation` | X | 이력 표현 전부 | supports_ordered_observations |
| `temporal.previous_distinct_value` | X | 이력 표현 전부 | supports_ordered_observations |
| `temporal.changed_between_endpoints` | X | 이력 표현 전부 | supports_ordered_observations |
| `temporal.changed_within_window` | X | 이력 표현 전부 | supports_ordered_observations |
| `temporal.change_count` | X | 이력 표현 전부 | supports_intra_bucket_changes |
| `temporal.throughout` | X | validity_interval | supports_continuous_validity |

미지원 여섯은 같은 뿌리를 갖는다: **주체별 정렬과 행 선택**(PartitionBy/OrderBy/LimitPerEntity/Lag)
primitive 가 실행 IR 에 없다. 그것을 추가하면 lowerer 만 채우면 된다(계약은 이미 선언되어 있다).

`temporal.consecutive_buckets`('3개월 연속')가 여섯 번째다. 앵커가 있는 '최근 N칸 연속'은
그 구간의 칸 전칭(`temporal.every_bucket`)과 **같은 집합**이므로 그쪽으로 정확히 표현되고,
앵커가 없는 '아무 N칸이나 연속'만 이 연산자로 닫힌다. 성립한 칸의 총 개수 비교로 근사하면
흩어진 칸도 통과해 다른 집합이 나가므로 근사하지 않는다.

기존 대문자 qualifier(`temporal_semantics.AS_OF` 등)는 **입력 호환 별칭**으로만 유지한다
(`registry.LEGACY_OPERATOR_ALIASES`). 새 직렬화는 namespace 이름만 내보낸다.

## 낮추는 다섯 가지 모양

전부 기존 `event_ir` 노드의 조합이다. 문장 하나를 위한 노드는 만들지 않는다.

| 의미 | 실행 IR |
| --- | --- |
| 시점 상태 | `Exists(Filter(Source, And(TimeFilter(칸), 값비교)))` |
| 구간 존재 | `Exists(Filter(Source, And(TimeFilter(구간), 술어…)))` |
| 구간 부재 | `Not(Exists(...))` |
| 관측 전칭 | `And(Exists(구간), Not(Exists(구간 ∧ ¬술어)))` |
| 칸 전칭 | `Comparison(Aggregate(count distinct 시각), '=', 기대 칸 수)` |

`current_only` 관측은 시간 조건 없이 주체 행을 직접 비교하고, `latest_only` 관측은
`binding="subject_column"` 이라 컴파일러가 EXISTS 없이 컬럼 비교로 렌더한다.

## 선언 카탈로그

`temporal_bindings.json` 은 metric(무엇을)과 binding(어떻게 관측하는가)을 나눠 선언한다.

```
member.grade                      metric — 값 도메인 grade, 비교 연산자 집합
  member.grade.current            binding — subject.EMART_GRADE_CD, current_only
  member.grade.monthly_snapshot   binding — MONTHCRMINFO.ZTS_GRADE, periodic_snapshot(month)
```

binding 은 lowering 이 필요로 하는 **모든** 사실을 담는다: 소스, 상관키, 값·시각 필드,
한 시점의 행 수와 동점자 정책, 칸 단위와 저장 표기, 칸 대표 의미(`bucket_semantics`),
받을 수 있는 연산자, 관측 능력 8종, 적재 범위.

로딩(`load_temporal_catalog`)은 물리 카탈로그와 **교차검증**한다: 소스 존재, 필드 존재와 소유
소스, 값 도메인 일치, 상관키 일치, 칸 단위 = 저장 컬럼 grain, storage_codec 일치, row_identity
컬럼 실재, 유일성 주장과 row_identity 정합, 연산자 이름 등록 여부. 어긋나면 이름을 대며 실패한다.

### 관측 능력(§8)

representation **이름으로 능력을 추론하지 않는다**. 여덟 항목을 전부 선언해야 로딩된다.

```
supports_point_state / supports_ordered_observations / supports_exact_transition_time /
supports_intra_bucket_changes / supports_continuous_validity /
supports_complete_bucket_enumeration / supports_event_count / supports_all_occurrences
```

`supports_all_occurrences` 가 `latest_only` 와 `event_log` 를 가른다. 마지막 로그인만 아는
표현은 "구간에 로그인이 있었는가"에 답할 수 없고, 답할 수 있는 것은 "마지막 로그인이 그
구간인가"(`temporal.latest_in_window`)뿐이다. 두 질문은 다른 quantifier 이므로 절대 섞이지 않는다.

## 실패의 네 가지 결말

| status | 뜻 | SQL |
| --- | --- | :---: |
| `compiled` | 실행 IR + 영수증 | O (적재 경고가 붙을 수 있다) |
| `unsupported` | 데이터 표현이 그 질문에 답할 수 없다 | X (정책과 무관) |
| `invalid` | 요청 자체가 성립하지 않는다(선언 밖 값, 모호한 관측) | X |
| `blocked_by_coverage` | 의미는 표현되지만 적재가 없고 정책이 block 이다 | X |

**의미 지원 여부와 적재 여부는 다른 축이다.** coverage 기본 정책은 `advise` — 적재 범위 밖이어도
SQL 을 만들고 경고를 붙인다(0건은 정상적인 답이다). `block` 은 요청 문맥에서 켠다.

적재 판정은 시작·끝 두 날짜만 보지 않는다. 범위 안이어도 중간 파티션이 비면
`supported_but_incomplete` 이고 결측 칸 목록을 함께 낸다.

## 영수증과 부분 합성 차단

성공한 lowering 은 `TemporalLoweringReceipt` 를 낸다: 근거 구간, metric, binding, operator,
selector/quantifier/predicate, 선택 전략, **정규화된 절대 구간**, 기준 순간, 시간대, 칸 단위,
값 도메인과 canonical 값, 비교 연산자, subject 상관키, 기대 칸 수, operator/binding 버전,
낮춘 트리의 해시, 적재 판정과 경고.

영수증은 lowerer 가 "했다고 말한 것"이 아니라 **낮춘 트리를 되읽어**(`_composition_gaps`)
확인한 뒤 발급한다. 소스·시간 조건·값 비교 중 하나라도 트리에 없으면 SQL 을 내지 않는다.

`compose_audience` 는 여러 조건을 **전부 또는 아무것도**로 합성한다. 시간 조건 하나가 실패했는데
나머지로 SQL 을 내면 사용자가 요청하지 않은 더 넓은 집합이 나간다.

## 시간 정책

> 이 절은 **월별 스냅샷 관측**의 시간 의미다. 사건(주문 등)이 일어난 날짜·시각의 정책 —
> 시각 토큰의 단위, `까지`/`이후` 의 열린 경계, 반복 시각대, 주 경계 — 은 별도 계층이며
> `clock_and_calendar_semantics.md` 가 소유한다. 겹치는 규칙(시간대 `Asia/Seoul`, 반개구간)은
> 두 문서가 같은 값을 쓴다.

- 기준 시각은 `TemporalRequestContext.now`(timezone-aware) 하나뿐이다. 이 패키지에 `now()` 는 없다.
- 모든 상대 표현은 요청 시각으로 **절대 구간**으로 확정된다. 롤링 창도 실행 시점 함수가 아니라
  요청 시각 기준으로 고정된다(같은 입력·같은 기준 시각 → 같은 SQL).
  기존 슬롯 경로의 `RollingWindow`(GETDATE 컷오프)와 다른 정책이며, 재현성을 택한 결과다.
- 구간은 반개구간 `[start, end)`. `Boundary.END` 는 칸 **안의 마지막 순간**이다.
- 업무 시간대 기본은 `Asia/Seoul`. 월 경계는 시간대에 따라 실제로 다른 순간이다.
- 요청 구간이 적재 칸에 맞지 않으면 근사하지 않는다. 일 단위 적재에서만 저장 정밀도(날짜)만큼
  확장하고 그 사실을 영수증 경고(`window_expanded_to_day_grain`)로 남긴다.
- **시점 정밀도는 anchor 의 종류가 아니라 정밀도로 판정한다.** 칸 단위(월 이상) 적재는 칸 안의
  어떤 순간도 대표하지 못하므로, 절대 시각·'지금'·칸보다 잘게 나눈 상대 시점은 전부
  `temporal_anchor_grain_too_fine` 으로 닫힌다. 달력 시점(지난달 말)만 답할 수 있고, 그때도
  binding 이 `bucket_semantics` 로 그 끝을 대표한다고 선언해야 한다.
- `missing_policy=error` 는 적재 선언이 공백을 증명하면 `blocked_by_coverage` 로 답한다.
  '관측 없음'을 '조건 불성립'으로 조용히 바꾸지 않겠다는 요청이기 때문이다.
- 낮출 수 없는 정책은 계약에서 거절한다. 예: `temporal.all` 은 `null_policy=treat_as_mismatch`
  만, 나머지 구간 연산자는 `exclude` 만 받는다(실행 IR 에 IS NULL 술어가 없다).

## 확장 방법

| 하려는 일 | 바꾸는 것 |
| --- | --- |
| 기존 연산자를 쓰는 새 속성 | `temporal_bindings.json` 에 metric + binding 항목 (Python 무변경) |
| 기존 표현에 새 연산자 | `operators.operator_definitions()` 에 정의 하나 + 공통 lowerer |
| 새 데이터 보유 형태 | `Representation` 값 + 관측 능력 선언 + (필요시) 범용 실행 primitive |
| 새 시간 의미 축 | `semantic_ir` 의 selector/quantifier/predicate 타입 하나 (문구별 노드는 금지) |

## 검토에서 닫은 구멍 (2026-08-05 적대적 검토)

4개 렌즈 · 12 에이전트 검토에서 확인된 결함을 전부 수정하고 회귀 테스트로 고정했다.

| 결함 | 수정 |
| --- | --- |
| 시점 정밀도 검사가 상대 시점에만 걸려 절대 시각·'지금'이 월 칸으로 접힘 | `_anchor_precision_issues` 로 정밀도 기준 판정 |
| 전이 술어가 as_of 의 시점 계약을 건너뜀 | `_transition_issues` 가 `_point_state_issues` 합성 |
| `null_policy=error` 등 낮출 수 없는 정책이 기본값과 같은 SQL 로 나감 | 구간 연산자 `accepted_null_policies` 축소 |
| `missing_policy=error` 가 아무 데서도 소비되지 않음 | 적재 판정과 결합해 `blocked_by_coverage` |
| `every_bucket` + 무한 구간이 결과 대신 예외를 던짐 | `temporal_unbounded_bucket_count` 로 거절 |
| 결측 파티션만 선언한 coverage 가 '선언 없음'으로 무시됨 | `CoverageSpec.declared` 를 모든 적재 사실로 확장 |
| 답할 수 있는 binding 이 둘일 때 이름 순서로 임의 선택 | 실행 경로에서 `temporal_binding_ambiguous` fail-close |
| 값 필드 없는 관측 + 값 술어가 예외로 합성 전체를 죽임 | `temporal_value_field_unavailable` 결과 |
| 관계 폭의 월·연 단위가 30일·365일로 조용히 환산 | 일·주 단위만 허용 |
| 열린 구간이 quantifier 를 잃어 '이전에 한 번도 없음' 표현 불가 | `lower_open_window` 가 극성 유지 |
| 합성 경계가 다른 컴파일러 조건의 근거를 요구하지 않음 | 합성 트리 전체에 `validate_evidence` |

## 자연어 생산자 (`temporal_claims`)

2026-08-05 에 생산자가 생겼고, 그것으로 이 계층이 라이브 경로에 배선됐다. 생산자는 **판정을
새로 하지 않는다** — 표면형→범용 연산자는 `targeting_domain.temporal_lexicon()`(닫힌 집합은
`temporal_semantics`)이, 값 표면어→canonical 값은 `canonical_audience_claims`가, 전이 값 쌍의
어순·방향 검증은 `transition_claims`/`transition_metrics`가 이미 소유한다. 남은 일은 그
연산자를 `selector × quantifier × predicate` 조합으로 옮기는 것이고, 그 사상은
`temporal_claims._OPERATOR_PLANS` **선언표 한 곳**이다(문형별 분기 없음).

| 범용 연산자 | 조합 | 파생 이름 |
| --- | --- | --- |
| `AS_OF` | AsOf + Exists + State | `temporal.as_of` |
| `IMMEDIATELY_PRECEDING` | Previous(bucket) + Exists + State | `temporal.previous_bucket` |
| `WITHIN_INTERVAL` · `AT_LEAST_ONCE_IN_INTERVAL` | Window + Exists + State | `temporal.in_window` |
| `NEVER_IN_INTERVAL` | Window + None + State | `temporal.none` |
| `THROUGHOUT_INTERVAL` | Window + AllObservations + State | `temporal.all` |
| `EVERY_SUBINTERVAL` | Window(bucket) + EveryBucket + State | `temporal.every_bucket` |
| `UNCHANGED_THROUGHOUT` | Window + AllObservations + Unchanged | `temporal.unchanged_observations` |
| `CHANGE_BETWEEN` | AsOf\|Window + Exists + Transition | `temporal.direct_transition` |
| `CHANGE_COUNT` | Window + Exists + ChangeCount | `temporal.change_count` |
| `CONSECUTIVE_SUBINTERVALS` | Window(bucket) + ConsecutiveBuckets + State | `temporal.consecutive_buckets` |

기간이 붙은 전이는 selector 가 window 로 **승격**될 뿐 술어는 그대로다 — 기간 전이를 위한
별도 노드나 별도 연산자를 만들지 않는다.

배선의 네 게이트(합성 라우터 · 청구 커버리지 · 의무 영수증 · 의미 불변식)는
`tests/test_temporal_claims_wiring.py` 가 고정한다. 의무 방면의 조인 키는 근거 구간이 아니라
**낮춘 원자의 일치**다 — 구간만 보면 다른 컴파일러의 조건에 그 구간을 붙여 방면을 위조할 수
있고(실측), 그때 이력 조건이 현재값 조건으로 조용히 바뀐다.

## 아직 하지 않은 것

- 주체별 정렬·행 선택 primitive(PartitionBy/OrderBy/LimitPerEntity/Lag). 이것이 들어오면
  위 표의 `X` 여섯 개가 lowerer 만 채워 열린다.
- 이벤트 로그의 칸 전칭('최근 3개월 매월 구매'). `temporal.every_bucket` 은 현재
  periodic_snapshot 계열만 받는다 — 이벤트 로그에서 빈 칸이 '무발생'을 뜻하는지는 capability
  가 아니라 **적재 선언(coverage)** 의 책임이고, 그 선언 없이 열면 적재 공백이 '그 달 구매
  없음'으로 조용히 바뀐다.
- 상태(정상/휴면) 축. 전이 지표도 이력 소스도 선언이 없어 `temporal_metric_not_declared` 로
  닫힌다. 어휘 문제가 아니라 선언 문제이므로 카탈로그가 먼저 서야 한다.
