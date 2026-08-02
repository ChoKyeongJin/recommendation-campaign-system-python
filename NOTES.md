# 작업 노트 — canonical audience 경로 + 실행 계층 범용화 (2026-08-02)

두 개의 큰 변화가 같은 날 착지했다. 앞의 것은 다른 세션이(79dad3d, f4b8034), 뒤의 것은 이 문서를
쓰는 작업이 했다. NOTES 에 79dad3d 이후 기록이 전무했으므로 둘 다 여기 적는다.

---

## A. canonical audience 경로 (79dad3d + f4b8034, 다른 세션)

`+4,693/-847` 28파일 + 후속 수정. 커밋 메시지는 "ㅁ" 와 "오류수정" 이라 여기서 내용을 남긴다.

**무엇인가.** 의미 → SQL 로 가는 **두 번째 길**이 생겼고, 그쪽이 범용 경로다.

```
canonical  SemanticPlanV2 → semantic_plan_event_lowering → Event IR → event_compiler → SQL
           물리 바인딩은 docs/data/runtime/semantics/audience_catalog.json 이 선언한다.
           lowering 에는 사건별 분기가 없다(새 사건 = JSON 한 항목).
legacy     query_plan 슬롯 → 20개 빌더 레지스트리 → SQL
           빌더 몸통이 SQL 모양을 코드로 들고 있다.
```

`graph_rag._apply_semantic_plan_pipeline` 이 canonical 을 **먼저** 시도하고(all-or-nothing,
fail-close), 표현하지 못할 때만 legacy 브리지로 내려간다.

**신규 모듈.** `resolved_semantic_catalog`(바인딩 해석) · `semantic_plan_event_lowering`(결정론 하강) ·
`audience_runtime`(카탈로그 로딩) · `audience_schema`($ref/$defs 압축 LLM 스키마) ·
`canonical_audience_claims`(청구·영수증).

**Event IR 관계대수 확장.** `Project`/`Summarize`/`Order`/`Limit` + semi/anti Join 을 추가해
"2019년 가장 많이 팔린 상품 10개를 구매한 고객" 류를 **일반 대수**로 표현한다(전용 빌더 아님).

**LLM 스키마 압축.** 깊이 전개형 → `$ref`/`$defs` 고정형. 카탈로그가 커져도 전송면이 고정된다.
라이브에서 OpenAI strict 모드 수락 확인(2026-08-02).

## B. 실행 계층 범용화 (Phase 0~5)

상세는 `docs/plans_generic_execution_layer.md`. 여기서는 **재현 가능한 숫자와 함정**만.

### 측정

| 항목 | 착수 전 | 완료 후 |
|---|---|---|
| pytest | 964 / 0 failed | **1,135 passed / 24 skipped / 0 failed** |
| 소스 물리 바인딩 | 기준선 343 (실측 333) | **184** |
| graph_rag.py | 17,470줄 (상한과 동일, 여유 0) | **17,247줄** (상한 17,300) |
| db_swap_preflight | PASS (레지스트리 2종) | **PASS (4종)** |
| live-26 | 재현 불가 | **SQL 4 / 미지원 12 / 되묻기 9 / 실패 1** |

### live-26 기준선 분쟁이 닫혔다

같은 코퍼스에 대해 세 숫자(NOTES 14/26 · architecture_generic_core 0/26 · 세션 기억 12/26)가
서로를 반박하며 공존했다. 원인은 단순했다 — **측정 러너가 저장소 밖(세션 스크래치패드)에 있었다.**
`tools/live_prompt_baseline.py` + `docs/data/test_baselines/live_prompts.json` 로 커밋했다.

**함정 1: 분류기가 status 를 봐야 한다.** 첫 측정은 `clarification_questions` 를 먼저 봐서
정직한 미지원 12건을 전부 '되묻기'로 셌다(unsupported 0). 미지원 응답도 같은 문구를 그 필드에
싣기 때문이다. 권위는 `status`(`success`/`needs_clarification`/`unsupported`/`no_verified_sql`).

**함정 2: 방출 편차가 있다.** 같은 프롬프트가 실행마다 다른 귀결을 낸다(#12 가 sql↔clarification).
러너는 `--repeat` 를 지원하고 편차가 있으면 **가장 나쁜 귀결**로 센다.

### 순서가 곧 SQL 이었는데 그 지식이 주석에만 있었다

빌더 dispatch 는 첫 non-None 승자라 **순서 자체가 의미**다. 그런데 순서 지식은 레지스트리 주석
8줄이 전부였고 **그중 하나는 이미 거짓**이었다("analytical 가장 먼저" — 실제 3번째).

선행 제약 9종을 사유와 함께 `capability_validation.BUILDER_PRECEDENCE` 로 옮기고(축 E),
위상정렬이 현재 순서를 재현하는지 검증한다. 강제 지점은 테스트 **와** import 시점 두 곳이다 —
이 저장소에서 계약 테스트는 두 번(ce39f68, 8ba50b6) 일괄 삭제된 전력이 있다.

### 물리 이름이 네 곳에 살았다

JSON / 코드 미러 / 접근자 인라인 기본값(`config.get("table", "ODS_MALL_OMS_CART")`) /
confidence·member_policy 의 자체 사본.

셋째 층은 미러가 키를 선언하는 한 죽은 코드지만, **미러가 키를 빠뜨리면 조용히 되살아나 구DB
이름으로 컴파일된다** — `base_entity.age_column` 이 정확히 그 상태였다. 미러를
`member_filters_config.CODE_DEFAULTS`(순수 설정 모듈)로 옮기고 인라인 기본값 76건을 걷었다.

**계약은 한 방향뿐이다: JSON ⊇ 미러.** 로더가 최상위 키 단위로 섹션을 통째로 대체하므로 JSON 에
키가 빠지면 미러 값이 폴백되는 게 아니라 `None` 이 된다. 역방향(미러 ⊇ JSON)을 강제하면 방금
걷어낸 이중 소유가 원래 크기로 되살아난다 — 미러는 **파일이 통째로 없을 때 기동만 되게** 하는 것이다.

### 아무도 특성화하지 않았던 관문 두 개

`plan_validation`(전) 과 `sql_guard`(후)는 모든 빌더 산출물이 통과하는데 계약이 없었다.

- **`sql_guard` 는 SQL 을 재작성한다**: 입력은 `sql`, 실행용은 `safe_sql`(행 제한 부착).
  빌더가 만든 문자열이 그대로 나가지 않으므로 회귀 귀속 시 둘을 구분해야 한다.
- **`validate_join_keys(column_types=...)` 의 값은 원시 타입이 아니라 타입군**이다.
  원시 타입을 넣으면 `bigint` vs `int` 가 불일치로 잡힌다(`load_column_types` 가 이미 환산한다).

### preflight 오탐 하나가 게이트를 죽일 뻔했다

canonical 카탈로그를 preflight 범위에 넣자 즉시 FAIL 이 났다 — 그런데 **오탐**이었다.
캠페인 사건은 `time_column="CAMP_SDATE"` 를 선언하지만 실제로는 `time_expression="ZC.CAMP_SDATE"`
(조인된 `Z_CAMPAIGN`)를 쓴다. `*_expression` 이 있으면 나란한 `*_column` 은 이름표다.
**오탐이 나면 사람이 게이트를 끈다 — 정확도가 곧 게이트의 수명이다.**

### BFF 계약

형제 레포 `route.ts` 가 이름으로 읽는 키를 백엔드 테스트로 고정했다. 라벨 키는
`*_condition_labels`(**단수** condition)다 — 복수형으로 바꾸면 조건 목록은 남고 라벨만 조용히 사라진다.

---

## 재현·검증

```bash
python -m pytest -q                          # 1,135 passed
python db_swap_preflight.py                  # PASS (레지스트리 4종)
python tools/physical_binding_inventory.py   # 184
python tools/live_prompt_baseline.py --repeat 2
```

카탈로그를 재생성했다면 재시작만으로는 부족하다 — `docs/operations/catalog_freshness_runbook.md`.

---

# 작업 노트 — 타겟팅 프롬프트 14종 실패 분석·수리 (2026-08-01)

`/target-sql` 로 실패하던 프롬프트 14종의 원인 분석부터 구조화 계층 수리, 검증까지의 기록.
모든 변경은 워킹트리에 있으며 **아직 커밋되지 않았다**.

---

## 1. 무엇을 했나

### 1-1. 원인 분석 (추측이 아니라 실데이터로)

- 로컬 postgres `campaign_query_failure_logs` 에서 실패 11건의 실기록(stage_log·query_plan·빌더 decisions)을 확보하고, 로그에 없던 3건은 로컬 API 재실행으로 재현했다.
- 14건 전부 DB/인프라가 아니라 **SQL 생성 단계의 fail-close 게이트**에서 차단된 것이었고, 원인은 3군집으로 갈렸다:
  - **유형 ① 지원되는데 LLM 구조화기가 못 감** (최대 군집): 슬롯 미방출(cart_aggregate), coarse enum 붕괴(집계→repeat_buyer), 잉여 중복 방출(맞는 집계 옆에 repeat_buyer), 환각(cart_abandoner), 최근접 오배선(캠페인 평균→객단가).
  - **유형 ② 표현할 슬롯이 없음**: 랭킹 슬롯 strict 스키마 미노출, campaign_buy_amount agg 부재(당시), 프로필 지표 슬롯 미배선, lapsed_buyer `_supported:false`, 기간비교(당시).
  - **유형 ③ 이중 해석·순서**: '이십만원'을 상품명으로 재해석, 랭킹 승자 슬롯의 not_found 잔재 미회수, semantic_ir 게이트가 동시구매 백필보다 먼저 실행.
- "게이트가 막았다"는 결과이지 원인이 아니다 — 게이트가 없었으면 대부분 조용히 틀린 SQL 이 나갔을 문장들이다.

### 1-2. 수리 (제안 우선순위 ⓪~⑦ 구현; ⑧~⑪은 장기 과제로 보류)

| 단계 | 내용 |
|---|---|
| ⓪ 재기준화 | HEAD 에서 14건 재실행 — 2번은 이미 성공, 7·9·11 실패 지점 이동 확인. 이후 모든 효과 측정의 기준선 |
| ① 골든 연결 | `tests/golden/cases.json` 의 expect/forbid_slots 를 실제 assertion 으로 — 오프라인(스키마 3계층 분류) + 옵트인 라이브(GOLDEN_LIVE_LLM=1) |
| ②③ 게이트 순서·소유권 | 동시구매 백필 + capability 소유 결핍 sweep + 출력계약 + 정상회원 정책계약을 semantic_ir 게이트 **앞**에 배선. `_has_plan_meaning` 에 condition_evaluations 인정 |
| ④ 스팬 재구성 | `_requirement_span` 에 결정론 4단 재구성(semantic_evidence→literal_bindings→레지스트리 동의어, 유일 후보만) + `span_precision` 필드 |
| ⑤ 리터럴 감사 | 미소비 숫자/기간 리터럴의 **비차단** 자문 `literal_binding_advisories`(단위 환산 포함) |
| ⑥ 파생 라벨 강등 | 집계가 함의하는 잉여 behaviors 를 `claim_slot` 소유권 이동으로 강등(`behavior_demotion.py`) + lapsed_buyer 폴백 컴파일 차단 가드 |
| ⑦ 드리프트 가드 | 골든 계약 테스트에 통합(스키마 노출·metric_id 결속·지향 슬롯 생산자 부재 가드) |

### 1-3. 검증

- 변경 전 615개 → 변경 후 **660개 테스트 전부 통과**(신규 45), 회귀 0.
- 적대적 리뷰 워크플로(29 에이전트, 4관점 리뷰→반증 검증)가 **17건 확정 결함**을 찾았고 전부 반영했다(아래 2-6).
- 최종 14건 재실행: **SQL 출고 1건 → 5건**(2·5·12·13·14). 12번은 실DB 실행(CUSTOMER_COUNT 반환)까지 확인. 13·14의 SQL 은 수동 검토로 의미 정확성 확인(백분율 공식·0→X 증가 해석).

---

## 2. 내린 결정과 이유

1. **로직은 순수 모듈, graph_rag 에는 배선만.**
   graph_rag.py 는 줄수 래칫(상한) 아래 있고, 저장소 관례("동시구매 IR 백필은 condition_evaluation_ir 로 뺐고 …얇은 배선만")를 따랐다. 래칫은 17140→17185 로 상향하고 baseline JSON 의 reason 에 사유를 기재했다(커밋 메시지에도 필요).

2. **강등은 '삭제'가 아니라 '소유권 이동'.**
   잉여 behaviors 제거를 `pop` 이 아니라 `slot_ownership.claim_slot` 으로 — superseded_conditions/decisions 에 근거가 남아 "이 조건이 왜 없나"를 사후 추적할 수 있다. 행동↔지표 등가는 코드가 아니라 설정(`behaviors.*.metric_id`)이 단일 소유한다(이중 소유 재발 방지 — plans_ir_decoupling 의 교훈).

3. **함의 판정은 보수적(fail-close).**
   `>=` 행동은 창이 있어도 함의 성립(부분구간 카운트 ≤ 평생 카운트), `=` 행동(first_purchase)은 **무창일 때만**, anti_join(no_purchase)은 절대 강등 금지(극성 반전 위험), per_member grain 만. 불확실하면 강등하지 않는다 — 남기면 최악이 reject 지만, 잘못 강등하면 조용히 틀린 오디언스가 나간다.

4. **sweep 은 게이트 앞 + 귀속 가드 3종.**
   처음 구현(어휘 토큰만으로 걷기)은 적대 리뷰에서 치명 결함으로 확정됐다 — 다른 절의 진짜 결핍('~했고 지난 시즌에 구매한')까지 삼켜 조용한 lifetime 오답이 가능(실행 재현됨). 최종형: ① 동시구매 어구 밖에 구매동사·엔터티표지가 있으면 sweep 전체 포기, ② 기간 축은 IR 이 time_range 를 실제 보유할 때만 소유('기간 언급됐으나 미파싱'≠'기간 미언급'), ③ 같은 신호의 두 번째 채널(unresolved_source_conditions 의 llm_semantic_ir 행)도 동일 규칙, 걷은 항목은 plan_decisions 에 CLAIM 기록. 잔여 위험(표지 없는 맨 고유명사 인접: '신라면과 같은 상품…')은 9단계 LLM 의미검증이 담당 — docstring 에 명시.

5. **출력 계약·회원 정책도 capability 가 소유.**
   출력계약 생산자가 규칙 철거로 사라져 expected_grain 기본값 'member' 가 '고객수' COUNT 결과를 grain 불일치로 차단했다 → IR 의 final_result(스칼라 카운트)에서 파생한 계약만 결정론 인정(`scalar_count_output_contract`). 정상회원 기본 필터는 계약(member_policy.appliedPolicyFilters) 없이 술어만 넣으면 의미검증기가 spurious 로 오판 → 계약을 기록해 기존 면제 경로를 태웠다.

6. **차단 게이트를 완화하지 않았다.**
   리터럴 감사는 비차단 자문으로만 추가(차단 승격 지점은 invariants 인자 한 곳으로 모아 둠). dropped_signal_warnings 는 이름과 달리 차단 채널임을 확인하고 건드리지 않았다.

7. **스팬 재구성은 유일 후보만 채택.**
   복수 후보(같은 숫자 2회)에서 임의 선택하면 소유권 판정이 오염된다 — condition_evaluation_ir 의 기존 스팬 규칙과 동일. 형제 배열 원소([1]+)가 [0]/무인덱스 증거를 상속하는 것도 금지(리뷰 반영). 리터럴 스팬은 본문 일치까지 요구(재작성 프롬프트 좌표계 오염 방지).

8. **골든 라이브 테스트는 이중 옵트인**(GOLDEN_LIVE_LLM=1 + OPENAI_API_KEY).
   CI 는 키를 주입하지 않는 정책이라 skipif 로 항상 스킵되고, 개발자 셸에 키가 떠 있는 것만으로는 발화하지 않는다. 구조화 폴백(intent=unknown)은 채점하지 않고 스킵 — 네트워크 장애가 의미 회귀로 위장되는 것을 막는다.

9. **골든 기대 슬롯의 3+1 계층 분류.**
   expect_slots 를 LLM 노출/앱 소유/컴파일러 파생으로 나누고, 생산자가 실제로 없는 event_expression·canonical_targeting_expression 은 '지향(aspirational)' 계층으로 정직하게 분리 — 생산자가 생기면 가드 테스트가 빨개져 승격을 강제한다(공허 통과 방지, 리뷰 반영).

---

## 3. 수정한 파일

### 소스 (7)

| 파일 | 변경 |
|---|---|
| `graph_rag.py` | 배선만(+46줄): 게이트 앞 백필·sweep·출력계약·정책계약(9880대), behaviors 강등 호출(9900대), 리터럴 감사 생산·응답 키 2곳, order_count 빌더 미지원 규칙 가드, import 2줄 |
| `condition_evaluation_ir.py` | `drop_capability_owned_missing_fields`(귀속 가드 3종+이중 채널+CLAIM 감사), `scalar_count_output_contract`, 소유 어휘·정규화 헬퍼 |
| `behavior_demotion.py` **(신규)** | 집계 함의 잉여 행동 강등(순수 모듈, claim_slot 기록) |
| `semantic_requirements.py` | `_requirement_span` 4단 재구성+`span_precision` 필드, `unconsumed_literal_advisories`(+단위 환산), member_filters_config import |
| `member_filters_config.py` | `behavior_aggregate_equivalents()`, `order_count_rule_supported()` |
| `query_structurer/semantic_ir.py` | `_has_plan_meaning` 에 condition_evaluations 추가 |
| `docs/data/runtime/sql/member_target_filters.json` | behaviors 에 `metric_id` 등가 선언(first_purchase/repeat_buyer) + 섹션 주석 |

### 테스트·베이스라인 (7)

| 파일 | 내용 |
|---|---|
| `tests/test_golden_slot_schema_contract.py` **(신규)** | 골든 expect/forbid ↔ V4 스키마 3+1계층 계약, 지향 슬롯 생산자 부재 가드 |
| `tests/test_golden_live_structuring.py` **(신규)** | 옵트인 라이브 구조화 채점(19케이스) |
| `tests/test_semantic_ir_owned_missing_fields.py` **(신규)** | sweep 계약+귀속 가드 4종+이중 채널+CLAIM 감사+배선 순서 소스 가드 |
| `tests/test_behavior_demotion.py` **(신규)** | 강등 함의 규칙 전수+metric_id 결속 가드+빌더 미지원 규칙 가드 |
| `tests/test_requirement_span_reconstruction.py` **(신규)** | 4단 재구성·유일성·형제 오귀속 금지·digest 왕복 |
| `tests/test_literal_binding_advisories.py` **(신규)** | 소비 3축+단위 환산+comparison 제외 |
| `docs/data/test_baselines/module_size_baseline.json` | graph_rag 상한 17140→17185 + reason 갱신 |

---

## 4. 결과 (14건 전후)

| # | 프롬프트 요지 | HEAD 기준선 | 최종 |
|---|---|---|---|
| 1 | 1년 특정 브랜드 2회 | conditions_missing | 차단(의미검증) — '특정 브랜드'는 플레이스홀더라 확인 질문이 정답 |
| 2 | 6개월 5건+50만 | ✅ SQL | ✅ SQL |
| 3 | 3개월 有+30일 無 | 차단 | 차단(lapsed_buyer 미지원 — 더 이른 단계에서 정직하게) |
| 4 | 누적 상위 10% | internal_invalid | 차단(방출 실패 계열) |
| 5 | 카트 종류2+10만 | 차단 | ✅ **SQL** |
| 6·7·8 | 캠페인 빈도/평균/구매주기 | 차단 | 차단(방출 실패·슬롯 미배선 계열) |
| 9 | 이십만원, 남자 제외 | 차단 | 차단(clarification) |
| 10·11 | 최다판매 상품 구매 고객 | internal_invalid/차단 | 차단(랭킹→회원 합성 미완) |
| 12 | 동시구매 고객수 | grain_mismatch | ✅ **SQL + 실DB 실행** |
| 13 | 2월↔3월 10%↑ | 차단 | ✅ **SQL**(백분율 공식 검토 완료) |
| 14 | 2월↔3월 증가 | 차단 | ✅ **SQL** |

⚠ 13·14는 LLM 방출 편차로 런마다 흔들릴 수 있다(기간비교 컴파일 경로 자체는 존재 확인 — '기간비교=미지원'이라는 과거 지식은 폐기).

---

## 5. 남은 할 일

### 단기
- [ ] **커밋 분리**: 소스/설정/테스트를 논리 단위로 커밋. graph_rag 래칫 상향(17140→17185) 사유를 커밋 메시지에 기재(래칫 파일 자체 요구사항).
- [ ] 13·14 **재현성 확인**: 같은 프롬프트 수 회 반복 실행해 방출 편차 정량화(실패 로그로 추적 가능).
- [ ] `graph_rag.py:9978` 부근 `query_plan["output_contract"]` 직접 키 접근 — 계약 부재 시 잠재 KeyError(단락 평가로 가려져 있음). 방어 접근으로 정리.
- [ ] 리뷰 확정 경미 결함 중 미반영분: 리터럴 감사의 우연 값 충돌 오억제('30세'와 '30일')은 알려진 잡음으로 문서화만 됨 — span_precision 보급이 늘면 스팬 축으로 자연 개선.

### 중기 (효과 큰 순서)
- [ ] **⑨ 구조화기 모델 A/B 실험**: 남은 실패 다수가 '지원되는데 미방출'(gpt-5-mini). 골든 19 + 실패 14 코퍼스로 상위 모델 비교 — `GOLDEN_LIVE_LLM=1` 라이브 테스트가 측정 도구. 결과가 Canonical IR 투자 규모를 결정한다.
- [ ] **⑧ patch retry**: 현행은 스키마 실패 시 전문 재생성·첫 통과 승자·의미 점수 없음(`query_structurer/structurer.py`). 의미 보존 패치 방식으로 — 점수는 LLM 심판이 아니라 requirement 커버리지 델타(결정론)로.
- [ ] 6·7·8 방출 실패 수리: campaign_response_frequency/campaign_buy_amount(AVG)/buy_cycle 슬롯은 이미 존재·노출 — 프롬프트 가이던스 보강 또는 모델 교체로 해결될 가능성(⑨ 결과에 따라).
- [ ] 10·11 랭킹→회원 합성: entity_set 경로의 죽은 배선(parse_entity_set_condition 호출자 0) 복원 또는 analytical 라우트에 회원 필터 합성 추가.

### 장기

> **2026-08-01: 실행 플랜 수립 완료 — `docs/plans_canonical_ir_capability.md`.** 현황 조사에서 아래 서술의
> 전제 일부가 사실과 다름이 확인됐다(logical_expression.py 는 존재하지 않는 유령 참조, capability_registry.py 는
> ac924ff 에서 이미 삭제됨, 진짜 씨앗은 targeting_expression 의 타입드 트리). 정정 내역과 Phase 0(잔해 정리)→
> A(⑪)→B0(경계 정리)→B1(⑩ 본체) 순서·게이트는 플랜 문서가 권위다.
>
> **2026-08-02: 리뷰 반영 개정 + Phase 0·A 구현 완료.** 테스트 660→680 passed, preflight PASS.
> ⑪ 은 사실상 완료(노출면 파생·라벨/각주 facet·validator 4축·표 자동 생성), 장기 3번(문서 동기화)은
> `docs/generated/supported_conditions.md` 자동 생성으로 구조 해소. 상세는 플랜 문서 '진행 상태' 표.

- [ ] **⑩ Canonical IR 이행**: 신규 설계가 아니라 기존 씨앗(`condition_evaluation_ir` 의 "검증된 구성 서명만 허용", ~~`logical_expression.py`~~(부존재 — 플랜 §0-1), `targeting_expression.py`)의 일반화로. adapter 경계는 단방향 + "adapter 이후 의미 추가 금지" freeze 계약 필수(인플레이스 변형이 함정). `docs/plans_ir_decoupling.md` 5웨이브와 정합 확인. → 플랜 Phase B
- [ ] **⑪ Capability Registry 단일 권위**: 지원 목록→LLM 스키마→validator→compiler dispatch→테스트를 한 곳에서 생성. 이번에 넣은 드리프트 가드들이 그때까지의 임시 결속. → 플랜 Phase A(씨앗은 `targeting_ir.CONDITION_SPECS` facet 확장, 신규 파일·JSON 외부화 아님)
- [ ] 기간비교·동시구매처럼 "미지원인 줄 알았는데 경로가 생긴" 항목들의 문서/기억 동기화(~~운영 문서의 지원 조건 표 갱신~~ — 갱신할 표가 저장소에 없음이 확인됨. 손 갱신 대신 플랜 A-4 의 레지스트리 파생 자동 생성 표로 해소).

---

## 6. 재현·검증 방법

```bash
# 전체 테스트 (로컬 파이썬, ~14초)
python -m pytest tests -q

# 라이브 골든 채점 (옵트인, OPENAI 키 필요)
GOLDEN_LIVE_LLM=1 python -m pytest tests/test_golden_live_structuring.py -q

# 14건 배치 재실행 (api 컨테이너 기동 상태에서)
# 스크립트: 세션 스크래치패드 run_baseline.py 참조 — /target-sql 에
# execute_sql=false, persist_targeting=false 로 POST

# 실패 원인 조회 (실기록이 1차 소스)
docker exec recommendation-campaign-system-python-postgres-1 \
  psql -U postgres -d campaign_db \
  -c "SELECT created_at, failure_reason, left(prompt,40) FROM campaign_query_failure_logs ORDER BY created_at DESC LIMIT 20;"
```

코드 반영은 api 컨테이너 재시작으로 충분하다(볼륨 마운트): `docker restart recommendation-campaign-system-python-api-1`

---

# 작업 노트 — 타겟팅 프롬프트 26종 실패 분석·수리 (2026-08-02)

26종 사용자 프롬프트 전수 감사(최초 4/26 성공, 가짜 성공 2 포함) 후 5단계 수리. 위 14종 작업의 후속이다.
변경은 워킹트리에 있으며 아직 커밋되지 않았다. 회귀 코퍼스·응답 전문은 세션 스크래치패드에 있음.

## 무엇을 했나 (Phase 1~5)

1. **실패의 정직화**: `plan_validation_internal_invalid` 무언 실패 제거(이슈별 한국어 사유, `failure_messages.py`),
   semantic_ir 범용 문구→필드 라벨화, `_RECURRENCE_RE`가 '구매주기' 안의 '매주'를 오탐하던 결함 수정(한글 lookbehind).
2. **등급/상태 시점·이력 축 신설**: `compositional_targeting` 유령 부활 — `attribute_catalog.json`(물리 바인딩; 값 사전은
   eq_filters 참조) + 결정론 감지기 10패턴 + 리졸버(지원 경계=카탈로그 선언) + as_of/transition 스냅샷 SQL.
   실측: `CRM_MB_MONTHCRMINFO`는 201701 단일 월 + `PREV_*` 직전값 → 전이/기준월은 지원, 다월 연산(내내 유지/N회 변경
   /모든 월 존재)은 적재 현황 명시 미지원(`snapshot_months_available` 숫자만 올리면 열림). `member_state`(정상/휴면)
   이력은 소스 부재 명시(STATE_GRADE는 활동등급 — MEMBER_STATE_CD와 무상관, 실측). `member_attribute_history.py`가
   오케스트레이션+소유권 sweep. 시간 한정어는 `member_state_history` 의무로 원장에 기록(가짜 성공 구조 차단),
   영수증 발급은 컴파일 분기 안에서만(plans_ir_decoupling W5-4의 '첫 컴파일러' 경로).
3. **방출 실패 봉합 3종 표준**: ① 레지스트리 파생 결정론 백필(fill-if-empty) — 프로필 지표(`metric_registry`),
   캠페인 횟수/금액(`campaign_condition_backfill`), 카트 집계·기간 대 기간·회원 지표 랭킹(`numeric_condition_backfill`),
   랭킹→회원(`parse_entity_set_condition` 부활 배선) ② 표적 재방출 1회(`slot_reemission` — 미귀결 라벨을 힌트로 보완
   제출, coerce 통과분만 병합) ③ 소유권 sweep — 컴파일된 IR이 semantic_ir.missing_fields 와 llm_semantic_ir 자유문장
   행 **두 채널**의 stale 결핍 보고를 회수(`_unresolved_source_condition_is_deterministically_resolved` 확장).
4. **오배선 결정론 교정**: `no_*` 캠페인 canonical 은 항상 negated(구매반응 없음 반전 실사고), 근거 없는
   cart_abandoner 환각 강등, lapsed 문형('주문 있었지만 최근 무구매')을 purchase_membership+purchase_inactivity 합성으로
   정규화(`behavior_demotion`), '회원 수' 결정론 분석 의도 채택+출력 계약 승격(진짜 COUNT SQL), 조작된 캠페인 구성
   필드 요구(채널/혜택/목적) 행 stale 판정.
5. **플레이스홀더 조기 질문**: '특정 브랜드'는 파싱 직후 "브랜드 이름을 지정해 주세요"로 즉시 질문(재방출 제외).

## 결과 (26종 라이브)

- 성공(개별 검증 기준): #2 #4 #5 #6 #7 #8 #9 #10 #11 #12 #13 #14 #15 #19 — 컴파일러·백필·sweep 결합으로 안정화.
  (#3 #8은 LLM 방출 편차 잔존 — ⑨ 모델 A/B 실험 대상; #3용 랭킹 백필은 member_metrics.json synonyms 추가로 배선)
- 정직한 명시 미지원(메시지+개방 조건): 다월 등급 연산 #17 #20 #22 #24 #25 #26, 상태 이력 #16 #18 #21 #23.
- 의도된 조기 질문: #1(특정 브랜드).
- pytest 734 passed(신규 계약 테스트 5파일), preflight PASS, 지원 조건 표 재생성.
- graph_rag 래칫 17185→17335(사유는 baseline reason에 기록 — 로직은 전부 소유 모듈로, graph_rag엔 얇은 배선만).

## 함정 기록

- LLM 결핍 보고는 **두 채널**(semantic_ir.missing_fields + llm_semantic_ir 자유문장 행)이라 sweep 은 양쪽을 함께
  걷어야 한다. 한쪽만 걷으면 다른 채널이 그대로 차단한다(동시구매 docstring의 교훈이 세 번째로 재확인됨).
- llm_semantic_ir 행의 condition 은 원문 전체가 실리곤 한다 — stale 판정은 reason 텍스트로 해야 한다.
- api 컨테이너의 LLM 로그는 요청별 파일 `logs/rag_llm/<date>/<HHMMSS-hash>.jsonl`이다(날짜 파일은 스코프 밖 기록).

## 적대적 리뷰(28 에이전트) 후속 — 반영 및 잔여

- 반영(즉시 수리): entity_set surface 항진 stale 판정→절 경계 비교, member_state_history 의무 값-인접
  마커화(구매 절 과발화 제거), as-of 값 앵커 인접 선택, 전이 제외 문맥 스킵, as_of_month 단일 스냅샷
  가용성 게이트, kind 일괄 영수증→단일 절 가드+월 반복 의무 절 문맥 필터, 캠페인 백필 최장 동의어
  승자+비캠페인 짧은 일반어 가드+AVG 어순, entity_set 기간 스윕 롤링 창 인지, 재방출 후 이력 리졸버
  재실행, plan_schema 에 relational_operations/relational_ir 등재.
- 잔여(후속 과제):
  1. attribute_catalog.json 이 db_swap_preflight 대상 밖 — 바인딩 컬럼·snapshot_months_available 을
     live DB 와 대조하는 섹션 추가 필요(적재 확장 시 숫자 갱신을 잊으면 다월 연산이 계속 미지원으로 남음).
  2. relational_ir_unsupported 가 unsupported_reasons 닫힌 집합·failure_stage UI 계약 밖(기존 경로가
     이번에 실활성화됨) — 닫힌 집합 등재는 생산 리터럴 규약과 함께 정리 필요.
  3. 등급 값 어휘(VIP/골드/…)가 member_attribute_history·semantic_requirements·compositional 3곳
     정규식에 재등장 — eq_filters 파생으로 통합하고 드리프트 가드 테스트 추가.

---

# 작업 노트 — 의미 해석과 query plan 생성의 분리 (SemanticPlanV2, 2026-08-02)

26종 감사 수리(위 절)에서 표준으로 삼았던 "결정론 백필 3종"을 **일반화하지 않고 삭제**하고,
의미의 소유자를 타입드 중간 표현으로 옮겼다. 위 절의 봉합책이 이 절에서 철거된다.

## 왜 (문제의 구조)

같은 원문을 세 번 해석하고 있었다.

```
원문 ─┬─ LLM        → query_plan 슬롯 + semantic_ir.missing_fields + status
      ├─ 정규식 백필 → 같은 문장을 다시 읽어 빈 슬롯을 fill-if-empty
      └─ sweep      → 앞 단계가 만든 missing_fields 를 사후 삭제
```

의미의 소유자가 없으니 새 조건마다 `_apply_*_backfill` 하나와 `_drop_*_missing_fields`
하나가 늘었고, 어느 해석이 이겼는지는 **코드 순서에 숨었다**.

## 목표 구조

```
원문 → SemanticPlanV2 추출(LLM) → 값 정규화 → coverage 검증(+누락 구간만 재추출)
     → capability 판정 → 검증 → 결정론 컴파일 → 기존 query_plan
```

- **missing 은 계산값**: `required_fields(node) - populated_fields(node)`.
- **status 는 파생값**: `derive_status(missing, unsupported, validation_errors, conflicts, uncovered)`.
- **슬롯의 생산자는 하나**: `LegacyQueryPlanCompiler`. LLM 노출면에서 해당 슬롯을 뺐다.
- 원문을 다시 읽는 곳은 **coverage 검증 하나**이고, 그 산출물은 슬롯이 아니라 재추출 요청이다.

## 삭제한 것 (원문 재해석 계층)

| 기존 | 대체 |
|---|---|
| `numeric_condition_backfill.apply` / `_fill` / `_drop_trend_owned_missing_fields` | AggregatePredicate / MetricComparison / RankedSet |
| `campaign_condition_backfill.apply` | AggregatePredicate(scope=campaign) |
| `metric_registry.apply_profile_condition_backfill` / `detect_profile_conditions` | Predicate(프로필 지표·날짜 상태) |
| `graph_rag._apply_entity_set_backfill` / `entity_set.parse_entity_set_condition` / `drop_entity_set_owned_missing_fields` | EntitySetMembership |
| `compositional_targeting.detect_member_attribute_history` / `apply_member_attribute_history_backfill` / `_drop_history_owned_missing_fields` | RelationPredicate(as_of/transition/…) |
| `condition_evaluation_ir.apply_same_product_co_purchase_backfill` / `drop_capability_owned_missing_fields` / 감지 정규식 | RelationPredicate(co_purchase) |
| `slot_reemission.attempt` | coverage 재추출(누락 구간 한정) |
| `query_structurer.semantic_ir.drop_satisfied_missing_fields` / `materialize_semantic_operations` | 파생 `project_semantic_ir` |
| `campaign_plan_v4._drop_campaign_constraint_requirements` | (불필요 — 캠페인 필드는 노드 필드가 아니다) |
| `graph_rag._drop_fabricated_purchase_period_fields` | (불필요 — 기간은 노드 소유) |

## 신규 모듈

`semantic_plan`(노드·스키마·status 파생) / `semantic_normalizers`(값 정규화) /
`semantic_capability`(축별 판정 + 실패 분류) / `semantic_coverage`(원문 대조) /
`semantic_candidates`(후보 병합·충돌) / `semantic_plan_llm`(LLM 계약) /
`semantic_pipeline`(단계 조립) / `legacy_plan_compiler`(슬롯 지식 단일 소유자) /
`semantic_plan_bridge`(graph_rag 배선).

## 검증

- pytest **763 passed / 19 skipped**(기준선 744), preflight PASS, 지원 조건 표 재생성.
- 코드베이스 전수 검사: 원문 정규식→슬롯 대입 0건, fill-if-empty 백필 호출 0건,
  missing_fields 사후 삭제 0건, 삭제 대상 함수 정의 0건(`tests/test_single_interpretation_path.py` 상시 가드).
- graph_rag.py 17,335 → 17,322줄(순감). 신규 로직은 전부 소유 모듈.

## 남은 원문 해석기(이번 범위 밖 — 다음 이행 대상)

`analyze_analytical_intent`(집계 의도), `behavior_demotion.*`(행동 라벨 강등),
`active_member_filter`(회원 정책), V4 슬롯 계층의 coarse 축(성별/연령/행동/캠페인 제약).
이들은 슬롯 백필이 아니라 각자 다른 축의 생산자라 같은 이행을 한 번 더 해야 한다 —
노드 타입 추가 + capability 선언 + 컴파일러 매핑 3줄이면 되도록 확장 지점은 열려 있다.

---

# 작업 노트 — 이탈 위험 문형(존재 창 + 부재 창) 수리 (2026-08-02)

대상 프롬프트: "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원을 추출해서 이탈방지 캠페인을 만들어줘."
라이브 실측 **1/5 → 8/8**(같은 프롬프트·같은 모델·`query_parser=auto`).

## 왜 1/5 였나 — 그리고 그 1이 왜 더 나쁜 신호였나

성공한 1회는 LLM 이 노드를 **더 못 만들어서** 통과한 것이었다: 타입 확정 단계에서 폐기(unresolved) →
슬롯 청구로 소거 → 결정론 합성이 SQL 을 만듦. 반대로 LLM 이 노드를 제대로 만들면 컴파일러까지 도달해
하드 에러가 됐다. 판정이 뒤집혀 있었다는 뜻이고, 그래서 케이스별 미봉으로는 닫히지 않았다.

원인 4종(전부 실측 근거 있음):

| | 증상 | 진앙 |
|---|---|---|
| C1 | 존재 표현(`order_count > 0`)이 **착지할 슬롯이 없어** 임계 슬롯의 양수 도메인에 걸림. 게다가 사유가 "지표가 실행 어휘에 없다"로 오보고(어휘엔 **있었다**) | `targeting_ir._pos_number` / `_coerce_threshold_list` / 컴파일러의 사유 추측 |
| C2 | 이 문형 전용 결정론 구제가 fail-close 게이트보다 **40줄 뒤**라 영영 도달 불가 | `graph_rag.build_sql_result` 호출 순서 |
| C3 | 절대 기간 분기가 set-then-pop **no-op** — '최근 3개월'이 무음 드롭돼 평생 집계가 됨 | `legacy_plan_compiler._compile_order_aggregate` |
| C4 | 자식 하나짜리 `logical_expression` 이 두 절 전체를 커버로 청구 → 표현되지 않은 절이 조용히 사라짐 | `semantic_coverage._node_spans` |
| C5 | (수리 중 새로 드러남) 캠페인 **이름**('이탈방지')에서 `exclude.lifecycle=[dormant_user]` 환각 | V4 구조화기 |

## 어떻게 고쳤나 (케이스가 아니라 축)

1. **카운트 대수**(`semantic_normalizers.CountThresholdNormalizer`) — (연산자, 임계값)을 정수 카운트 위에서
   항진식/존재/부재/모순/임계로 분류하는 **순수 대수**. 어구 목록이 아니라 값으로 갈리므로 새 지표가 늘어도
   손댈 곳이 없다. 컴파일러가 존재→`purchase_membership`, 부재(창 있음)→`purchase_inactivity` 로 환원한다.
   - 환원 자격은 설정이 선언한다: `semantic_type ∈ {count, count_distinct}` **그리고** `null_as == 0`.
     후자가 없으면 안 된다 — 브랜드가 빈 주문만 가진 회원은 `distinct_brand_count = 0` 인데 구매는 했다.
2. **`purchase_membership` 슬롯 신설** — 실행 술어·커버리지·신뢰도는 이미 있었고 **슬롯 선언만** 없었다.
   `SLOT_SHAPES` + `CONDITION_SPECS`(fact_join=False) + `SLOT_KO_LABELS` 한 줄씩.
   `NODE_SLOT_MAP` 에는 싣되 `COMPILER_OWNED_SLOTS` 에서는 뺀다(`SHARED_REDUCTION_SLOTS`) — '노출 XOR 컴파일러
   소유' 계약을 지키면서 매핑표가 거짓말하지 않게. 이 둘을 동시에 만족시키는 유일한 길이었다.
3. **거부 사유의 단일 소스**(`SlotShape.reject`) — coerce 와 사유가 **같은 함수**를 쓴다. 두 벌로 나누면 곧
   어긋나고, 그때는 틀린 사유가 사유 없음보다 나쁘다.
4. **순서 역전 수리** — 결정론 구제를 의미 파이프라인 **앞**으로. 슬롯 청구가 소유 판정의 입력이 되어야
   같은 어구를 다시 방출한 노드가 '유실'이 아니라 '중복'으로 판정된다. + 컴파일 실패에도 슬롯 소유 소거를
   적용(`supersede_slot_owned_failures`) — `semantic_ir` 를 고치는 게 아니라 **파생 입력**을 줄이고 다시 계산한다.
5. **기간 정직화** — 절대 구간은 오늘로 끝나는 후행 창일 때만 일수로 환산, 아니면 fail-close. 창이 사라진
   집계는 '평생'이 되어 원문과 다른 대상을 낸다.
6. **커버리지 정직화** — 컨테이너 노드는 자식이 덮지 않는 구간을 청구하지 못한다. 그리고 기간 소유 노드는
   자기 절의 기간 원자를 **하나만** 청구한다(절 전체 면제는 다른 조건의 기간까지 덮었다).
7. **`duration` 리터럴 신설** — '6개월'의 '6' 이 주인 없는 맨 숫자로 남던 것을 타입 있는 원자로. 이게 없으면
   ⑥ 을 고친 순간 '최근 6개월 5건 이상'이 0/5 로 무너진다(실제로 무너뜨렸다가 되살렸다).
8. **환각 제외 강등** — 원문에 제외 표지가 없으면 `exclude.*` 는 근거 없는 조건이다. 조건의 부정('구매가 없는')은
   표지가 아니다.

## 함정 기록

- **순서 이동만으로는 아무것도 고쳐지지 않는다.** `semantic_ir.status` 는 파이프라인이 계산한 파생값이라,
  구제를 앞으로 옮겨도 게이트는 낡은 unsupported 를 그대로 본다. 소거 경로가 반드시 함께 필요하다.
- **`COMPILER_OWNED_SLOTS` 에 슬롯을 더하는 것은 4개 가드를 동시에 건드린다**(결정론 백필 금지·
  FORBIDDEN_OUTPUT_KEYS·V4 노출 제거·semantic_plan 스키마 부재). target_user 슬롯에는 plan 컨테이너 같은
  '노출 제외 선언' 장치가 없어서 '노출'과 '컴파일러 소유'가 배타다.
- **커버리지를 정직하게 만들면 그동안 거짓 커버가 가려 준 결핍이 드러난다.** ⑥ 직후 무관한 프롬프트가
  0/5 로 무너졌고, 원인은 상대 기간의 수가 주인 없는 원자였다는 **원래부터 있던** 구멍이었다.
- 인접 프롬프트 회귀는 **반드시 `git stash` 로 기준선을 재서** 판정한다. 이번에도 "내가 깼나?" 두 건 중
  하나는 진짜 회귀(고침), 하나는 원래부터 실패(90일 미접속)였다.

## 검증

```
python -m pytest tests -q          # 901 passed, 20 skipped
python db_swap_preflight.py        # PASS
python tools/generate_supported_conditions.py   # 문서 재생성(커밋 필수 — CI 가 git diff 로 잡는다)
```
라이브: 대상 프롬프트 8/8, 인접 5종(임계·카트·절대기간·평생무구매·제외) 회귀 없음.
신규 테스트: `tests/test_lapsed_buyer_regression.py`(C1~C4 축별 고정),
`tests/test_deterministic_rescue_ordering.py`(순서 불변식), 골든 케이스 `lapsed_buyer_window_pair`.

## 남은 것 (이번 범위 밖, 실측으로 확인됨)

- "이십만원 이상 구매한 회원 중 남자는 제외해줘" — 금액 조건이 SQL 에 반영되지 않아 의미검증에서 차단.
  **수정 전후 동일**(clean tree 에서도 실패). 26종 감사 #8 과 같은 건.
- "90일 이상 접속하지 않은 회원" — 구조화기가 `buy_cycle`(평균 구매주기)로 환각. **수정 전후 동일 0/4**.
  이번 변경으로 사유만 정직해졌다("임계값 0 는 이 슬롯이 받지 않는다").
- 대상 프롬프트의 결과는 여전히 **0건**이다 — 실데이터가 2017년인데 창은 오늘 기준. SQL·실행 모두 정상.
- `intent=find_user_segment` 라 메시지 생성은 `intent_not_recommend_campaign` 으로 스킵된다(별건).
- 이탈 문형 + 집계("…회원 수를 알려줘")는 여전히 `query_plan_conditions_missing` 으로 막힌다.
  26종 감사 #14 와 같은 analytic 라우팅 군집이고 이번 범위 밖이다.

## 적대적 리뷰(6렌즈) 반영

리뷰가 낸 결함 중 **직접 재현한 것만** 고쳤다. 전부 이번 변경이 만든 것이다.

| 결함 | 왜 위험한가 | 수리 |
|---|---|---|
| 존재 환원이 브랜드 한정어·grain 을 버림 | 실패가 아니라 **조용히 넓어진다**('나이키 1번 이상' → '구매한 적 있음') | 한정어 있는 노드는 환원하지 않고 임계 경로에 남긴다(`_has_narrowing_qualifier`) |
| `!=` 가 부분 문자열 폴백으로 `=` 가 됨 | `order_count != 0`(주문 있음)이 **부재**로 분류 — 극성 반전 | 분류기 전용 **엄격** 연산자 표. 모르는 표기는 환원하지 않는다 |
| 두 존재 노드가 서로 덮어씀 | **노드 순서가 곧 대상**이 되고 근거가 안 남음 | 값이 다르면 충돌로 보고하고 막는다 |
| 이탈 정규식 청구가 매치 전체 | 두 절 사이에 낀 무관한 조건("서울에 사는 30대 중")까지 삼킴 | 절별 named group 으로 **각 절 구간만** 청구 |
| 기간 원자 1:1 배정 | 기간을 둘 소유하는 `metric_comparison`(baseline·current)이 통째로 막힘 | 노드가 **채운 기간 필드 수만큼** 청구 |
| `inf` 임계값 | `OverflowError` 가 노드별 예외 처리를 빠져나가 파이프라인 전체를 죽임 | 비유한값은 환원하지 않는다 |

### 되돌린 것 — 환각 제외 강등(`demote_unevidenced_exclusions`)

계획 밖으로 추가했다가 **삭제했다.** 리뷰가 셋을 짚었고 전부 맞다: (a) 제외 표지 목록이 자연스러운
한국어 표현을 다 못 담아 **진짜 사용자 조건을 지운다**, (b) 지운 사실이 응답에 안 보인다,
(c) 공백 제거 텍스트에 `in` 이라 '빼빼로'·'해외의' 같은 낱말이 근거로 통과한다.

올바른 판정 기준은 부정 어휘가 아니라 **값의 표면 근거**여야 한다(제외된 값의 이름이 원문에 있는가 —
'dormant_user' 면 '휴면'이 있는가). 그러려면 canonical→한국어 표면 지도가 필요한데 지금 없다.
없는 채로 근사하면 사용자 조건을 지우므로, 정직한 실패를 남기는 편을 택했다 — 이 프롬프트는 그 없이도
6/6 이고, 환각이 나오면 "생애주기 제외 조건을 SQL 에 반영하지 못했습니다"로 **이름을 대며** 막힌다.

### 반영하지 않은 것

`_partition_by_slot_ownership`(타입 확정 실패 경로)의 빈 앵커 구멍은 **이번 변경 이전부터** 있던 것이다.
컴파일 실패 경로에는 같은 구멍을 막았지만(`require_anchor=True`), 재확정 경로에 같은 엄격함을 넣으면
지금 통과하는 프롬프트가 다수 막힌다 — 별도 측정이 필요하다.

## 의도한 상호작용 하나 (측정해 둠)

구제를 앞으로 옮기면서 `_normalize_purchase_aggregation_request`(graph_rag:3908)가 **결정론으로 합성된**
`purchase_membership` 을 보게 됐다. 실측: 집계 필터의 `ORDER_DATE >= P30D` 가 `P90D` 로 재작성된다.
이것은 그 함수의 선언된 계약 그대로다 — docstring 이 "구매 존재 조건의 window_days 를 단일 진실 소스로
사용한다"고 말한다. 이탈 문형에서 모집단의 **긍정** 창은 90일이 맞고(30일은 부재 술어로 따로 붙는다),
재작성 전의 P30D 는 오히려 요청의 반대("최근 30일에 주문한")를 세고 있었다. 다만 라이브에서는
`contractSource == analytics_registry` 조기 반환에 걸려 재작성이 발동하지 않는다.
