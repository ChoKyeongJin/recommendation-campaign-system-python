# 사전적재 - 샘플 데이터

## 캠페인, 사용자 샘플 데이터 적재

### 요청사항

- 파이썬으로 개발
- 입력 데이터 형식은 json
- RAG 인제션용 Python 전처리 코드를 작성해줘. HTML 제거, 특수문자 제거, Unicode 정규화, 공백 정리, 중복 문장 제거를 포함하고 pandas DataFrame을 입력받아 text 컬럼을 정제하도록 만들어줘

### 구현 파일

- `rag_index.py`

JSON의 `nodes` 배열을 읽어 다음 작업을 수행한다.

- pandas DataFrame의 `text` 컬럼 RAG 전처리
- HTML 제거, 특수문자 제거, Unicode 정규화, 공백 정리, 중복 문장 제거
- campaign/user 노드 검증
- 정제된 `text_for_embedding` 기반 임베딩 생성
- 원본 노드 필드와 추천 edge를 Qdrant payload로 보존
- Qdrant 벡터 DB에 campaign/user RAG 노드 인덱싱

### RAG 벡터 DB

RAG 벡터 DB는 Qdrant를 사용한다.

- 로컬 기본 URL: `http://localhost:6333`
- 기본 컬렉션: `campaign_user_rag_nodes`
- 실행 파일: `rag_index.py`

Qdrant 로컬 실행:

```bash
docker compose up -d qdrant
```

### 입력형식

```json
{
  "nodes": [
    {
      "id": "camp_001",
      "type": "campaign",
      "name": "여름 장바구니 리마인드",
      "objective": "purchase",
      "category": "fashion",
      "channel": [
        "kakao",
        "app_push"
      ],
      "target_segments": [
        "20s_female",
        "cart_abandoner",
        "price_sensitive"
      ],
      "offer": "10% 할인 쿠폰",
      "budget_krw": 3000000,
      "start_date": "2026-07-15",
      "end_date": "2026-07-31",
      "keywords": [
        "여름",
        "의류",
        "쿠폰",
        "장바구니"
      ],
      "expected_ctr": 3.8,
      "expected_cvr": 5.2,
      "text_for_embedding": "여름 장바구니 리마인드 캠페인. 목적은 purchase이고 카테고리는 fashion입니다. 타겟 세그먼트는 20s_female, cart_abandoner, price_sensitive이며 채널은 kakao, app_push입니다. 혜택은 10% 할인 쿠폰입니다. 키워드: 여름, 의류, 쿠폰, 장바구니."
    },
	{
      "id": "user_001",
      "type": "user",
      "age": 27,
      "gender": "female",
      "region": "Seoul",
      "lifecycle": "active",
      "interests": [
        "fashion",
        "beauty",
        "travel"
      ],
      "preferred_channels": [
        "app_push",
        "kakao"
      ],
      "avg_order_value_krw": 68000,
      "purchase_count_90d": 4,
      "last_active_days": 1,
      "price_sensitivity": "high",
      "predicted_ltv_segment": "mid",
      "recent_behaviors": [
        "cart_abandoned:fashion",
        "clicked:coupon"
      ],
      "text_for_embedding": "user_001 사용자는 27세 female이며 지역은 Seoul입니다. 라이프사이클은 active이고 관심사는 fashion, beauty, travel입니다. 선호 채널은 app_push, kakao이며 최근 행동은 cart_abandoned:fashion, clicked:coupon입니다. 가격 민감도는 high, LTV 세그먼트는 mid입니다."
    },
]
```

추천 학습/검색용 edge가 있는 경우 `recommendation_edges` 배열을 함께 입력할 수 있다.

```json
{
  "nodes": [],
  "recommendation_edges": [
    {
      "user_id": "user_001",
      "campaign_id": "camp_001",
      "reason": "fashion cart_abandoner price_sensitive channel match",
      "label": "high"
    }
  ]
}
```

### 실행 예시

DataFrame text 컬럼 정제:

```python
import pandas as pd

from rag_index import clean_text_dataframe

df = pd.DataFrame(
  {
    "text": [
      "<p>여름&nbsp;쿠폰!</p> 여름 쿠폰! ###",
      "Ａ  Ｂ<script>remove()</script>  C@@",
    ]
  }
)

cleaned_df = clean_text_dataframe(df)
```

입력 JSON 검증:

```bash
python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --validate-only
```

Qdrant 인덱싱:

```bash
python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --recreate
```

campaign 노드만 인덱싱:

```bash
python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --node-type campaign --recreate
```

전처리를 건너뛰고 원문 `text_for_embedding`을 그대로 인덱싱:

```bash
python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --no-clean-text
```

Docker Compose 환경에서 실행:

```bash
docker compose up -d qdrant
docker compose run --rm python python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json --recreate
```

원격 Qdrant를 사용할 때는 환경 변수 또는 옵션으로 접속 정보를 지정한다.

```bash
set QDRANT_URL=http://localhost:6333
set QDRANT_RAG_COLLECTION=campaign_user_rag_nodes
python rag_index.py docs/data/campaign_user_rag_sample_50_with_edges.json
```
