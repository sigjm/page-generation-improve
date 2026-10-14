import subprocess
from pathlib import Path

import pytest

from detail_page_ai.finetune.codex import CodexJsonClient, CodexError


class FakeRun:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def __call__(self, args, *, input, capture_output, text, timeout):
        out = Path(args[args.index("-o") + 1])
        image = Path(args[args.index("-i") + 1]) if "-i" in args else None
        self.calls.append({"args": args, "input": input, "image": image.read_bytes() if image else None})
        out.write_text(self.reply, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_codex_client_sends_the_prompt_and_image_and_parses_fenced_json():
    run = FakeRun('```json\n{"answer": "문제없습니다.", "has_problem": false}\n```')
    client = CodexJsonClient(run=run)

    reply = client.generate_json("검수하라", image=b"\x89PNG", mime_type="image/png")

    assert reply == {"answer": "문제없습니다.", "has_problem": False}
    call = run.calls[0]
    assert "검수하라" in call["input"] and call["image"] == b"\x89PNG"
    args = call["args"]
    assert args[:2] == ["codex", "exec"]
    assert {"-s", "read-only", "--ephemeral", "--skip-git-repo-check"} <= set(args)
    assert "service_tier=priority" in args and "model_reasoning_effort=xhigh" in args


def test_codex_client_raises_when_the_reply_is_not_json():
    client = CodexJsonClient(run=FakeRun("모르겠습니다"))

    with pytest.raises(CodexError):
        client.generate_json("JSON으로 답하라")
