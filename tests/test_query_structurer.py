import json

from query_structurer import LLMQueryStructurer, QueryStructuringInput, StructuringContext


class ResponseSequence:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages = []

    def __call__(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return self.responses.pop(0)


def _structure(query, payload, *, context=None):
    completion = ResponseSequence([json.dumps(payload, ensure_ascii=False)])
    structurer = LLMQueryStructurer(completion)
    result = structurer.structure(
        QueryStructuringInput(
            query=query,
            context=context or StructuringContext(current_date="2026-07-27", timezone="Asia/Seoul"),
        )
    )
    return result, completion


def _payload(query, **overrides):
    payload = {
        "originalQuery": query,
        "normalizedQuery": query,
        "intent": "fact_lookup",
        "complexity": "simple",
        "subjects": [],
        "constraints": {"metadata": []},
        "informationNeeds": [{"id": "need_1", "target": "answer", "subjectRefs": [], "description": query}],
        "operations": ["find"],
        "dependencies": [],
        "plannerHints": {
            "requiresMultipleRetrievals": False,
            "requiresSequentialExecution": False,
            "requiresComparison": False,
            "requiresAggregation": False,
            "reason": "단일 정보를 조회하면 된다.",
        },
    }
    payload.update(overrides)
    return payload


def test_simple_question_keeps_a_single_information_need():
    query = "결제 시스템 담당 부서는 어디야?"
    result, _ = _structure(
        query,
        _payload(
            query,
            normalizedQuery="결제 시스템 담당 부서",
            subjects=[{"name": "결제 시스템", "type": "system"}],
            informationNeeds=[
                {
                    "id": "need_1",
                    "target": "responsible_department",
                    "subjectRefs": ["결제 시스템"],
                    "description": "결제 시스템 담당 부서",
                }
            ],
        ),
    )

    assert result.complexity == "simple"
    assert len(result.information_needs) == 1
    assert result.planner_hints.requires_multiple_retrievals is False


def test_compound_conditions_are_normalized_and_sequential():
    query = "2025년 결제 장애 중 복구에 2시간 이상 걸린 사례의 원인과 재발 방지책을 비교해줘."
    result, _ = _structure(
        query,
        _payload(
            query,
            normalizedQuery="2025년 결제 장애 중 복구 시간이 120분 이상인 사례의 원인과 재발 방지책 비교",
            intent="comparison",
            complexity="complex",
            subjects=[{"name": "결제 장애", "type": "incident"}],
            constraints={
                "timeRange": {"from": "2025-01-01", "to": "2025-12-31", "originalExpression": "2025년"},
                "metadata": [{"field": "recoveryTimeMinutes", "expression": "복구에 2시간 이상", "operator": "gte", "value": 120}],
            },
            informationNeeds=[
                {"id": "need_1", "target": "matching_incidents", "subjectRefs": ["결제 장애"], "description": "조건에 맞는 결제 장애 사례"},
                {"id": "need_2", "target": "root_cause", "subjectRefs": ["결제 장애"], "description": "각 사례의 원인"},
                {"id": "need_3", "target": "preventive_action", "subjectRefs": ["결제 장애"], "description": "각 사례의 재발 방지책"},
            ],
            operations=["find", "filter", "compare"],
            dependencies=[
                {"from": "need_1", "to": "need_2", "reason": "사례를 먼저 확정해야 한다."},
                {"from": "need_1", "to": "need_3", "reason": "사례를 먼저 확정해야 한다."},
            ],
            plannerHints={
                "requiresMultipleRetrievals": True,
                "requiresSequentialExecution": True,
                "requiresComparison": True,
                "requiresAggregation": False,
                "reason": "사례 선정 후 원인과 재발 방지책을 비교한다.",
            },
        ),
    )

    assert result.constraints.time_range.from_date == "2025-01-01"
    assert result.constraints.time_range.to_date == "2025-12-31"
    assert result.constraints.metadata[0].value == 120
    assert [need.target for need in result.information_needs] == ["matching_incidents", "root_cause", "preventive_action"]
    assert "compare" in result.operations
    assert result.planner_hints.requires_sequential_execution is True


def test_parallel_comparison_has_independent_information_needs():
    query = "DB 장애와 API 장애의 원인과 대응 방식을 비교해줘."
    result, _ = _structure(
        query,
        _payload(
            query,
            normalizedQuery="DB 장애와 API 장애의 원인 및 대응 방식 비교",
            intent="comparison",
            complexity="moderate",
            subjects=[{"name": "DB 장애", "type": "incident"}, {"name": "API 장애", "type": "incident"}],
            informationNeeds=[
                {"id": "need_1", "target": "root_cause", "subjectRefs": ["DB 장애"], "description": "DB 장애의 원인"},
                {"id": "need_2", "target": "response_action", "subjectRefs": ["DB 장애"], "description": "DB 장애의 대응 방식"},
                {"id": "need_3", "target": "root_cause", "subjectRefs": ["API 장애"], "description": "API 장애의 원인"},
                {"id": "need_4", "target": "response_action", "subjectRefs": ["API 장애"], "description": "API 장애의 대응 방식"},
            ],
            operations=["find", "compare"],
            plannerHints={
                "requiresMultipleRetrievals": True,
                "requiresSequentialExecution": False,
                "requiresComparison": True,
                "requiresAggregation": False,
                "reason": "각 장애 정보를 독립적으로 조회한 뒤 비교한다.",
            },
        ),
    )

    assert [subject.name for subject in result.subjects] == ["DB 장애", "API 장애"]
    assert len(result.information_needs) == 4
    assert result.planner_hints.requires_multiple_retrievals is True
    assert result.planner_hints.requires_sequential_execution is False


def test_multi_hop_question_uses_absolute_date_and_dependencies():
    query = "지난해 가장 오래 지속된 장애를 찾고 해당 장애의 원인과 담당 부서 후속 조치를 알려줘."
    result, _ = _structure(
        query,
        _payload(
            query,
            normalizedQuery="지난해 가장 오래 지속된 장애와 해당 장애의 원인 및 담당 부서 후속 조치",
            intent="multi_hop",
            complexity="complex",
            subjects=[{"name": "장애", "type": "incident"}, {"name": "담당 부서", "type": "organization"}],
            constraints={"timeRange": {"from": "2025-01-01", "to": "2025-12-31", "originalExpression": "지난해"}, "metadata": []},
            informationNeeds=[
                {"id": "need_1", "target": "longest_incident", "subjectRefs": ["장애"], "description": "지난해 가장 오래 지속된 장애"},
                {"id": "need_2", "target": "root_cause", "subjectRefs": ["장애"], "description": "선정 장애의 원인"},
                {"id": "need_3", "target": "responsible_department", "subjectRefs": ["장애"], "description": "선정 장애의 담당 부서"},
                {"id": "need_4", "target": "follow_up_action", "subjectRefs": ["담당 부서"], "description": "담당 부서의 후속 조치"},
            ],
            operations=["find", "rank", "explain"],
            dependencies=[
                {"from": "need_1", "to": "need_2", "reason": "장애 선정 후 원인을 찾는다."},
                {"from": "need_1", "to": "need_3", "reason": "장애 선정 후 담당 부서를 찾는다."},
                {"from": "need_3", "to": "need_4", "reason": "담당 부서 확인 후 후속 조치를 찾는다."},
            ],
            plannerHints={
                "requiresMultipleRetrievals": True,
                "requiresSequentialExecution": True,
                "requiresComparison": False,
                "requiresAggregation": True,
                "reason": "대상 선정 뒤 순차적으로 관련 정보를 확인한다.",
            },
        ),
    )

    assert result.constraints.time_range.from_date == "2025-01-01"
    assert result.information_needs[0].target == "longest_incident"
    assert {(dependency.from_id, dependency.to_id) for dependency in result.dependencies} == {
        ("need_1", "need_2"),
        ("need_1", "need_3"),
        ("need_3", "need_4"),
    }
    assert result.planner_hints.requires_sequential_execution is True


def test_conversation_reference_uses_only_supplied_context():
    query = "그중 복구가 가장 오래 걸린 장애의 원인이 뭐야?"
    context = StructuringContext(
        current_date="2026-07-27",
        timezone="Asia/Seoul",
        conversation_context="결제 시스템 장애 사례를 조회했다.",
    )
    result, completion = _structure(
        query,
        _payload(
            query,
            normalizedQuery="결제 시스템 장애 사례 중 복구 시간이 가장 긴 장애의 원인",
            intent="multi_hop",
            complexity="complex",
            subjects=[{"name": "결제 시스템 장애 사례", "type": "incident"}],
            informationNeeds=[
                {"id": "need_1", "target": "longest_recovery_incident", "subjectRefs": ["결제 시스템 장애 사례"], "description": "복구 시간이 가장 긴 결제 시스템 장애"},
                {"id": "need_2", "target": "root_cause", "subjectRefs": ["결제 시스템 장애 사례"], "description": "선정 장애의 원인"},
            ],
            operations=["find", "rank", "explain"],
            dependencies=[{"from": "need_1", "to": "need_2", "reason": "장애를 먼저 선정해야 원인을 조회할 수 있다."}],
            plannerHints={
                "requiresMultipleRetrievals": True,
                "requiresSequentialExecution": True,
                "requiresComparison": False,
                "requiresAggregation": True,
                "reason": "문맥의 장애 사례 중 대상을 선정한 뒤 원인을 확인한다.",
            },
        ),
        context=context,
    )

    assert result.subjects[0].name == "결제 시스템 장애 사례"
    assert [(dependency.from_id, dependency.to_id) for dependency in result.dependencies] == [("need_1", "need_2")]
    assert result.output_preference is None
    assert '"currentDate": "2026-07-27"' in completion.messages[0][1]["content"]
    assert '"timezone": "Asia/Seoul"' in completion.messages[0][1]["content"]
    assert "결제 시스템 장애 사례를 조회했다." in completion.messages[0][1]["content"]


def test_invalid_llm_json_retries_twice_then_returns_fallback():
    query = "복잡한 질문"
    completion = ResponseSequence(["not json", "[]", "{\"unexpected\": true}"])
    result = LLMQueryStructurer(completion).structure(
        QueryStructuringInput(query=query, context=StructuringContext(current_date="2026-07-27"))
    )

    assert completion.calls == 3
    assert result.to_dict() == {
        "originalQuery": query,
        "normalizedQuery": query,
        "intent": "unknown",
        "complexity": "simple",
        "subjects": [],
        "constraints": {"metadata": []},
        "informationNeeds": [{"id": "need_1", "target": "answer", "subjectRefs": [], "description": query}],
        "operations": ["find"],
        "dependencies": [],
        "plannerHints": {
            "requiresMultipleRetrievals": False,
            "requiresSequentialExecution": False,
            "requiresComparison": False,
            "requiresAggregation": False,
            "reason": "질문 구조화에 실패하여 원본 질문을 그대로 전달한다.",
        },
    }
    assert "Validation Error" in completion.messages[1][-1]["content"]