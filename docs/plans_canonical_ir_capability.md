# 장기 과제 실행 플랜 — Canonical IR 이행(⑩) · Capability 단일 권위(⑪) · 지원 문서 동기화

## 진행 상태 (2026-08-02 갱신)

**Phase 0 + Phase A 구현 완료. Phase B0 은 게이트 충족 시 착수 가능, B1 은 결정 묶음 대기.**
테스트 660 → **680 passed**(신규 계약 20), preflight PASS(capability 경량 축 포함).

| 단계 | 상태 | 핵심 결과 |
|---|---|---|
| Phase 0 | 완료 | logical_expression 잔해 8지점 제거(BFF 소비 0건 확인), 죽은 참조 정정, doc_claims 에 docs/data JSON 축(.py 한정) 추가 — build_rag_knowledge·init_rag_collections 의 삭제된 기본 경로 잠복 결함 발견·표지 |
| A-1 | 완료 | V4 target_user 노출면 SLOT_SHAPES 파생 전환 — tool 스키마 **바이트 동일**(82,242B) + 파생 계약 3종. plan 슬롯은 제외 사유 선언 dict + '노출 ∨ 선언된 제외' 계약 |
| A-2 | 완료 | `targeting_ir.SLOT_KO_LABELS`(23슬롯, 키 동등 assert)·`SLOT_SUPPORT_NOTES` facet 신설, graph_rag 라벨 42종 → 잔여 22 + 파생 20(값 동일 검증), 힌트 폴백 파생화 + stale 7항목 삭제, JSON 힌트 lapsed_buyer 모순 해소 |
| A-3 | 완료 | `capability_validation.py` 신규(4축: 라벨/노출/빌더 소유권 1:1+선언 예외/base×qualifier) + tests/test_capability_contract.py(역검증 5종) + preflight 경량 배선 + 응답 `capability_check` 를 검증 요약으로 실생산(plan 무변형) |
| A-4 | 완료 | `docs/generated/supported_conditions.md` 자동 생성(tools/generate_supported_conditions.py) + 최신성 테스트 — 기간비교·동시구매·lapsed_buyer 조건부 지원 명시(장기 3번 해소) |
| A-5/A-6 | 완료(1건 보류) | requirement_capabilities 축소 명시(Phase 0-3), 가드 문구 전환. resolves_unsupported 삭제는 보류(살아 있는 배선 발견 — 본문 정정 참조) |
| B0 (B-1~B-3) | 미착수 | 게이트: 응답 계약 green(현재 green) + Phase 0 완료(충족) — 착수 가능 상태 |
| B1 (B-4~B-5) | 미착수 | 결정 묶음(⑨·⑧·shadow divergence·이중해석 잔존·래칫) 대기 |

> 생성 2026-08-01. 근거: NOTES.md 장기 과제 3항목 + 현황 조사 2건(IR 씨앗 모듈 전수 / capability 권위 산개 전수)
> + `docs/plans_ir_decoupling.md` 정합 검토. 조사에서 NOTES 의 전제 일부가 사실과 다름이 확인되어,
> 이 문서는 **전제 정정부터** 시작한다.
>
> **개정 2026-08-01(리뷰 반영):** ① Phase B 를 B0(경계 정리, 모델 실험 무관)/B1(IR 본체)로 분리하고
> B1 게이트를 ⑨ 단독에서 **결정 묶음**으로 교체 ② Phase 0 에 외부(BFF) 소비 특성화 선행 추가
> ③ 바이트 동일 검증은 마이그레이션 한정임을 명시 ④ A-2 에 kind 등가 별칭 facet 추가(표면 동의어 제외)
> ⑤ B-3 에 스키마 압력 테스트·완료 판정 추가. 빌더 1:1 소유는 완화하지 않는다(선언된 예외로 관리 —
> dispatch 가 우선순위 튜플+첫 non-None 승자라 다중 소유 허용 시 튜플 순서가 의미가 되는 것이 근거).

---

## 0. 전제 정정 — 조사로 뒤집힌 사실

장기 과제 서술(NOTES.md:133-135)이 딛고 있던 전제 중 다음이 현재 코드와 다르다. 전부 직접 검증됨.

1. **`logical_expression.py` 는 존재하지 않는다** (파일 검색 0건). NOTES 가 "기존 씨앗" 셋 중 하나로
   지목했지만, `logical_expression` 은 plan 최상위 슬롯 이름일 뿐이며 **생산자(`plan["logical_expression"]=` 대입)가
   레포에 0건**이다. 소비만 3곳(게이트 `graph_rag.py:2573`, 컴파일러 등록 `:11767`, 빌더 `:15942-15971`).
   feature flag `LOGICAL_OR_COMPILER` 는 docker-compose 에서 프로덕션 "1"이지만 **소스에서 이 env 를 읽는
   코드가 없다**(주석 `graph_rag.py:15917` 뿐). `rag/trace.py:222` 는 stage 6 출처로 이 없는 파일명을
   사용자에게 표시한다(허위 출처).
2. **`capability_registry.py` 는 삭제됐다** (커밋 `ac924ff` "정리", 209줄). `validate_capabilities()` 는
   "호출 0건"이 아니라 **정의 자체가 소멸**했다. `requirement_capabilities.json` 도 같은 커밋에서
   `compiler_strategy`/`join_path`/`filter_field` 키가 제거되어 `supported`/`message` 만 남았다 —
   `COMPILER_STRATEGIES` 의 선언적 소비자는 현재 0이다. `plans_ir_decoupling.md` W1-4(a)의
   "validate_capabilities 3중 배선" 항목은 그대로는 실행 불가하며 **복원이 아니라 신규 작성**으로 재정의해야 한다.
   `docs/data/condition_ownership_policy.json` 도 같은 커밋에서 삭제됐는데 `docs/overview/structure.md:31,:201` 과
   `condition_reconciliation.py:20` 이 여전히 권위로 인용한다.
3. **`plan_schema.py` 는 축소 구현이다** — facet 은 사실상 `kind`(condition/derived/non_condition) 하나.
   W4-2 가 명세한 14 facet 은 미구현이고, 파생 소비자는 `ir_snapshot.py` 하나뿐이며
   `semantic_requirements.py:64-79` 는 여전히 자체 리터럴 `_PLAN_REQUIREMENT_SLOTS` 를 들고 있다.
4. **W5-1(build_sql_result 순수화)은 미착수이고 부채가 늘었다.** 2026-08-01 작업(NOTES.md)이
   `build_sql_result` 진입부에 인플레이스 변형 4건(백필·sweep·output_contract·member_policy,
   `graph_rag.py:9884-9897`)을 추가했다. deepcopy 방어 없음, `_finalize_execution_plan` 부재.
5. **진짜 씨앗은 `targeting_expression.py` 의 타입드 트리(신 API)다.** 노드 ID·content-hash 핑거프린트·
   SourceSpan·불변식 검증·provenance strip 을 이미 갖췄지만 소비자가 `plan_validation.py` 하나뿐인 고아다.
   실행 경로(graph_rag)는 같은 파일의 dict 기반 구 API 만 쓴다. `condition_evaluation_ir` 의 "검증된 구성
   서명"은 대수(algebra)가 아니라 **하드코딩 인스턴스 1개짜리 화이트리스트**이고(capability 상수 1개,
   expected 20경로 정확 비교), 컴파일러와 SQL 검증기가 리터럴을 각각 재선언하는 이중 렌더러다
   (임계값 2 가 3곳: `:437/:561/:598`).
6. **지원 조건 표는 문서에 아예 없다.** 유일한 사용자 대면 지원 목록은
   `member_target_filters.json:1048` `supported_condition_hint` 산문 문자열(22항목)인데, 같은 파일의
   `lapsed_buyer._supported:false` 와 자가모순이고(구매 횟수를 포괄 지원으로 광고),
   `graph_rag.py:560` 에 7항목짜리 옛 폴백 사본이 별도로 상존한다.

**수치 현황**: "무엇이 지원되는가"를 선언·판정하는 권위 **14곳**, 조건종류→컴파일러 매핑 **7갈래**,
층위별 개수 불일치(CONDITION_SPECS 32 kind → SLOT_SHAPES 23 슬롯 → LLM 노출 21)를 설명하는 단일 선언 없음.
출처 배제 정규형 3벌(`semantic_fields.strip_provenance` / `targeting_expression._strip_semantic_provenance` /
`ir_snapshot._canonical`). 물리 바인딩 래칫 343(하향 중), graph_rag 줄수 래칫 17185.

---

## 순서 요지

```
Phase 0   잔해 정리 (S~M)         — 유령 참조·죽은 표면 처분. 외부 소비 특성화 선행. 즉시 가능
Phase A   ⑪ Capability 단일 권위  — ⑨(모델 A/B)와 무관하게 진행 가능. 단계별 독립 출하
Phase B0  경계 정리(IR-전 단계)    — B-1~B-3. 모델 실험과 무관. 게이트: 응답 계약 green + Phase 0
Phase B1  ⑩ Canonical IR 본체     — B-4~B-5. 게이트: 결정 묶음(아래)
```

- 문서 동기화(장기 3번)는 별도 작업이 아니라 **A-4(표 자동 생성)로 구조적으로 해소**한다 — 손으로 갱신하는
  표는 만들자마자 다시 드리프트한다(§0-6 이 그 증거).
- A 를 B 앞에 두는 이유: A 는 선언·설정 수준이라 위험이 낮고 ⑨ 결과와 무관하게 가치가 있으며
  ("지원되는데 LLM 미방출" 계열을 노출면 파생화로 좁힌다), B 의 어댑터가 소비할 단일 권위를 먼저 만든다.
- **B0 은 ⑨를 기다리지 않는다.** B-1(순수화)·B-2(정규형 통합)·B-3(서명 일반화)은 IR 본체가 아니라
  컴파일러 경계·정규화 작업이고 모델 교체와 완전히 독립이다 — 원안이 이 세 항목까지 ⑨ 게이트에 묶은 것은
  과잉이었다(리뷰 반영).
- **B1 결정 묶음(단일 축 금지).** 타입드 트리가 실제로 사는 것은 유형 ③(이중 해석·소유권)과 인플레이스
  결합이지 유형 ①(미방출)이 아니므로, ⑨ 단독으로 B1 을 결정하면 축이 어긋난다. 판정 입력 5개:
  ① ⑨ 모델 A/B 잔존 실패율 ② **⑧ patch retry 효과**(중기 과제 — 미방출 개선분이 겹친다)
  ③ B-4 shadow 어댑터의 divergence 실측 ④ 이중 해석·소유권 계열 실패 잔존(소유권 원장 기준)
  ⑤ 변경 비용 추이(래칫: 물리 바인딩·모듈 크기·known_failures). ①②가 미방출을 대부분 해소해도
  ③④가 남아 있으면 B1 은 진행할 가치가 있고, 역으로 ③이 크면 어댑터 설계 재검토가 먼저다.

---

## Phase 0 — 잔해 정리 (착수 게이트 없음, 즉시 가능)

### 0-0. 외부 소비 특성화 (모든 제거 작업의 선행 조건)

- 이 시스템의 응답·trace 는 형제 레포 **frontend BFF** 가 소비한다. 응답 키·trace 스테이지·출처 배지를
  제거·변경하기 전에 BFF 레포에서 해당 키의 소비 여부를 grep 으로 확인하고 결과를 커밋 메시지에 기록한다.
- 외부 소비가 확인된 키는 즉시 삭제하지 않는다 — **폐기 예고(주석·CHANGELOG)+호환 기간**을 두거나
  BFF 측 제거와 같은 배포 창으로 묶는다. 내부 전용(생산자 0 이라 응답에 나타난 적 없는 키)만 즉시 삭제 대상.

### 0-1. logical_expression 잔해 처분

- **권고: 삭제.** 근거 — 생산자 0건, flag 는 아무것도 제어하지 않음, OR 수요는 `union_condition` 경로가
  실제로 흡수하고 있음. 삭제 대상: 게이트 참조(`graph_rag.py:2573`), 컴파일러 등록(`:11767`),
  빌더 `build_logical_expression_sql_candidate`(`:15942-15971`), 주석 블록(`:15915-15941`),
  `rag/trace.py:222` 허위 출처, docker-compose 2곳의 죽은 env, `tests/golden/cases.json` env 의 동일 키,
  `plan_schema.py:66` 의 CONDITION 등재(또는 '지향' 계층 이동 — 골든 계약의 aspirational 절차를 따른다).
- 대안(보류 시): 생산자를 복원해 슬롯을 살리는 길 — 이는 Phase B 의 타입드 트리와 역할이 겹치므로
  **B 에서 Or/And/Not 노드로 흡수하는 것이 맞고, 지금 별도 복원은 이중 투자**다.
- **검증**: `logical_expression` grep 이 plan_schema(잔류 시)와 이 문서 밖에서 0건. 전체 스위트 green.
  trace 스테이지 수 변화가 있으면 `_TRACE_STAGE_REFS` 계약 테스트 갱신.

### 0-2. `capability_check` 죽은 응답 키 처분

- `plan_schema` 가 derived 로 선언하지만 **생산자 0건**(읽기만 `graph_rag.py:7092`), 응답 계약
  `tests/test_api_response_contract.py:30` 은 항상-None 계약이다.
- **권고: A-3 validator 의 산출물로 실생산 재정의**(제거보다 낫다 — 이름과 자리가 이미 계약에 있으므로
  validator 결과를 싣는 게 자연스럽다). A-3 전까지는 계약 테스트에 "생산자 없음" 주석만 명시.

### 0-3. 죽은 참조 일괄 정정 + 가드 확장

- 정정 대상: `condition_ownership_policy.json` 인용 2곳(`docs/overview/structure.md`,
  `condition_reconciliation.py:20`), `requirement_capabilities.json` `_comment` 의 capability_registry 인용,
  `plans_ir_decoupling.md` 에 "2026-08-01 정정" 노트 추가(W1-4(a) 재정의·capability_registry 삭제 사실),
  `docs/overview/structure.md` §7.1 의 구시대 서술.
- `tests/test_doc_claims.py` 를 **`docs/data/**.json` 경로 인용까지** 확장 — 설정 파일 삭제가 문서 허위
  인용으로 남는 재발(이번이 두 번째)을 차단.
- **검증**: test_doc_claims 확장분 도입 시점 0건.

---

## Phase A — ⑪ Capability Registry 단일 권위

**설계 원칙.**
- 권위는 **Python 소스 레지스트리**로 둔다. `plans_ir_decoupling.md` '하지 않을 것' 의 판례 그대로 —
  슬롯/지원 선언은 물리 스키마가 아니라 IR 어휘라 JSON 외부화 대상이 아니며, JSON 외부화는 로드 실패
  침묵 양식을 재생산한다. JSON 에 남는 것은 물리 바인딩(member_target_filters 등)뿐.
- 신규 파일 신설이 아니라 **`targeting_ir.CONDITION_SPECS`(32 kind)를 씨앗으로 facet 확장**한다.
  이미 `kind/fact/fact_join/signals_target/extract/confidence/slot` 을 갖고 SLOT_SHAPES 와 결속돼 있다.
- 각 단계는 독립 출하 가능하고, 기존 드리프트 가드(test_golden_slot_schema_contract 등)가 각 단계의
  전후 동등성 판정기 역할을 한다.

### A-1. LLM 스키마 노출 파생화 (S)

- `query_structurer/campaign_plan_v4.py:200-220` 의 **손 나열 20개 + plan 루트 1개**를 SLOT_SHAPES 파생으로
  교체. 미노출 2개(`region_density_target`, `purchase_count_ranking` — properties 없는 조각이라 strict 표현
  불가)는 **명시 제외 집합 + 사유**로 선언. 새 슬롯 추가 = SLOT_SHAPES 한 항목(현재는 2군데 손편집).
- **검증**: 전환 전후 tool 스키마 **바이트 동일** 스냅샷 비교(LLM 동작 변화 0 보장).
  `test_golden_slot_schema_contract` 의 노출면 검사가 동어반복이 됨을 확인.
  **바이트 비교는 마이그레이션 한정 안전장치다** — 전환이 끝나면 영구 가드는 바이트가 아니라
  파생 계약(파생 함수가 SLOT_SHAPES+제외 집합에서 노출면을 만든다는 구조 단언)으로 바꾼다.
  바이트 동일을 영구 기준으로 두면 무해한 표현 변경(키 순서 등)까지 막아 개선을 봉인한다(리뷰 반영).
- **위험**: 파생이 실수로 제외 집합을 무시하면 strict 스키마가 깨져 구조화 전면 실패 — 바이트 비교가 차단.

### A-2. 지원 상태·라벨 facet (M)

- CONDITION_SPECS 에 facet 추가: `status`(삭제된 taxonomy 부활 — supported/not_implemented/no_join_path/
  missing_filter_field/compilation_unavailable/disabled_by_policy), `ko_label`, `llm_exposed`,
  `unsupported_reason`(unsupported_reasons.ALL 의 원소로 제한).
- **별칭(alias) facet — kind 등가까지만.** 여러 표현이 하나의 정규 kind 로 귀결되는 **kind 수준 등가**
  (예: 행동↔지표 `behaviors.*.metric_id` 등가)는 레지스트리 facet 으로 선언한다. 단 **표면 표현 동의어는
  제외** — 표면어는 렉시콘 계층(parser_lexicon/surface registries)이 단일 소유하며, capability 레지스트리에
  넣으면 표면어 이중 소유를 재생산한다(금지 표면 목록 5종 원칙 유지).
- 파생 전환: `_UNSUPPORTED_CONDITION_LABELS` 42개(`graph_rag.py:12164`) → ko_label facet 파생,
  `supported_condition_hint` 산문(`member_target_filters.json:1048`) → 레지스트리 파생 자동 생성,
  `graph_rag.py:560` 의 7항목 stale 폴백 삭제. **lapsed_buyer 자가모순 해소**(hint 가 `_supported:false` 반영).
- 32→23→21 격차가 facet 조합(slot 유무 × llm_exposed × status)으로 **기계 설명 가능**해진다.
- **검증**: 라벨 파생 전후 문자열 동일 스냅샷. hint 는 의도 변경이므로 전후 diff 를 커밋 메시지에 명시.

### A-3. validator 신규 작성 — validate_capabilities 재탄생 (M)

- 정적 교차검증: `status==supported` ⇒ `_sql_target_builder_registry` 에 **정확히 1개 빌더 소유**
  (삭제된 fact_join 소유권 불변식 부활, `graph_rag.py:11749-11751` 의 자백 해소) ∧ 필요한 설정 섹션 실재
  ∧ filter_field 류는 schema_catalog 실재. `status!=supported` ⇒ reason+label 필수.
- **1:1 소유는 완화하지 않는다 — "1:1 기본 + 선언된 예외 목록"으로 관리한다.** dispatch 가 우선순위
  튜플 + 첫 non-None 승자 구조라, 한 kind 를 여러 빌더가 소유하는 순간 튜플 순서가 의미가 된다(순서
  하나 바뀌면 SQL 이 조용히 바뀜 — "승자만 SQL 내고 나머지 드롭"이 이 레포의 확인된 실패 양식).
  원 불변식의 기존 예외(복합 컴파일러 3종 kind 미소유, fact_join=False 허용목록)는 validator 에
  **명시 선언 데이터**로 승격한다. 여기에 보조 불변식 3개를 추가(완화의 대체물이 아니라 추가):
  ① 미선언 kind 생성 금지(레지스트리 밖 kind 가 plan 에 나타나면 red) ② supported kind 의 컴파일러
  누락 금지 ③ 각 노출 kind 의 정규화·검증 경로가 결정적(coerce 경유 강제).
- 배선 3중: 계약 테스트(`tests/test_capability_contract.py` 신규) + `db_swap_preflight` 검사 섹션 추가 +
  `REGISTRY_HEALTH`/`/health` 노출(절대 raise 금지 — W1-4 원안의 원칙 유지).
- **0-2 의 `capability_check` 응답 키를 이 validator 산출물로 실생산**한다.
- **검증**: 역검증 3종 — 빌더 하나를 레지스트리에서 빼면 red, status 를 supported 로 바꾸고 빌더가 없으면
  red, 라벨을 지우면 red.

### A-4. 지원 조건 표 자동 생성 — 장기 3번(문서 동기화) 해소 (S)

- `docs/generated/supported_conditions.md` 를 CONDITION_SPECS 파생으로 생성(도구 `tools/` 아래).
  조건별: kind·한글 라벨·status·**조건부 지원의 조건**(기간비교=수치 집계 지표만 `graph_rag.py:15027-15035`,
  동시구매=동일 주문 내 동일 상품 수량 서명만, lapsed_buyer=미지원+사유)·미지원 시 안내 문구.
- CI 최신성 체크(재생성 diff 0). 손편집 금지 헤더.
- 운영 문서가 코드와 갈라질 수 있는 유일한 원인(손 갱신)이 제거된다 — "미지원인 줄 알았는데 경로가
  생긴" 항목의 동기화가 커밋마다 자동으로 일어난다.

### A-5. 직교 축 정리 (S)

- `requirement_capabilities.json`(base×qualifier)은 CONDITION_SPECS 와 **직교 축이므로 통합하지 않고
  유지**하되, 로드 검증을 A-3 validator 에 합류시킨다.
- `compiler_strategy` 키 재결속 여부 결정 — **권고: 실소비가 생길 때까지 supported/message 전용으로 축소를
  명시**(주석·스키마 검증에 반영). `COMPILER_STRATEGIES` 는 직접 호출 dispatch(`DIRECT_COLUMN_FILTER_SPECS`
  경로)로만 남긴다. 죽은 선언 키를 되살리는 것은 W1-4 시대의 오배선 재생산이다.
- `SlotShape.resolves_unsupported` — **삭제 보류(2026-08-02 구현 중 정정)**: 값을 선언한 슬롯은 0이지만
  소비 배선이 살아 있다(graph_rag `_candidate_resolves_unsupported` 생산 → plan_resolver 소비,
  unsupported_reason 닫힌집합 테스트도 참조). 필드만 지우면 배선이 죽은 코드로 남는다 —
  배선 전체의 존폐를 별도 검토한 뒤 함께 처분한다.

### A-6. 임시 드리프트 가드 전환 (S)

- 파생화로 동어반복이 된 가드는 **회귀망으로 유지**하되 "임시 결속" 주석을 제거하고, 가드의 단언 대상을
  '두 사본의 일치'에서 '파생 함수의 계약'으로 바꿔 서술을 정직화한다.

**Phase A 성공 지표**

| 지표 | 현재 | 목표 |
|---|---|---|
| 지원 판정 권위 수 | 14 | 5 (단일 레지스트리 + 직교 4축: base×qualifier / metric 연산자 / behaviors 물리 / 사건식 노드) |
| 새 조건 추가 시 손편집 지점 | 선언 2 + 라벨 + hint + 문서 (5+) | 레지스트리 1 선언 + 빌더 1 + 물리 설정 1 |
| 노출면 숫자 격차(32/23/21) 설명 | 없음 | facet 조합으로 기계 설명 + validator 가 상시 검증 |
| 사용자 대면 지원 문서 | 산문 1곳(자가모순) + stale 폴백 | 자동 생성 표 1곳, CI 최신성 강제 |

---

## Phase B0 — 경계 정리 (IR-전 단계, 모델 실험과 무관)

B-1~B-3 은 Canonical IR 본체가 아니라 **컴파일러 경계·정규화 작업**이다(W5-1/W5-3② 그대로).
⑨를 기다리지 않는다.

**착수 게이트 2개**:
1. `tests/test_api_response_contract.py` green + build_sql_result 이후 `query_plan` 을 읽는 하류 지점
   전수 목록 고정(확인된 곳: `graph_rag.py:6555` 응답 조립, `:7092` capability_check).
2. Phase 0 완료(잔해 위에 경계를 긋지 않는다).

### B-1. build_sql_result 순수화 = W5-1 (L)

- `plans_ir_decoupling.md` W5-1 그대로 + **이번에 늘어난 진입부 인플레이스 4건**(`graph_rag.py:9884-9897`)을
  포함해 산출물을 반환 dict 로 이관, `_finalize_execution_plan` 경계 신설, 진입부 deepcopy.
- NOTES 단기 항목 `graph_rag.py:9978` `output_contract` 직접 키 접근 방어도 여기 흡수.
- **freeze 계약의 물리적 실체가 이것이다** — "adapter 이후 의미 추가 금지"는 규범이 아니라
  "build_sql_result 가 입력을 변형할 수 없다"는 테스트(`test_build_sql_result_does_not_mutate_input`)로 강제.
- **검증·위험**: W5-1 명세 그대로(역검증 먼저 — 계약 테스트가 실제로 red 가 되는지 확인 후 착수).

### B-2. 출처 배제 정규형 통합 (M)

- 3벌(`semantic_fields.strip_provenance` / `targeting_expression._strip_semantic_provenance` /
  `ir_snapshot._canonical` 의 `_`-접두 제거)을 `semantic_fields` 한 곳으로. 저위험 첫 수확 —
  이후 B-4 의 shadow 비교가 "같은 정규형 위의 비교"가 된다.
- **검증**: 골든 스냅샷 바이트 불변(정규형 통합은 표현 변경이지 의미 변경이 아님을 스냅샷이 증명).

### B-3. condition_evaluation_ir 서명 일반화 = W5-3 ② (M)

- 인스턴스 1개 화이트리스트를 **서명 목록**으로: `expected` 튜플을 `docs/data/capabilities/*.json` 스펙으로
  분리(물리 바인딩이므로 JSON 이 맞다), 빌더·검증기·SQL 조각·조건토큰이 전부 하나에서 파생,
  `compile_evaluation` 과 `validate_compiled_sql` 이 **같은 포맷터 함수를 공유**(이중 렌더러 해소, 임계값 2 단일 선언).
- 이로써 "검증된 구성 서명만 허용"이 NOTES 가 상정한 **일반 메커니즘**이 된다 — capability 를 2개째
  추가할 때 3벌 분기가 아니라 스펙 파일 1개.
- **스키마 압력 테스트(리뷰 반영)**: 인스턴스 1개(n=1)만 보고 스펙 스키마를 설계하면 same-product
  가정이 스키마에 박힌다. 설계 시점에 **가상의 두 번째 서명 1~2개**(예: 주문 횡단 동시구매, 상이 상품
  동시구매)로 스키마 모양만 압력 테스트한다 — 구현하지 않는다(범용 관계 연산자 API 선행 설계는
  과잉 일반화라 금지, '하지 않을 것' 참조). **완료 판정**: 두 번째 실제 capability 가 코드 분기 0 으로
  스펙 파일 1개 추가만으로 들어오는 것.
- **검증**: W5-3 ② 명세 그대로(스펙에서 임계값을 바꾸면 4곳이 모두 따라오는지) + 물리 바인딩 래칫
  condition_evaluation_ir 14→0.

---

## Phase B1 — ⑩ Canonical IR 본체

**착수 게이트: 결정 묶음(단일 축 금지, '순서 요지' 참조)** — ① ⑨ 잔존 실패율 ② ⑧ patch retry 효과
③ B-4 shadow divergence 실측 ④ 이중 해석·소유권 계열 잔존 ⑤ 래칫 추이. B0 완료가 전제.
①②가 미방출을 대부분 해소해도 ③④가 남으면 B1 은 가치가 있고, ③이 크면 어댑터 재설계가 먼저다.

### B-4. adapter 경계 + freeze — 타입드 트리 shadow (L)

- `targeting_expression` 신 API(타입드 트리)를 canonical 형으로 채택. `query_plan` 조건 슬롯 →
  `TargetingExpression` 어댑터를 **B-1 이 만든 경계 위에** 단방향으로 배선.
- **shadow 모드 먼저**: 트리를 병행 생성해 핑거프린트를 plan 스냅샷에 기록만 한다. 관측 지표 —
  재실행 핑거프린트 안정성(멱등), 골든 19 + 실패 14 코퍼스에서 트리↔슬롯 정보 손실 0
  (`condition_claim_invariant_issues` 활용). 컴파일은 아직 트리를 읽지 않는다.
- freeze 가드: "어댑터 이후 의미 필드 변형 0" = B-1 의 무변형 계약 + 핑거프린트 전후 동일 테스트.
- **위험(이 플랜 최대)**: 어댑터가 손실 변환이면 조용한 조건 소실 — shadow 관측 기간을 두고,
  divergence 파일 고정(증가 시 red) 후에만 B-5 진입.

### B-5. 컴파일 소비 전환 — kind 단위 점진 (L)

- 빌더가 dict 슬롯 대신 트리 노드를 소비하도록 **kind 단위로** 전환. 시작점은 이미 순수한
  `compiler_registry` 4종(duration/aggregate/campaign_buy_amount/profile_metric) — 입출력이 좁아 전환 비용이
  가장 낮다. `_sql_target_builder_registry` 의 우선순위 튜플 구조는 유지(전환된 빌더만 트리를 받는다).
- `plan_schema` facet 확장(W4-2)은 빅뱅이 아니라 **전환에 필요한 facet 만** 그때그때 추가.
- Phase 0-1 에서 삭제한 OR 표현 수요는 여기서 `Or/And/Not` 노드로 정식 흡수(union_condition 경로의 후계).
- **검증**: kind 하나 전환마다 SQL 골든 바이트 불변 + 의미 골든 불변. 전환된 kind 목록을 파일로 고정.

**Phase B0/B1 성공 지표**

| 지표 | 현재 | 목표(B0) | 목표(B1) |
|---|---|---|---|
| build_sql_result 입력 변형 골든 케이스 | 13+ (증가) | 0 | 0 |
| 출처 배제 정규형 구현 수 | 3 | 1 | 1 |
| 동시구매류 capability 추가 비용 | 3벌 분기 | 스펙 JSON 1개 | 스펙 JSON 1개 |
| 트리 소비 빌더 수 | 0 | 0 (shadow 없음) | compiler_registry 4종 + 점진 |
| 타입드 트리 소비자 | plan_validation 1곳 | 동일 | 어댑터 + 컴파일러 |

---

## plans_ir_decoupling.md 와의 정합

| 이 플랜 | 5웨이브 대응 | 비고 |
|---|---|---|
| 0-1, 0-3 | W1-2/W1-4 계열(죽은 참조) | 삭제 커밋 `ac924ff` 이후 재발분 |
| 0-2, A-3 | W1-4(a) | **재정의**: 복원 불가 → 신규 작성. preflight 검사 추가는 원안 유지 |
| A-1 | '하지 않을 것'의 "LLM tool 스키마 확장" 과 구분 | 노출면 **불변**(바이트 동일) 파생화라 해당 금지에 저촉 안 됨 |
| B-1 | W5-1 | 명세 그대로 + 신규 부채 4건 포함 |
| B-3 | W5-3 ② | 명세 그대로 |
| B-4/B-5 | W5-5 와 별개 | W5-5(스팬 좌표계)는 이 플랜에 흡수하지 않음 — 독립 트랙 유지 |
| W5-2, W5-3 ①③④⑤⑥, W5-4 | 이 플랜 범위 밖 | 기존 문서의 착수 게이트·순서 그대로 유효 |

## 하지 않을 것

- **capability_registry.py 원복** — 삭제 전 형태는 "프로덕션이 소비하지 않는 별도 파일"이라는 오배선
  자체가 문제였다. 권위는 CONDITION_SPECS facet 으로, 검증은 A-3 신규 validator 로.
- **슬롯/지원 선언의 JSON 외부화** — plans_ir_decoupling '하지 않을 것' 판례 유지. JSON 은 물리 바인딩만.
- **빅뱅 전환** — B-5 는 kind 단위, A 는 단계별 바이트 동일 스냅샷으로 각각 독립 출하.
- **LLM 스키마에 새 슬롯 광고를 섞는 것** — A-1 은 노출면 불변 파생화다. 신규 광고(예: 지향 슬롯 승격)는
  auto 경로 분포를 바꾸므로 별도 결정.
- **logical_expression 슬롯의 지금 복원** — B-5 의 트리 노드가 정식 후계다.
- **범용 관계 연산자 API 의 선행 설계** — 인스턴스 1개에서 범용 API 를 뽑는 것은 과잉 일반화다.
  B-3 은 스키마 압력 테스트(가상 서명, 미구현)까지만 하고, 범용성은 두 번째 실제 capability 가
  스펙 파일 1개로 들어오는지로 사후 검증한다.
- **빌더 1:1 소유의 완화** — 다중 소유를 허용하면 dispatch 튜플 순서가 의미가 된다. 예외는 완화가
  아니라 선언(A-3 예외 목록)으로 관리한다.
