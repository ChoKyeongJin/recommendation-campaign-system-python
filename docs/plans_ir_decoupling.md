# IR 결합 완화 실행 플랜

> 생성 2026-07-31. 근거: IR 결합 전수 분석(에이전트 8) + 전략안 4개 독립 설계 + 위험 클러스터 6개 코드 검증 + 3렌즈 채점 합성(에이전트 14).

## 진행 상태 (2026-08-01 갱신)

**Wave 1~4 완료. Wave 5 미착수(사유는 아래).** 테스트 21 failed → 0.
현재 `1365 passed, 14 skipped, 1 xfailed`, preflight PASS, CI 신설(테스트가 이미지 푸시 게이트).

| 웨이브 | 상태 | 핵심 결과 |
|---|---|---|
| Wave 1 안전망 | 완료 | CI 신설, 죽은 참조 19곳 정정, 골든 정상화, 가드 5종, preflight green, env 격리 |
| Wave 2 fail-close | 완료 | OR 게이트 fail-OPEN 수정, 안내 라벨 6종 침묵 제거, behaviors 단일 소스화, 레지스트리 강등 가시화·cwd 독립, 설정 교차참조 가드 |
| Wave 3 계측 | 완료 | 멱등성 3축, auto 필터 순서 계약(매직 인덱스 제거), 물리 바인딩 인벤토리 369건 래칫, plan 키 AST 인벤토리, API 응답 계약 게이트 |
| Wave 4 단일소스화 | 완료 | plan_schema 레지스트리 신설 + ir_snapshot 파생 전환, 미분류 13→0, 분류 모순 0, SQL 리터럴 7→1, 정규화 2→1, unsupported reason 닫힌 집합 |
| Wave 5 구조 이동 | 미착수 | 착수 게이트(W3-5)는 확보. 부채는 측정·고정됨 |

> **2026-08-01 정정 — capability_registry 삭제.** 커밋 `ac924ff`("정리")가 `capability_registry.py`(209줄),
> `docs/data/condition_ownership_policy.json` 을 삭제하고 `requirement_capabilities.json` 을
> supported/message 만 남게 축소했다. 따라서 W1-4 (a) 의 "validate_capabilities(177-206) 3중 배선"과
> 위험표의 클러스터1-①·2-⑩ 은 **복원이 아니라 신규 작성** 대상이다 — 후속 플랜은
> `docs/plans_canonical_ir_capability.md`(Phase A-3: targeting_ir.CONDITION_SPECS facet 기반 validator
> 신규 작성 + preflight 배선)가 권위다. 같은 문서 Phase 0 이 logical_expression 잔해(생산자 0 슬롯·
> 미작동 LOGICAL_OR_COMPILER env·trace 허위 출처)를 처분했다 — 본문의 관련 서술은 그 시점 이전 기록이다.

> **2026-08-01 추가 — 빌더 흡수 파일럿.** 이 문서가 여러 곳에서 예로 드는 `region_member_count_target`
> 은 더 이상 존재하지 않는다. 표현 하나가 슬롯·빌더·정규식·분류 레지스트리 5개 파일에 걸쳐 있던
> 것을 **등록형 집계 IR**(analytical_intent + analytics_registry + aggregation_requirements + sql_ast)로
> 흡수하고 전용 빌더 `build_region_member_count_sql_candidate` 를 삭제했다. 아래 W3-4/W4-4 서술의
> 그 키는 '미분류였다가 분류된' 사례가 아니라 **'흡수로 사라진'** 사례로 읽어야 한다.
> 새 지역 축(동/읍면동 등)은 이제 `docs/data/runtime/semantics/analytics_registry.json` 항목 하나로 열린다.

**단일소스화 성과**

| 대상 | 이전 | 현재 |
|---|---|---|
| SQL 리터럴 이스케이프 | 7곳 복제 | `sql_dialect.quote_literal` (AST 스캔으로 재발 차단) |
| 값 정규화 | 2곳 복제 | `common_utils.normalize_entity_term` |
| plan 키 분류 | 4개 목록 | `plan_schema` (ir_snapshot 은 파생 뷰) |
| 미분류 plan 키 | 13건 | 0건 |
| 분류 모순 | 1건(policy_constraints) | 0건 |
| order_count_behaviors | 3소비자 각자 | `member_filters_config` |
| unsupported reason | 3곳 손배선 | `unsupported_reasons.ALL` 닫힌 집합 |
| auto 필터 경계 | `[:5]` 매직 인덱스 | 이름 기반 분할 + 순서 계약 |

**Wave 2~4에서 고친 실제 결함**

- **OR 논리식 fail-OPEN**: 게이트가 손 나열이라 `cart_absence`·`metric_trend` 를 몰랐다. leftover 검사가 이 집합만 훑으므로 집합 밖 슬롯은 채워져 있어도 통과 → 조건이 SQL 없이 사라진다. `SLOT_SHAPES` 파생으로 교체.
- **안내 침묵 삭제**: '남는 조건' 목록이 라벨 사전 멤버십으로 걸러져 6종이 통째로 빠졌다.
- **lapsed_buyer 의미 반전**: 컴파일·파서 배선이 없는 죽은 선언인데 그 표현이 `no_purchase`(무구매)로 흘러간다 — 구매 EXISTS 와 no_purchase 가 동시에 생성된다. `_supported: false` 로 명시하고 반전 사실을 테스트로 고정(구현은 별건).
- **설정 로더 cwd 의존 5곳**: cwd 가 레포 밖이면 레지스트리가 조용히 빈다. 모듈 기준 절대경로 + `REGISTRY_HEALTH` 로 강등 가시화.
- **objective_repurchase 누락**: 코드가 `f"objective_{objective}"` 로 조회하는데 짝이 없어 신호가 조용히 버려졌다.

**측정된 부채(허용목록으로 고정, 하향 전용 래칫)**

| 항목 | 수 | 해소 대상 |
|---|---|---|
| 소스 권위 번들 재실행 비멱등 | 1건 (`or_of_age_with_and_threshold`) | W5-2 |
| `build_sql_result` 입력 변형 | 1건 (`co_purchase_same_product`) | W5-1 |
| SQL 도 사유도 없이 끝나는 케이스 | 4건 | failure_stage 축 |
| 소스 하드코딩 물리 바인딩 | 369건 (graph_rag 214) | W5-3 |

**Wave 5 를 멈춘 이유**: W5-1(빌더 순수화)은 플랜이 "가장 조용한 실패 벡터"로 지목한 항목이고,
계측 결과 위험이 구체적으로 확인됐다 — `capability_check`/`output_contract` 는 파생 키라 골든에서
제외되고 커버리지가 19케이스 표본뿐이라, 순수화를 잘못하면 **모든 골든이 초록인 채 API 응답만
얇아진다**. 밀어붙이는 대신 부채를 측정값으로 고정했고, 늘어나면 CI 가 잡는다.

---

## 요약

먼저 안전망(CI·골든·삭제된 계약 113개·preflight)을 복구해 "빨강이 진짜 빨강"이 되게 한 뒤, 지금 사용자에게 잘못 나가는 fail-close 구멍 5종을 고치고, 그 위에서만 계측→단일소스화→구조 이동 순으로 옮긴다.

## 순서 근거

가드가 먼저 와야 하는 이유는 취향이 아니라 측정 결과다. 세 전략안의 검증 문장 대부분이 "pytest 통과" 또는 "골든 스냅샷 무변경"에 걸려 있는데, 실측하면 `pytest tests/ -q` = 21 failed / 1223 passed / 14 skipped 이고 `.github/workflows` 에는 `docker-publish.yml` 하나뿐이라 그 판정을 내려 줄 실행기가 아예 없다. 빨간 테스트는 "내 리팩터가 깼다"와 "원래 깨져 있었다"를 구분하지 못하므로, 이 상태에서 5중 사본을 파생으로 바꾸거나 build_sql_result 를 순수화하면 조용한 슬롯 소실을 잡을 수단이 없다.

더 중요한 사실은 세 안 중 어느 것도 인지하지 못한 것이다: 안전망은 "꺼져 있는" 게 아니라 **3일 전에 삭제됐다**. 213741e(94개) + ce39f68(19개) = 테스트 113개가 지워졌고, 그 안에 test_capability_contract / test_semantic_requirements / test_sql_builder_registry / test_plan_decisions / test_plan_resolver / test_slot_ownership / test_retrieve_trace_stages / test_sql_delivery_fail_closed 처럼 이 플랜이 건드릴 모듈의 계약이 통째로 들어 있다. 따라서 "알려진 실패 21건"은 생존한 30% 위의 숫자이고, 21에 상한을 걸어 래칫으로 고정하면 **이미 드리프트된 상태를 정상으로 봉인**한다. W1-2(회수·트리아지)를 W1-1의 대장 상한 확정보다 앞이나 같은 웨이브에 둔 이유가 이것이다.

Wave 2를 계측(Wave 3)보다 앞에 둔 것은 의도적 순서 변경이다. 전략 1은 14단계 중 8단계가 순수 계측이라 3~4주간 사용자 체감이 0이고 본인 fails_if ⑥이 "팀이 견디지 못하고 이탈하면 절반 깔린 안전망 + 새 관리대상 5개만 남는 순손실"이라고 자인한다. 반면 전략 2가 찾아낸 두 결함(_LOGIC_CONDITION_SLOTS 누락으로 OR leaf 게이트가 fail-OPEN, _UNSUPPORTED_CONDITION_LABELS 6종 누락으로 '남는 조건' 안내에서 장바구니·캠페인 조건이 침묵 삭제)은 지금 실제로 오답을 내는 지점이고, 수정 방향이 fail-close 라 프로젝트 원칙과 일치하며, 골든 코퍼스가 이미 `LOGICAL_OR_COMPILER=1` 로 돌아(cases.json env 실측) 검증까지 된다. 이 둘은 Wave 1이 끝나면 즉시 안전하게 칠 수 있는 저비용·고효용 수정이다.

구조 변경 전에 확정돼야 하는 것은 셋이다. (1) **의미 골든의 판정력** — 현재 골든 17건 실패가 전부 `source_spans` 차이이고 의미 필드는 완전 동일함을 직접 확인했다(no_purchase_prefix: baseline `[]` vs 현재 `[{start:0,end:11}]`). 축을 분리하지 않으면 "의미 회귀"와 "구간 이동"이 같은 빨강으로 보여 재생성 회피가 반복된다. (2) **멱등/순수성의 실측치** — 소스권위 번들 재실행 비멱등이 정확히 1건(or_of_age_with_and_threshold)이라는 측정이 있어야 W5-2의 성공이 기계 판정 가능하다. (3) **build_sql_result 하류 소비 지점** — `capability_check`/`output_contract` 는 ir_snapshot.DERIVED_PLAN_KEYS 에 있어 의미 골든에서 제외되고 tests/ 전체에 실질 커버리지가 없는데(수동 입력 1건뿐), graph_rag.py:17487 이 build_sql_result 이후 같은 query_plan 에서 읽는다. 즉 deepcopy 순수화는 IR 골든·SQL 골든·멱등 허용목록을 전부 초록으로 유지한 채 API 응답을 조용히 비울 수 있다. 그래서 W5-1은 W3-5(하류 계약 테스트) 없이는 착수 금지다.

물리 스키마 축(Wave 3의 W3-3 → Wave 5의 W5-3)은 다른 트랙과 병행 가능하게 떼어 놨다. 전략 2·3은 이 축 기여가 0인데, "실DB는 계속 바뀐다 → 스키마 지식을 소스 밖으로"는 이 프로젝트의 명시 제약이다. 세는 장치(인벤토리 래칫)가 이관보다 먼저 와야 되돌아감을 막을 수 있고, preflight 가 지금 ok=false(종료코드 1)라는 사실이 그 축의 가드가 실제로 꺼져 있음을 증명한다.

---

## Wave 1 — 안전망 복구 — 테스트를 돌게 만들고, 삭제된 계약을 회수하고, 빨강을 진짜 빨강으로

**목표.** 모든 PR에서 pytest·preflight가 실행되고, 남은 빨강은 사유가 적힌 대장에만 존재하며, 3일 전 삭제된 계약 자산 113개가 트리아지되어 기준선이 사실이 된다.

**여기서 멈춰도.** 여기서 멈춰도: 새 이미지 import 폭탄이 제거되고, 회귀가 CI에서 잡히기 시작하며, 삭제된 계약 중 지금도 유효한 것들이 되살아나고, DB 스왑 프리플라이트가 실제 게이트가 된다. 구조는 하나도 안 옮겼지만 '지금 잘못돼도 아무도 모르는' 구간이 사라진다.

### W1-1. 테스트 실행 배관 + CI 게이트 신설 + 알려진 실패 대장(strict xfail)

- **공수** M / **선행** 없음 (대장 상한은 복원 없이 현행 실패 수로 확정 — W1-2 범위 변경으로 트리아지 대기 불필요)
- **해소 대상** 축④ 죽은 가드 — CI 잡 부재. 이후 모든 항목의 검증 수단.
- **파일** `requirements.txt`, `pytest.ini`, `.github/workflows/tests.yml`, `.github/workflows/docker-publish.yml`, `tests/conftest.py`, `tests/known_failures.json`, `tests/test_known_failures_ratchet.py`

**작업.** (a) requirements.txt 에 jsonschema 추가 — aggregate_parser_config.py:31 이 모듈 임포트 시점에 import 하는데 requirements 에 없어, Dockerfile 의 pip install 만 거친 새 이미지에서는 `import graph_rag` 자체가 ModuleNotFoundError 로 죽는다(현 개발 환경엔 우연히 설치돼 있어 잠복 상태). (b) pytest.ini 신설: testpaths=tests, pythonpath=. tests(현재 tests/conftest.py 의 sys.path.insert 와 rootdir 자동삽입에 암묵 의존 — golden_support 를 top-level 로 import 하는 관례 보존), --strict-markers. (c) .github/workflows/tests.yml 신설, 두 잡으로 나눈다: `contracts` 잡(빠른 계약 + `python db_swap_preflight.py`)과 `suite` 잡(pytest tests -q). docker-publish.yml 의 build-and-push 에 `needs: [contracts]` 를 걸어 계약 실패 시 이미지가 안 나가게 한다. (d) tests/known_failures.json 대장 + tests/conftest.py 훅: 대장에 실린 nodeid 만 pytest.xfail(strict=True). strict 가 핵심 — 고쳐지면 XPASS 로 빨강이 되어 대장에서 지우도록 강제한다. 엔트리는 {nodeid, reason, found_at, owner} 필수. (e) tests/test_known_failures_ratchet.py: 항목 수 상한 + 필수 필드 + 존재하지 않는 nodeid 금지. **대장 상한은 21이 아니라 W1-2 트리아지 결과를 반영해 확정한다** — 21은 생존한 테스트만 센 숫자다.

**검증.** 깨끗한 venv 에서 `pip install -r requirements.txt && python -m pytest tests/ -q` → exit 0, 요약이 `1223 passed, 21 xfailed, 14 skipped`(현 실측 21 failed / 1223 passed / 14 skipped 와 일치). tests/test_known_failures_ratchet.py::test_entries_have_required_fields, ::test_no_stale_nodeids, ::test_count_under_ceiling. 대장에서 항목 하나를 지우면 그 테스트가 다시 빨강이 되는지 확인. GitHub Actions 에서 contracts/suite 두 잡이 모두 실행되는지 확인.

**위험.** xfail 대장은 잘못 쓰면 실패를 정당화하는 도구가 된다 — 전에는 최소한 21건이 빨강으로 보였는데 초록으로 위장될 수 있다. 완화: strict=True, 항목 수 상한 래칫, reason/owner/found_at 필수, 대장 파일 변경은 리뷰 필수를 CODEOWNERS 에 명시. jsonschema 추가는 이미 설치돼 동작 확인된 상태라 런타임 동작 변경 없음.

### W1-2. 소스의 죽은 테스트 참조 제거 — 복원 대신 정직화 (사용자 결정으로 범위 변경)

- **공수** M / **선행** 없음 (편집 대상이 주석·docstring 이라 배관과 무관)
- **해소 대상** 축④ 죽은/사라진 가드 중 "소스가 거짓 안전감을 주는" 측면
- **파일** `targeting_ir.py`, `graph_rag.py`, `compiler_strategies.py`, `semantic_requirements.py`, 그 외 전수 스캔 결과

> **2026-07-31 범위 변경.** 원안은 삭제된 테스트 113개를 `git checkout 213741e^ -- tests/` 로 회수해
> green/red/api-changed 로 트리아지하는 것이었다. 사용자 결정으로 **복원하지 않는다** — 삭제는 의도적이었고,
> 전량 복원은 red 대량 유입으로 W1-1 대장의 신호가 잡음에 묻히는 비용이 크다.
> 대신 삭제된 테스트를 계약 근거로 인용하는 소스의 죽은 참조를 제거해, 소스가 거짓말하지 않게 만든다.

**작업.** 프로젝트 전체(.py + docs/*.md)에서 존재하지 않는 테스트를 인용하는 지점을 전수 스캔해 제거한다.
알려진 지점: `targeting_ir.py:16-17`("모든 fact_join kind 는 정확히 하나의 빌더가 소유한다를 테스트가 강제한다"),
`graph_rag.py:4046`(test_deterministic_filter_registry 인용), `graph_rag.py:849`(test_metric_registry 인용),
`graph_rag.py:878-882`("가시적 실패는 테스트가 잡는다"), `graph_rag.py:22572-22574`, `compiler_strategies.py:13`,
`semantic_requirements.py:26`. 전수 스캔으로 추가 지점을 찾는다.

편집 원칙 — **불변식은 살리고 허위 근거만 지운다**:
- 주석이 서술하는 도메인 지식(예: "fact_join kind 는 정확히 하나의 빌더가 소유한다")은 **보존**한다.
  이 지식이 사라지면 다음 사람이 규약 자체를 모른다.
- 없어진 테스트를 근거로 든 부분("테스트가 강제한다", "tests/test_x.py 참조", "테스트가 잡는다")만 제거하거나,
  가드가 실제로 없다는 사실을 드러내는 정직한 표기로 바꾼다.
- 새 가드가 필요한 불변식은 W1-4 에서 **옛 테스트 복원이 아니라 최소 규모로 새로 작성**한다.

**검증.** `tests/test_doc_claims.py` 신설: 소스의 모든 `tests/test_*.py` 인용을 AST/정규식으로 수집해
실재 파일 집합에 포함되는지 단언(`test_no_source_comment_cites_missing_test`). 도입 시점에 0건이어야 하고,
이후 테스트를 지우면서 인용을 남기면 즉시 빨강이 된다.

**위험.** 주석을 지우면서 불변식 서술까지 날리면 지식이 소실된다 — 이것이 이 항목의 유일한 실질 위험이다.
완화: 지우기 전에 각 지점의 `invariant_claimed` 를 먼저 추출해 기록하고, 교체 문구를 사전에 확정한 뒤 치환한다.
프로덕션 동작 변경은 0(주석·docstring 만 편집).

### W1-3. 골든 기준선 정상화 — IR 골든을 의미축/출처축으로 분리 재생성 + regex 래칫 갱신

- **공수** M / **선행** W1-1
- **해소 대상** 축② 검증 계기 복구. 이후 모든 구조 이동의 유일한 신뢰 가능 판정 축.
- **파일** `tests/test_ir_golden_corpus.py`, `tests/golden_support.py`, `tools/regen_ir_goldens.py`, `tests/golden/snapshots/`, `tests/golden/spans/`, `docs/data/regex_inventory_baseline.json`, `tests/known_failures.json`

**작업.** 현재 골든 실패 17건은 전부 source_spans 만 다르다(직접 확인: no_purchase_prefix 는 baseline `"source_spans": []` vs 현재 `[{"start":0,"end":11}]`, 의미 필드 완전 동일). ir_snapshot.snapshot() 은 provenance 를 담고 parser_shadow 는 비교 직전 semantic_fields.strip_provenance 로 걷어내는데, 골든만 provenance 까지 통째로 비교해 스팬 기록 개선이 곧 17건 빨강이 되고 아무도 재생성하지 않았다. → tests/golden/snapshots/<id>.json 은 strip_provenance(snapshot) 만 담고, 출처는 tests/golden/spans/<id>.json 으로 분리한다. test_ir_golden_corpus.py 는 의미 골든 대조(회귀=빨강)와 출처 골든 대조(별도 테스트, 실패 메시지에 '의미는 같고 구간만 바뀌었다'를 명시)를 나눈다. tools/regen_ir_goldens.py 를 두 파일 생성으로 갱신하되 **의미 필드 변화가 있는 케이스는 재생성 거부**(수동 검토 강제). 함께 test_regex_inventory_ratchet 실패 1건 해소: 늘어난 패턴(_BALANCE_DEFER_PATTERN, _THRESHOLD_CUE_RE, analytical_intent._OUTPUT_ACTION_RE 등)을 docs/data/runtime/language/parser_lexicon.json 으로 이관하거나 `tools/regex_inventory.py --set-baseline --reason` 으로 사유와 함께 상향. 재생성 전에 스팬 변화를 만든 커밋을 `git log -S` 로 특정해 '개선인가'를 판정한 기록을 커밋 메시지에 남긴다.

**검증.** `pytest tests/test_ir_golden_corpus.py tests/test_ir_schema_contract.py tests/test_provenance_contract.py tests/test_regex_inventory_ratchet.py -q` 전부 통과. 신규 테스트명: test_ir_golden_corpus.py::test_semantic_snapshot_matches_baseline, ::test_span_snapshot_matches_baseline. 역검증: 임의 슬롯 값을 코드에서 바꾸면 의미 골든만 깨지고, 스팬 기록만 바꾸면 출처 골든만 깨진다. 대장 항목 수 21→3(실회귀 test_aggregate_semantic_conflict::test_satisfiable_event_expressions_still_compile / test_conceptual_targeting::test_resolved_purchase_window_cannot_return_as_conceptual_unsupported / test_event_set_ownership::test_event_and_legacy_member_condition_share_one_supported_canonical_and).

**위험.** 재생성이 '회귀를 축복하는 도장'이 될 수 있다(regen 스크립트 docstring 이 이미 경고). 특히 slot_ownership._source_compatible 이 스팬으로 소유권을 판정하므로 잘못된 스팬을 골든으로 굳히면 W5-5 의 판단 근거가 오염된다. 완화: 스크립트가 diff 가 source_spans 에만 국한됨을 자동 판정해 출력하고, 의미 필드 변화 시 재생성 거부.

### W1-4. 죽은 가드 4종 소생 + 소스 주석이 인용하는 허위 테스트 참조 제거

- **공수** M / **선행** W1-1 (복원분이 없으므로 가드는 전부 신규 작성)
- **해소 대상** 축④ 전체. 입력2 클러스터1-①②⑥⑦, 클러스터2-⑧⑩
- **파일** `tests/test_capability_contract.py`, `tests/test_builder_ownership_contract.py`, `tests/test_join_paths_contract.py`, `tests/test_sql_literal_mirror_parity.py`, `tests/test_doc_claims.py`, `capability_registry.py`, `compiler_strategies.py`, `join_paths.py`, `targeting_ir.py`, `api.py`, `db_swap_preflight.py`

**작업.** 호출 0건임을 직접 확인한 가드들을 실행 경로에 연결한다. (a) capability_registry.validate_capabilities(177-206): 이 모듈은 어디서도 import 되지 않는다(grep 0건). 지금 돌리면 위반 0건이므로 tests/test_capability_contract.py(W1-2 복원분)가 `validate_capabilities()==[]` 를 강제하고, db_swap_preflight.run_preflight() 에 '검사 5: capability↔실행자산' 섹션을 추가하며, api.py startup 에서 try/except 로 감싸 app.state.capability_errors + /health 필드로만 노출한다(절대 raise 하지 않는다). (b) targeting_ir.fact_join_kinds(): graph_rag.py:131 에서 import 만 되고 호출 0건인데 targeting_ir.py:16-17 과 graph_rag.py:22572-22574 docstring 은 '테스트가 강제한다'고 주장한다 → tests/test_builder_ownership_contract.py 신설, 불변식 4개: 모든 fact_join kind 가 정확히 1빌더 소유(현재 25 kind, 미소유 0, 중복 0), 레지스트리 kind ⊆ CONDITION_SPECS kind, fact_join=False 인데 소유된 kind 는 허용목록 {campaign_responses} 만, kind 미소유 빌더는 복합 컴파일러 3종만. (c) join_paths: render_join_line 호출 0건 → tests/test_join_paths_contract.py: 모든 JoinPath 의 테이블/조인 컬럼이 docs/data/generated/schema_catalog.json 에 실재하고 FORBIDDEN_PRODUCT_JOIN_KEYS 를 쓰지 않는다. (d) SQL 리터럴 미러 parity: compiler_strategies.py:13 이 인용하는 tests/test_capability_contract.py 가 삭제됐었다 → graph_rag._sql_quote / compiler_strategies._quote / event_compiler._sql_quote 3중 미러 바이트 동일, _sql_nlike_contains 동일을 강제(W4-5 에서 단일화할 때까지의 임시 가드). 마지막으로 tests/test_doc_claims.py 메타테스트 신설: 전 .py 소스에서 정규식 `tests/test_[a-z0-9_]+\.py` 로 인용된 경로가 실재하는지 강제 — 확인된 허위 인용 8곳(targeting_ir.py:16-17, graph_rag.py:22573/4046/849/878-882, compiler_strategies.py:13, semantic_requirements.py:26, capability_registry.py:16)을 실체 생성 또는 문구 정정으로 해소한다. join_paths.py:16-17 의 'graph_rag 빌더가 JOIN_PATHS[name] 으로 조인 라인을 얻는다'는 거짓이므로 W5-3 이관 전까지 사실대로 정정.

**검증.** 신규 5개 파일 전부 초록. 테스트명: test_capability_contract.py::test_registry_static_validation_clean, test_builder_ownership_contract.py::test_every_fact_join_kind_has_exactly_one_owner / ::test_registry_kinds_subset_of_condition_specs, test_join_paths_contract.py::test_join_paths_columns_exist_in_catalog, test_sql_literal_mirror_parity.py::test_quote_mirrors_are_byte_identical, test_doc_claims.py::test_cited_test_files_exist. 역검증: requirement_capabilities.json 의 compiler_strategy 를 오타로 바꾸면 (a) 실패, _sql_target_builder_registry 에서 kind 하나를 지우면 (b) 실패, join_paths 에 없는 컬럼을 넣으면 (c) 실패, compiler_strategies._quote 를 한 글자 바꾸면 (d) 실패.

**위험.** api.py 기동 시 validate_capabilities 예외가 서비스를 죽일 수 있다. 완화: try/except 로 로그+헬스 필드만, 절대 raise 금지(fail-close 승격은 별도 결정). test_doc_claims 도입 즉시 8건 red 가 되므로 같은 PR 에서 실체 생성 또는 문구 정정을 마쳐야 한다.

### W1-5. db_swap_preflight 를 실제 게이트로 승격 + 현재 FAIL 3건 해소

- **공수** S / **선행** W1-1 (preflight 테스트는 복원이 아니라 신규 작성)
- **해소 대상** 축③ 물리 스키마. 입력2 클러스터3-①②
- **파일** `docs/data/runtime/sql/member_target_filters.json`, `db_swap_preflight.py`, `graph_rag.py`, `tests/test_db_swap_preflight.py`, `tests/test_portability_guard_presence.py`, `docs/operations/db_portability_audit.md`

**작업.** `python db_swap_preflight.py --json` 실행 결과 ok=false / 종료코드 1 / problems 3건임을 직접 확인했다(문서 docs/operations/db_portability_audit.md:130-160 은 A/B/C/D 전부 ✅ 로 표기 — 문서와 현실이 갈라져 있다). 원인 2건은 실제 결합 결함이다. ① member_target_filters.json 의 aggregate_targets.metrics.distinct_category_count 가 column_table 로 **논리 심볼** 'product' 를 쓰는데 그 논리→물리 매핑(product→CRM_CM_PRODUCT)이 graph_rag.py:25208 _PRODUCT_MASTER_TABLE 코드 상수에만 있어 설정만 읽는 도구가 해석 불가 → member_target_filters.json 최상위에 `table_symbols` 섹션 신설(product/order_detail/order_header/member/cart), graph_rag 에 `_table_symbol(name)` 접근자를 두고 _PRODUCT_MASTER_TABLE/_PRODUCT_SCOPE_TABLE 참조 5곳(17746, 24833, 25217, 25281, 25910)을 교체, preflight 의 _walk_table_refs/_configured_table_columns 가 table_symbols 로 먼저 해석한 뒤 카탈로그와 대조하고 미등록 심볼은 별도 보고. ② campaign_response_targets.contact_member_list 가 campaign_date_table 을 선언하지 않아 Z_CAMPAIGN 소유 CAMP_SDATE 가 Z_CAMP_MBR 소속으로 오귀속 → `"campaign_date_table": "Z_CAMPAIGN"` 한 줄 추가. 이어 preflight 를 pytest 로 끌어올린다: 복원한 tests/test_db_swap_preflight.py 에 `test_preflight_is_green_on_current_repo` 추가.

**검증.** `python db_swap_preflight.py --json` → ok:true, problems 0, 종료코드 0. tests/test_db_swap_preflight.py::test_preflight_is_green_on_current_repo, ::test_logical_table_symbol_resolves(미선언 심볼 'widget' 은 반드시 실패), ::test_cross_table_date_column_uses_declared_owner. tests/test_portability_guard_presence.py::test_required_portability_guards_exist — {test_db_swap_preflight, test_join_key_guard, test_registry_single_source} 각 파일이 실재하고 최소 1개 test_ 함수를 갖는지(테스트 삭제 커밋 한 방에 계약이 증발하는 것을 막는 유일한 층). 골든 SQL 무변화(값이 동일하므로 바이트 불변이어야 함).

**위험.** table_symbols 접근자화가 5개 참조 지점을 건드리므로 상품 스코프/마스터 테이블이 관여하는 SQL 이 영향권. 값이 동일하면 바이트 불변이어야 하고 골든 코퍼스가 즉시 확인해 준다. 문서 정정(✅ → 실제 상태)을 같은 PR 에 포함하지 않으면 다음 사람이 또 속는다.

### W1-6. 테스트 env 전역 누수 차단 — 측정 도구 자체의 오염 제거

- **공수** S / **선행** W1-1
- **해소 대상** 축⑤ env 분기. Wave 3 멱등성 측정의 신뢰 전제 — 안 고치면 멱등성 테스트조차 실행 순서에 따라 다른 답을 낸다.
- **파일** `tests/golden_support.py`, `tests/conftest.py`, `tools/regen_ir_goldens.py`, `tests/test_env_isolation.py`

**작업.** tests/golden_support.py:41 apply_corpus_env 가 os.environ 을 프로세스 전역으로 영구 변경하고 복원하지 않는다(직접 확인: `for key, value in env.items(): os.environ[str(key)] = str(value)` 뒤 restore 없음). cases.json 의 env 는 {TARGET_OBJECT_LLM_FALLBACK:false, SURFACE_LEXICON_LLM:off, CONDITION_SLOT_LLM_FALLBACK:off, LOGICAL_OR_COMPILER:1} 이므로, 골든 테스트가 먼저 돌면 이후 모든 테스트가 LOGICAL_OR_COMPILER=1 상태로 실행된다 = 테스트 순서 의존 오염. monkeypatch 스코프 fixture 로 전환하고, tests/conftest.py 의 모듈 임포트 시점 os.environ 설정도 autouse fixture 로 옮긴다. 재생성 스크립트(tools/regen_ir_goldens.py)는 CLI 라 전역 설정을 유지해도 되므로 함수를 두 갈래(fixture 용 / CLI 용)로 나눈다.

**검증.** `pytest tests/ -q -p no:randomly` 와 `pytest tests/test_ir_golden_corpus.py tests/ -q`(골든 먼저) 결과가 동일해야 한다(현재는 누수로 보장 없음). tests/test_env_isolation.py::test_corpus_env_does_not_leak_across_tests — 골든 플랜 생성 후 os.environ 에 LOGICAL_OR_COMPILER 가 남지 않음을 단언.

**위험.** fixture 전환 시 build_plan 을 직접 호출하는 테스트가 env 없이 돌아 결과가 바뀔 수 있다. 완화: build_plan 자체가 fixture 를 요구하도록 시그니처를 바꾸지 말고, monkeypatch 를 받는 컨텍스트 매니저 형태로 감싸 호출부 변경을 최소화.

---

## Wave 2 — 사용자 대면 fail-close 복원 — 지금 조용히 틀리고 있는 것부터

**목표.** 조건이 조용히 사라지거나 안내에서 침묵 삭제되거나 설정이 조용히 격하되는 경로 5종을 명시 fail-close/unsupported 로 바꾼다.

**여기서 멈춰도.** 여기서 멈춰도: OR 논리식에서 장바구니·지표추세 조건이 무성 삭제되던 것이 막히고, '남는 조건' 안내에서 사라지던 6종이 표시되며, 레지스트리가 조용히 빈 채로 오답을 내던 3경로가 명시 차단되고, 설정 교차참조가 풀렸을 때 침묵 대신 실패한다. 구조는 그대로지만 정확성은 실제로 개선된다.

### W2-1. 논리식 leaf 게이트 fail-OPEN 구멍 — _LOGIC_CONDITION_SLOTS 를 targeting_ir 파생으로

- **공수** S / **선행** Wave 1 전체(특히 W1-3 의미 골든)
- **해소 대상** 축② 다중소유가 fail-close 구멍으로 발현. 입력2 클러스터2-①
- **파일** `targeting_ir.py`, `graph_rag.py`, `tests/test_logic_leaf_slot_coverage.py`

**작업.** graph_rag.py:26894-26901 _LOGIC_CONDITION_SLOTS(27개) 와 targeting_ir.SLOT_SHAPES 중 container=='target_user'(17개)의 차집합이 정확히 ['cart_absence','metric_trend'] 임을 직접 확인했다(입력2는 entity_set_condition 을 포함한 3개로 봤으나 그것은 target_user 컨테이너 슬롯이 아니라 plan 최상위 키다 — 별도 축이므로 W4-4 에서 처리). 게이트는 graph_rag.py:27074-27079 의 `leftover = [slot for slot in _LOGIC_CONDITION_SLOTS if ...]; if leftover: raise LeafUnsupported`. 목록에 없는 슬롯은 leftover 계산에 들어오지 않으므로 Leaf 가 '전부 처리됨'으로 통과 = fail-close 가 fail-OPEN 으로 뒤집힌다. 두 슬롯 모두 LLM 이 직접 채우는 정식 슬롯이고(targeting_ir.py:587-599 SlotShape), LOGICAL_OR_COMPILER 는 docker-compose.yml:72 / docker-compose.ec2.yml:74 에서 프로덕션 '1' 이다. → targeting_ir 에 `target_user_condition_slots()` 파생 함수 추가(SLOT_SHAPES 의 target_user 슬롯 ∪ SlotShape 가 없는 coarse 슬롯 명시 frozenset), graph_rag 의 리터럴을 그 호출로 교체. _LOGIC_HANDLED_SLOTS 는 '이 컴파일러가 실제로 렌더 가능한 슬롯'이라 의미가 달라 graph_rag 에 남긴다.

**검증.** tests/test_logic_leaf_slot_coverage.py::test_condition_slots_cover_all_slot_shapes(`target_user_condition_slots() >= {target_user SlotShape 키}`), ::test_rule_plan_slots_are_gated(`set(_build_rule_query_plan(...)['target_user']) <= _LOGIC_CONDITION_SLOTS`), ::test_cart_absence_in_or_expression_fails_closed — LOGICAL_OR_COMPILER=1 에서 '장바구니가 없거나 최근 30일 구매금액 10만원 이상인 회원'을 컴파일해 plan['unsupported'] 또는 SQL 에 cart_absence 가 반드시 나타남(둘 다 아니면 조용한 드롭으로 실패). 골든 코퍼스가 이미 LOGICAL_OR_COMPILER=1 로 돌므로 회귀 즉시 감지.

**위험.** 지금까지 조용히 통과하던 OR 질의가 unsupported 로 바뀐다 — 사용자 체감으로는 회귀처럼 보이는 정확성 개선. 완화: 배포 전 골든 코퍼스로 영향 건수를 세고, clarification 문구가 함께 반환되는지 확인하며, 커밋 메시지에 '조용한 오답보다 fail-close' 원칙 적용임을 명시.

### W2-2. '남는 조건' 안내의 침묵 누락 — 미지원 라벨 6종 + 멤버십 필터 제거

- **공수** S / **선행** Wave 1
- **해소 대상** 축② 다중소유. 입력2 클러스터2-①(라벨 부분)
- **파일** `graph_rag.py`, `tests/test_unsupported_threshold_attributes.py`, `tests/test_remaining_condition_labels.py`

**작업.** _UNSUPPORTED_CONDITION_LABELS(graph_rag.py:22994-23031, target_user 라벨 25개)에 실제 target_user 슬롯 6종이 없음을 직접 확인했다: campaign_buy_amount, campaign_buy_count, campaign_response_frequency, cart_aggregate, cart_retention, cell_rate_target. 그런데 _remaining_condition_labels(11462-11470)가 `f"target_user.{slot}" in _UNSUPPORTED_CONDITION_LABELS` 로 필터링하므로, 이 6종은 '남는 조건' 안내에서 조용히 지워진다 — 사용자는 자기 조건이 무시됐다는 사실조차 모른다. → 6종 라벨을 채우고 멤버십 필터를 제거한다. **결정적으로 필터 제거 후 bare 인덱싱을 쓰면 안 된다** — 이 루프는 선언된 레지스트리가 아니라 런타임 plan['target_user'] 의 실제 키를 순회하므로, 런타임에만 나타나는 키 하나면 KeyError 로 응답이 죽거나 원시 슬롯명이 사용자에게 샌다. `_UNSUPPORTED_CONDITION_LABELS.get(path)` + 폴백 라벨('기타 조건') + 미등록 발생 시 logger.warning 형태로 구현한다.

**검증.** tests/test_remaining_condition_labels.py::test_every_target_user_slot_has_label(SLOT_SHAPES target_user 슬롯 전원이 라벨 보유), ::test_unknown_runtime_slot_falls_back_not_raises(가짜 슬롯을 plan 에 심어도 예외 없이 폴백 라벨), ::test_cart_retention_appears_in_remaining_labels('장바구니 30일 이상 보관 + 미지원 속성' 프롬프트에서 안내에 장바구니 조건 포함). 기존 tests/test_unsupported_threshold_attributes.py 의 INTERNAL_TERMS 누출 금지 단언이 그대로 초록.

**위험.** 라벨 없는 슬롯에서 내부 이름이 사용자에게 샐 수 있다. 완화: 폴백 라벨을 내부명이 아닌 일반 문구로 두고, 미등록 발생을 로그로 올려 W4-2 의 ko_label facet 이관 시 잡히게 한다.

### W2-3. order_count_behaviors 3소비자 불일치 + lapsed_buyer 죽은 설정 fail-close

- **공수** M / **선행** Wave 1
- **해소 대상** 축② 다중소유 + 축④ 죽은 설정. 입력2 클러스터2-②
- **파일** `member_filters_config.py`, `targeting_ir.py`, `confidence.py`, `canonical_targeting.py`, `graph_rag.py`, `tests/test_order_count_behavior_parity.py`

**작업.** 설정과 코드가 이미 값으로 갈라져 있음을 직접 확인했다: member_target_filters.json 의 order_count_targets.behaviors 는 4종(first_purchase/repeat_buyer/no_purchase/**lapsed_buyer**), targeting_ir.DEFAULT_ORDER_COUNT_BEHAVIORS 는 3종. 주입은 graph_rag.py:7506 한 곳뿐이고 confidence.py:138 과 canonical_targeting.py:564 는 미주입 폴백이다. 결과: lapsed_buyer 는 graph_rag 에서 kind='order_count_behavior'(fact_join=True, confidence 메타 있음), 나머지 둘에서는 'unclassified_behavior'(signals_target=False, confidence=None) — 같은 조건이 confidence 리포트에서 사라지고 canonical 소유권 트리에서 다른 노드가 된다. 더 나쁜 것은 lapsed_buyer 의 스키마가 {exists_before_period, not_exists_in_period, default_days} 뿐인데 graph_rag.py:27254-27256 이 `rule.get('operator','=')` / `rule.get('count',1)` 로 폴백해 `HAVING COUNT(DISTINCT ORDER_ID) = 1` 로 컴파일한다 = '이탈 구매자'가 '첫 구매 회원'이 된다(현재 이 값을 채우는 파서 경로가 없어 잠복). → (1) 신규 순수 모듈 member_filters_config.py(stdlib 만, lru_cache, GRAPH_RAG_MEMBER_TARGET_FILTERS env 존중)에 `order_count_behaviors()` 를 두고 세 소비자가 이것만 호출. (2) targeting_ir.extract_target_conditions 의 order_count_behaviors 를 기본값 없는 keyword-only 필수 인자로 승격 — 4번째 소비자가 생겨도 조용히 갈라질 수 없다. (3) 27254-27256 의 폴백 제거, operator/count 도 anti_join 도 없는 rule 은 `unsupported_order_count_behavior` 로 명시 반환.

**검증.** tests/test_order_count_behavior_parity.py::test_all_consumers_share_one_source(`member_filters_config.order_count_behaviors() == frozenset(JSON 로드값)` + `inspect.signature(extract_target_conditions).parameters['order_count_behaviors'].default is inspect.Parameter.empty`), ::test_every_declared_behavior_is_compilable(JSON 의 모든 behaviors 항목이 anti_join 이거나 operator+count 보유 — lapsed_buyer 를 지금 잡아낸다), ::test_same_kind_across_three_call_paths(behaviors=['lapsed_buyer'] plan 으로 세 경로가 같은 condition.kind).

**위험.** extract_target_conditions 공개 시그니처가 바뀌므로 호출부 3곳 + 복원한 테스트들이 수정 대상. test_every_declared_behavior_is_compilable 은 도입 즉시 red 이므로 lapsed_buyer 를 JSON 에서 지울지 빌더를 구현할지 같은 PR 에서 결정해야 한다(권장: 빌더 없으면 제거).

### W2-4. 레지스트리 로드 실패 삼킴 3종 → 헬스 가시화 + 소비지점 명시 차단 + cwd 독립

- **공수** M / **선행** W1-3
- **해소 대상** 축④ 죽은 가드. 입력2 클러스터1-④
- **파일** `graph_rag.py`, `metric_registry.py`, `segment_semantics.py`, `api.py`, `db_swap_preflight.py`, `tests/test_registry_health.py`

**작업.** graph_rag.py:846-856(_load_metric_registry → 빈 MetricRegistry), 862-870(_load_segment_semantics → None), 876-885(_load_requirement_registry → None)이 실패를 삼켜 조용히 강등된다. 각 강등의 귀결: (a) _METRIC_REGISTRY 가 비면 13346-13356 이 항상 None → 13330-13343 이 코드 기본 단위로 폴백 → 주석 스스로 경고하는 '30일 이상에서 일을 못 흡수해 조건이 통째로 누락되고 옆 절의 100회를 훔쳐온다'가 재현, (b) _SEGMENT_SEMANTICS 가 None 이면 10551 에서 즉시 return → 쿠폰 capability 게이트 전체가 no-op → 임계값 조용한 축소, (c) _REQUIREMENT_REGISTRY 가 None 이면 18679-18680 회계가 None → qualifier 사일런트 드롭 방지 계층이 통째로 꺼짐. 더 조용한 구멍: metric_registry.py:57 DEFAULT_METRIC_SPEC_DIR 이 cwd 상대라 존재하지 않는 디렉터리에서 glob 이 빈 결과를 내고 **예외조차 안 난다**. → (1) 세 로더가 강등 사유를 graph_rag.REGISTRY_HEALTH dict 에 기록 + logger.error(반환 계약 유지, 호출부 변경 0). (2) 소비지점 3곳을 no-op 이 아니라 명시 차단으로: 10551 은 쿠폰 어구가 원문에 있으면 unsupported{reason:'segment_semantics_unavailable'}, 18679 는 unresolved_source_conditions 에 기록, 13338 은 dropped_signal_warnings 승격. (3) metric_registry.load() 가 디렉터리 부재/스펙 0건을 MetricSpecError 로 승격 + 기본 경로를 `Path(__file__).resolve().parent` 기준으로. (4) 운영 강제는 옵트인 env STRICT_REGISTRIES=1(운영 compose 기본값)로 startup 에서 app.state.startup_error → 503. import 시 raise 는 채택하지 않는다 — api.py:308-318 이 이미 graph 로드 실패를 startup_error 로 흡수하는 패턴을 확립해 뒀는데 모듈 import 에서 raise 하면 재시작 루프가 된다.

**검증.** tests/test_registry_health.py::test_three_registries_load_nonempty(metric specs>0, segment metrics>0, requirement capabilities>0), ::test_no_registry_is_degraded(REGISTRY_HEALTH 전부 None), ::test_degraded_segment_semantics_blocks_instead_of_noop(monkeypatch 로 강등 주입 시 쿠폰 질의가 unsupported 로 귀결 — 조용한 통과 금지 자체를 테스트), ::test_loader_paths_are_cwd_independent(임의 cwd 에서 load 성공). db_swap_preflight 에 레지스트리 헬스 섹션 추가 후 `python db_swap_preflight.py` 종료코드 0. /health 에 registries 필드 노출.

**위험.** (2)가 동작 변경이라 회귀 위험 실재 — 지금 조용히 통과하던 degraded 경로가 차단으로 바뀐다. 다만 정상 환경에서는 degraded 가 발생하지 않으므로(실측 3종 정상) 실사용 경로 변화는 없어야 하고 골든 코퍼스로 확인 가능. graph_rag.py:849 와 878-882 가 근거로 든 안전망(tests/test_metric_registry.py, tests/test_semantic_requirements.py)은 삭제됐었으므로 W1-2 복원분과 함께 가야 한다.

### W2-5. 설정↔설정 / 설정↔코드 교차참조의 조용한 침묵 5종 제거

- **공수** L / **선행** W1-1, W1-4(contracts 잡)
- **해소 대상** 축② 설정 다중소유 + 축④ 죽은 설정. 입력2 클러스터2-⑪⑫, 메모리의 boolean_filters 죽은 레지스트리
- **파일** `aggregate_spans.py`, `aggregate_parser_config.py`, `graph_rag.py`, `analytical_intent.py`, `lexicon_llm.py`, `api.py`, `docs/data/runtime/sql/member_target_filters.json`, `tests/test_config_crossref_contract.py`

**작업.** 다섯 모두 '풀리지 않으면 조용히 넘어간다'는 같은 실패 모드이고 현재 어떤 테스트에도 안 걸린다. ① aggregate_spans.py:299-314 build_attribute_index 가 aggregate_parser_rules.json 의 supported_attribute_sources[].section 을 점표기로 member_target_filters.json 에 대입하며 미해결 시 `continue` 로 침묵(현재 두 경로 다 해석되지만 가드 없음) → 미해결 경로를 모아 AggregateParserConfigError 로 승격, 호출부에서 잡아 fail-close. ② docs/data/runtime/sql/metrics/*.json ↔ numeric_filters 조인키가 'B.' 접두어 물리 컬럼 문자열(graph_rag.py:13346-13356) — 컬럼 기반 폴백은 현재 **단 한 건도 성사되지 않는 죽은 경로**이고 미매칭 시 13343 이 조용히 타입 기본 문법으로 격하 → 별칭을 벗기고 비교하도록 조인키 정정 + numeric_filters 항목의 '알려진 미등록' 명시 필드 요구. ③ surface_concepts.json concept_id 를 코드가 `f'objective_{objective}'` 로 보간 조회하는데(graph_rag.py:7575, 9258, analytical_intent.py:394) 짝이 없으면 lexicon_llm.py:126 이 조용히 버린다 — 현재 24개가 우연히 다 맞지만 CAMPAIGN_OBJECTIVES(graph_rag.py:532)는 repurchase 포함 6종인데 objective_ concept 은 5종이라 잠복 구멍 → lexicon_llm.require_concepts() 로 가능한 값 전체를 부팅/CI 에서 확인. ④ api.py 의 지연 import + `except Exception: pass` 하드코딩 폴백 3곳(_build_external_member_schema 3514, _build_grade_labels_and_rank 3896, _load_grade_lifecycle_canonicals 3932) → 폴백값 == 설정 파생값 동치 테스트(불일치면 동치 강제 대신 현재 불일치를 고정하는 특성화 테스트로 시작). ⑤ member_target_filters.json 의 boolean_filters 는 코드가 읽지 않는 죽은 최상위 키(실측 존재 확인) → '읽히지 않는 설정 최상위 키' 목록을 테스트에 명시하고 증가 금지.

**검증.** tests/test_config_crossref_contract.py::test_all_attribute_source_sections_resolve, ::test_numeric_filter_metric_join_is_alias_free, ::test_every_objective_has_surface_concept(현재 objective_repurchase 에서 red — surface_concepts.json 보강 또는 CAMPAIGN_OBJECTIVES 위상 결정 강제), ::test_api_fallbacks_match_config_derived_values, ::test_unread_config_top_level_keys_do_not_grow. 역검증: member_target_filters.json 의 섹션 이름을 바꾸면 ① 실패, numeric_filters 컬럼에서 'B.' 를 빼면 ② 실패, _infer_objective 에 새 objective 추가 후 surface_concepts 미수정이면 ③ 실패. graph_rag 를 import 하지 않고 도는 몇 안 되는 테스트가 되므로 contracts 잡에 넣는다.

**위험.** ①의 예외 승격은 설정이 깨진 배포에서 파서를 죽인다 — 볼륨 마운트 restart 배포라 즉시 드러나는 편이 낫지만 예외를 잡아 unsupported 로 바꾸는 fail-close 지점을 반드시 함께 둬야 한다. ③은 도입 즉시 red 이므로 repurchase 결정이 선행돼야 한다. ④는 폴백이 '의도적으로 다른 값'인 경우 오탐 — 특성화 테스트로 시작해 동작 변경 없이 사실만 고정.

---

## Wave 3 — 계측 — 구조 이동의 안전성 주장을 측정으로 바꾼다

**목표.** 멱등성·경로 분기·물리 바인딩·plan 키 분류·build_sql_result 하류 소비를 전부 기계가 답하는 수치로 만들어, Wave 4~5 의 성공 판정을 사람 판단이 아닌 테스트에 넘긴다.

**여기서 멈춰도.** 여기서 멈춰도: '전 패스 멱등'이라는 비강제 계약이 처음으로 검사되고, 운영이 실제 쓰는 auto 경로가 회귀 자산에 들어오며, DB 이식성 부채가 세어져 늘어나면 빨강이 되고, plan 키 분류 모순이 코퍼스 밖에서도 잡힌다. 프로덕션 코드 변경은 사실상 0이다.

### W3-1. 멱등성·순수성 3축 측정 — 필터 단위 / 소스권위 번들 / build_sql_result

- **공수** L / **선행** W1-3(의미 골든), W1-6(env 누수 차단 — 안 고치면 이 테스트가 실행 순서에 따라 다른 답을 낸다)
- **해소 대상** 축① 인플레이스 변형/패스 재실행. W5-1·W5-2 의 성공 판정 근거.
- **파일** `tests/test_pass_idempotence.py`, `tests/golden/idempotence_exceptions.json`, `graph_rag.py`, `parser_shadow.py`, `semantic_fields.py`

**작업.** 정확성이 '전 패스 멱등'이라는 비강제 계약에 걸려 있는데(같은 패스 묶음이 2691/15137/20274 에서 최대 3회 재실행) 아무도 측정한 적이 없다. 세 축으로 코드화한다. (a) **필터 단위**: _deterministic_filter_registry 의 49개 필터 × 골든 19케이스에 _apply_named_filter 를 두 번 돌려 ir_snapshot.snapshot 동일 확인 — 위반 0 예상이므로 허용목록 없이 초록 착지. (b) **번들 단위**: _run_source_authoritative_stages(4928-5027 의 33단계 손배선 목록)를 완성된 plan 에 재실행 — 19케이스 중 1건 위반(or_of_age_with_and_threshold 에서 2회차가 plan.union_condition·plan.combine_mode·target_user.aggregate_conditions 를 되살린다 = 1회차에서 소실된 조건이 재실행으로 부활). tests/golden/idempotence_exceptions.json 에 이 1건만 사유와 함께 등록하고 W5-2 에서 0으로 만든다. (c) **build_sql_result 순수성**: 같은 plan 으로 두 번 호출 → SQL 은 19/19 동일(즉시 강제 가능)하지만 호출자 plan 의 IR 이 13/19 에서 변형된다. 그중 12건은 provenance 전용, co_purchase_same_product 는 조건인 canonical_targeting_expression 이 바뀐다 → '의미 슬롯 변형 0건'을 즉시 강제하되 그 1건만 예외 등록. 비교는 parser_shadow.compare + semantic_fields.strip_provenance 재사용으로 출처 잡음 배제. 테스트는 코퍼스 파생으로 작성해 케이스가 늘면 커버리지가 자동으로 는다.

**검증.** tests/test_pass_idempotence.py::test_every_deterministic_filter_is_idempotent, ::test_source_authoritative_bundle_rerun_is_noop(예외 파일에 정확히 1건), ::test_build_sql_result_does_not_mutate_semantic_slots(예외 1건), ::test_build_sql_result_sql_is_stable_across_calls(19/19). 역검증: 임의 _apply_* 필터에서 early-return 가드를 지우면 (a)가 즉시 실패. 로컬 실행시간 1분 이내(49필터 × 19케이스).

**위험.** 코퍼스 19케이스는 표본이 작아 '멱등하다'가 과대주장될 수 있다 — 실제로 필터 49개 전부가 이 표본에서 멱등이어도 다른 프롬프트에서 깨질 수 있고, 그러면 Wave 5 구조 이동의 안전성 주장이 무너진다. 완화: 코퍼스 파생으로 작성해 W3-2 의 auto 경로 케이스가 추가되면 자동 포함되게 하고, 테스트 docstring 에 '표본 위의 주장'임을 명시.

### W3-2. 실행 경로·플래그 매트릭스 가드 + _AUTO_FILTERS 매직 인덱스 계약

- **공수** L / **선행** W1-6, W3-1
- **해소 대상** 축⑤ env/contextvar 경로 분기. 5축 중 유일하게 어떤 테스트도 안 덮던 축. W4-2 의 LLM 스키마 관련 작업 전제.
- **파일** `tests/test_parser_path_parity.py`, `tests/test_auto_filter_order_contract.py`, `graph_rag.py`, `tests/golden/path_divergence.json`

**작업.** 운영 .env 는 QUERY_PARSER=auto 인데 골든/계약 테스트는 전부 parser='rules' 로만 돈다(cases.json 실측 parser:'rules') — '규칙은 되는데 auto만 실패'의 구조적 사각지대다. (a) tests/test_parser_path_parity.py: façade 의 monkeypatch 대상(_build_llm_targeting_ir_candidate / _build_llm_sql_fallback_candidate / _llm_extract_condition_slots / _apply_llm_object_fallback — tests/test_graph_rag_facade.py 의 MONKEYPATCHED_SYMBOLS 에 이미 등재)을 결정론 스텁으로 갈아끼운 뒤 parser='auto' 로 코퍼스를 돌려 rules IR 과 parser_shadow.compare 로 대조, 차이를 KNOWN_PATH_DIVERGENCE 로 고정. 스텁은 LLM '출력 형태'만 흉내내고 그 뒤 결정론 파이프라인은 실제 코드를 그대로 통과시켜 검증 대상을 '플럼빙'으로 명시 한정한다. (b) _AUTO_FILTERS[:5]/[5:] 매직 인덱스 분할(graph_rag.py:5408-5411, 주석은 'macro_region까지 앞 5개'인데 코드는 숫자 5) → `_AUTO_FILTERS.index('macro_region') == 4` 를 계약으로 박아 리스트 순서 변경 시 즉시 빨강. (c) env 매트릭스: LOGICAL_OR_COMPILER on/off × SOURCE_AUTHORITATIVE_IR_VALIDATION on/off × QUERY_PLAN_AUTHORITY(rules_first/shadow/llm_first)에서 IR 스냅샷 동일 또는 선언된 차이만 허용.

**검증.** tests/test_parser_path_parity.py::test_auto_path_matches_rules_ir(divergence 목록이 파일에 고정), tests/test_auto_filter_order_contract.py::test_macro_region_is_index_four, ::test_auto_filter_split_matches_comment. env 매트릭스는 파라미터라이즈드로 tests/test_parser_path_parity.py::test_ir_is_stable_across_flag_matrix. `pytest tests/ -q -p no:randomly` 와 골든 먼저 실행 결과가 동일(W1-6 이후 보장).

**위험.** auto 경로 스텁이 실제 LLM 출력 분포와 달라 '통과하는데 운영은 실패'하는 가짜 안전감 — QUERY_PARSER=auto 는 네트워크 없이는 결코 완전 재현되지 않는다. 완화: 검증 대상을 플럼빙으로 한정 명시하고, 이 테스트가 'LLM 정확도'를 보장한다고 어디에도 쓰지 않는다.

### W3-3. 소스 하드코딩 물리 바인딩 인벤토리 + 카탈로그 대조 + 래칫

- **공수** L / **선행** W1-5(table_symbols — 논리 심볼은 하드코딩이 아니므로 스캐너가 구분해야 한다)
- **해소 대상** 축③ 물리 스키마 산개. W5-3 이관의 세는 장치 + 되돌아감 방지.
- **파일** `physical_binding_inventory.py`, `tools/physical_binding_inventory.py`, `tests/test_physical_binding_ratchet.py`, `docs/data/test_baselines/physical_binding_baseline.json`, `db_swap_preflight.py`

**작업.** regex_inventory.py 와 같은 패턴의 순수 모듈 physical_binding_inventory.py 신설. AST 로 '문자열 모양'이 아니라 '바인딩 위치'로 수집한다(graph_rag 의 141개 오류코드/env명 같은 대문자 잡음 배제): ① EventSpec/FieldSpec/JoinPath/JoinCondition 생성자 인자(event_compiler.py:136-168, join_paths.py), ② 키가 table/*_table/column/*_column/columns/alias/*_alias 인 dict 리터럴(condition_evaluation_ir.py:154-164·256-283, graph_rag grain_columns 17731-17736, graph_rag 조립 폴백 24281-24364), ③ f-string/문자열 안의 `FROM <IDENT>`·`JOIN <IDENT>`(condition_evaluation_ir.py:393-401). 수집한 테이블/컬럼은 전부 docs/data/generated/schema_catalog.json 과 대조 — 카탈로그에 없으면 즉시 실패(DB 스왑 시 조용한 0명 대신 배포 전 빨강). 기준선 docs/data/test_baselines/physical_binding_baseline.json(런타임 모듈 실측: graph_rag 11/32, event_compiler 3/12, confidence 6/5, product_master_resolver 1/7, condition_evaluation_ir 2/5, join_paths 3/3, api 0/3, member_policy 1/2, targeting_expression 0/2 ≈ 테이블 27 / 컬럼 71). db_swap_preflight.py 에 '검사 6: 소스 하드코딩 바인딩' 추가 — 설정만 보던 프리플라이트가 코드도 보게 한다. write_baseline 은 사유 없는 상향을 거부하고 각 행이 source(constructor/dict_key/sql_fragment)를 밝힌다.

**검증.** `python tools/physical_binding_inventory.py` 가 파일:줄:심볼 목록 출력. tests/test_physical_binding_ratchet.py::test_all_bindings_exist_in_catalog, ::test_binding_counts_do_not_regress, ::test_baseline_raise_requires_reason. 역검증: event_compiler EVENT_REGISTRY 에 없는 컬럼명을 넣으면 카탈로그 대조 실패, 새 하드코딩 컬럼 추가 시 래칫 실패. `python db_swap_preflight.py --json` 종료코드 0.

**위험.** 스캐너 오탐이 잦으면 래칫이 신뢰를 잃는다(regex 래칫이 지금 스테일로 방치돼 21건 빨강의 일부였던 것이 정확히 그 결말의 실증). 완화: 수집 규칙을 바인딩 위치로 좁히고, 사유 없는 상향 거부를 구현으로 강제.

### W3-4. plan 키 다중소유 정적 드리프트 계약 — 코퍼스에 의존하지 않는 인벤토리

- **공수** M / **선행** W1-1
- **해소 대상** 축② plan 스키마 다중소유. 입력2 클러스터2-③의 인벤토리 측면. W4 이관의 회귀 판정 근거.
- **파일** `tests/test_plan_key_ownership_drift.py`, `tests/test_ir_schema_contract.py`, `ir_snapshot.py`, `plan_decisions.py`, `semantic_requirements.py`, `graph_rag.py`, `docs/data/slot_policy.json`

**작업.** 현행 tests/test_ir_schema_contract.py 의 unclassified_plan_keys 계약은 19개 골든 프롬프트가 실제로 만들어낸 키만 보므로, 코퍼스가 안 만드는 키는 영원히 안 걸린다. AST 로 graph_rag.py 및 plan 을 쓰는 모듈을 파싱해 `plan["lit"] =` / `query_plan["lit"] =` / `plan.setdefault("lit"` 대입 키를 전부 모으고 분류 여부를 강제한다. 확인된 미분류 12키: aggregation_request, aggregation_request_validation, combine_mode, group_ranking_target, literal_bindings, region_density_target, region_member_count_target, semantic_ir, semantic_ir_reconciliation, union_condition, unmatched_source_conditions, unsupported. 동시에 목록 간 상호 대조도 건다: ir_snapshot.DERIVED/KNOWN, plan_decisions.NON_CONDITION, semantic_requirements._PLAN_REQUIREMENT_SLOTS, graph_rag._LOGIC_CONDITION_SLOTS/_LOGIC_HANDLED_SLOTS/_EVENT_IR_BLOCKING_PLAN_KEYS/_GENERATION_QUERY_PLAN_KEYS/_UNSUPPORTED_CONDITION_LABELS, slot_policy.registered_slots(), plan_semantic_ast._LIST_ATTRIBUTE_SLOTS. 실측된 위반을 KNOWN_DRIFT 로 고정(래칫): policy_constraints 이중분류, DERIVED∩NON_CONDITION 8키 이중소유, _PLAN_REQUIREMENT_SLOTS 의 6키 무주공산(aggregation_request/group_ranking_target/member_column_selection_filter/region_density_target/region_member_count_target/union_condition), slot_policy 의 target_user.purchase_membership 양방향 드리프트. 각 항목에 해소 예정 항목 id(W4-3/W4-4)를 주석으로 박고 개수 상한 고정.

**검증.** tests/test_plan_key_ownership_drift.py::test_every_assigned_plan_key_is_classified(AST 인벤토리 기준 — 코퍼스 사각지대 제거), ::test_known_drift_count_does_not_grow, ::test_no_list_is_empty(목록을 비우면 통과해버리는 퇴화 방지). 역검증: _PLAN_REQUIREMENT_SLOTS 에 새 키를 넣고 ir_snapshot 에 등록하지 않으면 즉시 실패.

**위험.** KNOWN_DRIFT 가 영구 면제로 굳을 위험. 완화: 각 항목에 해소 항목 id 주석 + 개수 상한 고정 + Wave 4 에서 0 으로 내리는 것이 W4-3/W4-4 의 완료 조건.

### W3-5. build_sql_result 하류 소비 계약 테스트 — W5-1 착수의 필수 선행

- **공수** M / **선행** W3-1 (하류 계약 테스트는 신규 작성)
- **해소 대상** 축① 인플레이스 변형. W5-1 의 착수 게이트.
- **파일** `tests/test_api_response_contract.py`, `tests/test_retrieve_trace_stages.py`, `tests/test_sql_delivery_fail_closed.py`, `graph_rag.py`, `api.py`, `docs/overview/structure.md`

**작업.** 이 항목이 없으면 W5-1(deepcopy 순수화)은 IR 골든·SQL 골든·멱등 허용목록을 전부 초록으로 유지한 채 API 응답을 조용히 비울 수 있다. 코드로 확인한 경로: build_sql_result 내부의 _attach_query_output_contract(20306/20308 → 7251)가 plan['output_contract'] 와 plan['capability_check'](7294-7299)를 쓰는데, 파이프라인은 build_sql_result 호출(15337) 이후 **같은 query_plan** 을 build_stage_log / render_answer_prompt / build_message_context / build_recommendation_api_response(17450)에 넘기고 17487-17488 이 query_plan['capability_check'] / ['selected_route'] 를 읽는다. 두 키는 ir_snapshot.DERIVED_PLAN_KEYS 에 있어 의미 골든에서 제외되고, tests/ 전체 실질 커버리지는 test_semantic_verification_tristate.py 의 수동 입력 1건뿐이다(직접 확인). → (1) build_sql_result 호출 이후 query_plan 을 읽는 모든 지점을 전수 열거해 docs 에 고정, (2) 각 키(capability_check, output_contract, selected_route, failure_stage, decisions, dropped_signal_warnings, query_tuning)에 대해 API 응답 수준 계약 테스트를 만든다, (3) W1-2 에서 회수한 test_retrieve_trace_stages.py / test_sql_delivery_fail_closed.py / test_failure_stage 계열을 이 축의 회귀 자산으로 복원한다.

**검증.** tests/test_api_response_contract.py::test_response_carries_capability_check, ::test_response_carries_output_contract, ::test_response_carries_failure_stage, ::test_trace_stages_are_populated — 각각 코퍼스 케이스로 실제 파이프라인을 태워 응답 필드가 비어 있지 않음을 단언. 역검증: build_sql_result 진입부에 임시로 `query_plan = dict(query_plan)` 를 넣으면 이 테스트들이 빨강이 되어야 한다(= W5-1 의 위험을 실제로 탐지하는지 확인하는 메타 검증).

**위험.** 이 테스트들이 '현재 값'을 고정하면 나중에 정당한 개선까지 막을 수 있다. 완화: 값 동등이 아니라 '필드가 존재하고 비어 있지 않다' 수준의 계약으로 시작하고, 값 고정은 필요한 키에만 선별 적용.

---

## Wave 4 — 단일소스화 — 12개 사본을 하나의 선언에서 파생시킨다

**목표.** plan 슬롯의 존재·분류·어휘·라벨을 plan_schema.py 하나가 소유하고 나머지는 전부 파생 뷰가 되어, 상호 드리프트 허용목록이 0이 된다.

**여기서 멈춰도.** 여기서 멈춰도: 새 슬롯을 추가할 때 12곳이 아니라 1곳만 고치면 되고, 분류 모순·무주공산·이중소유가 전부 사라지며, 미러 함수 4종이 같은 함수를 부르게 되어 '바이트 동일'을 주석으로 지키던 계약이 구조로 대체된다.

### W4-1. plan_schema.py 신설 — PlanSlot 레지스트리 골격(소비자 0)

- **공수** M / **선행** W3-4
- **해소 대상** 축② plan 스키마 다중소유
- **파일** `plan_schema.py`, `tests/test_plan_schema_registry.py`, `targeting_ir.py`

**작업.** 순수 모듈 plan_schema.py(graph_rag 미import) 신설. @dataclass(frozen=True) PlanSlot 과 PLAN_SLOTS 레지스트리, facet: path/container/value_kind/origin(interpreted|config_lookup|derived|control)/meaning_bearing/snapshot_in(+snapshot_exclusion_reason)/ko_label/vocab_key/shape(targeting_ir.SlotShape 참조)/rules_init/logic_leaf(handled|unhandled|n_a)/answer_projection/event_ir_blocking/required_keys. import 방향은 plan_schema→targeting_ir 단방향으로 못 박고 docstring 에 명시(역방향은 순환). W3-4 인벤토리의 합집합을 초기 원소로 채우되 **이 항목에서는 아무도 소비하지 않는다**. 레지스트리를 JSON 이 아니라 소스에 두는 이유 3가지를 모듈 docstring 에 남긴다: (a) 파싱/정규화(coerce)는 소스 소유이고 슬롯 선언은 그 coerce 와 같은 객체에 붙어야 한다, (b) 슬롯 이름은 물리 스키마가 아니라 IR 어휘라 DB 이식성 제약이 요구하는 '소스 밖으로' 대상이 아니다, (c) JSON 외부화는 W2-4 가 고친 로드 실패 침묵 양식을 재생산한다. 대신 docs/data/plan_slots.snapshot.json 을 **생성물**로 내보내 리뷰 diff 와 BFF 소비를 얻는다(읽기 전용). targeting_ir.SLOT_SHAPES(실측 20개)는 폐기가 아니라 'coerce 보유 슬롯의 부분집합 뷰'로 남긴다 — targeting_ir.py:1075 의 `set(SLOT_SHAPES) <= _SPEC_KINDS` 불변식과 소비자 4곳(graph_rag 5646/6024/6051, query_structurer/campaign_plan_v2.py:109)을 깨지 않는다.

**검증.** tests/test_plan_schema_registry.py::test_paths_are_unique, ::test_container_is_known, ::test_shape_matches_slot_shapes(shape 가 있으면 SLOT_SHAPES[name] 과 동일 객체), ::test_ko_label_is_required(기본값 금지). `python -c "import plan_schema"` 가 graph_rag 없이 성공(순수 모듈 불변식). W3-4 인벤토리 테스트를 '레지스트리 ⊇ 각 목록' 방향 단언으로 확장.

**위험.** 레지스트리가 '13번째 사본'이 되는 것 — 이 항목 자체가 위험의 원천이다. 완화: W4-2 를 같은 스프린트에 붙여 소비자 전환을 즉시 시작하고, W3-4 의 포함관계 단언을 상시 켜 사본이 갈라지면 빨강.

### W4-2. 12개 사본을 facet 파생으로 전환 — 동등성 선통과 → 리터럴 삭제 2커밋 규율

- **공수** L / **선행** W4-1, W2-1, W2-2
- **해소 대상** 축② 전체. 입력2 클러스터2-①③의 구조적 해소
- **파일** `plan_schema.py`, `graph_rag.py`, `ir_snapshot.py`, `plan_decisions.py`, `semantic_requirements.py`, `plan_semantic_ast.py`, `tests/test_plan_key_ownership_drift.py`

**작업.** 전환 순서와 규율이 이 항목의 본체다. **모든 전환은 두 커밋으로 나눈다**: 커밋1 = '파생 값 == 기존 리터럴' 동등성 테스트를 추가해 통과시킨다, 커밋2 = 리터럴을 지운다. 이 규율 없이 5중 사본을 파생으로 바꾸면 원소 하나가 조용히 빠져도 아무도 모른다. 전환 대상: ① ir_snapshot.DERIVED_PLAN_KEYS/KNOWN_CONDITION_PLAN_KEYS + plan_decisions.NON_CONDITION_PLAN_KEYS → classification facet(첫 소비자로 삼는 이유: 골든 계약 테스트가 이미 강제하는 유일한 목록이라 정확성을 즉시 검증할 수 있다), ② semantic_requirements._PLAN_REQUIREMENT_SLOTS(55-72) → meaning_bearing facet, ③ graph_rag._build_rule_query_plan 리터럴(5469-5517) → plan_schema.new_plan() — **주의: 이 리터럴은 40+ 슬롯 중 22개만 선초기화하고 cart_absence/campaign_responses/balance_conditions 등은 이후 setdefault 로 도착하므로 rules_init facet 은 '부분 초기화 집합'을 정확히 인코딩해야 한다**(한 칸이라도 늘면 _is_empty 판정과 plan_decisions.snapshot 이 동시에 흔들려 골든이 통째로 움직인다), ④ _LOGIC_CONDITION_SLOTS/_LOGIC_HANDLED_SLOTS → logic_leaf facet(W2-1 의 파생을 흡수), ⑤ _UNSUPPORTED_CONDITION_LABELS → ko_label facet(W2-2 흡수), ⑥ _GENERATION_QUERY_PLAN_KEYS(16207-16226) → answer_projection facet, ⑦ _EVENT_IR_BLOCKING_PLAN_KEYS(6907-6911) → event_ir_blocking facet, ⑧ plan_semantic_ast._LIST_ATTRIBUTE_SLOTS(51-56) → 투영 facet. 제어/트레이스 키(_llm_trace, _conceptual_scope, _decision_marks, _coupon_ir)는 origin=control 로 명시 등록해 'IR 과 동거하는 상태'를 최소한 선언으로 남긴다.

**검증.** 각 전환마다 커밋1 의 동등성 테스트: tests/test_plan_schema_registry.py::test_derived_classification_matches_legacy_literal, ::test_requirement_slots_match_legacy_literal, ::test_new_plan_keys_match_rule_plan_literal, ::test_logic_leaf_facets_match_legacy_sets, ::test_ko_labels_match_legacy_dict, ::test_answer_projection_matches_legacy_keys. 커밋2 후 W1-3 의미 골든 19건 무변화 + 출처 골든 무변화 + 전체 스위트 무변화(xfail 대장 3건 그대로) + W3-4 의 KNOWN_DRIFT 감소.

**위험.** 파생으로 바꾸면서 어느 목록의 원소가 하나 빠지면 조건이 스냅샷/감사에서 조용히 사라진다. 완화: 두 커밋 규율 + 이관 전후 각 목록의 원소 집합을 파일로 덤프해 diff 0 을 단언하는 일회성 특성화 테스트. ③은 setdefault 의존 지점(plan[...]= 90곳, setdefault 71곳) 중 append/extend 호출부(예: plan['policy_constraints'].append at 14697)를 grep 으로 선점검해야 KeyError 를 피한다.

### W4-3. 분류 모순 3종 확정 해소 — policy_constraints / 이중소유 8키 / 반대방향 누락 12키

- **공수** M / **선행** W4-2
- **해소 대상** 축② 다중소유. 입력2 클러스터2-④⑤⑥
- **파일** `plan_schema.py`, `ir_snapshot.py`, `plan_decisions.py`, `semantic_requirements.py`, `tests/test_ir_schema_contract.py`

**작업.** ① **policy_constraints 정면 모순**: semantic_requirements.py:65 는 '사용자 의미 슬롯', ir_snapshot.py:47 은 DERIVED 로 정반대임을 직접 확인했다. 코드가 답을 준다 — graph_rag.py:14690-14705 _apply_policy_constraints 는 사용자 발화가 아니라 _load_business_policies() 의 정책 스토어 항목을 질의 매칭으로 붙이고 policy_id/table/column 같은 물리 메타까지 싣는다 → **DERIVED 가 맞다**. facet 으로는 origin=config_lookup + meaning_bearing=True + snapshot_in=False(사유: 설정 파일 값에 따라 변해 골든이 코드와 무관하게 흔들린다)로 한 번만 선언 — 두 목록이 '입력 해석인가'와 '사용자 의미를 담는가'라는 서로 다른 질문에 답하고 있었다는 사실이 facet 분리로 드러나고, 모순이 아니라 두 facet 의 다른 값이 된다. ② **DERIVED ∩ NON_CONDITION 8키 이중소유**(canonical_projection, canonical_targeting_validation, canonical_targeting_version, condition_claims, event_compiler_capability, event_semantic_validation, ownership_reconciliation_complete, source_requirements — 직접 확인): ir_snapshot.py:70 이 `_NON_CONDITION = plan_decisions.NON_CONDITION_PLAN_KEYS` 로 OR 판정하므로 한쪽 삭제는 효과가 없다 = 삭제가 조용히 무효화된다. plan_decisions(감사 메타 소유자)에만 남기고 DERIVED 에서 제거, 교집합 공집합을 계약으로. ③ **반대방향 누락**: KNOWN_CONDITION_PLAN_KEYS 에 있는데 원장에 없는 12키 중 compound_dimension_filters / entity_set / metric_trend / event_expression / external_conditions 는 명백히 사용자가 말한 조건이다('지역이 서울'은 원장에 남고 '지역이 서울이면서 등급이 골드'는 안 남는 비대칭) → `_PLAN_REQUIREMENT_SLOTS = KNOWN_CONDITION_PLAN_KEYS - _NOT_A_USER_REQUIREMENT` 파생으로 자동 편입하고, 빼야 할 것(intent/retrieval_scope/cart_context/unresolved_source_conditions)만 명시 선언.

**검증.** tests/test_ir_schema_contract.py::test_derived_and_non_condition_are_disjoint, ::test_requirement_slots_derive_from_condition_keys(`_PLAN_REQUIREMENT_SLOTS <= KNOWN_CONDITION_PLAN_KEYS` and `not (_PLAN_REQUIREMENT_SLOTS & DERIVED_PLAN_KEYS)`), ::test_exclusion_list_is_declared(`(KNOWN - REQUIREMENT) == _NOT_A_USER_REQUIREMENT` — 선언 없는 누락은 red), ::test_exclusion_entries_are_live_keys(존재하지 않는 키를 제외 선언하는 죽은 항목 방지). W3-4 KNOWN_DRIFT 에서 해당 3종 제거.

**위험.** 봉인 원장의 행 구성이 바뀌어 source_requirements_digest 값이 변하고 관련 골든이 갱신 대상이 된다. 차단 로직은 type=='semantic_obligation' 필터 때문에 영향 없음(확인). _requirement_span 이 새 슬롯 값 모양(중첩 dict/list)에서 스팬을 못 찾으면 span=None 으로 떨어지므로 None 허용 여부 확인 필요.

### W4-4. 미분류 12키 확정 + slot_policy 참조 무결성 + 전 슬롯 커버리지 래칫

- **공수** M / **선행** W4-3, W1-3
- **해소 대상** 축② 다중소유. 입력2 클러스터2-③의 분류 확정 측면
- **파일** `plan_schema.py`, `ir_snapshot.py`, `plan_decisions.py`, `slot_policy.py`, `docs/data/slot_policy.json`, `tests/test_slot_policy.py`, `tests/test_ir_schema_contract.py`

**작업.** W3-4 가 잡은 미분류 12키를 세 갈래로 확정한다: **조건**(union_condition, group_ranking_target, region_density_target, region_member_count_target, aggregation_request — 전부 사용자가 말한 것을 담는 슬롯이고 semantic_requirements 도 이미 의미 슬롯으로 본다), **파생**(unsupported, aggregation_request_validation, semantic_ir_reconciliation, unmatched_source_conditions — 해석의 결과·판정이지 입력이 아니다), **계측/실행 메타**(combine_mode, literal_bindings, semantic_ir). entity_set_condition(W2-1 에서 target_user 축이 아님을 확인한 plan 최상위 키)도 여기서 명시 분류한다. member_column_selection_filter 는 plan 대입이 없고 graph_rag.py:3817 drop 목록에만 나오므로 살아 있는 슬롯인지 확인해 KNOWN 에 넣거나 제거. ir_snapshot.SCHEMA_VERSION 을 3 으로 올려 골든이 어느 규칙으로 만들어졌는지 파일이 답하게 한다. 이어 slot_policy.py 로더가 JSON slots 키를 plan_schema.PLAN_SLOTS 와 대조해 미등록 이름(현재의 target_user.purchase_membership)을 실패로 올리고, 레지스트리에 있는데 정책이 없는 슬롯을 노출한다(현재 unregistered_default 가 owner=rule/risk=high 로 조용히 삼킨다). tests/test_slot_policy.py:106-113 의 가드를 plan 컨테이너 한정에서 전 컨테이너로 확대하고 '정책 미등록 슬롯 수'를 하향 전용 래칫으로.

**검증.** W3-4 의 test_every_assigned_plan_key_is_classified 가 미분류 0 으로 통과. tests/test_slot_policy.py::test_registered_slots_exist_in_registry, ::test_unregistered_slot_count_ratchet, ::test_backstop_none_requires_reason(기존 SILENT_LOSS_CEILING 재사용). SCHEMA_VERSION 상향에 따른 골든 재생성 diff 를 눈으로 검토해 커밋.

**위험.** ir_snapshot 출력 형태가 바뀌어 골든 파일 전량 재생성 필요(unsupported/semantic_ir 가 조건에서 빠지고 union_condition 등이 정식 조건으로 들어옴). W1-3 의 의미/출처 축 분리가 되어 있어야 이 재생성이 안전하다. slot_policy 를 fail-close 로 바꾸면 파일 오타 하나로 기동이 막히므로, 로더 실패는 예외로 올리되 코드 폴백 dict 를 유지하고 '폴백을 썼다'를 응답 디버그에 남긴다.

### W4-5. 미러 함수 4종 단일화 — SQL 리터럴 / 정규화 / Aggregate→metric_id

- **공수** M / **선행** W1-4(임시 parity 가드가 먼저 있어야 통합 전후 동등을 확인 가능)
- **해소 대상** 축② 다중소유. 입력2 클러스터2-⑦⑧⑬
- **파일** `sql_dialect.py`, `common_utils.py`, `event_ir.py`, `graph_rag.py`, `compiler_strategies.py`, `semantic_requirements.py`, `tests/test_sql_literal_single_source.py`, `tests/test_common_utils_normalization.py`, `tests/test_event_ir.py`

**작업.** 주석으로만 묶인 미러들을 같은 함수 호출로 바꾼다. ① **_sql_quote / _sql_nlike_contains**: compiler_strategies.py:14 가 '바이트 동일'을 선언하고 graph_rag.py:22524-22536 이 compiler_strategies 산출 fragment 를 graph_rag 자체 술어와 같은 where_clauses 에 섞어 _unique_strings 로 중복 제거하므로, 표기가 한 글자만 갈라지면 술어가 중복 삽입되고 semantic_requirements 의 SQL 리터럴 폴백 대조가 어긋난다. 미세 차이가 이미 있다(compiler_strategies 는 str() 코어스, graph_rag 는 안 한다) → sql_dialect.py(stdlib 조차 import 하지 않는 순수 모듈, 이미 graph_rag/event_compiler/aggregation_ast/api 가 import)에 quote_literal / nlike_contains 를 두고 양쪽은 얇은 별칭으로(호출부 111회 무수정). 비문자열 입력은 조용히 통과시키지 말고 TypeError 로 fail-close. ② **_normalize_value / _normalize_product_term**: semantic_requirements.py:176-182 와 graph_rag.py:14469-14471 이 같은 정규식이고 docstring 이 '동일 규칙'이라 못 박는다. account_requirements 의 반영 확인 폴백(`_normalize_value(raw) in _normalize_value(sql)`)에 쓰이므로 갈라지면 clarification 이 오탐/미탐된다 → common_utils.py(docstring 이 '바이트 동일하게 복제된 함수를 모으는 것이 목적'이라 선언)에 normalize_entity_term 하나로. ③ **Aggregate→metric_id**: graph_rag.py:6950-6959 는 필드 심볼 완전 일치 4종, event_ir.py:1239-1241 은 사건 심볼을 통째로 무시한 2종이다. graph_rag docstring(6935-6940)이 event_ir 쪽 버그를 명시하는데 원본은 안 고쳤다 — '최근 30일 장바구니 3번 이상'이 event_ir 에서 metric_id='order_count' 가 된다(FIELD_REGISTRY 에 cart.order_id 가 없어 expression=None 인 count 노드가 되므로). 현재 폭발 반경은 좁다(try_convert_to_legacy_slots 의 유일한 프로덕션 호출부 graph_rag.py:7149 가 감사 로그 값이고 plan 에 안 쓰인다) = '거짓말하는 진단'이지만 이름이 초대하는 대로 누가 plan 에 붙이는 순간 무성 오답 → event_ir.aggregate_metric_id() 단일 소스로 합치고(Aggregate 노드 소유자이자 순수 모듈이라 유일한 합류점), 롤링 창 1개 제한 같은 **소유권 이전 정책**은 graph_rag 에 남긴다.

**검증.** tests/test_sql_literal_single_source.py::test_quote_helpers_are_the_same_object, ::test_no_quote_pattern_outside_sql_dialect(AST 로 `"'" + ....replace("'","''")` 와 `LIKE N'%` 조립이 sql_dialect 밖에 없는지 — regex 래칫 선례 재사용), ::test_quote_escape_table(홑따옴표 0/1/2개, 한글, 정수, None). tests/test_common_utils_normalization.py::test_normalizers_are_the_same_object, ::test_normalization_rule_table. tests/test_event_ir.py::test_aggregate_metric_id_requires_event_symbol(cart count 노드 → None, purchase count(order_id) → 'order_count'), 기존 test_routing_authority_matches_slot_expressibility 에 장바구니 케이스 추가. 골든 SQL 바이트 불변.

**위험.** ③에서 cart 케이스가 requires_general_ir=False 인데 try_convert=None 이 되면 event_ir.py:1252-1258 의 불변식이 깨진다 — 그 케이스를 requires_general_ir=True 로 승격할지 불변식 문구를 좁힐지 결정이 선행돼야 한다(결정 전에는 감사 로그가 None 이 될 뿐 SQL 불변). ①에서 str() 코어스를 어느 쪽으로 통일하느냐에 따라 지금 AttributeError 로 드러나던 잘못된 입력이 조용히 통과할 수 있으므로 TypeError fail-close 를 택한다.

### W4-6. unsupported reason 을 닫힌 집합으로

- **공수** L / **선행** W4-2
- **해소 대상** 축② 다중소유. 입력2 클러스터2-⑨
- **파일** `unsupported_reasons.py`, `graph_rag.py`, `targeting_ir.py`, `tests/test_unsupported_reason_closure.py`

**작업.** reason 문자열이 세 계층에 손배선돼 있고 오타 한 글자면 '해소'와 '덮어쓰기'가 조용히 불발한다. 생산: graph_rag 만 20+ 곳 리터럴(10765, 10778, 10789, 10799, 10810, 10832, 10874, 10913, 10934, 10959, 11500, 11674, 11705, 25832…). 소비 ①: targeting_ir.SlotShape.resolves_unsupported(602-611)가 문자열 동등 비교(graph_rag.py:6056-6065), ② graph_rag.py:887 _COUPON_OVERRIDABLE_REASONS 가 targeting_ir 의 두 값과 우연히 같은 집합인데 서로를 참조하지 않는다, ③ 7179 같은 인라인 비교. 어느 소비 지점도 '그 reason 이 실제로 생산되는가'를 검증하지 않는다 → 신규 순수 모듈 unsupported_reasons.py(stdlib 만)에 모든 reason 상수 + ALL frozenset + `mark(plan, reason, message, clarification=None)` 기입 헬퍼(reason ∉ ALL 이면 ValueError). graph_rag 의 대입 20+ 곳을 mark() 로 치환하고 targeting_ir/그 집합들을 상수로 교체. **헬퍼는 기입과 검증만 하고 우선순위 규칙(graph_rag.py:10752 의 '이미 unsupported 가 있으면 덮지 않는다')은 호출부에 남긴다** — 흡수하면 우선순위가 조용히 바뀐다.

**검증.** tests/test_unsupported_reason_closure.py::test_every_produced_reason_is_declared(AST 로 `"reason":` 리터럴 수집 → ALL 포함), ::test_no_dead_reason(ALL 의 각 상수가 소스 어딘가에서 참조 — 이름만 남고 아무도 안 내는 reason 이 게이트에 남는 것 방지), ::test_consumer_sets_are_subsets(`_COUPON_OVERRIDABLE_REASONS <= ALL`, `union(SlotShape.resolves_unsupported) <= ALL`). 메시지/clarification 문구는 그대로이므로 응답 텍스트 불변 — 골든 응답 테스트로 확인.

**위험.** graph_rag 의 20+ 대입 지점을 치환하는 넓지만 기계적인 변경. mark() 가 우선순위 규칙을 흡수하면 '이미 있는 unsupported 를 덮는' 동작 변경이 조용히 생긴다.

---

## Wave 5 — 구조 이동 — 순수화·멱등화·물리 바인딩 이관·좌표계 복구

**목표.** build_sql_result 가 입력을 변형하지 않고, 패스 재실행이 증명된 no-op 이며, 물리 스키마 지식이 소스에서 설정으로 빠지고, 소유권 판정이 종류가 아니라 구간 기준으로 돌아온다.

**여기서 멈춰도.** 여기서 멈춰도(항목 단위로 중단 가능): 각 항목이 독립적으로 in-place 변형 진앙 하나씩을 제거하거나 물리 바인딩 래칫을 한 단계 내린다. 부분 완료 상태에서도 이전 웨이브의 가드가 전부 살아 있어 일관 상태를 유지한다.

### W5-1. build_sql_result 를 입력에 대해 순수화 + 내부 복제 10패스를 하나의 경계로

- **공수** L / **선행** W3-5(필수 게이트), W3-1, W4-2
- **해소 대상** 축① 인플레이스 변형의 최대 진앙
- **파일** `graph_rag.py`, `api.py`, `tests/test_pass_idempotence.py`, `tests/golden/idempotence_exceptions.json`, `tests/test_api_response_contract.py`

**작업.** build_sql_result(graph_rag.py:20274-20308)가 자기 입력 plan 을 재변형하며 플래너 패스 ~10개(_apply_condition_evaluation_ir, compositional_targeting.apply_to_plan, _apply_entity_set_condition, _reconcile_deterministic_member_exclusions, _reconcile_condition_ownership, _reconcile_semantic_ir_with_execution_plan, _guard_unparsed_entity_ranking, _normalize_aggregation_axis_filters, _normalize_purchase_aggregation_request, _apply_core_membership_semantics)를 내부 복제한다. W3-1 (c)가 실측한 대로 호출자 plan 이 19케이스 중 13건에서 변형되고 co_purchase_same_product 에서는 조건인 canonical_targeting_expression 까지 바뀐다. → 함수 진입부에서 `plan = copy.deepcopy(query_plan)` 으로 작업 사본을 뜨고, 호출자에게 돌려줄 산출물(capability_check, output_contract, selected_route, failure_stage, decisions, dropped_signal_warnings, query_tuning, 플랜 스냅샷)을 **반환 dict 의 명시 필드로** 싣는다. 동시에 그 10개 패스를 _finalize_execution_plan(query, plan, schema_path) 하나로 묶어(순서·인자 그대로) 15137 의 _apply_source_authoritative_stages 호출부와 나란히 읽히게 하고, graph_rag.py:15212 의 query_plan.clear()+update 통째 교체도 같은 경계 함수로 흡수한다. **착수 순서: (1) W3-5 의 하류 계약 테스트가 전부 초록, (2) build_sql_result 이후 query_plan 을 읽는 지점을 전수 열거해 docs 에 고정, (3) 각 키를 반환 dict 로 이관, (4) 그 다음에야 deepcopy.**

**검증.** W3-1 (c)의 허용목록이 의미 1건 + provenance 12건 → 0건. tests/test_pass_idempotence.py::test_build_sql_result_does_not_mutate_input(예외 파일 비어 있음). W3-5 의 test_api_response_contract 전 항목 초록(capability_check/output_contract/failure_stage/trace 가 여전히 채워짐). SQL 골든·의미 골든 무변화. deepcopy 비용을 측정해 기록(plan 이 순수 JSON-ish 이므로 회당 0.2ms 수준 예상).

**위험.** 호출자가 '변형된 plan'에 의존하고 있으면 순수화가 곧 기능 소실이다 — 이것이 이 플랜에서 가장 조용한 실패 벡터다. capability_check/output_contract 는 DERIVED 라 의미 골든에서 제외되고 tests/ 실질 커버리지가 없어(직접 확인) 모든 골든이 초록인 채로 응답이 빌 수 있다. 완화: W3-5 를 착수 게이트로 두고, 역검증(임시 dict 복사로 W3-5 가 실제로 빨강이 되는지)을 먼저 통과시킨다.

### W5-2. 소스 권위 번들의 비멱등 1건 해소 + 33단계 순서 선언을 데이터로

- **공수** M / **선행** W5-1, W3-1, W3-2
- **해소 대상** 축① 패스 재실행. 조건 소실 known_gap 의 실제 원인 제거.
- **파일** `graph_rag.py`, `slot_ownership.py`, `docs/data/source_authoritative_stages.json`, `tests/test_pass_idempotence.py`, `tests/golden/cases.json`

**작업.** (a) W3-1 (b)가 고정한 유일한 비멱등: or_of_age_with_and_threshold 에서 _run_source_authoritative_stages 재실행이 union_condition/combine_mode/aggregate_conditions 를 되살린다. 원인은 목록 안의 union_condition/logical_expression 감지기가 '이미 소유권이 넘어간 어구'를 다시 감지하기 때문이므로, 두 감지기에 slot_ownership 기반 early-return 을 추가한다 — 목록의 다른 감지기들이 이미 쓰는 규약(4970-4980 주석의 '이미 값이 있으면 덮지 않는 감지기라 재실행은 멱등')과 동일하다. early-return 조건은 '값이 이미 있고 **그 값의 소유 구간이 같은 원문 구간**'으로 좁힌다(종류 기준으로 넓히면 정당한 재감지가 막혀 소유권 판정이 퇴화한다). 이 케이스는 tests/golden/cases.json 에 known_gap('20대 또는 30대 연령 OR 이 통째로 사라진다')으로 이미 기록돼 있어, 멱등화 과정에서 그 조건 소실의 실제 원인이 패스 순서 artifact 임을 함께 확인할 수 있다. (b) _source_authoritative_stages(4928-5027)의 33단계 손배선 튜플에서 '단계명·사유'를 docs/data/source_authoritative_stages.json 으로 빼고 실행자만 코드에 남긴다(파싱 로직은 소스, 순서·사유 선언은 데이터라는 기존 3계층 기준에 부합).

**검증.** W3-1 (b) 허용목록 0건: tests/test_pass_idempotence.py::test_source_authoritative_bundle_rerun_is_noop 이 예외 없이 통과. tests/test_source_authoritative_stages_contract.py::test_json_stage_list_matches_code_executors(한쪽만 고치는 사고 차단). 의미 골든 무변화 — 단 or_of_age_with_and_threshold 의 known_gap 이 해소되면 골든 갱신 + cases.json 의 known_gap 블록 제거를 명시적 커밋으로. W3-2 경로 패리티의 divergence 목록이 늘지 않는지로 early-return 의 부작용 검증.

**위험.** early-return 을 잘못 걸면 정당한 재감지(원문 좌표계로 스팬을 다시 기록하는 목적)가 막혀 소유권 판정이 '종류 기준 회수'로 퇴화한다 — 이 목록 주석이 경고하는 바로 그 사고. 완화: 조건을 원문 구간 동일로 좁히고, W3-2 divergence 목록 증가 여부로 검증.

### W5-3. 물리 바인딩 이관 — event_compiler / condition_evaluation_ir / grain / cart / dimension / join_paths

- **공수** L / **선행** W3-3(인벤토리·래칫), W1-5(table_symbols), W5-1(SQL 생성 경계 순수화 이후라야 '값이 안 바뀌었다'를 SQL 동일성으로 증명 가능)
- **해소 대상** 축③ 물리 스키마 산개 전체. 입력2 클러스터3-③④⑤⑥⑦⑧, 클러스터1-⑦(join_paths 중기)
- **파일** `event_compiler.py`, `condition_evaluation_ir.py`, `graph_rag.py`, `join_paths.py`, `docs/data/runtime/sql/member_target_filters.json`, `docs/data/capabilities/same_product_co_purchase.json`, `docs/data/runtime/semantics/event_semantic_registry.json`, `docs/data/dimension_value_index.json`, `docs/data/test_baselines/physical_binding_baseline.json`

**작업.** W3-3 인벤토리의 상위 항목부터 내린다. 6갈래이고 각각 독립 커밋으로 분리한다. ① **event_compiler 삼중소유 + 이중 폴백**(코드 상수 136-168 22개 리터럴 + graph_rag 조립부 24281-24364 의 필드별 `.get(k,'<옛 DB 리터럴>')` 폴백 + 설정 JSON): 폴백이 남아 있으면 설정 키가 사라져도 예외 대신 옛 DB 값으로 조용히 컴파일되어 SQL 은 성공하고 0명이 나온다 = fail-close 원칙 정면 위반 → member_target_filters.json 에 event_bindings 섹션 신설(기존 order_count_targets/recent_login_target/signup_target/cart_targets 값을 dot-path 참조로), event_compiler 의 EVENT_REGISTRY/FIELD_REGISTRY/SubjectSpec 기본값 삭제 + overrides 가 비면 SqlCompileError, graph_rag 조립부는 폴백 없는 순수 어댑터로 축소. ② **condition_evaluation_ir 4중소유 + 임계값 2 의 6곳**(빌더 153-165 / 검증기 256-283 / SQL 조각 393-401 / graph_rag 조건토큰 21011-21023; 임계값은 139, 248, 370, 398, 21016, 21019): docs/data/capabilities/same_product_co_purchase.json 으로 스펙을 분리하고 빌더·검증기·SQL 조각·조건토큰이 전부 그 하나에서 파생 — 특히 validate_compiled_sql 의 required 부분문자열은 compile_evaluation 과 **같은 포맷터 함수를 공유**해야 한다(두 렌더러를 따로 두면 반드시 갈라진다는 event_compiler.py:20-22 의 검증된 규칙). ③ **grain_columns 이중 하드코딩**(17731-17736 capability 산출용 / 25209-25213 렌더용): aggregate_targets.grain_axes 로 축→컬럼 매핑만 설정에 두고 축의 집합(의미)은 코드가 계속 소유, 두 사본을 _agg_grain_columns() 단일 접근자로 병합. ④ **카트 하드코딩**: _logical_cart_fragment(26959)의 `KEEP_YN='Y'` 는 형제 3경로가 이미 설정에서 읽는데 이 한 곳만 리터럴이라 LOGICAL_OR_COMPILER 경로에서만 옛 컬럼이 섞이는 경로별 스키마 분기가 된다 → _cart_active_predicate(alias) 헬퍼로 통합. _CART_AGGREGATE_METRIC_EXPRESSIONS(26076-26081)의 물리 컬럼 4개도 cart_targets 에서 조립(집계 함수 선택은 도메인 의미이므로 코드에 남긴다). ⑤ **dimension_catalog 물리좌표 IR 유입**(8781-8782 이 target_table/target_column 을 plan 값 안에 넣고 12722 가 `table=='CRM_CM_PRODUCT'` 로 조건 분기): IR 페이로드에서 물리 좌표를 빼고 논리 심볼(dimension_id + role)만 보유, 소비 시점에 카탈로그 조회. 값 해석은 docs/data/dimension_value_index.json 스냅샷 우선 + DS_SQL 런타임 실행은 갱신 경로로 격하 — IR 생성이 DB 연결 유무에 따라 달라지던 것이 오프라인 결정론이 된다. ⑥ **join_paths 활성화**: render_join_line 호출 0건이라 카트→상품 조인이 4중소유(join_paths / member_target_filters.cart_targets.product_join / graph_rag 폴백 리터럴 / schema_catalog), 구매→상품도 3리터럴(24834, 25310, 25933) → render_join_line 호출로 교체.

**검증.** physical_binding_baseline 카운트 하락(event_compiler 22→0, condition_evaluation_ir 26→0). tests/test_event_ir.py::test_no_physical_literals_in_event_compiler, ::test_missing_event_bindings_fails_closed(설정 제거 시 SqlCompileError → unsupported, 0명 SQL 아님). tests/test_condition_evaluation_ir.py::test_spec_is_single_source, ::test_threshold_declared_once(스펙에서 2→3 으로 바꾸면 빌더·검증기·컴파일 SQL·조건토큰 네 곳이 모두 따라오는지), ::test_renamed_columns_still_compile. tests/test_registry_single_source.py::test_grain_axes_single_source(설정에서 per_brand 컬럼을 바꾸면 capability 산출과 렌더 SQL 둘 다 따라오는지). tests/test_cart_binding_single_source.py::test_cart_active_condition_single_source(active_condition 을 바꾸면 논리식/anti-join/사건IR 세 SQL 모두 새 값). tests/test_ir_schema_contract.py::test_dimension_filter_ir_has_no_physical_coordinates, ::test_dimension_ir_is_deterministic_offline(DB 커넥션 차단 monkeypatch 에서도 같은 IR). 전 항목 공통: `python db_swap_preflight.py` 종료코드 0, 골든 SQL 바이트 불변(값 동일 시).

**위험.** ①의 fail-close 전환은 설정 누락 시 기존에 동작하던 경로를 막는다 = 배포 시 설정 파일 누락이 곧 장애. 완화: 설정을 코드와 같은 이미지에 담고(볼륨 마운트 restart 배포), 기동 시 레지스트리 로드 결과를 헬스에 노출, 전환 커밋을 나머지와 분리. ⑤는 plan 스냅샷 형태가 바뀌므로 ir_snapshot 분류 목록(W4-4)과 동기화하지 않으면 unclassified 로 CI 실패 — 그게 오히려 안전망. ⑥은 SQL 문자열이 바뀔 수 있어 골든 재생성 동반 가능.

### W5-4. 의미 영수증 경로 배선 준비 + 컴파일러 등록 등식

- **공수** M / **선행** W4-3, W4-4(ir_snapshot 분류가 먼저여야 골든이 안 깨진다)
- **해소 대상** 축④ 죽은 가드. 입력2 클러스터1-⑤
- **파일** `semantic_requirements.py`, `graph_rag.py`, `ir_snapshot.py`, `plan_decisions.py`, `tests/test_source_semantic_contract.py`

**작업.** semantic_requirements.record_source_requirement_receipt(843-881)는 프로덕션 호출 0건이고 tests/test_source_semantic_contract.py:69 에서만 불린다. 다만 재확인 결과 **현재 호출 0건 자체는 의도된 fail-close 다** — 봉인 대상 3연산자(temporal_recurrence / referenced_entity_set / snapshot_selector)를 컴파일하는 빌더가 아직 없어서 graph_rag.py:18885-18889 가 영수증 없는 것을 unresolved 로 승격해 SQL 출고를 막는 것이 설계대로다('매월 1회 이상 구매한 회원' → sql=None 로 차단, 대조군 '최근 6개월 3회 이상' → SQL 생성). 진짜 결함은 셋이다: (a) 미래에 컴파일러가 생겨도 영수증을 남기라고 강제하는 장치가 전무 — 컴파일해 놓고도 계속 차단되는 조용한 반대 사고가 준비돼 있다, (b) 영수증 키 source_requirement_receipts 가 ir_snapshot 분류 밖이라 첫 배선 순간 test_every_plan_key_is_classified 가 전 골든에서 깨진다, (c) 856-862 가 원장에 없는 id 면 ValueError 를 raise 해 원장 미부착 plan 에서 컴파일러가 크래시한다. → (1) source_requirement_receipts 를 DERIVED 로 등록(컴파일러 실행 기록이지 사용자 의미가 아니며, plan_decisions.py:65 의 'canonical targeting receipts are derived execution metadata' 판례와 동일), (2) graph_rag 에 얇은 헬퍼 _discharge_semantic_obligation(plan, *, kind, compiler, evidence)를 추가해 원장이 없거나 kind 가 없으면 조용히 무시((c) 해소), (3) **핵심**: graph_rag._OBLIGATION_COMPILERS 레지스트리를 두고 '차단으로 남는 obligation kind 집합 == 영수증을 낼 컴파일러가 등록되지 않은 kind 집합'을 테스트로 강제 — 누가 컴파일러를 붙이는 순간 테스트가 '영수증을 배선하라'고 요구한다. 영수증 실제 발급(=동작 변경)은 3연산자 중 하나를 컴파일하는 빌더가 생길 때 shadow→enforce 로 별도 진행한다.

**검증.** tests/test_source_semantic_contract.py::test_receipt_key_is_classified(unclassified_plan_keys 비어 있음 — 배선 순간 골든 계약이 깨지는 함정 제거), ::test_discharge_on_ledgerless_plan_is_noop(예외 없고 plan 무변형), ::test_blocked_kinds_equal_uncompiled_kinds(핵심 불변식). 현재 차단 동작 유지 확인: '매월 1회 이상 구매한 회원'이 여전히 sql=None + query_plan_required_conditions_missing. 사용자 관측 동작 변화 0.

**위험.** 영수증을 잘못 붙이면 실제로 컴파일 안 된 조건이 통과해 조용한 오답이 된다 — 프로젝트 원칙의 정반대. 완화: 이 항목에서는 발급을 하지 않고 배선만 준비한다. 실제 발급은 컴파일 산출물(SQL fragment/evidence)이 존재하는 분기 안쪽에서만, env 플래그 shadow 관측 후 enforce.

### W5-5. slot_ownership 좌표계 신뢰 복구 — 소유권 회수를 '종류'에서 '구간'으로

- **공수** L / **선행** W5-1, W5-2, W3-2 (소유권 테스트는 신규 작성)
- **해소 대상** 메모리의 'surface evidence ownership ledger' + 축② 소유권 판정 퇴화
- **파일** `slot_ownership.py`, `graph_rag.py`, `tests/test_surface_ownership_dedup.py`, `tests/test_time_expression_ownership.py`, `tests/test_event_set_ownership.py`, `tests/test_slot_ownership.py`, `tests/test_slot_span_ownership.py`

**작업.** slot_ownership._source_compatible(107-116)이 두 텍스트가 같은 좌표계인지를 접두어 문자열 관계로만 판정한다. 그래서 정규화/재작성본에서 만들어진 스팬이 trusted=False 로 강등되고, claim_slot 이 '문장의 같은 구간'이 아니라 '조건의 종류' 기준 회수로 퇴화한다(graph_rag.py:4301 주석이 자인). → 텍스트마다 명시적 좌표계 식별자(원문 다이제스트 + 파생 관계: identity/rewrite/clause_split)를 부여하고, 스팬 기록 시 그 식별자를 함께 저장해 비교가 문자열 접두어 추측이 아니라 선언된 관계로 이뤄지게 한다. 재작성본 스팬은 원문 좌표로 사상 가능한 경우에만 trusted 로 승격하되, 승격 조건을 '원문에서 동일 부분문자열이 유일하게 1회 등장'으로 보수적으로 시작한다. W5-2 에서 union/logical 감지기에 넣은 early-return 도 이 판정을 쓰게 통일한다. 승격 판정은 env 플래그 뒤에 둬 shadow 로 먼저 관측한 뒤 켠다.

**검증.** 기존 소유권 테스트 3종 + W1-4/W5-5 에서 신규 작성한 소유권 계약 테스트 전부 초록. W3-2 경로 패리티의 divergence 목록 감소 또는 최소한 증가 없음. plan['decisions'] 에서 claim/keep 비율 변화를 코퍼스 19케이스로 전후 비교해 리포트로 남기고, 의미 골든 변화가 있으면 케이스별로 '개선인가'를 판단해 명시 커밋. W1-3 의 출처 골든이 이 변경의 영향 범위를 보여 준다.

**위험.** 좌표계 승격이 과하면 서로 다른 문장의 구간이 같은 것으로 오인돼 정당한 조건이 회수된다(조용한 소실 = 최악 실패). 이 플랜에서 가장 위험한 변경이므로 안전망이 최대로 갖춰진 마지막에 둔다 — 이 시점에는 의미 골든(W1-3)·드리프트(W3-4)·멱등성(W3-1)·경로 패리티(W3-2)·순수성(W5-1)이 모두 초록이라 회귀가 즉시 드러난다. 승격은 env 플래그로 즉시 되돌릴 수 있게 한다.

---

## 위험 항목 → 작업 매핑

| 심각도 | 항목 | 처리 |
|---|---|---|
| high | [클러스터1-①] capability_registry.validate_capabilities() 호출 0건 + 저장소에 테스트 실행 CI 잡 자체가 없음 | W1-1 (CI 게이트 신설) + W1-4 (a) (validate_capabilities 를 테스트·preflight·startup 3중 배선) |
| high | [클러스터1-③] 삭제된 tests/test_capability_contract.py · tests/test_semantic_requirements.py (복원 가능, 무수정 42건 통과) | W1-2 (삭제 테스트 113개 회수·트리아지, 두 파일은 최우선 복원 대상) |
| high | [클러스터1-④] _load_metric_registry / _load_segment_semantics / _load_requirement_registry 의 조용한 실패 삼킴 + metric_registry 기본 경로 cwd 의존 | W2-4 (REGISTRY_HEALTH 가시화 + 소비지점 3곳 명시 차단 + cwd 독립 경로 + STRICT_REGISTRIES) |
| high | [클러스터2-①] _LOGIC_CONDITION_SLOTS 가 cart_absence/metric_trend 를 몰라 OR 컴파일러 fail-close 게이트가 fail-OPEN — 조건 무성 삭제 (LOGICAL_OR_COMPILER 는 프로덕션 '1') | W2-1 (targeting_ir 파생으로 교체 + 회귀 케이스) → W4-2 ④ 에서 logic_leaf facet 으로 흡수 |
| high | [클러스터2-①의 라벨 측면] _UNSUPPORTED_CONDITION_LABELS 6종 누락 + _remaining_condition_labels 의 멤버십 필터로 '남는 조건' 안내에서 침묵 삭제 | W2-2 (라벨 채움 + 필터 제거를 .get 폴백으로 안전 구현) → W4-2 ⑤ ko_label facet |
| high | [클러스터2-②] order_count_behaviors 3소비자 불일치 (설정 4종 vs 코드 3종) + lapsed_buyer 가 '첫 구매'로 오컴파일되는 죽은 설정 | W2-3 (member_filters_config 단일 로더 + 필수 인자 승격 + 폴백 제거 fail-close) |
| high | [클러스터2-③] plan 최상위 12키가 어느 분류에도 없음 (unsupported 가 '조건'으로 스냅샷에 유입) + 계약 테스트가 코퍼스 기반이라 못 잡음 | W3-4 (AST 정적 인벤토리 계약으로 사각지대 제거) + W4-4 (12키 3갈래 확정 + SCHEMA_VERSION 상향) |
| high | [클러스터3-①] 이식성 가드 전멸 (preflight 관련 테스트 5종 삭제) + db_swap_preflight 가 CLI 전용이라 pytest 에서 한 번도 안 돌고 지금 FAIL 인데 문서는 ✅ | W1-2 (테스트 복원) + W1-5 (pytest/CI 게이트 승격 + test_portability_guard_presence + 문서 정정) |
| high | [클러스터3-②] preflight FAIL 3건 중 2건이 실제 결함 — 논리 심볼 'product' 매핑이 코드 상수에만 존재, contact_member_list 의 campaign_date_table 미선언으로 컬럼 오귀속 | W1-5 (table_symbols 섹션 신설 + 접근자화 + campaign_date_table 선언) |
| high | [클러스터3-③] event_compiler 사건/필드 레지스트리 삼중소유 + graph_rag 조립부 이중 폴백 → 설정 키 소실 시 옛 DB 값으로 조용히 컴파일되어 SQL 성공 + 0명 | W3-3 (인벤토리로 22개 리터럴 계수) + W5-3 ① (event_bindings 이관 + 폴백 제거 + fail-close) |
| high | [클러스터3-④] condition_evaluation_ir 물리 바인딩 4중소유 + 임계값 2 의 6곳 산개 — DB 스왑 실수가 '미지원 조건'으로 위장 | W3-3 (26개 리터럴 계수) + W5-3 ② (capability 스펙 JSON 분리 + 포맷터 공유) |
| high | [입력1·렌즈] tests/golden_support.py:41 apply_corpus_env 가 os.environ 을 복원 없이 전역 변경 — 테스트 순서 의존 오염 (LOGICAL_OR_COMPILER=1 이 이후 전 테스트에 누출) | W1-6 (monkeypatch 스코프 fixture 전환) |
| high | [입력1·렌즈] 골든 17건 + regex 래칫 1건이 스테일 기준선으로 상시 빨강 — 스팬 기록 개선이 곧 빨강이 되어 아무도 재생성 안 함 | W1-3 (의미축/출처축 분리 재생성 + 래칫 기준선 갱신 + 의미 변화 시 재생성 거부) |
| high | [입력1·렌즈] 운영은 QUERY_PARSER=auto 인데 골든/계약 테스트는 전부 parser='rules' — '규칙은 되는데 auto만 실패'의 구조적 사각지대 | W3-2 (a)(c) (결정론 스텁 기반 auto 경로 패리티 + env 매트릭스) |
| high | [입력1] build_sql_result 가 입력 plan 을 재변형 (19케이스 중 13건, co_purchase_same_product 는 조건까지) + 플래너 패스 ~10개 내부 복제 + query_plan.clear()+update 통째 교체 | W3-1 (c) 측정 → W3-5 하류 계약 확보 → W5-1 (deepcopy 순수화 + _finalize_execution_plan 경계) |
| high | [입력1] 소스 권위 번들 재실행 비멱등 1건 (or_of_age_with_and_threshold 에서 소실된 조건이 재실행으로 부활) + 33단계 손배선 순서 목록 | W3-1 (b) 측정 → W5-2 (slot_ownership 기반 early-return + 순서·사유 JSON 분리) |
| high | [입력1·렌즈] capability_check / output_contract 가 build_sql_result 이후 소비되는데 DERIVED 라 의미 골든 제외 + tests/ 실질 커버리지 0 → 순수화가 응답을 조용히 비울 수 있음 | W3-5 (API 응답 수준 계약 테스트 — W5-1 의 착수 게이트) |
| high | [입력1·메모리] slot_ownership._source_compatible 이 접두어 문자열로만 좌표계를 판정해 재작성본 스팬이 trusted=False 로 강등, claim_slot 이 '종류 기준 회수'로 퇴화 | W5-5 (명시 좌표계 식별자 + 보수적 승격 + shadow 플래그) |
| high | [입력1·검증] requirements.txt 에 jsonschema 부재 — aggregate_parser_config.py:31 이 모듈 임포트 시점에 import 하므로 requirements 만 거친 새 이미지에서 import graph_rag 자체가 실패 | W1-1 (a) |
| high | [검증] 삭제된 테스트 113개 (213741e 94개 + ce39f68 19개) — 세 전략안 어디에도 없는 사실. '21건 빨강'은 생존한 30% 위의 숫자라 그대로 래칫하면 드리프트를 정상으로 봉인 | W1-2 (회수·트리아지·선별 복원 + quarantine_triage 래칫) |
| medium | [클러스터1-②] fact_join 소유권 불변식 테스트가 실존하지 않음 (fact_join_kinds 는 import 만, 호출 0건) | W1-4 (b) tests/test_builder_ownership_contract.py 신설 |
| medium | [클러스터1-⑤] record_source_requirement_receipt 프로덕션 호출 0건 — 미래 컴파일러가 생겨도 영수증을 강제할 장치 없음, 영수증 키 미분류, 원장 없는 plan 에서 ValueError | W5-4 (ir_snapshot 분류 등록 → 헬퍼 → _OBLIGATION_COMPILERS 등식) |
| medium | [클러스터1-⑥] 소스 주석·독스트링이 존재하지 않는 테스트를 계약 근거로 인용 (확인된 8곳) | W1-4 (tests/test_doc_claims.py 메타테스트 + 실체 생성 또는 문구 정정) |
| medium | [클러스터1-⑦] requirement_capabilities.json 이중 로더 (프로덕션이 쓰는 쪽이 실행자산 대조를 안 함) + join_paths 런타임 미사용으로 조인 지식 삼중소유 | W1-4 (a)(c) 단기 배선·가드 + W5-3 ⑥ (join_paths render_join_line 활성화로 조인 소유자 단일화) |
| medium | [클러스터2-④] policy_constraints 정면 모순 (semantic_requirements=의미 슬롯 / ir_snapshot=DERIVED) + _PLAN_REQUIREMENT_SLOTS 6키 무주공산 | W4-3 ① (facet 분해로 한 번만 선언, 코드 근거상 DERIVED 확정) |
| medium | [클러스터2-⑤] 반대방향 누락 — KNOWN_CONDITION_PLAN_KEYS 12키가 의미 원장에 없음 (dimension_filters 는 기록되는데 compound_dimension_filters 는 안 됨) | W4-3 ③ (파생식 + _NOT_A_USER_REQUIREMENT 명시 선언 + 대칭 가드) |
| medium | [클러스터2-⑥] DERIVED_PLAN_KEYS ∩ NON_CONDITION_PLAN_KEYS 8키 이중소유 — 한쪽 삭제가 조용히 무효화됨 | W4-3 ② (plan_decisions 한쪽으로 몰고 교집합 공집합 계약) |
| medium | [클러스터2-⑦] Aggregate→metric_id 이중 구현 — event_ir 이 사건 심볼을 무시해 장바구니 집계를 order_count 로 오표기 | W4-5 ③ (event_ir.aggregate_metric_id 단일 소스, 소유권 이전 정책은 graph_rag 잔류) |
| medium | [클러스터2-⑧] _sql_quote/_sql_nlike_contains 미러가 '바이트 동일' 전제로 같은 WHERE 리스트에 섞이는데 parity 테스트가 삭제됨 + 이미 str() 코어스 차이 존재 | W1-4 (d) 임시 parity 가드 → W4-5 ① sql_dialect.py 단일화 + AST 재발 방지 |
| medium | [클러스터2-⑨] unsupported reason 문자열이 닫힌 집합 없이 3계층 손배선 — 오타 한 글자면 해소/덮어쓰기 게이트가 조용히 불발 | W4-6 (unsupported_reasons.py 닫힌 집합 + mark() 헬퍼 + 양방향 폐쇄 테스트) |
| medium | [클러스터2-⑩] validate_capabilities 죽은 정적 게이트 (클러스터1-①과 동일 항목의 다중소유 관점 재기술) | W1-4 (a) — 클러스터1-① 과 같은 항목으로 통합 처리 |
| medium | [클러스터2-⑪] 설정JSON↔설정JSON 교차참조 침묵 2건 (aggregate_spans 섹션 점표기 continue, surface_concepts concept_id f-string 짝 없으면 침묵 + objective_repurchase 잠복 구멍) | W2-5 ①③ (AggregateParserConfigError 승격 + lexicon_llm.require_concepts + CI 대조) |
| medium | [클러스터2-⑫] metrics/*.json ↔ numeric_filters 조인키가 'B.' 접두어 물리 컬럼 — 컬럼 폴백은 현재 도달 0건인 죽은 경로이고 미매칭 시 조용히 타입 기본 문법으로 격하 | W2-5 ② (별칭 제거 조인키 정정 + '알려진 미등록' 명시 필드 요구) |
| medium | [클러스터3-⑤] grain_columns 이중 하드코딩 (capability 산출용/렌더용) + 상품 조인 3리터럴 + _PRODUCT_SCOPE_TABLE 코드 상수 | W1-5 (table_symbols) + W5-3 ③⑥ (grain_axes 설정화 + 단일 접근자 + render_join_line 교체) |
| medium | [클러스터3-⑥] 논리식 컴파일러 경로만 카트 KEEP_YN='Y' 와 카트 지표식 컬럼을 하드코딩 — 경로별 스키마 분기 ('auto만 실패' 의 물리 스키마 판) | W5-3 ④ (_cart_active_predicate 통합 + cart_targets 에서 지표 컬럼 조립) |
| medium | [클러스터3-⑦] dimension_catalog 의 target_table/target_column 이 IR 값 페이로드로 유입 + 값 해석이 런타임 실DB 실행 의존 → IR 이 DB 연결 유무에 따라 달라짐 | W5-3 ⑤ (IR 은 논리 심볼+role 만, 소비 시점 카탈로그 조회, dimension_value_index 스냅샷 우선) |
| medium | [클러스터3-⑧] join_paths.py / capability_registry.py 완전 사문화 → 카트→상품 조인 4중소유, FORBIDDEN_PRODUCT_JOIN_KEYS 가드가 SQL 생성 경로에 한 번도 안 걸림 | W1-4 (a)(c) 가드 소생 + W5-3 ⑥ (render_join_line 실사용 전환) |
| medium | [입력1] _AUTO_FILTERS[:5]/[5:] 매직 인덱스 분할 (주석과 코드가 다른 근거) | W3-2 (b) (_AUTO_FILTERS.index('macro_region')==4 계약 테스트) |
| medium | [입력1] api.py 의 지연 import + except Exception: pass 하드코딩 폴백 3곳 (3514, 3896, 3932) — 폴백이 stale 해져도 침묵 | W2-5 ④ (폴백값 == 설정 파생값 특성화 → 동치 테스트) |
| low | [클러스터2-⑬] semantic_requirements._normalize_value ↔ graph_rag._normalize_product_term 미러 (주석으로만 묶임, 가드 테스트 삭제됨) | W4-5 ② (common_utils.normalize_entity_term 단일화 + 동일 객체 단언) |
| low | [메모리·입력2] member_target_filters.json 의 boolean_filters 가 코드가 읽지 않는 죽은 최상위 키 | W2-5 ⑤ ('읽히지 않는 설정 최상위 키' 목록 명시 + 증가 금지) |

## 재확인 결과 — 서술 정정 항목

- 입력2의 findings 는 전부 still_real=true 로 판정돼 있었고, 내 재확인에서도 '문제 아님'으로 뒤집힌 finding 은 없다. 아래는 **문제는 실재하나 서술이 부정확해 작업 범위를 조정한** 항목들이다.
- [범위 축소] '_LOGIC_CONDITION_SLOTS 에 cart_absence/metric_trend/entity_set_condition 3종 누락' → 실측 결과 SLOT_SHAPES 중 container=='target_user' 와의 차집합은 정확히 ['cart_absence','metric_trend'] 2종이다. entity_set_condition 은 target_user 슬롯이 아니라 plan 최상위 키라 같은 축의 누락이 아니다 — W2-1 은 2종만 다루고, entity_set_condition 은 plan 키 분류 축(W4-4)에서 처리한다.
- [수치 정정] 'SLOT_SHAPES 19개' → 실측 20개(그중 target_user 컨테이너 17개). 계획의 어떤 판단도 바뀌지 않지만 커버리지 목표 수치는 20 기준으로 잡는다.
- [수치 정정] '골든 실패 16건' → 실측 17건. 전체 21 = 골든 17 + regex 래칫 1 + 실회귀 3. 전략1의 2단계가 '16건 제거'로 계산하면 대장에 1건이 남아 어긋나므로 W1-1 의 대장 상한과 W1-3 의 제거 건수를 17/1/3 으로 확정했다.
- [성격 정정] 'jsonschema 누락으로 지금 import graph_rag 가 죽는다' → 현 개발·실행 환경에는 jsonschema 가 설치돼 있어 지금 죽지는 않는다(테스트 1223 passed 가 그 증거). requirements.txt 만 거친 **새 이미지**에서만 터지는 잠복 결함이며, 그래도 severity high 유지 — 배포 시점에 처음 드러나는 종류라 더 나쁘다.
- [성격 정정] 'record_source_requirement_receipt 호출 0건 = 의미 의무가 항상 차단되는 버그(전략1 9단계의 전제)' → 재확인 결과 호출 0건 자체는 **의도된 fail-close** 다. 봉인 대상 3연산자를 컴파일하는 빌더가 아직 없으므로 차단이 설계대로 동작하는 것이고, '정상 컴파일된 조건이 미지원으로 고지된다'는 전략1의 서술은 현재 상태에서는 성립하지 않는다. 따라서 W5-4 는 영수증 발급(동작 변경)을 하지 않고 배선 준비와 미래 사고 방지 등식만 건다 — 전략1 9단계의 shadow→enforce 승격은 이 플랜에서 제외했다.
- [확인됨, 문제 아님] 'db_swap_preflight 는 종료코드 0/1 게이트 도구로 설계돼 있다' → 실측 확인(ok=false 일 때 종료코드 1). 게이트로 승격 가능하다는 입력2의 전제가 맞고, W1-5 가 그대로 활용한다.
- [확인됨, 문제 아님] 'import 방향이 깨끗하다(순수 모듈 규약, graph_rag→하위 단방향)' → plan_schema→targeting_ir, semantic_requirements→ir_snapshot, compiler_strategies→sql_dialect, event_ir→(자기 완결) 전부 순환 없이 성립함을 확인했다. Wave 4 의 파생 전환이 순환 위험 없이 가능하다.

## 하지 않을 것

### graph_rag.py 29,695줄 모놀리스 분해 및 rag/ 패키지 이행

이 플랜은 '분해를 안전하게 만드는 준비'까지만 한다. 지금 분해하면 façade 외부 소비 심볼 150개 중 98개(65%)가 _프라이빗이라 무엇이 계약인지 정의되지 않은 상태로 경계를 긋게 되고, 골든/계약 테스트조차 graph_rag 전체(로컬 ~50모듈 + networkx/fastembed/qdrant_client) import 를 요구해 회귀 판정 비용이 분해 비용을 넘는다. Wave 4 의 plan_schema 와 Wave 5 의 _finalize_execution_plan 경계가 분해의 첫 이음매가 되지만, 분해 자체는 별도 프로젝트다.

### PatchSet 기반 순수 패스 전면 이행 (59개 _apply_* 의 쓰기 모델 전환)

방향은 축① 의 유일한 근본 해답이지만 채택 조건이 충족되지 않았다. (a) 제시안이 4단계 중간에서 잘려 있어 승부처(33단계 cut-over, build_sql_result 복제 제거, 위상정렬 순서 골든)의 계획이 존재하지 않는다 — 절반에서 멈추면 patch regime 과 in-place regime 이 공존해 지금보다 순서 추론이 어려워진다. (b) 값 기반 diff/apply 는 현행 코드의 참조 공유(중첩 dict 를 꺼내 넘기는 패턴)를 조용히 끊는데, 제시된 왕복 속성 테스트는 값 동등만 증명하고 참조 동등은 원리적으로 탐지하지 못한다. (c) 샌드박스 실행이 예외 원자성을 뒤집어 '절반 쓰인 plan'을 읽는 fail-close 게이트가 조용히 우회될 수 있다. 대신 그 안의 최고 통찰 두 가지 — plan_decisions.snapshot 을 차분 엔진으로 재사용하지 않는다(언더스코어 사이드채널 _slot_spans/_owned_spans 유실 방지), 원문 vs 재작성본 좌표계 축 측정 — 는 W3-1 과 W5-5 에 흡수했다.

### plan 슬롯 값 내부 구조의 전면 JSON-schema 검증

규칙 경로가 만드는 더 풍부한 shape 와 충돌해 fail-close 가 폭발한다. 대신 coerce 보유 슬롯에 대해 '멱등' 과 '규칙 경로 산출물도 coerce 를 통과해 자기 자신이 나온다' 두 지점만 못 박는 것으로 충분하며(전략2 11단계의 절제된 판단), 그마저도 왕복이 깨지는 슬롯이 다수 나오면 즉시 고치지 말고 xfail 상한 래칫으로 관리한다.

### plan 슬롯 레지스트리를 JSON 설정으로 외부화

슬롯 이름은 물리 스키마가 아니라 IR 어휘라 'DB 이식성 → 스키마 지식을 소스 밖으로' 제약의 대상이 아니다. 반대로 JSON 외부화는 W2-4 가 고치는 로드 실패 침묵 양식(_REQUIREMENT_REGISTRY / _load_metric_registry 가 조용히 None/빈 레지스트리가 되는 것)을 정확히 재생산한다. docs/data/plan_slots.snapshot.json 은 읽기 전용 **생성물**로만 내보내 리뷰 diff 와 BFF 소비를 얻는다.

### LLM tool 스키마 확장 (exclude / campaign_constraints 슬롯 신규 광고)

전략2의 6단계에 포함돼 있으나 이는 리팩터가 아니라 LLM 동작 변경이다. 광고하면 LLM 이 새 슬롯을 채우기 시작해 auto 경로 IR 이 분포 수준에서 바뀌는데, 골든 코퍼스가 parser='rules' 로만 돌아 CI 는 전부 초록으로 남는다. W3-2 의 auto 경로 패리티가 안정적으로 초록이 된 뒤 별도 작업으로 분리하고, 이 플랜에는 넣지 않는다.

### semantic_requirements.RequirementRegistry 를 capability_registry.CapabilityRegistry 어댑터로 통합 (중기 단일화)

판정 근거가 '선언(supported=true)'에서 '산출(resolve_status)'로 바뀌므로 미지원 판정이 늘어난다 — 지금 조용히 통과하던 조합이 unsupported/clarification 으로 노출되는 것은 의도한 방향이지만 골든·응답 문구 회귀 범위가 넓다. W1-4 의 validate_capabilities 배선만으로도 '프로덕션이 소비하는 그 JSON'이 실행자산과 정적 대조되어 사일런트 실패 창구는 닫힌다. 통합은 그 가드가 CI 에서 green 으로 돌기 시작한 뒤 별도 결정.

### 골든 코퍼스 케이스 확장 (19케이스 → N)

모든 '초록'이 19케이스 표본 위의 주장이라는 한계는 실재하지만, 케이스 추가는 비용이 크고 이 플랜의 어느 단계도 그것을 전제하지 않는다. 대신 W3-1·W3-2 를 코퍼스 파생으로 작성해 케이스가 늘면 커버리지가 자동으로 늘게 만들고, 테스트 docstring 에 '표본 위의 주장'임을 명시해 과대주장을 막는다. 확장 자체는 별도 백로그.

### 의미 영수증(record_source_requirement_receipt)의 실제 발급 및 enforce 승격

재확인 결과 현재의 호출 0건은 버그가 아니라 의도된 fail-close 다(3연산자를 컴파일하는 빌더가 아직 없음). 발급을 만들면 '실제로 컴파일 안 된 조건이 통과'하는 조용한 오답 위험만 생긴다. W5-4 는 배선 준비와 '컴파일러가 등록되는 순간 영수증을 요구하는' 등식만 걸고, 발급은 첫 컴파일러가 생길 때 그 PR 안에서 shadow→enforce 로 처리한다.

### 삭제된 테스트 113개 전량 복원

표본 실행 결과 상당수가 red 이고 그것이 '의도된 동작 변경'인지 '조용한 회귀'인지 가리는 비용이 크다. 전량을 CI 에 올리면 신호가 잡음에 묻혀 W1-1 의 대장이 즉시 무의미해진다. green 판정분만 tests/ 로 복원하고 red 는 tests_quarantine/ + quarantine_triage.json 으로 격리해 하향 전용 래칫으로 관리한다.

## 성공 지표

| 지표 | 현재 | 목표 |
|---|---|---|
| pytest 전체 스위트 결과 | 21 failed / 1223 passed / 14 skipped (실측), CI 에서 실행되지 않음 | 0 failed / exit 0. 남은 알려진 실패는 tests/known_failures.json 에 사유·발견일·담당과 함께 strict-xfail 로만 존재하며 항목 수가 하향 전용 래칫 |
| CI 에서 실행되는 테스트/게이트 잡 수 | 0 (.github/workflows 에 docker-publish.yml 하나뿐, pytest·preflight·린트 스텝 전무) | 2 (contracts 잡 = 설정 계약 + db_swap_preflight, suite 잡 = pytest tests). docker-publish 가 needs:[contracts] 로 계약 실패 시 이미지 미발행 |
| db_swap_preflight 결과 | ok=false, problems 3건, 종료코드 1, pytest 에서 한 번도 실행되지 않음, 문서는 A/B/C/D 전부 ✅ 로 표기 | ok=true, problems 0. pytest(test_preflight_is_green_on_current_repo)와 CI contracts 잡에서 상시 실행. 검사 항목 4 → 6(capability 정적 검증, 소스 하드코딩 바인딩 추가) |
| 정의됐지만 프로덕션 호출 0건인 가드 심볼 수 | 4 (capability_registry.validate_capabilities, targeting_ir.fact_join_kinds, join_paths.render_join_line, semantic_requirements.record_source_requirement_receipt) | 0 — 앞의 3개는 테스트·preflight·SQL 렌더 경로에 실배선, 영수증은 _OBLIGATION_COMPILERS 등식으로 '컴파일러가 생기면 반드시 배선'이 강제됨 |
| 소스 주석이 계약 근거로 인용하는 존재하지 않는 테스트 파일 수 | 8 (targeting_ir.py:16-17, graph_rag.py:22573/4046/849/878-882, compiler_strategies.py:13, semantic_requirements.py:26, capability_registry.py:16, join_paths.py:16-17) | 0, 그리고 tests/test_doc_claims.py 가 재발을 상시 차단 |
| 삭제된 테스트 113개의 처리 상태 | 0 회수 (213741e 94개 + ce39f68 19개가 tests/ 에서 사라진 채 아무 기록 없음) | green 판정분 전량 tests/ 복원, red 는 tests_quarantine/ + quarantine_triage.json 에 verdict(regression/intended)+evidence 로 등재, verdict=regression 항목 수는 하향 전용 |
| plan 슬롯/키 목록의 독립 사본 수 | 12 (rules 리터럴, SLOT_SHAPES, ir_snapshot DERIVED/KNOWN, plan_decisions NON_CONDITION, semantic_requirements _PLAN_REQUIREMENT_SLOTS, _LOGIC_CONDITION_SLOTS/_LOGIC_HANDLED_SLOTS, _EVENT_IR_BLOCKING_PLAN_KEYS, _GENERATION_QUERY_PLAN_KEYS, _UNSUPPORTED_CONDITION_LABELS, slot_policy.json, plan_semantic_ast._LIST_ATTRIBUTE_SLOTS) | 1 (plan_schema.PLAN_SLOTS) + 전부 facet 파생. 상호 드리프트 테스트의 KNOWN_DRIFT 허용목록 4 → 0 |
| AST 인벤토리 기준 미분류 plan 최상위 키 수 | 12 (aggregation_request, aggregation_request_validation, combine_mode, group_ranking_target, literal_bindings, region_density_target, region_member_count_target, semantic_ir, semantic_ir_reconciliation, union_condition, unmatched_source_conditions, unsupported) — 코퍼스 기반 계약이라 잡히지 않음 | 0. 계약이 코퍼스가 아니라 AST 대입 인벤토리 기준으로 바뀌어 코퍼스가 안 만드는 키도 잡힘 |
| DERIVED_PLAN_KEYS ∩ NON_CONDITION_PLAN_KEYS 이중소유 키 수 | 8 | 0, 교집합 공집합이 계약 테스트로 상시 강제 |
| target_user 슬롯 중 논리식 leaf 게이트가 인식하지 못하는 슬롯 수 / 미지원 라벨이 없는 슬롯 수 | 게이트 미인식 2 (cart_absence, metric_trend) — fail-close 가 fail-OPEN 으로 뒤집힘 / 라벨 없음 6 (campaign_buy_amount, campaign_buy_count, campaign_response_frequency, cart_aggregate, cart_retention, cell_rate_target) — '남는 조건' 안내에서 침묵 삭제 | 둘 다 0, 그리고 SLOT_SHAPES 에 새 슬롯이 추가되면 자동으로 게이트·라벨에 편입되는 파생 구조 |
| 소스에 하드코딩된 물리 테이블/컬럼 바인딩 수 (AST 바인딩 위치 기준) | 미측정 (실측 근사: 테이블 27 / 컬럼 71. 그중 event_compiler 22개 리터럴, condition_evaluation_ir 26개) | 인벤토리 + 카탈로그 대조 + 하향 전용 래칫이 존재하고, event_compiler 22→0, condition_evaluation_ir 26→0. 카탈로그에 없는 바인딩은 즉시 CI 실패 |
| build_sql_result 호출이 입력 plan 을 변형하는 골든 케이스 수 | 19케이스 중 13 (의미 슬롯 변형 1건: co_purchase_same_product 의 canonical_targeting_expression, provenance 변형 12건) | 0. 산출물은 전부 반환 dict 의 명시 필드로 이관되고 tests/test_api_response_contract.py 가 응답 필드 유실을 차단 |
| 소스 권위 번들 재실행 시 IR 이 바뀌는 골든 케이스 수 / 비멱등 결정론 필터 수 | 번들 1건 (or_of_age_with_and_threshold) / 필터 0건 — 둘 다 어떤 테스트도 측정하지 않음 | 번들 0건, 필터 0건이 tests/test_pass_idempotence.py 로 상시 강제. 허용목록 파일이 비어 있음 |
| auto 파서 경로를 태우는 회귀 케이스 수 / env 플래그 매트릭스 조합 수 | 0 케이스 (전부 parser='rules') / 0 조합 — 운영은 QUERY_PARSER=auto | 19 케이스 × (LOGICAL_OR_COMPILER on·off × SOURCE_AUTHORITATIVE_IR_VALIDATION on·off × QUERY_PLAN_AUTHORITY 3종). divergence 는 파일에 고정되고 증가 시 빨강 |
| SQL/정규화 미러 함수 쌍 수 (주석으로만 묶인 '바이트 동일' 계약) | 4쌍 (_sql_quote×3, _sql_nlike_contains×2, _normalize_value/_normalize_product_term, Aggregate→metric_id ×2) | 0 — 전부 같은 함수 객체를 부르고, AST 스캔이 미러 재발(sql_dialect/common_utils 밖의 quote·normalize 조립)을 차단 |
| 테스트 실행 순서 의존성 | 존재 (apply_corpus_env 가 os.environ 을 복원 없이 전역 변경 — 골든 먼저 돌면 이후 전 테스트가 LOGICAL_OR_COMPILER=1) | 없음. `pytest tests -q -p no:randomly` 와 골든 우선 실행 결과가 동일함을 test_env_isolation 이 강제 |
