# Codex 작업 메모

최종 갱신: 2026-08-04

> **2026-08-05 폐기 고지.** 이 메모가 `SemanticPlanV2` / `semantic_pipeline` /
> `semantic_plan_bridge` 를 현재형으로 적은 곳은 전부 과거다 — 그 중간표현과 스택은
> 2026-08-05 모듈 파일까지 삭제됐다. 아래 "canonical 의미 소유 경로" 도식에서
> `SemanticPlanV2` 단계는 빠지고, LLM 이 `audience_requirement.expression`(canonical Event IR)을
> 직접 낸 뒤 도메인·카탈로그 결속 → 컴파일로 이어진다.

이 문서는 현재 작업 트리의 변경 내용과 저장소 전체 하드코딩 감사 결과를 다음 작업자가 이어서 진행할 수 있도록 정리한 인수인계 메모다. 정식 구조 이동 계획은 `docs/plans_ir_decoupling.md`와 `docs/plans_event_ir_only.md`를 기준으로 한다.

## 1. 지금까지 한 작업

### Audience 표면 단일 소스화

- `plan_schema.PlanKey`에 `audience: bool | None` 분류를 추가했다.
- 모든 `CONDITION` 키가 audience 여부를 명시하도록 강제하고, 파생 키는 audience 분류를 가질 수 없게 했다.
- 하위 audience 컨테이너 `("target_user", "exclude")`의 소유권을 `plan_schema.AUDIENCE_CONTAINERS`로 옮겼다.
- 두 번째 audience 언어로 판정할 최상위 키를 `plan_schema.audience_keys()`에서 파생하도록 했다.
- `canonical_event_ir_grounding.has_empty_legacy_audience_surface()`가 자체 리터럴 목록 대신 위 registry를 사용하도록 변경했다.
- `legacy_audience_migration.AUDIENCE_CONTAINERS`는 호환성을 위해 `plan_schema` 값을 재수출하도록 변경했다.
- audience 분류 누락, 파생 키 오분류, 현재 gate 표면, 자기참조 키, 소비자 drift를 검사하는 테스트를 추가했다.
- cut-over 자산이 온라인 실행 경로로 유입되지 않고 legacy 표면과 rollback 가능성이 유지되는지를 검사하는 테스트 파일을 추가했다.

### 저장소 전체 하드코딩 감사

- Python 파일 365개를 정적 검색했고, 테스트 152개와 런타임 후보 188개를 분리했다.
- 런타임 모듈의 리터럴 컬렉션 선언 341개를 후보로 수집했다. 이 숫자는 결함 수가 아니며, 닫힌 계약과 보안 allowlist도 포함한다.
- 완전히 동일한 선언을 여러 파일이 소유하는 중복군 10개를 확인했다.
- 물리 테이블/컬럼 하드코딩 기준선 184개를 확인했다.
  - `graph_rag.py`: 79
  - `member_filters_config.py`: 48
  - `event_compiler.py`: 24
  - `condition_evaluation_ir.py`: 14
  - 기타: 19
- `tools/physical_binding_inventory.py`가 `.venv`를 제외하지 않아 제3자 패키지까지 세며 184개를 431개로 오염시키는 문제를 확인했다.
- `semantic_ir` 생산자, 직접 대입, nested mutation, schema/validator, SemanticPlan/Event IR 투영 경로를 추적했다.
- 시간 기본값, 월/년 고정 일수 환산, 금액·비율의 `float` 사용, 코드 기반 물리 바인딩 fallback을 전수 검색했다.

### 테스트 확인

- literal 관련 타깃 테스트: 56 passed.
- 선택한 구조/계약 테스트: 76 passed, 2 failed.
  - 두 실패 모두 `.venv`가 physical-binding inventory에 포함된 문제였다.
- 확인 시점의 전체 테스트 결과: 2143 passed, 26 skipped, 5 failed.
  - `tests/test_canonical_event_ir_grounding.py::test_canonical_semantic_lowering_failure_never_calls_legacy_bridge`
  - `tests/test_capability_discovery_cli.py::test_candidate_cli_writes_only_to_explicit_path_and_promotion_is_blocked`
  - `tests/test_module_size_ratchet.py::test_no_module_exceeds_its_ceiling`
  - `tests/test_physical_binding_ratchet.py::test_total_does_not_regress`
  - `tests/test_physical_binding_ratchet.py::test_no_file_regresses_individually`

### 2026-08-04 후속 작업 확인

- physical-binding scanner를 로컬 설치물과 분리해 184건 기준선을 복구했다.
- Capability candidate CLI 테스트가 삭제된 `active_state` 식별자에 묶이지 않고 현재 gap을 사용한다.
- literal scanner의 상대 날짜가 기준일 누락/오류 시 시스템 날짜로 fallback하지 않는다.
- 활성 `query_structurer` 경로의 `semantic_ir` writer를 하나로 모으고 typed `SemanticOutcome`을 추가했다.
- query structurer enum과 API 외부 DB 목록을 각각 타입/connection registry에서 파생했다.
- 전체 테스트: 2161 passed, 26 skipped, 2 failed.
  - 삭제 예정 Event IR 레거시 테스트 1건(요청에 따라 작업 범위 제외)
  - 변경되지 않은 `graph_rag.py`의 기존 module-size 상한 초과 1건
- 위 두 기존 항목을 제외한 활성 경로: 2153 passed, 26 skipped, 1 deselected.

감사 과정에서는 구현 소스를 수정하지 않았다. 테스트 실행 중 생성된 임시 artifact는 정리했고, 기존 작업 트리 변경은 보존했다.

## 2. 내린 결정과 이유

### `semantic_ir`는 canonical 실행 IR로 확장하지 않는다

`semantic_pipeline.project_semantic_ir()`는 `SemanticPlanV2`에서 `semantic_ir`를 파생하고 `operations`를 항상 빈 배열로 만든다. 신규 LLM 출력 표면에서도 `semantic_ir`는 애플리케이션 소유 필드로 제외된다. 따라서 `query_structurer/semantic_ir.py`를 범용 실행 IR로 키우지 않고, typed Outcome/Receipt의 레거시 JSON projection으로 축소한다.

Canonical 의미 소유 경로는 다음으로 고정한다.

```text
원문 literal 추출
→ SemanticPlanV2
→ 도메인/카탈로그 기반 의미 결속
→ Canonical Event IR
→ 주입된 물리 바인딩으로 컴파일
→ typed Outcome/Receipt
```

### 모든 하드코딩을 제거하지 않는다

다음은 코드에 남겨야 하는 닫힌 계약이다.

- Event IR의 comparison/arithmetic/aggregate operator 대수
- AST discriminator와 schema/capability version
- 명시적인 상태 머신
- 보안 allowlist와 차단 식별자
- Python callable을 보유하는 dispatch/registry
- `plan_schema`처럼 계약과 분류를 소유하는 registry

제거 또는 단일 소스화할 대상은 다음 조건 중 하나를 만족하는 하드코딩이다.

- 같은 의미를 두 곳 이상이 독립적으로 소유한다.
- 새 도메인 값 하나를 추가하려고 generic/core 모듈을 수정해야 한다.
- 문맥 없이 업무 의미를 추측한다.
- 현재 시각, DB, 정책 기본값 등 실행 context를 숨긴다.
- 설정 장애 시 과거 시스템의 값으로 조용히 fallback한다.

### literal 표면과 업무 의미를 분리한다

`semantic_ir.py`의 `회/번/건 → order_count`는 문맥 없는 추측이다. `3회` extractor는 span, 숫자, surface unit 또는 일반 `count`까지만 만들고, `order_count`, `login_count`, `campaign_send_count` 선택은 확정된 metric/source 문맥을 가진 binder가 수행한다. 문맥이 없으면 원문을 보존한 clarification/unsupported로 fail-close한다.

### 결정성은 context 주입으로 보장한다

- 상대 날짜 해석에는 timezone-aware 기준시각을 명시적으로 주입한다.
- 의미 계산 경로에서 `date.today()`와 `datetime.now()` fallback을 제거한다.
- month/year는 30/365일로 바꾸지 않고 calendar duration/offset으로 보존한다.
- 금액과 비율은 내부적으로 `Decimal`을 사용하고 직렬화 정책을 한 곳에서 관리한다.
- 운영 로그의 생성 시각처럼 의미에 영향을 주지 않는 clock 사용은 별도로 유지할 수 있다.

### schema와 validator는 한 선언에서 파생한다

`SEMANTIC_IR_LLM_JSON_SCHEMA`와 `validate_semantic_ir()`가 필드, enum, 타입을 중복 소유하고 이미 검증 강도가 어긋난다. 기존 프로젝트의 dataclass/`FieldSpec` 패턴을 사용해 구조 schema와 파서를 한 선언에서 파생하고, 별도 semantic validator는 상태 간 불변식과 cross-reference만 담당하게 한다.

### 물리 바인딩은 필수 주입하고 fail-close한다

IR에는 논리 event/field만 남기고 테이블, 컬럼, join 표현은 검증된 catalog/compile context에서 주입한다. 설정이 없거나 파손되면 구 DB 코드 기본값으로 돌아가지 않고 readiness/preflight에서 blocked/system failure로 처리한다. 이 작업은 새 계획을 만들지 않고 `docs/plans_ir_decoupling.md`의 미착수 Wave 5.3을 갱신해 이어간다.

### 현재 audience 단일소스화 작업을 먼저 안정화한다

현재 `plan_schema` 변경은 같은 audience 표면 목록을 여러 파일이 소유하던 문제를 해결하는 방향이므로 되돌리지 않는다. 하드코딩 구조 이동과 겹치는 파일이 많아, 이 변경의 테스트를 먼저 green으로 만든 뒤 `semantic_ir`와 물리 바인딩 작업을 시작한다.

## 3. 확인된 주요 위험 위치

- `query_structurer/semantic_ir.py`
  - 문맥 없는 counter 업무 의미 매핑
  - 비교어·기간·금액 regex의 중복 소유
  - 모든 숫자의 `float` 경유
  - handwritten JSON Schema와 manual validator 중복
  - 실제로 생산되지 않는 legacy `operations` 계약
- `query_structurer/audience_execution.py`
  - `empty_semantic_ir(status="unsupported")` 생성 후 nested mutation으로 완성하여 중간 invalid 상태 발생
- `calendar_window.py`, `event_ir.py`, `semantic_plan_bridge.py`, `graph_rag.py`
  - 의미 계산 경로의 암묵적 시스템 시계 사용
- `event_ir.py`, `condition_normalizers.py`, `targeting_ir.py`, `graph_rag.py`
  - month=30, year=365 고정 환산의 중복
- `semantic_normalizers.py`, `targeting_ir.py`, `query_structurer/semantic_ir.py`
  - 금액·비율·순위 값의 `float` 또는 `int(round(...))` 변환
- `event_ir.py`
  - legacy projection이 source와 무관하게 모든 count를 `order_count`, field sum을 `purchase_amount`로 투영
- `graph_rag.py`
  - behaviors/categories/interests/channels/offers/objectives의 코드 소유
  - targeting lexicon 코드 fallback
  - `top_n=100` 등 의미 결과에 영향을 주는 숨은 기본 정책
- `event_compiler.py`, `condition_evaluation_ir.py`, `member_filters_config.py`, `graph_rag.py`
  - CRM 물리 테이블/컬럼과 구 시스템 fallback
- `query_structurer/types.py`와 `query_structurer/schema.py`
  - Intent, Complexity, MetadataOperator, Operation, OutputFormat 중복 선언
- `api.py`와 `db_connections.py`
  - 외부/read-only DB 목록 중복 선언

## 4. 현재 수정 파일 목록

### 기존 작업 트리 변경

- `canonical_event_ir_grounding.py`
  - audience 표면 목록을 `plan_schema`에서 파생하도록 변경
- `legacy_audience_migration.py`
  - `AUDIENCE_CONTAINERS`를 `plan_schema`에서 재수출하도록 변경
- `plan_schema.py`
  - `PlanKey.audience`, `AUDIENCE_CONTAINERS`, `audience_keys()` 추가
  - CONDITION/DERIVED audience 분류 불변식 추가
- `tests/test_plan_schema_registry.py`
  - audience 표면 단일 소스와 분류 계약 테스트 추가
- `tests/test_cutover_execution_isolation.py` — 미추적 파일
  - 온라인 실행 경로의 cut-over 모듈 import 금지
  - legacy 표면 보존과 rollback 계약 테스트

### 이번 메모 작업에서 추가한 파일

- `NOTES.codex.md`

하드코딩 감사 자체에서는 위 구현 파일을 추가로 수정하지 않았다.

## 5. 남은 할 일

### Phase 0 — 기준선과 감사 도구 복구

- [ ] 현재 audience 단일소스화 변경의 실패 테스트를 분석하고 green으로 만든다.
- [x] `tools/physical_binding_inventory.py`에서 `.venv`, `venv`, `site-packages`, `build`, `dist`를 제외했다.
- [x] 설치 패키지 집합과 무관하게 inventory가 동일하다는 테스트를 추가했다.
- [x] 실제 제품 코드 기준 physical-binding count를 184건(테이블 57 / 컬럼 127)으로 다시 확인했다.
- [x] 나머지 실패를 분리했다. `capability_discovery_cli`는 테스트 식별자 `active_state`가 현재
  projection의 gap/concept에 없는 기존 fixture drift이고, `module_size_ratchet`은 변경되지 않은
  `graph_rag.py`가 HEAD의 기존 상한 17,330을 751줄 초과한 기준선 문제다. Event IR 레거시 실패는
  삭제 예정 경로이므로 이번 작업 범위에서 제외했다.

### Phase 1 — 현행 의미 characterization

- [x] literal span과 overlap 계약을 고정했다.
- [x] 절대 날짜, 상대 날짜, 금액, 비율, counter, duration, comparison golden test를 추가했다.
- [x] 같은 입력과 같은 context에서 결과가 byte-for-byte 동일함을 검사했다.
- [x] 기준일 누락/오류 시 시스템 날짜로 fallback하지 않도록 구현하고 테스트를 추가했다.
- [ ] `Decimal` 정밀도 및 JSON round-trip 테스트를 추가한다.
- [x] AST 기반으로 활성 `query_structurer` 경로의 `semantic_ir` 직접 대입·mutating method·nested
  mutation을 탐지하고 `write_semantic_ir()` 한 곳만 쓰기를 허용한다. 삭제 예정 Event IR 레거시
  경로의 writer는 범위에서 제외했다.

### Phase 2 — typed Semantic Outcome

- [x] frozen dataclass 기반 `SemanticOutcome` 모델과 상태별 factory를 만들었다.
- [ ] schema/구조 파서를 한 선언에서 파생한다.
- [ ] cross-reference와 상태 불변식만 별도 validator에 남긴다.
- [x] `query_structurer/semantic_ir.py`를 호환 façade로 유지했다.
- [x] 신규 typed outcome의 `operations` 생산을 구조적으로 금지하고 빈 배열은 wire projection으로만 남겼다.

### Phase 3 — literal scanner/normalizer/binder 분리

- [ ] extractor가 surface evidence만 만들도록 한다.
- [ ] counter의 업무 metric 결정을 domain-aware binder로 이동한다.
- [ ] `aggregate_parser_rules.json`, normalization lexicon, targeting domain catalog의 소유권을 명확히 나눈다.
- [ ] 상대 날짜에 기준시각과 timezone을 필수 주입한다.
- [ ] month/year를 calendar 타입으로 보존한다.
- [ ] 금액·비율을 `Decimal` 내부 표현으로 전환하고 호환 serializer를 둔다.

### Phase 4 — 도메인 어휘 단일 소스화

- [ ] `graph_rag.py`의 behaviors/categories/interests/channels/offers/objectives를 domain catalog facet으로 이동한다.
- [ ] prompt, schema, merge allowlist, validator가 같은 catalog에서 파생되게 한다.
- [x] `query_structurer/types.py`와 `schema.py`의 enum 중복을 제거했다.
- [ ] 한글 숫자, comparison, aggregate/window operator 중복은 공통 선언 또는 명시적 subset/parity 계약으로 정리한다.

### Phase 5 — 물리 바인딩 Wave 5.3

- [ ] `docs/plans_ir_decoupling.md`의 오래된 369건 수치를 scanner 수정 후 갱신한다.
- [ ] `event_compiler` registry를 필수 주입형 catalog로 바꾼다.
- [ ] `condition_evaluation_ir`에서 논리 IR과 물리 binding을 분리한다.
- [ ] `member_filters_config.CODE_DEFAULTS`와 `graph_rag` fallback을 제거하거나 배포 시 검증되는 생성물로 전환한다.
- [x] `api.py`의 DB 목록을 `db_connections` registry에서 파생한다.
- [ ] PR마다 184 기준선을 감소시키고, 설정 누락 시 0명 SQL이 아니라 명시적 unsupported/error가 나오는지 검사한다.

### Phase 6 — Event IR/레거시 정리

- [ ] Event IR의 parser/schema/serializer를 node definition registry에서 파생한다.
- [ ] source를 무시하는 count/sum legacy metric 투영을 catalog 기반 무손실 mapping으로 교체한다.
- [ ] 숨은 `top_n`/limit 기본값을 명시적 policy 또는 clarification으로 바꾸고 receipt를 남긴다.
- [ ] legacy reader 사용량이 0이 된 뒤 `semantic_ir.operations`, 예전 validator, 호환 re-export를 제거한다.
- [ ] wire 변경은 core Event IR 버전과 분리된 capability/version으로 배포한다.

## 6. 롤아웃 및 완료 기준

- 한 번에 전체를 바꾸지 않고 단계별 작은 PR로 진행한다.
- 구조 이동 PR은 기존 wire 결과를 유지하고 characterization test를 통과해야 한다.
- 의미 변경은 모호한 입력을 fail-close하는 경우에만 명시적으로 승인하고 golden diff를 케이스별 검토한다.
- 구/신 경로 shadow 비교는 SQL 문자열만이 아니라 canonical semantic snapshot도 비교한다.
- rollback은 호환 adapter/명시적 feature switch로 보장하고, 숨은 코드 기본값으로 대체하지 않는다.
- 최종 완료 조건:
  - 동일 입력+데이터+기준시각의 결과가 결정적이다.
  - 새 표면어는 language registry 변경만으로 추가할 수 있다.
  - 새 업무 metric은 domain/operator registry 등록으로 추가할 수 있다.
  - generic literal extractor에 도메인 metric ID와 물리 DB 이름이 없다.
  - 논리 IR에 테이블·컬럼 이름이 없다.
  - schema와 runtime parser가 같은 선언에서 파생된다.
  - `semantic_ir` 신규 producer가 중앙 projector 하나뿐이다.
  - physical-binding 래칫이 지속적으로 감소하고 전체 테스트가 green이다.
