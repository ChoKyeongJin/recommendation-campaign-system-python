# 라이브 감사 40~86 수리 기록 — 구조 이동과 남은 부채

> **후속(2026-08-08).** 아래 §3 이 남긴 부채 B·D·E·F·G 는
> [layer_ownership_refactor_20260808.md](layer_ownership_refactor_20260808.md) 에서 계층 책임으로
> 닫혔다. 이 문서의 §1·§2 는 그대로 유효하고, §3 은 **그 시점의 진단**으로 남긴다.

작업일 **2026-08-08**. 대상은 [live_prompt_audit_40_86.md](live_prompt_audit_40_86.md) 가 분류한
케이스 A~H 이고, 목표는 그 케이스를 개별로 통과시키는 것이 아니라 **같은 종류의 결함이 다시
들어오지 못하는 구조**를 만드는 것이다.

측정 기준선(호스트 python, `pytest -q -p no:randomly`)::

    작업 전   3892 passed · 6 failed · 27 skipped
    작업 후   3926 passed · 6 failed · 27 skipped

실패 6건은 작업 전부터 있던 것이고 그대로다(`test_aggregation_decimal` 2 ·
`test_money_literal_bindings` 2 · `test_query_pipeline_type_contracts` 1 ·
`test_semantic_literal_characterization` 1). **회귀 0**, 신규 테스트 34개.
mypy 오류 4건도 전부 기존 것이며 이번에 건드린 코드에는 없다.

> 이 문서는 감사 문서를 대체하지 않는다. 감사 문서는 **그날의 실측**이고 그대로 두어야 한다.
> 여기에는 그 실측에 대해 **무엇을 고쳤고 무엇을 남겼는지**만 적는다.

---

## 1. 무엇이 끝났나

### Phase 1 — Boolean 합성 안전성 (감사 A1 · P0)

**결함.** [event_compiler.py](../../event_compiler.py) `_combine` 이 피연산자마다 괄호를 씌우면서
합성 **결과**는 감싸지 않았다. 그 조각을 받는 쪽(`plan.where` → `" AND ".join`)은 감쌀지 말지를
판단할 근거가 없었다.

sqlite 로 실행해 재현했다 — **주문이 하나도 없는 회원이 매칭됐다**:

```sql
EXISTS (... WHERE EO.MEMBER_NO = B.MEMBER_NO AND (AMT = 1) OR (AMT = 2))
```

**고친 방법.** 문자열을 조립하는 계층에 **결합도**를 넣었다.

| 심볼 | 자리 | 책임 |
|---|---|---|
| `SqlPrecedence` | [event_compiler.py:121](../../event_compiler.py#L121) | `OR < AND < ATOM` |
| `CompiledCondition.precedence` | [event_compiler.py:139](../../event_compiler.py#L139) | 조각이 자기 결합도를 싣고 다닌다 |
| `as_conjunct` | [event_compiler.py:188](../../event_compiler.py#L188) | AND 문맥 삽입 — 필요할 때만 괄호 |
| `as_raw_conjunct` | [event_compiler.py:225](../../event_compiler.py#L225) | 결합도를 **잃은** 문자열(카탈로그 선언·모듈 경계) |
| `has_top_level_disjunction` | [event_compiler.py:146](../../event_compiler.py#L146) | 깊이 0 의 `OR` 스캐너(리터럴·식별자 제외) |
| `_assert_conjunction_safe` | [event_compiler.py:206](../../event_compiler.py#L206) | AND 결합 직전 런타임 방벽 |

합성 결과에 **잉여 괄호를 붙이지 않는다**. 결합도가 `AND` 이상인 조각은 AND 문맥에서 뜻이
변하지 않으므로 기존 SQL 은 바이트 동일하고, 실제로 바뀐 것은 새던 자리뿐이다.

**한 뿌리, 네 증상.** 매퍼가 별개로 보고한 네 지점은 전부
`compile_relation(Filter)` 하나를 지난다. 그래서 [event_compiler.py:916](../../event_compiler.py#L916)
한 줄로 넷이 닫혔다.

| 모양 | 누수했을 때의 결과 |
|---|---|
| `Exists(Filter(Or))` | 세그먼트가 전체 회원 |
| `Not(Exists(Filter(Or)))` | 세그먼트가 공집합(방향만 반대) |
| 상관 스칼라 집계 | COUNT 가 팩트 테이블 전체를 셈 |
| 멤버십 낮춤 | NULL 가드가 엉뚱한 분기에 붙음 |

**감사에 없던 누수 하나를 더 찾았다.** [graph_rag.py:13226](../../graph_rag.py#L13226) 은 "이 조각이
분기인가"를 **최상위 IR 노드 타입**으로 추측하고 있었고, `And((Or(a, b),))` 에서 틀렸다 —
`_combine` 이 피연산자가 하나면 그대로 돌려주므로 노드는 `And` 인데 문자열은 최상위가 `OR` 이다.
이 자리는 최외곽 WHERE 라서, 새면 **회원 상태 술어가 분기 밖으로 나간다**. 추측을 사실
(`as_raw_conjunct`)로 바꿨다.

**런타임 방벽은 한 번도 울리지 않았다**(3926개 테스트). 모든 생산자가 안전 경로를 탄다는 뜻이다.

### Phase 2 — 임계값의 적용 grain (감사 A2 · P0)

**결함.** `총 구매금액이 30만원 이상인 회원`(합계)과 `구매금액이 30만원 이상인 주문을 한 회원`
(그런 주문의 존재)이 **바이트 동일한 SQL** 을 냈다. 실DB 기준 9,585명 대 688명 — 14배다.
임계값에 값과 단위만 있고 **적용 grain** 이 없었기 때문이다.

**고친 방법.** [grain_claims.py](../../grain_claims.py) 신설. 세 책임을 분리한다.

```text
원문  →  detect_grain_claims     표면 주장
IR    →  expression_grains       트리가 실현한 grain(순수 IR · 원문을 읽지 않음)
IR    →  regrain_to_row          IR → IR 낮춤(새 노드 없음)
```

**새 한국어 목록을 만들지 않았다.** 필요한 어휘는 이미 선언돼 있었다 —
`aggregate_sum_marker` · `change_inclusive_bound` · `change_strict_bound` · `event_count_unit` ·
`event_alias_*`. row 규칙이 "주문에 대한 규칙"이 아니라 **머리(head)에 대한 규칙**이라, 새 사건
별칭이 어휘에 늘면 규칙을 고치지 않아도 함께 열린다(`lexicon_patterns.vocabulary_names()` 추가).

생산 배선은 표현이 확정되는 자리 하나다 —
[audience_execution.py:1491](../../query_structurer/audience_execution.py#L1491) 이
[`_settle_threshold_grain`](../../query_structurer/audience_execution.py#L825) 을 부르고, 바꾼 사실은
`plan_decisions` 로 남아 응답의 `decisions` 에 드러난다(조용히 바꾸면 의미 검증기가 자기 SQL 을
근거 없는 조건으로 되잡는다).

**개입 조건이 이 축의 핵심이다.**

| 원문 | 주장 | 동작 |
|---|---|---|
| `총 …이상인 회원` | subject | 트리가 이미 subject — 무개입 |
| `…이상인 주문을 한 회원` | row | 충돌 → `EXISTS(...)` 로 낮춤 |
| `2019년에 이십만원 이상을 구매한 고객` | 없음 | **아무것도 바꾸지 않는다** |

세 번째 줄이 정책이다. 원문이 정하지 않은 자리에 기본값을 넣는 것이 곧 추측이므로(§12),
주장이 없으면 기존 동작이 그대로 유지된다 — 지금 나가던 SQL 이 사라지지 않는다.

> 구현 중 이 모듈 자신에게서 버그를 하나 잡았다. `이상인 **회**원` 의 `회` 가 계수 단위로
> 매치됐다(한글에는 낱말 경계가 없다). 조사 기반 오른쪽 경계로 막고 테스트로 고정했다.

### Phase 5 — 선언으로 연 축 (감사 C)

브랜드와 주문 디바이스를 [audience_catalog.json](../data/runtime/semantics/audience_catalog.json)
**선언만으로** 열었다. 새 컴파일러 분기 0줄이다 — `서로 다른 브랜드를 정확히 두 개`는 장바구니
상품 종류를 세던 그 `aggregate.count_distinct` 를 그대로 탄다.

물리 근거는 실DB(CRMDW)로 먼저 확인했다.

| 컬럼 | 실측 |
|---|---|
| `CRM_CM_PRODUCT.BRAND_ID` · `BRAND_NAME` | 199행 전부 채워짐 · 브랜드 25종 |
| `CRM_SL_ORDERHEADERMALL.DEVICE_TYPE_CD` | PC 116,530 · APP 69,195 · MW 44,717 |

브랜드 조인은 **이미 있었다** — `purchase_line` 소스의 `from_sql` 이 상품마스터를
`{alias}_PRODUCT` 로 LEFT JOIN 한다. 브랜드는 조인 문제가 아니라 선언 부재였다.

주문 디바이스는 회원측 `login_channel` 과 같은 코드 사전을 쓰지만 **축이 다르다**(회원의 마지막
상태 vs 주문 건의 속성). 저쪽은 `B.LAST_LOGIN_CHANNEL` 에 묶인 eq_filters 정체성이라 주문 헤더
컬럼을 대신 소유할 수 없으므로 `order_device` 를 인라인으로 선언했다.

**이 축이 Phase 1 의 값어치를 실데이터로 보여준다.** `앱·모바일웹·PC 중 하나로 주문`(감사 #54):

| | 회원 수 |
|---|---|
| 괄호 누락(옛 컴파일러) | **69,609** = 전체 회원 |
| 현재 | 61,212 |

Phase 1 이 없었다면 **이번에 연 축이 그대로 새 조용한 오답**이 됐을 것이다. 순서를 지킨 이유가
이것이다.

부수적으로 능력/카탈로그 모순도 해소됐다. `requirement_capabilities.json` 은
`purchase.qualifiers.brand.supported = true` 라고 광고해 왔는데 카탈로그에는 브랜드 필드가
**0개**였다. 광고를 내리는 대신 필드를 선언하는 방향으로 맞췄다.

> **별칭 함정(실측).** 값 별칭에 `앱` 을 넣자 `앱푸시 수신 동의` 의 `앱푸시` 안에서 매치돼
> 동의 축 4종이 `catalog_value` 극성 검증에서 깨졌다. 저장소가 이미 문서로 적어 둔 규칙
> ("다른 실재 컬럼의 표면어를 접두어로 갖는 짧은 별칭 금지")이다. 한 낱말 별칭을 빼고 두 토큰
> 이상으로 바꿔 해결했고, 새로 연 도메인에 한정한 가드를 붙였다 — 전 카탈로그에 걸면
> `'동의' ⊂ '이메일 수신 동의'` 같은 **같은 축 안의 정상 포함**이 84쌍이라 탐지력 없는 red 가 된다.

---

## 2. 계층 소유권 — 지금 상태

### 의미의 Single Source of Truth

| 의미 | 소유자 | 다른 계층이 다시 정하지 않는다 |
|---|---|---|
| 조건 조각의 **결합도** | `CompiledCondition.precedence` | 소비자가 노드 타입으로 추측하지 않는다 |
| 임계값의 **적용 grain** | `grain_claims`(주장) + IR 트리(실현) | 컴파일러는 트리 모양만 본다 |
| 필드·값 도메인·물리 바인딩 | `audience_catalog.json` | 코드에 컬럼명을 적지 않는다 |
| 표면 어휘 | `parser_lexicon.json` + `lexicon_patterns` | 모듈마다 낱말 목록을 만들지 않는다 |

### 각 계층이 더 이상 하지 않는 일

- `_combine` — 자기 결과를 감쌀지 **고민하지 않는다**. 결합도만 선언한다.
- `plan.where` 소비자 — 조각이 안전한지 **검사하지 않는다**. 삽입 헬퍼가 보장한다.
- `graph_rag` 최외곽 WHERE — 분기 여부를 **IR 노드 타입으로 추측하지 않는다**.
- 컴파일러 — grain 을 **선택하지 않는다**. 받은 트리 모양이 곧 grain 이다.
- 브랜드·디바이스 — 전용 빌더가 **없다**. 선언이 기존 범용 경로를 탄다.

### 앞으로 어디에 추가하나

| 추가할 것 | 자리 |
|---|---|
| 새 필드/값 도메인 | `audience_catalog.json` 의 `fields` / `value_domains` — **코드 변경 없음** |
| 새 표면어 | `parser_lexicon.json`(코드 폴백은 `lexicon_patterns._CODE_FALLBACK`) |
| 새 조합 연산자 | `event_ir` 노드 + `compile_condition` 분기, 결합도 선언 필수 |
| 새 의미 주장 | `*_claims.py` 규약(표면 → typed 값). 컴파일러·판정자는 typed 값만 본다 |

> **주의.** 공개 심볼을 만들고 생산 소비자를 붙이지 않으면
> `test_unwired_symbol_ratchet` 가 red 가 된다. 이번에도 `grain_claims` 를 배선하기 전에 걸렸다.
> 저장소가 반쯤 지은 추상을 거부하는 장치이니 우회하지 말 것.

### 같은 버그를 막는 불변식

| 불변식 | 파일 |
|---|---|
| 임의 And/Or/Not 트리를 컴파일해 **실행 결과**가 기준 구현과 같다(전수 열거 · sqlite) | [test_boolean_composition_invariant.py](../../tests/test_boolean_composition_invariant.py) |
| AND 문맥 삽입 후 조각에 깊이 0 `OR` 이 없다 | 〃 |
| `RelationPlan.where` 의 모든 항목이 AND 결합에 안전하다 | 〃 |
| 상관 서브쿼리 네 모양 전부에서 분기가 괄호 안에 남는다 | 〃 |
| row grain 과 subject grain 이 **다른 SQL** 을 낸다 | [test_threshold_grain.py](../../tests/test_threshold_grain.py) |
| 원문이 grain 을 말하지 않으면 주장이 없다 | 〃 |
| 광고된 브랜드 지원이 실제 카탈로그 필드로 뒷받침된다 | [test_purchase_brand_and_device_axes.py](../../tests/test_purchase_brand_and_device_axes.py) |
| 새 값 별칭이 다른 축의 표면어를 잠식하지 않는다 | 〃 |

전수 열거를 쓴 이유는 hypothesis 가 이 저장소의 의존이 아니기도 하지만(§56), **전수가 재현
가능하기 때문**이다 — 실패한 트리가 실행마다 달라지지 않는다(§63).

---

## 3. 남은 부채 — 하지 않은 것과 그 이유

감사의 케이스 중 B·D·E·F·G 는 **손대지 않았다**. 아래는 조사로 확인한 사실과, 다음 사람이
바로 이어갈 수 있는 순서다.

### F. 선언된 disposition 이 버려진다 (확인됨 · 착수 전 선행 작업 필요)

`temporal_claims` 는 **11곳**에서 `disposition=CLARIFICATION` 을 선언하는데,
[audience_execution.py](../../query_structurer/audience_execution.py) 의 소비자는 그 값을
`payload[TEMPORAL_REJECTION_KEY]` 에 적어만 두고 `status="unsupported"` 를 하드코딩한다.
선언된 사유가 소비자 없이 죽어 있는 것이다.

**그런데 이것만 고치면 감사 #73 이 퇴행한다.** `여성이면서 정상에서 휴면으로 바뀐 회원` 은
지금 `VALUE_COUNT_MISMATCH`(disposition=clarification)로 반려되는데, 감사는 그 **귀결**
(`unsupported`)이 옳고 **이름**이 틀렸다고 적었다. 회원 상태 이력 소스가 실재하지 않으므로
문장을 고쳐도 열리지 않는다 — `clarification` 으로 뒤집으면 운영자가 사용자에게 고칠 수 없는
것을 고치라고 안내하게 된다.

순서:

1. 상태 이력 부재를 **그 이유로** 반려하게 한다(코드 선택 교정, disposition=unsupported).
2. 그 다음에 선언된 disposition 을 귀결로 존중한다.

거꾸로 하면 맞게 동작하던 케이스가 깨진다.

### B. 컴파일되는데 미지원으로 닫는다 (가장 큰 남은 작업)

지원을 판정하는 계층이 **약 15개**이고, 실제로 canonical 표현을 만들어 컴파일해 보는 것은
[lowering_planner.py](../../lowering_planner.py) 하나다. 그런데 그 모듈의 **비테스트 호출자 7개가
전부 거부 위치**에서만 쓴다 — 미지원 판정을 반박할 수는 있어도 지원을 스스로 생산하지 못한다.

목표는 미지원 문구를 내기 **전에** 후보 표현을 컴파일해 보는 경로를 두고, 실패했을 때 나온
컴파일러의 사유가 그대로 사용자 문구가 되게 하는 것이다. 감사 B1·B2(창을 가진 부재, 사건
로그에 대한 칸별 전칭)는 결정론 경로에서 이미 정확한 SQL 이 나오는 것이 확인돼 있다.

### D. 시각 해상도 (Phase 1 이 끝나 이제 안전하다)

IR 은 이미 표현할 수 있다 — `AbsoluteInterval.start_time` / `end_time` 이 HHMMSS 로 검증되고
**경계일 의미**(시작 시각은 첫날, 끝 시각은 마지막 날, 끝 포함)까지 정의돼 있다.
막는 것은 [event_compiler.py:565](../../event_compiler.py#L565) 의 fail-close 하나이고, 그 fail-close
자체는 옳다(날짜 컬럼 하나에 시각 경계를 걸 수 없다).

열려면 소스에 시각 컬럼 바인딩(`purchase.order_time`)을 선언하고 경계일에만 시각을 거는 낮춤을
붙여야 한다. **경계일 vs 중간일을 틀리면 그 자리가 바로 조용한 오답**이라(7월 1일 18:30 이후
주문이 사라진다) 끝까지 검증할 수 있을 때 착수한다.

자정 넘김(`23:00~02:00`)이 요구하는 `OR` 은 Phase 1 로 이미 안전하다.

### G. evidence 가 능력 판정을 덮는다 (원인이 감사와 다르다)

감사는 `#47` 이 "근거 스팬 요구"에 막혔다고 적었는데, 조사 결과 **검증기는 범인이 아니다** —
좁은 per-field evidence 를 가진 DNF 는 리졸버를 issue 0 으로 통과한다. 실제 차단은 둘이다.

- **모델이 `consent_count` 라는 문자열을 정확히 써야** 구제 합성이 열린다(자유 텍스트 argument 라우팅).
- **문장의 모든 리터럴 바인딩이** 수량자 매치 안에 있어야 한다 — 무관한 리터럴 하나(`최근 30일`)면 구제가 죽는다.

즉 고칠 곳은 evidence 회계가 아니라 **구제 진입 조건**이다.

---

## 4. 이 문서를 거짓이 되지 않게 하는 것

위 §2 의 불변식 표는 전부 실제 테스트에 대응한다. 표에 줄을 추가할 때는 대응하는 테스트를 함께
만들고, 테스트를 지울 때는 줄도 지운다.

감사 문서의 기준선 갱신에 대한 경고는 **아직 유효하다**. A1·A2 는 닫혔지만 B·D·F·G 가 열려 있고,
라이브 코퍼스는 같은 코드로 두 번 돌려도 귀결이 갈린다. 갱신한다면 결정론 테스트가 green 인
것을 먼저 확인하고, `sql` 이 나왔다는 사실만으로 성공으로 굳히지 않는다.
