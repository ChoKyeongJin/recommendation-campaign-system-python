# 기간 대 기간 "변화 크기" 부류 — 원인 분석과 범용 수정 플랜

작성 2026-08-08. 계기: `2026년 2월과 3월의 구매금액차이가 10% 이상 증가한 고객 리스트` 가
UI 7단계 스텝퍼의 1/7 `condition_recognition` 에서 막힘.

> **구현 완료 2026-08-08.** 아래 플랜은 그대로 실행됐고, 최종 설계는 §1의 원인 분석과
> 사용자가 확정한 10개 불변식(span overlap ≠ semantic support / 요구는 사라지지 않는다 /
> SATISFIED 는 영수증으로만 / generic receipt 수렴 …)을 따른다. 구현 요약은
> `tests/test_semantic_conservation.py` 와 `tests/test_relative_calendar_token_boundary.py`
> 가 계약으로 고정한다. 아래 Phase 번호와 실제 구현의 대응은 다음과 같다.
>
> | 플랜 | 구현 |
> |---|---|
> | Phase 0-A | `semantic_requirements.CHANGE_MAGNITUDE_KIND` + `parse_change_magnitudes` (숫자형·낱말형, 소유 대장 가드) |
> | Phase 0-B | `lexicon_patterns.starts_new_token` + `calendar_window._resolve_overlapping_candidates` |
> | Phase 1 | `LoweringPlan.satisfied_requirement_ids` · `unsettled_requirements` · `plan_satisfying_span` (겹침은 후보 탐색 전용) |
> | Phase 2·3 | `ComparisonObligation.threshold` + `_lower_change_threshold` (유리수 교차곱 / 뺄셈 / `right > 0` 가드) |
> | Phase 4 | `canonical_audience_claims.settle_source_requirements` — 종류별 예외 목록을 증명자 레지스트리 하나로 수렴 |
> | Phase 5 | `render_change_comparison_recipe` + `_change_comparison_synthesis` (마지막에 오픈) |
> | R4(추가) | `audience_runtime.source_pins_its_time_column` — 스냅샷 고정 소스는 계획하지 않는다 |

---

## 0. 직접 재현한 사실 (이 문서 작성 시 실행)

**(1) 실패 재현** — `POST /target-sql {query_parser:"auto"}`, HTTP 200, SQL 없음.

```json
"failure_stage": {"code": "condition_recognition", "order": 1, "total": 7,
                  "reason": "semantic_emission_failure"},
"semantic_ir": {"status": "needs_clarification", "failure_kind": "system_failure",
                "missing_fields": ["audience.requirement"],
                "message": "요청한 조건은 지원되는 의미이지만 실행 표현으로 확정되지 않았습니다."}
"debug.unresolved_source_conditions": [{
   "path": "source_coverage.percent_change_between_periods",
   "label": "2026년 2월과 3월의 구매금액차이가 10% 이상",
   "code": "unsupported_semantics", "source": "canonical_audience_contract"}]
```

**(2) 임계값이 캡처에서 사라진다** — `lowering_planner.detect_comparison_obligations(q, today=2026-08-08)`:

의무 2개(`purchase_amount`, `total_buy_amt`), 둘 다 `operator='>'`, `source_span=(0,31)` =
`'2026년 2월과 3월의 구매금액차이가 10% 이상 증가'`. **구조에 `10%` 를 실을 필드가 없다.**
`ComparisonObligation.__dataclass_fields__` = `left/operator/right/source_text/source_span/kind`.

**(3) 임계 없는 같은 문형은 정상 출고된다** — `2026년 2월과 3월의 구매금액이 증가한 회원`:

```sql
(SELECT ISNULL(SUM(EO.PAYMENT_AMT),0) FROM CRM_SL_ORDERHEADERMALL EO
   WHERE EO.MEMBER_NO = B.MEMBER_NO AND EO.ORDER_DATE >= '20260301' AND EO.ORDER_DATE < '20260401')
>
(SELECT ISNULL(SUM(EO.PAYMENT_AMT),0) FROM CRM_SL_ORDERHEADERMALL EO
   WHERE EO.MEMBER_NO = B.MEMBER_NO AND EO.ORDER_DATE >= '20260201' AND EO.ORDER_DATE < '20260301')
```

즉 **두 창 집계 비교 자체는 이미 컴파일된다.** 빠진 것은 크기뿐이다.

**(4) 임계 표현은 Core IR 변경 없이 지금 컴파일된다** — `event_ir.Comparison` + `event_ir.Arithmetic`
(`ARITHMETIC_OPERATORS = {"+","-","*","/"}`, event_ir.py:418)로 5형태를 만들어 컴파일한 결과,
전부 `validate_evidence` 통과 · JSON round-trip 일치 · `unsupported_capabilities() == ()` ·
정상 T-SQL 출력. `capabilities = {aggregate.scalar, aggregate.derived_expression, scalar.arithmetic}`.

| 형태 | IR | 결과 |
|---|---|---|
| 무임계(현재) | `mar > feb` | OK |
| 비율(float) | `mar >= feb * 1.1` | OK |
| **비율(정수 스케일)** | `mar * 100 >= feb * 110` | **OK — float 없음** |
| 절대 증가액 | `(mar - feb) >= 50000` | OK |
| 감소 비율 | `mar <= feb * 0.8` | OK |

`event_ir.Literal` 은 `Decimal` 을 거부한다(event_ir.py:441, `int|float|str|bool` 만 허용).
정수 스케일 형태가 CLAUDE.md §14(비율에 float 금지)를 지키는 유일한 형태다 → D3.

또한 LLM 노출 스키마(`audience_schema.py:128-137`)에 `arithmetic` 이 **이미 있다.**
따라서 이 부류는 **능력(capability) 결손이 아니라 캡처·정산·배선 결손**이다.

**(5) 별건 확인 — 상대 달력 접두어 부분 매치**(아래 R2):

| 입력 | 파싱 결과 | 기대 |
|---|---|---|
| `지지난달` | span (1,4) `지난달` → 2026년 7월 | 6월 |
| `전전월` | span (1,3) `전월` → 2026년 7월 | 6월 |
| `지지난주` | span (1,4) `지난주` → 7/27~8/2 | 그 전주 |
| `그그제` | span (1,3) `그제` → 8월 6일 | 8월 5일 |

경고 0으로 한 달/한 주 어긋난 SQL 이 나간다.

---

# 원인 분석 + 범용 수정 플랜: 기간 대 기간 변화(변화율·변화량·배수) 부류

## 0. 한 문장 요약

이 부류에서 시스템은 **"방향"만 모델링하고 "크기"는 어느 계층에서도 모델링하지 않는다.** 그런데 크기가 적힌 텍스트의 **소유권은 주장한다.** 그 두 사실의 조합이 확인된 9개 발견 전부의 상류다 — 조용한 의미 약화(최악), 거짓 지원 판정, 종결 불가 재시도가 모두 여기서 갈라져 나온다.

---

## 1. 근본 원인 (심각도 순)

| ID | 근본 원인 | 심각도 | 파생 증상(확인된 발견) |
|---|---|---|---|
| **R1** | 변화의 **크기(임계)** 를 캡처·표현·정산 어느 계층도 모델링하지 않는다 | **S1 조용한 의미 오답** | #1(A), #3(절반), #4, #6 |
| **R2** | 상대 달력 어휘가 **접두 확장을 부분 매치**한다(`지지난달`→`지난달`) | **S1 조용한 의미 오답** | #9 |
| **R3** | 계획 스팬이 **min/max hull** 이라, 모델링하지 않은 텍스트의 소유권을 주장한다 | S2 캡처/판정 실패 | #1, #2 |
| **R4** | 지원 오라클이 "컴파일되면 지원"만 보고 **소스 선언의 시간 고정(스냅샷)** 을 안 본다 | S2 캡처/판정 실패 | #5, #8 |
| **R5** | 리터럴 정산 어휘에서 `percentage` 토큰 생산자가 **랭킹 `limit.percent` 하나뿐**이다 | S2 캡처/판정 실패 | #4 |
| **R6** | 결정론 낮춤 결과에 **생산자가 없다** — 판정에만 쓰고 방출/합성에 안 쓴다 | S2 캡처/판정 실패 | #6, #7 |
| **R7** | 같은 표면어에 카탈로그 지표 2종 매칭 → 의무·계획 중복 | S3 중복/노이즈 | #5·#8의 generality |

### R1 — 크기 미모델링 (최악)

`ComparisonObligation`(lowering_planner.py:73-81)의 필드는 `left/operator/right/source_text/source_span` 뿐이다. `detect_comparison_obligations`(:226)의 표지는 **창 2개 + 집계 지표 + 방향 낱말** 셋이고 비교 임계가 없다. 그래서:

- 숫자형 임계(`10%`·`5만원`·`2배`)는 리터럴 원장(`extract_literal_bindings`)에는 잡히지만 IR 에 소비될 짝이 없어 `validation_mismatch` 로 **차단**된다 → SQL 0건, 종결 불가 재시도.
- **낱말형 임계(`절반으로`·`반으로`)는 리터럴 원장에도 안 잡힌다** → 정산이 잡을 것이 없고, LLM 이 낸 맨 비교(`3월 < 2월`)가 그대로 통과한다. 실측 라이브 8회 중 1회 `status=success` 로 **'조금이라도 줄어든 전원'** SQL 이 사용자에게 나갔다(9단계 의미검증도 `pass`).

즉 R1 의 피해는 두 갈래인데, **더 조용한 쪽이 낱말형**이다. 지원 확대보다 이쪽 차단이 먼저다.

### R2 — 달력 접두 미소비 (조용한 오답)

`_scan_relative_calendar_months`(calendar_window.py:749-786)가 어휘를 `re.escape` 로만 잇고 한글 경계 룩비하인드를 두지 않는다. 같은 파일이 상대 **연도**(:802-804 `(?<![가-힣])`)와 단어형 기간에는 그 가드를 이미 갖고 있다. 결과: `지지난달`→2026년 7월(span 시작 1), `지지난주`→지난주, `전전월`→7월, `그그제`→그제. 경고 0으로 **한 달/한 주 어긋난 SQL 이 출고**된다. 결함은 이 부류 밖(단독 기간 필터)까지 오염시킨다.

### R3 — 소유권과 의미의 분리

`span_sources = [left, right, metric, direction]` 뒤 `min/max`(lowering_planner.py:290-294). 두 창과 방향어 **사이에 낀** `10% 이상`이 hull 에 통째로 삼켜진다. 하류 소비자 5곳은 전부 `spans_overlap` 겹침 판정이라, 1글자만 걸쳐도 "그 자리는 낮춰진다"는 권위 있는 반박이 성립한다. 실패 로그 20건 중 7건은 모델의 근거 스팬이 정확히 `10% 이상`(22,28) 하나였고, 그 회차의 반박 발화 원인이 hull 이었다(스팬을 (0,18)로 좁히면 `rebut=False`).

**단, hull 만 좁혀도 SQL 은 바이트 동일하다**(실측). R3 은 "왜 정직한 미지원 신고가 뒤집히는가"의 원인이지 "왜 임계가 사라지는가"의 원인이 아니다. 두 원인을 분리해 두는 것이 이 플랜의 전제다.

### R4 — 오라클이 소스 선언의 시간 고정을 안 본다

`try_plan`(:374)은 `spec.where`/`spec.joins`/`allowed_operators`/`source_spec.time_field` 만 본다. 소스 선언의 `extra_predicates`(`{alias}.YYYYMM = (SELECT MAX(YYYYMM) …)`)는 조회하지 않는다. 같은 선언을 읽는 `audience_runtime._pins_its_own_time_column`(audience_runtime.py:900)은 이미 이 함정을 계산해 프롬프트에 "(기간 창 불가) … 결과가 조용히 비어 있다"고 광고한다. **앱이 경고하는 것을 판정자만 모른다.**

피해 실측: `평균 구매금액`·`구매주기`·`누적 구매건수` 같은 프롬프트는 **유일한 계획이 pinned 스냅샷**이라, 자기모순 SQL(현 배포에서 항상 공집합, 창이 스냅샷 달을 덮으면 69,132/69,609)이 "지원됨"의 증거가 되어 옳은 미지원 신고를 뒤집는다.

### R5 — 정산 어휘가 랭킹에 묶여 있다

`_semantic_tokens`(canonical_audience_claims.py:59-60)는 `percentage` 토큰을 `node_type=="limit"` 의 `percent` 에서만 만든다. `_binding_target`(:80-97)은 `kind=="percentage"` 바인딩을 `("percentage", 10)` 으로 요구하고, 매칭은 종류·값 정확 일치(:265)다. 그래서 **정확한 변화율 표현 3형태(비율형·곱셈형·×100형) 전부가 컴파일 성공인데도** `validation_mismatch literal_bindings[2] '10%'` 로 반려된다. 카탈로그 전수에 `unit=percent` 인 field/metric 은 0개 — 퍼센트를 '상위 N%' 외의 뜻으로 쓰는 모든 문형(변화율·비중·달성률)이 같은 벽이다.

### R6 — 생산자 부재

`LoweringPlan.expression`/`.sql` 의 프로덕션 소비처는 `query_structurer/prompt.py:283` 하나이고, 그 앞 :265 가 `kind == MEMBER_STATE_HISTORY` 로 걸러 `aggregate_comparison` 을 배제한다. `_synthesis_for_issue`(audience_execution.py:612)의 합성기 5종(consent_cardinality/campaign_average/rolling_absence/member_scalar/temporal)에도 이 축이 없다. 결정론이 옳게 계산한 표현을 두고 **LLM 에게 다시 그리게 하고**, 반전 여부는 또 다른 LLM(9단계, `noncredible_inverted` 필터 있음)에 맡긴다. 같은 프롬프트 3회에 극성 반전·성공·방출실패가 나온 구조적 이유다.

### R7 — 지표 중복 (노이즈)

`구매금액` 이 `purchase_amount` 와 `total_buy_amt` 양쪽 별칭에 정확 일치해 의무·계획이 항상 2개다. 하류는 `next(첫 겹침)` 존재 판정이라 **귀결에 영향이 없다**(승자 뒤집기 실험에서 출력 바이트 동일). 고칠 대상이 아니라 **불변식으로 고정할 대상**이다.

---

## 2. 명시적으로 거절하는 수정안

1. **"10% 이상 증가" 전용 정규식/슬롯/빌더 추가** — 거절. 같은 결손이 `5만원 이상 증가`·`2배 이상`·`20% 이상 감소`·`절반으로`에서 동일하게 재현되므로, 표면 하나를 잡으면 나머지 넷이 조용히 남는다. 구조 결손을 표면 패치로 덮는 것은 CLAUDE.md 2·6 위반.
2. **Core IR 에 `percent_change`/`change_threshold` 노드 신설** — 거절. `Comparison`+`Arithmetic`(+`NullIf`)의 조합으로 이미 컴파일된다(실측: `unsupported_capabilities()==()`, 정상 T-SQL). 조합으로 표현되는 것에 타입을 주면 같은 뜻이 두 모양을 갖는다(event_ir.py:513 이 스스로 선언한 규칙).
3. **legacy `target_user.metric_trend` 슬롯 부활** — 거절. `audience_admission.declares_audience` 게이트(graph_rag.py:12085-12090)가 legacy 레인을 2026-08-07 폐쇄했고, 슬롯을 채우는 순간 그 플랜이 자기 자신을 막는다. 되살리려면 canonical Event IR 단일 경로 방향과 정면 충돌하는 아키텍처 결정이 필요하다.
4. **prompt.py:265 의 kind 필터만 푸는 안** — 거절. 임계 캡처 전에 풀면 `10% 이상`이 빠진 형상을 "verbatim 으로 내라"고 **프롬프트가 직접 가르치게** 되고, 같은 스팬에 모순되는 verbatim 지시 2개(purchase_amount/total_buy_amt)가 동시에 나간다. 지금은 clarification 으로 끝나는 요청이 조용한 오답으로 끝난다.
5. **hull 을 좁히는 것만으로 끝내는 안** — 거절. 스팬을 (0,18)로 좁혀도 SQL 은 바이트 동일이고 실패 20건 중 13건은 그대로 반박된다. 소유권 정직화는 필요조건이지 충분조건이 아니다.
6. **의미검증기(9단계)에 "퍼센트 누락" 규칙 추가** — 거절. LLM 판정이라 확률적이다(실측 8회 중 1회가 `pass`+`faithful=true` 로 통과). 결정론 계층에서 막아야 한다.

---

## 3. 재사용할 기존 자산 (새 계층 신설 없음)

| 필요 | 기존 자산 | 위치 |
|---|---|---|
| 임계 값·단위·스팬·연산자 추출 | `extract_literal_bindings` (percentage/money/number_with_unit/comparison_operator, Decimal 정확) | query_structurer/semantic_ir.py:332-345 |
| 방향·비교 표지 어휘 | `lexicon_patterns` + `parser_lexicon.json`(trend_increase/decrease, comparative_marker) | docs/data/runtime/language/parser_lexicon.json:166-190 |
| **미소비 의미의 차단형 원장 + 영수증 면제** | `capture_source_semantic_obligations` / `semantic_obligation_issues` / temporal 영수증 면제 | semantic_requirements.py:1412, canonical_audience_claims.py:1304·1319-1322 |
| fail-close 출고 차단 | `unresolved_source_conditions` → `_unresolved_source_blocking_sql_result` | graph_rag.py:9867-9873 |
| 결정론 합성 레지스트리 | `_synthesis_for_issue` (5종 선례) | query_structurer/audience_execution.py:612 |
| 낮춤 형상 프롬프트 주입 | `render_temporal_state_recipe` | query_structurer/prompt.py:239-290 |
| 스냅샷 소스 판정 | `_pins_its_own_time_column` | audience_runtime.py:900 |
| Decimal 직렬화/SQL 렌더 | `exact_decimal` / `decimal_json_value` / `decimal_sql_text` | semantic_normalizers.py:223-274 |
| 한글 경계 가드 선례 | `_STANDALONE_YEAR_RE` 의 `(?<![가-힣])` | calendar_window.py:802-804 |

**설계 요지: 시간·이력 축(`member_state_history`)이 이미 갖고 있는 모양 — 의무 원장 kind → 차단 → 컴파일 영수증 → 면제 → 낮춤 probe → 프롬프트 레시피 → 합성 — 을 "변화 크기" 축에 그대로 한 벌 더 적용한다.** 새 클래스·새 계층 0개, 새 dependency 0개.

---

## Phase 0 — 안전: 조용한 오답을 먼저 막는다 (지원 확대 없음)

두 항목은 서로 독립적으로 배포 가능하다.

### 0-A. 변화 크기 의무를 원장에 캡처하고, 방면되지 않으면 fail-close

**무엇을** — `semantic_requirements.py`
- `CHANGE_MAGNITUDE_KIND = "change_magnitude"` 상수 추가(:1375 옆, `TEMPORAL_QUALIFIER_KIND` 와 같은 자리).
- `_change_magnitude_obligations(query)` 추가(`_member_state_history_obligations`:1390 와 같은 모양) → `capture_source_semantic_obligations`:1425-1431 목록에 한 줄 등록.
- 캡처 규칙(전부 기존 자산 조합, 새 어휘 파일 없음):
  1. `extract_literal_bindings` 의 `percentage`/`money`/`number_with_unit` 바인딩 **또는** 어휘가 소유한 배수·분수 낱말(`배`·`절반`·`반으로`)
  2. 같은 절 안에서 `trend_increase`/`trend_decrease` 방향 낱말이 뒤따를 것(국소 인접, 절 경계는 기존 `_clause_bounds_at`:1378 재사용)
  3. **소유권 가드**: 그 리터럴 스팬을 이미 다른 청구자(랭킹 `상위/하위 N%`, 기존 의무 스팬)가 덮으면 청구하지 않는다.
- `value = {"magnitude_text": …, "direction": …}` 만 기록한다. **정량 해석은 여기서 하지 않는다**(Phase 2).

**왜 그 자리인가** — 이 원장은 "원문에서 캡처한 손실성 의미 연산자는 **명시적 컴파일러 영수증으로만** 방면된다"는 계약을 이미 갖고 있고(semantic_requirements.py:1420-1424 독스트링), 그 미방면은 `semantic_obligation_issues`(canonical_audience_claims.py:1304) → `canonical_claim_issues` → `unresolved_source_conditions` → 출고 차단으로 **이미 배선돼 있다**. 새 게이트를 만들지 않고 `절반` 같은 낱말형까지 한 번에 덮는 유일한 자리다. 부수 효과로 프롬프트의 `[Application-owned Semantic Obligations]` 섹션에 이 요구가 실려, 모델이 "크기가 요구되었다"는 사실을 보게 된다.

**테스트** — `tests/test_aggregate_comparison_lowering.py` 에 §6 신설:
```python
MAGNITUDE_CORPUS = (
    ("…구매금액차이가 10% 이상 증가한 고객 리스트", "10% 이상"),
    ("…구매금액이 5만원 이상 증가한 회원",          "5만원 이상"),
    ("…구매금액이 2배 이상 증가한 회원",            "2배 이상"),
    ("…구매금액이 20% 이상 감소한 회원",            "20% 이상"),
    ("…구매금액이 절반으로 줄어든 고객",            "절반"),   # 낱말형 = 리터럴 0개
)
```
- `test_magnitude_marker_is_captured_as_a_source_obligation` — 5종 모두 `change_magnitude` 의무 1개, 스팬이 크기 표면을 덮는다.
- `test_unmodelled_magnitude_blocks_sql_end_to_end` — **핵심 안전 테스트.** 모델이 임계 없는 `Comparison('<', sum(3월), sum(2월))` 를 방출했다고 가정하고 `canonical_claim_issues` 가 비지 않음 + `attach_campaign_query_plan_v4_identity` 종단에서 `event_expression` 이 제거됨을 고정한다(`절반` 케이스 포함 — 지금 유일하게 새는 구멍).
- **오탐 0 대조군**: `상위 10% 회원 중 구매금액이 증가한 …`(랭킹 소유), `10만원 이상 구매한 회원`(방향어 없음), 기존 `PARAPHRASES` 5종(크기 없음) → 의무 0개, 기존 귀결 불변.

**되돌리기/호환성** — 되돌리기는 `capture_source_semantic_obligations` 의 등록 한 줄 삭제. 캡처 실패는 기존 관례대로 빈 목록(fail-safe). **호환성 영향은 의도된 것**: 지금까지 크기를 잃고 나가던 SQL 이 명시적 clarification 으로 바뀐다. "SQL 생성 > 거절"이라는 프로젝트 목표와 충돌하지만 **의미 약화가 더 나쁘고**, Phase 2-4 가 이 거절 창을 다시 생성으로 닫는다.

### 0-B. 상대 달력 어휘 한글 경계 가드

**무엇을** — `calendar_window.py:768-770`(월 스캐너), 같은 결함이 있는 주 스캐너·`전전월`·`그그제` 경로. 어휘 교대 패턴을 `rf"(?<![가-힣])(?:{alternation})"` 로 감싼다. 같은 파일 :802-804 가 이미 쓰는 관용구를 그대로 쓴다.

**왜 그 자리인가** — 상대 연도에만 있고 월/주/일에 빠진 가드다. 어휘 파일이 아니라 스캐너의 문제이므로 어휘 추가로 우회하지 않는다.

**테스트** — 신규 `tests/test_relative_calendar_prefix_boundary.py` 또는 기존 `tests/test_standalone_relative_year_window.py` 확장:
- `지지난달`/`지지난주`/`전전월`/`그그제` → 창 0개(정직한 결핍), 좌표 1에서 시작하는 창이 **없음**을 고정.
- 회귀: `지난달`/`이번 달`/`전월`/`그제`/`저번 달` 는 그대로 매치.
- 문장 수준: `지지난달 대비 이번달 …증가` → 비교 의무 0개(현재는 7월 vs 8월 SQL 출고).

**되돌리기/호환성** — 정규식 한 조각. 이 어휘를 **지원하고 싶다면** 별도 결정(D9)으로, 어휘 JSON 에 offset −2 항목을 추가하는 선언적 변경이 된다. 안전 가드가 그 논의에 막히지 않도록 분리한다.

---

## Phase 1 — 소유권 정직화: 모델링하지 않은 텍스트를 청구하지 않는다

**무엇을** — `lowering_planner.py`
- `detect_comparison_obligations`:290-294 의 hull 산식은 유지하되, hull 확정 직후 **불변식 검사**를 추가한다: hull 구간 안에 Phase 0-A 가 캡처한 `change_magnitude` 의무 스팬이 있고 그 구조를 이 의무가 담지 못하면 → **의무를 세우지 않는다**(`continue`).
- 이유를 주석으로 못 박는다: 모듈 독스트링(:30-33)이 이미 "없는 지원을 있다고 말하면 옳은 미지원 신고까지 반박해 재시도만 반복하게 된다"고 선언한 그 fail-safe 다.

**왜 그 자리인가** — hull 을 좁히는 것(스팬 성형)으로는 20건 중 13건이 그대로 반박된다. 청구 자체를 포기하는 것만이 "계획의 존재 = 지원의 증명"이라는 이 모듈의 계약을 참으로 되돌린다. 스팬 규칙을 긍정형으로 서술하면: **스팬은 구조가 실제로 모델한 표지들의 hull 이어야 한다.**

**테스트** — §6 계속:
- `test_no_plan_claims_an_unmodelled_magnitude_span` — 5종 전부 `plans_for_query == ()`, `_lowering_plan_conflicts(...) == []`.
- `test_the_outcome_is_honest_unsupported_not_emission_failure` — 종단 귀결이 `semantic_emission_failure`("지원되는 의미인데 표현이 안 섰다")가 **아니어야** 한다. 잘못된 사유가 운영에서 없는 방출 실패를 쫓게 만든 것이 관측된 피해다.
- 기존 계약 회귀: 크기 없는 `PARAPHRASES` 5종은 여전히 `can_plan == True`, `capabilities == {"aggregate.scalar"}`, 반개구간 3개 유지(기존 테스트 그대로 통과해야 함 — 약화 금지).

**되돌리기/호환성** — 한 조건문. Phase 2 가 들어오면 이 조건은 자연히 거짓이 되어(의무가 임계를 담으므로) 계획이 다시 선다 — 삭제할 필요가 없는 항구 불변식이다.

---

## Phase 2 — 크기를 구조에 싣는다 (캡처 + 낮춤)

**무엇을**
1. `lowering_planner.py:73-81` — `ComparisonObligation` 에 **선택 필드 하나** 추가:
   ```python
   threshold: ChangeThreshold | None = None   # frozen dataclass: kind('ratio'|'absolute'), value: Decimal, unit: str, operator: str
   ```
   Core IR(`event_ir`) 이 아니라 **판정자 내부 의무 모델**이므로 CLAUDE.md 3항의 "Core IR 최상위 필드 추가 금지"에 걸리지 않는다. 값은 `extract_literal_bindings` 가 이미 만든 정규화 결과(`{value, unit}` + `comparison_operator`)를 **그대로 받아** 담는다 — 새 파서를 쓰지 않는다.
2. `detect_comparison_obligations`:290 부근 — Phase 0-A 의무 스팬과 리터럴 바인딩을 대조해 `threshold` 를 채운다. 채우지 못하면 Phase 1 규칙대로 의무를 세우지 않는다.
3. `try_plan`:428-446 `operand()` 뒤 — 임계가 있으면 비교식을 낮춘다:
   ```python
   # 10% 이상 증가:  left * 100 >= right * 110      (정수 스케일, 나눗셈 없음)
   # 5만원 이상 증가: left      >= right + 50000
   ```
   `Arithmetic` 만 쓰고 `NullIf`·나눗셈을 쓰지 않는다(D3 참조).

**왜 그 자리인가** — 임계가 사라지는 유일한 자리가 캡처다(hull 이 아니다). 그리고 컴파일러는 이미 이 형태를 낸다(실측 `caps={aggregate.scalar, aggregate.derived_expression, scalar.arithmetic}`, `unsupported=()`). 지표·창 문법이 늘어도 같이 열리도록 `_aggregate_metric_hits`/카탈로그 경로는 손대지 않는다.

**테스트** — §7 신설:
- `test_threshold_is_captured_with_unit_and_operator` — `Decimal("10")`/`percent`/`">="`, `Decimal("50000")`/`KRW`. **float 금지 확인**(`isinstance(value, Decimal)`).
- `test_threshold_survives_into_the_lowered_sql` — SQL 에 스케일 정수가 실리고, 임계 없는 비교와 **SQL 이 달라야** 한다(현재는 바이트 동일).
- `test_direction_and_operator_polarity_matrix` — {증가,감소}×{이상,초과,미만} 6조합 극성 표(기존 4번 불변식의 확장, 반전은 최악).
- `test_relative_and_absolute_thresholds_use_different_shapes` — 비율/절대 형태 분리 고정.
- 경계: `0%`, `100%`, 소수 퍼센트(`10.5%`), 음수/파싱 실패 → 계획 없음(추측 금지).

**되돌리기/호환성** — `threshold=None` 기본값이라 기존 무임계 경로는 바이트 동일. 되돌리기는 낮춤 분기 하나 제거.

---

## Phase 3 — 정산 영수증: 크기가 실린 표현을 통과시키고, 안 실린 것은 계속 막는다

**무엇을** — `canonical_audience_claims.py`
- `change_threshold_compiled_spans(query, expression)` 신설 — `temporal_obligation_compiled_spans` 와 **같은 모양**: 낮춰진 트리를 되읽어 (a) 두 창 집계, (b) 방향 연산자, (c) 임계 산술이 **전부** 있는지 확인한 뒤에만 스팬 영수증을 발급한다.
- `canonical_claim_issues`:1626-1632 의 temporal 면제 블록 옆에 같은 모양으로 한 블록 추가 → 영수증 스팬이 덮는 `percentage` 리터럴 이슈와 Phase 0-A 의 `change_magnitude` 의무 이슈를 함께 면제.
- `_semantic_tokens`/`_binding_target` 은 **건드리지 않는다**(D6 참조).

**왜 그 자리인가** — R5 의 진짜 문제는 "IR 표현력"이 아니라 "정산 어휘의 종류 매핑"이고, 이 저장소에는 그것을 푸는 확립된 관용구가 **이미 셋**(rolling_absence / temporal / consent_cardinality) 있다. 네 번째를 같은 자리에 같은 모양으로 놓는 것이 최소 변경이고, 영수증이 없으면 차단이 유지되므로 **면제가 구멍이 되지 않는다.**

**테스트** — §8:
- `test_faithful_percent_change_expression_passes_the_literal_ledger` — 곱셈형이 `canonical_claim_issues == []`.
- `test_threshold_less_expression_is_still_blocked` — **비대칭 테스트**. 같은 원문·같은 바인딩에서 임계 없는 표현은 여전히 차단(면제가 넓어지지 않았음의 증거).
- `test_receipt_requires_all_three_parts` — 창/방향/임계 중 하나를 빼면 영수증 미발급.

**되돌리기/호환성** — 면제 블록 하나. 되돌리면 Phase 0-A 의 차단으로 되돌아갈 뿐 오답이 나가지 않는다(안전한 방향의 실패).

---

## Phase 4 — 생산자 배선: 결정론이 만든 표현을 실제로 내보낸다

**무엇을**
1. `query_structurer/prompt.py:262-266` — `kind == MEMBER_STATE_HISTORY` 필터를 **kind→렌더러 레지스트리**로 바꾸고 `aggregate_comparison` 렌더러를 등록한다. 함수 독스트링(:249-256)의 계약("낮출 수 없는 요청에는 아무것도 내지 않는다")은 그대로 유지 — Phase 1·2 덕분에 이제 크기를 담은 계획만 실린다.
   - 중복 계획(R7)은 렌더 직전에 `(kind, span, capabilities)` 로 접고, 지표가 갈리면 **아무것도 싣지 않는다**(모순되는 verbatim 지시 2개 방지).
2. `query_structurer/audience_execution.py:612-707` — `_synthesis_for_issue` 에 `_change_comparison_synthesis` 를 추가한다. 조건은 기존 합성기와 동일: 모델 issue 의 근거가 계획 스팬 안에 있고, 계획이 유일하며, 영수증(Phase 3)이 발급될 것. 반환은 `plan.expression`.

**왜 그 자리인가** — 이 축에는 표적 재방출(반박 문구)만 배선돼 있고 결정론 백필 3종 중 나머지 둘이 비어 있다. 순서가 중요하다: **Phase 2 이전에 합성하면 임계 없는 표현을 앱이 스스로 출고**하게 되어 최악의 결말이 된다. 그래서 이 단계가 마지막이다.

**테스트** — §9:
- `test_application_emits_the_expression_when_the_model_fails` — `expression=null` + `unsupported_semantics` 를 넣어도 종단에 `event_expression` 이 있고 그 SQL 에 임계가 실린다.
- `test_recipe_only_teaches_shapes_the_planner_can_lower` — 기존 `tests/test_temporal_claims_wiring.py:575` 계약을 새 kind 로 확장(약화 금지).
- `test_ambiguous_metric_emits_no_recipe` — R7 중복 시 안내 0줄.
- 극성 회귀: 합성 SQL 의 좌우 피연산자가 창 순서와 일치(반전 0).

**되돌리기/호환성** — 레지스트리 등록 두 줄. 되돌리면 Phase 3 상태(차단)로 안전 복귀.

---

## Phase 5 — 오라클 fail-safe: 시간 축이 고정된 소스에는 창을 얹지 않는다

**무엇을** — `lowering_planner.py:400-410`(이미 `spec.where`/`joins` fail-safe 가 있는 바로 그 자리)에 조건 하나 추가: `source_spec` 이 자기 `time_field` 를 고정하면 계획을 세우지 않는다. 판정 술어는 `audience_runtime._pins_its_own_time_column`(audience_runtime.py:900)을 공개 이름으로 재사용한다(선언에서 파생, 테이블/컬럼 이름을 새로 적지 않음).

**왜 그 자리인가** — 같은 선언에서 프롬프트는 "(기간 창 불가)"라고 경고하는데 판정자만 모른다. 두 소비자가 **같은 술어**를 부르게 하는 것이 드리프트를 없애는 유일한 방법이다.

**테스트** — §10:
- `test_pinned_snapshot_metrics_are_not_planned` — `total_buy_amt`/`mean_buy_amt`/`buy_cycle` 의무는 `can_plan == False`.
- `test_the_prompt_warning_and_the_oracle_agree` — 프롬프트가 "(기간 창 불가)"로 경고하는 지표 집합과 계획이 서지 않는 지표 집합이 **같다**(드리프트 가드).
- `test_pinned_only_query_is_not_reported_as_emission_failure` — `2026년 2월과 3월의 평균 구매금액이 증가한 회원` 의 사유가 방출 실패로 오분류되지 않는다.
- 회귀: `purchase_amount`(주문 테이블) 계획은 그대로.

**되돌리기/호환성** — 조건 한 줄. 지원 축소가 아니라 **거짓 지원 선언의 철회**다(그 계획의 SQL 은 애초에 출고되지 않았다).

---

## Phase 6 (선택) — 중복 정산은 고치지 말고 고정한다

`R7` 은 귀결에 영향이 없음이 실증됐다. 코드 변경 대신 **불변식 테스트**만 추가한다: 같은 스팬에 계획이 2개여도 `_lowering_plan_conflicts` 출력이 1건이고, 계획 순서를 뒤집어도 출력이 바이트 동일. 이렇게 두면 Phase 4 의 레시피 배선이 이 중복에 걸려 조용히 모순 지시를 내는 회귀를 테스트가 잡는다.

---

## 4. 임의로 정하지 않고 결정을 요청하는 지점

| ID | 결정 지점 | 선택지 | 권고 |
|---|---|---|---|
| **D1** | 변화율의 **기준선이 0**일 때(2월 매출 0) | (a) `right > 0` 요구해 제외 (b) 증가로 포함 (c) unsupported | **(a)**. 레포에 이미 선언된 선례(graph_rag.py:14823 주석: "0→양수는 증가이지만 '몇 % 증가'인지 정의되지 않는다"). 곱셈형은 `0 >= 0*1.1` 이 참이라 이 술어 없이는 **비구매자 전원이 뽑힌다** — 반드시 함께 간다. |
| **D2** | **2월 무주문(NULL)** vs **2월 0원** | (a) `right > 0` 이 둘 다 제외 (b) `Exists` 로 구별해 무주문만 제외 (c) 둘 다 포함 | **(a)**. D1 과 같은 술어 하나로 끝나고, `ISNULL(SUM,0)` 접기와도 일관된다. 구별이 필요하면 IR 은 이미 `Exists`/`Not`/카운트 대수로 표현 가능하다(표현력 제약 아님, 정책 선택). |
| **D3** | 임계 표현 형태 | (a) 정수 스케일 곱셈형 `L*100 >= R*110` (b) 비율형 `(L-R)/NULLIF(R,0) >= 0.1` (c) Decimal 곱셈형 `L >= R*1.1` | **(a)**. (b)는 정수 지표에서 **조용히 틀린다**(실서버 실측 `(11-10)/NULLIF(10,0)=0` → +10% 회원 탈락). (c)는 `event_ir.Literal` 이 Decimal 을 거부(event_ir.py:441)해 float 를 쓰게 되고 CLAUDE.md 14 와 충돌. (a)는 정수만 쓰고 나눗셈이 없다. |
| **D4** | `구매금액차이가 10% 이상 증가` 의 `10%` | (a) 상대 변화율 (b) 퍼센트포인트 (c) 절대 변화량 | **(a)**. `%p` 는 리터럴 원장이 이미 명시적으로 배제하고 있고(semantic_ir.py:46-50) 이 지표는 비율이 아니다. `차이가` 는 변화 서술어로 읽는다. |
| **D5** | 낱말형 크기(`절반`·`반으로`·`두 배`·`급증`) | (a) 영구 unsupported (b) 정량 매핑을 정책 카탈로그에 **선언**(`절반`=0.5, 연산자는 `<=` 인가 `=` 인가까지) (c) clarification 질문 | **초기 (a) → 필요 시 (b)**. `절반으로 줄어든` 이 "≤50%"인지 "≈50%"인지는 도메인 결정이고 코드가 정할 일이 아니다(CLAUDE.md 12). 선언한다면 `AUDIENCE_QUALITATIVE_DEFAULTS` 선례를 따라 **카탈로그에** 두고, 정책이 채운 값임을 의미검증기에 고지한다. |
| **D6** | `percentage` 바인딩 방면 방식 | (a) 영수증 면제(Phase 3) (b) `_binding_target` 이 `("number", v)` 도 허용 | **(a)**. (b)는 무관한 숫자 10(예: `상위 10명`의 10)이 `10%` 를 방면해 버린다. |
| **D7** | 절대 변화량의 기준선 0(`5만원 이상 증가`, 2월 무주문) | (a) 포함 (b) 제외 | **(a)**. 절대 증가량은 0 기준에서도 정의된다. D1 과 다르게 가는 것을 **명시적으로** 결정해야 한다. |
| **D8** | 암묵 두 번째 창(`전월 대비 …`, `최근 3개월 vs 그 이전 3개월`) | (a) 미지원 유지 (b) 앵커+오프셋으로 파생 | **(a) 유지, 별도 과제**. 현재 롤링 창 두 개가 같은 값으로 나오는 등 앵커 개념 자체가 없다. 이 플랜의 범위를 넘는다. |
| **D9** | `지지난달`/`지지난주`/`전전월` 지원 여부 | (a) 가드만(창 없음 → 결핍 질문) (b) 어휘에 offset −2 선언 | **(a) 먼저, (b)는 선택**. 안전 수정이 의미 논의에 막히지 않도록 분리. |

---

## 5. 이 플랜이 고치지 못하는 것

1. **암묵 창·롤링 비교** — `전월 대비 구매금액이 증가한`(두 번째 창 생략), `최근 3개월 vs 그 이전 3개월`(앵커/오프셋 부재)은 캡처 0 그대로다. 캡처에 성공하는 `전월 대비 당월 …` 조차 라이브에서 `semantic_registry_gap` 으로 끝나므로, 이 부류의 라이브 실패 원인은 상류(V4 방출)에 따로 있다.
2. **비카탈로그 지표** — `방문횟수` 등은 `_aggregate_metric_hits` 가 못 잡아 계획이 서지 않는다. 카탈로그 확장 과제이지 이 플랜의 대상이 아니다.
3. **월 스냅샷 지표의 기간 비교** — `MONTHCRMINFO` 는 실데이터가 201701 한 달뿐이다. Phase 5 는 **오분류를 고칠 뿐 지원을 확대하지 않는다**. 이 지표들의 기간 비교는 물리적으로 불가능하다.
4. **LLM 방출 변동성** — Phase 4 의 합성은 모델이 `expression=null` 을 냈을 때만 개입한다. 모델이 *틀린* 표현을 자신 있게 내면 여전히 리터럴 정산과 9단계 의미검증에 의존하고, 후자는 확률적이다(`_is_noncredible_inverted_verdict` 로 반전 판정이 강등되는 경로가 있다).
5. **소수 퍼센트의 스케일** — `10.5%` 는 `L*1000 >= R*1105` 가 된다. 자릿수가 큰 금액 합계에서 오버플로 여유를 확인해야 한다(decimal 컬럼이라 여유는 있으나 실측 필요).
6. **`상위 10%` 축과의 상호작용** — 한 문장에 랭킹 퍼센트와 변화율 퍼센트가 동시에 있으면 소유권 대장이 갈라야 한다. Phase 0-A 의 가드로 막지만, 그 조합의 회귀 코퍼스는 없다.

## 6. 남은 위험

- **Phase 0 은 의도적으로 거절을 늘린다.** 프로젝트 목표("SQL 생성 > 거절")와 일시적으로 충돌하며, Phase 2-4 가 배포되기 전까지 이 부류는 clarification 으로 끝난다. 되돌리려면 등록 한 줄이지만, 되돌리는 순간 `절반` 류 조용한 오답이 되살아난다.
- **`change_magnitude` 의무의 오탐이 가장 큰 회귀 위험이다.** 이 원장의 미방면은 곧 출고 차단이므로, 오탐 하나가 무관한 프롬프트를 막는다. 소유권 가드와 대조군 코퍼스를 캡처 규칙과 **같은 커밋에** 넣어야 한다.
- **hull 청구 포기(Phase 1)는 실패 사유를 바꾼다.** `semantic_emission_failure` → `unsupported`. 실패 사유로 계약된 테스트(`tests/test_failure_reason_preservation.py`)가 임계 있는 원문을 쓰지 않는지 확인해야 한다(현재 픽스처는 임계 없는 원문이라 안전하다).
- **근거의 출처가 균일하지 않다.** 아래 §0 의 네 항목은 이 문서 작성 시 직접 재현했다. 그 밖의 실측(라이브 8회 중 1회 출고, `(11-10)/NULLIF(10,0)=0`, `MAX(YYYYMM)=201701`, 실패 로그 20건 분포 등)은 조사 단계의 검증 결과이며 재확인하지 않았다. pytest 기준선도 재실행하지 않았다 — 각 Phase 착수 전에 기준선을 다시 재야 한다.

---

**핵심 파일 좌표**
`c:\git\recommendation-campaign-system-python\lowering_planner.py`(73-81, 226-305, 290-294, 374-456, 400-410) ·
`c:\git\recommendation-campaign-system-python\semantic_requirements.py`(1375-1435, 1645) ·
`c:\git\recommendation-campaign-system-python\canonical_audience_claims.py`(47-97, 230-310, 1304-1350, 1593-1680) ·
`c:\git\recommendation-campaign-system-python\query_structurer\audience_execution.py`(163-232, 612-707, 1229-1247) ·
`c:\git\recommendation-campaign-system-python\query_structurer\prompt.py`(239-290, 307) ·
`c:\git\recommendation-campaign-system-python\query_structurer\semantic_ir.py`(46-52, 332-338) ·
`c:\git\recommendation-campaign-system-python\calendar_window.py`(749-786, 802-804) ·
`c:\git\recommendation-campaign-system-python\audience_runtime.py`(900-940) ·
`c:\git\recommendation-campaign-system-python\event_ir.py`(437-530) ·
`c:\git\recommendation-campaign-system-python\graph_rag.py`(9867-9873) ·
`c:\git\recommendation-campaign-system-python\tests\test_aggregate_comparison_lowering.py`(전체 — §6~§10 확장 대상)