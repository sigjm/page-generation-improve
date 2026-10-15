"""Check that a local text server answers with OpenAI-style tool calls.

    PYTHONPATH=src python scripts/runtime/smoke_tool_calling.py \
        --url http://127.0.0.1:11234 --model ddalcu/Qwen3.8-27B-MLX-Serve-4bit

Exits 0 when the reply calls `record_color` with a JSON object argument.
"""

import argparse
import json
import sys

from local_detail_page_ai.clients import LocalModelError, MlxServeChatClient

TOOL = {
    "type": "function",
    "function": {
        "name": "record_color",
        "description": "색 이름 하나를 기록한다.",
        "parameters": {
            "type": "object",
            "properties": {"color": {"type": "string"}},
            "required": ["color"],
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    client = MlxServeChatClient(base_url=args.url, model=args.model, timeout=args.timeout)
    try:
        reply = client.chat_with_tools(
            [{"role": "user", "content": "하늘색을 record_color 도구로 기록해."}],
            [TOOL],
        )
    except LocalModelError as exc:
        print(f"FAIL request: {exc}")
        return 1
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    calls = [call for call in reply["tool_calls"] if call["name"] == "record_color"]
    if not calls:
        print("FAIL: no record_color tool call")
        return 1
    try:
        arguments = json.loads(calls[0]["arguments"])
    except json.JSONDecodeError:
        print("FAIL: arguments are not JSON")
        return 1
    if not isinstance(arguments, dict) or not arguments.get("color"):
        print("FAIL: arguments have no color")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
