# 관측 선택자(최근·현재·최신·직전·이전) — 원인과 구현

상태: **구현 완료 2026-08-08**. 아래 1절은 고친 원인, 2절은 지금의 설계, 3절은 남은 것.

시작점: `최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘` 가
`failure_stage 1/7 타겟 조건 인식` 으로 막혔다
(`The phrase '최근' has no duration; a period is required to interpret '최근 상태'.`).

---

## 1. 원인 — 낱말이 뜻을 정하고 있었다

| # | 자리 | 무엇이 잘못됐나 |
|---|---|---|
| ① | [prompt.py](../query_structurer/prompt.py) | 맨 '최근'이면 **무조건** `missing_argument(period)` 로 닫으라고 지시. `최근 상태`(최신 관측 선택)와 `최근 30일 구매`(창)를 가르는 단서가 없었다. |
| ② | [audience_issue_contract.py](../audience_issue_contract.py) | 기간 결핍 가드가 `source_latest_selector = ('최신','최근')` 을 시간 한정어로 세는데, 그 '최근'이 **판정 대상 자신**이었다 — 자기가 다루려던 표지에서 한 번도 발화하지 못하는 가드. |
| ③ | [temporal_claims.py](../temporal_claims.py) | `PreviousSelector(previous_kind="bucket")` 하드코딩. `직전 상태` 가 '지난달 칸의 현재값'으로 내려가, 적재가 한 달(201701)인 배포에서는 조건이 통째로 비었다. |
| ④ | [targeting_domain.py](../targeting_domain.py) | `최근/현재 + 축` 마커가 아예 없어 1절('최근 상태가 VIP')은 의무도 계획도 만들지 않았다 — 뜻이 사라져도 아무 흔적이 없었다. |
| ⑤ | [transition_claims.py](../transition_claims.py) | 전이를 **문형**(`A에서 B로 …`)으로만 탐지. 같은 뜻의 두 절 문형은 전이가 되지 못했다. |

공통 원인 하나: 선택자 낱말의 뜻을 **어휘 단계에서 확정**했다.

---

## 2. 설계 — 머리(head)가 뜻을 정하고, 데이터 계약이 낮춤을 정한다

### 2-1. 어휘: 선택자 후보 + 머리

[targeting_domain.py](../targeting_domain.py) 의 `_OBSERVATION_SELECTOR_CUES` 한 표가
`최근·최신·현재·지금 → AS_OF`, `직전·이전 → IMMEDIATELY_PRECEDING` 을 소유한다. 마커 템플릿은
**선택자 낱말 + 머리**를 함께 문다:

```
(?:{as_of_cues})\s*{particles}\s*(?:{axis}|{values})
(?:{previous_cues})\s*{particles}\s*(?:{axis}|{values})
```

머리를 패턴 안에 두는 것이 요점이다 — 낱말만 보면 `최근 30일 구매`가 관측 선택자가 되고,
머리를 마커 밖에서 찾으면 같은 절의 다른 낱말이 머리 행세를 한다. `temporal_head_kind()` 가
선언된 축·값 어휘에서 머리 종류를 파생하고, 그것이 뜻을 정하는 **유일한** 근거다(I1).

### 2-2. 청구: axis + selector + value

새 IR 노드는 없다. 기존 `selector × quantifier × predicate` 조합에 축 하나가 붙었을 뿐이다.

| 원문 | axis(metric) | selector | value |
|---|---|---|---|
| `최근 상태가 VIP` | `member.grade` | AS_OF(현재값) | `vip` |
| `직전 상태는 골드` | `member.grade` | PREVIOUS(observation) | `gold_grade` |

`_observation_plan()` 이 머리를 보고 `previous_kind` 를 확정한다 — 속성 축이면 관측,
그 밖이면 종전대로 달력 칸(I1·I3).

### 2-3. 정규화: 전이는 문형이 아니라 짝에서 나온다(I5)

`_merge_state_transitions()` 가 **이미 만들어진 청구** 중 (지금 값 · 직전 값) 쌍을 전이 하나로
접는다. 결합 조건은 절 위치도 텍스트 거리도 아니다:

* 같은 지표(= 같은 엔터티의 같은 의미 축)
* 같은 시점(anchor)
* 양쪽 다 동등 비교 상태 술어, 값이 서로 다름
* 직전 쪽이 관측(observation) 선택자

그래서 아래 다섯 문형이 **한 EXISTS 안의 두 비교**로 수렴한다.

```
골드에서 VIP로 승급한 회원
현재 등급이 VIP이고 이전 등급이 GOLD
최근 상태가 VIP이고 직전 상태는 골드였던 회원
최근 등급은 VIP, 이전 등급은 GOLD
직전에는 GOLD였는데 지금은 VIP
→ MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP' AND MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'
```

### 2-4. 소유권: '지금 값'만 있는 절은 이 계층이 갖지 않는다

짝을 이루지 못한 CURRENT/LATEST 청구는 **조건을 만들지 않는다**. `현재 등급이 VIP` 의 뜻은
현재값 자산이 그대로 답하고(`B.EMART_GRADE_CD`), 그 위에서 '현재'는 동어반복이다. 관측
조건을 함께 내면 같은 조건에 주인이 둘 생기고, 스냅샷 적재 월이 앵커와 다른 배포에서는 답까지
달라진다. 같은 이유로 요구 원장도 그 절을 **이력 의무로 기록하지 않는다**
(`targeting_domain.selects_current_value()` 가 두 소비자의 단일 판정자다).

소유하지 않는 것과 **잃는 것**은 다르다(I6) — 축과 값은 카탈로그 값 청구로 그대로 남는다.

### 2-5. 낮춤: 논리 선택자 ≠ 저장소 조회

`treg.state_value_field()` 하나가 (논리 선택자 × 선언된 저장 계약) → 읽을 필드를 답한다.

```
PREVIOUS(observation) + prev_value_field 선언 있음 → PREV_*  (같은 칸)
PREVIOUS(bucket)                                   → 값 컬럼 (앞 칸)
prev_value_field 선언 없음                          → temporal_previous_value_unavailable
```

`temporal.previous_observation` 은 이제 낮춰진다. 판정 근거를 capability 문자열에서 **스키마
모양**으로 옮겼다 — 한 행에 직전 값이 있으면 정렬 없이 읽을 수 있고, 없으면 이름을 대며 닫힌다.

### 2-6. 기간 결핍은 **만들어지지 않는다**(I4)

선택자로 해석된 낱말은 세 소비자가 모두 같은 판정을 본다.

| 소비자 | 무엇을 하는가 |
|---|---|
| `audience_validators` | 그 '최근'에 결핍을 **만들지 않는다** |
| `audience_issue_contract._has_external_temporal_qualifier` | 그 낱말을 기간 한정어로 세지 않는다(자기 참조 제거) |
| `audience_execution.validate_audience_issue` | 모델이 그 자리에 낸 기간 결핍을 **계약 위반으로 반려**(→ 재방출) |

신고한 뒤 겹침으로 취소하는 구조가 아니다. 프롬프트는 완화했지만 판정의 source of truth 는
프롬프트가 아니라 애플리케이션이다.

### 2-7. 적재 범위는 의미를 바꾸지 않는다

`최근 상태` 를 '적재된 마지막 달'로 재해석하지 않는다. 요청한 칸을 그대로 읽고, 적재 밖이면
`audience_coverage_warnings: ["out_of_coverage"]` 로 드러낸다(예전에는 이 경고가 백필 경로에서
버려졌다).

---

## 3. 남은 것

* `VIP이고 이전 등급이 GOLD인 회원` — 맨 값 `VIP` 에는 선택자 낱말이 없다. 전이로 합치려면
  "문장 안 현재값 아무거나 직전 값과 짝짓는다"는 넓은 heuristic 이 필요해 **하지 않았다**.
  결과는 `PREV_ZTS_GRADE='GOLD'`(이력 계층) + `EMART_GRADE_CD='VIP'`(현재값 자산)로, 집합은
  같고 소유자만 둘이다.
* `직전 구매` — 머리가 사건이라 관측 선택자가 아니고, 사건의 '직전 발생'을 고르는 실행
  primitive 도 없다. 마커를 만들지 않으므로 이 계층은 침묵한다(종전과 같음).
* `temporal.previous_distinct_value` — 여전히 선언된 미지원(Lag/윈도 함수 필요).
* 회원 **상태**(정상/휴면) 축 — 물리 부재로 영구 미지원. `직전 상태는 휴면` 은
  `temporal_metric_not_declared` 로 정직하게 닫힌다.
* `latest_at_or_before` 앵커 전환(적재된 마지막 달을 '지금'으로 읽기)은 **하지 않았다** —
  모든 스냅샷 질의의 의미가 바뀌는 정책 결정이다.
