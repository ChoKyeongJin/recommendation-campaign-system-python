# 사전적재 - 동의어 및 정규화 사전

## 캠페인 RAG/그래프 추천 전처리용 동의어 및 정규화 사전

### 요청사항

- 파이썬으로 개발
- 입력 데이터 형식은 json

### 구현 파일

- `ingest.py`

JSON 정규화 사전을 읽어 다음 작업을 수행한다.

- 단일 용어 정규화
- JSON 용어 목록 정규화
- 텍스트 안의 동의어/부정 동의어를 canonical 값으로 치환
- 사전 인덱스 출력
- Qdrant 벡터 DB에 동의어 사전 용어 인덱싱

### RAG 벡터 DB

RAG 벡터 DB는 Qdrant를 사용한다.

- 로컬 기본 URL: `http://localhost:6333`
- 기본 컬렉션: `campaign_normalization_terms`
- 실행 파일: `qdrant_index.py`

Qdrant 로컬 실행:

```bash
docker compose up -d qdrant
```

### 입력형식

```json
{
  "version": "1.0",
  "description": "캠페인 RAG/그래프 추천 전처리용 동의어 및 정규화 사전",
  "normalization_rules": [
    {
      "rule_id": "gender_male",
      "canonical": "male",
      "ko_label": "남성",
      "synonyms": [
        "남성",
        "남자",
        "남",
        "남자 고객",
        "남성 고객",
        "male",
        "man",
        "men",
        "m"
      ],
      "negative_synonyms": [
        "여자가 아닌 사람",
        "여성이 아닌 사람",
        "female이 아닌 사람",
        "not female"
      ]
    }
  ]
}
```

### 실행 예시

```bash
python ingest.py docs/data/normalization_rules.sample.json --term "남자 고객"
```

```bash
python ingest.py docs/data/normalization_rules.sample.json --text "남자 고객과 not female 대상 캠페인"
```

```bash
python ingest.py docs/data/normalization_rules.sample.json --dump-index
```

용어 목록은 JSON 배열 또는 `terms` 배열을 가진 객체로 입력한다.

```json
["남성", "female이 아닌 사람", "알 수 없음"]
```

```bash
python ingest.py docs/data/normalization_rules.sample.json --terms-file terms.json --output normalized_terms.json
```

Qdrant 인덱싱 예시:

```bash
python qdrant_index.py docs/data/normalization_rules.sample.json --recreate
```

원격 Qdrant를 사용할 때는 환경 변수 또는 옵션으로 접속 정보를 지정한다.

```bash
set QDRANT_URL=http://localhost:6333
set QDRANT_COLLECTION=campaign_normalization_terms
python qdrant_index.py docs/data/normalization_rules.sample.json
```
