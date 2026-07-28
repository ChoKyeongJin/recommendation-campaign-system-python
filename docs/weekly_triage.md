# 주간 이행 리포트

## 1. 이행 지표

| 지표 | 값 | 뜻 |
|---|---:|---|
| 어휘형 정규식 | 8 | 코드에 남은 표면어 목록. 0 이 목표 |
| 업무의미형 정규식 | 149 | 어휘+구조 혼합. 어휘 부분만 분리 대상 |
| 문법형 정규식 | 31 | 구조만. 코드에 남는 것이 정상 |
| LLM 소유 슬롯 | 2 | 정책상 LLM 이 확정하는 슬롯 |
| 조용한 소실 슬롯 | 2 | 백스톱도 fail-close 도 없는 자리. **0 이어야 한다** |
| shadow 관찰 | 0 | 누적 비교 건수 |
| 미해석 표현 | 1 | 이번 큐에 쌓인 서로 다른 표현 |

### ⚠ 조용한 소실이 남아 있다

이 슬롯들은 값을 못 만들면 아무 표시 없이 사라진다 — 미해석 큐에 잡히지 않으므로 이 루프가 돌지 않는다.

- `target_user.purchase_object` — 결정론 상품명 추출기가 없어 TARGET_OBJECT_LLM_FALLBACK=false 면 조건이 조용히 사라진다(fail-open). 최소한 fail_close 표시가 필요하고, 이상적으로는 상품 마스터 기반 백스톱을 둔다.
- `target_user.purchase_object_kind` — purchase_object 와 같은 결함(값의 종속 슬롯).

## 2. 미해석 표현 (A/B/C 분류)

초안: A 어휘 0 / B 파라미터 0 / C 능력 1

A 가 많으면 사전에 낱말이 모자란 것이고, C 가 많으면 정말로 새 능력이 필요한 것이다.
`decision` 은 사람이 채운다 — 초안은 정렬용이지 판정이 아니다.

| 빈도 | 초안 | 표현 | 근거 | 판정 사유 | decision |
|---:|---|---|---|---|---|
| 1 | C | 혼수 준비중인 고객에게 쿠폰 발송 | no_target_conditions | 기존 어휘와 겹치는 뼈대가 없다 — 새 능력일 가능성이 높다 |  |

## 3. 슬롯 승격 후보 (rule → llm)

승격은 누적 shadow 관찰이 위험 등급별 문턱을 넘고, 위험 판정이 0 건일 때만 가능하다.

| 슬롯 | 소유 | 위험 | 관찰 | 일치율 | 위험판정 | 막는 것 |
|---|---|---|---:|---:|---:|---|
| `campaign_constraints.channels` | rule | low | 0 | 0.000 | 0 | observations 0/20, agreement 0.000/0.9 |
| `campaign_constraints.objective` | rule | low | 0 | 0.000 | 0 | observations 0/20, agreement 0.000/0.9 |
| `campaign_constraints.offer_type` | rule | low | 0 | 0.000 | 0 | observations 0/20, agreement 0.000/0.9 |
| `plan.dimension_filters` | rule | medium | 0 | 0.000 | 0 | observations 0/50, agreement 0.000/0.97 |
| `plan.intent` | rule | low | 0 | 0.000 | 0 | observations 0/20, agreement 0.000/0.9 |
| `plan.logical_expression` | rule | high | 0 | 0.000 | 0 | observations 0/200, agreement 0.000/0.995 |
| `plan.member_metric_ranking` | rule | high | 0 | 0.000 | 0 | observations 0/200, agreement 0.000/0.995 |
| `plan.result_limit` | rule | low | 0 | 0.000 | 0 | observations 0/20, agreement 0.000/0.9 |
| `plan.semantic_conditions` | rule | high | 0 | 0.000 | 0 | observations 0/200, agreement 0.000/0.995 |
| `plan.set_expressions` | rule | high | 0 | 0.000 | 0 | observations 0/200, agreement 0.000/0.995 |

승격 가능: 0건

