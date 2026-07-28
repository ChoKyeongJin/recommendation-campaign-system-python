import networkx as nx

import graph_rag as g
from query_structurer import build_fallback


class _RecordingStructurer:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def structure(self, input):
        self.queries.append(input.query)
        return build_fallback(input.query)


def test_delivery_channel_suffix_never_enters_targeting_pipeline(monkeypatch):
    targeting = "장바구니에 상품을 담고 결제하지 않은 고객에게 재구매를 유도하고 싶어요"
    query = targeting + "\n발송 채널: RCS (리치 메시지, 버튼 및 이미지 지원)"
    structurer = _RecordingStructurer()
    monkeypatch.setattr(
        g,
        "_verify_sql_semantics",
        lambda *_args, **_kwargs: {"ran": True, "faithful": True, "issues": []},
    )

    result = g.retrieve(
        query=query,
        graph=nx.Graph(),
        collection="unused",
        url="http://unused",
        api_key=None,
        embedding_model_name="unused",
        vector_top_k=0,
        keyword_top_k=0,
        graph_top_k=1,
        hops=0,
        query_parser="rules",
        retrieval_scope="targeting",
        generate_answer=False,
        generate_messages=False,
        query_structurer=structurer,
    )

    assert structurer.queries == [targeting]
    assert result["structured_query"]["originalQuery"] == targeting
    assert result["prompt_normalization"]["original"] == targeting
    assert result["query_plan"]["raw_query"] == query
    assert result["query_plan"]["original_query"] == targeting
    assert result["query_plan"]["campaign_constraints"]["channels"] == []
    assert result["query_plan"]["target_user"]["preferred_channels"] == []
    assert result["sql_result"]["is_success"] is True

