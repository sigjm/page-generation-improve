from local_detail_page_ai.clients import LocalModelError, MlxServeChatClient


class FakeJsonTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, payload, timeout):
        self.calls.append((url, payload, timeout))
        return self.response


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "끝낸다",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def test_chat_with_tools_sends_tools_and_parses_tool_calls():
    transport = FakeJsonTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "finish",
                                    "arguments": '{"summary": "끝"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )
    client = MlxServeChatClient(
        base_url="http://model", model="qwen-text", transport=transport
    )

    reply = client.chat_with_tools([{"role": "user", "content": "안녕"}], TOOLS)

    url, payload, _ = transport.calls[0]
    assert url == "http://model/v1/chat/completions"
    assert payload["model"] == "qwen-text"
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert payload["temperature"] == 0
    assert "response_format" not in payload
    assert reply == {
        "content": "",
        "tool_calls": [
            {"id": "call-a", "name": "finish", "arguments": '{"summary": "끝"}'}
        ],
    }


def test_chat_with_tools_returns_text_when_model_skips_tools():
    transport = FakeJsonTransport(
        {"choices": [{"message": {"content": "도구 없이 답합니다"}}]}
    )

    reply = MlxServeChatClient(transport=transport).chat_with_tools([], TOOLS)

    assert reply == {"content": "도구 없이 답합니다", "tool_calls": []}


def test_chat_with_tools_fills_missing_ids_and_serializes_object_arguments():
    transport = FakeJsonTransport(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "finish", "arguments": {"summary": "x"}}}
                        ]
                    }
                }
            ]
        }
    )

    reply = MlxServeChatClient(transport=transport).chat_with_tools([], TOOLS)

    assert reply["tool_calls"] == [
        {"id": "call-0", "name": "finish", "arguments": '{"summary": "x"}'}
    ]


def test_chat_with_tools_rejects_response_without_message():
    transport = FakeJsonTransport({"choices": []})

    try:
        MlxServeChatClient(transport=transport).chat_with_tools([], TOOLS)
    except LocalModelError:
        pass
    else:
        raise AssertionError("a response without a message must raise LocalModelError")


def test_local_model_errors_are_model_call_errors():
    from detail_page_ai.ports import ModelCallError

    assert issubclass(LocalModelError, ModelCallError)
