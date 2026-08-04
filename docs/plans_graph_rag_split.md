# graph_rag.py 분할 — 진행 상황과 다음 단계

> 2026-08-01. 근거: 전수 AST 측정(호출그래프·폐포·SCC·죽은코드) + 단계별 실측.
> 관련: [IR 결합 완화 플랜](plans_ir_decoupling.md) — 이 문서는 그 Wave 5 의 "구조 이동"
> 축과 같은 방향이고, 착수 게이트가 없는 부분부터 먼저 진행한 결과다.

## 왜 나눴나 — 측정이 뒤집은 가설

처음 가설은 "파일이 크니 안 쓰는 코드가 쌓였을 것"이었다. **틀렸다.**

| 측정 | 값 |
|---|---|
| 완전히 죽은 코드 | 10개 함수 **170 LOC (0.6%)** |
| 죽은 상수 | **0개** |
| 내부 호출그래프 SCC(순환) | **0개 — 순수 DAG** |
| `retrieve()` 의존 폐포 | **25,341 LOC (파일의 85%)** |
| 실제 "그래프 RAG" 부분 | **551 LOC (1.9%)** |

즉 29,775줄은 전부 살아 있는 코드였고, 파일이 큰 원인은 미사용 코드가 아니라
**모듈 경계의 부재**였다. 그리고 파일 이름이 내용의 2% 만 설명하니(나머지 98% 는
한국어 NL→타겟팅 SQL 컴파일러) 새 코드가 갈 곳이 없어 계속 여기로 모였다.

SCC 가 0 이라는 사실이 결정적이었다 — 논리적 계층은 **이미 존재했고** 파일 경계만
없었다. 그래서 분할은 설계가 아니라 이동 문제였다.

## 완료 — Phase 0~1

| 단계 | 산출 | graph_rag |
|---|---|---|
| Phase 0 | 죽은 함수 10 + 고아 import 6 제거 | 29,775 → 29,572 |
| Phase 1a | `rag/search.py` (522줄) + `test_module_layering` | → 29,050 |
| Phase 1b | `rag/llm_io.py` (152줄) | → 28,909 |
| Phase 1c | `rag/message.py` + `rag/config.py` (1,211줄) | → 27,722 |
| Phase 1d | `rag/trace.py` + failure_stage · attribute_tokens · plan_inspect (1,436줄) | → **26,307** |

누적 **-3,468줄 (-11.6%)**, 신설 모듈 8개. 전 구간 `1372 passed / preflight ok=true`.

### 순서를 바꾼 이유 (기록해 둘 가치가 있는 실패)

원안은 "폐포가 가장 깨끗한 것부터"(message · trace)였다. 실측해 보니 **둘 다 같은
하부 배관을 필요로 했다** — message 는 8개, trace 는 5개의 graph_rag 잔류 심볼을
참조했고 그 중 5개가 LLM 호출·로그·프롬프트 로딩으로 겹쳤다.

상위를 먼저 떼면 그것들이 배관을 쓰려고 graph_rag 를 되돌아 import 하게 되고,
분할이 순환으로 퇴화한다. 그래서 **바닥(fan-in 최대 리프)부터 올리는** 순서로
바꿨고, 그 뒤로는 매번 "공유 의존 0개"가 나왔다:

```
llm_io(공유 0) → config(공유 0) → message(공유 0)
failure_stage(공유 0) · attribute_tokens(공유 0) · plan_inspect(공유 0) → trace(공유 0)
```

**교훈: 분할 순서는 '떼기 쉬운 것'이 아니라 '아래에 있는 것'으로 정한다.**

### 현재 계층 (의존은 항상 위 → 아래)

```
graph_rag.py (26,307)          NL→SQL 타겟팅 컴파일러 + façade 재수출
  ├── rag/trace.py     (1,129) 10단계 트레이스 조립 · 참조 배지 · 실패 진단
  ├── rag/message.py   (1,257) 채널 정책 · 변형 생성 · 통신사 규격 검증
  ├── rag/search.py      (568) 지식그래프 · 벡터/키워드 검색 · 컨텍스트 조립
  ├── rag/attribute_tokens.py (183) 회원속성 토큰 승격 문법
  ├── rag/llm_io.py      (183) LLM 호출 · 프롬프트 로딩 · RAG LLM 로그
  ├── rag/failure_stage.py(181) 실패 사유 → 파이프라인 단계(프론트 스텝퍼 계약)
  ├── rag/plan_inspect.py (62) "제안된 필터가 실행 IR 에 실제로 붙었는가"
  └── rag/config.py       (45) 기본 경로 · 모델 · 컬렉션
```

부수 효과: `fastembed` · `qdrant_client` 가 graph_rag 에서 빠졌다. 무거운 임베딩
의존이 `vector_search` 한 곳으로 모여, 임베딩 없이 도는 경로를 위한 지연 import
여지가 생겼다(`reference_docs.py:17` 이 우회하려던 바로 그 문제).

### 신설 가드

| 테스트 | 강제하는 것 |
|---|---|
| `test_module_layering.py` | rag.* 는 graph_rag 를 import 하지 않는다 · rag.* 끼리 무순환 · 반출 심볼의 façade 보존 |
| `test_module_size_ratchet.py` | 모듈별 줄 수 상한(하향 전용) · 상한 없는 신규 모듈 금지 · 상한이 헐거우면 조이기 요구 |

둘 다 역검증했다(위반을 심으면 빨강). 기존 `test_graph_rag_facade` 는 이번에
`_message_repair_context` 재수출 누락을 실제로 잡았고, `test_doc_claims` 는
아직 없는 테스트를 근거로 인용한 docstring 을 잡았다 — 설계대로 동작했다.

## 다음 단계

### Phase 2 — 물리 스키마 리터럴 이관
graph_rag 에 물리 테이블 리터럴 68회(고유 11개)가 남아 있다. 이는 크기 문제가 아니라
[DB 이식성 제약](plans_ir_decoupling.md) 축이고, **W5-3 과 동일 작업**이므로 그쪽
항목을 그대로 수행한다(중복 투자 금지). `physical_binding_baseline.json`은 현재 총 31건이며, 모두 삭제 예정 Event IR 레거시에만 남아 있다(활성 비-Event 경로 0건).

### Phase 3 — SQL 빌더 계층 (착수 게이트 있음)
약 5,400줄로 가장 큰 덩어리지만 **지금 손대면 안 된다**. 폐포는 4,791줄인데 공개
API 가 118개로 튄다 — 빌더와 결정론 필터가 헬퍼를 양방향으로 공유한다는 뜻이다.

**선행 필수: W5-1(`build_sql_result` 입력 순수화).** 현재 `build_sql_result` 는 자기
입력 plan 을 재변형하고(19케이스 중 13건에서 호출자 plan 변형 확인), `capability_check`
/`output_contract` 는 파생 키라 골든에서 제외된다. 이 상태로 파일을 가르면 **모든
골든이 초록인 채 API 응답만 얇아진다**. W3-5 하류 계약 테스트가 게이트다.

### Phase 4 — 결정론 필터 본체 (~5,300줄)
`_apply_*` 59개 중 **49개가 이미 `_deterministic_filter_registry` 에 등록**돼 있다.
레지스트리가 있으니 본체를 `rag/filters/*.py` 로 흩고 레지스트리만 남기는 것이
자연스럽다. "새 필터 = spec 한 줄" 규약은 그대로 유지된다.

### Phase 5 — façade 축소
외부 소비처(api.py · tools · tests)를 새 경로로 이관한 뒤
`test_graph_rag_facade.py` 의 151개 목록을 단계적으로 줄인다.

## 작업 방법 (다음 사람용)

각 반출 전에 **반드시 폐포를 먼저 측정한다**. "이 정도면 독립적이겠지"는 매번 틀렸다.
확인할 것은 셋이다:

1. **공유 의존이 0인가** — 0이 아니면 그 심볼들이 더 아래 계층이라는 뜻이다. 그것부터 뺀다.
2. **공개 API 크기** — 100 을 넘으면 경계가 잘못됐다(Phase 3 이 118로 그 예다).
3. **새 모듈이 바깥에서 가져와야 할 이름** — 전부 stdlib/하위 rag.* 여야 한다.

반출 뒤에는 고아 import 를 반드시 확인한다(매번 3~6개씩 나왔다).
façade 재수출은 **계약 목록에 있거나 실제로 쓰는 것만** 남긴다 — 안 쓰는 재수출이
쌓이면 façade 가 계약보다 커져서 Phase 5 에서 줄일 수 없다.
