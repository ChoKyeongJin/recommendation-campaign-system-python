# 파서 이행 운영 절차

"새 표현이 나올 때마다 파이썬을 고치는" 구조를 "주 1회 분류하는" 구조로 바꾸는 프로그램의 운영 문서다.

## 원칙 하나

새 표현이 들어오면 셋 중 하나다. **C 만 코드다.**

| | 무엇 | 어디에 | 배포 |
|---|---|---|---|
| **A 어휘** | 기존 능력의 새 표면어 | `docs/data/parser_lexicon.json` 의 `vocabularies` | 불필요 |
| **B 파라미터** | 기존 팩트의 새 조건 모양 | `targeting_ir.CONDITION_SPECS` / `_FilterSpec` 레지스트리 | 필요 |
| **C 능력** | 새 grain·조인이 필요 | 새 ConditionSpec + 빌더 | 필요 |

A 가 코드로 가는 것이 원래 문제였다. C 가 코드인 것은 정상이다.

## 주간 루틴 (15분)

```bash
docker compose exec -e PYTHONPATH=/app -w /app api \
  python tools/weekly_triage.py --unresolved logs/unresolved.jsonl --shadow logs/parser_shadow.jsonl
```

`docs/weekly_triage.md` 를 열고 **`decision` 열만 채운다.**

1. **§1 이행 지표** — `조용한 소실 슬롯` 이 0 이 아니면 그것부터 본다. 그 슬롯은 값을 못 만들 때
   아무 표시 없이 사라지므로, 미해석 큐에 잡히지 않는다. 즉 루프의 사각지대다.
2. **§2 미해석 표현** — 빈도순. 초안(A/B/C)은 정렬용이지 판정이 아니다. 판정 후:
   - A → 사전 어휘에 낱말 추가 → `pytest tests/` → 끝 (배포 불필요)
   - B → 레지스트리 한 줄 + 골든 케이스 추가
   - C → `cases.json` 에 `known_gap` 으로 먼저 기록하고 별도로 설계
3. **§3 슬롯 승격 후보** — `막는 것` 이 비어 있는 슬롯만 `docs/data/slot_policy.json` 에서
   `owner: llm` 으로 바꾼다. 문턱을 손으로 낮추지 않는다.

## 환경 변수

| 변수 | 값 | 뜻 |
|---|---|---|
| `UNRESOLVED_LOG` | 경로 | 미해석 큐 적재 위치. **미설정이면 큐가 안 쌓여 루프가 돌지 않는다** |
| `PARSER_SHADOW_MODE` | `off`(기본)/`shadow`/`enforce` | shadow 는 관찰만, enforce 는 LLM 소유 슬롯만 채택 |
| `PARSER_SHADOW_LOG` | 경로 | shadow 관찰 누적 위치(승격 판단의 유일한 근거) |
| `PARSER_LEXICON_PATH` | 경로 | 파서 표면어 사전 |
| `SLOT_POLICY_PATH` | 경로 | 슬롯 소유권 정책 |
| `TARGET_OBJECT_LLM_FALLBACK` | `true`(기본)/`false` | 상품명 LLM 후보. **끄면 상품 조건이 조용히 사라진다** |

## 안전장치 (전부 `pytest tests/` 가 강제)

| 장치 | 파일 | 막는 것 |
|---|---|---|
| 골든 IR 스냅샷 | `tests/golden/` | 파서 변경이 조건을 조용히 잃는 것 |
| `known_gap` 마커 | `tests/golden/cases.json` | 결함을 스냅샷으로 축복하는 것 (고쳐지면 마커를 지우라고 실패) |
| 어휘형 정규식 래칫 | `docs/data/regex_inventory_baseline.json` | 새 표면어를 또 코드로 받는 것 |
| rule 생산자 래칫 | `tests/golden/method_mix_baseline.json` | 조건 생산자가 정규식으로 늘어나는 것 |
| 조용한 소실 상한 | `tests/test_slot_policy.py` | 백스톱도 fail-close 도 없는 슬롯이 느는 것 |
| 이관 동등성 | `tests/test_lexicon_patterns.py` | 사전으로 옮기며 몰래 어휘를 넓히는 것 |
| 미해석 오탐 | `tests/test_ir_golden_corpus.py` | 탐지기가 정상 프롬프트를 잡아 큐를 잡음으로 덮는 것 |

## 재생성 명령

```bash
# 골든 스냅샷 + 방법 구성 기준선 (diff 를 눈으로 검토한 뒤 커밋)
python tools/regen_ir_goldens.py

# 정규식 인벤토리 (--set-baseline 은 어휘형 상한을 내릴 때만)
python tools/regex_inventory.py [--set-baseline]
```

## 남은 결함 (2026-07-29 기준)

- **조용한 소실 2건** — `target_user.purchase_object` / `purchase_object_kind`. 결정론 백스톱이 없고
  fail-close 표시도 없다. 7단계의 종료 조건은 이 값이 0 이 되는 것이다.
- **부분 소실은 탐지 못 한다** — 미해석 탐지기는 "조건이 하나도 없는" 경우만 잡는다. `안양에 사는`
  처럼 일부만 빠지는 것은 shadow 비교의 `only_candidate` 판정이 담당하므로, shadow 를 켜야 보인다.
- **어휘형 정규식 8개** — `_BALANCE_*` 3, `_THRESHOLD_CUE_RE`, `_OUTPUT_ACTION_RE`,
  `_MEMBER_METRIC_COMPARISON_RE`, `_TREND_ORDER_MARKER_RE`, `_NOISE_LABEL`(빌드 스크립트).
- **업무의미형 149개** — 어휘와 구조가 섞여 건별 판단이 필요하다. `docs/regex_inventory.md` 참조.
