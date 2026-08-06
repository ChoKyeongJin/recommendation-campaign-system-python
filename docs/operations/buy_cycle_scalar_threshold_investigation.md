# 조사 인수인계 — `구매주기가 30일 이하인 회원` 컴파일 실패

**상태: 조사 완료 / 벽 A·벽 B 구현 완료(미커밋) — 라이브 LLM 경로는 미달성.**
구현 기록·실측 결과는 [§구현 기록 (2026-08-06)](#구현-기록-2026-08-06), 항목별 판정은 문서 마지막
`완료 판정 기준` 을 본다. **§1~§9 의 조사 서술은 조사 시점(코드 수정 전)의 사실이며 그대로 보존한다** —
그중 구현 과정의 실측으로 뒤집힌 전제는 §구현 기록의 "실측으로 뒤집힌 조사 전제" 표에 모아 두었다.

조사 작성 2026-08-06. 조사 시점의 워킹트리는 `M campaign_metric_claims.py, docs/architecture/member_scalar_and_composite_aggregates.md,
docs/data/runtime/semantics/audience_catalog.json, query_structurer/audience_execution.py,
tests/test_composite_aggregate_lowering.py, tests/test_retired_axes_fail_close.py` (HEAD `7d411e8`).

이 문서는 **재현 가능한 사실**만 담는다. 각 항목은 api 컨테이너 안에서 실행해 확인했고,
적대적 검증에서 뒤집힌 주장은 §7 에 별도로 남겼다 — **뒤집힌 것을 지우지 않은 이유는 다음 사람이
같은 잘못된 길을 다시 가지 않게 하기 위해서다.**

## 결론 요약

**이 결함은 날짜 파서 하나의 문제가 아니다.** 서로 독립적인 벽이 둘이고 각각이 실패의
**필요조건**이다(§2 의 2x2 실측). **벽 A 와 벽 B 를 모두 고쳐야 한다.**

- **벽 A — LLM 에 제공되는 metric recipe 가 컴파일 불가능하거나 서로 충돌한다(§3).**
  애플리케이션이 프롬프트에 넣는 `[Metric recipes]` 안내가 `member_scalar_*` 에 대해 참조할 관계가
  없는 bare field recipe 를 지시한다(실측: 14개 metric 전부 `compiler_operation_unsupported`).
  게다가 같은 표면어 `구매주기` 에 aggregate avg recipe(179행)와 member_scalar field recipe(191행)
  **두 개가 동시에** 걸려 서로 충돌한다. 문장에 시간 리터럴이 없어도 남는 **전역 벽**이다.
- **벽 B — `30일` 같은 단위 리터럴의 소유권 정보가 특정 합성 경로에만 묶여 있다(§4).**
  그 리터럴이 스칼라 지표의 임계값인지 실제 시간 창인지 구분하는 지식(`scalar_literal_spans`)이
  `_ApplicationOwnedSynthesis` 라는 합성 부산물에만 존재한다. 생산자가 LLM 으로 바뀌면
  (`expression≠None, issues=[]`) 그 지식이 사라지고, 스칼라 임계값이 "누락된 기간 창"으로 계수된다.

**둘 중 하나만 고치면 이 프롬프트는 계속 실패하거나 잘못된 SQL 이 생성될 수 있다.**

- 벽 A 만 고치면: 모델이 올바른 `Exists` 를 내도 `validation_mismatch[period]` 로 계속 반려된다
  (§2 표 3행 → `needs_clarification`).
- 벽 B 만 고치면: 모델이 여전히 bare field recipe 를 따라가 `compiler_operation_unsupported` 로 죽거나
  (§2 표 2행), 검증기가 창의 **개수만** 비교하는 틈을 타 사용자가 말하지 않은 `최근 30일 구매` 가
  덧붙은 **조용히 다른 오디언스 SQL** 이 나갈 수 있다(§4 마지막).

**권장 해결책은 문장별 예외처리가 아니라 `member_scalar` 지표 전체에 적용되는 레지스트리 기반
공통 규칙이다.** `구매주기` 문자열만 검사하는 단건 하드코딩은 이 결함을 다른 13개 metric 에 그대로
남긴 채 덮는다(§8 안티패턴).

> 권장안은 §8 의 식 역산 스팬 청구 방식이다. 다만 이 방식은 벽 A 의 recipe 수정과 함께 적용해야 하며,
> 반대 방향 의미 변조를 차단하는 테스트까지 포함해야 한다.

실행 지침은 §10, 완료 판정은 문서 마지막 `완료 판정 기준` 절을 본다.

## 구현 기록 (2026-08-06)

HEAD `6b2aeb4` 위 **미커밋 워킹트리**. 아래는 전부 api 컨테이너에서 실제 실행해 얻은 출력이다.

### 무엇을 고쳤는가

| 파일 | 변경 |
|---|---|
| `audience_runtime.py` | `[Metric recipes]` 루프에서 recipe 조립을 **공개 함수 `metric_recipe_wire(declaration)`** 로 분리. member_scalar 9 / field 3 / transition 2 = **14종이 bare field 대신 `Exists(Filter(Source, Comparison))` 골격**을 안내받는다. 자리표시자 5개를 모듈 상수(`METRIC_RECIPE_*`)로 노출, 선언이 불완전하면 `None` → 안내에 "recipe 없음, 이 지표를 쓰지 않는다"(규칙 11, 조용한 생략 금지). `[Fixed wire shapes]` 4줄 추가, `[Metric kind selection]` 절을 **카탈로그에서 파생**(label 충돌 9쌍) |
| `member_scalar_metric_claims.py` | **`consumed_scalar_threshold_spans()` 신설**(§8 의 7개 조건, fail-close). 국소 인접 규칙만 `_threshold_phrase_is_adjacent` 로 추출해 기존 `_whole_phrase_matches` 와 공유 — **`_whole_phrase_matches` 의 동작은 불변**(규칙 47). 예외는 `resolved_semantic_catalog.CatalogError` 만 국소적으로 잡는다 |
| `query_structurer/audience_execution.py` | `_claimed_scalar_threshold_spans()` 신설. `run_audience_resolver` 가 **`synthesis` 유무와 무관하게** 호출해 기존 `scalar_literal_spans` 와 **합집합**(치환 아님). 레지스트리 스냅샷이 `None` 이면 `()` |
| `tests/test_metric_recipe_capability.py` (신규 112건) | 안내 recipe 를 `event_compiler.validate_compiler_capability` 로 실제로 재는 래칫 |
| `tests/test_member_scalar_threshold_spans.py` (신규 31건) | **청구 좌표 자체**를 고정(문장·표면어는 레지스트리에서 파생) |
| `tests/test_retired_axes_fail_close.py` (+2건) | 라이브 모양(`expression≠None, issues=[]`) 갈래 고정. 기존 테스트는 손대지 않았다 |

**`구매주기` 같은 문자열 하드코딩은 없다.** 지표별 차이는 전부 레지스트리·카탈로그 데이터에서 파생한다.

### 실측 결과

```text
capability          14/14 supported (catalog compile_context 필수 — 기본 컨텍스트로 재면 다른 사유가 나온다)
guidance            207행/21,150자 → 238행/28,905자, 두 번 호출 바이트 동일(결정론 유지)
                    aggregate 15종 recipe 문자열 불변, [Metric recipes] 밖 기존 줄 전부 보존
청구 좌표           Q1 '구매주기가 30일 이하인 회원'          → ((6,9),(10,12))  창 1 → 0
                    Q2 '최근 30일 구매한 회원' + 환각식        → ()               창 1 → 1
                    Q3 혼합문                                  → ((6,9),(10,12))  창 2 → 1   (18,21) 미청구
결말 변화           Q1 라이브 모양: HEAD ['validation_mismatch[period]'] → 수정본 []
테스트              영향 5파일 209 passed / 96.09s
                    영향+guidance·별칭 7파일 288 passed / 129.44s
                    전체 스위트 5 failed, 3301 passed, 29 skipped / 415.61s
                      └ 실패 5건(test_aggregation_decimal 2 / test_money_literal_bindings 2 /
                        test_semantic_literal_characterization 1)은 **이 변경과 무관한 기존 실패**.
                        HEAD 사본 트리에서 동일하게 5 failed 재현.
ruff                프로젝트 설정(`ruff check .`) 6 errors — 전부 HEAD 에도 있는 기존 위반, 신규 0
```

### 라이브 LLM — **개선을 증명하지 못했다**

| 트리 | 프롬프트 | 성공 |
|---|---|---|
| HEAD 사본 | `구매주기가 30일 이하인 회원` | 0/5 |
| 수정본(연속 배치) | 같은 문장 | 0/5 |
| 수정본(앞선 배치) | 같은 문장 | 3/5 |
| 수정본 합계 | 같은 문장 | **3/14** |
| 수정본 | 혼합문 | 1/6 |

같은 코드가 배치마다 0/5 ↔ 3/5 로 뒤집힌다 — **노이즈가 지배해 이 표본으로는 개선도 악화도 말할 수 없다.**
오늘 응답 43개 집계로 잡힌 **구체적 방출 실패 3종**(다음 사람의 1순위 작업):

1. **evidence 오배치 33/43** — 모델이 evidence 를 `Comparison` 이 아니라 자식 `literal` 안에 넣는다
   → `조건에 원문 근거가 없습니다: comparison` 19회. **HEAD 사본 배치에서도 나오므로 이 변경이 만든 것인지 분리하지 못했다.**
2. **임계 리터럴 타입 불안정** — member_scalar 응답 22개 중 문자열 `"30"` 13 / 숫자 `30` 9.
   문자열이면 청구 조건 6(값 일치)이 깨져 `claimed=()` → 반려. 통과했다면 SQL 이 `MS.BUY_CYCLE <= '30'` 이 된다(직접 컴파일 확인).
   자리표시자가 따옴표 안이라 의심했으나 A/B(5:5)는 결과가 뒤집혀 **인과 미증명**.
3. **aggregate 쌍둥이 오가기가 없어지지 않았다** — 응답 43개 중 `member_scalar_buy_cycle` 22 / `member_metric_buy_cycle` 20.
   `[Metric kind selection]` 절을 넣었는데도 그렇다 → **§10 벽 A 체크리스트 4항(충돌 제거)은 미달성**이다.

### 이번 변경이 **새로 연 표면** (위험)

스냅샷 5종(`member_grade`, `member_worth_grade`, `member_newproduct_favor`, 전이 2)은 HEAD 에서 애초에
컴파일 불가였으나 이제 컴파일된다. 그런데 `member_month_snapshot` 소스는 `extra_predicates=()` 라
**월 고정이 붙지 않는다**(최신월 고정은 `member_scalar_*` 소스에만 있다):

```sql
member_scalar_buy_cycle → … AND MS.YYYYMM = (SELECT MAX(YYYYMM) …) AND MS.BUY_CYCLE = 30
member_grade            → … AND MS.ZTS_GRADE = 'MEM_GRADE_CD.FAMILY'      ← 월 고정 없음
member_grade_transition → … AND (MS.ZTS_GRADE = …) AND (MS.PREV_ZTS_GRADE = …) ← 월 고정 없음
```

즉 "기준월 등급"이 아니라 **"어느 달이든 한 번이라도"** 로 낮아진다. 실데이터가 단일월이라 지금은 결과가
같지만 **다월 적재 시 조용히 뜻이 바뀐다.** 선언의 `time="snapshot_month"` / `coverage="monthly_attribute_snapshot"`
가 컴파일에 반영되는지는 **미확인**이다(resolved SourceSpec 의 `coverage` 는 `'unknown'`). capability `supported`
는 의미 정합의 증거가 아니다.

부수로 확인된 기존 결함: transition recipe 2종은 비-transition 쌍둥이와 **문자열이 완전히 동일**했고
`prev_expression_field` 가 통째로 누락돼 있었다(전이 요청이 조용히 단일 시점 비교로 낮아진다).
이번 변경에서 prev 필드를 포함시켰다.

### 실측으로 뒤집힌 조사 전제 (§1~§9 본문은 그대로 두고 여기에 정정만 모은다)

| 조사 서술 | 실측 정정 | 근거 |
|---|---|---|
| §3·§10 "경쟁 레시피 중 하나가 컴파일 불가" | **둘 다 `supported`** 다. 충돌은 컴파일 가능 여부가 아니라 **모집단·NULL 정책이 다른 두 SQL** 이라는 뜻이다(aggregate 쪽은 active-member 조인 + `IS NOT NULL` 이 붙는다). 어느 쪽을 남길지는 capability 로 결정할 수 없는 **정책 결정** | aggregate recipe 직접 컴파일 |
| §9 "마스킹을 넓혀도 `literal_bindings` 백스톱이 막는다" | **재현되지 않았다.** 식의 evidence 가 duration 구간을 덮으면 전 검증을 통과한다. 백스톱은 evidence 가 그 구간을 **덮지 않을 때만** 뜬다 → 역방향 안전은 오직 §8 조건 4(synonym 등장)·5(국소 인접)에만 걸려 있다 | Q2 환각식 evidence 4종 주입 |
| §10 "컨테이너 env 에 `AUDIENCE_DEFAULT_PERIOD=3 day` 만 있다" | env 에 **없다**. `configured_default_period() = None`(정책 꺼짐) | 컨테이너 env 덤프 |
| §4 검증기가 누락을 잡는다(암묵) | `audience_validators.py:181` 은 `expected > count` **단방향**이라 **과도 청구는 절대 검출되지 않는다**. 진짜 창까지 마스킹해도 `issues=[]` → 회귀 테스트를 `issues == []` 로 쓰면 아무것도 고정하지 못한다 | 혼합문 정답식 + 과도 마스킹 |
| §5 `test_...snapshot_row` 범위 `:231-265` | 실제 `:231-267` | 파일 확인 |
| §3 "벽 A 는 14개 metric" | 맞다. 다만 그 14개는 member_scalar 9 + **field 3 + transition 2** 이고, 뒤의 5개는 스냅샷 소스라 월 고정 문제가 따로 있다(위 "새로 연 표면") | kind 분포 실측 29종 |

### 남은 작업 (착수 순서)

1. **방출 실패 3종 봉합** — 위 1·2 는 애플리케이션이 소유한 **결정론 보정**으로 닫을 수 있다
   (선례: `canonical_audience_claims.apply_window_kinds`, `rolling_absence_claims.normalize_rolling_absence_evidence`
   — 모델 산출물을 고치고 무엇을 고쳤는지 `normalizations` 에 기록하는 방식).
   단 규칙 20 을 지켜 **선언(`value_type`)에 근거한 명시적 보정**이어야 하고 모호하면 fail-close 여야 한다.
2. **라이브 A/B 재측정** — 반드시 **교차 배치(A→B→A→B)**, 팔당 최소 12회. 연속 배치 비교는 드리프트에 무너진다.
3. **86종 corpus 수정 전/후 비교** — 미실행. 성공→실패로 뒤집힌 프롬프트가 이 작업 최대 리스크다.
4. **스냅샷 5종의 월 고정** — 카탈로그 또는 컴파일러 쪽 결정이 필요하다.

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

### 무엇이 "범용 처리"인가

여기서 말하는 범용 처리는 **문장을 보고 예외를 두는 것이 아니라, 리터럴의 의미를 마지막에
확정하는 것**이다. 처리 순서:

```text
원문의 숫자·단위 리터럴 추출
→ 아직 날짜 조건으로 확정하지 않음
→ 지표 표면어, 비교 연산자, 레지스트리 단위, 최종 expression 구조를 대응
→ 조건을 모두 만족한 처리기가 해당 원문 span 을 소비했다고 청구
→ 청구되지 않은 시간 표현만 TemporalSpanValidator 가 검사
```

- **`30일` 자체에는 스칼라 또는 시간 창이라는 고정 의미가 없다.** 바로 위 원장 실측이 그 증거다 —
  `구매주기가 30일 이하인 회원` 의 `30일` 과 `최근 30일 구매한 회원` 의 `30일` 은 원자가 완전히 동일하다.
- **최종 의미는 원문의 지표 표면어와 연산자, 레지스트리 선언, 생성된 expression 구조의 일치로
  결정해야 한다.**

예시:

```text
구매주기 + 30일 + 이하
→ member_scalar_buy_cycle <= 30
→ 첫 번째 30일은 스칼라 임계값

최근 + 30일 + 구매
→ rolling 30 day purchase window
→ 30일은 시간 창
```

혼합문:

```text
구매주기가 30일 이하이고 최근 30일 이내 구매한 회원
```

이 문장에서는 **첫 번째 `30일` 만 member scalar 처리기가 소비하고, 두 번째 `30일` 은 시간 창으로
남아야 한다.** (§7 계약 2 의 실측: 마스킹 없음 2 → 스칼라 구간만 마스킹 1.)

### 추천: `member_scalar_metric_claims` 가 소유하는 **식 역산 스팬 청구**

이것이 이 조사가 도달한 **설계 결정**이다(구현은 미착수). issue 를 받지 않는 공개 함수 하나:

```python
consumed_scalar_threshold_spans(
    query, expression, literal_bindings, registry, catalog
) -> tuple[tuple[int, int], ...]
```

**이 함수는 `구매주기` 전용 함수가 아니다.** `kind == "member_scalar"` 이고 `threshold_unit` 이
선언된 **모든** metric 에 동일하게 적용되는 공통 엔진이다. 지표별 차이는 코드의
`if metric == "buy_cycle"` 분기가 아니라 **레지스트리 데이터**로만 표현한다:

- metric id
- kind
- synonyms
- threshold unit
- value field 또는 expression source

두 경로가 같은 함수를 써야 한다: **모델 생성 경로(LLM 이 expression 을 낸 경우)와 애플리케이션 합성
경로(`_member_scalar_synthesis`)가 동일한 span 청구 함수를 호출**해야 지식이 두 벌로 갈라지지 않는다.

**span 은 문장 전체나 절 전체가 아니라 정확한 리터럴 좌표 단위로만 청구한다.** 절 단위 면제는 같은
문장의 진짜 창까지 지운다(§7 계약 2 실측: 술어 전체 `(0,12)` 마스킹 → 진짜 창까지 1로 줄어듦).

성립 조건 — **전부 필요. 하나라도 어긋나면 빈 튜플(= fail-close)**:

1. 최종 expression 에 `Exists(Filter(Source(member_scalar_X), Comparison(FieldRef(X.value), op, Literal(v))))` 원자가 존재한다
2. `catalog.metric(X).kind == "member_scalar"` 이다
3. 레지스트리가 그 지표의 `threshold_unit` 을 선언한다
4. 원문에 그 지표의 `synonyms` 가 등장한다
5. metric 표면어 → threshold binding → operator binding 이 그 순서로 **국소적으로 인접**하고 사이에
   조사·공백만 있다(`member_scalar_metric_claims.py:203-207` 의 국소 규칙)
6. binding 의 (값, 단위) == (`Literal.value`, 선언된 `threshold_unit`)
7. operator binding 의 `normalized` == expression 의 `Comparison.operator`

**값과 단위만 같다는 이유로(조건 6만으로) span 을 청구해서는 안 된다.** 조건 1·2·4·5·7 이 빠지면
아래 "왜 이 안인가" 의 실측 반례가 그대로 통과한다.

금지 안티패턴 — 문장별 예외처리:

```python
if "구매주기" in query:
    ignore_duration()
```

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

#### 벽 A — metric recipe 를 컴파일 가능한 모양으로 (구현 체크리스트)

1. `audience_runtime.py:776-777` 의 recipe 생성에서 **`member_scalar` kind 를 별도 분기로 처리**한다.
   지금은 kind 가 aggregate/existence 도 아니고 function 도 없으면 무조건 else 로 떨어진다.
2. `member_scalar` kind 에 대해 **bare field recipe 를 생성하지 않는다**
   (`{"type": "field", "name": "member_scalar_X.value"}` 금지).
3. compiler 가 실제로 지원하는 다음 구조를 안내하도록 한다:

   ```text
   Exists(
     Filter(
       Source(member_scalar_X),
       Comparison(FieldRef(member_scalar_X.value), operator, Literal(value))
     )
   )
   ```

4. **같은 표면어에 걸린 aggregate recipe 와 member scalar recipe 의 충돌을 제거한다.**
   지금 `구매주기` 는 179행 `member_metric_buy_cycle` 평균 aggregate 와 191행 `member_scalar_buy_cycle`
   양쪽으로 안내되고 있고, attempt 1·3 과 attempt 2 가 각각 여기에 대응한다. 어느 쪽을 남길지는
   **정책 결정**이며, 남겨두면 모델이 계속 둘 사이를 오간다.
5. **recipe 를 고친 뒤 실제 LLM 이 올바른 `Exists` 구조를 생성하는지는 별도로 라이브 측정해야 한다.**
   이번 조사에서는 LLM 재실행을 하지 않아 미측정이다(§10 미확인).
6. **프롬프트 문자열만 바꾸고 compiler capability 검증을 생략하지 않는다.** 수정한 recipe 각각을
   `event_compiler` 로 실제 컴파일해 `supported` 인지 확인한다(아래 recipe capability 래칫 테스트).

#### 벽 B — 스팬 청구를 생산자와 무관하게 (구현 체크리스트)

1. `member_scalar_metric_claims` 에 공개 함수 `consumed_scalar_threshold_spans(...)` 를 추가한다
   (성립 조건 7개는 §8).
2. 기존 `_member_scalar_synthesis` 도 **가능한 경우 같은 함수를 재사용**한다 — 지식이 두 벌이 되지 않게.
3. `query_structurer/audience_execution.py` 에서 **`synthesis` 의 존재 여부와 관계없이** 호출한다.
4. 기존 synthesis 가 제공하는 spans 와 새로 역산한 spans 를 **안전하게 병합**한다.
5. **중복 span 을 제거**한다.
6. **정확한 리터럴 span 만** `TemporalSpanValidator` 의 마스킹 입력으로 전달한다(절·문장 단위 금지).
7. 대응을 증명하지 못하면 **마스킹하지 않고 fail-close** 한다(빈 튜플).

현재 구조 — 이렇게 되지 않도록 한다:

```text
synthesis가 있을 때만 scalar_literal_spans 존재
LLM이 expression을 생성하면 span 정보 소실
```

수정 후 구조:

```text
expression의 생산자가 LLM인지 합성기인지와 무관하게
최종 expression과 원문을 기준으로 scalar threshold span을 재계산
```

### 병합 필수 테스트

지금 이 판정에는 테스트가 하나도 없다(§7 계약 3). 아래는 **병합 전 필수 완료 조건**이다.

#### 라이브 경로 재현

입력 형태:

```text
expression != None
issues == []
```

목적:

- 실제 LLM 성공 응답과 같은 모양에서도 자동 보정과 span 청구가 정상 작동하는지 확인한다.
- **기존 `expression=None + issue` 테스트만으로 완료 처리하지 않는다**
  (현행 `test_retired_axes_fail_close.py:231` 은 이 모양을 건드리지 않는다 — §5 계측 A/B/C).

#### 기본 성공 사례

```text
구매주기가 30일 이하인 회원
```

기대:

- `member_scalar_buy_cycle <= 30` 으로 컴파일된다.
- 불필요한 TimeFilter 가 생성되지 않는다.
- SQL 생성에 성공한다(§2 의 SQL 모양).

#### 반대 방향 안전 테스트

```text
최근 30일 구매한 회원
```

잘못된 expression:

```text
member_scalar_buy_cycle <= 30
```

기대:

- 원문에 `구매주기` synonym 이 없으므로 **scalar span 을 청구하지 않는다**(§8 조건 4).
- 의미 변조가 검증에서 차단된다. 목적: 마스킹이 안전망을 무력화해 뜻이 `MS.BUY_CYCLE<=30` 으로
  바뀌는 실측 반례(§8 "왜 이 안인가")를 회귀로 고정한다.

#### 혼합문 보존 테스트

```text
구매주기가 30일 이하이고 최근 30일 이내 구매한 회원
```

기대:

- 첫 번째 `30일` 만 scalar span 으로 소비한다.
- 두 번째 실제 시간 창은 남는다.
- **마스킹 후 시간 창 개수는 1이다.**

#### 기존 fail-close 유지

```text
구매주기가 30일 이하이고 여성인 회원
```

기대:

- 기존 `tests/test_revived_axes_event_ir_only.py:205`
  `test_member_scalar_synthesis_refuses_an_ambiguous_sentence` 계약을 **무심코 완화하지 않는다**
  (규칙 47).
- 공유 함수 `_whole_phrase_matches` 를 **문장 전역에서 느슨하게 바꾸지 않는다** — 국소 인접 규칙은
  새 함수에만 둔다.

#### recipe capability 래칫

- §3 에 나열된 **14개 metric** 의 recipe 가 모두 compiler 에서 `supported` 인지 검사한다
  (`member_scalar_*` 9개 + `member_grade`, `member_grade_transition`, `member_newproduct_favor`,
  `member_worth_grade`, `member_worth_grade_transition`).
- 목적: 벽 A 가 `구매주기` 한 지표만 고쳐지고 나머지 13개가 그대로 남는 것을 막는다.

#### 전체 회귀 검사

각각을 **구분해서 기록**한다(하나로 뭉뚱그리지 않는다):

- 관련 단위 테스트(`tests/test_retired_axes_fail_close.py`, `tests/test_revived_axes_event_ir_only.py`,
  `tests/test_audience_validators.py`) 결과
- 전체 pytest 결과 — 이번 조사에서는 미실행이다
- `live_prompts.json` 86개 corpus 재실행 결과와 **수정 전 결과와의 비교**
  (`artifacts/measure/run_before.json` / `run_after.json`)
- 수정 후 **라이브 API 재현**(단순 단위 테스트가 아니라 실제 요청 경로)
- 필요한 경우 **컨테이너 재시작**(`/app` bind mount 라도 프로세스는 코드를 다시 읽어야 한다)

### 미확인으로 남은 것

아래는 **이번 조사에서 확인하지 못한 것**이며, 구현 완료 후 별도로 검증해야 한다. 하나도 지우지 말 것.
(2026-08-06 구현 뒤 상태를 각 항목에 `→` 로 덧붙였다. 원 서술은 지우지 않는다.)

- **§6 회귀 경위는 적대적 검증을 못 거쳤다**(에이전트 중단). 커밋 귀속을 그대로 인용하기 전에 재확인할 것.
  → **여전히 미확인.** 구현 과정에서도 검증하지 않았다.
- recipe 문자열을 고친 뒤 **모델이 실제로 `Exists` 를 내는지** — LLM 재실행을 안 해 미측정.
  → **측정했으나 증명 실패**(3/14, 배치별 0/5↔3/5). §구현 기록 참조. 교차 배치로 재측정해야 한다.
- `SEMANTIC_VALIDATION_V2` / `SEMANTIC_AST_GATE` 가 켜진 라이브 API 전 구간 미검증
  (컨테이너 env 에는 `AUDIENCE_DEFAULT_PERIOD=3 day` 만 있다).
  → **여전히 미검증.** 다만 괄호 안 서술은 **틀렸다** — 그 env 는 존재하지 않고 `configured_default_period()` 는 `None` 이다.
- pytest **전체** 스위트 미실행. 확인한 것은 `tests/test_retired_axes_fail_close.py` 25 passed 뿐이다.
  → **실행 완료**: 5 failed / 3301 passed / 29 skipped. 실패 5건은 HEAD 사본 트리에서도 동일한 기존 실패.
- (a) 상대 월/년 계수를 켤지 여부는 **정책 결정**이다. 켜면 `구매 활동 개월 수` 가 즉시 같은 함정에 들어가고,
  안 켜면 그 부류에서 안전망이 계속 죽어 있다. 어느 쪽이든 (a)(b)를 함께 처리하지 않으면
  **한쪽 결함이 다른 쪽 결함을 가린다.**
- (b) `두주`·`한주` compact matching 유령 창(§7 (b))은 **이번 수정 범위 밖으로 남는다.** 코퍼스에서는
  id 53 하나이고 지금은 앞단(`semantic_registry_gap`)에 가려 드러나지 않는다. 별도로 판단할 것.

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

---

## 완료 판정 기준

아래를 **모두** 만족해야 수정 완료로 판단한다. 하나라도 비면 이 결함은 다른 문장·다른 지표에서
같은 모양으로 다시 나온다.

2026-08-06 구현분 판정 — **10개 중 8개 충족, 2개 미충족. 따라서 아직 완료가 아니다.**

| # | 기준 | 판정 | 근거 |
|---|---|---|---|
| 1 | **벽 A 와 벽 B 가 모두 수정됐다.** 하나만 고친 상태는 완료가 아니다 | 충족 | §구현 기록 변경표 3파일 |
| 2 | `구매주기가 30일 이하인 회원` 이 올바른 SQL 로 컴파일된다 | 충족(애플리케이션 계층) | 라이브 모양 `issues=[]`, SQL 에 `MS.BUY_CYCLE <= 30` + 최신월 고정. **단 라이브 LLM 경로는 #10 참조** |
| 3 | **사용자가 말하지 않은 `최근 구매` TimeFilter 가 추가되지 않는다** | 충족 | E2E 테스트가 구매일자 술어 부재를 고정 |
| 4 | `최근 30일 구매한 회원` 이 구매주기 조건으로 오해되지 않는다 | 충족 | 청구 `()`(조건 4 근거), 단위 테스트 고정 |
| 5 | 혼합문에서 **실제 시간 창이 삭제되지 않는다** | 충족 | 청구 `((6,9),(10,12))`, `(18,21)` 미청구, 마스킹 후 창 1 |
| 6 | 기존 혼합문 fail-close 테스트가 유지된다 | 충족 | `test_member_scalar_synthesis_refuses_an_ambiguous_sentence` 통과, `_whole_phrase_matches` 미변경 |
| 7 | **14개 metric recipe capability 테스트가 통과한다** | 충족 | 14/14 supported, 신규 래칫 112건 |
| 8 | 관련 단위 테스트와 **전체 테스트** 결과가 기록된다 | 충족 | 288 passed(영향 7파일) / 전체 5 failed·3301 passed, 실패 5건은 HEAD 에서도 동일 |
| 9 | **전체 corpus 재실행 결과가 수정 전 결과와 비교되어 기록된다** | **미충족** | 미실행 |
| 10 | **라이브 LLM 이 수정된 recipe 를 따라 실제로 `Exists` 를 생성하는지 측정 결과가 남아 있다** | **미충족** | 측정은 했으나 3/14, 같은 코드가 배치별 0/5↔3/5 로 뒤집혀 **개선 증명 실패** |

기준 2 에 대한 주석: 애플리케이션 계층은 열렸다(HEAD `validation_mismatch[period]` → `[]`). 그러나
**라이브에서 이 문장이 SQL 로 나가는 비율은 아직 낮고**, 그 원인은 벽 A·B 가 아니라 §구현 기록의
방출 실패 3종이다. 그것까지 닫아야 이 프롬프트가 실제로 동작한다고 말할 수 있다.
