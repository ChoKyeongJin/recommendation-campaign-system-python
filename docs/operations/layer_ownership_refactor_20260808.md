# 계층 책임 고정 — 의미 → 판정 → 진단 → 귀결

작업일 **2026-08-08**. [live_prompt_audit_40_86_remediation.md](live_prompt_audit_40_86_remediation.md) §3 이
남겨 둔 부채(B·D·E·F·G)를 계층 책임으로 닫는다. 개별 케이스를 통과시키는 것이 목적이 아니므로,
아래 표의 왼쪽은 전부 **책임**이고 오른쪽은 그 책임을 가진 자리 하나다.

측정 기준선(호스트 python, `pytest -q -p no:randomly`)::

    작업 전   3926 passed · 6 failed · 27 skipped
    작업 후   4009 passed · 6 failed · 27 skipped

실패 6건은 작업 전부터 있던 것이고 그대로다(`test_aggregation_decimal` 2 ·
`test_money_literal_bindings` 2 · `test_query_pipeline_type_contracts` 1 ·
`test_semantic_literal_characterization` 1). **신규 회귀 0**, 신규 테스트 83개.
새 모듈 두 개(`clause_semantics` · `semantic_diagnostics`)와 `lowering_planner` 는 mypy 오류 0 이고,
ruff(`pyproject.toml` 검사 범위)는 작업 전과 같은 6건 — 전부 이번에 손대지 않은 자리다.

---

## 1. 계층이 새로 소유하게 된 책임

| 의미 | 소유자 | 다른 계층이 다시 정하지 않는다 |
|---|---|---|
| 절의 **극성·수량자** | [clause_semantics.py](../../clause_semantics.py) | 컴파일러·판정자가 부정어를 다시 읽지 않는다 |
| 절의 **기간 소유권** | `ClauseSemantics.temporal` | 문장 수준 `has_period`/`missing_period` 사본이 없다 |
| 기간 값의 **출처** | `ClauseTemporal.provenance` | 정책이 채운 값과 사용자 값을 검증기가 구별한다 |
| **지원 여부** | [lowering_planner.resolve_executable](../../lowering_planner.py) | 목록 조회가 아니라 낮춰 보고 답한다 |
| 실패의 **원인** | [semantic_diagnostics.Diagnostic](../../semantic_diagnostics.py) | 사유를 하류에서 추론하지 않는다 |
| 원인 → **사용자 귀결** | `semantic_diagnostics.outcome_for` 표 하나 | `status="unsupported"` 를 직접 적지 않는다 |
| 하루 안의 **시각** | `audience_catalog.sources.*.time_of_day_column` | 컴파일러에 컬럼명을 적지 않는다 |
| **미래를 담는 필드** | `audience_catalog.fields.*.supports_future_values` | 방향을 모든 날짜 필드에 열지 않는다 |

### 각 계층이 더 이상 하지 않는 일

- `temporal_claims` — 값 개수를 세기 **전에** 그 축이 관측 가능한지 묻는다(순서가 곧 진단의 정확도다).
- `audience_execution` 종결 갈래 — 능력 부재·주체 불일치를 **자산 대조보다 먼저** 본다.
- `consent_cardinality` — 모델이 쓴 문자열로 구제를 열지 않고, 다른 절의 리터럴을 책임지지 않는다.
- `event_compiler` — 시각 경계를 만나면 근사하지 않고, **선언이 있으면** 경계일 술어로 낮춘다.
- 절 낮춤 — 기간을 말한 절의 창을 만들지 못하면 **낮추지 않는다**(하드 제약 보존).

---

## 2. 감사 케이스별 귀결

| 케이스 | 전 | 후 |
|---|---|---|
| **B1** `최근 90일 동안 주문하지 않은` | 표현할 수 없다 | `NOT EXISTS(90일 창)` |
| **B1** `최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는` | 기간 값이 없습니다 | 창이 **부정 절**에 귀속, 뒤 절은 무한 구간 |
| **B2** `최근 3개월 동안 매월 한 번 이상 구매한` | 미지원 | 칸별 `AND(EXISTS×3)` |
| **A3** `앱으로 로그인하지 않은` | `LAST_LOGIN_CHANNEL != 'APP'`(조용한 오답) | `supports_all_occurrences` 부재로 미지원 |
| **F** `구매 회원이 100명 이상인 브랜드를` | `failure`, 사용자 문장 **없음** | `UnsupportedSubject`, 문장 있음 |
| **H** `정상에서 휴면으로 바뀐` | 값 개수 불일치(되묻기 성격) | 이력 소스 부재(미지원) — 귀결 유지, 이름 교정 |
| **G** `이메일, 문자, 앱푸시 중 정확히 두 개` | 모델 어휘 운에 좌우 | typed 주장 + 공유 근거 |
| **D1** `7월 1일 23:59:59까지` | 시각이 사라짐 | `ORDER_DATE = … AND ORDER_TIME <= '235959'` |
| **D1** `7/1 09:00 ~ 7/2 18:30` | 시각이 사라짐 | 경계일에만 시각, 가운데 날은 하루 전체 |
| **D1** `23:00~02:00` | — | 자정 넘김은 `OR`(괄호 안전) |
| **D1** `최근 24시간` | 기간을 되물음 | 해상도 미지원(요청/가능 단위를 명시) |
| **D2** `3개월 전부터 1개월 전까지` | 반쪽 금지로 미지원 | 하나의 구간으로 합성 |
| **D2** `향후 7일 안에 구매예정일이` | 사유 없는 미지원 | 미래 능력 선언 필드에서 컴파일 |

`앱으로 로그인하지 않은` 의 **부재 계약이 두 갈래**인 것이 이 축의 요점이다.

| 요청 | 필요 능력 | 근거 |
|---|---|---|
| 맨 부재(`최근 30일간 로그인하지 않은`) | `supports_all_occurrences` **또는** `supports_point_state` | 마지막 로그인이 창보다 앞이면 그 창에 로그인은 없다(단조성) |
| 한정 부재(`**앱으로** 로그인하지 않은`) | `supports_all_occurrences` 만 | 마지막 로그인의 채널만 알면 그 이전 채널은 모른다 |

가르는 것은 부정어가 아니라 **한정어의 유무**다. 이 구분이 없으면 살아 있던 미접속 경로가
함께 막히거나(과잉 차단), 채널 부재가 조용한 오답으로 나간다.

---

## 3. 추가한 불변식

| 불변식 | 파일 |
|---|---|
| 낮춰지는 요구가 미지원으로 끝나지 않는다(모순 금지) | [test_lowering_resolution.py](../../tests/test_lowering_resolution.py) |
| `Executable` 은 계획 없이 만들 수 없다 | 〃 |
| `Undetermined` 는 어떤 귀결과도 모순되지 않는다 | 〃 |
| 판정자의 답이 응답에 기록된다(shadow) | 〃 |
| 한 절의 부정이 다른 절의 극성을 오염시키지 않는다 | [test_clause_semantics.py](../../tests/test_clause_semantics.py) |
| 창은 자기 절에 귀속된다(절이 둘이면 창도 둘) | 〃 |
| 능력 이름이 `temporal_bindings.json` 키와 갈라지지 않는다 | 〃 |
| 모든 진단 코드에 사용자 귀결이 선언돼 있다(미선언은 큰 소리로 실패) | [test_semantic_diagnostics.py](../../tests/test_semantic_diagnostics.py) |
| 모든 비-SQL 결과에 사용자 문장 + 개발자 상세가 있다 | 〃 |
| 카디널리티는 공유 근거로 증명된다(멤버별 스팬 불요) | [test_cardinality_evidence_contract.py](../../tests/test_cardinality_evidence_contract.py) |
| 근거 metadata 가 실행 가능 여부를 바꾸지 않는다 | 〃 |
| 진리표가 다른 표현은 여전히 막힌다 | 〃 |
| 시각이 경계일에만 걸린다(가운데 날은 하루 전체) | [test_temporal_boundary_lowering.py](../../tests/test_temporal_boundary_lowering.py) |
| 자정 넘김이 precedence-safe `OR` 로 나온다 | 〃 |
| 날짜만 있는 구간의 SQL 이 바이트 동일하다 | 〃 |
| 미래 표지가 과거 창이 되지 않는다 | 〃 |
| 선언이 없는 소스는 시각 경계를 여전히 fail-close 한다 | 〃 |

---

## 4. 구현 중 실측한 결함 셋

문서로 남기는 이유는 셋 다 **테스트가 아니라 구현 중에** 드러났고, 같은 함정이 다시 열릴 수
있기 때문이다.

1. **`except Exception` 이 배선 결함을 '선언 없음'으로 위장했다.** 능력 조회가 인자 하나를
   빠뜨린 호출이었는데 넓은 except 가 그것을 '선언을 못 읽음'으로 접어, 부재 능력 계약이
   **한 번도 돌지 않은 채** 통과했다. 예외를 `TemporalCatalogError` 로 좁혀 고정했다(§33).
2. **판정자가 기준일을 넘기지 않아 사용자가 말한 기간이 사라졌다.** 기준일 없이 리터럴을
   추출하면 기준일 의존 창이 fail-close 로 없어지고, 그 절은 '창 없는 절'로 보인다 — 부재
   조건이 **구간 없는 `NOT EXISTS`** 로 컴파일됐다. 기준일을 필수로 넘기고, 그 위에
   "기간을 말한 절의 창을 못 만들면 낮추지 않는다"를 방벽으로 두었다.
3. **선언된 긴 표면어 안의 사건 별칭이 사건으로 읽혔다.** `구매금액` 의 `구매` 가 존재
   조건이 되어 비교 의무를 덮었고, `구매예정일` 의 `구매` 는 `향후 7일` 을 **과거 창**으로
   뒤집었다. 소유 대장 규칙(같은 근거 표현은 한 번만)을 지표에서 **필드까지** 넓혀 닫았다.

---

## 5. 남은 legacy 지원 판정 자리

판정 권위를 한 곳으로 모으는 작업은 **첫 게이트만** 이관했다(Phase 3C 의 한 단위). 아래가
남아 있고, 각 자리의 다음 단계는 판정자의 답과 legacy 답의 차이를 `audience_planner_resolution`
에서 읽어 확인한 뒤 옮기는 것이다.

| 자리 | 무엇을 판정하나 |
|---|---|
| `audience_execution` 실행 자산 대조(`semantic_registry_gap`) | 자산은 선언됐는데 생산자가 없다 |
| `audience_execution` 방출 실패(`_supported_obligation_conflicts`) | 의무 allowlist 기반 반박 |
| `analytical_intent` 의 `unsupported_reason` 3종 | 지표·한정어·랭킹 지표 미해결 |
| `graph_rag` 의 미지원 의도 게이트 | 컴파일 불가 표현의 결정론 차단 |
| `external_conditions.service` | 외부 조건 평가 미지원 |
| `temporal_ir` 연산자 `unsupported_reason` 선언 | 실행 IR primitive 부재(정직한 선언) |

마지막 줄은 **옮길 대상이 아니다** — 연산자 선언이 스스로 말하는 한계이고, 판정자는 그 선언을
읽어 답한다.

## 6. 실제로 미지원으로 남는 능력

| 요청 | 부재한 것 |
|---|---|
| 채널·값으로 한정된 로그인 부재 | 채널별 로그인 **이력** 소스 |
| 회원 상태(`MEMBER_STATE_CD`) 시점·이력 | 그 축의 시간 관측 지표 선언 |
| 하루보다 잘은 롤링 창(`최근 24시간`) | 시각 해상도 시간 컬럼(창 축) |
| 회원이 아닌 주체를 행으로 내는 질의 | 복수 주체 선언 + 그룹 테이블 투영 |
| 끊기지 않은 N칸(`3개월 연속`) | PartitionBy/OrderBy/Lag primitive |

---

## 7. 이 문서를 거짓이 되지 않게 하는 것

§3 의 불변식 표는 전부 실제 테스트에 대응한다. 표에 줄을 추가할 때는 대응 테스트를 함께
만들고, 테스트를 지울 때는 줄도 지운다.

라이브 코퍼스로 이 작업을 검증하지 않았다 — 같은 코드로 두 번 돌려도 귀결이 갈리는 항목이
있기 때문이다. 위의 모든 주장은 결정론 경로(컴파일러·판정자를 직접 호출한 결과)나 실DB 실측
(`ORDER_TIME` = `nvarchar(6)` HHMMSS · `CRM_MB_MONTHCRMINFO.BUY_DUE_DATE` = char8 미래 값)에
근거한다.
