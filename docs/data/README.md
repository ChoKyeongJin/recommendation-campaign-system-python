# 데이터 자산 구조

`docs/data`의 JSON은 주 역할에 따라 분리한다. 파일을 추가할 때는 소비 코드보다 먼저 아래 소유권을
기준으로 디렉터리를 고른다. SQL·Markdown 원본은 현재 루트에 유지한다.

| 경로 | 역할 | 예시 |
|---|---|---|
| `runtime/language/` | 문장 표면어, 단위, 정규화, 안내 문구 | `parser_lexicon.json`, `aggregate_parser_rules.json` |
| `runtime/semantics/` | 의미 개념, 분석 계약, 지원 가능 조합 | `concept_catalog.json`, `requirement_capabilities.json` |
| `runtime/sql/` | 논리 조건을 실제 테이블·컬럼으로 바꾸는 설정 | `member_target_filters.json`, `metrics/*.json` |
| `runtime/external/` | 날씨 등 외부 조건과 지역 매핑 | `external_condition_catalog.json` |
| `runtime/policies/` | 업무 정책 | `business_policies.sample.json` |
| `generated/` | 빌드나 DB 조회로 다시 만들 수 있는 산출물 | `schema_catalog.json`, `rag_knowledge_base.json` |
| `schemas/` | JSON 형식 검증 계약 | `aggregate_parser_rules.schema.json` |
| `test_baselines/` | 운영에서 읽지 않는 테스트 래칫 기준선 | `module_size_baseline.json` |

## 관리 원칙

- 자동 생성 파일은 직접 고치기보다 해당 빌더를 실행한다.
- 운영 로더의 기본 경로와 문서 예시는 반드시 실제 위치와 함께 변경한다.
- 파일명은 참조 파일 API의 기존 basename 계약을 위해 디렉터리 전체에서 유일하게 유지한다.
- 새 지표는 `runtime/sql/metrics/<metric_id>.json`에 한 파일로 추가한다.
