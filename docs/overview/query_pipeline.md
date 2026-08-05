# 계층 파이프라인 (`query_pipeline/`)

자연어 → SQL 경로를 **네 개의 서로 다른 타입**으로 나눈 계층 구조. 2026-08-03 도입.

## 1. 왜 나눴는가

이전 구조에서는 `audience_requirement.expression` 과 `event_expression.expression` 이
사실상 같은 Event IR 이었다. 두 이름이 같은 타입을 가리키면 다음이 구조적으로 가능하다.

- 검증되지 않은 Event IR 이 SQL 컴파일러에 도달한다.
- 사용자 요구의 모호성(`missing`/`ambiguous`/`unsupported`)이 실행 IR 까지 흘러든다.
- SQL 컴파일러가 검증과 기본값 처리까지 떠맡는다.
- SQL 이 틀렸을 때 **어느 단계**가 틀렸는지 되짚을 좌표가 없다.

`query_plan` dict 하나를 여러 단계가 in-place 로 변형하던 구조(감사 결론: IR 결합의 진앙)와
같은 문제다. 그래서 단계마다 다른 Python 클래스를 두고, **호출 시그니처로** 역방향을 막았다.

## 2. 계층과 타입

```text
사용자 문장
  ↓ RequirementParser        query_pipeline/requirement/
AudienceRequirement          결핍·모호·추론을 허용한다. 실행 가능성은 주장하지 않는다.
  ↓ RequirementResolver
EventQuerySpec               query_pipeline/event_query/
                             미해결 상태가 없다(타입과 검증기가 함께 막는다).
  ↓ LogicalPlanner
LogicalPlan                  query_pipeline/planning/
                             조회·필터·집계·정렬 순서. 테이블/컬럼/방언을 모른다.
  ↓ SqlCompiler
CompiledSql                  query_pipeline/compiler/
                             문장 + 파라미터 + 방언.
```

| 계층 | 모듈 | 핵심 타입 |
| --- | --- | --- |
| requirement | `requirement/models.py`, `issues.py`, `parser.py`, `resolver.py`, `validation.py` | `AudienceRequirement`, `RequirementConstraint`, `RequirementValue`(resolved/inferred/ambiguous/missing), `RequirementIssue`, `ProposedExpression`, `ExpressionValidator` |
| event_query | `event_query/expressions.py`, `models.py`, `receipts.py`, `event_ir_bridge.py` | `EventExpression`(discriminated union), `EventQuerySpec`, `QueryOutput`, `ResolutionReceipt`, `Assumption` |
| planning | `planning/models.py`, `logical_planner.py` | `LogicalPlan`(scan/filter/aggregate/sort/limit/project) |
| compiler | `compiler/models.py`, `base.py`, `sql_compiler.py`, `postgresql.py`, `audience.py`, `bindings.py`, `capability.py` | `CompiledSql`, `SqlParameter`, `SchemaBindings`, `SqlCompilationContext` |
| pipeline | `pipeline/query_pipeline.py` | `QueryPipeline`, `QueryPipelineReady`, `QueryPipelineNeedsResolution` |
| compatibility | `compatibility/legacy_event_expression.py` | `LegacyEventExpressionAdapter`(deprecated) |

import 방향은 `requirement → event_query → planning → compiler` 로 고정되고
`tests/test_query_pipeline_layering.py` 가 모듈 전수로 강제한다. `requirement` 는
`compiler` 를 import 하지 않는다 — capability 판정은 프로토콜로 주입된다.

같은 파일이 **바깥 방향**도 막는다: 패키지 어느 모듈도 이 배포의 오디언스 도메인
(`audience_runtime`·`audience_validators`·`canonical_audience_claims`·`execution_assets`·
`event_parser`·`plan_decisions`·`graph_rag`·`query_structurer`)을 import 하지 않는다.
(`semantic_plan` 은 2026-08-05 이 목록에서 빠졌다 — SemanticPlanV2 중간표현이 폐기되어
import 할 모듈 자체가 없다.)
패키지 내부 방향만 보는 가드로는 "범용 계층이 도메인 계층이 되는" 경로를 막지 못한다 —
검증기를 요구 계층에 그대로 옮겼다면 정확히 그 경로로 샜을 것이다.

`event_ir`·`event_compiler`·`sql_dialect`·`calendar_window` 는 그 목록에 없다. 각각 사건
대수·물리 렌더 엔진·방언 문법·달력 문법의 **단일 소유자**이고, 특정 오디언스 스키마를
모른다. 같은 지식을 이 패키지가 다시 적는 것이 훨씬 큰 부채다.

## 3. 두 모델을 나눈 지점

`AudienceRequirement.expression` 의 타입은 `ProposedExpression` 이고, 그 안의 `payload` 는
pydantic `JsonValue` 다(`Any` 가 아니다). **실행 타입으로 선언하지 않은 것이 핵심이다**:
이 시점의 트리는 심볼이 카탈로그에 있는지, 근거 구간이 원문과 맞는지, 컴파일러가 표현할 수
있는지 아무것도 확인되지 않았다. 실행 타입으로 선언하면 세 가지가 확인된 것처럼 보인다.

그 결과 다음 호출은 **정적 타입 검사에서 실패한다**(`tests/type_contracts/forbidden_calls.py`
+ `tests/test_query_pipeline_type_contracts.py` 가 mypy 로 확인한다).

```python
sql_compiler.compile(requirement, sql_context)
sql_compiler.compile(requirement.expression, sql_context)
logical_planner.create_plan(requirement)
```

## 4. Resolver 의 책임 경계

한다: 자연어 기간 정규화(타임존 달력) · 논리 참조 해결 · 기본값 적용 · 후보 선택 ·
capability 검증 · policy 검증 · **주입된 표현 검증기 실행** · assumption/receipt 생성 ·
사양 생성.
하지 않는다: SQL 생성, 물리 테이블/컬럼 결정, 표현의 축소, **무엇이 유효한지 스스로 판단**.

미해결이 하나라도 남으면 `UnresolvedResolution` 을 돌려주고 **사양을 만들지 않는다**.
`EventQuerySpec` 은 생성 시점에 재귀 검증한다(축약 표기 잔재, 바인딩 없는 속성 참조).

### 4-1. 기간 표현: 표면 문법을 소유하지 않는다

`resolve_period_phrase` 는 표면어 표를 갖지 않고 `calendar_window`(달력 표현·기간 길이·
시점 판정)와 `event_ir.CALENDAR_KIND_WINDOW_TYPES`(그 종류가 어느 창인가)에 위임한다.
그래서 '지난달'이 두 뜻을 가질 자리가 없다.

세 종류가 서로 다른 결말을 갖는다.

| 표현 | 창 | 확정 시점 |
| --- | --- | --- |
| '지난달' · '2025년 3월' · '작년' | `AbsoluteWindow` | 계획 시점(타임존 달력) |
| '최근 30일' · '일주일' | `RollingWindow` | **실행 시점**(SQL 함수로 렌더) |
| '3개월 전' | `AbsoluteWindow`(그 달력 칸) | 계획 시점 |

롤링을 계획 시점 날짜로 접지 않는 것이 규칙이다 — 접으면 '최근 30일'이 그 날의 30일로
고정돼 내일 실행해도 어제의 창을 본다(`event_compiler` 가 같은 규칙을 명문화한다).

기간 해석은 **범위 연산자(BETWEEN)** 자리에서만 한다. 위임한 소유자는 문장을 훑는
스캐너라 아무 문자열에나 걸면 '2025년 신년 프로모션' 같은 값이 조용히 시간 창이 된다.

### 4-2. 표현 검증기는 주입된다

"이 표현이 유효한가"는 요구 계층이 답할 수 없다 — 심볼 카탈로그도, 컴파일러 능력도,
원문 리터럴 색인도 이 계층 밖에 있다. 그래서 `requirement/validation.py` 는 모양만
선언하고(`ExpressionValidator`), 구현은 도메인이 준다(`audience_validators.py`).

오디언스 경로가 주입하는 넷은 **순서가 계약**이다(`semantic_ir.message` 가 첫 issue 의
문장이다): 카탈로그 심볼 → 컴파일러 capability → 시간 한정 소실 → canonical 주장 대조.

## 5. 기존 Event IR 과의 관계

`event_ir.py` 의 대수(비교·존재·시간창·시간관계·부정·논리결합 + 관계 대수 + 스칼라)는 그대로
남고, `event_query/expressions.py` 가 그것을 **타입 있는 pydantic 모델로 미러링**한다.
왕복은 `event_ir_bridge` 가 담당하며 `tests/test_query_pipeline_bridge.py` 가 노드 타입
전수로 무손실을 강제한다. 표현하지 못하는 노드는 무시하지 않고 `ExpressionBridgeError` 다.

SQL **렌더링**은 여전히 `event_compiler` 가 소유한다. 실CRM 물리 사실(반개구간 경계·char8
날짜·subject_column 바인딩·semi/anti 조인 전개·롤링 창의 실행시점 렌더)이 그 모듈에
축적돼 있고, 그것을 새로 쓰는 것은 리팩터링이 아니라 재작성이다.
`compiler/audience.py` 가 `LogicalPlan` 을 받아 그 엔진에 위임하므로 **생성되는 SQL 은
바이트 동일**하다(`tests/test_query_pipeline_compiler.py::test_audience_predicate_matches_legacy_engine_byte_for_byte`).

## 6. 운영 경로 연결

`graph_rag.build_event_expression_sql_candidate` 는 저장된 `plan["event_expression"]` 을
그 자리에서 파싱해 컴파일러에 넘기던 코드를 다음으로 바꿨다.

```python
condition_sql = query_pipeline.compile_audience_predicate(
    payload, compile_context_factory=_event_compile_context
).sql
```

즉 운영 SQL 도 **어댑터 → 사양 → 계획 → 컴파일러**를 통과한다. 실패는 단계 이름을 들고
나오며(`QueryPipelineError.stage`), `unresolved_source_conditions[].stage` 에 남는다.

`SqlCompilationContext.schema_bindings` 는 그 경로에서 **같은 컴파일 컨텍스트로부터
파생**된다(`schema_bindings_from_compiler`). 손으로 적으면 물리 사실의 두 번째 소유자가
생기므로 파생만 한다. 그 결과 `AudiencePredicateCompiler._verify_bindings` 가 실제로
동작해, 선언되지 않은 논리 entity 는 단계 이름을 단 `MissingBindingError` 로 멈춘다.

### 6-1. 구조화기 경로

`query_structurer/campaign_plan_v4._derive_audience_execution` 은 두 호출로만 남았다.

```python
resolution = audience_execution.run_audience_resolver(payload, query, current_date=...)
return audience_execution.project_resolution_to_plan(payload, resolution)
```

- `run_audience_resolver` — 계약 형태 확인 · 창 종류 정규화 · 사건 IR 파싱/근거 대조 ·
  그리고 **요구 계층에 검증기를 주입해** issue 를 받는다.
- `project_resolution_to_plan` — 그 결과를 legacy plan 키 6개(`event_expression` ·
  `semantic_ir` · `audience_unsupported_hypotheses` · `audience_execution_assets` ·
  `audience_requirement.expression` · `.issues`)와 결정 로그로 투영한다.

투영의 산출은 `tests/test_audience_execution_projection.py` 가 6갈래로 **전량 고정**한다.
`semantic_ir.failure_kind` 3분기가 사용자에게 나가는 결말을 정하므로, 그 파일이 이 경로의
회귀 안전망이다.

## 7. 파라미터 바인딩

`CompiledSql` 은 언제나 (문장, 파라미터) 쌍이다. 값이 문장에 인라인되는 경우는
`ParameterStyle.INLINE_LITERAL` 을 **명시적으로** 고른 경로뿐이며, 그 경로는 완성된 SQL
문자열을 검사하는 기존 파이프라인(`sql_guard`·커버리지 검증)과의 호환을 위해 존재한다.
식별자는 파라미터로 묶을 수 없으므로 `SchemaBinding` 이 안전한 형태만 통과시킨다.

## 8. deprecated 와 다음 단계

`compatibility/legacy_event_expression.py` 는 3단계에서 삭제한다. 조건은 두 가지다.

1. `plan["event_expression"]` 의 생산자가 남지 않는다(구조화기·lowering·cutover 전부가
   `EventQuerySpec` 를 직접 저장한다).
2. 저장된 플랜 재생 경로(shadow/rollback)가 사양 스냅샷으로 재생된다.

아직 남은 것:

- `LogicalPlanSqlCompiler` 는 `EXISTS`/집계/정렬/상한/롤링 창까지 표현하지만, 실CRM 경로가
  쓰는 semi/anti 조인 전개와 char8 **절대** 경계는 아직 `event_compiler` 위임으로만
  지원된다. 이 항목을 지금 갚지 않는 근거는 `docs/plans_query_pipeline_debt.md` Phase 4.
- mypy strict 범위는 `query_pipeline/` + 그것이 따라 들어가는 모듈
  (`event_ir`·`event_compiler`·`sql_dialect`·`lexicon_patterns`·`semantic_fields`)이다.
  `calendar_window` 는 의존 폐포가 저장소 본체로 열려 있어 아직 경계를 끊어 둔다.

## 9. 검사 실행

```bash
pytest                 # 저장소 전체
mypy                   # query_pipeline/ strict + 따라 들어가는 모듈
ruff check .           # 검사 범위는 pyproject.toml [tool.ruff] include 가 선언한다
```
