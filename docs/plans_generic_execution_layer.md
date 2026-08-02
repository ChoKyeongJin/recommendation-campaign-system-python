# 실행 계층 범용화 — 플랜과 실행 기록 (2026-08-02)

의미 해석 계층(SemanticPlanV2 / generic core)은 이미 범용이고 `tests/test_generic_core_layering.py`
가 그 경계를 지킨다. 범용이 아니었던 것은 **실행(SQL 생성) 계층**이다: 20개 손-정렬 빌더 체인,
소스에 흩어진 물리 바인딩, 주석으로만 존재하던 순서 계약.

이 문서는 그 범용화의 플랜이자 **실행 기록**이다. 계획과 다르게 판단한 곳은 `실행 노트`로 적었다.

## 실측 요약

| 항목 | 착수 전 (f4b8034) | 완료 후 |
|---|---|---|
| pytest | 964 passed / 0 failed | **1,135 passed / 24 skipped / 0 failed** |
| 소스 물리 바인딩 | 기준선 343 / 실측 333 | **184** |
| `graph_rag.py` | 17,470줄 (상한과 동일, 여유 0) | **17,247줄** (상한 17,300) |
| `db_swap_preflight` | PASS (레지스트리 2종) | **PASS (4종 — canonical 카탈로그 편입)** |
| live-26 기준선 | 재현 불가(러너 미커밋, 14/0/12 분쟁) | **SQL 4 / 미지원 12 / 되묻기 9 / 실패 1** (재현 가능) |

## 착수 전에 확정한 사실

- **"red 가드 3개"는 존재하지 않는 결함이었다.** `test_event_ir` 노드 타입 / V4 스키마 계약 2종의
  실패는 HEAD 테스트 본문 × 워킹트리 소스가 섞인 관측 창의 아티팩트였다. 착지 후 전부 초록.
  → 플랜에서 "가드 3개 수정" 워크스트림을 **삭제**했다.
- **빌더 dispatch 는 owned-kind 를 읽지 않는다** — 선형 first-non-None 스캔이며, 소유권은
  `capability_validation.builder_ownership_issues`(축 C)가 이미 게이트한다. **없던 것은 순서 계약뿐.**
- **가장 위험한 결합은 하드코딩이 아니라 dispatch 의 fail-close 프로토콜**(인플레이스 변형 +
  covered/dropped 손 큐레이션).
- **`graph_rag.py` 는 크기 상한과 정확히 같아 여유가 0줄**이었다 — 신규 코드는 전부 소유 모듈로.

---

## Phase 0 — 착지와 재기준선

- **0-1 웨이브 착지** — 동시 세션의 canonical-audience 웨이브가 f4b8034 로 착지(트리 clean, 964 green).
- **0-2 NOTES.md 회복** — 79dad3d/f4b8034 의 canonical audience 아키텍처 기록(문서화 전무였음).
- **0-3 live-26 러너 커밋 + 재측정** — `tools/live_prompt_baseline.py` +
  `docs/data/test_baselines/live_prompts.json`(코퍼스 26종) + `tests/test_live_prompt_baseline.py`.
- **0-4 strict-mode 검증** — 라이브 스모크로 새 `$ref/$defs` 스키마 수락 확인(SQL 출고 성공) +
  `tests/test_strict_tool_schema_contract.py` 오프라인 드리프트 가드.

> **실행 노트 — 분류기가 틀렸고, 그게 기준선 분쟁의 원인이었다.**
> 첫 측정은 `clarification_questions` 를 먼저 봐서 **정직한 미지원 12건을 전부 '되묻기'로 오집계**했다
> (unsupported 0). 권위는 `status` 필드다(`success`/`needs_clarification`/`unsupported`/`no_verified_sql`)
> — 미지원 응답도 같은 문구를 `clarification_questions` 에 싣기 때문이다. 고친 뒤 재측정한 값이 위 표의
> 4/12/9/1 이다. **재현할 수 없는 수치는 기준선이 아니다** — 러너가 저장소 밖에 있었기 때문에 같은
> 코퍼스에 대해 14/26·0/26·12/26 세 숫자가 서로를 반박하며 공존했다.

## Phase 1 — 안전망 신규 작성

- **1-1 빌더 순서 계약(축 E)** — 선행 제약 9종을 `capability_validation.BUILDER_PRECEDENCE` 에
  **사유와 함께** 선언하고 `builder_order_issues` 가 강제. `tests/test_builder_order_contract.py`.
- **1-2 import 시점 하드 게이트** — `enforce_builder_contracts` 를
  `graph_rag._install_sql_builder_admission_guards` 에서 호출(위반 시 기동 실패).
  계약 테스트는 이 저장소에서 두 번(ce39f68, 8ba50b6) 일괄 삭제된 전력이 있어 기동 경로에 둔다.
- **1-3 스테일 주석/문서 수리** — 삭제된 `test_registry_ownership_guards.py` 인용,
  `targeting_ir` 의 "현재 가드 없음", 거짓이 된 "가장 먼저" 주석, `plans_ir_decoupling` W1-4 의
  '복합 3종'(실제 2종) 정정.
- **1-4 관문 특성화** — `tests/test_admission_boundary_contract.py`:
  `plan_validation`(닫힌 status 5종, 읽기 전용) + `sql_guard`(허용목록·금지어·타입군 조인·**재작성**).
- **1-5 BFF 응답 계약** — `tests/test_bff_response_contract.py` 가 형제 레포 `route.ts` 소비 키를 고정.

> **실행 노트 — 특성화가 두 가지 함정을 문서화했다.**
> ① `sql_guard` 는 판정만 하지 않고 **재작성**한다: 입력은 `sql`, 실행용은 `safe_sql`(행 제한 부착).
> 빌더 산출물이 그대로 나가지 않으므로 회귀 귀속 시 둘을 구분해야 한다.
> ② `validate_join_keys(column_types=...)` 의 값은 원시 타입이 아니라 **타입군**이다
> (`load_column_types` 가 이미 환산). 원시 타입을 넣으면 `bigint` vs `int` 가 불일치로 잡힌다.
> ③ BFF 라벨 키는 `*_condition_labels`(**단수** condition)다. 복수형으로 바꾸면 라벨만 조용히 사라진다.

## Phase 2 — 물리 바인딩 이관 (343 → 184)

1. **스캐너 정의 수정(-70, 이관 아님)** — `build_table_relationships`/`build_dimension_catalog`/
   `build_rag_knowledge` 는 카탈로그 **생산자**다(새 DB 를 향해 다시 실행할 대상이지 부채가 아니다).
   한 파일이 전체의 20% 를 차지해 런타임 이관 진척을 가리고 있었다.
2. **미러 이관(graph_rag -108, member_filters_config +48)** — 물리 이름이 **세 곳**(JSON / 코드 미러 /
   접근자 인라인 기본값)에 살았다. 미러 249줄을 순수 설정 모듈 `member_filters_config.CODE_DEFAULTS`
   로 옮기고 죽은 인라인 기본값 64건을 걷었다.
3. **네 번째 사본 제거(confidence -11, member_policy -5)** — 두 모듈은 각자 JSON 을 직접 읽고 파일
   부재 시 빈 dict 를 돌려줬기 때문에 인라인 물리 기본값이 **살아 있었다**. 같은 미러를 공유하도록 배선.
4. **죽은 기본 인자 제거(-3)** — `entity_set`/`targeting_expression` 의 `member_key`/`age_column`
   기본값을 필수 인자로. `AGE` 는 `base_entity.age_column` 으로 설정화(JSON·미러 양쪽 선언).

드리프트 가드: `tests/test_member_filters_defaults_drift.py`.

> **실행 노트 — 미러는 사본이 아니라 최소 폴백이다(계약으로 못 박음).**
> 처음엔 "미러 ⊇ JSON"도 강제하려 했으나, 배포 JSON 이 훨씬 풍부하다(동의어·단위·힌트 어휘).
> 같게 만들면 방금 걷어낸 이중 소유를 원래 크기로 되살리는 일이다. 그래서 계약은 **한 방향만**이다:
> **JSON ⊇ 미러**. 로더가 최상위 키 단위로 섹션을 통째로 대체하므로, JSON 에 키가 빠지면 미러 값이
> 폴백되는 게 아니라 `None` 이 된다 — 실제로 `base_entity.age_column` 이 정확히 그 상태였다.

## Phase 3 — 라우팅 선언화 (빌더 몸통 무변경)

- **3-1 배타 라우팅 선언화** — "이 슬롯이 있으면 이 빌더만 시도한다"를
  `capability_validation.EXCLUSIVE_ROUTES` 에 사유와 함께 선언하고 dispatch 가 소비.
  (이전에는 `event_expression` 바이패스가 dispatch 안 if 문이었고 "왜 여기만 예외인가"가 코드에 묻혀 있었다.)
- **3-2 순서 파생 검증** — `derive_builder_order`(안정 위상정렬)가 현재 튜플 순서를 **그대로 재현**함을
  테스트로 확인. 재현되지 않으면 제약이 모순이거나 순서에 설명되지 않은 의도가 숨어 있다는 뜻이다.

> **실행 노트 — 실행 모델은 바꾸지 않았다.**
> kind→builder 직접 dispatch 로 바꾸지 않았다. 시도별 부작용(unsupported/unresolved 인플레이스 변형,
> decision 로그)이 실패 표면의 load-bearing 동작이고, 그 표면을 BFF 가 읽는다. 위상정렬은 순서를
> **바꾸는** 장치가 아니라 제약이 현재 순서를 **설명하는지 검증하는** 장치로 쓴다.

## Phase 4 — 실행 계층 선언화

> **실행 노트 — 계획을 재정의했다.** 원래는 "빌더 20개 중 8개를 새 선언 스펙으로 다시 쓴다"였다.
> 그러나 조사 결과 **canonical Event IR 경로가 이미 그 선언 계층**이었다:
> `SemanticPlanV2 → semantic_plan_event_lowering → Event IR → event_compiler → SQL`, 물리 바인딩은
> `audience_catalog.json` 이 선언하고 lowering 코드에는 사건별 분기가 없다.
> 즉 범용화의 방향은 "legacy 빌더를 선언적으로 다시 쓰기"가 아니라 **canonical 이 덮는 범위를 넓혀
> legacy 로 내려가지 않게 하기**다. 새 선언 스펙을 하나 더 만들면 세 번째 소유자가 생길 뿐이다.

- **4-1 소유권 계약** — `tests/test_audience_catalog_ownership.py`:
  코드 폴백(`event_compiler.EVENT_REGISTRY`/`FIELD_REGISTRY`)은 카탈로그의 **부분집합**이어야 하고,
  런타임 해석본이 폴백을 덮으며, 카탈로그의 모든 테이블·컬럼이 `schema_catalog` 에 실재해야 한다.
- **4-2 확장 경계의 기계 검증** — JSON 에 사건을 추가하면 코드 변경 없이 해석되고
  발생 시각 필드까지 파생되는지 실측(`test_adding_a_source_is_json_only`).
- **4-3 lowering 순수성** — lowering 에 사건별 리터럴(`"purchase"`/`"cart"` …)이 들어오면 실패.

## Phase 5 — 잔여 결정

- **preflight 범위 확장** — `audience_catalog.json`·`attribute_catalog.json` 을
  `REGISTRY_PATHS` 에 편입. legacy 설정만 검사하면 canonical 경로가 게이트 밖에 남아
  "레거시는 되는데 canonical 만 0명"이 된다. 규칙 자체를 테스트로 고정.
- **preflight 오탐 수리** — `*_expression` 이 있으면 나란한 `*_column` 은 이름표다.
  canonical 캠페인 사건은 `time_column="CAMP_SDATE"` 를 선언하지만 실제로는
  `time_expression="ZC.CAMP_SDATE"`(조인된 `Z_CAMPAIGN`)를 쓴다 — 소스 테이블에 귀속하면 상시 빨강.
  **오탐이 나면 사람이 게이트를 끈다.**
- **카탈로그 신선도 런북** — `docs/operations/catalog_freshness_runbook.md`.
  "코드는 재시작으로 충분"이 **카탈로그 재생성에는 적용되지 않는다**는 것과 그 전파 순서를 적었다.
- **ranked_entity_set 방출 — 결정: LLM 방출을 유지한다.** canonical `Summarize/Order/Limit` 의
  비-테스트 생산자는 `relation_from_dict`(LLM 출력 역직렬화)뿐이다. 결정론 lowering 을 추가하는 것은
  원문을 두 번 읽는 계층의 부활이라 `tests/test_single_interpretation_path.py` 가 금지한다.
  안전망은 결정론 백필이 아니라 **미귀결 영수증**(`canonical_audience_claims` 의 discharge)이며,
  이것이 fail-close 로 작동한다. 라이브 #9(랭킹→멤버십)는 현재 SQL 출고 성공.

---

## 남은 것 (이번 범위 밖)

- **live-26 의 되묻기 9건** — 대부분 `semantic_ir_needs_clarification`/`semantic_structurer_failure`,
  즉 **방출 품질**이지 실행 계층 결함이 아니다. 개선 축은 모델 A/B 와 노드 스키마 enum 노출.
- **#21 "6개월 이상 접속하지 않은 휴면 고객"** — `login` 소스는 카탈로그에 있는데 `'이상'`+시간 창
  조합을 Event IR 이 소비하지 못한다. 데이터가 아니라 **의미**의 결핍이라 카탈로그 한 줄로는 안 열린다.
- **#4 `sql_guard_failed`** — 장바구니 다중 상품. 관문에서 막히는 유일한 케이스(특성화는 됐고 수리는 별건).
- **물리 바인딩 잔여 184** — `graph_rag` 79 / 미러 48 / `event_compiler` 24 / `condition_evaluation_ir` 14 /
  `join_paths` 7 등. 미러 48 은 의도된 최소 폴백이다. 스캐너는 별칭 접두(`C.KEEP_YN`)·표현식 내장
  (`SUM(QTY)`)을 세지 않으므로 **실제 부채는 184 보다 많다** — 수치를 "0 에 도달"으로 읽지 마라.
- **W5-1/B-1 `build_sql_result` 정화** — 미착수. graph-rag-split Phase 3(빌더 계층 구조 변경)의 선행 게이트.

## 하지 않은 것 (의도적)

- kind→builder 직접 dispatch(부작용 표면 파괴), 빌더 몸통용 새 선언 스펙(세 번째 소유자 생성),
  삭제된 테스트 원문 복원(W1-2 사용자 결정), 소유권 계약 테스트 중복 작성
  (`test_capability_contract.py` 가 유일 게이트), 결정론 백필류 원문 재해석 계층 부활.
