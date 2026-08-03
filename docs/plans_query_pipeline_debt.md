# query_pipeline 기술 부채 상환 플랜

> 생성 2026-08-03. 근거: `query_pipeline/` 4계층 도입(같은 날) 직후 남긴 부채 5종을 코드로 실측하고,
> 실측 중 발견한 정합성 결함 1종을 추가해 6종으로 재정리한 것. 계층 구조 자체의 설명은
> `docs/overview/query_pipeline.md` 가 권위이고, 이 문서는 **그 위에 남은 일**만 다룬다.

## 진행 상태

| Phase | 대상 부채 | 상태 | 위험 | 규모 |
| --- | --- | --- | --- | --- |
| 0 안전망 | (선행) | **완료 2026-08-03** | 낮음 | 반나절 |
| 1 값싼 것 | ⑥ 롤링 창 · ⑤ 달력 이중소유 · ③ 바인딩 구멍 | **완료 2026-08-03** | 낮음 | 1일 |
| 3-A mypy 인접 | ④ 부분 | **완료 2026-08-03** | 낮음 | 반나절 |
| 2 Resolver 이관 | ① `_derive_audience_execution` | **완료 2026-08-03** | **높음** | 2~3일 |
| 3-B/C mypy 확장 | ④ 나머지 | 미착수 | 중간 | 미정 |
| 4 generic compiler | ② | **보류(의도적)** | — | — |

권장 순서는 `0 → 1 → 3-A → 2` 였고 그대로 진행했다. 3-A 를 2 앞에 둔 이유는 아래 Phase 3 에
있고, **실제로 값을 했다** — 경계를 열자마자 `compiler/bindings.py` 의 루프 변수 가림이
드러났다(`spec` 이 `FieldSpec` 과 `EventSpec` 두 타입을 오갔다).

### 이관 결과 요약 (재측정 전까지 이 값을 기준으로 한다)

| 항목 | 이관 전 | 이관 후 |
| --- | --- | --- |
| `_derive_audience_execution` | 217줄 | **13줄**(두 호출 + 예외 어휘 변환) |
| 그 함수의 도메인 의존 | 7종 인라인 | 0(모듈 두 개로 분리) |
| `pytest` | 1648 passed / 24 skipped | **1701 passed / 24 skipped** |
| mypy | 0(27 files, 경계 3종 `follow_imports=skip`) | **0(28 files, 경계 없음)** |
| ruff | query_pipeline + 그 테스트 | 좌동 + 신규 도메인 모듈 2개·테스트 2개 |

신규 파일: `query_pipeline/requirement/validation.py` · `audience_validators.py` ·
`query_structurer/audience_execution.py` · `tests/test_audience_execution_projection.py` ·
`tests/test_audience_validators.py`.

---

## 실측값 (2026-08-03, 재측정 전까지 이 값을 기준으로 판단한다)

| 항목 | 값 | 재측정 명령 |
| --- | --- | --- |
| `_derive_audience_execution` | 217줄. plan 키 6개 기록: `event_expression`(pop 포함) · `semantic_ir` · `audience_unsupported_hypotheses` · `audience_execution_assets` · `requirement.expression` · `requirement.issues` + `plan_decisions.record` | `query_structurer/campaign_plan_v4.py:1153-1370` |
| 그 함수의 도메인 의존 | `audience_runtime` · `canonical_audience_claims` · `event_compiler` · `event_ir` · `execution_assets` · `plan_decisions` · `event_parser`(지연 import) | 같은 블록 |
| mypy 저장소 전체 | **566 errors / 75 files** (non-strict). graph_rag 125 · conceptual_targeting 43 · api 34 · metric_registry 25 | `python -m mypy --ignore-missing-imports $(ls *.py)` |
| query_pipeline 이 의존하는 3종 | `sql_dialect` **0** · `event_ir` **2** · `event_compiler` **8** (strict 에서도 sql_dialect 0, event_ir 2) | `python -m mypy --ignore-missing-imports --follow-imports=silent event_ir.py` |
| ↑ **이 수치는 과소평가였다**(3-A 실측) | `--follow-imports=silent` 가 각 모듈의 **의존 폐포**를 가린다. 경계를 실제로 열면 `event_ir → lexicon_patterns`(9)가 함께 들어와 합계 **21개**였다(수정 완료) | `python -m mypy` |
| 달력 표면어 단일 소유자 | `calendar_window._scan_relative_calendar_months` + `parser_lexicon.json`(`calendar_current_month`/`calendar_previous_month`). 공개 진입점 `parse_calendar_window(text, *, today)` → `{from,to,label}` | `calendar_window.py:449`, `:639` |
| semi/anti 조인 구현 규모 | `compile_relation`(~130줄) + `_compile_membership_join`(~40) + `_subquery` + `_derived_field_bindings` + `RelationPlan` 스코프 전개 | `event_compiler.py:552`, `:707` |
| 회귀 게이트 | 라이브 코퍼스 **77 프롬프트** + 순수 분류 러너 | `docs/data/test_baselines/live_prompts.json`, `tools/live_prompt_baseline.py` |
| 현재 검사 상태 | `pytest` 1648 passed / 24 skipped · `mypy` 0(27 files) · `ruff` clean | `pytest && mypy && ruff check query_pipeline` |

---

## 부채 목록 (번호는 아래 Phase 에서 참조한다)

| # | 부채 | 성격 |
| --- | --- | --- |
| ① | `_derive_audience_execution` 이 요구 검증과 실행 표현 파생을 함께 한다 | 구조 |
| ② | `LogicalPlanSqlCompiler` 가 semi/anti 조인·char8 경계를 표현하지 못한다 | 기능(보류) |
| ③ | 오디언스 경로의 `SqlCompilationContext.schema_bindings` 가 비어 있다 | 구조 |
| ④ | mypy strict 범위가 `query_pipeline/` 뿐이다 | 안전망 |
| ⑤ | `resolve_period_phrase` 의 표면어 표가 `calendar_window` 와 이중 소유다 | 구조 |
| ⑥ | **`resolve_period_phrase` 가 "최근 N일"을 계획 시점 절대 구간으로 접는다** | **결함** |

### ⑥ 이 결함인 이유 (재논의 금지 — 근거가 소스에 있다)

`event_compiler.py:458` 이 명문화한다: *"롤링 경계는 실행 시점 함수로 렌더한다 — 계획 시점 날짜로
굳히면 '최근 30일'이 고정된다."* 메모리의 `duration temporal kind` 결정(rolling_duration →
`window.type="rolling"`, past_point → `"relative"`)도 같은 방향이다. 그런데 도입 시점의
`resolve_period_phrase._ROLLING_DAYS_RE` 는 `최근 N일` 을 `AbsoluteWindow` 로 접는다. 운영 경로가
아직 이 코드를 타지 않아 사고는 없었지만, 부채가 아니라 **규칙 위반**이므로 Phase 1 최우선이다.

---

## Phase 0 — 안전망 (선행 필수)

이관의 성공 조건은 "동치"인데 지금은 동치를 **증명할 수단이 없다**. 이 저장소의 자체 원칙
(`docs/plans_ir_decoupling.md`: 안전망 먼저, 구조 이동은 계측 뒤)을 그대로 따른다.

- **0-1 행동 스냅샷** — 아직 **없다**(이 Phase 에서 새로 만든다). 파일명 `tests/test_audience_execution_projection.py`.
  입력 payload 대표 6종에 대해 `attach_campaign_query_plan_v4_identity` 결과의 plan 키 6개 값을 전량 고정한다.
  - 성공(표현 확정) / `missing_argument` / `ambiguous_requirement`
  - `unsupported_semantics` + 실행자산 반박 있음(→ `audience_unsupported_hypotheses`)
  - `unsupported_semantics` + 자산은 있으나 생산자 없음(→ `audience_execution_assets`)
  - 모델 누락(`missing_field_causes.cause == model_omission` → `failure_kind="structurer_failure"`)
- **0-2 라이브 기준선 기록.** `python tools/live_prompt_baseline.py --base-url http://localhost:8000`
  결과를 `artifacts/` 에 저장. 이후 모든 Phase 의 회귀 판정 기준.

> **게이트**: 스냅샷 6종 통과 + 기준선 파일 존재. 없으면 Phase 2 를 시작하지 않는다.

### 0-2 는 회귀 판정 기준이 되지 못한다 (2026-08-03 실측 — 이 절이 권위다)

기준선을 실제로 두 번 뜨고 나서 드러난 사실: **코드가 같은데도 귀결이 흔들린다.**

| 실행 | 코드 | sql / unsupported / clarification / failure / error |
| --- | --- | --- |
| `live_prompt_run_20260803_after_rag_reload.json` (21:12) | 이관 전 | 9 / 21 / 34 / 11 / 2 |
| `live_prompt_baseline_20260803_phase0.json` (23:14) | **같은** 이관 전 | 10 / 29 / 33 / 5 / 0 |

두 실행 사이에 **77종 중 43종(56%)의 귀결이 달라졌다**. 둘 다 이관 전 코드다(API 컨테이너는
`--reload` 없이 21시간 떠 있었고 bind mount 만 있다 — 편집이 반영되지 않는다). 즉 이 차이는
전부 LLM 비결정성이고, 그 폭이 이관이 낼 수 있는 어떤 차이보다 크다.

**그래서 동치는 결정론 경로에서 증명했다.** `git stash` 로 이관 전 코드를 세우고, 넓힌 19종
payload(4개 issue 코드 · 미등록 심볼 · 집계/부정/OR · 창 종류 보정 · 미지원 2갈래 · 결핍 원인
2축 · 계약 부재)에 대해 투영 6키를 대조해 **19/19 완전 동일**을 확인했다. 라이브 러너는
"호출이 깨지지 않는다"는 smoke 이상으로 읽지 말 것 — 그 위에서 회귀를 판정하면 잡음을
회귀로 읽거나(오탐), 진짜 회귀를 잡음으로 덮는다(미탐).

> 라이브 코퍼스를 진짜 안전망으로 쓰려면 `--repeat` 로 프롬프트별 분포를 재고 **불안정 항목을
> 먼저 격리**해야 한다. 그 전까지 회귀 게이트는 `pytest`(특히 투영 스냅샷)와 위 차등 대조다.

---

## Phase 1 — 값싸고 독립적인 것 (부채 ⑥·⑤·③)

셋 다 `query_pipeline/` 안에서 끝나고 운영 SQL 을 건드리지 않는다.

### 1-A. 롤링 창 정합성 (부채 ⑥, 최우선)

- `requirement/resolver.py` 의 `_ROLLING_DAYS_RE` 제거 → "최근 N일" 은 `RollingWindow` 를 돌려준다.
- `PeriodResolution.window` 타입을 `AbsoluteWindow` → `TimeWindow` 로 넓힌다. `_normalize` 와
  `_row_condition` 은 이미 `TimeWindowExpression` 에 그대로 싣는다.
- `compiler/sql_compiler.py::_render_time_window` 가 롤링을 지원한다. **새 문법을 쓰지 않고**
  `sql_dialect` 의 기존 단일 소유자를 호출한다: `char8_cutoff(days)` / `datetime_cutoff(days)`
  (`sql_dialect.py:68`, `:85`). 컬럼 grain 판정은 `SchemaBinding` 에 `data_type` 을 추가해야 하므로
  1-C 와 함께 한다.
- 회귀 테스트: 같은 사양을 **서로 다른 기준일**로 컴파일해도 SQL 이 동일해야 한다(계획 시점 고정 금지).

### 1-B. 달력 표면어 이중 소유 제거 (부채 ⑤)

- `_MONTH_OFFSETS` / `_YEAR_OFFSETS` 표를 **삭제**한다.
- `calendar_window.parse_calendar_window(phrase, today=<타임존 적용 date>)` 위임 →
  `event_ir.AbsoluteInterval.from_calendar_window({from,to})` 로 반개구간 변환 →
  `expressions.AbsoluteWindow` 로 옮긴다.
- 타임존 의미는 유지된다: 지금도 `now.astimezone(zone).date()` 를 계산하므로 그것을 `today` 로 넘긴다.
- 부수 이득: '올해/작년/상반기/N분기/YYYY년 M월/YYYY-MM-DD' 가 공짜로 열린다(현재 표는 5개 표현뿐).

### 1-C. schema_bindings 구멍 메우기 (부채 ③)

- `query_pipeline/__init__.py::compile_audience_predicate` 가 **같은** `compile_context_factory()`
  결과에서 바인딩을 파생한다:
  `schema_bindings_from_compiler(events=ctx.registry, fields=ctx.fields, subject=ctx.subject)`.
- 두 번째 소유자가 생기지 않는 근거: 카탈로그가 만든 그 컨텍스트에서 **파생**하는 것이지 따로 적는 것이
  아니다. 손으로 적는 순간 이 항목은 부채 상환이 아니라 부채 신설이 된다.
- 그러면 `AudiencePredicateCompiler._verify_bindings` 가 실제로 동작해 미선언 entity 가
  단계명을 단 `MissingBindingError` 로 나온다.
- `SchemaBinding` 에 `data_type: Mapping[str, str]`(속성 → number/string/date/date_char8) 추가 —
  1-A 의 grain 판정 입력이다. `event_compiler.FieldSpec.data_type` 에서 파생한다.

> **게이트**: `pytest` 전량 + `mypy` + `ruff` + **바이트 동일성 테스트 유지**
> (`test_audience_predicate_matches_legacy_engine_byte_for_byte`).
> 규모: 합계 ~250줄 변경, 신규 테스트 ~8개.

---

## Phase 3-A — mypy 인접 확장 (부채 ④ 일부, Phase 2 **앞**에 둔다)

현재 `pyproject.toml` 은 `event_ir` / `event_compiler` / `sql_dialect` 에 대해
`follow_imports = "skip"` 로 경계를 끊어 두었다. 그 설정이 새 계층과 legacy IR 사이의 **실제 타입
불일치를 가리고 있다**. 실측상 세 모듈의 부채는 합쳐서 **10개**뿐이다.

1. `sql_dialect`(0) → override 에서 제거.
2. `event_ir`(2) → 수정 후 제거.
3. `event_compiler`(8) → 수정 후 제거.
4. `follow_imports = "skip"` 삭제. `ignore_missing_imports` 만 남긴다.

Phase 2 앞에 두는 이유: 이관 중 발생하는 경계 타입 불일치를 mypy 가 잡아 준다. 안전망이 하나 더
생긴 상태에서 큰 이동을 한다.

> **하지 않는 것**: `graph_rag`(125) · `api`(34) · `conceptual_targeting`(43). 통과 못 하는 검사는
> 곧 무시되는 검사가 된다.

---

## Phase 2 — `_derive_audience_execution` 이관 (부채 ①, 핵심)

217줄이 **두 가지 일**을 섞고 있다: (a) 요구 검증 → issue 판정, (b) legacy plan 키 6개로 투영.
**(a)만 옮기고 (b)는 얇은 투영 함수로 남긴다.**

### 2-A. 검증기를 주입 가능한 계약으로 추출

지금 그대로 옮기면 `requirement` 계층이 `event_parser` · `canonical_audience_claims` ·
`execution_assets` 에 직접 결합해 **범용 계층이 도메인 계층이 된다**(계층 가드
`tests/test_query_pipeline_layering.py` 는 패키지 내부 방향만 보므로 이것을 막지 못한다).

`query_pipeline/requirement/validation.py` 신설:

```python
class ExpressionValidator(Protocol):
    def validate(
        self, expression: EventExpression, *, query: str, literals: Sequence[JsonValue]
    ) -> tuple[RequirementIssue, ...]: ...
```

구현 4종은 **도메인 쪽**(신규 `audience_validators.py` 또는 `query_structurer/`)에 둔다:

| 구현 | 현재 위치 | 옮기는 것 |
| --- | --- | --- |
| `CatalogSymbolValidator` | `campaign_plan_v4.py:1210-1224` | `unsupported_events` / `unsupported_fields` |
| `CompilerCapabilityValidator` | `:1227-1237` | `validate_compiler_capability` |
| `TemporalSpanValidator` | `:1238-1240` + `_temporal_requirement_issues` | `event_parser.source_time_span_count` + 맨 '최근' 검출 |
| `CanonicalClaimValidator` | `:1246-1253` | `canonical_audience_claims.canonical_claim_issues` |

`apply_window_kinds`(`:1179-1193`)는 **검증이 아니라 정규화**다. `ReceiptAction.NORMALIZED_VALUE`
영수증을 남기는 resolver 단계로 옮기고, `plan_decisions.record` 는 투영 단계에서 영수증을 읽어 쓴다.

### 2-B. Resolver 가 validator 를 실행

`DefaultRequirementResolver.__init__(validators: Sequence[ExpressionValidator] = ())` 추가 →
`_resolve_proposed` 에서 표현 변환 직후 실행 → 결과 issue 를 기존 `log.issue` 통로로 합류.
코드 → kind 매핑은 이미 `requirement/parser.py::_DRAFT_ISSUE_KINDS` 에 있다.

### 2-C. `_derive_audience_execution` 을 투영으로 축소

```python
def _derive_audience_execution(payload, query, *, current_date) -> bool:
    result = run_audience_resolver(payload, query, current_date=current_date)
    return project_resolution_to_plan(payload, result)
```

`project_resolution_to_plan` 이 기존 분기를 **그대로** 재현한다:

- 성공 → `event_expression` + receipts + `semantic_ir(status="resolved")`
- 미해결 → `event_expression` pop + `missing`/`unsupported` 분류 →
  실행자산 반박 있음 → `audience_unsupported_hypotheses` 후 `False` 반환(SemanticPlan 경로로 넘김)
  · 자산만 있고 생산자 없음 → `audience_execution_assets` + `failure_kind="system_failure"`
  · 그 외 → `semantic_ir(status="unsupported" | "needs_clarification")` + `missing_field_causes`

> **여기서 동작을 개선하지 않는다.** 이관과 개선을 섞으면 회귀 원인을 가릴 수 없다.
> 개선 아이디어는 별도 항목으로 이 문서 맨 아래 '이관 후 후보'에 적는다.

### 2-D. 동치 확인

Phase 0 스냅샷 6종 + 라이브 77종 재실행 → **분류 분포가 동일**해야 통과.

> **위험(높음)**: `semantic_ir.failure_kind` 3분기(structurer_failure / user_clarification /
> system_failure)가 사용자 응답 문구를 결정한다. 한 칸만 어긋나도 사용자가 보는 결말이 바뀐다.
> **롤백**: 2-C 는 한 함수 교체라 커밋 하나를 되돌리면 끝난다.
> **규모**: ~400줄 이동/재배치, 신규 파일 2개.

---

## Phase 4 — generic compiler 완성 (부채 ②): **보류. 재논의 전 이 절을 읽을 것**

`LogicalPlanSqlCompiler` 에 semi/anti 조인·char8 경계를 넣으려면 `compile_relation`(~130줄) +
`_compile_membership_join` + `_subquery` + `_derived_field_bindings` + `RelationPlan` 스코프/바인딩
전개를 재구현해야 하고, 그 결과물의 유일한 검증 수단은 **기존 엔진과의 출력 비교**다.

지금 갚지 않는 근거 둘:

1. **소비자가 없다.** 실행 DB 는 MSSQL 하나다. 메모리의 이식성 목표(`DB portability constraint`)는
   *"스키마 지식을 소스 밖(설정·카탈로그)으로"* 이지 *"SQL 렌더러를 하나 더"* 가 아니다.
   Phase 1-C 가 그 목표에 직접 기여한다.
2. **위험 대비 이득이 역전돼 있다.** 두 렌더러가 공존하면 "어느 쪽이 정본인가"가 새 부채가 된다 —
   이 저장소가 canonical/legacy 이중 표현으로 이미 지불한 비용이다.

**여는 조건**(둘 중 하나가 성립할 때만 착수):

- 두 번째 실행 DB 가 실제로 도입된다.
- `event_compiler` 위임이 계층 경계를 실제로 막는다(예: LogicalPlan 이 표현하는데 위임으로는
  못 내는 계획이 생긴다).

---

## 구현 기록 — 계획과 **다르게** 한 것 (2026-08-03)

계획대로 된 것은 적지 않는다. 아래는 실제로 코드를 만지면서 계획을 바꾼 지점과 그 근거다.

**1-B. `calendar_window` 위임은 그대로 되지 않았다.** `parse_calendar_window` 는 '올해/작년'을
창으로 만들지 않는다 — 그 어휘는 한정자의 연도를 정하는 **앵커**로만 쓰인다('작년 상반기').
계획이 약속한 "'올해/작년'이 공짜로 열린다"를 지키려면 소유자 쪽에 앵커→창 변환이 필요해
`calendar_window.parse_relative_year_window` 를 신설했다(앵커 **전체**가 표현일 때만 창이다).
호출자가 각자 그 변환을 적으면 상대 연도 어휘의 두 번째 소유자가 생기므로 소유자에 둔다.

**1-B. 영어 표면어는 사라졌다.** `last month`/`this year` 등은 저장소 어휘(`parser_lexicon.json`)에
없다. 그 다섯을 살리려면 이 계층이 두 번째 표를 갖게 되므로 살리지 않았다 — 필요해지면 답은
어휘를 늘리는 것이다. 대신 소유자가 아는 표현이 전부 열렸다(단어형 '일주일/반년', '금월/당월/
전월', 'YYYY-MM-DD', 상·하반기, N분기).

**1-B. 기간 해석에 연산자 게이트를 넣었다.** 위임한 소유자는 문장을 **훑는 스캐너**다. 종전처럼
모든 문자열 값에 걸면 `'2025년 신년 프로모션'` 이 조용히 시간 창이 된다. 그래서 `_normalize` 는
`RequirementOperator.BETWEEN` 자리에서만 기간을 해석한다 — 범위가 아닌 연산자에는 창을 실을
자리가 없다(어느 경계를 쓸지 추측해야 한다).

**1-A. '3개월 전'도 함께 처리된다.** 위임한 소유자가 rolling/past_point 를 구분해 주므로 그 구분을
버릴 수 없었다. `event_ir.CALENDAR_KIND_WINDOW_TYPES` 를 따라 rolling 은 접지 않고, past_point 는
`event_ir.resolve_relative_window` 로 그 달력 칸을 계획 시점에 확정한다.

**1-A. capability 선언에 숨은 결함이 있었다.** `GENERIC_SQL_CAPABILITIES` 는 `A | B - C` 로 적혀
있어 `-` 가 먼저 묶였고, 그 결과 **아무것도 빼지 않았다**(`node.temporal_relation` 이 계속 지원으로
선언됨). 롤링을 지원 목록에 넣으려다 발견해 괄호로 고쳤고, 창 목록은 렌더 표
(`sql_compiler.SUPPORTED_WINDOW_KINDS`)에서 파생하게 했다. 드리프트 가드는
`test_capability_declaration_matches_what_the_renderer_actually_does`.

**3-A. 실측 10 은 과소평가였다.** `--follow-imports=silent` 는 각 모듈의 **의존 폐포**를 가린다.
경계를 실제로 열자 `event_ir → lexicon_patterns`(9)가 함께 들어와 21개였고 전부 고쳤다.
`calendar_window`(자체 2개)는 폐포가 `targeting_ir`·`condition_normalizers` 로 저장소 본체까지
열려 `follow_imports = "skip"` 을 유지한다 — 그 범위는 Phase 3-B/C 다. 통과하지 못하는 검사는
곧 무시되는 검사가 된다는 원칙을 여기서도 그대로 적용했다.

**2-A. `apply_window_kinds` 는 resolver 가 아니라 도메인 어댑터에 남겼다.** 계획은 이것을 resolver
단계로 옮기라고 했지만, 그러려면 `ExpressionValidator` 와 짝이 되는 정규화 프로토콜을 하나 더
만들고 **요구 계층이 wire dict 를 만지게** 해야 한다. 그것은 계획 자신이 2-A 에서 경계한 "범용
계층이 도메인 계층이 되는" 방향이다. 대신 정규화 기록을 `AudienceResolution.normalizations` 로
투영까지 들고 가서, `plan_decisions.record` 는 계획대로 **투영 단계**에서만 쓴다.

**2-A. 검증기 계약에 근거 구간이 필요했다.** `RequirementIssue` 에는 evidence 가 없어 legacy issue
dict(`{code, argument, message, evidence}`)를 복원할 수 없었다. `evidence: SourceEvidence | None`
을 추가하고, `code ↔ kind` 표와 그 역함수를 `requirement/validation.py` 한 곳에 두어
`issue_from_report`/`report_from_issue` 왕복이 무손실임을 테스트로 고정했다
(`test_issue_report_roundtrip_is_lossless`).

**2-A. 계층 가드를 바깥 방향으로도 세웠다.** 계획이 지적한 대로 기존 가드는 패키지 내부 방향만
본다. `test_package_never_imports_the_audience_domain` 이 도메인 모듈 9종의 import 를 금지한다 —
이 가드가 없으면 다음 사람이 검증기를 요구 계층에 "잠깐" 옮겨 넣는 것을 아무도 못 막는다.

**2-B. 파싱은 도메인에 남았다.** `DefaultRequirementResolver` 가 wire dict 파싱 실패를 issue 로
바꾸는 것과 달리, 기존 경로는 그것을 `CampaignQueryPlanValidationError` 로 **올린다**(구조화기
재시도의 신호다). 이관과 개선을 섞지 않기 위해 파싱·근거 대조는 어댑터에 남기고, resolver 에는
이미 파싱된 표현만 넘긴다. 그래서 resolver 의 파싱 실패 갈래는 이 경로에서 도달 불가다.

## 이관 후 후보 (Phase 2 완료 전에는 손대지 않는다)

- `_temporal_requirement_issues` 의 맨 '최근' 검출은 `_INCOMPLETE_RECENCY_RE` 정규식 손배선이다.
  1-B 로 `calendar_window` 위임이 서면 그쪽 어휘에서 파생할 수 있는지 재검토한다.
- `AudienceRequirement.constraints` 경로는 현재 테스트 코퍼스에만 쓰인다. 운영 LLM 계약이
  `expression` 갈래만 쓰므로, 이관 후에도 소비자가 없으면 **제거 후보**로 올린다
  (죽은 갈래를 남겨 두면 "지원한다고 말했는데 아무도 안 쓴다"가 된다).
- `QueryOutput` 은 요구의 `output` 에서만 채워진다. 오디언스 경로는 항상 비어 있으므로,
  이관 후 실제 사용 패턴을 보고 필수/선택을 다시 정한다.

## 검사 명령

```bash
pytest                                    # 저장소 전체(현재 1701 passed / 24 skipped)
mypy                                      # query_pipeline/ strict + 따라 들어가는 모듈
ruff check .                              # 범위는 pyproject.toml [tool.ruff] include 가 선언한다
python tools/live_prompt_baseline.py --base-url http://localhost:8000   # 라이브 77종
```

> 라이브 러너는 **실행 중인 API 컨테이너**를 때린다. 그 컨테이너는 `--reload` 없이 뜨므로
> 편집이 반영되지 않는다 — 코드를 고친 뒤 재실행하려면 먼저 재시작해야 한다(bind mount 라
> 재빌드는 불필요). 재시작 없이 나온 수치는 고치기 **전** 코드의 수치다.
>
> 그리고 그 수치는 회귀 판정에 쓸 수 없다(위 'Phase 0 — 0-2 는 회귀 판정 기준이 되지
> 못한다' 참조: 같은 코드로 두 번 돌려 56% 가 달라졌다).
