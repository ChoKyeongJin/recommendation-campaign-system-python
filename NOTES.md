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
> A(⑪)→B(⑩) 순서·게이트는 플랜 문서가 권위다.

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
