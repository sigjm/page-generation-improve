"""Codex CLI as the dataset's teacher: sorts OCR sentences and answers image-inspection questions.

Same interface as the local chat clients (``generate_json``), so it plugs into the structuring
step and into the agent's inspect_* tools. Never part of the runtime agent.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PREAMBLE = (
    "You are a data-labeling function. Do not run commands or read files. "
    "Answer the task below with only the JSON it asks for.\n\n"
)
SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class CodexError(RuntimeError):
    pass


class CodexJsonClient:
    def __init__(self, *, reasoning: str = "xhigh", service_tier: str = "priority", timeout: float = 900, run=subprocess.run):
        self.reasoning = reasoning
        self.service_tier = service_tier  # "priority" is Codex's Fast tier
        self.timeout = timeout
        self.run = run

    def generate_json(self, prompt: str, *, image: bytes | None = None, mime_type: str | None = None, json_schema=None) -> dict[str, Any]:
        del json_schema  # The prompt carries the schema; callers validate with Pydantic.
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "reply.txt"
            args = [
                "codex", "exec", "-s", "read-only", "--ephemeral", "--skip-git-repo-check", "-C", work,
                "-c", f"model_reasoning_effort={self.reasoning}", "-c", f"service_tier={self.service_tier}",
                "-o", str(out),
            ]
            if image:
                path = Path(work) / f"image{SUFFIX.get(mime_type or '', '.png')}"
                path.write_bytes(image)
                args += ["-i", str(path)]
            result = self.run(args, input=PREAMBLE + prompt, capture_output=True, text=True, timeout=self.timeout)
            if result.returncode != 0 or not out.exists():
                raise CodexError(f"codex exec failed ({result.returncode}): {result.stderr[-300:]}")
            return _parse(out.read_text(encoding="utf-8"))


def _parse(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise CodexError(f"reply is not JSON: {text[:200]}") from exc
    if not isinstance(value, dict):
        raise CodexError(f"reply is not a JSON object: {text[:200]}")
    return value
