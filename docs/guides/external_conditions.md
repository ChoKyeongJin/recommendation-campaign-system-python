# 상식 기반 외부·개념 조건 해석

기본 경로는 기상청 등 실시간 공급자를 호출하지 않는다. Query Plan이 `폭염지역` 같은 표현과
논리적 대상(`member.residence`)만 분류하고, 일반지식 LLM이 현재 DB 레지스트리에서 동적으로 발견한
opaque capability/value ID 후보 중 일부를 주관적으로 선택한다. 서버가 ID를 다시 검증한 뒤 실제
CRM 필터를 붙인다.

```text
사용자 원문
  → Query Plan.external_conditions (조건 종류 + target_basis)
  → schema/value/filter registry에서 실행 capability 동적 발견
  → 일반지식 LLM (opaque ID 부분집합만 선택)
  → 서버의 ID·근거·신뢰도·논리 역할 검증
  → dimension_filters / target_user 실행 슬롯
  → 기존 회원 SQL 컴파일러
```

`resolved` 결과와 생성 필터는 Query Plan의 `external_condition_results`,
`external_condition_resolution`, `conceptual_resolutions`,
`conceptual_targeting_resolution`에 감사 영수증으로 남는다. 결과에는
`basis=general_knowledge_non_realtime`, `realtime=false`, 모델·confidence·rationale·catalog digest를
명시한다. 모델 실패, 닫힌 후보 밖 ID, 낮은 신뢰도, 근거 불일치, 실행 필터 미부착은 SQL 생성 전에
차단된다.

## 설정

| 환경변수 | 기본값/의미 |
|---|---|
| `CONCEPTUAL_TARGETING_LLM` | `true`; 상식 grounding 실행 스위치 |
| `OPENAI_API_KEY` | 상식 grounding 사용 시 필수. 없으면 순수 결정론 질의는 기존대로 실행하지만, 이미 분류된 외부·상식 조건은 누락시키지 않고 차단 |
| `OPENAI_CONCEPTUAL_TARGETING_MODEL` | 미지정 시 요청의 LLM 모델 |
| `OPENAI_CONCEPTUAL_TARGETING_REASONING_EFFORT` | `low`; 후보 비교 추론 강도 |
| `CONCEPTUAL_TARGETING_CONFIDENCE_THRESHOLD` | `0.65` |
| `CONCEPTUAL_TARGETING_CACHE_TTL_SECONDS` | `86400` |
| `CONCEPTUAL_TARGETING_TIMEOUT_SECONDS` | `30`; SDK 내부 재시도는 끄고 grounding 서비스가 재시도 예산을 소유 |
| `EXTERNAL_CONDITION_CATALOG_PATH` | `docs/data/external_condition_catalog.json` |

이 결과는 실시간 관측이나 공식 판정이 아니다. 캠페인용 주관적 세그먼트이며 모델·후보 데이터·프롬프트
버전이 바뀌면 선택값도 달라질 수 있다.
일반 Query Planner가 성별·연령·등급·행동·관심사·채널·제외 조건 같은 실행 슬롯을 추측해 직접
추가하는 것은 허용하지 않는다. 원문에서 결정론적으로 확인된 값은 rules 후보가 소유하고, 주관적
표현은 이 문서의 closed-ID grounding 영수증을 통과한 경우에만 실행 조건으로 승격된다.
`현재`, `오늘`, `실시간`, `특보 발령`처럼 최신 상태를 명시적으로 요구한 조건은 일반지식으로
약화하지 않는다. 이 경우 실시간 공급자가 명시적으로 주입되지 않았다면 확인 필요 상태로 차단된다.

## 조건 추가

1. 표현의 종류·별칭·`default_target_basis`만 `external_condition_catalog.json`에 등록한다.
2. 실행 가능한 필드는 `member_target_filters.json`, 값은 `member_value_index.json`, 설명은
   `schema_catalog.json`에 선언한다.
3. target registry에는 물리 컬럼과 별개로 `target_basis`, `default_capability`를 둔다. 예를 들어
   `member.residence.default` 역할이 현재 레지스트리의 시도 컬럼에 연결된다.
4. 정상, provider 실패, 잘못된 ID, 낮은 신뢰도, 필터 변조·미부착, 캐시 만료 테스트를 추가한다.

핵심 Query Plan 생성 코드나 SQL 생성 코드에는 조건 코드별 `if/elif`를 추가하지 않는다. CRM이 시군구
컬럼을 바꾸더라도 registry의 논리 역할 매핑과 값 인덱스를 갱신하면 capability가 다시 발견된다.

호출자가 `external_condition_service`를 명시적으로 주입하는 레거시/특수 경로는 계속 지원한다.
단, 각 `resolved` 결과는 Query Plan에 실제로 붙인 필터와 정확히 같은 `generated_filter`를 반환해야
하며, 누락되거나 달라지면 컴파일 전에 차단된다. 기본 `retrieve()`는 그 서비스를 만들거나 기상청
네트워크를 호출하지 않는다.
