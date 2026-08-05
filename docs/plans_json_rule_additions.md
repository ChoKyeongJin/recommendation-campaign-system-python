# JSON 규칙 추가 플랜 (예시 89건 감사 기반)

작성 2026-08-04. 원천: `docs/examples/member_extraction_nl_complex_cases.md` 89건을 런타임 JSON 규칙 6계열과 전수 대조한 감사 + 그 보고서의 실측 주장 40건을 직접 재현한 Phase 0.

**이 문서의 핵심 결론: 어휘를 추가할 것은 거의 없다.** 후보 89건 중 45건이 "그 JSON 키를 읽는 프로덕션 소비자가 0"이라는 이유로 탈락했고, Phase 0 재현에서 생존 항목 중 다시 절반이 근거를 잃었다. 실제 사용자 피해는 어휘 부재가 아니라 파서 문법의 인접성 결함과 코드의 fail-open 기본값이다.

---

## 구현 완료 (2026-08-05)

Phase 1·2·3·5 와 Phase 4 축소분을 구현했다. 기준선 `19dca5f` 대비 **NEW FAILURES 0 / FIXED 0**,
가드 5종 상태 동일(선재 RED 3종 그대로), 수집 2468 → 2519(새 테스트 순증).

> **후속 고지(같은 날, 이 문서보다 나중)**: 이 표의 `legacy_plan_compiler.py` 변경분은
> 그날 늦게 **모듈째 삭제**됐다. SemanticPlanV2 중간표현이 폐기되면서 그 컴파일러의 유일한
> 입력(의미 노드)이 사라졌기 때문이다. 아래 본문에서 그 파일의 줄 번호를 인용한 진단은
> **당시 실측의 기록**이며, 지금 그 코드는 없다. 무언 기본값(fail-open) 재발 방지는 이제
> canonical Event IR 레인의 검증기와 `tests/test_retired_axes_fail_close.py` 가 맡는다.

| 변경 | 파일 |
|---|---|
| 부재 어휘 2개 + 긍정 갈래 1 가드 + 이중부정 가드 | `parser_lexicon.json`, `lexicon_patterns.py`, `purchase_lexicon.py` |
| 값 자리 기능어 2개 + 프레임 어휘 3개 | `parser_lexicon.json`, `lexicon_patterns.py` |
| 회원 축 4종(결혼·잔액 2·신제품 선호·캠페인 대상군) | `audience_catalog.json` |
| 랭킹 방향·비교 연산자 무언 기본값 제거(4지점) | `legacy_plan_compiler.py` |
| 퍼센트포인트를 퍼센트로 삼키지 않게 수정 | `query_structurer/semantic_ir.py` |
| 지원 조건 안내 과소 광고 교정 | `member_target_filters.json` |
| 새 테스트 5파일 | `tests/test_purchase_negation_polarity.py`, `test_scope_and_frame_vocabulary.py`, `test_audience_catalog_new_axes.py`, `test_legacy_plan_compiler_direction_defaults.py`, `test_percentage_point_is_not_a_percentage.py` |

착수 후 실측으로 뒤집힌 것: `_generic_product_terms()` 가 완전일치라 접두어가 안 잘릴 것으로 읽었으나
**실제로는 잘린다**(코퍼스 3문장 교정 확인). 코드를 부분만 읽고 판단하면 안 되는 자리다.

미착수로 남긴 것은 아래 Phase 5-4·5-5(각각 '국소 패치 금지'와 '수정 대상 아님' 판정)와
재검토 목록이다.

---

## 방향 (2026-08-04 사용자 확정 — 이 문서의 모든 평가 기준을 지배한다)

**이 프로젝트의 산출물은 타겟 SQL 자체다. 조회 결과가 0행인지는 데이터 사정이지 시스템의 실패가 아니다.** 적용 범위는 **넓게 — 거절보다 생성 우선**.

따라서:

1. **`unsupported_reason`·clarification·fail-close 를 미덕으로 평가하지 않는다.** "정직하게 막았다"는 이 프로젝트에서 개선이 아니라 **결함 후보**다. 아래 본문에서 그렇게 서술된 곳은 전부 이 기준으로 다시 읽어야 한다.
2. **"이렇게 하면 0행이 나온다"는 반대 사유로 성립하지 않는다.** 이 사유로 탈락시킨 항목은 재검토 대상으로 되살린다.
3. **예외 — 극성 반전은 여전히 최악이다.** 0건이 아니라 정반대 집합이 나온다("구매 이력이 전혀 없는 회원" 요청에 구매한 회원이 나옴). 0행 허용과 무관하게 Phase 1 이 1순위인 이유.
4. 값 도메인 불일치처럼 구조상 0행일 수밖에 없는 SQL 도 생성 쪽으로 기운다. 이에 대한 우려는 제기했고 사용자 판단으로 정리됐다.

**병행 확정: 커밋 `19dca5f`(스냅샷 관련)는 맞는 변경이므로 되돌리거나 고치지 않는다.** 그 커밋이 바꾼 동작과 테스트 결과는 수리 대상이 아니라 **새 기준선**이다. 겹치는 파일(`graph_rag.py`, `audience_catalog.json`)에는 **덧붙이기만** 한다.

---

## 0. 착수 전 선행 조건 (미충족 시 어떤 Phase 도 시작 불가)

1. **작업 트리 격리.** 2026-08-04 23:17~23:29 사이 다른 세션이 `graph_rag.py`·`semantic_receipts.py`·`semantic_relation_ownership.py`·`semantic_plan_event_lowering.py`·`query_structurer/campaign_plan_v4.py`·`audience_catalog.json` 과 테스트 6개를 편집 중이었다. 같은 pytest 명령이 실행 도중 **8 → 17 → 18 failed** 로 갈렸다. 이 상태에서는 기준선 측정도 회귀 판정도 성립하지 않는다. `audience_catalog.json` 은 Phase 3 의 편집 대상과 정면으로 겹친다.
2. **회귀 판정은 숫자가 아니라 nodeid 집합 diff 로 한다.** 실측에서 동시 편집이 기존 8건을 하나도 고치지 않은 채 실패를 10건 늘렸다. 숫자만 비교하면 집합이 바뀌어도 통과한다. 실행 직전·직후 `git diff` 지문이 동일함을 함께 기록한다.
3. **`pytest 전부 green` 을 완료 조건으로 쓰지 않는다.** clean 트리(HEAD=a56914b) 기준선이 이미 **8 failed / 2439 passed / 21 skipped** 이고, `module_size_ratchet` 과 `query_pipeline strict mypy` 가드 2개와 `ruff check .`(I001 1건)도 착수 전부터 red 다.
4. **`canonical_coverage` 래칫은 JSON 편집만으로 red 가 된다** (legacy JSON 에 컬럼 1개 추가 → 45→46). Phase 3·4 는 이 래칫을 반드시 건드리므로 값 갱신 여부를 사전에 결정한다.
5. **`19dca5f` 가 바꾼 것은 회귀가 아니다.** 기준선 재측정에서 이전 HEAD(a56914b) 대비 늘어난 실패가 있어도 그것은 의도된 변경이므로 수리하지 않고 새 기준선으로 기록한다.

---

## Phase 1 — 구매 부재 극성 반전

### 확정 사실

| 예시 | 입력 | 현재 출력 | 원인 |
|---|---|---|---|
| 3 | "현재까지 구매 이력이 전혀 없고" | `['purchase:exists']` | **긍정 갈래 2 의 부정 가드** `(?!(?:BOUND)*(?:NEG))` 가 인접성 결함으로 '전혀'를 못 넘어 통과 |
| 36 | "온라인몰 주문이 한 건도 없는 회원" | `['purchase:exists']` | 어미 `'한'`(event_completion_ending)이 수사 `'한'`(하나)과 표면 동형 → 긍정 갈래 1 이 `'주문이한'` 에서 오발화. **긍정 갈래 1 에는 부정 가드가 아예 없다** |

두 예시는 **원인이 다른 별개 결함**이다. 한 수정으로 묶지 않는다.

### 방침 결정 (선택지 2개, gap 방식 우선)

- **안 A — 어휘 추가**: `generic_negation` 에 `"전혀없"`, `"한건도없"`. 예시 3 은 완전 교정, 예시 36 은 `absent+exists` 양극성 동시 방출로 바뀔 뿐 **미해결**.
- **안 B — gap 방식(권장)**: `audience_frame.local_negation_spans` 가 `DEFAULT_NEGATION_GAP=8`(글자수 다리)로 **예시 3·36 을 이미 둘 다 정확히 잡고 있다.** 저장소 안에 어휘를 늘리지 않고 인접성 결함을 푸는 작동하는 선례가 있다. 같은 방식을 N1 다리와 긍정 가드에 **대칭 적용**하면 예시 36 의 긍정 가드 부재 문제까지 함께 풀린다.

안 A 를 채택하더라도 예시 36 은 별도 항목으로 뗀다. 착수 전에 하류가 `absent+exists` 를 어떻게 접는지(롤업 정책)를 먼저 확인해야 예시 36 의 실제 사용자 영향을 안다.

### Phase 0 이 뒤집은 것

- 단독형(`"전혀"`·`"한건도"`) 금지 근거로 제시된 반례 3문장(`'전혀 다른 카테고리를 구매한'`, `'한 건 도착한'`, `'한 건도 넘게 있는'`)은 **단독형/합성형/대조군에서 출력이 전부 같아** 아무것도 구분하지 못한다. 세 문장 모두 부정어가 구매 명사 **앞**에 있어 N1 다리와 긍정 가드에 애초에 닿지 않는다. 89개 전수 diff 0건, pytest 차이 0건.
  → **결론(단독형 금지)은 유지, 근거는 교체.** 실제 조건은 "부정어가 구매 명사 **뒤**에 올 때만 해가 난다". 회귀 테스트에 위 3문장을 박으면 단독형 회귀를 전혀 못 잡는 무력한 테스트가 된다.
- `lexicon_patterns._CODE_FALLBACK` **어휘 동등성 테스트는 존재하지 않는다.** 유일한 후보 `test_data_file_and_code_fallback_agree` 는 패턴 이름만, 그것도 단방향으로 본다. JSON 에만 넣으면 조용히 어긋나고 아무 테스트도 걸리지 않으며, 파일이 깨지면 반전이 되살아난다.
- **pytest 2447개가 이 어휘 변경에 대해 어떤 방향으로도 무감각하다.** Phase 1 은 안전망 없이 진행된다 — 수정과 함께 테스트를 새로 만들어야 한다.
- 미보고 회귀 1건: `'구매 이력이 전혀 없지 않다'`(이중부정, 사람 판단 EXISTS)가 합성형 적용 시 `exists → absent` 로 뒤집힌다.
- 부채 기록: `generic_negation` 은 compact 좌표계와 **원문 좌표계 양쪽**에서 소비되는데 공백 없는 합성형은 한쪽에서만 작동한다(`audience_frame:279` 에서는 죽은 낱말).

### 함께 만들 테스트

어휘 동등성(JSON ↔ `_CODE_FALLBACK`), 예시 3·36 극성 고정, 이중부정 `'전혀 없지 않다'`, 부정어가 구매 명사 뒤에 오는 5문장, `'두 건도'` 대조(수사/어미 동형 증명).

---

## ~~Phase 2 — "넘지 않는" 연산자 반전~~ → **삭제**

**헤드라인이 반증됐다.** 예시 68("어떤 브랜드도 결제금액 점유율 30%를 넘지 않는")은 조용히 뒤집히지 않는다.

- `legacy_plan_compiler.py:703·803` 의 `or ">="` 는 실재하지만 예시 68 과 무관한 optional 필드 2곳(기간대비 변화율 / 교집합 개수)에만 적용된다.
- `graph_rag.py:4443·4467` 경로는 `>=` 가 아니라 `>` 를 내며 **프로덕션 호출자가 0인 죽은 코드**다([[rules-layer-gutted-and-stale-snapshots]] 의 'rules 파서 전면 침묵'과 일치).
- 예시 68 이 실제로 도달할 `aggregate_predicate` 경로는 조용한 반전 없이 `validation_mismatch` 로 fail-close 한다.
- '점유율' 지표 자체가 시스템에 없어 예시 68 은 연산자 이전 단계에서 이미 막힌다.

**단, 위 두 줄은 '방향' 절에 따라 재분류된다.** 최초 작성 시에는 fail-close 를 "정직하다"고 긍정 평가했으나, SQL 생성이 목적이면 **SQL 이 아예 안 나오는 것이 결함**이다. 예시 68 은 "조용한 반전 없음 = 문제 없음"이 아니라 **"SQL 미생성 = 고쳐야 할 것"** 으로 다시 세운다.

**대체 안건 3개로 분리:**
1. `legacy_plan_compiler` optional 연산자 2곳의 fail-open 기본값 제거. 검증 문형은 예시 68 이 아니라 `'변화율이 10% 넘지 않는'` 으로 교체. (이 건은 방향과 무관 — 잘못된 값을 내는 것이지 안 내는 것이 아니다)
2. **`aggregate_predicate` 의 `validation_mismatch` fail-close 완화** — 어디까지 SQL 을 낼 수 있는지 조사. 신규 안건, 방향 전환으로 생김.
3. **'점유율(share) 지표' 지원** — 미지원 선언이 아니라 지표 추가로 방향 전환. 별도 안건.

`condition_normalizers._CODE_FALLBACK` 관련 정정: '바이트 동일'이 아니라 **파싱된 객체의 구조적 동등(==)**, '전 섹션'이 아니라 `_CODE_FALLBACK` 에 있는 **6개 섹션만**. 리스트 순서는 유의미하므로 두 소스에 같은 순서로 넣어야 한다. 인접 가드 2개: `test_comparison_word_operators_preserve_surface_regex_order`, `test_extended_operator_aliases_stay_out_of_core_words`.

---

## Phase 2(신) — 값 자리 잔여 기능어 제거

`parser_lexicon.json vocabularies.event_scope_value_stopword`.

- `"동안"` — 오염 제거는 정확히 동작하나 **교정은 3건이 아니라 1건(예25)** 이다. '3'은 claim 이 나온 문장 수(예23·25·29)를 교정 건수로 잘못 옮긴 것으로 보인다.
- **낱말 하나가 아니라 계열로 다룬다.** 같은 오염이 하나 더 실측됐다 — `"이상의"` 를 같은 목록에 넣으면 예29 가 `['이상의 중분류'] → ['중분류']` 로 교정된다.
- 잔여 노출 정정: `'동안 크림'` 한 건이 아니라 **띄어 쓴 `동안 <상품어>` 계열 전체**다. 반대로 실질 규모는 보고서보다 작다 — 이 배포의 값 인덱스·RAG 지식베이스에 standalone `'동안'` 토큰을 가진 값이 **0개**다(`'안양시 동안구'` 는 토큰이 달라 무영향). 즉 현재 데이터 기준 가설적 노출이다. **값 인덱스 재적재 시 재확인 항목으로 남긴다.**
- `frame_particle += "만"`, `request_directive += "남겨줘"/"남겨주세요"` — 동반 적용해야 `"…회원만 남겨줘"` 가 회수된다. 단독으로는 각각 코퍼스 이득 0. 보고서가 스스로 반증한 "예3/4/14/24/30/43/54/64/71 이 고쳐진다"는 실측 반증됐다(전부 다조건 문장) — 커밋 메시지에 쓰지 말 것.
- `bound_particle` 에는 `"만"` 을 넣지 않는다(구매 극성 정규식 다리에서 `10만원` 류와 상호작용).

---

## Phase 3 — canonical Event IR 축 4종 (`audience_catalog.json`)

물리 컬럼 6개 전부 `schema_catalog.json` 에 실재 확인. 코드 변경 0. 원안(`member_target_filters.json`)은 커버리지 래칫 red 로 기각 — 올바른 소유자는 canonical 레인이다.

| 축 | 상태 |
|---|---|
| (a) 결혼 여부 `subject.married` / `MARRIAGE_YN` | 채택 |
| (b) 예치금·적립금 잔액 `DEPOSIT_BALANCE_AMT` / `CARROT_BALANCE_AMT` | 채택. `numeric_filters`·`ratio_filters` 차집합 2컬럼도 함께 닫힘 |
| (c) 신제품 선호 `NEWPRODUCT_FAVOR_YN` | 채택. **SQL 바이트 동일 재현 완료** — `EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO MS WHERE MS.MEMBER_NO = B.MEMBER_NO AND MS.NEWPRODUCT_FAVOR_YN = 'Y')` |
| (d) 캠페인 대상군 `Z_CAMP_MBR` | **조건부** — 아래 수정 필수 |

### (d) 필수 수정 2건

1. **별칭 충돌 수리.** 보고서대로 alias `M` + from_sql 안 하드코딩 `ZC` 로 적으면 기존 `campaign_contact_success` 와 별칭이 같아 캠페인 단위 상관식이 `ZC.CAMP_SDATE = ZC.CAMP_SDATE` **항진명제로 조용히 무너진다**(검증기도 통과 — 별칭이 다 허용목록에 있음). alias 를 `MT` 로, 조인 별칭을 `{alias}_ZC` 로 매개변수화한다(새 별칭 0개, 허용목록 추가 불필요).
2. **의미 재기술.** 생성식은 회원 단위 `∃c target(c) ∧ ¬∃c (target(c)∧contacted(c))` 이고 예59 가 말하는 캠페인별은 `∃c (target(c) ∧ ¬contacted(c))` 로, 전자 ⊊ 후자다 — 캠페인 A 는 접촉 성공, B 는 미접촉인 회원이 조용히 빠진다. '예59 해결'이 아니라 **'예59 의 1절 중 회원 단위 근사'** 로 적는다. 캠페인 단위가 필요하면 `CAMP_ID` 기반 조인 필드가 있어야 한다(현재 `campaign_contact_success.execution_id` 는 `CONCAT(CAMP_ID,':',CAMP_EXEC_NO)` 로 존재하나 `campaign_target` 에는 없음).

### 미결 사항 해소

`signal_coverage.campaign_contact` 에 **포함하는 쪽이 맞다**. 감지기가 "캠페인대상"을 `campaign_contact` 로 방출하므로 별도 family 는 죽은 선언 + 오탐 경고를 만든다.

### 금지 별칭 규칙 (근거 축소)

`subject.married` 에 맨 `"결혼"` 금지. 근거는 **예27 하나**다(예4 는 정당한 발화라 이 계층에서 정발화/오발화 구분 불가). 대가는 조건 추가가 아니라 `execution_assets` canonical 계층이 그 구간을 자기 소유로 주장해 강등 판정에서 빠지는 것이다.
**일반화 규칙**: 한국어 단일 토큰 별칭은 부분문자열로 매칭되므로, **다른 실재 컬럼의 표면어를 접두어로 갖는 2글자 별칭은 전부 금지**(여기서는 `WEDDING_DAY` = '결혼기념일'). 제안된 `"결혼 여부"`(2토큰)는 순서보존 토큰 매칭이라 안전.
가드 테스트의 정확한 assertion: "예27 텍스트에 `subject.married` 가 발화하지 않는다".

### 재측정 필요

컴파일·래칫·별칭·신호커버리지 재현은 유효(의존 파일 전부 미변경). **'전체 스위트 회귀 0' 만은 안정된 작업트리에서 다시 재야 한다.**

---

## ~~Phase 4 — 미지원을 정직하게 선언~~ → **폐기 (방향 전환)**

> **거절을 늘리는 방향이라 프로젝트 방향과 정면으로 반대다.** 아래 항목 전부 보류하고, 대신 같은 표현들을 **지원하는** 방향으로 재검토한다.
>
> - clarification 3키 추가 → **보류.** 안내 문구를 늘리는 것은 SQL 을 안 내겠다는 선언이다.
> - `supported_condition_hint` 교체 → **부분 보류.** 과소 광고(구매주기·다음 구매예정일이 실제로는 되는데 미지원으로 적혀 있음) 교정만 살린다. 미지원 고지 8건 흡수는 폐기.
> - `attribute_catalog.grade_growth_type` 을 `binding: null` 로 두어 unsupported 귀결 → **역전.** `'등급 성장 유형'` 이 `member_grade` 로 오분류되는 것은 여전히 결함이지만, 해법은 unsupported 선언이 아니라 **`GRADE_GROW_TYPE` 값 코드 사전을 확보해 실제로 컴파일되게 하는 것**이다.
> - `_` 접두 문서화 키 3건 → 폐기(원래도 사용자 비노출이라 가치가 낮았다).
>
> 아래는 폐기 전 원안 기록.

### (원안 — 보류)

- `clarification_messages.ko.json` 에 3키(`semantic_scope_unknown`, `semantic_domain_unknown`, `semantic_complexity_limit`). 현재 나머지가 전부 한 문장으로 뭉개진다. 제약: 중괄호 치환자 금지, 내부 사유 코드·노드명·`NOT EXISTS`·`AGG` 표현 금지, `version` 1 유지. `event_expression_schema_invalid` 는 **넣지 말 것**(그 코드를 만드는 분기가 프로덕션 도달 불가).
- `member_target_filters.json :: supported_condition_hint` 교체. 과소 광고(구매주기·다음 구매예정일)와 과대 광고(가입매장) 동시 교정, 미지원 고지 8건을 한 문자열로 흡수. **프로세스 재시작 필요**(`lru_cache(maxsize=1)`).
- `attribute_catalog.json :: attributes.grade_growth_type`. 현재 `'등급 성장 유형'` 이 `'등급'` 2자 부분일치로 `member_grade(EMART_GRADE_CD)` 에 조용히 오분류된다. `binding: null` 로 `_blocked('unsupported', …)` 귀결. `value_category` 는 넣지 않는다(값 코드 사전 없음).
- `_` 접두 문서화 키 3건(`net_of_returns`, `next_purchase_due_date._unsupported`, `ratio_filters[0]`)은 **선택**. `_supported:false` 는 사용자에게 노출되지 않는다 — 가치는 커버리지 기준선 정합성과 사람의 오독 방지에 한정. 사용자 대면 고지는 전적으로 `supported_condition_hint` 가 담당.

---

## Phase 5 — 코드 결함 (4 → 5지점, 우선순위 재배치)

| 순위 | 지점 | 판정 |
|---|---|---|
| 1 | `legacy_plan_compiler.py:783` — `"descending"` 아닌 **모든** 값이 `"bottom"` 으로 떨어져 상위/하위 무언 반전 | **보고서에 없던 5번째 지점.** 실행 확인됨 |
| 2 | `legacy_plan_compiler.py:703·803` — `or ">="` fail-open 기본값 | 실재. 영향 범위는 optional 필드 2곳 |
| 3 | `query_structurer/semantic_ir.py:39` — `_PERCENT_RE` 가 `'10%포인트'` 에서 `'포인트'` 를 버려 **%p → % 축약** | 정확히 재현. 단 예49·73 은 채널 비중·할인율 지표가 어휘에 없어 상류에서 막히므로 **현재는 잠복 결함** |
| 4 | `graph_rag.py:4443·4467` — 부정형 반전 | **원인 귀속이 틀렸다.** 진짜 원인은 `넘` 원자도 `startswith` 도 아니고 **비교 문법 어디에도 부정 스코프 처리가 없다는 것**. `넘` 만 고치면 6분의 1만 고쳐지고 '넘지 않는'은 되는데 '초과하지 않는'은 안 되는 비일관을 새로 만든다. **국소 패치 금지.** 올바른 최소 수정은 절 단위 부정 탐지 패스를 하나 넣어 6개 원자에 균일 적용. 회귀위험 큼 — `_parse_amount_comparison` 은 age/balance/aggregate/count 가 공유하는 단일 문법이고([[shared-comparison-grammar]]) `_comparison_operator` 는 `tests/test_aggregate_span_binding.py` 4곳에서 직접 주입된다 |
| 5 | `attribute_token_groups.json` 문법 극성 반전 | **수정 대상 아님.** 반전 3종(접두 부정·나열형 제외·'여부가 N')은 예1·6·63 에서 재현되나 `member_flag` 는 실행기가 없어 **SQL 을 전혀 만들지 않는다**(8ba50b6 삭제). 실제 피해는 뒤집힌 SQL 이 아니라 올바른 LLM 재작성을 소실로 오판해 폐기하는 **드리프트 오탐**(원문 폴백이므로 fail-safe 방향). 보고서에 없는 더 명확한 결함 증거: `'여부가 Y'` 와 `'여부가 N'` 의 출력이 **동일**하다. 선행 결정 필요 — (a) 고아 스펙·게이트 폐기 vs (b) 실행기 복원. (b) 이후에야 문법 수정이 의미를 갖고, 그때도 세 구문은 JSON 접미어 정규식으로 표현 불가라 코드 패스로 가야 한다 |

부수 정리: `graph_rag.py:2641` 고아 주석, `5308·5452·5512` 의 사실과 다른 주석.

---

## 방향 전환으로 되살아난 것 (재검토 대상)

아래는 **"0행이 나온다"를 반대 사유로 탈락시킨** 항목들이다. 0행이 무해하다면 그 사유는 성립하지 않으므로 전부 재검토한다.

| 항목 | 원래 탈락 사유 | 재검토 시 확인할 것 |
|---|---|---|
| `purchase_product_target.match_columns` 에 `SALE_STATE_CD` | `LIKE N'%판매종료%'` 인데 저장값은 `END` 라 항상 0행 | 값 매핑(`판매종료`→`END`)을 주면 **실제로 맞는** SQL 이 된다. 0행이 아니라 값 도메인 문제였으므로, 매핑을 넣으면 정상 지원이 된다 |
| `eq_filters` 에 `BABY_YN_CD` | 값 도메인 미확인, 추측 값이 0행 | 실데이터로 값 도메인을 확인하면 해소. 확인 없이 추측 값을 넣는 것만 금지 |
| `unit_tokens` 에 `'배'`·`'%포인트'` | `'5배 이상'` → `purchase_amount >= 5` 로 **의미가 틀린** SQL | **되살리지 않는다.** 이건 0행 문제가 아니라 의미 왜곡이다(5배 ≠ 5원). 0행 허용과 무관 |
| `discount_amount.synonyms` 에 `'할인율'` | `SUM(DC_AMT) >= 20`(원) 으로 사실상 전원 매칭 | **되살리지 않는다.** 0행이 아니라 정반대(전원 매칭) 문제 |

즉 되살아나는 것은 **값 도메인·매핑만 채우면 맞는 SQL 이 되는 2건**이고, 의미가 왜곡되는 2건은 방향과 무관하게 그대로 탈락이다.

## 넣지 않기로 확정한 것 (재론 방지)

- **통계 어휘**(중앙값·표준편차·변동계수·이동평균·상관계수·분위수·백분위)를 `unsupported_attribute_hints` 에 추가 — `build_attribute_index`/`bind_clause`/`bind_attributes` 프로덕션 호출부 **0**, 메시지 키 렌더 코드도 0. 죽은 레지스트리를 하나 더 만든다.
- **`ambiguous_degree_terms` 전체**(상위권·꾸준히·주로·집중된·급감·규칙적인) — 유일 소비자 `normalization_orchestrator.resolve_condition` 을 테스트 2개만 import. 런타임 귀결이 1비트도 안 바뀐다.
- **`unit_tokens` 에 `'배'`·`'%포인트'`** — 조용한 오답. `'평균 구매금액의 5배 이상'` → `purchase_amount >= 5` 가 claimed_supported 가 된다.
- **`discount_amount.synonyms` 에 `'할인율'`** — `SUM(DC_AMT) >= 20`(원) 으로 사실상 전원 매칭.
- ~~**`purchase_product_target.match_columns` 에 `SALE_STATE_CD`**~~ → **재검토로 이동**(위 절 참조). 0행이 아니라 값 매핑 문제였다.
- **`span_binding.conjunction_tokens` 에 `'인데'`** — `'지만'` 이 부분문자열로 이미 24회 전부 덮고, `'온라인데이터'` 오절단이 실측된다.
- **`request_directive` 에 `계산해줘/분류해줘/정렬해줘`** — 잉여어 허용목록이라 추가 산출 지시가 조용히 삭제된다(fail-open). 코퍼스 이득 0.
- **`cart_terms` 확장** — 소비자 0. 장바구니 문장 21건 전부 `'장바구니'` 를 포함해 공백 0.
- 전체 목록(45건)과 사유는 감사 워크플로 산출물 참조.

## 재검토 보류 1건

`audience_catalog.sources.active_cart.aliases` 에 장바구니 유지 표현 추가 — 원래 REJECT 근거 두 축이 모두 반증됐다(별칭은 LLM 프롬프트 `[Sources]` 글로서리로 렌더돼 모델 소스 선택에 직접 영향, 강등 판정은 CANONICAL 을 제외하므로 무관). 다만 라이브 LLM 경로를 돌려보지 않아 예35·36·40·43 이 실제로 `active_cart` 로 가는지는 미실증. **이 항목만 별도 검증 후 판단.**
