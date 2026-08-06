# 조사 인수인계 — `구매주기가 30일 이하인 회원` 컴파일 실패

**상태: 조사 완료 / 수정 미착수. 코드는 한 줄도 고치지 않았다.**
작성 2026-08-06. 조사 시점의 워킹트리는 `M campaign_metric_claims.py, docs/architecture/member_scalar_and_composite_aggregates.md,
docs/data/runtime/semantics/audience_catalog.json, query_structurer/audience_execution.py,
tests/test_composite_aggregate_lowering.py, tests/test_retired_axes_fail_close.py` (HEAD `7d411e8`).

이 문서는 **재현 가능한 사실**만 담는다. 각 항목은 api 컨테이너 안에서 실행해 확인했고,
적대적 검증에서 뒤집힌 주장은 §7 에 별도로 남겼다 — **뒤집힌 것을 지우지 않은 이유는 다음 사람이
같은 잘못된 길을 다시 가지 않게 하기 위해서다.**

## 재현 환경

호스트 python 은 못 쓴다(asyncio DLL 차단). 모든 실행은 api 컨테이너에서:

```bash
docker exec recommendation-campaign-system-python-api-1 sh -lc 'cd /app && python <파일>'
docker exec recommendation-campaign-system-python-api-1 sh -lc 'cd /app && python -m pytest tests/<파일> -q'
```

`/app` 은 저장소 루트의 bind mount(rw)라 저장소에 쓴 파일이 즉시 보인다. **코드를 고친 뒤 라이브
API 로 재현하려면 컨테이너 재시작이 필요하다.** 요청별 LLM 로그는 `logs/rag_llm/<날짜>/<시각>-<해시>.jsonl`.

---

## 1. 증상

프롬프트 `구매주기가 30일 이하인 회원` → 사용자 메시지 **"요청한 조건을 실DB 술어로 컴파일하지 못했습니다."**
(`failure_messages.py:115`, `blocking.status != "explicit_unsupported"` 갈래)

LLM 구조화 3회 시도가 **전부 애플리케이션 검증에서 반려**되고 rules 폴백으로 떨어진다.
증거 로그 `logs/rag_llm/2026-08-06/085114-d730bb.jsonl` (레코드 5~11):

| attempt | 모델 출력 | 반려 사유 |
|---|---|---|
| 1 | `Comparison(FieldRef("member_scalar_buy_cycle.value"), "<=", Literal(30))`, `issues=[]` | `unsupported_semantics[compiler_operation_unsupported]` + `validation_mismatch[period]` |
| 2 | `aggregate.avg(member_metric_buy_cycle) + TimeFilter(rolling 30 days)` | `IrSchemaError: unsupported rolling unit: None` (`event_ir.py:277`) |
| 3 | attempt 1 과 같은 모양 | attempt 1 과 같은 두 사유 |

최종 진단(`artifacts/measure/run_after.json` id 7):
`rules_fallback:required_candidate_not_constructed`, `failure_stage=condition_recognition`,
`compile_outcomes[0].details.audience_expression_present=false`,
`recognized_symbols=[buy_cycle, member_metric_buy_cycle, member_metric_buy_cycle.value, buy_cycle, buy_cycle, purchase]`
— **심볼은 전부 인식됐는데 표현만 서지 않았다.**

---

## 2. 핵심 — 벽이 **둘**이고 각각이 필요조건이다

라이브 진입점(`query_structurer/structurer.py:375` `attach_campaign_query_plan_v4_identity`
→ `:380` `_audience_repair_error`)에서 잰 2x2. **두 에이전트가 독립적으로 같은 결과를 냈다.**

| 식 모양 | period 우회 | 결과 |
|---|---|---|
| flat comparison (모델 실제 출력) | 아니오 | `compiler_operation_unsupported` + `validation_mismatch[period]` / `unsupported` |
| flat comparison | 예 | `compiler_operation_unsupported` / `unsupported` |
| 정답 `Exists` | 아니오 | `validation_mismatch[period]` / `needs_clarification` |
| 정답 `Exists` | 예 | `issues=[]` / **`resolved`** |

**period 뒤에는 벽이 없다.** 정답 `Exists` + 스팬 주입으로 `graph_rag.build_query_plan` → `build_sql_result`
까지 밀면 `is_success=True` 로 SQL 이 나온다:

```sql
SELECT DISTINCT B.MEMBER_NO AS CUST_ID, ... FROM CRM_MB_BASEINFO B
WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'
  AND EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO MS
              WHERE MS.MEMBER_NO = B.MEMBER_NO
                AND MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)
                AND MS.BUY_CYCLE <= 30)
```

---

## 3. 벽 A — 애플리케이션이 프롬프트로 **컴파일 불가능한 모양을 지시**한다

### 소유자

`audience_runtime.py:776-777` 의 else 분기:

```python
recipe = json.dumps({"type": "field", "name": expression})
```

kind 가 aggregate/existence 도 아니고 function 도 없으면 **무조건 맨 field 를 찍는다.**
`graph_rag._v4_slot_guidance`(`graph_rag.py:253-256`)는 `return audience_runtime.audience_catalog_guidance()`
3줄 위임일 뿐이다 — **여기를 고치면 엉뚱한 파일을 고치는 것이다.**

생성된 안내 191~192행이 모델에게 이렇게 나간다:

```
[Metric recipes]
 - member_scalar_buy_cycle (구매주기): {"type": "field", "name": "member_scalar_buy_cycle.value"}
```

그리고 179행에 **경쟁 레시피**가 하나 더 있다:

```
 - buy_cycle (구매주기): {"type":"aggregate","function":"avg","relation":{"type":"source","name":"member_metric_buy_cycle"},...}
```

attempt 1·3 이 191행, attempt 2 가 179행에 정확히 대응한다. 즉 **표면어 `구매주기` 하나에 모순된
두 레시피가 걸려 있다.** (안내가 없는 것이 아니다 — 초기 가설은 틀렸다. §7 참조)

전달 경로: `prompt.py:114-116` 이 `slot_guidance` 를 `[Audience Semantic Catalog]` 절로 사용자
프롬프트에 넣는다. `StructuringContext` 생성처는 `api.py:82` 와 `graph_rag.py:16893` 둘뿐이고 둘 다
`slot_guidance` 를 넘기지 않으므로 **항상** `_v4_slot_guidance({})` 경로다.

### 범위 — 9개가 아니라 **14개 metric**

flat field recipe 를 받는 metric 전수 검사 결과 **14개 전부** `compiler_operation_unsupported`:

- `member_scalar_*` 9개: `activity_month_cnt, buy_cycle, buy_product_cnt, max_buy_amt, mean_buy_amt, min_buy_amt, total_buy_amt, total_buy_cnt, total_buy_qty`
- 추가 5개: `member_grade, member_grade_transition, member_newproduct_favor, member_worth_grade, member_worth_grade_transition`

실제 컴파일 예외:
`SqlCompileError: 'member_scalar_buy_cycle.value' 을 참조할 관계가 현재 스코프에 없습니다`
→ `event_compiler.py:1893` 이 이것을 `compiler_operation_unsupported` 로 접는다.

같은 지표를 `lower_member_scalar_metric` 으로 감싼 `Exists` 는 9개 전부 `supported` 다.

이 벽은 **문장에 의존하지 않는다.** 시간처럼 보이는 리터럴이 없는 문장에서는 이것만 남는다:
`누적 구매금액이 100만원 이상인 회원` + flat recipe → `[unsupported_semantics[compiler_operation_unsupported]]`.

---

## 4. 벽 B — 스칼라 임계값이 **사라진 기간 창**으로 계수된다

### 판정 지점

`audience_validators.py:123-232` `TemporalSpanValidator`, 판정은 `:181`:

```python
if expected > event_ir.count_time_constraints(condition):   # expected = source_time_span_count(...)
```

메시지 `원문에 있는 기간 조건이 canonical audience expression에서 누락되었습니다.` 는 저장소 전체에서
`audience_validators.py:187` 한 곳에만 있다(`argument="period"` 의 또 다른 생산자
`canonical_audience_claims.py:1290` `window_kind_issues` 는 메시지가 다르다 — 로그 135라인 중 134가 전자).

### 왜 1 이 되는가

```
event_parser.source_time_span_count("구매주기가 30일 이하인 회원") == 1
event_parser.source_time_span_count(..., masked_spans=((6,9),)) == 0
```

`source_time_span_count`(`event_parser.py:551-574`)는 **사건어가 든 절**만 본다(`_event_clauses`).
`구매주기` 안의 `구매` 가 사건어로 걸려 문장 전체가 사건 절이 되고, 그 안의 맨 `30일` 이
`RollingWindow(30, day)` 로 파싱된다. 대조 실측:

| 문장 | span_count | 이유 |
|---|---|---|
| `구매주기가 30일 이하인 회원` | **1** | `구매` 사건어 + `30일` 창 → **오탐** |
| `평균 구매주기가 30일 이하인 회원` | **1** | 동일 |
| `누적 구매금액이 30만원 이상인 회원` | 0 | 사건 절이지만 창 없음 |
| `활동 개월 수가 6개월 이상인 회원` | 0 | 사건어 없음 |
| `최근 30일 구매한 회원` | 1 | **진짜 창** |

### 해독제가 도달하지 못한다

`scalar_literal_spans` 는 `_ApplicationOwnedSynthesis` 에서만 온다 —
`query_structurer/audience_execution.py:785-787`:

```python
scalar_literal_spans=(synthesis.scalar_literal_spans if synthesis is not None else ())
```

그 `synthesis` 를 만드는 갈래는 둘뿐이고 **둘 다 이 문장에 도달하지 않는다**:

| 갈래 | 위치 | 왜 안 되는가 |
|---|---|---|
| `_application_owned_synthesis` | `:731-737` | `raw_expression is None and issues` 요구. 라이브 모델은 `expression≠None, issues=[]` |
| `_conjoinable_synthesis` | `:744` | 존재하지만 `:585-588` 에서 **시간 축(`_temporal_synthesis`)으로만** 제한 — member_scalar 는 통과 못 함 |

`scalar_literal_spans` 의 생산 지점은 `:567`(temporal), `:640`(member_scalar), `:665`(campaign_average)
세 곳이고 **`tests/` 전체에서 참조 0건**이다.

### 이 벽은 "넘을 수 없는 벽"이 아니다 — 더 나쁘다

검증기는 창의 **개수만** 비교한다. 정답 `Exists` 에 아무 `TimeFilter` 하나를 덧붙인 표현
(`Exists(member_scalar_buy_cycle, BUY_CYCLE<=30) AND Exists(purchase, rolling 30 day)`)을 넣으면
`issues=[]`, `resolved`, `is_success=True` 로 **SQL 이 나간다**. 사용자가 말하지 않은
`최근 30일 구매` 가 덧붙은 **조용히 다른 오디언스**다.

그리고 재시도 프롬프트(`prompt.py:234`)가 검증 오류 문자열을 그대로 모델에게 주입하므로,
모델은 "기간 조건이 누락되었습니다" 를 읽고 **창을 지어내도록 적극 유도된다** — attempt 2 가 정확히
그것이다. 즉 이 벽은 재시도 예산을 태우는 데 그치지 않고 **틀린 방향으로 민다.**

---

## 5. 테스트는 초록인데 라이브는 실패하는 이유

`tests/test_retired_axes_fail_close.py:231` `test_profile_scalar_metric_request_compiles_to_the_snapshot_row`
는 `_raw(BUY_CYCLE_QUERY, issues=[unsupported])` 로 **`expression=None`(기본값) + issue 1개**를
주입한다 → `audience_execution.py:731` 조건을 만족 → 합성 분기 → spans 채워짐 → 통과.
(`docker exec ... pytest tests/test_retired_axes_fail_close.py -q` → **25 passed**)

라이브 모델은 recipe 를 따라 **식을 냈고 `issues=[]`** 였으므로 그 갈래에 아예 들어가지 않는다.
계측으로 확정:

```
A. 라이브 실제 모양 (recipe + issues=[])
   issues=['unsupported_semantics[compiler_operation_unsupported]', 'validation_mismatch[period]']
   _application_owned_synthesis 호출: []          ← 한 번도 안 불린다

B. 테스트가 주입하는 모양 (expression=None + issue 1개)
   issues=[]  is_success=True
   호출: [('구매주기가 30일 이하인 회원', 1, 'member_scalar_metrics.catalog_literal_operator', ((6,9),(10,12)))]

C. B 와 같은 입력인데 dataclasses.replace 로 spans=() 강제
   issues=['unsupported_semantics[purchase_cycle]', 'validation_mismatch[period]']
   is_success=False  failure_reason=semantic_registry_gap  SQL 없음
```

**C 가 결정적이다** — 이 테스트의 초록은 오직 합성기가 넘겨주는 `scalar_literal_spans` 에 얹혀 있다.
그러니 이 테스트가 고정하는 것은 "구매주기 요청이 SQL 로 나간다"가 아니라
**"합성기가 이미 불린 뒤라면 SQL 로 나간다"** 이다. 라이브가 그 전제를 만족하지 못하게 만드는 것이
바로 같은 애플리케이션의 `[Metric recipes]` 안내다(§3).

---

## 6. 회귀 경위

> ⚠️ 이 절만 **적대적 검증을 거치지 못했다**(검증 에이전트가 중단됨). 나머지 절보다 신뢰도가 낮다.

- **"8/4 에는 성공했다"는 전제가 틀렸다.** `artifacts/live_prompt_postfix_20260804_final6_a.json` 은
  커밋 `9429e99 (8/4 07:15 KST)` 에서 추가됐고, 그 실행은 **8/3** 이다(로그는 UTC 디렉터리
  `logs/rag_llm/2026-08-03/` 의 011207·015542·020326·035824).
- **그 8/3 성공은 Event IR 경로가 아니었다.** 당시 구조화 산출물은
  `expression=null, issues=1, semantic_plan.nodes=[], audience_authority=None` — 오늘 실패 런과
  사실상 같다. SQL 은 SemanticPlan/레거시 슬롯 레인이 냈다.
- 1차 회귀(sql→clarification)는 **8/4 15:21~16:05 KST**(커밋 `f0e47e5` 구간)에 이미 발생.
  `live_prompt_lane_census_20260804.json` id 7 → `clarification / semantic_structurer_failure`.
- `6e93213 (8/5 14:49) "semanticPlan 제거"` 가 기존 소유자를 삭제했다:
  `profile_metric_claims.py`, `legacy_plan_compiler.py`, `semantic_plan*.py`, 그리고
  **`_semantic_reextractor`**(2차 구조화 패스 = 재시도 예산 6회 → 3회).
- `6a461e5 (8/5 16:05) "event ir 기능 강화"` 가 축을 되살리며 **동시에** 세 가지를 했다:
  (1) 모델에게 `member_scalar_*` 심볼 노출, (2) 그 심볼을 쓰면 반드시 지는 검증 조합
  (`audience_execution.py:732/786` + `TemporalSpanValidator`), (3) 결과적으로 성공 조건이
  **"모델이 3번 안에 스스로 `expression=null` 을 낼 것"** 이라는 확률 게임이 됨.
- 8/6 같은 코드에서 7런 중 **1런만 성공**(`055708` — attempt 2 가 `expression:null` 을 내서
  `member_scalar_metrics.catalog_literal_operator` 합성이 탔다).
- **라우팅은 원인이 아니다.** 8/3·8/4·8/6 전부 `attempt1 = gpt-4o-mini/structuring_override`,
  `attempt2+ = gpt-5-mini/validation_repair` 로 동일(`rag/llm_io.py:90 _campaign_structuring_route`).

---

## 7. 영향 범위

### 회귀 코퍼스

`artifacts/measure/run_after.json` 회귀 15개(`2,6,7,8,10,12,15,17,22,23,25,74,75,76,77`) 중
**이 결함이 설명하는 것은 id 7 하나다.** id 12(2개)·77(1개)은 span>0 이지만 세어진 것이 **진짜 창**이고,
나머지는 span=0.

`live_prompts.json` 86개 중 `source_time_span_count > 0` 인 것은 24개
(`7,9,11,12,14,27,38,39,40,42,43,45,53,62,66,69,71,72,77,79,80,83,84,86`, `today` 인자 필요 —
`today=None` 이면 20개). 그중 **세어진 것이 창이 아닌 문장은 3개**:

| id | 프롬프트 | 세어진 것의 실제 정체 |
|---|---|---|
| 7 | 구매주기가 30일 이하인 회원 | `buy_cycle <= 30` 스칼라 임계 |
| 39 | 회원별 평균 구매주기가 30일 이내이고 다음 구매예정일이 지난 고객… | 동일 |
| 53 | 앱과 PC 양쪽 채널에서 모두 주문한 회원 | **유령 창** — 아래 (b) |

로그 2,214개 전수 스캔: `validation_mismatch[period]` 를 실은 프롬프트 26종, 최다 발생이 id 7(33회).

### 지표 레지스트리

`docs/data/runtime/sql/member_metrics.json` 의 9개 중 `threshold_unit` 이 시간 단위인 것은 2개:

| metric_id | threshold_unit | 동의어×어미 전수 | 함정? |
|---|---|---|---|
| `buy_cycle` (:153) | **day** | 5×3 = **15/15 가 span 1** | **예** |
| `activity_month_cnt` (:167) | month | 4×3 = 12/12 가 span 0 | 지금은 아니오 |

`activity_month_cnt` 가 안전한 것은 **설계가 아니라 아래 (a) 결함 덕분**이다. 동의어 4개 중
`구매 활동 개월 수` 만 사건어를 품어 `_event_clauses` 를 통과하고(실측 `event_clause=True`,
`parse_time_windows=[{6, months}]`), **월 계수를 켜는 순간 즉시 같은 함정에 들어간다.**

### 조사 중 드러난 인접 결함 2종 (같은 검증기, 별개 방향)

**(a) 상대 월/년 창은 계수에서 조용히 사라진다.** `_clause_windows`(`event_parser.py:284-312`)가
`days` 키 없는 창을 `AbsoluteInterval` 로도 못 만들면 버린다. `6개월`/`3개월`/`1년`/`1개월` → 0.
단 **달력 고정 구간은 규모와 무관하게 정상 계수된다**(`지난달`/`지난 주`/`2019년`/`2026년 2월` → 1).
정확한 규칙: *상대 지속기간은 `days` 키가 있어야 세고(일·주), 달력 고정 구간은 항상 센다.*

**(b) 공백 제거 표면 매칭이 유령 창을 만든다.** `calendar_window.py:1138` `WORD_DURATION_SPECS` 의
`두주`(14일)·`한주`(7일)가 compact 텍스트에서 어절을 가로질러 매칭된다:

```
"…모두 주문한…" → compact "…모두주문한…" → "두주" → RollingWindow(14, day)
"한 주문건"      → "한주" → 7일
```

코퍼스 유령 창은 id 53 하나. 지금은 앞단(`semantic_registry_gap`)에서 먼저 막혀 드러나지 않는다.

### 안전망으로서 이 검증기가 지켜야 하는 계약

1. **전체 이력 폴백 금지.** `최근 30일 구매` 가 전 기간 구매가 되는 것은 오류가 아니라 조용히 다른 오디언스다.
2. **면제는 구간(span) 좌표 단위여야 한다.** 실측: `구매주기가 30일 이하이고 최근 30일 이내 구매한 회원`
   → 마스킹 없음 2, 스칼라 구간만 1, 술어 전체(0,12) 1, 둘 다 0. **값·단위 기준 매칭이나 절 전체 면제로
   가면 같은 문장의 진짜 창까지 지운다.**
3. **이 판정에는 테스트가 하나도 없다.** `tests/test_audience_validators.py:107-238` 의
   TemporalSpanValidator 테스트 7개는 전부 bare `최근` → `missing_argument` 갈래이거나 침묵 방향이다.
   `event_ir.validate_time_preserved` 는 **프로덕션 소비자 0**(테스트 전용).

---

## 8. 소유권 — 추천안과 그 근거

### 문제 정의

"이 리터럴은 창이 아니라 스칼라 임계값"이라는 지식이 지금 `_ApplicationOwnedSynthesis` 라는
**합성 부산물**에만 있다. 생산자가 바뀌면(= 모델이 식을 내면) 사라진다. 그것이 이번 결함의 정확한 형태다.

원장(`literal_bindings`)에는 그 지식이 **없다**. 실측:

```
"구매주기가 30일 이하인 회원"
  duration_1  '30일' (6,9)  normalized={value:30, semantic_unit:'days', temporal_kind:'rolling_duration'}
  comparison_operator_1 '이하' (10,12)  normalized='<='
"최근 30일 구매한 회원"
  duration_1  '30일' (3,6)  temporal_kind='rolling_duration'     ← 원자가 완전히 동일
```

`temporal_kind='rolling_duration'` 은 `calendar_window` 가 표지를 못 찾았을 때의 **기본값**이라
스칼라를 배제하기는커녕 적극적으로 창이라고 주장한다. 원장이 배제할 수 있는 것은 `past_point` 하나뿐이고
그 배제는 이미 `member_scalar_metric_claims.py:89` 가 쓰고 있다.

**스칼라인지 창인지를 가르는 유일한 정보는 원장 밖에 있다** — 레지스트리 표면어(`구매주기`) ↔
임계 리터럴 ↔ 비교 연산자의 **인접**.

### 추천: `member_scalar_metric_claims` 가 소유하는 **식 역산 스팬 청구**

issue 를 받지 않는 공개 함수 하나:

```python
consumed_scalar_threshold_spans(
    query, expression, literal_bindings, registry, catalog
) -> tuple[tuple[int, int], ...]
```

성립 조건 — **전부 필요. 하나라도 어긋나면 빈 튜플(= fail-close)**:

1. 식에 `Exists(Filter(Source(member_scalar_X), Comparison(FieldRef(X.value), op, Literal(v))))` 원자가 있다
2. `catalog.metric(X).kind == "member_scalar"` 이고 레지스트리가 그 지표의 `threshold_unit` 을 선언한다
3. 원문에 그 지표의 `synonyms` 가 등장한다
4. 표면어 → threshold binding → 연산자 binding 이 그 순서로 인접하고 사이에 조사·공백만 있다
   (`member_scalar_metric_claims.py:203-207` 의 국소 규칙)
5. binding 의 (단위, 값) == (선언 `threshold_unit`, `Literal.value`), 연산자 binding 의
   `normalized` == `Comparison.operator`

소비 지점은 `query_structurer/audience_execution.py:785-787` — 지금 `synthesis` 에서만 스팬을 꺼내는
그 자리에서, **`synthesis` 유무와 무관하게** 이 함수를 함께 부른다.

### 왜 이 안인가

- **소유권이 한 모듈에 남는다.** 현행 합성 경로(`_member_scalar_synthesis`)도 같은 함수를 쓸 수 있어
  지식이 두 벌이 되지 않는다.
- **청구가 식의 구조로 증명된다.** 조건 3·4 를 빼고 (값,단위)만 매칭하면 **실측 반례**가 통과한다:
  `최근 30일 구매한 회원` + 환각 `Exists(member_scalar_buy_cycle <= 30)` + 마스킹 `(3,6)` → `issues=[]`
  로 전 검증 통과, 뜻이 `MS.BUY_CYCLE<=30` 으로 바뀜. 별칭+인접을 요구하면 `_alias_candidates` 가
  빈 목록이라 **청구 자체가 생기지 않는다.**
- **마스킹은 스팬 하나만 지운다.** 어순을 바꿔도 정확히 한 스팬만 청구되고 같은 문장의 진짜 창은
  그대로 세어진다(실측 4행, §7 계약 2).
- **새 추상이 아니다.** `rolling_absence_claims.consumed_literal_binding_indices`(`:50`),
  `canonical_audience_claims.temporal_obligation_compiled_spans`(`:1104`)와 동형이고, 셋 다
  `canonical_claim_issues`(`:1303`)를 통해 모델·합성 양 경로를 지난다. **지금 문제의 판정만 그 대열에서
  빠져 있다.**

### 기존 테스트 제약 (규칙 47)

`tests/test_revived_axes_event_ir_only.py:205` `test_member_scalar_synthesis_refuses_an_ambiguous_sentence`
가 `구매주기가 30일 이하이고 여성인 회원` 을 **미해결로 남을 것**으로 못 박는다.
`_whole_phrase_matches` 의 마지막 줄 `audience_frame.is_frame_only` 는 **문장 전역** 판정이라 혼합문에서
False 다(실측). → **공유 함수 `_whole_phrase_matches` 를 완화하지 말고, 국소 인접 규칙만 새 함수에 둔다.**

### 검토했다 버린 후보

| 후보 | 왜 버렸나 |
|---|---|
| B. 표면 전용 청구(식 무관) | 모델이 그 절을 창으로 오독해도 마스킹이 그대로 나간다. 백스톱이 있을 거라 봤으나 없다(§9) |
| D. `TemporalSpanValidator` 가 직접 판정 | 검증기가 레지스트리 표면어를 들게 된다(어휘는 데이터, 구조만 코드). **단 테스트가 강제하는 사실은 아니다 — 설계 판단이다** |
| E. `source_time_span_count` 가 지표 어휘를 알게 | 달력 파서가 도메인 의존이 된다. `masked_spans` 의 docstring 계약(`event_parser.py:557-566`)이 애초에 "소유권은 밖에서 알려준다"는 설계라 되돌리는 셈 |
| F. 원장 생산자가 `temporal_kind='scalar_*'` 판정 | `event_ir.CALENDAR_KIND_WINDOW_TYPES`(`:357`, `{rolling_duration, past_point}` 2개)에 없는 kind 는 `apply_window_kinds`/`window_kind_issues` 기대 집합 밖으로 떨어진다. 게다가 `source_time_span_count` 는 원장이 아니라 달력 파서를 다시 돈다 |
| G. `temporal_clause.marker_bound` 재사용 | **실측 반례 2개**: `30일 이내 구매한 회원` → `marker_bound=False` 인데 진짜 창; 혼합문에서 스칼라 `'30일'@(6,9)` 이 다른 절의 `'최근'@(15,17)` 에 붙는 오배정 |

---

## 9. 조사 중 **뒤집힌** 주장 (같은 길을 다시 가지 않도록)

| 한때 믿었던 것 | 실제 |
|---|---|
| "치명적인 것은 period 하나다" | 벽이 둘이고 **각각 필요조건**. recipe 결함은 문장 무관 전역 벽 |
| "프롬프트에 `member_scalar_*` shape 안내가 없다" | 안내는 **있다**. 191행이 bare FieldRef 를 명시 지시하고 179행에 경쟁 레시피가 하나 더 있다 |
| "`[Metric recipes]` 는 `graph_rag._v4_slot_guidance` 가 만든다" | `audience_runtime.py:776-777` 이 만든다. graph_rag 는 3줄 위임 |
| "period 벽은 모델이 어떤 표현으로도 못 넘는다" | 개수만 비교하므로 **군더더기 TimeFilter 하나로 넘는다** → 조용히 다른 오디언스 SQL 이 나간다 |
| "이 벽은 member_scalar 8개에 걸린다" | 9개 + 등급/전이/선호 5개 = **14개** |
| "모델 bare Comparison 에 issue 가 3개 난다(`literal_bindings[1]` 미소비 포함)" | **2개다.** `canonical_claim_issues` 는 `[]` 를 돌려준다 |
| "마스킹을 넓히면 `최근 30일 구매` 오독이 `issues=[]` 로 통과한다" | **통과하지 않는다.** 프로덕션이 항상 붙이는 `literal_bindings` 를 넣으면 `validation_mismatch[literal_bindings[0]]` 로 막힌다. 이전 실측은 `literals=[]` 로 돌린 결과 |
| "카운터는 일/주 단위만 센다" | 달력 고정 구간(`지난달`/`2019년`)은 규모와 무관하게 센다. 버려지는 것은 **상대** 월/년뿐 |
| "회귀 15개 중 12개는 이 검증기가 관여하지 않았다" | span=0 이 끄는 것은 '창 소실' 갈래뿐. 두 번째 갈래(`_INCOMPLETE_RECENCY_RE`, `:198-229`)는 계속 돈다(id 17) |
| "모델 evidence 좌표는 못 쓴다" | attempt 1 만 어긋났고(`start=0,end=10` vs 12자 text) attempt 3 은 유효하다. **결론(재계산해야 안전)은 유지하되 이유가 다르다** |

---

## 10. 다음 사람이 할 일

### 수정 (둘 다 해야 한다 — 하나만 고치면 이 프롬프트는 계속 죽는다)

1. **벽 A**: `audience_runtime.py:776-777` 의 else 분기가 `member_scalar` kind 에 대해
   `Exists(Filter(Source, Comparison))` recipe 를 내도록. 동시에 `구매주기` 표면어의 **경쟁 레시피
   두 줄**(179행 member_metric avg / 191행 member_scalar field)을 정리해야 한다 — 남겨두면 모델이
   계속 둘 사이를 오간다.
2. **벽 B**: §8 의 `consumed_scalar_threshold_spans` 를 `member_scalar_metric_claims` 에 추가하고
   `audience_execution.py:785-787` 에서 `synthesis` 유무와 무관하게 호출.

### 반드시 추가할 테스트 (지금 전무하다)

- 라이브 모양 고정: **`expression≠None` + `issues=[]`** 로 주입해 SQL 까지 나오는지.
  현행 `test_retired_axes_fail_close.py:231` 은 이 모양을 건드리지 않는다.
- 반대 방향(안전망) 고정: `최근 30일 구매한 회원` 을 스칼라로 오독한 표현이 **반려되는지**.
- 혼합문에서 진짜 창이 살아남는지: `구매주기가 30일 이하이고 최근 30일 이내 구매한 회원` → 마스킹 후 span 1.
- `구매주기가 30일 이하이고 여성인 회원` 이 **여전히 미해결**인지(`test_revived_axes_event_ir_only.py:205` 유지).
- 14개 metric recipe 가 전부 `compiler_capability == supported` 인지(레시피 래칫).

### 미확인으로 남은 것

- **§6 회귀 경위는 적대적 검증을 못 거쳤다**(에이전트 중단). 커밋 귀속을 그대로 인용하기 전에 재확인할 것.
- recipe 문자열을 고친 뒤 **모델이 실제로 `Exists` 를 내는지** — LLM 재실행을 안 해 미측정.
- `SEMANTIC_VALIDATION_V2` / `SEMANTIC_AST_GATE` 가 켜진 라이브 API 전 구간 미검증
  (컨테이너 env 에는 `AUDIENCE_DEFAULT_PERIOD=3 day` 만 있다).
- pytest **전체** 스위트 미실행. 확인한 것은 `tests/test_retired_axes_fail_close.py` 25 passed 뿐이다.
- (a) 상대 월/년 계수를 켤지 여부는 **정책 결정**이다. 켜면 `구매 활동 개월 수` 가 즉시 같은 함정에 들어가고,
  안 켜면 그 부류에서 안전망이 계속 죽어 있다. 어느 쪽이든 (a)(b)를 함께 처리하지 않으면
  **한쪽 결함이 다른 쪽 결함을 가린다.**

---

## 근거 파일 색인

| 무엇 | 위치 |
|---|---|
| 사용자 메시지 문구 | `failure_messages.py:115` |
| period 판정 | `audience_validators.py:123-232` (판정 `:181`, 메시지 `:187`) |
| 창 계수 | `event_parser.py:551-574` (`source_time_span_count`), `:577-584` (`_mask_spans`), `:284-312` (`_clause_windows`) |
| 유령 창 출처 | `calendar_window.py:1138-1158` (`WORD_DURATION_SPECS`) |
| 합성 분기 / 스팬 전달 | `query_structurer/audience_execution.py:731-737`, `:744`, `:785-787` |
| 스팬 생산 3지점 | 같은 파일 `:567`, `:640`, `:665` |
| flat recipe 생성 | `audience_runtime.py:776-777` (위임 `graph_rag.py:253-256`) |
| 프롬프트 조립 / 재시도 | `query_structurer/prompt.py:114-116`, `:121-123`, `:234` |
| 컴파일 능력 판정 | `event_compiler.py:1881-1905` (`:1893` 이 예외를 접는다) |
| 낮추기 | `member_scalar_metrics.lower_member_scalar_metric`, 청구 규칙 `member_scalar_metric_claims.py:71/151/186/203-207/212` |
| 지표 선언 | `docs/data/runtime/sql/member_metrics.json` (`buy_cycle` :149-161, `threshold_unit` :153) |
| 라우팅 | `rag/llm_io.py:90` `_campaign_structuring_route`, 호출부 `graph_rag.py:330` |
| 결함 분기를 비껴가는 픽스처 | `tests/test_retired_axes_fail_close.py:51-72`, `:231-265` |
| 혼합문 미해결 래칫 | `tests/test_revived_axes_event_ir_only.py:205` |
| 실측 로그(실패) | `logs/rag_llm/2026-08-06/085114-d730bb.jsonl` |
| 실측 로그(오늘 유일한 성공) | `logs/rag_llm/2026-08-06/055708-ae4f1f.jsonl` |
| 계측 아티팩트 | `artifacts/measure/run_after.json` (id 7), `run_before.json` |
