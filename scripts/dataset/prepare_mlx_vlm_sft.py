"""Turn the dataset into mlx-vlm SFT splits for one target model.

mlx-vlm renders ``messages`` without ``tools`` and trains only the last assistant turn. So agent
conversations are split into one sample per assistant turn, and the tool definitions are baked
into the system message exactly as the model's chat template would render them at runtime.
Every sample is checked: mlx-vlm's rendering must equal the runtime rendering with tools.

Run in the training environment (mlx-vlm installed):
    PYTHONPATH=src <mlx-vlm-venv>/bin/python scripts/dataset/prepare_mlx_vlm_sft.py \
        --model ~/.mlx-serve/models/empero-ai/Qwen3.8-9B-Distill-MLX-4bit
"""

import argparse
import json
from pathlib import Path

from detail_page_ai.finetune.export import turn_samples

ROOT = Path(".local/finetune/kcdf/dataset")
SYSTEM_START, END = "<|im_start|>system\n", "<|im_end|>\n"


def bake_tools(messages: list[dict], tools: list[dict], tokenizer) -> list[dict]:
    """The system message with the template's own tool block, so no ``tools`` argument is needed."""
    if messages[0]["role"] != "system":
        raise ValueError("agent samples start with the system prompt")
    # The template refuses to render without a user turn, so render system + first user and cut.
    rendered = tokenizer.apply_chat_template(messages[:2], tools=tools, tokenize=False)
    if not rendered.startswith(SYSTEM_START):
        raise ValueError(f"unexpected system rendering: {rendered[:80]!r}")
    return [{"role": "system", "content": rendered[len(SYSTEM_START):rendered.index(END)]}, *messages[1:]]


def main() -> int:
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config, load_processor

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    model = args.model.expanduser()
    config = load_config(model)
    processor = load_processor(model)
    tokenizer = processor.tokenizer
    out = args.root / "mlx-vlm"
    out.mkdir(parents=True, exist_ok=True)

    counts = {}
    for split, name in (("train", "train"), ("val", "validation")):
        rows = []
        for line in (args.root / "vlm" / f"{split}.jsonl").open(encoding="utf-8"):
            row = json.loads(line)
            row["images"] = [str((args.root / image).resolve()) for image in row["images"]]
            rows.append(row)
        for line in (args.root / "agent" / f"{split}.jsonl").open(encoding="utf-8"):
            for sample in turn_samples(json.loads(line)):
                baked = bake_tools(sample["messages"], sample["tools"], tokenizer)
                runtime = tokenizer.apply_chat_template(sample["messages"], tools=sample["tools"], tokenize=False)
                trained = apply_chat_template(processor, config, baked, add_generation_prompt=False)
                if trained != runtime:
                    raise SystemExit(f"{sample['id']}: mlx-vlm rendering differs from the runtime rendering")
                rows.append({"id": sample["id"], "messages": baked})
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[name] = len(rows)
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
