# Resolution Policy · Clarification 계층

Canonical Request IR 과 Lowering Planner 사이에 **확정 계층**을 둔다. 목적은 한 문장이다.

> 자연어가 조금 부족해도 운영 정책으로 안전하게 확정할 수 있는 것은 자동으로 채워 SQL 까지
> 진행하고, 정책으로 정하면 결과가 크게 달라지는 것만 사용자에게 묻는다.

```text
Natural Language
  → Semantic Normalizer            (기존)
  → Canonical Request IR           (기존: audience_requirement.expression = Event IR)
  → Semantic Issue Detector        resolution/detection.py
  → Resolution Policy              resolution/policy.py     AUTO_RESOLVE | ASK_USER | UNSUPPORTED
      ├─ Auto Resolution / Applier resolution/applier.py · resolution/slots.py
      └─ Clarification Planner     resolution/clarification.py
  → (고정점 반복)                   resolution/loop.py
  → Lowering Planner               (기존)
  → SQL Compiler                   (기존)
  → Outcome Mapper                 (기존) + resolution/projection.py
```

## 왜 이 계층인가

같은 금액·기간·연산자인데 뜻이 다른 두 문장이 **바이트 동일한 SQL** 을 냈다(실측 2026-08-08,
라이브 id 42/43).

```text
최근 90일 동안 총 구매금액이 30만원 이상인 회원        9,585명   (회원별 합계)
최근 90일 동안 구매금액이 30만원 이상인 주문을 한 회원     688명   (그런 주문의 존재)
```

원문이 grain 을 말한 경우는 `grain_claims` 가 이미 맞춰 넣는다. 문제는 **원문이 말하지 않은**
경우다 — 그때까지는 모델이 고른 트리 모양이 곧 답이었고, 사용자는 자기가 어느 쪽을 받았는지
알 방법이 없었다. 14배 차이가 어디에도 드러나지 않는다.

거꾸로, 기간 없는 '최근' 하나 때문에 전부 되묻는 것도 답이 아니다. 그 값은 제품이 정할 수
있는 운영 정책이다.

두 경우의 차이는 **위험도**다. 그래서 이 계층은 결핍마다 위험도를 값으로 들고, 모드가 그
허용선을 정한다.

## 결정 규칙

`resolution/policy.py::ResolutionPolicy._decide` 의 순서가 곧 계약이다.

| 순서 | 조건 | 귀결 |
| --- | --- | --- |
| 1 | 미지원 계열(capability) | `UNSUPPORTED` — 질문하지 않는다 |
| 2 | 설정이 `require_clarification` 선언 | `ASK_USER` |
| 3 | 위험도 > 모드 허용선 | `ASK_USER` |
| 4 | 설정이 `allowed_auto_resolution` 미선언 | `ASK_USER` |
| 5 | 값 해결기 없음 / 값 없음 | `ASK_USER` |
| 6 | 값은 있는데 **넣을 자리가 없음** | `ASK_USER` |
| 7 | 그 외 | `AUTO_RESOLVE` |

모드별 자동 확정 허용 위험도:

```text
strict          없음
safe_defaults   LOW            (기본값)
best_effort     LOW + MEDIUM
```

`HIGH` 는 어느 모드에도 없다. row/subject grain, AND/OR, 부재 의미, 엔터티 식별자가 여기
속한다 — 잘못 고르면 대상 집합이 통째로 달라진다.

## 설정

- 판정 선언: `docs/data/runtime/policies/resolution_policy.sample.json`
  (경로 override: `RESOLUTION_POLICY_PATH`, 모드 override: `AUDIENCE_RESOLUTION_MODE`)
- **값은 여기 적지 않는다.** 기간 기본값의 소유자는 `default_period_policy` 이고
  (`AUDIENCE_DEFAULT_PERIOD` env · `qualitative_defaults` 카탈로그), 정책은 그 소유자에게
  묻기만 한다. 같은 사실을 두 곳에 적으면 "구조화기가 채운 창"과 "정책이 인정하는 창"이 갈린다.
- 설정을 읽지 못하면 **strict 로 닫는다**. 오타 하나가 조용히 기본값 적용으로 흐르지 않는다.

## 결핍 종류를 여는 방법

1. `resolution/issues.py::ISSUE_KIND_SPECS` 에 항목 하나(계열·위험도·슬롯·질문 모양).
2. `resolution/detection.py::DETECTORS` 에 그 종류를 내는 감지기 하나.
3. 자동 확정이 필요하면 `resolution/policy.py::AUTO_RESOLVERS` 에 값 해결기 하나.
4. 새 슬롯이면 `resolution/slots.py::SLOT_APPLIERS` 에 적용기 하나.

생산자가 없는 kind 는 선언하지 않는다 — 죽은 어휘는 "지원한다"는 거짓 신호가 된다.

## 응답 계약

`api_response.resolution`

```json
{
  "status": "needs_clarification",
  "resolution": "assumed",
  "mode": "safe_defaults",
  "assumptions": [
    {"code": "DEFAULT_RECENT_PERIOD", "slot": "audience.period",
     "value": {"type": "rolling", "value": 30, "unit": "day"},
     "provenance": "policy_default", "issue_id": "…", "evidence": {"text": "최근", "start": 0, "end": 2}}
  ],
  "questions": [
    {"question_id": "q-…", "issue_id": "…", "code": "AMBIGUOUS_AMOUNT_GRAIN",
     "text": "'20만원 이상 구매' 기준은 한 번의 주문인가요, 기간 내 합계인가요?",
     "slot": "comparison.grain",
     "options": [{"id": "row", "label": "한 번의 주문 금액"},
                 {"id": "subject", "label": "기간 내 총 구매금액"}],
     "allow_free_text": false}
  ]
}
```

기존 `clarification_questions[]`(문자열)은 이 블록에서 **파생된** 호환 표기다. 방향은 언제나
typed → legacy 이고, 내부 구조를 문자열 모양에 맞추지 않는다.

## 되묻기 답변

요청 본문에 실어 보낸다. **원문은 바뀌지 않는다.**

```json
POST /target-sql
{"prompt": "최근 20만원 이상 구매한 회원", "clarification_answers": [{"issue_id": "…", "option_id": "row"}]}
```

`issue_id` 는 **내용 주소**다(순번이 아니다) — 같은 원문·같은 자리·같은 종류이면 회차가 달라도
같은 값이라, 질문 수가 늘거나 줄어도 답이 엉뚱한 자리에 붙지 않는다.

적용은 두 가지다.

- **슬롯 패치**: 답이 가리키는 자리만 바꾼다(`comparison.grain`, `audience.period`).
  나머지 절의 창·값·극성·근거는 바이트 동일하게 남는다.
- **엔터티 결속**: 표현이 서지 못해 되묻는 자리(가장 흔하다)에는 넣을 슬롯이 아직 없다. 답을
  타입 있는 결속으로 들고 있다가 같은 구조화기에 **애플리케이션 소유 값**으로 알린다
  (`resolution/applier.py::render_entity_binding_instruction`). 답변 문자열을 원문에 이어
  붙이지 않는다.

## 불변식

`tests/test_resolution_invariants.py` 가 계약으로 잰다.

| | 불변식 |
| --- | --- |
| A | 운영 기본값은 정책 계층 밖에서 만들어지지 않는다 |
| B | 질문 생성기 입력은 `ASK_USER` 결정뿐이다 |
| C | 자동 확정된 결핍은 질문이 되지 않는다 |
| D | 미지원 결핍은 질문이 되지 않는다 |
| E | 답변은 그 결핍의 슬롯 밖 의미를 바꾸지 않는다 |
| F | 답변 처리는 원문을 다시 해석하지 않는다(정규화기 import 없음) |
| G | 사용자 명시가 아닌 의미가 SQL 에 있으면 provenance 영수증이 반드시 있다 |
| H | HIGH 위험 모호성은 명시적 답변 없이 사라지지 않는다 |

F 는 예시로 증명되지 않는다 — 적용기가 언젠가 정규화기를 부르기 시작하면 그날 이후의 모든
답변이 원문 전체를 흔들지만 어떤 기능 테스트도 그것을 보지 못한다. 그래서 import 그래프를
직접 잰다.

## 관측

`resolution/observability.py` 가 `resolution.settled` 구조화 로그를 낸다. 원문·개인정보·SQL 은
싣지 않는다 — 코드·개수·모드뿐이다.

```text
outcome                   exact | auto_resolved | clarification | unsupported
auto_resolution_by_code   어떤 기본값이 얼마나 자주 쓰였나
clarification_by_code     무엇을 얼마나 자주 물었나
provenance_counts         policy_default / semantic_inference / user_clarification
rounds · answer_count · deferred_question_count · unapplied_answer_count
```

나중에 "이 기본값이 실제 사용자 의도와 맞는가"를 물으려면 그때 데이터가 있어야 한다.
