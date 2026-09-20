"""Model discovery: what a provider says about its own models, verbatim.

The marketplace form used to be filled from a hand-written preset table alone, so a
1M-context vision model landed on 128k/vision=0 because the name gave no hint.
These tests pin the normalisation across the field names each vendor uses, and the
rule that an unmentioned field stays ``None`` (never invented).
"""

from __future__ import annotations

from app.models.discovery import ModelFacts, model_facts


def test_stepfun_style_payload_is_read_in_full() -> None:
    facts = model_facts({
        "id": "step-5-preview",
        "max_input_tokens": 1024000,
        "enable_vision_input": True,
        "enable_reason": True,
        "reasoning_effort_support_list": ["low", "medium", "high"],
        "supported_protocols": ["chat", "messages", "responses"],
    })
    assert facts.context_window == 1024000
    assert facts.vision_input is True
    assert facts.reasoning is True
    assert facts.reasoning_effort == ("low", "medium", "high")
    assert facts.protocols == ("chat", "messages", "responses")
    assert facts.known


def test_vllm_style_aliases_are_recognised() -> None:
    facts = model_facts({
        "id": "qwen",
        "context_length": 32768,
        "supports_vision": False,
        "supported_parameters": ["temperature", "top_p", "max_tokens"],
    })
    assert facts.context_window == 32768
    assert facts.vision_input is False
    assert facts.supported_parameters == ("temperature", "top_p", "max_tokens")


def test_modalities_list_counts_as_vision() -> None:
    assert model_facts({"input_modalities": ["text", "image"]}).vision_input is True
    assert model_facts({"input_modalities": ["text"]}).vision_input is False


def test_missing_fields_stay_none_rather_than_guessed() -> None:
    """A bare ``{id, owned_by}`` entry - what NVIDIA / ModelScope answer with."""
    facts = model_facts({"id": "01-ai/yi-large", "owned_by": "01-ai"})
    assert facts.known is False
    assert facts.as_dict() == {
        "context_window": None,
        "vision_input": None,
        "reasoning": None,
    }


def test_bools_are_not_read_as_context_windows() -> None:
    # A vendor that spells "no limit" as max_tokens: true must not become 1.
    assert model_facts({"max_tokens": True}).context_window is None
    # ...and a string number is accepted.
    assert model_facts({"context_length": "65536"}).context_window == 65536


def test_describe_explains_each_fact_in_plain_language() -> None:
    rows = model_facts({
        "max_input_tokens": 1024000,
        "enable_vision_input": False,
        "enable_reason": True,
        "reasoning_effort_support_list": ["low", "high"],
    }).describe()
    by_label = {row["label"]: row for row in rows}
    assert by_label["上下文窗口"]["value"] == "1,024,000 tokens"
    assert by_label["图像输入"]["value"] == "不支持"
    assert by_label["思考模式"]["value"] == "支持"
    assert by_label["思考强度"]["value"] == "low / high"
    # Every row explains what the fact means, not just what it is called.
    assert all(len(row["meaning"]) > 10 for row in rows)


def test_describe_lists_only_supported_parameters() -> None:
    """The form shows a knob only when the provider says the model accepts it."""
    rows = model_facts({"supported_parameters": ["temperature", "stream"]}).describe()
    assert rows == [{
        "label": "可调参数",
        "value": "temperature、stream",
        "meaning": "只有这些请求参数该模型真正接受；表单只显示这些，其余一律默认",
    }]


def test_empty_and_none_payloads_are_safe() -> None:
    assert model_facts(None).known is False
    assert model_facts({}).known is False
    assert model_facts([]).known is False  # not a dict at all
    empty = ModelFacts()
    assert empty.describe() == []
