from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 조건 슬롯 LLM 보완은 테스트에서 기본 끔. 켜두면 OPENAI_API_KEY 가 있는 환경에서 스냅샷 테스트가
# 네트워크·모델 출력에 의존하게 되어 같은 입력에 다른 결과가 나온다(골든 코퍼스가 흔들린다).
# 이 경로의 동작은 tests/test_condition_slot_llm.py 가 추출기를 스텁으로 갈아끼워 검증한다.
os.environ["CONDITION_SLOT_LLM_FALLBACK"] = "off"

# 표면 신호 LLM 해석(의도/목적/문맥 신호어)도 같은 이유로 기본 끔 — 켜두면 골든 코퍼스가 모델 출력에
# 의존한다. 끈 상태에서는 동결 백스톱 낱말만으로 동작하므로 이관 전 결정론 동작이 그대로 재현된다.
# 이 경로의 동작은 tests/test_surface_lexicon_llm.py 가 추출기를 스텁으로 갈아끼워 검증한다.
os.environ["SURFACE_LEXICON_LLM"] = "off"
