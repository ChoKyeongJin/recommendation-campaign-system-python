# 실시간 외부 조건 Resolver

외부 조건은 Query Plan이 종류를 분류하고, Resolver가 공식 공급자에서 현재 값을 확정한 뒤, Mapper가
CRM 필터로 변환한다. LLM과 GraphRAG는 현재 지역 목록을 만들거나 저장하지 않는다.

```text
사용자 원문
  → Query Plan.external_conditions (pending, 조건 종류만)
  → ExternalConditionService / ResolverRegistry
  → ResolverResult (공급자·관측/만료 시각·표준 지역)
  → AdministrativeRegionMapper
  → compound_dimension_filters (OR-of-AND, 지역 계층 보존)
  → 기존 회원 SQL 컴파일러
```

`resolved` 결과와 생성 필터는 Query Plan의 `external_condition_results`,
`external_condition_resolution`, `compound_dimension_filters`에 스냅샷으로 남는다. 공급자 원문은 저장하지
않고 SHA-256 응답 식별자만 저장한다. Resolver 실패, 빈 결과, 미지원 조건, 오래된 응답, 부분 지역 매핑
실패, 또는 필터 0개는 모두 SQL 생성 전에 `EXTERNAL_CONDITION_RESOLUTION_FAILED`로 차단된다.

## 설정

| 환경변수 | 기본값/의미 |
|---|---|
| `EXTERNAL_CONDITIONS_ENABLED` | `true`; 전체 Resolver 실행 스위치 |
| `EXTERNAL_CONDITION_CATALOG_PATH` | `docs/data/external_condition_catalog.json` |
| `EXTERNAL_REGION_MAPPING_PATH` | `docs/data/external_region_mapping.json` |
| `EXTERNAL_MEMBER_VALUE_INDEX_PATH` | `docs/data/member_value_index.json` |
| `KMA_WEATHER_ALERT_API_URL` | 기상청 기상특보 현황 REST endpoint |
| `KMA_WEATHER_ALERT_API_KEY` | 공공데이터포털 서비스 키. `DATA_GO_KR_SERVICE_KEY`로 대체 가능 |
| `KMA_WEATHER_ALERT_TIMEOUT_SECONDS` | `5` |
| `KMA_WEATHER_ALERT_CACHE_TTL_SECONDS` | `600` |
| `KMA_WEATHER_ALERT_MAX_AGE_SECONDS` | `21600` |

키와 공급자 원문은 로그에 남기지 않는다. 운영 배포 전에는 공공데이터포털의
[기상청 기상특보 정보](https://www.data.go.kr/data/15000415/openapi.do) 활용 신청 및 응답 계약을 확인한다.

## 조건 추가

1. `external_condition_catalog.json`에 조건 코드, 별칭, 문맥, 기본 target basis를 등록한다.
2. `ExternalConditionResolver`를 구현한다. 공급자별 응답을 `ResolverResult`로 정규화하고 관측/만료 시각을
   반드시 제공한다.
3. Resolver를 `build_default_service()`의 Registry에 등록한다.
4. 새 target basis가 필요하면 별도 Mapper를 구현해 Registry/서비스 조립부에서 연결한다.
5. 정상, 빈 결과, 시간 초과, 잘못된 응답, 오래된 응답, 부분 매핑 실패, 캐시 만료 테스트를 추가한다.

핵심 Query Plan 생성 코드나 SQL 생성 코드에는 조건 코드별 `if/elif`를 추가하지 않는다. CRM이 시군구
코드 컬럼을 제공하게 되면 매핑 파일과 Mapper 출력 컬럼을 코드 기반으로 전환하고, OR-of-AND 구조는
그대로 유지한다.
