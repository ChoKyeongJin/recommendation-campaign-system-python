# 범용 코어와 회원 타기팅 도메인 계층 (2026-08-02)

> **2026-08-05 폐기 고지 — 아래 §1~§9 는 삭제된 구조를 기술한다.**
>
> SemanticPlanV2 중간표현과 그 스택 13종(`semantic_plan` / `semantic_plan_llm` /
> `semantic_plan_bridge` / `semantic_plan_event_lowering` / `semantic_pipeline` /
> `semantic_capability` / `semantic_coverage` / `semantic_candidates` / `semantic_reemission` /
> `semantic_retype` / `requirement_ledger` / `compile_contract` / `legacy_plan_compiler`)은
> 2026-08-05 **모듈 파일까지 삭제**됐다. 오디언스 IR 은 canonical Event IR 하나다
> (`audience_requirement` → `event_ir` → `event_compiler`).
>
> 살아남은 것은 범용 코어 셋(`semantic_normalizers` / `temporal_semantics` /
> `semantic_domain_binding`)과 도메인 계층(`targeting_domain`)이며, 그 경계 계약은
> `tests/test_generic_core_layering.py` 가 계속 잰다. 결핍 원인·실패 성격 어휘는
> `semantic_outcome.py` 로 옮겨 살아 있다.
>
> 이 문서는 **당시 설계 의도의 기록**으로 남긴다. 여기 적힌 파일 지도·확장 지점·테스트
> 목록을 오늘의 구조로 읽지 마라.

이 문서는 "사용자 입력 → requirement → capability → 실행 계획 → 검증 → 제한적 재방출 →
구체적 실패/clarification" 파이프라인이 **어느 파일에 어떻게 나뉘어 있는지**와,
**무엇을 JSON 선언만으로 열 수 있고 무엇이 코드 변경을 요구하는지**를 정리한다.

가장 짧은 요약:

> 코어는 requirement / expression tree / candidate slot / capability / validation result 만 안다.
> 회원 타기팅의 속성·연산자·데이터 grain·서열·시간 의미는 **도메인 계층과 카탈로그 JSON** 소유다.
> 코어가 도메인 낱말을 알게 되는 순간 `tests/test_generic_core_layering.py` 가 빨개진다.

---

## 1. 파이프라인과 소유자 (2026-08-05 삭제된 구조)

아래 8단계는 **더 이상 존재하지 않는다.** (2)와 (8)만 남았다.

```
사용자 입력                                                    [삭제됨 2026-08-05]
  │
  ├─(1) requirement 추출          semantic_plan_llm      LLM 은 의미 노드만 낸다(슬롯·상태 금지)
  ├─(2) 값·시간 한정어 정규화     semantic_normalizers   값 문법            ← 살아 있음
  │                               temporal_semantics     시간 한정어 → 범용 연산자  ← 살아 있음
  ├─(3) coverage 검증             semantic_coverage      원문 근거 ↔ 노드 대응
  ├─(4) capability 판정(5축)      semantic_capability    engine/executor/grain/coverage/executable
  ├─(5) 실행 계획 컴파일          legacy_plan_compiler   ← 도메인(실행 슬롯 지식의 단일 소유자)
  ├─(6) requirement 원장 + 검증   requirement_ledger     조건별 최종 귀결 회계
  ├─(7) 미해결만 패치 재방출      semantic_reemission    보호 집합 + 회귀 되돌림 + 정책 횟수
  └─(8) 구체적 실패/clarification failure_messages       실패 종류별 한국어 렌더링  ← 살아 있음
```

단계 조립은 `semantic_pipeline.run` 하나가 했고, 도메인 주입은 `semantic_plan_bridge` 하나가 했다.
두 모듈 다 삭제됐다.

**지금의 경로**: LLM 이 `audience_requirement.expression`(canonical Event IR)을 직접 내고,
`query_structurer` 가 그것을 접지·검증한 뒤 `event_compiler` 가 SQL 로 낮춘다. 실행 가능 여부는
선언이 아니라 `event_compiler.validate_compiler_capability` 가 답하고, 표현할 수 없는 요청은
`audience_requirement.issues` 로 보존돼 미지원/되묻기로 닫힌다.

---

## 2. 파일 지도

### 2-1. 범용 코어 (도메인 낱말 금지)

| 파일 | 소유하는 개념 | 2026-08-05 |
|---|---|---|
| `semantic_plan.py` | expression tree(의미 노드), 필드 선언, 결핍 계산, 실패 코드·status 닫힌 집합 | 삭제 |
| `temporal_semantics.py` | **범용 시간 연산자 10종**과 인자 요구, 표면 감지 실행기(어휘는 주입) | 유지 |
| `semantic_normalizers.py` | 값 정규화(금액·수량·기간·랭킹·연산자·단위) | 유지 |
| `semantic_capability.py` | capability 5축 판정과 실패 분류 | 삭제 |
| `semantic_coverage.py` | 원문 근거 대응 검증(앵커 공급자는 등록 주입) | 삭제 |
| `semantic_candidates.py` | 후보 병합·충돌 기록 | 삭제 |
| `requirement_ledger.py` | **requirement 레코드**와 귀결 회계, 회귀 판정 | 삭제 |
| `semantic_reemission.py` | 패치 재방출(정책·보호 집합·되돌림) | 삭제 |
| `semantic_pipeline.py` | 단계 조립, 컨테이너 무관 산출물 적용 | 삭제 |
| `semantic_plan_llm.py` | LLM 출력 계약(스키마는 노드 선언에서 파생) | 삭제 |
| `compile_contract.py` | `CompileContext` / `CompileResult` — 코어↔컴파일러 경계 타입 | 삭제 |
| `semantic_domain_binding.py` | 도메인 플러그인 바인딩(모듈 이름은 설정값) | 유지 |
| `semantic_outcome.py` | 결핍 원인·실패 성격 어휘(리터럴만) — 삭제된 `semantic_plan` 에서 떼어냄 | 신규 |

### 2-2. 회원 타기팅 도메인 계층

| 파일 | 소유하는 지식 |
|---|---|
| `targeting_domain.py` | 도메인 선언 로딩, **표면형 정규식 원자**, 값 어휘 파생, 코어 주입(`core_bindings()`) — 앵커 공급자 등록(`install()`)은 2026-08-05 삭제됨(소비자 `semantic_coverage` 폐기) |
| ~~`legacy_plan_compiler.py`~~ | (삭제됨 2026-08-05) 실행 슬롯 이름과 컴파일 규칙. 슬롯 **정의**는 `targeting_ir.SLOT_SHAPES` 가 계속 소유하고, SemanticPlan 노드를 그 슬롯으로 옮기던 계층만 사라졌다 |
| ~~`semantic_plan_bridge.py`~~ | (삭제됨 2026-08-05) 조립 지점. 남은 도메인 주입은 `targeting_domain.core_bindings()` 하나다 |
| `compositional_targeting.py` | 속성 값 카탈로그 로더(등급·상태 표면형 파생의 소스) — 실행 절반(슬롯 리졸버·SQL 컴파일러)은 2026-08-05 삭제 |
| `semantic_requirements.py` | 원문 의무 원장(시간 한정어 의무는 도메인 어휘 파생) |
| `metric_registry.py` / `member_filters_config.py` | 지표·회원 속성 값 사전 접근자 |

### 2-3. 카탈로그 JSON (선언)

| 파일 | 선언 내용 |
|---|---|
| `docs/data/runtime/semantics/targeting_domain.json` | 노드 필드 어휘, capability 축, 실행 플랜 컨테이너, 엔터티 별칭, 계수 단위, 시간 연산자 별칭·실행 연산자 표, 조건 라벨 (SemanticPlan 소비자는 폐기 — 파일 안 `_comment` 가 남은 소비자를 적는다) |
| `docs/data/runtime/semantics/semantic_capabilities.json` | 데이터 grain 과 적재 구간, 적재 부족 정책. 노드별 지원 축(`node_types`)은 2026-08-05 비워졌다 — 읽는 판정기도 컴파일러도 없다 |
| `docs/data/runtime/semantics/attribute_catalog.json` | 속성의 물리 바인딩, 적재 월 수. 이 바인딩으로 이력 SQL 을 만들던 계층은 폐기됐고, 값·표면형 파생과 적재 깊이 선언만 살아 있다 |
| `docs/data/runtime/sql/member_target_filters.json` | 속성 값 사전(등급 서열 `rank` 포함) — **값 어휘의 단일 소유자** |

---

## 3. requirement 레코드가 보존하던 것 (삭제됨 2026-08-05)

`requirement_ledger.Requirement` 하나가 조건 하나의 전 생애를 가졌다. 원장의 생산자
(SemanticPlanV2 파이프라인)가 폐기되며 모듈째 사라졌고, 지금 "어느 조건이 왜 막혔는가"는
`semantic_ir.missing_field_causes` + `failure_messages` 가 답한다.

| 필드 | 내용 |
|---|---|
| `requirement_id` | 노드 id 또는 `uncovered-N` |
| `label` | 사용자에게 보이는 조건 라벨(도메인 선언에서 파생) |
| `source_span` (+ `source_start`/`source_end`) | 사용자 원문 구절과 좌표 |
| `predicate` | 정규화된 술어(값 정규화 이후) |
| `temporal` | 범용 시간 연산자 + 인자 |
| `required_grain` | 필요한 데이터 grain |
| `capability` | 5축 판정 + 실패 코드 + 사유 |
| `candidate_slots` / `compiled_slot` | 후보 출력 슬롯과 실제로 채운 슬롯 |
| `validation` | 최종 귀결(outcome·failure_code·reason) |

귀결(`outcome`)은 닫힌 5종이다:
`compiled` / `pending` / `unsupported` / `failed` / `uncovered`.

**가짜 성공 차단**: `RequirementLedger.is_complete()` 는 조건이 하나 이상 있고 **전부**
`compiled` 일 때만 참이었다. 빈 원장은 완료가 아니었다. 같은 불변식은 지금 오디언스 계층이
갖는다 — 빈/누락 오디언스로 SQL 이 나가는 것을 `audience_admission` 이 막는다.

---

## 4. capability 판정의 5축 (삭제됨 2026-08-05)

`supported: true/false` 하나가 아니라:

| 축 | 의미 | 실패 시 코드 |
|---|---|---|
| `engine_supported` | 엔진이 그 의미 연산자를 정의하는가 | `unsupported_semantics` |
| `executor_supported` | 컴파일러 + 실행 빌더가 있는가 | `unsupported_semantics` |
| `required_grain` / `available_grain` | 필요한 데이터 grain이 확보됐는가 | `unsupported_data_grain` |
| `data_coverage` | 요청 구간이 적재 구간 안인가 | `data_unavailable` |
| `executable_in_request` | 이번 요청 범위에서 실행 가능한가 | (위 넷의 논리곱) |

이 5축을 선언하던 `semantic_capabilities.json` 의 `node_types` 는 비워졌고, 판정기
(`semantic_capability.py`)도 삭제됐다. 실행 가능 여부는 이제 컴파일러가 직접 답한다
(`event_compiler.validate_compiler_capability`) — 선언과 구현이 어긋날 자리를 없앤 것이다.

---

## 5. 실패 분류와 사용자 표시

`semantic_plan.FAILURE_CODES` 였던 닫힌 집합(모듈 삭제 후 이 표는 기록이다):

| 코드 | 뜻 | 사용자 표시 |
|---|---|---|
| `missing_argument` | 필수 인자 누락 | 확인 질문 |
| `ambiguous_requirement` | 의미 모호 | 확인 질문 |
| `unsupported_semantics` | 연산 의미 미지원 | **미지원** |
| `unsupported_data_grain` | 데이터 grain 미지원 | **미지원**(+개방 조건) |
| `data_unavailable` | 데이터 부재 | **미지원**(+적재 구간) |
| `validation_mismatch` | 의미검증/스키마 실패 | 내부 오류 → 확인 요청 |
| `execution_failure` | 실행 실패 | 내부 오류 → 확인 요청 |
| `internal_fault` | 내부 오류 | 내부 오류 → 확인 요청 |

**내부 오류·실행 실패는 절대 '미지원'으로 표시되지 않는다.** 판정 지점은
`requirement_ledger.outcome_for` 하나였고, 응답 렌더링은
`failure_messages.requirement_failure_report` 가 `has_internal_failure` /
`has_unsupported` 로 갈라 줬다 — 렌더러는 2026-08-05 삭제됐다(원장 생산자 폐기).
현재 `requirement_report` 는 빈 보고로 유지되고, 실패 사유는 `missing_field_causes` 가 말한다.

---

## 6. 시간 한정어

표현별 예외처리 대신 **범용 연산자 10종**으로 정규화한다.

| 연산자 | 뜻 | 필수 인자 |
|---|---|---|
| `AS_OF` | 지정 시점의 값 | — (앵커 없으면 최신) |
| `IMMEDIATELY_PRECEDING` | 직전 관측 시점의 값 | — |
| `WITHIN_INTERVAL` | 구간 안 어느 시점 | `interval` |
| `THROUGHOUT_INTERVAL` | 구간 내내 | `interval` |
| `AT_LEAST_ONCE_IN_INTERVAL` | 구간 중 최소 1회 | `interval` |
| `NEVER_IN_INTERVAL` | 구간 중 0회 | `interval` |
| `EVERY_SUBINTERVAL` | 모든 하위 구간 | `interval`, `subinterval_unit` |
| `UNCHANGED_THROUGHOUT` | 구간 내내 불변 | `interval` |
| `CHANGE_BETWEEN` | 두 시점 사이 전이 | — |
| `CHANGE_COUNT` | 구간 내 변경 횟수 | `interval`, `count` |

- 한국어 표면형(`기준`·`내내`·`직전` …)은 `targeting_domain._TEMPORAL_MARKER_TEMPLATES` 가
  소유하고, 템플릿 안의 **값 어휘는 `eq_filters` 에서 파생**한다(같은 낱말이 여러 모듈에
  재등장하지 않는다).
- 실행 연산자로의 사상은 `targeting_domain.json` 의 `temporal.execution_operators` 표다.
- 인자 결핍은 `missing_argument` — 미지원이 아니다.

---

## 7. 재방출(패치) — 삭제됨 2026-08-05

`semantic_reemission` 과 그 정책·보호 집합은 원장과 함께 사라졌다. 아래는 그 설계의 기록이다.

- 대상은 원장의 **미해결 요구사항만**(`pending` / `uncovered`).
  미지원·내부 사고는 재방출로 고쳐지지 않으므로 요청에 담지 않는다.
- 요청 구간 밖에 근거를 둔 노드는 병합하지 않는다(조용한 전체 재해석 금지).
- 이미 `compiled` 인 요구사항의 노드는 **보호 집합**이라 교체·삭제되지 않는다.
- 한 라운드가 끝나면 원장을 다시 만들고, 회귀(`compiled` → 그 외)가 있으면 **그 라운드를
  통째로 되돌린다**.
- 라운드 수는 정책값: `ReemissionPolicy(max_rounds=…)` 또는 환경변수
  `SEMANTIC_REEMISSION_MAX_ROUNDS`(기본 1).

---

## 8. 확장 경계 — JSON 만으로 되는 것 / 코드가 필요한 것

`targeting_domain.extension_boundary()` 가 이 표의 기계 판독본이다
(`tests/test_generic_core_layering.py` 가 공허해지지 않게 검사한다).

### JSON 선언만으로 열리는 것

| 하고 싶은 일 | 고칠 파일 |
|---|---|
| 새 회원 속성 축(시점·이력 대상) 추가 | `attribute_catalog.json` 항목 1개 |
| 새 속성 값·동의어·서열 추가 | `member_target_filters.json` `eq_filters` 1행 |
| 새 집계 도메인·그레인·정렬 방향 등 노드 필드 값 | `targeting_domain.json` `vocabularies` |
| 새 엔터티 별칭 / 계수 단위 | `targeting_domain.json` |
| 기존 시간 연산자의 실행 연산자 이름 변경·추가 | `targeting_domain.json` `temporal.execution_operators` |
| 지표·노드의 지원 여부, 필요 grain, 적재 구간 | `semantic_capabilities.json` |
| 적재 월 수가 늘어 다월 연산 개방 | `attribute_catalog.json` `snapshot_months_available` 숫자 |

> 값 어휘가 늘면 시간 한정어 감지도 **자동으로** 함께 열린다(정규식이 값 사전에서 파생하므로).

### 코드 변경이 필요한 것

| 하고 싶은 일 | 고칠 곳 |
|---|---|
| **새로운 시간 연산 의미** | `temporal_semantics` 연산자 1개 + `targeting_domain` 실행 사상 + 컴파일러 + 검증기 |
| 새 오디언스 표현 종류 | `audience_schema` 대수 + `event_ir` 노드 + `event_compiler` lowering (예전에는 `semantic_plan` 클래스 + `semantic_capabilities.json` 선언 + 컴파일러 핸들러였다) |
| 새 값 종류(kind) | `semantic_normalizers` 정규화기 (`semantic_plan.VALUE_KINDS` 는 모듈과 함께 삭제) |
| 새 실행 슬롯 | `targeting_ir.SLOT_SHAPES` + 실행 빌더 (`legacy_plan_compiler` 는 삭제) |
| 같은 연산자의 새 표면 표현 | `targeting_domain._TEMPORAL_MARKER_TEMPLATES` 한 줄 |

이 표의 기계 판독본은 `targeting_domain.extension_boundary()` 이고, 둘이 어긋나면 그 함수가
권위다(테스트가 함수 쪽만 잰다).

경계를 한 문장으로: **"데이터가 늘어난다"는 선언, "의미가 늘어난다"는 코드.**

---

## 9. 실측 상태 (2026-08-02)

| 항목 | 결과 |
|---|---|
| pytest | 854 passed / 19 skipped (계층 분리 전 773) |
| `db_swap_preflight.py` | PASS |
| 26종 라이브(`/target-sql`, HEAD 기준선) | **SQL 출고 0/26** |
| 26종 라이브(계층 분리 후) | SQL 출고 0/26, 단 #6·#9 가 해석 단계를 통과해 후단 게이트까지 진행 |

**중요**: 0/26 은 이 계층 분리가 만든 상태가 아니다. 같은 스크립트를 변경 전 워킹트리
(`git stash` 후 HEAD)에 돌려도 0/26 이다. 원인은 그 앞 단계(SemanticPlanV2 이행에서
결정론 백필 7종을 삭제하고 방출을 전부 LLM 에 맡긴 것)이며, 그 이행은 26종 라이브
재실행 없이 마무리됐다. 현재 최대 실패 군집은 **LLM 이 카탈로그에 없는 지표 id 를
지어내거나(예: 장바구니 지표에 주문 지표 id) 범주 값을 수치 필드에 넣는 것**이다.

이번 변경으로 그 군집에 두 가지 일반 대책이 들어갔다:
1. `node_field_vocabularies` 선언에서 파생한 **닫힌 어휘 결속 안내**를 구조화 프롬프트에 주입
   (새 결속은 JSON 한 줄).
2. 실패 문구를 **필드 + 기대 값 종류**로 낮춰(요구사항 원장) 오배선이 즉시 보이게.

남은 일은 방출 품질 자체(모델 A/B, 지표 어휘의 스키마 enum 노출)이며 이 문서의 범위 밖이다.

---

## 10. 드리프트 가드

| 테스트 | 막는 것 |
|---|---|
| `tests/test_generic_core_layering.py` | 코어에 도메인 낱말/도메인 import 가 스며드는 것, 선언만으로 확장이 안 되는 것 |
| `tests/test_temporal_semantics.py` | 표현별 분기 부활, 코어가 표면형 패턴을 직접 컴파일하는 것 |
| ~~`tests/test_requirement_ledger.py`~~ | (삭제됨 2026-08-05) 요구사항 원장은 SemanticPlanV2 파이프라인의 산출물이었고, 그 파이프라인이 폐기되며 원장의 생산자도 사라졌다 |
| ~~`tests/test_semantic_reemission.py`~~ | (삭제됨 2026-08-05) 재방출 계층과 함께 사라졌다 — 전체 재생성·보호 집합 훼손을 막던 가드는 이제 **없다**(재방출 자체가 없다) |
| ~~`tests/test_semantic_pipeline.py`~~ | (삭제됨 2026-08-05) 파이프라인과 함께 사라졌다. '가짜 성공' 차단은 `tests/test_audience_admission.py` 와 `tests/test_retired_axes_fail_close.py` 로 옮겨 살아 있다 |
| `tests/test_single_interpretation_path.py` | 원문 재해석 계층(정규식 백필·결핍 사후 삭제)의 부활 |
| `tests/test_retired_axes_fail_close.py` | 폐기 축(등급/상태 이력·전이)이 다시 SQL 을 내는 것 + 복귀 축(프로필 스칼라 지표·캠페인당 평균)이 조용히 다른 뜻으로 바뀌는 것 |
| `tests/test_revived_axes_event_ir_only.py` | 복귀 축이 Event IR 말고 두 번째 경로(옛 슬롯·폴백)를 얻는 것 |
| `tests/test_member_scalar_metric_contract.py` | 전역 순위 지표가 회원별 스칼라 자리에 들어와 모집단 정책이 사라지는 것 |
| `tests/test_composite_aggregate_lowering.py` | 복합 집계식이 `AVG`로 접히거나 다중 컬럼 키가 구분자 결합으로 되돌아가는 것 |
| `tests/test_composite_aggregate_sql_results.py` | 위 두 축의 SQL 이 문법은 맞고 **값이 다른** 상태로 나가는 것(픽스처 DB 실행) |
| `tests/test_no_semantic_plan_residue.py` | 삭제된 SemanticPlan 모듈·플랜 키·LLM 스키마 표면의 부활 |
