"""LLM 호출·프롬프트 로딩·RAG LLM 로그 — 파이프라인 전 단계가 공유하는 리프 계층.

graph_rag.py 에서 분리했다. 여기 있는 것은 "어떤 모델에 무엇을 물어보고, 그 왕복을
어디에 기록하고, 프롬프트 원문을 어디서 읽는가"뿐이다 — 도메인 판단은 하나도 없다.
그래서 타겟팅·메시지·의미검증 어느 상위 계층에서도 같은 배관을 쓸 수 있다.

이 모듈이 먼저 분리돼야 하는 이유: fan-in 이 가장 크다(_write_rag_llm_log 25곳,
_read_prompt_template 16곳, _openai_chat_create 15곳). 상위 모듈(rag.message 등)을
먼저 떼어내면 그것들이 배관을 쓰려고 graph_rag 를 되돌아 import 하게 된다 —
분할이 순환으로 퇴화하는 전형적 경로다. 바닥부터 올라간다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 프로젝트 의존은 prompt_store 뿐이다.
소비자는 기존 내부 이름을 보존하려고 alias 로 import 한다(common_utils 와 같은 관례).

모델 라우팅 3종(main / fast / 의미검증)이 여기 모여 있다 — 단계별로 다른 모델을 쓰는
결정이 한 곳에서만 내려지도록.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from string import Template
from typing import Any

import prompt_store


DEFAULT_RAG_LLM_LOG_DIR = Path(os.getenv("RAG_LLM_LOG_DIR", "logs/rag_llm"))


def _model_restricts_sampling(model: str | None) -> bool:
    """gpt-5·o-series 추론 모델은 Chat Completions 에서 temperature 기본값(1)만 허용한다.
    이런 모델에 temperature=0 등을 보내면 400(invalid_request)로 실패한다."""
    lowered = (model or "").lower()
    return lowered.startswith(("gpt-5", "o1", "o3", "o4"))


def _openai_chat_create(client: Any, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    """모델 호환 OpenAI Chat 호출 래퍼. 모든 chat.completions 호출은 이걸 거친다.

    - gpt-5/o-series 는 temperature!=1 미지원 → 제약 모델이면 temperature 를 떼고 기본값(1)을 쓴다.
    - 신모델은 max_tokens 대신 max_completion_tokens 를 요구 → 있으면 이관(구모델도 허용).
    이렇게 안 하면 이런 모델에서 전 LLM 단계가 400 으로 실패하고 규칙으로 조용히 폴백한다.
    """
    params = dict(kwargs)
    if "max_tokens" in params:
        params.setdefault("max_completion_tokens", params.pop("max_tokens"))
    if _model_restricts_sampling(model):
        params.pop("temperature", None)
        # 추론 모델은 기본 추론 깊이가 커서 느리다(재작성 1회 ~18s > 12s 타임아웃 → 규칙 폴백).
        # 이 파이프라인은 구조화 추출이 대부분이라 최소 추론으로 충분하고 빠르다(~4s). env 로 조절 가능.
        params.setdefault("reasoning_effort", os.getenv("OPENAI_REASONING_EFFORT", "minimal"))
    return client.chat.completions.create(model=model, messages=messages, **params)


STRUCTURING_MODEL_ENV = "OPENAI_STRUCTURING_MODEL"
REPAIR_MODEL_ENV = "OPENAI_REPAIR_MODEL"


def _structuring_llm_model(current: str | None) -> str | None:
    """QueryPlan v4 **1차 구조화** 모델. 기본은 빠른 모델(넓게 훑는 단계라 지연이 곱해진다).

    이 함수가 따로 있는 이유는 라우팅을 **선언적으로** 만들기 위해서다. 예전에는 이 호출이
    `_fast_llm_model` 을 그대로 타서, 설정이 `OPENAI_MODEL=gpt-5-mini` 여도 실제로는
    gpt-4o-mini 로 돌았고 그 사실이 어디에도 기록되지 않았다(실측 2026-08-02).
    이제 강등은 이 함수의 **선언**이고 로그에 requested/actual 이 함께 남는다.
    """
    if current is None:
        return None
    return os.getenv(STRUCTURING_MODEL_ENV) or _fast_llm_model(current) or current


def _repair_llm_model(current: str | None) -> str | None:
    """재방출(복구) 전용 모델 — 기본은 메인 모델.

    강한 모델은 **복구 계층에만** 쓴다. 1차에서 전부 강한 모델로 돌리면 비용·지연이 모든
    요청에 곱해지지만, 실제로 어려운 것은 검증기가 거절한 소수의 구간뿐이다.
    """
    if current is None:
        return None
    return os.getenv(REPAIR_MODEL_ENV) or current


def _fast_llm_model(current: str | None) -> str | None:
    """지연에 민감하거나 정확도가 중요한 경량 단계(재작성·타겟/채널 분리·상품추출·의미검증)용 모델.

    메인 OPENAI_MODEL 이 느린 추론모델(gpt-5 등)이면 이 단계들은 12s 타임아웃에 걸리거나(폴백) 최소
    추론에서 조건을 드롭해 가드에 반려된다. 그래서 이들만 빠르고 정확한 모델(기본 gpt-4o-mini)로 고정한다.
    current=None(규칙 모드)이면 그대로 None 을 돌려줘 LLM 을 건너뛴다. OPENAI_FAST_MODEL 로 조절 가능.
    """
    if current is None:
        return None
    return os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"


def _semantic_verify_model(current: str | None) -> str | None:
    """의미검증(최종 SQL↔원문 직접 대조) 전용 모델. 재작성·타겟분리·상품추출과 분리해 따로 지정할 수 있게
    한다(OPENAI_SEMANTIC_VERIFY_MODEL). 미지정이면 fast 모델을 그대로 쓴다. 규칙 모드(current=None)면 None."""
    if current is None:
        return None
    return os.getenv("OPENAI_SEMANTIC_VERIFY_MODEL") or _fast_llm_model(current)


def _rag_llm_log_enabled() -> bool:
    value = os.getenv("RAG_LLM_LOG_ENABLED", "true").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def _rag_llm_log_dir() -> Path:
    configured_dir = os.getenv("RAG_LLM_LOG_DIR")
    return Path(configured_dir) if configured_dir else DEFAULT_RAG_LLM_LOG_DIR


# 캠페인 생성(프롬프트 1건 = retrieve() 1회) 단위로 로그 파일을 분리하기 위한 실행 스코프.
# 값이 설정돼 있으면 해당 실행의 모든 이벤트가 같은 파일(<날짜>/<시각-해시>.jsonl)에 기록된다.
_rag_llm_run_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_rag_llm_run_path", default=None
)


@contextlib.contextmanager
def rag_llm_run_scope():
    """retrieve() 한 번을 하나의 캠페인 로그 파일로 묶는 컨텍스트."""
    if not _rag_llm_log_enabled():
        yield None
        return
    now = datetime.now().astimezone()
    run_key = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
    log_path = _rag_llm_log_dir() / now.date().isoformat() / f"{run_key}.jsonl"
    token = _rag_llm_run_path.set(log_path)
    try:
        yield log_path
    finally:
        _rag_llm_run_path.reset(token)


def _write_rag_llm_log(event: str, payload: dict[str, Any]) -> None:
    if not _rag_llm_log_enabled():
        return
    try:
        now = datetime.now().astimezone()
        log_path = _rag_llm_run_path.get()
        if log_path is None:
            # 실행 스코프 밖에서 호출된 경우 기존과 동일하게 날짜별 파일로 남긴다.
            log_path = _rag_llm_log_dir() / f"{now.date().isoformat()}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "event": event,
            **payload,
        }
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=_json_log_default) + "\n")
    except Exception as exc:
        print(f"rag_llm_log_failed:{exc.__class__.__name__}", flush=True)


def _json_log_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    return str(value)


def _message_summary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": message.get("role"),
            "content_length": len(str(message.get("content") or "")),
        }
        for message in messages
    ]


def _read_prompt_template(prompt_dir: Path | None, filename: str, fallback: str) -> str:
    # 1) DB(prompt_store 캐시) 우선
    db_template = _read_prompt_from_db(filename)
    if db_template:
        return db_template
    # 2) 파일(prompt_dir)
    if prompt_dir is not None:
        try:
            template = (prompt_dir / filename).read_text(encoding="utf-8").strip()
        except OSError:
            template = ""
        if template:
            return template
    # 3) 코드 내 하드코딩 fallback
    return fallback


def _read_prompt_from_db(filename: str) -> str | None:
    try:
        import prompt_store

        return prompt_store.get_template(filename)
    except Exception:  # noqa: BLE001 - DB 미가용 시 파일/하드코딩 fallback으로 진행
        return None


def _render_prompt_template(template: str, **values: str) -> str:
    return Template(template).safe_substitute(values)
